"""VisibleTextStreamer 单元测试:可见正文过滤 + Phase 3 粒度钩子(思考直播/工具名预告)。"""
from framework.core.stream_filter import VisibleTextStreamer


def run_stream(raw: str, *, in_thought=False, chunk=3):
    """按小块喂入(模拟逐 token),收集三路输出。"""
    thinking, pending = [], []
    s = VisibleTextStreamer(in_thought=in_thought,
                            on_thinking=thinking.append,
                            on_tool_pending=pending.append)
    visible = ""
    for i in range(0, len(raw), chunk):
        visible += s.feed(raw[i:i + chunk])
    visible += s.close()
    return visible, "".join(thinking), pending


def test_bare_text_passthrough():
    v, t, p = run_stream("你好,这是最终答复。")
    assert v == "你好,这是最终答复。" and t == "" and p == []


def test_thought_channel_streams_to_thinking():
    v, t, p = run_stream("<|channel>thought 想一想这个问题<channel|>答案是 42。")
    assert v == "答案是 42。"
    assert t == "想一想这个问题"
    assert p == []


def test_seeded_thought_state():
    """enable_thinking 播种态:流从思考体内开始(无频道头),直播照常。"""
    v, t, p = run_stream("先思考…\n<channel|>正文来了", in_thought=True)
    assert v == "正文来了"
    assert t == "先思考…\n"


def test_tool_block_announces_name_and_suppresses():
    raw = '好的。<|tool_call>{"name": "web_research", "arguments": {"query": "天气"}}<tool_call|>'
    v, t, p = run_stream(raw)
    assert v == "好的。"
    assert p == ["web_research"]          # 块内一出现 name 即预告(执行前)


def test_tool_name_announced_before_block_completes():
    """名字在块未闭合时就已预告——这正是'执行前几十秒可见'的来源。"""
    pending = []
    s = VisibleTextStreamer(on_tool_pending=pending.append)
    s.feed('<|tool_call>{"name": "run_shell_command", "argu')
    assert pending == ["run_shell_command"]   # 参数还没生成完


def test_legacy_call_prefix_format():
    v, t, p = run_stream('<|tool_call>call: get_current_time {}<tool_call|>')
    assert p == ["get_current_time"]


def test_multiple_tool_blocks_each_announced():
    raw = ('<|tool_call>{"name": "a_tool", "arguments": {}}<tool_call|>'
           '<|tool_call>{"name": "b_tool", "arguments": {}}<tool_call|>')
    _, _, p = run_stream(raw)
    assert p == ["a_tool", "b_tool"]


def test_text_channel_visible_not_thinking():
    v, t, p = run_stream("<|channel>text 正文频道内容<channel|>")
    assert v == "正文频道内容"
    assert t == ""


def test_budget_forced_close_stops_thinking():
    """思考预算强插 \\n<channel|> 后:思考直播停止,后续裸文本入正文。"""
    v, t, p = run_stream("思考中被掐断\n<channel|>继续输出正文", in_thought=True)
    assert t == "思考中被掐断\n"
    assert v == "继续输出正文"


def test_dotted_tool_name_announced():
    """带点工具名的预告(response_parser 同口径修复,08-16 gemma tool_call 案)。"""
    v, t, p = run_stream('<|tool_call>call:country_info.capital{country:<|"|>Brazil<|"|>}<tool_call|>')
    assert p == ["country_info.capital"]
    assert v == ""    # 抑制半边同样要钉：调用块不得流出为可见正文


def test_qwen35_xml_function_name_announced():
    """qwen35 方言(XML 调用体)的工具名预告——此前两种形态都匹配不上,预告静默失灵。"""
    pending = []
    s = VisibleTextStreamer(soc="<think>", eoc="</think>",
                            stc="<tool_call>", etc="</tool_call>",
                            on_tool_pending=pending.append)
    s.feed("<tool_call>\n<function=web.search>\n<parameter=query>\n天气\n</parameter>")
    assert pending == ["web.search"]
