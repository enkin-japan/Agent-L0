import json
import requests
from ..base import BaseTool
from ...utils.config_loader import load_section, rag_auth_headers

# 配置统一在仓库根 config.yaml（rag 段），寻址走 config_loader
_rag_cfg = load_section("rag")
RAG_HOST = _rag_cfg.get("host", "127.0.0.1")
RAG_PORT = _rag_cfg.get("port", 14160)

RAG_HEALTH_URL = f"http://{RAG_HOST}:{RAG_PORT}/health"
RAG_SEARCH_URL = f"http://{RAG_HOST}:{RAG_PORT}/api/v1/document/query"
RAG_CATEGORY_URL = f"http://{RAG_HOST}:{RAG_PORT}/api/v1/category"

class RAGSearchTool(BaseTool):
    """
    RAG 知识库检索插件。
    连接到本地高精度 RAG 向量数据库引擎获取外部上下文。
    """
    name = "rag_search"
    description = "Search the local high-precision RAG vector database. You must use this tool when you need to access a user’s personal knowledge base, local historical documents, or answer questions that require external sources for verification."
    requires_approval = False
    is_concurrency_safe = True  # 只读向量检索，可与其他只读调用并发

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
                return f"No results were found in the database for '{query}' in category '{category}'."
            
            # 格式化检索结果供大模型阅读
            formatted_context = f"[Search results for '{query}' in category '{category}': {len(docs)} highly relevant results found]\n\n"
            for i, item in enumerate(docs):
                score = item.get("rerank_score", 0)
                doc_text = item.get("document", "")
                # rag 服务将来源信息放在 metadata 中（title / source_path）；
                # Path 供 rag_delete_document 等管理工具精确引用文档
                metadata = item.get("metadata") or {}
                source = metadata.get("title") or "Unknown"
                source_path = metadata.get("source_path") or "Unknown"

                formatted_context += (
                    f"--- Result {i+1} (Match Score: {score:.2f}, Source: {source}, Path: {source_path}) ---\n"
                    f"{doc_text}\n\n"
                )
                
            return formatted_context.strip()

        except requests.ConnectionError as e:
            return f"System Error: Tool execution failed. Unable to connect to the RAG server (please check if {RAG_HOST}:{RAG_PORT} is running)."
        except Exception as e:
            import traceback
            return f"System Error: Tool execution exception occurred: {str(e)}\n{traceback.format_exc()}"