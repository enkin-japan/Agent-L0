"""A100 侧 vLLM 的 Gemma-4 MTP 对照台——**一次进程只跑一格**。

为什么一格一进程：vLLM V1 的引擎持有 KV 池与 CUDA Graph，`del llm` +
`empty_cache()` **并不归还显存**（实测第二格开场只剩 5.43/39.49 GiB 就崩了）。
唯一可靠的释放是进程退出。故由 shell 驱动逐格调用本脚本。

与 Mac 侧 `backend/scripts/scan_mtp_block.py` 口径逐项对齐：
  · 同一条 prompt，**直接下发 token id**（不各自套模板，杜绝模板差异）
  · temp=0、max_tokens 相同、单请求（无并发）
  · 报同一个统计量 P(接受数 ≥ k)

统计量等价性［源码实证 双侧］：
  vLLM `v1/spec_decode/metrics.py:46` for i in range(num_accepted_tokens): per_pos[i] += 1
       `:117` acceptance_rates = sum(pos_matrix, axis=0) / num_drafts
  mlx_vlm `speculative/common.py:151-157` accepted = 首次不匹配前的命中数（不含 bonus）
  两边 accepted 语义一致 ⇒ acceptance_rates[k-1] ≡ P(accepted ≥ k)，可逐位对表。

用法：python vllm_cell.py <target> <draft|none> <k|0> <prompt.json> <max_tokens> <out.json>
          [第7参 eager|graph] [第8参 quantization，如 fp8]
第 7 参只认字面量 "eager"（其余值一律等于 graph），但要传第 8 参就必须占住第 7 位，
故惯用 "graph" 当占位。`max_model_len` 由环境变量 VLLM_MAXLEN 控制（缺省 4096）。

SPEC_METHOD 环境变量（2026-08-15 复核场新增）：非空时写进 speculative_config 的
"method" 键（如 "mtp"）。台账 batch6 的 1.92× 走的是 method:"mtp"（KV/嵌入共享的
官方 MTP 路），而本脚本既有各批（e4b/31B/26B B5）从未传 method——vLLM 对
Gemma4MTP 架构的草稿件会不会自动路由到 mtp 路径，正是本键要 A/B 的问题之一。
故输出里同时记请求值（spec_method）与**引擎生效值**（spec_method_effective，
从 vllm_config 读回）——A/B 的证据必须是后者。
"""
import json
import os
import sys
import time

os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")  # 探针要挂进引擎进程

TARGET, DRAFT, K = sys.argv[1], sys.argv[2], int(sys.argv[3])
PROMPT_FILE, MAX_TOKENS, OUT = sys.argv[4], int(sys.argv[5]), sys.argv[6]
EAGER = len(sys.argv) > 7 and sys.argv[7] == "eager"
# 第 8 参 = 量化方式（如 fp8）。A100 是 Ampere，vLLM 的 fp8 在这里走 Marlin
# **权重量化**路——与 MLX 的 8bit 权重量化同类，比 bf16 对齐得多。
QUANT = sys.argv[8] if len(sys.argv) > 8 else None
SPEC_METHOD = os.environ.get("SPEC_METHOD", "").strip() or None
UTIL = float(os.environ.get("VLLM_UTIL", "0.80"))
# max_model_len 直接决定 KV 池的**需求**。31B fp8 权重占满卡后只剩 0.89GiB，
# 而 4096 需要 3.45GiB → 引擎拒起。本对照只用 61+400=461 tok，故可压到 1024。
# 缺省仍为 4096，保持既有 e4b/26B 那两批数据的口径不变。
MAXLEN = int(os.environ.get("VLLM_MAXLEN", "4096"))

ROUNDS: list = []


def install_probe():
    from vllm.v1.spec_decode import metrics as M
    orig = M.SpecDecodingStats.observe_draft

    def patched(self, num_draft_tokens, num_accepted_tokens):
        ROUNDS.append((int(num_draft_tokens), int(num_accepted_tokens)))
        return orig(self, num_draft_tokens, num_accepted_tokens)

    M.SpecDecodingStats.observe_draft = patched


def effective_spec_method(llm):
    """从引擎读回实际生效的投机 method（记录进产物）。

    三种异常态用可区分哨兵值，绝不与真值 "mtp" 的缺席混淆（评审）：
      no_spec_config —— 引擎根本没挂投机（K>0 时出现即投机静默未生效，本探针
                        存在的头号意义就是抓它）；
      no_method_attr —— vllm 升级改了属性名；
      probe_fail:XX  —— 属性路径整个变了。
    """
    try:
        sc = llm.llm_engine.vllm_config.speculative_config
        if sc is None:
            return "no_spec_config"
        return getattr(sc, "method", "no_method_attr")
    except Exception as exc:
        return f"probe_fail:{type(exc).__name__}"


def main():
    import vllm as _vllm
    from vllm import LLM, SamplingParams
    from vllm.inputs import TokensPrompt

    install_probe()
    ids = json.load(open(PROMPT_FILE))["token_ids"]
    sp = SamplingParams(temperature=0.0, max_tokens=MAX_TOKENS)

    kw = dict(model=TARGET, max_model_len=MAXLEN, gpu_memory_utilization=UTIL,
              enforce_eager=EAGER, disable_log_stats=False)
    if QUANT:
        kw["quantization"] = QUANT
    if K > 0:
        kw["speculative_config"] = {"model": DRAFT, "num_speculative_tokens": K}
        if SPEC_METHOD:
            kw["speculative_config"]["method"] = SPEC_METHOD

    # max_model_len 小于 prompt+生成 会被引擎悄悄截断：tps 按短跑算，
    # 而 JSON 里仍记着请求的 max_tokens —— 一行看不出问题的坏数据。
    need = len(ids) + MAX_TOKENS
    if MAXLEN < need:
        raise SystemExit(
            f"VLLM_MAXLEN={MAXLEN} < prompt {len(ids)} + max_tokens {MAX_TOKENS} = {need}："
            "本格会被截断且不报错，拒绝开跑。")

    llm = LLM(**kw)
    llm.generate([TokensPrompt(prompt_token_ids=ids)], sp)      # 预热，不计时
    ROUNDS.clear()
    t0 = time.perf_counter()
    r = llm.generate([TokensPrompt(prompt_token_ids=ids)], sp)
    dt = time.perf_counter() - t0

    tok = list(r[0].outputs[0].token_ids)
    n = len(tok)
    drafted = sum(d for d, _ in ROUNDS)
    accepted = sum(a for _, a in ROUNDS)
    out = {
        "target": TARGET, "draft": DRAFT if K > 0 else None, "gamma": K,
        "spec_method": SPEC_METHOD if K > 0 else None,
        "spec_method_effective": effective_spec_method(llm) if K > 0 else None,
        "vllm": _vllm.__version__,
        "eager": EAGER, "quant": QUANT, "max_model_len": MAXLEN,
        "max_tokens": MAX_TOKENS, "prompt_tokens": len(ids),
        "tps": n / dt, "tokens": n, "sec": dt, "rounds": len(ROUNDS),
        "drafted": drafted, "accepted": accepted,
        "rate": (accepted / drafted) if drafted else None,
        "AL": (1 + accepted / len(ROUNDS)) if ROUNDS else 1.0,
        "P_ge_k": [sum(1 for _, a in ROUNDS if a >= j) / len(ROUNDS)
                   for j in range(1, K + 1)] if ROUNDS else [],
        "token_ids": tok,
    }
    json.dump(out, open(OUT, "w"), ensure_ascii=False)
    print(f"CELL_DONE γ={K} tps={out['tps']:.2f} AL={out['AL']:.2f} "
          f"rate={out['rate']} rounds={out['rounds']}", flush=True)


if __name__ == "__main__":
    main()
