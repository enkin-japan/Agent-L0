"""升级策略（⑤′-3，2026-08-23）：编排层确定性护栏——什么时候把活交给云端导师/建议换档。

**护栏是代码**：三种触发、一张决策表、零模型判断；真正的出网仍走 `cloud_consult` 工具 →
审批闸（config cloud.require_approval）→ L0 出网守卫 → 台账，本模块一个字节都不出网
（铁律 6：云端只顾问，落地永远本地）。

触发（按优先级）：
1. **用户显式要求**（"问问云端/让 Claude 看看/ask the cloud advisor"）——用户已经做了决定；
2. **目录谓词命中**（`cloud.boundary.match`，自动档只认 confirmed，建议档含 weak）；
3. **本地连续失败 ≥ N**（同一轮内连续失败的物理调用数，编排层 D2 三态的计数版）。

动作：
- `cloud_consult`：`mode=auto` → 编排层**代模型发起**一次 cloud_consult（仍进审批闸，用户在
  审批卡上看到将出网的原文，拒批即不出网）；`mode=suggest` → 只给模型一条一次性提醒
  （尾注，不进历史），由它决定要不要调；`mode=off` → **整个护栏关闭**（不判、不提醒、不提示
  换档；用户显式要求时模型自己仍可调 cloud_consult 工具）。
- 一轮可同时得到一条 consult 决策和一条 tier 决策（两类不互斥），各至多一条。
- `switch_tier`：**永远只建议**给用户（定档 2026-08-23：64G 单档常驻，运行期动态换档已
  否决）——收尾答复末尾一行确定性提示，告诉用户用 `./devctl.sh model` 切档、是否要开思考。
- `tool_assist`：给模型一条一次性提醒（尾注）——把算术/检索这类本地模型做不稳的步骤交给确定性
  工具（如 run_math）。零换档、零出网、与 mode 无关（off 除外）；用户面不提示。
- `caveat`：只在收尾答复末尾给用户一行提醒（本档已知弱项，关键结果请核对）；不换档、不出网、
  不打扰模型。B1 图表读数用此（用户 08-23 拍板：64G 上为一题换整个生产档不划算）。
- 云端不可用（无钥/无供应商/配置非法，`degrade.availability`）：不发起；用户显式要求时在
  收尾答复说明原因（不静默）。

一轮只触发一次（审批续跑重入 run_turn 也不重触发：看历史里本轮是否已有 cloud_consult 调用）。
角色边界：云端导师 = 商用 API；Colab 与生产无关。
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .boundary import RequestFacts, match
from .degrade import CLOUD_CONSULT_TOOL, availability, _REASON_ZH

MODES = ("auto", "suggest", "off")

_EXPLICIT_RE = re.compile(
    r"(?:问问云端|问一下云端|让云端|交给云端|云端导师|用云端|请云端|"
    r"让\s*(?:GPT|ChatGPT|Claude|Gemini|Opus|Fable)\s*(?:看看|来|帮|分析|回答)|"
    r"(?<![A-Za-z])(?:ask|consult)(?: the)? (?:cloud (?:advisor|model)|external model)(?![A-Za-z_-])|"
    r"(?<![A-Za-z])(?:ask|consult)(?: the)? cloud(?=\s*(?:[?？。!！,，.:：;；]|$))|"
    r"(?<![A-Za-z])ask (?:Claude|GPT|ChatGPT|Gemini)(?![A-Za-z_-])|"
    r"(?<![A-Za-z])use the cloud (?:advisor|model)(?![A-Za-z_-])|"
    r"(?<![A-Za-z])cloud_consult(?![A-Za-z]))", re.I)
#: 显式**否定**（"不要问云端"/"don't ask the cloud"）：命中即不算显式要求（评审 m8）
_NEGATED_RE = re.compile(
    r"(?:不要|不用|别|不必|无需|不准|禁止|不想)\s*(?:去|再)?\s*(?:用|让|问|交给|请)?\s*(?:云端|GPT|ChatGPT|Claude|Gemini)|"
    r"(?<![A-Za-z])(?:don'?t|do not|never|without|no need to|stop)\s+(?:\w+\s+){0,2}(?:ask|consult|use)(?:ing)?\s+(?:the\s+)?(?:cloud|Claude|GPT|ChatGPT|Gemini)", re.I)


def _short(repo: Optional[str]) -> str:
    """./devctl.sh model 的短名（注册表 repo 末段）。"""
    return (repo or "").split("/")[-1]


@dataclass(frozen=True)
class UpgradeConfig:
    mode: str = "suggest"
    failure_threshold: int = 2       # 本地连续失败 ≥N 触发
    tier_notice: bool = True         # switch_tier 命中是否在收尾答复里提示用户

    @classmethod
    def from_cloud_section(cls, cloud: Optional[dict]) -> "UpgradeConfig":
        up = (cloud or {}).get("upgrade") or {}
        mode = str(up.get("mode", "suggest")).strip().lower()
        if mode not in MODES:
            raise ValueError(f"config cloud.upgrade.mode 非法: {mode!r}（可用: {MODES}）")
        n = int(up.get("failure_threshold", 2))
        if n < 1:
            raise ValueError("config cloud.upgrade.failure_threshold 须 ≥1")
        return cls(mode=mode, failure_threshold=n, tier_notice=bool(up.get("tier_notice", True)))


@dataclass(frozen=True)
class Decision:
    trigger: str                         # explicit / catalog / failures
    action: str                          # consult_auto / consult_suggest / tier_suggest / unavailable / none
    boundaries: List[str] = field(default_factory=list)
    tier_target: Optional[str] = None    # tier_suggest：目标档 repo；tool_hint：工具名（协议面同名字段，PROTOCOL 已注）
    tier_gen: Optional[Dict[str, Any]] = None
    reason: str = ""                     # 人/日志可读
    tier_cost: str = ""                  # 换档代价句（目录 action_cost；没实测就空 → 通用措辞）
    hint: str = ""                       # tool_assist：给模型的提醒原文（目录 action_hint）
    notice: str = ""                     # caveat：给用户的提醒原文（目录 action_notice）

    def as_event(self) -> Dict[str, Any]:
        return {"trigger": self.trigger, "action": self.action, "boundaries": list(self.boundaries),
                "tier_target": self.tier_target, "reason": self.reason}


def explicit_request(text: str) -> bool:
    t = text or ""
    return bool(_EXPLICIT_RE.search(t)) and not _NEGATED_RE.search(t)


CONSULT_ACTIONS = ("consult_auto", "consult_suggest", "unavailable")   # 会占用"本轮云端咨询"名额的决策


def decide_at_turn_start(facts: RequestFacts, cfg: UpgradeConfig, *,
                         cloud_cfg: Optional[dict] = None,
                         consult_registered: bool,
                         registered_tools: Optional[set] = None) -> List[Decision]:
    """轮首决策：consult 类 / tier 类 / tool_hint / caveat **各至多一条**（显式要求 > 目录命中）。
    空列表 = 不触发。`mode=off` 整个护栏关闭（含 tool_hint/caveat——它是"编排层往模型上下文里塞
    东西"的总开关，见 config 注）。tool_assist 的目标工具未注册（`registered_tools` 给了但不含）
    → 不出决策（不能命令模型去调一个不在清单里的工具）。"""
    if cfg.mode == "off":
        return []
    out: List[Decision] = []
    ok, kind, detail = availability(cloud_cfg)
    cloud_ok = ok and consult_registered
    hits = match(facts, include_weak=(cfg.mode != "auto"))
    consult_hits = [b for b in hits if b.action == "cloud_consult"]
    tier_hits = [b for b in hits if b.action == "switch_tier"]
    tool_hits = [b for b in hits if b.action == "tool_assist"
                 and (registered_tools is None or b.action_target in registered_tools)]
    caveat_hits = [b for b in hits if b.action == "caveat"]
    if explicit_request(facts.text):
        if cloud_ok:
            out.append(Decision("explicit", "consult_auto" if cfg.mode == "auto" else "consult_suggest",
                                reason="用户显式要求云端导师"))
        else:
            out.append(Decision("explicit", "unavailable",
                                reason=_REASON_ZH.get(kind or "", "云端导师工具未注册")))
    elif consult_hits and cloud_ok:
        # 云端不可用时目录命中不打扰用户（用户没要求云端；"不可用"说明只在显式要求时给）
        out.append(Decision("catalog", "consult_auto" if cfg.mode == "auto" else "consult_suggest",
                            boundaries=[b.id for b in consult_hits],
                            reason="；".join(b.title for b in consult_hits)))
    if tier_hits and cfg.tier_notice:
        b = tier_hits[0]
        out.append(Decision("catalog", "tier_suggest", boundaries=[x.id for x in tier_hits],
                            tier_target=b.action_target, tier_gen=b.action_gen, reason=b.title,
                            tier_cost=b.action_cost))
    if tool_hits:
        b = tool_hits[0]
        out.append(Decision("catalog", "tool_hint", boundaries=[x.id for x in tool_hits],
                            tier_target=b.action_target, reason=b.title, hint=b.action_hint))
    if caveat_hits:
        b = caveat_hits[0]
        out.append(Decision("catalog", "caveat", boundaries=[x.id for x in caveat_hits],
                            reason=b.title, notice=b.action_notice))
    return out


def decide_on_failures(consecutive_failures: int, cfg: UpgradeConfig, *,
                       cloud_cfg: Optional[dict] = None,
                       consult_registered: bool) -> Optional[Decision]:
    """轮中决策：本地连续失败达阈。"""
    if cfg.mode == "off" or consecutive_failures < cfg.failure_threshold:
        return None
    ok, kind, detail = availability(cloud_cfg)
    if not (ok and consult_registered):
        return None          # 云端不可用时失败路不打扰用户（失败本身已有 D2 护栏）
    return Decision("failures", "consult_auto" if cfg.mode == "auto" else "consult_suggest",
                    reason=f"本地连续失败 {consecutive_failures} 次（阈 {cfg.failure_threshold}）")


# ---------- 产物：给模型的尾注 / 给用户的收尾提示 / 代模型发起的调用 ----------

def model_hint(d: Decision) -> str:
    """suggest 档 / tool_hint 给模型的一次性提醒（尾注，不进历史）。"""
    if d.action == "tool_hint":
        return "[System guardrail] " + d.hint          # action_hint 构造期已保证非空
    why = d.reason or "this request matches a known local-model boundary"
    return ("[System guardrail] Cloud advisor suggested: " + why + ". The local model is known to "
            "be weak here. Consider calling cloud_consult ONCE with a focused question (and only the "
            "material it needs) before answering; the advice is text only — you still decide and act "
            "locally. If you choose not to, say so briefly in your answer.")


_QUESTION_CAP = 4000
_CONTEXT_CAP = 3000


def _cap(text: str, n: int) -> str:
    t = str(text or "")
    return t if len(t) <= n else t[:n] + "…[截断]"


def synthetic_consult_call(user_text: str, failure_context: str = "") -> Dict[str, Any]:
    """auto 档代模型发起的 cloud_consult 调用（仍进审批闸 + L0）。问题 = 用户指令原文（不附
    粘贴材料：隐私最小化，用户在审批卡上看到的就是出网的全部）；failures 触发时把触发它的
    失败观察作 context 一并交给顾问（否则顾问看不到到底哪里失败，付费建议等于白问）。
    超长截断带标记，审批卡上看得出被截。"""
    args: Dict[str, Any] = {"question": _cap(user_text, _QUESTION_CAP)}
    if failure_context:
        args["context"] = _cap(failure_context, _CONTEXT_CAP)
    return {"type": "function", "name": CLOUD_CONSULT_TOOL, "arguments": args}


def failure_context(observations: List[Dict[str, Any]], limit: int = 3) -> str:
    """最近的失败观察（System Error 开头）拼成顾问 context；每条截 600 字。"""
    fails = [str(o.get("response") or "") for o in observations
             if str(o.get("response") or "").lstrip().startswith("System Error")]
    fails = fails[-limit:]
    return "\n\n".join(f"[local failure {i + 1}] {_cap(t, 600)}" for i, t in enumerate(fails))


def user_notice(d: Decision) -> str:
    """收尾答复末尾的确定性提示（switch_tier 建议 / 云端不可用说明）。"""
    if d.action == "tier_suggest":
        think = ""
        if d.tier_gen and d.tier_gen.get("enable_thinking"):
            think = f"并开启思考（预算 {d.tier_gen.get('thinking_budget', 1024)}）"
        cost = d.tier_cost or "切换后响应会更慢"
        return (f"—— 提示：本题属于已知边界「{d.reason}」，本机 `{_short(d.tier_target)}` 档更稳"
                f"（`./devctl.sh model` 切换{think}；{cost}）；本回答仍由当前档完成。")
    if d.action == "unavailable":
        # 只给用户可读的原因短语（不带 key_env 变量名等内部细节，评审 m20）
        return f"—— 你要求的云端导师当前不可用（{d.reason}）；本回答由本地模型完成。"
    if d.action == "caveat":
        return f"—— 提示：{d.notice}"
    return ""


def already_consulted_this_turn(history: List[Dict[str, Any]]) -> bool:
    """本轮（自最后一条 user 起）是否已有 cloud_consult 调用（含待审批/已执行）——一轮只触发一次。"""
    start = 0
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            start = i
            break
    for msg in history[start:]:
        if msg.get("role") == "assistant":
            for c in msg.get("tool_calls") or []:
                if c.get("name") == CLOUD_CONSULT_TOOL:
                    return True
        if msg.get("role") == "tool":
            for tr in msg.get("tool_responses") or []:
                if tr.get("name") == CLOUD_CONSULT_TOOL:
                    return True
    return False


def facts_from_user_message(msg: Dict[str, Any], *, context_tokens: int = 0,
                            explicit_deep: bool = False) -> RequestFacts:
    """从会话里的 user 消息（content 块列表）抽请求侧事实：首个文本块=指令，其余文本块=摄入材料
    （只计长度），image_url 块=带图。纯字符串 content 也兼容。"""
    content = msg.get("content")
    text, extra, imgs = "", 0, False
    if isinstance(content, str):
        text = content
    else:
        first = True
        for blk in content or []:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text":
                if first:
                    text, first = str(blk.get("text") or ""), False
                else:
                    extra += len(str(blk.get("text") or ""))
            elif blk.get("type") == "image_url":
                imgs = True
    return RequestFacts(text=text, has_images=imgs, context_tokens=context_tokens,
                        explicit_deep=explicit_deep, attachments_chars=extra)
