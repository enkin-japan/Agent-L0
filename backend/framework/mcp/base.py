from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class ToolOutput:
    """工具产出：给模型的文本 + 给客户端的渲染载荷（R3）。

    `execute` 照旧可以直接返回 str（存量 17 个工具一个都不用改）；只有需要渲染格的
    工具改返回本类。**载荷在结构还活着的地方产出**——工具把结构化数据格式化成给模型
    读的文本时，score / source / 引文列表这些结构就死了，让下游从格式化文本里再解析
    回来既脆又蠢（分隔符撞上正文就散架）。

    两条随之而来的好处：
    - 载荷天然**不吃观察截断**（截断只作用于 text，护的是模型上下文）；
    - 载荷天然**不进模型上下文**（白名单隔开，D.12），故可以放模型不必看的体量。
    """

    __slots__ = ("text", "payload")

    def __init__(self, text: str, payload: Optional[Dict[str, Any]] = None):
        self.text = str(text)
        self.payload = payload

    def __str__(self) -> str:
        return self.text


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
    # 审批策略分类（build 78 approval policy）："read" = 只读取/检索/列举、进程外无副作用；
    # 其余一律 "write"（写、删、发送、通知、UI 动作、出网咨询……）。默认 "write" = 新工具
    # 忘了声明时落在保守一侧。用户的 read/write 两档旋钮按此分类生效，见
    # framework/core/approval_policy.py 与 MCPRegistry.approval_required。
    effect: str = "write"
    # 钉死的人工审批：用户/配置对本工具显式要求过审批（API 声明里写了 requires_approval、
    # cloud.require_approval=true）时为 True。钉死且本次调用需审批 ⇒ 任何策略档位都审批，
    # "auto" 不得推翻人的显式决定（"ask" 本就更严）。
    approval_pinned: bool = False
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
    # 渲染格（R3）：本工具的结果该被画成什么。chart | table | slides | code | diff |
    # citations | text；None = 不声明 = 客户端画现状的灰框（故接入是零破坏的，工具可
    # 逐个迁移）。hint 是**账本里的数据**，不是渲染代码：后端只说"这是什么"，三端各自
    # 决定"怎么画"（铁律 4）。工具直接返回 UI 组件在本项目物理不可能——后端 Python、
    # web TS、原生 Swift 三种语言。
    render_hint: str = None
    # 依赖漂移巡检（drift patrol，phase 1 只报不修）。对抗**活的外部服务**的工具
    # （yt-dlp 之于视频站、SearXNG 之于搜索引擎）会因外部变化而在版本不动的情况下失效，
    # 这类工具声明 drift_class="adversarial" 并覆写 canary()，空闲维护回合逐项轮巡；
    # 世界不动就不坏的依赖（mlx/chromadb 等）保持默认 frozen，永不入巡检。
    # drift_fix：canary 失败时给用户的一句修复指引（写进告警笔记正文）。
    drift_class: str = "frozen"    # adversarial | api | frozen
    drift_fix: str = ""

    def needs_approval(self, arguments: Dict[str, Any]) -> bool:
        """
        按调用动态审批钩子：引擎在每次调用前带实参询问。
        默认返回类属性 requires_approval（静态审批，现有工具行为不变）；
        风险随参数变化的工具（如 shell：只读命令放行、变更命令弹窗）应覆写本方法。
        """
        return self.requires_approval

    def effect_for(self, arguments: Dict[str, Any] = None) -> str:
        """按调用的效果分类（"read" | "write"）。默认返回类属性 effect；
        效果随参数变化的工具（shell：只读命令 = read）覆写本方法。"""
        return self.effect

    def is_available(self) -> bool:
        """
        探活钩子：注册前由 MCPRegistry 调用。
        依赖外部服务的工具应覆写此方法；返回 False 时该工具将被舍弃，
        不会注册进会话（LLM 也不会看到它的 Schema）。
        """
        return True

    def canary(self):
        """真实功能探针（漂移巡检用，可选覆写）：做一次最小的**真实功能**验证
        （is_available 只测「在不在」，canary 测「还灵不灵」——yt-dlp 装着但提取器
        失效时前者过后者不过）。返回 (ok: bool, detail: str)；未覆写 = 不入巡检。
        允许阻塞网络 IO（巡检方在线程里跑并带熔断）；异常按失败计，不必自兜。"""
        return None

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