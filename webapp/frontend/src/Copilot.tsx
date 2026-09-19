import { useEffect, useState } from 'react';
import { request } from './api';
import { Badge } from './Scope';
import { format, text } from './utils';

interface JevStatus {
  enabled:boolean;configured:boolean;mode:'live'|'mock';model:string;
  question_set:string;execution:'display-only';
}
interface Distribution { [key:string]:number }
interface JevAdvisory {
  schema:string;model:string;question_set:string;latency_ms:number;summary:string;cached:boolean;
  intent:{value:string;confidence?:number;probabilities:Distribution};
  flight_phase:{value:string;confidence?:number;probabilities:Distribution};
  workload:{score?:number;confidence?:number;legend:Record<string,string>;probabilities:Distribution};
  data_sufficient_probability:number;contact_confidence?:number;proposal_allowed:boolean;
  contact?:{id:string;name:string;type:string;category:string;latitude:number;longitude:number;distance_nm?:number;bearing_true_deg?:number;age_seconds:number}|null;
  execution:'display-only';usage?:{input_tokens?:number;output_tokens?:number};
}

const examples=[
  'Find the hostile ground contact that best matches the convoy east of me.',
  'What flight phase does the current state support?',
  'Diagnose why the companion data may be unavailable.',
];
const percent=(value:unknown)=>typeof value==='number'?`${Math.round(value*100)}%`:'—';
const label=(value:string)=>value.replaceAll('_',' ');

function ProbabilityRows({values}:{values:Distribution}) {
  const rows=Object.entries(values).sort((a,b)=>b[1]-a[1]);
  return rows.length?<div className="jev-probabilities">{rows.map(([name,value])=><div key={name}><span>{label(name)}</span><meter min="0" max="1" value={value}/><b>{percent(value)}</b></div>)}</div>:<p className="muted">Probability distribution unavailable.</p>;
}

export default function Copilot({usable}:{usable:boolean}) {
  const [status,setStatus]=useState<JevStatus>();
  const [query,setQuery]=useState(examples[0]);
  const [result,setResult]=useState<JevAdvisory>();
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{let live=true;request('/api/jev/status',undefined,{timeoutMs:4000}).then(value=>{if(live)setStatus(value as JevStatus);}).catch(err=>{if(live)setError(err instanceof Error?err.message:'Jev status unavailable.');});return()=>{live=false;};},[]);
  async function evaluate(event:React.FormEvent){
    event.preventDefault();if(busy||query.trim().length<2)return;
    setBusy(true);setError('');setResult(undefined);
    try{setResult(await request('/api/jev/evaluate',{query:query.trim()},{timeoutMs:20000}) as JevAdvisory);}
    catch(err){setError(err instanceof Error?err.message:'Jev evaluation failed.');}
    finally{setBusy(false);}
  }
  const ready=usable&&status?.enabled&&status.configured;
  return <div className="page-view jev-page"><div className="page-title"><div><span className="eyebrow">TYPED JUDGMENTS · DISPLAY-ONLY OUTPUT</span><h1>Jev copilot<span className="title-period">.</span></h1><p>Ask for a bounded interpretation of the current companion state. Exact navigation and every aircraft action remain ordinary guarded code.</p></div><Badge tone={ready?'green':'amber'}>{ready?`${status?.mode.toUpperCase()} · READY`:'NOT CONFIGURED'}</Badge></div>
    <section className="panel jev-query"><form onSubmit={event=>void evaluate(event)}><label htmlFor="jev-request">Companion request</label><textarea id="jev-request" value={query} maxLength={500} rows={3} onChange={event=>setQuery(event.target.value)} placeholder="Describe the contact, navigation question, flight phase, or companion problem."/><div className="jev-query-actions"><div>{examples.map(example=><button type="button" className="plain-button" key={example} onClick={()=>setQuery(example)}>{example.split(' ').slice(0,4).join(' ')}…</button>)}</div><button className="primary-button" disabled={!ready||busy||query.trim().length<2}>{busy?'Evaluating…':'Evaluate current state'}</button></div></form><p className="micro">Only a reduced state is sent: the request, basic ownship state, verified fresh single-player contacts, and nearby navigation candidates. Local paths, bindings, logs, display text, credentials, and raw packets are excluded.</p></section>
    {!ready&&<p className="notice amber">Set <code>TYPESAFE_API_KEY</code> on the DCS computer and restart the dashboard. The rest of DCS Companion remains offline-capable.</p>}
    {error&&<p className="notice error" role="alert">{error}</p>}
    {result&&<div className="jev-results"><section className="panel jev-summary"><div className="panel-header"><span className="eyebrow">DETERMINISTIC POLICY RESULT</span><Badge tone={result.proposal_allowed?'green':'amber'}>{result.proposal_allowed?'PROPOSAL AVAILABLE':'ADVISORY ONLY'}</Badge></div><h2>{result.summary}</h2><dl className="step-details"><div><dt>Intent</dt><dd>{label(result.intent.value)} · {percent(result.intent.confidence)}</dd></div><div><dt>Flight phase</dt><dd>{label(result.flight_phase.value)} · {percent(result.flight_phase.confidence)}</dd></div><div><dt>Data sufficient</dt><dd>{percent(result.data_sufficient_probability)}</dd></div><div><dt>Workload</dt><dd>{format(result.workload.score,2)} / 2 · {percent(result.workload.confidence)}</dd></div><div><dt>Model / latency</dt><dd>{result.model} · {format(result.latency_ms,0)} ms{result.cached?' · cached':''}</dd></div><div><dt>Execution</dt><dd>Display only · no command queued</dd></div></dl></section>
      {result.contact&&<section className="panel jev-contact"><div className="panel-header"><span className="eyebrow">VERIFIED MISSION CANDIDATE</span><Badge tone="green">{percent(result.contact_confidence)}</Badge></div><h2>{result.contact.name}</h2><p>{result.contact.type} · {result.contact.category}</p><dl className="step-details"><div><dt>Coordinates</dt><dd>{result.contact.latitude.toFixed(5)}, {result.contact.longitude.toFixed(5)}</dd></div><div><dt>Relative navigation</dt><dd>{result.contact.distance_nm===undefined?'Unavailable':`${format(result.contact.distance_nm,1)} NM · ${format(result.contact.bearing_true_deg,0)}° TRUE`}</dd></div><div><dt>Source age</dt><dd>{format(result.contact.age_seconds,1)} s</dd></div></dl><p className="micro">Single-player mission awareness, not onboard radar or IFF. Selecting this candidate does not program the aircraft.</p></section>}
      <section className="panel jev-detail"><div className="panel-header"><span className="eyebrow">INTENT DISTRIBUTION</span><Badge>{result.question_set}</Badge></div><ProbabilityRows values={result.intent.probabilities}/><details><summary>Flight-phase distribution</summary><ProbabilityRows values={result.flight_phase.probabilities}/></details><p className="micro">Input {text(result.usage?.input_tokens,'—')} tokens · output {text(result.usage?.output_tokens,'—')} tokens.</p></section></div>}
  </div>;
}
