from pathlib import Path
import importlib
import inspect
from threading import RLock
from typing import Dict, List, Any
from .base import BaseTool

class MCPRegistry:
    """
    MCP 工具注册中心：负责动态加载、注册、分发物理工具的执行请求。
    """
    def __init__(self, tools_dir: Path):
        self.tools_dir = tools_dir
        self._tools: Dict[str, BaseTool] = {}
        self._schemas: List[Dict[str, Any]] = []
        # 探活未过的工具实例留档（D 阶段热重扫）：依赖后装（LibreOffice 案）时经
        # rescan_unavailable 重探补注册，不必重启执行层
        self._discarded: List[BaseTool] = []
        self._candidate_lock = RLock()
        self._load_all_tools()

    def _load_all_tools(self):
        """扫描 tools_dir 目录下所有继承自 BaseTool 的插件"""
        self._tools.clear()
        self._schemas.clear()
        if not self.tools_dir.exists():
            self.tools_dir.mkdir(parents=True, exist_ok=True)
            print(f"[MCP] 工具目录 {self.tools_dir} 不存在，已自动创建。")
            return

        # 排序钉死注册顺序：schema 列表原样进提示词稳定前缀（orchestrator 不再排），
        # 而 iterdir 的目录项顺序随文件系统/同步方式（git checkout、rsync、bundle 组装）
        # 漂移——顺序一变，稳定前缀整段失效、算力端全量重 prefill（KV 缓存白攒）。
        for file in sorted(self.tools_dir.iterdir()):
            if file.name.endswith(".py") and not file.name.startswith("__"):
                module_name = file.name[:-3]

                # 以包内模块的方式导入（而非裸文件加载），保证工具模块内部的
                # 相对导入（如 from ..base import BaseTool）能正确解析父包
                full_module_name = f"{__package__}.tools.{module_name}"
                try:
                    module = importlib.import_module(full_module_name)
                    # 遍历模块中的所有成员，找到继承自 BaseTool 且不是 BaseTool 本身的类
                    for name, obj in inspect.getmembers(module):
                        if inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool:
                            tool_instance = obj()
                            # 探活检查：依赖的外部服务不可用时舍弃该工具，
                            # 避免把注定失败的工具 Schema 暴露给 LLM
                            if not tool_instance.is_available():
                                print(f"[MCP] ⛔ 工具 {tool_instance.name} 不可用（依赖服务探活失败），已舍弃。")
                                self._discarded.append(tool_instance)
                                continue
                            self.register(tool_instance)
                except Exception as e:
                    print(f"[MCP] ⚠️ 加载插件 {module_name} 失败: {e}")

    def register(self, tool: BaseTool):
        """注册单个工具实例"""
        with self._candidate_lock:
            self._tools[tool.name] = tool
            self._schemas.append(tool.get_schema())
            print(f"[MCP] ✅ 成功加载工具: {tool.name}")

    def discard_unavailable(self, name: str) -> None:
        """Withdraw pending candidates only; running instances still require restart."""
        with self._candidate_lock:
            self._discarded = [old for old in self._discarded if old.name != name]

    def queue_tool(self, tool: BaseTool) -> None:
        """Reserve the latest candidate before handing its probe to a worker."""
        with self._candidate_lock:
            self.discard_unavailable(tool.name)
            self._discarded.append(tool)

    def load_tool(self, tool: BaseTool, *, queued: bool = False) -> bool:
        """Probe a candidate and register it only while it is still current.

        API saves reserve synchronously before dispatching this method to a
        thread, so a subsequent disable/delete also revokes a not-yet-run probe.
        Availability checks run outside the lock; only candidate ownership and
        registration are atomic. No network call or tool execution is added.
        """
        if not queued:
            self.queue_tool(tool)
        try:
            ok = tool.is_available()
        except Exception as e:
            print(f"[MCP] ⚠️ 工具 {tool.name} 探活异常，视为不可用: {e}")
            ok = False
        with self._candidate_lock:
            if not any(old is tool for old in self._discarded):
                return self._tools.get(tool.name) is tool
            if not ok:
                print(f"[MCP] ⛔ 工具 {tool.name} 不可用（依赖服务探活失败），已舍弃。")
                return False
            self._discarded = [old for old in self._discarded if old is not tool]
            self.register(tool)
            return True

    def rescan_unavailable(self) -> List[str]:
        """Reprobe pending candidates; only add, never replace running instances.

        A concurrent save may replace a candidate and disable/delete may revoke
        one. Never reinsert an obsolete snapshot after its slow probe returns.
        """
        newly: List[str] = []
        with self._candidate_lock:
            candidates = list(self._discarded)
        for tool in candidates:
            try:
                ok = tool.is_available()
            except Exception as e:
                print(f"[MCP] ⚠️ 工具 {tool.name} 重探异常，仍视为不可用: {e}")
                ok = False
            with self._candidate_lock:
                if not any(old is tool for old in self._discarded):
                    continue
                if tool.name in self._tools:
                    self._discarded = [old for old in self._discarded if old is not tool]
                    continue  # Rescan only adds; running instances keep their identity.
                if ok:
                    self._discarded = [old for old in self._discarded if old is not tool]
                    self.register(tool)
                    newly.append(tool.name)
        return newly

    def tool_names(self) -> set:
        """已注册的工具名集合（skill 剧本的 requires 探活据此判定）。"""
        with self._candidate_lock:
            return set(self._tools)

    def declaration_matches(self, name: str, declaration: dict) -> "bool | None":
        """Read-only saved/runtime comparison, when supported; no availability probe."""
        matcher = getattr(self._tools.get(name), "matches_declaration", None)
        if not callable(matcher):
            return None
        try:
            result = matcher(declaration)
            return result if isinstance(result, bool) else None
        except Exception:
            return None

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """供给 LLM 的系统提示词使用的工具描述清单"""
        return self._schemas

    def requires_approval(self, name: str, arguments: Dict[str, Any] = None) -> bool:
        """
        查询某次调用是否需要用户在前端授权（按调用动态判定）。
        钩子异常时 fail-closed 按需要审批处理——宁可多弹一次窗，不放行未知风险。
        """
        tool = self._tools.get(name)
        if tool is None:
            return False
        try:
            return bool(tool.needs_approval(arguments or {}))
        except Exception as e:
            print(f"[MCP] ⚠️ 工具 {name} 审批判定异常，按需审批处理: {e}")
            return True

    def validate_call(self, name: str, arguments: Dict[str, Any] = None) -> "str | None":
        """
        审批/执行前的参数预检（工具可选实现 validate_arguments(arguments) -> Optional[str]）。
        返回错误文本 = 该调用注定失败，编排层就地打回模型重发（不执行、不进审批——
        绝不拿一条必败调用去打扰用户）；返回 None = 放行。只做确定性静态检查；
        预检自身异常 fail-open 放行，走正常执行路径暴露真实错误。
        """
        tool = self._tools.get(name)
        checker = getattr(tool, "validate_arguments", None)
        if tool is None or not callable(checker):
            return None
        try:
            err = checker(arguments or {})
            return str(err) if err else None
        except Exception as e:
            print(f"[MCP] ⚠️ 工具 {name} 参数预检异常，放行走执行路径: {e}")
            return None

    def is_concurrency_safe(self, name: str, arguments: Dict[str, Any] = None) -> bool:
        """
        查询某次调用是否可与同批其他安全调用并发执行（编排层据此把连续安全段并发跑）。
        未知工具或判定异常时 fail-safe 返回 False（保守串行，绝不误并发有副作用的调用）。
        `is True` 收紧：属性若被误写成方法/真值对象不会被当成安全。
        """
        tool = self._tools.get(name)
        if tool is None:
            return False
        try:
            return getattr(tool, "is_concurrency_safe", False) is True
        except Exception:
            return False

    def tool_timeout(self, name: str) -> "int | None":
        """查询工具级超时覆盖（秒）。未声明/非法值返回 None（编排层回落全局值）。"""
        tool = self._tools.get(name)
        if tool is None:
            return None
        try:
            t = getattr(tool, "timeout_seconds", None)
            if t is None:
                return None
            t = int(t)
            return t if t > 0 else None
        except (TypeError, ValueError):
            return None

    def is_read_only(self, name: str, arguments: Dict[str, Any] = None) -> bool:
        """
        查询某次调用是否只读（对系统状态无副作用）。驱动压缩层取代去重的 _safe/_target
        元数据与「写屏障」判定。工具未显式声明 is_read_only 时跟随 is_concurrency_safe。
        """
        tool = self._tools.get(name)
        if tool is None:
            return False
        try:
            ro = getattr(tool, "is_read_only", None)
            if ro is not None:
                return ro is True
            return getattr(tool, "is_concurrency_safe", False) is True
        except Exception:
            return False

    def list_tools(self) -> List[Dict[str, Any]]:
        """已注册工具的完整明细（供前端上下文面板展示，含审批标记）"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "properties": tool.parameters.get("properties", {}),
                    "required": tool.parameters.get("required", []),
                },
                "requires_approval": tool.requires_approval,
            }
            for tool in self._tools.values()
        ]

    def execute_tool(self, name: str, arguments: dict) -> "tuple[str, Dict[str, Any]]":
        """核心分发逻辑：纯粹的物理调用。返回 `(给模型的文本, 渲染格 或 None)`。

        注：这里不再拦截 _system_format_error，工具格式的解析容错将由上层 Engine 处理。

        渲染格（R3）：工具返回 ToolOutput 且声明了 render_hint 时，组装成
        `{hint, payload}` 一并交回；否则为 None（= 客户端画现状的灰框）。
        **fail-open**：渲染是锦上添花，组装出问题只降级灰框，绝不能让一次已经成功的
        工具调用看起来像失败（与 is_read_only 的 fail-safe 同一路数：坏了也别把主链路
        带下水）。故此处只信 tool 自己给的载荷，不做任何可能抛错的加工。
        """
        if name not in self._tools:
            return (f"System Error: No tool named '{name}' was found in the MCP registry.", None)

        try:
            tool = self._tools[name]
            result = tool.execute(**arguments)
            # 统一转为字符串返回，对大语言模型更友好（ToolOutput.__str__ 即其 text）
            text = str(result)
            payload = getattr(result, "payload", None)
            hint = getattr(tool, "render_hint", None)
            render = {"hint": str(hint), "payload": payload} if (hint and payload is not None) else None
            return (text, render)
        except Exception as e:
            import traceback
            # 返回明确的 Traceback 帮助大模型理解工具为什么崩溃
            return (f"System Error: Exception occurred while executing '{name}': {str(e)}\n{traceback.format_exc()}",
                    None)
