"""Complete native RetierMonitor, fake URLProtocol/settings/approval/notifications/clock."""
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
ROOT=Path(__file__).resolve().parents[2]
pytestmark=pytest.mark.skipif(sys.platform!='darwin' or not shutil.which('xcrun'),reason='requires macOS Swift')
def compile_retier(tmp, mutation=None):
    source=ROOT/'apple-app/Sources/App/RetierMonitor.swift'
    text=source.read_text()
    if mutation:
        replacements={
          'snooze':('guard manual || !snoozed else { return }','guard !snoozed else { return }'),
          'confirmation':('guard accepted else {','guard true else {'),
          'single-flight':('guard !isBusy, !following else { return }','guard !following else { return }'),
          'before-post':('''    private func startRebuild(baselineStartedAt: String? = nil) async {
        guard connectionIsCurrent else { connectionChanged(); return }''','''    private func startRebuild(baselineStartedAt: String? = nil) async {'''),
          'after-status':('connectionIsCurrent, (resp as? HTTPURLResponse)?.statusCode == 200','(resp as? HTTPURLResponse)?.statusCode == 200'),
          'after-follow':('''                guard connectionIsCurrent else { connectionChanged(); return }
                if !occurred {''','''                if !occurred {'''),
          'force':('ForceBody(force: false)','ForceBody(force: true)'),
          'notice-owner':('get { noticeConnection == connection() ? recordedNotice : nil }','get { recordedNotice }'),
          'network-feedback':('manualNotice = L("请求是否受理尚未确认，请刷新状态后再决定是否重试。")','manualNotice = nil'),
          'network-owner':('''            let occurred = await follow(baselineStartedAt: baselineStartedAt)
            guard connectionIsCurrent else { connectionChanged(); return }
            if !occurred {''','''            let occurred = await follow(baselineStartedAt: baselineStartedAt)
            if !occurred {'''),
        }
        old,new=replacements[mutation]
        assert text.count(old)==(2 if mutation=='after-follow' else 1)
        source=tmp/'RetierMonitor.swift';source.write_text(text.replace(old,new))
    binary=tmp/'retier'
    r=subprocess.run(['xcrun','swiftc','-parse-as-library','-module-cache-path',str(tmp/'modules'),str(ROOT/'apple-app/Sources/API/ServiceStatus.swift'),str(source),str(ROOT/'apple-app/Tests/retier_manual_test.swift'),'-o',str(binary)],capture_output=True,text=True,timeout=90)
    assert r.returncode==0,r.stdout+r.stderr
    return binary
@pytest.fixture(scope='module')
def retier(tmp_path_factory):return compile_retier(tmp_path_factory.mktemp('native-retier'))
MODES=['decline','snoozed','healthy','unknown','status-failed','coalesce','changed-fetch','changed-confirm','changed-key','changed-post','changed-follow','changed-follow-conflict','post-old','post-new','conflict-running','already-running','unreachable','stalled','notifications-off','completed-switch','post-network-old','post-network-new','post-network-unreachable','post-network-changed-follow']
@pytest.mark.parametrize('mode',MODES)
def test_manual_retier_pipeline(retier,mode,tmp_path):
    r=subprocess.run([str(retier),mode],capture_output=True,text=True,timeout=15)
    (tmp_path/'case-output.txt').write_text(r.stdout+r.stderr)
    assert r.returncode==0,r.stdout+r.stderr
@pytest.mark.parametrize('mutation,mode,expected',[
 ('snooze','snoozed','manual entry bypasses snooze'),
 ('confirmation','decline','declining confirmation cannot rebuild'),
 ('single-flight','coalesce','in-flight check coalesces'),
 ('before-post','changed-confirm','connection change after confirmation blocks POST'),
 ('after-status','changed-fetch','stale status cannot prompt'),
 ('after-follow','changed-follow','late old operation cannot overwrite'),
 ('force','post-new','manual rebuild never forces healthy data'),
 ('notice-owner','completed-switch','completed old connection notice cannot describe the new backend'),
 ('network-feedback','post-network-old','uncertain POST leaves a clear unconfirmed outcome'),
 ('network-owner','post-network-changed-follow','late old operation cannot overwrite connection-change notice'),
])
def test_retier_guards(tmp_path,mutation,mode,expected):
    binary=compile_retier(tmp_path,mutation)
    r=subprocess.run([str(binary),mode],capture_output=True,text=True,timeout=15)
    (tmp_path/'mutation-output.txt').write_text(r.stdout+r.stderr)
    assert r.returncode==1 and expected in r.stdout,r.stdout+r.stderr
    print('Mutation '+mutation+': intended failure: '+r.stdout.strip())
