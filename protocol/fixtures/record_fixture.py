#!/usr/bin/env python3
"""协议夹具录制器（OSS 扫描 §5 第三件,D.25 顺序 5,2026-08-28;首审后重写）。

「先落夹具再改实现」是铁律 5,录制这半边此前从未实现（9 条夹具全手工构造,008/009
各付了一次手工税）。本工具把 `GET /chat/{sid}/events?after=0` 的信封落盘,夹具从
「手写事件流」变成「录一遍真会话 + 人工审终态」。

⚠️ **只录本工具启动后新产生的轮**（首审 Critical 1）:journal 对历史轮做瘦身持久化
（turn_ended 只剩 {status,turn_index},工具载荷截 2000 字符）,attach 在有历史轮的
会话上会把瘦身版固化成「自洽的垃圾夹具」——EMIT 会照着它生成期望,两端还都绿。
故正确流程是:**先起录制器附着新会话,再发消息**;录到无 response 的 turn_ended
时本工具拒绝落盘（--allow-degraded 可越过,自担后果）。

⚠️ 本路径只覆盖**后端已实现的行为**（复审 S13）:夹具先行的新协议行为仍须手工构造
——step 2 的期望态由已提交归约器生成,对 web 端是同义反复,Swift 回放才是独立校验。

用法(FakeEngine dev 栈即可,不需要真模型):
    python3 protocol/fixtures/record_fixture.py 010-user-cancel --session <sid> \\
        [--base http://127.0.0.1:14159] [--until-turns 1] [--raw] [--keep-attach]
  暂停态夹具(审批中/挂起中,002/004/005/006/009 类):用 `--until-turns 0`,到达暂停态
  后 Ctrl-C 收针——已录信封照常落盘。
  需要 stream_attached 帧的夹具(005/006 类,B2 代际围栏):加 `--keep-attach`,它强制
  `--raw`(attach 帧 seq 恒 0 且 data.last_seq 引用信封 seq,重编号会把两者改坏)。

三步纪律(refresh 态:期望值由已提交代码重生成,人审 diff——不许手写终态):
 1. 录 `<name>.events.jsonl`(默认 seq→1..N/ts→整拍重编号;--raw 保留原值);
 2. `cd frontend && EMIT_FIXTURE_EXPECTED=<name> npm test -- turn-reducer`
    → 生成 `<name>.expected.json` 并**必红**(确认收据);人工审它是「应该」的终态;
 3. 双端回放全绿(web + `apple-app/Tests/run_reducer_fixtures.sh`)+ 登记 README 表格
    + 写 `<name>.md` 场景说明,才算入库。(doc-numbers 门已于 2026-08-29 退役,不再刷计数。)

鉴权:AGENT_API_KEY 环境变量,只进请求头;**非回环 --base 不附密钥**（明文外发面),
且不跟随任何重定向（30x 会把 Bearer 带去别的主机）。纯 stdlib。
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ATTACH_TYPES = {"stream_attached"}     # 流控制帧:默认不入夹具;005/06 类用 --keep-attach 保留
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # 30x 一律拒绝——Bearer 不许被带去别的主机
        raise urllib.error.HTTPError(a[1].full_url, a[3], "重定向被拒绝(密钥保护)", a[4], None)


def parse_sse_lines(lines, keep_attach=False):
    """SSE 字节行 → 信封列表(纯函数,可钉测)。只认 `data:` 行;`event:`/心跳注释/空行
    不载信封;解析不动的 data 行**响亮丢弃**(复审 S7:保真工具里的静默丢帧最毒)。"""
    envelopes = []
    for rawline in lines:
        line = rawline.decode("utf-8", "replace").rstrip("\r\n") \
            if isinstance(rawline, bytes) else rawline.rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        try:
            env = json.loads(payload)
        except ValueError:
            print(f"[record] ⚠️ 丢弃解析不动的 data 行: {payload[:80]!r}", file=sys.stderr)
            continue
        if env.get("type") in ATTACH_TYPES and not keep_attach:
            continue
        envelopes.append(env)
    return envelopes


def renumber(envelopes):
    """信封簿记字段重编号(纯函数):seq→1..N、ts→整拍。**不碰** data 载荷与 turn_id
    (data 里的 timestamp 等留真值,人审时两套钟并存属已知,复审 N16)。
    ⚠️ 本函数不许喂含 stream_attached 的流:attach 帧 seq 恒 0(协议),其 data.last_seq
    引用信封 seq——重编号两者都会改坏(复审 S3);调用方用 --keep-attach 时走 --raw。"""
    assert not any(e.get("type") in ATTACH_TYPES for e in envelopes), \
        "renumber 不接受 stream_attached(--keep-attach 应已强制 --raw)"
    base_ts = 1783600000.0
    out = []
    for i, env in enumerate(envelopes):
        out.append({**env, "seq": i + 1, "ts": round(base_ts + i * 0.1, 3)})
    return out


def check_not_degraded(envelopes):
    """journal 历史重放的瘦身痕迹(首审 Critical 1):turn_ended 无 response 键 = 录到了
    历史轮。返回问题描述或 None。"""
    for env in envelopes:
        if env.get("type") == "turn_ended" and "response" not in (env.get("data") or {}):
            return (f"seq={env.get('seq')} 的 turn_ended 载荷无 response——这是 journal "
                    f"对历史轮的瘦身形态。你附着在有历史轮的会话上了:换**新会话**,"
                    f"先起录制器再发消息(工具载荷同样被截 2000 字符)。")
    return None


def record(base, session, after, key, until_turns, keep_attach, out_stream):
    url = f"{base}/api/v1/chat/{session}/events?after={after}"
    host = urllib.parse.urlparse(base).hostname or ""
    headers = {"Accept": "text/event-stream"}
    if key and host in _LOOPBACK:
        headers["Authorization"] = f"Bearer {key}"
    elif key:
        print(f"[record] ⚠️ 非回环 --base({host}):不附密钥(明文外发面)", file=sys.stderr)
    elif not key:
        print("[record] ⚠️ AGENT_API_KEY 未设——后端若启鉴权将 401", file=sys.stderr)
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=headers)
    raw_lines, ended = [], 0
    try:
        with opener.open(req, timeout=300) as resp:
            for rawline in resp:
                raw_lines.append(rawline)
                line = rawline.decode("utf-8", "replace")
                if '"turn_ended"' in line and line.startswith("data:"):
                    ended += 1
                    print(f"  [turn_ended #{ended}]", file=sys.stderr)
                    if until_turns and ended >= until_turns:
                        break
                elif line.startswith("data:"):
                    m = re.search(r'"type": "([a-z_]+)"', line)
                    if m:
                        print(f"  [{m.group(1)}]", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"\n[record] 收针(Ctrl-C),已收 {len(raw_lines)} 行", file=sys.stderr)
    except (ConnectionError, TimeoutError, OSError) as e:
        # 断流不丢录(复审 S4):已收的行照常走落盘判定,操作员自己决定要不要
        print(f"\n[record] ⚠️ 流中断({e!r}),已收 {len(raw_lines)} 行——按已录内容落盘",
              file=sys.stderr)
    return parse_sse_lines(raw_lines, keep_attach=keep_attach)


def main():
    ap = argparse.ArgumentParser(description="录制协议夹具事件流(用法详见文件头)")
    ap.add_argument("name", help="夹具名,如 010-user-cancel(NNN-kebab)")
    ap.add_argument("--session", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:14159")
    ap.add_argument("--after", type=int, default=0)
    ap.add_argument("--until-turns", type=int, default=1,
                    help="录到第 N 个 turn_ended 收针;0=只靠 Ctrl-C(暂停态夹具用)")
    ap.add_argument("--raw", action="store_true", help="保留真实 seq/ts,不重编号")
    ap.add_argument("--keep-attach", action="store_true",
                    help="保留 stream_attached 帧(005/006 类);强制 --raw")
    ap.add_argument("--allow-degraded", action="store_true",
                    help="明知录到 journal 瘦身历史轮仍落盘(通常是错的)")
    args = ap.parse_args()
    if not re.fullmatch(r"[0-9]{3}-[a-z0-9-]+", args.name):
        print(f"[record] 夹具名须形如 010-user-cancel(NNN-kebab):{args.name!r}",
              file=sys.stderr)
        return 2
    if args.keep_attach and not args.raw:
        print("[record] --keep-attach 强制 --raw(attach 帧 seq=0 与 data.last_seq "
              "都会被重编号改坏)", file=sys.stderr)
        args.raw = True
    out = Path(__file__).parent / f"{args.name}.events.jsonl"
    if out.exists():
        print(f"[record] {out} 已存在——夹具是入库档案,不覆写;换名或先删", file=sys.stderr)
        return 2
    key = os.environ.get("AGENT_API_KEY", "")
    try:
        envelopes = record(args.base, args.session, args.after, key,
                           args.until_turns, args.keep_attach, sys.stderr)
    except urllib.error.HTTPError as e:
        hint = {404: "会话尚未落地(PROTOCOL §2.4 常态):先受理一条消息再附着,或核对 sid",
                401: "鉴权被拒:核对 AGENT_API_KEY", 403: "鉴权被拒:核对 AGENT_API_KEY"}
        print(f"[record] HTTP {e.code}:{hint.get(e.code, e.reason)}", file=sys.stderr)
        return 1
    if not envelopes:
        print("[record] 0 条信封——没录到东西,不写文件", file=sys.stderr)
        return 1
    problem = check_not_degraded(envelopes)
    if problem and not args.allow_degraded:
        print(f"[record] 拒绝落盘:{problem}(--allow-degraded 可越过)", file=sys.stderr)
        return 3
    if problem:
        print(f"[record] ⚠️ --allow-degraded:{problem}", file=sys.stderr)
    if not args.raw:
        envelopes = renumber(envelopes)
    out.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in envelopes),
        encoding="utf-8")
    print(f"[record] {len(envelopes)} 条信封 → {out}", file=sys.stderr)
    stem = out.name.removesuffix(".events.jsonl")
    print(f"[record] 下一步(refresh 纪律,见文件头): cd frontend && "
          f"EMIT_FIXTURE_EXPECTED={stem} npm test -- turn-reducer", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
