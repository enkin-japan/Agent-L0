"""推理后端注册面（HETERO_COMPUTE P0/D7）。

物理承载相关的名字（MLX / CUDA / 远程，含各家 ASR provider 与默认模型名）
全部收编在 backends/ 之下；框架其余部分只经本模块的工厂函数取用。
惰性导入：具体后端的重型依赖只在真正选用时才加载。
"""

# ---- 推理后端 ----

DEFAULT_BACKEND = "mlx"


def get_backend_class(name: str = ""):
    """按名字返回 InferenceBackend 实现类（D7 目录约定：backends/<name>/）。"""
    name = name or DEFAULT_BACKEND
    if name == "mlx":
        from .mlx.backend import MLXBackend
        return MLXBackend
    if name == "remote":
        from .remote.backend import RemoteBackend
        return RemoteBackend
    raise ValueError(f"未知推理后端: {name!r}（可用: mlx, remote）")


# ---- ASR provider（ingestion/parsers/asr.py 经此取转写实现）----

#: 缺省 = 跟随推理承载（见 _resolve_auto）。**刻意不是某个具体 provider**：
#: `inference.mode` 与 `ingestion.asr_provider` 是两个旋钮，各说各话时症状很隐蔽——
#: mode=remote（推理在 A）却把 asr 留在 mlx_whisper，edge 就得为一个用不上的
#: 转写实现继续背 torch/librosa 全家（R5-2 的瘦身直接白做，而且没有任何报错）。
DEFAULT_ASR_PROVIDER = "auto"
# 当前默认 provider 的默认模型（provider 专属命名，故收编于此）
DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"

_ASR_PROVIDERS = ("auto", "mlx_whisper", "remote")


class AsrProviderUnavailable(RuntimeError):
    """provider 依赖缺失/不可用（调用方 fail-open 成 unavailable 标记文本）。"""


def _resolve_auto() -> str:
    """auto → 具体 provider：推理在哪，转写就在哪。

    edge（`inference.mode: remote`）= 权重在设备 A，本机连一个字都生成不出来——
    那么"音频离线可用"本就不成立，转写留在 edge 只剩体积代价（D.14）。
    """
    from ..utils.config_loader import load_section
    mode = str((load_section("inference") or {}).get("mode", "inprocess"))
    return "remote" if mode == "remote" else "mlx_whisper"


def get_asr_provider(name: str = ""):
    """返回转写函数 transcribe(src, *, model=None, language=None) -> str。
    实现内部对依赖缺失抛 AsrProviderUnavailable，其余异常原样上抛。"""
    name = name or DEFAULT_ASR_PROVIDER
    if name == "auto":
        name = _resolve_auto()
    if name == "mlx_whisper":
        from .mlx.asr import transcribe
        return transcribe
    if name == "remote":
        from .remote.asr import transcribe
        return transcribe
    raise ValueError(f"未知 ASR provider: {name!r}（可用: {', '.join(_ASR_PROVIDERS)}）")
