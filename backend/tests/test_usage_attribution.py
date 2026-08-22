"""子沙箱用量归因金测试（第 2 层波 F6）。

**病根**（三条，本文件逐条钉死）：
① `history.jsonl` 落盘的 metrics 系统性低估——web_research / math_research /
   excel_research 在自己的一次性会话里跑的每一次 `generate_once` 完全不入父轮的账
   （项目自记「实测低估 8 倍」）；
② 升级验收口径对「一次调研到底烧了多少」是瞎的；
③ `max_total_iterations` 熔断只数主循环迭代——一个连调五次 web_research 的轮次
   消耗三十次生成却只计五次，保险丝形同虚设。

**归因的唯一通道**是 `note_usage(sink, ...)`：沙箱把计数回填父轮的 `turn_usage`。
正因为它是沙箱唯一的向外出口，本文件同样钉死它的**类型闸与隔离边界**——
sink 只准过数值（非 int 含 bool 一律丢弃、负值钳 0），且绝不能出现在任何 prompt 面
（`inject_env_context = False` 的反方向：别让沙箱上下文顺着记账口漏回父轮）。

分桶纪律：`sub_*` 是子 agent，计入熔断；`goal_verify_*` 是护栏不是子 agent，
单独成桶、**不计入熔断**（把安全网自己烧进保险丝就本末倒置了）。
"""
import json

import pytest

from framework.agents import runtime as agent_runtime
from framework.agents import math_sandbox, research_sandbox
from framework.agents.sandbox_runtime import (EphemeralSession, SandboxSpec,
                                              note_usage, run_sandbox)

from .fake_engine import FakeBackend, FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import GEN, begin_turn, make_orch, run

PLAN3 = {"tool_calls": [call("update_plan", op="replace", steps=["查A", "查B", "汇总"])]}
DONE3 = {"tool_calls": [call("complete_step", result="A=1"),
                        call("complete_step", result="B=2"),
                        call("complete_step", result="汇总=3")]}


def _gen(text_or_calls, tokens):
    """一条带自定指标的脚本项（token 求和可断言）。"""
    step = dict(text_or_calls)
    step["metrics"] = {"prompt_tokens": 10, "generation_tokens": tokens,
                       "generation_tps": 1.0}
    return step


# ==========================================================================
# 1. note_usage：累加、分桶、类型闸、空槽
# ==========================================================================

def test_note_usage_accumulates_within_and_across_buckets():
    sink = {}
    note_usage(sink, generations=1, generation_tokens=30)
    note_usage(sink, generations=1, generation_tokens=12)
    note_usage(sink, generations=1, generation_tokens=7, bucket="goal_verify")
    assert sink == {"sub_generations": 2, "sub_generation_tokens": 42,
                    "goal_verify_generations": 1, "goal_verify_generation_tokens": 7}


def test_note_usage_seeded_sink_keeps_prior_total():
    """审批分段续算的地基：槽里已有的数是前几段的，只能加不能覆盖。"""
    sink = {"sub_generations": 5, "sub_generation_tokens": 500}
    note_usage(sink, generations=2, generation_tokens=40)
    assert sink == {"sub_generations": 7, "sub_generation_tokens": 540}


def test_note_usage_none_sink_is_a_no_op():
    """脚本直跑沙箱（无轮次上下文）时记账静默空转——仪表绝不影响沙箱本体。"""
    note_usage(None, generations=1, generation_tokens=10)
    note_usage(None, generations=1, generation_tokens=10, bucket="goal_verify")


def test_note_usage_drops_bool_generations():
    """bool 是 int 的子类：不拦住的话 True 会静默变成一次生成。"""
    sink = {}
    note_usage(sink, generations=True, generation_tokens=20)
    assert sink == {"sub_generation_tokens": 20}
    assert "sub_generations" not in sink


def test_note_usage_drops_bool_tokens():
    sink = {}
    note_usage(sink, generations=1, generation_tokens=False)
    assert sink == {"sub_generations": 1}
    assert "sub_generation_tokens" not in sink


@pytest.mark.parametrize("bad", [1.5, "3", None, [1], {"n": 1}, object()])
def test_note_usage_drops_non_int(bad):
    """sink 是沙箱通向父轮的唯一出口：只准过数值，任何非 int 面一律丢弃。"""
    sink = {}
    note_usage(sink, generations=bad, generation_tokens=bad)
    assert sink == {}


def test_note_usage_clamps_negative_to_zero():
    """用量不会是负的：负值只可能来自算错或注入，钳 0 而不是让它把账做小。"""
    sink = {"sub_generations": 5}
    note_usage(sink, generations=-3, generation_tokens=-100)
    assert sink == {"sub_generations": 5, "sub_generation_tokens": 0}


def test_note_usage_bucket_name_is_the_only_difference():
    sink = {}
    note_usage(sink, generations=1, generation_tokens=9, bucket="goal_verify")
    assert sink == {"goal_verify_generations": 1, "goal_verify_generation_tokens": 9}
    assert not any(k.startswith("sub_") for k in sink)


# ==========================================================================
# 2. 隔离边界：sink 只记数，绝不进 prompt 面
# ==========================================================================

def test_usage_sink_never_reaches_the_prompt_face():
    """记账口不是回流口：sink 的内容不得出现在沙箱 history（= 喂给模型的全部上下文）。
    sink 不挂会话对象(复审 L5:真实路径是 SandboxSpec/显式参数)——这里驱动真实的
    run_sandbox 记账路径,再查 prompt 面。"""
    sink = {"sub_generations": 7, "probe": "SINK-LEAK-CANARY"}
    sess = EphemeralSession(FakeBackend(), "system prompt", "opening question")

    assert not hasattr(sess, "usage_sink")              # 死字段不许回潮
    blob = json.dumps(sess.history, ensure_ascii=False, default=str)
    assert "SINK-LEAK-CANARY" not in blob               # prompt 面没有
    assert "sub_generations" not in blob
    assert "usage_sink" not in blob
    # 隐私边界一并复核：F6 不得成为沙箱上下文回流的新缝
    assert sess.inject_env_context is False


@pytest.mark.asyncio
async def test_sandbox_history_stays_clean_after_accounting():
    """跑完一整段沙箱循环后，历史里仍不含任何记账痕迹。"""
    sink = {"probe": "SINK-LEAK-CANARY"}
    eng = FakeEngine([_gen({"tool_calls": [call("noop")]}, 11),
                      _gen({"tool_calls": [call("noop", i=2)]}, 22)])
    sess = EphemeralSession(eng.backend, "sys", "go")
    await run_sandbox(eng, _spec(sess, sink, max_steps=2))

    blob = json.dumps(sess.history, ensure_ascii=False, default=str)
    assert "SINK-LEAK-CANARY" not in blob
    assert "sub_generations" not in blob


# ==========================================================================
# 3. run_sandbox / 各沙箱：每次子生成都入账
# ==========================================================================

def _spec(sess, sink, *, max_steps):
    """最小可跑 SandboxSpec：分发恒成功、不收束，把 max_steps 次生成跑满。"""
    async def _dispatch(calls):
        return [{"name": c.get("name"), "response": "ok"} for c in calls], None

    async def _exhausted():
        return "[exhausted]"

    return SandboxSpec(tag="probe", session=sess, tools=[{"name": "noop"}],
                       max_steps=max_steps, max_tokens=64,
                       dispatch=_dispatch,
                       on_give_up=lambda text: "[gave up]",
                       on_exhausted=_exhausted,
                       parse_feedback="resend",
                       usage_sink=sink)


@pytest.mark.asyncio
async def test_run_sandbox_records_every_generation():
    sink = {}
    eng = FakeEngine([_gen({"tool_calls": [call("noop", i=1)]}, 11),
                      _gen({"tool_calls": [call("noop", i=2)]}, 22),
                      _gen({"tool_calls": [call("noop", i=3)]}, 33)])
    sess = EphemeralSession(eng.backend, "sys", "go")

    out = await run_sandbox(eng, _spec(sess, sink, max_steps=3))

    assert out == "[exhausted]"
    assert len(eng.calls) == 3
    assert sink == {"sub_generations": 3, "sub_generation_tokens": 66}


@pytest.mark.asyncio
async def test_run_sandbox_without_sink_still_runs():
    """无轮次上下文（sink=None）时沙箱行为一字不变。"""
    eng = FakeEngine([_gen({"tool_calls": [call("noop")]}, 11)])
    sess = EphemeralSession(eng.backend, "sys", "go")
    assert not hasattr(sess, "usage_sink")   # 死字段已除(复审 L5),不许回潮
    assert await run_sandbox(eng, _spec(sess, None, max_steps=1)) == "[exhausted]"


@pytest.mark.asyncio
async def test_math_sandbox_records_the_exhausted_generation_too():
    """预算用尽后的那次强制生成同样烧算力——run_sandbox 之外的调用点不能漏记。"""
    sink = {}
    eng = FakeEngine([_gen({"text": "答案是 4"}, 5),      # 纯文本 → 推回
                      _gen({"text": "还是 4"}, 7)])       # on_exhausted 的强制生成
    await math_sandbox.run_math_research(eng, "2+2", {"max_steps": 1}, usage_sink=sink)

    assert len(eng.calls) == 2
    assert sink == {"sub_generations": 2, "sub_generation_tokens": 12}


@pytest.mark.asyncio
async def test_page_digest_single_shot_is_recorded():
    """web_read 的单发沙箱（run_sandbox 之外的第四个调用点）同样入账。"""
    sink = {}
    eng = FakeEngine([_gen({"text": "页面讲了 X"}, 42)])
    out = await research_sandbox.run_page_digest(eng, "讲了啥", "<material/>", {},
                                                 usage_sink=sink)

    assert out == "页面讲了 X"
    assert sink == {"sub_generations": 1, "sub_generation_tokens": 42}


# ==========================================================================
# 4/5. 编排层：熔断计入子生成、metrics 落盘、goal_verify 单独分桶
# ==========================================================================

class _SinkWritingRegistry(FakeRegistry):
    """工具线程内经 ContextVar 取本轮用量槽并记账。

    刻意走真实交接路径（orchestrator 在轮内 set → asyncio.to_thread 拷上下文进工具
    线程 → 工具线程 get），而不是直接拿着 dict 写——本波的接线正是在这条路径上。
    """

    def __init__(self, tools, *, per_call=0, tokens=0, bucket="sub"):
        super().__init__(tools)
        self.per_call = per_call
        self.tokens = tokens
        self.bucket = bucket
        self.sinks_seen = []

    def execute_tool(self, name, arguments):
        sink = agent_runtime.get_usage_sink()
        self.sinks_seen.append(sink)
        note_usage(sink, generations=self.per_call, generation_tokens=self.tokens,
                   bucket=self.bucket)
        return super().execute_tool(name, arguments)


_DIG_SCENE = [{"tool_calls": [call("dig", q=1)]},
              {"tool_calls": [call("dig", q=2)]},
              {"text": "结论"}]


def test_sub_generation_fuse_aborts_runaway_research(make_session):
    """子生成走**独立**保险丝（复审修正）：每次调研烧 3 次子生成、上限 6 →
    两次工具调用后熔断,主循环预算(40)分毫未动。原病根「子生成完全无界」由此闭合。"""
    engine = FakeEngine(list(_DIG_SCENE))
    registry = _SinkWritingRegistry([FakeTool("dig")], per_call=3, tokens=100)
    orch = make_orch(engine, registry, max_sub_generations=6)
    session = make_session()
    session.add_user_message("查三件事")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "warning"
    assert "子调研生成已达预算上限" in result["response"]
    assert len(engine.calls) == 2                        # 第三次生成没机会发生
    assert registry.sinks_seen and registry.sinks_seen[0] is not None  # 交接真的接上了
    # 熔断路同样要落账（复审 Critical:此前这条返回路 metrics 是空 dict）
    assert result["metrics"]["sub_generations"] == 6
    assert result["metrics"]["sub_generation_tokens"] == 200
    assert result["metrics"]["generations"] == 2


def test_sub_generations_do_not_shrink_the_main_budget(make_session):
    """独立丝的另一半（复审 S3）：大量子生成**不**挤占主循环预算——一次 excel_research
    ≈16 次生成,计进主分母会把「原来能答的问题」变成未收敛。"""
    engine = FakeEngine(list(_DIG_SCENE))
    registry = _SinkWritingRegistry([FakeTool("dig")], per_call=3, tokens=100)
    orch = make_orch(engine, registry, max_total_iterations=6, max_sub_generations=100)
    session = make_session()
    session.add_user_message("查三件事")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"                 # 6 次子生成,主丝(6)不受影响
    assert result["response"] == "结论"
    assert result["metrics"]["sub_generations"] == 6


def test_same_scene_without_sub_generations_converges(make_session):
    """阳性对照：现场一字不改、只是不写 sink → 正常收敛,零用量不加字段。"""
    engine = FakeEngine(list(_DIG_SCENE))
    registry = _SinkWritingRegistry([FakeTool("dig")], per_call=0)
    orch = make_orch(engine, registry, max_sub_generations=6)
    session = make_session()
    session.add_user_message("查三件事")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "结论"
    assert len(engine.calls) == 3
    assert "sub_generations" not in result["metrics"]    # 零用量不给存量轮次加字段


def test_goal_verify_bucket_never_burns_the_fuse(make_session):
    """同一现场、只把桶换成 goal_verify：护栏开销再大也不进保险丝。"""
    engine = FakeEngine(list(_DIG_SCENE))
    registry = _SinkWritingRegistry([FakeTool("dig")], per_call=50, tokens=9,
                                    bucket="goal_verify")
    orch = make_orch(engine, registry, max_total_iterations=6)
    session = make_session()
    session.add_user_message("查三件事")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"                 # 100 次护栏生成，零熔断影响
    assert len(engine.calls) == 3
    assert result["metrics"]["goal_verify_generations"] == 100
    assert result["metrics"]["goal_verify_generation_tokens"] == 18
    assert "sub_generations" not in result["metrics"]


def test_metrics_carry_sub_usage_without_polluting_main_counters(make_session):
    engine = FakeEngine([{"tool_calls": [call("dig", q=1)]}, {"text": "结论"}])
    registry = _SinkWritingRegistry([FakeTool("dig")], per_call=4, tokens=250)
    orch = make_orch(engine, registry, max_total_iterations=20)
    session = make_session()
    session.add_user_message("查一件事")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    m = result["metrics"]

    assert result["status"] == "success"
    assert m["sub_generations"] == 4
    assert m["sub_generation_tokens"] == 250
    # 主循环口径不被子生成污染：generations 仍是主循环迭代数（两个数各有各的用途）
    assert m["generations"] == 2


def test_goal_verify_generation_lands_in_its_own_bucket(make_session):
    """真实 goal_verify 路径（收尾审计的那次小生成）落 goal_verify 桶，不混进 sub。"""
    engine = FakeEngine([PLAN3, DONE3,
                         {"text": "A=1，B=2，汇总=3。"},
                         _gen({"text": "VERDICT: PASS\ncomplete"}, 17)])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("查 A 和 B 并汇总数值")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    m = result["metrics"]

    assert result["status"] == "success"
    assert len(engine.calls) == 4
    assert m["goal_verify_generations"] == 1
    assert m["goal_verify_generation_tokens"] == 17
    assert "sub_generations" not in m


def test_plain_turn_gains_no_new_metric_keys(make_session):
    """无子生成的普通轮次：metrics 形状一字不变（history.jsonl 是既有消费面）。"""
    engine = FakeEngine([{"text": "你好!"}])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")
    begin_turn(session)

    m = run(orch.run_turn(session, journal=session.journal, **GEN))["metrics"]

    assert not any(k.startswith(("sub_", "goal_verify_")) for k in m)


# ==========================================================================
# 6. 审批分段：用量跨段续算
# ==========================================================================

def test_usage_carries_across_the_approval_split(make_session):
    """审批把一轮切成两段：用量必须跨段续算，否则「弹审批→续跑」又成了绕过保险丝的路。"""
    engine = FakeEngine([
        {"tool_calls": [call("dig", q=1)]},          # 段一：烧 2 次子生成
        {"tool_calls": [call("danger_op", target="prod")]},   # → 挂起待审批
        {"tool_calls": [call("dig", q=2)]},          # 段二：再烧 2 次
        {"text": "结论"},
    ])
    registry = _SinkWritingRegistry(
        [FakeTool("dig"),
         FakeTool("danger_op", result="done", approval=True, read_only=False,
                  concurrency_safe=False)],
        per_call=2, tokens=50)
    orch = make_orch(engine, registry, max_total_iterations=20)
    session = make_session()
    session.add_user_message("查完再动手")
    begin_turn(session)

    first = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert first["status"] == "pending_approval"
    # 暂存的整轮口径带上用量（与 breakdown/gen_tokens 同行）
    assert session.carried_turn_stats["usage"] == {"sub_generations": 2,
                                                   "sub_generation_tokens": 50}

    resumed = run(orch.resume_after_approval(session, approved=True,
                                             journal=session.journal, **GEN))

    assert resumed["status"] == "success"
    assert resumed["metrics"]["sub_generations"] == 4       # 2（段一）+ 2（段二）
    assert resumed["metrics"]["sub_generation_tokens"] == 100


# ==========================================================================
# 7. MCP 包壳交接（复审 gap-a）：get_usage_sink → usage_sink= 这一跳,删掉任何
#    一处 kwarg 全套照绿——每个包壳各钉一条,断言沙箱协程收到的正是本线程的槽
# ==========================================================================

import threading as _threading


class _BgLoop:
    """后台事件循环:承接 run_coroutine_threadsafe(包壳的真实调度路径)。"""

    def __enter__(self):
        import asyncio
        self.loop = asyncio.new_event_loop()
        self.thread = _threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *a):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def _wire_runtime(monkeypatch, loop):
    monkeypatch.setattr(agent_runtime, "is_ready", lambda: True)
    monkeypatch.setattr(agent_runtime, "get_engine", lambda: object())
    monkeypatch.setattr(agent_runtime, "get_loop", lambda: loop)


def _capture_coro(captured, ret="ok"):
    async def _fake(*args, **kwargs):
        captured.update(kwargs)
        return ret
    return _fake


def test_web_research_wrapper_hands_the_sink_over(monkeypatch):
    from framework.agents import research_sandbox
    from framework.mcp.tools import web_research as wr
    captured, sink = {}, {}
    with _BgLoop() as loop:
        _wire_runtime(monkeypatch, loop)
        monkeypatch.setattr(agent_runtime, "get_progress_hook", lambda: None)
        # 包壳在函数体内 import,替身必须打到源模块
        monkeypatch.setattr(research_sandbox, "run_research", _capture_coro(captured))
        agent_runtime.set_usage_sink(sink)
        tool = wr.WebResearchTool.__new__(wr.WebResearchTool)
        tool.execute("查一下")
    assert captured["usage_sink"] is sink


def test_math_research_wrapper_hands_the_sink_over(monkeypatch):
    from framework.agents import math_sandbox
    from framework.mcp.tools import math_research as mr
    captured, sink = {}, {}
    with _BgLoop() as loop:
        _wire_runtime(monkeypatch, loop)
        monkeypatch.setattr(math_sandbox, "run_math_research", _capture_coro(captured))
        agent_runtime.set_usage_sink(sink)
        tool = mr.MathResearchTool.__new__(mr.MathResearchTool)
        tool.execute("1+1")
    assert captured["usage_sink"] is sink


def test_excel_research_wrapper_hands_the_sink_over(monkeypatch, make_session):
    from framework.mcp.tools import excel_research as er
    captured, sink = {}, {}
    session = make_session("f6-excel-wrap")
    with _BgLoop() as loop:
        _wire_runtime(monkeypatch, loop)
        monkeypatch.setattr(agent_runtime, "get_current_session", lambda: session)
        monkeypatch.setattr(agent_runtime, "get_progress_hook", lambda: None)
        from framework.agents import excel_sandbox
        monkeypatch.setattr(excel_sandbox, "run_excel_task", _capture_coro(captured))
        agent_runtime.set_usage_sink(sink)
        tool = er.ExcelResearchTool.__new__(er.ExcelResearchTool)
        tool.execute("做张表")
    assert captured["usage_sink"] is sink


def test_web_read_wrapper_hands_the_sink_over(monkeypatch):
    from framework.agents import web_tools
    from framework.mcp.tools import web_read as wrd
    captured, sink = {}, {}
    with _BgLoop() as loop:
        _wire_runtime(monkeypatch, loop)
        monkeypatch.setattr(agent_runtime, "report_progress", lambda *a, **k: None)
        monkeypatch.setattr(web_tools, "reset_extraction_cache", lambda: None)
        monkeypatch.setattr(web_tools, "open_pages",
                            lambda urls, cfg: [{"url": urls[0], "text": "正文"}])
        monkeypatch.setattr(web_tools, "wrap_untrusted", lambda pages: "<material/>")
        from framework.agents import research_sandbox
        monkeypatch.setattr(research_sandbox, "run_page_digest", _capture_coro(captured))
        agent_runtime.set_usage_sink(sink)
        tool = wrd.WebReadTool.__new__(wrd.WebReadTool)
        tool.execute("https://example.com", "讲了啥")
    assert captured["usage_sink"] is sink


# ==========================================================================
# 8. 取消也计费（复审 gap-b）：run_sandbox 的记账在 cancelled 早退**之前**——
#    被取消的生成同样烧了算力,两行换序这里就红
# ==========================================================================

def test_cancelled_generation_is_still_billed():
    from framework.agents import sandbox_runtime
    from framework.agents.sandbox_runtime import EphemeralSession, SandboxSpec
    sink = {}
    eng = FakeEngine([{"cancelled": True, "thoughts": [], "text": "", "tool_calls": [],
                       "metrics": {"generation_tokens": 37}}])
    sess = EphemeralSession(eng.backend, "s", "o")

    async def _dispatch(calls):
        return [], None

    spec = SandboxSpec(tag="probe", session=sess, tools=[{"name": "noop"}],
                       max_steps=3, max_tokens=64, dispatch=_dispatch,
                       on_give_up=lambda text: "[gave up]",
                       on_exhausted=lambda: "[exhausted]",
                       parse_feedback="resend", usage_sink=sink)
    run(sandbox_runtime.run_sandbox(eng, spec))
    assert sink.get("sub_generations") == 1              # 烧了就入账
    assert sink.get("sub_generation_tokens") == 37


def test_fuse_blown_on_entry_still_stamps_metrics(make_session):
    """★ 复审 Critical 的钉子:携带态(审批分段)越过阈值时 while 体**一次都不进**,
    循环内的 stamp 点全都够不着——熔断返回路自己不 stamp 的话 metrics 就是空 dict,
    整轮口径恰好在 F6 造出的易达路径上失守。初版夹具让循环跑了两轮,循环内 stamp
    把账补上了,变异体存活揪出——本条必须从「进门即熔断」驱动。"""
    engine = FakeEngine([{"text": "不该被生成"}])
    registry = _SinkWritingRegistry([FakeTool("dig")])
    orch = make_orch(engine, registry, max_total_iterations=2, max_sub_generations=100)
    session = make_session()
    session.add_user_message("查")
    begin_turn(session)
    # 布携带态:上一段已烧满主循环预算,并带着子用量
    session.carried_iterations = 2
    session.carried_turn_stats = {"breakdown": {}, "gen_tokens": 77,
                                  "usage": {"sub_generations": 4,
                                            "sub_generation_tokens": 100}}

    # keep_recent_calls=True = 审批续跑段:携带态不归零(新用户轮会清零,只有续跑
    # 段才带着上一段的计数进门——这正是 Critical 场景的入口)
    result = run(orch.run_turn(session, journal=session.journal,
                               keep_recent_calls=True, **GEN))

    assert result["status"] == "warning"
    assert "未能收敛" in result["response"]
    assert len(engine.calls) == 0                        # while 体确实一次没进
    # 熔断路的 metrics 不是空 dict:携带的主循环计数与子用量都落了账
    assert result["metrics"]["generations"] == 2
    assert result["metrics"]["generation_tokens_turn"] == 77
    assert result["metrics"]["sub_generations"] == 4
    assert result["metrics"]["sub_generation_tokens"] == 100
