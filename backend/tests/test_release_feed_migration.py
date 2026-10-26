"""Run the real publication block (⑥) of release-macos.sh with a synthetic gh.

No credentials, no network: the fake gh shadows the real one on PATH, GitHub
tokens are stripped from the environment, and the test asserts the fake is the
one bash resolves. The fake logs every call, answers the asset listing from
what was uploaded to it (name, size, sha256 digest), and can be told to fail
one step. Guards under test (2026-10-26 per-build layout): the Release is
created as a draft before anything is uploaded, the payload (DMG + this
build's deltas only) is uploaded and verified before the draft is published,
and only then is the appcast pushed to the canonical feed and the legacy
bridge, in that order; the feed-only `updates` release is created without
taking Latest away from the build.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'apple-app/release-macos.sh'
VER, BUILD = '0.16.0', '99'
TAG = f'macos-{VER}-{BUILD}'
FAKE_GH = '''import hashlib, json, os, sys
args = sys.argv[1:]
log = os.environ['TASK_GH_LOG']
with open(log, 'a') as f:
    f.write(json.dumps(args) + '\\n')
fail = os.environ['TASK_GH_FAILURE']
repo = args[args.index('-R') + 1] if '-R' in args else ''
if args[:2] == ['release', 'upload']:
    feed = any(a.endswith('appcast.xml') for a in args)
    if fail == 'assets' and not feed: sys.exit(17)
    if fail == 'primary_feed' and feed and repo == 'enkin-japan/Agent-L0': sys.exit(19)
    if fail == 'legacy_feed' and feed and repo == 'enkin-japan/Agent-L0-macOS-app': sys.exit(23)
elif args[:2] == ['release', 'view']:
    if '--json' not in args:
        # Existence probe of the feed-only release: it does not exist yet in this
        # synthetic repo, so the script must create it.
        sys.exit(1)
    jq = args[args.index('--jq') + 1]
    if '.digest' in jq:
        # Asset listing of the per-build release: whatever was uploaded to that tag in that repo.
        tag = args[2]
        for line in open(log):
            call = json.loads(line)
            if call[:3] == ['release', 'upload', tag] and call[call.index('-R') + 1] == repo:
                for path in call[3:]:
                    if os.path.isfile(path):
                        size = os.path.getsize(path) + (1 if fail == 'verify' else 0)
                        digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
                        print(f'{os.path.basename(path)} {size} sha256:{digest}')
    else:
        print('appcast.xml')
'''


def publish(source, tmp_path, failure=''):
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / 'releases'
    archive.mkdir()
    for name in (f'MacAgent-{VER}-{BUILD}.dmg', f'MacAgent{BUILD}-98.delta', 'MacAgent98-97.delta', 'appcast.xml'):
        (archive / name).write_text(f'synthetic public artifact {name}')
    log = tmp_path / 'calls.jsonl'
    log.touch()
    fake = tmp_path / 'gh'
    fake.write_text(f'#!{sys.executable}\n' + FAKE_GH)
    fake.chmod(0o755)
    scratch = tmp_path / 'tmp'
    scratch.mkdir()
    path = str(tmp_path) + os.pathsep + os.environ['PATH']
    assert shutil.which('gh', path=path) == str(fake), 'the synthetic gh must shadow the real one'
    env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'BASH_ENV')}
    env.update({'PATH': path, 'GH_CONFIG_DIR': str(scratch), 'TMPDIR': str(scratch),
                'TASK_GH_LOG': str(log), 'TASK_GH_FAILURE': failure,
                'ARCHIVE_DIR': str(archive), 'DMG': str(archive / f'MacAgent-{VER}-{BUILD}.dmg'),
                'VER': VER, 'BUILD': BUILD, 'RELEASE_TAG': TAG})
    env['RELEASE_COMMIT'] = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                                           text=True, check=True).stdout.strip()
    variables = '\n'.join(re.findall(r'^(?:RELEASE_REPO|LEGACY_RELEASE_REPO|FEED_TAG)=.*$', source, re.M))
    start = source.index('echo "== ⑥')
    end = source.index('echo "== ⑦', start)
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + variables + '\n' + source[start:end]],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def uploads_of(calls):
    return [c for c in calls if c[:2] == ['release', 'upload']]


def assert_delivery(source, tmp_path):
    result, calls = publish(source, tmp_path)
    assert result.returncode == 0, result.stderr
    uploads = uploads_of(calls)
    assert len(uploads) == 3, 'both feeds must follow the payload upload'
    payload = uploads[0]
    assert payload[2] == TAG and not any(a.endswith('appcast.xml') for a in payload), 'payload must precede the feed'
    assert any(a.endswith(f'MacAgent-{VER}-{BUILD}.dmg') for a in payload)
    assert any(a.endswith(f'MacAgent{BUILD}-98.delta') for a in payload)
    assert not any(a.endswith('MacAgent98-97.delta') for a in payload), "only this build's deltas go to its release"
    repos = [u[u.index('-R') + 1] for u in uploads]
    assert repos == ['enkin-japan/Agent-L0', 'enkin-japan/Agent-L0', 'enkin-japan/Agent-L0-macOS-app'], 'canonical publish and legacy bridge must both be delivered'
    assert uploads[1][2] == uploads[2][2] == 'updates'
    assert uploads[1][-1] == uploads[2][-1] and uploads[1][-1] == str(tmp_path / 'releases' / 'appcast.xml')
    # Draft first, before any upload; published (and marked latest) only after the
    # payload is uploaded and verified, and before any feed.
    creates = [c for c in calls if c[:2] == ['release', 'create']]
    build_create = next(c for c in creates if c[2] == TAG)
    assert '--draft' in build_create and '--latest' not in build_create
    assert calls.index(build_create) < calls.index(payload)
    verify_idx = next(i for i, c in enumerate(calls) if c[:2] == ['release', 'view'] and c[2] == TAG and '--json' in c)
    publish_idx = next(i for i, c in enumerate(calls) if c[:2] == ['release', 'edit'] and c[2] == TAG)
    assert '--draft=false' in calls[publish_idx] and '--latest' in calls[publish_idx]
    assert calls.index(payload) < verify_idx < publish_idx < calls.index(uploads[1])
    # The feed-only release is created on first use without taking Latest from the build.
    feed_create = next(c for c in creates if c[2] == 'updates')
    assert '--latest=false' in feed_create and publish_idx < calls.index(feed_create) < calls.index(uploads[1])
    settings = yaml.safe_load((ROOT / 'apple-app/project.yml').read_text())
    feed = settings['targets']['MacAgent-macOS']['info']['properties']['SUFeedURL']
    assert feed == f'https://github.com/{repos[0]}/releases/download/updates/appcast.xml'


def assert_failure_stops(source, tmp_path, failure, call_count):
    result, calls = publish(source, tmp_path, failure)
    assert result.returncode != 0, 'a failed step must fail the run'
    assert len(uploads_of(calls)) == call_count
    published = any(c[:2] == ['release', 'edit'] and '--draft=false' in c for c in calls)
    if failure in ('assets', 'verify'):
        assert not published, 'a release whose assets failed or mismatched must stay a draft'
        if failure == 'verify':
            assert '远端资产与本地不一致' in result.stderr
    else:
        assert published
    if failure == 'legacy_feed':
        assert '兼容 feed 同步失败' in result.stderr


def test_publisher_uploads_payload_before_both_feeds(tmp_path):
    assert_delivery(SCRIPT.read_text(), tmp_path)


@pytest.mark.parametrize('failure,call_count', [('assets', 1), ('verify', 1), ('primary_feed', 2), ('legacy_feed', 3)])
def test_publication_failure_stops_later_steps(tmp_path, failure, call_count):
    assert_failure_stops(SCRIPT.read_text(), tmp_path, failure, call_count)


@pytest.mark.parametrize('fault', ['payload', 'verify', 'legacy'])
def test_publication_guard_mutation(tmp_path, fault):
    """Each mutant removes one guard and must make the test that protects it fail."""
    source = SCRIPT.read_text()
    if fault == 'payload':
        target = 'gh release upload "$RELEASE_TAG" -R "$RELEASE_REPO" --clobber "${UPLOADS[@]}"'
        assert source.count(target) == 1
        mutant = source.replace(target, ': # synthetic removal', 1)
        expect = 'returncode'   # the verification guard stops the run; assert_delivery's first assert fires
    elif fault == 'verify':
        start = source.index('diff "$ASSET_CHECK/local" "$ASSET_CHECK/remote"')
        end = source.index('\n', source.index('exit 1; }', start)) + 1
        mutant = source[:start] + ': # synthetic removal\n' + source[end:]
        expect = 'a failed step must fail the run'
    else:
        start = source.index('gh release upload "$FEED_TAG" -R "$LEGACY_RELEASE_REPO"')
        end = source.index('\n}', start) + 2
        mutant = source[:start] + ': # synthetic removal' + source[end:]
        expect = 'both feeds must follow the payload upload'
    with pytest.raises(AssertionError) as failure:
        if fault == 'verify':
            assert_failure_stops(mutant, tmp_path / 'probe', 'verify', 1)
        else:
            assert_delivery(mutant, tmp_path / 'probe')
    assert expect in str(failure.value) or (expect == 'returncode' and '远端资产与本地不一致' in str(failure.value))
    (tmp_path / 'mutation-output.txt').write_text(str(failure.value))
    print('Expected publication mutation failure:', failure.value)
