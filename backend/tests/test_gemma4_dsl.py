"""gemma-4 工具协议的**差分金测试**（§7-8 迁移，2026-08-14）。

与本仓其余金测试的关键区别：基准不是我方写的期望字节，而是 **transformers 渲染
官方 chat_template.jinja 的实际输出**。这是 §S.16/§S.17 换来的纪律——那两次事故
的共同形态是「我方拼装层偏离官方模板若干字节，被误判为模型/引擎缺陷」，而当时
所有金测试都在拿我方自己的期望值对我方自己的实现，偏离对它们完全不可见。

覆盖三层：
1. `dsl.py` 三个编码器 vs jinja 宏（声明体/调用体/观测体，逐字节）；
2. `PromptBuilder.build` 的工具相关轮结构 vs jinja 整段渲染（逐字节）；
3. `tool_grammar.py` 产出的 Lark 语法 vs `dsl.format_tool_call` 的产物
   （约束形状必须恰好接受回放格式——两者漂移正是本次迁移要消灭的故障类）。

需要真 tokenizer（仅分词器，不载权重，CPU、秒级）。模型目录缺失时整体 skip。
"""
import re
from pathlib import Path

import pytest

from framework.dialects.gemma4.dsl import (format_function_declaration, format_tool_call,
                                           format_tool_response)
from framework.dialects.gemma4.prompt_builder import PromptBuilder
from framework.dialects.gemma4.tool_grammar import build_tool_call_grammar

_MODELS = Path(__file__).resolve().parents[1] / "models"
_CANDIDATES = ("gemma-4-31b-it-4bit", "gemma-4-26b-a4b-it-4bit",
               "gemma-4-e4b-it-8bit")  # D.20 保留档。次序有讲究：e4b 模板缺
# 幽灵思考抑制块（比 26b/31b 短 130B），差分基准须优先落在带抑制块的模板上，
# 否则该字节段（"发现 A"同类）失去官方对照覆盖；e4b 仅作兜底。


def _model_dir():
    for name in _CANDIDATES:
        d = _MODELS / name
        if (d / "chat_template.jinja").exists():
            return d
    return None


_DIR = _model_dir()
pytestmark = pytest.mark.skipif(_DIR is None,
                                reason="本机无 gemma-4 模型目录（差分基准需官方 chat template）")


@pytest.fixture(scope="module")
def tok():
    transformers = pytest.importorskip("transformers")
    return transformers.AutoTokenizer.from_pretrained(str(_DIR))


# ---------- 用例池 ----------

_TOOLS = [
    {"name": "get_current_time", "description": "Get the current time.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_weather", "description": "Weather lookup",
     "parameters": {"type": "object", "properties": {
         "loc": {"type": "string", "description": "City name"},
         "unit": {"type": "string", "enum": ["c", "f"], "description": "Unit"},
         "days": {"type": "integer", "description": "How many days"},
         "verbose": {"type": "boolean"},
         "ratio": {"type": "number"},
         "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
         "opts": {"type": "object",
                  "properties": {"a": {"type": "string", "description": "deep"},
                                 "b": {"type": "integer"}},
                  "required": ["a"]},
         "nul": {"type": "string", "nullable": True},
     }, "required": ["loc"]}},
    {"name": "run_python", "description": "含中文 & \"quotes\"",
     "parameters": {"type": "object",
                    "properties": {"code": {"type": "string", "description": "代码"}},
                    "required": ["code"]}},
]
_NESTED = [{"type": "function", "function": t} for t in _TOOLS]


def _jinja(tok, messages, tools=None, gen=False):
    return tok.apply_chat_template(messages, tools=tools, tokenize=False,
                                   add_generation_prompt=gen)


# ---------- 1. 编码器 vs jinja 宏 ----------

@pytest.mark.parametrize("idx", range(len(_TOOLS)))
def test_declaration_matches_official_template(tok, idx):
    """声明体逐字节对表官方 `format_function_declaration` 宏。

    这正是 §S.17 的病灶：我方递 JSON、官方是 `declaration:名字{…}` 紧凑 DSL。
    实测同模型同问题只换声明格式，12B 就从思考死循环变成干净调用。
    """
    want = re.findall(r"<\|tool>(.*?)<tool\|>", _jinja(tok, [{"role": "user", "content": "x"}],
                                                       tools=[_NESTED[idx]]), re.DOTALL)[0]
    assert format_function_declaration(_TOOLS[idx]) == want      # 扁平形态（本栈注册表）
    assert format_function_declaration(_NESTED[idx]) == want     # OpenAI 嵌套形态


def test_multiple_declarations_are_adjacent(tok):
    """多工具紧邻无分隔符——中间多一个换行就是新的字节偏离。"""
    rendered = _jinja(tok, [{"role": "user", "content": "x"}], tools=_NESTED)
    want = "".join(f"<|tool>{d}<tool|>"
                   for d in re.findall(r"<\|tool>(.*?)<tool\|>", rendered, re.DOTALL))
    got = "".join(f"<|tool>{format_function_declaration(t)}<tool|>" for t in _TOOLS)
    assert got == want


@pytest.mark.parametrize("args", [
    {},
    {"s": "hi", "i": 3, "f": 1.5, "b": True, "bf": False},
    {"o": {"k": "v", "n": 2}, "l": ["a", 1, True]},
    {"a": {"b": {"c": ["x", {"d": "e"}]}}},
    {"深": "值", "z": 'line1\nline2 "q"'},          # 多行 + 内嵌引号（run_python 的真实形态）
    {"Bee": 1, "apple": 2, "Cat": 3},               # dictsort 是大小写不敏感的
    {"e": {}, "l": []},
])
def test_tool_call_matches_official_template(tok, args):
    """调用体逐字节对表官方模板的 tool_calls 分支（含 dictsort 键序）。"""
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "a", "type": "function",
                             "function": {"name": "fn", "arguments": args}}]}]
    want = re.findall(r"<\|tool_call>(.*?)<tool_call\|>", _jinja(tok, msgs), re.DOTALL)[0]
    assert format_tool_call("fn", args) == want


@pytest.mark.parametrize("resp", ["22C sunny", {"time": "12:00", "ok": True, "n": 3}, 42,
                                  ["a", "b"], {}, 'a\nb "q"'])
def test_tool_response_matches_official_template(tok, resp):
    """观测体逐字节对表官方 `format_tool_response_block` 宏（mapping 展开 / 其余裹 value）。"""
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "a", "type": "function",
                             "function": {"name": "fn", "arguments": {}}}],
             "tool_responses": [{"name": "fn", "response": resp}]}]
    want = re.findall(r"<\|tool_response>(.*?)<tool_response\|>", _jinja(tok, msgs), re.DOTALL)[0]
    assert format_tool_response("fn", resp) == want


# ---------- 2. 整段 prompt 结构 vs jinja ----------

class _Proc:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer


def _builder(tok):
    # 抑制预填是我方按注册表开关注入的（官方模板在生成头处才发），关掉才能做历史段差分
    return PromptBuilder(_Proc(tok), suppress_ghost_thinking=False, env={})


def _history(tok, messages, tools):
    """我方 prompt 去掉本栈特有的可变尾部（环境锚点轮 + 生成头），只留历史段。"""
    p = _builder(tok).build(messages, tools=tools)
    return p[:p.rfind("<|turn>system\nCurrent date:")]


_CALL = {"id": "c1", "type": "function",
         "function": {"name": "get_weather", "arguments": {"loc": "Tokyo"}}}
_CALL2 = {"id": "c2", "type": "function",
          "function": {"name": "get_weather", "arguments": {"loc": "Osaka"}}}


@pytest.mark.parametrize("label,ours,theirs", [
    ("system+tools",
     [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}],
     [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}]),
    ("no-system",
     [{"role": "user", "content": "u"}],
     [{"role": "user", "content": "u"}]),
    ("tool-loop",
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "", "tool_calls": [_CALL]},
      {"role": "tool", "tool_responses": [{"name": "get_weather", "content": "22C"}]},
      {"role": "assistant", "content": "It is 22C."}],
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "", "tool_calls": [_CALL]},
      {"role": "tool", "tool_call_id": "c1", "content": "22C"},
      {"role": "assistant", "content": "It is 22C."}]),
    ("content-before-calls",
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "Let me check.", "tool_calls": [_CALL]},
      {"role": "tool", "tool_responses": [{"name": "get_weather", "content": "22C"}]},
      {"role": "user", "content": "thanks"}],
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "Let me check.", "tool_calls": [_CALL]},
      {"role": "tool", "tool_call_id": "c1", "content": "22C"},
      {"role": "user", "content": "thanks"}]),
    ("multi-call",
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "", "tool_calls": [_CALL, _CALL2]},
      {"role": "tool", "tool_responses": [{"name": "get_weather", "content": "22C"},
                                          {"name": "get_weather", "content": "25C"}]},
      {"role": "assistant", "content": "ok"}],
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "", "tool_calls": [_CALL, _CALL2],
       "tool_responses": [{"name": "get_weather", "response": "22C"},
                          {"name": "get_weather", "response": "25C"}]},
      {"role": "assistant", "content": "ok"}]),
    ("consecutive-tool-messages",
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "", "tool_calls": [_CALL, _CALL2]},
      {"role": "tool", "tool_responses": [{"name": "get_weather", "content": "22C"}]},
      {"role": "tool", "tool_responses": [{"name": "get_weather", "content": "25C"}]},
      {"role": "assistant", "content": "ok"}],
     [{"role": "user", "content": "u"},
      {"role": "assistant", "content": "", "tool_calls": [_CALL, _CALL2],
       "tool_responses": [{"name": "get_weather", "response": "22C"},
                          {"name": "get_weather", "response": "25C"}]},
      {"role": "assistant", "content": "ok"}]),
])
def test_history_matches_official_template(tok, label, ours, theirs):
    """历史段（含工具轮结构）逐字节对表官方模板。

    这条守的是**轮拓扑**：官方从不开 <|turn>tool 轮，一次「调用→观测→回答」自始至终
    是同一个 model 轮；正文排在工具块之后。三者任一漂移都会让模型看到训练里不存在
    的结构，而 count/qa 类质量哨探不到——只有对表能探到。
    """
    assert _history(tok, ours, _TOOLS) == _jinja(tok, theirs, tools=_NESTED)


def test_tool_result_then_user_closes_the_turn(tok):
    """有意偏离官方的唯一轮结构项：观测后直接换 user 说话时我方收口。

    官方在此留下永不闭合的 model 轮（模板自身缺陷）；本栈有真实路径会走到这里
    （turn_runner 的 cancelled 观测），未闭合轮头接 <|turn>user 是模型没见过的形状。
    """
    msgs = [{"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [_CALL]},
            {"role": "tool", "tool_responses": [{"name": "get_weather", "content": "22C"}]},
            {"role": "user", "content": "stop"}]
    assert "<tool_response|><turn|>\n<|turn>user\nstop" in _history(tok, msgs, _TOOLS)


def test_declaration_emitted_once_with_rolling_summary(tok):
    """会话带滚动摘要时有两条 system 消息，声明只能进第一条。"""
    msgs = [{"role": "system", "content": "S1"}, {"role": "system", "content": "S2"},
            {"role": "user", "content": "u"}]
    assert _history(tok, msgs, _TOOLS).count("declaration:") == len(_TOOLS)


# ---------- 3. 约束语法 vs 回放格式 ----------

@pytest.fixture(scope="module")
def matcher_factory(tok):
    llguidance = pytest.importorskip("llguidance")
    from llguidance import hf as llhf
    ltok = llhf.from_tokenizer(tok)
    grammar = llguidance.LLMatcher.grammar_from_lark(build_tool_call_grammar(_TOOLS))
    probe = llguidance.LLMatcher(ltok, grammar)
    assert not probe.is_error(), f"语法编译失败: {probe.get_error()}"

    def accepts(text: str) -> bool:
        m = llguidance.LLMatcher(ltok, grammar)
        for t in tok.encode(text, add_special_tokens=False):
            if not m.consume_token(t) or m.is_error():
                return False
        return m.is_accepting()

    return accepts


@pytest.mark.parametrize("name,args", [
    ("get_current_time", {}),
    ("get_weather", {"loc": "Tokyo"}),
    ("get_weather", {"days": 3, "loc": "T", "nul": "n", "opts": {"a": "y", "b": 1},
                     "ratio": 1.5, "tags": ["a", "b"], "unit": "c", "verbose": True}),
    ("get_weather", {"loc": "T", "unit": "f"}),
    ("get_weather", {"loc": "T", "tags": []}),
    ("get_weather", {"days": -2, "loc": "T"}),
    ("run_python", {"code": 'import os\nprint("hi")\n'}),     # 多行含引号
    ("run_python", {"code": ""}),
    ("run_python", {"code": "打印('中文')"}),
])
def test_grammar_accepts_replay_format(matcher_factory, name, args):
    """约束语法必须接受 dsl 编码器产出的每一种合法调用。

    这是「约束形状 == 回放格式」的正向锚：语法比回放严一点，模型就会被逼进
    自己历史里没见过的形状（且 fail-open 兜底会把它掩盖成"能跑但慢慢坏"）。
    """
    assert matcher_factory(format_tool_call(name, args))


@pytest.mark.parametrize("label,text", [
    ("未知工具", "call:nope{}"),
    ("未知键", 'call:get_weather{loc:<|"|>T<|"|>,zzz:1}'),
    ("缺必填", "call:get_weather{days:3}"),
    ("嵌套缺必填", 'call:get_weather{loc:<|"|>T<|"|>,opts:{b:1}}'),
    ("越界 enum", 'call:get_weather{loc:<|"|>T<|"|>,unit:<|"|>k<|"|>}'),
    ("键序颠倒", 'call:get_weather{loc:<|"|>T<|"|>,days:3}'),
    ("重复键", 'call:get_weather{loc:<|"|>T<|"|>,loc:<|"|>T<|"|>}'),
    ("整数写成串", 'call:get_weather{days:<|"|>3<|"|>,loc:<|"|>T<|"|>}'),
    ("布尔非法值", 'call:get_weather{loc:<|"|>T<|"|>,verbose:yes}'),
    ("迁移前的 JSON 形态", '{"name":"get_weather","arguments":{"loc":"T"}}'),
    ("无参工具却带参", "call:get_current_time{x:1}"),
    ("未闭合", 'call:get_weather{loc:<|"|>T<|"|>'),
])
def test_grammar_rejects_malformed(matcher_factory, label, text):
    """反向锚：约束必须真的拦得住。语法写松了 = 白开一道闸还以为有保护。"""
    assert not matcher_factory(text), label


# ---------- 4. 有意偏离官方的两处，逐字节钉死 ----------

def test_declaration_closes_parameters_when_type_missing(tok):
    """`parameters` 缺 `type` 时官方吐出**花括号不平衡**的声明，我方补 OBJECT 闭合。

    这条不是边角料：`update_plan` 等元工具的 parameters 就没有 `type` 键，
    **每一份生产 prompt 都走这个分支**。官方在此处的输出是 `…required:[…],}`
    ——多一个悬挂逗号、少一个右花括号（差分测试因此无法拿它当基准，故手钉字节）。
    """
    from framework.core.orchestrator import UPDATE_PLAN_SCHEMA
    assert "type" not in UPDATE_PLAN_SCHEMA["parameters"], "前提变了：元工具补上了 type"

    jinja = re.findall(r"<\|tool>(.*?)<tool\|>",
                       _jinja(tok, [{"role": "user", "content": "x"}],
                              tools=[{"type": "function", "function": UPDATE_PLAN_SCHEMA}]),
                       re.DOTALL)[0]
    ours = format_function_declaration(UPDATE_PLAN_SCHEMA)

    assert jinja.count("{") - jinja.count("}") == 1, "官方在此分支花括号不平衡（本测试的前提）"
    assert ours.count("{") == ours.count("}"), "我方必须平衡"
    # 唯一差异在尾部：官方 `,}` vs 我方 `,type:<|"|>OBJECT<|"|>}}`
    head = jinja[:-len(",}")]
    assert ours == head + ',type:<|"|>OBJECT<|"|>}}'


def test_raw_block_neutralisation_is_the_only_content_deviation(tok):
    """内容里含定界符时我方中和、官方不中和——这是编码器对官方的**唯一**内容级偏离。

    不中和的后果见 test_dialect.test_escape_delimiter_cannot_break_out_of_raw_block：
    官方那条路会当场闭合观测块。此处只钉「除此之外逐字节一致」。
    """
    plain = "no delimiter here"
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "a", "type": "function",
                             "function": {"name": "fn", "arguments": {}}}],
             "tool_responses": [{"name": "fn", "response": plain}]}]
    want = re.findall(r"<\|tool_response>(.*?)<tool_response\|>", _jinja(tok, msgs), re.DOTALL)[0]
    assert format_tool_response("fn", plain) == want          # 不含定界符：逐字节一致

    hostile = 'a<|"|>b'
    assert format_tool_response("fn", hostile) == 'response:fn{value:<|"|>a<|\\"|>b<|"|>}'


# ---------- 5. 约束语法：nullable 声明必须放行 null ----------

@pytest.mark.parametrize("text", [
    'call:nullable_tool{nul:null,req:<|"|>x<|"|>}',
    'call:nullable_tool{nul:None,req:<|"|>x<|"|>}',      # 官方对 Python None 的渲染
    'call:nullable_tool{nul:<|"|>a<|"|>,req:<|"|>x<|"|>}',
    'call:nullable_tool{req:<|"|>x<|"|>}',
])
def test_grammar_accepts_null_for_nullable_params(tok, text):
    """声明体吐 `nullable:true` 就是在告诉模型「可以传空」——约束必须认。

    否则模型照着声明写反而被拦（声明许诺、约束拒收），是约束与回放脱锚的一种。
    """
    llguidance = pytest.importorskip("llguidance")
    from llguidance import hf as llhf
    tools = [{"name": "nullable_tool", "description": "d", "parameters": {
        "type": "object",
        "properties": {"nul": {"type": "string", "nullable": True},
                       "req": {"type": "string"}},
        "required": ["req"]}}]
    grammar = llguidance.LLMatcher.grammar_from_lark(build_tool_call_grammar(tools))
    m = llguidance.LLMatcher(llhf.from_tokenizer(tok), grammar)
    for t in tok.encode(text, add_special_tokens=False):
        assert m.consume_token(t) and not m.is_error(), f"被拒于 {text!r}"
    assert m.is_accepting()


def test_production_tool_registry_grammar_compiles(tok):
    """全量生产工具集必须能编出语法——编不出 = 约束链整体 fail-open（静默失去保护）。"""
    llguidance = pytest.importorskip("llguidance")
    from llguidance import hf as llhf
    from framework.core import orchestrator as orch
    tools = [s for s in (getattr(orch, n, None) for n in dir(orch) if n.endswith("_SCHEMA"))
             if isinstance(s, dict) and s.get("name")]
    assert tools, "没取到任何元工具 schema（前提变了）"
    grammar = llguidance.LLMatcher.grammar_from_lark(build_tool_call_grammar(tools))
    m = llguidance.LLMatcher(llhf.from_tokenizer(tok), grammar)
    assert not m.is_error(), f"语法编译失败: {m.get_error()}"


# ---------- 6. 自审揪出的两条（首过评审未覆盖） ----------

@pytest.mark.parametrize("msgs", [
    [{"role": "user", "content": "u"}, {"role": "system", "content": "S"},
     {"role": "user", "content": "v"}],
    [{"role": "system", "content": "S1"}, {"role": "system", "content": "S2"},
     {"role": "user", "content": "u"}],
])
def test_declaration_always_leads_when_first_message_is_not_system(tok, msgs):
    """官方的条件是 `tools or messages[0].role in [system, developer]`——**有工具就必有
    一个领头的 system 轮**。写成「第一个 system 轮」会在 messages[0] 不是 system 时
    （如 [user, system, …]）把整份声明塞进对话中段，稳定前缀也随之报废。
    """
    assert _history(tok, msgs, _TOOLS) == _jinja(tok, msgs, tools=_NESTED)


def test_continuation_assistant_gets_no_ghost_thinking_prefill(tok):
    """续轮的 assistant **不预填**空思考频道：官方的思考频道只在轮首出现。

    「保持 KV 对齐」的理由在折轮处不成立——生成时这段前面是环境轮 + 新轮头，
    重放时紧跟 <tool_response|>，字节本就不同。凭空插一个空频道只是又一处偏离。
    """
    msgs = [{"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [_CALL]},
            {"role": "tool", "tool_responses": [{"name": "get_weather", "content": "22C"}]},
            {"role": "assistant", "content": "It is 22C."}]
    p = PromptBuilder(_Proc(tok), suppress_ghost_thinking=True, env={}).build(msgs, tools=_TOOLS)
    hist = p[:p.rfind("<|turn>system\nCurrent date:")]
    assert "<tool_response|>It is 22C." in hist, "续写段前不得有空思考频道"
    # 轮首的预填仍然照发（那里 KV 对齐的理由成立）
    assert "<|turn>model\n<|channel>thought\n<channel|><|tool_call>" in hist
