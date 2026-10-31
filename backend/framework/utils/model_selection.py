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

# 发现采纳态（remote 形态启动期由 app.py 写一次；build 72 复盘）：记在**本模块**而非
# app.MODEL——resolve_endpoints / model_router / RemoteBackend 都在调用期经
# resolve_selected_model 重推主模型，只改 app 全局会让路由面继续拿未采纳的缺省
# （修正评审 C1：e4b 声明 audio 而采纳的 26b 没有 → 路由静默把请求发给错能力集）。
_ADOPTED: Optional[Tuple[str, Optional[str]]] = None


def adopt_model(repo: str, revision: Optional[str]) -> None:
    """记录算力端发现采纳（进程级，一次性）。此后 resolve_selected_model 全链路
    （含 resolve_endpoints 的 main、路由的能力/画像推导）以采纳值为准。"""
    global _ADOPTED
    _ADOPTED = (repo, revision)


def resolve_selected_model() -> Tuple[str, Optional[str], str]:
    """返回 (repo_or_path, revision, source)。source ∈ {"adopted", "selection", "default"}。
    优先级：发现采纳（remote 启动期定案）> .model-selection 显式选型 > config 缺省。"""
    if _ADOPTED is not None:
        return _ADOPTED[0], _ADOPTED[1], "adopted"
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


def discover_remote_model(base_url: str, timeout: float = 5.0
                          ) -> Optional[Tuple[str, Optional[str], dict]]:
    """算力端模型**发现**（分离部署铁则：模型状态靠发现不靠抄；build 72 实机复盘）。

    remote 形态的执行端（尤其 app bundle：没有 .model-selection、config 默认键 D27 起
    为本机 Qwen3.8-27B-4bit，只管 inprocess 回落）启动时问算力端 /health 拿真身，就地采纳——分词器/
    方言/条目开关全部跟着算力端在跑的模型走，不再依赖两侧手抄一致。

    返回 (repo, revision, entry)：revision 取**本地注册表钉扎**而非活体值——分词器
    按本地钉扎解析（本地缓存对应它），且身份护栏的 revision 臂要靠「本地钉扎 vs
    活体」的差异才能抓两侧仓库不同步（修正评审 C2：曾把活体值原样喂给 expected，
    护栏比对同源恒真、臂成摆设）；活体≠钉扎时这里先响一声，护栏随后拒启指引 pull。
    返回 None = 不采纳、调用方回落本地缺省（护栏 fail-closed 兜底）：服务端未报
    身份（旧版），或 service 报了别的名字（端口被外来程序占用——不比对 service 就
    采纳，等于让任意 http 服务决定我们的分词器，修正评审 S4）。
    响亮失败：不可达/HTTP 错把 requests 异常抛给调用方；表外 repo 抛 RuntimeError
    （方言无从查起——库层不 SystemExit，进程退出策略归调用方，修正评审 S10）。
    """
    import requests
    r = requests.get(f"{base_url}/health", timeout=timeout)
    r.raise_for_status()
    try:
        ident = r.json()
    except ValueError:
        ident = {}
    if not isinstance(ident, dict):
        ident = {}
    svc = ident.get("service")
    if svc and svc != "macagent-inference":
        return None
    repo = ident.get("model_repo")
    if not repo:
        return None
    entry = model_options(repo)
    if not entry:
        raise RuntimeError(
            f"算力端在跑注册表之外的模型 {repo!r}——方言与开关无从查起，拒绝采纳。"
            f"请让算力端换回注册表内的档；或两侧同步注册该模型后重启"
            f"（dev：config.yaml inference.model_registry；app：更新到含该注册项的版本）。")
    live_rev = ident.get("model_revision")
    reg_rev = entry.get("revision")
    if live_rev and reg_rev and live_rev != reg_rev:
        print(f"⚠️ [System] 算力端实跑 revision（{live_rev[:12]}…）≠ 本地注册表钉扎"
              f"（{str(reg_rev)[:12]}…）：两侧仓库不同步，身份护栏将拒启并指引对齐")
    return repo, reg_rev, entry


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


def capabilities_of(entry: Optional[dict]) -> set:
    """注册表**条目** → 受控能力集合（model_capabilities 的条目级形态）。

    路由器（core/model_router）拿着条目直接调这里——测试注入 cfg 时不经全局
    config 读取。表外值静默滤除、"text" 恒在：消费方永远拿到非空且词汇受控的集合。
    """
    caps = (entry or {}).get("capabilities") or []
    if not isinstance(caps, (list, tuple)):
        caps = []
    return {c for c in caps if c in KNOWN_CAPABILITIES} | {"text"}


def model_capabilities(repo: Optional[str]) -> set:
    """按 repo 取注册表声明的模态能力集合；表外/未声明回落 {"text"}（最保守）。

    多模型协作的确定性路由消费面（接口准备②）：如「输入含图像 → 只在含 vision 的
    档位里选」。值域钉死 KNOWN_CAPABILITIES（金测试校验 config 声明合法）。
    """
    return capabilities_of(model_options(repo))


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


def merge_entry_speculative(inference_cfg: dict, entry: dict,
                            env_forced: bool = False) -> dict:
    """注册表条目级 speculative 覆盖全局段（D.21：qwen38 生产 MTP 转正，2026-08-17）。

    投机配对是**模型绑定事实**（qwen38 内建 MTP 头 vs gemma 无可用路线），全局
    开关无法表达"选中 qwen38 自动配对、gemma 档天然 off"——条目声明 `speculative`
    时覆盖 `inference.speculative`。两个消费方（inference/server.py 与
    AgentEngine.create）共用本函数，防两侧口径漂移。

    优先级：**env 强制（INFER_SPEC_*）> 条目 > 全局段**。env_forced=True 时条目
    整个不参与——评测臂必须能把任意档强制 off/drafter，否则基线被条目静默污染
    （13:57 事故同型）。

    本函数是**纯字典合并**——draft_model 保持注册仓 id 原样，路径解析下沉到
    MLXBackend.load 的装载线程里做（首过评审 C1/C2：在这里解析会让 remote/edge
    机白拽 15-28G 草稿权重、且同步大下载堵死 asyncio 事件循环）。draft_revision
    键随 dict 一路传到装载处按钉扎解析。返回新 dict，不改入参。
    """
    spec_entry = (entry or {}).get("speculative")
    if not spec_entry or env_forced:
        return inference_cfg
    mode = str(spec_entry.get("mode") or "off")
    if mode not in ("off", "prompt_lookup", "drafter"):
        # 白名单（与 bench_baseline --spec-mode 同口径）：条目 mode 拼错会在下游
        # 被 `== "drafter"` 静默跳过、跑成非投机——配置错误必须响亮（§S.3）
        raise ValueError(f"注册表条目 speculative.mode 只认 off|prompt_lookup|drafter，"
                         f"收到 {mode!r}（repo={entry.get('repo')!r}）")
    merged = {**(inference_cfg.get("speculative") or {}), **spec_entry}
    return {**inference_cfg, "speculative": merged}
