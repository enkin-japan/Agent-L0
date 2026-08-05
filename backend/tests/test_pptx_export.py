"""deck→pptx 交付波金测试（OfficeCLI 集成第 2 站）。

守的是什么：
① **第二投影的确定性**——deck spec → batch 指令映射不漂移：12×12 栅格→16:9 英寸、
  角色→tokens 派生字号/颜色、bullets→逐段 list=bullet、chart 资产在盘→嵌真 PNG、
  资产缺失/image→风格化占位（与前端同语义）、icon 跳过如实入 stats、notes 随页。
② **分工诚实**——PDF 是预览即所得真身，pptx 是可编辑近似投影：工具描述明示漂移，
  占位/缺资产必须进回执 Note，不谎称与预览一致。
③ **门禁形状**——batch 原子 + validate，失败删产物（绝不交半成品）。
④ **工具面**——二进制缺失不注册（rescan 热补惯例）；免审批依据=只读 deck+写会话
  assets。

带 [live] 的用例需要本机 officecli，缺失自动 skip。
"""
import shutil
from pathlib import Path

import pytest

from framework.ppt import spec
from framework.ppt.pptx_export import (SLIDE_H_IN, SLIDE_W_IN, deck_to_commands,
                                       export_pptx)
from framework.office.officecli import find_officecli

HAS_CLI = find_officecli() is not None or shutil.which("officecli") is not None


def _deck(*slots, theme="light"):
    deck = spec.new_deck("测试甲板", theme)
    for s in slots:
        deck["slides"].append(spec.expand_slide(s))
    return deck


# ---- ① 投影确定性 ----

def test_grid_maps_to_169_inches_and_background():
    deck = _deck({"layout": "bullets", "title": "标题行", "bullets": ["甲", "乙"]})
    cmds, stats = deck_to_commands(deck, Path("/nonexistent"))
    slide = next(c for c in cmds if c["type"] == "slide")
    assert slide["props"]["background"] == "ffffff"          # light 主题 bg 剥 #
    title = next(c for c in cmds if c["type"] == "shape")
    # 栅格 (1,1,12,2) → x=0, y=0, w=全宽, h=2/12 高
    assert title["props"]["x"] == "0.000in" and title["props"]["y"] == "0.000in"
    assert title["props"]["width"] == f"{SLIDE_W_IN:.3f}in"
    assert title["props"]["height"] == f"{SLIDE_H_IN / 6:.3f}in"
    assert stats["slides"] == 1


def test_bullets_emit_list_paragraphs_with_pinned_typography():
    deck = _deck({"layout": "bullets", "title": "t", "bullets": ["甲", "乙"]})
    cmds, _ = deck_to_commands(deck, Path("/nonexistent"))
    paras = [c for c in cmds if c["type"] == "paragraph"]
    assert [p["props"]["text"] for p in paras] == ["甲", "乙"]
    assert all(p["props"]["list"] == "bullet" and p["props"]["size"] == 15
               and p["props"]["color"] for p in paras)
    body = [c for c in cmds if c["type"] == "shape"][1]
    assert body["props"]["valign"] == "top"                  # 首版目检修掉的垂直居中


def test_role_typography_from_theme_tokens():
    deck = _deck({"layout": "title", "title": "主标题", "subtitle": "副题"},
                 theme="warm")
    cmds, _ = deck_to_commands(deck, Path("/nonexistent"))
    shapes = [c for c in cmds if c["type"] == "shape"]
    t, sub = shapes[0]["props"], shapes[1]["props"]
    assert t["size"] == 40 and t["bold"] and t["align"] == "center"
    assert t["color"] == "9a3412"                            # warm primary
    assert sub["color"] == "78716c" and sub["size"] == 18    # warm muted
    assert t["font"] == "Georgia"                            # warm heading 字体族首名


def test_chart_asset_embeds_picture_when_on_disk(tmp_path):
    deck = _deck({"layout": "chart", "title": "图", "chart": {
        "chart_type": "bar", "labels": ["a"], "series": [{"values": [1]}]}})
    blk = next(b for b in deck["slides"][0]["blocks"] if b["type"] == "chart")
    blk["content"]["asset"] = "c.png"
    blk["visual_request"] = None
    (tmp_path / "c.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    cmds, stats = deck_to_commands(deck, tmp_path)
    pic = next(c for c in cmds if c["type"] == "picture")
    assert pic["props"]["src"] == str(tmp_path / "c.png")
    assert stats["pictures"] == 1 and stats["placeholders"] == 0


def test_missing_asset_and_image_become_placeholders_with_warning(tmp_path):
    deck = _deck({"layout": "image_full", "image": {"prompt": "外观照片", "alt": "办公室"}})
    blk = next(b for b in deck["slides"][0]["blocks"] if b["type"] == "chart"
               or b["type"] == "image")
    cmds, stats = deck_to_commands(deck, tmp_path)
    ph = [c for c in cmds if c["type"] == "shape"
          and "办公室" in c["props"].get("text", "")]
    assert ph and ph[0]["props"]["fill"]                     # 风格化占位块
    assert stats["placeholders"] == 1
    # 资产名在但盘上无档 → warning 如实上报
    blk["content"]["asset"] = "gone.png"
    _, stats2 = deck_to_commands(deck, tmp_path)
    assert any("gone.png" in w for w in stats2["warnings"])


def test_icon_skipped_into_stats_and_notes_emitted():
    deck = _deck({"layout": "two_col", "title": "t",
                  "left": {"icon": "rocket", "bullets": ["x"]},
                  "right": {"text": "右"}, "notes": "讲稿一句"})
    cmds, stats = deck_to_commands(deck, Path("/nonexistent"))
    assert stats["icons_skipped"] == 1                       # lucide 无位图，如实跳过
    notes = next(c for c in cmds if c["type"] == "notes")
    assert notes["props"]["text"] == "讲稿一句"


# ---- ②③④ 分工诚实 / 门禁 / 工具面 ----

def test_export_refuses_empty_deck(tmp_path):
    (tmp_path / "deck").mkdir()
    ok, msg, _ = export_pptx("sid", tmp_path)
    assert not ok and "deck" in msg


def test_tool_shape_and_honest_description():
    from framework.mcp.tools.deck_tools import DeckExportPptxTool
    t = DeckExportPptxTool()
    assert t.requires_approval is False
    assert "EDITABLE" in t.description
    assert "approximation" in t.description                  # 明示与预览有漂移
    assert "exactly" in t.description                        # PDF=真身的分工写进描述


# ---- [live] 真二进制全链 ----

@pytest.mark.skipif(not HAS_CLI, reason="officecli not installed")
def test_live_export_validates_and_zip_ok(tmp_path):
    from framework.ppt import deck_ops
    deck = _deck(
        {"layout": "title", "title": "実績", "subtitle": "8 月"},
        {"layout": "bullets", "title": "要点", "bullets": ["甲", "乙", "丙"]},
        {"layout": "quote", "quote": "質は速度に勝る。", "attribution": "会議"})
    deck_ops.save_deck(tmp_path, deck)
    ok, fname, stats = export_pptx("sid", tmp_path)
    assert ok, fname
    out = tmp_path / "assets" / fname
    import zipfile
    assert zipfile.ZipFile(out).testzip() is None
    assert stats["slides"] == 3 and not stats["warnings"]
