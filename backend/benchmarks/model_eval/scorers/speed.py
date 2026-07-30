"""speed 维度：不设专用题，从所有轨迹的 engine metrics 顺带聚合（README 维度表）。

指标全部来自后端实测（prompt_tps / generation_tps / peak_memory / reused_prefix），
无自建计时口径——wall_s 只作对照参考。
"""
import statistics
from typing import Any, Dict, List


def _stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {}
    return {"mean": round(statistics.fmean(values), 2),
            "median": round(statistics.median(values), 2),
            "min": round(min(values), 2), "max": round(max(values), 2),
            "n": len(values)}


def aggregate(trajectories: List[Dict[str, Any]]) -> Dict[str, Any]:
    gen_tps: List[float] = []
    prompt_tps: List[float] = []
    peak_mem: List[float] = []
    prefix_hits: List[float] = []   # 多轮续跑轮的前缀命中率（reused/prompt）
    for traj in trajectories:
        for i, turn in enumerate(traj["turns"]):
            m = turn.get("metrics") or {}
            if m.get("generation_tps"):
                gen_tps.append(float(m["generation_tps"]))
            if m.get("prompt_tps"):
                prompt_tps.append(float(m["prompt_tps"]))
            if m.get("peak_memory"):
                peak_mem.append(float(m["peak_memory"]))
            if i > 0 and m.get("prompt_tokens"):
                prefix_hits.append(float(m.get("reused_prefix") or 0)
                                   / float(m["prompt_tokens"]))
    return {"generation_tps": _stats(gen_tps),
            "prompt_tps": _stats(prompt_tps),
            "peak_memory_max": round(max(peak_mem), 2) if peak_mem else None,
            "kv_prefix_hit_rate": _stats([round(v, 3) for v in prefix_hits])}
