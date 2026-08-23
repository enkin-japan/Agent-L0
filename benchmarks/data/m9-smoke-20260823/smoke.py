"""m9 server 冒烟：起 26b-4bit server → 看预建日志 → /metrics constraint_engine_ready
→ 一个带 tool_specs 的请求 → done 事件含 constrained_steps，failopen 计数仍 0。"""
import json, sys, time, os
from pathlib import Path
sys.path.insert(0, "/Volumes/enkin/project/Agent/backend/scripts")
import bench_baseline as bb

PORT = 14161
REPO = "mlx-community/gemma-4-26b-a4b-it-4bit"
LOG = Path(sys.argv[1])
key = bb.read_env_key()
proc, fh = bb.start_server(REPO, PORT, LOG, api_key=key, spec={"mode": "off"})
cli = bb.Client(PORT, key)
try:
    t0 = time.time()
    while not cli.health_ok():
        if proc.poll() is not None:
            print("server died"); sys.exit(2)
        if time.time() - t0 > 240:
            print("server not ready in 240s"); sys.exit(3)
        time.sleep(2)
    bb.log(f"ready in {time.time()-t0:.0f}s")
    m = cli.json_call("GET", "/metrics")
    print("metrics@start:", {k: m[k] for k in ("constraint_engine_ready", "constraint_failopen_total")})
    entry = bb.model_options(REPO)
    model_dir = bb.BACKEND_DIR / "models" / REPO.split("/")[-1]
    dialect, tok = bb.make_dialect(model_dir, entry)
    prompt = bb.build_user_prompt(dialect, bb.SENTINEL_TOOL_PROMPT, tools=[bb.SENTINEL_TOOL_SPEC])
    done = {}
    out = cli.generate(prompt, sampling={"max_tokens": 120, "temperature": 0.0},
                       features={"tool_specs": [bb.SENTINEL_TOOL_SPEC]}, done_out=done)
    text = out[1]
    print("text:", text[:300].replace("\n", "\\n"))
    print("done:", {k: done.get(k) for k in ("constrained_steps", "generation_tokens", "finish_reason")})
    m = cli.json_call("GET", "/metrics")
    print("metrics@end:", {k: m[k] for k in ("constraint_engine_ready", "constraint_failopen_total", "generations_total", "errors_total")})
finally:
    proc.terminate()
    try:
        proc.wait(40)
    except Exception:
        proc.kill()
    fh.close()
