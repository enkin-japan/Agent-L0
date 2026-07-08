"""
macOS Accessibility (AX) GUI 自动化工具集：桌面任务工具链的最后一层兜底。

工具优先级（也写入系统提示词）：能终端就终端 → 应用有脚本接口就 AppleScript
→ 两者都够不着（如无脚本接口的第三方 App）才用本组 GUI 工具。

感知以「AX 树的文本序列化」为主（剪枝后喂给模型，可交互元素带 [编号]）——原生 App 的
AX 树质量足以支撑操作；截图作为验证手段属后置项（需多模态注入管线，暂未接入）。

依赖 pyobjc（requirements.txt）。缺失或非 macOS 时本组工具整体不注册（is_available=False）。
所有操作都需要宿主进程被授予「辅助功能」权限（系统设置 → 隐私与安全性 → 辅助功能）；
未授权时工具返回清晰引导，运行期授予后无需重启即可生效（权限在执行时检查，而非注册时）。
"""
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from ..base import BaseTool

# ---- guarded 依赖导入：缺失则本组工具不可用，但模块本身可正常 import ----
_AX_AVAILABLE = False
_AX_IMPORT_ERROR = ""
try:
    if sys.platform == "darwin":
        import AppKit  # noqa: F401
        import Quartz
        from ApplicationServices import (
            AXIsProcessTrusted,
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            AXUIElementCopyActionNames,
            AXUIElementPerformAction,
            AXUIElementSetAttributeValue,
            AXUIElementSetMessagingTimeout,
            kAXChildrenAttribute,
            kAXRoleAttribute,
            kAXRoleDescriptionAttribute,
            kAXTitleAttribute,
            kAXDescriptionAttribute,
            kAXValueAttribute,
            kAXEnabledAttribute,
            kAXFocusedWindowAttribute,
            kAXMainWindowAttribute,
            kAXWindowsAttribute,
        )
        _AX_AVAILABLE = True
except Exception as e:  # pragma: no cover - 仅在依赖缺失时触发
    _AX_IMPORT_ERROR = str(e)
    _AX_AVAILABLE = False

# AX 消息超时（秒）：防止个别无响应的 App 让 AX 调用挂死，占住会话锁
_AX_TIMEOUT = 5.0
# UI 树遍历上限：AX 树原始 dump 极易超出观察截断上限，必须剪枝
_MAX_DEPTH = 18
_MAX_NODES = 400

# read_ui_tree 与动作工具之间共享的元素索引缓存。
# GUI 是单桌面全局资源，个人 Agent 场景假定同一时刻只有一个会话在做 GUI 操作，
# 因此用模块级缓存（而非按会话）：读树时记录 index→AXUIElement 引用，动作按 index 解析。
_element_cache: List[Any] = []
_cache_lock = threading.Lock()

# 有副作用即视为「可交互」的 AX 动作（据此决定是否给元素编号）
_INTERACTIVE_ACTIONS = {"AXPress", "AXConfirm", "AXPick", "AXIncrement", "AXDecrement", "AXShowMenu"}
# 即便无动作也值得展示（可读取/可能可编辑）的角色
_VALUE_ROLES = {"AXTextField", "AXTextArea", "AXStaticText", "AXComboBox", "AXSlider", "AXCheckBox"}


# ==========================================
# AX 底层辅助（依赖 pyobjc；仅在 _AX_AVAILABLE 时调用）
# ==========================================
def _permission_ok() -> bool:
    return bool(AXIsProcessTrusted())


_PERMISSION_HINT = (
    "Accessibility permission not granted. The GUI tools need it. Ask the user to open "
    "System Settings → Privacy & Security → Accessibility and enable the app running this "
    "agent (Terminal / the Python process), then retry — no restart needed."
)


def _attr(element: Any, attribute: str) -> Any:
    """安全读取一个 AX 属性；失败返回 None。"""
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
        return value if err == 0 else None
    except Exception:
        return None


def _actions(element: Any) -> List[str]:
    try:
        err, names = AXUIElementCopyActionNames(element, None)
        return list(names) if err == 0 and names else []
    except Exception:
        return []


def _frontmost_app():
    """返回 (pid, name) 或 None。免 AX 权限。"""
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return None
    return int(front.processIdentifier()), str(front.localizedName() or "")


def _app_by_name(name: str):
    """按本地化名称/bundle 名匹配一个运行中的 GUI 应用，返回 (pid, name) 或 None。"""
    key = name.strip().lower()
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() != 0:  # 仅常规 GUI 应用
            continue
        local = str(app.localizedName() or "")
        bundle = str(app.bundleIdentifier() or "")
        if key in local.lower() or key in bundle.lower():
            return int(app.processIdentifier()), local
    return None


def _target_window(pid: int):
    """取应用的目标窗口（聚焦窗口优先，退回主窗口/首个窗口）。"""
    app_el = AXUIElementCreateApplication(pid)
    try:
        AXUIElementSetMessagingTimeout(app_el, _AX_TIMEOUT)
    except Exception:
        pass
    win = _attr(app_el, kAXFocusedWindowAttribute) or _attr(app_el, kAXMainWindowAttribute)
    if win is None:
        windows = _attr(app_el, kAXWindowsAttribute)
        if windows:
            win = windows[0]
    return app_el, win


def _node_info(element: Any) -> Dict[str, Any]:
    """把一个 AX 元素抽取为剪枝所需的精简信息 dict。"""
    role = _attr(element, kAXRoleAttribute) or ""
    title = _attr(element, kAXTitleAttribute)
    desc = _attr(element, kAXDescriptionAttribute)
    value = _attr(element, kAXValueAttribute)
    enabled = _attr(element, kAXEnabledAttribute)
    actions = _actions(element)
    # value 可能是复杂对象；仅保留可读的标量文本
    value_text = value if isinstance(value, str) else ("" if value is None else str(value))
    return {
        "role": str(role),
        "name": str(title or desc or ""),
        "value": value_text[:120],
        "enabled": bool(enabled) if enabled is not None else True,
        "actions": actions,
        "interactive": bool(set(actions) & _INTERACTIVE_ACTIONS) or str(role) in _VALUE_ROLES,
    }


def _walk(element: Any, depth: int, collected: List[Tuple[int, Dict[str, Any], Any]]) -> None:
    """
    深度优先遍历 AX 树，收集 (depth, info, element)。剪枝：深度/节点上限。
    仅在到达上限时停止；info 里的 interactive 决定后续是否分配编号。
    """
    if len(collected) >= _MAX_NODES or depth > _MAX_DEPTH:
        return
    info = _node_info(element)
    collected.append((depth, info, element))
    children = _attr(element, kAXChildrenAttribute)
    if children:
        for child in children:
            if len(collected) >= _MAX_NODES:
                break
            _walk(child, depth + 1, collected)


# ==========================================
# 纯逻辑（不依赖 pyobjc，可独立测试）
# ==========================================
def _format_tree(nodes: List[Dict[str, Any]]) -> Tuple[str, int]:
    """
    把精简节点列表渲染为带缩进的文本树，给可交互元素编号（[n]）。
    返回 (文本, 分配出的可交互元素数)。nodes 每项含 depth/role/name/value/enabled/interactive。
    """
    lines: List[str] = []
    idx = 0
    for node in nodes:
        indent = "  " * node["depth"]
        role = node["role"].replace("AX", "", 1) if node["role"].startswith("AX") else node["role"]
        label = ""
        if node["interactive"]:
            marker = f"[{idx}] "
            idx += 1
        else:
            marker = ""
        name = node.get("name", "")
        if name:
            label += f' "{name}"'
        value = node.get("value", "")
        if value and value != name:
            label += f" value={value!r}"
        if not node.get("enabled", True):
            label += " (disabled)"
        lines.append(f"{indent}{marker}{role}{label}".rstrip())
    return "\n".join(lines), idx


def _parse_key_combo(combo: str) -> Tuple[Optional[int], int]:
    """
    解析 'cmd+shift+s' 这类组合键为 (修饰键掩码, 虚拟键码)。
    无法识别的主键返回 (None, 0)。纯函数，便于测试；掩码依赖 Quartz 常量，
    测试时通过注入的 _MOD_MASKS/_KEYCODES 校验解析结果。
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return None, 0
    mods = 0
    key_token = parts[-1]
    for mod in parts[:-1]:
        mask = _MOD_MASKS.get(mod)
        if mask is None:
            return None, 0
        mods |= mask
    keycode = _KEYCODES.get(key_token)
    if keycode is None:
        return None, 0
    return mods, keycode


# 虚拟键码表（ANSI 键盘）：覆盖字母、数字与常用功能键，够常见快捷键使用
_KEYCODES: Dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32,
    "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "forwarddelete": 117,
}


def _init_masks() -> Dict[str, int]:
    if not _AX_AVAILABLE:
        # 测试/无依赖环境下用占位掩码，仅保证解析逻辑可验证
        return {"cmd": 1 << 20, "command": 1 << 20, "shift": 1 << 17,
                "opt": 1 << 19, "option": 1 << 19, "alt": 1 << 19,
                "ctrl": 1 << 18, "control": 1 << 18, "fn": 1 << 23}
    return {
        "cmd": Quartz.kCGEventFlagMaskCommand, "command": Quartz.kCGEventFlagMaskCommand,
        "shift": Quartz.kCGEventFlagMaskShift,
        "opt": Quartz.kCGEventFlagMaskAlternate, "option": Quartz.kCGEventFlagMaskAlternate,
        "alt": Quartz.kCGEventFlagMaskAlternate,
        "ctrl": Quartz.kCGEventFlagMaskControl, "control": Quartz.kCGEventFlagMaskControl,
        "fn": Quartz.kCGEventFlagMaskSecondaryFn,
    }


_MOD_MASKS = _init_masks()


# ==========================================
# 工具定义
# ==========================================
class ListApplicationsTool(BaseTool):
    """列出正在运行的 GUI 应用（免 AX 权限）"""
    name = "list_applications"
    description = (
        "List the currently running GUI applications (name, pid, and which one is frontmost). "
        "Use it to see what is open before reading a window's UI or driving an app."
    )
    requires_approval = False
    is_concurrency_safe = True  # 只读枚举运行中的应用，无副作用
    parameters = {"properties": {}, "required": []}

    def is_available(self) -> bool:
        return _AX_AVAILABLE

    def execute(self) -> str:
        front = _frontmost_app()
        front_pid = front[0] if front else None
        rows = []
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if app.activationPolicy() != 0:
                continue
            pid = int(app.processIdentifier())
            name = str(app.localizedName() or "")
            mark = "  ← frontmost" if pid == front_pid else ""
            rows.append(f"- {name} (pid {pid}){mark}")
        if not rows:
            return "No GUI applications are currently running."
        return "Running GUI applications:\n" + "\n".join(rows)


class ReadUITreeTool(BaseTool):
    """读取应用聚焦窗口的 Accessibility 树（剪枝，可交互元素带编号）"""
    name = "read_ui_tree"
    description = (
        "Read the Accessibility (UI) tree of an app's focused window as pruned, indented text. "
        "Interactive elements are numbered like [3] — use that number with click_ui_element / "
        "set_ui_element_value. Read the tree BEFORE acting, and again after an action to verify "
        "the result. Pass 'app' to target a specific application, otherwise the frontmost app is used."
    )
    requires_approval = False
    # 只读（对系统无副作用，参与取代去重），但**不可并发**：读树会覆写模块级的
    # _element_cache 编号索引，同批并发两次读树会让编号↔元素错位、后续点击点错元素。
    is_concurrency_safe = False
    is_read_only = True
    parameters = {
        "properties": {
            "app": {
                "type": "string",
                "description": "Optional application name (e.g. 'Safari'). Defaults to the frontmost app."
            }
        },
        "required": []
    }

    def is_available(self) -> bool:
        return _AX_AVAILABLE

    def execute(self, app: str = "") -> str:
        if not _permission_ok():
            return _PERMISSION_HINT

        if app and app.strip():
            target = _app_by_name(app)
            if target is None:
                return f"No running GUI application matches '{app}'. Use list_applications to see options."
        else:
            target = _frontmost_app()
            if target is None:
                return "Could not determine the frontmost application."
        pid, name = target

        app_el, win = _target_window(pid)
        if win is None:
            return f"Application '{name}' (pid {pid}) has no accessible window to read."

        collected: List[Tuple[int, Dict[str, Any], Any]] = []
        _walk(win, 0, collected)

        nodes = [{"depth": d, **info} for d, info, _el in collected]
        text, count = _format_tree(nodes)

        # 缓存可交互元素的引用（按编号顺序），供动作工具解析
        with _cache_lock:
            _element_cache.clear()
            _element_cache.extend(el for _d, info, el in collected if info["interactive"])

        truncated = " (tree truncated at node limit)" if len(collected) >= _MAX_NODES else ""
        header = f"UI tree of '{name}' focused window — {count} interactive elements{truncated}:\n"
        return header + text


def _resolve_element(index: int):
    """按编号取缓存的 AX 元素引用；越界/未读树返回 (None, 提示)。"""
    with _cache_lock:
        if not _element_cache:
            return None, "No UI snapshot available. Call read_ui_tree first, then use the [n] indices."
        if index < 0 or index >= len(_element_cache):
            return None, (f"Element index {index} is out of range "
                          f"(current snapshot has {len(_element_cache)} interactive elements). "
                          "Re-read the UI tree.")
        return _element_cache[index], ""


class ClickUIElementTool(BaseTool):
    """点击 UI 树中按编号指定的元素"""
    name = "click_ui_element"
    description = (
        "Click (press) an interactive element by its [n] index from the most recent read_ui_tree "
        "output. After clicking, read the UI tree again to confirm the effect."
    )
    requires_approval = False
    parameters = {
        "properties": {
            "index": {"type": "integer", "description": "The element's [n] index from read_ui_tree."}
        },
        "required": ["index"]
    }

    def is_available(self) -> bool:
        return _AX_AVAILABLE

    def execute(self, index: int) -> str:
        if not _permission_ok():
            return _PERMISSION_HINT
        element, err = _resolve_element(int(index))
        if element is None:
            return err
        try:
            code = AXUIElementPerformAction(element, "AXPress")
        except Exception as e:
            return f"System Error: click failed: {str(e)}"
        if code != 0:
            return (f"Click on element [{index}] returned error code {code} — it may not be pressable. "
                    "Re-read the UI tree and pick a pressable element.")
        return f"Clicked element [{index}]. Read the UI tree again to verify the result."


class SetUIValueTool(BaseTool):
    """给 UI 树中按编号指定的元素设值（文本框等）"""
    name = "set_ui_element_value"
    description = (
        "Set the text value of an editable element (text field / area) by its [n] index from "
        "read_ui_tree. Use this to fill in fields directly; for free-form typing into the focused "
        "element use type_text instead."
    )
    requires_approval = False
    parameters = {
        "properties": {
            "index": {"type": "integer", "description": "The element's [n] index from read_ui_tree."},
            "value": {"type": "string", "description": "The text value to set."}
        },
        "required": ["index", "value"]
    }

    def is_available(self) -> bool:
        return _AX_AVAILABLE

    def execute(self, index: int, value: str) -> str:
        if not _permission_ok():
            return _PERMISSION_HINT
        element, err = _resolve_element(int(index))
        if element is None:
            return err
        try:
            code = AXUIElementSetAttributeValue(element, kAXValueAttribute, value)
        except Exception as e:
            return f"System Error: set value failed: {str(e)}"
        if code != 0:
            return (f"Setting value on element [{index}] returned error code {code} — "
                    "it may not be editable. Re-read the UI tree.")
        return f"Set element [{index}] value. Read the UI tree again to verify."


class TypeTextTool(BaseTool):
    """向当前聚焦元素键入文本（Unicode 键盘合成）"""
    name = "type_text"
    description = (
        "Type a string of text into whatever element currently has keyboard focus, as if typed on "
        "the keyboard. Click or focus the target field first. For keyboard shortcuts (e.g. Cmd+S) "
        "use press_keys instead."
    )
    requires_approval = False
    parameters = {
        "properties": {
            "text": {"type": "string", "description": "The text to type into the focused element."}
        },
        "required": ["text"]
    }

    def is_available(self) -> bool:
        return _AX_AVAILABLE

    def execute(self, text: str) -> str:
        if not _permission_ok():
            return _PERMISSION_HINT
        if not text:
            return "System Error: empty text."
        try:
            # 逐块用 Unicode 事件注入，避免依赖各键的虚拟键码
            for chunk_start in range(0, len(text), 20):
                chunk = text[chunk_start:chunk_start + 20]
                for down in (True, False):
                    ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        except Exception as e:
            return f"System Error: type_text failed: {str(e)}"
        return f"Typed {len(text)} characters into the focused element."


class PressKeysTool(BaseTool):
    """发送组合键（如 cmd+s、cmd+shift+t）"""
    name = "press_keys"
    description = (
        "Send a keyboard shortcut / key combination such as 'cmd+s', 'cmd+shift+t', 'return', "
        "'escape', 'cmd+c'. Modifiers: cmd, shift, opt/alt, ctrl, fn. Use this for shortcuts; "
        "for entering text use type_text."
    )
    requires_approval = False
    parameters = {
        "properties": {
            "combo": {
                "type": "string",
                "description": "Key combination, e.g. 'cmd+s' or 'return'. '+'-separated, main key last."
            }
        },
        "required": ["combo"]
    }

    def is_available(self) -> bool:
        return _AX_AVAILABLE

    def execute(self, combo: str) -> str:
        if not _permission_ok():
            return _PERMISSION_HINT
        mods, keycode = _parse_key_combo(str(combo))
        if mods is None:
            return (f"Could not parse key combo '{combo}'. Use '+'-separated tokens with the main key "
                    "last, e.g. 'cmd+shift+s'. Supported modifiers: cmd, shift, opt/alt, ctrl, fn.")
        try:
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
                if mods:
                    Quartz.CGEventSetFlags(ev, mods)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        except Exception as e:
            return f"System Error: press_keys failed: {str(e)}"
        return f"Sent key combo '{combo}'."
