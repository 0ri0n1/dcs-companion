import { useEffect, useRef, useState } from 'react';
import { isLoopbackHost, request } from './api';
export default function PairDevice({connected,lanEnabled}:{connected:boolean;lanEnabled:boolean}){
  const dialog=useRef<HTMLDialogElement>(null),trigger=useRef<HTMLButtonElement>(null),generation=useRef(0);
  const [qr,setQr]=useState(''),[expires,setExpires]=useState(0),[now,setNow]=useState(Date.now()),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const local=isLoopbackHost(window.location.hostname),remaining=Math.max(0,Math.ceil((expires-now)/1000));
  useEffect(()=>{if(!expires)return;const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[expires]);
  async function create(){
    if(!local||!lanEnabled||!connected||busy)return;const ticket=++generation.current;setBusy(true);setError('');setQr('');setExpires(0);
    try{const result=await request('/api/pairing-ticket',{}, {timeoutMs:5000});if(ticket!==generation.current)return;if(typeof result.qr_svg!=='string'||!result.qr_svg.includes('<svg')||result.qr_svg.length>1000000||typeof result.expires_at!=='number')throw new Error('Invalid QR response');setQr(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(result.qr_svg)}`);setExpires(result.expires_at*1000);setNow(Date.now());}
    catch{if(ticket===generation.current)setError('A pairing code could not be created. Check tablet access and try again.');}
    finally{if(ticket===generation.current)setBusy(false);}
  }
  function close(){generation.current++;dialog.current?.close();setQr('');setExpires(0);setBusy(false);trigger.current?.focus();}
  function open(){dialog.current?.showModal();if(local&&lanEnabled)void create();}
  return <><button className="pair-device-button" ref={trigger} disabled={!connected} aria-haspopup="dialog" onClick={open}>Pair phone</button><dialog ref={dialog} className="pair-device-dialog" aria-labelledby="pair-device-title" onCancel={event=>{event.preventDefault();close();}}><div className="pair-device-heading"><div><span className="eyebrow">YOUR SECOND SCREEN</span><h2 id="pair-device-title">Pair your phone</h2></div><button aria-label="Close phone pairing" onClick={close}>×</button></div>
    {!local?<p>Create a phone pairing code on your DCS computer.</p>:!lanEnabled?<p>Tablet access is off. Start the companion with tablet access on your DCS computer, then create a pairing code here.</p>:<><p>Keep your phone on the same Wi-Fi as this computer. Scan the code with its camera to open and pair the companion.</p>{busy&&<p role="status">Creating your pairing code…</p>}{error&&<p role="alert" className="pair-device-error">{error}</p>}{qr&&remaining>0&&<><img className="pair-device-qr" src={qr} alt="Scan this QR code to pair your phone"/><p className="pair-device-expiry" role="status">Expires in {Math.floor(remaining/60)}:{String(remaining%60).padStart(2,'0')} · one use</p></>}{qr&&remaining===0&&<p role="status">This code has expired. Create a new code to pair your phone.</p>}<p className="pair-device-note">The code works once and expires after two minutes. Your normal pairing token stays private.</p><button disabled={busy} onClick={()=>void create()}>Create new QR code</button></>}
    <div className="pair-device-footer"><button onClick={close}>Done</button></div></dialog></>;
}
