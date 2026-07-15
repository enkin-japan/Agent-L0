import hmac
import json
import logging
import os
import time
import uuid
import threading

# hf_xet 的分块缓存默认落 ~/.cache/huggingface/xet（与 local_dir 参数无关）。本项目要求
# 模型文件全部收敛在仓库内，故禁用 xet、退回纯 HTTPS 直下（断点数据存 local_dir/.cache，
# 项目内）。须先于 huggingface_hub 首次导入设置；下方再改常量属性以无视导入顺序兜底。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from concurrent.futures import ThreadPoolExecutor, as_completed
import torch
import torch.nn.functional as F
from pathlib import Path
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Request, Header, Depends
from pydantic import BaseModel, Field
from torch import Tensor
from transformers import AutoTokenizer, AutoModel, AutoModelForCausalLM
import chromadb
import yaml
from huggingface_hub import constants as _hf_constants
from huggingface_hub import snapshot_download
_hf_constants.HF_HUB_DISABLE_XET = True

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

RAG_CFG = config["rag"]
RAG_HOST = RAG_CFG["host"]
RAG_PORT = RAG_CFG["port"]
MAX_LENGTH = RAG_CFG["max_length"]
BATCH_SIZE = RAG_CFG["batch_size"]

# ── 档位解析（HETERO_COMPUTE B2a）──────────────────────────────────────────
# 选择制换档：仓库根 .rag-tier-selection 指针（./devctl.sh rag-model 写）→ active_tier 默认
#   → 旧 embed_path/rerank_path 兜底。解析次序镜像主模型 .model-selection。
# 一档 = 一对 {嵌入, 重排} 同尺寸家族模型；modality=text/vl；dtype 决定加载精度。
_TIER_SELECTION_FILE = CONFIG_PATH.parent / ".rag-tier-selection"


def _resolve_active_tier(rag_cfg: dict) -> dict:
    tiers = rag_cfg.get("tiers") or []
    sel_id = None
    try:
        sel_id = json.loads(_TIER_SELECTION_FILE.read_text(encoding="utf-8")).get("tier")
    except Exception:
        sel_id = None
    active_id = sel_id or rag_cfg.get("active_tier")
    for t in tiers:
        if t.get("id") == active_id:
            return dict(t)
    # 无 tiers / 未命中：用旧两键合成一个 text 档（向后兼容，行为与档位化之前完全一致）
    return {
        "id": active_id or "legacy",
        "modality": "text",
        "embed_repo": rag_cfg["embed_path"],
        "rerank_repo": rag_cfg["rerank_path"],
        "embed_dim": None,
        "dtype": "fp16",
        "thresholds": {},
    }


ACTIVE_TIER = _resolve_active_tier(RAG_CFG)
EMBED_MODEL = ACTIVE_TIER["embed_repo"]
RERANK_MODEL = ACTIVE_TIER["rerank_repo"]
MODALITY = str(ACTIVE_TIER.get("modality", "text")).lower()
_DTYPE_MAP = {
    "fp16": torch.float16, "float16": torch.float16,
    "bf16": torch.bfloat16, "bfloat16": torch.bfloat16,
    "fp32": torch.float32, "float32": torch.float32,
}
TIER_DTYPE = _DTYPE_MAP.get(str(ACTIVE_TIER.get("dtype", "fp16")).lower(), torch.float16)
TIER_EMBED_DIM_HINT = ACTIVE_TIER.get("embed_dim")   # config 声明值（护栏以运行时探测为准）

# 相似度阈值：档位 thresholds 覆盖 > tuning 全局兜底 > 硬编默认（余弦分布随尺寸变，须各档标定）
tuning = config.get("tuning") or {}
_tier_thr = ACTIVE_TIER.get("thresholds") or {}


def _thr(key, default):
    return _tier_thr.get(key, tuning.get(key, default))


INSERT_ROUTING_MAX_DISTANCE = _thr("insert_routing_max_distance", 0.8)
QUERY_ROUTING_MAX_DISTANCE = _thr("query_routing_max_distance", 0.85)
RERANK_MIN_SCORE = _thr("rerank_min_score", 0.5)
QUERY_MAX_DBS = tuning.get("query_max_dbs", 3)
QUERY_MAX_CHUNKS = tuning.get("query_max_chunks", 4)
QUERY_FINAL_MAX_RESULTS = tuning.get("query_final_max_results", 3)
CHUNK_MAX_TOKENS = tuning.get("chunk_max_tokens", 256)
CHUNK_OVERLAP_TOKENS = tuning.get("chunk_overlap_tokens", 128)

# ── 鉴权（HETERO_COMPUTE B2a，照抄 backend api/auth 的常量时间比对）───────────
# RAG 独立进程/venv，不 import backend，故就地实现。密钥走 env RAG_API_KEY（.env.local，
# devctl source，两侧同值）；留空 = 未启用（启动告警后放行，仅限开发/回环）。
RAG_API_KEY = os.environ.get("RAG_API_KEY", "")

DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
CHROMA_DB_PATH = BASE_WORKSPACE / "chroma_data"
# 换档护栏：建库时给存储盖戳（嵌入模型身份+维度），换档维度不符即快速失败提示先重建
TIER_STAMP_FILE = CHROMA_DB_PATH / ".tier_stamp.json"

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
# 下载完整性标记：snapshot_download 成功返回后才写入。不能用「目录非空」判完整——
# hf 下载一开始就建 .cache/huggingface 元数据目录，中断残留必然非空，曾导致半截
# 模型被判有效、永不重下、启动死循环。（与 backend/framework/utils/path_resolver.py 同步）
_MARKER_NAME = ".download_complete"


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
    marker = model_path / _MARKER_NAME
    if marker.exists():
        return str(model_path)

    # 无标记 = 从未下载或上次中断：重调 snapshot_download 自愈（按 .metadata 边车跳过
    # 已完成文件、对 .incomplete 续传，不会重下已有的部分）
    had_local_files = model_path.is_dir() and any(model_path.iterdir())
    print(f"🌐 Local copy missing or unverified. Syncing from Hugging Face Hub: {model_id_or_path}")
    try:
        snapshot_download(
            repo_id=model_id_or_path,
            local_dir=model_path,
        )
    except Exception as e:
        # 断网兜底：只放行本次调用前就存在的本地文件（标记机制之前的完整下载没有标记，
        # 不能把原本能离线启动的模型堵死；真半截则由加载器报错，联网后下次启动自愈）。
        # 本次才落的文件不放行——刚失败的下载必然不完整。
        if had_local_files:
            print(f"⚠️ Could not verify local model against Hugging Face Hub ({e}). "
                  f"Using existing files as-is: {model_path} — if loading fails, "
                  f"reconnect and restart to resume the download.")
            return str(model_path)
        raise ValueError(f"❌ Unable to find the local path, and unable to download the model from HF. Error message: {e}")

    marker.write_text(json.dumps({
        "repo_id": model_id_or_path,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(model_path)

# ==========================================
# 模型加载（档位化：文本 / 视觉多模态两条装配路径，HETERO_COMPUTE B2a）
# ==========================================
def _load_text_models(embed_path: str, rerank_path: str, dtype) -> dict:
    """纯文本族（Qwen3-Embedding / Qwen3-Reranker）：既有装配路径，0.6B 行为一字不变。"""
    return {
        "embed_tokenizer": AutoTokenizer.from_pretrained(embed_path, padding_side='left'),
        "embed_model": AutoModel.from_pretrained(embed_path, dtype=dtype).to(DEVICE).eval(),
        "rerank_tokenizer": AutoTokenizer.from_pretrained(rerank_path, padding_side='left'),
        "rerank_model": AutoModelForCausalLM.from_pretrained(rerank_path, dtype=dtype).to(DEVICE).eval(),
    }


def _load_vl_models(embed_path: str, rerank_path: str, dtype) -> dict:
    """视觉多模态族（Qwen3-VL-Embedding / Qwen3-VL-Reranker）。

    ⚠️ B2a L1 边界：本路径把 VL 模型**加载**并支持**文本 I/O**（分词器取自 AutoProcessor，
    池化/重排复用文本路径），足以在开发机单拉 RAG（bf16）实测"能选/能下/能加载/能出向量"。
    图像**真入库/检索**（processor 喂图、图文混合索引）属 L2 另一期，不在此实现。

    加载契约已用真机 2B VL 权重实测校准（B2b，见 HETERO_COMPUTE §S.3）：
    - **不需要 trust_remote_code**：qwen3_vl 是 transformers 原生架构（≥4.57，本 venv 5.8.0），
      两个 repo 的 config.json 均无 auto_map。不传 = 不给远程代码执行面（隐私/安全默认）。
    - **重排模型必须走 AutoModelForImageTextToText**：checkpoint 是 Qwen3VLForConditionalGeneration，
      `AutoModelForCausalLM` 不认 Qwen3VLConfig（直接 ValueError）——文本族两条路径在此**分叉**。
    - 嵌入侧 `AutoModel` → Qwen3VLModel，last_hidden_state + last_token_pool 口径与文本族同
      （官方 1_Pooling/config.json 亦为 lasttoken + Normalize），实测 dim=2048、语义可分。
    """
    from transformers import AutoProcessor, AutoModelForImageTextToText

    def _tok_from_processor(path):
        proc = AutoProcessor.from_pretrained(path)
        tok = getattr(proc, "tokenizer", None)
        if tok is None:
            tok = AutoTokenizer.from_pretrained(path, padding_side='left')
        else:
            tok.padding_side = 'left'
        return tok

    return {
        "embed_tokenizer": _tok_from_processor(embed_path),
        "embed_model": AutoModel.from_pretrained(embed_path, dtype=dtype).to(DEVICE).eval(),
        "rerank_tokenizer": _tok_from_processor(rerank_path),
        "rerank_model": AutoModelForImageTextToText.from_pretrained(
            rerank_path, dtype=dtype).to(DEVICE).eval(),
    }


def _probe_embed_dim() -> int:
    """探测当前嵌入模型的实际输出维度——护栏以此为准，绕开 config 声明值（尤其 VL 档）的不确定性。"""
    return len(get_embeddings(["dimension probe"])[0])


def _guard_or_stamp_store(runtime_dim: int) -> None:
    """换档护栏：比对存储戳与当前档位。

    - 有戳且维度/模型不符 → 快速失败（RuntimeError），提示先跑 reembed.py 重建。
    - 无戳（新库 / 档位化之前的旧库）→ 采纳当前档位盖戳。旧库若曾用别的档位建（无戳无从
      得知），首个 add/query 会在 chroma 层撞维度错误，由入库/检索处包成同样的"先重建"提示兜底。
    """
    try:
        stamp = json.loads(TIER_STAMP_FILE.read_text(encoding="utf-8"))
    except Exception:
        stamp = None

    if stamp:
        if int(stamp.get("embed_dim", -1)) != int(runtime_dim) or stamp.get("embed_repo") != EMBED_MODEL:
            raise RuntimeError(
                f"❌ RAG 换档护栏：向量库是用档位 '{stamp.get('tier_id')}'"
                f"（{stamp.get('embed_repo')}, dim={stamp.get('embed_dim')}）建立的，"
                f"而当前选择的是 '{ACTIVE_TIER.get('id')}'（{EMBED_MODEL}, dim={runtime_dim}）。\n"
                f"   换档必须重建向量库：先跑  python rag/scripts/reembed.py  从 storage/raw_files "
                f"全量重灌，或改回原档位。（不变量：库一旦入库，档位即固定。）"
            )
        return

    CHROMA_DB_PATH.mkdir(parents=True, exist_ok=True)
    TIER_STAMP_FILE.write_text(json.dumps({
        "tier_id": ACTIVE_TIER.get("id"),
        "modality": MODALITY,
        "embed_repo": EMBED_MODEL,
        "rerank_repo": RERANK_MODEL,
        "embed_dim": int(runtime_dim),
        "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info(f"🏷️ 向量库盖戳：档位 {ACTIVE_TIER.get('id')} / {EMBED_MODEL} / dim={runtime_dim}")


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
def _collection_of_raw_file(path: Path) -> str:
    """原文备份所在目录 = 它当初入的库（unclassified_raw/ → unclassified_db；raw_files/{类}/ → {类}）。"""
    return "unclassified_db" if path.parent == UNCLASSIFIED_DIR else path.parent.name


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

    all_embeddings = []

    for i in range(0, total_chunks, BATCH_SIZE):
        batch_chunks = chunks[i: i + BATCH_SIZE]
        all_embeddings.extend(get_embeddings(batch_chunks))

    # 【核心：Metadata 注入原始文件路径】(Chroma metadata 仅支持标量，路径需转 str)
    ids = [str(uuid.uuid4()) for _ in chunks]
    metadatas = [{"source_path": str(backup_path), "title": doc_title} for _ in chunks]

    try:
        target_db.add(embeddings=all_embeddings, documents=chunks, ids=ids, metadatas=metadatas)
    except Exception as e:
        # 换档护栏兜底：库是别的维度建的、当前档位维度不符（启动戳未拦住的旧库无戳边缘情形）
        if "dimension" in str(e).lower():
            logger.error(
                f"❌ 入库维度不匹配：库 '{target_category}' 与当前档位 "
                f"'{ACTIVE_TIER.get('id')}'(dim={GLOBAL_OBJECTS.get('embed_dim')}) 冲突——换档后未重建。"
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
    print(f"正在初始化硬件设备: {DEVICE}...  档位: {ACTIVE_TIER.get('id')} "
          f"(modality={MODALITY}, dtype={ACTIVE_TIER.get('dtype')})")
    if not RAG_API_KEY:
        logger.warning("⚠️ RAG_API_KEY 未设置：鉴权未启用（放行）——仅限开发/回环，跨机部署必须设置。")
    print("正在检查模型目录")
    embed_model_path = resolve_model_path(EMBED_MODEL)
    rerank_model_path = resolve_model_path(RERANK_MODEL)

    # 档位分派装配：文本族 vs 视觉多模态族（两条路径产出同一 GLOBAL_OBJECTS 形状）
    loader = _load_vl_models if MODALITY == "vl" else _load_text_models
    GLOBAL_OBJECTS.update(loader(embed_model_path, rerank_model_path, TIER_DTYPE))

    GLOBAL_OBJECTS["token_false_id"] = GLOBAL_OBJECTS["rerank_tokenizer"].convert_tokens_to_ids("no")
    GLOBAL_OBJECTS["token_true_id"] = GLOBAL_OBJECTS["rerank_tokenizer"].convert_tokens_to_ids("yes")
    GLOBAL_OBJECTS["rerank_prefix_tokens"] = GLOBAL_OBJECTS["rerank_tokenizer"].encode(
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n", add_special_tokens=False
    )
    # 重排收尾串按族分叉（B2b 实测校准）：文本族 Qwen3-Reranker 官方用例带空 <think> 块；
    # VL 族自带的 additional_chat_templates/reranker.jinja 收在 assistant\n 即止、**无** think 块。
    # 前缀两族字节相同（已对过 VL 模板），只有收尾不同。实测 2B VL：用 VL 收尾的相关/不相关
    # 分离度 0.61，误用文本族收尾降到 0.43（分数仍可用但判别力打折），故不可一族通吃。
    GLOBAL_OBJECTS["rerank_suffix_tokens"] = GLOBAL_OBJECTS["rerank_tokenizer"].encode(
        "<|im_end|>\n<|im_start|>assistant\n" if MODALITY == "vl"
        else "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
        add_special_tokens=False
    )

    # 换档护栏：探测运行维度 → 比对存储戳（不符即快速失败提示先 reembed），无戳则盖戳
    runtime_dim = _probe_embed_dim()
    GLOBAL_OBJECTS["embed_dim"] = runtime_dim
    if TIER_EMBED_DIM_HINT is not None and int(TIER_EMBED_DIM_HINT) != int(runtime_dim):
        logger.warning(f"⚠️ 档位 {ACTIVE_TIER.get('id')} 声明 embed_dim={TIER_EMBED_DIM_HINT}，"
                       f"实测={runtime_dim}——以实测为准（建议更新 config 的 embed_dim）。")
    _guard_or_stamp_store(runtime_dim)

    print(f"🚀 双模型 RAG 系统就绪（档位 {ACTIVE_TIER.get('id')} / dim={runtime_dim}）！")
    yield
    GLOBAL_OBJECTS.clear()

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
    return {
        "status": "healthy",
        "device": DEVICE,
        "tier": ACTIVE_TIER.get("id"),
        "modality": MODALITY,
        "embed_dim": GLOBAL_OBJECTS.get("embed_dim"),
    }

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
async def insert_docs(req: InsertRequest, background_tasks: BackgroundTasks,
                      user_id: str = Depends(_current_user)):
    # user_id 为 D3 多用户埋点（参数面已通，逻辑未实现）：当前恒为 "default"，
    # _scoped_collection 对其返回原名——入库路径行为与现状全同。见模块头 _scoped_collection。
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

def _enforce_bind_guardrail() -> None:
    """部署铁则（§8 / §7 预留 2）：非回环绑定 + 空 RAG_API_KEY = 拒启。

    gpu profile 里 RAG 跟推理服务同住设备 A、由设备 B 跨机拨号（B2 的 rag.host 指向服务器）——
    "改了 host 忘了 key"会让整个知识库对 tailnet 裸奔（比推理服务更糟：那是用户的原文）。
    回环绑定时空密钥仍放行（dev 现状不变）。与 backend/app.py、inference/server.py 同一条纪律。
    """
    if str(RAG_HOST).strip() in ("127.0.0.1", "localhost", "::1") or RAG_API_KEY:
        return
    raise SystemExit(
        f"❌ [rag] 拒绝启动：绑定到非回环地址 {RAG_HOST!r} 但未设置 RAG_API_KEY——"
        f"知识库会对整个可达网络裸奔。\n"
        f"   请在 .env.local 设置 RAG_API_KEY=<强随机串>（与设备 B 同值），"
        f"或把 rag.host 改回 127.0.0.1。\n"
        f"   （部署铁则 §8：非回环绑定只允许出现在受信内网 + 必须带 Bearer。）"
    )


if __name__ == "__main__":
    import uvicorn
    _enforce_bind_guardrail()   # 先于加载模型：省掉几十秒权重加载才发现配置错
    uvicorn.run(app, host=RAG_HOST, port=RAG_PORT, reload=False)