"""inference-server（HETERO_COMPUTE B1/D2/D7）：InferenceBackend 协议的 HTTP/SSE 服务面。

设备 A（算力机）上运行，内包一个进程内推理后端（B1 先包 MLX，A 线完成后可换 CUDA——
换后端只改 config 的 inference.backend，本服务一行不动）。设备 B 的 RemoteBackend
（backend/framework/backends/remote/）是它唯一的预期客户端。

隐私边界（§3.1）：本服务不持久化会话文本/记忆；会话**派生物**面两类：
媒体（media/，sha256 内容寻址，按最后使用时间 TTL 自动清扫，inference.media_ttl_days）
与冻结 KV（kvstore/，**缺省不落**——执行端 kv_persist 总开关放行才写，留存期随请求
逐份携带、到期清扫，DELETE /v1/kvstore 立即全清；D 方案 2026-08-31）。手动清空
media/ 也可以，但长命客户端的上传缓存最长 1 小时后才会重探/重传。

端点（把 §1.1 协议 SSE 化）：
  POST   /v1/generate                  SSE 生成流（events: meta / chunk / done / error）
  POST   /v1/generate/{gen_id}/cancel  段粒度取消（对应引擎逐 chunk 的取消检查点）
  POST   /v1/media                     内容寻址上传（sha256 去重）→ {media_id}
  HEAD   /v1/media/{media_id}          客户端上传缓存的探活+续命（缺陷三 TTL 配套）
  POST   /v1/transcribe                音频转写（R5-2/D.14：edge 不留 whisper）→ {text}
  POST   /v1/cache                     新建缓存 → {cache_id}
  POST   /v1/cache/{id}/clone          分支克隆 → {cache_id}；不支持时 501（客户端冷启动回退）
  DELETE /v1/cache/{id}                冻结/删会话时释放
  DELETE /v1/kvstore                   清空全部落盘 KV（app「立即清除」按钮；D 方案）
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
import queue
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from concurrent.futures import CancelledError as FuturesCancelled
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
    capabilities_of, merge_entry_speculative, model_options, resolve_selected_model)
from framework.utils.path_resolver import resolve_model_path
# 方言经注册面取（R0）：server 只要工具段标记（dialect.markers.stc/etc 喂约束采样），
# 不 import 任何家族私有模块——直接 import gemma4 的 ResponseParser 就是把
# 「引擎不认模型家族字面量」（CHARTER 方言层）在服务入口这儿重新违反一遍。
from framework.dialects import get_dialect_class
from framework.core.constrained import ToolConstraintEngine, ToolCallGuard, llguidance as _llg
from framework.core.backend_protocol import CancelScope
from framework.backends import (AsrProviderUnavailable, get_asr_provider,
                                get_backend_class, resolve_local_asr_provider)
from framework.core import prompt_stream as pstream
from framework.core.stream_filter import VisibleTextStreamer

import batch_lane as _bl     # 同目录：§7-0① 批处理泳道（import 期零 mlx/config 依赖）
import openai_facade as oai  # 同目录纯逻辑层（T6 门面）

MEDIA_DIR = Path(__file__).resolve().parent / "media"
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}(\.[A-Za-z0-9]{1,8})?$")  # 内容寻址名，防路径穿越
# KV 冻结落盘（Q9-a；D 方案后）：会话 KV 的服务端存放处。与 media/ 同一隐私档位——
# 本机磁盘、可随时清空。**写不写由执行端总开关决定**（本机 config 的 kv_persist 对
# 服务端已不再是闸，见 lifespan 的能力强开注释）；本机想整面拒收走
# INFER_KV_STORE_DISABLE=1（机主对自家磁盘的最后否决权）。
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
# media/ TTL（分离部署缺陷三）：算力端不该无限囤积用户媒体。按**最后使用时间**清
# （用到即 touch 续命，见 _media_paths / HEAD /v1/media），客户端上传缓存过期先
# HEAD 探活、404 才重传——两端差着量级（客户端鲜度 1h << 这里默认 7 天），不存在
# 「刚确认就被清」的窗。0 = 关闭清扫（旧行为）。天数须 ≥1（守住上面那个量级差）。
def _ttl_seconds(days_raw, key: str) -> float:
    """<key> 天数 → 秒（media_ttl_days / kv_ttl_days 共用）。「须 ≥1 天或恰为 0
    （关闭）」不能只是散文（首评 S）：media 侧客户端鲜度 1h 靠量级差防「刚确认就
    被清」。负数/空串/null/布尔/非有限数一律拒启（阶段复审 M1 + 修正评审：
    `days_raw or 0` 曾把 -1/'' 静默放成「关闭」；`.nan`/`.inf` 会让清扫比较恒假=
    静默关闭；YAML 的 true 会被 float() 悄悄化成 1，shell_exec 预算旋钮同款先例）。
    用户想关就写 0，别的写法都是配置错误——响亮优于猜意图，§S.3。"""
    import math
    if isinstance(days_raw, bool):
        raise SystemExit(f"❌ [Server] inference.{key} 不接受布尔值：{days_raw!r}"
                         f"——写天数，关闭清扫写 0")
    try:
        days = float(days_raw)
    except (TypeError, ValueError):
        raise SystemExit(f"❌ [Server] inference.{key} 不是数值：{days_raw!r}"
                         f"——关闭清扫请显式写 0")
    if not math.isfinite(days):
        raise SystemExit(f"❌ [Server] inference.{key} 须为有限数：{days_raw!r}"
                         f"（nan/inf 会让清扫静默失效）")
    if days != 0 and days < 1:
        raise SystemExit(f"❌ [Server] inference.{key} 须为 0（关闭）或 ≥1 天，"
                         f"收到 {days}——负数不是关闭，<1 天是竞态窗/误伤热会话")
    return days * 86400


MEDIA_TTL_SECONDS = _ttl_seconds(INFERENCE_CFG.get("media_ttl_days", 7), "media_ttl_days")
# kvstore 兜底留存（kv_persist D 方案）：正主留存期由执行端随每次 persist 请求携带
# （逐份记在 .retention.json）；本值只兜「没带 ttl 的旧存量/裸调用」。0 = 不兜底清扫
# （有逐份到期的照清）。
KV_TTL_SECONDS = _ttl_seconds(INFERENCE_CFG.get("kv_ttl_days", 14), "kv_ttl_days")
# 机主否决权（首评 S）：能力面默认恒开（决策在执行端），但磁盘属于算力机——
# 旧版执行端（无门）或共享钥匙的第二客户端仍可能来写；机主设 1 即整面拒收
# （persist 回 {persisted:false}、restore 404），清理端点照常可用。
KV_STORE_DISABLED = os.environ.get("INFER_KV_STORE_DISABLE", "").strip() in ("1", "true")
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
    model_repo: str = ""                          # /health 模型身份（分离部署缺陷二：执行端
    model_revision: Optional[str] = None          # RemoteBackend 装配期据此比对本地选型，
    model_capabilities: list = []                 # 档位错开响亮拒启，不是首轮生成静默出垃圾）
    facade_cache = None                           # T6 门面专用缓存（不入注册表、不 TTL；
                                                  # 聊天客户端全量重发历史 → 前缀匹配白拿增量预填）
    facade_stream = None                          # 门面 prompt 字节流（上位解）：与 facade_cache
                                                  # 配对；会话延续（指纹校验）时走「记录流+增量段」，
                                                  # 跨轮 token 纯追加，全部缓存类型可复用前缀
    gen_sem: Optional[asyncio.Semaphore] = None   # 生成并发闸（T7 参数化：MLX=1 即旧
                                                  # 互斥语义；vLLM=cuda.max_concurrency，
                                                  # 引擎连续批处理白拿并发）
    gens: Dict[str, Dict[str, Any]] = {}          # gen_id → {"cancel": bool}（进行中的生成）
    batch_lane = None                             # §7-0① 批处理泳道（MLX 旋钮>1 时武装）
    single_busy: int = 0                          # 单流路在生成的计数：泳道据此让路（交互优先）
    constraint_engine = None                      # lifespan 预建（m9）：LLTokenizer 序列化是唯一
                                                  # 碰 Rust 分词器的一步，只许在无在飞生成时做
    constraint_disabled = _llg is None            # 启动期定案（config 关 / 无 llguidance / 方言不支持）
    constraint_probe_ok: Optional[bool] = None    # 启动探针结论（None=未跑/已禁用）；/metrics 巡检面
    constraint_failed_keys: set = set()           # 请求期编译失败的工具集摘要（负缓存：同集只响一次）
    dialect = None                                # ModelDialect：按模型注册表条目装配（R0）
    metrics = {"generations_total": 0, "cancelled_total": 0, "errors_total": 0,
               "constraint_failopen_total": 0}    # 请求期按次 fail-open 计数（m9 分级兜底）


S = ServerState()


def _unbusy(_f=None) -> None:
    S.single_busy = max(0, S.single_busy - 1)


def _busy_executor_call(loop, fn):
    """在推理执行器上跑 GPU 工作并全程压住 single_busy（批泳道据此让路）。

    递减挂在执行器任务**真正结束**上（评审 M6/M7）：协程在 await 处被取消时
    线程还在跑，提前递减会让批泳道与单流路同时压 GPU——正是让路机制要防的事。"""
    S.single_busy += 1
    fut = loop.run_in_executor(S.executor, fn)
    fut.add_done_callback(_unbusy)
    return fut


def _cancel_gen(gen_state: Dict[str, Any]) -> None:
    """单流路取消（断连 finally 与 /cancel 端点共用；只在事件循环线程调，不 await）。

    置标志 = 推理线程在下一个 chunk 边界停（旧语义，MLX 等无钩子后端仍只靠它）；
    scope.cancel() = 同步执行后端登记的中止钩子（CUDA：唤醒阻塞的首包 get + 私有循环
    engine.abort），预填期也立即生效。不在这里 close 后端生成器：它此刻多半正被推理
    线程 next() 着，跨线程 close 只会 `ValueError: generator already executing`——
    close 由推理线程自己在退出时做（_blocking_generate 的 finally）。
    S.gens 的注销仍挂在推理线程退出上（它是真实占用）；本函数只把「取消→退出」的
    延迟从「首个 chunk」缩到「后端响应中止」。"""
    gen_state["cancel"] = True
    scope = gen_state.get("scope")
    if scope is not None:
        scope.cancel()


# 后端对「调用方取消」的应答签名（CUDA：中止钩子触发后 get 抛的 RuntimeError 文案，
# 见 backends/cuda/backend.py stream_generate）。后端未定义专用异常类型，只能按文案认。
_CALLER_CANCEL_MARK = "已被调用方取消"


def _note_cancel_exc(e: BaseException) -> None:
    """取消标志已置时后端抛的异常一律按取消处理（不计 error），但非「调用方取消」
    应答的异常（如 CUDA 的「600s 无产出（引擎挂死？）」）留一行日志，诊断不丢。"""
    if isinstance(e, (asyncio.CancelledError, FuturesCancelled)) \
            or _CALLER_CANCEL_MARK in str(e):
        return
    print(f"[Server] 取消后后端异常（按取消处理）: {type(e).__name__}: {e}")


def _close_stream(stream) -> None:
    """在拥有生成器的推理线程里显式关闭后端流：确定性地执行后端 finally（CUDA 的
    fut.cancel/abort 链、MLX 的资源收尾），不依赖 CPython 引用计数回收的时机。"""
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception as e:  # noqa: BLE001 —— 收尾路径只记账
            print(f"[Server] 关闭后端生成流异常（已忽略）: {type(e).__name__}: {e}")


# 启动探针用的最小工具声明：只为在 lifespan 内走一遍「方言语法 builder → llguidance
# 编译」链路，把「方言按设计不出语法」（qwen35 XML 调用体抛 NotImplementedError）这种
# 确定性结论从首个工具请求提前到启动期——用户看到的是启动日志，不是请求期静默裸奔。
def _constraint_probe_tools():
    # 每次现造：交给方言 builder 的是一次性对象，builder 原地规范化也污染不到常量
    return [{"name": "_startup_probe", "description": "constraint engine startup probe",
             "parameters": {"type": "object", "properties": {"q": {"type": "string"}},
                            "required": ["q"]}}]


def _build_constraint_engine() -> None:
    """约束采样引擎**启动期预建**（m9，第 4 层阶段复审立案）。

    只许在 lifespan 内、`S.backend` / `S.dialect` 就绪之后调用：此时没有在飞生成，
    对共享 HF 分词器 `encode` 与 llguidance 的 `from_tokenizer`（copy + to_str 序列化
    Rust 分词器）都安全。旧实现在首个带 tool_specs 的请求里懒建——调用点落在
    `gen_sem` 之外的事件循环线程，与推理线程的 encode/detok 并发 → `Already borrowed`
    → 兜底把 `constraint_disabled` 永久置 True 且只打一行日志：进程级静默裸奔。

    失败分级（启动期就响，不等首请求）：
    - config 未启用 / 环境无 llguidance → 禁用并明示（这两支与 engine.py 同策）；
    - 方言 builder 抛 NotImplementedError = 方言按设计不出语法 → 禁用并明示（确定性
      结论；**先于建引擎**判——qwen35 这类方言不该为用不上的引擎付 LLTokenizer 代价，
      更不该因它拒启，评审 M1）；
    - 引擎建不起来（标记非单 token、分词器序列化失败）= 结构性故障 → **拒启**
      （config 明确要求约束采样而给不出，静默裸奔就是 m9 本身；要裸奔请显式关
      inference.constrained_tool_calls）。此支只能落在权重装载之后（引擎要的就是
      装好的分词器），与 lifespan 里其它「装载前拒启」不同——故 m9 落地前须真机冒烟；
    - llguidance 编译探针其它异常 → 不下结论、不禁用，交请求期按次分级（probe_ok=False
      上 /metrics）。
    """
    S.constraint_probe_ok = None
    if not bool(INFERENCE_CFG.get("constrained_tool_calls", False)):
        S.constraint_disabled = True
        S.constraint_engine = None
        print("[Server] 约束采样未启用（inference.constrained_tool_calls=false）")
        return
    if _llg is None:
        S.constraint_disabled = True
        S.constraint_engine = None
        print("⚠️ [Server] constrained_tool_calls=true 但本环境无 llguidance——"
              "约束采样禁用（自由采样 + 解析纠错闭环兜底）")
        return
    try:
        S.dialect.tool_call_grammar(_constraint_probe_tools())
    except NotImplementedError as e:
        S.constraint_disabled = True
        S.constraint_engine = None
        print(f"[Server] 方言声明不出调用体语法，约束采样整段禁用"
              f"（自由采样 + 解析纠错闭环）: {e}")
        return
    except Exception as e:
        # 探针声明未必合方言口味：不据此下结论，照样建引擎，编译探针那一步再看
        print(f"⚠️ [Server] 方言语法探针未通过（不据此禁用）: {type(e).__name__}: {e}")
    try:
        proc = S.backend.processor
        tok = proc.tokenizer if hasattr(proc, "tokenizer") else proc
        stc = tok.encode(S.dialect.markers.stc, add_special_tokens=False)
        etc = tok.encode(S.dialect.markers.etc, add_special_tokens=False)
        if len(stc) != 1 or len(etc) != 1:
            raise RuntimeError("工具调用标记不是单 token，约束状态机无法锚定")
        # 语法由方言提供（R0/D1 修复）：少传这个参数 = TypeError → 以前是约束永久禁用
        # → e4b 裸奔发畸形 JSON；现在是启动期拒启，漂移当场可见
        engine = ToolConstraintEngine(tok, stc[0], etc[0], S.dialect.tool_call_grammar)
    except Exception as e:
        raise SystemExit(
            f"❌ [Server] 约束采样引擎预建失败：{type(e).__name__}: {e}——"
            f"config 要求约束采样而引擎建不起来是结构性故障，拒启优于静默裸奔；"
            f"确需无约束运行请显式关 inference.constrained_tool_calls") from e
    S.constraint_engine = engine
    S.constraint_disabled = False
    S.constraint_failed_keys = set()
    try:
        engine.matcher_for(_constraint_probe_tools())
    except Exception as e:
        # 编译探针未过：不据此禁用（探针规格未必合方言口味），请求期按工具集分级；
        # 结论上 /metrics（constraint_probe_ok=False）——"就绪"不等于"会约束"
        S.constraint_probe_ok = False
        print(f"⚠️ [Server] 约束采样引擎就绪但启动编译探针未通过（不禁用，请求期按次 fail-open）: "
              f"{type(e).__name__}: {e}")
        return
    S.constraint_probe_ok = True
    print("[Server] 约束采样引擎就绪（llguidance，启动期预建 + 方言/编译探针通过）")


def _tool_guard(tool_specs) -> Optional[ToolCallGuard]:
    """服务端按客户端捎带的工具声明重建约束 guard。

    m9 后本函数在请求期**不碰 Rust 分词器**：引擎由 `_build_constraint_engine` 在
    lifespan 预建，这里只做语法编译/复用（llguidance 自持 LLTokenizer）。调用位置也
    随之挪到**推理线程、gen_sem 已持有之后**（`_arm_tool_guard`）：`matcher_for` 对
    同工具集返回**同一个**缓存 matcher 并 `reset()` 它——在事件循环线程做会把在飞生成
    正在消费的状态机拨回起点（评审 M4）；MLX gen_sem=1 下进了临界区即与一切生成互斥。
    失败分级：
    - 启动期已禁用 → None 直接放行（结论在启动日志里，不在这里重复）；
    - 未禁用却无引擎 = lifespan 未走预建（入口漂移/测试桩错）→ 响亮抛错，不静默裸奔
      （在推理线程抛 → SSE error 事件 + errors_total，不是裸 500）；
    - 请求期异常（坏 schema、语法编译错）→ **本请求** fail-open：计数 + 日志明示，
      不再永久禁用——一个客户端的坏工具集不得毒化全进程后续所有客户端；同一工具集
      进负缓存，再来只计数不再重编译/重刷屏（评审 M3：编译失败 matcher_for 不缓存）。
    """
    if not tool_specs or S.constraint_disabled:
        return None
    eng = S.constraint_engine
    if eng is None:
        raise RuntimeError("约束采样未禁用但引擎未预建——lifespan 须先 _build_constraint_engine()")
    # 负缓存键 = 引擎正缓存的同一内容摘要（引擎拥有键语义；这里不另造一套）
    key = eng._tools_key(tool_specs)
    if key in S.constraint_failed_keys:
        S.metrics["constraint_failopen_total"] += 1
        return None
    try:
        return ToolCallGuard(eng.matcher_for(tool_specs), eng.stc_id, eng.etc_id, eng.vocab_size)
    except Exception as e:
        S.metrics["constraint_failopen_total"] += 1
        S.constraint_failed_keys.add(key)
        print(f"⚠️ [Server] 本请求工具集约束构建失败，本次 fail-open（同集后续只计数不重试；"
              f"累计 {S.metrics['constraint_failopen_total']}）: {type(e).__name__}: {e}")
        return None


def _arm_tool_guard(features: Dict[str, Any], tool_specs) -> Optional[ToolCallGuard]:
    """在推理线程、gen_sem 临界区内为本次生成装配约束面（guard + 声明式规格）。

    返回 guard（MLX 逐 token 后端消费 features.tool_guard；cuda/vLLM 首选
    features.tool_constraint 的 structural-tag，guard 只是其回退）。"""
    guard = _tool_guard(tool_specs)
    if guard is not None:
        features["tool_guard"] = guard
        # 声明式约束规格（引擎原生结构化输出的后端吃这个，如 cuda/vLLM structural-tag
        # ——per-request logits_processors 已被 vLLM 0.26 拒收，2026-08-07 实测）。
        # guard 构建成功即隐含语法 builder 可用（qwen35 XML 方言在启动探针里已
        # 整段禁用，走不到这里）；MLX 等逐 token 后端忽略此键，语义各自单一来源。
        # kind 随行（§7-8）：后端据此判断能否表达该语法，不能表达时必须拒。
        _tc_grammar = S.dialect.tool_call_grammar(tool_specs)
        features["tool_constraint"] = {
            "kind": _tc_grammar.kind,
            "body": _tc_grammar.body,
            "begin": S.dialect.markers.stc,
            "end": S.dialect.markers.etc,
        }
    return guard


_touch_failed_once = False


def _touch_media(p: Path) -> None:
    """用到即续命：TTL 按最后使用时间算，活跃会话反复引用的媒体不会被清走。"""
    global _touch_failed_once
    try:
        os.utime(p)
    except OSError as e:
        if not _touch_failed_once:   # 只响一次：续命全废时清扫会反复清在用媒体，
            _touch_failed_once = True  # 症状只是费带宽（客户端重传自愈），但得留痕
            print(f"[Server] ⚠️ media touch 失败（续命失效，TTL 清扫将偏激进）: {e}")


def _media_paths(ids, kind: str):
    """media_id 列表 → 服务端本地文件路径列表；缺失/非法 id 跳过并告警（不炸整轮）。"""
    out = []
    for mid in ids or []:
        if not _MEDIA_ID_RE.match(str(mid)):
            print(f"[Server] 非法 {kind} media_id，跳过: {mid!r}")
            continue
        p = MEDIA_DIR / mid
        if p.is_file():
            _touch_media(p)
            out.append(str(p))
        else:
            print(f"[Server] {kind} 媒体缺失（客户端需重传），跳过: {mid}")
    return out


def _sweep_media() -> int:
    """TTL 清扫：删掉超过 MEDIA_TTL_SECONDS 没被使用的媒体（含中断残留的 .tmp）。
    返回清掉的文件数。媒体是内容寻址的派生缓存——正主在执行端会话里，删了客户端
    会按需重传（配合 RemoteBackend 的缓存鲜度 + HEAD 探活，不产生静默缺媒体）。"""
    if MEDIA_TTL_SECONDS <= 0:
        return 0
    cutoff = time.time() - MEDIA_TTL_SECONDS
    n = 0
    try:
        entries = list(MEDIA_DIR.iterdir())
    except OSError:
        return 0
    for p in entries:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            continue   # 并发 touch/删除竞态：下一轮再说
    return n


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

    # INFER_KV_BITS：KV 缓存量化的环境覆盖（阶段三测量场；同 INFER_SPEC_* 惯例：
    # env 非空即赢、构造副本不改全局）。"dense"/"off"/"null" 显式回稠密（压过
    # config）；数值档全部经 float——注意 scheme 由 config kv_quant_scheme 决定
    # （缺省 turboquant 时整数值也走 TurboQuant，不是均匀量化——评审实读
    # turboquant_enabled 的结论，别按位宽形状臆断路径）。
    # 非法/越域值**启动期**拒启（上游 _validate_bits 到首次生成才炸——那时一条
    # 测量臂已静默空跑，评审「拒启优于静默跑错口径」的窗口就在这里）。
    _kv_env = os.environ.get("INFER_KV_BITS", "").strip()
    if _kv_env:
        if _kv_env.lower() in ("dense", "off", "null"):
            _inf_cfg = {**_inf_cfg, "kv_bits": None}
            print("[Server] KV 量化被环境覆盖：稠密（kv_bits=None）")
        else:
            try:
                _kv_val = float(_kv_env)
            except ValueError:
                raise SystemExit(f"❌ [Server] INFER_KV_BITS 不是数值/dense："
                                 f"{_kv_env!r}——拒启优于静默跑错口径")
            import math
            if (not math.isfinite(_kv_val) or _kv_val < 1
                    or round(_kv_val * 2) / 2 != _kv_val):
                raise SystemExit(f"❌ [Server] INFER_KV_BITS 越域：{_kv_val!r}"
                                 f"——须为 ≥1 的半整数（上游 _validate_bits 口径，"
                                 f"启动期就拒，不等首次生成才炸）")
            _inf_cfg = {**_inf_cfg, "kv_bits": _kv_val}
            print(f"[Server] KV 量化被环境覆盖：kv_bits={_kv_val}"
                  f"（scheme={_inf_cfg.get('kv_quant_scheme') or 'turboquant'}）")
    print(f"🚀 [Server] loading model: {model} (backend={backend_name or 'default'})")

    # T7 并发参数化：锁与执行器按后端能力开闸——MLX 共享单例不可并发（=1，旧语义
    # 逐字保留）；vLLM 引擎自带连续批处理，服务面放 N 路并发进去即白拿吞吐。
    n_concurrency = 1
    if backend_name == "cuda":
        n_concurrency = max(1, int((INFERENCE_CFG.get("cuda") or {}).get("max_concurrency") or 8))
    S.executor = ThreadPoolExecutor(max_workers=n_concurrency, thread_name_prefix="inference")
    # KV 持久化能力恒开（kv_persist D 方案）：**要不要落盘由执行端总开关定夺**
    # （config_loader.kv_persist_enabled，缺省 false），本端只提供能力——若这里也读
    # config 的 kv_persist（现缺省 false）就成了两侧手抄同步：执行端开了、算力端
    # 还得改一遍 config 才生效，正是分离部署审计要消灭的形态。盘由逐份留存 +
    # kv_ttl_days 兜底 + DELETE /v1/kvstore 守着。
    S.backend = await get_backend_class(backend_name).load(
        model_path, executor=S.executor, **{**_inf_cfg, "kv_persist": True})
    # 方言跟着所选模型的注册表条目走（与 engine.py 同一条路）；server 只消费 markers
    _opts = model_options(model)
    S.dialect = get_dialect_class(str(_opts.get("dialect") or "")).create(
        S.backend.processor, options=_opts)
    # m9：约束采样引擎在此预建——无在飞生成，是唯一能安全碰 Rust 分词器的窗口
    _build_constraint_engine()
    S.registry = CacheRegistry(S.backend, CACHE_TTL_SECONDS)
    S.gen_sem = asyncio.Semaphore(n_concurrency)
    if n_concurrency > 1:
        print(f"[Server] 生成并发闸开到 {n_concurrency} 路（vLLM 连续批处理）")

    # §7-0① 批处理泳道（MLX 引擎内连续批处理，上游 BatchGenerator 直驱）：
    # 旋钮 = env INFER_MLX_MAX_CONCURRENCY（非空即赢，测量场用）> config
    # inference.mlx.max_concurrency > 1。缺省 1 = 泳道不武装，行为与旧版逐字节一致。
    # 只对 MLX 后端武装（cuda 走 gen_sem>1 的 vLLM 路，remote 不是 server 后端）。
    _mlx_n_env = os.environ.get("INFER_MLX_MAX_CONCURRENCY", "").strip()
    try:
        _mlx_n = max(1, int(_mlx_n_env or (INFERENCE_CFG.get("mlx") or {})
                            .get("max_concurrency") or 1))
    except (TypeError, ValueError):
        raise SystemExit(f"❌ [Server] INFER_MLX_MAX_CONCURRENCY / "
                         f"inference.mlx.max_concurrency 不是整数："
                         f"{_mlx_n_env!r}——修正后再启（拒启优于静默取 1）")
    if backend_name in ("", "mlx") and _mlx_n > 1:
        # wired limit 钉死在上游同款目标值（评审 M10）：BatchGenerator 与单流路
        # 各自的 wired_limit(model) 上下文嵌套时，退出顺序会把限值来回拉扯
        # （单流路先退 = 批中途掉回低限）。预先钉成同值让所有嵌套进出幂等。
        # 只在泳道武装时钉——缺省档行为保持逐字节不变。
        try:
            import mlx.core as _mx
            _rec = int(_mx.device_info()["max_recommended_working_set_size"])
            _mx.set_wired_limit(_rec)
            print(f"[Server] wired limit 预钉 {_rec / 2**30:.1f}G"
                  f"（嵌套 wired_limit 上下文自此幂等）")
        except Exception as _e:
            print(f"[Server] wired limit 预钉失败（嵌套语义退回上游缺省）: {_e}")
        S.batch_lane = _bl.BatchLane(
            build_generator=_bl.build_upstream_context(S.backend, max_num_seqs=_mlx_n),
            max_num_seqs=_mlx_n,
            interactive_busy=lambda: S.single_busy > 0)
        print(f"[Server] 批处理泳道武装：max_num_seqs={_mlx_n}"
              f"（opt-in：features.batch_ok；交互恒优先）"
              + ("" if _mlx_n_env == "" else f"［env 覆盖 {_mlx_n_env}］"))
    S.model_name = str(model)
    # /health 身份三件套（含 INFER_MODEL_REPO 覆盖后的定案值）：capabilities 走注册表
    # 条目（表外/本地路径回落 {"text"}，与路由消费面同口径）；启动时定案，/health 零查询
    S.model_repo = str(model) if model else ""   # 空选型不报 "None"：报空串=「未报身份」，
    S.model_revision = revision                  # 执行端护栏照样 fail-closed（首评 nit）
    S.model_capabilities = sorted(capabilities_of(model_options(model)))
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    async def _sweep_loop():
        while True:
            try:
                await asyncio.sleep(300)
                n = S.registry.sweep()
                if n:
                    print(f"🧹 [Server] TTL 回收 {n} 份空闲缓存（剩余 {len(S.registry)}）")
                nm = await asyncio.to_thread(_sweep_media)   # 逐文件 stat 不占事件循环
                if nm:
                    print(f"🧹 [Server] TTL 清扫 {nm} 份闲置媒体（media_ttl_days，"
                          f"客户端需要时会重传）")
                nk = await asyncio.to_thread(_sweep_kvstore)
                if nk:
                    print(f"🧹 [Server] 到期清扫 {nk} 份落盘 KV（逐份留存/kv_ttl_days，"
                          f"解冻退冷路）")
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
    if S.batch_lane is not None:
        try:
            S.batch_lane.shutdown()   # 先收泳道（驱动线程停在步边界）再放后端
        except Exception as e:
            print(f"[Server] 批泳道收摊异常: {e}")
    try:
        S.backend.shutdown()
    except Exception as e:
        print(f"[Server] 后端释放异常: {e}")


app = FastAPI(title="Agent inference-server", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    # 免鉴权探活：lifespan 完成（模型已加载）后才受理 → 200 即就绪。
    # 身份字段（分离部署缺陷二，范式同 backend #85 /health 与 RAG 计算面 /health）：
    # 执行端 RemoteBackend 装配期比对本地选型与这里的真身，档位错开当场拒启——
    # 两侧 .model-selection 各管各的，没有发现机制时错开=分词器/方言驱动错权重静默出垃圾。
    # service 供区分「端口被外来程序占用」与「旧版服务未报身份」。零 IO（字段启动期定案）。
    return {"status": "ok", "service": "macagent-inference",
            "model_repo": S.model_repo, "model_revision": S.model_revision,
            "capabilities": S.model_capabilities,
            "inference_options": (S.backend.runtime_options()
                                  if callable(getattr(S.backend, "runtime_options", None))
                                  else None)}


@app.get("/metrics", dependencies=[Depends(verify_api_key)])
async def metrics():
    # cache_ttl_seconds：客户端（长稳工装）据此校验「我读的 config」与「正在跑的服务」
    # 是不是同一个 TTL。CACHE_TTL_SECONDS 在 import 时绑定，改了 config 不重启服务，
    # 两边就悄悄分家——而工装的「离开」冷档窗宽正是按 TTL 算的，分家即冷重建零样本。
    out = {**S.metrics, "cache_count": len(S.registry), "active_generations": len(S.gens),
           "cache_ttl_seconds": CACHE_TTL_SECONDS,
           # m9 冒烟/巡检面：预建是否就绪（False = 启动期已禁用，原因在启动日志）
           "constraint_engine_ready": S.constraint_engine is not None
                                      and not S.constraint_disabled,
           # 探针结论：True=方言+编译链路启动期走通；False=引擎在但探针没过（请求期可能
           # 按次 fail-open，看 constraint_failopen_total）；None=已禁用/未跑
           "constraint_probe_ok": S.constraint_probe_ok}
    read_spec_metrics = getattr(S.backend, "speculative_metrics", None)
    out["speculative_decode"] = (await read_spec_metrics()
                                 if callable(read_spec_metrics) else
                                 {"status": "unknown", "source": "unsupported",
                                  "reason": "backend_without_speculative_metrics",
                                  "draft_tokens": None, "accepted_tokens": None,
                                  "num_drafts": None})
    # Read busy state after the metrics await, which may yield to new requests.
    # Includes OpenAI facade work, which does not enter S.gens.
    out["active_gpu_work"] = S.single_busy + (S.batch_lane.active_count if S.batch_lane is not None else 0)
    if S.batch_lane is not None:
        out["batch_lane"] = {"active": S.batch_lane.active_count,
                             # decoding ≠ active：后者含引擎内排队的行——
                             # 「真同批共跑」的证据只能看 decoding（评审 C4）
                             "decoding": S.batch_lane.decoding_count,
                             "paused": S.batch_lane.paused,
                             "steps_total": S.batch_lane.steps_total,
                             "dead": S.batch_lane.dead,
                             "max_num_seqs": S.batch_lane.max_num_seqs}
    return out


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
        cloned = await _busy_executor_call(loop, lambda: S.backend.clone_cache(src))
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


# ── kvstore 逐份留存（kv_persist D 方案：执行端定留存期，本端只记账+到期清扫）──
KV_RETENTION_FILE = KV_DIR / ".retention.json"
_kv_retention_lock = threading.Lock()


def _kv_retention_load() -> dict:
    """无锁读是安全的**仅因**所有写方都走 tmp+原子改名（读到的要么旧版要么新版，
    绝无半个 JSON）——给账本加第二种写法前先读这行。"""
    try:
        data = json.loads(KV_RETENTION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _kv_retention_update(mutate) -> bool:
    """读-改-写一体（锁内）：persist/restore/drop/sweep 多线程并发碰同一份账本。
    fail-open：账本写不动最坏退回「按服务端兜底 TTL 清」，不炸持久化主路；
    返回写入成败供调用方在兜底也关闭时响亮（见 _kv_note_retention）。"""
    with _kv_retention_lock:
        data = _kv_retention_load()
        mutate(data)
        try:
            KV_DIR.mkdir(parents=True, exist_ok=True)
            tmp = KV_RETENTION_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(KV_RETENTION_FILE)
            return True
        except OSError as e:
            print(f"[Server] ⚠️ kv 留存账本写入失败（退兜底 TTL）: {e}")
            return False


def _kv_note_retention(hint: str, ttl_days) -> None:
    """persist/restore 成功后记/续到期。ttl_days 来自执行端请求；非法/缺省回落服务端
    兜底值（便利参数不拒请求——隐私门在执行端开关，这里只是清扫节奏）。
    上限钳 365 天（首评 S）：磁盘是本机的，authed 客户端传 1e6 天=变相永久留存，
    inf 还会把账本写成非标 JSON；与 app 设置面的 1..365 钳一致。"""
    import math
    try:
        days = float(ttl_days)
        if isinstance(ttl_days, bool) or not math.isfinite(days) or days < 1:
            days = 0.0
    except (TypeError, ValueError):
        days = 0.0
    days = min(days, 365.0)
    seconds = days * 86400 if days else KV_TTL_SECONDS
    if seconds <= 0:
        return   # 双双关闭：不记账（该份只随会话删除/手动清除走）
    expires = time.time() + seconds
    ok = _kv_retention_update(lambda d: d.__setitem__(
        hint, {"expires": expires, "ttl_days": days or KV_TTL_SECONDS / 86400}))
    if not ok and KV_TTL_SECONDS <= 0:
        # 兜底也关着时账本失败=该份将无限期保留（fail-open 的注明例外，首评 S）
        print(f"[Server] ⚠️ kv 留存账本写不动且兜底 TTL=0：{hint} 将无限期保留，"
              f"请人工清理或修盘")


def _sweep_kvstore() -> int:
    """到期清扫：逐份到期（执行端定）优先，没记账的按服务端兜底 TTL（0=不兜）。
    返回清掉的文件数。KV 是会话派生物，删了解冻走冷路（慢 3-13s），不丢数据。"""
    now = time.time()
    n = 0
    try:
        entries = list(KV_DIR.glob("*.safetensors"))
    except OSError:
        return 0
    retention = _kv_retention_load()
    stale_hints = []
    for p in entries:
        hint = p.name[:-len(".safetensors")]
        ent = retention.get(hint)
        if isinstance(ent, dict) and "expires" in ent:
            try:
                expired = now > float(ent["expires"])
            except (TypeError, ValueError):
                expired = True   # 坏账目按到期处理（fail-closed 向隐私侧）
        elif KV_TTL_SECONDS > 0:
            try:
                expired = p.stat().st_mtime < now - KV_TTL_SECONDS
            except OSError:
                continue
        else:
            expired = False
        if expired:
            try:
                p.unlink()
                n += 1
                stale_hints.append(hint)
            except OSError:
                continue
    if stale_hints or retention:
        def _prune(d: dict) -> None:
            for h in stale_hints:
                d.pop(h, None)
            # 死账在锁内按**当前**账本重算（首评 nit：快照期间刚重落盘的份不许被销账）
            for h in [k for k in list(d) if not (KV_DIR / f"{k}.safetensors").exists()]:
                d.pop(h, None)
        _kv_retention_update(_prune)
    return n


@app.post("/v1/cache/{cache_id}/persist", dependencies=[Depends(verify_api_key)])
async def cache_persist(cache_id: str, request: Request):
    """KV 落盘（Q9-a）：把注册表里的缓存序列化到服务端本地盘。开关关闭时后端
    返回 False（{persisted: false}，边端按冷路处理）。序列化在推理执行器线程内做
    且持生成锁——与 clone 同一纪律（张量归属线程 + 不落写到一半的缓存）。"""
    if KV_STORE_DISABLED:
        return {"persisted": False}   # 机主整面拒收：边端按冷路语义处理（既有契约）
    body = await request.json()
    hint = str(body.get("hint", ""))
    path = _kv_path(hint)
    handle = S.registry.get(cache_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="缓存不存在或已被 TTL 回收")
    loop = asyncio.get_running_loop()
    async with S.gen_sem:
        ok = await _busy_executor_call(
            loop, lambda: S.backend.persist_cache(handle, str(path)))
    if ok:
        # 逐份留存（D 方案）：留存期由执行端随请求携带，本端记到期账。
        # 账本锁可能被 to_thread 的清扫短暂持有——记账也下线程，不占事件循环
        await asyncio.to_thread(_kv_note_retention, hint, body.get("ttl_days"))
    return {"persisted": bool(ok)}


@app.post("/v1/cache/restore", dependencies=[Depends(verify_api_key)])
async def cache_restore(request: Request):
    """KV 热恢复：盘上产物 → 新缓存句柄入注册表。无产物/开关关/校验不过 → 404（边端退基座/冷路）。"""
    if KV_STORE_DISABLED:
        raise HTTPException(status_code=404, detail="本机已拒收 KV 落盘（INFER_KV_STORE_DISABLE）")
    body = await request.json()
    hint = str(body.get("hint", ""))
    path = _kv_path(hint)
    loop = asyncio.get_running_loop()
    async with S.gen_sem:
        handle = await _busy_executor_call(
            loop, lambda: S.backend.restore_cache(str(path)))
    if handle is None:
        raise HTTPException(status_code=404, detail="无可恢复的 KV 产物")
    await asyncio.to_thread(_kv_note_retention, hint, body.get("ttl_days"))   # 用到即续期
    return {"cache_id": S.registry.adopt(handle)}


@app.delete("/v1/cache/persisted/{hint}", dependencies=[Depends(verify_api_key)])
async def cache_persisted_drop(hint: str):
    """会话删除时的服务端清理（边端 best-effort 调用；文件本可随时清空，失败无害）。"""
    path = _kv_path(hint)
    existed = path.exists()
    path.unlink(missing_ok=True)
    _kv_retention_update(lambda d: d.pop(hint, None))
    return {"dropped": existed}


@app.delete("/v1/kvstore", dependencies=[Depends(verify_api_key)])
async def kvstore_clear():
    """立即清空全部落盘 KV（kv_persist D 方案：app 设置页「立即清除」按钮的落点；
    关掉开关后清历史残留正是主用例）。KV 是派生物：删了解冻走冷路，不丢会话数据。
    磁盘遍历下线程（首评 S）：几百份 × 百 MB 的 unlink 不许挂在事件循环上噎住 SSE。"""
    def _clear():
        deleted = 0
        freed = 0
        try:
            entries = list(KV_DIR.glob("*.safetensors"))
        except OSError:
            entries = []
        for p in entries:
            try:
                size = p.stat().st_size
                p.unlink()
                deleted += 1
                freed += size   # 删成了才计（首评 nit：失败的删除不算释放）
            except OSError:
                continue
        _kv_retention_update(lambda d: d.clear())
        return deleted, freed
    deleted, freed = await asyncio.to_thread(_clear)
    print(f"🧹 [Server] kvstore 手动清空：{deleted} 份 / {freed / 2**20:.1f} MB")
    return {"deleted": deleted, "bytes": freed}


@app.post("/v1/transcribe", dependencies=[Depends(verify_api_key)])
async def transcribe(request: Request):
    """音频转写（R5-2/D.14）：edge 不再自带 whisper，整条音频路在设备 A。

    音频经 /v1/media 先行上传（内容寻址、天然去重），此处只收 media_id——与生成路
    对媒体的处理同一套，不另开上传通道。

    **跑在 S.executor（单线程）上**：与生成共用同一个推理 worker，故转写与生成在 A 上
    天然串行、不抢统一内存带宽——正是 CHARTER §3「单引擎批内并发」保留的单实例
    物理内核要的形状（2026-08-23 修订自「单 worker 串行」，转写侧语义不变），
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
    _touch_media(p)   # 转写也是使用：TTL 时钟拨回

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
        # 转写是真 GPU 负载（whisper 装载+前向）：必须对批泳道可见（评审 M7），
        # 否则批 decode 与转写同时压统一内存 = 单 GPU 纪律要防的形态
        text = await _busy_executor_call(
            loop, lambda: fn(str(p), model=model, language=language,
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
    else:
        _touch_media(dest)   # 去重命中也算一次使用：TTL 时钟拨回
    return {"media_id": media_id, "bytes": len(data)}


@app.head("/v1/media/{media_id}", dependencies=[Depends(verify_api_key)])
async def media_exists(media_id: str):
    """客户端缓存鲜度探活（缺陷三）：上传缓存过期时先问在不在，404 才重传整份字节。
    命中即 touch——客户端马上要用它，TTL 时钟拨回。"""
    if not _MEDIA_ID_RE.match(media_id):
        raise HTTPException(status_code=400, detail="非法 media_id")
    p = MEDIA_DIR / media_id
    if not p.is_file():
        raise HTTPException(status_code=404, detail="媒体不存在（请重传）")
    _touch_media(p)
    return {}


@app.post("/v1/generate/{gen_id}/cancel", dependencies=[Depends(verify_api_key)])
async def generate_cancel(gen_id: str):
    g = S.gens.get(gen_id)
    if g is None:
        return {"cancelled": False}  # 已结束/不存在：幂等返回
    _cancel_gen(g)
    lane_req = g.get("lane_req")
    if lane_req and S.batch_lane is not None:
        S.batch_lane.cancel(lane_req)   # 批泳道行：驱动线程在步边界 remove
    S.metrics["cancelled_total"] += 1
    return {"cancelled": True}


def _batch_generate_sse(lane, prompt: str, sampling, want_delta: bool
                        ) -> StreamingResponse:
    """批处理泳道的 SSE 面：与单流路同一事件形状（meta/chunk/done/error）。

    submit 与 gens 登记都在生成器**体内**（评审 M14）：响应体若从未被迭代
    （中间件炸/客户端已断），什么都不曾发生——不留孤儿行、不漏 gens 表项。
    泳道输出是线程队列（驱动线程投递）；15s 无事件发一行 SSE 注释保活
    （让路窗口内泳道零产出，中间层的空闲超时会掐连接，评审 m20）。
    断连/取消统一收敛到 lane.cancel（幂等；驱动线程在步边界执行 remove）。"""

    async def _sse():
        req_id, gen_id = None, None
        try:
            req_id, out_q = lane.submit(prompt, sampling, want_delta=want_delta)
            gen_id = uuid.uuid4().hex
            S.gens[gen_id] = {"cancel": False, "lane_req": req_id}
            S.metrics["generations_total"] += 1
            yield ("event: meta\ndata: "
                   + json.dumps({"gen_id": gen_id, "cache_id": None,
                                 "lane": "batch"}) + "\n\n")
            while True:
                try:
                    kind, payload = await asyncio.to_thread(out_q.get, True, 15.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if kind == "chunk":
                    yield ("event: chunk\ndata: "
                           + json.dumps(payload, ensure_ascii=False) + "\n\n")
                    continue
                if kind == "error":
                    S.metrics["errors_total"] += 1
                    yield ("event: error\ndata: "
                           + json.dumps(payload, ensure_ascii=False) + "\n\n")
                else:
                    yield ("event: done\ndata: "
                           + json.dumps(payload, ensure_ascii=False) + "\n\n")
                break
        except RuntimeError as e:
            # submit 撞上泳道下线/背压：以 error 事件收尾（客户端会重试或退避；
            # 路由层的"回单流路"只覆盖流开始前，进了流就只能响亮结束）
            yield ("event: error\ndata: "
                   + json.dumps({"message": str(e)}, ensure_ascii=False) + "\n\n")
        finally:
            # 断连/正常收尾统一：向泳道投取消意图（已结束的行是无害幂等空操作）
            if req_id is not None:
                lane.cancel(req_id)
            if gen_id is not None:
                S.gens.pop(gen_id, None)

    return StreamingResponse(_sse(), media_type="text/event-stream")


@app.post("/v1/generate", dependencies=[Depends(verify_api_key)])
async def generate(request: Request):
    body = await request.json()
    prompt = str(body.get("prompt", ""))
    sampling = body.get("sampling") or {}
    features = dict(body.get("features") or {})

    # §7-0① 批处理泳道路由：显式 opt-in（features.batch_ok）且无单流路专属特性
    # 才进泳道；其余一切走既有单流路（含 batch_ok 但判不合格的——fail-closed，
    # 理由进日志，绝不静默把 guard/缓存语义丢进批路）。
    if features.get("batch_ok"):
        lane = S.batch_lane
        # 投机在场即拒（drafter **或** prompt-lookup——批路静默退非投机 = 口径
        # 分母污染）；其余单流路专属特性见 route_reason（fail-closed）
        _spec_on = (getattr(S.backend, "draft_model", None) is not None
                    or getattr(S.backend, "speculative", None) is not None)
        reason = _bl.route_reason(
            body, armed=(lane is not None and not lane.dead),
            has_speculative=_spec_on)
        if not reason:
            return _batch_generate_sse(lane, prompt, sampling,
                                       bool(features.get("want_delta")))
        print(f"[Server] batch_ok 请求回单流路：{reason}")

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

    # 约束面装配推迟到推理线程（_blocking_generate 内、gen_sem 已持有）：见 _tool_guard
    # 文档——在这里（事件循环线程、闸外）做会与在飞生成共用并 reset 同一 matcher。
    tool_specs = features.pop("tool_specs", None)

    images = _media_paths(body.get("images"), "image")
    audios = _media_paths(body.get("audios"), "audio")

    gen_id = uuid.uuid4().hex
    scope = CancelScope()
    gen_state = {"cancel": False, "scope": scope}
    # 无条件覆盖：客户端 JSON 里同名键（标量）绝不能冒充进程内取消入口
    features["cancel_scope"] = scope
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()

    def _blocking_generate():
        """推理执行器线程：消费后端生成流，逐 chunk 投回事件循环。"""
        guard = None
        stream = None
        try:
            # 约束面在此装配：已在 gen_sem 内，与一切在飞生成互斥（评审 M4）；
            # 构建异常（含 lifespan 未预建的结构错）走下方 except → SSE error 事件
            guard = _arm_tool_guard(features, tool_specs)
            if gen_state["cancel"]:
                return ("cancelled", {})      # 排队/装配期间已断连：不起后端
            stream = S.backend.stream_generate(
                prompt, cache=cache,
                images=images or None, audios=audios or None,
                sampling=sampling, features=features,
            )
            for chunk in stream:
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
            if gen_state["cancel"]:
                return ("cancelled", {})      # 钩子中止后后端可能安静收尾：不冒充完成
            done_payload = {}
            if guard is not None and guard.masked_positions:
                done_payload["constrained_steps"] = guard.masked_positions
            return ("done", done_payload)
        except Exception as e:
            if gen_state["cancel"]:
                _note_cancel_exc(e)
                return ("cancelled", {})      # 中止钩子让后端以异常结束：属取消，不计 error
            return ("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            if stream is not None:
                _close_stream(stream)

    async def _sse():
        # 生成互斥：底层共享单例不可并发。acquire 被取消（排队中客户端断连）时
        # 锁未持有、finally 不会走——语义正确
        await S.gen_sem.acquire()
        S.single_busy += 1              # 批泳道据此让路（配对递减在 _finish）
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
            # 断连/取消/完成统一收尾：置取消标志（生成线程在下一 chunk 边界停）并触发
            # 后端中止钩子（预填期也立即生效，见 _cancel_gen；完成路径上钩子已注销，空操作）。
            # 此处**绝不能 await**：客户端断连时本生成器经任务取消收场，粘性取消会让
            # finally 里的任何挂起点立即再抛 CancelledError、跳过其后的清理——放锁改为
            # 生成线程退出时的 done-callback（跑在事件循环线程），既保证"锁的释放严格
            # 晚于生成线程退出"（单例不并发的不变量），又对取消免疫（实测断连泄锁定此案）。
            _cancel_gen(gen_state)

            def _finish(_fut=None):
                S.gens.pop(gen_id, None)
                S.single_busy = max(0, S.single_busy - 1)
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
    返回 (prompt, 尾块快照或 None, 增量偏移或 None)——偏移供后端走 token 级
    增量喂入（历史段取缓存记录、永不重分词）。门面无多模态入口，恒可下发。"""
    ps = S.facade_stream
    delta_fn = getattr(S.dialect, "build_prompt_delta", None)
    if delta_fn is not None and pstream.stream_valid(ps, msgs, None):
        out = delta_fn(msgs[ps.seen + 1:], prev_tail=ps.tail)
        if out is not None:
            delta, tail = out
            return ps.stream + delta, tail, len(ps.stream)
    prompt = S.dialect.build_prompt(messages=msgs, tools=None, include_env=False)
    tail_fn = getattr(S.dialect, "tail_state", None)
    return prompt, (tail_fn() if tail_fn is not None else None), None


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

        def _run(prompt, delta_off):
            last, parts, raw = None, [], []
            for ch in S.backend.stream_generate(
                    prompt, cache=S.facade_cache, sampling=sampling,
                    features={"prompt_delta_offset": delta_off}):
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
            # 组装-生成-提交整组进临界区（评审 M6，_facade_commit docstring）；
            # single_busy 由 _busy_executor_call 托管——协程在 await 被取消时
            # 线程还在生成，递减必须挂在线程真正结束上
            prompt, stream_tail, delta_off = _facade_assemble(dialect_msgs)
            if delta_off is not None:
                S.metrics["facade_stream_delta_total"] = \
                    S.metrics.get("facade_stream_delta_total", 0) + 1
            text, last, raw_text = await _busy_executor_call(
                loop, lambda: _run(prompt, delta_off))
            _facade_commit(prompt, raw_text, dialect_msgs, stream_tail, text)
        pt = last.prompt_tokens if last else 0
        gt = last.generation_tokens if last else 0
        return oai.completion_response(
            S.model_name, cid, text, pt, gt,
            oai.finish_reason_of(gt, sampling["max_tokens"]))

    # 流式：复用 /v1/generate 的纪律（有界队列背压 + 绝不在 finally await + done-callback 放锁）
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    stop = {"flag": False}
    scope = CancelScope()          # 断连止损同 /v1/generate（见 _cancel_gen）
    filt = _facade_streamer()

    asm: Dict[str, Any] = {}   # 组装结果（在 _sse 拿到 gen_sem 后才填——评审 M6）

    def _blocking():
        last, raw, vis = None, [], []
        stream = None
        try:
            if stop["flag"]:
                return last, "", "", False
            stream = S.backend.stream_generate(
                asm["prompt"], cache=S.facade_cache, sampling=sampling,
                features={"prompt_delta_offset": asm["delta_off"],
                          "cancel_scope": scope})
            for ch in stream:
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
            if stop["flag"]:
                return last, "", "", False    # 钩子中止后后端可能安静收尾：不提交
            return last, "".join(raw), "".join(vis), True
        except Exception as e:
            if not stop["flag"]:
                print(f"[Facade] 生成异常: {type(e).__name__}: {e}")
            else:
                _note_cancel_exc(e)
            return last, "", "", False
        finally:
            if stream is not None:
                _close_stream(stream)

    async def _sse():
        await S.gen_sem.acquire()
        S.single_busy += 1              # 批泳道让路（配对递减在 _finish）
        task = None
        try:
            yield oai.stream_line(S.model_name, cid, role="assistant")
            asm["prompt"], asm["tail"], asm["delta_off"] = \
                _facade_assemble(dialect_msgs)
            if asm["delta_off"] is not None:
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
            scope.cancel()             # 不 await；完成路径上钩子已注销，空操作

            def _finish(_f=None):
                S.single_busy = max(0, S.single_busy - 1)
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
