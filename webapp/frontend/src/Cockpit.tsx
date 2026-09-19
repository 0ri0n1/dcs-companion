import { useState } from 'react';
import { Badge, Metric } from './Scope';
import type { RecordData, Snapshot } from './types';
import { age, list, number, obj, text } from './utils';

function denied(capabilities:unknown) {
  const caps=obj(capabilities);
  return ['cockpit','ownship','sensor'].some(key=>['denied','export denied'].includes(text(obj(caps[key]).verdict??obj(caps[key]).status??caps[key],'').toLowerCase()));
}
function Elements({elements,historical}:{elements:RecordData[];historical:boolean}) {
  return <div className={`cockpit-readout ${historical?'historical':''}`}><p className="eyebrow">{historical?'LAST EXPORTED TEXT — HISTORICAL, NOT CURRENT':'CURRENT EXPORTED TEXT'}</p><dl>{elements.map((element,index)=><div key={`${text(element.name)}:${index}`}><dt>{text(element.name,'Unnamed exported element')}</dt><dd>{typeof element.value==='string'?element.value:text(element.value,'No value exported')}</dd></div>)}</dl></div>;
}
export default function Cockpit({snapshot,usable,elapsed=0}:{snapshot?:Snapshot;usable:boolean;elapsed?:number}) {
  const [search,setSearch]=useState('');
  const cockpit=obj(snapshot?.cockpit),health=snapshot?.health;
  const matches=Boolean(health?.aircraft&&cockpit.aircraft===health.aircraft);
  const exportDenied=denied(health?.export_capabilities);
  const allowed=usable&&matches&&!exportDenied;
  const displays=list(cockpit.displays),needle=search.trim().toLowerCase();
  const anyFresh=displays.some(display=>display.status==='Available'&&number(display.age_s)!==undefined&&Number(display.age_s)>=0&&Number(display.age_s)+elapsed<=3)&&health?.telemetry_fresh===true;
  const status=!usable?'Offline':!matches?'Unavailable':exportDenied?'Export denied':cockpit.status==='Available'&&!anyFresh?'Stale':text(cockpit.status,'Unavailable');
  const find=(elements:unknown)=>list(elements).filter(element=>`${text(element.name,'')} ${text(element.value,'')}`.toLowerCase().includes(needle));
  return <div className="page-view cockpit-view"><div className="page-title"><div><span className="eyebrow">READ-ONLY · EXPORTED DISPLAY TEXT</span><h1>Cockpit<span className="title-period">.</span></h1><p>DDI, UFC, AMPCD and other exported readouts. Text only; this is not a video or pixel reproduction.</p></div><Badge tone={status==='Available'?'green':'amber'}>{status}</Badge></div>
    <div className="panel cockpit-summary"><Metric label="AIRCRAFT" value={text(health?.aircraft,'No aircraft')} small/><Metric label="AGGREGATE DISPLAY AGE" value={age(health?.display_age_s)} small/><p>Each display has its own clock. Historical text is separated from current readouts.</p></div>
    <div className="export-toolbar"><label className="search-box"><span>⌕</span><input aria-label="Search cockpit text" value={search} onChange={event=>setSearch(event.target.value)} placeholder="Search display labels or values…"/></label><span className="muted">{displays.length} display slots</span></div>
    <div className="cockpit-grid">{displays.map((display,index)=>{
      const reported=text(display.status,'Unavailable');
      const displayAge=number(display.age_s),currentAge=displayAge===undefined?undefined:displayAge+elapsed;
      const blocked=['denied','export denied','unsupported','wrong aircraft'].includes(reported.toLowerCase());
      const fresh=allowed&&!blocked&&reported==='Available'&&currentAge!==undefined&&currentAge>=0&&currentAge<=3&&health?.telemetry_fresh===true;
      const current=fresh?find(display.elements):[];
      const historical=allowed&&!blocked&&displayAge!==undefined&&displayAge>=0?find(display.last_observed_elements):[];
      const actual=!usable?'Offline':!matches?'Unavailable':exportDenied?'Export denied':reported==='Available'&&!fresh?(currentAge===undefined?'Unverified':'Stale'):reported;
      return <section className="panel cockpit-display" key={text(display.id,String(index))}><div className="panel-header"><span className="eyebrow">DISPLAY {text(display.id)}</span><Badge tone={fresh?'green':'amber'}>{actual}</Badge></div><div className="display-title"><h2>{text(display.label,`Display ${text(display.id)}`)}</h2><span className="muted">{age(currentAge)}</span></div>
        {current.length>0&&<Elements elements={current} historical={false}/>}{historical.length>0&&<Elements elements={historical} historical/>}
        {!current.length&&!historical.length&&<div className="cockpit-empty"><span>◇</span><strong>{needle?'No matching exported text':'No eligible display text'}</strong><p>{needle?'Try a different label or value.':'Nothing current is available from this display. Blank, missing or stale exports are not recreated.'}</p></div>}
        <details className="sensor-source"><summary>Source details</summary><p>{text(display.source,'Source unavailable')}</p>{Array.isArray(display.notes)&&display.notes.map((note,i)=><p key={i}>{text(note)}</p>)}</details>
      </section>;
    })}</div>
    {!displays.length&&<div className="panel empty-state"><h2>No cockpit text available</h2><p>Open an aircraft with a permitted display-text export. Readouts will appear as their packets arrive.</p></div>}
    <div className="panel footnote-panel"><strong>These readouts cannot press cockpit buttons.</strong><p>Labels and values are shown exactly as exported, without reconstructing graphics, video, or a tactical contact picture.</p></div>
  </div>;
}
