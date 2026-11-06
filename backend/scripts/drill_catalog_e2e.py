"""D0③:catalog 逃生口真机端到端演练。

前置:后端已起,且所选主模型处于 catalog 档——W-2(D29,2026-11-01)起 gemma4 两条目、D44 (iii)
(2026-11-05)起 qwen35 两条目也在注册表条目级 `tool_schema_mode: catalog`。演练工具取
**file_extract**(非 core 的带参工具;首审 6:run_shell_command 已进 agent.tool_routing.core、常驻
完整 schema,验不到目录行逃生口;原演练件 run_math 也于 D44 (iii) 进了 core,同理换掉。file_extract
只读、免审批、零副作用——move_file / copy_file 需审批会挂成 pending_approval,web_read 要联网且
回执里放不进标记串)。本脚本先在临时目录写一个内容为标记串的 .txt,演练后删掉。单轮无计划 =
不建任务,首个任务前无路由件,file_extract 在前缀里只是一条目录行。
与 golden 的关系(只部分重合):tests/test_context_knee_orchestrator.py::
test_catalog_prefix_precheck_non_core_tool_feeds_declaration 守的是「目录行工具发错参数 → 预检打回、
回执附完整声明」(FakeEngine 脚本直接带参调用,不经 get_tool_schema);本演练验的是真模型走
「目录行 → get_tool_schema 取详情 → 带参调用」并真执行。两者共用「file_extract 在前缀里只是目录行」
这一前提,打回附声明那条路本演练只在模型恰好先发错参时才会走到,不作判据。判据(全部满足才算过):
① 轮次以 success 完结;
② 事件流里有 file_extract 的 tool_started 且 **args 非空**——§6.5 的病是
  catalog 下带参工具被钳成空实参,这一条就是「逃生口全面可用」的直接证据。
  ⚠️ 若模型先 update_plan 建了任务、且 file_extract 被任务期路由选成完整 schema(④ 的
  tool_routed=true),则本次调用看到的是完整声明,**没有走到目录行逃生口**——②③ 过了
  也只证明带参调用可用,不证明「目录行 → get_tool_schema → 带参调用」这条路;如实看 ④;
③ 对应 tool_result 的 response 含临时文件里的标记串(文件真被读了);
④ (如实记录,不作硬判据)两项观察:模型是否调过 get_tool_schema 取详情(D0① 的方言渲染
  是否被消费是模型行为,模型可能凭目录名直接调用);file_extract 是否出现在任务的
  routed_tools 里(取自 plan_updated 事件的任务快照;没建任务 = 不在)。

退出码:0=①②③全真;3=不满足;1=环境/脚本错误。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drill_restart_fence import SSECollector, submit_turn, wait_port  # noqa: E402

MARK = f"catalog-ok-{int(time.time())}"
TOOL = "file_extract"   # 非 core 带参工具(见模块说明)


def main() -> int:
    if not wait_port(10):
        print("[catalog-drill] 后端未在 :14159 监听", file=sys.stderr)
        return 1
    fd, path = tempfile.mkstemp(prefix="drill-catalog-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(MARK + "\n")
        return _drill(path)
    finally:
        Path(path).unlink(missing_ok=True)


def _drill(path: str) -> int:
    sid = f"drill-catalog-{int(time.time())}"
    prompt = (f"请先用 get_tool_schema 查询 {TOOL} 的参数定义,"
              f"然后用 {TOOL} 读取文件 {path} ,并把文件内容原样告诉我。")
    r = submit_turn(sid, prompt, max_tokens=2048)
    print(f"[catalog-drill] 已受理 turn_id={r['turn_id']}")
    c = SSECollector(f"/api/v1/chat/{sid}/events", after=0)
    c.start()
    ok_end = c.wait_for(lambda cc: cc.count("turn_ended") > 0, 240)
    c.stop()
    if c.error is not None or not ok_end:
        # 环境失败(SSE 断/超时)≠ 逃生口不可用——与 drill_restart_fence 同一退出码纪律
        print(f"[catalog-drill] 环境失败: err={c.error} turn_ended={ok_end}",
              file=sys.stderr)
        return 1

    obs = {"turn_ended": ok_end}
    # started/result 按 call_id 配对——首个 started 配任意 result 会把 A 调用的实参
    # 和 B 调用的输出拼成一个判定
    events = list(c.events)                      # 读线程可能仍在收尾,取快照再迭代
    shell_started = {}
    result_call, saw_get_schema, status = None, False, None
    routed_seen = False                           # TOOL 是否被任务期路由选成完整 schema
    for e in events:
        d = e["data"]
        if e["type"] == "tool_started":
            if d.get("name") == TOOL:
                shell_started[d.get("call_id")] = d.get("args")
            if d.get("name") == "get_tool_schema":
                saw_get_schema = True
        elif e["type"] == "tool_result":
            if d.get("name") == "get_tool_schema":
                saw_get_schema = True
            if d.get("name") == TOOL and MARK in str(d.get("response", "")):
                result_call = d.get("call_id")
        elif e["type"] == "plan_updated":
            task = d.get("task") or {}
            if TOOL in (task.get("routed_tools") or []):
                routed_seen = True
        elif e["type"] == "turn_ended":
            status = d.get("status")

    started_args = shell_started.get(result_call)
    args_ok = isinstance(started_args, dict) and any(
        str(v).strip() for v in started_args.values())
    obs.update({
        "status": status,
        "tool_args": started_args,
        "args_nonempty": args_ok,
        "marker_output_seen": result_call is not None,
        "used_get_tool_schema": saw_get_schema,   # 观察项,不作判据
        "tool_routed": routed_seen,               # 观察项:true = 本次未走目录行逃生口(见判据②)
    })
    print(f"[catalog-drill] 观察: {json.dumps(obs, ensure_ascii=False)}")
    ok = bool(status == "success" and obs["args_nonempty"] and obs["marker_output_seen"])
    print("[catalog-drill] 判定:", "catalog 模式带参调用端到端可用 ✅" if ok
          else "逃生口仍不可用 ❌")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
