"""拐点 harness 编排器模式（W-K，`run_knee.py --orchestrator`）金测试：FakeEngine / CPU，不起服务、不载模型。

守的是什么：编排器模式要让 W1 护栏、W2 步骤边界压缩、轮首压缩与轮统计真的在测量路径上，
且四个对照臂（无管理 / 现状 / +W2 / 全开）的开关与放置语义各自如实生效——臂开关接错了，
对照表照样画得出来，只是每一列都在量同一件事。直调模式（缺省）行为不变由
test_context_knee_harness.py 原样钉住，这里只钉编排器专属 flag 在直调模式下被拒。
"""
import asyncio
import copy
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))

from context_knee import orch_tools as OT       # noqa: E402
from context_knee import run_knee as K          # noqa: E402
import tests.fake_engine as fe                  # noqa: E402
from tests.fake_engine import call              # noqa: E402
from framework.core import events               # noqa: E402
from framework.core.journal import new_turn_id  # noqa: E402
from framework.core.orchestrator import Orchestrator   # noqa: E402
from framework.memory.session import ChatSession      # noqa: E402
from framework.utils.config_loader import data_dir, load_section   # noqa: E402

LEVEL = 16384


def _item():
    return {"id": "lc-x-001", "dimension": "longchain", "lang": "zh",
            "source": {"origin": "self", "fit": "测试用题——编排器模式"},
            "task": {"user": "读一下 a.txt", "env": "vfs", "env_init": {"files": {"a.txt": "1"}}},
            "expect": {"type": "end_state",
                       "asserts": [{"path": ["files", "a.txt"], "exists": True}]}}


def _m(pt):
    return {"prompt_tokens": pt, "generation_tokens": 20, "prompt_tps": 1000.0,
            "generation_tps": 50.0}


def _plan_script(pt=100, extra_call=None):
    """update_plan → env 工具 ×2 → complete_step → 收尾正文。"""
    second = extra_call or call("read_file", path="a.txt")
    return [{"tool_calls": [call("update_plan", op="replace", steps=["读取 a.txt"])], "metrics": _m(pt)},
            {"tool_calls": [call("list_files")], "metrics": _m(pt)},
            {"tool_calls": [second], "metrics": _m(pt)},
            {"tool_calls": [call("complete_step", result="a.txt = 1")], "metrics": _m(pt)},
            {"text": "a.txt 的内容是 1。", "metrics": _m(pt)}]


def _ctx(tmp_path, placement="turn", guard=True, step=True, turn_comp=None, guard_cfg=None):
    base = copy.deepcopy(load_section("agent") or {})
    if guard_cfg:
        base["context_guard"] = {**(base.get("context_guard") or {}), **guard_cfg}
    ctx = K.OrchContext(out_dir=tmp_path / "out", agent_base=base, model_options={},
                        fill_placement=placement, guard=guard, step_compaction=step,
                        turn_compaction=(placement == "history") if turn_comp is None else turn_comp,
                        goal_verify=False)
    events.set_path(ctx.events_path)        # conftest 在用例结束时还原
    ctx.cursor = OT.EventCursor(ctx.events_path)
    return ctx


_HANDLE_RE = re.compile(r'file_extract\(path="(spill:[^"]+)"\)')


def _copy_step(st):
    return {k: (v if k == "gate" else copy.deepcopy(v)) for k, v in st.items()}


def _run(monkeypatch, ctx, script, *, level=LEVEL, bpc=False, prefix=None, item=None,
         allow_error=False, timeout=0):
    """经 _one_run_orch 跑一 run；FakeEngine 换成固定脚本并记下会话（断言 history 用）。
    脚本步 {"readback": True}：从 history 的压缩桩里取第一个 spill 句柄，发 file_extract 回读。"""
    sessions = []
    first = []          # 首次生成前一刻的 history 快照

    class _E(fe.FakeEngine):
        def __init__(self, _dry):
            super().__init__([_copy_step(st) for st in script])

        async def generate_once(self, session, **kw):
            if not sessions:
                first.append(copy.deepcopy(session.history))
            sessions.append(session)
            if self.script and self.script[0].pop("readback", False):
                blob = "\n".join(str(tr.get("response")) for m in session.history
                                  if m.get("role") == "tool" for tr in m.get("tool_responses") or [])
                handle = _HANDLE_RE.search(blob).group(1)
                self.script[0]["tool_calls"] = [call("file_extract", path=handle)]
            return await super().generate_once(session, **kw)
    monkeypatch.setattr(fe, "FakeEngine", _E)
    monkeypatch.setattr(fe.FakeBackend, "block_prefix_cache", bpc)
    item = item or _item()
    args = SimpleNamespace(seed=7, backend="fake", run_timeout=timeout)
    meta = {"model": "m", "sampling_overrides": {"sent": {}, "ignored": {}}, **K.orch_meta(ctx)}
    rec = asyncio.run(K._one_run_orch(args, None, K.fake_counter(), item, 0, level, 0, meta,
                                      prefix, ctx))
    if not allow_error:
        assert "error" not in rec, rec.get("traceback")
    _FIRST[:] = first
    return rec, (sessions[0] if sessions else None)      # 首个生成的会话 = 主会话


_FIRST: list = []       # 最近一次 _run 的首次生成前 history


def _user_idx(hist):
    return next(i for i, m in enumerate(hist) if m.get("role") == "user"
                and isinstance(m.get("content"), list))       # 题面（add_user_message 的块形态）


def _filler_tool_msgs(hist, lo, hi):
    return [m for m in hist[lo:hi] if m.get("role") == "tool"]


def _n_compacted(msgs):
    return sum(1 for m in msgs for tr in m.get("tool_responses") or [] if tr.get("_compacted"))


def _n_pseudo_users(hist, lo, hi):
    return sum(1 for m in hist[lo:hi] if m.get("role") == "user")


# ---- 1. 直调模式：编排器专属 flag 被拒；直调行记 mode=direct ----

@pytest.mark.parametrize("flag", [["--fill-placement", "turn"], ["--guard", "off"],
                                  ["--step-compaction", "off"], ["--turn-compaction", "on"],
                                  ["--goal-verify", "on"]])
def test_orchestrator_only_flags_rejected_in_direct_mode(flag, tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1",
                "--out", str(tmp_path / "d")] + flag)
    assert e.value.code == 2 and "只在 --orchestrator 下有效" in capsys.readouterr().err


def test_orchestrator_rejects_handmade_routed_prefix(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        K.main(["--dry-run", "--orchestrator", "--prefix", "routed", "--levels", "0",
                "--out", str(tmp_path / "d")])
    assert e.value.code == 2 and "--prefix mock|prod|catalog" in capsys.readouterr().err


def test_direct_mode_rows_record_mode_direct_and_null_switches(tmp_path):
    out = tmp_path / "d"
    assert K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)]) == 0
    rec = json.loads((out / "transcripts.jsonl").read_text().splitlines()[0])
    assert rec["mode"] == "direct"
    assert all(rec[k] is None for k in ("fill_placement", "guard", "step_compaction",
                                        "turn_compaction", "goal_verify",
                                        "orchestrator_cfg_source", "tool_schema_mode_effective"))
    assert "turn_status" not in rec and "turn_metrics" not in rec
    assert not (out / "sessions").exists() and not (out / "guardrail_events.jsonl").exists()


# ---- 2. 编排器干跑端到端 ----

def test_orchestrator_end_to_end_plan_steps_meta_calls_and_score(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="turn")
    rec, session = _run(monkeypatch, ctx, _plan_script(), level=0)
    assert rec["mode"] == "orchestrator" and rec["turn_status"] == "success"
    assert rec["turn_metrics"]["generations"] == 5 and "token_breakdown" in rec["turn_metrics"]
    assert rec["meta_calls"] == {"update_plan": 1, "complete_step": 1}
    names = [c["name"] for t in rec["turns"] for c in t["tool_calls"]]
    assert names == ["list_files", "read_file"]                     # 元工具不进 tool_calls
    feeds = [f for t in rec["turns"] for f in t.get("feed") or []]
    assert [f["name"] for f in feeds] == ["list_files", "read_file"]
    assert json.loads(feeds[0]["content"]) == {"files": ["a.txt"]}
    assert json.loads(feeds[1]["content"]) == {"path": "a.txt", "content": "1"}
    # 评分由 longchain 评分器算出（步数 = 只数 env 调用；终态断言过；有收尾）
    assert rec["score"]["steps"] == 2 and rec["score"]["state_ok"] is True
    assert rec["score"]["pass"] is True and rec["aborted_reason"] == ""
    assert rec["turns"][-1]["text"] == "a.txt 的内容是 1。"
    assert rec["max_total_iterations"] == 8                          # vfs 题缺省回合上限
    assert rec["context_guard_bound"] is True and len(rec["gens"]) == 5
    assert rec["gens"][0]["measure_source"] == "guard"               # 生成记账取护栏的那次量测
    assert rec["tool_effects"]["write_file"] == "write" and rec["tool_effects"]["read_file"] == "read"
    assert Path(rec["session_dir"]).is_dir() and Path(rec["session_dir"]).parent == ctx.sessions_dir
    # 填充在 level 0 下为空；题面经 add_user_message 落在 system 之后
    assert session.history[0]["role"] == "system" and _user_idx(session.history) == 1


# ---- 3. turn 放置：本轮内填充被 W2 折叠 ----

LOW_SOFT = {"soft_tokens": 50}      # 首次量测（脚本 prompt_tokens=100）即 soft


def test_turn_placement_step_compaction_folds_filler_and_spills(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="turn", guard_cfg=LOW_SOFT)
    rec, session = _run(monkeypatch, ctx, _plan_script(), bpc=True)
    hist = session.history
    assert _user_idx(hist) == 1 and session.turn_history_start_idx == 2   # 填充在 user 之后
    n_fill = rec["fill"]["n_messages"]
    fill_tools = _filler_tool_msgs(hist, 2, 2 + n_fill)
    assert fill_tools and _n_compacted(fill_tools) > 0                    # 填充观察被折成桩
    assert rec["guard_events"].get("context_soft") == 1
    assert rec["guard_events"].get("step_compaction", 0) >= 1
    assert rec["turn_metrics"]["step_compactions"] >= 1
    assert rec["turn_metrics"]["context_guard_level"] == "soft"
    spills = list((Path(rec["session_dir"]) / "spill").glob("*.txt"))
    assert spills and Path(rec["session_dir"]) == ctx.sessions_dir / session.session_id
    assert rec["turn_compaction"] == "off" and rec["fill_placement"] == "turn"
    # 轨迹只含本 run 自己的段：填充里的伪调用不进 tool_calls
    assert [c["name"] for t in rec["turns"] for c in t["tool_calls"]] == ["list_files", "read_file"]


def test_turn_placement_filler_is_in_turn_shape_and_hits_target(tmp_path, monkeypatch):
    """turn 放置的填充是轮内形态：history[2:] 到首次生成前无 user、无纯文本 assistant，末条是 tool；
    按 level 重新凑数，误差不超过单个配对的最大代价（filler 同一容差口径）。"""
    ctx = _ctx(tmp_path, placement="turn")
    rec, _ = _run(monkeypatch, ctx, _plan_script())
    pre = _FIRST[0][2:]
    assert pre and len(pre) == rec["fill"]["n_messages"]
    assert not any(m["role"] == "user" for m in pre)
    assert not any(m["role"] == "assistant" and not m.get("tool_calls") for m in pre)
    assert pre[-1]["role"] == "tool"
    assert [m["role"] for m in pre] == ["assistant", "tool"] * (len(pre) // 2)
    assert rec["fill_shape"] == "in_turn" and len(rec["fill"]["cards"]) == 3
    item = _item()
    asm = K.item_prefix(item)
    plan = K.plan_run(K.fake_counter(), item, LEVEL, 0, 7, 0, asm, fill_after_user=True)
    tol = max(plan.filler.turn_costs)
    assert abs(plan.prompt_tokens_est - LEVEL) <= tol, (plan.prompt_tokens_est, tol)
    assert abs(rec["fill"]["prompt_tokens_est"] - LEVEL) <= tol
    # 整块补对：缺口不超过一对平均代价（超过就重凑），记 fill_est_delta
    mean = sum(plan.filler.turn_costs) / len(plan.filler.turn_costs)
    assert plan.fill_est_delta == LEVEL - plan.prompt_tokens_est and plan.fill_est_delta <= mean
    assert rec["fill"]["fill_est_delta"] == LEVEL - rec["fill"]["prompt_tokens_est"]
    full = K.plan_run(K.fake_counter(), item, LEVEL, 0, 7, 0, asm)
    assert len(plan.filler.turns) > len(full.filler.turns)          # 每轮更小 → 多取配对
    assert [t.call for t in full.filler.turns] == [t.call for t in plan.filler.turns[:len(full.filler.turns)]]


# ---- 4. history 放置 + 轮首压缩：压的是历史轮，W2 不碰 ----

def test_history_placement_turn_start_compaction_but_no_step_compaction(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="history", guard_cfg=LOW_SOFT)
    rec, session = _run(monkeypatch, ctx, _plan_script(), bpc=True)
    hist = session.history
    n_fill = rec["fill"]["n_messages"]
    assert _user_idx(hist) == 1 + n_fill                                  # 填充在 user 之前
    assert session.turn_history_start_idx == 2 + n_fill                   # 锚点由 run_turn 自己置
    fill_tools = _filler_tool_msgs(hist, 1, 1 + n_fill)
    assert _n_compacted(fill_tools) > 0                                   # 轮首机械压缩压掉超预算旧观察
    assert rec["guard_events"].get("context_soft") == 1                   # W2 门槛够了……
    assert rec["guard_events"].get("step_compaction", 0) == 0             # ……但它只压本轮
    assert "step_compactions" not in rec["turn_metrics"]
    assert rec["turn_compaction"] == "on" and rec["fill_shape"] == "turns"
    assert _n_pseudo_users(hist, 1, 1 + n_fill) > 0                      # 历史轮仍是完整伪轮次


# ---- 5. 无管理臂 ----

def test_unmanaged_arm_no_guard_events_and_filler_verbatim(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="history", guard=False, step=False, turn_comp=False,
               guard_cfg=LOW_SOFT)
    rec, session = _run(monkeypatch, ctx, _plan_script(), bpc=True)
    n_fill = rec["fill"]["n_messages"]
    fill_tools = _filler_tool_msgs(session.history, 1, 1 + n_fill)
    assert fill_tools and _n_compacted(fill_tools) == 0
    assert not any(t.startswith("context_") or t == "step_compaction" for t in rec["guard_events"])
    assert "context_guard_level" not in rec["turn_metrics"]
    assert rec["gens"][0]["measure_source"] == "proxy"                    # 护栏没量，代理自己量
    assert rec["guard"] == "off" and rec["step_compaction"] == "off"
    assert rec["orchestrator_cfg_source"]["overrides"]["context_guard.enabled"] is False


# ---- 6. ceiling 终止 ----

def test_ceiling_aborts_with_complete_record(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="turn")
    # 缺省上限 65536 − 预留 12288 = ceiling 53248；量值 60000 → 应急压缩后仍超 → 终止
    rec, _ = _run(monkeypatch, ctx, _plan_script(pt=60000), level=4096)
    assert rec["aborted_reason"] == "context_guard" and rec["turn_status"] == "warning"
    assert rec["gens"] == [] and rec["guard_events"].get("turn_abort") == 1
    assert rec["score"]["aborted"] is True and rec["score"]["pass"] is False
    assert "context_guard" in rec["score"]["why"]
    assert rec["turns"][-1]["text"] == ""                         # 护栏样板不是答案
    assert "上下文已达模型上限" in rec["turn_response"]
    assert rec["score"]["final_text_ok"] is False
    # 终止前没有一次生成实际发出（gens == []）：实发峰值 = null；护栏量到的 60000 只进
    # peak_prompt_measured（那份是「量到的」，不是「发出去的」）。
    assert rec["peak_prompt_tokens"] is None and rec["peak_prompt_measured"] == 60000
    assert rec["fill"]["target_tokens"] == 4096


# ---- 7. 事件文件只落产物目录 ----

def _lines(p: Path) -> int:
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0


def test_events_written_only_under_out_dir(tmp_path):
    watched = {REPO / "backend" / "guardrail_events.jsonl", data_dir() / "guardrail_events.jsonl"}
    before = {p: _lines(p) for p in watched}
    prev = events.get_path()
    out = tmp_path / "o"
    # dry-run 脚本的 prompt_tokens = 计划估算 ≈ 32768 ≥ soft 24576 → 至少一条 context_soft
    assert K.main(["--dry-run", "--orchestrator", "--levels", "32768", "--reps", "1",
                   "--limit", "1", "--out", str(out)]) == 0
    assert {p: _lines(p) for p in watched} == before
    assert events.get_path() == prev                                      # 批次结束还原
    evs = [json.loads(ln) for ln in (out / "guardrail_events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "context_soft" for e in evs)
    rec = json.loads((out / "transcripts.jsonl").read_text().splitlines()[0])
    assert rec["guard_events"].get("context_soft") == 1
    lo, hi = rec["guard_events_lines"]
    assert hi - lo == len(evs) and lo == 0


# ---- 8. 生产工具可用桩（W-K2） ----

#: 按参数名给的有效实参（其余按类型取缺省）；文件类串成「复制 → 移走 → 扔掉」不碰题目的 a.txt
_ARG_BY_NAME = {"path": "a.txt", "source": "a.txt", "destination": "b.txt", "command": "ls",
                "code": "1 + 1", "old_string": "1", "new_string": "2", "query": "a",
                "category": "default", "optimized_query": "a", "source_path": "p.md",
                "urls": ["https://example.com"], "when": "2026-01-02 10:00",
                "spec": {"kind": "daily", "time": "09:00"}, "series": [{"name": "s", "values": [1]}]}
_ARG_OVERRIDE = {"move_file": {"source": "b.txt", "destination": "c.txt"},
                 "trash_file": {"path": "c.txt"}}
_BY_TYPE = {"string": "x", "integer": 1, "number": 1, "boolean": False, "array": ["x"],
            "object": {}}


def _valid_args(schema):
    params = schema.get("parameters") or {}
    props = params.get("properties") or {}
    out = {}
    for k in params.get("required") or []:
        spec = props.get(k) or {}
        out[k] = (_ARG_BY_NAME[k] if k in _ARG_BY_NAME
                  else (spec.get("enum") or [None])[0] or _BY_TYPE.get(spec.get("type"), "x"))
    out.update(_ARG_OVERRIDE.get(schema["name"], {}))
    return out


def _feeds(rec):
    return [f for t in rec["turns"] for f in t.get("feed") or []]


def _prod_registry(tmp_path, item=None):
    from benchmarks.model_eval.mock_envs import make_env
    item = item or _item()
    asm = K.item_prefix(item, "prod")
    env = make_env(item["task"]["env"], item["task"].get("env_init"))
    spill = OT.SpillReadTool()
    reg = OT.build_registry(tmp_path / "tools-empty", env, OT.stub_schemas(asm), spill_tool=spill)
    return reg, env, spill, asm


def _x(reg, name, **args):
    text, _render = reg.execute_tool(name, args)
    return text


def test_prod_prefix_every_stub_is_usable_and_counted(tmp_path, monkeypatch):
    item = _item()
    item["task"]["max_turns"] = 20
    asm = K.item_prefix(item, "prod")
    stubs = OT.stub_schemas(asm)
    mock = {t["name"] for t in asm.mock_tools}
    assert len(stubs) >= 40 and not ({s["name"] for s in stubs} & (mock | OT.META_NAMES))
    # 不经 call()：skill_invoke / register_* / memory_write 的实参里本就有 "name"
    calls = [{"type": "function", "name": s["name"], "arguments": _valid_args(s)} for s in stubs]
    script = [{"tool_calls": calls, "metrics": _m(100)}] + [
        {"text": "a.txt 的内容是 2。", "metrics": _m(100)} for _ in range(3)]
    ctx = _ctx(tmp_path, placement="turn")
    rec, _ = _run(monkeypatch, ctx, script, level=0, prefix=asm, item=item)
    feeds = _feeds(rec)
    assert {f["name"] for f in feeds} == {s["name"] for s in stubs}
    for f in feeds:
        assert "Unknown tool" not in f["content"], f
        assert not f["content"].lstrip().startswith("System Error"), f
    assert rec["stub_unknown"] == 0 and rec["system_errors"] == 0 and rec["stub_failed"] == 0
    assert rec["stub_served"] == {s["name"]: 1 for s in stubs}
    assert sum(rec["stub_served"].values()) == len(calls)
    # 文件链真落到 vfs：edit_file 把 a.txt 的 1 改成 2，复制件 b.txt 经 move → c.txt → trash 最终不在
    assert rec["score"]["state_ok"] is True
    edit = next(f["content"] for f in feeds if f["name"] == "edit_file")
    assert edit.startswith("OK: replaced 1 occurrence in a.txt")
    assert next(f["content"] for f in feeds if f["name"] == "trash_file") == \
        "Moved to Trash (recoverable): c.txt"
    assert set(rec["tool_effects"]) == mock


def test_stub_unknown_counts_remaining_unknown_tool_replies():
    hist = [{"role": "assistant", "content": "", "tool_calls": [call("x"), call("y")]},
            {"role": "tool", "tool_responses": [
                {"name": "x", "response": json.dumps({"error": "Unknown tool: x"})},
                {"name": "y", "response": "No results for 'q' in category 'c': …"}]}]
    _traj, extra = OT.traj_from_history(hist, 0, item_id="i", rep=0, env=None)
    assert extra["stub_unknown"] == 1 and extra["stub_served"] == {} and extra["stub_failed"] == 0


def test_rag_stubs_classify_as_empty_retrieval(tmp_path):
    from framework.core import retrieval_evidence as RE
    reg, _env, _spill, _asm = _prod_registry(tmp_path)
    args = {"query": "季度营收", "category": "default", "optimized_query": "季度营收"}
    text = _x(reg, "rag_search", **args)
    assert text.startswith("No results for '季度营收' in category 'default': ")
    assert RE.observation("rag_search", args, text, None, False)["kind"] == "empty"
    ctx_args = {"source_path": "doc.md", "seq": 3}
    ctx_text = _x(reg, "rag_get_context", **ctx_args)
    assert RE.observation("rag_get_context", ctx_args, ctx_text, None, False)["kind"] == "empty"
    from framework.core.orchestrator import _resp_failed       # 编排层失败判据（文本回落层）
    assert not _resp_failed(text) and not _resp_failed(ctx_text)


def test_rag_search_stub_does_not_block_delivery_or_complete_step(tmp_path, monkeypatch):
    from framework.core import retrieval_evidence as RE
    item = _item()
    asm = K.item_prefix(item, "prod")
    script = [{"tool_calls": [call("update_plan", op="replace", steps=["检索并写入"])], "metrics": _m(100)},
              {"tool_calls": [call("rag_search", query="a", category="default",
                                   optimized_query="a")], "metrics": _m(100)},
              {"tool_calls": [call("write_file", path="out.txt", content="结论")], "metrics": _m(100)},
              {"tool_calls": [call("read_file", path="out.txt")], "metrics": _m(100)},
              {"tool_calls": [call("complete_step", result="写入 out.txt")], "metrics": _m(100)},
              {"text": "已写入 out.txt。", "metrics": _m(100)}]
    ctx = _ctx(tmp_path, placement="turn")
    rec, session = _run(monkeypatch, ctx, script, level=0, prefix=asm, item=item)
    blob = json.dumps(session.history, ensure_ascii=False)
    assert "delivery blocked" not in blob and "检索未完成" not in blob
    assert RE.REPAIR_NOTE[:60] not in blob
    assert rec["system_errors"] == 0 and rec["turn_status"] == "success"
    assert rec["meta_calls"] == {"update_plan": 1, "complete_step": 1}
    assert json.loads(next(f["content"] for f in _feeds(rec) if f["name"] == "write_file")) == {
        "status": "written", "path": "out.txt"}
    assert rec["stub_served"] == {"rag_search": 1}


def test_shell_emulation_over_vfs(tmp_path):
    item = _item()
    item["task"]["env_init"]["files"] = {"a.txt": "alpha\nBeta 2\nalpha 3", "d/b.txt": "x"}
    reg, _env, _spill, _asm = _prod_registry(tmp_path, item)
    assert _x(reg, "run_shell_command", command="ls") == "exit_code: 0\nstdout:\na.txt\nd"
    assert _x(reg, "run_shell_command", command="cat a.txt") == \
        "exit_code: 0\nstdout:\nalpha\nBeta 2\nalpha 3"
    assert _x(reg, "run_shell_command", command="grep -n alpha a.txt") == \
        "exit_code: 0\nstdout:\n1:alpha\n3:alpha 3"
    assert _x(reg, "run_shell_command", command="find . -name '*.txt'") == \
        "exit_code: 0\nstdout:\na.txt\nd/b.txt"
    assert _x(reg, "run_shell_command", command="grep zzz a.txt").startswith(
        "exit_code: 1 (benign: negative result, not an error)")
    miss = _x(reg, "run_shell_command", command="cat nope.txt")
    assert miss.startswith("exit_code: 1\n") and "No such file or directory" in miss
    for cmd in ("python3 run.py", "cat a.txt | wc -l", "echo x > a.txt"):
        out = _x(reg, "run_shell_command", command=cmd)
        assert out.startswith("exit_code: 127\nstderr:\n") and "offline sandbox emulates only" in out
    assert reg.effect_of("run_shell_command", {"command": "ls"}) == "read"
    assert reg.effect_of("run_shell_command", {"command": "rm a.txt"}) == "write"


def test_vfs_bridged_file_tools_reflect_and_change_env_state(tmp_path):
    reg, env, spill, _asm = _prod_registry(tmp_path)
    assert spill.bridge is not None and reg._tools["list_dir"]._bridge is spill.bridge
    _x(reg, "write_file", path="notes/n.md", content="hello world")       # EnvTool 写
    listing = _x(reg, "list_dir", path="~")
    assert listing.splitlines()[0] == ".  (2 entries)" and "  [dir]  notes/" in listing
    assert "a.txt" in listing
    assert "notes/n.md" in _x(reg, "find_files", query="n.md")
    assert "notes/n.md" in _x(reg, "find_files", query="world")           # 名字无命中退内容
    ok = _x(reg, "edit_file", path="notes/n.md", old_string="world", new_string="there")
    assert ok.startswith("OK: replaced 1 occurrence in notes/n.md")
    assert env.snapshot()["files"]["notes/n.md"] == "hello there"
    bad = _x(reg, "edit_file", path="notes/n.md", old_string="nope", new_string="x")
    assert bad.startswith("System Error: old_string was NOT found in the file.")   # 写类失败 = 生产前缀
    assert _x(reg, "trash_file", path="nope.txt") == "System Error: path not found: nope.txt"
    assert _x(reg, "copy_file", source="a.txt", destination="notes/n.md").startswith(
        "System Error: destination already exists")
    assert _x(reg, "list_dir", path="nope") == "Error: path not found: nope"       # 读类失败不判失败
    assert reg._tools["edit_file"].failed == 1 and reg._tools["trash_file"].failed == 1
    assert _x(reg, "file_extract", path="notes/n.md") == \
        "[n.md lines 1-1 of 1]\n    1 | hello there"
    assert _x(reg, "copy_file", source="a.txt", destination="notes/") == "Copied a.txt → notes/a.txt"
    assert _x(reg, "trash_file", path="notes/a.txt").startswith("Moved to Trash")
    assert sorted(env.snapshot()["files"]) == ["a.txt", "notes/n.md"]
    assert spill.served == 1 and spill.readbacks == 0


def test_stub_effects_mirror_production_and_no_vfs_falls_to_offline(tmp_path):
    reg, _env, _spill, _asm = _prod_registry(tmp_path)
    t = reg._tools
    assert (t["list_dir"].effect, t["list_dir"].is_concurrency_safe) == ("read", True)
    assert (t["edit_file"].effect, reg.is_read_only("edit_file", {})) == ("write", False)
    assert reg.is_read_only("read_ui_tree", {}) and not reg.is_concurrency_safe("read_ui_tree", {})
    assert t["rag_search"].effect == "read" and t["notify_user"].effect == "write"
    assert not any(getattr(x, "approval_pinned", False) or x.requires_approval
                   for x in t.values())
    todo = {"id": "t", "task": {"user": "u", "env": "todo", "env_init": {}}}
    reg2, _e2, spill2, _a2 = _prod_registry(tmp_path / "todo", todo)
    assert spill2.bridge is None and reg2._tools["list_dir"].kind == "offline"
    for name, args in (("list_dir", {"path": "~"}), ("file_extract", {"path": "a.txt"}),
                       ("run_shell_command", {"command": "ls"})):
        out = _x(reg2, name, **args)
        assert out.startswith(f"{name} is not available in this offline sandbox"), out
    assert "Use the task's own tools" in _x(reg2, "web_research", query="x")


def test_run_math_stub_evaluates_arithmetic(tmp_path):
    reg, _env, _spill, _asm = _prod_registry(tmp_path)
    assert _x(reg, "run_math", code="1200 + 830 + 455") == "2485"
    assert _x(reg, "run_math", code="a = sqrt(16)\nprint(a)\nround(10 / 3, 2)") == "4.0\n3.33"
    assert _x(reg, "run_math", code="x = 2").startswith("(no output")
    assert _x(reg, "run_math", code="1/0") == \
        "Math sandbox error (fix the code and retry):\nZeroDivisionError: division by zero"
    assert _x(reg, "run_math", code="__import__('os')").startswith("Math sandbox error")
    for code in ("[0] * 10**9", "10**3000 * 10**3000", "2**20000"):
        out = _x(reg, "run_math", code=code)
        assert out.startswith("Math sandbox error") and "OverflowError" in out, code
    assert _x(reg, "get_current_time") == "2026-01-01 Thursday 09:00:00 JST (UTC+0900)"


def test_offline_stub_is_read_only_so_action_claim_is_pushed_back(tmp_path, monkeypatch):
    from framework.core.orchestrator import _is_local_mutation
    reg, _env, _spill, _asm = _prod_registry(tmp_path)
    for name in ("calendar_add", "notify_user", "memory_write", "deck_init"):
        assert reg._tools[name].kind == "offline" and reg.is_read_only(name, {}), name
        assert not _is_local_mutation(reg, {"name": name, "arguments": {}}), name
        assert reg._tools[name].effect == "write"                   # 审批分档口径仍照生产
    item = _item()
    script = [{"tool_calls": [call("calendar_add", title="周会", when="2026-01-02 10:00")],
               "metrics": _m(100)},
              {"text": "已为你创建日程。", "metrics": _m(100)},
              {"text": "离线沙箱里日历不可用，日程没有创建。", "metrics": _m(100)}]
    rec, _ = _run(monkeypatch, _ctx(tmp_path / "r", placement="turn"), script, level=0,
                  prefix=K.item_prefix(item, "prod"), item=item)
    assert rec["guard_events"].get("action_claim_nudge") == 1      # D2 零变更守门照常推回


def test_bridged_write_failure_is_failed_and_counted_apart_from_system_errors(tmp_path, monkeypatch):
    item = _item()
    script = [{"tool_calls": [call("edit_file", path="a.txt", old_string="zzz", new_string="y")],
               "metrics": _m(100)},
              {"tool_calls": [call("trash_file")], "metrics": _m(100)},       # 缺必填 path
              {"text": "改好了。", "metrics": _m(100)},
              {"text": "edit_file 失败：a.txt 里没有 zzz。", "metrics": _m(100)}]
    rec, _ = _run(monkeypatch, _ctx(tmp_path, placement="turn"), script, level=0,
                  prefix=K.item_prefix(item, "prod"), item=item)
    feeds = _feeds(rec)
    assert feeds[0]["content"].startswith("System Error: old_string was NOT found")
    assert feeds[1]["content"].startswith("System Error: Exception occurred while executing "
                                          "'trash_file': execute() missing required argument(s): 'path'")
    assert rec["stub_failed"] == 2 and rec["system_errors"] == 0
    assert rec["guard_events"].get("failure_swallow_nudge") == 1   # 编排层把它判成失败


def test_bridged_reads_do_not_consume_env_read_faults(tmp_path):
    item = _item()
    item["task"]["env_init"]["faults"] = {"read_file": {"fail_first_n": 1, "error": "EBUSY: retry"}}
    reg, _env, _spill, _asm = _prod_registry(tmp_path, item)
    assert _x(reg, "run_shell_command", command="cat a.txt") == "exit_code: 0\nstdout:\n1"
    assert _x(reg, "file_extract", path="a.txt").endswith("    1 | 1")
    assert "System Error" not in _x(reg, "copy_file", source="a.txt", destination="b.txt")
    assert json.loads(_x(reg, "read_file", path="a.txt")) == {"error": "EBUSY: retry"}   # 故障留给模型


def test_shell_grep_options_bre_and_tail_from_line(tmp_path):
    item = _item()
    item["task"]["env_init"]["files"] = {"a.txt": "foo\nbar\n(x)\nfoo|bar\nend"}
    reg, _env, _spill, _asm = _prod_registry(tmp_path, item)
    for cmd in ("grep -A 2 foo a.txt", "grep --color foo a.txt", "grep -C1 foo a.txt"):
        out = _x(reg, "run_shell_command", command=cmd)
        assert out.startswith("exit_code: 2\nstderr:\ngrep: unsupported option"), cmd
    assert _x(reg, "run_shell_command", command="grep 'foo\\|end' a.txt") == \
        "exit_code: 0\nstdout:\nfoo\nfoo|bar\nend"                      # BRE 交替
    assert _x(reg, "run_shell_command", command="grep 'foo|bar' a.txt") == \
        "exit_code: 0\nstdout:\nfoo|bar"                                  # BRE 裸 | 是字面量
    assert _x(reg, "run_shell_command", command="grep '(x)' a.txt") == "exit_code: 0\nstdout:\n(x)"
    assert _x(reg, "run_shell_command", command="grep -E 'foo|end' a.txt") == \
        "exit_code: 0\nstdout:\nfoo\nfoo|bar\nend"
    assert _x(reg, "run_shell_command", command="tail -n +4 a.txt") == \
        "exit_code: 0\nstdout:\nfoo|bar\nend"
    assert _x(reg, "run_shell_command", command="tail -n 1 a.txt") == "exit_code: 0\nstdout:\nend"


def test_registry_holds_only_stubs_and_env_tools(tmp_path):
    from benchmarks.model_eval.mock_envs import make_env
    item = _item()
    asm = K.item_prefix(item, "prod")
    env = make_env("vfs", item["task"]["env_init"])
    spill = OT.SpillReadTool()
    reg = OT.build_registry(tmp_path / "tools-empty", env, OT.stub_schemas(asm), spill_tool=spill)
    kinds = {type(t).__name__ for t in reg._tools.values()}
    assert kinds == {"UsableStub", "EnvTool", "SpillReadTool"}
    assert reg._tools["file_extract"] is spill                    # prod 档的 file_extract 桩被顶替
    prod_fe = next(s for s in OT.stub_schemas(asm) if s["name"] == "file_extract")
    assert reg.get_all_schemas()[[s["name"] for s in reg.get_all_schemas()].index("file_extract")] \
        == prod_fe                                                # 声明取生产那份、位置不变
    assert [s["name"] for s in reg.get_all_schemas()] == [s["name"] for s in OT.stub_schemas(asm)] \
        + [t["name"] for t in env.tools()]
    # mock 档：最小声明、排在 env 工具之后
    reg2 = OT.build_registry(tmp_path / "t2", env, [], spill_tool=OT.SpillReadTool())
    assert [s["name"] for s in reg2.get_all_schemas()][-1] == "file_extract"


def test_orchestrator_catalog_prefix_dry_run_and_disclosure(tmp_path, monkeypatch):
    out = tmp_path / "c"
    assert K.main(["--dry-run", "--orchestrator", "--prefix", "catalog", "--levels", "0",
                   "--reps", "1", "--limit", "1", "--out", str(out)]) == 0
    rec = json.loads((out / "transcripts.jsonl").read_text().splitlines()[0])
    assert "error" not in rec, rec.get("traceback")
    assert rec["prefix_mode"] == "catalog" and rec["tool_schema_mode_effective"] == "catalog"
    assert rec["stub_unknown"] == 0 and isinstance(rec["stub_served"], dict)
    assert K.orch_model_options({"a": 1}, "prod") == {"a": 1}
    # 经 _one_run_orch 实发的 prompt 面：mock 工具恒完整（与直调 catalog 档同），生产集只剩目录行，
    # 生产 core 照旧完整；语法面仍是完整 schema
    seen = []
    orig = fe.FakeEngine.generate_once

    async def spy(self, session, tools=None, grammar_tools=None, **kw):
        seen.append((list(tools or []), list(grammar_tools or [])))
        return await orig(self, session, tools=tools, grammar_tools=grammar_tools, **kw)
    monkeypatch.setattr(fe.FakeEngine, "generate_once", spy)
    item = _item()
    asm = K.item_prefix(item, "catalog")
    ctx = _ctx(tmp_path, placement="turn")
    ctx.model_options = K.orch_model_options({}, "catalog")
    _run(monkeypatch, ctx, _plan_script(), level=0, prefix=asm, item=item)
    decl = {d["name"]: d for d in seen[0][0]}
    assert "get_tool_schema" in decl
    for t in asm.mock_tools:                                         # 每件 mock 工具完整 schema
        assert decl[t["name"]]["parameters"] == t["parameters"], t["name"]
    catalog_only = [n for n, d in decl.items() if "parameters" not in d]
    assert "web_read" in catalog_only and "rag_search" in catalog_only
    assert not set(catalog_only) & {t["name"] for t in asm.mock_tools}
    assert "parameters" in decl["run_shell_command"]                 # 生产 core 仍完整
    grammar = {g["name"]: g for g in seen[0][1]}
    assert grammar["web_read"]["parameters"]["required"] == ["urls"]
    # 只动本 run 的 cfg 副本：prod 档原样
    base = {"tool_routing": {"top_k": 6, "core": ["edit_file"]}}
    assert OT.keep_mock_tools_full(base, K.item_prefix(item, "prod")) is base
    assert OT.keep_mock_tools_full(base, asm)["tool_routing"]["core"][0] == "edit_file"
    assert base["tool_routing"]["core"] == ["edit_file"]


def test_build_registry_rejects_non_empty_tools_dir(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "x.py").write_text("")
    with pytest.raises(RuntimeError, match="空目录"):
        OT.build_registry(d, None, [])


# ---- 9. resume / append 口径 ----

def test_resume_compat_rejects_mixing_direct_and_orchestrator(tmp_path):
    base = ["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1"]
    d = tmp_path / "d"
    assert K.main(base + ["--out", str(d)]) == 0
    with pytest.raises(SystemExit, match="W-K"):
        K.main(base + ["--out", str(d), "--orchestrator", "--append"])
    o = tmp_path / "o"
    assert K.main(base + ["--out", str(o), "--orchestrator"]) == 0
    with pytest.raises(SystemExit, match="W-K"):
        K.main(base + ["--out", str(o), "--append"])                       # 直调追加进编排器目录
    with pytest.raises(SystemExit, match="W-K"):
        K.main(base + ["--out", str(o), "--orchestrator", "--guard", "off", "--resume"])
    with pytest.raises(SystemExit, match="W-K"):
        K.main(base + ["--out", str(o), "--orchestrator", "--fill-placement", "history", "--append"])
    # 同口径：接受
    assert K.main(base[:2] + ["0,4096"] + base[3:] + ["--out", str(o), "--orchestrator",
                                                      "--resume"]) == 0
    t = o / "transcripts.jsonl"
    rows = [json.loads(ln) for ln in t.read_text().splitlines()]
    assert len(rows) == 2 and {r["mode"] for r in rows} == {"orchestrator"}
    assert rows[0]["fill_shape"] == "in_turn" and rows[0]["guard_thresholds"]["soft"] == 24576
    # 旧行缺 mode = 直调
    K.check_resume_compat(d / "transcripts.jsonl", "dry-run/fake", {}, 7, {},
                          harness_mode=K.harness_mode_of({}))


@pytest.mark.parametrize("key,patch", [("guard_thresholds", {"soft": 1}),
                                       ("step_compaction_cfg", {"keep_recent": 9}),
                                       ("fill_shape", "turns"),
                                       ("tool_schema_mode_effective", "catalog"),
                                       ("agent_cfg_fingerprint", "0" * 16)])
def test_resume_compat_rejects_different_effective_cfg(tmp_path, key, patch):
    """覆盖后的生效阈值 / W2 参数 / 填充形态不同 → 续跑拒。"""
    base = ["--dry-run", "--orchestrator", "--levels", "0", "--reps", "1", "--limit", "1"]
    o = tmp_path / "o"
    assert K.main(base + ["--out", str(o)]) == 0
    t = o / "transcripts.jsonl"
    rows = [json.loads(ln) for ln in t.read_text().splitlines()]
    for r in rows:
        r[key] = {**r[key], **patch} if isinstance(patch, dict) else patch
    t.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    with pytest.raises(SystemExit, match="W-K"):
        K.main(base[:3] + ["0,4096"] + base[4:] + ["--out", str(o), "--resume"])


# ---- 10. 续跑语义钉子：预置锚点在 keep_recent_calls=True 下沿用 ----

class _Snap(fe.FakeEngine):
    def __init__(self, script):
        super().__init__(script)
        self.snaps = []

    async def generate_once(self, session, **kw):
        self.snaps.append({k: getattr(session, k, None) for k in (
            "turn_history_start_idx", "turn_step_start_idx", "turn_peak_prompt_tokens",
            "turn_context_guard_level", "turn_step_compactions", "turn_compaction_freed_chars",
            "turn_full_renders", "carried_iterations", "turn_thinking_replay")})
        return await super().generate_once(session, **kw)


INITIAL = {"turn_step_start_idx": None, "turn_peak_prompt_tokens": 0,
           "turn_context_guard_level": None, "turn_step_compactions": 0,
           "turn_compaction_freed_chars": 0, "turn_full_renders": 0, "carried_iterations": 0,
           "turn_thinking_replay": None}


def _anchored_session(tmp_path, sid):
    s = ChatSession(sid, tmp_path / "sessions", system_prompt="sys")
    s.add_user_message("题面")
    s.history[2:2] = [{"role": "user", "content": "伪问"},
                      {"role": "assistant", "thoughts": "", "content": "伪答", "tool_calls": []}]
    s.turn_history_start_idx = 2
    s.journal.emit("turn_queued", {"prompt": "题面", "attachments": [], "gen": {}, "origin": "chat"},
                   turn_id=new_turn_id())
    s.journal.emit("turn_started", {})
    return s


@pytest.mark.parametrize("keep", [True, False])
def test_keep_recent_calls_keeps_preset_anchor_and_fresh_session_defaults(tmp_path, keep):
    s = _anchored_session(tmp_path, f"k{int(keep)}")
    assert {k: getattr(s, k, None) for k in INITIAL} == INITIAL           # 构造缺省即初值
    eng = _Snap([{"text": "好", "metrics": _m(100)}])
    orch = Orchestrator(eng, OT.build_registry(tmp_path / "t", None, []),
                        {"goal_verify": False, "thinking_mode": "manual"})   # 不 bind：不量测
    r = asyncio.run(orch.run_turn(s, journal=s.journal, keep_recent_calls=keep,
                                  compact_history=False, max_tokens=256, enable_thinking=False))
    assert r["status"] == "success"
    snap = eng.snaps[0]
    if keep:      # 续跑语义：锚点沿用、轮字段未被重置（= 构造初值）
        assert snap["turn_history_start_idx"] == 2
        assert {k: snap[k] for k in INITIAL} == INITIAL
    else:         # 新用户轮：锚点被重置到 history 末尾——证明上面的断言分得清两条路
        assert snap["turn_history_start_idx"] == 4
        assert snap["turn_step_start_idx"] == 4


@pytest.mark.parametrize("old", [None, "unknown-tool"])
def test_resume_refuses_orchestrator_dir_with_other_stub_impl(tmp_path, old):
    """W-K2 前的编排器目录（行缺 stub_impl = 生产工具回 Unknown tool）或别的桩实现：续跑 / 追加拒。"""
    base = ["--dry-run", "--orchestrator", "--levels", "0", "--reps", "1", "--limit", "1"]
    o = tmp_path / "o"
    assert K.main(base + ["--out", str(o)]) == 0
    t = o / "transcripts.jsonl"
    rows = [json.loads(ln) for ln in t.read_text().splitlines()]
    assert {r["stub_impl"] for r in rows} == {K.STUB_IMPL}
    for r in rows:
        if old is None:
            r.pop("stub_impl")
        else:
            r["stub_impl"] = old
    t.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    with pytest.raises(SystemExit, match="W-K"):
        K.main(base[:3] + ["0,4096"] + base[4:] + ["--out", str(o), "--resume"])


def test_resume_compat_orchestrator_rows_missing_new_keys_not_compared(tmp_path):
    base = ["--dry-run", "--orchestrator", "--levels", "0", "--reps", "1", "--limit", "1"]
    o = tmp_path / "o"
    assert K.main(base + ["--out", str(o)]) == 0
    t = o / "transcripts.jsonl"
    rows = [json.loads(ln) for ln in t.read_text().splitlines()]
    for r in rows:
        for k in ("agent_cfg_fingerprint", "tool_schema_mode_effective", "guard_thresholds"):
            r.pop(k)
    t.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    assert K.main(base[:3] + ["0,4096"] + base[4:] + ["--out", str(o), "--resume"]) == 0


# ---- 一审 M1：审计子生成不顶替主会话 ----

def test_goal_verify_audit_generation_does_not_hijack_main_session(tmp_path, monkeypatch):
    released = []
    monkeypatch.setattr(fe.FakeBackend, "release_cache", lambda self, h: released.append(h))
    ctx = _ctx(tmp_path, placement="turn")
    ctx.goal_verify = True
    script = [{"tool_calls": [call("update_plan", op="replace", steps=["一", "二", "三"])], "metrics": _m(100)},
              {"tool_calls": [call("list_files")], "metrics": _m(100)},
              {"tool_calls": [call("complete_step", result="1"), call("complete_step", result="2"),
                              call("complete_step", result="3")], "metrics": _m(100)},
              {"text": "a.txt 的内容是 1。", "metrics": _m(100)},
              {"text": "VERDICT: PASS\nok", "metrics": _m(30)}]       # 收尾审计（EphemeralSession）
    rec, session = _run(monkeypatch, ctx, script, level=0)
    assert rec["turn_status"] == "success" and rec["goal_verify"] == "on"
    assert rec["audit_generations"] == 1 and len(rec["gens"]) == 4    # 审计不进 gens
    assert rec["full_renders"] is not None                            # 取的是主会话
    assert rec["guard_events"].get("goal_verify") == 1
    assert len(released) == 1 and session.brain_memory_state is None  # 释放的是主会话缓存


def test_orch_proxy_measure_prompt_only_when_inner_has_it():
    class _Bare:
        backend = fe.FakeBackend()

        async def generate_once(self, session, **kw):
            return {}
    assert not hasattr(K.OrchKneeEngine(_Bare()), "measure_prompt")
    assert hasattr(K.OrchKneeEngine(fe.FakeEngine([])), "measure_prompt")


# ---- 一审 M2：spill 句柄回读 ----

def test_spill_handle_readback_returns_original_and_rejects_others(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="turn", guard_cfg=LOW_SOFT)
    script = _plan_script()[:-1] + [
        {"readback": True, "metrics": _m(100)},
        {"tool_calls": [call("file_extract", path="/etc/hosts")], "metrics": _m(100)},
        {"tool_calls": [call("file_extract", path="spill:../../../etc/hosts")], "metrics": _m(100)},
        {"text": "a.txt 的内容是 1。", "metrics": _m(100)}]
    rec, session = _run(monkeypatch, ctx, script, bpc=True)
    assert rec["guard_events"].get("step_compaction", 0) >= 1
    feeds = [f for t in rec["turns"] for f in t.get("feed") or [] if f["name"] == "file_extract"]
    assert len(feeds) == 3
    spills = {p.read_text(encoding="utf-8") for p in (Path(rec["session_dir"]) / "spill").glob("*.txt")}
    assert feeds[0]["content"] in spills                              # 拿回折叠前原文
    assert feeds[1]["content"] == "Error: file not found: /etc/hosts"  # 非 spill = 桩读 vfs
    assert "not found" in json.loads(feeds[2]["content"])["error"]   # 穿越拒
    assert rec["spill_readbacks"] == 1 and rec["stub_served"] == {"file_extract": 1}


def test_spill_tool_rejects_traversal_and_foreign_session(tmp_path):
    s = ChatSession("sp", tmp_path / "sessions", system_prompt="sys")
    (s.session_dir / "spill").mkdir()
    (s.session_dir / "spill" / "abcd1234.txt").write_text("原文", encoding="utf-8")
    (s.session_dir / "secret.txt").write_text("x", encoding="utf-8")
    t = OT.SpillReadTool()
    t.session = s
    assert t.execute(path="spill:abcd1234") == "原文" and t.readbacks == 1
    for bad in ("spill:../secret", "spill:..%2Fsecret", "spill:abcd1234/../../secret"):
        assert "not found" in json.loads(t.execute(path=bad))["error"]
    foreign = t.execute(path=str(s.session_dir / "secret.txt"))     # 无 vfs 桥 = 离线不可用
    assert foreign.startswith("file_extract is not available in this offline sandbox")
    assert "原文" not in foreign and t.served == 1
    t.session = None
    assert "not found" in json.loads(t.execute(path="spill:abcd1234"))["error"]


# ---- 一审 m7 / m13：超时、异常路径的事件结算与全局还原 ----

def test_orchestrator_timeout_records_events_and_session(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="turn", guard_cfg=LOW_SOFT)
    script = [{"tool_calls": [call("list_files")], "metrics": _m(100)},
              {"gate": asyncio.Event(), "text": "never", "metrics": _m(100)}]
    rec, _ = _run(monkeypatch, ctx, script, level=0, allow_error=True, timeout=0.3)
    assert rec["error"] == "timeout" and rec["mode"] == "orchestrator"
    assert rec["stub_served"] == {} and rec["stub_unknown"] == 0 and rec["stub_failed"] == 0
    assert rec["guard_events"].get("context_soft") == 1 and rec["guard_events_lines"][1] >= 1
    assert Path(rec["session_dir"]).is_dir() and len(rec["gens"]) == 1


def test_orchestrator_run_exception_settles_event_cursor(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, placement="turn", guard_cfg=LOW_SOFT)
    ok, _ = _run(monkeypatch, ctx, _plan_script(), level=0)
    no_env = _item()
    del no_env["task"]["env"]
    bad, _ = _run(monkeypatch, ctx, _plan_script(), level=0, item=no_env, allow_error=True)
    assert "编排器模式只支持带 mock env" in bad["error"]
    assert (bad["stub_served"], bad["stub_unknown"], bad["stub_failed"]) == ({}, 0, 0)
    assert bad["guard_events"] == {} and bad["guard_events_lines"] == [ok["guard_events_lines"][1]] * 2
    nxt, _ = _run(monkeypatch, ctx, _plan_script(), level=0)
    assert nxt["guard_events_lines"][0] == ok["guard_events_lines"][1]   # 游标行号不漂


def test_amain_exception_restores_events_path_and_approval_policy(tmp_path, monkeypatch):
    from framework.core import approval_policy
    prev = events.get_path()
    pol = approval_policy.current()

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(K, "build_prefixes", boom)
    with pytest.raises(RuntimeError, match="boom"):
        K.main(["--dry-run", "--orchestrator", "--levels", "0", "--out", str(tmp_path / "x")])
    assert events.get_path() == prev and approval_policy.current() is pol
