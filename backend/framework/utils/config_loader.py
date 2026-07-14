"""统一配置寻址：整个项目只有一份 config.yaml，在仓库根目录（backend 与 rag 共用）。

此前 backend 与 rag 各持一份 config.yaml，六个模块各自向上路径寻址、各自 yaml.load——
改配置要开两个文件、寻址逻辑散落一地。收拢为本模块单点定义：
- CONFIG_PATH   仓库根的 config.yaml（唯一真相）
- load_config() 读取并缓存整份配置（进程内解析一次；各模块取自己的段）

rag 服务是独立进程/独立 venv，不 import 本模块，但其 CONFIG_PATH 同样指向仓库根
（见 rag/rag_server.py），两侧读的是同一个文件。
"""
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[2]   # …/backend
REPO_ROOT = BACKEND_DIR.parent                       # 仓库根（config.yaml 所在）
CONFIG_PATH = REPO_ROOT / "config.yaml"


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    """读取仓库根 config.yaml（缓存单份）。文件缺失/损坏时抛出，由启动方尽早失败。"""
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_section(name: str) -> Dict[str, Any]:
    """取某个配置段（缺失返回空 dict）。工具模块在 import 期取段时的容错入口。"""
    try:
        return load_config().get(name) or {}
    except Exception:
        return {}


def rag_auth_headers() -> Dict[str, str]:
    """RAG 客户端鉴权头（HETERO_COMPUTE B2a）。

    读 env RAG_API_KEY（.env.local，devctl 启动时 source，与 rag 服务端同一变量）；
    空 = 不加头（dev/回环，与服务端"空 key 放行"对称）。三个 rag 客户端
    （rag_search / rag_admin / rag_sink）统一经此注入 Authorization，跨机部署即自动携带。
    """
    key = os.environ.get("RAG_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}
