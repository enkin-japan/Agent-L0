from pathlib import Path
import importlib
import inspect
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
        self._load_all_tools()

    def _load_all_tools(self):
        """扫描 tools_dir 目录下所有继承自 BaseTool 的插件"""
        self._tools.clear()
        self._schemas.clear()
        if not self.tools_dir.exists():
            self.tools_dir.mkdir(parents=True, exist_ok=True)
            print(f"[MCP] 工具目录 {self.tools_dir} 不存在，已自动创建。")
            return

        for file in self.tools_dir.iterdir():
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
                                continue
                            self.register(tool_instance)
                except Exception as e:
                    print(f"[MCP] ⚠️ 加载插件 {module_name} 失败: {e}")

    def register(self, tool: BaseTool):
        """注册单个工具实例"""
        self._tools[tool.name] = tool
        self._schemas.append(tool.get_schema())
        print(f"[MCP] ✅ 成功加载工具: {tool.name}")

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

    def execute_tool(self, name: str, arguments: dict) -> str:
        """
        核心分发逻辑：纯粹的物理调用。
        注：这里不再拦截 _system_format_error，工具格式的解析容错将由上层 Engine 处理。
        """
        if name not in self._tools:
            return f"System Error: No tool named '{name}' was found in the MCP registry."
        
        try:
            tool = self._tools[name]
            result = tool.execute(**arguments)
            # 统一转为字符串返回，对大语言模型更友好
            return str(result)
        except Exception as e:
            import traceback
            # 返回明确的 Traceback 帮助大模型理解工具为什么崩溃
            return f"System Error: Exception occurred while executing '{name}': {str(e)}\n{traceback.format_exc()}"