"""W-3 验收探针：Colab 部署 google/gemma-4-31B-it-qat-w4a16-ct 后，一次纯文本 + 一次图文请求
走通（视觉塔 vLLM 端到端）。

只用 inference-server 的**原生**协议（与生产 RemoteBackend 同一条路；OpenAI 门面拒收媒体）：
  1. GET /health                 → 身份核对（framework guard_model_identity，同装配期护栏）
  2. POST /v1/generate（纯文本）  → 「3 乘 7」回复须含 21
  3. POST /v1/media + /v1/generate(images=[media_id]) → 回复须含 W3/2026 且含 B
prompt 由方言注册面渲染（gemma4 PromptBuilder：图片项 → <|image><|image|><image|>），分词器
本地只读（resolve_tokenizer_only_path，不下权重）。不改任何框架代码。

用法见同目录 README.md。退出码：0 = 三步全过；1 = 有步未过或装配错误（缺地址/表外 repo）；2 = argparse 用法错误。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

EXPECTED_REPO = "google/gemma-4-31B-it-qat-w4a16-ct"
TEXT_QUESTION = "用一句话回答：3 乘 7 等于多少？"
IMAGE_QUESTION = "图里的大字是什么？哪根柱子最高？用一句话回答。"
IMAGE_TEXT = "W3 OK 2026"
BARS = (("A", 10), ("B", 30), ("C", 20))
MAX_TOKENS = 128
CONNECT_TIMEOUT = 10.0

#: 采样 = 框架缺省（engine.oneshot_instruction 同款：temperature/min_p/repetition_*），只把
#: max_tokens 压到 128；features 同款只给 enable_thinking=False（思考关）
SAMPLING = {"temperature": 0.2, "min_p": 0.05, "max_tokens": MAX_TOKENS,
            "repetition_penalty": 1.1, "repetition_context_size": 50}
FEATURES = {"enable_thinking": False}


# ---------------- 地址 / 鉴权 ----------------

def resolve_base_url(base_url: Optional[str]) -> str:
    """--base-url 优先；否则 INFER_REMOTE_HOST（host 或 host:port），缺省端口取 config
    inference.remote.port（RemoteBackend._load_sync 同一来源，现为 14161）。"""
    if base_url:
        return base_url.rstrip("/")
    host = os.environ.get("INFER_REMOTE_HOST", "").strip()
    if not host:
        raise SystemExit("❌ 需要算力端地址：设 INFER_REMOTE_HOST=<host[:port]> 或给 --base-url")
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    if re.fullmatch(r"[^:\[\]]+:\d+", host):
        return f"http://{host}"
    from framework.utils.config_loader import load_section
    port = int((load_section("inference").get("remote") or {}).get("port", 14161))
    return f"http://{host}:{port}"


def make_session():
    """RemoteBackend.__init__ 同款：INFER_API_KEY 非空即带 Bearer。"""
    import requests
    s = requests.Session()
    key = os.environ.get("INFER_API_KEY", "")
    if key:
        s.headers["Authorization"] = f"Bearer {key}"
    else:
        print("⚠️ 未设 INFER_API_KEY：算力端若要求鉴权会 401")
    return s


# ---------------- prompt 渲染（方言注册面） ----------------

def build_dialect(repo: str):
    """生产同路装配方言：注册表条目 → 本地分词器（tokenizer-only，命中标记即零网络）
    → RemoteBackend 的 _ProcessorShim → get_dialect_class(dialect).create。"""
    from transformers import AutoTokenizer
    from framework.backends.remote.backend import _ProcessorShim
    from framework.dialects import get_dialect_class
    from framework.utils.model_selection import model_options
    from framework.utils.path_resolver import resolve_tokenizer_only_path
    opts = model_options(repo)
    if not opts:
        raise SystemExit(f"❌ {repo!r} 不在 config.yaml model_registry：无法确定方言/分词器")
    tok_path = resolve_tokenizer_only_path(repo, revision=opts.get("revision"))
    processor = _ProcessorShim(AutoTokenizer.from_pretrained(tok_path))
    return get_dialect_class(str(opts.get("dialect") or "")).create(
        processor, options=opts, env={})


def user_messages(question: str, n_images: int) -> List[Dict[str, Any]]:
    """会话消息形状同 sandbox_runtime（图片项在前、文本项在后）；纯文本走 str 正文。"""
    if not n_images:
        return [{"role": "user", "content": question}]
    content: List[Dict[str, Any]] = [{"type": "image"} for _ in range(n_images)]
    content.append({"type": "text", "text": question})
    return [{"role": "user", "content": content}]


def render_prompt(dialect, question: str, n_images: int = 0) -> str:
    return dialect.build_prompt(messages=user_messages(question, n_images), tools=None,
                                enable_thinking=False)


# ---------------- 测试图 ----------------

def make_png() -> bytes:
    """确定性 PNG：白底 512×256，顶部大字 W3 OK 2026，下方 A/B/C 三柱（10/30/20）。
    字体用 Pillow 内置（load_default(size)），不依赖本机字体 → 同版本 Pillow 字节一致。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (512, 256), "white")
    d = ImageDraw.Draw(img)
    big = ImageFont.load_default(size=56)
    small = ImageFont.load_default(size=26)
    w = d.textlength(IMAGE_TEXT, font=big)
    d.text(((512 - w) / 2, 6), IMAGE_TEXT, fill="black", font=big)
    base_y, unit, bar_w = 214, 4.5, 60
    for i, (label, h) in enumerate(BARS):
        x0 = 116 + i * 110
        d.rectangle([x0, base_y - h * unit, x0 + bar_w, base_y], fill="black")
        lw = d.textlength(label, font=small)
        d.text((x0 + (bar_w - lw) / 2, base_y + 6), label, fill="black", font=small)
    d.line([90, base_y, 430, base_y], fill="black", width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def media_id_for(data: bytes, suffix: str = ".png") -> Tuple[str, str]:
    """RemoteBackend._ensure_media 同款内容寻址名：sha256 hex + 小写后缀。"""
    digest = hashlib.sha256(data).hexdigest()
    return digest, digest + suffix.lower()


# ---------------- 协议 ----------------

def upload_media(session, base_url: str, data: bytes, name: str) -> Dict[str, Any]:
    """POST /v1/media：multipart file + 表单 sha256（_ensure_media 同款字段）。"""
    digest, expect_id = media_id_for(data, Path(name).suffix)
    out: Dict[str, Any] = {"status": None, "expected_media_id": expect_id}
    try:
        r = session.post(f"{base_url}/v1/media", files={"file": (name, data)},
                         data={"sha256": digest}, timeout=(CONNECT_TIMEOUT, 60.0))
        out["status"] = r.status_code
        if r.status_code == 200:
            out["media_id"] = (r.json() or {}).get("media_id")
        else:
            out["error"] = r.text[:300]
    except Exception as e:  # 连接错 / 非 JSON 200：记成失败步骤，不让整轮崩掉
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out


def consume_sse(lines: Iterable[Any]) -> Dict[str, Any]:
    """按 RemoteBackend.stream_generate 的口径消费 SSE：event:/data: 行，meta/chunk/done/error；
    text 段逐 chunk 累加 = 完整原始文本。流在 done/error 前 EOF 记为 interrupted。"""
    event, text = "", ""
    res: Dict[str, Any] = {"gen_id": None, "cache_id": None, "chunks": 0, "done": False,
                           "error": None, "last_chunk": {}, "done_payload": None}
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = json.loads(line[len("data:"):].strip() or "{}")
        if event == "meta":
            res["gen_id"], res["cache_id"] = data.get("gen_id"), data.get("cache_id")
        elif event == "chunk":
            res["chunks"] += 1
            text += data.get("text", "")
            res["last_chunk"] = {k: data.get(k) for k in
                                 ("prompt_tokens", "generation_tokens", "prompt_tps",
                                  "generation_tps", "peak_memory")}
        elif event == "done":
            res["done"], res["done_payload"] = True, data
            break
        elif event == "error":
            res["error"] = data.get("message", "未知错误")
            break
    if not res["done"] and res["error"] is None:
        res["error"] = "SSE 流在 done 前中断"
    res["raw_text"] = text
    return res


def generate(session, base_url: str, prompt: str, images: List[str], timeout: float
             ) -> Dict[str, Any]:
    body = {"cache_id": None, "prompt": prompt, "images": images, "audios": [],
            "sampling": dict(SAMPLING), "features": dict(FEATURES)}
    resp = session.post(f"{base_url}/v1/generate", json=body, stream=True,
                        timeout=(CONNECT_TIMEOUT, timeout))
    try:
        if resp.status_code != 200:
            return {"http_status": resp.status_code, "done": False,
                    "error": f"HTTP {resp.status_code} {resp.text[:300]}", "raw_text": ""}
        res = consume_sse(resp.iter_lines())
        res["http_status"] = 200
        if not res["done"] and res.get("gen_id"):
            try:   # 同 stream_generate 的 finally：未到 done 即离场 → 显式取消服务端生成
                session.post(f"{base_url}/v1/generate/{res['gen_id']}/cancel", timeout=5.0)
            except Exception:
                pass
        if res.get("cache_id"):
            try:   # 一次性缓存：用完即还（服务端 45min TTL 兜底）
                session.delete(f"{base_url}/v1/cache/{res['cache_id']}", timeout=5.0)
            except Exception:
                pass
        return res
    finally:
        resp.close()


def visible_text(dialect, raw: str) -> str:
    """方言解析出的正文（剥思考频道/工具段）；解析不出正文时退回原文去掉轮收口符。"""
    try:
        txt = (dialect.parse_response(raw) or {}).get("text") or ""
    except Exception:
        txt = ""
    return (txt or raw).replace("<turn|>", "").strip()


# ---------------- 判定 ----------------

_LETTER_RE = re.compile(r"(?<![A-Za-z])[ABC](?![A-Za-z])")
_21_RE = re.compile(r"(?<!\d)21(?!\d)")


def _b_is_tallest(reply: str) -> bool:
    """每处「最高」：前 10 字内最近的独立字母若存在须是 B；否则后 10 字内第一个独立字母须是 B。
    「B柱最高」「最高的是 B 柱」通过；「A 最高，B 和 C 较矮」不通过。"""
    for m in re.finditer("最高", reply):
        before = _LETTER_RE.findall(reply[max(0, m.start() - 10):m.start()])
        if before:
            if before[-1] == "B":
                return True
            continue
        after = _LETTER_RE.findall(reply[m.end():m.end() + 10])
        if after and after[0] == "B":
            return True
    return False


def judge_text(reply: str) -> bool:
    return bool(_21_RE.search(reply))


def judge_image(reply: str) -> bool:
    return ("W3" in reply or "2026" in reply) and _b_is_tallest(reply)


# ---------------- 主流程 ----------------

def run(args, session=None, dialect=None) -> Tuple[int, Dict[str, Any]]:
    base_url = resolve_base_url(args.base_url)
    out_dir = Path(args.out or REPO / "benchmarks" / "data"
                   / f"w3-vision-smoke-{datetime.now():%Y%m%d}")
    out_dir.mkdir(parents=True, exist_ok=True)
    session = session or make_session()
    result: Dict[str, Any] = {"base_url": base_url, "expected_repo": EXPECTED_REPO,
                              "any_model": bool(args.any_model), "max_tokens": MAX_TOKENS,
                              "sampling": SAMPLING, "features": FEATURES,
                              "started_at": datetime.now().astimezone().isoformat(),
                              "steps": {}}

    def _save() -> None:
        (out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Step 1 health
    t0 = time.monotonic()
    h: Dict[str, Any] = {"pass": False}
    health: Dict[str, Any] = {}
    try:
        r = session.get(f"{base_url}/health", timeout=CONNECT_TIMEOUT)
        h["http_status"] = r.status_code
        health = r.json() if r.status_code == 200 else {}
    except Exception as e:
        h["error"] = f"{type(e).__name__}: {e}"
    h["latency_s"] = round(time.monotonic() - t0, 3)
    result["health"] = health
    served = str(health.get("model_repo") or "")
    print(f"[health] {json.dumps(health, ensure_ascii=False)}")
    if h.get("http_status") == 200:
        from framework.backends.remote.backend import guard_model_identity
        from framework.utils.model_selection import model_options
        if args.any_model:
            h["pass"] = True
            h["note"] = f"--any-model：不核对身份（服务端报 {served or '空'}）"
        else:
            try:
                guard_model_identity(health, {"repo": EXPECTED_REPO, "revision":
                                              model_options(EXPECTED_REPO).get("revision")},
                                     base_url)
                h["pass"] = True
            except RuntimeError as e:
                h["error"] = str(e)
        caps = health.get("capabilities") or []
        if h["pass"] and not args.any_model and "vision" not in caps:
            h["pass"], h["error"] = False, f"capabilities 不含 vision：{caps}"
    h["request"] = "GET /health"
    result["steps"]["health"] = h
    if not h["pass"]:
        _save()
        print_verdict(result)
        return 1, result

    # 方言：按服务端真身（--any-model 且表内有条目时）否则按期望件
    from framework.utils.model_selection import model_options
    dialect_repo = served if (args.any_model and served and model_options(served)) \
        else EXPECTED_REPO
    dialect = dialect or build_dialect(dialect_repo)
    result["dialect_repo"] = dialect_repo

    # Step 2 text
    prompt = render_prompt(dialect, TEXT_QUESTION)
    result["steps"]["text"] = _gen_step(session, base_url, dialect, prompt, [], args.timeout,
                                        TEXT_QUESTION, judge_text)
    _save()

    # Step 3 image
    png = make_png()
    digest, _ = media_id_for(png)
    (out_dir / "w3_smoke.png").write_bytes(png)
    result["png_sha256"] = digest
    up = upload_media(session, base_url, png, "w3_smoke.png")
    print(f"[media] {json.dumps(up, ensure_ascii=False)}")
    step: Dict[str, Any]
    if up.get("status") == 200 and up.get("media_id") == up["expected_media_id"]:
        prompt = render_prompt(dialect, IMAGE_QUESTION, n_images=1)
        step = _gen_step(session, base_url, dialect, prompt, [up["media_id"]], args.timeout,
                         IMAGE_QUESTION, judge_image)
    else:
        step = {"pass": False, "error": f"媒体上传未成功或 media_id 不符：{up}"}
    step["media_upload"] = up
    result["steps"]["image"] = step
    result["finished_at"] = datetime.now().astimezone().isoformat()
    _save()
    print_verdict(result)
    print(f"[out] {out_dir}")
    ok = all(s.get("pass") for s in result["steps"].values())
    return (0 if ok else 1), result


def _gen_step(session, base_url, dialect, prompt, images, timeout, question, judge
              ) -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        res = generate(session, base_url, prompt, images, timeout)
    except Exception as e:
        res = {"done": False, "error": f"{type(e).__name__}: {e}", "raw_text": ""}
    latency = round(time.monotonic() - t0, 3)
    reply = visible_text(dialect, res.get("raw_text", ""))
    ok = bool(res.get("done")) and not res.get("error") and judge(reply)
    step = {"request": {"question": question, "images": images, "prompt_chars": len(prompt),
                        "prompt": prompt},
            "reply": reply, "raw_text": res.get("raw_text", ""), "latency_s": latency,
            "pass": ok, "sse": {k: res.get(k) for k in
                                ("http_status", "gen_id", "cache_id", "chunks", "done",
                                 "error", "last_chunk", "done_payload")}}
    print(f"[{'image' if images else 'text'}] {latency}s reply={reply!r}")
    return step


def print_verdict(result: Dict[str, Any]) -> None:
    for name in ("health", "text", "image"):
        s = result["steps"].get(name)
        if s is None:
            print(f"{name:<7} SKIP")
            continue
        detail = s.get("error") or s.get("note") or s.get("reply") or ""
        print(f"{name:<7} {'PASS' if s.get('pass') else 'FAIL'}  "
              f"{s.get('latency_s', '-')}s  {str(detail).replace(chr(10), ' ')[:100]}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=None,
                    help="结果目录（缺省 benchmarks/data/w3-vision-smoke-<YYYYMMDD>/）")
    ap.add_argument("--base-url", default=None, help="覆盖 INFER_REMOTE_HOST，如 http://h:14161")
    ap.add_argument("--timeout", type=float, default=300.0, help="SSE 逐块读超时（秒）")
    ap.add_argument("--any-model", action="store_true",
                    help="不核对服务端模型身份（协议联调用）")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    code, _ = run(build_parser().parse_args(argv))
    return code


if __name__ == "__main__":
    sys.exit(main())
