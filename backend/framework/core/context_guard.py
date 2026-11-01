"""上下文上限护栏（W1，D1/D2/D33–D35）：纯函数，无 I/O。

编排层每次主循环生成前用执行端分词器实测整段 prompt（engine.measure_prompt），与三档比较：
  soft    ：只记账（本轮峰值 + 事件）；
  hard    ：记账 + 事件 + 尾注提醒模型收敛；
  ceiling = 上限 − generation_reserve：轮内应急机械压缩一次，仍超则可解释地终止本轮，
            不让后端收到一个注定 overflow（或静默退化）的请求。
阈值是整段 prompt 的绝对 token 数（含前缀），与 summary_* 的「前缀之上」口径不同。

配置失败关闭：未知键、非正整数、soft ≥ hard、hard ≥ ceiling 一律在装配期抛 ValueError——inprocess 即
启动失败；remote 为计算装配失败并退避重试（数据服务照常），装配后探活发现上限不相容则算力端标为不可用——
拼错的键静默回落缺省，会把「护栏配了却没生效」伪装成模型行为问题。
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

CONTEXT_GUARD_KEYS = frozenset({"enabled", "soft_tokens", "hard_tokens", "generation_reserve"})
DEFAULTS: Dict[str, Any] = dict(enabled=True, soft_tokens=24576, hard_tokens=39322,
                                generation_reserve=12288)

LEVELS = ("ok", "soft", "hard", "ceiling")

# 每张图片在文本 prompt 里只是占位符（qwen35 VISION_PLACEHOLDER 之类），文本分词量不到视觉 token；
# CUDA 上报的 prompt_tokens 也是文本 ids 数。守卫宁高估不低估：按图片数加这个常量。
IMAGE_TOKENS_EST = 1024

# 护栏事件名（guardrail_events.jsonl）
EVT_SOFT = "context_soft"
EVT_HARD = "context_hard"
EVT_FOLD = "context_guard_fold"
EVT_CLAMP = "context_guard_max_tokens_clamped"
EVT_UNBOUND = "context_guard_unbound"
ABORT_REASON = "context_guard"


@dataclass(frozen=True)
class ContextGuardConfig:
    enabled: bool
    soft: int
    hard: int
    reserve: int


@dataclass(frozen=True)
class ContextLimits:
    limit: int
    ceiling: int


@dataclass(frozen=True)
class PromptMeasure:
    """一次 prompt 规模测量（engine.measure_prompt 的返回值）。
    method: "tokenizer"（执行端分词器 encode）| "chars"（fallback_estimate）。"""
    tokens: int
    method: str
    chars: int
    images: int


def _positive_int(name: str, value: Any) -> int:
    # bool 是 int 子类：`soft_tokens: true` 不能被当成 1
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"agent.context_guard.{name} 必须是正整数，得到 {value!r}")
    return value


def resolve_context_guard(agent_cfg: Optional[Dict[str, Any]]) -> ContextGuardConfig:
    """从 agent 段解析护栏配置；段缺省 = DEFAULTS。任何不合法即 ValueError（装配失败，见模块头）。"""
    raw = (agent_cfg or {}).get("context_guard")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"agent.context_guard 必须是映射，得到 {type(raw).__name__}")
    unknown = sorted(set(raw) - CONTEXT_GUARD_KEYS)
    if unknown:
        raise ValueError(f"agent.context_guard 含未知键 {unknown}；合法键：{sorted(CONTEXT_GUARD_KEYS)}")
    merged = {**DEFAULTS, **raw}
    enabled = merged["enabled"]
    if not isinstance(enabled, bool):
        raise ValueError(f"agent.context_guard.enabled 必须是布尔值，得到 {enabled!r}")
    soft = _positive_int("soft_tokens", merged["soft_tokens"])
    hard = _positive_int("hard_tokens", merged["hard_tokens"])
    reserve = _positive_int("generation_reserve", merged["generation_reserve"])
    if soft >= hard:
        raise ValueError(f"agent.context_guard 须 soft_tokens < hard_tokens，得到 {soft} ≥ {hard}")
    return ContextGuardConfig(enabled=enabled, soft=soft, hard=hard, reserve=reserve)


def derive_limits(cfg: ContextGuardConfig, context_limit: int) -> ContextLimits:
    """由后端上限派生 ceiling = limit − reserve；要求 hard < ceiling，否则 ValueError（装配失败/算力端不可用，见模块头）。"""
    limit = int(context_limit)
    ceiling = limit - cfg.reserve
    if not cfg.hard < ceiling:
        raise ValueError(
            f"上下文护栏阈值与模型上限不相容：上限 limit={limit}、生成预留 reserve={cfg.reserve} → "
            f"ceiling={ceiling}，而 hard={cfg.hard} 须 < ceiling。调小 agent.context_guard.hard_tokens"
            f"（或 generation_reserve），或调大上限（本机 inference.mlx.context_limit / 算力端 "
            f"--max-model-len）。")
    return ContextLimits(limit=limit, ceiling=ceiling)


def classify(prompt_tokens: int, cfg: ContextGuardConfig, limits: ContextLimits) -> str:
    """四档判定，边界取等号归上一档。"""
    if prompt_tokens >= limits.ceiling:
        return "ceiling"
    if prompt_tokens >= cfg.hard:
        return "hard"
    if prompt_tokens >= cfg.soft:
        return "soft"
    return "ok"


def level_rank(level: Optional[str]) -> int:
    """档位序（None = 未测量，排在 ok 之前）。"""
    return LEVELS.index(level) if level in LEVELS else -1


def fallback_estimate(text: str) -> int:
    """无分词器时的保守估算：ASCII 字符数 // 4 + 非 ASCII 字符数（CJK 约 1 字 1 token）。"""
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return ascii_n // 4 + (len(text) - ascii_n)
