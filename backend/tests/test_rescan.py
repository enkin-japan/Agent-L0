"""工具注册表热重扫金测试（D 阶段工程票）。

覆盖：①只增不删的重探（探活翻绿补注册、翻红者留档下次再试、异常按不可用）；
②编排层声明/阈值随重扫刷新（refresh_tools_declaration 与 __init__ 同一路径）；
③turn_runner 前置重扫接线（点名不可用剧本 → 先重扫再分发）；
④runtime 钩子未登记时安全空转。
"""
import asyncio

from framework.core.orchestrator import Orchestrator
from framework.mcp.base import BaseTool
from framework.mcp.registry import MCPRegistry

from .fake_engine import FakeEngine, FakeRegistry, FakeTool


class _FlakyTool(BaseTool):
    """探活可翻转的工具替身（LibreOffice 装好前后）。"""
    name = "flaky"
    description = "flaky tool"
    parameters = {"properties": {}, "required": []}
    alive = False

    def is_available(self):
        return type(self).alive

    def execute(self):
        return "ok"


class _BoomTool(BaseTool):
    name = "boom"
    description = "probe raises"
    parameters = {"properties": {}, "required": []}

    def is_available(self):
        raise RuntimeError("probe exploded")

    def execute(self):
        return "ok"


def _registry(tmp_path) -> MCPRegistry:
    return MCPRegistry(tools_dir=tmp_path / "no-tools-dir-scan")


def test_rescan_registers_newly_available_only(tmp_path):
    reg = _registry(tmp_path)
    _FlakyTool.alive = False
    assert reg.load_tool(_FlakyTool()) is False
    assert reg.load_tool(_BoomTool()) is False  # 目录扫描外的补注册失败同样留档
    assert reg.tool_names() == set()

    assert reg.rescan_unavailable() == []      # 依赖仍缺：不注册、不丢档
    _FlakyTool.alive = True
    assert reg.rescan_unavailable() == ["flaky"]
    assert "flaky" in reg.tool_names()
    assert reg.rescan_unavailable() == []      # 已注册者不重复；boom 仍留档
    assert len(reg._discarded) == 1 and reg._discarded[0].name == "boom"


def test_orchestrator_refresh_follows_registry_growth():
    engine = FakeEngine([])
    reg = FakeRegistry([FakeTool("a")])
    orch = Orchestrator(engine, reg, {"summary_high_threshold": 8000,
                                      "summary_low_threshold": 2000})
    before_est = orch.stable_prefix_tokens_est
    before_decl = len(orch._tools_decl)

    reg._tools["b"] = FakeTool("b")            # 模拟重扫补注册
    reg_schema = {"name": "b", "description": "fake tool b", "parameters": {"properties": {}}}
    reg.get_all_schemas().append(reg_schema)
    orch.refresh_tools_declaration()

    assert len(orch._tools_decl) == before_decl + 1
    assert orch.stable_prefix_tokens_est > before_est
    # 阈值从基值重锚而非累加（连续刷新不得漂移）
    assert orch.summary_high_threshold == 8000 + orch.stable_prefix_tokens_est
    orch.refresh_tools_declaration()
    assert orch.summary_high_threshold == 8000 + orch.stable_prefix_tokens_est


def test_runtime_hook_default_noop_and_wiring():
    from framework.agents import runtime
    old = runtime._RESCAN
    try:
        runtime._RESCAN = None
        assert runtime.rescan_tools() == {"newly_available_tools": [],
                                          "skills_available": []}
        runtime.set_rescan_hook(lambda: {"newly_available_tools": ["x"],
                                         "skills_available": ["s"]})
        assert runtime.rescan_tools()["newly_available_tools"] == ["x"]
    finally:
        runtime._RESCAN = old


def test_turn_runner_prescans_before_dispatch():
    """接线断言：accept_turn 在分发前对「点名了不可用剧本」走 rescan_tools。"""
    import inspect
    from framework.core import turn_runner
    src = inspect.getsource(turn_runner)
    assert "slash_name(" in src and "rescan_tools" in src
    idx_rescan = src.index("rescan_tools")
    idx_dispatch = src.index("dispatch_slash_skill(prompt")
    assert idx_rescan < idx_dispatch  # 先重扫后分发


def test_slash_name_helper():
    from framework.core.skill_registry import slash_name
    assert slash_name("/chart-report 画个图") == "chart-report"
    assert slash_name("  /deck") == "deck"
    assert slash_name("普通文本 /deck") is None
    assert slash_name("/Bad") is None


def test_rescan_endpoint_registered():
    from framework.api.routes import router
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/v1/tools/rescan" in paths
