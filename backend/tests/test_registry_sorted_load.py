"""_load_all_tools 扫描顺序确定性金测试（AST 钉子，不实跑扫描）。

守 registry.py 里 `for file in sorted(self.tools_dir.iterdir()):`：工具的注册顺序
即 schema 列表落入系统提示词的顺序，是提示词稳定前缀的一部分（orchestrator 不再
排序，registry 原样吐给上层）。裸 `iterdir()` 的目录项顺序会随文件系统/同步方式
（git checkout、rsync、bundle 组装）漂移——顺序一变，稳定前缀整段失效，算力端
整轮重新 prefill（KV 缓存白攒，见 registry.py 内的同一段注释）。

静态 AST 钉住「遍历对象必须是 `sorted(...)`，且其唯一实参必须是
`self.tools_dir.iterdir()`」：谁若图省事把 `sorted(...)` 拆掉、改回裸 iterdir
遍历，这里当场红，不必等设备实测才发现 KV 缓存命中率骤降。
"""
import ast
from pathlib import Path

_REGISTRY = Path(__file__).resolve().parent.parent / "framework" / "mcp" / "registry.py"


def _find_load_all_tools_for_loop():
    tree = ast.parse(_REGISTRY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_load_all_tools":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.For):
                    return stmt
    return None


def test_load_all_tools_iterates_sorted_tools_dir():
    for_node = _find_load_all_tools_for_loop()
    assert for_node is not None, \
        "registry.py 的 _load_all_tools 里找不到 For 循环——函数名/结构是否已改？"

    it = for_node.iter
    assert isinstance(it, ast.Call), \
        "_load_all_tools 的遍历对象必须是 sorted(...) 调用（当前不是函数调用）"
    assert isinstance(it.func, ast.Name) and it.func.id == "sorted", \
        "扫描顺序必须由 sorted(...) 钉死，不能改回裸 iterdir()——目录项顺序会随" \
        "文件系统/同步方式（git checkout、rsync、bundle 组装）漂移，进而打掉提示词" \
        "稳定前缀与 KV 缓存"
    assert len(it.args) == 1, "sorted() 应恰好一个实参（self.tools_dir.iterdir()）"

    inner = it.args[0]
    assert isinstance(inner, ast.Call), "sorted() 的实参必须是 self.tools_dir.iterdir() 调用"
    assert (
        isinstance(inner.func, ast.Attribute) and inner.func.attr == "iterdir"
        and isinstance(inner.func.value, ast.Attribute) and inner.func.value.attr == "tools_dir"
        and isinstance(inner.func.value.value, ast.Name) and inner.func.value.value.id == "self"
    ), "sorted() 的实参必须精确是 self.tools_dir.iterdir()"
