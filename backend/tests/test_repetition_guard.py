"""流式复读护栏金测试（core/repetition_guard + engine 接线 + 编排层推回/终止）。

检测器：必须命中 / 必须不命中两组夹具逐条钉死（判宽了会把正常表格、JSON 工具调用、
缩进代码误杀成复读；判窄了放过整屏 `123.45 123.45 …`）。引擎：命中即停止消耗生成流
（消耗的块数少于后端提供的块数）、终态 repetition_guard、事件流落 repetition 记录。
编排层：本轮首次命中 → 一次性尾注推回并继续；第二次 → 终止本轮并落 turn_abort。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from framework.core import events
from framework.core.journal import new_turn_id
from framework.core.orchestrator import Orchestrator
from framework.core.repetition_guard import RepetitionDetector

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

# ---------- 夹具 ----------

PROSE = (
    "The quarterly review covered three areas. Revenue grew in the northern region, "
    "mostly because two large contracts closed earlier than planned, while the southern "
    "region stayed flat after a slow spring. Management expects the second half to be "
    "steadier, although supply costs remain a concern for the hardware line.\n\n"
    "On the product side, the team shipped the new onboarding flow and retired the legacy "
    "billing screens. Support tickets about password resets dropped by roughly a third in "
    "the month after launch, and the mobile app finally reached feature parity with the "
    "desktop client for invoice exports.\n\n"
    "For next quarter the plan is to consolidate vendors, hire two engineers for the data "
    "platform, and run a pricing experiment in one market before rolling anything out "
    "widely. Each of these items has an owner and a review date, and progress will be "
    "reported in the monthly operations meeting rather than in separate status emails."
)

TABLE = "| 月份 | 营收（万元） | 成本（万元） | 毛利率 | 同比 |\n|---|---|---|---|---|\n" + "".join(
    f"| 2025-{m:02d} | {1200 + m * 37.5:,.1f} | {800 + m * 21.25:,.2f} | {30 + m * 0.7:.1f}% "
    f"| {(m * 7.3) % 19 - 4:+.1f}% |\n"
    for m in range(1, 13))

JSON_CALL = json.dumps({"name": "write_rows", "arguments": {"rows": [
    {"id": i, "name": f"item_{i}", "value": i * 13 % 97, "unit": "kg"} for i in range(1, 41)]}},
    ensure_ascii=False, indent=None).replace("}, {", "},\n    {")

CODE = "def compute_all(data):\n    results = []\n" + "".join(
    f"    value_{i} = transform(data[{i}], scale={i % 5 + 1})\n"
    f"    if value_{i} > threshold_{i % 3}:\n"
    f"        results.append((\"{chr(97 + i % 26)}{i}\", value_{i}))\n"
    for i in range(14)) + "    return results\n"

DIGITS = "123.45 " * 200
SAME_LINES = "营收 1,234 万元\n" * 12
ZERO_MATRIX = "[0, 0, 0, 0, 0, 0, 0, 0, 0, 0],\n" * 10
BLANK_TABLE = "|      |      |      |\n" * 20
NA_ROWS = "| N/A | N/A | N/A | N/A |\n" * 7
PHRASE = "这个数据需要进一步核实一下然后再继续往下分析，我们先看看上一季度的情况。"[:30] * 40


def _feed(det, channel, text, chunk=7):
    for i in range(0, len(text), chunk):
        hit = det.feed(channel, text[i:i + chunk])
        if hit is not None:
            return hit
    return None


# ---------- 检测器单测 ----------

def test_fixture_sizes_are_meaningful():
    """夹具本身要够长才算数：短于 min_chars 的「不命中」什么也没证明。"""
    assert len(PHRASE) == 1200 and len(PHRASE[:30]) == 30
    for text in (PROSE, TABLE, JSON_CALL, CODE):
        assert len(text) >= 600, len(text)
    assert JSON_CALL.count("\n") >= 39 and JSON_CALL.count('"unit"') == 40


@pytest.mark.parametrize("name,text", [("prose", PROSE), ("table", TABLE),
                                       ("json_tool_call", JSON_CALL), ("code", CODE),
                                       ("zero_matrix", ZERO_MATRIX), ("blank_table", BLANK_TABLE),
                                       ("na_filler_rows", NA_ROWS)])
@pytest.mark.parametrize("chunk", [1, 7, 10_000])
def test_must_not_fire(name, text, chunk):
    for channel in ("content", "thinking"):
        assert _feed(RepetitionDetector(), channel, text, chunk) is None, (name, channel)


@pytest.mark.parametrize("text,reason", [(DIGITS, "ngram_dup"), (SAME_LINES, "line_repeat"),
                                         (PHRASE, "ngram_dup")])
@pytest.mark.parametrize("chunk", [1, 7, 10_000])
def test_must_fire(text, reason, chunk):
    hit = _feed(RepetitionDetector(), "content", text, chunk)
    assert hit is not None and hit.channel == "content" and hit.reason == reason
    assert 0 < len(hit.sample) <= 120


def test_line_repeat_fires_exactly_at_the_threshold():
    det = RepetitionDetector(line_repeats=6)
    for _ in range(5):
        assert det.feed("content", "营收 1,234 万元\n") is None
    hit = det.feed("content", "营收 1,234 万元\n")
    assert hit is not None and hit.reason == "line_repeat" and hit.sample == "营收 1,234 万元"


def test_ngram_threshold_is_inclusive_and_min_chars_respected():
    det = RepetitionDetector()
    # 不到 min_chars 的复读不判（判据 a），即便占比已经很高
    assert det.feed("content", "ab" * 290) is None
    assert det._dup_ratio("x" * 100) == pytest.approx(84 / 85)
    # 占比恰好等于阈值也算命中（>=）：把阈值设成这段文本的实测占比
    w = DIGITS[:2000]
    exact = RepetitionDetector(max_dup_ratio=det._dup_ratio(w))
    assert exact.feed("content", w) is not None


def test_channel_buffers_are_independent():
    """思考与正文各自一份缓冲：分摊在两个频道里的同一行，各自都没到阈值，不得合并计数。"""
    det = RepetitionDetector(line_repeats=6)
    line = "营收 1,234 万元\n"
    for _ in range(3):
        assert det.feed("thinking", line) is None
        assert det.feed("content", line) is None
    hit = None
    for _ in range(3):
        hit = det.feed("thinking", line) or hit
    assert hit is not None and hit.channel == "thinking"


def test_thinking_channel_hit_reports_thinking():
    hit = _feed(RepetitionDetector(), "thinking", DIGITS)
    assert hit is not None and hit.channel == "thinking"


def test_disabled_config_builds_no_detector_and_defaults_apply():
    assert RepetitionDetector.from_config({"enabled": False}) is None
    det = RepetitionDetector.from_config({})
    assert (det.window_chars, det.min_chars, det.ngram, det.max_dup_ratio, det.line_repeats) \
        == (2000, 600, 16, 0.80, 10)
    det = RepetitionDetector.from_config({"ngram": "bogus", "line_repeats": 3})
    assert det.ngram == 16 and det.line_repeats == 3
    # YAML 里手写的 "false"/0/off 也算关；占比出 (0, 1] 回落缺省（0 会让一切命中）
    for off in ("false", "off", 0, "0", "No"):
        assert RepetitionDetector.from_config({"enabled": off}) is None, off
    for bad in (0, -0.5, 1.5, float("nan")):
        assert RepetitionDetector.from_config({"max_dup_ratio": bad}).max_dup_ratio == 0.80, bad


# ---------- 引擎接线 ----------

class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return list(range(len(s)))


class _CountingBackend:
    """逐块吐出给定文本；记录消费了多少块、生成器是否被关闭（取消路径同款）。"""

    def __init__(self, pieces):
        self.processor = SimpleNamespace(tokenizer=_Tok())
        self.pieces = list(pieces)
        self.consumed = 0
        self.closed = False

    def stream_generate(self, prompt, **kwargs):
        try:
            for i, p in enumerate(self.pieces):
                self.consumed += 1
                yield SimpleNamespace(text=p, delta=None, prompt_tokens=3,
                                      generation_tokens=i + 1, prompt_tps=1.0,
                                      generation_tps=1.0, peak_memory=0.0, extra={})
        finally:
            self.closed = True


def _engine(backend, dialect="gemma4"):
    from framework.core.engine import AgentEngine
    return AgentEngine(backend, kv_config={}, model_options={"dialect": dialect})


def _session():
    return SimpleNamespace(history=[{"role": "system", "content": "s"},
                                    {"role": "user", "content": "hi"}],
                           images=[], audios=[], brain_memory_state=None,
                           cancel_requested=False, inject_env_context=False,
                           prompt_stream=None, session_id="rep-s",
                           journal=SimpleNamespace(current_turn_id="turn-1"))


def _events():
    p = events.get_path()
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_engine_stops_a_repeating_content_stream(capsys):
    be = _CountingBackend(["123.45 "] * 400)
    deltas = []
    r = asyncio.run(_engine(be).generate_once(_session(), tools=None,
                                              on_text_delta=deltas.append))
    assert r["repetition_guard"] == {"channel": "content", "reason": "ngram_dup"}
    assert r["cancelled"] is False and r["text"] == "" and r["tool_calls"] == []
    assert be.consumed < len(be.pieces) and be.consumed < 200
    assert be.closed, "提前离场必须关闭生成器（远程后端据此发 cancel 并断开 SSE）"
    out = capsys.readouterr().out
    assert f"[Guard] 复读输出 channel=content reason=ngram_dup gen={be.consumed} tokens" in out
    rec = [e for e in _events() if e["type"] == "repetition"]
    assert len(rec) == 1 and rec[0]["session"] == "rep-s"
    assert rec[0]["detail"] == {"channel": "content", "reason": "ngram_dup", "turn": "turn-1"}


def test_engine_detects_without_streaming_callbacks():
    """没有直播回调（沙箱/审计等直调路径）护栏照样生效。"""
    be = _CountingBackend(["营收 1,234 万元\n"] * 50)
    r = asyncio.run(_engine(be).generate_once(_session(), tools=None))
    assert r["repetition_guard"]["reason"] == "line_repeat"
    assert be.consumed == 10


def test_engine_guards_text_inside_a_tool_call_block():
    """工具调用块内的复读也算 content：qwen35 失控工具参数在本部署上没有约束采样兜底
    （runaway 拦截依赖 constrained_steps）。切掉 on_tool_text 接线 → 这里的块内文本永远到
    不了检测器、50 块全部消费完。"""
    probe = _engine(_CountingBackend([]))
    stc = probe.dialect.markers.stc
    be = _CountingBackend([stc + 'write_file{"content": "'] + ["营收 1,234 万元\n"] * 50)
    session = _session()
    sentinel = SimpleNamespace(stream=None)      # 空流：装配按整段重算，但对象身份可辨
    session.prompt_stream = sentinel
    r = asyncio.run(_engine(be).generate_once(session, tools=None))
    assert r["repetition_guard"] == {"channel": "content", "reason": "line_repeat"}
    assert be.consumed < 20
    # 与取消同语义：后端记录未前进，prompt_stream 原对象保留、未被替换
    assert session.prompt_stream is sentinel


def test_engine_reports_thinking_channel_on_qwen35():
    be = _CountingBackend(["Wait, let me re-check the total again. "] * 200)
    thinking = []
    r = asyncio.run(_engine(be, "qwen35").generate_once(
        _session(), tools=None, enable_thinking=True, on_thinking_delta=thinking.append))
    assert r["repetition_guard"]["channel"] == "thinking"
    assert be.consumed < len(be.pieces)


def test_engine_normal_stream_untouched():
    pieces = [PROSE[i:i + 9] for i in range(0, len(PROSE), 9)]
    be = _CountingBackend(pieces)
    deltas = []
    r = asyncio.run(_engine(be).generate_once(_session(), tools=None,
                                              on_text_delta=deltas.append))
    assert "repetition_guard" not in r and be.consumed == len(pieces)
    assert r["text"] == PROSE


def test_engine_guard_disabled_never_fires(monkeypatch):
    import framework.core.engine as engine_mod
    monkeypatch.setattr(engine_mod, "repetition_guard_config", lambda: {"enabled": False})
    be = _CountingBackend(["123.45 "] * 400)
    r = asyncio.run(_engine(be).generate_once(_session(), tools=None))
    assert "repetition_guard" not in r and be.consumed == 400
    assert not [e for e in _events() if e["type"] == "repetition"]


# ---------- 编排层：首次推回、二次终止 ----------

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)
REP = {"channel": "content", "reason": "ngram_dup"}
NUDGE = "检测到输出在重复（content）。请换一种思路继续，或直接给出目前能给的结论；不要重复刚才的内容。"


def _orch(engine):
    return Orchestrator(engine, FakeRegistry([FakeTool("read_thing")]),
                        {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
                         "observation_max_chars": 4000, "tool_timeout_seconds": 5,
                         "max_parse_retries": 3, "thinking_mode": "manual"})


def _begin(session):
    session.journal.emit("turn_queued", {"prompt": "p", "attachments": [], "gen": dict(GEN),
                                         "origin": "chat"}, turn_id=new_turn_id())
    session.journal.emit("turn_started", {})


def test_first_hit_nudges_once_and_the_loop_continues(make_session):
    engine = FakeEngine([{"repetition_guard": REP}, {"text": "结论：营收同比增长 12%。"}])
    session = make_session()
    session.add_user_message("分析一下")
    _begin(session)
    result = asyncio.run(_orch(engine).run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "success"
    assert engine.calls[1]["tail_notes"] == [NUDGE]
    # 复读产出整体丢弃：history 里只有最终答复
    assistants = [m for m in session.history if m.get("role") == "assistant"]
    assert [m.get("content") for m in assistants] == ["结论：营收同比增长 12%。"]
    assert not [e for e in _events() if e["type"] == "turn_abort"]


def test_second_hit_in_a_turn_ends_it_with_a_visible_message(make_session):
    engine = FakeEngine([{"repetition_guard": REP},
                         {"repetition_guard": {"channel": "thinking", "reason": "line_repeat"}},
                         {"text": "不该走到这里"}])
    session = make_session()
    session.add_user_message("分析一下")
    _begin(session)
    result = asyncio.run(_orch(engine).run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "warning"
    assert "模型连续两次陷入重复输出，本轮已停止。" in result["response"]
    assert len(engine.calls) == 2 and len(engine.script) == 1
    assert session.history[-1] == {**session.history[-1], "role": "assistant",
                                   "content": "模型连续两次陷入重复输出，本轮已停止。"}
    assert session.activity["phase"] == "done"
    aborts = [e for e in _events() if e["type"] == "turn_abort"]
    assert len(aborts) == 1 and aborts[0]["detail"]["reason"] == "repetition_guard"
    assert result["metrics"]["generations"] == 2


def test_hits_after_a_nudge_in_a_new_turn_start_over(make_session):
    """计数按轮：上一轮推回过一次，新轮首次命中仍只是推回。"""
    engine = FakeEngine([{"repetition_guard": REP}, {"text": "好的"},
                         {"repetition_guard": REP}, {"text": "好的2"}])
    orch = _orch(engine)
    session = make_session()
    for prompt in ("一", "二"):
        session.add_user_message(prompt)
        _begin(session)
        assert asyncio.run(orch.run_turn(session, journal=session.journal,
                                         **GEN))["status"] == "success"
    assert engine.calls[3]["tail_notes"] == [NUDGE]
