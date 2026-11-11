"""send_email：给设置页「邮件发送」里登记的联系人发一封纯文本邮件（只发不收、不带附件）。

用户要求：发信前必须出审批卡（收件人 + 内容摘要），确认后才发。故
requires_approval + approval_pinned 按类钉死——写档 "auto" 不放行，定时任务等无人值守轮
一律自动拒绝（orchestrator / schedule.runner 既有规则）。审批卡显示的是 approval_preview
解析后的收件人「名字 <地址>」，不是模型给的原串。approval_card_only：只能在 app 内的审批卡
上批准（推送通知不带「允许」按钮，批准请求须带 card_ack；预览出不来则不出卡、打回模型），
批准后执行前编排层重算预览，与卡上显示的不同就不发。

页面类内容不当附件发：先用 publish_page 发布成链接，正文里带链接（用户 11-11 的设计）。

收件人白名单：`to` 只能解析到设置里登记的唯一一个联系人（名字或地址）；未登记的地址
即使语法合法也一律拒发——模型（以及被注入的网页 / 文件内容）无法把邮件发给任意地址。
解析、实参校验在 validate_arguments（审批前，失败不出卡）、approval_preview 与 execute
三处走**同一个**函数；execute 绝不假设预检跑过，自己从头再查一遍。

传输：SMTP over implicit TLS（smtplib.SMTP_SSL + ssl.create_default_context()，证书校验
恒开）；无 STARTTLS / 明文回落、无重试（重试可能发两遍）。数据可能已交给服务器之后的失败
报「投递不确定，不要自动重发」。密码只交给 login，不进任何返回文本与日志。

时间预算：套接字超时 10 s；开始 send_message 前若已过 30 s 就放弃（确定未发）。之后最多
5 次往返（MAIL / RCPT / DATA / 正文 / QUIT）× 10 s，总计远低于工具级熔断 120 s——熔断触发时
工作线程不可能还在发信。
"""
import smtplib
import ssl
import time
import unicodedata
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Dict, List, Optional, Tuple

from ..base import BaseTool
from ...mail import settings as mail_settings

MAX_SUBJECT = 200
MAX_BODY = 4000
SMTP_TIMEOUT = 10
SEND_DEADLINE = 30.0          # 开始 send_message 前允许用掉的秒数
_TOOL = "send_email"
_clock = time.monotonic       # 测试注入


def _refuse(reason: str) -> str:
    return f"System Error: {_TOOL} was not executed (nothing was sent): {reason}"


def resolve_recipient(to: Any, contacts: List[Dict[str, str]]) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """`to` → 唯一联系人。返回 (联系人, None) 或 (None, 拒绝理由)。理由只列联系人**名字**。

    归一：strip → NFKC → casefold；名字比较另去掉至多一个结尾敬称（さん / 様 / 先生 …）。
    地址按 casefold 比。命中 0 个或多于 1 个 → 拒；含 , ; 或换行（多收件人形态）→ 拒。"""
    names = ", ".join(c["name"] for c in contacts) or "(none)"
    if not isinstance(to, str) or not to.strip():
        return None, f"'to' must be the name or address of one registered contact. Registered contacts: {names}."
    if any(ch in to for ch in ",;\r\n\u2028\u2029\x85"):
        return None, ("exactly one recipient is allowed ('to' must not contain ',' ';' or line "
                      f"breaks; no cc/bcc). Registered contacts: {names}.")
    raw = unicodedata.normalize("NFKC", to.strip()).casefold().strip()
    key = mail_settings.normalize_name(to)
    hits = [c for c in contacts
            if raw == c["address"].casefold()
            or (key and key == mail_settings.normalize_name(c["name"]))]
    if len(hits) == 1:
        return hits[0], None
    shown = to.strip()[:80]
    if not hits:
        return None, (f"{shown!r} is not a registered mail contact; only contacts registered in "
                      f"the app's mail settings can receive mail. Registered contacts: {names}. "
                      "Do not guess an address: use one of these names, or ask the user to add "
                      "the contact in the app's mail settings.")
    return None, (f"{shown!r} matches more than one registered contact; use the exact contact "
                  f"name. Registered contacts: {names}.")


def check_call(arguments: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """全部实参与配置检查（预检 / 预览 / 执行共用）。→ (发信计划, None) 或 (None, 拒绝理由)。"""
    args = arguments or {}
    settings = mail_settings.load()
    if not mail_settings.is_configured(settings):
        return None, ("mail sending is not set up (the user must enter the server, username, "
                      "app password and at least one contact in the app's mail settings)")
    contact, err = resolve_recipient(args.get("to"), settings["contacts"])
    if err:
        return None, err
    subject, body = args.get("subject"), args.get("body")
    if not isinstance(subject, str) or not subject.strip():
        return None, "subject must not be empty"
    if len(subject) > MAX_SUBJECT or any(ch in subject for ch in "\r\n\u2028\u2029\x85"):
        return None, f"subject must be one line of at most {MAX_SUBJECT} characters"
    if mail_settings._CTRL_RE.search(subject):
        return None, "subject must not contain control characters"
    if not isinstance(body, str) or not body.strip():
        return None, "body must not be empty"
    if len(body) > MAX_BODY:
        return None, f"body must be at most {MAX_BODY} characters"
    return {"settings": settings, "contact": contact, "subject": subject.strip(),
            "body": body}, None


def _recipient(contact: Dict[str, str]) -> str:
    return f"{contact['name']} <{contact['address']}>"


def _code(e: BaseException) -> Optional[int]:
    """SMTP 应答码（只取码，不取服务器原文——原文可能回显地址 / 用户名）。"""
    code = getattr(e, "smtp_code", None)
    if isinstance(code, int):
        return code
    recipients = getattr(e, "recipients", None)
    if isinstance(recipients, dict):
        for v in recipients.values():
            if isinstance(v, tuple) and v and isinstance(v[0], int):
                return v[0]
    return None


def _describe(e: BaseException) -> str:
    code = _code(e)
    return type(e).__name__ + (f", SMTP reply {code}" if code is not None else "")


class SendEmailTool(BaseTool):
    name = "send_email"
    summary = "给设置里登记的联系人发邮件（メール送信）"
    description = (
        "Send a plain-text email (メール / 邮件) to one contact registered in the app's mail "
        "settings; any other address is refused. No attachments: to share a page, publish it "
        "with publish_page and put the link in the body. The user approves every send."
    )
    parameters = {
        "properties": {
            "to": {"type": "string", "description": "Registered contact name or address."},
            "subject": {"type": "string", "description": "One line."},
            "body": {"type": "string", "description": "Plain text."},
        },
        "required": ["to", "subject", "body"],
    }
    requires_approval = True     # 用户要求：每封都先出卡确认
    # 钉死：审批卡就是「发给谁、发什么」的确认闸，写档 "auto" 不得放行
    approval_pinned = True
    # 只认卡：推送按钮 / 不带 card_ack 的批准不算数；预览出不来就不出卡
    approval_card_only = True
    is_concurrency_safe = False
    is_read_only = False
    timeout_seconds = 120

    def is_available(self) -> bool:
        try:
            return mail_settings.is_configured()
        except Exception:
            return False

    def validate_arguments(self, arguments: Dict[str, Any]) -> "str | None":
        _, err = check_call(arguments)
        return _refuse(err) if err else None

    def approval_preview(self, arguments: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
        """审批卡行（展示序）：收件人（解析后的「名字 <地址>」）、主题、正文全文。

        检查不过 → None（只认卡的工具据此 fail-closed：不出卡、不执行），绝不显示没解析的
        收件人冒充已解析。批准后编排层会重算本函数并与卡上显示的逐行比对。"""
        plan, _ = check_call(arguments or {})
        if plan is None:
            return None
        return [{"key": "to", "value": _recipient(plan["contact"])},
                {"key": "subject", "value": plan["subject"]},
                {"key": "body", "value": plan["body"]}]

    @staticmethod
    def _message(plan: Dict[str, Any]) -> EmailMessage:
        settings, contact = plan["settings"], plan["contact"]
        sender = mail_settings.sender(settings)
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = formataddr((contact["name"], contact["address"]))
        msg["Subject"] = plan["subject"]
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
        msg.set_content(plan["body"], charset="utf-8")
        return msg

    def execute(self, to: Any = None, subject: Any = None, body: Any = None,
                **extra: Any) -> str:
        started = _clock()
        if extra:
            return _refuse(f"unknown argument(s): {', '.join(sorted(map(str, extra)))}")
        plan, err = check_call({"to": to, "subject": subject, "body": body})
        if err:
            return _refuse(err)
        try:
            msg = self._message(plan)
        except Exception as e:
            return _refuse(f"the message could not be built ({type(e).__name__})")
        return self._send(plan, msg, started)

    def _send(self, plan: Dict[str, Any], msg: EmailMessage, started: float) -> str:
        settings, contact = plan["settings"], plan["contact"]
        sender = mail_settings.sender(settings)
        auth_hint = ("System Error: email NOT sent — the mail server rejected the login. Ask the "
                     "user to check the username and app password in the app's mail settings.")
        smtp = None
        try:
            smtp = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=SMTP_TIMEOUT,
                                    context=ssl.create_default_context())
            smtp.login(settings["username"], mail_settings.password())
        except smtplib.SMTPAuthenticationError:
            self._close(smtp)
            print("[SendEmail] login rejected")
            return auth_hint
        except (smtplib.SMTPException, OSError) as e:   # 含 SSLError / 超时
            self._close(smtp)
            print(f"[SendEmail] connect/login failed: {_describe(e)}")
            return f"System Error: email NOT sent — could not connect or log in to the mail server ({_describe(e)})."
        except Exception as e:
            self._close(smtp)
            print(f"[SendEmail] connect/login failed: {type(e).__name__}")
            return f"System Error: email NOT sent ({type(e).__name__})."
        if _clock() - started > SEND_DEADLINE:
            self._close(smtp)
            print("[SendEmail] too slow before sending; aborted")
            return ("System Error: email NOT sent — connecting to the mail server took too long "
                    f"(more than {int(SEND_DEADLINE)} s), so the send was abandoned before any "
                    "message data was transmitted. It is safe to try again later.")
        try:
            smtp.send_message(msg, from_addr=sender, to_addrs=[contact["address"]])
        except smtplib.SMTPAuthenticationError:
            self._close(smtp)
            print("[SendEmail] send rejected: authentication")
            return auth_hint
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError) as e:
            # 服务器明确拒收（发件人 / 收件人 / DATA 应答为错误码）= 确定未投递
            self._close(smtp)
            print(f"[SendEmail] send refused: {_describe(e)}")
            return (f"System Error: email NOT sent — the mail server refused the message "
                    f"({_describe(e)}).")
        except Exception as e:
            self._close(smtp)
            print(f"[SendEmail] send failed, delivery uncertain: {_describe(e)}")
            return ("System Error: delivery UNCERTAIN — the connection failed while the message "
                    f"was being handed to the mail server ({_describe(e)}). It may or may not "
                    "have been sent. Do NOT call send_email again for this message automatically; "
                    "tell the user and let them check their Sent folder first.")
        try:
            smtp.quit()
        except Exception:
            self._close(smtp)      # 已交付后的 QUIT 失败不影响结果
        print("[SendEmail] sent")
        return f"Email sent to {_recipient(contact)}, subject {plan['subject']!r}. Do not send it again."

    @staticmethod
    def _close(smtp) -> None:
        if smtp is None:
            return
        try:
            smtp.close()
        except Exception:
            pass
