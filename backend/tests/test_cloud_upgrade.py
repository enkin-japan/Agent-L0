"""⑤′-3 升级策略金测试：决策表（纯函数）+ 编排层接线（触发/不触发/预算拒绝/一轮一次/审批）。

铁律 6 在这里的形状：编排层**代模型发起**的 cloud_consult 走与模型发起完全相同的审批闸
（pending_approval + pending_tools），云端建议只是文本观察；switch_tier 永远只提示用户。
"""
import pytest

from framework.cloud import upgrade as U
from framework.cloud.boundary import RequestFacts
from framework.cloud.degrade import CLOUD_CONSULT_TOOL, degraded_observation

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import GEN, begin_turn, ring_of, run
from framework.core.orchestrator import Orchestrator

PROV = [{"name": "oa", "kind": "openai_compat", "base_url": "https://x.example", "model": "m1",
         "key_env": "T_UP_KEY", "input_usd_per_m": 1.0, "output_usd_per_m": 1.0}]


def cloud(mode="suggest", **up):
    return {"providers": PROV, "require_approval": True,
            "upgrade": {"mode": mode, "failure_threshold": 2, **up}}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("T_UP_KEY", "k")


def make_orch(engine, registry, cloud_cfg=None, **cfg_over):
    cfg = {"react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
           "observation_max_chars": 4000, "tool_timeout_seconds": 5,
           "max_parse_retries": 3, "thinking_mode": "manual",
           "cloud": cloud_cfg if cloud_cfg is not None else cloud()}
    cfg.update(cfg_over)
    return Orchestrator(engine, registry, cfg)


def consult_tool(result="[cloud advisor · oa/m1] advice text", approval=True):
    return FakeTool(CLOUD_CONSULT_TOOL, result=result, approval=approval,
                    read_only=False, concurrency_safe=False)


# ---------- 决策表 ----------

def test_config_parsing_and_validation():
    c = U.UpgradeConfig.from_cloud_section({"upgrade": {"mode": "AUTO", "failure_threshold": 3}})
    assert (c.mode, c.failure_threshold, c.tier_notice) == ("auto", 3, True)
    assert U.UpgradeConfig.from_cloud_section({}).mode == "suggest"
    with pytest.raises(ValueError):
        U.UpgradeConfig.from_cloud_section({"upgrade": {"mode": "maybe"}})
    with pytest.raises(ValueError):
        U.UpgradeConfig.from_cloud_section({"upgrade": {"failure_threshold": 0}})


@pytest.mark.parametrize("text,hit", [
    ("问问云端导师这题怎么解", True), ("让 Claude 看看这段代码", True), ("ask the cloud advisor", True),
    ("please consult the cloud model", True), ("call cloud_consult for this", True),
    ("ask the cloud?", True), ("ask Claude about it", True), ("让  GPT 来分析", True),
    ("今天天气", False), ("cloudflare 配置怎么写", False), ("use the cloud-init script", False),
    ("不要问云端", False), ("别让云端插手", False), ("don't ask the cloud advisor", False),
    ("ask the cloud provider's status page", False), ("consult the cloud config", False),
    ("Claude Code 怎么用", False), ("cloud storage 怎么配", False),
])
def test_explicit_request_detection(text, hit):
    assert U.explicit_request(text) is hit


def _one(ds):
    assert len(ds) == 1, ds
    return ds[0]


@pytest.fixture
def tier_entry(monkeypatch):
    """目录现无 switch_tier 条目（B1 降 caveat，B3 改 tool_assist）；换档建议机制用临时条目守。"""
    import framework.cloud.boundary as bd
    from framework.cloud.boundary import Boundary, RequestFacts as RF
    entry = Boundary(id="tmp_tier", title="临时换档条目", predicate=lambda f: "换档探针" in f.text,
                     status="confirmed", action="switch_tier", action_target="mlx-community/Qwen3.8-27B-4bit",
                     evidence="e", probes="p", action_gen={"enable_thinking": True, "thinking_budget": 1024},
                     action_cost="该档在这类任务上约慢 5–8 倍（实测 4.9–7.8×）")
    monkeypatch.setattr(bd, "CATALOG", bd.CATALOG + (entry,))
    return entry


def test_turn_start_decision_table(chart_caveat_live):
    c = cloud()
    up = U.UpgradeConfig(mode="suggest")
    D = lambda text, cfg=up, cc=c, reg=True, **k: U.decide_at_turn_start(
        RequestFacts(text=text, **k), cfg, cloud_cfg=cc, consult_registered=reg)
    # 显式 > 目录（consult 类只出一条）
    d = _one(D("请严格证明它，问问云端"))
    assert (d.trigger, d.action) == ("explicit", "consult_suggest")
    assert _one(D("问问云端", U.UpgradeConfig(mode="auto"))).action == "consult_auto"
    # off = 护栏整体关（显式要求也不出决策，模型自己仍可调工具）
    assert D("问问云端", U.UpgradeConfig(mode="off")) == []
    assert D("按状态分组统计条数与读数合计", U.UpgradeConfig(mode="off")) == []
    # 显式但不可用 → unavailable，原因是用户可读短语、不含 key_env 名
    d = _one(D("问问云端", reg=False))
    assert d.action == "unavailable" and "未注册" in d.reason
    nokey = [dict(PROV[0], key_env="T_UP_MISSING_KEY")]      # 有供应商、环境无钥
    d = _one(D("问问云端", cc={"providers": nokey}))
    assert d.action == "unavailable" and d.reason == "云端供应商未配置密钥" and "T_UP_MISSING_KEY" not in d.reason
    # 目录 cloud_consult（weak）：suggest 含 weak；auto 只认 confirmed → 无
    f = "请严格证明这个不等式"
    assert _one(D(f)).action == "consult_suggest"
    assert D(f, U.UpgradeConfig(mode="auto")) == []
    assert D(f, cc={"providers": []}) == []                # 目录命中但云端不可用 → 不打扰
    # 目录 caveat（B1）→ 只给用户提醒；与云端可用性、tier_notice 无关
    dv = _one(D("这张柱状图的峰值是多少", has_images=True, cc={"providers": []}, reg=False))
    assert dv.action == "caveat" and "核对" in dv.notice and "vision_chart_numeric" not in dv.reason
    assert U.user_notice(dv).startswith("—— 提示：图表数值读取是本档已知弱项") and U.model_hint(dv)
    assert _one(D("这张柱状图的峰值是多少", U.UpgradeConfig(tier_notice=False), has_images=True)).action == "caveat"
    # 目录 tool_assist → tool_hint（B3：算术交 run_math；与云端可用性无关；工具须已注册）
    dt = _one(D("按状态分组统计条数与读数合计", cc={"providers": []}, reg=False))
    assert dt.action == "tool_hint" and dt.tier_target == "run_math" and "run_math" in dt.hint
    assert "run_math" in U.model_hint(dt) and U.user_notice(dt) == ""     # 只提醒模型，不打扰用户
    assert D("按状态分组统计条数与读数合计", cc={"providers": []}, reg=False,
             ) and U.decide_at_turn_start(RequestFacts(text="按状态分组统计条数与读数合计"), up,
                                          cloud_cfg={"providers": []}, consult_registered=False,
                                          registered_tools={"read_file"}) == []   # run_math 未注册 → 不出决策
    # consult 与 tool_hint 并存：两条都出（评审 m16）
    ds = D("请严格证明各组合计的趋势")
    assert [x.action for x in ds] == ["consult_suggest", "tool_hint"]
    # 无命中
    assert D("今天吃什么") == []


def test_catalog_auto_consult_currently_unreachable_pin():
    """auto 档目录触发只认 confirmed；目录里现无 confirmed 的 cloud_consult 条目。本测试红了
    = 有人把目录条目升成 confirmed+cloud_consult，从此编排层会**自动**代发起出网——请确认这是
    有意为之再改这里。"""
    from framework.cloud.boundary import CATALOG
    assert not [b.id for b in CATALOG if b.status == "confirmed" and b.action == "cloud_consult"]


def test_failures_decision():
    c = cloud()
    up = U.UpgradeConfig(mode="suggest", failure_threshold=2)
    assert U.decide_on_failures(1, up, cloud_cfg=c, consult_registered=True) is None
    assert U.decide_on_failures(2, up, cloud_cfg=c, consult_registered=True).action == "consult_suggest"
    assert U.decide_on_failures(5, U.UpgradeConfig(mode="auto"), cloud_cfg=c, consult_registered=True).action == "consult_auto"
    assert U.decide_on_failures(5, U.UpgradeConfig(mode="off"), cloud_cfg=c, consult_registered=True) is None
    assert U.decide_on_failures(5, up, cloud_cfg={"providers": []}, consult_registered=True) is None


def test_user_notice_shapes():
    d = U.Decision("catalog", "tier_suggest", ["implicit_aggregation"], "mlx-community/Qwen3.8-27B-4bit",
                   {"enable_thinking": True, "thinking_budget": 1024}, "散落记录的隐式聚合")
    n = U.user_notice(d)
    assert "Qwen3.8-27B-4bit" in n and "devctl.sh model" in n and "开启思考" in n and "本回答仍由当前档完成" in n
    assert "切换后响应会更慢" in n                       # 无实测代价句 → 通用措辞
    d2 = U.Decision("catalog", "tier_suggest", ["implicit_aggregation"], "mlx-community/Qwen3.8-27B-4bit",
                    None, "散落记录的隐式聚合", tier_cost="该档在这类任务上约慢 5–8 倍（实测 4.9–7.8×）")
    assert "4.9–7.8×" in U.user_notice(d2)
    assert U.user_notice(U.Decision("explicit", "unavailable", reason="no_key: x")).startswith("—— 你要求的云端导师当前不可用")
    assert U.user_notice(U.Decision("catalog", "consult_suggest")) == ""
    assert "colab" not in (n + U.model_hint(d)).lower()


def test_synthetic_call_caps_and_failure_context():
    c = U.synthetic_consult_call("x" * 5000)
    assert c["arguments"]["question"].endswith("…[截断]") and len(c["arguments"]["question"]) == 4000 + len("…[截断]")
    assert "context" not in c["arguments"]
    obs = [{"name": "a", "response": "ok"}, {"name": "b", "response": "System Error: boom 1"},
           {"name": "c", "response": "  System Error: boom 2"}]
    ctx = U.failure_context(obs)
    assert "[local failure 1] System Error: boom 1" in ctx and "boom 2" in ctx and "ok" not in ctx
    assert U.synthetic_consult_call("q", ctx)["arguments"]["context"] == ctx


def test_facts_from_user_message_blocks():
    msg = {"role": "user", "content": [{"type": "text", "text": "合计多少"},
                                        {"type": "text", "text": "材料" * 1000},
                                        {"type": "image_url", "image_url": {"url": "x.png"}}]}
    f = U.facts_from_user_message(msg)
    assert f.text == "合计多少" and f.has_images and f.attachments_chars == 2000
    assert U.facts_from_user_message({"role": "user", "content": "纯字符串"}).text == "纯字符串"


# ---------- 编排层接线 ----------

def test_suggest_mode_injects_one_shot_hint_and_records(make_session):
    engine = FakeEngine([{"text": "证明如下：……"}])
    orch = make_orch(engine, FakeRegistry([consult_tool()]))
    session = make_session()
    session.add_user_message("请严格证明这个不等式")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "success"
    assert any("Cloud advisor suggested" in n for n in engine.calls[0]["tail_notes"])
    ev = ring_of(session, "upgrade_triggered")
    assert ev == [{"trigger": "catalog", "action": "consult_suggest", "boundaries": ["deep_reasoning"],
                   "tier_target": None, "reason": "深推理（证明/推导/严格多步）"}]
    assert r["metrics"]["cloud_upgrade"][0]["action"] == "consult_suggest"
    assert "—— " not in r["response"]                      # 建议档不给用户加提示


def test_auto_mode_initiates_consult_through_approval_gate(make_session):
    engine = FakeEngine([{"text": "结合云端建议，结论是 X。"}])
    registry = FakeRegistry([consult_tool()])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("问问云端导师：这个方案可行吗")
    begin_turn(session)
    pending = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert pending["status"] == "pending_approval"
    assert pending["pending_tools"] == [{"name": CLOUD_CONSULT_TOOL,
                                         "arguments": {"question": "问问云端导师：这个方案可行吗"}}]
    assert engine.calls == [], "代发起在任何生成之前；模型一个 token 都没生成"
    assert registry.executed == [], "审批前不出网"
    assert session.history[-1]["role"] == "assistant" and session.history[-1]["tool_calls"][0]["name"] == CLOUD_CONSULT_TOOL
    assert ring_of(session, "upgrade_triggered")[0]["action"] == "consult_auto"
    resumed = run(orch.resume_after_approval(session, approved=True, journal=session.journal, **GEN))
    assert resumed["status"] == "success" and resumed["response"] == "结合云端建议，结论是 X。"
    assert registry.executed == [{"name": CLOUD_CONSULT_TOOL, "args": {"question": "问问云端导师：这个方案可行吗"}}]
    assert len(ring_of(session, "upgrade_triggered")) == 1, "续跑重入不重触发"
    assert session.history[-2]["role"] == "tool" and session.history[-2]["tool_responses"][0]["name"] == CLOUD_CONSULT_TOOL


def test_auto_mode_denied_does_not_egress(make_session):
    engine = FakeEngine([{"text": "本地作答。"}])
    registry = FakeRegistry([consult_tool()])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("问问云端导师")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))
    r = run(orch.resume_after_approval(session, approved=False, journal=session.journal, **GEN))
    assert r["status"] == "success" and registry.executed == []


def test_auto_mode_without_approval_knob_executes_inline(make_session):
    engine = FakeEngine([{"text": "ok"}])
    registry = FakeRegistry([consult_tool(approval=False)])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("ask the cloud advisor about this")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "success" and len(registry.executed) == 1
    roles = [m["role"] for m in session.history]
    assert roles[-3:] == ["assistant", "tool", "assistant"]


def test_budget_refusal_branch_degrades_once_and_annotates(make_session):
    """auto 代发起 → 预算耗尽 → ⑤′-1 降级观察 → 本地作答 + 标注；不再重触发。"""
    engine = FakeEngine([{"text": "本地分析如下。"}])
    registry = FakeRegistry([consult_tool(result=degraded_observation("budget_exhausted", "日预算已用尽"))])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("问问云端导师")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))
    r = run(orch.resume_after_approval(session, approved=True, journal=session.journal, **GEN))
    assert r["status"] == "success"
    assert "本回答由本地模型独立完成" in r["response"] and "预算" in r["response"]
    assert r["metrics"]["cloud_degraded"] == ["budget_exhausted"]
    assert len(ring_of(session, "upgrade_triggered")) == 1


def test_tier_suggest_appends_user_notice_only(make_session, tier_entry):
    engine = FakeEngine([{"text": "结果如下。"}])
    orch = make_orch(engine, FakeRegistry([consult_tool()]))
    session = make_session()
    session.add_user_message("换档探针")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["response"].startswith("结果如下。")
    assert "Qwen3.8-27B-4bit" in r["response"] and "devctl.sh model" in r["response"] and "4.9–7.8×" in r["response"]
    assert engine.calls[0]["tail_notes"] == []              # 换档不打扰模型
    assert r["metrics"]["cloud_upgrade"][0]["action"] == "tier_suggest"
    assert U.decide_at_turn_start(RequestFacts(text="换档探针"), U.UpgradeConfig(tier_notice=False),
                                  cloud_cfg=cloud(), consult_registered=True) == []


def test_caveat_appends_user_notice_only(make_session, chart_caveat_live):
    """B1（用户 08-23 拍板降为 caveat）：带图图表问法 → 答复末尾一行核对提醒；不换档、不出网、不扰模型。"""
    engine = FakeEngine([{"text": "峰值约 42。"}])
    registry = FakeRegistry([consult_tool()])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("这张柱状图的峰值是多少", images=["chart.png"])
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "success" and r["response"].startswith("峰值约 42。")
    assert "图表数值读取是本档已知弱项" in r["response"] and "核对" in r["response"]
    assert engine.calls[0]["tail_notes"] == [] and registry.executed == []
    assert r["metrics"]["cloud_upgrade"][0]["action"] == "caveat"


def test_tool_hint_injects_model_note_without_user_notice(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("run_math", code="sum([1,2])")]},
        {"text": "| 正常 | 3 | 1798 |"},
    ])
    orch = make_orch(engine, FakeRegistry([consult_tool(), FakeTool("run_math", result="3", read_only=True)]))
    session = make_session()
    session.add_user_message("按状态分组统计条数与读数合计")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["response"] == "| 正常 | 3 | 1798 |"            # 用户面无提示
    assert any("run_math" in n for n in engine.calls[0]["tail_notes"])
    assert engine.calls[1]["tail_notes"] == []                # 一次性：第二次生成不再注
    assert not any("run_math tool" in str(m.get("content", "")) for m in session.history)   # 不进历史
    assert r["metrics"]["cloud_upgrade"][0]["action"] == "tool_hint"


def test_tool_hint_skipped_when_tool_not_registered(make_session):
    engine = FakeEngine([{"text": "| 正常 | 3 | 1798 |"}])
    orch = make_orch(engine, FakeRegistry([consult_tool()]))        # 无 run_math
    session = make_session()
    session.add_user_message("按状态分组统计条数与读数合计")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert engine.calls[0]["tail_notes"] == [] and "cloud_upgrade" not in r["metrics"]


def test_tool_hint_does_not_block_failures_escalation(make_session):
    """评审 M1：tool_hint 不占"本轮云端咨询"名额——之后连续失败仍可触发 consult_suggest。"""
    engine = FakeEngine([
        {"tool_calls": [call("flaky", n=1)]},
        {"tool_calls": [call("flaky", n=2)]},
        {"text": "done"},
        {"text": "失败了。"},
    ])
    flaky = FakeTool("flaky", result="System Error: boom", read_only=True)
    orch = make_orch(engine, FakeRegistry([consult_tool(), flaky, FakeTool("run_math", result="0", read_only=True)]))
    session = make_session()
    session.add_user_message("按状态分组统计条数与读数合计")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))
    assert [(e["trigger"], e["action"]) for e in ring_of(session, "upgrade_triggered")] == \
        [("catalog", "tool_hint"), ("failures", "consult_suggest")]


def test_auto_consult_then_tool_hint_both_recorded_and_replayed(make_session):
    """评审 m3：显式要云端 + 聚合题：两条决策都入账；审批续跑后 run_math 尾注补注一次。"""
    engine = FakeEngine([{"text": "结合建议，合计如下。"}])
    registry = FakeRegistry([consult_tool(), FakeTool("run_math", result="0", read_only=True)])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("问问云端导师：按状态分组统计条数与读数合计")
    begin_turn(session)
    p = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert p["status"] == "pending_approval"
    assert [d["action"] for d in p["metrics"]["cloud_upgrade"]] == ["consult_auto", "tool_hint"]
    r = run(orch.resume_after_approval(session, approved=True, journal=session.journal, **GEN))
    assert [d["action"] for d in r["metrics"]["cloud_upgrade"]] == ["consult_auto", "tool_hint"]
    assert any("run_math" in n for n in engine.calls[0]["tail_notes"])


def test_explicit_request_but_cloud_unavailable_is_told(make_session):
    engine = FakeEngine([{"text": "本地回答。"}])
    orch = make_orch(engine, FakeRegistry([]), cloud_cfg={"providers": [], "upgrade": {"mode": "auto"}})
    session = make_session()
    session.add_user_message("问问云端导师")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "success"
    assert "云端导师当前不可用" in r["response"] and "未配置云端供应商" in r["response"]
    assert "no_provider" not in r["response"]               # 用户面不露内部 kind


@pytest.mark.parametrize("prompt,images", [("请严格证明这个不等式", None), ("按状态分组统计条数与读数合计", None),
                                           ("问问云端导师", None), ("这张柱状图的峰值是多少", ["chart.png"])])
def test_off_mode_never_triggers(make_session, prompt, images, chart_caveat_live):
    engine = FakeEngine([{"text": "ok"}])
    orch = make_orch(engine, FakeRegistry([consult_tool(), FakeTool("run_math", result="0", read_only=True)]),
                     cloud_cfg=cloud("off"))
    session = make_session()
    session.add_user_message(prompt, images=images)
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert ring_of(session, "upgrade_triggered") == [] and "cloud_upgrade" not in r["metrics"]
    assert engine.calls[0]["tail_notes"] == [] and r["response"] == "ok"


def test_no_trigger_on_ordinary_turn(make_session):
    engine = FakeEngine([{"text": "ok"}])
    orch = make_orch(engine, FakeRegistry([consult_tool()]), cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("今天吃什么")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "success" and ring_of(session, "upgrade_triggered") == []


def test_failures_trigger_suggest_after_threshold(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("flaky", n=1)]},
        {"tool_calls": [call("flaky", n=2)]},
        {"text": "done"},
        {"text": "两次都失败了，无法完成。"},       # D2 失败吞没推回后的二次收尾（既有语义）
    ])
    flaky = FakeTool("flaky", result="System Error: boom", read_only=True)
    orch = make_orch(engine, FakeRegistry([consult_tool(), flaky]))
    session = make_session()
    session.add_user_message("做这件事")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    ev = ring_of(session, "upgrade_triggered")
    assert [e["trigger"] for e in ev] == ["failures"] and ev[0]["action"] == "consult_suggest"
    assert any("Cloud advisor suggested" in n for n in engine.calls[2]["tail_notes"])
    assert not any("Cloud advisor" in n for n in engine.calls[1]["tail_notes"]), "阈前不提醒"


def test_failures_trigger_auto_initiates_consult(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("flaky", n=1)]},
        {"tool_calls": [call("flaky", n=2)]},
        {"text": "有了建议，这样做。"},
    ])
    flaky = FakeTool("flaky", result="System Error: boom", read_only=True)
    registry = FakeRegistry([consult_tool(), flaky])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("做这件事")
    begin_turn(session)
    pending = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert pending["status"] == "pending_approval" and pending["pending_tools"][0]["name"] == CLOUD_CONSULT_TOOL
    r = run(orch.resume_after_approval(session, approved=True, journal=session.journal, **GEN))
    assert r["status"] == "success"
    assert [e["name"] for e in registry.executed] == ["flaky", "flaky", CLOUD_CONSULT_TOOL]
    assert len(ring_of(session, "upgrade_triggered")) == 1
    # 失败触发的代发起把失败观察作 context 交给顾问（评审 M5）
    assert "System Error: boom" in registry.executed[-1]["args"]["context"]


def test_failure_counter_resets_on_success(make_session):
    engine = FakeEngine([
        {"tool_calls": [call("flaky", n=1)]},
        {"tool_calls": [call("good", n=1)]},
        {"tool_calls": [call("flaky", n=2)]},
        {"text": "done"},
        {"text": "最后一步失败了。"},               # D2 推回后的二次收尾
    ])
    registry = FakeRegistry([consult_tool(), FakeTool("flaky", result="System Error: boom", read_only=True),
                             FakeTool("good", result="ok", read_only=True)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("做这件事")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))
    assert ring_of(session, "upgrade_triggered") == []


def test_suspend_resume_does_not_retrigger(make_session):
    """挂起续跑（compact_history=False 重入）不是新轮：不再判、不再提醒、不再记事件。"""
    engine = FakeEngine([{"tool_calls": [call("probe", n=1)]}, {"text": "证明如下。"}])
    registry = FakeRegistry([consult_tool(), FakeTool("probe", result="ok", read_only=True)])
    session = make_session()
    _orig = registry.execute_tool

    def _exec(name, arguments):          # 工具执行中用户点了挂起 → 下一迭代边界让出
        session.suspend_requested = True
        return _orig(name, arguments)
    registry.execute_tool = _exec
    orch = make_orch(engine, registry)
    session.add_user_message("请严格证明这个不等式")
    begin_turn(session)
    first = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert first["status"] == "suspended"
    assert len(ring_of(session, "upgrade_triggered")) == 1
    r = run(orch.run_turn(session, journal=session.journal, compact_history=False, **GEN))
    assert r["status"] == "success"
    assert len(ring_of(session, "upgrade_triggered")) == 1
    # 续跑段没有轮首决策 → 没有尾注（轮首那段的提醒已随挂起作废）；这是已知口径，记档即可


def test_tier_notice_survives_approval_resume(make_session, chart_caveat_live):
    """评审 M2：轮首决策（此处 B1 caveat）经审批续跑后仍落提示与审计键。"""
    engine = FakeEngine([
        {"tool_calls": [call("danger", x=1)]},
        {"text": "| 正常 | 3 | 1798 |"},
    ])
    registry = FakeRegistry([consult_tool(), FakeTool("danger", result="done", approval=True,
                                                      read_only=False, concurrency_safe=False)])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("这张柱状图的峰值是多少", images=["chart.png"])
    begin_turn(session)
    assert run(orch.run_turn(session, journal=session.journal, **GEN))["status"] == "pending_approval"
    r = run(orch.resume_after_approval(session, approved=True, journal=session.journal, **GEN))
    assert r["status"] == "success"
    assert "核对" in r["response"] and r["metrics"]["cloud_upgrade"][0]["action"] == "caveat"
    assert len(ring_of(session, "upgrade_triggered")) == 1


def test_cancelled_turn_does_not_egress(make_session):
    """评审 M1：用户已取消的轮不得代发起出网。"""
    engine = FakeEngine([{"text": "ok"}])
    registry = FakeRegistry([consult_tool(approval=False)])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("问问云端导师")
    begin_turn(session)
    session.cancel_requested = True
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "cancelled" and registry.executed == []
    assert ring_of(session, "upgrade_triggered") == []


def test_unattended_turn_downgrades_auto_to_suggest(make_session):
    """评审 m19：定时轮（origin=schedule）没人点审批 → auto 降为 suggest。"""
    from framework.core.journal import new_turn_id
    engine = FakeEngine([{"text": "ok"}])
    registry = FakeRegistry([consult_tool()])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("问问云端导师")
    session.journal.emit("turn_queued", {"prompt": "问问云端导师", "attachments": [], "gen": dict(GEN),
                                         "origin": "schedule"}, turn_id=new_turn_id())
    session.journal.emit("turn_started", {})
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert r["status"] == "success" and registry.executed == []
    assert ring_of(session, "upgrade_triggered")[0]["action"] == "consult_suggest"
    assert any("Cloud advisor suggested" in n for n in engine.calls[0]["tail_notes"])


def test_approved_consult_does_not_disarm_fabrication_guard(make_session):
    """评审 M3（行为版）：批准一次咨询 ≠ 本轮有成功变更；零工具却声称"已创建文件"仍被推回。"""
    engine = FakeEngine([
        {"text": "已经为您创建了文件 a.txt。"},     # 零变更声明 → 推回
        {"text": "我没有创建任何文件；这是建议。"},   # 改口 → 放行
    ])
    registry = FakeRegistry([consult_tool()])
    orch = make_orch(engine, registry, cloud_cfg=cloud("auto"))
    session = make_session()
    session.add_user_message("问问云端导师：这个方案可行吗")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))
    r = run(orch.resume_after_approval(session, approved=True, journal=session.journal, **GEN))
    assert r["status"] == "success" and r["response"].startswith("我没有创建任何文件")
    assert "claims you performed actions" in " ".join(engine.calls[1]["tail_notes"])


def test_duplicate_paid_consult_still_blocked_after_a_write(make_session):
    """阶段复审 M1：cloud_consult 不是只读——同参重复调用的指纹不得被写屏障清掉（付费重复出网）。"""
    engine = FakeEngine([
        {"tool_calls": [call(CLOUD_CONSULT_TOOL, question="q")]},
        {"tool_calls": [call("write_file", path="/tmp/x", content="1")]},
        {"tool_calls": [call(CLOUD_CONSULT_TOOL, question="q")]},       # 同参重发 → 拦
        {"text": "done"},
    ])
    registry = FakeRegistry([consult_tool(approval=False),
                             FakeTool("write_file", result="ok", read_only=False, concurrency_safe=False)])
    orch = make_orch(engine, registry, cloud_cfg=cloud("off"))
    session = make_session()
    session.add_user_message("做这件事")
    begin_turn(session)
    r = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert [e["name"] for e in registry.executed] == [CLOUD_CONSULT_TOOL, "write_file"]
    blocked = [tr for m in session.history if m.get("role") == "tool"
               for tr in m.get("tool_responses", []) if str(tr.get("response", "")).startswith("Duplicate call blocked")]
    assert len(blocked) == 1 and blocked[0]["name"] == CLOUD_CONSULT_TOOL
    assert r["response"] == "done"


def test_failures_count_batches_not_calls(make_session):
    """评审 m17：同一迭代两个调用都失败只算一次（阈 2 不被一批触发）。"""
    engine = FakeEngine([
        {"tool_calls": [call("flaky", n=1), call("flaky", n=2)]},
        {"text": "done"},
        {"text": "失败了。"},
    ])
    flaky = FakeTool("flaky", result="System Error: boom", read_only=True)
    orch = make_orch(engine, FakeRegistry([consult_tool(), flaky]))
    session = make_session()
    session.add_user_message("做这件事")
    begin_turn(session)
    run(orch.run_turn(session, journal=session.journal, **GEN))
    assert ring_of(session, "upgrade_triggered") == []


def test_real_cloud_consult_is_not_read_only():
    """阶段复审 M1 的真钉：上面的行为测试跑在 FakeTool 上，真工具若再被标 is_read_only=True，
    写屏障会清掉它的重复指纹（付费重复出网）而行为测试毫无反应——变异 U 首跑存活证明了这一点。"""
    from types import SimpleNamespace
    from framework.mcp.tools.cloud_consult import CloudConsultTool
    from framework.mcp.registry import MCPRegistry
    assert getattr(CloudConsultTool, "is_read_only", None) is not True
    stub = SimpleNamespace(_tools={CLOUD_CONSULT_TOOL: CloudConsultTool.__new__(CloudConsultTool)})
    assert MCPRegistry.is_read_only(stub, CLOUD_CONSULT_TOOL, {"question": "q"}) is False
