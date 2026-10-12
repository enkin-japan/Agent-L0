"""Native credential mapping, using synthetic values and no Keychain access."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_native_credential_wiring():
    sources = ROOT / "apple-app/Sources"
    settings = (sources / "App/AppSettings.swift").read_text()
    onboarding = (sources / "Views/OnboardingView.swift").read_text()
    sidecar = (sources / "App/SidecarManager.swift").read_text()
    assert 'Keychain.save("rag-compute-api-key", ragComputeApiKey)' in settings
    assert 'saved: Keychain.load("rag-compute-api-key"), legacyData: storedRagDataKey' in settings
    assert 'settings.ragComputeApiKey = SidecarManager.cleanKey(ragKey)' in onboarding
    assert 'SecureField("RAG_COMPUTE_API_KEY"' in onboarding
    assert 'ragData: settings?.ragApiKey ?? "", ragCompute: settings?.ragComputeApiKey ?? ""' in sidecar
    assert 'env = credentials.applying(to: env)' in sidecar


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires Swift toolchain")
def test_native_credential_mapping(tmp_path):
    binary = tmp_path / "credentials"
    # Compile the real settings persistence code against in-memory platform stores.
    # Neither UserDefaults.standard nor the live Keychain is touched by this test.
    settings = (ROOT / "apple-app/Sources/App/AppSettings.swift").read_text()
    start = settings.index("enum Keychain {")
    end = settings.index("/// 连接设置：", start)
    settings = settings[:start] + settings[end:]
    settings = settings.replace("UserDefaults.standard", "FixtureDefaults.standard")
    settings_file = tmp_path / "AppSettings.swift"
    settings_file.write_text(settings)
    compilation = subprocess.run([
        "xcrun", "swiftc", "-parse-as-library", "-module-cache-path", str(tmp_path / "modules"),
        str(ROOT / "apple-app/Sources/App/ServiceCredentials.swift"),
        str(settings_file),
        str(ROOT / "apple-app/Tests/service_credentials_test.swift"), "-o", str(binary),
    ], capture_output=True, text=True, timeout=90)
    assert compilation.returncode == 0, compilation.stderr
    subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)
