"""gemma-4 方言的工具调用体**约束语法**（§7-8，2026-08-14 取代 tool_schema.py）。

产出 llguidance 的 Lark 语法源码，约束 `<|tool_call>` … `<tool_call|>` 之间的字节。
迁移前这里产出的是 JSON schema——那与 prompt_builder 回放的自建 JSON 调用体配套；
迁移到官方紧凑 DSL 后，调用体不再是 JSON（裸键、`<|"|>` 定界的原文字符串块），
JSON schema 无法表达，故整体改为 Lark。

**为什么不能用字面量表达 `<|"|>`**（实测，2026-08-14）：它是单个特殊 token（id 52，
special_tokens_map 里的 escape_token）。特殊 token 对字节级语法不可见——把它写成
字面字符串 `"<|\\"|>"` 会让 matcher 在该 token 处直接判失败（Parser Error: byte
'ÿ' fails parse）。必须用 llguidance 的特殊 token 引用语法 `<|"|>`；名字打错会
**响亮报错**并列出可用 token（不会静默退化成字节匹配），故此处安全。

**键序**：官方模板三处（声明体/调用实参/观测体）全部经 jinja `dictsort`，模型看到
的键序恒为字典序（大小写不敏感）。本语法按同一口径**要求**字典序——不是洁癖：
乱序放行会让约束与回放漂移，而漂移正是 §S.17 的病根。

**约束强度**：对**声明了类型的属性**——键名限定在声明的属性集内（未知键拒）、
必填键强制、每个键至多一次（有序链天然保证）、值按声明类型分型；比迁移前的
JSON schema 口径不弱。
⚠️ 但走 `_generic()` 兜底的位置（未声明类型的参数、`type: OBJECT` 缺 properties、
`type: ARRAY` 缺 items）只保证**结构**合法：那里的键名不受限、也不查重。声明写得
含糊，约束就只能给到这么多——这不是语法的缺陷，是声明的信息量上限。

⚠️ 改动前先看 `prompt_builder._build_tool_calls` 与 `dsl.format_tool_call`：三者
必须锚定同一形状。约束形状与回放格式一旦漂移，模型会被约束成自己历史里没见过
的东西——那正是本次迁移要消灭的故障类。
"""
from typing import Any, Dict, List, Mapping

from .dsl import tool_function

#: llguidance Lark 里对 escape_token 的引用（按名，非字面量——见模块 docstring）
_ESC = '<|"|>'

#: 通用值（声明没给类型/给了未知类型时的兜底），以及它依赖的终结符。
#: 只在真的用到时才拼进语法，避免给简单工具塞无用规则。
_GENERIC_RULES = f"""
gval: gstr | gnum | gbool | gnull | garr | gobj
gnull: "null" | "None"
gstr: {_ESC} GBODY {_ESC}
gnum: /-?[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?/
gbool: "true" | "false"
garr: "[" "]" | "[" gval ("," gval)* "]"
gobj: "{{" "}}" | "{{" GKEY ":" gval ("," GKEY ":" gval)* "}}"
GBODY: /(?s:.*)/
GKEY: /[^:,{{}}\\[\\]\\n]+/
"""


def _lit(s: str) -> str:
    """把任意文本编成 Lark 字符串字面量（双引号 + 反斜杠转义）。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sorted_keys(props: Mapping[str, Any]) -> List[str]:
    """与 dsl._dictsort 同口径：大小写不敏感的字典序。"""
    return sorted(props, key=lambda k: k.lower() if isinstance(k, str) else k)


class _Builder:
    def __init__(self) -> None:
        self.rules: List[str] = []
        self._n = 0
        self.needs_generic = False

    def fresh(self, stem: str) -> str:
        self._n += 1
        return f"{stem}{self._n}"

    # ---- 值 ----
    def value(self, spec: Any) -> str:
        """一个参数声明 → 匹配其取值的规则名。

        `nullable: true` 的属性额外放行 null/None：声明体会吐 `nullable:true` 告诉
        模型可以传空，约束若不认就成了「声明许诺、约束拒收」——模型照声明写反而被
        挡，正是约束与回放脱锚的一种（首过评审揪出）。`None` 亦须在列：官方
        format_argument 把 Python None 渲染成裸 `None`（dsl 的对表结论）。
        """
        if not isinstance(spec, Mapping):
            self.needs_generic = True
            return "gval"
        if spec.get("nullable"):
            inner = self.value({k: v for k, v in spec.items() if k != "nullable"})
            name = self.fresh("v")
            self.rules.append(f'{name}: {inner} | "null" | "None"')
            return name
        vtype = str(spec.get("type") or "").upper()

        if vtype == "STRING":
            enum = spec.get("enum")
            enum = [e for e in (enum or []) if isinstance(e, str) and e]
            if enum:
                # 只收非空字符串成员：空串会生成空 Lark 字面量（语法错），非字符串
                # 成员 stringify 出来的字面量 format_argument 根本不会产出
                alts = " | ".join(f"{_ESC} {_lit(e)} {_ESC}" for e in enum)
                name = self.fresh("v")
                self.rules.append(f"{name}: {alts}")
                return name
            name = self.fresh("v")
            body = self.fresh("SBODY").upper()
            # 定界符是特殊 token，串体可含任意字节（含换行）——run_python 传多行代码
            # 走的就是这条路，收窄它等于把合法调用挡在门外。
            self.rules.append(f"{name}: {_ESC} {body} {_ESC}")
            self.rules.append(f"{body}: /(?s:.*)/")
            return name
        if vtype == "INTEGER":
            name = self.fresh("v")
            self.rules.append(f"{name}: /-?[0-9]+/")
            return name
        if vtype == "NUMBER":
            name = self.fresh("v")
            self.rules.append(f"{name}: /-?[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?/")
            return name
        if vtype == "BOOLEAN":
            name = self.fresh("v")
            self.rules.append(f'{name}: "true" | "false"')
            return name
        if vtype == "ARRAY":
            items = spec.get("items")
            inner = self.value(items) if isinstance(items, Mapping) and items else self._generic()
            name = self.fresh("v")
            self.rules.append(f'{name}: "[" "]" | "[" {inner} ("," {inner})* "]"')
            return name
        if vtype == "OBJECT":
            props = spec.get("properties")
            if isinstance(props, Mapping) and props:
                chain = self.object_chain(props, set(spec.get("required") or []))
                name = self.fresh("v")
                self.rules.append(f'{name}: "{{" {chain} "}}"')
                return name
            return self._generic()
        return self._generic()

    def _generic(self) -> str:
        self.needs_generic = True
        return "gval"

    # ---- 有序可选键链 ----
    def object_chain(self, props: Mapping[str, Any], required: set) -> str:
        """生成「按字典序、必填强制、未知键拒」的键值链，返回入口规则名。

        两条链：T_i = 「还没吐过任何键」（首个键不带逗号），S_i = 「已吐过键」
        （之后每个键前置逗号）。可选键在两条链上各多一条跳过分支。规模线性于
        键数——枚举全排列会在参数一多时把 llguidance 撑到 ParserTooComplex。
        """
        keys = _sorted_keys(props)
        stem = self.fresh("o")
        empty = f"{stem}_e"
        self.rules.append(f"{empty}:")
        for i in range(len(keys) - 1, -1, -1):
            key = keys[i]
            vrule = self.value(props[key])
            nxt_t = f"{stem}_t{i + 1}" if i + 1 < len(keys) else empty
            nxt_s = f"{stem}_s{i + 1}" if i + 1 < len(keys) else empty
            alts_t = [f"{_lit(key + ':')} {vrule} {nxt_s}"]
            alts_s = [f"{_lit(',' + key + ':')} {vrule} {nxt_s}"]
            if key not in required:
                alts_t.append(nxt_t)
                alts_s.append(nxt_s)
            self.rules.append(f"{stem}_t{i}: " + " | ".join(alts_t))
            self.rules.append(f"{stem}_s{i}: " + " | ".join(alts_s))
        return f"{stem}_t0" if keys else empty


def build_tool_call_grammar(tools: List[Dict[str, Any]]) -> str:
    """把注册工具编成调用体的 Lark 语法（按工具 anyOf 分支）。

    锚定 `dsl.format_tool_call` 的回放形态：`call:名字{k:v,…}`，不含起止标记
    （标记由 ToolCallGuard 的状态机锚定，进语法的只是标记之间那一段）。
    """
    b = _Builder()
    branches: List[str] = []
    for tool in tools or []:
        fn = tool_function(tool)
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters")
        props = params.get("properties") if isinstance(params, Mapping) else None
        head = _lit(f"call:{name}{{")
        rule = b.fresh("c")
        if isinstance(props, Mapping) and props:
            chain = b.object_chain(props, set((params.get("required") or [])))
            b.rules.append(f'{rule}: {head} {chain} "}}"')
        else:
            # 声明里没有属性（官方声明体即 parameters:{type:<|"|>OBJECT<|"|>}）：
            # 唯一合法调用就是空实参
            b.rules.append(f'{rule}: {head} "}}"')
        branches.append(rule)

    if not branches:
        # 没有可调用的工具：不产出可接受的调用体。调用方（AgentEngine/server）
        # 在 tools 为空时根本不建 guard，这里只是防御。
        return 'start: "call:{}"\n'
    src = "start: " + " | ".join(branches) + "\n" + "\n".join(b.rules) + "\n"
    if b.needs_generic:
        src += _GENERIC_RULES
    return src
