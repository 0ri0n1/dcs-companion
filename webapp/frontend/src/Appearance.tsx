import { useRef } from 'react';
import { PALETTES, paletteName } from './themes';
import type { Palette, TextSize, ThemeChoice } from './themes';

interface Props {
  theme:ThemeChoice; setTheme:(theme:ThemeChoice)=>void;
  textSize:TextSize; setTextSize:(size:TextSize)=>void;
  brightness:number; setBrightness:(brightness:number)=>void;
  resolved:Palette;
}

export default function Appearance({theme,setTheme,textSize,setTextSize,brightness,setBrightness,resolved}:Props) {
  const dialog=useRef<HTMLDialogElement>(null),trigger=useRef<HTMLButtonElement>(null);
  function close(){dialog.current?.close();trigger.current?.focus();}
  return <>
    <button className="appearance-button" ref={trigger} aria-haspopup="dialog" onClick={()=>dialog.current?.showModal()}><span aria-hidden="true">◐</span> Appearance</button>
    <dialog className="appearance-dialog" ref={dialog} aria-labelledby="appearance-title" onCancel={event=>{event.preventDefault();close();}}>
      <div className="appearance-heading"><div><span className="eyebrow">MAKE IT YOUR COCKPIT</span><h2 id="appearance-title">Appearance</h2></div><button aria-label="Close appearance" onClick={close}>×</button></div>
      <p className="appearance-intro">Choose a look that is easy to read on your screen. Changes apply immediately and stay on this device.</p>
      <div className="appearance-controls"><label>Color theme<select value={theme} onChange={event=>setTheme(event.target.value as ThemeChoice)}><option value="auto">Automatic · follow aircraft</option>{PALETTES.map(palette=><option key={palette.id} value={palette.id}>{palette.name}</option>)}</select></label><label>Text size<select value={textSize} onChange={event=>setTextSize(event.target.value as TextSize)}><option value="comfortable">Comfortable</option><option value="large">Large</option></select></label></div>
      <p className="appearance-current" role="status">{theme==='auto'?`Automatic · ${paletteName(resolved)}. Follows your aircraft; keeps its look while disconnected.`:`${paletteName(resolved)} stays selected when you change aircraft.`}</p>
      <div className="theme-gallery" aria-label="Theme palettes">{PALETTES.map(palette=><button key={palette.id} className="theme-choice" aria-pressed={theme===palette.id} onClick={()=>setTheme(palette.id)}><span className="theme-swatches" aria-hidden="true">{palette.colors.map(color=><i key={color} style={{background:color}}/>)}</span><strong>{palette.name}</strong><small>{palette.description}</small></button>)}</div>
      <div className="appearance-preview"><strong>Clear readings at a glance</strong><small>Secondary details stay readable in every theme.</small><div><span className="badge green">Ready</span><span className="badge amber">Attention</span><span className="badge red">Unavailable</span></div></div>
      <label className="appearance-brightness">Brightness <output>{brightness}%</output><input aria-label="Appearance brightness" type="range" min="35" max="100" value={brightness} onChange={event=>setBrightness(Number(event.target.value))}/></label>
      <p className="appearance-tip">Use 100% brightness for the clearest text. Daylight suits a bright room; High contrast keeps the darkest background.</p>
      <div className="appearance-footer"><button onClick={()=>{setTheme('auto');setTextSize('comfortable');setBrightness(100);}}>Reset appearance</button><button className="primary-button" onClick={close}>Done</button></div>
    </dialog>
  </>;
}
