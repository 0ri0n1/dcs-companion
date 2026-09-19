import { useEffect, useState } from 'react';
import { isThemeChoice } from './themes';
import type { TextSize, ThemeChoice } from './themes';

export const VIEWS = ['Navigation Scope', 'Controls', 'Guide', 'Sensors', 'Cockpit', 'Flight data', 'Data Health', 'Remote'] as const;
export const DISPLAY_STORAGE_KEY = 'dcs-copilot-display-v1';
export interface DisplayPreferences {
  view: typeof VIEWS[number];
  brightness: number;
  orientation: 'north' | 'track';
  range: number;
  rings: boolean;
  airfields: boolean;
  navaids: boolean;
  route: boolean;
  mission: boolean;
  enemies: boolean;
  friendlies: boolean;
  theme: ThemeChoice;
  textSize: TextSize;
}
const defaults: DisplayPreferences = {view:'Navigation Scope',brightness:100,orientation:'north',range:80,rings:true,airfields:true,navaids:true,route:true,mission:true,enemies:true,friendlies:true,theme:'auto',textSize:'comfortable'};

/** This explicit allowlist is the complete browser persistence boundary. */
export function readDisplayPreferences(): DisplayPreferences {
  const prefs = {...defaults};
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(DISPLAY_STORAGE_KEY) ?? '{}');
    const saved = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string,unknown> : {};
    if (VIEWS.includes(saved.view as DisplayPreferences['view'])) prefs.view = saved.view as DisplayPreferences['view'];
    if (isThemeChoice(saved.theme)) prefs.theme = saved.theme;
    if (saved.textSize === 'comfortable' || saved.textSize === 'large') prefs.textSize = saved.textSize;
    const brightness = saved.brightness ?? Number(localStorage.getItem('scope-brightness'));
    if (typeof brightness === 'number' && Number.isFinite(brightness) && brightness >= 35 && brightness <= 100) prefs.brightness = brightness;
    if (saved.orientation === 'north' || saved.orientation === 'track') prefs.orientation = saved.orientation;
    if (typeof saved.range === 'number' && [10,20,40,80,160,320].includes(saved.range)) prefs.range = saved.range;
    for (const key of ['rings','airfields','navaids','route','mission','enemies','friendlies'] as const) if (typeof saved[key] === 'boolean') prefs[key] = saved[key];
  } catch { /* Corrupt or unavailable storage is an ordinary default-display case. */ }
  return prefs;
}

export function useDisplayPreference<K extends keyof DisplayPreferences>(key: K) {
  const [value,setValue] = useState<DisplayPreferences[K]>(() => readDisplayPreferences()[key]);
  useEffect(() => {
    try { localStorage.setItem(DISPLAY_STORAGE_KEY,JSON.stringify({...readDisplayPreferences(),[key]:value})); }
    catch { /* The dashboard also works when browser persistence is disabled. */ }
  },[key,value]);
  return [value,setValue] as const;
}
