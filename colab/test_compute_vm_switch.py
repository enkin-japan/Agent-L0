"""CPU-only compute_vm switch contract; synthetic credentials and mocked services/GPU."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack
import yaml

spec = importlib.util.spec_from_file_location('vm_switch_test', Path(__file__).with_name('compute_vm.py'))
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


class VMSwitchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.private = self.root/'private'; self.private.mkdir()
        self.app = self.root/'app'; self.app.mkdir()
        for name,value in [('PRIVATE',self.private),('APP',self.app),('JOB',None),('SECRET_VALUES',[])]:
            self.stack.enter_context(patch.object(vm,name,value))
        self.job = 'a'*12
        self.stage = self.private/'staging'/self.job; self.stage.mkdir(parents=True)
        self.options = dict(model='test/Qwen',revision='b'*40,quantization='none',
                            speculative_config={'method':'mtp','num_speculative_tokens':3},
                            attention_backend='TRITON_ATTN',kv_cache_dtype='bfloat16',
                            max_model_len=32768,gpu_memory_utilization=.85,bootstrap_timeout=60,
                            staging_dir=str(self.stage),
                            vm_layout={'private':str(self.private),'app':str(self.app)})
        self.options_path = self.private/f'switch-{self.job}.json'
        self.options_path.write_text(json.dumps(self.options))
        self.config = {'inference':{'cuda':{},'model_registry':[
            {'repo':'test/Qwen','revision':'b'*40,'cuda_deployment':{'supports_mtp':True}}]},
            'rag':{'preserve':'sentinel'}}
        (self.app/'config.yaml').write_text(yaml.safe_dump(self.config))
        (self.app/'.env.local').write_text("INFER_API_KEY=synthetic-infer-key\nRAG_COMPUTE_API_KEY=synthetic-rag-key\nINFER_SPEC_CONFIG=off\nINFER_KV_DTYPE=auto\n")
        (self.private/'tailnet.json').write_text(json.dumps({'dns_name':'test.example.ts.net'}))
        self.archive({'config.yaml':yaml.safe_dump(self.config),'inference/server.py':'new infer',
                      'rag/compute_server.py':'must not overlay'})
        (self.app/'rag').mkdir(); (self.app/'rag/compute_server.py').write_text('existing rag')
        self.rag = dict(status='healthy',device='cpu',tier='vl-2b',embed_repo='embedding',rerank_repo='rerank')
        self.infer = dict(status='ok',service='macagent-inference',model_repo='test/Qwen',model_revision='b'*40,
                          inference_options={k:self.options[k] for k in ['speculative_config','attention_backend','kv_cache_dtype']})
        self.calls=[]; self.progress=[]
        self.stack.enter_context(patch.object(vm,'progress',side_effect=lambda phase,**kw:self.progress.append((phase,kw))))
        self.stack.enter_context(patch.object(vm,'log_line'))
        self.run = self.stack.enter_context(patch.object(vm,'run',side_effect=lambda *a,**kw:self.calls.append(a)))
        self.gpu = self.stack.enter_context(patch.object(vm.subprocess,'check_output',return_value=''))
        def get(url,key=None,payload=None):
            if '14162' in url:return dict(self.rag)
            if url.endswith('/metrics'):return {'active_generations':0,'active_gpu_work':0}
            return dict(self.infer)
        self.get = self.stack.enter_context(patch.object(vm,'json_get',side_effect=get))

    def archive(self, files):
        with tarfile.open(self.stage/'source.tar.gz','w:gz') as tar:
            for name,value in files.items():
                data=value.encode(); info=tarfile.TarInfo(name); info.size=len(data)
                tar.addfile(info,io.BytesIO(data))

    def switch(self):
        vm.switch_main(str(self.options_path),self.job)

    def test_switch_reuses_services_preserves_rag_and_secrets_applies_options(self):
        self.switch()
        self.assertEqual(self.calls[0][1:],(str(self.app/'devctl.sh'),'stop','infer'))
        self.assertEqual(self.calls[-1][1:],(str(self.app/'devctl.sh'),'start','infer'))
        self.assertFalse(any('rag-compute' in call for call in self.calls))
        self.assertEqual((self.app/'rag/compute_server.py').read_text(),'existing rag')
        self.assertEqual(yaml.safe_load((self.app/'config.yaml').read_text())['rag'],self.config['rag'])
        self.assertEqual(yaml.safe_load((self.app/'config.yaml').read_text())['inference']['cuda']['max_num_seqs'],1)
        values,_=vm.service_environment()
        self.assertEqual(values['INFER_API_KEY'],'synthetic-infer-key')
        self.assertEqual(json.loads(values['INFER_SPEC_CONFIG']),self.options['speculative_config'])
        self.assertEqual(self.progress[-1][0],'ready')
        self.assertEqual(self.gpu.call_count,2)

    def test_busy_unknown_and_foreign_service_refuse_stop(self):
        for metrics,service in [({'active_generations':0,'active_gpu_work':1},'macagent-inference'),({'active_generations':0},'macagent-inference'),({},'macagent-inference'),({'active_gpu_work':0},'foreign')]:
            with self.subTest(metrics=metrics,service=service):
                with patch.object(vm,'json_get',side_effect=lambda url,*a:metrics if url.endswith('/metrics') else {'service':service}):
                    with self.assertRaises(RuntimeError):vm.check_idle('synthetic')
        self.run.assert_not_called()

    def test_foreign_gpu_refuses_download_and_start(self):
        self.gpu.return_value='999, foreign, 1200 MiB'
        with self.assertRaisesRegex(RuntimeError,'GPU still occupied'):self.switch()
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('ready',[p for p,_ in self.progress])

    def test_explicit_stop_prevents_any_service_change(self):
        (self.private/'stop.requested').touch()
        with self.assertRaisesRegex(RuntimeError,'stop requested'):self.switch()
        self.run.assert_not_called()

    def test_stop_during_download_prevents_new_inference(self):
        def run(*args,**kw):
            self.calls.append(args)
            if 'compute_fetch.py' in args[1]:(self.private/'stop.requested').touch()
        self.run.side_effect=run
        with self.assertRaisesRegex(RuntimeError,'stop requested'):self.switch()
        self.assertFalse(any('start' in call for call in self.calls))

    def test_identity_or_options_mismatch_cannot_report_ready(self):
        self.infer['inference_options']={**self.infer['inference_options'],'speculative_config':None}
        with self.assertRaisesRegex(RuntimeError,'mismatch'):self.switch()
        self.assertNotIn('ready',[p for p,_ in self.progress])

    def test_remote_lock_excludes_orphaned_switch(self):
        with (self.private/'inference-switch.lock').open('a') as lock:
            vm.fcntl.flock(lock,vm.fcntl.LOCK_EX|vm.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.switch()
        self.run.assert_not_called()

    def test_archive_traversal_and_wrong_staging_rejected(self):
        self.archive({'../outside':'bad'})
        with self.assertRaisesRegex(ValueError,'unsafe'):self.switch()
        self.run.assert_not_called()
        self.options['staging_dir']=str(self.root)
        self.options_path.write_text(json.dumps(self.options))
        with self.assertRaisesRegex(ValueError,'staging'):self.switch()

    def test_mtp_off_clears_previous_configuration(self):
        self.options['speculative_config']=None
        vm.configure_model(self.options)
        self.assertIsNone(yaml.safe_load((self.app/'config.yaml').read_text())['inference']['cuda']['speculative_config'])

    def test_ready_checks_rag_identity(self):
        old_get=self.get.side_effect; count=0
        def get(url,*args):
            nonlocal count
            result=old_get(url,*args)
            if '14162' in url:
                count+=1
                if count==2:result['embed_repo']='different'
            return result
        self.get.side_effect=get
        with self.assertRaisesRegex(RuntimeError,'RAG identity'):self.switch()
        self.assertNotIn('ready',[p for p,_ in self.progress])

    def test_switch_refuses_options_for_another_vm_layout(self):
        self.options['vm_layout']={'private':'/content/agent-compute-private','app':'/content/agent-compute'}
        self.options_path.write_text(json.dumps(self.options))
        with self.assertRaisesRegex(ValueError,'VM 布局'):self.switch()
        self.run.assert_not_called()

    def test_layout_is_chosen_only_from_known_private_directories(self):
        common=vm.common
        self.assertEqual(vm.known_layout_for(common.LEGACY_LAYOUT['private']),common.LEGACY_LAYOUT)
        self.assertEqual(vm.known_layout_for(common.LAYOUT['private']+'/'),common.LAYOUT)
        with self.assertRaises(ValueError):vm.known_layout_for('/content/elsewhere')

    def test_rag_health_noise_does_not_hide_stalled_switch(self):
        (self.app/'logs').mkdir()
        (self.app/'logs/rag-compute.log').write_text('GET /health 200 OK\n')
        (self.app/'logs/infer.log').write_text('model loading\n')
        with patch.object(vm,'JOB',self.job), patch.object(vm,'log_line') as log:
            vm.service_lines({})
        self.assertEqual(log.call_args_list, [unittest.mock.call('infer.log | model loading')])
