"""Create a fresh, private staging directory for one switch job."""
import json  # noqa: F401
from pathlib import Path


def main(params):
    Path(params['stage']).mkdir(mode=0o700, parents=True, exist_ok=False)
    print('COMPUTE_SWITCH_STAGE_READY')
