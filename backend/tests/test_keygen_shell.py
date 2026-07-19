"""devctl.sh keygen 子命令金测试（shell 侧，实跑一次）。

守护栏配套逃生口：`./devctl.sh keygen` 把四把 Bearer 钥匙的空值/弱值换成强随机串。
⚠️ 只用 DEVCTL_ENV_FILE 指向临时文件，绝不碰仓库根 .env.local（用户真实密钥）。
python3/perl 缺席时跳过（CI 最小镜像兜底）。
"""
import os
import shutil
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEVCTL = os.path.join(_REPO, "devctl.sh")


def _read_key(text: str, key: str) -> str:
    val = ""
    for line in text.splitlines():
        if line.startswith(key + "="):
            val = line[len(key) + 1:].strip().strip('"').strip("'")
    return val


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("perl"),
                    reason="需要 bash + perl")
def test_devctl_syntax_ok():
    r = subprocess.run(["bash", "-n", _DEVCTL], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("perl"),
                    reason="需要 bash + perl")
def test_keygen_upsert_perms_and_preserve(tmp_path):
    envfile = tmp_path / "sandbox.env"
    envfile.write_text(
        'AGENT_API_KEY=\n'
        'INFER_API_KEY="test"\n'
        'RAG_API_KEY="strong-existing-key-abcdef123456"\n'
        'RAG_COMPUTE_API_KEY=\n'
        '# preserve me\n'
        'OTHER=keep\n',
        encoding="utf-8",
    )
    env = dict(os.environ, DEVCTL_ENV_FILE=str(envfile))
    r = subprocess.run(["bash", _DEVCTL, "keygen"], cwd=_REPO,
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr + r.stdout

    out = envfile.read_text(encoding="utf-8")
    # 弱值/空值被换成强随机（48 hex = token_hex(24)）
    for key in ("AGENT_API_KEY", "INFER_API_KEY", "RAG_COMPUTE_API_KEY"):
        val = _read_key(out, key)
        assert len(val) >= 16, f"{key} 未达强度: {val!r}"
        assert val not in ("", "test")
    # 已强的值原样保留
    assert _read_key(out, "RAG_API_KEY") == "strong-existing-key-abcdef123456"
    # 其余行原样保留
    assert "# preserve me" in out
    assert "OTHER=keep" in out
    # 权限 600
    assert oct(envfile.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("perl"),
                    reason="需要 bash + perl")
def test_keygen_idempotent(tmp_path):
    envfile = tmp_path / "idem.env"
    envfile.write_text("AGENT_API_KEY=\n", encoding="utf-8")
    env = dict(os.environ, DEVCTL_ENV_FILE=str(envfile))
    subprocess.run(["bash", _DEVCTL, "keygen"], cwd=_REPO, capture_output=True, text=True, env=env)
    first = envfile.read_text(encoding="utf-8")
    subprocess.run(["bash", _DEVCTL, "keygen"], cwd=_REPO, capture_output=True, text=True, env=env)
    second = envfile.read_text(encoding="utf-8")
    assert first == second, "二次 keygen 不应改动已强的值"
