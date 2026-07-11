"""脚本化引擎/工具注册表替身(金测试地基)。

FakeEngine 鸭子类型对齐 AgentEngine.generate_once 的调用契约(编排层只依赖
这一个方法 + summarize),按脚本逐次吐出生成结果——不碰模型、不碰 MLX 生成路径。
FakeRegistry 鸭子类型对齐 MCPRegistry 被编排层使用的全部查询/执行接口。
"""
from typing import Any, Dict, List, Optional


class FakeEngine:
    """按脚本回放的生成引擎。

    script 每项是一个 dict,支持键:
      text: str            纯文本输出(默认经 on_text_delta 整段回吐,模拟真流式)
      thoughts: list[str]  思考块
      tool_calls: list     解析出的工具调用(与 ResponseParser 输出同构;
                           {"type":"error", raw_input, error_msg} 表示解析失败项)
      truncated: bool      本次生成被 max_tokens 截断
      cancelled: bool      本次生成期间命中取消
      metrics: dict        自定义指标(缺省给一份最小合法指标)
      stream: bool         False 时不经 on_text_delta 回吐(默认 True)
    """

    def __init__(self, script: List[Dict[str, Any]]):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []  # 每次生成收到的关键参数(断言用)

    async def generate_once(self, session, tools=None, task_block=None,
                            tail_notes=None, on_text_delta=None, **kwargs):
        self.calls.append({
            "kwargs": dict(kwargs),
            "tail_notes": list(tail_notes or []),
            "task_block": task_block,
            "n_tools": len(tools or []),
        })
        assert self.script, "FakeEngine 脚本耗尽:编排层多转了一圈(检查场景脚本长度)"
        step = self.script.pop(0)
        text = step.get("text", "")
        if text and step.get("stream", True) and on_text_delta is not None \
                and not step.get("tool_calls") and not step.get("cancelled"):
            on_text_delta(text)
        return {
            "cancelled": step.get("cancelled", False),
            "thoughts": step.get("thoughts", []),
            "text": text,
            "tool_calls": step.get("tool_calls", []),
            "truncated": step.get("truncated", False),
            "metrics": step.get("metrics", {"prompt_tokens": 100, "generation_tokens": 20,
                                            "generation_tps": 50.0}),
        }

    async def summarize(self, content, prior_summary="", **kwargs):
        return "[fake summary]"


class FakeTool:
    def __init__(self, name: str, result: str = "ok-result", *,
                 approval: bool = False, read_only: bool = True,
                 concurrency_safe: bool = True, timeout: Optional[int] = None,
                 raises: Optional[Exception] = None):
        self.name = name
        self.result = result
        self.approval = approval
        self.read_only = read_only
        self.concurrency_safe = concurrency_safe
        self.timeout = timeout
        self.raises = raises


class FakeRegistry:
    """对齐编排层消费面的注册表替身;记录每次执行供断言。"""

    def __init__(self, tools: List[FakeTool]):
        self._tools = {t.name: t for t in tools}
        self.executed: List[Dict[str, Any]] = []  # {"name", "args"} 按执行序

    # --- 编排层查询面 ---
    def get_all_schemas(self):
        return [{"name": t.name, "description": f"fake tool {t.name}",
                 "parameters": {"type": "object", "properties": {}, "required": []}}
                for t in self._tools.values()]

    def requires_approval(self, name, arguments=None):
        t = self._tools.get(name)
        return bool(t and t.approval)

    def is_read_only(self, name, arguments=None):
        t = self._tools.get(name)
        return bool(t and t.read_only)

    def is_concurrency_safe(self, name, arguments=None):
        t = self._tools.get(name)
        return bool(t and t.concurrency_safe)

    def tool_timeout(self, name):
        t = self._tools.get(name)
        return t.timeout if t else None

    def list_tools(self):
        return [{"name": t.name, "description": f"fake tool {t.name}",
                 "parameters": {"properties": {}, "required": []},
                 "requires_approval": t.approval} for t in self._tools.values()]

    # --- 执行面(编排层经 asyncio.to_thread 调用,保持同步签名) ---
    def execute_tool(self, name, arguments):
        self.executed.append({"name": name, "args": dict(arguments or {})})
        t = self._tools.get(name)
        if t is None:
            return f"System Error: No tool named '{name}' was found in the MCP registry."
        if t.raises is not None:
            return f"System Error: Exception occurred while executing '{name}': {t.raises}"
        return t.result


def call(name: str, **arguments) -> Dict[str, Any]:
    """构造一条合法工具调用(ResponseParser 输出同构)。"""
    return {"type": "function", "name": name, "arguments": arguments}


def bad_call(raw: str = '{"name": broken', msg: str = "Expecting value") -> Dict[str, Any]:
    """构造一条解析失败项。"""
    return {"type": "error", "raw_input": raw, "error_msg": msg}


class EmitCollector:
    """收集编排层 emit 的结构化事件,供断言事件序。"""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def __call__(self, event: str, data: Dict[str, Any]):
        self.events.append({"event": event, "data": data})

    def types(self) -> List[str]:
        return [e["event"] for e in self.events]

    def of(self, event_type: str) -> List[Dict[str, Any]]:
        return [e["data"] for e in self.events if e["event"] == event_type]
