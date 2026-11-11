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


@pytest.fixture(autouse=True)
def _isolate_search_latch(tmp_path):
    """搜索不可用闩锁（含确认查询的健康时间戳）是进程级状态：按测试复位，
    防一个测试上的闩漏进另一个文件，让 web_research 入口无端短路。"""
    from framework.agents import search_providers, web_tools
    web_tools.reset_search_latch()
    # 搜索 API 提供方（批 C）：数据根钉到每个测试自己的空临时目录 + 冷却 / 状态清零——
    # dev 数据根（backend/）里用户添加过的提供方不许漏进金测试（否则「没配 API」的
    # 批 B 回归会被真实配置改道，甚至真发出付费请求）。
    search_providers.reset_state()
    old_root = search_providers._root_override
    search_providers._root_override = tmp_path / "search-providers-isolated"
    yield
    search_providers._root_override = old_root
    search_providers.reset_state()
    web_tools.reset_search_latch()


class _MemorySecrets:
    """api_tool_factory 读写面的内存替身（只给 mail.settings 用）：自动隔离下的邮件设置
    密码读写全在内存里，不碰任何 .secrets.env 文件。"""

    def __init__(self):
        self.values = {}

    def read_secret(self, env_name):
        return self.values.get(env_name, "")

    def write_secret(self, env_name, value):
        self.values[env_name] = value


@pytest.fixture(autouse=True)
def _isolate_mail_settings(tmp_path):
    """邮件发送设置（send_email）：数据根钉到每个测试自己的空临时目录——dev 数据根（backend/）
    里用户保存过的联系人 / 用户名不许漏进金测试（未配置 = 工具不注册、预检拒发）；密码读写换成
    内存替身——任何测试（现在的、以后的）不经显式装置就碰不到真实密钥文件。要测真实文件读写
    的测试显式换回 mail_settings._default_factory 并把 AGENT_DATA_DIR 指向临时目录。"""
    from framework.mail import settings as mail_settings
    old_root, old_factory = mail_settings._root_override, mail_settings._factory
    mail_settings._root_override = tmp_path / "mail-settings-isolated"
    shim = _MemorySecrets()
    mail_settings._factory = lambda: shim
    yield
    mail_settings._root_override = old_root
    mail_settings._factory = old_factory


@pytest.fixture(autouse=True)
def _isolate_publish_settings(tmp_path):
    """页面发布设置（publish_page）：同上，数据根钉到每个测试自己的空临时目录。"""
    from framework.publish import settings as publish_settings
    old_root = publish_settings._root_override
    publish_settings._root_override = tmp_path / "publish-settings-isolated"
    yield
    publish_settings._root_override = old_root


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
