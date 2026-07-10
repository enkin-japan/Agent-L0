"""启动模型选择解析：.model-selection 指针 → config.yaml 注册表条目。

app.py（加载模型）与 scripts/fetch_model.py（devctl 前台下载）共用本函数，保证两处
解析到同一个 (repo, revision)——否则前台下好了 A、backend 却去加载 B。

设计：.model-selection（仓库根，gitignored）只存指针 {"repo": …}，revision 等真相
一律在 config.yaml 的 inference.model_registry 里查——指针文件损坏、指向注册表外
的仓库时静默回落 inference.model 默认项，永远不让一个坏指针挡住启动。
"""
import json
from typing import Optional, Tuple

from .config_loader import REPO_ROOT, load_section

SELECTION_FILE = REPO_ROOT / ".model-selection"


def resolve_selected_model() -> Tuple[str, Optional[str], str]:
    """返回 (repo_or_path, revision, source)。source ∈ {"selection", "default"}，供日志。"""
    inference = load_section("inference")
    default = (inference.get("model"), inference.get("model_revision"), "default")
    registry = inference.get("model_registry") or []
    try:
        selected = json.loads(SELECTION_FILE.read_text(encoding="utf-8")).get("repo")
    except FileNotFoundError:
        return default
    except Exception as e:
        print(f"⚠️ .model-selection 无法解析（{e}），回落默认模型")
        return default
    for entry in registry:
        if entry.get("repo") == selected:
            return entry["repo"], entry.get("revision"), "selection"
    print(f"⚠️ .model-selection 指向注册表外的 {selected!r}，回落默认模型"
          f"（注册表见 config.yaml inference.model_registry）")
    return default


def model_options(repo: Optional[str]) -> dict:
    """按 repo 取注册表条目（含 suppress_ghost_thinking 等模型级行为开关）。

    与 resolve_selected_model 解耦：开关跟着最终选中的 repo 走，无论它来自选择文件
    还是 inference.model 默认——默认模型若也在注册表内，同样拿到自己的开关。
    表外 repo / 本地路径返回 {}（一切开关取保守默认）。
    """
    inference = load_section("inference")
    for entry in inference.get("model_registry") or []:
        if entry.get("repo") == repo:
            return entry
    return {}
