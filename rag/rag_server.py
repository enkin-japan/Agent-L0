import logging
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import torch
import torch.nn.functional as F
from pathlib import Path
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Body
from pydantic import BaseModel, Field
from torch import Tensor
from transformers import AutoTokenizer, AutoModel, AutoModelForCausalLM
import chromadb
import yaml
from huggingface_hub import snapshot_download

# ==========================================
# 全局配置与 Mac (MPS) 设备初始化
# ==========================================
BASE_WORKSPACE = Path(__file__).parent
# 配置统一在仓库根（backend 与 rag 共用一份 config.yaml；rag 读其 rag/tuning 段）
CONFIG_PATH = BASE_WORKSPACE.parent / "config.yaml"

# 【工程补丁】预先创建物理隔离的备份文件夹
STORAGE_BASE = BASE_WORKSPACE / "storage"
RAW_FILES_DIR = STORAGE_BASE / "raw_files"
UNCLASSIFIED_DIR = STORAGE_BASE / "unclassified_raw"
RAW_FILES_DIR.mkdir(parents=True, exist_ok=True)
UNCLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)

with CONFIG_PATH.open("r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

EMBED_MODEL = config["rag"]["embed_path"]
RERANK_MODEL = config["rag"]["rerank_path"]
RAG_HOST = config["rag"]["host"]
RAG_PORT = config["rag"]["port"]
MAX_LENGTH = config["rag"]["max_length"]
BATCH_SIZE = config["rag"]["batch_size"]

# 相似度阈值等调优参数（从 config.yaml 的 tuning 段读取，缺省时使用默认值）
tuning = config.get("tuning") or {}
INSERT_ROUTING_MAX_DISTANCE = tuning.get("insert_routing_max_distance", 0.8)
QUERY_ROUTING_MAX_DISTANCE = tuning.get("query_routing_max_distance", 0.85)
QUERY_MAX_DBS = tuning.get("query_max_dbs", 3)
QUERY_MAX_CHUNKS = tuning.get("query_max_chunks", 4)
QUERY_FINAL_MAX_RESULTS = tuning.get("query_final_max_results", 3)
RERANK_MIN_SCORE = tuning.get("rerank_min_score", 0.5)
CHUNK_MAX_TOKENS = tuning.get("chunk_max_tokens", 256)
CHUNK_OVERLAP_TOKENS = tuning.get("chunk_overlap_tokens", 128)

DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
CHROMA_DB_PATH = BASE_WORKSPACE / "chroma_data"

GLOBAL_OBJECTS = {}
MPS_LOCK = threading.Lock()
thread_local_storage = threading.local()

# 常驻查询线程池：线程存活则 thread_local 的 Chroma 客户端可被复用。
# 此前每个查询请求临时新建 ThreadPoolExecutor——新线程上 thread_local 永远未命中，
# 等于每库每请求都重建一次 PersistentClient，纯粹的性能税。
QUERY_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rag-query")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RAG_Server")

# ==========================================
# 模型路径解析
# ==========================================
def resolve_model_path(model_id_or_path: str) -> str:
    """
    内部辅助函数：解析模型路径，支持本地绝对/相对路径以及 HuggingFace Repo ID
    """
    if isinstance(model_id_or_path, str):
        model_path = Path(model_id_or_path)
    else:
        raise TypeError("❌ The model path must be a string.")
    if model_path.exists() and model_path.is_dir():
        print(f"✅ Local model directory detected: {model_id_or_path}")
        return model_id_or_path
    
    model_path = BASE_WORKSPACE / "models" / Path(model_id_or_path).name
    if model_path.exists() and model_path.is_dir() and any(model_path.iterdir()):
        return str(model_path)
    print(f"🌐 The path was not found locally. Preparing to sync from Hugging Face Hub: {model_id_or_path}")
    try:
        snapshot_download(
            repo_id=model_id_or_path,
            local_dir=model_path,
        )
        return str(model_path)
    except Exception as e:
        raise ValueError(f"❌ Unable to find the local path, and unable to download the model from HF. Error message: {e}")

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

def last_token_pool(last_hidden_states: Tensor, attention_mask: Tensor) -> Tensor:
    left_padding = (attention_mask[:, -1].sum() == attention_mask.shape[0])
    if left_padding:
        return last_hidden_states[:, -1]
    else:
        sequence_lengths = attention_mask.sum(dim=1) - 1
        batch_size = last_hidden_states.shape[0]
        return last_hidden_states[torch.arange(batch_size, device=last_hidden_states.device), sequence_lengths]

def get_embeddings(texts: List[str]) -> List[List[float]]:
    """通用的高可用向量提取器"""
    tokenizer = GLOBAL_OBJECTS["embed_tokenizer"]
    model = GLOBAL_OBJECTS["embed_model"]
    
    batch_dict = tokenizer(
        texts, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt"
    ).to(DEVICE)

    with MPS_LOCK:
        with torch.no_grad():
            outputs = model(**batch_dict)
            embeddings = last_token_pool(outputs.last_hidden_state, batch_dict['attention_mask'])
            embeddings = F.normalize(embeddings, p=2, dim=1).cpu().tolist()
        # 不再每次 empty_cache：高频调用下反复清空/重建 MPS 分配器缓存是显著性能税，
        # PyTorch 的缓存分配器会自行复用；显存压力场景由重排侧的批清理兜底。

    return embeddings

def count_tokens(text: str, model_type: str = "embedding") -> int:
    tokenizer = GLOBAL_OBJECTS["embed_tokenizer"] if model_type == "embedding" else GLOBAL_OBJECTS["rerank_tokenizer"]
    tokens = tokenizer.encode(text, add_special_tokens=False)
    return len(tokens)

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
    # 批量 tokenize：一次调用算完全部行（此前逐行 encode，大文档是 O(行数) 次调用，慢一个量级）
    tokenizer = GLOBAL_OBJECTS["embed_tokenizer"]
    encoded = tokenizer(lines, add_special_tokens=False)["input_ids"]
    doc = list(zip(lines, (len(ids) for ids in encoded)))
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
def auto_classify_and_insert(raw_document: str, doc_title: str, forced_category: Optional[str] = None):
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
                    if top_distance < INSERT_ROUTING_MAX_DISTANCE:
                        target_category = routes['ids'][0][0]
                        logger.info(f"🎯 文档 [{doc_title}] 成功匹配到分类: {target_category} (距离: {top_distance:.4f})")
                    else:
                        logger.warning(f"⚠️ 相似度不足 (距离 {top_distance:.4f} > 阈值 {INSERT_ROUTING_MAX_DISTANCE})，流入待分类库。")
        except Exception as e:
            logger.error(f"分类查询失败，默认进入待分类库。原因: {e}")

    # 3. 文本切片（先于备份：空文档直接返回，不在磁盘留下无切片指向的孤儿备份文件）
    chunks = split_text_to_chunks(raw_document)
    total_chunks = len(chunks)
    if total_chunks == 0:
        logger.warning(f"⚠️ 文档 [{doc_title}] 内容为空，跳过入库（不写备份）。")
        return

    # 4. 备份原始文件
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
    all_embeddings = []

    for i in range(0, total_chunks, BATCH_SIZE):
        batch_chunks = chunks[i: i + BATCH_SIZE]
        all_embeddings.extend(get_embeddings(batch_chunks))

    # 【核心：Metadata 注入原始文件路径】(Chroma metadata 仅支持标量，路径需转 str)
    ids = [str(uuid.uuid4()) for _ in chunks]
    metadatas = [{"source_path": str(backup_path), "title": doc_title} for _ in chunks]
    
    target_db.add(embeddings=all_embeddings, documents=chunks, ids=ids, metadatas=metadatas)
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
    print(f"正在初始化硬件设备: {DEVICE}...")
    print("正在检查模型目录")
    embed_model_path = resolve_model_path(EMBED_MODEL)
    rerank_model_path = resolve_model_path(RERANK_MODEL)

    GLOBAL_OBJECTS["embed_tokenizer"] = AutoTokenizer.from_pretrained(embed_model_path, padding_side='left')
    GLOBAL_OBJECTS["embed_model"] = AutoModel.from_pretrained(embed_model_path, dtype=torch.float16).to(DEVICE).eval()

    GLOBAL_OBJECTS["rerank_tokenizer"] = AutoTokenizer.from_pretrained(rerank_model_path, padding_side='left')
    GLOBAL_OBJECTS["rerank_model"] = AutoModelForCausalLM.from_pretrained(rerank_model_path, dtype=torch.float16).to(DEVICE).eval()
    
    GLOBAL_OBJECTS["token_false_id"] = GLOBAL_OBJECTS["rerank_tokenizer"].convert_tokens_to_ids("no")
    GLOBAL_OBJECTS["token_true_id"] = GLOBAL_OBJECTS["rerank_tokenizer"].convert_tokens_to_ids("yes")
    GLOBAL_OBJECTS["rerank_prefix_tokens"] = GLOBAL_OBJECTS["rerank_tokenizer"].encode(
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n", add_special_tokens=False
    )
    GLOBAL_OBJECTS["rerank_suffix_tokens"] = GLOBAL_OBJECTS["rerank_tokenizer"].encode(
        "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n", add_special_tokens=False
    )
    print("🚀 双模型 RAG 系统及存储结构已就绪！")
    yield
    GLOBAL_OBJECTS.clear()

app = FastAPI(lifespan=lifespan, title="RAG 知识库 API")

# ==========================================
# 模型与接口
# ==========================================
class InsertRequest(BaseModel):
    title: str
    document: str
    # 调用方显式指定的目标分类：必须已存在（或 unclassified_db），不存在则 400 拒绝并返回现有目录；
    # 缺省 None 时走原有的向量自动路由（低于阈值进未分类池）
    category: Optional[str] = None

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
    return {"status": "healthy", "device": DEVICE}

@app.post("/tokenize", tags=["基础监控"])
async def token_counter(text: str = Body(embed=True), model_type: str = Body("embedding", embed=True)):
    return {"token_count": count_tokens(text, model_type)}

# ---------------- 分类治理 ----------------
@app.get("/api/v1/category", tags=["分类管理"])
async def list_categories():
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

# ---------------- 数据流向与治理 ----------------
@app.post("/api/v1/document/insert", tags=["数据入库"])
async def insert_docs(req: InsertRequest, background_tasks: BackgroundTasks):
    forced = (req.category or "").strip() or None

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

    background_tasks.add_task(
        auto_classify_and_insert,
        raw_document=req.document, doc_title=req.title, forced_category=forced,
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

    # 同步删除磁盘上的原始备份文件
    backup_file = Path(source_path)
    if backup_file.is_file() and backup_file.resolve().is_relative_to(STORAGE_BASE.resolve()):
        backup_file.unlink(missing_ok=True)

    if deleted_chunks == 0:
        # 教学式反馈：零命中大概率是路径不精确，引导调用方先检索拿到确切 Path
        return {"msg": (
            "未找到匹配该 source_path 的任何切片（删除了 0 条，可能路径不精确或文档已删除）。"
            "请先用检索接口确认目标文档元数据中 'Path: ...' 的精确值后重试。"
        ), "deleted_chunks": 0}
    return {"msg": f"全库扫描清理完毕，共删除 {deleted_chunks} 个切片", "deleted_chunks": deleted_chunks}

@app.post("/api/v1/governance/reclassify", tags=["数据治理"])
async def trigger_reclassification(background_tasks: BackgroundTasks):
    background_tasks.add_task(reclassify_background_task)
    return {"msg": "数据重构清洗任务已置入后台运行"}

# ---------------- 核心并发检索 ----------------
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
    # 1. 计算路由向量与检索向量（合一批：省一次模型前向与 MPS 往返）
    cat_emb, opt_query_emb = get_embeddings([req.category, req.optimized_query])
    
    # 2. 软路由：获取目标库名
    target_dbs = []
    meta_db = get_chroma_collection("meta_classification_db")
    meta_count = meta_db.count()
    if meta_count > 0:
        routes = meta_db.query(query_embeddings=[cat_emb], n_results=min(req.top_k_dbs, meta_count))
        
        for db_name, distance in zip(routes["ids"][0], routes["distances"][0]):
            if distance <= QUERY_ROUTING_MAX_DISTANCE:
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

    if not all_retrieved_chunks:
        return {"original_query": req.query, "scanned_dbs": target_dbs, "total_chunks_retrieved": 0, "results": []}

    # 4. Reranker 全局统一重排
    task_desc = 'Given a web search query, retrieve relevant passages that answer the query'
    pairs = [f"<Instruct>: {task_desc}\n<Query>: {req.query}\n<Document>: {item['document']}" for item in all_retrieved_chunks]
    
    rerank_tokenizer = GLOBAL_OBJECTS["rerank_tokenizer"]
    rerank_model = GLOBAL_OBJECTS["rerank_model"]
    
    inputs = rerank_tokenizer(
        pairs, padding=False, truncation='longest_first', 
        return_attention_mask=False, 
        max_length=MAX_LENGTH - len(GLOBAL_OBJECTS["rerank_prefix_tokens"]) - len(GLOBAL_OBJECTS["rerank_suffix_tokens"])
    )
    for i, ele in enumerate(inputs['input_ids']):
        inputs['input_ids'][i] = GLOBAL_OBJECTS["rerank_prefix_tokens"] + ele + GLOBAL_OBJECTS["rerank_suffix_tokens"]
    
    inputs = rerank_tokenizer.pad(inputs, padding=True, return_tensors="pt").to(DEVICE)

    with MPS_LOCK:
        with torch.no_grad():
            batch_scores = rerank_model(**inputs).logits[:, -1, :]
            true_vector = batch_scores[:, GLOBAL_OBJECTS["token_true_id"]]
            false_vector = batch_scores[:, GLOBAL_OBJECTS["token_false_id"]]
            batch_scores = torch.stack([false_vector, true_vector], dim=1)
            batch_scores = torch.nn.functional.log_softmax(batch_scores, dim=1)
            rerank_scores = batch_scores[:, 1].exp().tolist() 
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()

    # 5. 回填分数并截断
    for item, score in zip(all_retrieved_chunks, rerank_scores):
        item["rerank_score"] = score
        
    all_retrieved_chunks.sort(key=lambda x: x["rerank_score"], reverse=True)
    confident_chunks = [
        chunk for chunk in all_retrieved_chunks 
        if chunk["rerank_score"] >= RERANK_MIN_SCORE
    ]
    final_results = confident_chunks[:req.rerank_top_k]

    logger.info(f"📊 原始召回 {len(all_retrieved_chunks)} 条，动态置信度过滤后剩 {len(confident_chunks)} 条，最终喂给模型 {len(final_results)} 条。")

    return {
        "original_query": req.query,
        "scanned_dbs": target_dbs,
        "total_chunks_retrieved": len(all_retrieved_chunks),
        "results": final_results
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=RAG_HOST, port=RAG_PORT, reload=False)