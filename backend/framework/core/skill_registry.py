"""Skill 注册中心（EXECUTION_LEDGER R2a）：目录扫描加载可复用流程剧本。

**skill 填的空档**：不值得做成工具（无确定性逻辑，本质就是提示词），也不需要起子 agent
（不需隔离），但需要**被复用**——此前这类流程知识要么焊死在某处 Python 里，要么根本
不存在、每次靠模型从工具描述里自己拼。

**形态（D.11）**：skill = **主轮次的提示词剧本**，不是隔离子会话。`skill_invoke` 把
正文作为 tool 响应交回，正文即落进主编排轮上下文，模型随后用**主循环的工具**照做。

**为什么是这个形态**——安全边界：审批闸门只存在于 `orchestrator.py:551`（经
`registry.requires_approval`）。沙箱从不碰 registry，`run_sandbox` **没有审批通路**，
此前也不需要（不变量 1 保证沙箱工具由构造即无害）。若 skill 能自带 `tools:` 又跑在
沙箱执行器上，一个 md 文件就成了绕过审批执行 `run_shell_command` / `write_file` 的
路径——这个洞今天不存在，不能由 skill 引入。剧本形态下工具沿用主循环，闸门照常生效，
**零新增提权面**。

frontmatter 解析共用 `utils/frontmatter.py`（R4 的笔记同格式，不再各拷一份）。

照 `MCPRegistry` 的形状：目录扫描 + 探活舍弃 + 名字索引。探活对 skill 的含义 =
`requires:` 点名的工具是否都在注册表里（例：deck 剧本依赖 `deck_export_pdf`，而它自身
探 playwright；playwright 缺失时该剧本整体不可用，不该advertise 给模型）。
"""
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..utils.frontmatter import parse_frontmatter

# 斜杠点名：行首 /<剧本名>（名字允许小写字母数字与连字符），其后是任务正文（可空）
_SLASH_RE = re.compile(r"^/([a-z0-9][a-z0-9-]*)(?:\s|$)", re.S)


def dispatch_slash_skill(prompt: str, ingested_text: str,
                         registry: "Optional[SkillRegistry]") -> Tuple[str, str]:
    """斜杠点名剧本的**确定性分发**（D1/D.17）。

    R2a 的语义本就是「用户点名才调」——而点名是机械可判的，不该指望模型悟：
    D1 实测（e4b）「使用 X 剧本」「/x」两种说法模型都不会主动调 `skill_invoke`，
    四个验收案例全部裸奔。故摄入期直接判：prompt 以 `/<已注册剧本名>` 开头时，
    把剧本正文注入 `ingested_text` 通道（与 PDF 摄入同形态：作为紧随用户文本的
    独立块烘进历史、重放不重解析），用户气泡与 prompt 原样保留。

    不认识的斜杠原样放行（可能是普通文本）；`skill_invoke` 工具保留（模型侧的
    显式通路 + 剧本目录的错误提示面）。零新增提权面：注入的只是文本，工具照走
    主循环审批闸门（D.11 不变）。
    """
    m = _SLASH_RE.match(prompt.strip())
    if not m or registry is None:
        return prompt, ingested_text
    skill = registry.get(m.group(1))
    if skill is None:
        # 存在但被探活舍弃的剧本：如实告知而非静默放行（否则模型对着点名瞎打，
        # 用户不知道为什么没按剧本走——D1 实机 /chart-report 实证）
        missing = registry.unavailable_reason(m.group(1))
        if missing:
            notice = (f"[系统注入：用户点名的剧本「{m.group(1)}」当前不可用——依赖工具 "
                      f"{missing} 未注册（依赖未就绪，如缺 LibreOffice/SearXNG 一类外部件）。"
                      "请直接告知用户这一点与缺失项，再询问是否改用其他方式；"
                      "不要假装按剧本执行。]")
            combined = f"{notice}\n\n{ingested_text}" if ingested_text.strip() else notice
            return prompt, combined
        return prompt, ingested_text
    block = (f"[系统注入：用户以 /{skill.name} 点名了剧本「{skill.name}」"
             f"（{skill.description}），正文如下——按剧本执行本轮任务]\n\n{skill.body}")
    combined = f"{block}\n\n{ingested_text}" if ingested_text.strip() else block
    return prompt, combined


class Skill:
    """一份剧本：frontmatter 元数据 + md 正文（正文即注入主轮次的提示词）。"""

    def __init__(self, name: str, description: str, body: str,
                 requires: Optional[List[str]] = None, source: Optional[Path] = None):
        self.name = name
        self.description = description
        self.body = body
        self.requires: List[str] = list(requires or [])
        self.source = source

    def is_available(self, tool_names: set) -> bool:
        """探活：requires 点名的工具必须全部在注册表里（照 BaseTool.is_available 的位置）。"""
        return all(t in tool_names for t in self.requires)


class SkillRegistry:
    """Skill 注册中心：扫描 skills_dir 下的 *.md，探活后按名索引。"""

    def __init__(self, skills_dir: Path, tool_names: Optional[set] = None):
        self.skills_dir = skills_dir
        self._skills: Dict[str, Skill] = {}
        # 被探活舍弃的剧本 → 缺失工具清单。用户点名它们时要**如实告知**而不是静默
        # 放行（D1 实机：/chart-report 在 LibreOffice 缺失时无声穿透，模型瞎打）。
        self._unavailable: Dict[str, List[str]] = {}
        self._load_all(tool_names or set())

    def _load_all(self, tool_names: set) -> None:
        self._skills.clear()
        if not self.skills_dir.exists():
            print(f"[Skill] 剧本目录 {self.skills_dir} 不存在，跳过加载。")
            return
        for file in sorted(self.skills_dir.glob("*.md")):
            try:
                meta, body = parse_frontmatter(file.read_text(encoding="utf-8"), file)
                name = str(meta.get("name") or "").strip()
                description = str(meta.get("description") or "").strip()
                if not name or not description:
                    raise ValueError(f"{file.name}: frontmatter 必须含 name 与 description")
                if not body:
                    raise ValueError(f"{file.name}: 正文为空（剧本正文就是它的全部价值）")
                if name in self._skills:
                    raise ValueError(f"{file.name}: 剧本名 {name!r} 与 "
                                     f"{self._skills[name].source} 重名")
                skill = Skill(name, description, body, meta.get("requires"), file)
                # 探活：依赖的工具不在注册表时舍弃，避免把注定跑不通的剧本交给模型
                if not skill.is_available(tool_names):
                    missing = [t for t in skill.requires if t not in tool_names]
                    self._unavailable[name] = missing
                    print(f"[Skill] ⛔ 剧本 {name} 不可用（缺工具 {missing}），已舍弃。")
                    continue
                self._skills[name] = skill
                print(f"[Skill] ✅ 成功加载剧本: {name}")
            except Exception as e:
                print(f"[Skill] ⚠️ 加载剧本 {file.name} 失败: {e}")

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def unavailable_reason(self, name: str) -> Optional[List[str]]:
        """剧本存在但被探活舍弃时，返回缺失工具清单；否则 None。"""
        return self._unavailable.get(name)

    def names(self) -> List[str]:
        return sorted(self._skills)

    def catalog(self) -> List[Dict[str, str]]:
        """名字 + 描述清单。R2a 只用于错误提示与前端罗列；把它塞进系统提示让模型
        自主挑选 = R2b，挂在 DEFERRED.md（e4b 上剧本一多大概率选不准）。"""
        return [{"name": s.name, "description": s.description}
                for s in (self._skills[n] for n in self.names())]
