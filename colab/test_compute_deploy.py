"""CPU subprocess tests with a fake Colab executable; never use live credentials."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parent.resolve()


def load(name):
    spec=importlib.util.spec_from_file_location(name,SOURCE/(name+'.py'))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class DeployProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.state=self.root/'.colab-compute'
        files=['config.yaml','devctl.sh','inference/server.py','rag/compute_server.py',
               'backend/scripts/fetch_model.py','backend/framework/fake.py',
               'colab/compute_vm.py','colab/compute_fetch.py','colab/compute_common.py',
               'colab/requirements-compute.lock']
        for name in files:
            path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('synthetic')
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','-C',str(self.root),'add','--',*files],check=True)
        self.key=self.root/'key'; self.key.write_text('tskey-auth-synthetic'); self.key.chmod(0o600)
        envfile=self.root/'.env.local'
        envfile.write_text('INFER_API_KEY=synthetic-infer-key\nRAG_COMPUTE_API_KEY=synthetic-rag-key\n'); envfile.chmod(0o600)
        self.runner=self.root/'runner.py'
        self.runner.write_text('''import importlib.util,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('compute_ctl',%r)
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.ROOT=Path(__file__).parent; m.STATE=m.ROOT/'.colab-compute'; m.__file__=__file__
m.registered_models=lambda: {'google/gemma-4-31B-it-qat-w4a16-ct':{'revision':'a'*40,'cuda_deployment':{'prequantized':True}},'RedHatAI/Qwen3.8-27B-INT4':{'revision':'91bd022d5b49442a868bc35008f6c21e1860edfa','dialect':'qwen35','cuda_deployment':{'prequantized':True,'supports_mtp':True}}}
import os,time
m.check_start_resources=lambda *args: (time.sleep(float(os.environ.get('SYNTHETIC_PREFLIGHT_DELAY','0'))) or 'example.ts.net')
m.verify_dns=lambda *args: None
import os,contextlib
if os.environ.get('SYNTHETIC_DISABLE_CLEANUP_SIGNALS'): m.cleanup_signals=contextlib.nullcontext
if os.environ.get('SYNTHETIC_DISABLE_ALLOCATION_SIGNALS'): m.allocation_signals=contextlib.nullcontext
raise SystemExit(m.main())
''' % str(SOURCE/'compute_ctl.py'))
        binpath=self.root/'bin'; binpath.mkdir()
        fake=binpath/'colab'
        fake.write_text('#!'+sys.executable+'\n'+'''import json,os,sys,time
from pathlib import Path
root=Path(os.environ['SYNTHETIC_COLAB_ROOT']); db=root/'assignment.json'
args=sys.argv[1:]; cmd=args[0]
with (root/'commands.jsonl').open('a') as trace: trace.write(json.dumps(args)+'\\n')
if cmd=='new':
 (root/'allocation-started').touch()
 time.sleep(float(os.environ.get('SYNTHETIC_ALLOCATION_DELAY','0')))
 db.write_text(json.dumps({'session':args[args.index('-s')+1]}))
elif cmd=='sessions':
 if db.exists(): print('['+json.loads(db.read_text())['session']+'] owned-endpoint | Hardware: A100')
 else: print('No active sessions found on server.')
elif cmd=='stop':
 time.sleep(.4); db.unlink(missing_ok=True)
elif cmd=='exec':
 code=Path(args[args.index('-f')+1]).read_text()
 if 'COMPUTE_GPU_VERIFIED' in code: print('COMPUTE_GPU_VERIFIED A100 40960')
 elif 'COMPUTE_BOOTSTRAP_STARTED' in code: print('COMPUTE_BOOTSTRAP_STARTED')
 elif 'COMPUTE_SWITCH_STAGE_READY' in code: print('COMPUTE_SWITCH_STAGE_READY')
 elif 'COMPUTE_SWITCH_STARTED' in code:
  (root/'switch-started').touch(); print('COMPUTE_SWITCH_STARTED')
 elif 'COMPUTE_SWITCH_PROGRESS' in code:
  data=json.loads((root/'switch-options.json').read_text())
  job=data['staging_dir'].rsplit('/',1)[-1]
  if os.environ.get('SYNTHETIC_SWITCH_WAIT'):
   print('COMPUTE_SWITCH_PROGRESS '+json.dumps({'job':job,'phase':'downloading-model','activity_seq':1}))
  else: print('COMPUTE_SWITCH_PROGRESS '+json.dumps({'job':job,'phase':'ready','activity_seq':1,'model':data['model'],'revision':data['revision'],'effective_options':data,'host':'100.64.0.1'}))
 elif 'COMPUTE_PROGRESS' in code: print('COMPUTE_PROGRESS '+json.dumps({'phase':'ready','host':'100.64.0.1','activity_seq':1}))
 elif 'COMPUTE_LOGOUT_OK' in code: print('COMPUTE_LOGOUT_OK')
 elif 'COMPUTE_HEALTH' in code: print('COMPUTE_HEALTH {"infer":true,"rag":true}')
elif cmd=='upload':
 if args[-1].endswith('.json') and '/switch-' in args[-1]:
  (root/'switch-options.json').write_bytes(Path(args[-2]).read_bytes())
elif cmd!='usage': raise SystemExit(2)
'''); fake.chmod(0o700)
        self.env=dict(os.environ,PATH=str(binpath)+os.pathsep+os.environ['PATH'],SYNTHETIC_COLAB_ROOT=str(self.root))
        self.processes=[]; self.workers=[]; self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        for pid in self.workers:
            try: os.kill(pid,signal.SIGTERM)
            except ProcessLookupError: pass

    def launch(self,*args):
        process=subprocess.Popen([sys.executable,str(self.runner),*args],env=self.env,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,text=True)
        self.processes.append(process)
        return process

    def until(self,predicate,seconds=10):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.03)
        self.fail('condition timed out')

    def data(self):
        p=self.state/'current.json'
        return json.loads(p.read_text()) if p.exists() else {}

    def log_contains(self,text):
        p=self.state/'deploy.log'
        return p.exists() and text in p.read_text()

    def test_repeated_terminal_ctrl_c_cannot_interrupt_vm_release(self):
        proc=self.launch('start','--foreground','--auth-key',str(self.key))
        self.until(lambda: self.data().get('state')=='ready')
        os.killpg(proc.pid,signal.SIGINT)
        self.until(lambda: self.log_contains('正在请求 Colab 释放 VM'))
        os.killpg(proc.pid,signal.SIGINT)
        proc.communicate(timeout=8)
        self.assertEqual(proc.returncode,130)
        self.assertEqual(self.data()['state'],'stopped')
        self.assertFalse((self.root/'assignment.json').exists())
        self.assertTrue(self.log_contains('已确认 VM 从 Colab 服务端移除'))

    def test_repeated_signal_protection_mutation_is_detected(self):
        self.env['SYNTHETIC_DISABLE_CLEANUP_SIGNALS']='1'
        with self.assertRaises(AssertionError):
            self.test_repeated_terminal_ctrl_c_cannot_interrupt_vm_release()

    def test_background_stop_during_preflight_prevents_allocation(self):
        self.env['SYNTHETIC_PREFLIGHT_DELAY']='2.5'
        launcher=self.launch('start','--auth-key',str(self.key))
        launcher.communicate(timeout=4)
        self.assertEqual(launcher.returncode,0)
        self.workers.append(self.data()['worker_pid'])
        stopper=self.launch('stop'); stopper.communicate(timeout=4)
        self.assertEqual(stopper.returncode,0)
        time.sleep(2)
        self.assertEqual(self.data()['state'],'stopped')
        self.assertFalse((self.root/'allocation-started').exists())
        self.assertFalse((self.root/'assignment.json').exists())

    def test_ctrl_c_during_allocation_waits_for_identity_then_releases(self):
        self.env['SYNTHETIC_ALLOCATION_DELAY']='1.5'
        proc=self.launch('start','--foreground','--auth-key',str(self.key))
        self.until(lambda:(self.root/'allocation-started').exists())
        os.killpg(proc.pid,signal.SIGINT)
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode,130)
        self.assertEqual(self.data()['state'],'stopped')
        self.assertIn('endpoint',self.data())
        self.assertFalse((self.root/'assignment.json').exists())

    def test_allocation_signal_mutation_is_detected(self):
        self.env['SYNTHETIC_DISABLE_ALLOCATION_SIGNALS']='1'
        with self.assertRaises(AssertionError):
            self.test_ctrl_c_during_allocation_waits_for_identity_then_releases()

    def test_stop_during_background_allocation_waits_then_releases(self):
        self.env['SYNTHETIC_ALLOCATION_DELAY']='2'
        launcher=self.launch('start','--auth-key',str(self.key))
        self.until(lambda:(self.root/'allocation-started').exists())
        self.workers.append(self.data()['worker_pid'])
        stopper=self.launch('stop'); stopper.communicate(timeout=10)
        launcher.communicate(timeout=4)
        self.assertEqual(stopper.returncode,0)
        self.assertEqual(self.data()['state'],'stopped')
        self.assertFalse((self.root/'assignment.json').exists())

    def test_background_survives_terminal_close_and_log_viewer_ctrl_c(self):
        launcher=self.launch('start','--auth-key',str(self.key))
        self.until(lambda: self.data().get('state')=='ready')
        worker=self.data()['worker_pid']; self.workers.append(worker)
        self.assertNotEqual(os.getpgid(worker),launcher.pid)
        if launcher.poll() is None:
            os.killpg(launcher.pid,signal.SIGHUP)
        launcher.communicate(timeout=3)
        os.kill(worker,0)
        viewer=self.launch('logs','--follow')
        time.sleep(.1); os.killpg(viewer.pid,signal.SIGINT); viewer.communicate(timeout=3)
        self.assertTrue((self.root/'assignment.json').exists())
        self.assertEqual(self.data()['state'],'ready')
        stopper=self.launch('stop'); stopper.communicate(timeout=8)
        self.assertEqual(stopper.returncode,0)
        self.assertEqual(self.data()['state'],'stopped')
        self.assertFalse((self.root/'assignment.json').exists())
        # The background worker must also release its inherited lease lock.
        import fcntl
        def unlocked():
            with (self.state/'lease.lock').open('a') as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); return True
                except BlockingIOError: return False
        self.until(unlocked)

    def test_background_switch_reuses_retained_vm_while_watch_owns_lease(self):
        # Start on another model: waiting for the model field is how this test sees the switch land.
        starter=self.launch('start','--retain','--model','google/gemma-4-31B-it-qat-w4a16-ct',
                            '--auth-key',str(self.key))
        self.until(lambda:self.data().get('state')=='ready')
        starter.communicate(timeout=3)
        before=self.data(); self.workers.append(before['worker_pid'])
        switcher=self.launch('switch','--model','RedHatAI/Qwen3.8-27B-INT4',
                             '--speculative','mtp','--draft-tokens','5')
        switcher.communicate(timeout=4)
        self.assertEqual(switcher.returncode,0)
        self.until(lambda:self.data().get('model')=='RedHatAI/Qwen3.8-27B-INT4')
        after=self.data()
        self.assertEqual(after['state'],'ready')
        for field in ('session','endpoint','deadline','retain','worker_pid'):
            self.assertEqual(before[field],after[field])
        self.assertEqual(after['options']['speculative_config'],{'method':'mtp','num_speculative_tokens':5})
        self.assertEqual(after['options']['attention_backend'],'TRITON_ATTN')
        self.assertEqual(after['options']['kv_cache_dtype'],'bfloat16')
        trace=[json.loads(line) for line in (self.root/'commands.jsonl').read_text().splitlines()]
        self.assertEqual(sum(row[0]=='new' for row in trace),1)
        self.assertFalse(any(row[0]=='stop' for row in trace))
        self.assertNotIn('synthetic-infer-key',(self.state/'deploy.log').read_text())
        stopper=self.launch('stop'); stopper.communicate(timeout=8)
        self.assertEqual(stopper.returncode,0)

    def test_stop_during_background_switch_cannot_be_revived(self):
        self.env['SYNTHETIC_SWITCH_WAIT']='1'
        starter=self.launch('start','--retain','--model','google/gemma-4-31B-it-qat-w4a16-ct','--auth-key',str(self.key))
        self.until(lambda:self.data().get('state')=='ready')
        starter.communicate(timeout=3)
        self.workers.append(self.data()['worker_pid'])
        switcher=self.launch('switch','--model','RedHatAI/Qwen3.8-27B-INT4','--speculative','mtp')
        self.until(lambda:(self.root/'switch-started').exists())
        self.until(lambda:bool(self.data().get('switch',{}).get('worker_pid')))
        self.workers.append(self.data()['switch']['worker_pid'])
        switcher.communicate(timeout=4)
        self.assertEqual(switcher.returncode,0)
        stopper=self.launch('stop'); stopper.communicate(timeout=8)
        self.assertEqual(stopper.returncode,0)
        self.until(lambda:self.log_contains('切换已终止'))
        self.assertEqual(self.data()['state'],'stopped')
        self.assertFalse((self.root/'assignment.json').exists())
        self.assertEqual(self.data()['model'],'google/gemma-4-31B-it-qat-w4a16-ct')


class DeployProgressTests(unittest.TestCase):
    def assert_stream_redacted(self, vm):
        secret='synthetic-private-key-fragmented'
        with tempfile.TemporaryDirectory() as directory, patch.object(vm,'PRIVATE',Path(directory)), \
             patch.object(vm,'SECRET_VALUES',[secret]):
            vm.run(sys.executable,'-u','-c',
                   'import sys,time; sys.stdout.write(sys.argv[1][:12]); sys.stdout.flush(); time.sleep(.1); print(sys.argv[1][12:])',secret)
            text='\n'.join(line for _,line in vm.LOG_TAIL)
            self.assertNotIn(secret,text)
            self.assertIn('[REDACTED]',text)

    def test_stream_redacts_secrets_split_across_reads(self):
        self.assert_stream_redacted(load('compute_vm'))

    def test_stream_redaction_mutation_is_detected(self):
        vm=load('compute_vm')
        with patch.object(vm,'redact',side_effect=lambda s:s):
            with self.assertRaises(AssertionError): self.assert_stream_redacted(vm)

    def test_download_counts_payload_and_partials_not_metadata(self):
        fetch=load('compute_fetch')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'weights').write_bytes(b'x'*100)
            partial=root/'.cache/huggingface/download'; partial.mkdir(parents=True)
            (partial/'next.incomplete').write_bytes(b'x'*30)
            (partial/'irrelevant.metadata').write_bytes(b'x'*200)
            self.assertEqual(fetch.downloaded_bytes(root,{'weights':100,'next':50}),130)

    def test_subprocess_progress_is_visible_before_command_finishes(self):
        vm=load('compute_vm')
        with tempfile.TemporaryDirectory() as directory, patch.object(vm,'PRIVATE',Path(directory)):
            vm.progress('installing-dependencies')
            import threading
            worker=threading.Thread(target=lambda:vm.run(sys.executable,'-u','-c',
                'import time; print("first step complete",flush=True); time.sleep(.6); print("last step")'))
            worker.start()
            deadline=time.monotonic()+2
            while time.monotonic()<deadline and not vm.LOG_TAIL: time.sleep(.01)
            self.assertTrue(worker.is_alive())
            self.assertIn('first step complete',vm.LOG_TAIL[0][1])
            worker.join(timeout=3); self.assertFalse(worker.is_alive())

    def test_runtime_command_timeout_is_bounded(self):
        vm=load('compute_vm')
        with tempfile.TemporaryDirectory() as directory, patch.object(vm,'PRIVATE',Path(directory)):
            started=time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                vm.run(sys.executable,'-c','import time; time.sleep(30)',timeout=.1)
            self.assertLess(time.monotonic()-started,3)


if __name__=='__main__': unittest.main()
