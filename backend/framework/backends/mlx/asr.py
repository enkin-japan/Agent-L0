"""ASR provider：mlx-whisper（Apple Silicon 本地转写）。

- 懒加载：首次转写时 mlx_whisper 才载入模型，之后进程内自动复用（不占推理 worker，
  发生在生成锁之外）。
- 依赖缺失抛 AsrProviderUnavailable，其余异常原样上抛——fail-open 的字符串化
  由调用方（ingestion/parsers/asr.py）统一处理，provider 只做转写本身。
"""
from typing import Optional

from .. import AsrProviderUnavailable, DEFAULT_ASR_MODEL
from ...utils import resolve_model_path


def transcribe(src: str, *, model: Optional[str] = None,
               language: Optional[str] = None) -> str:
    try:
        import mlx_whisper
    except Exception as e:  # 依赖缺失：交调用方 fail-open
        raise AsrProviderUnavailable(f"mlx-whisper not installed ({e})")

    # 与主模型同一条 resolver：命中 MacAgent/models/<name>/ 则用本地，缺失则一次性
    # snapshot_download 到项目内（不落 ~/.cache/huggingface）。收敛依赖、避免散落项目外。
    resolved = str(resolve_model_path(model or DEFAULT_ASR_MODEL))
    kwargs = {"path_or_hf_repo": resolved}
    if language:
        kwargs["language"] = language
    result = mlx_whisper.transcribe(src, **kwargs)
    return (result.get("text") or "").strip()
