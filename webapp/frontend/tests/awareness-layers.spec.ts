import { expect, test, type Page } from '@playwright/test';
import { awarenessView } from '../src/MissionAwareness';
import type { Snapshot } from '../src/types';

// Synthetic positions are test fixtures only. No simulator or command endpoint is used.
function fixture():Snapshot {
  const contact=(id:string,name:string,lat:number,coalition:number)=>({id,name,lat,lon:42,type:'Fixture aircraft',category:'air',coalition,altitude_ft:12000,speed_kt:400,age_s:.2});
  return {
    server:{dry_run:true,lan_enabled:false},
    health:{status:'Live',aircraft:'F-22A',session:'layer-session',model_advancing:true,telemetry_fresh:true,telemetry_age_s:.1,dcs_running:true},
    aircraft:{unit_name:'exported-player'},
    navigation:{terrain:'Caucasus',status:'Available',available_terrains:['Caucasus'],ownship:{lat:42,lon:42,heading_true_deg:0,track_true_deg:0,ground_speed_kt:300,altitude_ft:15000},airfields:[],navaids:[],route:[]},
    awareness:{status:'Available',mode:'mission-awareness',single_player:true,model_advancing:true,session:'layer-session',aircraft:'F-22A',ownship_unit:'exported-player',mission_player_unit_id:'1',age_s:.2,source:'Synthetic fixed-query fixture',contacts:[contact('2','Enemy fixture',42.15,1)],friendlies:[contact('3','Friendly fixture',42.3,2)],total_count:1,friendly_total_count:1,truncated:false,friendly_truncated:false},
    bindings:{status:'Current',devices:[],actions:[]},queue:{state:'Idle'},adapters:[],
  };
}

async function mock(page:Page,state=fixture()) {
  await page.addInitScript(f=>{
    (window as any).__layerState=f;
    (window as any).EventSource=class {onmessage:any;timer:any;constructor(){this.timer=setInterval(()=>this.onmessage?.({data:JSON.stringify((window as any).__layerState)}),250);}addEventListener(){}close(){clearInterval(this.timer);}};
  },state);
  await page.route('**/api/session',route=>route.fulfill({json:{ok:true}}));
  await page.route('**/api/state',async route=>route.fulfill({json:await page.evaluate(()=>(window as any).__layerState)}));
}

for(const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
  test(`matching scope layer checkboxes and independent selection work at ${viewport.width}px`,async({page})=>{
    await page.setViewportSize(viewport);await mock(page);let sends=0;
    await page.route('**/api/commands**',route=>{sends++;return route.fulfill({json:{}});});
    await page.goto('/');
    const friendlies=page.getByRole('button',{name:'Mission friendlies',exact:true}),enemies=page.getByRole('button',{name:'Mission enemies',exact:true});
    await expect(friendlies).toBeVisible();await expect(enemies).toBeVisible();
    await expect(friendlies).toHaveAttribute('aria-pressed','true');await expect(enemies).toHaveAttribute('aria-pressed','true');
    await expect(page.locator('.declutter').getByRole('button',{name:'Mission friendlies',exact:true})).toBeVisible();
    await expect(page.locator('.declutter').getByRole('button',{name:'Mission enemies',exact:true})).toBeVisible();
    await expect(friendlies.locator('.toggle-dot.on')).toHaveCount(1);await expect(enemies.locator('.toggle-dot.on')).toHaveCount(1);
    await expect(page.locator('.friendly-symbol')).toHaveCount(1);await expect(page.locator('.enemy-symbol:not(.friendly-symbol)')).toHaveCount(1);
    const layout=await page.evaluate(()=>{
      const controls=[...document.querySelectorAll('.declutter button')];
      return {matching:controls.length===7&&controls.every(e=>e.querySelector('.toggle-dot')),overflow:document.documentElement.scrollWidth>innerWidth+1,heights:controls.map(e=>e.getBoundingClientRect().height)};
    });
    expect(layout.matching).toBe(true);expect(layout.overflow).toBe(false);expect(layout.heights.every(height=>height>=44)).toBe(true);

    const selection=page.locator('.navigation-solution');
    await page.getByRole('button',{name:'Inspect mission friendly Friendly fixture',exact:true}).click();
    await expect(selection).toHaveAttribute('data-selection-kind','contact');await expect(selection).toContainText('Friendly fixture');
    await enemies.click();await expect(enemies).toHaveAttribute('aria-pressed','false');
    await expect(selection).toHaveAttribute('data-selection-kind','contact');await expect(selection).toContainText('Friendly fixture');
    await expect(page.locator('.enemy-symbol:not(.friendly-symbol)')).toHaveCount(0);await expect(page.locator('.friendly-symbol')).toHaveCount(1);
    await friendlies.click();await expect(selection).toHaveAttribute('data-selection-kind','none');await expect(selection).not.toContainText('Friendly fixture');await expect(page.locator('.enemy-symbol')).toHaveCount(0);
    await friendlies.focus();await page.keyboard.press('Space');await expect(friendlies).toHaveAttribute('aria-pressed','true');
    await expect(enemies).toHaveAttribute('aria-pressed','false');await expect(page.locator('.friendly-symbol')).toHaveCount(1);await expect(selection).toHaveAttribute('data-selection-kind','none');

    await page.reload();await expect(friendlies).toHaveAttribute('aria-pressed','true');await expect(enemies).toHaveAttribute('aria-pressed','false');
    const stored=await page.evaluate(()=>localStorage.getItem('dcs-copilot-display-v1')!);
    expect(JSON.parse(stored)).toMatchObject({friendlies:true,enemies:false});expect(stored).not.toContain('fixture');expect(stored).not.toContain('layer-session');expect(sends).toBe(0);
    await enemies.click();await page.getByRole('button',{name:'Inspect mission enemy Enemy fixture',exact:true}).click();await enemies.click();
    await expect(selection).toHaveAttribute('data-selection-kind','none');await expect(selection).not.toContainText('Enemy fixture');await expect(page.locator('.friendly-symbol')).toHaveCount(1);
  });
}

test('mission units on both sides disappear together on stale, multiplayer and changed session',async({page})=>{
  await page.setViewportSize({width:390,height:844});await mock(page);await page.goto('/');
  await expect(page.locator('.enemy-symbol')).toHaveCount(2);
  for(const failure of ['stale','multiplayer','session']) {
    await page.getByRole('button',{name:'Inspect mission friendly Friendly fixture',exact:true}).click();
    await expect(page.locator('.navigation-solution')).toHaveAttribute('data-selection-kind','contact');
    await page.evaluate(mode=>{const s=(window as any).__layerState;s.awareness.age_s=mode==='stale'?6:.2;s.awareness.single_player=mode!=='multiplayer';s.awareness.session=mode==='session'?'old':'layer-session';},failure);
    await expect(page.locator('.enemy-symbol')).toHaveCount(0);await expect(page.locator('.enemy-row')).toHaveCount(0);
    await expect(page.locator('.navigation-solution')).toHaveAttribute('data-selection-kind','none');await expect(page.locator('.navigation-solution')).not.toContainText('Friendly fixture');
    await expect(page.getByRole('button',{name:'Mission friendlies',exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'Mission enemies',exact:true})).toBeVisible();
    await page.evaluate(()=>{Object.assign((window as any).__layerState.awareness,{age_s:.2,single_player:true,session:'layer-session'});});
    await expect(page.locator('.enemy-symbol')).toHaveCount(2);
    await expect(page.locator('.navigation-solution')).toHaveAttribute('data-selection-kind','none');
  }
});

test('both layer readers reject ownship, duplicate identities and expired positions',()=>{
  const state=fixture(),awareness=state.awareness!;
  const row=(awareness.friendlies as any[])[0];
  awareness.friendlies=[row,{...row,id:'1',name:'Ownship must not render'},{...row,id:'2',name:'Cross-side duplicate'},{...row,id:'4',age_s:6}];
  const view=awarenessView(state,true,true,[42,42]);
  expect(view.contacts.map(row=>row.id)).toEqual(['2']);expect(view.friendlies.map(row=>row.id)).toEqual(['3']);
  expect(view.friendlies[0].affiliation).toBe('friendly');expect(view.contacts[0].affiliation).toBe('enemy');
  expect(awarenessView(state,true,true,[42,42],5).friendlies).toEqual([]);
});

test('both coalitions point along true flight direction in north-up and track-up, with upright labels',async({page})=>{
  const state=fixture();
  Object.assign(state.navigation!.ownship as object,{track_true_deg:90});
  Object.assign((state.awareness!.contacts as any[])[0],{track_true_deg:90,heading_true_deg:120});
  Object.assign((state.awareness!.friendlies as any[])[0],{track_true_deg:0,heading_true_deg:30});
  await mock(page,state);await page.goto('/');
  const enemy=page.getByRole('button',{name:'Select mission enemy Enemy fixture',exact:true});
  const friendly=page.getByRole('button',{name:'Select mission friendly Friendly fixture',exact:true});
  // Transform the north-pointing glyph vector through the actual SVG matrix.
  // This checks displayed direction, including nested scope transforms.
  const vector=async(symbol:typeof enemy)=>symbol.locator('.contact-direction').evaluate(element=>{
    const matrix=(element as SVGGraphicsElement).getScreenCTM()!;
    const length=Math.hypot(matrix.c,matrix.d);
    return [Number((-matrix.c/length).toFixed(4)),Number((-matrix.d/length).toFixed(4))].map(value=>value||0);
  });
  await page.getByRole('button',{name:'North up',exact:true}).click();
  await expect.poll(()=>vector(enemy)).toEqual([1,0]);
  await expect.poll(()=>vector(friendly)).toEqual([0,-1]);
  await page.getByRole('button',{name:'Track up',exact:true}).click();
  await expect.poll(()=>vector(enemy)).toEqual([0,-1]);
  await expect.poll(()=>vector(friendly)).toEqual([-1,0]);
  for(const marker of [enemy,friendly]){
    const tilt=await marker.locator('text').first().evaluate(element=>{
      const matrix=(element as SVGGraphicsElement).getScreenCTM()!;return [matrix.b,matrix.c];
    });
    expect(tilt).toEqual([0,0]);
  }
  await page.evaluate(()=>{
    const awareness=(window as any).__layerState.awareness;
    awareness.contacts[0].track_true_deg=270;awareness.friendlies[0].track_true_deg=180;
  });
  await expect.poll(()=>vector(enemy)).toEqual([0,1]);
  await expect.poll(()=>vector(friendly)).toEqual([1,0]);
  await page.evaluate(()=>{
    const row=(window as any).__layerState.awareness.contacts[0];
    delete row.track_true_deg;row.heading_true_deg=0;
  });
  await expect(enemy.locator('.contact-direction')).toHaveAttribute('data-direction-source','heading');
  await expect.poll(()=>vector(enemy)).toEqual([-1,0]);
  await page.evaluate(()=>{
    const row=(window as any).__layerState.awareness.contacts[0];
    row.track_true_deg=360;row.heading_true_deg=-1;
  });
  await expect(enemy.locator('.contact-direction')).toHaveAttribute('data-direction-source','unknown');
  await expect(enemy.locator('.contact-direction')).not.toHaveAttribute('transform',/rotate/);
});

for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
  test(`contact and navigation selections share the strip below the scope at ${viewport.width}px`,async({page})=>{
    const state=fixture();
    state.navigation!.airfields=[{id:'test-airport',name:'Fixture airport',lat:42.1,lon:42.1,elevation_m:100,runways:[],navaids:[],frequencies:[]}];
    state.navigation!.navaids=[{id:'test-aid',name:'Fixture navaid',type:'TACAN',lat:41.9,lon:42.25,channel:44,mode:'X'}];
    state.navigation!.route=[{id:'test-waypoint',name:'Fixture waypoint',lat:42.05,lon:42.05}];
    Object.assign((state.awareness!.contacts as any[])[0],{heading_true_deg:0,track_true_deg:90});
    Object.assign((state.awareness!.friendlies as any[])[0],{altitude_ft:0,speed_kt:0});
    await page.setViewportSize(viewport);await mock(page,state);await page.goto('/');
    const selection=page.getByRole('region',{name:'Selected scope item',exact:true});
    const airport=page.getByRole('button',{name:'Select Fixture airport',exact:true}).locator('.hit-target');
    await expect(page.locator('.navigation-solution')).toHaveCount(1);await expect(selection).toHaveAttribute('data-selection-kind','none');
    await airport.click();
    await expect(selection).toHaveAttribute('data-selection-kind','navigation');await expect(selection).toContainText('Fixture airport');
    await expect(page.locator('.field-card h2')).toHaveText('Fixture airport');await expect(page.locator('.direct-line')).toHaveCount(1);
    await page.getByRole('button',{name:'Select mission enemy Enemy fixture',exact:true}).locator('.enemy-hit').click();
    await expect(selection).toHaveAttribute('data-selection-kind','contact');await expect(selection).toContainText('Enemy fixture');
    await expect(selection).not.toContainText('Fixture airport');await expect(page.locator('.direct-line')).toHaveCount(0);
    await expect(page.locator('.contact-details-card,.enemy-detail')).toHaveCount(0);
    await expect(selection).toContainText(/12,000\s*ft/);await expect(selection).toContainText(/400\s*kt/);await expect(selection).toContainText(/9\.0\s*NM/);
    await expect(selection.locator('.metric').filter({hasText:'HEADING'})).toContainText('000°');await expect(selection).toContainText('090°');
    const [scopeBox,selectionBox]=await Promise.all([page.locator('.scope-panel').boundingBox(),selection.boundingBox()]);
    expect(selectionBox!.y).toBeGreaterThanOrEqual(scopeBox!.y+scopeBox!.height-1);
    expect(Math.abs(selectionBox!.x-scopeBox!.x)).toBeLessThanOrEqual(1);expect(Math.abs(selectionBox!.width-scopeBox!.width)).toBeLessThanOrEqual(1);
    await page.screenshot({path:`test-results/scope-details-${viewport.width}.png`,fullPage:true});
    await page.getByRole('button',{name:'Inspect mission friendly Friendly fixture',exact:true}).click();
    await expect(selection).toContainText('Friendly fixture');
    await expect(selection.locator('.metric').filter({hasText:'ALTITUDE · MSL'}).locator('div')).toHaveText(/^0\s*ft$/);
    await expect(selection.locator('.metric').filter({hasText:'GROUND SPEED'}).locator('div')).toHaveText(/^0\s*kt$/);
    await expect(selection).toContainText(/18\.0\s*NM/);
    // Range follows current ownship, and changing selections must not retain contact metrics.
    await page.evaluate(()=>{(window as any).__layerState.navigation.ownship.lat=42.15;});
    await expect(selection).toContainText(/9\.0\s*NM/);
    const navigationSelections=[
      {name:'Fixture airport',button:airport},
      {name:'Fixture navaid',button:page.getByRole('button',{name:'Select navaid Fixture navaid',exact:true}).locator('circle')},
      {name:'Fixture waypoint',button:page.locator('.route-strip').getByRole('button',{name:'01 · Fixture waypoint',exact:true})},
    ];
    for(const navigationSelection of navigationSelections){
      await navigationSelection.button.click();
      await expect(selection).toHaveAttribute('data-selection-kind','navigation');await expect(selection).toContainText(navigationSelection.name);
      await expect(selection).not.toContainText('Friendly fixture');await expect(selection).not.toContainText('Enemy fixture');
      await expect(page.locator('.enemy-symbol.selected,.enemy-row.selected')).toHaveCount(0);await expect(page.locator('.direct-line')).toHaveCount(1);
      await page.getByRole('button',{name:'Inspect mission enemy Enemy fixture',exact:true}).click();
      await expect(selection).toHaveAttribute('data-selection-kind','contact');await expect(selection).toContainText('Enemy fixture');
      await expect(selection).not.toContainText(navigationSelection.name);await expect(page.locator('.direct-line')).toHaveCount(0);
    }
    // Expiring a contact clears the common strip; the old waypoint must not return.
    await page.evaluate(()=>{(window as any).__layerState.awareness.age_s=6;});
    await expect(selection).toHaveAttribute('data-selection-kind','none');await expect(selection).not.toContainText('Enemy fixture');
    await expect(selection).not.toContainText('Fixture waypoint');await expect(selection).not.toContainText(/12,000\s*ft/);await expect(page.locator('.direct-line')).toHaveCount(0);
    await page.evaluate(()=>{(window as any).__layerState.awareness.age_s=.2;});
    await expect(page.locator('.enemy-symbol')).toHaveCount(2);await expect(selection).toHaveAttribute('data-selection-kind','none');
    await airport.click();await page.getByRole('button',{name:'Inspect mission friendly Friendly fixture',exact:true}).click();
    await page.getByRole('button',{name:'Mission friendlies',exact:true}).click();
    await expect(selection).toHaveAttribute('data-selection-kind','none');await expect(selection).not.toContainText('Fixture airport');
    await expect(page.locator('.direct-line')).toHaveCount(0);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  });
}
