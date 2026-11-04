from pathlib import Path
import importlib
import inspect
import logging
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Tuple
from .base import BaseTool

_log = logging.getLogger(__name__)

#: 实参预检（D44）tool_error 事件里 missing / unknown 名单的上限（回执文本不截）
PRECHECK_LIST_MAX = 8
#: 全量披露（full 模式）下预检回执的尾句：声明已在前缀里，只提示回去对照
PRECHECK_FULL_MODE_HINT = "\nCheck the tool's declaration and call it again."

def _accepted_keys(params: Dict[str, Any]) -> List[str]:
    """声明接受的实参名：properties 的键（声明序）+ required 里没进 properties 的键。"""
    props = params.get("properties") if isinstance(params, dict) else None
    out = [str(k) for k in props] if isinstance(props, dict) else []
    req = params.get("required") if isinstance(params, dict) else None
    if isinstance(req, (list, tuple)):
        out += [k for k in req if isinstance(k, str) and k not in out]
    return out


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

    def get_summaries(self) -> Dict[str, str]:
        """{工具名: 目录行 summary}（W-2 / D30），只含显式声明了 summary 的工具。

        与 get_all_schemas 分开给：summary 不进 schema 字典，full 模式声明逐字节不变。"""
        # 与 get_all_schemas 同样不取锁（测试与脚本会用 __new__ 造半初始化注册表）；
        # 先拍快照再遍历，热重扫并发 register 时不撞「字典遍历中被改」
        out = {}
        for name, tool in list(self._tools.items()):
            text = getattr(tool, "summary", "")
            if isinstance(text, str) and text.strip():
                out[name] = text.strip()
        return out

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

    def effect_of(self, name: str, arguments: Dict[str, Any] = None) -> str:
        """本次调用的效果分类："read" | "write"（审批策略的分档依据）。
        未知工具、钩子异常、声明值不是字面 "read" —— 一律按 "write"（保守一侧）。"""
        tool = self._tools.get(name)
        if tool is None:
            return "write"
        try:
            effect = tool.effect_for(arguments or {})
        except Exception as e:
            print(f"[MCP] ⚠️ 工具 {name} 效果分类异常，按 write 处理: {e}")
            return "write"
        return "read" if effect == "read" else "write"

    def approval_required(self, name: str, arguments: Dict[str, Any], policy) -> bool:
        """审批闸门的唯一判定（Contract P）：策略 policy[read|write] ∈ auto/default/ask。
        - 未知工具 ⇒ 恒审批（fail closed，不论策略）；
        - 钉死审批（approval_pinned）且工具本次要求审批 ⇒ 恒审批（"auto" 不推翻人的显式决定）；
        - "auto" ⇒ 免审批；"ask" ⇒ 审批；
        - "default" ⇒ 沿用 requires_approval（工具自身的静态/动态判定，build 77 行为）；
        - 取不到档位或档位非法 ⇒ 审批。
        """
        tool = self._tools.get(name)
        if tool is None:
            return True
        try:
            pinned = getattr(tool, "approval_pinned", False) is True
        except Exception as e:
            print(f"[MCP] ⚠️ 工具 {name} 钉死审批判定异常，按需审批处理: {e}")
            return True
        if pinned and self.requires_approval(name, arguments):
            return True
        try:
            mode = policy[self.effect_of(name, arguments)]
        except Exception as e:
            print(f"[MCP] ⚠️ 审批策略取值异常，按需审批处理: {e}")
            return True
        if mode == "auto":
            return False
        if mode == "ask":
            return True
        if mode == "default":
            return self.requires_approval(name, arguments)
        return True

    def argument_problems(self, name: str, arguments: Any = None) -> Tuple[List[str], List[str]]:
        """按工具**声明的** parameters（JSON schema）做确定性实参核对（D44），返回 (missing, unknown)：

        - missing = `required` 里实参缺席**或值为 None** 的键（模型常发 null；与 harness 桩
          orch_stubs.missing_required 同口径，两侧必须一致）；
        - unknown = 实参里既不在 `properties`、也不在 `required` 的键（接受集 = 二者之并：声明里只写进
          required 没写进 properties 的键也是工具要的，不能报成未知）——仅当 `properties` 是 dict 且
          `additionalProperties` 既不是 True 也不是 schema dict 时才查；工具没声明 properties
          （含空 dict——api_tool_factory 把省略的 params 规整成 `properties: {}`，其 execute(**kwargs)
          照单转发任意实参）就整个不查（无从判定）。
        未知工具、实参不是 dict、声明形态异常 → ([], [])（fail-open：交给执行路径暴露真实错误）。"""
        tool = self._tools.get(name)
        if tool is None or not (arguments is None or isinstance(arguments, dict)):
            return [], []
        args = arguments or {}
        try:
            params = getattr(tool, "parameters", None)
            if not isinstance(params, dict):
                return [], []
            req = params.get("required") or []
            missing = ([str(k) for k in req if isinstance(k, str) and args.get(k) is None]
                       if isinstance(req, (list, tuple)) else [])
            props = params.get("properties")
            extra = params.get("additionalProperties")
            unknown: List[str] = []
            if isinstance(props, dict) and props and extra is not True and not isinstance(extra, dict):
                accepted = set(_accepted_keys(params))
                unknown = [str(k) for k in args if k not in accepted]
            return missing, unknown
        except Exception as e:
            print(f"[MCP] ⚠️ 工具 {name} 声明核对异常，放行走执行路径: {e}")
            return [], []

    def validate_call(self, name: str, arguments: Dict[str, Any] = None, *,
                      schema_feedback: Optional[Callable[[str], str]] = None) -> "str | None":
        """
        审批/执行前的参数预检。返回错误文本 = 该调用注定失败，编排层就地打回模型重发（不执行、
        不进审批——绝不拿一条必败调用去打扰用户）；返回 None = 放行。只做确定性静态检查；
        预检自身异常 fail-open 放行，走正常执行路径暴露真实错误。

        两层，按序：
        1. 通用声明核对（D44，argument_problems）：缺必填 / 未知键 → 回执
           `System Error: '<名>' was not executed: missing required argument(s): 'a'; unknown
           argument(s): 'c' (accepted: x, y).`（空的那一句省略；不带 traceback）。
           schema_feedback（catalog 模式由编排层传入 = get_tool_schema 同一渲染）给了就在回执后
           追加 `Full declaration of '<名>':` + 完整声明——目录里只有名字和一句摘要，重发不该再靠猜；
           没给（full 模式，声明已在前缀里）追加一句「回去对照声明」。核对不过时**不再调**第 2 层。
        2. 工具可选实现的 validate_arguments(arguments) -> Optional[str]（只对过了第 1 层的调用）。
        """
        tool = self._tools.get(name)
        if tool is None:
            return None
        missing, unknown = self.argument_problems(name, arguments)
        if missing or unknown:
            clauses = []
            if missing:
                clauses.append("missing required argument(s): " + ", ".join(repr(k) for k in missing))
            if unknown:
                accepted = ", ".join(_accepted_keys(tool.parameters))
                clauses.append("unknown argument(s): " + ", ".join(repr(k) for k in unknown)
                               + f" (accepted: {accepted})")
            text = f"System Error: '{name}' was not executed: " + "; ".join(clauses) + "."
            fed = None
            if schema_feedback is not None:
                try:
                    fed = schema_feedback(name)
                except Exception as e:   # 渲染坏了也不能吞掉预检回执本身：退回 full 模式尾句
                    print(f"[MCP] ⚠️ 工具 {name} 预检附声明失败，退回提示句: {e}")
                    fed = None
            if fed:
                return text + f"\nFull declaration of '{name}':\n" + str(fed)
            return text + PRECHECK_FULL_MODE_HINT
        checker = getattr(tool, "validate_arguments", None)
        if not callable(checker):
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
                "effect": self.static_effect(tool),
            }
            for tool in self._tools.values()
        ]

    @staticmethod
    def static_effect(tool: BaseTool) -> str:
        """类级（不看实参的）效果分类，供清单展示；非字面 "read" 一律 "write"。"""
        return "read" if getattr(tool, "effect", "write") == "read" else "write"

    def dynamic_effect_tools(self) -> List[str]:
        """覆写了 effect_for（效果随实参变化）的已注册工具名，排序。"""
        return sorted(name for name, tool in self._tools.items()
                      if type(tool).effect_for is not BaseTool.effect_for)

    def tool_effects(self) -> Dict[str, str]:
        """{工具名: 类级效果}（GET /api/v1/approval-policy 的 tools 映射）。"""
        return {name: self.static_effect(tool) for name, tool in sorted(self._tools.items())}

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
            # 模型面只给「异常类型: 异常文本」（前 500 字符）：traceback 是实现细节、对重发参数没有
            # 信息量，还会把源码路径和内部变量名喂进上下文（D44 文本卫生）。全栈留在日志里。
            _log.warning("工具 %s 执行异常", name, exc_info=True)
            return (f"System Error: Exception occurred while executing '{name}': "
                    + f"{type(e).__name__}: {e}"[:500], None)
