"""W-2 稳定前缀 token 表（离线分词器，只读，零网络）。

量法与 2026-11-01 scout 同口径：整段 prompt = 方言 builder.build(messages=[system:
DEFAULT_SYSTEM_PROMPT], tools=声明, include_env=False)——即 engine.prewarm_base_cache 渲染的
那一段（含日期块与生成头）。三档声明：
  full            全部工具完整 schema + 三件常驻元工具（生产 full 口径）
  catalog         元工具（含 get_tool_schema）+ agent.tool_routing.core 完整，其余目录行
  catalog+routed  上一档 + 示例任务 goal 经 tool_disclosure.route_tools 选出的 top_k 件完整
每档另给「RAG 在线（+rag_admin 7 件）」一列。另附逐件声明之和（test_model_visible_text 门的
口径：不含 system、包裹与固定教学文本）。

分词器：backend/models/<repo 名> 本地目录（只载 tokenizer，不载权重）。用法：
  cd backend && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python scripts/measure_prefix.py
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_spec = importlib.util.spec_from_file_location("tool_token_audit",
                                               BACKEND / "scripts" / "tool_token_audit.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("tool_token_audit", audit)
_spec.loader.exec_module(audit)

from framework.core import tool_disclosure as td  # noqa: E402
from framework.dialects.gemma4.dsl import format_function_declaration  # noqa: E402
from framework.dialects.gemma4.prompt_builder import PromptBuilder  # noqa: E402
from framework.dialects.qwen35.prompt_builder import (Qwen35PromptBuilder,  # noqa: E402
                                                      format_tool_declaration)
from framework.memory.session import DEFAULT_SYSTEM_PROMPT  # noqa: E402
from framework.utils.config_loader import load_section  # noqa: E402

GEMMA4_REPO = "mlx-community/gemma-4-31b-it-4bit"
QWEN35_REPO = "mlx-community/Qwen3.8-27B-4bit"
META_NAMES = [audit.UPDATE_PLAN_SCHEMA["name"], audit.COMPLETE_STEP_SCHEMA["name"],
              audit.REMEMBER_SCHEMA["name"], audit.GET_TOOL_SCHEMA_SCHEMA["name"]]
# 示例任务（路由档用）：典型中文请求各一类，只为看路由件的体量与选中谁，不是评测集
EXAMPLE_GOALS = [
    "帮我做一份公司季度业绩的 PPT，最后导出成 PDF",
    "每天早上 9 点提醒我看邮件，并把昨天的日程总结一下",
    "在我的知识库里找上次那份合同的付款条款",
]


def _tokenizer(repo):
    from transformers import AutoTokenizer
    local = BACKEND / "models" / repo.split("/")[-1]
    if not local.is_dir():
        sys.exit(f"本地分词器目录不在：{local}")
    tok = AutoTokenizer.from_pretrained(str(local))
    return lambda s: len(tok.encode(s, add_special_tokens=False)), tok


def main() -> int:
    schemas = audit.collect_schemas()                  # 53 物理 + 4 元（含 get_tool_schema）
    rag_admin = audit.collect_rag_admin_schemas()
    summaries = audit.collect_summaries(include_rag_admin=True)
    top_k, core_cfg = td.resolve_tool_routing(load_section("agent") or {})
    names = {s["name"] for s in schemas}
    core, unknown = td.split_core(core_cfg, names)

    gs = audit.GET_TOOL_SCHEMA_SCHEMA["name"]
    physical = [s for s in schemas if s["name"] not in META_NAMES]
    metas_full = [s for s in schemas if s["name"] in META_NAMES and s["name"] != gs]
    metas_cat = [s for s in schemas if s["name"] in META_NAMES]

    def full_decl(rag):
        return physical + (rag_admin if rag else []) + metas_full

    def cat_decl(rag, routed=()):
        return td.disclose(physical + (rag_admin if rag else []) + metas_cat, mode="catalog",
                           meta_names=META_NAMES, core=core, routed=routed,
                           summaries=summaries)

    count_g, tg = _tokenizer(GEMMA4_REPO)
    count_q, tq = _tokenizer(QWEN35_REPO)
    families = [
        ("gemma4", count_g, PromptBuilder(SimpleNamespace(tokenizer=tg),
                                          suppress_ghost_thinking=True),
         format_function_declaration),
        ("qwen35", count_q, Qwen35PromptBuilder(SimpleNamespace(tokenizer=tq)),
         format_tool_declaration),
    ]

    print(f"工具：{len(physical)} 物理 + {len(metas_cat)} 元（RAG 在线另 +{len(rag_admin)}）；"
          f"显式 summary {len(summaries)} 件；tool_routing top_k={top_k} core={list(core)}"
          + (f"；core 未注册已跳过 {list(unknown)}" if unknown else ""))
    routed_examples = []
    for goal in EXAMPLE_GOALS:
        routed = td.route_tools(goal, physical, core=core, top_k=top_k,
                                exclude=META_NAMES, summaries=summaries)
        routed_examples.append((goal, routed))
        print(f"  路由示例「{goal}」→ {routed}")
    print()
    print("## 整段前缀 token（system + 声明，include_env=False）")
    print(f"{'档位':28s} {'gemma4':>8s} {'+rag':>8s} {'qwen35':>8s} {'+rag':>8s}")

    def build(builder, tools):
        return builder.build(messages=[{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}],
                             tools=tools, include_env=False)

    rows = [("system only", lambda rag: None),
            ("full", full_decl),
            ("catalog (meta+core)", cat_decl)]
    for i, (goal, _) in enumerate(routed_examples, 1):
        rows.append((f"catalog+routed 例{i}",
                     lambda rag, g=goal: cat_decl(rag, td.route_tools(
                         g, physical + (rag_admin if rag else []), core=core, top_k=top_k,
                         exclude=META_NAMES, summaries=summaries))))
    for label, decl in rows:
        cells = []
        for _, count, builder, _fmt in families:
            for rag in (False, True):
                cells.append(count(build(builder, decl(rag))))
        print(f"{label:28s} " + " ".join(f"{c:8d}" for c in cells))

    print()
    print("## 逐件声明之和（test_model_visible_text 门口径；不含 system/包裹/教学文本，无 RAG）")
    for fam, count, _b, fmt in families:
        full_sum = sum(count(fmt(s)) for s in full_decl(False))
        cat_sum = sum(count(fmt(s)) for s in cat_decl(False))
        print(f"{fam}: full {full_sum}  catalog(meta+core) {cat_sum}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
