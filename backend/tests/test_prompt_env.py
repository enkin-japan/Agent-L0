"""环境注入(想法1)金测试:静态环境事实计算 + build 的 include_env 门控。

门控是隐私边界:个人事实(位置/语言/画像)只进主编排轮(include_env=True),
沙箱一次性会话替身默认 include_env=False——个人信息绝不进沙箱子生成上下文。
"""
from framework.core.prompt_builder import PromptBuilder


class _FakeTok:
    special_tokens_map = {}


class _FakeProcessor:
    tokenizer = _FakeTok()


def _pb(env):
    return PromptBuilder(_FakeProcessor(), env=env)


def test_machine_fact_injected_by_default():
    assert any("Operating system:" in l for l in _pb({}).static_env_lines)


def test_machine_fact_suppressible():
    assert not any("Operating system:" in l
                   for l in _pb({"inject_machine": False}).static_env_lines)


def test_personal_facts_optin():
    lines = _pb({"inject_machine": False, "language": "中文",
                 "location": "东京", "about": "工程背景"}).static_env_lines
    joined = "\n".join(lines)
    assert "中文" in joined and "东京" in joined and "工程背景" in joined


def test_null_personal_facts_absent():
    assert _pb({"inject_machine": False, "language": None,
                "location": None, "about": None}).static_env_lines == []


def test_build_gates_personal_env_on_include_flag():
    pb = _pb({"inject_machine": False, "location": "东京"})
    with_env = pb.build(messages=[], include_env=True)
    without_env = pb.build(messages=[], include_env=False)
    assert "东京" in with_env           # 主编排轮:注入
    assert "东京" not in without_env     # 沙箱子生成:不注入(隐私边界)
    # 日期锚点两种情况都在(日期永远注入)
    assert "Current date:" in with_env and "Current date:" in without_env
