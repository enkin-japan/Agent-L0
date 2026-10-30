"""超长观察落盘的回取容错金测试（3a）：短句柄 `spill:<hash>` 与抄错的落盘绝对路径。

实案 app-9daf9c9fee44：模型把中缝里的 `…/dev.enkin.macagent/…/spill/<hash>.txt` 两次抄成
`dev.inkin` / `dev.injin`，file_extract 两次 not found。修法只在**当前会话**的 spill 目录内
解析，realpath 越界（符号链接出逃、`spill:../x`）一律照旧报 not found。
"""
import os

import pytest

from framework.agents import runtime
from framework.core.spill import spill_handle, spill_observation, truncate_observation
from framework.mcp.tools.file_extract import FileExtractTool

BODY = "".join(f"line {i}: SPILL-MARKER-{i}\n" for i in range(1, 301))
NOT_FOUND = "System Error: file not found: "


@pytest.fixture()
def spilled(make_session):
    session = make_session("spill-sess")
    path, _ = spill_observation(session, "ab12cd34", BODY)
    token = runtime._CURRENT_SESSION.set(session)
    yield session, path
    runtime._CURRENT_SESSION.reset(token)


def _read(path, **kw):
    return FileExtractTool().execute(path=path, start=10, end=12, **kw)


def test_exact_path_still_works(spilled):
    _, path = spilled
    assert "SPILL-MARKER-11" in _read(path)


@pytest.mark.parametrize("bad_segment", ["/sessions/", "/spill-sess/"])
def test_miscopied_absolute_path_resolves_in_current_session(spilled, bad_segment):
    _, path = spilled
    miscopied = path.replace(bad_segment, bad_segment.replace("s", "z", 1))
    assert miscopied != path and not os.path.exists(miscopied)
    assert "SPILL-MARKER-11" in _read(miscopied)


@pytest.mark.parametrize("handle", ["spill:ab12cd34", "spill:ab12cd34.txt"])
def test_short_handle_resolves(spilled, handle):
    assert "SPILL-MARKER-11" in _read(handle)


@pytest.mark.parametrize("handle", ["spill:../x", "spill:../../spill-sess/spill/ab12cd34",
                                    "spill:/etc/passwd", "spill:", "spill:AB12CD34"])
def test_traversal_and_malformed_handles_rejected(spilled, handle):
    assert _read(handle) == NOT_FOUND + handle


def test_symlink_escaping_the_spill_dir_is_rejected(spilled, tmp_path):
    """文件名合法、但 realpath 出了会话 spill 目录 → 拒（判据是 realpath 包含，不是文件名）。"""
    session, path = spilled
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("TOP-SECRET\n" * 20)
    link = session.session_dir / "spill" / "deadbeef.txt"
    os.symlink(outside, link)
    for ref in ("spill:deadbeef", path.replace("ab12cd34", "deadbeef").replace("/sessions/", "/sezsions/")):
        out = _read(ref)
        assert out == NOT_FOUND + ref and "TOP-SECRET" not in out


def test_other_sessions_spill_is_not_reachable(spilled, make_session):
    other = make_session("other-sess")
    other_path, _ = spill_observation(other, "cafe0001", "OTHER-SESSION\n" * 5)
    assert _read("spill:cafe0001") == NOT_FOUND + "spill:cafe0001"
    miscopied = other_path.replace("/sessions/", "/sezsions/")
    assert _read(miscopied) == NOT_FOUND + miscopied


@pytest.mark.parametrize("ref", ["/nope/really/missing.txt", "/x/spill/not-a-hash.txt",
                                 "/x/notspill/ab12cd34.txt", "relative/missing.pdf"])
def test_unrelated_missing_paths_still_not_found(spilled, ref):
    assert _read(ref) == NOT_FOUND + ref


def test_no_session_context_means_no_fallback(spilled):
    _, path = spilled
    token = runtime._CURRENT_SESSION.set(None)
    try:
        assert _read("spill:ab12cd34") == NOT_FOUND + "spill:ab12cd34"
    finally:
        runtime._CURRENT_SESSION.reset(token)


def test_hint_offers_the_short_handle(spilled):
    _, path = spilled
    assert spill_handle(path) == "spill:ab12cd34"
    seam = truncate_observation(BODY, 4000, (path, len(BODY)))
    assert f'path="{path}"' in seam and 'path="spill:ab12cd34"' in seam
    one_line = "x" * 9000
    seam1 = truncate_observation(one_line, 4000, (path, len(one_line)))
    assert 'path="spill:ab12cd34"' in seam1
