#!/usr/bin/env python3
"""A8 实测：多轮对话下 Qwen3.8 off vs drafter 的端到端净收益（MTP_PROJECT §3.1 A8）。

口径沿革（v3，2026-08-17，上位解验收）：客户端组装改为**流式**（镜像引擎
_assemble_prompt/commit：turn1 全量、turn2+ 记录流+方言增量段，prompt 字节只增
不改）——v2 的全量重渲染口径里 env 块逐轮后移，测不到上位解要验的纯追加快路。
新增 e4b-8bit 臂验旋转态滑窗放行；turn2+ 硬断言：走增量路 + 门不拒 +
reused_prefix 非零，任一不满足整臂作废。

口径沿革（v2，2026-08-17，首过评审 C1/C2/C3 后改写）：
- 原前提「drafter 丢前缀复用、每轮退冷预填」对 Qwen3.8 **不成立**（评审 C2）：
  缓冲缓存转换只认 gemma 系 RotatingKVCache，qwen3_5 是 KVCache+ArraysCache，
  drafter 臂照常走前缀复用路。
- 而该复用路本身对 qwen3_5 曾是静默错乱（评审 C1：线性注意力 ArraysCache 无
  回滚接口、被裁剪循环跳过）——T8 根修后引擎在该形态**响亮放弃复用退冷预填**。
  故本测 v2 的意义：两臂同为冷预填口径下，多轮墙钟里 drafter 的解码增益净值。
- prompt_tps 引擎口径含复用 token（评审 C3），**不入分析**；预填速度看 ttft 与
  prefill_tokens_new。
- MAX_TOKENS=320 会截断部分轮次（truncated 字段记录）：多轮语义是"半自然对话+
  确定性填充"，速度测量口径不受影响。

测量纪律：
- 走生产 serve 路（inference/server.py + INFER_SPEC_* env，off 臂显式下发
  mode=off，不依赖 config 缺省）；每臂起服后断言 /v1/models 身份防串臂；
- 每臂先跑一轮热身（丢弃，删缓存）再进正式对话（JIT/内核编译不入 turn1）；
- 逐轮记录 prompt_sha16 / text_sha16（sha256 截 16 位）/ text_head（可审计、可跨臂对比字节）；
- gen_tokens<=2 的轮次响亮拒收（bench v1 秒 EOS 事故口径）；drafter 臂任一轮
  缺 spec_drafted 即拒收（草稿件未接管防呆）；
- 断点续跑：只保留**完整臂**的行（不完整臂整臂重跑，残行重写时丢弃）；臂键
  含 block_size；
- 单 GPU 纪律：串行起收 server，一臂一进程。

用法（backend/ 下，GPU 空闲时）：
  .venv/bin/python scripts/bench_a8_multiturn.py \\
      --out ../benchmarks/data/a8-multiturn/a8-<stamp>.jsonl
"""
import argparse
import hashlib
import json
import platform
import sys
import time
from importlib.metadata import version as _pkg_version
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from bench_baseline import (                                    # noqa: E402
    Client, FILLER_SENTENCES, SAMPLING_BASE, drop_cache, make_dialect,
    read_env_key, start_server, stop_server, wait_health)
from framework.core import prompt_stream as pstream             # noqa: E402
from framework.utils.model_selection import model_options       # noqa: E402
from framework.utils.path_resolver import resolve_model_path    # noqa: E402

MAX_TOKENS = 320          # 每轮生成上限：够一段结构化摘要，不至于失控
FILLER_PER_TURN = 36      # 每轮背景句数（~800-1000 token/轮，历史随轮线性增长）
PORT = 14161              # bench 专用口（与生产 :14159/:14160 隔离）

ARMS = [
    # (registry repo, mode, draft 目录名, block_size)
    # e4b 臂（v3 增，上位解验收）：滑窗 512/1024 在五轮长填充下必然旋转——
    # 验的是「旋转态缓存在纯追加口径下不再被 T8 门拒」（修复前超窗即整段冷预填）
    ("mlx-community/gemma-4-e4b-it-8bit", "off",  None,                  None),
    ("mlx-community/Qwen3.8-27B-8bit", "off",     None,                  None),
    ("mlx-community/Qwen3.8-27B-8bit", "drafter", "Qwen3.8-27B-MTP-8bit", 5),
    ("mlx-community/Qwen3.8-27B-4bit", "off",     None,                  None),
    ("mlx-community/Qwen3.8-27B-4bit", "drafter", "Qwen3.8-27B-MTP-4bit", 3),
]

_QUESTIONS = [
    "请用三条要点总结上面这段背景资料的主要内容。",
    "结合前面所有背景资料，指出其中重复出现的主题，并各举一例。",
    "对比本轮与第一轮的背景资料，列出两处不同点。",
    "基于目前全部资料，给出一个 100 字以内的整体概述。",
    "最后，请按轮次顺序各用一句话回顾每一轮资料的重点。",
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _filler(turn: int) -> str:
    base = turn * FILLER_PER_TURN
    parts = [f"第{base + i + 1}句：{FILLER_SENTENCES[(base + i) % len(FILLER_SENTENCES)]}"
             for i in range(FILLER_PER_TURN)]
    return f"背景资料（第{turn + 1}批）：\n" + "\n".join(parts)


def run_arm(repo: str, mode: str, draft_dir, block_size, out_fh, run_id: str) -> None:
    entry = model_options(repo)
    if not entry:
        raise RuntimeError(f"{repo} 不在注册表（model_options 返回空）——防静默回落错方言")
    model_dir = resolve_model_path(repo, revision=entry.get("revision"))
    dialect, _tok = make_dialect(Path(model_dir), entry)
    api_key = read_env_key()

    if mode == "drafter":
        draft_path = BACKEND_DIR / "models" / draft_dir
        if not draft_path.is_dir():
            raise RuntimeError(f"草稿件目录不存在: {draft_path}")
        spec = {"mode": "drafter", "draft_model": str(draft_path),
                "block_size": block_size}
    else:
        spec = {"mode": "off"}   # 显式下发：不依赖 config 缺省（评审 M3）

    outp = Path(out_fh.name)
    log_path = outp.with_name(f"{outp.stem}.{Path(repo).name}.{mode}.server.log")
    proc, lf = start_server(repo, PORT, log_path, api_key=api_key, spec=spec)
    cli = Client(PORT, api_key=api_key)
    try:
        wait_health(cli, proc)
        got = cli.json_call("GET", "/v1/models")
        served = (got.get("data") or [{}])[0].get("id", "")
        if served != repo:
            raise RuntimeError(f"起服身份不符：期望 {repo}，/v1/models 报告 {served!r}"
                               "——疑似上一臂进程未退干净（评审 M5）")

        # 热身轮（JIT/内核编译不入 turn1；评审 M7）：独立缓存，用完即删
        warm_box = {}
        cli.generate(dialect.build_prompt(
            messages=[{"role": "user", "content": "热身：请回答 1+1=?"}],
            tools=None, include_env=False),
            sampling={**SAMPLING_BASE, "max_tokens": 16}, cache_out=warm_box)
        drop_cache(cli, warm_box)

        messages = [{"role": "system", "content": "你是一个耐心细致的资料分析助手。"}]
        cache_id = None
        cache_box = {}
        # v3（上位解验收口径）：镜像引擎 _assemble_prompt/commit 的流式组装——
        # turn1 全量渲染，turn2+ 走「记录流+方言增量段」，prompt 字节只增不改。
        # v2 的客户端全量重渲染口径测不到新机制（env 块逐轮后移正是被修掉的破坏者）。
        ps = None
        prev_total = None      # 上轮 prompt_tokens + generation_tokens（复用期望值）
        overhang_falls = 0     # drafter 臂「停止符后同块已接受 token」的合法门拒次数
        for turn in range(len(_QUESTIONS)):
            messages.append({"role": "user",
                             "content": f"{_filler(turn)}\n\n{_QUESTIONS[turn]}"})
            used_delta, tail = False, None
            if ps is not None and pstream.stream_valid(ps, messages, None):
                d_out = dialect.build_prompt_delta(messages[ps.seen + 1:],
                                                   prev_tail=ps.tail)
                if d_out is not None:
                    prompt, tail = ps.stream + d_out[0], d_out[1]
                    used_delta = True
            if not used_delta:
                prompt = dialect.build_prompt(messages=messages, tools=None,
                                              include_env=False)
                tail = dialect.tail_state()
            raw_box = {}   # 原始字节口径（评审 C2）：流提交与引擎同源，不用展示 delta
            last, text, ttft, cache_id, wall = cli.generate(
                prompt, cache_id=cache_id,
                # token 级增量喂入：历史段取服务端缓存记录、永不重分词（根除
                # 采样出非规范 BPE 序列时的再分词错位——v3 首跑三臂败因之一）
                features=({"prompt_delta_offset": len(ps.stream)}
                          if used_delta else None),
                sampling={**SAMPLING_BASE, "max_tokens": MAX_TOKENS},
                cache_out=cache_box, raw_out=raw_box)
            gen_tokens = (last or {}).get("generation_tokens") or 0
            if gen_tokens <= 2:
                raise RuntimeError(
                    f"turn {turn + 1} gen_tokens={gen_tokens}：疑似秒 EOS 退化"
                    "（bench v1 事故口径），整臂作废（评审 C4）")
            extra = (last or {}).get("extra") or {}
            if mode == "drafter" and not extra.get("spec_drafted"):
                raise RuntimeError(
                    f"turn {turn + 1} 无 spec_drafted：drafter 臂草稿件未接管，"
                    "整臂作废（评审 M4）")
            if mode == "off" and extra.get("spec_drafted"):
                raise RuntimeError(
                    f"turn {turn + 1} 出现 spec_drafted：off 臂被投机接管，"
                    "疑似串臂/旧 spec 残留，整臂作废（评审 M10）")
            ps = pstream.commit(prompt, raw_box.get("text", ""), messages, None, tail)
            messages.append({"role": "assistant", "content": text})
            ptok = (last or {}).get("prompt_tokens")
            reused = extra.get("reused_prefix")
            rec = {"run_id": run_id, "model": repo, "mode": mode,
                   "block_size": block_size, "turn": turn + 1,
                   "ttft_s": round(ttft, 3), "wall_s": round(wall, 3),
                   "prompt_tokens": ptok, "generation_tokens": gen_tokens,
                   # 引擎 prompt_tps 分子含复用 token（评审 C3），只留档不入分析
                   "prompt_tps_raw": (last or {}).get("prompt_tps"),
                   "prefill_tokens_new": (ptok - reused
                                          if ptok is not None and reused is not None
                                          else None),
                   "generation_tps": (last or {}).get("generation_tps"),
                   "peak_memory": (last or {}).get("peak_memory"),
                   "truncated": gen_tokens >= MAX_TOKENS,
                   "assembly": "stream" if used_delta else "full",
                   "extra": extra,
                   "prompt_sha16": _sha(prompt), "text_sha16": _sha(text),
                   "text_head": text[:120], "text_chars": len(text)}
            out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out_fh.flush()
            print(f"  [{Path(repo).name} {mode}] turn {turn + 1}: "
                  f"ttft {ttft:.2f}s wall {wall:.2f}s "
                  f"prompt {ptok} gen {gen_tokens} extra {extra}", flush=True)

            # v3 验收断言（上位解金标准）——行已落盘再断言（首过评审 C2：断言先炸
            # 会把肇事轮的全部数据一起带走，排障就得重烧 GPU）。turn2+：
            #  · 必须走增量路（组装侧回归）；reused_prefix 仪表必须在（缺仪表≠漂移）；
            #  · 门拒里唯一放行 drafter 臂的「缓存悬垂」（reuse_diag.prefix_len==
            #    record_len：MTP 停止符后同块已接受 token 污染线性层状态，门拒正确、
            #    冷回退自愈）；其余门拒与命中不足（期望 上轮总量±1，停止符喂入与否
            #    差一枚）一律整臂作废。
            if turn >= 1:
                diag = extra.get("reuse_diag") or {}
                if not used_delta:
                    raise RuntimeError(f"turn {turn + 1} 未走流增量路（stream_valid/"
                                       f"delta 拒了）：上位解组装回归，整臂作废")
                if not extra.get("delta_feed"):
                    # 后端静默退字节路（评审 S10/C3 配套仪表）：即便本轮分词恰好
                    # 规范、复用照常命中，也必须被看见——否则失效无痕
                    raise RuntimeError(f"turn {turn + 1} 后端未走 token 级喂入"
                                       f"（extra 无 delta_feed），整臂作废")
                if "reused_prefix" not in extra:
                    raise RuntimeError(f"turn {turn + 1} extra 缺 reused_prefix：前缀"
                                       f"命中仪表缺失（非漂移），整臂作废")
                if extra.get("reuse_blocked"):
                    # 悬垂签名（评审 C3）：token 级喂入下 prefix_len==record_len
                    # 恒真，不能作判据——悬垂的本质是缓存流位置超出公共前缀
                    _mo = diag.get("max_offset")
                    if mode == "drafter" and _mo is not None \
                            and diag.get("prefix_len") == diag.get("record_len") \
                            and _mo > (diag.get("prefix_len") or 0):
                        overhang_falls += 1
                        print(f"  [诊断] turn {turn + 1} 缓存悬垂门拒（合法冷回退）："
                              f"{diag}", flush=True)
                    else:
                        raise RuntimeError(
                            f"turn {turn + 1} 复用被门拒：{extra['reuse_blocked']} "
                            f"diag={diag}——纯追加+token 级喂入下即记录错位，整臂作废")
                else:
                    rp = extra.get("reused_prefix") or 0
                    if prev_total is not None \
                            and not (prev_total - 1 <= rp <= prev_total):
                        raise RuntimeError(
                            f"turn {turn + 1} 复用不足：reused_prefix={rp}，期望 "
                            f"{prev_total}±1（上轮 prompt+gen），整臂作废")
            prev_total = (ptok or 0) + gen_tokens
        if overhang_falls:
            print(f"  [臂小结] {Path(repo).name}×{mode} 悬垂冷回退 {overhang_falls} 次"
                  f"（合法门拒，MTP 停止符后同块已接受 token）", flush=True)
        drop_cache(cli, cache_box)
    finally:
        stop_server(proc, lf)
        time.sleep(3)   # Metal 释放余裕，防下一臂装载与回收撞车


def _arm_key(rec) -> tuple:
    return (rec.get("model"), rec.get("mode"), rec.get("block_size"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="逐轮 JSONL 产物路径")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S")

    # 断点续跑：只承认完整臂；不完整臂的残行在重写时丢弃（评审 M1/M2）
    done, kept_lines = set(), []
    if out.exists():
        arms_seen = {}
        rows = []
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                     # SIGKILL 截断的残行：丢弃
            if r.get("kind") != "header" and "run_id" not in r:
                continue                 # v1 旧 schema 行：不认领、重写时丢弃（评审 M9）
            if r.get("kind") is None and "assembly" not in r:
                continue                 # v2 全量重渲染口径行：与 v3 不可混档，丢弃
            rows.append((line, r))
            if r.get("kind") in ("header", "arm_failed"):
                continue
            arms_seen.setdefault(_arm_key(r), set()).add(r.get("turn"))
        done = {k for k, turns in arms_seen.items()
                if turns >= set(range(1, len(_QUESTIONS) + 1))}
        # arm_failed 行恒保留（v3 评审 C3）：失败证据不能被下一次续跑抹掉
        kept_lines = [l for l, r in rows
                      if r.get("kind") in ("header", "arm_failed")
                      or _arm_key(r) in done]
        dropped = len(rows) - len(kept_lines)
        out.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""),
                       encoding="utf-8")
        if done or dropped:
            print(f"断点续跑：已完成臂 {sorted(done)}，丢弃残行 {dropped}")

    pending = [(r, m, d, b) for r, m, d, b in ARMS if (r, m, b) not in done]
    if not pending:
        print("A8_ALL_DONE（全部臂已完成，无事可做）")
        return 0

    with out.open("a", encoding="utf-8") as fh:
        # 环境头：每次真正开跑的进程一行，自描述产物
        header = {"kind": "header", "caliber": "v3-stream", "run_id": run_id,
                  "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "machine": platform.platform(),
                  "mlx": _pkg_version("mlx"), "mlx_vlm": _pkg_version("mlx-vlm"),
                  "max_tokens": MAX_TOKENS, "filler_per_turn": FILLER_PER_TURN,
                  "arms": [{"repo": r, "mode": m, "draft": d, "block_size": b,
                            "revision": (model_options(r) or {}).get("revision")}
                           for r, m, d, b in ARMS]}
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        fh.flush()
        failures = []
        for repo, mode, draft_dir, block_size in pending:
            print(f"== 臂 {Path(repo).name} × {mode}"
                  + (f"（draft={draft_dir} b{block_size}）" if draft_dir else ""),
                  flush=True)
            try:
                run_arm(repo, mode, draft_dir, block_size, fh, run_id)
            except Exception as e:           # 单臂失败不终止整场（评审 M9）
                failures.append((repo, mode, f"{type(e).__name__}: {e}"))
                fh.write(json.dumps({"kind": "arm_failed", "run_id": run_id,
                                     "model": repo, "mode": mode,
                                     "block_size": block_size,
                                     "error": f"{type(e).__name__}: {e}"},
                                    ensure_ascii=False) + "\n")
                fh.flush()               # 残行留档有标记，分析侧可辨（评审 M11）
                print(f"❌ 臂 {Path(repo).name}×{mode} 失败：{e}", flush=True)
    if failures:
        print(f"A8_DONE_WITH_FAILURES: {failures}")
        return 1
    print("A8_ALL_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
