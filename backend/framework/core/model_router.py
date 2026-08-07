"""确定性任务路由器（混合架构第一波：CHARTER §7 分期纪律「任务路由器」）。

给定一次**无状态单发生成**的任务特征（kind + 所需模态），从已声明的推理端点
（config `inference.endpoints`，Q10 多 port）中确定性选出承载模型。三条设计约束：

- **纯规则零推理**：不做「外层决策推理」（CHARTER §3 护栏是代码）。路由依据是
  model_registry 条目的 `profile` 实测画像——由 model_eval 评测 run 回填（设计
  文档纪律：路由阈值由实测校准，**不由人手填猜测值**）。
- **main 粘滞**：专科端点只有实测指标显著超出 main（ROUTING_MARGIN）才被选中；
  未画像的模型永远不因「猜它更强」被切换过去。切换有真实成本（算力端多驻一个
  实例、无基座 KV 共享），显著优势才值回票价。
- **只路由无状态单发**：主循环轮次恒走 main——会话记忆、KV 前缀连续性、审批
  闸门都锚在 main 上。铁律 6 方向：将来云端后端也只以「顾问单发」形态从这里
  进入，绝不进主循环执行工具。

词汇表（ROUTE_KINDS / PROFILE_METRICS）钉死在本模块并由金测试守卫——kind 拼错
是调用方 bug，必须响亮 ValueError；静默走默认路由 = 视觉请求落到无视觉模型，
症状比报错难查十倍（base_url_for 同款立场）。
"""
from typing import Iterable, Optional

from ..utils.model_selection import KNOWN_CAPABILITIES, capabilities_of, resolve_endpoints
from ..utils.config_loader import load_section

#: kind → 排序用画像指标（None = 只做能力过滤，恒 main 粘滞）。
#: 指标口径见 config model_registry 的 profile 注释（通过率/生成 tps 中位）。
ROUTE_KINDS = {
    "vision_qa": "vision",     # 视觉单发回读（excel 渲染页 QA 等）
    "tool_use": "tool_call",   # 工具调用格式敏感的单发
    "longchain": "longchain",  # 多步工具编排类子任务
    "chat": None,              # 普通文本单发（摘要器/沙箱总结）
    "summary": None,
}

#: profile 字典的合法键（金测试校验 config 声明；rates ∈ [0,1]，tps 为正）
#: vision_hard（2026-08-08 ChartQA 加难批）：多步读数图表 QA——路由暂无 kind
#: 引用它，先作合法画像维度登记（图表密集型任务的路由依据候选）
PROFILE_METRICS = ("tool_call", "longchain", "vision", "vision_hard", "tps")

#: 专科挑战 main 需要的最小实测优势（避免在测量噪声上来回切换）
ROUTING_MARGIN = 0.05


class RoutingUnsatisfiable(RuntimeError):
    """没有任何已声明端点满足模态需求。消费方自行决定降级（fail-soft 回 main、
    跳过该增强步骤等）——路由器本身不做静默回落。"""


def _score(entry: dict, metric: str) -> Optional[float]:
    v = ((entry or {}).get("profile") or {}).get(metric)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _tps(entry: dict) -> float:
    v = ((entry or {}).get("profile") or {}).get("tps")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def select_endpoint(kind: str, needs: Iterable[str] = (), *,
                    inference_cfg: Optional[dict] = None) -> str:
    """按任务特征选端点名（"main" 或 config endpoints 里的声明名）。

    kind ∈ ROUTE_KINDS；needs ⊆ KNOWN_CAPABILITIES（如 ("vision",)）。
    inference_cfg 仅供测试注入；缺省读 config.yaml（与 resolve_endpoints 同款约定）。
    抛 ValueError（词汇错误=调用方 bug）/ RoutingUnsatisfiable（无端点满足需求）。
    """
    if kind not in ROUTE_KINDS:
        raise ValueError(f"未知路由 kind {kind!r}（词汇表: {sorted(ROUTE_KINDS)}）")
    needs = set(needs)
    unknown = needs - set(KNOWN_CAPABILITIES)
    if unknown:
        raise ValueError(f"未知模态需求 {sorted(unknown)}（值域: {KNOWN_CAPABILITIES}）")

    cfg = inference_cfg if inference_cfg is not None else load_section("inference")
    registry = {e.get("repo"): e for e in cfg.get("model_registry") or []}
    # (声明序, 端点名, 注册表条目)——表外 repo 条目为空 dict（能力回落 {"text"}，最保守）
    eligible = [(i, ep["name"], registry.get(ep["repo"]) or {})
                for i, ep in enumerate(resolve_endpoints(inference_cfg))
                if needs <= capabilities_of(registry.get(ep["repo"]))]
    if not eligible:
        raise RoutingUnsatisfiable(
            f"无推理端点满足模态需求 {sorted(needs)}（kind={kind}）；"
            f"在 config inference.endpoints 声明具备该能力的模型，或降级处理")

    metric = ROUTE_KINDS[kind]
    names = [n for _, n, _ in eligible]
    if metric is None:
        return "main" if "main" in names else eligible[0][1]

    main_entry = next((e for _, n, e in eligible if n == "main"), None)
    challengers = [(i, n, s, _tps(e)) for i, n, e in eligible if n != "main"
                   for s in [_score(e, metric)] if s is not None]
    challengers.sort(key=lambda c: (-c[2], -c[3], c[0]))  # 实测分降序 → tps 降序 → 声明序

    if main_entry is not None:
        base = _score(main_entry, metric)
        if base is None:
            return "main"  # main 未画像：没有实测对比依据，不做切换
        if challengers and challengers[0][2] > base + ROUTING_MARGIN:
            return challengers[0][1]
        return "main"
    # main 不满足需求（如无 vision 的主模型遇视觉任务）：实测优先，其次声明序
    return challengers[0][1] if challengers else eligible[0][1]
