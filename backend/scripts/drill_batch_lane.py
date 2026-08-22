"""§7-0① 批处理泳道真机冒烟（e4b-8bit 缺省档，自管 server 生命周期）。

判据（全部满足才算过）：
① 2 路 batch_ok 并发请求都以 done 收尾，chunk 带 extra.batch_lane 标记，
   各自产出 ≥32 生成 token；
② 并发窗口内 /metrics 观察到 batch_lane.decoding ≥ 2 至少一次（真·同批共跑
   ——active 含引擎内排队，拿它当证据是假证，评审 C4），且 steps_total 递增；
③ 泳道有活时发一条**单流路**请求（不带 batch_ok）：其生成期间观察到
   batch_lane.paused=true 至少一次（交互让路真的发生），交互与批行都正常完结；
④ 回归：单流路请求行为正常（有文本、done 收尾）。
退出码：0=①②③④全真；3=判据不满足；1=环境/脚本错误。
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_baseline import (Client, build_user_prompt, make_dialect,  # noqa: E402
                            read_env_key, start_server, stop_server, wait_health)

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
from framework.utils.model_selection import model_options       # noqa: E402
from framework.utils.path_resolver import resolve_model_path    # noqa: E402

PORT = 14161
MODEL = "mlx-community/gemma-4-e4b-it-8bit"


def gen_batch(cli, prompt, out, max_tokens=64):
    try:
        last, text, ttft, _, wall = cli.generate(
            prompt, sampling={"temperature": 0, "max_tokens": max_tokens},
            features={"batch_ok": True})
        out.update({"ok": True, "last": last, "text": text, "wall": wall})
    except Exception as e:
        out.update({"ok": False, "err": f"{type(e).__name__}: {e}"})


def main() -> int:
    import os
    api_key = read_env_key()
    os.environ["INFER_MLX_MAX_CONCURRENCY"] = "3"
    # §7-1 口径：一律带真模板（裸 prompt 无模板 = 退化垃圾伪象，首轮冒烟实踩）
    entry = model_options(MODEL)
    dialect, _tok = make_dialect(Path(resolve_model_path(
        MODEL, revision=entry.get("revision"))), entry)
    _p = lambda text: build_user_prompt(dialect, text)  # noqa: E731
    log_path = BACKEND_DIR / "server-drill-batchlane.log"
    proc, lf = start_server(MODEL, PORT, log_path, api_key,
                            spec={"mode": "off"})   # 冒烟=非投机口径
    cli = Client(PORT, api_key)
    try:
        wait_health(cli, proc, budget=600)

        # ── ①② 两路并发 batch_ok ──
        r1, r2 = {}, {}
        t1 = threading.Thread(target=gen_batch, args=(cli, _p("请用三句话介绍一下长江。"), r1))
        t2 = threading.Thread(target=gen_batch, args=(cli, _p("请用三句话介绍一下黄河。"), r2))
        t1.start(); t2.start()
        seen_active2, seen_steps = 0, 0
        while t1.is_alive() or t2.is_alive():
            try:
                m = cli.json_call("GET", "/metrics", timeout=5).get("batch_lane") or {}
                seen_active2 = max(seen_active2, int(m.get("decoding") or 0))
                seen_steps = max(seen_steps, int(m.get("steps_total") or 0))
            except Exception:
                pass
            time.sleep(0.2)
        t1.join(); t2.join()

        ok1 = bool(r1.get("ok") and r2.get("ok"))
        gt1 = r1.get("last") and r1["last"].get("generation_tokens", 0) or 0
        gt2 = r2.get("last") and r2["last"].get("generation_tokens", 0) or 0
        marked = all(bool((r.get("last") or {}).get("extra", {}).get("batch_lane"))
                     for r in (r1, r2))
        ok1 = ok1 and gt1 >= 32 and gt2 >= 32 and marked
        ok2 = seen_active2 >= 2 and seen_steps > 0
        print(f"[drill] ① 双路完结={ok1} (gt={gt1}/{gt2}, 标记={marked}, "
              f"err1={r1.get('err')}, err2={r2.get('err')})")
        print(f"[drill] ② 同批共跑={ok2} (decoding峰值={seen_active2}, steps={seen_steps})")

        # ── ③ 交互让路：先塞一条长批行，再发单流路请求，其间盯 paused ──
        rb, ri = {}, {}
        tb = threading.Thread(target=gen_batch,
                              args=(cli, _p("请详细讲讲二十四节气的由来与含义。"), rb, 512))
        tb.start()
        time.sleep(2.0)                     # 批行先跑起来（含预填）

        def gen_interactive():
            try:
                last, text, ttft, cid, wall = cli.generate(
                    _p("数到十。"), sampling={"temperature": 0, "max_tokens": 48})
                ri.update({"ok": True, "text": text, "cid": cid})
            except Exception as e:
                ri.update({"ok": False, "err": f"{type(e).__name__}: {e}"})

        titer = threading.Thread(target=gen_interactive)
        titer.start()
        seen_paused = False
        while titer.is_alive():
            try:
                m = cli.json_call("GET", "/metrics", timeout=5).get("batch_lane") or {}
                seen_paused = seen_paused or bool(m.get("paused"))
            except Exception:
                pass
            time.sleep(0.1)
        titer.join(); tb.join()
        if ri.get("cid"):
            try:
                cli.json_call("DELETE", f"/v1/cache/{ri['cid']}")
            except Exception:
                pass
        ok3 = bool(ri.get("ok") and rb.get("ok") and seen_paused)
        ok4 = bool(ri.get("ok") and (ri.get("text") or "").strip())
        print(f"[drill] ③ 交互让路={ok3} (paused 观察到={seen_paused}, "
              f"交互 ok={ri.get('ok')}, 批行 ok={rb.get('ok')}, err={ri.get('err') or rb.get('err')})")
        print(f"[drill] ④ 单流路回归={ok4}")

        obs = {"ok1": ok1, "ok2": ok2, "ok3": ok3, "ok4": ok4,
               "decoding_peak": seen_active2, "steps": seen_steps,
               "gt": [gt1, gt2],
               "batch_text_head": [(r1.get("text") or "")[:60],
                                   (r2.get("text") or "")[:60]],
               "interactive_text": (ri.get("text") or "")[:80]}
        print(f"[drill] 观察: {json.dumps(obs, ensure_ascii=False)}")
        allok = ok1 and ok2 and ok3 and ok4
        print("[drill] 判定:", "批处理泳道端到端可用 ✅" if allok else "不通过 ❌")
        return 0 if allok else 3
    except Exception as e:
        print(f"[drill] 环境失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        stop_server(proc, lf)


if __name__ == "__main__":
    sys.exit(main())
