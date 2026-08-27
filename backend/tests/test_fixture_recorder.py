"""夹具录制器纯函数金测试(复审 S14:CLI 半边是操作员工具首录磨合,解析/重编号/瘦身
检测三个纯函数值得钉——它们决定夹具的保真度)。"""
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "record_fixture",
    Path(__file__).resolve().parents[2] / "protocol" / "fixtures" / "record_fixture.py")
rf = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("record_fixture", rf)
_spec.loader.exec_module(rf)


def test_parse_sse_lines_shapes():
    lines = [
        b'event: turn_queued\r\n',
        b'data: {"seq": 5, "type": "turn_queued", "data": {}}\r\n',
        b': ping\n',                                   # 心跳注释帧
        b'\n',
        b'data:{"seq": 6, "type": "turn_started", "data": {}}\n',   # 无空格形态
        b'data: not-json\n',                           # 响亮丢弃,不炸
        b'data: {"seq": 0, "type": "stream_attached", "data": {"last_seq": 6}}\n',
    ]
    envs = rf.parse_sse_lines(lines)
    assert [e["type"] for e in envs] == ["turn_queued", "turn_started"]   # attach 默认剥
    kept = rf.parse_sse_lines(lines, keep_attach=True)
    assert [e["type"] for e in kept][-1] == "stream_attached"


def test_renumber_dense_and_payload_untouched():
    envs = [{"seq": 41, "ts": 9.9, "type": "a", "data": {"timestamp": 123.0}},
            {"seq": 99, "ts": 10.0, "type": "b", "data": {}}]
    out = rf.renumber(envs)
    assert [e["seq"] for e in out] == [1, 2]
    assert out[0]["data"] == {"timestamp": 123.0}      # 载荷真值不动(两套钟并存已知)
    assert envs[0]["seq"] == 41                        # 入参不被原地改


def test_renumber_refuses_attach_frames():
    import pytest
    with pytest.raises(AssertionError):
        rf.renumber([{"seq": 0, "type": "stream_attached", "data": {"last_seq": 3}}])


def test_degraded_history_is_detected():
    ok = [{"type": "turn_ended", "seq": 3, "data": {"status": "success", "response": "hi"}}]
    slim = [{"type": "turn_ended", "seq": 3, "data": {"status": "success", "turn_index": 0}}]
    assert rf.check_not_degraded(ok) is None
    assert "瘦身" in rf.check_not_degraded(slim)
