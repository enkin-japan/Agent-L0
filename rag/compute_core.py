"""RAG 计算核（数据/计算拆分，HETERO_COMPUTE B2c）。

从 rag_server.py **原样抽取**的模型侧代码：档位解析、模型下载/加载、嵌入、重排、
token 计数。两个宿主共用同一份：
  - rag_server.py 融合模式（rag.compute.host 为空）：进程内 import 本模块就地加载
    ——单机现状，行为与拆分前逐字一致；
  - compute_server.py（算力端 :14162）：把本模块包成无状态 HTTP 服务，数据面跨机拨号。

本模块**不碰任何用户数据**（chroma_data / storage 归数据面）；唯一的磁盘足迹是
rag/models/ 下的模型权重（代码资产，非用户数据，恒在代码目录旁）。

环境变量：
  RAG_CONFIG    config.yaml 路径覆盖（缺省 = 仓库根；bundle/测试布局注入）
"""
import json
import logging
import os
import threading
import time

# hf_xet 的分块缓存默认落 ~/.cache/huggingface/xet（与 local_dir 参数无关）。本项目要求
# 模型文件全部收敛在仓库内，故禁用 xet、退回纯 HTTPS 直下（断点数据存 local_dir/.cache，
# 项目内）。须先于 huggingface_hub 首次导入设置；下方再改常量属性以无视导入顺序兜底。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from pathlib import Path
from typing import List, Optional

import torch
import torch.nn.functional as F
import yaml
from torch import Tensor
from transformers import AutoTokenizer, AutoModel, AutoModelForCausalLM
from huggingface_hub import constants as _hf_constants
from huggingface_hub import snapshot_download
_hf_constants.HF_HUB_DISABLE_XET = True

BASE_WORKSPACE = Path(__file__).parent
# 配置统一在仓库根（backend 与 rag 共用一份 config.yaml；rag 读其 rag/tuning 段）
_cfg_override = os.environ.get("RAG_CONFIG", "").strip()
CONFIG_PATH = Path(_cfg_override) if _cfg_override else BASE_WORKSPACE.parent / "config.yaml"

with CONFIG_PATH.open("r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

RAG_CFG = config["rag"]
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

# 设备优先级：cuda（Colab/生产 GPU 档，2026-08-08 RAG 选型波补）> mps（开发 Mac）> cpu
DEVICE = ("cuda" if torch.cuda.is_available()
          else "mps" if torch.backends.mps.is_available() else "cpu")

GLOBAL_OBJECTS = {}
MPS_LOCK = threading.Lock()

# 重排指令：调用方（数据面 /query）不再自带模板——模板/收尾串是模型家族绑定事实，
# 与权重同侧（本模块），数据面只传 query+documents（可选覆盖 instruct）。
DEFAULT_RERANK_INSTRUCT = 'Given a web search query, retrieve relevant passages that answer the query'

logger = logging.getLogger("RAG_Compute")

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


def load_models() -> None:
    """装配全套模型对象 + 重排模板 token + 运行维度探测（原 rag_server lifespan 的模型段）。"""
    print(f"正在初始化硬件设备: {DEVICE}...  档位: {ACTIVE_TIER.get('id')} "
          f"(modality={MODALITY}, dtype={ACTIVE_TIER.get('dtype')})")
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

    # 运行维度探测：护栏/identity 以此为准，绕开 config 声明值（尤其 VL 档）的不确定性
    runtime_dim = len(embed_texts(["dimension probe"])[0])
    GLOBAL_OBJECTS["embed_dim"] = runtime_dim
    if TIER_EMBED_DIM_HINT is not None and int(TIER_EMBED_DIM_HINT) != int(runtime_dim):
        logger.warning(f"⚠️ 档位 {ACTIVE_TIER.get('id')} 声明 embed_dim={TIER_EMBED_DIM_HINT}，"
                       f"实测={runtime_dim}——以实测为准（建议更新 config 的 embed_dim）。")
    print(f"🚀 RAG 计算核就绪（档位 {ACTIVE_TIER.get('id')} / dim={runtime_dim}）")


def unload_models() -> None:
    GLOBAL_OBJECTS.clear()


def identity() -> dict:
    """计算面的身份自述：数据面据此做换档护栏比对 + 采纳档位阈值。"""
    return {
        "tier": ACTIVE_TIER.get("id"),
        "modality": MODALITY,
        "embed_repo": EMBED_MODEL,
        "rerank_repo": RERANK_MODEL,
        "embed_dim": GLOBAL_OBJECTS.get("embed_dim"),
        "dtype": str(ACTIVE_TIER.get("dtype", "fp16")),
        "device": DEVICE,
        "thresholds": dict(ACTIVE_TIER.get("thresholds") or {}),
    }


# ==========================================
# 计算原语：嵌入 / token 计数 / 重排
# ==========================================
def last_token_pool(last_hidden_states: Tensor, attention_mask: Tensor) -> Tensor:
    left_padding = (attention_mask[:, -1].sum() == attention_mask.shape[0])
    if left_padding:
        return last_hidden_states[:, -1]
    else:
        sequence_lengths = attention_mask.sum(dim=1) - 1
        batch_size = last_hidden_states.shape[0]
        return last_hidden_states[torch.arange(batch_size, device=last_hidden_states.device), sequence_lengths]


def _embed_batch(texts: List[str]) -> List[List[float]]:
    """单批向量提取（原 get_embeddings，逐字不变）。"""
    tokenizer = GLOBAL_OBJECTS["embed_tokenizer"]
    model = GLOBAL_OBJECTS["embed_model"]

    # tokenizer 也在锁内：HF fast tokenizer（Rust）并发 encode 会抛
    # "RuntimeError: Already borrowed"（2026-07-30 检索评估集并发入库实测）。
    # MPS 前向本就被锁串行，扩大锁面无吞吐损失。
    with MPS_LOCK:
        batch_dict = tokenizer(
            texts, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt"
        ).to(DEVICE)
        with torch.no_grad():
            outputs = model(**batch_dict)
            embeddings = last_token_pool(outputs.last_hidden_state, batch_dict['attention_mask'])
            embeddings = F.normalize(embeddings, p=2, dim=1).cpu().tolist()
        # 不再每次 empty_cache：高频调用下反复清空/重建 MPS 分配器缓存是显著性能税，
        # PyTorch 的缓存分配器会自行复用；显存压力场景由重排侧的批清理兜底。

    return embeddings


def embed_texts(texts: List[str]) -> List[List[float]]:
    """向量提取，内部按 BATCH_SIZE 分批（拆分模式下数据面整单发来，分批责任归计算侧——
    原先在 rag_server 的入库循环里，逐批 HTTP 往返只是徒增延迟）。"""
    out: List[List[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        out.extend(_embed_batch(texts[i: i + BATCH_SIZE]))
    return out


def token_counts(texts: List[str], model_type: str = "embedding") -> List[int]:
    """批量 token 计数（切块器与 /tokenize 共用；一次调用算完全部行）。"""
    tokenizer = GLOBAL_OBJECTS["embed_tokenizer"] if model_type == "embedding" else GLOBAL_OBJECTS["rerank_tokenizer"]
    if not texts:
        return []
    with MPS_LOCK:   # fast tokenizer 非线程安全（Already borrowed），与前向共锁
        encoded = tokenizer(texts, add_special_tokens=False)["input_ids"]
    return [len(ids) for ids in encoded]


def rerank_scores(query: str, documents: List[str], instruct: Optional[str] = None) -> List[float]:
    """判别式重排打分（原 /query 第 4 步，逐字平移）：返回与 documents 等长的 yes 概率。"""
    if not documents:
        return []
    task_desc = instruct or DEFAULT_RERANK_INSTRUCT
    pairs = [f"<Instruct>: {task_desc}\n<Query>: {query}\n<Document>: {doc}" for doc in documents]

    rerank_tokenizer = GLOBAL_OBJECTS["rerank_tokenizer"]
    rerank_model = GLOBAL_OBJECTS["rerank_model"]

    with MPS_LOCK:   # encode/pad 一并入锁：fast tokenizer 非线程安全（Already borrowed）
        inputs = rerank_tokenizer(
            pairs, padding=False, truncation='longest_first',
            return_attention_mask=False,
            max_length=MAX_LENGTH - len(GLOBAL_OBJECTS["rerank_prefix_tokens"]) - len(GLOBAL_OBJECTS["rerank_suffix_tokens"])
        )
        for i, ele in enumerate(inputs['input_ids']):
            inputs['input_ids'][i] = GLOBAL_OBJECTS["rerank_prefix_tokens"] + ele + GLOBAL_OBJECTS["rerank_suffix_tokens"]

        inputs = rerank_tokenizer.pad(inputs, padding=True, return_tensors="pt").to(DEVICE)

        with torch.no_grad():
            batch_scores = rerank_model(**inputs).logits[:, -1, :]
            true_vector = batch_scores[:, GLOBAL_OBJECTS["token_true_id"]]
            false_vector = batch_scores[:, GLOBAL_OBJECTS["token_false_id"]]
            batch_scores = torch.stack([false_vector, true_vector], dim=1)
            batch_scores = torch.nn.functional.log_softmax(batch_scores, dim=1)
            scores = batch_scores[:, 1].exp().tolist()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()

    return scores
