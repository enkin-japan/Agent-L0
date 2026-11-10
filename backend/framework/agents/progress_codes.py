"""工具进度上报的稳定代号表（build 84：进度文案跟随客户端界面语言）。

后端写死的中文进度文案（`runtime.report_progress` / 沙箱 `_report`）在客户端里显示为
步骤详情与活动行。客户端切到日语/英语后，这些文案需要按界面语言重新渲染——于是每条
上报在原有文本之外，再附带一个稳定代号 `code`（点分小写）与扁平参数 `args`
（只含 str / int），经 `tool_progress` 事件原样带到客户端。

不变量：
- **文本逐字节不变**：`label` 由本表的中文模板代入参数渲染得到，与改动前的写死文本
  逐字节相同（backend/tests/test_progress_codes.py 以改动前的原值为 golden 锁定）。
  旧版 app、网页前端、日志看到的内容与 build 83 完全相同；`code`/`args` 只增不改。
- **中文模板是权威**：客户端（apple-app/Sources/Store/ChatMessage.swift 的
  `StepText.progressTemplates`）的 zh 模板必须与本表逐字相同，日/英译文挂在 L10n.swift
  以 zh 模板为键的表项里——同一测试文件把两边对齐，任何一侧漂移都会红。
- 占位符写法 `{name}`（str.format 语义，模板里不出现字面花括号）；参数只准 str / int。
"""
from typing import Dict, Mapping, Optional, Tuple, Union

ArgValue = Union[str, int]

# 代号 → 中文模板。新增上报点：在这里加一行，客户端 StepText.progressTemplates 与
# L10n.table（以模板为键，补 ja / en）同步各加一行；测试会逐项核对三处一致。
PROGRESS_TEMPLATES: Dict[str, str] = {
    # research_sandbox（web_research 沙箱）
    "research.thinking": "联网调查：思考中（{step}/{max}）",
    "research.searching": "联网调查：搜索「{query}」",
    "research.reading": "联网调查：阅读 {count} 个页面",
    "research.reading_rendered": "联网调查：阅读 {count} 个页面（强制渲染）",
    "research.summarizing": "联网调查：归纳结论",
    # web_read
    "web_read.fetching": "阅读网页：抓取 {count} 个页面",
    "web_read.summarizing": "阅读网页：归纳内容",
    # excel_sandbox（excel_research 沙箱；detail 字段仍是文件名等原样数据，不进模板）
    "excel.verifying": "运行交付门禁",
    "excel.visual_qa": "视觉回读",
    "excel.delivering": "交付",
    "excel.inspecting": "拆解表格结构",
    "excel.running_code": "执行代码",
    # media_download（detail 字段仍是 URL / 模式原样数据）
    "media.resolving": "正在解析链接",
    "media.downloading": "正在下载媒体",
}


def clean_args(args: Optional[Mapping]) -> Dict[str, ArgValue]:
    """只留 str 键、str / int 值（bool 是 int 的子类，排除）。进度是仪表：不合规的项静默丢弃，
    客户端缺键时会回退显示后端文本，不会崩。"""
    out: Dict[str, ArgValue] = {}
    for k, v in (args or {}).items():
        if isinstance(k, str) and isinstance(v, (str, int)) and not isinstance(v, bool):
            out[k] = v
    return out


def render(code: str, args: Optional[Mapping] = None) -> str:
    """按中文模板渲染进度文本（即事件 `label` 字段）。未登记的代号抛 KeyError——上报点
    写错代号应在测试里红，而不是在线上悄悄发出空文本。"""
    return PROGRESS_TEMPLATES[code].format(**clean_args(args))


def progress(code: str, **args: ArgValue) -> Tuple[str, str, Dict[str, ArgValue]]:
    """上报点的便捷入口：返回 (label, code, args)，label 已按中文模板渲染。"""
    cleaned = clean_args(args)
    return render(code, cleaned), code, cleaned


class _LooseDict(dict):
    """str.format_map 的宽松代入：缺键时原样留下占位符，不抛 KeyError（兜底用，不是正常路径）。"""

    def __missing__(self, key):
        return "{" + key + "}"


def safe_progress(code: str, args: Optional[Mapping] = None) \
        -> Tuple[Optional[str], Optional[str], Optional[Dict[str, ArgValue]]]:
    """渲染永不抛的入口：render() 抛异常时退回不带 code/args 的旧式上报，而不是让整条
    进度悄悄丢失。三处上报点（research_sandbox / excel_sandbox / runtime.report_progress_code）
    统一调它。

    - 代号已登记、渲染正常：返回 (渲染文本, code, 清洗后的 args)；
    - 代号已登记、渲染异常（如 args 类型不对、缺占位符）：退回 (模板宽松代入或模板原文, None, None)
      ——仍发一条看得懂的中文文本，只是不带 code/args，客户端按后端原文展示；
    - 代号本身未登记（调用点写错代号）：返回 (None, None, None)，调用方视作不上报——这是
      真正的程序 bug，应在 test_codes_are_dotted_lowercase_and_args_are_str_or_int_placeholders
      之类的代号表测试里红，不该在生产环境里现造一句看不出哪里错的文本。
    """
    tpl = PROGRESS_TEMPLATES.get(code)
    if tpl is None:
        return None, None, None
    try:
        cleaned = clean_args(args)
        return render(code, cleaned), code, cleaned
    except Exception:
        try:
            label = tpl.format_map(_LooseDict(clean_args(args)))
        except Exception:
            label = tpl
        return label, None, None
