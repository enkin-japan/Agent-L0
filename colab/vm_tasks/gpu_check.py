"""Create the private directory and refuse anything but one idle A100 40GB."""
import json  # noqa: F401  (remote_task appends a json.loads call)
from pathlib import Path
import subprocess


def query(*fields):
    result = subprocess.run(['nvidia-smi', '--query-' + fields[0], '--format=csv,noheader,nounits'],
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


def main(params):
    Path(params['private']).mkdir(mode=0o700, exist_ok=False)
    rows = query('gpu=name,memory.total').splitlines()
    if len(rows) != 1:
        raise SystemExit('exactly one GPU required')
    name, memory = rows[0].rsplit(',', 1)
    low, high = params['memory_mib']
    if params['gpu_name'] not in name or not low <= int(memory.strip()) <= high:
        raise SystemExit('wrong GPU type or size; refusing an oversized allocation')
    if query('compute-apps=pid'):
        raise SystemExit('GPU already occupied; refusing to load another model')
    print('COMPUTE_GPU_VERIFIED', name.strip(), memory.strip())
