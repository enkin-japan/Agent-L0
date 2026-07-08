import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from transformers.processing_utils import ProcessorMixin

class PromptBuilder:
    """
    负责将对话历史、多模态上下文及 MCP 工具定义转换为大模型底层的 Token 字符串。
    """
    def __init__(self, processor: ProcessorMixin):
        vocab = processor.tokenizer.special_tokens_map if hasattr(processor.tokenizer, "special_tokens_map") else {}
        
        self.bos_token = vocab.get("bos_token", "<bos>")
        self.eos_token = vocab.get("eos_token", "<eos>")
        self.sot_token = vocab.get("sot_token", "<|turn|>")
        self.eot_token = vocab.get("eot_token", "<turn|>")
        
        self.boi_token = vocab.get("boi_token", "<|image>")
        self.eoi_token = vocab.get("eoi_token", "<image|>")
        self.image_token = vocab.get("image_token", "<|image|>")
        
        self.boa_token = vocab.get("boa_token", "<|audio>")
        self.eoa_token = vocab.get("eoa_token", "<audio|>")
        self.audio_token = vocab.get("audio_token", "<|audio|>")
        
        self.std_token = vocab.get("std_token", "<|tool>")
        self.etd_token = vocab.get("etd_token", "<tool|>")
        self.stc_token = vocab.get("stc_token", "<|tool_call>")
        self.etc_token = vocab.get("etc_token", "<tool_call|>")
        self.str_token = vocab.get("str_token", "<|tool_response>")
        self.etr_token = vocab.get("etr_token", "<tool_response|>")

        self.soc_token = vocab.get("soc_token", "<|channel>")
        self.eoc_token = vocab.get("eoc_token", "<channel|>")

    def _extract_text(self, item: Dict[str, Any]) -> str:
        text = item.get("text") or item.get("content") or ""
        if isinstance(text, (dict, list)):
            return json.dumps(text, ensure_ascii=False)
        return str(text)

    def _build_tools_declaration(self, tools: List[Dict[str, Any]]) -> str:
        tools_dec = ""
        if not tools:
            return tools_dec
        for t in tools:
            tools_json = json.dumps(t, ensure_ascii=False)
            tools_dec += f"{self.std_token}{tools_json}{self.etd_token}\n"
        return tools_dec

    def _build_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> str:
        result = ""
        for call in tool_calls:
            # 如果是异常解析对象，则跳过（异常流由 Engine 处理，此处仅组装合法动作）
            if call.get("type") == "error":
                continue
                
            func_data = call.get("function", call)
            if isinstance(func_data.get("arguments"), str):
                # 拷贝后再解析：不原地改写 history 里的消息 dict（副作用会渗回会话记忆）
                func_data = dict(func_data)
                try:
                    func_data["arguments"] = json.loads(func_data["arguments"])
                except json.JSONDecodeError:
                    pass
            call_json = json.dumps(func_data, ensure_ascii=False)
            result += f"{self.stc_token}{call_json}{self.etc_token}\n"
        return result

    def _build_tool_responses(self, tool_responses: List[Dict[str, Any]]) -> str:
        result = ""
        for resp in tool_responses:
            resp_json = json.dumps(resp, ensure_ascii=False)
            result += f"{self.str_token}{resp_json}{self.etr_token}\n"
        return result

    def build(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
              enable_thinking: bool = False, task_block: Optional[str] = None,
              tail_notes: Optional[List[str]] = None) -> str:
        """
        前缀分层布局（KV 缓存友好）：
        [稳定前缀] system 提示词 + 工具声明（启动后不变）
        [追加区]   对话历史（只增不改）
        [可变尾部] task_block + tail_notes —— 当前任务状态块与一次性护栏提醒，每轮可能变化，
                   放在最尾部，其变动只打掉自身与生成段的缓存，前面的稳定段与历史照常命中。
        tail_notes（T3-c）：一次性瞬态提醒（如截断续写、提前收尾推回），只此一轮生效、不进历史，
                   避免累积、也不被摘要压缩污染滚动摘要。
        """
        prompt = self.bos_token
        
        for msg in messages:
            role = msg.get("role", "user")
            prompt += f"{self.sot_token}{role}\n"
            
            thoughts = msg.get("thoughts", "")
            if thoughts:
                prompt += f"{self.soc_token}thought\n{thoughts}\n{self.eoc_token}\n"

            content = msg.get("content", "")
            if isinstance(content, str) and content:
                prompt += content + "\n"
            elif isinstance(content, list):
                for item in content:
                    item_type = item.get("type", "")
                    if item_type in ["image", "image_url"]:
                        prompt += f"{self.boi_token}{self.image_token}{self.eoi_token}"
                    elif item_type in ["audio", "audio_url"]:
                        prompt += f"{self.boa_token}{self.audio_token}{self.eoa_token}"
                    elif item_type == "text":
                        prompt += self._extract_text(item) + "\n"

            if "tool_calls" in msg and msg["tool_calls"]:
                prompt += self._build_tool_calls(msg["tool_calls"])

            if role == "tool" or "tool_responses" in msg:
                if "tool_responses" in msg:
                    prompt += self._build_tool_responses(msg["tool_responses"])
                elif isinstance(content, str):
                    prompt += self._build_tool_responses([{"name": msg.get("name", "unknown_tool"), "content": content}])
            
            if role == "system" and tools:
                prompt += f"{self.sot_token}system\n"
                prompt += self._build_tools_declaration(tools)
                prompt += f"{self.eot_token}\n"

            prompt = prompt.rstrip() + f"{self.eot_token}\n"

        # 可变尾部：任务状态块（多步任务进行中才存在）
        if task_block:
            prompt += f"{self.sot_token}system\n{task_block}\n{self.eot_token}\n"

        # 可变尾部：一次性护栏提醒（T3-c）——只此一轮注入、用完即弃，不进历史、不被摘要压缩
        if tail_notes:
            notes = "\n".join(n for n in tail_notes if n and n.strip())
            if notes.strip():
                prompt += f"{self.sot_token}system\n{notes}\n{self.eot_token}\n"

        # 被动注入当前日期（仅到"天"，放最尾部对 KV 缓存友好——同一天多轮不变、跨天只失效尾部一小段）：
        # 给模型一个"今天"的时间锚点，使其能把"明天/最近/今年"等相对时间落到具体日期。
        # 需精确到时分秒/时区时，模型另有 get_current_time 工具可主动调用（mcp/tools/datetime_now.py）。
        _now = datetime.now()
        _date_line = f"Current date: {_now.strftime('%Y-%m-%d')} ({_now.strftime('%A')})"
        prompt += f"{self.sot_token}system\n{_date_line}\n{self.eot_token}\n"

        prompt += f"{self.sot_token}assistant\n"
        if enable_thinking:
            prompt += f"{self.soc_token}thought\n"

        return prompt