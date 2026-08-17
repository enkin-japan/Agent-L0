"""上位解「prompt 字节流只增不改」金测试（2026-08-17 开工，台账「上位解开工准备」）。

守的不变量：
- **字节前缀性**：第 N 轮 prompt == 第 N-1 轮「prompt + 生成原始字节」+ 增量段，
  历史段绝不重渲染——跨轮 token 纯追加，T8 门对全部缓存类型放行前缀复用；
- 增量段恒以本方言收口符开头（停止符已喂/未喂两种缓存情形都归零 excess，
  与 gemma4_generate._fed_length 记录截断配对）；
- 尾块「变了才追加」：同日多轮 env 锚点块零重复、跨天才追加新块；task_block
  变更才追加、从有到无追加撤销声明；tail_notes 恒追加（一次性语义）；
- 失效自愈：assistant 重放/未知角色 → 方言拒渲染（None）；历史改写/工具集
  变化/占位 assistant 未就位 → stream_valid 拒 → 引擎退全量重渲染并重新记录；
- 取消不提交：旧流保持为缓存内容的字节前缀；
- 后端记录截断（_fed_length）：截到 offset 承载层的最大流位置，无 offset 保持原样。
"""
import asyncio
import datetime as _dt
from types import SimpleNamespace

import pytest

from framework.core import prompt_stream as pstream
from framework.core.dialect_protocol import TASK_BLOCK_RETRACT_NOTE
from framework.dialects.gemma4 import prompt_builder as g_mod
from framework.dialects.gemma4.prompt_builder import PromptBuilder
from framework.dialects.qwen35 import prompt_builder as q_mod
from framework.dialects.qwen35.prompt_builder import (
    IM_END, IM_START, Qwen35PromptBuilder)


class _FakeTok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return list(range(len(s)))   # 引擎 token 去向仪表用（字符数即可）


class _FakeProcessor:
    tokenizer = _FakeTok()


# 关掉全部自动探测项：测试字节与机器无关
_ENV_OFF = {"inject_machine": False, "inject_timezone": False, "inject_locale": False}


@pytest.fixture
def gb():
    return PromptBuilder(_FakeProcessor(), env=_ENV_OFF)


@pytest.fixture
def qb():
    return Qwen35PromptBuilder(_FakeProcessor(), env=_ENV_OFF)


def _freeze_day(monkeypatch, mod, day: str):
    """钉住 builder 模块的 datetime.now()（env 锚点块的唯一时变项）。"""
    y, m, d = (int(x) for x in day.split("-"))

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, m, d, 12, 0, 0)

    monkeypatch.setattr(mod, "datetime", _FakeDT)


# ---------- gemma4 增量段字节形态 ----------

def test_gemma_byte_prefix_property(gb):
    """三轮会话的字节前缀性：prompt_N 以「prompt_{N-1}+生成字节」为严格前缀。"""
    msgs = [{"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"}]
    p1 = gb.build(msgs, include_env=False)
    gen1 = "Hello there!"
    tail1 = gb.tail_state(include_env=False)
    stream1 = p1 + gen1

    out = gb.build_prompt_delta([{"role": "user", "content": "again"}],
                                include_env=False, prev_tail=tail1)
    assert out is not None
    delta, tail2 = out
    # 只增不改的实质：增量段不含任何历史重渲染字节（评审 minor：startswith
    # 对 stream+delta 是同义反复，真正要守的是 delta 里没有旧轮内容）
    assert "You are helpful." not in delta and "hi" not in delta
    assert gen1 not in delta
    assert delta.startswith(f"{gb.eot_token}\n")          # 恒以收口符开头
    assert f"{gb.sot_token}user\nagain{gb.eot_token}\n" in delta
    assert "Current date" not in delta                    # 同日：env 块零重复
    assert delta.endswith(gb._generation_head(False))
    assert tail2 == tail1
    assert stream1 + delta == p1 + gen1 + delta           # 组装即前缀（构造性）


def test_gemma_delta_tool_observation_own_model_turn(gb):
    """观测独立成 model 轮（全量渲染对 tool 开头历史本就产出的形状），连续观测共轮。"""
    resp1 = [{"name": "foo", "response": {"a": 1}}]
    resp2 = [{"name": "bar", "response": "ok"}]
    tail = gb.tail_state(include_env=False)
    out = gb.build_prompt_delta(
        [{"role": "tool", "tool_responses": resp1},
         {"role": "tool", "tool_responses": resp2}],
        include_env=False, prev_tail=tail)
    assert out is not None
    delta, _ = out
    expected = (f"{gb.eot_token}\n{gb.sot_token}model\n"
                + gb._build_tool_responses(resp1) + gb._build_tool_responses(resp2)
                + f"{gb.eot_token}\n" + gb._generation_head(False))
    assert delta == expected
    assert delta.count(f"{gb.sot_token}model\n") == 2     # 观测轮 + 生成头


def test_gemma_delta_system_guardrail_and_user_after_tool(gb):
    """tool → system 护栏 → user 的混合增量：观测轮先收口再各归各轮。
    system 正文 rstrip 不 strip——与全量渲染收口语义逐字节一致（评审 M2：
    build 只 rstrip 轮体、保留前导空白，两侧分叉会打碎失效后的前缀命中）。"""
    tail = gb.tail_state(include_env=False)
    out = gb.build_prompt_delta(
        [{"role": "tool", "name": "t", "content": "42"},
         {"role": "system", "content": "\n  [System guardrail] plan now "},
         {"role": "user", "content": "go on"}],
        include_env=False, prev_tail=tail)
    assert out is not None
    delta, _ = out
    obs = gb._build_tool_responses([{"name": "t", "content": "42"}])
    assert delta == (f"{gb.eot_token}\n{gb.sot_token}model\n{obs}{gb.eot_token}\n"
                     f"{gb.sot_token}system\n\n  [System guardrail] plan now{gb.eot_token}\n"
                     f"{gb.sot_token}user\ngo on{gb.eot_token}\n"
                     + gb._generation_head(False))
    # 与全量渲染对同一条 system 消息的字节对表（M2 的防漂移钉）
    full = gb.build([{"role": "system", "content": "sys head"},
                     {"role": "system", "content": "\n  [System guardrail] plan now "}],
                    include_env=False)
    assert f"{gb.sot_token}system\n\n  [System guardrail] plan now{gb.eot_token}\n" in full


def test_gemma_env_block_appends_only_on_date_change(gb, monkeypatch):
    _freeze_day(monkeypatch, g_mod, "2026-08-17")
    tail = gb.tail_state(include_env=False)
    delta, tail_same = gb.build_prompt_delta([], include_env=False, prev_tail=tail)
    assert "Current date" not in delta
    assert tail_same["env"] == tail["env"]
    _freeze_day(monkeypatch, g_mod, "2026-08-18")
    delta2, tail_new = gb.build_prompt_delta([], include_env=False, prev_tail=tail)
    assert "Current date: 2026-08-18" in delta2            # 跨天：追加新锚点块
    assert tail_new["env"] != tail["env"]


def test_gemma_task_block_change_and_retract(gb):
    tail = {"env": gb._env_block_content(False), "task": "step 1/3"}
    # 未变：不追加
    d_same, t1 = gb.build_prompt_delta([], task_block="step 1/3",
                                       include_env=False, prev_tail=tail)
    assert "step 1/3" not in d_same and t1["task"] == "step 1/3"
    # 变更：追加新块
    d_new, t2 = gb.build_prompt_delta([], task_block="step 2/3",
                                      include_env=False, prev_tail=tail)
    assert f"{gb.sot_token}system\nstep 2/3\n{gb.eot_token}\n" in d_new
    assert t2["task"] == "step 2/3"
    # 从有到无：追加撤销声明（防模型接续已死任务）
    d_gone, t3 = gb.build_prompt_delta([], task_block=None,
                                       include_env=False, prev_tail=tail)
    assert TASK_BLOCK_RETRACT_NOTE in d_gone and t3["task"] == ""
    # 从无到无：什么都不追加
    d_none, _ = gb.build_prompt_delta([], task_block=None, include_env=False,
                                      prev_tail={"env": tail["env"], "task": ""})
    assert TASK_BLOCK_RETRACT_NOTE not in d_none


def test_gemma_tail_notes_always_appended(gb):
    tail = gb.tail_state(include_env=False)
    delta, _ = gb.build_prompt_delta([], tail_notes=["note A", "", "note B"],
                                     include_env=False, prev_tail=tail)
    assert f"{gb.sot_token}system\nnote A\nnote B\n{gb.eot_token}\n" in delta


def test_gemma_delta_refuses_unsupported_shapes(gb):
    tail = gb.tail_state(include_env=False)
    # assistant 重放：回放字节恒等是全量路的 §S.17 难题，增量绝不重蹈
    assert gb.build_prompt_delta([{"role": "assistant", "content": "x"}],
                                 include_env=False, prev_tail=tail) is None
    assert gb.build_prompt_delta([{"role": "weird", "content": "x"}],
                                 include_env=False, prev_tail=tail) is None
    assert gb.build_prompt_delta([{"role": "system", "content": "s",
                                   "tool_calls": [{"function": {"name": "f"}}]}],
                                 include_env=False, prev_tail=tail) is None


def test_gemma_thinking_head_variants(gb):
    tail = gb.tail_state(include_env=False)
    d_think, _ = gb.build_prompt_delta([], enable_thinking=True,
                                       include_env=False, prev_tail=tail)
    assert d_think.endswith(f"{gb.sot_token}model\n{gb.soc_token}thought\n")
    gb_sup = PromptBuilder(_FakeProcessor(), suppress_ghost_thinking=True, env=_ENV_OFF)
    d_sup, _ = gb_sup.build_prompt_delta([], include_env=False,
                                         prev_tail=gb_sup.tail_state(include_env=False))
    # 幽灵思考抑制的空频道预填字节与全量渲染生成头逐位一致（尾部无换行）
    assert d_sup.endswith(f"{gb_sup.sot_token}model\n"
                          f"{gb_sup.soc_token}thought\n{gb_sup.eoc_token}")


# ---------- qwen35 增量段字节形态 ----------

def test_qwen_byte_prefix_property(qb):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    p1 = qb.build(msgs, include_env=False)
    gen1 = "<think>\nhm\n</think>\n\nHello!"
    tail1 = qb.tail_state(include_env=False)
    out = qb.build_prompt_delta([{"role": "user", "content": "again"}],
                                include_env=False, prev_tail=tail1)
    assert out is not None
    delta, _ = out
    assert "sys" not in delta and gen1 not in delta       # 历史绝不重渲染
    assert delta.startswith(f"{IM_END}\n")
    assert f"{IM_START}user\nagain{IM_END}\n" in delta
    assert "Current date" not in delta
    assert delta.endswith(qb._generation_head(False))


def test_qwen_delta_tool_observation_matches_full_render_shape(qb):
    resp = [{"name": "foo", "response": {"a": 1}}]
    tail = qb.tail_state(include_env=False)
    out = qb.build_prompt_delta([{"role": "tool", "tool_responses": resp}],
                                include_env=False, prev_tail=tail)
    assert out is not None
    delta, _ = out
    assert delta == (f"{IM_END}\n" + qb._build_tool_responses(resp)
                     + qb._generation_head(False))


def test_qwen_delta_refuses_assistant_and_env_change(qb, monkeypatch):
    tail = qb.tail_state(include_env=False)
    assert qb.build_prompt_delta([{"role": "assistant", "content": "x"}],
                                 include_env=False, prev_tail=tail) is None
    _freeze_day(monkeypatch, q_mod, "2026-08-18")
    delta, _ = qb.build_prompt_delta([], include_env=False, prev_tail=tail)
    assert "Current date: 2026-08-18" in delta


def test_qwen_task_retract_note(qb):
    tail = {"env": qb._env_block_content(False), "task": "old task"}
    delta, _ = qb.build_prompt_delta([], task_block=None,
                                     include_env=False, prev_tail=tail)
    assert TASK_BLOCK_RETRACT_NOTE in delta


# ---------- 流生命周期（core/prompt_stream） ----------

def _hist():
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_stream_valid_happy_path_and_slice():
    h = _hist()
    ps = pstream.commit("PROMPT", "GEN", h, None, {"env": "e", "task": ""})
    assert ps.stream == "PROMPTGEN" and ps.seen == 2
    h2 = h + [{"role": "assistant", "content": "GEN"},
              {"role": "user", "content": "next"}]
    assert pstream.stream_valid(ps, h2, None)
    assert h2[ps.seen + 1:] == [{"role": "user", "content": "next"}]


def test_stream_invalid_cases():
    h = _hist()
    ps = pstream.commit("P", "G", h, None, {})
    assert not pstream.stream_valid(None, h, None)
    assert not pstream.stream_valid(ps, h, None)                 # 占位 assistant 未就位
    h_wrong = h + [{"role": "user", "content": "x"}]
    assert not pstream.stream_valid(ps, h_wrong, None)           # 占位角色不符
    h_ok = h + [{"role": "assistant", "content": "G"}]
    assert not pstream.stream_valid(ps, h_ok, [{"name": "t"}])   # 工具集变化
    rewritten = [{"role": "system", "content": "s"},
                 {"role": "user", "content": "REWRITTEN"},
                 {"role": "assistant", "content": "G"}]
    assert not pstream.stream_valid(ps, rewritten, None)         # 历史被改写（压缩/回滚）
    assert pstream.stream_valid(ps, h_ok, None)


# ---------- 引擎级接线（真方言 + 假后端） ----------

class _FakeBackend:
    """最小后端：捕获喂入的 prompt 与 features，回吐固定生成文本。"""

    def __init__(self, gen_text="Hello!"):
        self.processor = _FakeProcessor()
        self.prompts = []
        self.features = []
        self.gen_text = gen_text

    def stream_generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        self.features.append(dict(kwargs.get("features") or {}))
        yield SimpleNamespace(text=self.gen_text, delta=None, prompt_tokens=3,
                              generation_tokens=2, prompt_tps=1.0,
                              generation_tps=1.0, peak_memory=0.0, extra={})


def _mk_engine(backend):
    from framework.core.engine import AgentEngine
    return AgentEngine(backend, kv_config={}, model_options={"dialect": "gemma4"})


def _mk_session():
    return SimpleNamespace(history=[{"role": "system", "content": "s"},
                                    {"role": "user", "content": "hi"}],
                           images=[], audios=[], brain_memory_state=None,
                           cancel_requested=False, inject_env_context=False,
                           prompt_stream=None)


def test_engine_second_turn_uses_stream_delta():
    be = _FakeBackend(gen_text="Hi from model")
    eng = _mk_engine(be)
    s = _mk_session()
    r1 = asyncio.run(eng.generate_once(s, tools=None))
    assert r1["text"] == "Hi from model"
    ps1 = s.prompt_stream
    assert ps1 is not None and ps1.stream == be.prompts[0] + "Hi from model"

    s.history.append({"role": "assistant", "content": "Hi from model"})
    s.history.append({"role": "user", "content": "and then?"})
    r2 = asyncio.run(eng.generate_once(s, tools=None))
    # 字节前缀性（引擎级）：第二轮 prompt 以「第一轮 prompt+生成字节」为严格前缀
    assert be.prompts[1].startswith(be.prompts[0] + "Hi from model")
    assert r2["metrics"].get("stream_delta") is True
    assert s.prompt_stream.stream == be.prompts[1] + "Hi from model"
    # token 级增量喂入偏移（评审 S9）：turn1 无、turn2 == 上轮流字节长
    assert be.features[0].get("prompt_delta_offset") is None
    assert be.features[1].get("prompt_delta_offset") == len(ps1.stream)


def test_engine_falls_back_on_history_rewrite_and_recovers():
    be = _FakeBackend()
    eng = _mk_engine(be)
    s = _mk_session()
    asyncio.run(eng.generate_once(s, tools=None))
    # 模拟摘要压缩改写历史：指纹失配 → 全量重渲染并重新记录
    s.history = [{"role": "system", "content": "s"},
                 {"role": "user", "content": "compressed summary"}]
    r2 = asyncio.run(eng.generate_once(s, tools=None))
    assert "stream_delta" not in r2["metrics"]
    assert s.prompt_stream.stream == be.prompts[1] + "Hello!"
    # 改写后的下一轮又能走增量路（自愈）
    s.history.append({"role": "assistant", "content": "Hello!"})
    s.history.append({"role": "user", "content": "next"})
    r3 = asyncio.run(eng.generate_once(s, tools=None))
    assert r3["metrics"].get("stream_delta") is True


def test_engine_cancel_does_not_commit():
    be = _FakeBackend()
    eng = _mk_engine(be)
    s = _mk_session()
    asyncio.run(eng.generate_once(s, tools=None))
    ps1 = s.prompt_stream
    s.cancel_requested = True
    r = asyncio.run(eng.generate_once(s, tools=None))
    assert r["cancelled"] is True
    assert s.prompt_stream is ps1        # 取消不提交：旧流仍是缓存内容的字节前缀


# ---------- 后端记录截断（_fed_length） ----------

def test_fed_length_trims_to_min_offset():
    from framework.backends.mlx.patch.gemma4_generate import _fed_length
    dense = SimpleNamespace(offset=9)
    stateful = object()                   # 无 offset 协议（ArraysCache 类）
    assert _fed_length([dense, stateful, None], 10) == 9    # 停止符未喂：截 1
    assert _fed_length([SimpleNamespace(offset=10)], 10) == 10  # 已喂：原样
    assert _fed_length([stateful], 7) == 7                  # 无 offset 层：保持原样
    assert _fed_length([SimpleNamespace(offset=12)], 10) == 10  # 上限封顶
    # 层间分歧取 min（评审 M8）：取 max 会把滞后层没消费的 token 记进去，
    # 下一轮快路放行后滞后层静默缺 token=损坏；min 最坏只是多裁/多拒
    assert _fed_length([SimpleNamespace(offset=9),
                        SimpleNamespace(offset=7)], 10) == 7


# ---------- 评审整改的守卫（C1/C2/M4/M7/M11/M3） ----------

def test_gemma_task_appends_cap_forces_collapse(gb):
    """任务块快照堆积达阈 → 拒渲染退全量收拢（评审 C2）；阈内计数递增。"""
    tail = {"env": gb._env_block_content(False), "task": "v1", "task_appends": 5}
    out = gb.build_prompt_delta([], task_block="v2", include_env=False, prev_tail=tail)
    assert out is not None
    _, t = out
    assert t["task_appends"] == 6
    capped = {"env": gb._env_block_content(False), "task": "v6", "task_appends": 6}
    assert gb.build_prompt_delta([], task_block="v7", include_env=False,
                                 prev_tail=capped) is None
    # 未变更不受阈值影响（不追加也不递增）
    out2 = gb.build_prompt_delta([], task_block="v6", include_env=False,
                                 prev_tail=capped)
    assert out2 is not None and out2[1]["task_appends"] == 6


def test_qwen_task_appends_cap(qb):
    capped = {"env": qb._env_block_content(False), "task": "v6", "task_appends": 6}
    assert qb.build_prompt_delta([], task_block="v7", include_env=False,
                                 prev_tail=capped) is None


def test_engine_no_commit_with_tail_notes():
    """一次性尾注不冻结进流（评审 C1）：注当轮照渲染，但该轮不提交，
    且**旧流必须作废**（二过评审 C1：后端记录已前进，陈旧流在「不追加消息就
    重试」的编排路径上会把想丢弃的产出经 token 级喂入原样喂回，无痕污染）。"""
    be = _FakeBackend()
    eng = _mk_engine(be)
    s = _mk_session()
    asyncio.run(eng.generate_once(s, tools=None))
    assert s.prompt_stream is not None
    s.history.append({"role": "assistant", "content": "Hello!"})
    s.history.append({"role": "user", "content": "next"})
    asyncio.run(eng.generate_once(s, tools=None, tail_notes=["one-shot note"]))
    assert "one-shot note" in be.prompts[1]   # 本轮照常喂给模型（还吃了增量提速）
    assert s.prompt_stream is None            # 旧流作废：下一轮强制退全量


def test_engine_no_commit_on_parse_error_calls():
    """解析失败调用的原始坏字节不冻结进流（评审 M4：全量渲染刻意不回放 error）。"""
    be = _FakeBackend(gen_text="<|tool_call>call:foo{broken")   # 有头无尾 → error
    eng = _mk_engine(be)
    s = _mk_session()
    r = asyncio.run(eng.generate_once(s, tools=None))
    assert any(c.get("type") == "error" for c in r["tool_calls"])
    assert s.prompt_stream is None


def test_engine_prewarm_noop_when_stream_valid():
    """流有效时预热必须 no-op（评审 M11）：全量渲染喂进缓存会打碎纯追加命中。"""
    be = _FakeBackend()
    eng = _mk_engine(be)
    s = _mk_session()
    asyncio.run(eng.generate_once(s, tools=None))
    s.history.append({"role": "assistant", "content": "Hello!"})
    s.brain_memory_state = object()           # 预热的前置：缓存在位
    s.session_id = "t"
    fed_before = len(be.prompts)
    assert asyncio.run(eng.prewarm_session_cache(s)) is False
    assert len(be.prompts) == fed_before      # 没喂任何东西


def test_stream_valid_expected_text_guard():
    """门面路占位 assistant 的预期正文校验（评审 M7）：客户端改写回复 → 退全量。"""
    h = _hist()
    ps = pstream.commit("P", "RAW", h, None, {}, expected_text="visible")
    ok = h + [{"role": "assistant", "content": "visible"}]
    edited = h + [{"role": "assistant", "content": "user-edited reply"}]
    assert pstream.stream_valid(ps, ok, None)
    assert not pstream.stream_valid(ps, edited, None)


def test_qwen_tools_declared_once_and_prepended_without_system(qb):
    """声明只发一次（评审 M3：旧写法跟在每条 system 后）；无 system 时前置兜底。"""
    tools = [{"name": "f", "description": "d", "parameters": {}}]
    two_sys = qb.build([{"role": "system", "content": "a"},
                        {"role": "user", "content": "u"},
                        {"role": "system", "content": "rolling summary"}],
                       tools=tools, include_env=False)
    assert two_sys.count("# Tools") == 1
    no_sys = qb.build([{"role": "user", "content": "u"}], tools=tools,
                      include_env=False)
    assert no_sys.count("# Tools") == 1
    assert no_sys.index("# Tools") < no_sys.index(f"{IM_START}user")


# ---------- 接线在位钉（源文本；行为面由引擎级测试与实机回归背书） ----------

def test_server_facade_wiring_present():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    server = (root / "inference/server.py").read_text(encoding="utf-8")
    assert "_facade_assemble(dialect_msgs)" in server
    assert "_facade_commit(prompt, raw_text, dialect_msgs, stream_tail, text)" in server
    assert ('_facade_commit(asm["prompt"], raw_text, dialect_msgs,\n'
            '                                   asm["tail"], vis_text + tail)') in server
    assert "facade_stream" in server
    patch = (root / "backend/framework/backends/mlx/patch/gemma4_generate.py"
             ).read_text(encoding="utf-8")
    assert "all_ids[:_fed_length(tracked_cache, len(all_ids))]" in patch
