"""ASR provider：mlx-whisper（Apple Silicon 本地转写）。

- 懒加载：首次转写时 mlx_whisper 才载入模型，之后进程内自动复用（不占推理 worker，
  发生在生成锁之外）。
- 依赖缺失抛 AsrProviderUnavailable，其余异常原样上抛——fail-open 的字符串化
  由调用方（ingestion/parsers/asr.py）统一处理，provider 只做转写本身。
- **区间切片归 provider**（R5-2）：原先切片在 ingestion/parsers/asr.py 里用 librosa 做，
  那会把 librosa→numba→llvmlite(125M)+scipy(97M)+sklearn(46M) 钉在 edge 上，而 edge
  根本不转写（D.14）。切片对两个 provider 本就是不同的事——本地用 librosa 切，
  remote 让设备 A 切——故下放到各自实现里。
"""
import os
import tempfile
from pathlib import Path
from typing import Optional

from .. import AsrProviderUnavailable, DEFAULT_ASR_MODEL
from ...utils import resolve_model_path


def transcribe(src: str, *, model: Optional[str] = None,
               language: Optional[str] = None,
               start: Optional[float] = None, end: Optional[float] = None) -> str:
    try:
        import mlx_whisper
    except Exception as e:  # 依赖缺失：交调用方 fail-open
        raise AsrProviderUnavailable(f"mlx-whisper not installed ({e})")

    tmp: Optional[Path] = None
    if start is not None or end is not None:
        src, tmp = _slice_to_tmp_wav(src, start, end)
    try:
        return _transcribe_file(mlx_whisper, src, model, language)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass


def _slice_to_tmp_wav(src: str, start: Optional[float],
                      end: Optional[float]) -> "tuple[str, Path]":
    """用 librosa 切 [start, end] 秒区间到临时 wav。whisper 自带 30s 窗口分块，
    整段直喂比按固定长度切更准，故只在调用方显式点名区间时才切。"""
    import librosa
    import soundfile
    audio, sr = librosa.load(src, sr=16000)
    s = int(max(0.0, start or 0.0) * sr)
    e = int(end * sr) if end is not None else len(audio)
    clip = audio[s:max(s, e)]
    fd, tmp_name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)  # 只用路径；fd 不关会每次区间转写泄漏一个文件描述符
    tmp = Path(tmp_name)
    soundfile.write(str(tmp), clip, sr)
    return str(tmp), tmp


def _transcribe_file(mlx_whisper, src: str, model: Optional[str],
                     language: Optional[str]) -> str:

    # 与主模型同一条 resolver：命中 MacAgent/models/<name>/ 则用本地，缺失则一次性
    # snapshot_download 到项目内（不落 ~/.cache/huggingface）。收敛依赖、避免散落项目外。
    resolved = str(resolve_model_path(model or DEFAULT_ASR_MODEL))
    kwargs = {"path_or_hf_repo": resolved}
    if language:
        kwargs["language"] = language
    result = mlx_whisper.transcribe(src, **kwargs)
    return (result.get("text") or "").strip()
