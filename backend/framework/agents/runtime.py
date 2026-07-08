"""
进程级运行时句柄：把推理引擎与主事件循环暴露给「无状态 MCP 工具」。

MCP 工具由 MCPRegistry 无参实例化、经 asyncio.to_thread 在工作线程里同步执行，拿不到
app.state / engine / event loop。而 web_research 需要复用主模型跑一段子循环生成，因此在
启动装配阶段（app.py lifespan）把 engine 与 running loop 登记到这里，工具运行时按需取用。
沿用现有工具直读 config.yaml 的「进程全局资源」惯例，不引入依赖注入框架。
"""
from typing import Optional
import asyncio
from contextvars import ContextVar

_ENGINE = None            # framework.core.engine.AgentEngine
_LOOP: "Optional[asyncio.AbstractEventLoop]" = None

# 当前正在执行工具的会话（ChatSession）。用 ContextVar 而非全局变量：
# orchestrator 在 _invoke_tool 协程内 set，asyncio.to_thread 拷贝上下文进工具线程，
# 并发的工具调用各自持有独立快照，未来多 worker 也不会串台。
# 会话级工具（render_chart / deck_* 等需要落盘到 sessions/{id}/ 的）由此取归属。
_CURRENT_SESSION: "ContextVar[Optional[object]]" = ContextVar("current_session", default=None)


def set_current_session(session) -> None:
    """工具执行前登记归属会话（orchestrator._invoke_tool 内调用）。"""
    _CURRENT_SESSION.set(session)


def get_current_session():
    """工具线程内取当前会话（ChatSession）；非会话上下文返回 None。"""
    return _CURRENT_SESSION.get()


def set_runtime(engine, loop: "asyncio.AbstractEventLoop") -> None:
    """启动时登记推理引擎与主事件循环（lifespan 内调用）。"""
    global _ENGINE, _LOOP
    _ENGINE = engine
    _LOOP = loop


def get_engine():
    return _ENGINE


def get_loop() -> "Optional[asyncio.AbstractEventLoop]":
    return _LOOP


def is_ready() -> bool:
    return _ENGINE is not None and _LOOP is not None
