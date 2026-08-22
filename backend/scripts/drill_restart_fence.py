"""B2 代际围栏真机演练：轮次执行中 devctl restart，验证客户端视角能否收敛。

两种期望（--expect），同一脚本先后跑两遍：
- --expect defect：围栏落地**前**的基线。期望复现缺陷——重启后重附着，崩溃轮的
  `turn_ended` 永远不来（recover_inflight_turns 只补写 history、不发账本事件），
  纯事件流消费者停在 generating。**基线不红，围栏落地后那声「通过」什么也不证明。**
- --expect fixed：围栏落地**后**。期望：重附着首帧 `stream_attached.generation` 与
  重启前不同（客户端据此重锚），且补发的 `turn_ended`（status=fail）经重放到达。

只读会话数据面之外不碰任何状态；重启动作 = `./devctl.sh restart backend`（等价
stop+start，就是要演练的那个日常动作）。鉴权读仓库根 .env.local 的 AGENT_API_KEY
（不打印）。退出码：0=观察与期望一致；3=不一致；1=脚本/环境错误。
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:14159"


def _load_key() -> str:
    # 与 devctl 同序:.env 先、.env.local 后加载**覆盖**(不是首中即返——那会让
    # 陈旧的根 .env 遮蔽 .env.local,首审 L17)
    key = ""
    for envfile in (ROOT / ".env", ROOT / ".env.local"):
        if not envfile.exists():
            continue
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


KEY = _load_key()


def _req(path: str, data=None, headers=None, method=None):
    h = {"Authorization": f"Bearer {KEY}"} if KEY else {}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    return urllib.request.urlopen(req, timeout=30)


def wait_port(timeout_s: float = 120.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            _req("/api/v1/tools").read()
            return True
        except urllib.error.HTTPError as e:
            # 4xx = 服务在而鉴权/请求不对——别把 401 报成「后端没起」(首审 L17)。
            # 放行继续跑,让后续请求以明确的 HTTP 错误失败。
            print(f"[drill] ⚠️ 探活收到 HTTP {e.code}(服务在;若为 401 请核对 AGENT_API_KEY)",
                  file=sys.stderr)
            return True
        except Exception:
            time.sleep(1.0)
    return False


class SSECollector(threading.Thread):
    """附着一条 SSE 流（会话事件或全局 feed），逐事件收集；首帧另存。"""

    def __init__(self, path: str, after: int, *, seq_key: str = "seq"):
        super().__init__(daemon=True)
        self.path, self.after, self.seq_key = path, after, seq_key
        self.events, self.attached = [], None
        self.error = None
        self._stop = threading.Event()

    def run(self):
        try:
            resp = _req(f"{self.path}?after={self.after}")
            buf_type, buf_data = None, []
            while not self._stop.is_set():
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    buf_type = line[6:].strip()
                elif line.startswith("data:"):
                    buf_data.append(line[5:].strip())
                elif line == "" and buf_type:
                    try:
                        env = json.loads("\n".join(buf_data) or "{}")
                    except Exception:
                        env = {}
                    if buf_type == "stream_attached" and self.attached is None:
                        self.attached = env
                    self.events.append({"seq": env.get(self.seq_key, 0), "type": buf_type,
                                        "data": env.get("data", {}), "env": env})
                    buf_type, buf_data = None, []
        except Exception as e:
            self.error = e

    def stop(self):
        self._stop.set()

    def max_seq(self) -> int:
        return max((e["seq"] for e in self.events if isinstance(e["seq"], int)), default=self.after)

    def count(self, etype: str) -> int:
        return sum(1 for e in self.events if e["type"] == etype)

    def wait_for(self, pred, timeout_s: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if pred(self):
                return True
            if self.error is not None:
                return False
            time.sleep(0.3)
        return pred(self)


def submit_turn(sid: str, prompt: str, max_tokens: int = 2048) -> dict:
    import urllib.parse
    body = urllib.parse.urlencode({
        "prompt": prompt, "max_tokens": str(max_tokens),
        "temperature": "0.2", "min_p": "0.05",
    }).encode("utf-8")
    with _req(f"/api/v1/chat/{sid}/turns", data=body,
              headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
        return json.loads(r.read())


def turn_state(sid: str) -> dict:
    with _req(f"/api/v1/chat/{sid}/turn") as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", choices=["defect", "fixed"], required=True)
    ap.add_argument("--min-deltas", type=int, default=15,
                    help="重启前须观察到的 token_delta 数（证明真在生成）")
    ap.add_argument("--verdict-wait", type=float, default=45.0,
                    help="重附着后等 turn_ended 的窗口秒数")
    args = ap.parse_args()

    if not wait_port(10):
        print("[drill] 后端未在 :14159 监听——先 ./devctl.sh start backend", file=sys.stderr)
        return 1

    sid = f"drill-fence-{int(time.time())}"
    obs = {}

    # 全局 feed 观察者(--expect fixed 的围栏主角):重启前认领代际与 feed 游标
    f1 = SSECollector("/api/v1/events", after=0, seq_key="feed_seq")
    f1.start()
    f1.wait_for(lambda c: c.attached is not None, 15)
    feed_gen_before = (f1.attached or {}).get("data", {}).get("generation")
    obs["feed_generation_before"] = feed_gen_before

    # 1. 附着 → 提交长轮次 → 等真生成
    c1 = SSECollector(f"/api/v1/chat/{sid}/events", after=0)
    # 会话懒创建：先发一轮空跑？——直接提交轮次再附着（404 窗口）更稳
    r = submit_turn(sid, "请从 1 数到 300，每个数字单独一行，不要解释。")
    print(f"[drill] 已受理 turn_id={r['turn_id']} seq={r['seq']}")
    c1.start()
    def _active(c):
        # token 与 thinking 增量都算「真在生成」:崩溃砸在思考期与正文期,对恢复语义
        # 等价(inflight 标记都在);e4b 在 0.2 温度下偶尔先走工具路,只认 token_delta
        # 会在那些轮空等
        return c.count("token_delta") + c.count("thinking_delta") >= args.min_deltas
    if not c1.wait_for(_active, 90):
        print(f"[drill] 90s 内增量不足 {args.min_deltas}"
              f"（token={c1.count('token_delta')} thinking={c1.count('thinking_delta')}；"
              f"err={c1.error}）", file=sys.stderr)
        return 1
    gen_before = (c1.attached or {}).get("data", {}).get("generation")
    obs["pre_restart_deltas"] = c1.count("token_delta") + c1.count("thinking_delta")
    obs["generation_before"] = gen_before
    cursor = c1.max_seq()
    print(f"[drill] 生成确认（{obs['pre_restart_deltas']} deltas），"
          f"游标 seq={cursor}，generation={gen_before!r}")

    # 2. 轮次执行中重启
    t0 = time.time()
    subprocess.run(["./devctl.sh", "restart", "backend"], cwd=ROOT,
                   check=True, capture_output=True, timeout=300)
    if not wait_port(120):
        print("[drill] 重启后 :14159 未回来", file=sys.stderr)
        return 1
    print(f"[drill] restart 完成（{time.time()-t0:.1f}s）")
    c1.stop()

    # 3. 按 §2.4 契约用已消费的最大 seq 重附着
    c2 = SSECollector(f"/api/v1/chat/{sid}/events", after=cursor)
    c2.start()
    if not c2.wait_for(lambda c: c.attached is not None, 30):
        print(f"[drill] 重附着无首帧（err={c2.error}）", file=sys.stderr)
        return 1
    gen_after = c2.attached.get("data", {}).get("generation")
    obs["generation_after"] = gen_after
    obs["generation_changed"] = (gen_before is not None and gen_after is not None
                                 and gen_before != gen_after)

    got_end = c2.wait_for(lambda c: c.count("turn_ended") > 0, args.verdict_wait)
    obs["turn_ended_after_restart"] = got_end
    ts = turn_state(sid)
    obs["turn_state_after"] = (ts.get("turn") or {}).get("state") if ts.get("turn") else None
    obs["turn_generation_field"] = ts.get("generation")
    c2.stop()

    # 3b. feed 围栏(仅 fixed 判定用,defect 模式只记录):模拟带围栏的客户端——
    # 用陈旧游标重附着,首帧代际变了 → 游标作废,清零重连,断言补发的 turn_ended
    # feed 条目在新代环里(通知路径的端到端)
    f1.stop()
    stale_feed_cursor = f1.max_seq()
    f2 = SSECollector("/api/v1/events", after=stale_feed_cursor, seq_key="feed_seq")
    f2.start()
    f2.wait_for(lambda c: c.attached is not None, 15)
    feed_gen_after = (f2.attached or {}).get("data", {}).get("generation")
    obs["feed_generation_changed"] = (feed_gen_before is not None
                                      and feed_gen_after is not None
                                      and feed_gen_before != feed_gen_after)
    f2.stop()
    f3 = SSECollector("/api/v1/events", after=0, seq_key="feed_seq")   # 围栏:清零重锚
    f3.start()
    obs["feed_backfill_delivered"] = f3.wait_for(
        lambda c: any(e["type"] == "turn_ended"
                      and e["env"].get("session_id") == sid
                      and e["data"].get("status") == "fail" for e in c.events), 20)
    f3.stop()

    # 4. 交叉核对：崩溃轮确实以 fail 落档（两种期望下都应成立）
    with _req(f"/api/v1/chat/{sid}/history") as r:
        hist = json.loads(r.read())
    turns = hist.get("turns") or hist.get("history") or []
    last_status = (turns[-1] or {}).get("status") if turns else None
    obs["last_history_status"] = last_status

    print(f"[drill] 观察: {json.dumps(obs, ensure_ascii=False)}")

    if args.expect == "defect":
        ok = (not obs["turn_ended_after_restart"]) and last_status == "fail"
        print("[drill] 基线判定:", "缺陷复现（turn_ended 永远没来）✅" if ok
              else "未复现——演练本身看不见缺陷，围栏验收无效 ❌")
    else:
        ok = (obs["turn_ended_after_restart"] and obs["generation_changed"]
              and last_status == "fail" and obs["turn_generation_field"] is not None
              and obs["feed_generation_changed"] and obs["feed_backfill_delivered"])
        print("[drill] 修后判定:", "会话流(代际+补发) + feed(代际变化+重锚后补发条目)均到达 ✅"
              if ok else "围栏未生效 ❌")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
