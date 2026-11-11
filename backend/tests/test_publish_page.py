"""publish_page（把 ~/AgentPages 的 HTML 发布成公开链接）金测试：源文件边界、发布目录只新建、
随机目录名 / 权限 / 文件名回落、撞名重抽与失败清理、未配置、审批分档（不钉死）、设置路由、审批卡预览。

隔离：数据根（AGENT_DATA_DIR → page_publish.json）、HOME（~/AgentPages）与发布 root 都是本测试的
临时目录；不联网。
"""
import os
import re
import stat

import pytest
from fastapi.testclient import TestClient

from framework.core import approval_policy
from framework.core.approval_policy import ApprovalPolicy
from framework.mcp.registry import MCPRegistry
from framework.mcp.tools import publish_page as pp
from framework.mcp.tools.publish_page import PublishPageTool
from framework.publish import settings as publish_settings

BASE_URL = "https://pages.example.com"
KEY = "routes-key-for-tests"
AUTH = {"Authorization": f"Bearer {KEY}"}
URL = "/api/v1/publish/settings"
PAGE = "<!doctype html><title>紅葉</title><h1>京都</h1>".encode("utf-8")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    data, home, root = tmp_path / "data", tmp_path / "home", tmp_path / "www"
    data.mkdir()
    (home / "AgentPages").mkdir(parents=True)
    root.mkdir()
    monkeypatch.setenv("AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(publish_settings, "_root_override", None)
    return {"data": data, "home": home, "pages": home / "AgentPages", "root": root,
            "tmp": tmp_path}


@pytest.fixture()
def configured(env):
    publish_settings.update({"root_dir": str(env["root"]), "base_url": BASE_URL + "/"})
    assert publish_settings.is_configured()
    page = env["pages"] / "plan.html"
    page.write_bytes(PAGE)
    return {**env, "page": page}


def _snapshot(root):
    """root 下全部条目 → {相对路径: 字节或 'dir'}（用于「别的东西没被动」的断言）。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root)] = "dir"
        for f in filenames:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


def _new_dirs(root, before):
    return sorted(set(os.listdir(root)) - set(before))


# ── 1. 源文件边界 ──────────────────────────────────────────────────────────────

def test_source_inside_agentpages_accepted_including_relative(configured):
    tool = PublishPageTool()
    for path in (str(configured["page"]), "~/AgentPages/plan.html", "plan.html"):
        assert tool.validate_arguments({"path": path}) is None, path


def test_source_refusals_create_nothing(configured):
    pages, tmp, root = configured["pages"], configured["tmp"], configured["root"]
    outside = tmp / "outside.html"
    outside.write_bytes(PAGE)
    (pages / "link.html").symlink_to(outside)
    (pages / "folder.html").mkdir()
    (pages / "notes.txt").write_text("x")
    (pages / "empty.html").write_bytes(b"")
    with open(pages / "big.html", "wb") as f:
        f.truncate(2 * 1024 * 1024 + 1)
    cases = {
        str(outside): "inside ~/AgentPages",
        "~/AgentPages/../../outside.html": "inside ~/AgentPages",
        str(pages / "link.html"): "inside ~/AgentPages",
        str(pages / "folder.html"): "not a regular file",
        str(pages / "missing.html"): "not found",
        str(pages / "notes.txt"): ".html / .htm",
        str(pages / "empty.html"): "empty",
        str(pages / "big.html"): "larger than 2 MB",
        str(pages): "inside ~/AgentPages",
        "": "'path' must be",
    }
    tool = PublishPageTool()
    for path, needle in cases.items():
        err = tool.validate_arguments({"path": path})
        assert err and err.startswith("System Error") and needle in err, (path, err)
        assert tool.execute(path=path).startswith("System Error")
    assert os.listdir(root) == []


def test_exactly_two_megabytes_and_uppercase_extension_accepted(configured):
    f = configured["pages"] / "OK.HTM"
    with open(f, "wb") as fh:
        fh.truncate(2 * 1024 * 1024)
    assert PublishPageTool().validate_arguments({"path": str(f)}) is None


@pytest.mark.parametrize("rel", ["secrets/page.html", ".ssh/page.html", ".env.html"])
def test_secret_looking_source_refused_by_the_real_detector(configured, rel):
    """真 shell_exec._secret_path_hit（不替身）：密钥目录段 / .env.* 名都拒，什么都不建。"""
    f = configured["pages"] / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(PAGE)
    tool = PublishPageTool()
    err = tool.validate_arguments({"path": str(f)})
    assert err and "credential" in err, err
    assert tool.execute(path=str(f)).startswith("System Error")
    assert os.listdir(configured["root"]) == []


# ── 2. 正常发布 ────────────────────────────────────────────────────────────────

def test_happy_path_modes_url_and_untouched_neighbours(configured):
    root = configured["root"]
    (root / "existing.txt").write_bytes(b"keep me")
    (root / "site").mkdir()
    (root / "site" / "index.html").write_bytes(b"<p>old</p>")
    before_snap = _snapshot(root)
    before = os.listdir(root)
    old_umask = os.umask(0o077)
    try:
        out = PublishPageTool().execute(path="plan.html")
    finally:
        os.umask(old_umask)
    assert not out.startswith("System Error"), out
    new = _new_dirs(root, before)
    assert len(new) == 1
    token = new[0]
    assert len(token) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", token)
    token_dir = root / token
    assert os.listdir(token_dir) == ["plan.html"]
    assert (token_dir / "plan.html").read_bytes() == PAGE
    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((token_dir / "plan.html").stat().st_mode) == 0o644
    first_line = out.splitlines()[0]
    assert first_line == f"{BASE_URL}/{token}/plan.html"
    assert "Anyone who has this link" in out
    after = _snapshot(root)
    for k, v in before_snap.items():
        assert after[k] == v
    # 再发一次：另一个目录
    out2 = PublishPageTool().execute(path="plan.html")
    new2 = _new_dirs(root, before + [token])
    assert len(new2) == 1 and new2[0] != token and new2[0] in out2


# ── 3. 文件名回落 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["紅葉プラン.html", "-dash.html", "with space.html",
                                  "a" * 81 + ".html", ".hidden.html"])
def test_odd_names_fall_back_to_index_html(configured, name):
    (configured["pages"] / name).write_bytes(PAGE)
    root = configured["root"]
    out = PublishPageTool().execute(path=str(configured["pages"] / name))
    assert not out.startswith("System Error"), out
    (token,) = os.listdir(root)
    assert os.listdir(root / token) == ["index.html"]
    assert out.splitlines()[0] == f"{BASE_URL}/{token}/index.html"


def test_safe_name_kept():
    assert pp._published_name("/x/Plan_v2.1.HTM") == "Plan_v2.1.HTM"
    assert pp._published_name("/x/a.html") == "a.html"


# ── 4. 撞名重抽与失败清理 ───────────────────────────────────────────────────────

def test_collision_draws_a_new_token(configured, monkeypatch):
    root = configured["root"]
    real_mkdir, tokens, calls = os.mkdir, iter(["A" * 32, "B" * 32]), []
    monkeypatch.setattr(pp.secrets, "token_urlsafe", lambda n: next(tokens))

    def mkdir(path, mode=0o777):
        calls.append(path)
        if len(calls) == 1:
            raise FileExistsError(path)
        return real_mkdir(path, mode)

    monkeypatch.setattr(pp.os, "mkdir", mkdir)
    out = PublishPageTool().execute(path="plan.html")
    assert [os.path.basename(c) for c in calls] == ["A" * 32, "B" * 32]
    assert os.listdir(root) == ["B" * 32] and f"/{'B' * 32}/plan.html" in out


def test_collisions_exhausted_creates_nothing(configured, monkeypatch):
    def mkdir(path, mode=0o777):
        raise FileExistsError(path)

    monkeypatch.setattr(pp.os, "mkdir", mkdir)
    out = PublishPageTool().execute(path="plan.html")
    assert out.startswith("System Error") and os.listdir(configured["root"]) == []


def test_write_failure_removes_only_what_this_call_created(configured, monkeypatch):
    root = configured["root"]
    (root / "keep.txt").write_bytes(b"keep")
    (root / "other").mkdir()
    before = _snapshot(root)
    real_fdopen, seen = os.fdopen, []

    def failing_fdopen(fd, *a, **kw):
        seen.append(fd)                       # 真 fdopen 失败时不接管描述符
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pp.os, "fdopen", failing_fdopen)
    out = PublishPageTool().execute(path="plan.html")
    monkeypatch.setattr(pp.os, "fdopen", real_fdopen)
    assert out.startswith("System Error") and "OSError" in out
    assert _snapshot(root) == before
    with pytest.raises(OSError):              # 目标描述符已被关掉，没有泄漏
        os.fstat(seen[0])


def test_failure_before_file_creation_removes_token_dir(configured, monkeypatch):
    root = configured["root"]

    real_open = os.open

    def failing_open(path, flags, *a, **kw):
        if flags & os.O_CREAT:                # 只让「建目标文件」失败；读源照常
            raise PermissionError(13, "denied")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(pp.os, "open", failing_open)
    out = PublishPageTool().execute(path="plan.html")
    assert out.startswith("System Error") and "PermissionError" in out
    assert os.listdir(root) == []


# ── 5. root 不存在 / 不是目录 ──────────────────────────────────────────────────

def test_missing_root_is_refused_and_not_created(env):
    page = env["pages"] / "p.html"
    page.write_bytes(PAGE)
    missing = env["tmp"] / "nowhere" / "www"
    publish_settings.update({"root_dir": str(missing), "base_url": BASE_URL})
    tool = PublishPageTool()
    assert tool.is_available() is True              # 只看设置，不碰文件系统
    out = tool.execute(path="p.html")
    assert out.startswith("System Error") and "publish folder" in out
    assert not missing.exists() and not missing.parent.exists()


def test_root_that_is_a_file_is_refused(env):
    (env["pages"] / "p.html").write_bytes(PAGE)
    f = env["tmp"] / "www-file"
    f.write_bytes(b"not a dir")
    publish_settings.update({"root_dir": str(f), "base_url": BASE_URL})
    out = PublishPageTool().execute(path="p.html")
    assert out.startswith("System Error") and f.read_bytes() == b"not a dir"


def test_is_available_does_not_touch_root(env, monkeypatch):
    publish_settings.update({"root_dir": str(env["tmp"] / "www"), "base_url": BASE_URL})
    root = str(env["tmp"] / "www")
    for name in ("stat", "lstat", "listdir", "scandir"):
        real = getattr(os, name)

        def guard(p, *a, _real=real, **kw):
            assert not str(p).startswith(root), f"is_available touched the publish root ({p})"
            return _real(p, *a, **kw)

        monkeypatch.setattr(os, name, guard)
    assert PublishPageTool().is_available() is True


# ── 6. 未配置 ──────────────────────────────────────────────────────────────────

def test_unconfigured_refuses(env):
    (env["pages"] / "p.html").write_bytes(PAGE)
    tool = PublishPageTool()
    assert tool.is_available() is False
    err = tool.validate_arguments({"path": "p.html"})
    assert err and "not set up" in err
    assert tool.execute(path="p.html").startswith("System Error")
    assert os.listdir(env["root"]) == []


# ── 7. 审批分档：写档工具、不钉死 ─────────────────────────────────────────────

@pytest.mark.parametrize("policy, expected", [
    (approval_policy.DEFAULT, True),
    (ApprovalPolicy(write="ask"), True),
    (ApprovalPolicy(write="auto"), False),
    (ApprovalPolicy(read="ask", write="auto"), False),
])
def test_follows_the_write_policy_and_is_not_pinned(tmp_path, policy, expected):
    registry = MCPRegistry(tmp_path / "no-tools")
    registry.register(PublishPageTool())
    assert PublishPageTool.approval_pinned is False and PublishPageTool.effect == "write"
    assert registry.approval_required("publish_page", {"path": "p.html"}, policy) is expected


# ── 8. 设置路由 ────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch, env):
    import app as entry
    from framework.api.devices import DeviceStore
    for name, value in (("api_key", KEY), ("auth_devices", None),
                        ("device_store", DeviceStore(env["data"] / "auth" / "devices.json")),
                        ("compute_ready", True), ("compute_installed", True),
                        ("compute_state", "ready")):
        monkeypatch.setattr(entry.app.state, name, value, raising=False)
    return TestClient(entry.app, raise_server_exceptions=False)


def test_routes_get_put_normalise_and_mode(client, env):
    r = client.get(URL, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"root_dir": "", "base_url": "",
                                                 "configured": False}
    r = client.put(URL, json={"root_dir": "~/www-root", "base_url": BASE_URL + "/p///"},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"root_dir": str(env["home"] / "www-root"),
                        "base_url": BASE_URL + "/p", "configured": True}
    path = env["data"] / publish_settings.FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (env["home"] / "www-root").exists()     # 保存不建目录
    r = client.put(URL, json={"root_dir": "", "base_url": ""}, headers=AUTH)
    assert r.status_code == 200 and r.json()["configured"] is False


_INVALID = [
    {"root_dir": "relative/dir"},
    {"root_dir": "/"},
    {"root_dir": "~"},
    {"root_dir": "~/"},
    {"root_dir": "/tmp/a\nb"},
    {"root_dir": ""},                                  # 只填一个
    {"base_url": ""},
    {"base_url": "http://pages.example.com"},
    {"base_url": "HTTPS://pages.example.com"},
    {"base_url": "ftp://pages.example.com"},
    {"base_url": "https://localhost"},
    {"base_url": "https://user@pages.example.com"},
    {"base_url": "https://user:pw@pages.example.com"},
    {"base_url": "https://pages.example.com:8443"},
    {"base_url": "https://pages.example.com/?q=1"},
    {"base_url": "https://pages.example.com/#frag"},
    {"base_url": "https://pages.example.com/a b"},
    {"base_url": "https://pages.example.com/a\tb"},
    {"base_url": "https://pag_es.example.com"},
    {"base_url": "pages.example.com"},
    {"base_url": "https://"},
    {"unknown": 1},
    {"root_dir": 5},
]


@pytest.mark.parametrize("patch", _INVALID)
def test_put_rejects_invalid_and_leaves_file_unchanged(client, env, patch):
    good = {"root_dir": str(env["root"]), "base_url": BASE_URL}
    assert client.put(URL, json=good, headers=AUTH).status_code == 200
    path = env["data"] / publish_settings.FILENAME
    before = path.read_bytes()
    r = client.put(URL, json={**good, **patch}, headers=AUTH)
    assert r.status_code == 422, (patch, r.status_code, r.text)
    assert path.read_bytes() == before


def test_put_requires_auth_and_accepts_paired_devices(client, env):
    import app as entry
    body = {"root_dir": str(env["root"]), "base_url": BASE_URL}
    assert client.get(URL).status_code == 401
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert client.put(URL, json=body, headers=headers).status_code == 401
    assert client.get(URL, headers=AUTH).json()["configured"] is False
    store = entry.app.state.device_store
    token, record, _ = store.issue(name="iPhone", via="qr")
    device = {"Authorization": f"Bearer {token}"}
    assert client.put(URL, json=body, headers=device).status_code == 200
    store.revoke(record["id"])
    assert client.put(URL, json=body, headers=device).status_code == 401


def test_routes_mount_same_auth_as_mail_and_work_before_compute(client, monkeypatch):
    import app as entry
    from framework.api import routes
    from framework.api.auth import require_owner, require_owner_or_paired
    deps = {}
    for r in entry.app.routes:
        if getattr(r, "path", "") == URL:
            for m in r.methods:
                deps[m] = {d.call for d in r.dependant.dependencies}
    assert set(deps) == {"GET", "PUT"}
    assert require_owner_or_paired in deps["PUT"] and require_owner not in deps["PUT"]
    assert require_owner_or_paired not in deps["GET"]
    assert entry.verify_api_key in deps["GET"] and entry.verify_api_key in deps["PUT"]
    for op in (("GET", URL), ("PUT", URL)):
        assert op in routes._LOCAL_READ_ROUTES and op not in routes._EXECUTION_ROUTES
    for name, value in (("compute_ready", False), ("compute_installed", False),
                        ("compute_state", "retrying")):
        monkeypatch.setattr(entry.app.state, name, value, raising=False)
    assert client.get(URL, headers=AUTH).status_code == 200


def test_settings_reread_after_file_change_and_corrupt_is_unconfigured(env):
    publish_settings.update({"root_dir": str(env["root"]), "base_url": BASE_URL})
    assert publish_settings.is_configured()
    path = env["data"] / publish_settings.FILENAME
    path.write_text("{broken")
    assert not publish_settings.is_configured()


# ── 9. 审批卡预览 ─────────────────────────────────────────────────────────────

def test_preview_rows(configured):
    with open(configured["pages"] / "big.html", "wb") as f:
        f.write(b"x" * 3072)
    rows = PublishPageTool().approval_preview({"path": "big.html"})
    assert rows == [{"key": "file", "value": "big.html (3.0 KB)"},
                    {"key": "site", "value": "pages.example.com"}]


def test_preview_in_pending_payload(configured, make_session):
    import asyncio
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
    from .test_orchestrator_lifecycles import GEN, begin_turn
    real = PublishPageTool()
    tool = FakeTool("publish_page", approval=True, read_only=False, concurrency_safe=False,
                    validate=real.validate_arguments, parameters=real.parameters)
    tool.approval_preview = real.approval_preview
    approval_policy.replace(approval_policy.DEFAULT)
    registry = FakeRegistry([tool])
    orch = Orchestrator(FakeEngine([{"tool_calls": [call("publish_page", path="plan.html")]},
                                    {"text": "done"}]),
                        registry, {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    result = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "pending_approval"
    assert result["pending_tools"] == [{
        "name": "publish_page", "arguments": {"path": "plan.html"},
        "preview": [{"key": "file", "value": f"plan.html ({len(PAGE)} B)"},
                    {"key": "site", "value": "pages.example.com"}]}]
    assert registry.executed == []


# ── 第二轮评审补的护栏 oracle ──────────────────────────────────────────────────

def test_execute_refuses_unknown_arguments_and_creates_nothing(configured):
    out = PublishPageTool().execute(path="plan.html", overwrite=True)
    assert out.startswith("System Error") and "unknown argument" in out
    assert os.listdir(configured["root"]) == []


def test_read_source_refuses_a_symlinked_last_component(configured, tmp_path):
    outside = tmp_path / "outside.html"
    outside.write_bytes(b"<p>not this</p>")
    link = configured["pages"] / "swapped.html"
    link.symlink_to(outside)
    data, err = pp.read_source(str(link))
    assert data is None and "cannot be opened" in err


def test_source_swapped_for_symlink_after_the_check_publishes_nothing(configured, tmp_path,
                                                                      monkeypatch):
    """检查通过之后、读之前，源文件最后一段被换成指向外面的软链：O_NOFOLLOW 打不开，什么都不发布。"""
    outside = tmp_path / "outside.html"
    outside.write_bytes(b"<p>secret elsewhere</p>")
    page = configured["page"]
    real_check = pp.check_call

    def check_then_swap(arguments):
        plan, err = real_check(arguments)
        page.unlink()
        page.symlink_to(outside)
        return plan, err

    monkeypatch.setattr(pp, "check_call", check_then_swap)
    out = PublishPageTool().execute(path="plan.html")
    assert out.startswith("System Error") and "cannot be opened" in out
    assert os.listdir(configured["root"]) == []


def test_read_source_refuses_non_regular_and_oversize(configured):
    assert pp.read_source(str(configured["pages"]))[0] is None          # 目录
    big = configured["pages"] / "big.html"
    with open(big, "wb") as f:
        f.truncate(2 * 1024 * 1024 + 1)
    assert "larger than 2 MB" in pp.read_source(str(big))[1]
    assert pp.read_source(str(configured["page"])) == (PAGE, None)
