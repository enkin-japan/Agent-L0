"""待确认注册存储（D.18 对话式注册的状态机腿；DEFERRED §4 完整版）。

模型经 register_api_tool / register_skill 起草的声明**落进这里而不是生效目录**：
data_dir()/registrations/{kind}-{name}.yaml，状态恒 pending。确认/驳回只走设置页
（POST /registrations/{id}/confirm、DELETE /registrations/{id}），模型没有确认工具
——红线②「模型自注册是权限自举回路，声明不可静默生效」的物理执行层。

红线①（密钥不走对话通道）在提交口执行：声明里任何形似密钥实值的字段直接拒绝
（headers 不收鉴权头、常见密钥前缀模式拒收），密钥只能以 auth.env **变量名**出现，
实值由用户在设置页密钥框录入（api_tool_factory.write_secret → 0600 文件）。
"""
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..utils.config_loader import data_dir

KINDS = ("api_tool", "skill")

#: headers 里不收的鉴权头（密钥应走 auth.env 侧通道，而不是明文写进声明）
_AUTHY_HEADERS = {"authorization", "x-api-key", "api-key", "x-auth-token",
                  "x-access-token", "apikey", "token"}
#: 常见密钥实值前缀/形态（宁可错杀——命中者让用户走设置页密钥框）
_SECRETY_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|xox[bap]-|AKIA[0-9A-Z]{8,}"
    r"|(?i:bearer)\s+\S{12,})")


def pending_dir() -> Path:
    """惰性寻址（照 api_tool_factory.user_decl_dir：进程内 env 不变，测试各自注入）。"""
    return data_dir() / "registrations"


def find_inline_secret(decl: Dict[str, Any]) -> Optional[str]:
    """返回问题描述；None = 干净。只查声明文本面，不看运行期 env。"""
    headers = decl.get("headers") or {}
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).strip().lower() in _AUTHY_HEADERS:
                return (f"headers 不收鉴权头 {k!r}：密钥走 auth.env 变量名 + "
                        f"设置页密钥框，绝不进声明/对话")
            if _SECRETY_VALUE.search(str(v)):
                return f"headers[{k!r}] 形似密钥实值：声明只能引用变量名"
    def _walk(x):
        if isinstance(x, dict):
            return any(_walk(v) for v in x.values())
        if isinstance(x, list):
            return any(_walk(v) for v in x)
        return isinstance(x, str) and bool(_SECRETY_VALUE.search(x))
    if _walk({k: v for k, v in decl.items() if k != "headers"}):
        return "声明中出现形似密钥的实值：密钥只能以环境变量名引用，实值在设置页录入"
    return None


def submit(kind: str, name: str, payload: Dict[str, Any],
           note: str = "") -> str:
    """落一份待确认声明，返回注册单 id。同 id 重复提交 = 覆盖（模型迭代草稿）。"""
    if kind not in KINDS:
        raise ValueError(f"kind 非法: {kind}")
    rid = f"{kind}-{name}"
    d = pending_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.yaml").write_text(
        yaml.safe_dump({"id": rid, "kind": kind, "name": name, "status": "pending",
                        "proposed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "note": note, "payload": payload},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return rid


_RID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _path_of(rid: str) -> Path:
    if not _RID_RE.match(rid):        # id 即文件名——先验格式，杜绝路径逃逸
        raise ValueError(f"注册单 id 非法: {rid!r}")
    return pending_dir() / f"{rid}.yaml"


def get(rid: str) -> Optional[Dict[str, Any]]:
    p = _path_of(rid)
    if not p.exists():
        return None
    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def remove(rid: str) -> bool:
    p = _path_of(rid)
    if not p.exists():
        return False
    p.unlink()
    return True


def list_pending() -> List[Dict[str, Any]]:
    d = pending_dir()
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.yaml")):
        try:
            item = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            item = None
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append({"id": f.stem, "kind": "?", "name": f.stem,
                        "status": "corrupt", "payload": {}})
    return out
