#!/usr/bin/env python3
"""§7 阶段一 · 基线格测量序架（EXECUTION_LEDGER §7-1/§7-2，2026-08-13 生产机）。

口径（§7-1，违者数据作废）：
- server 进程态：本脚本自管 inference/server.py 生命周期（v3 起每格一起一收），
  经 /v1/generate 打**带真模板**的 prompt（方言 PromptBuilder 构造，与生产同路）；
- 引擎权威计时：prompt_tps / generation_tps / peak_memory 取自生成链末 chunk
  （mlx GenerationResult 透传），TTFT 客户端记录；
- 每格 n≥3 取中位（--runs≥3；=2 仅告警，稳态样本退化为单点），首跑（含全量
  prefill/JIT）单列不计中位；
- KV 真实物化：**每 run 独立新缓存、全量 prefill 后测 decode**（v2 改法，2026-08-13）。
  v1 的同 cache 前缀复用被撤：26B(MoE)+suppress_ghost_thinking 在缓存复用后瞬间
  EOS（gen_tokens=1）、31B 输出头部窜出孤立 <channel|>——引擎级复用 bug 另案排查，
  测量先绕开；副产品是 prefill tps 也有 n≥3 中位；
- 每格质量哨：数到十 / 知识问答 / tool_call 探针（带 tool_specs 约束路径）；
- 上下文档：0/4k/8k/16k/32k/64k，确定性填充按 tokenizer 计数配平（±2% 验收）；
- 基线格前提：config speculative.mode=off + kv_bits 缺省稠密——脚本启动即校验，
  不符拒跑（防「测了个投机采样基线」的静默口径污染）；
- v3 口径变更（2026-08-14，与已入账的 v1/v2 两轮不可直接并表）：采样显式钉死
  temperature=0 / min_p=0.0 / repetition_penalty=None（真裸贪心：None 才不装
  重复惩罚 logits 处理器；v2 走 backend 缺省 rep_penalty=1.1，**这是 v2/v3
  不可并表的真实轴**——temp=0 下 min_p 本被 argmax 忽略，0.05→0.0 无行为差）；
  且 server 改为**每格**一起一收（峰值内存口径回到格级）。注：环境自证不再靠
  import mlx_vlm（父进程导入会在 server 旁开第二个 Metal 上下文），版本号走
  distribution 元数据，导入级损坏会以首格 server 起不来的形式暴露。

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
from typing import Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from framework.utils.config_loader import load_config          # noqa: E402
from framework.utils.model_selection import model_options       # noqa: E402
from framework.dialects import get_dialect_class                # noqa: E402

DEFAULT_MODELS = [  # D.20 阵容收束（2026-08-16）：缺省臂=保留六档；表外档「跳过」是响亮日志但仍属静默缩臂，改名单而非依赖跳过
    "mlx-community/gemma-4-e4b-it-8bit",
    "mlx-community/gemma-4-26b-a4b-it-4bit",
    "mlx-community/gemma-4-26b-a4b-it-8bit",
    "mlx-community/gemma-4-31b-it-4bit",
    "mlx-community/Qwen3.8-27B-4bit",
    "mlx-community/Qwen3.8-27B-8bit",
]
DEFAULT_CTX = [0, 4096, 8192, 16384, 32768, 65536]
MAX_TOKENS = 350
CTX_TOL = 0.02          # ±2%（§7-1）
INTER_CHUNK_TIMEOUT = 120
# 采样口径：真裸贪心，显式钉死——不钉则 backend 缺省（min_p=0.05/rep_penalty=1.1）
# 悄悄定义基线，换版即漂。repetition_penalty 必须是 None 而非 1.0：
# make_logits_processors 只在 None/0 时跳过处理器，1.0 仍装（白花每 token 的巡检成本）
SAMPLING_BASE = {"temperature": 0.0, "min_p": 0.0, "repetition_penalty": None}

DECODE_TASKS = [
    "请写一篇约六百字的说明文，介绍潮汐现象的成因与规律。",
    "请写一篇约六百字的说明文，介绍光合作用的基本过程。",
    "请写一篇约六百字的说明文，介绍风的形成机制。",
    "请写一篇约六百字的说明文，介绍河流冲积平原的形成过程。",
]
SENTINEL_COUNT = "请用中文数字从一数到十，逗号分隔，只输出这一行。"
SENTINEL_QA = "水的化学式是什么？只回答化学式本身。"
# 工具哨用**天气**而非原来的 get_current_time（2026-08-14 换，附完整证据）。
# 原探针与 prompt 尾部的环境锚点行 `Current date: …` 语义打架：模型被告知"你没有
# 时钟，去调工具查时间"，紧接着又被塞了当天日期，于是在思考频道里空转不调用。
# 隔离实验（12b-4bit @4k，单变量，各跑一次）：
#   时间工具 × 尾部日期锚 ❌ | 时间工具 × 尾部无关句 ✅ | 时间工具 × 尾部地点锚 ✅
#   天气工具 × 尾部日期锚 ✅ | 天气工具 × 尾部天气锚 ✅
# 即：既不是"尾部有 system 轮"的问题（换内容就通），也不是"锚点满足了请求"的
# 泛化效应（天气锚 × 天气工具照样通），是日期/时间这一对特有的冲突。
# 留着它，工具列测的就不是工具调用能力，而是"模型顶不顶得住我们自己塞的矛盾"。
# 顺带收益：带一个字符串实参，正好压到 §7-8 紧凑 DSL 的 <|"|> 原文块编码路径。
SENTINEL_TOOL_PROMPT = ("你没有联网能力，无法自行知道天气。"
                        "请立即调用工具 get_weather 查询东京的天气，不要凭空回答。")
SENTINEL_TOOL_SPEC = {
    "name": "get_weather",
    "description": "查询指定城市的当前天气",
    "parameters": {"type": "object",
                   "properties": {"city": {"type": "string", "description": "城市名"}},
                   "required": ["city"]},
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


def sysctl(name: str):
    """读 sysctl 标量；读不到返回 None（环境头缺一项不值得整跑崩在起跑线）。"""
    try:
        r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                           timeout=10)
        return r.stdout.strip() or None
    except Exception as e:
        log(f"⚠️  sysctl {name} 读取失败：{type(e).__name__}: {e}")
        return None


def parse_skips(spec: str) -> set:
    """--skip 解析：<repo尾名>:<ctx>。格式不合即报错——静默忽略等于「以为跳了其实在跑」。"""
    out = set()
    for it in (spec or "").split(","):
        it = it.strip()
        if not it:
            continue
        if ":" not in it:
            raise ValueError(f"--skip 项 {it!r} 缺冒号，应为 <repo尾名>:<ctx>")
        name, c = it.rsplit(":", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"--skip 项 {it!r} 模型名为空")
        try:
            out.add((name, int(c.strip())))
        except ValueError:
            raise ValueError(f"--skip 项 {it!r} 的 ctx {c.strip()!r} 不是整数") from None
    return out


def check_runs(runs: int) -> None:
    """--runs 门槛：首跑不计中位，故 <2 连一个稳态样本都没有；==2 的「中位」是单点。"""
    if runs < 2:
        raise ValueError(f"--runs 至少为 2（首跑单列不计中位），当前 {runs}")
    if runs == 2:
        log("⚠️  --runs=2：稳态样本仅 1 个，中位=单点，建议 ≥3")


def med(rows, key):
    """中位 + 样本数。仅丢 None（缺测），0.0 是合法测量值（如亚毫秒 TTFT 圆整）；
    丢了几个必须随值落账，否则「3 次中位」与「1 次单点」在产物里长得一模一样。"""
    vals = [r[key] for r in rows if r.get(key) is not None]
    return (round(statistics.median(vals), 2) if vals else None), len(vals)


def drop_cache(cli, holder) -> None:
    """删服务端 KV 缓存；失败留证（吞掉 = 长上下文格一路泄到 server 收摊）。"""
    cid = (holder or {}).get("cache_id")
    if not cid:
        return
    try:
        cli.json_call("DELETE", f"/v1/cache/{cid}")
    except Exception as e:
        log(f"⚠️  KV 缓存 {cid} 删除失败：{type(e).__name__}: {e}")


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
                 first_chunk_budget: float = 300.0, cache_out: dict = None):
        """SSE 消费 /v1/generate。返回 (chunks 末件, 全文, ttft, cache_id, wall)。
        看门狗：首 chunk 前按 prefill 预算，之后逐 chunk INTER_CHUNK_TIMEOUT。
        cache_out：meta 事件一到就把 cache_id 写进去——异常路径（超时/error/EOF/坏 JSON）
        的服务端缓存靠它才删得掉，否则 id 随异常一起丢，KV 泄到 server 收摊为止。"""
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
            n_chunk, done = 0, False
            while True:
                # r.readline()（非 r.fp）：HTTPResponse 才会解 chunked 传输编码
                line = r.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith(b"event:"):
                    event = line.split(b":", 1)[1].strip().decode()
                    continue
                if not line.startswith(b"data:"):
                    continue
                payload = json.loads(line.split(b":", 1)[1].strip())
                kind, event = event, ""  # 消费即清：野 data 行不许挂到上一事件名下
                if kind == "meta":
                    got_cache = payload.get("cache_id", got_cache)
                    if cache_out is not None and got_cache:
                        cache_out["cache_id"] = got_cache
                elif kind == "chunk":
                    if ttft is None:
                        ttft = time.time() - t0
                        c.sock.settimeout(INTER_CHUNK_TIMEOUT)  # 切换到逐 chunk 看门狗
                    last = payload
                    n_chunk += 1
                    text += payload.get("delta") or ""
                elif kind == "error":
                    raise RuntimeError(f"engine error: {payload}")
                elif kind == "done":
                    done = True
                    break
            if not done:
                # socket EOF 早于 done 事件 = 生成被腰斩，不是正常收尾：
                # 当成功处理会把半截数据当满格入账（末 chunk 的 tps 也是半截口径）
                raise RuntimeError(
                    f"SSE 流在 done 事件前断开（EOF）：已收 {n_chunk} chunk / "
                    f"{(last or {}).get('generation_tokens')} token")
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

def start_server(repo: str, port: int, log_path: Path, api_key: str = "",
                 spec: Optional[dict] = None):
    """起 server 子进程，返回 (proc, 日志文件句柄)。
    INFER_API_KEY 显式下发 read_env_key() 的同一把钥匙——两侧各读各的必然有对不上的一天。
    spec：MTP 格的投机配置，经 INFER_SPEC_* 下发（server 侧 env 非空即赢）。"""
    import os
    env = dict(os.environ)
    env.update({"INFER_MODEL_REPO": repo, "INFER_REMOTE_HOST": "127.0.0.1",
                "INFER_PORT": str(port), "PYTHONUNBUFFERED": "1",
                # .env.local 无钥时回落继承的环境钥——无条件覆写空串会把子进程
                # 鉴权静默关掉（回环绑定，bind 护栏拦不住这形态）
                "INFER_API_KEY": api_key or env.get("INFER_API_KEY", "")})
    # 显式键名映射 + **无条件清洗**：只加不清会让操作者 shell 里遗留的
    # INFER_SPEC_* 悄悄把一次基线跑变成投机跑，而产物头部仍自称 off——
    # 那是所有格的分母，是最坏的一种污染
    _SPEC_ENV = {"mode": "INFER_SPEC_MODE",
                 "draft_model": "INFER_SPEC_DRAFT_MODEL",
                 "block_size": "INFER_SPEC_BLOCK_SIZE"}
    for key, name in _SPEC_ENV.items():
        if spec and key in spec:
            env[name] = str(spec[key])
        else:
            env.pop(name, None)
    lf = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            [str(BACKEND_DIR / ".venv/bin/python"), str(REPO_ROOT / "inference/server.py")],
            cwd=str(BACKEND_DIR), env=env, stdout=lf, stderr=subprocess.STDOUT)
    except Exception:
        lf.close()
        raise
    return proc, lf


def wait_health(cli: Client, proc: subprocess.Popen, budget: float = 1200):
    t0 = time.time()
    while time.time() - t0 < budget:
        if proc.poll() is not None:
            raise RuntimeError(f"server 提前退出 rc={proc.returncode}")
        if cli.health_ok():
            return time.time() - t0
        time.sleep(3)
    raise RuntimeError("server 健康检查超时")


def stop_server(proc: subprocess.Popen, logfile=None):
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    if logfile is not None:      # 每格一起一收 = 每格一个句柄，不关就攒着
        try:
            logfile.close()
        except Exception:
            pass


# ---------- 质量哨 ----------

def run_sentinels(cli: Client, dialect, filler: str, cache_id, prefill_budget: float):
    out = {}
    def fresh_gen(prompt, max_tokens, features=None):
        """每哨独立新缓存（v2：复用 bug 绕行），用完即删。
        返回 (全文, 末 chunk)——末件里的 gen_tokens 是「瞬间 EOT(=1)」与「答歪了」的
        唯一分辨依据，只回文本等于把两种失败糊成一种。"""
        holder = {}
        try:
            last, text, ttft, cid, wall = cli.generate(
                prompt, cache_id=None,
                sampling={**SAMPLING_BASE, "max_tokens": max_tokens},
                features=features, first_chunk_budget=prefill_budget,
                cache_out=holder)
        finally:
            drop_cache(cli, holder)
        return text, last

    # ① 数到十（中文或阿拉伯数字序列均可——判"退化与否"不判排版偏好）
    text, last = fresh_gen(build_user_prompt(dialect, filler + SENTINEL_COUNT), 80)
    ok = all(z in text for z in ("一", "五", "十")) or \
        bool(re.search(r"1\D{1,4}2\D.*?5\D.*?10", text))
    out["count"] = {"ok": ok, "sample": text[:120],
                    "gen_tokens": (last or {}).get("generation_tokens"),
                    "prompt_tokens": (last or {}).get("prompt_tokens")}
    # ② 知识问答（宽容判定：H₂O/下标/LaTeX 形态均收，看骨架不看排版）
    text, last = fresh_gen(build_user_prompt(dialect, filler + SENTINEL_QA), 40)
    norm = text.replace("₂", "2").replace("$", "").upper()
    out["qa"] = {"ok": bool(re.search(r"H.{0,8}?2.{0,8}?O", norm)), "sample": text[:80],
                 "gen_tokens": (last or {}).get("generation_tokens"),
                 "prompt_tokens": (last or {}).get("prompt_tokens")}
    # ③ tool_call 探针（真模板带工具段 + tool_specs 约束路径）
    prompt = build_user_prompt(dialect, filler + SENTINEL_TOOL_PROMPT,
                               tools=[SENTINEL_TOOL_SPEC])
    text, last = fresh_gen(prompt, 120, features={"tool_specs": [SENTINEL_TOOL_SPEC]})
    # 判据加严：除了发出调用，实参也必须到位——§7-8 迁移后实参走 <|"|> 原文块，
    # 只查工具名会漏掉"调用发了但参数编码坏了"这一整类。
    out["tool"] = {"ok": (SENTINEL_TOOL_SPEC["name"] in text
                          and dialect.markers.stc in text
                          and "东京" in text),
                   "sample": text[:150],
                   "gen_tokens": (last or {}).get("generation_tokens"),
                   "prompt_tokens": (last or {}).get("prompt_tokens")}
    return out


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--ctx", default=",".join(str(c) for c in DEFAULT_CTX))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--port", type=int, default=14161)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip", default="",
                    help="逗号分隔的 <repo尾名>:<ctx> 已证不可行格（如 OOM 实证），跳过不测")
    # ---- MTP 格（§7 阶段二）：投机配置逐格下发给 server，不改 config.yaml ----
    ap.add_argument("--spec-mode", default="",
                    help="投机模式：留空=用 config（生产缺省 off）；drafter=草稿件投机")
    ap.add_argument("--draft-model", default="",
                    help="草稿件目录或 HF id（--spec-mode=drafter 时必给）")
    ap.add_argument("--draft-block-size", type=int, default=None,
                    help="草稿块大小（单值正整数）；留空=用草稿件自带值。k 扫描请分次跑"
                         "本脚本并落到不同 --out，避免把两种 k 的 run 混进同一格中位")
    ap.add_argument("--sentinels-only", action="store_true",
                    help="只跑质量哨，跳过解码 run 与中位（§S.16 修复后的质量重跑："
                         "速度数据未受污染，无需重烧解码段）")
    args = ap.parse_args()

    models = [m for m in args.models.split(",") if m]
    ctx_levels = [int(x) for x in args.ctx.split(",")]

    # 投机配置：只在显式给了 --spec-mode 时下发；否则一个 INFER_SPEC_* 都不设，
    # server 走 config.yaml 原路（= 生产缺省 off），基线格行为逐字不变。
    spec_env = {}
    _mode = args.spec_mode.strip()
    if _mode:
        # 白名单：模式名写错（如 "mtp"）会一路静默跑成非投机，而产物头部与格记录
        # 都自称投机格——自证口径的字段证了个假，比没有还坏
        if _mode not in ("off", "prompt_lookup", "drafter"):
            ap.error(f"--spec-mode 只认 off|prompt_lookup|drafter，收到 {_mode!r}")
        spec_env["mode"] = _mode
        if _mode == "drafter":
            if not args.draft_model:
                ap.error("--spec-mode=drafter 必须同时给 --draft-model")
            spec_env["draft_model"] = args.draft_model
        if args.draft_block_size is not None:
            if args.draft_block_size <= 0:
                ap.error("--draft-block-size 必须为正整数")
            spec_env["block_size"] = args.draft_block_size
    elif args.draft_model or args.draft_block_size is not None:
        ap.error("--draft-model / --draft-block-size 需与 --spec-mode 同时给"
                 "——单给它们不会生效，静默跑成非投机格是最坏结果")
    try:
        skips = parse_skips(args.skip)
        check_runs(args.runs)
    except ValueError as e:
        ap.error(str(e))
    out_path = Path(args.out)

    cfg = load_config()
    inf = cfg.get("inference") or {}
    # 护栏看 **config** 值、环境头记 **生效** 值——两者不是一回事，混成一个变量会
    # 让 --spec-mode 一给就被基线护栏打死（且错误信息还指着 config 让人去改生产文件）
    cfg_spec_mode = str(((inf.get("speculative") or {}).get("mode")) or "off")
    if cfg_spec_mode != "off":
        sys.exit("❌ 本脚本要求 config speculative.mode=off（当前 %r）——投机格请用 "
                 "--spec-mode 下发，不要改生产 config" % cfg_spec_mode)
    spec_mode = str(spec_env.get("mode") or cfg_spec_mode)
    if inf.get("kv_bits") is not None:
        sys.exit("❌ 基线格要求 KV 稠密（config 不得设 kv_bits）")

    done = set()
    if out_path.exists():
        for ln in out_path.read_text().splitlines():
            try:
                r = json.loads(ln)
                # 口径必须同款才算「已测」：哨兵格里没有 decode/prefill/峰值，
                # 拿它顶一个全格会让续跑静默跳过、产出一份缺列的 jsonl
                # 投机配置同样是口径的一部分：不比对它，一份基线 jsonl 会把
                # 非投机格顶给 MTP 跑（反之亦然），也会让两个 k 的格互相顶掉，
                # 最后产出一份「看着完整、实则混口径」的文件
                if (r.get("type") == "cell" and r.get("complete")
                        and bool(r.get("sentinels_only")) == bool(args.sentinels_only)
                        and (r.get("spec") or {}) == spec_env):
                    done.add((r["model"], r["ctx"]))
            except Exception:
                pass

    import platform
    from importlib.metadata import version as pkg_version
    mem_raw = sysctl("hw.memsize")
    try:
        mem_gb = int(mem_raw) // 2**30
    except (TypeError, ValueError):
        log(f"⚠️  hw.memsize 不可解析（{mem_raw!r}），环境头 mem_gb 记 None")
        mem_gb = None
    header = {
        "type": "header", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": sysctl("hw.model"),
        "mem_gb": mem_gb,
        "wired_limit_mb": sysctl("iogpu.wired_limit_mb"),
        "macos": platform.mac_ver()[0],
        "mlx": pkg_version("mlx"), "mlx_vlm": pkg_version("mlx-vlm"),
        "speculative": spec_mode, "spec_config": dict(spec_env), "kv": "dense",
        "max_tokens": MAX_TOKENS, "runs": args.runs,
        # 口径自证：哨兵口径的文件里没有解码段，header 不写清楚会让读者
        # 以为 runs/max_tokens 真跑过
        "sentinels_only": bool(args.sentinels_only),
    }
    with out_path.open("a") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
    log(f"环境头已记：{header['machine']} {header['mem_gb']}G wired={header['wired_limit_mb']}MB "
        f"mlx={header['mlx']} spec={spec_mode}")

    api_key = read_env_key()
    cli = Client(args.port, api_key)

    for repo in models:
        todo = [c for c in ctx_levels if (repo, c) not in done
                and (repo.split("/")[-1], c) not in skips]
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

        short = repo.split("/")[-1]
        for ctx in todo:
            cell = {"type": "cell", "model": repo, "ctx": ctx,
                    "revision": marker.get("revision"),
                    "weights_bytes": weights_bytes, "runs": [], "complete": False,
                    # 投机配置逐格落账：产物必须自描述，否则两次不同 k 的 jsonl
                    # 放在一起就再也分不出谁是谁（非投机跑此键为空 dict）
                    "spec": dict(spec_env),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            # 峰值内存口径=格级：peak_memory 是进程生命期高水位，一个 server 连跑多格
            # 会把前一格（尤其 64k）的高水位带进后一格 → server 每格一起一收
            slog = out_path.parent / f"server_{short}_ctx{ctx}.log"
            proc = lf = None
            started = launching = False
            try:
                # 配平是纯 tokenizer 活，放装载前——配平失败不值得白付一次模型装载，
                # 也别让模型驻着显存陪二分搜索
                task0 = DECODE_TASKS[0]
                filler = fit_filler(dialect, tok, ctx, task0) if ctx else ""
                prefill_budget = 180 + ctx / 40  # 40tps 最坏预算
                launching = True  # 配平已过——此后失败才可能是「server 起不来」

                log(f"🚀 起 server: {repo} ctx={ctx}（权重 {weights_bytes / 2**30:.1f}G）")
                proc, lf = start_server(repo, args.port, slog, api_key, spec=spec_env)
                dt = wait_health(cli, proc, budget=1200)
                started = True
                log(f"   server 就绪（{dt:.0f}s）")
                # 质量重跑口径：只跑哨兵段。server 仍每格一起一收（少省那点装载时间
                # 不值得把 v1 的跨格共享 server 状态请回来——prefill 才是大头）
                for i in ([] if args.sentinels_only
                          else range(args.runs + 1)):  # +1 = 首跑（JIT 热身，不计中位）
                    task = DECODE_TASKS[i % len(DECODE_TASKS)]
                    prompt = build_user_prompt(dialect, filler + task)
                    # v2：每 run 独立新缓存 + 全量 prefill（复用 bug 绕行，见文件头）
                    holder = {}
                    try:
                        last, text, ttft, run_cache, wall = cli.generate(
                            prompt, cache_id=None,
                            sampling={**SAMPLING_BASE, "max_tokens": MAX_TOKENS},
                            first_chunk_budget=prefill_budget, cache_out=holder)
                    finally:
                        drop_cache(cli, holder)
                    if ((last or {}).get("generation_tokens") or 0) <= 2:
                        raise RuntimeError(
                            f"run{i} 秒停（gen_tokens≤2）——复用 bug 形态在新缓存下复现？")
                    # 要投机却一个草稿都没出 = 这一格实际跑成了非投机，而格记录
                    # 里的 spec 字段仍自称投机。响亮拒收：口径自证的字段证了假，
                    # 比没有更坏（同 gen_tokens≤2 / ±2% 两处的处置风格）
                    if spec_env.get("mode") not in (None, "", "off") and \
                            ((last or {}).get("extra") or {}).get("spec_drafted") is None:
                        raise RuntimeError(
                            f"run{i} 要求 spec={spec_env.get('mode')} 却零草稿回吐"
                            "——投机未真正生效，本格数据不可用")
                    gen_tokens = last.get("generation_tokens") if last else None
                    prompt_tokens = last.get("prompt_tokens") if last else None
                    cell["runs"].append({
                        "idx": i, "ttft_s": round(ttft, 3) if ttft is not None else None,
                        "wall_s": round(wall, 2),
                        "prompt_tokens": prompt_tokens,
                        "prompt_tps": last.get("prompt_tps") if last else None,
                        "gen_tokens": gen_tokens,
                        "gen_tps": last.get("generation_tps") if last else None,
                        "peak_mem_gb": last.get("peak_memory") if last else None,
                        # 提前 EOS 不算错，但要能审：解码量不足 MAX_TOKENS 的 run
                        # 其 tps 采样窗更短，分析时需知情
                        "truncated": (gen_tokens or 0) < MAX_TOKENS,
                        # MTP 格：接受率随 chunk.extra 回吐。注意 server 发的是
                        # **嵌套** {"extra": {...}}，不是平铺——取错层级会得到恒 None，
                        # 而恒 None 与"没开投机"长得一模一样，是会静默骗人的那类错。
                        "spec_drafted": ((last or {}).get("extra") or {}).get("spec_drafted"),
                        "spec_accepted": ((last or {}).get("extra") or {}).get("spec_accepted"),
                    })
                    log(f"   [{short} ctx={ctx}] run{i}: "
                        f"gen {cell['runs'][-1]['gen_tps']} tps, "
                        f"prefill {cell['runs'][-1]['prompt_tps']} tps, "
                        f"ttft {cell['runs'][-1]['ttft_s']}s")
                    # 口径校验：±2%（每 run 均为全量 prefill，故 run 皆须验，不能只验首跑）
                    # 放在 run 日志行之后——出事的 run 得先在 stdout 留脸再抛
                    pt = prompt_tokens or 0
                    if ctx and abs(pt - ctx) > ctx * CTX_TOL:
                        raise RuntimeError(
                            f"run{i} prompt_tokens={pt} 偏离目标 {ctx} 超 ±2%")
                if args.sentinels_only:
                    # 无解码 run → 无中位、无峰值（空序列 max 会抛）；显式标记口径，
                    # 免得后人把这些格与含速度的格并表
                    cell["sentinels_only"] = True
                else:
                    steady = cell["runs"][1:]
                    cell["decode_tps_median"], cell["decode_tps_median_n"] = med(steady, "gen_tps")
                    cell["prefill_tps"], cell["prefill_tps_n"] = med(steady, "prompt_tps")
                    cell["ttft_full_s"], cell["ttft_full_s_n"] = med(steady, "ttft_s")
                    cell["peak_mem_gb"] = max((r["peak_mem_gb"] or 0) for r in cell["runs"])
                # 哨兵炸了不陪葬测量：decode/prefill 中位已到手，SSE 在哨兵段断线
                # 只该丢质量探针不该丢整格（复跑一格=再付一次装载+全量 prefill）
                try:
                    cell["sentinels"] = run_sentinels(cli, dialect, filler, None,
                                                      prefill_budget)
                except Exception as se:
                    cell["sentinel_error"] = f"{type(se).__name__}: {se}"
                    log(f"⚠️  哨兵段失败（测量保留）: {cell['sentinel_error']}")
                # 哨兵口径下哨兵**就是**全部产出：炸了就是空格，不能标 complete
                # （标了会被续跑永久跳过）。全格口径下速度中位已到手，仍算完成。
                cell["complete"] = not (args.sentinels_only and cell.get("sentinel_error"))
                sent = cell.get("sentinels") or {}
                ok = bool(sent) and all(v["ok"] for v in sent.values())
                qmark = '全过' if ok else ('⚠️ 有挂' if sent else '⚠️ 未跑成')
                if args.sentinels_only:
                    bits = []
                    for k, v in sent.items():
                        gt = v.get("gen_tokens")
                        bits.append(f"{k}:✓" if v.get("ok") else f"{k}:✗(gt={gt})")
                    log(f"✅ 格 {short} × {ctx}（哨兵口径）: {qmark} — {' / '.join(bits)}")
                else:
                    log(f"✅ 格 {short} × {ctx}: decode 中位 "
                        f"{cell['decode_tps_median']} tps, prefill {cell['prefill_tps']} tps, "
                        f"峰值 {cell['peak_mem_gb']}G, 质量哨 {qmark}")
            except Exception as e:
                cell["error"] = f"{type(e).__name__}: {e}"
                log(f"❌ 格 {short} × {ctx} 失败: {cell['error']}")
            finally:
                if proc is not None:
                    # server 自己死了（OOM/崩）要点名留证，否则下一格只看见一堆连不上
                    if proc.poll() is not None:
                        cell["server_died"] = (f"server 进程已退出 rc={proc.returncode}"
                                               f"（日志 {slog.name}）")
                        log(f"💥 {cell['server_died']}")
                    try:
                        stop_server(proc, lf)
                        log(f"🛑 收 server: {repo} ctx={ctx}")
                    except Exception as se:
                        # 收摊失败不许吞掉格记录（写行在 finally 之后）；进程可能
                        # 还占着 GPU，点名留证并继续——下一格起 server 前健康检查会撞出来
                        cell["stop_error"] = f"{type(se).__name__}: {se}"
                        log(f"⚠️  收 server 失败（进程可能残留）: {cell['stop_error']}")
                with out_path.open("a") as f:
                    f.write(json.dumps(cell, ensure_ascii=False) + "\n")
            if launching and not started:
                # server 都起不来（装载失败/健康超时 1200s）多半是模型级问题，
                # 逐格重试只会每格再烧 20 分钟——同模型剩余格记级联跳过后换模型
                # （配平失败 launching=False 不级联：那是格级配额问题，下一格照跑）
                for c2 in todo[todo.index(ctx) + 1:]:
                    skip_cell = {"type": "cell", "model": repo, "ctx": c2,
                                 "revision": marker.get("revision"),
                                 "weights_bytes": weights_bytes, "runs": [],
                                 "complete": False,
                                 "error": f"级联跳过：ctx={ctx} 的 server 启动失败",
                                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                    with out_path.open("a") as f:
                        f.write(json.dumps(skip_cell, ensure_ascii=False) + "\n")
                log(f"⏭️  {short} 剩余格级联跳过（server 启动失败）")
                break

    log("🏁 基线格全部完成")


if __name__ == "__main__":
    main()
