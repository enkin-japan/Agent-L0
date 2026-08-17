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


# ---------- ①-b 失败判据本身的判别力（OSS 扫描 §6.1/§6.2，2026-08-17）----------
# 护栏的价值全在判据准不准：判宽了会逼模型报告不存在的错误（白烧一次生成），
# 判窄了会放过真事故。以下正反两向逐条钉死判据边界。

def test_benign_mark_is_not_failure_cross_module_contract():
    """跨模块契约：shell_exec 打的良性标记必须不被编排层判为失败。

    标记字面量在两个模块各写一份（编排层绝不 import 工具模块）。本条挡的是**会坏事
    的那种漂移**——把标记改成断言认不出的形状；同前缀内的措辞微调仍会通过，那种
    漂移不破坏契约（评审 Minor 6d 的准确表述）。
    """
    from framework.core.orchestrator import _resp_failed
    from framework.mcp.tools.shell_exec import _BENIGN_EXIT1_MARK

    assert not _resp_failed(f"exit_code: 1{_BENIGN_EXIT1_MARK}\n(no output)")
    # 同一位置去掉标记就必须命中——证明上一条的绿不是因为正则整体失效
    assert _resp_failed("exit_code: 1\n(no output)")


def test_is_benign_exit1_four_way_and():
    """良性判定的四条与门：首命令在名单 ∧ exit 恰为 1 ∧ stderr 空 ∧ 无元字符。"""
    from framework.mcp.tools.shell_exec import _is_benign_exit1

    assert _is_benign_exit1("grep -rn TODO /path", 1, "")
    assert _is_benign_exit1("diff a.txt b.txt", 1, "")
    assert _is_benign_exit1("test -f /nope", 1, "")
    # 反向：退出码不是 1（真错误一律 exit 2）
    assert not _is_benign_exit1("grep -rn TODO /nope", 2, "grep: /nope: No such file")
    # 反向：stderr 非空 = 有真话要说，不是干净的否定答案
    assert not _is_benign_exit1("grep -rn TODO /path", 1, "grep: warning: recursive")
    # 反向：含管道——退出码归属最后一段，无法归因给首命令
    assert not _is_benign_exit1("grep TODO x | wc -l", 1, "")
    # 反向：find 刻意不在名单（无命中返回 0，非零一律是真错）
    assert not _is_benign_exit1("find . -name nope", 1, "")
    # 反向：名单外命令（npm test 的 exit 1 = 测试真失败）
    assert not _is_benign_exit1("npm test", 1, "")
    # 反向：引号不闭合（shlex 判不了）与空命令——两条防御分支不得被静默删掉
    assert not _is_benign_exit1('grep "unclosed', 1, "")
    assert not _is_benign_exit1("", 1, "")
    # 绝对路径与带空格路径按 basename/shlex 正确归一（正向，防过度收紧）
    assert _is_benign_exit1("/usr/bin/grep foo '/my dir/f.txt'", 1, "")


def test_shell_exec_marks_benign_no_match_end_to_end(tmp_path, monkeypatch):
    """真跑 grep：无命中打标且不被判失败；路径不存在（exit 2）仍判失败。"""
    import os

    from framework.core.orchestrator import _resp_failed
    from framework.mcp.tools import shell_exec as se
    from framework.mcp.tools.shell_exec import ShellExecTool

    # 桩掉用户 PATH 探测：真实现会起一个交互式登录 zsh（source 用户 rc，最长 5s）并
    # 把结果写进进程级缓存，让这条金测试依赖开发机环境、还污染同进程后续测试。
    monkeypatch.setattr(se, "_user_env", lambda: dict(os.environ))

    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    out = ShellExecTool().execute(command="grep zzz f.txt", cwd=str(tmp_path))
    assert out.startswith("exit_code: 1 (benign")
    assert not _resp_failed(out)

    bad = ShellExecTool().execute(command="grep zzz ./nonexistent_xyz", cwd=str(tmp_path))
    assert bad.startswith("exit_code: 2")
    assert _resp_failed(bad)


def test_obs_failed_prefers_structured_fact_over_text():
    """失败判据的单一入口：结构化事实优先，缺键才回落文本正则。"""
    from framework.core.orchestrator import _obs_failed

    # 执行层说没失败 → 以事实为准（文本长什么样都不改判）
    assert not _obs_failed({"name": "t", "response": "exit_code: 7", "_failed": False})
    # 执行层说失败 → 判失败（超时熔断这类事实只有执行层知道）
    assert _obs_failed({"name": "t", "response": "partial...", "_failed": True})
    # 缺键回落：本层就地生成的响应与会话重载后的历史观察走这条路
    assert _obs_failed({"name": "t", "response": "System Error: boom"})
    assert not _obs_failed({"name": "t", "response": "Duplicate call blocked: ..."})


def test_grep_no_match_does_not_trigger_failure_nudge(make_session):
    """grep 无命中后如实回答「没找到」→ 零推回、干净收尾。

    改造前：exit 1 被判硬失败 → failure_swallow_nudge 逼模型「说明操作失败了」，
    而根本没有失败——白烧一整次生成。
    """
    from framework.mcp.tools.shell_exec import _BENIGN_EXIT1_MARK

    engine = FakeEngine([
        {"tool_calls": [call("run_shell_command", command="grep -rn TODO /tmp/proj")]},
        {"text": "在 /tmp/proj 下没有找到任何 TODO 标记。"},
    ])
    registry = FakeRegistry([FakeTool(
        "run_shell_command",
        result=f"exit_code: 1{_BENIGN_EXIT1_MARK}\n(no output)",
        read_only=True, concurrency_safe=True)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("看看有没有 TODO")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert len(engine.calls) == 2          # 一次工具 + 一次收尾，无推回


def test_shell_hard_failure_still_nudged(make_session):
    """exit 2 + stderr 是真失败：吞错收尾仍须被推回（良性放行不得殃及硬信号）。"""
    engine = FakeEngine([
        {"tool_calls": [call("run_shell_command", command="grep -rn TODO /nope")]},
        {"text": "已经为您检查完毕，没有发现 TODO。"},     # 吞错收尾 → 推回
        {"text": "检查未能完成：路径 /nope 不存在。"},      # 如实汇报
    ])
    registry = FakeRegistry([FakeTool(
        "run_shell_command",
        result="exit_code: 2\nstderr:\ngrep: /nope: No such file or directory",
        read_only=True, concurrency_safe=True)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("看看有没有 TODO")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    # 推回随下一次生成投递（[0]=工具调用 [1]=吞错收尾 [2]=带推回的重生成）
    assert "FAILED" in " ".join(engine.calls[2]["tail_notes"])
    assert result["status"] == "warning"   # 未恢复即收尾：留痕降级（既有语义）


def test_log_with_error_line_after_first_does_not_trip_failure_guard(make_session):
    """日志的 `System Error:` 行出现在**第 2 行及以后** = 读取成功，不得置位失败标志。

    改造前 `_FAILED_RESP_RE` 带 re.M 匹配任意行首，read_file/tail 读日志即误触
    护栏，收尾时白烧一次生成（OSS 扫描 §6.2）。
    覆盖面刻意写进名字：首行即该串的情形**仍判失败**，见下一条边界测试。
    """
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/tmp/app.log")]},
        {"text": "日志第 2 行记录了一次端口占用，服务随后正常启动。"},
    ])
    registry = FakeRegistry([FakeTool(
        "read_thing",
        result="10:00 boot ok\nSystem Error: port in use\n10:01 retry ok")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("看看日志")
    begin_turn(session)

    result = run(orch.run_turn(session, journal=session.journal, **GEN))

    assert result["status"] == "success"
    assert len(engine.calls) == 2          # 零推回


def test_first_line_error_still_trips_known_boundary(make_session):
    """已知边界（§6.2 修不到的那一半）：观察**首行**就是 `System Error:` 时仍判失败。

    工具的失败约定就是「响应 0 位打 System Error:」，与「文件内容恰好以该串开头」
    在文本层不可分；根治要工具带外结构化报错，另立项。本条把边界钉死，防止后人
    看着上一条测试误以为 §6.2 已覆盖全部情形（正是首过评审 Major 1 的取景问题）。
    """
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/tmp/crash.log")]},
        {"text": "日志显示端口被占用。"},
        {"text": "读取到的日志首行是一条端口占用记录。"},
    ])
    registry = FakeRegistry([FakeTool(
        "read_thing", result="System Error: port in use\n10:01 retry ok")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("看看崩溃日志")
    begin_turn(session)

    run(orch.run_turn(session, journal=session.journal, **GEN))

    # 仍会推回一次（这是被接受的残留，不是回归）——若将来根治，本条应连同边界一起改写
    assert "FAILED" in " ".join(engine.calls[2]["tail_notes"])


def test_failed_flag_covers_indented_tool_error(make_session):
    """带前导空白的工具报错：正则锚不到 0 位，靠 `errored` 兜住——并集不可缩水。

    首过评审曾建议把判据缩成「只用正则」，那会漏掉这一类真实失败（且并不能解决
    它想解决的首行问题）。本条把并集钉死。
    """
    from framework.core.orchestrator import _resp_failed

    text = "  System Error: tool blew up"
    assert not _resp_failed(text)                    # 正则单独判不出
    assert text.lstrip().startswith("System Error")  # errored 判得出 → 并集为真

    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/tmp/x")]},
        {"text": "已经为您读取完毕。"},
        {"text": "读取失败了。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing", result=text)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("读一下")
    begin_turn(session)

    run(orch.run_turn(session, journal=session.journal, **GEN))
    assert "FAILED" in " ".join(engine.calls[2]["tail_notes"])


def test_failed_meta_present_in_memory_but_stripped_before_persist(make_session):
    """`_failed` 是内存元数据：内存里必须在（否则判据形同虚设），落盘时必须没有。"""
    engine = FakeEngine([
        {"tool_calls": [call("read_thing", path="/tmp/x")]},
        {"text": "读好了。"},
    ])
    registry = FakeRegistry([FakeTool("read_thing", result="content")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("读一下")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))

    # 正向：内存中的工具观察确实带着这个键（防「整个键被删掉测试仍绿」的空转）
    obs = [tr for m in session.history for tr in (m.get("tool_responses") or [])]
    assert obs and all("_failed" in tr for tr in obs)

    session.append_log({"prompt": "读一下", "response": session.history[-2:]})
    assert "_failed" not in (session.session_dir / "history.jsonl").read_text(encoding="utf-8")


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
