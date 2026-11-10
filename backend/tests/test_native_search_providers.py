"""build 85 批 C：设置页「搜索」——真 Models.swift + 真 SearchProviderText.swift + 真 L10n.swift 编译成独立二进制，
跑提供方列表 JSON（后端真实形状）的解码、状态 / 测试结果文案三语映射、请求体编码。
L10n 三语齐全与重键检查在 test_progress_codes.py。"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"),
                                reason="requires macOS Swift")


def test_search_providers_decoding_and_status_text(tmp_path):
    src = ROOT / "apple-app/Sources"
    binary = tmp_path / "search_providers"
    r = subprocess.run(
        ["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
         *map(str, sorted((src / "API").glob("*.swift"))),
         *map(str, sorted((src / "Store").glob("Offline*.swift"))),
         str(src / "Store/TurnReducer.swift"), str(src / "Store/ChatMessage.swift"),
         str(src / "App/L10n.swift"), str(src / "Views/SearchProviderText.swift"),
         str(ROOT / "apple-app/Tests/search_providers_test.swift"),
         "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    out = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "OK search_providers" in out.stdout, out.stdout + out.stderr
