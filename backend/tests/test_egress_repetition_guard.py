"""出网 L0 腿 × 流式复读护栏：模型腿被护栏打断时必须 fail-closed（build 83 一审 Minor-4）。

护栏命中后 generate_once 返回 text=""，`_parse_spans("")` 会得出「没有敏感段」——那是静默放行，
不是「这腿没跑成」。detect_sensitive_spans 对 repetition_guard 终态抛错，sanitize 把它记成
model_leg="unavailable: …"，规则腿成果照旧保留。
"""
import asyncio

import pytest

from framework.cloud import egress_guard as eg


class _FakeBackend:
    def new_cache(self):
        return None


class _GuardTripped:
    backend = _FakeBackend()

    async def generate_once(self, sess, **kw):
        return {"cancelled": False, "repetition_guard": {"channel": "content", "reason": "ngram_dup"},
                "thoughts": [], "text": "", "tool_calls": [], "metrics": {}}


def test_detect_sensitive_spans_raises_when_the_guard_tripped():
    with pytest.raises(RuntimeError, match="repetition guard"):
        asyncio.run(eg.detect_sensitive_spans(_GuardTripped(), "王秀兰的邮箱是 wang@example.com"))


def test_sanitize_reports_the_leg_unavailable_not_ok():
    s = eg.sanitize("王秀兰的邮箱是 wang@example.com", engine=_GuardTripped(), loop=None)
    assert s["model_leg"].startswith("unavailable:") and "repetition guard" in s["model_leg"]
    assert s["counts"] == {"EMAIL": 1}            # 规则腿成果保留
    assert "wang@example.com" not in s["text"]
