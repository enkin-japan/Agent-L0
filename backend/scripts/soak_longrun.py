"""本机长稳测试工装（26b-4bit 12h 门禁，2026-08-18）。

**为什么是重写**：2026-08-08 那次 26B 12h 长稳的工装只活在 Colab VM 上、从未入库，
随实例销毁（全分支 git 历史与 Drive 双向搜索均无）。台账只留下结论与负载形态描述。
本文件按那份描述在本机 MLX 栈上重建并入库——下次不必再写第三遍。

判据沿用上次已验证的三条（台账「26B 12h 长稳测试站 PASS」）：
  无泄漏：进程 RSS / peak_memory / 系统 wired 与 compressed 起步一档上抬后走平；
          收官在册缓存数回到预期、active_generations 归零；
  无漂移：把全程切三段比 tps / TTFT / 时长——**单调劣化才是漂移，方差不是**；
  无错误：errors_total 增量为零、健康探针全绿、取消双路清理干净。

**本工装的第一原则：宁可不测，也不假装测到。**
初版（首过评审 6 Critical）的教训全在这里——工具腿没渲染工具声明，约束采样一步没跑
却报「工具轮全过」；`constrained_steps` 只在 done 事件里而客户端丢弃了 done；RSS 用
`lsof -ti :port` 取 PID 会抓到工装自己，「无泄漏」建立在一条根本不是服务端的平线上。
故凡是判据依赖的量，这里都**要么测到并断言、要么响亮记为失效**，不留静默空转。

与 Colab 那次的口径差异（换平台必须换的）：Apple Silicon 没有 VRAM，统一内存的等价
观测量是 peak_memory（MLX 进程高水位）+ 进程 RSS + 系统 wired + **compressor 页数与
swapouts**——后者才是 MLX 超售换页崩塌的先兆（单 GPU 纪律里那条 page-thrash cliff）。

用法（backend venv，服务须已就绪）：
  cd backend && PYTHONUNBUFFERED=1 .venv/bin/python scripts/soak_longrun.py \
      --hours 12 --outdir ../benchmarks/data/soak-26b4bit-<日期>
密钥只走 `INFER_API_KEY` 环境变量或 `.env.local`——**刻意不提供 --api-key**，命令行参数
会把钥匙暴露在 `ps` 输出里长达 12 小时。

退出码是**运行态**信号，不是质量判决：0=跑完且无错无泄漏 / 1=有错误或缓存泄漏 /
2=连续失败熔断提前中止。漂移的 PASS/FAIL 要人读 summary 的三分段表（见上「无漂移」）。
"""
import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_baseline import (BACKEND_DIR, Client, build_user_prompt,  # noqa: E402
                            make_dialect, model_options, read_env_key)
from framework.core import prompt_stream as pstream  # noqa: E402

REPO = "mlx-community/gemma-4-26b-a4b-it-4bit"

# 负载权重按上次 313 请求的实际构成归一（chat 220 / tool 65 / longchain 19 / cancel 9），
# 保持同一口径便于跨次比对。
MIX = [("chat", 0.70), ("tool", 0.21), ("longchain", 0.06), ("cancel", 0.03)]

IDLE_SHORT = (20, 90)            # 秒
IDLE_LONG = (15 * 60, 45 * 60)   # 「离开」窗：磨 TTL 回收与冷启动重建
LONG_IDLE_EVERY = 40
SAMPLE_INTERVAL = 300.0          # sys.jsonl 采样间隔
HEARTBEAT_INTERVAL = 300.0       # 心跳按**墙钟**——按请求数计会在长空闲窗里静默一小时，
                                 # 把 3 分钟级的 task-sentinel 骗成停滞告警
MAX_CONSEC_FAIL = 5              # 连续失败熔断：服务死了不该空转到 12h
CHAT_TURNS = 5                   # 一段会话的轮数（多轮才吃得到前缀复用）

TOPICS = ["周末露营装备", "阳台种菜浇水", "旧笔记本装 Linux", "咖啡手冲参数",
          "地铁通勤路线", "冰箱除霜", "自行车链条保养", "阳台防水补漆"]
FOLLOWUPS = ["那预算大概多少", "有没有更省事的做法", "第一步先做什么",
             "冬天需要注意什么吗", "这个能自己动手吗"]

TOOL_SPEC = {
    "name": "lookup_note",
    "description": "Look up a personal note by keyword.",
    "parameters": {"type": "object",
                   "properties": {"keyword": {"type": "string"},
                                  "limit": {"type": "integer"}},
                   "required": ["keyword"]},
}


def _page_size() -> int:
    """从 vm_stat 首行读页大小，不硬编码 16384。"""
    try:
        head = subprocess.run(["vm_stat"], capture_output=True, text=True,
                              timeout=10).stdout.splitlines()[0]
        for tok in head.replace(",", " ").split():
            if tok.isdigit() and int(tok) >= 4096:
                return int(tok)
    except Exception:
        pass
    return 16384


def _vm_stat(page: int) -> dict:
    """系统内存态：wired / compressor / swap。这是本机版的「VRAM 曲线」。"""
    out = {}
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10)
        for line in (r.stdout or "").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            v = v.strip().rstrip(".")
            if not v.isdigit():
                continue
            k = k.strip()
            if k == "Pages wired down":
                out["wired_mb"] = int(v) * page // (1 << 20)
            elif k == "Pages occupied by compressor":
                out["compressed_mb"] = int(v) * page // (1 << 20)
            elif k == "Swapins":
                out["swapins"] = int(v)
            elif k == "Swapouts":
                out["swapouts"] = int(v)
    except Exception as e:
        out["vm_stat_error"] = str(e)[:80]
    return out


def _listener_pid(port: int) -> "str | None":
    """服务端 PID。**必须带 -sTCP:LISTEN**：裸 `lsof -ti :PORT` 同时匹配本地与远端端口，
    工装自己那条长连接照样命中，而 pids[0] 取的是枚举顺序不是监听者——实测两进程探针
    `lsof -ti :P -> [服务端, 客户端]`。取错就会采到工装自己的 RSS，得到一条完美平线。"""
    try:
        pids = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=10).stdout.split()
        return pids[0] if pids else None
    except Exception:
        return None


def _rss_mb(pid: "str | None") -> "int | None":
    if not pid:
        return None
    try:
        r = subprocess.run(["ps", "-o", "rss=", "-p", pid],
                           capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip()) // 1024
    except Exception:
        return None


class Soak:
    def __init__(self, args, dialect):
        self.args = args
        self.dialect = dialect
        self.client = Client(args.port, args.api_key)
        self.out = Path(args.outdir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.req_f = (self.out / "req.jsonl").open("a", encoding="utf-8")
        self.sys_f = (self.out / "sys.jsonl").open("a", encoding="utf-8")
        self.rng = random.Random(args.seed)
        self.page = _page_size()
        self.t0 = time.time()
        self.deadline = self.t0 + args.hours * 3600
        self.n = 0
        self.errors = 0
        self.consec_fail = 0
        self.samples = 0
        self.sampler_died = None
        self.del_attempt = 0
        self.del_ok = 0
        self.ttl_pending = 0          # 交给 TTL 的缓存数（收官判泄漏要减掉它）
        self.stop = threading.Event()
        self.sampler_thread = None
        self.base_metrics = {}

    # ---------- 落盘（逐行 flush：进程被杀只丢最后一行）----------
    def _write(self, f, rec: dict) -> None:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()

    def _req(self, **kw) -> None:
        self.n += 1
        self._write(self.req_f, {"i": self.n, "t": round(time.time() - self.t0, 2), **kw})
        if kw.get("ok", True):
            self.consec_fail = 0
        else:
            self.errors += 1
            self.consec_fail += 1

    # ---------- 系统采样线程 ----------
    def _sample_once(self) -> dict:
        pid = _listener_pid(self.args.port)
        rec = {"t": round(time.time() - self.t0, 1), **_vm_stat(self.page),
               "server_pid": pid, "server_rss_mb": _rss_mb(pid)}
        t = time.time()
        try:
            self.client.json_call("GET", "/health", timeout=10)
            rec["health_ms"] = round((time.time() - t) * 1000, 1)
        except Exception as e:
            rec["health_error"] = str(e)[:120]
        try:
            rec["metrics"] = self.client.json_call("GET", "/metrics", timeout=10)
        except Exception as e:
            rec["metrics_error"] = str(e)[:120]
        return rec

    def sampler(self) -> None:
        # 整个循环体套 try：采样线程静默死亡 = 内存曲线断掉 = 「无泄漏」这条结论没了，
        # 而 summary 照样会拿前两小时的数据算出三段来，看起来毫无异常。
        while not self.stop.wait(SAMPLE_INTERVAL):
            try:
                self._write(self.sys_f, self._sample_once())
                self.samples += 1
            except Exception as e:
                self.sampler_died = f"{type(e).__name__}: {str(e)[:120]}"
                try:
                    self._write(self.sys_f, {"t": round(time.time() - self.t0, 1),
                                             "sampler_error": self.sampler_died})
                except Exception:
                    pass       # 连错误都写不下去（盘满）：留给 finish() 的行数比对暴露

    # ---------- 四类负载 ----------
    def _chat_turn(self, sess, history):
        """短聊多轮：**镜像引擎的流式组装**（上位解口径）——turn1 全量渲染，turn2+ 走
        「记录流 + 方言增量段」+ token 级增量喂入。

        绝不能用「每轮重渲染整段历史」：实测那样得到的 turn2 prompt **不以 turn1 的
        prompt+生成为前缀**（env 锚点块每轮后移、assistant 回放带思考频道前缀，正是
        上位解修掉的两个破坏者），前缀复用一次都不会命中——chat 腿会静默退化成
        「每轮冷预填」，而 KV 增长与复用这两条恰恰是长稳最该盯的。
        与 bench_a8_multiturn v3 同源。
        """
        history.append({"role": "user",
                        "content": self.rng.choice(FOLLOWUPS) if history
                        else f"帮我想想{self.rng.choice(TOPICS)}要准备什么"})
        ps, cache_id = sess["ps"], sess["cache_id"]
        used_delta, tail = False, None
        if ps is not None and pstream.stream_valid(ps, history, None):
            d_out = self.dialect.build_prompt_delta(history[ps.seen + 1:], prev_tail=ps.tail)
            if d_out is not None:
                prompt, tail = ps.stream + d_out[0], d_out[1]
                used_delta = True
        if not used_delta:
            prompt = self.dialect.build_prompt(messages=history, tools=None,
                                               include_env=False)
            tail = self.dialect.tail_state()
        done, raw = {}, {}
        last, text, ttft, cid, wall = self.client.generate(
            prompt, cache_id=cache_id, sampling={"max_tokens": 160, "temperature": 0.7},
            features=({"prompt_delta_offset": len(ps.stream)} if used_delta else None),
            done_out=done, raw_out=raw)
        # 次序不可颠倒：commit 记下 seen=len(history)，并**预期下一轮 history[seen]
        # 正是这条 assistant**——先 append 再 commit 会让 seen 多算一位，
        # stream_valid 从此恒假、增量路永不生效（chat 腿静默退化成每轮冷预填）。
        # 提交用**原始字节口径**（raw_out）：展示 delta 与引擎生成字节不逐字等价。
        sess["ps"] = pstream.commit(prompt, raw.get("text", text), history, None, tail)
        history.append({"role": "assistant", "content": text})
        sess["cache_id"] = cid
        gen_tok = (last or {}).get("generation_tokens") or 0
        turn = len(history) // 2
        reused = (last or {}).get("extra", {}).get("reused_prefix")
        # gen_tok<=2 视为失败：§S.14 那类「同 cache 复用后首 token 即 EOT」的回归就长这样，
        # 只判 bool(text) 会让它伪装成一次「tps 偏低」的正常轮次
        ok = bool(text) and gen_tok > 2
        return dict(kind="chat", ok=ok, turn=turn, ttft=ttft, wall=round(wall, 3),
                    gen_tok=gen_tok, tps=(last or {}).get("generation_tps"),
                    prompt_tok=(last or {}).get("prompt_tokens"),
                    peak_mem=(last or {}).get("peak_memory"),
                    reused=reused, cache_rotated=bool(cache_id and cid != cache_id),
                    error=None if ok else f"生成疑似秒停 gen_tok={gen_tok}"), cid

    def _tool_turn(self):
        """工具轮：prompt 必须**带工具声明段**（方言渲染），否则模型根本不知道有工具，
        guard 建起来也永不激活——初版正是这样让 21% 的负载退化成普通短聊。"""
        prompt = build_user_prompt(
            self.dialect,
            f"请立即调用 lookup_note 工具，查询关键词「{self.rng.choice(TOPICS)}」的笔记。",
            tools=[TOOL_SPEC])
        done = {}
        last, text, ttft, cid, wall = self.client.generate(
            prompt, sampling={"max_tokens": 120, "temperature": 0.3},
            features={"tool_specs": [TOOL_SPEC]}, done_out=done)
        steps = done.get("constrained_steps")
        called = TOOL_SPEC["name"] in (text or "")
        # 约束一步没跑 = 这条腿没在测它该测的东西，记为失败而不是静默放过
        ok = bool(text) and steps is not None and steps > 0
        return dict(kind="tool", ok=ok, ttft=ttft, wall=round(wall, 3),
                    tps=(last or {}).get("generation_tps"),
                    peak_mem=(last or {}).get("peak_memory"),
                    constrained_steps=steps, called=called,
                    error=None if ok else f"约束未生效 constrained_steps={steps}"), cid

    def _longchain_turn(self):
        """长链：长预填 + 768 生成，打预填延迟与大 KV 的分配/回收。"""
        filler = "。".join(f"第{i}条是关于{self.rng.choice(TOPICS)}的记录，"
                          f"要点是准备{self.rng.choice(TOPICS)}相关的物品与时间安排"
                          for i in range(220))
        prompt = build_user_prompt(self.dialect, f"{filler}。请据此总结要点。")
        last, text, ttft, cid, wall = self.client.generate(
            prompt, sampling={"max_tokens": 768, "temperature": 0.6})
        return dict(kind="longchain", ok=bool(text), ttft=ttft, wall=round(wall, 3),
                    prompt_tok=(last or {}).get("prompt_tokens"),
                    gen_tok=(last or {}).get("generation_tokens"),
                    peak_mem=(last or {}).get("peak_memory"),
                    tps=(last or {}).get("generation_tps")), cid

    def _stream_partial(self, prompt: str, n_chunks: int):
        """开一条 /v1/generate 流，读到第 n_chunks 个 chunk 就**停在半路**，把
        (连接, gen_id, cache_id, 已读 chunk 数, 是否已自然结束) 交出去。

        为什么不复用 Client.generate：它会把整条流读完。取消腿要的恰恰是「不读完」——
        用它写出来的取消测试会静默退化成一次普通长生成。
        """
        body = {"prompt": prompt, "sampling": {"max_tokens": 900, "temperature": 0.7},
                "features": {"want_delta": True}}
        c = self.client._conn(120.0)
        try:
            c.request("POST", "/v1/generate", body=json.dumps(body),
                      headers=self.client.headers)
            r = c.getresponse()
            if r.status >= 300:
                raise RuntimeError(f"/v1/generate -> {r.status}")
            gen_id = cache_id = None
            seen, event, finished = 0, "", False
            while seen < n_chunks:
                line = r.readline()
                if not line:
                    finished = True
                    break
                if line.startswith(b"event:"):
                    event = line.split(b":", 1)[1].strip().decode()
                elif line.startswith(b"data:"):
                    payload = json.loads(line.split(b":", 1)[1].strip())
                    kind, event = event, ""
                    if kind == "meta":
                        gen_id = payload.get("gen_id") or gen_id
                        cache_id = payload.get("cache_id") or cache_id
                    elif kind == "chunk":
                        seen += 1
                    elif kind in ("done", "error"):
                        finished = True
                        break
            return c, gen_id, cache_id, seen, finished
        except Exception:
            c.close()          # 异常路径也必须关：否则服务端那次生成会跑满 900 token
            raise              # 并一直占着 gen_sem，而主循环已经走下一个请求了

    def _settle(self, baseline: int, timeout: float = 30.0):
        """等 active_generations 回落到基线。取消是否**真的清理干净**只有这里能证明——
        端点自己的返回值只说明「收到了取消」，不说明信号量释放了。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                m = self.client.json_call("GET", "/metrics", timeout=10)
                if (m.get("active_generations") or 0) <= baseline:
                    return round(time.time() - t0, 2), True
            except Exception:
                pass
            time.sleep(1.0)
        return round(time.time() - t0, 2), False

    def _cancel_turn(self):
        """流式中途取消：取消端点与纯断连五五开——两条清理路径都要磨，且都要证明落停。"""
        via = "endpoint" if self.rng.random() < 0.5 else "disconnect"
        prompt = build_user_prompt(self.dialect,
                                   f"详细讲讲{self.rng.choice(TOPICS)}的完整流程。")
        try:
            base = (self.client.json_call("GET", "/metrics", timeout=10)
                    .get("active_generations") or 0)
        except Exception:
            base = 0
        conn, gen_id, cid, seen, finished = self._stream_partial(prompt, n_chunks=8)
        try:
            if finished:      # 8 个 chunk 之前就自然收尾：这次没测到取消，如实标注不算错
                return dict(kind="cancel", via=via, ok=True, skipped="生成先自然结束",
                            chunks_before=seen), cid
            if via == "endpoint":
                if not gen_id:
                    return dict(kind="cancel", via=via, ok=False,
                                error="meta 事件没给 gen_id，取消端点无从寻址"), cid
                r = self.client.json_call("POST", f"/v1/generate/{gen_id}/cancel", timeout=15)
                cancelled = bool(r.get("cancelled"))
            else:
                cancelled = None
        finally:
            conn.close()      # 两条腿都在这里断连；endpoint 腿是「先取消再断」
        settle_s, settled = self._settle(base)
        return dict(kind="cancel", via=via, ok=settled, chunks_before=seen,
                    cancelled=cancelled, settle_s=settle_s,
                    error=None if settled else f"active_generations {settle_s}s 未回落"), cid

    # ---------- 缓存生命周期（删除 70% / TTL 30% 双路磨）----------
    def _retire_cache(self, cache_id) -> None:
        if not cache_id:
            return
        if self.rng.random() >= 0.7:
            self.ttl_pending += 1      # 交给 TTL：收官判泄漏时要把它们减掉
            return
        self.del_attempt += 1
        try:
            r = self.client.json_call("DELETE", f"/v1/cache/{cache_id}", timeout=15)
            if r.get("released"):
                self.del_ok += 1
        except Exception:
            pass                        # 计数差值会在 summary 里暴露删除腿整体失效

    # ---------- 主循环 ----------
    def run(self) -> int:
        print(f"== 长稳起跑：{self.args.hours}h，产物 {self.out}", flush=True)
        # 起跑体检打**需要鉴权**的 /metrics：/health 免鉴权，钥匙错时它照样绿，
        # 然后 12h 全 401
        try:
            self.base_metrics = self.client.json_call("GET", "/metrics", timeout=15)
        except Exception as e:
            print(f"❌ /metrics 不通（鉴权或服务未就绪），拒绝起跑: {str(e)[:160]}")
            return 1
        self._write(self.sys_f, {**self._sample_once(), "note": "t0 基线"})
        self.samples += 1
        self.sampler_thread = threading.Thread(target=self.sampler, daemon=True)
        self.sampler_thread.start()

        history, sess = [], {"ps": None, "cache_id": None}
        last_beat = time.time()
        while time.time() < self.deadline and not self.stop.is_set():
            kind = self.rng.choices([k for k, _ in MIX], [w for _, w in MIX])[0]
            try:
                if kind == "chat":
                    rec = self._chat_turn(sess, history)
                    if len(history) >= CHAT_TURNS * 2:
                        self._retire_cache(sess["cache_id"])
                        history, sess = [], {"ps": None, "cache_id": None}
                elif kind == "tool":
                    rec, cid = self._tool_turn()
                    self._retire_cache(cid)
                elif kind == "longchain":
                    rec, cid = self._longchain_turn()
                    self._retire_cache(cid)
                else:
                    rec, cid = self._cancel_turn()
                    self._retire_cache(cid)
            except Exception as e:
                rec = dict(kind=kind, ok=False,
                           error=f"{type(e).__name__}: {str(e)[:160]}")
                if kind == "chat":      # 只有 chat 腿失败才弃会话；别让别的腿的一次超时
                    self._retire_cache(sess["cache_id"])  # 把多轮样本连带缓存一起丢掉
                    history, sess = [], {"ps": None, "cache_id": None}
            self._req(**rec)

            if self.consec_fail >= MAX_CONSEC_FAIL:
                print(f"❌ 连续 {self.consec_fail} 次失败，熔断中止（不空转到 12h）",
                      flush=True)
                self.finish()
                return 2
            if time.time() - last_beat >= HEARTBEAT_INTERVAL:
                last_beat = time.time()
                alive = self.sampler_thread.is_alive()
                print(f"[心跳] {(time.time() - self.t0)/3600:.2f}h/{self.args.hours}h "
                      f"请求 {self.n} 错误 {self.errors} 采样 {self.samples}"
                      f"{'' if alive else ' ⚠️采样线程已死'}", flush=True)

            long_idle = self.n % LONG_IDLE_EVERY == 0
            lo, hi = (IDLE_LONG if long_idle else IDLE_SHORT)
            nap = self.rng.uniform(lo, hi)
            if long_idle:
                print(f"[空闲] 进入「离开」窗 {nap/60:.1f}min", flush=True)
            if self.stop.wait(nap):
                break
            if long_idle:
                print("[空闲] 「离开」窗结束", flush=True)
        self._retire_cache(sess["cache_id"])   # 收官前退休在手的会话缓存（否则必报假泄漏）
        return self.finish()

    # ---------- 收官 ----------
    def finish(self) -> int:
        self.stop.set()
        rows, bad_lines = self._read_jsonl(self.out / "req.jsonl")
        sysrows, bad_sys = self._read_jsonl(self.out / "sys.jsonl")
        try:
            final = self.client.json_call("GET", "/metrics", timeout=15)
        except Exception as e:
            final = {"error": str(e)[:120]}

        def seg(rs, i):
            n = max(1, len(rs) // 3)
            return rs[i * n:(i + 1) * n] if i < 2 else rs[2 * n:]

        def avg(rs, key):
            vals = [r[key] for r in rs if isinstance(r.get(key), (int, float))]
            return round(sum(vals) / len(vals), 3) if vals else None

        def drift(pred, keys):
            """按同质分组做三分段——四类负载混在一起平均，段间构成的随机波动会被
            读成漂移（或把真漂移稀释掉）。n<5 的格子标 null，不给不可用的数。"""
            sel = [r for r in rows if pred(r)]
            out = {}
            for k in keys:
                cells, ns = [], []
                for i in range(3):
                    s = seg(sel, i)
                    ns.append(len(s))
                    cells.append(avg(s, k) if len(s) >= 5 else None)
                out[k] = {"三段": cells, "n": ns}
            return out

        elapsed_h = (time.time() - self.t0) / 3600
        expect_samples = int(elapsed_h * 3600 / SAMPLE_INTERVAL) + 1
        chat2 = [r for r in rows if r.get("kind") == "chat" and (r.get("turn") or 0) >= 2]
        summary = {
            "hours": round(elapsed_h, 2), "requests": len(rows),
            "errors": sum(1 for r in rows if not r.get("ok")),
            "by_kind": {k: sum(1 for r in rows if r.get("kind") == k) for k, _ in MIX},
            "drift_三段": {
                "chat_turn1_冷": drift(lambda r: r.get("kind") == "chat" and r.get("turn") == 1,
                                       ("ttft", "tps", "wall")),
                "chat_turn2+_热": drift(lambda r: r.get("kind") == "chat" and (r.get("turn") or 0) >= 2,
                                        ("ttft", "tps", "wall")),
                "tool": drift(lambda r: r.get("kind") == "tool", ("ttft", "tps")),
                "longchain": drift(lambda r: r.get("kind") == "longchain", ("ttft", "tps")),
            },
            "mem_三段": {k: [avg(seg(sysrows, i), k) for i in range(3)]
                        for k in ("server_rss_mb", "wired_mb", "compressed_mb")},
            "peak_mem_三段": [avg(seg(rows, i), "peak_mem") for i in range(3)],
            "swap_首末": [sysrows[0].get("swapouts") if sysrows else None,
                         sysrows[-1].get("swapouts") if sysrows else None],
            "前缀复用": {"turn≥2 样本": len(chat2),
                       "reused>0 占比": (round(sum(1 for r in chat2 if (r.get("reused") or 0) > 0)
                                             / len(chat2), 3) if chat2 else None)},
            "缓存删除腿": {"发起": self.del_attempt, "成功": self.del_ok,
                        "交给 TTL": self.ttl_pending},
            "采样健康": {"实收": len(sysrows), "应有约": expect_samples,
                       "线程死于": self.sampler_died,
                       "坏行": {"req": bad_lines, "sys": bad_sys}},
            "metrics_首末": {k: [self.base_metrics.get(k), final.get(k)]
                            for k in ("errors_total", "generations_total", "cancelled_total")},
            "final_metrics": final,
        }
        (self.out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("== 收官 ==", json.dumps(summary, ensure_ascii=False, indent=1), flush=True)

        # 泄漏判据要减掉「交给 TTL 还没到期」的那些，否则收官必报假泄漏（TTL 45min，
        # 最后一个 TTL 周期内产生的缓存必然还在册）
        cc = (final.get("cache_count") or 0) if isinstance(final, dict) else 0
        ag = (final.get("active_generations") or 0) if isinstance(final, dict) else 0
        leaked = cc > self.ttl_pending or ag > 0
        if leaked:
            print(f"⚠️ 收官残留：cache_count={cc}（交给 TTL 的有 {self.ttl_pending} 份）"
                  f" active_generations={ag}")
        if self.sampler_died or len(sysrows) < expect_samples * 0.8:
            print(f"⚠️ 采样不完整（实收 {len(sysrows)} / 应有约 {expect_samples}）"
                  f"——内存类结论不可用")
        for f in (self.req_f, self.sys_f):
            try:
                f.close()
            except Exception:
                pass
        return 1 if (summary["errors"] or leaked) else 0

    @staticmethod
    def _read_jsonl(p: Path):
        """逐行容错：被 SIGKILL 打断留下的半行不该让 12h 的判决整个丢掉。"""
        rows, bad = [], 0
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    bad += 1
        except Exception:
            pass
        return rows, bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--port", type=int, default=int(os.environ.get("INFER_PORT", 14161)))
    ap.add_argument("--seed", type=int, default=20260818)
    args = ap.parse_args()
    # 钥匙只走 env / .env.local——命令行参数会在 ps 里挂 12 小时
    args.api_key = os.environ.get("INFER_API_KEY", "") or read_env_key()
    if not args.api_key:
        print("❌ 取不到 INFER_API_KEY（env 与 .env.local 都没有），拒绝起跑")
        return 1

    out = Path(args.outdir)
    if out.exists() and any(out.glob("*.jsonl")):
        # 续跑会把两次的行合并统计而 hours 只算本次，得出的三段是错的
        print(f"❌ {out} 已有产物，拒绝在同一目录续跑（换个 outdir，或先归档）")
        return 1

    entry = model_options(REPO) or {}
    model_dir = BACKEND_DIR / "models" / REPO.split("/")[-1]
    dialect, _ = make_dialect(model_dir, entry)   # 仅分词器，CPU-only、不占 GPU

    soak = Soak(args, dialect)
    for sig in (signal.SIGINT, signal.SIGTERM):
        # 只置停止事件：正在途中的那次请求要等它自己的 socket 超时才会回来，
        # 故 Ctrl-C 到真正退出可能有分钟级延迟（首 chunk 预算 300s）。要立刻停就 kill -9。
        signal.signal(sig, lambda *_: soak.stop.set())
    return soak.run()


if __name__ == "__main__":
    sys.exit(main())
