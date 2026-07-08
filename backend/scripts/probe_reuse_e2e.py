"""
端到端验证缓存复用改动，两个场景：
  S1 完整复用（excess=0）：第二轮含第一轮全部前缀 → 输出连贯即可（trim 可为 0）。
  S2 部分复用（excess>0）：第二轮共享前缀后转向不同话题 → 强制裁掉上一轮残留 KV。
     判据：TurboQuant 触发 trim(>0)；裁剪后输出正确回答“新话题”、不被旧话题污染；稠密同样正确。
对照 kv_bits 关闭时零回归、且绝不触碰 TurboQuant 分支。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mlx_vlm.utils import load
from mlx_vlm.generate import PromptCacheState
from mlx_vlm import turboquant as tq
from framework.patch.gemma4_generate import stream_generate
from framework.utils.path_resolver import resolve_model_path

print("加载模型中…")
model, processor = load(resolve_model_path("mlx-community/gemma-4-e4b-it-4bit"))
print("模型已加载。\n")

trim_calls = {"n": 0}
_orig = tq.TurboQuantKVCache.trim
def _trim(self, n):
    trim_calls["n"] += 1
    return _orig(self, n)
tq.TurboQuantKVCache.trim = _trim

def chat(msgs):
    return processor.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)

def run(prompt, state, kv):
    text = ""
    for r in stream_generate(model, processor, prompt, prompt_cache_state=state,
                             max_tokens=48, temperature=0.0, **kv):
        text += r.text
    return text.strip()

# 两轮共享前缀 "用一句话说明"，第二轮转向“二分查找”，强制 excess>0
Q1 = [{"role": "user", "content": "用一句话说明快速排序的基本思想。"}]
Q2_div = [{"role": "user", "content": "用一句话说明什么是二分查找。"}]

def scenario(tag, kv):
    trim_calls["n"] = 0
    state = PromptCacheState()
    t1 = run(chat(Q1), state, kv)
    t2 = run(chat(Q2_div), state, kv)   # 第二轮独立话题，与第一轮仅共享开头
    print(f"===== {tag} =====")
    print("轮1(快排):", repr(t1))
    print("轮2(二分):", repr(t2))
    print("TurboQuant.trim 调用:", trim_calls["n"])
    # 正确性：第二轮必须答“二分查找”，不被“快排”残留污染
    on_topic = ("二分" in t2) or ("查找" in t2) or ("有序" in t2) or ("折半" in t2)
    contaminated = ("快速排序" in t2) or ("基准" in t2) or ("pivot" in t2.lower())
    return t1, t2, trim_calls["n"], on_topic, contaminated

print(">>> A) 稠密（关闭）")
_, _, td, ontop_d, cont_d = scenario("稠密", {})
print("\n>>> B) TurboQuant kv_bits=3.5")
_, _, tq_n, ontop_q, cont_q = scenario("TurboQuant 3.5", {"kv_bits": 3.5, "quantized_kv_start": 0})

print("\n" + "=" * 40)
ok = True
# 稠密零回归 + 正确 + 不碰量化分支
if td != 0:
    print(f"❌ 稠密不应调用 TurboQuant.trim（={td}）"); ok = False
else:
    print("✅ 稠密未触碰 TurboQuant 分支")
if ontop_d and not cont_d:
    print("✅ 稠密第二轮正确答二分查找、无污染")
else:
    print(f"❌ 稠密第二轮异常 on_topic={ontop_d} contaminated={cont_d}"); ok = False
# TurboQuant：trim 触发 + 正确 + 无污染
if tq_n > 0:
    print(f"✅ TurboQuant 部分复用触发 trim（={tq_n}）")
else:
    print(f"❌ TurboQuant excess>0 场景未触发 trim（={tq_n}）"); ok = False
if ontop_q and not cont_q:
    print("✅ TurboQuant 裁剪后第二轮正确答二分查找、未被上一轮残留污染")
else:
    print(f"❌ TurboQuant 第二轮异常 on_topic={ontop_q} contaminated={cont_q}"); ok = False

print("\n最终:", "✅ 缓存复用改动验证通过" if ok else "❌ 存在问题")
