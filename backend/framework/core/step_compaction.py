"""轮内步骤边界压缩（W2 / D37）：纯函数，无 I/O。

步骤完成（complete_step 落地）时，把**本轮已完成步骤段**（到本批 tool 消息之前——同批观察模型还没看过，
归下一边界再压）的工具观察折成桩（首 digest_chars 字 + 回读句柄）；该步 result 已在 TaskState 作步骤摘要，
不另调模型。hard 档首次触发时也对该段压一次（进行中步骤不碰），压后重量、仍 ≥ hard 才发尾注，仍超则
W1 ceiling 应急压缩兜底。

触发按 W1 档位，不按「被压 token × 后续迭代 > 一次全量重填」（两边量纲不齐，W0 没有每 token·每迭代的
保留代价系数）；起压档按后端缓存语义分（后端可选属性 block_prefix_cache，见 core/backend_protocol.py）：
  块级前缀缓存（远端 vLLM、本机 CUDA vLLM）：改历史只重填分叉点之后，轮内档位 ≥ trigger_level（缺省 soft）
    起每个步骤边界都压；
  缓存不可裁（MLX：改历史 = 整段冷预填）：≥ cold_refill_trigger_level（缺省 hard）起才压——hard 那次本就
    退全量，之后每个边界再各付一次冷填（轮统计 full_renders 记账）。

配置失败关闭（与 context_guard 同）：未知键、类型/取值不合法一律在装配期抛 ValueError——拼错的键
静默回落缺省，会把「压缩配了却没生效」伪装成模型行为问题。
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .context_guard import level_rank

STEP_COMPACTION_KEYS = frozenset({"enabled", "trigger_level", "cold_refill_trigger_level",
                                  "keep_recent", "digest_chars", "spill_full"})
DEFAULTS: Dict[str, Any] = dict(enabled=True, trigger_level="soft", cold_refill_trigger_level="hard",
                                keep_recent=2, digest_chars=200, spill_full=True)
TRIGGER_LEVELS = ("soft", "hard")

# 护栏事件名（guardrail_events.jsonl）；detail.trigger = "step"（步骤边界）| "hard"（hard 档首次）
EVT_STEP_COMPACT = "step_compaction"


@dataclass(frozen=True)
class StepCompactionConfig:
    enabled: bool
    trigger_level: str
    cold_refill_trigger_level: str
    keep_recent: int
    digest_chars: int
    spill_full: bool


def _bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"agent.step_compaction.{name} 必须是布尔值，得到 {value!r}")
    return value


def _level(name: str, value: Any) -> str:
    if not isinstance(value, str) or value not in TRIGGER_LEVELS:
        raise ValueError(f"agent.step_compaction.{name} 必须是 {list(TRIGGER_LEVELS)} 之一，得到 {value!r}")
    return value


def resolve_step_compaction(agent_cfg: Optional[Dict[str, Any]]) -> StepCompactionConfig:
    """从 agent 段解析步骤边界压缩配置；段缺省 = DEFAULTS。任何不合法即 ValueError（装配失败）。"""
    raw = (agent_cfg or {}).get("step_compaction")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"agent.step_compaction 必须是映射，得到 {type(raw).__name__}")
    unknown = sorted(set(raw) - STEP_COMPACTION_KEYS)
    if unknown:
        raise ValueError(f"agent.step_compaction 含未知键 {unknown}；合法键：{sorted(STEP_COMPACTION_KEYS)}")
    merged = {**DEFAULTS, **raw}
    keep = merged["keep_recent"]
    # bool 是 int 子类：`keep_recent: true` 不能被当成 1
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
        raise ValueError(f"agent.step_compaction.keep_recent 必须是 ≥0 的整数，得到 {keep!r}")
    digest = merged["digest_chars"]
    if isinstance(digest, bool) or not isinstance(digest, int) or digest <= 0:
        raise ValueError(f"agent.step_compaction.digest_chars 必须是正整数，得到 {digest!r}")
    return StepCompactionConfig(
        enabled=_bool("enabled", merged["enabled"]),
        trigger_level=_level("trigger_level", merged["trigger_level"]),
        cold_refill_trigger_level=_level("cold_refill_trigger_level", merged["cold_refill_trigger_level"]),
        keep_recent=keep,
        digest_chars=digest,
        spill_full=_bool("spill_full", merged["spill_full"]),
    )


def trigger_rank(cfg: StepCompactionConfig, block_prefix_cache: bool) -> int:
    """步骤边界压缩的起压档位序：块级前缀缓存后端取 trigger_level，缓存不可裁后端取 cold_refill_trigger_level。"""
    return level_rank(cfg.trigger_level if block_prefix_cache else cfg.cold_refill_trigger_level)


def should_compact(cfg: StepCompactionConfig, block_prefix_cache: bool,
                   turn_level: Optional[str]) -> bool:
    """步骤边界是否压：启用且本轮最高档位 ≥ 起压档（未测量 None 排在 ok 之前，恒不压）。"""
    return cfg.enabled and level_rank(turn_level) >= trigger_rank(cfg, block_prefix_cache)
