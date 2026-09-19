import { Badge, Metric } from './Scope';
import type { RecordData, Snapshot } from './types';
import { age, list, number, obj, text } from './utils';

const titles: Record<string,string>={flir:'FLIR / targeting pod',laser:'Laser / spot tracker',radar:'Airborne radar',rwr:'Radar warning receiver'};
// The backend supplies sanitized display labels. This separate display-only
// allowlist deliberately excludes world objects, raw tracks and target counts.
const displayFieldIds = new Set(['flir.track_mode','flir.zoom','flir.zoom_text','flir.focus','flir.mask_label','flir.target_range','laser.status_label','laser.lst_code','laser.ltdr_code','laser.lst_label','laser.ltdr_label']);
function exportDenied(capabilities: unknown) {
  const caps=obj(capabilities);
  return ['sensor','cockpit','ownship'].some(key=>{
    const value=caps[key];const verdict=text(obj(value).verdict??obj(value).status??value,'').toLowerCase();
    return verdict==='denied'||verdict==='export denied';
  });
}
function Readings({ fields, lastObserved }: {fields:RecordData[];lastObserved:boolean}) {
  return <div className={lastObserved?'sensor-readings last-observed':'sensor-readings'}>
    <p className="eyebrow">{lastObserved?'LAST EXPORTED DISPLAY TEXT — INDIVIDUAL DISPLAY AGE UNKNOWN':'FRESH EXPORTED DISPLAY TEXT'}</p>
    <dl>{fields.map((field,index)=><div key={`${text(field.id,String(index))}:${text(field.display_id)}`}><dt>{text(field.label)}</dt><dd>{text(field.value)}</dd><small>Display {text(field.display_id)} · {lastObserved?'Individual age Unknown':`Field age ${age(field.age_s)}`}</small><details className="sensor-source"><summary>Source details</summary><small>Element · {text(field.element)}</small><small>{text(field.confidence)}</small><small>{text(field.source)}</small></details></div>)}</dl>
  </div>;
}
export default function Sensors({snapshot,usable,elapsed=0}:{snapshot?:Snapshot;usable:boolean;elapsed?:number}) {
  const sensors=obj(snapshot?.sensors),health=snapshot?.health;
  const matching=health?.aircraft==='FA-18C_hornet'&&sensors.aircraft===health.aircraft;
  const denied=exportDenied(health?.export_capabilities);
  const fresh=usable&&matching&&health?.telemetry_fresh===true&&health?.displays_fresh===true&&!denied;
  const layers=list(sensors.layers);
  const overall=!usable?'Offline':!matching?'Unavailable':denied?'Export denied':!fresh?'Stale':text(sensors.status,'Unverified');
  return <div className="page-view sensors-view"><div className="page-title"><div><span className="eyebrow">READ-ONLY · EXPORTED COCKPIT EVIDENCE</span><h1>Sensors<span className="title-period">.</span></h1><p>{text(health?.aircraft,'No aircraft')} · Exact display text, source and freshness. No sensor or weapon commands.</p></div><Badge tone={overall==='Available'?'green':'amber'}>{overall}</Badge></div>
    <div className="panel sensor-summary"><Metric label="OWNSHIP TELEMETRY AGE" value={age(health?.telemetry_age_s)} small/><Metric label="AGGREGATE DISPLAY AGE" value={age(health?.display_age_s)} small/><div><strong>{text(sensors.display_freshness,'Individual display age: Unknown')}</strong><p>Each eligible field keeps its own source and age. A fresh aggregate stream does not prove that every displayed page was refreshed.</p></div></div>
    <div className="sensor-grid">{Object.entries(titles).map(([id,title])=>{
      const layer=layers.find(item=>item.id===id)??{};
      const status=!usable?'Offline':!matching?'Unavailable':denied?'Export denied':!fresh?'Stale':text(layer.status,'Unavailable');
      const permitted=fresh&&!['stale','offline','export denied','unavailable','unsupported'].includes(status.toLowerCase());
      const displayLayer=id==='flir'||id==='laser';
      const accepted=(value:unknown)=>displayLayer&&permitted?list(value).filter(field=>displayFieldIds.has(text(field.id,''))&&text(field.id,'').startsWith(`${id}.`)&&['string','number'].includes(typeof field.value)&&[2,3,4].includes(Number(field.display_id))):[];
      const currentFields=['Available','Fresh','Verified'].includes(status)?accepted(layer.fields).filter(field=>number(field.age_s)!==undefined&&Number(field.age_s)>=0&&Number(field.age_s)+elapsed<=3).map(field=>({...field,age_s:Number(field.age_s)+elapsed})):[];
      const lastFields=accepted(layer.last_observed_fields);
      return <section key={id} className="panel sensor-card"><div className="panel-header"><span className="eyebrow">{id.toUpperCase()}</span><Badge tone={status==='Available'?'green':'amber'}>{status}</Badge></div><h2>{title}</h2>
        {currentFields.length>0&&<Readings fields={currentFields} lastObserved={false}/>}{lastFields.length>0&&<Readings fields={lastFields} lastObserved={true}/>}
        {!currentFields.length&&!lastFields.length&&<div className="sensor-empty"><span>◇</span><strong>{displayLayer?'No eligible display text':'No verified tactical contacts'}</strong><p>{displayLayer?'Values are withheld when stale, unavailable, denied, or from a different aircraft.':'Availability is reported here; raw object lists and unverified tracks are never shown as enemies.'}</p></div>}
        <div className="sensor-limits"><p>{id==='flir'?'FLIR image/video is not exported by this bridge.':id==='laser'?'A displayed code or MASK label does not establish that the laser is firing.':id==='radar'?'Threat-warning data does not establish an airborne radar picture.':'Legacy countermeasure data cannot establish a threat count.'}</p><details className="sensor-source"><summary>Source details</summary><p>{text(layer.source,'Source unavailable')}</p>{id==='radar'&&<p>LoGetTWSInfo is Threat Warning System data, not an airborne radar TWS picture.</p>}{id==='rwr'&&<p>The legacy rwr_count value came from LoGetSnares chaff/flare data and is not an RWR or enemy count.</p>}{Array.isArray(layer.notes)&&layer.notes.filter(note=>typeof note==='string').map((note,index)=><p key={index}>{String(note)}</p>)}</details></div>
      </section>;
    })}</div>
    <div className="panel footnote-panel"><strong>No enemy or world-object display.</strong><p>These cards show eligible aircraft-exported evidence only. No raw world objects, unverified contact coordinates, laser-firing inference, or command controls are displayed.</p></div>
  </div>;
}
