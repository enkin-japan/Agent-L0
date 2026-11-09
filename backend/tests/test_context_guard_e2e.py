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

import pytest

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
    """量值 = fallback_estimate(会话全部正文 + 观察 + 尾注 + 任务块)；模拟引擎的全量渲染计数。
    density（D48b）缺省 None = 上述 chars 量法、不报密度；给了则模拟分词器实测 = round(density × 估算)、
    method="tokenizer"、PromptMeasure.density=density（真引擎口径：实测 ÷ fallback_estimate）。"""

    def __init__(self, script, density=None):
        super().__init__(script)
        self.density = density
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
        if self.density is None:
            self.measured.append(n)
            return PromptMeasure(tokens=n, method="chars", chars=0, images=0)
        n = round(self.density * n)
        self.measured.append(n)
        return PromptMeasure(tokens=n, method="tokenizer", chars=0, images=0, density=self.density)

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


# ---- fold_mode=minimal（W1 修订 2026-11-09：hard 提到 ceiling 附近，折到刚好够用、需要几次折几次） ----
# 观察 ≈ 12,000 token（fallback_estimate：ASCII 4 字符 ≈ 1 token）；hard 51,200、ceiling 53,248
OBS12K = 48_000
HARD_NEAR_CEILING = 51_200


def _prefilled_run(make_session, *, mode, n_prefill, script, tool_results, obs_max=200_000,
                   obs_chars=OBS12K, density=None):
    """history 里预铺 n_prefill 条 obs_chars 字符（缺省 ≈12K token）的旧观察（内容互异：a/b/c…），再跑
    一轮 script。tool_results：{工具名: 返回正文}；density 透传 ContentMeasureEngine。
    返回 (结果, 引擎, 会话, 编排器, 预铺观察原文)。"""
    eng = ContentMeasureEngine(script, density=density)
    cfg = {"react_max_steps": 4, "max_total_iterations": 20, "replan_max": 3,
           "observation_max_chars": obs_max, "observation_budget_chars": 10 ** 7,
           "tool_timeout_seconds": 5, "max_parse_retries": 3, "thinking_mode": "manual",
           "goal_verify": False,
           "context_guard": {"hard_tokens": HARD_NEAR_CEILING, "fold_mode": mode}}
    reg = FakeRegistry([FakeTool(n, result=t) for n, t in tool_results.items()])
    orch = Orchestrator(eng, reg, cfg)
    orch.bind_context_limit()
    assert orch.context_guard.fold_mode == mode and orch.context_limits.ceiling == 53_248
    session = make_session()
    originals = []
    for i in range(n_prefill):
        text = "abcdefgh"[i] * obs_chars
        originals.append(text)
        session.add_user_message(f"旧问 {i}")
        session.add_assistant_message(tool_calls=[call("read_old", path=f"o{i}")])
        session.add_tool_message([{"name": "read_old", "response": text}])
        session.add_assistant_message(content=f"旧答 {i}")
    runner = TurnRunner(orch, TaskScheduler(policy=ClassPolicy()))

    async def go():
        ctx = await runner.accept_turn(session, session.session_id, prompt="继续", images=[],
                                       audios=[], ingested_text="", attachments=[], gen=dict(GEN))
        return await runner.run_accepted(session, session.session_id, ctx)
    return asyncio.run(go()), eng, session, orch, originals


def _live_obs(session):
    return [tr["response"] for m in session.history if m.get("role") == "tool"
            for tr in m["tool_responses"]]


def test_minimal_fold_first_prompt_over_ceiling_folds_only_oldest(make_session):
    r, eng, session, orch, orig = _prefilled_run(
        make_session, mode="minimal", n_prefill=5, script=[{"text": "完成"}],
        tool_results={"read_old": "x"})
    limits = orch.context_limits
    assert eng.measured[0] >= limits.ceiling                       # 首 prompt 即到 ceiling
    assert r["status"] == "success" and len(eng.calls) == 1        # 折后照常生成
    fold = _events(cg.EVT_FOLD)
    assert len(fold) == 1
    d = fold[0]["detail"]
    # 场景确定：5 条预铺观察（keep_recent=2 窗外 3 条候选）+ 固定内容长度 → 首批即把窗外最旧
    # 1 条折完就够（够量：60,085 → 48,162 < hard 51,200），精确值而非下界
    assert d["mode"] == "minimal" and d["passes"] == 1 and d["compacted"] == 1
    assert d["before"] >= limits.ceiling and d["after"] < HARD_NEAR_CEILING
    assert eng.measured[-1] == d["after"]                          # 发出去的就是折后量到的那份
    live = _live_obs(session)
    assert STUB_MARK in live[0] and len(live[0]) < len(orig[0])    # 只折最旧
    assert live[1:3] == orig[1:3]                                  # 更新的非窗内观察仍是原文
    assert live[3:] == orig[3:]                                    # 保护窗（最近 2 条）原文
    assert _events("turn_abort") == []


def _second_ceiling_script():
    return [{"tool_calls": [call("read_huge", path="n1")]}, {"text": "完成"}]


def test_minimal_fold_refolds_on_second_ceiling_where_full_aborts(make_session):
    # minimal：首次 ceiling 折最旧 1 条；大观察回灌后同一轮再到 ceiling → 再折（第二个 fold 事件）
    r, eng, session, orch, orig = _prefilled_run(
        make_session, mode="minimal", n_prefill=5, script=_second_ceiling_script(),
        tool_results={"read_old": "x", "read_huge": HUGE})
    ceiling = orch.context_limits.ceiling
    assert r["status"] == "success" and len(eng.calls) == 2 and eng.script == []
    folds = [e["detail"] for e in _events(cg.EVT_FOLD)]
    assert len(folds) == 2 and all(f["mode"] == "minimal" for f in folds)
    assert folds[0]["iteration"] == 1 and folds[1]["iteration"] == 2
    assert all(f["before"] >= ceiling and f["after"] < HARD_NEAR_CEILING for f in folds)
    # 场景确定：第二次 ceiling 时窗外候选 = orig[1..3]（orig[0] 已折、orig[4]/HUGE 在保护窗内）；
    # HUGE 把量值推到 78,163，一批凑足缺口须吞下全部 3 条候选才够（78,163 → 42,394 < hard），精确值
    assert folds[0]["compacted"] == 1 and folds[1]["compacted"] == 3
    assert _events("turn_abort") == []
    live = _live_obs(session)
    assert live[-2:] == [orig[4], HUGE]                            # 保护窗原文
    folded = [STUB_MARK in x for x in live[:-2]]
    assert folded[0] and folded == sorted(folded, reverse=True)   # 折的恒是最旧的一段前缀


def test_full_fold_second_ceiling_in_same_scenario_aborts(make_session):
    r, eng, session, orch, orig = _prefilled_run(
        make_session, mode="full", n_prefill=5, script=_second_ceiling_script(),
        tool_results={"read_old": "x", "read_huge": HUGE})
    assert r["status"] == "warning" and len(eng.calls) == 1        # 第二次 ceiling 被拦
    folds = [e["detail"] for e in _events(cg.EVT_FOLD)]
    assert len(folds) == 1 and folds[0]["mode"] == "full" and folds[0]["passes"] == 1
    assert folds[0]["compacted"] == 3                              # 保护窗外一次全折
    aborts = _events("turn_abort")
    assert len(aborts) == 1 and aborts[0]["detail"]["reason"] == cg.ABORT_REASON
    assert aborts[0]["detail"]["compacted"] == 3


def test_minimal_fold_candidates_exhausted_aborts_with_cumulative_count(make_session):
    # 第 1 次生成前折 1 条；回灌 ≈60K token 的观察后窗外候选全折仍 ≥ ceiling → 终止
    giant = "g" * 240_000
    r, eng, session, orch, orig = _prefilled_run(
        make_session, mode="minimal", n_prefill=5, obs_max=10 ** 6,
        script=[{"tool_calls": [call("read_giant", path="n1")]}, {"text": "不该走到这里"}],
        tool_results={"read_old": "x", "read_giant": giant})
    ceiling = orch.context_limits.ceiling
    assert r["status"] == "warning" and len(eng.calls) == 1
    assert "上下文已达模型上限" in r["response"][-1]["content"]
    folds = [e["detail"] for e in _events(cg.EVT_FOLD)]
    assert len(folds) == 1 and folds[0]["compacted"] == 1          # 第二次折完仍 ceiling：无 fold 事件
    aborts = _events("turn_abort")
    assert len(aborts) == 1 and aborts[0]["detail"]["reason"] == cg.ABORT_REASON
    assert aborts[0]["detail"]["prompt_tokens"] >= ceiling
    assert aborts[0]["detail"]["compacted"] == 1 + 3                # 两次 ceiling 累计（非覆盖）
    live = _live_obs(session)
    assert all(STUB_MARK in x for x in live[:-2]) and live[-2:] == [orig[4], giant]


# ---- D48b：minimal 候选可省量按实测密度校准（量值带 density 的分词器口径） ----
def test_minimal_fold_density_calibrated_folds_fewer(make_session):
    """估算偏低 2×（density 2.0，d48 批实测填充正文约 2.2×）：预铺 5 条 24,000 字符观察，每条估 6,000、
    实测 12,000。折前实测 ≈ 2 × 30,085 ≈ 60,170 ≥ ceiling，缺口 ≈ 60,170 − 51,200 + 1 ≈ 8,971。
      校准：每条可省 round(2 × (6,000 − 50 − 80)) = 11,740 ≥ 缺口 → 1 批 1 条，折后 ≈ 48.3K < hard。
      不校准（系数 1）：每条 round(6,000 − 50 − 80) = 5,870 < 缺口 → 首批凑 2 条（11,740）
      ——D48b 的「超折」，多折一条。"""
    r, eng, session, orch, orig = _prefilled_run(
        make_session, mode="minimal", n_prefill=5, script=[{"text": "完成"}],
        tool_results={"read_old": "x"}, obs_chars=24_000, density=2.0)
    limits = orch.context_limits
    assert eng.measured[0] >= limits.ceiling
    assert eng.measured[0] - HARD_NEAR_CEILING + 1 < 12_000        # 场景前提：缺口小于一条的实测量
    assert r["status"] == "success" and len(eng.calls) == 1
    fold = _events(cg.EVT_FOLD)
    assert len(fold) == 1
    d = fold[0]["detail"]
    assert d["mode"] == "minimal" and d["compacted"] == 1 and d["passes"] == 1
    assert d["density"] == pytest.approx(2.0)
    assert d["density_raw"] == pytest.approx(2.0) and d["method"] == "tokenizer"   # Minor 3
    assert d["after"] < HARD_NEAR_CEILING and eng.measured[-1] == d["after"]
    live = _live_obs(session)
    assert STUB_MARK in live[0]
    assert live[1] == orig[1]                                      # 第二旧的观察未被折（不超折）
    assert live[2:] == orig[2:]


def test_minimal_fold_density_below_one_folds_enough_in_one_pass(make_session):
    """估算偏高（density 0.5，例如 CJK 一字一估而实测更省）：预铺 6 条 96,000 字符观察，每条估
    24,000、实测 12,000。折前实测 ≈ 0.5 × (144,000 + ~100) ≈ 72,05x，缺口 ≈ 20,85x（介于 1 条与 2 条
    实测量之间）。
      校准：每条可省 round(0.5 × (24,000 − 50 − 80)) = 11,935 → 首批凑 2 条（23,870 ≥ 缺口），1 批到 < hard。
      不校准：每条 24,000 − 50 − 80 = 23,870 ≥ 缺口 → 首批只折 1 条，实测只省 ≈12K 仍 ≥ hard
      → 第 2 批补 1 条（2 批）。"""
    r, eng, session, orch, orig = _prefilled_run(
        make_session, mode="minimal", n_prefill=6, script=[{"text": "完成"}],
        tool_results={"read_old": "x"}, obs_chars=96_000, density=0.5)
    gap = eng.measured[0] - HARD_NEAR_CEILING + 1
    assert 12_000 < gap < 2 * 11_935                                # 场景前提
    assert r["status"] == "success" and len(eng.calls) == 1
    fold = _events(cg.EVT_FOLD)
    assert len(fold) == 1
    d = fold[0]["detail"]
    assert d["mode"] == "minimal" and d["compacted"] == 2 and d["passes"] == 1
    assert d["density"] == pytest.approx(0.5)
    assert d["after"] < HARD_NEAR_CEILING
    live = _live_obs(session)
    assert [STUB_MARK in x for x in live] == [True, True, False, False, False, False]
    assert live[2:] == orig[2:]


def test_minimal_fold_without_density_reports_unit_coefficient(make_session):
    """chars 量法（density 未知）：系数 1.0，行为同 D48b 前；事件里 density 记 1.0、density_raw 记
    None、method 记 "chars"（Minor 3：量值本身的原始口径，与校准系数区分开）；full 记 None。"""
    _prefilled_run(make_session, mode="minimal", n_prefill=5, script=[{"text": "完成"}],
                   tool_results={"read_old": "x"})
    (d,) = [e["detail"] for e in _events(cg.EVT_FOLD)]
    assert d["density"] == 1.0 and d["density_raw"] is None and d["method"] == "chars"


def test_full_fold_event_density_is_none(make_session):
    _prefilled_run(make_session, mode="full", n_prefill=5, script=[{"text": "完成"}],
                   tool_results={"read_old": "x"}, density=2.0, obs_chars=24_000)
    (d,) = [e["detail"] for e in _events(cg.EVT_FOLD)]
    assert d["mode"] == "full" and d["density"] is None
    assert d["density_raw"] is None and d["method"] is None       # Minor 3：full 恒不写
