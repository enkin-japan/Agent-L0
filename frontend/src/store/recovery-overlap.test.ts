import { beforeEach,afterEach,it,expect,vi } from 'vitest'
let data:Map<string,string>,releaseHistory:(r:Response)=>void,releaseSubmit:(r:Response)=>void
let streams:Array<ReadableStreamDefaultController<Uint8Array>>,submitted:number,hold:boolean
const oldTurn={status:'success',prompt:'T1',response:[{role:'assistant',content:'old answer'}]}
beforeEach(()=>{
 vi.resetModules();releaseHistory=undefined!;releaseSubmit=undefined!;data=new Map();streams=[];submitted=0;hold=true
 vi.stubGlobal('localStorage',{getItem:(k:string)=>data.get(k)??null,setItem:(k:string,v:string)=>data.set(k,v)})
 vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
  if(url.endsWith('/history')){if(hold){hold=false;return new Promise<Response>(r=>{releaseHistory=r})}return Response.json({status:true,count:0,turns:[],replay_after:0})}
  if(url.endsWith('/turn'))return Response.json({turn:null,last_seq:2})
  if(url.endsWith('/turns/last'))return Response.json({status:true,remaining_turns:0})
  if(url.endsWith('/turns')){submitted++;return new Promise<Response>(r=>{releaseSubmit=r})}
  if(url.endsWith('/task'))return Response.json({active:false})
  if(url.includes('/events?'))return new Response(new ReadableStream<Uint8Array>({start(c){streams.push(c);init?.signal?.addEventListener('abort',()=>{try{c.close()}catch { /* synthetic stream already closed */ }},{once:true})}}))
  throw Error(url)
 }))
})
afterEach(()=>{for(const c of streams){try{c.close()}catch { /* synthetic stream already closed */ }}vi.unstubAllGlobals()})
async function store(){const {useChatStore:s}=await import('./useChatStore');s.setState({activeId:'A',conversations:['A','B'].map(id=>({id,title:id,updatedAt:0,turnCount:0,historyLoaded:false,messages:[]}))});return s}
async function emit(seq:number,id:string,type:string,payload:object){streams[0].enqueue(new TextEncoder().encode(`data: ${JSON.stringify({seq,turn_id:id,type,data:payload})}\n\n`));await new Promise(r=>setTimeout(r,5))}
it('historical T1 terminal preserves pending T2 identity and renders input once',async()=>{
 const s=await store();const loading=s.getState().loadHistory('A');const sending=s.getState().sendMessage('T2',[])
 releaseHistory(Response.json({status:true,count:1,turns:[oldTurn],replay_after:0}));await loading;await vi.waitFor(()=>expect(streams.length).toBe(1))
 await emit(2,'old','turn_ended',{...oldTurn,turn_index:0})
 const preserved=Boolean(JSON.parse(data.get('macagent-outbox-v1')??'{}').A)
 releaseSubmit(Response.json({status:true,turn_id:'new',seq:3},{status:202}));await sending
 await emit(3,'new','turn_queued',{prompt:'T2'});await emit(4,'new','turn_ended',{status:'success',prompt:'T2',response:[{role:'assistant',content:'new answer'}],turn_index:1})
 expect({preserved,count:s.getState().conversations[0].messages.filter(m=>m.role==='user'&&m.content==='T2').length}).toEqual({preserved:true,count:1})
})
it('edited resend does not survive ABA after DELETE but before history returns',async()=>{
 const s=await store();const updating=s.getState().updateLastTurn('old edit')
 await vi.waitFor(()=>expect(releaseHistory).toBeDefined())
 s.getState().selectConversation('B');await s.getState().loadHistory('B')
 s.getState().selectConversation('A');await s.getState().loadHistory('A')
 releaseHistory(Response.json({status:true,count:0,turns:[],replay_after:0}))
 await vi.waitFor(()=>expect(submitted).toBeGreaterThanOrEqual(0));await new Promise(r=>setTimeout(r,10))
 if(submitted)releaseSubmit(Response.json({status:true,turn_id:'wrong',seq:3},{status:202}))
 await updating;expect(submitted).toBe(0)
})

for (const samePrompt of [false,true]) for (const earlyTerminal of [false,true]) it(`old queued/terminal cannot claim pending submission, samePrompt=${samePrompt} early=${earlyTerminal}`,async()=>{
 const s=await store()
 const text=samePrompt?'T1':'T2'
 const loading=s.getState().loadHistory('A'),sending=s.getState().sendMessage(text,[])
 const pending=JSON.parse(data.get('macagent-outbox-v1')!).A
 releaseHistory(Response.json({status:true,count:1,turns:[oldTurn],replay_after:0}));await loading;await vi.waitFor(()=>expect(streams.length).toBe(1))
 await emit(1,'old','turn_queued',{prompt:'T1',client_turn_id:'old-key'})
 await emit(2,'old','turn_ended',{...oldTurn,turn_index:0})
 expect(JSON.parse(data.get('macagent-outbox-v1')!).A.key).toBe(pending.key)
 expect(s.getState().generating.A).toBe(true)
 expect(s.getState().conversations[0].messages.filter(m=>m.turnIndex===undefined&&m.role==='user')).toHaveLength(1)
 if(earlyTerminal){
  await emit(3,'new','turn_queued',{prompt:text,client_turn_id:pending.key})
  await emit(4,'new','turn_ended',{status:'success',prompt:text,response:[{role:'assistant',content:'new answer'}],turn_index:1})
 }
 releaseSubmit(Response.json({status:true,turn_id:'new',seq:3},{status:202}));await sending
 if(!earlyTerminal){await emit(3,'new','turn_queued',{prompt:text,client_turn_id:pending.key});await emit(4,'new','turn_ended',{status:'success',prompt:text,response:[{role:'assistant',content:'new answer'}],turn_index:1})}
 expect(s.getState().conversations[0].messages.filter(m=>m.turnIndex===1&&m.role==='user')).toHaveLength(1)
 expect(s.getState().conversations[0].messages.filter(m=>m.turnIndex===undefined&&m.role==='user')).toHaveLength(0)
 expect(JSON.parse(data.get('macagent-outbox-v1')!).A).toBeUndefined()
})
it('pending submission failure after old replay preserves its retry key',async()=>{
 const s=await store(),loading=s.getState().loadHistory('A'),sending=s.getState().sendMessage('T2',[])
 const pending=JSON.parse(data.get('macagent-outbox-v1')!).A
 releaseHistory(Response.json({status:true,count:1,turns:[oldTurn],replay_after:0}));await loading;await vi.waitFor(()=>expect(streams.length).toBe(1))
 await emit(1,'old','turn_queued',{prompt:'T1',client_turn_id:'old-key'});await emit(2,'old','turn_ended',{...oldTurn,turn_index:0})
 releaseSubmit(Response.json({}, {status:500}));await sending
 expect(JSON.parse(data.get('macagent-outbox-v1')!).A.key).toBe(pending.key)
 expect(s.getState().conversations[0].messages.filter(m=>m.role==='user'&&m.turnIndex===undefined)).toHaveLength(0)
})
