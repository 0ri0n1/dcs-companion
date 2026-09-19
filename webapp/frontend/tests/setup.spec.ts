import { test, expect, type Page } from '@playwright/test';
import type { SetupState, Snapshot } from '../src/types';

const state:Snapshot={server:{dry_run:true,lan_enabled:false,version:'setup-test'},health:{status:'Live',aircraft:'FA-18C_hornet',terrain:'Caucasus',telemetry_age_s:.1,telemetry_fresh:true,dcs_running:true},navigation:{terrain:'Caucasus',airfields:[],navaids:[],route:[]}};
function setupFixture():SetupState {
  return {
    status:'Ready',selected:{install_id:'install-one',profile_id:'profile-one',install_path:'E:\\Simulator\\DCS World',profile_path:'C:\\Users\\Pilot\\Saved Games\\DCS'},
    install_candidates:[{id:'install-one',name:'DCS World',path:'E:\\Simulator\\DCS World',evidence:['Running DCS process'],valid:true},{id:'install-two',name:'DCS World Steam',path:'F:\\Steam Library\\steamapps\\common\\DCSWorld',evidence:['Steam library'],valid:true},{id:'missing',name:'Removed installation',path:'G:\\DCS',evidence:['Old registration'],valid:false}],
    profile_candidates:[{id:'profile-one',name:'DCS',path:'C:\\Users\\Pilot\\Saved Games\\DCS',evidence:['Saved Games folder'],valid:true},{id:'profile-two',name:'DCS.openbeta',path:'D:\\Flight Profiles\\DCS.openbeta',evidence:['Saved Games folder'],valid:true}],
    issues:[],inventory:{installed_aircraft:[{id:'FA-18C_hornet',name:'F/A-18C Hornet',path:'E:\\Simulator\\DCS World\\Mods\\aircraft\\FA-18C'},{id:'F-16C_50',name:'F-16C Viper'}],saved_aircraft:[{id:'FA-18C_hornet',name:'FA-18C_hornet'}],devices:[{id:'joystick-one',name:'Fixture HOTAS',type:'joystick',guid:'synthetic-guid',connection:'Unknown',aircraft:['FA-18C_hornet'],diff_files:[{aircraft:'FA-18C_hornet',path:'C:\\Users\\Pilot\\Saved Games\\DCS\\Config\\Input\\FA-18C_hornet\\joystick\\Fixture HOTAS.diff.lua'}]}],counts:{installed_aircraft:2,saved_aircraft:1,devices:1,diff_files:1}},
    active:{install_path:'E:\\Simulator\\DCS World',profile_path:'C:\\Users\\Pilot\\Saved Games\\DCS'},pending_restart:false,selection_locked:false,assignment_policy:'read-only',
    bindings:[{aircraft:'FA-18C_hornet',label:'F/A-18C Hornet',status:'Current',counts:{actions_total:100,actions_bound:70,devices:2,conflicting_combos:3},display_only:true},{aircraft:'F-16C_50',label:'F-16C Viper',status:'Detected only',reason:'No resolved catalogue is available for this aircraft.',display_only:true}],
  };
}
async function mock(page:Page,snapshot:Snapshot=state) {
  const events={setupReads:[] as string[],selections:[] as unknown[],commands:0};
  let setup=setupFixture(),getError='',saveError='';
  await page.addInitScript(snapshot=>{
    (window as any).EventSource=class {onmessage:any;onerror:any;timer:any;constructor(){this.timer=setInterval(()=>this.onmessage?.({data:JSON.stringify(snapshot)}),300);}addEventListener(){}close(){clearInterval(this.timer);}};
  },snapshot);
  await page.route('**/api/state',route=>route.fulfill({json:snapshot}));
  await page.route('**/api/session',route=>route.fulfill({json:{ok:true}}));
  await page.route(/\/api\/setup(?:\?.*)?$/,route=>{
    events.setupReads.push(route.request().url());
    return getError?route.fulfill({status:503,json:{detail:getError}}):route.fulfill({json:setup});
  });
  await page.route('**/api/setup/selection',route=>{
    const selected=route.request().postDataJSON();events.selections.push(selected);
    if(saveError)return route.fulfill({status:409,json:{detail:saveError}});
    setup={...setup,pending_restart:true,selected:{install_id:selected.install_id,profile_id:selected.profile_id,install_path:setup.install_candidates.find(c=>c.id===selected.install_id)!.path,profile_path:setup.profile_candidates.find(c=>c.id===selected.profile_id)!.path}};
    return route.fulfill({json:setup});
  });
  page.on('request',request=>{if(request.method()==='POST'&&new URL(request.url()).pathname.startsWith('/api/commands'))events.commands++;});
  return {events,change:(next:SetupState)=>{setup=next;},failGet:(message:string)=>{getError=message;},failSave:(message:string)=>{saveError=message;}};
}
async function open(page:Page) {
  await page.getByRole('button',{name:'Your setup',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'Your setup',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Refresh setup',exact:true})).toBeEnabled();
}

test('setup waits for the companion connection and remains available when DCS is stopped',async({page})=>{
  const stopped:Snapshot={...state,health:{status:'Waiting for DCS',dcs_running:false,telemetry_fresh:false}};
  const {events}=await mock(page,stopped);let release!:()=>void;const waiting=new Promise<void>(resolve=>{release=resolve;});
  await page.route('**/api/state',async route=>{await waiting;return route.fulfill({json:stopped});});
  await page.goto('/');await expect(page.getByRole('button',{name:'Your setup',exact:true})).toBeDisabled();expect(events.setupReads).toHaveLength(0);
  release();await open(page);await expect(page.getByRole('combobox',{name:'DCS installation',exact:true})).toBeVisible();expect(events.setupReads).toHaveLength(1);expect(events.commands).toBe(0);
});

test('setup reads on demand, distinguishes discovered aircraft and verified bindings, and never claims devices are connected',async({page})=>{
  const {events}=await mock(page);await page.goto('/');expect(events.setupReads).toHaveLength(0);await open(page);
  await expect(page.getByText('Learns your existing controls. DCS bindings are not changed.',{exact:true})).toBeVisible();
  const hornet=page.locator('.setup-binding').filter({hasText:'F/A-18C Hornet'});
  await expect(hornet).toContainText('70');await expect(hornet).toContainText('30');await expect(hornet).toContainText('3');
  const viper=page.locator('.setup-binding').filter({hasText:'F-16C Viper'});await expect(viper).toContainText('Detected only');await expect(viper.locator('dl')).toHaveCount(0);
  await expect(page.locator('.setup-device')).toContainText('Connection Unknown');await expect(page.locator('.setup-device')).toContainText('Fixture HOTAS');
  await page.getByText('Saved profile evidence',{exact:true}).click();await expect(page.locator('.setup-device')).toContainText('Fixture HOTAS.diff.lua');
  await expect(page.getByRole('button',{name:'Save selection',exact:true})).toBeDisabled();
  await page.getByRole('button',{name:'Done',exact:true}).click();await expect(page.getByRole('button',{name:'Your setup',exact:true})).toBeFocused();
  expect(events.setupReads).toHaveLength(1);expect(events.selections).toEqual([]);expect(events.commands).toBe(0);
});

test('refresh re-discovers the selected setup and closing then reopening performs a fresh read',async({page})=>{
  const controls=await mock(page);await page.goto('/');await open(page);
  const updated=setupFixture();updated.inventory.devices!.push({id:'new',name:'New saved controller',connection:'Unknown'});controls.change(updated);
  await page.getByRole('button',{name:'Refresh setup',exact:true}).click();await expect(page.locator('.setup-device')).toHaveCount(2);
  expect(controls.events.setupReads.at(-1)).toContain('?refresh=true');
  await page.keyboard.press('Escape');await expect(page.getByRole('dialog',{name:'Your setup',exact:true})).not.toBeVisible();
  await open(page);expect(controls.events.setupReads).toHaveLength(3);expect(controls.events.commands).toBe(0);
});

test('saving sends only discovered IDs and clearly separates pending selection from active control catalogues',async({page})=>{
  const {events}=await mock(page);await page.goto('/');await open(page);
  await expect(page.getByRole('option',{name:'Removed installation · unavailable',exact:true})).toHaveJSProperty('disabled',true);
  await page.getByRole('combobox',{name:'DCS installation',exact:true}).selectOption('install-two');
  await page.getByRole('combobox',{name:'Saved Games profile',exact:true}).selectOption('profile-two');
  await page.getByRole('button',{name:'Save selection',exact:true}).click();
  await expect(page.getByText('Selection saved. Restart the companion to use these locations.',{exact:true})).toBeVisible();
  await expect(page.getByText('Restart needed ·',{exact:false})).toContainText('control catalogues below describe the locations in use now');
  expect(events.selections).toEqual([{install_id:'install-two',profile_id:'profile-two'}]);
  await page.getByText('Locations in use now',{exact:true}).click();await expect(page.locator('.setup-active')).toContainText('E:\\Simulator\\DCS World');
  await expect(page.locator('.setup-choice')).toContainText(['F:\\Steam Library','D:\\Flight Profiles']);
  await expect(page.getByRole('button',{name:'Save selection',exact:true})).toBeDisabled();
  const storage=await page.evaluate(()=>JSON.stringify({...localStorage}));expect(storage).not.toContain('Steam Library');expect(storage).not.toContain('profile-two');expect(events.commands).toBe(0);
});

test('startup overrides lock selection and ambiguous discovery requires attention rather than inventing a default',async({page})=>{
  const control=await mock(page),ambiguous=setupFixture();ambiguous.status='Needs attention';ambiguous.selected={};ambiguous.issues=['Choose which detected DCS installation to use.'];control.change(ambiguous);
  await page.goto('/');await open(page);await expect(page.getByRole('combobox',{name:'DCS installation',exact:true})).toHaveValue('');await expect(page.getByRole('button',{name:'Save selection',exact:true})).toBeDisabled();
  await expect(page.getByRole('region',{name:'Setup needs attention'})).toContainText('Choose which detected DCS installation');
  const locked=setupFixture();locked.selection_locked=true;control.change(locked);await page.getByRole('button',{name:'Refresh setup',exact:true}).click();
  await expect(page.getByRole('combobox',{name:'DCS installation',exact:true})).toBeDisabled();await expect(page.getByRole('combobox',{name:'Saved Games profile',exact:true})).toBeDisabled();
  await expect(page.getByText('Locations were set when the companion started.',{exact:false})).toBeVisible();expect(control.events.selections).toEqual([]);
});

test('read and save errors stay visible and never imply a successful setup change',async({page})=>{
  const control=await mock(page);control.failGet('Discovery could not read the Saved Games folder.');await page.goto('/');await open(page);
  await expect(page.getByRole('alert')).toHaveText('Discovery could not read the Saved Games folder.');await expect(page.getByRole('button',{name:'Save selection',exact:true})).toHaveCount(0);
  control.failGet('');await page.getByRole('button',{name:'Refresh setup',exact:true}).click();await expect(page.getByRole('combobox',{name:'DCS installation',exact:true})).toBeVisible();
  control.failSave('The selected installation is no longer available. Refresh setup.');await page.getByRole('combobox',{name:'DCS installation',exact:true}).selectOption('install-two');await page.getByRole('button',{name:'Save selection',exact:true}).click();
  await expect(page.getByRole('alert')).toHaveText('The selected installation is no longer available. Refresh setup.');await expect(page.getByText('Selection saved.',{exact:false})).toHaveCount(0);expect(control.events.commands).toBe(0);
});

test('paired remote devices can inspect setup but cannot submit location changes',async({page})=>{
  const origin='http://192.0.2.24:4173';
  await page.route(`${origin}/**`,async route=>{const url=new URL(route.request().url());const response=await page.request.get(`http://127.0.0.1:4173${url.pathname}`);await route.fulfill({response});});
  const {events}=await mock(page);await page.goto(origin);await open(page);
  await expect(page.getByRole('combobox',{name:'DCS installation',exact:true})).toBeDisabled();await expect(page.getByRole('button',{name:'Save selection',exact:true})).toBeDisabled();
  await expect(page.getByText('To change these locations, open the companion on your DCS computer.',{exact:false})).toBeVisible();expect(events.selections).toEqual([]);expect(events.commands).toBe(0);
});

for(const viewport of [{width:2560,height:1440},{width:1180,height:820},{width:820,height:1180},{width:390,height:844}])test(`setup remains readable with large text at ${viewport.width}x${viewport.height}`,async({page})=>{
  await page.setViewportSize(viewport);const {events}=await mock(page);await page.addInitScript(()=>localStorage.setItem('dcs-copilot-display-v1',JSON.stringify({theme:'daylight',textSize:'large'})));
  await page.goto('/');await open(page);const modal=page.getByRole('dialog',{name:'Your setup',exact:true});
  const sizes=await modal.evaluate(element=>({client:element.clientWidth,scroll:element.scrollWidth,viewport:innerWidth,page:document.documentElement.scrollWidth}));
  expect(sizes.scroll).toBeLessThanOrEqual(sizes.client+1);expect(sizes.page).toBeLessThanOrEqual(sizes.viewport+1);
  for(const label of ['DCS installation','Saved Games profile']) {const bounds=await page.getByRole('combobox',{name:label,exact:true}).boundingBox();expect(bounds?.height).toBeGreaterThanOrEqual(44);}
  await page.screenshot({path:`test-results/setup/setup-large-${viewport.width}.png`,fullPage:true});
  await page.getByRole('button',{name:'Done',exact:true}).scrollIntoViewIfNeeded();await page.getByRole('button',{name:'Done',exact:true}).click();await expect(modal).not.toBeVisible();expect(events.commands).toBe(0);
});
