"""编排层生命周期金测试(Phase 0 回归网,Phase 1 起断言账本事件)。

覆盖 Orchestrator.run_turn / resume_after_approval 的全部关键收尾路径与护栏行为。
过程事件一律从会话账本(session.journal.ring)断言——账本是唯一事件出口
(PROTOCOL.md:先记账后分发),不存在旁路。
"""
import asyncio

from framework.core.journal import new_turn_id
from framework.core.orchestrator import Orchestrator
from framework.core.task_state import TaskState

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, bad_call, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)


def make_orch(engine, registry, **cfg_over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual"}
    cfg.update(cfg_over)
    return Orchestrator(engine, registry, cfg)


def begin_turn(session, prompt="test"):
    """建立轮次上下文(正式路径由 TurnRunner 发 turn_queued;测试等价起点)。"""
    session.journal.emit("turn_queued", {"prompt": prompt, "attachments": [],
                                         "gen": dict(GEN), "origin": "chat"},
                         turn_id=new_turn_id())
    session.journal.emit("turn_started", {})


def ring_of(session, etype):
    return [e["data"] for e in session.journal.ring if e["type"] == etype]


def ring_types(session):
    return [e["type"] for e in session.journal.ring]


def run(coro):
    return asyncio.run(coro)


# ---------- 基本收尾 ----------

def test_plain_text_success(make_session):
    engine = FakeEngine([{"text": "你好!", "thoughts": ["简单问候"]}])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "你好!"
    assert session.history[-1]["role"] == "assistant"
    assert session.history[-1]["content"] == "你好!"
    assert session.activity["phase"] == "done"
    # 账本:回调由编排层集中注入(单一写入者)——正文与思考增量都在账本里
    assert ring_of(session, "iteration_started") == [{"n": 1, "thinking": False}]
    assert ring_of(session, "token_delta") == [{"text": "你好!"}]
    assert ring_of(session, "thinking_delta") == [{"text": "简单问候"}]


def test_tool_then_answer(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/tmp/x")]},
        {"text": "答案是 42。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing", result="42")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("x 是多少?")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert registry.executed == [{"name": "read_thing", "args": {"path": "/tmp/x"}}]
    # 历史结构:system(会话头)→ user → assistant(tool_calls) → tool → assistant(text)
    roles = [m["role"] for m in session.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    # 账本事件序:tool_started 先于 tool_result,携配对 call_id
    started, finished = ring_of(session, "tool_started"), ring_of(session, "tool_result")
    assert len(started) == len(finished) == 1
    assert started[0]["call_id"] == finished[0]["call_id"]
    assert finished[0]["status"] == "ok" and finished[0]["response"] == "42"
    types = ring_types(session)
    assert types.index("tool_started") < types.index("tool_result")


# ---------- 审批闸门 ----------

def test_approval_gate_and_approve_resume(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("danger_op", target="prod")]},
        {"text": "已执行完毕。"},  # 决议批准后的续跑生成
    ])
    registry = FakeRegistry([FakeTool("danger_op", result="done",
                                      approval=True, read_only=False,
                                      concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("执行危险操作")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "pending_approval"
    assert result["pending_tools"] == [{"name": "danger_op", "arguments": {"target": "prod"}}]
    assert registry.executed == []                       # 挂起期间未执行
    assert session.pending_tool_calls                    # 暂存待决议
    assert session.carried_iterations == 1               # 熔断分母跨续跑累计
    assert ring_of(session, "tool_started") == []        # 未执行 = 无工具事件

    resumed = run(orch.resume_after_approval(session, approved=True,
                                             journal=session.journal, **GEN))

    assert resumed["status"] == "success"
    assert resumed["response"] == "已执行完毕。"
    assert registry.executed == [{"name": "danger_op", "args": {"target": "prod"}}]
    assert session.pending_tool_calls is None
    # 修复验收:批准执行的敏感调用,其 tool_started/result 事件不再丢失
    started, finished = ring_of(session, "tool_started"), ring_of(session, "tool_result")
    assert [s["name"] for s in started] == ["danger_op"]
    assert [f["status"] for f in finished] == ["ok"]


def test_approval_deny_feeds_model(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("danger_op", target="prod")]},
        {"text": "好的,已取消该操作。"},
    ])
    registry = FakeRegistry([FakeTool("danger_op", approval=True, read_only=False,
                                      concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("执行危险操作")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))

    resumed = run(orch.resume_after_approval(session, approved=False,
                                             journal=session.journal, **GEN))

    assert resumed["status"] == "success"
    assert registry.executed == []                       # 拒绝 = 绝不执行
    tool_msgs = [m for m in session.history if m["role"] == "tool"]
    assert any("DENIED" in str(m) for m in tool_msgs)    # 拒绝结果反馈给模型


# ---------- 取消 / 挂起 ----------

def test_cancel_before_generation(make_session):
    engine = FakeEngine([])                              # 不该发生任何生成
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")
    begin_turn(session)
    session.cancel_requested = True

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "cancelled"
    assert engine.calls == []


def test_cancel_during_generation(make_session):
    engine = FakeEngine([{"cancelled": True}])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "cancelled"


def test_suspend_at_iteration_boundary(make_session):
    engine = FakeEngine([{"tool_calls": [call("read_thing")]}])
    registry = FakeRegistry([FakeTool("read_thing")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("做点事")
    begin_turn(session)

    async def scenario():
        # 第一迭代(工具执行)后置位挂起 → 循环在下一个迭代边界让出
        async def flag_after_first():
            while not registry.executed:
                await asyncio.sleep(0)
            session.suspend_requested = True
        task = asyncio.create_task(flag_after_first())
        r = await orch.run_turn(session, journal=session.journal, **GEN)
        await task
        return r

    result = run(scenario())

    assert result["status"] == "suspended"
    assert session.suspend_requested is False            # 标志被消费
    assert session.activity["phase"] == "suspended"
    assert len(engine.calls) == 1                        # 未再多转一圈生成


# ---------- 护栏:重复调用 / 解析断环 / 截断恢复 / 验证门控 ----------

def test_duplicate_call_blocked(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/a")]},
        {"tool_calls": [call("read_thing", path="/a")]},   # 同名同参复读
        {"text": "汇总完成。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing", result="data")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("看两眼")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert len(registry.executed) == 1                   # 第二次被拦,未真执行
    dup_feedback = [tr for m in session.history if m["role"] == "tool"
                    for tr in m["tool_responses"] if "Duplicate call blocked" in tr["response"]]
    assert len(dup_feedback) == 1


def test_write_barrier_allows_reread(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/a")]},
        {"tool_calls": [call("write_thing", path="/a")]},  # 写调用 → 解禁 r: 指纹
        {"tool_calls": [call("read_thing", path="/a")]},   # 读-改-再读:应放行
        {"text": "验证通过。"},
    ])
    registry = FakeRegistry([
        FakeTool("read_thing", result="v1"),
        FakeTool("write_thing", result="written", read_only=False, concurrency_safe=False),
    ])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("改后复查")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert [e["name"] for e in registry.executed] == ["read_thing", "write_thing", "read_thing"]


def test_parse_error_loop_aborts(make_session):
    engine = FakeEngine([{"tool_calls": [bad_call()]} for _ in range(3)])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("hi")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "warning"
    assert "解析" in result["response"]                   # 优雅终止而非烧光预算
    assert len(engine.calls) == 3                        # max_parse_retries 断环


def test_truncation_escalates_then_recovers(make_session):
    engine = FakeEngine([
        {"text": "前半截", "truncated": True},
        {"text": "完整的答案。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("长答复")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "完整的答案。"
    # 升档重试:第二次生成的 max_tokens 被抬到 ESCALATED,半截产出被丢弃不入历史
    assert engine.calls[1]["kwargs"]["max_tokens"] > GEN["max_tokens"]
    assistant_texts = [m.get("content") for m in session.history if m["role"] == "assistant"]
    assert assistant_texts == ["完整的答案。"]


def test_verify_nudge_on_unverified_write(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("write_file", path="/a", content="x")]},
        {"text": "已修好。"},        # 写后未复查即收尾 → 推回一次
        {"text": "已修好并验证。"},  # 推回预算用尽 → 放行
    ])
    registry = FakeRegistry([FakeTool("write_file", result="ok",
                                      read_only=False, concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("修一下")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "已修好并验证。"
    # 推回经可变尾部注入(不进历史):第三次生成应看到验证提醒 tail_note
    assert any("verify" in n.lower() for n in engine.calls[2]["tail_notes"])


# ---------- 计划元工具与提前收尾门控 ----------

def test_plan_flow_and_premature_finish_gate(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["查资料", "写结论"])]},
        {"text": "先说到这。"},                                    # 计划未完 → 推回
        {"tool_calls": [call("complete_step", result="资料已查"),
                        call("complete_step", result="结论已写")]},
        {"text": "全部完成:结论如下。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("做个两步任务")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "全部完成:结论如下。"
    assert session.task_state is None                    # 完成即弃(blackboard=消息流)
    # 提前收尾推回走 tail_note,第三次生成能看到
    assert any("unfinished steps" in n for n in engine.calls[2]["tail_notes"])
    # 账本:计划快照出现过两步计划;两次 step_completed 且标题按序
    snapshots = [d["task"] for d in ring_of(session, "plan_updated") if d.get("task")]
    assert any(len(s.get("plan", [])) == 2 for s in snapshots)
    steps = ring_of(session, "step_completed")
    assert [s["title"] for s in steps] == ["查资料", "写结论"]
    assert [s["result"] for s in steps] == ["资料已查", "结论已写"]


def test_remember_carries_value_into_later_step(make_session):
    """记事板端到端:某步 remember 的值,在之后的迭代里仍出现在 task_block 中喂回模型。"""
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["拿到接口地址", "用地址取数"])]},
        {"tool_calls": [call("remember", note="API endpoint = https://api.example.com/v2"),
                        call("complete_step", result="拿到地址")]},
        {"tool_calls": [call("complete_step", result="取数完成")]},
        {"text": "全部完成。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("两步任务,第二步要用第一步的地址")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    # 第三次生成(index 2)的任务块应携带第二步存下的便签——跨步可见即记事板的意义
    assert "https://api.example.com/v2" in (engine.calls[2]["task_block"] or "")
    # remember 的回执确实反馈给了模型
    remembered = [tr for m in session.history if m["role"] == "tool"
                  for tr in m.get("tool_responses", []) if tr.get("name") == "remember"]
    assert remembered and "scratchpad" in remembered[0]["response"].lower()
    # 账本:plan_updated 快照带上了 notes(snapshot 口径统一)
    snaps = [d["task"] for d in ring_of(session, "plan_updated") if d.get("task")]
    assert any("API endpoint = https://api.example.com/v2" in s.get("notes", []) for s in snaps)


def test_remember_without_plan_redirects(make_session):
    """无活跃计划时 remember 被重定向,不创建任务、不污染单步问答。"""
    engine = FakeEngine([
        {"tool_calls": [call("remember", note="something")]},
        {"text": "好的。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("随口记一下")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert session.task_state is None                    # 未因 remember 造出空任务
    redirects = [tr for m in session.history if m["role"] == "tool"
                 for tr in m.get("tool_responses", []) if tr.get("name") == "remember"]
    assert redirects and "No active plan" in redirects[0]["response"]


def test_completion_reminder_references_goal(make_session):
    """想法3:全部步骤完成时的收尾提醒引用用户最初目标 + 目标对齐语义(防小模型漂移)。"""
    orch = make_orch(FakeEngine([]), FakeRegistry([]))
    task = TaskState(goal="把下载文件夹按类型整理好")
    task.plan = [{"id": i, "title": f"step{i}", "status": "done", "note": ""} for i in range(1, 4)]
    assert task.all_done()

    msg = orch._structural_reminder(task, did_complete_step=True)

    assert msg is not None
    assert "整理" in msg                                    # 引用了原始目标文本
    assert "ACHIEVES" in msg or "achieve" in msg.lower()    # 目标对齐(非仅"已验证")


def test_total_iteration_fuse(make_session):
    engine = FakeEngine([{"tool_calls": [call("read_thing", i=i)]} for i in range(12)])
    registry = FakeRegistry([FakeTool("read_thing")])
    orch = make_orch(engine, registry, max_total_iterations=12)
    session = make_session()
    session.add_user_message("转圈")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "warning"
    assert "12" in result["response"]                    # 总熔断兜底,优雅收尾


# ---------- 并发只读批 ----------

def test_concurrent_readonly_batch_order_preserved(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/a"), call("read_thing", path="/b"),
                        call("write_thing", path="/c")]},
        {"text": "齐活。"},
    ])
    registry = FakeRegistry([
        FakeTool("read_thing", result="r"),
        FakeTool("write_thing", result="w", read_only=False, concurrency_safe=False),
    ])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("批量")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    # 写调用前必须先冲刷只读批:两个 read 都在 write 之前执行
    names = [e["name"] for e in registry.executed]
    assert names.index("write_thing") == 2
    # 响应与调用一一对应、按声明序回填
    tool_msg = next(m for m in session.history if m["role"] == "tool")
    assert [tr["name"] for tr in tool_msg["tool_responses"]] \
        == ["read_thing", "read_thing", "write_thing"]


# ---------- 无计划收敛提醒(NO_PLAN_NUDGE_ITERATIONS) ----------

def test_no_plan_nudge_after_three_unplanned_calls(make_session):
    """实测事故(会话 e2348829):多件套任务无计划连打工具后以"承诺继续"文本收尾,
    旧阈值(react_max_steps+2=6)永远够不着。3 次无计划工具迭代须提醒一次:
    建计划或直接作答——这是唤醒记事板/提前收尾推回/goal_verify 的唯一入口。"""
    engine = FakeEngine([
        {"tool_calls": [call("probe", i=1)]},
        {"tool_calls": [call("probe", i=2)]},
        {"tool_calls": [call("probe", i=3)]},
        {"text": "查完了,结论如下。"},
    ])
    orch = make_orch(engine, FakeRegistry([FakeTool("probe")]))
    session = make_session()
    session.add_user_message("多查几处再说")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    nudges = [m for m in session.history
              if m.get("role") == "system" and "without a plan" in str(m.get("content"))]
    assert len(nudges) == 1


def test_no_plan_nudge_absent_for_short_tool_runs(make_session):
    """两次以内的无计划工具调用是正常轻任务形态,不得打扰。"""
    engine = FakeEngine([
        {"tool_calls": [call("probe", i=1)]},
        {"tool_calls": [call("probe", i=2)]},
        {"text": "两步答完。"},
    ])
    orch = make_orch(engine, FakeRegistry([FakeTool("probe")]))
    session = make_session()
    session.add_user_message("小事")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert not [m for m in session.history
                if m.get("role") == "system" and "without a plan" in str(m.get("content"))]


# ---------- 全思考空产出推回(T1-5) ----------

def test_thinking_only_output_pushed_back_once(make_session):
    """整段生成全进思考频道(典型:无收尾频道块被解析器 §2.7 路由)→ 用户什么都看不到。
    须推回一次要求产出可见答复,思考留档进历史;而不是静默空转烧到总熔断。"""
    engine = FakeEngine([
        {"thoughts": ["分析都在这里,正文忘了写"], "text": ""},
        {"text": "这是补上的可见答复。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("问点事")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert result["response"] == "这是补上的可见答复。"
    assert any("entirely into thinking" in n for n in engine.calls[1]["tail_notes"])
    # 思考已留档,续跑可直接引用其中的分析
    assert any(m.get("thoughts") for m in session.history if m.get("role") == "assistant")


# ---------- 步内执行数事实标记(complete_step 虚报红旗) ----------

def test_complete_step_marks_zero_tool_executions(make_session):
    """实测事故(复测会话 d618f0d6):step 零物理调用却宣称"已向数据库插入记录"。
    complete_step 须在 note 落 [tool calls: 0] 事实标记,反馈里提醒模型自查。"""
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["读文件", "执行操作"])]},
        {"tool_calls": [call("probe", path="/tmp/x")]},           # 步1:真执行了 1 次
        {"tool_calls": [call("complete_step", result="文件已读")]},
        {"tool_calls": [call("complete_step", result="操作已执行")]},  # 步2:零执行就宣布完成
        {"text": "都做完了。"},
    ])
    orch = make_orch(engine, FakeRegistry([FakeTool("probe")]))
    session = make_session()
    session.add_user_message("两步任务")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    snapshots = [d["task"] for d in ring_of(session, "plan_updated") if d.get("task")]
    final = snapshots[-1]["plan"]
    assert "[tool calls: 1]" in final[0]["note"]          # 真执行的步:如实记 1
    assert "[tool calls: 0]" in final[1]["note"]          # 零执行的步:红旗落痕
    # 零执行完成的反馈里带自查提醒(喂回模型的 tool response)
    zero_warns = [m for m in session.history if m.get("role") == "tool"
                  and "ZERO tools" in str(m.get("tool_responses"))]
    assert len(zero_warns) == 1


# ---------- 参数预检(validate_call):必败调用不进审批不执行 ----------

def test_precheck_rejects_before_approval(make_session):
    """实测事故(d618f0d6):引号不配对的 shell 命令通过审批、执行必败、修正后再骗一次
    审批点击。带 validate 的工具:坏参数就地打回模型,绝不挂起审批、绝不执行。"""
    def quote_check(args):
        if "'" in str(args.get("command", "")):
            return "System Error: shell syntax error (unmatched quote) — fix and re-issue."
        return None

    engine = FakeEngine([
        {"tool_calls": [call("shellish", command="mkdir -p ~/x')")]},   # 必败:预检打回
        {"tool_calls": [call("shellish", command="mkdir -p ~/x")]},     # 修正:正常走审批
        {"text": "建好了。"},
    ])
    registry = FakeRegistry([FakeTool("shellish", result="ok", approval=True,
                                      read_only=False, concurrency_safe=False,
                                      validate=quote_check)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("建个目录")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    # 第一次调用被预检打回:没执行、没挂审批;第二次(合法)才挂起等审批
    assert result["status"] == "pending_approval"
    assert result["pending_tools"] == [{"name": "shellish",
                                        "arguments": {"command": "mkdir -p ~/x"}}]
    assert registry.executed == []
    # 预检错误已作为工具响应喂回模型(第二次生成读到)
    fed = [m for m in session.history if m.get("role") == "tool"
           and "unmatched quote" in str(m.get("tool_responses"))]
    assert len(fed) == 1

    resumed = run(orch.resume_after_approval(session, approved=True,
                                             journal=session.journal, **GEN))
    assert resumed["status"] == "success"
    assert registry.executed == [{"name": "shellish", "args": {"command": "mkdir -p ~/x"}}]


# ---------- 失败路由菜单:Split 选项(范围过大的步) ----------

def test_failure_route_menu_offers_split(make_session):
    """两轮复测(d618f0d6/78126c52)证实:「逐页读完 103 页」类范围过大的步在预算内
    结构性必败,原菜单(换法/绕开/降级/问用户)无"拆小"出路,模型只能落到问用户。"""
    engine = FakeEngine([
        {"tool_calls": [call("update_plan", op="replace", steps=["读完全部日志"])]},
        {"tool_calls": [call("probe", i=1)]},
        {"tool_calls": [call("probe", i=2)]},
        {"tool_calls": [call("probe", i=3)]},
        {"tool_calls": [call("probe", i=4)]},      # 第 4 次迭代耗尽步内预算 → 失败路由
        {"text": "步骤受阻,我拆小重试。"},
    ])
    orch = make_orch(engine, FakeRegistry([FakeTool("probe")]))
    session = make_session()
    session.add_user_message("读日志")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    menus = [m for m in session.history if m.get("role") == "system"
             and "Choose your next strategy" in str(m.get("content"))]
    assert len(menus) == 1
    menu = str(menus[0]["content"])
    assert "Split" in menu and "TOO BIG" in menu
    assert "E. Only if none of the above" in menu   # 重排后的兜底项仍在
    # 账本:步骤失败事件照常
    fails = ring_of(session, "step_failed")
    assert len(fails) == 1 and "读完全部日志" in fails[0]["title"]
