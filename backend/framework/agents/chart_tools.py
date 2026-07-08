"""数据图表渲染核心：结构化 spec → PNG。模型全程不写绘图代码。

被两处共用（与 math_tools.py 同格局——能力本体放 agents，MCP 层只是包壳）：
 · framework/mcp/tools/render_chart.py —— 聊天里直接出图的工具；
 · framework/ppt/visual_router.py      —— deck 视觉路由的 chart 分支（回填 asset_id）。

为什么输入是结构化 JSON 而不是让模型写 matplotlib 代码：
 · 小模型写码不可靠（实测 run_math 时代的全角符号/API 混用问题），数据字段不会坏；
 · 数据不是代码，没有注入面——无需 seatbelt 沙箱，进程内直接渲染；
 · 图表外观由本层统一模板保证（PPT.md 的原则：图表绝不文生图、也不自由放飞样式）。

CJK：默认字体链 Helvetica Neue → PingFang SC → Hiragino Sans，中日文标题/标签不出豆腐块。
"""
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

CHART_TYPES = ("bar", "barh", "line", "pie", "scatter")

# 默认调色板（无 theme 时使用；deck 集成时由 tokens.palette 覆盖）
_DEFAULT_PALETTE = ["#4C72B0", "#DD8452", "#55A868", "#C44E52",
                    "#8172B3", "#937860", "#DA8BC3", "#8C8C8C"]

_FONT_STACK = ["Helvetica Neue", "PingFang SC", "Hiragino Sans", "Arial Unicode MS"]

MAX_POINTS = 500      # 单序列数据点上限（图表是给人看的，超过这个量该先聚合）
MAX_SERIES = 8


class ChartSpecError(ValueError):
    """spec 校验失败：message 直接回喂模型（fix-and-retry 闭环），须具体可操作。"""


def _require_numbers(values, where: str) -> List[float]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ChartSpecError(f"{where} must be a non-empty array of numbers.")
    if len(values) > MAX_POINTS:
        raise ChartSpecError(f"{where} has {len(values)} points (max {MAX_POINTS}); "
                             "aggregate the data first.")
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ChartSpecError(f"{where} contains a non-numeric value: {v!r}. "
                                 "All values must be plain numbers (no strings, no units).")
        out.append(float(v))
    return out


def validate_spec(spec: Dict) -> Dict:
    """规范化并校验 spec；返回净化后的副本。失败抛 ChartSpecError（消息可直接回喂模型）。"""
    chart_type = spec.get("chart_type")
    if chart_type not in CHART_TYPES:
        raise ChartSpecError(f"chart_type must be one of {list(CHART_TYPES)}, got {chart_type!r}.")

    raw_series = spec.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise ChartSpecError('series must be a non-empty array, e.g. '
                             '[{"name": "sales", "values": [1, 2, 3]}].')
    if len(raw_series) > MAX_SERIES:
        raise ChartSpecError(f"too many series ({len(raw_series)}, max {MAX_SERIES}).")

    series = []
    for i, s in enumerate(raw_series):
        if not isinstance(s, dict):
            raise ChartSpecError(f"series[{i}] must be an object with a 'values' array.")
        values = _require_numbers(s.get("values"), f"series[{i}].values")
        item = {"name": str(s.get("name", "")), "values": values}
        if chart_type == "scatter":
            item["x"] = _require_numbers(s.get("x"), f"series[{i}].x (required for scatter)")
            if len(item["x"]) != len(values):
                raise ChartSpecError(f"series[{i}]: x has {len(item['x'])} points but values "
                                     f"has {len(values)}; they must match.")
        series.append(item)

    labels = spec.get("labels")
    if labels is not None:
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise ChartSpecError("labels must be an array of strings.")
        labels = [str(x) for x in labels]

    if chart_type == "pie":
        if len(series) != 1:
            raise ChartSpecError("pie accepts exactly one series (its values are the slices).")
        if not labels or len(labels) != len(series[0]["values"]):
            raise ChartSpecError("pie requires labels with one label per slice "
                                 f"({len(series[0]['values'])} slices).")
        if any(v < 0 for v in series[0]["values"]):
            raise ChartSpecError("pie values must be non-negative.")
    elif chart_type in ("bar", "barh", "line"):
        n = len(series[0]["values"])
        for i, s in enumerate(series):
            if len(s["values"]) != n:
                raise ChartSpecError(f"all series must have the same length; series[0] has {n} "
                                     f"but series[{i}] has {len(s['values'])}.")
        if labels and len(labels) != n:
            raise ChartSpecError(f"labels has {len(labels)} entries but each series has {n} "
                                 "values; they must match.")

    return {
        "chart_type": chart_type,
        "title": str(spec.get("title", "")),
        "x_label": str(spec.get("x_label", "")),
        "y_label": str(spec.get("y_label", "")),
        "labels": labels,
        "series": series,
        "theme": spec.get("theme") if isinstance(spec.get("theme"), dict) else None,
    }


def render_chart(spec: Dict, out_path: Path) -> Path:
    """按已校验的 spec 渲染 PNG 到 out_path。纯函数式：同 spec 恒同图。"""
    import matplotlib
    matplotlib.use("Agg")  # 无头后端：工具线程渲染，绝不触碰 GUI 主线程
    import matplotlib.pyplot as plt

    theme = spec.get("theme") or {}
    palette = theme.get("palette") or _DEFAULT_PALETTE
    fg = theme.get("fg", "#1a1a1a")
    bg = theme.get("bg", "#ffffff")

    with plt.rc_context({
        "font.family": _FONT_STACK,
        "axes.unicode_minus": False,
        "figure.facecolor": bg, "axes.facecolor": bg,
        "text.color": fg, "axes.labelcolor": fg,
        "xtick.color": fg, "ytick.color": fg,
        "axes.edgecolor": fg,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.titlesize": 15, "axes.titleweight": "bold", "axes.titlepad": 14,
        "legend.frameon": False,
    }):
        fig, ax = plt.subplots(figsize=(10, 6), dpi=150)
        ct = spec["chart_type"]
        series = spec["series"]
        n = len(series[0]["values"])
        labels = spec["labels"] or [str(i + 1) for i in range(n)]
        colors = [palette[i % len(palette)] for i in range(len(series))]
        show_legend = len(series) > 1 or any(s["name"] for s in series)

        if ct in ("bar", "barh"):
            import numpy as np
            idx = np.arange(n)
            width = 0.8 / len(series)
            for i, s in enumerate(series):
                offset = (i - (len(series) - 1) / 2) * width
                if ct == "bar":
                    bars = ax.bar(idx + offset, s["values"], width,
                                  label=s["name"] or f"series {i+1}", color=colors[i])
                else:
                    bars = ax.barh(idx + offset, s["values"], width,
                                   label=s["name"] or f"series {i+1}", color=colors[i])
                if n * len(series) <= 20:  # 柱子少时标数值，多了会糊成一团
                    ax.bar_label(bars, fmt="%g", padding=2, fontsize=9)
            if ct == "bar":
                ax.set_xticks(idx, labels)
                ax.grid(axis="y", alpha=0.25)
            else:
                ax.set_yticks(idx, labels)
                ax.grid(axis="x", alpha=0.25)
        elif ct == "line":
            xs = range(n)
            for i, s in enumerate(series):
                ax.plot(xs, s["values"], marker="o", markersize=4, linewidth=2,
                        label=s["name"] or f"series {i+1}", color=colors[i])
            ax.set_xticks(range(n), labels)
            ax.grid(axis="y", alpha=0.25)
        elif ct == "scatter":
            for i, s in enumerate(series):
                ax.scatter(s["x"], s["values"], s=36, alpha=0.8,
                           label=s["name"] or f"series {i+1}", color=colors[i])
            ax.grid(alpha=0.25)
        elif ct == "pie":
            vals = series[0]["values"]
            ax.pie(vals, labels=labels, autopct="%1.1f%%", startangle=90,
                   colors=[palette[i % len(palette)] for i in range(len(vals))],
                   textprops={"color": fg})
            ax.axis("equal")

        if spec["title"]:
            ax.set_title(spec["title"])
        if spec["x_label"] and ct != "pie":
            ax.set_xlabel(spec["x_label"])
        if spec["y_label"] and ct != "pie":
            ax.set_ylabel(spec["y_label"])
        if show_legend and ct != "pie":
            ax.legend()

        # 旋转过长的分类刻度，防重叠
        if ct in ("bar", "line") and max((len(str(x)) for x in labels), default=0) > 6:
            plt.setp(ax.get_xticklabels(), rotation=30, ha="right")

        fig.tight_layout()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, format="png")
        plt.close(fig)
    return out_path


def asset_filename(title: str, kind: str = "chart", ext: str = "png") -> str:
    """生成资产文件名：时间戳保唯一，标题 slug 保可读；只含 [a-z0-9-]，天然免路径穿越。"""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or kind).lower()).strip("-")[:40] or kind
    return f"{kind}-{int(time.time() * 1000)}-{slug}.{ext}"
