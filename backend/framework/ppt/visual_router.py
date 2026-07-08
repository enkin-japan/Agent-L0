"""视觉资源路由：按类型分派 spec 里的 visual_request（PPT.md 第五节的裁剪实现）。

原则：不是所有「图」都该文生图——
 · chart → 代码渲染（chart_tools/matplotlib，主题色从 deck tokens 派生），本机即时回填；
 · icon  → lucide 名字透传，前端原生渲染，不产资产、不经本层；
 · image → 【生产机接入点】本机阶段一律保持占位（content.asset=None，前端渲染
   风格化占位块）。文生图子 agent 上线后在 resolve_visuals 的 image 分支接入：
   依 visual_request.prompt 生成图片存 assets/、回填 content.asset、清 visual_request。
   详见 docs/ppt-production-todo.md。

本层由 deck_ops.set_slide 在写入前同步调用（chart 渲染 ~200ms，不值得异步化）。
"""
from pathlib import Path
from typing import Dict, List

from ..agents import chart_tools


def resolve_visuals(deck: Dict, slide: Dict, assets_dir: Path) -> List[str]:
    """解析单页所有 visual_request。返回提示列表（占位说明等）；chart spec 非法时
    抛 chart_tools.ChartSpecError（消息可直接回喂模型改参重试）。"""
    notes: List[str] = []
    tokens = deck["meta"].get("tokens", {})
    palette = tokens.get("palette", {})
    theme = {"palette": palette.get("series"),
             "bg": palette.get("bg"), "fg": palette.get("fg")}

    for block in slide["blocks"]:
        req = block.get("visual_request")
        if not req:
            continue
        if req["kind"] == "chart":
            spec = chart_tools.validate_spec(dict(req["spec"], theme=theme))
            fname = chart_tools.asset_filename(spec.get("title") or block["content"].get("alt", ""),
                                               kind="deck-chart")
            chart_tools.render_chart(spec, assets_dir / fname)
            block["content"]["asset"] = fname
            block["visual_request"] = None  # 已解析：spec 数据仍在 content 之外不再保留
        elif req["kind"] == "image":
            # 生产机接入点：文生图子 agent 在此生成并回填。本机阶段保持占位。
            notes.append(f"image \"{req.get('prompt', '')[:60]}\" rendered as a styled "
                         "placeholder (image generation arrives with the production machine).")
    return notes
