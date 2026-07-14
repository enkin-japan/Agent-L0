"""音频转写解析器（provider 分发面）。

- provider 制（HETERO_COMPUTE P0）：转写实现经 framework.backends 的 ASR provider
  注册面取用（config: ingestion.asr_provider，默认本地 whisper 实现），物理承载
  相关的名字全部收编在 backends/ 之下；本层只做区间切片与 fail-open。
- fail-open：任何异常（provider 缺失 / 依赖未装 / 模型载入失败 / 解码失败）一律
  返回错误标记文本，绝不让转写异常打断整轮对话。返回值始终是可直接注入上下文的字符串。
"""
import os
from pathlib import Path
from typing import Optional

from ...backends import (AsrProviderUnavailable, DEFAULT_ASR_MODEL,
                         get_asr_provider)


def transcribe_audio(path, *, model: Optional[str] = DEFAULT_ASR_MODEL,
                     language: Optional[str] = None,
                     start: Optional[float] = None,
                     end: Optional[float] = None,
                     provider: Optional[str] = None) -> str:
    """
    转写整段音频（或 [start, end] 秒区间）为文本。whisper 自带 30s 窗口分块，
    无需外部 VAD 切片——直接喂整文件比按固定长度切更准（不会切在词中间）。
    start/end 均以秒计，缺省转写全程。provider 缺省用默认转写实现（行为不变）。
    """
    try:
        fn = get_asr_provider(provider or "")
    except Exception as e:  # provider 未知/注册面异常：fail-open
        return f"[Audio transcription unavailable: {e}]"

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

        text = fn(src, model=model, language=language)
        return text or "[Empty transcript (no speech detected)]"
    except AsrProviderUnavailable as e:  # 依赖缺失：fail-open
        return f"[Audio transcription unavailable: {e}]"
    except Exception as e:  # 解码/载入失败：fail-open
        return f"[Audio transcription failed: {e}]"
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass
