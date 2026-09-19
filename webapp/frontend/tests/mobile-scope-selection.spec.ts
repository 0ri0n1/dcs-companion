import { devices, expect, test, type Locator, type Page } from '@playwright/test';
import type { Snapshot } from '../src/types';

// Deliberately synthetic coordinates and aircraft. All API requests are intercepted;
// these touch tests neither read the live simulator nor send cockpit inputs.
function fixture():Snapshot {
  return {
    server:{dry_run:true,lan_enabled:false},
    health:{status:'Live',aircraft:'F-22A',session:'synthetic-touch-session',model_advancing:true,telemetry_fresh:true,telemetry_age_s:.1,dcs_running:true},
    aircraft:{unit_name:'synthetic-player'},
    navigation:{
      terrain:'Caucasus',status:'Available',available_terrains:['Caucasus'],
      ownship:{lat:42,lon:42,heading_true_deg:0,track_true_deg:0,ground_speed_kt:300,altitude_ft:15000},
      airfields:[{id:'synthetic-airport',name:'Synthetic airport',lat:42,lon:42.4,elevation_m:100,desired_course_true_deg:90,cross_track_nm:.4,runways:[],navaids:[],frequencies:[]}],
      navaids:[],route:[],
    },
    awareness:{
      status:'Available',mode:'mission-awareness',single_player:true,model_advancing:true,
      session:'synthetic-touch-session',aircraft:'F-22A',ownship_unit:'synthetic-player',mission_player_unit_id:'1',age_s:.2,
      source:'Synthetic touch-selection fixture',
      contacts:[{id:'2',name:'Synthetic enemy',lat:42.2,lon:42,type:'Synthetic aircraft',category:'air',coalition:1,altitude_ft:12000,speed_kt:400,heading_true_deg:120,track_true_deg:90,age_s:.2}],
      friendlies:[{id:'3',name:'Synthetic friendly',lat:41.8,lon:42,type:'Synthetic aircraft',category:'air',coalition:2,altitude_ft:8000,speed_kt:250,heading_true_deg:0,track_true_deg:350,age_s:.2}],
      total_count:1,friendly_total_count:1,truncated:false,friendly_truncated:false,
    },
    bindings:{status:'Current',devices:[],actions:[]},queue:{state:'Idle'},adapters:[],
  };
}

async function mock(page:Page) {
  const writes:string[]=[];
  await page.addInitScript(state=>{
    (window as any).__touchScopeState=state;
    (window as any).EventSource=class {
      onmessage:any;timer:any;
      constructor(){this.timer=setInterval(()=>this.onmessage?.({data:JSON.stringify((window as any).__touchScopeState)}),250);}
      addEventListener(){}close(){clearInterval(this.timer);}
    };
  },fixture());
  await page.route('**/api/**',async route=>{
    const request=route.request(),pathname=new URL(request.url()).pathname;
    if(pathname==='/api/session')return route.fulfill({json:{ok:true}});
    if(request.method()!=='GET'){
      writes.push(`${request.method()} ${pathname}`);
      return route.fulfill({status:405,json:{detail:'Synthetic scope test forbids writes'}});
    }
    if(pathname==='/api/state')return route.fulfill({json:await page.evaluate(()=>(window as any).__touchScopeState)});
    return route.fulfill({json:{}});
  });
  return writes;
}

const strip=(page:Page)=>page.getByRole('region',{name:'Selected scope item',exact:true});
const marker=(page:Page,side:'enemy'|'friendly')=>page.getByRole('button',{name:`Select mission ${side} Synthetic ${side}`,exact:true});
const touchHit=(page:Page,side:'enemy'|'friendly')=>page.locator(`.contact-touch-hit[data-contact-id="${side==='enemy'?'2':'3'}"]`);
const row=(page:Page,side:'enemy'|'friendly')=>page.getByRole('button',{name:`Inspect mission ${side} Synthetic ${side}`,exact:true});

async function metric(page:Page,label:string,value:string|RegExp) {
  const item=strip(page).locator('.metric').filter({has:page.getByText(label,{exact:true})});
  await expect(item).toHaveCount(1);await expect(item.locator('div')).toHaveText(value);
}

async function commonStrip(page:Page,kind:'contact'|'navigation') {
  await expect(page.locator('.navigation-solution')).toHaveCount(1);
  await expect(strip(page)).toHaveAttribute('data-selection-kind',kind);
  await expect(strip(page).locator('.solution-title>.eyebrow')).toHaveText('DIRECT TO · DISPLAY ONLY');
  await expect(strip(page).locator('.metric')).toHaveCount(5);
  await expect(page.locator('.contact-details-card,.enemy-detail')).toHaveCount(0);
  await expect(page.locator('.mission-awareness')).toBeVisible();
  await expect(page.locator('.mission-awareness .enemy-row')).toHaveCount(2);
}

async function contactDetails(page:Page,side:'enemy'|'friendly') {
  await commonStrip(page,'contact');
  await expect(strip(page).locator('.solution-title>strong')).toHaveText(`Synthetic ${side}`);
  await metric(page,'BEARING · TRUE',side==='enemy'?'000°':'180°');
  await metric(page,'DISTANCE',/^12\.0\s*NM$/);
  await metric(page,'HEADING · TRUE',side==='enemy'?'120°':'000°');
  await metric(page,'GROUND SPEED',side==='enemy'?/^400\s*kt$/:/^250\s*kt$/);
  await metric(page,'ALTITUDE · MSL',side==='enemy'?/^12,000\s*ft$/:/^8,000\s*ft$/);
  await expect(strip(page).locator('.solution-title')).toContainText(side==='enemy'?'Track 090° true':'Track 350° true');
  await expect(page.locator('.direct-line')).toHaveCount(0);
}

async function airportDetails(page:Page) {
  await commonStrip(page,'navigation');
  await expect(strip(page).locator('.solution-title>strong')).toHaveText('Synthetic airport');
  await metric(page,'BEARING · TRUE','090°');
  await metric(page,'DISTANCE',/^17\.8\s*NM$/);
  await metric(page,'DESIRED COURSE',/^090°\s*TRUE$/);
  await metric(page,'ETA · GROUND SPEED',/^3\.6\s*min$/);
  await metric(page,'CROSS-TRACK',/^0\.40\s*NM$/);
  await expect(page.locator('.direct-line')).toHaveCount(1);
}

async function readoutAboveDock(page:Page) {
  // Do not scroll here: selecting the item must reveal the existing strip itself.
  await expect.poll(()=>strip(page).evaluate(element=>{
    const readout=element.getBoundingClientRect();
    const dock=document.querySelector('.mobile-view-nav')!.getBoundingClientRect();
    const viewport=window.visualViewport;
    const top=viewport?.offsetTop??0,bottom=top+(viewport?.height??innerHeight);
    return readout.top>=top-1&&readout.bottom<=Math.min(bottom,dock.top)+1;
  })).toBe(true);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
}

async function showTarget(target:Locator) {
  // Bring only the item to be tapped into view, never the resulting readout.
  await target.evaluate(element=>element.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'}));
}

async function tapMarkerCenter(page:Page,side:'enemy'|'friendly') {
  const target=marker(page,side).locator('.enemy-hit');
  await showTarget(target);
  const box=(await target.boundingBox())!;
  await page.touchscreen.tap(box.x+box.width/2,box.y+box.height/2);
}

const phones=[
  {name:'iPhone 13',device:devices['iPhone 13'],viewport:{width:390,height:844}},
  {name:'short iPhone viewport',device:devices['iPhone 13'],viewport:{width:390,height:664}},
  {name:'larger iPhone viewport',device:devices['iPhone 13 Pro Max'],viewport:{width:428,height:926}},
];

for(const phone of phones){
  test.describe(phone.name,()=>{
    const {defaultBrowserType:_,...deviceContext}=phone.device;
    test.use({...deviceContext,viewport:phone.viewport,isMobile:true,hasTouch:true});

    test('map and Mission Awareness taps reveal the same DIRECT TO readout',async({page})=>{
      const writes=await mock(page);await page.goto('/');
      expect(await page.evaluate(()=>navigator.maxTouchPoints)).toBeGreaterThan(0);
      const airport=page.getByRole('button',{name:'Select Synthetic airport',exact:true}).locator('.hit-target');
      await showTarget(airport);await airport.tap();
      await airportDetails(page);await readoutAboveDock(page);
      for(const side of ['enemy','friendly'] as const){
        await tapMarkerCenter(page,side);await contactDetails(page,side);await readoutAboveDock(page);
      }
      for(const side of ['enemy','friendly'] as const){
        await showTarget(row(page,side));await row(page,side).tap();
        await contactDetails(page,side);await readoutAboveDock(page);
      }
      await showTarget(airport);await airport.tap();
      await airportDetails(page);await readoutAboveDock(page);
      await expect(page.locator('.enemy-symbol.selected,.enemy-row.selected')).toHaveCount(0);
      expect(writes).toEqual([]);
    });

    test('44px aircraft hit circles work at the edge and resolve nearby centers',async({page})=>{
      const writes=await mock(page);await page.goto('/');
      for(const side of ['enemy','friendly'] as const){
        await expect.poll(async()=>Math.min((await touchHit(page,side).boundingBox())!.width,(await touchHit(page,side).boundingBox())!.height)).toBeGreaterThanOrEqual(43.5);
      }
      const enemyHit=touchHit(page,'enemy');
      await showTarget(enemyHit);
      let box=(await enemyHit.boundingBox())!;
      await page.touchscreen.tap(box.x+box.width/2-20,box.y+box.height/2);
      await contactDetails(page,'enemy');await readoutAboveDock(page);

      // A narrower scope must recompute the SVG-space radius and keep 44 CSS px.
      await page.setViewportSize({width:375,height:phone.viewport.height});
      await expect.poll(async()=>(await enemyHit.boundingBox())!.width).toBeGreaterThanOrEqual(43.5);
      await showTarget(enemyHit);box=(await enemyHit.boundingBox())!;
      await page.touchscreen.tap(box.x+box.width/2-20,box.y+box.height/2);
      await contactDetails(page,'enemy');await readoutAboveDock(page);

      // Both centers are intentionally inside both circles. The later-painted
      // friendly and the enemy label must not steal a tap on the enemy's center.
      await page.evaluate(()=>{
        const awareness=(window as any).__touchScopeState.awareness;
        Object.assign(awareness.contacts[0],{lat:42.15,lon:42});
        Object.assign(awareness.friendlies[0],{lat:42.15,lon:42.055});
      });
      await expect.poll(async()=>{
        const enemy=(await enemyHit.boundingBox())!,friendly=(await touchHit(page,'friendly').boundingBox())!;
        const distance=Math.hypot(enemy.x+enemy.width/2-friendly.x-friendly.width/2,enemy.y+enemy.height/2-friendly.y-friendly.height/2);
        return distance>2&&distance<Math.min(enemy.width,friendly.width)/2;
      }).toBe(true);
      for(const side of ['enemy','friendly','enemy'] as const){
        await tapMarkerCenter(page,side);
        await commonStrip(page,'contact');
        await expect(strip(page).locator('.solution-title>strong')).toHaveText(`Synthetic ${side}`);
        await expect(marker(page,side)).toHaveClass(/selected/);await readoutAboveDock(page);
      }
      expect(writes).toEqual([]);
    });
  });
}

test.describe('desktop shared readout',()=>{
  test.use({viewport:{width:1440,height:1000},isMobile:false,hasTouch:false});
  test('airport and both contact selections retain one strip below the scope',async({page})=>{
    const writes=await mock(page);await page.goto('/');
    await page.getByRole('button',{name:'Select Synthetic airport',exact:true}).locator('.hit-target').click();
    await airportDetails(page);
    await marker(page,'enemy').locator('.enemy-hit').click();await contactDetails(page,'enemy');
    await row(page,'friendly').click();await contactDetails(page,'friendly');
    const [scopeBox,readoutBox]=await Promise.all([page.locator('.scope-panel').boundingBox(),strip(page).boundingBox()]);
    expect(readoutBox!.y).toBeGreaterThanOrEqual(scopeBox!.y+scopeBox!.height-1);
    expect(Math.abs(readoutBox!.x-scopeBox!.x)).toBeLessThanOrEqual(1);
    expect(writes).toEqual([]);
  });
});
