"""
外层编排循环（Orchestrator）。

两层循环的落法（结合本地推理成本做的关键取舍）：
- 不做「外层单独决策推理」的双模型调用——那会让每步推理翻倍；
- 而是统一为一个生成循环，**护栏全部是确定性代码**：
    · 预算：单步迭代上限(react_max_steps)、总迭代熔断(max_total_iterations)、
      重规划上限(replan_max)；
    · 死循环检测：同名同参的物理工具调用直接拦截；同一步（按标题）反复失败则收窄策略菜单；
    · 失败路由：代码管配额和菜单（重试/改计划/降级/问用户兜底），模型在菜单内选策略；
    · 写后验证门控（T1-4）：有写调用、之后无任何只读复查就收尾 → 推回一次要求先验证；
    · 终止：全部步骤完成 + 模型输出最终答复；取消标志在每个 token 段与迭代边界检查。
- 计划是数据：模型只能通过 update_plan / complete_step 元工具修改（见 task_state.py）；
- 元工具由本层拦截执行，不进 MCP 注册表（无审批、无探活、永远可用）；
- 工具目录两级可见性：full 模式全量 schema 常驻（工具少时更省、缓存更稳）；
  catalog 模式目录常驻 + get_tool_schema 按名取详情（工具规模大后切换，
  取回的 schema 作为 tool_response 进入追加式 history，对 KV 缓存友好）。
"""
import asyncio
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import events
from .journal import TurnJournal
from .task_state import TaskState, call_fingerprint
from ..memory.session import ChatSession
from ..memory.compactor import compact_tool_observations
from ..mcp.registry import MCPRegistry

# 会话资产的 markdown 链接（图表图片 ![..](/api/v1/files/..)、导出 PDF [..](/api/v1/files/..)）
_ASSET_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\((/api/v1/files/[^)\s]+)\)")


def _append_missing_asset_links(text: str, turn_messages: List[Dict[str, Any]]) -> str:
    """收尾兜底：本轮工具产出的用户资产链接若没被模型转贴进最终答复，原样补挂到末尾。

    render_chart / deck_export_pdf 的回执要求模型「原样复制这行 markdown」，但弱模型
    经常丢——图渲染成功、用户却什么都看不到。交付不该依赖模型的转写忠实度：
    确定性扫描本轮 tool 观察，凡 /api/v1/files/ 链接未出现在最终文本里的一律补挂。"""
    found: List[Tuple[str, str]] = []  # (markdown 全行, 资产路径)
    seen = set()
    for msg in turn_messages:
        if msg.get("role") != "tool":
            continue
        for tr in msg.get("tool_responses", []) or []:
            resp = tr.get("response")
            if not isinstance(resp, str) or "/api/v1/files/" not in resp:
                continue
            for m in _ASSET_LINK_RE.finditer(resp):
                if m.group(1) not in seen:
                    seen.add(m.group(1))
                    found.append((m.group(0), m.group(1)))
    missing = [md for md, path in found if path not in text]
    if not missing:
        return text
    return (text.rstrip() + "\n\n" + "\n".join(missing)) if text.strip() else "\n".join(missing)


# ---------- 元工具 Schema（编排层拦截，不进 MCP 注册表） ----------
UPDATE_PLAN_SCHEMA = {
    "name": "update_plan",
    "description": (
        "Create or modify the multi-step task plan. For complex tasks (ones that need multiple "
        "steps / multiple tool calls), first use op='replace' to lay out a concise plan of 2-8 "
        "steps, then execute step by step; use add/remove/reorder for small adjustments along "
        "the way, or replace again to re-plan when the situation changes substantially (limited "
        "number of re-plans). Do NOT create a plan for simple questions — just answer them."
    ),
    "parameters": {
        "properties": {
            "op": {
                "type": "string",
                "enum": ["replace", "add", "remove", "reorder", "clear"],
                "description": "replace=rebuild from steps; add=insert one step; remove=delete one step; reorder=re-sequence; clear=abandon the plan",
            },
            "steps": {"type": "array", "items": {"type": "string"},
                      "description": "Step titles for op=replace (one short sentence each)"},
            "title": {"type": "string", "description": "Step title for op=add"},
            "position": {"type": "integer", "description": "Insert position for op=add (defaults to appending at the end)"},
            "step_id": {"type": "integer", "description": "Target step id for op=remove"},
            "order": {"type": "array", "items": {"type": "integer"},
                      "description": "Complete permutation of step ids for op=reorder"},
        },
        "required": ["op"],
    },
}

COMPLETE_STEP_SCHEMA = {
    "name": "complete_step",
    "description": (
        "Mark the current step of the plan as done and advance to the next one. Call this "
        "immediately after finishing each step. `result` MUST carry the step's concrete "
        "output — the actual value(s) it produced (numbers, ids, names, urls, paths), never "
        "a vague \"done\": recorded results stay pinned in the task state and the final "
        "answer is assembled from them. After all steps are done, output the final answer "
        "to the user directly."
    ),
    "parameters": {
        "properties": {
            "result": {"type": "string",
                       "description": ("This step's concrete outcome INCLUDING the actual value(s) "
                                       "it produced, e.g. 'station 3 KEY_METRIC = 15887' — keep it "
                                       "short but never omit the values")},
        },
        "required": ["result"],
    },
}

GET_TOOL_SCHEMA_SCHEMA = {
    "name": "get_tool_schema",
    "description": (
        "Fetch the full parameter definition of tools by name. The tool catalog only lists "
        "names and short summaries; if you are unsure about a tool's parameters, fetch its "
        "complete schema with this tool before calling it."
    ),
    "parameters": {
        "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "description": "List of tool names"},
        },
        "required": ["names"],
    },
}

REMEMBER_SCHEMA = {
    "name": "remember",
    "description": (
        "Save one short note to your scratchpad — a concrete value/fact you will need in a LATER "
        "step (a url, an id, a file path, a number, a name). The scratchpad stays visible in every "
        "following step, so you never have to re-derive or re-fetch what you already found. Write "
        "down key intermediate results the moment you obtain them, instead of trusting they will "
        "stay in view. Only meaningful during a multi-step task (create a plan first)."
    ),
    "parameters": {
        "properties": {
            "note": {"type": "string",
                     "description": "One short line to remember — a concrete fact/value, not a whole paragraph"},
        },
        "required": ["note"],
    },
}

META_TOOL_NAMES = {"update_plan", "complete_step", "remember", "get_tool_schema"}

# 输出截断恢复护栏（T1-3）：撞 max_tokens 时先把上限升到 ESCALATED 重试同一步一次（多数长答复
# 一口气能写完），仍不够则多轮「接着写」续写，最多 MAX_TOKENS_RECOVERIES 次后接受半截答复。
ESCALATED_MAX_TOKENS = 12288
MAX_TOKENS_RECOVERIES = 3
# 无计划收敛提醒：无计划连打 NO_PLAN_NUDGE_ITERATIONS 次工具即提醒一次（答复或建计划
# 二选一）。原阈值 react_max_steps+2=6 实测永远够不着——事故（会话 e2348829，103 页 PDF
# 多件套任务）里模型 3-4 次无计划工具迭代后就以「承诺继续」文本收尾；而不建计划意味着
# 记事板/提前收尾推回/goal_verify 全体休眠，这条提醒是把它们唤醒的唯一入口。
NO_PLAN_NUDGE_ITERATIONS = 3
# 全思考空产出推回：整段生成都进了思考频道（典型：无收尾频道块被解析器 §2.7 路由后
# 正文为空），用户什么都看不到；静默空转只会烧到总熔断。推回一次，用尽即放行给熔断。
THINKING_ONLY_NUDGE_MAX = 1
# 提前收尾门控（T1-2）：计划未完成却吐纯文本时，每步最多推回 PREMATURE_FINISH_MAX 次，
# 用尽后放行（视为向用户提问/汇报障碍），避免与「坚持要作答」的模型死循环。
PREMATURE_FINISH_MAX = 1
# 收尾目标校验（DEFERRED §2 完整版，2026-07-12 A/B 实测触发条件 1 后上马）：
# 无计划逃逸兜底：模型无视 no_plan_nudge 全程不建计划时，步骤预算/提前收尾/审计全体
# 休眠（实测四测 662eac7d：20+ 迭代自由爬行后以"已完成全部请求"虚报收尾零拦截）。
# 无计划但整轮总迭代（跨审批段累计）≥ 此阈值的最终答复照样过收尾审计——
# "不建计划"不能成为护栏逃逸通道。短平快轮次（1-4 迭代）不受影响。
GOAL_VERIFY_NOPLAN_MIN_ITERS = 5
# 计划 ≥ GOAL_VERIFY_MIN_STEPS 步且全部完成、产出最终文本后，用一次独立小生成审计
# 「答复是否兑现原始目标」（串行、每轮最多一次），FAIL 推回自纠一次；异常/歧义 fail-open 放行。
GOAL_VERIFY_MIN_STEPS = 3
GOAL_VERIFY_MAX_TOKENS = 220
# 写后验证门控（T1-4）：本轮改过文件、之后没有任何复查就要收尾 = 把「已执行」当「已验证」
# （弱模型改一行就宣布修好的高频失败形态）。最多推回 VERIFY_NUDGE_MAX 次，用尽后放行——
# 模型坚持不验证时接受其答复，杜绝死循环。
VERIFY_NUDGE_MAX = 1
# 「写」判据只认真实的文件写工具：run_shell_command 的 is_read_only 对 cat/ls 也返回 False
# （无法静态判断命令是否改动），用它会让每个只读 shell 轮次误触发验证门控、白烧一次生成。
# 故只把这两个确定改文件的工具计作写；shell 内的 sed -i/rm 等写不被覆盖（尽力而为，误报比漏报更伤）。
_FILE_WRITE_TOOLS = {"edit_file", "write_file"}


def _last_user_text(history: List[Dict[str, Any]]) -> str:
    """从会话历史中提取最近一条用户文本（作为任务 goal）。"""
    for msg in reversed(history):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        return str(item.get("text", ""))[:500]
            elif isinstance(content, str):
                return content[:500]
    return ""


# 取代去重（T3-b）的目标键：从只读工具的参数里取「标识资源的那个字段」。同一工具对同一目标的
# 重复读取才互相取代。按优先级取第一个存在的键，通用而不硬编码具体工具名（新读工具自动适配）；
# 取不到则返回 None（该观察不参与取代，保守）。
_TARGET_KEYS = ("file_path", "path", "source_path", "app", "app_name",
                "bundle_id", "url", "query")


def _observation_target(name: str, args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    for k in _TARGET_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


class Orchestrator:
    """读状态 → 模型定下一步 → 执行 → 更新状态 的编排循环，护栏为确定性代码。"""

    def __init__(self, engine, mcp_registry: MCPRegistry, agent_cfg: Optional[Dict[str, Any]] = None):
        cfg = agent_cfg or {}
        self.engine = engine
        self.registry = mcp_registry

        # 护栏配置（config.yaml 的 agent 段，用户可调）
        self.react_max_steps = int(cfg.get("react_max_steps", 4))          # 单步内自纠预算
        self.max_total_iterations = int(cfg.get("max_total_iterations", 40))  # 一次任务总熔断
        self.replan_max = int(cfg.get("replan_max", 3))
        self.observation_max_chars = int(cfg.get("observation_max_chars", 4000))
        self.tool_timeout_seconds = int(cfg.get("tool_timeout_seconds", 120))  # 单次工具执行熔断
        self.max_parse_retries = int(cfg.get("max_parse_retries", 3))  # 工具格式解析错误的断环阈值
        self.tool_schema_mode = str(cfg.get("tool_schema_mode", "full"))   # full | catalog
        # 思考位置策略：把前端二值思考开关翻译成逐迭代策略（_thinking_for_iteration）。
        # dynamic（默认）：关思考→首轮自动思考+后续迭代关闭（思考价值集中在规划轮，
        # 消化轮多为机械转录）；开思考→全程常开。前端开关语义随之重定义（关=动态、
        # 开=常开），零前端改动。审批/挂起续跑重入 run_turn 时 iterations 归零，
        # 续跑首轮同样自动思考（重定向）。first_only：关→全不思考，开→仅首轮；
        # manual：旧语义直通。沙箱/摘要器直调 generate_once，不经本策略。
        self.thinking_mode = str(cfg.get("thinking_mode", "dynamic"))
        # dream 式笔记整理旋钮（framework/memory/dream.py 消费；app 空闲维护回合调用）
        self.notes_dream_cfg = dict(cfg.get("notes_dream") or {})
        # 依赖时效巡检旋钮（framework/maintenance/drift_patrol.py 消费；同一空闲回合调用）
        self.drift_patrol_cfg = dict(cfg.get("drift_patrol") or {})

        # 上下文压缩（Step 1：机械式工具观察压缩）：任务沉降后削旧观察，保留最近若干条原文，
        # 把 model-facing 上下文规模钉在有界范围（原始观察总量 ≤ keep_recent × 单条上限）。
        self.observation_keep_recent = int(cfg.get("observation_keep_recent", 6))
        self.observation_compact_chars = int(cfg.get("observation_compact_chars", 200))
        # 机械层观察总预算（T3-a）：全部观察总字符越过即在旧区按体量驱逐；不越则旧观察保持原文。
        self.observation_budget_chars = int(cfg.get("observation_budget_chars", 20000))

        # 摘要式压缩（Step 2，方案A）：越过阈值即把旧轮次折叠进滚动摘要。
        # 高阈值=活跃期兜底（轮次落盘后判定，昂贵路径，设高）；低阈值=空闲/freeze 前优化（免费，设低）。
        # 配置值语义 = 「稳定前缀之上」的历史预算（D4）：比较基准 last_prompt_tokens 量的
        # 是整个 prompt，而工具声明+系统提示的地板就 ≥9k——原裸阈值 8000 恒被前缀单独
        # 吃满，条件恒真，超过 keep_tail 的会话每轮都在折叠，「高阈值兜底」名存实亡。
        # 故两个阈值统一抬到 前缀估算+配置值，工具规模（D2）增长时自动跟随，无需重调参。
        self.summary_high_threshold = int(cfg.get("summary_high_threshold", 8000))
        self.summary_low_threshold = int(cfg.get("summary_low_threshold", 2000))
        self.summary_keep_tail_turns = int(cfg.get("summary_keep_tail_turns", 4))
        self.summary_idle_seconds = int(cfg.get("summary_idle_seconds", 300))  # 空闲多久进入低阈值维护
        # 长期记忆沉淀（Stage 3）：折叠旧轮时把原文沉淀进 RAG agent_memory 类目（无损承重网）。
        self.sediment_to_rag = bool(cfg.get("sediment_to_rag", True))

        # 收尾目标校验开关（DEFERRED §2 完整版）：大计划完成后一次独立审计生成，防虚报完成。
        self.goal_verify = bool(cfg.get("goal_verify", True))

        # 工具声明属于稳定前缀：启动时构建一次（注册表就是单一数据源），
        # 任务期内绝不变动，保证 KV 缓存的稳定段命中。热重扫补注册工具后经
        # refresh_tools_declaration 重建（稀有事件，接受一次 KV 前缀失配）。
        self._summary_high_base = self.summary_high_threshold
        self._summary_low_base = self.summary_low_threshold
        self.refresh_tools_declaration()

    def refresh_tools_declaration(self) -> None:
        """重建工具声明并重锚摘要阈值（__init__ 与热重扫共用）。

        前缀 token 估算：后端无 tokenizer，用 chars/4（对本声明 JSON 实测 4.02 字/token）；
        catalog 模式下声明变小，估算与阈值随之自动收紧。"""
        self._tools_decl = self._build_tools_declaration()
        self.stable_prefix_tokens_est = sum(
            len(json.dumps(t, ensure_ascii=False)) for t in self._tools_decl) // 4
        self.summary_high_threshold = self._summary_high_base + self.stable_prefix_tokens_est
        self.summary_low_threshold = self._summary_low_base + self.stable_prefix_tokens_est

    # ---------- 工具目录（两级可见性） ----------
    def _build_tools_declaration(self) -> List[Dict[str, Any]]:
        if self.tool_schema_mode == "catalog":
            catalog = [
                {"name": s.get("name"), "description": str(s.get("description", "")).split("\n")[0]}
                for s in self.registry.get_all_schemas()
            ]
            return catalog + [UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA, GET_TOOL_SCHEMA_SCHEMA]
        # full 模式：全部工具完整 schema 常驻（当前工具规模下更省 token 且缓存更稳）
        return list(self.registry.get_all_schemas()) + [UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA]

    async def prewarm_base_cache(self) -> bool:
        """T3：以真实轮次同款入参（默认系统提示 + 当前工具声明）预填基座前缀缓存。

        幂等（engine 按内容 key 判断），app 启动与空闲回合都可调；工具集经 rescan
        变化后 key 变，下次调用自动重建。自定义系统提示的会话不匹配基座——前缀匹配
        自然回退，只是享受不到，无正确性问题。"""
        from ..memory.session import DEFAULT_SYSTEM_PROMPT
        return await self.engine.prewarm_base_cache(DEFAULT_SYSTEM_PROMPT, self._tools_decl)

    async def prewarm_session_cache(self, session) -> bool:
        """T1：压缩改写上下文后的空闲预热（真实轮次同款工具声明）。"""
        return await self.engine.prewarm_session_cache(session, self._tools_decl)

    # ---------- 思考位置策略 ----------
    def _thinking_for_iteration(self, requested: bool, iteration: int) -> bool:
        """把前端二值思考开关翻译成本次迭代的实际思考态（策略详见 __init__ 注释）。"""
        if self.thinking_mode == "dynamic":
            return requested or iteration == 0
        if self.thinking_mode == "first_only":
            return requested and iteration == 0
        return requested  # manual：直通旧语义

    # ---------- 编排主循环 ----------
    async def run_turn(self, session: ChatSession, *,
                       journal: TurnJournal,
                       keep_recent_calls: bool = False,
                       compact_history: bool = True,
                       resume_unverified_write: bool = False,
                       **gen_kwargs) -> Dict[str, Any]:
        """
        执行一轮完整的编排闭环（一次用户消息 → 最终答复 / 挂起审批 / 取消 / 熔断）。
        取消、审批与故障的落盘语义与生命周期事件由 TurnRunner 统一处理,本方法只返回
        结果状态,并把**过程事件**无条件写入账本(PROTOCOL.md §6 发射点对照表):
        iteration_started / thinking_delta / tool_started / tool_progress / tool_result /
        plan_updated / step_completed / step_failed。token_delta 由调用方经 on_text_delta
        回调写账本(gen_kwargs 原样透传 generate_once)。

        journal 必传:先记账后分发,不存在"某条路径忘发事件"的结构可能。
        """
        def _emit_plan() -> None:
            t = session.task_state
            journal.emit("plan_updated",
                         {"task": t.snapshot() if (t is not None and t.plan) else None})

        # 流式回调集中在此注入(单一写入者纪律):任何调用方(TurnRunner/定时任务)
        # 都自动获得完整直播——正文逐段、思考逐段(Phase 3)、工具名预告(Phase 3)。
        gen_kwargs["on_text_delta"] = lambda t: journal.emit("token_delta", {"text": t})
        gen_kwargs["on_thinking_delta"] = lambda t: journal.emit("thinking_delta", {"text": t})
        gen_kwargs["on_tool_pending"] = lambda name: journal.emit("tool_call_pending", {"name": name})

        task = session.task_state
        if task is not None and not keep_recent_calls:
            task.recent_calls = []  # 重复调用拦截按轮生效（审批续跑保留：刚执行的调用不可立即复发）
        # 总迭代熔断的累计分母（跨审批续跑存续，见 session.carried_iterations）：
        # keep_recent_calls 仅在审批续跑时为真——新用户轮次归零，续跑沿用先前累计值，
        # 使「反复吐坏写→弹审批→续跑」的循环仍被同一个 max_total_iterations 保险丝罩住。
        if not keep_recent_calls:
            session.carried_iterations = 0
            session.carried_turn_stats = {}
        no_task_calls: List[str] = []  # 无任务态下的调用指纹（同样拦截重复）
        turn_start_idx = len(session.history)  # 本轮起点：收尾时扫描本轮工具产出的资产链接
        parse_errors: Dict[str, int] = {}  # 本轮坏调用原文 -> 次数（断环 + 升级纠错）
        nudged = False
        iterations = 0
        metrics: Dict[str, Any] = {}
        # [仪表 Phase 0] 整轮累计口径：ReAct 循环内多次生成的分解求和 + 废轮计数。
        # 每次生成后 stamp 进当前 metrics dict（该 dict 会随任一返回路径带出）。
        # 审批把一轮切成多个 run_turn 段：从 session.carried_turn_stats 播种续算，
        # 使 history.jsonl 最终落盘的 metrics 是整轮口径而非末段（实测被低估 8 倍）。
        _carried = getattr(session, "carried_turn_stats", None) or {}
        turn_breakdown: Dict[str, int] = dict(_carried.get("breakdown") or {})
        turn_gen_tokens = int(_carried.get("gen_tokens") or 0)
        turn_spec = {"drafted": int((_carried.get("spec") or {}).get("drafted") or 0),
                     "accepted": int((_carried.get("spec") or {}).get("accepted") or 0)}
        carried_parse_errors = int(_carried.get("parse_errors") or 0)

        def _stamp_turn_stats() -> None:
            metrics["token_breakdown"] = dict(turn_breakdown)
            metrics["generations"] = session.carried_iterations + iterations
            metrics["generation_tokens_turn"] = turn_gen_tokens
            metrics["parse_error_count"] = carried_parse_errors + sum(parse_errors.values())
            if turn_spec["drafted"]:
                metrics["spec_drafted"] = turn_spec["drafted"]
                metrics["spec_accepted"] = turn_spec["accepted"]
        # 有界恢复计数（T2-5 地基，承载 T1-2/T1-3；到顶即放行，杜绝新增分支造成死循环）
        max_tokens_override: Optional[int] = None   # T1-3：非 None 时本轮后续生成用升档上限
        max_tokens_recoveries = 0                    # T1-3：多轮「接着写」已用次数
        premature_finish: Dict[str, int] = {}        # T1-2：按步（标题）计的提前收尾推回次数
        # T1-4：写后验证门控状态。unverified_write=已有写调用且其后没有任何只读复查
        # （审批续跑时刚执行的批准写调用由 resume_after_approval 传入）；verify_nudges=已推回次数。
        unverified_write = resume_unverified_write
        verify_nudges = 0
        goal_verified = False        # 收尾目标校验预算（每次 run_turn 最多审计一次）
        thinking_only_nudges = 0     # 全思考空产出推回预算（THINKING_ONLY_NUDGE_MAX）
        base_max_tokens = int(gen_kwargs.get("max_tokens", 4096))
        # T3-c：一次性护栏提醒走可变尾部注入（用完即弃、不进历史、不被摘要压缩）。
        # 下一次生成消费后立即清空；由 T1-2/T1-3 分支按需填充。
        pending_tail_notes: List[str] = []

        def _transition(reason: str) -> None:
            """标注一次续跑原因（可审计：为什么又转一圈），便于日志追踪与死循环定位。"""
            print(f"[Orchestrator] transition → {reason}")

        session.reset_activity()  # 新一轮的实时步骤流从空开始

        # [上下文压缩·机械] 在本轮生成前，把已沉降的工具观察（保留最近若干条原文之外的）
        # 压成摘要：已完成任务的冗长观察就此让位，控制 model-facing 上下文规模与注意力焦点。
        # 只改内存历史（此刻全为上一轮及更早的已落盘消息，日志保留全量、不受影响）。
        # compact_history=False（审批/挂起续跑等轮中重入路径）时跳过：此时 history 尾部
        # 含本轮尚未落盘的观察，且这些 dict 与暂存轮次的 response 共享引用——在这里压缩
        # 会把压缩桩写进最终落盘日志，违背「持久化日志保留全量」；压缩推迟到下一轮轮首。
        if compact_history:
            n_compacted = compact_tool_observations(
                session.history,
                keep_recent=self.observation_keep_recent,
                digest_chars=self.observation_compact_chars,
                budget_chars=self.observation_budget_chars,
            )
            if n_compacted:
                print(f"[Orchestrator] 上下文压缩：削减 {n_compacted} 条历史工具观察")

        while session.carried_iterations + iterations < self.max_total_iterations:
            # --- 取消检查点（迭代边界；token 级检查在 engine 内） ---
            if session.cancel_requested:
                return {"status": "cancelled", "response": "任务已被用户取消。", "metrics": metrics}

            # --- 挂起检查点（迭代边界：上一步的生成与工具结果已提交进 history，
            #     此处是安全让出点，零浪费）：让出 worker，KV/task_state 留内存待恢复。
            #     恢复时路由再次进入 run_turn，从 task_state + 已提交 history 续跑。 ---
            if session.suspend_requested:
                session.suspend_requested = False
                session.set_activity("已挂起（可恢复）", phase="suspended")
                return {"status": "suspended", "response": "任务已挂起，可稍后恢复。", "metrics": metrics}

            task = session.task_state  # 元工具可能在上一迭代创建/清除任务
            task_block = (
                task.render(self.react_max_steps, self.replan_max)
                if task is not None and task.plan else None
            )
            # 实时反馈：进入推理段（轮询期间显示"思考中"）
            cur_step = task.current_step() if (task is not None and task.plan) else None
            session.set_activity(f"思考中：{cur_step['title']}" if cur_step else "思考中…")
            call_kwargs = dict(gen_kwargs)
            if max_tokens_override is not None:  # T1-3：截断升档后本轮后续生成沿用更高上限
                call_kwargs["max_tokens"] = max_tokens_override
            # 思考位置策略（见 __init__ 注释）：翻译本次迭代的实际思考态
            call_kwargs["enable_thinking"] = self._thinking_for_iteration(
                bool(call_kwargs.get("enable_thinking")), iterations)
            journal.emit("iteration_started", {
                "n": iterations + 1, "thinking": bool(call_kwargs["enable_thinking"]),
            })
            result = await self.engine.generate_once(
                session=session, tools=self._tools_decl, task_block=task_block,
                tail_notes=pending_tail_notes or None, **call_kwargs
            )
            pending_tail_notes = []  # 一次性提醒已被本次生成消费，清空（T3-c）
            fresh_metrics = result.get("metrics")
            metrics = fresh_metrics or metrics
            if metrics.get("prompt_tokens"):
                session.last_prompt_tokens = metrics["prompt_tokens"]  # 上下文规模估计（摘要阈值/空闲维护用）
            # [仪表 Phase 0] 累计本次生成的分解与投机计数，并 stamp 到 metrics。
            # 只在拿到本次生成的新 metrics 时累计——沿用旧 dict（其值已是累计口径）会双计。
            if fresh_metrics:
                for k, v in (fresh_metrics.get("token_breakdown") or {}).items():
                    turn_breakdown[k] = turn_breakdown.get(k, 0) + v
                turn_gen_tokens += fresh_metrics.get("generation_tokens", 0) or 0
                turn_spec["drafted"] += fresh_metrics.get("spec_drafted", 0) or 0
                turn_spec["accepted"] += fresh_metrics.get("spec_accepted", 0) or 0
            if result.get("cancelled"):
                _stamp_turn_stats()
                return {"status": "cancelled", "response": "任务已被用户取消。", "metrics": metrics}

            iterations += 1
            _stamp_turn_stats()
            if task is not None and task.plan and task.current_step() is not None:
                task.step_iterations += 1

            tool_calls = result["tool_calls"]
            text = result["text"]
            thoughts = result["thoughts"]

            # 思考已在生成中经 on_thinking_delta 逐段直播(Phase 3),此处不再整块补发
            # (重复累积);权威思考文本随 turn_ended.response 的 thoughts 字段对账。

            # --- 恢复分支：输出被 max_tokens 截断且非工具调用（T1-3） ---
            # 撞上限的是纯文本/思考（工具调用即便被截断也走下方 parse-error 路径），先升一档
            # 重试同一步、再多轮「接着写」，都用尽才接受半截答复。避免半截答复+下轮道歉复述。
            if result.get("truncated") and not tool_calls:
                # [失控约束调用拦截] 截断时约束采样已深度介入（大量位置受 schema 约束）却没
                # 解析出任何调用 = 模型在工具参数的字符串字段里复读失控。升档只会把 4096 的
                # 浪费放大成 12288（实测一次烧五六分钟），改按坏调用纠错：丢弃产出、反馈
                # 「用紧凑参数重发」，计入断环阈值。阈值 512 区分「调用刚起头就被截断」
                # （几十~几百约束步，升档一次即可装下，走原路径）与复读失控（数千步）。
                if int((result.get("metrics") or {}).get("constrained_steps", 0) or 0) >= 512:
                    key = "__runaway_tool_call__"
                    parse_errors[key] = parse_errors.get(key, 0) + 1
                    _stamp_turn_stats()
                    events.emit("runaway_call", session=session.session_id,
                                n=parse_errors[key],
                                constrained_steps=(result.get("metrics") or {}).get("constrained_steps"))
                    if parse_errors[key] >= self.max_parse_retries:
                        session.set_activity("已终止（工具调用参数反复失控）", phase="done")
                        events.emit("turn_abort", session=session.session_id, reason="runaway_call")
                        return {
                            "status": "warning",
                            "response": ("多次生成工具调用参数失控（超长复读），已终止本次任务。"
                                         "可以试试：①把需求拆成几件更小的事分别发起；"
                                         "②如果涉及长文本，让我分段处理。"),
                            "thoughts": ["System: repeated runaway constrained tool calls; aborted."],
                            "metrics": metrics,
                        }
                    _transition("runaway_tool_call_retry")
                    # 失控产出整体丢弃（不落 history，KV 前缀下次生成自动裁剪对齐）
                    pending_tail_notes.append(
                        "[System guardrail] Your last tool call ran away past the length limit and was "
                        "DISCARDED — it never executed. Re-issue the tool call with COMPACT arguments: "
                        "short strings, no repeated phrases, only the fields the tool needs. If the "
                        "content is long, cut it down to the essentials.")
                    continue
                if max_tokens_override is None and base_max_tokens < ESCALATED_MAX_TOKENS:
                    # 升一档重试：丢弃半截产出（不落 history），KV 前缀下次生成自动裁剪对齐
                    max_tokens_override = ESCALATED_MAX_TOKENS
                    _transition("max_tokens_escalate")
                    events.emit("truncation_recovery", session=session.session_id, kind="escalate")
                    continue
                if max_tokens_recoveries < MAX_TOKENS_RECOVERIES:
                    # 升档后仍截断：落半截产出，注入「从断点接着写」续写指令
                    max_tokens_recoveries += 1
                    _transition("max_tokens_recovery")
                    events.emit("truncation_recovery", session=session.session_id, kind="continue")
                    session.add_assistant_message(content=text, thoughts=thoughts)
                    # 续写指令走可变尾部（T3-c）：只此一轮提醒，用完即弃、不进历史、不被摘要压缩
                    pending_tail_notes.append(
                        "[System guardrail] Your output was cut off by the length limit. Continue "
                        "writing from exactly where you stopped — no apologies, no repeating what "
                        "you already wrote; split the remaining content into smaller parts if needed.")
                    continue
                # 恢复预算耗尽 → 落到下方文本分支，接受这半截作为最终答复

            # --- 动作分支：工具调用 ---
            if tool_calls:
                session.add_assistant_message(tool_calls=tool_calls, thoughts=thoughts)

                meta_calls = [c for c in tool_calls
                              if c.get("type") != "error" and c.get("name") in META_TOOL_NAMES]
                phys_calls = [c for c in tool_calls if c not in meta_calls]

                responses: List[Dict[str, Any]] = []
                for call in meta_calls:
                    mname = call.get("name")
                    # complete_step 的目标步标题须在执行前取（执行后游标已推进）
                    cur = (session.task_state.current_step()
                           if (mname == "complete_step" and session.task_state is not None
                               and session.task_state.plan) else None)
                    responses.append({
                        "name": mname,
                        "response": self._handle_meta_call(session, call),
                    })
                    # 实时反馈：计划类动作记入步骤流 + 账本
                    if mname == "update_plan":
                        session.push_activity("plan", "制定/调整计划")
                    elif mname == "complete_step":
                        session.push_activity("plan", "完成一步")
                        if cur is not None:
                            journal.emit("step_completed", {
                                "title": cur.get("title", ""),
                                "result": str((call.get("arguments") or {}).get("result", "")),
                            })
                if meta_calls:
                    _emit_plan()  # 计划/步骤已变更，写入最新任务快照
                task = session.task_state

                # [参数预检] 审批/执行前的确定性静态校验（工具自声明 validate_arguments）：
                # 注定失败的调用（如引号不配对的 shell 命令）就地打回让模型重发——绝不拿
                # 一条必败命令去打扰用户审批（实测事故 d618f0d6：mkdir …') 带语法伤过审、
                # 执行必败 zsh unmatched '、修正后再骗一次审批点击）。
                validated: List[Dict[str, Any]] = []
                for c in phys_calls:
                    verr = (self.registry.validate_call(c.get("name"), c.get("arguments", {}))
                            if c.get("type") != "error" else None)
                    if verr:
                        events.emit("tool_error", session=session.session_id,
                                    tool=c.get("name"), timeout=False,
                                    msg=str(verr)[:160], kind="precheck")
                        responses.append({"name": c.get("name"), "response": verr})
                    else:
                        validated.append(c)
                phys_calls = validated

                # [审批闸门] 物理调用中含敏感调用：先落元工具结果，再整批挂起
                # （按调用动态判定：同一 shell 工具只读命令放行、变更命令弹窗）
                approval_needed = [
                    c for c in phys_calls
                    if c.get("type") != "error"
                    and self.registry.requires_approval(c.get("name"), c.get("arguments", {}))
                ]
                if approval_needed:
                    if responses:
                        session.add_tool_message(responses)
                    session.pending_tool_calls = phys_calls
                    # 累积本次 run_turn 已消耗的迭代到会话上：审批续跑（新 run_turn）在此基础上
                    # 继续计数，总迭代熔断跨审批循环持续生效（防审批死循环绕过保险丝）。
                    session.carried_iterations += iterations
                    # 仪表随行暂存：续跑段在此基础上继续累计（整轮口径，见 run_turn 播种处）
                    session.carried_turn_stats = {
                        "breakdown": dict(turn_breakdown),
                        "gen_tokens": turn_gen_tokens,
                        "spec": dict(turn_spec),
                        "parse_errors": carried_parse_errors + sum(parse_errors.values()),
                    }
                    names = "、".join(c.get("name") for c in approval_needed)
                    session.set_activity(f"等待授权：{names}", phase="awaiting_approval")
                    print(f"[Orchestrator] 敏感工具调用挂起待审批: {[c.get('name') for c in approval_needed]}")
                    return {
                        "status": "pending_approval",
                        "pending_tools": [
                            {"name": c.get("name"), "arguments": c.get("arguments", {})}
                            for c in approval_needed
                        ],
                        "metrics": metrics,
                    }

                tracker = task.recent_calls if task is not None else no_task_calls
                responses.extend(
                    await self._execute_physical(session, phys_calls, tracker, parse_errors,
                                                 journal=journal)
                )
                _stamp_turn_stats()  # [仪表 Phase 0] 刷新废轮计数（_execute_physical 可能新增 parse 失败）
                # [验证门控 T1-4 状态跟踪] 按声明序扫描本批物理调用：改文件（edit_file/write_file）
                # 置「未验证」，其后任何非写调用（读回文件 / 跑代码 / grep 等）视为复查、清除标记。
                # 写屏障（_execute_physical）保证写后重读不会被去重拦截，故复查总能真正执行；
                # 声明序与批内实际执行序一致（串行写之前会先冲刷此前积压的只读批）。
                for c in phys_calls:
                    if c.get("type") == "error":
                        continue
                    if c.get("name") in _FILE_WRITE_TOOLS:
                        unverified_write = True
                    else:
                        unverified_write = False
                # [结构性提醒 T1-1] 在最新观察旁贴一条条件式 nudge（步骤边界/漂移/收尾验证）。
                # 与 prompt 尾部的 task_block 互补：task_block 渲染计划本身，这里给的是它覆盖不到、
                # 且贴着最近观察（更强近因）的即时提醒。触发稀疏，避免每轮膨胀与 KV 抖动。
                did_complete = any(c.get("name") == "complete_step" for c in meta_calls)
                reminder = self._structural_reminder(session.task_state, did_complete)
                if reminder:
                    responses.append({"name": "system", "response": reminder})
                if responses:
                    session.add_tool_message(responses)

                # [断环] 工具格式反复解析失败（同一坏调用重复，或本轮累计过多）→ 优雅终止，
                # 避免烧光整个迭代预算后输出无关内容（升级纠错已在 _execute_physical 中给过机会）
                if parse_errors and (
                    max(parse_errors.values()) >= self.max_parse_retries
                    or sum(parse_errors.values()) >= self.max_parse_retries + 2
                ):
                    session.set_activity("已终止（工具格式反复解析失败）", phase="done")
                    events.emit("turn_abort", session=session.session_id, reason="parse_loop")
                    return {
                        "status": "warning",
                        "response": ("多次未能解析要调用的工具格式，已终止本次任务。"
                                     "可以试试：①原样重发一次；②把需求说得更简单具体些；"
                                     "③把任务拆成几个更小的步骤分别提出。"),
                        "thoughts": ["System: repeated tool-call parse failures; aborted to avoid a loop."],
                        "metrics": metrics,
                    }

                # --- 护栏：内层预算耗尽 → 失败路由（上报外层，不是让模型硬猜） ---
                if task is not None and task.plan:
                    step = task.current_step()
                    if step is not None and task.step_iterations >= self.react_max_steps:
                        self._route_step_failure(session, task, journal)
                        _emit_plan()  # 步骤受阻、策略已调整，写入最新任务快照
                # --- 护栏：无计划却持续调用工具 → 提示收敛（一次性） ---
                elif not nudged and iterations >= NO_PLAN_NUDGE_ITERATIONS:
                    nudged = True
                    events.emit("no_plan_nudge", session=session.session_id, iterations=iterations)
                    session.history.append({
                        "role": "system",
                        "content": ("[System guardrail] You have made several tool calls without a plan "
                                    "or a conclusion. If this task needs more steps, create an explicit "
                                    "plan with update_plan NOW and follow it; otherwise answer the user "
                                    "directly."),
                    })
                continue

            # --- 完结分支：纯文本 = 给用户的答复 ---
            if text:
                # [完成门控 T1-2] 计划未完成却吐纯文本 = 可能提前偷懒收尾。每步最多推回
                # PREMATURE_FINISH_MAX 次：要么明确提出必须用户回答的问题，要么继续执行剩余步骤；
                # 用尽后放行（视为向用户提问/汇报障碍），杜绝与「坚持要作答」的模型死循环。
                task = session.task_state
                if task is not None and task.plan and not task.all_done():
                    step = task.current_step()
                    key = step["title"].strip().lower() if step else "__open__"
                    if premature_finish.get(key, 0) < PREMATURE_FINISH_MAX:
                        premature_finish[key] = premature_finish.get(key, 0) + 1
                        _transition("premature_finish_nudge")
                        events.emit("premature_finish_nudge", session=session.session_id,
                                    step=(step or {}).get("title"))
                        session.add_assistant_message(content=text, thoughts=thoughts)
                        title = step["title"] if step else "current step"
                        # 提前收尾推回走可变尾部（T3-c）：一次性提醒，用完即弃、不进历史、不被摘要压缩
                        pending_tail_notes.append(
                            f"[System guardrail] The plan still has unfinished steps (current step: \"{title}\"). "
                            "If you genuinely need information from the user that you cannot find out "
                            "yourself, ask a specific question; otherwise do not wrap up here — keep "
                            "executing the remaining steps.")
                        continue

                # [验证门控 T1-4] 收尾前的确定性检查：本轮改动过系统状态（写调用）、之后却没有
                # 任何只读复查 = 「已执行」被当成「已验证」。推回一次，要求先复查改动是否真正
                # 解决用户报告的问题。仅在正常收尾路径触发（计划未完成的纯文本 = 向用户提问/
                # 汇报障碍，不适用）；推回有界，模型坚持不验证则放行其答复。
                wrapping_up = task is None or not task.plan or task.all_done()
                if wrapping_up and unverified_write and verify_nudges < VERIFY_NUDGE_MAX:
                    verify_nudges += 1
                    _transition("verify_nudge")
                    events.emit("verify_nudge", session=session.session_id)
                    session.add_assistant_message(content=text, thoughts=thoughts)
                    # 推回走可变尾部（T3-c）：一次性提醒，用完即弃、不进历史、不被摘要压缩
                    pending_tail_notes.append(
                        "[System guardrail] You modified state this turn (write tool calls) but never "
                        "re-checked the result. Do NOT tell the user it is done/fixed yet. First verify: "
                        "re-read what you changed, trace the flow end-to-end to confirm the change is "
                        "actually reached and takes effect, and check that it explains and resolves the "
                        "user's ORIGINAL problem (not just a plausible nearby issue); run a real check if "
                        "feasible. If verification shows the fix is wrong or incomplete, keep working. "
                        "If you genuinely cannot verify, say so explicitly in your answer and tell the "
                        "user what to test — do not claim success.")
                    continue

                # [收尾目标校验 T-goal] DEFERRED §2 完整版：大计划全部完成后的最终答复，
                # 用一次独立小生成对照原始目标审计（实测：长任务两侧确定性虚报完成 → 上马）。
                # 预算 1 次；FAIL 推回自纠（同提前收尾形态），PASS/异常/预算尽 → 放行。
                plan_done_big = (task is not None and task.plan and task.all_done()
                                 and len(task.plan) >= GOAL_VERIFY_MIN_STEPS)
                # 无计划扩展：goal 取最近用户消息（审批续跑轮的"可以/同意"类短语审不出
                # 结论时由 fail-open 放行——审计器判不了就不拦）。
                noplan_substantial = ((task is None or not task.plan)
                                      and (session.carried_iterations + iterations)
                                      >= GOAL_VERIFY_NOPLAN_MIN_ITERS)
                if (self.goal_verify and not goal_verified
                        and (plan_done_big or noplan_substantial)):
                    goal_verified = True
                    goal_src = (task.goal if plan_done_big
                                else _last_user_text(session.history))
                    session.set_activity("正在核对结果与目标…")
                    ok_goal, why = await self._goal_alignment_check(goal_src, text)
                    events.emit("goal_verify", session=session.session_id,
                                passed=ok_goal, reason=why)
                    if not ok_goal:
                        _transition("goal_verify_pushback")
                        session.add_assistant_message(content=text, thoughts=thoughts)
                        pending_tail_notes.append(
                            "[System guardrail] A completion audit against the user's ORIGINAL goal "
                            f"says your answer falls short: {why} — do NOT present this as done. "
                            "Fix it now: pull the missing concrete values from the recorded step "
                            "results / scratchpad / tool observations above and deliver the complete "
                            "answer. Only if the data truly does not exist may you say exactly what "
                            "is missing and why.")
                        continue

                session.set_activity("已完成", phase="done")
                text = _append_missing_asset_links(text, session.history[turn_start_idx:])
                session.add_assistant_message(content=text, thoughts=thoughts)
                if task is not None and (task.all_done() or not task.plan):
                    # 任务完成即弃：工作过程已沉淀在本轮消息流（blackboard=消息流本身）
                    session.task_state = None
                # 未完成而输出文本 = 向用户提问/汇报障碍，任务跨轮保留，等用户答复继续
                return {"status": "success", "response": text, "thoughts": thoughts, "metrics": metrics}

            # --- 全思考空产出（T1-5）：生成不空但全进了思考频道，用户什么都看不到 ---
            # （典型：无收尾频道块被解析器 §2.7 路由进 thoughts 后正文为空。）思考留档进
            # 历史——里面往往已有分析结论，续跑可直接引用；推回一次，用尽即放行给总熔断。
            if thoughts and not tool_calls and thinking_only_nudges < THINKING_ONLY_NUDGE_MAX:
                thinking_only_nudges += 1
                _transition("thinking_only_nudge")
                events.emit("thinking_only_nudge", session=session.session_id)
                session.add_assistant_message(thoughts=thoughts)
                pending_tail_notes.append(
                    "[System guardrail] Your last output went entirely into thinking — the user "
                    "saw NOTHING. Thinking is never shown to the user. Now produce the actual "
                    "visible reply (or the next tool call); if your thinking already contains "
                    "the answer, write it out as the reply.")
                continue

            # 空输出：浪费一次迭代，靠总熔断兜底

        # --- 总迭代熔断 ---
        events.emit("turn_abort", session=session.session_id, reason="max_iterations")
        return {
            "status": "warning",
            "response": (f"任务在 {self.max_total_iterations} 次迭代内未能收敛，已终止。"
                         "可以试试：①把任务拆成几个更小的目标分别发起；②缩小范围（只处理一部分）；"
                         "③告诉我可以跳过哪些环节。"),
            "thoughts": ["System Warning: orchestration loop threshold exceeded."],
            "metrics": metrics,
        }

    # ---------- 元工具处理（计划是数据，操作有校验） ----------
    def _handle_meta_call(self, session: ChatSession, call: Dict[str, Any]) -> str:
        name = call.get("name")
        args = call.get("arguments") or {}
        if not isinstance(args, dict):
            return "Invalid arguments: expected a JSON object."

        if name == "update_plan":
            task = session.task_state
            if task is None:
                task = TaskState(goal=_last_user_text(session.history))
                session.task_state = task
            # 重规划预算：推翻已有计划才计数（首次建计划、微调不计）
            if args.get("op") == "replace" and task.plan:
                if task.replan_count >= self.replan_max:
                    events.emit("replan_budget_exhausted", session=session.session_id)
                    return (f"Replan budget exhausted ({self.replan_max}). Do NOT rebuild the plan. "
                            "Continue with the current plan, skip blocked steps, "
                            "or give the user your best final answer with what you have.")
                task.replan_count += 1
            ok, msg = task.apply_op(args)
            if not ok:
                events.emit("invalid_plan_op", session=session.session_id,
                            op=args.get("op"), msg=msg)
            return msg

        if name == "complete_step":
            task = session.task_state
            if task is None or not task.plan:
                return "No active plan. Use update_plan to create one, or just answer the user."
            ok, msg = task.complete_current(str(args.get("result", "")))
            return msg

        if name == "remember":
            task = session.task_state
            if task is None or not task.plan:
                return ("No active plan — the scratchpad carries values across the steps of a "
                        "multi-step task. Create a plan with update_plan first, or just answer.")
            ok, msg = task.remember(str(args.get("note", "")))
            return msg

        if name == "get_tool_schema":
            names = args.get("names") or []
            if not isinstance(names, list) or not names:
                return "Invalid arguments: 'names' must be a non-empty list of tool names."
            by_name = {s.get("name"): s for s in self.registry.get_all_schemas()}
            found = [by_name[n] for n in names if n in by_name]
            missing = [n for n in names if n not in by_name]
            parts = []
            if found:
                parts.append(json.dumps(found, ensure_ascii=False))
            if missing:
                parts.append(f"Unknown tools (check the catalog for exact names): {missing}")
            return "\n".join(parts)

        return f"Unknown meta tool: {name}"

    # ---------- 收尾目标校验（想法3 完整版：任务级独立审计，串行、每轮最多一次） ----------
    async def _goal_alignment_check(self, goal: str, answer: str) -> "tuple[bool, str]":
        """一次无工具小生成审计「最终答复是否兑现原始目标」。返回 (通过?, 理由)。

        成本刻意收敛：只喂 goal+答复（不带全历史），max_tokens 小、思考关闭；
        engine.generate_once 内部持有推理锁，与主循环天然串行（不违反单 worker）。
        任何异常/歧义一律 fail-open 放行——审计是安全网，绝不能反过来卡死收尾。
        """
        backend = getattr(self.engine, "backend", None)
        if backend is None:
            return True, "verifier unavailable"

        # 惰性导入：core 不静态依赖 agents（同 :989 的 agents.runtime 先例）。
        from ..agents.sandbox_runtime import EphemeralSession

        audit = EphemeralSession(
            backend,
            "You are a strict completion auditor. Judge ONLY whether the answer "
            "delivers what the goal asked for. Reply with first line exactly "
            "'VERDICT: PASS' or 'VERDICT: FAIL', then ONE short reason line. "
            "FAIL when the answer omits requested deliverables, contains "
            "placeholders like '[value]' or 'TBD', claims required data was "
            "unavailable, or answers a different question. "
            "PASS when the answer is primarily a clarifying question to the user "
            "or an honest report of a concrete obstacle — pausing to ask is "
            "legitimate; only completion CLAIMS are audited strictly.",
            f"Goal:\n{goal}\n\nFinal answer:\n{answer}\n\n"
            "Does the answer fully deliver the goal?")

        try:
            r = await self.engine.generate_once(audit, tools=None,
                                                enable_thinking=False, temperature=0.1,
                                                max_tokens=GOAL_VERIFY_MAX_TOKENS)
            verdict_text = (r.get("text") or "").strip()
        except Exception as e:
            events.emit("goal_verify_error", error=f"{type(e).__name__}: {e}")
            return True, "verifier error"
        lines = verdict_text.splitlines()
        first = lines[0].upper() if lines else ""
        passed = "FAIL" not in first    # 空输出/无法辨认 → 放行（fail-open）
        reason = (lines[1].strip() if len(lines) > 1 else first)[:300]
        return passed, reason

    # ---------- 结构性提醒（T1-1：步骤边界 / 漂移 / 收尾验证的条件式 nudge） ----------
    def _structural_reminder(self, task: Optional[TaskState], did_complete_step: bool) -> Optional[str]:
        """在最新观察旁贴的条件式提醒；无活跃计划或不满足触发条件时返回 None（保持稀疏）。"""
        if task is None or not task.plan:
            return None
        # 收尾验证 + 目标对齐（想法3 轻量）：刚完成某步且全部步骤已就绪、计划有一定规模 →
        # ①把结果对回「用户最初的目标」（弱模型跑长多步任务极易漂移到相邻的事）②别把「已执行」
        # 当「已验证」。确定性触发（all_done），零额外生成——只是最终答复前读到的一条提醒。
        if did_complete_step and task.all_done() and len(task.plan) >= 3:
            goal = (task.goal or "").strip()
            goal_ref = f" the user's ORIGINAL goal (\"{goal[:120]}\")" if goal else " the user's ORIGINAL goal"
            return ("[Reminder] All plan steps are marked done. Before giving the final answer, "
                    "re-read" + goal_ref + " and check your result actually ACHIEVES it — not just a "
                    "nearby thing you drifted into. Also confirm the key deliverables were truly "
                    "verified (\"executed\" is not \"verified\"). If it falls short, keep working "
                    "instead of wrapping up.")
        # 步骤边界：刚推进到新步 → 重新锚定注意力，别回头重做已完成的步。
        # 记事板提示自限：仅在便签仍为空时追加——一旦模型开始记录即消失，不逐步膨胀。
        if did_complete_step:
            step = task.current_step()
            if step is not None:
                hint = ("" if task.notes else
                        " If the step you just finished produced a value a later step will need "
                        "(a url, id, path, number), save it now with remember.")
                return (f"[Reminder] Now on step \"{step['title']}\". Focus on this step only; "
                        "do not go back and redo steps that are already done." + hint)
        # 漂移：当前步接近迭代上限（失败路由触发前一步）→ 提示收敛或换法
        step = task.current_step()
        if step is not None and task.step_iterations >= self.react_max_steps - 1:
            return (f"[Reminder] Step \"{step['title']}\" is close to its iteration limit. Converge "
                    "now or switch to a clearly different approach — do not keep circling.")
        return None

    # ---------- 物理工具执行（纠错 / 重复拦截 / 观察截断 / 实时反馈 / 只读并发） ----------
    async def _execute_physical(self, session: ChatSession, calls: List[Dict[str, Any]],
                                tracker: List[str],
                                parse_errors: Dict[str, int],
                                *, journal: TurnJournal
                                ) -> List[Dict[str, Any]]:
        """
        执行一批物理调用，保持响应与调用一一对应（按原序返回）。
        并发策略（T2-4）：连续的 concurrency-safe 只读调用并发执行（asyncio.gather），
        非安全/有副作用调用逐个串行；串行调用前先冲刷已积压的安全批，保证跨类别的执行顺序。
        解析错误与重复调用在本层就地生成响应、不进入执行（不占并发名额）。
        """
        by_idx: Dict[int, Dict[str, Any]] = {}
        safe_batch: List[tuple] = []  # 积压的只读安全调用：(idx, call, call_id)

        async def flush_safe_batch() -> None:
            if not safe_batch:
                return
            coros = [self._invoke_tool(session, c, cid, journal) for (_, c, cid) in safe_batch]
            results = await asyncio.gather(*coros)
            for (idx, _, _), r in zip(safe_batch, results):
                by_idx[idx] = r
            safe_batch.clear()

        for idx, call in enumerate(calls):
            # [容错闭环] 调用格式无法解析 → 升级式纠错（就地响应，不执行、不占并发名额）
            if call.get("type") == "error":
                raw = str(call.get("raw_input", "")).strip()
                parse_errors[raw] = parse_errors.get(raw, 0) + 1
                n = parse_errors[raw]
                events.emit("parse_error", session=session.session_id,
                            n=n, error=call.get("error_msg"), raw_head=raw[:120])
                if n == 1:
                    feedback = (
                        "System Error: your tool call could not be parsed.\n"
                        f"Your invalid output: {call.get('raw_input')}\n"
                        f"Parser error: {call.get('error_msg')}\n"
                        "A tool call must be VALID JSON with double quotes — the exact same format as "
                        "the tool calls you already made successfully. For example, to update the plan:\n"
                        '{"name": "update_plan", "arguments": {"op": "replace", "steps": ["step one", "step two"]}}\n'
                        "Do NOT use single quotes, a 'call:' prefix, or bare braces. Retry with correct JSON."
                    )
                else:
                    feedback = (
                        f"System Error: you emitted the exact same invalid tool call {n} times. STOP repeating it.\n"
                        "Planning is optional — do NOT retry that call. Instead, either complete the task "
                        "directly with a correctly-formatted tool call (the same JSON format that worked "
                        "for you before), or briefly tell the user what you were trying to do and why you're stuck."
                    )
                by_idx[idx] = {"name": "system", "response": feedback}
                continue

            name = call.get("name")
            args = call.get("arguments", {})

            # [死循环拦截] 同名同参调用直接打回（最常见的循环形态）。指纹按序登记并带
            # r:/w: 前缀区分只读与写：写调用执行后清掉 r: 指纹（写屏障，与 T3-b 同思路）——
            # 「读→改→再读验证」的复读放行，重复写调用（重复发信/重复删除）仍被拦。
            read_only = self.registry.is_read_only(name, args)
            fingerprint = ("r:" if read_only else "w:") + call_fingerprint(name, args)
            if fingerprint in tracker:
                events.emit("duplicate_call", session=session.session_id, tool=name)
                by_idx[idx] = {
                    "name": name,
                    "response": ("Duplicate call blocked: you already executed this exact tool call "
                                 "in this turn. Reuse the earlier result, change the arguments, "
                                 "or take a different approach."),
                }
                continue
            tracker.append(fingerprint)

            call_id = uuid.uuid4().hex[:8]
            if self.registry.is_concurrency_safe(name, args):
                safe_batch.append((idx, call, call_id))
            else:
                # 有副作用/有状态调用：先冲刷积压的只读批（保证它在这些安全调用之后执行），再串行跑
                await flush_safe_batch()
                by_idx[idx] = await self._invoke_tool(session, call, call_id, journal)
                if not read_only:
                    # 写屏障：系统状态已变，此前的只读结果不再代表现状，解禁同参复读
                    tracker[:] = [f for f in tracker if not f.startswith("r:")]

        await flush_safe_batch()
        return [by_idx[i] for i in sorted(by_idx)]

    async def _invoke_tool(self, session: ChatSession, call: Dict[str, Any], call_id: str,
                           journal: TurnJournal) -> Dict[str, Any]:
        """真正执行单个物理工具（超时熔断 + 观察截断 + 实时反馈）。可被 gather 并发调用。"""
        name = call.get("name")
        args = call.get("arguments", {})
        print(f"[Orchestrator] 执行工具: {name}")
        # 登记归属会话：会话级工具（render_chart/deck_* 等落盘 sessions/{id}/ 的）经
        # runtime.get_current_session() 取用。ContextVar + to_thread 的上下文拷贝保证
        # gather 并发的各调用互不串台。
        from ..agents import runtime as _agent_runtime
        _agent_runtime.set_current_session(session)

        # 进度钩子：长耗时工具（web_research 等）在执行中上报「正在搜索/正在阅读」。
        # 钩子可能从工具线程被调：activity 的字典赋值在 GIL 下安全（与 set_activity
        # 并发语义一致，最后写入者为准），账本 emit 必须回主循环线程，走 call_soon_threadsafe。
        loop = asyncio.get_running_loop()

        def _tool_progress(label: str, detail: str = "") -> None:
            session.set_activity(label + (f"（{detail}）" if detail else ""))
            loop.call_soon_threadsafe(
                journal.emit, "tool_progress",
                {"call_id": call_id, "label": label, "detail": detail})

        _agent_runtime.set_progress_hook(_tool_progress)
        # [事实记账] 当前步的物理执行数 +1（complete_step 时随 note 落痕：0 执行却宣称
        # "已执行"的虚报从此可见）。覆盖 run_turn 与审批续跑两条路径（都经本函数）。
        _task = session.task_state
        if _task is not None and _task.plan:
            _task.step_tool_calls += 1
        # 实时反馈：进入工具执行段（并发时以最后写入者为准，仅展示用途，无正确性影响）
        session.set_activity(f"调用工具：{name}")
        # 工具开始事件（call_id 配对 started/result，供前端渲染工具卡片）
        journal.emit("tool_started", {"call_id": call_id, "name": name, "args": args})
        # 工具级超时熔断：卡死的工具会一直占着会话锁，到点放弃等待并反馈让模型改路
        # （被放弃的线程仍在后台跑完，但不再阻塞本轮；工具自身也应有内部超时，如 shell）。
        # 长任务工具（excel_research 等多轮子 agent）可用 BaseTool.timeout_seconds
        # 覆盖全局值——工具自身的 total_timeout 硬熔断先触发，这里只是最外层兜底。
        timed_out = False
        render = None            # 渲染格（R3）：工具没给就是 None = 灰框
        timeout_s = self.registry.tool_timeout(name) or self.tool_timeout_seconds
        try:
            result_text, render = await asyncio.wait_for(
                asyncio.to_thread(self.registry.execute_tool, name, args),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            timed_out = True
            result_text = (
                f"System Error: tool '{name}' timed out after {timeout_s}s "
                "and was abandoned. Try a lighter operation, add a shorter timeout argument, "
                "or take a different approach."
            )

        # [事件回流] 工具级错误反馈（含超时）也是涣散痕迹：参数马虎/路径不存在/匹配失败等
        # 由工具自身拦下的错误，与编排层护栏一起构成完整事件流（涣散度量的全部组分入流）。
        errored = str(result_text).lstrip().startswith("System Error")
        if errored:
            events.emit("tool_error", session=session.session_id, tool=name,
                        timeout=timed_out, msg=str(result_text)[:160])

        # [观察截断] 保护每步延迟与上下文（护栏参数，见 config agent.observation_max_chars）
        # 注：截断只作用于给模型的文本。渲染载荷由工具在结构尚在时另行产出（ToolOutput），
        # 从不经过这里——故不存在"从半截文本里投影出坏图表"的可能。
        # 保头 2/3 + 尾 1/3（grok-build 评估采纳）：shell/日志类输出的错误行多在尾部，
        # 只保头会把最有用的部分砍掉；中缝标注原始体积 + 收窄取回的出路（S.3：给路）。
        if len(result_text) > self.observation_max_chars:
            total = len(result_text)
            head_n = self.observation_max_chars * 2 // 3
            tail_n = self.observation_max_chars - head_n
            result_text = (
                result_text[:head_n]
                + f"\n...[observation truncated: {total} chars total, showing first {head_n} "
                  f"and last {tail_n}; re-fetch a narrower slice (page/start/end/limit args) "
                  f"if you need the omitted middle]...\n"
                + result_text[-tail_n:]
            )
        # 实时反馈：记录一条已完成的工具步骤（结果取首行摘要）
        summary = result_text.strip().splitlines()[0] if result_text.strip() else ""
        session.push_activity("tool", name, summary)
        journal.emit("tool_result", {"call_id": call_id, "name": name,
                                     "response": result_text,
                                     "status": "timeout" if timed_out else "ok"})
        # 挂上压缩层用的内存元数据（T3-a/b）：_safe=只读可取代、_target=取代键（同工具同目标去重）。
        # 均为 `_` 前缀，落盘时被 append_log 剥离，仅存内存（与 _compacted 同规矩）。
        # 判据用 is_read_only（而非 is_concurrency_safe）：read_ui_tree 这类「只读但不可并发」
        # 的观察最大、最需要取代去重，不能因退出并发批而失去 _safe。
        obs = {"name": name, "response": result_text}
        # 渲染格：只在工具真跑成功时挂——超时/报错时没有载荷可言。
        # `render` **不带 `_` 前缀**：它要随 append_log 落盘，重载会话后还得在
        # （`_` 前缀会被 _strip_internal_meta 剥掉 → 刷新即退回灰框，且退得很安静）。
        if render and not timed_out and not errored:
            obs["render"] = render
        if not timed_out and self.registry.is_read_only(name, args):
            obs["_safe"] = True
            target = _observation_target(name, args)
            if target:
                obs["_target"] = target
        return obs

    # ---------- 失败路由（代码管配额和菜单，模型选策略） ----------
    def _route_step_failure(self, session: ChatSession, task: TaskState,
                            journal: TurnJournal) -> None:
        step = task.current_step()
        if step is None:
            return
        task.fail_current(f"Iteration budget exhausted ({self.react_max_steps} iterations without completion)")
        session.push_activity("note", "步骤受阻，调整策略", step["title"])
        journal.emit("step_failed", {
            "title": step["title"],
            "reason": f"iteration budget exhausted ({self.react_max_steps})",
        })
        title_key = step["title"].strip().lower()
        fail_count = task.step_failures.get(title_key, 0)
        events.emit("failure_route", session=session.session_id,
                    step=step["title"], fail_count=fail_count)

        options = []
        if fail_count < 2:
            options.append("A. Retry this step with a clearly different approach (re-add it via "
                           "update_plan's add op — do NOT repeat the original approach)")
        # Split：范围过大的步（如「逐页读完 103 页」）在迭代预算内结构性必败——换法重试
        # 救不了尺寸问题。实测两轮复测（d618f0d6/78126c52）均因菜单无此出路而落到「问用户」。
        options.append("B. Split: this step may simply be TOO BIG for the iteration budget — "
                       "replace it with 2-4 smaller steps via update_plan (each small enough to "
                       "finish within the budget, e.g. one page-range per step), then execute "
                       "them one by one")
        options.append("C. Modify the plan to route around this step (update_plan)")
        options.append("D. Degrade: drop this step and continue the remaining steps, or wrap up, "
                       "with the information you already have")
        options.append("E. Only if none of the above is feasible: explain the concrete obstacle "
                       "to the user and ask for instructions (output plain text)")
        if task.replan_count >= self.replan_max:
            options = [o for o in options if not o.startswith("C.")]

        session.history.append({
            "role": "system",
            "content": (
                f"[System guardrail] Step \"{step['title']}\" failed to complete after "
                f"{self.react_max_steps} consecutive iterations and is now marked failed "
                f"(cumulative failures for this step: {fail_count}). Choose your next strategy:\n"
                + "\n".join(options)
                + "\nRepeating the failed approach verbatim is forbidden."
            ),
        })

    # ---------- 审批决议恢复 ----------
    async def resume_after_approval(self, session: ChatSession, approved: bool, *,
                                    journal: TurnJournal,
                                    **gen_kwargs) -> Dict[str, Any]:
        """
        用户对挂起的敏感工具调用作出决议后恢复编排闭环：
        批准则真正执行；拒绝则反馈拒绝结果，由模型决定后续（多步任务继续按计划推进）。
        """
        pending_calls = session.pending_tool_calls or []
        session.pending_tool_calls = None

        if approved:
            task = session.task_state
            tracker = task.recent_calls if task is not None else []
            # 批准的敏感调用同样经账本发事件（修复旧实现漏发 tool_started/result 的洞）
            responses = await self._execute_physical(session, pending_calls, tracker, {},
                                                     journal=journal)
        else:
            responses = [
                {
                    "name": call.get("name", "unknown"),
                    "response": (
                        "Operation DENIED: the user rejected this operation in the system "
                        "confirmation dialog, so it was NOT executed. Do NOT retry or ask again. "
                        "Briefly acknowledge the cancellation and continue with the rest of the task "
                        "(skip or adapt the affected step)."
                    ),
                }
                for call in pending_calls if call.get("type") != "error"
            ] or [{"name": "system", "response": "Operation denied by user in the confirmation dialog."}]

        session.add_tool_message(responses)
        # 刚批准执行的调用含改文件工具 → 续跑的 run_turn 以「未验证写」状态起步（T1-4）：
        # 文件写恰是最不该「改完即宣布成功」的一类，续跑不能丢失这个标记。判据与主循环一致
        # （只认 edit_file/write_file），不用对 shell 无效的 is_read_only。
        wrote_unverified = approved and any(
            c.get("name") in _FILE_WRITE_TOOLS
            for c in pending_calls if c.get("type") != "error"
        )
        # 回到编排主循环（若再次命中敏感工具会再次挂起）。keep_recent_calls：保留刚执行的
        # 批准调用指纹，防模型在续跑首步立即原样重复该敏感调用。
        # compact_history=False：轮中重入，本轮未落盘观察不参与压缩（防压缩桩落盘）。
        return await self.run_turn(session, journal=journal,
                                   keep_recent_calls=True, compact_history=False,
                                   resume_unverified_write=wrote_unverified, **gen_kwargs)
