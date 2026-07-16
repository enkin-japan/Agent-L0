"""护栏事件回流（guardrail event stream）。

每次护栏触发/纠偏记一条结构化事件（JSONL 追加），用途：
- 涣散度量：基准复跑按会话聚合事件，替代事后扫 history 文本标记；
- 31b 验收：e4b 上触发护栏的场景集合，模型升级后复跑对比触发率，
  即模型层搁置项的自动验收标准；
- 线上归因：哪类护栏在哪些会话高频触发，定位提示词/工具描述薄弱点。

设计：fire-and-forget——写入永不抛异常、永不阻塞主流程；单文件追加 + 线程锁
（编排在事件循环线程、工具在 to_thread 线程、约束采样在推理线程，都会来写）。
"""
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
# 落盘在可写数据根而非代码树（R5-4/D.16）：dev 缺省两者相同（backend/），
# app 模式下代码树在签名的 bundle 里，写进去会破坏签名。
from ..utils.config_loader import data_dir
_PATH = data_dir() / "guardrail_events.jsonl"


def set_path(path) -> None:
    """自定义落盘位置（app 启动注入 / 测试隔离用）。"""
    global _PATH
    _PATH = Path(path)


def get_path() -> Path:
    return _PATH


def emit(event_type: str, session: Optional[str] = None, **detail: Any) -> None:
    """记一条护栏事件。detail 值统一压成可 JSON 的短字段（长文本截断到 300 字符）。"""
    try:
        rec: dict = {
            "ts": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": event_type,
            "session": session,
        }
        if detail:
            rec["detail"] = {
                k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300])
                for k, v in detail.items()
            }
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK, open(_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 事件流是仪表，绝不能反过来影响主流程
