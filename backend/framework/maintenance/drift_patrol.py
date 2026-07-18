"""依赖时效巡检（drift patrol，phase 1：只报不修）。

对抗**活的外部服务**的依赖（yt-dlp/SearXNG/curl_cffi/playwright+Chrome）会在代码
一行不动的情况下被世界改坏，且多半是**静默失效**（搜索结果慢慢变差、下载偶发报错）。
本模块把失效变成显式信号：声明了 canary() 的工具（见 mcp/base.py）在空闲维护回合
逐项轮巡，连续失败达阈值就写一条告警笔记——笔记索引每轮注入提示词，agent 下次对话
自然会告诉用户「X 疑似过时，这样修」。

红线（沿记忆治理波）：
- 只报不修——巡检**永不**升级/改动任何依赖；修复指引（drift_fix）写给用户看。
- 巡检只动自己名下的笔记（NOTE_PREFIX 前缀），恢复时也只清这个前缀的；
  笔记无自动抢救标记，dream 的 DROP 护栏天然不许删它。
- 一切失败 fail-open：巡检坏了只损失一次探测，绝不掀翻维护循环。

节流：单项两次探测至少隔 min_hours（默认 72h），每个空闲回合最多探一项（canary 是
真实网络 IO，不该在用户设备上密集打外网）；单次失败不告警（fail_threshold 默认 2），
防网络抖动误报。
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

STATE_FILE = "drift_patrol.json"
NOTE_PREFIX = "drift-"
DEFAULT_MIN_HOURS = 72.0
DEFAULT_FAIL_THRESHOLD = 2
DEFAULT_CANARY_TIMEOUT = 90.0


def _note_name(tool_name: str) -> str:
    """工具名转笔记 slug（工具名带下划线，NoteStore 只收 kebab-case）。"""
    return NOTE_PREFIX + tool_name.replace("_", "-").lower()


# ---------- 状态（DATA_DIR/maintenance/drift_patrol.json） ----------

def read_state(state_dir: Path) -> Dict[str, dict]:
    """{tool_name: {last_at, ok, fails, detail}}；缺失/损坏 = 空（全员待巡）。"""
    try:
        return dict(json.loads((Path(state_dir) / STATE_FILE).read_text(encoding="utf-8")))
    except Exception:
        return {}


def write_state(state_dir: Path, state: Dict[str, dict]) -> None:
    try:
        d = Path(state_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    except OSError as e:
        print(f"[DriftPatrol] ⚠️ 状态落盘失败（本次结果丢失，下轮重探）: {e}")


# ---------- 选目标 ----------

def patrol_targets(registry) -> List:
    """声明即纳入：覆写了 canary() 的已注册工具。新工具带声明自动入巡，无人工清单。"""
    from ..mcp.base import BaseTool
    out = []
    for tool in getattr(registry, "_tools", {}).values():
        if type(tool).canary is not BaseTool.canary:
            out.append(tool)
    return out


def select_target(tools: List, state: Dict[str, dict], min_hours: float):
    """到期者中挑上次探测最久远的一个；全员新鲜 = None。缺戳 = 从未探过，最优先。"""
    now = time.time()
    due = [t for t in tools
           if now - float(state.get(t.name, {}).get("last_at", 0)) >= min_hours * 3600]
    if not due:
        return None
    return min(due, key=lambda t: float(state.get(t.name, {}).get("last_at", 0)))


# ---------- 告警面（NoteStore 笔记 = 索引注入提示词的现成抵达路径） ----------

def _report(tool, ok: bool, fails: int, detail: str, threshold: int) -> None:
    """连败达阈值写告警笔记；恢复即清。只碰 NOTE_PREFIX 名下的笔记（红线）。"""
    from ..agents import runtime
    store = runtime.get_note_store()
    if store is None:
        return
    name = _note_name(tool.name)
    try:
        if ok:
            if store.get(name) is not None:
                store.delete(name)
                print(f"[DriftPatrol] {tool.name} 恢复正常，告警笔记已清除")
            return
        if fails < threshold:
            return                          # 单次失败先记账不吵人（防网络抖动误报）
        day = time.strftime("%Y-%m-%d")
        fix = f"\n\n修复指引：{tool.drift_fix}" if getattr(tool, "drift_fix", "") else ""
        store.write(
            name,
            f"[巡检] {tool.name} 依赖疑似过时（{day} 连续 {fails} 次探针失败）",
            "project",
            f"drift patrol 探针连续 {fails} 次失败（{day}）。最近一次：{detail}\n"
            f"该工具对抗外部活服务（{getattr(tool, 'drift_class', '?')}），版本不动也会被"
            f"外部变化改坏；请告知用户并建议更新。{fix}",
        )
        print(f"[DriftPatrol] ⚠️ {tool.name} 连续 {fails} 次探针失败，已写告警笔记 {name}")
    except Exception as e:
        print(f"[DriftPatrol] ⚠️ 告警笔记读写失败（不影响巡检记账）: {e}")


# ---------- 主入口（app.py 空闲维护回合，全系统安静时调用） ----------

async def maybe_patrol(registry, cfg: dict, state_dir: Path) -> Optional[Tuple[str, bool, str]]:
    """探最多一项；无到期项/未启用返回 None，否则返回 (tool_name, ok, detail)。"""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return None
    min_hours = float(cfg.get("min_hours", DEFAULT_MIN_HOURS))
    threshold = int(cfg.get("fail_threshold", DEFAULT_FAIL_THRESHOLD))
    timeout = float(cfg.get("canary_timeout", DEFAULT_CANARY_TIMEOUT))

    tools = patrol_targets(registry)
    if not tools:
        return None
    state = read_state(state_dir)
    target = select_target(tools, state, min_hours)
    if target is None:
        return None

    try:
        res = await asyncio.wait_for(asyncio.to_thread(target.canary), timeout=timeout)
        ok, detail = (bool(res[0]), str(res[1])) if isinstance(res, tuple) else (False, f"探针返回值形状非法: {res!r}")
    except asyncio.TimeoutError:
        ok, detail = False, f"探针超时（>{timeout:.0f}s）"
    except Exception as e:
        ok, detail = False, f"探针异常（{type(e).__name__}: {e}）"

    prev = state.get(target.name, {})
    fails = 0 if ok else int(prev.get("fails", 0)) + 1
    state[target.name] = {"last_at": time.time(), "ok": ok, "fails": fails,
                          "detail": detail[:500]}
    write_state(state_dir, state)
    _report(target, ok, fails, detail, threshold)
    print(f"[DriftPatrol] {target.name}: {'✅' if ok else '❌'} {detail[:120]}")
    return (target.name, ok, detail)
