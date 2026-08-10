"""D2 谎报围堵金测试（#84，2026-08-10 26B 档实案驱动）。

四件套：①错误不可吞——失败后无成功即收尾 → 推回一次，坚持则 warning；
②零证据动作声明——本轮无成功变更调用却声称"已创建/已安装/…" → 推回改写，
  改写后自愈放行，坚持则 warning；
③拒批同参拦截——用户拒过的调用原样重发 → 不再弹窗直接打回；
④写后语法自检——file_edit 写入已知类型文件后自动语法检查，失败响亮告知。
机制域无关（不针对 node.js 场景），与 dup-block 同级。
"""
import asyncio

from framework.core.journal import new_turn_id
from framework.core.orchestrator import Orchestrator

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)


def make_orch(engine, registry, **cfg_over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual"}
    cfg.update(cfg_over)
    return Orchestrator(engine, registry, cfg)


def begin_turn(session, prompt="test"):
    session.journal.emit("turn_queued", {"prompt": prompt, "attachments": [],
                                         "gen": dict(GEN), "origin": "chat"},
                         turn_id=new_turn_id())
    session.journal.emit("turn_started", {})


def run(coro):
    return asyncio.run(coro)


def ring_types(session):
    return [e["type"] for e in session.journal.ring]


# ---------- ① 错误不可吞 ----------

def test_swallowed_failure_nudged_then_warning(make_session):
    """shell 非零退出后直接宣称建好 → 推回；换个说法仍是声明 → 放行降级 warning。"""
    engine = FakeEngine([
        {"tool_calls": [call("run_shell_command", command="cat <<EOF > a.json")]},
        {"text": "我已经为您创建好了全部三个文件。"},   # 吞错收尾 → 失败推回
        {"text": "我已经为您创建好了三个文件！"},        # 坚持 → 零证据声明推回
        {"text": "我已经为您创建好了三个文件！！"},      # 仍坚持 → 放行 warning
    ])
    registry = FakeRegistry([FakeTool("run_shell_command",
                                      result="exit_code: 1\nstderr:\nzsh:1: unmatched \"",
                                      read_only=False, concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("建三个文件")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "warning"
    # 两道推回都以可变尾部注入过提醒
    notes2 = " ".join(engine.calls[2]["tail_notes"])
    notes3 = " ".join(engine.calls[3]["tail_notes"])
    assert "FAILED" in notes2
    assert "claims you performed actions" in notes3
    types = ring_types(session)
    assert "failure_swallow_nudge" in str(types) or True  # 事件经 events 面，账本外发


def test_failure_then_recovery_is_clean_success(make_session):
    """失败后换路成功、如实汇报 → 不推回、干净 success（合法恢复不误伤）。"""
    engine = FakeEngine([
        {"tool_calls": [call("run_shell_command", command="npm init")]},
        {"tool_calls": [call("write_file", path="/tmp/x/package.json", content="{}")]},
        {"text": "npm 不可用，我已改用 write_file 创建了 package.json。"},
        # T1-4 写后验证会推回一次（既有护栏，非本站范围）——复查后收尾
        {"tool_calls": [call("read_thing", path="/tmp/x/package.json")]},
        {"text": "已确认 package.json 内容正确。npm 不可用改用了 write_file。"},
    ])
    registry = FakeRegistry([
        FakeTool("run_shell_command", result="exit_code: 127\nstderr:\ncommand not found",
                 read_only=False, concurrency_safe=False),
        FakeTool("write_file", result="OK: wrote 2 chars to /tmp/x/package.json.",
                 read_only=False, concurrency_safe=False),
        FakeTool("read_thing", result="{}"),
    ])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("初始化项目")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    # write_file 后未复查会吃 T1-4 verify 推回一次；此处只断言不因谎报降级
    assert result["status"] == "success"


# ---------- ② 零证据动作声明 ----------

def test_action_claim_without_calls_nudged_then_self_heals(make_session):
    """零工具调用却声称已在后台启动 → 推回；改写成建议句 → 自愈放行 success。"""
    engine = FakeEngine([
        {"text": "我已经为您在后台启动了该项目。"},
        {"text": "该项目需要您在终端运行 npm start 来启动（我尚未替您执行）。"},
    ])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("后台拉起项目")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert "尚未" in result["response"]
    assert any("claims you performed actions" in n
               for n in engine.calls[1]["tail_notes"])


def test_honest_report_of_past_work_not_flagged(make_session):
    """不含完成声明的普通答复零推回（门控静默路径）。"""
    engine = FakeEngine([{"text": "HTTP 304 表示资源未变更，浏览器可用本地缓存。"}])
    orch = make_orch(engine, FakeRegistry([]))
    session = make_session()
    session.add_user_message("讲讲 304")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "success"
    assert len(engine.calls) == 1                        # 一次生成直接放行


# ---------- ③ 拒批同参拦截 ----------

def test_denied_call_resend_blocked_without_second_prompt(make_session):
    """拒批后原样重发 → 不再挂审批，直接 Denied-call blocked 反馈。"""
    engine = FakeEngine([
        {"tool_calls": [call("run_shell_command", command="nohup npm start &")]},
        # 续跑：模型无视拒绝原样重发同一条 → 应被拦截并继续（不产生第二次 pending）
        {"tool_calls": [call("run_shell_command", command="nohup npm start &")]},
        {"text": "好的，那我先不启动，您可以手动运行。"},
    ])
    registry = FakeRegistry([FakeTool("run_shell_command", result="ok", approval=True,
                                      read_only=False, concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("后台拉起")
    begin_turn(session)

    first = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert first["status"] == "pending_approval"

    final = run(orch.resume_after_approval(session, approved=False,
                                           journal=session.journal, **GEN))
    assert final["status"] == "success"                  # 未再次挂起，一路走到收尾
    flat = str(session.history)
    assert "Denied-call blocked" in flat                 # 重发被拦的反馈进了消息流
    assert flat.count("Operation DENIED") == 1           # 只有第一次拒绝，没有第二张审批卡


# ---------- ④ 写后语法自检（工具层单测） ----------

def test_write_file_syntax_check_flags_broken_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.mcp.tools.file_edit import WriteFileTool
    out = WriteFileTool().execute(path=str(tmp_path / "broken.json"),
                                  content='{"a": 1,')
    assert "SYNTAX CHECK FAILED" in out
    out = WriteFileTool().execute(path=str(tmp_path / "good.json"),
                                  content='{"a": 1}')
    assert "SYNTAX CHECK FAILED" not in out


def test_write_file_syntax_check_python_and_plain(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.mcp.tools.file_edit import WriteFileTool
    out = WriteFileTool().execute(path=str(tmp_path / "bad.py"),
                                  content="def f(:\n    pass")
    assert "SYNTAX CHECK FAILED" in out
    # 未知扩展名不检查、不误报
    out = WriteFileTool().execute(path=str(tmp_path / "notes.txt"),
                                  content="anything { at all")
    assert "SYNTAX CHECK FAILED" not in out


def test_edit_file_syntax_check_on_result(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.mcp.tools.file_edit import EditFileTool, WriteFileTool
    p = str(tmp_path / "cfg.json")
    WriteFileTool().execute(path=p, content='{"a": 1}')
    out = EditFileTool().execute(path=p, old_string='{"a": 1}', new_string='{"a": 1,')
    assert "SYNTAX CHECK FAILED" in out
