import { test,expect,type Page } from '@playwright/test';
import { presetWorkspace,validateWorkspace,workspaceKey } from '../src/workspace';
import type { RemoteState } from '../src/remote-types';
const aircraft='FA-18C_hornet';
const state={server:{dry_run:true,remote_armed:false},health:{aircraft,session:'remote-test',status:'Live',telemetry_fresh:true,telemetry_age_s:.1,displays_fresh:true,display_age_s:.1,dcs_running:true},bindings:{aircraft,status:'Current',devices:[{id:'keyboard',name:'Keyboard',type:'keyboard'},{id:'stick',name:'My HOTAS',type:'joystick'}],actions:Array.from({length:6000},(_,i)=>({id:`action-${i}`,name:`Reference action ${i}`,device_id:i<5000?'keyboard':'stick',combos:i%2?['JOY_BTN1']:[],category:['Flight']}))},cockpit:{aircraft,status:'Available',displays:[2,3,4,6].map(id=>({id,label:id===6?'UFC':`DDI ${id}`,status:'Available',age_s:.1,elements:[{name:'Scratchpad',value:`DISPLAY ${id} CURRENT`}]}))}};
function catalog():RemoteState{return {status:'Ready',mode:'preview',armed:false,controller_connected:false,can_edit:true,busy:false,aircraft,panels:[{id:'ufc',label:'UFC',buttons:[{id:'hornet.ufc.digit.1',name:'UFC Keyboard Pushbutton - 1',label:'1',available:true},{id:'hornet.ufc.ent',name:'UFC Keyboard Pushbutton - ENT',label:'ENT',available:false,reason:'No keyboard binding'}]},...['left_mdi','right_mdi','ampcd'].map(id=>({id,label:id==='left_mdi'?'Left DDI':id==='right_mdi'?'Right DDI':'AMPCD',buttons:Array.from({length:20},(_,i)=>({id:`hornet.${id}.pb.${i+1}`,name:`${id} PB ${i+1}`,label:String(i+1),available:true}))}))],workflows:[{id:'fixture-workflow',name:'Fixture sequence',aircraft,steps:[{type:'action',action_id:'hornet.ufc.digit.1'},{type:'delay',seconds:.5}]}],scripts:[{id:'mission-status',name:'Mission status',description:'Display mission time',aircraft:['*'],context:'mission',status:'Registered',available:true,sha256:'a'.repeat(64)}]};}
async function mock(page:Page,remote=catalog()){
  const requests:Array<{path:string;body:any;method:string}>=[];
  let connectionId='';
  await page.addInitScript(fixture=>{(window as any).__remoteFixture=fixture;(window as any).EventSource=class{onmessage:any;timer:any;constructor(){this.timer=setInterval(()=>this.onmessage?.({data:JSON.stringify((window as any).__remoteFixture)}),300);}addEventListener(){}close(){clearInterval(this.timer);}};},state);
  await page.route('**/api/state',route=>route.fulfill({json:state}));
  await page.route('**/api/remote**',async route=>{
    const req=route.request(),path=new URL(req.url()).pathname;
    const compact=()=>({...remote,panels:[],workflows:[],scripts:[]});
    if(req.method()==='GET')return route.fulfill({json:path.endsWith('/status')?compact():remote});
    const body=req.postData()?req.postDataJSON():{};requests.push({path,body,method:req.method()});
    if(path.endsWith('/heartbeat')){if(connectionId!==body.connection_id){remote.armed=false;remote.mode='preview';}connectionId=body.connection_id;remote.controller_connected=true;}
    if(path.endsWith('/disconnect')&&connectionId===body.connection_id){connectionId='';remote.controller_connected=false;remote.armed=false;remote.mode='preview';}
    if(path.endsWith('/arm')){remote.armed=body.enabled;remote.mode=body.enabled?'live':'preview';}
    if(path.endsWith('/workflows'))remote.workflows.push(body);
    const action=['/tap','/run','/script'].some(end=>path.endsWith(end));
    if(action)remote.last_run={id:body.idempotency_key,status:'Completed',mode:remote.mode,step_index:1,step_count:1,message:remote.mode==='live'?'Live input completed.':'Preview complete. No inputs sent.'};
    if(path.endsWith('/stop')){remote.active_run=null;remote.busy=false;}
    return route.fulfill({json:action?remote.last_run:compact()});
  });
  page.on('request',req=>{const path=new URL(req.url()).pathname;if(path.startsWith('/api/commands')&&req.method()==='POST')requests.push({path,body:req.postDataJSON(),method:'POST'});});
  await page.goto('/');return {requests,remote};
}
async function openRemote(page:Page){await page.getByRole('button',{name:'Remote',exact:true}).click();await expect(page.getByRole('heading',{name:'Remote.'})).toBeVisible();await expect(page.getByRole('button',{name:'Connect controls',exact:true})).toBeEnabled();await page.getByRole('button',{name:'UFC',exact:true}).click();}
const actuations=(rows:Array<{path:string}>)=>rows.filter(row=>/\/(tap|run|script|arm|commands)$/.test(row.path));

test('6000-action mobile reference searches all rows, selects one device and keeps favourites without sending',async({page})=>{
  await page.setViewportSize({width:390,height:844});const {requests}=await mock(page);await page.getByRole('button',{name:'Controls',exact:true}).click();
  await expect(page.locator('.binding-row')).toHaveCount(100);await expect(page.locator('.reference-diagrams')).not.toHaveAttribute('open','');await page.getByLabel('Search bindings').fill('Reference action 4999');await expect(page.locator('.binding-row')).toHaveCount(1);await page.getByRole('button',{name:'Favourite Reference action 4999',exact:true}).click();
  await page.getByLabel('Search bindings').fill('');await page.getByRole('button',{name:'Favourites',exact:true}).click();await expect(page.locator('.binding-row')).toHaveCount(1);await page.reload();await page.getByRole('button',{name:'Favourites',exact:true}).click();await expect(page.getByText('Reference action 4999',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'All',exact:true}).click();await page.getByLabel('Binding device').selectOption('stick');await page.getByLabel('Search bindings').fill('5999');await expect(page.getByText('Reference action 5999',{exact:true})).toBeVisible();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);expect(requests).toEqual([]);
});

for(const viewport of [{width:390,height:844},{width:844,height:390},{width:1180,height:820},{width:2560,height:1440}])test(`remote layouts fit ${viewport.width}x${viewport.height} with no automatic control calls`,async({page})=>{
  await page.setViewportSize(viewport);const {requests}=await mock(page);await openRemote(page);const phone=viewport.width<700||viewport.height<500;await expect(page.locator('.remote-panel-slot')).toHaveCount(phone?1:2);
  for(const style of ['touch','cockpit']){await page.getByLabel('Remote panel style',{exact:true}).selectOption(style);await page.getByRole('button',{name:'Left DDI',exact:true}).click();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);for(const button of await page.locator('.remote-panel-slot .remote-control').all()){const bounds=await button.boundingBox();expect(bounds!.height).toBeGreaterThanOrEqual(44);expect(bounds!.width).toBeGreaterThanOrEqual(44);}if(phone){await page.getByRole('button',{name:'Left DDI',exact:true}).click();await page.locator('.remote-panel-slot').first().scrollIntoViewIfNeeded();const bounds=await page.getByRole('navigation',{name:'Mobile navigation'}).boundingBox();expect(bounds!.y).toBeGreaterThan(0);expect(bounds!.y+bounds!.height).toBeLessThanOrEqual(viewport.height+1);expect(bounds!.y+bounds!.height).toBeGreaterThanOrEqual(viewport.height-1);}await page.screenshot({path:`test-results/remote/${style}-${viewport.width}.png`,fullPage:true});}
  if(phone){await page.evaluate(()=>{const prefs=JSON.parse(localStorage.getItem('dcs-copilot-display-v1')!);localStorage.setItem('dcs-copilot-display-v1',JSON.stringify({...prefs,theme:'daylight',brightness:35}));});await page.reload();await expect(page.locator('html')).toHaveAttribute('data-theme','daylight');await expect(page.getByRole('navigation',{name:'Mobile navigation'})).toHaveCSS('filter','brightness(0.35)');await page.getByRole('button',{name:'Left DDI',exact:true}).click();await page.locator('.remote-panel-slot').first().scrollIntoViewIfNeeded();const bounds=await page.getByRole('navigation',{name:'Mobile navigation'}).boundingBox();expect(bounds!.y+bounds!.height).toBeLessThanOrEqual(viewport.height+1);}
  expect(requests).toEqual([]);
});

test('workspace imports reject commands and invalid panels; mixed styles persist through phone rotation without actuation',async({page})=>{
  await page.setViewportSize({width:390,height:844});const {requests}=await mock(page);await openRemote(page);await page.getByText('Edit workspace · phone layout',{exact:true}).click();await page.getByLabel('Left DDI panel style',{exact:true}).selectOption('cockpit');await page.getByLabel('Left DDI panel size',{exact:true}).selectOption('large');await page.getByRole('button',{name:'Move Left DDI down',exact:true}).click();
  const saved=await page.evaluate(key=>JSON.parse(localStorage.getItem(key)!),workspaceKey(aircraft,'phone'));expect(saved.panels.find((item:any)=>item.id==='left-ddi')).toMatchObject({style:'cockpit',size:'large'});expect(saved.panels[0].id).toBe('right-ddi');
  await page.getByLabel('Import workspace JSON').setInputFiles({name:'bad.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({...presetWorkspace(),commands:['send']}))});await expect(page.getByRole('alert')).toContainText('not a valid companion layout');expect(validateWorkspace({...presetWorkspace(),panels:[{id:'arbitrary',visible:true,size:'regular'}]})).toBeUndefined();
  await page.setViewportSize({width:844,height:390});await expect(page.locator('.remote-workspace')).toHaveAttribute('data-device','phone');await expect(page.getByLabel('Left DDI panel style',{exact:true})).toHaveValue('cockpit');await page.reload();await page.getByText('Edit workspace · phone layout',{exact:true}).click();await expect(page.getByLabel('Left DDI panel style',{exact:true})).toHaveValue('cockpit');expect(actuations(requests)).toEqual([]);
});

test('one explicit remote tap sends only a registered ID and fresh UUID, missing binding stays disabled and reload never replays',async({page})=>{
  const {requests}=await mock(page);await openRemote(page);const one=page.getByRole('button',{name:'UFC Keyboard Pushbutton - 1',exact:true});await expect(one).toBeDisabled();await expect(page.getByRole('button',{name:'UFC Keyboard Pushbutton - ENT',exact:true})).toBeDisabled();await expect(page.getByText('No keyboard binding',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Connect controls',exact:true}).click();await expect(one).toBeEnabled();await one.click();await expect(page.locator('.remote-result')).toContainText('Preview complete');expect(actuations(requests)).toHaveLength(1);expect(actuations(requests)[0].body).toEqual({action_id:'hornet.ufc.digit.1',idempotency_key:expect.stringMatching(/^[a-f0-9-]{36}$/)});
  await page.reload();await expect(page.getByRole('button',{name:'Connect controls',exact:true})).toBeEnabled();await page.getByRole('button',{name:'UFC',exact:true}).click();await expect(one).toBeDisabled();expect(actuations(requests)).toHaveLength(1);
});

test('remote readouts withhold stale and denied text while layout remains available',async({page})=>{
  await mock(page);await openRemote(page);await expect(page.getByText('DISPLAY 6 CURRENT',{exact:true})).toBeVisible();await page.evaluate(()=>{(window as any).__remoteFixture.cockpit.displays.find((item:any)=>item.id===6).age_s=20;});await expect(page.getByText('DISPLAY 6 CURRENT',{exact:true})).toHaveCount(0);await expect(page.getByLabel('UFC exported readout')).toContainText('Stale / unavailable');
  await page.evaluate(()=>{(window as any).__remoteFixture.health.export_capabilities={cockpit:'DENIED'};});await expect(page.getByLabel('UFC exported readout')).toContainText('Export denied');
});

test('PC workflow editor saves bounded catalogue steps without running and phone keeps editing separate from arming',async({page})=>{
  const {requests,remote}=await mock(page);await openRemote(page);await page.getByRole('button',{name:'Workflows',exact:true}).click();await page.getByRole('button',{name:'Create workflow',exact:true}).click();await page.getByLabel('Workflow name', {exact:true}).fill('My navigation check');await page.getByRole('button',{name:'Add control',exact:true}).click();await page.getByRole('button',{name:'Add delay',exact:true}).click();await page.getByRole('button',{name:'Add display check',exact:true}).click();await page.getByRole('button',{name:'Add script',exact:true}).click();await page.getByRole('button',{name:'Save workflow',exact:true}).click();await expect(page.getByRole('button',{name:'Run My navigation check',exact:true})).toBeVisible();
  const payload=requests.find(row=>row.path==='/api/remote/workflows')!.body;expect(payload.steps.map((step:any)=>step.type)).toEqual(['action','delay','wait','mission_script']);expect(actuations(requests)).toHaveLength(0);remote.can_edit=false;remote.can_arm=true;await page.setViewportSize({width:390,height:844});await page.reload();await page.getByRole('button',{name:'Workflows',exact:true}).click();await expect(page.getByRole('button',{name:'Create workflow',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'Edit Fixture sequence',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'Enable live controls',exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'Enable live controls',exact:true})).toBeDisabled();expect(actuations(requests)).toHaveLength(0);
});

test('paired phone explicitly connects, enables live and taps once; scoped disconnect and reload never replay',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  const fixture=catalog();fixture.can_edit=false;fixture.can_arm=true;
  const {requests}=await mock(page,fixture);await openRemote(page);
  const session=page.locator('.remote-session'),enable=page.getByRole('button',{name:'Enable live controls',exact:true}),one=page.getByRole('button',{name:'UFC Keyboard Pushbutton - 1',exact:true});
  await expect(session).toContainText('Controls disconnected');await expect(enable).toBeDisabled();await expect(one).toBeDisabled();expect(requests).toEqual([]);
  await page.getByRole('button',{name:'Connect controls',exact:true}).click();await expect(enable).toBeEnabled();await expect(session).toContainText('Connected in preview');expect(actuations(requests)).toEqual([]);
  const connectionId=requests.find(row=>row.path==='/api/remote/heartbeat')!.body.connection_id;expect(connectionId).toMatch(/^[a-f0-9-]{36}$/);
  await enable.click();await expect(session).toContainText('Live controls enabled for this flight');await expect(one).toBeEnabled();expect(actuations(requests).map(row=>row.path)).toEqual(['/api/remote/arm']);
  await one.click();await expect(page.locator('.remote-result')).toContainText('Live input completed');expect(actuations(requests).map(row=>row.path)).toEqual(['/api/remote/arm','/api/remote/tap']);expect(actuations(requests)[1].body).toEqual({action_id:'hornet.ufc.digit.1',idempotency_key:expect.stringMatching(/^[a-f0-9-]{36}$/)});
  await page.getByRole('button',{name:'Disconnect controls',exact:true}).click();await expect(session).toContainText('Controls disconnected');await expect(one).toBeDisabled();await expect(enable).toBeDisabled();expect(requests.filter(row=>row.path==='/api/remote/disconnect')).toEqual([{path:'/api/remote/disconnect',method:'POST',body:{connection_id:connectionId}}]);expect(requests.filter(row=>row.path==='/api/remote/stop')).toEqual([]);
  await page.getByRole('button',{name:'Connect controls',exact:true}).click();await expect(enable).toBeEnabled();await expect(session).toContainText('Connected in preview');expect(actuations(requests)).toHaveLength(2);
  await enable.click();await expect(session).toContainText('Live controls enabled for this flight');expect(actuations(requests)).toHaveLength(3);
  await page.reload();await expect(page.getByRole('button',{name:'Connect controls',exact:true})).toBeEnabled();await page.getByRole('button',{name:'UFC',exact:true}).click();await expect(session).toContainText('Controls disconnected');await expect(enable).toBeDisabled();await expect(one).toBeDisabled();expect(actuations(requests)).toHaveLength(3);
  await page.getByRole('button',{name:'Connect controls',exact:true}).click();await expect(enable).toBeEnabled();await expect(session).toContainText('Connected in preview');expect(actuations(requests)).toHaveLength(3);
  await page.getByRole('button',{name:'Workflows',exact:true}).click();await expect(page.getByRole('button',{name:'Create workflow',exact:true})).toHaveCount(0);await expect(page.getByRole('button',{name:'Edit Fixture sequence',exact:true})).toHaveCount(0);
});

test('workflow and registered script run by ID with visible progress and explicit Stop; no run on reconnect',async({page})=>{
  const {requests,remote}=await mock(page);await openRemote(page);await page.getByRole('button',{name:'Connect controls',exact:true}).click();await page.getByRole('button',{name:'Workflows',exact:true}).click();const run=page.getByRole('button',{name:'Run Fixture sequence',exact:true});await expect(run).toBeEnabled();await run.click();expect(actuations(requests).at(-1)?.body).toMatchObject({workflow_id:'fixture-workflow'});await expect(page.locator('.workflow-progress')).toContainText('Completed');
  await page.getByRole('button',{name:'Run Mission status',exact:true}).click();expect(actuations(requests).at(-1)?.body).toMatchObject({script_id:'mission-status'});remote.active_run={id:'active',status:'Running',mode:'preview',step_index:0,step_count:2,message:'Waiting'};remote.busy=true;await expect(page.getByRole('button',{name:'Stop current run',exact:true})).toBeEnabled();await page.getByRole('button',{name:'Stop current run',exact:true}).click();expect(requests.filter(row=>row.path==='/api/remote/stop')).toHaveLength(1);await page.reload();expect(actuations(requests)).toHaveLength(2);
});

test('global execution ribbon distinguishes armed remote controls from the Guide preview',async({page})=>{
  await mock(page);await page.evaluate(()=>{(window as any).__remoteFixture.server.remote_armed=true;});await expect(page.locator('.health-ribbon .safety-mode')).toHaveText('Remote LIVE enabled · Guide preview');
});

test('compact active-run polling restores the next touch promptly and preserves catalogue arrays',async({page})=>{
  const {remote}=await mock(page);let polls=0,catalogReads=0;
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/remote')catalogReads++;});
  await page.route('**/api/remote/tap',route=>{remote.active_run={id:route.request().postDataJSON().idempotency_key,status:'Queued',mode:'preview',step_index:0,step_count:1};remote.busy=true;return route.fulfill({json:remote.active_run});});
  await page.route('**/api/remote/status',route=>{polls++;if(polls>=2){remote.last_run={...remote.active_run!,status:'Completed',step_index:1};remote.active_run=null;remote.busy=false;}return route.fulfill({json:{...remote,panels:[],workflows:[],scripts:[]}});});
  await openRemote(page);await page.getByRole('button',{name:'Connect controls',exact:true}).click();const button=page.getByRole('button',{name:'UFC Keyboard Pushbutton - 1',exact:true});await expect(button).toBeEnabled();await button.click();await expect(button).toBeEnabled({timeout:1300});expect(polls).toBeGreaterThanOrEqual(2);expect(catalogReads).toBe(1);await expect(page.getByRole('button',{name:'UFC Keyboard Pushbutton - ENT',exact:true})).toBeDisabled();
});

test('completed tap acknowledgement releases controls without waiting for a delayed status read',async({page})=>{
  const {requests}=await mock(page);let releaseStatus:(()=>void)|undefined,statusStarted=false;
  await page.route('**/api/remote/status',async route=>{statusStarted=true;await new Promise<void>(resolve=>{releaseStatus=resolve;});await route.fulfill({json:{...catalog(),controller_connected:true,panels:[],workflows:[],scripts:[]}});});
  await openRemote(page);await page.getByRole('button',{name:'Connect controls',exact:true}).click();const button=page.getByRole('button',{name:'UFC Keyboard Pushbutton - 1',exact:true});await expect(button).toBeEnabled();
  try{
    await button.click();await expect.poll(()=>statusStarted).toBe(true);await expect(page.locator('.remote-result')).toContainText('Completed');
    await expect(button).toBeEnabled({timeout:500});expect(actuations(requests)).toHaveLength(1);
  }finally{releaseStatus?.();}
});

test('queued tap stays blocked after acknowledgement until status confirms completion and duplicate touches are not queued',async({page})=>{
  const {remote}=await mock(page);let releaseStatus:(()=>void)|undefined,statusStarted=false,taps=0;
  await page.route('**/api/remote/tap',route=>{taps++;remote.active_run={id:route.request().postDataJSON().idempotency_key,status:'Queued',mode:'preview',step_index:0,step_count:1};remote.busy=true;return route.fulfill({json:remote.active_run});});
  await page.route('**/api/remote/status',async route=>{statusStarted=true;await new Promise<void>(resolve=>{releaseStatus=resolve;});remote.last_run={...remote.active_run!,status:'Completed',step_index:1};remote.active_run=null;remote.busy=false;await route.fulfill({json:{...remote,panels:[],workflows:[],scripts:[]}});});
  await openRemote(page);await page.getByRole('button',{name:'Connect controls',exact:true}).click();const button=page.getByRole('button',{name:'UFC Keyboard Pushbutton - 1',exact:true});await expect(button).toBeEnabled();
  try{
    await button.evaluate(element=>{(element as HTMLButtonElement).click();(element as HTMLButtonElement).click();});await expect.poll(()=>statusStarted).toBe(true);
    await expect(page.locator('.remote-result')).toContainText('Queued');await expect(button).toBeDisabled();expect(taps).toBe(1);
    releaseStatus!();await expect(page.locator('.remote-result')).toContainText('Completed');await expect(button).toBeEnabled({timeout:500});expect(taps).toBe(1);
    await page.reload();await expect(page.getByRole('button',{name:'Connect controls',exact:true})).toBeEnabled();expect(taps).toBe(1);
  }finally{releaseStatus?.();}
});

test('Stop reaches the server while a run acknowledgement is pending and its late response cannot restore the run',async({page})=>{
  const {remote}=await mock(page);let release:(()=>void)|undefined,runs=0,stops=0;
  await page.route('**/api/remote/run',async route=>{runs++;const queued={id:route.request().postDataJSON().idempotency_key,status:'Queued',mode:'preview',step_index:0,step_count:2};remote.active_run=queued;remote.busy=true;await new Promise<void>(resolve=>{release=resolve;});await route.fulfill({json:queued});});
  await page.route('**/api/remote/stop',route=>{stops++;remote.last_run={...remote.active_run!,status:'Cancelled',message:'Stopped explicitly'};remote.active_run=null;remote.busy=false;return route.fulfill({json:{...remote,panels:[],workflows:[],scripts:[]}});});
  await openRemote(page);await page.getByRole('button',{name:'Connect controls',exact:true}).click();await page.getByRole('button',{name:'Workflows',exact:true}).click();await page.getByRole('button',{name:'Run Fixture sequence',exact:true}).click();await expect.poll(()=>runs).toBe(1);
  await expect(page.getByRole('button',{name:'Stop current run',exact:true})).toBeEnabled();await page.getByRole('button',{name:'Stop current run',exact:true}).click();await expect.poll(()=>stops).toBe(1);await expect(page.locator('.remote-result')).toContainText('Cancelled');release!();await page.waitForTimeout(400);await expect(page.locator('.remote-result')).toContainText('Cancelled');await page.reload();expect(runs).toBe(1);
});
