#!/usr/bin/env python3
"""inference-server 协议契约测试（黑盒，模型无关；CUDA 兼容波的验收门禁）。

对一台**正在运行**的 inference-server 逐条断言协议行为——不关心生成质量，只钉
协议形状：换后端（mlx→cuda）时"服务一行不动"的承诺由本脚本背书。schema_builder
事故（S.9：协议面差一个参数、故障面在千里之外）的教训：新推理承载上线，第一件
事就是跑这个。

用法：
  backend/.venv/bin/python backend/scripts/check_infer_contract.py \
      --base-url http://127.0.0.1:14161 [--key $INFER_API_KEY]
key 缺省取环境变量 INFER_API_KEY；服务未启鉴权时跳过 401 组。
"""
import argparse
import json
import os
import sys
import time

import requests

FAIL = []


def check(name: str, ok: bool, detail: str = ""):
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)


def sse_events(resp, limit_s: float = 120.0):
    """解析 SSE 流为 (event, data_dict) 序列。"""
    t0 = time.time()
    event = None
    for raw in resp.iter_lines(decode_unicode=True):
        if time.time() - t0 > limit_s:
            break
        if raw is None:
            continue
        if raw.startswith("event: "):
            event = raw[len("event: "):].strip()
        elif raw.startswith("data: ") and event:
            try:
                yield event, json.loads(raw[len("data: "):])
            except ValueError:
                yield event, {}
            event = None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--key", default=os.environ.get("INFER_API_KEY", ""))
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    H = {"Authorization": f"Bearer {a.key}"} if a.key else {}

    # ① 探活：200 即模型已加载（lifespan 完成才受理）；身份字段是执行端护栏的
    #   契约面（分离部署缺陷二）——服务端哪天不再盖戳，这里要红，不能等执行端拒启才发现
    r = requests.get(f"{base}/health", timeout=30)
    check("health 200 即就绪", r.status_code == 200, f"got {r.status_code}")
    h = r.json() if r.status_code == 200 else {}
    check("health 报模型身份（service+model_repo）",
          h.get("service") == "macagent-inference" and bool(h.get("model_repo")),
          f"got {h!r}"[:120])

    # ② 鉴权：错钥/缺钥必须 401（服务未启鉴权时跳过）
    if a.key:
        r = requests.get(f"{base}/metrics", timeout=10)
        check("缺钥 401", r.status_code == 401, f"got {r.status_code}")
        r = requests.get(f"{base}/metrics",
                         headers={"Authorization": "Bearer wrong-key-0000"}, timeout=10)
        check("错钥 401", r.status_code == 401, f"got {r.status_code}")
    else:
        print("⚠ 未提供 key：跳过 401 组（生产必须启鉴权）")

    # ③ 缓存生命周期：new → clone（允许 501=冷路回退语义）→ release
    r = requests.post(f"{base}/v1/cache", headers=H, timeout=30)
    check("cache new", r.status_code == 200 and "cache_id" in r.json(),
          f"{r.status_code} {r.text[:80]}")
    cid = r.json().get("cache_id", "")
    r = requests.post(f"{base}/v1/cache/{cid}/clone", headers=H, timeout=60)
    check("cache clone（200 或 501=冷路回退）", r.status_code in (200, 501),
          f"got {r.status_code}")
    if r.status_code == 200:
        cid2 = r.json().get("cache_id", "")
        check("clone 得新 id", bool(cid2) and cid2 != cid)
        requests.delete(f"{base}/v1/cache/{cid2}", headers=H, timeout=30)
    r = requests.delete(f"{base}/v1/cache/{cid}", headers=H, timeout=30)
    check("cache release", r.status_code == 200, f"got {r.status_code}")

    # ④ 生成 SSE：meta（gen_id+cache_id）→ chunk*（字段齐全）→ done 终态
    body = {"prompt": "Say OK.", "sampling": {"max_tokens": 16, "temperature": 0.0}}
    with requests.post(f"{base}/v1/generate", headers=H, json=body,
                       stream=True, timeout=300) as r:
        check("generate 200 SSE", r.status_code == 200
              and "text/event-stream" in r.headers.get("content-type", ""))
        events = list(sse_events(r, limit_s=240))
    kinds = [k for k, _ in events]
    check("首事件 meta 携 gen_id+cache_id",
          bool(events) and kinds[0] == "meta"
          and {"gen_id", "cache_id"} <= set(events[0][1]))
    chunks = [d for k, d in events if k == "chunk"]
    check("至少一个 chunk", len(chunks) >= 1, f"kinds={kinds}")
    need = {"text", "delta", "prompt_tokens", "generation_tokens",
            "prompt_tps", "generation_tps", "peak_memory", "extra"}
    check("chunk 字段齐全", bool(chunks) and all(need <= set(c) for c in chunks),
          f"missing={need - set(chunks[0]) if chunks else 'no chunk'}")
    check("token 计数单调不减",
          all(chunks[i]["generation_tokens"] <= chunks[i + 1]["generation_tokens"]
              for i in range(len(chunks) - 1)))
    check("终态 done", bool(kinds) and kinds[-1] == "done", f"kinds[-1]={kinds[-1:]}")
    reuse_cid = events[0][1].get("cache_id", "") if events else ""

    # ⑤ 缓存续用：带上一轮 cache_id 再生成，meta 回报同一 id（未被 TTL 回收时）
    body2 = {"prompt": "Say OK again.", "cache_id": reuse_cid,
             "sampling": {"max_tokens": 8, "temperature": 0.0}}
    with requests.post(f"{base}/v1/generate", headers=H, json=body2,
                       stream=True, timeout=300) as r:
        ev2 = list(sse_events(r, limit_s=240))
    check("缓存续用回报同 id",
          bool(ev2) and ev2[0][1].get("cache_id") == reuse_cid)

    # ⑥ 取消：长生成中途 cancel → 流应尽快收场且无 error 事件（断连即语义，无 done）
    body3 = {"prompt": "Count from 1 to 10000, one number per line.",
             "sampling": {"max_tokens": 2048}}
    t0 = time.time()
    with requests.post(f"{base}/v1/generate", headers=H, json=body3,
                       stream=True, timeout=300) as r:
        it = sse_events(r, limit_s=120)
        gen_id = ""
        for k, d in it:
            if k == "meta":
                gen_id = d.get("gen_id", "")
            if k == "chunk":
                break                      # 已在产出中 → 发取消
        rc = requests.post(f"{base}/v1/generate/{gen_id}/cancel", headers=H, timeout=10)
        check("cancel 受理", rc.status_code == 200 and rc.json().get("cancelled") is True,
              f"{rc.status_code} {rc.text[:80]}")
        tail = list(it)                    # 消费余流至服务端收场
    elapsed = time.time() - t0
    check("取消后流在 60s 内收场", elapsed < 60, f"{elapsed:.0f}s")
    check("取消路径无 error 事件", all(k != "error" for k, _ in tail))

    # ⑦ OpenAI 门面：/v1/models + 非流式 chat/completions
    r = requests.get(f"{base}/v1/models", headers=H, timeout=30)
    check("facade models", r.status_code == 200 and r.json().get("data"),
          f"{r.status_code}")
    model_id = (r.json().get("data") or [{}])[0].get("id", "")
    r = requests.post(f"{base}/v1/chat/completions", headers=H, timeout=300, json={
        "model": model_id, "stream": False, "max_tokens": 16,
        "messages": [{"role": "user", "content": "Say OK."}]})
    ok = r.status_code == 200 and (r.json().get("choices") or [{}])[0] \
        .get("message", {}).get("content") is not None
    check("facade chat/completions 非流式", ok, f"{r.status_code} {r.text[:120]}")

    print("\n" + ("🎉 契约全过" if not FAIL else f"❌ {len(FAIL)} 项失败: {FAIL}"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
