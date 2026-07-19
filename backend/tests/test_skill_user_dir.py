"""剧本「用户目录」来源与保存/删除端点金测试（照 test_api_tool_factory 的双目录
纪律与假 Request 手法）。

守：①双目录加载（repo 优先、user 兜底、跨目录重名 repo 赢、user 坏文件 fail-open）；
②listing 带 source/file（含被探活舍弃者，UI 要能定位坏剧本去删）；③保存/删除端点
的效果语义（registered/saved_unavailable/removed，非法 400、内置重名 409、无 rebuild
兜底 norebuild）。写入只落数据根 skills/（内置 skills/ 只读，app 模式下不可写）。
"""
import asyncio

import pytest

from framework.core.skill_registry import SkillRegistry


_OK = """---
name: demo
description: 一份示范剧本
---
Body line one.
"""

_NEEDY = """---
name: needy
description: 依赖某工具的剧本
requires: [some_tool]
---
Do the thing.
"""


def _write(d, name, text):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


# ---------- 双目录加载 ----------

def test_user_dir_skill_loads_with_source_user(tmp_path):
    repo, user = tmp_path / "repo", tmp_path / "user"
    _write(repo, "demo.md", _OK)
    _write(user, "mine.md",
           "---\nname: mine\ndescription: 我的剧本\n---\nMy body.\n")
    reg = SkillRegistry(repo, tool_names=set(), user_dir=user)
    assert set(reg.names()) == {"demo", "mine"}
    by_name = {s["name"]: s for s in reg.listing()}
    assert by_name["mine"]["source"] == "user" and by_name["mine"]["file"] == "mine.md"
    assert by_name["mine"]["body"] == "My body."          # 用户条目带正文供编辑器预填
    assert by_name["mine"]["requires"] == []              # requires 同带——不带则编辑保存静默丢依赖
    assert by_name["demo"]["source"] == "repo"
    assert "body" not in by_name["demo"]                  # 内置条目不带正文（不膨胀载荷）
    assert "requires" not in by_name["demo"]
    assert reg.origin_of("mine") == "user" and reg.origin_of("demo") == "repo"


def test_cross_dir_dup_repo_wins(tmp_path):
    repo, user = tmp_path / "repo", tmp_path / "user"
    _write(repo, "demo.md", _OK)
    _write(user, "demo.md",
           "---\nname: demo\ndescription: 用户覆盖版\n---\nUser override body.\n")
    reg = SkillRegistry(repo, tool_names=set(), user_dir=user)
    assert reg.origin_of("demo") == "repo"                 # 内置优先
    assert reg.get("demo").body == "Body line one."        # 用户同名被跳过
    assert reg.listing()[0]["source"] == "repo"


def test_user_bad_file_fails_open(tmp_path):
    repo, user = tmp_path / "repo", tmp_path / "user"
    _write(repo, "demo.md", _OK)
    _write(user, "broken.md", "no frontmatter here\n")     # 坏用户剧本
    _write(user, "good.md",
           "---\nname: good\ndescription: 好剧本\n---\nGood body.\n")
    reg = SkillRegistry(repo, tool_names=set(), user_dir=user)
    assert set(reg.names()) == {"demo", "good"}            # 坏文件只废自己


def test_listing_includes_discarded_with_source_file(tmp_path):
    repo, user = tmp_path / "repo", tmp_path / "user"
    _write(repo, "demo.md", _OK)
    _write(user, "needy.md", _NEEDY)                       # 依赖 some_tool 缺失 → 舍弃
    reg = SkillRegistry(repo, tool_names=set(), user_dir=user)
    by_name = {s["name"]: s for s in reg.listing()}
    assert by_name["needy"]["available"] is False
    assert by_name["needy"]["missing"] == ["some_tool"]
    assert by_name["needy"]["source"] == "user"           # 坏依赖用户剧本 UI 里要能删
    assert by_name["needy"]["file"] == "needy.md"
    assert "body" not in by_name["needy"]                 # 舍弃条目不带正文（仅可用用户条目带）


# ---------- 保存/删除端点（假 Request 直调处理器） ----------

class _FakeState:
    pass


class _FakeReq:
    def __init__(self, rebuild=None, has_rebuild=True):
        self.app = _FakeState()
        self.app.state = _FakeState()
        if has_rebuild:
            self.app.state.rebuild_skills = rebuild


def _rebuilder(repo, user, tool_names):
    """返回一个 rebuild_skills 可调用：每次现读目录重建注册表（含 runtime 登记）。"""
    from framework.agents import runtime as _runtime

    def rebuild():
        reg = SkillRegistry(repo, tool_names=set(tool_names), user_dir=user)
        _runtime.set_skill_registry(reg)
        return reg
    return rebuild


def _save(body_kw, tmp_path, monkeypatch, tool_names=(), has_rebuild=True):
    from framework.api.routes import save_skill
    from framework.api.schemas import SkillSaveRequest
    from framework.agents import runtime as _runtime
    from fastapi import HTTPException

    data = tmp_path / "data"
    repo = tmp_path / "repo"
    monkeypatch.setenv("AGENT_DATA_DIR", str(data))
    user = data / "skills"
    # 保存端点用 runtime.get_skill_registry() 判内置重名——先按当前 repo 建好现场
    _runtime.set_skill_registry(SkillRegistry(repo, tool_names=set(tool_names), user_dir=user))
    rebuild = _rebuilder(repo, user, tool_names) if has_rebuild else None
    req = _FakeReq(rebuild, has_rebuild=has_rebuild)
    try:
        return asyncio.run(save_skill(req, SkillSaveRequest(**body_kw)))
    except HTTPException as e:
        return {"http_error": e.status_code, "detail": e.detail}


_BODY = {"name": "mine", "description": "我的剧本", "body": "剧本正文。"}


def test_save_registers_new_skill(tmp_path, monkeypatch):
    out = _save(_BODY, tmp_path, monkeypatch)
    assert out["effect"] == "registered"
    assert (tmp_path / "data" / "skills" / "mine.md").exists()


def test_save_rejects_invalid_name(tmp_path, monkeypatch):
    out = _save({**_BODY, "name": "Bad_Name"}, tmp_path, monkeypatch)
    assert out["http_error"] == 400


def test_save_rejects_empty_body(tmp_path, monkeypatch):
    out = _save({**_BODY, "body": "   "}, tmp_path, monkeypatch)
    assert out["http_error"] == 400


def test_save_repo_name_conflict_409(tmp_path, monkeypatch):
    _write(tmp_path / "repo", "mine.md",
           "---\nname: mine\ndescription: 内置同名\n---\n内置正文。\n")
    out = _save(_BODY, tmp_path, monkeypatch)
    assert out["http_error"] == 409 and out["detail"] == "与内置剧本重名"


def test_save_overwrites_existing_user_skill(tmp_path, monkeypatch):
    _save(_BODY, tmp_path, monkeypatch)
    out = _save({**_BODY, "body": "改过的正文。"}, tmp_path, monkeypatch)
    assert out["effect"] == "registered"
    text = (tmp_path / "data" / "skills" / "mine.md").read_text(encoding="utf-8")
    assert "改过的正文。" in text


def test_save_missing_tool_is_saved_unavailable(tmp_path, monkeypatch):
    out = _save({**_BODY, "requires": ["some_tool"]}, tmp_path, monkeypatch)
    assert out["effect"] == "saved_unavailable"
    assert "some_tool" in out["detail"]
    # 补上工具后重建即可用（requires 内联落盘、可回读的回归守卫）
    out2 = _save({**_BODY, "requires": ["some_tool"]}, tmp_path, monkeypatch,
                 tool_names=("some_tool",))
    assert out2["effect"] == "registered"


def test_save_without_rebuild_hook(tmp_path, monkeypatch):
    out = _save(_BODY, tmp_path, monkeypatch, has_rebuild=False)
    assert out["effect"] == "saved_norebuild"
    assert (tmp_path / "data" / "skills" / "mine.md").exists()


def _delete(name, tmp_path, monkeypatch, has_rebuild=True):
    from framework.api.routes import delete_skill
    from fastapi import HTTPException

    data = tmp_path / "data"
    repo = tmp_path / "repo"
    monkeypatch.setenv("AGENT_DATA_DIR", str(data))
    user = data / "skills"
    rebuild = _rebuilder(repo, user, ()) if has_rebuild else None
    req = _FakeReq(rebuild, has_rebuild=has_rebuild)
    try:
        return asyncio.run(delete_skill(name, req))
    except HTTPException as e:
        return {"http_error": e.status_code, "detail": e.detail}


def test_delete_removes_user_skill(tmp_path, monkeypatch):
    _save(_BODY, tmp_path, monkeypatch)
    path = tmp_path / "data" / "skills" / "mine.md"
    assert path.exists()
    out = _delete("mine", tmp_path, monkeypatch)
    assert out["effect"] == "removed" and not path.exists()


def test_delete_missing_is_404(tmp_path, monkeypatch):
    out = _delete("mine", tmp_path, monkeypatch)               # 无此用户剧本（内置不可删）
    assert out["http_error"] == 404


def test_delete_without_rebuild_hook(tmp_path, monkeypatch):
    _save(_BODY, tmp_path, monkeypatch, has_rebuild=False)
    out = _delete("mine", tmp_path, monkeypatch, has_rebuild=False)
    assert out["effect"] == "removed_norebuild"
