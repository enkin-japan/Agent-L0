"""题库装载 + 鉴定纪律强制（schema 见 README.md）。

鉴定纪律是**装载期硬校验**而非文档口号：source.fit 缺失/敷衍 → 响亮 ValueError，
一道未鉴定的题都进不了执行器。宁可跑不起来，不可静默混入劣题污染画像。
"""
import json
from pathlib import Path
from typing import Any, Dict, List

DIMENSIONS = ("tool_call", "vision", "longchain", "text")
_FIT_MIN_LEN = 8   # 鉴定结论最短长度——挡"ok"式敷衍，不挡认真写的短句
_LANGS = ("zh", "en", "ja")   # ja：2026-08-24 通用文本批加入（用户在日语环境）


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
    history = task.get("history")
    if history is not None:
        if not isinstance(history, list):
            _fail(path, lineno, item_id, "task.history 须为消息列表")
        for i, msg in enumerate(history):
            if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant") \
                    or not isinstance(msg.get("content"), str):
                _fail(path, lineno, item_id,
                      f"task.history[{i}] 须为 {{role: user|assistant, content: str}}")
    refs = task.get("tools_ref")
    if refs is not None:
        if tools is not None:
            _fail(path, lineno, item_id, "task.tools 与 task.tools_ref 二选一")
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r for r in refs):
            _fail(path, lineno, item_id, "task.tools_ref 须为非空工具名列表")


_SNAPSHOT_NAME = "real_tool_schemas.json"
_snapshot_cache: Dict[str, Dict[str, Any]] = {}


def _real_tool_snapshot(src_dir: Path) -> Dict[str, Any]:
    """题库目录旁的生产工具声明快照（items/tool_call/real_tool_schemas.json，
    由 snapshot_real_tools.py 生成）。按目录缓存。"""
    key = str(src_dir)
    if key not in _snapshot_cache:
        snap = src_dir / _SNAPSHOT_NAME
        if not snap.is_file():
            raise ValueError(f"tools_ref 需要 {snap}（运行 snapshot_real_tools.py 生成）")
        _snapshot_cache[key] = json.loads(snap.read_text(encoding="utf-8"))["tools"]
    return _snapshot_cache[key]


def resolve_tools_ref(item: Dict[str, Any], src_dir: Path) -> None:
    """把 task.tools_ref（工具名列表）就地解析成 task.tools（生产 schema 快照副本）。
    未知名字响亮失败——题目引用了不存在的工具就是坏题。"""
    refs = (item.get("task") or {}).get("tools_ref")
    if not refs:
        return
    snap = _real_tool_snapshot(src_dir)
    missing = [r for r in refs if r not in snap]
    if missing:
        raise ValueError(f"[{item['id']}] tools_ref 含快照中不存在的工具: {missing}")
    item["task"]["tools"] = [json.loads(json.dumps(snap[r])) for r in refs]
    item["task"]["tools_ref_resolved"] = item["task"].pop("tools_ref")   # 解析后二选一约束仍成立


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
            resolve_tools_ref(item, path.parent)
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
