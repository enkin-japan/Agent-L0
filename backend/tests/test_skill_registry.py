"""Skill 剧本体系金测试（EXECUTION_LEDGER R2a）。

守的核心是**形态边界**（D.11）：skill 是主轮次的提示词剧本，**不是**自带工具集的隔离
子会话。这条边界是安全边界，不是口味问题——审批闸门只存在于 `orchestrator.py:551`
（经 registry.requires_approval），沙箱执行器没有审批通路。若 skill 能自带 tools 又跑在
沙箱上，一个 md 文件就成了绕过审批执行 run_shell_command / write_file 的路径。

故本文件钉死：剧本只产出**文本**，不产出工具授权。
"""
import pytest

from framework.core.skill_registry import SkillRegistry
from framework.utils.frontmatter import parse_frontmatter as _parse_frontmatter


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


# ---------- D1/D.17 · 斜杠点名的确定性分发 ----------

def _registry_with_demo(tmp_path):
    _write(tmp_path, "demo.md", _OK)
    return SkillRegistry(tmp_path)


def test_slash_dispatch_injects_playbook(tmp_path):
    """`/demo 任务` → 剧本正文进 ingested 通道；prompt 原样（用户气泡不变）。"""
    from framework.core.skill_registry import dispatch_slash_skill
    reg = _registry_with_demo(tmp_path)
    p, ing = dispatch_slash_skill("/demo 查一下天气", "", reg)
    assert p == "/demo 查一下天气"
    assert "Body line one." in ing and "系统注入" in ing and "/demo" in ing


def test_slash_dispatch_preserves_existing_ingestion(tmp_path):
    """已有摄入文本（如 PDF 提取）时剧本置于其前，两者都保留。"""
    from framework.core.skill_registry import dispatch_slash_skill
    reg = _registry_with_demo(tmp_path)
    _, ing = dispatch_slash_skill("/demo", "PDF 正文", reg)
    assert "Body line one." in ing and "PDF 正文" in ing
    assert ing.index("Body line one.") < ing.index("PDF 正文")


@pytest.mark.parametrize("prompt", [
    "/unknown 干活",          # 未注册的斜杠：原样放行（可能是普通文本）
    "普通问题 /demo 不在行首",  # 非行首不触发
    "去看看 https://x.com/demo",  # URL 里的斜杠不触发
])
def test_slash_dispatch_leaves_non_matches_alone(tmp_path, prompt):
    from framework.core.skill_registry import dispatch_slash_skill
    reg = _registry_with_demo(tmp_path)
    p, ing = dispatch_slash_skill(prompt, "原摄入", reg)
    assert p == prompt and ing == "原摄入"


def test_slash_dispatch_hyphen_names_and_nil_registry(tmp_path):
    """连字符剧本名（chart-report）可分发；registry 未装配时零行为。"""
    from framework.core.skill_registry import dispatch_slash_skill
    _write(tmp_path, "cr.md", "---\nname: chart-report\ndescription: d\n---\nCR body.\n")
    reg = SkillRegistry(tmp_path)
    _, ing = dispatch_slash_skill("/chart-report 分析", "", reg)
    assert "CR body." in ing
    p, ing2 = dispatch_slash_skill("/chart-report 分析", "x", None)
    assert p == "/chart-report 分析" and ing2 == "x"


def test_slash_dispatch_reports_unavailable_skill(tmp_path):
    """点名「存在但被探活舍弃」的剧本 → 注入如实告知（缺什么、别假装执行），
    而非静默放行（D1 实机 /chart-report 无声穿透实证）。"""
    from framework.core.skill_registry import dispatch_slash_skill
    _write(tmp_path, "needs.md",
           "---\nname: needs\ndescription: d\nrequires: [no_such_tool]\n---\nBody.\n")
    reg = SkillRegistry(tmp_path, tool_names=set())
    p, ing = dispatch_slash_skill("/needs 干活", "原摄入", reg)
    assert p == "/needs 干活"
    assert "不可用" in ing and "no_such_tool" in ing and "原摄入" in ing
    assert "Body." not in ing   # 剧本正文绝不注入——它注定跑不通


def test_turn_runner_wires_slash_dispatch():
    """接线断言：accept_turn 里必须真调 dispatch_slash_skill（纯函数对了但没接上
    = 全部白做，且症状是静默的）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "framework" / "core" / "turn_runner.py").read_text(encoding="utf-8")
    assert "dispatch_slash_skill(" in src


# ---------- D1 · 发货剧本对真实工具面（扩充纪律：每个新 skill 必须有金测试） ----------

# 发货清单钉死：探活不过的剧本是**静默舍弃**（风险 5 同款症状——启动正常、剧本消失、
# 无报错）。少一个名字这里就红，新增剧本必须同步登记。
_SHIPPED_SKILLS = {"deck", "research", "chart-report", "ingest", "briefing"}


def _declared_tool_names():
    """全部工具类的**声明名**（静态 AST 扫，不经探活）——「名字是否存在」与
    「此刻是否可用」是两个问题：前者抓 requires 打错字，后者随环境浮动（例：
    soffice 残链修复后 excel_research 在缺 LibreOffice 的机器上不注册，D1 实证）。"""
    import ast
    from pathlib import Path
    names = {"memory_write", "skill_invoke"}   # 装配期补注册的两个
    tools_dir = Path(__file__).resolve().parent.parent / "framework" / "mcp" / "tools"
    for f in tools_dir.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if (isinstance(stmt, ast.Assign)
                            and any(getattr(t, "id", "") == "name" for t in stmt.targets)
                            and isinstance(stmt.value, ast.Constant)
                            and isinstance(stmt.value.value, str)):
                        names.add(stmt.value.value)
    return names


def test_shipped_skills_requires_point_at_real_tools():
    """`requires:` 里打错一个工具名 = 剧本在真机上静默蒸发。对着**声明名全集**核对
    （静态，不受本机依赖缺失影响）。"""
    from pathlib import Path
    universe = _declared_tool_names()
    skills_dir = Path(__file__).resolve().parent.parent.parent / "skills"
    seen = set()
    for f in sorted(skills_dir.glob("*.md")):
        meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"), f)
        seen.add(str(meta.get("name")))
        missing = [t for t in (meta.get("requires") or []) if t not in universe]
        assert not missing, f"{f.name} 的 requires 指向不存在的工具: {missing}"
    assert seen == _SHIPPED_SKILLS, \
        f"发货剧本清单漂移: 现场 {sorted(seen)} != 登记 {sorted(_SHIPPED_SKILLS)}"


def test_shipped_skills_load_against_real_tool_universe():
    """终局断言：对着**本机真实可用面**，「requires 满足的剧本」必须一个不少地加载
    ——期望集按可用面条件推导（依赖缺失的剧本被舍弃是探活正确行为，不是回归；
    例：本机 LibreOffice 残链 → excel_research 不注册 → chart-report 合法缺席）。"""
    from pathlib import Path
    from framework.mcp.registry import MCPRegistry

    root = Path(__file__).resolve().parent.parent
    available = set(MCPRegistry(tools_dir=root / "framework" / "mcp" / "tools").tool_names())
    available |= {"memory_write", "skill_invoke"}
    skills_dir = root.parent / "skills"
    expected = set()
    for f in skills_dir.glob("*.md"):
        meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"), f)
        if all(t in available for t in (meta.get("requires") or [])):
            expected.add(str(meta.get("name")))
    reg = SkillRegistry(skills_dir, tool_names=available)
    assert set(reg.names()) == expected, \
        f"加载结果 {sorted(reg.names())} != 可用面推导 {sorted(expected)}（有剧本被静默舍弃）"
    assert expected, "本机可用面推导出的剧本集为空——环境坏得离谱，别让全绿骗过去"
