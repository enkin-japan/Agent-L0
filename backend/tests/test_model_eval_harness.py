"""评测框架金测试（benchmarks/model_eval，E1/E2）：装载纪律、匹配器、打分器、回喂形态。

守的是什么：评测产物直接喂多模型路由决策（HETERO_COMPUTE §10）——判分规则若
静默漂移，画像失真、路由跟着错，且症状不暴露。全部纯函数测试，零模型依赖。
"""
import json
from pathlib import Path

import pytest

from benchmarks.model_eval import itemlib
from benchmarks.model_eval.executor import feed_for_calls
from benchmarks.model_eval.matchers import match_args, match_text, match_value
from benchmarks.model_eval.scorers import get_scorer

ITEMS_ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "model_eval" / "items"


def _item(**over):
    base = {"id": "tc-x-001", "dimension": "tool_call", "lang": "zh",
            "source": {"origin": "self", "fit": "测试用题——校验装载路径本身"},
            "task": {"user": "问点什么", "tools": [{"name": "t1", "parameters": {}}]},
            "expect": {"type": "tool_call", "call": {"name": "t1"}}}
    base.update(over)
    return base


def _traj(*turns, aborted=""):
    return {"item_id": "tc-x-001", "repeat": 0, "wall_s": 1.0,
            "aborted_reason": aborted,
            "turns": [{"thoughts": "", "text": t.get("text", ""),
                       "tool_calls": t.get("tool_calls", []), "metrics": {},
                       "wall_s": 0.5} for t in turns]}


# ---- 匹配器 ----

def test_match_value_eq_is_type_sensitive():
    """eq 类型敏感：int 15 ≠ str "15"——方言参数类型还原正确与否正是测量点。"""
    assert match_value({"eq": 15}, 15)[0]
    assert not match_value({"eq": 15}, "15")[0]
    assert match_value({"eq": True, "type": "boolean"}, True)[0]


def test_match_value_conditions_conjunct():
    ok, _ = match_value({"type": "array", "contains_all": ["a", "b"]}, ["a", "b", "c"])
    assert ok
    assert not match_value({"type": "array", "contains_all": ["a", "z"]}, ["a", "b"])[0]
    assert match_value({"in": ["北京", "Beijing"]}, "北京")[0]
    assert match_value({"regex": "(?i)aapl|apple"}, "AAPL")[0]


def test_match_value_unknown_condition_raises():
    """拼错条件键必须炸——静默放行等于没判。"""
    with pytest.raises(ValueError, match="不认识的条件键"):
        match_value({"equals": 1}, 1)
    with pytest.raises(ValueError, match="不认识的类型名"):
        match_value({"type": "int"}, 1)


def test_match_args_required_optional_extra():
    spec = {"city": {"eq": "Tokyo"}, "days": {"optional": True, "type": "integer"}}
    assert match_args(spec, {"city": "Tokyo"})[0]
    assert match_args(spec, {"city": "Tokyo", "days": 3})[0]
    ok, fails = match_args(spec, {"days": 3})
    assert not ok and any("缺必需参数" in f for f in fails)
    ok, fails = match_args(spec, {"city": "Tokyo", "extra": 1}, allow_extra=False)
    assert not ok and any("多余参数" in f for f in fails)


def test_match_text_specs():
    assert match_text({"must_any": ["31", "sunny"]}, "It is sunny.")[0]
    assert not match_text({"must_any": ["31"]}, "cloudy")[0]
    assert not match_text({"must_not": ["<think>"]}, "leak <think> here")[0]
    with pytest.raises(ValueError):
        match_text({"must": ["x"]}, "x")


# ---- 装载纪律 ----

def _load_one(tmp_path, item):
    f = tmp_path / "bank.jsonl"
    f.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
    return itemlib.load_items([f])


def test_load_valid_item_and_comment_lines(tmp_path):
    f = tmp_path / "bank.jsonl"
    f.write_text("# 注释行\n\n" + json.dumps(_item(), ensure_ascii=False) + "\n",
                 encoding="utf-8")
    items = itemlib.load_items([f])
    assert len(items) == 1 and items[0]["_src_dir"] == str(tmp_path)


def test_vetting_discipline_rejects_missing_or_lazy_fit(tmp_path):
    """鉴定纪律是装载期硬校验：fit 缺失/敷衍装载即拒，劣题进不了执行器。"""
    with pytest.raises(ValueError, match="鉴定纪律"):
        _load_one(tmp_path, _item(source={"origin": "BFCL-v3#1", "fit": "ok"}))
    with pytest.raises(ValueError, match="鉴定纪律"):
        _load_one(tmp_path, _item(source={"origin": "self"}))


def test_load_rejects_dup_id_unknown_dim_missing_expect(tmp_path):
    f = tmp_path / "bank.jsonl"
    line = json.dumps(_item(), ensure_ascii=False)
    f.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="id 重复"):
        itemlib.load_items([f])
    with pytest.raises(ValueError, match="dimension"):
        _load_one(tmp_path, _item(dimension="toolcall"))
    with pytest.raises(ValueError, match="expect"):
        _load_one(tmp_path, _item(expect={}))


def test_seed_bank_loads_clean():
    """入库种子题本身必须全数过装载纪律（题库坏一行整个评测起不来，宁早炸）。"""
    files = itemlib.discover_item_files(ITEMS_ROOT, ["tool_call"])
    assert files, "种子题库丢失"
    items = itemlib.load_items(files)
    assert len(items) >= 12
    assert {i["dimension"] for i in items} == {"tool_call"}
    assert {"zh", "en"} <= {i["lang"] for i in items}   # 鉴定纪律第 4 条：双语覆盖


# ---- tool_call 打分器 ----

_CALL_OK = {"type": "function", "name": "t1", "arguments": {"q": "x"}}
_CALL_BAD = {"type": "function", "name": "t1", "arguments": {"q": "wrong"}}
_ERR = {"type": "error", "raw_input": "<function=t1 broken", "error_msg": "Malformed"}


def _tc_item(**expect_over):
    expect = {"type": "tool_call", "call": {"name": "t1", "args": {"q": {"eq": "x"}}},
              "final": {"must_any": ["done"]}}
    expect.update(expect_over)
    return _item(expect=expect)


def test_scorer_perfect_two_turn_pass():
    traj = _traj({"tool_calls": [_CALL_OK]}, {"text": "All done."})
    s = get_scorer("tool_call")(traj, _tc_item())
    assert s["pass"] and s["first_shot_ok"] and s["valid_call_ok"]
    assert not s["corrected"] and s["parse_errors"] == 0


def test_scorer_wrong_arg_fails_with_reason():
    traj = _traj({"tool_calls": [_CALL_BAD]}, {"text": "done"})
    s = get_scorer("tool_call")(traj, _tc_item())
    assert not s["pass"] and not s["valid_call_ok"]
    assert any("无完全合格调用" in w for w in s["why"])


def test_scorer_error_then_corrected():
    """纠错闭环有效性：首轮解析错误、次轮合格调用 → corrected=True 且 first_shot=False。"""
    traj = _traj({"tool_calls": [_ERR]}, {"tool_calls": [_CALL_OK]}, {"text": "done"})
    s = get_scorer("tool_call")(traj, _tc_item())
    assert s["pass"] and s["corrected"] and not s["first_shot_ok"]
    assert s["parse_errors"] == 1


def test_scorer_no_call_trap_both_ways():
    item = _item(expect={"type": "no_call", "final": {"must_any": ["299"]}})
    ok = get_scorer("tool_call")(_traj({"text": "大约 299792 公里"}), item)
    assert ok["pass"] and ok["restraint_ok"]
    bad = get_scorer("tool_call")(
        _traj({"tool_calls": [_CALL_OK]}, {"text": "大约 299792 公里"}), item)
    assert not bad["pass"] and not bad["restraint_ok"]


def test_scorer_hallucinated_tool_recorded_and_abort_fails():
    ghost = {"type": "function", "name": "ghost_tool", "arguments": {}}
    traj = _traj({"tool_calls": [ghost]}, {"text": "done"})
    s = get_scorer("tool_call")(traj, _tc_item())
    assert s["hallucinated_tools"] == ["ghost_tool"]
    aborted = _traj({"tool_calls": [_CALL_OK]}, {"text": "done"}, aborted="回合上限")
    assert not get_scorer("tool_call")(aborted, _tc_item())["pass"]


def test_unregistered_dimension_scorer_raises():
    with pytest.raises(KeyError, match="没有打分器"):
        get_scorer("speed")   # speed 是顺带聚合面，不是可点名维度


# ---- 执行器回喂形态 ----

def test_feed_error_call_uses_orchestrator_envelope():
    """error 调用回喂 = 编排层同款信封 + 方言教学段；同 raw 复读则升级止损文案。"""
    counts = {}
    feed = feed_for_calls([_ERR], {}, "HINT-SENTINEL", counts)
    assert feed[0]["name"] == "system"
    assert feed[0]["response"].startswith("System Error: your tool call could not be parsed.")
    assert feed[0]["response"].endswith("HINT-SENTINEL")
    feed2 = feed_for_calls([_ERR], {}, "HINT-SENTINEL", counts)
    assert "2 times" in feed2[0]["response"]


def test_feed_function_call_scripted_and_unknown():
    feed = feed_for_calls([_CALL_OK, {"type": "function", "name": "nope", "arguments": {}}],
                          {"t1": {"result": 4183}}, "H", {})
    assert feed[0] == {"name": "t1", "content": '{"result": 4183}'}
    assert feed[1]["name"] == "nope" and "not available" in feed[1]["content"]


# ---- cot_purity 打分器 ----

from benchmarks.model_eval.scorers import cot_purity  # noqa: E402


def test_cot_ctrl_leak_and_empty_think():
    traj = _traj({"text": "答案是 3 <think> 泄漏了 </think>"})
    traj["turns"][0]["thoughts"] = ""
    c = cot_purity.analyze(traj, {"enable_thinking": True})
    assert c["any_ctrl_leak"] and "<think>" in c["turns"][0]["ctrl_leaks"]
    assert c["empty_think_turns"] == 1
    assert cot_purity.analyze(traj, {"enable_thinking": False}) is None


def test_cot_dup_and_recite_rates():
    clean = "用户问东京天气，需要调 get_weather 工具查一下再答。"
    assert cot_purity.dup_rate(clean) == 0.0
    looped = "我需要再想一想这个问题。" * 20
    assert cot_purity.dup_rate(looped) > 0.5
    th = "查询结果是晴天 31 度，我应该告诉用户东京今天晴，气温三十一摄氏度体感舒适。"
    assert cot_purity.recite_rate("东京今天晴，气温三十一摄氏度体感舒适。", th) > 0.5
    assert cot_purity.recite_rate("Sunny, 31°C.", th) == 0.0


def test_cot_script_ratios_flags_stray_scripts():
    """西里尔/谚文混入 = 思考跑飞的典型症状；中英互混不计入 other。"""
    r = cot_purity.script_ratios("先想 think 一下 приве 문제")
    assert r["other"] > 0 and r["cjk"] > 0 and r["latin"] > 0
    pure = cot_purity.script_ratios("中文加 English 思考完全正常")
    assert pure["other"] == 0.0


def test_cot_aggregate_over_records():
    t1 = _traj({"text": "ok"})
    t1["turns"][0]["thoughts"] = "正常思考一句。"
    t2 = _traj({"text": "leak <tool_call> x"})
    t2["turns"][0]["thoughts"] = "咕 " * 100
    recs = [{"cot": cot_purity.analyze(t, {})} for t in (t1, t2)]
    agg = cot_purity.aggregate(recs)
    assert agg["n"] == 2 and agg["ctrl_leak_traj_rate"] == 0.5
    assert agg["dup_rate_max"] > 0.5
    assert cot_purity.aggregate([{"cot": None}]) == {}


# ---- mock 环境与 longchain 打分器 ----

from benchmarks.model_eval.mock_envs import TodoEnv, VfsEnv, make_env  # noqa: E402
from benchmarks.model_eval.scorers.longchain import check_assert  # noqa: E402


def test_vfs_env_flows_and_error_paths():
    env = VfsEnv({"files": {"a.txt": "hi"}})
    assert env.handle("read_file", {"path": "a.txt"})["content"] == "hi"
    assert "error" in env.handle("read_file", {"path": "nope.txt"})
    assert "error" in env.handle("append_file", {"path": "j.txt", "content": "x"})
    env.handle("write_file", {"path": "j.txt", "content": "x"})
    env.handle("append_file", {"path": "j.txt", "content": "y"})
    env.handle("delete_file", {"path": "a.txt"})
    assert env.snapshot() == {"files": {"j.txt": "xy"}}
    with pytest.raises(ValueError, match="未知 mock 环境"):
        make_env("shell")


def test_todo_env_complete_by_text_with_candidates_on_miss():
    env = TodoEnv({})
    env.handle("todo_add", {"text": "买菜"})
    miss = env.handle("todo_complete", {"text": "买 菜"})
    assert "error" in miss and "买菜" in miss["error"]   # 错误回执给候选（恢复线索）
    env.handle("todo_complete", {"text": "买菜"})
    assert env.snapshot()["todos"] == [{"text": "买菜", "done": True}]


def test_check_assert_vocabulary():
    state = {"files": {"r.md": "- 牛奶\n- 鸡蛋"}, "todos": [{"text": "买菜", "done": True}]}
    assert check_assert(state, {"path": ["files", "r.md"], "exists": True,
                                "contains_all": ["牛奶", "鸡蛋"]})[0]
    assert check_assert(state, {"path": ["files", "gone.txt"], "exists": False})[0]
    assert not check_assert(state, {"path": ["files", "r.md"], "exists": False})[0]
    assert check_assert(state, {"path": ["todos"],
                                "contains_obj": {"text": "买菜", "done": True}})[0]
    ok, why = check_assert(state, {"path": ["todos"],
                                   "contains_obj": {"text": "买菜", "done": False}})
    assert not ok and "无元素匹配" in why


def _lc_item(**over):
    base = {"id": "lc-x-001", "dimension": "longchain", "lang": "zh",
            "source": {"origin": "self", "fit": "测试用题——校验打分路径"},
            "task": {"user": "做事", "env": "vfs"},
            "expect": {"type": "end_state", "max_steps": 4,
                       "asserts": [{"path": ["files", "out.txt"], "exists": True,
                                    "contains": "done"}]}}
    base.update(over)
    return base


def _lc_traj(calls_per_turn, env_state, final_text="完成了", aborted=""):
    turns = []
    for calls in calls_per_turn:
        turns.append({"thoughts": "", "text": "", "tool_calls": calls, "metrics": {},
                      "wall_s": 0.1,
                      "feed": [{"name": c.get("name", ""), "content": "{\"status\": \"ok\"}"}
                               for c in calls]})
    turns.append({"thoughts": "", "text": final_text, "tool_calls": [], "metrics": {},
                  "wall_s": 0.1})
    return {"item_id": "lc-x-001", "repeat": 0, "wall_s": 1.0, "turns": turns,
            "aborted_reason": aborted, "env_state": env_state}


def _fc(name, **args):
    return {"type": "function", "name": name, "arguments": args}


def test_longchain_pass_and_state_fail():
    good = _lc_traj([[_fc("read_file", path="a")], [_fc("write_file", path="out.txt", content="done")]],
                    {"files": {"out.txt": "done"}})
    s = get_scorer("longchain")(good, _lc_item())
    assert s["pass"] and s["state_ok"] and s["steps"] == 2 and s["redundant_calls"] == 0
    bad = get_scorer("longchain")(
        _lc_traj([[_fc("write_file", path="wrong.txt", content="x")]], {"files": {"wrong.txt": "x"}}),
        _lc_item())
    assert not bad["pass"] and any("out.txt" in w for w in bad["why"])


def test_longchain_process_metrics_and_recovery():
    traj = _lc_traj([[_fc("read_file", path="a")], [_fc("read_file", path="a")],
                     [_fc("write_file", path="out.txt", content="done")]],
                    {"files": {"out.txt": "done"}})
    traj["turns"][0]["feed"] = [{"name": "read_file", "content": "{\"error\": \"File not found: a\"}"}]
    s = get_scorer("longchain")(traj, _lc_item())
    assert s["redundant_calls"] == 1 and s["env_errors"] == 1 and s["recovered"]
    over = _lc_traj([[_fc("read_file", path=str(i))] for i in range(5)],
                    {"files": {"out.txt": "done"}})
    s2 = get_scorer("longchain")(over, _lc_item())
    assert not s2["steps_ok"] and not s2["pass"]


def test_longchain_seed_bank_loads_clean():
    files = itemlib.discover_item_files(ITEMS_ROOT, ["longchain"])
    items = itemlib.load_items(files)
    assert len(items) >= 6
    assert all(i["task"].get("env") in ("vfs", "todo") for i in items)


# ---- text_answer 打分器（vision/text 共用）与视觉题库 ----

def test_text_answer_scorer():
    item = _item(dimension="vision",
                 expect={"type": "text", "final": {"must_all": ["42"],
                                                   "must_any": ["MANGO", "mango"]}})
    ok = get_scorer("vision")(_traj({"text": "The image shows MANGO 42."}), item)
    assert ok["pass"] and ok["stray_calls"] == 0
    bad = get_scorer("vision")(_traj({"text": "I see a fruit."}), item)
    assert not bad["pass"] and bad["why"]


def test_vision_seed_bank_loads_and_assets_exist():
    files = itemlib.discover_item_files(ITEMS_ROOT, ["vision"])
    items = itemlib.load_items(files)
    assert len(items) >= 5
    for it in items:
        for img in it["task"].get("images") or []:
            assert (Path(it["_src_dir"]) / img).exists(), f"{it['id']} 图片资产缺失: {img}"


def test_match_text_regex_any_word_boundary():
    """短数字答案词边界：\\b3\\b 不落进 13（子串误判是社区借题的判分暗坑）。"""
    assert match_text({"regex_any": [r"\b3\b", "(?i)three"]}, "There are 3 bars.")[0]
    assert not match_text({"regex_any": [r"\b3\b"]}, "There are 13 bars.")[0]
    assert match_text({"regex_any": ["(?i)gr[ae]y"]}, "The grey one.")[0]


def test_emit_only_item_not_marked_aborted():
    """纯发射题（无回喂表，单轮观测）末轮发出调用 = 预期终态，不得误标 aborted。
    2026-07-30 全量批实测：BFCL 批 13 题全被「回合上限耗尽」误杀，why 为空。"""
    import asyncio
    from benchmarks.model_eval.executor import run_item

    class _FakeBackend:
        pass

    class _FakeEngine:
        backend = None
        dialect = None

        async def generate_once(self, session, **kw):
            return {"thoughts": "", "text": "",
                    "tool_calls": [{"type": "function", "name": "t1",
                                    "arguments": {"q": "x"}}],
                    "metrics": {}}

    item = _item()          # 无 tool_responses / 无 env
    traj = asyncio.run(run_item(_FakeEngine(), item))
    assert traj["aborted_reason"] == ""
    s = get_scorer("tool_call")(traj, _tc_item(final=None))
    assert not s["aborted"]
