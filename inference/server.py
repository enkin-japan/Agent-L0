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
  POST   /v1/transcribe                音频转写（R5-2/D.14：edge 不留 whisper）→ {text}
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
# 同目录模块（openai_facade）：作为脚本跑时 sys.path[0] 就是本目录，但金测试按路径
# 装载本文件时没有这条——显式补上，S.7「没人 import 的入口」纪律
sys.path.insert(0, str(REPO_ROOT / "inference"))

import os
import uvicorn
from fastapi import FastAPI, Depends, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from framework.utils.config_loader import load_config
from framework.utils.model_selection import (
    merge_entry_speculative, model_options, resolve_selected_model)
from framework.utils.path_resolver import resolve_model_path
# 方言经注册面取（R0）：server 只要工具段标记（dialect.markers.stc/etc 喂约束采样），
# 不 import 任何家族私有模块——直接 import gemma4 的 ResponseParser 就是把
# 「引擎不认模型家族字面量」（CHARTER 方言层）在服务入口这儿重新违反一遍。
from framework.dialects import get_dialect_class
from framework.core.constrained import ToolConstraintEngine, ToolCallGuard, llguidance as _llg
from framework.backends import (AsrProviderUnavailable, get_asr_provider,
                                get_backend_class, resolve_local_asr_provider)
from framework.core import prompt_stream as pstream
from framework.core.stream_filter import VisibleTextStreamer

import openai_facade as oai  # 同目录纯逻辑层（T6 门面）

MEDIA_DIR = Path(__file__).resolve().parent / "media"
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}(\.[A-Za-z0-9]{1,8})?$")  # 内容寻址名，防路径穿越
# KV 冻结落盘（Q9-a）：会话 KV 的服务端存放处。与 media/ 同一隐私档位——本机磁盘、
# 可随时清空、config inference.kv_persist=false 即整面关闭（写读全停）
KV_DIR = Path(__file__).resolve().parent / "kvstore"
_KV_HINT_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")  # 会话 id 形状，防路径穿越

config = load_config()
INFERENCE_CFG = config.get("inference") or {}
REMOTE_CFG = INFERENCE_CFG.get("remote") or {}
# env 覆盖=运行时改拓扑不必重打包/改 config：非空即赢，覆盖 config 里的值。
# 语义：env 非空 > config 非空 > 空（拒启，见 _require_bind_host）——config 缺省为空，
# **不再回落 127.0.0.1**（单机部署走 inference.mode: inprocess，根本不起本服务）。
HOST = os.environ.get("INFER_REMOTE_HOST", "").strip() or str(REMOTE_CFG.get("host") or "").strip()
# INFER_PORT：多实例第二档的端口覆盖（与 INFER_MODEL_REPO 配对；Q10 固定端口 14163 起顺延）
PORT = int(os.environ.get("INFER_PORT") or REMOTE_CFG.get("port", 14161))
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
    model_name: str = ""                          # /v1/models 报告用
    facade_cache = None                           # T6 门面专用缓存（不入注册表、不 TTL；
                                                  # 聊天客户端全量重发历史 → 前缀匹配白拿增量预填）
    facade_stream = None                          # 门面 prompt 字节流（上位解）：与 facade_cache
                                                  # 配对；会话延续（指纹校验）时走「记录流+增量段」，
                                                  # 跨轮 token 纯追加，全部缓存类型可复用前缀
    gen_sem: Optional[asyncio.Semaphore] = None   # 生成并发闸（T7 参数化：MLX=1 即旧
                                                  # 互斥语义；vLLM=cuda.max_concurrency，
                                                  # 引擎连续批处理白拿并发）
    gens: Dict[str, Dict[str, Any]] = {}          # gen_id → {"cancel": bool}（进行中的生成）
    constraint_engine = None                      # 懒建（LLTokenizer ~1s）
    constraint_disabled = _llg is None
    dialect = None                                # ModelDialect：按模型注册表条目装配（R0）
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
            stc = tok.encode(S.dialect.markers.stc, add_special_tokens=False)
            etc = tok.encode(S.dialect.markers.etc, add_special_tokens=False)
            if len(stc) != 1 or len(etc) != 1:
                raise RuntimeError("工具调用标记不是单 token，约束状态机无法锚定")
            # 语法由方言提供（R0/D1 修复）：少传这个参数 = TypeError → 约束永久禁用
            # → e4b 裸奔发畸形 JSON（沙箱 calls=[] / schedule_create 参数崩的共同根因）
            S.constraint_engine = ToolConstraintEngine(tok, stc[0], etc[0],
                                                       S.dialect.tool_call_grammar)
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
    # 多实例第二档（Q10 多 port）：INFER_MODEL_REPO 覆盖本实例所服务的模型——
    # 同机再起一份 server 时与 INFER_PORT 配对使用（每模型一实例一端口）。
    # 只认注册表内条目（方言/revision 以注册表为单一真相），表外响亮拒启。
    _override = os.environ.get("INFER_MODEL_REPO", "").strip()
    if _override:
        _entry = model_options(_override)
        if not _entry:
            raise SystemExit(f"❌ [Server] INFER_MODEL_REPO 不在 config.yaml model_registry: "
                             f"{_override!r}——先注册（含 dialect/revision）再引用")
        model, revision, source = _override, _entry.get("revision"), "env INFER_MODEL_REPO"
    model_path = resolve_model_path(model, revision=revision)
    # INFER_BACKEND：后端环境覆盖（与 INFER_REMOTE_HOST/INFER_PORT/INFER_MODEL_REPO
    # 同惯例：env 非空即赢）。Colab/生产 GPU 档据此起 cuda，不必改 config。
    backend_name = (os.environ.get("INFER_BACKEND", "").strip()
                    or str(INFERENCE_CFG.get("backend") or ""))

    # INFER_SPEC_*：投机采样的环境覆盖（同 INFER_BACKEND 惯例：env 非空即赢）。
    # 阶段二 MTP 格要在一次跑里逐格切 mode / 草稿件 / 块大小；改 config.yaml 既
    # 污染生产文件又没法逐格切换。构造成 INFERENCE_CFG 的**副本**下发，不改全局。
    _inf_cfg = INFERENCE_CFG
    _spec_env = {}
    if os.environ.get("INFER_SPEC_MODE", "").strip():
        _spec_env["mode"] = os.environ["INFER_SPEC_MODE"].strip()
    if os.environ.get("INFER_SPEC_DRAFT_MODEL", "").strip():
        _spec_env["draft_model"] = os.environ["INFER_SPEC_DRAFT_MODEL"].strip()
    if os.environ.get("INFER_SPEC_BLOCK_SIZE", "").strip():
        _spec_env["draft_block_size"] = int(os.environ["INFER_SPEC_BLOCK_SIZE"])
    # 条目级投机覆盖（D.21 qwen38 MTP 转正）+ env 覆盖，优先级从低到高：
    # 全局段 < 注册表条目 < INFER_SPEC_*（逐键覆盖）。条目整体压制只由
    # INFER_SPEC_MODE 触发（评测臂强制 off/drafter 的口子）；只给
    # INFER_SPEC_BLOCK_SIZE 之类调参键不该把条目整个丢掉、静默跑成非投机
    # （首过评审 M4）。合并语义与 AgentEngine.create 共用同一函数（防两侧漂移）。
    _entry_spec = (model_options(model) or {}).get("speculative")
    _inf_cfg = merge_entry_speculative(_inf_cfg, model_options(model),
                                       env_forced=("mode" in _spec_env))
    if _spec_env:
        _inf_cfg = {**_inf_cfg,
                    "speculative": {**(_inf_cfg.get("speculative") or {}),
                                    **_spec_env}}
        print(f"[Server] 投机配置被环境覆盖：{_spec_env}")
    # 处置日志（首过评审 16）：条目存在时三种去向必须可辨——生效 / 被
    # INFER_SPEC_MODE 压制 / 条目本身声明 off
    if _entry_spec:
        if "mode" in _spec_env:
            print(f"[Server] 注册表条目 speculative 被 INFER_SPEC_MODE="
                  f"{_spec_env['mode']!r} 压制（条目声明 mode={_entry_spec.get('mode')!r}）")
        else:
            print(f"[Server] 注册表条目 speculative 生效：mode={_entry_spec.get('mode')}"
                  f" draft={_entry_spec.get('draft_model')}"
                  f" block={_entry_spec.get('draft_block_size')}")
    _eff_spec = (_inf_cfg.get("speculative") or {})
    if str(_eff_spec.get("mode") or "off") != "off":
        print(f"[Server] 投机生效配置：mode={_eff_spec.get('mode')} "
              f"draft={_eff_spec.get('draft_model')} "
              f"block={_eff_spec.get('draft_block_size') or _eff_spec.get('block_size')}")
    print(f"🚀 [Server] loading model: {model} (backend={backend_name or 'default'})")

    # T7 并发参数化：锁与执行器按后端能力开闸——MLX 共享单例不可并发（=1，旧语义
    # 逐字保留）；vLLM 引擎自带连续批处理，服务面放 N 路并发进去即白拿吞吐。
    n_concurrency = 1
    if backend_name == "cuda":
        n_concurrency = max(1, int((INFERENCE_CFG.get("cuda") or {}).get("max_concurrency") or 8))
    S.executor = ThreadPoolExecutor(max_workers=n_concurrency, thread_name_prefix="inference")
    S.backend = await get_backend_class(backend_name).load(
        model_path, executor=S.executor, **_inf_cfg)
    # 方言跟着所选模型的注册表条目走（与 engine.py 同一条路）；server 只消费 markers
    _opts = model_options(model)
    S.dialect = get_dialect_class(str(_opts.get("dialect") or "")).create(
        S.backend.processor, options=_opts)
    S.registry = CacheRegistry(S.backend, CACHE_TTL_SECONDS)
    S.gen_sem = asyncio.Semaphore(n_concurrency)
    if n_concurrency > 1:
        print(f"[Server] 生成并发闸开到 {n_concurrency} 路（vLLM 连续批处理）")
    S.model_name = str(model)
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
    async with S.gen_sem:
        loop = asyncio.get_running_loop()
        cloned = await loop.run_in_executor(S.executor, S.backend.clone_cache, src)
    if cloned is None:
        raise HTTPException(status_code=501, detail="后端不支持缓存克隆（客户端应冷启动回退）")
    return {"cache_id": S.registry.adopt(cloned)}


@app.delete("/v1/cache/{cache_id}", dependencies=[Depends(verify_api_key)])
async def cache_release(cache_id: str):
    return {"released": S.registry.release(cache_id)}


def _kv_path(hint: str) -> Path:
    if not _KV_HINT_RE.match(hint):
        raise HTTPException(status_code=400, detail="非法 hint（限 [A-Za-z0-9._-]）")
    return KV_DIR / f"{hint}.safetensors"


@app.post("/v1/cache/{cache_id}/persist", dependencies=[Depends(verify_api_key)])
async def cache_persist(cache_id: str, request: Request):
    """KV 落盘（Q9-a）：把注册表里的缓存序列化到服务端本地盘。开关关闭时后端
    返回 False（{persisted: false}，边端按冷路处理）。序列化在推理执行器线程内做
    且持生成锁——与 clone 同一纪律（张量归属线程 + 不落写到一半的缓存）。"""
    body = await request.json()
    path = _kv_path(str(body.get("hint", "")))
    handle = S.registry.get(cache_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="缓存不存在或已被 TTL 回收")
    loop = asyncio.get_running_loop()
    async with S.gen_sem:
        ok = await loop.run_in_executor(
            S.executor, lambda: S.backend.persist_cache(handle, str(path)))
    return {"persisted": bool(ok)}


@app.post("/v1/cache/restore", dependencies=[Depends(verify_api_key)])
async def cache_restore(request: Request):
    """KV 热恢复：盘上产物 → 新缓存句柄入注册表。无产物/开关关/校验不过 → 404（边端退基座/冷路）。"""
    body = await request.json()
    path = _kv_path(str(body.get("hint", "")))
    loop = asyncio.get_running_loop()
    async with S.gen_sem:
        handle = await loop.run_in_executor(
            S.executor, lambda: S.backend.restore_cache(str(path)))
    if handle is None:
        raise HTTPException(status_code=404, detail="无可恢复的 KV 产物")
    return {"cache_id": S.registry.adopt(handle)}


@app.delete("/v1/cache/persisted/{hint}", dependencies=[Depends(verify_api_key)])
async def cache_persisted_drop(hint: str):
    """会话删除时的服务端清理（边端 best-effort 调用；文件本可随时清空，失败无害）。"""
    path = _kv_path(hint)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"dropped": existed}


@app.post("/v1/transcribe", dependencies=[Depends(verify_api_key)])
async def transcribe(request: Request):
    """音频转写（R5-2/D.14）：edge 不再自带 whisper，整条音频路在设备 A。

    音频经 /v1/media 先行上传（内容寻址、天然去重），此处只收 media_id——与生成路
    对媒体的处理同一套，不另开上传通道。

    **跑在 S.executor（单线程）上**：与生成共用同一个推理 worker，故转写与生成在 A 上
    天然串行、不抢统一内存带宽——正是 CHARTER「单 worker 串行」那条铁律要的形状，
    白拿。（edge 侧摄入本就在 run_turn 之前串行发生，两者不并发。）

    fail-open 由调用方负责：edge 的 ingestion/parsers/asr.py 把任何异常收成
    「[Audio transcription failed: …]」标记文本，绝不打断整轮对话。
    """
    body = await request.json()
    media_id = str(body.get("media_id", ""))
    if not _MEDIA_ID_RE.match(media_id):
        raise HTTPException(status_code=400, detail="非法 media_id")
    p = MEDIA_DIR / media_id
    if not p.is_file():
        raise HTTPException(status_code=404, detail="媒体缺失（请先经 /v1/media 上传）")

    model = body.get("model") or None
    language = body.get("language") or None
    # 区间切片也在 A 做（edge 不留 librosa，见 backends/remote/asr.py）
    start = body.get("start")
    end = body.get("end")
    # A 侧就是本地实现——点名本机腿，不走 auto（auto 读的是 config 的 inference.mode，
    # 而这份 config 与设备 B 共用一份；A 上 auto 会解析成 remote 打回自己）。
    # 具体是哪条本机腿按物理承载定（mac=mlx_whisper / CUDA=faster_whisper，A3）：
    # 原先这里钉死 mlx_whisper，A 换成 CUDA 机后整个 /v1/transcribe 必 503。
    fn = get_asr_provider(resolve_local_asr_provider())
    loop = asyncio.get_running_loop()
    try:
        text = await loop.run_in_executor(
            S.executor, lambda: fn(str(p), model=model, language=language,
                                   start=start, end=end))
    except AsrProviderUnavailable as e:
        raise HTTPException(status_code=503, detail=f"ASR provider 不可用: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"转写失败: {e}")
    return {"text": text or ""}


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

    tool_specs = features.pop("tool_specs", None)
    guard = _tool_guard(tool_specs)
    if guard is not None:
        features["tool_guard"] = guard
        # 声明式约束规格（引擎原生结构化输出的后端吃这个，如 cuda/vLLM structural-tag
        # ——per-request logits_processors 已被 vLLM 0.26 拒收，2026-08-07 实测）。
        # guard 构建成功即隐含语法 builder 可用（qwen35 XML 方言在 _tool_guard
        # 内已 fail-open，走不到这里）；MLX 等逐 token 后端忽略此键，语义各自单一来源。
        # kind 随行（§7-8）：后端据此判断能否表达该语法，不能表达时必须拒。
        _tc_grammar = S.dialect.tool_call_grammar(tool_specs)
        features["tool_constraint"] = {
            "kind": _tc_grammar.kind,
            "body": _tc_grammar.body,
            "begin": S.dialect.markers.stc,
            "end": S.dialect.markers.etc,
        }

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
        await S.gen_sem.acquire()
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
                S.gen_sem.release()

            if task is None:
                _finish()
            else:
                task.add_done_callback(_finish)

    return StreamingResponse(_sse(), media_type="text/event-stream")


# ---------- T6 OpenAI 入口门面（/v1/models + /v1/chat/completions） ----------
# 同一份常驻权重顺便服务任何 OpenAI 客户端（lm-eval、编辑器插件），不双载模型。
# 无状态门面：不暴露缓存句柄/约束采样/思考预算（Agent 自家协议的控制面）；
# 服务端留一个门面专用缓存吃前缀匹配（聊天客户端全量重发历史 → 增量预填白拿）。


def _facade_streamer() -> VisibleTextStreamer:
    """把方言频道标记（思考/工具段）从门面输出里滤掉——OpenAI 客户端只该看到正文。"""
    mk = S.dialect.markers
    return VisibleTextStreamer(soc=mk.soc, eoc=mk.eoc, stc=mk.stc, etc=mk.etc,
                               in_thought=False)


@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def oai_models():
    return oai.models_response(S.model_name)


def _facade_assemble(msgs):
    """门面 prompt 组装（上位解）：客户端全量重发历史，若与上次门面会话延续
    （prompt_stream 指纹校验：已覆盖段逐字节未改 + 新首条是我们生成的 assistant
    占位）则走「已记录字节流 + 方言增量段」——跨轮 token 纯追加，T8 门对全部
    缓存类型放行前缀复用。否则（新会话/多客户端交错/方言不支持）退全量渲染。
    返回 (prompt, 尾块快照或 None, 是否走了增量路)。"""
    ps = S.facade_stream
    delta_fn = getattr(S.dialect, "build_prompt_delta", None)
    if delta_fn is not None and pstream.stream_valid(ps, msgs, None):
        out = delta_fn(msgs[ps.seen + 1:], prev_tail=ps.tail)
        if out is not None:
            delta, tail = out
            return ps.stream + delta, tail, True
    prompt = S.dialect.build_prompt(messages=msgs, tools=None, include_env=False)
    tail_fn = getattr(S.dialect, "tail_state", None)
    return prompt, (tail_fn() if tail_fn is not None else None), False


def _facade_commit(prompt: str, raw_text: str, msgs, tail, visible_text: str) -> None:
    """成功生成后的门面流提交。组装与提交都在 gen_sem 临界区内的事件循环线程
    发生（评审 M6：组装-生成-提交是一组读改写，出了临界区会被并发请求交错
    clobber；MLX 形态 gen_sem=1 即完整串行——cuda >1 形态门面缓存本就共享，
    流退化为「最后提交者赢」，正确性由指纹兜底）。
    中途断连/取消不提交——旧流仍是缓存内容的字节前缀，残局由前缀匹配消化。
    visible_text 是滤掉思考/工具频道后的正文（客户端拿到的字节）：记为占位
    assistant 消息的预期正文，客户端改写过回复（编辑重答类 UI）时下一轮指纹
    校验必须察觉并退全量（评审 M7）。"""
    if tail is not None:
        S.facade_stream = pstream.commit(prompt, raw_text, msgs, None, tail,
                                         expected_text=visible_text)


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def oai_chat_completions(request: Request):
    body = await request.json()
    err = oai.validate_messages(body.get("messages"))
    if err:
        raise HTTPException(status_code=400, detail=err)
    dialect_msgs = oai.to_dialect_messages(body["messages"])
    sampling = oai.parse_sampling(body)
    if S.facade_cache is None:
        S.facade_cache = S.backend.new_cache()
    cid = oai.new_completion_id()
    loop = asyncio.get_running_loop()

    if not bool(body.get("stream")):
        filt = _facade_streamer()

        def _run(prompt):
            last, parts, raw = None, [], []
            for ch in S.backend.stream_generate(prompt, cache=S.facade_cache,
                                                sampling=sampling, features={}):
                last = ch
                raw.append(ch.text)   # 原始字节（含思考/工具块）——门面流提交用
                v = filt.feed(ch.text)
                if v:
                    parts.append(v)
            tail = filt.close()
            if tail:
                parts.append(tail)
            return "".join(parts), last, "".join(raw)

        async with S.gen_sem:
            # 组装-生成-提交整组进临界区（评审 M6，_facade_commit docstring）
            prompt, stream_tail, used_delta = _facade_assemble(dialect_msgs)
            if used_delta:
                S.metrics["facade_stream_delta_total"] = \
                    S.metrics.get("facade_stream_delta_total", 0) + 1
            text, last, raw_text = await loop.run_in_executor(
                S.executor, lambda: _run(prompt))
            _facade_commit(prompt, raw_text, dialect_msgs, stream_tail, text)
        pt = last.prompt_tokens if last else 0
        gt = last.generation_tokens if last else 0
        return oai.completion_response(
            S.model_name, cid, text, pt, gt,
            oai.finish_reason_of(gt, sampling["max_tokens"]))

    # 流式：复用 /v1/generate 的纪律（有界队列背压 + 绝不在 finally await + done-callback 放锁）
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    stop = {"flag": False}
    filt = _facade_streamer()

    asm: Dict[str, Any] = {}   # 组装结果（在 _sse 拿到 gen_sem 后才填——评审 M6）

    def _blocking():
        last, raw, vis = None, [], []
        try:
            for ch in S.backend.stream_generate(asm["prompt"], cache=S.facade_cache,
                                                sampling=sampling, features={}):
                last = ch
                raw.append(ch.text)   # 原始字节——门面流提交用（仅完整走完才提交）
                v = filt.feed(ch.text)
                if not v:
                    if stop["flag"]:
                        return last, "", "", False
                    continue
                vis.append(v)         # 可见正文——占位 assistant 预期正文（评审 M7）
                fut = asyncio.run_coroutine_threadsafe(queue.put(v), loop)
                while True:
                    if stop["flag"]:
                        fut.cancel()
                        return last, "", "", False
                    try:
                        fut.result(timeout=0.5)
                        break
                    except FuturesTimeout:
                        continue
                    except Exception:
                        return last, "", "", False
            return last, "".join(raw), "".join(vis), True
        except Exception as e:
            print(f"[Facade] 生成异常: {type(e).__name__}: {e}")
            return last, "", "", False

    async def _sse():
        await S.gen_sem.acquire()
        task = None
        try:
            yield oai.stream_line(S.model_name, cid, role="assistant")
            asm["prompt"], asm["tail"], asm["used_delta"] = \
                _facade_assemble(dialect_msgs)
            if asm["used_delta"]:
                S.metrics["facade_stream_delta_total"] = \
                    S.metrics.get("facade_stream_delta_total", 0) + 1
            task = loop.run_in_executor(S.executor, _blocking)
            while True:
                get_q = asyncio.ensure_future(queue.get())
                await asyncio.wait({get_q, task}, return_when=asyncio.FIRST_COMPLETED)
                if get_q.done():
                    yield oai.stream_line(S.model_name, cid, content=get_q.result())
                    continue
                get_q.cancel()
                last, raw_text, vis_text, completed = task.result()
                while not queue.empty():
                    yield oai.stream_line(S.model_name, cid,
                                          content=queue.get_nowait())
                tail = filt.close()
                if completed:
                    # 提交仍持锁（放锁在 done-callback，生成线程已退出）：与下一
                    # 请求的组装不可交错。预期正文=可见流全文（含冲刷尾段）。
                    _facade_commit(asm["prompt"], raw_text, dialect_msgs,
                                   asm["tail"], vis_text + tail)
                if tail:
                    yield oai.stream_line(S.model_name, cid, content=tail)
                gt = last.generation_tokens if last else 0
                yield oai.stream_line(
                    S.model_name, cid,
                    finish_reason=oai.finish_reason_of(gt, sampling["max_tokens"]))
                yield oai.done_line()
                break
        finally:
            stop["flag"] = True

            def _finish(_f=None):
                S.gen_sem.release()

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
    if str(HOST).strip() in ("127.0.0.1", "localhost", "::1") or len(API_KEY) >= 16:
        return
    raise SystemExit(
        f"❌ [inference-server] 拒绝启动：绑定到非回环地址 {HOST!r} 但 INFER_API_KEY "
        f"缺失或过弱（长度 {len(API_KEY)} < 16）——服务会对整个可达网络裸奔。\n"
        f"   密钥过短/过弱同样拒绝。请 `./devctl.sh keygen` 生成强随机值（与设备 B 同值，"
        f"app 形态填进设置页），或把 inference.remote.host 改回 127.0.0.1。\n"
        f"   （部署铁则 §8：非回环绑定只允许出现在受信内网 + 必须带 Bearer。）"
    )


def _preflight_bind_addr() -> None:
    """启动前本机绑定地址预检：HOST 非本机任何网卡地址（EADDRNOTAVAIL）立即拒启。

    实机事故（2026-07-19）：算力端在别的设备上启动而 config 的 host 还填着另一台设备的
    IP → uvicorn 报晦涩的 `[Errno 49] can't assign requested address`。就地小副本，与
    backend/app.py 的 preflight_bind_addr 同语义（三服务各自独立 venv，不共享代码）。
    域名解析失败（gaierror）等其余错误静默放行，留给真实 bind 报错。
    """
    import errno
    import socket
    h = str(HOST or "").strip()
    if h in ("", "127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        return
    fam = socket.AF_INET6 if ":" in h else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((h, 0))
    except OSError as e:
        if getattr(e, "errno", None) == errno.EADDRNOTAVAIL:
            raise SystemExit(
                f"❌ [inference-server] 拒绝启动：绑定地址 '{h}' 不是本机任何网卡的地址（EADDRNOTAVAIL）。\n"
                f"   config 的 inference.remote.host（或环境变量 INFER_REMOTE_HOST）应填**本机**"
                f"（对外提供服务的这台设备自己）的 IP。\n"
                f"   查看本机地址：`tailscale ip -4` 或 `ifconfig | grep 'inet '`；\n"
                f"   若该地址属于另一台设备，请到那台设备上启动本服务。"
            )
    finally:
        s.close()


def _require_bind_host() -> None:
    """配置完整性：绑定地址为空（config 与 env 均未配置）= 拒启，不再回落 127.0.0.1。

    config.yaml 的 inference.remote.host 缺省为空（通用模板不钉机器 IP）；算力端专属地址
    落本机 .env.local（INFER_REMOTE_HOST）或 app 设置。空地址 = 没告诉本服务绑哪，与其让
    uvicorn 拿空串拼出晦涩失败不如启动即拒。先于密钥/地址检查（配置完整性最优先）。
    """
    if str(HOST or "").strip():
        return
    raise SystemExit(
        "❌ [inference-server] 拒绝启动：绑定地址为空（config 的 inference.remote.host 与环境变量\n"
        "   INFER_REMOTE_HOST 均未配置）。\n"
        "   请在本机（算力端）.env.local 写 INFER_REMOTE_HOST=<本机 tailnet IP>；\n"
        "   单机部署请改 inference.mode: inprocess（不再需要本服务）。"
    )


if __name__ == "__main__":
    _require_bind_host()        # 配置完整性先于密钥/地址检查：空绑定地址立即拒启
    _enforce_bind_guardrail()   # 先于加载权重：省掉几十秒才发现配置错
    _preflight_bind_addr()      # 密钥护栏之后：绑定地址不属本机立即拒启
    uvicorn.run(app, host=HOST, port=PORT)
