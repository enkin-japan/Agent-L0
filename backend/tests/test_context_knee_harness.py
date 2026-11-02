"""拐点 harness 金测试（benchmarks/context_knee，W0）：填充形态、计量单调、事实卡、聚合算术、护栏记录。

守的是什么：拐点曲线直接决定 W1/W2 的阈值。填充若不是编排层同构的形态（压缩层看不见、
方言渲染出畸形）、高档不是低档的超集、回忆评分判错、Pass^2 算错——曲线照样画得出来，
只是错的。全部 CPU、FakeEngine，零模型依赖。
"""
import asyncio
import copy
import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "benchmarks") not in sys.path:
    sys.path.append(str(REPO / "benchmarks"))

from context_knee import filler as F            # noqa: E402
from context_knee import run_knee as K          # noqa: E402
from context_knee import summarize as S         # noqa: E402
from benchmarks.model_eval.executor import run_item   # noqa: E402
from framework.dialects.gemma4.prompt_builder import PromptBuilder   # noqa: E402
from framework.memory.compactor import compact_tool_observations    # noqa: E402
from tests.fake_engine import FakeEngine, call   # noqa: E402

VFS_TOOLS = ["list_files", "read_file", "write_file", "append_file", "delete_file"]


def _cost(msgs):
    return math.ceil(len(json.dumps(msgs, ensure_ascii=False)) / 3.5)


def _build(target, rep=0, tools=VFS_TOOLS, seed=7, task_key="tk", rotation=0, **kw):
    return F.build_filler(target, seed=seed, rep=rep, tool_names=tools, turn_cost=_cost,
                          task_key=task_key, rotation=rotation, **kw)


class _Tok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return [1]


class _Proc:
    tokenizer = _Tok()


# ---- ① 单调 + 超集 ----

def test_filler_targets_monotone_and_levels_are_supersets():
    levels = [3000, 8000, 16384, 24576, 32768]
    fills = [_build(t) for t in levels]
    ns = [len(f.turns) for f in fills]
    ests = [f.est_tokens for f in fills]
    assert ns == sorted(ns) and len(set(ns)) == len(ns), ns
    assert ests == sorted(ests)
    for f, t in zip(fills, levels):
        # 贴近目标：误差不超过单轮最大代价
        assert abs(f.est_tokens - t) <= max(f.turn_costs), (t, f.est_tokens)
    lo, hi = fills[1], fills[-1]
    # 伪轮次（不含卡）是前缀：高档只增不换
    for a, b in zip(lo.turns, hi.turns):
        assert a.messages() == b.messages()
    # 卡值同一 rep 跨档不变；rep 之间不同，且填充内容不同
    assert [c["value"] for c in lo.cards] == [c["value"] for c in hi.cards]
    other = _build(8000, rep=1)
    assert [c["value"] for c in other.cards] != [c["value"] for c in lo.cards]
    assert other.turns[0].messages() != lo.turns[0].messages()
    # 确定性
    assert _build(8000).fingerprint() == lo.fingerprint()
    # 填充按题独立：换题面指纹 → 换伪轮次流（卡值仍只随 rep 变）
    other_task = _build(8000, task_key="tk2")
    assert other_task.turns[0].messages() != lo.turns[0].messages()
    assert _build(0).messages == [] and _build(0).cards == []


# ---- ② 形态 + 渲染 + 压缩层可见 ----

def test_filler_message_shapes_match_orchestrator():
    f = _build(16384)
    msgs = f.messages
    assert len(msgs) == 4 * len(f.turns)
    for i in range(0, len(msgs), 4):
        u, a, t, c = msgs[i:i + 4]
        assert u["role"] == "user" and isinstance(u["content"], str) and u["content"]
        assert a["role"] == "assistant" and a["content"] == "" and len(a["tool_calls"]) == 1
        assert a["tool_calls"][0]["type"] == "function"
        n_sent = a["thoughts"].count(". ") + 1
        assert isinstance(a["thoughts"], str) and 1 <= n_sent <= 3, a["thoughts"]
        assert t["role"] == "tool" and len(t["tool_responses"]) == 1
        tr = t["tool_responses"][0]
        assert "response" in tr and "content" not in tr
        assert tr["name"] == a["tool_calls"][0]["name"]
        json.loads(tr["response"])                      # mock_envs 同款 JSON 回执
        assert c["role"] == "assistant" and c["content"] and not c["tool_calls"]
    read_obs = [m["tool_responses"][0] for m in msgs if m["role"] == "tool"
                and m["tool_responses"][0]["name"] == "read_file"
                and not m["tool_responses"][0]["_failed"]]
    lens = [len(o["response"]) for o in read_obs]
    assert min(lens) >= 300 and max(lens) <= 3400, (min(lens), max(lens))
    assert all(o.get("_safe") is True and o.get("_target") for o in read_obs)


@pytest.mark.parametrize("dialect", ["gemma4", "qwen35"])
def test_filler_renders_through_dialect(dialect):
    from framework.dialects import get_dialect_class
    f = _build(8000)
    d = get_dialect_class(dialect).create(_Proc(), options={}, env={})
    history = [{"role": "system", "content": "sys"}, *f.messages,
               {"role": "user", "content": "task"}]
    prompt = d.build_prompt(messages=history, tools=[{"name": "read_file", "description": "r",
                                                      "parameters": {"type": "object",
                                                                     "properties": {}}}],
                            enable_thinking=True, include_env=False)
    for c in f.cards:
        assert prompt.count(c["value"]) == 1, c          # 卡进了 prompt，且只一次
    first_thought = f.messages[1]["thoughts"]
    assert first_thought in prompt
    assert "_safe" not in prompt and "_target" not in prompt
    if dialect == "gemma4":
        pb = PromptBuilder(_Proc(), env={})
        p2 = pb.build(messages=history, tools=None, include_env=False)
        assert p2.count("<|tool_response>") == len(f.turns)


def test_filler_observations_visible_to_compactor():
    """response 键 → compactor 量得到体积、能驱逐；换成 executor 的 content 键就看不见。"""
    f = _build(16384)
    hist = copy.deepcopy(f.messages)
    n = compact_tool_observations(hist, keep_recent=2, digest_chars=80, budget_chars=2000)
    assert n > 0
    as_content = copy.deepcopy(f.messages)
    for m in as_content:
        for tr in m.get("tool_responses") or []:
            tr["content"] = tr.pop("response")
            tr.pop("_safe", None)
    assert compact_tool_observations(as_content, keep_recent=2, digest_chars=80,
                                     budget_chars=2000) == 0


# ---- ③ 事实卡位置 + 回忆评分 ----

@pytest.mark.parametrize("target", [16384, 32768])
def test_cards_land_in_three_position_bands(target):
    f = _build(target)
    by = {c["slot"]: c for c in f.cards}
    assert set(by) == {"front", "mid", "back"}
    assert 0.0 <= by["front"]["frac_est"] <= 0.2
    assert 0.4 <= by["mid"]["frac_est"] <= 0.6
    assert 0.8 <= by["back"]["frac_est"] <= 1.0
    blob = json.dumps(f.messages, ensure_ascii=False)
    for c in f.cards:
        assert blob.count(c["value"]) == 1
    # 卡在观察体里、是一行
    obs = [tr["response"] for m in f.messages if m["role"] == "tool" for tr in m["tool_responses"]]
    for c in f.cards:
        holder = [o for o in obs if c["value"] in o]
        assert len(holder) == 1
        body = json.loads(holder[0])
        text = body.get("content") or "\n".join(t["text"] for t in body.get("todos", []))
        assert any(c["value"] in ln for ln in text.splitlines())


def test_cards_in_todo_only_env():
    f = _build(4000, tools=["todo_add", "todo_list", "todo_complete"])
    assert len(f.cards) == 3
    assert all(t.call["name"] in ("todo_list", "todo_add") for t in f.turns)


def test_recall_scoring_hit_miss_boundary():
    cards = [{"slot": "front", "value": "/opt/halyard/conf/halyard-abc123.toml"},
             {"slot": "mid", "value": "OPS-12345"}, {"slot": "back", "value": "7340"}]
    full = F.score_recall("路径 /opt/halyard/conf/halyard-abc123.toml，工单 OPS-12345，阈值 7340。", cards)
    assert full == {"recall_ok": True, "recall_hits": 3,
                    "recall_by_slot": {"front": True, "mid": True, "back": True},
                    "recall_by_kind": {}}
    one = F.score_recall("工单 OPS-12345，阈值 7340，路径记不清", cards)
    assert one["recall_ok"] is False and one["recall_hits"] == 2
    assert one["recall_by_slot"]["front"] is False
    none = F.score_recall("", cards)
    assert none["recall_hits"] == 0 and none["recall_ok"] is False
    # 逐字：17340 / OPS-123456 / 7,340 不算命中
    edge = F.score_recall("阈值 17340，工单 OPS-123456，7,340", cards)
    assert edge["recall_hits"] == 0


# ---- ④ 聚合算术 ----

def _rec(level, item, rep, ok, **score):
    return {"level": level, "item_id": item, "rep": rep, "wall_s": 1.0,
            "score": {"pass": ok, "redundant_calls": 0, "env_errors": 0, **score},
            "gens": [{"prompt_tokens": 1000, "prompt_tps": 500.0}], "fill": {"fill_error": 10}}


def test_pass_hat_and_at_k_arithmetic():
    assert S.pass_hat_k(2, 2) == 1 and S.pass_hat_k(2, 1) == 0 and S.pass_hat_k(2, 0) == 0
    assert S.pass_at_k(2, 2) == 1 and S.pass_at_k(2, 1) == 1 and S.pass_at_k(2, 0) == 0
    assert S.pass_hat_k(3, 2) == pytest.approx(1 / 3)
    assert S.pass_at_k(3, 1) == pytest.approx(2 / 3)
    assert S.pass_hat_k(1, 1) is None
    recs = [_rec(0, "A", 0, True), _rec(0, "A", 1, True),
            _rec(0, "B", 0, True), _rec(0, "B", 1, False),
            _rec(0, "C", 0, False), _rec(0, "C", 1, False),
            _rec(0, "D", 0, True)]                     # 单 rep：不进 Pass^2/@2
    a = S.aggregate(recs)["0"]
    assert a["n"] == 7 and a["n_tasks2"] == 3
    assert a["success_rate"] == pytest.approx(4 / 7)
    assert a["pass_hat_2"] == pytest.approx(1 / 3)
    assert a["pass_at_2"] == pytest.approx(2 / 3)
    assert a["prefill_s_median"] == pytest.approx(2.0)


def test_aggregate_recall_slots_and_repetition():
    recs = [_rec(8000, "A", 0, True, recall_ok=True, recall_hits=3, repetition_hits=0,
                 recall_by_slot={"front": True, "mid": True, "back": True}),
            _rec(8000, "A", 1, False, recall_ok=False, recall_hits=1, repetition_hits=2,
                 recall_by_slot={"front": True, "mid": False, "back": False}),
            _rec(8000, "B", 0, False, recall_ok=False, recall_hits=0, aborted=True,
                 recall_by_slot={"front": False, "mid": False, "back": False})]
    a = S.aggregate(recs)["8000"]
    assert a["recall_rate"] == 0.5 and a["recall_n"] == 2
    assert (a["recall_front"], a["recall_mid"], a["recall_back"]) == (1.0, 0.5, 0.5)
    assert a["repetition_hits"] == 2 and a["aborted"] == 1


def test_aggregate_peak_prompt_median_skips_legacy_rows():
    """W1：峰值中位数按 run 级 peak_prompt_tokens 取；旧行（缺键）不进分母。"""
    recs = [_rec(0, "A", 0, True), _rec(0, "A", 1, True), _rec(0, "B", 0, True)]
    recs[0]["peak_prompt_tokens"], recs[1]["peak_prompt_tokens"] = 3000, 5000
    assert S.aggregate(recs)["0"]["peak_prompt_tokens_median"] == 4000
    assert S.aggregate([_rec(0, "A", 0, True)])["0"]["peak_prompt_tokens_median"] is None


# ---- ⑤ 复读护栏记录 + 填充注入位置 ----

class _SpyEngine(FakeEngine):
    def __init__(self, script):
        super().__init__(script)
        self.roles_at_call = []

    async def generate_once(self, session, **kwargs):
        self.roles_at_call.append([m.get("role") for m in session.history])
        return await super().generate_once(session, **kwargs)


def _vfs_item():
    return {"id": "lc-x-001", "dimension": "longchain", "lang": "zh",
            "source": {"origin": "self", "fit": "测试用题——护栏记录"},
            "task": {"user": "写个文件", "env": "vfs", "env_init": {"files": {"a.txt": "1"}}},
            "expect": {"type": "end_state",
                       "asserts": [{"path": ["files", "a.txt"], "exists": True}]}}


def test_repetition_guard_recorded_and_filler_injected_before_task():
    item = _vfs_item()
    counter = K.fake_counter()
    plan = K.plan_run(counter, item, 4000, 0, seed=7)
    guard = {"channel": "thinking", "reason": "ngram_dup"}
    inner = _SpyEngine([{"tool_calls": [call("list_files")],
                         "metrics": {"prompt_tokens": 4100, "prompt_tps": 800.0,
                                     "generation_tokens": 10, "generation_tps": 40.0}},
                        {"repetition_guard": guard,
                         "metrics": {"prompt_tokens": 4200, "reused_prefix": 4100,
                                     "generation_tokens": 900}}])
    proxy = K.KneeEngine(inner, plan.filler.messages)
    traj = asyncio.run(run_item(proxy, plan.item))
    roles = inner.roles_at_call[0]
    assert roles[0] == "system" and roles[-1] == "user"
    assert len(roles) == 2 + len(plan.filler.messages)
    assert proxy.gens[1]["repetition_guard"] == guard
    assert proxy.gens[0]["repetition_guard"] is None
    assert proxy.gens[1]["reused_prefix"] == 4100
    score = K.score_run(plan, traj, proxy.gens, item)
    assert score["repetition_hits"] == 1 and score["final_text_ok"] is False
    rec = K.build_record(plan, traj, proxy.gens, score, item, {"model": "m"}, 1.0)
    assert rec["repetition_events"] == [{"gen": 1, **guard}]
    assert rec["fill"]["prompt_tokens_actual"] == 4100
    assert rec["peak_prompt_tokens"] == 4200       # W1：峰值取各次生成 max，不是首生成
    assert rec["fill"]["fill_error"] == 100
    assert rec["recall_suffix"].strip().startswith("另外")


def test_peak_prompt_measured_kept_separate_from_sent_peak():
    """编排器量到的峰值（可能含护栏 fold 前的量值）不得混进实发峰值 peak_prompt_tokens：
    build_record 的峰值只看 gens 实测 prompt_tokens（30000），编排器量到的 58000 另记
    peak_prompt_measured（_one_run_orch 里用 K._peak_prompt_measured 校验后赋值）。"""
    item = _vfs_item()
    plan = K.plan_run(K.fake_counter(), item, 4000, 0, seed=7)
    gens = [{"i": 0, "prompt_tokens": 20000}, {"i": 1, "prompt_tokens": 30000}]
    rec = K.build_record(plan, {"turns": []}, gens, {"recall_hits": 0}, item, {}, 0.0)
    assert rec["peak_prompt_tokens"] == 30000
    assert K._peak_prompt_measured(58000) == 58000
    assert K._peak_prompt_measured(0) is None and K._peak_prompt_measured(None) is None
    assert K._peak_prompt_measured(True) is None       # bool 是 int 子类，须显式排除


def test_fill_prompt_tokens_measured_records_pre_fold_measurement():
    """fold run 里首次生成的 prompt_tokens 是折后值，真实量值（fold 前）另记
    fill.prompt_tokens_measured；两者不同就说明首生成前折过。"""
    item = _vfs_item()
    plan = K.plan_run(K.fake_counter(), item, 4000, 0, seed=7)
    gens = [{"i": 0, "prompt_tokens": 29297, "measure_tokens": 58249}]
    rec = K.build_record(plan, {"turns": []}, gens, {"recall_hits": 0}, item, {}, 0.0)
    assert rec["fill"]["prompt_tokens_measured"] == 58249
    assert rec["fill"]["prompt_tokens_actual"] == 29297


# ---- ⑥ 端到端干跑 + summarize ----

def test_dry_run_end_to_end_and_summarize(tmp_path, capsys):
    out = tmp_path / "knee"
    assert K.main(["--dry-run", "--levels", "0,4000", "--reps", "2", "--limit", "2",
                   "--out", str(out)]) == 0
    lines = (out / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
    recs = [json.loads(ln) for ln in lines]
    assert len(recs) == 2 * 2 * 2
    assert {r["level"] for r in recs} == {0, 4000}
    for r in recs:
        assert r["backend"] == "fake" and r["fill"]["prompt_tokens_actual"]
        if r["level"]:
            assert len(r["fill"]["cards"]) == 3 and r["fill"]["fill_error"] is not None
            assert r["score"]["recall_hits"] == (3 if r["rep"] == 0 else 2)
        else:
            assert r["fill"]["cards"] == [] and r["score"]["recall_ok"] is None
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert set(summary["by_level"]) == {"0", "4000"} and summary["n_runs"] == 8
    capsys.readouterr()
    assert S.main([str(out), str(out)]) == 0
    table = capsys.readouterr().out
    assert "| Pass^2 |" in table and "| fake | 4000 | 4 |" in table
    assert table.count("| fake | 0 |") == 2              # 两个目录并排


# ---- 一审修订：命名空间隔离 / 卡值唯一 / 轮换 / 归一化 / 批次纪律 / 组装口径 ----

def _all_items():
    from benchmarks.model_eval.itemlib import load_items
    return load_items([K.ITEMS_DIR / "seed.jsonl", K.ITEMS_DIR / "hard.jsonl"])


def test_filler_namespace_disjoint_from_every_longchain_env():
    """伪历史不得引用任何题面 env_init 的文件路径或目录名（Major-2 方案二）。"""
    from benchmarks.model_eval.itemlib import task_fingerprint
    items = _all_items()
    paths, dirs = set(), set()
    for it in items:
        for path in ((it["task"].get("env_init") or {}).get("files") or {}):
            paths.add(path)
            parts = path.split("/")
            dirs.update(parts[:-1])
    assert paths and dirs          # 题库里确实有路径可比
    for it in items:
        tools = [t["name"] for t in K.item_tools(it)]
        f = _build(8000, tools=tools, task_key=task_fingerprint(it))
        blob = json.dumps(f.messages, ensure_ascii=False)
        for path in paths:
            assert path not in blob, (it["id"], path)
        for d in dirs:
            assert f"{d}/" not in blob and f"/{d}/" not in blob, (it["id"], d)
        for m in f.messages:
            if m["role"] == "user":
                assert "legacy-2025q3" in m["content"] and (
                    "已归档" in m["content"] or "archived" in m["content"])
        for t in f.turns:
            if t.path:
                assert t.path.startswith(F.LEGACY_ROOT + "/")
            for fp in t.extra.get("files") or []:
                assert fp.startswith(F.LEGACY_ROOT + "/")


@pytest.mark.parametrize("seed", range(7, 31))
def test_card_values_unique_in_filler(seed):
    for rep in (0, 1):
        f = _build(16384, seed=seed, rep=rep)
        blob = json.dumps(f.messages, ensure_ascii=False)
        for c in f.cards:
            assert len(F.value_pattern(c["value"]).findall(blob)) == 1, (seed, rep, c)


def test_card_value_collision_triggers_redraw(monkeypatch):
    import re
    bare = _build(16384, with_cards=False)
    blob = json.dumps(bare.messages, ensure_ascii=False)
    clash = re.search(r"(?<![0-9A-Za-z])([1-9][0-9]{3})(?![0-9A-Za-z])(?!\.[0-9])", blob).group(1)
    real = F.card_values

    def rigged(seed, rep, attempt=0):
        v = real(seed, rep, attempt)
        if attempt == 0:
            v["threshold"] = clash
        return v
    monkeypatch.setattr(F, "card_values", rigged)
    f = _build(16384)
    th = next(c for c in f.cards if c["kind"] == "threshold")
    assert th["value"] != clash and th["redraws"] >= 1
    assert len(F.value_pattern(th["value"]).findall(json.dumps(f.messages, ensure_ascii=False))) == 1


def test_card_kind_rotation_across_slots_and_runs():
    seen = []
    for rot in range(3):
        f = _build(16384, rotation=rot)
        kinds = [c["kind"] for c in f.cards]
        assert sorted(kinds) == sorted(F.CARD_KINDS)
        assert kinds == [F.CARD_KINDS[(i + rot) % 3] for i in range(3)]
        seen.append(kinds[0])
    assert sorted(seen) == sorted(F.CARD_KINDS)        # 每种卡都轮到过 front
    item = _vfs_item()
    counter = K.fake_counter()
    k = [K.plan_run(counter, item, 4096, rep, 7, item_idx=1).filler.cards[0]["kind"]
         for rep in range(3)]
    assert k == [F.CARD_KINDS[(1 + r) % 3] for r in range(3)]


def test_recall_normalization_and_decimal_boundary():
    cards = [{"slot": "front", "kind": "ticket", "value": "OPS-12345"},
             {"slot": "mid", "kind": "threshold", "value": "7340"},
             {"slot": "back", "kind": "config_path", "value": "/opt/halyard/conf/halyard-abc123.toml"}]
    r = F.score_recall("工单 OPS‐12345，阈值 ７３４０，路径 /opt/halyard/conf/halyard-abc123.toml", cards)
    assert r["recall_ok"] and r["recall_by_kind"]["threshold"]
    r2 = F.score_recall("OPS—12345 阈值 7340.5", cards)
    assert r2["recall_by_slot"] == {"front": True, "mid": False, "back": False}


def test_level_below_min_fill_warns_and_is_recorded():
    item = _vfs_item()
    plan = K.plan_run(K.fake_counter(), item, 300, 0, 7)
    assert plan.warning and "最小填充" in plan.warning
    assert len(plan.filler.turns) >= 3
    rec = K.build_record(plan, {"turns": []}, [], {"recall_hits": 0}, item, {}, 0.0)
    assert rec["fill"]["warning"] == plan.warning
    assert rec["peak_prompt_tokens"] is None       # W1：无生成 = null
    rec = K.build_record(plan, {"turns": []}, [{"i": 0, "prompt_tokens": None}],
                         {"recall_hits": 0}, item, {}, 0.0)
    assert rec["peak_prompt_tokens"] is None       # 全缺 prompt_tokens 同样 null
    ok = K.plan_run(K.fake_counter(), item, 8192, 0, 7)
    assert ok.warning is None
    assert K.parse_levels(K.DEFAULT_LEVELS) == [0, 4096, 8192, 16384, 32768, 49152, 57344]


def test_summarize_excludes_aborted_from_recall_and_errors_from_rates():
    slot = {"front": True, "mid": True, "back": True}
    miss = {"front": False, "mid": False, "back": False}
    recs = [_rec(4096, "A", 0, True, recall_ok=True, recall_hits=3, recall_by_slot=slot),
            _rec(4096, "A", 1, False, recall_ok=False, recall_hits=0, aborted=True,
                 recall_by_slot=miss),
            {"level": 4096, "item_id": "B", "rep": 0, "error": "RuntimeError: boom"}]
    # 首生成 reused 报 900/1000 也不过滤（远端首生成的 num_cached_tokens 不可信）
    recs[1]["gens"] = [{"prompt_tokens": 1000, "prompt_tps": 100.0, "reused_prefix": 900},
                       {"prompt_tokens": 1100, "prompt_tps": 30000.0},
                       {"prompt_tokens": 1200, "prompt_tps": 50000.0}]
    a = S.aggregate(recs)["4096"]
    assert a["n"] == 2 and a["errors"] == 1
    assert a["recall_rate"] == 1.0 and a["recall_n"] == 1 and a["aborted"] == 1
    assert a["prefill_s_median"] == pytest.approx((2.0 + 10.0) / 2)
    assert a["first_prompt_tps_median"] == pytest.approx(300.0)
    assert a["later_prompt_tps_median"] == pytest.approx(40000.0)   # 只有第二条有后续生成
    assert a["reused_prefix_median"] == 900


def test_pass_hat_merges_reps_across_stamps_latest_wins():
    """resume 补跑的 rep 带新 stamp：Pass^2 按题合并；同 (题, rep) 取最新一条。"""
    recs = [dict(_rec(0, "A", 0, True), stamp="s1"), dict(_rec(0, "A", 1, True), stamp="s2")]
    a = S.aggregate(recs)["0"]
    assert a["n_tasks2"] == 1 and a["pass_hat_2"] == 1.0
    recs = [dict(_rec(0, "A", 0, True), stamp="s1"), dict(_rec(0, "A", 1, False), stamp="s1"),
            dict(_rec(0, "A", 1, True), stamp="s2")]           # r1 重跑后通过，顶替旧值
    a = S.aggregate(recs)["0"]
    assert a["n_tasks2"] == 1 and a["pass_hat_2"] == 1.0 and a["pass_at_2"] == 1.0


def test_run_error_is_recorded_and_batch_continues(tmp_path, monkeypatch):
    real = K.run_item
    calls = {"n": 0}

    async def flaky(engine, item, repeat_idx=0):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return await real(engine, item, repeat_idx=repeat_idx)
    monkeypatch.setattr(K, "run_item", flaky)
    out = tmp_path / "k"
    assert K.main(["--dry-run", "--levels", "4096", "--reps", "2", "--limit", "1",
                   "--out", str(out)]) == 0
    recs = [json.loads(ln) for ln in (out / "transcripts.jsonl").read_text().splitlines()]
    assert len(recs) == 2 and "boom" in recs[1]["error"] and recs[1]["rep"] == 1
    summ = json.loads((out / "summary.json").read_text())
    assert summ["n_errors"] == 1 and summ["by_level"]["4096"]["errors"] == 1
    # 已有 transcripts：缺省拒绝，--append 才追加
    with pytest.raises(SystemExit):
        K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)])
    assert K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1",
                   "--out", str(out), "--append"]) == 0
    assert len((out / "transcripts.jsonl").read_text().splitlines()) == 3


def test_knee_engine_releases_session_cache():
    from tests.fake_engine import FakeBackend

    class _B(FakeBackend):
        def __init__(self):
            self.released = []

        def release_cache(self, h):
            self.released.append(h)
    inner = FakeEngine([{"text": "done"}])
    inner.backend = _B()
    proxy = K.KneeEngine(inner, [])
    asyncio.run(run_item(proxy, _vfs_item()))
    handle = proxy.session.brain_memory_state
    proxy.release()
    assert inner.backend.released == [handle] and proxy.session.brain_memory_state is None


def test_prompt_counter_matches_engine_first_generation_assembly(tmp_path):
    """PromptCounter 的组装参数 == AgentEngine.generate_once 首次生成实际喂给后端的 prompt。
    真 AgentEngine + 离线假后端（截获 stream_generate 的 prompt），经 KneeEngine 注入填充。"""
    from types import SimpleNamespace
    from framework.core.engine import AgentEngine

    class _LenTok:
        special_tokens_map = {}

        def encode(self, s, add_special_tokens=False):
            return [1] * max(1, len(s) // 4)

    class _Be:
        processor = SimpleNamespace(tokenizer=_LenTok())

        def __init__(self):
            self.prompts = []

        def new_cache(self):
            return SimpleNamespace(state=SimpleNamespace(cache=None, token_ids=None))

        def clone_cache(self, src):
            return None

        def release_cache(self, h):
            pass

        def shutdown(self):
            pass

        def stream_generate(self, prompt, *, cache, **kw):
            self.prompts.append(prompt)
            yield SimpleNamespace(text="完成。", delta=None, prompt_tokens=1, generation_tokens=1,
                                  prompt_tps=1.0, generation_tps=1.0, peak_memory=0.0, extra={})

    for dialect in ("gemma4", "qwen35"):
        be = _Be()
        eng = AgentEngine(be, model_options={"dialect": dialect}, env={})
        counter = K.engine_counter(eng)
        item = _vfs_item()
        plan = K.plan_run(counter, item, 4096, 0, 7)
        proxy = K.KneeEngine(eng, plan.filler.messages)
        asyncio.run(run_item(proxy, plan.item))
        system = {"role": "system", "content": K.DEFAULT_SYSTEM_PROMPT}
        user = {"role": "user", "content": plan.item["task"]["user"]}
        expect = counter.render([system, *plan.filler.messages, user], plan.tools, True)
        assert be.prompts[0] == expect, dialect


def test_zero_cost_counter_fails_loudly_instead_of_hanging():
    with pytest.raises(ValueError, match="计数器不可用"):
        F.build_filler(4096, seed=7, rep=0, tool_names=VFS_TOOLS, turn_cost=lambda m: 0)


# ---- 二审追加：采样覆盖 / 连贯度 ----

def test_parse_sampling_applies_mapped_keys_and_records_ignored(capsys):
    got = K.parse_sampling("temperature=0.7,repetition_penalty=1.0,top_p=0.9,max_tokens=1024,typical_p=0.5")
    assert got == {"sent": {"temperature": 0.7, "repetition_penalty": 1.0, "top_p": 0.9,
                               "max_tokens": 1024},
                   "ignored": {"typical_p": "0.5"}}
    assert "远端未映射，忽略" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        K.parse_sampling("temperature")


def test_sampling_override_reaches_fake_engine_and_backend():
    from types import SimpleNamespace
    from framework.core.engine import AgentEngine
    # FakeEngine：generate_once 形参层（run_item 传的 temperature 被覆盖）
    inner = FakeEngine([{"text": "done"}])
    proxy = K.KneeEngine(inner, [], {"temperature": 0.9, "repetition_penalty": 1.0})
    asyncio.run(run_item(proxy, _vfs_item()))
    assert inner.calls[0]["kwargs"]["temperature"] == 0.9
    # 真 AgentEngine + 离线后端：sampling 字典层（条目无 sampling 时引擎回落旧常量
    # repetition_penalty 1.1——W-1/A 起由 _resolve_sampling 解析——这里被覆盖层盖过）

    class _Be:
        processor = _Proc()

        def __init__(self):
            self.sampling = []

        def new_cache(self):
            return SimpleNamespace(state=SimpleNamespace(cache=None, token_ids=None))

        def clone_cache(self, src):
            return None

        def release_cache(self, h):
            pass

        def stream_generate(self, prompt, *, cache, sampling=None, **kw):
            self.sampling.append(dict(sampling or {}))
            yield SimpleNamespace(text="ok", delta=None, prompt_tokens=1, generation_tokens=1,
                                  prompt_tps=1.0, generation_tps=1.0, peak_memory=0.0, extra={})
    be = _Be()
    over = {"temperature": 0.9, "repetition_penalty": 1.0, "min_p": 0.0}
    assert K.install_sampling_override(be, over) is True
    assert K.install_sampling_override(be, over) is False          # 幂等
    eng = AgentEngine(be, model_options={}, env={})
    asyncio.run(run_item(K.KneeEngine(eng, [], over), _vfs_item()))
    s = be.sampling[0]
    assert s["repetition_penalty"] == 1.0 and s["temperature"] == 0.9 and s["min_p"] == 0.0
    assert s["max_tokens"] == 2048                                   # 未覆盖的键照旧


def test_dry_run_records_sampling_overrides(tmp_path):
    out = tmp_path / "s"
    assert K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out),
                   "--sampling", "temperature=0.6,top_k=20,typical_p=0.5"]) == 0
    rec = json.loads((out / "transcripts.jsonl").read_text().splitlines()[0])
    want = {"sent": {"temperature": 0.6, "top_k": 20}, "ignored": {"typical_p": "0.5"}}
    assert rec["sampling_overrides"] == want
    assert json.loads((out / "summary.json").read_text())["sampling_overrides"] == want
    assert rec["final_text_quality"]["len_chars"] > 0


PROBE_GIBBERISH = ('By\n Note:\nHere — I.\n\n "Thisは I\nThis?\n I\'ll(1\n\n\n “Actually 19文字 omitted\n\n'
                   'I = scriptで 4脚本 scripts\nThis content? This树94\n Actually depends: no\n'
                   'Attribute:\n"\n the: I\'s所表示 "script of attributes —\n19その: Here—\n'
                   '“ released by it year later. My名称: 191919.')
ZH_OK = "读完了：summary.txt 共 5 行，总额 15330 元已写入。路径 /opt/halyard/conf/halyard-abc.toml，工单 OPS-12345。"


def test_final_text_quality_metrics():
    from context_knee.quality import final_text_quality, is_coherent, longest_run
    assert longest_run("x191919191919y") == 6 and longest_run("abc") == 1
    assert longest_run("\n\n\n\n") == 1                         # 纯空白片段不计
    # 探针乱码（zh 题）：假名漂入 → 不连贯；字符层重复指标抓不到它
    bad = final_text_quality(PROBE_GIBBERISH, "zh")
    assert bad["kana_hangul_count"] >= 3 and bad["foreign_script_ratio"] >= 0.01
    assert bad["dup_ngram_ratio"] < 0.3 and bad["coherent"] is False
    # 正常中英混排（zh 题）：mixed_token 高但只记录 → 连贯
    ok = final_text_quality(ZH_OK, "zh")
    assert ok["foreign_script_ratio"] == 0 and ok["mixed_token_ratio"] > 0.05 and ok["coherent"] is True
    # en 题：中文漂入算外文字；词内拉丁+CJK 混写进判据
    assert final_text_quality("The total is 15330.", "en")["coherent"] is True
    assert final_text_quality("The total 总额 is 15330.", "en")["coherent"] is False
    assert final_text_quality("Thisは fine and the rest is okay here", "en")["coherent"] is False
    # 复读仍由 16-gram 判据兜住
    loop = final_text_quality("PTOSPP.2" * 60, "en")
    assert loop["dup_ngram_ratio"] > 0.9 and loop["coherent"] is False
    assert final_text_quality("", "zh")["coherent"] is None
    assert is_coherent(bad, {"foreign_script_ratio": 0.5}) is True        # 阈值可改


def test_summarize_coherent_rate_and_threshold_override(tmp_path, capsys):
    def rec(text, item, rep, lang="zh"):
        r = dict(_rec(0, item, rep, True), lang=lang)
        r["turns"] = [{"text": text}]
        return r
    recs = [rec(ZH_OK, "A", 0), rec(PROBE_GIBBERISH, "A", 1), rec("", "B", 0)]
    a = S.aggregate(recs)["0"]
    assert a["coherent_rate"] == 0.5 and a["coherent_n"] == 2
    d = tmp_path / "q"
    d.mkdir()
    (d / "transcripts.jsonl").write_text("\n".join(json.dumps(r) for r in recs))
    assert S.main([str(d), "--coherent-foreign", "0.5"]) == 0
    assert "| 100% | 2 |" in capsys.readouterr().out
    # 记录里存的旧口径值不影响：summarize 按 turns + lang 现算
    recs[1]["final_text_quality"] = {"len_chars": 5, "coherent": True}
    assert S.aggregate(recs)["0"]["coherent_rate"] == 0.5


# ---- 三审追加：run 级超时 / resume ----

class _Sleep10:
    async def wait(self):
        await asyncio.sleep(10)


def test_run_timeout_records_error_and_batch_continues(tmp_path, monkeypatch, capsys):
    real = K.dry_script
    calls = {"n": 0}

    def script(plan):
        calls["n"] += 1
        steps = real(plan)
        if calls["n"] == 1:
            steps[0] = {**steps[0], "gate": _Sleep10()}       # FakeEngine 在此 await sleep(10)
        return steps
    monkeypatch.setattr(K, "dry_script", script)
    out = tmp_path / "t"
    assert K.main(["--dry-run", "--levels", "4096", "--reps", "2", "--limit", "1",
                   "--out", str(out), "--run-timeout", "0.2"]) == 0
    recs = [json.loads(ln) for ln in (out / "transcripts.jsonl").read_text().splitlines()]
    assert recs[0]["error"] == "timeout" and recs[0]["rep"] == 0 and recs[0]["item_id"]
    assert 0.2 <= recs[0]["elapsed_s"] < 5
    assert "error" not in recs[1] and recs[1]["rep"] == 1               # 下一个 run 照跑
    assert "⏱ L4096" in capsys.readouterr().out
    assert json.loads((out / "summary.json").read_text())["complete"] is False


def test_resume_skips_done_and_reruns_errors(tmp_path, capsys):
    out = tmp_path / "r"
    out.mkdir()
    items = K.select_items(type("A", (), {"tasks": "hard", "ids": "", "limit": 2})())
    a, b = items[0]["id"], items[1]["id"]
    tr = {"thinking_replay_mode": "last_k", "thinking_replay_k": 2}   # W3 口径同本次缺省
    pre = [{"level": 4096, "item_id": a, "rep": 0, "stamp": "old", "score": {"pass": True},
            "gens": [], "fill": {}, **tr},
           {"level": 4096, "item_id": a, "rep": 1, "stamp": "old", "score": {"pass": True},
            "gens": [], "fill": {}, **tr},
           {"level": 4096, "item_id": b, "rep": 0, "stamp": "old", "error": "timeout", **tr}]
    (out / "transcripts.jsonl").write_text("\n".join(json.dumps(r) for r in pre) + "\n")
    assert K.main(["--dry-run", "--levels", "4096", "--reps", "2", "--limit", "2",
                   "--out", str(out), "--resume"]) == 0
    assert "跳过 2 个已完成 run" in capsys.readouterr().out
    recs = [json.loads(ln) for ln in (out / "transcripts.jsonl").read_text().splitlines()]
    new = [(r["item_id"], r["rep"]) for r in recs[3:]]
    assert new == [(b, 0), (b, 1)]                       # 只补跑缺的（含 error 那条）
    summ = json.loads((out / "summary.json").read_text())
    assert summ["complete"] is True and summ["n_rows"] == 5 and summ["n_runs"] == 4   # 整个 transcripts、去重后
    assert summ["by_level"]["4096"]["n_tasks2"] == 2          # 跨 stamp 的 a 两 rep 合并


# ---- 提交前一审：排空 / resume 口径 / summary 去重 / 空白答复 ----

class _ExecEngine:
    """带真单线程推理执行器的假引擎：generate_once 把阻塞 sleep 投进执行器（模拟卡住的远端 SSE 读），
    协程被 wait_for 取消后线程仍占用——排空必须等它。"""
    def __init__(self, block_s):
        from concurrent.futures import ThreadPoolExecutor
        from tests.fake_engine import FakeBackend
        self._inference_executor = ThreadPoolExecutor(max_workers=1)
        self.backend = FakeBackend()
        self.block_s = block_s
        self.calls = 0

    async def generate_once(self, session, **kw):
        import time as _t
        self.calls += 1
        loop = asyncio.get_running_loop()
        if self.calls == 1:
            await loop.run_in_executor(self._inference_executor, _t.sleep, self.block_s)
        return {"cancelled": False, "thoughts": [], "text": "done", "tool_calls": [],
                "truncated": False, "metrics": {"prompt_tokens": 10, "prompt_tps": 1.0}}


def test_timeout_drains_inference_executor_then_continues(tmp_path):
    """排空等待上限 = min(60, run_timeout)：阻塞 0.45 s、超时 0.3 s → 排空约 0.15 s 后成功。"""
    import time as _t
    eng = _ExecEngine(block_s=0.45)
    item = _vfs_item()
    args = type("A", (), {"seed": 7, "backend": "remote", "run_timeout": 0.3})()
    counter = K.fake_counter()
    meta = {"model": "m", "sampling_overrides": {"sent": {}, "ignored": {}}}
    t0 = _t.monotonic()
    rec = asyncio.run(K._one_run(args, eng, counter, item, 0, 4096, 0, meta))
    took = _t.monotonic() - t0
    assert rec["error"] == "timeout" and rec["drain_timeout"] is False
    assert rec["level"] == 4096 and rec["item_id"] == item["id"] and rec["rep"] == 0
    assert 0.3 <= rec["elapsed_s"] < 0.45 and took >= 0.44       # 排空真的等到了阻塞线程退出
    assert rec["drain_s"] > 0
    nxt = asyncio.run(K._one_run(args, eng, counter, item, 0, 4096, 1, meta))
    assert "error" not in nxt and nxt["rep"] == 1                 # 下一 run 照跑
    # 排空等不到：drain_timeout=True 记入 error 行
    eng2 = _ExecEngine(block_s=1.5)
    rec2 = asyncio.run(K._one_run(args, eng2, counter, item, 0, 4096, 0, meta))
    assert rec2["error"] == "timeout" and rec2["drain_timeout"] is True
    eng2._inference_executor.shutdown(wait=True)
    assert asyncio.run(K.drain_inference(FakeEngine([]), 0.1)) is None   # fake 无执行器：跳过


def test_resume_rejects_mismatched_model_sampling_or_seed(tmp_path):
    out = tmp_path / "m"
    assert K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out),
                   "--sampling", "temperature=0.5"]) == 0
    for extra in (["--sampling", "temperature=0.6"], ["--seed", "8"],
                  ["--model", "gemma-4-31b-it-4bit", "--sampling", "temperature=0.5"]):
        with pytest.raises(SystemExit, match="口径不一致"):
            K.main(["--dry-run", "--levels", "0,4096", "--reps", "1", "--limit", "1", "--out", str(out),
                    "--resume", *extra])
    assert K.main(["--dry-run", "--levels", "0,4096", "--reps", "1", "--limit", "1", "--out", str(out),
                   "--resume", "--sampling", "temperature=0.5"]) == 0


def _rows(path, **upd):
    """把 transcripts 每行改字段（None = 删键），模拟旧目录。"""
    recs = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    for r in recs:
        for k, v in upd.items():
            if v is None:
                r.pop(k, None)
            else:
                r[k] = v
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def test_resume_compat_entry_sampling_green(tmp_path):
    """条目采样口径相同（含 W-1 前旧行无 entry_sampling ≡ {}）：resume / append 照常。"""
    out = tmp_path / "e"
    base = ["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)]
    assert K.main(base) == 0
    rec = json.loads((out / "transcripts.jsonl").read_text().splitlines()[0])
    assert rec["entry_sampling"] == {}                       # meta 入行
    assert json.loads((out / "summary.json").read_text())["entry_sampling"] == {}
    _rows(out / "transcripts.jsonl", entry_sampling=None)   # W-1 之前的旧目录
    assert K.main(base[:2] + ["0,4096"] + base[3:] + ["--resume"]) == 0
    assert K.main(base + ["--append"]) == 0
    t = out / "transcripts.jsonl"
    K.check_resume_compat(t, "dry-run/fake", {}, 7, {})      # 不抛


def test_resume_compat_entry_sampling_red(tmp_path):
    """条目采样口径变了：resume 与单纯 append 都拒，错误信息点明 W-1、另起目录。"""
    out = tmp_path / "e"
    base = ["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)]
    assert K.main(base) == 0
    t = out / "transcripts.jsonl"
    _rows(t, entry_sampling={"top_k": 20})
    for extra in (["--resume"], ["--append"]):
        with pytest.raises(SystemExit, match="注册表条目采样口径已变（W-1），不能与旧目录合并；另起目录"):
            K.main(base + extra)
    _rows(t, entry_sampling=None)                            # 旧目录无键 ≡ {}，现条目非空
    with pytest.raises(SystemExit, match="条目采样口径已变"):
        K.check_resume_compat(t, "dry-run/fake", {}, 7, {"top_k": 20})
    with pytest.raises(SystemExit, match="条目采样口径已变"):
        K.check_resume_compat(t, "other/model", {}, 8, {"top_k": 20}, entry_only=True)
    K.check_resume_compat(t, "other/model", {}, 8, {}, entry_only=True)   # append 只比条目


def test_summary_dedupes_latest_non_error():
    recs = [dict(_rec(0, "A", 0, False), stamp="s1"), dict(_rec(0, "A", 0, True), stamp="s2"),
            {"level": 0, "item_id": "A", "rep": 0, "stamp": "s3", "error": "timeout"},
            {"level": 0, "item_id": "B", "rep": 0, "stamp": "s1", "error": "timeout"}]
    u = S.dedupe(recs)
    assert len(u) == 2
    a_row = next(r for r in u if r["item_id"] == "A")
    assert "error" not in a_row and a_row["stamp"] == "s2"        # 晚到的 error 不顶替真实行
    agg = S.aggregate(recs)["0"]
    assert agg["n"] == 1 and agg["errors"] == 1 and agg["success_rate"] == 1.0


def test_blank_final_text_not_judged():
    from context_knee.quality import final_text_quality
    assert final_text_quality("  \n\t ", "zh")["coherent"] is None


# ---- W-1/C：harness 可中断（W0：推理线程卡在远端 SSE 读时客户端信号杀不掉） ----

_BLOCKING_DRIVER = r'''
import sys, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.append(sys.argv[1])
from context_knee import run_knee as K
import tests.fake_engine as fe


class _BlockedEngine:
    """真·非守护单线程推理执行器，generate_once 把线程永久阻塞（= iter_lines 卡死）。"""
    def __init__(self, _script):
        self._inference_executor = ThreadPoolExecutor(max_workers=1)
        self.backend = fe.FakeBackend()

    async def generate_once(self, session, **kw):
        import asyncio

        def _stuck():
            print("BLOCKED", flush=True)
            threading.Event().wait()
        await asyncio.get_running_loop().run_in_executor(self._inference_executor, _stuck)


fe.FakeEngine = _BlockedEngine
sys.exit(K.main(sys.argv[2:]))
'''


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_second_signal_during_teardown_exits_immediately(tmp_path, signame):
    """收尾期（首个信号后在排空 2 s 里）再来一次同信号：≤3 s 退出、退出码 128+signum。
    改动前 finally 开头把处理器回成默认：第二次 SIGTERM 直接被杀（-15），第二次 SIGINT 以
    KeyboardInterrupt 乱入收尾、再卡在非守护推理线程的 join 上直到看门狗（12 s）。"""
    import signal as _signal
    import subprocess
    import threading
    import time as _t
    sig = getattr(_signal, signame)
    driver = tmp_path / "driver.py"
    driver.write_text(_BLOCKING_DRIVER, encoding="utf-8")
    out = tmp_path / "o"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(driver), str(REPO / "benchmarks"), "--dry-run",
         "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)],
        cwd=str(REPO / "backend"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines, blocked = [], threading.Event()

    def _reader():
        for ln in proc.stdout:
            lines.append(ln)
            if ln.strip() == "BLOCKED":
                blocked.set()
    threading.Thread(target=_reader, daemon=True).start()
    try:
        assert blocked.wait(60), "".join(lines)[-2000:]
        proc.send_signal(sig)
        _t.sleep(0.5)                       # 此时在 finally 的 drain_inference（2 s）里
        assert proc.poll() is None, "".join(lines)[-2000:]
        t0 = _t.monotonic()
        proc.send_signal(sig)
        try:
            rc = proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail(f"第二次 {signame} 后 3 s 未退出：\n" + "".join(lines)[-2000:])
        assert _t.monotonic() - t0 < 3
        assert rc == 128 + sig, (rc, "".join(lines)[-2000:])
        text = "".join(lines)
        assert "收尾中收到信号" in text and "KeyboardInterrupt" not in text, text[-2000:]
    finally:
        if proc.poll() is None:
            proc.kill()          # 只杀本测试自己起的子进程（红测路径）
            proc.wait()


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_signal_interrupts_blocked_inference_and_exits(tmp_path, signame):
    """子进程跑 harness，推理线程阻塞时发信号：≤5 s 退出、退出码 128+signum（受控退出，
    不是被信号直接杀掉的 -signum）、summary.json 已写（中断前的 run 照常落盘）。"""
    import signal as _signal
    import subprocess
    import threading
    import time as _t
    sig = getattr(_signal, signame)
    driver = tmp_path / "driver.py"
    driver.write_text(_BLOCKING_DRIVER, encoding="utf-8")
    out = tmp_path / "o"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(driver), str(REPO / "benchmarks"), "--dry-run",
         "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)],
        cwd=str(REPO / "backend"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines, blocked = [], threading.Event()

    def _reader():
        for ln in proc.stdout:
            lines.append(ln)
            if ln.strip() == "BLOCKED":
                blocked.set()
    threading.Thread(target=_reader, daemon=True).start()
    try:
        assert blocked.wait(60), "".join(lines)[-2000:]
        t0 = _t.monotonic()
        proc.send_signal(sig)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{signame} 后 5 s 未退出：\n" + "".join(lines)[-2000:])
        took = _t.monotonic() - t0
        assert rc == 128 + sig, (rc, "".join(lines)[-2000:])
        assert took < 5
        assert (out / "summary.json").exists()
        assert any("收到信号" in ln for ln in lines)
    finally:
        if proc.poll() is None:
            proc.kill()          # 只杀本测试自己起的子进程（红测路径）
            proc.wait()


# ---- W-2 前缀档（--prefix mock|prod|catalog|routed） ----

from context_knee import prefix as P                                       # noqa: E402
from framework.core.orchestrator import (COMPLETE_STEP_SCHEMA, GET_TOOL_SCHEMA_SCHEMA,  # noqa: E402
                                         REMEMBER_SCHEMA, UPDATE_PLAN_SCHEMA)
from framework.memory.session import DEFAULT_SYSTEM_PROMPT as PROD_SYSTEM  # noqa: E402

FULL_METAS = (UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA)
CATALOG_METAS = FULL_METAS + (GET_TOOL_SCHEMA_SCHEMA,)


def _lc_item(env="vfs"):
    return next(it for it in _all_items() if it["task"].get("env") == env)


def _is_in(obj, seq):
    return any(obj is x for x in seq)


def test_prefix_mock_is_byte_identical_to_pre_w2():
    """mock 档 = W-2 前：system 为 executor 极简提示、tools 为题目 mock 工具、整段渲染逐字相同；
    引擎收到的 tools 未被改写、grammar_tools 不传（None），run_item 不传 system_prompt。"""
    from benchmarks.model_eval.executor import DEFAULT_SYSTEM_PROMPT as EXEC_SYSTEM
    counter = K.fake_counter()
    for item in (_lc_item("vfs"), _lc_item("todo"), _lc_item("office")):
        for level in (0, 4096):
            plan = K.plan_run(counter, item, level, 0, 7)
            explicit = K.plan_run(counter, item, level, 0, 7, prefix=K.item_prefix(item, "mock"))
            assert plan.tools == explicit.tools == K.item_tools(item)
            assert plan.system_prompt is None and plan.grammar_tools is None
            assert plan.prefix_mode == "mock" and plan.mock_tools == plan.tools
            assert plan.filler.messages == explicit.filler.messages
            system = {"role": "system", "content": EXEC_SYSTEM}
            user = {"role": "user", "content": plan.item["task"]["user"]}
            et = bool((item.get("gen") or {}).get("enable_thinking", True))
            assert plan.base_tokens == counter.count([system, user], K.item_tools(item), et)
            pre = counter.render([system, *plan.filler.messages, user], K.item_tools(item), True)
            got = counter.render([{"role": "system", "content": item["task"].get("system")
                                   or plan.system_prompt or K.DEFAULT_SYSTEM_PROMPT},
                                  *explicit.filler.messages, user], explicit.tools, True)
            assert got == pre
    item = _vfs_item()
    plan = K.plan_run(counter, item, 0, 0, 7)
    inner = _SpyEngine([{"text": "完成。"}])
    traj = asyncio.run(run_item(K.KneeEngine(inner, plan.filler.messages), plan.item))
    assert inner.calls[0]["tools"] == K.item_tools(item) and inner.calls[0]["grammar_tools"] is None
    assert traj["turns"][0]["text"] == "完成。"


def test_prefix_prod_full_schemas_equal_grammar_and_all_production_tools():
    item = _lc_item("vfs")
    ts = P.production_toolset()
    a = K.item_prefix(item, "prod")
    mock = K.item_tools(item)
    names = [t["name"] for t in a.tools]
    assert a.system_prompt == PROD_SYSTEM and a.mode == "prod"
    assert a.tools == a.grammar_tools and len(names) == len(set(names))
    assert len(ts.physical) >= 40 and not any(s["name"].startswith("rag_admin") for s in ts.physical)
    assert {s["name"] for s in ts.physical} | {m["name"] for m in FULL_METAS} <= set(names)
    assert GET_TOOL_SCHEMA_SCHEMA["name"] not in names       # full 档元工具 = 3 件（_meta_schemas）
    for m in FULL_METAS:
        assert _is_in(m, a.tools)                             # 与 orchestrator 同一份对象
    for t in a.tools:                                         # 全部完整 schema
        assert _is_in(t, ts.physical) or _is_in(t, FULL_METAS) or t in mock   # mock 每次新建，比值
    assert a.tools[-len(mock):] == mock                       # mock 工具在生产声明之后


def test_prefix_mock_tool_wins_name_collision():
    item = _lc_item("vfs")
    ts = P.production_toolset()
    prod_write = next(s for s in ts.physical if s["name"] == "write_file")
    mock_write = next(t for t in K.item_tools(item) if t["name"] == "write_file")
    assert prod_write != mock_write                           # 撞名是真的（形状不同）
    for mode in ("prod", "catalog", "routed"):
        a = K.item_prefix(item, mode)
        for side in (a.tools, a.grammar_tools):
            hits = [t for t in side if t["name"] == "write_file"]
            assert len(hits) == 1 and hits[0] == mock_write != prod_write, mode
        assert a.shadowed == ["write_file"]
        if "write_file" in ts.core_cfg and mode != "prod":
            assert "write_file" in a.core_skipped and "write_file" not in a.core
    todo = K.item_prefix(_lc_item("todo"), "prod")
    assert todo.shadowed == [] and _is_in(prod_write, todo.tools)   # 无撞名则生产件照留


def test_prefix_catalog_lines_meta_core_full_grammar_full():
    item = _lc_item("vfs")
    ts = P.production_toolset()
    a = K.item_prefix(item, "catalog")
    mock = K.item_tools(item)
    mock_names = {t["name"] for t in mock}
    meta_names = {m["name"] for m in CATALOG_METAS}
    assert a.system_prompt == PROD_SYSTEM and a.routed is None
    assert a.core and set(a.core) <= set(ts.core_cfg) - mock_names
    physical = [s for s in ts.physical if s["name"] not in mock_names]
    assert a.grammar_tools == physical + list(CATALOG_METAS) + mock   # 语法面 = 完整全集
    assert {t["name"] for t in a.tools} == {t["name"] for t in a.grammar_tools}
    lines = 0
    for t in a.tools:
        n = t["name"]
        if n in meta_names or n in a.core or n in mock_names:
            assert _is_in(t, a.grammar_tools) and "parameters" in t, n
        else:
            assert set(t) == {"name", "description"}, t
            assert len(t["description"]) <= 80
            lines += 1
    assert lines == len(physical) - len(a.core)
    for m in CATALOG_METAS:
        assert _is_in(m, a.tools)


def test_prefix_routed_full_subset_of_noncore_nonmeta_and_frozen_per_item():
    item = _lc_item("vfs")
    ts = P.production_toolset()
    a = K.item_prefix(item, "routed")
    cat = K.item_prefix(item, "catalog")
    mock_names = {t["name"] for t in K.item_tools(item)}
    meta_names = {m["name"] for m in CATALOG_METAS}
    assert ts.top_k > 0 and a.routed and len(a.routed) <= ts.top_k
    assert set(a.routed) <= ({s["name"] for s in ts.physical} - set(a.core) - meta_names
                             - mock_names)
    assert a.grammar_tools == cat.grammar_tools and a.core == cat.core
    by_name = {t["name"]: t for t in a.tools}
    for n in a.routed:
        assert _is_in(by_name[n], a.grammar_tools) and "parameters" in by_name[n]
    full = {t["name"] for t in a.tools if _is_in(t, a.grammar_tools)}
    assert full == meta_names | set(a.core) | set(a.routed) | mock_names
    # 路由查询 = 原题面（不含回忆句）：同题重算恒同；不同档 / rep 的计划共用这一份
    assert K.item_prefix(item, "routed").routed == a.routed
    counter = K.fake_counter()
    p0, p1 = (K.plan_run(counter, item, 0, r, 7, prefix=a) for r in (0, 1))
    assert p0.tools == p1.tools == a.tools and p0.grammar_tools == a.grammar_tools


@pytest.mark.parametrize("mode", ["prod", "catalog", "routed"])
def test_prefix_filler_stream_same_as_mock(mode):
    """填充按 mock 工具取名：非 mock 档同一 (seed, rep, 题) 的伪轮次是 mock 档的前缀（同源、
    只因前缀更大而更少）；system 用生产提示。"""
    item = _lc_item("vfs")
    counter = K.fake_counter()
    mock = K.plan_run(counter, item, 24576, 0, 7)
    other = K.plan_run(counter, item, 24576, 0, 7, prefix=K.item_prefix(item, mode))
    assert 3 <= len(other.filler.turns) < len(mock.filler.turns)
    for a, b in zip(other.filler.turns, mock.filler.turns):
        assert a.messages() == b.messages()
    assert other.base_tokens > mock.base_tokens + 3000
    user = {"role": "user", "content": other.item["task"]["user"]}
    assert other.base_tokens == counter.count(
        [{"role": "system", "content": PROD_SYSTEM}, user], other.tools, True)


@pytest.mark.parametrize("mode", ["prod", "catalog", "routed"])
def test_prefix_reaches_engine_through_one_run(mode, monkeypatch):
    """_one_run 把本档 prompt 面声明 / grammar_tools / 生产 system 交给引擎；填充与 mock 档同源；
    生产工具被调用时走既有未知工具回执（不执行）。"""
    import tests.fake_engine as fe
    seen = []

    class _Spy(fe.FakeEngine):
        async def generate_once(self, session, **kw):
            seen.append((session.history[0]["content"], kw.get("tools"), kw.get("grammar_tools")))
            return await super().generate_once(session, **kw)
    monkeypatch.setattr(fe, "FakeEngine", _Spy)
    item = _lc_item("vfs")
    a = K.item_prefix(item, mode)
    args = type("A", (), {"seed": 7, "backend": "fake", "run_timeout": 0})()
    meta = {"model": "m", "sampling_overrides": {"sent": {}, "ignored": {}}}
    rec = asyncio.run(K._one_run(args, None, K.fake_counter(), item, 0, 0, 0, meta, a))
    assert "error" not in rec, rec.get("traceback")
    assert seen and all(s == (PROD_SYSTEM, a.tools, a.grammar_tools) for s in seen)
    # 生产工具不执行：mock env 的未知工具回执
    from benchmarks.model_eval.executor import feed_for_calls
    from benchmarks.model_eval.mock_envs import make_env
    env = make_env(item["task"]["env"], item["task"].get("env_init"))
    fb = feed_for_calls([{"type": "function", "name": "run_shell_command",
                          "arguments": {"command": "ls"}}], {}, "", {}, env=env)
    assert "Unknown tool: run_shell_command" in json.dumps(fb, ensure_ascii=False)


def test_prefix_dry_run_records_meta(tmp_path, capsys):
    out = tmp_path / "r"
    assert K.main(["--dry-run", "--prefix", "routed", "--levels", "0", "--reps", "1",
                   "--limit", "2", "--out", str(out)]) == 0
    recs = [json.loads(ln) for ln in (out / "transcripts.jsonl").read_text().splitlines()]
    assert len(recs) == 2 and not any("error" in r for r in recs)
    ids = [r["item_id"] for r in recs]
    summ = json.loads((out / "summary.json").read_text())
    assert summ["prefix_mode"] == "routed"
    for k in K.PER_ITEM_META_KEYS:                                  # 全批字典只在 summary
        assert set(summ[k]) == set(ids), k
    for r in recs:
        it = next(x for x in _all_items() if x["id"] == r["item_id"])
        a = K.item_prefix(it, "routed")
        assert r["prefix_mode"] == "routed" and r["prefix_base_tokens"] is None   # 无本地分词器
        assert r["prefix_routed"] == a.routed and isinstance(r["prefix_routed"], list)
        assert r["prefix_shadowed"] == a.shadowed and r["prefix_core_skipped"] == a.core_skipped
        assert r["prefix_n_tools"] == summ["prefix_n_tools"][r["item_id"]]
        for k in K.PER_ITEM_META_KEYS:                              # 行里只有本题那一项
            assert r[k] == summ[k][r["item_id"]], k
        assert r["prefix_top_k"] == P.production_toolset().top_k
        assert r["fill"]["base_tokens"] > 3000                      # 生产 system + 目录
    assert capsys.readouterr().out.count("prefix_base_tokens 记 null") == 1
    mock = tmp_path / "m"
    assert K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1",
                   "--out", str(mock)]) == 0
    rec = json.loads((mock / "transcripts.jsonl").read_text().splitlines()[0])
    assert rec["prefix_mode"] == "mock" and rec["prefix_base_tokens"] is None
    assert "prefix_routed" not in rec and "prefix_shadowed" not in rec
    assert "prefix_base_tokens 记 null" not in capsys.readouterr().out   # mock + fake 不告警


def test_audit_tool_order_matches_production_registry(monkeypatch, tmp_path):
    """prod/catalog/routed 档的声明顺序 = 生产：audit 的工具名序列 == 真 MCPRegistry 装载循环
    （sorted 模块 × inspect.getmembers）的注册序。探活一律放行（只比顺序，不比可用性）；
    rag_admin 不比（audit 刻意排除）；skill_invoke 在 audit 末尾（app.py 经 load_tool 补注册，
    扫描时剧本表未装配必被跳过）。"""
    import importlib
    import pkgutil
    from unittest import mock
    import framework.mcp.tools as tools_pkg
    from framework.mcp.base import BaseTool
    from framework.mcp.registry import MCPRegistry
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    audit_names = [s["name"] for s in P.production_toolset().physical]
    with mock.patch("requests.get", side_effect=ConnectionError("hermetic")):
        mods = [importlib.import_module(f"framework.mcp.tools.{i.name}")
                for i in pkgutil.iter_modules(tools_pkg.__path__)]

        def _walk(c):
            for sub in c.__subclasses__():
                yield sub
                yield from _walk(sub)
        for cls in set(_walk(BaseTool)):
            if "is_available" in vars(cls):
                monkeypatch.setattr(cls, "is_available", lambda self: True)
        reg = MCPRegistry(tools_dir=Path(tools_pkg.__file__).parent)
    rag_admin = {getattr(o, "name", None) for m in mods if m.__name__.endswith(".rag_admin")
                 for o in vars(m).values() if isinstance(o, type) and issubclass(o, BaseTool)
                 and o.__module__ == m.__name__}
    reg_names = [s["name"] for s in reg.get_all_schemas() if s["name"] not in rag_admin]
    assert len(reg_names) >= 40 and len(reg_names) == len(set(reg_names))
    assert audit_names[-1] == "skill_invoke"
    assert audit_names[:-1] == [n for n in reg_names if n != "skill_invoke"]
    assert audit_names[0] == "click_ui_element"          # getmembers 类名序（非 vars 定义序）


def test_resume_compat_prefix_mode(tmp_path):
    """prefix_mode 不同：resume 与单纯 append 都拒；相同放行；旧行缺键按 mock。"""
    out = tmp_path / "p"
    base = ["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)]
    assert K.main(base + ["--prefix", "prod"]) == 0
    t = out / "transcripts.jsonl"
    for extra in (["--resume"], ["--append"]):
        for other in ("mock", "catalog"):
            with pytest.raises(SystemExit, match=r"前缀档 --prefix 不同（W-2）.*prod ≠ " + other):
                K.main(base + extra + ["--prefix", other])
    assert K.main(base + ["--append", "--prefix", "prod"]) == 0           # 同档绿
    K.check_resume_compat(t, "dry-run/fake", {}, 7, {}, prefix_mode="prod")
    with pytest.raises(SystemExit, match="前缀档"):
        K.check_resume_compat(t, "dry-run/fake", {}, 7, {}, prefix_mode="routed", entry_only=True)
    _rows(t, prefix_mode=None)                                            # W-2 之前的旧目录
    K.check_resume_compat(t, "dry-run/fake", {}, 7, {})                   # 缺键 = mock
    K.check_resume_compat(t, "dry-run/fake", {}, 7, {}, prefix_mode="mock", entry_only=True)
    with pytest.raises(SystemExit, match="mock ≠ prod"):
        K.check_resume_compat(t, "dry-run/fake", {}, 7, {}, prefix_mode="prod")


# ---------- W3 思考回放口径开关（--thinking-replay / --thinking-replay-k） ----------

def test_thinking_replay_override_syncs_engine_and_counter():
    """不给参数 = 条目策略（无覆盖、计数器取引擎条目策略、历史填充思考被投影掉）；
    给 all = 引擎属性换成新策略，计数器随之全部回放。"""
    from types import SimpleNamespace
    from framework.core.engine import AgentEngine
    from framework.core.thinking_replay import ThinkingReplayPolicy
    f = _build(8000)
    first_thought = f.messages[1]["thoughts"]
    history = [{"role": "system", "content": "sys"}, *f.messages, {"role": "user", "content": "task"}]
    opts = {"dialect": "gemma4", "thinking_replay": "tool_turns"}
    ap = K.build_parser()

    none = ap.parse_args([])
    assert K.resolve_replay_override(none, opts) is None
    eng = AgentEngine(SimpleNamespace(processor=_Proc()), model_options=opts, env={})
    c0 = K.engine_counter(eng)
    assert c0.replay == ThinkingReplayPolicy("tool_turns", 2)
    assert first_thought not in c0.render(history, None, True)

    pol = K.resolve_replay_override(ap.parse_args(["--thinking-replay", "all"]), opts)
    assert pol == ThinkingReplayPolicy("all", 2)
    eng.thinking_replay = pol
    c1 = K.engine_counter(eng)
    assert first_thought in c1.render(history, None, True)
    assert K.resolve_replay_override(ap.parse_args(["--thinking-replay-k", "3"]), opts) \
        == ThinkingReplayPolicy("tool_turns", 3)                          # 只给 K：模式取条目
    with pytest.raises(ValueError, match="thinking_replay_k"):
        K.resolve_replay_override(ap.parse_args(["--thinking-replay-k", "0"]), opts)


@pytest.mark.parametrize("extra,mode,source", [([], "last_k", "default"),
                                               (["--thinking-replay", "all"], "all", "cli")])
def test_thinking_replay_dry_run_records_meta(tmp_path, capsys, extra, mode, source):
    out = tmp_path / "r"
    assert K.main(["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1",
                   "--out", str(out), *extra]) == 0
    summ = json.loads((out / "summary.json").read_text())
    recs = [json.loads(ln) for ln in (out / "transcripts.jsonl").read_text().splitlines()]
    for d in (summ, *recs):
        assert (d["thinking_replay_mode"], d["thinking_replay_k"],
                d["thinking_replay_source"]) == (mode, 2, source)


def test_resume_compat_thinking_replay(tmp_path):
    """思考回放口径不同：resume 与单纯 append 都拒；旧行缺键按 (all, None)；k 只在 last_k 时比。"""
    out = tmp_path / "tr"
    base = ["--dry-run", "--levels", "0", "--reps", "1", "--limit", "1", "--out", str(out)]
    assert K.main(base) == 0                                              # 缺省 last_k/2
    t = out / "transcripts.jsonl"
    for extra in (["--resume"], ["--append"]):
        with pytest.raises(SystemExit, match=r"思考回放口径 --thinking-replay 不同（W3）.*last_k/2 ≠ all"):
            K.main(base + extra + ["--thinking-replay", "all"])
        with pytest.raises(SystemExit, match=r"last_k/2 ≠ last_k/3"):
            K.main(base + extra + ["--thinking-replay-k", "3"])
    assert K.main(base + ["--append"]) == 0                               # 同口径绿
    _rows(t, thinking_replay_mode=None, thinking_replay_k=None)           # W3 之前的旧目录
    with pytest.raises(SystemExit, match=r"all/None ≠ last_k/2"):
        K.main(base + ["--append"])                                       # 本次缺省 last_k → 拒
    assert K.main(base + ["--append", "--thinking-replay", "all"]) == 0  # 显式 all → 通过
    K.check_resume_compat(t, "dry-run/fake", {}, 7, {}, thinking_replay=("all", None))
    with pytest.raises(SystemExit, match=r"all/None ≠ last_k/2"):
        K.check_resume_compat(t, "dry-run/fake", {}, 7, {})               # 直调缺省 = last_k/2
    _rows(t, thinking_replay_mode="tool_turns", thinking_replay_k=2)
    K.check_resume_compat(t, "dry-run/fake", {}, 7, {}, thinking_replay=("tool_turns", 5))  # 非 last_k 不比 k


class _KneeLenTok:
    special_tokens_map = {}

    def encode(self, s, add_special_tokens=False):
        return [1] * max(1, len(s) // 4)


class _KneeBe:
    """真后端路替身：只捕获 prompt，不装载任何模型。"""

    def __init__(self):
        from types import SimpleNamespace
        self.processor = SimpleNamespace(tokenizer=_KneeLenTok())
        self.prompts = []

    def new_cache(self):
        from types import SimpleNamespace
        return SimpleNamespace(state=SimpleNamespace(cache=None, token_ids=None))

    def clone_cache(self, src):
        return None

    def release_cache(self, h):
        pass

    def shutdown(self):
        pass

    def stream_generate(self, prompt, *, cache, **kw):
        from types import SimpleNamespace
        self.prompts.append(prompt)
        yield SimpleNamespace(text="完成。", delta=None, prompt_tokens=1, generation_tokens=1,
                              prompt_tps=1.0, generation_tps=1.0, peak_memory=0.0, extra={})


def test_thinking_replay_override_on_real_engine_path(tmp_path, monkeypatch, capsys):
    """真后端路（make_engine 换成假后端引擎）：--thinking-replay all 覆盖引擎属性，
    engine_counter 取到同一策略并渲染出历史填充思考；meta 记 cli。"""
    import re
    from framework.core.engine import AgentEngine
    from framework.core.thinking_replay import ThinkingReplayPolicy
    made, counters = [], []

    async def fake_make_engine(args, repo, opts):
        eng = AgentEngine(_KneeBe(), model_options=opts, env={})
        made.append(eng)
        return eng

    orig_counter = K.engine_counter

    def spy_counter(engine):
        c = orig_counter(engine)
        counters.append(c)
        return c
    monkeypatch.setattr(K, "make_engine", fake_make_engine)
    monkeypatch.setattr(K, "engine_counter", spy_counter)
    out = tmp_path / "real"
    assert K.main(["--model", "gemma-4-31b-it-4bit", "--backend", "remote", "--levels", "4096",
                   "--reps", "1", "--limit", "1", "--out", str(out),
                   "--thinking-replay", "all"]) == 0
    (eng,), (counter,) = made, counters
    assert eng.thinking_replay == ThinkingReplayPolicy("all", 2) == counter.replay
    first = eng.backend.prompts[0]
    history_part = first[:first.rfind("<|turn>user")]
    assert re.search(r"<\|channel>thought\n[^<]", history_part)            # 历史填充思考在
    summ = json.loads((out / "summary.json").read_text())
    assert (summ["thinking_replay_mode"], summ["thinking_replay_source"]) == ("all", "cli")


# ---- 阶段复审 M1：量法记录 / 全量渲染次数 / 上下文上限 meta ----

def test_dry_run_records_measure_full_renders_and_context_limit_meta(tmp_path):
    out = tmp_path / "m"
    assert K.main(["--dry-run", "--levels", "0,4000", "--reps", "1", "--limit", "1",
                   "--out", str(out)]) == 0
    recs = [json.loads(ln) for ln in (out / "transcripts.jsonl").read_text().splitlines()]
    summ = json.loads((out / "summary.json").read_text())
    for d in (summ, *recs):
        assert (d["context_limit"], d["context_limit_source"]) == (None, "fake")
    for r in recs:
        assert r["full_renders"] == 0                 # FakeEngine 不走全量路计数；ChatSession 有该属性
        assert r["gens"]
        for g in r["gens"]:
            # 生成前量值与该次实测 prompt_tokens 都在（FakeEngine 的量值取下一步脚本的 prompt_tokens）
            assert g["measure_method"] == "tokenizer"
            assert g["measure_tokens"] == g["prompt_tokens"] and g["prompt_tokens"]


def test_real_engine_path_measures_same_prompt_and_counts_full_renders(tmp_path, monkeypatch):
    """真引擎路（make_engine 换成假后端引擎）：measure_prompt 走与 generate_once 同一装配，量值 =
    同一 prompt 的分词数；首生成全量渲染 → full_renders ≥ 1；meta 记引擎上限与 make_engine 的来源。"""
    from framework.core.engine import AgentEngine
    made = []

    async def fake_make_engine(args, repo, opts):
        be = _KneeBe()
        be.context_limit, be.context_limit_source = 32768, "health"   # remote 后端自记来源
        eng = AgentEngine(be, model_options=opts, env={})
        made.append(eng)
        return eng
    monkeypatch.setattr(K, "make_engine", fake_make_engine)
    out = tmp_path / "real"
    assert K.main(["--model", "gemma-4-31b-it-4bit", "--backend", "remote", "--levels", "4096",
                   "--reps", "1", "--limit", "1", "--out", str(out)]) == 0
    (eng,) = made
    (rec,) = [json.loads(ln) for ln in (out / "transcripts.jsonl").read_text().splitlines()]
    assert "error" not in rec, rec.get("traceback")
    assert (rec["context_limit"], rec["context_limit_source"]) == (32768, "health")
    g0 = rec["gens"][0]
    assert g0["measure_method"] == "tokenizer"
    assert g0["measure_tokens"] == len(_KneeLenTok().encode(eng.backend.prompts[0]))
    assert "prompt_tokens" in g0
    assert isinstance(rec["full_renders"], int) and rec["full_renders"] >= 1


def test_knee_engine_without_measure_or_failing_measure_records_null():
    class _NoMeasure(FakeEngine):
        measure_prompt = None

    class _Boom(FakeEngine):
        def measure_prompt(self, *a, **k):
            raise RuntimeError("tokenizer gone")
    item = _vfs_item()
    for inner, err in ((_NoMeasure([{"text": "完成"}]), None),
                       (_Boom([{"text": "完成"}]), "RuntimeError: tokenizer gone")):
        proxy = K.KneeEngine(inner, [])
        asyncio.run(run_item(proxy, item))
        (g,) = proxy.gens
        assert (g["measure_tokens"], g["measure_method"]) == (None, None)
        assert g.get("measure_error") == err
        assert g["prompt_tokens"] == 100                    # 实测照记，run 不中止


def test_resume_and_summarize_accept_legacy_rows_without_new_keys(tmp_path, capsys):
    """旧行（M1 之前）缺 measure_* / full_renders / context_limit*：--resume 续跑与 summarize 都不报错。"""
    out = tmp_path / "legacy"
    assert K.main(["--dry-run", "--levels", "4096", "--reps", "2", "--limit", "1",
                   "--out", str(out)]) == 0
    path = out / "transcripts.jsonl"
    recs = [json.loads(ln) for ln in path.read_text().splitlines()]
    legacy = recs[0]
    for k in ("full_renders", "context_limit", "context_limit_source"):
        legacy.pop(k)
    for g in legacy["gens"]:
        g.pop("measure_tokens"), g.pop("measure_method")
    path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")
    assert K.main(["--dry-run", "--levels", "4096", "--reps", "2", "--limit", "1",
                   "--out", str(out), "--resume"]) == 0
    assert "跳过 1 个已完成 run" in capsys.readouterr().out
    rows = [json.loads(ln) for ln in path.read_text().splitlines()]
    assert len(rows) == 2 and "full_renders" not in rows[0] and rows[1]["full_renders"] == 0
    summ = json.loads((out / "summary.json").read_text())
    assert summ["complete"] is True and summ["n_runs"] == 2
    assert S.main([str(out)]) == 0
