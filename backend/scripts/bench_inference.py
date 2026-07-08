#!/usr/bin/env python3
"""推理加速收益基准（Phase 0 仪表口径，开发机/生产机通用）。

对运行中的 Agent 服务跑一组固定负载，记录每轮 generation_tps / token 去向 /
废轮数 / 投机接受率。用法：
  1) config 里关掉 constrained_tool_calls + speculative.mode=off，重启服务，
     python3 scripts/bench_inference.py baseline
  2) 打开两项，重启服务，
     python3 scripts/bench_inference.py enabled
  3) 两份 JSON 落在 /tmp/bench_{tag}.json，人工/脚本对比。
"""
import json
import sys
import time
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:14159/api/v1"

PARA = ("The archive room smelled of dust and cold iron. Rows of ledgers "
        "recorded a century of shipping manifests, each line written in the "
        "same patient copperplate hand, each page stamped with the harbor "
        "master's faded blue seal.")

CASES = [
    ("t1_free_zh", "用三四句话说明什么是二分查找。", {}),
    ("t2_tool", "现在的精确时间是几点几分几秒？请用工具查询后告诉我。", {}),
    ("t3_repeat", "Do NOT use any tools. In your reply text, repeat the following paragraph verbatim, twice:\n\n" + PARA, {}),
    ("t4_plan", "请用 update_plan 制定一个两步计划：第一步用工具查当前时间，"
                "第二步把时间汇报给我。然后按计划执行并逐步 complete_step 完成。", {}),
    ("t5_thinking", "9 的平方根加 16 的平方根等于多少？",
     {"enable_thinking": "true", "thinking_budget": "256"}),
]


def post_chat(sid, prompt, extra):
    fields = {"prompt": prompt, "enable_thinking": "false", "max_tokens": "2048"}
    fields.update(extra)
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f"{BASE}/chat/{sid}", data=data, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        body = json.loads(r.read())
    body["_wall_seconds"] = round(time.time() - t0, 2)
    return body


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "run"
    out = {}
    for name, prompt, extra in CASES:
        sid = f"bench-{tag}-{name}"
        try:
            r = post_chat(sid, prompt, extra)
            texts = [m.get("content", "") for m in r.get("response", [])
                     if m.get("role") == "assistant" and "content" in m]
            rec = {
                "status": r.get("status"),
                "wall_seconds": r["_wall_seconds"],
                "generation_tps": r.get("generation_tps"),
                "prompt_tokens": r.get("prompt_tokens"),
                "generations": r.get("generations"),
                "generation_tokens_turn": r.get("generation_tokens_turn"),
                "token_breakdown": r.get("token_breakdown"),
                "parse_error_count": r.get("parse_error_count"),
                "spec_drafted": r.get("spec_drafted"),
                "spec_accepted": r.get("spec_accepted"),
                "final_text": (texts[-1][:160] if texts else ""),
            }
        except Exception as e:
            rec = {"status": f"EXC {type(e).__name__}: {e}"}
        finally:
            try:
                urllib.request.urlopen(
                    urllib.request.Request(f"{BASE}/chat/{sid}", method="DELETE"),
                    timeout=30)
            except Exception:
                pass
        out[name] = rec
        acc = ""
        if rec.get("spec_drafted"):
            acc = f"  接受率={rec['spec_accepted']}/{rec['spec_drafted']}"
        print(f"[{name}] {rec.get('status')}  tps={rec.get('generation_tps')}  "
              f"wall={rec.get('wall_seconds')}s  gen={rec.get('generation_tokens_turn')}tok  "
              f"废轮={rec.get('parse_error_count')}{acc}")
        print(f"    breakdown={rec.get('token_breakdown')}")
        print(f"    text={rec.get('final_text', '')[:100]!r}")

    path = f"/tmp/bench_{tag}.json"
    json.dump(out, open(path, "w"), ensure_ascii=False, indent=1)
    print(f"\n结果已存 {path}")


if __name__ == "__main__":
    main()
