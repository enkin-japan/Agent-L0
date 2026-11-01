"""W1 上限护栏端到端金测试（阶段复审补测；FakeEngine / CPU，不起服务、不载模型）。

单元用例（test_context_guard.py）用 measure_script 逐次给定量值；这里改由会话内容现算量值：
一条长链里工具每次回大观察、模型每次再调工具，prompt 量测随 history 自然增长，依次穿过
soft → hard → ceiling。压缩把 history 里的观察折成桩，量值随之回落——护栏各档动作之间的
衔接（尾注、应急压缩、二次 ceiling 终止）走的是真实的 run_turn 循环，不是逐档拼出来的。

全量渲染计数：FakeEngine 不经 _assemble_prompt，这里按引擎同一判据模拟（engine.generate_once：
流无效即全量 —— 首次生成、上一次生成带一次性尾注（不提交流）、history 已提交段被改写）。
真引擎上的计数本身由 test_step_compaction.py::test_compaction_forces_full_render_with_stub_and_counts_it 钉住。
"""
import asyncio
import json

from framework.core import context_guard as cg
from framework.core import events
from framework.core.context_guard import PromptMeasure, fallback_estimate
from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import ClassPolicy, TaskScheduler
from framework.core.turn_runner import TurnRunner

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)

# fallback_estimate：ASCII 4 字符 ≈ 1 token。小观察 ≈ 8,500 token，大观察 ≈ 30,000 token
SMALL = "s" * 34_000
HUGE = "h" * 120_000
STUB_MARK = "Observation compacted"


def _events(etype=None):
    p = events.get_path()
    recs = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in recs if etype is None or e["type"] == etype]


def _snapshot(history):
    out = []
    for m in history:
        trs = tuple(str(tr.get("response", "")) for tr in m.get("tool_responses") or [])
        out.append((m.get("role"), str(m.get("content") or ""), trs))
    return out


class ContentMeasureEngine(FakeEngine):
    """量值 = fallback_estimate(会话全部正文 + 观察 + 尾注 + 任务块)；模拟引擎的全量渲染计数。"""

    def __init__(self, script):
        super().__init__(script)
        self.measured = []         # 每次 measure_prompt 的量值
        self.full = []             # 每次生成是否全量渲染
        self._last_view = None     # 上一次生成时的 history 快照
        self._last_tail = False

    def measure_prompt(self, session, tools=None, task_block=None, tail_notes=None,
                       enable_thinking=False):
        super().measure_prompt(session, tools=tools, task_block=task_block,
                               tail_notes=tail_notes, enable_thinking=enable_thinking)
        parts = [c for _r, c, trs in _snapshot(session.history) for c in (c, *trs)]
        parts += list(tail_notes or []) + [task_block or ""]
        n = fallback_estimate("\n".join(parts))
        self.measured.append(n)
        return PromptMeasure(tokens=n, method="chars", chars=0, images=0)

    async def generate_once(self, session, **kw):
        view = _snapshot(session.history)
        prev = self._last_view
        full = prev is None or self._last_tail or view[:len(prev)] != prev
        self.full.append(full)
        if full:
            session.turn_full_renders += 1
        self._last_view, self._last_tail = view, bool(kw.get("tail_notes"))
        return await super().generate_once(session, **kw)


def _script():
    """6 次小观察（第 4 次生成前 soft、第 6 次前 hard）→ 大观察（第 8 次前 ceiling，折叠后回落）
    → 再一次大观察（第 9 次前二次 ceiling，终止）。每次调用参数不同，避开重复调用屏障。"""
    small = [{"tool_calls": [call("read_small", path=f"s{i}")]} for i in range(6)]
    huge = [{"tool_calls": [call("read_huge", path=f"h{i}")]} for i in range(2)]
    return small + huge + [{"text": "不该走到这里"}]


def test_long_chain_crosses_soft_hard_ceiling_and_aborts_with_originals_on_disk(make_session):
    eng = ContentMeasureEngine(_script())
    cfg = {"react_max_steps": 4, "max_total_iterations": 20, "replan_max": 3,
           "observation_max_chars": 200_000, "observation_budget_chars": 10 ** 7,
           "tool_timeout_seconds": 5, "max_parse_retries": 3, "thinking_mode": "manual",
           "goal_verify": False}
    reg = FakeRegistry([FakeTool("read_small", result=SMALL), FakeTool("read_huge", result=HUGE)])
    orch = Orchestrator(eng, reg, cfg)
    orch.bind_context_limit()
    limits = orch.context_limits
    session = make_session()
    runner = TurnRunner(orch, TaskScheduler(policy=ClassPolicy()))

    async def go():
        ctx = await runner.accept_turn(session, session.session_id, prompt="长链任务", images=[],
                                       audios=[], ingested_text="", attachments=[], gen=dict(GEN))
        return await runner.run_accepted(session, session.session_id, ctx)
    r = asyncio.run(go())

    # 走完了整条链：8 次生成，第 9 次在 ceiling 处被拦（后端没收到请求）
    assert len(eng.calls) == 8 and eng.script == [{"text": "不该走到这里"}]
    levels = [cg.classify(n, orch.context_guard, limits) for n in eng.measured]
    assert levels.index("soft") < levels.index("hard") < levels.index("ceiling")

    # soft：只记账（事件一次，不发尾注）
    soft = _events(cg.EVT_SOFT)
    assert len(soft) == 1 and soft[0]["detail"]["level"] == "soft"
    first_soft_gen = soft[0]["detail"]["iteration"] - 1
    assert all(not any(n.startswith("上下文已用 ") for n in c["tail_notes"])
               for c in eng.calls[:first_soft_gen + 1])
    # hard：事件一次、本次生成带尾注；尾注不提交流 → 下一次生成退全量
    hard = _events(cg.EVT_HARD)
    assert len(hard) == 1
    hard_gen = hard[0]["detail"]["iteration"] - 1
    assert hard_gen > first_soft_gen
    assert any(n.startswith("上下文已用 ") for n in eng.calls[hard_gen]["tail_notes"])
    assert eng.full[0] and not any(eng.full[1:hard_gen + 1]) and eng.full[hard_gen + 1]
    # ceiling：首次应急折叠一次（量值回落到 ceiling 以下、照常生成），二次 ceiling 终止
    fold = _events(cg.EVT_FOLD)
    assert len(fold) == 1
    d = fold[0]["detail"]
    assert d["before"] >= limits.ceiling > d["after"] and d["compacted"] >= 1
    assert eng.full[d["iteration"] - 1]                       # 折叠改写已提交段 → 全量
    # TurnRunner 返回面 = 落盘轮次（run_turn 的 metrics 平铺进来）
    assert r["full_renders"] == sum(eng.full) >= 1
    assert r["context_guard_level"] == "ceiling"
    assert r["peak_prompt_tokens"] == max(eng.measured)

    # 用户可见的终止样板（本轮最后一条 assistant 消息）
    assert r["status"] == "warning"
    shown = r["response"][-1]
    assert shown["role"] == "assistant"
    for frag in ("上下文已达模型上限", f"上限 {limits.limit:,} token", "本轮已停止",
                 "①新开会话继续", "②把任务拆小分别发起", "③减少大输出工具的连续调用"):
        assert frag in shown["content"], frag
    aborts = _events("turn_abort")
    assert len(aborts) == 1 and aborts[0]["detail"]["reason"] == cg.ABORT_REASON
    assert aborts[0]["detail"]["prompt_tokens"] >= limits.ceiling

    # model-facing history 里确有桩；history.jsonl 落盘的是原件（无桩）
    live = [tr["response"] for m in session.history if m.get("role") == "tool"
            for tr in m["tool_responses"]]
    assert any(STUB_MARK in x for x in live)
    lines = [json.loads(l) for l in session.history_log.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == 1
    disk = [tr["response"] for m in lines[0]["response"] if m.get("role") == "tool"
            for tr in m["tool_responses"]]
    assert disk == [SMALL] * 6 + [HUGE] * 2
    assert "上下文已达模型上限" in lines[0]["response"][-1]["content"]
