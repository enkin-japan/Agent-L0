"""Fence the VM against late starts, then log its ephemeral tailnet node out."""
import json  # noqa: F401
from pathlib import Path
import subprocess


def main(params):
    private = Path(params['private'])
    (private / 'stop.requested').touch(mode=0o600)
    result = subprocess.run([params['tailscale'], '--socket=' + str(private / 'ts.sock'), 'logout'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode == 0:
        print('COMPUTE_LOGOUT_OK')
