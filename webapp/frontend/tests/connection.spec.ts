import { test, expect, type Page } from '@playwright/test';
import fs from 'node:fs/promises';
import { CONNECTION_HISTORY_KEY, sanitizeConnectionHistory } from '../src/connection-history';

const ticketA='A'.repeat(43),ticketB='B'.repeat(43);
const fixture={server:{dry_run:true,lan_enabled:true},health:{status:'Live',aircraft:'F-22A',session:'test-session',terrain:'Caucasus',telemetry_age_s:.1,display_age_s:.1,telemetry_fresh:true,displays_fresh:true,model_advancing:true,dcs_running:true},aircraft:{unit_name:'test-player'},navigation:{terrain:'Caucasus',ownship:{lat:42,lon:42,track_true_deg:40,heading_true_deg:40},airfields:[],navaids:[],route:[]},awareness:{status:'Available',mode:'mission-awareness',aircraft:'F-22A',session:'test-session',ownship_unit:'test-player',single_player:true,model_advancing:true,age_s:.1,contacts:[{id:'test-enemy',name:'SENSITIVE FLIGHT NAME',lat:42.1,lon:42,age_s:.1}]}};
async function mock(page:Page){
  const seen={reads:0,pairs:[] as any[],qr:0,commands:0};
  await page.addInitScript(snapshot=>{(window as any).__connectionFixture=snapshot;(window as any).__streams=[];(window as any).EventSource=class{onmessage:any;onerror:any;timer:any;closed=false;constructor(){(window as any).__streams.push(this);this.timer=setInterval(()=>{if(!(window as any).__silent)this.onmessage?.({data:JSON.stringify((window as any).__connectionFixture)});},300);}addEventListener(){}close(){this.closed=true;clearInterval(this.timer);}};},fixture);
  await page.route('**/api/state',route=>{seen.reads++;return route.fulfill({json:fixture});});
  await page.route('**/api/session',route=>{seen.pairs.push(route.request().postDataJSON());return route.fulfill({json:{ok:true}});});
  await page.route('**/api/pairing-ticket',route=>{seen.qr++;return route.fulfill({json:{url:`http://192.0.2.24:18787/#pair=${ticketA}`,expires_at:Math.floor(Date.now()/1000)+120,expires_in:120,qr_svg:'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="white"/><path d="M20 20h60v60H20z" fill="black"/></svg>'}});});
  page.on('request',request=>{if(request.method()==='POST'&&new URL(request.url()).pathname.startsWith('/api/commands'))seen.commands++;});
  return seen;
}
async function health(page:Page){await page.getByRole('button',{name:'Data Health',exact:true}).click();await expect(page.getByRole('region',{name:'Companion connection history'})).toBeVisible();}

test('healthy live stream avoids redundant state polls and false offline transitions',async({page})=>{
  const seen=await mock(page);await page.goto('/');await expect(page.locator('.ownship')).toHaveCount(1);await page.waitForTimeout(3200);
  expect(seen.reads).toBe(1);await expect(page.locator('.header-status')).toContainText('Live');expect(seen.commands).toBe(0);
});

test('failed recovery does not replay commands and late callbacks from an old stream cannot replace newer state',async({page})=>{
  const seen=await mock(page);await page.goto('/');await expect(page.locator('.ownship')).toHaveCount(1);
  await page.route('**/api/state',route=>route.abort());await page.evaluate(()=>{(window as any).__silent=true;(window as any).__streams[0].onerror();});
  await expect(page.locator('.header-status')).toContainText('Offline');
  const newer={...fixture,health:{...fixture.health,aircraft:'FA-18C_hornet'}};
  await page.route('**/api/state',route=>route.fulfill({json:newer}));await page.evaluate(snapshot=>{(window as any).__connectionFixture=snapshot;(window as any).__silent=false;},newer);
  await expect(page.locator('.session-identity')).toContainText('FA-18C_hornet',{timeout:7000});
  await page.evaluate(old=>(window as any).__streams[0].onmessage({data:JSON.stringify(old)}),fixture);
  await expect(page.locator('.session-identity')).toContainText('FA-18C_hornet');await health(page);await expect(page.locator('.connection-event-list')).toContainText('Dashboard updates resumed.');expect(seen.commands).toBe(0);
});

test('a silent stream expires telemetry, records the overdue update and recovers through the watchdog',async({page})=>{
  const seen=await mock(page);await page.goto('/');await expect(page.locator('.ownship')).toHaveCount(1);await page.evaluate(()=>(window as any).__silent=true);
  await expect(page.locator('.ownship')).toHaveCount(0,{timeout:5000});await health(page);
  await expect(page.locator('.connection-event-list')).toContainText('No dashboard update arrived for six seconds',{timeout:9000});
  await expect.poll(()=>seen.reads,{timeout:9000}).toBe(2);expect(await page.evaluate(()=>(window as any).__streams[0].closed)).toBe(true);expect(seen.commands).toBe(0);
});

test('a hung state request times out and permits the next recovery attempt',async({page})=>{
  const seen=await mock(page);let reads=0;await page.route('**/api/state',route=>{reads++;if(reads===1)return;return route.fulfill({json:fixture});});
  await page.goto('/');await health(page);await expect(page.locator('.connection-event-list')).toContainText('request deadline',{timeout:6000});
  await expect(page.locator('.header-status')).toContainText('Live',{timeout:9000});expect(reads).toBe(2);expect(seen.commands).toBe(0);
});

test('telemetry and mission export availability have distinct deduplicated history while the browser stays connected',async({page})=>{
  const seen=await mock(page);await page.goto('/');await expect(page.locator('.enemy-symbol')).toHaveCount(1);await health(page);
  await page.evaluate(()=>{(window as any).__connectionFixture.health.telemetry_fresh=false;(window as any).__connectionFixture.awareness.status='Stale';});
  await expect(page.locator('.connection-event-list')).toContainText('Ownship telemetry became stale');await expect(page.locator('.connection-health-heading .badge')).toHaveText('connected');
  await page.waitForTimeout(1100);let events=await page.evaluate(key=>JSON.parse(sessionStorage.getItem(key)!),CONNECTION_HISTORY_KEY);expect(events.filter((item:any)=>item.code==='telemetry_stale')).toHaveLength(1);expect(events.filter((item:any)=>item.code==='awareness_unavailable')).toHaveLength(1);
  await page.evaluate(()=>{(window as any).__connectionFixture.health.telemetry_fresh=true;(window as any).__connectionFixture.awareness.status='Available';});
  await expect(page.locator('.connection-event-list')).toContainText('Current mission positions resumed.');expect(seen.commands).toBe(0);
});

test('history and downloaded report keep bounded controlled reasons instead of payloads or credentials',async({page})=>{
  const injected=Array.from({length:250},(_,index)=>({timestamp:new Date(1700000000000+index).toISOString(),code:'stream_error',reason:'TOKEN_SECRET C:\\Private\\flight.json',token:'TOKEN_SECRET',position:{lat:42},duration_ms:10}));
  const sanitized=sanitizeConnectionHistory(injected);expect(sanitized).toHaveLength(200);expect(JSON.stringify(sanitized)).not.toMatch(/TOKEN_SECRET|Private|position/);
  await mock(page);await page.addInitScript(({key,events})=>sessionStorage.setItem(key,JSON.stringify(events)),{key:CONNECTION_HISTORY_KEY,events:injected});
  await page.route('**/api/diagnostics',route=>route.fulfill({json:{server_id:'test-server',started_at:'2026-09-18T00:00:00Z',events:[],log_file:'connection-events.jsonl'}}));
  await page.goto('/');await health(page);const downloaded=page.waitForEvent('download');await page.getByRole('button',{name:'Download connection report',exact:true}).click();const file=await downloaded;const content=await fs.readFile((await file.path())!,'utf8');await file.delete();
  const report=JSON.parse(content);expect(report.browser.history).toHaveLength(200);expect(report.server.server_id).toBe('test-server');expect(content).not.toMatch(/TOKEN_SECRET|Private|SENSITIVE FLIGHT NAME|test-enemy/);
  expect(await page.evaluate(()=>Object.keys(sessionStorage))).toEqual([CONNECTION_HISTORY_KEY]);
});

test('QR code is issued only on an explicit dialog request and rendered as an image',async({page})=>{
  const seen=await mock(page);await page.goto('/');await expect(page.getByRole('button',{name:'Pair phone',exact:true})).toBeEnabled();expect(seen.qr).toBe(0);
  await page.getByRole('button',{name:'Pair phone',exact:true}).click();await expect(page.getByRole('img',{name:'Scan this QR code to pair your phone'})).toHaveAttribute('src',/^data:image\/svg\+xml/);expect(seen.qr).toBe(1);
  await expect(page.getByText('one use',{exact:false})).toBeVisible();await page.getByRole('button',{name:'Create new QR code',exact:true}).click();await expect.poll(()=>seen.qr).toBe(2);await page.getByRole('button',{name:'Close phone pairing',exact:true}).click();
  const storage=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));expect(storage).not.toContain(ticketA);expect(seen.commands).toBe(0);
});

test('QR pairing strips the fragment before the one-use exchange and sends no ordinary token',async({page})=>{
  const seen=await mock(page);let hashAtExchange='unobserved';await page.route('**/api/session',async route=>{hashAtExchange=await page.evaluate(()=>location.hash);seen.pairs.push(route.request().postDataJSON());return route.fulfill({json:{ok:true}});});
  await page.goto(`/#pair=${ticketA}`);await expect(page.locator('.header-status')).toContainText('Live');expect(hashAtExchange).toBe('');expect(seen.pairs).toEqual([{ticket:ticketA}]);expect(page.url()).not.toContain(ticketA);
  expect(await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}))).not.toContain(ticketA);expect(seen.commands).toBe(0);
});

test('expired QR has an explicit pairing error and a new scan in the same tab recovers without empty-token fallback',async({page})=>{
  const seen=await mock(page);await page.route('**/api/session',route=>{const payload=route.request().postDataJSON();seen.pairs.push(payload);return payload.ticket===ticketB?route.fulfill({json:{ok:true}}):route.fulfill({status:401,json:{detail:'Expired pairing ticket'}});});
  await page.goto(`/#pair=${ticketA}`);await expect(page.getByRole('alert')).toContainText('invalid, expired or already used');expect(seen.reads).toBe(0);expect(seen.pairs).toEqual([{ticket:ticketA}]);expect(page.url()).not.toContain(ticketA);
  await page.evaluate(ticket=>location.hash=`pair=${ticket}`,ticketB);await expect(page.locator('.header-status')).toContainText('Live');expect(seen.pairs).toEqual([{ticket:ticketA},{ticket:ticketB}]);expect(page.url()).not.toContain(ticketB);expect(seen.commands).toBe(0);
});

test('disabled tablet access explains the requirement without creating a pairing ticket',async({page})=>{
  const seen=await mock(page);await page.route('**/api/state',route=>route.fulfill({json:{...fixture,server:{...fixture.server,lan_enabled:false}}}));await page.goto('/');await page.getByRole('button',{name:'Pair phone',exact:true}).click();await expect(page.getByText('Tablet access is off.',{exact:false})).toBeVisible();expect(seen.qr).toBe(0);
});
