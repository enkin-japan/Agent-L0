"""catalog 模式下「prompt 声明」与「约束语法源」必须解耦（OSS 扫描 §6.5）。

守的是什么：catalog 模式是稳定前缀的**逃生口**——工具规模撑爆 12k 前缀预算时，把
prompt 里的工具声明瘦成「名字 + 一行描述」，模型需要详情时再 get_tool_schema 取。
但瘦身瘦错了对象就会把逃生口焊死：约束采样的语法若也建在瘦目录上，目录条目没有
`parameters` → 语法只生成 `call:name{}` 一条分支 → **物理上只允许空实参**，全部带参
工具被钳死。净效果是「阈值早已定好、开关拨不动」。

本文件钉死：语法源恒为完整 schema；full 模式下两者逐字同值（无行为变化）。

**覆盖面的诚实声明**：这里断言的是语法**源与形状**，不是「喂一个调用体进去被接受
还是被拒」。真做 accept/reject 要 llguidance 的 `LLMatcher`，而它必须挂一个真实 HF
分词器——那会让金测试依赖模型文件是否在本机（CPU-only 纪律之外的环境依赖，正是
波一 Minor 7 刚清掉的那类）。故形状级断言 + 与坍缩形态的对照是本层的上限；
真正的 accept/reject 归真机套件。
"""
from framework.core.orchestrator import Orchestrator
from framework.dialects.gemma4.tool_grammar import build_tool_call_grammar

from .fake_engine import FakeEngine, FakeRegistry, FakeTool


class _ParamRegistry(FakeRegistry):
    """带**真实参数**的注册表桩：FakeRegistry 的 schema 是空 properties，
    在这里分辨不出「瘦身了」还是「本来就没参数」。"""

    def get_all_schemas(self):
        return [{
            "name": "run_shell_command",
            "description": "Run a shell command.\n多行描述的第二行（catalog 只取首行）",
            # 小写 type：与 MCPBaseTool.get_schema 的真实产出一致（builder 内部会大写化）
            "parameters": {"type": "object",
                           "properties": {"command": {"type": "string"},
                                          "cwd": {"type": "string"}},
                           "required": ["command"]},
        }]


def _has_arg_chain(grammar: str, tool: str) -> bool:
    """该工具的调用分支里，调用头与闭合之间是否还有键值链规则引用。

    坍缩形态是 `cN: "call:X{" "}"`——调用头之后直接闭合，实参无处可放。
    只认调用头这一个形状（既有 test_tool_call_grammar_anchors_replay_format 已把它
    钉为契约），不碰参数键的转义与排版，故 builder 的纯格式重构不会误伤本判据。
    """
    head = f'"call:{tool}{{"'
    rule = next(ln for ln in grammar.splitlines() if head in ln)
    return rule.split(head, 1)[1].strip() != '"}"'


def _orch(mode):
    return Orchestrator(FakeEngine([]), _ParamRegistry([FakeTool("run_shell_command")]),
                        {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
                         "observation_max_chars": 4000, "tool_timeout_seconds": 5,
                         "max_parse_retries": 3, "thinking_mode": "manual",
                         "tool_schema_mode": mode})


def test_catalog_prompt_is_slim_but_grammar_keeps_parameters():
    """catalog 模式：prompt 面瘦身（省前缀 token），语法面保留完整参数。"""
    o = _orch("catalog")
    decl = next(t for t in o._tools_decl if t.get("name") == "run_shell_command")
    gram = next(t for t in o._grammar_tools if t.get("name") == "run_shell_command")

    assert "parameters" not in decl                      # 瘦身生效：省的就是这块
    assert decl["description"] == "Run a shell command."  # 只取首行
    assert gram["parameters"]["properties"]["command"]    # 语法源不跟着瘦


def test_catalog_grammar_admits_arguments_not_only_empty_calls():
    """要害：catalog 模式下带参调用必须仍能通过约束采样。

    缺陷形态是语法坍缩成 `c1: "call:run_shell_command{" "}"`——模型被物理钳成
    只能发空实参。这条直接比对两种语法源产出的语法体。
    """
    o = _orch("catalog")
    good = build_tool_call_grammar(o._grammar_tools)
    broken = build_tool_call_grammar(o._tools_decl)      # 修复前实际喂进去的东西

    assert _has_arg_chain(good, "run_shell_command")     # 带参分支在
    # 对照：瘦目录建出来的语法确实是坍缩的（证明上一条不是白断言）
    assert not _has_arg_chain(broken, "run_shell_command")
    # 元工具两份都保留完整参数（它们本就不瘦身）——证明坍缩只发生在被瘦的那批
    assert _has_arg_chain(broken, "update_plan")


def test_full_mode_declaration_and_grammar_are_identical():
    """full 模式（当前生产档）两者逐字同值——本次解耦对生产路径零行为变化。"""
    o = _orch("full")
    assert o._grammar_tools == o._tools_decl


def test_meta_tools_shared_by_both_faces():
    """元工具集合两面同源：catalog 模式多一个 get_tool_schema，且始终带完整参数。

    两面若各自维护一份，语法就可能允许（或禁止）一个声明里不存在（或存在）的工具。
    """
    names = lambda ts: {t.get("name") for t in ts}
    cat, full = _orch("catalog"), _orch("full")

    assert names(cat._grammar_tools) == names(cat._tools_decl)
    assert names(full._grammar_tools) == names(full._tools_decl)
    assert "get_tool_schema" in names(cat._tools_decl)
    assert "get_tool_schema" not in names(full._tools_decl)
    # 元工具本来就要带参调用，catalog 模式下也不瘦身
    meta = next(t for t in cat._tools_decl if t.get("name") == "update_plan")
    assert meta.get("parameters")


def test_grammar_cache_key_separates_same_names_different_schemas():
    """语法缓存键必须认内容，不能只认名字。

    解耦之后，「同名不同 schema」的两份声明在同一进程内首次真实存在（瘦目录 vs
    完整 schema）。名字做键会让第二份静默命中第一份的语法——服务端那份缓存还是
    跨客户端共享的，一个版本歪斜的旧客户端就能毒化所有人，且不报错、不 fail-open。
    """
    from framework.core.constrained import ToolConstraintEngine as TCE

    slim = [{"name": "run_shell_command", "description": "Run a shell command."}]
    full = [{"name": "run_shell_command", "description": "Run a shell command.",
             "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}]

    assert TCE._tools_key(slim) != TCE._tools_key(full)            # 同名不同 schema → 不同键
    assert TCE._tools_key(full) == TCE._tools_key(list(full))      # 同内容同键（缓存仍有效）

    two_a = full + [{"name": "read_file", "parameters": {"type": "object"}}]
    assert TCE._tools_key(two_a) != TCE._tools_key(full)           # 工具集变了必须换键
    # 顺序无关：保住旧键（名字集合）的这条性质，否则声明换个次序就白白重编译等价语法
    assert TCE._tools_key(two_a) == TCE._tools_key(list(reversed(two_a)))


def test_engine_feeds_constraint_face_from_grammar_tools(monkeypatch):
    """引擎侧消费：guard / 语法体 / tool_specs 三处都必须吃 grammar_tools。

    只测「编排层发了这个 kwarg」是不够的——首过评审用变异体证明：把引擎里的
    `gtools` 改回 `tools`（等于撤销修复），全量金测试照样绿。本条钉的正是那三行。
    `_tool_guard` 真身要 llguidance + 真 HF 分词器，故桩掉它并记录入参；被桩掉的
    只是「能不能建 guard」，三处消费面照常执行。
    """
    import asyncio
    from types import SimpleNamespace

    from .test_prompt_stream import _FakeBackend, _mk_engine

    slim = [{"name": "run_shell_command", "description": "Run a shell command."}]
    full = [{"name": "run_shell_command", "description": "Run a shell command.",
             "parameters": {"type": "OBJECT", "properties": {"command": {"type": "STRING"}},
                            "required": ["command"]}}]

    def _mk_sess():
        return SimpleNamespace(history=[{"role": "system", "content": "s"},
                                        {"role": "user", "content": "hi"}],
                               images=[], audios=[], brain_memory_state=None,
                               cancel_requested=False, inject_env_context=False,
                               prompt_stream=None)

    be = _FakeBackend()
    eng = _mk_engine(be)
    seen = {}

    def _fake_guard(self, tools):
        seen["guard_tools"] = tools
        # 非 None 即走约束分支；masked_positions 是收尾仪表读的字段
        return SimpleNamespace(masked_positions=0)

    monkeypatch.setattr(type(eng), "_tool_guard", _fake_guard)

    asyncio.run(eng.generate_once(_mk_sess(), tools=slim, grammar_tools=full))
    assert seen["guard_tools"] == full                       # guard 建在完整 schema 上
    assert be.features[0]["tool_specs"] == full              # 远端重建 guard 的那份
    assert '"command:"' in be.features[0]["tool_constraint"]["body"]  # 语法体带参数

    # 不传 grammar_tools 时沿用 tools（沙箱/摘要器/审计等直调路径语义不变）
    asyncio.run(eng.generate_once(_mk_sess(), tools=slim))
    assert seen["guard_tools"] == slim
    assert be.features[1]["tool_specs"] == slim


def test_grammar_tools_reach_the_engine(make_session):
    """接线：编排层确实把语法源作为独立入参发给 engine.generate_once。

    只覆盖「编排层→引擎」这一跳；引擎内部的消费由上一条钉死。
    """
    import asyncio

    from framework.core.journal import new_turn_id

    engine = FakeEngine([{"text": "好的。"}])
    o = Orchestrator(engine, _ParamRegistry([FakeTool("run_shell_command")]),
                     {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
                      "observation_max_chars": 4000, "tool_timeout_seconds": 5,
                      "max_parse_retries": 3, "thinking_mode": "manual",
                      "tool_schema_mode": "catalog"})
    s = make_session()
    s.add_user_message("跑个命令")
    s.journal.emit("turn_queued", {"prompt": "跑个命令", "attachments": [], "gen": {},
                                   "origin": "chat"}, turn_id=new_turn_id())
    s.journal.emit("turn_started", {})
    asyncio.run(o.run_turn(s, journal=s.journal, temperature=0.2, min_p=0.05,
                           enable_thinking=False, thinking_budget=2048, max_tokens=4096))

    gtools = engine.calls[0]["grammar_tools"]
    shell = next(t for t in gtools if t.get("name") == "run_shell_command")
    assert shell["parameters"]["properties"]["command"]
