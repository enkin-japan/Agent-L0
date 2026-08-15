#!/usr/bin/env python3
"""Design C：26b-8bit 组件级归因——残差到底在不在专家内核里。

# 要回答的唯一问题

生产口径 v(4)=1.461（§1.3-b 08-15 复核；同表 bf16 口径 1.465，判据带按两者算）,
纯字节下限 1.345（修正后，§1.8）。高出的 ~8% 在哪：专家内核的逐对重读（缓存没
吃掉的部分）？还是排序/路由/非专家组件随 M 的增长被记进了 v(M)？判据
（`prediction_designC.md` 先行，v1.1）：

    Experts 的 t(L=4)/t(L=1) ≳ 2.15（校正后） ⇒ 残差在专家内核（S2 值得跑）
                              ≲ 1.95 且校正占比小 ⇒ 缓存已回收满（内核线止）

# 方法（为什么这样设计；§4.20 三坑与两轮评审的修正都在内）

- **真模型、真路由、真 KV、真生成循环**——不是微基准，无缓存假象、无解析式外推；
- 逐组件包一层 `perf_counter + mx.eval(输出)`，按 **(run, 组件, 输入序列长 L)**
  三元分桶。**必须带 run**：草稿件复用 gemma4 的 DecoderLayer（首过评审 Critical），
  MTP 跑里草稿步的 L=1 调用会污染基线桶——L=1 只取 mode=off 那一跑的桶；
- MLX 是惰性图：`mx.eval(本组件输出)` 会把上游未计相的零头结到本组件账上，
  且**每次调用带一份与 L 无关的固定同步开销 c**——它会系统性压低 t(4)/t(1)
  （首过评审 Critical）。故开跑先标定 c（对已求值数组空跑 mx.eval），
  比值同时报**原始**与**减 c 校正**两个值；测得比值一律是真值的**下界**，
  "≥阈值 ⇒ 内核残差"的判读方向安全，"≤阈值 ⇒ 缓存已满"只在 c 占比小时可判；
- **阳性对照内置**（P1）：稠密量化件（dense MLP / lm_head）在 M=6 处 qmv_wide
  瓦片 1→2，t(6)/t(4) 应落在预测带 [1.5, 2.2]；<1.3 判探针不可信整组作废（退出码 2）。
  L=6 桶为空是**另一种失败**（block6 未生效/截断），单独报（退出码 3），
  不许错记成对照失败——判据先于结论，不许事后挑格（§4.23）;
- **前提在跑前断言**：排序阈值补丁已装且阈值 16（否则全部参照常数口径不对，
  等于给没排序的跑批发归因证书——上一轮 check_moe_sort_equiv 的同款闸门）；
- **漂移哨**：末尾再跑一次 mode=off，逐组件比首跑，|漂移| >5% 则显著标警。

# 口径

26b-8bit + 4bit 草稿件、生产装载路、temp=0 / rep1.1 / ctx50 / 单请求 /
每态 300 tok。五次生成按序：off①(冷) → block4 → block6 → off②(漂移哨，热) →
block4(只测 _gather_sort/_scatter_unsort，嵌套插桩会互染故分开跑)。
本表绝对 ms 一律不入归档；结论只取比值与判定。

单 GPU 纪律：本脚本自己装载模型；跑前确认无其它真模型进程。
用法：
  python3 scripts/probe_moe_components.py [目标件] [草稿件] [tokens]
"""
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
os.chdir(str(Path(__file__).resolve().parents[1]))

import mlx.core as mx  # noqa: E402

PROMPT_TEXT = ("详细说明光合作用的完整过程，分光反应与暗反应两部分展开，每部分至少三点。")

TARGET = sys.argv[1] if len(sys.argv) > 1 else "models/gemma-4-26b-a4b-it-8bit"
DRAFT = sys.argv[2] if len(sys.argv) > 2 else "models/gemma-4-26b-a4b-it-assistant-4bit"
MAX_TOKENS = int(sys.argv[3]) if len(sys.argv) > 3 else 300

RUN_TAG = "idle"          # 由 main 在每次生成前设置；探针按它分桶
BUCKETS: dict = {}        # {(run, 组件, L): [秒, ...]}


def _acc(name: str, L: int, dt: float) -> None:
    BUCKETS.setdefault((RUN_TAG, name, L), []).append(dt)


def _x_seq(a, k) -> int:
    if a:
        return int(a[0].shape[-2])
    # 不猜 kwargs：Attention 的首个 kwarg 是 mask（常为 None），猜错 = 静默错桶，
    # 而错桶不可见正是本脚本最怕的事（首过评审 Suggestion）。
    raise RuntimeError("被探针包住的调用点改用了关键字传参——请更新 _x_seq 的取形状逻辑")


def _wrap_method(cls, attr: str, name: str):
    """把 cls.attr 换成计时版（类属性，与生产补丁同一手法）。返回条件还原函数。"""
    orig = getattr(cls, attr)

    def timed(self, *a, **k):
        L = _x_seq(a, k)
        t0 = time.perf_counter()
        r = orig(self, *a, **k)
        mx.eval(r)
        _acc(name, L, time.perf_counter() - t0)
        return r

    setattr(cls, attr, timed)

    def _undo():
        if getattr(cls, attr) is timed:      # 只还原确实是本探针的（不抹上层）
            setattr(cls, attr, orig)
    return _undo


def _wrap_func(mod, attr: str, name: str, seq_of):
    orig = getattr(mod, attr)

    def timed(*a, **k):
        L = seq_of(a, k)
        t0 = time.perf_counter()
        r = orig(*a, **k)
        mx.eval(r)
        _acc(name, L, time.perf_counter() - t0)
        return r

    setattr(mod, attr, timed)

    def _undo():
        if getattr(mod, attr) is timed:
            setattr(mod, attr, orig)
    return _undo


def _install_all(specs):
    """依次安装；任何一步失败先回滚已装的再抛（不留半装态）。返回总还原函数。"""
    done = []
    try:
        for install in specs:
            done.append(install())
    except Exception:
        for u in reversed(done):
            u()
        raise

    def uninstall():
        for u in reversed(done):
            u()
    return uninstall


def install_component_probes():
    from mlx_vlm.models.gemma4 import language as G
    return _install_all([
        lambda: _wrap_method(G.Attention, "__call__", "attention"),
        lambda: _wrap_method(G.MLP, "__call__", "dense_mlp"),
        lambda: _wrap_method(G.Router, "__call__", "router"),
        lambda: _wrap_method(G.Experts, "__call__", "experts"),
        lambda: _wrap_method(G.LanguageModel, "logits_from_hidden", "lm_head"),
    ])


def install_sort_probes():
    from mlx_vlm.models import switch_layers as SL
    return _install_all([
        lambda: _wrap_func(SL, "_gather_sort", "gather_sort",
                           lambda a, k: int(a[1].shape[-2])),
        lambda: _wrap_func(SL, "_scatter_unsort", "scatter_unsort", lambda a, k: -1),
    ])


def calibrate_probe_floor() -> float:
    """标定每次探针调用的固定开销 c（perf_counter + 对已求值数组的 mx.eval）。

    这是**下界**：生成中途的 eval 还要排干在飞的图，真实开销 ≥ c。
    用它校正出的比值仍是真值下界，判读方向见模块头。
    """
    x = mx.zeros((1,))
    mx.eval(x)
    samples = []
    for _ in range(2000):
        t0 = time.perf_counter()
        mx.eval(x)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def build_prompt(target_dir: str) -> str:
    from transformers import AutoTokenizer
    from framework.dialects import get_dialect_class
    tok = AutoTokenizer.from_pretrained(target_dir)
    d = get_dialect_class("gemma4").create(
        SimpleNamespace(tokenizer=tok), options={"suppress_ghost_thinking": True})
    return d.build_prompt(messages=[{"role": "user", "content": PROMPT_TEXT}],
                          include_env=False)


def run_once(be, prompt: str, sampling: dict) -> float:
    last = None
    for ch in be.stream_generate(prompt, cache=be.new_cache(), sampling=sampling):
        last = ch
    if not last or not last.generation_tps:
        raise RuntimeError("后端未报 generation_tps（§4.12：口径不可混用墙钟）")
    return last.generation_tps


def med_ms(run: str, name: str, L: int):
    v = BUCKETS.get((run, name, L))
    if not v:
        return None, 0
    return statistics.median(v) * 1e3, len(v)


def _num_layers(target_dir: str) -> int:
    cfg = json.loads((Path(target_dir) / "config.json").read_text(encoding="utf-8"))
    tc = cfg.get("text_config") or cfg
    return int(tc["num_hidden_layers"])


async def main() -> int:
    global RUN_TAG
    from framework.backends.mlx.patch import moe_sort_threshold as P
    from framework.backends.mlx.patch.gemma4_generate import lookup_generate_step  # noqa: F401  确保补丁链已装
    from framework.backends.mlx.backend import MLXBackend
    # 装载前先验证探针目标存在（上游改名时别烧一次 31GB 装载才发现）
    from mlx_vlm.models.gemma4 import language as G
    for attr in ("Attention", "MLP", "Router", "Experts", "LanguageModel"):
        if not hasattr(G, attr):
            raise RuntimeError(f"上游 gemma4.language 缺 {attr}，探针目标已变，先适配再跑")

    # 前提闸门：参照常数（2.235~2.245 / 1.925 / 18.4ms）全部是"排序阈值 16 生效"
    # 口径下的数。补丁没装或阈值不对时跑出来的归因对不上任何参照——直接拒跑。
    if not P.is_installed() or P.min_size() != 16:
        raise RuntimeError(
            f"排序阈值补丁状态不对（installed={P.is_installed()}, "
            f"min_size={P.min_size()}）——Design C 的全部参照常数都是阈值 16 口径，拒跑。")

    n_layers = _num_layers(TARGET)
    samp = {"max_tokens": MAX_TOKENS, "temperature": 0.0,
            "repetition_penalty": 1.1, "repetition_context_size": 50}
    print(f"目标 {TARGET}\n草稿 {DRAFT}\ntokens={MAX_TOKENS}  层数={n_layers}"
          f"  口径：temp=0/rep1.1/ctx50/单请求；逐组件 mx.eval 插桩，只采信比值\n")

    c_floor = calibrate_probe_floor()
    print(f"空 eval 下界 ≈ {c_floor*1e6:.1f} µs/次（仅供对照——v1.1 证明它与真实探针"
          "开销差三个数量级，v2 校正一律用 router 零参照，见预测文件 v1.2）\n")

    be = await MLXBackend.load(TARGET, speculative={
        "mode": "drafter", "draft_model": DRAFT, "draft_block_size": 4})
    prompt = build_prompt(TARGET)

    # off①(冷) → block4 → block6 → off②(漂移哨)。首跑含预填/JIT 首特化，
    # 各 run 桶内取中位可吸收单次首调用离群。
    runs = [("off1", "off", None), ("b4", "drafter", 4),
            ("b6", "drafter", 6), ("off2", "off", None)]
    uninstall = install_component_probes()
    try:
        for tag, mode, blk in runs:
            be.speculative["mode"] = mode
            if blk:
                be.speculative["draft_block_size"] = blk
            RUN_TAG = tag
            tps = run_once(be, prompt, samp)
            # 仅供操作者看运行是否正常；探针态 tps 不可比、不入档
            print(f"[{tag}] 解码 {tps:.2f} tps（探针态，禁止引用）")
    finally:
        RUN_TAG = "idle"
        uninstall()

    # 细粒度：排序辅助函数单独一趟（嵌套插桩会互染，分开跑）
    be.speculative["mode"] = "drafter"
    be.speculative["draft_block_size"] = 4
    un2 = install_sort_probes()
    try:
        RUN_TAG = "sort_b4"
        run_once(be, prompt, samp)
    finally:
        RUN_TAG = "idle"
        un2()

    comps = ["attention", "dense_mlp", "router", "experts", "lm_head"]
    # v2（预测文件 v1.2）：**router 作在跑零参照**。它的真实计算 ~1µs 量级，
    # 测得值即"纯探针开销 o(run,L)"（launch + 管线排干），v1.1 实测跨 L 恒定。
    # 校正 t̂ = m − o(同 run 同 L)；t̂ < 0.02ms 的格判"不可靠"，不参与判定。
    o1 = med_ms("off1", "router", 1)[0]
    o4 = med_ms("b4", "router", 4)[0]
    o6 = med_ms("b6", "router", 6)[0]
    if not (o1 and o4 and o6):
        print("⛔ router 零参照桶缺失，v2 校正无从谈起——口径失败（退出码 3）")
        return 3
    UNRELIABLE = 0.020  # ms
    print(f"\nrouter 零参照 o(L=1/4/6) = {o1:.3f} / {o4:.3f} / {o6:.3f} ms"
          f"（应跨 L 恒定；v1.1 为 0.222/0.224/0.219）")
    print(f"\n{'组件':<12}{'off① L=1':>10}{'n':>6}{'b4 L=4':>10}{'n':>6}"
          f"{'b6 L=6':>10}{'n':>6}{'t4/t1':>7}{'校正':>7}{'t6/t4':>7}{'校正':>7}")
    print("-" * 92)
    raw41, cor41, raw64, cor64, unrel = {}, {}, {}, {}, {}
    for c in comps:
        m1, n1 = med_ms("off1", c, 1)
        m4, n4 = med_ms("b4", c, 4)
        m6, n6 = med_ms("b6", c, 6)
        t1 = (m1 - o1) if m1 else None
        t4 = (m4 - o4) if m4 else None
        t6 = (m6 - o6) if m6 else None
        unrel[c] = (c != "router") and any(
            t is not None and t < UNRELIABLE for t in (t1, t4, t6))
        r41 = (m4 / m1) if (m1 and m4) else float("nan")
        k41 = (t4 / t1) if (t1 and t4 and t1 > 0) else float("nan")
        r64 = (m6 / m4) if (m4 and m6) else float("nan")
        k64 = (t6 / t4) if (t4 and t6 and t4 > 0) else float("nan")
        raw41[c], cor41[c], raw64[c], cor64[c] = r41, k41, r64, k64
        mark = " ⚠️薄" if unrel[c] else ""
        print(f"{c:<12}{m1 or 0:>10.3f}{n1:>6}{m4 or 0:>10.3f}{n4:>6}"
              f"{m6 or 0:>10.3f}{n6:>6}{r41:>7.3f}{k41:>7.3f}{r64:>7.3f}{k64:>7.3f}{mark}")

    # 桶盘点（带计数：截断/自适应回退的杂散 L 一眼可见）
    strays = sorted((run, name, L, len(v)) for (run, name, L), v in BUCKETS.items()
                    if run in ("b4", "b6") and L not in (4, 6))
    if strays:
        print("\n杂散桶（run, 组件, L, 次数）——含预填与草稿步（草稿件复用 gemma4 类），"
              "不参与判定：")
        for row in strays[:20]:
            print(f"  {row}")

    # ── 空桶判定先于对照判定（两种失败必须分开报）─────────────────────────
    if any(med_ms("b6", c, 6)[1] == 0 for c in ("dense_mlp", "lm_head", "experts")):
        print("\n⛔ L=6 桶为空：block6 未生效或全部被截断/回退——这是**探针口径失败**"
              "（退出码 3），不是阳性对照失败。查 prefer_requested_block_size 旁路"
              "是否仍在（scan_mtp_block 的 block6 行历史上是生效的）。")
        return 3

    # ── P1' 阳性对照（v1.2）：硬对照 = dense_mlp 校正比值；lm_head 软对照只报──
    ctrl = cor64["dense_mlp"]
    if ctrl != ctrl:                                     # nan 检查
        print("\n⛔ 阳性对照数据不完整（dense_mlp 校正比值为 nan）——口径失败，退出码 3。")
        return 3
    if ctrl < 1.3:
        print(f"\n⛔ 阳性对照失败：dense_mlp 校正后 t̂(6)/t̂(4) = {ctrl:.2f}，"
              "未见瓦片 1→2 跳变（预测带 [1.5,2.2]，底线 1.3）。探针不可信，"
              "整组作废（P1' 预先承诺）。")
        return 2
    band = "在预测带内" if 1.5 <= ctrl <= 2.2 else "⚠️ 过底线但出了预测带 [1.5,2.2]，判读降级"
    print(f"\n✅ P1' 阳性对照通过：dense_mlp 校正后 t̂(6)/t̂(4) = {ctrl:.2f}（{band}）；"
          f"软对照 lm_head = {cor64['lm_head']:.2f}"
          "（每步单调、排干口径与逐层零参照不全匹配，预期偏低，只报不判）")

    # ── 漂移哨：off② vs off①（逐组件，>5% 显著标警）───────────────────────
    worst = 0.0
    for c in comps:
        a, _ = med_ms("off1", c, 1)
        b, _ = med_ms("off2", c, 1)
        if a and b:
            worst = max(worst, abs(b / a - 1))
    flag = "⚠️ 超 5%，t4/t1 比值带方向性偏置，判读需谨慎" if worst > 0.05 else "可接受"
    print(f"漂移哨：off②/off① 逐组件最大偏移 {worst*100:.1f}%（{flag}）")

    # ── P2' 主判据（router 零参照校正值）─────────────────────────────────────
    k = cor41["experts"]
    rel = "（⚠️ 校正后残差薄，不可靠）" if unrel["experts"] else ""
    print(f"\n主判据：Experts t̂(L=4)/t̂(L=1) 原始 {raw41['experts']:.3f} / "
          f"校正 **{k:.3f}**{rel}")
    print("  参照：≈2.24（v(4)=1.461~1.465 反算，残差在专家内核 ⇒ S2 值得跑）"
          "｜1.925（纯并集下限，缓存已回收满 ⇒ 内核线止）")
    if unrel["experts"] or k != k:
        print("  ⇒ 校正后数据不可靠，不判——改跑 S2 用 E=8 对撞直接量")
    elif k >= 2.15:
        print("  ⇒ 判定：残差集中在专家内核的逐对重读，**进 S2（E=8 对撞）**")
    elif k <= 1.95:
        print("  ⇒ 判定：贴近纯字节下限，缓存已把去重做满，**内核线到此为止**")
    else:
        print("  ⇒ 灰区（1.95–2.15，预先承诺不强判）：改跑 S2 用 E=8 对撞直接量")

    # ── P3/P4 判语（预注册项都要有下场，不许静默）───────────────────────────
    print(f"P3 路由：v2 起 router 即零参照，恒 ≈1 是构造使然（原始 {raw41['router']:.2f}），"
          "改为观察项不判")
    m4a, _ = med_ms("b4", "attention", 4)
    step4 = n_layers * sum((med_ms('b4', x, 4)[0] or 0)
                           for x in ("attention", "dense_mlp", "router", "experts")) \
        + (med_ms("b4", "lm_head", 4)[0] or 0)
    att_share = n_layers * (m4a or 0) / step4 if step4 else float("nan")
    print(f"P4 注意力：t4/t1 = {raw41['attention']:.2f}（预测 1.0~2.0），"
          f"占探针态步时 {att_share*100:.0f}%（预测 <15%）"
          f"{'✅' if raw41['attention'] <= 2.0 and att_share < 0.15 else '⚠️ 出预测带'}")

    # ── P6 量级一致性（探针态，只查数量级不查精确）───────────────────────────
    v4t1 = 18.4  # §1.3-b 生产口径 v(4)×T1（非探针态）
    n_probe_calls = 4 * n_layers + 1
    probe_oh_ms = n_probe_calls * o4
    print(f"P6 组件和：探针态 {step4:.1f} ms/步（其中探针开销约 {probe_oh_ms:.1f} ms，"
          f"按 router 零参照 ×{n_probe_calls} 次估）vs 无探针参照 {v4t1} ms——"
          "只作量级自检，不作判据（两口径探针开销不同）")

    # ── P5 排序辅助（上界读法：探针把 µs 级小算子大幅抬高，只能排除、不能立项）──
    gs = [v for (r, n, _), vals in BUCKETS.items() if r == "sort_b4" and n == "gather_sort"
          for v in vals]
    su = [v for (r, n, _), vals in BUCKETS.items() if r == "sort_b4" and n == "scatter_unsort"
          for v in vals]
    if not gs or not su:
        print("\n⛔ P5 失败：sort_b4 跑里 _gather_sort/_scatter_unsort 零调用——"
              "排序没被触发（补丁状态？）或包装失败。这必须响，不许静默跳过。")
        return 3
    per_step_ms = (statistics.median(gs) + statistics.median(su)) * n_layers * 1e3
    share_ub = per_step_ms / v4t1
    verdict = ("<2% ⇒ P5 排除，排序辅助不值得动" if share_ub < 0.02 else
               "上界 ≥2%——**不可据此立项**（探针把 µs 级算子抬高数倍），"
               "要立项须先做无探针 A/B")
    print(f"\nP5 排序辅助（**探针态上界**）：gather {statistics.median(gs)*1e6:.0f}µs + "
          f"unsort {statistics.median(su)*1e6:.0f}µs ×{n_layers} 层 ≈ {per_step_ms:.2f} ms/步 "
          f"= v(4)·T1 的 {share_ub*100:.1f}%（上界）。{verdict}")

    print("\n判读与后续动作写回 MTP_PROJECT.md §6.2（S1 行）；本表绝对 ms 不入归档。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
