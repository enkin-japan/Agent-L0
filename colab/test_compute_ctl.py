"""CPU-only tests of compute_ctl / compute_vm; fake credentials, fake CLI, no VM or models."""
import argparse
import base64
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('compute_ctl', Path(__file__).with_name('compute_ctl.py'))
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
vm_spec = importlib.util.spec_from_file_location('compute_vm', Path(__file__).with_name('compute_vm.py'))
vm = importlib.util.module_from_spec(vm_spec)
vm_spec.loader.exec_module(vm)


class ComputeCtlTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        temporary_state = patch.object(ctl, 'STATE', Path(state.name)/'state')
        temporary_state.start()
        self.addCleanup(temporary_state.stop)
        registry = patch.object(ctl,'registered_models',return_value={ctl.MODEL:{'revision':'a'*40}})
        registry.start(); self.addCleanup(registry.stop)
        for signum in (ctl.signal.SIGINT,ctl.signal.SIGTERM,ctl.signal.SIGHUP):
            self.addCleanup(ctl.signal.signal,signum,ctl.signal.getsignal(signum))

    def test_retained_monitor_ignores_deadline_and_health_failure_until_stop(self):
        lease = {'session':'agent-compute-0123456789ab','state':'ready','retain':True,'deadline':0,
                 'supervision_options':{'health_interval':1}}
        ctl.save_state(lease)
        calls = []
        def unhealthy(_):
            calls.append('probe')
            ctl.save_state({**lease,'state':'stopped'})
            raise RuntimeError('synthetic health failure')
        with patch.object(ctl,'check_compute',side_effect=unhealthy), patch.object(ctl,'stop_owned') as stop:
            ctl.monitor_ready(lease)
            self.assertEqual(calls,['probe'])
            stop.assert_not_called()

    def test_timed_monitor_still_expires(self):
        lease = {'session':'agent-compute-0123456789ab','state':'ready','deadline':0,
                 'supervision_options':{'health_interval':1}}
        ctl.save_state(lease)
        with patch.object(ctl,'check_compute') as probe:
            ctl.monitor_ready(lease)
            probe.assert_not_called()

    def test_watch_checks_exact_owned_endpoint_before_any_state_change(self):
        lease = {'session':'agent-compute-0123456789ab','state':'ready','endpoint':'owned','deadline':0}
        ctl.save_state(lease)
        with patch.object(ctl,'server_sessions',return_value={'owned':'unrelated'}), patch.object(ctl.subprocess,'Popen') as spawn:
            with self.assertRaises(ctl.LeaseStopped):
                ctl.watch_existing(argparse.Namespace(retain=True),-1)
            spawn.assert_not_called()
            self.assertEqual(ctl.current(),lease)

    def test_retained_watch_failure_never_releases_vm(self):
        lease = {'session':'agent-compute-0123456789ab','state':'ready','endpoint':'owned','retain':True,'deadline':0}
        ctl.save_state(lease)
        with patch.object(ctl,'server_sessions',return_value={'owned':lease['session']}), \
             patch.object(ctl,'monitor_ready',side_effect=OSError('synthetic disconnect')), \
             patch.object(ctl,'stop_owned') as stop:
            with self.assertRaises(OSError):
                ctl.watch_existing(argparse.Namespace(job=lease['session'],retain=False),-1,worker=True)
            stop.assert_not_called()
            self.assertTrue(ctl.current()['retain'])

    def test_failed_watch_child_marks_interrupted_without_releasing(self):
        lease = {'session':'agent-compute-0123456789ab','state':'ready','endpoint':'owned','retain':True,
                 'supervision':{'state':'starting'}}
        ctl.save_state(lease)
        with patch.object(ctl,'server_sessions',side_effect=OSError('synthetic slow discovery')), patch.object(ctl,'stop_owned') as stop:
            with self.assertRaises(OSError):
                ctl.run_watch_worker(argparse.Namespace(job=lease['session']),-1)
            self.assertEqual(ctl.current()['supervision']['state'],'interrupted')
            stop.assert_not_called()

    def test_interrupted_watch_does_not_overwrite_explicit_stop(self):
        lease = {'session':'agent-compute-0123456789ab','state':'stopped','retain':True}
        ctl.save_state(lease)
        ctl.mark_supervision_interrupted(lease['session'])
        self.assertEqual(ctl.current(),lease)

    def test_watch_signal_records_interruption_and_restores_handlers(self):
        import signal
        lease = {'session':'agent-compute-0123456789ab','state':'ready','retain':True}
        ctl.save_state(lease)
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(*args,**kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
        with patch.object(ctl,'watch_existing',side_effect=terminate):
            with self.assertRaises(KeyboardInterrupt):
                ctl.run_watch_worker(argparse.Namespace(job=lease['session']),-1)
        self.assertEqual(ctl.current()['supervision']['state'],'interrupted')
        self.assertEqual(signal.getsignal(signal.SIGTERM),previous)

    def test_status_does_not_trust_stale_or_recycled_supervisor_pid(self):
        lease = {'session':'agent-compute-0123456789ab','supervision':{'state':'active','pid':123}}
        for reply,expected in [
            (subprocess.CompletedProcess([],1,'',''),'interrupted'),
            (subprocess.CompletedProcess([],0,f'{os.getuid()} /usr/bin/python unrelated.py _worker --job {lease["session"]}',''),'interrupted'),
            (subprocess.CompletedProcess([],1,'','permission denied'),'unknown'),
            (subprocess.CompletedProcess([],0,f'{os.getuid()} {sys.executable} {ctl.__file__} _watch --job {lease["session"]}',''),'active')]:
            with patch.object(ctl.subprocess,'run',return_value=reply):
                self.assertEqual(ctl.supervisor_status(lease)['supervision']['state'],expected)
        self.assertEqual(lease['supervision']['state'],'active')

    def inventory(self, **updates):
        return dict({'sessions': {'original': 'llm'}, 'magic_dns': True,
                     'dns_suffix': 'example.ts.net', 'visible_compute_nodes': []}, **updates)

    def test_start_checks_leftover_resources_before_credentials_or_allocation(self):
        cases = [self.inventory(sessions={'old': 'agent-compute-0123456789ab'}),
                 self.inventory(sessions={'old': 'agent-demo-0123456789ab'}),  # legacy layout VM
                 self.inventory(sessions={'old': '?'}),
                 self.inventory(magic_dns=False), self.inventory(magic_dns=None),
                 self.inventory(visible_compute_nodes=[{'Online': False}])]
        for inventory in cases:
            with self.subTest(inventory=inventory), patch.object(ctl,'current',return_value=None), \
                 patch.object(ctl,'inspect_resources',return_value=inventory), \
                 patch.object(ctl,'env_credentials') as credentials, patch.object(ctl,'cli') as cli:
                with self.assertRaises(RuntimeError):
                    ctl.start(argparse.Namespace(hostname=ctl.HOSTNAME))
                credentials.assert_not_called(); cli.assert_not_called()

    def test_repeated_failed_starts_preserve_predecessor_resource_identity(self):
        ctl.save_state({'session':'agent-compute-0123456789ab','state':'stopped',
                         'endpoint':'old-endpoint','tailnet':{'id':'renamed-node'}},new_lease=True)
        with patch.object(ctl,'inspect_resources',return_value=self.inventory(sessions={'old-endpoint':'renamed-owned'})), \
             patch.object(ctl,'env_credentials',side_effect=AssertionError('unsafe credential phase reached')):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError,'still allocated'):
                    ctl.start(argparse.Namespace(hostname=ctl.HOSTNAME))
                self.assertEqual(ctl.current()['predecessor']['endpoint'],'old-endpoint')
                self.assertEqual(ctl.current()['predecessor']['tailnet']['id'],'renamed-node')

    def test_predecessor_guard_mutation_is_detected(self):
        with patch.object(ctl,'prior_resources',side_effect=lambda previous:
                          {k:previous[k] for k in ('endpoint','tailnet') if previous and k in previous}):
            with self.assertRaises(AssertionError):
                self.test_repeated_failed_starts_preserve_predecessor_resource_identity()

    def test_stopped_lease_is_checked_against_server_and_unrelated_vm_is_allowed(self):
        with patch.object(ctl,'inspect_resources',return_value=self.inventory()):
            self.assertEqual(ctl.check_start_resources(ctl.HOSTNAME,None),'example.ts.net')
            with self.assertRaisesRegex(RuntimeError,'still allocated'):
                ctl.check_start_resources(ctl.HOSTNAME,{'state':'stopped','endpoint':'original'})

    def test_inventory_reports_offline_legacy_conflicts_and_recorded_renamed_node(self):
        peers = {'a': {'ID':'one','HostName':'agent-demo-old','Online':False},
                 'b': {'ID':'two','DNSName':'agent-compute-1.example.ts.net.'},
                 'c': {'ID':'three','HostName':'renamed'},
                 'd': {'ID':'four','HostName':'colab-llm'}}
        with patch.object(ctl,'server_sessions',return_value={}), \
             patch.object(ctl,'current',return_value={'tailnet':{'id':'three'}}), \
             patch.object(ctl,'tailnet_status',return_value={'Peer':peers,'CurrentTailnet':{'MagicDNSEnabled':True}}):
            found=ctl.inspect_resources(ctl.HOSTNAME)
        self.assertEqual([p['ID'] for p in found['visible_compute_nodes']],['one','two','three'])

    def test_logout_result_is_not_assumed_and_does_not_prevent_vm_release(self):
        for reply, expected in [('COMPUTE_LOGOUT_OK\n','ephemeral-logout-confirmed'),
                                ('','logout-unconfirmed'),
                                (subprocess.TimeoutExpired('fake',10),'logout-unconfirmed')]:
            data={'session':'agent-compute-0123456789ab','endpoint':'owned','ephemeral':True}
            with self.subTest(reply=reply), \
                 patch.object(ctl,'server_sessions',side_effect=[{'owned':data['session']},{},{}]), \
                 patch.object(ctl,'remote',side_effect=reply if isinstance(reply,Exception) else None,
                              return_value=reply), patch.object(ctl,'cli') as cli, \
                 patch.object(ctl,'save_state'):
                ctl.stop_owned(data)
                cli.assert_called_once_with('stop','-s',data['session'],timeout=90)
                self.assertEqual(data['state'],'stopped')
                self.assertEqual(data['tailnet_cleanup'],expected)

    def test_normal_node_logout_does_not_claim_removal(self):
        data={'session':'agent-compute-0123456789ab'}
        with patch.object(ctl,'remote',return_value='COMPUTE_LOGOUT_OK\n'):
            ctl.logout_tailnet(data)
        self.assertEqual(data['tailnet_cleanup'],'logout-confirmed-removal-unverified')

    def tailnet_with(self,ip):
        return {'Peer':{'n':{'DNSName':'agent-compute.example.ts.net.','TailscaleIPs':[ip,'fd7a::1']}}}

    def test_tailnet_must_map_the_name_to_new_vm_and_retry_is_bounded(self):
        with patch.object(ctl,'tailnet_status',return_value=self.tailnet_with('100.64.0.2')) as tailnet, \
             patch.object(ctl,'system_addresses') as system, patch.object(ctl.time,'sleep'):
            with self.assertRaises(RuntimeError): ctl.verify_dns('agent-compute.example.ts.net','100.64.0.1')
            self.assertEqual(tailnet.call_count,3)
            system.assert_not_called()
        right=subprocess.CompletedProcess(['fake'],0,stdout='["100.64.0.1"]')
        with patch.object(ctl,'tailnet_status',side_effect=[{'Peer':{}},self.tailnet_with('100.64.0.1')]), \
             patch.object(ctl.subprocess,'run',return_value=right), patch.object(ctl.time,'sleep'):
            self.assertEqual(ctl.verify_dns('agent-compute.example.ts.net','100.64.0.1'),'ok')

    def test_unreadable_local_tailscale_keeps_ready_vm(self):
        # Daemon restarting / Mac waking says nothing about the VM; the VM already
        # verified its own exact name. Only a readable, wrong mapping releases.
        events=[]
        with patch.object(ctl,'tailnet_status',side_effect=RuntimeError('本机 Tailscale 未运行')), \
             patch.object(ctl,'system_addresses') as system, \
             patch.object(ctl,'event',side_effect=lambda text,**f: events.append((text,f))), \
             patch.object(ctl.time,'sleep'):
            self.assertEqual(ctl.verify_dns('agent-compute.example.ts.net','100.64.0.1'),'unknown')
        system.assert_not_called()
        self.assertEqual(events[-1][1]['error_type'],'RuntimeError')
        with patch.object(ctl,'tailnet_status',side_effect=[RuntimeError('down'),self.tailnet_with('100.64.0.2'),
                                                            RuntimeError('down')]), \
             patch.object(ctl.time,'sleep'):
            with self.assertRaises(RuntimeError): ctl.verify_dns('agent-compute.example.ts.net','100.64.0.1')

    def test_local_resolver_spawn_failure_is_advisory(self):
        with patch.object(ctl.subprocess,'run',side_effect=OSError('spawn')):
            self.assertIsNone(ctl.system_addresses('agent-compute.example.ts.net'))
        bad=subprocess.CompletedProcess(['fake'],0,stdout='not json')
        with patch.object(ctl.subprocess,'run',return_value=bad):
            self.assertIsNone(ctl.system_addresses('agent-compute.example.ts.net'))

    def test_stale_local_resolver_is_reported_not_fatal(self):
        # Observed 2026-10-24: MagicDNS had the new VM, mDNSResponder still served a
        # cached "no such host"; the launcher released a ready VM over it.
        events=[]
        with patch.object(ctl,'tailnet_status',return_value=self.tailnet_with('100.64.0.1')), \
             patch.object(ctl.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'fake')) as resolve, \
             patch.object(ctl,'event',side_effect=lambda text,**f: events.append((text,f))), \
             patch.object(ctl.time,'sleep'):
            self.assertEqual(ctl.verify_dns('agent-compute.example.ts.net','100.64.0.1'),'stale')
        self.assertEqual(resolve.call_count,3)
        self.assertIn('mDNSResponder',events[-1][1]['refresh'])

    def test_reused_name_does_not_stop_or_logout_the_other_vm(self):
        data={'session':'agent-compute-0123456789ab','endpoint':'old'}
        assignments={'old':'?','other':data['session']}
        with patch.object(ctl,'server_sessions',side_effect=[assignments,assignments,{'other':data['session']}]), \
             patch.object(ctl,'remote') as remote, patch.object(ctl,'cli') as cli, \
             patch.object(ctl,'save_state'), patch.object(ctl,'release_endpoint') as release:
            ctl.stop_owned(data)
            remote.assert_not_called(); cli.assert_not_called()
            release.assert_called_once_with('old')

    def test_vm_uses_ephemeral_identity(self):
        command=vm.tailscaled_command('/synthetic.sock')
        self.assertIn('--state=mem:',command)
        self.assertEqual(len([a for a in command if a.startswith('--state=')]),1)

    def test_vm_rejects_suffixed_dns_but_keeps_identity_for_recovery(self):
        for dns in ['agent-compute.example.ts.net.','agent-compute-1.example.ts.net.']:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                payload={'BackendState':'Running','Self':{'ID':'node-one','DNSName':dns,
                                                        'TailscaleIPs':['100.64.0.1']}}
                result=subprocess.CompletedProcess(['fake'],0,stdout=json.dumps(payload))
                with patch.object(vm,'PRIVATE',root), patch.object(vm.subprocess,'run',return_value=result):
                    if '-1.' in dns:
                        with self.assertRaises(ValueError): vm.record_tailnet('socket','agent-compute.example.ts.net')
                    else:
                        self.assertEqual(vm.record_tailnet('socket','agent-compute.example.ts.net'),'100.64.0.1')
                    vm.progress('failed')
                    self.assertEqual(json.loads((root/'progress.json').read_text())['tailnet']['id'],'node-one')

    def test_unknown_dns_blocks_start(self):
        with patch.object(ctl,'current',return_value=None), \
             patch.object(ctl,'inspect_resources',return_value=self.inventory(magic_dns=None)), \
             patch.object(ctl,'env_credentials',side_effect=AssertionError('credentials reached')):
            with self.assertRaises(RuntimeError):
                ctl.start(argparse.Namespace(hostname=ctl.HOSTNAME))

    def test_stalled_bootstrap_is_bounded_and_cleanup_is_attempted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); key=root/'key'; key.write_text('tskey-auth-synthetic'); key.chmod(0o600)
            with patch.object(ctl,'STATE',root/'state'), \
                 patch.object(ctl,'check_start_resources',return_value='example.ts.net'), \
                 patch.object(ctl,'env_credentials',return_value={}), patch.object(ctl,'make_archive',return_value=5), \
                 patch.object(ctl,'cli'), \
                 patch.object(ctl,'server_sessions',side_effect=lambda: {'endpoint':ctl.current()['session']}), \
                 patch.object(ctl,'remote',side_effect=['COMPUTE_GPU_VERIFIED','COMPUTE_BOOTSTRAP_STARTED']), \
                 patch.object(ctl,'poll_progress',return_value={'phase':'loading-models'}), \
                 patch.object(ctl.time,'monotonic',side_effect=[0,0,100,100]), \
                 patch.object(ctl,'collect_failure_diagnostic') as diagnostic, patch.object(ctl,'stop_owned') as stop:
                with self.assertRaisesRegex(RuntimeError,'停滞'):
                    ctl.start(argparse.Namespace(auth_key=key,minutes=1,hostname=ctl.HOSTNAME,stall_timeout=30))
                stop.assert_called_once(); diagnostic.assert_called_once()

    def test_health_failures_have_bounded_retries(self):
        with patch.object(ctl,'remote',return_value='COMPUTE_HEALTH {"infer":false,"rag":true}\n') as probe, \
             patch.object(ctl.time,'sleep'):
            with self.assertRaises(RuntimeError): ctl.check_compute({'session':'synthetic'})
            self.assertEqual(probe.call_count,3)

    def test_model_validation_precedes_allocation(self):
        with self.assertRaisesRegex(ValueError,'已注册的 CUDA'):
            ctl.deployment_options(argparse.Namespace(model='mlx-community/synthetic'))

    def test_online_quantization_is_rejected_d25(self):
        # D25: every CUDA entry is a prequantized checkpoint; no --quantization path survives.
        qat = 'google/gemma-4-31B-it-qat-w4a16-ct'
        with patch.object(ctl,'registered_models',return_value={
                qat:{'revision':'a'*40,'dialect':'gemma4','cuda_deployment':{'prequantized':True}}}):
            with self.assertRaisesRegex(ValueError,'D25'):
                ctl.deployment_options(argparse.Namespace(model=qat,quantization='fp8'))
            self.assertEqual(ctl.deployment_options(argparse.Namespace(model=qat))['quantization'],'none')

    def test_registered_models_requires_prequantized_cuda_entries_d25(self):
        # setUp patches registered_models; REAL_REGISTRY (module bottom) is the unpatched function.
        def registry_with(entries):
            directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
            root = Path(directory.name)
            (root/'config.yaml').write_text(json.dumps({'inference':{'model_registry':entries}}))
            return root
        mlx = {'repo':'mlx-community/synthetic-4bit','revision':'c'*40}        # MLX: not a CUDA entry
        ok = {'repo':'org/prequantized','revision':'a'*40,'cuda_deployment':{'prequantized':True}}
        for bad in ({'repo':'org/bf16','revision':'b'*40},
                    {'repo':'org/bf16','revision':'b'*40,'cuda_deployment':{'supports_mtp':True}},
                    {'repo':'org/bf16','revision':'b'*40,'cuda_deployment':{'prequantized':'yes'}}):
            with self.subTest(bad=bad), patch.object(ctl,'ROOT',registry_with([mlx,ok,bad])):
                with self.assertRaisesRegex(ValueError,'D25.*org/bf16|org/bf16.*D25'):
                    REAL_REGISTRY()
        with patch.object(ctl,'ROOT',registry_with([mlx,ok])):
            self.assertEqual(set(REAL_REGISTRY()),{'org/prequantized'})

    def test_quantization_flag_is_gone_from_the_cli(self):
        parser = ctl.build_parser()
        for command in ('start','switch'):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit):
                    parser.parse_args([command,'--quantization','fp8'])
                self.assertIn('unrecognized arguments',err.getvalue())

    def test_stop_terminal_state_survives_stale_boot_progress(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ctl,'STATE',Path(directory)):
            pending={'session':'agent-compute-0123456789ab','state':'booting'}
            ctl.save_state(pending)
            stopping=dict(pending,state='stopping')
            ctl.save_state(stopping)
            with self.assertRaises(ctl.LeaseStopped): ctl.save_state(dict(pending,tailnet={'id':'late'}))
            ctl.save_state(dict(stopping,state='release-unconfirmed'))
            with self.assertRaises(ctl.LeaseStopped): ctl.save_state(dict(pending,state='ready'))
            ctl.save_state(dict(stopping,state='stopped'))
            with self.assertRaises(ctl.LeaseStopped): ctl.save_state(dict(pending,state='ready'))
            self.assertEqual(ctl.current()['state'],'stopped')
            next_lease={'session':'agent-compute-abcdef012345','state':'preparing'}
            ctl.save_state(next_lease,new_lease=True)
            with self.assertRaises(ctl.LeaseStopped): ctl.save_state(dict(stopping,state='stopped'))
            self.assertEqual(ctl.current(),next_lease)

    def test_terminal_state_guard_mutation_is_detected(self):
        # Simulate the previous unconditional writer without modifying source.
        def stale_writer(data, **_):
            ctl.private_dir(ctl.STATE)
            (ctl.STATE/'current.json').write_text(json.dumps(data))
        with patch.object(ctl,'save_state',side_effect=stale_writer):
            with self.assertRaises(AssertionError): self.test_stop_terminal_state_survives_stale_boot_progress()

    def test_ready_vm_with_stale_local_dns_becomes_ready_not_released(self):
        now=time.time()
        lease={'session':'agent-compute-0123456789ab','started':now,'deadline':now+600,
               'expected_dns':'agent-compute.example.ts.net',
               'options':{'bootstrap_timeout':600,'stall_timeout':60}}
        with patch.object(ctl,'lease_alive'), patch.object(ctl,'save_state'), \
             patch.object(ctl,'poll_progress',return_value={'phase':'ready','host':'100.64.0.1'}), \
             patch.object(ctl,'verify_dns',return_value='stale'):
            ctl._await_ready(lease)
        self.assertEqual((lease['state'],lease['ip'],lease['local_dns']),('ready','100.64.0.1','stale'))

    def test_stop_during_progress_preserves_shutdown_and_retains_cleanup_responsibility(self):
        for stopped in ('stopping','stopped','release-unconfirmed'):
            with self.subTest(state=stopped), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); key=root/'key'; key.write_text('tskey-auth-synthetic'); key.chmod(0o600)
                def stopped_progress(*_):
                    data=ctl.current(); data['state']=stopped; ctl.save_state(data)
                    return {'phase':'ready','host':'100.64.0.1','tailnet':{'id':'late'}}
                with patch.object(ctl,'STATE',root/'state'), \
                     patch.object(ctl,'check_start_resources',return_value='example.ts.net'), \
                     patch.object(ctl,'env_credentials',return_value={}), patch.object(ctl,'make_archive',return_value=5), \
                     patch.object(ctl,'cli'), \
                     patch.object(ctl,'server_sessions',side_effect=lambda: {'endpoint':ctl.current()['session']}), \
                     patch.object(ctl,'remote',side_effect=['COMPUTE_GPU_VERIFIED','COMPUTE_BOOTSTRAP_STARTED']), \
                     patch.object(ctl,'poll_progress',side_effect=stopped_progress), \
                     patch.object(ctl,'stop_owned') as stop, patch.object(ctl,'verify_dns') as dns:
                    ctl.start(argparse.Namespace(auth_key=key,minutes=1,hostname=ctl.HOSTNAME))
                    self.assertEqual(ctl.current()['state'],stopped)
                    if stopped == 'stopped': stop.assert_not_called()
                    else: stop.assert_called_once()
                    dns.assert_not_called()

    def test_stop_failure_persists_unconfirmed_and_next_owner_retries(self):
        data={'session':'agent-compute-0123456789ab','endpoint':'owned','state':'ready'}
        ctl.save_state(data)
        with patch.object(ctl,'server_sessions',side_effect=[{'owned':data['session']},OSError('synthetic failure')]), \
             patch.object(ctl,'remote',return_value=''), \
             patch.object(ctl,'cli',side_effect=subprocess.TimeoutExpired('fake',10)):
            with self.assertRaises(OSError): ctl.stop_attempt(dict(data))
        self.assertEqual(ctl.current()['state'],'release-unconfirmed')
        with patch.object(ctl,'server_sessions',side_effect=[{'owned':data['session']},{},{}]), \
             patch.object(ctl,'remote',return_value='COMPUTE_LOGOUT_OK\n'), patch.object(ctl,'cli') as cli:
            ctl.stop_owned(dict(data))
            cli.assert_called_once()
        self.assertEqual(ctl.current()['state'],'stopped')

    def test_stop_waiter_skips_success_and_recovers_abandoned_stopping(self):
        data={'session':'agent-compute-0123456789ab','endpoint':'owned','state':'ready'}
        ctl.save_state(dict(data,state='stopped'))
        with patch.object(ctl,'server_sessions') as inventory:
            ctl.stop_owned(dict(data))
            inventory.assert_not_called()
        # A killed stop releases its OS lock but can leave a stopping checkpoint.
        ctl.save_state(dict(data,state='stopping'))
        with patch.object(ctl,'server_sessions',return_value={}), patch.object(ctl,'remote') as remote:
            ctl.stop_owned(dict(data))
            remote.assert_not_called()
        self.assertEqual(ctl.current()['state'],'stopped')

    def assert_cleanup_lock_blocks(self, disable_lock=False):
        data={'session':'agent-compute-0123456789ab','endpoint':'owned','state':'ready'}
        ctl.save_state(data)
        code='''import importlib.util,json,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('ctl',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.STATE=Path(sys.argv[2]); m.server_sessions=lambda: {}
if sys.argv[3]=='mutate': m.fcntl.flock=lambda *args: None
print('ATTEMPTING',flush=True)
m.stop_owned(m.current())
'''
        with (ctl.STATE/'cleanup.lock').open('a') as lock:
            ctl.fcntl.flock(lock,ctl.fcntl.LOCK_EX)
            child=subprocess.Popen([sys.executable,'-c',code,str(Path(ctl.__file__).resolve()),
                                    str(ctl.STATE),'mutate' if disable_lock else 'normal'],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertTrue(select.select([child.stdout],[],[],5)[0],'child failed to start')
                self.assertEqual(child.stdout.readline().strip(),'ATTEMPTING')
                with self.assertRaises(subprocess.TimeoutExpired): child.communicate(timeout=.2)
            finally:
                ctl.fcntl.flock(lock,ctl.fcntl.LOCK_UN)
                try: child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.communicate(); raise
            self.assertEqual(child.returncode,0)
        self.assertEqual(ctl.current()['state'],'stopped')

    def test_cleanup_lock_serializes_processes(self):
        self.assert_cleanup_lock_blocks()

    def test_cleanup_lock_mutation_is_detected(self):
        with self.assertRaises(AssertionError): self.assert_cleanup_lock_blocks(disable_lock=True)

    def test_resource_and_ephemeral_guard_mutations_are_detected(self):
        with patch.object(ctl,'check_start_resources',return_value='example.ts.net'):
            # The protected test must fail before any credential is consumed.
            with self.assertRaises(AssertionError): self.test_unknown_dns_blocks_start()
        with patch.object(vm,'tailscaled_command',return_value=['tailscaled','--state=/tmp/persistent']):
            with self.assertRaises(AssertionError): self.test_vm_uses_ephemeral_identity()

    def test_archive_does_not_include_private_or_runtime_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = ['config.yaml','devctl.sh','inference/server.py','rag/compute_server.py',
                     'backend/framework/core/test.py','backend/scripts/fetch_model.py','.env.local','.keys/authkey',
                     'backend/sessions/private/history.py','inference/media/picture.png']
            for name in files:
                p = root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_text('synthetic')
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','-C',str(root),'add','--',*files],check=True)
            archive = root/'result.tar.gz'
            ctl.make_archive(root,archive)
            with tarfile.open(archive) as tar:
                self.assertEqual(set(tar.getnames()), set(files[:6]))
            self.assertEqual(archive.stat().st_mode & 0o777,0o600)
            (root/'inference/server.py').unlink()
            (root/'inference/server.py').symlink_to(root/'.env.local')
            with self.assertRaises(ValueError): ctl.make_archive(root,archive)

    def test_secret_permissions_literal_and_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'env'
            p.write_text('INFER_API_KEY="synthetic-infer-key"\nRAG_COMPUTE_API_KEY=synthetic-rag-key-123\nOTHER_SECRET=must-not-transfer\n')
            p.chmod(0o600)
            values=ctl.env_credentials([p])
            self.assertEqual(set(values),{'INFER_API_KEY','RAG_COMPUTE_API_KEY'})
            p.chmod(0o644)
            with self.assertRaises(ValueError): ctl.env_credentials([p])

    def test_ambiguous_creation_failure_still_attempts_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); key=root/'key'; key.write_text('tskey-auth-synthetic'); key.chmod(0o600)
            state=root/'state'
            with patch.object(ctl,'STATE',state), patch.object(ctl,'env_credentials',return_value={}), \
                 patch.object(ctl,'make_archive',return_value=5), \
                 patch.object(ctl,'check_start_resources',return_value='example.ts.net'), \
                 patch.object(ctl,'cli',side_effect=subprocess.TimeoutExpired('colab',120)), \
                 patch.object(ctl,'stop_owned') as stop:
                with self.assertRaises(subprocess.TimeoutExpired):
                    ctl.start(argparse.Namespace(auth_key=key,minutes=1,hostname=ctl.HOSTNAME))
                self.assertEqual(stop.call_count,1)
                self.assertTrue(stop.call_args.args[0]['session'].startswith('agent-compute-'))

    def test_local_preparation_failure_leaves_no_allocation_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); key=root/'key'; key.write_text('tskey-auth-synthetic'); key.chmod(0o600)
            args=argparse.Namespace(auth_key=key,minutes=1,hostname=ctl.HOSTNAME)
            with patch.object(ctl,'STATE',root/'state'), patch.object(ctl,'env_credentials',return_value={}), \
                 patch.object(ctl,'make_archive',side_effect=ValueError('synthetic unsafe source')) as archive, \
                 patch.object(ctl,'check_start_resources',return_value='example.ts.net'), \
                 patch.object(ctl,'cli') as cli, patch.object(ctl,'stop_owned') as stop:
                with self.assertRaisesRegex(ValueError,'unsafe source'): ctl.start(args)
                cli.assert_not_called(); stop.assert_not_called()
                self.assertEqual(ctl.current()['state'],'stopped')
                archive.side_effect=None; archive.return_value=5
                cli.side_effect=subprocess.TimeoutExpired('colab',120)
                with self.assertRaises(subprocess.TimeoutExpired): ctl.start(args)
                self.assertEqual(cli.call_args.args[0],'new')
                stop.assert_called_once()

    def test_refuses_unowned_stop(self):
        with patch.object(ctl,'cli') as cli:
            with self.assertRaises(ValueError): ctl.stop_owned({'session':'llm'})
            cli.assert_not_called()

    def test_stop_checks_server_and_recovers_pruned_local_record(self):
        data={'session':'agent-compute-0123456789ab','endpoint':'owned-endpoint'}
        with patch.object(ctl,'server_sessions',side_effect=[{'owned-endpoint':'?'},{'owned-endpoint':'?'},{}]), \
             patch.object(ctl,'remote',side_effect=RuntimeError), patch.object(ctl,'cli'), \
             patch.object(ctl,'save_state'), patch.object(ctl,'release_endpoint') as release:
            ctl.stop_owned(data)
            release.assert_called_once_with('owned-endpoint')
            self.assertEqual(data['state'],'stopped')

    def test_missing_local_identity_is_not_release_success(self):
        data={'session':'agent-compute-0123456789ab'}
        with patch.object(ctl,'server_sessions',return_value={'orphan':'?'}), patch.object(ctl,'save_state'), \
             patch.object(ctl,'release_endpoint') as release:
            with self.assertRaises(RuntimeError): ctl.stop_owned(data)
            self.assertEqual(data['state'],'release-unconfirmed')
            release.assert_not_called()

    def test_cli_rejected_allocation_releases_lease_once_server_shows_nothing(self):
        # `colab new` exited non-zero: stop may record STOPPED, but only after the
        # server list shows neither this session nor an unattributed VM.
        data={'session':'agent-compute-0123456789ab','allocation_attempted':True,'allocation_failed':'cli-error'}
        with patch.object(ctl,'server_sessions',return_value={}) as sessions, patch.object(ctl,'save_state'), \
             patch.object(ctl.time,'sleep') as sleep, \
             patch.object(ctl,'release_endpoint') as release, patch.object(ctl,'cli') as cli:
            ctl.stop_owned(data)
            self.assertEqual(data['state'],'stopped'); release.assert_not_called(); cli.assert_not_called()
            self.assertEqual(sessions.call_count,3); self.assertEqual(sleep.call_count,2)
        # An unattributed VM shows up only on a later read (CLI failed after the server accepted).
        data={'session':'agent-compute-0123456789ab','allocation_attempted':True,'allocation_failed':'cli-error'}
        views=iter([{},{}])
        with patch.object(ctl,'server_sessions',side_effect=lambda: next(views,{'late':'?'})), \
             patch.object(ctl,'save_state'), patch.object(ctl.time,'sleep'), patch.object(ctl,'cli') as cli:
            with self.assertRaises(RuntimeError): ctl.stop_owned(data)
            self.assertEqual(data['state'],'release-unconfirmed'); cli.assert_not_called()
        data={'session':'agent-compute-0123456789ab','allocation_attempted':True,'allocation_failed':'cli-error'}
        with patch.object(ctl,'server_sessions',return_value={'orphan':'?'}), patch.object(ctl,'save_state'):
            with self.assertRaises(RuntimeError): ctl.stop_owned(data)
            self.assertEqual(data['state'],'release-unconfirmed')
        data={'session':'agent-compute-0123456789ab','allocation_attempted':True}  # timeout leaves no marker
        with patch.object(ctl,'server_sessions',return_value={}), patch.object(ctl,'save_state'):
            with self.assertRaises(RuntimeError): ctl.stop_owned(data)
            self.assertEqual(data['state'],'release-unconfirmed')

    def test_operator_confirmation_clears_unconfirmed_lease_only_without_unknown_vms(self):
        data={'session':'agent-compute-0123456789ab','allocation_attempted':True}
        with patch.object(ctl,'server_sessions',return_value={'orphan':'?'}), patch.object(ctl,'save_state'), \
             patch.object(ctl.time,'sleep'):
            with self.assertRaises(RuntimeError): ctl.stop_owned(data, operator_confirmed=True)
            self.assertEqual(data['state'],'release-unconfirmed')
        with patch.object(ctl,'server_sessions',return_value={}), patch.object(ctl,'save_state'), \
             patch.object(ctl.time,'sleep'):
            ctl.stop_owned(data, operator_confirmed=True)
            self.assertEqual(data['state'],'stopped')
            self.assertNotIn('operator_confirmed_unallocated',data)  # confirmation is per invocation

    def test_stop_command_passes_operator_confirmation_only_when_flag_given(self):
        lease={'session':'agent-compute-0123456789ab','state':'release-unconfirmed','allocation_attempted':True}
        with patch.object(ctl,'snapshot',return_value=dict(lease)), patch.object(ctl,'stop_owned') as stop:
            ctl.dispatch(argparse.Namespace(command='stop',confirm_unallocated=True))
            self.assertEqual(stop.call_args.kwargs,{'operator_confirmed':True})
            stop.reset_mock()
            ctl.dispatch(argparse.Namespace(command='stop',confirm_unallocated=False))
            self.assertEqual(stop.call_args.kwargs,{'operator_confirmed':False})
            stop.reset_mock()
            ctl.dispatch(argparse.Namespace(command='stop'))  # older callers without the flag
            self.assertEqual(stop.call_args.kwargs,{'operator_confirmed':False})
            self.assertNotIn('operator_confirmed_unallocated',stop.call_args.args[0])

    def test_failed_new_marks_lease_and_unmocked_stop_recovers_to_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); key=root/'key'; key.write_text('tskey-auth-synthetic'); key.chmod(0o600)
            args=argparse.Namespace(auth_key=key,minutes=1,hostname=ctl.HOSTNAME)
            calls=[]
            def fake_cli(*a,**k):
                calls.append(a[0])
                if a[0]=='new': raise subprocess.CalledProcessError(1,'colab')
                if a[0]=='sessions':
                    return subprocess.CompletedProcess(a,0,stdout='No active sessions found on server.\n',stderr='')
                raise AssertionError(a)
            with patch.object(ctl,'STATE',root/'state'), patch.object(ctl,'env_credentials',return_value={}), \
                 patch.object(ctl,'make_archive',return_value=5), \
                 patch.object(ctl,'check_start_resources',return_value='example.ts.net'), \
                 patch.object(ctl,'cli',side_effect=fake_cli), patch.object(ctl.time,'sleep'):
                with self.assertRaises(subprocess.CalledProcessError): ctl.start(args)
                current=ctl.current()
                self.assertEqual(current['allocation_failed'],'cli-error')
                self.assertEqual(current['state'],'stopped')
                self.assertNotIn('stop',calls)
                log=(ctl.STATE/'deploy.log').read_text()
                self.assertIn('Colab CLI 申请 VM 失败',log)
                self.assertIn('CalledProcessError',log)

    def test_secret_guard_mutations_are_detected(self):
        # Mutate only in-memory function globals, proving the archive test detects leakage.
        original=ctl.source_allowed
        with patch.object(ctl,'source_allowed',side_effect=lambda n: original(n) or n=='.env.local'):
            with self.assertRaises(AssertionError):
                self.test_archive_does_not_include_private_or_runtime_data()

    def test_failure_output_redacts_before_truncating(self):
        secret='synthetic-random-service-key-abcd0123456789'
        output='x'*50+secret+'z'*2480
        with tempfile.TemporaryDirectory() as directory, patch.object(vm,'PRIVATE',Path(directory)), \
             patch.object(vm,'SECRET_VALUES',[secret]):
            with self.assertRaises(subprocess.CalledProcessError):
                vm.run(sys.executable,'-c','import sys; print(sys.argv[1]); sys.exit(1)',output)
            self.assertNotIn('abcd0123456789',vm.LAST_FAILURE)
            self.assertLessEqual(len(vm.LAST_FAILURE),2500)

    def test_failure_artifact_preserves_worker_cause_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); logs=root/'logs'; logs.mkdir()
            secret='synthetic-service-key-abcdefgh'
            (logs/'infer.log').write_text('ROOT CAUSE: unsupported kernel\n'+secret+
                '\nhttps://example.test/file?Signature=synthetic-signed-value\n'+'traceback\n'*500)
            with patch.object(vm,'APP',root), patch.object(vm,'PRIVATE',root), \
                 patch.object(vm,'SECRET_VALUES',[secret]), patch.object(vm,'LAST_FAILURE','short tail'):
                vm.save_failure_diagnostic()
            sanitized=(root/'failure-diagnostic.log').read_text()
            self.assertIn('ROOT CAUSE: unsupported kernel',sanitized)
            self.assertNotIn(secret,sanitized)
            self.assertNotIn('synthetic-signed-value',sanitized)
            output='COMPUTE_TEXT '+base64.b64encode(sanitized.encode()).decode()+'\n'
            with patch.object(ctl,'STATE',root), patch.object(ctl,'remote',return_value=output):
                ctl.collect_failure_diagnostic({'session':'agent-compute-0123456789ab',
                                                'vm_layout':ctl.common.LAYOUT})
            saved=root/'failure-agent-compute-0123456789ab.log'
            self.assertEqual(saved.read_text(),sanitized)
            self.assertEqual(saved.stat().st_mode & 0o777,0o600)

    def test_diagnostic_redaction_mutation_is_detected(self):
        with patch.object(vm,'redact',side_effect=lambda text:text):
            with self.assertRaises(AssertionError):
                self.test_failure_artifact_preserves_worker_cause_without_credentials()

    def test_progress_query_recovers_from_timeout_and_is_bounded(self):
        timeout=subprocess.TimeoutExpired('colab',120)
        with patch.object(ctl,'remote',side_effect=[timeout,'COMPUTE_PROGRESS {"phase":"ready"}\n']) as remote, \
             patch.object(ctl.time,'sleep'):
            self.assertEqual(ctl.poll_progress({'session':'synthetic'},time.time()+600)['phase'],'ready')
            self.assertEqual(remote.call_count,2)
        with patch.object(ctl,'remote',side_effect=timeout) as remote, patch.object(ctl.time,'sleep'):
            with self.assertRaises(subprocess.TimeoutExpired): ctl.poll_progress({'session':'synthetic'},time.time()+600)
            self.assertEqual(remote.call_count,3)
        with patch.object(ctl,'remote') as remote:
            with self.assertRaises(RuntimeError): ctl.poll_progress({'session':'synthetic'},time.time()-1)
            remote.assert_not_called()



class GuardTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.root = Path(state.name)
        for name, value in [('STATE', self.root/'state'),
                            ('registered_models', lambda: {ctl.MODEL: {'revision': 'a'*40}})]:
            patcher = patch.object(ctl, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        for signum in ctl.INTERRUPT_SIGNALS:
            self.addCleanup(ctl.signal.signal, signum, ctl.signal.getsignal(signum))

    def boot_with_state_damage(self, damage, retain=False):
        """Boot until VM progress is flowing, then damage the state file just before the
        next liveness read -- the last read before finish_start decides about release."""
        key = self.root/'key'; key.write_text('tskey-auth-synthetic'); key.chmod(0o600)
        reached, real_alive = [], ctl.lease_alive
        def alive(lease):
            if reached:  # second poll: VM is booting, its progress already recorded
                damage(ctl.STATE/'current.json')
            else:
                reached.append(ctl.current()['state'])
            return real_alive(lease)
        with patch.object(ctl, 'check_start_resources', return_value='example.ts.net'), \
             patch.object(ctl, 'env_credentials', return_value={}), \
             patch.object(ctl, 'make_archive', return_value=5), patch.object(ctl, 'cli'), \
             patch.object(ctl, 'server_sessions', side_effect=lambda: {'endpoint': ctl.current()['session']}), \
             patch.object(ctl, 'remote', side_effect=['COMPUTE_GPU_VERIFIED', 'COMPUTE_BOOTSTRAP_STARTED']), \
             patch.object(ctl, 'poll_progress', return_value={'phase': 'loading-models'}), \
             patch.object(ctl, 'lease_alive', side_effect=alive), patch.object(ctl.time, 'sleep'), \
             patch.object(ctl, 'collect_failure_diagnostic'), patch.object(ctl, 'stop_owned') as stop:
            args = argparse.Namespace(auth_key=key, minutes=1, hostname=ctl.HOSTNAME, retain=retain)
            with contextlib.suppress(Exception):
                ctl.start(args)
        self.assertEqual(reached, ['booting'])  # the fixture really reached a booting VM
        return stop

    def test_lost_or_corrupt_state_file_still_releases_the_allocated_vm(self):
        for name, damage in [('missing', lambda p: p.unlink()),
                             ('corrupt', lambda p: p.write_text('{not json'))]:
            for retain in (False, True):  # a booting VM is never "retained READY"
                with self.subTest(state=name, retain=retain):
                    shutil.rmtree(ctl.STATE, ignore_errors=True)
                    self.boot_with_state_damage(damage, retain).assert_called_once()

    def test_real_release_overwrites_a_corrupt_state_file(self):
        lease = {'session': 'agent-compute-0123456789ab', 'endpoint': 'owned', 'state': 'ready'}
        ctl.private_dir(ctl.STATE)
        (ctl.STATE/'current.json').write_text('{not json')
        with patch.object(ctl, 'server_sessions', side_effect=[{'owned': lease['session']}, {}, {}]), \
             patch.object(ctl, 'remote', return_value='COMPUTE_LOGOUT_OK\n'), \
             patch.object(ctl, 'cli') as cli:
            ctl.stop_owned(dict(lease))
        cli.assert_called_once_with('stop', '-s', lease['session'], timeout=90)
        self.assertEqual(ctl.current()['state'], 'stopped')

    def test_corrupt_state_accepts_only_release_writes(self):
        ctl.private_dir(ctl.STATE)
        (ctl.STATE/'current.json').write_text('{not json')
        for state in ('ready', 'booting', 'switching'):
            with self.subTest(state=state), self.assertRaises(ctl.LeaseStopped):
                ctl.save_state({'session': 'agent-compute-0123456789ab', 'state': state})
        self.assertEqual((ctl.STATE/'current.json').read_text(), '{not json')

    def test_operator_stop_on_corrupt_state_explains_instead_of_crashing(self):
        ctl.private_dir(ctl.STATE)
        (ctl.STATE/'current.json').write_text('{not json')
        with patch.object(ctl, 'cli') as cli, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ctl.main(['stop']), 1)
        cli.assert_not_called()
        self.assertIn('状态文件已损坏', (ctl.STATE/'deploy.log').read_text())

    def test_watch_worker_releases_timed_lease_when_state_becomes_unreadable(self):
        lease = {'session': 'agent-compute-0123456789ab', 'state': 'ready', 'endpoint': 'owned',
                 'deadline': 0}
        ctl.private_dir(ctl.STATE)
        for retain, expected in ((False, 1), (True, 0)):
            with self.subTest(retain=retain):
                ctl._write_state({**lease, 'retain': retain})
                def vanish(_):
                    (ctl.STATE/'current.json').unlink()
                with patch.object(ctl, 'server_sessions', return_value={'owned': lease['session']}), \
                     patch.object(ctl, 'monitor_ready', side_effect=vanish), \
                     patch.object(ctl, 'stop_owned') as stop:
                    ctl.watch_existing(argparse.Namespace(job=lease['session']), -1, worker=True)
                self.assertEqual(stop.call_count, expected)

    def test_cli_private_release_requires_a_verified_version(self):
        with patch.object(ctl, 'colab_cli_interpreter', return_value='/synthetic/python'), \
             patch.object(ctl.subprocess, 'run') as run:
            with patch.object(ctl, 'colab_cli_version', return_value='0.8.0'):
                with self.assertRaisesRegex(RuntimeError, '未经验证'):
                    ctl.release_endpoint('owned')
                run.assert_not_called()
            with patch.object(ctl, 'colab_cli_version', return_value=sorted(ctl.VERIFIED_COLAB_CLI)[0]):
                ctl.release_endpoint('owned')
                self.assertEqual(run.call_args.args[0][0], '/synthetic/python')
                self.assertEqual(run.call_args.args[0][-1], 'owned')

    def test_unparseable_sessions_name_the_cli_version(self):
        reply = subprocess.CompletedProcess([], 0, 'a changed table format', '')
        with patch.object(ctl, 'cli', return_value=reply), \
             patch.object(ctl, 'colab_cli_version', return_value='9.9.9'):
            with self.assertRaisesRegex(RuntimeError, '9.9.9'):
                ctl.server_sessions()

    def test_parser_scopes_options_to_commands_and_hides_internals(self):
        parser = ctl.build_parser()
        for argv in (['stop', '--model', 'x'], ['logs', '--speculative', 'mtp'],
                     ['start', '--draft-tokens', '0'], ['start', '--minutes', '241'],
                     ['watch', '--model', 'x'], ['_worker', '--job', 'x']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        args = parser.parse_args(['start'])
        self.assertIsNone(args.speculative)  # resolved per model below
        for name in ('minutes', 'model', 'max_model_len', 'gpu_memory',
                     'stall_timeout', 'bootstrap_timeout', 'poll_interval', 'health_interval'):
            self.assertEqual(getattr(args, name), ctl.DEFAULTS[name], name)
        self.assertIsNone(args.draft_tokens)
        self.assertEqual((ctl.MODEL, ctl.DEFAULTS['max_model_len']), ('RedHatAI/Qwen3.8-27B-INT4', 65536))
        # MTP defaults on exactly where the registry declares it (Qwen3.8), off elsewhere (gemma-4).
        registry = {ctl.MODEL: {'revision': 'a' * 40, 'dialect': 'qwen35',
                                'cuda_deployment': {'prequantized': True, 'supports_mtp': True}},
                    'google/gemma-4-31B-it-qat-w4a16-ct': {'revision': 'b' * 40, 'dialect': 'gemma4',
                                                           'cuda_deployment': {'prequantized': True}}}
        with patch.object(ctl, 'registered_models', return_value=registry):
            self.assertEqual(ctl.deployment_options(args)['speculative_config'],
                             {'method': 'mtp', 'num_speculative_tokens': ctl.MTP_DRAFT_TOKENS})
            gemma = parser.parse_args(['start', '--model', 'google/gemma-4-31B-it-qat-w4a16-ct'])
            self.assertIsNone(ctl.deployment_options(gemma)['speculative_config'])
            off = parser.parse_args(['switch', '--speculative', 'off'])
            self.assertIsNone(ctl.deployment_options(off)['speculative_config'])
            with self.assertRaises(ValueError):  # draft tokens on a model whose default is off
                ctl.deployment_options(parser.parse_args(
                    ['start', '--model', 'google/gemma-4-31B-it-qat-w4a16-ct', '--draft-tokens', '2']))
        self.assertNotIn('_worker', parser.format_help())

    def test_internal_workers_log_once(self):
        output = io.StringIO()
        with patch.object(ctl, 'ECHO', False), contextlib.redirect_stdout(output):
            ctl.event('worker line')
        self.assertEqual(output.getvalue(), '')
        self.assertIn('worker line', (ctl.STATE/'deploy.log').read_text())


class TailscaleInstallTests(unittest.TestCase):
    def bundle(self, directory):
        name = f'tailscale_{vm.TAILSCALE_VERSION}_amd64'
        archive = Path(directory)/'bundle.tgz'
        with tarfile.open(archive, 'w:gz') as tar:
            for binary in ('tailscale', 'tailscaled'):
                data = ('#!/bin/sh\n# ' + binary + '\n').encode()
                info = tarfile.TarInfo(f'{name}/{binary}'); info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return archive

    def install(self, directory, digest):
        archive = self.bundle(directory)
        def fake_run(*args, **kwargs):
            self.assertIn(f'tailscale_{vm.TAILSCALE_VERSION}_amd64.tgz', args[2])
            Path(args[args.index('-o') + 1]).write_bytes(archive.read_bytes())
        with patch.object(vm, 'PRIVATE', Path(directory)), patch.object(vm, 'run', side_effect=fake_run), \
             patch.object(vm.platform, 'machine', return_value='x86_64'), \
             patch.object(vm, 'TAILSCALE_SHA256', digest(archive)):
            vm.install_tailscale()

    def test_pinned_digest_gates_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, '校验和'):
                self.install(directory, lambda archive: '0' * 64)
            self.assertFalse((Path(directory)/'bin/tailscaled').exists())
            self.install(directory, vm.sha256_of)
            for binary in ('tailscale', 'tailscaled'):
                self.assertEqual((Path(directory)/'bin'/binary).stat().st_mode & 0o777, 0o700)
            with patch.object(vm, 'PRIVATE', Path(directory)):
                self.assertEqual(vm.tailscaled_command('sock')[0], str(Path(directory)/'bin/tailscaled'))


REAL_REGISTRY = ctl.registered_models  # captured before any test patches it


class RegistryTests(unittest.TestCase):
    def test_default_model_is_registered_in_the_real_config(self):
        # Every other test patches the registry; a bare `deploy.sh start` reads this file.
        registry = REAL_REGISTRY()
        self.assertIn(ctl.MODEL, registry)
        self.assertIs(registry[ctl.MODEL]['cuda_deployment']['supports_mtp'], True)
        # D27 roster: exactly the two prequantized CUDA checkpoints (D25), gemma without MTP.
        self.assertEqual(set(registry), {'google/gemma-4-31B-it-qat-w4a16-ct', 'RedHatAI/Qwen3.8-27B-INT4'})
        for entry in registry.values():
            self.assertIs(entry['cuda_deployment']['prequantized'], True)
        self.assertNotIn('supports_mtp', registry['google/gemma-4-31B-it-qat-w4a16-ct']['cuda_deployment'])


if __name__ == '__main__':
    unittest.main()
