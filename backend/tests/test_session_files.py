"""GET /api/v1/files/{session_id}（会话产物清单）金测试：全部离线。

钉住的行为：
1. 只列 assets/ 里的常规文件，隐藏文件/子目录/不合出货白名单的名字被滤掉；
2. 按 mtime 降序；
3. 无 assets 目录 = 空清单（不是 404——「还没有产物」是正常态）；
4. 非法会话 ID → 400（与既有资产出货端点同一道闸）。
"""
import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from framework.api.routes import list_session_files


def _request_with(sessions_dir):
    state = SimpleNamespace(session_manager=SimpleNamespace(sessions_dir=sessions_dir))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_listing_filters_and_sorts(tmp_path):
    assets = tmp_path / "s1" / "assets"
    assets.mkdir(parents=True)
    (assets / "old.pdf").write_bytes(b"a")
    (assets / "new.mp4").write_bytes(b"bb")
    (assets / ".DS_Store").write_bytes(b"x")          # 隐藏文件不出货
    (assets / "sub").mkdir()                          # 子目录不出货
    os.utime(assets / "old.pdf", (1000, 1000))
    os.utime(assets / "new.mp4", (2000, 2000))

    res = asyncio.run(list_session_files("s1", _request_with(tmp_path)))
    names = [f["name"] for f in res["files"]]
    assert names == ["new.mp4", "old.pdf"]            # mtime 降序
    assert res["files"][0]["size"] == 2
    assert res["files"][1]["mtime"] == 1000


def test_no_assets_dir_is_empty_not_404(tmp_path):
    (tmp_path / "s2").mkdir()
    res = asyncio.run(list_session_files("s2", _request_with(tmp_path)))
    assert res == {"status": True, "files": []}


def test_invalid_session_id_rejected(tmp_path):
    with pytest.raises(HTTPException) as e:
        asyncio.run(list_session_files("../etc", _request_with(tmp_path)))
    assert e.value.status_code == 400


def test_route_registered():
    from framework.api.routes import router
    paths = [r.path for r in router.routes]
    assert "/api/v1/files/{session_id}" in paths
    # 既有的两个 files 子路径不受影响
    assert "/api/v1/files/{session_id}/assets/{filename}" in paths
    assert "/api/v1/files/{session_id}/deck" in paths
