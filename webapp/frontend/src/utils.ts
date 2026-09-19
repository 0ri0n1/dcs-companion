import type { RecordData } from './types';
export const obj = (value: unknown): RecordData => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordData : {};
export const list = (value: unknown): RecordData[] => Array.isArray(value) ? value.map(obj) : [];
export const number = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
export const text = (value: unknown, fallback = 'Unknown'): string => typeof value === 'string' && value.length ? value : typeof value === 'number' && Number.isFinite(value) ? String(value) : fallback;
export const format = (value: unknown, decimals = 0, suffix = '') => number(value) === undefined ? 'Unknown' : `${(value as number).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}${suffix}`;
export const age = (value: unknown) => number(value) === undefined ? 'Unknown' : (value as number) > 3600 ? `${((value as number) / 3600).toFixed(1)} h` : (value as number) > 60 ? `${((value as number) / 60).toFixed(1)} min` : `${(value as number).toFixed(1)} s`;
export const heading = (value: unknown) => number(value) === undefined ? '—' : `${Math.round(value as number).toString().padStart(3, '0')}°`;
export function coords(record: RecordData): [number, number] | undefined {
  const p = obj(record.position ?? record.coordinates ?? record.geo ?? record.position_geo);
  const lat = number(record.latitude ?? record.lat ?? p.latitude ?? p.lat);
  const lon = number(record.longitude ?? record.lon ?? p.longitude ?? p.lon);
  return lat !== undefined && lon !== undefined && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? [lat, lon] : undefined;
}
export const recordId = (record: RecordData) => text(record.id ?? record.identifier ?? record.name, '');
export const recordName = (record: RecordData) => text(record.name ?? record.title ?? record.identifier ?? record.id);
export function bindingText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(bindingText).filter(v => v !== 'Unbound').join(' / ') || 'Unbound';
  const v = obj(value);
  if (typeof v.combo === 'string') return v.combo;
  if (v.key) return [...(Array.isArray(v.reformers) ? v.reformers : []), v.key].join(' + ');
  return 'Unbound';
}
export const radians = (deg: number) => deg * Math.PI / 180;
export function navigation(from: [number, number], to: [number, number], speed?: number) {
  const a = radians(from[0]), b = radians(to[0]), dlat = b-a, dlon = radians(to[1]-from[1]);
  const h = Math.sin(dlat/2)**2 + Math.cos(a)*Math.cos(b)*Math.sin(dlon/2)**2;
  const distance = 3440.065 * 2*Math.atan2(Math.sqrt(h), Math.sqrt(Math.max(0, 1-h)));
  const bearing = (Math.atan2(Math.sin(dlon)*Math.cos(b), Math.cos(a)*Math.sin(b)-Math.sin(a)*Math.cos(b)*Math.cos(dlon))*180/Math.PI+360)%360;
  return { distance_nm: distance, bearing_true_deg: bearing, eta_minutes: speed !== undefined && speed > 1 ? distance/speed*60 : undefined };
}
export function uuid() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6]&15)|64; b[8] = (b[8]&63)|128;
  const s = Array.from(b, n => n.toString(16).padStart(2,'0')).join('');
  return `${s.slice(0,8)}-${s.slice(8,12)}-${s.slice(12,16)}-${s.slice(16,20)}-${s.slice(20)}`;
}
