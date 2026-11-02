"""有效上下文拐点测量（W0）：longchain 题前注入可控长度的真实形态历史，看成功率随 prompt 规模的走势。

  backend/.venv/bin/python benchmarks/context_knee/run_knee.py --dry-run --levels 0,4000 --reps 2 --limit 2
  INFER_REMOTE_HOST=<算力端> INFER_API_KEY=… backend/.venv/bin/python benchmarks/context_knee/run_knee.py \\
      --model RedHatAI/Qwen3.8-27B-INT4 --backend remote --tasks hard

复用 backend/benchmarks/model_eval（run_item 执行 + longchain 打分，源码不改）。每个
(level × 题 × rep)：新建会话 → 首次生成前把填充历史铺进 system 与任务之间 → 照 run_item
跑题 → longchain 评分 + 精确回忆评分。产物：<out>/transcripts.jsonl（每 run 一行，
增量追加）+ summary.json；表格用 summarize.py。用法细节见 README.md。
--orchestrator（W-K）：改为每 run 新建 Orchestrator、填充铺好后直调 run_turn（W1 护栏 / W2 步骤边界
压缩 / 轮统计在路径上），见 README「编排器模式」与 orch_tools.py；不给该 flag 时行为同上。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, replace as dc_replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
for _p in (BACKEND, BACKEND / "benchmarks"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))       # 末位：只为 import context_knee

from benchmarks.model_eval.executor import DEFAULT_SYSTEM_PROMPT, run_item  # noqa: E402
from benchmarks.model_eval.itemlib import load_items, task_fingerprint      # noqa: E402
from benchmarks.model_eval.mock_envs import make_env                       # noqa: E402
from benchmarks.model_eval.scorers import get_scorer                       # noqa: E402
from context_knee.filler import Filler, build_filler, recall_suffix, score_recall  # noqa: E402
from context_knee import prefix as prefix_mod                               # noqa: E402
from context_knee.quality import final_text_quality                        # noqa: E402
from framework.core.thinking_replay import DEFAULT_K as DEFAULT_REPLAY_K           # noqa: E402
from framework.core.thinking_replay import DEFAULT_MODE as DEFAULT_REPLAY_MODE     # noqa: E402
from framework.core.thinking_replay import (THINKING_REPLAY_MODES, ThinkingReplayPolicy,  # noqa: E402
                                            project_thoughts, resolve_thinking_replay,
                                            turn_start_index)

ITEMS_DIR = BACKEND / "benchmarks" / "model_eval" / "items" / "longchain"
FAKE_CHARS_PER_TOKEN = 3.5
#: 0 = 自然基线（无填充、无回忆句）；4096 = 带回忆句的最小填充基线（与高档同口径）；
#: 57344 顶档给远端 max_model_len 65536 留生成余量
DEFAULT_LEVELS = "0,4096,8192,16384,32768,49152,57344"


# ---------------- 采样覆盖 ----------------

#: CUDA 后端映射进 vLLM SamplingParams 的键（cuda/backend.py:489-500；top_p/top_k/presence/
#: frequency 为 2026-10-31 加入的可选透传，远端代码须为该版本）；其余键远端不认
SAMPLING_KEYS = {"temperature": float, "min_p": float, "max_tokens": int,
                 "repetition_penalty": float, "top_p": float, "top_k": int,
                 "presence_penalty": float, "frequency_penalty": float}
#: 同时是 generate_once 形参的键：在引擎入口覆盖（engine 的截断判定等用的是同一个值）
_GEN_KWARGS = ("temperature", "min_p", "max_tokens")


def parse_sampling(spec: str) -> Dict[str, Any]:
    """`k=v[,k=v...]` → {"sent": {...}, "ignored": {...}}。sent = 本端已发送给后端的键值——
    远端是否真的用上没有回执核验（服务端不回显 SamplingParams）；非映射键只警告、记录、不发送。"""
    applied: Dict[str, Any] = {}
    ignored: Dict[str, str] = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise SystemExit(f"❌ --sampling 项须为 k=v：{part!r}")
        k, v = (x.strip() for x in part.split("=", 1))
        if k in SAMPLING_KEYS:
            try:
                applied[k] = SAMPLING_KEYS[k](v)
            except ValueError:
                raise SystemExit(f"❌ --sampling {k}={v!r} 不是数值")
        else:
            print(f"⚠️ --sampling {k}={v}：远端未映射，忽略")
            ignored[k] = v
    return {"sent": applied, "ignored": ignored}


def install_sampling_override(backend, overrides: Dict[str, Any]) -> bool:
    """在后端实例上包一层 stream_generate（RemoteBackend.stream_generate 收 `sampling`
    字典，remote/backend.py:336-341）：把覆盖值 merge 进 sampling（覆盖优先）再转发。
    引擎按注册表条目 sampling 解析采样（W-1），这里的 --sampling 覆盖层仍优先。幂等。"""
    if not overrides or getattr(backend, "_knee_sampling", None) is not None:
        return False
    orig = backend.stream_generate

    def stream_generate(prompt, *args, sampling=None, **kw):
        return orig(prompt, *args, sampling={**(sampling or {}), **overrides}, **kw)
    backend.stream_generate = stream_generate
    backend._knee_sampling = dict(overrides)
    return True


# ---------------- 计量 ----------------

class _DummyTok:
    """fake 模式的方言装配替身（方言只在构造期碰 tokenizer；计数走 chars/3.5）。"""
    special_tokens_map: Dict[str, Any] = {}

    def encode(self, s, add_special_tokens=False):
        return [1]


class _DummyProc:
    tokenizer = _DummyTok()


@dataclass
class PromptCounter:
    """按引擎同款组装渲染整段 prompt 再计 token。

    组装参数照 engine.generate_once 首次生成（全量路）：task_block/tail_notes 为空，
    include_env = ChatSession.inject_env_context（缺省 True），env_extra = 引擎的笔记索引。
    replay = 引擎的思考回放策略（W3 / D36）：渲染前做与引擎同一投影（轮起点取「末条 user 之后」，
    即首次生成时编排层置的 turn_history_start_idx）——填充里题面之前的思考属历史轮、按策略丢弃，
    不投影则计数与实发 prompt 不等。None = 不投影（全部回放）。
    """
    dialect: Any
    encode_len: Callable[[str], int]
    env_extra: Optional[List[str]] = None
    mode: str = "engine"            # engine = 引擎分词器；chars/3.5 = fake 估算
    replay: Optional[ThinkingReplayPolicy] = None

    def render(self, messages: List[Dict[str, Any]], tools, enable_thinking: bool,
               turn_start: Optional[int] = None) -> str:
        """turn_start：显式本轮起点（编排器模式 turn 放置 = 题面 user 之后一条，同
        session.turn_history_start_idx）；None = 「末条 user 之后」（直调模式口径）。"""
        if self.replay is not None:
            anchor = (SimpleNamespace(turn_history_start_idx=turn_start)
                      if turn_start is not None else None)
            messages, _stats = project_thoughts(messages, turn_start_index(anchor, messages),
                                                self.replay)
        return self.dialect.build_prompt(
            messages=messages, tools=tools, enable_thinking=enable_thinking,
            task_block=None, tail_notes=None, include_env=True, env_extra=self.env_extra)

    def count(self, messages: List[Dict[str, Any]], tools, enable_thinking: bool,
              turn_start: Optional[int] = None) -> int:
        return self.encode_len(self.render(messages, tools, enable_thinking, turn_start))


def engine_counter(engine) -> PromptCounter:
    tok = engine.processor.tokenizer
    notes = getattr(engine, "_notes_index", None)
    return PromptCounter(dialect=engine.dialect,
                         encode_len=lambda s: len(tok.encode(s, add_special_tokens=False)),
                         env_extra=notes() if callable(notes) else None, mode="engine",
                         replay=getattr(engine, "thinking_replay", None))


def resolve_replay_override(args, opts: Dict[str, Any]) -> Optional[ThinkingReplayPolicy]:
    """--thinking-replay / --thinking-replay-k（W3）→ 本次运行的思考回放策略；两个都不给 = None
    （沿用注册表条目策略，行为不变）。只给一个时另一个取条目值，合并后走与引擎同一个
    resolve_thinking_replay（非法值同样 ValueError）。"""
    mode, k = getattr(args, "thinking_replay", None), getattr(args, "thinking_replay_k", None)
    if mode is None and k is None:
        return None
    return resolve_thinking_replay({
        "thinking_replay": mode if mode is not None else opts.get("thinking_replay"),
        "thinking_replay_k": k if k is not None else opts.get("thinking_replay_k")})


def fake_counter(model_options: Optional[Dict[str, Any]] = None) -> PromptCounter:
    from framework.dialects import get_dialect_class
    opts = dict(model_options or {})
    dialect = get_dialect_class(str(opts.get("dialect") or "")).create(
        _DummyProc(), options=opts, env={})
    return PromptCounter(dialect=dialect,
                         encode_len=lambda s: math.ceil(len(s) / FAKE_CHARS_PER_TOKEN),
                         mode=f"chars/{FAKE_CHARS_PER_TOKEN}",
                         replay=resolve_thinking_replay(opts))


# ---------------- 计划 ----------------

@dataclass
class RunPlan:
    item: Dict[str, Any]            # 本 run 实际跑的题（task.user 已追加回忆要求）
    level: int
    rep: int
    filler: Filler
    base_tokens: int                # 无填充时首次生成的 prompt token（同一追加后终问）
    prompt_tokens_est: int
    tools: List[Dict[str, Any]]     # prompt 面声明（mock 档 = 题目 mock 工具）
    min_fill_tokens: int = 0        # 最少 3 个伪轮次的代价（目标再小也得这么多）
    warning: Optional[str] = None
    # --prefix 档位（W-2）；mock 档下三者为 None，run_item / 引擎走 W-2 前原路径
    prefix_mode: str = "mock"
    system_prompt: Optional[str] = None             # run_item(system_prompt=)
    grammar_tools: Optional[List[Dict[str, Any]]] = None
    mock_tools: Optional[List[Dict[str, Any]]] = None
    # W-K turn 放置：level − 整块估算（逐对代价和与整块渲染不可加，凑数后按整块补对）；直调 None
    fill_est_delta: Optional[int] = None


def item_tools(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    task = item["task"]
    env = make_env(task["env"], task.get("env_init")) if task.get("env") else None
    return (env.tools() if env is not None else None) or task.get("tools") or []


def item_prefix(item: Dict[str, Any], mode: str = "mock",
                toolset: Optional["prefix_mod.ProdToolset"] = None) -> "prefix_mod.PrefixAssembly":
    """一道题在 --prefix 某档下的装配。routed 档的路由查询 = 原题面 task.user（不含回忆句，
    跨 level / rep 不变 → 每题一份、题内冻结）。"""
    return prefix_mod.assemble(mode, item_tools(item), item["task"].get("user", ""), toolset)


def plan_run(counter: PromptCounter, item: Dict[str, Any], level: int, rep: int,
             seed: int, item_idx: int = 0,
             prefix: Optional["prefix_mod.PrefixAssembly"] = None, *,
             fill_after_user: bool = False) -> RunPlan:
    """fill_after_user（编排器模式 turn 放置，W-K）：填充取轮内形态 in_turn（只留 assistant 调用 +
    tool 观察配对，无伪 user、无纯文本收尾，末条是 tool），逐轮代价与整段估算都按 [system, user, *填充]、
    本轮起点 = 2 计，按 level 目标重新凑数（每轮更小 → 多取几轮）。伪轮次流与卡值同源。
    缺省 False = 直调模式原口径（完整伪轮次铺在 user 之前）。"""
    run_item_obj = copy.deepcopy(item)
    task = run_item_obj["task"]
    if level > 0:
        task["user"] = task["user"] + recall_suffix(item.get("lang", "en"))
    asm = prefix if prefix is not None else item_prefix(item)
    tools = asm.tools
    et = bool((item.get("gen") or {}).get("enable_thinking", True))
    system = {"role": "system",
              "content": task.get("system") or asm.system_prompt or DEFAULT_SYSTEM_PROMPT}
    user = {"role": "user", "content": task["user"]}
    base = counter.count([system, user], tools, et)

    def turn_cost(msgs: List[Dict[str, Any]]) -> int:
        if fill_after_user:
            # 轮内配对的代价按「长填充中段」的渲染计：last_k 下本轮只回放最近 K 次生成的思考，
            # 绝大多数配对的思考被投影丢掉——逐对单独量（思考保留）会让和式高估、凑数偏少
            if counter.replay is not None and counter.replay.mode == "last_k":
                msgs = [{k: v for k, v in m.items() if k != "thoughts"}
                        if m.get("role") == "assistant" else m for m in msgs]
            return counter.count([system, user, *msgs], tools, et, turn_start=2) - base
        return counter.count([system, *msgs, user], tools, et) - base

    # 填充流按题目 mock 工具取名（不随 --prefix 变）：同一 (seed, rep, 题) 各档填充同源，
    # 高前缀档只是伪轮次更少（level 是 prompt 总量目标）
    def _build(target: int) -> Filler:
        return build_filler(target, seed=seed, rep=rep,
                            tool_names=[t["name"] for t in asm.mock_tools], turn_cost=turn_cost,
                            task_key=task_fingerprint(item), rotation=(item_idx + rep) % 3,
                            shape="in_turn" if fill_after_user else "turns")
    target = max(level - base, 1) if level > 0 else 0
    filler = _build(target)
    fill_est_delta = None
    if fill_after_user:
        # 逐对单独渲染各付一次包裹开销、和式与整块渲染不可加（实测整块系统性偏低）：凑数后量整块，
        # 缺口超过一对（平均）代价就把缺口加进目标重凑（目标增大 → 前缀超集），最多 8 次
        est = counter.count([system, user, *filler.messages], tools, et, turn_start=2)
        for _ in range(8):
            mean_cost = sum(filler.turn_costs) / max(len(filler.turn_costs), 1)
            if level <= 0 or level - est <= mean_cost:
                break
            target += level - est
            filler = _build(target)
            est = counter.count([system, user, *filler.messages], tools, et, turn_start=2)
        fill_est_delta = (level - est) if level > 0 else None
    else:
        est = counter.count([system, *filler.messages, user], tools, et)
    min_fill = sum(filler.turn_costs[:3])
    warning = None
    if level > 0 and level < base + min_fill:
        warning = (f"level {level} < 基础 prompt {base} + 最小填充 {min_fill}：实际 prompt "
                   f"≈{est}，本档对 {item['id']} 不达目标口径")
    return RunPlan(item=run_item_obj, level=level, rep=rep, filler=filler,
                   base_tokens=base, prompt_tokens_est=est, tools=tools,
                   min_fill_tokens=min_fill, warning=warning, prefix_mode=asm.mode,
                   system_prompt=asm.system_prompt, grammar_tools=asm.grammar_tools,
                   mock_tools=asm.mock_tools, fill_est_delta=fill_est_delta)


# ---------------- 引擎代理 ----------------

class KneeEngine:
    """包住真引擎（或 FakeEngine）的薄代理：
    ① 首次生成前把填充铺进 session.history（system 之后、题面消息之前）；
    ② 逐次记录 generate_once 的返回仪表与 repetition_guard（executor 不记这个字段）。
    ③ --prefix 非 mock 档：把 run_item 传来的 tools（题目 mock 工具）换成本档 prompt 面声明，
       并给 grammar_tools（mock 档 tools=None → 不改写，与 W-2 前一致）。
    其余行为原样透传——run_item 只用 generate_once / backend / dialect。"""

    def __init__(self, inner, prefill: List[Dict[str, Any]],
                 sampling_overrides: Optional[Dict[str, Any]] = None, *,
                 tools: Optional[List[Dict[str, Any]]] = None,
                 grammar_tools: Optional[List[Dict[str, Any]]] = None):
        self._inner = inner
        self._tools = tools
        self._grammar_tools = grammar_tools
        self._gen_overrides = {k: v for k, v in (sampling_overrides or {}).items()
                               if k in _GEN_KWARGS}
        self.backend = inner.backend
        self.dialect = getattr(inner, "dialect", None)
        self._prefill = prefill
        self._injected = False
        self.session = None             # run 结束时据此释放后端缓存
        self.gens: List[Dict[str, Any]] = []

    def _measure(self, session, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """生成前按同一份 prompt 入参调 engine.measure_prompt（W1 护栏量法），与该次生成实测
        prompt_tokens 并记，看量法偏差。内层无此方法（旧替身）= 两键 null；量法抛错只记账，
        不中止 run（仪表故障不得杀掉可测的 run）。"""
        fn = getattr(self._inner, "measure_prompt", None)
        if not callable(fn):
            return {"measure_tokens": None, "measure_method": None}
        try:
            pm = fn(session, tools=kwargs.get("tools"), task_block=kwargs.get("task_block"),
                    tail_notes=kwargs.get("tail_notes"),
                    enable_thinking=bool(kwargs.get("enable_thinking", False)))
            return {"measure_tokens": pm.tokens, "measure_method": pm.method}
        except Exception as e:      # noqa: BLE001
            return {"measure_tokens": None, "measure_method": None,
                    "measure_error": f"{type(e).__name__}: {e}"}

    @property
    def full_renders(self) -> Optional[int]:
        """本 run 会话的全量渲染次数（engine.generate_once 在 ChatSession.turn_full_renders 上累加；
        harness 不经编排层、无轮首归零——executor 每 run 新建会话，即整 run 口径）。"""
        v = getattr(self.session, "turn_full_renders", None)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def release(self) -> None:
        """释放本 run 会话的后端缓存（远端=服务端缓存条目，MLX=KV）；后端无此方法则跳过。"""
        rel = getattr(self.backend, "release_cache", None)
        handle = getattr(self.session, "brain_memory_state", None)
        if callable(rel) and handle is not None:
            rel(handle)
            self.session.brain_memory_state = None

    async def generate_once(self, session, **kwargs):
        self.session = session
        if not self._injected:
            hist = session.history
            if not hist or hist[0].get("role") != "system" or hist[-1].get("role") != "user":
                raise RuntimeError(f"首次生成前会话形态意外：{[m.get('role') for m in hist]}")
            hist[1:1] = copy.deepcopy(self._prefill)
            self._injected = True
        kwargs.update(self._gen_overrides)
        if self._tools is not None:
            kwargs["tools"] = self._tools
            kwargs["grammar_tools"] = self._grammar_tools
        measured = self._measure(session, kwargs)
        t0 = time.monotonic()
        r = await self._inner.generate_once(session, **kwargs)
        m = r.get("metrics") or {}
        self.gens.append({
            "i": len(self.gens),
            "wall_s": round(time.monotonic() - t0, 3),
            "prompt_tokens": m.get("prompt_tokens"),
            "reused_prefix": m.get("reused_prefix"),
            **measured,
            "prompt_tps": m.get("prompt_tps"),
            "generation_tps": m.get("generation_tps"),
            "generation_tokens": m.get("generation_tokens"),
            "cancelled": bool(r.get("cancelled")),
            "truncated": bool(r.get("truncated")),
            "repetition_guard": r.get("repetition_guard"),
        })
        return r


class OrchKneeEngine(KneeEngine):
    """编排器模式（--orchestrator）的引擎代理：Orchestrator 直接持有它。

    - 不注入填充（填充在 run_turn 之前由 harness 铺进 session.history：_context_guard_pre 的量测
      在 generate_once 之前，代理内注入会让首次量测看不到填充）；
    - 不改写 tools / grammar_tools（声明由编排器按注册表与披露模式自己组）；采样覆盖经 run_turn
      的 gen_kwargs 进编排层（max_tokens 先受 generation_reserve 钳制），代理不再覆盖；
    - 主会话由 harness 预置（self.session），generate_once **不改写它**：编排层的子生成（收尾目标
      校验的 EphemeralSession 等）也经代理，若像直调代理那样 `self.session = session`，release()
      会释放错缓存、full_renders 取错会话、信号处理器取消错对象。主会话的生成记 gens，其余记
      audit_gens（不量测、不进 gens）；
    - measure_prompt 只在内层有该方法时存在（__getattr__ 包一层），并记下最后一次量值：主会话生成
      记账的 measure_tokens 取护栏刚量过的那一份（量到的就是要发出去的），护栏没量（guard off /
      未绑上限）时代理自己量一次；
    - 其余属性（context_limit、tokenizer/processor、thinking_replay、prewarm_* 等）经 __getattr__
      透传内层引擎；gens 记账键与直调模式同口径。"""

    def __init__(self, inner):
        super().__init__(inner, [])
        self._injected = True             # 关闭直调模式的首次生成注入路径
        self._last_measure: Optional[Dict[str, Any]] = None
        self.audit_gens: List[Dict[str, Any]] = []

    def __getattr__(self, name):
        # 只在常规查找失败时调用；_inner 本身缺失（构造早期）不递归
        if name == "_inner":
            raise AttributeError(name)
        if name == "measure_prompt":
            fn = getattr(self._inner, "measure_prompt")      # 内层没有 → AttributeError 原样

            def measure_prompt(session, tools=None, task_block=None, tail_notes=None,
                               enable_thinking=False):
                pm = fn(session, tools=tools, task_block=task_block, tail_notes=tail_notes,
                        enable_thinking=enable_thinking)
                if session is self.session:
                    self._last_measure = {"measure_tokens": pm.tokens, "measure_method": pm.method,
                                          "measure_source": "guard"}
                return pm
            return measure_prompt
        return getattr(self._inner, name)

    def _measure(self, session, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        cached, self._last_measure = self._last_measure, None
        if cached is not None:
            return cached
        out = super()._measure(session, kwargs)
        out["measure_source"] = "proxy" if out.get("measure_method") else None
        return out

    async def generate_once(self, session, **kwargs):
        if self.session is None:
            self.session = session            # harness 未预置时首个会话即主会话
        main = session is self.session
        measured = self._measure(session, kwargs) if main else {}
        t0 = time.monotonic()
        r = await self._inner.generate_once(session, **kwargs)
        m = r.get("metrics") or {}
        sink = self.gens if main else self.audit_gens
        sink.append({
            "i": len(sink),
            "wall_s": round(time.monotonic() - t0, 3),
            "prompt_tokens": m.get("prompt_tokens"),
            "reused_prefix": m.get("reused_prefix"),
            **measured,
            "prompt_tps": m.get("prompt_tps"),
            "generation_tps": m.get("generation_tps"),
            "generation_tokens": m.get("generation_tokens"),
            "cancelled": bool(r.get("cancelled")),
            "truncated": bool(r.get("truncated")),
            "repetition_guard": r.get("repetition_guard"),
        })
        return r


def score_run(plan: RunPlan, traj: Dict[str, Any], gens: List[Dict[str, Any]],
              original_item: Dict[str, Any]) -> Dict[str, Any]:
    """longchain 原样打分（终态断言不变）+ recall_* + 复读护栏命中。"""
    score = get_scorer(original_item["dimension"])(traj, original_item)
    final_text = traj["turns"][-1]["text"] if traj.get("turns") else ""
    if plan.filler.cards:
        score.update(score_recall(final_text, plan.filler.cards))
    else:
        score.update({"recall_ok": None, "recall_hits": None, "recall_by_slot": None})
    score["repetition_hits"] = sum(1 for g in gens if g.get("repetition_guard"))
    return score


def _peak_prompt_measured(tm_peak: Any) -> Optional[int]:
    """编排器自己量到的峰值（`turn_metrics["peak_prompt_tokens"]`）校验为正整数后原样返回，
    否则 null。只作为 `peak_prompt_measured` 另记——它可能含 W1 护栏 fold 前的量值（量了
    58249、折到 29297 才发），不得混进 `build_record` 已算的实发峰值 `peak_prompt_tokens`。"""
    return tm_peak if isinstance(tm_peak, int) and not isinstance(tm_peak, bool) and tm_peak > 0 else None


def build_record(plan: RunPlan, traj: Dict[str, Any], gens: List[Dict[str, Any]],
                 score: Dict[str, Any], original_item: Dict[str, Any], meta: Dict[str, Any],
                 wall_s: float, full_renders: Optional[int] = None) -> Dict[str, Any]:
    first = gens[0] if gens else {}
    actual = first.get("prompt_tokens")
    # 峰值 prompt（W1）：本 run 各次生成实测 prompt_tokens 取大；无生成/全缺 = null
    _pts = [g["prompt_tokens"] for g in gens if g.get("prompt_tokens")]
    peak = max(_pts) if _pts else None
    fill = {
        "target_tokens": plan.level,
        "base_tokens": plan.base_tokens,
        "filler_tokens_est": plan.filler.est_tokens,
        "prompt_tokens_est": plan.prompt_tokens_est,
        "prompt_tokens_actual": actual,
        "prompt_tokens_measured": first.get("measure_tokens"),
        "fill_error": (actual - plan.level) if (plan.level > 0 and actual) else None,
        "est_error": (actual - plan.prompt_tokens_est) if actual else None,
        "min_fill_tokens": plan.min_fill_tokens,
        "warning": plan.warning,
        "n_pseudo_turns": len(plan.filler.turns),
        "n_messages": len(plan.filler.messages),
        "cards": plan.filler.cards,
        "fingerprint": plan.filler.fingerprint() if plan.filler.messages else None,
    }
    return {**meta, "level": plan.level, "rep": plan.rep, "item_id": original_item["id"],
            "card_kinds": {c["slot"]: c["kind"] for c in plan.filler.cards},
            "dimension": original_item["dimension"], "lang": original_item.get("lang"),
            "task_fp": task_fingerprint(original_item),
            "recall_suffix": plan.item["task"]["user"][len(original_item["task"]["user"]):],
            "fill": fill, "score": score, "gens": gens, "peak_prompt_tokens": peak,
            "full_renders": full_renders,
            "repetition_events": [{"gen": g["i"], **g["repetition_guard"]}
                                  for g in gens if g.get("repetition_guard")],
            "wall_s": round(wall_s, 2),
            "final_text_quality": final_text_quality(
                traj["turns"][-1]["text"] if traj.get("turns") else "", original_item.get("lang")),
            "turns": traj.get("turns"), "aborted_reason": traj.get("aborted_reason"),
            "env_state": traj.get("env_state")}


# ---------------- dry-run 脚本 ----------------

def dry_script(plan: RunPlan) -> List[Dict[str, Any]]:
    """FakeEngine 脚本：一次只读调用 → 收尾正文（逐字写出卡值；奇数 rep 故意漏 back 卡，
    让 recall 汇总在干跑里也有非平凡数）。仪表 prompt_tokens 取计划估算值。调用名按题目 mock
    工具取（生产工具只声明不执行）。"""
    names = {t["name"] for t in (plan.mock_tools if plan.mock_tools is not None else plan.tools)}
    call = ({"type": "function", "name": "list_files", "arguments": {}} if "list_files" in names
            else {"type": "function", "name": "todo_list", "arguments": {}})
    vals = [c["value"] for c in plan.filler.cards
            if not (plan.rep % 2 == 1 and c["slot"] == "back")]
    est = plan.prompt_tokens_est
    return [
        {"thoughts": "dry-run", "tool_calls": [call],
         "metrics": {"prompt_tokens": est, "generation_tokens": 24, "prompt_tps": 1000.0,
                     "generation_tps": 50.0}},
        {"text": "dry-run 收尾。" + (" 值：" + "、".join(vals) if vals else ""),
         "metrics": {"prompt_tokens": est + 200, "reused_prefix": est, "generation_tokens": 30,
                     "prompt_tps": 1000.0, "generation_tps": 50.0}},
    ]


# ---------------- 主流程 ----------------

def parse_levels(s: str) -> List[int]:
    vals = sorted({int(x) for x in s.split(",") if x.strip()})
    if not vals or vals[0] < 0:
        raise SystemExit(f"❌ --levels 须为非负整数列表：{s!r}")
    return vals


def select_items(args) -> List[Dict[str, Any]]:
    items = load_items([ITEMS_DIR / f"{args.tasks}.jsonl"])
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
        missing = wanted - {it["id"] for it in items}
        if missing:
            raise SystemExit(f"❌ 题库中不存在: {sorted(missing)}")
        items = [it for it in items if it["id"] in wanted]
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise SystemExit("❌ 过滤后题目为空")
    return items


async def make_engine(args, repo: str, opts: Dict[str, Any]):
    """真引擎装配。remote：只拉分词器（app.py:613 同款 resolve_tokenizer_only_path，不下权重）
    + 身份护栏（engine_pool.py 同款 expected_model）；mlx：run_eval 同款全量解析。"""
    from framework.core.engine import AgentEngine
    from framework.utils.config_loader import load_section
    inference_cfg = dict(load_section("inference"))
    if args.backend == "remote":
        host = str((inference_cfg.get("remote") or {}).get("host") or "").strip()
        if not host:
            raise SystemExit("❌ remote 需要算力端地址：设环境变量 INFER_REMOTE_HOST（config 里为空）")
        if not os.environ.get("INFER_API_KEY"):
            print("⚠️ 未设 INFER_API_KEY：算力端若要求鉴权会 401")
        from framework.utils.path_resolver import resolve_tokenizer_only_path
        model_path = resolve_tokenizer_only_path(repo, revision=opts.get("revision"))
        kv = {**inference_cfg,
              "expected_model": {"repo": repo, "revision": opts.get("revision")}}
    else:
        from framework.utils.path_resolver import resolve_model_path
        model_path = resolve_model_path(repo, revision=opts.get("revision"))
        kv = inference_cfg
    engine = await AgentEngine.create(model_path, kv_config=kv, model_options=opts, env={},
                                      backend=args.backend)
    # 上下文上限来源（meta.context_limit_source）：remote 由后端自己记（engine.backend.context_limit_source，
    # health = 算力端 /health 报的 max_model_len；config / default = 回落），见 context_limit_meta；
    # mlx 后端不记来源，这里按 config 键在不在判定
    if args.backend == "mlx":
        engine.knee_context_limit_source = (
            "config" if (inference_cfg.get("mlx") or {}).get("context_limit") is not None
            else "default")
    return engine


def context_limit_meta(engine) -> Dict[str, Any]:
    """meta：本批引擎的上下文上限与来源。真后端取 engine.context_limit（W1 护栏同一读法）；来源
    先读后端自记的 context_limit_source（remote），没有再取 make_engine 判定的（mlx），都没有 = null。
    fake（干跑）没有真上限：上限 null、来源 fake。"""
    if engine is None:
        return {"context_limit": None, "context_limit_source": "fake"}
    src = getattr(getattr(engine, "backend", None), "context_limit_source", None)
    if src is None:
        src = getattr(engine, "knee_context_limit_source", None)
    return {"context_limit": getattr(engine, "context_limit", None), "context_limit_source": src}


def _seed_sampler(backend: str, sampling_seed: int) -> bool:
    """进程内 MLX 采样走 mx.random 全局态：每 run 前播种。remote/cuda 的采样在服务端，
    generate_once 无 seed 参数——记 False（不可控），不假装播了。"""
    if backend != "mlx":
        return False
    import mlx.core as mx
    mx.random.seed(sampling_seed)
    return True


def _prefix_counter(engine, repo: str, opts: Dict[str, Any]):
    """prefix_base_tokens 用的 (方言, encode_len)：真后端 = 引擎分词器；fake = backend/models 下
    离线分词器（只载 tokenizer）。不可得 → (None, None, 原因)。"""
    if engine is not None:
        tok = engine.processor.tokenizer
        return engine.dialect, (lambda s: len(tok.encode(s, add_special_tokens=False))), None
    tok, enc, why = prefix_mod.local_tokenizer_encode_len(repo)
    if tok is None:
        return None, None, why
    from types import SimpleNamespace
    from framework.dialects import get_dialect_class
    o = dict(opts or {})
    dialect = get_dialect_class(str(o.get("dialect") or "")).create(
        SimpleNamespace(tokenizer=tok), options=o, env={})
    return dialect, enc, None


def _tool_counts(a: "prefix_mod.PrefixAssembly") -> Dict[str, int]:
    """prompt 面件数 / 其中完整 schema 件数（与语法面同一对象）/ 语法面件数。"""
    full_ids = {id(g) for g in a.grammar_tools or []}
    return {"prompt": len(a.tools), "prompt_full": sum(1 for t in a.tools if id(t) in full_ids),
            "grammar": len(a.grammar_tools or [])}


#: 按题的前缀 meta：全批字典只留在 meta / summary.json，transcript 行只放本题那一项
PER_ITEM_META_KEYS = ("prefix_shadowed", "prefix_core_skipped", "prefix_n_tools", "prefix_routed")


def row_meta(meta: Dict[str, Any], item_id: str) -> Dict[str, Any]:
    """transcript 行用的 meta：PER_ITEM_META_KEYS 的 {题: 值} 换成本题的值（行体积不随题数涨）。"""
    out = dict(meta)
    for k in PER_ITEM_META_KEYS:
        if isinstance(out.get(k), dict):
            out[k] = out[k].get(item_id)
    return out


def build_prefixes(mode: str, items: List[Dict[str, Any]], engine, repo: str,
                   opts: Dict[str, Any]):
    """每题一份前缀装配（题内冻结）+ meta 字段。prefix_base_tokens = 生产稳定前缀
    （system + 生产声明，未剔撞名、不含 mock 工具与路由件，include_env=False；mock 档 = 极简
    system 无工具），真分词器计数；分词器不可得记 None 并告警一次（mock + fake 不载分词器、不告警）。"""
    toolset = None if mode == "mock" else prefix_mod.production_toolset()
    prefixes = {it["id"]: item_prefix(it, mode, toolset) for it in items}
    meta: Dict[str, Any] = {"prefix_mode": mode, "prefix_base_tokens": None}
    if mode != "mock" or engine is not None:
        dialect, enc, why = _prefix_counter(engine, repo, opts)
        if dialect is None:
            print(f"⚠️ prefix_base_tokens 记 null：{why}")
        else:
            system = (prefix_mod.production_system_prompt() if mode != "mock"
                      else DEFAULT_SYSTEM_PROMPT)
            meta["prefix_base_tokens"] = prefix_mod.prefix_base_tokens(
                dialect, enc, system, prefix_mod.production_decl(mode, toolset))
    if mode != "mock":
        meta["prefix_top_k"] = toolset.top_k if mode == "routed" else None
        meta["prefix_shadowed"] = {i: a.shadowed for i, a in prefixes.items()}
        meta["prefix_core_skipped"] = {i: a.core_skipped for i, a in prefixes.items()}
        meta["prefix_n_tools"] = {i: _tool_counts(a) for i, a in prefixes.items()}
        if mode == "routed":
            meta["prefix_routed"] = {i: a.routed for i, a in prefixes.items()}
    return prefixes, meta


class _OrchGlobals:
    """编排器模式改动的两个进程级全局：护栏事件流落盘路径与审批策略持有者。install 在批次装配
    最早处（make_engine 之前：远端装配期的 context_limit_fallback 等事件也要落产物目录），
    restore 在 amain 的 finally——异常退出同样还原。"""

    def __init__(self) -> None:
        self.events_prev = None
        self.policy_prev = None
        self.installed = False

    def install(self, ctx: "OrchContext") -> None:
        from framework.core import approval_policy, events
        self.events_prev = events.get_path()
        # 私有持有者（无公开读取器；current() 首次会读 data_dir() 的 approval_policy.json）
        self.policy_prev = getattr(approval_policy, "_current", None)
        self.installed = True
        # 缺省落 data_dir()/guardrail_events.jsonl = backend/ 或用户数据根，绝不能写进去
        events.set_path(ctx.events_path)
        ctx.cursor = _orch_tools().EventCursor(ctx.events_path)
        # 审批进程内全放行（不落盘、不读数据根）
        approval_policy.replace(approval_policy.ApprovalPolicy(read="auto", write="auto"))

    def restore(self) -> None:
        if not self.installed:
            return
        from framework.core import approval_policy, events
        events.set_path(self.events_prev)
        if self.policy_prev is not None:
            approval_policy.replace(self.policy_prev)
        else:
            approval_policy._current = None     # 回到「未装载」：下次 current() 照旧惰性读
        self.installed = False


async def amain(args) -> int:
    g = _OrchGlobals()
    try:
        return await _amain(args, g)
    finally:
        g.restore()


async def _amain(args, orch_globals: "_OrchGlobals") -> int:
    from framework.utils.model_selection import model_options
    levels = parse_levels(args.levels)
    items = select_items(args)
    if args.dry_run:
        args.backend = "fake"
    if args.backend not in ("remote", "mlx", "fake"):
        raise SystemExit(f"❌ --backend 须为 remote|mlx|fake：{args.backend!r}")

    repo, opts = "dry-run/fake", {}
    if args.model:
        from benchmarks.model_eval.run_eval import resolve_repo
        repo = resolve_repo(args.model)
        opts = model_options(repo)
    elif args.backend != "fake":
        raise SystemExit("❌ 真后端须给 --model")

    sampling = parse_sampling(args.sampling)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.out) if args.out else HERE / "results" / Path(repo).name / stamp
    transcripts = outdir / "transcripts.jsonl"
    if args.resume:
        if not args.out:
            raise SystemExit("❌ --resume 须配 --out <原产物目录>")
        args.append = True
    done = load_done(transcripts) if args.resume else set()
    # 注册表条目的采样键（W-1 起引擎把它并入实际发送的 sampling）：入 meta、参与合并口径
    entry_sampling = dict(opts.get("sampling") or {})
    # W3 思考回放口径：CLI 覆盖 > 条目键 > 模块缺省；装载前解析（写错不白等装配），参与合并口径
    replay_override = resolve_replay_override(args, opts)
    replay = replay_override if replay_override is not None else resolve_thinking_replay(opts)
    replay_source = ("cli" if replay_override is not None else
                     "entry" if any(opts.get(k) is not None
                                    for k in ("thinking_replay", "thinking_replay_k")) else
                     "default")
    # W-K 编排器模式：开关 → 装配上下文（config.yaml agent 段只读）；直调模式 None
    orch_ctx = make_orch_context(args, outdir, opts) if getattr(args, "orchestrator", False) else None
    if orch_ctx is not None:
        # 生产工具效果分类的一次性枚举（密封构造会临时打断 requests.get、摘云端钥 env）：批次开头做完，
        # 不落在任何 run 里
        from context_knee import orch_stubs
        orch_stubs.production_traits()
    mode_meta = orch_meta(orch_ctx)
    if args.append:     # --resume 已隐含 --append
        check_resume_compat(transcripts, repo, sampling["sent"], args.seed, entry_sampling,
                            entry_only=not args.resume, prefix_mode=args.prefix,
                            thinking_replay=(replay.mode, replay.k),
                            harness_mode=harness_mode_of(mode_meta))
    if transcripts.exists() and not args.append:
        raise SystemExit(f"❌ {transcripts} 已存在：换 --out，或显式 --append 追加"
                         f"（或 --resume 续跑缺的 run）")
    outdir.mkdir(parents=True, exist_ok=True)
    if orch_ctx is not None:
        orch_globals.install(orch_ctx)

    if args.backend == "fake":
        engine = None
        counter = fake_counter(opts)
        if replay_override is not None:
            counter.replay = replay_override
    else:
        engine = await make_engine(args, repo, opts)
        try:
            install_sampling_override(engine.backend, sampling["sent"])
            if replay_override is not None:
                engine.thinking_replay = replay_override   # 计数器随后从引擎取，自然同步
            counter = engine_counter(engine)
        except BaseException:
            engine.backend.shutdown()
            raise
    try:
        prefixes, prefix_meta = build_prefixes(args.prefix, items, engine, repo, opts)
    except BaseException:
        if engine is not None:
            engine.backend.shutdown()
        raise
    meta = {"model": repo, "revision": opts.get("revision"),
            "dialect": opts.get("dialect") or "gemma4", "backend": args.backend,
            "tasks": args.tasks, "seed": args.seed, "token_counter": counter.mode,
            "stamp": stamp, "sampling_overrides": sampling, "entry_sampling": entry_sampling,
            # W3：实际生效的思考回放口径（计数器与引擎同一策略）；
            # source = cli（--thinking-replay*）| entry（条目显式键）| default（条目没写、模块缺省）
            "thinking_replay_mode": counter.replay.mode if counter.replay else None,
            "thinking_replay_k": counter.replay.k if counter.replay else None,
            "thinking_replay_source": replay_source,
            **context_limit_meta(engine),
            **prefix_meta,
            # W-K：harness 口径（mode = direct | orchestrator；直调模式其余键 null）
            **mode_meta}
    print(f"== 拐点测量 {repo}（{args.backend}，计数 {counter.mode}，前缀 {args.prefix}"
          f"{'' if prefix_meta['prefix_base_tokens'] is None else ' ' + str(prefix_meta['prefix_base_tokens']) + ' tok'}"
          f"）：{len(items)} 题 × {len(levels)} 档 {levels} × {args.reps} rep → {outdir}")

    records: List[Dict[str, Any]] = []
    if args.resume:
        print(f"== resume：跳过 {len(done)} 个已完成 run（error 行不算完成，重跑）")
    if orch_ctx is not None:
        print(f"== 编排器模式：放置 {orch_ctx.fill_placement}，guard {mode_meta['guard']}，"
              f"W2 {mode_meta['step_compaction']}，轮首压缩 {mode_meta['turn_compaction']}，"
              f"goal_verify {mode_meta['goal_verify']}，披露 {mode_meta['tool_schema_mode_effective']}"
              f"；事件 → {orch_ctx.events_path}")
    _INTERRUPT.reset()
    loop = asyncio.get_running_loop()
    installed = _install_interrupt(loop, asyncio.current_task())
    try:
        with transcripts.open("a", encoding="utf-8") as fh:
            for level in levels:
                for item_idx, item in enumerate(items):
                    for rep in range(args.reps):
                        if (level, item["id"], rep) in done:
                            continue
                        if orch_ctx is not None:
                            rec = await _one_run_orch(args, engine, counter, item, item_idx, level,
                                                      rep, meta, prefixes[item["id"]], orch_ctx)
                        else:
                            rec = await _one_run(args, engine, counter, item, item_idx, level, rep,
                                                 meta, prefixes[item["id"]])
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        fh.flush()
                        records.append(rec)
                        _print_run(rec)
    except asyncio.CancelledError:
        if _INTERRUPT.signum is None:
            raise
        asyncio.current_task().uncancel()   # 信号引起的取消到此为止：收尾里的 await 照常
    finally:
        # 收尾期（排空 / 写 summary / 同步的 backend.shutdown）不回默认处理器：默认下第二次
        # Ctrl-C 会以 KeyboardInterrupt 乱入收尾、第二次 SIGTERM 直接被杀（-15）。改成
        # 「立即 _hard_exit(128+signum)」，main 退出前才恢复默认（见 _restore_default_signals）
        _arm_hard_exit_signals(loop, installed)
        if _INTERRUPT.signum is not None and _INTERRUPT.inner is not None:
            await drain_inference(_INTERRUPT.inner, INTERRUPT_DRAIN_S)
        try:
            from context_knee.summarize import aggregate, dedupe, load_records
            if args.append and transcripts.exists():
                records = load_records(outdir)      # summary 按整个 transcripts 聚合
            uniq = dedupe(records)                  # 同 (level,题,rep) 取最新非 error
            summary = {**meta, "levels": levels, "reps": args.reps,
                       "items": [it["id"] for it in items], "n_rows": len(records),
                       "n_runs": len(uniq), "n_errors": sum(1 for r in uniq if "error" in r),
                       "complete": {(lv, it["id"], r) for lv in levels for it in items
                                    for r in range(args.reps)} <= load_done(transcripts),
                       "by_level": aggregate(uniq)}
            (outdir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"== 产物: {outdir}（表格：summarize.py {outdir}）")
        finally:
            if engine is not None:
                try:
                    engine.backend.shutdown()
                except Exception as e:
                    print(f"⚠️ 后端关闭异常: {e}")
    return _INTERRUPT.code if _INTERRUPT.signum is not None else 0


# ---------------- 可中断（W-1/C） ----------------

#: 收到信号后的收尾上限：超过即 os._exit（收尾本身卡住——如释放缓存的 HTTP——也要能退）。
#: 构成：proxy.release 的 DELETE 超时 5 s + 排空 INTERRUPT_DRAIN_S 2 s + 写 summary / 关后端
#: 的余量 ≈ 5 s。原 4 s 比单个 DELETE 超时还短，正常收尾也会被看门狗截断、summary 写不出。
INTERRUPT_GRACE_S = 12.0
#: 收尾里给推理线程的排空等待：解码中的线程见 cancel_requested 会在下一 chunk 停并
#: POST cancel；卡在 SSE 读（预填期无 chunk）的线程等不到，交给进程退出
INTERRUPT_DRAIN_S = 2.0


class _Interrupt:
    """SIGINT/SIGTERM 的进程级状态：当前 run 的代理与引擎（信号处理器据此置取消）。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.signum: Optional[int] = None
        self.armed: List[int] = []      # 收尾期挂了「立即退出」处理器的信号（main 退出前恢复默认）
        self.proxy = None
        self.inner = None

    @property
    def code(self) -> int:
        return 128 + int(self.signum or 0)


_INTERRUPT = _Interrupt()


def _hard_exit(code: int) -> None:
    """最后兜底的 os._exit：推理执行器线程是非守护线程，卡在远端 SSE 的 iter_lines 时
    （读超时 300 s）解释器退出要 join 它——进程就"SIGTERM 不死"。transcripts 每 run
    已 flush、summary 在收尾里写完，这里只冲一下标准流。进程退出由内核关掉 socket，
    服务端据断连走止损路径（inference/server.py _cancel_gen）。
    不从别的线程 resp.close()：响应对象是 RemoteBackend 生成器的局部变量，且 POSIX 上
    跨线程 close 一个正在 recv 的 socket 并不保证唤醒那次 recv（只有 shutdown 保证）。"""
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            pass
    os._exit(code)


def _install_interrupt(loop, main_task) -> List[int]:
    """首个信号：置当前会话 cancel_requested、起收尾看门狗、取消主任务（amain 收尾写
    summary、排空、关后端后返回 128+signum，main 再 _hard_exit）。第二个信号：立即退出。"""
    def _on_signal(signum: int) -> None:
        if _INTERRUPT.signum is not None:
            print(f"== 再次收到信号 {signum}：立即退出", flush=True)
            _hard_exit(128 + signum)
        _INTERRUPT.signum = signum
        print(f"== 收到信号 {signal.Signals(signum).name}：取消当前 run，写 summary 后退出"
              f"（{INTERRUPT_GRACE_S:.0f}s 内未收尾则强制退出）", flush=True)
        sess = getattr(_INTERRUPT.proxy, "session", None)
        if sess is not None:
            sess.cancel_requested = True
        dog = threading.Timer(INTERRUPT_GRACE_S, _hard_exit, args=(128 + signum,))
        dog.daemon = True
        dog.start()
        main_task.cancel()

    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass                     # 非主线程 / 平台不支持：维持默认行为
    return installed


def _exit_now_handler(signum: int, _frame) -> None:
    print(f"== 收尾中收到信号 {signal.Signals(signum).name}：立即退出", flush=True)
    _hard_exit(128 + signum)


def _arm_hard_exit_signals(loop, sigs: List[int]) -> None:
    """把 loop 级信号处理器换成进程级「立即 _hard_exit(128+signum)」处理器。

    用 signal.signal 而不是 loop.add_signal_handler：收尾里的 backend.shutdown() 是同步调用，
    卡在 HTTP 上时事件循环转不动，loop 级处理器永远轮不到；进程级处理器在主线程的系统调用
    被 EINTR 打断时即执行。loop.remove_signal_handler 会先把处理器回成默认，换手期间屏蔽
    这两个信号，期间到达的信号在解除屏蔽后交给新处理器，不会落到默认处理器上。"""
    if not sigs:
        return
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
    try:
        for sig in sigs:
            loop.remove_signal_handler(sig)
            signal.signal(sig, _exit_now_handler)
            _INTERRUPT.armed.append(sig)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def _restore_default_signals() -> None:
    """main 退出前：收尾期的「立即退出」处理器还原为默认（与 loop.remove_signal_handler 同口径）。"""
    for sig in _INTERRUPT.armed:
        if signal.getsignal(sig) is _exit_now_handler:
            signal.signal(sig, signal.default_int_handler if sig == signal.SIGINT
                          else signal.SIG_DFL)
    _INTERRUPT.armed = []


async def drain_inference(engine, timeout: float) -> Optional[bool]:
    """超时后排空推理执行器：往引擎的单线程执行器投一个空任务并等它跑到。被取消的生成
    还占着线程时（远端卡在 SSE 读），这一步等它退出；等不到就警告——后续 run 会排在它
    后面。返回 drain_timeout（True=没排空；None=引擎无执行器，跳过）。"""
    ex = getattr(engine, "_inference_executor", None)
    if ex is None:
        return None
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(ex, lambda: None), timeout=timeout)
        return False
    except asyncio.TimeoutError:
        print(f"⚠️ 推理线程仍占用（排空等待 {timeout:.0f}s 未果），后续 run 可能连环超时")
        return True


async def _one_run(args, engine, counter, item, item_idx, level, rep, meta,
                   prefix: Optional["prefix_mod.PrefixAssembly"] = None) -> Dict[str, Any]:
    """单 run；任何异常收成一条 {"error": …} 记录，不中止批次。prefix=None = mock 档。"""
    meta = row_meta(meta, item["id"])
    sampling_seed = args.seed * 1000 + rep
    proxy = None
    t0 = time.monotonic()
    try:
        plan = plan_run(counter, item, level, rep, args.seed, item_idx, prefix)
        if plan.warning:
            print(f"⚠️ {plan.warning}")
        applied = _seed_sampler(args.backend, sampling_seed)
        if args.backend == "fake":
            from tests.fake_engine import FakeEngine
            inner = FakeEngine(dry_script(plan))
        else:
            inner = engine
        mock = plan.prefix_mode == "mock"
        proxy = KneeEngine(inner, plan.filler.messages,
                           (meta.get("sampling_overrides") or {}).get("sent"),
                           tools=None if mock else plan.tools,
                           grammar_tools=None if mock else plan.grammar_tools)
        _INTERRUPT.proxy, _INTERRUPT.inner = proxy, inner
        timeout = getattr(args, "run_timeout", 0) or 0
        # mock 档调用形态与 W-2 前逐字相同（不传 system_prompt）
        coro = (run_item(proxy, plan.item, repeat_idx=rep) if mock else
                run_item(proxy, plan.item, repeat_idx=rep, system_prompt=plan.system_prompt))
        try:
            traj = await (asyncio.wait_for(coro, timeout) if timeout > 0 else coro)
        except asyncio.TimeoutError:
            # wait_for 已取消 run_item 协程；推理线程里的同步生成循环只认 cancel_requested
            # （engine.py:542-552：命中即 break → stream.close() → 远端 POST cancel）
            if proxy.session is not None:
                proxy.session.cancel_requested = True
            elapsed = round(time.monotonic() - t0, 2)
            print(f"⏱ L{level} {item['id']} r{rep} timeout（{elapsed}s > {timeout}s）")
            drain_timeout = await drain_inference(inner, min(60.0, timeout))
            try:
                proxy.release()
            except Exception as e:
                print(f"⚠️ 释放会话缓存失败: {e}")
            return {**meta, "level": level, "rep": rep, "item_id": item["id"],
                    "error": "timeout", "elapsed_s": elapsed, "run_timeout_s": timeout,
                    "drain_timeout": drain_timeout,
                    "drain_s": round(time.monotonic() - t0 - elapsed, 2),
                    "gens": proxy.gens, "full_renders": proxy.full_renders,
                    "wall_s": round(time.monotonic() - t0, 2)}
        score = score_run(plan, traj, proxy.gens, item)
        return build_record(plan, traj, proxy.gens, score, item,
                            {**meta, "sampling_seed": sampling_seed,
                             "sampling_seed_applied": applied}, time.monotonic() - t0,
                            full_renders=proxy.full_renders)
    except Exception as e:      # noqa: BLE001 —— 批次纪律：单 run 失败只记账
        import traceback
        return {**meta, "level": level, "rep": rep, "item_id": item["id"],
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-2000:],
                "gens": proxy.gens if proxy is not None else [],
                "wall_s": round(time.monotonic() - t0, 2)}
    finally:
        if _INTERRUPT.signum is None:
            _INTERRUPT.proxy = _INTERRUPT.inner = None   # 中断时留给 amain 收尾排空
        if proxy is not None:
            try:
                proxy.release()
            except Exception as e:
                print(f"⚠️ 释放会话缓存失败: {e}")


# ---------------- 编排器模式（W-K，--orchestrator） ----------------

#: 编排器模式专属 CLI 开关（直调模式给了即 argparse error）与缺省值
ORCH_FLAGS = {"fill_placement": "turn", "guard": "on", "step_compaction": "on",
              "turn_compaction": "on", "goal_verify": "off"}
#: 编排器模式支持的前缀档：披露（full / catalog + get_tool_schema + 任务期路由）由编排器自己做。
#: mock = 无生产工具桩；prod = 生产可用桩，披露按注册表条目 tool_schema_mode（> agent 段 > full）；
#: catalog = 注册表同 prod，披露强制 catalog（orch_model_options 把 tool_schema_mode 写进条目层，
#: 条目层优先）。routed 的「按题面手工选路由件」是直调模式的装配形态，编排器的路由在任务建立时
#: 自己做，故无对应物
ORCH_PREFIX_MODES = ("mock", "prod", "catalog")


def orch_model_options(opts: Optional[Dict[str, Any]], prefix: str) -> Dict[str, Any]:
    """编排器模式交给 Orchestrator 的条目选项：--prefix catalog 时条目层 tool_schema_mode 强制
    catalog（与 tool_disclosure.resolve_tool_schema_mode 的「条目 > agent 段」同一优先级）；其余原样。"""
    out = dict(opts or {})
    if prefix == "catalog":
        out["tool_schema_mode"] = "catalog"
    return out
#: meta 里的口径键（续跑校验比这一组）。guard_thresholds / step_compaction_cfg = 覆盖后的生效值
HARNESS_MODE_KEYS = ("mode", "fill_placement", "guard", "step_compaction", "turn_compaction",
                     "goal_verify", "fill_shape", "guard_thresholds", "step_compaction_cfg",
                     "tool_schema_mode_effective", "agent_cfg_fingerprint", "stub_impl")
#: 编排器模式生产工具桩的实现版本（W-K2）：W-K2 之前的编排器行回的是 Unknown tool，缺键 = 旧实现，
#: 续跑 / 追加与之不比「缺键不比」规则、恒比（见 harness_mismatch）
STUB_IMPL = "usable-v1"
#: 编排器行即便缺键也恒比的口径键
_ALWAYS_COMPARED = ("mode", "stub_impl")
#: 直调模式（及缺 mode 的旧行）的口径：只认 mode 与填充形态，其余不比
_DIRECT_HARNESS = {"mode": "direct", "fill_shape": "turns"}


def harness_mismatch(r: Dict[str, Any], new: Tuple[Any, ...]) -> bool:
    """一条已有行与本次口径是否不同。直调 / 缺 mode 的旧行按直调口径整组比；编排器行只比它
    **有的**键（后加的口径键旧行缺 = 不比），mode 恒比。"""
    old = harness_mode_of(r)
    if old[0] == "direct" or new[0] == "direct":
        return old != tuple(new)
    return any(o != n for k, o, n in zip(HARNESS_MODE_KEYS, old, new)
               if k in _ALWAYS_COMPARED or k in r)


def agent_cfg_fingerprint(cfg: Dict[str, Any]) -> str:
    """生效 agent 段（开关覆盖后、逐题 max_total_iterations 覆盖前）的指纹：json.dumps(sort_keys) 的
    sha256 前 16 位。config.yaml agent 段任何键变了，续跑即拒。"""
    import hashlib
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def harness_mode_of(r: Dict[str, Any]) -> Tuple[Any, ...]:
    """一行（或 meta）的 harness 口径元组；缺 mode 的旧行 / 直调行 = 直调口径（完整伪轮次填充，
    编排器开关与 cfg 键不比）。"""
    if not r.get("mode") or r.get("mode") == "direct":
        return tuple(_DIRECT_HARNESS.get(k) for k in HARNESS_MODE_KEYS)
    return tuple(r.get(k) for k in HARNESS_MODE_KEYS)


def effective_cfg_meta(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """覆盖后 agent 段的 W1 阈值与 W2 参数生效值（与 Orchestrator 构造期同一解析）。"""
    from framework.core import context_guard, step_compaction
    g = context_guard.resolve_context_guard(cfg)
    sc = step_compaction.resolve_step_compaction(cfg)
    return {"guard_thresholds": {"soft": g.soft, "hard": g.hard, "reserve": g.reserve},
            "step_compaction_cfg": {"trigger_level": sc.trigger_level,
                                    "cold_refill_trigger_level": sc.cold_refill_trigger_level,
                                    "keep_recent": sc.keep_recent, "digest_chars": sc.digest_chars}}


@dataclass
class OrchContext:
    """一批编排器模式 run 的共用装配：agent 段基线、开关、目录与事件游标。"""
    out_dir: Path
    agent_base: Dict[str, Any]
    model_options: Dict[str, Any]
    fill_placement: str = "turn"
    guard: bool = True
    step_compaction: bool = True
    turn_compaction: bool = True        # 已按放置生效（turn 放置恒 False）
    goal_verify: bool = False
    cursor: Any = None                  # orch_tools.EventCursor（指向 out_dir 的事件文件）

    @property
    def sessions_dir(self) -> Path:
        return self.out_dir / "sessions"

    @property
    def tools_dir(self) -> Path:
        return self.out_dir / "tools-empty"

    @property
    def events_path(self) -> Path:
        return self.out_dir / "guardrail_events.jsonl"


def orch_meta(ctx: Optional[OrchContext], args=None) -> Dict[str, Any]:
    """meta 的 harness 口径键。ctx=None = 直调模式（mode=direct、其余 null）。"""
    if ctx is None:
        return {"mode": "direct", "fill_placement": None, "guard": None, "step_compaction": None,
                "turn_compaction": None, "goal_verify": None, "fill_shape": "turns",
                "guard_thresholds": None, "step_compaction_cfg": None,
                "orchestrator_cfg_source": None, "tool_schema_mode_effective": None,
                "stub_impl": None}
    from framework.core import tool_disclosure
    cfg, over = _orch_tools().agent_cfg(ctx.agent_base, guard=ctx.guard, step=ctx.step_compaction,
                                        goal_verify=ctx.goal_verify)
    if "max_total_iterations" in ctx.agent_base:
        over["max_total_iterations"] = "per item: task.max_turns 或 executor 缺省（记入行 max_total_iterations）"
    onoff = lambda b: "on" if b else "off"      # noqa: E731
    return {"mode": "orchestrator", "fill_placement": ctx.fill_placement,
            "guard": onoff(ctx.guard), "step_compaction": onoff(ctx.step_compaction),
            "turn_compaction": onoff(ctx.turn_compaction), "goal_verify": onoff(ctx.goal_verify),
            # turn 放置 = 轮内形态（调用 + 观察配对）；history 放置 = 完整伪轮次
            "fill_shape": "in_turn" if ctx.fill_placement == "turn" else "turns",
            **effective_cfg_meta(cfg),
            "agent_cfg_fingerprint": agent_cfg_fingerprint(cfg),
            "orchestrator_cfg_source": {"section": "config.yaml:agent", "overrides": over},
            "tool_schema_mode_effective": tool_disclosure.resolve_tool_schema_mode(
                ctx.model_options, ctx.agent_base),
            "stub_impl": STUB_IMPL}


def _orch_tools():
    from context_knee import orch_tools
    return orch_tools


def make_orch_context(args, out_dir: Path, opts: Dict[str, Any]) -> OrchContext:
    """CLI 开关 → OrchContext（agent 段从 config.yaml 读，不改文件）。"""
    from framework.utils.config_loader import load_section
    placement = args.fill_placement or ORCH_FLAGS["fill_placement"]
    on = lambda k: (getattr(args, k) or ORCH_FLAGS[k]) == "on"   # noqa: E731
    turn_comp = on("turn_compaction")
    if placement == "turn" and turn_comp:
        if args.turn_compaction == "on":
            print("⚠️ --fill-placement turn 下轮首压缩强制 off（续跑语义：run_turn compact_history=False）")
        turn_comp = False
    return OrchContext(out_dir=Path(out_dir), agent_base=dict(load_section("agent") or {}),
                       model_options=orch_model_options(opts, getattr(args, "prefix", "mock")),
                       fill_placement=placement,
                       guard=on("guard"), step_compaction=on("step_compaction"),
                       turn_compaction=turn_comp, goal_verify=on("goal_verify"))


def _gen_kwargs(item: Dict[str, Any], sent: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """run_turn 的生成入参：executor.run_item 同款四键 + --sampling 里同名形参（覆盖优先）。"""
    gen = item.get("gen") or {}
    out = {"enable_thinking": bool(gen.get("enable_thinking", True)),
           "thinking_budget": int(gen.get("thinking_budget", 1024)),
           "max_tokens": int(gen.get("max_tokens", 2048)),
           "temperature": float(gen.get("temperature", 0.2))}
    out.update({k: v for k, v in (sent or {}).items() if k in _GEN_KWARGS})
    return out


async def _one_run_orch(args, engine, counter, item, item_idx, level, rep, meta,
                        prefix: Optional["prefix_mod.PrefixAssembly"],
                        ctx: OrchContext) -> Dict[str, Any]:
    """编排器模式单 run：每 run 新建注册表 / Orchestrator / ChatSession，填充按放置铺好后直调
    orch.run_turn，按 history 映射轨迹打分。任何异常收成一条 {"error": …} 记录（同 _one_run）。"""
    import uuid
    from benchmarks.model_eval.executor import _default_max_turns
    from framework.core.journal import new_turn_id
    from framework.core.orchestrator import Orchestrator
    from framework.memory.session import ChatSession
    OT = _orch_tools()
    meta = row_meta(meta, item["id"])
    sampling_seed = args.seed * 1000 + rep
    proxy = None
    session = None
    sid = None
    registry = None
    own_start = 0
    t0 = time.monotonic()
    mark = ctx.cursor.mark()          # 本 run 的事件区间从装配起算（任何出口都结算游标）
    try:
        asm = prefix if prefix is not None else item_prefix(item)
        task = item["task"]
        if not task.get("env"):
            raise ValueError(f"[{item['id']}] 编排器模式只支持带 mock env 的题（task.env）")
        env = make_env(task["env"], task.get("env_init"))
        summaries = prefix_mod.production_toolset().summaries if asm.mode != "mock" else {}
        spill = OT.SpillReadTool()            # W2 / ceiling 桩里的 spill 句柄经它回读
        registry = OT.build_registry(ctx.tools_dir, env, OT.stub_schemas(asm), summaries,
                                     spill_tool=spill)
        max_turns = int(task.get("max_turns") or _default_max_turns(task))
        cfg, _over = OT.agent_cfg(ctx.agent_base, guard=ctx.guard, step=ctx.step_compaction,
                                  goal_verify=ctx.goal_verify, max_total_iterations=max_turns)
        cfg = OT.keep_mock_tools_full(cfg, asm)     # catalog 档：mock 工具并入本 run 的 core
        if args.backend == "fake":
            # 干跑脚本要按计划估算出（prompt_tokens），计划又要先有编排器的声明：先以空脚本构造，
            # 计划后再填 dry_script（脚本已非空 = 测试替身自带脚本，不覆盖）
            from tests.fake_engine import FakeEngine
            inner = FakeEngine([])
        else:
            inner = engine
        proxy = OrchKneeEngine(inner)
        orch = Orchestrator(proxy, registry, cfg, model_options=ctx.model_options)
        turn = ctx.fill_placement == "turn"
        # 首次生成的 prompt 面声明给计划估算。_tools_decl 是 Orchestrator 的私有属性（无公开访问器）：
        # 改名 / 改形态时 test_context_knee_orchestrator.py 的
        # test_orchestrator_end_to_end_plan_steps_meta_calls_and_score（及全部 _run 用例）记 error 行变红
        plan = plan_run(counter, item, level, rep, args.seed, item_idx,
                        dc_replace(asm, tools=list(orch._tools_decl)), fill_after_user=turn)
        if plan.warning:
            print(f"⚠️ {plan.warning}")
        if args.backend == "fake" and not inner.script:
            inner.script.extend(dry_script(plan))
        applied = _seed_sampler(args.backend, sampling_seed)
        bound = getattr(inner, "context_limit", None) is not None
        if bound:
            orch.bind_context_limit()
        system = task.get("system") or asm.system_prompt or DEFAULT_SYSTEM_PROMPT
        sid = f"L{level}-{item['id']}-r{rep}-{uuid.uuid4().hex[:6]}"
        session = ChatSession(sid, ctx.sessions_dir, system_prompt=system,
                              backend=getattr(inner, "backend", None))
        session.add_user_message(plan.item["task"]["user"])     # TurnRunner 同一方法
        fill = copy.deepcopy(plan.filler.messages)
        if turn:
            # 本轮内：user 之后，锚点 2；keep_recent_calls=True 沿用锚点（审批续跑语义），其余轮字段
            # （步骤锚 None、峰值 / 档位 / 计数 0）是新建 ChatSession 的构造缺省
            session.history[2:2] = fill
            session.turn_history_start_idx = 2
        else:
            session.history[1:1] = fill                      # 历史轮：system 之后、user 之前
        own_start = len(session.history)
        proxy.session = session
        spill.session = session
        _INTERRUPT.proxy, _INTERRUPT.inner = proxy, inner
        session.journal.emit("turn_queued", {"prompt": task["user"], "attachments": [],
                                             "gen": {}, "origin": "chat"}, turn_id=new_turn_id())
        session.journal.emit("turn_started", {})
        gen_kwargs = _gen_kwargs(item, (meta.get("sampling_overrides") or {}).get("sent"))
        timeout = getattr(args, "run_timeout", 0) or 0
        coro = orch.run_turn(session, journal=session.journal, keep_recent_calls=turn,
                             compact_history=ctx.turn_compaction, **gen_kwargs)
        try:
            result = await (asyncio.wait_for(coro, timeout) if timeout > 0 else coro)
        except asyncio.TimeoutError:
            # 编排器的取消钩子 = session.cancel_requested（迭代边界 + 引擎逐 chunk 都认）
            session.cancel_requested = True
            elapsed = round(time.monotonic() - t0, 2)
            print(f"⏱ L{level} {item['id']} r{rep} timeout（{elapsed}s > {timeout}s）")
            drain_timeout = await drain_inference(inner, min(60.0, timeout))
            ev, _ab, lines = ctx.cursor.since(mark, sid)
            return {**meta, "level": level, "rep": rep, "item_id": item["id"],
                    "error": "timeout", "elapsed_s": elapsed, "run_timeout_s": timeout,
                    "drain_timeout": drain_timeout,
                    "drain_s": round(time.monotonic() - t0 - elapsed, 2),
                    "gens": proxy.gens, "full_renders": proxy.full_renders,
                    "guard_events": ev, "guard_events_lines": lines,
                    **OT.stub_fields(registry, session.history, own_start),
                    "session_dir": str(session.session_dir),
                    "wall_s": round(time.monotonic() - t0, 2)}
        ev, aborts, lines = ctx.cursor.since(mark, sid)
        status = str(result.get("status") or "")
        aborted = OT.aborted_reason_for(status, aborts, max_turns)
        traj, extra = OT.traj_from_history(
            session.history, own_start, item_id=item["id"], rep=rep, env=env,
            aborted_reason=aborted, wall_s=time.monotonic() - t0, registry=registry)
        if aborted == "context_guard" and traj["turns"]:
            traj["turns"][-1]["text"] = ""    # 护栏终止样板不是答案（原文另记 turn_response）
        score = score_run(plan, traj, proxy.gens, item)
        rec = build_record(plan, traj, proxy.gens, score, item,
                           {**meta, "sampling_seed": sampling_seed,
                            "sampling_seed_applied": applied}, time.monotonic() - t0,
                           full_renders=proxy.full_renders)
        tm = dict(result.get("metrics") or {})
        peak_prompt_measured = _peak_prompt_measured(tm.get("peak_prompt_tokens"))
        rec["fill"]["fill_est_delta"] = plan.fill_est_delta
        rec.update({"peak_prompt_measured": peak_prompt_measured,
                    "turn_status": status, "turn_metrics": tm,
                    "turn_response": result.get("response"),
                    "audit_generations": len(proxy.audit_gens), "audit_gens": proxy.audit_gens,
                    "spill_readbacks": spill.readbacks,
                    "guard_events": ev, "guard_events_lines": lines,
                    "meta_calls": extra["meta_calls"], "dup_blocked": extra["dup_blocked"],
                    "system_errors": extra["system_errors"],
                    "stub_served": extra["stub_served"], "stub_unknown": extra["stub_unknown"],
                    "stub_failed": extra["stub_failed"],
                    "session_dir": str(session.session_dir),
                    "max_total_iterations": cfg.get("max_total_iterations"),
                    "context_guard_bound": bound,
                    "tool_effects": registry.tool_effects() if asm.mode == "mock" else
                    {n: e for n, e in registry.tool_effects().items()
                     if n in {t["name"] for t in asm.mock_tools}}})
        return rec
    except Exception as e:      # noqa: BLE001 —— 批次纪律：单 run 失败只记账
        import traceback
        ev, _ab, lines = ctx.cursor.since(mark, sid)    # 结算游标：后续 run 的行号不漂
        return {**meta, "level": level, "rep": rep, "item_id": item["id"],
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-2000:],
                "gens": proxy.gens if proxy is not None else [],
                "guard_events": ev, "guard_events_lines": lines,
                **OT.stub_fields(registry, session.history if session is not None else None,
                                 own_start),
                "session_dir": str(session.session_dir) if session is not None else None,
                "wall_s": round(time.monotonic() - t0, 2)}
    finally:
        if _INTERRUPT.signum is None:
            _INTERRUPT.proxy = _INTERRUPT.inner = None   # 中断时留给 amain 收尾排空
        if proxy is not None:
            try:
                proxy.release()
            except Exception as e:
                print(f"⚠️ 释放会话缓存失败: {e}")


def _sent_of(r: Dict[str, Any]) -> Dict[str, Any]:
    so = r.get("sampling_overrides") or {}
    return so.get("sent", so.get("applied")) or {}      # applied = 改名前的旧键


def check_resume_compat(transcripts: Path, model: str, sent: Dict[str, Any], seed: int,
                        entry: Dict[str, Any], *, entry_only: bool = False,
                        prefix_mode: str = "mock",
                        thinking_replay: Tuple[str, Optional[int]] = (DEFAULT_REPLAY_MODE, DEFAULT_REPLAY_K),
                        harness_mode: Optional[Tuple[Any, ...]] = None) -> None:
    """续跑只能接同口径的批：已有行的 model / 已发送采样 / seed / 注册表条目采样任一不同即拒
    （混进来的 rep 会被 Pass^2 当作同一组）。缺 model/sampling/seed 字段的行（极旧格式）不参与
    这三项比较；缺 entry_sampling 的行（W-1 之前）视为 {}——当时引擎不读条目采样。
    entry_only（单纯 --append）：只比条目采样与前缀档——summary 按整个 transcripts 聚合，条目口径
    一变，新旧行实际发送的采样就不同，却在 sampling_overrides 上看不出来。
    前缀档（W-2 --prefix）：缺 prefix_mode 的行（W-2 之前）视为 mock；档不同 resume / append 都拒
    （system 与工具声明不同 = 不同测量形态，混算即把两条曲线揉成一条）。
    思考回放口径（W3）：thinking_replay = 本次 (mode, k)（缺省 = 模块缺省 last_k/2，即条目没写键时的口径）；缺 thinking_replay_mode 的行（W3 之前）视为
    (all, None)——当时引擎全部回放；mode 不同即拒，k 只在 mode == last_k 时比较；resume / append 都拒。
    harness 口径（W-K）：harness_mode = HARNESS_MODE_KEYS 元组（None = 直调模式）；缺 mode 的旧行 =
    直调模式；直调行与编排器行、或编排器行开关 / 放置 / 填充形态 fill_shape / 生效阈值 guard_thresholds /
    W2 参数 step_compaction_cfg / 披露生效值 tool_schema_mode_effective / 生效 agent 段指纹
    agent_cfg_fingerprint 不同，resume / append 都拒（直调行只比 mode 与 fill_shape；编排器旧行缺的键不比）。"""
    if not transcripts.exists():
        return
    bad = set()
    entry_bad = set()
    prefix_bad = set()
    replay_bad = set()
    harness_bad = set()
    new_harness = harness_mode if harness_mode is not None else harness_mode_of({})
    new_mode, new_k = thinking_replay
    for ln in transcripts.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        old_prefix = r.get("prefix_mode") or "mock"
        if old_prefix != prefix_mode:
            prefix_bad.add(f"{old_prefix} ≠ {prefix_mode}")
        old_mode = r.get("thinking_replay_mode") or "all"
        old_k = r.get("thinking_replay_k") if "thinking_replay_mode" in r else None
        if old_mode != new_mode or (new_mode == "last_k" and old_k != new_k):
            replay_bad.add(f"{old_mode}/{old_k} ≠ {new_mode}/{new_k}")
        if harness_mismatch(r, tuple(new_harness)):
            harness_bad.add(f"{_harness_label(harness_mode_of(r))} ≠ {_harness_label(tuple(new_harness))}")
        old_entry = r.get("entry_sampling") or {}
        if old_entry != entry:
            entry_bad.add(f"{json.dumps(old_entry, sort_keys=True)} ≠ {json.dumps(entry, sort_keys=True)}")
        if entry_only:
            continue
        if "model" in r and r["model"] != model:
            bad.add(f"model {r['model']} ≠ {model}")
        if "sampling_overrides" in r and _sent_of(r) != sent:
            bad.add(f"sampling {json.dumps(_sent_of(r), sort_keys=True)} ≠ {json.dumps(sent, sort_keys=True)}")
        if "seed" in r and r["seed"] != seed:
            bad.add(f"seed {r['seed']} ≠ {seed}")
    if entry_bad:
        bad.add("注册表条目采样口径已变（W-1），不能与旧目录合并；另起目录（entry_sampling "
                + "；".join(sorted(entry_bad)) + "）")
    if prefix_bad:
        bad.add("前缀档 --prefix 不同（W-2），不能与旧目录合并；另起目录（prefix_mode "
                + "；".join(sorted(prefix_bad)) + "）")
    if replay_bad:
        bad.add("思考回放口径 --thinking-replay 不同（W3），不能与旧目录合并；另起目录（thinking_replay "
                + "；".join(sorted(replay_bad)) + "）")
    if harness_bad:
        bad.add("harness 模式 / 编排器开关不同（W-K），直调行与编排器行不能混入同一 transcripts；另起目录（"
                + "；".join(sorted(harness_bad)) + "）")
    if bad:
        raise SystemExit("❌ --resume/--append 口径不一致，拒绝合并：" + "；".join(sorted(bad)))


def _harness_label(t: Tuple[Any, ...]) -> str:
    if t[0] == "direct":
        return "direct"
    return "/".join(f"{k}={json.dumps(v, sort_keys=True, ensure_ascii=False) if isinstance(v, dict) else v}"
                    for k, v in zip(HARNESS_MODE_KEYS, t))


def load_done(transcripts: Path) -> set:
    """已成功完成的 (level, item_id, rep)；error 行（含 timeout）不算完成。"""
    done = set()
    if transcripts.exists():
        for ln in transcripts.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            if "error" not in r:
                done.add((int(r["level"]), r["item_id"], int(r["rep"])))
    return done


def _print_run(rec: Dict[str, Any]) -> None:
    head = f"L{rec['level']} {rec['item_id']} r{rec['rep']}"
    if rec.get("error") == "timeout":
        return                                  # ⏱ 行已在 _one_run 打过
    if "error" in rec:
        print(f"💥 {head} 异常（已记录，继续）: {rec['error'][:160]}")
        return
    score, f = rec["score"], rec["fill"]
    mark = "✅" if score.get("pass") else "❌"
    rc = "-" if score["recall_hits"] is None else f"{score['recall_hits']}/3"
    why = "" if score.get("pass") else "  | " + "; ".join(score.get("why") or [])[:100]
    print(f"{mark} {head} prompt={f['prompt_tokens_actual']} (est {f['prompt_tokens_est']}, "
          f"err {f['fill_error']}) recall {rc} rep_guard {score['repetition_hits']} "
          f"({rec['wall_s']}s){why}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="有效上下文拐点测量（longchain + 真实形态填充）")
    ap.add_argument("--model", default="", help="注册表 repo 全名或末段短名（真后端必填）")
    ap.add_argument("--backend", default="remote", help="remote|mlx|fake")
    ap.add_argument("--levels", default=DEFAULT_LEVELS,
                    help="首次生成 prompt 总 token 目标（0 = 不填充的自然基线）")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--tasks", default="hard", choices=["hard", "seed"])
    ap.add_argument("--ids", default="", help="只跑这些题 id（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="", help="产物目录（缺省 results/<模型短名>/<时间戳>）")
    ap.add_argument("--dry-run", action="store_true", help="= --backend fake：FakeEngine 跑完全流程")
    ap.add_argument("--sampling", default="",
                    help="采样覆盖 k=v[,k=v]；生效键 temperature/min_p/max_tokens/repetition_penalty/"
                         "top_p/top_k/presence_penalty/frequency_penalty")
    ap.add_argument("--run-timeout", type=float, default=600.0,
                    help="单 run 墙钟上限秒（0=不限）；超时记 error=timeout 行并继续")
    ap.add_argument("--resume", action="store_true",
                    help="隐含 --append：跳过 --out 里已成功的 (level,题,rep)，error 行重跑")
    ap.add_argument("--append", action="store_true",
                    help="--out 已有 transcripts.jsonl 时追加（缺省拒绝，防两批混算）")
    ap.add_argument("--prefix", default="mock", choices=list(prefix_mod.PREFIX_MODES),
                    help="前缀档（W-2）：mock=极简 system + 题目 mock 工具（缺省，W-2 前口径）；"
                         "prod=生产 system + 生产工具全量完整 schema；catalog=生产 system + 元工具/"
                         "core 完整、其余目录行；routed=catalog + 按题面路由 top_k 件完整。"
                         "题目 mock 工具恒完整、同名优先；直调模式生产工具只声明不执行，"
                         "--orchestrator 下 prod / catalog 注册生产可用桩（orch_stubs.py）")
    ap.add_argument("--thinking-replay", default=None, choices=list(THINKING_REPLAY_MODES),
                    help="思考回放口径覆盖（W3 / D36）：缺省沿用注册表条目策略；all = 全部回放"
                         "（10-30/31 已有结果的口径，同口径对比用）")
    ap.add_argument("--thinking-replay-k", type=int, default=None,
                    help="last_k 的 K 覆盖（正整数；缺省沿用条目值，条目也无则 2）")
    # W-K 编排器模式：以下开关只在 --orchestrator 下有效（缺省 None = 未给；直调模式给了即报错）
    ap.add_argument("--orchestrator", action="store_true",
                    help="编排器模式：每 run 新建 Orchestrator 直调 run_turn（W1 护栏 / W2 步骤边界压缩 / "
                         "轮统计在路径上）；缺省 = 直调 engine.generate_once（executor.run_item）")
    ap.add_argument("--fill-placement", default=None, choices=["history", "turn"],
                    help="[编排器] 填充放置：history = 题面 user 之前（历史轮）；turn = user 之后、"
                         "本轮内（缺省）")
    ap.add_argument("--guard", default=None, choices=["on", "off"],
                    help="[编排器] W1 上限护栏 agent.context_guard.enabled（缺省 on）")
    ap.add_argument("--step-compaction", default=None, choices=["on", "off"],
                    help="[编排器] W2 步骤边界压缩 agent.step_compaction.enabled（缺省 on）")
    ap.add_argument("--turn-compaction", default=None, choices=["on", "off"],
                    help="[编排器] 轮首机械压缩 run_turn(compact_history=)（缺省 on；turn 放置强制 off）")
    ap.add_argument("--goal-verify", default=None, choices=["on", "off"],
                    help="[编排器] 收尾目标校验 agent.goal_verify（缺省 off：省一次生成）")
    return ap


def check_mode_args(ap: argparse.ArgumentParser, args) -> None:
    """编排器专属开关只在 --orchestrator 下有效；编排器模式只支持 ORCH_PREFIX_MODES。"""
    if not args.orchestrator:
        given = [f"--{k.replace('_', '-')}" for k in ORCH_FLAGS if getattr(args, k) is not None]
        if given:
            ap.error(f"{' '.join(given)} 只在 --orchestrator 下有效")
        return
    if args.prefix not in ORCH_PREFIX_MODES:
        ap.error(f"--orchestrator 只支持 --prefix {'|'.join(ORCH_PREFIX_MODES)}（得到 {args.prefix}）："
                 "编排器模式的披露（full / catalog + 任务期路由）由编排器自己做（prod 按注册表条目 "
                 "tool_schema_mode，catalog 强制目录模式）；routed 是直调模式的手工路由形态")


def main(argv: Optional[List[str]] = None) -> int:
    try:
        ap = build_parser()
        args = ap.parse_args(argv)
        check_mode_args(ap, args)
        rc = asyncio.run(amain(args))
        if _INTERRUPT.signum is not None:
            _hard_exit(rc)      # 被信号中断：不等非守护推理线程（见 _hard_exit）
        return rc
    finally:
        _restore_default_signals()   # 「立即退出」处理器只活到 main 真正退出前


if __name__ == "__main__":
    sys.exit(main())
