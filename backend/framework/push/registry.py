"""设备注册表：iOS 设备 token / 通知偏好 / Live Activity push-to-start token 的落盘账。

存储 backend/push/devices.json（原子写：tmp + rename，对齐 ScheduleStore 纪律）。
全部方法在事件循环线程调用；文件小（每设备一条），同步小写与 journal 落盘同纪律。

偏好默认值即产品决策（评估里与用户对齐）：
  approval=True（鉴权要求默认开——安全关键）；results=True；
  schedule_start=False（周期任务的"开始了"横幅半夜会吵，默认关，Live Activity 仍照亮）；
  content_level="category"（隐私默认：推类别+会话名，不推内容；full/placeholder 可选）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PREFS: Dict[str, Any] = {
    "results": True,
    "schedule_start": False,
    "approval": True,
    "content_level": "category",   # full | category | placeholder
}
_VALID_CONTENT_LEVELS = {"full", "category", "placeholder"}


class PushRegistry:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "devices.json"
        self.devices: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ---------- 落盘 ----------
    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.devices = {k: v for k, v in data.items() if isinstance(v, dict)}
        except Exception as e:
            print(f"[Push] 注册表读取失败（从空账开始）: {e}")
            self.devices = {}

    def _persist(self) -> None:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.devices, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.rename(self.path)
        except Exception as e:
            print(f"[Push] 注册表落盘失败（内存态仍有效）: {e}")

    # ---------- 写入 ----------
    def upsert(self, device_id: str, *, platform: str = "ios",
               token: Optional[str] = None,
               push_to_start_token: Optional[str] = None,
               prefs: Optional[Dict[str, Any]] = None,
               app_build: Optional[str] = None) -> Dict[str, Any]:
        d = self.devices.get(device_id) or {
            "device_id": device_id, "platform": platform,
            "env": "auto", "prefs": dict(DEFAULT_PREFS),
        }
        d["platform"] = platform or d.get("platform", "ios")
        if token is not None:
            if token != d.get("token"):
                d["env"] = "auto"   # token 轮换后环境重新学习
            d["token"] = token
        if push_to_start_token is not None:
            d["push_to_start_token"] = push_to_start_token
        if app_build is not None:
            d["app_build"] = app_build
        if prefs:
            d["prefs"] = self._merge_prefs(d.get("prefs") or {}, prefs)
        d["updated_at"] = round(time.time(), 3)
        self.devices[device_id] = d
        self._persist()
        return d

    @staticmethod
    def _merge_prefs(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        out = {**DEFAULT_PREFS, **base}
        for k in ("results", "schedule_start", "approval"):
            if k in patch:
                out[k] = bool(patch[k])
        lvl = patch.get("content_level")
        if lvl in _VALID_CONTENT_LEVELS:
            out["content_level"] = lvl
        return out

    def update_prefs(self, device_id: str, prefs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        d = self.devices.get(device_id)
        if d is None:
            return None
        d["prefs"] = self._merge_prefs(d.get("prefs") or {}, prefs)
        d["updated_at"] = round(time.time(), 3)
        self._persist()
        return d

    def set_env(self, device_id: str, env: str) -> None:
        """把 auto 探测学到的实际 APNs 环境粘滞住（此后不再双发）。"""
        d = self.devices.get(device_id)
        if d is not None and env in ("prod", "sandbox") and d.get("env") != env:
            d["env"] = env
            self._persist()

    def remove(self, device_id: str) -> bool:
        if self.devices.pop(device_id, None) is not None:
            self._persist()
            return True
        return False

    def remove_by_token(self, token: str) -> None:
        """APNs 410 Unregistered：按 token 清理（device_id 未知的回收路径）。"""
        stale = [k for k, v in self.devices.items() if v.get("token") == token]
        for k in stale:
            self.devices.pop(k, None)
        if stale:
            self._persist()

    # ---------- 读取 ----------
    def all(self) -> List[Dict[str, Any]]:
        return list(self.devices.values())

    def with_pref(self, key: str) -> List[Dict[str, Any]]:
        """启用了某通知类别、且有可用 token 的设备。"""
        out = []
        for d in self.devices.values():
            if not d.get("token"):
                continue
            prefs = {**DEFAULT_PREFS, **(d.get("prefs") or {})}
            if prefs.get(key):
                out.append(d)
        return out

    def push_to_start_capable(self) -> List[Dict[str, Any]]:
        return [d for d in self.devices.values() if d.get("push_to_start_token")]
