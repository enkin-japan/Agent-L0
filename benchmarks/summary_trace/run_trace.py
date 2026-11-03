"""TRACE 式成对续跑评估（W4 / D4 / D5）：同一执行状态分叉两支续跑，量摘要折叠让续跑多做了多少重复功。

  backend/.venv/bin/python benchmarks/summary_trace/run_trace.py --dry-run --limit 2 --reps 1 --out <dir>
  backend/.venv/bin/python benchmarks/summary_trace/run_trace.py --report --out <dir>

每 (题 × rep)：
  Turn 1   新 ChatSession + 新 Orchestrator（mock env 工具，与 run_knee 编排器模式同一套装配），
           max_total_iterations = --turn1-iterations，跑题面直到熔断/收尾；按 TurnRunner._turn_record
           同一形态落盘一条轮次记录（含 W4 的 task_state；--task-state off 则不带）。
  分叉     env 从 turn 1 结束时的环境对象**深拷贝**（含故障配额与只读集；snapshot() 只有
           files / todos，不足以还原）；两支各自独立的会话目录。
  PRE      history = turn 1 全文 → 续跑 user 消息 → run_turn（--continue-iterations）。
  POST     先 maybe_summarize_context 折叠整个 turn 1（history 重建为 system + 摘要块；--template
           决定摘要指令，--tool-digest-chars 决定摘要器输入带不带工具段）→ 同一句续跑 user 消息 → run_turn。
           实际送进 engine.summarize 的输入原文落在行字段 summary_input。
指标与报表见 README.md；产物：<out>/transcripts.jsonl（每支一行）、<out>/sessions/、事件流。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
for _p in (BACKEND, BACKEND / "benchmarks"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))       # 末位：只为 import context_knee / summary_trace

from benchmarks.model_eval.executor import DEFAULT_SYSTEM_PROMPT, function_calls  # noqa: E402
from benchmarks.model_eval.scorers import get_scorer                        # noqa: E402
from context_knee import orch_tools as OT                                   # noqa: E402
from context_knee import run_knee as K                                      # noqa: E402
from framework.core.orchestrator import NO_PLAN_NUDGE_ITERATIONS            # noqa: E402
from framework.core.thinking_replay import THINKING_REPLAY_MODES            # noqa: E402
from framework.memory.compactor import (SUMMARY_TOOL_DIGEST_CHARS,          # noqa: E402
                                        _TOOL_SECTION_HEADER)

CONTINUE_PROMPT = {
    "en": "Continue the task from where it stopped and finish the remaining steps.",
    "zh": "从上次停下的地方继续这个任务，把剩下的步骤做完。",
}
TEMPLATES = ("legacy", "new")
BRANCHES = ("pre", "post")


def continue_prompt(item: Dict[str, Any]) -> str:
    return CONTINUE_PROMPT["zh" if str(item.get("lang") or "").startswith("zh") else "en"]


def template_instruction(template: str) -> str:
    """--template → 传给 summarize 的指令全文（两档都显式传，产物可追溯）。"""
    from framework.core.engine import AgentEngine
    if template == "legacy":
        return AgentEngine.SUMMARY_INSTRUCTION_LEGACY
    if template == "new":
        return AgentEngine.SUMMARY_INSTRUCTION
    raise ValueError(f"--template 须为 {TEMPLATES} 之一：{template!r}")


def call_key(c: Dict[str, Any]) -> Tuple[str, str]:
    """(name, args 规范化 JSON)：与 longchain 评分器 redundant_calls 同一口径（sort_keys）。"""
    return (str(c.get("name")),
            json.dumps(c.get("arguments"), sort_keys=True, ensure_ascii=False))


def traj_calls(traj: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [call_key(c) for t in traj.get("turns") or [] for c in function_calls(t)]


def count_repeated(history: List[Dict[str, Any]], start: int,
                   c1: Set[Tuple[str, str]]) -> int:
    """续跑轮（history[start:]）里 (name, args) ∈ turn 1 调用集、且**真执行了**的调用数。

    回执以 DUP_PREFIX（「Duplicate call blocked」）开头的调用不计——它已计入 blocked，
    再计 repeated 就双计了。元工具不计。调用与回执按工具名在紧随其后的 tool 消息里按序
    配对（编排层回执顺序 = 调用顺序）；找不到回执的调用照计（没被拦即视为执行）。
    """
    n = 0
    msgs = history[start:]
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        calls = [c for c in m.get("tool_calls") or []
                 if c.get("type") == "function" and c.get("name") not in OT.META_NAMES]
        if not calls:
            continue
        nxt = msgs[i + 1] if i + 1 < len(msgs) and msgs[i + 1].get("role") == "tool" else {}
        pending: Dict[str, List[str]] = {}
        for tr in nxt.get("tool_responses") or []:
            if isinstance(tr, dict):
                pending.setdefault(str(tr.get("name")), []).append(str(tr.get("response", "")))
        for c in calls:
            queue = pending.get(str(c.get("name"))) or []
            resp = queue.pop(0) if queue else ""
            if call_key(c) in c1 and not resp.startswith(OT.DUP_PREFIX):
                n += 1
    return n


def count_tool_lines(text: str) -> int:
    """摘要器输入里工具段的调用行数：工具段头行之后、连续以 `- ` 开头的行（task_state 的便签
    行同样以 `- ` 开头，故只数头行之后那一段）。"""
    n, inside = 0, False
    for ln in (text or "").splitlines():
        if ln == _TOOL_SECTION_HEADER:
            inside = True
        elif inside and ln.startswith("- "):
            n += 1
        else:
            inside = False
    return n


class SummarizeCapture:
    """POST 支摘要器输入捕获：maybe_summarize_context 只用 engine.summarize 这一个面，传它一个
    转发壳，记下**实际送入**的 content（= 生产拼好的 batch_text，不含 engine 自己加的
    [Existing summary] / [New conversation] 外壳）。选它而不是在 harness 里用 _collect_turn_texts
    重算一遍：重算要复刻水位线切片与 join 口径，生产一改就静默漂移；转发壳只依赖 summarize 的签名。
    也不改共享引擎对象（真后端各题共用同一个 engine）。"""

    def __init__(self, inner):
        self.inner = inner
        self.contents: List[str] = []

    def __getattr__(self, name):
        # 其余属性一律转发到被包的引擎（生产以后若在 summarize 之外多读一个属性，不至于 AttributeError）
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    async def summarize(self, content, *args, **kwargs):
        self.contents.append(content)
        return await self.inner.summarize(content, *args, **kwargs)


# ---------------- 干跑脚本（FakeEngine） ----------------

def _read_call(env) -> Dict[str, Any]:
    names = {t["name"] for t in env.tools()}
    name = "list_files" if "list_files" in names else "todo_list"
    return {"type": "function", "name": name, "arguments": {}}


def _m(pt: int) -> Dict[str, Any]:
    return {"prompt_tokens": pt, "generation_tokens": 24, "prompt_tps": 1000.0,
            "generation_tps": 50.0}


def dry_turn1_script(env) -> List[Dict[str, Any]]:
    """建两步计划 → 一次只读调用 → remember 记一个值（3 次迭代，--turn1-iterations 缺省 3 熔断）。"""
    return [
        {"tool_calls": [{"type": "function", "name": "update_plan",
                         "arguments": {"op": "replace", "steps": ["inspect inputs", "write result"]}}],
         "metrics": _m(600)},
        {"tool_calls": [_read_call(env)], "metrics": _m(700)},
        {"tool_calls": [{"type": "function", "name": "remember",
                         "arguments": {"note": "dry-run key = 42"}}], "metrics": _m(800)},
    ]


def dry_continue_script(env) -> List[Dict[str, Any]]:
    """续跑：重做 turn 1 的那次只读调用（repeated 必为正）→ 两步 complete_step → 收尾正文。"""
    return [
        {"tool_calls": [_read_call(env)], "metrics": _m(500)},
        {"tool_calls": [{"type": "function", "name": "complete_step", "arguments": {"result": "inputs read"}},
                        {"type": "function", "name": "complete_step", "arguments": {"result": "result written"}}],
         "metrics": _m(600)},
        {"text": "dry-run 续跑收尾。", "metrics": _m(700)},
    ]


# ---------------- 装配 ----------------

def _registry(ctx: "K.OrchContext", env, asm):
    """每支一个注册表（env 工具绑本支的 env 副本）；tools_dir = 产物目录下的空目录（同 run_knee）。"""
    summaries = K.prefix_mod.production_toolset().summaries if asm.mode != "mock" else {}
    spill = OT.SpillReadTool()
    reg = OT.build_registry(ctx.tools_dir, env, OT.stub_schemas(asm), summaries,
                            spill_tool=spill)
    return reg, spill


def _cfg(ctx: "K.OrchContext", iterations: int) -> Dict[str, Any]:
    """config.yaml agent 段 + W1/W2/goal-verify 关（本评估只测摘要）+ 迭代熔断覆盖。"""
    cfg, _over = OT.agent_cfg(ctx.agent_base, guard=False, step=False, goal_verify=False)
    cfg["max_total_iterations"] = int(iterations)    # agent 段缺该键时 agent_cfg 不覆盖，这里显式钉
    return cfg


async def _run_turn(args, orch, session, prompt: str, item: Dict[str, Any], seed: int,
                    inner) -> Tuple[Dict[str, Any], int, float]:
    """一轮：user 消息 → turn_queued/started → run_turn。返回 (result, 本轮起点, 墙钟)。"""
    from framework.core.journal import new_turn_id
    session.cancel_requested = False
    session.add_user_message(prompt)                  # TurnRunner 同一方法
    start = len(session.history)                      # = turn_runner 的 history_start_idx
    session.journal.emit("turn_queued", {"prompt": prompt, "attachments": [], "gen": {},
                                         "origin": "chat"}, turn_id=new_turn_id())
    session.journal.emit("turn_started", {})
    K._seed_sampler(args.backend, seed)
    gen = K._gen_kwargs(item, None)
    t0 = time.monotonic()
    coro = orch.run_turn(session, journal=session.journal, **gen)
    timeout = float(getattr(args, "run_timeout", 0) or 0)
    try:
        result = await (asyncio.wait_for(coro, timeout) if timeout > 0 else coro)
    except asyncio.TimeoutError:
        session.cancel_requested = True
        await K.drain_inference(inner, min(60.0, timeout))
        raise TimeoutError(f"run_turn 超时（{timeout}s）")
    return result, start, time.monotonic() - t0


def turn_record(orch, prompt: str, item: Dict[str, Any], status: str,
                messages: List[Dict[str, Any]], metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """轮次记录：直接复用 TurnRunner._turn_record（生产同一形态；调度器不参与组记录）。"""
    from framework.core.turn_runner import TurnRunner
    ctx = {"prompt": prompt, "images": [], "audios": [], "ingested_text": "", "attachments": [],
           "gen": K._gen_kwargs(item, None)}
    return TurnRunner(orch, None)._turn_record(ctx, status, messages, metrics)


def land_turn1(session, record: Dict[str, Any], task_state_on: bool) -> None:
    """落盘 turn 1 记录。on = 生产路径（append_log 盖 task_state）；off = 模拟 W4 前：落盘期间
    把任务状态与弃前渲染藏起来，记录不带该键，随后原样放回（续跑两支照常持有该任务）。"""
    if task_state_on:
        session.append_log(record)
        return
    held, session.task_state = session.task_state, None
    session.last_task_state_render = ""
    try:
        session.append_log(record)
    finally:
        session.task_state = held


def fork_session(src, sid: str, sessions_dir: Path, system_prompt: str, backend):
    """从 turn 1 会话分叉一支：新会话目录（不共享 memory.json）、复制 history.jsonl、
    history 深拷贝、任务状态经 snapshot 重建、路由保留集合照抄。"""
    from framework.core.task_state import TaskState
    from framework.memory.session import ChatSession
    s = ChatSession(sid, sessions_dir, system_prompt=system_prompt, backend=backend)
    if src.history_log.exists():
        shutil.copyfile(src.history_log, s.history_log)
    s.history = copy.deepcopy(src.history)
    s.task_state = TaskState.from_snapshot(src.task_state.snapshot()) if src.task_state else None
    s.last_routed_tools = copy.deepcopy(src.last_routed_tools)
    return s


def _est_tokens(counter, text: str) -> int:
    return int(counter.encode_len(text)) if text else 0


# ---------------- 单题 ----------------

async def run_pair(args, engine, counter, item: Dict[str, Any], rep: int, meta: Dict[str, Any],
                   asm, ctx: "K.OrchContext") -> List[Dict[str, Any]]:
    """一道题一个 rep：turn 1 → PRE / POST 两支续跑。返回两行（异常 = 一行 error）。"""
    from benchmarks.model_eval.mock_envs import make_env
    from framework.core.orchestrator import Orchestrator
    from framework.memory.compactor import maybe_summarize_context
    from framework.memory.session import ChatSession
    t0 = time.monotonic()
    task = item["task"]
    base = {**meta, "item": item["id"], "rep": rep, "lang": item.get("lang"),
            "template": args.template, "task_state": args.task_state,
            "drop_task": args.drop_task}
    proxies: List[Any] = []
    cap: Optional[SummarizeCapture] = None   # POST 支的摘要器捕获；失败支也要落盘它看到的输入
    try:
        if not task.get("env"):
            raise ValueError(f"[{item['id']}] 只支持带 mock env 的题（task.env）")
        system = task.get("system") or asm.system_prompt or DEFAULT_SYSTEM_PROMPT
        tag = f"{item['id']}-r{rep}-{uuid.uuid4().hex[:6]}"
        seed = args.seed * 1000 + rep

        def make_inner(script):
            if args.backend == "fake":
                from tests.fake_engine import FakeEngine
                return FakeEngine(list(script))
            return engine

        # ---- Turn 1 ----
        env1 = make_env(task["env"], task.get("env_init"))
        reg1, spill1 = _registry(ctx, env1, asm)
        inner1 = make_inner(dry_turn1_script(env1))
        proxy1 = K.OrchKneeEngine(inner1)
        proxies.append(proxy1)
        orch1 = Orchestrator(proxy1, reg1, OT.keep_mock_tools_full(_cfg(ctx, args.turn1_iterations), asm),
                             model_options=ctx.model_options)
        s1 = ChatSession(f"{tag}-t1", ctx.sessions_dir, system_prompt=system,
                         backend=getattr(inner1, "backend", None))
        proxy1.session = s1
        spill1.session = s1
        r1, start1, _w1 = await _run_turn(args, orch1, s1, task["user"], item, seed, inner1)
        status1 = str(r1.get("status") or "")
        traj1, extra1 = OT.traj_from_history(s1.history, start1, item_id=item["id"], rep=rep,
                                             env=env1, registry=reg1)
        c1 = set(traj_calls(traj1))
        e1 = env1.snapshot()
        record = turn_record(orch1, task["user"], item, status1,
                             s1.persistable_messages(start1), r1.get("metrics"))
        land_turn1(s1, record, args.task_state == "on")
        turn1_meta = {"turn1_status": status1, "turn1_calls": len(traj_calls(traj1)),
                      "turn1_distinct_calls": len(c1), "turn1_env_state": e1,
                      "turn1_record_has_task_state": "task_state" in record,
                      # turn 1 结束时有没有建成计划：缺省 --turn1-iterations 3 = NO_PLAN_NUDGE_ITERATIONS，
                      # nudge 到第 3 次无计划迭代才贴、而熔断恰在第 3 次，模型没有迭代去响应它
                      "turn1_plan_built": s1.task_state is not None and bool(s1.task_state.plan),
                      "turn1_dup_blocked": extra1["dup_blocked"],
                      "turn1_gens": len(proxy1.gens),
                      "turn1_session_dir": str(s1.session_dir)}
        proxy1.release()

        rows: List[Dict[str, Any]] = []
        instruction = template_instruction(args.template)
        for branch in BRANCHES:
            b0 = time.monotonic()
            env = copy.deepcopy(env1)                    # 含故障配额 / 只读集
            if env.snapshot() != e1:
                raise RuntimeError("env 分叉副本与 turn 1 快照不一致")
            reg, spill = _registry(ctx, env, asm)
            inner = make_inner(dry_continue_script(env))
            proxy = K.OrchKneeEngine(inner)
            proxies.append(proxy)
            orch = Orchestrator(proxy, reg,
                                OT.keep_mock_tools_full(_cfg(ctx, args.continue_iterations), asm),
                                model_options=ctx.model_options)
            s = fork_session(s1, f"{tag}-{branch}", ctx.sessions_dir, system,
                             getattr(inner, "backend", None))
            if args.drop_task == "on":
                # 模拟「任务完成后被弃」：续跑只能靠 history（PRE 原文 / POST 摘要）找回进度
                s.task_state = None
            cont_live_task = s.task_state is not None
            proxy.session = s
            spill.session = s
            summary = ""
            summary_input: Optional[str] = None
            folded = 0
            if branch == "post":
                cap = SummarizeCapture(inner)
                folded = await maybe_summarize_context(
                    s, cap, threshold_tokens=1, keep_tail_turns=0,
                    last_prompt_tokens=10 ** 9, sediment=False,
                    max_tokens=args.summary_max_tokens, rewrite_every=0,
                    instruction=instruction, tool_digest_chars=args.tool_digest_chars)
                summary = s.rolling_summary
                summary_input = cap.contents[-1] if cap.contents else None
                if not folded or not summary.strip():
                    raise RuntimeError("POST 折叠失败（摘要为空或无可折叠轮次）")
            prompt = continue_prompt(item)
            # 首次生成 prompt 估算：续跑 user 消息加进 history 后、首次生成前（PromptCounter 同口径）
            est_hist = s.history + [{"role": "user", "content": prompt}]
            gen = K._gen_kwargs(item, None)
            first_est = counter.count(est_hist, list(orch._tools_decl),
                                      bool(gen.get("enable_thinking")))
            mark = ctx.cursor.mark()
            r, start, wall = await _run_turn(args, orch, s, prompt, item, seed, inner)
            status = str(r.get("status") or "")
            guard_ev, aborts, _lines = ctx.cursor.since(mark, s.session_id)
            ev = OT.aborted_reason_for(status, aborts, args.continue_iterations)
            cont_traj, extra = OT.traj_from_history(
                s.history, start, item_id=item["id"], rep=rep, env=env,
                aborted_reason=ev, wall_s=wall, registry=reg)
            scorer = get_scorer(item["dimension"])
            cont_score = scorer(cont_traj, item)
            full_traj = {**cont_traj, "turns": list(traj1["turns"]) + list(cont_traj["turns"])}
            full_score = scorer(full_traj, item)
            gens = proxy.gens
            first_actual = gens[0].get("prompt_tokens") if gens else None
            rows.append({
                **base, **turn1_meta, "branch": branch,
                "repeated": count_repeated(s.history, start, c1),
                "cont_live_task": cont_live_task,
                "blocked": extra["dup_blocked"] + extra["system_errors"],
                "dup_blocked": extra["dup_blocked"], "system_errors": extra["system_errors"],
                "env_errors": cont_score["env_errors"],
                "score": bool(full_score["pass"]), "state_ok": bool(full_score["state_ok"]),
                "score_why": full_score["why"],
                "cont_steps": cont_score["steps"], "cont_redundant_calls": cont_score["redundant_calls"],
                "meta_calls": extra["meta_calls"],
                "first_prompt_tokens": first_est if args.backend == "fake" else first_actual,
                "first_prompt_tokens_est": first_est,
                "first_prompt_tokens_source": "chars/3.5" if args.backend == "fake" else "engine",
                "summary_chars": len(summary) if branch == "post" else None,
                "summary_tokens_est": _est_tokens(counter, summary) if branch == "post" else None,
                "summary": summary if branch == "post" else None,
                "summary_input": summary_input,
                "summary_input_chars": len(summary_input) if summary_input is not None else None,
                "summary_input_tool_lines": (count_tool_lines(summary_input)
                                             if summary_input is not None else None),
                "history_head": (s.history[1].get("content") if len(s.history) > 1 else None)
                if branch == "post" else None,
                "cont_status": status, "cont_aborted_reason": ev, "guard_events": guard_ev,
                "cont_response": r.get("response"),
                "cont_gens": len(gens), "cont_turns": cont_traj["turns"],
                "env_state": cont_traj.get("env_state"),
                "session_dir": str(s.session_dir),
                "wall_s": round(time.monotonic() - b0, 2),
            })
            proxy.release()
        for row in rows:
            row["pair_wall_s"] = round(time.monotonic() - t0, 2)
        return rows
    except Exception as e:      # noqa: BLE001 —— 批次纪律：单题失败只记账
        import traceback
        failed_input = cap.contents[-1] if cap is not None and cap.contents else None
        return [{**base, "branch": None, "error": f"{type(e).__name__}: {e}",
                 "traceback": traceback.format_exc()[-2000:],
                 "summary_input": failed_input,
                 "summary_input_chars": len(failed_input) if failed_input is not None else None,
                 "wall_s": round(time.monotonic() - t0, 2)}]
    finally:
        for p in proxies:
            try:
                p.release()
            except Exception as e:      # noqa: BLE001
                print(f"⚠️ 释放会话缓存失败: {e}")


# ---------------- 主流程 ----------------

async def amain(args) -> int:
    from framework.memory import session as session_mod
    g = K._OrchGlobals()
    header_prev = session_mod.SUMMARY_BLOCK_HEADER
    if args.template == "legacy":
        # legacy 臂连摘要块头也用 W4 前的旧头：A/B 两臂只差「指令 + 头」。进程内 run 期间生效，finally 还原
        session_mod.SUMMARY_BLOCK_HEADER = session_mod.SUMMARY_BLOCK_HEADER_LEGACY
    try:
        return await _amain(args, g)
    finally:
        session_mod.SUMMARY_BLOCK_HEADER = header_prev
        g.restore()


async def _amain(args, orch_globals) -> int:
    from framework.utils.config_loader import load_section
    from framework.utils.model_selection import model_options
    items = K.select_items(args)
    if args.dry_run:
        args.backend = "fake"
    if args.backend not in ("remote", "mlx", "fake"):
        raise SystemExit(f"❌ --backend 须为 remote|mlx|fake：{args.backend!r}")
    template_instruction(args.template)              # 写错先拒
    repo, opts = "dry-run/fake", {}
    if args.model:
        from benchmarks.model_eval.run_eval import resolve_repo
        repo = resolve_repo(args.model)
        opts = model_options(repo)
    elif args.backend != "fake":
        raise SystemExit("❌ 真后端须给 --model")
    replay_override = K.resolve_replay_override(args, opts)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.out) if args.out else HERE / "results" / Path(repo).name / stamp
    transcripts = outdir / "transcripts.jsonl"
    if transcripts.exists():
        raise SystemExit(f"❌ {transcripts} 已存在：换 --out（成对评估不支持追加混批）")
    outdir.mkdir(parents=True, exist_ok=True)
    ctx = K.OrchContext(out_dir=outdir, agent_base=dict(load_section("agent") or {}),
                        model_options=K.orch_model_options(opts, args.prefix),
                        fill_placement="history",
                        guard=False, step_compaction=False, turn_compaction=True,
                        goal_verify=False)
    orch_globals.install(ctx)                        # 事件流落产物目录、审批进程内全放行

    if args.backend == "fake":
        engine = None
        counter = K.fake_counter(opts)
        if replay_override is not None:
            counter.replay = replay_override
    else:
        engine = await K.make_engine(args, repo, opts)
        try:
            if replay_override is not None:
                engine.thinking_replay = replay_override
            counter = K.engine_counter(engine)
        except BaseException:
            engine.backend.shutdown()
            raise
    try:
        prefixes, prefix_meta = K.build_prefixes(args.prefix, items, engine, repo, opts)
    except BaseException:
        if engine is not None:
            engine.backend.shutdown()
        raise
    meta = {"model": repo, "revision": opts.get("revision"), "backend": args.backend,
            "tasks": args.tasks, "seed": args.seed, "prefix_mode": args.prefix,
            "turn1_iterations": args.turn1_iterations,
            # 无计划提醒阈值（orchestrator.NO_PLAN_NUDGE_ITERATIONS）：turn1_iterations ≤ 它时，
            # 提醒最多贴在熔断那一次迭代上，模型没有机会响应（建计划 → task_state 才有 plan）
            "no_plan_nudge_iterations": NO_PLAN_NUDGE_ITERATIONS,
            "tool_digest_chars": args.tool_digest_chars,
            "continue_iterations": args.continue_iterations,
            "summary_max_tokens": args.summary_max_tokens, "drop_task": args.drop_task,
            "token_counter": counter.mode,
            "thinking_replay_mode": counter.replay.mode if counter.replay else None,
            "thinking_replay_k": counter.replay.k if counter.replay else None,
            "stamp": stamp, **K.context_limit_meta(engine)}
    print(f"== 成对续跑评估 {repo}（{args.backend}，模板 {args.template}，task_state {args.task_state}）："
          f"{len(items)} 题 × {args.reps} rep → {outdir}")
    n_err = 0
    try:
        with transcripts.open("a", encoding="utf-8") as fh:
            for item in items:
                for rep in range(args.reps):
                    rows = await run_pair(args, engine, counter, item, rep, meta,
                                          prefixes[item["id"]], ctx)
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                        if "error" in row:
                            n_err += 1
                            print(f"❌ {item['id']} r{rep}: {row['error']}")
                        else:
                            print(f"   {item['id']} r{rep} {row['branch']}: repeated={row['repeated']} "
                                  f"blocked={row['blocked']} pass={row['score']} "
                                  f"first_pt={row['first_prompt_tokens']}")
                    fh.flush()
    finally:
        if engine is not None:
            try:
                engine.backend.shutdown()
            except Exception as e:      # noqa: BLE001
                print(f"⚠️ 后端关闭异常: {e}")
    (outdir / "meta.json").write_text(json.dumps(
        {**meta, "template": args.template, "task_state": args.task_state,
         "items": [it["id"] for it in items], "reps": args.reps, **prefix_meta},
        ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    from summary_trace.summarize_trace import report
    print(report([outdir], seed=args.seed))
    print(f"== 产物: {outdir}（{n_err} 个 error）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="TRACE 式成对续跑评估（摘要折叠前/后续跑对照）")
    ap.add_argument("--model", default="", help="注册表 repo 全名或末段短名（真后端必填）")
    ap.add_argument("--backend", default="remote", help="remote|mlx|fake")
    ap.add_argument("--dry-run", action="store_true", help="= --backend fake：FakeEngine 跑完全流程")
    ap.add_argument("--tasks", default="hard", choices=["hard", "seed"])
    ap.add_argument("--ids", default="", help="只跑这些题 id（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="", help="产物目录（缺省 results/<模型短名>/<时间戳>）")
    ap.add_argument("--prefix", default="mock", choices=list(K.ORCH_PREFIX_MODES),
                    help="前缀档：mock = 极简 system + 题目 mock 工具；prod = 生产 system + 生产工具桩")
    ap.add_argument("--template", default="new", choices=list(TEMPLATES),
                    help="POST 支摘要指令：legacy = W4 前自由要点；new = W4 固定节模板")
    ap.add_argument("--task-state", default="on", choices=["on", "off"],
                    help="turn 1 落盘记录带不带 task_state（off = 模拟 W4 前的摘要器输入）")
    ap.add_argument("--turn1-iterations", type=int, default=3,
                    help="turn 1 的 max_total_iterations（截断点）。缺省 3 = 无计划提醒阈值"
                         "（NO_PLAN_NUDGE_ITERATIONS）：提醒贴在熔断那一次，模型来不及响应；要看提醒"
                         "起作用（建计划 → task_state 带 plan）用 6")
    ap.add_argument("--continue-iterations", type=int, default=5,
                    help="续跑轮的 max_total_iterations")
    ap.add_argument("--tool-digest-chars", type=int, default=SUMMARY_TOOL_DIGEST_CHARS,
                    help="POST 摘要器输入里每条工具结果 digest 的上限（缺省 = 生产 "
                         "compactor.SUMMARY_TOOL_DIGEST_CHARS；0 = 关，只列工具名的旧格式）")
    ap.add_argument("--summary-max-tokens", type=int, default=900,
                    help="POST 摘要生成 max_tokens（缺省 900 = config.yaml agent.summary_max_tokens）")
    ap.add_argument("--drop-task", default="off", choices=["on", "off"],
                    help="on = 两支续跑前都把 turn 1 遗留的任务状态置空（任务已弃、只能靠摘要的 W4 "
                         "目标场景）；off = 保留（生产里未完成任务的真实形态）")
    ap.add_argument("--run-timeout", type=float, default=600.0,
                    help="单次 run_turn 墙钟上限秒（0=不限）；超时该题记 error 行")
    ap.add_argument("--thinking-replay", default=None, choices=list(THINKING_REPLAY_MODES),
                    help="思考回放口径覆盖（同 run_knee）")
    ap.add_argument("--thinking-replay-k", type=int, default=None, help="last_k 的 K 覆盖（同 run_knee）")
    ap.add_argument("--report", action="store_true",
                    help="只读 --out 的 transcripts.jsonl 出汇总表（CPU），不跑")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.report:
        if not args.out:
            ap.error("--report 须配 --out <产物目录>")
        from summary_trace.summarize_trace import report
        print(report([Path(args.out)], seed=args.seed))
        return 0
    for k in ("turn1_iterations", "continue_iterations"):
        if getattr(args, k) < 1:
            ap.error(f"--{k.replace('_', '-')} 须 ≥ 1")
    if args.tool_digest_chars < 0:
        ap.error("--tool-digest-chars 须 ≥ 0（0 = 关）")
    if not 128 <= args.summary_max_tokens <= 4096:
        ap.error("--summary-max-tokens 须在 128..4096（与 agent.summary_max_tokens 同界）")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
