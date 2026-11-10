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
import hashlib
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import approval_policy, context_guard, events, step_compaction
from .engine import DEFAULT_MAX_TOKENS
from .journal import TurnJournal
from .task_state import TaskState, call_fingerprint
from . import tool_disclosure
from ..memory.session import ChatSession, DEFAULT_SYSTEM_PROMPT
from ..memory.compactor import (compact_tool_observations, compact_tool_observations_detailed,
                                resolve_summary_calibration)
from ..mcp.registry import PRECHECK_LIST_MAX, MCPRegistry
from .spill import (SPILL_HANDLE_PREFIX, resolve_spill_ref, spill_handle, spill_observation,
                    truncate_observation)

# W2 步骤边界压缩不落刀的观察：工具 schema 详情（模型据此组参，压了就得再取一次）、云端咨询答复
# （出网换来的、不可廉价重取）、结构提醒（name=system，短且贴着近因）。W1 ceiling 与轮首压缩不豁免。
STEP_COMPACT_EXEMPT = frozenset({"get_tool_schema", "cloud_consult", "system"})

# 同名同参重发的拦截回执（_execute_physical 去重；D44 预检打回的调用原样重发同样用它）
DUPLICATE_CALL_BLOCKED = ("Duplicate call blocked: you already executed this exact tool call "
                          "in this turn. Reuse the earlier result, change the arguments, "
                          "or take a different approach.")
from .retrieval_evidence import (RetrievalEvidence, REPAIR_NOTE, DELIVERY_BLOCKED,
                                 is_delivery,
                                 target_key as retrieval_target,
                                 observation as retrieval_observation)

# 会话资产的 markdown 链接（图表图片 ![..](/api/v1/files/..)、导出 PDF [..](/api/v1/files/..)）
_ASSET_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\((/api/v1/files/[^)\s]+)\)")

# 方言未提供 parse_error_retry_hint 时的兜底教学段（正常路径永远取方言——真实引擎的
# 方言全部实现该方法；这里只兜 FakeEngine 之类无方言场景，故取格式中立的措辞）
_PARSE_HINT_FALLBACK = (
    "Re-issue the call in the EXACT tool-call format you were taught in the system "
    "prompt — one complete, well-formed block."
)


def parse_error_feedback(call: Dict[str, Any], n: int, format_hint: str) -> str:
    """容错闭环的回喂文案。信封（System Error 前缀 + 原文回显 + 复读升级）是编排策略；
    「正确格式长什么样」是方言事实，由 format_hint（dialect.parse_error_retry_hint()）
    提供——gemma4 教官方紧凑 DSL（§7-8 前教 JSON）、qwen35 教 XML，硬编码任何一种
    都会把另一家族带偏。
    模块级函数：评测框架（benchmarks/model_eval）复刻纠错闭环时复用同一形态。"""
    if n == 1:
        return (
            "System Error: your tool call could not be parsed.\n"
            f"Your invalid output: {call.get('raw_input')}\n"
            f"Parser error: {call.get('error_msg')}\n"
            + format_hint
        )
    return (
        f"System Error: you emitted the exact same invalid tool call {n} times. STOP repeating it.\n"
        "Planning is optional — do NOT retry that call. Instead, either complete the task "
        "directly with a correctly-formatted tool call (the same format that worked "
        "for you before), or briefly tell the user what you were trying to do and why you're stuck."
    )


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


def _cloud_degraded_scan(history: List[Dict[str, Any]]) -> Tuple[List[str], bool]:
    """本轮（自最后一条 user 消息起）cloud_consult 观察扫描 → (降级原因序列, 同轮是否有成功咨询)。

    扫历史而非轮内局部变量：cloud_consult 要审批，降级观察多半是在
    resume_after_approval 里落进历史、再重入 run_turn 的——轮内变量那时已经没了。
    只认工具名为 cloud_consult 的条目（评审 M2：read_file 翻到同款日志不算）。"""
    from ..cloud.degrade import cloud_consult_succeeded, degraded_kind   # core 不静态依赖 cloud
    start = 0
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            start = i
            break
    kinds: List[str] = []
    succeeded = False
    for msg in history[start:]:
        if msg.get("role") != "tool":
            continue
        for tr in msg.get("tool_responses", []) or []:
            k = degraded_kind(tr.get("response"), tr.get("name"))
            if k:
                kinds.append(k)
            elif cloud_consult_succeeded(tr.get("response"), tr.get("name")):
                succeeded = True
    return kinds, succeeded


from ..cloud.degrade import CLOUD_CONSULT_TOOL as _CLOUD_CONSULT_TOOL   # 常量，无重依赖


def _is_local_mutation(registry, call: Dict[str, Any]) -> bool:
    """D2「成功的变更类调用」判据：非只读工具，且**不是云端咨询**——咨询出网花钱但不改本机
    任何状态，批准一次咨询不得解除「零变更却声称已做」守门（⑤′-3 评审 M3）。不把 cloud_consult
    标成只读来达到同一效果：只读指纹会被写屏障清掉，付费重复调用就拦不住了（阶段复审 M1）。"""
    name = call.get("name")
    if name == _CLOUD_CONSULT_TOOL:
        return False
    return not registry.is_read_only(name, call.get("arguments", {}))


def _already_consulted(history: List[Dict[str, Any]]) -> bool:
    from ..cloud.upgrade import already_consulted_this_turn
    return already_consulted_this_turn(history)


def _append_upgrade_notices(text: str, metrics: Dict[str, Any], decisions: List[Any]) -> str:
    """⑤′-3 收尾：升级决策入 metrics（审计面）；tier_suggest / unavailable 追加用户提示。"""
    if not decisions:
        return text
    from ..cloud.upgrade import user_notice
    metrics["cloud_upgrade"] = [d.as_event() for d in decisions]
    notes = [n for n in (user_notice(d) for d in decisions) if n and n not in text]
    if not notes:
        return text
    return (text.rstrip() + "\n\n" + "\n".join(notes)) if text.strip() else "\n".join(notes)


def _finalize_cloud_degraded(session: ChatSession, metrics: Dict[str, Any], text: str,
                             *, append_notice: bool) -> str:
    """⑤′-1 收尾：本轮降级过 → metrics 平铺 `cloud_degraded`（审计面，所有出口都记）；
    最终答复末尾确定性追加「本地作答」说明——**仅当同轮没有成功的云端咨询**（评审 M1：
    首次超时、重试成功 = 真用了云端建议，再挂"未经云端导师"就是反向谎报）。

    不依赖模型是否听话转述（降级观察里已要求它说，但交付不能建在转写忠实度上——
    与 _append_missing_asset_links 同一路数）。已含同一句则不重复。"""
    # 落盘视图（W1）：轮内应急压缩后 history 里是桩，降级判据要看原文
    _view = getattr(session, "persistable_messages", None)
    kinds, succeeded = _cloud_degraded_scan(_view(0) if callable(_view) else session.history)
    if not kinds:
        return text
    metrics["cloud_degraded"] = list(kinds)
    metrics["cloud_consult_ok"] = bool(succeeded)
    if not append_notice or succeeded:
        return text
    from ..cloud.degrade import user_notice
    note = user_notice(kinds)
    events.emit("cloud_degraded_notice", session=session.session_id, kinds=list(kinds))
    if not note or note in text:
        return text
    return (text.rstrip() + "\n\n" + note) if text.strip() else note


# ---------- 元工具 Schema（编排层拦截，不进 MCP 注册表） ----------
UPDATE_PLAN_SCHEMA = {
    "name": "update_plan",
    "description": (
        "Create or modify the multi-step task plan. For complex tasks (ones that need multiple "
        "steps / multiple tool calls), first use op='replace' to lay out a concise plan of 2-8 "
        "steps, then execute step by step; use add/remove/reorder for small adjustments along "
        "the way, or replace again to re-plan when the situation changes substantially. "
        # D2-减法(类B):重规划上限句删——task.render 每轮渲染 "re-plans N/max",信息在场。
        "Do NOT create a plan for simple questions — just answer them."
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
        "Save one short note to your scratchpad — a concrete value/fact you will need LATER "
        "(a url, an id, a file path, a number, a name). You do not need a plan to use it: in "
        "ordinary conversation too, when a tool result gives you a value you may need again, "
        "call remember with it — raw tool results are shortened or dropped as the conversation "
        "grows, while your scratchpad is carried into the summary when older turns are folded. "
        "During a planned multi-step task the scratchpad also stays visible in every following "
        "step, so you never have to re-derive or re-fetch what you already found. Write down key "
        "intermediate results the moment you obtain them, instead of trusting they will stay in view."
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
# W1 ceiling 应急压缩 fold_mode=minimal：最多分几批折（每批折后重量）；估算一条观察折成桩后剩多少 token 时，
# 桩里首部之外的固定部分（「…[Observation compacted; originally N chars」+ 线索 ≤240 字 + 句柄行）的估值
# （fallback_estimate 口径估值，与正文一并乘密度——桩里的线索 ≤240 字抽自被折正文，密度与正文相同）
CEILING_FOLD_MAX_PASSES = 8
CEILING_FOLD_STUB_OVERHEAD_EST = 80
# ceiling 应急压缩的保护窗（全 history 观察倒数，两种 fold_mode 同）
CEILING_KEEP_RECENT = 2
# 「写」判据只认真实的文件写工具：run_shell_command 的 is_read_only 对 cat/ls 也返回 False
# （无法静态判断命令是否改动），用它会让每个只读 shell 轮次误触发验证门控、白烧一次生成。
# 故只把这两个确定改文件的工具计作写；shell 内的 sed -i/rm 等写不被覆盖（尽力而为，误报比漏报更伤）。
_FILE_WRITE_TOOLS = {"edit_file", "write_file"}
# 纯内容文件：没有可运行的「真检查」，写工具的成功回执本身就是验证。写这类文件不武装 T1-4
# 门控——2026-11-07 P2 批 lc-hard-004/008 实案：门控推回逼模型逐文件回读刚写的 .md，每 run
# 白多 4 步、步数超限判败（docs/plans/2026-11-07-verify-nudge-reread-spec.md）。无扩展名按代码算。
_RECEIPT_IS_VERIFICATION_EXTS = {".md", ".txt", ".log", ".csv", ".tsv", ".rst"}
# 例外：扩展名虽是 .txt 但本质是依赖 / 构建清单（requirements*.txt、constraints*.txt、CMakeLists.txt），
# 改了有真检查可跑（装依赖、构建），仍按代码武装（一审 m4）。
_CODE_LIKE_TXT = re.compile(r"^(?:requirements|constraints)[^/\\]*\.txt$|^CMakeLists\.txt$", re.IGNORECASE)
#: T1-4 武装判据的规则版本：knee harness 记进每行的口径键并恒比，判据再变即拒与旧批混跑 / 续跑
#: （与 stub_clue / mock_merge_rule 同一惯例；2026-11-07 之前的批缺键 = 任何写都武装）。
VERIFY_GATE_RULE = "prose-neutral-v1"

# ── D2 谎报围堵（#84，2026-08-10 26B 档实案）────────────────────────────────
# 失败判据：工具层显式错误，或 shell 响应头部的非零退出码。只认硬信号——语义级
# 失败（"没找到"之类正常结果）不算，误报会把正常轮次推进无谓的自纠循环。
# 判据的**单一入口是 `_obs_failed`**（结构化事实优先），本正则只是它的回落层。
#
# 两处修正（OSS 扫描 §6.1/§6.2，2026-08-17）——上面这句「只认硬信号」原先并未兑现：
# ① 去掉 `re.M`：两个锚点必须是「整段开头」。带 re.M 时匹配的是任意一行的行首，
#    read_file 读日志、tail/grep 命中含 `System Error:` 或 `exit_code: N` 的行，
#    都会把 unresolved_failure 误置位 → 收尾白烧一次生成。
# ② 负向断言排除良性非零码：grep 无命中 / diff 有差异 / test 条件为假的 exit 1 是
#    正常否定答案，由 `shell_exec._is_benign_exit1` 就地判定并在首行打标
#    （标记字面量 `mcp/tools/shell_exec.py:_BENIGN_EXIT1_MARK`，两侧注释互指，
#    跨模块契约由 test_fabrication_guardrails.py 钉死）。不打标的非零码照旧算失败。
# `\d+\b` 里的 `\b` 不可省：没有它，多位退出码会靠回溯绕过负向断言——`exit_code: 12
# (benign…)` 里 `\d+` 退让成 "1"、断言只看到 "2" 便放行，整条判成失败。今天走不到
# （标记只在 returncode==1 时打），但第二个良性码一旦出现就是静默陷阱。
_FAILED_RESP_RE = re.compile(r"^System Error:|^exit_code: (?!0\b)\d+\b(?! \(benign\b)")
# 动作声明判据：收尾文本声称完成了变更类动作（创建/安装/启动/写入…，中英）。
# 只在「本轮零成功变更调用」时参与判定；推回文案允许模型改写为"此前完成/建议你执行"，
# 改写后不再命中 = 自愈放行（防误伤对历史工作的转述）。
_ACTION_CLAIM_RE = re.compile(
    r"已(?:经)?(?:成功)?(?:为您|为你|帮您|帮你)?(?:成功)?(?:在后台)?"
    r"(?:创建|新建|建好|安装|装好|启动|拉起|运行|执行|写入|保存|部署|删除|清理|修改|配置)"
    r"|(?:I have|I've)\s+(?:successfully\s+)?"
    r"(?:created|installed|started|launched|executed|written|saved|deployed|deleted|modified|configured)",
)


def _resp_failed(resp: Any) -> bool:
    return bool(_FAILED_RESP_RE.search(str(resp or "")))


def _obs_failed(obs: Any) -> bool:
    """失败判据的单一入口：优先读物理执行层挂的结构化事实 `_failed`。

    `_failed` 由 `_invoke_tool` 单点判定并随观察挂上（在观察截断**之前**算，且能纳入
    只有那一层知道的事实——超时熔断）。收尾护栏读它，而不是把响应文本再解析一遍：
    「这次调用失败了没有」是执行层的事实，不是文本模式匹配的推断结果。

    缺键只有一种活路径：`_execute_physical` 就地生成的「Duplicate call blocked」响应
    （没真执行，本就不算物理失败，回落正则同样判 False）。解析错误响应虽也缺键，但两个
    消费点都在调用本函数**之前**按 `type == "error"` 跳过了它；会话重载后的历史观察
    （`_` 前缀不落盘）根本不流经这两个消费点——不要据此以为存在读旧观察的消费者。
    """
    if isinstance(obs, dict) and "_failed" in obs:
        return bool(obs["_failed"])
    return _resp_failed(obs.get("response") if isinstance(obs, dict) else obs)


def _arms_verify_gate(call: Dict[str, Any], obs: Any) -> bool:
    """T1-4 写后验证门控的武装判据（单一入口：主循环扫描与审批续跑共用，判据不得分叉）。

    只有**成功**写到**非纯内容文件**的 edit_file / write_file 才算「未验证写」：
    C4 拦截的调用没真执行；失败写什么都没改（归 D2 失败吞没门控管）；纯内容文件
    （`_RECEIPT_IS_VERIFICATION_EXTS`）没有可运行的真检查，成功回执本身就是验证——
    2026-11-07 P2 批实案：对这些写也推回，模型只会逐文件回读刚写的内容、白烧步数。
    """
    if call.get("name") not in _FILE_WRITE_TOOLS:
        return False
    if isinstance(obs, dict) and obs.get("_blocked"):
        return False
    if _obs_failed(obs):
        return False
    path = str((call.get("arguments") or {}).get("path") or "")
    if _CODE_LIKE_TXT.match(os.path.basename(path)):
        return True
    return os.path.splitext(path)[1].lower() not in _RECEIPT_IS_VERIFICATION_EXTS


def _last_user_text(history: List[Dict[str, Any]], limit: Optional[int] = 500) -> str:
    """从会话历史中提取最近一条用户文本（作为任务 goal；limit=None 取原文全文）。"""
    for msg in reversed(history):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        return str(item.get("text", ""))[:limit]
            elif isinstance(content, str):
                return content[:limit]
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

    def __init__(self, engine, mcp_registry: MCPRegistry, agent_cfg: Optional[Dict[str, Any]] = None,
                 *, model_options: Optional[Dict[str, Any]] = None):
        """model_options：配置选定的主模型注册表条目（app.py 的 MODEL_OPTIONS）；只读其
        `tool_schema_mode`（D29）。不传 = 只看 agent 段全局缺省。"""
        cfg = agent_cfg or {}
        self.engine = engine
        self.registry = mcp_registry
        # ⑤′-3 升级策略：cloud 段（config.yaml）或测试注入 cfg["cloud"]。mode 非法 → 装配期响亮拒
        from ..cloud.upgrade import UpgradeConfig   # core 不静态依赖 cloud
        if "cloud" in cfg:
            self.cloud_cfg = dict(cfg.get("cloud") or {})
        else:
            from ..utils.config_loader import load_section   # 读不到 = 配置坏了，响亮（不静默关护栏）
            self.cloud_cfg = dict(load_section("cloud") or {})
        self.upgrade_cfg = UpgradeConfig.from_cloud_section(self.cloud_cfg)

        # 护栏配置（config.yaml 的 agent 段，用户可调）
        self.react_max_steps = int(cfg.get("react_max_steps", 4))          # 单步内自纠预算
        self.max_total_iterations = int(cfg.get("max_total_iterations", 40))  # 一次任务总熔断
        # [F6] 子沙箱生成的独立保险丝:主熔断守编排失控,这条守调研失控（一次
        # excel_research ≈16 次生成,计进主分母会把研究型轮次预算压掉一个量级）
        self.max_sub_generations = int(cfg.get("max_sub_generations", 80))
        self.replan_max = int(cfg.get("replan_max", 3))
        self.observation_max_chars = int(cfg.get("observation_max_chars", 4000))
        # ⚠️ 回落值必须与 `mcp/tools/shell_exec.py` 的 `_OUTER_BUDGET_FALLBACK_S` 逐字
        # 相同：那边按本值反推 shell 命令的内层上限，两侧不一致时孤儿窗口会悄悄回来
        # （曾是 120 vs 180 = 45s 窗口）。跨模块契约由
        # `test_shell_exec_timeout_clamp.py` 钉死。
        # 单次工具执行熔断。畸形配置防御与 shell_exec._derive_outer_budget 同一纪律
        # (阶段复审 L10):bool 是 int 子类,`tool_timeout_seconds: true` 会把外层预算
        # 塌成 1s,而 shell_exec 侧防住了自己 → 内层 165s > 外层 1s,孤儿窗口反向回归
        _tts = cfg.get("tool_timeout_seconds", 180)
        if isinstance(_tts, bool) or not isinstance(_tts, (int, float)) or int(_tts) <= 0:
            _tts = 180
        self.tool_timeout_seconds = int(_tts)
        self.max_parse_retries = int(cfg.get("max_parse_retries", 3))  # 工具格式解析错误的断环阈值
        # 工具披露（W-2，D29/D31）：模式 = 注册表条目 > agent 段 > "full"，非法值拒启。
        # 模式跟**配置选定的主模型条目**走，构造期定案：core/model_router 的专科切换
        # （同一编排器把某轮派给别的引擎）**不改**披露——工具声明紧跟 system，换披露形态
        # = 前缀缓存整段失效，前缀稳定优先于专科模型的口味。
        self.tool_schema_mode = tool_disclosure.resolve_tool_schema_mode(model_options, cfg)
        # 任务期路由（只在 catalog 模式生效）：top_k 非法拒启；core 名字未注册的告警跳过
        # （rag_admin 等按探活出现，不能因此拒启），见 refresh_tools_declaration。
        self.tool_routing_top_k, self._core_cfg = tool_disclosure.resolve_tool_routing(cfg)
        self._core_warned: set = set()
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
        # W4（D4 / D5）摘要校准：生成上限 + 每 K 次折叠一次重写（0 = 永不）。不合法即 ValueError
        # （与 context_guard 同口径：装配失败，不静默回落）。两条摘要触发路径（routes 活跃 /
        # app 空闲）都从这里取。
        self.summary_max_tokens, self.summary_rewrite_every = resolve_summary_calibration(cfg)
        # 长期记忆沉淀（Stage 3）：折叠旧轮时把原文沉淀进 RAG agent_memory 类目（无损承重网）。
        self.sediment_to_rag = bool(cfg.get("sediment_to_rag", True))

        # 收尾目标校验开关（DEFERRED §2 完整版）：大计划完成后一次独立审计生成，防虚报完成。
        self.goal_verify = bool(cfg.get("goal_verify", True))

        # 上下文上限护栏（W1）：配置在构造期校验（未知键/非正整数/soft ≥ hard 即 ValueError——
        # inprocess 为启动失败；remote 为计算装配失败并退避重试，数据服务照常）；
        # 上限要等引擎装载完才知道（remote 取算力端 /health），由 bind_context_limit 绑定，
        # 装配后的算力端探活（app._probe_dependencies）发现上限变化时重绑。
        # 只守主循环生成；goal-verify / 沙箱 / 摘要等子生成不守（残留由方案记档）。
        self.context_guard = context_guard.resolve_context_guard(cfg)
        self.context_limits: Optional[context_guard.ContextLimits] = None
        self._guard_unbound_warned = False
        # 轮内步骤边界压缩（W2 / D37，core/step_compaction.py）：配置同样构造期失败关闭。
        self.step_compaction = step_compaction.resolve_step_compaction(cfg)

        # 工具声明属于稳定前缀（注册表就是单一数据源）。full 模式：启动时构建一次，
        # 此后恒定。catalog 模式（W-2 / D31）：基座声明（元工具 + core 完整、其余目录行）
        # 同样启动时构建一次；任务建立时按词法路由选出的 top_k 件另以完整 schema 入前缀，
        # 该集合**任务期内冻结**（存在 TaskState.routed_tools，同任务后续轮不重算），任务结束
        # 后仍由会话级 last_routed_tools 保留，只在下一个任务建立时被替换——每个任务至多一次
        # 前缀失配（发生在建任务处），退出任务不再失配。热重扫补注册工具后经
        # refresh_tools_declaration 重建（稀有事件，接受一次 KV 前缀失配）。
        self._summary_high_base = self.summary_high_threshold
        self._summary_low_base = self.summary_low_threshold
        self.refresh_tools_declaration()

    def refresh_tools_declaration(self) -> None:
        """重建工具声明并重锚摘要阈值（__init__ 与热重扫共用）。

        前缀 token 估算：启动期引擎未必已装载，故仍用字符估算（声明 JSON chars/4，实测 4.02
        字/token；系统提示走 context_guard.fallback_estimate）；与 context_guard 的实测量法无关。
        catalog 模式下声明变小，估算与阈值随之自动收紧。"""
        known = {s.get("name") for s in self.registry.get_all_schemas()}
        self._core_names, unknown = tool_disclosure.split_core(self._core_cfg, known)
        new_unknown = [n for n in unknown if n not in self._core_warned]
        if self.tool_schema_mode == "catalog" and new_unknown:
            self._core_warned.update(new_unknown)
            events.emit("tool_routing_core_unknown", names=list(new_unknown))
            print(f"[Orchestrator] ⚠️ agent.tool_routing.core 中 {new_unknown} 未注册"
                  f"（探活未过或拼写错误），已跳过")
        get_summaries = getattr(self.registry, "get_summaries", None)
        summaries = get_summaries() if callable(get_summaries) else {}
        self._summaries: Dict[str, str] = dict(summaries) if isinstance(summaries, dict) else {}
        self._routed_decl_cache: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
        # 基座声明（无路由）：prewarm_base_cache、stable_prefix_tokens_est 与首个任务前的轮次共用。
        # catalog 下路由件会让真实前缀比估算多出 top_k 件完整 schema（约 1–2K tok），摘要阈值
        # 不为此预留——阈值本是近似量，且路由集合任务期冻结，不会逐轮漂。
        self._tools_decl = self._build_tools_declaration()
        # 约束采样的语法源与 prompt 声明**解耦**：前者恒用完整 schema，后者可瘦身。
        # 二者在 full 模式下逐字相同，只有 catalog 模式才分叉（见 _build_grammar_tools）。
        self._grammar_tools = self._build_grammar_tools()
        self.stable_prefix_tokens_est = sum(
            len(json.dumps(t, ensure_ascii=False)) for t in self._tools_decl) // 4 \
            + context_guard.fallback_estimate(DEFAULT_SYSTEM_PROMPT)
        self.summary_high_threshold = self._summary_high_base + self.stable_prefix_tokens_est
        self.summary_low_threshold = self._summary_low_base + self.stable_prefix_tokens_est

    # ---------- 上下文上限护栏（W1） ----------
    def bind_context_limit(self) -> None:
        """绑定上限：app lifespan 在引擎装载后调（须先于任何 run_turn）；远端算力端重开换了
        max_model_len 时由探活路径（compute_lifecycle.refresh_context_limit）再调。

        引擎报不出上限（None）→ 不绑，主循环首轮告警一次并跳过测量；阈值与上限不相容 →
        derive_limits 的 ValueError 原样抛出（装配期 = 计算装配失败并重试；探活期 = 算力端
        标为不可用，与模型身份不符同一处理），原绑定不变。guard disabled 也绑（只为在
        inspector 暴露数字）。model_router / engine_pool 的专科引擎不走主循环，不参与绑定。"""
        lim = self.engine.context_limit
        if lim is None:
            return
        self.context_limits = context_guard.derive_limits(self.context_guard, lim)

    def _emergency_compact(self, session: ChatSession, *, measure=None,
                           current: Optional["context_guard.PromptMeasure"] = None,
                           stats: Optional[Dict[str, Any]] = None) -> Tuple[int, int]:
        """轮内应急机械压缩（ceiling 兜底）。按 context_guard.fold_mode 分两种折法：

        full（缺省）：model-facing history 里保护窗口（最近 2 条）之外的工具观察一律折成桩。
        minimal：见 _emergency_compact_minimal——从最旧的观察起分批折、折后用 measure 重量，
        量到 < hard 即停；须传 measure（无参、返回与 _context_guard_pre 同一量法的 PromptMeasure，量的
        是真要发出去的那份 prompt）与 current（折前的 PromptMeasure；其 density 用来校准首批候选的
        估计可省量）。stats 非 None 时写入 passes（full 记 1）；minimal 另写 density（末批所用系数）、
        density_raw 与 method（末批所用量值的 PromptMeasure.density 原值与 method；0 批时三者皆 None）。
        摘要折叠只能在轮边界做（rebuild_from_log 会清掉进行中的本轮），归 W2。

        落盘日志保留全文：本轮观察的 dict 与暂存轮次（审批/挂起）的 response 共享引用，且
        轮末落盘就是 history 切片——故先把本轮（session.turn_history_start_idx 起，含审批/挂起
        前的段）每条工具消息换成拷贝（tool_responses 逐项拷）断开别名，原件登记进
        session.guard_persist_originals，落盘切片经 session.persistable_messages 换回原件。
        本轮之前的消息早已落盘，与轮首机械压缩同样就地改即可。代价：history 指纹变化，前缀
        缓存整段失配一次（接受：此刻已顶到上限，失配一次远比 overflow 便宜）。
        范围 = 整个 history（含进行中步骤）；副本 + 原件表循环与 W2 共用 _compact_turn_range。

        W1b 回读句柄：被折的观察全文先补写进本会话 spill 目录、桩末带 file_extract 短句柄（与 W2
        同一 _spill_stub_handle），**不看** step_compaction.spill_full——ceiling 桩若不给路，内存里
        的原文对模型就是彻底消失（2026-11-02 57K 臂实测：事实卡召回 91% → 0%）。会话无 session_dir
        或补写失败 → 桩不给句柄（旧行为）。返回 (压缩条数, 带句柄条数)；minimal 为各批累计。"""
        if self.context_guard.fold_mode == "minimal":
            if (measure is None or current is None
                    or not isinstance(current, context_guard.PromptMeasure)):
                raise ValueError("fold_mode=minimal 的应急压缩须传入 measure 与 current"
                                 "（须是 PromptMeasure，与 _context_guard_pre 同一量法）")
            return self._emergency_compact_minimal(session, measure, current, stats)
        cb = self._spill_stub_handle(session, digest_chars=self.observation_compact_chars,
                                     label="ceiling 应急压缩")
        out = self._compact_turn_range(
            session, 0, None, keep_recent=CEILING_KEEP_RECENT,
            digest_chars=self.observation_compact_chars, label="ceiling", stub_handle=cb)
        if stats is not None:
            stats["passes"] = 1
        return out["count"], (cb.spilled if cb is not None else 0)

    def _minimal_fold_candidates(self, session: ChatSession,
                                 density: float = 1.0) -> List[Tuple[int, int]]:
        """fold_mode=minimal 的折桩候选 [(history 下标, 估计折后可省 token)]，最旧在前。口径与
        compact_tool_observations_detailed 的预算驱逐候选一致：全 history 观察倒数 CEILING_KEEP_RECENT
        条之外、未压过、正文 > observation_compact_chars。可省 = round(density × (fallback_estimate(正文)
        − fallback_estimate(首部) − 桩固定部分估值))（下限 1：候选必有进展）。density 来自最近一次量值
        （context_guard.fold_density）：fallback_estimate 对真实正文可偏低 2× 以上（D48b），不校准就会
        把缺口高估成多条候选、一批多折；桩固定部分估值也是 fallback_estimate 口径（桩里的线索 ≤240 字
        抽自被折正文、密度与正文相同），故与正文一并乘密度，不单独扣减。"""
        digest = self.observation_compact_chars
        obs: List[Tuple[int, Dict[str, Any]]] = []
        for mi, msg in enumerate(session.history):
            if msg.get("role") == "tool":
                for tr in msg.get("tool_responses") or []:
                    if isinstance(tr, dict):
                        obs.append((mi, tr))
        out: List[Tuple[int, int]] = []
        for mi, tr in obs[:max(0, len(obs) - CEILING_KEEP_RECENT)]:
            resp = str(tr.get("response", ""))
            if tr.get("_compacted") or len(resp) <= digest:
                continue
            saving = round(density * (context_guard.fallback_estimate(resp)
                                      - context_guard.fallback_estimate(resp.strip()[:digest])
                                      - CEILING_FOLD_STUB_OVERHEAD_EST))
            out.append((mi, max(1, saving)))
        return out

    def _emergency_compact_minimal(self, session: ChatSession, measure,
                                   current: "context_guard.PromptMeasure",
                                   stats: Optional[Dict[str, Any]]) -> Tuple[int, int]:
        """fold_mode=minimal：折到刚好够用。每批：取最旧的候选、累计估计可省量直到凑够「当前量值 −
        hard + 1」的缺口，把落刀区间 [0, 末个入选候选下标 + 1) 交给 _compact_turn_range（保护窗仍按全
        history 倒数，区间小也不会误保护/误折窗内条目；区间里更早的候选都已入选，同一消息里的其余观察
        一并折），折后 measure() 重量；量值 < hard、候选耗尽、批数到 CEILING_FOLD_MAX_PASSES 或某批
        0 条进展即停——本函数单次调用内有界。外层（_context_guard_pre 的 ceiling 分支）还会在同一次
        生成前随外层 range(3) 最多重入 3 次，每次都可能再跑满 CEILING_FOLD_MAX_PASSES 批：两层上限
        叠加，用完仍 ≥ ceiling 也终止，不是只有「折到候选耗尽（0 条）」才终止。带句柄规则与 full 同
        （每批一个 _spill_stub_handle 回调）。

        密度校准（D48b）：候选可省量按 fallback_estimate 估，与分词器实测差一个随内容变的系数（英文日志、
        代码、CJK 各不相同；d48 批实测填充正文偏低约 2.2×，首批多折约 2.6×）。每批先取最近一次量值的
        context_guard.fold_density 缩放候选可省量；density 是整段 prompt（系统提示、工具 schema、模板、
        各类观察混在一起）的混合密度，只是候选正文密度的近似——系统提示是中文（密度 <1）会把系数拉到
        低于填充正文自身的密度，首批校准因此只能部分消除超折，剩余超折属预期。折后重量的 PromptMeasure
        带回新 density，下一批即按它重校准：折掉一批正文会改变整段的内容构成，混合密度随之变化，用
        最新量值总比沿用旧值更接近；精确做法是算上一批的边际密度（(折前 tokens − 折后 tokens) ÷
        (折前估值 − 折后估值)），留作后续。density 未知（chars 量法 / 假引擎）= 1.0，与校准前逐字节
        同行为。"""
        hard = self.context_guard.hard
        total = spilled = passes = 0
        cur_m = current
        used_dens = None        # 末个真落刀批所用的系数（0 批 = None）
        used_density_raw = None  # 末个真落刀批所用量值的 PromptMeasure.density 原值（D48b；可为 None）
        used_method = None       # 同上，量值的 method
        while passes < CEILING_FOLD_MAX_PASSES and cur_m.tokens >= hard:
            dens = context_guard.fold_density(cur_m)
            cands = self._minimal_fold_candidates(session, dens)
            if not cands:
                break
            gap = cur_m.tokens - hard + 1
            acc, end = 0, 0
            for mi, saving in cands:
                acc += saving
                end = mi + 1
                if acc >= gap:
                    break
            cb = self._spill_stub_handle(session, digest_chars=self.observation_compact_chars,
                                         label="ceiling 应急压缩（minimal）")
            out = self._compact_turn_range(
                session, 0, end, keep_recent=CEILING_KEEP_RECENT,
                digest_chars=self.observation_compact_chars, label="ceiling-minimal",
                stub_handle=cb)
            passes += 1
            used_dens = dens
            used_density_raw = cur_m.density
            used_method = cur_m.method
            total += out["count"]
            spilled += cb.spilled if cb is not None else 0
            if not out["count"]:
                break           # 候选在区间内却 0 条落刀（不应发生）：不空转
            cur_m = measure()
        if stats is not None:
            stats["passes"] = passes
            stats["density"] = used_dens
            stats["density_raw"] = used_density_raw
            stats["method"] = used_method
        return total, spilled

    def _compact_turn_range(self, session: ChatSession, start: int, end: Optional[int], *,
                            keep_recent: int, digest_chars: int, label: str,
                            stub_handle=None, exempt_names=None,
                            exempt_spill_readbacks: bool = False) -> Dict[str, int]:
        """轮内机械压缩的共用实现（W1 ceiling 应急 / W2 步骤边界与 hard 钩子）：只对 history 下标
        [start, end) 的工具观察落刀（保护窗 keep_recent 仍按全 history 倒数），返回
        {"count", "freed_chars", "readback_exempt"}。

        本轮段（turn_history_start_idx 起）内、落刀范围里的工具消息先换成拷贝（tool_responses 逐项拷）、
        原件登记 session.guard_persist_originals——本轮观察 dict 与暂存轮次共享引用、轮末落盘是
        history 切片，就地改会把桩写进 history.jsonl（见 _emergency_compact）。本轮之前的消息早已
        落盘，就地改。拷贝内容不变时指纹不变，不会凭空打断前缀流。budget_chars=1：范围内超出
        保护窗、体量 > digest 的观察一律驱逐。exempt_names 内的条目不落刀、exempt_spill_readbacks
        为真时 spill 回读不进驱逐候选（D43b；二者都是 W2 传，W1 不传）。label 只用于日志。"""
        originals = getattr(session, "guard_persist_originals", None)
        if originals is None:
            originals = session.guard_persist_originals = {}
        hist = session.history
        n = len(hist)
        turn_start = getattr(session, "turn_history_start_idx", None)
        turn_start = turn_start if isinstance(turn_start, int) and 0 <= turn_start <= n else 0
        lo = max(0, int(start or 0))
        hi = n if end is None else max(0, min(int(end), n))
        for i in range(max(turn_start, lo), hi):
            m = hist[i]
            if m.get("role") != "tool" or not isinstance(m.get("tool_responses"), list):
                continue
            if id(m) in originals and originals[id(m)][0] is m:
                continue   # 已是本轮早先换进来的副本：原件已登记，直接复用
            cp = dict(m)
            cp["tool_responses"] = [dict(tr) if isinstance(tr, dict) else tr
                                    for tr in m["tool_responses"]]
            originals[id(cp)] = (cp, m)
            hist[i] = cp
        out = compact_tool_observations_detailed(
            hist, keep_recent=keep_recent, digest_chars=digest_chars, budget_chars=1,
            start=start, end=end, stub_handle=stub_handle, exempt_names=exempt_names,
            exempt_spill_readbacks=exempt_spill_readbacks)
        if out["count"]:
            # 范围按实际落刀区间打印（起点钳到 ≥0、终点钳到 history 长度）
            print(f"[Orchestrator] 轮内压缩（{label}）：[{lo}, {hi}) 折叠 {out['count']} 条观察，"
                  f"释放 {out['freed_chars']:,} 字符")
        return out

    def _step_stub_handle(self, session: ChatSession):
        """W2 spill 补写回调（交给压缩器的 stub_handle）：spill_full 关 → None（桩不给句柄）；
        开 → _spill_stub_handle（digest = step_compaction.digest_chars）。"""
        cfg = self.step_compaction
        if not cfg.spill_full:
            return None
        return self._spill_stub_handle(session, digest_chars=cfg.digest_chars, label="步骤边界压缩")

    def _spill_stub_handle(self, session: ChatSession, *, digest_chars: int, label: str):
        """spill 补写回调的共用实现（W2 步骤边界 / W1b ceiling 应急）：被压观察正文 > digest_chars
        → 先把全文落进本会话 spill 目录（复用 spill_observation：0600、撞名 -n、128MB 会话预算、
        目录创建），返回 file_extract 短句柄调用串；正文 ≤ digest（桩里已是全文）/预算满/写失败 →
        None（桩不给路径——给一条走不通的路比不给更毒，见 spill.py）。session_dir 为 None → 整个
        回调为 None。fail-open：补写抛异常与返回 None 同按失败处理，失败告警（含异常文本）每个回调
        （= 每次压缩）只打一次。回调上挂 .spilled = 给出句柄的条数（含复用已有文件的，ceiling 事件用）。
        文件名 = 正文 sha256 前 16 hex（过 spill.py 的 call_id / 落盘名正则）：ceiling 应急压缩扫整个
        history，重建/恢复会话后旧轮观察以全文回到内存，下一次 ceiling 会再压它们——同名文件已在且
        字节相同（含 spill_observation 撞名后缀 -1..-7）就复用其句柄、不重写，免得同一份正文按新名
        反复落盘吃 128MB 会话预算；同 hash 异内容照常交给 spill_observation 撞名换 -n。不承诺一次读完：file_extract 文本分支回读带行号前缀
        （每行 "%5d | " + 头行），3,000–4,000 字的全文读回会超过 observation_max_chars、被编排层
        再截断一次——截断中缝照常给出续读路（剩余行区间），模型按中缝续读即可。截断过的观察压缩器
        不调本回调（内存里不是全文）。"""
        if getattr(session, "session_dir", None) is None:
            return None
        warned = [False]

        def _cb(tr: Dict[str, Any]) -> Optional[str]:
            text = str(tr.get("response", ""))
            if len(text) <= digest_chars:
                return None
            err = ""
            try:
                body = text.encode("utf-8", "surrogatepass")
                call_id = hashlib.sha256(body).hexdigest()[:16]
                got = None
                for stem in (call_id, *(f"{call_id}-{k}" for k in range(1, 8))):
                    f = resolve_spill_ref(session, SPILL_HANDLE_PREFIX + stem)
                    if f is not None and f.read_bytes() == body:
                        got = (str(f), len(text))   # 同一份正文已落过盘：复用句柄、不重写
                        break
                if got is None:
                    got = spill_observation(session, call_id, text)
            except Exception as e:   # fail-open：补写出错只丢句柄，不打断压缩
                err, got = f"（{e!r}）", None
            if got is None:
                if not warned[0]:
                    warned[0] = True
                    print(f"[Orchestrator] ⚠️ {label}：观察 {tr.get('name')} 全文补写 spill 失败{err}，"
                          "桩不给回读句柄（本次压缩后续失败不再重复告警）")
                return None
            _cb.spilled += 1
            return f'file_extract(path="{spill_handle(got[0])}")'
        _cb.spilled = 0
        return _cb

    def _step_compact(self, session: ChatSession, start: int, end: int, *, trigger: str,
                      step_id: Any = None) -> int:
        """W2 一次压缩（步骤边界 trigger="step" / hard 档首次 trigger="hard"）：[start, end) 是本轮
        已完成步骤段。count>0 时发 step_compaction 事件并累计轮统计，返回条数。

        D43b：spill 回读（模型刚花一次迭代读回的全文）不进驱逐候选——再压就是同一句柄的桩、白读。
        本次**首次**免压的回读条数 > 0 时另发 step_readback_exempt 事件（与 count 无关：全免的边界也
        留痕；同一条回读跨边界只计一次）；step_compaction 事件本身不变。"""
        cfg = self.step_compaction
        if end <= start:
            return 0
        out = self._compact_turn_range(session, start, end, keep_recent=cfg.keep_recent,
                                       digest_chars=cfg.digest_chars, label=trigger,
                                       stub_handle=self._step_stub_handle(session),
                                       exempt_names=STEP_COMPACT_EXEMPT,
                                       exempt_spill_readbacks=True)
        if out["count"]:
            events.emit(step_compaction.EVT_STEP_COMPACT, session=session.session_id,
                        trigger=trigger, step_id=step_id, count=out["count"],
                        freed_chars=out["freed_chars"],
                        level=getattr(session, "turn_context_guard_level", None),
                        start=start, end=end)
            session.turn_step_compactions = int(getattr(session, "turn_step_compactions", 0) or 0) + 1
            session.turn_compaction_freed_chars = (
                int(getattr(session, "turn_compaction_freed_chars", 0) or 0) + out["freed_chars"])
        if out.get("readback_exempt"):
            events.emit(step_compaction.EVT_STEP_READBACK_EXEMPT, session=session.session_id,
                        step_id=step_id, count=int(out["readback_exempt"]),
                        level=getattr(session, "turn_context_guard_level", None),
                        start=start, end=end)
        return out["count"]

    def _block_prefix_cache(self) -> bool:
        """后端缓存语义（W2 起压档位用）：后端类可选属性 block_prefix_cache（remote / cuda 的 vLLM
        块级前缀缓存 True；缺省 False = 缓存不可裁，如 mlx），见 core/backend_protocol.py。"""
        return bool(getattr(getattr(self.engine, "backend", None), "block_prefix_cache", False))

    # ---------- 工具目录（两级可见性） ----------
    def _meta_schemas(self) -> List[Dict[str, Any]]:
        """随模式变化的编排层元工具集合（catalog 模式多一个 get_tool_schema）。

        声明面与语法面共用这一份：两边对「本模式下有哪些工具可调」必须同口径，
        否则语法会允许（或禁止）一个声明里不存在（或存在）的工具。
        元工具即便在 catalog 模式也保留完整 schema——它们本来就要带参调用。
        """
        metas = [UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA]
        return (metas + [GET_TOOL_SCHEMA_SCHEMA]) if self.tool_schema_mode == "catalog" else metas

    def _build_tools_declaration(self, routed: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
        """**prompt 面**的工具声明（进稳定前缀、算 token 预算的那一份）。

        full：全部工具完整 schema 常驻（与 W-2 前逐字节一致）。catalog：元工具 + core +
        routed 完整 schema，其余目录行（名字 + summary），装配见 tool_disclosure.disclose。"""
        metas = self._meta_schemas()
        return tool_disclosure.disclose(
            list(self.registry.get_all_schemas()) + metas, mode=self.tool_schema_mode,
            meta_names=[m["name"] for m in metas], core=self._core_names,
            routed=routed, summaries=self._summaries)

    def _route_tools(self, query: str) -> List[str]:
        """D31/D32：任务建立时的一次性词法路由（纯本地、确定性）。full 模式或 top_k=0 → []。"""
        if self.tool_schema_mode != "catalog" or self.tool_routing_top_k <= 0:
            return []
        return tool_disclosure.route_tools(
            query, self.registry.get_all_schemas(), core=self._core_names,
            top_k=self.tool_routing_top_k,
            exclude=[m["name"] for m in self._meta_schemas()], summaries=self._summaries)

    @staticmethod
    def _routing_query(goal: str, history: List[Dict[str, Any]]) -> str:
        """路由输入 = 任务 goal + 建立该任务的用户请求原文（goal 本就截自原文时不重复计）。"""
        goal = (goal or "").strip()
        request = _last_user_text(history, limit=None).strip()
        if not goal or request.startswith(goal):
            return request or goal
        return f"{goal}\n{request}"

    def _tools_decl_for(self, session: ChatSession) -> List[Dict[str, Any]]:
        """本次生成的 prompt 面声明。full 模式 = 基座声明。catalog 模式：有带 plan 的任务 =
        基座 + 该任务冻结的路由件；否则（无任务，或 D47 轮首种下、尚无 plan 的任务）= 基座 +
        会话级保留集合（上一个任务的路由件，任务结束不撤，免一次前缀失配；首个任务前为空 =
        基座）。旧快照恢复的带 plan 任务没有路由记录（None）时就地补算一次并冻结（此后与
        新建任务同语义）。"""
        if self.tool_schema_mode != "catalog":
            return self._tools_decl
        task = session.task_state
        if task is not None and task.plan:
            if task.routed_tools is None:
                task.routed_tools = self._route_tools(
                    self._routing_query(task.goal, session.history))
                session.last_routed_tools = list(task.routed_tools)
            names = task.routed_tools
        else:
            names = getattr(session, "last_routed_tools", None) or []
        known = {s.get("name") for s in self.registry.get_all_schemas()}
        routed = tuple(n for n in names if n in known)
        if not routed:
            return self._tools_decl
        decl = self._routed_decl_cache.get(routed)
        if decl is None:
            decl = self._build_tools_declaration(routed)
            if len(self._routed_decl_cache) >= 64:     # 长驻进程按任务累积，设上限防无界增长
                self._routed_decl_cache.clear()
            self._routed_decl_cache[routed] = decl
        return decl

    def _build_grammar_tools(self) -> List[Dict[str, Any]]:
        """**约束采样面**的语法源：恒为完整 schema，与 prompt 面的瘦身解耦。

        prompt 面在 catalog 模式下刻意只留名字+一行描述（省稳定前缀 token），但语法
        面不能跟着瘦：目录条目没有 `parameters` → `tool_grammar` 只生成
        `call:name{}` 这一条分支 → **约束采样物理上只允许空实参，全部带参工具被钳死**
        （OSS 扫描 §6.5；实测 `run_shell_command` 的语法坍缩成
        `start: c1 / c1: "call:run_shell_command{" "}"`）。
        而 catalog 模式的设计本就是「模型先 get_tool_schema 取详情，再带参调用」——
        语法必须允许它这么做，否则整个逃生口拨不动。
        full 模式下本函数与 `_build_tools_declaration` 逐字同值（无行为变化）。
        注意两面**别名同一批 schema dict**（registry 与模块级元工具常量都不复制）：
        今天没有任何就地改写 schema 的人，一旦出现，改动会同时出现在两面。
        """
        return list(self.registry.get_all_schemas()) + self._meta_schemas()

    async def prewarm_base_cache(self) -> bool:
        """T3：以真实轮次同款入参（默认系统提示 + 当前工具声明）预填基座前缀缓存。

        幂等（engine 按内容 key 判断），app 启动与空闲回合都可调；工具集经 rescan
        变化后 key 变，下次调用自动重建。自定义系统提示的会话不匹配基座——前缀匹配
        自然回退，只是享受不到，无正确性问题。"""
        from ..memory.session import DEFAULT_SYSTEM_PROMPT
        return await self.engine.prewarm_base_cache(DEFAULT_SYSTEM_PROMPT, self._tools_decl)

    async def prewarm_session_cache(self, session) -> bool:
        """T1：压缩改写上下文后的空闲预热（真实轮次同款工具声明）。"""
        return await self.engine.prewarm_session_cache(session, self._tools_decl_for(session))

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
                       resume_mutated_ok: bool = False,
                       resume_failure: bool = False,
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
        # Once retrieval fails, the next generation is provisional: never publish
        # an invented answer before the deterministic evidence gate can inspect it.
        # The flag is fixed at generation start, not changed mid-stream by tools.
        provisional_generation = False
        gen_kwargs["on_text_delta"] = lambda t: (
            None if provisional_generation else journal.emit("token_delta", {"text": t}))
        gen_kwargs["on_thinking_delta"] = lambda t: (
            None if provisional_generation else journal.emit("thinking_delta", {"text": t}))
        gen_kwargs["on_tool_pending"] = lambda name: journal.emit("tool_call_pending", {"name": name})

        task = session.task_state
        if task is not None and not keep_recent_calls:
            task.recent_calls = []  # 重复调用拦截按轮生效（审批/挂起续跑保留）
        # 总迭代熔断的累计分母（跨审批/挂起续跑存续，见 session.carried_iterations）：
        # keep_recent_calls 在轮中续跑时为真——新用户轮次归零，续跑沿用先前累计值，
        # 使「反复吐坏写→弹审批→续跑」的循环仍被同一个 max_total_iterations 保险丝罩住。
        if not keep_recent_calls:
            session.carried_iterations = 0
            session.carried_turn_stats = {}
            # [D2 谎报围堵] 拒批指纹按轮生效：新用户轮清零，审批/挂起续跑沿用
            session.denied_call_fps = set()
            # [W1 上限护栏] 本轮峰值/最高档、一次性动作旗标、应急压缩还原表与本轮起点按轮生效
            # （审批/挂起续跑段沿用：不重复压缩、不重复发尾注与事件）。还原表可清：上一轮的
            # 落盘切片早已换回原件。
            session.turn_peak_prompt_tokens = 0
            session.turn_context_guard_level = None
            session.turn_guard_flags = {}
            session.guard_persist_originals = {}
            session.turn_history_start_idx = len(session.history)
            session.turn_thinking_replay = None   # [W3] 投影统计按轮：不让上一轮的值盖进本轮
            # [W2 步骤边界压缩] 已完成步骤段的终点锚（本轮起点起算）与轮统计；续跑段沿用不清
            session.turn_step_start_idx = session.turn_history_start_idx
            session.turn_step_compactions = 0
            session.turn_compaction_freed_chars = 0
            session.turn_full_renders = 0          # engine.generate_once 全量渲染时 +1
        # 无任务态指纹也属于整轮：不能靠暂停/审批重入清掉重复调用屏障。
        no_task_calls: List[str] = list(session.carried_turn_stats.get("no_task_calls") or [])
        turn_start_idx = len(session.history)  # 本轮起点：收尾时扫描本轮工具产出的资产链接
        parse_errors: Dict[str, int] = {}  # 本轮坏调用原文 -> 次数（断环 + 升级纠错）
        nudged = False
        iterations = 0
        metrics: Dict[str, Any] = {}
        # [仪表 Phase 0] 整轮累计口径：ReAct 循环内多次生成的分解求和 + 废轮计数。
        # 每次生成后 stamp 进当前 metrics dict（该 dict 会随任一返回路径带出）。
        # 审批/挂起把一轮切成多个 run_turn 段：从 session.carried_turn_stats 播种续算，
        # 使 history.jsonl 最终落盘的 metrics 是整轮口径而非末段（实测被低估 8 倍）。
        _carried = getattr(session, "carried_turn_stats", None) or {}
        retrieval = RetrievalEvidence(_carried.get("retrieval_evidence"))
        turn_breakdown: Dict[str, int] = dict(_carried.get("breakdown") or {})
        turn_gen_tokens = int(_carried.get("gen_tokens") or 0)
        turn_spec = {"drafted": int((_carried.get("spec") or {}).get("drafted") or 0),
                     "accepted": int((_carried.get("spec") or {}).get("accepted") or 0)}
        carried_parse_errors = int(_carried.get("parse_errors") or 0)
        # [F6 子沙箱用量归因] 本轮的用量回收槽：web/math/excel 沙箱与收尾目标校验在自己的
        # 一次性会话里跑的每次生成，都往这里回填计数（见 agents/sandbox_runtime.note_usage）。
        # 此前这些子生成完全不入父轮的账——落盘 metrics 系统性低估、熔断只数主循环迭代。
        turn_usage: Dict[str, int] = dict(_carried.get("usage") or {})

        def _suspend_for_approval(phys_calls_: List[Dict[str, Any]], responses_: List[Dict[str, Any]],
                                  approval_needed_: List[Dict[str, Any]]) -> Dict[str, Any]:
            """[审批闸门] 整批挂起待审批（模型发起与编排层代发起共用同一条路）。"""
            if responses_:
                session.add_tool_message(responses_)
            session.pending_tool_calls = phys_calls_
            _carry_turn_state()
            names = "、".join(c.get("name") for c in approval_needed_)
            session.set_activity(f"等待授权：{names}", phase="awaiting_approval")
            _exit_audit()   # 挂起态也记：用户改发新消息时这份 metrics 会被当作 turn_ended 落盘
            print(f"[Orchestrator] 敏感工具调用挂起待审批: {[c.get('name') for c in approval_needed_]}")
            return {
                "status": "pending_approval",
                "pending_tools": [
                    {"name": c.get("name"), "arguments": c.get("arguments", {})}
                    for c in approval_needed_
                ],
                "metrics": metrics,
            }

        # 沙箱包壳（无状态 MCP 工具）拿不到本方法的局部量，经 ContextVar 交接：在此处
        # （run_turn 的任务上下文里）set，asyncio.to_thread 把上下文拷进工具线程，包壳在
        # 那里 get 出来再作参数显式传进沙箱协程——投回主循环的协程不继承工具线程上下文。
        # 刻意不放进 _invoke_tool：那里够不到 turn_usage，塞进 session 就是多一处可变状态。
        # 惰性导入：core 不静态依赖 agents（与 _invoke_tool / _goal_alignment_check 同先例）。
        from ..agents import runtime as _usage_runtime
        _usage_runtime.set_usage_sink(turn_usage)

        def _stamp_turn_stats() -> None:
            metrics["token_breakdown"] = dict(turn_breakdown)
            metrics["generations"] = session.carried_iterations + iterations
            metrics["generation_tokens_turn"] = turn_gen_tokens
            metrics["parse_error_count"] = carried_parse_errors + sum(parse_errors.values())
            if turn_spec["drafted"]:
                metrics["spec_drafted"] = turn_spec["drafted"]
                metrics["spec_accepted"] = turn_spec["accepted"]
            # [F6] 子沙箱/护栏用量按桶盖进落盘 metrics。只在非零时出现：不给存量轮次
            # 凭空加字段（history.jsonl 是既有消费面）。
            for k in ("sub_generations", "sub_generation_tokens",
                      "goal_verify_generations", "goal_verify_generation_tokens"):
                if turn_usage.get(k):
                    metrics[k] = turn_usage[k]
            # [W1] 本轮主循环 prompt 峰值（守卫测量值与生成实测值取大）与护栏最高档；同样只在有值时出现
            _peak = int(getattr(session, "turn_peak_prompt_tokens", 0) or 0)
            if _peak:
                metrics["peak_prompt_tokens"] = _peak
            _lvl = getattr(session, "turn_context_guard_level", None)
            if _lvl:
                metrics["context_guard_level"] = _lvl
            # [W3] 最近一次生成 prompt 的思考回放投影统计（engine.generate_once 写）；有才出现
            # all 模式不投影（计数恒 0），不凭空加字段
            _replay = getattr(session, "turn_thinking_replay", None)
            if isinstance(_replay, dict) and _replay.get("mode") != "all":
                metrics["thinking_replay"] = {k: _replay.get(k) for k in
                                              ("mode", "kept", "dropped", "kept_chars",
                                               "dropped_chars")}
            # [W2] 步骤边界压缩次数 / 释放字符 / 全量重渲染次数；同样只在非零时出现
            for _mk, _attr in (("step_compactions", "turn_step_compactions"),
                               ("compaction_freed_chars", "turn_compaction_freed_chars"),
                               ("full_renders", "turn_full_renders")):
                _v = getattr(session, _attr, 0)
                if isinstance(_v, int) and not isinstance(_v, bool) and _v:
                    metrics[_mk] = _v

        def _carry_turn_state() -> None:
            """审批/挂起共用检查点；先stamp再转存，当前段iterations只计一次。"""
            _stamp_turn_stats()
            session.carried_iterations += iterations
            session.carried_turn_stats = {
                "breakdown": dict(turn_breakdown),
                "gen_tokens": turn_gen_tokens,
                "spec": dict(turn_spec),
                "parse_errors": carried_parse_errors + sum(parse_errors.values()),
                "usage": dict(turn_usage),
                "no_task_calls": list(no_task_calls),
                "retrieval_evidence": retrieval.checkpoint(),
            }
        # 有界恢复计数（T2-5 地基，承载 T1-2/T1-3；到顶即放行，杜绝新增分支造成死循环）
        max_tokens_override: Optional[int] = None   # T1-3：非 None 时本轮后续生成用升档上限
        max_tokens_recoveries = 0                    # T1-3：多轮「接着写」已用次数
        premature_finish: Dict[str, int] = {}        # T1-2：按步（标题）计的提前收尾推回次数
        # T1-4：写后验证门控状态。unverified_write=已有写调用且其后没有任何只读复查
        # （审批续跑时刚执行的批准写调用由 resume_after_approval 传入）；verify_nudges=已推回次数。
        unverified_write = resume_unverified_write
        verify_nudges = 0
        # [D2 谎报围堵：26B 档实案 2026-08-10] 三态跟踪——
        # unresolved_failure：最近的物理调用失败（System Error / shell 非零退出）且其后
        #   再无成功的物理调用（heredoc 写炸后直接宣布"三个文件都建好了"实案）；
        # mutated_ok_this_turn：本轮是否有成功放行的变更类物理调用（零调用却声称
        #   "已用 nohup 在后台拉起"实案的判据）。收尾时据此推回一次，仍坚持则降级 warning。
        unresolved_failure = resume_failure
        mutated_ok_this_turn = resume_mutated_ok
        failure_nudges = 0
        fabrication_nudges = 0
        repetition_hits = 0          # 流式复读护栏本轮命中次数（第 1 次推回、第 2 次终止）
        goal_verified = False        # 收尾目标校验预算（每次 run_turn 最多审计一次）
        thinking_only_nudges = 0     # 全思考空产出推回预算（THINKING_ONLY_NUDGE_MAX）
        base_max_tokens = int(gen_kwargs.get("max_tokens", DEFAULT_MAX_TOKENS))
        # T3-c：一次性护栏提醒走可变尾部注入（用完即弃、不进历史、不被摘要压缩）。
        # 下一次生成消费后立即清空；由 T1-2/T1-3 分支按需填充。
        pending_tail_notes: List[str] = []

        def _transition(reason: str) -> None:
            """标注一次续跑原因（可审计：为什么又转一圈），便于日志追踪与死循环定位。"""
            print(f"[Orchestrator] transition → {reason}")

        # [W1 上限护栏] 每轮一次的事件/动作闸：挂在 session 上，跨审批/挂起续跑段沿用
        guard_cfg = self.context_guard
        guard_flags = getattr(session, "turn_guard_flags", None)
        if not isinstance(guard_flags, dict):
            guard_flags = session.turn_guard_flags = {}
        for _k in ("clamped", "soft", "hard", "compacted"):
            guard_flags.setdefault(_k, False)
        for _k in ("ceiling_compacted", "ceiling_spilled"):   # 本轮 ceiling 应急压缩计数（终止事件用）
            guard_flags.setdefault(_k, 0)

        def _note_peak(tokens: Any) -> None:
            if isinstance(tokens, int) and not isinstance(tokens, bool) \
                    and tokens > (getattr(session, "turn_peak_prompt_tokens", 0) or 0):
                session.turn_peak_prompt_tokens = tokens

        def _guard_measure(prompt_args: Dict[str, Any], limits: "context_guard.ContextLimits"):
            m = self.engine.measure_prompt(**prompt_args)
            _note_peak(m.tokens)
            level = context_guard.classify(m.tokens, guard_cfg, limits)
            if context_guard.level_rank(level) > context_guard.level_rank(
                    getattr(session, "turn_context_guard_level", None)):
                session.turn_context_guard_level = level
            return m, level

        def _context_guard_pre(prompt_args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            """生成前护栏（max_tokens 钳制之后）：量 → 四档处理。返回非 None = 本轮已终止的结果，
            调用方原样返回、**不得**再发生成请求。prompt_args 与 generate_once 的 prompt 入参
            是同一份 dict（hard 尾注追加后就地更新），measure_prompt 与 generate_once 又走同一个
            _assemble_prompt 判定（流有效 = 流+增量段，否则全量渲染），故量到的就是要发出去的。
            残差：增量路后端按记录的 token 喂历史段，与对同一文本重分词可在边界差几个 token。"""
            if not guard_cfg.enabled:
                return None
            limits = self.context_limits
            if limits is None:
                if not self._guard_unbound_warned:
                    self._guard_unbound_warned = True
                    events.emit(context_guard.EVT_UNBOUND, session=session.session_id)
                    print("[Orchestrator] ⚠️ 上下文上限未绑定（引擎报不出 context_limit，或装配未调 "
                          "bind_context_limit）：上限护栏本进程不生效")
                return None

            def _detail(m, level):
                return dict(prompt_tokens=m.tokens, method=m.method, level=level,
                            limit=limits.limit, ceiling=limits.ceiling,
                            iteration=session.carried_iterations + iterations + 1)

            m, level = _guard_measure(prompt_args, limits)
            fold_mode = guard_cfg.fold_mode
            minimal = fold_mode == "minimal"
            last_measure: Dict[str, Any] = {}

            def _remeasure() -> "context_guard.PromptMeasure":
                """minimal 折叠的逐批重量：与上面同一量法（_guard_measure），末次结果留给下面续判；
                返回整个 PromptMeasure（其 density 供下一批重校准）。"""
                last_measure["ml"] = _guard_measure(prompt_args, limits)
                return last_measure["ml"][0]

            # 每个分支各翻一面旗，最多各走一次；循环只为让「压缩后落到 hard」与「尾注把 hard
            # 推到 ceiling」都回到同一套判定，不出现分支间漏判。fold_mode=minimal 的 ceiling 分支不受
            # 一次性旗限制（每次生成前到 ceiling 都可再折）——但仍是有界的：本次生成前这个 for 最多
            # range(3) 次外层 pass，每次 ceiling 分支再各自最多 CEILING_FOLD_MAX_PASSES 批
            # （_emergency_compact_minimal 内部）；两层上限叠加，用完仍 ≥ ceiling 也终止，不是只有
            # 「折到候选耗尽（0 条）」才终止——只是真走到那一步（可达性低：要估算长期高估可省量、
            # 批数/外层 pass 还没耗尽候选先耗尽）时同样会提前终止，省掉继续空扫。
            # [D48] 本次调用累计（minimal 可能在同一次调用里折好几次：ceiling→hard→尾注推回
            # ceiling→再折）：事件/print 用首个 before 与累计 compacted/spilled/passes，不是最后
            # 一次折叠的局部值。是否发事件仍按旧语义——某次折叠尝试后 level 真的离开了 ceiling
            # （fold_succeeded），而不是按 compacted 条数：full 模式下即便 compacted==0，只要那次
            # remeasure 把 level 量出了 ceiling 之外也算成功（test_ceiling_compaction_short_
            # observation_has_no_handle）；反之 compacted>0 但 level 量出来仍是 ceiling（候选全折完
            # 仍不够）不算成功、不发事件（test_ceiling_abort_after_compaction_reports_counts、
            # 以及 minimal 第二次 ceiling 折完仍 ceiling 的那次尝试）。
            fold_before = fold_after = None
            fold_compacted = fold_spilled = fold_passes = 0
            fold_dens = None             # minimal 末次折叠末批所用的密度系数（full 恒 None）
            fold_density_raw = None     # 同上，所用量值的 PromptMeasure.density 原值（可为 None）
            fold_method = None          # 同上，所用量值的 method
            fold_succeeded = False
            for _ in range(3):
                if level == "ceiling" and (minimal or not guard_flags["compacted"]):
                    guard_flags["compacted"] = True
                    if fold_before is None:
                        fold_before = m.tokens
                    fold_stats: Dict[str, Any] = {}
                    if minimal:
                        last_measure.clear()
                        n, n_spilled = self._emergency_compact(
                            session, measure=_remeasure, current=m, stats=fold_stats)
                        if "ml" in last_measure:       # 末次重量即折后量值；0 批则未重量、仍是折前值
                            m, level = last_measure["ml"]
                            fold_after = m.tokens
                    else:
                        n, n_spilled = self._emergency_compact(session, stats=fold_stats)
                        m, level = _guard_measure(prompt_args, limits)
                        fold_after = m.tokens
                    guard_flags["ceiling_compacted"] += n
                    guard_flags["ceiling_spilled"] += n_spilled
                    passes = fold_stats.get("passes", 1)
                    if fold_stats.get("density") is not None:
                        fold_dens = fold_stats["density"]
                        fold_density_raw = fold_stats.get("density_raw")
                        fold_method = fold_stats.get("method")
                    fold_compacted += n
                    fold_spilled += n_spilled
                    fold_passes += passes
                    if level != "ceiling":
                        fold_succeeded = True
                    if level == "ceiling" and minimal and not n:
                        # minimal：折了 0 条（候选耗尽）仍 ≥ ceiling → 终止。只是省掉接下来的空扫——
                        # 不 break 同样会在外层 range(3) 用完后落到下面的 ceiling 终止判定。
                        break
                    continue
                if level == "hard" and not guard_flags["hard"]:
                    guard_flags["hard"] = True
                    events.emit(context_guard.EVT_HARD, session=session.session_id, **_detail(m, level))
                    # [W2 hard 钩子] 首次 hard：压本轮已完成步骤段 [轮起点, 步骤锚)——锚 = 最近一次边界批 tool
                    # 消息的下标，进行中步骤与边界批本身都不碰；块级前缀缓存与不可裁缓存后端一律（门槛就是
                    # hard），enabled=False 不压。不可裁缓存（MLX）上：hard 那次本就退全量，之后每个边界再各付
                    # 一次冷填（full_renders 记账）。压了就先重量、按新量值决定：仍 ≥ hard 才发尾注（tokens
                    # 用压后值），回落到 hard 以下不发尾注（hard 事件与旗标照旧已发/已置）；仍超 ceiling 则
                    # 下一 pass 由 W1 应急压缩兜底。每次改动 prompt 后都重量，最终发出去的就是最后一次量到的。
                    if self.step_compaction.enabled:
                        _t0 = getattr(session, "turn_history_start_idx", None)
                        _s0 = getattr(session, "turn_step_start_idx", None)
                        if isinstance(_t0, int) and isinstance(_s0, int) and _s0 > _t0:
                            if self._step_compact(session, _t0, min(_s0, len(session.history)),
                                                  trigger="hard"):
                                m, level = _guard_measure(prompt_args, limits)
                                if context_guard.level_rank(level) < context_guard.level_rank("hard"):
                                    continue
                    pending_tail_notes.append(
                        f"上下文已用 {m.tokens:,}/{limits.limit:,} token，接近上限。请收敛：少回灌大段观察，"
                        "尽快给出结论或完成当前步骤。")
                    prompt_args["tail_notes"] = list(pending_tail_notes)
                    m, level = _guard_measure(prompt_args, limits)   # 量的是含尾注、真要发出去的那份
                    continue
                break
            if fold_succeeded:          # 本次调用至少一次折叠把 level 量出了 ceiling 之外才发事件
                events.emit(context_guard.EVT_FOLD, session=session.session_id,
                            before=fold_before, after=fold_after, ceiling=limits.ceiling,
                            compacted=fold_compacted, spilled=fold_spilled, mode=fold_mode,
                            passes=fold_passes, density=fold_dens,
                            density_raw=fold_density_raw, method=fold_method,
                            iteration=session.carried_iterations + iterations + 1)
                session.push_activity("note", "上下文接近上限，已折叠旧观察")
                _dens_note = f"，密度 {fold_dens:.2f}" if fold_dens is not None else ""
                print(f"[Orchestrator] 上下文上限护栏：应急压缩（{fold_mode}，{fold_passes} 批{_dens_note}）"
                      f"{fold_compacted} 条观察，{fold_before:,} → {fold_after:,} token"
                      f"（ceiling {limits.ceiling:,}）")
            if level == "ceiling":
                msg = (f"上下文已达模型上限（{m.tokens:,} / 上限 {limits.limit:,} token，"
                       f"生成预留 {guard_cfg.reserve:,}），本轮已停止。可以试试：①新开会话继续；"
                       "②把任务拆小分别发起；③减少大输出工具的连续调用。")
                session.add_assistant_message(content=msg)
                session.set_activity("已终止（上下文达模型上限）", phase="done")
                events.emit("turn_abort", session=session.session_id,
                            reason=context_guard.ABORT_REASON, **_detail(m, level),
                            compacted=guard_flags["ceiling_compacted"],
                            spilled=guard_flags["ceiling_spilled"])
                print(f"[Orchestrator] 上下文上限护栏：{m.tokens:,} ≥ ceiling {limits.ceiling:,}，终止本轮")
                _stamp_turn_stats()
                _exit_audit()
                return {
                    "status": "warning",
                    "response": _with_retrieval_note(msg),
                    "thoughts": "System: context ceiling reached; aborted.",
                    "metrics": metrics,
                }
            if level == "soft" and not guard_flags["soft"]:
                guard_flags["soft"] = True
                events.emit(context_guard.EVT_SOFT, session=session.session_id, **_detail(m, level))
            return None

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

        # [F6] 子沙箱生成走**独立**保险丝（复审修正）:计进主熔断分母会把研究型轮次的
        # 有效预算压掉 5–16 倍（一次 excel_research ≈16 次生成），「原来能答的问题现在
        # 熔断」比低估更伤。两条丝各守各的:主循环 40 防编排失控,子生成 max_sub_generations
        # 防调研失控——原病根「子生成完全无界」由后者闭合。goal_verify 桶两条都不计
        # （护栏不是子 agent,把护栏烧进保险丝等于让安全网自食其果）。
        # ⑤′-3 升级策略（护栏是代码）：新用户轮轮首按 显式要求 > 目录谓词 决策；审批续跑
        # （keep_recent_calls）不重判。consult_auto = 编排层代模型发起 cloud_consult——仍走
        # 审批闸（审批卡即将出网原文）+ L0 + 台账；switch_tier 永远只建议（64G 单档常驻，
        # 运行期换档已否决）。一轮只触发一次。
        # 决策随会话带过审批/挂起续跑（与 carried_turn_stats 同理）：换档提示与审计键要在
        # 续跑段的收尾落出，不能因为中间弹了一次审批就丢（评审 M2）
        _fresh_turn = compact_history and not keep_recent_calls
        if _fresh_turn:
            session.carried_upgrade = []
            session._tool_hint_replayed = False
            # D47：轮首种任务（goal = 本轮用户文本，plan 空）——轮末摘要器视图（[Goal]/[Scratchpad]）
            # 与无计划的 remember 都靠它；不路由（catalog 路由只在首次建成 plan 时做，见
            # _tools_decl_for）、不注入 task_block（plan 非空才注入）。上一轮带着未完成 plan 留下的
            # 任务原样沿用；无 plan 的残留（中止出口不弃任务）换成本轮新 goal。
            _seed = session.task_state
            if _seed is None or not _seed.plan:
                session.task_state = TaskState(goal=_last_user_text(session.history))
        upgrade_decisions: List[Any] = list(getattr(session, "carried_upgrade", None) or [])
        session.carried_upgrade = upgrade_decisions

        def _exit_audit() -> None:
            """⑤′-1/⑤′-3 审计键在**所有**收尾出口落 metrics（PROTOCOL §2.3 的承诺；阶段复审 M2：
            runaway/parse_loop/cancelled 三类出口曾漏）。不追加用户文案——这些出口的 response 不是答复。"""
            _finalize_cloud_degraded(session, metrics, "", append_notice=False)
            _append_upgrade_notices("", metrics, upgrade_decisions)

        def _with_retrieval_note(response: str) -> str:
            # Budget/parse exits keep their real reason; an unresolved retrieval is
            # appended as a second fact, never substituted for the real cause.
            if not retrieval.unresolved:
                return response
            metrics["retrieval_incomplete"] = True
            return response + "\n\n" + retrieval.explanation()

        def _retrieval_incomplete() -> Dict[str, Any]:
            # Provisional-generation path only (no tool calls after one REPAIR_NOTE
            # nudge): do not retain the discarded draft/thoughts in the transcript.
            text = retrieval.explanation()
            session.add_assistant_message(content=text)
            session.set_activity("检索未完成", phase="done")
            _stamp_turn_stats()
            _exit_audit()
            return {"status": "warning", "response": text, "thoughts": [], "metrics": metrics}
        # 只在**新用户轮**判：审批续跑（keep_recent_calls）与挂起续跑（compact_history=False
        # 的轮中重入）都不是新轮——重入再判就是一轮两次提醒/两次代发起。
        # 用户已点取消/挂起的轮不判（先于代发起：取消了的轮不该再花钱出网，评审 M1）
        if _fresh_turn and not session.cancel_requested and not session.suspend_requested:
            _new = self._upgrade_at_turn_start(session, journal)
            upgrade_decisions.extend(_new)      # 先全部入账（审计面），再逐条处理——代发起挂起也不丢后面的决策
            for _d in _new:
                if _d.action in ("consult_suggest", "tool_hint"):
                    from ..cloud.upgrade import model_hint
                    pending_tail_notes.append(model_hint(_d))
                elif _d.action == "consult_auto":
                    from ..cloud.upgrade import synthetic_consult_call
                    # 排在后面的 tool_hint 尾注先入队（审批续跑重入时由下方"续跑补注"再补一次）
                    for _later in _new[_new.index(_d) + 1:]:
                        if _later.action in ("consult_suggest", "tool_hint"):
                            from ..cloud.upgrade import model_hint as _mh
                            pending_tail_notes.append(_mh(_later))
                    _calls = [synthetic_consult_call(self._last_user_facts(session).text)]
                    _verr = self.registry.validate_call(_calls[0]["name"], _calls[0]["arguments"])
                    if _verr:
                        print(f"[Orchestrator] 升级策略代发起被参数预检拒绝：{_verr}")
                        continue
                    session.add_assistant_message(
                        tool_calls=_calls,
                        thoughts=(f"[System upgrade] 编排层代发起云端咨询（{_d.trigger}：{_d.reason}）"
                                  "——仍需审批；云端只顾问，落地本地。"))
                    if self._gate_requires_approval(_calls[0]["name"], _calls[0]["arguments"],
                                                    self._approval_policy_for(journal)):
                        return _suspend_for_approval(_calls, [], _calls)
                    # 审批旋钮已关：直接执行（L0 与台账仍恒开），观察落历史后进主循环
                    _tracker = session.task_state.recent_calls if session.task_state is not None else no_task_calls
                    _resp = await self._execute_physical(session, _calls, _tracker, parse_errors,
                                                         journal=journal)
                    session.add_tool_message(_resp)
        elif not _fresh_turn:
            # 续跑补注：轮首的一次性尾注随挂起/审批作废了，重入后的第一次生成再给一次（只限 tool_hint：
            # consult_suggest 重入时多半已经咨询过，由 _already_consulted 与 CONSULT_ACTIONS 守）
            from ..cloud.upgrade import model_hint as _mh
            for _d in upgrade_decisions:
                if _d.action == "tool_hint" and not getattr(session, "_tool_hint_replayed", False):
                    pending_tail_notes.append(_mh(_d))
            session._tool_hint_replayed = True
        upgrade_failures = 0          # 本轮连续失败的**批次**数（一次迭代的物理调用批算一次）


        while session.carried_iterations + iterations < self.max_total_iterations:
            if turn_usage.get("sub_generations", 0) >= self.max_sub_generations:
                _stamp_turn_stats()
                events.emit("turn_abort", session=session.session_id,
                            reason="max_sub_generations",
                            sub_generations=turn_usage.get("sub_generations", 0))
                _exit_audit()
                return {
                    "status": "warning",
                    "response": _with_retrieval_note(
                        f"子调研生成已达预算上限（{self.max_sub_generations} 次），任务终止。"
                        f"本轮主循环 {session.carried_iterations + iterations} 次、"
                        f"子调研生成 {turn_usage.get('sub_generations', 0)} 次。"
                        "可以试试：①减少调研类工具的连续调用；②把任务拆小分别发起。"),
                    "thoughts": "System Warning: sandbox generation budget exceeded.",
                    "metrics": metrics,
                }
            # --- 取消检查点（迭代边界；token 级检查在 engine 内） ---
            if session.cancel_requested:
                _exit_audit()
                return {"status": "cancelled", "response": "任务已被用户取消。", "metrics": metrics}

            # --- 挂起检查点（迭代边界：上一步的生成与工具结果已提交进 history，
            #     此处是安全让出点，零浪费）：让出 worker，KV/task_state 留内存待恢复。
            #     恢复时路由再次进入 run_turn，从 task_state + 已提交 history 续跑。 ---
            if session.suspend_requested:
                session.suspend_requested = False
                _carry_turn_state()
                session.set_activity("已挂起（可恢复）", phase="suspended")
                _exit_audit()   # 同 pending_approval：挂起轮被弃时这份 metrics 就是终态
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
            # [W1 上限护栏 ①] max_tokens 钳到 generation_reserve：ceiling = 上限 − reserve 的前提。
            # guard disabled 也钳（关掉的是测量与处置，不是这条不变量）。
            # 缺键 = 引擎缺省（engine.DEFAULT_MAX_TOKENS），同样受钳：最终总写回 min(请求, reserve)
            _req_max = call_kwargs.get("max_tokens", DEFAULT_MAX_TOKENS)
            if isinstance(_req_max, int) and not isinstance(_req_max, bool) \
                    and _req_max > guard_cfg.reserve:
                call_kwargs["max_tokens"] = guard_cfg.reserve
                if not guard_flags["clamped"]:
                    guard_flags["clamped"] = True
                    events.emit(context_guard.EVT_CLAMP, session=session.session_id,
                                requested=_req_max, clamped_to=guard_cfg.reserve)
            elif "max_tokens" not in call_kwargs:
                call_kwargs["max_tokens"] = _req_max
            # prompt 入参只组这一份：护栏量它、生成发它（防分叉，W1）
            prompt_args: Dict[str, Any] = {
                "session": session, "tools": self._tools_decl_for(session),
                "task_block": task_block, "tail_notes": pending_tail_notes or None,
                "enable_thinking": call_kwargs.pop("enable_thinking"),
            }
            # [W1 上限护栏 ②–⑤] 量 → soft/hard/ceiling；终止时后端收不到这次请求
            _aborted = _context_guard_pre(prompt_args)
            if _aborted is not None:
                return _aborted
            journal.emit("iteration_started", {
                "n": iterations + 1, "thinking": bool(prompt_args["enable_thinking"]),
            })
            provisional_generation = retrieval.unresolved
            result = await self.engine.generate_once(
                **prompt_args, grammar_tools=self._grammar_tools, **call_kwargs
            )
            pending_tail_notes = []  # 一次性提醒已被本次生成消费，清空（T3-c）
            fresh_metrics = result.get("metrics")
            metrics = fresh_metrics or metrics
            if metrics.get("prompt_tokens"):
                session.last_prompt_tokens = metrics["prompt_tokens"]  # 上下文规模估计（摘要阈值/空闲维护用）
                _note_peak(metrics["prompt_tokens"])   # [W1] 生成实测值也入本轮峰值
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
                _exit_audit()
                return {"status": "cancelled", "response": "任务已被用户取消。", "metrics": metrics}

            iterations += 1
            _stamp_turn_stats()
            if task is not None and task.plan and task.current_step() is not None:
                task.step_iterations += 1

            # --- 护栏：流式复读（engine 已当场停生成、记日志与 repetition 事件） ---
            # 产出整体丢弃（不落 history，与 runaway 同：KV 前缀下次生成自动裁剪对齐）。
            # 本轮首次 → 一次性尾注推回换思路（failure_swallow_nudge 同机制）；
            # 第二次 → 终止本轮，答复里如实说明（runaway/parse_loop 同出口形态）。
            rep = result.get("repetition_guard")
            if rep:
                channel = str(rep.get("channel") or "content")
                repetition_hits += 1
                if repetition_hits >= 2:
                    msg = "模型连续两次陷入重复输出，本轮已停止。"
                    session.add_assistant_message(content=msg)
                    session.set_activity("已终止（模型连续两次陷入重复输出）", phase="done")
                    events.emit("turn_abort", session=session.session_id,
                                reason="repetition_guard", channel=channel)
                    _stamp_turn_stats()
                    _exit_audit()
                    return {
                        "status": "warning",
                        "response": _with_retrieval_note(msg),
                        "thoughts": "System: repeated output loop detected twice; aborted.",
                        "metrics": metrics,
                    }
                _transition("repetition_nudge")
                session.push_activity("note", "检测到重复输出，已打断并换思路")
                pending_tail_notes.append(
                    f"检测到输出在重复（{channel}）。请换一种思路继续，或直接给出目前能给的结论；"
                    "不要重复刚才的内容。")
                continue

            tool_calls = result["tool_calls"]
            text = result["text"]
            thoughts = result["thoughts"]

            if provisional_generation:
                # Even a tool-bearing repair iteration must not archive unverified
                # narrative/thoughts. Tool calls still use the existing approval gate.
                thoughts = []
                if not tool_calls:
                    if retrieval.nudges < 1:
                        retrieval.nudges += 1
                        pending_tail_notes.append(REPAIR_NOTE)
                        _transition("retrieval_evidence_nudge")
                        continue
                    return _retrieval_incomplete()

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
                        _exit_audit()
                        return {
                            "status": "warning",
                            "response": _with_retrieval_note(
                                "多次生成工具调用参数失控（超长复读），已终止本次任务。"
                                "可以试试：①把需求拆成几件更小的事分别发起；"
                                "②如果涉及长文本，让我分段处理。"),
                            "thoughts": "System: repeated runaway constrained tool calls; aborted.",
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
                completed_step_ids: List[Any] = []   # [W2] 本批真落地的 complete_step（步骤边界判据）
                for call in meta_calls:
                    mname = call.get("name")
                    # Meta calls run before the physical batch. While a retrieval
                    # failure is pending, neither completion results nor remembered
                    # facts may solidify an unverified value, whatever the batch holds.
                    if mname in {"complete_step", "remember"} and retrieval.unresolved:
                        blocked_action = "step completion" if mname == "complete_step" else "fact recording"
                        responses.append({"name": mname, "_failed": True, "_blocked": True,
                                          "response": (
                                              f"System Error: {blocked_action} blocked pending retrieval evidence. "
                                              "No completion or fact was recorded. First retrieve the source, then "
                                              f"call {mname} in a later batch with the verified result.")})
                        continue
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
                            # [W2] complete_step 真落地（有当前步可完成；被证据门挡下的已在上面 continue）
                            completed_step_ids.append(cur.get("id"))
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
                # D44：注册表先按工具声明核对实参（缺必填 / 未知键）；catalog 模式下回执附该工具完整
                # 声明（前缀里只有名字 + 一句摘要），重发只需再一次迭代、不必再取 schema 或靠猜。
                # 该工具的完整声明本就在本次前缀里（core / 路由件）→ 不重复附，给 full 模式尾句。
                _schema_fb = (self._precheck_schema_feedback(session)
                              if self.tool_schema_mode == "catalog" else None)
                # 打回的调用照样是**失败的调用**（F1）：进下方失败扫描（unresolved_failure / 批次失败 →
                # failure_swallow_nudge、⑤′-3 升级计数），并登记去重指纹——原样重发直接按重复调用拦。
                # 没执行任何东西 → `_safe`：压缩器不把它当写屏障。
                tracker = task.recent_calls if task is not None else no_task_calls
                _decl_order = list(phys_calls)
                _rejected: Dict[int, Dict[str, Any]] = {}
                validated: List[Dict[str, Any]] = []
                for c in phys_calls:
                    verr = (self.registry.validate_call(c.get("name"), c.get("arguments", {}),
                                                        schema_feedback=_schema_fb)
                            if c.get("type") != "error" else None)
                    if verr:
                        _cname, _cargs = c.get("name"), c.get("arguments", {})
                        _fp = (("r:" if self.registry.is_read_only(_cname, _cargs) else "w:")
                               + call_fingerprint(_cname, _cargs))
                        if _fp in tracker:
                            events.emit("duplicate_call", session=session.session_id, tool=_cname)
                            _r = {"name": _cname, "response": DUPLICATE_CALL_BLOCKED,
                                  "_failed": True, "_safe": True}
                        else:
                            tracker.append(_fp)
                            _probs = getattr(self.registry, "argument_problems", None)
                            _miss, _unk = (_probs(_cname, _cargs) if callable(_probs) else ([], []))
                            events.emit("tool_error", session=session.session_id,
                                        tool=_cname, timeout=False,
                                        msg=str(verr)[:160], kind="precheck",
                                        schema_fed="\nFull declaration of '" in str(verr),
                                        missing=",".join(list(_miss)[:PRECHECK_LIST_MAX]),
                                        unknown=",".join(list(_unk)[:PRECHECK_LIST_MAX]))
                            _r = {"name": _cname, "response": verr, "_failed": True, "_safe": True}
                        _rejected[id(c)] = _r
                        responses.append(_r)
                    else:
                        validated.append(c)
                phys_calls = validated

                # A failed knowledge-base retrieval cannot be replaced with model-
                # invented delivered content. Block known-unverified delivery before
                # the single approval gate; keep the proposed calls in the audit trail.
                # A delivery preceded by a retrieval in this batch is deferred to
                # _execute_physical, which flushes that retrieval and re-checks.
                if retrieval.unresolved:
                    permitted = []
                    retrieval_before = False
                    for c in phys_calls:
                        if c.get("type") == "error":
                            permitted.append(c)
                            continue
                        if (not retrieval_before
                                and is_delivery(self.registry, c)):
                            responses.append({"name": c.get("name"), "response": DELIVERY_BLOCKED,
                                              "_failed": True, "_blocked": True})
                            continue
                        if retrieval_target(c.get("name"), c.get("arguments") or {}):
                            retrieval_before = True
                        permitted.append(c)
                    phys_calls = permitted

                # [D2 谎报围堵：拒批同参拦截] 本轮已被用户拒绝的调用原样重发 → 不再弹窗，
                # 直接打回（26B 实案：nohup 同命令被拒 2 次仍连发 4 次，用户陷入拒批拉锯）。
                denied_fps = getattr(session, "denied_call_fps", set())
                if denied_fps:
                    redenied = [c for c in phys_calls
                                if c.get("type") != "error"
                                and call_fingerprint(c.get("name"),
                                                     c.get("arguments", {})) in denied_fps]
                    if redenied:
                        events.emit("denied_resend_blocked", session=session.session_id,
                                    tools=[c.get("name") for c in redenied])
                        for c in redenied:
                            phys_calls.remove(c)
                            responses.append({
                                "name": c.get("name"),
                                "response": ("Denied-call blocked: the user ALREADY denied this exact "
                                             "call this turn. Re-sending it unchanged is not allowed. "
                                             "Change your approach, or explain in your reply what you "
                                             "wanted to do and let the user decide."),
                            })

                # [审批闸门] 物理调用中含敏感调用：先落元工具结果，再整批挂起
                # （按调用动态判定：同一 shell 工具只读命令放行、变更命令弹窗；
                #  用户的 read/write 审批策略在此生效，见 core/approval_policy.py）
                _policy = self._approval_policy_for(journal)
                approval_needed = [
                    c for c in phys_calls
                    if c.get("type") != "error"
                    and self._gate_requires_approval(c.get("name"), c.get("arguments", {}), _policy)
                ]
                if approval_needed:
                    return _suspend_for_approval(phys_calls, responses, approval_needed)

                phys_responses = await self._execute_physical(session, phys_calls, tracker,
                                                              parse_errors, journal=journal,
                                                              retrieval=retrieval)
                # [D2 谎报围堵] 按声明序扫描本批结果：失败置位、其后任何成功清位；
                # 成功的变更类调用记账（收尾时"零变更却声称已做"的判据）。
                _batch_failed = _batch_ok = False
                # 声明序扫：执行了的调用 + 预检打回的调用（失败，见上）；证据门 / 拒批拦下的照旧不扫
                _executed = {id(c): r for c, r in zip(phys_calls, phys_responses)}
                _scan = [(c, _rejected[id(c)] if id(c) in _rejected else _executed[id(c)])
                         for c in _decl_order if id(c) in _rejected or id(c) in _executed]
                for c, r in _scan:
                    if c.get("type") == "error":
                        continue
                    rtext = str(r.get("response") or "")
                    if r.get("_cloud_degraded"):
                        # ⑤′-1：云端导师降级不是"吞掉的失败"——按设计本地作答、禁止重试，
                        # 收尾由编排层确定性标注；不进三态（既不是失败也不是成功变更），
                        # 否则失败吞没推回会逼模型"修复重试"并把本轮降级成 warning
                        continue
                    if _obs_failed(r):
                        unresolved_failure = True
                        # C4 retrieval blocks (`_blocked`) are guard refusals, not tool
                        # failures: they must not drive the ⑤′-3 consult escalation.
                        if not r.get("_blocked"):
                            _batch_failed = True
                        continue
                    unresolved_failure = False
                    _batch_ok = True
                    # 拦截/取消类响应 = 没真执行，不算成功变更
                    executed = not rtext.startswith(("Duplicate call blocked",
                                                     "Operation cancelled",
                                                     "Operation DENIED",
                                                     "Denied-call blocked"))
                    if executed and _is_local_mutation(self.registry, c):
                        mutated_ok_this_turn = True
                responses.extend(phys_responses)
                # ⑤′-3 失败计数按**批次**（一次迭代 = 模型一次自纠机会）：批内有成功即归零
                if _batch_ok:
                    upgrade_failures = 0
                elif _batch_failed:
                    upgrade_failures += 1
                _stamp_turn_stats()  # [仪表 Phase 0] 刷新废轮计数（_execute_physical 可能新增 parse 失败）
                # [验证门控 T1-4 状态跟踪] 按声明序扫描本批物理调用：成功改代码类文件（edit_file/
                # write_file，扩展名不在 _RECEIPT_IS_VERIFICATION_EXTS）置「未验证」，其后任何非写调用
                # （读回文件 / 跑代码 / grep 等）视为复查、清除标记。纯内容写（成功回执即验证）与失败写
                # （什么都没改，归 D2 失败吞没门控管）是中性的：既不武装也不清除（2026-11-07 修：此前
                # 任何写都武装，逼模型回读刚写的 .md、白烧步数）。
                # 写屏障（_execute_physical）保证写后重读不会被去重拦截，故复查总能真正执行；
                # 声明序与批内实际执行序一致（串行写之前会先冲刷此前积压的只读批）。
                for c, r in zip(phys_calls, phys_responses):
                    # A C4-blocked call never ran: it neither wrote nor re-checked.
                    if c.get("type") == "error" or r.get("_blocked"):
                        continue
                    if c.get("name") in _FILE_WRITE_TOOLS:
                        if _arms_verify_gate(c, r):
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
                _batch_idx = len(session.history)   # [W2] 本批 tool 消息将落在的下标
                if responses:
                    session.add_tool_message(responses)
                # [W2 步骤边界压缩] 本批有 complete_step 真落地（下一次生成之前）：档位够（块级前缀缓存
                # trigger_level / 不可裁缓存 cold_refill_trigger_level）就把本轮已完成步骤段 [轮起点, 本批
                # tool 消息) 的观察折成桩——本批同批的物理观察模型还没看过，不碰，归下一边界再压；无论压没压，
                # 步骤锚前移到本批 tool 消息下标（hard 钩子范围 [轮起点, 锚) 因此同样不含本批）。被证据门挡下
                # 的 complete_step 与 step_failed 路由不算边界（模型可能要回看观察重试）。
                # step_id 取本批最后一个落地的 complete_step。
                if completed_step_ids:
                    _sc_end = _batch_idx
                    if step_compaction.should_compact(
                            self.step_compaction, self._block_prefix_cache(),
                            getattr(session, "turn_context_guard_level", None)):
                        _t0 = getattr(session, "turn_history_start_idx", None)
                        _t0 = _t0 if isinstance(_t0, int) and 0 <= _t0 <= _sc_end else 0
                        self._step_compact(session, _t0, _sc_end, trigger="step",
                                           step_id=completed_step_ids[-1])
                    session.turn_step_start_idx = _sc_end

                # [断环] 工具格式反复解析失败（同一坏调用重复，或本轮累计过多）→ 优雅终止，
                # 避免烧光整个迭代预算后输出无关内容（升级纠错已在 _execute_physical 中给过机会）
                if parse_errors and (
                    max(parse_errors.values()) >= self.max_parse_retries
                    or sum(parse_errors.values()) >= self.max_parse_retries + 2
                ):
                    session.set_activity("已终止（工具格式反复解析失败）", phase="done")
                    events.emit("turn_abort", session=session.session_id, reason="parse_loop")
                    _exit_audit()
                    return {
                        "status": "warning",
                        "response": _with_retrieval_note(
                            "多次未能解析要调用的工具格式，已终止本次任务。"
                            "可以试试：①原样重发一次；②把需求说得更简单具体些；"
                            "③把任务拆成几个更小的步骤分别提出。"),
                        "thoughts": "System: repeated tool-call parse failures; aborted to avoid a loop.",
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
                                    "directly. Either way, first save with remember any value from "
                                    "these results that you will need later."),
                    })
                # ⑤′-3 失败触发：本地连续失败达阈 → 建议/代发起云端咨询（一轮一次，云端不可用不打扰）。
                # 放在本分支末尾：验证门控状态、结构性提醒、观察落历史、解析断环都已照常走完
                from ..cloud.upgrade import CONSULT_ACTIONS as _CONSULT_ACTIONS
                if (not any(d.action in _CONSULT_ACTIONS for d in upgrade_decisions)
                        and not _already_consulted(session.history)):
                    from ..cloud.upgrade import (decide_on_failures, failure_context, model_hint,
                                                 synthetic_consult_call)
                    _fd = decide_on_failures(upgrade_failures, self._effective_upgrade_cfg(journal),
                                             cloud_cfg=self.cloud_cfg,
                                             consult_registered=self._consult_registered())
                    if _fd is not None:
                        upgrade_decisions.append(_fd)
                        events.emit("upgrade_triggered", session=session.session_id, **_fd.as_event())
                        journal.emit("upgrade_triggered", _fd.as_event())
                        if _fd.action == "consult_suggest":
                            pending_tail_notes.append(model_hint(_fd))
                        elif _fd.action == "consult_auto":
                            _calls = [synthetic_consult_call(self._last_user_facts(session).text,
                                                             failure_context(responses))]
                            _verr = self.registry.validate_call(_calls[0]["name"], _calls[0]["arguments"])
                            if _verr:
                                print(f"[Orchestrator] 升级策略代发起被参数预检拒绝：{_verr}")
                                continue
                            session.add_assistant_message(
                                tool_calls=_calls,
                                thoughts=(f"[System upgrade] 编排层代发起云端咨询（failures：{_fd.reason}）"
                                          "——仍需审批；云端只顾问，落地本地。"))
                            if self._gate_requires_approval(_calls[0]["name"], _calls[0]["arguments"],
                                                            self._approval_policy_for(journal)):
                                return _suspend_for_approval(_calls, [], _calls)
                            _resp = await self._execute_physical(session, _calls, tracker, parse_errors,
                                                                 journal=journal)
                            session.add_tool_message(_resp)
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
                # 任何只读复查 = 「已执行」被当成「已验证」。推回一次，要求先验证改动的**效果**是否真正
                # 解决用户报告的问题（2026-11-07 改文案：原文「re-read what you changed」逼模型回读刚写的
                # 文件，回执已证明落盘、回读证明不了更多，只白烧步数）。仅在正常收尾路径触发（计划未完成的纯文本 = 向用户提问/
                # 汇报障碍，不适用）；推回有界，模型坚持不验证则放行其答复。
                wrapping_up = task is None or not task.plan or task.all_done()
                if wrapping_up and unverified_write and verify_nudges < VERIFY_NUDGE_MAX:
                    verify_nudges += 1
                    _transition("verify_nudge")
                    events.emit("verify_nudge", session=session.session_id)
                    session.add_assistant_message(content=text, thoughts=thoughts)
                    # 推回走可变尾部（T3-c）：一次性提醒，用完即弃、不进历史、不被摘要压缩
                    pending_tail_notes.append(
                        "[System guardrail] You modified state this turn (write tool calls) and then "
                        "wrapped up without any real check. The write receipts already confirm the file "
                        "contents landed, so do NOT re-read the files you just wrote — that proves nothing "
                        "more. Do NOT tell the user it is done/fixed yet. If no real check is feasible "
                        "here (nothing to execute), answer now and say explicitly what you could not "
                        "verify and what the user should test — do not claim success. Otherwise verify "
                        "the EFFECT first: run the real check (execute the code, run the test or "
                        "command), follow the call path from the entry point to your change to confirm "
                        "it is actually reached and takes effect, and check that it resolves the user's "
                        "ORIGINAL problem (not just a plausible nearby issue). If verification shows the "
                        "fix is wrong or incomplete, keep working.")
                    continue

                # [D2 谎报围堵：错误不可吞] 最近的物理调用失败且其后再无成功调用，却要收尾
                # ——推回一次：要么修复重试、要么在答复里明说失败。仍坚持则放行但降级 warning
                # （26B 实案：heredoc 写炸 exit 1 后直接宣布"三个文件都建好了"）。
                if wrapping_up and unresolved_failure and failure_nudges < 1:
                    failure_nudges += 1
                    _transition("failure_swallow_nudge")
                    events.emit("failure_swallow_nudge", session=session.session_id)
                    session.add_assistant_message(content=text, thoughts=thoughts)
                    pending_tail_notes.append(
                        "[System guardrail] Your most recent tool call FAILED and nothing succeeded "
                        "after it. Do NOT report success. Either fix and retry now (a different, "
                        "correct approach), or state plainly in your reply that the operation failed "
                        "and what the error was.")
                    continue

                # [D2 谎报围堵：零证据动作声明] 本轮没有任何成功的变更类调用，收尾文本却声称
                # 完成了创建/安装/启动等动作——推回一次改写或真去执行；改写后不再命中即自愈
                # 放行（26B 实案：零调用声称"已用 nohup 在后台拉起"）。仍坚持则降级 warning。
                if (wrapping_up and not mutated_ok_this_turn and fabrication_nudges < 1
                        and _ACTION_CLAIM_RE.search(text)):
                    fabrication_nudges += 1
                    _transition("action_claim_nudge")
                    events.emit("action_claim_nudge", session=session.session_id)
                    session.add_assistant_message(content=text, thoughts=thoughts)
                    pending_tail_notes.append(
                        "[System guardrail] Your reply claims you performed actions (created/installed/"
                        "started/...), but NO state-changing tool call succeeded this turn. If the "
                        "action still needs doing, do it now with real tool calls. If you are "
                        "describing work from an EARLIER turn, or suggesting steps for the user, "
                        "reword to make that unmistakably clear. Never present unexecuted actions "
                        "as done.")
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
                    ok_goal, why = await self._goal_alignment_check(goal_src, text,
                                                                    usage_sink=turn_usage)
                    events.emit("goal_verify", session=session.session_id,
                                passed=ok_goal, reason=why)
                    # [F6] 审计那次生成刚记进 goal_verify 桶，立即刷进 metrics：放行
                    # return 这条出路走不到别的 stamp 点（推回 continue 下一迭代会补上,
                    # 但放行是常态路径）,不在这里刷,护栏的开销就落不了盘（病根 ②）。
                    _stamp_turn_stats()
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
                # 落盘视图（W1）：应急压缩桩会截掉观察头部之后的资产链接
                text = _append_missing_asset_links(
                    text, session.persistable_messages(turn_start_idx))
                # ⑤′-1 降级路：云端导师本轮不可用过 → 答复末尾明示「本地作答」，不静默装满血
                text = _finalize_cloud_degraded(session, metrics, text, append_notice=True)
                # ⑤′-3：换档建议 / 显式要求但云端不可用 → 收尾确定性提示（用户要自己动手的事）
                text = _append_upgrade_notices(text, metrics, upgrade_decisions)
                session.add_assistant_message(content=text, thoughts=thoughts)
                if task is not None and (task.all_done() or not task.plan):
                    # W4：弃之前留一份摘要器视图，随本轮记录落盘（摘要器的关键值逐字来源）
                    # D47-next (b)：纯 [Goal]（无 plan 无便签）不留，记录不盖 task_state
                    session.last_task_state_render = ("" if task.is_bare_goal()
                                                      else task.render_for_summary())
                    # 任务完成即弃：工作过程已沉淀在本轮消息流（blackboard=消息流本身）
                    session.task_state = None
                # [D2 谎报围堵] 推回后仍坚持吞错/零证据声明 → 放行但状态降级 warning
                # （汇报可疑要在轮次状态上留痕，前端和评测脚本都看得见）
                status = "success"
                if unresolved_failure and failure_nudges >= 1:
                    status = "warning"
                elif (not mutated_ok_this_turn and fabrication_nudges >= 1
                      and _ACTION_CLAIM_RE.search(text)):
                    status = "warning"
                # 未完成而输出文本 = 向用户提问/汇报障碍，任务跨轮保留，等用户答复继续
                return {"status": status, "response": text, "thoughts": thoughts, "metrics": metrics}

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
        # 本路必须 stamp（复审 Critical）:携带态越过阈值时 while 体一次都不进,
        # metrics 还是空 dict——恰恰是审批分段+子生成把这条路变成了易达路径,
        # 不落账等于整轮口径在自己造出的路径上失守
        _stamp_turn_stats()
        _sub_gens = turn_usage.get("sub_generations", 0)
        events.emit("turn_abort", session=session.session_id, reason="max_iterations",
                    sub_generations=_sub_gens)
        # 如实说明预算烧在哪：主循环之外另有多少子调研生成，用户据此才知道
        # 该拆任务还是该少调研（子生成有自己的保险丝,这里只如实附注）。
        _budget_detail = (f"（期间另有子调研生成 {_sub_gens} 次）" if _sub_gens else "")
        _exit_audit()
        return {
            "status": "warning",
            "response": _with_retrieval_note(
                f"任务在 {self.max_total_iterations} 次主循环迭代内未能收敛{_budget_detail}，已终止。"
                "可以试试：①把任务拆成几个更小的目标分别发起；②缩小范围（只处理一部分）；"
                "③告诉我可以跳过哪些环节。"),
            "thoughts": "System Warning: orchestration loop threshold exceeded.",
            "metrics": metrics,
        }

    # ---------- ⑤′-3 升级策略 ----------
    def _consult_registered(self) -> bool:
        names = getattr(self.registry, "tool_names", None)
        try:
            from ..cloud.degrade import CLOUD_CONSULT_TOOL
            return CLOUD_CONSULT_TOOL in (names() if callable(names) else set())
        except Exception:
            return False

    @staticmethod
    def _history_chars(history: List[Dict[str, Any]]) -> int:
        """历史文本字符数（用户消息按文本块、工具消息按观察、assistant 按正文），供长度类谓词粗估。"""
        n = 0
        for m in history:
            c = m.get("content")
            if isinstance(c, str):
                n += len(c)
            elif isinstance(c, list):
                n += sum(len(str(b.get("text") or "")) for b in c if isinstance(b, dict))
            for tr in m.get("tool_responses") or []:
                n += len(str((tr or {}).get("response") or ""))
        return n

    def _last_user_facts(self, session: ChatSession):
        from ..cloud.upgrade import facts_from_user_message
        from ..cloud.boundary import RequestFacts
        for msg in reversed(session.history):
            if msg.get("role") == "user":
                # 上下文长度粗估：历史文本字符 / 1.3（中文 ≈1.24 字/token；只供长度类谓词）
                return facts_from_user_message(msg, context_tokens=int(self._history_chars(session.history) / 1.3))
        return RequestFacts()

    @staticmethod
    def _approval_policy_for(journal: TurnJournal):
        """本轮审批闸门的有效策略（Contract P）。无人值守轮（turn_origin != "chat"，
        定时任务 origin=schedule；与 _effective_upgrade_cfg 同一判据）恒用 DEFAULT：
        不用 "auto"（没人在场，放开不安全），也不用 "ask"（没人能答，read=ask 会让每次
        定时只读调用被自动拒绝、整轮被拒批保险丝掐断）——即 build 77 的工具自身判定。"""
        if getattr(journal, "turn_origin", "chat") != "chat":
            return approval_policy.DEFAULT
        return approval_policy.current()

    def _gate_requires_approval(self, name: str, arguments: Dict[str, Any], policy) -> bool:
        """闸门判定：只有已注册工具进入审批判定；未注册名（模型幻觉）不弹审批卡，
        落回原有执行错误路径（"No tool named …"，build 77 行为）。
        registry.approval_required 本身对未知工具仍 fail-closed（API 调用方用）。"""
        names = getattr(self.registry, "tool_names", None)
        if callable(names) and name not in names():
            return False
        return self.registry.approval_required(name, arguments or {}, policy)

    def _effective_upgrade_cfg(self, journal: TurnJournal):
        """无人值守轮（定时任务 origin=schedule）没人点审批：auto 降为 suggest，免得代发起的
        审批被 runner 自动拒绝并计入拒批保险丝（评审 m19）。"""
        cfg = self.upgrade_cfg
        if cfg.mode == "auto" and getattr(journal, "turn_origin", "chat") != "chat":
            from dataclasses import replace
            return replace(cfg, mode="suggest")
        return cfg

    def _upgrade_at_turn_start(self, session: ChatSession, journal: TurnJournal) -> List[Any]:
        """轮首决策 + 账本/事件记录（决策本身在 cloud.upgrade，纯函数）。"""
        from ..cloud.upgrade import decide_at_turn_start
        if _already_consulted(session.history):
            return []
        facts = self._last_user_facts(session)
        names = getattr(self.registry, "tool_names", None)
        ds = decide_at_turn_start(facts, self._effective_upgrade_cfg(journal), cloud_cfg=self.cloud_cfg,
                                  consult_registered=self._consult_registered(),
                                  registered_tools=set(names()) if callable(names) else None)
        for d in ds:
            events.emit("upgrade_triggered", session=session.session_id, **d.as_event())
            journal.emit("upgrade_triggered", d.as_event())
            print(f"[Orchestrator] 升级策略触发：{d.trigger}/{d.action} {d.reason}")
        return ds

    # ---------- 元工具处理（计划是数据，操作有校验） ----------
    def _handle_meta_call(self, session: ChatSession, call: Dict[str, Any]) -> str:
        name = call.get("name")
        args = call.get("arguments") or {}
        if not isinstance(args, dict):
            return "Invalid arguments: expected a JSON object."

        if name == "update_plan":
            task = session.task_state
            if task is None:
                # 兜底：D47 起新鲜轮轮首已种任务，这里只剩续跑 / 旧会话无任务的情形；只建不路由
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
            # D31（D47 改写）：任务路由在首次建成 plan 后的下一次生成前算一次并冻结
            # （`_tools_decl_for`，只在 catalog 模式写；full 模式保持 None）；goal 不变故
            # replan 不重算；apply_op 失败 plan 仍空 → 不路由。
            return msg

        if name == "complete_step":
            task = session.task_state
            if task is None or not task.plan:
                return "No active plan. Use update_plan to create one, or just answer the user."
            ok, msg = task.complete_current(str(args.get("result", "")))
            return msg

        if name == "remember":
            task = session.task_state
            # D47：新鲜轮轮首恒有任务（无 plan 也收）；只剩续跑 / 旧会话无任务时拒
            if task is None:
                return "No active task state — just answer the user."
            ok, msg = task.remember(str(args.get("note", "")))
            return msg

        if name == "get_tool_schema":
            names = args.get("names") or []
            if not isinstance(names, list) or not names:
                return "Invalid arguments: 'names' must be a non-empty list of tool names."
            by_name = {s.get("name"): s for s in self.registry.get_all_schemas()}
            found = [by_name[n] for n in names if n in by_name]
            missing = [n for n in names if n not in by_name]
            parts = self._render_tool_declarations(found) if found else []
            if missing:
                parts.append(f"Unknown tools (check the catalog for exact names): {missing}")
            return "\n".join(parts)

        return f"Unknown meta tool: {name}"

    def _render_tool_declarations(self, found: List[Dict[str, Any]], *,
                                  source: str = "get_tool_schema") -> List[str]:
        """工具声明的模型面渲染（get_tool_schema 与 D44 预检附声明共用一条路）。

        D0①:详情按**方言声明格式**回喂——回裸 JSON 是教模型一种它在稳定前缀里没见过的形状,
        再要求它按第三种形状(调用 DSL)发实参。方言未实现渲染器时回退 JSON(fail-open,功能
        可用只是次优;整批一段 json.dumps(found))。"""
        render = getattr(getattr(self.engine, "dialect", None), "render_tool_declaration", None)
        rendered = None
        if callable(render):
            try:
                # 先整体渲染进局部列表再并入(复审 Critical):对着 parts 直接 extend 生成器,
                # 第 N 件渲染炸掉时前 N-1 件已经进了 parts,再补整段 JSON 就是「半 DSL 半 JSON
                # 还重复」的观察
                rendered = [str(render(t)) for t in found]
            except Exception as e:
                # fail-open 也要留痕(复审):方言渲染器永久坏掉的话,每次取详情都在静默降级,
                # 无痕则永远查不到
                events.emit("tool_schema_render_fallback", error=f"{type(e).__name__}: {e}",
                            source=source)
                print(f"[Orchestrator] ⚠️ {source} 方言渲染失败,回退 JSON: {e}")
                rendered = None
        return rendered if rendered is not None else [json.dumps(found, ensure_ascii=False)]

    def _tool_declaration_text(self, name: str) -> str:
        """单个工具的完整声明（D44 catalog 模式预检回执附带；与 get_tool_schema 同一渲染）。
        注册表声明里找不到该名 → 空串（validate_call 退回 full 模式尾句）。"""
        by_name = {s.get("name"): s for s in self.registry.get_all_schemas()}
        if name not in by_name:
            return ""
        return "\n".join(self._render_tool_declarations([by_name[name]], source="precheck"))

    def _precheck_schema_feedback(self, session: ChatSession):
        """catalog 模式预检的 schema_feedback 回调：该工具的完整声明已在本次前缀里（core / 路由件，
        `_tools_decl_for` 里带 parameters 的条目）→ 空串（= full 模式尾句，不重复喂同一份声明）；
        否则 = _tool_declaration_text。前缀集合按需（首次打回时）取一次。"""
        full: List[set] = []

        def _fb(name: str) -> str:
            if not full:
                full.append({d.get("name") for d in self._tools_decl_for(session)
                             if isinstance(d, dict) and "parameters" in d})
            return "" if name in full[0] else self._tool_declaration_text(name)
        return _fb

    # ---------- 收尾目标校验（想法3 完整版：任务级独立审计，串行、每轮最多一次） ----------
    async def _goal_alignment_check(self, goal: str, answer: str,
                                    usage_sink: Optional[Dict[str, int]] = None
                                    ) -> "tuple[bool, str]":
        """一次无工具小生成审计「最终答复是否兑现原始目标」。返回 (通过?, 理由)。

        成本刻意收敛：只喂 goal+答复（不带全历史），max_tokens 小、思考关闭；
        engine.generate_once 内部持有推理锁，与主循环天然串行（不违反单 worker）。
        任何异常/歧义一律 fail-open 放行——审计是安全网，绝不能反过来卡死收尾。

        usage_sink：本轮的用量槽，由调用处（run_turn）显式传入——本方法是 Orchestrator
        的方法而非工具，走参数而不是全局/ContextVar。它记进 goal_verify 桶：**护栏的
        开销要可见（口径 ②），但不计入熔断（口径 ③）**。
        """
        backend = getattr(self.engine, "backend", None)
        if backend is None:
            return True, "verifier unavailable"

        # 惰性导入：core 不静态依赖 agents（同 :989 的 agents.runtime 先例）。
        from ..agents.sandbox_runtime import EphemeralSession, note_usage

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
            note_usage(usage_sink, generations=1, bucket="goal_verify",
                       generation_tokens=(r.get("metrics") or {}).get("generation_tokens", 0) or 0)
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
        # 2026-11-07 改文案：明说写回执已证明落盘、别为确认写入而回读（原文被模型读成「去回读」）。
        if did_complete_step and task.all_done() and len(task.plan) >= 3:
            goal = (task.goal or "").strip()
            goal_ref = f" the user's ORIGINAL goal (\"{goal[:120]}\")" if goal else " the user's ORIGINAL goal"
            return ("[Reminder] All plan steps are marked done. Before giving the final answer, "
                    "re-read" + goal_ref + " and check your result actually ACHIEVES it — not just a "
                    "nearby thing you drifted into. Also confirm the key deliverables are truly "
                    "verified — for code, by a real check that actually ran (\"executed\" is not "
                    "\"verified\"); successful write receipts already prove file contents landed, so "
                    "do not re-read files just to confirm writes. If it falls short, keep working "
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
                                *, journal: TurnJournal,
                                retrieval: Optional[RetrievalEvidence] = None
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
            if retrieval is not None:
                retrieval.observe([c for (_, c, _) in safe_batch], results)
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
                hint_fn = getattr(getattr(self.engine, "dialect", None),
                                  "parse_error_retry_hint", None)
                hint = hint_fn() if callable(hint_fn) else _PARSE_HINT_FALLBACK
                by_idx[idx] = {"name": "system",
                               "response": parse_error_feedback(call, n, hint)}
                continue

            name = call.get("name")
            args = call.get("arguments", {})

            # A delivery always waits for the retrievals queued before it in this
            # batch (RAG and render_chart are both concurrency-safe): serialize, do
            # not block. Only then, and only while a failure is pending, is it held
            # back. A delivery ahead of every retrieval with nothing pending runs as
            # before. A blocked call is never executed and never takes a fingerprint.
            if retrieval is not None and is_delivery(self.registry, call):
                # (a) let queued retrievals complete (or fail) first, unconditionally
                if any(
                        retrieval_target(c.get("name"), c.get("arguments") or {})
                        for (_, c, _) in safe_batch):
                    await flush_safe_batch()
                # (b) do not reorder tools or deliver ahead of a later retrieval
                if retrieval.unresolved and any(
                        retrieval_target(c.get("name"), c.get("arguments") or {})
                        for c in calls[idx + 1:] if c.get("type") != "error"):
                    by_idx[idx] = {"name": name, "_failed": True, "_blocked": True,
                                   "response": (
                                       "System Error: delivery blocked until the later retrieval in this batch "
                                       "has completed. This operation was NOT executed. First retrieve the "
                                       "source, then deliver in a later batch using verified evidence.")}
                    continue
                # (c) still unresolved
                if retrieval.unresolved:
                    by_idx[idx] = {"name": name, "response": DELIVERY_BLOCKED,
                                   "_failed": True, "_blocked": True}
                    continue

            # [死循环拦截] 同名同参调用直接打回（最常见的循环形态）。指纹按序登记并带
            # r:/w: 前缀区分只读与写：写调用执行后清掉 r: 指纹（写屏障，与 T3-b 同思路）——
            # 「读→改→再读验证」的复读放行，重复写调用（重复发信/重复删除）仍被拦。
            read_only = self.registry.is_read_only(name, args)
            fingerprint = ("r:" if read_only else "w:") + call_fingerprint(name, args)
            if fingerprint in tracker:
                events.emit("duplicate_call", session=session.session_id, tool=name)
                by_idx[idx] = {"name": name, "response": DUPLICATE_CALL_BLOCKED}
                continue
            tracker.append(fingerprint)

            call_id = uuid.uuid4().hex[:8]
            if self.registry.is_concurrency_safe(name, args):
                safe_batch.append((idx, call, call_id))
            else:
                # 有副作用/有状态调用：先冲刷积压的只读批（保证它在这些安全调用之后执行），再串行跑
                await flush_safe_batch()
                by_idx[idx] = await self._invoke_tool(session, call, call_id, journal)
                if retrieval is not None:
                    retrieval.observe([call], [by_idx[idx]])
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

        def _tool_progress(label: str, detail: str = "", code: Optional[str] = None,
                           args: Optional[dict] = None) -> None:
            session.set_activity(label + (f"（{detail}）" if detail else ""))
            payload = {"call_id": call_id, "label": label, "detail": detail}
            # build 84：可选的稳定代号 + 扁平参数（只增不改，旧客户端忽略未知字段），
            # 客户端据此按界面语言渲染；label/detail 仍是原样中文文本（见 progress_codes）。
            if isinstance(code, str) and code:
                try:
                    from ..agents.progress_codes import clean_args
                    payload["code"] = code
                    payload["args"] = clean_args(args)
                except Exception:
                    # clean_args 对非法 args（如非映射类型）可能抛；退回不带 code/args 的
                    # 旧式事件，保证事件本身不丢（见 test_progress_codes.py）。
                    payload.pop("code", None)
                    payload.pop("args", None)
            loop.call_soon_threadsafe(journal.emit, "tool_progress", payload)

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
            if name == _CLOUD_CONSULT_TOOL:
                # 云端咨询被工具级熔断 = 同样是降级（阶段复审 m8：否则绕过整条 ⑤′-1 标注路）
                from ..cloud.degrade import degraded_observation
                result_text = degraded_observation(
                    "timeout", f"tool-level timeout after {timeout_s}s（L0 脱敏 + HTTP 串行超出熔断）")

        # [事件回流] 工具级错误反馈（含超时）也是涣散痕迹：参数马虎/路径不存在/匹配失败等
        # 由工具自身拦下的错误，与编排层护栏一起构成完整事件流（涣散度量的全部组分入流）。
        errored = str(result_text).lstrip().startswith("System Error")
        if errored:
            events.emit("tool_error", session=session.session_id, tool=name,
                        timeout=timed_out, msg=str(result_text)[:160])
        # ⑤′-1：云端导师降级观察（工具名 + 前缀常量双锚）。账本事件在 tool_result 之后发
        # （PROTOCOL：紧随对应 tool_result）；收尾标注另由 _cloud_degraded_scan 扫历史
        from ..cloud.degrade import degraded_kind as _degraded_kind_of   # core 不静态依赖 cloud
        degraded = _degraded_kind_of(result_text, name)

        # [D2 谎报围堵] 失败事实在此单点判定，随观察以 `_failed` 挂出（见 _obs_failed）。
        # 必须在下面的观察截断**之前**算：截断会改写文本，判据不能建在被改写的文本上。
        # 三个来源取并：工具层显式报错（errored，比正则多容忍前导空白——缩进过的真错误
        # 不能漏）、超时熔断（只有本层知道）、shell 非零退出码（正则，良性码已由
        # shell_exec 打标排除）。
        #
        # 已知边界（§6.2 修不到的那一半）：工具的失败约定就是「响应 0 位打 System
        # Error:」，因此**观察首行恰好就是该串**时仍判失败——read_file 读到一份以这行
        # 开头的日志，与工具自己报错，在文本层不可分。根治要工具带外结构化报错
        # （registry.execute_tool 返回失败标志而非文本前缀），另立项。去掉 re.M 已经
        # 消掉了发生率高得多的那一半（错误行在第 2 行及以后）。
        failed = errored or timed_out or _resp_failed(result_text)
        retrieval_fact = retrieval_observation(name, args, result_text, render, failed)

        # [观察截断] 保护每步延迟与上下文（护栏参数，见 config agent.observation_max_chars）
        # 注：截断只作用于给模型的文本。渲染载荷由工具在结构尚在时另行产出（ToolOutput），
        # 从不经过这里——故不存在"从半截文本里投影出坏图表"的可能。
        # 保头 2/3 + 尾 1/3（grok-build 评估采纳）：shell/日志类输出的错误行多在尾部，
        # 只保头会把最有用的部分砍掉；中缝标注原始体积 + 收窄取回的出路（S.3：给路）。
        if len(result_text) > self.observation_max_chars > 0:
            # 落盘走线程：最坏 4M 字符（CJK 编码后约 12MB）的同步写会卡住事件循环，
            # 而上面工具执行本身就是 to_thread 出去的——同一处不该一半在循环里写盘。
            spill = await asyncio.to_thread(
                spill_observation, session, call_id, result_text)
            result_text = truncate_observation(
                result_text, self.observation_max_chars, spill)
        # 实时反馈：记录一条已完成的工具步骤（结果取首行摘要）
        summary = result_text.strip().splitlines()[0] if result_text.strip() else ""
        session.push_activity("tool", name, summary)
        journal.emit("tool_result", {"call_id": call_id, "name": name,
                                     "response": result_text,
                                     "status": "timeout" if timed_out else "ok"})
        if degraded:
            events.emit("cloud_degraded", session=session.session_id, name=name, kind=degraded)
            journal.emit("cloud_degraded", {"call_id": call_id, "name": name, "kind": degraded})
        # 挂上压缩层用的内存元数据（T3-a/b）：_safe=只读可取代、_target=取代键（同工具同目标去重）。
        # 均为 `_` 前缀，落盘时被 append_log 剥离（与 _compacted 同规矩）；`_failed` 同规矩。
        # 注意「剥离」在两个面各自发生：持久化面由 `_strip_internal_meta` 清 tool_responses,
        # 下发面由 `public_turn` **递归**全剥（B2 起）——这几个键不再随 turn_ended 帧下发。
        # 模型 prompt 面另有白名单（方言 `_MODEL_VISIBLE_KEYS`）挡着，不影响 KV 前缀字节。
        # 判据用 is_read_only（而非 is_concurrency_safe）：read_ui_tree 这类「只读但不可并发」
        # 的观察最大、最需要取代去重，不能因退出并发批而失去 _safe。
        obs = {"name": name, "response": result_text, "_failed": failed}
        if retrieval_fact is not None:
            obs["_retrieval"] = retrieval_fact
        if degraded:
            obs["_cloud_degraded"] = degraded   # `_` 前缀：落盘剥离；收尾标注扫的是文本前缀
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

        # [F6] 先清过期用量槽（复审）:本方法在 run_turn **之前**执行已批准的工具,而
        # ContextVar 里可能还躺着同一 Task 上一段 run_turn 的 sink（定时轮 runner 在
        # 一个 Task 里连跑 run_turn→resume_after_approval）——不清的话,这里执行的
        # 沙箱工具会把用量记进上一段的孤儿 dict。今天没有审批门控的沙箱工具,这是
        # 防御带;哪天有了,正确做法是把 carried 的 usage 在这里 set 而不是 None。
        from ..agents import runtime as _usage_runtime
        _usage_runtime.set_usage_sink(None)

        resume_mutated_ok = False
        resume_failure = False
        if approved:
            task = session.task_state
            # 无计划的批准调用也要进入整轮重复屏障，随后run_turn从同一携带态恢复。
            tracker = task.recent_calls if task is not None else list(
                session.carried_turn_stats.get("no_task_calls") or [])
            # 批准的敏感调用同样经账本发事件（修复旧实现漏发 tool_started/result 的洞）
            retrieval = RetrievalEvidence(session.carried_turn_stats.get("retrieval_evidence"))
            responses = await self._execute_physical(session, pending_calls, tracker, {},
                                                     journal=journal, retrieval=retrieval)
            session.carried_turn_stats["retrieval_evidence"] = retrieval.checkpoint()
            if task is None:
                session.carried_turn_stats["no_task_calls"] = tracker
            # [D2 谎报围堵] 续跑轮的三态起点必须带上刚执行的批准调用（否则续跑里的
            # "已执行完毕"会被零证据声明门误推）——判据与主循环扫描完全一致
            for c, r in zip(pending_calls, responses):
                if c.get("type") == "error":
                    continue
                rtext = str(r.get("response") or "")
                if r.get("_cloud_degraded"):
                    continue      # ⑤′-1：同主循环，降级不进三态
                if _obs_failed(r):
                    resume_failure = True
                    continue
                resume_failure = False
                if not rtext.startswith(("Duplicate call blocked", "Operation cancelled",
                                         "Operation DENIED", "Denied-call blocked")) \
                        and _is_local_mutation(self.registry, c):
                    resume_mutated_ok = True
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
            # [D2 谎报围堵] 登记拒批指纹（按轮生效，续跑经 keep_recent_calls 沿用）：
            # 原样重发不再弹窗，直接在审批闸前打回
            fps = getattr(session, "denied_call_fps", None)
            if fps is None:
                fps = session.denied_call_fps = set()
            for call in pending_calls:
                if call.get("type") != "error":
                    fps.add(call_fingerprint(call.get("name"), call.get("arguments", {})))

        session.add_tool_message(responses)
        # 刚批准执行的调用含改文件工具 → 续跑的 run_turn 以「未验证写」状态起步（T1-4）：
        # 文件写恰是最不该「改完即宣布成功」的一类，续跑不能丢失这个标记。判据与主循环共用
        # `_arms_verify_gate`（成功写到非纯内容文件；不用对 shell 无效的 is_read_only）。
        wrote_unverified = approved and any(
            _arms_verify_gate(c, r)
            for c, r in zip(pending_calls, responses) if c.get("type") != "error"
        )
        # 回到编排主循环（若再次命中敏感工具会再次挂起）。keep_recent_calls：保留刚执行的
        # 批准调用指纹，防模型在续跑首步立即原样重复该敏感调用。
        # compact_history=False：轮中重入，本轮未落盘观察不参与压缩（防压缩桩落盘）。
        return await self.run_turn(session, journal=journal,
                                   keep_recent_calls=True, compact_history=False,
                                   resume_unverified_write=wrote_unverified,
                                   resume_mutated_ok=resume_mutated_ok,
                                   resume_failure=resume_failure, **gen_kwargs)
