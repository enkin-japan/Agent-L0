"""render_chart：数据图表生成工具（结构化数据进、PNG 出，模型不写绘图代码）。

渲染核心在 framework/agents/chart_tools.py（与 deck 视觉路由共用）；本文件只是 MCP 包壳。
产物落盘 sessions/{id}/assets/，经 GET /api/v1/files/{session_id}/assets/{name} 出货给
前端（同源相对路径，Authorization 由 Vite/Nginx 代理注入）；工具返回一行 markdown 图片
链接，模型原样贴进答复即可展示。

会话归属经 runtime.get_current_session()（orchestrator 在工具执行前登记）。
"""
from ..base import BaseTool
from ...agents import chart_tools


class RenderChartTool(BaseTool):
    name = "render_chart"
    description = (
        "Render a data chart (bar / barh / line / pie / scatter) as an image the user will see. "
        "Give it STRUCTURED DATA ONLY — you never write plotting code. For factual/statistical "
        "charts use real numbers from the conversation or tool results; never fabricate facts. "
        "Plotting a mathematical function is NOT fabrication: compute the points yourself "
        "(via run_math if non-trivial) and pick a sensible default range when the user gives "
        "none — do not ask for data you can compute. Returns a markdown image "
        "line — copy that line into your reply EXACTLY as returned to display the chart. "
        "For bar/line: 'labels' are the category names (one per value), each series is "
        "{name, values}. For pie: one series, labels = slice names. For scatter: each series "
        "needs x and values (y) of equal length. Write the title and labels in the language "
        "the USER is conversing in (unless they ask otherwise) — do not let the data file's "
        "language leak into a chart made for the user."
    )
    requires_approval = False   # 只写本会话 assets 目录（文件名由本层生成，无路径注入面）
    is_concurrency_safe = True  # 文件名含毫秒时间戳，并发互不覆盖
    is_read_only = False        # 产生新资产 = 写操作（同参复读仍应被拦——同图重画无意义）

    parameters = {
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": list(chart_tools.CHART_TYPES),
                "description": "Chart kind.",
            },
            "title": {"type": "string", "description": "Chart title (optional but recommended)."},
            "labels": {
                "type": "array", "items": {"type": "string"},
                "description": "Category names: x-axis ticks for bar/line, slice names for pie.",
            },
            "series": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Series name (shown in legend)."},
                        "values": {"type": "array", "items": {"type": "number"},
                                   "description": "Numeric values (y-values for scatter)."},
                        "x": {"type": "array", "items": {"type": "number"},
                              "description": "X coordinates — scatter only."},
                    },
                    "required": ["values"],
                },
                "description": "One or more data series.",
            },
            "x_label": {"type": "string", "description": "X-axis label (optional)."},
            "y_label": {"type": "string", "description": "Y-axis label (optional)."},
        },
        "required": ["chart_type", "series"],
    }

    def is_available(self) -> bool:
        try:
            import matplotlib  # noqa: F401
        except Exception as e:
            print(f"[MCP-render_chart] ⚠️ matplotlib 不可用（{e}），工具不注册。")
            return False
        return True

    def execute(self, chart_type: str, series: list, labels: list = None,
                title: str = "", x_label: str = "", y_label: str = "") -> str:
        from ...agents import runtime
        session = runtime.get_current_session()
        if session is None:
            return "System Error: no active session context; render_chart must run inside a chat turn."

        try:
            spec = chart_tools.validate_spec({
                "chart_type": chart_type, "series": series, "labels": labels,
                "title": title, "x_label": x_label, "y_label": y_label,
            })
        except chart_tools.ChartSpecError as e:
            return f"Chart spec error (fix the arguments and retry): {e}"

        fname = chart_tools.asset_filename(title)
        out_path = session.session_dir / "assets" / fname
        try:
            chart_tools.render_chart(spec, out_path)
        except Exception as e:
            return f"System Error: chart rendering failed: {e}"

        url = f"/api/v1/files/{session.session_id}/assets/{fname}"
        alt = title or f"{chart_type} chart"
        return (
            "Chart rendered successfully. To show it to the user, include this exact markdown "
            f"line in your reply:\n![{alt}]({url})"
        )
