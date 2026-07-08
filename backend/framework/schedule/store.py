"""定时任务的持久层：schedules/{id}/schedule.json（定义）+ runs.jsonl（运行历史）。

落盘在 sessions/ 的同级目录（不放其内部——避免会话目录扫描/驱逐逻辑把它当会话）。
原子写（tmp+rename），对齐 session.py 落盘习惯。threading.Lock 保护：写方来自
事件循环（tick/路由经 to_thread）与 MCP 工具线程两侧。

spec 用结构化字段而非 cron 表达式——弱模型经约束采样产出结构化 JSON 远比 cron
字符串可靠，UI 渲染也直白。四种：
  {"type": "interval", "minutes": N}
  {"type": "daily",    "at": "HH:MM"}
  {"type": "weekly",   "weekday": 0-6, "at": "HH:MM"}   # 0=Monday
  {"type": "once",     "at": "YYYY-MM-DD HH:MM"}        # 触发后自动停用（保历史）
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

SPEC_TYPES = ("interval", "daily", "weekly", "once")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class ScheduleSpecError(ValueError):
    """spec 校验失败：消息面向模型可喂（指出哪个字段错、给出正确示例）。"""


def _parse_hhmm(value: Any) -> tuple:
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    raise ScheduleSpecError(
        f"'at' must be 24h \"HH:MM\" (e.g. \"08:00\", \"21:30\"), got {value!r}.")


def validate_spec(spec: Any, min_interval_minutes: int = 5) -> Dict[str, Any]:
    """校验并归一化 spec。失败抛 ScheduleSpecError（模型可据报错自纠重试）。"""
    if not isinstance(spec, dict):
        raise ScheduleSpecError(
            'spec must be an object like {"type": "daily", "at": "08:00"}.')
    stype = spec.get("type")
    if stype not in SPEC_TYPES:
        raise ScheduleSpecError(
            f"spec.type must be one of {list(SPEC_TYPES)}, got {stype!r}. Examples: "
            '{"type": "interval", "minutes": 30} / {"type": "daily", "at": "08:00"} / '
            '{"type": "weekly", "weekday": 0, "at": "09:00"} (0=Monday) / '
            '{"type": "once", "at": "2026-07-09 15:00"}.')

    if stype == "interval":
        try:
            minutes = int(spec.get("minutes"))
        except Exception:
            raise ScheduleSpecError('interval spec needs integer "minutes", e.g. {"type": "interval", "minutes": 30}.')
        if minutes < min_interval_minutes:
            raise ScheduleSpecError(
                f"interval minutes must be >= {min_interval_minutes} (got {minutes}).")
        return {"type": "interval", "minutes": minutes}

    if stype == "daily":
        h, m = _parse_hhmm(spec.get("at"))
        return {"type": "daily", "at": f"{h:02d}:{m:02d}"}

    if stype == "weekly":
        try:
            weekday = int(spec.get("weekday"))
            assert 0 <= weekday <= 6
        except Exception:
            raise ScheduleSpecError('weekly spec needs "weekday" 0-6 (0=Monday), e.g. {"type": "weekly", "weekday": 4, "at": "17:00"}.')
        h, m = _parse_hhmm(spec.get("at"))
        return {"type": "weekly", "weekday": weekday, "at": f"{h:02d}:{m:02d}"}

    # once
    raw = str(spec.get("at", "")).strip()
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except Exception:
        raise ScheduleSpecError(
            f'once spec needs "at" as "YYYY-MM-DD HH:MM" (e.g. "2026-07-09 15:00"), got {raw!r}.')
    if dt.timestamp() <= time.time():
        raise ScheduleSpecError(f'once time "{raw}" is already in the past — pick a future time.')
    return {"type": "once", "at": raw}


def compute_next_run(spec: Dict[str, Any], after: float) -> Optional[float]:
    """严格晚于 after 的下一次触发时刻。once 已过期则 None（触发后即停用）。"""
    stype = spec["type"]
    if stype == "interval":
        return after + spec["minutes"] * 60

    if stype == "once":
        ts = datetime.strptime(spec["at"], "%Y-%m-%d %H:%M").timestamp()
        return ts if ts > after else None

    h, m = _parse_hhmm(spec["at"])
    base = datetime.fromtimestamp(after)
    candidate = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if stype == "daily":
        if candidate.timestamp() <= after:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    # weekly
    delta_days = (spec["weekday"] - candidate.weekday()) % 7
    candidate += timedelta(days=delta_days)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=7)
    return candidate.timestamp()


def describe_spec(spec: Dict[str, Any]) -> str:
    """spec 的一行人话（英文，进模型回执；前端另有 i18n 渲染）。"""
    stype = spec["type"]
    if stype == "interval":
        return f"every {spec['minutes']} minutes"
    if stype == "daily":
        return f"every day at {spec['at']}"
    if stype == "weekly":
        return f"every {_WEEKDAYS[spec['weekday']]} at {spec['at']}"
    return f"once at {spec['at']}"


class ScheduleStore:
    """定时任务定义与运行历史的落盘仓库（进程内单例，经 set_store 注入）。"""

    def __init__(self, base_dir: Path, run_history_keep: int = 50,
                 min_interval_minutes: int = 5):
        self.base_dir = Path(base_dir)
        self.run_history_keep = int(run_history_keep)
        self.min_interval_minutes = int(min_interval_minutes)
        self._lock = threading.Lock()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 内部 IO（持锁调用） ----------
    def _def_path(self, schedule_id: str) -> Path:
        return self.base_dir / schedule_id / "schedule.json"

    def _runs_path(self, schedule_id: str) -> Path:
        return self.base_dir / schedule_id / "runs.jsonl"

    def _write_def(self, task: Dict[str, Any]) -> None:
        p = self._def_path(task["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    def _read_def(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        p = self._def_path(schedule_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Schedule] schedule.json 损坏，跳过 {schedule_id}: {e}")
            return None

    # ---------- CRUD ----------
    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for d in sorted(self.base_dir.iterdir()) if self.base_dir.exists() else []:
                if d.is_dir():
                    task = self._read_def(d.name)
                    if task is not None:
                        out.append(task)
            out.sort(key=lambda t: t.get("created_at", 0))
            return out

    def get(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read_def(schedule_id)

    def create(self, name: str, prompt: str, spec: Dict[str, Any],
               created_by: str = "") -> Dict[str, Any]:
        name = str(name or "").strip()
        prompt = str(prompt or "").strip()
        if not name:
            raise ScheduleSpecError('"name" must be a non-empty short title for the task.')
        if not prompt:
            raise ScheduleSpecError('"prompt" must be the non-empty instruction to execute on each run.')
        spec = validate_spec(spec, self.min_interval_minutes)
        now = time.time()
        task = {
            "id": f"sch_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}",
            "name": name,
            "prompt": prompt,
            "spec": spec,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            "created_by_session": created_by or "",
            "next_run": compute_next_run(spec, now),
        }
        with self._lock:
            self._write_def(task)
        return task

    def update(self, schedule_id: str, *, name: Optional[str] = None,
               prompt: Optional[str] = None, spec: Optional[Dict[str, Any]] = None,
               enabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """部分字段更新。spec 或 enabled=True 变更会从当前时刻重算 next_run。"""
        if spec is not None:
            spec = validate_spec(spec, self.min_interval_minutes)
        with self._lock:
            task = self._read_def(schedule_id)
            if task is None:
                return None
            if name is not None and str(name).strip():
                task["name"] = str(name).strip()
            if prompt is not None and str(prompt).strip():
                task["prompt"] = str(prompt).strip()
            respec = False
            if spec is not None:
                task["spec"] = spec
                respec = True
            if enabled is not None:
                if bool(enabled) and not task["enabled"]:
                    respec = True  # 重新启用：从现在起算，防止过期 next_run 立刻触发
                task["enabled"] = bool(enabled)
            if respec:
                task["next_run"] = compute_next_run(task["spec"], time.time())
            task["updated_at"] = time.time()
            self._write_def(task)
            return task

    def delete(self, schedule_id: str) -> bool:
        import shutil
        with self._lock:
            d = self.base_dir / schedule_id
            if not self._def_path(schedule_id).exists():
                return False
            shutil.rmtree(d, ignore_errors=True)
            return True

    # ---------- 触发推进与运行历史 ----------
    def advance_after_fire(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """触发后推进 next_run（从当前时刻起算——睡眠错过的多次只补跑这一次）。
        once 型触发后自动停用（保历史不删除）。"""
        with self._lock:
            task = self._read_def(schedule_id)
            if task is None:
                return None
            if task["spec"]["type"] == "once":
                task["next_run"] = None
                task["enabled"] = False
            else:
                task["next_run"] = compute_next_run(task["spec"], time.time())
            task["updated_at"] = time.time()
            self._write_def(task)
            return task

    def append_run(self, schedule_id: str, run: Dict[str, Any]) -> None:
        with self._lock:
            p = self._runs_path(schedule_id)
            if not p.parent.exists():
                return  # 任务已被删除：运行记录无处可写，静默丢弃
            lines = []
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()
            lines.append(json.dumps(run, ensure_ascii=False, default=str))
            if len(lines) > self.run_history_keep:
                lines = lines[-self.run_history_keep:]
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(p)

    def read_runs(self, schedule_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """最近的运行记录，新在前。"""
        with self._lock:
            p = self._runs_path(schedule_id)
            if not p.exists():
                return []
            out = []
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            return list(reversed(out))[: max(1, int(limit))]


# ---------- 进程级仓库句柄（MCP 工具经此取用，对齐 agents/runtime.py 惯例） ----------
_STORE: Optional[ScheduleStore] = None


def set_store(store: ScheduleStore) -> None:
    """启动装配时注入（app.py lifespan，须先于 MCPRegistry 构造）。"""
    global _STORE
    _STORE = store


def get_store() -> Optional[ScheduleStore]:
    return _STORE
