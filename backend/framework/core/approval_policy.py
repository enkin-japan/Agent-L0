"""审批策略（build 78 Contract P）：按工具效果分类（read / write）的两档用户旋钮。

每档取值 MODES：
- "auto"    该类调用一律免审批；
- "default" 沿用工具自身的 requires_approval / needs_approval（= build 77 行为）；
- "ask"     该类调用一律审批。
两档缺省都是 "default"——用户不动旋钮，审批行为与 build 77 逐条相同。
钉死审批（BaseTool.approval_pinned）与无人值守轮（恒用 DEFAULT）见 registry / orchestrator。

判定在 MCPRegistry.approval_required（未知工具 fail-closed 恒审批）；本模块只管策略值的
校验、持久化（<data_root>/approval_policy.json，0600，原子写）与进程级持有者——
PATCH 改完立即对下一次工具闸门生效，不需重启。

持久化文件缺失 = 缺省（DEFAULT）；存在但损坏/读不了 = 全审批（ASK_ALL，fail closed）；
各一行告警。
"""
import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

MODES = ("auto", "default", "ask")
CLASSES = ("read", "write")
FILENAME = "approval_policy.json"


@dataclass(frozen=True)
class ApprovalPolicy:
    read: str = "default"
    write: str = "default"

    def __post_init__(self):
        for cls in CLASSES:
            mode = getattr(self, cls)
            if mode not in MODES:
                raise ValueError(f"approval policy {cls!r}: invalid mode {mode!r} (use {'/'.join(MODES)})")

    def __getitem__(self, cls: str) -> str:
        """Contract P 写作 policy[cls]；只认 read/write，别的键 KeyError（判定方按审批处理）。"""
        if cls not in CLASSES:
            raise KeyError(cls)
        return getattr(self, cls)

    def as_dict(self) -> Dict[str, str]:
        return {cls: getattr(self, cls) for cls in CLASSES}


DEFAULT = ApprovalPolicy()
# 策略文件存在却读不了/解析不了/取值非法：fail closed 到全审批（用户调过旋钮的证据还在，
# 只是内容坏了——不能悄悄回到可能比用户设定更松的缺省）。
ASK_ALL = ApprovalPolicy(read="ask", write="ask")


def validate(patch: Any, base: Optional[ApprovalPolicy] = None) -> ApprovalPolicy:
    """把部分更新 {"read"?: MODE, "write"?: MODE} 合进 base（缺省 = DEFAULT）。
    非映射、未知键、非法取值（含 null）一律 ValueError——不做部分接受。"""
    if not isinstance(patch, dict):
        raise ValueError("approval policy patch must be an object")
    unknown = sorted(str(k) for k in patch if k not in CLASSES)
    if unknown:
        raise ValueError(f"unknown approval policy key(s): {unknown} (allowed: {list(CLASSES)})")
    for cls, mode in patch.items():
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError(f"approval policy {cls!r}: invalid mode {mode!r} (use {'/'.join(MODES)})")
    merged = (base or DEFAULT).as_dict()
    merged.update(patch)
    return ApprovalPolicy(**merged)


def _path(data_root: Optional[Path] = None) -> Path:
    if data_root is None:
        from ..utils.config_loader import data_dir
        data_root = data_dir()
    return Path(data_root) / FILENAME


def load(data_root: Optional[Path] = None) -> ApprovalPolicy:
    """读持久化策略（绝不抛）。缺失 → DEFAULT；存在但读不了/损坏/取值非法 →
    ASK_ALL（fail closed）。两种情形各一行告警。"""
    path = _path(data_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return validate(raw)
    except FileNotFoundError:
        print(f"[ApprovalPolicy] ⚠️ {path} 不存在，使用缺省策略 {DEFAULT.as_dict()}")
        return DEFAULT
    except Exception as e:
        print(f"[ApprovalPolicy] ⚠️ {path} 无法使用（{type(e).__name__}: {e}），"
              f"按全审批处理 {ASK_ALL.as_dict()}")
        return ASK_ALL


def save(policy: ApprovalPolicy, data_root: Optional[Path] = None) -> Path:
    """原子写（同目录临时文件 + fsync + os.replace），文件权限 0600。"""
    path = _path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(policy.as_dict(), ensure_ascii=False, indent=1).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)        # 换名前钉权限：可见的文件从第一刻起就是 0600
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


# ---------------- 进程级持有者 ----------------
# 首次 current() 惰性从数据根装载（app.py 生命周期不需要改）；PATCH 经 update()
# 先落盘再替换，落盘失败则内存策略不变。
_lock = threading.Lock()
_current: Optional[ApprovalPolicy] = None


def current() -> ApprovalPolicy:
    global _current
    policy = _current
    if policy is not None:
        return policy
    with _lock:
        if _current is None:
            _current = load()
        return _current


def replace(policy: ApprovalPolicy) -> ApprovalPolicy:
    """替换进程内策略（不落盘）。"""
    global _current
    if not isinstance(policy, ApprovalPolicy):
        raise TypeError("replace() expects an ApprovalPolicy")
    with _lock:
        _current = policy
    return policy


def update(patch: Any, data_root: Optional[Path] = None) -> ApprovalPolicy:
    """校验 → 落盘 → 替换（PATCH 端点用）。校验失败 ValueError、落盘失败 OSError，
    两种情形内存与文件都不变。基线取内存现值（未装载则读文件，损坏 = ASK_ALL）。"""
    global _current
    with _lock:
        base = _current if _current is not None else load(data_root)
        policy = validate(patch, base)
        save(policy, data_root)
        _current = policy
    return policy


def reset() -> None:
    """测试用：回到「尚未装载」，下一次 current() 重新读数据根。"""
    global _current
    with _lock:
        _current = None
