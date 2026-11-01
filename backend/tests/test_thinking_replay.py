"""W3 思考回放策略（D19 / D36）金测试。

守的不变量：
- 解析失败关闭：非法模式 / 非正整数 K 装配期 ValueError（引擎构造即拒）；
- 投影纯函数：history 原字典绝不改；未改消息原对象原位（引擎 ps.seen 身份校验依赖）；
  `all` 返回同一 list；历史轮丢思考（纯思考留档改占位）；本轮 tool_turns / last_k 按规则留；
- 引擎接线：流有效性判定、增量段、全量渲染、流提交全在**同一投影视图**上做——投影一变
  （思考滑出窗口 / 换用户轮）⇒ 指纹变 ⇒ 退全量；占位 assistant 的思考被投影掉 ⇒ 退全量；
- measure_prompt 与 generate_once 同文本（W1 防分叉）在投影下仍成立；
- 编排层 turn stats 带 thinking_replay 四键。
"""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from framework.core import thinking_replay as tr
from framework.core.thinking_replay import (OMITTED_PLACEHOLDER, ThinkingReplayPolicy,
                                            project_thoughts, resolve_thinking_replay,
                                            turn_start_index)

from .fake_engine import FakeEngine, call


# ====================================================================== resolve
def test_resolve_defaults_to_last_k_2():
    assert resolve_thinking_replay({}) == ThinkingReplayPolicy("last_k", 2)
    assert resolve_thinking_replay(None) == ThinkingReplayPolicy("last_k", 2)
    assert (tr.DEFAULT_MODE, tr.DEFAULT_K) == ("last_k", 2)


@pytest.mark.parametrize("mode", ["all", "tool_turns", "last_k"])
def test_resolve_each_mode(mode):
    assert resolve_thinking_replay({"thinking_replay": mode}).mode == mode


def test_resolve_none_is_unset():
    pol = resolve_thinking_replay({"thinking_replay": None, "thinking_replay_k": None})
    assert pol == ThinkingReplayPolicy("last_k", 2)


def test_resolve_k_used():
    assert resolve_thinking_replay({"thinking_replay": "last_k", "thinking_replay_k": 5}).k == 5


@pytest.mark.parametrize("bad", ["ALL", "last-k", "", 1, True, ["all"]])
def test_resolve_rejects_bad_mode(bad):
    with pytest.raises(ValueError, match="thinking_replay"):
        resolve_thinking_replay({"thinking_replay": bad})


@pytest.mark.parametrize("bad_k", [0, -1, True, "2", 1.5])
@pytest.mark.parametrize("mode", ["all", "tool_turns", "last_k"])
def test_resolve_rejects_bad_k_in_any_mode(mode, bad_k):
    with pytest.raises(ValueError, match="thinking_replay_k"):
        resolve_thinking_replay({"thinking_replay": mode, "thinking_replay_k": bad_k})


def test_engine_construction_rejects_bad_entry():
    from framework.core.engine import AgentEngine
    with pytest.raises(ValueError, match="thinking_replay"):
        AgentEngine(_CaptureBackend([]), kv_config={},
                    model_options={"dialect": "gemma4", "thinking_replay": "sometimes"})


# ====================================================================== project（纯函数）
def _calls(name="read_thing"):
    return [call(name, path="x")]


def _asst(thoughts=None, content="", tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    if thoughts is not None:
        m["thoughts"] = thoughts
    return m


def _tool():
    return {"role": "tool", "tool_responses": [{"name": "read_thing", "response": "ok"}]}


LAST_K2 = ThinkingReplayPolicy("last_k", 2)
TOOL_TURNS = ThinkingReplayPolicy("tool_turns", 2)
ALL = ThinkingReplayPolicy("all", 2)


def _two_turn_history():
    """历史轮（下标 1-5）+ 本轮（下标 6 起，turn_start=7）。"""
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        _asst("旧调用思考", tool_calls=_calls()),          # 2 历史轮·带调用
        _tool(),                                            # 3
        _asst("旧纯思考"),                                   # 4 历史轮·纯思考留档
        _asst("旧答思考", content="答一"),                   # 5 历史轮·仅正文
        {"role": "user", "content": "u2"},                  # 6
        _asst("新调用思考", tool_calls=_calls()),            # 7 本轮
        _tool(),                                            # 8
    ]


def test_project_all_returns_same_list_and_zero_stats():
    h = _two_turn_history()
    view, stats = project_thoughts(h, 7, ALL)
    assert view is h
    assert stats == {"mode": "all", "k": 2, "kept": 0, "dropped": 0, "kept_chars": 0,
                     "dropped_chars": 0, "placeholders": 0}


@pytest.mark.parametrize("pol", [LAST_K2, TOOL_TURNS])
def test_project_history_turns_drop_thoughts_and_keep_originals(pol):
    h = _two_turn_history()
    snap = copy.deepcopy(h)
    view, stats = project_thoughts(h, 7, pol)
    assert len(view) == len(h)
    assert "thoughts" not in view[2] and view[2]["tool_calls"] == h[2]["tool_calls"]
    assert view[4]["thoughts"] == OMITTED_PLACEHOLDER
    assert "thoughts" not in view[5] and view[5]["content"] == "答一"
    assert view[7]["thoughts"] == "新调用思考"
    assert h == snap                                    # 原 history 字典一个没改
    assert stats["dropped"] == 3 and stats["placeholders"] == 1 and stats["kept"] == 1
    assert stats["dropped_chars"] == len("旧调用思考") + len("旧纯思考") + len("旧答思考")
    assert stats["kept_chars"] == len("新调用思考")


def test_project_identity_of_untouched_and_copies_of_touched():
    h = _two_turn_history()
    view, _ = project_thoughts(h, 7, TOOL_TURNS)
    for i in (0, 1, 3, 6, 7, 8):
        assert view[i] is h[i]
    for i in (2, 4, 5):
        assert view[i] is not h[i] and "thoughts" in h[i]


def test_project_tool_turns_current_turn():
    h = [{"role": "user", "content": "u"},
         _asst("调用思考", tool_calls=_calls()), _tool(),
         _asst("纯思考"),
         _asst("答思考", content="答")]
    view, stats = project_thoughts(h, 1, TOOL_TURNS)
    assert view[1]["thoughts"] == "调用思考"
    assert view[3]["thoughts"] == "纯思考" and view[3] is h[3]
    assert "thoughts" not in view[4]
    assert stats["kept"] == 2 and stats["dropped"] == 1 and stats["placeholders"] == 0


def _three_gens():
    return [{"role": "user", "content": "u"},
            _asst("一", tool_calls=_calls()), _tool(),
            _asst("二", tool_calls=_calls()), _tool(),
            _asst("三", tool_calls=_calls()), _tool()]


def test_project_last_k2_drops_oldest_of_three():
    h = _three_gens()
    view, stats = project_thoughts(h, 1, LAST_K2)
    assert "thoughts" not in view[1]
    assert view[3]["thoughts"] == "二" and view[5]["thoughts"] == "三"
    assert stats["kept"] == 2 and stats["dropped"] == 1 and stats["dropped_chars"] == 1


def test_project_last_k1():
    view, stats = project_thoughts(_three_gens(), 1, ThinkingReplayPolicy("last_k", 1))
    assert [("thoughts" in view[i]) for i in (1, 3, 5)] == [False, False, True]
    assert stats["kept"] == 1 and stats["dropped"] == 2


def test_project_last_k_pure_thought_exempt_and_kept():
    h = [{"role": "user", "content": "u"},
         _asst("一", tool_calls=_calls()), _tool(),
         _asst("纯甲"),
         _asst("二", tool_calls=_calls()), _tool(),
         _asst("纯乙")]
    view, stats = project_thoughts(h, 1, LAST_K2)
    assert view[3]["thoughts"] == "纯甲" and view[6]["thoughts"] == "纯乙"
    assert view[1]["thoughts"] == "一" and view[4]["thoughts"] == "二"   # 纯思考不占名额
    assert stats["kept"] == 4 and stats["dropped"] == 0


def test_project_last_k_fewer_than_k_keeps_all():
    h = [{"role": "user", "content": "u"}, _asst("一", tool_calls=_calls()), _tool()]
    view, stats = project_thoughts(h, 1, LAST_K2)
    assert view[1] is h[1] and stats["dropped"] == 0 and stats["kept"] == 1


def test_project_legacy_list_thoughts_do_not_raise():
    h = [{"role": "user", "content": "u1"},
         _asst(["[System upgrade] 旧 list 型"], tool_calls=_calls()), _tool(),
         {"role": "user", "content": "u2"}]
    view, stats = project_thoughts(h, 4, LAST_K2)
    assert "thoughts" not in view[1]
    assert stats["dropped_chars"] == len(str(["[System upgrade] 旧 list 型"]))


# ====================================================================== turn_start
def test_turn_start_fallback_after_last_user():
    h = _two_turn_history()
    assert turn_start_index(SimpleNamespace(), h) == 7
    assert turn_start_index(None, h) == 7
    assert turn_start_index(None, [{"role": "system", "content": "s"}]) == 0


@pytest.mark.parametrize("bad", [-1, 100, None, "3", True])
def test_turn_start_out_of_range_attr_falls_back(bad):
    h = _two_turn_history()
    assert turn_start_index(SimpleNamespace(turn_history_start_idx=bad), h) == 7


def test_turn_start_valid_attr_wins():
    h = _two_turn_history()
    assert turn_start_index(SimpleNamespace(turn_history_start_idx=2), h) == 2
    assert turn_start_index(SimpleNamespace(turn_history_start_idx=len(h)), h) == len(h)


# ====================================================================== 引擎层（真方言 + 假后端）
class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return s.split()


class _CaptureBackend:
    """捕获喂入的 prompt；每次生成依次回吐 gen_texts（用尽回吐 "ok"）。"""

    def __init__(self, gen_texts):
        self.processor = SimpleNamespace(tokenizer=_Tok())
        self.prompts = []
        self.features = []
        self.gen_texts = list(gen_texts)

    def stream_generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        self.features.append(dict(kwargs.get("features") or {}))
        text = self.gen_texts.pop(0) if self.gen_texts else "ok"
        yield SimpleNamespace(text=text, delta=None, prompt_tokens=len(prompt.split()),
                              generation_tokens=1, prompt_tps=1.0, generation_tps=1.0,
                              peak_memory=0.0, extra={})


def _engine(dialect, mode=None, k=None, gen_texts=()):
    from framework.core.engine import AgentEngine
    opts = {"dialect": dialect}
    if mode is not None:
        opts["thinking_replay"] = mode
    if k is not None:
        opts["thinking_replay_k"] = k
    be = _CaptureBackend(gen_texts)
    return AgentEngine(be, kv_config={}, model_options=opts), be


def _session(history, start=None):
    s = SimpleNamespace(history=history, images=[], audios=[], brain_memory_state=None,
                        cancel_requested=False, inject_env_context=False,
                        prompt_stream=None, session_id="tr-s")
    if start is not None:
        s.turn_history_start_idx = start
    return s


def _assemble(eng, s):
    inc, extra = eng._prompt_env(s)
    return eng._assemble_prompt(s, None, enable_thinking=False, task_block=None,
                                tail_notes=None, include_env=inc, env_extra=extra)


def _gen(eng, s):
    return asyncio.run(eng.generate_once(s, tools=None))


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_engine_last_k2_slides_window_and_invalidates_stream(dialect):
    eng, be = _engine(dialect, "last_k", 2)
    s = _session([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], start=2)
    _gen(eng, s)
    s.history += [_asst("思考甲", tool_calls=_calls()), _tool()]
    assert _assemble(eng, s)[2] is not None                    # 1 条思考：流有效
    _gen(eng, s)
    s.history += [_asst("思考乙", tool_calls=_calls()), _tool()]
    assert _assemble(eng, s)[2] is not None                    # 2 条 = K：仍有效
    _gen(eng, s)
    s.history += [_asst("思考丙", tool_calls=_calls()), _tool()]
    prompt, _tail, offset, view, stats = _assemble(eng, s)
    assert offset is None                                      # 甲滑出窗口 → 指纹变 → 全量
    assert "思考甲" not in prompt and "思考乙" in prompt and "思考丙" in prompt
    assert stats["kept"] == 2 and stats["dropped"] == 1
    assert "thoughts" in s.history[2]                          # 投影不改 history
    _gen(eng, s)
    assert s.turn_thinking_replay["dropped"] == 1
    assert "思考甲" not in be.prompts[-1]


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_engine_tool_turns_keeps_stream_in_turn_and_drops_on_new_user_turn(dialect):
    eng, be = _engine(dialect, "tool_turns", gen_texts=["思考甲", "思考乙", "思考丙"])
    s = _session([{"role": "system", "content": "s"}, {"role": "user", "content": "u1"}], start=2)
    _gen(eng, s)
    s.history += [_asst("思考甲", tool_calls=_calls()), _tool()]
    p2, _t, off2, _v, _s = _assemble(eng, s)
    assert off2 is not None and "思考甲" in p2
    _gen(eng, s)
    s.history += [_asst("思考乙", tool_calls=_calls()), _tool()]
    p3, _t, off3, _v, _s = _assemble(eng, s)
    assert off3 is not None and "思考甲" in p3 and "思考乙" in p3
    _gen(eng, s)
    s.history.append(_asst("思考丙", content="答完"))
    # 换用户轮：追加 user、置新起点
    s.history.append({"role": "user", "content": "u2"})
    s.turn_history_start_idx = len(s.history)
    p4, _t, off4, _v, stats = _assemble(eng, s)
    assert off4 is None
    assert all(t not in p4 for t in ("思考甲", "思考乙", "思考丙"))
    assert stats["dropped"] == 3 and stats["kept"] == 0


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_engine_commit_fingerprints_projected_view(dialect):
    """上一轮的思考在视图里被丢：commit 若按原 history 算指纹，下一步必然失配退全量。"""
    eng, _be = _engine(dialect, "tool_turns")
    h = [{"role": "system", "content": "s"}, {"role": "user", "content": "u1"},
         _asst("上轮思考", tool_calls=_calls()), _tool(), _asst("上轮答思考", content="答"),
         {"role": "user", "content": "u2"}]
    s = _session(h, start=len(h))
    _gen(eng, s)
    s.history += [_asst("本轮思考", tool_calls=_calls()), _tool()]
    prompt, _t, offset, _v, _s = _assemble(eng, s)
    assert offset is not None
    assert "上轮思考" not in prompt and "上轮答思考" not in prompt
    _gen(eng, s)
    s.history += [_asst("本轮思考二", tool_calls=_calls()), _tool()]
    assert _assemble(eng, s)[2] is not None


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_engine_seen_identity_check_forces_full_render(dialect):
    """流里记账的那条 assistant（仅正文带思考）在 tool_turns 下被投影掉思考：
    指纹覆盖段没变，但流字节与视图不符——必须退全量。"""
    eng, _be = _engine(dialect, "tool_turns", gen_texts=["原始思考字节"])
    s = _session([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], start=2)
    _gen(eng, s)
    s.history.append(_asst("原始思考字节", content="先答一句"))
    s.history.append({"role": "user", "content": "继续"})       # 方言增量段接受的新消息
    prompt, _t, offset, _v, _s = _assemble(eng, s)
    assert offset is None
    assert "原始思考字节" not in prompt


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_engine_all_mode_bytes_identical_to_unprojected(dialect):
    eng, _be = _engine(dialect, "all")
    s = _session(_two_turn_history(), start=7)
    prompt, _t, offset, view, stats = _assemble(eng, s)
    assert offset is None and view is s.history and stats["dropped"] == 0
    inc, extra = eng._prompt_env(s)
    want = eng.dialect.build_prompt(messages=s.history, tools=None, enable_thinking=False,
                                    task_block=None, tail_notes=None,
                                    include_env=inc, env_extra=extra)
    assert prompt == want
    assert "旧调用思考" in prompt and "旧答思考" in prompt


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_engine_default_policy_is_last_k_and_renders_projection(dialect):
    eng, _be = _engine(dialect)
    assert eng.thinking_replay == ThinkingReplayPolicy("last_k", 2)
    s = _session(_two_turn_history())                 # 无属性 → 回落「末条 user 之后」
    prompt = _assemble(eng, s)[0]
    assert "旧调用思考" not in prompt and "旧答思考" not in prompt
    assert OMITTED_PLACEHOLDER in prompt and "新调用思考" in prompt


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_measure_equals_generate_with_projection(dialect):
    """W1 防分叉扩一条：带投影的历史，measure 量到的就是实发文本。"""
    eng, be = _engine(dialect, "last_k", 2)
    s = _session(_two_turn_history(), start=7)
    m = eng.measure_prompt(s)
    _gen(eng, s)
    assert m.tokens == len(be.prompts[0].split()) and m.chars == len(be.prompts[0])
    assert "旧答思考" not in be.prompts[0]


def test_prewarm_session_cache_treats_whole_history_as_past_turns():
    eng, be = _engine("gemma4", "tool_turns")
    s = _session(_two_turn_history(), start=7)       # 起点属性还指着刚结束的那一轮
    s.brain_memory_state = object()
    assert asyncio.run(eng.prewarm_session_cache(s)) is True
    assert "旧答思考" not in be.prompts[-1] and "新调用思考" not in be.prompts[-1]


def _common_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _prewarm_then_next_turn(eng, be, s, answer):
    """预热（两轮之间）→ 下一用户轮首次装配；返回 (预热 prompt, 下一轮 prompt, 下一轮 offset)。"""
    s.brain_memory_state = object()
    assert asyncio.run(eng.prewarm_session_cache(s)) is True     # 未 no-op：真的预填了
    warm = be.prompts[-1]
    s.history.append({"role": "user", "content": "下一问"})
    s.turn_history_start_idx = len(s.history)
    nxt, _t, offset, _v, _s = _assemble(eng, s)
    # 预热字节是下一轮全量 prompt 的前缀，至少覆盖到上一轮最后一条回答的正文
    assert _common_prefix_len(warm, nxt) >= warm.index(answer) + len(answer)
    return warm, nxt, offset


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_prewarm_after_turn_seen_projected_falls_through(dialect):
    """流指纹仍有效、但流里记账的那条 assistant（本轮最后的回答带思考）在预热视图里被投影掉
    → _stream_usable 的身份判据拒 → 预热全量渲染，且是下一轮首次全量 prompt 的前缀。"""
    from framework.core import prompt_stream as pstream
    eng, be = _engine(dialect, "last_k", 2)   # 轮内两条都留：只有「全按历史轮」才丢
    s = _session([{"role": "system", "content": "s"}, {"role": "user", "content": "u1"}], start=2)
    _gen(eng, s)
    s.history.append(_asst("末轮思考", content="末轮回答"))
    pre_view, _ = project_thoughts(s.history, len(s.history), eng.thinking_replay)
    assert pstream.stream_valid(s.prompt_stream, pre_view, None)          # 指纹判据过
    assert pre_view[s.prompt_stream.seen] is not s.history[s.prompt_stream.seen]   # 身份判据拒
    warm, nxt, offset = _prewarm_then_next_turn(eng, be, s, "末轮回答")
    assert "末轮思考" not in warm and "末轮思考" not in nxt and offset is None


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_prewarm_after_turn_stream_invalid_falls_through(dialect):
    """本轮较早的带调用思考在预热视图里被丢 → 指纹判据拒 → 预热全量渲染（同为下一轮前缀）。"""
    from framework.core import prompt_stream as pstream
    eng, be = _engine(dialect, "last_k", 2)   # 轮内两条都留：只有「全按历史轮」才丢
    s = _session([{"role": "system", "content": "s"}, {"role": "user", "content": "u1"}], start=2)
    _gen(eng, s)
    s.history += [_asst("调用思考", tool_calls=_calls()), _tool()]
    _gen(eng, s)
    s.history.append(_asst("答思考", content="最终回答"))
    pre_view, _ = project_thoughts(s.history, len(s.history), eng.thinking_replay)
    assert not pstream.stream_valid(s.prompt_stream, pre_view, None)      # 指纹判据拒
    warm, nxt, offset = _prewarm_then_next_turn(eng, be, s, "最终回答")
    assert all(t not in warm for t in ("调用思考", "答思考")) and offset is None


# ====================================================================== 编排层 turn stats
class _ReplayFakeEngine(FakeEngine):
    async def generate_once(self, session, **kw):
        session.turn_thinking_replay = dict(self.replay_stats)
        return await super().generate_once(session, **kw)


def test_turn_stats_carry_thinking_replay(make_session):
    from .test_context_guard import _orch, _run
    eng = _ReplayFakeEngine([{"text": "完成"}])
    eng.replay_stats = {"mode": "last_k", "k": 2, "kept": 1, "dropped": 3, "kept_chars": 7,
                        "dropped_chars": 42, "placeholders": 1}
    r = _run(_orch(eng), make_session())
    assert r["status"] == "success"
    assert r["metrics"]["thinking_replay"] == {"mode": "last_k", "kept": 1, "dropped": 3,
                                               "kept_chars": 7, "dropped_chars": 42}


def test_turn_stats_all_mode_adds_no_key(make_session):
    from .test_context_guard import _orch, _run
    eng = _ReplayFakeEngine([{"text": "完成"}])
    eng.replay_stats = {"mode": "all", "k": 2, "kept": 0, "dropped": 0, "kept_chars": 0,
                        "dropped_chars": 0, "placeholders": 0}
    r = _run(_orch(eng), make_session())
    assert "thinking_replay" not in r["metrics"]


def test_turn_stats_without_engine_stats_have_no_key(make_session):
    from .test_context_guard import _orch, _run
    r = _run(_orch(FakeEngine([{"text": "完成"}])), make_session())
    assert "thinking_replay" not in r["metrics"]


def test_turn_stats_previous_turn_replay_stats_reset_at_turn_start(make_session):
    """阶段复审 m5-L（2b12243：轮首清统计）：上一轮生成留下的 last_k 投影统计不得盖进下一轮。
    下一轮在 ceiling 处生成前即终止（零次生成、engine 不再写 turn_thinking_replay）——
    轮首不清零，终止轮的 metrics 会带上一轮的 thinking_replay。"""
    from .test_context_guard import _orch, _run
    eng = _ReplayFakeEngine([{"text": "完成"}, {"text": "不该走到这里"}])
    eng.replay_stats = {"mode": "last_k", "k": 2, "kept": 1, "dropped": 3, "kept_chars": 7,
                        "dropped_chars": 42, "placeholders": 1}
    orch = _orch(eng)
    session = make_session()
    eng.measure_script = [100]
    r1 = _run(orch, session)
    assert r1["metrics"]["thinking_replay"]["mode"] == "last_k"
    eng.replay_stats = {"mode": "all", "k": 2, "kept": 0, "dropped": 0, "kept_chars": 0,
                        "dropped_chars": 0, "placeholders": 0}
    eng.measure_script = [60000, 60000]
    r2 = _run(orch, session)
    assert r2["status"] == "warning" and len(eng.calls) == 1      # 第二轮零次生成
    assert "thinking_replay" not in r2["metrics"]
