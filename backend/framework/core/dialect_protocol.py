"""模型方言协议（EXECUTION_LEDGER R0）：智能层与模型模板/输出格式之间的唯一边界。

AgentEngine（core/engine.py）持有一个 ModelDialect 实例；方言负责 prompt 组装、
原始输出反序列化，以及把「哪些标记划分频道与工具段」这一**模型家族事实**告诉引擎。
协议对上只承诺行为语义，不承诺实现——gemma-4 的频道式模板、将来的 ChatML / Harmony
系模板都是它的实现，全部住在 dialects/ 之下（与 backends/ 同构）。

**为什么需要这层**（CHARTER §2 自陈「仅有的 gemma 方言层：PromptBuilder /
ResponseParser / patch」）：InferenceBackend（P0）抽象了**算力承载**，却没抽象
**模型方言**。目标模型体量（30–70B 稠密 / 100–300B MoE）必然离开 gemma-4 家族
（那个体量的开源模型用 ChatML + <tool_call> JSON 或 Harmony 等异构方言），届时
prompt 模板与工具调用格式全变。没有这层接缝，换模型 = 改 engine 承重代码（撞铁律 1）。
R0 = 把 P0 对算力做过的那台协议手术，对方言再做一遍。

设计约定（与既有语义逐条对齐，**R0 零行为变化的依据**）：
- markers 是**方言事实**：引擎用它做思考播种、流过滤器切段、约束采样锚点、token
  去向仪表的正则。此前这些一律读 `ResponseParser` 的属性——**它事实上兼任了方言
  标记登记处**；R0 只是把这个隐式职责显式化，取值来源与结果完全不变。
- `build_prompt` / `parse_response` 的签名与原 `PromptBuilder.build` /
  `ResponseParser.parse` **逐字对齐**——R0 是搬运，不是重写。
- `tool_call_schema` 归方言：它锚定的是本方言**回放给模型看的调用体格式**（原
  `constrained.build_tool_call_schema` 的 docstring 自陈此事）。而
  `core/constrained.py` 的状态机本身框架无关、方言无关（起止标记夹一段 JSON，
  llguidance 做 mask），故**留在 core**，只由方言喂参数（stc/etc + schema）。
- 模型级行为开关（如 gemma-4 大杯的 `suppress_ghost_thinking`）经 `create` 的
  `options` 透传——它们是**方言事实**，不该出现在 AgentEngine 的签名里。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class DialectMarkers:
    """本方言用于切分生成流的标记字面量。

    soc/eoc  频道块（thought / text）的起止
    stc/etc  工具调用体的起止

    ⚠️ stc/etc 必须各自是**单 token**：约束采样的状态机靠 token id 锚定
    （见 core/constrained.py 与 engine._tool_guard 的单 token 校验）。
    """
    soc: str
    eoc: str
    stc: str
    etc: str


class ModelDialect(Protocol):
    """模型方言协议。全部同步；构造后只读（无可变状态，可被多次生成共享）。"""

    @classmethod
    def create(cls, processor, *, options: Optional[Dict[str, Any]] = None,
               env: Optional[Dict[str, Any]] = None) -> "ModelDialect":
        """按 processor（只消费其 tokenizer）与注册表条目装配方言。

        options: config.yaml `inference.model_registry` 的所选条目——模型级行为开关
                 （如 gemma-4 的 suppress_ghost_thinking）由方言自行解读。
        env:     config.yaml `agent.environment`（环境事实注入，隐私 opt-in）。
        """
        ...

    @property
    def markers(self) -> DialectMarkers:
        """本方言的流切分标记。引擎据此做思考播种/流过滤/约束锚点/仪表正则。"""
        ...

    def build_prompt(self, messages: List[Dict[str, Any]],
                     tools: Optional[List[Dict[str, Any]]] = None,
                     enable_thinking: bool = False,
                     task_block: Optional[str] = None,
                     tail_notes: Optional[List[str]] = None,
                     include_env: bool = False,
                     env_extra: Optional[List[str]] = None) -> str:
        """组装完整 prompt 字符串。

        实现须保持**前缀分层布局**（KV 缓存友好）：稳定前缀（system+工具声明）→
        追加区（历史，只增不改）→ 可变尾部（task_block + tail_notes）。
        include_env 是**隐私门控**：个人环境事实只进主编排轮，沙箱一次性会话
        替身传 False（backend/tests/test_prompt_env.py 守着这条）。
        env_extra（R4）是运行期才知道的环境事实（如长期记忆索引），实现**必须**把它
        与静态环境事实一起压在同一条 include_env 门控之下——它同样是个人事实。
        """
        ...

    def parse_response(self, raw: str) -> Dict[str, Any]:
        """把原始生成文本反序列化为 {thoughts, text, tool_calls}。

        **铁律级不变量**（backend/tests/test_response_parser.py 12 项守着）：
        标记残缺的任何事故形态都**不得把垃圾漏进正文**——有头无尾/有尾无头的工具
        调用一律转 error 调用走纠错闭环（而非漏成正文），未闭合的思考频道整段路由
        进 thoughts，error 的 raw_input 必须截断（它会被回显给模型并进账本）。
        """
        ...

    def thinking_prefill(self, enable_thinking: bool) -> str:
        """生成头的思考播种串（enable_thinking=False 时为空串）。

        必须用本方言的**实际 token**而非硬编码字面量——换模型/token 表时保持一致。
        """
        ...

    def tool_call_schema(self, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """本方言回放格式对应的调用体 JSON schema（约束采样用）。

        必须与 build_prompt 回放工具调用的格式**锚定一致**：约束采样按此 schema 做
        logits mask，schema 与回放格式一旦漂移，模型会被约束成自己历史里没见过的形状。
        """
        ...

    def parse_error_retry_hint(self) -> str:
        """解析纠错反馈里的**格式教学段**（方言事实）。

        编排层的容错闭环把无法解析的调用回喂给模型时，信封（System Error 前缀 +
        raw_input/错误原文回显）是编排策略，但"正确格式长什么样"只有方言知道——
        gemma4 教 JSON 调用体，qwen35 教 XML 调用体。硬编码任何一种都会把另一家族
        的模型往错误格式带偏（2026-07-30 评测框架波发现的接缝遗漏）。
        返回文本须与本方言 build_prompt 的工具教学**锚定一致**。
        """
        ...
