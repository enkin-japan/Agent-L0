"""build 84：app 侧进度代号 / 工具名按界面语言显示（StepText）——真 L10n.swift + 真归约器 / ChatMessage
编译成独立二进制，跑 code → 本地化文本、回退矩阵（旧后端 / 未知代号 / 缺参 / 类型不对）与工具名表。
跨语言对齐（后端代号表 == StepText.progressTemplates、L10n 三语齐全）在 test_progress_codes.py。"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"),
                                reason="requires macOS Swift")


def test_step_text_localization_and_fallbacks(tmp_path):
    src = ROOT / "apple-app/Sources"
    binary = tmp_path / "step_text"
    r = subprocess.run(
        ["xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
         *map(str, sorted((src / "API").glob("*.swift"))),
         *map(str, sorted((src / "Store").glob("Offline*.swift"))),
         str(src / "Store/TurnReducer.swift"), str(src / "Store/ChatMessage.swift"),
         str(src / "App/L10n.swift"), str(ROOT / "apple-app/Tests/step_text_test.swift"),
         "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    out = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "OK step_text" in out.stdout, out.stdout + out.stderr
