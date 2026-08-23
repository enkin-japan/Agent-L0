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
- `tool_call_grammar` 归方言：它锚定的是本方言**回放给模型看的调用体格式**（原
  `constrained.build_tool_call_schema` 的 docstring 自陈此事）。而
  `core/constrained.py` 的状态机本身框架无关、方言无关（起止标记夹一段调用体，
  llguidance 做 mask），故**留在 core**，只由方言喂参数（stc/etc + 语法）。
  §7-8（2026-08-14）把它由「返回 JSON schema」放宽为「返回 ToolCallGrammar」：
  gemma-4 迁到官方紧凑 DSL 后调用体不再是 JSON，schema 表达不了，方言需要能说
  「我的语法是 Lark」。core 只按 kind 分派，仍不解读语法内容。
- 模型级行为开关（如 gemma-4 大杯的 `suppress_ghost_thinking`）经 `create` 的
  `options` 透传——它们是**方言事实**，不该出现在 AgentEngine 的签名里。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

#: 任务状态块「撤销」尾注（上位解）：prompt 字节流只增不改后，task_block 从有到无
#: 时旧状态块会留在上下文里，必须追加一条撤销声明防止模型接续已死任务。内容方言
#: 无关（与环境事实同理），字面量收编在协议层——两方言各写一遍必然漂移（§S.17）。
TASK_BLOCK_RETRACT_NOTE = ("[System] The task status block above has concluded "
                           "and is no longer in effect.")


@dataclass(frozen=True)
class DialectMarkers:
    """本方言用于切分生成流的标记字面量。

    soc/eoc  频道块（thought / text）的起止
    stc/etc  工具调用体的起止

    ⚠️ stc/etc 必须各自是**单 token**：约束采样的状态机靠 token id 锚定
    （见 core/constrained.py 与 engine._tool_guard 的单 token 校验；inference/server
    的同款校验在 `_build_constraint_engine` 启动期跑，config 开约束而标记非单 token
    时 server **拒启**而非 fail-open——改标记的人要知道这条后果）。
    """
    soc: str
    eoc: str
    stc: str
    etc: str


@dataclass(frozen=True)
class ToolCallGrammar:
    """方言给出的调用体约束语法（§7-8）。

    kind  "lark" | "json_schema" —— core/constrained.py 据此选 llguidance 的编译入口；
          远程后端（vLLM structural-tag 等）据此判断自己能不能表达该语法。
    body  对应形态的语法体：Lark 源码字符串 / JSON schema 字典。
    """
    kind: str
    body: Any


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

    def build_prompt_delta(self, new_messages: List[Dict[str, Any]], *,
                           enable_thinking: bool = False,
                           task_block: Optional[str] = None,
                           tail_notes: Optional[List[str]] = None,
                           include_env: bool = False,
                           env_extra: Optional[List[str]] = None,
                           prev_tail: Optional[Dict[str, str]] = None
                           ) -> Optional[Tuple[str, Dict[str, str]]]:
        """（可选方法，上位解）渲染接在「已记录字节流」之后的增量段。

        调用方（core/prompt_stream.py 的消费面）保证：流以一次生成的原始字节结尾
        （开放的 assistant/model 轮，停止符不在流内），new_messages 是流覆盖点之后
        的新消息（生成对应的 assistant 消息已被跳过）。实现恒以**本方言的轮收口符
        开头**（后端记录侧截到真实已喂长度后，停止符已喂/未喂两种情形在此都归零
        excess，见 gemma4_generate._fed_length），随后渲染新消息、按 prev_tail
        「变了才追加」尾块（env 锚点/任务块），最后是生成头。
        返回 (增量字节, 新尾块状态)；遇到本方言增量不支持的消息形态（如 assistant
        重放）返回 None——调用方退全量重渲染，正确性永不受损。
        不实现本方法的方言天然全程走全量路（引擎按 getattr 探测）。
        """
        ...

    def tail_state(self, task_block: Optional[str] = None,
                   include_env: bool = False,
                   env_extra: Optional[List[str]] = None) -> Dict[str, str]:
        """（可选方法，上位解）全量渲染路径下本轮落进流的尾块内容快照，
        供下一轮增量段做「变了才追加」比对。键与 build_prompt_delta 的
        prev_tail 一致：{"env": 环境锚点块内容, "task": 任务状态块内容}。"""
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

    def tool_call_grammar(self, tools: List[Dict[str, Any]]) -> "ToolCallGrammar":
        """本方言回放格式对应的调用体约束语法（约束采样用）。

        必须与 build_prompt 回放工具调用的格式**锚定一致**：约束采样按此语法做
        logits mask，语法与回放格式一旦漂移，模型会被约束成自己历史里没见过的形状。
        不支持约束的方言抛 NotImplementedError（调用方 fail-open，退回解析纠错闭环）。
        """
        ...

    def render_tool_declaration(self, tool: Dict[str, Any]) -> str:
        """把单个工具 schema 渲染成**本方言 prompt 面的声明格式**（D0①）。

        catalog 模式下 get_tool_schema 按名取详情回喂模型——回裸 JSON 的话,模型拿到
        的形状与它在稳定前缀里见过的声明**不同形**,等于教它一种没见过的写法再要求
        它照第三种格式发调用。回本方言的声明串,取详情与常驻声明同形。
        方言未实现时协议层允许回退 JSON（调用方 getattr 兜底）——功能可用,只是次优。
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
