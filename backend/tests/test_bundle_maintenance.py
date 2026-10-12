"""Exercise the production bundle copy rule using synthetic data only."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'apple-app/bundle-macos.sh'


def exercise_copy(script: str, tmp_path: Path):
    assert shutil.which('rsync'), 'rsync is required to validate the bundle copy rule'
    source = tmp_path / 'source'
    resources = tmp_path / 'resources'
    backend = source / 'backend'
    (backend / 'framework/maintenance').mkdir(parents=True)
    (backend / 'maintenance').mkdir()
    (backend / 'app.py').write_text('# synthetic app\n')
    (backend / 'framework/maintenance/drift_patrol.py').write_text('# synthetic code\n')
    (backend / 'maintenance/drift_patrol.json').write_text('{"synthetic": "runtime data"}')
    destination = resources / 'backend'
    (destination / 'maintenance').mkdir(parents=True)
    (destination / 'maintenance/drift_patrol.json').write_text('{"synthetic": "previous bundle"}')
    start = script.index('rsync -a --delete --delete-excluded')
    end = script.index('"$ROOT/backend/" "$RES/backend/"', start)
    command = script[start:end] + '"$ROOT/backend/" "$RES/backend/"'
    result = subprocess.run(['bash', '-c', command],
                            env={**os.environ, 'ROOT': str(source), 'RES': str(resources)},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert (destination / 'app.py').read_text() == '# synthetic app\n'
    assert (destination / 'framework/maintenance/drift_patrol.py').is_file(), 'maintenance code must remain'
    assert not (destination / 'maintenance').exists(), 'runtime maintenance state must not be bundled'


def test_bundle_excludes_runtime_maintenance_but_keeps_code(tmp_path):
    exercise_copy(SCRIPT.read_text(), tmp_path)


def test_bundle_maintenance_exclusion_mutation(tmp_path):
    source = SCRIPT.read_text()
    target = " --exclude='/maintenance/'"
    assert source.count(target) == 1
    mutant = source.replace(target, '')
    (tmp_path / 'bundle-mutant.sh').write_text(mutant)
    with pytest.raises(AssertionError, match='runtime maintenance state must not be bundled') as failure:
        exercise_copy(mutant, tmp_path / 'probe')
    (tmp_path / 'mutation-output.txt').write_text(str(failure.value))
    print('Expected mutation failure:', failure.value)
