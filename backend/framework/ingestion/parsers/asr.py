"""音频转写解析器（provider 分发面）。

- provider 制（HETERO_COMPUTE P0）：转写实现经 framework.backends 的 ASR provider
  注册面取用（config: ingestion.asr_provider，默认 auto = 跟随 inference.mode），
  物理承载相关的名字全部收编在 backends/ 之下；**本层只做 fail-open**。
- 区间切片已下放到各 provider（R5-2/D.14）：本层原先用 librosa 切片，那会把
  librosa→numba→llvmlite(125M)+scipy(97M)+sklearn(46M) 钉在 edge 上，而 edge 根本
  不转写。切片对两个 provider 本就是不同的事（本地切 vs 让设备 A 切）。
- fail-open：任何异常（provider 缺失 / 依赖未装 / 模型载入失败 / 解码失败）一律
  返回错误标记文本，绝不让转写异常打断整轮对话。返回值始终是可直接注入上下文的字符串。
"""
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
    start/end 均以秒计，缺省转写全程；切片由 provider 自行完成（见模块头注）。
    provider 缺省 auto = 跟随 inference.mode。
    """
    try:
        fn = get_asr_provider(provider or "")
    except Exception as e:  # provider 未知/注册面异常：fail-open
        return f"[Audio transcription unavailable: {e}]"

    try:
        text = fn(str(path), model=model, language=language, start=start, end=end)
        return text or "[Empty transcript (no speech detected)]"
    except AsrProviderUnavailable as e:  # 依赖缺失/设备 A 不可达：fail-open
        return f"[Audio transcription unavailable: {e}]"
    except Exception as e:  # 解码/载入失败：fail-open
        return f"[Audio transcription failed: {e}]"
