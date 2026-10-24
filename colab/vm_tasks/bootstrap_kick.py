"""Start the uploaded VM helper detached; its progress file is the only channel back."""
import json  # noqa: F401
import os
from pathlib import Path
import subprocess


def main(params):
    private = Path(params['private'])
    os.chmod(private / 'credentials.json', 0o600)
    with (private / 'bootstrap.log').open('w') as log:
        process = subprocess.Popen(['python3', str(private / params['helper'])],
                                   stdout=log, stderr=log, start_new_session=True)
    (private / 'bootstrap.pid').write_text(str(process.pid))
    print('COMPUTE_BOOTSTRAP_STARTED')
