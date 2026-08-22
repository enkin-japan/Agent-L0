"""B2 代际围栏金测试(后端确定性层)。

守三件事:① `BOOT_ID` 是进程级常量——冷会话只读实例与真账本必须报同一代际;
② 崩溃轮补发 `turn_ended`:recover 补 history 之后,补发事件要能越过重启前的
客户端游标经重放送达(「停在 generating」的收敛路径);③ `public_turn` 递归剥离。
端点面(四个 generation 下发点)在本文件直接驱动 routes 的处理函数断言(首审 H2:
只靠真机演练守端点,删字段全套照绿);真机演练另证端到端时序。
"""
import json

from framework.core.feed import GlobalFeed
from framework.core.journal import BOOT_ID, TurnJournal, new_turn_id
from framework.core.turn_runner import public_turn
from framework.memory.session import SessionManager


# ---- ① 进程代际(端点面:四个下发点逐一钉住,少一个都过不去) ----

def _mini_app(tmp_path):
    """最小装配:真 router + 真 SessionManager/GlobalFeed,鉴权未配置(放行)。"""
    from fastapi import FastAPI
    from framework.api.routes import router
    mgr = SessionManager(tmp_path / "sessions")
    feed = GlobalFeed()
    mgr.feed = feed
    app = FastAPI()
    app.include_router(router)
    app.state.session_manager = mgr
    app.state.feed = feed
    app.state.api_key = ""
    app.state.auth_devices = {}
    return app, mgr


def _sse_first_data(resp_coro):
    """驱动 SSE 端点协程,取首帧 data 后立即收流(不过网络栈——TestClient 收半开
    SSE 会卡在 15s 心跳等待上,这里直接消费 body_iterator)。"""
    import asyncio

    async def run():
        resp = await resp_coro
        gen = resp.body_iterator
        try:
            chunk = await gen.__anext__()
        finally:
            await gen.aclose()
        data_lines = [l[5:].strip() for l in str(chunk).splitlines()
                      if l.startswith("data:")]
        return json.loads("\n".join(data_lines))

    return asyncio.run(run())


def test_all_four_generation_emission_sites_agree(tmp_path):
    """★ 四个下发点(/turn 两个 return + 两条 SSE 首帧)都带 generation 且同值同源
    (首审 H2:此前删掉任何一处,全套金测试照绿——只有真机演练看得见)。"""
    import asyncio
    from types import SimpleNamespace
    from framework.api import routes
    _app, mgr = _mini_app(tmp_path)
    sdir = tmp_path / "sessions" / "g-1"
    sdir.mkdir(parents=True)
    (sdir / "history.jsonl").write_text(
        json.dumps({"prompt": "p", "status": "success", "response": []}) + "\n",
        encoding="utf-8")
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_manager=mgr, feed=mgr.feed, scheduler=None)))

    cold = asyncio.run(routes.get_turn_state("g-1", req))           # 冷会话只读实例
    missing = asyncio.run(routes.get_turn_state("no-such", req))    # 目录不存在分支
    sse_session = _sse_first_data(routes.attach_events("g-1", req, after=0))
    sse_feed = _sse_first_data(routes.attach_global_feed(req, after=0))
    values = {cold.get("generation"), missing.get("generation"),
              sse_session.get("data", {}).get("generation"),
              sse_feed.get("data", {}).get("generation")}
    assert values == {BOOT_ID}                  # 四处都在,且是同一个值
    assert sse_feed["data"]["last_feed_seq"] == 0  # 新进程 feed 从零起(重锚语义的前提)


def test_boot_id_varies_per_process(tmp_path):
    """代际是「进程」代际:两个进程各自 import,必须得到不同值(同进程内恒定)。"""
    import subprocess, sys
    cmd = [sys.executable, "-c",
           "from framework.core.journal import BOOT_ID; print(BOOT_ID)"]
    outs = [subprocess.run(cmd, capture_output=True, text=True, cwd=".",
                           check=True).stdout.strip() for _ in range(2)]
    assert all(len(o) == 16 for o in outs)
    assert outs[0] != outs[1]
    assert BOOT_ID not in outs                  # 与本进程也不同


# ---- ② 崩溃轮补发 turn_ended ----

def _crashed_session(tmp_path, sid: str):
    """布一个「进程死在生成中」的现场:两轮已落盘 + inflight 残留 + 客户端游标。"""
    sdir = tmp_path / "sessions" / sid
    sdir.mkdir(parents=True)
    j = TurnJournal(sdir)
    tid = new_turn_id()
    j.emit("turn_queued", {"prompt": "长任务", "gen": {}, "origin": "chat"}, turn_id=tid)
    j.emit("turn_started", {})
    for _ in range(120):
        j.emit("token_delta", {"text": "字"})       # 占号不落盘——游标落在这些号上
    cursor = j.seq                                   # 客户端已消费到的最大 seq
    (sdir / "history.jsonl").write_text(
        json.dumps({"prompt": "旧轮", "status": "success", "response": []}) + "\n",
        encoding="utf-8")
    (sdir / "inflight.json").write_text(
        json.dumps({"prompt": "长任务", "gen": {}, "turn_id": tid}), encoding="utf-8")
    return sdir, tid, cursor


def test_backfilled_turn_ended_clears_the_stale_cursor(tmp_path):
    """★ 要害:补发的 turn_ended 必须 > 重启前游标,重附着重放即送达。
    没有它,recover 只写 history,纯事件流消费者永远停在 generating(基线演练已实证)。"""
    sdir, tid, cursor = _crashed_session(tmp_path, "fence-1")
    mgr = SessionManager(tmp_path / "sessions")
    mgr.feed = GlobalFeed()

    recovered = mgr.recover_inflight_turns()          # 进程重启第一步(工作线程域)
    assert [r["turn_id"] for r in recovered] == [tid]
    assert recovered[0]["turn_index"] == 1            # fail 轮追加在旧轮之后
    mgr.emit_recovery_events(recovered)               # 装配阶段(循环线程域)

    j2 = TurnJournal(sdir)                            # 重附着走的账本视角
    delivered = [e for e in j2.replay(after=cursor) if e["type"] == "turn_ended"]
    assert len(delivered) == 1
    assert delivered[0]["seq"] > cursor
    assert delivered[0]["turn_id"] == tid
    assert delivered[0]["data"]["status"] == "fail"


def test_backfill_mirrors_into_the_global_feed(tmp_path):
    """通知路径:feed 无持久层,重启后环里只有新代事件——补发事件要在环里,
    客户端按新代际 after=0 重锚时才补得到「后台轮次死了」这一声。"""
    _sdir, tid, _cursor = _crashed_session(tmp_path, "fence-2")
    mgr = SessionManager(tmp_path / "sessions")
    feed = GlobalFeed()
    mgr.feed = feed

    mgr.emit_recovery_events(mgr.recover_inflight_turns())
    ended = [it for it in feed.replay(0) if it["type"] == "turn_ended"]
    assert len(ended) == 1
    assert ended[0]["turn_id"] == tid
    assert ended[0]["data"]["status"] == "fail"
    # prompt 随 feed 既有瘦身形状带上(与正常 turn_ended 镜像一致);今日消费方
    # (dispatcher 取自身 meta、Swift 忽略 data.prompt)不读它——断言的是形状,不是消费
    assert ended[0]["data"]["prompt"] == "长任务"


def test_recovery_without_marker_emits_nothing(tmp_path):
    """阳性对照的反面:无 inflight 残留 → 不补发任何事件(不许无中生有 turn_ended)。"""
    sdir = tmp_path / "sessions" / "fence-3"
    sdir.mkdir(parents=True)
    (sdir / "history.jsonl").write_text(
        json.dumps({"prompt": "旧轮", "status": "success", "response": []}) + "\n",
        encoding="utf-8")
    mgr = SessionManager(tmp_path / "sessions")
    feed = GlobalFeed()
    mgr.feed = feed
    recovered = mgr.recover_inflight_turns()
    assert recovered == []
    mgr.emit_recovery_events(recovered)
    assert feed.replay(0) == []
    assert not (sdir / "journal.jsonl").exists()


def test_legacy_marker_without_turn_id_still_backfills(tmp_path):
    """旧 inflight 标记没有 turn_id 键:补发照做,turn_id 为 null(会话级收敛语义)。"""
    sdir = tmp_path / "sessions" / "fence-4"
    sdir.mkdir(parents=True)
    (sdir / "inflight.json").write_text(
        json.dumps({"prompt": "旧格式", "gen": {}}), encoding="utf-8")
    mgr = SessionManager(tmp_path / "sessions")
    mgr.feed = GlobalFeed()
    recovered = mgr.recover_inflight_turns()
    mgr.emit_recovery_events(recovered)
    ended = [e for e in TurnJournal(sdir).replay(0) if e["type"] == "turn_ended"]
    assert len(ended) == 1 and ended[0]["turn_id"] is None


def test_inflight_carries_turn_id(make_session):
    """write_inflight 的新参数真的落盘——没有它,补发永远指认不了正主。"""
    session = make_session("fence-5")
    session.write_inflight("p", {"max_tokens": 4}, "t_abc123")
    data = json.loads(session.inflight_path.read_text(encoding="utf-8"))
    assert data["turn_id"] == "t_abc123"
    # 兼容:不传仍可写(定时轮/交互轮两个调用点都已传,这是防御面)
    session.write_inflight("p", {})
    assert json.loads(session.inflight_path.read_text(encoding="utf-8"))["turn_id"] is None


# ---- ③ public_turn 递归剥离 ----

def test_public_turn_strips_nested_internal_keys():
    turn = {
        "prompt": "p", "_rollback_idx": 3,
        "response": [
            {"role": "tool", "tool_responses": [
                {"name": "t", "response": "ok", "_safe": True,
                 "_target": {"path": "/x"}},
            ]},
            {"role": "assistant", "content": "答", "_draft": "x"},
        ],
        "meta": {"nested": {"_hidden": 1, "kept": 2}, "arr": [{"_a": 1, "b": 2}]},
    }
    out = public_turn(turn)
    assert "_rollback_idx" not in out
    tr = out["response"][0]["tool_responses"][0]
    assert tr == {"name": "t", "response": "ok"}
    assert "_draft" not in out["response"][1]
    assert out["meta"] == {"nested": {"kept": 2}, "arr": [{"b": 2}]}
    # 原 dict 不被改动(投影是新建的整棵树,内存态/KV 前缀不受影响)
    assert turn["response"][0]["tool_responses"][0]["_safe"] is True
    assert turn["_rollback_idx"] == 3


def test_backfill_floors_seq_when_journal_is_missing(tmp_path):
    """L15:journal.jsonl 缺失(被删/首次落盘就崩)时临时账本从 0 起,补发 seq=1 越不过
    任何保留游标——必须垫底到水位余量。"""
    from framework.core.journal import SEQ_RESTART_RESERVE
    sdir = tmp_path / "sessions" / "fence-6"
    sdir.mkdir(parents=True)
    (sdir / "inflight.json").write_text(
        json.dumps({"prompt": "p", "gen": {}, "turn_id": "t_x"}), encoding="utf-8")
    # 刻意不建 journal.jsonl
    mgr = SessionManager(tmp_path / "sessions")
    mgr.feed = GlobalFeed()
    mgr.emit_recovery_events(mgr.recover_inflight_turns())
    ended = [e for e in TurnJournal(sdir).replay(0) if e["type"] == "turn_ended"]
    assert len(ended) == 1
    assert ended[0]["seq"] > SEQ_RESTART_RESERVE
