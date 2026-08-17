import json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from transformers.processing_utils import ProcessorMixin

# 环境事实行已收编 dialects/env_facts.py（qwen35 方言波）：内容方言无关，各方言共用；
# 本 builder 的 _compute_static_env_lines 委托它，输出逐字节不变（test_prompt_env 守着）。
from ..env_facts import compute_static_env_lines
from ...core.dialect_protocol import TASK_BLOCK_RETRACT_NOTE
# 工具协议的官方紧凑 DSL 编码器（§7-8）。三类块（声明/调用/观测）的字节形态全部
# 出自 dsl.py，与约束采样语法（tool_grammar.py）共用同一份实现——两处各写一遍
# 必然漂移，而漂移就是 §S.17。
from .dsl import format_function_declaration, format_tool_call, format_tool_response


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
        # 兜底值必须是 <|turn>（真值，见 tokenizer 的 special_tokens_map）——旧写法
        # 兜底成 <|turn|> 是错的，且金测试用空 special_tokens_map 正测过这个错兜底。
        self.sot_token = vocab.get("sot_token", "<|turn>")
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
        """官方紧凑 DSL 的工具声明串（§7-8 迁移，2026-08-14）。

        旧形态是我方自建 JSON（`<|tool>{"name":…}<tool|>`），模型从没在训练里见过——
        实测后果：12B 递 JSON 时进 `<|channel>thought` 无限循环、e4b 幻觉「我已经获取
        了时间」而根本不发调用；同模型同问题只把声明换成官方 DSL 即干净调用（§S.17）。
        工具间无分隔符、末尾无换行——官方是 <|tool>…<tool|><|tool>…<tool|> 紧邻。
        """
        if not tools:
            return ""
        return "".join(f"{self.std_token}{format_function_declaration(t)}{self.etd_token}"
                       for t in tools)

    def _build_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> str:
        """回放模型自己发过的调用：`<|tool_call>call:名字{k:v}<tool_call|>`。

        块末**不带换行**、多调用紧邻（官方模板如此）。本方法与约束采样的 Lark 语法
        （tool_grammar.build_tool_call_grammar）必须锚定同一形状：回放格式与约束形状
        一旦漂移，模型会被约束成自己历史里没见过的东西。
        """
        result = ""
        for call in tool_calls:
            # 如果是异常解析对象，则跳过（异常流由 Engine 处理，此处仅组装合法动作）
            if call.get("type") == "error":
                continue

            func_data = call.get("function", call)
            arguments = func_data.get("arguments")
            if isinstance(arguments, str):
                # 解析器产出的实参恒为 dict，但历史可能来自 OpenAI 形态（JSON 字符串）。
                # 解析成功走 DSL 编码；失败则原样放行（官方对字符串实参亦原样吐出）。
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            body = format_tool_call(func_data.get("name", ""), arguments)
            result += f"{self.stc_token}{body}{self.etc_token}"
        return result

    #: 工具观察里**唯一**允许进模型上下文的键。tool_responses 条目同时是「模型
    #: 上下文」与「账本/前端渲染源」两用面，新挂的元数据（压缩层的 _safe/_target、
    #: R3 的 render）默认都不该给模型看。黑名单会漏——此前正是整个 dict 序列化，
    #: `"_safe": true` 一路进了提示词（`_` 前缀只在落盘与 public_turn 处剥，提示词
    #: 构建处从来没剥过）。
    #: §7-8 起这道白名单**由结构承担**而非按键过滤：_build_tool_responses 只取
    #: name 与观测体本身（response 优先于 content），别的键根本没有出口。常量保留
    #: 作为「模型可见面」的声明式记录，qwen35 方言仍按键过滤用它。
    _MODEL_VISIBLE_KEYS = ("name", "response", "content")

    def _build_tool_responses(self, tool_responses: List[Dict[str, Any]]) -> str:
        """工具观测：`<|tool_response>response:名字{k:v}<tool_response|>`（官方 DSL）。

        观测体是 mapping 时键裸写展开，其余一律裹成 `{value:…}`（官方
        format_tool_response_block 语义）。块末**不带换行**。
        白名单仍然生效且更紧：只取 name 与观测体本身，元数据一律进不来。
        """
        result = ""
        for resp in tool_responses:
            name = resp.get("name", "unknown_tool")
            # response 优先于 content（两者是同一位置的两种历史写法），都缺则空串。
            body = resp["response"] if "response" in resp else resp.get("content", "")
            result += f"{self.str_token}{format_tool_response(name, body)}{self.etr_token}"
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
        tools_emitted = False   # 工具声明只发一次（官方：进第一个 system 轮）
        turn_open = False       # 上一轮是否故意没收口（等着工具观测/续写折进来）
        pending_content = ""    # 本轮正文（官方轮内次序：正文排在工具块之后）
        body_start = None       # 当前轮体起点（收口 rstrip 的下界）

        n_msgs = len(messages)
        for i, msg in enumerate(messages):
            role = msg.get("role", "user")
            # 官方模板：`role = 'model' if message['role'] == 'assistant' else role`
            # ——轮头写的是 model 不是 assistant。实测（2026-08-14，26B-4bit ×
            # 0/4k/16k/32k）两种写法输出均正确，即本项与 §S.16 不同、无实害；
            # 仍按字节协议纪律对齐官方（"我测的档位没事"正是当初对多余 \n 的误判）。
            # 注意：只改**轮头字面量**，下面的分支判定仍用原始 role——写成覆盖
            # role 会让 `role == "assistant"` 的重放抑制分支静默失效（本文件的
            # 字节回归测试当场抓到过一次）。
            # 工具观测折进 model 轮（§7-8）：官方模板整个循环体裹在 `role != 'tool'`
            # 之下——它**从不开 <|turn>tool 轮**，工具结果是紧跟 <|tool_call> 之后、
            # 同一个 model 轮内的 <|tool_response> 块。旧写法给每条工具结果单开一个
            # <|turn>tool 轮头，那是模型训练里不存在的形状。故 tool 消息的轮头字面量
            # 也是 model（无上一轮可折时自己开一个，见下方 is_model_turn 不变量）。
            turn_role = "model" if role in ("assistant", "tool") else role
            # **不变量：开放轮恒为 model 轮**。turn_open 只在 model 轮上置位，fold 也
            # 只在 turn_open 时发生，两者必须同口径——否则会出现「留了个 user 轮不
            # 收口、下一条却不折」的双轮头无收口畸形，以及 user 轮吞掉观测块、用户
            # 正文被挪到观测之后（2026-08-14 首过评审揪出，两例均已实证复现）。
            is_model_turn = (turn_role == "model")
            # 有开放轮就折进去。turn_open 只在 model 轮上置位、且只当下一条是 tool 或
            # assistant 时置位（见下方收口判定），故此处 turn_open 恒蕴含「本条是
            # tool/assistant，且上一轮是等着它的 model 轮」——续轮无需再看别的判据。
            # （曾按官方 continue_same_model_turn 加了 prev_non_tool_role=="assistant"
            #  判据，在 history 以工具结果开头时判错：观测轮不收口、assistant 又不折，
            #  吐出两个轮头中间没有收口符的畸形。判据与不变量重复且更弱，删掉。）
            fold_into_prev = turn_open and is_model_turn
            if not fold_into_prev:
                prompt += f"{self.sot_token}{turn_role}\n"
            # 轮体起点：收口时的 rstrip 只作用于此位置之后，绝不吃掉轮头后的 \n
            # （官方在正文为空时也保留它；旧的整串 rstrip 会连轮头一起削）。
            body_start = len(prompt)

            thoughts = msg.get("thoughts", "")
            if thoughts:
                prompt += f"{self.soc_token}thought\n{thoughts}\n{self.eoc_token}"
            elif role == "assistant" and self.suppress_ghost_thinking and not fold_into_prev:
                # 历史重放与生成头预填保持字节一致：该轮生成时 assistant 头部预填过
                # 空思考频道（见 build 尾部），重放时不吐同样的空频道会让 KV 前缀
                # 在此断裂，之后整段重预填。
                # **续轮的 assistant 不预填**（not fold_into_prev）：官方的思考频道只在
                # 轮首出现，续写段里凭空插一个空频道是它没有的形状；而"保持 KV 对齐"
                # 的理由在此也不成立——折轮时这段字节本就与生成时不同（生成时它前面
                # 是环境轮+新轮头，重放时紧跟 <tool_response|>）。
                prompt += f"{self.soc_token}thought\n{self.eoc_token}"

            content = msg.get("content", "")
            # 正文**后置**（§7-8）：官方轮内次序是 思考 → tool_calls → tool_responses
            # → 正文，我方原来把正文放在最前。正文暂存于此、在收口（或本轮工具声明）
            # 前才落盘——工具观测折进本轮时，正文同样要排在观测之后才与官方一致。
            # role=="tool" 的正文属于 <|tool_response> 块（见下），此处不能再吐一遍
            # ——否则每条工具结果在上下文里出现两次（2026-08-14 查出，白烧 token
            # 且让模型看到重复观测）。
            if isinstance(content, str) and content and role != "tool":
                pending_content += content + "\n"
            elif isinstance(content, list) and role != "tool":
                # role=="tool" 的 list 正文同样归 <|tool_response> 块（见下）。少了这道
                # 门禁，工具观测的文本会当作模型自己的散文漏进轮体（首过评审揪出）。
                for item in content:
                    item_type = item.get("type", "")
                    if item_type in ["image", "image_url"]:
                        pending_content += f"{self.boi_token}{self.image_token}{self.eoi_token}"
                    elif item_type in ["audio", "audio_url"]:
                        pending_content += f"{self.boa_token}{self.audio_token}{self.eoa_token}"
                    elif item_type == "text":
                        pending_content += self._extract_text(item) + "\n"

            if "tool_calls" in msg and msg["tool_calls"]:
                prompt += self._build_tool_calls(msg["tool_calls"])

            emitted_responses = False
            if role == "tool" or "tool_responses" in msg:
                if "tool_responses" in msg:
                    prompt += self._build_tool_responses(msg["tool_responses"])
                    emitted_responses = True
                else:
                    # 回退路径：role=tool 但没有 tool_responses 字段，观测体在 content 里。
                    # list 形态（多模态工具结果）按官方语义把 text 段拼起来 —— 不能只认
                    # str，否则整条观测既不进观测块、又被上面的门禁挡在轮体外 = 静默丢失。
                    body = content
                    if isinstance(content, list):
                        body = "".join(self._extract_text(it) for it in content
                                       if it.get("type") == "text")
                    prompt += self._build_tool_responses(
                        [{"name": msg.get("name", "unknown_tool"), "content": body}])
                    emitted_responses = True
                    if isinstance(content, list):
                        # 媒体标记仍按官方跟在观测块之后
                        for item in content:
                            item_type = item.get("type", "")
                            if item_type in ("image", "image_url"):
                                prompt += self.image_token
                            elif item_type in ("audio", "audio_url"):
                                prompt += self.audio_token

            if role == "system" and i == 0 and tools and not tools_emitted:
                # 官方模板（2026-08-14 逐字节对表 + 边界渲染实证）：
                #  · 声明紧跟 system 轮正文，不另开轮头、正文与 <|tool> 间无换行、
                #    多工具紧邻无分隔，整轮由下面统一的 <turn|> 收口；
                #  · 只在 messages[0] 就是 system 时并进它，否则另开一个**排在最前**的
                #    system 轮只放声明（下面的 tools_emitted 兜底）——官方的条件是
                #    `tools or messages[0].role in [system, developer]`，即有工具就必有
                #    一个领头的 system 轮。写成"第一个 system 轮"会在 messages[0] 不是
                #    system 时（如 [user, system, …]）把声明塞进对话中段（自审揪出）。
                #    多 system 消息时后续轮一律不带——会话带滚动摘要时那是第二条
                #    system，旧写法会把整份声明发两遍；
                #  · 正文按 `| trim` 掐两端空白，但**轮头后的 \n 保留**（正文为空时
                #    官方仍是 <|turn>system\n<|tool>…）。
                # 声明**体**已于 §7-8（2026-08-14）迁到官方 declaration:名字{…} DSL。
                prompt += pending_content.strip()
                pending_content = ""
                prompt += self._build_tools_declaration(tools)
                tools_emitted = True

            next_role = messages[i + 1].get("role", "user") if i + 1 < n_msgs else None

            # 不收口的两种情形（官方轮内形态）——两者都**必须发生在 model 轮上**
            # （见上方 is_model_turn 不变量）：
            #  · 下一条是工具结果 —— 留给它把观测块折进本轮；
            #  · 本条刚吐完观测且无正文，下一条是 assistant —— 它续写同一轮的回答。
            # 官方在第二种情形下**无条件**不收口（下一条是 user 也不收），那会留下
            # 一个永不闭合的 model 轮；此处加 next_role 判据收口，是**有意偏离**：
            # 中途取消/工具结果后直接换人说话在本栈是真实路径（turn_runner 的
            # cancelled 观测），而未闭合轮头接 <|turn>user 是模型训练里不存在的形状。
            if is_model_turn and (next_role == "tool"
                                  or (emitted_responses
                                      and not pending_content.strip()
                                      and next_role == "assistant")):
                turn_open = True
                continue

            prompt += pending_content
            pending_content = ""
            # 只 rstrip 轮体，轮头后的 \n 不动（官方在空正文轮里也保留它）
            prompt = prompt[:body_start] + prompt[body_start:].rstrip() + f"{self.eot_token}\n"
            body_start = None
            turn_open = False

        if turn_open:
            # 防御性收口。当前控制流走不到这里（turn_open 只在还有后继消息时置位，
            # 而最后一条消息必定收口），保留是因为「开放轮溢出到可变尾部」是本文件
            # 最贵的一类错误：可变尾部（task_block/tail_notes/环境锚点）与生成头必须
            # 排在闭合轮之后，未闭合轮头接 <|turn>system 是模型没见过的形状。
            # rstrip 同样按 body_start 设界——整串 rstrip 会削掉轮头后的 \n（见上）。
            prompt += pending_content
            pending_content = ""
            cut = body_start if body_start is not None else 0
            prompt = prompt[:cut] + prompt[cut:].rstrip() + f"{self.eot_token}\n"
            turn_open = False

        if tools and not tools_emitted:
            # 一条 system 消息都没有时，官方仍单开一个 system 轮只放声明，且排在
            # 对话最前（实测渲染）。旧写法此时把声明整个丢掉——模型看不见工具，
            # 表现为「装作调用过了」的幻觉，极难归因。
            decl = (f"{self.sot_token}system\n"
                    f"{self._build_tools_declaration(tools)}{self.eot_token}\n")
            prompt = self.bos_token + decl + prompt[len(self.bos_token):]

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
        prompt += (f"{self.sot_token}system\n"
                   + self._env_block_content(include_env, env_extra)
                   + f"\n{self.eot_token}\n")

        prompt += self._generation_head(enable_thinking)

        return prompt

    def _env_block_content(self, include_env: bool,
                           env_extra: Optional[List[str]] = None) -> str:
        """环境锚点块内容（全量渲染尾部与增量段共用同一字节权威——上位解）。"""
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
        return "\n".join(env_lines)

    def _generation_head(self, enable_thinking: bool) -> str:
        """生成头（全量渲染与增量段共用）。官方模板生成头亦为 model（见 build 内角色映射注）。"""
        head = f"{self.sot_token}model\n"
        if enable_thinking:
            head += f"{self.soc_token}thought\n"
        elif self.suppress_ghost_thinking:
            # 关思考时预填「空思考频道」（官方 12B/26B/31B 模板的 ghost-thinking 抑制）：
            # 模型看到已开而即闭的 thought 频道，不再自发思考，直接产出正文/工具调用。
            # **尾部不带换行**——官方模板的字符串字面量就在 '<channel|>' 处结束
            # （`{{- … -}}` 削的是模板源码里标签两侧的空白，与输出内容无关，别混淆）。
            # 多一个 \n 的后果实测（2026-08-14）：26B-4bit ≥4k
            # 上下文首 token 即 EOT（原「发现 A」的真因，曾被误判为 PTQ 量化损伤）、
            # 12B 输出退化成 '<|channel>::' 循环。字节级对齐官方模板，一个空白都不许多。
            head += f"{self.soc_token}thought\n{self.eoc_token}"
        return head

    def tail_state(self, task_block: Optional[str] = None,
                   include_env: bool = False,
                   env_extra: Optional[List[str]] = None) -> Dict[str, str]:
        """全量渲染路径落进流的尾块快照（上位解「变了才追加」的比对基准）。
        与 build 尾部各调一次 datetime.now()——跨午夜微秒的记录/渲染错位至多
        造成一次多余追加或一天内的旧日期滞留，次日自愈，不设共享时钟。"""
        return {"env": self._env_block_content(include_env, env_extra),
                "task": task_block or "", "task_appends": 0}

    #: 任务块冻结快照的收拢阈值（评审 C2）：增量路连续追加这么多份任务块后，
    #: 下一次变更拒渲染、退全量重渲染收拢。一步任务的 Budget 行逐迭代变化，
    #: 阈值权衡「快照堆积的上下文税」与「收拢时不可回滚缓存的整段冷预填」。
    TASK_APPENDS_COLLAPSE = 6

    def build_prompt_delta(self, new_messages: List[Dict[str, Any]], *,
                           enable_thinking: bool = False,
                           task_block: Optional[str] = None,
                           tail_notes: Optional[List[str]] = None,
                           include_env: bool = False,
                           env_extra: Optional[List[str]] = None,
                           prev_tail: Optional[Dict[str, str]] = None
                           ) -> Optional[Tuple[str, Dict[str, str]]]:
        """增量段（上位解）：接在「已记录字节流」（以一次生成的原始字节收尾、
        model 轮开放、停止符不在流内）之后的追加字节。

        形态决策（08-17）：**恒以 `<turn|>\\n` 收口开头**，观测/新消息各归各轮，
        不做官方的「观测折进上一 model 轮」内联形态——模型停止时采样出的 <turn|>
        可能已喂入缓存（流水线预喂）也可能没有，收口符打头让两种情形的 token
        流都与缓存记录（截到真实已喂长度，见 gemma4_generate._fed_length）纯追加
        对齐；而观测独立成 model 轮是全量渲染对「history 以工具结果开头」本就
        产出的形状（自开 model 轮），模型见过。尾块按 prev_tail 变了才追加——
        同日多轮 env 块零重复，跨天才追加新块（旧日期戳留在当轮位置，语义上
        就是「那轮发生的日期」）。

        返回 None = 本方言增量不支持的消息形态（assistant 重放/未知角色/system
        带工具字段），调用方退全量重渲染。
        """
        prev_tail = prev_tail or {}
        delta = f"{self.eot_token}\n"
        obs_open = False   # 观测承载 model 轮是否开放（连续 tool 消息共用一轮）

        def _close_obs() -> str:
            nonlocal obs_open
            if obs_open:
                obs_open = False
                return f"{self.eot_token}\n"
            return ""

        for msg in new_messages:
            role = msg.get("role", "user")
            if role == "assistant":
                # assistant 重放（审批合并/截断改写等改写路径）：增量不支持——
                # 回放字节恒等正是全量路的 §S.17 难题，增量绝不重蹈
                return None
            if msg.get("thoughts") or (role != "tool" and (
                    "tool_responses" in msg or msg.get("tool_calls"))):
                # 全量渲染对这些形态另有字节（非 assistant 轮的 thoughts、user 轮
                # 内嵌观测块等），现网生产者不产出——宁可退全量也不静默丢字段
                return None
            if role == "tool":
                if not obs_open:
                    delta += f"{self.sot_token}model\n"
                    obs_open = True
                content = msg.get("content", "")
                if "tool_responses" in msg:
                    delta += self._build_tool_responses(msg["tool_responses"])
                else:
                    body = content
                    if isinstance(content, list):
                        body = "".join(self._extract_text(it) for it in content
                                       if it.get("type") == "text")
                    delta += self._build_tool_responses(
                        [{"name": msg.get("name", "unknown_tool"), "content": body}])
                    if isinstance(content, list):
                        for item in content:
                            item_type = item.get("type", "")
                            if item_type in ("image", "image_url"):
                                delta += self.image_token
                            elif item_type in ("audio", "audio_url"):
                                delta += self.audio_token
            elif role == "user":
                delta += _close_obs()
                body = ""
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    body += content + "\n"
                elif isinstance(content, list):
                    for item in content:
                        item_type = item.get("type", "")
                        if item_type in ["image", "image_url"]:
                            body += f"{self.boi_token}{self.image_token}{self.eoi_token}"
                        elif item_type in ["audio", "audio_url"]:
                            body += f"{self.boa_token}{self.audio_token}{self.eoa_token}"
                        elif item_type == "text":
                            body += self._extract_text(item) + "\n"
                delta += f"{self.sot_token}user\n{body.rstrip()}{self.eot_token}\n"
            elif role == "system":
                # 编排器的中段护栏消息（no_plan_nudge / 步骤失败菜单）——纯文本形态。
                # rstrip 不 strip：与全量渲染的收口语义逐字节一致（build 只 rstrip
                # 轮体、保留正文前导空白——评审 M2，两侧分叉会打碎失效后的前缀命中）
                content = msg.get("content", "")
                if not isinstance(content, str):
                    return None
                delta += _close_obs()
                delta += f"{self.sot_token}system\n{content.rstrip()}{self.eot_token}\n"
            else:
                return None   # 未知角色：增量不支持
        delta += _close_obs()

        env_content = self._env_block_content(include_env, env_extra)
        prev_task = prev_tail.get("task", "")
        task_appends = int(prev_tail.get("task_appends", 0) or 0)
        if task_block:
            if task_block != prev_task:
                # 任务块逐迭代变化（Budget 计数器行），冻结快照会无限堆积（评审
                # C2）：追加计数达阈即拒渲染 → 引擎退全量重渲染，历史快照收拢为
                # 尾部一块并重新记录。阈值内的快照按时序读是任务进度的转录，最新
                # 一块（Current task state + 就近性）即现状。
                if task_appends >= self.TASK_APPENDS_COLLAPSE:
                    return None
                delta += f"{self.sot_token}system\n{task_block}\n{self.eot_token}\n"
                task_appends += 1
        elif prev_task:
            delta += f"{self.sot_token}system\n{TASK_BLOCK_RETRACT_NOTE}\n{self.eot_token}\n"
            task_appends += 1
        new_tail = {"env": env_content, "task": task_block or "",
                    "task_appends": task_appends}
        if tail_notes:
            notes = "\n".join(n for n in tail_notes if n and n.strip())
            if notes.strip():
                delta += f"{self.sot_token}system\n{notes}\n{self.eot_token}\n"
        if env_content != prev_tail.get("env"):
            delta += f"{self.sot_token}system\n{env_content}\n{self.eot_token}\n"

        delta += self._generation_head(enable_thinking)
        return delta, new_tail
