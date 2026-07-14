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

DEFAULT_ASR_PROVIDER = "mlx_whisper"
# 当前默认 provider 的默认模型（provider 专属命名，故收编于此）
DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"


class AsrProviderUnavailable(RuntimeError):
    """provider 依赖缺失/不可用（调用方 fail-open 成 unavailable 标记文本）。"""


def get_asr_provider(name: str = ""):
    """返回转写函数 transcribe(src, *, model=None, language=None) -> str。
    实现内部对依赖缺失抛 AsrProviderUnavailable，其余异常原样上抛。"""
    name = name or DEFAULT_ASR_PROVIDER
    if name == "mlx_whisper":
        from .mlx.asr import transcribe
        return transcribe
    raise ValueError(f"未知 ASR provider: {name!r}（可用: {DEFAULT_ASR_PROVIDER}）")
