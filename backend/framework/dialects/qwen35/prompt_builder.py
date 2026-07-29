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
from typing import Any, Dict, List, Optional

from ..env_facts import compute_static_env_lines

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


class Qwen35PromptBuilder:
    """ChatML 组装器。构造后只读；同一实例可被多次生成共享。"""

    def __init__(self, processor, env: Optional[Dict[str, Any]] = None):
        # markers 是家族字面量事实（Qwen3.5/Ornith tokenizer 实测全部单 token），
        # 不经 special_tokens_map 取名——Qwen 的 map 里没有为这些起键名。
        self.env = env or {}
        self.static_env_lines = compute_static_env_lines(self.env)

    def _extract_text(self, item: Dict[str, Any]) -> str:
        text = item.get("text") or item.get("content") or ""
        if isinstance(text, (dict, list)):
            return json.dumps(text, ensure_ascii=False)
        return str(text)

    def _system_block(self, content: str) -> str:
        return f"{IM_START}system\n{content}{IM_END}\n"

    def _build_tools_declaration(self, tools: List[Dict[str, Any]]) -> str:
        """官方形态：system 块内 <tools> 每行一个 JSON 声明 + 调用格式教学。"""
        if not tools:
            return ""
        lines = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
        return ("# Tools\n\nYou have access to the following functions:\n\n"
                f"<tools>\n{lines}\n</tools>" + _TOOL_FORMAT_GUIDE)

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

        for msg in messages:
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

            prompt += f"{IM_START}{role}\n{body.rstrip()}{IM_END}\n"

            # 工具声明并入稳定前缀：紧跟首条 system（与 gemma4 同布局）
            if role == "system" and tools:
                prompt += self._system_block(self._build_tools_declaration(tools))

        # 可变尾部：任务状态块 → 一次性护栏尾注 → 环境锚点（与 gemma4 同序同语义）
        if task_block:
            prompt += self._system_block(task_block)
        if tail_notes:
            notes = "\n".join(n for n in tail_notes if n and n.strip())
            if notes.strip():
                prompt += self._system_block(notes)

        _now = datetime.now()
        env_lines = [f"Current date: {_now.strftime('%Y-%m-%d')} ({_now.strftime('%A')})"]
        if include_env:
            # 隐私门控写死在 builder（与 gemma4 同条纪律）：env_extra（长期记忆索引）
            # 同为个人事实，沙箱替身即便误传也被这道闸挡下
            if self.static_env_lines:
                env_lines.extend(self.static_env_lines)
            if env_extra:
                env_lines.extend(l for l in env_extra if l and l.strip())
        prompt += self._system_block("\n".join(env_lines))

        # 生成头。关思考时预填空 <think> 块（官方模板 enable_thinking=false 同款）：
        # Qwen3.5/Ornith 默认自发思考，空块预填是本家族的"幽灵思考抑制"，恒开无开关。
        prompt += f"{IM_START}assistant\n"
        if enable_thinking:
            prompt += f"{THINK_OPEN}\n"
        else:
            prompt += f"{THINK_OPEN}\n\n{THINK_CLOSE}\n\n"
        return prompt
