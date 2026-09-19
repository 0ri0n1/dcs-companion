import type { RecordData, Snapshot } from './types';
import { age, coords, format, heading, list, navigation, number, obj, text } from './utils';

export type EnemyContact=RecordData & {id:string;name:string;category:string;affiliation:'enemy'|'friendly';distance_nm:number;bearing_true_deg:number;age_s:number};
export interface AwarenessView {allowed:boolean;status:string;detail:string;source:string;age_s?:number;contacts:EnemyContact[];friendlies:EnemyContact[];reported?:number;truncated:boolean;dropped?:number;friendlyReported?:number;friendlyTruncated:boolean;friendlyDropped?:number;notes:string[]}

/** Mission coalition is not an aircraft detection or an IFF reply. */
export function awarenessView(snapshot:Snapshot|undefined,usable:boolean,automatic:boolean,ownPosition:[number,number]|undefined,elapsed=0):AwarenessView {
  const awareness=obj(snapshot?.awareness),health=snapshot?.health;
  const baseAge=number(awareness.age_s),currentAge=baseAge===undefined?undefined:baseAge+elapsed;
  let status=text(awareness.status,'Unavailable'),detail=text(awareness.detail,'No single-player mission-awareness export is available.');
  if(!automatic){status='Hidden · offline library';detail='Mission units are hidden while browsing a manual terrain library.';}
  else if(!usable){status='Offline';detail='Reconnect to receive current mission positions.';}
  else if(awareness.mode!=='mission-awareness'){status='Unavailable';detail='A verified mission-awareness export is required.';}
  else if(awareness.single_player!==true){status=awareness.single_player===false?'Multiplayer · hidden':'Single-player status unknown';detail='Mission positions are available only in explicitly verified single-player sessions.';}
  else if(!health?.aircraft||!health.session||awareness.aircraft!==health.aircraft||awareness.session!==health.session||(typeof snapshot?.aircraft?.unit_name==='string'&&awareness.ownship_unit!==snapshot.aircraft.unit_name)){status='Session mismatch';detail='The export must match the current aircraft, player unit and flight session.';}
  else if(awareness.model_advancing!==true||health.model_advancing!==true||/pause/i.test(text(health.status,''))){status='Paused / unverified';detail='Mission positions require an advancing simulation clock.';}
  else if(!health.telemetry_fresh||!ownPosition){status='Ownship unavailable';detail='Fresh ownship telemetry and position are required.';}
  else if(currentAge===undefined||currentAge<0||currentAge>5){status='Stale';detail='Mission positions have expired; no previous positions are retained on the scope.';}
  const allowed=status==='Available',seen=new Set<string>([text(awareness.mission_player_unit_id,'')]);
  const readContacts=(key:'contacts'|'friendlies',affiliation:'enemy'|'friendly'):EnemyContact[]=>allowed?list(awareness[key]).flatMap(contact=>{
    const position=coords(contact),receivedAge=number(contact.age_s),id=text(contact.id,'');
    if(!position||!id||seen.has(id)||receivedAge===undefined||receivedAge<0||receivedAge+elapsed>5)return [];
    seen.add(id);
    return [{...contact,id,affiliation,name:text(contact.name,id),category:text(contact.category,'unknown').toLowerCase(),...navigation(ownPosition!,position),age_s:receivedAge+elapsed}];
  }).sort((a,b)=>a.distance_nm-b.distance_nm):[];
  const contacts=readContacts('contacts','enemy'),friendlies=readContacts('friendlies','friendly');
  return {allowed,status,detail,source:text(awareness.source,'Source unavailable'),age_s:currentAge,contacts,friendlies,
    reported:allowed?number(awareness.total_count):undefined,truncated:allowed&&awareness.truncated===true,
    dropped:allowed?number(awareness.omitted_count??awareness.dropped_count??awareness.dropped):undefined,
    friendlyReported:allowed?number(awareness.friendly_total_count):undefined,friendlyTruncated:allowed&&awareness.friendly_truncated===true,
    friendlyDropped:allowed?number(awareness.friendly_omitted_count):undefined,
    notes:allowed&&Array.isArray(awareness.notes)?awareness.notes.filter((note):note is string=>typeof note==='string'):[]};
}

export function enemyCategory(contact:EnemyContact) {
  if(/helicopter|helo|rotary/.test(contact.category))return 'helicopter';
  if(/air|plane|fixed/.test(contact.category))return 'air';
  if(/ship|naval|sea/.test(contact.category))return 'ship';
  if(/ground|vehicle|static|train/.test(contact.category))return 'ground';
  return 'unknown';
}

export function trueDirection(value:unknown) {
  const degrees=number(value);
  return degrees!==undefined&&degrees>=0&&degrees<360?degrees:undefined;
}

export function EnemySymbol({contact,point,selected,rotation,onSelect,label}:{contact:EnemyContact;point:[number,number];selected:boolean;rotation:number;onSelect:()=>void;label:boolean}) {
  const category=enemyCategory(contact),track=trueDirection(contact.track_true_deg),bearing=trueDirection(contact.heading_true_deg),friendly=contact.affiliation==='friendly';
  const direction=track??bearing,directionSource=track!==undefined?'track':bearing!==undefined?'heading':'unknown';
  const shortName=contact.name.length>28?`${contact.name.slice(0,27)}…`:contact.name;
  return <g className={`enemy-symbol ${friendly?'friendly-symbol':''} category-${category} ${selected?'selected':''}`} transform={`translate(${point[0]},${point[1]})`} role="button" tabIndex={0} aria-label={`Select mission ${contact.affiliation} ${contact.name}`} onClick={onSelect} onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();onSelect();}}}>
    <circle className="enemy-hit" r="18"/>{friendly&&<rect className="friendly-frame" x="-12" y="-12" width="24" height="24" rx="8"/>}{selected&&<circle className="enemy-selection" r="15"/>}
    <g className="contact-direction" data-direction-source={directionSource} transform={direction===undefined?undefined:`rotate(${direction-rotation})`}>
      {category==='air'?(direction===undefined?<circle className="contact-direction-unknown" r="7"/>:<path d="M0 -9 8 7 0 3 -8 7Z"/>):category==='helicopter'?<><circle r="5"/><path d="M-9 0H9M0 -9V9"/></>:category==='ship'?(direction===undefined?<rect x="-8" y="-5" width="16" height="10" rx="4"/>:<path d="M0 -10 6 -4V8H-6V-4Z"/>):category==='ground'?<rect x="-6" y="-6" width="12" height="12"/>:<path d="M0 -8 8 0 0 8 -8 0Z"/>}
      {direction!==undefined&&<path className="contact-direction-cue" d="M0 -11V-24M-4 -19L0 -24 4 -19"/>}
    </g>
    {label&&<><rect className="enemy-label-hit" x="12" y="-25" width={shortName.length*7+8} height="32"/><text x="15" y="-11">{shortName}</text></>}<title>{contact.name} · {friendly?'Friendly':'Enemy'} mission unit · {text(contact.type)} · {format(contact.distance_nm,1)} NM · {direction===undefined?'Direction unknown':`${directionSource==='track'?'Ground track':'Heading'} ${heading(direction)} TRUE`} · Mission awareness, not aircraft radar</title>
  </g>;
}

export function AwarenessPanel({view,selected,onSelect,inRange,showEnemies=true,showFriendlies=true}:{view:AwarenessView;selected?:EnemyContact;onSelect:(id:string)=>void;inRange:Set<string>;showEnemies?:boolean;showFriendlies?:boolean}) {
  const contacts=[...(showEnemies?view.contacts:[]),...(showFriendlies?view.friendlies:[])].sort((a,b)=>a.distance_nm-b.distance_nm);
  const enemyCount=view.contacts.filter(contact=>inRange.has(contact.id)).length,friendlyCount=view.friendlies.filter(contact=>inRange.has(contact.id)).length;
  return <section className="panel mission-awareness" aria-label="Mission awareness"><div className="awareness-header"><div><strong>Mission awareness</strong><span>Single-player only · Not aircraft radar</span></div><div className="awareness-count">{view.allowed?<><strong>{showEnemies?`${view.contacts.length} enemies · ${enemyCount} on scope`:'Enemies hidden'}</strong><strong>{showFriendlies?`${view.friendlies.length} friendlies · ${friendlyCount} on scope`:'Friendlies hidden'}</strong></>:<strong>{view.status}</strong>}<span>{view.allowed?`Export age ${age(view.age_s)}`:'No mission positions displayed'}</span></div></div>
    {view.allowed?<div className="awareness-body contact-list-only"><div className="enemy-list" aria-label="Mission unit list">{contacts.map(contact=><button className={`enemy-row ${contact.affiliation==='friendly'?'friendly-row':''} ${selected?.id===contact.id?'selected':''}`} key={contact.id} onClick={()=>onSelect(contact.id)} aria-label={`Inspect mission ${contact.affiliation} ${contact.name}`}><span className="enemy-category">{contact.affiliation==='friendly'?'⌒':({air:'△',helicopter:'⊕',ground:'□',ship:'▱',unknown:'◇'})[enemyCategory(contact)]}</span><span><strong>{contact.name}</strong><small>{contact.affiliation==='friendly'?'Friendly':'Enemy'} · {text(contact.type)} · {inRange.has(contact.id)?'On scope':'Outside scope range'}</small></span><span>{format(contact.distance_nm,1)} NM<small>{heading(contact.bearing_true_deg)} TRUE</small></span></button>)}{!contacts.length&&<p className="empty-small">No eligible units in the visible mission layers.</p>}</div></div>:<p className="awareness-unavailable">{view.detail}</p>}
    {(view.truncated||Boolean(view.dropped)||view.friendlyTruncated||Boolean(view.friendlyDropped)||view.notes.length>0)&&<div className="awareness-warning">{view.truncated&&<span>Partial export · {view.reported??'Unknown'} total enemies reported. </span>}{Boolean(view.dropped)&&<span>{view.dropped} records dropped. </span>}{view.friendlyTruncated&&<span>Partial friendly export · {view.friendlyReported??'Unknown'} total friendlies reported. </span>}{Boolean(view.friendlyDropped)&&<span>{view.friendlyDropped} friendly records dropped. </span>}{view.notes.map((note,index)=><span key={index}>{note} </span>)}</div>}
    <details className="awareness-source"><summary>Source · {view.source} · {age(view.age_s)}</summary><p>{view.detail}</p><p>Aircraft radar remains a separate, unverified sensor capability. This layer shows permitted single-player mission positions.</p></details>
  </section>;
}
