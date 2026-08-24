"""通用文本批（items/text/general.jsonl）+ App 截图批（items/vision/app_screens.jsonl）
+ 执行器多轮前史（executor.apply_history）的金测试。

守的是什么：
1. **期望本身不能空转**。每道题在这里配一条手写的正确答案与一条错误答案，
   断言正确答案过、错误答案不过——这就是这批期望的证伪证据。一条只会「全过」的
   正则和没有正则等价，而症状不会暴露：跑分虚高，路由跟着错。
2. **多轮前史的铺设顺序与形态**。history 若铺错位置（跑到终问之后）或角色写错却被
   静默吞掉，多轮承接题测的就不再是承接能力，而是一个假题面。
3. **资产可重建**。App 批的 PNG 与 gen_assets.py 一并入库，重新生成必须逐字节相同，
   否则「题库可复现」只是口号。
全部 CPU 纯函数 / 假引擎，零模型依赖。
"""
import asyncio
import importlib.util
import json
import re
from pathlib import Path

import pytest

from benchmarks.model_eval import itemlib
from benchmarks.model_eval.executor import apply_history, run_item
from benchmarks.model_eval.matchers import match_text

MODEL_EVAL = Path(__file__).resolve().parents[1] / "benchmarks" / "model_eval"
ITEMS_ROOT = MODEL_EVAL / "items"
GENERAL_BANK = ITEMS_ROOT / "text" / "general.jsonl"
APP_BANK = ITEMS_ROOT / "vision" / "app_screens.jsonl"
APP_ASSETS = ITEMS_ROOT / "vision" / "assets" / "app"
GEN_ASSETS_PY = ITEMS_ROOT / "vision" / "assets" / "gen_assets.py"

MAX_ASSET_BYTES = 150 * 1024

# ---- 手写答案对：(过, 不过)。改题面必须同步改这里，否则覆盖率断言先炸 ----

GENERAL_ANSWERS = {
    "tx-gen-001": (
        "- 周一至周五闭馆时间推迟到 21 点\n- 周末开放时段维持不变\n- 新增三十个自习座位，需提前一天预约",
        "以下是三条要点：\n- 延长开放时间\n- 周末不变\n- 新增座位"),
    "tx-gen-002": (
        "{\"name\": \"Ada\", \"city\": \"Berlin\", \"age\": 30}",
        "```json\n{\"name\": \"Ada\", \"city\": \"Berlin\", \"age\": \"30\"}\n```"),
    "tx-gen-003": (
        "| 城市 | 人口 |\n| --- | --- |\n| 大阪 | 275 |\n| 名古屋 | 232 |\n| 福冈 | 163 |",
        "| 城市 | 人口 |\n| --- | --- |\n| 大阪 | 275 |\n| 名古屋 | 232 |\n| 福冈 | 163 |\n| 合计 | 670 |"),
    "tx-gen-004": (
        "Tokyo",
        "The capital city of Japan is Tokyo."),
    "tx-gen-005": (
        "KEEP IT SIMPLE AND CLEAR",
        "Sure! Here it is: KEEP IT SIMPLE AND CLEAR"),
    "tx-gen-006": (
        "简单来说，缓存就是把常用的数据先存在取用更快的地方，下次直接拿来用。",
        "好的。\n简单来说，缓存就是把常用数据先存起来，下次直接用。"),
    "tx-gen-007": (
        "A checksum is a short value computed from a block of data so that accidental changes can be detected. Comparing the stored checksum with a freshly computed one tells you whether the data is still intact. That is all.",
        "A checksum is a short value computed from data to detect corruption. You compare it after transfer. That is all, hope this helps!"),
    "tx-gen-008": (
        "1. 注册并配置开发者账号\n2. 准备证书与描述文件\n3. 归档构建产物\n4. 上传到 App Store Connect\n5. 填写信息并提交审核",
        "1. 注册开发者账号\n2. 准备证书\n3. 归档构建\n4. 上传\n5. 提交审核\n以上就是全部步骤。"),
    "tx-gen-009": (
        "星尘耳机现在只需 199 元，这个价位相当划算。",
        "星尘耳机现在只要 199 元，真的很便宜。"),
    "tx-gen-010": (
        "No delivery within 30 days of ordering means you can request a full refund.",
        "If the package has not arrived within thirty days of the order date, the customer is entitled to request a full refund of the purchase price."),
    "tx-gen-011": (
        "We stayed in Kyoto for 3 days, mostly around the Kiyomizu-dera area.",
        "我们在京都待了三天，主要在清水寺附近活动。"),
    "tx-gen-012": (
        "1. apples\n2. bread\n3. milk\n4. eggs",
        "1. bread\n2. apples\n3. milk\n4. eggs"),
    "tx-gen-013": (
        "Dear Customer,\n\nThank you for your order. Your Nimbus Router has now shipped. We regret to inform you that the Vertex Hub is delayed, and the Orion Cable is currently out of stock.\n\nSincerely,\nCustomer Support",
        "Nimbus Router shipped. Vertex Hub late. Orion Cable gone."),
    "tx-gen-014": (
        "hannah.wu@northbay-logistics.com",
        "noreply@northbay-logistics.com"),
    "tx-gen-015": (
        "138-0013-8000",
        "010-6688-2100"),
    "tx-gen-016": (
        "2026-03-19",
        "2026-02-02"),
    "tx-gen-017": (
        "¥12,100",
        "¥14,500"),
    "tx-gen-018": (
        "ORD-2026-8831",
        "ORD-2019-1102"),
    "tx-gen-019": (
        "周慕云打造的云梯，让每一步都轻松登高。",
        "让每一步都轻松登高，效率触手可及。"),
    "tx-gen-020": (
        "120",
        "Each project gets 200."),
    "tx-gen-021": (
        "You should look up the currency used in Portugal, which is the euro.",
        "You should look up the currency of Spain."),
    "tx-gen-022": (
        "报税\n- 上年度的收入证明\n- 本人身份证件",
        "换轮胎\n- 千斤顶\n- 备用轮胎"),
    "tx-gen-023": (
        "Version control is a system that records changes to files over time. It lets you review the history of a project and return to any earlier state.",
        "版本控制是一种记录文件变更的系统，它让你可以回到任意一个历史版本。"),
    "tx-gen-024": (
        "建议清蒸鳕鱼配西兰花和半碗糙米饭，大约 450 大卡，清淡好消化。",
        "推荐一份麻辣香锅，大概 700 大卡。"),
    "tx-gen-025": (
        "240",
        "252"),
    "tx-gen-026": (
        "2026-04-18",
        "2026-04-19"),
    "tx-gen-027": (
        "20.1",
        "20.12"),
    "tx-gen-028": (
        "84",
        "140"),
    "tx-gen-029": (
        "としょかん",
        "図書館の読みは「としょかん」です。"),
    "tx-gen-030": (
        "- 通勤の時間がなくなる\n- 集中しやすい環境を選べる\n- 住む場所を自由に決められる",
        "リモートワークの利点は次のとおりです。\n- 通勤がない\n- 集中できる\n- 場所が自由"),
    "tx-gen-031": (
        "2026-05-07",
        "2026-01-20"),
    "tx-gen-032": (
        "お客様各位\n\nこのたびは「ソラリス Pro」の出荷が翌週にずれ込むこととなり、誠に申し訳ございません。出荷の準備が整い次第、改めてご連絡いたします。",
        "ソラリス Pro の出荷、来週にずれ込む。すまん。"),
}

APP_ANSWERS = {
    "vi-app-001": (
        "21.06",
        "19.50"),
    "vi-app-002": (
        "Dark mode",
        "Auto-update"),
    "vi-app-003": (
        "E-4471",
        "E-4417"),
    "vi-app-004": (
        "Email address",
        "Workspace URL"),
    "vi-app-005": (
        "5",
        "3"),
    "vi-app-006": (
        "6,210",
        "9,540"),
    "vi-app-007": (
        "09:45",
        "19:45"),
    "vi-app-008": (
        "archive.zip",
        "photo.heic"),
    "vi-app-009": (
        "quorum",
        "committee"),
    "vi-app-010": (
        "19",
        "12"),
    "vi-app-011": (
        "67%",
        "76%"),
    "vi-app-012": (
        "Pro 这一栏价格更高",
        "Standard"),
}

_cache = {}


def _bank(path: Path):
    if path not in _cache:
        _cache[path] = {it["id"]: it for it in itemlib.load_items([path])}
    return _cache[path]


def _regexes(spec):
    return list(spec.get("regex_any", [])) + list(spec.get("regex_all", []))


# ================== 通用文本批：装载纪律 ==================

def test_general_bank_loads_clean():
    """32 题全数过装载校验，id 连号，维度统一——坏一行整个 text 维度起不来。"""
    items = _bank(GENERAL_BANK)
    assert len(items) == 32
    assert sorted(items) == [f"tx-gen-{i:03d}" for i in range(1, 33)]
    assert {it["dimension"] for it in items.values()} == {"text"}


def test_general_bank_language_split():
    """鉴定纪律第 4 条：批内语言覆盖。ja 是本批新增（用户在日语环境）。"""
    langs = [it["lang"] for it in _bank(GENERAL_BANK).values()]
    assert set(langs) == {"zh", "en", "ja"}
    assert langs.count("zh") == 14 and langs.count("en") == 14 and langs.count("ja") == 4


def test_general_history_items_have_valid_roles():
    """多轮承接题的前史形态：角色白名单 + content 为字符串，且确有 6 道带前史。"""
    with_history = [it for it in _bank(GENERAL_BANK).values() if it["task"].get("history")]
    assert len(with_history) == 6
    for it in with_history:
        for msg in it["task"]["history"]:
            assert msg["role"] in ("user", "assistant"), it["id"]
            assert isinstance(msg["content"], str) and msg["content"].strip(), it["id"]


def test_general_bank_expect_shape_and_regexes_compile():
    """expect 一律 text 型；每条正则必须能编译（写坏的正则在跑分时才炸最贵）。"""
    for it in _bank(GENERAL_BANK).values():
        assert it["expect"]["type"] == "text", it["id"]
        final = it["expect"]["final"]
        assert final, it["id"]
        assert set(final) <= {"must_all", "must_any", "must_not", "regex_any", "regex_all"}
        for rx in _regexes(final):
            re.compile(rx)


def test_general_answer_pairs_cover_every_item():
    """答案对必须逐题齐备：漏一题就等于那题的期望没被证伪过。"""
    assert set(GENERAL_ANSWERS) == set(_bank(GENERAL_BANK))


@pytest.mark.parametrize("item_id", sorted(GENERAL_ANSWERS))
def test_general_expectation_discriminates(item_id):
    """一正一反：正确答案过、错误答案不过。后者才是这批期望的证伪证据。"""
    spec = _bank(GENERAL_BANK)[item_id]["expect"]["final"]
    good, bad = GENERAL_ANSWERS[item_id]
    ok, why = match_text(spec, good)
    assert ok, f"{item_id} 正确答案被判不过: {why}"
    assert not match_text(spec, bad)[0], f"{item_id} 错误答案竟通过——该题期望空转"


# ================== 执行器：多轮前史 ==================

class _RecordingEngine:
    """最小引擎替身：记下首次生成时看到的 session.history 快照，然后终答收场。"""

    backend = None
    dialect = None

    def __init__(self):
        self.seen = None

    async def generate_once(self, session, **kw):
        if self.seen is None:
            self.seen = json.loads(json.dumps(session.history))
        return {"thoughts": "", "text": "done", "tool_calls": [], "metrics": {}}


def _hist_item(history=None):
    task = {"user": "最后这一问"}
    if history is not None:
        task["history"] = history
    return {"id": "tx-hist-001", "dimension": "text", "lang": "zh",
            "source": {"origin": "self", "fit": "执行器多轮前史铺设路径的金测试用题"},
            "task": task, "expect": {"type": "text", "final": {"must_any": ["done"]}}}


def test_run_item_lays_history_before_final_user():
    """前史按序铺在终问之前；assistant 条目补齐 thoughts/tool_calls（方言取值面）。"""
    eng = _RecordingEngine()
    asyncio.run(run_item(eng, _hist_item([
        {"role": "user", "content": "我叫周慕云"},
        {"role": "assistant", "content": "记住了"},
        {"role": "user", "content": "还有个补充"}])))
    assert eng.seen[0]["role"] == "system"      # ChatSession 自带的系统提示条目
    assert eng.seen[1:] == [
        {"role": "user", "content": "我叫周慕云"},
        {"role": "assistant", "thoughts": "", "content": "记住了", "tool_calls": []},
        {"role": "user", "content": "还有个补充"},
        {"role": "user", "content": "最后这一问"},
    ]


def test_run_item_without_history_is_unchanged():
    """缺 history 时行为与旧版一致：历史里只有那一条终问。"""
    eng = _RecordingEngine()
    asyncio.run(run_item(eng, _hist_item()))
    assert eng.seen[0]["role"] == "system"
    assert eng.seen[1:] == [{"role": "user", "content": "最后这一问"}]


def test_run_item_bad_history_role_raises():
    """坏角色必须响亮失败：静默吞掉等于把多轮题降级成单轮题，跑分却照常出。"""
    with pytest.raises(ValueError, match="role"):
        asyncio.run(run_item(_RecordingEngine(),
                             _hist_item([{"role": "system", "content": "x"}])))


class _Sess:
    def __init__(self):
        self.history = []


def test_apply_history_rejects_malformed_shapes():
    for bad, pat in (("不是列表", "消息列表"),
                     ([["role", "user"]], r"\[0\] 须为对象"),
                     ([{"role": "tool", "content": "x"}], "role"),
                     ([{"role": "user", "content": ["块", "结构"]}], "content"),
                     ([{"content": "没写角色"}], "role")):
        with pytest.raises(ValueError, match=pat):
            apply_history(_Sess(), bad, "tx-x-001")


def test_apply_history_none_is_noop():
    s = _Sess()
    apply_history(s, None, "tx-x-001")
    assert s.history == []


# ================== App 截图批 ==================

def test_app_bank_loads_clean_and_assets_exist():
    items = _bank(APP_BANK)
    assert len(items) == 12
    assert sorted(items) == [f"vi-app-{i:03d}" for i in range(1, 13)]
    assert {it["dimension"] for it in items.values()} == {"vision"}
    langs = [it["lang"] for it in items.values()]
    assert langs.count("zh") == 6 and langs.count("en") == 6
    for it in items.values():
        imgs = it["task"].get("images") or []
        assert len(imgs) == 1, it["id"]
        path = Path(it["_src_dir"]) / imgs[0]
        assert path.is_file(), f"{it['id']} 资产缺失: {path}"
        assert path.stat().st_size <= MAX_ASSET_BYTES, f"{it['id']} 资产过大"
        for rx in _regexes(it["expect"]["final"]):
            re.compile(rx)


def test_app_answer_pairs_cover_every_item():
    assert set(APP_ANSWERS) == set(_bank(APP_BANK))


@pytest.mark.parametrize("item_id", sorted(APP_ANSWERS))
def test_app_expectation_discriminates(item_id):
    spec = _bank(APP_BANK)[item_id]["expect"]["final"]
    good, bad = APP_ANSWERS[item_id]
    ok, why = match_text(spec, good)
    assert ok, f"{item_id} 正确答案被判不过: {why}"
    assert not match_text(spec, bad)[0], f"{item_id} 错误答案竟通过——该题期望空转"


def _load_gen_assets():
    spec = importlib.util.spec_from_file_location("model_eval_gen_assets", GEN_ASSETS_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:  # 收集期软探测：缺包/缺字体 → 只跳字节重建一测，不拖垮整个文件
    from benchmarks.model_eval.items.vision.assets.gen_assets import REQUIRED_FONTS as _RF
    _FONTS_COMPLETE = len(_RF) == 4          # 四族（UI/UI_BOLD/MONO/CJK）都解析到才算齐
except Exception:                             # noqa: BLE001
    _FONTS_COMPLETE = False


@pytest.mark.skipif(
    not _FONTS_COMPLETE,
    reason="逐字节重建以本机四族系统字体齐备为前提（gen_assets 口径=同机重现）；缺族则跳过而不是误报")
def test_app_assets_are_deterministically_regenerable(tmp_path):
    """入库 PNG 必须能由 gen_assets.py 逐字节重建（无随机、无时间戳、不写 pnginfo）。
    重建对不上 = 题库不可复现，跨机/跨时的跑分就没有可比性。"""
    mod = _load_gen_assets()
    mod.generate_app_screens(tmp_path)
    made = sorted(p.name for p in tmp_path.glob("*.png"))
    assert len(made) == 12
    for name in made:
        rebuilt = (tmp_path / name).read_bytes()
        stored = (APP_ASSETS / name).read_bytes()
        assert rebuilt == stored, f"{name} 重建与入库副本不一致"


def test_app_assets_geometry_is_screenshot_like():
    """宽度落在 600–900px、字号 ≥18px 的渲染前提：太窄或超宽都不像 App 真实截图。"""
    from PIL import Image
    for path in sorted(APP_ASSETS.glob("*.png")):
        with Image.open(path) as im:
            assert 600 <= im.width <= 900, f"{path.name} 宽度 {im.width} 越界"
            assert im.height >= 200, path.name
