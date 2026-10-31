"""CPU-only deployment contract tests. All keys, sessions and CLI calls are synthetic."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('ctl_switch_test', SOURCE / 'compute_ctl.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
GEMMA = 'google/gemma-4-31B-it-qat-w4a16-ct'
QWEN = 'RedHatAI/Qwen3.8-27B-INT4'
REVISION = '91bd022d5b49442a868bc35008f6c21e1860edfa'


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.stack.enter_context(patch.object(ctl, 'STATE', self.root / 'state'))
        self.stack.enter_context(patch.object(ctl, 'ROOT', self.root))
        self.stack.enter_context(patch.object(ctl, 'registered_models', return_value={
            GEMMA: {'revision': 'a' * 40, 'dialect': 'gemma4', 'cuda_deployment': {'prequantized': True}},
            QWEN: {'revision': REVISION, 'dialect': 'qwen35', 'cuda_deployment': {'prequantized': True, 'supports_mtp': True}},
        }))
        files = ['config.yaml', 'devctl.sh', 'backend/scripts/fetch_model.py',
                 'backend/framework/inference/runtime.py', 'inference/server.py',
                 'rag/compute_server.py', 'colab/compute_vm.py', 'colab/compute_fetch.py',
                 'colab/compute_common.py']
        for name in files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic source')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '--', *files], check=True)
        # No vm_layout: the shape of a lease written before the rename. Most tests here
        # therefore also prove the live legacy VM stays switchable under the new code.
        self.lease = {'session': 'agent-demo-0123456789ab', 'endpoint': 'owned-endpoint',
                      'state': 'ready', 'deadline': time.time() + 3600, 'retain': True,
                      'model': GEMMA, 'options': {'model': GEMMA},
                      'tailnet': {'id': 'synthetic-node'}, 'supervision': {'state': 'active', 'pid': 123}}
        ctl.save_state(self.lease)
        self.stack.enter_context(patch.object(ctl, 'server_sessions', return_value={
            self.lease['endpoint']: self.lease['session']}))
        self.stack.enter_context(patch.object(ctl, 'event'))
        # No switch step may inspect even a synthetic credential file.
        self.stack.enter_context(patch.object(ctl, 'read_private', side_effect=AssertionError('credential read')))
        self.stack.enter_context(patch.object(ctl, 'env_credentials', side_effect=AssertionError('credential read')))
        self.stack.enter_context(patch.object(ctl, 'stop_owned', side_effect=AssertionError('VM release')))

    def args(self, **updates):
        values = dict(model=QWEN, speculative='mtp', draft_tokens=None, attention_backend='auto',
                      kv_cache_dtype='auto', quantization='none', foreground=False, retain=False,
                      bootstrap_timeout=60, stall_timeout=30, poll_interval=2)
        return argparse.Namespace(**{**values, **updates})

    def reserve(self, **updates):
        args = self.args(**updates)
        data = ctl.reserve_switch(args)
        args.job = data['switch']['job']
        return args, data

    def ready(self, data):
        options = data['switch']['requested_options']
        return {'job': data['switch']['job'], 'phase': 'ready', 'activity_seq': 1,
                'model': options['model'], 'revision': options['revision'],
                'effective_options': options, 'host': '100.64.0.1'}

    def worker(self, args, data, progress=None, upload=None):
        uploads, kicks = [], []
        def fake_cli(*words, **kwargs):
            self.assertEqual(words[0], 'upload')
            if upload:
                upload(words)
            uploads.append((words, Path(words[3]).read_bytes()))
            return subprocess.CompletedProcess(words, 0, '')
        def fake_remote(session, code, **kwargs):
            self.assertEqual(session, self.lease['session'])
            if 'COMPUTE_SWITCH_STAGE_READY' in code:
                return 'COMPUTE_SWITCH_STAGE_READY'
            self.assertIn('COMPUTE_SWITCH_STARTED', code)
            self.assertIn("(private / 'stop.requested').exists()", code)
            app = ctl.common.layout_of(ctl.current())['app']
            self.assertIn(app + '/backend/.venv/bin/python', code)
            kicks.append(code)
            return 'COMPUTE_SWITCH_STARTED'
        with patch.object(ctl, 'cli', side_effect=fake_cli), patch.object(ctl, 'remote', side_effect=fake_remote), \
             patch.object(ctl, 'poll_switch_progress', side_effect=progress or (lambda *a: self.ready(data))):
            result = ctl.run_switch(args)
        return result, uploads, kicks

    def test_qwen_mtp_effective_options_and_off_defaults(self):
        actual = ctl.deployment_options(self.args(draft_tokens=5))
        self.assertEqual(actual['speculative_config'], {'method': 'mtp', 'num_speculative_tokens': 5})
        self.assertEqual(actual['attention_backend'], 'TRITON_ATTN')
        self.assertEqual(actual['kv_cache_dtype'], 'bfloat16')
        self.assertEqual(actual['revision'], REVISION)
        actual = ctl.deployment_options(self.args(model=GEMMA, speculative='off'))
        self.assertIsNone(actual['speculative_config'])
        self.assertEqual((actual['attention_backend'], actual['kv_cache_dtype']), ('auto', 'auto'))
        actual = ctl.deployment_options(self.args(attention_backend='flashinfer', kv_cache_dtype='fp8'))
        self.assertEqual((actual['attention_backend'], actual['kv_cache_dtype']), ('FLASHINFER', 'fp8'))

    def test_invalid_mtp_is_rejected_before_any_cli_or_state_change(self):
        for updates in [dict(model=GEMMA), dict(draft_tokens=0), dict(draft_tokens=6),
                        dict(speculative='off', draft_tokens=2), dict(attention_backend='bad'),
                        dict(kv_cache_dtype='bad')]:
            with self.subTest(updates=updates), patch.object(ctl, 'cli') as cli:
                with self.assertRaises(ValueError):
                    ctl.reserve_switch(self.args(**updates))
                cli.assert_not_called()
                self.assertEqual(ctl.current(), self.lease)

    def test_switch_reuses_lease_and_stages_sources_without_credentials(self):
        args, data = self.reserve(draft_tokens=5)
        result, uploads, kicks = self.worker(args, data)
        self.assertEqual(result, 0)
        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(uploads), 5)
        options = json.loads(uploads[-1][1])
        self.assertEqual(options['draft_tokens'], 5)
        self.assertEqual(options['speculative_config']['num_speculative_tokens'], 5)
        self.assertEqual(options['staging_dir'], '/content/agent-demo-private/staging/' + args.job)
        for (words, payload) in uploads:
            self.assertEqual(words[1:3], ('-s', self.lease['session']))
            self.assertNotIn('credentials', words[-1])
            self.assertNotIn(b'synthetic-private-api-key', payload)
        self.assertTrue(all('/staging/' in words[-1] for words, _ in uploads[:4]))
        final = ctl.current()
        self.assertEqual(final['state'], 'ready')
        self.assertEqual(final['model'], QWEN)
        for field in ('session', 'endpoint', 'deadline', 'retain', 'tailnet', 'supervision'):
            self.assertEqual(final[field], self.lease[field])

    def test_identity_mismatch_and_ambiguous_session_never_reserve(self):
        for assignments in [{'owned-endpoint': 'unrelated'},
                            {'different': self.lease['session']},
                            {'owned-endpoint': self.lease['session'], 'duplicate': self.lease['session']}]:
            with patch.object(ctl, 'server_sessions', return_value=assignments):
                with self.assertRaises(ctl.LeaseStopped):
                    ctl.reserve_switch(self.args())
                self.assertEqual(ctl.current(), self.lease)

    def assert_old_timed_supervisor_rejected(self, capability, retain_option):
        old = {**self.lease, 'retain': False,
               'supervision': {**self.lease['supervision'], 'switch_aware': capability}}
        # Synthetic file replacement models an old launcher's recorded lease.
        ctl._write_state(old)
        with patch.object(ctl, 'cli') as cli, patch.object(ctl, 'remote') as remote, \
             patch.object(ctl, 'server_sessions', return_value={old['endpoint']: old['session']}) as inventory, \
             patch.object(ctl, 'make_archive') as archive:
            with self.assertRaisesRegex(ValueError, 'switch-aware supervisor.*stop normally.*updated launcher'):
                ctl.reserve_switch(self.args(retain=retain_option))
            self.assertEqual(ctl.current(), old)
            cli.assert_not_called()
            remote.assert_not_called()
            inventory.assert_not_called()
            archive.assert_not_called()

    def test_old_timed_supervisor_rejects_switch_without_side_effects(self):
        for capability in (None, False, 'true'):
            for retain_option in (False, True):
                with self.subTest(capability=capability, retain_option=retain_option):
                    self.assert_old_timed_supervisor_rejected(capability, retain_option)

    def test_old_retained_supervisor_accepts_switch_and_preserves_lease(self):
        self.assertNotIn('switch_aware', self.lease['supervision'])
        args, data = self.reserve()
        self.worker(args, data)
        final = ctl.current()
        self.assertEqual(final['state'], 'ready')
        self.assertTrue(final['retain'])
        self.assertEqual(final['deadline'], self.lease['deadline'])
        self.assertEqual(final['supervision'], self.lease['supervision'])

    def test_new_timed_supervisor_accepts_switch_without_extending_lease(self):
        lease = {**self.lease, 'retain': False,
                 'supervision': {**self.lease['supervision'], 'switch_aware': True}}
        ctl._write_state(lease)
        args, data = self.reserve()
        self.worker(args, data)
        final = ctl.current()
        self.assertEqual(final['state'], 'ready')
        self.assertFalse(final['retain'])
        self.assertEqual(final['deadline'], lease['deadline'])
        self.assertEqual(final['supervision'], lease['supervision'])

    def test_supervisor_capability_rechecked_inside_reservation_lock(self):
        lease = {**self.lease, 'retain': False,
                 'supervision': {**self.lease['supervision'], 'switch_aware': True}}
        ctl._write_state(lease)
        def identity_change(*a):
            ctl._write_state({**lease, 'supervision': {'state': 'active', 'pid': 456}})
        with patch.object(ctl, 'verify_switch_owner', side_effect=identity_change):
            with self.assertRaisesRegex(ValueError, 'switch-aware supervisor'):
                ctl.reserve_switch(self.args())
        final = ctl.current()
        self.assertEqual(final['state'], 'ready')
        self.assertNotIn('switch', final)
        self.assertFalse(final['retain'])

    def test_old_supervisor_guard_mutation_is_detected_without_changing_sources(self):
        import hashlib
        before = hashlib.sha256(Path(ctl.__file__).read_bytes()).hexdigest()
        # Only the isolated fixture and in-memory guard are mutated. The original
        # behavioral test must reject a launcher that omits this compatibility gate.
        with patch.object(ctl, 'require_switch_aware_supervision', return_value=None):
            with self.assertRaises(AssertionError):
                self.assert_old_timed_supervisor_rejected(None, False)
        self.assertEqual(hashlib.sha256(Path(ctl.__file__).read_bytes()).hexdigest(), before)

    def test_failed_switch_retains_vm_and_can_be_retried(self):
        args, data = self.reserve()
        with self.assertRaisesRegex(RuntimeError, 'remote switch failed'):
            self.worker(args, data, lambda *a: {'job': args.job, 'phase': 'failed'})
        self.assertEqual(ctl.current()['state'], 'switch-failed')
        self.assertTrue(ctl.current()['retain'])
        self.assertEqual(ctl.current()['endpoint'], self.lease['endpoint'])
        new = ctl.reserve_switch(self.args(speculative='off'))
        self.assertNotEqual(new['switch']['job'], args.job)

    def test_stop_during_upload_prevents_remote_start_and_late_ready(self):
        args, data = self.reserve()
        def stopping(words):
            current = ctl.current()
            ctl.save_state({**current, 'state': 'stopping'})
        result, uploads, kicks = self.worker(args, data, upload=stopping)
        self.assertEqual(result, 1)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(kicks, [])
        self.assertEqual(ctl.current()['state'], 'stopping')
        with self.assertRaises(ctl.LeaseStopped):
            ctl.switch_update(data['session'], args.job, state='ready')

    def test_stop_after_remote_ready_always_wins(self):
        args, data = self.reserve()
        def late_ready(*a):
            ctl.save_state({**ctl.current(), 'state': 'stopped'})
            return self.ready(data)
        result, _, _ = self.worker(args, data, late_ready)
        self.assertEqual(result, 1)
        self.assertEqual(ctl.current()['state'], 'stopped')
        self.assertEqual(ctl.current()['model'], GEMMA)

    def test_stop_priority_mutation_is_detected_without_changing_sources(self):
        import hashlib
        before = hashlib.sha256(Path(ctl.__file__).read_bytes()).hexdigest()
        def unguarded_update(session, job, **updates):
            data = ctl.current()
            data.update(updates)
            ctl._write_state(data)
            return data
        # Isolated synthetic state; remove precisely the stop/job CAS and prove
        # the behavioral acceptance test rejects the late READY resurrection.
        with patch.object(ctl, 'switch_update', side_effect=unguarded_update):
            with self.assertRaises(AssertionError):
                self.test_stop_after_remote_ready_always_wins()
        self.assertEqual(hashlib.sha256(Path(ctl.__file__).read_bytes()).hexdigest(), before)

    def test_ready_requires_model_revision_and_effective_options_identity(self):
        args, data = self.reserve()
        bad = self.ready(data)
        bad['effective_options'] = {**bad['effective_options'], 'speculative_config': None}
        with self.assertRaisesRegex(RuntimeError, 'identity did not match'):
            self.worker(args, data, lambda *a: bad)
        self.assertEqual(ctl.current()['state'], 'switch-failed')

    def test_old_supervisor_snapshot_cannot_undo_switch_or_retention(self):
        old = dict(self.lease, retain=False)
        args, data = self.reserve()
        ctl.save_state(old)
        self.assertEqual(ctl.current()['state'], 'switching')
        self.assertEqual(ctl.current()['switch']['job'], args.job)
        self.worker(args, data)
        ctl.save_state({**self.lease, 'supervision': {'state': 'active', 'pid': 456}})
        final = ctl.current()
        self.assertEqual(final['state'], 'ready')
        self.assertEqual(final['model'], QWEN)
        self.assertTrue(final['retain'])
        self.assertEqual(final['supervision']['pid'], 456)

    def test_monitor_does_not_probe_during_switch_or_failure_and_respects_stop(self):
        args, data = self.reserve()
        for state in ('switching', 'switch-failed'):
            ctl.switch_update(data['session'], args.job, state=state)
            def stopped(*a):
                ctl.save_state({**ctl.current(), 'state': 'stopping'})
            with patch.object(ctl, 'check_compute') as health, patch.object(ctl.time, 'sleep', side_effect=stopped):
                ctl.monitor_ready({**self.lease, 'supervision_options': {'health_interval': 1}})
                health.assert_not_called()
                self.assertIs(ctl.current()['supervision']['switch_aware'], True)
            if state == 'switching':
                ctl._write_state({**data, 'state': 'switch-failed'})

    def test_source_archive_includes_only_allowed_nonignored_untracked_sources(self):
        new = self.root / 'backend/framework/core/new_runtime.py'
        new.parent.mkdir(parents=True)
        new.write_text('untracked runtime')
        ignored = self.root / 'backend/framework/core/ignored.py'
        ignored.write_text('synthetic-private-api-key')
        (self.root / '.gitignore').write_text('ignored.py\n')
        (self.root / '.env.local').write_text('synthetic-private-api-key')
        archive = self.root / 'source.tar.gz'
        ctl.make_archive(self.root, archive)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            self.assertIn('backend/framework/core/new_runtime.py', names)
            self.assertNotIn('backend/framework/core/ignored.py', names)
            self.assertNotIn('.env.local', names)
            self.assertTrue(all(b'synthetic-private-api-key' not in tar.extractfile(n).read() for n in names))
        linked = self.root / 'backend/framework/link'
        linked.symlink_to(self.root / 'backend/framework/core', target_is_directory=True)
        # git does not traverse untracked symlink directories; a tracked link is refused too.
        subprocess.run(['git', '-C', str(self.root), 'add', '--', 'backend/framework/link'], check=True)
        # Direct .py links cannot conceal secrets even when their target is inside the repo.
        secret_link = self.root / 'inference/private.py'
        secret_link.symlink_to(self.root / '.env.local')
        with self.assertRaisesRegex(ValueError, '拒绝链接'):
            ctl.make_archive(self.root, archive)

    def test_job_progress_cannot_complete_from_another_switch(self):
        args, data = self.reserve()
        stale = {**self.ready(data), 'job': 'f' * 12}
        with patch.object(ctl, 'remote', return_value='COMPUTE_SWITCH_PROGRESS ' + json.dumps(stale)):
            with self.assertRaisesRegex(RuntimeError, 'another job'):
                ctl.poll_switch_progress(data, args.job, time.time() + 60)

    def test_background_worker_reads_every_option_from_the_lease(self):
        args = self.args(minutes=120, auth_key=Path('/synthetic/auth'), hostname='synthetic',
                         max_model_len=32768, gpu_memory=.72, health_interval=45, draft_tokens=4)
        child = unittest.mock.Mock(pid=123)
        child.wait.side_effect = subprocess.TimeoutExpired('synthetic', 1)
        ctl.save_state({**self.lease, 'state': 'stopped'})
        with patch.object(ctl.subprocess, 'Popen', return_value=child) as spawn:
            self.assertEqual(ctl.start_background(args, 99), 0)
        lease = ctl.current()
        # The worker command carries identity only; no option can be forgotten in transit.
        self.assertEqual(spawn.call_args.args[0][2:],
                         ['_worker', '--lease-fd', '99', '--job', lease['session']])
        self.assertEqual(spawn.call_args.kwargs['pass_fds'], (99,))
        self.assertEqual(lease['options']['speculative_config'],
                         {'method': 'mtp', 'num_speculative_tokens': 4})
        self.assertEqual((lease['hostname'], lease['auth_key']), ('synthetic', '/synthetic/auth'))
        self.assertEqual(lease['supervision_options'], {'poll_interval': 2, 'health_interval': 45})
        self.assertEqual(lease['vm_layout'], ctl.common.LAYOUT)
        self.assertTrue(lease['session'].startswith(ctl.common.SESSION_PREFIX))

    def test_new_layout_lease_switches_under_new_vm_paths(self):
        ctl._write_state({**self.lease, 'vm_layout': ctl.common.LAYOUT})
        args, data = self.reserve()
        result, uploads, kicks = self.worker(args, data)
        self.assertEqual(result, 0)
        options = json.loads(uploads[-1][1])
        self.assertEqual(options['staging_dir'], '/content/agent-compute-private/staging/' + args.job)
        self.assertEqual(options['vm_layout'], ctl.common.LAYOUT)
        self.assertIn('/content/agent-compute/backend/.venv/bin/python', kicks[0])
        self.assertTrue(all(words[-1].startswith('/content/agent-compute-private/')
                            for words, _ in uploads))


if __name__ == '__main__':
    unittest.main()
