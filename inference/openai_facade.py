"""OpenAI 入口门面（T6）——/v1/chat/completions 的纯逻辑层。

定位（HETERO_COMPUTE §9.3）：让同一份常驻权重同时服务 Agent 与任何 OpenAI 客户端
工具（lm-eval、编辑器插件等），16GB 机上不必为第三方工具双载模型。门面刻意**不**
暴露自家协议的控制面（缓存句柄、约束采样、思考预算）——那些是 Agent 协议的资产；
这里只有无状态的 messages→text。服务端仍用一个专用缓存句柄吃自动前缀匹配的甜头
（聊天客户端每次全量重发历史，增量预填白拿），但句柄对客户端不可见。

本文件零重依赖（不 import FastAPI/mlx）：消息校验、方言消息整形、两种响应体
（一次性/流式 SSE 行）全是纯函数，金测试按路径装载直接钉行为。
"""
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

_ALLOWED_ROLES = ("system", "user", "assistant")


def validate_messages(messages: Any) -> Optional[str]:
    """返回错误描述（给 400 body）或 None。只收纯文本消息——图像等多模态部件
    走 Agent 自家协议（/v1/generate + /v1/media），门面不重复造多模态路。"""
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array"
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            return f"messages[{i}] must be an object"
        role = m.get("role")
        if role not in _ALLOWED_ROLES:
            return f"messages[{i}].role must be one of {_ALLOWED_ROLES}"
        content = m.get("content")
        if isinstance(content, str):
            continue
        if isinstance(content, list):
            for j, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") != "text":
                    return (f"messages[{i}].content[{j}]: only text parts are "
                            f"supported (use the native /v1/generate API for media)")
            continue
        return f"messages[{i}].content must be a string or an array of text parts"
    return None


def to_dialect_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI 消息 → 方言 builder 消息：content 部件列表压成纯文本串。"""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(str(p.get("text", "")) for p in content)
        out.append({"role": m["role"], "content": content or ""})
    return out


def parse_sampling(body: Dict[str, Any]) -> Dict[str, Any]:
    """采样参数映射（未认识的键静默忽略——OpenAI 客户端会带各种可选项）。"""
    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens") or 1024
    temperature = body.get("temperature")
    return {
        "max_tokens": max(1, int(max_tokens)),
        "temperature": 0.7 if temperature is None else float(temperature),
    }


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def completion_response(model: str, cid: str, text: str,
                        prompt_tokens: int, completion_tokens: int,
                        finish_reason: str = "stop") -> Dict[str, Any]:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def stream_line(model: str, cid: str, *, role: Optional[str] = None,
                content: Optional[str] = None,
                finish_reason: Optional[str] = None) -> str:
    delta: Dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def done_line() -> str:
    return "data: [DONE]\n\n"


def finish_reason_of(generation_tokens: int, max_tokens: int) -> str:
    return "length" if generation_tokens >= max_tokens else "stop"


def models_response(model: str) -> Dict[str, Any]:
    return {"object": "list",
            "data": [{"id": model, "object": "model",
                      "created": int(time.time()), "owned_by": "local"}]}
