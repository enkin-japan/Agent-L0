"""
长期记忆沉淀汇（Stage 3）。

作用：压缩把工作记忆变小，沉淀保证「别真丢信息」——被摘要压缩折叠掉的对话内容，
在折叠的同时原文沉淀进 RAG 的专用类目 agent_memory，作为压缩的**无损承重网**：
上层对话摘要允许有损，被丢的细节日后仍可经 rag_search（agent_memory 类目）召回。

设计取舍：
- 系统级功能，非模型工具，直连 RAG 服务（HTTP）。不额外调用大模型：沉淀的是折叠轮次的
  原文文本（rag_search 侧由 RAG 服务分块索引），零推理成本、内容无损。
- fail-open：RAG 离线或出错一律静默跳过（返回 False），绝不影响压缩主流程与对话可用性。
- 类目 agent_memory 幂等 upsert：首次沉淀或启动时确保存在；应在 MCPRegistry 初始化前
  于启动阶段 ensure 一次，好让 rag_search 在启动探测类目时就能把它列入可选召回类目。
"""
import requests

from ..utils.config_loader import load_section, rag_auth_headers

# 与 rag_search 一致地从仓库根 config.yaml 寻址 RAG 后端（保持同一后端）
_rag_cfg = load_section("rag")
RAG_HOST = _rag_cfg.get("host", "127.0.0.1")
RAG_PORT = _rag_cfg.get("port", 14160)
_BASE_URL = f"http://{RAG_HOST}:{RAG_PORT}"

MEMORY_CATEGORY = "agent_memory"
MEMORY_CATEGORY_DESC = (
    "The agent's long-term conversational memory: user preferences and identity, past "
    "requests and conclusions, and earlier conversation details folded away by context "
    "compaction. Search this category to recall things the user said or did before, or "
    "old information missing from the current context."
)

_category_ready = False


def ensure_memory_category() -> bool:
    """幂等确保 agent_memory 类目存在。成功后置位缓存，避免每次沉淀都打一次网络。"""
    global _category_ready
    if _category_ready:
        return True
    try:
        resp = requests.post(f"{_BASE_URL}/api/v1/category",
                             json={"db_name": MEMORY_CATEGORY, "description": MEMORY_CATEGORY_DESC},
                             headers=rag_auth_headers(), timeout=5)
        resp.raise_for_status()
        _category_ready = True
        return True
    except Exception as e:
        print(f"[RAGSink] 确保 agent_memory 类目失败（跳过沉淀）: {e}")
        return False


def sediment(text: str, title: str) -> bool:
    """
    把一段（被折叠掉的）对话文本沉淀进 agent_memory 类目。
    fail-open：任何失败均静默返回 False，不抛出、不影响调用方的压缩流程。
    """
    if not text.strip():
        return False
    if not ensure_memory_category():
        return False
    try:
        resp = requests.post(f"{_BASE_URL}/api/v1/document/insert",
                             json={"title": title, "document": text, "category": MEMORY_CATEGORY},
                             headers=rag_auth_headers(), timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[RAGSink] 沉淀失败（已跳过，不影响压缩）: {e}")
        return False
