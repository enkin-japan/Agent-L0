"""D1+D2-闸半:模型可见文本金测试(OSS 扫描第 5 层,D.25 顺序 2,2026-08-28)。

守两件事——1800+ 项金测试全在测行为,此前没有一项测「模型实际看到的那串文本」,
而本仓的护栏措辞是逐条事故驱动调出来的(skill_invoke 例句 2/5→5/5、heredoc 写炸、
cloud_consult provider 幻觉):

1. **预算闸**(真分词器,D.26 重锚线 12300;原 D4 est 尺 12k 线已随 D.26 退役):
   确定性工具面的方言渲染声明总量不越线,
   余量打进失败文案。D28(2026-10-31 用户拍板):gemma4 与 qwen35 两家族**共用这一条线**,
   参数化两档,同口径——各按自家方言渲染、自家在册分词器计数,都只数逐件声明之和
   (gemma4=逐件 DSL,不算 <|tool> 包裹;qwen35=<tools> 内逐行 JSON,不含 "# Tools" 头、
   <tools> 包裹与调用格式教学)。
2. **快照**:声明文本 + DEFAULT_SYSTEM_PROMPT 逐字节钉死,「顺手改措辞」必须显式过审。

口径(与 `scripts/tool_token_audit.py` 的 full 口径同一实现,经 tests/_tool_fleet 取数):
  含 58 件确定性工具(update_plan/complete_step/remember 三件常驻元工具在内)、
  skill_invoke 为**生产形**(repo 剧本索引已追加,复审 C3——裸类形少算 ~130 token,
  恰好吞掉全部余量);不含 get_tool_schema(仅 catalog 进声明)、DEFAULT_SYSTEM_PROMPT
  (预算另计,快照钉字)、每工具 <|tool>/<tool|> 包裹(~2/件,与 est 尺同样排除)、
  动态 api_tools/用户目录剧本/活 RAG 类目(挂账盲区,D0 起如实注明)。
  构造是**密封**的(无云钥、无网络、repo 剧本):环境相关文本进不了快照(复审 C1/C2),
  也测不出假红。

复审已并入:不钉活 registry(探活随机器红)→确定性清单;不设单工具字节上限(与
test_tool_norms 准则句对着拉)→不做;UPDATE 分支重生成后**必红**(复审 S5:静默绿
=挂着环境变量永久关门);件数断言防依赖性缩水(复审 S6:枚举吞 import 错,缩水会把
锅误报成「文本变了」)。
"""
import os
from pathlib import Path

import pytest

from framework.memory.session import DEFAULT_SYSTEM_PROMPT

from framework.dialects.qwen35.prompt_builder import format_tool_declaration

from ._tool_fleet import (GEMMA4_TOKENIZER_REPO, GET_TOOL_SCHEMA_NAME, META_NAMES,
                          QWEN35_TOKENIZER_REPO, audit, collect_schemas, real_lltokenizer)

# D.26 重锚线(2026-08-28 用户;真 token 生产形口径);D28(2026-10-31 用户)起 gemma4 与
# qwen35 两家族同口径共用这一条,不另立 qwen35 线。再动须用户拍板。
# 2026-10-31 实测(W-1/A):gemma4 12036 / qwen35 12144(qwen35 整块含 "# Tools" 头与
# 调用格式教学为 12325,不计入本闸,供 W-2 前缀瘦身参考)
# 2026-11-11 send_email(第 57 件)落地:加前 gemma4 12085 / qwen35 12193,加后 12227 / 12338
# (+142 / +145;描述已压到仍可读的最短),线按最小需要上取到下一个 50 → 12350。
# 同日 publish_page(第 58 件)+ send_email 去掉 attachment 参数、描述加「页面先 publish_page 再发
# 链接」一句:12227 / 12338 → 12316 / 12429(+89 / +91),线上取到下一个 50 → 12450。
# 两次上调均经用户 2026-11-11 批准,仅限 feat/send-email 分支。
BUDGET = 12450
EXPECTED_DECLS = 58       # 确定性工具面件数(复审 S6:枚举静默吞 import 错,缩水要报真锅)
SNAP = Path(__file__).parent / "data" / "model_visible_text.snap"


def _schemas():
    """full 口径的确定性工具面 schema:剔除 get_tool_schema,按名排序,件数/撞名守卫。"""
    schemas = [s for s in collect_schemas() if s["name"] != GET_TOOL_SCHEMA_NAME]
    names = [s["name"] for s in schemas]
    assert len(names) == len(set(names)), (
        f"工具名撞车(复审 N15,元工具名被普通工具类顶替也算): {sorted(names)}")
    assert len(schemas) == EXPECTED_DECLS, (
        f"确定性工具面 {len(schemas)} 件 ≠ 预期 {EXPECTED_DECLS}——枚举缩水/膨胀了"
        f"(某工具模块 import 失败会被静默吞掉,先查 collect_schemas 的枚举路径,"
        f"不是文本变了)。新增/删除工具属刻意改动:改本常量并重生成快照。")
    return sorted(schemas, key=lambda s: s["name"])


def _declarations():
    """(name, gemma4 DSL 渲染后声明文本) 列表(快照与 gemma4 预算档的取数)。"""
    return [(s["name"], audit.format_function_declaration(s)) for s in _schemas()]


def _gemma4_texts():
    return [d for _, d in _declarations()]


def _qwen35_texts():
    return [format_tool_declaration(s) for s in _schemas()]


def _render_snapshot() -> str:
    parts = [f"===== {n} =====\n{d}\n" for n, d in _declarations()]
    parts.append(f"===== DEFAULT_SYSTEM_PROMPT =====\n{DEFAULT_SYSTEM_PROMPT}\n")
    return "\n".join(parts)


@pytest.mark.parametrize("family, render, repo", [
    pytest.param("gemma4", _gemma4_texts, GEMMA4_TOKENIZER_REPO, id="gemma4"),
    pytest.param("qwen35", _qwen35_texts, QWEN35_TOKENIZER_REPO, id="qwen35"),
])
def test_tool_declarations_under_token_budget(family, render, repo):
    """预算闸:真分词器计数(缺 tokenizer/llguidance=可见跳过,不静默绿)。
    D28:两家族同口径同一条 BUDGET,各按自家方言渲染 + 自家在册分词器计数。"""
    tok = real_lltokenizer(repo)
    total = sum(len(tok.tokenize_str(d)) for d in render())
    headroom = BUDGET - total
    print(f"[budget] {family} full 工具声明 {total}/{BUDGET},余量 {headroom}"
          f"(新工具落地前先跑 backend/scripts/tool_token_audit.py 看逐件账)")
    assert total <= BUDGET, (
        f"{family} full 工具声明 {total} tok 已越 D.26/D28 的 {BUDGET} 线(超 {-headroom})。"
        f"出路按 D.25 顺序:先减法(TOOL_NORMS_AUDIT.md §减法与预算,只删两类机械可判句)"
        f"→ 议重锚(用户拍板)→ catalog(最后)。逐件账:backend/scripts/tool_token_audit.py")


# catalog 口径门（W-2 / D30，2026-11-01）：声明 = 元工具四件（含 get_tool_schema）+
# agent.tool_routing.core 完整 schema + 其余确定性工具的目录行（名字 + summary），不含任务期
# 路由件（每任务 ≤ top_k 件，随任务变）；计数口径同上（逐件声明之和、自家方言、自家分词器）。
# 2026-11-01 实测 gemma4 3735 / qwen35 3715（core 七件 = config.yaml 当日值，含 run_shell_command；
# 首审修订后 summary 口径），线取较大者 +2% = 3810。
# 2026-11-06 重定（D44 (iii)，用户 11-05 拍板）：core 九件 = 七件 + run_math + rag_search，实测 gemma4 4494 /
# qwen35 4457；同日七件口径为 3784 / 3764（较 11-01 已 +49，期间文本改动），新增两件增量 run_math
# +369 / +364、rag_search +341 / +329（gemma4 / qwen35），合计 +710 / +693。线仍取较大者 +2%（上取整到 10）。
# 整段前缀（含 DEFAULT_SYSTEM_PROMPT 与包裹，九件口径）实测 gemma4 6025 / qwen35 6022（11-01 七件
# 5266 / 5280），见 backend/scripts/measure_prefix.py。
BUDGET_CATALOG = 4590


def _catalog_schemas():
    from framework.core.tool_disclosure import disclose, resolve_tool_routing, split_core
    from framework.utils.config_loader import load_section
    schemas = collect_schemas()
    _, core_cfg = resolve_tool_routing(load_section("agent") or {})
    core, unknown = split_core(core_cfg, {s["name"] for s in schemas})
    assert not unknown, f"agent.tool_routing.core 有名字不在确定性工具集: {unknown}"
    return disclose(schemas, mode="catalog", meta_names=META_NAMES, core=core,
                    summaries=audit.collect_summaries())


@pytest.mark.parametrize("family, fmt, repo", [
    pytest.param("gemma4", audit.format_function_declaration, GEMMA4_TOKENIZER_REPO, id="gemma4"),
    pytest.param("qwen35", format_tool_declaration, QWEN35_TOKENIZER_REPO, id="qwen35"),
])
def test_catalog_declarations_under_token_budget(family, fmt, repo):
    """catalog 口径门：元 + core 完整 + 其余目录行，两家族各自方言、自家分词器 ≤ BUDGET_CATALOG。"""
    decl = _catalog_schemas()
    assert len(decl) == EXPECTED_DECLS + 1          # 58 件 + get_tool_schema，件数不缩水
    tok = real_lltokenizer(repo)
    total = sum(len(tok.tokenize_str(fmt(s))) for s in decl)
    print(f"[budget] {family} catalog 工具声明 {total}/{BUDGET_CATALOG},余量 {BUDGET_CATALOG - total}")
    assert total <= BUDGET_CATALOG, (
        f"{family} catalog 工具声明 {total} tok 越 {BUDGET_CATALOG} 线（超 {total - BUDGET_CATALOG}）。"
        f"先查是不是 summary 写长了/core 加了件，再议重锚。逐档账：backend/scripts/measure_prefix.py")


def test_model_visible_text_snapshot():
    """快照:模型可见稳定文本逐字节钉死。刻意改动 → UPDATE_MODEL_VISIBLE_SNAPSHOT=1
    重生成(重生成本身**必红**,红文案即确认收据;快照文件绝不手改,见 data/README.md)。"""
    current = _render_snapshot()
    if os.environ.get("UPDATE_MODEL_VISIBLE_SNAPSHOT") == "1":
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        SNAP.write_text(current, encoding="utf-8")
        pytest.fail("快照已重生成(这不是事故,是确认收据):把 git diff 纳入评审,"
                    "然后**不带**环境变量重跑本测试收绿。挂着它跑=门被静默关掉。")
    assert SNAP.exists(), (
        f"快照不存在({SNAP})。首次生成:UPDATE_MODEL_VISIBLE_SNAPSHOT=1 重跑本测试。")
    expected = SNAP.read_text(encoding="utf-8")
    if current != expected:
        import difflib
        lines = list(difflib.unified_diff(
            expected.splitlines(keepends=True), current.splitlines(keepends=True),
            "snapshot", "current", n=1))
        shown = "".join(lines[:80])
        more = f"\n… (diff 截断,还有 {len(lines) - 80} 行)" if len(lines) > 80 else ""
        pytest.fail(
            "模型可见文本变了(工具声明或 DEFAULT_SYSTEM_PROMPT)。这些措辞是逐条事故"
            "驱动调出来的——若是刻意改动:UPDATE_MODEL_VISIBLE_SNAPSHOT=1 重生成快照,"
            "把 git diff 纳入评审;若非刻意,恢复原文。\n" + shown + more)
