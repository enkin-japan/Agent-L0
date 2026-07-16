"""`---` frontmatter 解析（skill 剧本与 agent 笔记共用）。

刻意只认最小子集（`key: value` 与 `key: [a, b]`）而不引 YAML 解析器：这些都是本地
可信文件，但少一个解析器就少一类解析歧义；真需要复杂结构时说明该做成代码而非文件。

**解析歧义必须炸而不是猜**：这些文件的内容会原样进模型上下文，猜错等于悄悄改提示词。

原在 `core/skill_registry.py`（R2a）；R4 的笔记要用同一格式，按 R1 的教训——
拷贝一份就会漂移——提到这里共用。
"""
from pathlib import Path
from typing import Any, Dict


def parse_frontmatter(text: str, source: Path) -> "tuple[Dict[str, Any], str]":
    """解析 `---` 包裹的 frontmatter，返回 (meta, body)。解析不了就抛 ValueError。"""
    name = getattr(source, "name", str(source))
    if not text.startswith("---"):
        raise ValueError(f"{name}: 缺少 frontmatter（首行须为 ---）")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError(f"{name}: frontmatter 未闭合（缺少结束的 ---）")
    meta: Dict[str, Any] = {}
    for line in text[3:end].strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{name}: frontmatter 行无法解析: {line!r}")
        key, _, val = line.partition(":")
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            meta[key.strip()] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = val
    body = text[end + 4:].strip()
    return meta, body
