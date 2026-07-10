import json
import os
import time
from pathlib import Path
from typing import Optional

# hf_xet 的分块缓存默认落 ~/.cache/huggingface/xet（与 local_dir 参数无关，一个 4GB 模型
# 会在项目外留下同量级缓存）。本项目要求模型文件全部收敛在仓库内，故禁用 xet、退回纯
# HTTPS 直下：断点数据存 local_dir/.cache/huggingface/download/（项目内），支持字节续传。
# 环境变量在 huggingface_hub 首次导入时定格，若别处（如 mlx_vlm）先导入则 setdefault
# 已迟——is_xet_available() 是调用时读常量属性，直接改属性可无视导入顺序兜底。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
from huggingface_hub import constants as _hf_constants
from huggingface_hub import snapshot_download
_hf_constants.HF_HUB_DISABLE_XET = True

_path = Path(__file__)
_utils_path = _path.parent
_framework_path = _utils_path.parent
BASE_WORKSPACE = _framework_path.parent

# 下载完整性标记：snapshot_download 成功返回后才写入。不能用「目录非空」判完整——
# hf 下载一开始就建 .cache/huggingface 元数据目录，中断残留必然非空，曾导致半截
# 模型被判有效、永不重下、启动死循环。
_MARKER_NAME = ".download_complete"


def resolve_model_path(model_id_or_path: str, revision: Optional[str] = None) -> str:
    """
    内部辅助函数：解析模型路径，支持本地绝对/相对路径以及 HuggingFace Repo ID。

    revision：钉死 HF 仓库的 commit（config.yaml inference.model_revision）。不钉死时
    snapshot_download 拉 main 最新——上游 re-upload 会让两台机器下到结构不同的权重
    （实例：gemma-4-e4b-it-4bit 2026-07-06 重传后裁掉共享 KV 权重，mlx-vlm 0.5.0
    加载直接 Missing 54 parameters）。凡走 HF 下载的常驻模型都应钉死。
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
        # 标记记录了当时下载的 revision：与钉死值一致（或双方任一未指定）即命中。
        # 不一致 = 本地是别的版本（多半是钉死之前拉的 main），落到下方 snapshot_download
        # 按钉死 revision 重同步自愈。旧格式标记（无 revision 字段）无从校验，放行并提示
        # ——若随后加载报缺权重，删掉模型目录重启即可按钉死版本重下。
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8")).get("revision")
        except Exception:
            recorded = None
        if revision is None or recorded is None or recorded == revision:
            if revision is not None and recorded is None:
                print(f"ℹ️ Local model {model_path.name} predates revision pinning and cannot be "
                      f"verified against {revision[:8]}. If loading fails, delete the directory "
                      f"and restart to re-download the pinned revision.")
            return str(model_path)
        print(f"♻️ Local model {model_path.name} is revision {str(recorded)[:8]}, "
              f"but {revision[:8]} is pinned. Re-syncing…")

    # 无标记 = 从未下载或上次中断：重调 snapshot_download 自愈（按 .metadata 边车跳过
    # 已完成文件、对 .incomplete 续传，不会重下已有的部分）
    had_local_files = model_path.is_dir() and any(model_path.iterdir())
    print(f"🌐 Local copy missing or unverified. Syncing from Hugging Face Hub: {model_id_or_path}")
    try:
        snapshot_download(
            repo_id=model_id_or_path,
            local_dir=model_path,
            revision=revision,
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
        "revision": revision,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(model_path)

if __name__ == "__main__":
    pass
