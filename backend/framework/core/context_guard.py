"""上下文上限护栏（W1，D1/D2/D33–D35）：纯函数，无 I/O。

编排层每次主循环生成前用执行端分词器实测整段 prompt（engine.measure_prompt），与三档比较：
  soft    ：只记账（本轮峰值 + 事件）；
  hard    ：记账 + 事件 + 尾注提醒模型收敛；
  ceiling = 上限 − generation_reserve：轮内应急机械压缩一次，仍超则可解释地终止本轮，
            不让后端收到一个注定 overflow（或静默退化）的请求。
阈值是整段 prompt 的绝对 token 数（含前缀），与 summary_* 的「前缀之上」口径不同。

ceiling 应急压缩的两种折法（fold_mode）：
  full    ：（缺省 = 生产现行）保护窗（最近 2 条）外的工具观察一次全部折桩；一轮只折一次，
            再到 ceiling 即终止。
  minimal ：从最旧的观察起分批折桩、每批折后重量，量到 < hard 即停（折到刚好够用）；每次生成前
            到 ceiling 都可再折，折了 0 条仍 ≥ ceiling 才终止。动机：w12eval 批实测窗内整体 fold
            净负（57K 档 Pass^2 26% → 13%），fold 的价值只剩防溢出（2026-11-09 用户拍板）。
            候选的估计可省量按实测密度校准（D48b，2026-11-09）：fallback_estimate 对真实正文偏低约
            2.2×，未校准时首批凑缺口会多折约 2.6×；引擎量 prompt 时顺带给出 density（分词器实测
            token ÷ 同一文本的 fallback_estimate，是整段 prompt 的混合密度，只是候选正文密度的近似），
            每批用最近一次量值的 density 缩放（见 fold_density）；折掉一批后混合密度会变，故逐批重取
            而非沿用首批（精确做法留作后续，见 orchestrator._emergency_compact_minimal）。

配置失败关闭：未知键、非正整数、soft ≥ hard、hard ≥ ceiling 一律在装配期抛 ValueError——inprocess 即
启动失败；remote 为计算装配失败并退避重试（数据服务照常），装配后探活发现上限不相容则算力端标为不可用——
拼错的键静默回落缺省，会把「护栏配了却没生效」伪装成模型行为问题。
"""
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

CONTEXT_GUARD_KEYS = frozenset({"enabled", "soft_tokens", "hard_tokens", "generation_reserve",
                                "fold_mode"})
DEFAULTS: Dict[str, Any] = dict(enabled=True, soft_tokens=24576, hard_tokens=39322,
                                generation_reserve=12288, fold_mode="full")
FOLD_MODES = ("full", "minimal")

LEVELS = ("ok", "soft", "hard", "ceiling")

# 每张图片在文本 prompt 里只是占位符（qwen35 VISION_PLACEHOLDER 之类），文本分词量不到视觉 token；
# CUDA 上报的 prompt_tokens 也是文本 ids 数。守卫宁高估不低估：按图片数加这个常量。
IMAGE_TOKENS_EST = 1024

# minimal 折叠的密度校准钳位（D48b）：实测 token 与 fallback_estimate 相差 4 倍以上视为仪表异常
# （分词器/装配出错、文本几乎为空之类），不让一次离谱量值把折叠量放大或缩小到失控——钳到边界
# 仍是「往实测方向修正」，逐批重量会再兜一次。
FOLD_DENSITY_MIN = 0.25
FOLD_DENSITY_MAX = 4.0

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
    fold_mode: str = "full"


@dataclass(frozen=True)
class ContextLimits:
    limit: int
    ceiling: int


@dataclass(frozen=True)
class PromptMeasure:
    """一次 prompt 规模测量（engine.measure_prompt 的返回值）。
    method: "tokenizer"（执行端分词器 encode）| "chars"（fallback_estimate）。
    density: 文本部分实测 token ÷ fallback_estimate(同一文本)（不含图片估值）；None = 未知
    （无分词器 / chars 量法 / 假引擎）。minimal 折叠用它校准候选的估计可省量（fold_density）。"""
    tokens: int
    method: str
    chars: int
    images: int
    density: Optional[float] = None


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
    fold_mode = merged["fold_mode"]
    if not isinstance(fold_mode, str) or fold_mode not in FOLD_MODES:
        raise ValueError(f"agent.context_guard.fold_mode 须为 {'|'.join(FOLD_MODES)}，得到 {fold_mode!r}")
    return ContextGuardConfig(enabled=enabled, soft=soft, hard=hard, reserve=reserve,
                              fold_mode=fold_mode)


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
    ascii_n = len(text.encode("ascii", "ignore"))     # = ord(ch) < 128 的字符数（C 速度；每次量都算）
    return ascii_n // 4 + (len(text) - ascii_n)


def fold_density(m: Optional[PromptMeasure]) -> float:
    """minimal 折叠用的密度系数：m / m.density 缺失、非 int/float（bool 也拒；字符串等需显式转换的
    类型不接）、非有限数或 ≤ 0 → 1.0（不校准，旧行为）；否则钳到 [FOLD_DENSITY_MIN, FOLD_DENSITY_MAX]。"""
    d = getattr(m, "density", None) if m is not None else None
    if not isinstance(d, (int, float)) or isinstance(d, bool):
        return 1.0
    d = float(d)
    if not math.isfinite(d) or d <= 0:
        return 1.0
    return min(FOLD_DENSITY_MAX, max(FOLD_DENSITY_MIN, d))
