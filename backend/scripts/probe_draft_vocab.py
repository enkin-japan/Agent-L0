#!/usr/bin/env python3
"""Phase 1.2 前置检查（go/no-go）：草稿模型与主模型词表是否逐 id 一致。

投机采样要求草稿/主模型 tokenizer 完全对齐（同 id 同串、同特殊 token），
否则草稿 token 无法直接送主模型验证。校验 e2b / e4b（本地）/ 31B 三方。

用法：python3 scripts/probe_draft_vocab.py
"""
import sys
import json
import random
from pathlib import Path

from huggingface_hub import hf_hub_download

LOCAL_E4B = Path(__file__).resolve().parent.parent / "models" / "gemma-4-e4b-it-4bit"
REPOS = {
    "e2b": "mlx-community/gemma-4-e2b-it-4bit",
    "31b": "mlx-community/gemma-4-31b-it-4bit",
}

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    print(("  ✅ PASS " if cond else "  ❌ FAIL ") + name + (f"  [{extra}]" if extra else ""))
    PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)


def load_vocab(path):
    data = json.load(open(path, encoding="utf-8"))
    vocab = data["model"]["vocab"]
    if isinstance(vocab, dict):          # BPE: {token: id}
        return {int(v): k for k, v in vocab.items()}, data.get("added_tokens", [])
    return {i: (v[0] if isinstance(v, list) else v) for i, v in enumerate(vocab)}, data.get("added_tokens", [])


def main():
    paths = {"e4b(本地)": LOCAL_E4B / "tokenizer.json"}
    for tag, repo in REPOS.items():
        paths[tag] = Path(hf_hub_download(repo, "tokenizer.json"))
        print(f"已下载 {tag}: {repo}")

    vocabs = {}
    for tag, p in paths.items():
        v, added = load_vocab(p)
        vocabs[tag] = (v, {t["id"]: t["content"] for t in added})
        print(f"  {tag}: vocab={len(v)}  added_tokens={len(added)}")

    ref_tag = "31b"
    ref_v, ref_added = vocabs[ref_tag]
    for tag in ("e4b(本地)", "e2b"):
        v, added = vocabs[tag]
        check(f"{tag} vs {ref_tag}: 词表大小一致", len(v) == len(ref_v),
              f"{len(v)} vs {len(ref_v)}")
        ids = random.Random(42).sample(sorted(ref_v), min(50000, len(ref_v)))
        mismatch = sum(1 for i in ids if v.get(i) != ref_v.get(i))
        check(f"{tag} vs {ref_tag}: 抽样 5 万 id 串一致", mismatch == 0, f"mismatch={mismatch}")
        check(f"{tag} vs {ref_tag}: added_tokens 一致", added == ref_added,
              "" if added == ref_added else f"{len(added)} vs {len(ref_added)}")

    print(f"\n===== 汇总 =====\n{PASS}/{PASS + FAIL} 通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
