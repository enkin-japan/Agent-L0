"""依赖时效巡检金测试（drift patrol phase 1，2026-07-18 采纳）。

守护的失败形态：①未声明 canary 的工具被拉进巡检（frozen 类依赖被无谓打探针）；
②单次网络抖动就告警吵人——阈值前不许写笔记；③恢复后告警笔记赖着不清，或巡检
删到自己前缀之外的笔记；④探针异常/超时掀翻空闲维护循环；⑤轮转失灵——同一项
被反复探测而其他项永远轮不到。
"""
import asyncio
import time

import pytest

from framework.agents import runtime
from framework.maintenance import drift_patrol as dp
from framework.mcp.base import BaseTool
from framework.memory.notes import NoteStore


class _PlainTool(BaseTool):
    name = "plain_tool"
    description = "无 canary 声明"
    def execute(self, **kw):
        return ""


def _make_canary_tool(tool_name, result):
    """result: (ok, detail) 元组 / 异常实例 / 可调用。"""
    class _T(BaseTool):
        name = tool_name
        description = "带探针"
        drift_class = "adversarial"
        drift_fix = "pip install -U something"
        calls = 0
        def execute(self, **kw):
            return ""
        def canary(self):
            type(self).calls += 1
            if isinstance(result, Exception):
                raise result
            return result() if callable(result) else result
    return _T()


class _Registry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = NoteStore(tmp_path / "notes")
    monkeypatch.setattr(runtime, "get_note_store", lambda: st)
    return st


def _patrol(reg, tmp_path, cfg=None):
    base = {"min_hours": 72, "fail_threshold": 2}
    return asyncio.run(dp.maybe_patrol(reg, {**base, **(cfg or {})}, tmp_path / "maint"))


# ---------- 目标圈定 ----------

def test_only_declared_tools_enter_patrol(store, tmp_path):
    plain, probed = _PlainTool(), _make_canary_tool("probed_tool", (True, "ok"))
    reg = _Registry([plain, probed])
    assert [t.name for t in dp.patrol_targets(reg)] == ["probed_tool"]
    res = _patrol(reg, tmp_path)
    assert res == ("probed_tool", True, "ok")


def test_disabled_runs_nothing(store, tmp_path):
    t = _make_canary_tool("a_tool", (True, "ok"))
    assert _patrol(_Registry([t]), tmp_path, {"enabled": False}) is None
    assert type(t).calls == 0


# ---------- 节流与轮转 ----------

def test_rotation_oldest_first_then_all_fresh(store, tmp_path):
    t1, t2 = _make_canary_tool("t_one", (True, "ok")), _make_canary_tool("t_two", (True, "ok"))
    reg = _Registry([t1, t2])
    n1, _, _ = _patrol(reg, tmp_path)     # 双双缺戳（last_at=0 同龄）：探到其一
    n2, _, _ = _patrol(reg, tmp_path)     # 另一个成为最久远者：轮到它
    assert {n1, n2} == {"t_one", "t_two"}
    assert _patrol(reg, tmp_path) is None  # 全员新鲜：本回合不打探针
    assert type(t1).calls == 1 and type(t2).calls == 1


def test_state_survives_corruption(tmp_path):
    (tmp_path / "maint").mkdir()
    (tmp_path / "maint" / dp.STATE_FILE).write_text("not json{")
    assert dp.read_state(tmp_path / "maint") == {}


# ---------- 告警面 ----------

def test_single_failure_records_but_does_not_alert(store, tmp_path):
    t = _make_canary_tool("flaky_tool", (False, "HTTP 403"))
    _patrol(_Registry([t]), tmp_path)
    st = dp.read_state(tmp_path / "maint")["flaky_tool"]
    assert st["ok"] is False and st["fails"] == 1
    assert store.get(dp._note_name("flaky_tool")) is None   # 阈值前不吵人


def test_threshold_failure_writes_note_with_fix(store, tmp_path):
    t = _make_canary_tool("dead_tool", (False, "extractor broken"))
    reg = _Registry([t])
    _patrol(reg, tmp_path)
    _patrol(reg, tmp_path, {"min_hours": 0})               # 连续第二次：达阈值
    note = store.get("drift-dead-tool")                    # 下划线转 kebab（slug 规则）
    assert note is not None and note.type == "project"
    assert "extractor broken" in note.body and "pip install -U something" in note.body
    assert "疑似过时" in note.description


def test_recovery_clears_alert_and_resets_fails(store, tmp_path):
    outcome = {"v": (False, "down")}
    t = _make_canary_tool("wobbly_tool", lambda: outcome["v"])
    reg = _Registry([t])
    _patrol(reg, tmp_path)
    _patrol(reg, tmp_path, {"min_hours": 0})
    assert store.get("drift-wobbly-tool") is not None
    outcome["v"] = (True, "back")
    _patrol(reg, tmp_path, {"min_hours": 0})
    assert store.get("drift-wobbly-tool") is None          # 恢复即清
    assert dp.read_state(tmp_path / "maint")["wobbly_tool"]["fails"] == 0
    # 巡检从头到尾只碰自己前缀的笔记
    store.write("user-fact", "手写事实", "user", "正文")
    _patrol(reg, tmp_path, {"min_hours": 0})
    assert store.get("user-fact") is not None


# ---------- fail-open ----------

def test_canary_exception_counts_as_failure_not_crash(store, tmp_path):
    t = _make_canary_tool("bomb_tool", RuntimeError("boom"))
    name, ok, detail = _patrol(_Registry([t]), tmp_path)
    assert name == "bomb_tool" and ok is False and "boom" in detail


def test_malformed_canary_return_is_failure(store, tmp_path):
    t = _make_canary_tool("weird_tool", lambda: "just a string")
    _, ok, detail = _patrol(_Registry([t]), tmp_path)
    assert ok is False and "形状非法" in detail


def test_no_note_store_still_records_state(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "get_note_store", lambda: None)
    t = _make_canary_tool("lonely_tool", (False, "down"))
    reg = _Registry([t])
    _patrol(reg, tmp_path)
    _patrol(reg, tmp_path, {"min_hours": 0})               # 达阈值但无笔记本：不崩
    assert dp.read_state(tmp_path / "maint")["lonely_tool"]["fails"] == 2
