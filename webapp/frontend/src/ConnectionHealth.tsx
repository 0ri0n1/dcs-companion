import { useState } from 'react';
import { request } from './api';
import type { ConnectionState, TransportDiagnostics } from './api';
import type { ConnectionEvent } from './connection-history';
import { sanitizeConnectionHistory } from './connection-history';

interface Props {connection:ConnectionState;receivedAt:number;now:number;error:string;diagnostics:TransportDiagnostics;history:ConnectionEvent[];}
export default function ConnectionHealth({connection,receivedAt,now,error,diagnostics,history}:Props){
  const [busy,setBusy]=useState(false),[downloadNote,setDownloadNote]=useState('');
  const seconds=receivedAt?Math.max(0,now-receivedAt)/1000:undefined;
  const delayed=connection==='delayed'||connection==='connected'&&seconds!==undefined&&seconds>=6;
  async function download(){
    setBusy(true);setDownloadNote('');let server:unknown;
    try{server=await request('/api/diagnostics',undefined,{timeoutMs:4000});}catch{server={status:'Unavailable'};setDownloadNote('Downloaded browser history. Server diagnostics were unavailable.');}
    try{
      const report={version:1,generated_at:new Date().toISOString(),browser:{connection:delayed?'delayed':connection,transport:diagnostics.transport,last_update_at:receivedAt?new Date(receivedAt).toISOString():null,update_age_s:seconds??null,reconnects:diagnostics.reconnectCount,history:sanitizeConnectionHistory(history)},server};
      const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download='dcs-companion-connection-report.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch{setDownloadNote('The report could not be downloaded. Try again.');}finally{setBusy(false);}
  }
  const source=diagnostics.transport==='stream'?'Live stream':diagnostics.transport==='poll'?'Recovery request':diagnostics.transport==='reconnecting'?'Repairing live stream':'Waiting for updates';
  return <section className="panel connection-health" aria-label="Companion connection history"><div className="connection-health-heading"><div><span className="eyebrow">BROWSER TO COMPANION</span><h2>Connection history</h2></div><span className={`badge ${connection==='connected'&&!delayed?'green':'amber'}`}>{delayed?'Updates delayed':connection}</span></div>
    <dl className="connection-health-metrics"><div><dt>Receiving through</dt><dd>{source}</dd></div><div><dt>Last dashboard update</dt><dd>{seconds===undefined?'None yet':`${seconds.toFixed(1)} s ago`}</dd></div><div><dt>Stream repairs this visit</dt><dd>{diagnostics.reconnectCount}</dd></div></dl>
    <p>Aircraft telemetry and mission positions have their own freshness checks. A paused simulation or expired mission export does not by itself mean the browser disconnected.</p>
    {(error||diagnostics.lastIssue)&&<p className="connection-issue"><strong>{error?'Current issue':'Last connection issue'}</strong> {error||diagnostics.lastIssue}</p>}
    <div className="connection-history-toolbar"><span>{history.length} recent events · this browser tab</span><button disabled={busy} onClick={()=>void download()}>{busy?'Preparing report…':'Download connection report'}</button></div>
    {downloadNote&&<p role="status">{downloadNote}</p>}
    {history.length?<ol className="connection-event-list">{history.slice(-20).reverse().map((event,index)=><li key={`${event.timestamp}-${index}`}><time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleTimeString()}</time><div><strong>{event.reason}</strong>{event.duration_ms!==undefined&&<small>After {(event.duration_ms/1000).toFixed(1)} s</small>}</div></li>)}</ol>:<p>No interruptions recorded in this tab.</p>}
    <p className="connection-history-note">The latest 200 event times and reasons survive a page reload in this tab. Flight data, positions and pairing codes are excluded.</p>
  </section>;
}
