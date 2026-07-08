"""不加载大模型，验证 TurboQuantKVCache 的复用原语：trim / clone(eval+deepcopy) / 独立性。"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlx.core as mx
from mlx_vlm.turboquant import TurboQuantKVCache

D, H = 256, 2  # gemma4-e4b: head_dim=256, n_kv_heads=2

def rand_kv(t):
    return mx.random.normal((1, H, t, D)), mx.random.normal((1, H, t, D))

c = TurboQuantKVCache(bits=3.5)
# prefill 10 tokens
k, v = rand_kv(10)
c.update_and_fetch(k, v)
# 再逐 token 追加 5 个（模拟解码）
for _ in range(5):
    k1, v1 = rand_kv(1)
    c.update_and_fetch(k1, v1)
print("offset after 10+5:", c.offset, "| self.keys type:", type(c.keys).__name__)
assert c.offset == 15

# --- trim 到 prefix_len=12 ---
assert c.is_trimmable()
n = c.trim(c.offset - 12)
print("trim ->", n, "new offset:", c.offset)
assert c.offset == 12

# --- clone：先 mx.eval 现有 keys/values，再 deepcopy（当前 clone_kv_cache 的做法） ---
try:
    for attr in ("keys", "values"):
        obj = getattr(c, attr, None)
        if obj is not None:
            mx.eval(obj)   # 关键：量化态元组能否被 eval
    clone = copy.deepcopy(c)
    print("clone OK, clone.offset:", clone.offset)
except Exception as e:
    import traceback; traceback.print_exc()
    print("❌ clone 失败:", e); sys.exit(1)

# --- 独立性：裁剪克隆不影响原缓存 ---
clone.trim(clone.offset - 5)
assert clone.offset == 5 and c.offset == 12, (clone.offset, c.offset)
print("独立性 OK: clone.offset=5, original.offset=12")

# --- 克隆后继续 update 能跑（不崩） ---
k2, v2 = rand_kv(1)
clone.update_and_fetch(k2, v2)
assert clone.offset == 6
print("clone 续写 OK, offset:", clone.offset)

print("\n🎉 TurboQuant 复用原语（trim/clone/独立性/续写）全部通过")
