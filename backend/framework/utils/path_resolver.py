from pathlib import Path
from huggingface_hub import snapshot_download

_path = Path(__file__)
_utils_path = _path.parent
_framework_path = _utils_path.parent
BASE_WORKSPACE = _framework_path.parent


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
    # any(...) 必须有：裸 iterdir() 是生成器、恒为真，空目录（下载中断残留）会被误判为有效模型
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

if __name__ == "__main__":
    pass