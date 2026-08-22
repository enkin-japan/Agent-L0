#!/usr/bin/env python3
"""§7 阶段三 · TurboQuant KV 缓存量化格（2026-08-22；首过评审后重写）。

测什么（§7-4，按 D.20 阵容收束裁剪）：
- 速度侧：kv {dense, 3.5, 2.5} × ctx {8k, 32k, 64k} 的 prefill/decode/TTFT/峰值内存
  ——KV 税减半/减四的实收（gemma-4 混合注意 1:5 下税基小，值不值得开靠数据）；
- 质量侧：**取针探针**（KV 量化伤远距召回，普通问答测不出）——同一填充文里
  10%/~53% 两个行深各埋一枚合成验证码（query 在尾部，10% 位置即距问题最远的
  远召回针），尾部一问同时召回两枚；生成同时兼作速度样本（一次预填两用）；
- MTP×KV 交叉（qwen38-8bit，条目投机自动生效）：kv {dense, 3.5} × ctx {8k, 32k}
  ——投机验证路径的数值被量化 KV 改变，输出漂移专项记录（对照 kv=dense 臂）。
  ⚠️ 交叉臂逐 run 校验 spec_drafted>0：投机没挂上就如实作废该 run——「口径
  自证的字段证了假，比没有更坏」（bench_baseline 同一条纪律）。

口径（§7-1）：server 进程态 + 真模板 + 真裸贪心（temp=0/min_p=0/rep=None；
qwen38 交叉臂例外——投机是条目生产口径，rep **显式下发 1.1**——既生效又落账，
不许靠后端缺省让产物自称裸贪心）；每格 warmup 1 + n runs 中位（n<1 拒跑，
n<3 响亮降级）；每 run 独立新缓存全量 prefill（KV 真实物化）+ 测后删缓存；
ctx 按 tokenizer 配平（单侧带：针只会加 token）；秒停（gen≤2）与截断
（gen≥GEN_TOKENS）逐 run 记账；环境头钉死。KV 档经 INFER_KV_BITS 下发
（server env 非空即赢、启动期校验，逐 kv 档重启服务）。
峰值内存口径：server 每 kv 档一起一收、臂内 ctx **强制升序**——mx.get_peak_memory
是进程终身高水位，升序下每格读数≈该格峰值（乱序会静默污染内存列）。
故障纪律：任一格失败写 error 记录后继续下一格（13:57 教训：整臂静默零数据
比失败响亮可怕得多）；server 死亡则本臂剩余格逐格记 server_died。

用法（backend/ 下）：
  .venv/bin/python scripts/bench_kv.py --out /path/to/kv.jsonl
  可选：--models …（显式给出即视为定向重跑，自动跳过 qwen38 交叉臂）
        --kv dense,3.5,2.5  --ctx 8192,32768,65536  --runs 3  --skip-cross
断点续跑：--out 已有 complete 格自动跳过（键含 spec_mode——混口径不许互认）。
"""
import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_baseline import (Client, build_user_prompt, fit_filler,  # noqa: E402
                            make_dialect, read_env_key, start_server,
                            stop_server, sysctl, wait_health)

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
from framework.utils.model_selection import model_options       # noqa: E402

DEFAULT_MODELS = ["mlx-community/gemma-4-26b-a4b-it-4bit",
                  "mlx-community/gemma-4-31b-it-4bit"]
CROSS_MODEL = "mlx-community/Qwen3.8-27B-8bit"      # MTP×KV 交叉臂（条目投机）
DEFAULT_KV = ["dense", "3.5", "2.5"]
DEFAULT_CTX = [8192, 32768, 65536]
CROSS_KV = ["dense", "3.5"]
CROSS_CTX = [8192, 32768]
GEN_TOKENS = 192

# 取针：两枚合成验证码埋进填充文（深度按行号比例），尾部一问同时召回。
# 值是无语义随机串——填充句池里绝不会撞出来，召回=真的读到了那一行。
NEEDLE_A = "QK-7311"
NEEDLE_B = "ZP-4406"
NEEDLE_LINE_A = f"（重要备忘：甲区仓库的门禁码是 {NEEDLE_A}，请务必记住。）"
NEEDLE_LINE_B = f"（重要备忘：乙区仓库的门禁码是 {NEEDLE_B}，请务必记住。）"
NEEDLE_TASK = ("请回答：甲区仓库和乙区仓库的门禁码分别是什么？"
               "先答两个码，再用一句话说明出处。")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_kv_arms(spec: str):
    """--kv 解析：strip + 白名单（dense 或 ≥1 的半整数）。非法值响亮拒跑——
    静默跑成另一个口径是最坏的失败（评审同款纪律）。"""
    arms = []
    for raw in spec.split(","):
        v = raw.strip()
        if not v:
            continue
        if v.lower() == "dense":
            arms.append("dense")
            continue
        try:
            f = float(v)
        except ValueError:
            raise SystemExit(f"❌ --kv 非法值：{raw!r}（dense 或 ≥1 的半整数）")
        if not math.isfinite(f) or f < 1 or round(f * 2) / 2 != f:
            raise SystemExit(f"❌ --kv 越域：{raw!r}（须为 ≥1 的半整数）")
        arms.append(v)
    if not arms:
        raise SystemExit("❌ --kv 为空")
    return arms


def needle_hit(text: str, code: str):
    """宽容匹配（评审：delta 展示口径 + 良性格式化会造成单向假阴性，
    把排版差异误判成 KV 损伤）。返回 (命中?, 命中上下文±40字 或 None)。"""
    a, b = code.split("-", 1)
    m = re.search(re.escape(a) + r"\W{0,3}" + re.escape(b), text or "")
    if not m:
        return False, None
    lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
    return True, text[lo:hi]


def plant_needles(filler: str) -> str:
    """把两枚针按行号比例（约 10% / 53% 深度）插进配平后的填充文。
    插入只会**加** token——配平校验用单侧带。"""
    lines = filler.splitlines()
    if len(lines) < 10:
        raise RuntimeError("填充文过短，埋不了针")
    a, b = max(1, len(lines) // 10), max(2, (len(lines) * 55) // 100)
    lines.insert(a, NEEDLE_LINE_A)
    lines.insert(b, NEEDLE_LINE_B)
    return "\n".join(lines)


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


def run_cell(cli, prompt, runs, sampling, *, ctx, expect_spec):
    """一格：warmup 1 + n runs。每 run 新缓存全量 prefill；测后删缓存。
    返回 (有效 runs, 无效 runs)——秒停/交叉臂投机未挂上的 run 记入无效侧。"""
    ok_runs, bad_runs = [], []
    budget = 180.0 + ctx / 40           # 预填看门狗随 ctx 缩放（bench 同款）
    for i in range(runs + 1):
        cache_out = {}
        try:
            last, text, ttft, cid, wall = cli.generate(
                prompt, sampling=dict(sampling), cache_out=cache_out,
                first_chunk_budget=budget)
        finally:
            cid_del = cache_out.get("cache_id")
            if cid_del:
                try:
                    cli.json_call("DELETE", f"/v1/cache/{cid_del}")
                except Exception:
                    pass
        if i == 0:
            continue        # warmup 不计
        gen_tokens = (last or {}).get("generation_tokens", 0)
        spec = {k: v for k, v in ((last or {}).get("extra") or {}).items()
                if k.startswith("spec_")}
        hit_a, ctx_a = needle_hit(text, NEEDLE_A)
        hit_b, ctx_b = needle_hit(text, NEEDLE_B)
        rec = {"ttft": ttft, "wall": wall,
               "prompt_tokens": (last or {}).get("prompt_tokens", 0),
               "gen_tokens": gen_tokens,
               "truncated": gen_tokens >= GEN_TOKENS,   # 截断≠召回失败，分开记
               "prompt_tps": (last or {}).get("prompt_tps", 0.0),
               "decode_tps": (last or {}).get("generation_tps", 0.0),
               "peak_mem": (last or {}).get("peak_memory", 0.0),
               "spec": spec,
               "needle_a": hit_a, "needle_b": hit_b,
               "needle_context": [ctx_a, ctx_b],        # 可复审的证据窗
               "text_head": (text or "")[:160]}
        if gen_tokens <= 2:
            rec["invalid"] = "秒停（gen≤2，tps 无意义）"
            bad_runs.append(rec)
            continue
        if expect_spec and not spec.get("spec_drafted"):
            rec["invalid"] = "交叉臂投机未挂上（spec_drafted 缺失）——口径自证证了假"
            bad_runs.append(rec)
            continue
        ok_runs.append(rec)
    return ok_runs, bad_runs


def _med(runs, key):
    vals = [r[key] for r in runs if r.get(key) is not None]
    return statistics.median(vals) if vals else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", default="",
                    help="缺省=26b-4bit,31b-4bit+qwen38交叉臂；显式给出=定向重跑"
                         "（自动跳过交叉臂）")
    ap.add_argument("--kv", default=",".join(DEFAULT_KV))
    ap.add_argument("--ctx", default=",".join(map(str, DEFAULT_CTX)))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--port", type=int, default=14161)
    ap.add_argument("--skip-cross", action="store_true",
                    help="跳过 qwen38 MTP×KV 交叉臂")
    args = ap.parse_args()
    if args.runs < 1:
        sys.exit("❌ --runs 须 ≥1")
    if args.runs < 3:
        log(f"⚠️ --runs={args.runs} < 3：稳态样本不足，中位退化为点估计")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    api_key = read_env_key()
    kv_arms_cli = parse_kv_arms(args.kv)
    ctx_arms_cli = sorted({int(c) for c in args.ctx.split(",")})

    import platform
    import importlib.metadata as _md
    from framework.utils.config_loader import load_config
    _icfg = load_config().get("inference") or {}
    header = {
        "type": "header", "ts": time.strftime("%Y%m%d-%H%M%S"),
        "mlx": _md.version("mlx"), "mlx_vlm": _md.version("mlx-vlm"),
        "wired_limit_mb": sysctl("iogpu.wired_limit_mb"),
        "hw": sysctl("hw.model"), "mem_gb": int(int(sysctl("hw.memsize") or 0) / 2**30),
        "macos": platform.mac_ver()[0],
        "gen_tokens": GEN_TOKENS, "runs": args.runs,
        "kv_arms": kv_arms_cli, "ctx_arms": ctx_arms_cli,
        # 口径自描述：kv 值本身不完整——scheme/start 也在决定路径
        "kv_quant_scheme": _icfg.get("kv_quant_scheme") or "turboquant",
        "quantized_kv_start": int(_icfg.get("quantized_kv_start") or 0),
        "needles": [NEEDLE_A, NEEDLE_B],
    }
    with out_path.open("a") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")

    base_sampling = {"temperature": 0.0, "min_p": 0.0,
                     "repetition_penalty": None, "max_tokens": GEN_TOKENS}
    targeted = bool(args.models.strip())
    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if targeted else list(DEFAULT_MODELS))
    plan = [(m, kv_arms_cli, ctx_arms_cli, base_sampling, "off") for m in models]
    if targeted and not args.skip_cross:
        log("ℹ️ 显式 --models = 定向重跑：qwen38 交叉臂自动跳过（要跑请单独不带 --models）")
    if not targeted and not args.skip_cross:
        # 交叉臂：投机=条目生产口径；rep 显式 1.1（生效且落账——评审 C3）
        plan.append((CROSS_MODEL, CROSS_KV, CROSS_CTX,
                     {"temperature": 0.0, "min_p": 0.0,
                      "repetition_penalty": 1.1, "max_tokens": GEN_TOKENS},
                     "entry"))

    for repo, kv_arms, ctx_arms, sampling, spec_mode in plan:
        entry = model_options(repo)
        if not entry:
            log(f"❌ {repo} 不在注册表，跳过")
            continue
        # 本地件直读（评审：resolve_model_path 的 snapshot_download 兜底不许在
        # 测量窗内触发一次 15-28G 下载）
        model_dir = BACKEND_DIR / "models" / repo.rsplit("/", 1)[-1]
        if not model_dir.is_dir():
            log(f"❌ {repo} 本地模型目录缺失（{model_dir}），跳过——测量窗不下载")
            continue
        from transformers import AutoTokenizer
        dialect, _ = make_dialect(model_dir, entry)
        tok = AutoTokenizer.from_pretrained(str(model_dir))
        prompts = {}
        for ctx in ctx_arms:
            filler = fit_filler(dialect, tok, ctx, NEEDLE_TASK)
            prompts[ctx] = build_user_prompt(
                dialect, plant_needles(filler) + "\n\n" + NEEDLE_TASK)
        for kv in kv_arms:
            key_extra = {"spec_mode": spec_mode}
            todo = sorted(c for c in ctx_arms if not cell_done(
                out_path, {"model": repo, "kv": kv, "ctx": c, **key_extra}))
            if not todo:
                log(f"⏭  {repo} kv={kv} 全部 ctx 已完成，跳过")
                continue
            os.environ["INFER_MLX_MAX_CONCURRENCY"] = ""     # 单流口径，泳道不武装
            os.environ["INFER_KV_BITS"] = kv
            spec = {"mode": "off"} if spec_mode == "off" else None
            # server 日志随产物走（可入库的原始件——.gitignore 只豁免
            # benchmarks/data/**，扔 backend/ 下会被 *.log 吞掉）
            log_path = out_path.parent / (f"server-kv-"
                                          f"{repo.rsplit('/', 1)[-1]}-{kv}.log")
            proc, lf = start_server(repo, args.port, log_path, api_key, spec=spec)
            cli = Client(args.port, api_key)
            server_dead = False
            try:
                wait_health(cli, proc, budget=1800)
            except Exception as e:
                log(f"❌ {repo} kv={kv} server 起不来: {e}（日志 {log_path}）")
                server_dead = True
            for ctx in todo:
                base = {"type": "cell", "model": repo,
                        "revision": entry.get("revision"), "kv": kv,
                        "ctx": ctx, **key_extra, "sampling": sampling}
                if server_dead:
                    with out_path.open("a") as f:
                        f.write(json.dumps({**base, "error": "server_died",
                                            "complete": False},
                                           ensure_ascii=False) + "\n")
                    continue
                try:
                    ok_runs, bad_runs = run_cell(
                        cli, prompts[ctx], args.runs, sampling,
                        ctx=ctx, expect_spec=(spec_mode == "entry"))
                    pt = [r["prompt_tokens"] for r in ok_runs]
                    # 单侧带：针与任务只会加 token（评审 nitpick——对称带会
                    # 藏住配平回归）
                    ok_ctx = all(ctx <= p <= ctx * 1.04 for p in pt)
                    rec = {**base, "runs": ok_runs, "invalid_runs": bad_runs,
                           "n_runs": len(ok_runs),
                           "prompt_tokens": pt[0] if pt else None,
                           "ctx_ok": ok_ctx,
                           "prefill_tps_median": _med(ok_runs, "prompt_tps"),
                           "decode_tps_median": _med(ok_runs, "decode_tps"),
                           "ttft_median": _med(ok_runs, "ttft"),
                           "peak_mem_max": (max(r["peak_mem"] for r in ok_runs)
                                            if ok_runs else None),
                           "needle_a_pass": sum(1 for r in ok_runs if r["needle_a"]),
                           "needle_b_pass": sum(1 for r in ok_runs if r["needle_b"]),
                           "complete": len(ok_runs) >= args.runs and ok_ctx}
                    with out_path.open("a") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if ok_runs:
                        log(f"✅ {repo.rsplit('/', 1)[-1]} kv={kv} ctx={ctx}: "
                            f"prefill {rec['prefill_tps_median']:.0f} / decode "
                            f"{rec['decode_tps_median']:.1f} tps / ttft "
                            f"{rec['ttft_median']:.1f}s / peak "
                            f"{rec['peak_mem_max']:.1f}G / 取针 A "
                            f"{rec['needle_a_pass']}/{len(ok_runs)} B "
                            f"{rec['needle_b_pass']}/{len(ok_runs)}"
                            + ("" if ok_ctx else " ⚠️ctx配平越界")
                            + (f"（作废 {len(bad_runs)}）" if bad_runs else ""))
                    else:
                        log(f"❌ {repo} kv={kv} ctx={ctx}: 有效 run 为零"
                            f"（作废 {len(bad_runs)}，日志 {log_path}）")
                except Exception as e:
                    # 格级失败：写 error 记录后继续（13:57 教训——整臂静默零
                    # 数据比失败响亮可怕得多）；server 死亡则本臂剩余格全记档
                    with out_path.open("a") as f:
                        f.write(json.dumps({**base, "complete": False,
                                            "error": f"{type(e).__name__}: {e}"},
                                           ensure_ascii=False) + "\n")
                    log(f"❌ {repo} kv={kv} ctx={ctx} 失败: {type(e).__name__}: "
                        f"{e}（日志 {log_path}）")
                    if proc.poll() is not None:
                        server_dead = True
                        log(f"💀 server 已死（rc={proc.returncode}），"
                            f"本臂剩余格记 server_died")
            stop_server(proc, lf)
            time.sleep(3)
    log("🏁 KV 量化格测量收官")
    return 0


if __name__ == "__main__":
    sys.exit(main())
