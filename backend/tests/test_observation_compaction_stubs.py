"""机械压缩两个桩的「原文在这」尾巴（波 C1）。

`compact_tool_observations` 会把旧观察削成两种桩：取代桩（同一只读工具对同一目标的
重复读取，较旧那条被后来者取代）与预算 digest（总量越预算时按体量削）。C1 之后，
**真落过盘的**那些观察，桩里要带上回取句柄；没落过盘的绝不能带——给模型一个不存在的
路径比不给更毒，它会照着调一次 `file_extract`、拿回 not found，然后要么重跑整个工具
要么就此放弃。

句柄格式与编排层中缝**逐字同一种**：模型面只容忍一种回取方式，两种写法会让它在两条路
之间犹豫、或者两条都试。
"""
import re

from framework.core.spill import truncate_observation
from framework.memory.compactor import _spill_hint, compact_tool_observations


def _obs(name, resp, *, target=None, safe=False, spill=False):
    """spill=True 时把观察正文换成真的中缝（句柄的唯一来源就是它）。"""
    tr = {"name": name, "response": (_SEAM if spill else resp)}
    if safe:
        tr["_safe"] = True
    if target:
        tr["_target"] = target
    return tr


def _hist(*trs):
    return [{"role": "tool", "tool_responses": list(trs)}]


# ---- ★ 判据是这条观察自己的 _spill，不是「凡被压过的都给」 ----

_SEAM = truncate_observation("a\nb\nc\n" * 300, 120, ("/s/spill/x.txt", 1800))
_CALL = re.search(r'file_extract\([^)]+\)', _SEAM).group(0)


def test_no_spill_no_path():
    assert _spill_hint({"response": "x"}) == ""
    assert _spill_hint({"response": "普通观察，没有中缝"}) == ""


def test_spilled_observation_gets_the_handle():
    assert _CALL in _spill_hint({"response": _SEAM})


def test_a_single_line_spill_gets_no_handle():
    """★ 单行载荷落了盘但按行取不回来——编排层的中缝里刻意没有 `file_extract(...)`，
    桩这边就自然不会另造一条路。"""
    single = truncate_observation("x" * 900, 120, ("/s/spill/x.txt", 900))
    assert "no line breaks" in single
    assert _spill_hint({"response": single}) == ""


def test_the_stub_handle_is_byte_identical_to_the_seam_handle():
    """★ 两处必须是同一串句柄。句柄只有中缝那一份，桩从正文里原样捞回去——这条测试钉的
    就是「捞回原样」而不是「各拼一份」。"""
    assert _CALL in _SEAM
    assert _CALL in _spill_hint({"response": _SEAM})


# ---- 取代桩 ----

def test_superseded_stub_carries_the_path_when_it_was_spilled():
    hist = _hist(_obs("read_ui_tree", "A" * 500, target="win1", safe=True, spill=True),
                 _obs("read_ui_tree", "B" * 500, target="win1", safe=True))
    assert compact_tool_observations(hist, keep_recent=1, budget_chars=0) == 1
    old = hist[0]["tool_responses"][0]["response"]
    assert "superseded" in old
    assert _CALL in old
    # ★ 报的必须是**落盘前**的真实体积。取 len(response) 会报 500，紧挨着一条指向
    # 九万字文件的路径——两个数会互相打脸。
    assert "originally 1800 chars" in old


def test_superseded_stub_without_a_spill_offers_no_path():
    """★ 没落过盘的观察不许带路径——这条正是「比不给更毒」那一侧的取证。"""
    hist = _hist(_obs("read_ui_tree", "A" * 500, target="win1", safe=True),
                 _obs("read_ui_tree", "B" * 500, target="win1", safe=True))
    assert compact_tool_observations(hist, keep_recent=1, budget_chars=0) == 1
    old = hist[0]["tool_responses"][0]["response"]
    assert "superseded" in old
    assert "file_extract" not in old
    assert "on disk" not in old


# ---- 预算 digest 桩 ----

def test_budget_digest_carries_the_path_when_it_was_spilled():
    hist = _hist(_obs("shell_exec", "L" * 900, spill=True),
                 _obs("shell_exec", "S" * 10))
    assert compact_tool_observations(hist, keep_recent=1, digest_chars=20,
                                     budget_chars=100) == 1
    old = hist[0]["tool_responses"][0]["response"]
    assert "Observation compacted" in old
    assert _CALL in old
    assert "originally 1800 chars" in old


def test_budget_digest_without_a_spill_offers_no_path():
    """★ 预算驱逐削的多半是**没越过 observation_max_chars 的中等观察**——它们根本没有
    文件。这条桩最容易被写成「凡压过就给路径」，而那正好是最常见的假路径来源。"""
    hist = _hist(_obs("shell_exec", "L" * 900),
                 _obs("shell_exec", "S" * 10))
    assert compact_tool_observations(hist, keep_recent=1, digest_chars=20,
                                     budget_chars=100) == 1
    old = hist[0]["tool_responses"][0]["response"]
    assert "Observation compacted" in old
    assert "file_extract" not in old
    assert "on disk" not in old        # 连「原文还在盘上」这句都不许有：它根本不在


def test_the_protected_recent_window_is_never_stubbed():
    """最近 keep_recent 条是硬保护窗（保 KV 前缀复用与续跑连续性），压缩不许碰。"""
    hist = _hist(_obs("shell_exec", "L" * 900, spill=True))
    assert compact_tool_observations(hist, keep_recent=1, digest_chars=20,
                                     budget_chars=100) == 0
    assert hist[0]["tool_responses"][0]["response"] == _SEAM


# ---- ★ 端到端：桩里那条路径必须真的存在 ----

def test_a_real_spilled_observation_keeps_a_path_that_exists(make_session):
    """★ 前面几条都是手搭的 `_spill`；这条走真链路：`_invoke_tool` 落盘 → 压缩削桩 →
    桩里的路径**真的能打开**。

    中间任何一环断掉（编排层不挂 `_spill`、桩不读它、落盘路径与句柄不同源），单元测试
    各自都还绿，而模型拿到的是一条走不通的路——这正是「给一个不存在的路径比不给更毒」
    的完整形态，只有端到端这一条看得见。
    """
    import asyncio

    from framework.core.journal import new_turn_id

    from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
    from .test_orchestrator_lifecycles import make_orch

    body = "HEAD" + "".join(f"line{i}\n" for i in range(400)) + "TAIL"
    orch = make_orch(FakeEngine([]), FakeRegistry([FakeTool("t_long", result=body)]),
                     observation_max_chars=120)
    session = make_session()
    session.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    obs = asyncio.run(orch._invoke_tool(session, call("t_long"), "beef0001", session.journal))

    hist = _hist(obs, _obs("t_long", "S" * 10))
    assert compact_tool_observations(hist, keep_recent=1, digest_chars=20,
                                     budget_chars=100) == 1
    stub = hist[0]["tool_responses"][0]["response"]
    assert "Observation compacted" in stub

    import re
    m = re.search(r'file_extract\(path="([^"]+)"', stub)
    assert m, f"桩里没有可用的句柄：{stub}"
    from pathlib import Path
    p = Path(m.group(1))
    assert p.exists(), f"桩指向的路径不存在：{p}"
    assert p.read_text(encoding="utf-8") == body


# ---- ★ 冷恢复：`_` 前缀元数据落盘时被剥，桩要从正文里把句柄捞回来 ----

def test_a_cold_restored_observation_keeps_its_handle():
    """★ 重启后内存里没有 `_spill` 了（`_strip_internal_meta` 落盘时剥掉），而压缩桩
    恰恰要**覆盖掉正文**——中缝连同句柄一起被销毁。没有这条兜底的话，重启后第一次压缩
    就让盘上那份原文彻底失联：文件还在，上下文里再没有任何东西指向它。
    """
    restored = {"name": "shell_exec", "response": _SEAM}
    hist = _hist(restored, _obs("shell_exec", "S" * 10))
    assert compact_tool_observations(hist, keep_recent=1, digest_chars=20,
                                     budget_chars=100) == 1
    old = hist[0]["tool_responses"][0]["response"]
    assert _CALL in old
    assert "originally 1800 chars" in old     # 体积也从正文里捞回来了，不是截断后的长度


def test_a_cold_restored_observation_without_a_seam_gets_no_handle():
    """反向：正文里没有句柄就不许凭空造一个。"""
    hist = _hist({"name": "shell_exec", "response": "L" * 900},
                 _obs("shell_exec", "S" * 10))
    assert compact_tool_observations(hist, keep_recent=1, digest_chars=20,
                                     budget_chars=100) == 1
    old = hist[0]["tool_responses"][0]["response"]
    assert "file_extract" not in old
    assert "originally 900 chars" in old


# ---- ★ 分支：父会话被回收后，分支手里那条路径不能变成死路 ----

def test_a_branch_carries_the_spill_file_with_it(make_session, tmp_path):
    """★ 分支的 history.jsonl 原样带着父会话的绝对路径，而父子的 mtime 各自独立——
    `prune_expired_sessions` 可以先回收父、留下分支；用户也可以直接删掉父。分支于是
    握着一条指向虚空的路径，旁边还写着「不要重跑这个工具」。那正是这条改动要消灭的
    死路，只不过换到了分支上复发。
    """
    import asyncio
    import json
    import shutil

    from framework.core.journal import new_turn_id
    from framework.memory.session import SessionManager

    from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
    from .test_orchestrator_lifecycles import make_orch

    body = "H\n" + "".join(f"line{i}\n" for i in range(400)) + "T"
    parent = make_session("br-parent")
    orch = make_orch(FakeEngine([]), FakeRegistry([FakeTool("t_long", result=body)]),
                     observation_max_chars=120)
    parent.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    obs = asyncio.run(orch._invoke_tool(parent, call("t_long"), "5eed0001", parent.journal))
    parent.append_log({"prompt": "p", "status": "success",
                       "response": [{"role": "tool", "tool_responses": [obs]}]})

    mgr = SessionManager(parent.session_dir.parent)
    kept = parent.history_log.read_text(encoding="utf-8").splitlines()
    mgr._materialize_branch("br-parent", "br-child", kept, branch_point=0)

    child_line = (mgr.sessions_dir / "br-child" / "history.jsonl").read_text(encoding="utf-8")
    assert "br-parent/spill" not in child_line          # 路径已搬到分支自己名下
    assert "br-child/spill" in child_line

    shutil.rmtree(parent.session_dir)                   # 父会话被回收
    import re
    # 从 JSON 行里取回观察正文再抠路径（行内的引号是转义过的）
    turn = json.loads(child_line.splitlines()[0])
    resp = turn["response"][0]["tool_responses"][0]["response"]
    m = re.search(r'file_extract\(path="([^"]+)"', resp)
    assert m, resp
    from pathlib import Path
    assert Path(m.group(1)).exists()                    # 分支手里那条路径仍然走得通
    assert Path(m.group(1)).read_text(encoding="utf-8") == body
