"""工具披露策略（W-2 静态前缀瘦身，D29–D32，用户 2026-11-01 拍板）。

稳定前缀 = system + 工具声明。本模块只回答一个问题：**这一次生成的 prompt 里，每件工具
以什么形态出现**——完整 schema，还是一行目录（名字 + summary，详情经 get_tool_schema 取）。

- D29 分档：披露模式 = 注册表条目 `tool_schema_mode` > `agent.tool_schema_mode` > "full"；
  值限 full | catalog，其他值构造期 ValueError 拒启（同 W-1 sampling 的 fail-closed 风格）。
- D30 目录行：文本 = 工具类属性 `summary`，缺省取 description 首句，硬截 80 字符。summary
  **不进** full 模式渲染的 schema 字典（full 字节不变），由 registry.get_summaries() 另给。
- D31 任务期路由（只在 catalog 模式）：任务建立时按词法打分选 top_k 件，与元工具、core 一起
  以完整 schema 入前缀，其余目录行；任务期内冻结（冻结点由编排层持有，本模块无状态）。
- D32 打分：纯本地词法、确定性——小写 ASCII 词 + CJK 相邻双字 bigram，BM25（k1=1.2,
  b=0.75），只取得分 > 0 的，不足 top_k 不补，同分按名升序。不调 rag_server、不调 embedding。

约束：纯函数、无编排层依赖（benchmarks/context_knee 的 harness 复用本模块）。
**约束采样的语法面不经本模块**：它恒用完整 schema（orchestrator._build_grammar_tools），
只有 prompt 面会瘦身。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

TOOL_SCHEMA_MODES = ("full", "catalog")
SUMMARY_MAX_CHARS = 80
BM25_K1 = 1.2
BM25_B = 0.75
ROUTING_KEYS = frozenset({"top_k", "core"})
HIGH_DF_MIN_DOCS = 4          # df > N/2 剔除的最小语料规模

# 首句终止符：全角句末标点见即截（含该标点）；ASCII `.!?` 只在其后是空白或文末时才算句末——
# 否则 `config.yaml` / `v1.2` / `e.g` 里的点会把首句截成半个词。换行处截（不含换行）。
_CJK_TERMINATORS = "。．！？"
_ASCII_TERMINATORS = ".!?"

# 分词：小写 ASCII 词（下划线切开，另保留整词以便模型/用户直呼工具名时得高分）
# + CJK 相邻双字 bigram（单字串退化为该字本身）。
_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]+")

# 查询词过滤（首审 Minor 3）：BM25 的 idf 恒正，"please/for/me" 这类虚词会让任何英文闲聊都
# 给半数工具打出 >0 分、top_k 恒被填满。三层：① 小英文停用词表；② 语言无关的高频剔除——
# 在候选集里 df > N/2 的词不参与打分（只在 N ≥ 4 时启用：N=1/2 时「半数」退化成「任何
# 命中」，会把小语料里唯一的匹配也剔掉）（CJK bigram 的「工具」「文件」之类同样适用）；③ 查询里
# 直呼了一件被排除（core/元工具）的工具名时，该名及其切片不参与打分——它已常驻前缀，
# 其名字里的 run/shell/command 不该把别的工具捎带进来（route_tools 处理）。
QUERY_STOPWORDS = frozenset("""
a an the to for of and or in on at by from with without into onto about as is are was were be been
it its this that these those there here do does did done can could will would should may might must
i me my mine we us our you your he she they them their please pls thanks thank hi hello hey
what which who whom how why when where just also very so too not no yes ok okay some any all
help want need like let give tell write short long quick little something thing
then again now later first next last before after once more most much many every each other another
same new old good well back up down out over than if but because while until since such only even
still already yet maybe sure really actually make made get got use using try
""".split())   # 末行 = 请求里泛用的动词/形容词（"write a short poem" 不该捎带 write_document）


# ---------- D29：披露模式解析 ----------

def _check_mode(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or raw not in TOOL_SCHEMA_MODES:
        raise ValueError(f"{where} tool_schema_mode 须为 {' | '.join(TOOL_SCHEMA_MODES)}，"
                         f"得到 {raw!r}")
    return raw


def resolve_tool_schema_mode(entry_options: Optional[Mapping[str, Any]],
                             agent_cfg: Optional[Mapping[str, Any]]) -> str:
    """注册表条目 > agent 段 > "full"。两层任一层给了非法值即 ValueError（不静默回落）。

    条目层非法值即便全局层合法也拒启：条目是更具体的声明，写错了就是写错了。"""
    # 任一层键值为 None（YAML 里写了 `tool_schema_mode:` 没给值）视为未设，落到下一层。
    entry = entry_options if isinstance(entry_options, Mapping) else {}
    agent = agent_cfg if isinstance(agent_cfg, Mapping) else {}
    if entry.get("tool_schema_mode") is not None:
        mode = _check_mode(entry["tool_schema_mode"], "model_registry 条目")
        if agent.get("tool_schema_mode") is not None:
            _check_mode(agent["tool_schema_mode"], "agent")
        return mode
    if agent.get("tool_schema_mode") is not None:
        return _check_mode(agent["tool_schema_mode"], "agent")
    return "full"


def resolve_tool_routing(agent_cfg: Optional[Mapping[str, Any]]) -> Tuple[int, Tuple[str, ...]]:
    """`agent.tool_routing` → (top_k, core)。缺段 = (0, ())。

    top_k 须为 int ≥ 0（bool 不算 int），core 须为字符串列表；未知键、错类型一律 ValueError。
    core 名字是否已注册**不在这里判**——rag_admin 等按探活出现，见 split_core。"""
    agent = agent_cfg if isinstance(agent_cfg, Mapping) else {}
    raw = agent.get("tool_routing")
    if raw is None:
        return 0, ()
    if not isinstance(raw, Mapping):
        raise ValueError(f"agent.tool_routing 须为映射，得到 {type(raw).__name__}")
    unknown = sorted(set(raw) - ROUTING_KEYS)
    if unknown:
        raise ValueError(f"agent.tool_routing 含未知键 {unknown}；允许 {sorted(ROUTING_KEYS)}")
    top_k = raw.get("top_k", 0)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0:
        raise ValueError(f"agent.tool_routing.top_k 须为 ≥0 的整数，得到 {top_k!r}")
    core = raw.get("core")
    if core is None:
        core = []
    if not isinstance(core, (list, tuple)) or not all(isinstance(n, str) and n for n in core):
        raise ValueError(f"agent.tool_routing.core 须为非空字符串列表，得到 {core!r}")
    return top_k, tuple(dict.fromkeys(core))


def split_core(core: Iterable[str], known: Iterable[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """(已注册的 core, 未注册的 core)，均保持配置顺序。未注册者由调用方告警并跳过。"""
    known_set = set(known)
    core = tuple(core)
    return (tuple(n for n in core if n in known_set),
            tuple(n for n in core if n not in known_set))


# ---------- D30：目录行 ----------

def first_sentence(text: str) -> str:
    """description 的首句（含句末标点），去尾空白；无终止符则为整段。"""
    text = str(text or "").lstrip()
    for i, ch in enumerate(text):
        if ch == "\n":
            return text[:i].rstrip()
        if ch in _CJK_TERMINATORS:
            return text[:i + 1].rstrip()
        if ch in _ASCII_TERMINATORS and (i + 1 == len(text) or text[i + 1].isspace()):
            return text[:i + 1].rstrip()
    return text.rstrip()


def tool_summary(schema: Mapping[str, Any], summaries: Optional[Mapping[str, str]] = None) -> str:
    """目录行文本：显式 summary 优先，缺省取 description 首句；两者都硬截到 80 字符。"""
    explicit = (summaries or {}).get(str(schema.get("name", "")))
    if isinstance(explicit, str) and explicit.strip():
        text = explicit.strip()
    else:
        text = first_sentence(str(schema.get("description", "")))
    return text[:SUMMARY_MAX_CHARS].rstrip()


def catalog_entry(schema: Mapping[str, Any],
                  summaries: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """目录行：只有 name + description 两键（无 parameters → 声明里没有可抄的参数形状，
    模型需要详情时 get_tool_schema 取）。"""
    return {"name": schema.get("name"), "description": tool_summary(schema, summaries)}


# ---------- D32：词法打分 ----------

def tokenize(text: str) -> List[str]:
    """小写 ASCII 词（`a_b` 产出 a、b 与 a_b）+ CJK 相邻双字 bigram（单字串产出该字）。"""
    text = str(text or "")
    out: List[str] = []
    for m in _ASCII_WORD.finditer(text.lower()):
        word = m.group()
        parts = [p for p in word.split("_") if p]
        out.extend(parts)
        if len(parts) > 1:
            out.append(word.strip("_"))
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _doc_text(schema: Mapping[str, Any], summaries: Optional[Mapping[str, str]]) -> str:
    name = str(schema.get("name", ""))
    summary = (summaries or {}).get(name) or ""
    return f"{name}\n{schema.get('description', '')}\n{summary}"


def lexical_scores(query: str, schemas: Sequence[Mapping[str, Any]],
                   summaries: Optional[Mapping[str, str]] = None, *,
                   drop_terms: Iterable[str] = ()) -> Dict[str, float]:
    """BM25（k1=1.2, b=0.75；idf = ln((N−n+0.5)/(n+0.5)+1) 恒正）。语料 = 传入的 schemas，
    文档 = name + description + summary；查询词去重，剔除 QUERY_STOPWORDS、drop_terms 与
    df > N/2 的词。返回 {name: score}（含 0 分）。"""
    docs = [(str(s.get("name", "")), tokenize(_doc_text(s, summaries))) for s in schemas]
    n_docs = len(docs)
    if n_docs == 0:
        return {}
    avgdl = (sum(len(toks) for _, toks in docs) / n_docs) or 1.0
    tfs = []
    df: Dict[str, int] = {}
    for _, toks in docs:
        tf: Dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        tfs.append(tf)
        for t in tf:
            df[t] = df.get(t, 0) + 1
    drop = set(drop_terms)
    df_cap = n_docs / 2 if n_docs >= HIGH_DF_MIN_DOCS else n_docs
    q_terms = [t for t in dict.fromkeys(tokenize(query))
               if t not in QUERY_STOPWORDS and t not in drop and df.get(t, 0) <= df_cap]
    scores: Dict[str, float] = {}
    for (name, toks), tf in zip(docs, tfs):
        dl = len(toks)
        s = 0.0
        for t in q_terms:
            f = tf.get(t, 0)
            if not f:
                continue
            idf = math.log((n_docs - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
            s += idf * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
        scores[name] = s
    return scores


def route_tools(query: str, schemas: Sequence[Mapping[str, Any]], *,
                core: Iterable[str] = (), top_k: int,
                exclude: Iterable[str] = (),
                summaries: Optional[Mapping[str, str]] = None) -> List[str]:
    """候选 = schemas − core − exclude（元工具）；得分 > 0 的按 (−分, 名) 排，取前 top_k。

    不足 top_k 不补；top_k ≤ 0 = 不路由。IDF 只在候选集内统计（core/元工具不稀释词频）。"""
    if top_k <= 0:
        return []
    skip = set(core) | set(exclude)
    candidates = [s for s in schemas if s.get("name") not in skip]
    # 代价（如实记）：丢的是**词**不是「对这件工具的提及」——同名切片对其他工具的贡献一并被
    # 削。例：「用 list_dir 看看，再把日程 list 出来」里 list_dir 在 core，list 被丢，
    # calendar_list 少了 list 这一项得分（「日程」bigram 仍在）。为挡住 run/shell/command
    # 这类切片把无关工具捎带进来，接受这个代价。
    q_tokens = set(tokenize(query))
    drop = set()
    for name in skip:
        parts = tokenize(name)
        if parts and parts[-1] in q_tokens and "_" in name:   # 直呼了常驻工具的全名
            drop.update(parts)
    scores = lexical_scores(query, candidates, summaries, drop_terms=drop)
    ranked = sorted((n for n, v in scores.items() if v > 0), key=lambda n: (-scores[n], n))
    return ranked[:top_k]


# ---------- 装配 ----------

def disclose(schemas: Sequence[Dict[str, Any]], *, mode: str,
             meta_names: Iterable[str] = (), core: Iterable[str] = (),
             routed: Iterable[str] = (),
             summaries: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """prompt 面的工具声明。

    full：原 schema 列表（元素对象恒等，与 W-2 前逐字节一致）。
    catalog：元工具 → core → routed 的完整 schema（各自保持输入顺序），其后其余工具的目录行
    （输入顺序）。排序理由：system + 元工具 + core 段跨任务不变，与无路由的基座预热缓存
    共享最长前缀；路由集合只影响其后的部分。"""
    mode = _check_mode(mode, "disclose")
    if mode == "full":
        return list(schemas)
    metas, cores, routes = set(meta_names), set(core), set(routed)
    out = [s for s in schemas if s.get("name") in metas]
    out += [s for s in schemas if s.get("name") in cores and s.get("name") not in metas]
    out += [s for s in schemas
            if s.get("name") in routes and s.get("name") not in metas | cores]
    out += [catalog_entry(s, summaries) for s in schemas
            if s.get("name") not in metas | cores | routes]
    return out
