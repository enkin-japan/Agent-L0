#!/usr/bin/env python3
"""Ornith-1.5 身份探针（阶段复审 MJ-6：三份文档断言「自称 Claude」却无原始件可核）。

走生产装载路（text_only_vlm 补丁 + qwen35 同款 chat template），两问各存完整输出到
identity_probe.json。确定性口径：greedy（temperature=0）。
"""
import json
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
BACKEND = DIR.parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from framework.backends.mlx.patch.text_only_vlm import load_model_and_processor  # noqa: E402

MODEL = BACKEND / "models" / "Ornith-1.5-35B-A3B-MLX-4bit"
PROMPTS = ["你是谁？谁开发了你？", "Who are you and who created you?"]


def main():
    from mlx_vlm import generate
    model, proc = load_model_and_processor(MODEL)
    out = {"model": str(MODEL.name), "sampling": {"temperature": 0.0, "max_tokens": 400},
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "probes": []}
    for q in PROMPTS:
        prompt = proc.tokenizer.apply_chat_template(
            [{"role": "user", "content": q}], tokenize=False, add_generation_prompt=True)
        r = generate(model, proc, prompt, max_tokens=400, temperature=0.0, verbose=False)
        text = r.text if hasattr(r, "text") else str(r)
        out["probes"].append({"q": q, "output": text})
        print("Q:", q, "\nA:", text[:300], "\n---", flush=True)
    (DIR / "identity_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                                             encoding="utf-8")
    print("saved identity_probe.json")


if __name__ == "__main__":
    main()
