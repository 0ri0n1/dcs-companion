import { test, expect, Page } from '@playwright/test';
import type { Snapshot } from '../src/types';
import { coords, navigation } from '../src/utils';
import { isLoopbackHost } from '../src/api';
import { awarenessView } from '../src/MissionAwareness';

// Deliberately synthetic test fixtures. Production has no demonstration state or fallback tracks.
const base: Snapshot={server:{dry_run:true,lan_enabled:false,connected_clients:1,version:'test'},health:{status:'Live',aircraft:'F-22A',terrain:'Caucasus',mission:'test-mission',session:'test-session',telemetry_age_s:.2,display_age_s:90,telemetry_fresh:true,displays_fresh:false,dcs_running:true,dcs_focused:true,sensor_status:'Unverified'},aircraft:{ground_speed_kt:300},navigation:{terrain:'Caucasus',status:'Available',available_terrains:['Caucasus','PersianGulf','MarianaIslands','MarianaIslandsWWII'],ownship:{lat:42,lon:42,heading_true_deg:40,track_true_deg:43,ground_speed_kt:300,altitude_ft:5000},airfields:[{id:'field1',name:'Fixture Airfield',dcs_id:1,lat:42.1,lon:42.1,runway_geometry_status:'Unknown',runways:[],frequencies:[{value:131,unit:'MHz',modulation:'AM',purpose:'Tower',band:'VHF'}],navaids:[]}],navaids:[],route:[],provenance:{dcs_version:'test',schema_version:1,sources:[]}},bindings:{aircraft:'F-22A',status:'Current',source_fingerprint:'test-fingerprint',devices:[{id:'keyboard',name:'Keyboard',type:'keyboard'},{id:'tartarus',name:'Razer Tartarus Pro',type:'tartarus'},{id:'hotas',name:'HOTAS',type:'hotas'}],actions:[{id:'test-next',name:'Next waypoint',device_id:'keyboard',combos:['1'],category:['Navigation'],conflicts:[]},{id:'test-unbound',name:'Unbound fixture action',device_id:'keyboard',combos:[],category:['Navigation'],conflicts:[]}],physical_input:{status:'Unavailable'}},guide:{actions:[{id:'f22.nav.next',title:'Next waypoint',purpose:'Select the next navigation point',binding:'1',location:'Keyboard',expected_result:'Navigation selection advances',observable:false,risk:'benign',enabled:true}]},queue:{state:'Idle',history:[]},adapters:[]};

async function mock(page:Page,state:Snapshot=structuredClone(base)) {
  await page.addInitScript((fixture)=>{
    (window as any).__dashboardState=fixture;
    (window as any).__streams=[];
    (window as any).EventSource=class{onmessage:any;onerror:any;timer:any;constructor(){(window as any).__streams.push(this);this.timer=setInterval(()=>{if(!(window as any).__silenceStream)this.onmessage?.({data:JSON.stringify((window as any).__dashboardState)});},500);}addEventListener(){}close(){clearInterval(this.timer);}};
  },state);
  await page.route('**/api/session',route=>route.fulfill({json:{ok:true}}));
  await page.route('**/api/state',async route=>route.fulfill({json:await page.evaluate(()=>(window as any).__dashboardState)}));
  await page.route('**/api/navigation/*',route=>route.fulfill({json:{...base.navigation,terrain:decodeURIComponent(route.request().url().split('/').at(-1)!),ownship:null,status:'Offline library — selected terrain'}}));
}
async function overflow(page:Page) {expect(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1)).toBe(false);}

for(const viewport of [{width:2560,height:1440},{width:1180,height:820},{width:820,height:1180}]) {
  test(`seven views fit ${viewport.width}x${viewport.height}`,async({page})=>{
    await page.setViewportSize(viewport);await mock(page);await page.goto('/');
    await expect(page.getByRole('heading',{name:'Navigation Scope.'})).toBeVisible();await overflow(page);
    await page.getByRole('button',{name:'Controls',exact:true}).click();await expect(page.getByRole('heading',{name:'Controls.'})).toBeVisible();await overflow(page);
    await page.getByRole('button',{name:'Guide',exact:true}).click();await expect(page.getByRole('heading',{name:'Aircraft guide.'})).toBeVisible();await overflow(page);
    await page.getByRole('button',{name:'Sensors',exact:true}).click();await expect(page.getByRole('heading',{name:'Sensors.'})).toBeVisible();await overflow(page);
    await page.getByRole('button',{name:'Cockpit',exact:true}).click();await expect(page.getByRole('heading',{name:'Cockpit.'})).toBeVisible();await overflow(page);
    await page.getByRole('button',{name:'Flight data',exact:true}).click();await expect(page.getByRole('heading',{name:'Flight data.'})).toBeVisible();await overflow(page);
    await page.getByRole('button',{name:'Data Health',exact:true}).click();await expect(page.getByRole('heading',{name:'Data health.'})).toBeVisible();await overflow(page);
  });
}
test('navigation search, range, track up and unknown runway remain explicit',async({page})=>{
  await mock(page);await page.goto('/');await page.getByLabel('Search airfields').fill('Fixture');await page.getByRole('button',{name:'⊕ Fixture Airfield 1 ›',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Fixture Airfield'})).toBeVisible();await expect(page.getByText('Pavement geometry is Unknown',{exact:false})).toBeVisible();
  await page.getByRole('button',{name:'Track up',exact:true}).click();await expect(page.getByRole('button',{name:'Track up',exact:true})).toHaveAttribute('aria-pressed','true');
  await page.getByLabel('Scope range').selectOption('160');await expect(page.getByRole('img',{name:'Navigation Scope, 160 nautical mile range, track up'})).toBeVisible();
  await expect(page.getByText('131.000')).toBeVisible();await expect(page.getByText('AM · VHF')).toBeVisible();
});
test('manual terrain library disables ownship and never masquerades as active terrain',async({page})=>{
  await mock(page);await page.goto('/');await page.getByLabel('Terrain source').selectOption('PersianGulf');
  await expect(page.getByText('Manual library view · ownship guidance is disabled.')).toBeVisible();await expect(page.getByText('STATIC CHART · NO OWNSHIP')).toBeVisible();await expect(page.getByRole('button',{name:'Track up',exact:true})).toBeDisabled();
});
test('stale ownship hides its symbol while independent displays remain stale',async({page})=>{
  const state=structuredClone(base);state.health!.telemetry_fresh=false;state.health!.status='Stale telemetry';await mock(page,state);await page.goto('/');
  await expect(page.locator('.ownship')).toHaveCount(0);await expect(page.getByRole('button',{name:'Track up',exact:true})).toBeDisabled();await expect(page.getByText('POSITION UNAVAILABLE',{exact:true})).toBeVisible();
});
test('Observe cannot send; Assist needs review, checkbox, deliberate confirm and sends once',async({page})=>{
  await mock(page);const sent:any[]=[];await page.route('**/api/commands',async route=>{sent.push(route.request().postDataJSON());await route.fulfill({json:{state:'Queued'}});});
  await page.goto('/');await page.getByRole('button',{name:'Guide',exact:true}).click();await expect(page.getByRole('button',{name:'Observe mode · input disabled'})).toBeDisabled();
  await page.getByRole('button',{name:'Assist',exact:true}).click();await expect(page.getByText('DCS is already focused. A confirmed action',{exact:false})).toBeVisible();await page.getByRole('button',{name:'Review action →'}).click();
  await expect(page.getByRole('dialog')).toBeVisible();const confirm=page.getByRole('button',{name:'Confirm and queue once'});await expect(confirm).toBeDisabled();expect(sent).toHaveLength(0);
  await page.getByRole('checkbox').check();await confirm.click();await expect(page.getByRole('dialog')).not.toBeVisible();expect(sent).toHaveLength(1);expect(sent[0].confirmed).toBe(true);expect(sent[0].request_id).toMatch(/^[a-f0-9-]{36}$/);expect(sent[0].action_id).toBe('f22.nav.next');
  await page.reload();await expect(page.getByRole('heading',{name:'Aircraft guide.'})).toBeVisible();await expect(page.getByRole('button',{name:'Observe mode · input disabled'})).toBeDisabled();expect(sent).toHaveLength(1);
});
test('cancel makes only a cancellation request and does not replay on refresh',async({page})=>{
  const state=structuredClone(base);state.queue={state:'Awaiting DCS Focus',request_id:'6ce9f3b6-a142-45c4-9540-ef68a4866c45',action_id:'f22.nav.next'};await mock(page,state);let cancels=0;
  await page.route('**/api/commands/*/cancel',route=>{cancels++;return route.fulfill({json:{state:'Cancelled'}});});await page.goto('/');await page.getByRole('button',{name:'Guide',exact:true}).click();await page.getByRole('button',{name:'Cancel queued action'}).click();expect(cancels).toBe(1);await page.reload();expect(cancels).toBe(1);
});
test('authorization gates live data and token is not persisted',async({page})=>{
  await mock(page);let paired=false,reads=0;await page.route('**/api/session',route=>{const token=route.request().postDataJSON().token;if(token==='test-secret'){paired=true;return route.fulfill({json:{ok:true}});}return route.fulfill({status:401,json:{detail:'Pairing required'}});});await page.route('**/api/state',route=>{reads++;return paired?route.fulfill({json:base}):route.fulfill({status:401,json:{detail:'Pairing required'}});});
  await page.goto('/');await expect(page.getByText('PAIR THIS DEVICE')).toBeVisible();expect(reads).toBe(1);await page.getByLabel('Pairing token').fill('test-secret');await page.getByRole('button',{name:'Pair device',exact:true}).click();await expect(page.getByText('PAIR THIS DEVICE')).not.toBeVisible();expect(paired).toBe(true);expect(await page.evaluate(()=>JSON.stringify({...localStorage}))).not.toContain('test-secret');
});
test('controls show unbound truth without claiming physical input',async({page})=>{
  await mock(page);await page.goto('/');await page.getByRole('button',{name:'Controls',exact:true}).click();await expect(page.getByText('Physical-input highlighting is unavailable.')).toBeVisible();await page.getByRole('button',{name:'Unbound',exact:true}).click();await expect(page.getByText('Unbound fixture action')).toBeVisible();await expect(page.locator('.binding-row').filter({hasText:'Next waypoint'})).toHaveCount(0);
});
test('browser display math uses geographic distance and ground speed; missing coordinates stay unknown',()=>{
  const result=navigation([0,0],[0,1],120);expect(result.bearing_true_deg).toBeCloseTo(90,6);expect(result.distance_nm).toBeCloseTo(60.04,2);expect(result.eta_minutes).toBeCloseTo(30.02,2);
  expect(navigation([0,0],[0,1],0).eta_minutes).toBeUndefined();expect(coords({lat:null,lon:44})).toBeUndefined();expect(coords({lat:91,lon:44})).toBeUndefined();expect(coords({lat:0,lon:0})).toEqual([0,0]);
});
test('session change dismisses pending deliberate confirmation without sending',async({page})=>{
  await mock(page);let sends=0;await page.route('**/api/commands',route=>{sends++;return route.fulfill({json:{state:'Queued'}});});await page.goto('/');await page.getByRole('button',{name:'Guide',exact:true}).click();await page.getByRole('button',{name:'Assist',exact:true}).click();await page.getByRole('button',{name:'Review action →'}).click();await expect(page.getByRole('dialog')).toBeVisible();
  await page.evaluate(()=>{(window as any).__dashboardState.health.session='another-session';});await expect(page.getByRole('dialog')).not.toBeVisible();expect(sends).toBe(0);
});

test('loopback restart repairs an expired session without replaying commands or restoring Assist',async({page})=>{
  await mock(page);let expired=false,pairs=0,sends=0;
  await page.route('**/api/state',route=>expired?route.fulfill({status:401,json:{detail:'Session expired'}}):route.fulfill({json:base}));
  await page.route('**/api/session',route=>{pairs++;expect(route.request().postDataJSON()).toEqual({token:''});expired=false;return route.fulfill({json:{ok:true}});});
  await page.route('**/api/commands',route=>{sends++;return route.fulfill({json:{state:'Queued'}});});
  await page.goto('/');await expect(page.locator('.ownship')).toHaveCount(1);expect(pairs).toBe(0);
  await page.getByRole('button',{name:'Guide',exact:true}).click();await page.getByRole('button',{name:'Assist',exact:true}).click();await page.getByRole('button',{name:'Review action →'}).click();await page.getByRole('checkbox').check();await page.getByRole('button',{name:'Confirm and queue once'}).click();expect(sends).toBe(1);
  expired=true;await page.evaluate(()=>{(window as any).__silenceStream=true;(window as any).__streams.at(-1).onerror();});
  await expect.poll(()=>pairs).toBe(1);await expect(page.getByText('PAIR THIS DEVICE')).not.toBeVisible();await expect(page.getByRole('button',{name:'Observe mode · input disabled'})).toBeDisabled();expect(sends).toBe(1);
});

test('LAN session expiry requires explicit pairing and never submits an empty token',async({page})=>{
  const origin='http://192.0.2.24:4173';
  await page.route(`${origin}/**`,async route=>{const url=new URL(route.request().url());const response=await page.request.get(url.pathname);await route.fulfill({response});});
  await mock(page);let expired=false,pairs=0;
  await page.route('**/api/state',route=>expired?route.fulfill({status:401,json:{detail:'Pairing required'}}):route.fulfill({json:base}));
  await page.route('**/api/session',route=>{pairs++;return route.fulfill({status:401,json:{detail:'Pairing required'}});});
  await page.goto(origin);await expect(page.locator('.ownship')).toHaveCount(1);expect(pairs).toBe(0);
  expired=true;await page.evaluate(()=>{(window as any).__silenceStream=true;(window as any).__streams.at(-1).onerror();});await expect(page.getByText('PAIR THIS DEVICE')).toBeVisible();expect(pairs).toBe(0);await expect(page.locator('.ownship')).toHaveCount(0);
});

test('only display preferences survive reload; Guide starts in Observe and no terrain override is saved',async({page})=>{
  await mock(page);await page.goto('/');await page.getByLabel('Scope range').selectOption('160');await page.getByRole('button',{name:'Track up',exact:true}).click();await page.getByRole('button',{name:'Navaids',exact:true}).click();
  await page.getByLabel('Display brightness',{exact:true}).focus();await page.keyboard.press('Home');await page.keyboard.press('ArrowRight');
  await page.getByLabel('Terrain source').selectOption('PersianGulf');await page.getByRole('button',{name:'Guide',exact:true}).click();await page.getByRole('button',{name:'Assist',exact:true}).click();await page.getByRole('button',{name:'Review action →'}).click();await page.reload();
  await expect(page.getByRole('heading',{name:'Aircraft guide.'})).toBeVisible();await expect(page.getByRole('button',{name:'Observe mode · input disabled'})).toBeDisabled();await expect(page.getByRole('dialog')).not.toBeVisible();await expect(page.getByLabel('Display brightness',{exact:true})).toHaveValue('36');
  await page.getByRole('button',{name:'Navigation Scope',exact:true}).click();await expect(page.getByLabel('Scope range')).toHaveValue('160');await expect(page.getByRole('button',{name:'Track up',exact:true})).toHaveAttribute('aria-pressed','true');await expect(page.getByRole('button',{name:'Navaids',exact:true})).toHaveAttribute('aria-pressed','false');await expect(page.getByLabel('Terrain source')).toHaveValue('');
  const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('dcs-copilot-display-v1')!));expect(Object.keys(saved).sort()).toEqual(['view','brightness','orientation','range','rings','airfields','navaids','route','mission','enemies','friendlies','theme','textSize'].sort());
});

test('new fresh session selects Automatic, while an explicit choice within that session survives view changes',async({page})=>{
  const state=structuredClone(base);state.health!.telemetry_fresh=false;state.health!.session='offline-session';await mock(page,state);await page.goto('/');await page.getByLabel('Terrain source').selectOption('PersianGulf');
  await page.evaluate(()=>{(window as any).__dashboardState.health={...(window as any).__dashboardState.health,telemetry_fresh:true,session:'new-flight',status:'Live'};});await expect(page.getByLabel('Terrain source')).toHaveValue('');await expect(page.locator('.ownship')).toHaveCount(1);
  await page.getByLabel('Terrain source').selectOption('Caucasus');await page.getByRole('button',{name:'Controls',exact:true}).click();await page.getByRole('button',{name:'Navigation Scope',exact:true}).click();await expect(page.getByLabel('Terrain source')).toHaveValue('Caucasus');
  await page.evaluate(()=>{(window as any).__dashboardState.health.session='following-flight';});await expect(page.getByLabel('Terrain source')).toHaveValue('');
});

test('readiness banner displays backend next steps and watcher status without inventing readiness',async({page})=>{
  await page.setViewportSize({width:2560,height:1440});const state=structuredClone(base);state.smart={status:'waiting',title:'Enter a cockpit to begin',detail:'Your bindings are current. Navigation starts when ownship data arrives.',automatic:true,binding_refresh:{status:'Current'},autostart:{enabled:true,state:'Watching DCS'}};await mock(page,state);await page.goto('/');
  await expect(page.getByText('Enter a cockpit to begin')).toBeVisible();await expect(page.getByText('AUTOMATIC START ON')).toBeVisible();await expect(page.getByText('Bindings: Current')).toBeVisible();
  expect(await page.locator('.scope-canvas').evaluate(element=>element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(500);
  await page.evaluate(()=>{(window as any).__dashboardState.smart.autostart={enabled:false,state:'Unavailable'};});await expect(page.getByText('AUTO-START UNAVAILABLE')).toBeVisible();
});

test('automatic pairing origin allowlist accepts only explicit loopback names',()=>{
  for(const host of ['127.0.0.1','localhost','LOCALHOST','[::1]','::1'])expect(isLoopbackHost(host)).toBe(true);
  for(const host of ['192.168.1.1','localhost.example.com','127.0.0.1.example.com','0.0.0.0','dashboard.local','127.0.0.2'])expect(isLoopbackHost(host)).toBe(false);
});

test('malformed and unsafe stored preferences fall back to display defaults',async({page})=>{
  await mock(page);await page.addInitScript(()=>localStorage.setItem('dcs-copilot-display-v1',JSON.stringify({view:'Execute',brightness:-5,range:90000,orientation:'weapon',route:'false',terrain:'PersianGulf',session:'old-flight',command:{action_id:'f22.nav.next'},mode:'Assist'})));await page.goto('/');
  await expect(page.getByRole('heading',{name:'Navigation Scope.'})).toBeVisible();await expect(page.getByLabel('Scope range')).toHaveValue('80');await expect(page.getByLabel('Display brightness',{exact:true})).toHaveValue('100');await expect(page.getByLabel('Terrain source')).toHaveValue('');
  const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('dcs-copilot-display-v1')!));expect(saved).not.toHaveProperty('terrain');expect(saved).not.toHaveProperty('session');expect(saved).not.toHaveProperty('command');expect(saved).not.toHaveProperty('mode');
});

test('navigation refresh masks old manual library and reloads it after a new generation',async({page})=>{
  const state=structuredClone(base);state.smart={status:'waiting',navigation_refresh:{status:'Current',generation:0}};state.health!.telemetry_fresh=false;await mock(page,state);let name='Old library facility',reads=0;
  await page.route('**/api/navigation/*',route=>{reads++;return route.fulfill({json:{...base.navigation,ownship:null,airfields:[{id:'manual',name,lat:42,lon:42}]}});});
  await page.goto('/');await page.getByLabel('Terrain source').selectOption('Caucasus');await expect(page.getByRole('button',{name:'Select Old library facility',exact:true})).toBeVisible();
  await page.evaluate(()=>{(window as any).__dashboardState.smart.navigation_refresh={status:'Refreshing',generation:0};});await expect(page.getByRole('button',{name:'Select Old library facility',exact:true})).toHaveCount(0);expect(reads).toBe(1);
  name='Refreshed facility';await page.evaluate(()=>{(window as any).__dashboardState.smart.navigation_refresh={status:'Current',generation:1};});await expect(page.getByRole('button',{name:'Select Refreshed facility',exact:true})).toBeVisible();expect(reads).toBe(2);await expect(page.getByLabel('Terrain source')).toHaveValue('Caucasus');
  name='Next generation facility';await page.evaluate(()=>{(window as any).__dashboardState.smart.navigation_refresh={status:'Current',generation:2};});await expect(page.getByRole('button',{name:'Select Next generation facility',exact:true})).toBeVisible();expect(reads).toBe(3);
});

const hornetBindings={aircraft:'FA-18C_hornet',status:'Current',source_fingerprint:'hornet-only',display_only:true,devices:[{id:'keyboard',name:'Keyboard',type:'keyboard'},{id:'tartarus-hornet',name:'Tartarus Hornet',type:'tartarus'}],actions:[{id:'hornet-ltdr',name:'Hornet LTD/R switch',device_id:'keyboard',combos:['RAlt+L'],category:['Sensors'],conflicts:[]}],safe_actions:[]};
const catalogs=[{aircraft:'F-22A',label:'F-22A Raptor',status:'Current'},{aircraft:'FA-18C_hornet',label:'F/A-18C Hornet',status:'Current'}];

test('Hornet active controls never inherit F22 rows and manual offline catalogues stay read-only',async({page})=>{
  const state=structuredClone(base);state.binding_catalogs=catalogs;state.health!.telemetry_fresh=false;await mock(page,state);let posts=0;
  await page.route('**/api/bindings?aircraft=*',route=>route.fulfill({json:new URL(route.request().url()).searchParams.get('aircraft')==='FA-18C_hornet'?hornetBindings:base.bindings}));await page.route('**/api/commands',route=>{posts++;return route.fulfill({json:{}});});
  await page.goto('/');await page.getByRole('button',{name:'Controls',exact:true}).click();await page.getByLabel('Binding aircraft').selectOption('FA-18C_hornet');await expect(page.getByText('Hornet LTD/R switch',{exact:true})).toBeVisible();await expect(page.locator('.binding-row').filter({hasText:'Next waypoint'})).toHaveCount(0);await expect(page.getByText('Manual catalogue · browsing only',{exact:false})).toBeVisible();
  await page.getByLabel('Binding aircraft').selectOption('F-22A');await expect(page.locator('.binding-row').filter({hasText:'Next waypoint'})).toHaveCount(1);
  await page.evaluate(hornet=>{(window as any).__dashboardState.health={...(window as any).__dashboardState.health,aircraft:'FA-18C_hornet',session:'hornet-live',telemetry_fresh:true};(window as any).__dashboardState.bindings=hornet;(window as any).__dashboardState.guide={actions:[]};},hornetBindings);
  await expect(page.getByLabel('Binding aircraft')).toHaveValue('');await expect(page.getByText('Hornet LTD/R switch',{exact:true})).toBeVisible();await expect(page.locator('.binding-row').filter({hasText:'Next waypoint'})).toHaveCount(0);expect(posts).toBe(0);
  await page.getByRole('button',{name:'Guide',exact:true}).click();await expect(page.getByText('No verified actions available for the current aircraft.')).toBeVisible();
});

test('mismatched live binding payload is withheld instead of relabelled for Hornet',async({page})=>{
  const state=structuredClone(base);state.health!.aircraft='FA-18C_hornet';await mock(page,state);await page.goto('/');await page.getByRole('button',{name:'Controls',exact:true}).click();await expect(page.getByText('Waiting for this aircraft’s own binding catalogue.')).toBeVisible();await expect(page.locator('.binding-row')).toHaveCount(0);
});

function sensorFixture(){const state=structuredClone(base);state.health!.aircraft='FA-18C_hornet';state.health!.displays_fresh=true;state.health!.display_age_s=.1;state.health!.export_capabilities={sensor:'AVAILABLE',cockpit:'AVAILABLE',ownship:'AVAILABLE'};state.bindings=hornetBindings;state.sensors={aircraft:'FA-18C_hornet',status:'Unverified',display_freshness:'Aggregate only; individual display timestamps unavailable',contacts:[{name:'SECRET ENEMY CONTACT'}],world_objects:[{name:'SECRET WORLD OBJECT'}],layers:[{id:'flir',status:'Unverified',source:'list_indication(3)',fields:[],last_observed_fields:[{id:'flir.mask_label',label:'Mask label',value:'MASK',display_id:3,element:'MASK',confidence:'MEASURED display text',source:'list_indication(3)'},{id:'raw_enemy',label:'Raw enemy',value:'SECRET RAW SENSOR',display_id:3}]},{id:'laser',status:'Unverified',source:'list_indication(3)',fields:[],last_observed_fields:[{id:'laser.ltdr_code',label:'LTD/R code',value:'1688',display_id:3,element:'LTD/R',confidence:'MEASURED display text',source:'list_indication(3)'}]},{id:'radar',status:'Unverified',fields:[{id:'enemy_count',value:12}],source:'LoGetTWSInfo'},{id:'rwr',status:'Unverified',source:'LoGetSnares',fields:[{id:'rwr_count',value:2}]}]};return state;}

test('legacy FLIR/laser text stays explicitly last-observed and never exposes raw contacts',async({page})=>{
  await mock(page,sensorFixture());await page.goto('/');await page.getByRole('button',{name:'Sensors',exact:true}).click();await expect(page.getByText('MASK',{exact:true}).first()).toBeVisible();await expect(page.getByText('1688',{exact:true})).toBeVisible();await expect(page.getByText('LAST EXPORTED DISPLAY TEXT — INDIVIDUAL DISPLAY AGE UNKNOWN').first()).toBeVisible();await expect(page.getByText('FRESH EXPORTED DISPLAY TEXT')).toHaveCount(0);await expect(page.getByText(/SECRET (ENEMY|WORLD|RAW)/)).toHaveCount(0);await expect(page.getByText('A displayed code or MASK label does not establish that the laser is firing.')).toBeVisible();
});

for(const blocked of ['Stale','Export denied','Wrong aircraft'])test(`sensor text is masked for ${blocked}`,async({page})=>{
  const state=sensorFixture();if(blocked==='Stale')state.health!.displays_fresh=false;if(blocked==='Export denied')state.health!.export_capabilities={sensor:'DENIED'};if(blocked==='Wrong aircraft')state.sensors!.aircraft='F-22A';await mock(page,state);await page.goto('/');await page.getByRole('button',{name:'Sensors',exact:true}).click();await expect(page.getByText('1688',{exact:true})).toHaveCount(0);await expect(page.locator('.sensor-readings')).toHaveCount(0);
});

test('independently fresh sensor fields need a valid per-field age',async({page})=>{
  const state=sensorFixture();state.sensors!.layers=[{id:'laser',status:'Available',age_s:.2,source:'list_indication(3)',fields:[{id:'laser.ltdr_code',label:'LTD/R code',value:'1688',display_id:3,age_s:.2,element:'LTD/R',confidence:'MEASURED display text'},{id:'laser.lst_code',label:'Stale code',value:'1999',display_id:3,age_s:9},{id:'laser.status_label',label:'Unknown-age label',value:'DO NOT DISPLAY',display_id:3}],last_observed_fields:[]}];await mock(page,state);await page.goto('/');await page.getByRole('button',{name:'Sensors',exact:true}).click();await expect(page.getByText('FRESH EXPORTED DISPLAY TEXT')).toBeVisible();await expect(page.getByText('1688',{exact:true})).toBeVisible();await expect(page.getByText('1999',{exact:true})).toHaveCount(0);await expect(page.getByText('DO NOT DISPLAY',{exact:true})).toHaveCount(0);
});

function cockpitFixture(){const state=sensorFixture();state.mission={name:'Training flight — coastal navigation',status:'Available',theatre:'Caucasus',age_s:.1,source:'Mission bridge',detail:'Current mission exported by the bridge.'};state.cockpit={aircraft:'FA-18C_hornet',status:'Available',text_only:true,displays:[{id:3,label:'Right DDI',status:'Available',age_s:.1,source:'list_indication(3)',elements:[{name:'FLIR TRACK MODE',value:'INR AUTO'},{name:'Literal text',value:'<img src=x onerror=alert(1)>\nSECOND LINE'}],last_observed_elements:[]},{id:6,label:'UFC',status:'Available',age_s:.2,elements:[{name:'Scratchpad',value:'1688'}],last_observed_elements:[]},{id:2,label:'Left DDI',status:'Stale',age_s:8,elements:[{name:'Old current',value:'MUST NOT LOOK LIVE'}],last_observed_elements:[{name:'Last label',value:'HISTORICAL LEFT DDI'}]},{id:4,label:'AMPCD',status:'Available',age_s:null,elements:[{name:'Unknown clock',value:'UNKNOWN CLOCK MUST HIDE'}],last_observed_elements:[]}]};return state;}

test('cockpit shows exact escaped fresh text, historical readouts and mission provenance separately',async({page})=>{
  await mock(page,cockpitFixture());let posts=0;await page.route('**/api/commands',route=>{posts++;return route.fulfill({json:{}});});await page.goto('/');await expect(page.locator('.mission-identity strong')).toHaveText('Training flight — coastal navigation');await page.getByRole('button',{name:'Cockpit',exact:true}).click();
  await expect(page.getByText('INR AUTO',{exact:true})).toBeVisible();await expect(page.locator('.cockpit-readout dd').filter({hasText:'<img src=x onerror=alert(1)>'})).toContainText('SECOND LINE');await expect(page.locator('.cockpit-readout img')).toHaveCount(0);await expect(page.getByText('HISTORICAL LEFT DDI',{exact:true})).toBeVisible();await expect(page.getByText('LAST EXPORTED TEXT — HISTORICAL, NOT CURRENT')).toBeVisible();await expect(page.getByText('MUST NOT LOOK LIVE',{exact:true})).toHaveCount(0);await expect(page.getByText('UNKNOWN CLOCK MUST HIDE',{exact:true})).toHaveCount(0);
  await page.getByLabel('Search cockpit text').fill('Scratchpad');await expect(page.getByText('1688',{exact:true})).toBeVisible();await expect(page.getByText('INR AUTO',{exact:true})).toHaveCount(0);expect(posts).toBe(0);
});

for(const blocked of ['Denied','Sensor denied','Wrong aircraft'])test(`cockpit suppresses current and historical text for ${blocked}`,async({page})=>{
  const state=cockpitFixture();if(blocked==='Denied')state.health!.export_capabilities={cockpit:'DENIED'};else if(blocked==='Sensor denied')state.health!.export_capabilities={sensor:'DENIED'};else state.cockpit!.aircraft='F-22A';await mock(page,state);await page.goto('/');await page.getByRole('button',{name:'Cockpit',exact:true}).click();await expect(page.locator('.cockpit-readout')).toHaveCount(0);
});

test('paused cockpit historical text remains explicitly historical even when display status is unavailable',async({page})=>{
  const state=cockpitFixture();state.health!.telemetry_fresh=false;state.cockpit!.displays=[{id:6,label:'UFC',status:'Unavailable',age_s:12,elements:[],last_observed_elements:[{name:'Last scratchpad',value:'PAUSED HISTORY'}]}];await mock(page,state);await page.goto('/');await page.getByRole('button',{name:'Cockpit',exact:true}).click();await expect(page.getByText('PAUSED HISTORY',{exact:true})).toBeVisible();await expect(page.getByText('CURRENT EXPORTED TEXT',{exact:true})).toHaveCount(0);
});

function exportFixture(){const state=cockpitFixture();state.export_data={aircraft:'FA-18C_hornet',session:'test-session',status:'Available',captured_at:123,packet_types:['fast','future_packet'],recording:{status:'Recording',max_bytes:67108864},groups:[{id:'aircraft',label:'Aircraft data',status:'Available',age_s:.2,source:'fast packet',data:{heading:123,future_field:{custom_value:'FUTURE PACKET VALUE'}}},{id:'history',label:'Last exported diagnostics',status:'Last exported',age_s:12,source:'last packet',data:{remembered:'OLD DIAGNOSTIC VALUE'}},{id:'denied',label:'Restricted fields',status:'Denied',age_s:.2,data:{hidden:'DENIED RAW SECRET'}}]};return state;}

test('flight-data search reveals exact future fields and qualified history without commands',async({page})=>{
  await mock(page,exportFixture());let posts=0;await page.route('**/api/commands',route=>{posts++;return route.fulfill({json:{}});});await page.goto('/');await page.getByRole('button',{name:'Flight data',exact:true}).click();await expect(page.getByRole('heading',{name:'Training flight — coastal navigation'})).toBeVisible();await expect(page.getByLabel('Aircraft data exported JSON')).not.toBeVisible();await page.getByLabel('Search exported data').fill('custom_value');await expect(page.getByLabel('Aircraft data exported JSON')).toContainText('FUTURE PACKET VALUE');await expect(page.locator('.export-group')).toHaveCount(1);
  await page.getByLabel('Search exported data').fill('OLD DIAGNOSTIC VALUE');await expect(page.getByText('LAST EXPORTED / UNVERIFIED DATA — NOT A CURRENT TACTICAL PICTURE')).toBeVisible();await expect(page.getByLabel('Last exported diagnostics exported JSON')).toBeVisible();await page.getByLabel('Search exported data').fill('');await page.getByLabel('Export category').selectOption('denied');await page.locator('.export-group>summary').click();await expect(page.getByText('No permitted data exported for this category.')).toBeVisible();await expect(page.getByText('DENIED RAW SECRET',{exact:false})).toHaveCount(0);await expect(page.getByRole('link',{name:'Download latest packet archive'})).toHaveAttribute('href','/api/export/archive/0');expect(posts).toBe(0);
});

test('flight-data session mismatch withholds previous-flight raw values',async({page})=>{
  const state=exportFixture();state.export_data!.session='previous-flight';await mock(page,state);await page.goto('/');await page.getByRole('button',{name:'Flight data',exact:true}).click();await page.getByLabel('Search exported data').fill('FUTURE PACKET VALUE');await expect(page.locator('.export-group')).toHaveCount(0);await expect(page.getByText('No matching exported fields')).toBeVisible();
});

test('Data Health presents the current mission name while keeping export identity in source details',async({page})=>{
  const state=cockpitFixture();state.mission!.status='Current';await mock(page,state);await page.goto('/');await page.getByRole('button',{name:'Data Health',exact:true}).click();const card=page.locator('.health-card').first();await expect(card.getByText('Training flight — coastal navigation',{exact:true})).toBeVisible();await expect(card.getByText('Export identity · test-mission',{exact:true})).not.toBeVisible();await card.getByText('Mission source details',{exact:true}).click();await expect(card.getByText('Export identity · test-mission',{exact:true})).toBeVisible();await expect(card.getByText('Mission bridge',{exact:true})).toBeVisible();
});

function awarenessFixture(){const state=structuredClone(base);state.health!.model_advancing=true;state.aircraft!.unit_name='player-one';state.smart={status:'ready',title:'Aircraft connected',detail:'Navigation is current.',navigation_refresh:{status:'Current',generation:1}};state.awareness={status:'Available',mode:'mission-awareness',source:'Single-player mission hook',ownship_unit:'player-one',age_s:.3,session:'test-session',aircraft:'F-22A',single_player:true,model_advancing:true,total_count:5,truncated:false,contacts:[{id:'air',name:'Mission fighter',type:'MiG-29',category:'airplane',coalition:'red',lat:42.1,lon:42,altitude_ft:10000,speed_kt:350,age_s:.3},{id:'helo',name:'Mission helicopter',type:'Mi-24',category:'helicopter',coalition:'red',lat:42.2,lon:42,altitude_ft:1200,age_s:.3},{id:'ground',name:'Mission vehicle',type:'Tank',category:'ground',coalition:'red',lat:42.02,lon:42.02,altitude_ft:100,age_s:.3},{id:'ship',name:'Mission ship',type:'Frigate',category:'ship',coalition:'red',lat:42.5,lon:42,altitude_ft:0,age_s:.3},{id:'far',name:'Distant mission fighter',type:'Su-27',category:'airplane',coalition:'red',lat:45,lon:42,altitude_ft:20000,age_s:.3}]};return state;}

test('mission-awareness policy rejects uncertain context and expires both snapshot and individual coordinates',()=>{
  const state=awarenessFixture();expect(awarenessView(state,true,true,[42,42]).contacts).toHaveLength(5);expect(awarenessView(state,true,true,[42,42],4.8).contacts).toHaveLength(0);expect(awarenessView(state,false,true,[42,42]).contacts).toHaveLength(0);expect(awarenessView(state,true,false,[42,42]).contacts).toHaveLength(0);expect(awarenessView(state,true,true,undefined).contacts).toHaveLength(0);expect(awarenessView({...state,health:{...state.health,model_advancing:false}},true,true,[42,42]).contacts).toHaveLength(0);
  for(const patch of [{single_player:false},{single_player:null},{session:'another-flight'},{aircraft:'FA-18C_hornet'},{model_advancing:false},{status:'Stale'},{age_s:null},{mode:'radar'},{ownship_unit:'wrong-player'}])expect(awarenessView({...state,awareness:{...state.awareness,...patch}},true,true,[42,42]).contacts).toHaveLength(0);
  const contacts=(state.awareness!.contacts as any[]);state.awareness!.contacts=[...contacts,{...contacts[0],id:'bad-coordinate',lat:95},{...contacts[0],id:'old',age_s:5.1},{...contacts[0],id:'no-clock',age_s:null},{...contacts[0],id:'valid-zero',lat:0,lon:0}];expect(awarenessView(state,true,true,[42,42]).contacts.map(c=>c.id).sort()).toEqual(['air','helo','ground','ship','far','valid-zero'].sort());
});

for(const viewport of [{width:2560,height:1440},{width:1180,height:820},{width:820,height:1180}])test(`active mission-awareness layer fits ${viewport.width}x${viewport.height}`,async({page})=>{
  await page.setViewportSize(viewport);await mock(page,awarenessFixture());await page.goto('/');await expect(page.getByRole('button',{name:'Mission enemies',exact:true})).toHaveAttribute('aria-pressed','true');await expect(page.locator('.enemy-symbol')).toHaveCount(4);await expect(page.locator('.enemy-row')).toHaveCount(5);await expect(page.getByText('Single-player only · Not aircraft radar',{exact:true})).toBeVisible();await expect(page.getByText('5 enemies · 4 on scope',{exact:true})).toBeVisible();await page.getByRole('button',{name:'Select mission enemy Mission fighter',exact:true}).click();await expect(page.locator('.navigation-solution[data-selection-kind="contact"]')).toContainText(/10,000\s*ft/);await expect(page.locator('.navigation-solution[data-selection-kind="contact"]')).toContainText(/350\s*kt/);await overflow(page);if(viewport.width===2560)expect(await page.locator('.scope-canvas').evaluate(element=>element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(500);await page.screenshot({path:`test-results/mission-awareness-fixture-${viewport.width}.png`,fullPage:true});
});

test('mission enemies respect range and track-up while out-of-range units remain inspectable',async({page})=>{
  await mock(page,awarenessFixture());await page.goto('/');const symbol=page.getByRole('button',{name:'Select mission enemy Mission fighter',exact:true});const north=await symbol.getAttribute('transform');await page.getByRole('button',{name:'Track up',exact:true}).click();expect(await symbol.getAttribute('transform')).not.toBe(north);await page.getByLabel('Scope range').selectOption('10');await expect(page.locator('.enemy-symbol')).toHaveCount(2);await expect(page.locator('.enemy-row')).toHaveCount(5);await page.getByRole('button',{name:'Inspect mission enemy Distant mission fighter',exact:true}).click();await expect(page.locator('.navigation-solution[data-selection-kind="contact"]')).toContainText('Outside scope range');await expect(page.locator('.navigation-solution[data-selection-kind="contact"]')).toContainText(/Unknown\s*kt/);await page.getByLabel('Scope range').selectOption('320');await expect(page.getByRole('button',{name:'Select mission enemy Distant mission fighter',exact:true})).toBeVisible();
});

test('multiplayer, stale and changed-session updates remove mission-enemy symbols and selected details',async({page})=>{
  await mock(page,awarenessFixture());await page.goto('/');await page.getByRole('button',{name:'Select mission enemy Mission fighter',exact:true}).click();for(const update of [{single_player:false},{single_player:null},{single_player:true,age_s:7},{age_s:.1,session:'wrong-session'},{session:'test-session',model_advancing:false}]){await page.evaluate(patch=>Object.assign((window as any).__dashboardState.awareness,patch),update);await expect(page.locator('.enemy-symbol')).toHaveCount(0);await expect(page.locator('.enemy-row')).toHaveCount(0);await expect(page.locator('.navigation-solution[data-selection-kind="contact"]')).toHaveCount(0);}await page.evaluate(()=>Object.assign((window as any).__dashboardState.awareness,{model_advancing:true}));await expect(page.locator('.enemy-symbol')).toHaveCount(4);await expect(page.locator('.navigation-solution[data-selection-kind="contact"]')).toHaveCount(0);
});

test('mission-enemy display preference persists without positions; offline libraries and disconnects hide contacts',async({page})=>{
  await mock(page,awarenessFixture());let posts=0;await page.route('**/api/commands',route=>{posts++;return route.fulfill({json:{}});});await page.goto('/');await expect(page.locator('.enemy-symbol')).toHaveCount(4);await page.getByLabel('Terrain source').selectOption('Caucasus');await expect(page.locator('.enemy-symbol')).toHaveCount(0);await expect(page.locator('.enemy-row')).toHaveCount(0);await expect(page.getByText('Hidden · offline library',{exact:true})).toBeVisible();await page.getByLabel('Terrain source').selectOption('');await expect(page.locator('.enemy-symbol')).toHaveCount(4);await page.getByRole('button',{name:'Mission enemies',exact:true}).click();await expect(page.locator('.enemy-symbol')).toHaveCount(0);await page.reload();await expect(page.getByRole('button',{name:'Mission enemies',exact:true})).toHaveAttribute('aria-pressed','false');const saved=await page.evaluate(()=>localStorage.getItem('dcs-copilot-display-v1')!);expect(saved).not.toContain('Mission fighter');expect(JSON.parse(saved).enemies).toBe(false);await page.getByRole('button',{name:'Mission enemies',exact:true}).click();await expect(page.locator('.enemy-symbol')).toHaveCount(4);await page.route('**/api/state',route=>route.abort());await page.evaluate(()=>{(window as any).__silenceStream=true;(window as any).__streams.at(-1).onerror();});await expect(page.locator('.enemy-symbol')).toHaveCount(0);await expect(page.locator('.enemy-row')).toHaveCount(0);expect(posts).toBe(0);
});

test('mission-awareness export truncation and omitted records are explicit',async({page})=>{
  const state=awarenessFixture();Object.assign(state.awareness!,{truncated:true,total_count:1200,omitted_count:3,notes:['Snapshot capped at 1000 units.']});await mock(page,state);await page.goto('/');await expect(page.getByText('Partial export · 1200 total enemies reported.',{exact:false})).toBeVisible();await expect(page.getByText('3 records dropped.',{exact:false})).toBeVisible();await expect(page.getByText('Snapshot capped at 1000 units.',{exact:false})).toBeVisible();
});

test('full automatic airfield directory scrolls independently beside an active desktop awareness layer',async({page})=>{
  const state=awarenessFixture();state.navigation!.airfields=Array.from({length:29},(_,i)=>({id:`field-${i}`,name:`Full directory airfield ${i+1}`,lat:42+i*.02,lon:42+i*.02,runways:[],frequencies:[]}));await page.setViewportSize({width:2560,height:1440});await mock(page,state);await page.goto('/');await expect(page.locator('.airfield-item')).toHaveCount(29);await expect(page.locator('.enemy-row')).toHaveCount(5);expect(await page.locator('.scope-canvas').evaluate(element=>element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(500);expect(await page.locator('.destination-list').evaluate(element=>element.scrollHeight>element.clientHeight)).toBe(true);expect(await page.locator('.airfield-item').first().evaluate(element=>element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44);
});
