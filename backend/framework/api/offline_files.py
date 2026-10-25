"""Read-only enumeration of a session's portable files for offline clients.

The ordinary files panel remains an outputs-only view. This manifest also includes
uploaded inputs; it never walks arbitrary paths mentioned in model/tool output.
"""
from pathlib import Path
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter()
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KINDS = ("assets", "image", "audio", "attach")


def _session_root(request: Request, session_id: str) -> Path:
    if not _SESSION.fullmatch(session_id) or ".." in session_id:
        raise HTTPException(400, "Invalid session ID")
    root = request.app.state.session_manager.sessions_dir.resolve()
    session = root / session_id
    if session.is_symlink() or session.resolve().parent != root:
        raise HTTPException(404, "Session not found")
    return session


def _safe_file(session: Path, kind: str, filename: str) -> Path:
    if kind not in _KINDS or not filename or filename.startswith(".") \
            or any(c in filename for c in ("/", "\\")) \
            or any(ord(c) < 32 for c in filename):
        raise HTTPException(400, "Invalid file name")
    directory = session / kind
    path = directory / filename
    if directory.is_symlink() or path.is_symlink() \
            or path.resolve().parent != directory.absolute() or not path.is_file():
        raise HTTPException(404, "File not found")
    return path


@router.get("/api/v1/chat/{session_id}/offline-files")
async def offline_files(session_id: str, request: Request):
    session = _session_root(request, session_id)
    files = []
    for kind in _KINDS:
        directory = session / kind
        if directory.is_symlink() or not directory.is_dir():
            continue
        for candidate in sorted(directory.iterdir()):
            try:
                path = _safe_file(session, kind, candidate.name)
                stat = path.stat()
            except (HTTPException, FileNotFoundError):
                continue
            files.append({
                "path": f"/api/v1/files/{session_id}/offline/{kind}/{quote(path.name, safe='')}",
                "name": path.name, "kind": kind, "size": stat.st_size,
                "revision": f"{stat.st_mtime_ns}:{stat.st_size}",
            })
    return {"status": True, "session_id": session_id, "files": files}


@router.get("/api/v1/files/{session_id}/offline/{kind}/{filename}")
async def offline_file(session_id: str, kind: str, filename: str, request: Request):
    path = _safe_file(_session_root(request, session_id), kind, filename)
    return FileResponse(path, headers={
        # Files may change under the same name. The client uses manifest revisions;
        # do not place a year-long immutable response in the generic URL cache.
        "Cache-Control": "private, no-cache",
        # An uploaded .html/.svg is user content served from the backend's own
        # origin: force a download and forbid sniffing so it can never execute as
        # same-origin script against the API. filename* (RFC 5987) keeps non-ASCII
        # upload names intact; no ASCII fallback is emitted because a transliterated
        # one would disagree with the manifest entry the client already holds.
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name, safe='')}",
    })
