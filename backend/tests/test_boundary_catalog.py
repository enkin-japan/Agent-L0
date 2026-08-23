"""⑤′-2 边界目录金测试：谓词确定性 + 文档/代码一致 + 回流脚本信号。"""
import json
import re
from pathlib import Path

import pytest

from framework.cloud import boundary as B
from framework.cloud.boundary import RequestFacts, match

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "backend/benchmarks/model_eval/BOUNDARY.md"


def ids_of(facts, **kw):
    return [b.id for b in match(facts, **kw)]


def test_catalog_ids_unique_and_statuses_legal():
    assert len(set(B.ids())) == len(B.ids())
    for b in B.CATALOG:
        assert b.status in B.STATUSES and b.action in B.ACTIONS
        assert (b.action_target is None) == (b.action in ("cloud_consult", "caveat"))
        assert "colab" not in (b.evidence + b.rationale + b.probes + b.action_notice + b.action_hint).lower()
        if b.status == "cleared":
            assert b.predicate is B._never


def test_boundary_invariants_raise_not_assert():
    kw = dict(title="t", predicate=lambda f: True, evidence="e", probes="p")
    with pytest.raises(ValueError):
        B.Boundary(id="x", status="nope", action="cloud_consult", action_target=None, **kw)
    with pytest.raises(ValueError):
        B.Boundary(id="x", status="weak", action="switch_tier", action_target=None, **kw)
    with pytest.raises(ValueError):
        B.Boundary(id="x", status="weak", action="cloud_consult", action_target="m", **kw)
    with pytest.raises(ValueError):      # cleared 必须 _never
        B.Boundary(id="x", status="cleared", action="switch_tier", action_target="m", **kw)
    B.Boundary(id="x", status="cleared", action="switch_tier", action_target="m",
               title="t", predicate=B._never, evidence="e", probes="p")
    with pytest.raises(ValueError):      # caveat 必须带用户文案
        B.Boundary(id="x", status="weak", action="caveat", action_target=None, **kw)


def test_b1_action_is_caveat_not_tier_switch():
    """用户 08-23 拍板：64G 上两档不共驻，为一题换整个生产档不划算 → B1 只提醒核对。"""
    b = B.by_id("vision_chart_numeric")
    assert b.action == "caveat" and b.action_target is None
    assert "核对" in b.action_notice and "Qwen3.8-27B-4bit" in b.action_notice
    assert not [x.id for x in B.CATALOG if x.action == "switch_tier" and x.status != "cleared"], \
        "目录现无活跃换档条目（B2 cleared 的记录位除外）——若再加，先按 64G 算账"


def test_b3_action_is_tool_assist_with_run_math():
    """先列后算探针：列表 12/12 全对、算术仍 0/4 → 动作 = 算术交沙箱，不换档不出网。"""
    b = B.by_id("implicit_aggregation")
    assert b.action == "tool_assist" and b.action_target == "run_math"
    assert "run_math" in b.action_hint and "do NOT add or count" in b.action_hint and "dozens" in b.action_hint
    with pytest.raises(ValueError):          # tool_assist 必须带提示文案
        B.Boundary(id="x", title="t", predicate=lambda f: True, status="weak", action="tool_assist",
                   action_target="run_math", evidence="e", probes="p")


def test_doc_and_code_list_the_same_entries():
    """BOUNDARY.md 每条 `### B<n> · <id>` 与 CATALOG 一一对应；状态字样一致。"""
    text = DOC.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^### B\d+ · `([a-z_]+)`[^\n]*\n", text, flags=re.M))
    doc_ids = [m.group(1) for m in heads]
    assert doc_ids == B.ids(), (doc_ids, B.ids())
    assert f"`{B.PRODUCTION_TIER.split('/')[-1]}`" in text
    for m, b in zip(heads, B.CATALOG):
        end = text.find("\n### ", m.end())
        sec = text[m.end(): end if end != -1 else len(text)]          # 按标题切段，不按首次出现
        assert f"状态：**{b.status}**" in sec, f"{b.id} 文档状态与代码不一致"
        assert f"动作：`{b.action}`" in sec, f"{b.id} 文档动作与代码不一致"


def test_production_tier_is_the_designated_one():
    import yaml
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    reg = cfg["inference"]["model_registry"]
    entry = next(e for e in reg if e.get("repo") == B.PRODUCTION_TIER)
    assert "★ 定档生产单档" in str(entry.get("note", ""))
    assert sum(1 for e in reg if "★ 定档生产单档" in str(e.get("note", ""))) == 1


def test_vision_chart_predicate():
    assert ids_of(RequestFacts(text="这张柱状图里 2023 年的值是多少", has_images=True)) == ["vision_chart_numeric"]
    assert ids_of(RequestFacts(text="What is the peak value in the chart?", has_images=True)) == ["vision_chart_numeric"]
    assert ids_of(RequestFacts(text="这张图里 2023 年的数值是多少", has_images=True)) == ["vision_chart_numeric"]
    assert ids_of(RequestFacts(text="这张柱状图里 2023 年的值是多少", has_images=False)) == []
    assert ids_of(RequestFacts(text="帮我看看这张照片好看吗", has_images=True)) == []
    # 评审 M11：带图 + 一般英文问句不命中
    assert ids_of(RequestFacts(text="the online form screenshot, what should I fill in?", has_images=True)) == []
    assert ids_of(RequestFacts(text="what is the deadline written here?", has_images=True)) == []


@pytest.mark.parametrize("text", [
    "improve this function please", "Can you improve the error message?", "proofread this paragraph",
    "the derived class overrides run()", "这是一个 informal 的说明", "这是 approve 流程的说明",
    "what do you mean by that?", "give me the total picture of the repo layout", "平均一天要跑几次",
    "这顿饭的证明文件在哪", "今天吃什么", "把合同条款整理成表格", "把记录提取为 JSON 数组",
])
def test_no_false_positive_on_ordinary_asks(text):
    """评审 C1/C2 实测过的误报集合：无图纯文本，一条都不得命中（误报 = 花钱/慢 5–8×/审批打扰）。"""
    assert ids_of(RequestFacts(text=text), include_pending=True) == []


def test_long_context_structured_is_cleared_but_raw_predicate_kept_for_reprobe():
    f = RequestFacts(text="把文档里的全部记录提取为 JSON 数组", context_tokens=30_000)
    assert ids_of(f) == [] and ids_of(f, include_pending=True) == []     # cleared 恒不命中
    assert B.by_id("long_context_structured").status == "cleared"
    assert B._long_context_structured(f) is True                           # 重探时可直接复用
    assert B._long_context_structured(RequestFacts(text="把文档里的全部记录提取为 JSON 数组",
                                                    context_tokens=2_000)) is False
    assert B._long_context_structured(RequestFacts(text="讲讲这份材料的大意",
                                                    context_tokens=30_000)) is False
    assert B._long_context_structured(RequestFacts(text="整理成表格", attachments_chars=60_000)) is True


def test_pending_entries_never_trigger_by_default():
    """护栏不建在猜测上：pending 条目缺省排除（目录里现无 pending，用临时条目证明门在）。"""
    tmp = B.Boundary(id="tmp_pending", title="t", predicate=lambda f: True, status="pending",
                     action="cloud_consult", action_target=None, evidence="e", probes="p")
    import framework.cloud.boundary as mod
    orig = mod.CATALOG
    try:
        mod.CATALOG = orig + (tmp,)
        f = RequestFacts(text="今天吃什么")
        assert "tmp_pending" not in ids_of(f)
        assert "tmp_pending" in ids_of(f, include_pending=True)
    finally:
        mod.CATALOG = orig


def test_implicit_aggregation_predicate():
    for t in ("按状态分组统计条数与读数合计", "这些订单一共多少钱", "What is the total number of rows?",
              "统计一下这个词出现的数量", "sum up the readings per device", "求每组的平均值"):
        assert ids_of(RequestFacts(text=t)) == ["implicit_aggregation"], t
    assert ids_of(RequestFacts(text="把合同条款整理成表格")) == []          # 纯提取不算
    assert ids_of(RequestFacts(text="把记录提取为 JSON 数组")) == []


def test_keywords_do_not_read_attachments():
    """评审 M3：关键词只看指令；附件里满是"合计/total"也不命中。"""
    f = RequestFacts(text="帮我看看这份材料", attachments_chars=80_000)
    assert ids_of(f, include_pending=True) == []


def test_weak_entries_can_be_filtered_by_consumer():
    f = RequestFacts(text="请严格证明这个不等式")
    assert ids_of(f) == ["deep_reasoning"]
    assert ids_of(f, include_weak=False) == []


def test_deep_reasoning_predicate():
    for t in ("请严格证明这个不等式", "Prove that the sequence converges", "derive the closed form",
              "请推导出这个公式", "给出严格推理过程"):
        assert ids_of(RequestFacts(text=t)) == ["deep_reasoning"], t
    assert ids_of(RequestFacts(text="随便聊聊", explicit_deep=True)) == ["deep_reasoning"]
    assert ids_of(RequestFacts(text="今天吃什么")) == []


def test_match_preserves_catalog_order_and_can_multi_hit():
    f = RequestFacts(text="请严格证明图表里各组合计的峰值趋势", has_images=True)
    assert ids_of(f) == ["vision_chart_numeric", "implicit_aggregation", "deep_reasoning"]


# ---------- 回流脚本 ----------

def _turn(prompt, status="success", **kw):
    t = {"status": status, "prompt": prompt, "image": [], "audio": [], "ingested_text": "",
         "attachments": [], "response": [], "timestamp": 1_787_000_000.0,
         "prompt_tokens": 1000, "generations": 2, "parse_error_count": 0}
    t.update(kw)
    return t


def _write_session(root, sid, turns):
    d = root / sid
    d.mkdir(parents=True)
    (d / "history.jsonl").write_text("\n".join(json.dumps(t, ensure_ascii=False) for t in turns) + "\n",
                                     encoding="utf-8")


def test_harvest_signals_and_catalog_hits(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bh", REPO / "backend/scripts/boundary_harvest.py")
    bh = importlib.util.module_from_spec(spec); spec.loader.exec_module(bh)
    sys_err = {"role": "tool", "tool_responses": [{"name": "x", "response": "System Error: boom"}] * 3}
    _write_session(tmp_path, "s1", [
        _turn("把这份四十页合同里的每一条条款逐条整理成 JSON 数组，字段包括编号、标题、正文、生效日期、责任方", status="fail", prompt_tokens=40_000),
        _turn("把这份四十页合同里的每一条条款逐条整理成 JSON 数组，字段包括编号、标题、正文、生效日期、责任方，再试一次", generations=2),   # 前 40 字相同 → user_retry
        _turn("这些发票合计多少钱", status="fail"),
        _turn("这张图的峰值是多少", image=["a.png"], generations=15),
        _turn("聊聊天", response=[sys_err]),
        _turn("正常一轮"),
        _turn("分析", cloud_degraded=["budget_exhausted"]),
    ])
    # 注意 line 下标：第 2 行是聚合题
    _write_session(tmp_path, "s2", [_turn("普通成功", parse_error_count=3)])
    c = bh.harvest(tmp_path)
    by = {(x["session"], x["line"]): x for x in c}
    assert by[("s1", 0)]["signals"] == ["status_fail"]
    assert by[("s1", 0)]["catalog_hits"] == []                               # B2 已 cleared
    assert by[("s1", 0)]["verdict_hint"] == "目录盲区候选"
    assert by[("s1", 1)]["signals"] == ["user_retry"]
    assert by[("s1", 2)]["signals"] == ["status_fail"] and by[("s1", 2)]["catalog_hits"] == ["implicit_aggregation"]
    assert by[("s1", 2)]["verdict_hint"] == "目录证据候选"
    assert by[("s1", 3)]["signals"] == ["many_generations"] and by[("s1", 3)]["catalog_hits"] == ["vision_chart_numeric"]
    assert by[("s1", 4)]["signals"] == ["tool_errors"] and by[("s1", 4)]["verdict_hint"] == "目录盲区候选"
    assert ("s1", 5) not in by
    assert by[("s1", 6)]["signals"] == ["cloud_degraded"]
    assert by[("s2", 0)]["signals"] == ["parse_errors"]
    assert all(len(x["facts"]["prompt_head"]) <= 200 for x in c)


def test_harvest_prompt_head_cap_and_missing_dir_and_no_timestamp(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bh2", REPO / "backend/scripts/boundary_harvest.py")
    bh = importlib.util.module_from_spec(spec); spec.loader.exec_module(bh)
    assert bh.harvest(tmp_path / "nope") == []                                   # 目录不存在 = 0 候选
    long_prompt = "很长的提示" * 100
    t = _turn(long_prompt, status="fail"); t.pop("timestamp")
    _write_session(tmp_path, "s", [t])
    c = bh.harvest(tmp_path, since_ts=1_700_000_000.0)                            # 无时间戳不被 --since 过滤
    assert len(c) == 1 and len(c[0]["facts"]["prompt_head"]) == 200 and c[0]["ts"] is None



def test_harvest_keywords_ignore_ingested_material(tmp_path):
    """评审 M3：摄入材料里满是"合计/total"，指令平淡 → 不命中任何条目（只算长度）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("bh3", REPO / "backend/scripts/boundary_harvest.py")
    bh = importlib.util.module_from_spec(spec); spec.loader.exec_module(bh)
    _write_session(tmp_path, "s", [_turn("帮我看看这份材料", status="fail",
                                          ingested_text="…各月合计 total of 12,345 … 平均值 …" * 50)])
    c = bh.harvest(tmp_path)
    assert len(c) == 1 and c[0]["catalog_hits"] == []
    assert c[0]["facts"]["attachments_chars"] > 0


def test_harvest_output_is_gitignored():
    gi = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "backend/benchmarks/model_eval/boundary_candidates.jsonl" in gi, "候选含用户 prompt 片段，不得入库"


def test_longctx_struct_items_are_deterministic_and_valid():
    """探针题由生成器确定性重建：重生成 == 入库件；itemlib 鉴定纪律通过。"""
    import importlib.util
    from benchmarks.model_eval.itemlib import discover_item_files, load_items
    items = [i for i in load_items(discover_item_files(REPO / "backend/benchmarks/model_eval/items", ["text"]))
             if i["id"].startswith("tx-lcs-")]
    assert [i["id"] for i in items] == [f"tx-lcs-{s}-{k}" for s in ("8k", "16k", "32k", "48k") for k in ("json", "table", "table-think", "table-list")]
    spec = importlib.util.spec_from_file_location(
        "gen", REPO / "backend/benchmarks/model_eval/items/text/gen_longctx_struct.py")
    gen = importlib.util.module_from_spec(spec); spec.loader.exec_module(gen)
    rebuilt = gen.build()
    for a, b in zip(items, rebuilt):
        a = {k: v for k, v in a.items() if not k.startswith("_src")}
        assert a == b, a["id"]                                    # 整条比对（含 gen/_meta，评审 m22）
        doc = a["task"]["user"]
        assert all(n in doc for n in a["expect"]["final"].get("must_all", []) if n.startswith("DX-")), "编号须在文中"
    # 表格题判分钉条数+合计同行（评审 M7）
    tb = next(i for i in items if i["id"] == "tx-lcs-8k-table")
    assert len(tb["expect"]["final"]["regex_all"]) == 3
    from benchmarks.model_eval.matchers import match_text
    ok, _ = match_text(tb["expect"]["final"], "| 正常 | 3 | 1798 |\n| 停机 | 5 | 7068 |\n| 告警 | 4 | 1702 |")
    assert ok
    bad, fails = match_text(tb["expect"]["final"], "| 正常 | 2 | 1798 |\n| 停机 | 5 | 7068 |\n| 告警 | 4 | 1702 |")
    assert not bad and fails                                      # 条数错、合计对 → 不过
