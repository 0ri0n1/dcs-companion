import { useCallback, useEffect, useState } from 'react';
export const CONNECTION_HISTORY_KEY='dcs-copilot-connection-history-v1';
export const CONNECTION_HISTORY_LIMIT=200;
export const CONNECTION_REASONS={stream_error:'Live stream interrupted; checking the companion connection.',recovered:'Dashboard updates resumed.',update_overdue:'No dashboard update arrived for six seconds; reconnecting.',request_timeout:'The companion did not respond before the request deadline.',request_failed:'The companion could not be reached.',auth_required:'This device needs to pair with the companion.',auth_repair:'Renewing this computer’s companion session.',telemetry_stale:'Ownship telemetry became stale or unavailable.',telemetry_restored:'Fresh ownship telemetry resumed.',awareness_unavailable:'Current mission positions became unavailable.',awareness_recovered:'Current mission positions resumed.'} as const;
export type ConnectionEventCode=keyof typeof CONNECTION_REASONS;
export interface ConnectionEvent {timestamp:string;code:ConnectionEventCode;reason:string;duration_ms?:number;}
/** Only controlled reasons and event clocks survive reload; never server or flight payloads. */
export function sanitizeConnectionHistory(value:unknown):ConnectionEvent[]{
  if(!Array.isArray(value))return [];
  return value.flatMap(item=>{
    if(!item||typeof item!=='object'||typeof item.timestamp!=='string'||!Number.isFinite(Date.parse(item.timestamp))||!Object.hasOwn(CONNECTION_REASONS,item.code))return [];
    const code=item.code as ConnectionEventCode,event:ConnectionEvent={timestamp:new Date(item.timestamp).toISOString(),code,reason:CONNECTION_REASONS[code]};
    if(typeof item.duration_ms==='number'&&Number.isFinite(item.duration_ms)&&item.duration_ms>=0)event.duration_ms=Math.round(Math.min(item.duration_ms,604800000));
    return [event];
  }).slice(-CONNECTION_HISTORY_LIMIT);
}
function readHistory(){try{return sanitizeConnectionHistory(JSON.parse(sessionStorage.getItem(CONNECTION_HISTORY_KEY)??'[]'));}catch{return [];}}
export function useConnectionHistory(){
  const [history,setHistory]=useState<ConnectionEvent[]>(readHistory);
  useEffect(()=>{try{sessionStorage.setItem(CONNECTION_HISTORY_KEY,JSON.stringify(history));}catch{/* Optional diagnostics storage. */}},[history]);
  const record=useCallback((code:ConnectionEventCode,duration_ms?:number)=>setHistory(previous=>sanitizeConnectionHistory([...previous,{timestamp:new Date().toISOString(),code,duration_ms}])),[]);
  return {history,record};
}
