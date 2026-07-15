"""inference-server（HETERO_COMPUTE B1/D2/D7）：InferenceBackend 协议的 HTTP/SSE 服务面。

设备 A（算力机）上运行，内包一个进程内推理后端（B1 先包 MLX，A 线完成后可换 CUDA——
换后端只改 config 的 inference.backend，本服务一行不动）。设备 B 的 RemoteBackend
（backend/framework/backends/remote/）是它唯一的预期客户端。

隐私边界（§3.1）：本服务只见 prompt 字符串与媒体文件，不持久化任何会话数据；
媒体以 sha256 内容寻址落在本机 media/ 目录（可随时清空，客户端会按需重传）。

端点（把 §1.1 协议 SSE 化）：
  POST   /v1/generate                  SSE 生成流（events: meta / chunk / done / error）
  POST   /v1/generate/{gen_id}/cancel  段粒度取消（对应引擎逐 chunk 的取消检查点）
  POST   /v1/media                     内容寻址上传（sha256 去重）→ {media_id}
  POST   /v1/cache                     新建缓存 → {cache_id}
  POST   /v1/cache/{id}/clone          分支克隆 → {cache_id}；不支持时 501（客户端冷启动回退）
  DELETE /v1/cache/{id}                冻结/删会话时释放
  GET    /health  /metrics             devctl 探活 + 仪表

鉴权：Bearer 共享密钥（环境变量 INFER_API_KEY，.env.local 注入），常量时间比对，
与 backend 的 AGENT_API_KEY 同一纪律；为空 = 未启用（启动告警后放行，仅限开发）。
部署铁则（§8）：非回环绑定只允许出现在受信内网（Tailscale）。

线程纪律：与 AgentEngine 相同——模型在专用推理执行器线程内加载与生成（MLX 惰性
张量绑定创建线程），事件循环只做 HTTP/SSE 搬运；生成互斥锁保证底层单例不被并发触碰。

启动（B1 同机双进程验收形态；devctl profile 是 B3 的事）：
  cd <repo>/backend && .venv/bin/python ../inference/server.py
"""
import asyncio
import hashlib
import hmac
import json
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))  # framework 包住在 backend/ 下

import os
import uvicorn
from fastapi import FastAPI, Depends, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from framework.utils.config_loader import load_config
from framework.utils.model_selection import resolve_selected_model
from framework.utils.path_resolver import resolve_model_path
from framework.core.response_parser import ResponseParser
from framework.core.constrained import ToolConstraintEngine, ToolCallGuard, llguidance as _llg
from framework.backends import get_backend_class

MEDIA_DIR = Path(__file__).resolve().parent / "media"
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}(\.[A-Za-z0-9]{1,8})?$")  # 内容寻址名，防路径穿越

config = load_config()
INFERENCE_CFG = config.get("inference") or {}
REMOTE_CFG = INFERENCE_CFG.get("remote") or {}
HOST = str(REMOTE_CFG.get("host", "127.0.0.1"))
PORT = int(REMOTE_CFG.get("port", 14161))
CACHE_TTL_SECONDS = float(REMOTE_CFG.get("cache_ttl_minutes", 45)) * 60.0
API_KEY = os.environ.get("INFER_API_KEY", "")


async def verify_api_key(request: Request, authorization: Optional[str] = Header(None)) -> None:
    """Bearer 校验：照抄 backend/framework/api/auth.py 的常量时间比对纪律。"""
    if not API_KEY:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少或格式错误的 Authorization 头")
    provided = authorization[len("Bearer "):]
    if not hmac.compare_digest(provided.encode("utf-8"), API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="API 密钥无效")


class CacheRegistry:
    """cache_id → CacheHandle 注册表。TTL 空闲释放兜底客户端崩溃/未显式释放的泄漏
    （45min 略长于 backend 侧 30min 冻结——释放顺序由 backend 主导，TTL 只兜底）。"""

    def __init__(self, backend, ttl_seconds: float):
        self._backend = backend
        self._ttl = ttl_seconds
        self._entries: Dict[str, Dict[str, Any]] = {}

    def create(self) -> str:
        cid = uuid.uuid4().hex
        self._entries[cid] = {"handle": self._backend.new_cache(), "last_access": time.time()}
        return cid

    def get(self, cid: str):
        e = self._entries.get(cid)
        if e is None:
            return None
        e["last_access"] = time.time()
        return e["handle"]

    def adopt(self, handle) -> str:
        cid = uuid.uuid4().hex
        self._entries[cid] = {"handle": handle, "last_access": time.time()}
        return cid

    def release(self, cid: str) -> bool:
        e = self._entries.pop(cid, None)
        if e is None:
            return False
        try:
            self._backend.release_cache(e["handle"])
        except Exception as ex:
            print(f"[Registry] 释放缓存异常（已忽略）: {ex}")
        return True

    def sweep(self) -> int:
        now = time.time()
        stale = [cid for cid, e in self._entries.items() if now - e["last_access"] > self._ttl]
        for cid in stale:
            self.release(cid)
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)


class ServerState:
    backend = None
    executor: Optional[ThreadPoolExecutor] = None
    registry: Optional[CacheRegistry] = None
    gen_lock: Optional[asyncio.Lock] = None       # 生成互斥：底层共享单例不可并发
    gens: Dict[str, Dict[str, Any]] = {}          # gen_id → {"cancel": bool}（进行中的生成）
    constraint_engine = None                      # 懒建（LLTokenizer ~1s）
    constraint_disabled = _llg is None
    parser = None
    metrics = {"generations_total": 0, "cancelled_total": 0, "errors_total": 0}


S = ServerState()


def _tool_guard(tool_specs) -> Optional[ToolCallGuard]:
    """服务端按客户端捎带的工具声明重建约束 guard（与 AgentEngine._tool_guard 同纪律：
    任何失败永久禁用 fail-open，绝不影响生成主路径）。"""
    if not tool_specs or S.constraint_disabled \
            or not bool(INFERENCE_CFG.get("constrained_tool_calls", False)):
        return None
    try:
        if S.constraint_engine is None:
            proc = S.backend.processor
            tok = proc.tokenizer if hasattr(proc, "tokenizer") else proc
            stc = tok.encode(S.parser.stc, add_special_tokens=False)
            etc = tok.encode(S.parser.etc, add_special_tokens=False)
            if len(stc) != 1 or len(etc) != 1:
                raise RuntimeError("工具调用标记不是单 token，约束状态机无法锚定")
            S.constraint_engine = ToolConstraintEngine(tok, stc[0], etc[0])
            print("[Server] 约束采样引擎就绪（llguidance）")
        eng = S.constraint_engine
        return ToolCallGuard(eng.matcher_for(tool_specs), eng.stc_id, eng.etc_id, eng.vocab_size)
    except Exception as e:
        print(f"[Server] 约束采样构建失败，永久禁用: {type(e).__name__}: {e}")
        S.constraint_disabled = True
        return None


def _media_paths(ids, kind: str):
    """media_id 列表 → 服务端本地文件路径列表；缺失/非法 id 跳过并告警（不炸整轮）。"""
    out = []
    for mid in ids or []:
        if not _MEDIA_ID_RE.match(str(mid)):
            print(f"[Server] 非法 {kind} media_id，跳过: {mid!r}")
            continue
        p = MEDIA_DIR / mid
        if p.is_file():
            out.append(str(p))
        else:
            print(f"[Server] {kind} 媒体缺失（客户端需重传），跳过: {mid}")
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    model, revision, source = resolve_selected_model()
    model_path = resolve_model_path(model, revision=revision)
    backend_name = str(INFERENCE_CFG.get("backend") or "")
    print(f"🚀 [Server] loading model: {model} (backend={backend_name or 'default'})")

    S.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inference")
    S.backend = await get_backend_class(backend_name).load(
        model_path, executor=S.executor, **INFERENCE_CFG)
    S.parser = ResponseParser(S.backend.processor)
    S.registry = CacheRegistry(S.backend, CACHE_TTL_SECONDS)
    S.gen_lock = asyncio.Lock()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    async def _sweep_loop():
        while True:
            try:
                await asyncio.sleep(300)
                n = S.registry.sweep()
                if n:
                    print(f"🧹 [Server] TTL 回收 {n} 份空闲缓存（剩余 {len(S.registry)}）")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Server] TTL 清扫异常（已忽略）: {e}")

    sweep_task = asyncio.create_task(_sweep_loop())

    if API_KEY:
        print("🔐 [Server] 推理服务鉴权已启用（Bearer INFER_API_KEY）")
    else:
        print("⚠️  [Server] INFER_API_KEY 未设置：推理服务对可达网络裸奔（仅限开发/回环）。")
    print(f"✅ [Server] inference-server listening on {HOST}:{PORT}")
    yield

    sweep_task.cancel()
    print("\n🛑 [Server] shutting down…")
    try:
        S.backend.shutdown()
    except Exception as e:
        print(f"[Server] 后端释放异常: {e}")


app = FastAPI(title="Agent inference-server", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():  # 免鉴权探活：lifespan 完成（模型已加载）后才受理 → 200 即就绪
    return {"status": "ok"}


@app.get("/metrics", dependencies=[Depends(verify_api_key)])
async def metrics():
    return {**S.metrics, "cache_count": len(S.registry), "active_generations": len(S.gens)}


@app.post("/v1/cache", dependencies=[Depends(verify_api_key)])
async def cache_new():
    return {"cache_id": S.registry.create()}


@app.post("/v1/cache/{cache_id}/clone", dependencies=[Depends(verify_api_key)])
async def cache_clone(cache_id: str):
    src = S.registry.get(cache_id)
    if src is None:
        raise HTTPException(status_code=404, detail="缓存不存在或已被 TTL 回收")
    # 深拷贝在推理执行器线程内做（张量归属线程），并持生成锁防克隆到写一半的缓存
    async with S.gen_lock:
        loop = asyncio.get_running_loop()
        cloned = await loop.run_in_executor(S.executor, S.backend.clone_cache, src)
    if cloned is None:
        raise HTTPException(status_code=501, detail="后端不支持缓存克隆（客户端应冷启动回退）")
    return {"cache_id": S.registry.adopt(cloned)}


@app.delete("/v1/cache/{cache_id}", dependencies=[Depends(verify_api_key)])
async def cache_release(cache_id: str):
    return {"released": S.registry.release(cache_id)}


@app.post("/v1/media", dependencies=[Depends(verify_api_key)])
async def media_upload(file: UploadFile, sha256: str = Form("")):
    data = await file.read()
    digest = hashlib.sha256(data).hexdigest()
    if sha256 and sha256 != digest:
        raise HTTPException(status_code=400, detail="sha256 与内容不符（传输损坏）")
    suffix = Path(file.filename or "").suffix.lower()
    if not re.match(r"^\.[A-Za-z0-9]{1,8}$", suffix):
        suffix = ""
    media_id = digest + suffix
    dest = MEDIA_DIR / media_id
    if not dest.exists():  # 内容寻址：同内容天然去重
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return {"media_id": media_id, "bytes": len(data)}


@app.post("/v1/generate/{gen_id}/cancel", dependencies=[Depends(verify_api_key)])
async def generate_cancel(gen_id: str):
    g = S.gens.get(gen_id)
    if g is None:
        return {"cancelled": False}  # 已结束/不存在：幂等返回
    g["cancel"] = True
    S.metrics["cancelled_total"] += 1
    return {"cancelled": True}


@app.post("/v1/generate", dependencies=[Depends(verify_api_key)])
async def generate(request: Request):
    body = await request.json()
    prompt = str(body.get("prompt", ""))
    sampling = body.get("sampling") or {}
    features = dict(body.get("features") or {})

    cache_id = body.get("cache_id")
    if cache_id:
        cache = S.registry.get(str(cache_id))
        if cache is None:
            # 会话缓存被 TTL 回收：换新缓存冷启动（前缀复用丢失但正确性不受影响），
            # 新 id 经 meta 事件回填客户端句柄，后续轮次恢复热复用
            cache_id = S.registry.create()
            cache = S.registry.get(cache_id)
    else:
        cache_id = S.registry.create()
        cache = S.registry.get(cache_id)

    guard = _tool_guard(features.pop("tool_specs", None))
    if guard is not None:
        features["tool_guard"] = guard

    images = _media_paths(body.get("images"), "image")
    audios = _media_paths(body.get("audios"), "audio")

    gen_id = uuid.uuid4().hex
    gen_state = {"cancel": False}
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()

    def _blocking_generate():
        """推理执行器线程：消费后端生成流，逐 chunk 投回事件循环。"""
        try:
            for chunk in S.backend.stream_generate(
                prompt, cache=cache,
                images=images or None, audios=audios or None,
                sampling=sampling, features=features,
            ):
                # 有界队列防内存无界（SSE 消费慢于生成时施加背压）。单一 put 协程 +
                # 轮询其完成（绝不重试 put，防重复入队），等待期间取消标志同样生效
                item = ("chunk", {
                    "text": chunk.text, "delta": chunk.delta,
                    "prompt_tokens": chunk.prompt_tokens,
                    "generation_tokens": chunk.generation_tokens,
                    "prompt_tps": chunk.prompt_tps,
                    "generation_tps": chunk.generation_tps,
                    "peak_memory": chunk.peak_memory,
                    "extra": chunk.extra or {},
                })
                fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                while True:
                    if gen_state["cancel"]:
                        fut.cancel()
                        return ("cancelled", {})
                    try:
                        fut.result(timeout=0.5)
                        break
                    except FuturesTimeout:
                        continue  # 队列满（背压）→ 复查取消标志后继续等同一个 put
                    except Exception:
                        return ("cancelled", {})  # 事件循环已收摊等极端情形：按取消止损
            done_payload = {}
            if guard is not None and guard.masked_positions:
                done_payload["constrained_steps"] = guard.masked_positions
            return ("done", done_payload)
        except Exception as e:
            return ("error", {"message": f"{type(e).__name__}: {e}"})

    async def _sse():
        # 生成互斥：底层共享单例不可并发。acquire 被取消（排队中客户端断连）时
        # 锁未持有、finally 不会走——语义正确
        await S.gen_lock.acquire()
        task = None
        try:
            S.gens[gen_id] = gen_state
            S.metrics["generations_total"] += 1
            yield f"event: meta\ndata: {json.dumps({'gen_id': gen_id, 'cache_id': cache_id})}\n\n"
            task = loop.run_in_executor(S.executor, _blocking_generate)
            while True:
                get_q = asyncio.ensure_future(queue.get())
                await asyncio.wait({get_q, task}, return_when=asyncio.FIRST_COMPLETED)
                if get_q.done():
                    kind, payload = "chunk", get_q.result()[1]
                    yield f"event: chunk\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    continue
                get_q.cancel()
                kind, payload = task.result()
                # 冲刷执行器线程在终态前已入队的残余 chunk（先进先出保序）
                while not queue.empty():
                    _, p = queue.get_nowait()
                    yield f"event: chunk\ndata: {json.dumps(p, ensure_ascii=False)}\n\n"
                if kind == "error":
                    S.metrics["errors_total"] += 1
                    yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif kind == "done":
                    yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                # cancelled：客户端多半已离场，不需要终态事件（断连即语义）
                break
        finally:
            # 断连/取消/完成统一收尾：置取消标志（生成线程在下一 chunk 边界停）。
            # 此处**绝不能 await**：客户端断连时本生成器经任务取消收场，粘性取消会让
            # finally 里的任何挂起点立即再抛 CancelledError、跳过其后的清理——放锁改为
            # 生成线程退出时的 done-callback（跑在事件循环线程），既保证"锁的释放严格
            # 晚于生成线程退出"（单例不并发的不变量），又对取消免疫（实测断连泄锁定此案）。
            gen_state["cancel"] = True

            def _finish(_fut=None):
                S.gens.pop(gen_id, None)
                S.gen_lock.release()

            if task is None:
                _finish()
            else:
                task.add_done_callback(_finish)

    return StreamingResponse(_sse(), media_type="text/event-stream")


def _enforce_bind_guardrail() -> None:
    """部署铁则（§8 / §7 预留 2）：非回环绑定 + 空 INFER_API_KEY = 拒启。

    gpu profile 的常规形态就是绑 Tailscale 地址给设备 B 拨——"改了 host 忘了 key"
    会让推理服务对整个 tailnet 裸奔，只靠启动告警拦不住。回环绑定时空密钥仍放行（dev 不变）。
    与 backend/app.py、rag/rag_server.py 同一条纪律（三服务各自独立 venv，就地实现不共享代码）。
    """
    if str(HOST).strip() in ("127.0.0.1", "localhost", "::1") or API_KEY:
        return
    raise SystemExit(
        f"❌ [inference-server] 拒绝启动：绑定到非回环地址 {HOST!r} 但未设置 INFER_API_KEY——"
        f"服务会对整个可达网络裸奔。\n"
        f"   请在 .env.local 设置 INFER_API_KEY=<强随机串>（与设备 B 同值），"
        f"或把 inference.remote.host 改回 127.0.0.1。\n"
        f"   （部署铁则 §8：非回环绑定只允许出现在受信内网 + 必须带 Bearer。）"
    )


if __name__ == "__main__":
    _enforce_bind_guardrail()   # 先于加载权重：省掉几十秒才发现配置错
    uvicorn.run(app, host=HOST, port=PORT)
