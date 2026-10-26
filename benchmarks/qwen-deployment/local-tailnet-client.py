"""Use the product RemoteBackend across Tailnet, without starting a local service/model."""
from pathlib import Path
import importlib.util,json,os,sys,time
ROOT=Path('/Volumes/enkin/project/Agent');sys.path.insert(0,str(ROOT/'backend'))
spec=importlib.util.spec_from_file_location('deployment',ROOT/'colab/demo.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)
# Existing authorized deployment helper consumes the service key without printing it.
credentials=deploy.env_credentials([ROOT/'.env.local'])
os.environ['INFER_API_KEY']=credentials['INFER_API_KEY'];credentials.clear()
from framework.utils.model_selection import discover_remote_model
from framework.backends.remote.backend import RemoteBackend,guard_model_identity
from framework.dialects import get_dialect_class
infer_host=os.environ.get('AGENT_INFER_HOST') or (sys.argv[1] if len(sys.argv)>1 else '<infer-host>')
base=f'http://{infer_host}:14161'
repo,revision,entry=discover_remote_model(base)
assert repo=='RedHatAI/Qwen3.8-27B-INT4' and entry['dialect']=='qwen35'
backend=RemoteBackend(None,base);cache=backend.new_cache()
try:
 health=backend._session.get(base+'/health',timeout=10).json()
 guard_model_identity(health,{'repo':repo,'revision':revision},base)
 before=backend._session.get(base+'/metrics',timeout=10).json()['speculative_decode']
 dialect=get_dialect_class(entry['dialect']).create(None)
 prompt=dialect.build_prompt([{'role':'user','content':'写一个 Python 函数将摄氏温度转换成华氏温度，配一段简短说明。'}],enable_thinking=False)
 chunks=[];start=time.perf_counter();events=[]
 for chunk in backend.stream_generate(prompt,cache=cache,sampling={'temperature':0,'max_tokens':160},features={'want_delta':True}):
  chunks.append(chunk.text);events.append({'s':time.perf_counter()-start,'tokens':chunk.generation_tokens})
 parsed=dialect.parse_response(''.join(chunks));assert 'def ' in parsed['text'] and len(events)>1
 after=backend._session.get(base+'/metrics',timeout=10).json()['speculative_decode']
 assert before['status']==after['status']=='observed'
 delta={k:after[k]-before[k] for k in ('draft_tokens','accepted_tokens','num_drafts')}
 assert delta['draft_tokens']>0 and delta['accepted_tokens']>0
 result={'repo':repo,'revision':revision,'dialect':entry['dialect'],'base_url':base,
         'client':'product RemoteBackend','metrics_before':before,'metrics_after':after,'spec_delta':delta,
         'stream_events':events,'parsed':parsed,'local_model_loaded':False,'service_started':False}
 (ROOT/'logs/qwen-deployment/local-tailnet-client.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 print('TAILNET_PRODUCT_CLIENT_VERIFIED',repo,len(events),delta)
finally:
 backend.release_cache(cache);backend.shutdown()
