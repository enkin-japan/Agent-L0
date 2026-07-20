"""RAG 数据面服务（:14160）——知识库的唯一真相持有者。

数据/计算拆分（HETERO_COMPUTE B2c）：本进程管 Chroma 向量库、原文备份、分类路由、
切块、治理与检索编排；嵌入/重排/分词四则计算原语按 config rag.compute.host 分派：

  - 融合模式（compute.host 为空）：进程内 import compute_core 就地加载模型
    ——单机 all-in-one 现状，行为与拆分前逐字一致；
  - 拆分模式（compute.host 非空）：跨机拨号算力端的 compute_server（:14162），
    本进程不 import torch/transformers。用户数据（chroma_data / storage）从此
    只落工作端，算力端完全无状态（隐私边界与会话数据同一条纪律，§3.1）。

拆分模式的就绪语义：数据面启动**不依赖**计算面在线（工作端开机不被算力端卡死）；
纯数据操作（列类/删除）恒可用，需要计算的操作在计算面不可达时返回 503、恢复即自愈。
换档护栏在首次拿到计算面身份时比对存储戳，不符即拒绝服务并指向 reembed。

环境变量：
  RAG_CONFIG      config.yaml 路径覆盖（缺省 = 仓库根；bundle/测试布局注入）
  RAG_DATA_DIR    数据根覆盖（chroma_data/ 与 storage/ 的父目录；缺省 = rag/ 代码目录，
                  仓库布局现状。bundle 布局注入 app 数据根——bundle 本体只读）
"""
import hmac
import json
import logging
import os
import time
import uuid
import threading

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Dict
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Request, Header, Depends
from pydantic import BaseModel, Field
import chromadb
import requests
import yaml

from retrieval_extras import extract_identifiers, mmr_select

# ==========================================
# 全局配置与数据根
# ==========================================
BASE_WORKSPACE = Path(__file__).parent
# 配置统一在仓库根（backend 与 rag 共用一份 config.yaml；rag 读其 rag/tuning 段）
_cfg_override = os.environ.get("RAG_CONFIG", "").strip()
CONFIG_PATH = Path(_cfg_override) if _cfg_override else BASE_WORKSPACE.parent / "config.yaml"

# 数据根：用户数据（向量库 + 原文备份）所在。仓库布局 = rag/ 本目录（现状）；
# bundle/测试布局经 RAG_DATA_DIR 注入（bundle 的 Resources 只读，数据必须落数据根）。
DATA_ROOT = Path(os.environ.get("RAG_DATA_DIR", "").strip() or BASE_WORKSPACE)

# 【工程补丁】预先创建物理隔离的备份文件夹
STORAGE_BASE = DATA_ROOT / "storage"
RAW_FILES_DIR = STORAGE_BASE / "raw_files"
UNCLASSIFIED_DIR = STORAGE_BASE / "unclassified_raw"
RAW_FILES_DIR.mkdir(parents=True, exist_ok=True)
UNCLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)

with CONFIG_PATH.open("r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

RAG_CFG = config["rag"]
RAG_HOST = RAG_CFG["host"]
RAG_PORT = RAG_CFG["port"]

# 计算面分派：compute.host 空 = 融合（就地加载模型）；非空 = 拆分（HTTP 拨号算力端）
COMPUTE_CFG = RAG_CFG.get("compute") or {}
# env 覆盖=运行时改拓扑不必重打包/改 config：非空即赢，覆盖 config 里烘死的算力端拨号地址
COMPUTE_HOST = os.environ.get("RAG_COMPUTE_HOST", "").strip() or (COMPUTE_CFG.get("host") or "").strip()
COMPUTE_PORT = int(COMPUTE_CFG.get("port") or 14162)
SPLIT_MODE = bool(COMPUTE_HOST)

# ── 阈值：tuning 全局兜底立即生效；档位 thresholds 随计算面身份到位时覆盖 ──────
# （余弦分布随模型尺寸变，三阈值须逐档标定、跟档位走。融合模式启动即覆盖；拆分模式
# 首次连上计算面才知道档位——反正在那之前任何用到阈值的操作也都做不了。）
tuning = config.get("tuning") or {}
_THR_DEFAULTS = {
    "insert_routing_max_distance": 0.8,
    "query_routing_max_distance": 0.85,
    "rerank_min_score": 0.5,
}
THRESHOLDS: Dict[str, float] = {k: tuning.get(k, d) for k, d in _THR_DEFAULTS.items()}


def _apply_tier_thresholds(tier_thr: Optional[dict]) -> None:
    for k in _THR_DEFAULTS:
        if tier_thr and k in tier_thr:
            THRESHOLDS[k] = tier_thr[k]


QUERY_MAX_DBS = tuning.get("query_max_dbs", 3)
QUERY_MAX_CHUNKS = tuning.get("query_max_chunks", 4)
QUERY_FINAL_MAX_RESULTS = tuning.get("query_final_max_results", 3)
CHUNK_MAX_TOKENS = tuning.get("chunk_max_tokens", 256)
CHUNK_OVERLAP_TOKENS = tuning.get("chunk_overlap_tokens", 128)

# ── 鉴权（HETERO_COMPUTE B2a，照抄 backend api/auth 的常量时间比对）───────────
# RAG 独立进程/venv，不 import backend，故就地实现。密钥走 env RAG_API_KEY（.env.local，
# devctl source，两侧同值）；留空 = 未启用（启动告警后放行，仅限开发/回环）。
RAG_API_KEY = os.environ.get("RAG_API_KEY", "")

CHROMA_DB_PATH = DATA_ROOT / "chroma_data"
# 换档护栏：建库时给存储盖戳（嵌入模型身份+维度），换档维度不符即快速失败提示先重建
TIER_STAMP_FILE = CHROMA_DB_PATH / ".tier_stamp.json"

thread_local_storage = threading.local()

# 常驻查询线程池：线程存活则 thread_local 的 Chroma 客户端可被复用。
# 此前每个查询请求临时新建 ThreadPoolExecutor——新线程上 thread_local 永远未命中，
# 等于每库每请求都重建一次 PersistentClient，纯粹的性能税。
QUERY_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rag-query")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RAG_Server")


# ==========================================
# 计算面客户端（同一契约两个实现：进程内 / HTTP——镜像 InProcess/RemoteBackend）
# ==========================================
class ComputeUnavailable(Exception):
    """计算面暂不可达（拆分模式）：对外表现为 503，计算面恢复后自愈。"""


class _LocalCompute:
    """融合模式：进程内加载模型（compute_core），行为与拆分前一致。"""
    mode = "fused"

    def __init__(self):
        self.identity: Optional[dict] = None
        self.core = None

    def start(self) -> None:
        try:
            import compute_core as core   # 仅融合模式 import（拆分模式的数据面不背 torch）
        except ImportError as e:
            # ModuleNotFoundError 是 ImportError 子类，一并捕获。bundle 形态只随
            # rag_server.py（torch 不进包）：rag.compute.host 空即融合（本地）模式，
            # 在该布局天然缺依赖——换成友好拒启，指向拆分部署。
            raise SystemExit(
                "❌ [rag] 拒绝启动：rag.compute.host 为空即融合（本地）模式，但本机缺少计算面依赖\n"
                "   （torch/compute_core——bundle 形态只支持拆分部署）。\n"
                "   请填写算力端地址：app 形态在设置 → 执行层 → 算力端地址；dev 形态 .env.local 写\n"
                "   RAG_COMPUTE_HOST=<算力端 tailnet IP>。\n"
                f"   （底层导入错误：{e}）")
        self.core = core
        core.load_models()
        self.identity = core.identity()

    def ensure_ready(self) -> dict:
        return self.identity          # 启动即加载完成，恒就绪

    def embed(self, texts: List[str]) -> List[List[float]]:
        return self.core.embed_texts(texts)

    def token_counts(self, texts: List[str], model_type: str = "embedding") -> List[int]:
        return self.core.token_counts(texts, model_type)

    def rerank(self, query: str, documents: List[str]) -> List[float]:
        return self.core.rerank_scores(query, documents)

    def stop(self) -> None:
        if self.core:
            self.core.unload_models()


class _RemoteCompute:
    """拆分模式：跨机拨号算力端 compute_server。懒就绪——启动时探一次（不可达只告警），
    此后每次需要计算时确保身份已核验（换档护栏）；不可达抛 ComputeUnavailable → 503。"""
    mode = "split"

    def __init__(self, host: str, port: int):
        self.base_url = f"http://{host}:{port}"
        self.identity: Optional[dict] = None
        self._fatal: Optional[str] = None      # 换档不符/密钥不一致等配置性错误（非瞬断）
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        key = os.environ.get("RAG_COMPUTE_API_KEY", "")
        return {"Authorization": f"Bearer {key}"} if key else {}

    def start(self) -> None:
        try:
            self.ensure_ready()
            ident = self.identity or {}
            logger.info(f"🔗 计算面在线：{self.base_url} 档位 {ident.get('tier')} / dim={ident.get('embed_dim')}")
        except HTTPException as e:
            if e.status_code == 503:
                logger.warning(f"⚠️ 计算面暂不可达（{self.base_url}）：数据面照常启动，"
                               f"检索/入库将在计算面上线后自愈。")
            else:
                # 换档不符等配置性错误：启动时就知道的话立即拒启（与融合模式护栏同纪律）
                raise SystemExit(str(e.detail))

    def ensure_ready(self) -> dict:
        with self._lock:
            if self._fatal:
                raise HTTPException(status_code=500, detail=self._fatal)
            if self.identity is not None:
                return self.identity
            try:
                r = requests.get(f"{self.base_url}/health", timeout=5)
                r.raise_for_status()
                ident = r.json()
            except Exception as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"RAG 计算面不可达（{self.base_url}）：{e}。"
                           f"请确认算力端已运行 compute_server（./devctl.sh start --profile gpu）。")
            # 换档护栏：拿到身份的第一时间与存储戳比对；不符 = 配置性错误，缓存为 fatal
            try:
                _guard_or_stamp_store(ident)
            except RuntimeError as e:
                self._fatal = str(e)
                raise HTTPException(status_code=500, detail=self._fatal)
            _apply_tier_thresholds(ident.get("thresholds"))
            self.identity = ident
            return ident

    def _post(self, path: str, payload: dict, timeout: int) -> dict:
        self.ensure_ready()
        try:
            r = requests.post(f"{self.base_url}{path}", json=payload,
                              headers=self._headers(), timeout=timeout)
        except Exception as e:
            raise HTTPException(status_code=503,
                                detail=f"RAG 计算面请求失败（{self.base_url}{path}）：{e}")
        if r.status_code == 401:
            # 密钥不一致是配置错误，不是瞬断——说清指向，别让调用方当成网络问题重试
            raise HTTPException(status_code=500,
                                detail="RAG_COMPUTE_API_KEY 与计算面不一致（401）：检查两侧 .env.local 同值。")
        r.raise_for_status()
        return r.json()

    def embed(self, texts: List[str]) -> List[List[float]]:
        # 大语料整单发来（reembed 全量重灌）时嵌入耗时可观：超时给足
        return self._post("/api/v1/embed", {"texts": texts}, timeout=900)["embeddings"]

    def token_counts(self, texts: List[str], model_type: str = "embedding") -> List[int]:
        return self._post("/api/v1/tokenize", {"texts": texts, "model_type": model_type},
                          timeout=120)["counts"]

    def rerank(self, query: str, documents: List[str]) -> List[float]:
        return self._post("/api/v1/rerank", {"query": query, "documents": documents},
                          timeout=300)["scores"]

    def stop(self) -> None:
        pass


COMPUTE = _RemoteCompute(COMPUTE_HOST, COMPUTE_PORT) if SPLIT_MODE else _LocalCompute()


# ==========================================
# 换档护栏（存储戳）
# ==========================================
def _guard_or_stamp_store(ident: dict) -> None:
    """比对存储戳与计算面身份（档位/维度）。

    - 有戳且维度/模型不符 → 快速失败（RuntimeError），提示先跑 reembed.py 重建。
    - 无戳（新库 / 档位化之前的旧库）→ 采纳当前身份盖戳。旧库若曾用别的档位建（无戳无从
      得知），首个 add/query 会在 chroma 层撞维度错误，由入库/检索处包成同样的"先重建"提示兜底。
    """
    runtime_dim = ident.get("embed_dim")
    embed_repo = ident.get("embed_repo")
    try:
        stamp = json.loads(TIER_STAMP_FILE.read_text(encoding="utf-8"))
    except Exception:
        stamp = None

    if stamp:
        if int(stamp.get("embed_dim", -1)) != int(runtime_dim) or stamp.get("embed_repo") != embed_repo:
            raise RuntimeError(
                f"❌ RAG 换档护栏：向量库是用档位 '{stamp.get('tier_id')}'"
                f"（{stamp.get('embed_repo')}, dim={stamp.get('embed_dim')}）建立的，"
                f"而当前计算面是 '{ident.get('tier')}'（{embed_repo}, dim={runtime_dim}）。\n"
                f"   换档必须重建向量库：先跑  python rag/scripts/reembed.py  从 storage/raw_files "
                f"全量重灌，或改回原档位。（不变量：库一旦入库，档位即固定。）"
            )
        return

    CHROMA_DB_PATH.mkdir(parents=True, exist_ok=True)
    TIER_STAMP_FILE.write_text(json.dumps({
        "tier_id": ident.get("tier"),
        "modality": ident.get("modality"),
        "embed_repo": embed_repo,
        "rerank_repo": ident.get("rerank_repo"),
        "embed_dim": int(runtime_dim),
        "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info(f"🏷️ 向量库盖戳：档位 {ident.get('tier')} / {embed_repo} / dim={runtime_dim}")


# ==========================================
# 鉴权 + 多用户埋点（HETERO_COMPUTE B2a / D3）
# ==========================================
async def verify_rag_key(request: Request, authorization: Optional[str] = Header(None)) -> None:
    """全局 Bearer 校验（挂在 FastAPI app 依赖上）。/health 与 docs 自豁免；空 key=放行（dev）。"""
    path = request.url.path
    if path == "/health" or path in ("/openapi.json", "/redoc") or path.startswith("/docs"):
        return
    if not RAG_API_KEY:
        return  # 未配置密钥 → 放行（启动时已告警）
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少或格式错误的 Authorization 头（应为 'Bearer <key>'）。")
    provided = authorization[len("Bearer "):]
    # encode 后比对：compare_digest 对含非 ASCII 的 str 会抛 TypeError，bytes 恒安全
    if not hmac.compare_digest(provided.encode("utf-8"), RAG_API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="RAG API 密钥无效。")


# 多用户命名空间**埋点**（D3）：只定约定与参数面，**不实现多用户逻辑**。
# 约定（B2 全量落地时启用）：collection 命名 u_{user_id}__{category}、原文
#   storage/raw_files/{user_id}/{类}、meta 路由库按 user 分库。当前后端不发 X-Rag-User 头，
#   一律取默认 "default"，_scoped_collection 对默认用户返回原名——行为与现状全同。
def _current_user(x_rag_user: Optional[str] = Header(None)) -> str:
    return (x_rag_user or "default").strip() or "default"


def _scoped_collection(user_id: str, name: str) -> str:
    """按 D3 约定给 collection 名加 user 命名空间。默认用户 = 原名（当前唯一路径）。"""
    if not user_id or user_id == "default":
        return name
    return f"u_{user_id}__{name}"


# ==========================================
# 核心工具函数
# ==========================================
def get_chroma_client():
    """获取当前线程专属的 ChromaDB 客户端"""
    if not hasattr(thread_local_storage, "client"):
        thread_local_storage.client = chromadb.PersistentClient(path=str(CHROMA_DB_PATH))
    return thread_local_storage.client

def get_chroma_collection(collection_name: str):
    """动态获取或创建指定分类的 Collection（统一使用余弦距离，与路由阈值匹配）"""
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

def get_embeddings(texts: List[str]) -> List[List[float]]:
    """向量提取（经计算面：融合=进程内，拆分=HTTP）。分批责任在计算侧。"""
    return COMPUTE.embed(texts)

def count_tokens(text: str, model_type: str = "embedding") -> int:
    return COMPUTE.token_counts([text], model_type)[0]

def split_text_to_chunks(text: str, max_tokens: int = None, overlap_tokens: int = None) -> List[str]:
    """
    工业级 Token 感知切块器
    :param doc_tuples: list of tuple (line_text, token_count)
    :param max_tokens: 每个 Chunk 的最大 Token 数
    :param overlap_tokens: 触发重叠的 Token 阈值
    """
    if max_tokens is None:
        max_tokens = CHUNK_MAX_TOKENS
    if overlap_tokens is None:
        overlap_tokens = CHUNK_OVERLAP_TOKENS

    logger.info("正在分行计算入库数据的token数")
    lines = [t for t in text.splitlines(keepends=True) if t.strip()]
    if not lines:
        return []
    # 批量 tokenize：一次调用算完全部行（此前逐行 encode，大文档是 O(行数) 次调用，慢一个量级；
    # 拆分模式下这同时是「一次 HTTP 往返」而非每行一次）
    doc = list(zip(lines, COMPUTE.token_counts(lines)))
    logger.info("token数计算完成")

    chunks = []
    current_chunk_text = ""
    current_chunk_tc = 0

    # 用一个队列来暂存当前 Chunk 中的行，方便用于计算下一块的 Overlap
    current_lines = []

    for line, tc in doc:
        # 【Bug修复 1】安全带：如果单行直接超标，强行切断或单独成块
        if tc >= max_tokens:
            logger.warning(f"⚠️ 发现超长单行 (Token: {tc})，将其独立成块，可能需要二次切片。")
            if current_chunk_text:
                chunks.append(current_chunk_text)
            chunks.append(line)
            # 清空当前状态
            current_chunk_text, current_chunk_tc, current_lines = "", 0, []
            continue

        # 如果当前块加上这行会超标，说明当前块“满了”
        if current_chunk_tc + tc > max_tokens:
            chunks.append(current_chunk_text)

            # 【核心逻辑重构】：生成 Overlap 块
            # 回溯 current_lines，保留末尾的行，直到凑够约 overlap_tokens 的容量
            overlap_lines = []
            overlap_tc = 0
            for prev_line, prev_tc in reversed(current_lines):
                if overlap_tc + prev_tc <= overlap_tokens:
                    overlap_lines.insert(0, (prev_line, prev_tc))
                    overlap_tc += prev_tc
                else:
                    break

            # 开启新的一块，把 overlap 的内容作为前置上下文带入
            current_lines = overlap_lines + [(line, tc)]
            current_chunk_text = "".join(l for l, _ in current_lines)
            current_chunk_tc = overlap_tc + tc

        else:
            # 还能装下，继续装
            current_chunk_text += line
            current_chunk_tc += tc
            current_lines.append((line, tc))

    # 收尾：把最后没满的一块也加进去
    if current_chunk_text:
        chunks.append(current_chunk_text)

    # 【Bug修复 2】无需关心 1000 的上限，chunks 列表自动扩容
    return chunks


# ==========================================
# 后台任务：入库流水线
# ==========================================
def _collection_of_raw_file(path: Path) -> str:
    """原文备份所在目录 = 它当初入的库（unclassified_raw/ → unclassified_db；raw_files/{类}/ → {类}）。"""
    return "unclassified_db" if path.parent == UNCLASSIFIED_DIR else path.parent.name


VERSIONS_DIR = ".versions"   # 旧版原文归档子目录（记忆治理波 U1）；点开头+子目录双保险，
                             # reembed 的 glob("*.txt") 与 reclassify 的点前缀过滤都扫不进它


def _replace_previous_versions(category: str, doc_title: str) -> None:
    """替换式导入（U1）：同分类+同标题 = 新版本。

    旧切片清出向量库（不再被检索命中），旧原文移入同目录 .versions/ 归档——
    「过时」≠「没用」（旧版合同类内容仍可手工找回），归档先行与 dream 备份同哲学。
    fail-open：清不掉就照常入库，最坏退回 U1 之前的「新旧并存」，不阻断入库主流程。
    """
    try:
        col = get_chroma_collection(category)
        matched = col.get(where={"title": doc_title})
    except Exception as e:
        logger.warning(f"⚠️ 替换式导入查旧版失败（跳过替换，照常入库）: {e}")
        return
    ids = matched.get("ids") or []
    if not ids:
        return
    old_paths = {m.get("source_path") for m in (matched.get("metadatas") or [])
                 if m and m.get("source_path")}
    try:
        col.delete(ids=ids)
    except Exception as e:
        logger.warning(f"⚠️ 替换式导入清旧切片失败（跳过替换，照常入库）: {e}")
        return
    stamp = time.strftime("%Y%m%d%H%M%S")
    archived = 0
    for sp in old_paths:
        p = Path(sp)
        try:
            # 与 delete 端点同一守卫：只动 STORAGE_BASE 之内的文件
            if p.is_file() and p.resolve().is_relative_to(STORAGE_BASE.resolve()):
                dest_dir = p.parent / VERSIONS_DIR
                dest_dir.mkdir(parents=True, exist_ok=True)
                p.rename(dest_dir / f"{stamp}_{p.name}")
                archived += 1
        except Exception as e:
            logger.warning(f"⚠️ 旧版原文归档失败（切片已清，文件留在原处）: {sp}: {e}")
    logger.info(f"♻️ 替换式导入: [{doc_title}] 在 '{category}' 的旧版已让位"
                f"（清 {len(ids)} 切片，归档 {archived} 文件）")


def auto_classify_and_insert(raw_document: str, doc_title: str, forced_category: Optional[str] = None,
                             existing_source_path: Optional[str] = None):
    """入库流水线。existing_source_path 非空 = 原文备份**已在盘上**（reembed 重建场景）：
    复用该文件而非另写一份，见步骤 4。"""
    logger.info(f"🚀 开始处理文档: {doc_title}")

    if forced_category:
        # 调用方（如 Agent 按「查目录→选类/建类→入库」流程）已显式指定目标分类，
        # 跳过向量自动路由；存在性校验由接口层完成
        target_category = forced_category
        logger.info(f"📌 文档 [{doc_title}] 使用调用方指定分类: {target_category}")
    else:
        # 1. 计算文档级特征
        doc_feature_text = f"Title: {doc_title}\n\n{raw_document[:500]}"
        doc_embedding = get_embeddings([doc_feature_text])[0]

        # 2. 查询分类路由 (Meta DB)
        meta_db = get_chroma_collection("meta_classification_db")
        target_category = "unclassified_db"

        try:
            if meta_db.count() > 0:
                routes = meta_db.query(query_embeddings=[doc_embedding], n_results=1)
                # Chroma 距离越小越接近
                if routes['distances'] and routes['distances'][0]:
                    top_distance = routes['distances'][0][0]
                    if top_distance < THRESHOLDS["insert_routing_max_distance"]:
                        target_category = routes['ids'][0][0]
                        logger.info(f"🎯 文档 [{doc_title}] 成功匹配到分类: {target_category} (距离: {top_distance:.4f})")
                    else:
                        logger.warning(f"⚠️ 相似度不足 (距离 {top_distance:.4f} > 阈值 {THRESHOLDS['insert_routing_max_distance']})，流入待分类库。")
        except Exception as e:
            logger.error(f"分类查询失败，默认进入待分类库。原因: {e}")

    # 3. 文本切片（先于备份：空文档直接返回，不在磁盘留下无切片指向的孤儿备份文件）
    chunks = split_text_to_chunks(raw_document)
    total_chunks = len(chunks)
    if total_chunks == 0:
        logger.warning(f"⚠️ 文档 [{doc_title}] 内容为空，跳过入库（不写备份）。")
        return

    # 3.5 替换式导入（U1）：同分类+同标题的旧版让位（清旧切片+原文归档 .versions/）。
    #     仅新入库触发——reembed（existing_source_path）是重建派生物，另有同路径清理（步骤 5）。
    if not existing_source_path:
        _replace_previous_versions(target_category, doc_title)

    # 4. 备份原始文件
    #    不变量：raw_files 是原文的**唯一真相**，向量库是它的派生物（reembed 据此重建）。
    #    故"从原文重灌"绝不能再写一份原文——existing_source_path 即调用方声明"备份已在盘上，
    #    就地索引"。没有它时 reembed 每跑一遍原文语料翻一倍（实测 2 篇→4 篇，见 §S.3 B2b）。
    if existing_source_path:
        original_path = Path(existing_source_path)   # 移动前的路径：旧切片按它清理（见步骤 5）
        backup_path = original_path
        # 路由到了与所在目录不符的分类（未分类池重灌时会发生）：移动而非复制，保持单份
        expected_dir = (UNCLASSIFIED_DIR if target_category == "unclassified_db"
                        else RAW_FILES_DIR / target_category)
        if backup_path.parent != expected_dir:
            expected_dir.mkdir(parents=True, exist_ok=True)
            moved = expected_dir / backup_path.name
            backup_path.rename(moved)
            logger.info(f"📁 原文备份随路由迁移: {backup_path} → {moved}")
            backup_path = moved
    else:
        safe_title = "".join(c for c in doc_title if c.isalnum() or c in " _-").strip()
        file_name = f"{safe_title}_{uuid.uuid4().hex[:8]}.txt"

        if target_category == "unclassified_db":
            backup_path = UNCLASSIFIED_DIR / file_name
        else:
            cat_dir = RAW_FILES_DIR / target_category
            cat_dir.mkdir(parents=True, exist_ok=True)
            backup_path = cat_dir / file_name

        with backup_path.open("w", encoding="utf-8") as f:
            f.write(raw_document)

    # 5 & 6. 计算块级向量并入库
    target_db = get_chroma_collection(target_category)

    # 就地重索引（reembed）= 同一篇原文再入一次：先清掉它的旧切片，否则重复入库。
    # 让 rebuild 可安全重跑——大语料迁移中途失败后操作员必然重跑（正是最需要它的时刻）。
    # 普通入库不走此路（每篇都是新 uuid 路径，无从冲突），故 Agent 侧行为不变。
    # 两处都要清：①**原路径所在的旧库**——重灌未分类池时路由可能变（doc 从 unclassified_db
    #   迁到某分类），旧切片会滞留在旧库里指向已移走的文件（实测同一篇命中两次）；照
    #   reclassify_background_task 的既有惯例先按原路径删。②目标库的同路径（同类重跑去重）。
    if existing_source_path:
        for cat, sp in {(_collection_of_raw_file(original_path), str(original_path)),
                        (target_category, str(backup_path))}:
            try:
                get_chroma_collection(cat).delete(where={"source_path": sp})
            except Exception as e:
                logger.warning(f"⚠️ 重索引前清理旧切片失败（库 {cat}，可能本就没有）: {e}")

    # 整单交给计算面（分批责任在计算侧：融合=就地循环，拆分=一次 HTTP 往返）
    all_embeddings = get_embeddings(chunks)

    # 【核心：Metadata 注入原始文件路径】(Chroma metadata 仅支持标量，路径需转 str)
    # created_at（记忆治理波 P2a）：入库时刻，ISO 串可按字典序排序、可直接进提示词。
    # 重索引（existing_source_path）取原文件 mtime——reembed 只是重建派生物，不许刷新年龄。
    try:
        created_ts = backup_path.stat().st_mtime if existing_source_path else time.time()
    except OSError:
        created_ts = time.time()
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(created_ts))
    ids = [str(uuid.uuid4()) for _ in chunks]
    metadatas = [{"source_path": str(backup_path), "title": doc_title,
                  "created_at": created_at} for _ in chunks]

    try:
        target_db.add(embeddings=all_embeddings, documents=chunks, ids=ids, metadatas=metadatas)
    except Exception as e:
        # 换档护栏兜底：库是别的维度建的、当前档位维度不符（启动戳未拦住的旧库无戳边缘情形）
        if "dimension" in str(e).lower():
            ident = COMPUTE.identity or {}
            logger.error(
                f"❌ 入库维度不匹配：库 '{target_category}' 与当前档位 "
                f"'{ident.get('tier')}'(dim={ident.get('embed_dim')}) 冲突——换档后未重建。"
                f"请跑  python rag/scripts/reembed.py  全量重建，或改回原档位。原始错误: {e}")
        raise
    logger.info(f"✅ 入库完成: [{doc_title}] -> {target_category} ({total_chunks} 个片段)")

def reclassify_background_task():
    """扫描未分类文件夹并重新入库"""
    logger.info("🔄 启动离线数据治理：重新分拣未分类文件...")
    files = [f for f in UNCLASSIFIED_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        logger.info("📂 未分类文件夹为空，无需治理。")
        return

    for file in files:
        with file.open("r", encoding="utf-8") as f:
            content = f.read()

        # 根据文件名还原大致标题
        title = file.name.rsplit("_", 1)[0]

        # 将原 unclassified_db 里的切片根据路径全删掉
        unclass_db = get_chroma_collection("unclassified_db")
        try:
            unclass_db.delete(where={"source_path": str(file)})
        except Exception:
            pass

        # 重新走自动化入库流程
        auto_classify_and_insert(content, title)

        # 清理旧原始文件
        file.unlink(missing_ok=True)
    logger.info("🎉 重新分拣任务执行完毕！")

# ==========================================
# FastAPI 生命周期与应用定义
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not RAG_API_KEY:
        logger.warning("⚠️ RAG_API_KEY 未设置：鉴权未启用（放行）——仅限开发/回环，跨机部署必须设置。")
    logger.info(f"📦 数据根: {DATA_ROOT}  模式: {COMPUTE.mode}"
                + (f"（计算面 {COMPUTE_HOST}:{COMPUTE_PORT}）" if SPLIT_MODE else ""))
    COMPUTE.start()
    if COMPUTE.mode == "fused":
        # 融合模式：模型已就地加载，护栏/阈值立即生效（与拆分前的启动时序一致）
        ident = COMPUTE.identity
        try:
            _guard_or_stamp_store(ident)
        except RuntimeError as e:
            raise SystemExit(str(e))
        _apply_tier_thresholds(ident.get("thresholds"))
        print(f"🚀 双模型 RAG 系统就绪（档位 {ident.get('tier')} / dim={ident.get('embed_dim')}）！")
    yield
    COMPUTE.stop()

# 全局 Bearer 依赖（verify_rag_key 自豁免 /health 与 docs；空 key=放行）
app = FastAPI(lifespan=lifespan, title="RAG 知识库 API", dependencies=[Depends(verify_rag_key)])

# ==========================================
# 模型与接口
# ==========================================
class InsertRequest(BaseModel):
    title: str
    document: str
    # 调用方显式指定的目标分类：必须已存在（或 unclassified_db），不存在则 400 拒绝并返回现有目录；
    # 缺省 None 时走原有的向量自动路由（低于阈值进未分类池）
    category: Optional[str] = None
    # 重建专用（reembed.py）：声明"本文的原文备份已在盘上的此路径"，服务端**跳过备份写入**、
    # 直接以此为 source_path 元数据。缺省 None = 普通入库（照旧写一份新备份），Agent 侧不传。
    # 必须落在 storage/ 内（接口层校验），否则 400。
    source_path: Optional[str] = None

class QueryRequest(BaseModel):
    query: str                   # 原始提问
    category: str    # LLM 分析出的意图分类名
    optimized_query: str         # LLM 优化的检索句
    # 上限钳制：无界的 K 值会让重排一次性拼超大 batch，MPS 峰值内存无界
    top_k_dbs: int = Field(QUERY_MAX_DBS, ge=1, le=10)           # 最多选几个分类库并发
    chunk_k_per_db: int = Field(QUERY_MAX_CHUNKS, ge=1, le=20)      # 每个库捞多少条片段
    rerank_top_k: int = Field(QUERY_FINAL_MAX_RESULTS, ge=1, le=10)        # 最终输出几条

class CategoryItem(BaseModel):
    db_name: str
    description: str

@app.get("/health", tags=["基础监控"])
async def health_check():
    ident = COMPUTE.identity or {}
    payload = {
        "status": "healthy",
        "mode": COMPUTE.mode,
        "device": ident.get("device"),
        "tier": ident.get("tier"),
        "modality": ident.get("modality"),
        "embed_dim": ident.get("embed_dim"),
    }
    if SPLIT_MODE:
        payload["compute_url"] = COMPUTE.base_url
        payload["compute_reachable"] = COMPUTE.identity is not None
    return payload

@app.post("/tokenize", tags=["基础监控"])
async def token_counter(text: str = Body(embed=True), model_type: str = Body("embedding", embed=True)):
    return {"token_count": count_tokens(text, model_type)}

# ---------------- 分类治理 ----------------
@app.get("/api/v1/category", tags=["分类管理"])
async def list_categories():
    # 纯数据操作：计算面离线也可用（backend 启动探活/工具注册靠它，不能被算力端卡住）
    db = get_chroma_collection("meta_classification_db")
    data = db.get()
    return [{"db_name": id_, "description": doc} for id_, doc in zip(data["ids"], data["documents"])]

@app.post("/api/v1/category", tags=["分类管理"])
async def upsert_category(cat: CategoryItem):
    db = get_chroma_collection("meta_classification_db")
    emb = get_embeddings([cat.description])[0]
    db.upsert(ids=[cat.db_name], documents=[cat.description], embeddings=[emb])
    return {"msg": f"分类 {cat.db_name} 已创建/更新"}

@app.delete("/api/v1/category/{db_name}", tags=["分类管理"])
async def delete_category(db_name: str):
    db = get_chroma_collection("meta_classification_db")
    db.delete(ids=[db_name])
    return {"msg": f"分类 {db_name} 路由已删除（实际数据保留）"}

# ---------------- 数据浏览与真删（记忆治理波 P1：管理面板数据源） ----------------
TRASH_DIR = STORAGE_BASE / ".trash"   # 删除类操作归档先行（红线）：不 rm，移入回收目录


def _category_raw_dir(name: str) -> Path:
    return UNCLASSIFIED_DIR if name == "unclassified_db" else RAW_FILES_DIR / name


def _count_raw_files(name: str) -> int:
    d = _category_raw_dir(name)
    if not d.is_dir():
        return 0
    return sum(1 for f in d.iterdir() if f.is_file() and not f.name.startswith("."))


@app.get("/api/v1/document/list", tags=["数据治理"])
async def list_documents(category: Optional[str] = None):
    """管理面板数据源（P1）。不带 category = 分类总览；带 = 该分类的文档清单。

    纯数据操作：计算面离线也可用（与 list_categories 同理，面板不能被算力端卡住）。
    文档清单以向量库切片按 source_path 聚合，再并上盘上孤儿文件——原文目录是唯一
    真相，切片丢了文件还在的文档也必须让用户看见（chunks=0 即入库残缺的实症）。
    """
    if category is None:
        meta = get_chroma_collection("meta_classification_db")
        data = meta.get()
        cats = {id_: (doc or "") for id_, doc in zip(data["ids"], data["documents"])}
        cats.setdefault("unclassified_db", "自动路由未能归类的文档池")
        out = []
        for name in sorted(cats):
            try:
                chunk_count = get_chroma_collection(name).count()
            except Exception:
                chunk_count = 0
            out.append({"name": name, "description": cats[name],
                        "doc_count": _count_raw_files(name), "chunk_count": chunk_count})
        return {"categories": out}

    col = get_chroma_collection(category)
    docs: Dict[str, dict] = {}
    try:
        got = col.get(include=["metadatas"])
    except Exception as e:
        logger.warning(f"⚠️ 列举分类 {category} 切片失败（仅按盘上文件列举）: {e}")
        got = {}
    for m in got.get("metadatas") or []:
        sp = (m or {}).get("source_path")
        if not sp:
            continue
        d = docs.setdefault(sp, {"source_path": sp, "title": m.get("title") or Path(sp).stem,
                                 "created_at": "", "chunks": 0, "size_bytes": 0, "stale": ""})
        d["chunks"] += 1
        if m.get("created_at"):
            d["created_at"] = str(m["created_at"])
        if m.get("stale"):
            d["stale"] = str(m["stale"])
    raw_dir = _category_raw_dir(category)
    if raw_dir.is_dir():
        for f in raw_dir.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            d = docs.setdefault(str(f), {
                "source_path": str(f), "title": f.name.rsplit("_", 1)[0],
                "created_at": "", "chunks": 0, "size_bytes": 0, "stale": ""})
            try:
                st = f.stat()
                d["size_bytes"] = st.st_size
                if not d["created_at"]:   # 存量旧切片无戳：文件 mtime 兜底
                    d["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                                    time.localtime(st.st_mtime))
            except OSError:
                pass
    ordered = sorted(docs.values(), key=lambda d: d["created_at"], reverse=True)
    return {"category": category, "documents": ordered}


@app.delete("/api/v1/category/{db_name}/purge", tags=["数据治理"])
async def purge_category(db_name: str):
    """真删分类（P1）：向量 collection 整库丢弃 + 原文目录移入 .trash 回收 + 路由条目删除。

    与 DELETE /category/{name}（只删路由条目、数据保留）分工：那是「取消分拣路由」，
    这才是「删库」。原文不 rm——移入 storage/.trash/<时间戳>_<库名>/（归档先行红线）。
    """
    if db_name == "meta_classification_db":
        raise HTTPException(status_code=400, detail="系统路由表不可删除")
    client = get_chroma_client()
    dropped = False
    try:
        client.delete_collection(db_name)
        dropped = True
    except Exception:
        pass   # collection 本就不存在
    archived = False
    raw_dir = _category_raw_dir(db_name)
    if raw_dir.is_dir():
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        raw_dir.rename(TRASH_DIR / f"{time.strftime('%Y%m%d%H%M%S')}_{db_name}")
        archived = True
        if db_name == "unclassified_db":   # 未分类池是常驻结构，移走后重建空池
            UNCLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)
    try:
        get_chroma_collection("meta_classification_db").delete(ids=[db_name])
    except Exception:
        pass
    logger.info(f"🗑️ 分类 '{db_name}' 已真删（collection={dropped}, 原文归档={archived}）")
    return {"msg": f"分类 {db_name} 已删除（原文已移入回收目录，可手工找回）",
            "collection_dropped": dropped, "raw_archived": archived}


# ---------------- 数据流向与治理 ----------------
@app.post("/api/v1/document/insert", tags=["数据入库"])
async def insert_docs(req: InsertRequest, background_tasks: BackgroundTasks,
                      user_id: str = Depends(_current_user)):
    # user_id 为 D3 多用户埋点（参数面已通，逻辑未实现）：当前恒为 "default"，
    # _scoped_collection 对其返回原名——入库路径行为与现状全同。见模块头 _scoped_collection。
    forced = (req.category or "").strip() or None

    # 计算面前置确认：入库必然要算向量，计算面不可达时当场 503 告知调用方，
    # 而不是受理后在后台任务里静默失败（Agent 端看到的是"已启动"却永远查不到）。
    COMPUTE.ensure_ready()

    # 硬性前置校验（规则是代码）：显式指定的分类必须真实存在，
    # 不存在则拒绝并附上现有目录——调用方（Agent）据此自纠，而非静默流入未分类池
    if forced and forced != "unclassified_db":
        meta = get_chroma_collection("meta_classification_db")
        existing = set(meta.get()["ids"])
        if forced not in existing:
            catalog = "、".join(sorted(existing)) if existing else "（当前没有任何分类）"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"分类 '{forced}' 不存在，入库已拒绝。现有分类: {catalog}。"
                    "请改用现有分类；若确实没有合适的，先通过 POST /api/v1/category "
                    "创建该分类（附准确的路由描述）后再入库。"
                ),
            )

    # 重建场景的 source_path：必须真实存在且落在 storage/ 内——越界路径会让 source_path 元数据
    # 指向库外文件，而 /document/delete 按该元数据删盘上文件，等于把删除面扩到 storage 之外。
    existing_source = (req.source_path or "").strip() or None
    if existing_source:
        p = Path(existing_source)
        try:
            inside = p.resolve().is_relative_to(STORAGE_BASE.resolve())
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise HTTPException(status_code=400,
                                detail=f"source_path 必须位于 {STORAGE_BASE} 之内，已拒绝: {existing_source}")
        if not p.is_file():
            raise HTTPException(status_code=400,
                                detail=f"source_path 指向的原文备份不存在: {existing_source}")

    background_tasks.add_task(
        auto_classify_and_insert,
        raw_document=req.document, doc_title=req.title, forced_category=forced,
        existing_source_path=existing_source,
    )
    if forced:
        return {"msg": f"后台入库已启动（目标分类: {forced}）"}
    return {"msg": "后台流水分拣与入库已启动（自动路由分类）"}

@app.delete("/api/v1/document/delete", tags=["数据治理"])
async def delete_document_by_path(source_path: str = Body(embed=True)):
    """传入元数据中的备份路径，瞬间跨越切片进行整篇删除"""
    client = get_chroma_client()
    deleted_chunks = 0
    # 暴力但极简的做法：轮询所有 Collection，清扫相关片段
    for col_info in client.list_collections():
        col = client.get_collection(col_info.name)
        try:
            matched = col.get(where={"source_path": source_path})
            if matched["ids"]:
                col.delete(ids=matched["ids"])
                deleted_chunks += len(matched["ids"])
        except Exception as e:
            logger.error(f"清理库 {col_info.name} 时出错: {e}")

    # 原始备份文件移入回收目录（归档先行红线：不 rm，可手工找回）
    backup_file = Path(source_path)
    if backup_file.is_file() and backup_file.resolve().is_relative_to(STORAGE_BASE.resolve()):
        try:
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            backup_file.rename(
                TRASH_DIR / f"{time.strftime('%Y%m%d%H%M%S')}_{backup_file.name}")
        except OSError as e:
            logger.warning(f"⚠️ 原文移入回收目录失败（切片已清，文件留在原处）: {e}")

    if deleted_chunks == 0:
        # 教学式反馈：零命中大概率是路径不精确，引导调用方先检索拿到确切 Path
        return {"msg": (
            "未找到匹配该 source_path 的任何切片（删除了 0 条，可能路径不精确或文档已删除）。"
            "请先用检索接口确认目标文档元数据中 'Path: ...' 的精确值后重试。"
        ), "deleted_chunks": 0}
    return {"msg": f"全库扫描清理完毕，共删除 {deleted_chunks} 个切片", "deleted_chunks": deleted_chunks}

class StaleRequest(BaseModel):
    source_path: str
    reason: str = ""   # 空 = 取消标记


@app.post("/api/v1/document/stale", tags=["数据治理"])
async def mark_document_stale(req: StaleRequest):
    """过时标记（U3）：只写 metadata 不动内容——agent 对数据只有标记权没有改写权。

    检索结果与管理面板据此带出警示；reason 传空串即取消标记。恒写 stale 键
    （空串=无标记），不依赖「update 能否删键」的 Chroma 版本行为。
    """
    client = get_chroma_client()
    stale_val = req.reason.strip()
    stale_at = time.strftime("%Y-%m-%d") if stale_val else ""
    updated = 0
    for col_info in client.list_collections():
        col = client.get_collection(col_info.name)
        try:
            matched = col.get(where={"source_path": req.source_path})
            ids = matched["ids"]
            if not ids:
                continue
            metas = [{**dict(m or {}), "stale": stale_val, "stale_at": stale_at}
                     for m in matched["metadatas"]]
            col.update(ids=ids, metadatas=metas)
            updated += len(ids)
        except Exception as e:
            logger.error(f"标记库 {col_info.name} 时出错: {e}")
    if updated == 0:
        return {"msg": ("未找到匹配该 source_path 的任何切片。请先用检索接口确认"
                        "元数据中 'Path: ...' 的精确值后重试。"), "updated_chunks": 0}
    verb = "已标记过时" if stale_val else "已取消过时标记"
    return {"msg": f"{verb}（{updated} 个切片）", "updated_chunks": updated}


@app.post("/api/v1/governance/reclassify", tags=["数据治理"])
async def trigger_reclassification(background_tasks: BackgroundTasks):
    COMPUTE.ensure_ready()   # 重分拣要算向量：计算面不可达时当场 503
    background_tasks.add_task(reclassify_background_task)
    return {"msg": "数据重构清洗任务已置入后台运行"}

# ---------------- 核心并发检索 ----------------
_CONTAINS_LIMIT_PER_TOKEN = 5   # 每个标识符 token 最多补几条（重排是裁判，池别撑爆）


def _contains_prefetch(col_name: str, tokens: List[str]) -> List[dict]:
    """按标识符 token 做 where_document $contains 预取（fail-open，逐 token 限量）。"""
    out: List[dict] = []
    try:
        col = get_chroma_collection(col_name)
        if col.count() == 0:
            return out
        for tok in tokens:
            res = col.get(where_document={"$contains": tok},
                          limit=_CONTAINS_LIMIT_PER_TOKEN,
                          include=["documents", "metadatas"])
            for doc, meta, doc_id in zip(res.get("documents") or [],
                                         res.get("metadatas") or [],
                                         res.get("ids") or []):
                out.append({"db": col_name, "id": doc_id, "document": doc, "metadata": meta})
    except Exception as e:
        logger.warning(f"标识符预取 {col_name} 失败（跳过该路）: {e}")
    return out


def _query_single_db(col_name: str, query_emb: list, top_k: int):
    """供线程池使用的独立查库函数"""
    try:
        col = get_chroma_collection(col_name)
        if col.count() == 0: return []
        res = col.query(query_embeddings=[query_emb], n_results=top_k)

        if not res["documents"] or not res["documents"][0]: return []

        # 组装返回列表
        out = []
        for doc, meta, doc_id in zip(res["documents"][0], res["metadatas"][0], res["ids"][0]):
            out.append({"db": col_name, "id": doc_id, "document": doc, "metadata": meta})
        return out
    except Exception as e:
        logger.error(f"查询库 {col_name} 失败: {e}")
        return []

@app.post("/api/v1/document/query", tags=["核心检索"])
def advanced_query_rag(req: QueryRequest):
    COMPUTE.ensure_ready()   # 检索要算向量+重排：计算面不可达时当场 503（上线后自愈）

    # 1. 计算路由向量与检索向量（合一批：省一次模型前向与 MPS 往返；拆分模式还省一次 HTTP）
    cat_emb, opt_query_emb = get_embeddings([req.category, req.optimized_query])

    # 2. 软路由：获取目标库名
    target_dbs = []
    meta_db = get_chroma_collection("meta_classification_db")
    meta_count = meta_db.count()
    if meta_count > 0:
        routes = meta_db.query(query_embeddings=[cat_emb], n_results=min(req.top_k_dbs, meta_count))

        for db_name, distance in zip(routes["ids"][0], routes["distances"][0]):
            if distance <= THRESHOLDS["query_routing_max_distance"]:
                target_dbs.append(db_name)
            else:
                logger.info(f"🚫 库 {db_name} 距离过远 ({distance:.2f})，被动态剔除。")


    # 【强制带载未分类库兜底】
    if "unclassified_db" not in target_dbs:
        target_dbs.append("unclassified_db")

    logger.info(f"🔎 即将并发查询的候选库：{target_dbs}")

    # 3. 多库并发查询（常驻线程池：线程复用 → thread_local 的 Chroma 客户端复用）
    all_retrieved_chunks = []
    futures = {
        QUERY_POOL.submit(_query_single_db, db_name, opt_query_emb, req.chunk_k_per_db): db_name
        for db_name in target_dbs
    }
    for future in as_completed(futures):
        all_retrieved_chunks.extend(future.result())

    # 3b. 标识符子串预取（mem0 评估采纳）：纯向量把文件名/hash/键名摊平成语义噪声，
    #     对查询里的标识符 token 做 contains 预取并入重排池——重排当裁判，无关命中
    #     会被 rerank_min_score 筛掉，这一路只扩池不定序。
    idents = extract_identifiers(req.query, req.optimized_query)
    if idents:
        seen_ids = {c["id"] for c in all_retrieved_chunks}
        extra = 0
        for db_name in target_dbs:
            for c in _contains_prefetch(db_name, idents):
                if c["id"] not in seen_ids:
                    all_retrieved_chunks.append(c)
                    seen_ids.add(c["id"])
                    extra += 1
        if extra:
            logger.info(f"🔩 标识符预取 {idents} 补入 {extra} 条候选")

    if not all_retrieved_chunks:
        return {"original_query": req.query, "scanned_dbs": target_dbs, "total_chunks_retrieved": 0, "results": []}

    # 4. Reranker 全局统一重排（模板/收尾串归计算侧——模型家族绑定事实与权重同侧）
    rerank_scores = COMPUTE.rerank(req.query, [item["document"] for item in all_retrieved_chunks])

    # 5. 回填分数并截断
    for item, score in zip(all_retrieved_chunks, rerank_scores):
        item["rerank_score"] = score

    all_retrieved_chunks.sort(key=lambda x: x["rerank_score"], reverse=True)
    confident_chunks = [
        chunk for chunk in all_retrieved_chunks
        if chunk["rerank_score"] >= THRESHOLDS["rerank_min_score"]
    ]
    # MMR 多样性选择（grok-build 评估采纳）：候选多于名额时防 top-k 全是同一
    # 文档的近邻块；候选不足名额时原样通过（没得挑就不挑，行为与旧版全同）。
    final_results = mmr_select(confident_chunks, req.rerank_top_k)

    logger.info(f"📊 原始召回 {len(all_retrieved_chunks)} 条，动态置信度过滤后剩 {len(confident_chunks)} 条，最终喂给模型 {len(final_results)} 条。")

    return {
        "original_query": req.query,
        "scanned_dbs": target_dbs,
        "total_chunks_retrieved": len(all_retrieved_chunks),
        "results": final_results
    }

def _enforce_bind_guardrail() -> None:
    """部署铁则（§8 / §7 预留 2）：非回环绑定 + 空 RAG_API_KEY = 拒启。

    gpu profile 里 RAG 曾跟推理服务同住设备 A、由设备 B 跨机拨号——"改了 host 忘了 key"
    会让整个知识库对 tailnet 裸奔（比推理服务更糟：那是用户的原文）。拆分后数据面通常
    只绑回环（工作端本机），护栏保留：谁把它绑出去谁就必须带钥匙。
    回环绑定时空密钥仍放行（dev 现状不变）。与 backend/app.py、inference/server.py 同一条纪律。
    """
    if str(RAG_HOST).strip() in ("127.0.0.1", "localhost", "::1") or len(RAG_API_KEY) >= 16:
        return
    raise SystemExit(
        f"❌ [rag] 拒绝启动：绑定到非回环地址 {RAG_HOST!r} 但 RAG_API_KEY 缺失或过弱"
        f"（长度 {len(RAG_API_KEY)} < 16）——知识库会对整个可达网络裸奔。\n"
        f"   密钥过短/过弱同样拒绝。请 `./devctl.sh keygen` 生成强随机值（与设备 B 同值，"
        f"app 形态填进设置页），或把 rag.host 改回 127.0.0.1。\n"
        f"   （部署铁则 §8：非回环绑定只允许出现在受信内网 + 必须带 Bearer。）"
    )


def _preflight_bind_addr() -> None:
    """启动前本机绑定地址预检：RAG_HOST 非本机任何网卡地址（EADDRNOTAVAIL）立即拒启。

    实机事故（2026-07-19）：服务在别的设备上启动而 config 的 host 还填着另一台设备的
    IP → uvicorn 报晦涩的 `[Errno 49] can't assign requested address`。就地小副本，与
    backend/app.py 的 preflight_bind_addr 同语义（三服务各自独立 venv，不共享代码）。
    域名解析失败（gaierror）等其余错误静默放行，留给真实 bind 报错。
    """
    import errno
    import socket
    h = str(RAG_HOST or "").strip()
    if h in ("", "127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        return
    fam = socket.AF_INET6 if ":" in h else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((h, 0))
    except OSError as e:
        if getattr(e, "errno", None) == errno.EADDRNOTAVAIL:
            raise SystemExit(
                f"❌ [rag] 拒绝启动：绑定地址 '{h}' 不是本机任何网卡的地址（EADDRNOTAVAIL）。\n"
                f"   config 的 rag.host 应填**本机**（对外提供服务的这台设备自己）的 IP。\n"
                f"   查看本机地址：`tailscale ip -4` 或 `ifconfig | grep 'inet '`；\n"
                f"   若该地址属于另一台设备，请到那台设备上启动本服务。"
            )
    finally:
        s.close()


if __name__ == "__main__":
    import uvicorn
    _enforce_bind_guardrail()   # 先于加载模型：省掉几十秒权重加载才发现配置错
    _preflight_bind_addr()      # 密钥护栏之后：绑定地址不属本机立即拒启
    uvicorn.run(app, host=RAG_HOST, port=RAG_PORT, reload=False)
