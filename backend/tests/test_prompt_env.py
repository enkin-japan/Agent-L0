"""环境注入(想法1)金测试:静态环境事实计算 + build 的 include_env 门控。

门控是隐私边界:个人事实(位置/语言/画像)只进主编排轮(include_env=True),
沙箱一次性会话替身默认 include_env=False——个人信息绝不进沙箱子生成上下文。
"""
from framework.dialects.gemma4.prompt_builder import PromptBuilder


class _FakeTok:
    special_tokens_map = {}


class _FakeProcessor:
    tokenizer = _FakeTok()


def _pb(env):
    return PromptBuilder(_FakeProcessor(), env=env)


# 只留手填个人事实、关掉全部自动探测项的基准 env（测试聚焦各自开关时复用）
_AUTO_OFF = {"inject_machine": False, "inject_timezone": False, "inject_locale": False}


def test_machine_fact_injected_by_default():
    assert any("Operating system:" in l for l in _pb({}).static_env_lines)


def test_machine_fact_suppressible():
    assert not any("Operating system:" in l
                   for l in _pb({"inject_machine": False}).static_env_lines)


def test_timezone_injected_by_default():
    lines = _pb({"inject_machine": False, "inject_locale": False}).static_env_lines
    assert any(l.startswith("Time zone:") for l in lines)


def test_timezone_suppressible():
    assert not any("Time zone:" in l
                   for l in _pb({**_AUTO_OFF}).static_env_lines)


def test_locale_fallback_only_without_language():
    # language 未填：locale 兜底行可注入（内容依宿主系统，只验行首）
    without = _pb({"inject_machine": False, "inject_timezone": False}).static_env_lines
    # language 已填：locale 行必须让位——两行说同一件事只稀释注意力
    with_lang = _pb({"inject_machine": False, "inject_timezone": False,
                     "language": "中文"}).static_env_lines
    assert not any("system locale" in l for l in with_lang)
    assert any("preferred reply language" in l for l in with_lang)
    for l in without:
        assert "preferred reply language" not in l


def test_personal_facts_optin():
    lines = _pb({**_AUTO_OFF, "language": "中文",
                 "location": "东京", "about": "工程背景"}).static_env_lines
    joined = "\n".join(lines)
    assert "中文" in joined and "东京" in joined and "工程背景" in joined


def test_null_personal_facts_absent():
    assert _pb({**_AUTO_OFF, "language": None,
                "location": None, "about": None}).static_env_lines == []


def test_build_gates_personal_env_on_include_flag():
    pb = _pb({**_AUTO_OFF, "location": "东京"})
    with_env = pb.build(messages=[], include_env=True)
    without_env = pb.build(messages=[], include_env=False)
    assert "东京" in with_env           # 主编排轮:注入
    assert "东京" not in without_env     # 沙箱子生成:不注入(隐私边界)
    # 日期锚点两种情况都在(日期永远注入)
    assert "Current date:" in with_env and "Current date:" in without_env
