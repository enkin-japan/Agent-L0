"""vLLM 0.26 嵌入共享维度护栏回归热补丁（打在 VM 的 site-packages 上，
不改仓库内任何依赖；值得提上游 issue，账本第 6 站有全案）。

gemma4 assistant MTP：护栏见 目标 backbone 维(2816) != 草稿维(1024) 即弃共享，
但 Gemma4MTP 草稿的 pre_projection 本就期待 2×backbone_hidden 输入、共享是必需
——对 Gemma4MTP 一族豁免该护栏（新 worker 栈无条件共享，活跃旧栈误加了护栏）。
幂等可重跑；改不动时打印函数源码退出码 2，供人工定位。
"""
import pathlib
import re
import sys

import vllm

p = pathlib.Path(vllm.__file__).parent / "v1" / "spec_decode" / "llm_base_proposer.py"
if not p.exists():
    print(f"NO_FILE {p}")
    sys.exit(2)
src = p.read_text()
if "AGENT_MTP_EXEMPT" in src:
    print("already patched")
    sys.exit(0)

# 护栏形态：if <target_dim 表达式> != <draft_dim 表达式>: → 弃共享分支。
# 在条件上追加 Gemma4MTP 豁免（类型名前缀判定，与模型注册名解耦）。
pat = re.compile(r"if\s+(\w*target\w*_dim)\s*!=\s*(\w*draft\w*_dim)\s*:")
m = pat.search(src)
if not m:
    print("PATTERN_MISS — _maybe_share_embeddings 源码如下：")
    fm = re.search(r"def _maybe_share_embeddings.*?(?=\n    def |\nclass |\Z)", src, re.S)
    print(fm.group(0) if fm else src[:4000])
    sys.exit(2)

patched = src[:m.start()] + (
    f"if {m.group(1)} != {m.group(2)} and not type(self.model).__name__"
    f".startswith('Gemma4MTP'):  # AGENT_MTP_EXEMPT"
) + src[m.end():]
p.write_text(patched)
print(f"patched {p}")
