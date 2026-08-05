"""Word 生成波金测试（OfficeCLI 集成第 1 站）。

守的是什么：
① **转换纯函数确定性**——markdown 子集 → batch 指令的映射不漂移：标题→样式、
  嵌套列表→listStyle+numLevel、GFM 表→逐格 set、行内混排→run 序列、围栏代码
  原样保缩进（含长得像指令的文本——一切经 JSON batch，无 shell 转义面）。
② **诚实降级**——缺失/非本地图片不炸整文档，降级为可见占位段 + warning 上报。
③ **交付门禁形状**——write_docx 任一门禁失败删产物再抛错（绝不留半成品）；
  读回交叉校验的归一化两侧同规则（markitdown 会还原 markdown 记号，剥了再比）。
④ **落盘纪律**——子进程环境禁常驻（退出即落盘）+ 禁更新检查（出网红线纵深）。
⑤ **工具面**——二进制缺失不注册（rescan 热补惯例）；产物名经 asset_filename
  slug 化，天然免路径穿越。

带 [live] 的用例需要本机装有 officecli，缺失自动 skip——转换器金测试不依赖它。
"""
import shutil
from pathlib import Path

import pytest

from framework.office import md_docx
from framework.office.officecli import find_officecli

HAS_CLI = find_officecli() is not None or shutil.which("officecli") is not None


def _cmds(md, **kw):
    cmds, stats = md_docx.md_to_commands(md, **kw)
    # 剥掉样式定义前缀，测试聚焦内容指令
    body = [c for c in cmds if c.get("parent") != "/styles"]
    return body, stats


# ---- ① 转换确定性 ----

def test_headings_map_to_styles_and_style_defs_present():
    cmds, _ = md_docx.md_to_commands("# 一\n\n## 二\n\n#### 四\n\n###### 六")
    styles = [c for c in cmds if c.get("parent") == "/styles"]
    assert {c["props"]["id"] for c in styles} == {"Heading1", "Heading2",
                                                 "Heading3", "Heading4"}
    paras = [c for c in cmds if c["type"] == "paragraph"]
    assert [p["props"]["style"] for p in paras] == \
        ["Heading1", "Heading2", "Heading4", "Heading4"]  # 5/6 级并入 4


def test_nested_lists_and_ordered():
    body, _ = _cmds("- a\n  - b\n    - c\n1. x\n2. y")
    lists = [c["props"] for c in body if c["type"] == "paragraph"]
    assert lists[0] == {"listStyle": "bullet", "text": "a"}
    assert lists[1]["numLevel"] == 1 and lists[2]["numLevel"] == 2
    assert lists[3]["listStyle"] == "ordered" and "numLevel" not in lists[3]


def test_table_cells_with_header_formatting_and_escaped_pipe():
    body, stats = _cmds("| 名称 | 值 |\n|---|---|\n| a\\|b | 2 |\n")
    tbl = next(c for c in body if c["type"] == "table")
    assert tbl["props"] == {"rows": 2, "cols": 2}
    sets = [c for c in body if c.get("command") == "set"]
    hdr = next(c for c in sets if c["path"].endswith("tr[1]/tc[1]"))
    assert hdr["props"]["bold"] and hdr["props"]["shd"]
    assert next(c for c in sets if c["path"].endswith("tr[2]/tc[1]"))["props"]["text"] == "a|b"
    assert stats["tables"] == 1


def test_inline_mix_emits_runs_and_hyperlink():
    body, _ = _cmds("看 **粗** 与 *斜* 及 `code` 和 [站](https://e.co)。")
    runs = [c for c in body if c["type"] in ("run", "hyperlink")]
    kinds = [(c["type"], c["props"].get("bold"), c["props"].get("italic"),
              c["props"].get("font"), c["props"].get("url")) for c in runs]
    assert ("run", True, None, None, None) in kinds            # 粗
    assert ("run", None, True, None, None) in kinds            # 斜
    assert any(k[3] == "Courier New" for k in kinds)           # code
    assert ("hyperlink", None, None, None, "https://e.co") in kinds


def test_code_fence_verbatim_including_command_lookalikes():
    md = "```\nofficecli set x --prop text=hack\n    indented = True\n```"
    body, _ = _cmds(md)
    # 代码行以等宽 run 形态挂在空壳段落下
    texts = [c["props"]["text"] for c in body if c["type"] == "run"]
    assert texts[0] == "officecli set x --prop text=hack"      # 原样，不被当指令
    assert texts[1] == "    indented = True"                   # 缩进保留
    assert all(c["props"]["font"] == "Courier New"
               for c in body if c["type"] == "run")


def test_paragraph_soft_wrap_merge_and_hr_skip():
    body, _ = _cmds("第一行\n第二行\n\n---\n\n下一段")
    paras = [c["props"]["text"] for c in body if c["type"] == "paragraph"]
    assert paras == ["第一行 第二行", "下一段"]


# ---- ② 诚实降级 ----

def test_missing_image_degrades_with_warning(tmp_path):
    body, stats = _cmds("![图](nope.png)", image_root=tmp_path)
    assert stats["warnings"] and "nope.png" in stats["warnings"][0]
    assert not any(c["type"] == "picture" for c in body)
    texts = [c["props"].get("text", "") for c in body]
    assert any("image unavailable" in t for t in texts)        # 文档内可见占位


def test_local_image_resolved_against_root(tmp_path):
    (tmp_path / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    body, stats = _cmds("![走势](chart.png)", image_root=tmp_path)
    pic = next(c for c in body if c["type"] == "picture")
    assert pic["props"]["src"] == str(tmp_path / "chart.png")
    assert not stats["warnings"]


# ---- ③ 门禁形状 / ④ 落盘纪律 ----

def test_norm_for_match_strips_markdown_markers_both_sides():
    assert md_docx._norm_for_match("看 **粗** 的 [站](https://e.co)。") == "看粗的站。"


def test_subprocess_env_pins_no_resident_and_no_update():
    from framework.office.officecli import _env
    env = _env()
    assert env["OFFICECLI_NO_AUTO_RESIDENT"] == "1"
    assert env["OFFICECLI_SKIP_UPDATE"] == "1"


def test_write_docx_deletes_artifact_when_gate_fails(tmp_path, monkeypatch):
    out = tmp_path / "x.docx"
    if not HAS_CLI:
        # 无二进制：create 即失败，同样必须不留文件
        with pytest.raises(Exception):
            md_docx.write_docx("# t", out)
        assert not out.exists()
        return
    monkeypatch.setattr(md_docx, "_readback_coverage", lambda *a, **k: 0.0)
    with pytest.raises(md_docx.OfficeCliError, match="read-back"):
        md_docx.write_docx("# 标题\n\n正文足够长的一句话。", out)
    assert not out.exists()                                     # 门禁失败不留半成品


# ---- ⑤ 工具面 ----

def test_tool_shape_and_asset_naming():
    from framework.mcp.tools.doc_write import WriteDocumentTool
    from framework.agents.chart_tools import asset_filename
    t = WriteDocumentTool()
    assert t.requires_approval is False
    assert set(t.parameters["required"]) == {"file_name", "markdown"}
    fname = asset_filename("../../../etc/passwd", kind="doc", ext="docx")
    assert "/" not in fname and ".." not in fname               # slug 化免穿越


# ---- [live] 真二进制全链 ----

@pytest.mark.skipif(not HAS_CLI, reason="officecli not installed")
def test_live_full_chain_validate_and_crosscheck(tmp_path):
    md = ("# 実績レポート\n\n## 概要\n\n**増収 25%**、詳細は下表。\n\n"
          "| 地域 | 売上 |\n|---|---|\n| 東京 | 1,200 |\n\n- 要点甲\n  - 要点乙\n\n"
          "```\nprint('ok')\n```\n")
    out = tmp_path / "r.docx"
    stats = md_docx.write_docx(md, out)
    assert out.exists() and stats["coverage"] >= 0.9
    assert stats["tables"] == 1 and not stats["warnings"]
    import zipfile
    assert zipfile.ZipFile(out).testzip() is None               # 包完整
