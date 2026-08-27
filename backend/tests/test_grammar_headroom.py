"""D0②:llguidance 语法余量预验（tokenizer-only,不加载权重）。

守两件事:① **全量确定性工具集**的调用体语法在真 LLMatcher（真 LLTokenizer）上
可用——`validate_grammar`/惰性构造是**恒真判据**（复审实测:300 件工具 21 万字符的
语法 30ms 静默通过）,必须逐 token 喂一段真实调用体走 `consume` 才算数;
② 带参工具在语法里必须有**非空实参**分支——§6.5 的病（目录条目无 parameters →
语法坍缩成 `call:name{}`）波三已修,这里在语法源与真解析两面钉住不回潮。

工具清单:pkgutil 扫描 framework.mcp.tools,**真构造优先**（rag_search 等在
`__init__` 里组装 parameters,`__new__` 只读类属性会把它错归零参桶——复审 DC3）,
构造失败才退 `__new__`。`rag_admin` 除外:其模块体 import 即发 3s 网络探活,金测试
套件不做网络 I/O(复审 S10);其工具由同一探活门控,不进确定性清单,与动态 api_tools
一并挂账为余量结论的盲区。
"""
import importlib
import pkgutil
from pathlib import Path

import pytest

llguidance = pytest.importorskip("llguidance")  # venv 装了它;缺装时跳过并显示原因

from framework.core.orchestrator import (COMPLETE_STEP_SCHEMA, GET_TOOL_SCHEMA_SCHEMA,
                                         REMEMBER_SCHEMA, UPDATE_PLAN_SCHEMA)
from framework.dialects.gemma4.dsl import format_tool_call
from framework.dialects.gemma4.tool_grammar import build_tool_call_grammar
from framework.mcp.base import BaseTool

_SKIP_MODULES = {"rag_admin"}      # 模块体 import 即网络探活——套件纯净优先,盲区挂账


def _deterministic_tool_schemas():
    import framework.mcp.tools as tools_pkg
    schemas, seen = [], set()
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        if info.name in _SKIP_MODULES:
            continue
        try:
            mod = importlib.import_module(f"framework.mcp.tools.{info.name}")
        except Exception:
            continue          # 个别模块缺重依赖时跳过——floor 断言守住清单规模
        for obj in vars(mod).values():
            if (isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool
                    and getattr(obj, "name", "") and obj.name not in seen):
                inst = None
                try:
                    inst = obj()          # 真构造:__init__ 组装的 parameters 才收得全
                except Exception:
                    try:
                        inst = obj.__new__(obj)
                    except Exception:
                        continue
                try:
                    schemas.append(inst.get_schema())
                    seen.add(obj.name)
                except Exception:
                    continue
    assert len(schemas) >= 40, (
        f"确定性工具清单可疑地少({len(schemas)})——枚举路径坏了或依赖批量缺失"
        f"(floor 从 15 提到 40,复审 S9:三分之二蒸发时旧 floor 照绿)")
    return schemas


def _full_fleet():
    return _deterministic_tool_schemas() + [
        UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA, GET_TOOL_SCHEMA_SCHEMA]


def _real_lltokenizer():
    """真 LLTokenizer——取数统一走 tests/_tool_fleet.real_lltokenizer(经 model_selection
    决议,与引擎同源;D1 复审 S8:原实现只读 config inference.model,与 .model-selection
    脱钩,换 Qwen 档会拿错家族的尺)。"""
    from ._tool_fleet import real_lltokenizer
    return real_lltokenizer()


def test_full_fleet_grammar_parses_a_real_call_body():
    """★ 全量语法 + 真 tokenizer 构造 LLMatcher,并逐 token 喂一段带参调用体:
    这是唯一能触发 ParserTooComplex 类运行期失败的判据(复审 DC2:validate_grammar
    与惰性构造对 5 倍超限语法照样 30ms 放行)。"""
    fleet = _full_fleet()
    src = build_tool_call_grammar(fleet)
    ltok = _real_lltokenizer()
    grammar = llguidance.LLMatcher.grammar_from_lark(src)
    m = llguidance.LLMatcher(ltok, grammar)

    # 选一个带参工具,按方言渲染一条真实调用体,逐 token 走 consume
    target = next(s for s in fleet
                  if s["name"] == "run_shell_command"
                  and (s.get("parameters") or {}).get("properties"))
    body = format_tool_call(target["name"], {"command": "echo grammar-probe"})
    tokens = ltok.tokenize_str(body)
    assert tokens, "调用体分不出 token——tokenizer 面坏了"
    for t in tokens:
        assert not m.is_error(), f"语法在消费途中进入错误态: {m.get_error()}"
        m.consume_token(t)
    assert not m.is_error(), f"整段调用体消费完语法仍报错: {m.get_error()}"
    print(f"[grammar-headroom] 工具 {len(fleet)} 件,语法源 {len(src)} 字符,"
          f"调用体 {len(tokens)} token 全程可解析")


def test_parameterized_tools_keep_nonempty_branches():
    """带参工具的语法分支不许坍缩成空实参(§6.5 回潮探测)。
    阳性对照(复审 S16):同一份语法源里,无参工具**确实**以空实参形态存在——
    证明「坍缩形态」这个判据在当前发射器的引号/空白约定下真的匹配得出来。"""
    fleet = _full_fleet()
    src = build_tool_call_grammar(fleet)
    parameterized = [s["name"] for s in fleet
                     if (s.get("parameters") or {}).get("properties")]
    paramless = [s["name"] for s in fleet
                 if not (s.get("parameters") or {}).get("properties")]
    assert parameterized, "全量清单里竟无带参工具——枚举坏了"
    assert "rag_search" in parameterized, (
        "rag_search 掉进零参桶——__init__ 组装的 parameters 没被收到(复审 DC3 回潮)")
    for name in parameterized:
        assert f'"call:{name}{{" "}}"' not in src, (
            f"带参工具 {name} 的语法坍缩成空实参——§6.5 回潮")
    assert any(f'"call:{name}{{" "}}"' in src for name in paramless), (
        "阳性对照失效:发射器的坍缩形态已变,上面的否定断言从此恒真")
