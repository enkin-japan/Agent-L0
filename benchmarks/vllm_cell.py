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

用法：python vllm_cell.py <target> <draft|none> <k|0> <prompt.json> <max_tokens> <out.json> [eager]
"""
import json
import os
import sys
import time

os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")  # 探针要挂进引擎进程

TARGET, DRAFT, K = sys.argv[1], sys.argv[2], int(sys.argv[3])
PROMPT_FILE, MAX_TOKENS, OUT = sys.argv[4], int(sys.argv[5]), sys.argv[6]
EAGER = len(sys.argv) > 7 and sys.argv[7] == "eager"
UTIL = float(os.environ.get("VLLM_UTIL", "0.80"))

ROUNDS: list = []


def install_probe():
    from vllm.v1.spec_decode import metrics as M
    orig = M.SpecDecodingStats.observe_draft

    def patched(self, num_draft_tokens, num_accepted_tokens):
        ROUNDS.append((int(num_draft_tokens), int(num_accepted_tokens)))
        return orig(self, num_draft_tokens, num_accepted_tokens)

    M.SpecDecodingStats.observe_draft = patched


def main():
    from vllm import LLM, SamplingParams
    from vllm.inputs import TokensPrompt

    install_probe()
    ids = json.load(open(PROMPT_FILE))["token_ids"]
    sp = SamplingParams(temperature=0.0, max_tokens=MAX_TOKENS)

    kw = dict(model=TARGET, max_model_len=4096, gpu_memory_utilization=UTIL,
              enforce_eager=EAGER, disable_log_stats=False)
    if K > 0:
        kw["speculative_config"] = {"model": DRAFT, "num_speculative_tokens": K}

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
        "eager": EAGER, "max_tokens": MAX_TOKENS, "prompt_tokens": len(ids),
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
