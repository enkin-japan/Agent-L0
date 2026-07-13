"""ResponseParser 容错单元测试:标记残缺的各种事故形态都不得把垃圾漏进正文。"""
from framework.core.response_parser import ResponseParser


class _Tok:
    pass  # 无 special_tokens_map → parser 用默认方言标记


class _Proc:
    tokenizer = _Tok()


def make_parser():
    return ResponseParser(_Proc())


def test_normal_call_and_text():
    p = make_parser()
    out = p.parse('好的。<|tool_call>{"name": "get_current_time", "arguments": {}}<tool_call|>')
    assert out["text"] == "好的。"
    assert out["tool_calls"] == [
        {"name": "get_current_time", "arguments": {}, "type": "function"}]


def test_thought_channel_extraction():
    p = make_parser()
    out = p.parse("<|channel>thought 想一想<channel|>答案是 42。")
    assert out["thoughts"] == "想一想"
    assert out["text"] == "答案是 42。"


def test_unterminated_call_becomes_error_not_text():
    """有头无尾(§2.5):截断/忘写收尾 → error 调用走纠错闭环,不漏正文。"""
    p = make_parser()
    out = p.parse('<|tool_call>{"name": "write_file", "arguments": {"path": "/a", "content": "半截')
    assert out["text"] == ""
    assert len(out["tool_calls"]) == 1 and out["tool_calls"][0]["type"] == "error"
    assert "Unterminated" in out["tool_calls"][0]["error_msg"]


def test_tail_only_fragment_becomes_error_not_text():
    """有尾无头(§2.6,实测事故):思考预算强插 <channel|> 落在思考内草拟的调用中间,
    频道提取吞掉调用头,尾巴曾连着 <tool_call|> 标记漏成最终答复。"""
    p = make_parser()
    raw = ('<|channel>thought 我来创建提醒 <|tool_call>{"name": "schedule_create", '
           '"arguments": {"prompt": "查天气<channel|>'
           '明天（2026-07-12", "type": "once"}<tool_call|>')
    out = p.parse(raw)
    # 正文绝不含 JSON 残片与标记
    assert "type" not in out["text"] and "tool_call" not in out["text"]
    assert out["text"] == ""
    # 残片转 error 调用,反馈让模型完整重发(敏感调用重发照常撞审批闸门)
    errs = [c for c in out["tool_calls"] if c["type"] == "error"]
    assert len(errs) == 1 and "TAIL" in errs[0]["error_msg"]
    # 调用头被频道提取吞进 thoughts(既有行为,不断言其内容干净)
    assert "我来创建提醒" in out["thoughts"]


def test_stray_markers_stripped_from_text():
    p = make_parser()
    out = p.parse("回答完毕。<channel|>")
    assert out["text"] == "回答完毕。"
    # 游离 etc 走 §2.6(转 error);游离 eoc 直接清除
    out2 = p.parse("<|channel>thought 想<channel|>正文<channel|>")
    assert "channel" not in out2["text"]


def test_error_raw_input_capped():
    """失控超长的坏调用原文必须截断——它会被回显给模型并进入历史/账本。"""
    p = make_parser()
    big = "x" * 9000
    out = p.parse(f'<|tool_call>{{"name": "run_python", "arguments": {{"code": "{big}')
    err = out["tool_calls"][0]
    assert err["type"] == "error"
    assert len(err["raw_input"]) < 2000 and err["raw_input"].endswith("…[truncated]")


def test_constrained_pure_json_call():
    """约束采样产出的纯 JSON 调用(无 call: 前缀)补 type=function。"""
    p = make_parser()
    out = p.parse('<|tool_call>{"name": "rag_search", "arguments": {"query": "天气"}}<tool_call|>')
    assert out["tool_calls"][0]["type"] == "function"
    assert out["tool_calls"][0]["name"] == "rag_search"


def test_unclosed_thought_channel_routes_to_thoughts():
    """有头无尾的频道块(§2.7):实测事故(会话 e2348829)——<|channel>thought 后整段
    无收尾标记,内部独白连同标记原样漏成正文直呈用户。必须整段路由进 thoughts。"""
    p = make_parser()
    out = p.parse("<|channel>thought\nThe user wants me to complete 7 exercises…")
    assert out["thoughts"] == "The user wants me to complete 7 exercises…"
    assert out["text"] == ""
    assert "channel" not in out["text"]


def test_text_before_unclosed_thought_preserved():
    p = make_parser()
    out = p.parse("可见答案。<|channel>thought\n内部独白不该给用户看")
    assert out["text"] == "可见答案。"
    assert "内部独白" in out["thoughts"]
    assert "channel" not in out["text"]


def test_unclosed_text_channel_routes_to_text():
    """频道名是 text 的无收尾块:内容本来就是想说的话,进正文(剥掉标记)。"""
    p = make_parser()
    out = p.parse("<|channel>text\n这是给用户的正文")
    assert out["text"] == "这是给用户的正文"
    assert out["thoughts"] == []


def test_paired_then_stray_thought_channels_join_in_order():
    p = make_parser()
    out = p.parse("<|channel>thought 想法一<channel|>正文。<|channel>thought\n想法二没闭合")
    assert out["text"] == "正文。"
    assert out["thoughts"].index("想法一") < out["thoughts"].index("想法二没闭合")


def test_tool_call_inside_unclosed_thought_still_executes():
    """无收尾思考块里的完整调用照常提取执行(与既有'调用不因频道残缺而丢'语义一致)。"""
    p = make_parser()
    out = p.parse('<|channel>thought\n先分析一下<|tool_call>'
                  '{"name": "list_dir", "arguments": {}}<tool_call|>')
    assert out["tool_calls"] and out["tool_calls"][0]["name"] == "list_dir"
    assert "先分析一下" in out["thoughts"]
    assert out["text"] == ""
