"""smoke2 残留 2 份追查的复现场：干净服务 + 三账互对。

三本账：
  A. 服务端访问日志（uvicorn 逐请求行，含 DELETE 路径里的 cache_id）
  B. 工装自己的 req.jsonl / summary.json（出生与退休记账）
  C. 旁路 2s 级 /metrics 采样（cache_count 精确出生/死亡时刻线）

判决叉路：
  server POST /v1/generate 行数 > 工装请求数 → 客户端有隐藏重复请求
  行数相等但 cache_count 出生更多            → 服务端单请求多出生
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND = Path("/Volumes/enkin/project/Agent/backend")
sys.path.insert(0, str(BACKEND / "scripts"))
from bench_baseline import (Client, read_env_key, start_server,  # noqa: E402
                            stop_server, wait_health)

REPO = "mlx-community/gemma-4-26b-a4b-it-4bit"
PORT = 14161
OUT = Path("/Volumes/enkin/project/Agent/benchmarks/data/soak-residue-repro-20260823")
OUT.mkdir(parents=True, exist_ok=True)


def sidecar(cli: Client, stop: threading.Event, path: Path) -> None:
    """2s 级 cache_count 时刻线。单次失败落 error 行——静默丢采样是 13:57 的老病。"""
    with path.open("w") as f:
        while not stop.is_set():
            try:
                m = cli.json_call("GET", "/metrics", timeout=5)
                rec = {"t": time.time(),
                       "cache_count": m.get("cache_count"),
                       "generations_total": m.get("generations_total"),
                       "cancelled_total": m.get("cancelled_total"),
                       "active_generations": m.get("active_generations")}
            except Exception as e:
                rec = {"t": time.time(), "error": f"{type(e).__name__}: {e}"[:120]}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            stop.wait(2.0)


def main() -> int:
    api_key = read_env_key()
    cli = Client(PORT, api_key)
    if cli.health_ok():
        print("❌ :14161 已有服务在跑——本实验要求干净服务，拒绝叠加", flush=True)
        return 2
    proc, lf = start_server(REPO, PORT, OUT / "server.log", api_key)
    try:
        dt = wait_health(cli, proc, budget=1200)
        print(f"server 就绪 {dt:.0f}s", flush=True)
        m0 = cli.json_call("GET", "/metrics")
        print(f"起跑基线 cache_count={m0.get('cache_count')}（干净服务应为 0）", flush=True)

        stop = threading.Event()
        th = threading.Thread(target=sidecar, args=(cli, stop, OUT / "cc.jsonl"),
                              daemon=True)
        th.start()
        try:
            rc = subprocess.call(
                [str(BACKEND / ".venv/bin/python"), str(BACKEND / "scripts/soak_longrun.py"),
                 "--smoke", "--outdir", str(OUT / "soak"), "--force",
                 "--port", str(PORT)],
                cwd=str(BACKEND))
            print(f"soak rc={rc}", flush=True)
        finally:
            stop.set()
            th.join(timeout=10)
        mf = cli.json_call("GET", "/metrics")
        (OUT / "final_metrics.json").write_text(json.dumps(mf, ensure_ascii=False))
        print(f"末态 cache_count={mf.get('cache_count')}", flush=True)
        return rc
    finally:
        stop_server(proc, lf)
        print("server 已收", flush=True)


if __name__ == "__main__":
    sys.exit(main())
