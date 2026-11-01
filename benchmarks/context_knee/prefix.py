"""拐点 harness 的前缀档位（W-2「量法」：`--prefix mock|prod|catalog|routed`）。

方案 docs/plans/2026-10-31-context-management-plan.md §W-2：测量形态 ≠ 生产形态（harness 前缀
~0.6K vs 生产 full ~13.6K tok），本模块把题目的 mock 工具与生产前缀拼起来，供同一历史档位对比。

- mock（缺省）：executor 的极简 system + 题目 mock 工具；与 W-2 前逐字节一致（system_prompt /
  grammar_tools 为 None，run_item 与引擎走原路径）。
- prod：生产 system（session.DEFAULT_SYSTEM_PROMPT）+ 生产确定性工具全集完整 schema + full 档元工具
  （update_plan / complete_step / remember）+ 题目 mock 工具；grammar_tools 与 prompt 面同集合。
- catalog：system 同 prod；元工具（+get_tool_schema）与 agent.tool_routing.core 完整、其余生产工具
  目录行（tool_disclosure.disclose），其后 mock 工具完整；grammar_tools = 完整 schema 全集。
- routed：catalog 之上，按题面 task.user（不含回忆句，跨档不变）经 tool_disclosure.route_tools 选
  top_k（agent.tool_routing），选中者完整 schema。每题一次、题内冻结（纯函数，同输入同输出）。

工具集取法与 backend/scripts/measure_prefix.py 同源（scripts/tool_token_audit.collect_schemas：密封
构造、不探活 RAG、不含 rag_admin、skill_invoke 取生产形）。元工具 schema 是 orchestrator 的模块级常量
（与 Orchestrator._meta_schemas 同一份对象）；本模块不构造 Orchestrator、不起 MCPRegistry。

撞名：题目 mock 工具优先（它承载评分），同名生产工具/元工具剔除，记入 `shadowed`。生产工具只提供
声明、不执行：模型调用它们时走 executor 既有的未知工具回执（mock env 的 `Unknown tool: <名>`）。
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PREFIX_MODES = ("mock", "prod", "catalog", "routed")

_BACKEND = Path(__file__).resolve().parents[2] / "backend"


def _audit():
    """scripts/tool_token_audit.py（与 tests/_tool_fleet.py、measure_prefix.py 同一载法）。"""
    mod = sys.modules.get("tool_token_audit")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "tool_token_audit", _BACKEND / "scripts" / "tool_token_audit.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tool_token_audit"] = mod
        spec.loader.exec_module(mod)
    return mod


@dataclass(frozen=True)
class ProdToolset:
    physical: Tuple[Dict[str, Any], ...]       # 生产确定性工具（不含元工具、不含 rag_admin）
    metas_full: Tuple[Dict[str, Any], ...]     # full 档元工具（Orchestrator._meta_schemas，full）
    metas_catalog: Tuple[Dict[str, Any], ...]  # catalog 档元工具（多 get_tool_schema）
    summaries: Dict[str, str]                  # 目录行 summary（类属性，D30）
    top_k: int                                 # agent.tool_routing.top_k
    core_cfg: Tuple[str, ...]                  # agent.tool_routing.core（配置原样）


@lru_cache(maxsize=1)
def production_toolset() -> ProdToolset:
    from framework.core.orchestrator import (COMPLETE_STEP_SCHEMA, GET_TOOL_SCHEMA_SCHEMA,
                                             REMEMBER_SCHEMA, UPDATE_PLAN_SCHEMA)
    from framework.core import tool_disclosure as td
    from framework.utils.config_loader import load_section
    audit = _audit()
    metas_full = (UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA)
    metas_catalog = metas_full + (GET_TOOL_SCHEMA_SCHEMA,)
    meta_names = {m["name"] for m in metas_catalog}
    physical = tuple(s for s in audit.collect_schemas() if s["name"] not in meta_names)
    top_k, core_cfg = td.resolve_tool_routing(load_section("agent") or {})
    return ProdToolset(physical=physical, metas_full=metas_full, metas_catalog=metas_catalog,
                       summaries=dict(audit.collect_summaries(include_rag_admin=False)),
                       top_k=top_k, core_cfg=tuple(core_cfg))


def production_system_prompt() -> str:
    from framework.memory.session import DEFAULT_SYSTEM_PROMPT
    return DEFAULT_SYSTEM_PROMPT


@dataclass
class PrefixAssembly:
    mode: str
    system_prompt: Optional[str]                  # None = mock（task.system 或 executor 缺省）
    tools: List[Dict[str, Any]]                   # prompt 面声明（引擎 tools=）
    grammar_tools: Optional[List[Dict[str, Any]]]  # 约束语法源；None = mock（引擎沿用 tools）
    mock_tools: List[Dict[str, Any]]              # 题目 mock 工具（承载评分；填充按它们取名）
    shadowed: List[str]                           # 被 mock 同名顶掉的生产/元工具
    core: List[str]                               # 生效的 core（catalog/routed）
    core_skipped: List[str]                       # 配置了但不在生产集（含被撞名剔除）的 core
    routed: Optional[List[str]] = None            # routed 档选中名单


def _check_mode(mode: str) -> str:
    if mode not in PREFIX_MODES:
        raise ValueError(f"--prefix 须为 {' | '.join(PREFIX_MODES)}，得到 {mode!r}")
    return mode


def assemble(mode: str, mock_tools: Sequence[Dict[str, Any]], query: str = "",
             toolset: Optional[ProdToolset] = None) -> PrefixAssembly:
    """一道题在某档下的 (system, prompt 面 tools, grammar_tools)。mock 档不碰生产工具集。"""
    _check_mode(mode)
    mock_tools = list(mock_tools or [])
    if mode == "mock":
        return PrefixAssembly(mode=mode, system_prompt=None, tools=mock_tools,
                              grammar_tools=None, mock_tools=mock_tools, shadowed=[],
                              core=[], core_skipped=[])
    from framework.core import tool_disclosure as td
    ts = toolset or production_toolset()
    mock_names = {t["name"] for t in mock_tools}
    metas_src = ts.metas_full if mode == "prod" else ts.metas_catalog
    shadowed = [s["name"] for s in (*ts.physical, *metas_src) if s["name"] in mock_names]
    physical = [s for s in ts.physical if s["name"] not in mock_names]
    metas = [m for m in metas_src if m["name"] not in mock_names]
    system = production_system_prompt()
    if mode == "prod":
        tools = physical + metas + mock_tools
        return PrefixAssembly(mode=mode, system_prompt=system, tools=tools,
                              grammar_tools=list(tools), mock_tools=mock_tools,
                              shadowed=shadowed, core=[], core_skipped=[])
    meta_names = [m["name"] for m in metas]
    core, skipped = td.split_core(ts.core_cfg, [s["name"] for s in physical])
    routed: Optional[List[str]] = None
    if mode == "routed":
        routed = td.route_tools(query, physical, core=core, top_k=ts.top_k,
                                exclude=meta_names, summaries=ts.summaries)
    decl = td.disclose(physical + metas, mode="catalog", meta_names=meta_names, core=core,
                       routed=routed or (), summaries=ts.summaries)
    return PrefixAssembly(mode=mode, system_prompt=system, tools=decl + mock_tools,
                          grammar_tools=physical + metas + mock_tools, mock_tools=mock_tools,
                          shadowed=shadowed, core=list(core), core_skipped=list(skipped),
                          routed=routed)


def production_decl(mode: str, toolset: Optional[ProdToolset] = None) -> Optional[List[Dict[str, Any]]]:
    """prefix_base_tokens 口径的生产声明：未剔撞名、不含 mock 工具与路由件（routed 档 = catalog
    基座），与 scripts/measure_prefix.py 的 full / catalog(meta+core) 两行同口径。mock 档 = None。"""
    _check_mode(mode)
    if mode == "mock":
        return None
    from framework.core import tool_disclosure as td
    ts = toolset or production_toolset()
    if mode == "prod":
        return list(ts.physical) + list(ts.metas_full)
    meta_names = [m["name"] for m in ts.metas_catalog]
    core, _ = td.split_core(ts.core_cfg, [s["name"] for s in ts.physical])
    return td.disclose(list(ts.physical) + list(ts.metas_catalog), mode="catalog",
                       meta_names=meta_names, core=core, summaries=ts.summaries)


def prefix_base_tokens(dialect, encode_len, system_prompt: str,
                       decl: Optional[List[Dict[str, Any]]]) -> int:
    """整段稳定前缀 token：messages=[system]、include_env=False（engine.prewarm_base_cache /
    measure_prefix 同款渲染）。"""
    text = dialect.build_prompt(messages=[{"role": "system", "content": system_prompt}],
                                tools=decl, include_env=False)
    return int(encode_len(text))


def local_tokenizer_encode_len(repo: str):
    """fake 模式的离线真分词器：backend/models/<repo 末段>（只载 tokenizer）。不可得 → (None, 原因)。"""
    local = _BACKEND / "models" / Path(repo).name
    if not repo or "/" not in repo or not local.is_dir():
        return None, None, f"本地分词器目录不在：{local}"
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(str(local), local_files_only=True)
    except Exception as e:      # noqa: BLE001 —— 记 null，不中止批次
        return None, None, f"分词器载入失败：{type(e).__name__}: {e}"
    return tok, (lambda s: len(tok.encode(s, add_special_tokens=False))), None
