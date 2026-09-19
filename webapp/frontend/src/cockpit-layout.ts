import type {DisplayFeedPanelId} from './DisplayFeedSetup';
import type {PanelId} from './workspace';
import {list,number,obj,text} from './utils';

export type BezelEdge='left'|'top'|'right'|'bottom';
export interface BezelButton {actionId:string;label:string;position:number;}
export type BezelLayout=Record<BezelEdge,readonly BezelButton[]>;
export interface CockpitDisplay {
  id:string;label:string;textDisplayId:string;
  workspaceId?:PanelId;controlPanelId?:string;nativePanelId?:DisplayFeedPanelId;bezel?:BezelLayout;
}

// Installed FA-18C MPD_PB_defs.lua: visual order, not numeric catalogue order.
// Both MDI and AMPCD share these coordinates. MENU (18) is bottom centre.
export const HORNET_BEZEL:Record<BezelEdge,readonly number[]>={left:[5,4,3,2,1],top:[6,7,8,9,10],right:[11,12,13,14,15],bottom:[20,19,18,17,16]};
function hornetBezel(panel:string):BezelLayout{
  // Standard MPD menu anchors calibrated against the native 512px exports.
  // The baked border is part of the image: dividing the 1024-DI clipping
  // rectangle alone puts buttons too far out. AMPCD has a different Y scale.
  // Keep this aircraft/export geometry here, independent of viewport size.
  const horizontal=[97.5,177.25,257,336.75,416.5];
  const vertical=panel==='ampcd'?[107.5,187.875,268.25,348.625,429]:[112.5,190.5,268.5,346.5,424.5];
  const edge=(side:BezelEdge)=>HORNET_BEZEL[side].map((number,index)=>{
    const position=(side==='top'||side==='bottom'?horizontal:vertical)[index]/512;
    return {actionId:`hornet.${panel}.pb.${number}`,label:String(number),position};
  });
  return {left:edge('left'),top:edge('top'),right:edge('right'),bottom:edge('bottom')};
}
const layouts:Readonly<Record<string,readonly CockpitDisplay[]>>={
  'FA-18C_hornet':[
    {id:'left-ddi',label:'Left DDI',textDisplayId:'2',workspaceId:'left-ddi',controlPanelId:'left_mdi',nativePanelId:'left_mdi',bezel:hornetBezel('left_mdi')},
    {id:'ampcd',label:'AMPCD',textDisplayId:'4',workspaceId:'ampcd',controlPanelId:'ampcd',nativePanelId:'ampcd',bezel:hornetBezel('ampcd')},
    {id:'right-ddi',label:'Right DDI',textDisplayId:'3',workspaceId:'right-ddi',controlPanelId:'right_mdi',nativePanelId:'right_mdi',bezel:hornetBezel('right_mdi')},
  ],
};

/** Presentation is aircraft-specific and has no three-display limit.
 * Native capture remains limited to independently registered export regions.
 * Unmapped aircraft expose real exported text without invented bezel geometry.
 */
export function cockpitDisplays(aircraft:string,cockpitValue:unknown):readonly CockpitDisplay[]{
  if(Object.hasOwn(layouts,aircraft))return layouts[aircraft];
  const cockpit=obj(cockpitValue);if(cockpit.aircraft!==aircraft)return [];
  const seen=new Set<string>();
  return list(cockpit.displays).flatMap(display=>{
    const id=text(display.id,'');
    const observed=number(display.age_s)!==undefined||list(display.elements).length>0||list(display.last_observed_elements).length>0;
    if(!id||seen.has(id)||!observed)return [];seen.add(id);
    return [{id:`export-${id}`,label:text(display.label,`Display ${id}`),textDisplayId:id}];
  });
}
