"""skill_invoke：取一份流程剧本，正文交回主轮次（EXECUTION_LEDGER R2a）。

**注入路径**：本工具把剧本正文作为 tool 响应返回——正文即随 tool 响应落进主编排轮的
上下文，模型随后用**主循环的工具**照做。没有子会话、没有自带工具集，故审批闸门
（`orchestrator.py:551`）照常对剧本里的每一次工具调用生效：**零新增提权面**（D.11）。

**R2b 全开（D.17，2026-08-09）**：工具描述内嵌剧本索引（name+description，装配期
从剧本表生成），模型在任务明确匹配某剧本用途时**自主调用**，用户点名时照常必调。
e4b 时代的「只认点名」禁令撤销——31B 档口测已证：守禁能力在、手工路线自发复刻剧本
步骤（#77）。选错剧本的历史顾虑由索引 description 质量与 31B 判断力兜底；错配症状
（按错流程认真跑完）若在弱模型上复发，回退开关=恢复旧描述（git 可查）。
"""
from ..base import BaseTool
from ...agents import runtime


class SkillInvokeTool(BaseTool):
    name = "skill_invoke"
    _DESC_HEAD = (
        "Load the playbook for a skill and follow it with your normal tools. Call this "
        "when the user names a skill (e.g. \"use the deck skill\" or \"/deck\"), OR "
        "when the task clearly matches one of the skills listed below — invoke it BEFORE "
        "improvising your own workflow. Skip it when no listed skill clearly fits."
    )
    description = _DESC_HEAD  # 兜底文案；装配期在 __init__ 里追加剧本索引（R2b 全开）

    def __init__(self):
        # 两表互为前置：首轮目录扫描时剧本表未装配（is_available 会挡下），app.py 在
        # 剧本表就绪后经 registry.load_tool 重注册——那一次 __init__ 能拿到真索引。
        reg = runtime.get_skill_registry()
        if reg is not None and reg.names():
            index = "; ".join(f"{n}: {reg.get(n).description}" for n in reg.names())
            self.description = f"{self._DESC_HEAD} Available skills — {index}"
    requires_approval = False
    is_concurrency_safe = True
    is_read_only = True
    parameters = {
        "properties": {
            "name": {"type": "string",
                     "description": "Exact name of the skill to load, from the "
                                    "available skills list."},
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
