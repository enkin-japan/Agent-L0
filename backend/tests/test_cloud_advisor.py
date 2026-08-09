"""云端顾问金测试（混合架构第 2 站；铁律 6——云端只顾问不执行）。

守的是什么：
① **密钥纪律**——config 只存 key_env 变量名，内联密钥字段拒启；密钥值绝不落台账。
② **审批闸在位**——cloud_consult.requires_approval 恒 True（L0 落地前审批卡=脱敏
  检查点）；无 key 时工具不注册（离线形态零变化）。
③ **无条件出网台账 + 预算护栏**——成功/失败都写 egress.jsonl；今日累计达
  daily_budget_usd 即拒绝且**不发请求**。
④ **适配器请求形状**——openai_compat 的 max_tokens_field 覆盖、anthropic 的
  version 头与 content 块解析（形状错=首次真调用才炸，且烧真钱调试）。
⑤ **顾问纯文本结构钉板**——cloud 模块不得 import 任何方言/解析面（云端输出
  永远不被解析成工具调用，是代码形状不是口头纪律）。
"""
import json
from pathlib import Path

import pytest
import yaml

from framework.cloud import advisor

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cfg(providers=None, budget=2.0):
    return {"daily_budget_usd": budget, "max_tokens": 512, "timeout": 30,
            "providers": providers if providers is not None else [
                {"name": "oa", "kind": "openai_compat", "base_url": "https://x.example/v1",
                 "model": "m1", "key_env": "T_OA_KEY",
                 "max_tokens_field": "max_completion_tokens",
                 "input_usd_per_m": 1.0, "output_usd_per_m": 10.0},
                {"name": "an", "kind": "anthropic", "base_url": "https://y.example",
                 "model": "m2", "key_env": "T_AN_KEY",
                 "input_usd_per_m": 5.0, "output_usd_per_m": 25.0}]}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Transport:
    """假传输：记录请求，返回按 kind 预置的响应体。"""
    def __init__(self, data):
        self.data = data
        self.calls = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "body": json, "headers": headers})
        return _Resp(self.data)


# ---- ① 密钥纪律 ----

def test_inline_key_rejected_and_has_key_flag(monkeypatch):
    with pytest.raises(ValueError, match="内联密钥"):
        advisor.load_providers({"providers": [
            {"name": "x", "kind": "anthropic", "base_url": "u", "model": "m",
             "key_env": "E", "api_key": "sk-leak"}]})
    monkeypatch.delenv("T_OA_KEY", raising=False)
    monkeypatch.setenv("T_AN_KEY", "k2")
    ps = advisor.load_providers(_cfg())
    assert [p["has_key"] for p in ps] == [False, True]
    # 缺 key 的默认选择跳过 → 落到 an；点名无 key 的响亮报错
    assert advisor.pick_provider(cfg=_cfg())["name"] == "an"
    with pytest.raises(RuntimeError, match="未配置密钥"):
        advisor.pick_provider("oa", cfg=_cfg())


def test_config_yaml_cloud_section_legal():
    cloud = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))["cloud"]
    ps = cloud["providers"]
    assert [p["name"] for p in ps] == ["openai", "anthropic"]  # 声明序=用户偏好序
    for p in ps:
        assert p["kind"] in ("openai_compat", "anthropic")
        assert p["key_env"].startswith("CLOUD_")
        assert "api_key" not in p and "key" not in p
        assert p["input_usd_per_m"] > 0 and p["output_usd_per_m"] > 0
    assert cloud["timeout"] < 180  # 须小于 agent.tool_timeout_seconds（工具级兜底）


# ---- ③ 台账 + 预算护栏 ----

def test_egress_ledger_and_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_OA_KEY", "k1")
    t = _Transport({"choices": [{"message": {"content": "advice"}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})
    r = advisor.consult("q", context="c", cfg=_cfg(), transport=t)
    assert r["text"] == "advice" and r["provider"] == "oa"
    assert abs(r["cost_usd"] - (1000 * 1.0 + 500 * 10.0) / 1e6) < 1e-9
    rows = [json.loads(l) for l in advisor.egress_log_path().read_text().splitlines()]
    assert rows[-1]["ok"] is True and rows[-1]["payload_chars"] > 0
    assert "k1" not in advisor.egress_log_path().read_text()  # 密钥值绝不落台账
    assert advisor.spent_today_usd() > 0
    # 预算耗尽：拒绝且不发请求
    n_calls = len(t.calls)
    with pytest.raises(RuntimeError, match="日预算已用尽"):
        advisor.consult("q2", cfg=_cfg(budget=advisor.spent_today_usd()), transport=t)
    assert len(t.calls) == n_calls


def test_empty_advice_fails_loud_but_ledger_kept(tmp_path, monkeypatch):
    """推理型供应商把 max_tokens 花光在隐藏推理上→正文空返（L4 站实测）：
    必须响亮报错（空建议悄悄回填会误导本地决策），且台账照记（钱确实花了）。"""
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_OA_KEY", "k1")
    t = _Transport({"choices": [{"message": {"content": ""}}],
                    "usage": {"prompt_tokens": 400, "completion_tokens": 2048}})
    with pytest.raises(RuntimeError, match="空正文"):
        advisor.consult("q", cfg=_cfg(), transport=t)
    row = json.loads(advisor.egress_log_path().read_text().splitlines()[-1])
    assert row["ok"] is True and row["out_tokens"] == 2048


def test_failed_call_also_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_AN_KEY", "k2")

    class _Boom:
        def post(self, *a, **k):
            raise OSError("network down")

    with pytest.raises(OSError):
        advisor.consult("q", provider="an", cfg=_cfg(), transport=_Boom())
    row = json.loads(advisor.egress_log_path().read_text().splitlines()[-1])
    assert row["ok"] is False and "network down" in row["error"]


# ---- ④ 适配器请求形状 ----

def test_openai_compat_request_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_OA_KEY", "sekrit")
    t = _Transport({"choices": [{"message": {"content": "a"}}], "usage": {}})
    advisor.consult("Q", cfg=_cfg(), transport=t)
    call = t.calls[0]
    assert call["url"] == "https://x.example/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sekrit"
    assert call["body"]["max_completion_tokens"] == 512 and "max_tokens" not in call["body"]
    assert call["body"]["messages"][-1]["content"] == "Q"


def test_anthropic_request_shape_and_content_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_AN_KEY", "sekrit2")
    t = _Transport({"content": [{"type": "text", "text": "part1 "},
                                {"type": "thinking", "thinking": "x"},
                                {"type": "text", "text": "part2"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}})
    r = advisor.consult("Q", provider="an", cfg=_cfg(), transport=t)
    assert r["text"] == "part1 part2"  # 非 text 块（thinking 等）不进建议文本
    call = t.calls[0]
    assert call["url"] == "https://y.example/v1/messages"
    assert call["headers"]["x-api-key"] == "sekrit2"
    assert call["headers"]["anthropic-version"]
    assert call["body"]["max_tokens"] == 512


# ---- ②⑤ 工具面 + 结构钉板 ----

def test_tool_surface_and_availability(monkeypatch):
    from framework.mcp.tools.cloud_consult import CloudConsultTool
    t = CloudConsultTool()
    assert t.requires_approval is True          # 出网闸：L0 落地前审批卡=脱敏检查点
    assert t.parameters["required"] == ["question"]
    assert "leave this machine" in t.description  # 模型必须被告知数据将出网
    monkeypatch.delenv("CLOUD_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLOUD_ANTHROPIC_API_KEY", raising=False)
    assert t.is_available() is False            # 无 key 不注册（离线形态零变化）


def test_advisor_never_parses_tool_calls():
    """铁律 6 结构钉板：cloud 模块与工具不得触碰方言/解析面——云端输出永远是纯文本
    观察，绝不被反序列化成调用。"""
    for rel in ("backend/framework/cloud/advisor.py",
                "backend/framework/mcp/tools/cloud_consult.py"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for banned in ("dialects", "parse_tool_calls", "response_parser", "tool_schema"):
            assert banned not in src, f"{rel} 触碰了解析面: {banned}（铁律 6）"
