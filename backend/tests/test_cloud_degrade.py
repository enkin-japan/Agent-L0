"""⑤′-1 云端导师降级路金测试（2026-08-23）。

守的不变量：
- advisor 的每条失败路带 kind（无供应商/无钥/点名不存在/预算耗尽/空正文），且
  **拒发**也记出网台账（refused=kind，cost 0）——审计面不只记"发了"；
- cloud_consult 失败交回的是统一降级观察：`System Error:` 前缀保留（既有失败判定照常）、
  无 traceback、指示模型本地作答并告知用户；
- 编排层：降级观察 → 账本 `cloud_degraded` 事件；本轮最终答复末尾**确定性**追加
  「本地作答」标注（直接路径与审批续跑路径都覆盖）；turn metrics 平铺 cloud_degraded。
- 角色边界：降级原因里没有 Colab（云端导师 = 商用 API）。
"""
import json

import pytest

from framework.cloud import advisor, degrade
from framework.cloud.degrade import (CLOUD_CONSULT_TOOL, DEGRADED_PREFIX, KINDS,
                                     CloudAdvisorError, classify, cloud_consult_succeeded,
                                     degraded_kind, degraded_observation, user_notice)

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import GEN, begin_turn, make_orch, ring_of, run


def _cfg(budget=2.0, providers=None):
    return {"daily_budget_usd": budget, "max_tokens": 100, "timeout": 5,
            "providers": providers if providers is not None else [
                {"name": "oa", "kind": "openai_compat", "base_url": "https://x.example",
                 "model": "m1", "key_env": "T_OA_KEY",
                 "input_usd_per_m": 1.0, "output_usd_per_m": 10.0}]}


def _rows():
    return [json.loads(l) for l in advisor.egress_log_path().read_text().splitlines()]


# ---------- advisor 失败语义 ----------

def test_no_provider_kind_and_refusal_row(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", cfg=_cfg(providers=[]))
    assert ei.value.kind == "no_provider"
    assert isinstance(ei.value, RuntimeError)          # 旧调用方语义不变
    row = _rows()[-1]
    assert row["ok"] is False and row["refused"] == "no_provider" and row["cost_usd"] == 0.0
    # 点名供应商但零供应商 → 仍是 no_provider（评审 m11）
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", provider="x", cfg=_cfg(providers=[]))
    assert ei.value.kind == "no_provider"


def test_no_key_and_unknown_provider_kinds(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("T_OA_KEY", raising=False)
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", cfg=_cfg())
    assert ei.value.kind == "no_key"
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", provider="nope", cfg=_cfg())
    assert ei.value.kind == "unknown_provider"
    assert [r["refused"] for r in _rows()] == ["no_key", "unknown_provider"]


def test_budget_exhausted_kind_refusal_row_and_no_send(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_OA_KEY", "k1")
    sent = []

    class _T:
        def post(self, *a, **k):
            sent.append(1)
            raise AssertionError("预算耗尽不得发请求")
    advisor.record_egress({"ts": advisor.time.strftime("%Y-%m-%dT%H:%M:%S"), "cost_usd": 5.0})
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", cfg=_cfg(budget=1.0), transport=_T())
    assert ei.value.kind == "budget_exhausted" and not sent
    row = _rows()[-1]
    assert row["refused"] == "budget_exhausted" and row["provider"] == "oa"
    assert advisor.spent_today_usd() == 5.0            # 拒发行 cost 0，不污染预算累计
    assert "k1" not in advisor.egress_log_path().read_text()   # 新写入路径同样不落密钥值


def test_refusal_row_carries_sanitize_summary_and_bad_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("T_OA_KEY", raising=False)
    with pytest.raises(CloudAdvisorError):
        advisor.consult("q", cfg=_cfg(), sanitize_summary={"counts": {"email": 1}, "model_leg": "ok"})
    assert _rows()[-1]["sanitize"] == {"counts": {"email": 1}, "model_leg": "ok"}
    bad = _cfg(providers=[{"name": "x", "kind": "nope", "base_url": "u", "model": "m", "key_env": "E"}])
    with pytest.raises(ValueError):
        advisor.consult("q", cfg=bad)
    assert _rows()[-1]["refused"] == "bad_config"      # 配置非法也落账（评审 m7）
    # 长 provider 名截断入账（评审 m17）
    with pytest.raises(CloudAdvisorError):
        advisor.consult("q", provider="p" * 500, cfg=_cfg())
    assert len(_rows()[-1]["provider"]) == 64


def test_ledger_write_failure_becomes_ledger_error_not_connection_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("T_OA_KEY", raising=False)

    def _ro(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(advisor, "record_egress", _ro)
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", cfg=_cfg())
    assert ei.value.kind == "ledger_error" and "no_key" in str(ei.value)
    assert classify(ei.value)[0] == "ledger_error"


def test_empty_reply_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_OA_KEY", "k1")

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "  "}}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 100}}

    class _T:
        def post(self, *a, **k): return _R()
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", cfg=_cfg(), transport=_T())
    assert ei.value.kind == "empty_reply"


# ---------- 分类 / 观察 / 标注 ----------

def test_classify_transport_errors_by_type_name():
    class ReadTimeout(Exception): ...
    class HTTPError(Exception): ...
    class ConnectionError(Exception): ...
    # requests 全家继承 IOError：具体名须先于 OSError 兜底（评审 m10）
    class JSONDecodeError(IOError): ...
    class TooManyRedirects(IOError): ...
    class HTTPStatusError(Exception): ...
    assert classify(ReadTimeout("slow"))[0] == "timeout"
    assert classify(TimeoutError())[0] == "timeout"
    assert classify(HTTPError("500"))[0] == "http_error"
    assert classify(HTTPStatusError("502"))[0] == "http_error"
    assert classify(JSONDecodeError("bad body"))[0] == "http_error"
    assert classify(TooManyRedirects("loop"))[0] == "http_error"
    assert classify(ConnectionError("refused"))[0] == "connection_error"
    assert classify(OSError("network down"))[0] == "connection_error"
    assert classify(ValueError("cloud.providers[x].kind 非法"))[0] == "bad_config"
    assert classify(RuntimeError("whatever"))[0] == "unknown"
    assert classify(CloudAdvisorError("no_key", "x"))[0] == "no_key"


def test_degraded_observation_roundtrip_and_shape():
    for k in KINDS:
        obs = degraded_observation(k, "detail", provider="oa")
        assert obs.startswith(DEGRADED_PREFIX)
        assert obs.lstrip().startswith("System Error")     # 既有失败判定照常生效
        assert degraded_kind(obs) == k
        assert "Traceback" not in obs
        assert "LOCAL capability" in obs and "tell the user" in obs
    assert degraded_kind("plain text") is None
    assert degraded_kind("System Error: something else") is None
    assert degraded_kind(None) is None
    assert degraded_kind(f"{DEGRADED_PREFIX} made_up] x") == "unknown"
    assert degraded_kind(degraded_observation("not_a_kind", "")) == "unknown"
    # 双锚：工具名不是 cloud_consult 的同款文本不算（评审 M2）
    obs = degraded_observation("timeout", "x")
    assert degraded_kind(obs, "read_file") is None
    assert degraded_kind(obs, CLOUD_CONSULT_TOOL) == "timeout"
    assert cloud_consult_succeeded("[cloud advisor · oa/m1] advice", CLOUD_CONSULT_TOOL)
    assert not cloud_consult_succeeded(obs, CLOUD_CONSULT_TOOL)
    assert not cloud_consult_succeeded("advice", "read_file")


def test_tool_name_constant_matches_tool_class():
    from framework.mcp.tools.cloud_consult import CloudConsultTool
    assert CloudConsultTool.name == CLOUD_CONSULT_TOOL


def test_user_notice_dedup_order_and_no_colab():
    n = user_notice(["timeout", "budget_exhausted", "timeout"])
    assert n.startswith("—— 本回答由本地模型独立完成")
    assert n.index("超时") < n.index("预算")
    assert n.count("超时") == 1
    assert user_notice([]) == ""
    blob = (user_notice(KINDS) + " ".join(degrade._REASON_ZH.values())).lower()
    assert "colab" not in blob, "云端导师=商用 API，降级原因里不得出现 Colab"


def test_availability_states(monkeypatch):
    ok, kind, _ = degrade.availability(_cfg(providers=[]))
    assert (ok, kind) == (False, "no_provider")
    monkeypatch.delenv("T_OA_KEY", raising=False)
    assert degrade.availability(_cfg())[:2] == (False, "no_key")
    monkeypatch.setenv("T_OA_KEY", "k")
    assert degrade.availability(_cfg())[:2] == (True, None)
    bad = _cfg(providers=[{"name": "x", "kind": "nope", "base_url": "u", "model": "m",
                           "key_env": "E"}])
    assert degrade.availability(bad)[:2] == (False, "bad_config")


# ---------- cloud_consult 工具面 ----------

def test_cloud_consult_returns_degraded_observation_not_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from framework.mcp.tools.cloud_consult import CloudConsultTool
    import framework.mcp.tools.cloud_consult as cc
    monkeypatch.setattr(cc, "load_section", lambda *_: {"sanitize_model": False}, raising=False)
    import framework.utils.config_loader as cl
    monkeypatch.setattr(cl, "load_section",
                        lambda name: {"sanitize_model": False} if name == "cloud" else {})
    import framework.cloud.advisor as adv

    def _boom(*a, **k):
        raise CloudAdvisorError("budget_exhausted", "云端日预算已用尽（今日 $2.1 ≥ 预算 $2.00）")
    monkeypatch.setattr(adv, "consult", _boom)
    out = CloudConsultTool().execute(question="帮我分析", context="材料")
    assert degraded_kind(out) == "budget_exhausted"
    assert "Traceback" not in out and "预算" in out
    assert "[L0 egress guard:" in out                  # L0 状态行随降级观察同行

    def _bug(*a, **k):
        raise TypeError("consult() got an unexpected keyword argument")
    monkeypatch.setattr(adv, "consult", _bug)
    with pytest.raises(TypeError):                      # 编程错误不伪装成降级（评审 m12）
        CloudConsultTool().execute(question="q")


# ---------- 编排层：标注与账本 ----------

def _degraded_tool(approval=False, name=CLOUD_CONSULT_TOOL):
    # 与真工具同元数据：非只读、不可并发（评审 m14）
    return FakeTool(name, result=degraded_observation("budget_exhausted", "日预算已用尽"),
                    approval=approval, read_only=False, concurrency_safe=False)


def test_orchestrator_appends_local_answer_notice_direct_path(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"text": "这是我的本地分析。"},
    ])
    orch = make_orch(engine, FakeRegistry([_degraded_tool()]))
    session = make_session()
    session.add_user_message("请深入分析")
    begin_turn(session)
    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    # 降级不是"吞掉的失败"：不触发失败吞没推回、不降级 warning（按设计本地作答）。
    # 推回会多转一圈 → FakeEngine 两条脚本耗尽直接炸；status 亦不得是 warning
    assert result["status"] == "success"
    assert engine.script == []
    assert result["response"].startswith("这是我的本地分析。")
    assert "本回答由本地模型独立完成" in result["response"]
    assert "预算" in result["response"]
    assert result["metrics"]["cloud_degraded"] == ["budget_exhausted"]
    assert result["metrics"]["cloud_consult_ok"] is False
    assert ring_of(session, "cloud_degraded") == [
        {"call_id": ring_of(session, "tool_started")[0]["call_id"],
         "name": "cloud_consult", "kind": "budget_exhausted"}]
    # 账本序：cloud_degraded 紧随对应 tool_result 之后（PROTOCOL）
    types = [e["type"] for e in session.journal.ring]
    assert types.index("cloud_degraded") == types.index("tool_result") + 1
    # 历史里最终答复带标注（重载会话后仍可见）
    assert "本回答由本地模型独立完成" in session.history[-1]["content"]


def test_orchestrator_notice_survives_approval_resume(make_session):
    """cloud_consult 要审批：降级观察在 resume_after_approval 落进历史再重入 run_turn。"""
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"text": "本地作答。"},
    ])
    orch = make_orch(engine, FakeRegistry([_degraded_tool(approval=True)]))
    session = make_session()
    session.add_user_message("请深入分析")
    begin_turn(session)
    pending = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert pending["status"] == "pending_approval"
    resumed = run(orch.resume_after_approval(session, approved=True,
                                             journal=session.journal, **GEN))
    assert resumed["status"] == "success"
    assert "本回答由本地模型独立完成" in resumed["response"]
    assert resumed["metrics"]["cloud_degraded"] == ["budget_exhausted"]
    assert [e["kind"] for e in ring_of(session, "cloud_degraded")] == ["budget_exhausted"]


def test_orchestrator_no_notice_without_degradation(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"text": "云端建议已参考。"},
    ])
    ok_tool = FakeTool(CLOUD_CONSULT_TOOL, result="[cloud advisor · oa/m1] advice",
                       read_only=False, concurrency_safe=False)
    orch = make_orch(engine, FakeRegistry([ok_tool]))
    session = make_session()
    session.add_user_message("请深入分析")
    begin_turn(session)
    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert "本回答由本地模型独立完成" not in result["response"]
    assert "cloud_degraded" not in result["metrics"]
    assert ring_of(session, "cloud_degraded") == []


def test_orchestrator_notice_scoped_to_current_turn(make_session):
    """上一轮降级过、这一轮没碰云端 → 这一轮不标注（扫描自最后一条 user 起）。"""
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"text": "第一轮本地作答。"},
        {"text": "第二轮纯本地。"},
    ])
    orch = make_orch(engine, FakeRegistry([_degraded_tool()]))
    session = make_session()
    session.add_user_message("第一问")
    begin_turn(session)
    r1 = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert "本回答由本地模型独立完成" in r1["response"]
    session.add_user_message("第二问")
    begin_turn(session)
    r2 = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r2["response"] == "第二轮纯本地。"
    assert "cloud_degraded" not in r2["metrics"]


def test_no_notice_when_retry_succeeded_same_turn(make_session):
    """评审 M1：首次超时、重试成功 = 真用了云端建议，不得挂「未经云端导师」。"""
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"tool_calls": [call("cloud_consult", question="q", provider="an")]},
        {"text": "根据云端建议，结论是 X。"},
    ])

    flaky = FakeTool(CLOUD_CONSULT_TOOL, read_only=False, concurrency_safe=False)
    registry = FakeRegistry([flaky])
    results = iter([degraded_observation("timeout", "ReadTimeout"),
                    "[cloud advisor · an/m2] advice"])
    _orig = registry.execute_tool

    def _exec(name, arguments):
        flaky.result = next(results)      # 首次超时、第二次成功（同轮重试/换供应商）
        return _orig(name, arguments)
    registry.execute_tool = _exec
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("请深入分析")
    begin_turn(session)
    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["response"] == "根据云端建议，结论是 X。"
    assert result["metrics"]["cloud_degraded"] == ["timeout"]       # 审计面仍记降级
    assert result["metrics"]["cloud_consult_ok"] is True


def test_other_tool_reading_degraded_text_is_not_degradation(make_session):
    """评审 M2：read_file 翻到同款日志文本 ≠ 云端降级（工具名双锚）。"""
    engine = FakeEngine([
        {"tool_calls": [call("read_file", path="/tmp/egress.log")]},
        {"text": "日志读完了。"},
        {"text": "日志读完了（该文件内容是一条 System Error 记录）。"},   # 失败吞没推回后的二次收尾
    ])
    orch = make_orch(engine, FakeRegistry([_degraded_tool(name="read_file")]))
    session = make_session()
    session.add_user_message("看日志")
    begin_turn(session)
    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    # read_file 回 System Error 仍按普通失败走 D2（推回一次）——那是既有语义；这里只守
    # "不是云端降级"：无标注、无审计键、无账本事件
    assert "本回答由本地模型独立完成" not in result["response"]
    assert "cloud_degraded" not in result["metrics"]
    assert ring_of(session, "cloud_degraded") == []


def test_fuse_exit_still_records_degradation_in_metrics(make_session):
    """评审 M5：迭代保险丝出口不挂标注（不是答复），但审计键照记。"""
    engine = FakeEngine([{"tool_calls": [call("cloud_consult", question="q")]}]
                        + [{"tool_calls": [call("noop", i=i)]} for i in range(12)])
    noop = FakeTool("noop", result="ok", read_only=True)
    orch = make_orch(engine, FakeRegistry([_degraded_tool(), noop]), max_total_iterations=4)
    session = make_session()
    session.add_user_message("x")
    begin_turn(session)
    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "warning" and "未能收敛" in result["response"]
    assert result["metrics"]["cloud_degraded"] == ["budget_exhausted"]
    assert "本回答由本地模型独立完成" not in result["response"]


def test_audit_keys_recorded_on_parse_loop_exit(make_session):
    """阶段复审 M2：parse_loop 出口也要落 cloud_degraded / cloud_upgrade 审计键。"""
    from .fake_engine import bad_call
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"tool_calls": [bad_call()]}, {"tool_calls": [bad_call()]}, {"tool_calls": [bad_call()]},
    ])
    orch = make_orch(engine, FakeRegistry([_degraded_tool()]), max_parse_retries=3)
    session = make_session()
    session.add_user_message("按状态分组统计条数与读数合计")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "warning" and "解析" in r["response"]
    assert r["metrics"]["cloud_degraded"] == ["budget_exhausted"]
    assert r["metrics"]["cloud_upgrade"][0]["action"] == "tier_suggest"


def test_tool_level_timeout_of_consult_is_degradation(make_session):
    """阶段复审 m8：cloud_consult 被工具级熔断 → 统一降级观察（timeout）+ 标注，不绕过 ⑤′-1。"""
    import time as _t
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"text": "本地作答。"},
    ])
    slow = FakeTool("cloud_consult", result="never", read_only=False, concurrency_safe=False, timeout=0.2)
    registry = FakeRegistry([slow])
    _orig = registry.execute_tool

    def _exec(name, arguments):
        _t.sleep(0.5)
        return _orig(name, arguments)
    registry.execute_tool = _exec
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("分析一下")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["metrics"]["cloud_degraded"] == ["timeout"]
    assert "本回答由本地模型独立完成" in r["response"] and "超时" in r["response"]
    assert [e["kind"] for e in ring_of(session, "cloud_degraded")] == ["timeout"]


def test_success_path_ledger_failure_is_ledger_error(tmp_path, monkeypatch):
    """阶段复审 m3：发成功后台账写不进去 → ledger_error，不是「连接失败」。"""
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("T_OA_KEY", "k1")

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "advice"}}],
                                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    class _T:
        def post(self, *a, **k): return _R()
    def _ro(entry):
        raise OSError("read-only file system")
    monkeypatch.setattr(advisor, "record_egress", _ro)
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", cfg=_cfg(), transport=_T())
    assert ei.value.kind == "ledger_error_after_send" and "已发生" in str(ei.value)
    assert "已发生并计费" in user_notice([ei.value.kind])            # 用户面不说"拒绝出网"
    # 传输失败 + 台账失败：原因不被顶替（评审 m3′）
    class _Boom:
        def post(self, *a, **k): raise TimeoutError("slow")
    with pytest.raises(CloudAdvisorError) as ei:
        advisor.consult("q", cfg=_cfg(), transport=_Boom())
    assert ei.value.kind == "ledger_error" and "TimeoutError" in str(ei.value)


def test_consult_tool_timeout_tracks_config(monkeypatch):
    from types import SimpleNamespace
    from framework.mcp.tools.cloud_consult import CloudConsultTool
    from framework.mcp.registry import MCPRegistry
    import framework.utils.config_loader as cl
    monkeypatch.setattr(cl, "load_section", lambda name: {"sanitize_timeout": 45, "timeout": 120} if name == "cloud" else {})
    tool = CloudConsultTool.__new__(CloudConsultTool)
    assert tool.timeout_seconds == 180
    # 经注册表取（property 被正确解析，而不是类属性/None 回落全局值）
    assert MCPRegistry.tool_timeout(SimpleNamespace(_tools={"cloud_consult": tool}), "cloud_consult") == 180
    monkeypatch.setattr(cl, "load_section", lambda name: {"sanitize_timeout": 60, "timeout": 300} if name == "cloud" else {})
    assert tool.timeout_seconds == 375
    monkeypatch.setattr(cl, "load_section", lambda name: {"sanitize_timeout": 0, "timeout": 0} if name == "cloud" else {})
    assert tool.timeout_seconds == 180                          # `or` 回落与 advisor 同口径


def test_abandoned_pending_turn_metrics_carry_audit_keys(make_session):
    """复审 M1′：审批挂起态的 metrics 也带审计键——用户改发新消息时它就是 turn_ended 的终态。"""
    engine = FakeEngine([
        {"tool_calls": [call("cloud_consult", question="q")]},
        {"tool_calls": [call("danger", x=1)]},
    ])
    registry = FakeRegistry([_degraded_tool(), FakeTool("danger", result="done", approval=True,
                                                        read_only=False, concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("按状态分组统计条数与读数合计")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "pending_approval"
    assert r["metrics"]["cloud_degraded"] == ["budget_exhausted"]
    assert r["metrics"]["cloud_upgrade"][0]["action"] == "tier_suggest"
