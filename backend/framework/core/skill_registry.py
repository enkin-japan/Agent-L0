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

照 `MCPRegistry` 的形状：目录扫描 + 探活舍弃 + 名字索引。探活对 skill 的含义 =
`requires:` 点名的工具是否都在注册表里（例：deck 剧本依赖 `deck_export_pdf`，而它自身
探 playwright；playwright 缺失时该剧本整体不可用，不该advertise 给模型）。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional


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


def _parse_frontmatter(text: str, source: Path) -> "tuple[Dict[str, Any], str]":
    """解析 `---` 包裹的 YAML frontmatter，返回 (meta, body)。

    刻意只认最小子集（`key: value` 与 `key: [a, b]`）而不引 YAML 解析器：skill 是本地
    可信文件，但少一个解析器就少一类解析歧义；真需要复杂结构时说明该做成工具而非剧本。
    """
    if not text.startswith("---"):
        raise ValueError(f"{source.name}: 缺少 frontmatter（首行须为 ---）")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError(f"{source.name}: frontmatter 未闭合（缺少结束的 ---）")
    meta: Dict[str, Any] = {}
    for line in text[3:end].strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"{source.name}: frontmatter 行无法解析: {line!r}")
        key, _, val = line.partition(":")
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            meta[key.strip()] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = val
    body = text[end + 4:].strip()
    return meta, body


class SkillRegistry:
    """Skill 注册中心：扫描 skills_dir 下的 *.md，探活后按名索引。"""

    def __init__(self, skills_dir: Path, tool_names: Optional[set] = None):
        self.skills_dir = skills_dir
        self._skills: Dict[str, Skill] = {}
        self._load_all(tool_names or set())

    def _load_all(self, tool_names: set) -> None:
        self._skills.clear()
        if not self.skills_dir.exists():
            print(f"[Skill] 剧本目录 {self.skills_dir} 不存在，跳过加载。")
            return
        for file in sorted(self.skills_dir.glob("*.md")):
            try:
                meta, body = _parse_frontmatter(file.read_text(encoding="utf-8"), file)
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
                    print(f"[Skill] ⛔ 剧本 {name} 不可用（缺工具 {missing}），已舍弃。")
                    continue
                self._skills[name] = skill
                print(f"[Skill] ✅ 成功加载剧本: {name}")
            except Exception as e:
                print(f"[Skill] ⚠️ 加载剧本 {file.name} 失败: {e}")

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def names(self) -> List[str]:
        return sorted(self._skills)

    def catalog(self) -> List[Dict[str, str]]:
        """名字 + 描述清单。R2a 只用于错误提示与前端罗列；把它塞进系统提示让模型
        自主挑选 = R2b，挂在 DEFERRED.md（e4b 上剧本一多大概率选不准）。"""
        return [{"name": s.name, "description": s.description}
                for s in (self._skills[n] for n in self.names())]
