"""Return a redacted diagnostic or log tail. Redaction runs on the VM, before transfer."""
import base64
import importlib.util
import json  # noqa: F401
from pathlib import Path


def load_helper(private, helper, app):
    spec = importlib.util.spec_from_file_location('compute_helper', Path(private) / helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, 'use_layout'):  # legacy demo_vm.py carries its paths as constants
        module.use_layout({'private': str(private), 'app': app})
    if (Path(module.APP) / '.env.local').exists():
        module.service_environment()  # registers the service secrets for redaction
    return module


def tail(path, limit):
    with path.open('rb') as log:
        log.seek(0, 2)
        size = log.tell()
        log.seek(max(0, size - limit))
        raw = log.read(limit)
    return raw.partition(b'\n')[2] if size > limit else raw


def main(params):
    private = Path(params['private'])
    helper = load_helper(private, params['helper'], params['app'])
    if params['mode'] == 'diagnostic':
        target = private / 'failure-diagnostic.log'
        if not target.exists():
            helper.save_failure_diagnostic()
        text = target.read_text()
    else:
        text = helper.redact(tail(private / params['log'], params['max_bytes']).decode(errors='replace'))
    print('COMPUTE_TEXT ' + base64.b64encode(text.encode()).decode())
