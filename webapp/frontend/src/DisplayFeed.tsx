import {useEffect,useRef,useState} from 'react';
import type {ReactNode} from 'react';
import type {DisplayFeedPanelId,DisplayFeedState} from './DisplayFeedSetup';
import {FRAME_MAX_AGE,useDisplayStreamView} from './display-stream';

interface Props {panelId:DisplayFeedPanelId;label:string;feed?:DisplayFeedState;aircraft?:string;session?:string;usable:boolean;textReadout:ReactNode;surround?:(screen:ReactNode)=>ReactNode;}
export default function DisplayFeed({panelId,label,feed,usable,textReadout,surround}:Props){
  const stream=useDisplayStreamView(),mode=stream.sources[panelId]??(feed?.enabled?'image':'text'),frame=stream.frames[panelId];
  const root=useRef<HTMLDivElement>(null),[fullscreenError,setFullscreenError]=useState(''),[fullscreenActive,setFullscreenActive]=useState(false),[failedUrl,setFailedUrl]=useState('');
  useEffect(()=>{const changed=()=>setFullscreenActive(document.fullscreenElement===root.current);document.addEventListener('fullscreenchange',changed);return()=>document.removeEventListener('fullscreenchange',changed);},[]);
  const shown=mode==='image'&&usable&&feed?.enabled&&frame&&frame.url!==failedUrl&&Date.now()-frame.capturedAt<FRAME_MAX_AGE?frame:undefined;
  const unavailable=!usable?'Companion disconnected.':!feed?'Display image service unavailable. Open display setup on your DCS computer.':!feed.enabled?'Capture is off. Enable it in Live display images on your DCS computer.':stream.detail||'Waiting for fresh display frames.';
  async function fullscreen(){try{setFullscreenError('');if(document.fullscreenElement===root.current)await document.exitFullscreen();else await root.current?.requestFullscreen();}catch{setFullscreenError('Fullscreen is unavailable in this browser.');}}
  const screen=mode==='text'?textReadout:<div className="display-feed-image">{shown?<img src={shown.url} alt={`${label} live captured display`} data-sequence={shown.sequence} data-captured-at={shown.capturedAt} onError={()=>setFailedUrl(shown.url)}/>:<p role="status">{unavailable}</p>}</div>;
  return <div className="display-feed" role="region" ref={root} aria-label={`${label} display view`}><div className="display-feed-toolbar"><div role="group" aria-label={`${label} display source`}><button aria-pressed={mode==='image'} onClick={()=>stream.setSource(panelId,'image')}>Live stream</button><button aria-pressed={mode==='text'} onClick={()=>stream.setSource(panelId,'text')}>Text</button></div>{mode==='image'&&document.fullscreenEnabled&&<button aria-label={`${fullscreenActive?'Exit fullscreen':'Fullscreen'} ${label}`} onClick={()=>void fullscreen()}>⛶</button>}</div>{surround?surround(screen):screen}{mode==='image'&&<><div className="display-feed-caption"><strong>{shown?'Live stream':stream.status==='Paused'?'Paused':'Stream unavailable'}</strong><span>{shown?new Date(shown.capturedAt).toLocaleTimeString():'No stale frame is retained.'}</span></div>{!surround&&<small>Continuous native display stream. Text remains available independently.</small>}</>}{fullscreenError&&<p role="status">{fullscreenError}</p>}</div>;
}
