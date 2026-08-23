"""云端导师降级路（⑤′-1，2026-08-23）：失败语义统一 + 「本地作答」明确标注。

角色边界（用户 2026-08-23 拍板）：云端导师 = **商用 API**（ChatGPT/Claude 类）；
Colab 仅开发测试、与生产无关——本模块的降级原因里没有、也不该有 Colab 的影子。

三件事，全是代码形状不是口头纪律：
1. **统一失败分类**（`classify`）：无供应商 / 无密钥 / 供应商名不存在 / 日预算耗尽 /
   超时 / 连接失败 / HTTP 错 / 空正文 / 配置非法 / 其它。advisor 自己抛的带 `kind`
   的 `CloudAdvisorError` 直接取 kind；传输层异常按类型名归类（不 import requests，
   测试注入假传输同样适用）。
2. **统一降级观察**（`degraded_observation`）：cloud_consult 工具失败时交回本地循环的
   是一段以 `DEGRADED_PREFIX` 起头的观察——保持 `System Error:` 前缀（工具层失败判定
   `_obs_failed` / tool_error 事件照常），不再把 traceback 喂给模型；正文明确指示模型：
   **本地作答，并向用户说明没有云端导师**。编排层把它**排除出** D2 三态（既非吞掉的
   失败也非成功变更）：按设计本地作答、禁止重试，失败吞没推回与 warning 降级都不该
   为它触发——谎报围堵在这条路上由下一条的确定性标注承担。
3. **「本地作答」标注**（`user_notice`）：编排层在本轮最终答复末尾追加一行确定性
   说明（不依赖模型是否听话转述）——不静默装满血，这是 ⑤′-1 的验收核心。识别**只认
   `cloud_consult` 工具的观察**（工具名 + 前缀常量双锚，read_file 读到同款日志不算），
   且同轮若有成功的云端咨询则不加（首次超时、重试成功 = 真用了云端建议）。

铁律 6 不变：云端只顾问、落地永远本地；降级只是少了顾问，不改变任何执行路径。
"""
from typing import Iterable, Optional, Tuple

#: 云端导师工具名（mcp/tools/cloud_consult.py 以此为 name；编排层识别降级只看这个工具）
CLOUD_CONSULT_TOOL = "cloud_consult"

#: 降级观察的稳定前缀。编排层据此识别（常量共享，不做自由文本匹配）。
DEGRADED_PREFIX = "System Error: [cloud advisor degraded:"

KINDS = ("no_provider", "no_key", "unknown_provider", "budget_exhausted", "timeout",
         "connection_error", "http_error", "empty_reply", "bad_config", "ledger_error",
         "ledger_error_after_send", "unknown")

#: 用户可读的原因短语（中文，随最终答复落给用户）
_REASON_ZH = {
    "no_provider": "未配置云端供应商",
    "no_key": "云端供应商未配置密钥",
    "unknown_provider": "点名的云端供应商不存在",
    "budget_exhausted": "云端日预算已用尽",
    "timeout": "云端请求超时",
    "connection_error": "云端连接失败",
    "http_error": "云端服务返回错误",
    "empty_reply": "云端返回空正文",
    "bad_config": "云端配置非法",
    "ledger_error": "出网台账不可写（拒绝出网）",
    "ledger_error_after_send": "出网台账写入失败（本次调用已发生并计费，建议作废）",
    "unknown": "云端调用失败",
}


class CloudAdvisorError(RuntimeError):
    """advisor 自己判定的失败，带 `kind`（KINDS 之一）。仍是 RuntimeError：旧调用方
    `except RuntimeError` / `pytest.raises(RuntimeError)` 语义不变。"""

    def __init__(self, kind: str, message: str):
        if kind not in KINDS:
            raise ValueError(f"未知降级原因 {kind!r}（可用: {KINDS}）")
        super().__init__(message)
        self.kind = kind


# 类型名归类（不 import requests/httpx：测试注入假传输同样适用）。顺序即优先级：
# requests 全家都继承 IOError/OSError，所以"具体名"必须先于 OSError 兜底判，
# 否则 JSONDecodeError/TooManyRedirects 都会被读成「连接失败」（评审 m10）。
_TIMEOUT_NAMES = ("Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError", "timeout",
                  "TimeoutException", "ReadTimeoutError")
_HTTP_NAMES = ("HTTPError", "HTTPStatusError", "JSONDecodeError", "ChunkedEncodingError",
               "TooManyRedirects", "InvalidURL", "MissingSchema", "InvalidSchema",
               "ContentDecodingError", "DecodeError", "RemoteProtocolError")
_CONN_NAMES = ("ConnectionError", "ConnectError", "ConnectionRefusedError",
               "ConnectionResetError", "NewConnectionError", "gaierror", "SSLError")
_CONN_FALLBACK = ("OSError",)

#: 编程错误：不是云端失败，不该伪装成「云端不可用」（评审 m12）——工具面原样上抛
PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, AssertionError, ImportError)


def classify(exc: BaseException) -> Tuple[str, str]:
    """异常 → (kind, 详情)。advisor 的带 kind 异常优先；配置非法（load_providers 的
    ValueError）→ bad_config；传输层按类型名（含 MRO）归类。"""
    if isinstance(exc, CloudAdvisorError):
        return exc.kind, str(exc)
    names = [c.__name__ for c in type(exc).__mro__]
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, ValueError) and "cloud.providers" in str(exc):
        return "bad_config", detail
    for kind, table in (("timeout", _TIMEOUT_NAMES), ("http_error", _HTTP_NAMES),
                        ("connection_error", _CONN_NAMES), ("connection_error", _CONN_FALLBACK)):
        if any(n in names for n in table):
            return kind, detail
    return "unknown", detail


def degraded_observation(kind: str, detail: str = "", provider: Optional[str] = None) -> str:
    """交回本地循环的降级观察（给模型看的；用户侧标注另由 user_notice 负责）。"""
    if kind not in KINDS:
        kind = "unknown"
    who = f" provider={provider}" if provider else ""
    body = (f"{DEGRADED_PREFIX} {kind}]{who} {detail}".rstrip() + "\n"
            "The cloud advisor is NOT available for this request. Do not retry it in this turn. "
            "Continue with LOCAL capability only, and tell the user explicitly that this answer "
            "was produced locally without the cloud advisor (state the reason above). "
            "Never claim that the cloud advisor was consulted.")
    return body


def degraded_kind(observation: str, tool_name: Optional[str] = None) -> Optional[str]:
    """观察文本 → 降级原因；不是降级观察返回 None。

    双锚：前缀常量 **且** 工具名是 cloud_consult（传了 tool_name 才校验；其它工具读到
    同款文本——read_file 翻日志——不算，否则会给用户挂一条假的「本地作答」并记假事件，
    评审 M2）。"""
    if tool_name is not None and tool_name != CLOUD_CONSULT_TOOL:
        return None
    s = str(observation or "").lstrip()
    if not s.startswith(DEGRADED_PREFIX):
        return None
    rest = s[len(DEGRADED_PREFIX):].lstrip()
    kind = rest.split("]", 1)[0].strip()
    return kind if kind in KINDS else "unknown"


def user_notice(kinds: Iterable[str]) -> str:
    """本轮最终答复末尾的确定性标注（一次降级一行原因，去重保序）。"""
    seen, reasons = set(), []
    for k in kinds:
        k = k if k in KINDS else "unknown"
        if k not in seen:
            seen.add(k)
            reasons.append(_REASON_ZH[k])
    if not reasons:
        return ""
    return ("—— 本回答由本地模型独立完成，未经云端导师："
            + "；".join(reasons) + "。")


def cloud_consult_succeeded(observation: str, tool_name: Optional[str]) -> bool:
    """cloud_consult 的**成功**观察（非 System Error）。同轮有成功咨询 → 不挂「本地作答」。"""
    if tool_name != CLOUD_CONSULT_TOOL:
        return False
    s = str(observation or "").lstrip()
    return bool(s) and not s.startswith("System Error")


def availability(cfg: Optional[dict] = None) -> Tuple[bool, Optional[str], str]:
    """云端导师可用性（不发请求、不花钱）：(ok, kind, 详情)。

    供工具注册面与 ⑤′-3 升级策略共用：触发了升级却不可用时，原因要能落到
    用户标注里，而不是「工具不在清单里」这种无声缺席。"""
    from .advisor import load_providers
    try:
        provs = load_providers(cfg)
    except Exception as e:
        return False, "bad_config", f"{type(e).__name__}: {e}"
    if not provs:
        return False, "no_provider", "config cloud.providers 为空"
    if not any(p["has_key"] for p in provs):
        return False, "no_key", ("无任何云端供应商配置了密钥（.env.local 设 "
                                 + " / ".join(str(p["key_env"]) for p in provs) + "）")
    return True, None, ""
