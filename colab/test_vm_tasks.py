"""Run every VM task for real against temporary directories and fake system tools."""
import base64
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('compute_ctl_tasks', SOURCE / 'compute_ctl.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)


class TaskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.private = self.root / 'private'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        path = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH']})
        path.start()
        self.addCleanup(path.stop)

    def execute(self, name, params):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(compile(ctl.task_code(name, params), name, 'exec'), {'__name__': 'task'})
        return output.getvalue()

    def fake_tool(self, name, body):
        tool = self.bin / name
        tool.write_text('#!' + sys.executable + '\nimport sys\n' + body)
        tool.chmod(0o700)
        return tool

    def fake_nvidia(self, gpus='NVIDIA A100-SXM4-40GB, 40960', apps=''):
        self.fake_tool('nvidia-smi', f'''
query=sys.argv[1]
print({gpus!r} if query.startswith('--query-gpu') else {apps!r})
''')

    def test_gpu_check_accepts_only_one_idle_40gb_a100(self):
        params = {'private': str(self.private), 'gpu_name': ctl.common.GPU_NAME,
                  'memory_mib': ctl.common.GPU_MEMORY_MIB}
        self.fake_nvidia()
        self.assertIn('COMPUTE_GPU_VERIFIED', self.execute('gpu_check', params))
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):  # never reuse another deployment's directory
            self.execute('gpu_check', params)
        for gpus, apps in [('NVIDIA A100-SXM4-80GB, 81920', ''),
                           ('NVIDIA L4, 23034', ''),
                           ('NVIDIA A100-SXM4-40GB, 40960\nNVIDIA A100-SXM4-40GB, 40960', ''),
                           ('NVIDIA A100-SXM4-40GB, 40960', '1234')]:
            shutil.rmtree(self.private)
            self.fake_nvidia(gpus, apps)
            with self.subTest(gpus=gpus, apps=apps), self.assertRaises(SystemExit):
                self.execute('gpu_check', params)

    def test_progress_reports_file_or_placeholder_with_marker(self):
        self.private.mkdir()
        params = {'private': str(self.private), 'marker': 'COMPUTE_PROGRESS'}
        self.assertEqual(self.execute('progress', params).strip(),
                         'COMPUTE_PROGRESS {"phase": "starting"}')
        (self.private / 'progress.json').write_text('{"phase": "ready"}')
        self.assertEqual(ctl.marker_line(self.execute('progress', params), 'COMPUTE_PROGRESS'),
                         '{"phase": "ready"}')

    def test_logout_fences_starts_even_when_logout_fails(self):
        self.private.mkdir()
        for code, expected in [(0, 'COMPUTE_LOGOUT_OK'), (1, '')]:
            (self.private / 'stop.requested').unlink(missing_ok=True)
            tool = self.fake_tool('tailscale', f'raise SystemExit({code})')
            out = self.execute('logout', {'private': str(self.private), 'tailscale': str(tool)})
            self.assertEqual(out.strip(), expected)
            self.assertTrue((self.private / 'stop.requested').exists())

    def test_switch_stage_is_fresh_and_private(self):
        stage = self.private / 'staging' / ('a' * 12)
        self.assertIn('COMPUTE_SWITCH_STAGE_READY', self.execute('switch_stage', {'stage': str(stage)}))
        self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            self.execute('switch_stage', {'stage': str(stage)})

    def wait_for(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not path.exists():
            time.sleep(.02)
        self.assertTrue(path.exists(), path)

    def test_switch_kick_refuses_a_stopping_vm_and_launches_the_staged_helper(self):
        stage = self.private / 'staging' / 'job'
        stage.mkdir(parents=True)
        (stage / 'compute_vm.py').write_text(
            'import pathlib,sys; pathlib.Path(sys.argv[0]).with_name("ran").write_text(" ".join(sys.argv[1:]))')
        params = {'private': str(self.private), 'stage': str(stage), 'job': 'b' * 12,
                  'helper': 'compute_vm.py', 'options': '/synthetic/options.json',
                  'python': sys.executable}
        (self.private / 'stop.requested').touch()
        with self.assertRaises(SystemExit):
            self.execute('switch_kick', params)
        self.assertFalse((stage / 'ran').exists())
        (self.private / 'stop.requested').unlink()
        self.assertIn('COMPUTE_SWITCH_STARTED', self.execute('switch_kick', params))
        self.wait_for(stage / 'ran')
        self.assertEqual((stage / 'ran').read_text(),
                         'switch --options /synthetic/options.json --job ' + 'b' * 12)
        self.assertTrue((self.private / f'switch-{"b" * 12}.pid').exists())

    def test_bootstrap_kick_starts_helper_detached(self):
        self.private.mkdir()
        (self.private / 'credentials.json').write_text('{}')
        (self.private / 'compute_vm.py').write_text(
            'import pathlib,sys; pathlib.Path(sys.argv[0]).with_name("ran").touch()')
        self.fake_tool('python3', 'import os; os.execv(%r, [%r, *sys.argv[1:]])'
                       % (sys.executable, sys.executable))
        out = self.execute('bootstrap_kick', {'private': str(self.private), 'helper': 'compute_vm.py'})
        self.assertIn('COMPUTE_BOOTSTRAP_STARTED', out)
        self.wait_for(self.private / 'ran')
        self.assertEqual((self.private / 'credentials.json').stat().st_mode & 0o777, 0o600)

    def test_health_reports_each_service_independently(self):
        class Healthy(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Healthy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with socket.socket() as closed:
            closed.bind(('127.0.0.1', 0))
            dead_port = closed.getsockname()[1]
        out = self.execute('health', {'ports': {'infer': server.server_port, 'rag': dead_port}})
        self.assertEqual(ctl.marker_line(out, 'COMPUTE_HEALTH'), '{"infer": true, "rag": false}')

    def helper_vm(self):
        self.private.mkdir()
        app = self.root / 'app'
        (app / 'logs').mkdir(parents=True)
        for name in ('compute_vm.py', 'compute_common.py'):
            shutil.copy(SOURCE / name, self.private / name)
        (app / '.env.local').write_text(ctl.common.format_env(
            {'INFER_API_KEY': 'synthetic-infer-secret-value', 'INFER_MODEL_REPO': 'visible/model'}))
        return app

    def sanitized(self, out):
        return base64.b64decode(ctl.marker_line(out, 'COMPUTE_TEXT')).decode()

    def test_sanitized_log_tail_is_redacted_on_the_vm(self):
        app = self.helper_vm()
        (self.private / 'bootstrap.log').write_text(
            'x' * 200 + '\nkey synthetic-infer-secret-value model visible/model\n')
        out = self.execute('sanitized_text', {'mode': 'log', 'private': str(self.private),
                                              'app': str(app), 'helper': 'compute_vm.py',
                                              'log': 'bootstrap.log', 'max_bytes': 100})
        text = self.sanitized(out)
        self.assertNotIn('synthetic-infer-secret-value', out + text)
        self.assertIn('[REDACTED] model visible/model', text)
        self.assertNotIn('x' * 50, text)  # bounded tail starts at a line boundary

    def test_sanitized_text_drives_a_legacy_helper_without_use_layout(self):
        # Interface of the pre-rename demo_vm.py still deployed on old VMs: paths are
        # module constants, service_environment() registers secrets, redact(text).
        app = self.root / 'legacy-app'
        app.mkdir()
        (app / '.env.local').write_text("INFER_API_KEY='synthetic-legacy-secret'\n")
        self.private.mkdir()
        (self.private / 'demo_vm.py').write_text(f'''
from pathlib import Path
import shlex
APP = Path({str(app)!r})
SECRET_VALUES = []
def redact(text):
    for value in SECRET_VALUES:
        text = text.replace(value, '[REDACTED]')
    return text
def service_environment():
    values = {{}}
    for line in (APP / '.env.local').read_text().splitlines():
        name, _, raw = line.partition('=')
        values[name] = shlex.split(raw)[0]
    SECRET_VALUES.extend(v for k, v in values.items() if 'KEY' in k)
    return values, {{}}
''')
        (self.private / 'bootstrap.log').write_text('legacy synthetic-legacy-secret line\n')
        out = self.execute('sanitized_text', {'mode': 'log', 'private': str(self.private),
                                              'app': '/content/agent-demo', 'helper': 'demo_vm.py',
                                              'log': 'bootstrap.log', 'max_bytes': 1000})
        self.assertEqual(self.sanitized(out).strip(), 'legacy [REDACTED] line')

    def test_sanitized_diagnostic_is_created_redacted_once(self):
        app = self.helper_vm()
        (app / 'logs/infer.log').write_text('ROOT CAUSE synthetic-infer-secret-value\n')
        params = {'mode': 'diagnostic', 'private': str(self.private), 'app': str(app),
                  'helper': 'compute_vm.py'}
        text = self.sanitized(self.execute('sanitized_text', params))
        self.assertIn('ROOT CAUSE [REDACTED]', text)
        self.assertEqual((self.private / 'failure-diagnostic.log').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
