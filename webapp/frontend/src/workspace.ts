import { useEffect, useState } from 'react';
export const PANEL_IDS=['left-ddi','right-ddi','ufc','ampcd','navigation','workflows'] as const;
export type PanelId=typeof PANEL_IDS[number];
export type PanelSize='compact'|'regular'|'large';
export interface Workspace {version:1;style:'cockpit'|'touch';panels:Array<{id:PanelId;visible:boolean;size:PanelSize;style:'inherit'|'cockpit'|'touch'}>;favourites:string[];}
export const panelNames:Record<PanelId,string>={'left-ddi':'Left DDI','right-ddi':'Right DDI',ufc:'UFC',ampcd:'AMPCD',navigation:'Navigation',workflows:'Workflows'};
export function presetWorkspace(style:'cockpit'|'touch'='touch'):Workspace{return {version:1,style,panels:PANEL_IDS.map(id=>({id,visible:true,size:'regular',style:'inherit'})),favourites:[]};}
const keys=(value:Record<string,unknown>,allowed:string[])=>Object.keys(value).every(key=>allowed.includes(key));
/** Layout files contain presentation preferences and action IDs only, never executable definitions. */
export function validateWorkspace(value:unknown):Workspace|undefined{
  if(!value||typeof value!=='object'||Array.isArray(value))return;
  const raw=value as Record<string,unknown>;
  if(!keys(raw,['version','style','panels','favourites'])||raw.version!==1||!['cockpit','touch'].includes(String(raw.style))||!Array.isArray(raw.panels)||raw.panels.length!==PANEL_IDS.length||!Array.isArray(raw.favourites)||raw.favourites.length>100)return;
  const ids=new Set<string>(),panels:Workspace['panels']=[];
  for(const item of raw.panels){if(!item||typeof item!=='object'||Array.isArray(item)||!keys(item,['id','visible','size','style'])||!PANEL_IDS.includes(item.id)||ids.has(item.id)||typeof item.visible!=='boolean'||!['compact','regular','large'].includes(item.size))return;if(item.style!==undefined&&!['inherit','cockpit','touch'].includes(item.style))return;ids.add(item.id);panels.push({id:item.id,visible:item.visible,size:item.size,style:item.style??'inherit'});}
  if(raw.favourites.some(id=>typeof id!=='string'||!/^[A-Za-z0-9_.:{} ()+%-]{1,240}$/.test(id)))return;
  return {version:1,style:raw.style as Workspace['style'],panels,favourites:[...new Set(raw.favourites as string[])]};
}
export function workspaceKey(aircraft:string,device:string){return `dcs-copilot-workspace-v1:${encodeURIComponent(aircraft.slice(0,80))}:${device}`;}
export function useDeviceLayout(){const bucket=()=>innerWidth<700||(innerHeight<500&&innerWidth<1000)?'phone':innerWidth<1300?'tablet':'desktop';const [device,setDevice]=useState(bucket);useEffect(()=>{const update=()=>setDevice(bucket());addEventListener('resize',update);return()=>removeEventListener('resize',update);},[]);return device;}
export function useWorkspace(aircraft:string,device:string){
  const key=workspaceKey(aircraft,device),read=()=>{try{return validateWorkspace(JSON.parse(localStorage.getItem(key)??'null'))??presetWorkspace();}catch{return presetWorkspace();}};
  const [stored,setStored]=useState<{key:string;value:Workspace}>(()=>({key,value:read()}));
  const value=stored.key===key?stored.value:read();
  const setValue=(update:Workspace|((previous:Workspace)=>Workspace))=>{const next=typeof update==='function'?update(value):update;const validated=validateWorkspace(next);if(!validated)return;setStored({key,value:validated});try{localStorage.setItem(key,JSON.stringify(validated));}catch{/* Optional preferences. */}};
  return [value,setValue] as const;
}
export function downloadLayout(workspace:Workspace){const url=URL.createObjectURL(new Blob([JSON.stringify(workspace,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='dcs-companion-layout.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
