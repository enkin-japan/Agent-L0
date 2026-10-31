"""金测试公共装置。

测试直接驱动 framework 包(backend 根加入 sys.path),不加载任何模型权重:
引擎由 FakeEngine 脚本化替身扮演,推理路径零依赖 GPU——毫秒级、确定性,
是重整工程(REARCHITECTURE.md)动编排/路由不变量前的回归网。
"""
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from framework.core import events  # noqa: E402  (需先补 sys.path)


@pytest.fixture(autouse=True)
def _isolate_guardrail_events(tmp_path):
    """护栏事件流(guardrail_events.jsonl)按测试隔离,不污染仓库工作区。"""
    old = events.get_path()
    events.set_path(tmp_path / "guardrail_events.jsonl")
    yield
    events.set_path(old)


@pytest.fixture()
def make_session(tmp_path):
    """建一个落在临时目录的真实 ChatSession(会话目录结构照常生成)。"""
    from framework.memory.session import ChatSession

    def _make(session_id: str = "t-session", system_prompt: str = "你是测试助手。"):
        return ChatSession(session_id, tmp_path / "sessions", system_prompt)

    return _make


@pytest.fixture(autouse=True)
def _isolate_approval_policy():
    """审批策略持有者按测试钉在缺省（read/write 均 default = build 77 行为）：
    dev 数据根（backend/）里若有用户调过的 approval_policy.json，不许漏进金测试。"""
    from framework.core import approval_policy
    approval_policy.replace(approval_policy.DEFAULT)
    yield
    approval_policy.replace(approval_policy.DEFAULT)


@pytest.fixture()
def chart_caveat_live(monkeypatch):
    """B1 `vision_chart_numeric` 在 D27（2026-10-31）后降为 pending（26b-4bit 口径，待对新生产档
    重探），缺省不触发。caveat 机制本身（尾注文案、审计键、审批续跑）仍要守：本 fixture 把该条
    临时恢复为 confirmed，其余字段原样。"""
    import dataclasses

    import framework.cloud.boundary as bd
    b1 = bd.by_id("vision_chart_numeric")
    assert b1.status == "pending" and b1.action == "caveat"
    monkeypatch.setattr(bd, "CATALOG", tuple(
        dataclasses.replace(b, status="confirmed") if b.id == "vision_chart_numeric" else b
        for b in bd.CATALOG))
