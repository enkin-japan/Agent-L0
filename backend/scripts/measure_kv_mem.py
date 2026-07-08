"""
KV Cache 显存对比：dense vs TurboQuant。用长上下文（~3000 token）prefill 后，
直接读每层缓存 nbytes（硬压缩指标）+ 进程峰值显存 + 层类型分布。
用法： measure_kv_mem.py dense|tq
每种模式独立进程运行，峰值显存互不污染。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlx.core as mx
from mlx_vlm.utils import load
from mlx_vlm.generate import PromptCacheState
from framework.patch.gemma4_generate import stream_generate
from framework.utils.path_resolver import resolve_model_path

mode = sys.argv[1] if len(sys.argv) > 1 else "dense"
kv = {"kv_bits": 3.5, "quantized_kv_start": 0} if mode == "tq" else {}

model, processor = load(resolve_model_path("mlx-community/gemma-4-e4b-it-4bit"))

# 构造 ~3000 token 的长上下文
para = ("量子计算利用叠加与纠缠等量子力学效应进行信息处理。"
        "与经典比特只能取 0 或 1 不同，量子比特可处于叠加态。")
long_ctx = para * 90  # 重复堆到数千 token
msgs = [{"role": "user", "content": long_ctx + "\n请用一句话总结上面这段话。"}]
prompt = processor.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)

state = PromptCacheState()
last = None
for r in stream_generate(model, processor, prompt, prompt_cache_state=state,
                         max_tokens=8, temperature=0.0, **kv):
    last = r

# 读每层缓存 nbytes 与类型
from collections import defaultdict
by_type_bytes = defaultdict(int)
by_type_count = defaultdict(int)
total = 0
for c in state.cache:
    t = type(c).__name__
    nb = getattr(c, "nbytes", 0) or 0
    by_type_bytes[t] += nb
    by_type_count[t] += 1
    total += nb

MB = 1024 * 1024
print(f"MODE={mode}")
print(f"prompt_tokens={last.prompt_tokens}")
print(f"layers={len(state.cache)}")
for t in sorted(by_type_bytes):
    print(f"  {t}: count={by_type_count[t]} bytes={by_type_bytes[t]/MB:.2f}MB")
print(f"KV_TOTAL_MB={total/MB:.2f}")
print(f"PEAK_MEM_GB={mx.get_peak_memory()/1e9:.3f}")
