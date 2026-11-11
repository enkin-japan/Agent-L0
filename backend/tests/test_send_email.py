"""send_email（只发不收、不带附件的邮件工具）金测试：设置存储 / 路由、收件人白名单、实参校验、
SMTP 发送与失败文案、时间预算、钉死 + 只认卡审批、审批卡预览与批准前复核、审批推送。

隔离：数据根（AGENT_DATA_DIR → 设置 json 与 api_tools/.secrets.env）与 HOME 都钉到本测试的临时
目录；本文件的 env 装置把 conftest 的内存密钥替身换回真实 api_tool_factory（只对临时数据根读写）；
MAIL_SMTP_PASSWORD 环境变量清掉（read_secret 先读 env）。smtplib.SMTP_SSL 换成记录调用的
替身——不联网、不发真信、不碰真实密钥文件。
"""
import asyncio
import json
import os
import smtplib
import stat
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from framework.core import approval_policy
from framework.core.approval_policy import ApprovalPolicy
from framework.mail import settings as mail_settings
from framework.mcp.registry import MCPRegistry
from framework.mcp.tools import send_email as se
from framework.mcp.tools.send_email import SendEmailTool

PW = "app-pass-T3st-0042-zz"
PW2 = "app-pass-T3st-0043-yy"
KEY = "routes-key-for-tests"
AUTH = {"Authorization": f"Bearer {KEY}"}
URL = "/api/v1/mail/settings"

CONTACTS = [{"name": "田中", "address": "tanaka@example.com"},
            {"name": "Taro", "address": "taro.y@example.org"},
            {"name": "ﾔﾏﾀﾞ", "address": "yamada@example.net"}]
BASE_SETTINGS = {"host": "smtp.fastmail.com", "port": 465, "username": "me@example.com",
                 "from_addr": "", "contacts": CONTACTS}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    data, home = tmp_path / "data", tmp_path / "home"
    data.mkdir()
    (home / "AgentPages").mkdir(parents=True)
    monkeypatch.setenv("AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(mail_settings.SECRET_NAME, raising=False)
    monkeypatch.setattr(mail_settings, "_root_override", None)
    monkeypatch.setattr(mail_settings, "_factory", mail_settings._default_factory)
    return {"data": data, "home": home, "pages": home / "AgentPages"}


@pytest.fixture()
def configured(env):
    mail_settings.update(dict(BASE_SETTINGS), new_password=PW)
    assert mail_settings.is_configured()
    return env


class FakeSMTP:
    """smtplib.SMTP_SSL 替身：记录构造参数与每次调用；按 fail 注入异常。"""

    def __init__(self, recorder, fail):
        self.rec, self.fail = recorder, fail

    def __call__(self, host, port, timeout=None, context=None):
        self.rec.append(("connect", host, port, timeout, context))
        if "connect" in self.fail:
            raise self.fail["connect"]
        return self

    def login(self, user, password):
        self.rec.append(("login", user, password))
        if "login" in self.fail:
            raise self.fail["login"]

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.rec.append(("send", msg, from_addr, list(to_addrs or [])))
        if "send" in self.fail:
            raise self.fail["send"]
        return {}

    def quit(self):
        self.rec.append(("quit",))
        if "quit" in self.fail:
            raise self.fail["quit"]

    def close(self):
        self.rec.append(("close",))


@pytest.fixture()
def smtp(monkeypatch):
    rec, fail = [], {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP(rec, fail))
    return rec, fail


def _send(**kw):
    args = {"to": "田中", "subject": "見積もりの件", "body": "こんにちは。\n資料を送ります。"}
    args.update(kw)
    return args


def _sends(rec):
    return [r for r in rec if r[0] == "send"]


# ── 1. 钉死审批：写档 auto 仍出卡（真实判定点 MCPRegistry.approval_required） ─────────

@pytest.mark.parametrize("read", approval_policy.MODES)
@pytest.mark.parametrize("write", approval_policy.MODES)
def test_send_email_asks_under_every_policy_including_write_auto(tmp_path, read, write):
    registry = MCPRegistry(tmp_path / "no-tools")
    registry.register(SendEmailTool())
    assert SendEmailTool.approval_pinned is True and SendEmailTool.requires_approval is True
    assert SendEmailTool.effect == "write"
    assert registry.approval_required("send_email", _send(),
                                      ApprovalPolicy(read=read, write=write)) is True


# ── 2. 未登记收件人：预检拒、直接 execute 也一封不发 ─────────────────────────────

@pytest.mark.parametrize("to", ["鈴木さん", "stranger@example.com", "TANAKA@example.co"])
def test_unregistered_recipient_refused_and_nothing_sent(configured, smtp, to):
    rec, _ = smtp
    tool = SendEmailTool()
    err = tool.validate_arguments(_send(to=to))
    assert err and err.startswith("System Error") and "not a registered mail contact" in err
    # 只列名字，绝不列地址
    assert "田中" in err and "Taro" in err
    assert "tanaka@example.com" not in err and "yamada@example.net" not in err
    out = tool.execute(**_send(to=to))
    assert out.startswith("System Error")
    assert rec == []          # SMTP 替身从未被构造


# ── 3. 名字解析 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("to, address", [
    ("田中", "tanaka@example.com"),
    ("田中さん", "tanaka@example.com"),
    ("  田中様 ", "tanaka@example.com"),
    ("田中先生", "tanaka@example.com"),
    ("TANAKA@EXAMPLE.COM", "tanaka@example.com"),
    ("ｔａｒｏ", "taro.y@example.org"),          # 全角 → NFKC
    ("taro", "taro.y@example.org"),
    ("Taroくん", "taro.y@example.org"),
    ("ヤマダさん", "yamada@example.net"),           # 半角片假名登记、全角输入
    ("Taro.Y@Example.ORG", "taro.y@example.org"),
])
def test_name_and_address_resolution(to, address):
    contact, err = se.resolve_recipient(to, CONTACTS)
    assert err is None and contact["address"] == address


def test_only_one_honorific_is_stripped():
    contact, err = se.resolve_recipient("田中さんさん", CONTACTS)
    assert contact is None and "not a registered" in err


def test_ambiguous_match_is_refused():
    contacts = [{"name": "Ann", "address": "ann@example.com"},
                {"name": "ann@example.com", "address": "other@example.com"}]
    contact, err = se.resolve_recipient("ann@example.com", contacts)
    assert contact is None and "more than one" in err
    assert "Ann" in err and "other@example.com" not in err


# ── 4. 多收件人形态 / 主题 / 正文 ──────────────────────────────────────────────

@pytest.mark.parametrize("args, needle", [
    (_send(to="田中, Taro"), "exactly one recipient"),
    (_send(to="田中;Taro"), "exactly one recipient"),
    (_send(to="田中\nTaro"), "exactly one recipient"),
    (_send(to="田中\r"), "exactly one recipient"),
    (_send(to="田中\u2028Taro"), "exactly one recipient"),
    (_send(to="田中\x85Taro"), "exactly one recipient"),
    (_send(subject="件名\x07ベル"), "control characters"),
    (_send(subject="件名\x1b[31m"), "control characters"),
    (_send(subject="件名\r\nBcc: x@example.com"), "one line"),
    (_send(subject="件名\nほか"), "one line"),
    (_send(subject="   "), "subject must not be empty"),
    (_send(subject="x" * 201), "one line"),
    (_send(body=""), "body must not be empty"),
    (_send(body=" \n "), "body must not be empty"),
    (_send(body="あ" * 4001), "at most 4000"),
])
def test_argument_checks(configured, smtp, args, needle):
    rec, _ = smtp
    tool = SendEmailTool()
    err = tool.validate_arguments(args)
    assert err and needle in err, err
    assert tool.execute(**args).startswith("System Error")
    assert rec == []


def test_execute_refuses_unknown_arguments(configured, smtp):
    rec, _ = smtp
    for extra in ({"attachment": "~/AgentPages/plan.html"}, {"cc": "x@example.com"}):
        out = SendEmailTool().execute(**_send(), **extra)
        assert out.startswith("System Error") and "unknown argument" in out
    assert rec == []


def test_attachment_parameter_is_gone():
    props = SendEmailTool.parameters["properties"]
    assert set(props) == {"to", "subject", "body"}
    assert "publish_page" in SendEmailTool.description


def test_limits_are_inclusive(configured):
    tool = SendEmailTool()
    assert tool.validate_arguments(_send(subject="x" * 200, body="あ" * 4000)) is None


# ── 6. is_available ⇔ 已配置 ────────────────────────────────────────────────────

def test_is_available_requires_username_contacts_and_password(env):
    tool = SendEmailTool()
    assert tool.is_available() is False                         # 什么都没有
    mail_settings.update(dict(BASE_SETTINGS))                    # 无密码
    assert tool.is_available() is False
    mail_settings.update(dict(BASE_SETTINGS), new_password=PW)
    assert tool.is_available() is True
    mail_settings.update({**BASE_SETTINGS, "contacts": []})      # 联系人清空
    assert tool.is_available() is False
    mail_settings.update({**BASE_SETTINGS, "username": ""})     # 用户名为空
    assert tool.is_available() is False
    mail_settings.update(dict(BASE_SETTINGS), clear_password=True)
    assert tool.is_available() is False
    err = tool.validate_arguments(_send())
    assert err and "not set up" in err


def test_unconfigured_execute_sends_nothing(env, smtp):
    rec, _ = smtp
    assert SendEmailTool().execute(**_send()).startswith("System Error")
    assert rec == []


# ── 7. 正常发送 ────────────────────────────────────────────────────────────────

def test_happy_path(configured, smtp):
    rec, _ = smtp
    body = ("田中さん\n\n京都の紅葉プランです。\n"
            "https://pages.example.com/AbCdEfGhIjKlMnOpQrStUvWxYz012345/plan.html\n"
            "よろしくお願いします。")
    out = SendEmailTool().execute(to="田中さん", subject="紅葉プラン", body=body)
    assert not out.startswith("System Error"), out
    assert "田中 <tanaka@example.com>" in out and "紅葉プラン" in out
    connect = rec[0]
    assert connect[:4] == ("connect", "smtp.fastmail.com", 465, 10)
    assert SendEmailTool.timeout_seconds == 120
    import ssl
    assert isinstance(connect[4], ssl.SSLContext)
    assert connect[4].verify_mode == ssl.CERT_REQUIRED and connect[4].check_hostname
    assert ("login", "me@example.com", PW) in rec
    sends = _sends(rec)
    assert len(sends) == 1
    _, msg, from_addr, to_addrs = sends[0]
    assert from_addr == "me@example.com" and to_addrs == ["tanaka@example.com"]
    parsed = BytesParser(policy=email_policy.default).parsebytes(msg.as_bytes())
    assert parsed["From"] == "me@example.com"
    assert parsed["To"].addresses[0].addr_spec == "tanaka@example.com"
    assert parsed["To"].addresses[0].display_name == "田中"
    assert parsed["Subject"] == "紅葉プラン"
    assert parsed["Message-ID"].endswith("@example.com>") and parsed["Date"]
    assert parsed.get_body(("plain",)).get_content().rstrip("\n") == body
    assert list(parsed.iter_attachments()) == [] and not parsed.is_multipart()
    assert rec[-1] == ("quit",)


def test_from_addr_overrides_username(env, smtp):
    rec, _ = smtp
    mail_settings.update({**BASE_SETTINGS, "from_addr": "alias@example.com"}, new_password=PW)
    out = SendEmailTool().execute(**_send())
    assert out.startswith("Email sent")
    _, msg, from_addr, _ = _sends(rec)[0]
    assert from_addr == "alias@example.com" and msg["From"] == "alias@example.com"
    assert ("login", "me@example.com", PW) in rec


# ── 8. 失败文案 ────────────────────────────────────────────────────────────────

def _fail_case(smtp, stage, exc):
    rec, fail = smtp
    fail[stage] = exc
    return SendEmailTool().execute(**_send()), rec


def test_auth_failure_text(configured, smtp):
    out, rec = _fail_case(smtp, "login", smtplib.SMTPAuthenticationError(535, b"bad " + PW.encode()))
    assert out.startswith("System Error") and "app password" in out and "mail settings" in out
    assert PW not in out and _sends(rec) == []


def test_connect_failure_text(configured, smtp):
    out, rec = _fail_case(smtp, "connect", ConnectionRefusedError(61, "refused"))
    assert out.startswith("System Error") and "ConnectionRefusedError" in out
    assert "NOT sent" in out and PW not in out and _sends(rec) == []


def test_recipient_refused_is_a_definite_failure(configured, smtp):
    out, _ = _fail_case(smtp, "send", smtplib.SMTPRecipientsRefused(
        {"tanaka@example.com": (550, b"no such user")}))
    assert out.startswith("System Error") and "NOT sent" in out and "550" in out
    assert "UNCERTAIN" not in out and "tanaka@example.com" not in out


@pytest.mark.parametrize("exc", [smtplib.SMTPDataError(451, b"try later"),
                                 smtplib.SMTPSenderRefused(553, b"no", "me@example.com")])
def test_server_rejection_inside_send_message_is_a_definite_failure(configured, smtp, exc):
    out, rec = _fail_case(smtp, "send", exc)
    assert out.startswith("System Error") and "NOT sent" in out and "UNCERTAIN" not in out
    assert str(exc.smtp_code) in out and len(_sends(rec)) == 1


@pytest.mark.parametrize("exc", [smtplib.SMTPServerDisconnected("gone"), TimeoutError("slow"),
                                 ConnectionResetError(54, "reset")])
def test_failure_inside_send_message_is_uncertain(configured, smtp, exc):
    out, rec = _fail_case(smtp, "send", exc)
    assert out.startswith("System Error") and "UNCERTAIN" in out
    assert "Do NOT call send_email again" in out and type(exc).__name__ in out
    assert PW not in out and len(_sends(rec)) == 1


def test_slow_connect_aborts_before_any_data_is_sent(configured, smtp, monkeypatch):
    rec, _ = smtp
    ticks = iter([1000.0, 1031.0])            # 起点、send_message 之前
    monkeypatch.setattr(se, "_clock", lambda: next(ticks))
    out = SendEmailTool().execute(**_send())
    assert out.startswith("System Error") and "NOT sent" in out and "too long" in out
    assert _sends(rec) == [] and ("close",) in rec


def test_just_under_the_deadline_still_sends(configured, smtp, monkeypatch):
    rec, _ = smtp
    ticks = iter([1000.0, 1029.5])
    monkeypatch.setattr(se, "_clock", lambda: next(ticks))
    assert SendEmailTool().execute(**_send()).startswith("Email sent")
    assert len(_sends(rec)) == 1


def test_timing_budget_fits_inside_the_tool_timeout():
    # 截止前 30 s + send_message 与 QUIT 至多 5 次往返 × 套接字超时，必须小于工具级熔断
    assert se.SEND_DEADLINE + 5 * se.SMTP_TIMEOUT < SendEmailTool.timeout_seconds


def test_quit_failure_after_send_counts_as_success(configured, smtp):
    out, rec = _fail_case(smtp, "quit", smtplib.SMTPServerDisconnected("bye"))
    assert out.startswith("Email sent to 田中 <tanaka@example.com>")
    assert len(_sends(rec)) == 1


def test_password_never_in_any_result_or_log(configured, smtp, capsys):
    rec, fail = smtp
    outs = []
    for stage, exc in (("connect", OSError(PW)), ("login", smtplib.SMTPAuthenticationError(535, PW)),
                       ("login", smtplib.SMTPException(PW)), ("send", RuntimeError(PW)),
                       ("quit", RuntimeError(PW))):
        fail.clear()
        fail[stage] = exc
        outs.append(SendEmailTool().execute(**_send()))
    fail.clear()
    outs.append(SendEmailTool().execute(**_send()))
    printed = capsys.readouterr()
    for text in outs + [printed.out, printed.err]:
        assert PW not in text


# ── 9. 设置路由 ────────────────────────────────────────────────────────────────

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


def _put(http, body, headers=AUTH):
    return http.put(URL, json=body, headers=headers)


def test_routes_get_put_password_lifecycle(client, env, capsys):
    http = client
    r = http.get(URL, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"host": "smtp.fastmail.com", "port": 465, "username": "", "from_addr": "",
                        "contacts": [], "has_password": False, "configured": False}
    r = _put(http, {**BASE_SETTINGS, "password": PW})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["has_password"] is True and view["configured"] is True
    assert view["contacts"] == CONTACTS
    path = env["data"] / mail_settings.FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert PW not in path.read_text()
    secrets_file = env["data"] / "api_tools" / ".secrets.env"
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    # 不带密码保存 = 保留；空串同
    r = _put(http, {**BASE_SETTINGS, "username": "me2@example.com"})
    assert r.status_code == 200 and r.json()["has_password"] is True
    r = _put(http, {**BASE_SETTINGS, "password": ""})
    assert r.status_code == 200 and r.json()["has_password"] is True
    assert mail_settings.password() == PW
    # 换密码
    assert _put(http, {**BASE_SETTINGS, "password": PW2}).status_code == 200
    assert mail_settings.password() == PW2
    # 清密码
    r = _put(http, {**BASE_SETTINGS, "clear_password": True})
    assert r.status_code == 200 and r.json()["has_password"] is False
    assert r.json()["configured"] is False and mail_settings.password() == ""
    texts = [http.get(URL, headers=AUTH).text, r.text]
    out = capsys.readouterr()
    for t in texts + [out.out, out.err]:
        assert PW not in t and PW2 not in t


_INVALID = [
    {"host": "https://smtp.fastmail.com"},
    {"host": "smtp.fastmail.com/path"},
    {"host": "smtp fastmail.com"},
    {"host": ""},
    {"port": 0}, {"port": 65536}, {"port": "465"}, {"port": True},
    {"username": "me"}, {"username": "a@b@example.com"}, {"username": "me @example.com"},
    {"from_addr": "Me <me@example.com>"},
    {"contacts": [{"name": "", "address": "a@example.com"}]},
    {"contacts": [{"name": "x" * 41, "address": "a@example.com"}]},
    {"contacts": [{"name": "a\nb", "address": "a@example.com"}]},
    {"contacts": [{"name": "a<b>", "address": "a@example.com"}]},
    {"contacts": [{"name": "a\"b", "address": "a@example.com"}]},
    {"contacts": [{"name": "ok", "address": "a@example.com, b@example.com"}]},
    {"contacts": [{"name": "ok", "address": "a;b@example.com"}]},
    {"contacts": [{"name": "ok", "address": "a@example"}]},
    {"contacts": [{"name": "田中", "address": "a@example.com"},
                  {"name": "田中さん", "address": "b@example.com"}]},
    {"contacts": [{"name": "Ｔａｒｏ", "address": "a@example.com"},
                  {"name": "taro", "address": "b@example.com"}]},
    {"contacts": [{"name": "A", "address": "same@example.com"},
                  {"name": "B", "address": "SAME@example.com"}]},
    {"contacts": [{"name": f"n{i}", "address": f"n{i}@example.com"} for i in range(21)]},
    {"contacts": [{"name": "さん", "address": "a@example.com"}]},
    {"contacts": [{"name": "田中, 鈴木", "address": "a@example.com"}]},
    {"contacts": [{"name": "田中;鈴木", "address": "a@example.com"}]},
    {"unknown": 1},
]


@pytest.mark.parametrize("patch", _INVALID)
def test_put_rejects_invalid_input_and_writes_nothing(client, env, patch):
    http = client
    assert _put(http, {**BASE_SETTINGS, "password": PW}).status_code == 200
    path = env["data"] / mail_settings.FILENAME
    before = path.read_bytes()
    secrets_before = (env["data"] / "api_tools" / ".secrets.env").read_bytes()
    r = _put(http, {**BASE_SETTINGS, **patch, "password": PW2})
    assert r.status_code == 422, (patch, r.status_code, r.text)
    assert path.read_bytes() == before
    assert (env["data"] / "api_tools" / ".secrets.env").read_bytes() == secrets_before
    assert PW2 not in r.text


@pytest.mark.parametrize("password", ["bad\npass", "bad\u2028pass", "x" * 513])
def test_put_rejects_bad_password_without_echo(client, env, password):
    r = _put(client, {**BASE_SETTINGS, "password": password})
    assert r.status_code == 422
    assert password not in r.text
    assert not (env["data"] / mail_settings.FILENAME).exists()
    assert mail_settings.password() == ""


def test_put_rejects_password_with_clear(client):
    r = _put(client, {**BASE_SETTINGS, "password": PW, "clear_password": True})
    assert r.status_code == 422 and PW not in r.text


def test_put_requires_auth_and_accepts_paired_devices(client):
    import app as entry
    http = client
    assert http.get(URL).status_code == 401
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert _put(http, {**BASE_SETTINGS, "password": PW}, headers=headers).status_code == 401
    assert http.get(URL, headers=AUTH).json()["has_password"] is False
    store = entry.app.state.device_store
    token, record, _ = store.issue(name="iPhone", via="qr")
    device = {"Authorization": f"Bearer {token}"}
    assert _put(http, {**BASE_SETTINGS, "password": PW}, headers=device).status_code == 200
    store.revoke(record["id"])
    assert _put(http, dict(BASE_SETTINGS), headers=device).status_code == 401


def test_routes_mount_same_auth_as_search_providers_and_work_before_compute(client, monkeypatch):
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
    assert _put(client, dict(BASE_SETTINGS)).status_code == 200


@pytest.mark.parametrize("change", [{"password": PW2}, {"clear_password": True}])
def test_secret_write_failure_rolls_back_the_json(client, env, monkeypatch, change):
    assert _put(client, {**BASE_SETTINGS, "password": PW}).status_code == 200
    path = env["data"] / mail_settings.FILENAME
    before = path.read_bytes()
    from framework.mcp.tools import api_tool_factory

    def broken(name, value):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(api_tool_factory, "write_secret", broken)
    r = _put(client, {**BASE_SETTINGS, "username": "other@example.com", **change})
    assert r.status_code == 503
    assert path.read_bytes() == before
    assert mail_settings.load()["username"] == "me@example.com"
    assert mail_settings.password() == PW


def test_secret_write_failure_on_first_save_leaves_no_file(env, monkeypatch):
    from framework.mcp.tools import api_tool_factory

    def broken(name, value):
        raise OSError(13, "denied")

    monkeypatch.setattr(api_tool_factory, "write_secret", broken)
    with pytest.raises(OSError):
        mail_settings.update(dict(BASE_SETTINGS), new_password=PW)
    assert not (env["data"] / mail_settings.FILENAME).exists()


def test_mail_and_search_providers_share_one_secrets_lock():
    from framework.agents import search_providers
    from framework.utils.secrets_lock import SECRETS_FILE_LOCK
    assert mail_settings._LOCK is SECRETS_FILE_LOCK
    assert search_providers._STORE_LOCK is SECRETS_FILE_LOCK


def test_autouse_isolation_keeps_mail_secret_in_memory(tmp_path, monkeypatch):
    """不用本文件的 env 装置：conftest 的自动隔离必须让密码读写碰不到任何密钥文件。"""
    from framework.mcp.tools import api_tool_factory

    def forbidden(*a, **kw):
        raise AssertionError("real secrets file touched without the explicit fixture")

    monkeypatch.setattr(api_tool_factory, "read_secret", forbidden)
    monkeypatch.setattr(api_tool_factory, "write_secret", forbidden)
    mail_settings.update(dict(BASE_SETTINGS), new_password=PW)
    assert mail_settings.password() == PW and mail_settings.is_configured()


def test_settings_reread_after_file_change(env):
    mail_settings.update(dict(BASE_SETTINGS), new_password=PW)
    assert len(mail_settings.load()["contacts"]) == 3
    path = env["data"] / mail_settings.FILENAME
    raw = json.loads(path.read_text())
    raw["contacts"] = raw["contacts"][:1]
    path.write_text(json.dumps(raw))
    assert [c["name"] for c in mail_settings.load()["contacts"]] == ["田中"]
    path.write_text("{broken")
    assert mail_settings.load()["contacts"] == [] and not mail_settings.is_configured()


def test_password_validation_mirrors_search_providers():
    from framework.agents import search_providers
    assert mail_settings._CTRL_RE.pattern == search_providers._CTRL_RE.pattern
    assert mail_settings.MAX_SECRET_LEN == search_providers.MAX_SECRET_LEN


# ── 10. 审批卡预览 ─────────────────────────────────────────────────────────────

def _orchestrate(make_session, tools, script, policy=approval_policy.DEFAULT):
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, FakeRegistry
    from .test_orchestrator_lifecycles import GEN, begin_turn
    approval_policy.replace(policy)
    registry = FakeRegistry(tools)
    orch = Orchestrator(FakeEngine(script), registry,
                        {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    return asyncio.run(orch.run_turn(session, journal=session.journal, **GEN)), registry


def _fake_send_email():
    """真 send_email 的预检 / 预览 / 只认卡属性挂在 FakeTool 上（执行记在 registry.executed）。"""
    from .fake_engine import FakeTool
    real = SendEmailTool()
    tool = FakeTool("send_email", approval=True, read_only=False, concurrency_safe=False,
                    validate=real.validate_arguments, parameters=real.parameters)
    tool.approval_pinned = True
    tool.approval_card_only = True
    tool.approval_preview = real.approval_preview
    return tool


def test_pending_payload_carries_resolved_preview_rows(configured, make_session):
    from .fake_engine import FakeTool, call
    other = FakeTool("write_file", approval=True, read_only=False, concurrency_safe=False)
    body = "本文1行目\n2行目"
    result, registry = _orchestrate(
        make_session, [_fake_send_email(), other],
        [{"tool_calls": [call("send_email", to="田中さん", subject="件名", body=body),
                         call("write_file", path="/tmp/x", content="y")]},
         {"text": "done"}],
        policy=ApprovalPolicy(write="auto"))       # 钉死：auto 也出卡
    assert result["status"] == "pending_approval"
    entries = {t["name"]: t for t in result["pending_tools"]}
    assert list(entries) == ["send_email"]       # write_file 按 auto 免审批，不上卡
    mail = entries["send_email"]
    assert mail["arguments"]["to"] == "田中さん"
    assert mail["preview"] == [
        {"key": "to", "value": "田中 <tanaka@example.com>"},
        {"key": "subject", "value": "件名"},
        {"key": "body", "value": body},
    ]
    assert mail["card_only"] is True
    assert registry.executed == []
    json.dumps(result["pending_tools"], ensure_ascii=False)


def test_tool_without_hook_has_no_preview_key(make_session):
    from .fake_engine import FakeTool, call
    tool = FakeTool("danger_op", approval=True, read_only=False, concurrency_safe=False)
    result, _ = _orchestrate(make_session, [tool],
                             [{"tool_calls": [call("danger_op", target="A")]}, {"text": "done"}])
    assert result["status"] == "pending_approval"
    assert result["pending_tools"] == [{"name": "danger_op", "arguments": {"target": "A"}}]


def test_raising_hook_gives_no_preview_and_still_asks(make_session):
    from framework.core import events
    from .fake_engine import FakeTool, call

    def boom(arguments):
        raise RuntimeError("preview broke")

    tool = FakeTool("danger_op", approval=True, read_only=False, concurrency_safe=False)
    tool.approval_preview = boom
    result, registry = _orchestrate(make_session, [tool],
                                    [{"tool_calls": [call("danger_op", target="A")]},
                                     {"text": "done"}])
    assert result["status"] == "pending_approval"
    assert result["pending_tools"] == [{"name": "danger_op", "arguments": {"target": "A"}}]
    assert registry.executed == []
    lines = [json.loads(l) for l in events.get_path().read_text().splitlines()]
    hits = [l for l in lines if l["type"] == "approval_preview_failed"]
    assert len(hits) == 1 and hits[0]["detail"] == {"tool": "danger_op", "error": "RuntimeError"}


def test_preview_with_bare_name_and_unresolvable_fallback(configured):
    tool = SendEmailTool()
    rows = tool.approval_preview(_send(to="Taro"))
    assert [r["key"] for r in rows] == ["to", "subject", "body"]
    assert rows[0]["value"] == "Taro <taro.y@example.org>"
    # 解析不了 / 检查不过 → None（只认卡的工具据此 fail-closed，绝不出「未解析」的卡）
    assert tool.approval_preview(_send(to="nobody")) is None
    assert tool.approval_preview(_send(body="")) is None


# ── 推送文案（iOS 审批通知） ────────────────────────────────────────────────────

def test_approval_push_text_includes_preview_recipient_and_subject():
    from framework.push.dispatcher import NotificationDispatcher, _approval_line
    plain = {"name": "write_file", "arguments": {"path": "/tmp/x"}}
    mail = {"name": "send_email", "arguments": {},
            "preview": [{"key": "to", "value": "田中 <tanaka@example.com>"},
                        {"key": "subject", "value": "件名" + "長" * 100},
                        {"key": "body", "value": "本文は含めない"}]}
    assert _approval_line(plain) == "写文件"                      # 短标签，不是注册名
    assert _approval_line({"name": "danger_op"}) == "danger_op"   # 未收录的用原名
    line = _approval_line(mail)
    assert line.startswith("发邮件（田中 <tanaka@example.com> / 件名")
    assert "本文" not in line and len(line) <= len("发邮件（）") + 80
    d = object.__new__(NotificationDispatcher)
    ctx = {"tools": ["send_email", "write_file"], "tool_lines": [line, "写文件"]}
    _, body = d._content("approval", "full", "", ctx)
    assert body == "等待授权：" + line + "、写文件"
    _, body = d._content("approval", "category", "", ctx)
    assert body == "有敏感操作等待你的授权"
    _, body = d._content("approval", "full", "", {"tools": ["write_file"]})
    assert body == "等待授权：write_file"


# ── 只认卡（B1）：批准须带 card_ack；推送不带可操作按钮；预览出不来就不出卡 ─────────

def _mail_runner(registry_tools, script):
    from .fake_engine import FakeEngine, FakeRegistry
    from .test_turn_runner import make_runner
    registry = FakeRegistry(registry_tools)
    return make_runner(FakeEngine(script), registry), registry


def _history_text(session):
    return json.dumps(session.history, ensure_ascii=False)


def test_approve_without_card_ack_is_409_and_changes_nothing(configured, make_session):
    from framework.core.turn_runner import CARD_CONFIRMATION_REQUIRED, TurnConflict
    from .fake_engine import call
    from .test_turn_runner import submit
    runner, registry = _mail_runner([_fake_send_email()],
                                    [{"tool_calls": [call("send_email", **_send())]},
                                     {"text": "done"}])
    session = make_session()

    async def scenario():
        await submit(runner, session, "s")
        before = session.journal.turn_snapshot()
        with pytest.raises(TurnConflict, match=CARD_CONFIRMATION_REQUIRED):
            await runner.accept_approval(session, "s", True)
        assert session.journal.turn_snapshot() == before
        assert session.journal.turn_state == "awaiting_approval"
        assert registry.executed == []
        await runner.accept_approval(session, "s", True, card_ack=True)
        await runner.run_approval(session, "s", True)

    asyncio.run(scenario())
    assert [e["name"] for e in registry.executed] == ["send_email"]


def test_deny_works_without_card_ack(configured, make_session):
    from .fake_engine import call
    from .test_turn_runner import submit
    runner, registry = _mail_runner([_fake_send_email()],
                                    [{"tool_calls": [call("send_email", **_send())]},
                                     {"text": "done"}])
    session = make_session()

    async def scenario():
        await submit(runner, session, "s")
        await runner.accept_approval(session, "s", False)
        await runner.run_approval(session, "s", False)

    asyncio.run(scenario())
    assert registry.executed == []


def test_card_ack_not_needed_for_batches_without_card_only_calls(make_session):
    from .fake_engine import FakeTool, call
    from .test_turn_runner import submit
    runner, registry = _mail_runner(
        [FakeTool("danger_op", approval=True, read_only=False, concurrency_safe=False)],
        [{"tool_calls": [call("danger_op", target="A")]}, {"text": "done"}])
    session = make_session()

    async def scenario():
        await submit(runner, session, "s")
        await runner.accept_approval(session, "s", True)
        await runner.run_approval(session, "s", True)

    asyncio.run(scenario())
    assert registry.executed == [{"name": "danger_op", "args": {"target": "A"}}]


def test_approval_route_answers_409_with_stable_body(configured, make_session):
    from types import SimpleNamespace
    from framework.api import routes
    from framework.api.schemas import ApprovalRequest
    from .fake_engine import call
    from .test_turn_runner import submit
    runner, registry = _mail_runner([_fake_send_email()],
                                    [{"tool_calls": [call("send_email", **_send())]},
                                     {"text": "done"}])
    session = make_session()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        turn_runner=runner,
        session_manager=SimpleNamespace(get_or_create=lambda sid: session))))

    async def scenario():
        await submit(runner, session, "s")
        return await routes.submit_approval("s", request, ApprovalRequest(approve=True))

    resp = asyncio.run(scenario())
    assert resp.status_code == 409
    assert json.loads(resp.body) == {
        "status": False, "reason": "card_confirmation_required",
        "message": ("This approval includes an action that can only be approved from the "
                    "approval card in the app. Open the app and approve it there.")}
    assert session.journal.turn_state == "awaiting_approval" and registry.executed == []
    assert ApprovalRequest(approve=True).card_ack is False
    with pytest.raises(Exception):
        ApprovalRequest(approve=True, card_ack="yes")


def test_card_only_call_with_raising_preview_never_reaches_a_card(make_session):
    from .fake_engine import FakeTool, call

    def boom(arguments):
        raise RuntimeError("preview broke")

    tool = FakeTool("send_email", approval=True, read_only=False, concurrency_safe=False)
    tool.approval_pinned = True
    tool.approval_card_only = True
    tool.approval_preview = boom
    result, registry = _orchestrate(make_session, [tool],
                                    [{"tool_calls": [call("send_email", **_send())]},
                                     {"text": "done"}, {"text": "done"}, {"text": "done"}])
    assert result["status"] != "pending_approval"
    assert registry.executed == []


def test_card_only_refusal_reaches_the_model_as_system_error(make_session):
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, FakeRegistry, FakeTool, call
    from .test_orchestrator_lifecycles import GEN, begin_turn
    tool = FakeTool("send_email", approval=True, read_only=False, concurrency_safe=False)
    tool.approval_card_only = True
    tool.approval_preview = lambda arguments: None
    registry = FakeRegistry([tool])
    orch = Orchestrator(FakeEngine([{"tool_calls": [call("send_email", **_send())]},
                                    {"text": "done"}, {"text": "done"}, {"text": "done"}]),
                        registry, {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    result = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] != "pending_approval" and registry.executed == []
    assert "approval card could not be prepared" in _history_text(session)


def test_card_only_push_is_not_actionable_and_names_recipient(tmp_path):
    from .test_push import feed_item, make_dispatcher, route_turn
    disp, _, apns = make_dispatcher(tmp_path, [
        {"device_id": "d1", "token": "tok1", "prefs": {"content_level": "full"}}])
    asyncio.run(route_turn(
        disp,
        feed_item("turn_queued", prompt="メールして", origin="chat"),
        feed_item("awaiting_approval", pending_tools=[{
            "name": "send_email", "arguments": {}, "card_only": True,
            "preview": [{"key": "to", "value": "田中 <tanaka@example.com>"},
                        {"key": "subject", "value": "件名"},
                        {"key": "body", "value": "本文"}]}])))
    (sent,) = apns.sent
    aps = sent["payload"]["aps"]
    assert "category" not in aps                       # 没有「允许 / 拒绝」按钮
    assert aps["interruption-level"] == "time-sensitive"
    body = aps["alert"]["body"]
    assert "发邮件（田中 <tanaka@example.com> / 件名）" in body and "本文" not in body
    assert "send_email" not in body and "审批卡" in body


def test_mixed_batch_with_a_card_only_call_is_not_actionable(tmp_path):
    from .test_push import feed_item, make_dispatcher, route_turn
    disp, _, apns = make_dispatcher(tmp_path, [
        {"device_id": "d1", "token": "tok1", "prefs": {"content_level": "category"}}])
    asyncio.run(route_turn(
        disp,
        feed_item("turn_queued", prompt="x", origin="chat"),
        feed_item("awaiting_approval", pending_tools=[
            {"name": "write_file", "arguments": {}},
            {"name": "send_email", "arguments": {}, "card_only": True}])))
    (sent,) = apns.sent
    assert "category" not in sent["payload"]["aps"]


# ── 卡外调用不得搭车（B2）、卡上显示 ≠ 执行（B3） ───────────────────────────────

def _suspend(make_session, tools, calls):
    from framework.core.orchestrator import Orchestrator
    from .fake_engine import FakeEngine, FakeRegistry
    from .test_orchestrator_lifecycles import GEN, begin_turn
    approval_policy.replace(approval_policy.DEFAULT)
    registry = FakeRegistry(tools)
    orch = Orchestrator(FakeEngine([{"tool_calls": calls}] + [{"text": "done"}] * 4),
                        registry, {"react_max_steps": 4, "max_total_iterations": 12})
    session = make_session()
    session.add_user_message("go")
    begin_turn(session)
    result = asyncio.run(orch.run_turn(session, journal=session.journal, **GEN))
    assert result["status"] == "pending_approval"
    return orch, registry, session, result


def _approve(orch, session):
    from .test_orchestrator_lifecycles import GEN
    session.journal.emit("resumed", {"trigger": "approve"})
    return asyncio.run(orch.resume_after_approval(session, approved=True,
                                                  journal=session.journal, **GEN))


def test_late_registered_call_not_on_the_card_is_not_executed(make_session):
    """评审探针（late registration）：卡上只有 write_file，send_email 在批准前被重扫注册。"""
    from .fake_engine import FakeTool, call
    write = FakeTool("write_file", approval=True, read_only=False, concurrency_safe=False)
    orch, registry, session, result = _suspend(
        make_session, [write],
        [call("send_email", to="x", subject="s", body="b"),
         call("write_file", path="/tmp/x", content="y")])
    assert [t["name"] for t in result["pending_tools"]] == ["write_file"]
    mail = FakeTool("send_email", approval=True, read_only=False, concurrency_safe=False)
    mail.approval_pinned = True
    registry._tools["send_email"] = mail          # 周期重扫在点击之前注册了它
    _approve(orch, session)
    assert [e["name"] for e in registry.executed] == ["write_file"]
    assert "was not shown on the approval card" in _history_text(session)


def test_off_card_call_that_still_needs_no_approval_runs(make_session):
    from .fake_engine import FakeTool, call
    write = FakeTool("write_file", approval=True, read_only=False, concurrency_safe=False)
    read = FakeTool("list_dir", approval=False, read_only=False, concurrency_safe=False)
    orch, registry, session, _ = _suspend(
        make_session, [write, read],
        [call("list_dir", path="/tmp"), call("write_file", path="/tmp/x", content="y")])
    _approve(orch, session)
    assert [e["name"] for e in registry.executed] == ["list_dir", "write_file"]


def test_changed_contact_between_card_and_approve_is_not_sent(configured, make_session):
    from .fake_engine import call
    orch, registry, session, result = _suspend(make_session, [_fake_send_email()],
                                               [call("send_email", **_send())])
    assert result["pending_tools"][0]["preview"][0]["value"] == "田中 <tanaka@example.com>"
    contacts = [dict(c) for c in CONTACTS]
    contacts[0]["address"] = "someone.else@example.com"
    mail_settings.update({**BASE_SETTINGS, "contacts": contacts})
    _approve(orch, session)
    assert registry.executed == []
    assert "details shown to the user for approval have changed" in _history_text(session)


def test_unchanged_card_is_sent(configured, make_session):
    from .fake_engine import call
    orch, registry, session, _ = _suspend(make_session, [_fake_send_email()],
                                          [call("send_email", **_send())])
    _approve(orch, session)
    assert [e["name"] for e in registry.executed] == ["send_email"]


def test_preview_raising_at_approve_time_is_not_executed(configured, make_session):
    from .fake_engine import call
    tool = _fake_send_email()
    orch, registry, session, _ = _suspend(make_session, [tool], [call("send_email", **_send())])

    def boom(arguments):
        raise RuntimeError("gone")

    tool.approval_preview = boom
    _approve(orch, session)
    assert registry.executed == []


# ── 名单类护栏 ─────────────────────────────────────────────────────────────────

def test_send_email_and_publish_page_are_delivery_tools():
    from framework.core.retrieval_evidence import DELIVERY_TOOLS
    assert {"send_email", "publish_page"} <= DELIVERY_TOOLS


def test_only_send_email_is_card_only():
    from framework.mcp.tools.publish_page import PublishPageTool
    assert SendEmailTool.approval_card_only is True
    assert PublishPageTool.approval_card_only is False


# ── 第二轮评审补的护栏 oracle ──────────────────────────────────────────────────

def test_accept_approval_falls_back_to_the_registry_for_card_only(make_session):
    """卡外、批准前才注册的 send_email：挂起时的卡片记录没标它只认卡，注册表兜底仍须 409。"""
    from framework.core.turn_runner import CARD_CONFIRMATION_REQUIRED, TurnConflict
    from .fake_engine import FakeTool, call
    from .test_turn_runner import submit
    write = FakeTool("write_file", approval=True, read_only=False, concurrency_safe=False)
    runner, registry = _mail_runner(
        [write], [{"tool_calls": [call("send_email", to="x", subject="s", body="b"),
                                  call("write_file", path="/tmp/x", content="y")]}]
        + [{"text": "done"}] * 4)
    session = make_session()

    async def scenario():
        await submit(runner, session, "s")
        assert not any(m.get("card_only") for m in session.pending_card_meta)
        mail = FakeTool("send_email", approval=True, read_only=False, concurrency_safe=False)
        mail.approval_pinned = True
        mail.approval_card_only = True
        registry._tools["send_email"] = mail
        with pytest.raises(TurnConflict, match=CARD_CONFIRMATION_REQUIRED):
            await runner.accept_approval(session, "s", True)
        assert session.journal.turn_state == "awaiting_approval"

    asyncio.run(scenario())
    assert registry.executed == []


@pytest.mark.parametrize("meta", [None, "short", "long"])
def test_recheck_without_matching_card_metadata_holds_calls_that_need_approval(make_session,
                                                                                meta):
    from .fake_engine import FakeTool, call
    danger = FakeTool("danger_op", approval=True, read_only=False, concurrency_safe=False)
    free = FakeTool("list_dir", approval=False, read_only=False, concurrency_safe=False)
    orch, registry, session, _ = _suspend(make_session, [danger, free],
                                          [call("list_dir", path="/tmp"),
                                           call("danger_op", target="A")])
    good = session.pending_card_meta
    session.pending_card_meta = {None: None, "short": good[:1],
                                 "long": good + [{"on_card": True, "preview": None}]}[meta]
    _approve(orch, session)
    # 记录对不上 = 全部按卡外处理：需审批的不执行，不需审批的照常
    assert [e["name"] for e in registry.executed] == ["list_dir"]
    assert "was not shown on the approval card" in _history_text(session)


def test_pending_card_meta_cleared_with_pending_calls(make_session):
    """拒批续跑清空挂起批次时，卡片记录同步清掉（两者绝不分叉）。"""
    from .fake_engine import FakeTool, call
    from .test_turn_runner import submit
    runner, _ = _mail_runner(
        [FakeTool("danger_op", approval=True, read_only=False, concurrency_safe=False)],
        [{"tool_calls": [call("danger_op", target="A")]}] + [{"text": "done"}] * 3)
    session = make_session()

    async def scenario():
        await submit(runner, session, "s")
        assert session.pending_card_meta is not None
        await runner.accept_approval(session, "s", False)
        await runner.run_approval(session, "s", False)

    asyncio.run(scenario())
    assert session.pending_tool_calls is None and session.pending_card_meta is None
