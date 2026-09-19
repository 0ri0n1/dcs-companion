import { useRef, useState } from 'react';
import { isLoopbackHost, request } from './api';
import type { SetupCandidate, SetupInventoryItem, SetupState } from './types';

function itemName(item:SetupInventoryItem|string) {
  return typeof item==='string'?item:item.label??item.name??(typeof item.aircraft==='string'?item.aircraft:undefined)??item.id??'Unidentified item';
}
function count(value:unknown) {return typeof value==='number'&&Number.isFinite(value)&&value>=0?value.toLocaleString():'Unknown';}
function unbound(total:unknown,bound:unknown) {return typeof total==='number'&&typeof bound==='number'?count(total-bound):'Unknown';}
function DeviceCard({device}:{device:SetupInventoryItem|string}) {
  const evidence=typeof device==='string'?[]:device.evidence??device.diff_files?.map(file=>`${file.aircraft} · ${file.path}`)??[];
  return <article className="setup-device"><strong>{itemName(device)}</strong><span className="badge">Connection Unknown</span>{typeof device!=='string'&&device.type&&<span className="setup-device-type">{device.type}{Array.isArray(device.aircraft)?` · ${device.aircraft.length} aircraft profiles`:''}</span>}{!!evidence.length&&<details><summary>Saved profile evidence</summary><ul>{evidence.map((item,index)=><li className="setup-path" key={index}>{item}</li>)}</ul></details>}</article>;
}
function PathChoice({title,id,setId,candidates,busy}:{title:string;id:string;setId:(id:string)=>void;candidates:SetupCandidate[];busy:boolean}) {
  const selected=candidates.find(candidate=>candidate.id===id);
  return <div className="setup-choice"><label>{title}<select aria-label={title} value={id} disabled={busy||!candidates.length} onChange={event=>setId(event.target.value)}><option value="">{candidates.length?'Choose a detected location':`No ${title.toLowerCase()} found`}</option>{candidates.map(candidate=><option key={candidate.id} value={candidate.id} disabled={candidate.valid===false}>{candidate.name}{candidate.valid===false?' · unavailable':''}</option>)}</select></label><p className="setup-path">{selected?.path??'No location selected.'}</p>{selected?.evidence?.length?<details><summary>How this location was found</summary><ul>{selected.evidence.map((evidence,index)=><li key={index}>{evidence}</li>)}</ul></details>:null}</div>;
}
function InventoryList({title,items}:{title:string;items:Array<SetupInventoryItem|string>}) {
  return <details className="setup-inventory-list"><summary>{title}<span>{items.length}</span></summary>{items.length?<ul>{items.map((item,index)=><li key={typeof item==='string'?`${item}-${index}`:`${item.id??itemName(item)}-${index}`}><strong>{itemName(item)}</strong>{typeof item!=='string'&&item.path&&<span className="setup-path">{item.path}</span>}</li>)}</ul>:<p>None reported in the selected locations.</p>}</details>;
}

export default function Setup({connected}:{connected:boolean}) {
  const dialog=useRef<HTMLDialogElement>(null),trigger=useRef<HTMLButtonElement>(null),generation=useRef(0);
  const [setup,setSetup]=useState<SetupState>(),[installId,setInstallId]=useState(''),[profileId,setProfileId]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  const local=isLoopbackHost(window.location.hostname);
  function accept(data:SetupState) {
    if(!data||!data.selected||!data.inventory||!Array.isArray(data.install_candidates)||!Array.isArray(data.profile_candidates))throw new Error('The companion returned incomplete setup information. Refresh to try again.');
    setSetup(data);setInstallId(data.selected.install_id??'');setProfileId(data.selected.profile_id??'');
  }
  async function load(refresh=false) {
    const ticket=++generation.current;setBusy(true);setError('');setMessage('');setSetup(undefined);
    try {const data:SetupState=await request(`/api/setup${refresh?'?refresh=true':''}`);if(ticket===generation.current)accept(data);}
    catch(error) {if(ticket===generation.current)setError(error instanceof Error?error.message:'Setup information is unavailable. Try refreshing.');}
    finally {if(ticket===generation.current)setBusy(false);}
  }
  function open() {dialog.current?.showModal();void load();}
  function close() {generation.current++;setBusy(false);dialog.current?.close();trigger.current?.focus();}
  const valid=Boolean(setup?.install_candidates.some(candidate=>candidate.id===installId&&candidate.valid!==false)&&setup?.profile_candidates.some(candidate=>candidate.id===profileId&&candidate.valid!==false));
  const changed=Boolean(setup&&(installId!==setup.selected.install_id||profileId!==setup.selected.profile_id));
  async function save() {
    if(!setup||setup.selection_locked||!local||!valid||!changed||busy)return;
    const ticket=++generation.current;setBusy(true);setError('');setMessage('');
    try {
      await request('/api/setup/selection',{install_id:installId,profile_id:profileId});
      const data:SetupState=await request('/api/setup');
      if(ticket===generation.current){accept(data);setMessage(data.pending_restart?'Selection saved. Restart the companion to use these locations.':'Selection saved. The companion is using these locations.');}
    } catch(error) {if(ticket===generation.current)setError(error instanceof Error?error.message:'The selection could not be saved. Refresh to check the current setup.');}
    finally {if(ticket===generation.current)setBusy(false);}
  }
  const installed=setup?.inventory?.installed_aircraft??[],saved=setup?.inventory?.saved_aircraft??[],devices=setup?.inventory?.devices??[];
  return <>
    <button className="setup-button" ref={trigger} aria-haspopup="dialog" disabled={!connected} title={connected?undefined:'Waiting for the companion connection'} onClick={open}><span aria-hidden="true">⚙</span> Your setup</button>
    <dialog ref={dialog} className="setup-dialog" aria-labelledby="setup-title" onCancel={event=>{event.preventDefault();close();}}>
      <div className="setup-heading"><div><span className="eyebrow">YOUR DCS COMPUTER</span><h2 id="setup-title">Your setup</h2></div><button aria-label="Close your setup" onClick={close}>×</button></div>
      <p className="setup-intro">Learns your existing controls. DCS bindings are not changed.</p>
      <div className="setup-toolbar"><span className={`badge ${setup?.status==='Ready'?'green':'amber'}`}>{busy?'Checking setup…':setup?.status??'Unavailable'}</span><button onClick={()=>void load(true)} disabled={busy}>Refresh setup</button></div>
      {error&&<p role="alert" className="setup-alert">{error}</p>}{message&&<p role="status" className="setup-success">{message}</p>}
      {setup&&<>
        {!!setup.issues?.length&&<section className="setup-issues" aria-label="Setup needs attention"><h3>Needs attention</h3><ul>{setup.issues.map((issue,index)=><li key={index}>{issue}</li>)}</ul></section>}
        <section className="setup-section"><h3>DCS locations</h3><p>Choose where the companion reads your aircraft and saved control profiles.</p><div className="setup-location-grid"><PathChoice title="DCS installation" id={installId} setId={setInstallId} candidates={setup.install_candidates??[]} busy={busy||Boolean(setup.selection_locked)||!local} /><PathChoice title="Saved Games profile" id={profileId} setId={setProfileId} candidates={setup.profile_candidates??[]} busy={busy||Boolean(setup.selection_locked)||!local} /></div>
          <div className="setup-save"><button className="primary-button" disabled={busy||setup.selection_locked||!local||!valid||!changed} onClick={()=>void save()}>{busy?'Working…':'Save selection'}</button><p>{setup.selection_locked?'Locations were set when the companion started. Change that startup configuration to choose different locations.':local?'Saved for this companion. Location changes take effect after the companion restarts.':'To change these locations, open the companion on your DCS computer. This device can view the detected setup.'}</p></div>
          {setup.pending_restart&&<p className="setup-alert" role="status">Restart needed · the companion is still using its previous locations. Aircraft and device discovery shows your saved selection; control catalogues below describe the locations in use now.</p>}
          {setup.active&&<details className="setup-active"><summary>Locations in use now</summary><dl><div><dt>DCS installation</dt><dd>{setup.active.install_path??'Unavailable'}</dd></div><div><dt>Saved Games profile</dt><dd>{setup.active.profile_path??'Unavailable'}</dd></div></dl></details>}
        </section>
        <section className="setup-section"><div className="setup-section-heading"><h3>Aircraft found</h3><span className="badge">{installed.length} installed · {saved.length} saved profiles</span></div><p>Finding an aircraft does not mean its controls are ready. Catalogue status is shown below.</p><div className="setup-location-grid"><InventoryList title="Installed aircraft" items={installed}/><InventoryList title="Saved aircraft profiles" items={saved}/></div></section>
        <section className="setup-section"><div className="setup-section-heading"><h3>Your controls</h3><span className="badge">Read-only</span></div><p>Bound, unbound and conflict totals come from each resolved aircraft catalogue. Unknown totals have not been verified.</p><div className="setup-bindings">{setup.bindings?.length?setup.bindings.map(binding=><article className="setup-binding" key={binding.aircraft}><div className="setup-section-heading"><strong>{binding.label??binding.aircraft}</strong><span className="badge">{binding.status}</span></div>{binding.reason&&<p>{binding.reason}</p>}{binding.status==='Current'&&binding.counts&&<dl><div><dt>Bound</dt><dd>{count(binding.counts.actions_bound)}</dd></div><div><dt>Unbound</dt><dd>{unbound(binding.counts.actions_total,binding.counts.actions_bound)}</dd></div><div><dt>Conflicting combinations</dt><dd>{count(binding.counts.conflicting_combos)}</dd></div></dl>}</article>):<p>No resolved control catalogues are available yet.</p>}</div></section>
        <section className="setup-section"><div className="setup-section-heading"><h3>Devices in saved profiles</h3><span className="badge">{devices.length} found</span></div><p>These names come from saved control files. Connection status is Unknown; a saved profile does not prove a device is plugged in.</p>{devices.length?<div className="setup-devices">{devices.map((device,index)=><DeviceCard device={device} key={typeof device==='string'?`${device}-${index}`:`${device.id??itemName(device)}-${index}`}/>)}</div>:<p>No device profiles were reported.</p>}</section>
      </>}
      <div className="setup-footer"><button onClick={close}>Done</button></div>
    </dialog>
  </>;
}
