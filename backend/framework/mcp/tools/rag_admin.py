"""
RAG 知识库管理插件集：在 rag_search 检索之外，把 RAG 服务的
入库、分类治理、删除等管理接口开放给 Agent 使用。

删除类操作不可逆，标记 requires_approval=True：
引擎会挂起调用并等待用户在前端授权后才真正执行。
"""
import requests
from ..base import BaseTool
from ...utils.config_loader import rag_auth_headers
# 复用 rag_search 的配置寻址结果，保证两组工具指向同一个 RAG 后端
from .rag_search import RAG_HOST, RAG_PORT, RAG_HEALTH_URL

RAG_BASE_URL = f"http://{RAG_HOST}:{RAG_PORT}"
RAG_INSERT_URL = f"{RAG_BASE_URL}/api/v1/document/insert"
RAG_CATEGORY_URL = f"{RAG_BASE_URL}/api/v1/category"
RAG_DOC_DELETE_URL = f"{RAG_BASE_URL}/api/v1/document/delete"
RAG_RECLASSIFY_URL = f"{RAG_BASE_URL}/api/v1/governance/reclassify"


def _probe_rag_alive() -> bool:
    """模块加载时探活一次；离线则本文件所有工具都不注册（与 rag_search 行为一致）"""
    try:
        return requests.get(RAG_HEALTH_URL, timeout=3).status_code == 200
    except Exception as e:
        print(f"[MCP-rag_admin] ⚠️ RAG 服务器 ({RAG_HOST}:{RAG_PORT}) 探活失败: {e}")
        return False


_RAG_ALIVE = _probe_rag_alive()


def _request(method: str, url: str, **kwargs) -> str:
    """统一的 HTTP 请求执行器：返回给 LLM 可读的结果或错误说明"""
    kwargs["headers"] = {**rag_auth_headers(), **(kwargs.get("headers") or {})}
    try:
        response = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
        response.raise_for_status()
        data = response.json()
        return data.get("msg", str(data)) if isinstance(data, dict) else str(data)
    except requests.ConnectionError:
        return (
            f"System Error: Unable to connect to the RAG server "
            f"(please check if {RAG_HOST}:{RAG_PORT} is running)."
        )
    except requests.HTTPError as e:
        # 4xx 多为服务端前置校验的教学式拒绝（如「分类不存在 + 现有目录」），
        # 必须把 detail 原文透传给模型供其自纠，不能吞成笼统的报错
        detail = ""
        if e.response is not None:
            try:
                body = e.response.json()
                detail = body.get("detail") or body.get("msg") or str(body)
            except Exception:
                detail = e.response.text[:500]
        status = e.response.status_code if e.response is not None else "?"
        return f"RAG server rejected the request (HTTP {status}): {detail}"
    except Exception as e:
        return f"System Error: RAG request failed: {str(e)}"


def _fetch_categories() -> tuple:
    """拉取现有分类目录。返回 (成功与否, {db_name: description} 或错误文本)"""
    try:
        response = requests.get(RAG_CATEGORY_URL, timeout=10, headers=rag_auth_headers())
        response.raise_for_status()
        return True, {
            str(c.get("db_name")): str(c.get("description", ""))
            for c in response.json()
        }
    except Exception as e:
        return False, f"System Error: failed to fetch categories from the RAG server: {str(e)}"


def _format_catalog(categories: dict) -> str:
    if not categories:
        return "(no categories exist yet)"
    return "\n".join(f"- {name}: {desc}" for name, desc in categories.items())


class RAGListCategoriesTool(BaseTool):
    """列出知识库全部分类及路由描述"""
    name = "rag_list_categories"
    description = (
        "List all knowledge-base categories in the RAG database together with their routing "
        "descriptions. Use it before inserting documents or managing categories to see what "
        "already exists."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读列举分类，无副作用（写/删类 RAG 工具保持串行）
    parameters = {"properties": {}, "required": []}

    def is_available(self) -> bool:
        return _RAG_ALIVE

    def execute(self) -> str:
        try:
            response = requests.get(RAG_CATEGORY_URL, timeout=10, headers=rag_auth_headers())
            response.raise_for_status()
            categories = response.json()
        except Exception as e:
            return f"System Error: failed to list categories: {str(e)}"

        if not categories:
            return "The RAG database currently has no categories. Documents will go to the unclassified pool."

        lines = [f"Found {len(categories)} categories:"]
        for cat in categories:
            lines.append(f"- {cat.get('db_name', '?')}: {cat.get('description', '')}")
        return "\n".join(lines)


class RAGInsertDocumentTool(BaseTool):
    """向知识库指定分类入库一篇文档（分类必须已存在，工具前置校验 + 服务端硬校验双闸门）"""
    name = "rag_insert_document"
    description = (
        "Save a document into a SPECIFIC category of the RAG knowledge base (the server chunks "
        "and indexes it in the background). Standard flow — follow it in order: "
        "1) check existing categories with rag_list_categories; "
        "2) decide, based on the document's content, which category fits best; if none fits, "
        "first create one with rag_upsert_category (write a precise routing description); "
        "3) call this tool with that category. "
        "Calls with a non-existent category are REJECTED and the current catalog is returned — "
        "pick from it or create the category, then retry. "
        "To UPDATE an outdated document, simply re-insert the new content with the SAME title "
        "into the SAME category: the old version is replaced (its chunks removed from search, "
        "raw file archived) — no need to delete first."
    )
    requires_approval = False
    parameters = {
        "properties": {
            "title": {
                "type": "string",
                "description": "A short descriptive title for the document (used for backup naming)."
            },
            "document": {
                "type": "string",
                "description": "The full text content to store. Preserve the original wording; do not summarize."
            },
            "category": {
                "type": "string",
                "description": (
                    "Target category id — must already exist (see rag_list_categories). "
                    "Use 'unclassified_db' ONLY when the user explicitly does not care where it goes."
                )
            }
        },
        "required": ["title", "document", "category"]
    }

    def is_available(self) -> bool:
        return _RAG_ALIVE

    def execute(self, title: str, document: str, category: str) -> str:
        if not document.strip():
            return "System Error: document content is empty, nothing to insert."

        cat = (category or "").strip()
        # [前置校验] 目标分类必须真实存在：这是数据正确性的硬约束（规则是代码），
        # 校验失败返回现有目录与下一步指引，让模型经自纠闭环走上正确的工具链
        ok, categories = _fetch_categories()
        if not ok:
            return categories  # 无法核实目录时拒绝盲入库（fail-closed）
        if not cat:
            return (
                "Insert REJECTED: 'category' is required.\n"
                f"Existing categories:\n{_format_catalog(categories)}\n"
                "Next step: pick the best-matching category above based on the document's content "
                "and call rag_insert_document again; if none fits, first create one with "
                "rag_upsert_category, then insert."
            )
        if cat != "unclassified_db" and cat not in categories:
            return (
                f"Insert REJECTED: category '{cat}' does not exist.\n"
                f"Existing categories:\n{_format_catalog(categories)}\n"
                "Next step: pick the best-matching category above and call rag_insert_document "
                "again with it; if none truly fits, first create one with rag_upsert_category "
                "(write a specific routing description), then insert."
            )

        result = _request("POST", RAG_INSERT_URL,
                          json={"title": title, "document": document, "category": cat})
        return f"{result} (The document is processed asynchronously; it may take a moment before it becomes searchable.)"


class RAGUpsertCategoryTool(BaseTool):
    """创建或更新一个分类的路由描述"""
    name = "rag_upsert_category"
    description = (
        "Create a new category or update an existing category's routing description in the RAG "
        "database. Use this BEFORE rag_insert_document when no existing category fits the "
        "document (check with rag_list_categories first — prefer reusing an existing category "
        "over creating near-duplicates). db_name is the unique category id; description "
        "determines how documents and queries are routed to this category, so make it specific "
        "and information-dense."
    )
    requires_approval = False
    parameters = {
        "properties": {
            "db_name": {
                "type": "string",
                "description": "Unique category id, e.g. 'python_programming'. Use lowercase letters, digits and underscores."
            },
            "description": {
                "type": "string",
                "description": "Routing description of what belongs in this category (topics, keywords, typical documents)."
            }
        },
        "required": ["db_name", "description"]
    }

    def is_available(self) -> bool:
        return _RAG_ALIVE

    def execute(self, db_name: str, description: str) -> str:
        return _request("POST", RAG_CATEGORY_URL, json={"db_name": db_name, "description": description})


class RAGDeleteCategoryTool(BaseTool):
    """删除一个分类的路由条目（敏感操作，需用户授权）"""
    name = "rag_delete_category"
    description = (
        "Delete a category's routing entry from the RAG database. The vector data inside the "
        "category is kept but will no longer be routed to, and the routing description is lost. "
        "When the user asks for this, call the tool directly — do NOT ask for permission in "
        "conversation. The system automatically shows the user a confirmation dialog before "
        "execution, and the tool response will tell you whether it was executed or denied."
    )
    requires_approval = True
    parameters = {
        "properties": {
            "db_name": {
                "type": "string",
                "description": "The category id to delete. Check with rag_list_categories first."
            }
        },
        "required": ["db_name"]
    }

    def is_available(self) -> bool:
        return _RAG_ALIVE

    def execute(self, db_name: str) -> str:
        return _request("DELETE", f"{RAG_CATEGORY_URL}/{db_name}")


class RAGDeleteDocumentTool(BaseTool):
    """按 source_path 跨库整篇删除文档（不可逆敏感操作，需用户授权）"""
    name = "rag_delete_document"
    description = (
        "Delete an entire document from the RAG knowledge base: all its chunks across every "
        "category database are removed from search, and the raw backup file is moved to a "
        "recoverable trash folder (storage/.trash). Identify the document by its "
        "source_path, which appears as 'Path: ...' in rag_search results. When the user asks for "
        "this, call the tool directly — do NOT ask for permission in conversation. The system "
        "automatically shows the user a confirmation dialog before execution, and the tool "
        "response will tell you whether it was executed or denied."
    )
    requires_approval = True
    parameters = {
        "properties": {
            "source_path": {
                "type": "string",
                "description": "The document's backup path from search-result metadata ('Path: ...'). Must be an exact match."
            }
        },
        "required": ["source_path"]
    }

    def is_available(self) -> bool:
        return _RAG_ALIVE

    def execute(self, source_path: str) -> str:
        return _request("DELETE", RAG_DOC_DELETE_URL, json={"source_path": source_path})


class RAGReclassifyTool(BaseTool):
    """触发未分类文档的后台重分拣"""
    name = "rag_reclassify_unclassified"
    description = (
        "Trigger background re-classification of all documents currently sitting in the "
        "unclassified pool of the RAG database. Use it after creating new categories with "
        "rag_upsert_category so previously unclassified documents get sorted into them."
    )
    requires_approval = False
    parameters = {"properties": {}, "required": []}

    def is_available(self) -> bool:
        return _RAG_ALIVE

    def execute(self) -> str:
        result = _request("POST", RAG_RECLASSIFY_URL)
        return f"{result} (Re-classification runs in the background and may take a while.)"
