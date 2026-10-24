"""Start the staged helper's switch transaction; never overwrite the running helper."""
import json  # noqa: F401
from pathlib import Path
import subprocess


def main(params):
    private = Path(params['private'])
    if (private / 'stop.requested').exists():
        raise SystemExit('VM stopping')
    job = params['job']
    command = [params['python'], str(Path(params['stage']) / params['helper']), 'switch',
               '--options', params['options'], '--job', job]
    with (private / f'switch-{job}.log').open('w') as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
    (private / f'switch-{job}.pid').write_text(str(process.pid))
    print('COMPUTE_SWITCH_STARTED')
