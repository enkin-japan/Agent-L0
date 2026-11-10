"""build 85 A3：app 不显示写给模型的护栏消息——真 Models / ChatMessage / L10n 编译成独立二进制，
跑气泡映射（ChatMessage.from(turn:)）与会话内搜索候选（ModelFacingMessage.searchableTexts）。
后端侧护栏消息前缀与 app 常量逐字一致在本文件第二条钉住。"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from framework.core import orchestrator

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"),
                    reason="requires macOS Swift")
def test_guardrail_system_messages_are_hidden_in_app(tmp_path):
    src = ROOT / "apple-app/Sources"
    binary = tmp_path / "guardrail_message"
    r = subprocess.run(
        ["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
         *map(str, sorted((src / "API").glob("*.swift"))),
         *map(str, sorted((src / "Store").glob("Offline*.swift"))),
         str(src / "Store/TurnReducer.swift"), str(src / "Store/ChatMessage.swift"),
         str(src / "App/L10n.swift"), str(ROOT / "apple-app/Tests/guardrail_message_test.swift"),
         "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    out = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "OK guardrail_message" in out.stdout, out.stdout + out.stderr


def test_backend_guardrail_prefix_matches_app_constant():
    """app 按前缀判「写给模型」：后端两条进 history 的 system 护栏与强制作答尾注都以同一前缀开头。"""
    swift = (ROOT / "apple-app/Sources/Store/ChatMessage.swift").read_text(encoding="utf-8")
    assert 'static let guardrailPrefix = "[System guardrail]"' in swift
    src = Path(orchestrator.__file__).read_text(encoding="utf-8")
    assert src.count('"role": "system"') == 2
    for chunk in src.split('"role": "system"')[1:]:
        assert chunk.lstrip(",\n ").split('"content": ', 1)[1].lstrip("(\n f").startswith(
            '"[System guardrail]'), chunk[:200]
    assert orchestrator.FORCED_ANSWER_NOTE.startswith("[System guardrail]")
