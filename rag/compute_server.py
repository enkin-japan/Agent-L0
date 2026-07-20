"""RAG 计算面服务（数据/计算拆分，HETERO_COMPUTE B2c，:14162）。

无状态的嵌入/重排/分词 HTTP 面：包裹 compute_core，不落任何用户数据——
知识库真相（chroma_data / storage 原文备份）全部归工作端的数据面（rag_server.py）。
算力端（--profile gpu）跑本服务；工作端数据面按 config rag.compute.host 跨机拨号。

鉴权：env RAG_COMPUTE_API_KEY（两侧 .env.local 同值），常量时间比对；/health 免鉴权
供探活。部署铁则同 backend/inference/rag：非回环绑定 + 空密钥 = 拒绝启动。
"""
import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from pydantic import BaseModel

import compute_core as core

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RAG_Compute_Server")

COMPUTE_CFG = core.RAG_CFG.get("compute") or {}
# host 一键两职（同 inference.remote.host / rag.host）：算力端拿它当绑定地址、
# 工作端数据面拿它当拨号地址。空 = 回环（单机双进程联调形态）。
# env 覆盖=运行时改拓扑不必重打包/改 config：非空即赢，覆盖 config 里烘死的绑定地址
BIND_HOST = os.environ.get("RAG_COMPUTE_HOST", "").strip() or (COMPUTE_CFG.get("host") or "").strip() or "127.0.0.1"
PORT = int(COMPUTE_CFG.get("port") or 14162)

COMPUTE_API_KEY = os.environ.get("RAG_COMPUTE_API_KEY", "")


async def verify_compute_key(request: Request, authorization: Optional[str] = Header(None)) -> None:
    """全局 Bearer 校验（照抄 rag_server verify_rag_key）。/health 与 docs 自豁免；空 key=放行（dev）。"""
    path = request.url.path
    if path == "/health" or path in ("/openapi.json", "/redoc") or path.startswith("/docs"):
        return
    if not COMPUTE_API_KEY:
        return  # 未配置密钥 → 放行（启动时已告警）
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少或格式错误的 Authorization 头（应为 'Bearer <key>'）。")
    provided = authorization[len("Bearer "):]
    # encode 后比对：compare_digest 对含非 ASCII 的 str 会抛 TypeError，bytes 恒安全
    if not hmac.compare_digest(provided.encode("utf-8"), COMPUTE_API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="RAG compute API 密钥无效。")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not COMPUTE_API_KEY:
        logger.warning("⚠️ RAG_COMPUTE_API_KEY 未设置：鉴权未启用（放行）——仅限开发/回环，跨机部署必须设置。")
    core.load_models()
    yield
    core.unload_models()


app = FastAPI(lifespan=lifespan, title="RAG 计算面 API（嵌入/重排/分词）",
              dependencies=[Depends(verify_compute_key)])


class EmbedRequest(BaseModel):
    texts: List[str]


class TokenizeRequest(BaseModel):
    texts: List[str]
    model_type: str = "embedding"     # embedding | rerank


class RerankRequest(BaseModel):
    query: str
    documents: List[str]
    instruct: Optional[str] = None    # 缺省用 core.DEFAULT_RERANK_INSTRUCT（模板归计算侧）


@app.get("/health", tags=["基础监控"])
async def health_check():
    return {"status": "healthy", **core.identity()}


@app.post("/api/v1/embed", tags=["计算原语"])
def embed(req: EmbedRequest):
    # def（非 async）：跑在 FastAPI 线程池，模型前向由 core.MPS_LOCK 串行化，不堵事件循环
    return {"embeddings": core.embed_texts(req.texts)}


@app.post("/api/v1/tokenize", tags=["计算原语"])
def tokenize(req: TokenizeRequest):
    return {"counts": core.token_counts(req.texts, req.model_type)}


@app.post("/api/v1/rerank", tags=["计算原语"])
def rerank(req: RerankRequest):
    return {"scores": core.rerank_scores(req.query, req.documents, req.instruct)}


def _enforce_bind_guardrail() -> None:
    """部署铁则（§8）：非回环绑定 + 空 RAG_COMPUTE_API_KEY = 拒启。

    与 backend/inference/rag 同一条纪律。计算面虽无状态，但入库/查询文本都会流经此处，
    裸奔等于把语料在途明文开放给整个可达网络。
    """
    if BIND_HOST in ("127.0.0.1", "localhost", "::1") or len(COMPUTE_API_KEY) >= 16:
        return
    raise SystemExit(
        f"❌ [rag-compute] 拒绝启动：绑定到非回环地址 {BIND_HOST!r} 但 RAG_COMPUTE_API_KEY "
        f"缺失或过弱（长度 {len(COMPUTE_API_KEY)} < 16）。\n"
        f"   密钥过短/过弱同样拒绝。请 `./devctl.sh keygen` 生成强随机值（与工作端同值，"
        f"app 形态填进设置页），或把 rag.compute.host 改回回环/留空。\n"
        f"   （部署铁则 §8：非回环绑定只允许出现在受信内网 + 必须带 Bearer。）"
    )


def _preflight_bind_addr() -> None:
    """启动前本机绑定地址预检：BIND_HOST 非本机任何网卡地址（EADDRNOTAVAIL）立即拒启。

    实机事故（2026-07-19）：算力端在别的设备上启动而 config 的 host 还填着另一台设备的
    IP → uvicorn 报晦涩的 `[Errno 49] can't assign requested address`。就地小副本，与
    backend/app.py 的 preflight_bind_addr 同语义（三服务各自独立 venv，不共享代码）。
    域名解析失败（gaierror）等其余错误静默放行，留给真实 bind 报错。
    """
    import errno
    import socket
    h = str(BIND_HOST or "").strip()
    if h in ("", "127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        return
    fam = socket.AF_INET6 if ":" in h else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((h, 0))
    except OSError as e:
        if getattr(e, "errno", None) == errno.EADDRNOTAVAIL:
            raise SystemExit(
                f"❌ [rag-compute] 拒绝启动：绑定地址 '{h}' 不是本机任何网卡的地址（EADDRNOTAVAIL）。\n"
                f"   config 的 rag.compute.host（或环境变量 RAG_COMPUTE_HOST）应填**本机**"
                f"（对外提供服务的这台设备自己）的 IP。\n"
                f"   查看本机地址：`tailscale ip -4` 或 `ifconfig | grep 'inet '`；\n"
                f"   若该地址属于另一台设备，请到那台设备上启动本服务。"
            )
    finally:
        s.close()


if __name__ == "__main__":
    import uvicorn
    _enforce_bind_guardrail()   # 先于加载模型：省掉几十秒权重加载才发现配置错
    _preflight_bind_addr()      # 密钥护栏之后：绑定地址不属本机立即拒启
    uvicorn.run(app, host=BIND_HOST, port=PORT, reload=False)
