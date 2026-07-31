"""云端顾问通道（混合架构第 2 站：云端推理后端；铁律 6——云端只顾问不执行）。

结构性约束（是代码形状，不是口头纪律）：
- 云端返回的**只是文本**，作为工具观察进入本地循环——落地为工具调用的永远是
  本地模型 + 既有审批闸门。本模块没有任何「把云端输出解析成调用」的代码路径，
  也永远不该有（金测试钉板）。
- **出网前审批**：消费方 cloud_consult 工具 requires_approval=True——L0 出网
  守卫落地前，人是脱敏判定的临时执行者（审批卡上就是将出网的完整 payload）；
  L0 波接管后这道人工闸再按配置放宽。
- **无条件出网台账**：每次出网写 data_dir()/cloud/egress.jsonl 一行（时间/供应商/
  模型/用途/payload 规模/tokens/费用/结果）。这同时是铁律 6 出网日志的雏形与
  费用台账——daily_budget_usd 预算护栏从这里累计，超了当日拒绝出网。
- **密钥只经环境变量**（.env.local 注入），config 只存 key_env 变量名。

适配器只有两种 kind：`openai_compat`（OpenAI 及一切兼容面：DeepSeek/OpenRouter/
国产模型托管/自建 vLLM…，base_url 即插即用）与 `anthropic`（Messages API）。
顾问单发 = 纯文本进出——跨供应商兼容的真正难点（工具调用格式/流式状态机/KV
语义）被铁律 6 天然裁掉，因此两家都做的代价只是两个小适配器。
"""
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Optional

from ..utils.config_loader import data_dir, load_section

_KINDS = ("openai_compat", "anthropic")

#: 顾问系统提示：中性、无个人环境事实（隐私边界：出网内容只有显式给出的 payload）
_ADVISOR_SYSTEM = ("You are an external consulting model. Answer the question using only "
                   "the material provided. Be concise and concrete. You have no tools and "
                   "cannot take actions; provide analysis or a draft only.")


def load_providers(cfg: Optional[dict] = None) -> list:
    """读 config cloud.providers，逐条校验并标注 has_key（key 值本身不外传）。

    kind/name/model/key_env 缺失或非法在装配期响亮 ValueError（S.3 教训：配置错误
    不得静默）。providers 为空 = 云端顾问整体不可用（离线纯本地形态，零行为变化）。
    """
    cloud = cfg if cfg is not None else load_section("cloud")
    out, seen = [], set()
    for p in cloud.get("providers") or []:
        name = str(p.get("name") or "").strip()
        kind = str(p.get("kind") or "").strip()
        if not name or name in seen:
            raise ValueError(f"cloud.providers 供应商名非法或重复: {name!r}")
        if kind not in _KINDS:
            raise ValueError(f"cloud.providers[{name}].kind 非法: {kind!r}（可用: {_KINDS}）")
        for field in ("base_url", "model", "key_env"):
            if not str(p.get(field) or "").strip():
                raise ValueError(f"cloud.providers[{name}] 缺 {field}")
        if "api_key" in p or "key" in p:
            raise ValueError(f"cloud.providers[{name}] 含内联密钥字段——密钥只经环境变量"
                             f"（.env.local），config 只存 key_env 名")
        out.append({**p, "has_key": bool(os.environ.get(str(p["key_env"]), "").strip())})
        seen.add(name)
    return out


def pick_provider(name: Optional[str] = None, cfg: Optional[dict] = None) -> dict:
    """选供应商：点名取点名（无 key 响亮报错），缺省取声明序第一个有 key 的。"""
    providers = load_providers(cfg)
    if name:
        p = next((p for p in providers if p["name"] == name), None)
        if p is None:
            raise KeyError(f"未声明的云端供应商 {name!r}（现有: {[q['name'] for q in providers]}）")
        if not p["has_key"]:
            raise RuntimeError(f"供应商 {name!r} 未配置密钥（.env.local 设 {p['key_env']}）")
        return p
    p = next((p for p in providers if p["has_key"]), None)
    if p is None:
        raise RuntimeError("无任何云端供应商配置了密钥（config cloud.providers + "
                           ".env.local 的 CLOUD_*_API_KEY）")
    return p


# ---- 出网台账（铁律 6 出网日志雏形 + 费用护栏的数据源） ----

def egress_log_path() -> Path:
    return data_dir() / "cloud" / "egress.jsonl"


def record_egress(entry: dict) -> None:
    """无条件追加一行出网台账。写失败原样上抛——审计日志不是 best-effort。"""
    path = egress_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def spent_today_usd() -> float:
    """从台账累计今日（本地日期）已花费。文件缺失 = 0；坏行跳过（台账只增不改）。"""
    path = egress_log_path()
    if not path.exists():
        return 0.0
    today = date.today().isoformat()
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if str(row.get("ts", "")).startswith(today):
                total += float(row.get("cost_usd") or 0.0)
        except Exception:
            continue
    return total


def _cost(p: dict, in_tokens: int, out_tokens: int) -> float:
    return (in_tokens * float(p.get("input_usd_per_m") or 0.0)
            + out_tokens * float(p.get("output_usd_per_m") or 0.0)) / 1_000_000


# ---- 两种适配器（纯文本单发；transport 可注入=金测试不打网络） ----

def _call_openai_compat(p: dict, payload_text: str, max_tokens: int, timeout: float,
                        transport) -> dict:
    url = str(p["base_url"]).rstrip("/") + "/chat/completions"
    field = str(p.get("max_tokens_field") or "max_tokens")
    body = {"model": p["model"],
            "messages": [{"role": "system", "content": _ADVISOR_SYSTEM},
                         {"role": "user", "content": payload_text}],
            field: max_tokens}
    r = transport.post(url, json=body, timeout=timeout, headers={
        "Authorization": f"Bearer {os.environ.get(str(p['key_env']), '')}"})
    r.raise_for_status()
    d = r.json()
    usage = d.get("usage") or {}
    return {"text": ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "",
            "in_tokens": int(usage.get("prompt_tokens") or 0),
            "out_tokens": int(usage.get("completion_tokens") or 0)}


def _call_anthropic(p: dict, payload_text: str, max_tokens: int, timeout: float,
                    transport) -> dict:
    url = str(p["base_url"]).rstrip("/") + "/v1/messages"
    body = {"model": p["model"], "max_tokens": max_tokens, "system": _ADVISOR_SYSTEM,
            "messages": [{"role": "user", "content": payload_text}]}
    r = transport.post(url, json=body, timeout=timeout, headers={
        "x-api-key": os.environ.get(str(p["key_env"]), ""),
        "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    d = r.json()
    usage = d.get("usage") or {}
    return {"text": "".join(b.get("text") or "" for b in d.get("content") or []
                            if b.get("type") == "text"),
            "in_tokens": int(usage.get("input_tokens") or 0),
            "out_tokens": int(usage.get("output_tokens") or 0)}


def consult(question: str, context: str = "", provider: Optional[str] = None,
            purpose: str = "consult", cfg: Optional[dict] = None,
            transport=None, sanitize_summary: Optional[dict] = None) -> dict:
    """一次云端顾问单发。返回 {text, provider, model, in_tokens, out_tokens, cost_usd}。

    出网的 payload 恰为 question(+context) 逐字拼接，无暗附加——脱敏发生在调用方
    （cloud_consult 经 egress_guard），本函数只发不改；sanitize_summary 是调用方
    交来的脱敏报告，无条件随台账落账（铁律 6 出网日志：出了什么、遮了什么，同行
    可查）。预算护栏：今日累计费用达 daily_budget_usd 即拒绝（先查后发）。
    """
    cloud = cfg if cfg is not None else load_section("cloud")
    p = pick_provider(provider, cfg=cloud)
    budget = float(cloud.get("daily_budget_usd") or 0.0)
    if budget > 0:
        spent = spent_today_usd()
        if spent >= budget:
            raise RuntimeError(f"云端日预算已用尽（今日 ${spent:.4f} ≥ 预算 ${budget:.2f}）；"
                               f"明日自动恢复，或调高 config cloud.daily_budget_usd")
    payload_text = question if not context else f"{question}\n\n--- material ---\n{context}"
    max_tokens = int(cloud.get("max_tokens") or 2048)
    timeout = float(cloud.get("timeout") or 120)
    if transport is None:
        import requests as transport  # noqa: PLC0415 —— 生产走 requests，测试注入假体
    call = _call_anthropic if p["kind"] == "anthropic" else _call_openai_compat
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    base = {"ts": ts, "provider": p["name"], "model": p["model"], "purpose": purpose,
            "payload_chars": len(payload_text),
            **({"sanitize": sanitize_summary} if sanitize_summary else {})}
    try:
        out = call(p, payload_text, max_tokens, timeout, transport)
    except Exception as e:
        record_egress({**base, "ok": False, "error": str(e)[:300], "cost_usd": 0.0})
        raise
    cost = _cost(p, out["in_tokens"], out["out_tokens"])
    record_egress({**base, "in_tokens": out["in_tokens"], "out_tokens": out["out_tokens"],
                   "cost_usd": round(cost, 6), "ok": True})
    return {"text": out["text"], "provider": p["name"], "model": p["model"],
            "in_tokens": out["in_tokens"], "out_tokens": out["out_tokens"],
            "cost_usd": cost}
