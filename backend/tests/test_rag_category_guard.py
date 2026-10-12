"""标准 CPU 门持续验证实际 RAG 分类/落盘护栏；不导入 Chroma 或计算面。"""
import ast
import asyncio
import ipaddress
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]


def _load_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


@pytest.fixture
def guards(tmp_path):
    storage = tmp_path / "storage"
    raw = storage / "raw_files"
    unclassified = storage / "unclassified_raw"
    raw.mkdir(parents=True)
    unclassified.mkdir()
    namespace = dict(Path=Path, Optional=Optional, HTTPException=HTTPException,
                     ipaddress=ipaddress, re=re, os=os, DATA_ROOT=tmp_path,
                     STORAGE_BASE=storage, RAW_FILES_DIR=raw, UNCLASSIFIED_DIR=unclassified)
    server = _load_functions(ROOT / "rag/rag_server.py",
        ["_validate_category_id", "_checked_storage_path", "_category_raw_dir", "_move_raw_file_no_replace"], namespace)
    admin = _load_functions(ROOT / "backend/framework/mcp/tools/rag_admin.py",
        ["_valid_category_id"], dict(ipaddress=ipaddress, re=re))
    return server, admin


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "a/b", "a\\b", "a..b", "127.0.0.1", "meta_classification_db", "ab", "x" * 513])
def test_actual_server_and_admin_reject_invalid_ids(guards, name):
    server, admin = guards
    with pytest.raises(HTTPException) as err:
        server._validate_category_id(name)
    assert err.value.status_code == 400
    assert not admin._valid_category_id(name)


@pytest.mark.parametrize("name", ["faq", "Legacy.Docs-2026", "999.999.999.999", "01.2.3.4", "x" * 64, "x" * 512])
def test_identifier_compatibility_separate_from_raw_directory_limit(guards, name):
    server, admin = guards
    assert server._validate_category_id(name) == name
    assert admin._valid_category_id(name)
    if len(os.fsencode(name)) > os.pathconf(server.RAW_FILES_DIR, "PC_NAME_MAX"):
        with pytest.raises(HTTPException) as err:
            server._category_raw_dir(name)
        assert "文件系统限制" in err.value.detail
    else:
        assert server._category_raw_dir(name) == server.RAW_FILES_DIR / name


@pytest.mark.parametrize("root", ["category", "raw", "storage"])
def test_resolved_paths_reject_symlink_escape(guards, tmp_path, root):
    server, _ = guards
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    if root == "category":
        (server.RAW_FILES_DIR / "faq").symlink_to(outside, target_is_directory=True)
    elif root == "raw":
        server.RAW_FILES_DIR.rmdir()
        server.RAW_FILES_DIR.symlink_to(outside, target_is_directory=True)
    else:
        server.RAW_FILES_DIR.rmdir()
        server.UNCLASSIFIED_DIR.rmdir()
        server.STORAGE_BASE.rmdir()
        server.STORAGE_BASE.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException):
        server._category_raw_dir("faq")
    assert list(outside.iterdir()) == []


def test_actual_create_entry_rejects_before_compute_or_meta(guards):
    server, _ = guards

    def forbidden(*args, **kwargs):
        pytest.fail("invalid category reached a compute/meta write")

    namespace = dict(vars(server), CategoryItem=SimpleNamespace,
                     _refuse_if_rebuilding=lambda op: None,
                     get_chroma_collection=forbidden, get_embeddings=forbidden)
    entry = _load_functions(ROOT / "rag/rag_server.py", ["upsert_category"], namespace)
    for name in ["../outside", "a..b", "127.0.0.1", "x" * 512]:
        with pytest.raises(HTTPException):
            asyncio.run(entry.upsert_category(SimpleNamespace(db_name=name, description="synthetic")))


def test_atomic_raw_move_does_not_overwrite_existing_file(guards):
    server, _ = guards
    source = server.UNCLASSIFIED_DIR / "source.txt"
    source.write_text("source original")
    destination = server.RAW_FILES_DIR / "destination.txt"
    destination.write_text("target original")
    with pytest.raises(HTTPException) as err:
        server._move_raw_file_no_replace(source, destination)
    assert err.value.status_code == 409
    assert source.read_text() == "source original"
    assert destination.read_text() == "target original"
