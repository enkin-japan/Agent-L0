"""Q14 受控实验：批压力下的交互延迟量化（阶段四进宪门禁的后半）。

进宪判定（用户拍板 2026-08-11）= 吞吐有提升（阶段一并发格已证：MoE 2.09×/dense
2.49× @b=4）+ **零并发事故 + 交互不被挤占**——后两者是本实验的测量对象。

形态（drill_batch_lane ③ 的量化扩展）：
  热身 1 条（弃，JIT/着色器成本不进任何臂——bench_baseline 同律）；
  臂 A（基线）：空载顺序打 N 条交互请求（单流路），记 ttft / wall / decode tps；
  臂 B（压力）：4 路 batch_ok 后台流持续解码（短行循环重发——行长按占空比配平，
    使臂 B 窗内持续发生**新行准入/预填**，交互与预填相撞的贵形态被真实采样到，
    而不是对着一个整窗冻结的批测「无退化」）；打与臂 A **同序同 prompt** 的
    N 条交互请求（贪心采样 → 两臂输出逐 token 同构，可做配对比较）；
  臂 A′（回基线）：再跑一遍臂 A，审计漂移（热、缓存、内存）与处理相分离。

/metrics 观察线程三臂对称（观察负载不偏袒任何臂）；批压力有连败熔断；
批行/交互行的缓存句柄逐行清理；批进度以 steps_total 差分记账（decoding 门控
在让路暂停期是冻结快照，不能当活性证据）。

判据**先于开跑登记**（写进 header，不许看完数字再定线）：
  PASS_TTFT_MED_DELTA_S / PASS_TTFT_MAX_DELTA_S / PASS_WALL_MED_RATIO；
  作废条件（任一即 exit 3）：批行误走单流（污染 gen_sem）、交互误走批路、
  errors_total 增量非零、压力在任一交互行期间归零、批压力熔断、收尾不净。

口径：真模板（§7-1）；26b-4bit（生产缺省候选，gemma 系天然无投机）；贪心
（temp=0/min_p=0/rep=None，与阶段一并发格同律）；INFER_MLX_MAX_CONCURRENCY=4；
kv 稠密（config 护栏 + env 校验）；HF_HUB_OFFLINE=1（测量窗不触下载兜底）。
服务日志随产物入库（--out 同目录）。预计全程 ~8-12 分钟（26B 装载为主）——
仍属长任务：挂 task-sentinel，🔴 独占 GPU 窗口。
"""
import argparse
import json
import os
import platform
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_baseline import (Client, build_user_prompt, drop_cache,  # noqa: E402
                            make_dialect, read_env_key, start_server,
                            stop_server, sysctl, wait_health)

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
from framework.utils.config_loader import load_config           # noqa: E402
from framework.utils.model_selection import model_options       # noqa: E402

PORT = 14161
MODEL = "mlx-community/gemma-4-26b-a4b-it-4bit"
CONCURRENCY = 4
PRESSURE_MIN = 3          # 批 decoding 门槛：低于它不算「压力已建立」
N_INTERACTIVE = 16
INTER_GAP_S = 1.0         # 交互间歇：批泳道恢复窗（真实交错形态；也是批的占空比来源）
BATCH_MAX_TOKENS = 48     # 行长配平：泳道占空比 ~20-30% 下行 wall ~15-20s，
                          # 臂 B ~45s 窗内每路可完结 2+ 行 → 新行准入/预填持续发生
INTER_MAX_TOKENS = 160
SAMPLING = {"temperature": 0.0, "min_p": 0.0, "repetition_penalty": None}
WORKER_FAIL_ABORT = 3     # 批工人连败熔断（防对着故障泳道刷请求风暴）

# ---- 进宪判据阈值（先登记后开跑；理由：设计代价 = 一次引擎步边界等待
# （b×步时 ~0.2s）+ 偶发与批行预填相撞（~0.4s），叠噪声给到 0.5s 中位余量；
# 最坏单点 1.5s 对应「撞上整段批预填」的可感知但不破坏体验的上界；
# wall 比值 1.15 = 解码期让路应近乎完全，交互全程拉长 >15% 即算被挤占）----
PASS_TTFT_MED_DELTA_S = 0.5
PASS_TTFT_MAX_DELTA_S = 1.5
PASS_WALL_MED_RATIO = 1.15

TOPICS = ["旅行计划", "读书笔记", "晚餐菜谱", "周末安排", "运动记录", "学习方法",
          "会议纪要", "购物清单", "花园养护", "相机选购", "城市历史", "咖啡冲煮",
          "阳台种菜", "整理照片", "家庭预算", "登山装备"]


def _med(xs):
    xs = [x for x in xs if x is not None]
    return (round(statistics.median(xs), 4), len(xs)) if xs else (None, 0)


class BatchPressure:
    """4 路 batch_ok 后台流：短行循环重发直到 stop。逐请求记账；连败熔断；
    行缓存逐行清理；shutdown 超时对在飞行发取消端点。"""

    def __init__(self, cli: Client, prompts):
        self.cli = cli
        self.prompts = prompts
        self.stop = threading.Event()
        self.fatal = None                  # 连败熔断留证
        self._rows = []
        self._lock = threading.Lock()
        self.threads = []
        self._inflight = {}                # wid -> cache_out（gen_id 取消寻址用）

    def rows(self):
        with self._lock:
            return list(self._rows)

    def _worker(self, wid: int) -> None:
        i, consec_fail = 0, 0
        while not self.stop.is_set():
            t0 = time.time()
            rec = {"worker": wid, "seq": i, "t0": round(t0, 3)}
            cout = {}
            self._inflight[wid] = cout
            try:
                last, _text, _ttft, _cid, wall = self.cli.generate(
                    self.prompts[(wid + i) % len(self.prompts)],
                    sampling={**SAMPLING, "max_tokens": BATCH_MAX_TOKENS},
                    features={"batch_ok": True}, cache_out=cout)
                rec.update({"ok": True, "wall": wall,
                            "gen_tok": (last or {}).get("generation_tokens"),
                            "tps": (last or {}).get("generation_tps"),
                            "batch_lane": bool(((last or {}).get("extra") or {})
                                               .get("batch_lane")),
                            "peak_mem": (last or {}).get("peak_memory")})
                consec_fail = 0
            except Exception as e:
                rec.update({"ok": False, "err": f"{type(e).__name__}: {e}"[:160]})
                consec_fail += 1
            finally:
                drop_cache(self.cli, cout)
                self._inflight.pop(wid, None)
            with self._lock:
                self._rows.append(rec)
            if consec_fail >= WORKER_FAIL_ABORT:
                self.fatal = (f"worker{wid} 连败 {consec_fail} 次，熔断"
                              f"（末错 {rec.get('err')}）")
                self.stop.set()
                return
            if not rec["ok"]:
                self.stop.wait(1.0)        # 失败退避：不许对故障泳道刷风暴
            i += 1

    def start(self, n: int) -> None:
        for w in range(n):
            t = threading.Thread(target=self._worker, args=(w,), daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self, timeout: float = 30.0) -> bool:
        self.stop.set()
        deadline = time.time() + timeout
        for t in self.threads:
            t.join(timeout=max(0.5, deadline - time.time()))
        if any(t.is_alive() for t in self.threads):
            # 温柔收不动 → 对在飞行发取消端点，再等一轮
            for cout in list(self._inflight.values()):
                gid = (cout or {}).get("gen_id")
                if gid:
                    try:
                        self.cli.json_call(
                            "POST", f"/v1/generate/{gid}/cancel", timeout=10)
                    except Exception:
                        pass
            deadline = time.time() + timeout
            for t in self.threads:
                t.join(timeout=max(0.5, deadline - time.time()))
        return not any(t.is_alive() for t in self.threads)


def interactive_round(cli: Client, prompt: str):
    """一条交互请求 + 生成期间并行盯 /metrics（三臂对称；观察错误入账）。"""
    obs = {"paused_seen": False, "decoding_max": 0, "steps_seen": [],
           "samples": 0, "watch_errors": 0}
    done = threading.Event()

    def _watch():
        while not done.is_set():
            try:
                m = cli.json_call("GET", "/metrics", timeout=5)
                bl = m.get("batch_lane") or {}
                obs["paused_seen"] = obs["paused_seen"] or bool(bl.get("paused"))
                obs["decoding_max"] = max(obs["decoding_max"],
                                          int(bl.get("decoding") or 0))
                obs["steps_seen"].append(int(bl.get("steps_total") or 0))
                obs["samples"] += 1
            except Exception:
                obs["watch_errors"] += 1
            done.wait(0.05)

    th = threading.Thread(target=_watch, daemon=True)
    th.start()
    cout = {}
    try:
        last, _text, ttft, _cid, wall = cli.generate(
            prompt, sampling={**SAMPLING, "max_tokens": INTER_MAX_TOKENS},
            features={}, cache_out=cout)
        gen_tok = (last or {}).get("generation_tokens")
        row = {"ok": True, "ttft": ttft, "wall": wall, "gen_tok": gen_tok,
               "tps": (last or {}).get("generation_tps"),
               "prompt_tps": (last or {}).get("prompt_tps"),
               "peak_mem": (last or {}).get("peak_memory"),
               "batch_lane": bool(((last or {}).get("extra") or {})
                                  .get("batch_lane")),
               "instant_stop": bool(gen_tok is not None and gen_tok <= 2)}
    except Exception as e:
        row = {"ok": False, "err": f"{type(e).__name__}: {e}"[:160]}
    finally:
        done.set()
        th.join(timeout=6)             # > 观察线程自身的 5s 超时（评审 m25）
        drop_cache(cli, cout)
    obs["steps_seen"] = [obs["steps_seen"][0], obs["steps_seen"][-1]] \
        if obs["steps_seen"] else []   # 只留首末：产物别背 20Hz 原始流
    row.update(obs)
    return row


def run_arm(cli: Client, dialect, arm: str):
    rows = []
    for i in range(N_INTERACTIVE):
        prompt = build_user_prompt(
            dialect, f"帮我想想{TOPICS[i % len(TOPICS)]}要准备什么，说三点。")
        row = interactive_round(cli, prompt)
        row["prompt_idx"] = i    # 配对键：按题配对，不按压实后的下标（复审 m11）
        rows.append(row)
        if i < N_INTERACTIVE - 1:      # 尾行不睡（评审 m29）
            time.sleep(INTER_GAP_S)
    print(f"[q14] 臂 {arm}：{sum(1 for r in rows if r.get('ok'))}/{len(rows)} ok",
          flush=True)
    return rows


def wait_pressure(cli: Client, budget: float = 90.0) -> int:
    """等批压力建立：decoding ≥ PRESSURE_MIN。等不到就响亮失败——
    压力没建立时测出的「无退化」是假证据。"""
    t0 = time.time()
    peak = 0
    while time.time() - t0 < budget:
        m = cli.json_call("GET", "/metrics", timeout=5)
        dec = int((m.get("batch_lane") or {}).get("decoding") or 0)
        peak = max(peak, dec)
        if dec >= PRESSURE_MIN:
            return dec
        time.sleep(0.3)
    raise RuntimeError(f"批压力 {budget}s 内未建立（decoding 峰值 {peak} < "
                       f"{PRESSURE_MIN}）——臂 B 无效，拒绝继续")


def _bl(cli: Client) -> dict:
    return (cli.json_call("GET", "/metrics", timeout=10)).get("batch_lane") or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="产物 jsonl（server 日志同目录）")
    args = ap.parse_args()
    out_path = Path(args.out)
    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"❌ {out_path} 已有内容——本脚本不续跑，拒绝覆盖既有 🔴 窗产物", flush=True)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- 口径护栏（全部先于起服）----
    cfg_inf = (load_config().get("inference") or {})
    if str(((cfg_inf.get("speculative") or {}).get("mode")) or "off") != "off":
        print("❌ 要求 config 全局段 speculative.mode=off", flush=True)
        return 2
    if cfg_inf.get("kv_bits") is not None or os.environ.get("INFER_KV_BITS"):
        print("❌ 要求 KV 稠密（config kv_bits=null 且环境无 INFER_KV_BITS）", flush=True)
        return 2
    api_key = read_env_key()
    cli = Client(PORT, api_key)
    if cli.health_ok():
        print("❌ :14161 已有服务——本实验要求独占窗口，拒绝叠加", flush=True)
        return 2
    entry = model_options(MODEL)
    if not entry:
        print(f"❌ {MODEL} 不在注册表", flush=True)
        return 2
    os.environ["INFER_MLX_MAX_CONCURRENCY"] = str(CONCURRENCY)
    os.environ["HF_HUB_OFFLINE"] = "1"     # 测量窗不触下载兜底：revision 漂移
    # 时 resolve 直接失败，而不是在 🔴 独占窗里拖一个多 GB 下载
    from framework.utils.path_resolver import resolve_model_path  # noqa: E402
    try:
        mp = resolve_model_path(MODEL, revision=entry.get("revision"))
    except Exception as e:
        print(f"❌ 本地模型目录缺失/revision 不符（离线模式拒绝下载）：{e}", flush=True)
        return 2
    if mp is None or not Path(mp).is_dir():
        print(f"❌ 本地模型目录缺失：{MODEL}", flush=True)
        return 2
    dialect, _tok = make_dialect(Path(mp), entry)
    bp_prompts = [build_user_prompt(
        dialect, f"详细写一段关于{t}的完整说明，尽量展开。") for t in TOPICS[:6]]

    f = out_path.open("w")                 # 先开产物再起服（评审 m22）
    write = lambda rec: (f.write(json.dumps(rec, ensure_ascii=False) + "\n"),  # noqa: E731
                         f.flush())
    proc, lf = start_server(MODEL, PORT, out_path.parent / "server-q14.log",
                            api_key, spec={"mode": "off"})
    invalid = []
    try:
        wait_health(cli, proc, budget=1200)
        m0 = cli.json_call("GET", "/metrics", timeout=10)
        from importlib.metadata import version as pkg_version
        mem_raw = sysctl("hw.memsize")
        write({"type": "header", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "model": MODEL, "revision": entry.get("revision"),
               "machine": sysctl("hw.model"),
               "mem_gb": int(mem_raw) // 2**30 if str(mem_raw).isdigit() else None,
               "wired_limit_mb": sysctl("iogpu.wired_limit_mb"),
               "macos": platform.mac_ver()[0],
               "mlx": pkg_version("mlx"), "mlx_vlm": pkg_version("mlx-vlm"),
               "kv": "dense", "speculative": "off",
               "concurrency": CONCURRENCY, "pressure_min": PRESSURE_MIN,
               "batch_lane_t0": m0.get("batch_lane"),   # max_num_seqs=4 的自证
               "n_interactive": N_INTERACTIVE, "inter_gap_s": INTER_GAP_S,
               "inter_max_tokens": INTER_MAX_TOKENS,
               "batch_max_tokens": BATCH_MAX_TOKENS,
               "sampling": SAMPLING,
               "thresholds": {"ttft_med_delta_s": PASS_TTFT_MED_DELTA_S,
                              "ttft_max_delta_s": PASS_TTFT_MAX_DELTA_S,
                              "wall_med_ratio": PASS_WALL_MED_RATIO},
               "cache_count_t0": m0.get("cache_count"),
               "swap_t0": sysctl("vm.swapusage"),
               "errors_total_t0": m0.get("errors_total")})

        # ── 热身（弃）──
        wu = interactive_round(cli, build_user_prompt(dialect, "简单打个招呼。"))
        write({"type": "warmup", "row": wu})

        # ── 臂 A：空载基线 ──
        rows_a = run_arm(cli, dialect, "A")
        write({"type": "arm", "arm": "A_baseline", "rows": rows_a})

        # ── 臂 B：批压力下交互 ──
        print("[q14] 起 4 路批压力…", flush=True)
        bp = BatchPressure(cli, bp_prompts)
        bp.start(CONCURRENCY)
        try:
            dec = wait_pressure(cli)
            print(f"[q14] 压力已建立 decoding={dec}", flush=True)
            bl0, tb0 = _bl(cli), time.time()
            rows_b = run_arm(cli, dialect, "B")
            bl1, tb1 = _bl(cli), time.time()
        finally:
            clean = bp.shutdown()
        batch_rows = bp.rows()
        write({"type": "arm", "arm": "B_pressured", "rows": rows_b,
               "window_s": round(tb1 - tb0, 2),
               "steps_total_window": [bl0.get("steps_total"), bl1.get("steps_total")],
               "pressure_clean_shutdown": clean, "pressure_fatal": bp.fatal,
               "batch_rows": batch_rows})

        # ── 臂 A′：回基线（漂移审计）──
        rows_a2 = run_arm(cli, dialect, "A2")
        write({"type": "arm", "arm": "A2_rebaseline", "rows": rows_a2})

        # ── 判据结算 ──
        mf = cli.json_call("GET", "/metrics", timeout=10)
        ok_a = [r for r in rows_a if r.get("ok")]
        ok_b = [r for r in rows_b if r.get("ok")]
        ok_a2 = [r for r in rows_a2 if r.get("ok")]
        med = lambda rows, k: _med([r.get(k) for r in rows])  # noqa: E731

        ttft_a, n_ta = med(ok_a, "ttft")
        ttft_b, n_tb = med(ok_b, "ttft")
        wall_a, _ = med(ok_a, "wall")
        wall_b, _ = med(ok_b, "wall")
        ttft_a2, _ = med(ok_a2, "ttft")
        # 配对差（同序同 prompt 贪心 → 按 prompt_idx 键配对）。修正（复审
        # m11）：旧版按过滤后列表下标 zip，一臂有失败行时其后全部跨题错位
        # （run 1 归档件 q14-run1-invalid.jsonl 的 pair 值即该口径，读档慎）；
        # 失败行现按真实缺席处理——只配两臂同题都 ok 的行。
        b_by_idx = {r.get("prompt_idx"): r for r in ok_b}
        pair_ttft = [round(b_by_idx[r["prompt_idx"]]["ttft"] - r["ttft"], 4)
                     for r in ok_a
                     if r.get("prompt_idx") in b_by_idx
                     and r.get("ttft") is not None
                     and b_by_idx[r["prompt_idx"]].get("ttft") is not None]
        ttft_max_delta = max(pair_ttft) if pair_ttft else None
        ttft_med_delta, _ = _med(pair_ttft)
        wall_ratio = (round(wall_b / wall_a, 4)
                      if wall_a and wall_b else None)

        # 批窗口进度（steps 差分为主证；窗内完结行 token 为下界旁证）
        steps_delta = ((bl1.get("steps_total") or 0) - (bl0.get("steps_total") or 0)
                       if bl0.get("steps_total") is not None else None)
        bwin = [r for r in batch_rows if r.get("ok") and r["t0"] >= tb0
                and r["t0"] + (r.get("wall") or 0) <= tb1]

        # 作废条件（任一即整场作废）
        if any(not r.get("batch_lane") for r in batch_rows if r.get("ok")):
            invalid.append("批行误走单流（gen_sem 污染臂 B 的 ttft）")
        if any(r.get("batch_lane") for r in ok_a + ok_b + ok_a2):
            invalid.append("交互行误走批路")
        if bp.fatal:
            invalid.append(f"批压力熔断：{bp.fatal}")
        if not clean:
            invalid.append("批压力收尾不净（结算时仍有在飞行）")
        pressure_lost = [i for i, r in enumerate(ok_b)
                         if (r.get("decoding_max") or 0) == 0]
        if pressure_lost:
            invalid.append(f"交互行 {pressure_lost} 期间批压力归零（decoding_max=0）")
        watch_dead = [i for i, r in enumerate(ok_b) if r.get("samples", 0) == 0]
        if watch_dead:
            invalid.append(f"交互行 {watch_dead} 观察线程零采样，让路证据缺失")
        if any(r.get("instant_stop") for r in ok_a + ok_b + ok_a2):
            invalid.append("存在秒停交互行（gen≤2，tps 采样窗失真）")
        err_delta = (mf.get("errors_total") or 0) - (m0.get("errors_total") or 0)
        if err_delta:
            invalid.append(f"服务端 errors_total 增量 {err_delta}")
        if len(ok_a) < N_INTERACTIVE or len(ok_b) < N_INTERACTIVE:
            invalid.append(f"交互行有失败（A {len(ok_a)}/{N_INTERACTIVE}，"
                           f"B {len(ok_b)}/{N_INTERACTIVE}）")

        yield_rate = (round(sum(1 for r in ok_b if r.get("paused_seen"))
                            / len(ok_b), 3) if ok_b else None)
        drift_flag = (ttft_a and ttft_a2
                      and abs(ttft_a2 - ttft_a) / ttft_a > 0.10)
        passed = (not invalid
                  and ttft_med_delta is not None
                  and ttft_med_delta <= PASS_TTFT_MED_DELTA_S
                  and ttft_max_delta <= PASS_TTFT_MAX_DELTA_S
                  and wall_ratio is not None
                  and wall_ratio <= PASS_WALL_MED_RATIO)
        verdict = {
            "type": "verdict", "passed": passed, "invalid": invalid,
            "ttft_med_s": {"A": [ttft_a, n_ta], "B": [ttft_b, n_tb],
                           "A2": ttft_a2, "A2_drift_gt_10pct": bool(drift_flag)},
            "ttft_pair_delta_s": {"med": ttft_med_delta, "max": ttft_max_delta,
                                  "n": len(pair_ttft)},
            "wall_med_s": {"A": wall_a, "B": wall_b, "ratio": wall_ratio},
            "decode_tps_med": {"A": med(ok_a, "tps")[0], "B": med(ok_b, "tps")[0]},
            "yield_seen_rate": yield_rate,
            "pressure_decoding_min": (min((r.get("decoding_max") or 0)
                                          for r in ok_b) if ok_b else 0),
            "batch_steps_in_window": steps_delta,
            "batch_rows_total": len(batch_rows),
            "batch_rows_failed": sum(1 for r in batch_rows if not r.get("ok")),
            "batch_rows_done_in_window": len(bwin),
            "batch_tok_in_window_lower_bound":
                sum(r.get("gen_tok") or 0 for r in bwin),
            "peak_mem_gb": max([r.get("peak_mem") or 0
                                for r in ok_a + ok_b + ok_a2 + batch_rows] or [0]),
            "cache_count_t1": mf.get("cache_count"),
            "swap_t1": sysctl("vm.swapusage"),
            "errors_total_delta": err_delta,
        }
        write(verdict)
        print("== Q14 判据 ==", json.dumps(verdict, ensure_ascii=False, indent=1),
              flush=True)
        return 0 if passed else 3
    except Exception as e:
        write({"type": "abort", "err": f"{type(e).__name__}: {e}"[:300]})
        raise
    finally:
        f.close()
        stop_server(proc, lf)
        print("[q14] server 已收", flush=True)


if __name__ == "__main__":
    sys.exit(main())
