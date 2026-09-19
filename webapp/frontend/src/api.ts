import { useCallback, useEffect, useRef, useState } from 'react';
import type { Snapshot } from './types';
import { CONNECTION_REASONS, useConnectionHistory } from './connection-history';
import type { ConnectionEventCode } from './connection-history';
export const isLoopbackHost=(hostname:string)=>['127.0.0.1','localhost','[::1]','::1'].includes(hostname.toLowerCase());
export type ConnectionState='connecting'|'connected'|'reconnecting'|'delayed'|'offline'|'pairing';
export interface TransportDiagnostics {transport:'none'|'stream'|'poll'|'reconnecting';lastStreamAt:number;lastPollAt:number;reconnectCount:number;lastIssue:string;}
interface RequestOptions {timeoutMs?:number;signal?:AbortSignal;method?:'DELETE';}
export async function request(path:string,payload?:unknown,options:RequestOptions={}){
  const controller=new AbortController();let timedOut=false;
  const abort=()=>controller.abort();
  if(options.signal?.aborted)abort();else options.signal?.addEventListener('abort',abort,{once:true});
  const timer=options.timeoutMs?setTimeout(()=>{timedOut=true;controller.abort();},options.timeoutMs):undefined;
  try{
    const response=await fetch(path,{method:options.method??(payload===undefined?'GET':'POST'),credentials:'same-origin',cache:'no-store',headers:payload===undefined?{}:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload),signal:controller.signal});
    let data;try{data=await response.json();}catch(error){if(controller.signal.aborted)throw error;data={};}
    if(!response.ok)throw Object.assign(new Error(typeof data.detail==='string'?data.detail:`Request failed (${response.status})`),{status:response.status});
    return data;
  }catch(error){if(timedOut)throw Object.assign(new Error('The companion request timed out.'),{code:'REQUEST_TIMEOUT'});throw error;}
  finally{if(timer)clearTimeout(timer);options.signal?.removeEventListener('abort',abort);}
}
// Consume QR credentials before any API request; StrictMode shares one exchange promise.
function incomingPairing(){
  if(typeof window==='undefined'||!window.location.hash.startsWith('#pair='))return undefined;
  const fragment=window.location.hash;history.replaceState(history.state,'',window.location.pathname+window.location.search);
  const match=/^#pair=([A-Za-z0-9_-]{43})$/.exec(fragment);
  return {ticket:match?.[1],invalid:!match};
}
let fragmentPairing=incomingPairing(),ticketExchange:Promise<unknown>|undefined;
function exchangeTicket(){
  if(!fragmentPairing)return undefined;
  if(!ticketExchange){const ticket=fragmentPairing.ticket;fragmentPairing={ticket:undefined,invalid:fragmentPairing.invalid};ticketExchange=ticket?request('/api/session',{ticket},{timeoutMs:4000}):Promise.reject(new Error('Invalid pairing code'));}
  return ticketExchange;
}
export function useSnapshot(){
  const [snapshot,setSnapshot]=useState<Snapshot>(),[connection,setConnection]=useState<ConnectionState>('connecting');
  const [error,setError]=useState(''),[session,setSession]=useState(0),[receivedAt,setReceivedAt]=useState(0);
  const [diagnostics,setDiagnostics]=useState<TransportDiagnostics>({transport:'none',lastStreamAt:0,lastPollAt:0,reconnectCount:0,lastIssue:''});
  const {history:connectionHistory,record}=useConnectionHistory();
  useEffect(()=>{const rescan=()=>{const incoming=incomingPairing();if(!incoming)return;fragmentPairing=incoming;ticketExchange=undefined;setSnapshot(undefined);setReceivedAt(0);setConnection('connecting');setSession(value=>value+1);};window.addEventListener('hashchange',rescan);return()=>window.removeEventListener('hashchange',rescan);},[]);
  const observations=useRef<{telemetry?:boolean;awareness?:boolean;telemetrySince?:number;awarenessSince?:number}>({});
  const observeAvailability=useCallback((telemetry:boolean,awareness:boolean)=>{
    const previous=observations.current,now=Date.now();
    for(const key of ['telemetry','awareness'] as const){const available=key==='telemetry'?telemetry:awareness,old=previous[key],since=key==='telemetry'?'telemetrySince':'awarenessSince';if(old!==available){if(old!==undefined||!available)record(key==='telemetry'?(available?'telemetry_restored':'telemetry_stale'):(available?'awareness_recovered':'awareness_unavailable'),available&&previous[since]!==undefined?now-previous[since]!:undefined);previous[key]=available;previous[since]=available?undefined:now;}}
  },[record]);
  const pair=useCallback(async(token:string)=>{await request('/api/session',{token},{timeoutMs:4000});fragmentPairing=undefined;ticketExchange=undefined;setSession(value=>value+1);setError('');},[]);
  useEffect(()=>{
    let events:EventSource|undefined,stopped=false,inFlight=false,authRejected=false;
    let acceptedVersion=0,lastAcceptedAt=0,lastStreamAt=0,nextAttemptAt=0,issueSince:number|undefined,lastFailure:ConnectionEventCode|undefined;
    let activeRequest:AbortController|undefined;
    const local=isLoopbackHost(window.location.hostname);
    function issue(code:ConnectionEventCode){if(stopped)return;if(issueSince===undefined)issueSince=Date.now();if(lastFailure!==code){record(code);lastFailure=code;}setError(CONNECTION_REASONS[code]);setDiagnostics(previous=>({...previous,lastIssue:CONNECTION_REASONS[code]}));}
    function accept(data:Snapshot,source:'stream'|'poll'){
      if(stopped)return;if(!data||typeof data!=='object'||!data.health||typeof data.health!=='object')throw new Error('Unreadable snapshot');
      const now=Date.now();acceptedVersion++;lastAcceptedAt=now;if(source==='stream')lastStreamAt=now;
      setSnapshot(data);setReceivedAt(now);setConnection('connected');setError('');setDiagnostics(previous=>({...previous,transport:source,...(source==='stream'?{lastStreamAt:now}:{lastPollAt:now})}));
      if(issueSince!==undefined){record('recovered',now-issueSince);issueSince=undefined;}lastFailure=undefined;
    }
    function openStream(){
      if(stopped||events)return;lastStreamAt=0;const stream=new EventSource('/api/events',{withCredentials:true});events=stream;
      const message=(event:MessageEvent)=>{if(stopped||events!==stream)return;try{accept(JSON.parse(event.data),'stream');}catch{issue('request_failed');}};
      stream.onmessage=message;stream.addEventListener('state',message as EventListener);
      stream.onerror=()=>{if(stopped||events!==stream)return;stream.close();events=undefined;issue('stream_error');setDiagnostics(previous=>({...previous,transport:'reconnecting',reconnectCount:previous.reconnectCount+1}));if(!lastAcceptedAt||Date.now()-lastAcceptedAt>=6000)setConnection('reconnecting');nextAttemptAt=Date.now()+2500;void connect();};
    }
    async function connect(){
      if(stopped||inFlight||authRejected)return;inFlight=true;const version=acceptedVersion,controller=new AbortController();activeRequest=controller;
      try{
        let data:Snapshot;
        try{data=await request('/api/state',undefined,{timeoutMs:4000,signal:controller.signal});}
        catch(error){if(stopped||version!==acceptedVersion)return;if((error as {status?:number}).status!==401||!local)throw error;issue('auth_repair');setSnapshot(undefined);setReceivedAt(0);setConnection('connecting');events?.close();events=undefined;await request('/api/session',{token:''},{timeoutMs:4000,signal:controller.signal});if(stopped)return;data=await request('/api/state',undefined,{timeoutMs:4000,signal:controller.signal});}
        if(stopped||version!==acceptedVersion)return;accept(data,'poll');openStream();
      }catch(error){
        if(stopped||version!==acceptedVersion)return;const status=(error as {status?:number}).status;
        if(status===401||status===403){authRejected=true;events?.close();events=undefined;setSnapshot(undefined);setReceivedAt(0);setConnection('pairing');issue('auth_required');}
        else{setConnection('offline');issue((error as {code?:string}).code==='REQUEST_TIMEOUT'?'request_timeout':'request_failed');}
      }finally{inFlight=false;if(activeRequest===controller)activeRequest=undefined;nextAttemptAt=Date.now()+2500;}
    }
    async function start(){
      if(fragmentPairing){inFlight=true;try{await exchangeTicket();}catch{if(!stopped){authRejected=true;setConnection('pairing');issue('auth_required');setError('This pairing code is invalid, expired or already used. Create a new QR code on your DCS computer, or enter a pairing token.');}return;}finally{inFlight=false;}if(stopped)return;}
      void connect();
    }
    void start();
    const watchdog=setInterval(()=>{
      if(stopped||authRejected)return;const now=Date.now();
      if(events){if(now-(lastStreamAt||lastAcceptedAt)<6000)return;events.close();events=undefined;issue('update_overdue');setConnection('delayed');setDiagnostics(previous=>({...previous,transport:'reconnecting',reconnectCount:previous.reconnectCount+1}));nextAttemptAt=now;}
      if(!inFlight&&now>=nextAttemptAt)void connect();
    },500);
    return()=>{stopped=true;clearInterval(watchdog);activeRequest?.abort();events?.close();};
  },[session,record]);
  return {snapshot,connection,error,pair,receivedAt,diagnostics,connectionHistory,observeAvailability};
}
