"""ASR provider：faster-whisper（CTranslate2/CUDA 本地转写，HETERO_COMPUTE A3）。

与 `mlx/asr.py` 同形状、逐条对齐（两者是 `get_asr_provider` 的互为替身）：
- 懒加载：首次转写时才 import faster_whisper 并建模型，之后进程内复用（不占推理
  worker，发生在生成锁之外）。
- 语言：`language=None` → whisper 自动检测语种（config `ingestion.asr_language: null`）。
- 返回纯文本字符串。
- 依赖缺失（faster-whisper 未装 / 无 CUDA 设备）抛 `AsrProviderUnavailable`，其余异常
  原样上抛——fail-open 的字符串化由调用方（ingestion/parsers/asr.py）统一处理。
- 区间切片归 provider（R5-2）：本腿不用 librosa（CUDA 机的清单里没有它），改用
  faster-whisper 自带的 `decode_audio`（PyAV，随 faster-whisper 一起装）在内存里切，
  连临时 wav 都不落——切片算术与 mlx 腿逐字相同。

**为什么无 CUDA 就判不可用而不是悄悄退 CPU**：本腿的存在理由就是 CUDA 腿。A 机上
CUDA 掉了却在 CPU 上跑 large-v3-turbo，表现是「能出结果、慢几十倍」——最难查的那种
故障。判不可用则调用方 fail-open 成一行醒目标记文本，症状当场可见。mac 上要本地
转写请用 `mlx_whisper`（auto 在 darwin 上本就先选它）。
"""
import threading
from typing import Optional

from .. import AsrProviderUnavailable
from ...utils.config_loader import data_dir

#: 本腿默认模型：faster-whisper 内置别名，解析到官方 CT2 转换权重。
#: 刻意**不复用** backends.DEFAULT_ASR_MODEL（那是 mlx-community/ 的 MLX 专用件）。
_DEFAULT_MODEL = "large-v3-turbo"
#: CUDA 上的算子精度（CT2 术语）。float16 = 转换权重的原生档，A100/H100 通吃。
_COMPUTE_TYPE = "float16"
_SAMPLE_RATE = 16000

_MODELS: "dict[str, object]" = {}
_LOCK = threading.Lock()


def transcribe(src: str, *, model: Optional[str] = None,
               language: Optional[str] = None,
               start: Optional[float] = None, end: Optional[float] = None) -> str:
    try:
        from faster_whisper import WhisperModel
    except Exception as e:  # 依赖缺失：交调用方 fail-open
        raise AsrProviderUnavailable(f"faster-whisper not installed ({e})")

    engine = _get_model(WhisperModel, _resolve_model_name(model))
    audio = src if (start is None and end is None) else _slice_audio(src, start, end)
    # segments 是惰性生成器——不消费完就等于没转写；language=None 即自动检测。
    segments, _info = engine.transcribe(audio, language=language or None)
    return "".join(seg.text for seg in segments).strip()


def _resolve_model_name(model: Optional[str]) -> str:
    """config 的 `ingestion.asr_model` → 本腿吃得下的模型名。

    现值是 `mlx-community/whisper-large-v3-turbo`：MLX 专用件（权重是 mlx 的 safetensors
    布局），CTranslate2 读不了它，直接透传会在模型加载处炸成一句难懂的格式错误。而这
    一个旋钮要同时服务两条腿（同一份 config.yaml 在 mac 与 CUDA 机之间共用，见
    remote/asr.py 头注），所以由**用得着的那条腿**把明显属于对家的名字翻译过来：
    含 "mlx-community/" → 用本腿默认（faster-whisper 别名，解析到官方 CT2 转换件）；
    其余（HF 上的 CT2 仓库 id、本地 CT2 目录、"large-v3"/"small" 等内置别名）原样透传，
    显式配置永远说了算。
    """
    name = (model or "").strip()
    if not name or "mlx-community/" in name.lower():
        return _DEFAULT_MODEL
    return name


def _get_model(WhisperModel, name: str):
    """懒加载单例（按模型名一份）。加载重（下权重 + 上显存），故进程内复用；
    加锁是因为摄入发生在请求线程里，可能有两路音频同时首次触发加载。"""
    with _LOCK:
        engine = _MODELS.get(name)
        if engine is None:
            _require_cuda()
            # download_root：权重收敛在项目可写数据根内（CHARTER；口径同 path_resolver
            # 的 _models_root）。不走 resolve_model_path——"large-v3-turbo" 这类别名不是
            # HF repo id，仓库解析归 faster-whisper 自己做。
            engine = WhisperModel(name, device="cuda", compute_type=_COMPUTE_TYPE,
                                  download_root=str(data_dir() / "models"))
            _MODELS[name] = engine
        return engine


def _require_cuda() -> None:
    try:
        import ctranslate2  # faster-whisper 的运行时，随其一起装
        count = ctranslate2.get_cuda_device_count()
    except Exception as e:
        raise AsrProviderUnavailable(f"ctranslate2 不可用（faster-whisper 的运行时）: {e}")
    if count < 1:
        raise AsrProviderUnavailable(
            "无可用 CUDA 设备：faster_whisper 是 CUDA 腿（不退 CPU，见模块头注）；"
            "Apple Silicon 请用 asr_provider=mlx_whisper")


def _slice_audio(src: str, start: Optional[float], end: Optional[float]):
    """切 [start, end] 秒区间。whisper 自带 30s 窗口分块，整段直喂比按固定长度切更准，
    故只在调用方显式点名区间时才切（同 mlx 腿）。"""
    from faster_whisper.audio import decode_audio
    audio = decode_audio(src, sampling_rate=_SAMPLE_RATE)
    s = int(max(0.0, start or 0.0) * _SAMPLE_RATE)
    e = int(end * _SAMPLE_RATE) if end is not None else len(audio)
    return audio[s:max(s, e)]
