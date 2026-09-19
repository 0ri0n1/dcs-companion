import { useEffect, useMemo, useRef, useState } from 'react';
import './awareness.css';
import { request } from './api';
import { useDisplayPreference } from './preferences';
import { AwarenessPanel, awarenessView, EnemySymbol, trueDirection } from './MissionAwareness';
import type { RecordData, Snapshot } from './types';
import { coords, format, heading, list, navigation, number, obj, radians, recordId, recordName, text } from './utils';

export function Metric({ label, value, unit, small = false }: {label: string; value: string; unit?: string; small?: boolean}) {
  return <div className={`metric ${small ? 'small' : ''}`}><span className="eyebrow">{label}</span><div>{value}<small>{unit}</small></div></div>;
}
export function Badge({ children, tone = '' }: { children: React.ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }

function Frequency({ freq }: {freq: RecordData}) {
  const hz = number(freq.frequency_hz);
  const value = number(freq.value) ?? (hz === undefined ? undefined : hz >= 1e6 ? hz/1e6 : hz/1000);
  const unit = text(freq.unit, hz === undefined ? '' : hz >= 1e6 ? 'MHz' : 'kHz');
  return <div className="frequency"><div><strong>{format(value, unit === 'MHz' ? 3 : 0)} <small>{unit}</small></strong><span>{text(freq.modulation)} · {text(freq.band, 'Band unknown')}</span></div><span>{text(freq.purpose, 'Purpose unknown')}</span></div>;
}

export function AirfieldCard({ field, runways, selectedRunway, setRunway, advisory }: {field?: RecordData; runways: RecordData[]; selectedRunway: string; setRunway: (id: string) => void; advisory?:RecordData}) {
  const navs = list(field?.navaids), freqs = list(field?.frequencies);
  const runway = runways.find(r => recordId(r) === selectedRunway) ?? runways[0];
  const ends = list(runway?.ends);
  const provenance = obj(field?.provenance);
  const suggestion=obj(advisory?.suggestion);
  return <aside className="panel field-card"><div className="panel-header"><span className="eyebrow">DESTINATION DOSSIER</span><span className="tiny-marker">◈</span></div>
    <h2>{field ? recordName(field) : 'Select an airfield'}</h2><div className="field-sub">{field ? text(field.dcs_id ?? field.identifier ?? field.id) : 'Search or choose a symbol on the scope'}</div>
    <div className="split-metrics"><Metric label="FIELD ELEVATION" value={format(field?.elevation_m)} unit="m" small/><Metric label="POSITION" value={text(field?.position_confidence ?? provenance.confidence)} small/></div>
    <section><div className="section-label">RUNWAY <Badge>{text(field?.runway_geometry_status, runways.length ? 'Available' : 'Unknown')}</Badge></div>
      {runways.length > 0 ? <><select aria-label="Select runway" value={recordId(runway ?? {})} onChange={e => setRunway(e.target.value)}>{runways.map((r,i) => <option key={recordId(r) || i} value={recordId(r)}>{list(r.ends).map(e => text(e.designator)).join(' / ') || recordId(r)}</option>)}</select><div className="runway-ends">{ends.map((end,i) => <div key={i}><strong>{text(end.designator)}</strong><span>{heading(end.heading_true_deg)} TRUE</span><span>{heading(end.heading_mag_deg)} MAG</span><small>Threshold {coords(obj(end.threshold) || end)?.map(n => n.toFixed(4)).join(', ') ?? 'Unknown'}</small></div>)}</div><p className="muted">{format(runway?.length_m)} × {format(runway?.width_m)} m · {text(runway?.surface)}</p></> : <p className="muted">Pavement geometry is Unknown until a Terrain API export is available. Localizer course is separate.</p>}
      <p className="micro">Suitability unknown: aircraft runway requirements are not yet verified.</p>
    </section>
    <section><div className="section-label">RADIO FREQUENCIES <span>{freqs.length}</span></div>{freqs.length ? freqs.map((f,i) => <Frequency key={i} freq={f}/>) : <p className="muted">Unknown</p>}</section>
    <section><div className="section-label">NAVIGATION AIDS <span>{navs.length}</span></div>{navs.length ? navs.map((n,i) => <div className="navaid-row" key={recordId(n) || i}><div><strong>{text(n.type)}</strong><span>{text(n.callsign, 'Callsign unknown')}</span></div><div>{n.channel !== undefined && n.channel !== null ? <strong>{text(n.channel)}{text(n.mode, ' ?')} <small>TACAN</small></strong> : <strong>{format(obj(n.frequency).value, 3)} {text(obj(n.frequency).unit, '')}</strong>}{number(n.localizer_front_course_true_deg) !== undefined && <small>LOC {heading(n.localizer_front_course_true_deg)} TRUE</small>}</div></div>) : <p className="muted">No verified facility data selected.</p>}</section>
    <section><div className="section-label">MISSION WIND</div>{advisory?.status==='advisory'&&suggestion.runway?<><p className="muted">Suggested runway {text(suggestion.runway)}</p><div className="split-metrics"><Metric label="HEADWIND" value={format(suggestion.headwind_kt,1)} unit="kt" small/><Metric label="CROSSWIND FROM RIGHT" value={format(suggestion.crosswind_from_right_kt,1)} unit="kt" small/></div></>:<p className="muted">{text(advisory?.reason, 'Runway suggestion unavailable until runway geometry and mission wind are known.')}</p>}<p className="micro amber-text">Advisory only · not ATC clearance or a confirmed active runway.</p></section>
    <details><summary>Source & confidence</summary><p className="micro">{text(provenance.confidence)} · {text(provenance.method ?? provenance.extraction_method)}</p><p className="source-path">{text(provenance.source_path ?? provenance.original_source_path)}</p><dl className="step-details"><div><dt>DCS version</dt><dd>{text(provenance.dcs_version)}</dd></div><div><dt>Extracted</dt><dd>{text(provenance.extraction_date)}</dd></div><div><dt>Source modified</dt><dd>{text(provenance.source_modified_utc)}</dd></div><div><dt>Source SHA-256</dt><dd className="hash">{text(provenance.source_hash)}</dd></div><div><dt>Coordinate reference</dt><dd>{text(provenance.coordinate_reference)}</dd></div><div><dt>Mission override</dt><dd>{provenance.mission_overlay_replaced===true?'Yes':provenance.mission_overlay_replaced===false?'No':'Unknown'}</dd></div></dl>{field?.position_provenance!==undefined&&<><p className="micro">POSITION REFERENCE · {text(obj(field.position_provenance).confidence)}</p><p className="source-path">{text(obj(field.position_provenance).original_source_path)}</p></>}</details>
  </aside>;
}

export default function NavigationScope({ snapshot, usable, elapsed=0 }: {snapshot?: Snapshot; usable: boolean;elapsed?:number}) {
  const [libraryTerrain,setLibraryTerrain] = useState(''), [library,setLibrary] = useState<RecordData>(), [libraryError,setLibraryError] = useState('');
  const [selectedAidId,setSelectedAidId] = useState('');
  const liveNav = obj(snapshot?.navigation), health = snapshot?.health;
  const navigationRefresh=snapshot?.smart?.navigation_refresh;
  const libraryReady=usable&&(!navigationRefresh||navigationRefresh.status==='Current');
  const nav = libraryTerrain ? libraryReady?library??{}:{} : liveNav;
  const terrainNames = Array.isArray(liveNav.available_terrains) ? liveNav.available_terrains.map(v=>text(v,'')) : list(liveNav.coverage).map(t=>text(t.terrain,''));
  useEffect(()=>{
    let cancelled=false;setLibrary(undefined);setLibraryError('');setSelectedId('');setSelectedAidId('');setSelectedWaypoint('');setRunway('');
    if(libraryTerrain&&libraryReady)void request(`/api/navigation/${encodeURIComponent(libraryTerrain)}`).then(data=>{if(!cancelled)setLibrary(obj(data));}).catch(error=>{if(!cancelled)setLibraryError(error instanceof Error?error.message:'Library unavailable');});
    return()=>{cancelled=true;};
  },[libraryTerrain,libraryReady,navigationRefresh?.generation]);
  const own = obj(nav.ownship), aircraft = obj(snapshot?.aircraft);
  const ownPosition = !libraryTerrain && usable && health?.telemetry_fresh === true ? coords(own) ?? coords(aircraft) : undefined;
  const airfields = list(nav.airfields), navaids = list(nav.navaids), route = list(nav.route);
  const [search, setSearch] = useState(''), [selectedId, setSelectedId] = useState(''), [selectedWaypoint, setSelectedWaypoint] = useState('');
  const [orientation, setOrientation] = useDisplayPreference('orientation'), [range, setRange] = useDisplayPreference('range'), [rings, setRings] = useDisplayPreference('rings');
  const [showAids, setShowAids] = useDisplayPreference('navaids'), [showFields, setShowFields] = useDisplayPreference('airfields'), [showRoute, setShowRoute] = useDisplayPreference('route'), [selectedRunway,setRunway] = useState('');
  const [showMission,setShowMission] = useDisplayPreference('mission');
  const [showEnemies,setShowEnemies] = useDisplayPreference('enemies'),[showFriendlies,setShowFriendlies]=useDisplayPreference('friendlies'),[selectedEnemyId,setSelectedEnemyId]=useState('');
  const scopeSvg=useRef<SVGSVGElement>(null),selectionStrip=useRef<HTMLDivElement>(null);
  const [contactHitRadius,setContactHitRadius]=useState(18),[selectionRevision,revealSelection]=useState(0);
  useEffect(()=>{
    const svg=scopeSvg.current;if(!svg)return;
    const resize=new ResizeObserver(()=>{
      const scale=svg.getScreenCTM()?.a;
      if(scale&&scale>0)setContactHitRadius(Math.max(18,22/scale));
    });
    resize.observe(svg);return()=>resize.disconnect();
  },[]);
  useEffect(()=>{
    if(!selectionRevision||!window.matchMedia('(max-width: 900px)').matches)return;
    const strip=selectionStrip.current;if(!strip)return;
    const frame=requestAnimationFrame(()=>{
      const rect=strip.getBoundingClientRect(),mobileNav=document.querySelector('.mobile-view-nav')?.getBoundingClientRect();
      const bottom=Math.min(window.innerHeight,mobileNav?.height?mobileNav.top:window.innerHeight)-12;
      const top=rect.height>bottom-12?12:rect.top<12?12:bottom-rect.height;
      if(rect.top<12||rect.bottom>bottom)window.scrollBy({top:rect.top-top,behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
    });
    return()=>cancelAnimationFrame(frame);
  },[selectionRevision]);
  const enemies=awarenessView(snapshot,usable,!libraryTerrain,ownPosition,elapsed);
  const visibleContacts=[...(showEnemies?enemies.contacts:[]),...(showFriendlies?enemies.friendlies:[])];
  const selectedEnemy=visibleContacts.find(contact=>contact.id===selectedEnemyId);
  useEffect(()=>{setSelectedEnemyId('');},[health?.aircraft,health?.session,libraryTerrain,usable]);
  useEffect(()=>{if(selectedEnemyId&&!visibleContacts.some(contact=>contact.id===selectedEnemyId))setSelectedEnemyId('');},[visibleContacts,selectedEnemyId]);
  const seenFreshSession=useRef<string|undefined>(undefined);
  const freshSession=usable&&health?.telemetry_fresh&&health.session?JSON.stringify([health.session,health.mission,health.aircraft]):undefined;
  useEffect(()=>{
    if(!freshSession||freshSession===seenFreshSession.current)return;
    seenFreshSession.current=freshSession;
    setLibraryTerrain('');setSelectedId('');setSelectedAidId('');setSelectedWaypoint('');setRunway('');setSearch('');
  },[freshSession]);
  const field = airfields.find(a => recordId(a) === selectedId);
  const selectedAid = navaids.find(a=>recordId(a)===selectedAidId);
  const navAdvisory=obj(nav.runway_advisory);
  const fieldAdvisory=navAdvisory.status==='Unknown'||(field&&(recordId(obj(nav.selected))===recordId(field)||navAdvisory.airfield_id===recordId(field)))?navAdvisory:undefined;
  const filtered = airfields.filter(a => `${recordName(a)} ${recordId(a)} ${text(a.dcs_id,'')} ${list(a.navaids).map(n => `${text(n.callsign,'')} ${text(n.type,'')} ${text(n.name,'')}`).join(' ')}`.toLowerCase().includes(search.toLowerCase()));
  const filteredAids = search ? navaids.filter(a=>`${recordName(a)} ${recordId(a)} ${text(a.callsign,'')} ${text(a.type,'')}`.toLowerCase().includes(search.toLowerCase())).slice(0,30) : [];
  const speed = usable && health?.telemetry_fresh ? number(own.ground_speed_kt ?? aircraft.ground_speed_kt) : undefined;
  const trueHeading = number(own.heading_true_deg ?? aircraft.heading_deg), track = number(own.track_true_deg ?? aircraft.ground_track_deg);
  const effectiveOrientation=orientation==='track'&&ownPosition&&track!==undefined?'track':'north';
  const rotation = effectiveOrientation === 'track' ? track! : 0;
  const waypoint = route.find(r => recordId(r) === selectedWaypoint);
  const target = waypoint ?? selectedAid ?? field, targetPosition = target ? coords(target) : undefined;
  const solution = ownPosition && targetPosition ? navigation(ownPosition,targetPosition,speed) : undefined;
  const nearest = useMemo(() => ownPosition ? [...airfields].map(a => ({...a, ... (coords(a) ? navigation(ownPosition,coords(a)!,speed) : {})})).filter(a => number(a.distance_nm) !== undefined).sort((a,b) => Number(a.distance_nm)-Number(b.distance_nm)).slice(0,8) : [], [airfields, ownPosition, speed]);
  const fieldPositions = airfields.map(coords).filter((p): p is [number,number] => !!p);
  const center = ownPosition ?? (field && coords(field)) ?? (fieldPositions.length ? [fieldPositions.reduce((sum,p) => sum+p[0],0)/fieldPositions.length,fieldPositions.reduce((sum,p) => sum+p[1],0)/fieldPositions.length] as [number,number] : undefined);
  const project = (record: RecordData): [number,number] | undefined => {
    const pos = coords(record); if (!center || !pos) return undefined;
    const lonDelta = ((pos[1]-center[1]+540)%360)-180;
    const east = lonDelta*60*Math.cos(radians(center[0])), north = (pos[0]-center[0])*60;
    const x = east*Math.cos(radians(rotation))-north*Math.sin(radians(rotation));
    const y = north*Math.cos(radians(rotation))+east*Math.sin(radians(rotation));
    return [400+x/range*342,400-y/range*342];
  };
  const selectField = (a: RecordData) => { setSelectedId(recordId(a)); setSelectedAidId(''); setSelectedWaypoint(''); setSelectedEnemyId(''); setRunway(''); revealSelection(value=>value+1); };
  const selectAid = (a: RecordData) => {setSelectedAidId(recordId(a));setSelectedId('');setSelectedWaypoint('');setSelectedEnemyId('');revealSelection(value=>value+1);};
  const selectContact = (id:string) => {setSelectedEnemyId(id);setSelectedId('');setSelectedAidId('');setSelectedWaypoint('');setRunway('');revealSelection(value=>value+1);};
  const missionObjects = ['carriers','farps','mobile_sources'].flatMap(key => list(obj(nav.overlay)[key]));
  const plottedEnemies=visibleContacts.flatMap(contact=>{const point=project(contact);return point&&contact.distance_nm<=range&&Math.hypot(point[0]-400,point[1]-400)<=342?[{contact,point}]:[];});
  const enemyIdsInRange=new Set(plottedEnemies.map(item=>item.contact.id));
  return <div className={`navigation-layout ${showEnemies||showFriendlies?'awareness-enabled':''}`}>
    <aside className="panel destination-list"><div className="panel-header"><span className="eyebrow">FLIGHT DIRECTORY</span><Badge>{text(nav.terrain ?? health?.terrain, 'No terrain')}</Badge></div><label className="library-selector"><span className="eyebrow">TERRAIN SOURCE</span><select aria-label="Terrain source" value={libraryTerrain} onChange={e=>{if(freshSession)seenFreshSession.current=freshSession;setLibraryTerrain(e.target.value);setSelectedId('');setSelectedAidId('');setSelectedWaypoint('');}}><option value="">Automatic · active flight</option>{terrainNames.map(name=><option key={name} value={name}>{name} · offline library</option>)}</select></label>{libraryTerrain&&<p className="micro library-note">Manual library view · ownship guidance is disabled.</p>}{libraryError&&<p className="error micro" role="alert">{libraryError}</p>}<label className="search-box"><span>⌕</span><input value={search} onChange={e => setSearch(e.target.value)} placeholder="Airfield, callsign, navaid…" aria-label="Search airfields"/></label>
      <div className="list-heading"><span>{search ? 'SEARCH RESULTS' : 'AIRFIELDS'}</span><span>{filtered.length.toString().padStart(2,'0')}</span></div>
      <div className="airfield-scroll">{filtered.length ? filtered.map(a => <button className={`airfield-item ${field === a ? 'selected' : ''}`} key={recordId(a)} onClick={() => selectField(a)}><span className="airfield-glyph">⊕</span><span><strong>{recordName(a)}</strong><small>{text(a.dcs_id ?? a.identifier, 'DCS identifier unknown')}</small></span><span className="chevron">›</span></button>) : <div className="empty-small">{search ? 'No matching airfields.' : 'Airfields appear when the active terrain is identified. No cross-terrain fallback is used.'}</div>}</div>
      {filteredAids.length>0&&<><div className="list-heading"><span>NAVAID RESULTS</span><span>{filteredAids.length}</span></div><div className="navaid-search-results">{filteredAids.map(a=><button className={`airfield-item ${selectedAid===a?'selected':''}`} key={recordId(a)} onClick={()=>selectAid(a)}><span className="airfield-glyph">◇</span><span><strong>{recordName(a)}</strong><small>{text(a.type)} · {text(a.callsign)}</small></span></button>)}</div></>}
      <div className="list-heading nearest-heading"><span>NEAREST AIRFIELDS</span><span>NM</span></div>{nearest.length ? nearest.slice(0,5).map(a => <button className="nearest-row" key={recordId(a)} onClick={() => selectField(a)}><span>{recordName(a)}<small>{heading(a.bearing_true_deg)} TRUE</small></span><strong>{format(a.distance_nm,1)}</strong></button>) : <p className="empty-small">Fresh ownship position is required.</p>}
      <div className="directory-note"><span className="dot"/>LOCAL TERRAIN DATABASE<span>No internet or map tiles required</span></div>
    </aside>
    <section className="scope-column"><div className="scope-title"><div><span className="eyebrow">SHARED NAVIGATION ENGINE</span><h1>Navigation Scope<span className="title-period">.</span></h1></div><Badge tone={ownPosition ? 'green' : 'amber'}>{ownPosition ? 'OWNSHIP LIVE' : 'POSITION UNAVAILABLE'}</Badge></div>
      <div className="flight-metrics"><Metric label="HEADING · TRUE" value={ownPosition ? heading(trueHeading) : '—'}/><Metric label="TRACK · TRUE" value={ownPosition ? heading(track) : '—'}/><Metric label="GROUND SPEED" value={format(speed)} unit="kt"/><Metric label="ALTITUDE · MSL" value={ownPosition ? format(own.altitude_ft ?? aircraft.altitude_msl_ft) : '—'} unit="ft"/></div>
      <div className="scope-panel"><div className="scope-toolbar"><div className="segmented"><button aria-pressed={effectiveOrientation === 'north'} onClick={() => setOrientation('north')}>North up</button><button aria-pressed={effectiveOrientation === 'track'} onClick={() => setOrientation('track')} disabled={!ownPosition || track === undefined}>Track up</button></div><label className="range-label">RANGE <select aria-label="Scope range" value={range} onChange={e => setRange(Number(e.target.value))}>{[10,20,40,80,160,320].map(r => <option key={r} value={r}>{r} NM</option>)}</select></label></div>
        <div className="scope-canvas"><svg ref={scopeSvg} viewBox="0 0 800 800" role="img" aria-label={`Navigation Scope, ${range} nautical mile range, ${effectiveOrientation} up`} onClickCapture={event=>{
          if(!(event.target instanceof Element)||!event.target.matches('.enemy-hit,.contact-touch-hit')||event.detail===0)return;
          const matrix=event.currentTarget.getScreenCTM();if(!matrix)return;
          // Overlapping touch circles must select the nearest aircraft, not the last one drawn.
          const point=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
          const closest=plottedEnemies.map(item=>({...item,distance:Math.hypot(item.point[0]-point.x,item.point[1]-point.y)})).sort((a,b)=>a.distance-b.distance)[0];
          if(closest&&closest.distance<=contactHitRadius){event.stopPropagation();selectContact(closest.contact.id);}
        }}><defs><radialGradient id="scopeGlow"><stop offset="0" stopColor="#152522"/><stop offset="1" stopColor="#0b1417"/></radialGradient><clipPath id="scopeClip"><circle cx="400" cy="400" r="352"/></clipPath></defs><circle cx="400" cy="400" r="352" fill="url(#scopeGlow)" stroke="#304139"/>
          <g className="scope-grid">{rings && [1,2,3,4].map(r => <circle key={r} cx="400" cy="400" r={r*85.5}/>)}<path d="M400 48V752M48 400H752"/>{Array.from({length:72},(_,i) => <line key={i} x1="400" y1={i%6 === 0 ? 49 : 53} x2="400" y2={i%6 === 0 ? 65 : 60} transform={`rotate(${i*5-rotation} 400 400)`}/>)}</g>
          {[0,90,180,270].map((h,i) => { const a=radians(h-rotation); return <text key={h} x={400+375*Math.sin(a)} y={405-375*Math.cos(a)} className="compass-label">{['N','E','S','W'][i]}</text>; })}
          <g clipPath="url(#scopeClip)">
          {/* Extra touch area sits below visible symbols so it cannot cover airport/navaid controls. */}
          {plottedEnemies.map(({contact,point})=><circle key={contact.id} className="contact-touch-hit" data-contact-id={contact.id} cx={point[0]} cy={point[1]} r={contactHitRadius} aria-hidden="true" onClick={()=>selectContact(contact.id)}/>)}
          {showRoute && <polyline className="route-line" points={route.map(project).filter(Boolean).map(p => p!.join(',')).join(' ')}/>}
          {ownPosition && target && project(target) && <line className="direct-line" x1="400" y1="400" x2={project(target)![0]} y2={project(target)![1]}/>}
          {showAids && navaids.map((n,i) => { const p=project(n); return p && <g className="navaid-symbol" key={recordId(n)||i} transform={`translate(${p[0]},${p[1]})`} role="button" tabIndex={0} aria-label={`Select navaid ${recordName(n)}`} onClick={()=>selectAid(n)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectAid(n);}}}><circle r="17" fill="transparent" stroke="none"/><path d="M0 -5 5 0 0 5 -5 0Z"/><title>{recordName(n)} · {text(n.type)} · {text(n.callsign)}</title></g>; })}
          {showFields && airfields.map((a,i) => { const p=project(a); return p && <g className={`field-symbol ${field === a ? 'active' : ''}`} role="button" tabIndex={0} aria-label={`Select ${recordName(a)}`} onClick={() => selectField(a)} onKeyDown={e => { if(e.key==='Enter'||e.key===' ') { e.preventDefault(); selectField(a); } }} key={recordId(a)||i} transform={`translate(${p[0]},${p[1]})`}><circle className="hit-target" r="20"/><circle r={field === a ? 10 : 6}/><path d="M-10 0H10M0 -10V10"/><text x="15" y="5">{recordName(a)}</text></g>; })}
          {showFields && airfields.flatMap(a => list(a.runways).map((r,i) => {const e=list(r.ends).map(end => project({...end,...obj(end.threshold)})).filter(Boolean);return e.length>=2 && <line className="runway-line" key={`${recordId(a)}-${i}`} x1={e[0]![0]} y1={e[0]![1]} x2={e[1]![0]} y2={e[1]![1]}/>;}))}
          {showRoute && route.map((r,i) => {const p=project(r);return p && <g className={`waypoint-symbol ${waypoint===r ? 'active' : ''}`} key={recordId(r)||i} transform={`translate(${p[0]},${p[1]})`}><path d="M0 -8 8 0 0 8 -8 0Z"/><text x="13" y="4">{recordName(r)}</text></g>;})}
          {showMission && missionObjects.map((r,i) => {const p=project(r);return p && <g className="mission-symbol" key={recordId(r)||i} transform={`translate(${p[0]},${p[1]})`}><rect x="-7" y="-7" width="14" height="14"/><text x="13" y="4">{recordName(r)}</text></g>;})}
          {plottedEnemies.map(({contact,point},index)=><EnemySymbol key={contact.id} contact={contact} point={point} rotation={rotation} selected={selectedEnemy?.id===contact.id} onSelect={()=>selectContact(contact.id)} label={selectedEnemy?.id===contact.id||index<4} />)}
          {ownPosition && <g className="ownship-graphics">{track !== undefined && <g transform={`rotate(${track-rotation} 400 400)`}><line className="track-vector" x1="400" y1="400" x2="400" y2="268"/><circle className="track-vector" cx="400" cy="266" r="5"/></g>}{trueHeading !== undefined && <g transform={`rotate(${trueHeading-rotation} 400 400)`}><line className="heading-vector" x1="400" y1="385" x2="400" y2="320"/><path className="ownship" d="M400 380 413 415 400 407 387 415Z"/></g>}</g>}
          </g>{rings && [1,2,3,4].map(r => <text key={r} x="409" y={400+r*85.5-6} className="ring-label">{range*r/4}</text>)}
          {!center && <g className="scope-empty"><text x="400" y="350">AWAITING NAVIGATION DATA</text><text x="400" y="378">Start DCS and enter a cockpit</text><text x="400" y="435">Static navigation stays available offline</text></g>}
        </svg><div className="scope-corner top-left"><span>{text(nav.terrain, 'TERRAIN UNKNOWN').toUpperCase()}</span><small>{ownPosition ? 'OWN-SHIP CENTERED' : center ? 'STATIC CHART · NO OWNSHIP' : 'WAITING FOR TERRAIN'}</small></div><div className="scope-corner bottom-left"><span className="green-text">— HEADING</span><span className="cyan-text">┄ GROUND TRACK</span></div><div className="scope-corner bottom-right"><span>{range} NM</span><small>SCHEMATIC · WGS84</small></div></div>
        <div className="declutter" role="group" aria-label="Scope layers"><span className="eyebrow">LAYERS</span>{[['Airfields',showFields,setShowFields],['Navaids',showAids,setShowAids],['Route',showRoute,setShowRoute],['Mission',showMission,setShowMission],['Friendlies',showFriendlies,setShowFriendlies,'Mission friendlies'],['Enemies',showEnemies,setShowEnemies,'Mission enemies'],['Rings',rings,setRings]].map(([label,on,set,accessibleLabel]) => <button key={String(label)} aria-label={accessibleLabel as string|undefined} aria-pressed={Boolean(on)} onClick={() => (set as (v:boolean)=>void)(!on)}><span className={`toggle-dot ${on ? 'on':''}`} aria-hidden="true"/>{String(label)}</button>)}</div>
      </div>
      <div ref={selectionStrip} className="navigation-solution" role="region" aria-label="Selected scope item" data-selection-kind={selectedEnemy?'contact':target?'navigation':'none'}>
        <div className="solution-title"><span className="eyebrow">{waypoint?'SELECTED WAYPOINT':'DIRECT TO · DISPLAY ONLY'}</span><strong>{selectedEnemy?selectedEnemy.name:target?recordName(target):'No destination selected'}</strong>{selectedEnemy&&<small>{selectedEnemy.affiliation==='friendly'?'Friendly':'Enemy'} · {text(selectedEnemy.type)}{trueDirection(selectedEnemy.track_true_deg)!==undefined?` · Track ${heading(trueDirection(selectedEnemy.track_true_deg))} true`:''}{!enemyIdsInRange.has(selectedEnemy.id)?' · Outside scope range':''}</small>}</div>
        <Metric label="BEARING · TRUE" value={heading(selectedEnemy?.bearing_true_deg??solution?.bearing_true_deg)} small/>
        <Metric label="DISTANCE" value={format(selectedEnemy?.distance_nm??solution?.distance_nm,1)} unit="NM" small/>
        {selectedEnemy?<>
          <Metric label="HEADING · TRUE" value={heading(trueDirection(selectedEnemy.heading_true_deg))} small/>
          <Metric label="GROUND SPEED" value={format(selectedEnemy.speed_kt)} unit="kt" small/>
          <Metric label="ALTITUDE · MSL" value={format(selectedEnemy.altitude_ft)} unit="ft" small/>
        </>:<>
          <Metric label="DESIRED COURSE" value={heading(target?.desired_course_true_deg)} unit="TRUE" small/>
          <Metric label="ETA · GROUND SPEED" value={format(solution?.eta_minutes,1)} unit="min" small/>
          <Metric label="CROSS-TRACK" value={format(target?.cross_track_nm,2)} unit="NM" small/>
        </>}
      </div>
      <div className="route-strip"><span className="eyebrow">MISSION ROUTE</span>{route.length ? route.map((r,i) => <button className={waypoint === r ? 'selected':''} key={recordId(r)||i} onClick={() => {setSelectedWaypoint(recordId(r));setSelectedEnemyId('');setSelectedId('');setSelectedAidId('');setRunway('');}}>{String(i+1).padStart(2,'0')} · {recordName(r)}</button>) : <span className="muted">No verified active mission route</span>}<Badge>Sequencing unavailable</Badge></div>
      {showEnemies||showFriendlies?<AwarenessPanel view={enemies} selected={selectedEnemy} onSelect={selectContact} inRange={enemyIdsInRange} showEnemies={showEnemies} showFriendlies={showFriendlies}/>:<div className="sensor-note"><span>◇</span><span><strong>Aircraft radar · {text(health?.sensor_status, 'Unverified')}</strong>Friendly and enemy mission units are hidden. Use the scope layer controls to show them.</span></div>}
    </section>{selectedAid?<aside className="panel field-card"><div className="panel-header"><span className="eyebrow">NAVIGATION FACILITY</span><Badge>{text(selectedAid.type)}</Badge></div><h2>{recordName(selectedAid)}</h2><div className="field-sub">{text(selectedAid.callsign,'Callsign unknown')}</div><section><div className="section-label">FREQUENCY / CHANNEL</div>{selectedAid.channel!==null&&selectedAid.channel!==undefined?<Metric label="TACAN CHANNEL / MODE" value={`${text(selectedAid.channel)}${text(selectedAid.mode,' ?')}`} small/>:<Frequency freq={obj(selectedAid.frequency)}/>}<p className="micro">Mode confidence: {text(selectedAid.mode_confidence??obj(selectedAid.provenance).confidence)}</p></section>{number(selectedAid.localizer_front_course_true_deg)!==undefined&&<section><div className="section-label">LOCALIZER FRONT COURSE</div><Metric label="TRUE · NOT PAVEMENT CENTERLINE" value={heading(selectedAid.localizer_front_course_true_deg)} small/></section>}<section><div className="section-label">POSITION</div><p className="muted">{coords(selectedAid)?.map(n=>n.toFixed(5)).join(', ')??'Unknown'}</p><p className="micro">{text(obj(selectedAid.provenance).confidence)} · {text(obj(selectedAid.provenance).method??obj(selectedAid.provenance).extraction_method)}</p><p className="source-path">{text(obj(selectedAid.provenance).source_path??obj(selectedAid.provenance).original_source_path)}</p></section></aside>:<AirfieldCard field={field} advisory={fieldAdvisory} runways={list(field?.runways)} selectedRunway={selectedRunway} setRunway={setRunway}/>}
  </div>;
}
