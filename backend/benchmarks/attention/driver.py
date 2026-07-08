"""注意力涣散基准（attention-drift benchmark）。

量化思路：涣散不是主观印象，它在本架构里留下确定性痕迹——
  过程指标（越低越专注）：解析错误、重复调用拦截、无效计划操作、失败路由、
    工具级错误反馈（System Error）、生成轮数、生成 token 量、墙钟时间；
  结果指标：文件系统断言判定的任务成败（执行≠成功，以最终产物为准）；
  行为指标：模糊任务是否先澄清（clarify_asked）、清晰任务是否误澄清（过度提问回归）。

用法：
  python3 driver.py --work /path/to/workroot --out results.jsonl [--reps 3] [--tasks A,B,...]

对同一套件在 main 与特性分支各跑一遍，对比 results 两份 jsonl 即为收益数据。
"""
import argparse
import json
import os
import re
import shutil
import time
import uuid

import requests

BASE = os.environ.get("BENCH_BASE", "http://127.0.0.1:14159")
SESSIONS = os.environ.get(
    "BENCH_SESSIONS", "/Volumes/enkin/project/agent/MacAgent/sessions")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MAX_APPROVAL_ROUNDS = 15
HTTP_TIMEOUT = 1800

# ---------------------------------------------------------------- 任务定义

def _cp(src, dst):
    shutil.copy2(os.path.join(FIXTURES, src), dst)
    return dst


def setup_single_file(fixture):
    def _setup(workdir):
        return {"file": _cp(fixture, os.path.join(workdir, fixture))}
    return _setup


def setup_lines(workdir):
    d = os.path.join(workdir, "lines")
    shutil.copytree(os.path.join(FIXTURES, "lines"), d)
    return {"dir": d}


def setup_messy(workdir):
    d = os.path.join(workdir, "messy")
    shutil.copytree(os.path.join(FIXTURES, "messy"), d)
    return {"dir": d}


def assert_small_edit(ctx, run):
    orig = open(os.path.join(FIXTURES, "hello.ejs")).read()
    new = open(ctx["file"]).read()
    applied = ">商品を追加</button>" in new
    # 尾部空行容差：编辑器/模型补一个换行属修改意图内，不算破坏文件其余部分
    intact = (new.replace(">商品を追加</button>", ">add</button>").rstrip("\n")
              == orig.rstrip("\n"))
    return applied and intact, {"applied": applied, "rest_intact": intact}


def assert_multi_edit(ctx, run):
    # 原文件本就有一个 addRow 按钮（onclick 还缺括号），正确的最小修复不需要新增按钮；
    # 断言看功能件是否齐活：有合计展示、有行追加机制、按钮没有被写丢。
    orig = open(os.path.join(FIXTURES, "hello.ejs")).read()
    new = open(ctx["file"]).read()
    changed = new != orig
    has_total = re.search(r"合計|合计|total|Total|总额|总价", new) is not None
    has_button = "<button" in new
    has_mechanism = re.search(
        r"appendChild|insertAdjacent|insertRow|innerHTML\s*\+=|cloneNode|"
        r"createElement|newRowHtml", new) is not None
    ok = changed and has_total and has_button and has_mechanism
    return ok, {"changed": changed, "has_total": has_total,
                "has_button": has_button, "has_mechanism": has_mechanism}


def assert_summary(ctx, run):
    expected = {"a.txt": 3, "b.txt": 1, "c.txt": 5, "d.txt": 2}
    path = os.path.join(ctx["dir"], "summary.txt")
    if not os.path.exists(path):
        return False, {"summary_exists": False}
    content = open(path).read()
    detail = {"summary_exists": True}
    ok = True
    for name, n in expected.items():
        line_ok = any(name in ln and re.search(rf"\b{n}\b", ln)
                      for ln in content.splitlines())
        detail[name] = line_ok
        ok = ok and line_ok
    return ok, detail


def assert_report(ctx, run):
    path = os.path.join(os.path.dirname(ctx["file"]), "report.md")
    if not os.path.exists(path):
        return False, {"report_exists": False}
    content = open(path).read()
    detail = {"report_exists": True}
    ok = True
    for cat, total in (("food", 1000), ("transport", 640), ("book", 850)):
        pair_ok = cat in content and str(total) in content
        detail[cat] = pair_ok
        ok = ok and pair_ok
    return ok, detail


def assert_ports(ctx, run):
    content = open(ctx["file"]).read()
    new_ok = all(str(p) in content for p in (9080, 4306, 7379))
    old_gone = not re.search(r"port\s*=\s*(8080|3306|6379)\b", content)
    others = "host = 0.0.0.0" in content and "ttl = 300" in content
    ok = new_ok and old_gone and others
    return ok, {"new_ports": new_ok, "old_gone": old_gone, "others_intact": others}


def assert_ambiguous(ctx, run):
    # 「成功」定义 = 先澄清而非瞎猜动手：没有写操作 且 最终答复向用户提了问题。
    asked = run["clarify_asked"]
    wrote = run["wrote"]
    return (asked and not wrote), {"asked": asked, "wrote": wrote}


TASKS = {
    "A_small_edit": {
        "setup": setup_single_file("hello.ejs"),
        "prompt": ("把 {file} 里 addRow 按钮的显示文字「add」改成「商品を追加」。"
                   "这是一个很小的单点修改，请用 edit_file 完成。"),
        "assert": assert_small_edit,
    },
    "B_multi_edit": {
        "setup": setup_single_file("hello.ejs"),
        "prompt": ("我的电脑上有一个 EJS 模板文件：{file} 。我企图通过添加button来实现"
                   "多条输入，然后根据输入计算总价并展示，但是没做出来。请你直接修改"
                   "这个文件来实现该目的（多行输入行 + 合计金额计算与展示）。"),
        "assert": assert_multi_edit,
    },
    "C_line_count": {
        "setup": setup_lines,
        "prompt": ("{dir} 目录下有若干 .txt 文件。请创建 {dir}/summary.txt，"
                   "内容为每个 .txt 文件一行，格式「文件名: 行数」，按文件名字母排序。"
                   "不要把 summary.txt 自己算进去。"),
        "assert": assert_summary,
    },
    "D_csv_report": {
        "setup": setup_single_file("data.csv"),
        "prompt": ("读取 {file}，按 category 分类求 amount 的总和，"
                   "把结果写入同目录下的 report.md，每个类别一行，格式「类别: 总额」。"),
        "assert": assert_report,
    },
    "E_ambiguous": {
        "setup": setup_messy,
        "prompt": "帮我整理一下 {dir} 这个目录。",
        "assert": assert_ambiguous,
    },
    "F_ini_ports": {
        "setup": setup_single_file("config_sample.ini"),
        "prompt": ("把 {file} 里所有的 port 值都加 1000（例如 8080 变 9080）。"
                   "其他配置一律不要动。"),
        "assert": assert_ports,
    },
}

# ---------------------------------------------------------------- 痕迹抓取

WRITE_TOOLS = {"edit_file", "write_file"}
_SHELL_WRITE = re.compile(r"<<|>\s*\S|\brm\b|\bmv\b|\bcp\b|\bmkdir\b|\btouch\b|\bsed\s+-i")


def scrape_session(sid):
    """从会话落盘的 turn 记录里提取护栏痕迹与行为特征。"""
    path = os.path.join(SESSIONS, sid, "history.jsonl")
    out = {
        "turn_status": None, "generations": 0, "gen_tokens": 0,
        "parse_errors": 0, "dup_blocks": 0, "invalid_ops": 0,
        "failure_routes": 0, "tool_errors": 0, "reminders": 0,
        "n_calls": 0, "calls": [], "wrote": False, "shell_wrote": False,
        "clarify_asked": False, "constrained_steps": 0,
    }
    if not os.path.exists(path):
        out["turn_status"] = "NO_HISTORY"
        return out
    records = [json.loads(l) for l in open(path) if l.strip()]
    last_text = ""
    for rec in records:
        out["turn_status"] = rec.get("status")
        out["generations"] += int(rec.get("generations") or 0)
        out["gen_tokens"] += int(rec.get("generation_tokens_turn") or 0)
        out["parse_errors"] += int(rec.get("parse_error_count") or 0)
        out["constrained_steps"] += int(rec.get("constrained_steps") or 0)
        for m in rec.get("response") or []:
            for tc in m.get("tool_calls") or []:
                if tc.get("type") == "error":
                    out["calls"].append("PARSE_ERROR")
                    continue
                name = tc.get("name", "?")
                out["calls"].append(name)
                out["n_calls"] += 1
                if name in WRITE_TOOLS:
                    out["wrote"] = True
                if name == "run_shell_command":
                    cmd = (tc.get("arguments") or {}).get("command", "")
                    if _SHELL_WRITE.search(cmd or ""):
                        out["wrote"] = True
                        out["shell_wrote"] = True
            for tr in m.get("tool_responses") or []:
                resp = str(tr.get("response", ""))
                if "Duplicate call blocked" in resp:
                    out["dup_blocks"] += 1
                if "Invalid op:" in resp:
                    out["invalid_ops"] += 1
                if "budget exhausted" in resp:
                    out["failure_routes"] += 1
                if "[Reminder]" in resp:
                    out["reminders"] += 1
                if ("System Error:" in resp
                        and "could not be parsed" not in resp
                        and "same invalid tool call" not in resp):
                    out["tool_errors"] += 1
            if m.get("role") == "assistant" and not m.get("tool_calls"):
                last_text = m.get("content") or last_text
    # parse_error_count 指标在审批续跑时可能被末段覆盖，以消息里的 error 调用数兜底
    out["parse_errors"] = max(out["parse_errors"], out["calls"].count("PARSE_ERROR"))
    out["clarify_asked"] = ("?" in last_text or "？" in last_text)
    out["final_text_head"] = last_text[:200]
    return out


EVENTS_FILE = os.path.join(os.path.dirname(SESSIONS), "guardrail_events.jsonl")


def scrape_events(sid):
    """护栏事件流覆盖度（特性分支才有产出；main 上恒为 0，作对照）。"""
    out = {"events_logged": 0, "event_types": {}}
    if not os.path.exists(EVENTS_FILE):
        return out
    for line in open(EVENTS_FILE):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("session") == sid:
            out["events_logged"] += 1
            t = e.get("type", "?")
            out["event_types"][t] = out["event_types"].get(t, 0) + 1
    return out


# ---------------------------------------------------------------- 执行

def run_one(task_name, spec, rep, workroot):
    workdir = os.path.join(workroot, f"{task_name}-r{rep}")
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir)
    ctx = spec["setup"](workdir)
    prompt = spec["prompt"].format(**ctx)
    sid = f"bench-{task_name}-r{rep}-{uuid.uuid4().hex[:6]}"

    t0 = time.time()
    approvals = 0
    status = "driver_error"
    try:
        r = requests.post(f"{BASE}/api/v1/chat/{sid}",
                          data={"prompt": prompt, "enable_thinking": "true"},
                          timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        while data.get("status") == "pending_approval" and approvals < MAX_APPROVAL_ROUNDS:
            approvals += 1
            r = requests.post(f"{BASE}/api/v1/chat/{sid}/approval",
                              json={"approve": True}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        status = data.get("status")
    except Exception as e:
        status = f"driver_error: {type(e).__name__}: {e}"
    wall = round(time.time() - t0, 1)

    run = scrape_session(sid)
    run.update(scrape_events(sid))
    success, detail = spec["assert"](ctx, run)
    drift_events = (run["parse_errors"] + run["dup_blocks"]
                    + run["invalid_ops"] + run["failure_routes"]
                    + run["tool_errors"])
    row = {"task": task_name, "rep": rep, "sid": sid, "status": status,
           "success": success, "assert_detail": detail, "wall_s": wall,
           "approvals": approvals, "drift_events": drift_events, **run}
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--rep0", type=int, default=0, help="起始 rep 编号（断点续跑用）")
    ap.add_argument("--tasks", default=",".join(TASKS))
    args = ap.parse_args()

    os.makedirs(args.work, exist_ok=True)
    names = [t for t in args.tasks.split(",") if t in TASKS]
    rows = []
    for name in names:
        for rep in range(args.rep0, args.reps):
            print(f"--> {name} r{rep} ...", flush=True)
            row = run_one(name, TASKS[name], rep, args.work)
            rows.append(row)
            with open(args.out, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"    status={row['status']} success={row['success']} "
                  f"drift={row['drift_events']} calls={row['n_calls']} "
                  f"wall={row['wall_s']}s", flush=True)

    print("\n==== AGGREGATE ====", flush=True)
    for name in names:
        rs = [r for r in rows if r["task"] == name]
        ok = sum(r["success"] for r in rs)
        drift = sum(r["drift_events"] for r in rs)
        wall = sum(r["wall_s"] for r in rs) / max(len(rs), 1)
        clarify = sum(r["clarify_asked"] and not r["wrote"] for r in rs)
        print(f"{name:14s} success {ok}/{len(rs)}  drift_sum {drift:3d}  "
              f"clarify {clarify}/{len(rs)}  avg_wall {wall:.0f}s", flush=True)


if __name__ == "__main__":
    main()
