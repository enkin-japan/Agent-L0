"""Sequential HTTP validation of the already-running engine; never loads GPU weights."""
import importlib.util,json,sys,time,urllib.request,subprocess,hashlib,threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
APP=Path('/content/agent-demo'); PRIVATE=Path('/content/agent-demo-private')
sys.path.insert(0,str(APP/'backend'))
spec=importlib.util.spec_from_file_location('deploy_helper',PRIVATE/'staging/351c16ce3e43/demo_vm.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
values,_=helper.service_environment(); key=values['INFER_API_KEY']
from framework.dialects.qwen35 import Qwen35Dialect
from transformers import AutoTokenizer
model=APP/'backend/models/Qwen3.8-27B-INT4'
tok=AutoTokenizer.from_pretrained(model,local_files_only=True)
dialect=Qwen35Dialect.create(tok)
base='http://127.0.0.1:14161'
def get(path):return helper.json_get(base+path,key)
health=get('/health'); print('HEALTH',json.dumps(health),flush=True)
assert health['model_repo']=='RedHatAI/Qwen3.8-27B-INT4'
assert health['model_revision']=='91bd022d5b49442a868bc35008f6c21e1860edfa'
assert get('/metrics')['active_generations']==0
processes=subprocess.check_output(['nvidia-smi','--query-compute-apps=pid,process_name,used_gpu_memory','--format=csv,noheader'],text=True).strip().splitlines()
assert len(processes)==1
print('SINGLE_GPU_PROCESS',processes,flush=True)
prose='青瓷工作台是一款本地优先的桌面助手，支持检索文档、整理笔记、生成报告和逐字显示回复。请写十章详细使用指南，每章至少一百字，包含操作例子。直接输出正文。'
code='请用 Python 实现线程安全的 LRU 缓存，支持容量上限、TTL、get、put、delete、clear和命中率统计。给出完整标准库代码、unittest测试和使用例子，不要省略实现。'
records=['以下是虚构的桌面任务记录。']+[f'记录{i:04d}：资料整理；耗时{17+i%70}分钟；优先级{i%4}；状态待复核；说明：已归档来源与过程笔记，下一步核对结果并补充缺失材料。' for i in range(400)]
ending='请综合记录写详细工作流改进报告，至少十二段，每段包含问题、改进方法、验证办法，不逐条复述。'
def prompt(text,tools=None):return dialect.build_prompt([{'role':'user','content':text}],tools=tools,enable_thinking=False)
lo,hi=1,len(records)
while lo<hi:
 mid=(lo+hi)//2
 if len(tok.encode(prompt('\n'.join(records[:mid])+ending),add_special_tokens=False))<13700:lo=mid+1
 else:hi=mid
cases={'prose':prompt(prose),'code':prompt(code),'long':prompt('\n'.join(records[:lo])+ending)}
results=[]
busy_event=threading.Event()
def generate(name,prompt_text,warmup=False,max_tokens=256):
 before=get('/metrics')['speculative_decode']; events=[];text=[];kind=None;done=False;cache=None
 body={'prompt':prompt_text,'sampling':{'temperature':0,'min_p':0.05,'max_tokens':max_tokens,'repetition_penalty':1.0},'features':{'want_delta':True}}
 req=urllib.request.Request(base+'/v1/generate',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
 start=time.perf_counter()
 with urllib.request.urlopen(req,timeout=300) as response:
  for raw in response:
   line=raw.decode().strip()
   if line.startswith('event:'):kind=line[6:].strip()
   if not line.startswith('data:'):continue
   payload=json.loads(line[5:])
   if kind=='meta':
    cache=payload['cache_id']
    if name=='busy_guard':busy_event.set()
   elif kind=='error':raise RuntimeError('generation error: '+payload.get('message',''))
   elif kind=='done':done=True
   elif kind=='chunk':
    text.append(payload['text']);n=payload['generation_tokens']
    if n and (not events or n>events[-1]['tokens']):events.append({'s':time.perf_counter()-start,'tokens':n})
 assert done and len(events)>1,'missing streamed terminal/interval'
 if cache:
  delete=urllib.request.Request(base+'/v1/cache/'+cache,method='DELETE',headers={'Authorization':'Bearer '+key})
  with urllib.request.urlopen(delete,timeout=20):pass
 after=get('/metrics')['speculative_decode'];delta=None
 if before['status']==after['status']=='observed':delta={k:after[k]-before[k] for k in ('draft_tokens','accepted_tokens','num_drafts')}
 spec_on=health['inference_options']['speculative_config'] is not None
 if spec_on:assert delta and delta['draft_tokens']>0 and delta['accepted_tokens']>0,'MTP not demonstrated by positive counters'
 row={'case':name,'warmup':warmup,'prompt_tokens':len(tok.encode(prompt_text,add_special_tokens=False)),
      'output_tokens':events[-1]['tokens'],'ttft_s':events[0]['s'],
      'decode_tps':(events[-1]['tokens']-events[0]['tokens'])/(events[-1]['s']-events[0]['s']),
      'metrics_before':before,'metrics_after':after,'spec_delta':delta,'done':done,'text':''.join(text),'events':events}
 results.append(row);print('RESULT',json.dumps({k:v for k,v in row.items() if k not in ('text','events')},ensure_ascii=False),flush=True)
 return row
for name,p in cases.items():
 for index in range(4):generate(name,p,warmup=index==0)
with ThreadPoolExecutor(max_workers=1) as pool:
 future=pool.submit(generate,'busy_guard',prompt(prose+'输出尽可能完整。'),False,1024)
 assert busy_event.wait(60)
 try:
  helper.check_idle(key)
 except RuntimeError as exc:
  assert 'busy or activity is unknown' in str(exc)
  print('BUSY_GUARD_REJECTED',flush=True)
 else:raise AssertionError('switch did not reject active inference')
 future.result(timeout=180)
tool={'name':'lookup_note','description':'Lookup a note by its exact title','parameters':{'type':'object','properties':{'title':{'type':'string'}},'required':['title']}}
row=generate('tool',prompt('请调用 lookup_note 查找标题为青瓷验收的笔记。不要猜测笔记内容。',[tool]),max_tokens=192)
parsed=dialect.parse_response(row['text']);assert any(c.get('name')=='lookup_note' and c.get('arguments',{}).get('title')=='青瓷验收' for c in parsed['tool_calls']),parsed
print('TOOL_PARSE',json.dumps(parsed,ensure_ascii=False),flush=True)
# The same running engine also serves the OpenAI facade; no second model is loaded.
request=urllib.request.Request(base+'/v1/chat/completions',data=json.dumps({'messages':[{'role':'user','content':'仅回复：青瓷连接正常'}],'max_tokens':64,'temperature':0,'stream':False}).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
with urllib.request.urlopen(request,timeout=120) as response:oai=json.load(response)
assert '青瓷' in oai['choices'][0]['message']['content']
print('OPENAI_FACADE',json.dumps(oai,ensure_ascii=False),flush=True)
receipt={'health':health,'results':results,'tool_parse':parsed,'facade_ok':True,'rag':helper.json_get('http://127.0.0.1:14162/health'),'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=name,memory.used,memory.total','--format=csv,noheader'],text=True)}
label='mtp3' if health['inference_options']['speculative_config'] else 'off'
(PRIVATE/f'validation-{label}.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
print('VALIDATION_COMPLETE',label,flush=True)
