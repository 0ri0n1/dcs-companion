import { useEffect, useState } from 'react';
import { request } from './api';
import { Badge } from './Scope';
import type { RecordData, Snapshot } from './types';
import { bindingText, list, obj, recordId, recordName, text } from './utils';
import { useDeviceLayout, useWorkspace } from './workspace';
const keyRows = [ ['Esc','F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12'],['`','1','2','3','4','5','6','7','8','9','0','-','=','Back'],['Tab','Q','W','E','R','T','Y','U','I','O','P','[',']','\\'],['CapsLock','A','S','D','F','G','H','J','K','L',';',"'",'Enter'],['LShift','Z','X','C','V','B','N','M',',','.','/','RShift'],['LCtrl','LWin','LAlt','Space','RAlt','RWin','RCtrl'] ];

export default function Controls({ snapshot, usable }: {snapshot?: Snapshot;usable:boolean}) {
  const [manualAircraft,setManualAircraft]=useState(''),[catalogue,setCatalogue]=useState<RecordData>(),[catalogueError,setCatalogueError]=useState('');
  const [manualContext,setManualContext]=useState<string>();
  const activeContext=usable&&snapshot?.health?.telemetry_fresh&&snapshot.health.aircraft?JSON.stringify([snapshot.health.aircraft,snapshot.health.session]):undefined;
  const useManual=Boolean(manualAircraft&&(!activeContext||activeContext===manualContext));
  const catalogs=snapshot?.binding_catalogs??[];
  const catalogMetadata=catalogs.find(c=>c.aircraft===manualAircraft);
  const liveBindings=obj(snapshot?.bindings);
  const activeMatch=!activeContext||liveBindings.aircraft===snapshot?.health?.aircraft;
  const bindings=useManual?(catalogue?.aircraft===manualAircraft?catalogue:{}):activeMatch?liveBindings:{aircraft:snapshot?.health?.aircraft,status:'Unavailable',reason:'Waiting for this aircraft’s own binding catalogue.'};
  const catalogueValid=!['Invalidated','Unavailable','Error','Refreshing','Waiting','Unsupported'].includes(text(bindings.status));
  const actions = catalogueValid?list(bindings.actions):[], devices = list(bindings.devices);
  const [search,setSearch] = useState(''), [key,setKey] = useState(''), [filter,setFilter] = useState('all');
  const [deviceId,setDeviceId]=useState(''),[category,setCategory]=useState(''),[limit,setLimit]=useState(100);
  const deviceLayout=useDeviceLayout();const [workspace,setWorkspace]=useWorkspace(text(bindings.aircraft,'unknown'),deviceLayout);
  const favouriteId=(a:RecordData)=>`${text(a.device_id)}:${recordId(a)}`;
  const toggleFavourite=(a:RecordData)=>{const id=favouriteId(a);setWorkspace({...workspace,favourites:workspace.favourites.includes(id)?workspace.favourites.filter(item=>item!==id):[...workspace.favourites,id].slice(-100)});};
  useEffect(()=>setLimit(100),[search,key,filter,deviceId,category,bindings.aircraft]);
  useEffect(()=>{
    if(activeContext&&manualAircraft&&activeContext!==manualContext){setManualAircraft('');setManualContext(undefined);setSearch('');setKey('');setFilter('all');}
  },[activeContext,manualAircraft,manualContext]);
  useEffect(()=>{
    let cancelled=false;setCatalogue(undefined);setCatalogueError('');
    if(manualAircraft&&useManual&&usable)void request(`/api/bindings?aircraft=${encodeURIComponent(manualAircraft)}`).then(data=>{
      if(cancelled)return;
      if(data.aircraft!==manualAircraft){setCatalogueError('The returned catalogue does not match the selected aircraft. No controls are shown.');return;}
      setCatalogue(obj(data));
    }).catch(error=>{if(!cancelled)setCatalogueError(error instanceof Error?error.message:'Catalogue unavailable');});
    return()=>{cancelled=true;};
  },[manualAircraft,useManual,usable,catalogMetadata?.status,catalogMetadata?.source_fingerprint,catalogMetadata?.generated_utc,catalogMetadata?.generation]);
  const overlaps = (a:RecordData, kind:'conflicts'|'contextual_overlaps')=>list(bindings[kind]).filter(c=>c.device_id===a.device_id && Array.isArray(a.combos) && a.combos.includes(c.combo));
  const filtered = actions.filter(a => `${recordName(a)} ${bindingText(a.combos)} ${text(a.device_id,'')}`.toLowerCase().includes(search.toLowerCase()) && (!key || (Array.isArray(a.combos) && a.combos.some(c => String(c).split(/[+]/).map(s=>s.trim()).includes(key)))) && (filter !== 'conflicts' || overlaps(a,'conflicts').length) && (filter !== 'unbound' || !Array.isArray(a.combos) || !a.combos.length) && (filter!=='favourites'||workspace.favourites.includes(favouriteId(a))) && (!category||(Array.isArray(a.category)?a.category:[a.category]).includes(category)));
  const keyboard = devices.find(d => d.type === 'keyboard');
  const keyboardActions = actions.filter(a => a.device_id === keyboard?.id);
  const actionForKey = (k: string) => keyboardActions.filter(a => Array.isArray(a.combos) && a.combos.some(c => String(c) === k));
  const columns = devices.length ? devices : [{id:'keyboard',name:'Keyboard',type:'keyboard'},{id:'tartarus',name:'Tartarus Pro',type:'tartarus'},{id:'hotas',name:'HOTAS / other devices',type:'hotas'}];
  const selectedDevice=columns.find(d=>d.id===deviceId)??columns[0];
  const visibleActions=filtered.filter(a=>a.device_id===selectedDevice.id);
  const categories=[...new Set(actions.flatMap(a=>Array.isArray(a.category)?a.category.map(String):[text(a.category,'Uncategorized')]))].sort();
  const categoryClass=(a:RecordData)=>{const name=Array.isArray(a.category)?a.category.join(' '):text(a.category,'');return /nav|radio|communication/i.test(name)?'category-nav':/weapon|counter|sensor/i.test(name)?'category-weapons':/engine|system|electrical/i.test(name)?'category-systems':/flight|autopilot/i.test(name)?'category-flight':'';};
  return <div className="page-view controls-reference"><div className="page-title"><div><span className="eyebrow">AIRCRAFT-SPECIFIC BINDING TRUTH</span><h1>Controls<span className="title-period">.</span></h1><p>Module defaults merged with Saved Games changes, separated by device identity.</p></div><Badge>{text(bindings.status, 'Unavailable')}</Badge></div>
    <div className="catalogue-toolbar panel"><label><span className="eyebrow">AIRCRAFT CATALOGUE</span><select aria-label="Binding aircraft" value={useManual?manualAircraft:''} onChange={event=>{setManualAircraft(event.target.value);setManualContext(activeContext);setSearch('');setKey('');setFilter('all');}}><option value="">Automatic · current aircraft</option>{catalogs.map(c=><option key={c.aircraft} value={c.aircraft}>{c.label??c.aircraft} · browse controls</option>)}</select></label><div><span className="eyebrow">SHOWING BINDINGS FOR</span><strong>{text(bindings.aircraft,useManual?manualAircraft:'Unknown aircraft')}</strong><span>{useManual?'Manual catalogue · browsing only; guide and commands remain tied to the active aircraft.':activeContext?'Active aircraft · automatic selection':'Offline catalogue · no live aircraft controls are implied.'}</span></div></div>
    {catalogueError&&useManual&&<p className="notice error" role="alert">{catalogueError}</p>}{bindings.reason!==undefined&&<p className="notice">{text(bindings.reason)}</p>}


    <div className="binding-toolbar"><label className="search-box"><span>⌕</span><input aria-label="Search bindings" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search actions or combinations…"/></label><div className="segmented">{['all','favourites','conflicts','unbound'].map(f => <button key={f} aria-pressed={filter===f} onClick={()=>setFilter(f)}>{f[0].toUpperCase()+f.slice(1)}</button>)}</div>{key && <button className="plain-button" onClick={()=>setKey('')}>Key: {key} ×</button>}<span className="muted">{visibleActions.length} actions on this device</span></div>
    <div className="reference-selectors"><label>Device<select aria-label="Binding device" value={String(selectedDevice.id)} onChange={event=>setDeviceId(event.target.value)}>{columns.map((device,index)=><option key={recordId(device)||index} value={String(device.id)}>{recordName(device).replace(/\s*\{[^}]+\}/g,'')}</option>)}</select></label><label>Category<select aria-label="Binding category" value={category} onChange={event=>setCategory(event.target.value)}><option value="">All categories</option>{categories.map(item=><option key={item}>{item}</option>)}</select></label></div>
    <div className="device-columns"><section className="panel device-column"><div className="device-title"><span className="eyebrow">{text(selectedDevice.type,'DEVICE').toUpperCase()}</span><h2>{recordName(selectedDevice).replace(/\s*\{[^}]+\}/g,'')}</h2><Badge>Connection {text(selectedDevice.connected)}</Badge><details><summary>Device identity</summary><p className="micro">{text(selectedDevice.guid??selectedDevice.id,'Identity unavailable')}</p></details></div><div className="device-actions">{visibleActions.slice(0,limit).map((a,i)=><div className={`binding-row ${categoryClass(a)}`} key={`${recordId(a)}-${i}`}><button className="binding-favourite" aria-label={`Favourite ${recordName(a)}`} aria-pressed={workspace.favourites.includes(favouriteId(a))} onClick={()=>toggleFavourite(a)}>{workspace.favourites.includes(favouriteId(a))?'★':'☆'}</button><div><strong>{recordName(a)}</strong><span>{Array.isArray(a.category)?a.category.join(' / '):text(a.category,'Uncategorized')}</span>{overlaps(a,'conflicts').length>0&&<Badge tone="red">Conflict · {overlaps(a,'conflicts').map(c=>text(c.combo)).join(', ')}</Badge>}{overlaps(a,'contextual_overlaps').length>0&&<Badge tone="amber">Context-dependent overlap</Badge>}</div><kbd>{bindingText(a.combos)}</kbd></div>)}{!visibleActions.length&&<p className="empty-small">No resolved actions in this view.</p>}</div>{visibleActions.length>limit&&<button className="reference-more" onClick={()=>setLimit(limit+100)}>Show next 100 actions ({visibleActions.length-limit} remaining)</button>}</section></div>
    <details className="reference-diagrams"><summary>Keyboard and device diagrams · inspection only</summary>
    <div className="control-visuals"><section className="panel keyboard-panel"><div className="panel-header"><span className="eyebrow">KEYBOARD</span><Badge>{keyboard ? 'Resolved DCS bindings' : 'Bindings unavailable'}</Badge></div><div className="keyboard">{keyRows.map((row,i) => <div className="keyboard-row" key={i}>{row.map(k => {const acts=actionForKey(k),conflict=acts.some(a=>overlaps(a,'conflicts').length>0);return <button key={k} className={`keycap ${k==='Space'?'space':''} ${key===k?'selected':''} ${acts.length?'bound':''} ${conflict?'conflict':''}`} title={acts.length ? acts.map(recordName).join(' / ') : 'No resolved direct binding'} onClick={() => setKey(key === k ? '' : k)}><span>{k}</span><small>{acts.length ? recordName(acts[0]).slice(0,19) : '—'}</small></button>;})}</div>)}</div><p className="micro">Select a key to inspect its bindings. This diagram never sends input.</p></section>
      <section className="panel tartarus-panel"><div className="panel-header"><span className="eyebrow">TARTARUS PRO</span><Badge tone="amber">Physical map unknown</Badge></div><div className="tartarus-grid">{Array.from({length:19},(_,i) => <div className="tartarus-key" key={i}><span>{String(i+1).padStart(2,'0')}</span><small>Unknown</small></div>)}</div><div className="thumb-controls"><span>✥<small>8-way thumbpad</small></span><span>↕<small>Wheel</small></span><span>▱<small>Thumb paddle</small></span></div><p className="micro">DCS-side JOY bindings appear below. Physical keycap and Synapse layer mapping needs verification.</p></section></div></details>

    <div className="input-legend"><span><i className="legend-circle"/>Physical input: {text(obj(bindings.physical_input).status, 'Unavailable')}</span><span><i className="legend-circle sent"/>Companion command: sender audit only</span><span><i className="legend-circle observed"/>Aircraft state: observed, actor unknown</span></div>
    <div className="panel footnote-panel"><strong>Physical-input highlighting is unavailable.</strong><p>A verified Windows Raw Input listener is required. Browser key events and telemetry are never presented as physical device events. HTML binding exports are not used as binding truth.</p></div>
  </div>;
}
