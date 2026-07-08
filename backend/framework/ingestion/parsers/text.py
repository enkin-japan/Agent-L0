"""
纯文本 / 代码解析器。策略分档（见路由层）：
- 小纯文本：内联（围栏包裹）；
- 代码、大文本：懒加载——只登记路径、不倾倒正文，模型按需用 file_extract/shell 读取。
本模块提供读取原语；内联 vs 懒的判定在路由层。fail-open。
"""
from pathlib import Path
from typing import Any, Dict, Optional

# 视为「代码」的扩展名：一律走懒加载，避免整库倾倒进上下文
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cc", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".m", ".mm",
    ".scala", ".sh", ".bash", ".zsh", ".sql", ".r", ".lua", ".pl", ".dart",
    ".vue", ".html", ".css", ".scss", ".json", ".yaml", ".yml", ".toml",
    ".xml", ".ini", ".cfg", ".gradle", ".makefile", ".dockerfile",
}


def is_code(filename: str) -> bool:
    suf = Path(filename).suffix.lower()
    if suf in CODE_EXTS:
        return True
    return Path(filename).name.lower() in {"makefile", "dockerfile"}


def peek_text(path) -> Dict[str, Any]:
    """轻量探查：返回 {"lines": int, "chars": int, "sample": str}。读取失败返回零值。"""
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"lines": 0, "chars": 0, "sample": ""}
    return {"lines": data.count("\n") + 1, "chars": len(data), "sample": data[:400]}


def read_text_range(path, start: Optional[int] = None, end: Optional[int] = None,
                    *, max_chars: int = 12000) -> str:
    """
    逃生舱：按行区间读取文本/代码（start/end 为 1-based 行号，缺省=全文）。
    超上限截断。fail-open 返回字符串。
    """
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"[Text read failed: {e}]"
    total = len(lines)
    lo = (start - 1) if start else 0
    hi = end if end else total
    lo = max(0, lo)
    hi = min(total, hi)
    if lo >= hi:
        return f"[Empty range: file has {total} lines, requested [{start}, {end}]]"
    body = "\n".join(f"{lo + i + 1:>5} | {line}" for i, line in enumerate(lines[lo:hi]))
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[Truncated; narrow the line range]"
    return f"[{Path(path).name} lines {lo + 1}-{hi} of {total}]\n{body}"
