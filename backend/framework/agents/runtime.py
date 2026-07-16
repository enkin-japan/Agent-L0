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


# 当前工具执行段的进度上报钩子 hook(label, detail="")。与 _CURRENT_SESSION 同一套惯例：
# orchestrator 在 _invoke_tool 内 set，to_thread 拷贝上下文进工具线程。长耗时工具
# （web_research / math_research 等）在关键节点上报「正在搜索/正在阅读」，钩子把它写进
# 会话活动流并经既有 task_progress 管道实时到前端 ActivityCard；非流式轮次退化为
# 只更新 activity（/task 轮询可见）。任意线程可调（钩子内部负责回主循环）。
# 注意：run_coroutine_threadsafe 投回主循环的协程【不继承】工具线程的 ContextVar
# （Task 上下文在循环线程捕获）——web_research 这类沙箱须在工具线程先 get_progress_hook()
# 再把钩子作参数传入协程；纯工具线程内（math_research）直接调 report_progress 即可。
_PROGRESS_HOOK: "ContextVar[Optional[object]]" = ContextVar("progress_hook", default=None)


def set_progress_hook(hook) -> None:
    """工具执行前登记进度钩子（orchestrator._invoke_tool 内调用）。"""
    _PROGRESS_HOOK.set(hook)


def get_progress_hook():
    """工具线程内取进度钩子；非工具上下文返回 None。"""
    return _PROGRESS_HOOK.get()


def report_progress(label: str, detail: str = "") -> None:
    """工具内部上报当前动作。无钩子零开销；异常吞掉——进度是仪表，绝不影响工具本体。"""
    hook = _PROGRESS_HOOK.get()
    if hook is None:
        return
    try:
        hook(label, detail)
    except Exception:
        pass


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


# ---------- Skill 剧本注册表（R2a） ----------
# 与 _ENGINE/_LOOP 同一惯例：装配阶段登记，工具线程内取用。skill_invoke 是无状态工具，
# 拿不到 app.state，故经此取剧本。
_SKILLS = None            # framework.core.skill_registry.SkillRegistry


def set_skill_registry(registry) -> None:
    """启动时登记剧本注册表（lifespan 内调用，须在 MCPRegistry 之后——探活要看工具表）。"""
    global _SKILLS
    _SKILLS = registry


def get_skill_registry():
    return _SKILLS


# ---------- 长期记忆笔记本（R4） ----------
# 同一惯例。memory_write 是无状态工具；prompt_builder 侧的索引注入则由 engine 取用
# （见 core/engine.py 的 env_extra）——两处都需要一个进程级句柄。
_NOTES = None             # framework.memory.notes.NoteStore


def set_note_store(store) -> None:
    """启动时登记笔记本（lifespan 内调用）。"""
    global _NOTES
    _NOTES = store


def get_note_store():
    return _NOTES
