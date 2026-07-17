"""轮次完结时间戳金测试（PROTOCOL.md §2.3 additive 字段，2026-07-17）。

约定：append_log 是落盘唯一漏斗，在此对**调用方原 dict** setdefault "timestamp"
（epoch 秒）——/chat 响应与 turn_ended 事件在落盘后引用同一 dict，三面天然同值。
旧历史行没有该字段，客户端按可选处理（§0.5 向前兼容）。
"""
import json
import time


def _read_turns(session):
    with open(session.history_log, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_append_log_stamps_timestamp(make_session):
    session = make_session()
    turn = {"status": "success", "prompt": "hi", "response": []}
    before = time.time()
    session.append_log(turn)
    after = time.time()

    # 三面同值的根据：原 dict 被盖章（响应/事件引用它）……
    assert before <= turn["timestamp"] <= after
    # ……且落盘行携带同一个值
    assert _read_turns(session)[-1]["timestamp"] == turn["timestamp"]


def test_append_log_respects_existing_timestamp(make_session):
    """setdefault 语义：已有时间戳（如恢复续跑补落盘的旧轮）不得被覆盖。"""
    session = make_session()
    turn = {"status": "success", "prompt": "hi", "response": [], "timestamp": 1000.5}
    session.append_log(turn)
    assert turn["timestamp"] == 1000.5
    assert _read_turns(session)[-1]["timestamp"] == 1000.5
