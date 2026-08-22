import json
import requests
from ..base import BaseTool, ToolOutput
from ...utils.config_loader import load_section, rag_auth_headers

# 配置统一在仓库根 config.yaml（rag 段），寻址走 config_loader
_rag_cfg = load_section("rag")
RAG_HOST = _rag_cfg.get("host", "127.0.0.1")
RAG_PORT = _rag_cfg.get("port", 14160)

RAG_HEALTH_URL = f"http://{RAG_HOST}:{RAG_PORT}/health"
RAG_SEARCH_URL = f"http://{RAG_HOST}:{RAG_PORT}/api/v1/document/query"
RAG_CATEGORY_URL = f"http://{RAG_HOST}:{RAG_PORT}/api/v1/category"


def format_results(query: str, category: str, docs: list) -> tuple:
    """把检索结果排成 (模型可读文本, 引文卡片载荷)。

    时效可见化（记忆治理波 U2）：片段头带入库日期（metadata.created_at，P2a 起注入），
    模型对照当天日期自判时效——「答错」降级为「带保留地答」。存量旧切片无此字段，
    整段省略不猜。
    """
    formatted_context = (f"[Search results for '{query}' in category '{category}': "
                         f"{len(docs)} highly relevant results found]\n\n")
    citations = []
    for i, item in enumerate(docs):
        score = item.get("rerank_score", 0)
        doc_text = item.get("document", "")
        # rag 服务将来源信息放在 metadata 中（title / source_path / created_at）；
        # Path 供 rag_delete_document 等管理工具精确引用文档
        metadata = item.get("metadata") or {}
        source = metadata.get("title") or "Unknown"
        source_path = metadata.get("source_path") or "Unknown"
        created = str(metadata.get("created_at") or "")[:10]   # 日期粒度足够判时效
        ingested = f", Ingested: {created}" if created else ""
        stale = str(metadata.get("stale") or "")               # U3：非空 = 已被标记过时
        stale_tag = f", STALE: {stale}" if stale else ""
        # 凭证指针（R3）：块序号供 rag_get_context 取相邻块；存量旧切片无此字段则省略
        seq = metadata.get("seq")
        chunk_tag = f", Chunk: {seq}" if seq is not None else ""

        formatted_context += (
            f"--- Result {i+1} (Match Score: {score:.2f}, Source: {source}"
            f"{ingested}{stale_tag}{chunk_tag}, Path: {source_path}) ---\n"
            f"{doc_text}\n\n"
        )
        cite = {
            "source": source,
            "path": source_path,
            "score": round(float(score), 4),
            # 卡片只摆摘要，全文照旧在 response 文本里给模型读
            "snippet": doc_text[:280],
        }
        if created:
            cite["created_at"] = created
        if stale:
            cite["stale"] = stale
        citations.append(cite)
    return formatted_context.strip(), citations

class RAGSearchTool(BaseTool):
    """
    RAG 知识库检索插件。
    连接到本地高精度 RAG 向量数据库引擎获取外部上下文。
    """
    name = "rag_search"
    # 2026-08-10 措辞纠错（#83 准则审计）：旧文案的 "questions that require external
    # sources for verification" 把 web_research 的职权划了过来——26B 档会照字面拿
    # 本地知识库去答新闻/时事题。RAG 只管用户自己收进来的资料。
    description = (
        "Search the user's LOCAL knowledge base (documents they ingested: notes, files, "
        "papers, past material). Use it when the answer likely lives in the user's own "
        "stored documents — 'what did that report say', their notes/manuals/records. It "
        "knows NOTHING about the public web or current events: for news, prices, or "
        "up-to-date public facts use web_research instead. If a first search misses, "
        "retry ONCE with rephrased keywords (synonyms, or the language the stored "
        "documents are written in) before concluding the knowledge base doesn't have it."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读向量检索，可与其他只读调用并发
    # 渲染格（R3 试点）：召回结果今天是灰框里一大坨格式化文本——命中分、来源、路径全
    # 埋在文字里，用户要自己数 "--- Result N ---"。这些结构在 execute 里本来就有，
    # 随 ToolOutput 交给客户端排成引文卡片即可（文本照旧给模型读）。
    render_hint = "citations"

    def __init__(self):
        # 初始化时动态探测可用的知识库分类，构建 Schema；
        # 探活失败则标记自身不可用，由 MCPRegistry 在注册阶段舍弃
        self._available = True
        categories = ["default"]
        try:
            response = requests.get(url=RAG_CATEGORY_URL, timeout=3, headers=rag_auth_headers()).json()
            if isinstance(response, list):
                categories = [i.get("db_name", "default") for i in response if isinstance(i, dict)]
        except Exception as e:
            self._available = False
            print(f"[MCP-rag_search] ⚠️ RAG 服务器 ({RAG_HOST}:{RAG_PORT}) 探活失败: {e}")

        self.parameters = {
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query used by the reranker. Maintain complete semantics and user intent."
                },
                "category": {
                    "type": "string",
                    "description": (
                        f"Specify the knowledge category. Categories known at startup: {', '.join(categories)}. "
                        "This list may be stale — categories created later (e.g. via rag_upsert_category) "
                        "also work; call rag_list_categories for the current catalog. "
                        "If multiple categories seem relevant, select the one that is most likely to contain the answer."
                    )
                },
                "optimized_query": {
                    "type": "string",
                    "description": (
                        "Keyword-based query used for embedding retrieval. "
                        "Represent the information need as a compact set of high-value search terms, "
                        "including entities, domain terminology, synonyms, abbreviations, and filters. "
                        "Avoid unnecessary natural-language wording."
                    )
                },
                "rerank_top_k": {
                    "type": "integer",
                    "description": "Number of top results to return after reranking. Default is 3."
                }
            },
            "required": ["query", "category", "optimized_query"]
        }

    def is_available(self) -> bool:
        """RAG 后端在线才视为可用；不可用时本工具不会被注册进会话。"""
        return self._available

    def execute(self, query: str, category: str, optimized_query: str, rerank_top_k: int = 3) -> str:
        """
        接收 LLM 传入的参数，执行实际的 HTTP RAG 请求。
        """
        # 构建给 RAG 后端的 payload（字段名须与 rag 服务的 QueryRequest 一致）
        payload = {
            "query": query,
            "category": category,
            "optimized_query": optimized_query,
            "chunk_k_per_db": max(5, rerank_top_k * 2),  # 初筛数量放大，为重排留足冗余
            "rerank_top_k": rerank_top_k
        }

        try:
            response = requests.post(
                url=RAG_SEARCH_URL,
                json=payload,
                headers={**rag_auth_headers(), 'Content-Type': 'application/json'},
                timeout=15
            )
            response.raise_for_status()
            result_dict = response.json()

            docs = result_dict.get("results", [])
            if not docs:
                # 空结果细分（F3 后半，复审修正后的口径）：total_chunks_retrieved 是
                # **最近邻池**大小——无距离阈值,非空库上几乎恒 >0（rag_server 的
                # _query_single_db 恒返回 top-k）。它能区分的不是「库里有没有这个主题」
                # （那要看重排分,服务端不回传）,而是「扫到了库但全被相关性阈值筛掉」vs
                # 「目标库是空的/不存在」。话术只许陈述这两件事实,**不许**据此宣称
                # 「库里有料」——那是在制造对缺失内容的假信心,比不细分更糟。
                try:
                    retrieved = int(result_dict.get("total_chunks_retrieved") or 0)
                except (TypeError, ValueError):
                    retrieved = 0          # 脏值按空库口径降级,不许把良性空结果炸成 System Error
                if retrieved > 0:
                    return (f"No results for '{query}' in category '{category}': the "
                            f"{retrieved} nearest chunk(s) all scored below the relevance "
                            "cutoff after reranking. The topic is likely not covered here, "
                            "or is phrased very differently. You may retry ONCE with "
                            "different wording or another category; otherwise report the "
                            "information as not found in the knowledge base.")
                return (f"No results for '{query}' in category '{category}': the target "
                        "store is empty or unavailable — nothing was retrieved at all. "
                        "Pick a different category, or the material may need to be "
                        "ingested first.")
            
            # 格式化检索结果供大模型阅读；渲染格载荷（R3）与格式化同源同循环——
            # 结构交给客户端排引文卡片，不必让谁再从格式化文本里解析回来。
            formatted_context, citations = format_results(query, category, docs)
            return ToolOutput(formatted_context,
                              {"query": query, "category": category, "items": citations})

        except requests.ConnectionError as e:
            return f"System Error: Tool execution failed. Unable to connect to the RAG server (please check if {RAG_HOST}:{RAG_PORT} is running)."
        except Exception as e:
            import traceback
            return f"System Error: Tool execution exception occurred: {str(e)}\n{traceback.format_exc()}"


class RagGetContextTool(BaseTool):
    """取相邻块（数据架构采纳波 R3）：检索命中后按凭证指针补前后文。

    「先搜索定位，再翻开细读」的第二步——比整篇重读省 token，比只凭单块作答可靠。
    """
    name = "rag_get_context"
    description = (
        "Fetch the chunks surrounding one rag_search result, to read more context around a "
        "hit without re-reading the whole document. Use the exact 'Path' and 'Chunk' values "
        "from a rag_search result header. Results that show no 'Chunk' field are legacy "
        "entries without positions — this tool won't work for those."
    )
    requires_approval = False
    is_concurrency_safe = True   # 纯只读数据操作

    parameters = {
        "properties": {
            "source_path": {"type": "string",
                            "description": "The exact 'Path: ...' value from a rag_search result."},
            "seq": {"type": "integer",
                    "description": "The 'Chunk: N' value from that result."},
            "n": {"type": "integer",
                  "description": "How many neighbors on each side (default 1, max 5)."},
        },
        "required": ["source_path", "seq"],
    }

    def is_available(self) -> bool:
        try:
            requests.get(RAG_HEALTH_URL, timeout=3)
            return True
        except Exception:
            return False

    def execute(self, source_path: str, seq: int, n: int = 1) -> str:
        try:
            r = requests.get(f"http://{RAG_HOST}:{RAG_PORT}/api/v1/document/context",
                             params={"source_path": source_path, "seq": seq, "n": n},
                             headers=rag_auth_headers(), timeout=15)
            r.raise_for_status()
            data = r.json()
        except requests.ConnectionError:
            return (f"System Error: unable to connect to the RAG server "
                    f"({RAG_HOST}:{RAG_PORT}).")
        except Exception as e:
            return f"System Error: rag_get_context failed: {e}"
        chunks = data.get("chunks") or []
        if not chunks:
            return data.get("msg") or "No chunks found for that path."
        head = (f"[Context around chunk {seq} of '{data.get('title', '')}' "
                f"({data.get('total_chunks')} chunks total)]\n")
        body = "\n".join(f"--- Chunk {c['seq']}"
                         + (" (the hit)" if c["seq"] == seq else "")
                         + f" ---\n{c['text'].strip()}" for c in chunks)
        return head + body