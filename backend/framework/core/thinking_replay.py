"""思考回放策略（W3，D19 / D36）：纯函数，无 I/O。

history 里 assistant 消息的思考只有一个字段 `thoughts`，落盘（history.jsonl）照旧全量；
本模块只决定「哪些 thoughts 渲染进 prompt」。投影在引擎装配 prompt 时做（engine._prompt_view），
方言 builder 照常渲染拿到的全部 thoughts——方言不知道有这层策略。

三种模式（注册表条目 `thinking_replay`，缺省 last_k）：
  all        ：全部回放（旧行为；差分锚点）；
  tool_turns ：历史轮一律丢；本轮只回放带 tool_calls 的生成的思考（Gemma 4 MLX 官方模板口径）；
  last_k     ：历史轮一律丢；本轮只留最近 K 次带思考的生成（`thinking_replay_k`，缺省 2）。
纯思考消息（无正文无调用：全思考空产出 nudge 留档）在本轮内恒保留、不占 K 名额；
在历史轮里改一行占位，免得渲染出空 assistant 轮。

投影必须参与 prompt_stream 的指纹（engine 在 view 上做 stream_valid / commit）：流里存的是
含思考的原始生成字节，只改渲染不改指纹，增量路会把旧思考照原样回放。故投影一变 ⇒ 指纹变 ⇒
退全量重渲染（KV 代价见 config.yaml 注释块）。

配置失败关闭：非法模式、非正整数 K 一律装配期 ValueError——拼错的值静默回落缺省，会把
「策略配了却没生效」伪装成模型行为问题。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

THINKING_REPLAY_MODES = ("all", "tool_turns", "last_k")
DEFAULT_MODE = "last_k"
DEFAULT_K = 2
OMITTED_PLACEHOLDER = "(earlier thinking omitted)"


@dataclass(frozen=True)
class ThinkingReplayPolicy:
    mode: str
    k: int


def _check_mode(raw: Any) -> str:
    if not isinstance(raw, str) or raw not in THINKING_REPLAY_MODES:
        raise ValueError(f"model_registry 条目 thinking_replay 须为 {' | '.join(THINKING_REPLAY_MODES)}，"
                         f"得到 {raw!r}")
    return raw


def _check_k(raw: Any) -> int:
    # bool 是 int 子类：`thinking_replay_k: true` 不能被当成 1
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"model_registry 条目 thinking_replay_k 必须是正整数，得到 {raw!r}")
    return raw


def resolve_thinking_replay(entry_options: Optional[Mapping[str, Any]]) -> ThinkingReplayPolicy:
    """注册表条目 → 策略。键不设或值为 None（YAML 写了键没给值）= 未设 → 缺省 (last_k, 2)。

    thinking_replay_k 在任何模式下给了都校验（写错了就是写错了），仅 last_k 使用。"""
    entry = entry_options if isinstance(entry_options, Mapping) else {}
    raw_mode = entry.get("thinking_replay")
    mode = DEFAULT_MODE if raw_mode is None else _check_mode(raw_mode)
    raw_k = entry.get("thinking_replay_k")
    k = DEFAULT_K if raw_k is None else _check_k(raw_k)
    return ThinkingReplayPolicy(mode=mode, k=k)


def turn_start_index(session: Any, history: List[Dict[str, Any]]) -> int:
    """本轮起点：history[idx:] 是本轮模型/工具消息。

    优先 session.turn_history_start_idx（编排层在新用户轮置位，指向本轮 user 消息之后一条；
    审批/挂起续跑段沿用）；属性缺失（沙箱 EphemeralSession、直调会话替身）或越界则回落
    「末条 role==user 消息之后」（无 user 则 0）——与官方模板「末问之后」同口径。"""
    idx = getattr(session, "turn_history_start_idx", None)
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx <= len(history):
        return idx
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            return i + 1
    return 0


def _is_pure_thought(msg: Dict[str, Any]) -> bool:
    return not msg.get("content") and not msg.get("tool_calls")


def _chars(thoughts: Any) -> int:
    return len(thoughts) if isinstance(thoughts, str) else len(str(thoughts))


def project_thoughts(history: List[Dict[str, Any]], turn_start: int,
                     policy: ThinkingReplayPolicy) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """返回 (view, stats)。view 与 history 等长；只动 role==assistant 且 thoughts 为真值的消息。

    改动的消息是浅拷贝（原 history 字典绝不改）；未改的消息是**同一对象**——引擎的
    ps.seen 身份校验（流里那条原始生成字节是否仍原样出现在视图里）依赖这一点。
    mode all：view 就是传入的 history 对象本身（不复制），stats 计数全 0（不逐条统计）。
    turn_start 传 len(history) = 全部按历史轮处理（两轮之间的预热：下一次生成必是新用户轮）。"""
    stats: Dict[str, Any] = {"mode": policy.mode, "k": policy.k, "kept": 0, "dropped": 0,
                             "kept_chars": 0, "dropped_chars": 0, "placeholders": 0}
    if policy.mode == "all":
        return history, stats

    # 先定本轮里要保留思考的下标（last_k：自新到旧数带思考的非纯思考消息，前 K 条）
    keep_recent = set()
    if policy.mode == "last_k":
        budget = policy.k
        for i in range(len(history) - 1, turn_start - 1, -1):
            if budget <= 0:
                break
            m = history[i]
            if m.get("role") == "assistant" and m.get("thoughts") and not _is_pure_thought(m):
                keep_recent.add(i)
                budget -= 1

    view: List[Dict[str, Any]] = []
    for i, msg in enumerate(history):
        thoughts = msg.get("thoughts") if msg.get("role") == "assistant" else None
        if not thoughts:
            view.append(msg)
            continue
        pure = _is_pure_thought(msg)
        if i < turn_start:
            keep, placeholder = False, pure
        elif pure:
            keep, placeholder = True, False
        elif policy.mode == "tool_turns":
            keep, placeholder = bool(msg.get("tool_calls")), False
        else:  # last_k
            keep, placeholder = i in keep_recent, False
        if keep:
            stats["kept"] += 1
            stats["kept_chars"] += _chars(thoughts)
            view.append(msg)
            continue
        stats["dropped"] += 1
        stats["dropped_chars"] += _chars(thoughts)
        out = dict(msg)
        if placeholder:
            out["thoughts"] = OMITTED_PLACEHOLDER
            stats["placeholders"] += 1
        else:
            del out["thoughts"]
        view.append(out)
    return view, stats
