"""W4 摘要校准金测试：轮末 TaskState 进折叠输入（§2）+ 每 K 次折叠一次重写（§3）。

钉住的判据：
1. 轮次记录的 "task_state" 键：任务在 → 现渲染；任务完成即弃 → 编排层弃前留的渲染；无任务
   不写键；作废轮次不写；写完弃前渲染即清空（不串到下一条记录）。审批/挂起放弃路径同样带上。
2. `_turn_to_text` 带 / 不带 task_state、截断、MEMO 拆引信；老记录无该键照常。
3. rewrite 模式只在第 K、2K… 次成功折叠且已有旧摘要时出现；K=0 永不；空摘要不计数；
   折叠计数随 memory.json 往返、老存档缺键读 0；max_tokens 透传；非法配置装配即拒。
"""
import asyncio
import json

import pytest

from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import ClassPolicy, TaskScheduler
from framework.core.task_state import TaskState
from framework.core.turn_runner import TurnRunner
from framework.memory import compactor
from framework.memory.compactor import (SUMMARY_TASK_STATE_CHARS, _collect_turn_texts,
                                        _turn_to_text, maybe_summarize_context,
                                        resolve_summary_calibration)

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)
CFG = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
       "observation_max_chars": 4000, "tool_timeout_seconds": 5,
       "max_parse_retries": 3, "thinking_mode": "manual"}


def _lines(session):
    if not session.history_log.exists():
        return []
    return [json.loads(l) for l in session.history_log.read_text(encoding="utf-8").splitlines() if l]


def _task(goal="整理报表", steps=("读数据", "写汇总"), note=None):
    t = TaskState(goal)
    t.apply_op({"op": "replace", "steps": list(steps)})
    if note:
        t.remember(note)
    return t


# ================= §2 TaskState 入轮次记录 =================

def test_live_task_rendered_into_record(make_session):
    s = make_session()
    s.task_state = _task(note="total = 15330")
    rec = {"status": "success", "prompt": "p", "response": []}
    s.append_log(rec)
    ts = _lines(s)[0]["task_state"]
    assert ts == s.task_state.render_for_summary()
    assert ts == "[Goal] 整理报表\n[Scratchpad]\n- total = 15330\n[Plan]\n1. [pending] 读数据\n2. [pending] 写汇总"
    assert rec["task_state"] == ts          # 与 timestamp 同理盖在原 dict 上（响应/事件同值）


def test_full_task_keeps_all_notes_and_truncates_in_plan(make_session):
    """一审 Major 1：12 步（标题/note 各满 200 字）+ 12 条满载便签——便签全数入折叠输入、
    无系统头 / Rules 样板，3000 字截断落在 Plan 段。"""
    t = TaskState("目标" * 400)
    t.apply_op({"op": "replace", "steps": [f"步骤{i:02d}" + "题" * 190 for i in range(12)]})
    notes = [f"KEY{i:02d}=" + "值" * 190 for i in range(12)]
    for n in notes:
        t.remember(n)
    for _ in range(11):
        t.complete_current("结果" * 100)
    assert len(t.render(4, 3)) > 4000                        # 旧口径：prompt 尾部块远超上限
    s = make_session()
    s.task_state = t
    s.append_log({"status": "success", "prompt": "p", "response": []})
    ts = _lines(s)[0]["task_state"]
    assert "system-maintained" not in ts and "Rules" not in ts and "Budget" not in ts
    for n in notes:
        assert n in ts
    text = _turn_to_text(_lines(s)[0])
    block = text.split("[Task state at end of turn]\n", 1)[1]
    assert len(block) == SUMMARY_TASK_STATE_CHARS + 1 and block.endswith("…")
    assert all(n in block for n in notes)                    # 截断后便签仍全在
    assert block.index("[Plan]") < len(block) - 1             # 截断点在 Plan 段内
    assert block.startswith("[Goal] " + "目标" * 150 + "…")    # 目标首行 ≤300 字


def test_no_task_no_key_and_render_cleared(make_session):
    s = make_session()
    s.append_log({"status": "success", "prompt": "p", "response": []})
    s.last_task_state_render = "[Current task state | x]"
    s.append_log({"status": "success", "prompt": "q", "response": []})
    s.append_log({"status": "success", "prompt": "r", "response": []})
    recs = _lines(s)
    assert "task_state" not in recs[0]
    assert recs[1]["task_state"] == "[Current task state | x]"
    assert "task_state" not in recs[2]              # 弃前渲染只属于紧随其后的一条
    assert s.last_task_state_render == ""


@pytest.mark.parametrize("status", ["fail", "cancelled"])
def test_void_turns_not_stamped(make_session, status):
    s = make_session()
    s.task_state = _task()
    s.last_task_state_render = "stale"
    s.append_log({"status": status, "prompt": "p", "response": []})
    assert "task_state" not in _lines(s)[0]
    assert s.last_task_state_render == ""


def test_task_completion_path_records_before_drop(make_session):
    """编排层「任务完成即弃」：置 None 前留下渲染（含步骤 note 与记事板），随下一条记录落盘。"""
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["拿到地址", "取数"])]},
        {"tool_calls": [call("remember", note="API = https://api.example.com/v2"),
                        call("complete_step", result="地址已拿到")]},
        {"tool_calls": [call("complete_step", result="取数完成 42 行")]},
        {"text": "全部完成。"},
    ])
    orch = Orchestrator(engine, FakeRegistry([]), dict(CFG, react_max_steps=5))
    s = make_session()
    s.add_user_message("两步任务")
    s.journal.emit("turn_queued", {"prompt": "两步任务", "attachments": [], "gen": dict(GEN),
                                   "origin": "chat"})
    s.journal.emit("turn_started", {})
    result = asyncio.run(orch.run_turn(s, journal=s.journal, **GEN))
    assert result["status"] == "success"
    assert s.task_state is None
    assert s.last_task_state_render                       # 弃前已渲染
    s.append_log({"status": "success", "prompt": "两步任务", "response": []})
    ts = _lines(s)[0]["task_state"]
    for frag in ("[Scratchpad]\n- API = https://api.example.com/v2", "取数完成 42 行", "地址已拿到",
                 "2. [done] 取数"):
        assert frag in ts, frag
    assert s.last_task_state_render == ""


def test_turn_runner_abandoned_suspended_turn_carries_task_state(make_session):
    """挂起轮被新消息放弃：warning 记录带被放弃任务的状态；新一轮（无任务）不带。"""
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["读", "写"])]},
        {"tool_calls": [call("read_thing")]},
        {"text": "新一轮答复。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing")])
    runner = TurnRunner(Orchestrator(engine, registry, CFG), TaskScheduler(policy=ClassPolicy()))
    s = make_session()

    async def submit(prompt):
        ctx = await runner.accept_turn(s, "t-session", prompt=prompt, images=[], audios=[],
                                       ingested_text="", attachments=[], gen=dict(GEN))
        return await runner.run_accepted(s, "t-session", ctx)

    async def scenario():
        async def flag():
            while not registry.executed:
                await asyncio.sleep(0)
            s.suspend_requested = True
        ft = asyncio.create_task(flag())
        rd = await submit("长任务")
        await ft
        assert rd["status"] == "suspended" and s.task_state is not None
        rd2 = await submit("换个话题")
        assert rd2["status"] == "success"

    asyncio.run(scenario())
    recs = _lines(s)
    assert [r["status"] for r in recs] == ["warning", "success"]
    assert recs[0]["task_state"].startswith("[Goal] 长任务")
    assert "1. [doing] 读" in recs[0]["task_state"]
    assert "task_state" not in recs[1]


def test_turn_to_text_with_and_without_task_state():
    base = {"prompt": "hi", "response": [{"role": "assistant", "content": "ok"}]}
    assert "[Task state at end of turn]" not in _turn_to_text(base)    # 老记录无该键
    out = _turn_to_text({**base, "task_state": "Goal: g\nPlan:\n  ✓ [1] a — /tmp/x"})
    assert out.endswith("[Task state at end of turn]\nGoal: g\nPlan:\n  ✓ [1] a — /tmp/x")
    assert _turn_to_text({**base, "task_state": ""}) == _turn_to_text(base)


def test_turn_to_text_truncates_task_state():
    long = "K" * (SUMMARY_TASK_STATE_CHARS + 500)
    out = _turn_to_text({"prompt": "hi", "response": [], "task_state": long})
    block = out.split("[Task state at end of turn]\n", 1)[1]
    assert block == "K" * SUMMARY_TASK_STATE_CHARS + "…"


def test_turn_to_text_defuses_memo_in_task_state():
    out = _turn_to_text({"prompt": "hi", "response": [], "task_state": "notes\nMEMO:\n- x | user | y"})
    assert "\nMEMO:\n" not in out and "[quoted] MEMO:" in out


def test_collect_texts_old_records_and_sediment_shares_text():
    lines = [json.dumps({"prompt": "old", "response": []}),
             json.dumps({"prompt": "new", "response": [], "task_state": "Goal: g"})]
    texts = _collect_turn_texts(lines)
    assert texts[0] == "User: old"
    assert texts[1].endswith("[Task state at end of turn]\nGoal: g")


# ================= §3 每 K 次折叠一次重写 =================

class _ModeEngine:
    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = list(outputs or [])

    async def summarize(self, content, prior_summary="", max_tokens=640, temperature=None,
                        cancel_check=None, *, mode="merge", instruction=None):
        self.calls.append({"mode": mode, "max_tokens": max_tokens, "prior": prior_summary,
                           "instruction": instruction, "content": content})
        return self.outputs.pop(0) if self.outputs else f"- 摘要 {len(self.calls)}"


def _fold_n(session, engine, n, **kw):
    for i in range(n):
        session.append_log({"status": "success", "prompt": f"问{i}",
                            "response": [{"role": "assistant", "content": f"答{i}"}]})
        asyncio.run(maybe_summarize_context(
            session, engine, threshold_tokens=1, keep_tail_turns=0,
            last_prompt_tokens=10_000, sediment=False, **kw))


def test_rewrite_every_4(make_session):
    s, eng = make_session(), _ModeEngine()
    _fold_n(s, eng, 8, rewrite_every=4)
    assert [c["mode"] for c in eng.calls] == ["merge"] * 3 + ["rewrite"] + ["merge"] * 3 + ["rewrite"]
    assert s.summary_folds == 8


def test_rewrite_every_0_never(make_session):
    s, eng = make_session(), _ModeEngine()
    _fold_n(s, eng, 5, rewrite_every=0)
    assert {c["mode"] for c in eng.calls} == {"merge"}


def test_first_fold_without_summary_is_merge_even_k1(make_session):
    s, eng = make_session(), _ModeEngine()
    _fold_n(s, eng, 2, rewrite_every=1)
    assert [c["mode"] for c in eng.calls] == ["merge", "rewrite"]
    assert eng.calls[0]["prior"] == ""


def test_empty_summary_does_not_count(make_session):
    s, eng = make_session(), _ModeEngine(outputs=["- 第一次", ""])
    _fold_n(s, eng, 2, rewrite_every=4)
    assert s.summary_folds == 1
    assert s.rolling_summary == "- 第一次"


def test_all_void_batch_does_not_count(make_session):
    s, eng = make_session(), _ModeEngine()
    s.append_log({"status": "fail", "prompt": "x", "response": []})
    n = asyncio.run(maybe_summarize_context(s, eng, threshold_tokens=1, keep_tail_turns=0,
                                            last_prompt_tokens=10_000, sediment=False))
    assert n == 1 and eng.calls == [] and s.summary_folds == 0


def test_max_tokens_and_instruction_passthrough(make_session):
    s, eng = make_session(), _ModeEngine()
    _fold_n(s, eng, 1, max_tokens=900, instruction="LEGACY-X")
    assert eng.calls[0]["max_tokens"] == 900 and eng.calls[0]["instruction"] == "LEGACY-X"
    s2, eng2 = make_session("other"), _ModeEngine()
    _fold_n(s2, eng2, 1)
    assert eng2.calls[0]["max_tokens"] == 640 and eng2.calls[0]["instruction"] is None


def test_summary_folds_memory_roundtrip(make_session):
    s, eng = make_session(), _ModeEngine()
    _fold_n(s, eng, 3)
    data = json.loads(s.memory_file.read_text(encoding="utf-8"))
    assert data["summary_folds"] == 3
    s.summary_folds = 0
    s.load_memory()
    assert s.summary_folds == 3


def test_old_memory_json_without_folds_reads_zero(make_session):
    s = make_session()
    s.append_log({"status": "success", "prompt": "p", "response": []})
    s.memory_file.write_text(json.dumps({"summary": "- 旧摘要", "summarized_turns": 1,
                                         "sedimented_turns": 1, "updated_at": 0}),
                             encoding="utf-8")
    s.summary_folds = 9
    s.load_memory()
    assert s.summary_folds == 0 and s.rolling_summary == "- 旧摘要"


@pytest.mark.parametrize("cfg", [
    {"summary_max_tokens": 127}, {"summary_max_tokens": 4097}, {"summary_max_tokens": True},
    {"summary_max_tokens": "640"}, {"summary_max_tokens": 640.0},
    {"summary_rewrite_every": -1}, {"summary_rewrite_every": False},
    {"summary_rewrite_every": 1.5}, {"summary_rewrite_every": "4"},
])
def test_invalid_calibration_rejected_at_assembly(cfg):
    with pytest.raises(ValueError):
        resolve_summary_calibration(cfg)
    with pytest.raises(ValueError):
        Orchestrator(FakeEngine([]), FakeRegistry([]), dict(CFG, **cfg))


def test_calibration_defaults_and_bounds():
    assert resolve_summary_calibration({}) == (900, 4)       # 缺省同 config.yaml（一审 Major 3）
    assert resolve_summary_calibration(None) == (900, 4)
    assert resolve_summary_calibration({"summary_max_tokens": 128, "summary_rewrite_every": 0}) == (128, 0)
    assert resolve_summary_calibration({"summary_max_tokens": 4096}) == (4096, 4)
    orch = Orchestrator(FakeEngine([]), FakeRegistry([]),
                        dict(CFG, summary_max_tokens=900, summary_rewrite_every=2))
    assert (orch.summary_max_tokens, orch.summary_rewrite_every) == (900, 2)


def test_active_path_passes_orchestrator_calibration(make_session, monkeypatch):
    """routes 活跃高阈值路径把编排器的两个校准值透传给 maybe_summarize_context。"""
    from framework.api import routes
    seen = {}

    async def fake_msc(session, engine, **kw):
        seen.update(kw)
        return 0
    monkeypatch.setattr(routes, "maybe_summarize_context", fake_msc)

    class _Sched:
        async def acquire(self, *a, **k):
            pass

        async def release(self, *a, **k):
            pass

    orch = Orchestrator(FakeEngine([]), FakeRegistry([]),
                        dict(CFG, summary_max_tokens=777, summary_rewrite_every=3))
    s = make_session()
    s.last_prompt_tokens = orch.summary_high_threshold + 1
    asyncio.run(routes._maybe_summarize_active(s, orch, _Sched(), "t-session"))
    assert seen["max_tokens"] == 777 and seen["rewrite_every"] == 3


def test_idle_path_passes_orchestrator_calibration(make_session, monkeypatch):
    """空闲低阈值路径（app._idle_maintenance_loop）同样透传：跑一回合，摘要调用处记下入参后取消循环。"""
    import time

    import app
    seen = {}

    async def fake_msc(session, engine, **kw):
        seen.update(kw)
        raise asyncio.CancelledError      # 记完即停：循环以 CancelledError 为退出信号
    monkeypatch.setattr(app, "maybe_summarize_context", fake_msc)

    class _Sched:
        def is_active(self, sid):
            return False

        async def acquire(self, *a, **k):
            pass

        async def release(self, *a, **k):
            pass

    orch = Orchestrator(FakeEngine([]), FakeRegistry([]),
                        dict(CFG, summary_max_tokens=555, summary_rewrite_every=6))
    s = make_session()
    s.is_cold = False
    s.last_access_time = time.time() - orch.summary_idle_seconds - 10
    s.last_prompt_tokens = orch.summary_low_threshold + 1

    class _Mgr:
        _all_sessions = {"t-session": s}

    # 循环把 CancelledError 当退出信号吞掉、正常返回（app.py 的循环尾）
    asyncio.run(asyncio.wait_for(
        app._idle_maintenance_loop(_Mgr(), _Sched(), orch, interval=0), timeout=10))
    assert seen["max_tokens"] == 555 and seen["rewrite_every"] == 6


def test_summary_mode_helper():
    class S:
        summary_folds = 3
        rolling_summary = "x"
    assert compactor.summary_mode(S, 4) == "rewrite"
    S.rolling_summary = "  "
    assert compactor.summary_mode(S, 4) == "merge"
    S.rolling_summary = "x"
    assert compactor.summary_mode(S, 0) == "merge"
