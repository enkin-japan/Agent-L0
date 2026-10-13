"""Run the real publication block with a synthetic gh; no credentials/network."""
from pathlib import Path
import json
import os
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'apple-app/release-macos.sh'


def publish(source, tmp_path, failure=''):
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / 'releases'
    archive.mkdir()
    for name in ('MacAgent-new.dmg', 'MacAgent-new.delta', 'appcast.xml'):
        (archive / name).write_text('synthetic public artifact')
    log = tmp_path / 'calls.jsonl'
    fake = tmp_path / 'gh'
    fake.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
args = sys.argv[1:]
with open(os.environ['TASK_GH_LOG'], 'a') as f:
    f.write(json.dumps(args)+'\\n')
if args[:2] == ['release', 'upload']:
    repo = args[args.index('-R')+1]
    feed = any(a.endswith('appcast.xml') for a in args)
    fail = os.environ['TASK_GH_FAILURE']
    if fail == 'assets' and not feed: sys.exit(17)
    if fail == 'primary_feed' and feed and repo == 'enkin-japan/Agent-L0': sys.exit(19)
    if fail == 'legacy_feed' and feed and repo == 'enkin-japan/Agent-L0-macOS-app': sys.exit(23)
''')
    fake.chmod(0o755)
    variables = '\n'.join(re.findall(r'^(?:RELEASE_REPO|LEGACY_RELEASE_REPO|FEED_TAG)=.*$', source, re.M))
    start = source.index('echo "== ⑥')
    end = source.index('echo "== ⑦', start)
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\n'+variables+'\n'+source[start:end]],
                            env={**os.environ, 'PATH': str(tmp_path)+os.pathsep+os.environ['PATH'],
                                 'TASK_GH_LOG': str(log), 'TASK_GH_FAILURE': failure,
                                 'ARCHIVE_DIR': str(archive), 'DMG': str(archive/'MacAgent-new.dmg')},
                            capture_output=True, text=True, timeout=20)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    uploads = [c for c in calls if c[:2] == ['release', 'upload']]
    return result, uploads


def assert_delivery(source, tmp_path):
    result, uploads = publish(source, tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(uploads) == 3, 'both feeds must follow the payload upload'
    assert not any(a.endswith('appcast.xml') for a in uploads[0]), 'payload must precede the feed'
    assert any(a.endswith('.dmg') for a in uploads[0])
    assert any(a.endswith('.delta') for a in uploads[0])
    repos = [u[u.index('-R')+1] for u in uploads]
    assert repos == ['enkin-japan/Agent-L0', 'enkin-japan/Agent-L0', 'enkin-japan/Agent-L0-macOS-app'], 'canonical publish and legacy bridge must both be delivered'
    assert uploads[1][-1] == uploads[2][-1] and uploads[1][-1].endswith('appcast.xml')
    settings = yaml.safe_load((ROOT/'apple-app/project.yml').read_text())
    feed = settings['targets']['MacAgent-macOS']['info']['properties']['SUFeedURL']
    assert feed == f'https://github.com/{repos[0]}/releases/download/updates/appcast.xml'


def test_publisher_uploads_payload_before_both_feeds(tmp_path):
    assert_delivery(SCRIPT.read_text(), tmp_path)


@pytest.mark.parametrize('failure,call_count', [('assets', 1), ('primary_feed', 2), ('legacy_feed', 3)])
def test_publication_failure_stops_later_steps(tmp_path, failure, call_count):
    result, uploads = publish(SCRIPT.read_text(), tmp_path, failure)
    assert result.returncode != 0
    assert len(uploads) == call_count
    if failure == 'legacy_feed':
        assert '兼容 feed 同步失败' in result.stderr


@pytest.mark.parametrize('fault', ['payload', 'legacy'])
def test_publication_guard_mutation(tmp_path, fault):
    source = SCRIPT.read_text()
    if fault == 'payload':
        target = 'gh release upload "$FEED_TAG" -R "$RELEASE_REPO" --clobber "${UPLOADS[@]}"'
        assert source.count(target) == 1
        mutant = source.replace(target, ': # synthetic removal', 1)
    else:
        start = source.index('gh release upload "$FEED_TAG" -R "$LEGACY_RELEASE_REPO"')
        end = source.index('\n}', start)+2
        mutant = source[:start]+': # synthetic removal'+source[end:]
    (tmp_path/'publisher-mutant.sh').write_text(mutant)
    with pytest.raises(AssertionError, match='both feeds must follow the payload upload') as failure:
        assert_delivery(mutant, tmp_path/'probe')
    (tmp_path/'mutation-output.txt').write_text(str(failure.value))
    print('Expected publication mutation failure:', failure.value)
