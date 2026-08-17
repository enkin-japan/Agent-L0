"""沙箱执行器共用件（EXECUTION_LEDGER R1-1/R1-2）：一次性会话替身 + ReAct 循环。

**为什么收编**：此前 `_EphemeralSession` 有 5 份近乎逐字相同的拷贝——research /
math / excel 各一份、excel 的视觉 QA 复用其一、orchestrator 的目标审计还抄了第五份
（`_AuditSession`，其 docstring 自陈「与 agents/research_sandbox._EphemeralSession
同型」）。三条隔离不变量（CHARTER §3）由此散在 5 处注释里，改一处修不了另外四处。

**契约面从哪来**：本类的字段集不是设计出来的，是 `engine.generate_once` 读什么就有
什么——`brain_memory_state` / `history` / `images` / `audios` / `cancel_requested`
/ `inject_env_context`。它刻意**不是** ChatSession 的子类：沙箱会话不落盘、无 meta、
无记忆写回，少一个字段就少一条泄漏路径。

守的三条不变量（test_sandbox_invariants.py 钉死）：
 · **KV 隔离**——`brain_memory_state` 必取 `backend.new_cache()`，不碰真实会话 KV。
 · **不落盘**——不暴露 append_log / save_memory / save_meta / history_log 任一面。
 · **隐私边界**——`inject_env_context = False` 显式写死。⚠️ 此前这条是靠「5 份拷贝
   都恰好忘了写这个属性」+ 引擎 `getattr(..., False)` 兜底而**偶然**成立的；哪份
   拷贝哪天手抖加上它，个人环境事实就会静默流进沙箱。故此处显式设死。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Dict, List, Optional, Tuple


class EphemeralSession:
    """一次性会话替身：只暴露 generate_once 读取的字段，全新 KV、用完即弃。不落盘。

    time_anchor：注入分钟级时间锚。主编排轮次刻意只注日粒度（KV 缓存跨天友好，精确
    时间靠 get_current_time 工具），但沙箱没有时间工具、KV 又是一次性的——直接注到
    分钟+时区零缓存代价。缺锚时模型对天气/时刻类问题会触发「无实时能力」的先验拒答。
    """

    #: 隐私边界：个人环境事实（位置/语言/画像）绝不进沙箱子生成上下文。
    #: 显式写死而非依赖引擎的 getattr 兜底——见模块 docstring。
    inject_env_context = False
    #: 上位解 prompt 字节流：引擎在成功生成后会写本属性（一次性会话通常只生成
    #: 一次、写了也用不上，但契约「暴露 generate_once 读写的字段」要如实）
    prompt_stream = None

    def __init__(self, backend, system_prompt: str, opening: str,
                 images: Optional[List[str]] = None, time_anchor: bool = False):
        self.brain_memory_state = backend.new_cache()

        if time_anchor:
            _anchor = datetime.now().astimezone().strftime("%Y-%m-%d (%A) %H:%M %Z (UTC%z)")
            system_prompt = f"{system_prompt}\n\nCurrent date & time: {_anchor}"

        content: object = opening
        if images:
            content = [{"type": "image"} for _ in images]
            content.append({"type": "text", "text": opening})

        self.history: List[Dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        self.images: List[str] = list(images or [])
        self.audios: List[str] = []
        self.cancel_requested = False


# ==========================================================================
# ReAct 循环（R1-2）：三个沙箱共用的骨架
# ==========================================================================
@dataclass
class SandboxSpec:
    """一个沙箱 = 一份声明 + 若干 hook。骨架由 run_sandbox 拥有，差异由 hook 承担。

    **抽象的目标是消除拷贝，不是消除差异**（CHARTER 铁律 1 的边界）。故凡是三个沙箱
    真正不同的地方（分发、放弃、收尾），一律留成 hook 而非塞进骨架用 if 分流。
    """
    tag: str                        # "math_research" / "web_research" / "excel_research"
    session: "EphemeralSession"
    tools: List[dict]
    max_steps: int
    max_tokens: int                 # 各沙箱产物形态不同：math 1024 / excel 1536 / web 2048

    #: 分发本步的工具调用。返回 (tool_responses, final)；final 非 None 即收束返回
    #: （math 的 submit_answer 成功、excel 的 finish 过门禁）。工具集与语义完全由沙箱拥有。
    dispatch: Callable[[List[dict]], Awaitable[Tuple[List[dict], Optional[str]]]]

    #: 模型不再调工具、且推回过一次仍不合作时的返回值（三者的诚实降级形态各不相同）。
    on_give_up: Callable[[str], str]

    #: 步数/时间预算用尽时的收尾。**三者形状不同**：web/math 会再做一次强制生成，
    #: excel 不再生成、直接按门禁状态交付或如实报败。故此处是完整回调，不是提示词模板。
    on_exhausted: Callable[[], Awaitable[str]]

    #: 解析纠错反馈里**本沙箱特有的额外引导**（拼在方言的格式教学段之后）。
    #: 刻意不做成模板：三份措辞不同且是各自调优过的——research 那两句额外引导源于一次
    #: 真实事故（沙箱因单次格式错误就「未能形成结论」）。骨架只统一闭环控制流
    #: （计数/入历史/continue），额外引导归沙箱自己。
    #: ⚠️「正确格式长什么样」**不在这里写**：那是方言事实，由 run_sandbox 从
    #: `engine.dialect.parse_error_retry_hint()` 取（与编排层同一来源）。三份沙箱
    #: 曾各自硬编码 JSON 示例，§7-8 迁到紧凑 DSL 后它们会把模型往废格式教——
    #: 这正是 §S.17 的形态：教学与实际协议脱锚，症状是"模型不听话"。
    parse_feedback: str

    max_parse_retries: int = 2

    #: 纯文本结论时推回的 user 提示词；None = 不推回（research 的摘要本就是文本产物，
    #: 推回它等于逼它把已成形的结论重说一遍）。
    nudge: Optional[str] = None

    #: 返回 True 则提前跳出循环转入 on_exhausted（research 的软截止：剩余时间不够再跑
    #: 一轮「生成+抓取」时，把硬超时全损换成减配交付）。
    should_converge: Optional[Callable[[], bool]] = None

    #: 生成**前**的进度上报（research 的 ActivityCard 钩子）。刻意在生成前——挪到生成后
    #: 会让「思考中」在思考结束后才亮，前端状态就错位了。
    on_step: Optional[Callable[[int], Awaitable[None]]] = None

    #: tool 响应入历史**后**的收尾动作（excel 的修复预算通告）。
    on_step_end: Optional[Callable[[], Awaitable[None]]] = None

    #: 附加到诊断行尾的沙箱自有状态（excel 的 repairs=N）。
    trace_extra: Optional[Callable[[], str]] = None


async def run_sandbox(engine, spec: SandboxSpec) -> str:
    """沙箱 ReAct 循环（协程，运行在主事件循环上）。

    engine.generate_once 内部持有推理锁，与其他会话生成串行（单 worker 不死锁：本协程
    由工具包壳经 run_coroutine_threadsafe 投回主事件循环，此刻主轮次正 await 在工具线程
    上、已释放推理锁，故沙箱可安全取锁，不与自身争锁）。

    骨架拥有的（此前三份各写一遍、并各自漂移）：
     · 步数循环与软截止 · 取消检查 · 调用过滤（type != "error"）
     · 解析纠错闭环 · 纯文本推回 · assistant/tool 两类历史追加 · 预算用尽转收尾
    """
    sess = spec.session
    parse_retries = 0
    nudged = False

    for step in range(spec.max_steps):
        if spec.should_converge is not None and spec.should_converge():
            break

        if spec.on_step is not None:
            await spec.on_step(step)

        result = await engine.generate_once(sess, tools=spec.tools, temperature=0.2,
                                            max_tokens=spec.max_tokens)
        if result.get("cancelled"):
            return f"[{spec.tag}: generation cancelled]"

        raw_calls = result.get("tool_calls", [])
        # 宽容过滤：原生 function 与纯 JSON 调用都收，只排除解析失败（对齐主 Agent）
        calls = [c for c in raw_calls if c.get("type") != "error"]
        text = result.get("text", "")
        thoughts = result.get("thoughts", "")

        extra = f" {spec.trace_extra()}" if spec.trace_extra is not None else ""
        print(f"[{spec.tag}] step {step + 1}/{spec.max_steps}: "
              f"calls={[c.get('name') for c in calls]}{extra}")

        if not calls:
            errors = [c for c in raw_calls if c.get("type") == "error"]
            if errors and parse_retries < spec.max_parse_retries:
                # 想调工具但格式崩了：回灌正确示例让它重发，而不是就此放弃。
                parse_retries += 1
                bad = errors[0]
                sess.history.append({"role": "assistant", "content": text,
                                     "thoughts": thoughts, "tool_calls": raw_calls})
                # 格式教学取自方言（单一来源），沙箱只补自己的额外引导。
                # 无方言场景（FakeEngine 等）用编排层同一条格式中立兜底——绝不能
                # 让纠错反馈里一句格式指导都没有，那等于让模型盲猜。
                _hint_fn = getattr(getattr(engine, "dialect", None),
                                   "parse_error_retry_hint", None)
                if callable(_hint_fn):
                    _fmt = _hint_fn()
                else:
                    # 与编排层共用同一条格式中立兜底（延迟导入：agents 在模块级导入
                    # core.orchestrator 会成环）。两份拷贝各被一个测试钉住必然漂移。
                    from ..core.orchestrator import _PARSE_HINT_FALLBACK
                    _fmt = _PARSE_HINT_FALLBACK
                sess.history.append({"role": "tool", "tool_responses": [{
                    "name": "system",
                    "response": ("System Error: your tool call could not be parsed and was "
                                 f"not executed.\nYour invalid output: {bad.get('raw_input')}\n"
                                 f"Parser error: {bad.get('error_msg')}\n"
                                 + (_fmt + "\n" if _fmt else "")
                                 + spec.parse_feedback)}]})
                continue

            if spec.nudge is not None and not nudged:
                # 纯文本结论 = 想绕开工具（math：靠记忆答题；excel：绕开工作流）：推回一次
                nudged = True
                sess.history.append({"role": "assistant", "content": text,
                                     "thoughts": thoughts, "tool_calls": []})
                sess.history.append({"role": "user", "content": spec.nudge})
                continue

            return spec.on_give_up(text)

        # 记下这一步的 assistant 轮（含 tool_calls），供下一次前缀复用
        sess.history.append({"role": "assistant", "content": text,
                             "thoughts": thoughts, "tool_calls": calls})

        responses, final = await spec.dispatch(calls)
        if final is not None:
            return final
        sess.history.append({"role": "tool", "tool_responses": responses})

        if spec.on_step_end is not None:
            await spec.on_step_end()

    return await spec.on_exhausted()
