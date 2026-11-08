"""拐点 harness 的前缀档位（W-2「量法」：`--prefix mock|prod|catalog|routed|full`）。

方案 docs/plans/2026-10-31-context-management-plan.md §W-2：测量形态 ≠ 生产形态（harness 前缀
~0.6K vs 生产 full ~13.6K tok），本模块把题目的 mock 工具与生产前缀拼起来，供同一历史档位对比。

- mock（缺省）：executor 的极简 system + 题目 mock 工具；与 W-2 前逐字节一致（system_prompt /
  grammar_tools 为 None，run_item 与引擎走原路径）。
- prod：生产 system（session.DEFAULT_SYSTEM_PROMPT）+ 生产确定性工具全集完整 schema + full 档元工具
  （update_plan / complete_step / remember）+ 题目 mock 工具；grammar_tools 与 prompt 面同集合。
  编排器模式下 prod 随条目生效披露（decl_mode，2026-11-07）：条目 tool_schema_mode=catalog 时装配与行内
  口径按 catalog 档算（实际送入编排器的就是 catalog 前缀），prefix_mode 仍记 prod、行另记 prefix_decl。
- catalog：system 同 prod；元工具（+get_tool_schema）与 agent.tool_routing.core 完整、其余生产工具
  目录行（tool_disclosure.disclose），其后 mock 工具完整；grammar_tools = 完整 schema 全集。
- routed：catalog 之上，按题面 task.user（不含回忆句，跨档不变）经 tool_disclosure.route_tools 选
  top_k（agent.tool_routing），选中者完整 schema。每题一次、题内冻结（纯函数，同输入同输出）。
- full（D44 (iii) 一审 m3，2026-11-06）：装配与 prod 逐字节相同（生产 system + 全量完整 schema）；只在
  `--orchestrator` 下有意义——run_knee.orch_model_options 把条目层 tool_schema_mode 强制成 full（镜像
  catalog 档），用来在条目已是 catalog 的模型上测 full 臂。直调模式的 prod 本就是全量完整声明（直调
  不经披露，条目的 tool_schema_mode 不起作用），run_knee 在直调模式拒 full（不给同一装配第二个名字，
  免得 prefix_mode 分组 / 续跑比对多一个同义档）。

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

PREFIX_MODES = ("mock", "prod", "catalog", "routed", "full")
#: 装配 = prod 全量完整声明的档（full 只是编排器模式下强制条目 full 的名字）
_FULL_DECL_MODES = ("prod", "full")

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


def decl_mode(mode: str, tool_schema_mode: Optional[str] = None) -> str:
    """装配与行内口径（prefix_base_tokens / prefix_n_tools / prefix_core_skipped）实际按哪一档算。

    prod 档不写死 full：编排器模式下随条目生效披露 tool_schema_mode（= orch_meta 的
    tool_schema_mode_effective，tool_disclosure.resolve_tool_schema_mode 解析的条目级 full | catalog）——
    生效 catalog 时实际送入编排器的是 catalog 前缀，口径也按 catalog 算（2026-11-07 P2 批 ANALYSIS §2
    缺陷 (1)：prod 行写 full 的 13757 tok / prompt_full 60，而首 prompt 与 catalog 臂逐 token 相同）。
    直调模式（tool_schema_mode=None）prod 本就是全量完整声明，原样。full / catalog / routed / mock
    不随它变（full 档本身就是把条目强制成 full 的名字，catalog 档同理）。生效值域取
    tool_disclosure.TOOL_SCHEMA_MODES（域扩了这里先拒，不静默当 full）。"""
    from framework.core.tool_disclosure import TOOL_SCHEMA_MODES
    _check_mode(mode)
    if tool_schema_mode is not None and tool_schema_mode not in TOOL_SCHEMA_MODES:
        raise ValueError(f"tool_schema_mode 须为 None | {' | '.join(TOOL_SCHEMA_MODES)}，得到 {tool_schema_mode!r}")
    if mode == "prod" and tool_schema_mode == "catalog":
        return "catalog"
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
    metas_src = ts.metas_full if mode in _FULL_DECL_MODES else ts.metas_catalog
    shadowed = [s["name"] for s in (*ts.physical, *metas_src) if s["name"] in mock_names]
    physical = [s for s in ts.physical if s["name"] not in mock_names]
    metas = [m for m in metas_src if m["name"] not in mock_names]
    system = production_system_prompt()
    if mode in _FULL_DECL_MODES:
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
    if mode in _FULL_DECL_MODES:
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
