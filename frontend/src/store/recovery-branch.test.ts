import { beforeEach,afterEach,it,expect,vi } from 'vitest'
let release: (r: Response)=>void
let streams: Array<{sid:string, signal?:AbortSignal|null, c:ReadableStreamDefaultController<Uint8Array>}>
beforeEach(()=>{
 vi.resetModules();streams=[]
 vi.stubGlobal('localStorage',{getItem:()=>null,setItem:()=>{}})
 vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
  const sid=url.split('/chat/')[1]?.split('/')[0]
  if(url.endsWith('/branch'))return new Promise<Response>(r=>{release=r})
  if(url.endsWith('/history'))return Response.json({status:true,count:0,turns:[],replay_after:0})
  if(url.endsWith('/turn'))return Response.json({turn:null,last_seq:0})
  if(url.includes('/events?'))return new Response(new ReadableStream<Uint8Array>({start(c){streams.push({sid,signal:init?.signal,c});init?.signal?.addEventListener('abort',()=>{try{c.close()}catch { /* synthetic stream already closed */ }},{once:true})}}))
  throw Error(url)
 }))
})
afterEach(()=>{for(const s of streams){try{s.c.close()}catch { /* synthetic stream already closed */ }}vi.unstubAllGlobals()})
for(const aba of [false,true])for(const accepted of [false,true])it(`late branch aba=${aba} accepted=${accepted}`,async()=>{
 const {useChatStore:store}=await import('./useChatStore')
 store.setState({activeId:'A',conversations:['A','B'].map(id=>({id,title:id,updatedAt:0,turnCount:0,historyLoaded:false,messages:[]}))})
 await store.getState().loadHistory('A');await vi.waitFor(()=>expect(streams.length).toBe(1))
 const old=store.getState().branchFrom('A',0)
 store.getState().selectConversation('B');await store.getState().loadHistory('B');await vi.waitFor(()=>expect(streams.length).toBe(2))
 if(aba){store.getState().selectConversation('A');await store.getState().loadHistory('A');await vi.waitFor(()=>expect(streams.length).toBe(3))}
 const before=store.getState()
 release(Response.json({status:accepted,session_id:accepted?'branch-new':null,response:'old-A failure',turns:1}))
 await old
 expect(store.getState()).toBe(before)
 expect(streams.at(-1)?.signal?.aborted).toBe(false)
})
