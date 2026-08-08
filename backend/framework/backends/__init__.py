"""推理后端注册面（HETERO_COMPUTE P0/D7）。

物理承载相关的名字（MLX / CUDA / 远程，含各家 ASR provider 与默认模型名）
全部收编在 backends/ 之下；框架其余部分只经本模块的工厂函数取用。
惰性导入：具体后端的重型依赖只在真正选用时才加载。
"""
import sys

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
    if name == "cuda":
        from .cuda.backend import CudaVLLMBackend
        return CudaVLLMBackend
    raise ValueError(f"未知推理后端: {name!r}（可用: mlx, remote, cuda）")


# ---- ASR provider（ingestion/parsers/asr.py 经此取转写实现）----

#: 缺省 = 跟随推理承载（见 _resolve_auto）。**刻意不是某个具体 provider**：
#: `inference.mode` 与 `ingestion.asr_provider` 是两个旋钮，各说各话时症状很隐蔽——
#: mode=remote（推理在 A）却把 asr 留在 mlx_whisper，edge 就得为一个用不上的
#: 转写实现继续背 torch/librosa 全家（R5-2 的瘦身直接白做，而且没有任何报错）。
DEFAULT_ASR_PROVIDER = "auto"
# 当前默认 provider 的默认模型（provider 专属命名，故收编于此）。**mlx 腿的**命名——
# CUDA 腿吃不下 mlx-community 的权重布局，故在 cuda/asr.py 里把这个值翻译成自家默认
# （见那里的 _resolve_model_name；一个 config 旋钮服务两条腿，翻译归用得着的那条）。
DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"

_ASR_PROVIDERS = ("auto", "mlx_whisper", "faster_whisper", "remote")

#: 各本机腿对应的**发行包名**（用于 auto 的"装没装"探测；恰好与 provider 名同名，
#: 但两者是不同的东西，故显式列表而不是拿 provider 名当模块名去 import）。
_ASR_LOCAL_PKGS = {"mlx_whisper": "mlx_whisper", "faster_whisper": "faster_whisper"}


class AsrProviderUnavailable(RuntimeError):
    """provider 依赖缺失/不可用（调用方 fail-open 成 unavailable 标记文本）。"""


def _installed(pkg: str) -> bool:
    """探测包是否已安装（不 import：只看 spec，auto 解析不该付出重型 import 的代价）。"""
    import importlib.util
    try:
        return importlib.util.find_spec(pkg) is not None
    except Exception:
        return False


def resolve_local_asr_provider() -> str:
    """**本机进程内**转写腿：按物理承载分腿——darwin 首选 mlx_whisper（Metal），
    其余平台首选 faster_whisper（CTranslate2/CUDA，HETERO_COMPUTE A3）。

    首选那条没装而另一条装了就用另一条：不该因为跑在 Linux 上就把一台装了
    mlx_whisper 的机器判死（反之亦然）。两条都没装时仍返回平台首选——让报错指向这台
    机器**本该**装的那条腿，而不是对家。

    与 `_resolve_auto` 的分工：这里不看 `inference.mode`，调用方（推理服务端的
    /v1/transcribe）已经确定"就在本机转"。
    """
    preferred, fallback = (("mlx_whisper", "faster_whisper") if sys.platform == "darwin"
                           else ("faster_whisper", "mlx_whisper"))
    if not _installed(_ASR_LOCAL_PKGS[preferred]) and _installed(_ASR_LOCAL_PKGS[fallback]):
        return fallback
    return preferred


def _resolve_auto() -> str:
    """auto → 具体 provider：推理在哪，转写就在哪。

    edge（`inference.mode: remote`）= 权重在设备 A，本机连一个字都生成不出来——
    那么"音频离线可用"本就不成立，转写留在 edge 只剩体积代价（D.14）。
    本机转写（mode != remote）则按平台挑腿，见 resolve_local_asr_provider。
    """
    from ..utils.config_loader import load_section
    mode = str((load_section("inference") or {}).get("mode", "inprocess"))
    return "remote" if mode == "remote" else resolve_local_asr_provider()


def get_asr_provider(name: str = ""):
    """返回转写函数 transcribe(src, *, model=None, language=None) -> str。
    实现内部对依赖缺失抛 AsrProviderUnavailable，其余异常原样上抛。"""
    name = name or DEFAULT_ASR_PROVIDER
    if name == "auto":
        name = _resolve_auto()
    if name == "mlx_whisper":
        from .mlx.asr import transcribe
        return transcribe
    if name == "faster_whisper":
        from .cuda.asr import transcribe
        return transcribe
    if name == "remote":
        from .remote.asr import transcribe
        return transcribe
    raise ValueError(f"未知 ASR provider: {name!r}（可用: {', '.join(_ASR_PROVIDERS)}）")
