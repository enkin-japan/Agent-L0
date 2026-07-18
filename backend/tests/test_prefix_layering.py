"""前缀分层金测试（T4R）：钉住 PromptBuilder 的 KV 缓存友好装配不回退。

M6 审计判明装配已是三段式（稳定前缀 / 追加区 / 可变尾部），T3 基座缓存与自动
前缀匹配的收益全部架在这个不变量上——任何人把逐轮易变内容（任务块、tail_notes、
日期、笔记索引）挪到历史之前，都会把每轮预填从「只算新增」退化成「全量重算」，
而且没有任何报错。本文件让这种回退在金测试里现形。

判定手法：两次 build 的公共字符串前缀必须完整盖住共享历史（以最后一条共享消息的
内容为地标）。字符串前缀是 token 前缀的保守下界（同一 tokenizer 下同串同 token）。
"""
import os
from datetime import datetime

from framework.dialects.gemma4.prompt_builder import PromptBuilder


class _FakeTok:
    special_tokens_map = {}


class _FakeProcessor:
    tokenizer = _FakeTok()


MSGS = [
    {"role": "system", "content": "你是一个有工具的助手。"},
    {"role": "user", "content": "帮我查一下天气"},
    {"role": "assistant", "content": "好的，我来查询。"},
    {"role": "user", "content": "顺便订个闹钟-LANDMARK-"},
]
TOOLS = [{"name": "get_weather", "description": "查天气",
          "parameters": {"properties": {"city": {"type": "string"}}}}]


def _pb():
    return PromptBuilder(_FakeProcessor())


def _common_covers_history(p1: str, p2: str):
    common = os.path.commonprefix([p1, p2])
    landmark = "-LANDMARK-"
    assert landmark in p1 and landmark in p2
    end = p1.index(landmark) + len(landmark)
    assert len(common) >= end, (
        f"公共前缀 {len(common)} 字符未盖住共享历史（地标止于 {end}）——"
        f"有逐轮易变段站到了历史之前")


def test_task_block_and_tail_notes_only_hit_tail():
    pb = _pb()
    p1 = pb.build(MSGS, tools=TOOLS)
    p2 = pb.build(MSGS, tools=TOOLS, task_block="任务：步骤1/3",
                  tail_notes=["一次性护栏提醒"])
    _common_covers_history(p1, p2)


def test_notes_index_only_hits_tail():
    pb = _pb()
    p1 = pb.build(MSGS, tools=TOOLS, include_env=True, env_extra=["笔记A"])
    p2 = pb.build(MSGS, tools=TOOLS, include_env=True, env_extra=["笔记B变了"])
    _common_covers_history(p1, p2)


def test_date_change_only_hits_tail(monkeypatch):
    import framework.dialects.gemma4.prompt_builder as mod

    class _D1:
        @staticmethod
        def now():
            return datetime(2026, 7, 18)

    class _D2:
        @staticmethod
        def now():
            return datetime(2026, 7, 19)

    pb = _pb()
    monkeypatch.setattr(mod, "datetime", _D1)
    p1 = pb.build(MSGS, tools=TOOLS)
    monkeypatch.setattr(mod, "datetime", _D2)
    p2 = pb.build(MSGS, tools=TOOLS)
    _common_covers_history(p1, p2)


def test_history_growth_keeps_shared_prefix():
    pb = _pb()
    p1 = pb.build(MSGS, tools=TOOLS)
    p2 = pb.build(MSGS + [{"role": "assistant", "content": "闹钟订好了"}], tools=TOOLS)
    _common_covers_history(p1, p2)


def test_stable_base_precedes_history():
    """基座（system+工具声明）必须站在第一条用户消息之前——T3 基座缓存的前提。"""
    pb = _pb()
    p = pb.build(MSGS, tools=TOOLS)
    assert p.index("get_weather") < p.index("帮我查一下天气")
    assert p.index("你是一个有工具的助手") < p.index("get_weather")


def test_thinking_toggle_only_hits_generation_head():
    """思考开关只动生成头（最后一段），不打任何历史前缀。"""
    pb = _pb()
    p1 = pb.build(MSGS, tools=TOOLS, enable_thinking=False)
    p2 = pb.build(MSGS, tools=TOOLS, enable_thinking=True)
    _common_covers_history(p1, p2)
