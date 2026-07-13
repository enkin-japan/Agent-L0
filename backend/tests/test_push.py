"""推送层金测试（framework/push）：JWT 签名 / 环境自愈 / 注册表 / 分发映射 /
内容分级 / Live Activity 归约与收尾 / 审批 expected_turn_id 防错批。

APNs 网络层由 FakeAPNs 替身扮演（零出网），dispatcher 的映射逻辑全部确定性驱动。
"""
import asyncio
import base64
import json
import time
import types

import pytest

from framework.core.feed import GlobalFeed
from framework.push.apns import APNsClient
from framework.push.dispatcher import NotificationDispatcher, _ActivityState
from framework.push.registry import PushRegistry, DEFAULT_PREFS


def run(coro):
    return asyncio.run(coro)


# ---------- 替身 ----------

class FakeAPNs:
    """捕获全部发送；可按 token 脚本化返回结果（默认成功）。"""

    def __init__(self):
        self.sent = []
        self.script = {}   # token -> result dict（一次性队列或固定 dict）

    async def send(self, token, payload, *, topic, push_type="alert", priority=10,
                   expiration=0, collapse_id=None, environment="prod"):
        self.sent.append({
            "token": token, "payload": payload, "topic": topic,
            "push_type": push_type, "priority": priority,
            "expiration": expiration, "collapse_id": collapse_id,
            "environment": environment,
        })
        res = self.script.get(token)
        if isinstance(res, list):
            res = res.pop(0) if res else None
        if res is None:
            env = environment if environment in ("prod", "sandbox") else "prod"
            res = {"ok": True, "status": 200, "reason": "", "environment": env}
        return dict(res)

    async def aclose(self):
        pass

    def by_kind(self, kind):
        return [s for s in self.sent
                if (s["payload"].get("ma") or {}).get("kind") == kind]


def make_dispatcher(tmp_path, devices=(), cfg=None):
    registry = PushRegistry(tmp_path / "push")
    for d in devices:
        registry.upsert(**d)
    apns = FakeAPNs()
    disp = NotificationDispatcher(
        GlobalFeed(), registry, apns,
        types.SimpleNamespace(_all_sessions={}),
        cfg or {"bundle_id": "dev.enkin.macagent", "approval_ttl_seconds": 900,
                "live_activity": {"enabled": True, "heartbeat_seconds": 60,
                                  "stale_seconds": 150, "min_push_interval": 0.05}},
        tmp_path / "sessions")
    return disp, registry, apns


def feed_item(etype, sid="s-1", turn_id="t_a", **data):
    return {"feed_seq": 1, "session_id": sid, "turn_id": turn_id,
            "ts": time.time(), "type": etype, "data": data}


async def route_turn(disp, *items):
    for it in items:
        await disp._route(it)


# ---------- APNs 客户端：ES256 JWT ----------

def _make_p8(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_key_bytes if False else key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    p = tmp_path / "AuthKey_TEST.p8"
    p.write_bytes(pem)
    return p, key.public_key()


def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jwt_es256_sign_and_cache(tmp_path):
    p8, pub = _make_p8(tmp_path)
    client = APNsClient(str(p8), "KEY1234567", "TEAM123456")
    tok1 = client._jwt()
    tok2 = client._jwt()
    assert tok1 == tok2                      # TTL 内缓存复用
    h, c, sig = tok1.split(".")
    header = json.loads(_b64url_decode(h))
    claims = json.loads(_b64url_decode(c))
    assert header == {"alg": "ES256", "kid": "KEY1234567"}
    assert claims["iss"] == "TEAM123456" and abs(claims["iat"] - time.time()) < 60
    # 裸 r‖s 转回 DER 验签
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    raw = _b64url_decode(sig)
    assert len(raw) == 64
    der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"),
                                     int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))  # 不抛=验签过


def test_apns_env_auto_fallback(tmp_path, monkeypatch):
    p8, _ = _make_p8(tmp_path)
    client = APNsClient(str(p8), "K", "T")
    calls = []

    async def fake_once(token, payload, *, topic, push_type, priority,
                        expiration, collapse_id, env):
        calls.append(env)
        if env == "prod":
            return {"ok": False, "status": 400, "reason": "BadDeviceToken",
                    "environment": env}
        return {"ok": True, "status": 200, "reason": "", "environment": env}

    monkeypatch.setattr(client, "_send_once", fake_once)
    res = run(client.send("tok", {}, topic="x", environment="auto"))
    assert res["ok"] and res["environment"] == "sandbox"
    assert calls == ["prod", "sandbox"]      # 只有 BadDeviceToken 才换环境

    calls.clear()

    async def fail_other(token, payload, *, topic, push_type, priority,
                         expiration, collapse_id, env):
        calls.append(env)
        return {"ok": False, "status": 400, "reason": "BadTopic", "environment": env}

    monkeypatch.setattr(client, "_send_once", fail_other)
    res = run(client.send("tok", {}, topic="x", environment="auto"))
    assert not res["ok"] and calls == ["prod"]   # 非环境错配不重试


# ---------- 注册表 ----------

def test_registry_defaults_merge_and_validation(tmp_path):
    reg = PushRegistry(tmp_path / "push")
    d = reg.upsert("dev-1", token="aa")
    assert d["prefs"] == DEFAULT_PREFS
    assert d["prefs"]["approval"] is True and d["prefs"]["schedule_start"] is False
    d = reg.update_prefs("dev-1", {"schedule_start": 1, "content_level": "bogus"})
    assert d["prefs"]["schedule_start"] is True        # 真值布尔化
    assert d["prefs"]["content_level"] == "category"   # 非法档位忽略
    assert reg.update_prefs("ghost", {}) is None


def test_registry_rotation_persistence_and_cleanup(tmp_path):
    reg = PushRegistry(tmp_path / "push")
    reg.upsert("dev-1", token="aa")
    reg.set_env("dev-1", "prod")
    reg.upsert("dev-1", token="aa")                    # 同 token：环境保持
    assert reg.devices["dev-1"]["env"] == "prod"
    reg.upsert("dev-1", token="bb")                    # token 轮换：环境重学
    assert reg.devices["dev-1"]["env"] == "auto"
    # 落盘回读
    reg2 = PushRegistry(tmp_path / "push")
    assert "dev-1" in reg2.devices and reg2.devices["dev-1"]["token"] == "bb"
    reg2.remove_by_token("bb")
    assert reg2.devices == {}


# ---------- 分发映射：横幅 ----------

def test_alert_approval_interactive(tmp_path):
    disp, _, apns = make_dispatcher(tmp_path, [
        {"device_id": "d1", "token": "tok1", "prefs": {"content_level": "full"}}])
    run(route_turn(
        disp,
        feed_item("turn_queued", prompt="部署到生产", origin="chat"),
        feed_item("awaiting_approval",
                  pending_tools=[{"name": "danger_op", "arguments": {}}])))
    assert len(apns.sent) == 1
    s = apns.sent[0]
    aps = s["payload"]["aps"]
    assert aps["category"] == "APPROVAL"
    assert aps["interruption-level"] == "time-sensitive"
    assert aps["thread-id"] == "s-1"
    assert "danger_op" in aps["alert"]["body"]                 # full 档带工具名
    assert s["payload"]["ma"] == {"v": 1, "kind": "approval",
                                  "session_id": "s-1", "turn_id": "t_a"}
    assert abs(s["expiration"] - (time.time() + 900)) < 10    # TTL 过期不投
    assert s["collapse_id"] == "t_a:approval"


def test_alert_schedule_autodeny_is_report_not_card(tmp_path):
    """定时轮的 awaiting_approval 紧跟自动拒绝——推通报，绝不推互动审批卡。"""
    disp, _, apns = make_dispatcher(tmp_path, [
        {"device_id": "d1", "token": "tok1"}])
    run(route_turn(
        disp,
        feed_item("turn_queued", prompt="Task: 夜间巡检", origin="schedule"),
        feed_item("awaiting_approval",
                  pending_tools=[{"name": "write_file", "arguments": {}}])))
    assert len(apns.sent) == 1
    aps = apns.sent[0]["payload"]["aps"]
    assert "category" not in aps                                # 无互动按钮
    assert apns.sent[0]["payload"]["ma"]["kind"] == "approval_autodenied"
    assert "自动拒绝" in aps["alert"]["body"]


def test_alert_result_levels_and_cancelled_skipped(tmp_path):
    disp, _, apns = make_dispatcher(tmp_path, [
        {"device_id": "df", "token": "tf", "prefs": {"content_level": "full"}},
        {"device_id": "dc", "token": "tc", "prefs": {"content_level": "category"}},
        {"device_id": "dp", "token": "tp", "prefs": {"content_level": "placeholder"}},
    ])
    run(route_turn(
        disp,
        feed_item("turn_queued", prompt="统计季度报表", origin="chat"),
        feed_item("turn_ended", status="success", turn_index=0)))
    bodies = {s["token"]: s["payload"]["aps"]["alert"]["body"] for s in apns.sent}
    assert "统计季度报表" in bodies["tf"]            # full：带任务内容
    assert "统计季度报表" not in bodies["tc"]        # category：只说类别
    assert bodies["tp"] == "有新事件，点开查看。"     # placeholder：零内容
    titles = {s["token"]: s["payload"]["aps"]["alert"]["title"] for s in apns.sent}
    assert titles["tp"] == "MacAgent"

    apns.sent.clear()
    run(route_turn(
        disp,
        feed_item("turn_queued", turn_id="t_b", prompt="x", origin="chat"),
        feed_item("turn_ended", turn_id="t_b", status="cancelled", turn_index=1)))
    assert apns.sent == []                           # 用户自己取消的不通知


def test_alert_pref_gates_and_schedule_start(tmp_path):
    disp, _, apns = make_dispatcher(tmp_path, [
        {"device_id": "d1", "token": "t1"},                                   # schedule_start 默认关
        {"device_id": "d2", "token": "t2", "prefs": {"schedule_start": True}},
        {"device_id": "d3", "token": "t3", "prefs": {"schedule_start": True},
         "push_to_start_token": "pts3"},
    ])
    run(route_turn(
        disp,
        feed_item("turn_queued", prompt="Task: 巡检", origin="schedule"),
        feed_item("turn_started")))
    alerts = [s for s in apns.sent if s["push_type"] == "alert"]
    assert sorted(s["token"] for s in alerts) == ["t2", "t3"]
    starts = [s for s in apns.sent if s["push_type"] == "liveactivity"]
    assert [s["token"] for s in starts] == ["pts3"]              # push-to-start 只发有 token 的
    aps = starts[0]["payload"]["aps"]
    assert aps["event"] == "start"
    assert aps["attributes-type"] == "AgentActivityAttributes"
    assert aps["attributes"]["sessionId"] == "s-1"
    assert starts[0]["topic"].endswith(".push-type.liveactivity")


def test_env_sticky_learning_and_410_cleanup(tmp_path):
    disp, reg, apns = make_dispatcher(tmp_path, [
        {"device_id": "d1", "token": "t1"},
        {"device_id": "d2", "token": "t2"},
    ])
    apns.script["t1"] = {"ok": True, "status": 200, "reason": "", "environment": "sandbox"}
    apns.script["t2"] = {"ok": False, "status": 410, "reason": "Unregistered",
                         "environment": "prod"}
    run(route_turn(
        disp,
        feed_item("turn_queued", prompt="p", origin="chat"),
        feed_item("turn_ended", status="success", turn_index=0)))
    assert reg.devices["d1"]["env"] == "sandbox"     # auto → 实测环境粘滞
    assert "d2" not in reg.devices                   # 410 清理


def test_feed_subscription_plumbing(tmp_path):
    """dispatcher.start() 经真实 GlobalFeed 收事件（订阅路径贯通，非只测 _route）。"""
    disp, _, apns = make_dispatcher(tmp_path, [{"device_id": "d1", "token": "t1"}])

    async def scenario():
        disp.start()
        env = {"seq": 1, "turn_id": "t_a", "ts": time.time(), "type": "turn_queued",
               "data": {"prompt": "hello", "origin": "chat", "gen": {}}}
        disp.feed.publish("s-1", env)
        env2 = {"seq": 2, "turn_id": "t_a", "ts": time.time(), "type": "awaiting_approval",
                "data": {"pending_tools": [{"name": "danger_op", "arguments": {}}]}}
        disp.feed.publish("s-1", env2)
        for _ in range(50):
            if apns.sent:
                break
            await asyncio.sleep(0.01)
        await disp.stop()

    run(scenario())
    assert len(apns.by_kind("approval")) == 1


# ---------- Live Activity ----------

def test_la_reduce_plan_window_and_states(tmp_path):
    disp, _, _ = make_dispatcher(tmp_path)
    st = _ActivityState("s-1", "t_a")
    plan = [{"id": i, "title": f"步骤{i}", "status": "done" if i < 5 else
             ("doing" if i == 5 else "pending")} for i in range(8)]
    changed = disp._la_reduce(st, "plan_updated", {"task": {"plan": plan}})
    assert changed
    c = st.content
    assert c["total"] == 8 and c["done"] == 5
    assert c["stage"] == "步骤5"
    assert len(c["steps"]) == 6
    assert {"t": "步骤5", "s": "doing"} in c["steps"]        # 窗口围绕当前步
    assert disp._la_reduce(st, "tool_started", {"name": "web_research"})
    assert c["stage"] == "联网调查"
    assert disp._la_reduce(st, "awaiting_approval",
                           {"pending_tools": [{"name": "write_file"}]})
    assert c["status"] == "awaiting_approval" and "等待授权" in c["stage"]
    assert disp._la_reduce(st, "resumed", {"trigger": "approve"})
    assert c["status"] == "running"
    assert not disp._la_reduce(st, "token_delta", {"text": "x"})   # 增量不扰动内容


def test_register_activity_lifecycle(tmp_path, make_session):
    session = make_session("s-la")
    disp, _, apns = make_dispatcher(tmp_path)
    disp.session_manager = types.SimpleNamespace(_all_sessions={"s-la": session})

    async def scenario():
        # 无活跃轮：拒绝，app 据此收尾孤儿活动
        assert disp.register_activity("s-la", "t_x", "d1", "la-tok")["active"] is False
        session.journal.emit("turn_queued", {"prompt": "p", "origin": "chat"},
                             turn_id="t_x")
        session.journal.emit("turn_started", {})
        # turn_id 不匹配：拒绝
        assert disp.register_activity("s-la", "t_wrong", "d1", "la-tok")["active"] is False
        res = disp.register_activity("s-la", "t_x", "d1", "la-tok")
        assert res["active"] is True and "s-la" in disp._activities
        await asyncio.sleep(0.05)                     # 首帧补推
        first = [s for s in apns.sent if s["push_type"] == "liveactivity"]
        assert first and first[0]["payload"]["aps"]["event"] == "update"
        assert "stale-date" in first[0]["payload"]["aps"]

        # 账本事件驱动内容更新（经真实 journal 订阅）
        session.journal.emit("tool_started", {"call_id": "c1", "name": "web_research",
                                              "args": {}})
        await asyncio.sleep(0.15)
        stages = [s["payload"]["aps"]["content-state"]["stage"]
                  for s in apns.sent if s["push_type"] == "liveactivity"]
        assert "联网调查" in stages

        # turn_ended → end 推送 + 状态回收
        session.journal.emit("turn_ended", {"status": "success", "turn_index": 0})
        await asyncio.sleep(0.15)
        last = [s for s in apns.sent if s["push_type"] == "liveactivity"][-1]
        assert last["payload"]["aps"]["event"] == "end"
        assert "dismissal-date" in last["payload"]["aps"]
        assert last["payload"]["aps"]["content-state"]["status"] == "ok"
        assert "s-la" not in disp._activities
        await disp.stop()

    run(scenario())


def test_activity_push_dedup_same_content(tmp_path):
    disp, _, apns = make_dispatcher(tmp_path)
    st = _ActivityState("s-1", "t_a")
    st.tokens["d1"] = "la-tok"

    async def scenario():
        await disp._activity_push(st, priority=5)
        await disp._activity_push(st, priority=5)      # 内容没变：不重发
        assert len(apns.sent) == 1
        st.content["stage"] = "新阶段"
        await disp._activity_push(st, priority=5)
        assert len(apns.sent) == 2

    run(scenario())


# ---------- 审批 expected_turn_id 防错批（TurnRunner 全链） ----------

def test_approval_expected_turn_id_guard(make_session):
    from framework.core.turn_runner import TurnConflict
    from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
    from .test_turn_runner import make_runner, submit

    engine = FakeEngine([
        {"tool_calls": [call("danger_op", target="prod")]},
        {"text": "执行完毕。"},
    ])
    registry = FakeRegistry([FakeTool("danger_op", result="done", approval=True,
                                      read_only=False, concurrency_safe=False)])
    runner = make_runner(engine, registry)
    session = make_session()

    async def scenario():
        ctx, rd = await submit(runner, session, "t-session", prompt="危险操作")
        assert rd["status"] == "pending_approval"
        # 迟到点击：目标轮已不是它 → 明确拒绝，不错递给当前轮
        with pytest.raises(TurnConflict) as ei:
            await runner.accept_approval(session, "t-session", True,
                                         expected_turn_id="t_stale")
        assert ei.value.reason == "approval_target_mismatch"
        # 带对锚点：照常受理
        await runner.accept_approval(session, "t-session", True,
                                     expected_turn_id=ctx["turn_id"])
        rd2 = await runner.run_approval(session, "t-session", True)
        assert rd2["status"] == "success"

    run(scenario())
    assert registry.executed == [{"name": "danger_op", "args": {"target": "prod"}}]


def test_schemas_accept_optional_fields():
    from framework.api.schemas import (ApprovalRequest, LiveActivityTokenRequest,
                                       PushPrefsRequest, PushRegisterRequest)
    a = ApprovalRequest(approve=True)
    assert a.expected_turn_id is None                  # 旧客户端不带字段照常工作
    r = PushRegisterRequest(device_id="d1", token="t")
    assert r.platform == "ios" and r.prefs is None
    p = PushPrefsRequest(results=False)
    assert p.model_dump()["approval"] is None
    la = LiveActivityTokenRequest(device_id="d", session_id="s", turn_id="t", token="k")
    assert la.token == "k"
