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


#: capabilities 值域（config.yaml model_registry 各条目按 HF config.json 实测声明）
KNOWN_CAPABILITIES = ("text", "vision", "audio")


def resolve_endpoints(inference_cfg: Optional[dict] = None) -> list:
    """多端点解析（多模型协作接口准备③；Q10：统一 host + 固定端口）。

    返回有序端点列表，每项 {name, repo, revision, port, host}；[0] 恒为主端点
    "main"（由 inference.remote 现状合成——`endpoints` 未声明时返回单端点，与
    改动前行为逐字节等价，零回归）。追加端点声明在 config `inference.endpoints`：
    条目 {name, repo, port}，host 一律共享 inference.remote.host（含 env 覆盖，
    config_loader 已合并——统一 host 是 Q10 决策，不给每端点开独立 host 口子）。

    校验即拒（ValueError，装配期响亮拒启，S.3 教训——配置错误不得静默）：
    name 缺失/重复/占用 "main"；repo 不在 model_registry（方言/revision 无从查起）；
    port 非整数/与其他端点或主端点撞。

    inference_cfg 参数仅供测试注入；缺省读 config.yaml 的 inference 段。
    """
    inference = inference_cfg if inference_cfg is not None else load_section("inference")
    remote = inference.get("remote") or {}
    host = str(remote.get("host") or "").strip()
    main_port = int(remote.get("port", 14161))
    if inference_cfg is None:
        main_repo, main_rev, _ = resolve_selected_model()
    else:  # 测试注入路径：不读 .model-selection 指针，直接取默认
        main_repo, main_rev = inference.get("model"), inference.get("model_revision")
    endpoints = [{"name": "main", "repo": main_repo, "revision": main_rev,
                  "port": main_port, "host": host}]
    registry = {e.get("repo"): e for e in inference.get("model_registry") or []}
    seen_names, seen_ports = {"main"}, {main_port}
    for d in inference.get("endpoints") or []:
        name = str(d.get("name") or "").strip()
        repo = d.get("repo")
        port = d.get("port")
        if not name or name in seen_names:
            raise ValueError(f"inference.endpoints 端点名非法或重复: {name!r}（main 为保留名）")
        if repo not in registry:
            raise ValueError(f"inference.endpoints 端点 {name!r} 的 repo 不在 model_registry: "
                             f"{repo!r}（方言与 revision 以注册表为单一真相，先注册再引用）")
        if not isinstance(port, int) or port in seen_ports:
            raise ValueError(f"inference.endpoints 端点 {name!r} 的 port 非法或与其他端点冲突: "
                             f"{port!r}（Q10 约定：固定端口，14163 起顺延，14162 归 RAG 计算面）")
        endpoints.append({"name": name, "repo": repo,
                          "revision": registry[repo].get("revision"),
                          "port": port, "host": host})
        seen_names.add(name)
        seen_ports.add(port)
    return endpoints


def model_capabilities(repo: Optional[str]) -> set:
    """按 repo 取注册表声明的模态能力集合；表外/未声明回落 {"text"}（最保守）。

    多模型协作的确定性路由消费面（接口准备②）：如「输入含图像 → 只在含 vision 的
    档位里选」。值域钉死 KNOWN_CAPABILITIES（金测试校验 config 声明合法），表外值
    静默滤除、"text" 恒在——路由代码永远拿到非空且词汇受控的集合。
    """
    caps = model_options(repo).get("capabilities") or []
    if not isinstance(caps, (list, tuple)):
        caps = []
    return {c for c in caps if c in KNOWN_CAPABILITIES} | {"text"}


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
