import {createContext, createElement, useContext, useEffect, useRef, useState} from 'react';
import type {ReactNode} from 'react';
import type {DisplayFeedPanelId, DisplayFeedState} from './DisplayFeedSetup';

export const STREAM_PANELS:DisplayFeedPanelId[]=['left_mdi','right_mdi','ampcd'];
export const MAX_LINE=3*1024*1024,MAX_JPEG=2*1024*1024,FRAME_MAX_AGE=2000;
export type DisplaySources=Partial<Record<DisplayFeedPanelId,'image'|'text'>>;
export interface StreamFrame {url:string;sequence:number;capturedAt:number;width:number;height:number;}
interface FrameRecord {type:'frame';panel:DisplayFeedPanelId;revision:string;sequence:number;captured_epoch:number;width:number;height:number;jpeg_base64:string;}
export interface StreamView {frames:Partial<Record<DisplayFeedPanelId,StreamFrame>>;status:string;detail:string;sources:DisplaySources;setSource:(panel:DisplayFeedPanelId,source:'image'|'text')=>void;}
export const DisplayStreamContext=createContext<StreamView>({frames:{},status:'Unavailable',detail:'Display stream unavailable.',sources:{},setSource:()=>{}});
export const useDisplayStreamView=()=>useContext(DisplayStreamContext);

/** Frame state belongs below the control workspace so image updates do not redraw it. */
export function DisplayStreamProvider({feed,usable,aircraft,session,requested,sources,setSource,children}:{feed?:DisplayFeedState;usable:boolean;aircraft?:string;session?:string;requested:DisplayFeedPanelId[];sources:DisplaySources;setSource:StreamView['setSource'];children:ReactNode}){
  const stream=useDisplayStream(feed,usable,aircraft,session,requested);
  return createElement(DisplayStreamContext.Provider,{value:{...stream,sources,setSource}},children);
}

/** The server emits bounded UTF-8 NDJSON, never an unbounded accumulated response. */
export class DisplayLineReader {
  private pending='';
  push(chunk:string){this.pending+=chunk;const lines:unknown[]=[];let end:number;while((end=this.pending.indexOf('\n'))>=0){if(end>MAX_LINE)throw new Error('Display stream record is too large.');const line=this.pending.slice(0,end);this.pending=this.pending.slice(end+1);if(line.trim())lines.push(JSON.parse(line));}if(this.pending.length>MAX_LINE)throw new Error('Display stream record is too large.');return lines;}
  finish(){if(this.pending.trim())throw new Error('The display stream ended during a frame.');}
}
export function frameRecord(value:unknown,revision:string,panels:ReadonlySet<string>,previous:number,now=Date.now()):FrameRecord|undefined {
  if(!value||typeof value!=='object')return;const item=value as FrameRecord;
  if(item.type!=='frame'||!panels.has(item.panel)||item.revision!==revision||!Number.isSafeInteger(item.sequence)||item.sequence<=previous||item.sequence<0||item.width!==512||item.height!==512||typeof item.jpeg_base64!=='string'||!item.jpeg_base64.length||item.jpeg_base64.length>Math.ceil(MAX_JPEG/3)*4)return;
  const captured=item.captured_epoch*1000;if(!Number.isFinite(captured)||captured<=0||now-captured>=FRAME_MAX_AGE||captured-now>2000)return;
  return item;
}
function jpegBlob(encoded:string){if(encoded.length%4!==0||!/^[A-Za-z0-9+/]+={0,2}$/.test(encoded))throw new Error('Invalid display image.');const decoded=atob(encoded);if(decoded.length<4||decoded.length>MAX_JPEG||decoded.charCodeAt(0)!==255||decoded.charCodeAt(1)!==216||decoded.charCodeAt(2)!==255)throw new Error('Invalid JPEG display image.');const bytes=new Uint8Array(decoded.length);for(let i=0;i<decoded.length;i++)bytes[i]=decoded.charCodeAt(i);return new Blob([bytes],{type:'image/jpeg'});}
function decodeImage(url:string){return new Promise<{width:number;height:number}>((resolve,reject)=>{const image=new Image(),timer=setTimeout(()=>{image.src='';reject(new Error('Display image decoding timed out.'));},1500);image.onload=()=>{clearTimeout(timer);resolve({width:image.naturalWidth,height:image.naturalHeight});};image.onerror=()=>{clearTimeout(timer);reject(new Error('The display image could not be decoded.'));};image.src=url;});}

// Recovery has its own deadline, even if a suspended browser does not settle
// its pending fetch or reader when the underlying request is aborted.
function abortable<T>(operation:Promise<T>,signal:AbortSignal){
  return new Promise<T>((resolve,reject)=>{
    const aborted=()=>{signal.removeEventListener('abort',aborted);reject(new DOMException('Display stream aborted.','AbortError'));};
    if(signal.aborted)aborted();else signal.addEventListener('abort',aborted,{once:true});
    operation.then(value=>{signal.removeEventListener('abort',aborted);resolve(value);},error=>{signal.removeEventListener('abort',aborted);reject(error);});
  });
}

export function useDisplayStream(feed:DisplayFeedState|undefined,usable:boolean,aircraft:string|undefined,session:string|undefined,requested:DisplayFeedPanelId[]){
  const [lifecycle,setLifecycle]=useState({visible:document.visibilityState!=='hidden',generation:0});
  const {visible}=lifecycle;
  const panelsKey=STREAM_PANELS.filter(id=>requested.includes(id)).join(',');
  const context=JSON.stringify([feed?.revision,feed?.enabled,feed?.aircraft,aircraft,session,usable,visible,lifecycle.generation,panelsKey]);
  const [state,setState]=useState<{context:string;frames:StreamView['frames'];status:string;detail:string}>({context:'',frames:{},status:'Waiting',detail:'Waiting for the display stream.'});
  const seen=useRef<{revision:string;sequences:Partial<Record<DisplayFeedPanelId,number>>}>({revision:'',sequences:{}});
  useEffect(()=>{
    const resume=()=>setLifecycle(previous=>({visible:document.visibilityState!=='hidden',generation:previous.generation+1}));
    const hide=()=>setLifecycle(previous=>({...previous,visible:false}));
    const shown=(event:PageTransitionEvent)=>{if(event.persisted)resume();};
    document.addEventListener('visibilitychange',resume);window.addEventListener('pagehide',hide);window.addEventListener('pageshow',shown);window.addEventListener('online',resume);
    return()=>{document.removeEventListener('visibilitychange',resume);window.removeEventListener('pagehide',hide);window.removeEventListener('pageshow',shown);window.removeEventListener('online',resume);};
  },[]);
  useEffect(()=>{
    let stopped=false,controller:AbortController|undefined,retry:ReturnType<typeof setTimeout>|undefined,heartbeat:ReturnType<typeof setTimeout>|undefined,attempt=0,failures=0,ready=false;
    const active=new Set(panelsKey.split(',').filter(Boolean)),revision=feed?.revision??'',urls=new Map<DisplayFeedPanelId,string>(),expiries=new Map<DisplayFeedPanelId,ReturnType<typeof setTimeout>>(),pending=new Map<DisplayFeedPanelId,FrameRecord>(),decoding=new Set<DisplayFeedPanelId>();
    const enabled=usable&&visible&&feed?.enabled===true&&!!revision&&!!session&&aircraft==='FA-18C_hornet'&&feed.aircraft===aircraft&&active.size>0;
    if(seen.current.revision!==JSON.stringify([revision,session]))seen.current={revision:JSON.stringify([revision,session]),sequences:{}};
    function clearPanel(id:DisplayFeedPanelId){const url=urls.get(id);if(url)URL.revokeObjectURL(url);urls.delete(id);clearTimeout(expiries.get(id));expiries.delete(id);setState(current=>{const frames={...(current.context===context?current.frames:{})};delete frames[id];return {...current,context,frames};});}
    function clear(){for(const id of STREAM_PANELS)clearPanel(id);pending.clear();}
    function status(label:string,detail:string){setState(current=>({context,frames:current.context===context?current.frames:{},status:label,detail}));}
    function resetHeartbeat(){clearTimeout(heartbeat);heartbeat=setTimeout(()=>{if(stopped)return;ready=false;clear();status('Stale','The display stream heartbeat stopped. Reconnecting.');controller?.abort();},FRAME_MAX_AGE);}
    async function decodeNext(id:DisplayFeedPanelId,connection:number){
      if(decoding.has(id))return;decoding.add(id);
      try{while(!stopped&&connection===attempt&&ready&&pending.has(id)){
        const item=pending.get(id)!;pending.delete(id);let url='';
        try{url=URL.createObjectURL(jpegBlob(item.jpeg_base64));const dimensions=await decodeImage(url);
          if(stopped||connection!==attempt||!ready||dimensions.width!==512||dimensions.height!==512||Date.now()-item.captured_epoch*1000>=FRAME_MAX_AGE){URL.revokeObjectURL(url);continue;}
          const previous=urls.get(id);urls.set(id,url);clearTimeout(expiries.get(id));
          const frame={url,sequence:item.sequence,capturedAt:item.captured_epoch*1000,width:dimensions.width,height:dimensions.height};
          setState(current=>({context,frames:{...(current.context===context?current.frames:{}),[id]:frame},status:'Streaming',detail:'Continuous display stream.'}));
          if(previous)URL.revokeObjectURL(previous);expiries.set(id,setTimeout(()=>clearPanel(id),Math.max(0,FRAME_MAX_AGE-(Date.now()-frame.capturedAt))));
        }catch{if(url)URL.revokeObjectURL(url);/* Bad frames never replace the last independently fresh frame. */}
      }}finally{decoding.delete(id);if(!stopped&&connection!==attempt&&ready&&pending.has(id))void decodeNext(id,attempt);}
    }
    async function connect(){
      if(stopped||!enabled)return;const connection=++attempt,abort=new AbortController();controller=abort;ready=false;status('Connecting','Connecting to the continuous display stream.');resetHeartbeat();
      try{
        const response=await abortable(fetch(`/api/display-feed/stream?panels=${encodeURIComponent(panelsKey)}&revision=${encodeURIComponent(revision)}`,{credentials:'same-origin',cache:'no-store',signal:abort.signal}).then(value=>{if(abort.signal.aborted)void value.body?.cancel().catch(()=>{});return value;}),abort.signal);
        if(!response.ok||!response.headers.get('Content-Type')?.toLowerCase().startsWith('application/x-ndjson')||!response.body)throw new Error('The display stream is unavailable.');
        const reader=response.body.getReader(),decoder=new TextDecoder(),lines=new DisplayLineReader();
        try{while(!stopped&&!abort.signal.aborted){const chunk=await abortable(reader.read(),abort.signal);if(chunk.done){lines.finish();throw new Error('The display stream closed.');}for(const value of lines.push(decoder.decode(chunk.value,{stream:true}))){
          if(stopped||connection!==attempt||abort.signal.aborted)break;
          const item=value as Record<string,unknown>;
          if(item?.type==='status'){
            if(typeof item.enabled!=='boolean'||typeof item.status!=='string'||!Array.isArray(item.panels))throw new Error('Invalid display stream status.');
            if(!item.enabled||item.revision!==revision||item.aircraft!==aircraft)throw new Error('Display context changed. Waiting for current setup.');
            resetHeartbeat();ready=item.status==='Ready';if(!ready){clear();status(item.status,typeof item.detail==='string'?item.detail:'Display capture is unavailable.');}else{failures=0;status('Streaming','Waiting for fresh display frames.');}
          }else if(item?.type==='frame'&&ready){
            const frame=frameRecord(item,revision,active,seen.current.sequences[item.panel as DisplayFeedPanelId]??-1);if(!frame)continue;
            seen.current.sequences[frame.panel]=frame.sequence;pending.set(frame.panel,frame);void decodeNext(frame.panel,connection);
          }else if(item?.type!=='frame')throw new Error('Invalid display stream record.');
        }}}finally{void reader.cancel().catch(()=>{});reader.releaseLock();}
      }catch(error){if(!stopped){ready=false;clear();status('Unavailable',abort.signal.aborted?'The display stream timed out. Reconnecting.':error instanceof Error?error.message:'Display stream unavailable.');}}
      finally{clearTimeout(heartbeat);if(controller===abort)controller=undefined;if(!stopped)retry=setTimeout(()=>void connect(),Math.min(2000,500*2**Math.min(failures++,2)));}
    }
    setState({context,frames:{},status:enabled?'Connecting':'Unavailable',detail:!visible?'Paused while this browser is hidden.':!usable?'Companion disconnected.':!feed?.enabled?'Capture is off.':'Waiting for a supported display session.'});
    if(enabled)void connect();return()=>{stopped=true;attempt++;controller?.abort();clearTimeout(retry);clearTimeout(heartbeat);clear();};
  },[context]);
  return state.context===context?state:{context,frames:{},status:'Connecting',detail:'Waiting for the current display stream.'};
}
