#!/usr/bin/env python3
"""MTP 单轮开销分解探针：把「非带宽的那 46%」拆开。

背景：31b-4bit + bf16 草稿实测 400 tok 用 10.9s，而按权重带宽算理论只需 5.9s
（目标验证前向 31.3ms + 草稿前向×3 5.1ms + 草稿嵌入表×3 3.0ms = 39.4ms/轮 × 150 轮）。
差出来的 5.0s（46%）不知去向。已排除两个嫌疑：逐位串行走查与处理器
（全关处理器只回收 1.3%）。本探针给出逐阶段归属。

方法：给一轮里的各阶段函数套计时壳，**每段末尾强制 mx.eval** 把惰性图的 GPU 时间
归到该段。代价是消掉了段间重叠，故**插桩后的总时长必然大于裸跑**——脚本同时报裸跑
总时长，两者之差即被消掉的重叠量，读数时必须带着这个偏差看。

阶段（对齐 mlx_vlm/speculative/mtp.py `_mtp_rounds` 的循环体）：
  next_block_size → draft_block（草稿 γ 次前向）→ verify_target（目标验证前向）
  → acceptance_walk（投影+处理器+采样）→ accept_verified（**接受后额外的草稿件调用**）
  → draft_hidden

用法：python3 scripts/profile_mtp_round.py [目标件] [草稿件] [block_total]
单 GPU 纪律：跑之前确认没有别的真模型进程/推理服务在跑。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TARGET = sys.argv[1] if len(sys.argv) > 1 else "models/gemma-4-31b-it-4bit"
DRAFT = sys.argv[2] if len(sys.argv) > 2 else "models/gemma-4-31b-it-assistant-bf16"
# 第三参 = block_total（= γ+1）。不给则不下发，落到草稿件 config 缺省 4（即 γ=3）。
# 实测最优是 5（MLX `qmv_wide` 的 5 向量瓦片边界，见 MTP_PROJECT.md §1.4），
# 故要 profile 最优档必须显式传 5。
BLOCK = int(sys.argv[3]) if len(sys.argv) > 3 else 0
if BLOCK == 1 or BLOCK < 0:
    raise SystemExit("block_total 必须 ≥2（或省略）：=1 时 MTP 轮循环在首个 token 后"
                     "即退出，会得到一份只有一轮的 profile 且不报错。")
MAX_TOKENS = 400

STATS: dict = {}


def _record(name: str, dt: float) -> None:
    s = STATS.setdefault(name, [0.0, 0])
    s[0] += dt
    s[1] += 1


def _force(obj):
    """尽量物化返回值里的惰性数组，把 GPU 时间归到当前段。"""
    import mlx.core as mx
    try:
        if obj is None:
            return
        if isinstance(obj, mx.array):
            mx.eval(obj)
        elif isinstance(obj, (tuple, list)):
            arrs = [o for o in obj if isinstance(o, mx.array)]
            if arrs:
                mx.eval(*arrs)
        else:  # _MTPVerifyResult 之类
            arrs = [getattr(obj, a) for a in ("hidden", "target_tokens")
                    if isinstance(getattr(obj, a, None), mx.array)]
            if arrs:
                mx.eval(*arrs)
    except Exception:
        pass


def timed(name: str, fn):
    def wrap(*a, **k):
        t0 = time.perf_counter()
        r = fn(*a, **k)
        _force(r)
        _record(name, time.perf_counter() - t0)
        return r
    return wrap


def install_probes(draft_model):
    from mlx_vlm.speculative import mtp as M

    M._mtp_next_block_size = timed("next_block_size", M._mtp_next_block_size)
    M._mtp_verify_target = timed("verify_target", M._mtp_verify_target)
    M._mtp_acceptance_walk = timed("acceptance_walk", M._mtp_acceptance_walk)
    M._mtp_draft_hidden = timed("draft_hidden", M._mtp_draft_hidden)
    # 实例方法：以第一类函数身份被传进 sampler_rng，故打在实例上有效
    draft_model.draft_block = timed("draft_block", draft_model.draft_block)
    av = getattr(draft_model, "accept_verified_tokens", None)
    if callable(av):
        draft_model.accept_verified_tokens = timed("accept_verified", av)


def _build_prompt(target_dir: str) -> str:
    from types import SimpleNamespace
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class
    tok = AutoTokenizer.from_pretrained(target_dir)
    d = get_dialect_class("gemma4").create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True})
    return d.build_prompt(messages=[{"role": "user", "content":
        "详细说明光合作用的完整过程，分光反应与暗反应两部分展开，每部分至少三点。"}],
        include_env=False)


async def main():
    from framework.backends.mlx.backend import MLXBackend

    spec = {"mode": "drafter", "draft_model": DRAFT}
    if BLOCK:
        spec["draft_block_size"] = BLOCK
    sampling = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
                "repetition_penalty": 1.1, "repetition_context_size": 50}
    prompt = _build_prompt(TARGET)

    def run(be):
        c = be.new_cache()
        last, t0 = None, time.perf_counter()
        for ch in be.stream_generate(prompt, cache=c, sampling=sampling):
            last = ch
        return last, time.perf_counter() - t0

    be = await MLXBackend.load(TARGET, speculative=spec)
    # 把块大小与是否走了旁路打进表头：传 BLOCK 会同时开 prefer_requested_block_size，
    # 与缺省路（保留上游自适应天花板）**不是同一个配置**，读数时必须能分辨。
    print(f"配置：block_total={BLOCK or '缺省(4)'}  "
          f"旁路={getattr(be.draft_model, 'prefer_requested_block_size', False)}  "
          f"目标={TARGET}  草稿={DRAFT}")

    print("=== 裸跑（无插桩，作为重叠量基准）===")
    last, bare = run(be)
    ex = last.extra or {}
    drafted, accepted = ex.get("spec_drafted"), ex.get("spec_accepted")
    print(f"  {last.generation_tokens} tok / {bare:.2f}s = {last.generation_tps:.1f} tps"
          f"  接受率 {accepted}/{drafted}")

    install_probes(be.draft_model)
    STATS.clear()

    print("\n=== 插桩跑（段末强制 eval，重叠被消掉）===")
    last2, inst = run(be)
    print(f"  {last2.generation_tokens} tok / {inst:.2f}s = {last2.generation_tps:.1f} tps")
    print(f"  插桩代价（重叠被消掉的部分）= {inst - bare:+.2f}s\n")

    rounds = max(1, STATS.get("verify_target", [0, 1])[1])
    total_phase = sum(v[0] for v in STATS.values())
    print(f"{'阶段':<18}{'累计 s':>9}{'占插桩总时':>10}{'调用次':>8}{'每轮 ms':>10}")
    print("-" * 57)
    for name, (dt, n) in sorted(STATS.items(), key=lambda kv: -kv[1][0]):
        print(f"{name:<18}{dt:>9.2f}{dt / inst:>10.1%}{n:>8}{dt / rounds * 1000:>10.2f}")
    print("-" * 57)
    print(f"{'阶段合计':<18}{total_phase:>9.2f}{total_phase / inst:>10.1%}")
    print(f"{'循环外/未归属':<18}{inst - total_phase:>9.2f}{(inst - total_phase) / inst:>10.1%}"
          f"   ← 生成器协议/detokenizer/yield/缓存维护等")
    print(f"\n轮数 {rounds}，每轮产出 {last2.generation_tokens / rounds:.2f} tok，"
          f"每轮插桩耗时 {inst / rounds * 1000:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
