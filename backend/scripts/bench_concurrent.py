#!/usr/bin/env python3
"""§7 阶段一 · 并发格测量架（批处理泳道，2026-08-22）。

测什么（§7-2 并发格）：batch 1/2/4 的每路 decode tps 衰减曲线与合计吞吐——
MoE 专家并集税（26b-4bit，预测 batch2 路均衰至 55-65%）对照 dense（31b-4bit，
预测 ~85-87%）；算力墙拐点定位。

口径（§7-1，违者数据作废）：
- server 进程态 + 真模板（方言 PromptBuilder），走 /v1/generate 批处理泳道
  （features.batch_ok）；INFER_SPEC_MODE=off 强制（并发格是基线口径，禁投机）；
- 真裸贪心（v3 同款）：temperature=0 / min_p=0 / repetition_penalty=None；
- 每格 warmup 1 次（JIT/装载摊销，不计数）+ n≥3 计中位；
- 每行 decode tps 取生成链末 chunk 的 generation_tps（泳道口径：首 token 起算，
  prefill 不混入分母）；ttft 客户端记录；
- 批不变性哨（记录项，不作硬判据）：同 prompt × batch 的各行输出与 B=1 参考
  是否逐字节一致——批合流的数值路径（BatchRotatingKVCache extend 的旋转重排）
  与单行不同，近平局贪心可能翻转（§S.15 ① 同族，属预期内漂移）；真退化
  （空输出/复读崩坏）才算失败；
- 环境头钉死：模型 revision / mlx 版本 / wired limit / 泳道旋钮全部入产物。

用法（backend/ 下）：
  .venv/bin/python scripts/bench_concurrent.py --out /path/to/concurrent.jsonl
  可选：--models repo1,repo2  --batches 1,2,4  --runs 3  --port 14161
断点续跑：--out 已有完整格自动跳过。
"""
import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_baseline import (Client, build_user_prompt, make_dialect,  # noqa: E402
                            read_env_key, start_server, stop_server,
                            sysctl, wait_health)

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
from framework.utils.model_selection import model_options       # noqa: E402
from framework.utils.path_resolver import resolve_model_path    # noqa: E402

DEFAULT_MODELS = [
    "mlx-community/gemma-4-26b-a4b-it-4bit",   # MoE：专家并集税主角
    "mlx-community/gemma-4-31b-it-4bit",       # dense 对照
]
DEFAULT_BATCHES = [1, 2, 4]
GEN_TOKENS = 256
SAMPLING = {"temperature": 0, "min_p": 0.0, "repetition_penalty": None,
            "max_tokens": GEN_TOKENS}

# 每行各不同的题（同字数级别，decode 主导下 prefill 差异可忽略）；
# 行数上限 = max(batches)。措辞刻意具体，防短答提前 EOS。
ROW_PROMPTS = [
    "请连续地详细介绍长江从源头到入海口沿途的地理特征、水文变化与主要城市。",
    "请连续地详细介绍黄河从源头到入海口沿途的地理特征、水文变化与主要城市。",
    "请连续地详细介绍珠江从源头到入海口沿途的地理特征、水文变化与主要城市。",
    "请连续地详细介绍黑龙江从源头到入海口沿途的地理特征、水文变化与主要城市。",
]
INVARIANCE_PROMPT = "请连续地详细介绍二十四节气在农业生产中的作用与演变。"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fire_wave(cli: Client, prompts, *, budget=600.0):
    """并发发 len(prompts) 路 batch_ok 请求，返回逐行结果 + 波墙钟。"""
    results = [dict() for _ in prompts]

    def _one(i, prompt):
        try:
            t0 = time.time()
            last, text, ttft, _, wall = cli.generate(
                prompt, sampling=dict(SAMPLING), features={"batch_ok": True},
                first_chunk_budget=budget)
            results[i].update({
                "ok": True, "ttft": ttft, "wall": wall,
                "gen_tokens": (last or {}).get("generation_tokens", 0),
                "decode_tps": (last or {}).get("generation_tps", 0.0),
                "prompt_tps": (last or {}).get("prompt_tps", 0.0),
                "peak_mem": (last or {}).get("peak_memory", 0.0),
                "lane": bool(((last or {}).get("extra") or {}).get("batch_lane")),
                "text": text,
            })
        except Exception as e:
            results[i].update({"ok": False, "err": f"{type(e).__name__}: {e}"})

    t0 = time.time()
    threads = [threading.Thread(target=_one, args=(i, p))
               for i, p in enumerate(prompts)]
    for t in threads:
        t.start()
    # 同批证据（评审 C4 同款）：波内采样 decoding 峰值——active 含引擎内排队，
    # 只有 decoding≥2 才证明行真的在一个 decode 批里共跑
    peak = {"decoding": 0}

    def _sample():
        while any(t.is_alive() for t in threads):
            try:
                m = cli.json_call("GET", "/metrics", timeout=5).get("batch_lane") or {}
                peak["decoding"] = max(peak["decoding"],
                                       int(m.get("decoding") or 0))
            except Exception:
                pass
            time.sleep(0.2)

    mon = threading.Thread(target=_sample)
    mon.start()
    for t in threads:
        t.join()
    mon.join()
    return results, time.time() - t0, peak["decoding"]


def cell_done(out_path: Path, key: dict) -> bool:
    if not out_path.exists():
        return False
    for line in out_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if all(rec.get(k) == v for k, v in key.items()) and rec.get("complete"):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--batches", default=",".join(map(str, DEFAULT_BATCHES)))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--port", type=int, default=14161)
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    batches = sorted({int(b) for b in args.batches.split(",")})
    api_key = read_env_key()
    if args.runs < 3:
        log(f"⚠️ --runs={args.runs} < 3：稳态样本不足，中位退化为点估计")

    import importlib.metadata as _md
    env_header = {
        "kind": "env", "ts": time.strftime("%Y%m%d-%H%M%S"),
        "mlx": _md.version("mlx"), "mlx_vlm": _md.version("mlx-vlm"),
        "wired_limit_mb": sysctl("iogpu.wired_limit_mb"),
        "hw": sysctl("hw.model"),
        "sampling": SAMPLING, "gen_tokens": GEN_TOKENS,
        "lane_max_num_seqs": max(batches),
    }
    with out_path.open("a") as f:
        f.write(json.dumps(env_header, ensure_ascii=False) + "\n")

    for repo in models:
        pending_cells = [b for b in batches
                         if not cell_done(out_path, {"model": repo, "batch": b})]
        inv_pending = not cell_done(out_path, {"model": repo, "kind": "invariance"})
        if not pending_cells and not inv_pending:
            log(f"⏭  {repo} 全部格已完成，跳过")
            continue
        entry = model_options(repo)
        if not entry:
            log(f"❌ {repo} 不在注册表，跳过")
            continue
        model_dir = Path(resolve_model_path(repo, revision=entry.get("revision")))
        dialect, _ = make_dialect(model_dir, entry)
        import os
        os.environ["INFER_MLX_MAX_CONCURRENCY"] = str(max(batches))
        log_path = BACKEND_DIR / f"server-bench-concurrent-{repo.rsplit('/', 1)[-1]}.log"
        proc, lf = start_server(repo, args.port, log_path, api_key,
                                spec={"mode": "off"})
        cli = Client(args.port, api_key)
        try:
            wait_health(cli, proc, budget=1800)
            log(f"🚀 {repo} server 就绪；warmup…")
            w, _, _ = fire_wave(cli, [build_user_prompt(dialect, ROW_PROMPTS[0])])
            if not w[0].get("ok"):
                log(f"❌ warmup 失败：{w[0].get('err')}——本模型作废")
                continue

            # ── 批不变性哨（同 prompt：B=1 参考 + B=max 对照）──
            if inv_pending:
                p_inv = build_user_prompt(dialect, INVARIANCE_PROMPT)
                ref, _, _ = fire_wave(cli, [p_inv])
                bmax = max(batches)
                wave, _, inv_dec = fire_wave(cli, [p_inv] * bmax)
                texts = [r.get("text", "") for r in wave]
                identical_rows = len(set(texts)) == 1
                match_ref = bool(texts and texts[0] == ref[0].get("text", ""))
                degenerate = any(not t.strip() for t in texts)
                rec = {"kind": "invariance", "model": repo, "batch": bmax,
                       "identical_across_rows": identical_rows,
                       "matches_b1": match_ref, "degenerate": degenerate,
                       "texts_head": [t[:80] for t in texts],
                       "ref_head": ref[0].get("text", "")[:80],
                       "decoding_peak": inv_dec,
                       "complete": all(r.get("ok") for r in wave + ref)}
                with out_path.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log(f"🔎 批不变性：rows一致={identical_rows} 对齐B1={match_ref} "
                    f"退化={degenerate}")

            # ── 主格：batch 1/2/4 ──
            for b in pending_cells:
                prompts = [build_user_prompt(dialect, ROW_PROMPTS[i % len(ROW_PROMPTS)])
                           for i in range(b)]
                runs = []
                for r_i in range(args.runs):
                    wave, wave_wall, dec_peak = fire_wave(cli, prompts)
                    bad = [w_ for w_ in wave if not w_.get("ok") or not w_.get("lane")]
                    if bad:
                        log(f"❌ {repo} b={b} run{r_i}: 行失败/未走泳道 {bad[:1]}")
                        continue
                    if dec_peak < b:
                        # 没观察到 b 行同批共跑：这一 run 量的不是 batch=b，作废
                        log(f"❌ {repo} b={b} run{r_i}: decoding 峰值仅 {dec_peak}，"
                            f"共跑证据不足，本 run 作废")
                        continue
                    runs.append({
                        "per_row_tps": [w_["decode_tps"] for w_ in wave],
                        "ttft": [w_["ttft"] for w_ in wave],
                        "gen_tokens": [w_["gen_tokens"] for w_ in wave],
                        "peak_mem": max(w_["peak_mem"] for w_ in wave),
                        "wave_wall": wave_wall,
                        "decoding_peak": dec_peak,
                        "agg_tps": sum(w_["gen_tokens"] for w_ in wave) / wave_wall,
                    })
                    log(f"  {repo} b={b} run{r_i}: 路均 "
                        f"{statistics.mean(runs[-1]['per_row_tps']):.1f} tps, "
                        f"合计 {runs[-1]['agg_tps']:.1f} tok/s")
                complete = len(runs) >= max(2, args.runs - 1)
                row_means = [statistics.mean(r["per_row_tps"]) for r in runs]
                rec = {"kind": "cell", "model": repo,
                       "revision": entry.get("revision"), "batch": b,
                       "runs": runs, "n_runs": len(runs),
                       "row_tps_median": (statistics.median(row_means)
                                          if row_means else None),
                       "agg_tps_median": (statistics.median(
                           [r["agg_tps"] for r in runs]) if runs else None),
                       "complete": complete}
                with out_path.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log(f"✅ {repo} b={b}: 路均中位 {rec['row_tps_median']} tps / "
                    f"合计中位 {rec['agg_tps_median']} tok/s（{len(runs)} runs）")
        finally:
            stop_server(proc, lf)
            time.sleep(3)      # 端口回收
    log("🏁 并发格测量收官")
    return 0


if __name__ == "__main__":
    sys.exit(main())
