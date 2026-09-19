export const PALETTES = [
  {id:'hornet',name:'Hornet',description:'Clear green · F/A-18C',colors:['#08130f','#effff0','#b0ff9c']},
  {id:'raptor',name:'Raptor',description:'Ice blue · F-22 & F-23',colors:['#101820','#f0f7ff','#a9dfff']},
  {id:'eagle',name:'Eagle',description:'Warm amber · F-15',colors:['#171613','#fff7e8','#ffd28c']},
  {id:'lightning',name:'Lightning',description:'Cool violet · F-35',colors:['#14141e','#f6f3ff','#d6c3ff']},
  {id:'daylight',name:'Daylight',description:'Light surfaces · dark ink',colors:['#edf2f8','#17273e','#154da9']},
  {id:'contrast',name:'High contrast',description:'Bright text · deep black',colors:['#000000','#ffffff','#85f5ff']},
  {id:'classic',name:'Classic',description:'Original cockpit palette',colors:['#0c1215','#f0f4ed','#c5ed9e']},
] as const;

export type Palette = typeof PALETTES[number]['id'];
export type ThemeChoice = 'auto' | Palette;
export type TextSize = 'comfortable' | 'large';
export function isThemeChoice(value:unknown):value is ThemeChoice {
  return value==='auto'||PALETTES.some(palette=>palette.id===value);
}

/** Presentation only: these names never confer aircraft/control support. */
export function aircraftPalette(aircraft:string):Palette {
  const id=aircraft.toUpperCase().replace(/[^A-Z0-9]/g,'');
  if(id.startsWith('FA18')||id.startsWith('F18'))return 'hornet';
  if(id.startsWith('F22')||id.startsWith('F23'))return 'raptor';
  if(id.startsWith('F15'))return 'eagle';
  if(id.startsWith('F35')||id.startsWith('VSNF35'))return 'lightning';
  return 'contrast';
}

export function paletteName(palette:Palette) {
  return PALETTES.find(item=>item.id===palette)!.name;
}
