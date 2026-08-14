import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from transformers.processing_utils import ProcessorMixin

# 环境事实行已收编 dialects/env_facts.py（qwen35 方言波）：内容方言无关，各方言共用；
# 本 builder 的 _compute_static_env_lines 委托它，输出逐字节不变（test_prompt_env 守着）。
from ..env_facts import compute_static_env_lines


class PromptBuilder:
    """
    负责将对话历史、多模态上下文及 MCP 工具定义转换为大模型底层的 Token 字符串。
    """
    def __init__(self, processor: ProcessorMixin, suppress_ghost_thinking: bool = False,
                 env: Optional[Dict[str, Any]] = None):
        # 幽灵思考抑制（官方 thinking 文档）：Gemma-4 12B/26B/31B 关思考时须在 model 轮
        # 开头预填空思考频道，否则模型会自发打开 thought 频道（幽灵思考）烧 token。
        # e4b 官方模板无此要求，保持 False。开关来自 config.yaml model_registry 条目。
        # 预填形态必须与官方模板逐字节一致（尾部无换行，见 build 尾部注释）——
        # 12B 亦在此列：曾因我方多一个 \n 被误判为「无视抑制」（2026-08-14 更正）。
        self.suppress_ghost_thinking = suppress_ghost_thinking
        # 环境上下文（想法1）：稳定的环境事实，启动时算一次。机器事实自动探测；个人事实来自
        # config.yaml agent.environment（默认全空、opt-in）。只在主编排轮注入——见 build。
        self.env = env or {}
        self.static_env_lines = self._compute_static_env_lines()
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

    def _compute_static_env_lines(self) -> List[str]:
        """委托 dialects/env_facts.py（内容方言无关，各方言共用；输出逐字节不变）。"""
        return compute_static_env_lines(self.env)

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

    #: 工具观察里**唯一**允许进模型上下文的键。白名单而非黑名单：tool_responses 条目
    #: 同时是「模型上下文」与「账本/前端渲染源」两用面，新挂的元数据（压缩层的
    #: _safe/_target、R3 的 render）默认都不该给模型看。黑名单会漏——此前正是整个 dict
    #: 序列化，`"_safe": true` 一路进了提示词（`_` 前缀只在落盘与 public_turn 处剥，
    #: 提示词构建处从来没剥过）。
    _MODEL_VISIBLE_KEYS = ("name", "response", "content")

    def _build_tool_responses(self, tool_responses: List[Dict[str, Any]]) -> str:
        result = ""
        for resp in tool_responses:
            visible = {k: resp[k] for k in self._MODEL_VISIBLE_KEYS if k in resp}
            resp_json = json.dumps(visible, ensure_ascii=False)
            result += f"{self.str_token}{resp_json}{self.etr_token}\n"
        return result

    def build(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
              enable_thinking: bool = False, task_block: Optional[str] = None,
              tail_notes: Optional[List[str]] = None, include_env: bool = False,
              env_extra: Optional[List[str]] = None) -> str:
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
            # 官方模板：`role = 'model' if message['role'] == 'assistant' else role`
            # ——轮头写的是 model 不是 assistant。实测（2026-08-14，26B-4bit ×
            # 0/4k/16k/32k）两种写法输出均正确，即本项与 §S.16 不同、无实害；
            # 仍按字节协议纪律对齐官方（"我测的档位没事"正是当初对多余 \n 的误判）。
            # 注意：只改**轮头字面量**，下面的分支判定仍用原始 role——写成覆盖
            # role 会让 `role == "assistant"` 的重放抑制分支静默失效（本文件的
            # 字节回归测试当场抓到过一次）。
            turn_role = "model" if role == "assistant" else role
            prompt += f"{self.sot_token}{turn_role}\n"
            
            thoughts = msg.get("thoughts", "")
            if thoughts:
                prompt += f"{self.soc_token}thought\n{thoughts}\n{self.eoc_token}"
            elif role == "assistant" and self.suppress_ghost_thinking:
                # 历史重放与生成头预填保持字节一致：该轮生成时 assistant 头部预填过
                # 空思考频道（见 build 尾部），重放时不吐同样的空频道会让 KV 前缀
                # 在此断裂，之后整段重预填。
                prompt += f"{self.soc_token}thought\n{self.eoc_token}"

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

        # 被动注入环境锚点块（放最尾部对 KV 缓存友好——静态事实与当天日期同处一块，跨天/无关
        # 变动只失效尾部一小段）。日期永远注入（相对时间锚点；需精确时分秒模型另有 get_current_time
        # 工具，mcp/tools/datetime_now.py）。机器/个人事实仅在主编排轮注入（include_env=True）——
        # 沙箱子生成复用同一 builder 但默认 include_env=False，个人信息绝不入沙箱上下文（隐私边界）。
        _now = datetime.now()
        env_lines = [f"Current date: {_now.strftime('%Y-%m-%d')} ({_now.strftime('%A')})"]
        if include_env:
            # 门控写在这里而不是靠调用方自觉：env_extra（R4 的长期记忆索引）同样是个人
            # 事实，沙箱即便误传了也必须被这道闸门挡下（R1-1 的教训：不变量要写死，
            # 不能靠"调用方不会那么干"）。
            if self.static_env_lines:
                env_lines.extend(self.static_env_lines)
            if env_extra:
                env_lines.extend(l for l in env_extra if l and l.strip())
        prompt += f"{self.sot_token}system\n" + "\n".join(env_lines) + f"\n{self.eot_token}\n"

        prompt += f"{self.sot_token}model\n"   # 官方模板生成头亦为 model（见上方角色映射注）
        if enable_thinking:
            prompt += f"{self.soc_token}thought\n"
        elif self.suppress_ghost_thinking:
            # 关思考时预填「空思考频道」（官方 12B/26B/31B 模板的 ghost-thinking 抑制）：
            # 模型看到已开而即闭的 thought 频道，不再自发思考，直接产出正文/工具调用。
            # **尾部不带换行**——官方模板的字符串字面量就在 '<channel|>' 处结束
            # （`{{- … -}}` 削的是模板源码里标签两侧的空白，与输出内容无关，别混淆）。
            # 多一个 \n 的后果实测（2026-08-14）：26B-4bit ≥4k
            # 上下文首 token 即 EOT（原「发现 A」的真因，曾被误判为 PTQ 量化损伤）、
            # 12B 输出退化成 '<|channel>::' 循环。字节级对齐官方模板，一个空白都不许多。
            prompt += f"{self.soc_token}thought\n{self.eoc_token}"

        return prompt