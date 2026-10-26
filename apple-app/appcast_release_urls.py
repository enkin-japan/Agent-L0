#!/usr/bin/env python3
"""Point every appcast enclosure at its own per-build GitHub Release.

generate_appcast writes one --download-url-prefix for all items; since 2026-10-26
each build lives in its own Release (tag macos-<ver>-<build>) and the rolling
`updates` Release holds only appcast.xml. This rewrites the URLs after
generate_appcast, byte-for-byte except the URL attributes, so the EdDSA
signatures (over the file contents, not the URL) stay valid.

Usage: appcast_release_urls.py <appcast.xml> <owner/repo>
Fails (non-zero, file untouched) on any URL it cannot map: a url attribute that is
not <prefix>/<tag>/<file>, a file name that is not this build's DMG or a delta to
this build, a build number that disagrees with <sparkle:version>, or a release
notes link that still points at the rolling release.
"""
import os
import re
import stat
import sys
import tempfile

ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
DMG_RE = re.compile(r"MacAgent-(?P<ver>\d+(?:\.\d+)*)-(?P<build>\d+)\.dmg")
# Every url attribute, whichever quote generate_appcast used; anything that is not
# a GitHub release download URL fails the run instead of slipping through.
URL_ATTR_RE = re.compile(r"""url=(?P<q>["'])(?P<url>.*?)(?P=q)""")
DOWNLOAD_RE = re.compile(r"^(?P<prefix>https://github\.com/[^/]+/[^/]+/releases/download/)(?P<tag>[^/]+)/(?P<file>[^/]+)$")
DELTA_RE = re.compile(r"MacAgent(?P<to>\d+)-(?P<frm>\d+)\.delta")
VERSION_RE = re.compile(r"<sparkle:version>\s*(\d+)\s*</sparkle:version>")
NOTES_RE = re.compile(r"<sparkle:releaseNotesLink>\s*(?P<url>[^<\s]+)\s*</sparkle:releaseNotesLink>")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def fail(msg: str) -> None:
    raise SystemExit(f"appcast_release_urls: {msg}")


def rewrite_item(item: str, repo: str, feed_tag: str) -> str:
    dmg = DMG_RE.search(item)
    if dmg is None:
        fail(f"item without a MacAgent-<ver>-<build>.dmg enclosure:\n{item[:200]}")
    ver, build = dmg.group("ver"), dmg.group("build")
    versions = VERSION_RE.findall(item)
    if versions != [build]:
        fail(f"item build {build} (from the dmg name) disagrees with <sparkle:version> {versions}")
    tag = f"macos-{ver}-{build}"
    prefix = f"https://github.com/{repo}/releases/download/"

    def repl(m: re.Match) -> str:
        url = m.group("url")
        d = DOWNLOAD_RE.match(url)
        if d is None:
            fail(f"item {build}: url is not a GitHub release download URL: {url}")
        name = d.group("file")
        if DMG_RE.fullmatch(name):
            if name != f"MacAgent-{ver}-{build}.dmg":
                fail(f"item {build} references another dmg: {name}")
        elif (dl := DELTA_RE.fullmatch(name)) is not None:
            if dl.group("to") != build:
                fail(f"item {build} references a delta to build {dl.group('to')}: {name}")
        else:
            fail(f"item {build}: unrecognised enclosure file name: {name}")
        q = m.group("q")
        return f"url={q}{prefix}{tag}/{name}{q}"

    out = URL_ATTR_RE.sub(repl, item)
    for n in NOTES_RE.finditer(out):
        if f"/releases/download/{feed_tag}/" in n.group("url"):
            fail(f"item {build}: release notes link still points at the rolling release: {n.group('url')}")
    return out


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    path, repo = sys.argv[1], sys.argv[2]
    if not REPO_RE.match(repo) or ".." in repo:
        fail(f"bad repo spec: {repo}")
    feed_tag = "updates"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not URL_ATTR_RE.search(text):
        fail("no url attributes found; is this an appcast?")
    out = ITEM_RE.sub(lambda m: rewrite_item(m.group(0), repo, feed_tag), text)
    mapped = [DOWNLOAD_RE.match(m.group("url")) for m in URL_ATTR_RE.finditer(out)]
    if any(d is None or d.group("tag") == feed_tag for d in mapped):
        fail("some enclosure still points at the rolling release or outside GitHub")
    # Atomic replace: a half-written appcast must never be what gets uploaded.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".appcast-", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(out)
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    for d in mapped:
        print(f"   {d.group('tag')}/{d.group('file')}")


if __name__ == "__main__":
    main()
