import type {Page} from '@playwright/test';
import type {DisplayFeedState} from '../src/DisplayFeedSetup';

export const aircraft='FA-18C_hornet';
export const snapshot={server:{dry_run:true},health:{aircraft,session:'feed-session',status:'Live',telemetry_fresh:true,telemetry_age_s:.1,displays_fresh:true,display_age_s:.1,dcs_running:true},cockpit:{aircraft,status:'Available',displays:[2,3,4].map(id=>({id,label:'Display',status:'Available',age_s:.1,elements:[{name:'Scratchpad',value:id===2?'EXPORTED TEXT FALLBACK':`DISPLAY ${id} TEXT`}]}))}};
export function status():DisplayFeedState{return {enabled:true,can_edit:true,status:'Ready',detail:'Capture eligible',revision:'revision-one',aircraft,panels:['left_mdi','right_mdi','ampcd'].map(id=>({id:id as 'left_mdi'|'right_mdi'|'ampcd',label:id,status:'Ready',width:512,height:512})),setup:{status:'active',detail:'Configured export regions',monitors:[{id:'primary',name:'Main display',kind:'physical',primary:true,x:0,y:0,width:1920,height:1080},{id:'exports',name:'Export display',kind:'physical',primary:false,x:1920,y:0,width:1920,height:1080}],can_apply:false,can_restore:false,dcs_running:true}};}
export async function mock(page:Page,feed=status(),overview=false){
  const control={feed,frames:[] as Array<{panel:string;revision:string;at:number}>,writes:[] as Array<{path:string;body:any}>,connections:[] as string[],httpFrames:0,silent:false,noFrames:false,stale:false,wrongRevision:false,duplicate:false,split:false,badDimensions:false,badJpeg:false,malformed:false,oversized:false,closeStream:false,streamStatus:200};
  const jpeg=await page.evaluate(()=>{const canvas=document.createElement('canvas');canvas.width=512;canvas.height=512;const context=canvas.getContext('2d')!;context.fillStyle='#043714';context.fillRect(0,0,512,512);context.fillStyle='#76fa98';context.font='40px monospace';context.fillText('TEST STREAM',90,240);return canvas.toDataURL('image/jpeg').split(',')[1];});
  await page.exposeFunction('__displayFixture',()=>({...control,frames:undefined,writes:undefined,connections:undefined,jpeg}));
  await page.exposeFunction('__displayRecord',(item:{panels?:string;panel?:string;revision?:string;at?:number})=>{if(item.panels!==undefined)control.connections.push(item.panels);else control.frames.push({panel:item.panel!,revision:item.revision!,at:item.at!});});
  await page.addInitScript(fixture=>{
    const win=window as any;win.__feedSnapshot=fixture;win.__createdUrls=[];win.__revokedUrls=[];win.__streamActive=0;win.__streamMaxActive=0;
    const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);URL.createObjectURL=blob=>{const url=create(blob);win.__createdUrls.push(url);return url;};URL.revokeObjectURL=url=>{win.__revokedUrls.push(url);revoke(url);};
    const cancelReader=ReadableStreamDefaultReader.prototype.cancel;ReadableStreamDefaultReader.prototype.cancel=function(reason){const cancelled=cancelReader.call(this,reason);return win.__stallStreamCancel?new Promise<void>(()=>{}):cancelled;};
    win.EventSource=class{onmessage:any;timer:any;constructor(){this.timer=setInterval(()=>this.onmessage?.({data:JSON.stringify(win.__feedSnapshot)}),250);}addEventListener(){}close(){clearInterval(this.timer);}};
    const nativeFetch=window.fetch.bind(window),sequences:Record<string,number>={};
    window.fetch=async(input,options)=>{
      const url=new URL(String(input),location.href);if(url.pathname!=='/api/display-feed/stream')return nativeFetch(input,options);
      const initial=await win.__displayFixture(),panels=(url.searchParams.get('panels')??'').split(',');await win.__displayRecord({panels:panels.join(',')});
      if(win.__stallStreamFetch>0){win.__stallStreamFetch--;return new Promise<Response>(()=>{});}
      if(initial.streamStatus!==200)return new Response('Unavailable',{status:initial.streamStatus});
      let ended=false,timer:ReturnType<typeof setInterval>,busy=false,streamController:ReadableStreamDefaultController<Uint8Array>;
      const encoder=new TextEncoder();win.__streamActive++;win.__streamMaxActive=Math.max(win.__streamMaxActive,win.__streamActive);
      const finish=()=>{if(ended)return;ended=true;clearInterval(timer);win.__streamActive--;try{streamController.close();}catch{}};
      const body=new ReadableStream<Uint8Array>({start(controller){streamController=controller;const tick=async()=>{if(busy||ended)return;busy=true;try{const current=await win.__displayFixture();if(ended)return;if(current.closeStream){finish();return;}if(current.silent)return;
        const send=(item:unknown)=>{const line=encoder.encode(JSON.stringify(item)+'\n');if(current.split){const middle=Math.floor(line.length/2);controller.enqueue(line.slice(0,middle));controller.enqueue(line.slice(middle));}else controller.enqueue(line);};
        send({type:'status',enabled:current.feed.enabled,status:current.feed.status,detail:current.feed.detail,revision:current.feed.revision,aircraft:current.feed.aircraft,panels:current.feed.panels});
        if(current.malformed){controller.enqueue(encoder.encode('{broken\n'));return;}if(current.oversized){controller.enqueue(encoder.encode('x'.repeat(3*1024*1024+1)));return;}
        if(current.noFrames||!current.feed.enabled||current.feed.status!=='Ready')return;
        for(const panel of panels){const revision=current.wrongRevision?'old-revision':current.feed.revision;sequences[panel]=(sequences[panel]??0)+(current.duplicate?0:1);send({type:'frame',panel,revision,sequence:sequences[panel],captured_epoch:(Date.now()-(current.stale?10000:0))/1000,width:current.badDimensions?1024:512,height:512,jpeg_base64:current.badJpeg?'AAAA':current.jpeg});await win.__displayRecord({panel,revision,at:Date.now()});}
      }finally{busy=false;}};void tick();timer=setInterval(()=>void tick(),125);},cancel(){finish();}});
      options?.signal?.addEventListener('abort',()=>{if(!win.__ignoreStreamAbort)finish();},{once:true});if(options?.signal?.aborted)finish();
      return new Response(body,{status:200,headers:{'Content-Type':'application/x-ndjson'}});
    };
  },snapshot);
  await page.route('**/api/state',route=>route.fulfill({json:snapshot}));
  await page.route('**/api/remote**',route=>route.fulfill({json:{status:'Ready',mode:'preview',armed:false,controller_connected:false,can_edit:true,busy:false,aircraft,panels:['left_mdi','right_mdi','ampcd'].map(id=>({id,label:id,buttons:Array.from({length:20},(_,index)=>({id:`hornet.${id}.pb.${index+1}`,name:`${id} PB ${index+1}`,label:String(index+1),available:true}))})),workflows:[],scripts:[]}}));
  await page.route('**/api/display-feed**',async route=>{const req=route.request(),url=new URL(req.url());if(url.pathname.endsWith('frame.jpg')){control.httpFrames++;return route.fulfill({status:500});}if(req.method()==='POST'){const body=req.postDataJSON();control.writes.push({path:url.pathname,body});if(url.pathname.endsWith('/plan')){const selected=control.feed.setup.monitors.find(item=>item.id===body.monitor_id);control.feed.setup.plan={id:'plan-one',monitor_id:body.monitor_id,summary:'Use the selected display for three DCS panels.',main_monitor:'Main display',export_monitor:selected?.name,export_kind:selected?.kind??'unknown',physical_monitors_preserved:selected?.kind==='virtual',window:{width:3840,height:1080},panels:{left_mdi:{x:1920,y:0,width:512,height:512}},changes:[{field:'monitorSetup',before:'1 Screen',after:'DCS Companion Displays'}],restart_required:true};}if(url.pathname.endsWith('/enabled'))control.feed.enabled=body.enabled;}return route.fulfill({json:control.feed});});
  page.on('request',req=>{if(req.method()==='POST'&&!new URL(req.url()).pathname.startsWith('/api/display-feed'))control.writes.push({path:new URL(req.url()).pathname,body:req.postDataJSON()});});
  await page.goto('/');await page.getByRole('button',{name:'Remote',exact:true}).last().click();if(!overview)await page.getByRole('button',{name:'Left DDI',exact:true}).click();return control;
}
export async function showFeed(page:Page){const feed=page.getByRole('region',{name:'Left DDI display view'});await feed.scrollIntoViewIfNeeded();return feed;}
export async function setup(page:Page){await page.locator('.display-feed-setup>summary').click();}
