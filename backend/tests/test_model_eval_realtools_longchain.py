"""真实工具题批 + 长链硬批的金测试（benchmarks/model_eval，2026-08-24）。

守的是什么：
- `items/tool_call/real.jsonl` 的工具面来自生产注册表的冻结快照，题目里写错一个参数名、
  写错一个枚举值，跑分时表现为「模型答错」而不是「题目坏了」——静默污染画像。故本文件把
  题面与快照做静态对表（args 键须是真参数、eq 枚举须是真枚举成员），坏题装载即红。
- 快照本身会随生产工具演进而过期：漂移测试拿当前可装载注册表逐项比对交集工具的
  parameters/description，不一致就提醒重新 snapshot（注册表构不起来时跳过，不误伤离线环境）。
- `mock_envs` 新增的故障注入（faults / readonly）是**判分面**：故障不确定或只读拦不住，
  长链的 recovered / 改动半径两个指标就都是假的。逐条钉死语义，并留一条「不配这两个键时
  行为与从前逐字节一致」的回归断言。

全部纯函数/纯内存，不起服务、不装模型。
"""
import json
import os
import re
from pathlib import Path

import pytest

from benchmarks.model_eval import itemlib
from benchmarks.model_eval.mock_envs import MOCK_ENVS, OfficeEnv, VfsEnv, make_env

ROOT = Path(__file__).resolve().parents[1]
ITEMS_ROOT = ROOT / "benchmarks" / "model_eval" / "items"
REAL_BANK = ITEMS_ROOT / "tool_call" / "real.jsonl"
HARD_BANK = ITEMS_ROOT / "longchain" / "hard.jsonl"
SNAPSHOT = ITEMS_ROOT / "tool_call" / "real_tool_schemas.json"


def _snapshot():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))["tools"]


@pytest.fixture(scope="module")
def real_items():
    return itemlib.load_items([REAL_BANK])


@pytest.fixture(scope="module")
def hard_items():
    return itemlib.load_items([HARD_BANK])


# ---- real.jsonl：装载纪律与工具面 ----

def test_real_bank_loads_with_resolved_tools(real_items):
    """30 题全数过装载纪律，tools_ref 全部解析成快照里的真声明。"""
    snap = _snapshot()
    assert len(real_items) == 30
    ids = [i["id"] for i in real_items]
    assert len(set(ids)) == 30
    assert all(re.fullmatch(r"tc-real-0(0[1-9]|[12][0-9]|30)", i) for i in ids), ids
    for it in real_items:
        refs = it["task"]["tools_ref_resolved"]
        assert all(r in snap for r in refs), (it["id"], refs)
        tools = it["task"]["tools"]
        assert 8 <= len(tools) <= 12, (it["id"], len(tools))
        assert [t["name"] for t in tools] == refs
        assert len(set(refs)) == len(refs), it["id"]


def test_real_bank_language_coverage(real_items):
    """鉴定纪律第 4 条：批次内双语必须都有（本批另含用户日语面）。"""
    langs = {i["lang"] for i in real_items}
    assert {"zh", "en"} <= langs


def test_real_bank_expected_tool_is_declared(real_items):
    """期望调用的工具必须在本题声明的目录里——否则考的是幻觉而不是选择。"""
    for it in real_items:
        exp = it["expect"]
        assert exp["type"] in ("tool_call", "no_call"), it["id"]
        if exp["type"] == "tool_call":
            declared = {t["name"] for t in it["task"]["tools"]}
            assert exp["call"]["name"] in declared, (it["id"], exp["call"]["name"])


def test_real_bank_has_restraint_items(real_items):
    """无工具陷阱题（含「该问用户就别猜」两题）不得少于 4 题：克制率是路由硬指标。"""
    no_call = [i for i in real_items if i["expect"]["type"] == "no_call"]
    assert len(no_call) >= 4
    assert all(i["expect"].get("final") for i in no_call), "陷阱题须给终答断言"


def test_real_bank_args_match_snapshot_schema(real_items):
    """静态对表：每个断言到的参数名都得是该工具真有的参数，eq 到枚举的值得是真枚举成员。

    题面写 `chart_kind` 或 `chart_type: "column"` 这类错，跑分时长得像模型答错——
    这条断言把它在装载期就变成红。
    """
    snap = _snapshot()
    for it in real_items:
        if it["expect"]["type"] != "tool_call":
            continue
        spec = it["expect"]["call"]
        props = snap[spec["name"]]["parameters"]["properties"]
        for key, cond in (spec.get("args") or {}).items():
            assert key in props, f"{it['id']}: {spec['name']} 没有参数 {key!r}"
            enum = props[key].get("enum")
            if enum is not None and "eq" in cond:
                assert cond["eq"] in enum, f"{it['id']}: {key}={cond['eq']!r} 不在枚举 {enum}"


def test_real_bank_closed_loop_items_declare_responses(real_items):
    """闭环题：有终答断言就得有脚本化回喂，否则模型无从整合、断言必然空转。"""
    for it in real_items:
        if it["expect"]["type"] != "tool_call" or not it["expect"].get("final"):
            continue
        responses = it["task"].get("tool_responses") or {}
        assert it["expect"]["call"]["name"] in responses, it["id"]


# ---- 快照漂移 ----

@pytest.mark.skipif(not os.environ.get("MODEL_EVAL_LIVE_REGISTRY"),
                    reason="装载真注册表会对 RAG/SearXNG 探活（网络态依赖），金测试缺省不跑；"
                           "设 MODEL_EVAL_LIVE_REGISTRY=1 显式开启（重跑快照前/评测前手动核一次）")
def test_snapshot_matches_live_registry():
    """快照 vs 当前注册表：交集工具的 parameters/description 必须逐字相同，并把
    新增/消失的工具名打出来（不判红——那是"要不要扩题"的信号，不是漂移）。

    不一致 = 生产工具改了而题库还在考旧声明，须重跑 snapshot_real_tools.py。
    """
    try:
        from framework.mcp.registry import MCPRegistry
        reg = MCPRegistry(ROOT / "framework" / "mcp" / "tools")
        live_list = reg.get_all_schemas()
    except Exception as e:                      # noqa: BLE001 - 环境不具备就跳过，不误伤
        pytest.skip(f"注册表装载不可用: {e}")
    live = {}
    for s in live_list:
        name = s.get("name") or (s.get("function") or {}).get("name")
        live[name] = s
    snap = _snapshot()
    common = sorted(set(snap) & set(live))
    assert len(common) >= 30, f"交集过小（{len(common)}），快照与注册表已严重脱节"
    drift = [n for n in common
             if snap[n].get("parameters") != live[n].get("parameters")
             or snap[n].get("description") != live[n].get("description")]
    assert not drift, f"工具声明已漂移，请重跑 snapshot_real_tools.py: {drift}"
    added, removed = sorted(set(live) - set(snap)), sorted(set(snap) - set(live))
    if added or removed:
        print(f"[snapshot] 注册表新增工具 {added} / 快照里多出 {removed}（离线探活差异或真增删，人工判）")


# ---- hard.jsonl：长链硬批 ----

def test_hard_bank_loads_clean(hard_items):
    assert len(hard_items) == 10
    ids = [i["id"] for i in hard_items]
    assert len(set(ids)) == 10
    assert all(re.fullmatch(r"lc-hard-0(0[1-9]|10)", i) for i in ids), ids
    assert {"zh", "en"} <= {i["lang"] for i in hard_items}
    for it in hard_items:
        assert it["task"]["env"] in MOCK_ENVS, (it["id"], it["task"]["env"])
        exp = it["expect"]
        assert exp["type"] == "end_state"
        assert exp.get("asserts"), it["id"]
        assert isinstance(exp.get("max_steps"), int) and exp["max_steps"] > 0, it["id"]


def test_hard_bank_envs_construct_and_assert_paths_are_reachable(hard_items):
    """每题的 env_init 都得真能建出环境，且断言的顶层键在初始快照里就存在（打错 files/todos
    这类键会让断言永远红，症状却像模型没做对）。"""
    for it in hard_items:
        env = make_env(it["task"]["env"], it["task"].get("env_init"))
        top = set(env.snapshot())
        for a in it["expect"]["asserts"]:
            assert str(a["path"][0]) in top, (it["id"], a["path"])


def test_hard_bank_turn_budget_covers_step_budget(hard_items):
    """回合预算须罩得住步数预算：一轮一调用的模型不该因为回合耗尽被误判成失败。"""
    for it in hard_items:
        assert it["task"]["max_turns"] > it["expect"]["max_steps"], it["id"]


def test_hard_bank_fault_and_readonly_items_are_wired(hard_items):
    """故障注入题至少各一道，且注入的工具名是环境真有的工具（打错名字 = 故障永不触发）。"""
    faulted = [i for i in hard_items if (i["task"].get("env_init") or {}).get("faults")]
    readonly = [i for i in hard_items if (i["task"].get("env_init") or {}).get("readonly")]
    assert faulted and readonly
    for it in faulted:
        env = make_env(it["task"]["env"], it["task"].get("env_init"))
        names = {t["name"] for t in env.tools()}
        for tool in it["task"]["env_init"]["faults"]:
            assert tool in names, (it["id"], tool)
    for it in readonly:
        files = it["task"]["env_init"].get("files") or {}
        for path in it["task"]["env_init"]["readonly"]:
            assert path in files, (it["id"], path)


# ---- 故障注入语义 ----

def test_fault_fails_first_n_then_recovers_without_mutating():
    """前 N 次失败且**不改状态**，第 N+1 次起正常——瞬时故障的定义就在这两句里。"""
    env = VfsEnv({"files": {"a.txt": "old"},
                  "faults": {"write_file": {"fail_first_n": 2, "error": "EBUSY: retry"}}})
    first = env.handle("write_file", {"path": "a.txt", "content": "new"})
    assert first == {"error": "EBUSY: retry"}
    assert env.snapshot() == {"files": {"a.txt": "old"}}          # 状态零变化
    assert "error" in env.handle("write_file", {"path": "a.txt", "content": "new"})
    assert env.faults["write_file"]["fail_first_n"] == 0          # 配额确实在递减
    assert env.handle("write_file", {"path": "a.txt", "content": "new"})["status"] == "written"
    assert env.snapshot() == {"files": {"a.txt": "new"}}
    assert env.handle("read_file", {"path": "a.txt"})["content"] == "new"


def test_fault_is_per_tool_and_defaults_to_ebusy_text():
    """配额按工具名各记各的；不给 error 文案时用默认的 EBUSY 串。"""
    env = VfsEnv({"files": {"a.txt": "x"},
                  "faults": {"delete_file": {"fail_first_n": 1}}})
    assert env.handle("read_file", {"path": "a.txt"})["content"] == "x"   # 别的工具不受影响
    err = env.handle("delete_file", {"path": "a.txt"})["error"]
    assert "EBUSY" in err and "retry" in err
    assert env.snapshot() == {"files": {"a.txt": "x"}}
    assert env.handle("delete_file", {"path": "a.txt"})["status"] == "deleted"


def test_readonly_rejects_mutations_but_allows_reads():
    """只读路径：写/追加/删一律 EACCES 且状态不动，读照常；同环境的其他文件不受牵连。"""
    env = VfsEnv({"files": {"ro.txt": "frozen", "rw.txt": "free"},
                  "readonly": ["ro.txt"]})
    for name, args in (("write_file", {"path": "ro.txt", "content": "x"}),
                       ("append_file", {"path": "ro.txt", "content": "x"}),
                       ("delete_file", {"path": "ro.txt"})):
        r = env.handle(name, args)
        assert r["error"] == "EACCES: read-only file: ro.txt", (name, r)
    assert env.handle("read_file", {"path": "ro.txt"})["content"] == "frozen"
    assert env.handle("list_files", {})["files"] == ["ro.txt", "rw.txt"]
    assert env.handle("write_file", {"path": "rw.txt", "content": "x"})["status"] == "written"
    assert env.snapshot() == {"files": {"ro.txt": "frozen", "rw.txt": "x"}}


def test_vfs_without_new_keys_behaves_exactly_as_before():
    """回归线：不配 faults/readonly 时新旧行为逐字节一致（老题库跑分可比性不能被这次改动打断）。"""
    plain = VfsEnv({"files": {"a.txt": "hi"}})
    assert plain.handle("write_file", {"path": "b.txt", "content": "x"}) == {
        "status": "written", "path": "b.txt"}
    assert plain.handle("append_file", {"path": "nope.txt", "content": "x"}) == {
        "error": "File not found: nope.txt (use write_file to create)"}
    assert plain.handle("read_file", {"path": "zz"}) == {"error": "File not found: zz"}
    assert plain.handle("write_file", {"path": "c.txt"}) == {
        "error": "Missing required parameter: content"}
    assert plain.handle("bogus", {}) == {"error": "Unknown tool: bogus"}
    assert plain.snapshot() == {"files": {"a.txt": "hi", "b.txt": "x"}}
    assert VfsEnv().snapshot() == {"files": {}}          # init=None 仍然可用


# ---- office 合体环境 ----

def test_office_env_routes_both_tool_families_and_snapshots_both():
    env = make_env("office", {"files": {"n.txt": "买菜\n交电费"}, "todos": [{"text": "旧事", "done": True}]})
    assert isinstance(env, OfficeEnv)
    names = [t["name"] for t in env.tools()]
    assert {"read_file", "write_file", "delete_file"} <= set(names)
    assert {"todo_add", "todo_list", "todo_complete"} <= set(names)
    assert len(names) == len(set(names)), "两族工具重名会让路由不确定"
    assert env.handle("read_file", {"path": "n.txt"})["content"] == "买菜\n交电费"
    assert env.handle("todo_add", {"text": "买菜"})["status"] == "added"
    assert env.handle("todo_complete", {"text": "买菜"})["status"] == "completed"
    env.handle("write_file", {"path": "remaining.txt", "content": "交电费"})
    snap = env.snapshot()
    assert set(snap) == {"files", "todos"}
    assert snap["files"]["remaining.txt"] == "交电费"
    assert {"text": "买菜", "done": True} in snap["todos"]
    assert {"text": "旧事", "done": True} in snap["todos"]
    assert env.handle("bogus_tool", {}) == {"error": "Unknown tool: bogus_tool"}


def test_office_env_honours_faults_and_readonly_on_the_file_side():
    env = make_env("office", {"files": {"ro.txt": "frozen"}, "readonly": ["ro.txt"],
                              "faults": {"write_file": {"fail_first_n": 1, "error": "EBUSY: retry"}}})
    assert env.handle("write_file", {"path": "new.txt", "content": "x"})["error"] == "EBUSY: retry"
    assert env.handle("write_file", {"path": "new.txt", "content": "x"})["status"] == "written"
    assert "EACCES" in env.handle("write_file", {"path": "ro.txt", "content": "x"})["error"]
    assert env.handle("todo_add", {"text": "不受文件故障影响"})["status"] == "added"
    assert env.snapshot()["files"]["ro.txt"] == "frozen"


def test_office_env_faults_also_fire_on_the_todo_side():
    env = make_env("office", {"faults": {"todo_add": {"fail_first_n": 1, "error": "EBUSY: todo"}}})
    assert env.handle("todo_add", {"text": "a"})["error"] == "EBUSY: todo"
    assert env.snapshot()["todos"] == []                   # 故障不改状态
    assert env.handle("todo_add", {"text": "a"})["status"] == "added"


def test_fault_quota_is_per_instance_so_repeats_replay_identically():
    """--repeat N 下每次 make_env 都从题面 env_init 重建；配额若被共享（浅拷贝）第二次就
    没故障了，终态断言的红绿不可复现。"""
    init = {"faults": {"write_file": {"fail_first_n": 1}}}
    for _ in range(3):
        env = make_env("vfs", init)
        assert "error" in env.handle("write_file", {"path": "a", "content": "x"})
        assert env.handle("write_file", {"path": "a", "content": "x"})["status"] == "written"
    assert init["faults"]["write_file"]["fail_first_n"] == 1   # 题面原件未被改写


def test_fault_is_not_consumed_by_calls_that_fail_validation():
    """本来就会失败的调用（缺参/只读/不存在）不消耗配额：故障落点与模型失误解耦。"""
    env = make_env("vfs", {"files": {"ro": "1"}, "readonly": ["ro"],
                           "faults": {"write_file": {"fail_first_n": 1}, "append_file": {"fail_first_n": 1}}})
    assert "Missing" in env.handle("write_file", {"path": "x"})["error"]            # 缺参
    assert "EACCES" in env.handle("write_file", {"path": "ro", "content": "2"})["error"]
    assert "not found" in env.handle("append_file", {"path": "nope", "content": "2"})["error"]
    assert env.faults["write_file"]["fail_first_n"] == 1 and env.faults["append_file"]["fail_first_n"] == 1
    assert "error" in env.handle("write_file", {"path": "x", "content": "1"})     # 现在才消耗
    assert env.handle("write_file", {"path": "x", "content": "1"})["status"] == "written"


def test_itemlib_rejects_malformed_history(tmp_path):
    bad = {"id": "h1", "dimension": "text", "lang": "zh",
           "source": {"origin": "self", "fit": "装载期拒坏 history，别等到装完模型才炸"},
           "task": {"user": "u", "history": [{"role": "system", "content": "x"}]},
           "expect": {"type": "text", "final": {"must_all": ["x"]}}}
    f = tmp_path / "h.jsonl"
    f.write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="history"):
        itemlib.load_items([f])


def test_task_fingerprint_tracks_task_face_not_expectations():
    """指纹动于题面（user/env/tools/回喂/max_turns/gen），不动于判分规格——重判合法性的机器判据。"""
    base = {"id": "f1", "dimension": "text", "lang": "zh",
            "source": {"origin": "self", "fit": "指纹语义钉扎：题面变=不可重判，判分变=重判的合法用途"},
            "task": {"user": "u", "max_turns": 3},
            "expect": {"type": "text", "final": {"must_all": ["x"]}}}
    fp = itemlib.task_fingerprint(json.loads(json.dumps(base)))
    same = json.loads(json.dumps(base)); same["expect"]["final"]["must_all"] = ["改判分"]
    assert itemlib.task_fingerprint(same) == fp          # 判分规格不进指纹
    for mutate in (lambda d: d["task"].__setitem__("user", "变了"),
                   lambda d: d["task"].__setitem__("system", "新系统提示"),
                   lambda d: d["task"].__setitem__("max_turns", 4),
                   lambda d: d["task"].__setitem__("tool_responses", {"t": "r"}),
                   lambda d: d["task"].__setitem__("history", [{"role": "user", "content": "h"}]),
                   lambda d: d["task"].__setitem__("env", "vfs"),
                   lambda d: d["task"].__setitem__("env_init", {"files": {"a": "1"}}),
                   lambda d: d["task"].__setitem__("images", ["x.png"]),
                   lambda d: d["task"].__setitem__("tools", [{"name": "t"}]),
                   lambda d: d["task"].__setitem__("tools_ref_resolved", ["run_math"]),
                   lambda d: d.__setitem__("dimension", "vision"),
                   lambda d: d.__setitem__("gen", {"max_tokens": 9})):
        m = json.loads(json.dumps(base)); mutate(m)
        assert itemlib.task_fingerprint(m) != fp, mutate
    assert fp.startswith("v2:")            # 版本前缀：算法升级时旧指纹降级为不可核验而非误报漂移


def test_task_fingerprint_hashes_image_bytes(tmp_path):
    (tmp_path / "img.png").write_bytes(b"AAA")
    base = {"id": "f2", "dimension": "vision", "lang": "zh", "_src_dir": str(tmp_path),
            "source": {"origin": "self", "fit": "图片按内容入纹：文件名不变字节变（换机重渲）必须被抓住"},
            "task": {"user": "u", "images": ["img.png"]},
            "expect": {"type": "text", "final": {"must_all": ["x"]}}}
    fp1 = itemlib.task_fingerprint(base)
    (tmp_path / "img.png").write_bytes(b"BBB")
    assert itemlib.task_fingerprint(base) != fp1


def test_run_eval_records_task_fp_in_transcript_records():
    """写侧契约钉扎：run_eval 的轨迹记录必须带 task_fp——删了它，重判护栏整体 fail-open。"""
    src = (Path(__file__).resolve().parents[1] / "benchmarks/model_eval/run_eval.py").read_text(encoding="utf-8")
    assert '"task_fp": task_fingerprint(item)' in src


def test_mock_envs_registry_lists_office():
    assert set(MOCK_ENVS) == {"vfs", "todo", "office"}
    assert MOCK_ENVS["office"] is OfficeEnv
