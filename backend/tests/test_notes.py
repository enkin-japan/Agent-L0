"""文件式长期记忆金测试（EXECUTION_LEDGER R4）。

守的核心是**隐私边界**：笔记是用户的个人事实。两条路各有一道闸门——

- **索引**随 `include_env=True` 注入 → 只进主编排轮，绝不进沙箱子生成。
  与静态环境事实共用同一道门（不另造闸门：两道闸门迟早会有一道忘了关）。
- **正文**走 RAG 类目 `agent_notes`，而沙箱工具集里根本没有 `rag_search`（沙箱不变量 1）。

外加一条**上限**：索引在每一轮都占上下文，不设上限就是让记忆系统慢慢吃掉可用窗口。
"""
import pytest

from framework.memory.notes import (MAX_INDEX_LINES, NOTE_TYPES, NoteStore)


def _store(tmp_path):
    return NoteStore(tmp_path)


def _one(store, name="likes-terse", type_="feedback"):
    return store.write(name, "用户要言简意赅", type_, "回答别铺开讲。\n\n**Why:** 他自己是工程师。")


# ---------- 隐私边界 ----------

def test_index_rides_the_env_gate_and_never_enters_a_sandbox(tmp_path):
    """索引只在 include_env=True 时出现。这是 R4 的红线：沙箱拿不到个人事实。

    门写在 builder 里而不是靠调用方自觉——R1-1 的教训：不变量要写死，不能靠
    "调用方不会那么干"（当年 5 份沙箱拷贝全靠引擎的 getattr 兜底才没泄漏）。
    """
    from framework.dialects.gemma4.prompt_builder import PromptBuilder

    class _Tok:
        special_tokens_map = {}
        def encode(self, s, add_special_tokens=False):
            return [1]

    class _Proc:
        tokenizer = _Tok()

    store = _store(tmp_path)
    _one(store)
    index = store.index_lines()
    assert any("likes-terse" in l for l in index)

    pb = PromptBuilder(_Proc(), suppress_ghost_thinking=False, env={})
    on = pb.build(messages=[], include_env=True, env_extra=index)
    off = pb.build(messages=[], include_env=False, env_extra=index)
    assert "likes-terse" in on
    assert "likes-terse" not in off, "沙箱即便传了 env_extra 也必须被门挡下"


def test_sandboxes_cannot_recall_note_bodies():
    """正文侧的隐私由构造保证：沙箱工具集里没有 rag_search，也就无从召回 agent_notes。
    这条随三沙箱的工具集变化而变化，故钉死——哪天有人给沙箱加了 rag_search，本用例会红。"""
    import pathlib
    agents_dir = pathlib.Path(__file__).resolve().parent.parent / "framework" / "agents"
    for f in agents_dir.glob("*_sandbox.py"):
        assert "rag_search" not in f.read_text(encoding="utf-8"), \
            f"{f.name} 出现了 rag_search——笔记正文会经它进沙箱上下文（R4 隐私红线）"


def test_notes_dir_is_not_in_the_code_tree():
    """笔记落在 backend/notes/（与 sessions/ 平级）且已 gitignore。
    个人事实进代码树 = 被提交上 GitHub。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent.parent
    ignored = (root / ".gitignore").read_text(encoding="utf-8")
    assert "backend/notes/" in ignored, "backend/notes/ 必须 gitignore（笔记是个人事实）"


# ---------- 索引上限 ----------

def test_index_is_capped_and_says_so(tmp_path):
    """超上限时截断并**明说还有更多**——宁可让模型知道"去搜"，
    也不能悄悄丢一半让它以为这就是全部。"""
    store = _store(tmp_path)
    for i in range(MAX_INDEX_LINES + 5):
        store.write(f"note-{i:03d}", f"第 {i} 条", "project", "body")
    lines = store.index_lines()
    assert len(lines) <= MAX_INDEX_LINES + 2          # 抬头 + 上限条 + 省略提示
    assert any("more note(s) not listed" in l for l in lines)


def test_empty_store_injects_nothing(tmp_path):
    """没有笔记就不注入任何东西——不给模型一段"你没有笔记"的废话占上下文。"""
    assert _store(tmp_path).index_lines() == []


# ---------- 写入面 ----------

def test_write_then_reload_roundtrip(tmp_path):
    _one(_store(tmp_path))
    again = NoteStore(tmp_path)                       # 重启即重扫目录
    assert again.names() == ["likes-terse"]
    n = again.get("likes-terse")
    assert n.type == "feedback" and "工程师" in n.body


def test_same_name_updates_in_place(tmp_path):
    """同名重写 = 更新那条事实，不是再攒一条（记忆的价值在提炼，不在堆积）。"""
    store = _store(tmp_path)
    store.write("x", "旧描述", "user", "旧正文")
    store.write("x", "新描述", "user", "新正文")
    assert store.names() == ["x"]
    assert store.get("x").description == "新描述"
    assert len(list(tmp_path.glob("x*.md"))) == 1


@pytest.mark.parametrize("bad_name", ["../escape", "has space", "UPPER", "", "-lead", "a/b"])
def test_illegal_names_rejected(tmp_path, bad_name):
    """名字即文件名：挡掉路径穿越与花名。

    大写也挡：macOS/Windows 文件系统大小写不敏感，`X.md` 与 `x.md` 是同一个文件，
    而内存字典会当成两条——索引说两条、磁盘一条，后写的悄悄盖掉先写的。"""
    with pytest.raises(ValueError):
        _store(tmp_path).write(bad_name, "d", "user", "b")


def test_unknown_type_rejected(tmp_path):
    with pytest.raises(ValueError):
        _store(tmp_path).write("x", "d", "diary", "b")


@pytest.mark.parametrize("field", ["description", "body"])
def test_empty_required_fields_rejected(tmp_path, field):
    """描述为空 = 索引里那一行是空的，模型永远不会想起这条笔记；正文为空 = 没有事实。"""
    kw = {"name": "x", "description": "d", "type_": "user", "body": "b"}
    kw[field] = "   "
    with pytest.raises(ValueError):
        _store(tmp_path).write(**kw)


def test_index_file_is_rebuilt_not_patched(tmp_path):
    """MEMORY.md 是产物不是真相，每次全量重建——增量修补是索引与事实分叉的经典来源。"""
    store = _store(tmp_path)
    store.write("a", "第一条", "user", "b")
    store.write("b", "第二条", "reference", "b")
    idx = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "第一条" in idx and "第二条" in idx
    assert "## user" in idx and "## reference" in idx
    (tmp_path / "MEMORY.md").write_text("被手改坏了", encoding="utf-8")
    store.write("c", "第三条", "user", "b")
    assert "被手改坏了" not in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_index_file_is_not_loaded_as_a_note(tmp_path):
    _one(_store(tmp_path))
    assert NoteStore(tmp_path).names() == ["likes-terse"]   # MEMORY.md 不算一条笔记


def test_bad_note_does_not_kill_the_rest(tmp_path):
    """一条写坏了不连累其他（照 MCPRegistry / SkillRegistry 的先例）。"""
    _one(_store(tmp_path))
    (tmp_path / "broken.md").write_text("没有 frontmatter\n", encoding="utf-8")
    (tmp_path / "wrongtype.md").write_text(
        "---\nname: w\ndescription: d\ntype: diary\n---\nbody\n", encoding="utf-8")
    assert NoteStore(tmp_path).names() == ["likes-terse"]


def test_missing_dir_is_not_fatal(tmp_path):
    assert NoteStore(tmp_path / "nope").names() == []


# ---------- 四类型分类学 ----------

def test_taxonomy_is_the_four_types():
    assert NOTE_TYPES == ("user", "feedback", "project", "reference")


def test_tool_description_carries_the_do_not_save_list():
    """「不该存什么」是本设计的核心（记忆的失败模式是存太多，不是存不下），
    它只存在于工具描述这段 prompt 里——被谁"精简"掉了不会报错，故钉死。"""
    from framework.mcp.tools.memory_write import MemoryWriteTool
    d = MemoryWriteTool.description
    assert "DO NOT save" in d
    assert "git history" in d and "only matters to this conversation" in d
    assert "When unsure, don't write." in d
    for t in NOTE_TYPES:
        assert f"{t}:" in d
