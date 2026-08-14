#!/usr/bin/env python3
"""把 gemma-4 assistant 草稿件量化并另存（A1：抬高 MTP 上界）。

为什么值得做［实测依据］：31B 草稿件 bf16 共 926 MB，其中 **537 MB（58%）是
`tie_word_embeddings=True` 下当 lm_head 用的绑定嵌入**（262144×1024×2B）。
γ=4 时草稿件占每轮约 10 ms / 95 ms ≈ 10.5%，且**随 γ 线性增长**——它同时压着
「当前档位的收益」和「更深 γ 是否划算」两件事。

风险方向与量化目标件相反［报告实证］：目标件量化只改验证成本，草稿件量化直接
改**提案分布**，可能压低接受率。故本脚本只负责产出件，收益由 `scan_mtp_block.py`
在同一口径下实测裁决（AL 与 tps 必须一起看）。

注意事项：
  · `use_ordered_embeddings=True` 的件（只有 e4b）带 `masked_embedding.token_ordering`
    这类 int32 静态缓冲，**不得量化**；本脚本用 mx.nn.quantize 的 class_predicate
    只挑 Linear/Embedding，天然跳过。
  · `tie_word_embeddings=True`：嵌入被量化后经 QuantizedEmbedding.as_linear 当
    lm_head 用，是 MLX 支持的路径。
  · 产出目录写入 config.json 的 `quantization` 段，`load_drafter` 走
    `mlx_vlm.utils.load_model` 会据此自动装配。

用法：python3 scripts/quantize_drafter.py <源目录> <目标目录> <bits> [group_size]
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if len(sys.argv) < 4:
    raise SystemExit(__doc__.strip().splitlines()[-1])
SRC = Path(sys.argv[1])
DST = Path(sys.argv[2])
BITS = int(sys.argv[3])
GROUP = int(sys.argv[4]) if len(sys.argv) > 4 else 64


def main() -> int:
    import mlx.core as mx
    import mlx.nn as nn
    from mlx.utils import tree_flatten
    from mlx_vlm.utils import get_model_path, load_model, save_weights, save_config

    if DST.exists():
        print(f"目标目录已存在，先删再跑：{DST}")
        return 1

    path = get_model_path(str(SRC))
    # `use_ordered_embeddings` 的件（目前只有 e4b）不能量化嵌入表：
    # bind() 会把 `model.embed_tokens.weight` **原样**交给 MaskedEmbedder，
    # 后者按 [vocab, hidden] 用；量化后那里是 packed uint32，形状与语义都不对。
    cfg0 = json.loads((path / "config.json").read_text())
    if cfg0.get("use_ordered_embeddings") or \
       cfg0.get("text_config", {}).get("use_ordered_embeddings"):
        raise SystemExit(
            "该草稿件 use_ordered_embeddings=True（centroids 掩码路），"
            "量化嵌入表会打断 MaskedEmbedder。拒绝处理。")
    print(f"装载 {path}")
    model = load_model(path)

    n_before = sum(v.size for _, v in tree_flatten(model.parameters()))
    bytes_before = sum(v.nbytes for _, v in tree_flatten(model.parameters()))
    print(f"量化前：{n_before/1e6:.1f}M 参数 / {bytes_before/2**20:.0f} MiB")

    def predicate(_path, module):
        # 只量化权重矩阵类；norm / 静态 int 缓冲（如 token_ordering）一律跳过。
        # 末维不足一组的层量化后反而更慢更糟，一并跳过。
        if not isinstance(module, (nn.Linear, nn.Embedding)):
            return False
        w = module.weight
        return w.ndim == 2 and w.shape[-1] % GROUP == 0

    nn.quantize(model, group_size=GROUP, bits=BITS, class_predicate=predicate)
    mx.eval(model.parameters())

    bytes_after = sum(v.nbytes for _, v in tree_flatten(model.parameters()))
    print(f"量化后：{bytes_after/2**20:.0f} MiB  （{bytes_after/bytes_before:.1%}）")

    DST.mkdir(parents=True)
    # save_weights 自己会调 model.parameters()，传模型而非已展平的 dict
    save_weights(DST, model, donate_weights=True)

    cfg = json.loads((path / "config.json").read_text())
    q = {"group_size": GROUP, "bits": BITS, "mode": "affine"}
    cfg["quantization"] = q
    cfg["quantization_config"] = dict(q)   # 两键各持一份，避免后续就地改动串联
    save_config(cfg, DST / "config.json")

    for name in ("tokenizer.json", "tokenizer_config.json", "generation_config.json",
                 "preprocessor_config.json", "special_tokens_map.json"):
        src = path / name
        if src.exists():
            shutil.copy(src, DST / name)

    print(f"已写出 {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
