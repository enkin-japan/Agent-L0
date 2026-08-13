#!/usr/bin/env python3
"""§7 阶段一 · 基线格测量序架（EXECUTION_LEDGER §7-1/§7-2，2026-08-13 生产机）。

口径（§7-1，违者数据作废）：
- server 进程态：本脚本自管 inference/server.py 生命周期（每模型一起一收），
  经 /v1/generate 打**带真模板**的 prompt（方言 PromptBuilder 构造，与生产同路）；
- 引擎权威计时：prompt_tps / generation_tps / peak_memory 取自生成链末 chunk
  （mlx GenerationResult 透传），TTFT 客户端记录；
- 每格 n≥3 取中位，首跑（含全量 prefill/JIT）单列不计中位；
- KV 真实物化：首跑全量 prefill 落缓存，后续 run 同 cache_id 前缀复用（KV 在位，
  decode 读全长）；
- 每格质量哨：数到十 / 知识问答 / tool_call 探针（带 tool_specs 约束路径）；
- 上下文档：0/4k/8k/16k/32k/64k，确定性填充按 tokenizer 计数配平（±2% 验收）；
- 基线格前提：config speculative.mode=off + kv_bits 缺省稠密——脚本启动即校验，
  不符拒跑（防「测了个投机采样基线」的静默口径污染）。

用法（backend/ 下）：
  .venv/bin/python scripts/bench_baseline.py --out /path/to/baseline.jsonl
  可选：--models repo1,repo2  --ctx 0,4096  --runs 3  --port 14161
断点续跑：--out 已有完整格自动跳过。
"""
import argparse
import http.client
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from framework.utils.config_loader import load_config          # noqa: E402
from framework.utils.model_selection import model_options       # noqa: E402
from framework.dialects import get_dialect_class                # noqa: E402

DEFAULT_MODELS = [
    "mlx-community/gemma-4-e4b-it-4bit",
    "mlx-community/gemma-4-e4b-it-8bit",
    "mlx-community/gemma-4-26b-a4b-it-4bit",
    "mlx-community/gemma-4-26b-a4b-it-8bit",
    "mlx-community/gemma-4-31b-it-4bit",
    "mlx-community/gemma-4-31b-it-8bit",
]
DEFAULT_CTX = [0, 4096, 8192, 16384, 32768, 65536]
MAX_TOKENS = 350
CTX_TOL = 0.02          # ±2%（§7-1）
INTER_CHUNK_TIMEOUT = 120

DECODE_TASKS = [
    "请写一篇约六百字的说明文，介绍潮汐现象的成因与规律。",
    "请写一篇约六百字的说明文，介绍光合作用的基本过程。",
    "请写一篇约六百字的说明文，介绍风的形成机制。",
    "请写一篇约六百字的说明文，介绍河流冲积平原的形成过程。",
]
SENTINEL_COUNT = "请用中文数字从一数到十，逗号分隔，只输出这一行。"
SENTINEL_QA = "水的化学式是什么？只回答化学式本身。"
SENTINEL_TOOL_PROMPT = ("你没有内置时钟，无法自行知道现在的时间。"
                        "请立即调用工具 get_current_time 查询当前时间，不要凭空回答。")
SENTINEL_TOOL_SPEC = {
    "name": "get_current_time",
    "description": "读取当前的日期与时间",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

# 确定性填充句池（含序号防退化压缩；内容百科体，与解码任务无 n-gram 依赖）
FILLER_SENTENCES = [
    "山脉由板块碰撞挤压隆起，风化与侵蚀又在漫长岁月里将其缓慢削平。",
    "河流自高处向低处汇集，携带泥沙在中下游沉积出宽阔的冲积平原。",
    "季风因海陆热力差异而生，夏季自海洋吹向陆地，冬季方向相反。",
    "土壤由母岩风化物、有机质、水分与空气构成，肥力取决于四者配比。",
    "洋流像传送带一样在大洋间输送热量，深刻影响沿岸地区的气候。",
    "森林通过蒸腾作用参与水循环，同时固定二氧化碳并释放氧气。",
    "冰川前进时刨蚀谷底与两壁，退缩后留下角峰、刃脊与冰碛湖。",
    "湿地被称为地球之肾，能够滞洪补枯、净化水质并养育众多物种。",
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_env_key() -> str:
    for line in (REPO_ROOT / ".env.local").read_text().splitlines():
        m = re.match(r"^INFER_API_KEY=(.+)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


# ---------- HTTP ----------

class Client:
    def __init__(self, port: int, api_key: str):
        self.port = port
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def _conn(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def json_call(self, method: str, path: str, body=None, timeout: float = 30):
        c = self._conn(timeout)
        try:
            c.request(method, path, body=json.dumps(body) if body is not None else None,
                      headers=self.headers)
            r = c.getresponse()
            data = r.read()
            if r.status >= 300:
                raise RuntimeError(f"{method} {path} -> {r.status}: {data[:200]!r}")
            return json.loads(data) if data else {}
        finally:
            c.close()

    def health_ok(self) -> bool:
        try:
            self.json_call("GET", "/health", timeout=5)
            return True
        except Exception:
            return False

    def generate(self, prompt: str, *, cache_id=None, sampling=None, features=None,
                 first_chunk_budget: float = 300.0):
        """SSE 消费 /v1/generate。返回 (chunks 末件, 全文, ttft, cache_id, wall)。
        看门狗：首 chunk 前按 prefill 预算，之后逐 chunk INTER_CHUNK_TIMEOUT。"""
        body = {"prompt": prompt, "sampling": sampling or {},
                # want_delta：不带则 delta 恒 None、text 只是 last_segment（分段重置），
                # 取全文必须靠 delta 累计（backend stream_generate 实读，2026-08-13）
                "features": {**(features or {}), "want_delta": True}}
        if cache_id:
            body["cache_id"] = cache_id
        c = self._conn(first_chunk_budget)
        t0 = time.time()
        try:
            c.request("POST", "/v1/generate", body=json.dumps(body), headers=self.headers)
            r = c.getresponse()
            if r.status >= 300:
                raise RuntimeError(f"/v1/generate -> {r.status}: {r.read()[:200]!r}")
            last, text, ttft, got_cache, event = None, "", None, cache_id, ""
            buf = b""
            while True:
                line = r.fp.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith(b"event:"):
                    event = line.split(b":", 1)[1].strip().decode()
                    continue
                if not line.startswith(b"data:"):
                    continue
                payload = json.loads(line.split(b":", 1)[1].strip())
                if event == "meta":
                    got_cache = payload.get("cache_id", got_cache)
                elif event == "chunk":
                    if ttft is None:
                        ttft = time.time() - t0
                        c.sock.settimeout(INTER_CHUNK_TIMEOUT)  # 切换到逐 chunk 看门狗
                    last = payload
                    text += payload.get("delta") or ""
                elif event == "error":
                    raise RuntimeError(f"engine error: {payload}")
                elif event == "done":
                    break
            return last, text, ttft, got_cache, time.time() - t0
        finally:
            c.close()


# ---------- prompt 构造 ----------

def make_dialect(model_dir: Path, entry: dict):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(str(model_dir))
    shim = SimpleNamespace(tokenizer=tok)
    dialect = get_dialect_class(str(entry.get("dialect") or "gemma4")).create(
        shim, options=entry)
    return dialect, tok


def build_user_prompt(dialect, text: str, tools=None) -> str:
    msgs = [{"role": "user", "content": text}]
    if tools:
        # 工具声明只在 system 轮后渲染（gemma4 PromptBuilder 行为，2026-08-13 实读）
        msgs.insert(0, {"role": "system", "content": "你是一个可以调用工具完成任务的助手。"})
    return dialect.build_prompt(messages=msgs, tools=tools, include_env=False)


def fit_filler(dialect, tok, target: int, task: str) -> str:
    """确定性填充配平：句池循环 + 序号前缀，二分句数直到 build 后 token 数进 ±2%。"""
    if target <= 0:
        return ""

    def text_of(n):
        parts = [f"第{i + 1}句：{FILLER_SENTENCES[i % len(FILLER_SENTENCES)]}"
                 for i in range(n)]
        return "背景资料（供后续问题参考）：\n" + "\n".join(parts) + "\n\n"

    def tokens_of(n):
        return len(tok(build_user_prompt(dialect, text_of(n) + task)).input_ids)

    lo, hi = 1, 64
    while tokens_of(hi) < target:
        lo, hi = hi, hi * 2
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if tokens_of(mid) < target:
            lo = mid
        else:
            hi = mid
    n = hi if abs(tokens_of(hi) - target) <= abs(tokens_of(lo) - target) else lo
    got = tokens_of(n)
    if abs(got - target) > target * CTX_TOL:
        raise RuntimeError(f"填充配平失败: 目标 {target} 实得 {got}")
    return text_of(n)


# ---------- server 生命周期 ----------

def start_server(repo: str, port: int, log_path: Path) -> subprocess.Popen:
    import os
    env = dict(os.environ)
    env.update({"INFER_MODEL_REPO": repo, "INFER_REMOTE_HOST": "127.0.0.1",
                "INFER_PORT": str(port), "PYTHONUNBUFFERED": "1"})
    lf = open(log_path, "ab")
    return subprocess.Popen(
        [str(BACKEND_DIR / ".venv/bin/python"), str(REPO_ROOT / "inference/server.py")],
        cwd=str(BACKEND_DIR), env=env, stdout=lf, stderr=subprocess.STDOUT)


def wait_health(cli: Client, proc: subprocess.Popen, budget: float = 1200):
    t0 = time.time()
    while time.time() - t0 < budget:
        if proc.poll() is not None:
            raise RuntimeError(f"server 提前退出 rc={proc.returncode}")
        if cli.health_ok():
            return time.time() - t0
        time.sleep(3)
    raise RuntimeError("server 健康检查超时")


def stop_server(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ---------- 质量哨 ----------

def run_sentinels(cli: Client, dialect, filler: str, cache_id, prefill_budget: float):
    out = {}
    # ① 数到十
    _, text, *_ = cli.generate(
        build_user_prompt(dialect, filler + SENTINEL_COUNT), cache_id=cache_id,
        sampling={"max_tokens": 80, "temperature": 0.0}, first_chunk_budget=prefill_budget)
    out["count"] = {"ok": all(z in text for z in ("一", "五", "十")), "sample": text[:120]}
    # ② 知识问答
    _, text, *_ = cli.generate(
        build_user_prompt(dialect, filler + SENTINEL_QA), cache_id=cache_id,
        sampling={"max_tokens": 40, "temperature": 0.0}, first_chunk_budget=prefill_budget)
    # 宽容判定：H₂O 可能以 LaTeX（$\text{H}_2\text{O}$）等形态出现，看骨架不看排版
    out["qa"] = {"ok": bool(re.search(r"H.{0,8}?2.{0,8}?O", text.upper())),
                 "sample": text[:80]}
    # ③ tool_call 探针（真模板带工具段 + tool_specs 约束路径）
    prompt = build_user_prompt(dialect, filler + SENTINEL_TOOL_PROMPT,
                               tools=[SENTINEL_TOOL_SPEC])
    _, text, *_ = cli.generate(
        prompt, cache_id=None,  # 工具段改变前缀，独立缓存
        sampling={"max_tokens": 120, "temperature": 0.0},
        features={"tool_specs": [SENTINEL_TOOL_SPEC]},
        first_chunk_budget=prefill_budget)
    out["tool"] = {"ok": ("get_current_time" in text and dialect.markers.stc in text),
                   "sample": text[:150]}
    return out


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--ctx", default=",".join(str(c) for c in DEFAULT_CTX))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--port", type=int, default=14161)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    models = [m for m in args.models.split(",") if m]
    ctx_levels = [int(x) for x in args.ctx.split(",")]
    out_path = Path(args.out)

    cfg = load_config()
    inf = cfg.get("inference") or {}
    spec_mode = str(((inf.get("speculative") or {}).get("mode")) or "off")
    if spec_mode != "off":
        sys.exit("❌ 基线格要求 config speculative.mode=off（当前 %r）——先关再跑" % spec_mode)
    if inf.get("kv_bits") is not None:
        sys.exit("❌ 基线格要求 KV 稠密（config 不得设 kv_bits）")

    done = set()
    if out_path.exists():
        for ln in out_path.read_text().splitlines():
            try:
                r = json.loads(ln)
                if r.get("type") == "cell" and r.get("complete"):
                    done.add((r["model"], r["ctx"]))
            except Exception:
                pass

    import platform
    from importlib.metadata import version as pkg_version
    import mlx_vlm  # noqa: F401  可导入性即环境自证
    header = {
        "type": "header", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": subprocess.run(["sysctl", "-n", "hw.model"], capture_output=True,
                                  text=True).stdout.strip(),
        "mem_gb": int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                     text=True).stdout.strip()) // 2**30,
        "wired_limit_mb": subprocess.run(["sysctl", "-n", "iogpu.wired_limit_mb"],
                                         capture_output=True, text=True).stdout.strip(),
        "macos": platform.mac_ver()[0],
        "mlx": pkg_version("mlx"), "mlx_vlm": pkg_version("mlx-vlm"),
        "speculative": spec_mode, "kv": "dense",
        "max_tokens": MAX_TOKENS, "runs": args.runs,
    }
    with out_path.open("a") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
    log(f"环境头已记：{header['machine']} {header['mem_gb']}G wired={header['wired_limit_mb']}MB "
        f"mlx={header['mlx']} spec={spec_mode}")

    cli = Client(args.port, read_env_key())

    for repo in models:
        todo = [c for c in ctx_levels if (repo, c) not in done]
        if not todo:
            log(f"⏭️  {repo} 全格已测，跳过")
            continue
        entry = model_options(repo)
        if not entry:
            log(f"❌ {repo} 不在注册表，跳过")
            continue
        model_dir = BACKEND_DIR / "models" / repo.split("/")[-1]
        weights_bytes = sum(p.stat().st_size for p in model_dir.glob("*.safetensors"))
        marker = json.loads((model_dir / ".download_complete").read_text())
        dialect, tok = make_dialect(model_dir, entry)

        log(f"🚀 起 server: {repo}（权重 {weights_bytes / 2**30:.1f}G）")
        slog = out_path.parent / f"server_{repo.split('/')[-1]}.log"
        proc = start_server(repo, args.port, slog)
        try:
            dt = wait_health(cli, proc, budget=1200)
            log(f"   server 就绪（{dt:.0f}s）")
            for ctx in todo:
                cell = {"type": "cell", "model": repo, "ctx": ctx,
                        "revision": marker.get("revision"),
                        "weights_bytes": weights_bytes, "runs": [], "complete": False,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                try:
                    task0 = DECODE_TASKS[0]
                    filler = fit_filler(dialect, tok, ctx, task0) if ctx else ""
                    prefill_budget = 180 + ctx / 40  # 40tps 最坏预算
                    cache_id = cli.json_call("POST", "/v1/cache", {})["cache_id"]
                    for i in range(args.runs + 1):  # +1 = 首跑（prefill/JIT，不计中位）
                        task = DECODE_TASKS[i % len(DECODE_TASKS)]
                        prompt = build_user_prompt(dialect, filler + task)
                        last, text, ttft, cache_id, wall = cli.generate(
                            prompt, cache_id=cache_id,
                            sampling={"max_tokens": MAX_TOKENS, "temperature": 0.0},
                            first_chunk_budget=prefill_budget if i == 0 else 240 + ctx / 200)
                        cell["runs"].append({
                            "idx": i, "ttft_s": round(ttft, 3) if ttft else None,
                            "wall_s": round(wall, 2),
                            "prompt_tokens": last.get("prompt_tokens") if last else None,
                            "prompt_tps": last.get("prompt_tps") if last else None,
                            "gen_tokens": last.get("generation_tokens") if last else None,
                            "gen_tps": last.get("generation_tps") if last else None,
                            "peak_mem_gb": last.get("peak_memory") if last else None,
                        })
                        log(f"   [{repo.split('/')[-1]} ctx={ctx}] run{i}: "
                            f"gen {cell['runs'][-1]['gen_tps']} tps, "
                            f"prefill {cell['runs'][-1]['prompt_tps']} tps, "
                            f"ttft {cell['runs'][-1]['ttft_s']}s")
                    # 口径校验：±2%（首跑的 prompt_tokens 为全量）
                    pt = cell["runs"][0]["prompt_tokens"] or 0
                    if ctx and abs(pt - ctx) > ctx * CTX_TOL:
                        cell["ctx_warning"] = f"prompt_tokens={pt} 偏离目标 >2%"
                    steady = [r["gen_tps"] for r in cell["runs"][1:] if r["gen_tps"]]
                    cell["decode_tps_median"] = round(statistics.median(steady), 2) if steady else None
                    cell["prefill_tps"] = cell["runs"][0]["prompt_tps"]
                    cell["ttft_full_s"] = cell["runs"][0]["ttft_s"]
                    cell["peak_mem_gb"] = max((r["peak_mem_gb"] or 0) for r in cell["runs"])
                    cell["sentinels"] = run_sentinels(cli, dialect, filler, cache_id,
                                                      prefill_budget)
                    cli.json_call("DELETE", f"/v1/cache/{cache_id}")
                    cell["complete"] = True
                    ok = all(v["ok"] for v in cell["sentinels"].values())
                    log(f"✅ 格 {repo.split('/')[-1]} × {ctx}: decode 中位 "
                        f"{cell['decode_tps_median']} tps, prefill {cell['prefill_tps']} tps, "
                        f"峰值 {cell['peak_mem_gb']}G, 质量哨 {'全过' if ok else '⚠️ 有挂'}")
                except Exception as e:
                    cell["error"] = f"{type(e).__name__}: {e}"
                    log(f"❌ 格 {repo.split('/')[-1]} × {ctx} 失败: {cell['error']}")
                with out_path.open("a") as f:
                    f.write(json.dumps(cell, ensure_ascii=False) + "\n")
        finally:
            stop_server(proc)
            log(f"🛑 收 server: {repo}")

    log("🏁 基线格全部完成")


if __name__ == "__main__":
    main()
