from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseTool(ABC):
    """
    MCP 标准工具基类。
    所有接入系统的工具都必须继承此类，并实现 execute 方法与定义元数据。
    """
    
    # 工具名称（必须全局唯一，如 "rag_search"）
    name: str = ""
    # 工具描述（告诉 LLM 什么情况下该调用它）
    description: str = ""
    # JSON Schema 格式的参数定义
    parameters: Dict[str, Any] = {}
    # 安全属性：标识执行该工具前是否需要人工/系统审批
    requires_approval: bool = False
    # 并发安全：可与同批其他安全调用并发执行（无副作用且可重入）设 True。
    # 默认 False（保守串行）；有状态或有副作用的工具（GUI 变更、写库、shell）保持 False。
    is_concurrency_safe: bool = False
    # 只读（对系统状态无副作用）：驱动压缩层的取代去重元数据(_safe/_target)与
    # 「写屏障后解禁复读」的指纹分类。None = 跟随 is_concurrency_safe（多数工具二者一致）；
    # 仅当工具「只读但不可并发」（如 read_ui_tree：读快照但写共享索引缓存）才需显式设 True。
    is_read_only: bool = None
    # 工具级超时覆盖（秒）：None = 用全局 agent.tool_timeout_seconds。长任务工具
    # （excel_research 等多轮子 agent）设为自身 total_timeout + 余量；覆盖值只改
    # 编排层对本工具的等待上限，其他工具不受影响。
    timeout_seconds: int = None

    def needs_approval(self, arguments: Dict[str, Any]) -> bool:
        """
        按调用动态审批钩子：引擎在每次调用前带实参询问。
        默认返回类属性 requires_approval（静态审批，现有工具行为不变）；
        风险随参数变化的工具（如 shell：只读命令放行、变更命令弹窗）应覆写本方法。
        """
        return self.requires_approval

    def is_available(self) -> bool:
        """
        探活钩子：注册前由 MCPRegistry 调用。
        依赖外部服务的工具应覆写此方法；返回 False 时该工具将被舍弃，
        不会注册进会话（LLM 也不会看到它的 Schema）。
        """
        return True

    def get_schema(self) -> Dict[str, Any]:
        """
        生成工具声明的 JSON Schema。
        扁平格式（顶层即 name/description/parameters），与编排层元工具
        （update_plan/complete_step）一致：全部工具声明形态统一，模型不再看到两种混发格式；
        catalog 模式与 get_tool_schema 按 name 索引也直接可用。
        """
        if not self.name or not self.description:
            raise ValueError(f"Tool {self.__class__.__name__} must define 'name' and 'description'")

        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters.get("properties", {}),
                "required": self.parameters.get("required", [])
            }
        }

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        """
        物理层执行逻辑。
        接收模型传入的字典参数解包，返回执行结果（必须返回字符串格式以利于 LLM 理解上下文）。
        """
        pass