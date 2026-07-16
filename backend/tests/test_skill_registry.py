"""Skill 剧本体系金测试（EXECUTION_LEDGER R2a）。

守的核心是**形态边界**（D.11）：skill 是主轮次的提示词剧本，**不是**自带工具集的隔离
子会话。这条边界是安全边界，不是口味问题——审批闸门只存在于 `orchestrator.py:551`
（经 registry.requires_approval），沙箱执行器没有审批通路。若 skill 能自带 tools 又跑在
沙箱上，一个 md 文件就成了绕过审批执行 run_shell_command / write_file 的路径。

故本文件钉死：剧本只产出**文本**，不产出工具授权。
"""
import pytest

from framework.core.skill_registry import SkillRegistry, _parse_frontmatter


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


_OK = """---
name: demo
description: 一份示范剧本
---
Body line one.
"""


# ---------- 形态边界（安全） ----------

def test_skill_grants_no_tool_authority():
    """**剧本不是授权书**：Skill 的面上不得出现任何工具授权字段。

    加上 `tools:` 就意味着 md 文件能决定子会话拿到哪些工具；而沙箱执行器无审批通路，
    等于绕过 orchestrator.py:551 的闸门。要加这个字段，先解决审批透出（见 D.11 选项三）。
    """
    from framework.core.skill_registry import Skill
    s = Skill("n", "d", "body")
    for granting in ("tools", "allowed_tools", "max_steps", "model"):
        assert not hasattr(s, granting), \
            f"Skill 不得有 {granting!r}——剧本只产出文本，不产出工具授权（D.11）"


def test_skill_invoke_returns_text_only():
    """skill_invoke 的产出是正文文本，随 tool 响应落进主轮次；无子会话、无工具集。"""
    from framework.mcp.tools.skill_invoke import SkillInvokeTool
    t = SkillInvokeTool()
    assert t.requires_approval is False       # 取一段文本本身无副作用
    assert t.is_read_only is True
    assert set(t.parameters["properties"]) == {"name"}, \
        "skill_invoke 只收剧本名——多一个参数就是多一条让模型自己定制授权的路"


# ---------- frontmatter 解析 ----------

def test_requires_parsed_as_list():
    meta, body = _parse_frontmatter(
        "---\nname: x\ndescription: d\nrequires: [a, b]\n---\nbody\n", type("P", (), {"name": "x.md"}))
    assert meta["requires"] == ["a", "b"] and body == "body"


@pytest.mark.parametrize("text,why", [
    ("no frontmatter at all\n", "缺 frontmatter"),
    ("---\nname: x\n", "frontmatter 未闭合"),
    ("---\nname x\n---\nbody\n", "行无法解析"),
])
def test_malformed_frontmatter_rejected_loudly(text, why):
    """解析歧义必须炸而不是猜——剧本内容会原样进模型上下文，猜错等于悄悄改提示词。"""
    with pytest.raises(ValueError):
        _parse_frontmatter(text, type("P", (), {"name": "bad.md"}))


# ---------- 注册面（照 MCPRegistry 形状） ----------

def test_loads_and_indexes_by_name(tmp_path):
    _write(tmp_path, "demo.md", _OK)
    reg = SkillRegistry(tmp_path)
    assert reg.names() == ["demo"]
    assert reg.get("demo").body == "Body line one."
    assert reg.catalog() == [{"name": "demo", "description": "一份示范剧本"}]


def test_unavailable_skill_is_discarded(tmp_path):
    """探活：requires 点名的工具不在注册表 → 舍弃。照 MCPRegistry 舍弃不可用工具的先例
    （例：deck 剧本依赖 deck_export_pdf，而它自身探 playwright；playwright 缺失时整份
    剧本跑不通，不该 advertise 给模型）。"""
    _write(tmp_path, "needs.md",
           "---\nname: needs\ndescription: d\nrequires: [deck_export_pdf]\n---\nbody\n")
    assert SkillRegistry(tmp_path, tool_names=set()).names() == []
    assert SkillRegistry(tmp_path, tool_names={"deck_export_pdf"}).names() == ["needs"]


def test_bad_skill_does_not_kill_the_rest(tmp_path):
    """一份剧本写坏了不得连累其他——照 MCPRegistry 逐插件 try/except 的先例。"""
    _write(tmp_path, "good.md", _OK)
    _write(tmp_path, "bad.md", "no frontmatter\n")
    assert SkillRegistry(tmp_path).names() == ["demo"]


def test_empty_body_rejected(tmp_path):
    """正文为空的剧本无意义——正文就是它的全部价值。"""
    _write(tmp_path, "hollow.md", "---\nname: hollow\ndescription: d\n---\n")
    assert SkillRegistry(tmp_path).names() == []


def test_duplicate_name_rejected(tmp_path):
    """重名剧本必须报错而非静默后者覆盖前者（覆盖 = 用户以为在跑 A 实则跑了 B）。"""
    _write(tmp_path, "a.md", _OK)
    _write(tmp_path, "b.md", _OK)
    assert SkillRegistry(tmp_path).names() == ["demo"]   # 后者被拒，前者留存


def test_missing_dir_is_not_fatal(tmp_path):
    assert SkillRegistry(tmp_path / "nope").names() == []


# ---------- 随仓库发货的剧本 ----------

def test_shipped_skills_all_parse():
    """`skills/` 下随仓库发货的剧本必须全部可加载——坏剧本只在启动日志里报警，
    很容易漏（R2a-4 的试点 deck.md 即在此列）。"""
    from pathlib import Path
    skills_dir = Path(__file__).resolve().parent.parent.parent / "skills"
    assert skills_dir.is_dir(), f"剧本目录不存在: {skills_dir}"
    shipped = sorted(p.name for p in skills_dir.glob("*.md"))
    assert shipped, "skills/ 下没有任何剧本"
    # 给足工具名，只验解析与元数据完整性（探活另有 test_unavailable_skill_is_discarded 守）
    for f in skills_dir.glob("*.md"):
        meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"), f)
        assert meta.get("name") and meta.get("description") and body, f"{f.name} 元数据不全"
