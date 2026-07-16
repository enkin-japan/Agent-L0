"""edge 依赖瘦身金测试（EXECUTION_LEDGER R5-2 / D.14）。

守的是**「edge 装不上就跑不起来」的静态前提**：整条音频路已移设备 A，edge 的
`requirements-edge.txt` 里没有 torch/librosa/mlx。这类退化极其安静——开发机的
`.venv` 什么都有，谁在顶层多写一句 `import torch`，本机照跑不误，**只有打包到
edge 时才炸**，而那时离引入已经很远了。

故这里不测"能不能跑"（开发机跑不出 edge 的缺依赖），只测**源码有没有把重依赖
钉回顶层**——这条在开发机上就能测，且正是会漂移的那条。
"""
import ast
import pathlib

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parent.parent

#: 这些包不在 requirements-edge.txt 里 → edge 上 import 即 ImportError。
_NOT_ON_EDGE = {"torch", "torchaudio", "torchvision", "torchcodec",
                "librosa", "soundfile", "silero_vad",
                "mlx", "mlx_lm", "mlx_vlm", "mlx_audio", "mlx_whisper",
                "numba", "scipy", "sklearn", "cv2"}

#: 允许顶层 import 上述包的文件：backends/mlx/* 是设备 A 的实现（edge 不 import 它，
#: 由 get_backend_class / get_asr_provider 惰性取用）。
_A_SIDE_ONLY = ("framework/backends/mlx/",)


def _toplevel_imports(path: pathlib.Path):
    """只收模块顶层的 import（函数体内的惰性 import 不算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:                      # 仅遍历顶层
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _edge_sources():
    for p in sorted(_BACKEND.glob("framework/**/*.py")):
        rel = p.relative_to(_BACKEND).as_posix()
        if any(rel.startswith(a) for a in _A_SIDE_ONLY):
            continue
        yield p
    yield _BACKEND / "app.py"


def test_no_edge_source_toplevel_imports_a_side_deps():
    """edge 侧源码不得在**顶层** import 只装在设备 A 的包。

    实际抓到过两处：`app.py` 的 `from silero_vad import load_silero_vad` 与
    `utils/file_handler.py` 的 `import torch/librosa/soundfile` ——两者都只服务
    原生音频直喂（modalities 含 audio，config 默认不含），却让 torch(385M) 无条件
    进内存、且让 edge 打包非背整条音频路不可。
    """
    offenders = []
    for p in _edge_sources():
        bad = _toplevel_imports(p) & _NOT_ON_EDGE
        if bad:
            offenders.append(f"{p.relative_to(_BACKEND).as_posix()}: {sorted(bad)}")
    assert not offenders, (
        "以下 edge 源码在顶层 import 了设备 A 才有的包（edge 上必 ImportError）：\n  "
        + "\n  ".join(offenders)
        + "\n改为函数体内惰性 import，或确认该功能本就不该在 edge 执行。")


def test_edge_requirements_excludes_the_audio_and_inference_stack():
    """`requirements-edge.txt` 里不得出现推理/音频依赖——它就是 edge 的定义。"""
    req = (_BACKEND / "requirements-edge.txt")
    assert req.is_file(), "requirements-edge.txt 不存在（R5-2 的交付物）"
    pinned = set()
    for line in req.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pinned.add(line.split("==")[0].split(">=")[0].split("[")[0].strip().lower())
    for banned in ("torch", "torchaudio", "torchvision", "torchcodec", "librosa",
                   "soundfile", "silero_vad", "mlx", "mlx-lm", "mlx-vlm",
                   "mlx-audio", "mlx-whisper", "mlx-metal"):
        assert banned not in pinned, f"{banned} 不该在 edge 清单里（D.14：整条音频/推理路在设备 A）"


def test_edge_requirements_keeps_what_edge_actually_runs():
    """反向守卫：别把 edge 真要执行的东西一起砍了。
    transformers 尤其危险——它看着像「模型库」，但 mode=remote 下 backend 仍要在本机
    build prompt / 数 token（README:66）。"""
    text = (_BACKEND / "requirements-edge.txt").read_text(encoding="utf-8").lower()
    for keep in ("transformers", "playwright", "trafilatura", "pymupdf",
                 "markitdown", "sympy", "matplotlib", "llguidance"):
        assert keep in text, f"{keep} 是 edge 真要跑的（见清单头注「刻意保留」），不该缺席"


@pytest.mark.parametrize("provider", ["mlx_whisper", "remote"])
def test_both_asr_providers_share_one_contract(provider):
    """两个 provider 是彼此的替身：签名必须一致，否则换 provider 就炸在调用点。
    区间切片（start/end）已下放到 provider——本地用 librosa 切、remote 让 A 切。"""
    import inspect
    from framework.backends import get_asr_provider
    fn = get_asr_provider(provider)
    params = inspect.signature(fn).parameters
    assert set(params) == {"src", "model", "language", "start", "end"}, \
        f"{provider} 的转写签名与另一个 provider 不一致"


def test_auto_provider_follows_inference_mode(monkeypatch):
    """auto = 跟随 inference.mode。两个旋钮各说各话的症状很隐蔽：mode=remote 却把
    asr 留在 mlx_whisper，edge 白背 torch 全家且**没有任何报错**。"""
    import framework.backends as B
    monkeypatch.setattr(B, "load_section", lambda *_: {"mode": "remote"}, raising=False)
    import framework.utils.config_loader as CL
    monkeypatch.setattr(CL, "load_section", lambda name: {"mode": "remote"})
    assert B._resolve_auto() == "remote"
    monkeypatch.setattr(CL, "load_section", lambda name: {"mode": "inprocess"})
    assert B._resolve_auto() == "mlx_whisper"
