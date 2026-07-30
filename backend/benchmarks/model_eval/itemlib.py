"""题库装载 + 鉴定纪律强制（schema 见 README.md）。

鉴定纪律是**装载期硬校验**而非文档口号：source.fit 缺失/敷衍 → 响亮 ValueError，
一道未鉴定的题都进不了执行器。宁可跑不起来，不可静默混入劣题污染画像。
"""
import json
from pathlib import Path
from typing import Any, Dict, List

DIMENSIONS = ("tool_call", "vision", "longchain", "text")
_FIT_MIN_LEN = 8   # 鉴定结论最短长度——挡"ok"式敷衍，不挡认真写的短句
_LANGS = ("zh", "en")


def _fail(path: Path, lineno: int, item_id: str, why: str) -> None:
    raise ValueError(f"题库校验失败 {path.name}:{lineno} [{item_id or '?'}]: {why}")


def validate_item(item: Dict[str, Any], path: Path = Path("?"), lineno: int = 0) -> None:
    """单题 schema 校验（响亮失败）。"""
    item_id = str(item.get("id") or "")
    if not item_id:
        _fail(path, lineno, "", "缺 id")
    if item.get("dimension") not in DIMENSIONS:
        _fail(path, lineno, item_id, f"dimension 须为 {DIMENSIONS}，得到 {item.get('dimension')!r}")
    if item.get("lang") not in _LANGS:
        _fail(path, lineno, item_id, f"lang 须为 {_LANGS}，得到 {item.get('lang')!r}")
    source = item.get("source") or {}
    if not str(source.get("origin") or "").strip():
        _fail(path, lineno, item_id, "缺 source.origin（self 或 数据集-split#原id）")
    fit = str(source.get("fit") or "").strip()
    if len(fit) < _FIT_MIN_LEN:
        _fail(path, lineno, item_id,
              f"鉴定纪律：source.fit 缺失或敷衍（{fit!r}）——写清这道题为何适合本项目目的")
    task = item.get("task") or {}
    if not str(task.get("user") or "").strip():
        _fail(path, lineno, item_id, "缺 task.user")
    if not isinstance(item.get("expect"), dict) or not item["expect"]:
        _fail(path, lineno, item_id, "缺 expect 判分规格")
    tools = task.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(
                isinstance(t, dict) and t.get("name") for t in tools):
            _fail(path, lineno, item_id, "task.tools 须为工具声明列表（每条含 name）")
    responses = task.get("tool_responses")
    if responses is not None and not isinstance(responses, dict):
        _fail(path, lineno, item_id, "task.tool_responses 须为 {工具名: 回喂内容} 映射")


def load_items(paths: List[Path]) -> List[Dict[str, Any]]:
    """装载多个 JSONL 题库文件，跨文件查重 id。空行与 # 注释行跳过。"""
    items: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"题库 JSON 解析失败 {path.name}:{lineno}: {e}") from e
            validate_item(item, path, lineno)
            if item["id"] in seen:
                _fail(path, lineno, item["id"], f"id 重复（先出现于 {seen[item['id']]}）")
            seen[item["id"]] = f"{path.name}:{lineno}"
            item["_src_dir"] = str(path.parent)   # 图片等资产的相对根
            items.append(item)
    return items


def discover_item_files(items_root: Path, dims: List[str]) -> List[Path]:
    """按维度找题库文件（items/<dim>/*.jsonl，排序保证稳定顺序）。"""
    files: List[Path] = []
    for dim in dims:
        d = items_root / dim
        if d.is_dir():
            files.extend(sorted(d.glob("*.jsonl")))
    return files
