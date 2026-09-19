import {memo} from 'react';
import type {CSSProperties,ReactNode} from 'react';
import type {RemoteButton,RemotePanel} from './remote-types';
import type {BezelEdge,BezelLayout} from './cockpit-layout';

export interface ControlProps {enabled:boolean;busy:boolean;onTap:(id:string)=>void;}
export function RemoteControl({button,enabled,busy,onTap,compact=false,number,bezelPosition}:{button:RemoteButton;compact?:boolean;number?:string;bezelPosition?:number}&ControlProps){
  const reason=!button.available?button.reason??'Binding unavailable':!enabled?'Connect controls to use this button':busy?'Another action is running':'';
  return <button className="remote-control" style={bezelPosition===undefined?undefined:{'--bezel-position':`${bezelPosition*100}%`} as CSSProperties} disabled={!enabled||busy||!button.available} title={reason?`${button.name} · ${reason}`:button.name} aria-label={button.name} data-button-number={number} data-action-id={button.id} onClick={()=>onTap(button.id)}><strong>{button.label??button.name}</strong>{!button.available&&!compact&&<small>{reason}</small>}</button>;
}
export interface BezelProps extends ControlProps {panel?:RemotePanel;controlPanelId:string;layout:BezelLayout;label:string;}
// Image updates do not need to render the sixty surrounding control buttons.
const BezelKeys=memo(function BezelKeys({edge,panel,layout,label,enabled,busy,onTap}:BezelProps&{edge:BezelEdge}){
  const positions=layout[edge].map(button=>button.position);
  const pitch=Math.min(1,...positions.slice(1).map((position,index)=>position-positions[index]));
  return <div className={`bezel-${edge}`} style={{'--bezel-pitch':`${pitch*100}%`} as CSSProperties} role="group" aria-label={`${label} ${edge} bezel buttons`}>{layout[edge].map(definition=>{
    const id=definition.actionId;
    const button=panel?.buttons.find(item=>item.id===id)??{id,name:`${label} PB ${definition.label}`,label:definition.label,available:false,reason:'Current control catalogue unavailable'};
    return <RemoteControl key={id} button={button} enabled={enabled} busy={busy} onTap={onTap} number={definition.label} bezelPosition={definition.position} compact/>;
  })}</div>;
});
export default function CockpitBezel({children,...props}:BezelProps&{children:ReactNode}){
  return <div className="cockpit-display-bezel" data-bezel-panel={props.controlPanelId}>
    <BezelKeys {...props} edge="top"/><BezelKeys {...props} edge="left"/>
    <div className="bezel-screen">{children}</div>
    <BezelKeys {...props} edge="right"/><BezelKeys {...props} edge="bottom"/>
  </div>;
}
