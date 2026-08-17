"""脚本化引擎/工具注册表替身(金测试地基)。

FakeEngine 鸭子类型对齐 AgentEngine.generate_once 的调用契约(编排层只依赖
这一个方法 + summarize + backend 缓存面),按脚本逐次吐出生成结果——不碰模型、
不碰真实生成路径。FakeBackend 对齐 InferenceBackend 的缓存生命周期面
(new/clone/release/shutdown;沙箱与收尾审计的一次性会话替身经 engine.backend
取全新缓存)。FakeRegistry 鸭子类型对齐 MCPRegistry 被编排层使用的全部查询/执行接口。
"""
from typing import Any, Dict, List, Optional


class FakeCacheHandle:
    """CacheHandle 替身:无物理缓存,token_ids 恒 None(冷启动语义)。"""
    token_ids: Optional[List[int]] = None


class FakeBackend:
    """InferenceBackend 的缓存面替身(生成面由 FakeEngine.generate_once 顶替)。"""

    def new_cache(self):
        return FakeCacheHandle()

    def clone_cache(self, src):
        return None  # 不支持克隆 → 调用方回退冷启动(既有语义)

    def release_cache(self, h):
        pass

    def shutdown(self):
        pass


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
        self.backend = FakeBackend()           # 缓存面(沙箱/审计替身经此取全新缓存)

    async def generate_once(self, session, tools=None, grammar_tools=None, task_block=None,
                            tail_notes=None, on_text_delta=None, **kwargs):
        self.calls.append({
            "kwargs": dict(kwargs),
            "tail_notes": list(tail_notes or []),
            "task_block": task_block,
            "n_tools": len(tools or []),
            "grammar_tools": grammar_tools,   # 约束语法源（与 prompt 声明解耦，§6.5）
            # 最后一条消息内容（截断）：审计类一次性会话替身的输入可断言（goal_verify 金测试用）
            "last_user": str((session.history[-1] or {}).get("content", ""))[:200]
                         if getattr(session, "history", None) else "",
        })
        assert self.script, "FakeEngine 脚本耗尽:编排层多转了一圈(检查场景脚本长度)"
        step = self.script.pop(0)
        if step.get("gate") is not None:      # 可控慢生成:等测试放行(并发场景用)
            await step["gate"].wait()
        # 模拟引擎的直播回调面(Phase 3):思考逐段、工具名预告、正文逐段
        on_thinking = kwargs.get("on_thinking_delta")
        if on_thinking is not None and step.get("thoughts") and not step.get("cancelled"):
            t = step["thoughts"]
            on_thinking(t if isinstance(t, str) else "\n".join(t))
        on_pending = kwargs.get("on_tool_pending")
        if on_pending is not None and not step.get("cancelled"):
            for call_ in step.get("tool_calls", []):
                if call_.get("type") != "error" and call_.get("name"):
                    on_pending(call_["name"])
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
                 raises: Optional[Exception] = None, validate=None, render=None):
        self.name = name
        self.result = result
        self.approval = approval
        self.read_only = read_only
        self.concurrency_safe = concurrency_safe
        self.timeout = timeout
        self.raises = raises
        self.validate = validate  # 可选参数预检: (args)->Optional[str]，对齐 validate_arguments
        self.render = render      # 渲染格(R3): {hint,payload} 字面量或 (result)->dict


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

    def validate_call(self, name, arguments=None):
        t = self._tools.get(name)
        if t is None or t.validate is None:
            return None
        err = t.validate(arguments or {})
        return str(err) if err else None

    def list_tools(self):
        return [{"name": t.name, "description": f"fake tool {t.name}",
                 "parameters": {"properties": {}, "required": []},
                 "requires_approval": t.approval} for t in self._tools.values()]

    # --- 执行面(编排层经 asyncio.to_thread 调用,保持同步签名) ---
    def execute_tool(self, name, arguments):
        """返回 (文本, 渲染格) —— 与 MCPRegistry.execute_tool 同构(R3)。
        FakeTool.render 为 {hint,payload} 字面量;默认 None = 灰框(存量用例行为不变)。"""
        self.executed.append({"name": name, "args": dict(arguments or {})})
        t = self._tools.get(name)
        if t is None:
            return (f"System Error: No tool named '{name}' was found in the MCP registry.", None)
        if t.raises is not None:
            return (f"System Error: Exception occurred while executing '{name}': {t.raises}", None)
        return (t.result, t.render)


def call(name: str, **arguments) -> Dict[str, Any]:
    """构造一条合法工具调用(ResponseParser 输出同构)。"""
    return {"type": "function", "name": name, "arguments": arguments}


def bad_call(raw: str = '{"name": broken', msg: str = "Expecting value") -> Dict[str, Any]:
    """构造一条解析失败项。"""
    return {"type": "error", "raw_input": raw, "error_msg": msg}
