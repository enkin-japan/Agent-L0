"""
验证 gemma4-e4b 是否原生支持 TurboQuant 量化缓存注意力（端到端真实推理）。

方法：同一提示词、temperature=0，跑两次官方 mlx_vlm.generate：
  A) 基线：稠密 KV cache
  B) TurboQuant：kv_bits=3.5, quantized_kv_start=0（强制从第 0 token 起量化）
并给 TurboQuantKVCache 的 decode/prefill attention 打计数，证明量化注意力路径确实被执行。

判据：
  - B 不崩溃
  - B 的 TurboQuant attention 计数 > 0（路径真的走到）
  - B 输出连贯（非空、可打印、与基线语义相近）

已验证结论（2026-07，mlx_vlm 0.5.0，gemma-4-e4b-it-4bit）：原生支持。
  基线与 kv_bits=3.5 输出几乎逐字相同（3.5-bit 质量中性），decode attention 计数=108（路径确执行）。
  运行：  .venv/bin/python scripts/probe_gemma4_turboquant.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlx.core as mx
from mlx_vlm.utils import load
from mlx_vlm import generate as mlx_generate_mod
from mlx_vlm.generate import generate
from mlx_vlm import turboquant as tq
from framework.utils.path_resolver import resolve_model_path

MODEL = "mlx-community/gemma-4-e4b-it-4bit"

print("加载模型中…")
model_path = resolve_model_path(MODEL)
model, processor = load(model_path)
print("模型已加载。")

# --- 给 TurboQuant 的注意力路径打计数（证明确实被执行） ---
counters = {"decode": 0, "prefill": 0}
_orig_decode = tq.TurboQuantKVCache.decode_attention
_orig_prefill = tq.TurboQuantKVCache.prefill_attention

def _decode(self, *a, **k):
    counters["decode"] += 1
    return _orig_decode(self, *a, **k)

def _prefill(self, *a, **k):
    counters["prefill"] += 1
    return _orig_prefill(self, *a, **k)

tq.TurboQuantKVCache.decode_attention = _decode
tq.TurboQuantKVCache.prefill_attention = _prefill

# --- 构造 chat 提示词 ---
messages = [{"role": "user", "content": "用一句话解释什么是哈希表。"}]
try:
    prompt = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
except Exception:
    prompt = "用一句话解释什么是哈希表。"

def run(tag, **kw):
    counters["decode"] = 0
    counters["prefill"] = 0
    try:
        res = generate(model, processor, prompt, max_tokens=64, temperature=0.0,
                       verbose=False, **kw)
        text = res.text if hasattr(res, "text") else str(res)
        print(f"\n===== {tag} =====")
        print("输出:", repr(text))
        print(f"TurboQuant 计数: prefill={counters['prefill']} decode={counters['decode']}")
        return text, dict(counters), None
    except Exception as e:
        import traceback
        print(f"\n===== {tag} 失败 =====")
        traceback.print_exc()
        return None, dict(counters), e

# A) 基线（稠密）
base_text, base_cnt, base_err = run("A 基线（稠密 KV）")
# B) TurboQuant 3.5-bit，强制从头量化
tq_text, tq_cnt, tq_err = run("B TurboQuant kv_bits=3.5",
                              kv_bits=3.5, quantized_kv_start=0)

# --- 判定 ---
print("\n" + "=" * 40)
ok = True
if base_err is not None:
    print("⚠️ 基线本身就失败了，测试环境有问题，结论不可靠")
    ok = False
if tq_err is not None:
    print("❌ TurboQuant 路径崩溃 → gemma4 不能直接吃量化缓存（或该版本有 bug）")
    ok = False
else:
    if tq_cnt["prefill"] == 0 and tq_cnt["decode"] == 0:
        print("❌ TurboQuant attention 计数为 0 → 量化路径根本没被走到（quantized_kv_start 未生效？）")
        ok = False
    else:
        print(f"✅ TurboQuant attention 路径确实执行（prefill={tq_cnt['prefill']}, decode={tq_cnt['decode']}）")
    if tq_text and tq_text.strip() and tq_text.isprintable():
        print("✅ TurboQuant 输出非空且可打印（连贯性需人工看上面的 repr）")
    else:
        print("⚠️ TurboQuant 输出为空或含异常字符，需人工检查")
        ok = False

print("\n最终结论:", "✅ gemma4 原生支持 TurboQuant 量化缓存注意力" if ok
      else "❌ 存在问题，见上")
