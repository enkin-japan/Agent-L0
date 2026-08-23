"""远程 ASR provider（R5-2/D.14）：音频送设备 A 转写，edge 不留 whisper。

**为什么整条音频路走**：torch(385M) 是被 `silero-vad` 钉住的（`Requires: torch,
torchaudio`），不是 whisper——`mlx-whisper` 本体只有 1.9M，`librosa` 另外钉住
numba→llvmlite(125M)+scipy(97M)+sklearn(46M)。故去掉 whisper 几乎不省空间；
音频路是一个耦合块，只能整条留（edge ≈1.57G）或整条走（≈0.85G）。

**为什么不用 Apple SpeechAnalyzer**：它是 Swift-only（Xcode SDK 的 ObjC 头里只有旧的
SFSpeechRecognizer 家族），仓库已用的 PyObjC 够不到 → ASR 会依赖 macOS app 存在，
而 dev（devctl 起的纯 Python 环境）、web-only、将来的 CUDA 机都没有 app。
它还要求先定 locale，会牺牲 `ingestion.asr_language: None` 的自动语言检测。
日后可作**同一接缝下**的可选快路（见 D.14）。

传输复用既有的 `/v1/media`（sha256 内容寻址，天然去重），不另开上传通道。
"""
import hashlib
import os
from pathlib import Path
from typing import Optional

import requests

from .. import AsrProviderUnavailable
from ...utils.config_loader import load_section

_CONNECT_TIMEOUT = 5.0
#: 转写读超时：A 上与生成共用同一个单线程 worker（CHARTER §3 单引擎单实例，
#: 2026-08-23 修订自「单 worker 串行」；单流形态下转写照旧排队），
#: 排在一次长生成后面是正常现象，故给得比控制面宽得多。
_TRANSCRIBE_READ_TIMEOUT = 600.0
_UPLOAD_READ_TIMEOUT = 120.0


def _base_url() -> str:
    cfg = (load_section("inference") or {}).get("remote") or {}
    host = str(cfg.get("host", "127.0.0.1"))
    port = int(cfg.get("port", 14161))
    return f"http://{host}:{port}"


def _session() -> requests.Session:
    s = requests.Session()
    # 密钥与 RemoteBackend 同一惯例：环境变量（.env.local 注入，不进仓库）
    key = os.environ.get("INFER_API_KEY", "")
    if key:
        s.headers["Authorization"] = f"Bearer {key}"
    return s


def transcribe(src, *, model: Optional[str] = None,
               language: Optional[str] = None,
               start: Optional[float] = None, end: Optional[float] = None) -> str:
    """把本地音频送设备 A 转写，返回文本。

    区间 [start, end] 也交给 A 切——edge 不留 librosa（切片在这里做就等于把
    numba/llvmlite/scipy 钉回来）。整文件内容寻址上传，反复取不同区间不会重传。

    契约与 `mlx/asr.py` 的同名函数一致（`get_asr_provider` 的两个实现互为替身）：
    依赖/服务不可用抛 `AsrProviderUnavailable`，其余异常原样上抛——
    两类都由 `ingestion/parsers/asr.py` 收成标记文本 fail-open，不打断整轮对话。
    """
    p = Path(src)
    try:
        data = p.read_bytes()
    except Exception as e:
        raise AsrProviderUnavailable(f"读不到音频文件 {p.name}: {e}")

    sess = _session()
    base = _base_url()
    digest = hashlib.sha256(data).hexdigest()
    try:
        # 1) 内容寻址上传（同内容 A 侧天然去重，重传不产生新副本）
        r = sess.post(f"{base}/v1/media",
                      files={"file": (p.name, data)}, data={"sha256": digest},
                      timeout=(_CONNECT_TIMEOUT, _UPLOAD_READ_TIMEOUT))
        r.raise_for_status()
        media_id = r.json()["media_id"]

        # 2) 转写
        body = {"media_id": media_id}
        if model:
            body["model"] = model
        if language:
            body["language"] = language
        if start is not None:
            body["start"] = float(start)
        if end is not None:
            body["end"] = float(end)
        r = sess.post(f"{base}/v1/transcribe", json=body,
                      timeout=(_CONNECT_TIMEOUT, _TRANSCRIBE_READ_TIMEOUT))
        r.raise_for_status()
        return str(r.json().get("text") or "")
    except requests.RequestException as e:
        # 连不上/超时/5xx：A 没跑或不可达。这是"provider 不可用"而不是"音频有问题"，
        # 说清楚是哪台机器，免得在 edge 上查一个根本不在 edge 的故障。
        raise AsrProviderUnavailable(
            f"设备 A 的转写服务不可达（{base}）: {e}") from e
    finally:
        sess.close()
