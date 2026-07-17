"""观察截断头+尾保留式金测试（grok-build 评估采纳，2026-07-18）。

守护的失败形态：旧实现只保头部——shell/日志类输出的错误行多在尾部，一条超长
构建日志被截后模型只看到开头的正常输出，误判「构建成功」。头 2/3 + 尾 1/3 +
中缝标注原始体积与收窄取回出路（S.3：给路不给谜语）。
"""
import asyncio

from framework.core.journal import new_turn_id

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import make_orch


def test_truncation_keeps_head_and_tail(make_session):
    body = "HEAD" + "x" * 500 + "TAIL_ERROR_LINE"
    tool = FakeTool("t_long", result=body)
    orch = make_orch(FakeEngine([]), FakeRegistry([tool]), observation_max_chars=120)
    session = make_session()
    session.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    obs = asyncio.run(orch._invoke_tool(session, call("t_long"), "c1", session.journal))
    out = obs["response"]
    assert out.startswith("HEAD")
    assert out.endswith("TAIL_ERROR_LINE")           # 尾部错误行活下来
    assert f"{len(body)} chars total" in out         # 报原始体积
    assert "narrower slice" in out                   # 给收窄取回的出路


def test_under_cap_untouched(make_session):
    tool = FakeTool("t_ok", result="short output")
    orch = make_orch(FakeEngine([]), FakeRegistry([tool]), observation_max_chars=120)
    session = make_session()
    session.journal.emit("turn_queued", {"prompt": "p"}, turn_id=new_turn_id())
    obs = asyncio.run(orch._invoke_tool(session, call("t_ok"), "c2", session.journal))
    assert obs["response"] == "short output"
