"""有效上下文拐点测量（W0）：longchain 题前注入可控长度的真实形态历史，看成功率随 prompt 规模的走势。

  backend/.venv/bin/python benchmarks/context_knee/run_knee.py --dry-run --levels 0,4000 --reps 2 --limit 2
  INFER_REMOTE_HOST=<算力端> INFER_API_KEY=… backend/.venv/bin/python benchmarks/context_knee/run_knee.py \\
      --model RedHatAI/Qwen3.8-27B-INT4 --backend remote --tasks hard

复用 backend/benchmarks/model_eval（run_item 执行 + longchain 打分，源码不改）。每个
(level × 题 × rep)：新建会话 → 首次生成前把填充历史铺进 system 与任务之间 → 照 run_item
跑题 → longchain 评分 + 精确回忆评分。产物：<out>/transcripts.jsonl（每 run 一行，
增量追加）+ summary.json；表格用 summarize.py。用法细节见 README.md。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
for _p in (BACKEND, BACKEND / "benchmarks"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))       # 末位：只为 import context_knee

from benchmarks.model_eval.executor import DEFAULT_SYSTEM_PROMPT, run_item  # noqa: E402
from benchmarks.model_eval.itemlib import load_items, task_fingerprint      # noqa: E402
from benchmarks.model_eval.mock_envs import make_env                       # noqa: E402
from benchmarks.model_eval.scorers import get_scorer                       # noqa: E402
from context_knee.filler import Filler, build_filler, recall_suffix, score_recall  # noqa: E402
from context_knee.quality import final_text_quality                        # noqa: E402

ITEMS_DIR = BACKEND / "benchmarks" / "model_eval" / "items" / "longchain"
FAKE_CHARS_PER_TOKEN = 3.5
#: 0 = 自然基线（无填充、无回忆句）；4096 = 带回忆句的最小填充基线（与高档同口径）；
#: 57344 顶档给远端 max_model_len 65536 留生成余量
DEFAULT_LEVELS = "0,4096,8192,16384,32768,49152,57344"


# ---------------- 采样覆盖 ----------------

#: CUDA 后端映射进 vLLM SamplingParams 的键（cuda/backend.py:489-500；top_p/top_k/presence/
#: frequency 为 2026-10-31 加入的可选透传，远端代码须为该版本）；其余键远端不认
SAMPLING_KEYS = {"temperature": float, "min_p": float, "max_tokens": int,
                 "repetition_penalty": float, "top_p": float, "top_k": int,
                 "presence_penalty": float, "frequency_penalty": float}
#: 同时是 generate_once 形参的键：在引擎入口覆盖（engine 的截断判定等用的是同一个值）
_GEN_KWARGS = ("temperature", "min_p", "max_tokens")


def parse_sampling(spec: str) -> Dict[str, Any]:
    """`k=v[,k=v...]` → {"sent": {...}, "ignored": {...}}。sent = 本端已发送给后端的键值——
    远端是否真的用上没有回执核验（服务端不回显 SamplingParams）；非映射键只警告、记录、不发送。"""
    applied: Dict[str, Any] = {}
    ignored: Dict[str, str] = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise SystemExit(f"❌ --sampling 项须为 k=v：{part!r}")
        k, v = (x.strip() for x in part.split("=", 1))
        if k in SAMPLING_KEYS:
            try:
                applied[k] = SAMPLING_KEYS[k](v)
            except ValueError:
                raise SystemExit(f"❌ --sampling {k}={v!r} 不是数值")
        else:
            print(f"⚠️ --sampling {k}={v}：远端未映射，忽略")
            ignored[k] = v
    return {"sent": applied, "ignored": ignored}


def install_sampling_override(backend, overrides: Dict[str, Any]) -> bool:
    """在后端实例上包一层 stream_generate（RemoteBackend.stream_generate 收 `sampling`
    字典，remote/backend.py:336-341）：把覆盖值 merge 进 sampling（覆盖优先）再转发。
    引擎按注册表条目 sampling 解析采样（W-1），这里的 --sampling 覆盖层仍优先。幂等。"""
    if not overrides or getattr(backend, "_knee_sampling", None) is not None:
        return False
    orig = backend.stream_generate

    def stream_generate(prompt, *args, sampling=None, **kw):
        return orig(prompt, *args, sampling={**(sampling or {}), **overrides}, **kw)
    backend.stream_generate = stream_generate
    backend._knee_sampling = dict(overrides)
    return True


# ---------------- 计量 ----------------

class _DummyTok:
    """fake 模式的方言装配替身（方言只在构造期碰 tokenizer；计数走 chars/3.5）。"""
    special_tokens_map: Dict[str, Any] = {}

    def encode(self, s, add_special_tokens=False):
        return [1]


class _DummyProc:
    tokenizer = _DummyTok()


@dataclass
class PromptCounter:
    """按引擎同款组装渲染整段 prompt 再计 token。

    组装参数照 engine.generate_once 首次生成（全量路）：task_block/tail_notes 为空，
    include_env = ChatSession.inject_env_context（缺省 True），env_extra = 引擎的笔记索引。
    """
    dialect: Any
    encode_len: Callable[[str], int]
    env_extra: Optional[List[str]] = None
    mode: str = "engine"            # engine = 引擎分词器；chars/3.5 = fake 估算

    def render(self, messages: List[Dict[str, Any]], tools, enable_thinking: bool) -> str:
        return self.dialect.build_prompt(
            messages=messages, tools=tools, enable_thinking=enable_thinking,
            task_block=None, tail_notes=None, include_env=True, env_extra=self.env_extra)

    def count(self, messages: List[Dict[str, Any]], tools, enable_thinking: bool) -> int:
        return self.encode_len(self.render(messages, tools, enable_thinking))


def engine_counter(engine) -> PromptCounter:
    tok = engine.processor.tokenizer
    notes = getattr(engine, "_notes_index", None)
    return PromptCounter(dialect=engine.dialect,
                         encode_len=lambda s: len(tok.encode(s, add_special_tokens=False)),
                         env_extra=notes() if callable(notes) else None, mode="engine")


def fake_counter(model_options: Optional[Dict[str, Any]] = None) -> PromptCounter:
    from framework.dialects import get_dialect_class
    opts = dict(model_options or {})
    dialect = get_dialect_class(str(opts.get("dialect") or "")).create(
        _DummyProc(), options=opts, env={})
    return PromptCounter(dialect=dialect,
                         encode_len=lambda s: math.ceil(len(s) / FAKE_CHARS_PER_TOKEN),
                         mode=f"chars/{FAKE_CHARS_PER_TOKEN}")


# ---------------- 计划 ----------------

@dataclass
class RunPlan:
    item: Dict[str, Any]            # 本 run 实际跑的题（task.user 已追加回忆要求）
    level: int
    rep: int
    filler: Filler
    base_tokens: int                # 无填充时首次生成的 prompt token（同一追加后终问）
    prompt_tokens_est: int
    tools: List[Dict[str, Any]]
    min_fill_tokens: int = 0        # 最少 3 个伪轮次的代价（目标再小也得这么多）
    warning: Optional[str] = None


def item_tools(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    task = item["task"]
    env = make_env(task["env"], task.get("env_init")) if task.get("env") else None
    return (env.tools() if env is not None else None) or task.get("tools") or []


def plan_run(counter: PromptCounter, item: Dict[str, Any], level: int, rep: int,
             seed: int, item_idx: int = 0) -> RunPlan:
    run_item_obj = copy.deepcopy(item)
    task = run_item_obj["task"]
    if level > 0:
        task["user"] = task["user"] + recall_suffix(item.get("lang", "en"))
    tools = item_tools(item)
    et = bool((item.get("gen") or {}).get("enable_thinking", True))
    system = {"role": "system", "content": task.get("system") or DEFAULT_SYSTEM_PROMPT}
    user = {"role": "user", "content": task["user"]}
    base = counter.count([system, user], tools, et)

    def turn_cost(msgs: List[Dict[str, Any]]) -> int:
        return counter.count([system, *msgs, user], tools, et) - base

    filler = build_filler(max(level - base, 1) if level > 0 else 0, seed=seed, rep=rep,
                          tool_names=[t["name"] for t in tools], turn_cost=turn_cost,
                          task_key=task_fingerprint(item), rotation=(item_idx + rep) % 3)
    est = counter.count([system, *filler.messages, user], tools, et)
    min_fill = sum(filler.turn_costs[:3])
    warning = None
    if level > 0 and level < base + min_fill:
        warning = (f"level {level} < 基础 prompt {base} + 最小填充 {min_fill}：实际 prompt "
                   f"≈{est}，本档对 {item['id']} 不达目标口径")
    return RunPlan(item=run_item_obj, level=level, rep=rep, filler=filler,
                   base_tokens=base, prompt_tokens_est=est, tools=tools,
                   min_fill_tokens=min_fill, warning=warning)


# ---------------- 引擎代理 ----------------

class KneeEngine:
    """包住真引擎（或 FakeEngine）的薄代理：
    ① 首次生成前把填充铺进 session.history（system 之后、题面消息之前）；
    ② 逐次记录 generate_once 的返回仪表与 repetition_guard（executor 不记这个字段）。
    其余行为原样透传——run_item 只用 generate_once / backend / dialect。"""

    def __init__(self, inner, prefill: List[Dict[str, Any]],
                 sampling_overrides: Optional[Dict[str, Any]] = None):
        self._inner = inner
        self._gen_overrides = {k: v for k, v in (sampling_overrides or {}).items()
                               if k in _GEN_KWARGS}
        self.backend = inner.backend
        self.dialect = getattr(inner, "dialect", None)
        self._prefill = prefill
        self._injected = False
        self.session = None             # run 结束时据此释放后端缓存
        self.gens: List[Dict[str, Any]] = []

    def release(self) -> None:
        """释放本 run 会话的后端缓存（远端=服务端缓存条目，MLX=KV）；后端无此方法则跳过。"""
        rel = getattr(self.backend, "release_cache", None)
        handle = getattr(self.session, "brain_memory_state", None)
        if callable(rel) and handle is not None:
            rel(handle)
            self.session.brain_memory_state = None

    async def generate_once(self, session, **kwargs):
        self.session = session
        if not self._injected:
            hist = session.history
            if not hist or hist[0].get("role") != "system" or hist[-1].get("role") != "user":
                raise RuntimeError(f"首次生成前会话形态意外：{[m.get('role') for m in hist]}")
            hist[1:1] = copy.deepcopy(self._prefill)
            self._injected = True
        kwargs.update(self._gen_overrides)
        t0 = time.monotonic()
        r = await self._inner.generate_once(session, **kwargs)
        m = r.get("metrics") or {}
        self.gens.append({
            "i": len(self.gens),
            "wall_s": round(time.monotonic() - t0, 3),
            "prompt_tokens": m.get("prompt_tokens"),
            "reused_prefix": m.get("reused_prefix"),
            "prompt_tps": m.get("prompt_tps"),
            "generation_tps": m.get("generation_tps"),
            "generation_tokens": m.get("generation_tokens"),
            "cancelled": bool(r.get("cancelled")),
            "truncated": bool(r.get("truncated")),
            "repetition_guard": r.get("repetition_guard"),
        })
        return r


def score_run(plan: RunPlan, traj: Dict[str, Any], gens: List[Dict[str, Any]],
              original_item: Dict[str, Any]) -> Dict[str, Any]:
    """longchain 原样打分（终态断言不变）+ recall_* + 复读护栏命中。"""
    score = get_scorer(original_item["dimension"])(traj, original_item)
    final_text = traj["turns"][-1]["text"] if traj.get("turns") else ""
    if plan.filler.cards:
        score.update(score_recall(final_text, plan.filler.cards))
    else:
        score.update({"recall_ok": None, "recall_hits": None, "recall_by_slot": None})
    score["repetition_hits"] = sum(1 for g in gens if g.get("repetition_guard"))
    return score


def build_record(plan: RunPlan, traj: Dict[str, Any], gens: List[Dict[str, Any]],
                 score: Dict[str, Any], original_item: Dict[str, Any], meta: Dict[str, Any],
                 wall_s: float) -> Dict[str, Any]:
    first = gens[0] if gens else {}
    actual = first.get("prompt_tokens")
    fill = {
        "target_tokens": plan.level,
        "base_tokens": plan.base_tokens,
        "filler_tokens_est": plan.filler.est_tokens,
        "prompt_tokens_est": plan.prompt_tokens_est,
        "prompt_tokens_actual": actual,
        "fill_error": (actual - plan.level) if (plan.level > 0 and actual) else None,
        "est_error": (actual - plan.prompt_tokens_est) if actual else None,
        "min_fill_tokens": plan.min_fill_tokens,
        "warning": plan.warning,
        "n_pseudo_turns": len(plan.filler.turns),
        "n_messages": len(plan.filler.messages),
        "cards": plan.filler.cards,
        "fingerprint": plan.filler.fingerprint() if plan.filler.messages else None,
    }
    return {**meta, "level": plan.level, "rep": plan.rep, "item_id": original_item["id"],
            "card_kinds": {c["slot"]: c["kind"] for c in plan.filler.cards},
            "dimension": original_item["dimension"], "lang": original_item.get("lang"),
            "task_fp": task_fingerprint(original_item),
            "recall_suffix": plan.item["task"]["user"][len(original_item["task"]["user"]):],
            "fill": fill, "score": score, "gens": gens,
            "repetition_events": [{"gen": g["i"], **g["repetition_guard"]}
                                  for g in gens if g.get("repetition_guard")],
            "wall_s": round(wall_s, 2),
            "final_text_quality": final_text_quality(
                traj["turns"][-1]["text"] if traj.get("turns") else "", original_item.get("lang")),
            "turns": traj.get("turns"), "aborted_reason": traj.get("aborted_reason"),
            "env_state": traj.get("env_state")}


# ---------------- dry-run 脚本 ----------------

def dry_script(plan: RunPlan) -> List[Dict[str, Any]]:
    """FakeEngine 脚本：一次只读调用 → 收尾正文（逐字写出卡值；奇数 rep 故意漏 back 卡，
    让 recall 汇总在干跑里也有非平凡数）。仪表 prompt_tokens 取计划估算值。"""
    names = {t["name"] for t in plan.tools}
    call = ({"type": "function", "name": "list_files", "arguments": {}} if "list_files" in names
            else {"type": "function", "name": "todo_list", "arguments": {}})
    vals = [c["value"] for c in plan.filler.cards
            if not (plan.rep % 2 == 1 and c["slot"] == "back")]
    est = plan.prompt_tokens_est
    return [
        {"thoughts": "dry-run", "tool_calls": [call],
         "metrics": {"prompt_tokens": est, "generation_tokens": 24, "prompt_tps": 1000.0,
                     "generation_tps": 50.0}},
        {"text": "dry-run 收尾。" + (" 值：" + "、".join(vals) if vals else ""),
         "metrics": {"prompt_tokens": est + 200, "reused_prefix": est, "generation_tokens": 30,
                     "prompt_tps": 1000.0, "generation_tps": 50.0}},
    ]


# ---------------- 主流程 ----------------

def parse_levels(s: str) -> List[int]:
    vals = sorted({int(x) for x in s.split(",") if x.strip()})
    if not vals or vals[0] < 0:
        raise SystemExit(f"❌ --levels 须为非负整数列表：{s!r}")
    return vals


def select_items(args) -> List[Dict[str, Any]]:
    items = load_items([ITEMS_DIR / f"{args.tasks}.jsonl"])
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
        missing = wanted - {it["id"] for it in items}
        if missing:
            raise SystemExit(f"❌ 题库中不存在: {sorted(missing)}")
        items = [it for it in items if it["id"] in wanted]
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise SystemExit("❌ 过滤后题目为空")
    return items


async def make_engine(args, repo: str, opts: Dict[str, Any]):
    """真引擎装配。remote：只拉分词器（app.py:613 同款 resolve_tokenizer_only_path，不下权重）
    + 身份护栏（engine_pool.py 同款 expected_model）；mlx：run_eval 同款全量解析。"""
    from framework.core.engine import AgentEngine
    from framework.utils.config_loader import load_section
    inference_cfg = dict(load_section("inference"))
    if args.backend == "remote":
        host = str((inference_cfg.get("remote") or {}).get("host") or "").strip()
        if not host:
            raise SystemExit("❌ remote 需要算力端地址：设环境变量 INFER_REMOTE_HOST（config 里为空）")
        if not os.environ.get("INFER_API_KEY"):
            print("⚠️ 未设 INFER_API_KEY：算力端若要求鉴权会 401")
        from framework.utils.path_resolver import resolve_tokenizer_only_path
        model_path = resolve_tokenizer_only_path(repo, revision=opts.get("revision"))
        kv = {**inference_cfg,
              "expected_model": {"repo": repo, "revision": opts.get("revision")}}
    else:
        from framework.utils.path_resolver import resolve_model_path
        model_path = resolve_model_path(repo, revision=opts.get("revision"))
        kv = inference_cfg
    return await AgentEngine.create(model_path, kv_config=kv, model_options=opts, env={},
                                    backend=args.backend)


def _seed_sampler(backend: str, sampling_seed: int) -> bool:
    """进程内 MLX 采样走 mx.random 全局态：每 run 前播种。remote/cuda 的采样在服务端，
    generate_once 无 seed 参数——记 False（不可控），不假装播了。"""
    if backend != "mlx":
        return False
    import mlx.core as mx
    mx.random.seed(sampling_seed)
    return True


async def amain(args) -> int:
    from framework.utils.model_selection import model_options
    levels = parse_levels(args.levels)
    items = select_items(args)
    if args.dry_run:
        args.backend = "fake"
    if args.backend not in ("remote", "mlx", "fake"):
        raise SystemExit(f"❌ --backend 须为 remote|mlx|fake：{args.backend!r}")

    repo, opts = "dry-run/fake", {}
    if args.model:
        from benchmarks.model_eval.run_eval import resolve_repo
        repo = resolve_repo(args.model)
        opts = model_options(repo)
    elif args.backend != "fake":
        raise SystemExit("❌ 真后端须给 --model")

    sampling = parse_sampling(args.sampling)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.out) if args.out else HERE / "results" / Path(repo).name / stamp
    transcripts = outdir / "transcripts.jsonl"
    if args.resume:
        if not args.out:
            raise SystemExit("❌ --resume 须配 --out <原产物目录>")
        args.append = True
    done = load_done(transcripts) if args.resume else set()
    # 注册表条目的采样键（W-1 起引擎把它并入实际发送的 sampling）：入 meta、参与合并口径
    entry_sampling = dict(opts.get("sampling") or {})
    if args.append:     # --resume 已隐含 --append
        check_resume_compat(transcripts, repo, sampling["sent"], args.seed, entry_sampling,
                            entry_only=not args.resume)
    if transcripts.exists() and not args.append:
        raise SystemExit(f"❌ {transcripts} 已存在：换 --out，或显式 --append 追加"
                         f"（或 --resume 续跑缺的 run）")
    outdir.mkdir(parents=True, exist_ok=True)

    if args.backend == "fake":
        engine = None
        counter = fake_counter(opts)
    else:
        engine = await make_engine(args, repo, opts)
        try:
            install_sampling_override(engine.backend, sampling["sent"])
            counter = engine_counter(engine)
        except BaseException:
            engine.backend.shutdown()
            raise
    meta = {"model": repo, "revision": opts.get("revision"),
            "dialect": opts.get("dialect") or "gemma4", "backend": args.backend,
            "tasks": args.tasks, "seed": args.seed, "token_counter": counter.mode,
            "stamp": stamp, "sampling_overrides": sampling, "entry_sampling": entry_sampling}
    print(f"== 拐点测量 {repo}（{args.backend}，计数 {counter.mode}）：{len(items)} 题 × "
          f"{len(levels)} 档 {levels} × {args.reps} rep → {outdir}")

    records: List[Dict[str, Any]] = []
    if args.resume:
        print(f"== resume：跳过 {len(done)} 个已完成 run（error 行不算完成，重跑）")
    _INTERRUPT.reset()
    loop = asyncio.get_running_loop()
    installed = _install_interrupt(loop, asyncio.current_task())
    try:
        with transcripts.open("a", encoding="utf-8") as fh:
            for level in levels:
                for item_idx, item in enumerate(items):
                    for rep in range(args.reps):
                        if (level, item["id"], rep) in done:
                            continue
                        rec = await _one_run(args, engine, counter, item, item_idx, level, rep, meta)
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        fh.flush()
                        records.append(rec)
                        _print_run(rec)
    except asyncio.CancelledError:
        if _INTERRUPT.signum is None:
            raise
        asyncio.current_task().uncancel()   # 信号引起的取消到此为止：收尾里的 await 照常
    finally:
        # 收尾期（排空 / 写 summary / 同步的 backend.shutdown）不回默认处理器：默认下第二次
        # Ctrl-C 会以 KeyboardInterrupt 乱入收尾、第二次 SIGTERM 直接被杀（-15）。改成
        # 「立即 _hard_exit(128+signum)」，main 退出前才恢复默认（见 _restore_default_signals）
        _arm_hard_exit_signals(loop, installed)
        if _INTERRUPT.signum is not None and _INTERRUPT.inner is not None:
            await drain_inference(_INTERRUPT.inner, INTERRUPT_DRAIN_S)
        try:
            from context_knee.summarize import aggregate, dedupe, load_records
            if args.append and transcripts.exists():
                records = load_records(outdir)      # summary 按整个 transcripts 聚合
            uniq = dedupe(records)                  # 同 (level,题,rep) 取最新非 error
            summary = {**meta, "levels": levels, "reps": args.reps,
                       "items": [it["id"] for it in items], "n_rows": len(records),
                       "n_runs": len(uniq), "n_errors": sum(1 for r in uniq if "error" in r),
                       "complete": {(lv, it["id"], r) for lv in levels for it in items
                                    for r in range(args.reps)} <= load_done(transcripts),
                       "by_level": aggregate(uniq)}
            (outdir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"== 产物: {outdir}（表格：summarize.py {outdir}）")
        finally:
            if engine is not None:
                try:
                    engine.backend.shutdown()
                except Exception as e:
                    print(f"⚠️ 后端关闭异常: {e}")
    return _INTERRUPT.code if _INTERRUPT.signum is not None else 0


# ---------------- 可中断（W-1/C） ----------------

#: 收到信号后的收尾上限：超过即 os._exit（收尾本身卡住——如释放缓存的 HTTP——也要能退）。
#: 构成：proxy.release 的 DELETE 超时 5 s + 排空 INTERRUPT_DRAIN_S 2 s + 写 summary / 关后端
#: 的余量 ≈ 5 s。原 4 s 比单个 DELETE 超时还短，正常收尾也会被看门狗截断、summary 写不出。
INTERRUPT_GRACE_S = 12.0
#: 收尾里给推理线程的排空等待：解码中的线程见 cancel_requested 会在下一 chunk 停并
#: POST cancel；卡在 SSE 读（预填期无 chunk）的线程等不到，交给进程退出
INTERRUPT_DRAIN_S = 2.0


class _Interrupt:
    """SIGINT/SIGTERM 的进程级状态：当前 run 的代理与引擎（信号处理器据此置取消）。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.signum: Optional[int] = None
        self.armed: List[int] = []      # 收尾期挂了「立即退出」处理器的信号（main 退出前恢复默认）
        self.proxy = None
        self.inner = None

    @property
    def code(self) -> int:
        return 128 + int(self.signum or 0)


_INTERRUPT = _Interrupt()


def _hard_exit(code: int) -> None:
    """最后兜底的 os._exit：推理执行器线程是非守护线程，卡在远端 SSE 的 iter_lines 时
    （读超时 300 s）解释器退出要 join 它——进程就"SIGTERM 不死"。transcripts 每 run
    已 flush、summary 在收尾里写完，这里只冲一下标准流。进程退出由内核关掉 socket，
    服务端据断连走止损路径（inference/server.py _cancel_gen）。
    不从别的线程 resp.close()：响应对象是 RemoteBackend 生成器的局部变量，且 POSIX 上
    跨线程 close 一个正在 recv 的 socket 并不保证唤醒那次 recv（只有 shutdown 保证）。"""
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            pass
    os._exit(code)


def _install_interrupt(loop, main_task) -> List[int]:
    """首个信号：置当前会话 cancel_requested、起收尾看门狗、取消主任务（amain 收尾写
    summary、排空、关后端后返回 128+signum，main 再 _hard_exit）。第二个信号：立即退出。"""
    def _on_signal(signum: int) -> None:
        if _INTERRUPT.signum is not None:
            print(f"== 再次收到信号 {signum}：立即退出", flush=True)
            _hard_exit(128 + signum)
        _INTERRUPT.signum = signum
        print(f"== 收到信号 {signal.Signals(signum).name}：取消当前 run，写 summary 后退出"
              f"（{INTERRUPT_GRACE_S:.0f}s 内未收尾则强制退出）", flush=True)
        sess = getattr(_INTERRUPT.proxy, "session", None)
        if sess is not None:
            sess.cancel_requested = True
        dog = threading.Timer(INTERRUPT_GRACE_S, _hard_exit, args=(128 + signum,))
        dog.daemon = True
        dog.start()
        main_task.cancel()

    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass                     # 非主线程 / 平台不支持：维持默认行为
    return installed


def _exit_now_handler(signum: int, _frame) -> None:
    print(f"== 收尾中收到信号 {signal.Signals(signum).name}：立即退出", flush=True)
    _hard_exit(128 + signum)


def _arm_hard_exit_signals(loop, sigs: List[int]) -> None:
    """把 loop 级信号处理器换成进程级「立即 _hard_exit(128+signum)」处理器。

    用 signal.signal 而不是 loop.add_signal_handler：收尾里的 backend.shutdown() 是同步调用，
    卡在 HTTP 上时事件循环转不动，loop 级处理器永远轮不到；进程级处理器在主线程的系统调用
    被 EINTR 打断时即执行。loop.remove_signal_handler 会先把处理器回成默认，换手期间屏蔽
    这两个信号，期间到达的信号在解除屏蔽后交给新处理器，不会落到默认处理器上。"""
    if not sigs:
        return
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
    try:
        for sig in sigs:
            loop.remove_signal_handler(sig)
            signal.signal(sig, _exit_now_handler)
            _INTERRUPT.armed.append(sig)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def _restore_default_signals() -> None:
    """main 退出前：收尾期的「立即退出」处理器还原为默认（与 loop.remove_signal_handler 同口径）。"""
    for sig in _INTERRUPT.armed:
        if signal.getsignal(sig) is _exit_now_handler:
            signal.signal(sig, signal.default_int_handler if sig == signal.SIGINT
                          else signal.SIG_DFL)
    _INTERRUPT.armed = []


async def drain_inference(engine, timeout: float) -> Optional[bool]:
    """超时后排空推理执行器：往引擎的单线程执行器投一个空任务并等它跑到。被取消的生成
    还占着线程时（远端卡在 SSE 读），这一步等它退出；等不到就警告——后续 run 会排在它
    后面。返回 drain_timeout（True=没排空；None=引擎无执行器，跳过）。"""
    ex = getattr(engine, "_inference_executor", None)
    if ex is None:
        return None
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(ex, lambda: None), timeout=timeout)
        return False
    except asyncio.TimeoutError:
        print(f"⚠️ 推理线程仍占用（排空等待 {timeout:.0f}s 未果），后续 run 可能连环超时")
        return True


async def _one_run(args, engine, counter, item, item_idx, level, rep, meta) -> Dict[str, Any]:
    """单 run；任何异常收成一条 {"error": …} 记录，不中止批次。"""
    sampling_seed = args.seed * 1000 + rep
    proxy = None
    t0 = time.monotonic()
    try:
        plan = plan_run(counter, item, level, rep, args.seed, item_idx)
        if plan.warning:
            print(f"⚠️ {plan.warning}")
        applied = _seed_sampler(args.backend, sampling_seed)
        if args.backend == "fake":
            from tests.fake_engine import FakeEngine
            inner = FakeEngine(dry_script(plan))
        else:
            inner = engine
        proxy = KneeEngine(inner, plan.filler.messages,
                           (meta.get("sampling_overrides") or {}).get("sent"))
        _INTERRUPT.proxy, _INTERRUPT.inner = proxy, inner
        timeout = getattr(args, "run_timeout", 0) or 0
        coro = run_item(proxy, plan.item, repeat_idx=rep)
        try:
            traj = await (asyncio.wait_for(coro, timeout) if timeout > 0 else coro)
        except asyncio.TimeoutError:
            # wait_for 已取消 run_item 协程；推理线程里的同步生成循环只认 cancel_requested
            # （engine.py:542-552：命中即 break → stream.close() → 远端 POST cancel）
            if proxy.session is not None:
                proxy.session.cancel_requested = True
            elapsed = round(time.monotonic() - t0, 2)
            print(f"⏱ L{level} {item['id']} r{rep} timeout（{elapsed}s > {timeout}s）")
            drain_timeout = await drain_inference(inner, min(60.0, timeout))
            try:
                proxy.release()
            except Exception as e:
                print(f"⚠️ 释放会话缓存失败: {e}")
            return {**meta, "level": level, "rep": rep, "item_id": item["id"],
                    "error": "timeout", "elapsed_s": elapsed, "run_timeout_s": timeout,
                    "drain_timeout": drain_timeout,
                    "drain_s": round(time.monotonic() - t0 - elapsed, 2),
                    "gens": proxy.gens, "wall_s": round(time.monotonic() - t0, 2)}
        score = score_run(plan, traj, proxy.gens, item)
        return build_record(plan, traj, proxy.gens, score, item,
                            {**meta, "sampling_seed": sampling_seed,
                             "sampling_seed_applied": applied}, time.monotonic() - t0)
    except Exception as e:      # noqa: BLE001 —— 批次纪律：单 run 失败只记账
        import traceback
        return {**meta, "level": level, "rep": rep, "item_id": item["id"],
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-2000:],
                "gens": proxy.gens if proxy is not None else [],
                "wall_s": round(time.monotonic() - t0, 2)}
    finally:
        if _INTERRUPT.signum is None:
            _INTERRUPT.proxy = _INTERRUPT.inner = None   # 中断时留给 amain 收尾排空
        if proxy is not None:
            try:
                proxy.release()
            except Exception as e:
                print(f"⚠️ 释放会话缓存失败: {e}")


def _sent_of(r: Dict[str, Any]) -> Dict[str, Any]:
    so = r.get("sampling_overrides") or {}
    return so.get("sent", so.get("applied")) or {}      # applied = 改名前的旧键


def check_resume_compat(transcripts: Path, model: str, sent: Dict[str, Any], seed: int,
                        entry: Dict[str, Any], *, entry_only: bool = False) -> None:
    """续跑只能接同口径的批：已有行的 model / 已发送采样 / seed / 注册表条目采样任一不同即拒
    （混进来的 rep 会被 Pass^2 当作同一组）。缺 model/sampling/seed 字段的行（极旧格式）不参与
    这三项比较；缺 entry_sampling 的行（W-1 之前）视为 {}——当时引擎不读条目采样。
    entry_only（单纯 --append）：只比条目采样——summary 按整个 transcripts 聚合，条目口径
    一变，新旧行实际发送的采样就不同，却在 sampling_overrides 上看不出来。"""
    if not transcripts.exists():
        return
    bad = set()
    entry_bad = set()
    for ln in transcripts.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        old_entry = r.get("entry_sampling") or {}
        if old_entry != entry:
            entry_bad.add(f"{json.dumps(old_entry, sort_keys=True)} ≠ {json.dumps(entry, sort_keys=True)}")
        if entry_only:
            continue
        if "model" in r and r["model"] != model:
            bad.add(f"model {r['model']} ≠ {model}")
        if "sampling_overrides" in r and _sent_of(r) != sent:
            bad.add(f"sampling {json.dumps(_sent_of(r), sort_keys=True)} ≠ {json.dumps(sent, sort_keys=True)}")
        if "seed" in r and r["seed"] != seed:
            bad.add(f"seed {r['seed']} ≠ {seed}")
    if entry_bad:
        bad.add("注册表条目采样口径已变（W-1），不能与旧目录合并；另起目录（entry_sampling "
                + "；".join(sorted(entry_bad)) + "）")
    if bad:
        raise SystemExit("❌ --resume/--append 口径不一致，拒绝合并：" + "；".join(sorted(bad)))


def load_done(transcripts: Path) -> set:
    """已成功完成的 (level, item_id, rep)；error 行（含 timeout）不算完成。"""
    done = set()
    if transcripts.exists():
        for ln in transcripts.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            if "error" not in r:
                done.add((int(r["level"]), r["item_id"], int(r["rep"])))
    return done


def _print_run(rec: Dict[str, Any]) -> None:
    head = f"L{rec['level']} {rec['item_id']} r{rec['rep']}"
    if rec.get("error") == "timeout":
        return                                  # ⏱ 行已在 _one_run 打过
    if "error" in rec:
        print(f"💥 {head} 异常（已记录，继续）: {rec['error'][:160]}")
        return
    score, f = rec["score"], rec["fill"]
    mark = "✅" if score.get("pass") else "❌"
    rc = "-" if score["recall_hits"] is None else f"{score['recall_hits']}/3"
    why = "" if score.get("pass") else "  | " + "; ".join(score.get("why") or [])[:100]
    print(f"{mark} {head} prompt={f['prompt_tokens_actual']} (est {f['prompt_tokens_est']}, "
          f"err {f['fill_error']}) recall {rc} rep_guard {score['repetition_hits']} "
          f"({rec['wall_s']}s){why}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="有效上下文拐点测量（longchain + 真实形态填充）")
    ap.add_argument("--model", default="", help="注册表 repo 全名或末段短名（真后端必填）")
    ap.add_argument("--backend", default="remote", help="remote|mlx|fake")
    ap.add_argument("--levels", default=DEFAULT_LEVELS,
                    help="首次生成 prompt 总 token 目标（0 = 不填充的自然基线）")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--tasks", default="hard", choices=["hard", "seed"])
    ap.add_argument("--ids", default="", help="只跑这些题 id（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="", help="产物目录（缺省 results/<模型短名>/<时间戳>）")
    ap.add_argument("--dry-run", action="store_true", help="= --backend fake：FakeEngine 跑完全流程")
    ap.add_argument("--sampling", default="",
                    help="采样覆盖 k=v[,k=v]；生效键 temperature/min_p/max_tokens/repetition_penalty/"
                         "top_p/top_k/presence_penalty/frequency_penalty")
    ap.add_argument("--run-timeout", type=float, default=600.0,
                    help="单 run 墙钟上限秒（0=不限）；超时记 error=timeout 行并继续")
    ap.add_argument("--resume", action="store_true",
                    help="隐含 --append：跳过 --out 里已成功的 (level,题,rep)，error 行重跑")
    ap.add_argument("--append", action="store_true",
                    help="--out 已有 transcripts.jsonl 时追加（缺省拒绝，防两批混算）")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    try:
        rc = asyncio.run(amain(build_parser().parse_args(argv)))
        if _INTERRUPT.signum is not None:
            _hard_exit(rc)      # 被信号中断：不等非守护推理线程（见 _hard_exit）
        return rc
    finally:
        _restore_default_signals()   # 「立即退出」处理器只活到 main 真正退出前


if __name__ == "__main__":
    sys.exit(main())
