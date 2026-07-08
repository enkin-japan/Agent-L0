"""
音频转写解析器（mlx-whisper，large-v3-turbo）。

- 懒加载：首次转写时 mlx_whisper 才载入模型，之后进程内自动复用（不占推理 worker，
  发生在生成锁之外）。
- fail-open：任何异常（未装 mlx-whisper / 模型载入失败 / 解码失败）一律返回错误标记
  文本，绝不让转写异常打断整轮对话。返回值始终是可直接注入上下文的字符串。
"""
import os
from pathlib import Path
from typing import Optional

from ...utils import resolve_model_path

DEFAULT_ASR_MODEL = "mlx-community/whisper-large-v3-turbo"


def transcribe_audio(path, *, model: str = DEFAULT_ASR_MODEL,
                     language: Optional[str] = None,
                     start: Optional[float] = None,
                     end: Optional[float] = None) -> str:
    """
    转写整段音频（或 [start, end] 秒区间）为文本。whisper 自带 30s 窗口分块，
    无需外部 VAD 切片——直接喂整文件比按固定长度切更准（不会切在词中间）。
    start/end 均以秒计，缺省转写全程。
    """
    try:
        import mlx_whisper
    except Exception as e:  # 依赖缺失：fail-open
        return f"[Audio transcription unavailable: mlx-whisper not installed ({e})]"

    src = str(path)
    tmp: Optional[Path] = None
    try:
        # 需要时间区间：用 librosa 切片写临时 wav 再转写
        if start is not None or end is not None:
            import librosa
            import soundfile
            import tempfile
            audio, sr = librosa.load(src, sr=16000)
            s = int(max(0.0, start or 0.0) * sr)
            e = int(end * sr) if end is not None else len(audio)
            clip = audio[s:max(s, e)]
            fd, tmp_name = tempfile.mkstemp(suffix=".wav")
            os.close(fd)  # 只用路径；fd 不关会每次区间转写泄漏一个文件描述符
            tmp = Path(tmp_name)
            soundfile.write(str(tmp), clip, sr)
            src = str(tmp)

        # 与主模型同一条 resolver：命中 MacAgent/models/<name>/ 则用本地，缺失则一次性
        # snapshot_download 到项目内（不落 ~/.cache/huggingface）。收敛依赖、避免散落项目外。
        resolved = str(resolve_model_path(model))
        kwargs = {"path_or_hf_repo": resolved}
        if language:
            kwargs["language"] = language
        result = mlx_whisper.transcribe(src, **kwargs)
        text = (result.get("text") or "").strip()
        return text or "[Empty transcript (no speech detected)]"
    except Exception as e:  # 解码/载入失败：fail-open
        return f"[Audio transcription failed: {e}]"
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass
