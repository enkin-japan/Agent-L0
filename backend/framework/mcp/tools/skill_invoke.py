"""skill_invoke：取一份流程剧本，正文交回主轮次（EXECUTION_LEDGER R2a）。

**注入路径**：本工具把剧本正文作为 tool 响应返回——正文即随 tool 响应落进主编排轮的
上下文，模型随后用**主循环的工具**照做。没有子会话、没有自带工具集，故审批闸门
（`orchestrator.py:551`）照常对剧本里的每一次工具调用生效：**零新增提权面**（D.11）。

**R2a = 显式调用**：剧本目录不进系统提示，本工具的描述也刻意不罗列剧本用途——只在
用户点名时才调。让模型自行浏览挑选 = R2b，挂 `DEFERRED.md`：e4b 上剧本一多大概率
选不准，而选错剧本的症状是「按错流程认真跑完」，比不用剧本更糟。
"""
from ..base import BaseTool
from ...agents import runtime


class SkillInvokeTool(BaseTool):
    name = "skill_invoke"
    description = (
        "Load the playbook for a skill the USER explicitly named (e.g. they said "
        "\"use the deck skill\" or \"/deck\"). Returns the playbook text; follow it "
        "with your normal tools. Do NOT call this to browse or to guess which skill "
        "might help — only when the user names one."
    )
    requires_approval = False
    is_concurrency_safe = True
    is_read_only = True
    parameters = {
        "properties": {
            "name": {"type": "string",
                     "description": "Exact skill name the user asked for."},
        },
        "required": ["name"],
    }

    def is_available(self) -> bool:
        """没有可用剧本就不注册——不给模型一个必然报错的工具。

        注：目录扫描会先撞见本工具一次，那时剧本表尚未装配（本工具的探活要看剧本表，
        而剧本表的 requires 探活要看工具表——两表互为前置，只能两段装配）。故那一轮
        必然跳过，随后由 app.py 在剧本表就绪后经 registry.load_tool 补注册。
        """
        reg = runtime.get_skill_registry()
        if reg is None:
            print("[MCP-skill_invoke] 剧本表尚未装配（两表互为前置），本轮跳过，"
                  "稍后由 app.py 补注册。")
            return False
        if not reg.names():
            print("[MCP-skill_invoke] ⚠️ 无可用剧本，工具不注册。")
            return False
        return True

    def validate_arguments(self, arguments: dict) -> "str | None":
        """未知剧本名就地打回（编排层不执行、不审批），并回显可用名——错名是模型手滑，
        让它重发比返回一段错误正文强。"""
        reg = runtime.get_skill_registry()
        if reg is None:
            return None                       # 探活已挡；此处 fail-open 走执行路径暴露真错
        name = str(arguments.get("name", "")).strip()
        if reg.get(name) is None:
            return (f"Unknown skill {name!r}. Available: {', '.join(reg.names())}. "
                    "Ask the user which one they meant instead of guessing.")
        return None

    def execute(self, name: str) -> str:
        reg = runtime.get_skill_registry()
        if reg is None:
            return "System Error: skill registry unavailable."
        skill = reg.get(str(name).strip())
        if skill is None:
            return (f"Unknown skill {name!r}. Available: {', '.join(reg.names())}.")
        return (f"Playbook for skill '{skill.name}' — follow it for this task:\n\n"
                f"{skill.body}")
