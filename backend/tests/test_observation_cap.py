"""观察截断头+尾保留式 + 超长观察落盘回取（grok-build 评估采纳 2026-07-18；波 C1 2026-08-18）。

守护的失败形态：
1. 旧实现只保头部——shell/日志类输出的错误行多在尾部，一条超长构建日志被截后模型只
   看到开头的正常输出，误判「构建成功」。故保头 2/3 + 尾 1/3。
2. **被砍掉的中段在系统里任何一处都不存在**（账本对 tool_result 还要再截一次，
   `ToolOutput.payload` 只服务渲染面、模型够不到）。而中缝原本那句
   「re-fetch a narrower slice (page/start/end/limit args)」对桌面任务最盛产的那几类
   超长观察——AX 树、shell 输出、剪贴板——**根本是空头支票**：它们压根不支持取更窄的
   一片。故截断前把原文落盘，中缝给一条真能走通的路（S.3：给路不给谜语）。
3. **落不了盘时绝不谎称落了**：给模型一个不存在的路径比不给更毒——它会照着调一次
   `file_extract`、拿回 not found，然后要么重跑整个工具要么放弃。
4. **句柄必须真的可用**：首审实测出三种「看着有路、走不通」的形态——缺省 max_chars 是
   观察上限的三倍（回来的东西又被截断、又落一次盘，无限循环）、行号占位符被原样抄进
   调用（`int("<line>")` 抛异常）、单行载荷根本没有行可取。三条各有一条测试钉着。
"""
import asyncio

import pytest

from framework.core.journal import new_turn_id
from framework.core.spill import spill_observation, truncate_observation

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import make_orch


def _lines(n, prefix="line"):
    return "".join(f"{prefix}{i}\n" for i in range(n))


def _run_tool(session, body, *, cap=120, call_id="c1"):
    orch = make_orch(FakeEngine([]), FakeRegistry([FakeTool("t_long", result=body)]),
                     observation_max_chars=cap)
    session.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    return asyncio.run(orch._invoke_tool(session, call("t_long"), call_id, session.journal))


def test_truncation_keeps_head_and_tail(make_session):
    body = "HEAD\n" + _lines(200) + "TAIL_ERROR_LINE"
    out = _run_tool(make_session(), body)["response"]
    assert out.startswith("HEAD")
    assert out.endswith("TAIL_ERROR_LINE")           # 尾部错误行活下来
    assert f"({len(body)} chars total)" in out       # 报原始体积


def test_under_cap_untouched(make_session):
    session = make_session()
    obs = _run_tool(session, "short output", call_id="c2")
    assert obs["response"] == "short output"
    assert not (session.session_dir / "spill").exists()


# ---- ★ 落盘 + 回取句柄 ----

def test_the_omitted_middle_is_recoverable(make_session):
    """★ 病根取证：被砍掉的中段此前在系统里任何一处都不存在。"""
    session = make_session()
    body = "HEAD\n" + _lines(400) + "TAIL_ERROR_LINE"
    obs = _run_tool(session, body, call_id="abc12345")

    spill = session.session_dir / "spill" / "abc12345.txt"
    assert spill.exists()
    assert spill.read_text(encoding="utf-8") == body        # 逐字节原文，不是又一次截断
    assert "line200" not in obs["response"]                 # 中段确实不在观察里
    assert "line200" in spill.read_text(encoding="utf-8")   # 但取得回来
    assert str(spill) in obs["response"]                    # 句柄写在模型看得见的地方


def test_the_spill_file_is_not_world_readable(make_session):
    """★ 落进来的恰恰是最敏感的那一类观察：shell 输出（`env`、被要求查看的 `.env`）、
    剪贴板、显示着令牌的窗口 AX 树。会话目录的既有文件是 0644，这一类不跟那个惯例。"""
    session = make_session()
    _run_tool(session, "S\n" + _lines(300), call_id="0ff1ce01")
    spill = session.session_dir / "spill" / "0ff1ce01.txt"
    assert spill.stat().st_mode & 0o077 == 0, oct(spill.stat().st_mode)
    assert (session.session_dir / "spill").stat().st_mode & 0o077 == 0


def test_the_handle_carries_concrete_line_numbers_and_a_budget(make_session):
    """★ 三条实测过的「看着有路、走不通」全在这一条里钉住。

    ① `<line>` 占位符会被原样抄进调用，`int("<line>")` 抛异常、registry 把整条
       traceback 甩给模型；故行号必须是算好的整数。
    ② `file_extract` 文本分支缺省 `max_chars=12000`，是观察上限的三倍——模型照缺省调
       一次，回来的东西又被截断、又落一次盘（新 call_id = 新路径，重复调用阻断器都拦
       不住，因为参数变了），一个计划步的自纠预算就这么烧光；故句柄自带 max_chars。
    ③ 旧那句 `page/start/end/limit` 空头支票不许再出现。
    """
    out = _run_tool(make_session(), "H\n" + _lines(400), call_id="c3")["response"]
    assert "<line>" not in out
    assert "max_chars=" in out
    assert "page/start/end/limit" not in out
    import re
    m = re.search(r'file_extract\(path="[^"]+", start=(\d+), end=(\d+), max_chars=(\d+)\)', out)
    assert m, out
    start, end, budget = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert 0 < start < end
    assert budget <= 120                     # 不超过本次观察上限，否则回来又被截一次


def test_the_advertised_call_actually_returns_the_omitted_middle(make_session):
    """★ 端到端走一次模型会走的路：照句柄原样调 `file_extract`，必须真拿回中段，
    且**不再触发一次截断**。前面几条都只在验文本长什么样。"""
    session = make_session()
    body = "H\n" + _lines(2000) + "T"
    out = _run_tool(session, body, cap=4000, call_id="c0ffee01")["response"]

    import re
    m = re.search(r'file_extract\(path="([^"]+)", start=(\d+), end=(\d+), max_chars=(\d+)\)', out)
    assert m, out
    from framework.mcp.tools.file_extract import FileExtractTool
    got = FileExtractTool().execute(path=m.group(1), start=int(m.group(2)),
                                    end=int(m.group(3)), max_chars=int(m.group(4)))
    assert not got.startswith("System Error")
    assert "[Empty range" not in got
    # 中段真的回来了：line400 落在被砍掉的那一段里（头部只到 line34x）
    assert "line400" in got
    assert "line400" not in out                    # …而它确实不在观察正文里
    assert len(got) <= int(m.group(4)) + 200       # 不会再撞一次观察上限


def test_a_single_line_payload_says_the_line_handle_will_not_work(make_session):
    """★ 剪贴板整段、压缩过的 JSON/HTML、base64——这些没有换行，按行取回**根本不成立**
    （`read_text_range` 会回 `[Empty range: file has 1 lines]`）。给行号句柄就是把旧那句
    空头支票换了个更自信的说法，还顺手掐掉「重跑工具」这条仅剩的退路。"""
    session = make_session()
    out = _run_tool(session, "x" * 900, call_id="1eaf0001")["response"]
    assert "no line breaks" in out
    assert "file_extract(path=" not in out          # 不给一条走不通的路
    assert "do NOT re-run" not in out.lower().replace("do not re-run", "do NOT re-run")

    spill = session.session_dir / "spill" / "1eaf0001.txt"
    assert spill.read_text(encoding="utf-8") == "x" * 900      # 原文照样存着


def test_the_spill_file_lands_where_file_extract_can_read_it(make_session):
    """后缀取 `.txt` 让 `file_extract` 走文本分支（按行区间那条路）。"""
    session = make_session()
    got = spill_observation(session, "deadbeef", "a\nb\nc\n")
    assert got is not None
    p, saved = got
    assert p.endswith(".txt") and saved == 6

    from framework.mcp.tools.file_extract import FileExtractTool
    ranged = FileExtractTool().execute(path=p, start=2, end=2)
    assert not ranged.startswith("System Error")
    assert "| b" in ranged                    # 取到了第 2 行
    assert "| c" not in ranged                # 而且**只**取了第 2 行（否则测的不是区间）


def test_a_failed_spill_never_claims_a_path(make_session):
    """★ 落不了盘（磁盘满/权限）时中缝必须如实说没有。

    谎称有路径比不给更毒：模型会照着调一次 file_extract、拿回 not found，然后要么
    重跑整个工具（正是这条改动要省掉的开销）要么就此放弃中段。
    """
    # 真造一次写失败，而不是把整个 spill_observation 换掉——换掉的话它自己那条
    # 「失败就返回 None」的分支根本没被跑到，测的就不是它了（变异体 C1-3 存活即此因）。
    # 在 spill 目录该在的位置放一个**文件**：`mkdir(exist_ok=True)` 遇到同名文件会抛
    # FileExistsError，不依赖权限、不依赖跑测用户身份。
    session = make_session()
    (session.session_dir / "spill").write_text("not a dir", encoding="utf-8")
    obs = _run_tool(session, "y\n" + _lines(300), call_id="c4")
    assert "file_extract" not in obs["response"]
    assert "NOT saved" in obs["response"]
    assert "chars total" in obs["response"]          # 体积照报，只是没有出路


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "", "ZZZZ", "0" * 17, None])
def test_a_non_uuid_call_id_never_writes(make_session, bad):
    """call_id 是编排层 uuid4 自己生成的，穿越面本就不存在；这条只是纵深。"""
    assert spill_observation(make_session(), bad, "x" * 100) is None


def test_a_call_id_collision_never_overwrites(make_session):
    """★ call_id 只有 32 bit。撞了就用另一条观察的内容覆盖这一条——**有效路径配错误内容
    比 not found 更坏**，因为没有任何信号告诉模型它读到的是别的东西。"""
    session = make_session()
    p1, _ = spill_observation(session, "beefbeef", "FIRST\ncontent\n")
    p2, _ = spill_observation(session, "beefbeef", "SECOND\ncontent\n")
    assert p1 != p2
    assert open(p1, encoding="utf-8").read().startswith("FIRST")
    assert open(p2, encoding="utf-8").read().startswith("SECOND")


def test_a_session_over_its_spill_budget_stops_writing(make_session, monkeypatch):
    """★ `prune_expired_sessions` 对 `schedule_*` 与 pinned 会话一律跳过——那两类的
    spill 目录没有任何回收路径，一个每天跑的定时任务会永远往同一个目录里堆。
    超预算就不再落盘（中缝如实说没保存），**绝不删已经许诺过的文件**：删掉等于把句柄
    变成死路，而死路正是这条改动要消灭的东西。"""
    monkeypatch.setattr("framework.core.spill._SPILL_SESSION_BUDGET_BYTES", 50)
    session = make_session()
    first = spill_observation(session, "aaaa1111", "z" * 100)
    assert first is not None
    assert spill_observation(session, "bbbb2222", "z" * 100) is None
    from pathlib import Path
    assert Path(first[0]).exists()               # 先前那份不许被删
    assert not (session.session_dir / "spill" / "bbbb2222.txt").exists()


def test_an_enormous_observation_is_capped_and_says_so(make_session, monkeypatch):
    """原文已经在内存里了，落盘不是新增一个数量级的风险；但跑飞的 `find /` 不该在会话
    目录里留下无界的一份。上限之下如实说清只存了多少——别把「存了一部分」写成「全存了」。"""
    monkeypatch.setattr("framework.core.spill._SPILL_MAX_CHARS", 500)
    session = make_session()
    body = "z\n" * 1000
    p, saved = spill_observation(session, "cafe0001", body)
    assert saved == 500
    with open(p, encoding="utf-8") as f:
        assert len(f.read()) == 500
    out = truncate_observation(body, 120, (p, saved))
    assert "only the first 500 chars fit" in out


def test_a_zero_cap_disables_truncation_instead_of_returning_everything(make_session):
    """`observation_max_chars: 0` 时 tail_n=0，而 `text[-0:]` 返回的是**全文**——旧实现
    会在全文前面插一段中缝、并且把每一条观察都落一次盘。0 = 关掉。"""
    assert truncate_observation("a" * 500, 0, ("/tmp/x.txt", 500)) == "a" * 500
