import re
import json
from typing import Dict, Any

# error 调用的 raw_input 上限：解析失败的原文会被原样回显给模型（纠错反馈）并进入
# 历史/账本——失控超长的半截 JSON（可达上万 token）必须截断，防止反馈自体膨胀。
_ERROR_RAW_MAX = 1500


def _cap(raw: str) -> str:
    raw = raw.strip()
    return raw if len(raw) <= _ERROR_RAW_MAX else raw[:_ERROR_RAW_MAX] + " …[truncated]"


class ResponseParser:
    """
    负责将大模型的流式或完整输出文本，反序列化为结构化的推理和动作信号。
    """
    def __init__(self, processor):
        vocab = processor.tokenizer.special_tokens_map if hasattr(processor.tokenizer, "special_tokens_map") else {}
        self.soc = vocab.get("soc_token", "<|channel>")
        self.eoc = vocab.get("eoc_token", "<channel|>")
        self.stc = vocab.get("stc_token", "<|tool_call>")
        self.etc = vocab.get("etc_token", "<tool_call|>")

    def parse(self, raw_response: str) -> Dict[str, Any]:
        result = {"thoughts": [], "text": "", "tool_calls": []}

        # 1. 提取 Channel 内容 (如 thought)
        channel_pattern = rf"{re.escape(self.soc)}(.*?){re.escape(self.eoc)}"
        for match in re.finditer(channel_pattern, raw_response, re.DOTALL):
            raw_content = match.group(1).strip()
            parts = re.split(r'\s+', raw_content, maxsplit=1)
            
            channel_name = parts[0]
            channel_text = parts[1] if len(parts) == 2 else ""
                
            if channel_name == "thought":
                result["thoughts"].append(channel_text)
            elif channel_name == "text":
                result["text"] += channel_text + "\n"
        
        if result["thoughts"]:
            result["thoughts"] = "\n".join(result["thoughts"])

        current_response = re.sub(channel_pattern, "", raw_response, flags=re.DOTALL)

        # 2. 提取 Tool Calls
        tool_pattern = rf"{re.escape(self.stc)}(.*?){re.escape(self.etc)}"
        for match in re.finditer(tool_pattern, current_response, re.DOTALL):
            raw_tool_content = match.group(1).strip()
            call_match = re.search(r'call\s*:\s*"?([a-zA-Z0-9_-]+)"?\s*(\{.*\})', raw_tool_content, re.DOTALL)
            
            if call_match:
                func_name = call_match.group(1)
                raw_args_str = call_match.group(2)
                
                # 容错清洗。<|"|> 是模型方言里的"原文字符串块"定界符：成对出现时把
                # 块内容整体 JSON 转义（内部引号/换行/反斜杠全保真——run_python 传多行
                # 含引号代码的唯一可靠通道）；奇数个（截断/漏写）退回旧的直替换。
                fixed_args = raw_args_str
                if '<|"|>' in fixed_args and fixed_args.count('<|"|>') % 2 == 0:
                    fixed_args = re.sub(r'<\|"\|>(.*?)<\|"\|>',
                                        lambda m: json.dumps(m.group(1), ensure_ascii=False),
                                        fixed_args, flags=re.DOTALL)
                else:
                    fixed_args = fixed_args.replace('<|"|>', '"')
                fixed_args = re.sub(r'([{,]\s*)([a-zA-Z0-9_]+)(\s*:)', r'\1"\2"\3', fixed_args)

                try:
                    # strict=False：允许字符串值里出现字面控制字符（未转义的换行/制表）。
                    # 小模型给 run_python 传多行代码时几乎必然直接换行而非 \n 转义，
                    # 严格模式一律 Invalid control character 拒收，白白烧掉纠错闭环。
                    args_dict = json.loads(fixed_args, strict=False)
                    result["tool_calls"].append({
                        "type": "function",
                        "name": func_name,
                        "arguments": args_dict
                    })
                except json.JSONDecodeError as e:
                    # 将格式错误作为一个独立的数据结构返回，由 Engine 统一拦截
                    result["tool_calls"].append({
                        "type": "error",
                        "raw_input": _cap(raw_tool_content),
                        "error_msg": str(e)
                    })
            else:
                try:
                    tool_data = json.loads(raw_tool_content, strict=False)  # 同上：容忍字面换行
                    # 类型校验：合法调用必须是含非空字符串 name 的 JSON 对象，arguments（若有）必须
                    # 是对象。裸字符串/数组/缺 name 的 JSON 一律归入 error 走上层纠错闭环——
                    # 否则非 dict 项会让下游 c.get(...) 直接 AttributeError 炸掉整轮。
                    if (isinstance(tool_data, dict)
                            and isinstance(tool_data.get("name"), str) and tool_data.get("name").strip()
                            and isinstance(tool_data.get("arguments", {}), dict)):
                        # 必须补 type="function"：约束采样产出的纯 JSON 调用不含 type 字段，
                        # 缺了它前端会把成功调用渲染成"格式错误"、导出时整条丢弃
                        result["tool_calls"].append({**tool_data, "type": "function"})
                    else:
                        result["tool_calls"].append({
                            "type": "error",
                            "raw_input": _cap(raw_tool_content),
                            "error_msg": ("A tool call must be a JSON object with a string 'name' "
                                          "and an 'arguments' object."),
                        })
                except json.JSONDecodeError:
                    result["tool_calls"].append({
                        "type": "error",
                        "raw_input": _cap(raw_tool_content),
                        "error_msg": "Severely malformed: neither native tool-call format nor valid JSON."
                    })

        current_response = re.sub(tool_pattern, "", current_response, flags=re.DOTALL)

        # 2.5 未闭合的工具调用块：起始标记后再无收尾标记（生成提前 EOS、截断、或模型忘写
        # 收尾）。实测事故：write_file 调用 fail-open 后忘写 <tool_call|> 直接 EOS，半截
        # JSON 被当成正文、轮次记 success——垃圾静默变最终答复。这里整段转 error 调用，
        # 交给上层既有的解析纠错闭环（反馈正确格式让模型重发），而不是放进正文。
        if self.stc in current_response:
            idx = current_response.index(self.stc)
            dangling = current_response[idx + len(self.stc):].strip()
            result["tool_calls"].append({
                "type": "error",
                "raw_input": _cap(dangling),
                "error_msg": ("Unterminated tool call: the closing marker is missing — your "
                              "output ended before the call was complete. Re-issue it as ONE "
                              "complete, compact JSON call (shorter string values if needed)."),
            })
            current_response = current_response[:idx]

        # 2.6 有尾无头的工具调用残片（§2.5 的镜像）：收尾标记之前没有任何起始标记。
        # 实测事故（挂起恢复的天气查询）：模型在思考频道内草拟工具调用,思考预算超限
        # 强插 "\n<channel|>" 恰落在调用 JSON 中间——频道提取把调用头吞进 thoughts,
        # 剩下 `…", "type": "once"}<tool_call|>` 这样的尾巴漏成正文,垃圾静默变最终答复。
        # 处理:残片(含标记)整段移出正文、转 error 调用走纠错闭环——该调用从未执行,
        # 反馈让模型完整重发（敏感调用重发后照常撞审批闸门,不会静默漏权限）。
        if self.etc in current_response:
            idx = current_response.rindex(self.etc)
            fragment = current_response[:idx].strip()
            result["tool_calls"].append({
                "type": "error",
                "raw_input": _cap(fragment),
                "error_msg": ("Corrupted tool call: only the TAIL of a call was found (its "
                              "beginning was cut off, e.g. by the thinking-budget limit). The "
                              "call was NOT executed. Re-issue it as ONE complete, compact JSON "
                              "call — and do not draft tool calls inside your thinking."),
            })
            current_response = current_response[idx + len(self.etc):]

        # 3. 兜底纯文本
        # 游离的 <channel|>：思考预算强制收尾后，模型仍以为自己在频道内、稍后自行补发
        # 收尾标记——无配对开启标记，channel_pattern 不消费，通用正则 <\|.*?\|> 也匹配
        # 不到它（不以 <| 开头），会原样漏进正文。此处显式清除（与 stream_filter 对齐）。
        # self.etc/stc 同理显式清除（<tool_call|> 不以 <| 开头,通用正则同样漏放）。
        clean_text = re.sub(
            r'<\|.*?\|>', '',
            current_response.replace(self.eoc, "").replace(self.etc, "").replace(self.stc, ""),
        ).strip()
        if clean_text:
            result["text"] = f'{result["text"]}\n{clean_text}'.strip() if result["text"] else clean_text
            
        if result["text"]:
            result["text"] = result["text"].replace("<|think|>", "").strip()

        return result