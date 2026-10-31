"""Qwen3.5 家族（含 Ornith 衍生档）的 prompt 组装：ChatML + XML 式工具调用体。

对照官方 chat_template.jinja（Qwen3.5-9B 与 Ornith-1.0-9B 逐行 diff 过，二者仅一处
不同）逐段镜像，但按本仓的**前缀分层布局**（KV 缓存友好，见 gemma4 同名文件）组织：
  [稳定前缀] system 提示词 + 工具声明（启动后不变）
  [追加区]   对话历史（只增不改）
  [可变尾部] task_block + tail_notes + 环境锚点块
与官方模板的三处刻意差异（都有据）：
  1. **思考恒回放**：官方 Qwen3.5 模板只回放"末问之后"轮次的 <think>，Ornith 模板
     恒回放全部——本 builder 取 Ornith 口径（长链任务保留推理轨迹 + 历史字节稳定
     不随"哪条是末问"漂移，KV 前缀不断裂）。对 Qwen3.5 是良性超集。
  2. **工具声明格式沿官方**（system 块 <tools> JSON 清单 + XML 调用格式教学），但
     教学文案压缩为要点——官方全文近千 token，弱模型注意力稀释（D4 口径）。
  3. 环境锚点块/任务块/尾注放历史之后的独立 system 轮（官方模板无此概念，ChatML
     对多 system 轮天然宽容；置尾=变动只打掉尾部缓存，与 gemma4 同布局）。
  4. **reasoning_effort 与 enable_thinking 解耦**（2026-08-22 透传波，首过评审
     C1-C3 逼出的架构决策）：官方模板只在开思考时注入效率指令，但官方语境里
     思考开关是会话级常量；本仓是**逐迭代动态**（thinking_mode dynamic 下每轮
     iteration 0 恒开思考），而指令注入在首 system = 稳定前缀的头部——若随
     思考开关翻转，每次翻转都打掉整条 KV 前缀（A8 实测过的 23s 级冷预填，
     正是上位解要防的事）。故本 builder 只由注册表键驱动：**不设键 = 恒不注入
     （官方 medium 同形，零行为变化）；显式设 xhigh/low = 恒注入、跨迭代字节
     恒定**。代价：思考关闭的迭代也带着这句指令（语义无害，模型不思考时它是
     一句闲话）；换取：前缀稳定性与 prompt_stream 全/增量渲染的一致性。

调用体格式（**方言事实**，ResponseParser 按同一格式解析，两处锚定一致）：
  <tool_call>
  <function=NAME>
  <parameter=KEY>
  VALUE（可多行，原文不转义——该格式的全部意义所在）
  </parameter>
  </function>
  </tool_call>
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..env_facts import compute_static_env_lines
from ...core.dialect_protocol import TASK_BLOCK_RETRACT_NOTE

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"
TOOL_RESP_OPEN = "<tool_response>"
TOOL_RESP_CLOSE = "</tool_response>"
VISION_PLACEHOLDER = "<|vision_start|><|image_pad|><|vision_end|>"

# 工具调用格式教学（官方模板同款语义，压缩为要点；出现在稳定前缀，改动即打缓存）
_TOOL_FORMAT_GUIDE = (
    "\n\nIf you choose to call a function ONLY reply in the following format "
    "with NO suffix:\n\n"
    "<tool_call>\n<function=example_function_name>\n"
    "<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
    "</function>\n</tool_call>\n\n"
    "Reminder:\n"
    "- Function calls MUST follow the specified format: an inner <function=...></function> "
    "block must be nested within <tool_call></tool_call> XML tags\n"
    "- Required parameters MUST be specified\n"
    "- You may reason in natural language BEFORE the function call, but NOT after"
)


# reasoning_effort 效率指令（Qwen3.8 官方模板逐字节移植，2026-08-22 透传波）：
# xhigh 注长句、low 注短句、medium 不注；注入门控见模块头差异 4（只由注册表键
# 驱动，与 enable_thinking 解耦）。句子字节不许改写——差分测试直接对官方模板
# 渲染结果（backend/models/ 依据件）。两个名字都是被测契约（公开导出）。
REASONING_EFFORTS = ("xhigh", "medium", "low")
REASONING_INSTRUCTIONS = {
    "xhigh": ("Reasoning effort is set to xhigh. Please think carefully through "
              "the task, validate key assumptions, consider plausible "
              "alternatives, and prioritize correctness, consistency, and "
              "clarity in the final answer."),
    "medium": "",
    "low": ("Reasoning effort is set to low. Keep your thinking brief and "
            "focused, moving directly to the conclusion without unnecessary "
            "elaboration."),
}


def format_tool_declaration(tool: Dict[str, Any]) -> str:
    """单件工具声明：<tools> 块内的一行 JSON。公开给可见文本预算闸（D28：两家族同口径
    同一条 12300 线——只数逐件声明之和，不含 "# Tools" 头、<tools> 包裹与调用格式教学，
    对齐 gemma4 档只数逐件 DSL、不算 <|tool> 包裹；tests/test_model_visible_text.py）。
    format_tools_declaration 同走本函数，两处不可能漂移。"""
    return json.dumps(tool, ensure_ascii=False)


def format_tools_declaration(tools: List[Dict[str, Any]]) -> str:
    """工具声明块（system 块正文，不含 im_start/im_end 包裹）：官方形态 <tools> 内
    每行一个 JSON 声明（format_tool_declaration）+ 调用格式教学。builder 内部走本函数。
    空/None → 空串。"""
    if not tools:
        return ""
    lines = "\n".join(format_tool_declaration(t) for t in tools)
    return ("# Tools\n\nYou have access to the following functions:\n\n"
            f"<tools>\n{lines}\n</tools>" + _TOOL_FORMAT_GUIDE)


class Qwen35PromptBuilder:
    """ChatML 组装器。构造后只读；同一实例可被多次生成共享。"""

    def __init__(self, processor, env: Optional[Dict[str, Any]] = None, *,
                 reasoning_effort: Optional[str] = None):
        # markers 是家族字面量事实（Qwen3.5/Ornith tokenizer 实测全部单 token），
        # 不经 special_tokens_map 取名——Qwen 的 map 里没有为这些起键名。
        self.env = env or {}
        self.static_env_lines = compute_static_env_lines(self.env)
        # None = 注册表未设键 = 恒不注入（零行为变化；官方 medium 同形）。
        # 给了值就必须合法：非法值装配期拒启，与官方模板 raise_exception 同语义
        # ——静默取缺省会把配置拼写错伪装成「怎么没生效」。
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"reasoning_effort 非法：{reasoning_effort!r}——支持 xhigh/"
                f"medium/low（Qwen3.8 官方模板同款取值域），或不设键=不注入")
        self.reasoning_effort = reasoning_effort
        self.reasoning_instructions = (
            REASONING_INSTRUCTIONS[reasoning_effort] if reasoning_effort else "")

    def _extract_text(self, item: Dict[str, Any]) -> str:
        text = item.get("text") or item.get("content") or ""
        if isinstance(text, (dict, list)):
            return json.dumps(text, ensure_ascii=False)
        return str(text)

    def _system_block(self, content: str) -> str:
        return f"{IM_START}system\n{content}{IM_END}\n"

    def _build_tools_declaration(self, tools: List[Dict[str, Any]]) -> str:
        """官方形态：system 块内 <tools> 每行一个 JSON 声明 + 调用格式教学。"""
        return format_tools_declaration(tools)

    def _build_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> str:
        """历史回放：官方 XML 格式。非字符串参数值 tojson（与官方模板同口径）。"""
        result = ""
        for call in tool_calls:
            if call.get("type") == "error":
                continue
            func = call.get("function", call)
            name = func.get("name", "")
            args = func.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            params = ""
            for k, v in (args or {}).items():
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                elif not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False)  # 数字/布尔/None → JSON 字面量
                params += f"<parameter={k}>\n{v}\n</parameter>\n"
            result += (f"{TOOL_CALL_OPEN}\n<function={name}>\n{params}"
                       f"</function>\n{TOOL_CALL_CLOSE}\n")
        return result

    #: 工具观察里唯一允许进模型上下文的键（与 gemma4 同一条白名单纪律——
    #: tool_responses 条目兼任账本/渲染两用面，新挂元数据默认不给模型看）
    _MODEL_VISIBLE_KEYS = ("name", "response", "content")

    def _build_tool_responses(self, tool_responses: List[Dict[str, Any]]) -> str:
        """官方形态：tool 结果以 user 轮承载，每个结果一个 <tool_response> 块。"""
        blocks = ""
        for resp in tool_responses:
            visible = {k: resp[k] for k in self._MODEL_VISIBLE_KEYS if k in resp}
            blocks += (f"{TOOL_RESP_OPEN}\n"
                       f"{json.dumps(visible, ensure_ascii=False)}\n{TOOL_RESP_CLOSE}\n")
        return f"{IM_START}user\n{blocks}{IM_END}\n"

    def build(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
              enable_thinking: bool = False, task_block: Optional[str] = None,
              tail_notes: Optional[List[str]] = None, include_env: bool = False,
              env_extra: Optional[List[str]] = None) -> str:
        prompt = ""  # Qwen 家族无 BOS（tokenizer_config bos_token: null）
        tools_emitted = False   # 声明只发一次（评审 M3：旧写法跟在**每条** system
                                # 之后——滚动摘要/中段护栏都是 system，声明会发多遍）
        # reasoning_effort 效率指令：只由注册表键驱动、与 enable_thinking 解耦
        # （模块头差异 4——随思考开关翻转会打掉整条 KV 前缀）。只进**开头的**
        # system（messages[0]）；中段 system（滚动摘要/护栏，含增量路渲染的）
        # 一律不带——官方模板对非开头 system 直接 raise，我方宽容布局下的等价
        # 纪律就是"绝不中段注入"。
        reasoning = self.reasoning_instructions
        first_is_system = bool(messages) and messages[0].get("role") == "system" \
            and "tool_responses" not in messages[0]
        reasoning_emitted = False

        for idx, msg in enumerate(messages):
            role = msg.get("role", "user")

            # tool 结果轮：官方以 user 轮 + <tool_response> 块承载，独立成段
            if role == "tool" or "tool_responses" in msg:
                if "tool_responses" in msg:
                    prompt += self._build_tool_responses(msg["tool_responses"])
                else:
                    content = msg.get("content", "")
                    prompt += self._build_tool_responses(
                        [{"name": msg.get("name", "unknown_tool"),
                          "content": content if isinstance(content, str) else str(content)}])
                continue

            body = ""
            thoughts = msg.get("thoughts", "")
            if thoughts and role == "assistant":
                # 思考恒回放（Ornith 官方口径，见文件头差异 1）
                body += f"{THINK_OPEN}\n{thoughts}\n{THINK_CLOSE}\n\n"

            content = msg.get("content", "")
            if isinstance(content, str) and content:
                body += content
            elif isinstance(content, list):
                parts = []
                for item in content:
                    t = item.get("type", "")
                    if t in ("image", "image_url"):
                        parts.append(VISION_PLACEHOLDER)
                    elif t == "text":
                        parts.append(self._extract_text(item))
                    # 音频：本家族无音频编码器（capabilities 不含 audio），摄入层的
                    # ASR 转写路径在上游已把音频变文本，这里不该再见到 audio 项
                body += "\n".join(parts)

            if role == "assistant" and msg.get("tool_calls"):
                calls = self._build_tool_calls(msg["tool_calls"])
                if calls:
                    body = (body.rstrip() + "\n\n" + calls) if body.strip() else calls

            if idx == 0 and first_is_system and reasoning:
                # 官方模板形态：指令在开头 system 内容之前，'\n\n' 相接
                body = f"{reasoning}\n\n{body}"
                reasoning_emitted = True
            prompt += f"{IM_START}{role}\n{body.rstrip()}{IM_END}\n"

            # 工具声明并入稳定前缀：紧跟首条 system（与 gemma4 同布局同纪律：
            # 后续 system——滚动摘要/护栏——一律不带，增量路渲染 system 亦不带）
            if role == "system" and tools and not tools_emitted:
                prompt += self._system_block(self._build_tools_declaration(tools))
                tools_emitted = True

        if tools and not tools_emitted:
            # 一条 system 消息都没有时声明单独成块、排在最前（gemma4 同兜底：
            # 声明整个丢掉的表现是「装作调用过了」的幻觉，极难归因）
            prompt = self._system_block(self._build_tools_declaration(tools)) + prompt
        if reasoning and not reasoning_emitted:
            # 无 system 消息：指令单独成块、排在一切之前（官方无 system 分支同义；
            # 有工具时官方把指令放在工具 system 块**开头**，故须在工具兜底块之前）
            prompt = self._system_block(reasoning) + prompt

        # 可变尾部：任务状态块 → 一次性护栏尾注 → 环境锚点（与 gemma4 同序同语义）
        if task_block:
            prompt += self._system_block(task_block)
        if tail_notes:
            notes = "\n".join(n for n in tail_notes if n and n.strip())
            if notes.strip():
                prompt += self._system_block(notes)

        prompt += self._system_block(self._env_block_content(include_env, env_extra))

        prompt += self._generation_head(enable_thinking)
        return prompt

    def _env_block_content(self, include_env: bool,
                           env_extra: Optional[List[str]] = None) -> str:
        """环境锚点块内容（全量渲染尾部与增量段共用同一字节权威——上位解）。"""
        _now = datetime.now()
        env_lines = [f"Current date: {_now.strftime('%Y-%m-%d')} ({_now.strftime('%A')})"]
        if include_env:
            # 隐私门控写死在 builder（与 gemma4 同条纪律）：env_extra（长期记忆索引）
            # 同为个人事实，沙箱替身即便误传也被这道闸挡下
            if self.static_env_lines:
                env_lines.extend(self.static_env_lines)
            if env_extra:
                env_lines.extend(l for l in env_extra if l and l.strip())
        return "\n".join(env_lines)

    def _generation_head(self, enable_thinking: bool) -> str:
        """生成头（全量渲染与增量段共用）。关思考时预填空 <think> 块（官方模板
        enable_thinking=false 同款）：Qwen3.5/Ornith 默认自发思考，空块预填是
        本家族的"幽灵思考抑制"，恒开无开关。"""
        head = f"{IM_START}assistant\n"
        if enable_thinking:
            head += f"{THINK_OPEN}\n"
        else:
            head += f"{THINK_OPEN}\n\n{THINK_CLOSE}\n\n"
        return head

    def tail_state(self, task_block: Optional[str] = None,
                   include_env: bool = False,
                   env_extra: Optional[List[str]] = None) -> Dict[str, str]:
        """全量渲染路径落进流的尾块快照（上位解「变了才追加」的比对基准）。
        与 build 尾部各调一次 datetime.now()——跨午夜微秒错位至多多追加一次
        或旧日期滞留一天、次日自愈，与 gemma4 同口径不设共享时钟。"""
        return {"env": self._env_block_content(include_env, env_extra),
                "task": task_block or "", "task_appends": 0}

    #: 任务块冻结快照的收拢阈值（评审 C2，与 gemma4 同值同语义）
    TASK_APPENDS_COLLAPSE = 6

    def build_prompt_delta(self, new_messages: List[Dict[str, Any]], *,
                           enable_thinking: bool = False,
                           task_block: Optional[str] = None,
                           tail_notes: Optional[List[str]] = None,
                           include_env: bool = False,
                           env_extra: Optional[List[str]] = None,
                           prev_tail: Optional[Dict[str, str]] = None
                           ) -> Optional[Tuple[str, Dict[str, str]]]:
        """增量段（上位解，与 gemma4 同名方法同一契约）：接在「已记录字节流」
        （以一次生成的原始字节收尾、assistant 轮开放、<|im_end|> 不在流内）之后。

        恒以 `<|im_end|>\\n` 收口开头——停止符已喂/未喂两种情形的 token 流都与
        缓存记录纯追加对齐（见 gemma4_generate._fed_length）。tool 结果按官方
        形态独立成 user 轮 <tool_response> 块（本方言无折轮概念，形态与全量
        渲染完全一致）。尾块按 prev_tail 变了才追加。返回 None = 增量不支持的
        消息形态，调用方退全量重渲染。
        """
        prev_tail = prev_tail or {}
        delta = f"{IM_END}\n"
        for msg in new_messages:
            role = msg.get("role", "user")
            if role == "assistant":
                return None   # 回放字节恒等是全量路的 §S.17 难题，增量绝不重蹈
            if msg.get("thoughts") or (role != "tool" and (
                    "tool_responses" in msg or msg.get("tool_calls"))):
                return None   # 全量渲染另有字节的形态：宁退全量不静默丢字段
            if role == "tool":
                if "tool_responses" in msg:
                    delta += self._build_tool_responses(msg["tool_responses"])
                else:
                    content = msg.get("content", "")
                    delta += self._build_tool_responses(
                        [{"name": msg.get("name", "unknown_tool"),
                          "content": content if isinstance(content, str) else str(content)}])
            elif role in ("user", "system"):
                content = msg.get("content", "")
                body = ""
                if isinstance(content, str) and content:
                    body += content
                elif isinstance(content, list):
                    parts = []
                    for item in content:
                        t = item.get("type", "")
                        if t in ("image", "image_url"):
                            parts.append(VISION_PLACEHOLDER)
                        elif t == "text":
                            parts.append(self._extract_text(item))
                    body += "\n".join(parts)
                elif not isinstance(content, str):
                    return None
                delta += f"{IM_START}{role}\n{body.rstrip()}{IM_END}\n"
            else:
                return None   # 未知角色：增量不支持
        env_content = self._env_block_content(include_env, env_extra)
        prev_task = prev_tail.get("task", "")
        task_appends = int(prev_tail.get("task_appends", 0) or 0)
        if task_block:
            if task_block != prev_task:
                if task_appends >= self.TASK_APPENDS_COLLAPSE:
                    return None   # 快照堆积达阈：退全量收拢（评审 C2）
                delta += self._system_block(task_block)
                task_appends += 1
        elif prev_task:
            delta += self._system_block(TASK_BLOCK_RETRACT_NOTE)
            task_appends += 1
        new_tail = {"env": env_content, "task": task_block or "",
                    "task_appends": task_appends}
        if tail_notes:
            notes = "\n".join(n for n in tail_notes if n and n.strip())
            if notes.strip():
                delta += self._system_block(notes)
        if env_content != prev_tail.get("env"):
            delta += self._system_block(env_content)
        delta += self._generation_head(enable_thinking)
        return delta, new_tail
