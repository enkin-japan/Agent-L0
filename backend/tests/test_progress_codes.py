"""进度上报稳定代号金测试（build 84：后端进度文案跟随 app 界面语言）。

13 处中文进度上报（research_sandbox 4 处 5 种文本、web_read 2、excel_sandbox 5、
media_download 2）在原有 label/detail 之外附带 `code` + `args`，客户端据此按界面语言重渲染。

本文件钉死三件事：
1. **文本逐字节不变**——每处上报的 label/detail 与 build 83（HEAD 301213a9）写死的中文
   原值相同（GOLDEN_* 是改动前源码里 f-string 的展开值，逐字抄写，不由模板反推）；
2. 每处上报带预期的 code / args，且经编排层钩子原样进 `tool_progress` 事件（只增不改：
   不带 code 的旧式上报事件形状与 build 83 相同）；
3. 跨语言一致：后端代号表 == app 侧 StepText.progressTemplates（zh 模板逐字相同），
   每个 zh 模板与工具 zh 名在 L10n.table 里有 ja/en 且占位符一致；工具显示名表覆盖
   backend/framework/mcp/tools/ 下全部注册名。
"""
import asyncio
import re
import sys
import types
from pathlib import Path

import pytest

from framework.agents import excel_sandbox, excel_tools, research_sandbox, web_tools
from framework.agents import runtime as agent_runtime
from framework.agents.progress_codes import (PROGRESS_TEMPLATES, clean_args, progress,
                                              render, safe_progress)

from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
from .test_orchestrator_lifecycles import GEN, begin_turn, make_orch, ring_of, run

ROOT = Path(__file__).resolve().parents[2]
SWIFT_STEPTEXT = ROOT / "apple-app/Sources/Store/ChatMessage.swift"
SWIFT_L10N = ROOT / "apple-app/Sources/App/L10n.swift"
TOOLS_DIR = ROOT / "backend/framework/mcp/tools"


class _Recorder:
    """进度钩子替身：签名与编排层 _tool_progress 相同。"""

    def __init__(self):
        self.events = []

    def __call__(self, label, detail="", code=None, args=None):
        self.events.append({"label": label, "detail": detail, "code": code, "args": args})

    @property
    def labels(self):
        return [(e["label"], e["detail"]) for e in self.events]

    @property
    def codes(self):
        return [(e["code"], e["args"]) for e in self.events]


# ==========================================================================
# 1. research_sandbox：四个上报点 + 强制渲染变体，假引擎走完整流程断言事件序列
# ==========================================================================

LONG_QUERY = "东京 2026 年 11 月 10 日 天气 气温 降水 风力 湿度 紫外线 空气质量 日出日落 预报"


@pytest.mark.asyncio
async def test_research_sandbox_progress_sequence(monkeypatch):
    assert len(LONG_QUERY) > 40   # 截断路径必须真被走到
    monkeypatch.setattr(web_tools, "reset_extraction_cache", lambda: None)
    monkeypatch.setattr(web_tools, "web_search", lambda q, cfg, n=None, tr=None, cat=None:
                        [{"url": "https://a.example/1", "title": "A"}])
    monkeypatch.setattr(web_tools, "format_search_results", lambda res: "results")
    monkeypatch.setattr(web_tools, "open_pages", lambda *a, **k: [])
    monkeypatch.setattr(web_tools, "wrap_untrusted", lambda res, reg=None: "<pages/>")
    monkeypatch.setattr(web_tools, "format_source_ledger", lambda reg: "")
    eng = FakeEngine([
        {"tool_calls": [call("web_search", query=LONG_QUERY)]},
        {"tool_calls": [call("open_pages", urls=["https://a.example/1", "https://b.example/2"])]},
        {"tool_calls": [call("open_pages", urls=["https://a.example/1"], render=True)]},
        {"text": "结论"},          # 预算用尽后的强制收敛生成
    ])
    rec = _Recorder()
    out = await research_sandbox.run_research(eng, "东京天气", {"max_steps": 3},
                                              progress_cb=rec)
    assert out == "结论"

    GOLDEN_RESEARCH = [   # build 83 原值（research_sandbox.py:173/182/198/215 的展开）
        ("联网调查：思考中（1/3）", ""),
        ("联网调查：搜索「" + LONG_QUERY[:40] + "」", ""),
        ("联网调查：思考中（2/3）", ""),
        ("联网调查：阅读 2 个页面", ""),
        ("联网调查：思考中（3/3）", ""),
        ("联网调查：阅读 1 个页面（强制渲染）", ""),
        ("联网调查：归纳结论", ""),
    ]
    assert rec.labels == GOLDEN_RESEARCH
    assert rec.codes == [
        ("research.thinking", {"step": 1, "max": 3}),
        ("research.searching", {"query": LONG_QUERY[:40]}),
        ("research.thinking", {"step": 2, "max": 3}),
        ("research.reading", {"count": 2}),
        ("research.thinking", {"step": 3, "max": 3}),
        ("research.reading_rendered", {"count": 1}),
        ("research.summarizing", {}),
    ]


@pytest.mark.asyncio
async def test_research_sandbox_without_callback_still_runs(monkeypatch):
    """无钩子（非会话上下文）时上报零开销、调查本体照常。"""
    monkeypatch.setattr(web_tools, "reset_extraction_cache", lambda: None)
    monkeypatch.setattr(web_tools, "format_source_ledger", lambda reg: "")
    eng = FakeEngine([{"text": "直接结论"}])
    out = await research_sandbox.run_research(eng, "q", {"max_steps": 2})
    assert out == "直接结论"


# ==========================================================================
# 2. web_read：抓取 + 归纳两处
# ==========================================================================

class _BgLoop:
    def __enter__(self):
        import threading
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *a):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def test_web_read_progress(monkeypatch):
    from framework.mcp.tools import web_read as wrd

    async def _digest(*a, **k):
        return "摘要"

    rec = _Recorder()
    with _BgLoop() as loop:
        monkeypatch.setattr(agent_runtime, "is_ready", lambda: True)
        monkeypatch.setattr(agent_runtime, "get_engine", lambda: object())
        monkeypatch.setattr(agent_runtime, "get_loop", lambda: loop)
        monkeypatch.setattr(web_tools, "reset_extraction_cache", lambda: None)
        monkeypatch.setattr(web_tools, "open_pages",
                            lambda urls, cfg: [{"url": u, "text": "正文"} for u in urls])
        monkeypatch.setattr(web_tools, "wrap_untrusted", lambda pages: "<material/>")
        monkeypatch.setattr(research_sandbox, "run_page_digest", _digest)
        agent_runtime.set_progress_hook(rec)
        try:
            tool = wrd.WebReadTool.__new__(wrd.WebReadTool)
            tool.execute(["https://a.example/1", "https://b.example/2"], "讲了啥")
        finally:
            agent_runtime.set_progress_hook(None)

    GOLDEN_WEB_READ = [("阅读网页：抓取 2 个页面", ""),   # web_read.py:123（build 83）
                       ("阅读网页：归纳内容", "")]          # web_read.py:133
    assert rec.labels == GOLDEN_WEB_READ
    assert rec.codes == [("web_read.fetching", {"count": 2}), ("web_read.summarizing", {})]


# ==========================================================================
# 3. media_download：解析 + 下载两处（假 yt_dlp，不出网）
# ==========================================================================

def test_media_download_progress(monkeypatch, tmp_path):
    from framework.mcp.tools.media_download import MediaDownloadTool

    out_file = tmp_path / "assets" / "clip.m4a"

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False, process=True):
            if not download:
                return {"_type": "video", "title": "T"}
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(b"x" * 1024)
            return {"title": "T", "requested_downloads": [{"filepath": str(out_file)}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYDL))
    session = types.SimpleNamespace(session_dir=str(tmp_path), session_id="s-media")
    monkeypatch.setattr(agent_runtime, "get_current_session", lambda: session)
    rec = _Recorder()
    agent_runtime.set_progress_hook(rec)
    try:
        out = MediaDownloadTool().execute(url="https://v.example/watch?v=1", mode="audio")
    finally:
        agent_runtime.set_progress_hook(None)
    assert out.startswith("Downloaded (audio")

    GOLDEN_MEDIA = [("正在解析链接", "https://v.example/watch?v=1"),   # media_download.py:161
                    ("正在下载媒体", "audio")]                          # media_download.py:180
    assert rec.labels == GOLDEN_MEDIA
    assert rec.codes == [("media.resolving", {}), ("media.downloading", {})]


# ==========================================================================
# 4. excel_sandbox：五处（拆解 → 执行代码 → 门禁 → 视觉回读 → 交付），门禁/REPL 用替身
# ==========================================================================

@pytest.mark.asyncio
async def test_excel_sandbox_progress_sequence(monkeypatch, tmp_path):
    class _FakeRepl:
        def __init__(self, workdir, timeout_default=None):
            self.workdir = Path(workdir)

        def run(self, code, timeout=None):
            return {"ok": True, "output": "done"}

        def close(self):
            pass

    def _fake_verify(target, **kw):
        return {"verdict": "GO", "sha256": excel_tools.file_sha256(target),
                "blockers": [], "stages": {}, "preview_pages": ["p1.png"]}

    async def _fake_visual(engine, pages, **kw):
        return "looks fine"

    monkeypatch.setattr(excel_tools, "SandboxRepl", _FakeRepl)
    monkeypatch.setattr(excel_sandbox, "inspect_workbook", lambda target, grid=None: {"sheets": []})
    monkeypatch.setattr(excel_sandbox, "verify_workbook", _fake_verify)
    monkeypatch.setattr(excel_sandbox, "_visual_qa", _fake_visual)

    session_dir = tmp_path / "sess"
    work = session_dir / "excel" / "work"
    work.mkdir(parents=True)
    (work / "report.xlsx").write_bytes(b"PK-fake-workbook")

    eng = FakeEngine([{"tool_calls": [
        call("inspect_workbook"),
        call("run_python", code="print(1)"),
        call("verify_workbook"),
        call("finish", summary="ok", assumptions=[]),
    ]}])
    rec = _Recorder()
    out = await excel_sandbox.run_excel_task(eng, "做张表", None, session_dir, "s-xl",
                                             {"max_steps": 2}, progress=rec)
    assert "excel" in out.lower()

    delivered = rec.events[-1]["detail"]
    assert delivered.endswith(".xlsx") and (session_dir / "assets" / delivered).exists()
    GOLDEN_EXCEL = [("拆解表格结构", "report.xlsx"),   # excel_sandbox.py:513（build 83）
                    ("执行代码", ""),                  # :520
                    ("运行交付门禁", "report.xlsx"),   # :444
                    ("视觉回读", ""),                  # :467
                    ("交付", delivered)]               # :489
    assert rec.labels == GOLDEN_EXCEL
    assert rec.codes == [("excel.inspecting", {}), ("excel.running_code", {}),
                         ("excel.verifying", {}), ("excel.visual_qa", {}),
                         ("excel.delivering", {})]


# ==========================================================================
# 5. 事件通道：runtime.report_progress(_code) → 编排层钩子 → journal tool_progress
# ==========================================================================

class _ProgressRegistry(FakeRegistry):
    """执行时按代号上报一次、旧式上报一次（工具线程内，与真工具同路径）。"""

    def execute_tool(self, name, arguments):
        agent_runtime.report_progress_code("web_read.fetching", count=3)
        agent_runtime.report_progress("旧式进度", "细节")
        agent_runtime.report_progress_code("no.such.code", count=1)   # 写错代号：吞掉，不发
        return super().execute_tool(name, arguments)


def test_tool_progress_event_carries_code_and_args(make_session):
    engine = FakeEngine([{"tool_calls": [call("read_thing")]}, {"text": "好"}])
    registry = _ProgressRegistry([FakeTool("read_thing", result="r")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "success"

    call_id = ring_of(session, "tool_started")[0]["call_id"]
    assert ring_of(session, "tool_progress") == [
        {"call_id": call_id, "label": "阅读网页：抓取 3 个页面", "detail": "",
         "code": "web_read.fetching", "args": {"count": 3}},
        # 不带代号的上报：事件形状与 build 83 完全相同（无 code / args 键）
        {"call_id": call_id, "label": "旧式进度", "detail": "细节"},
    ]


class _BadArgsRegistry(FakeRegistry):
    """clean_args 对非法 args（非映射类型）会抛——练 orchestrator._tool_progress 的兜底。"""

    def execute_tool(self, name, arguments):
        agent_runtime.report_progress("坏参进度", "", code="web_read.fetching", args=[1, 2, 3])
        return super().execute_tool(name, arguments)


def test_tool_progress_survives_clean_args_exception(make_session):
    """orchestrator._tool_progress 里 clean_args 抛异常时事件不丢，退回不带 code/args 的
    旧式事件（而不是被 report_progress 自身的 try/except 整条吞掉）。"""
    engine = FakeEngine([{"tool_calls": [call("read_thing")]}, {"text": "好"}])
    registry = _BadArgsRegistry([FakeTool("read_thing", result="r")])
    orch = make_orch(engine, registry)
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    result = run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "success"

    call_id = ring_of(session, "tool_started")[0]["call_id"]
    assert ring_of(session, "tool_progress") == [
        {"call_id": call_id, "label": "坏参进度", "detail": ""},
    ]


# ==========================================================================
# 6. 代号表本身：类型闸、未知代号、渲染
# ==========================================================================

def test_clean_args_keeps_only_flat_str_and_int():
    assert clean_args({"a": 1, "b": "x", "c": True, "d": 1.5, "e": [1], "f": None, 3: "k"}) \
        == {"a": 1, "b": "x"}
    assert clean_args(None) == {}


def test_render_and_progress():
    assert render("research.thinking", {"step": 2, "max": 6}) == "联网调查：思考中（2/6）"
    assert progress("research.searching", query="q") == \
        ("联网调查：搜索「q」", "research.searching", {"query": "q"})
    with pytest.raises(KeyError):
        render("no.such.code")


def test_report_progress_code_without_hook_is_noop():
    agent_runtime.set_progress_hook(None)
    agent_runtime.report_progress_code("web_read.fetching", count=1)   # 不抛


def test_safe_progress_falls_back_when_render_raises(monkeypatch):
    """render() 抛异常时 safe_progress 永不抛，退回不带 code/args 的旧式文本，而不是让
    整条进度丢失（见三处 _report / report_progress_code 的改动）。"""
    def _boom(code, args=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("framework.agents.progress_codes.render", _boom)
    label, code, args = safe_progress("research.thinking", {"step": 1, "max": 3})
    assert label == "联网调查：思考中（1/3）"   # 宽松代入填上了占位符，不是模板原文
    assert code is None and args is None

    # 未登记的代号仍然维持旧语义（吞掉，不发）——真正写错代号的 bug 该在代号表测试里红。
    assert safe_progress("no.such.code", {}) == (None, None, None)


def test_report_progress_code_falls_back_when_render_raises(monkeypatch):
    """上报仍发生：report_progress_code 在 render 抛异常时经钩子发出不带 code/args 的
    旧式事件，文本非空。"""
    def _boom(code, args=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("framework.agents.progress_codes.render", _boom)
    rec = _Recorder()
    agent_runtime.set_progress_hook(rec)
    try:
        agent_runtime.report_progress_code("research.thinking", step=1, max=3)
    finally:
        agent_runtime.set_progress_hook(None)
    assert len(rec.events) == 1
    ev = rec.events[0]
    assert ev["label"] and ev["code"] is None and ev["args"] is None


def test_codes_are_dotted_lowercase_and_args_are_str_or_int_placeholders():
    for code, tpl in PROGRESS_TEMPLATES.items():
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", code), code
        # 模板里只准出现 {name} 占位，不准字面花括号（客户端单遍替换的前提）
        assert re.sub(r"\{[a-z_]+\}", "", tpl).count("{") == 0, tpl
        assert "}" not in re.sub(r"\{[a-z_]+\}", "", tpl), tpl


# ==========================================================================
# 7. 跨语言一致性：后端代号表 ↔ app 侧 StepText ↔ L10n.table
# ==========================================================================

def _swift_string_dict(src: str, decl: str) -> dict:
    """解析 `static let <decl>: [String: String] = [ "k": "v", ... ]`（字面量里无转义引号）。"""
    m = re.search(r"static let " + re.escape(decl) + r": \[String: String\] = \[(.*?)\n    \]",
                  src, re.S)
    assert m, f"StepText.{decl} not found"
    pairs = re.findall(r'^\s*"([^"\\]*)":\s*"([^"\\]*)",?\s*(?://.*)?$', m.group(1), re.M)
    body_lines = [ln for ln in m.group(1).splitlines()
                  if ln.strip() and not ln.strip().startswith("//")]
    assert len(pairs) == len(body_lines), "unparsed entries in " + decl
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), f"duplicate keys in {decl}"
    return dict(pairs)


def _l10n_table() -> dict:
    src = SWIFT_L10N.read_text()
    body = src[src.index("static let table"):src.index("\n    ]\n}")]
    entries = re.findall(r'^\s*"((?:[^"\\]|\\.)*)": \("((?:[^"\\]|\\.)*)", "((?:[^"\\]|\\.)*)"\),$',
                         body, re.M)
    # 重复键检查覆盖全部表项（含值折行的多行表项、键独占一行的表项），值解析只需单行表项
    # （新增项都是单行）。键匹配只认行首的键，不要求同行跟着 `: (`——键独占一行时值在下一行。
    keys = re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', body, re.M)
    # 独立验证口径：按整行是否「以引号开头且含 `":`」再数一遍键数，两种数法必须一致——
    # 哪天前面的正则又漏匹配（如键独占一行的新写法），这里先红，而不是静默少查重复键。
    line_key_count = sum(1 for ln in body.splitlines() if re.match(r'^\s*"', ln) and '":' in ln)
    assert len(keys) == line_key_count >= 780
    assert len(keys) >= len(entries) > 700
    dups = sorted({k for k in keys if keys.count(k) > 1})
    # Swift 字典字面量重复键是运行期崩溃（Fatal error），不是编译错误——这里提前红
    assert not dups, f"duplicate L10n keys: {dups}"
    return {k: (ja, en) for k, ja, en in entries}


def _placeholders(s: str) -> list:
    return sorted(re.findall(r"\{([a-z_]+)\}", s))


def test_swift_progress_templates_match_backend():
    swift = _swift_string_dict(SWIFT_STEPTEXT.read_text(), "progressTemplates")
    assert swift == PROGRESS_TEMPLATES


def test_progress_templates_have_ja_en_with_same_placeholders():
    table = _l10n_table()
    for code, zh in PROGRESS_TEMPLATES.items():
        assert zh in table, f"L10n.table lacks progress template {zh!r} ({code})"
        ja, en = table[zh]
        assert ja and en
        assert _placeholders(ja) == _placeholders(zh) == _placeholders(en), code


def _registered_tool_names() -> set:
    names = set()
    for p in TOOLS_DIR.glob("*.py"):
        names.update(re.findall(r'^\s{4}name = "([a-z0-9_]+)"', p.read_text(), re.M))
    from framework.cloud.degrade import CLOUD_CONSULT_TOOL   # cloud_consult 以常量登记
    names.add(CLOUD_CONSULT_TOOL)
    return names


def test_tool_display_names_cover_every_registered_tool():
    names = _registered_tool_names()
    assert len(names) >= 55 and {"web_research", "web_read", "render_chart",
                                 "file_extract", "run_shell_command"} <= names
    display = _swift_string_dict(SWIFT_STEPTEXT.read_text(), "toolNames")
    missing = sorted(names - set(display))
    assert not missing, f"StepText.toolNames lacks: {missing}"
    table = _l10n_table()
    for tool, zh in display.items():
        assert zh in table, f"L10n.table lacks tool display name {zh!r} ({tool})"
        ja, en = table[zh]
        assert ja and en and "{" not in zh
