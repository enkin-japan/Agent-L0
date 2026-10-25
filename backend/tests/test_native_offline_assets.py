"""Real URLSession downloads followed by a new offline client process."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("xcrun"), reason="Requires Apple Swift SDK")
def test_native_asset_disk_lifecycle_and_backup_policy(tmp_path):
    binary = tmp_path / "asset-lifecycle"
    sources = [ROOT / "apple-app/Sources/Store/OfflineAssetStore.swift",
               ROOT / "apple-app/Sources/Store/OfflineCacheRoot.swift",
               ROOT / "apple-app/Tests/offline_assets_test.swift"]
    compiled = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path",
        str(tmp_path / "modules"), "-o", str(binary), *map(str, sources)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("xcrun"), reason="Requires Apple Swift SDK")
def test_native_asset_sync_then_offline_restart(tmp_path):
    api = ROOT / "apple-app/Sources/API"
    sources = sorted(api.glob("*.swift"))
    sources += list((ROOT / "apple-app/Sources/Store").glob("Offline*.swift"))
    sources += [ROOT / "apple-app/Tests/offline_assets_transport_test.swift"]
    binary = tmp_path / "test"
    compiled = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path",
        str(tmp_path / "modules"), "-o", str(binary), *map(str, sources)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    files = {"assets": ("report.txt", b"A" * 262144), "attach": ("source.txt", b"original upload"),
             "image": ("photo.png", b"synthetic-image"), "audio": ("sound.wav", b"synthetic-audio")}
    legacy_calls = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            nonlocal legacy_calls
            if self.headers.get("Authorization") != "Bearer fixture-only":
                self.send_response(401); self.end_headers(); return
            if self.path == "/api/v1/files/LEGACY":
                legacy_calls += 1
                body = json.dumps({"status": True, "files": [] if legacy_calls == 1 else [
                    {"name": "new.txt", "size": 3, "mtime": 1}]}).encode()
            elif self.path == "/api/v1/files/A":
                body = json.dumps({"status": True, "files": [
                    {"name": "report.txt", "size": len(files["assets"][1]), "mtime": 1}]}).encode()
            elif self.path == "/api/v1/files/A/assets/report.txt":
                # On-demand download of an output that automatic mirroring skipped.
                body = files["assets"][1]
            elif self.path.endswith("/offline-files"):
                body = json.dumps({"status": True, "session_id": "A", "files": [
                    {"path": f"/api/v1/files/A/offline/{kind}/{name}", "kind": kind,
                     "name": name, "size": len(data), "revision": "1:" + str(len(data))}
                    for kind, (name, data) in files.items()]}).encode()
            elif "/offline/" in self.path:
                body = files[self.path.split("/")[-2]][1]
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        online = subprocess.run([str(binary), "sync", str(tmp_path / "cache"), base],
                                capture_output=True, text=True, timeout=45)
        assert online.returncode == 0, online.stdout + online.stderr
        legacy = subprocess.run([str(binary), "legacy", str(tmp_path / "legacy-cache"), base],
                                capture_output=True, text=True, timeout=15)
        assert legacy.returncode == 0, legacy.stdout + legacy.stderr
        budget = subprocess.run([str(binary), "budget", str(tmp_path / "budget-cache"), base],
                                capture_output=True, text=True, timeout=45)
        assert budget.returncode == 0, budget.stdout + budget.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    offline = subprocess.run([str(binary), "offline", str(tmp_path / "cache"), base],
                             capture_output=True, text=True, timeout=10)
    assert offline.returncode == 0, offline.stdout + offline.stderr
