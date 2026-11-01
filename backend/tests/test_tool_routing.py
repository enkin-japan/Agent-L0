"""W-2 编排层工具披露接线（D29–D31，FakeEngine）。

守的是：catalog 模式下任务建立时路由一次、任务期内冻结（同任务跨轮工具指纹不变）、新任务
重算、任务外不路由；full 模式与 W-2 前（6c75ea9）同形；get_tool_schema 对目录行工具仍取回
完整 schema。纯函数层见 test_tool_disclosure.py。
"""
import asyncio
import json

from framework.core.orchestrator import (COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA,
                                         UPDATE_PLAN_SCHEMA, Orchestrator)
from framework.core.prompt_stream import tools_fingerprint
from framework.core.task_state import TaskState

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import GEN, begin_turn

_TOOLS = {
    "memory_write": ("Save ONE durable fact to your long-term notes.", "", ["name"]),
    "deck_init": ("Start a presentation deck for this session.", "做幻灯片/PPT：先建演示稿",
                  ["title"]),
    "deck_export_pdf": ("Export the session deck as a PDF file.", "演示稿导出 PDF", ["path"]),
    "calendar_add": ("Create an event in the user's macOS Calendar.", "在日历新建日程",
                     ["title", "start"]),
    "rag_search": ("Search the user's LOCAL knowledge base.", "检索用户本地知识库", ["query"]),
    "run_shell_command": ("Run a shell command on the user's macOS machine.",
                          "终端执行 shell 命令", ["command"]),
}


class _Registry(FakeRegistry):
    """带真实形状参数与 summary 的注册表桩（FakeRegistry 的 schema 是空 properties）。"""

    def get_all_schemas(self):
        out = []
        for name in self._tools:
            desc, _sm, params = _TOOLS[name]
            out.append({"name": name, "description": desc,
                        "parameters": {"type": "object",
                                       "properties": {p: {"type": "string"} for p in params},
                                       "required": params[:1]}})
        return out

    def get_summaries(self):
        return {n: _TOOLS[n][1] for n in self._tools if _TOOLS[n][1]}


def _orch(engine, mode="catalog", top_k=2, core=("memory_write",), model_options=None):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual", "goal_verify": False,
           "tool_schema_mode": mode,
           "tool_routing": {"top_k": top_k, "core": list(core)}}
    return Orchestrator(engine, _Registry([FakeTool(n) for n in _TOOLS]), cfg,
                        model_options=model_options)


def _full_names(tools):
    return {t["name"] for t in tools if "parameters" in t}


def _turn(orch, engine, session, prompt, script):
    engine.script = list(script)
    n0 = len(engine.calls)
    session.add_user_message(prompt)
    begin_turn(session, prompt)
    res = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    engine.script = []
    return res, engine.calls[n0:]


_PLAN = call("update_plan", op="replace", steps=["第一步", "第二步"])
_STALL = [{"text": "先做到这里。"}] * 4        # 计划未完成即收尾：提前收尾推回后放行，任务保留


def test_catalog_task_routes_once_and_freezes_across_turns(make_session):
    engine = FakeEngine([])
    orch = _orch(engine)
    s = make_session()

    _, calls1 = _turn(orch, engine, s, "帮我做一份 PPT，最后导出 PDF", [{"tool_calls": [_PLAN]}] + _STALL)
    assert s.task_state is not None and s.task_state.plan
    routed = s.task_state.routed_tools
    assert routed == ["deck_export_pdf", "deck_init"]          # top_k=2，按分排序
    # 建任务那次生成在任务外：只有元工具 + core 完整
    assert _full_names(calls1[0]["tools"]) == {"update_plan", "complete_step", "remember",
                                                "get_tool_schema", "memory_write"}
    # 任务建立后的生成：routed 以完整 schema 入前缀
    after = calls1[1]["tools"]
    assert {"deck_init", "deck_export_pdf"} <= _full_names(after)
    assert "calendar_add" not in _full_names(after)
    fp_task = tools_fingerprint(after)
    assert all(tools_fingerprint(c["tools"]) == fp_task for c in calls1[1:])
    # 约束语法面恒为完整 schema，不随路由变
    assert _full_names(calls1[1]["grammar_tools"]) == set(_TOOLS) | {
        "update_plan", "complete_step", "remember", "get_tool_schema"}

    # 同一任务的下一轮：用户文本换成日历，且模型 replace 重规划——路由集合都不重算
    # （只在建任务时算；replace 不改 goal）
    task1 = s.task_state
    replan = call("update_plan", op="replace", steps=["日历一步", "日历两步"])
    _, calls2 = _turn(orch, engine, s, "顺便在日历加个日程", [{"tool_calls": [replan]}] + _STALL)
    assert s.task_state is task1 and task1.replan_count == 1
    assert [st["title"] for st in task1.plan] == ["日历一步", "日历两步"]
    assert task1.routed_tools == routed
    assert calls2 and all(tools_fingerprint(c["tools"]) == fp_task for c in calls2)
    assert "calendar_add" not in _full_names(calls2[0]["tools"])

    # 任务正常完结（task_state 被编排层置 None）：声明不撤，沿用会话级保留集合 → 不失配
    done = [{"tool_calls": [call("complete_step", result="a")]},
            {"tool_calls": [call("complete_step", result="b")]}, {"text": "全部完成。"}]
    _, calls_done = _turn(orch, engine, s, "继续", done)
    assert s.task_state is None and s.last_routed_tools == routed
    _, calls_chat = _turn(orch, engine, s, "日历是什么？", [{"text": "解释。"}])
    assert all(tools_fingerprint(c["tools"]) == fp_task for c in calls_done + calls_chat)

    # 下一个任务建立（目标不同）才重新路由，集合随之变；建任务那次生成仍是旧集合
    _, calls3 = _turn(orch, engine, s, "在日历加一个明天的日程",
                      [{"tool_calls": [_PLAN]}] + _STALL)
    assert s.task_state.routed_tools == ["calendar_add"] == s.last_routed_tools
    assert tools_fingerprint(calls3[0]["tools"]) == fp_task
    assert "calendar_add" in _full_names(calls3[1]["tools"])
    assert "deck_init" not in _full_names(calls3[1]["tools"])
    assert tools_fingerprint(calls3[1]["tools"]) != fp_task

    # 会话级保留集合随冻结落盘、恢复时读回（save_task/load_task 同时机）
    s.task_state = None
    s.save_task()
    s.last_routed_tools = None
    s.load_task()
    assert s.last_routed_tools == ["calendar_add"] and s.task_state is None


def test_catalog_chat_before_first_task_is_not_routed(make_session):
    """首个任务建立前（会话级保留集合为空）：只有元工具 + core 完整。"""
    engine = FakeEngine([])
    orch = _orch(engine)
    s = make_session()
    _, calls = _turn(orch, engine, s, "PPT 和 PDF 是什么意思？", [{"text": "解释如下。"}])
    assert s.task_state is None and s.last_routed_tools is None
    assert calls[0]["tools"] == orch._tools_decl                # 基座声明
    assert _full_names(calls[0]["tools"]) == {"update_plan", "complete_step", "remember",
                                               "get_tool_schema", "memory_write"}


def test_restored_task_without_routing_record_routes_once(make_session):
    """旧快照（无 routed_tools 键）恢复的任务：首个生成就地补算一次并冻结。"""
    engine = FakeEngine([])
    orch = _orch(engine)
    s = make_session()
    t = TaskState.from_snapshot({"goal": "帮我做 PPT", "plan": [
        {"id": 1, "title": "x", "status": "doing", "note": ""}]})
    assert t.routed_tools is None
    s.task_state = t
    _, calls = _turn(orch, engine, s, "继续", _STALL)
    assert t.routed_tools == ["deck_init"] == s.last_routed_tools
    assert "deck_init" in _full_names(calls[0]["tools"])
    # 快照往返保住冻结记录
    assert TaskState.from_snapshot(t.snapshot()).routed_tools == ["deck_init"]


def test_full_mode_matches_pre_w2_shape(make_session):
    """full 模式：声明 = 注册表 schema + 三件元工具，任务前后不变——与 6c75ea9
    的 `list(registry.get_all_schemas()) + metas` 同值；tool_routing 配了也不生效。"""
    engine = FakeEngine([])
    orch = _orch(engine, mode="full", top_k=3)
    expected = list(orch.registry.get_all_schemas()) + [UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA,
                                                        REMEMBER_SCHEMA]
    assert orch._tools_decl == expected
    assert orch._grammar_tools == orch._tools_decl
    s = make_session()
    _, calls = _turn(orch, engine, s, "帮我做一份 PPT", [{"tool_calls": [_PLAN]}] + _STALL)
    assert s.task_state.routed_tools is None and s.last_routed_tools is None  # full 不写
    assert all(c["tools"] == orch._tools_decl for c in calls)


def test_get_tool_schema_returns_full_schema_for_catalog_line(make_session):
    engine = FakeEngine([])
    orch = _orch(engine)
    by = {t["name"]: t for t in orch._tools_decl}
    assert set(by["calendar_add"]) == {"name", "description"}
    assert by["calendar_add"]["description"] == "在日历新建日程"
    s = make_session()
    out = orch._handle_meta_call(s, call("get_tool_schema", names=["calendar_add"]))
    detail = json.loads(out)[0]
    assert detail["parameters"]["properties"] == {"title": {"type": "string"},
                                                  "start": {"type": "string"}}


# ---------- 会话级保留集合：轮级回滚 / 删轮 / 分支（复审 2026-11-01） ----------

def _runner(engine, approval_tool=None):
    from framework.core.scheduler import ClassPolicy, TaskScheduler
    from framework.core.turn_runner import TurnRunner
    tools = [FakeTool(n) for n in _TOOLS if n != approval_tool]
    if approval_tool:
        tools.append(FakeTool(approval_tool, approval=True, read_only=False,
                              concurrency_safe=False))
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual", "goal_verify": False,
           "tool_schema_mode": "catalog", "tool_routing": {"top_k": 2, "core": ["memory_write"]}}
    orch = Orchestrator(engine, _Registry(tools), cfg)
    return TurnRunner(orch, TaskScheduler(policy=ClassPolicy()))


async def _submit(runner, session, prompt):
    ctx = await runner.accept_turn(session, session.session_id, prompt=prompt, images=[],
                                   audios=[], ingested_text="", attachments=[], gen=dict(GEN))
    return await runner.run_accepted(session, session.session_id, ctx)


def test_cancelled_task_creating_turn_restores_retained_set(make_session):
    """建任务那一轮被取消回滚：last_routed_tools 回到轮初值（首个任务前 = None；
    已有保留集合 = 原值），不留下一个从未落地的任务选出的工具。"""
    s = make_session()
    engine = FakeEngine([{"tool_calls": [_PLAN]}, {"cancelled": True}])
    rd = asyncio.run(_submit(_runner(engine), s, "帮我做一份 PPT"))
    assert rd["status"] == "cancelled"
    assert s.task_state is None and s.last_routed_tools is None

    s.last_routed_tools = ["calendar_add"]
    engine.script = [{"tool_calls": [_PLAN]}, {"cancelled": True}]
    asyncio.run(_submit(_runner(engine), s, "帮我做一份 PPT"))
    assert s.last_routed_tools == ["calendar_add"]


def test_engine_crash_in_task_creating_turn_restores_retained_set(make_session):
    s = make_session()
    engine = FakeEngine([{"tool_calls": [_PLAN]}])     # 第二次生成脚本耗尽 → 引擎故障
    rd = asyncio.run(_submit(_runner(engine), s, "帮我做一份 PPT"))
    assert rd["status"] == "fail"
    assert s.task_state is None and s.last_routed_tools is None


def test_discarding_pending_turn_restores_retained_set(make_session):
    """删未落盘的待审批轮（_discard_unlogged_turn_locked 路径）：保留集合回到轮初值并落盘。"""
    from framework.memory.session import SessionManager
    s = make_session()
    runner = _runner(FakeEngine([{"tool_calls": [_PLAN]},
                                 {"tool_calls": [call("calendar_add", title="x")]}]),
                     approval_tool="calendar_add")
    rd = asyncio.run(_submit(runner, s, "帮我做一份 PPT"))
    assert rd["status"] == "pending_approval"
    assert s.last_routed_tools == ["deck_init"]   # 建任务已写入
    mgr = SessionManager(s.session_dir.parent)
    mgr._all_sessions[s.session_id] = s
    out = asyncio.run(runner.delete_last_turn(mgr, s.session_id))
    assert out["status"] is True
    assert s.last_routed_tools is None and not s.routing_file.exists()


def test_deleting_last_remaining_turn_clears_retained_set(make_session):
    """删已落盘轮：没有轮初快照，删到一轮不剩时清空保留集合（首个任务前为空）。"""
    from framework.memory.session import SessionManager
    s = make_session()
    asyncio.run(_submit(_runner(FakeEngine([{"tool_calls": [_PLAN]}] + _STALL)), s,
                        "帮我做一份 PPT，最后导出 PDF"))
    assert s.last_routed_tools == ["deck_export_pdf", "deck_init"]
    s.save_task()
    assert s.routing_file.exists()
    mgr = SessionManager(s.session_dir.parent)
    mgr._all_sessions[s.session_id] = s
    res = mgr._delete_last_turn_locked(s, s.history_log, {"session_id": s.session_id})
    assert res["remaining_turns"] == 0
    assert s.last_routed_tools is None and not s.routing_file.exists()


def test_branch_inherits_retained_set(make_session):
    """分支克隆父会话 KV，工具段必须同声明：保留集合随分支走（内存值 + routing.json）。"""
    from framework.memory.session import SessionManager
    parent = make_session("rt-parent")
    asyncio.run(_submit(_runner(FakeEngine([{"tool_calls": [_PLAN]}] + _STALL)), parent,
                        "帮我做一份 PPT，最后导出 PDF"))
    assert parent.last_routed_tools == ["deck_export_pdf", "deck_init"]
    mgr = SessionManager(parent.session_dir.parent)
    mgr._all_sessions[parent.session_id] = parent
    res = asyncio.run(mgr.branch_session("rt-parent", 0, "rt-child"))
    assert res["status"] is True
    child = mgr._all_sessions["rt-child"]
    assert child.last_routed_tools == ["deck_export_pdf", "deck_init"]
    assert json.loads(child.routing_file.read_text())["last_routed_tools"] == \
        ["deck_export_pdf", "deck_init"]

    # 冷父会话（未给内存值）：复制父会话的 routing.json
    parent.save_task()
    mgr._materialize_branch("rt-parent", "rt-cold-child",
                            [parent.history_log.read_text().splitlines()[0]], 0)
    assert (mgr.sessions_dir / "rt-cold-child" / "routing.json").exists()
