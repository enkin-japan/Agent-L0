"""publish_page：把 ~/AgentPages 里的一个 HTML 文件发布成公开链接（设置页「页面发布」）。

用户的设计：邮件里带链接而不是附件。本工具把源文件复制进用户配置的、由 web 服务器对外
提供的文件夹（root_dir）下一个**新建**的长随机目录（secrets.token_urlsafe(24)，32 字符），
返回 `<base_url>/<随机目录>/<文件名>`。链接即凭据：拿到链接的人都能打开页面。

审批：requires_approval=True 但**不钉死**——跟随用户的写档策略（write=auto 时免卡）。
审批卡预览给「文件名（大小）」与发布到的站点主机名。

文件系统边界（本工具在 root 里只做「新建」）：
- 源文件：realpath 后必须在 realpath(~/AgentPages) 之内（commonpath 判定，指向外面的软链拒），
  常规文件、.html/.htm、0 < 大小 ≤ 2 MB、不像密钥文件；预检 / 预览 / 执行走同一个检查函数，
  execute 绝不假设预检跑过。读一次、读到的字节就是发布的字节。
- root：必须已存在且是目录（不代建）；只 os.mkdir 一个新随机目录（排他，撞名重抽 ≤ 3 次），
  里面 O_EXCL|O_NOFOLLOW 建一个文件；权限显式 chmod 成 0755 / 0644（不受 umask 影响）。
- 失败清理只删本次调用建的东西（先 unlink 文件、再 rmdir 目录），绝不递归删除、绝不碰
  root 里任何别的条目；root 里别的东西也从不读、不列。
"""
import os
import re
import secrets
import stat
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ..base import BaseTool
from ...publish import settings as publish_settings

MAX_PAGE = 2 * 1024 * 1024
PAGE_EXTS = (".html", ".htm")
PAGES_DIR = "~/AgentPages"
TOKEN_TRIES = 3
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.html?$", re.IGNORECASE)
_TOOL = "publish_page"


def _refuse(reason: str) -> str:
    return f"System Error: {_TOOL} was not executed (nothing was published): {reason}"


def pages_root() -> str:
    return os.path.realpath(os.path.expanduser(PAGES_DIR))


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def check_source(path: Any) -> Tuple[Optional[str], Optional[str]]:
    """源路径 → (真实路径, None) 或 (None, 拒绝理由)。"""
    if not isinstance(path, str) or not path.strip():
        return None, f"'path' must be the path of an HTML file under {PAGES_DIR}"
    from .shell_exec import _secret_path_hit
    expanded = os.path.expanduser(path.strip())
    root = pages_root()
    if not os.path.isabs(expanded):
        expanded = os.path.join(root, expanded)      # 裸文件名 = AgentPages 里的文件
    real = os.path.realpath(expanded)
    try:
        inside = os.path.commonpath([root, real]) == root and real != root
    except ValueError:
        inside = False
    if not inside:
        return None, f"only files inside {PAGES_DIR} can be published (symlinks are resolved first)"
    if _secret_path_hit(expanded) or _secret_path_hit(real):
        return None, "this file looks like a credential and cannot be published"
    if not os.path.isfile(real):
        return None, f"file not found or not a regular file: {os.path.basename(real)!r}"
    if not real.lower().endswith(PAGE_EXTS):
        return None, "only .html / .htm files can be published"
    try:
        size = os.path.getsize(real)
    except OSError:
        return None, "the file cannot be read"
    if size <= 0:
        return None, "the file is empty"
    if size > MAX_PAGE:
        return None, "the file is larger than 2 MB"
    return real, None


def check_call(arguments: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """全部检查（预检 / 预览 / 执行共用）→ (计划, None) 或 (None, 拒绝理由)。不碰 root。"""
    settings = publish_settings.load()
    if not publish_settings.is_configured(settings):
        return None, ("page publishing is not set up (the user must enter the publish folder and "
                      "the public URL in the app's settings)")
    real, err = check_source((arguments or {}).get("path"))
    if err:
        return None, err
    return {"settings": settings, "source": real}, None


def read_source(real: str) -> Tuple[Optional[bytes], Optional[str]]:
    """读已解析的真实路径一次：O_NOFOLLOW 打开（检查之后最后一段被换成软链 = 打不开），fstat
    确认是常规文件且 0 < 大小 ≤ 2 MB，再从同一个描述符读至多 2 MB + 1 字节。读到的就是发布的。"""
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        return None, f"the file cannot be opened ({type(e).__name__})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "the file is not a regular file"
        if st.st_size <= 0:
            return None, "the file is empty"
        if st.st_size > MAX_PAGE:
            return None, "the file is larger than 2 MB"
        chunks, total = [], 0
        while total <= MAX_PAGE:
            chunk = os.read(fd, MAX_PAGE + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    except OSError as e:
        return None, f"the file cannot be read ({type(e).__name__})"
    finally:
        os.close(fd)
    if not data:
        return None, "the file is empty"
    if len(data) > MAX_PAGE:
        return None, "the file is larger than 2 MB"
    return data, None


def _published_name(source: str) -> str:
    name = os.path.basename(source)
    return name if _SAFE_NAME_RE.fullmatch(name) else "index.html"


class PublishPageTool(BaseTool):
    name = "publish_page"
    summary = "把网页发布成公开链接（ページを公開）"
    description = (
        "Publish one HTML page (ページを公開 / 发布页面) from ~/AgentPages to the user's web "
        "folder and return its public link (链接). Anyone with the link can open the page."
    )
    parameters = {
        "properties": {
            "path": {"type": "string", "description": "HTML file under ~/AgentPages."},
        },
        "required": ["path"],
    }
    requires_approval = True     # 跟随写档策略（不钉死）：write=auto 时免卡
    is_concurrency_safe = False
    is_read_only = False
    timeout_seconds = 30

    def is_available(self) -> bool:
        try:
            return publish_settings.is_configured()
        except Exception:
            return False

    def validate_arguments(self, arguments: Dict[str, Any]) -> "str | None":
        _, err = check_call(arguments)
        return _refuse(err) if err else None

    def approval_preview(self, arguments: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
        """审批卡行（展示序）：file = 文件名（大小），site = base_url 的主机名。"""
        args = arguments or {}
        plan, err = check_call(args)
        settings = publish_settings.load()
        site = urlsplit(settings.get("base_url") or "").hostname or "(not set up)"
        if plan is None:
            return [{"key": "file", "value": f"{str(args.get('path', ''))} (invalid: {err})"},
                    {"key": "site", "value": site}]
        real = plan["source"]
        return [{"key": "file",
                 "value": f"{os.path.basename(real)} ({_human_size(os.path.getsize(real))})"},
                {"key": "site", "value": site}]

    def execute(self, path: Any = None, **extra: Any) -> str:
        if extra:
            return _refuse(f"unknown argument(s): {', '.join(sorted(map(str, extra)))}")
        plan, err = check_call({"path": path})
        if err:
            return _refuse(err)
        settings, source = plan["settings"], plan["source"]
        root = os.path.realpath(settings["root_dir"])
        if not os.path.isdir(root):
            return _refuse("the publish folder does not exist or is not a folder; ask the user to "
                           "check the publish folder in the app settings")
        data, err = read_source(source)
        if err:
            return _refuse(err)
        name = _published_name(source)
        token_dir = token = None
        try:
            for _ in range(TOKEN_TRIES):
                token = secrets.token_urlsafe(24)
                candidate = os.path.join(root, token)
                try:
                    os.mkdir(candidate, 0o755)
                except FileExistsError:
                    continue
                token_dir = candidate
                break
        except OSError as e:
            return _refuse(f"could not create the page folder ({type(e).__name__})")
        if token_dir is None:
            return _refuse("could not create a new page folder (name collision)")
        dest = os.path.join(token_dir, name)
        created_file = False
        try:
            os.chmod(token_dir, 0o755)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            created_file = True
            try:
                f = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)          # fdopen 自己失败时描述符还没交给文件对象
                raise
            with f:
                f.write(data)
            os.chmod(dest, 0o644)
        except Exception as e:
            # 只删本次调用建的：先文件、再空目录；绝不递归、绝不碰 root 里别的东西
            if created_file:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
            try:
                os.rmdir(token_dir)
            except OSError:
                pass
            print(f"[PublishPage] publish failed: {type(e).__name__}")
            return f"System Error: the page was NOT published ({type(e).__name__})."
        print("[PublishPage] published one page")
        url = f"{settings['base_url']}/{token}/{name}"
        return f"{url}\nPublished. Anyone who has this link can open the page."
