"""沙箱不变量金测试（EXECUTION_LEDGER R1-0：重构前先建回归网）。

**为什么这个文件必须存在**：R1 要把三个沙箱（research / math / excel）各自一份的
`_EphemeralSession` 与 ReAct 循环收编成统一执行器。但审计发现**沙箱此前零测试覆盖**
——142 项金测试没有一项碰沙箱循环。在无回归网的情况下重构铁律 1 的承重代码，
正是 CHARTER 警告的那件事（「推倒重来只会得到一个更差的复制品」）。

故先建网：本文件钉死的行为，**R1 重构前后必须一字不变地全绿**——这是「行为零变化」
唯一可信的证据。

守的三条隔离不变量（research_sandbox.py 头注释的论证，CHARTER §3「物理隔离沙箱」的实现）：
1. **工具集物理受限**——沙箱手工装配工具，主 MCPRegistry 永不扫描它们；注入即便
   劫持沙箱也够不到任何能执行动作的工具（最坏返回被污染的文本，无行为劫持能力）。
2. **KV 隔离**——一次性会话替身用后端新建的全新缓存，不碰任何真实会话的 KV。
3. **隐私边界**——个人环境事实不进沙箱子生成上下文（include_env=False）。

⚠️ 这三条在重构中**绝不能蒸发**。它们此前只是注释里的论证 + 靠「忘了写
inject_env_context」而偶然成立——本文件把它们升级为可执行契约。
"""
import pytest

from framework.agents import math_sandbox
from framework.agents.sandbox_runtime import EphemeralSession
from tests.fake_engine import FakeEngine, bad_call, call


# ---------- 不变量 1：工具集物理受限 ----------

def test_sandbox_tool_set_is_physically_limited():
    """沙箱只有自己手工装配的两个工具，且都是纯计算——没有任何能执行动作的工具
    （无 shell、无文件写、无 GUI、无网络）。注入劫持了沙箱也够不到危险能力。"""
    names = {t["name"] for t in math_sandbox._TOOLS}
    assert names == {"run_python", "submit_answer"}


@pytest.mark.asyncio
async def test_sandbox_never_receives_main_registry_tools():
    """沙箱每次生成传给引擎的工具集恒为自己那份，与主 MCPRegistry 的 34 个工具无关。"""
    eng = FakeEngine([{"text": "done", "tool_calls": []},
                      {"text": "done", "tool_calls": []}])
    await math_sandbox.run_math_research(eng, "1+1", {"max_steps": 2})
    assert eng.calls, "沙箱未发生任何生成"
    for c in eng.calls:
        assert c["n_tools"] == len(math_sandbox._TOOLS) == 2


# ---------- 不变量 2：KV 隔离 ----------

@pytest.mark.asyncio
async def test_sandbox_uses_fresh_cache_never_a_real_session(monkeypatch):
    """沙箱的会话替身必须从 backend.new_cache() 取全新缓存——不碰任何真实会话的 KV。"""
    seen = {}

    class _SpyBackend:
        def __init__(self, inner):
            self._inner = inner
            self.new_cache_calls = 0

        def new_cache(self):
            self.new_cache_calls += 1
            h = self._inner.new_cache()
            seen["handle"] = h
            return h

    eng = FakeEngine([{"text": "x", "tool_calls": []}, {"text": "x", "tool_calls": []}])
    spy = _SpyBackend(eng.backend)
    eng.backend = spy

    captured = {}
    orig = eng.generate_once

    async def _capture(session, **kw):
        captured["sess"] = session
        return await orig(session, **kw)

    eng.generate_once = _capture
    await math_sandbox.run_math_research(eng, "1+1", {"max_steps": 2})

    assert spy.new_cache_calls == 1, "沙箱必须且只须取一份全新缓存"
    sess = captured["sess"]
    assert sess.brain_memory_state is seen["handle"], "沙箱 KV 必须是新建的那份"


@pytest.mark.asyncio
async def test_sandbox_session_is_ephemeral_not_persisted():
    """会话替身不落盘：不得具备真实 ChatSession 的持久化能力面。"""
    captured = {}
    # 脚本 2 项：循环 1 步 + 步数用尽后的强制收尾生成（既有行为，math_sandbox.py:218）
    eng = FakeEngine([{"text": "x", "tool_calls": []}, {"text": "x", "tool_calls": []}])
    orig = eng.generate_once

    async def _capture(session, **kw):
        captured["sess"] = session
        return await orig(session, **kw)

    eng.generate_once = _capture
    await math_sandbox.run_math_research(eng, "1+1", {"max_steps": 1})

    sess = captured["sess"]
    for persist_api in ("append_log", "save_memory", "save_meta", "history_log"):
        assert not hasattr(sess, persist_api), f"沙箱会话替身不得暴露持久化面: {persist_api}"


# ---------- 不变量 3：隐私边界 ----------

@pytest.mark.asyncio
async def test_sandbox_never_injects_personal_env_context():
    """个人环境事实（位置/语言/画像）绝不进沙箱子生成上下文。

    引擎读 `getattr(session, "inject_env_context", False)`——沙箱替身不设该属性即
    退化为 False。此前这条隐私边界是靠"没写"偶然成立的；这里把它钉成契约。
    """
    captured = {}
    eng = FakeEngine([{"text": "x", "tool_calls": []}, {"text": "x", "tool_calls": []}])
    orig = eng.generate_once

    async def _capture(session, **kw):
        captured["sess"] = session
        return await orig(session, **kw)

    eng.generate_once = _capture
    await math_sandbox.run_math_research(eng, "1+1", {"max_steps": 1})

    assert getattr(captured["sess"], "inject_env_context", False) is False


# ---------- 循环骨架（R1 重构必须逐条保持） ----------

@pytest.mark.asyncio
async def test_cancelled_generation_returns_marker():
    eng = FakeEngine([{"cancelled": True}])
    out = await math_sandbox.run_math_research(eng, "1+1", {"max_steps": 3})
    assert out == "[math_research: generation cancelled]"


@pytest.mark.asyncio
async def test_parse_error_feeds_back_and_retries():
    """工具格式崩了不是放弃，而是回灌正确格式教学让模型重发（容错闭环）。

    §7-8 起格式教学取自 `engine.dialect.parse_error_retry_hint()`（与编排层同一来源），
    沙箱不再硬编码——三份沙箱曾各自写死 JSON 示例，迁到紧凑 DSL 后它们会把模型往
    废格式教（§S.17 的形态：教学与实际协议脱锚，症状表现为"模型不听话"）。
    无方言的 FakeEngine 走格式中立兜底，但**绝不能一句格式指导都没有**。
    """
    eng = FakeEngine([
        {"text": "", "tool_calls": [bad_call('{"name": broken')]},
        {"text": "", "tool_calls": []},
        {"text": "", "tool_calls": []},
    ])
    captured = {}
    orig = eng.generate_once

    async def _capture(session, **kw):
        captured["sess"] = session
        return await orig(session, **kw)

    eng.generate_once = _capture
    await math_sandbox.run_math_research(eng, "1+1",
                                         {"max_steps": 3, "max_parse_retries": 2})
    hist = captured["sess"].history
    feedback = [m for m in hist if m.get("role") == "tool"
                for r in m.get("tool_responses", [])
                if "could not be parsed" in str(r.get("response", ""))]
    assert feedback, "解析失败必须回灌纠错反馈"
    assert "EXACT tool-call format" in str(feedback[0]), "无方言时必须走格式中立兜底"
    assert "Resend immediately" in str(feedback[0]), "沙箱自己的额外引导仍须保留"


@pytest.mark.asyncio
async def test_parse_error_feedback_takes_format_from_dialect():
    """有方言时，纠错反馈里的格式教学必须来自方言（而非沙箱硬编码）。"""
    from framework.dialects.gemma4.dialect import Gemma4Dialect

    class _Tok:
        special_tokens_map = {}

        def encode(self, s, add_special_tokens=False):
            return [1]

    class _Proc:
        tokenizer = _Tok()

    eng = FakeEngine([
        {"text": "", "tool_calls": [bad_call('{"name": broken')]},
        {"text": "", "tool_calls": []},
        {"text": "", "tool_calls": []},
    ])
    eng.dialect = Gemma4Dialect.create(_Proc(), options={}, env={})
    captured = {}
    orig = eng.generate_once

    async def _capture(session, **kw):
        captured["sess"] = session
        return await orig(session, **kw)

    eng.generate_once = _capture
    await math_sandbox.run_math_research(eng, "1+1",
                                         {"max_steps": 3, "max_parse_retries": 2})
    body = str(captured["sess"].history)
    assert "call:update_plan{" in body, "格式教学必须是方言的紧凑 DSL 示例"
    assert "VALID JSON with double quotes" not in body, "迁移前的 JSON 教学不得复活"


@pytest.mark.asyncio
async def test_text_only_conclusion_is_nudged_once_then_degraded():
    """纯文本结论 = 想靠记忆答题：推回一次强制计算；再犯则诚实降级标注 UNVERIFIED。"""
    eng = FakeEngine([
        {"text": "答案是 2", "tool_calls": []},   # 第一次：被推回
        {"text": "还是 2", "tool_calls": []},     # 第二次：已推回过 → 降级返回
    ])
    out = await math_sandbox.run_math_research(eng, "1+1", {"max_steps": 4})
    assert "UNVERIFIED" in out, "未经计算的结论必须标注为未验证"


@pytest.mark.asyncio
async def test_submit_answer_returns_interpreter_output_verbatim(monkeypatch):
    """答案的唯一权威来源是解释器输出，不是模型转写（submit_answer 成功即早返）。"""
    monkeypatch.setattr(math_tools_target(), "execute_code",
                        lambda code, timeout=None: {"ok": True, "output": "42"})
    eng = FakeEngine([{"text": "", "tool_calls": [
        call("submit_answer", answer_code="print(42)", explanation="加法")]}])
    out = await math_sandbox.run_math_research(eng, "问题", {"max_steps": 3})
    assert "42" in out and "加法" in out


def math_tools_target():
    from framework.agents import math_tools
    return math_tools


# ---------- 统一会话替身（R1-1：5 份拷贝收编成一个实现） ----------

class _StubBackend:
    def __init__(self):
        self.handles = []

    def new_cache(self):
        h = object()
        self.handles.append(h)
        return h


def test_ephemeral_session_privacy_is_explicit_not_accidental():
    """隐私边界必须是**写死的类属性**，不是靠「忘了写」+ 引擎 getattr 兜底。

    重构前 5 份拷贝都恰好没设这个属性，隐私边界因此偶然成立——任何一份哪天手抖
    加上它，个人环境事实就会静默流进沙箱且无人报错。这里钉死：它必须显式存在。
    """
    assert "inject_env_context" in EphemeralSession.__dict__, \
        "隐私边界必须显式定义在类上，不得退回靠属性缺失的偶然成立"
    assert EphemeralSession.inject_env_context is False
    assert EphemeralSession(_StubBackend(), "s", "o").inject_env_context is False


def test_ephemeral_session_takes_fresh_cache_and_never_persists():
    """KV 隔离 + 不落盘：这两条现在由单一实现保证，不再散在 5 处注释里。"""
    b = _StubBackend()
    sess = EphemeralSession(b, "sys", "open")
    assert len(b.handles) == 1 and sess.brain_memory_state is b.handles[0]
    for persist_api in ("append_log", "save_memory", "save_meta", "history_log"):
        assert not hasattr(sess, persist_api), f"不得暴露持久化面: {persist_api}"


def test_time_anchor_is_opt_in():
    """分钟级时间锚只有 research 侧需要（沙箱无时间工具、KV 一次性）；math/excel 不带，
    否则平白改变它们的提示前缀。"""
    b = _StubBackend()
    assert "Current date & time:" not in EphemeralSession(b, "sys", "o").history[0]["content"]
    anchored = EphemeralSession(b, "sys", "o", time_anchor=True).history[0]["content"]
    assert anchored.startswith("sys") and "Current date & time:" in anchored


def test_images_ride_as_content_parts_before_text():
    """带图会话的 user 内容必须是 [image..., text] 形状（excel 视觉回读依赖此形状）。"""
    sess = EphemeralSession(_StubBackend(), "sys", "look", images=["/tmp/a.png"])
    assert sess.history[1]["content"] == [{"type": "image"},
                                          {"type": "text", "text": "look"}]
    assert sess.images == ["/tmp/a.png"]


def test_sandboxes_keep_their_real_differences():
    """R1-2 的边界：抽象消除的是拷贝，**不是差异**。这三条分歧最容易被后来者
    「顺手统一」掉，故钉死——每一条都对应一个真实理由，不是遗漏。"""
    import asyncio
    import framework.agents.sandbox_runtime as sr
    from framework.agents import research_sandbox

    grabbed = {}
    orig = sr.run_sandbox

    async def _spy(engine, spec):
        grabbed[spec.tag] = spec
        return await orig(engine, spec)

    class _Cancel:
        backend = _StubBackend()

        async def generate_once(self, sess, **kw):
            return {"cancelled": True}

    for mod in (math_sandbox, research_sandbox):
        mod.run_sandbox = _spy
    try:
        asyncio.run(math_sandbox.run_math_research(_Cancel(), "1+1", {}))
        asyncio.run(research_sandbox.run_research(_Cancel(), "q", {}))
    finally:
        for mod in (math_sandbox, research_sandbox):
            mod.run_sandbox = orig

    # 1. research 不推回：摘要本就是文本产物，推回它等于逼它把结论重说一遍。
    assert grabbed["web_research"].nudge is None
    assert grabbed["math_research"].nudge is not None

    # 2. 只有 research 有软截止（时间预算 → 减配交付而非硬超时全损）。
    assert grabbed["web_research"].should_converge is not None
    assert grabbed["math_research"].should_converge is None

    # 3. 产物形态不同 → token 预算不同（math 短代码 1024 / web 摘要 2048）。
    assert grabbed["math_research"].max_tokens == 1024
    assert grabbed["web_research"].max_tokens == 2048


def test_all_sandboxes_share_the_one_implementation():
    """三个沙箱 + orchestrator 目标审计不得再各自持有会话替身拷贝（R1-1 的收编目标）。"""
    import pathlib
    root = pathlib.Path(math_sandbox.__file__).parent.parent
    for rel in ("agents/research_sandbox.py", "agents/math_sandbox.py",
                "agents/excel_sandbox.py", "core/orchestrator.py"):
        src = (root / rel).read_text()
        assert "class _EphemeralSession" not in src, f"{rel} 仍持有会话替身拷贝"
        assert "class _AuditSession" not in src, f"{rel} 仍持有会话替身拷贝"

    # R1-2：ReAct 骨架同理——三个沙箱不得再各自写一遍步数循环。
    for rel in ("agents/research_sandbox.py", "agents/math_sandbox.py",
                "agents/excel_sandbox.py"):
        src = (root / rel).read_text()
        assert "for step in range(" not in src, f"{rel} 仍持有自己的 ReAct 循环骨架"
