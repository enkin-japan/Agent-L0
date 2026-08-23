"""边界目录（⑤′-2，2026-08-23）：生产单档 26b-4bit 的已知弱项清册——**谓词是代码**。

每条 `Boundary` = 可判定谓词（对一次请求的结构化上下文 `RequestFacts` 返回命中/不命中）
+ 证据指针（档案路径/数字）+ 探针集 + 状态 + 建议动作。文档面
`backend/benchmarks/model_eval/BOUNDARY.md` 逐条对应（金测试钉 id 集合一致），证据与
探针结果写在文档，谓词只活在这里——两处不会各说各话。

消费方：⑤′-3 升级策略（编排层护栏：目录谓词命中 → 触发/建议 cloud_consult）；
`scripts/boundary_harvest.py`（生产日志回流：journal 失败案例 → 目录候选）。

判定原则：
- **只看请求侧事实**（文本、是否带图、估算上下文长度、用户显式要求），不看模型输出——
  谓词要在生成**之前**就能判，才谈得上"触发升级"；
- **关键词只看指令，不看材料**：`RequestFacts.text` 是用户的问法；粘贴/摄入的长材料只进
  `attachments_chars`（长度信号）——4 万字附件里总会有"合计/total"，拿它匹配等于每个长文档
  请求都命中（评审 M3）；
- 关键词匹配是确定性规则不是语义理解：宁可窄（漏报=照旧本地作答），不可宽（误报=
  无谓出网花钱 + 审批打扰）。英文一律字母边界锚定（`prove` 不得命中 improve/approve，
  `formal` 不得命中 informal——评审 C1 实测过）；
- 角色边界：建议动作里的「云端导师」= 商用 API；Colab 与生产无关，不出现在这里。
"""
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

#: 生产单档（定档 2026-08-23 用户拍板，暂且）——目录只对它有效；换档须重评
PRODUCTION_TIER = "mlx-community/gemma-4-26b-a4b-it-4bit"

STATUSES = ("confirmed", "weak", "pending", "cleared")
ACTIONS = ("cloud_consult", "switch_tier", "tool_assist", "caveat")


@dataclass(frozen=True)
class RequestFacts:
    """一次请求的请求侧事实（编排层在生成前可得）。"""
    text: str = ""                      # 本轮用户**指令**文本（不含粘贴/摄入材料，见模块头）
    has_images: bool = False
    context_tokens: int = 0             # 估算/实测的本轮 prompt token（含历史）
    explicit_deep: bool = False         # 用户显式要求深入/严格推理（由调用方按 UI/指令判定）
    attachments_chars: int = 0          # 附件/摄入文本字符数（长材料的长度信号，不做关键词匹配）


@dataclass(frozen=True)
class Boundary:
    id: str
    title: str
    predicate: Callable[[RequestFacts], bool]
    status: str                          # confirmed / weak / pending / cleared
    action: str                          # cloud_consult / switch_tier / tool_assist / caveat
    action_target: Optional[str]         # switch_tier 的目标档；tool_assist 的工具名；cloud_consult/caveat 为 None
    evidence: str                        # 一句话证据 + 档案指针（详见 BOUNDARY.md）
    probes: str                          # 探针集指针
    rationale: str = ""
    action_gen: Optional[Dict[str, object]] = None   # 动作附带的生成参数（如换档须开思考）——
                                                     # 是动作的一半，不能只活在散文里（评审 M9）
    action_cost: str = ""                            # 换档代价的用户可读句（只写实测过的；没测就留空）
    action_hint: str = ""                            # tool_assist：命中时给模型的一次性提醒（英文，进尾注不进历史）
    action_notice: str = ""                          # caveat：收尾答复末尾给用户的一行提醒（中文；不换档不出网）

    def __post_init__(self):
        # 目录不变量用显式异常（assert 在 -O 下会被剥掉，护栏不能靠它）
        if self.status not in STATUSES:
            raise ValueError(f"{self.id}: status {self.status!r} ∉ {STATUSES}")
        if self.action not in ACTIONS:
            raise ValueError(f"{self.id}: action {self.action!r} ∉ {ACTIONS}")
        if (self.action_target is None) != (self.action in ("cloud_consult", "caveat")):
            raise ValueError(f"{self.id}: switch_tier/tool_assist 必须带目标、cloud_consult/caveat 不得带")
        if self.action == "tool_assist" and not self.action_hint:
            raise ValueError(f"{self.id}: tool_assist 必须带给模型的提示文案 action_hint")
        if self.action == "caveat" and not self.action_notice:
            raise ValueError(f"{self.id}: caveat 必须带给用户的提醒文案 action_notice")
        if self.status == "cleared" and self.predicate is not _never:
            raise ValueError(f"{self.id}: cleared 条目谓词必须是 _never（探过没复现就不许触发）")


# ---------- 谓词构件（全部纯函数、确定性；英文一律字母边界锚定） ----------

def _en(*alts: str) -> str:
    """英文备选：两侧非字母（比 \b 严——`prove` 不命中 improve/approve/proven，`formal` 不命中
    informal/formality）。"""
    return r"(?<![A-Za-z])(?:" + "|".join(alts) + r")(?![A-Za-z])"


_CHART_RE = re.compile(
    r"(?:柱状图|折线图|饼图|条形图|统计图|图表|坐标轴|横轴|纵轴|图例|读数|峰值|最大值|最小值|"
    r"(?:图中|图里|图上|这张图|该图).{0,8}(?:多少|几个|几条|数值|数字|读数|哪年|哪个月|哪一)|"
    + _en("chart", "graph", "plot", "bar chart", "line chart", "pie chart", "histogram",
          "axis", "x-axis", "y-axis", "legend", "peak value", "maximum value", "minimum value",
          "data point", "trend line")
    + r")", re.I)
# 聚合：明确要求跨多条记录算合计/均值/分组计数的问法。去掉了 `mean`（"what do you mean"）、
# 裸 `平均`/`total`（"total picture"）——评审 C2 实测误报
_AGG_RE = re.compile(
    r"(?:合计|总和|总计|求和|加总|汇总表|平均值|平均数|求平均|按.{1,6}(?:分组|分类|归类)|"
    r"分组(?:统计|汇总|计数)|统计.{0,12}(?:条数|数量|个数|次数)|各有多少|一共(?:多少|有几|几)|共计|"
    + _en("sum up", "sum of", "total of", "total amount", "total number", "total count",
          "grand total", "average of", "average per", "group by", "grouped by", "count of",
          "count per", "count by", "how many of each", "how many per", "aggregate")
    + r")", re.I)
# 深推理：显式要证明/推导/严格推理。"证明"单独出现可能是"证明文件"，故要求动词搭配
_DEEP_RE = re.compile(
    r"(?:严格证明|数学证明|形式化证明|证明(?:这|该|此|下|以下|一下|一个|上述|定理|命题|不等式|引理)|"
    r"推导(?:出|一下|这|该|此|公式|过程)|严格推理|逐步推理|分步推理|反证法|归纳法证|复杂度分析|"
    + _en("prove", "proof of", "formal proof", "formally prove", "derive", "derivation of",
          "rigorous", "rigorously", "step-by-step reasoning", "step by step reasoning",
          "prove or disprove", "show that .{1,60} holds")
    + r")", re.I)

#: 长上下文阈值（token）。08-14 哨兵矩阵（旧口径）的弱格从 32k 起；阈值压到 24k 是留安全边
#: 给估算误差（编排层的 context_tokens 常是估算值）。现条目 cleared，阈值只供重探。
LONG_CONTEXT_TOKENS = 24_000
#: 中文材料 ≈1.24 字/token（gen_longctx_struct.py 实测），用 1.3 做保守换算
_CHARS_PER_TOKEN = 1.3


def _vision_chart(f: RequestFacts) -> bool:
    return f.has_images and bool(_CHART_RE.search(f.text))


def _long_context_structured(f: RequestFacts) -> bool:
    long = (f.context_tokens >= LONG_CONTEXT_TOKENS
            or f.attachments_chars >= LONG_CONTEXT_TOKENS * _CHARS_PER_TOKEN)
    return long and bool(_STRUCT_RE.search(f.text))


_STRUCT_RE = re.compile(
    r"(?:json|表格|列表|字段|提取|抽取|逐条|每条|整理成|schema|csv|"
    + _en("table", "extract", "structured", "fields", "as json", "into json")
    + r")", re.I)


def _never(_f: RequestFacts) -> bool:
    """cleared 条目：探过没复现，谓词恒不命中（留档防重复立项，见 BOUNDARY.md）。"""
    return False


def _implicit_aggregation(f: RequestFacts) -> bool:
    return bool(_AGG_RE.search(f.text))


def _deep_reasoning(f: RequestFacts) -> bool:
    return f.explicit_deep or bool(_DEEP_RE.search(f.text))


CATALOG: Tuple[Boundary, ...] = (
    Boundary(
        id="vision_chart_numeric",
        title="图表数值读取（ChartQA 型）",
        predicate=_vision_chart,
        status="confirmed",
        action="caveat",
        action_target=None,
        evidence=("08-16 横评 vision 212 题：26b-4bit 0.84 vs qwen38 4bit 0.958 / 8bit 0.943；"
                  "逐题差分 30 题 26b 错而两档 qwen38 全对（全部 vi-chartqa），对称反向（两档 qwen 都错而 "
                  "26b 对）5 题（宽口径 10），三档同错 3 题——见 BOUNDARY.md §B1 与 "
                  "benchmarks/data/boundary-probe-20260823/vision_diff.json"),
        probes="items/vision/chartqa_hard*.jsonl 中 vision_diff.json 列出的 30 题",
        rationale="带图 + 图表/读数类问法。qwen38 为 vision 特长位（0.958），但 64G 上两档不共驻：换档 = "
                  "卸 26b/装 qwen38（丢 KV 前缀、重载半分钟）+ 之后每轮生成 45 vs 90 tps、prefill 慢 5.5×——"
                  "为一条图表问题换整个生产档不划算（用户 08-23 拍板降为 caveat）。成批看图时由用户自行切档。",
        action_notice=("图表数值读取是本档已知弱项（212 题差分：26b 错而 qwen38 全对 30 题），关键数字请对照"
                       "原图核对；若本段工作以图表读数为主，可自行 `./devctl.sh model` 切到 Qwen3.8-27B-4bit"
                       "（需重载模型、整体吞吐约减半）。"),
    ),
    Boundary(
        id="long_context_structured",
        title="长上下文 × 结构化产出（答复正文）——探过未复现",
        predicate=_never,
        status="cleared",
        action="switch_tier",
        action_target="mlx-community/gemma-4-26b-a4b-it-8bit",
        evidence=("08-14 哨兵矩阵 26b-4bit tool@32k/64k ✗ 为 JSON 声明体旧口径，DSL 迁移后翻正"
                  "（20260814-dsl-fullmatrix）；⑤′-2 探针 items/text/longctx_struct.jsonl 任务 A"
                  "（JSON 提取，8k/16k/32k/48k）26b-4bit 4/4 全过、每档 12/12 记录全中 → cleared；"
                  "见 BOUNDARY.md §B2"),
        probes="items/text/longctx_struct.jsonl 的 tx-lcs-*-json（gen_longctx_struct.py 确定性重建）",
        rationale="原谓词（上下文 ≥24k 且要结构化产出）保留在 _long_context_structured 供重探；"
                  "cleared 期间恒不命中。26b-8bit 仍是定档的长上下文×结构化静态后备位。",
    ),
    Boundary(
        id="implicit_aggregation",
        title="散落记录的隐式聚合（分组计数/求和，关思考）",
        predicate=_implicit_aggregation,
        status="confirmed",
        action="tool_assist",
        action_target="run_math",
        evidence=("⑤′-2 探针任务 B（按状态分组 → 条数 + 读数合计）：26b-4bit 关思考 0/4（8k 即挂，"
                  "条数亦错）、26b-4bit 开思考 0/4（思考通道空产出）、26b-8bit 0/4；qwen38-4bit 关思考 "
                  "0/4、开思考（预算 1024）4/4。同文任务 A 26b 12/12。**「先列后算」探针（08-23 用户"
                  "拍板先跑再修）：26b-4bit 四档 12/12 全部正确列出，但对着自己列出的清单计数/求和仍 0/4**"
                  "→ 病根是关思考下对眼前清单的算术不可靠，不是提取。故动作 = 列表交模型、算术交 run_math"
                  "沙箱（确定性、免审批、零换档零出网）。见 BOUNDARY.md §B3，档案 benchmarks/data/boundary-probe-20260823/"),
        probes="items/text/longctx_struct.jsonl 的 tx-lcs-*-table / *-table-think / *-table-list",
        rationale="任务要求对多条记录做聚合（合计/总和/平均/分组统计/条数…）。与上下文长度无关。"
                  "后备（文档记录、不作动作）：换 qwen38-4bit 并开思考 4/4，代价墙钟约 5–8×（实测 4.9–7.8×）。",
        action_hint=("For counting / summing / averaging over many records: do NOT add or count in your "
                     "head — the local model's arithmetic over a list is unreliable even when the list is "
                     "correct. Extract the records into a Python list literal and compute the group counts "
                     "and sums with the run_math tool (plain Python), then report the sandbox's numbers. "
                     "For a handful of records you may also list them one per line first; for many records "
                     "(dozens or more) put them straight into the run_math call instead of listing them in "
                     "prose. If the user asked for only the final table, still use run_math but show only "
                     "the table."),
    ),
    Boundary(
        id="deep_reasoning",
        title="深推理（证明/推导/严格多步）",
        predicate=_deep_reasoning,
        status="weak",
        action="cloud_consult",
        action_target=None,
        evidence=("无本地题库直接证据（longchain 6 题 26b-4bit 1.00 测的是工具态机不是推理深度）；"
                  "弱项判断来自定档裁决「边界外交云端导师」与 26B 缺省不开思考的生产口径"
                  "（config suppress_ghost_thinking）。状态 weak：待生产日志回流（boundary_harvest）积累案例"),
        probes="暂无本地探针；回流候选见 benchmarks/model_eval/boundary_candidates.jsonl（harvest 产物）",
        rationale="用户显式要求证明/推导/严格推理。本地任何档都不是推理特长位 → 交云端导师顾问（铁律 6：只顾问）。",
    ),
)

_BY_ID: Dict[str, Boundary] = {b.id: b for b in CATALOG}
if len(_BY_ID) != len(CATALOG):
    raise ValueError("目录 id 重复")


def by_id(bid: str) -> Boundary:
    return _BY_ID[bid]


def ids() -> List[str]:
    return [b.id for b in CATALOG]


def match(facts: RequestFacts, *, include_pending: bool = False,
          include_weak: bool = True) -> List[Boundary]:
    """按目录顺序返回命中的条目。
    - `cleared` 永不返回（探过没复现）；
    - `pending` 缺省不返回（探针未跑完的条目不得触发升级——护栏不能建在猜测上），
      include_pending 供 harvest/巡检面对表用；
    - `weak` 缺省返回，消费方按用途再筛（⑤′-3：自动档只认 confirmed，建议档可含 weak）。"""
    out = []
    for b in CATALOG:
        if b.status == "cleared":
            continue
        if b.status == "pending" and not include_pending:
            continue
        if b.status == "weak" and not include_weak:
            continue
        if b.predicate(facts):
            out.append(b)
    return out
