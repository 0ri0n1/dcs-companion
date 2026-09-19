import {test,expect,type Locator} from '@playwright/test';
import {mock,status} from './display-fixture';

const hornetOrder=['left-ddi','ampcd','right-ddi'];
const sideNumbers={left:[5,4,3,2,1],top:[6,7,8,9,10],right:[11,12,13,14,15],bottom:[20,19,18,17,16]};
const phoneViewports=[{width:375,height:812},{width:390,height:844},{width:430,height:932},{width:844,height:390}];
// Independent standard-option anchors, calibrated from the real 512px baked
// exports in webapp/verification/display-stream-{left_mdi,right_mdi,ampcd}.jpg
// and identified using installed FA-18C MPD_PB_defs.lua. The raw images include
// the native display border; logical ±512DI coordinates alone are insufficient.
// Calibration uncertainty is about one raw pixel. Page-specific displaced text
// (such as radar labels) does not move the standard physical button anchors.
// Intentionally independent of the application's presentation model.
const imageX=[97.5,177.25,257,336.75,416.5].map(value=>value/512);
const ddiImageY=[112.5,190.5,268.5,346.5,424.5].map(value=>value/512);
const ampcdImageY=[107.5,187.875,268.25,348.625,429].map(value=>value/512);

async function checkBezel(bezel:Locator){
  await expect(bezel).toBeVisible();
  const viewportWidth=bezel.page().viewportSize()?.width;
  const minimumAlong=viewportWidth===375||viewportWidth===1180?40:44;
  const image=bezel.locator('.display-feed-image img');
  await expect(image).toBeVisible();
  const imageBounds=(await image.boundingBox())!;
  expect(Math.abs(imageBounds.width-imageBounds.height),'captured display retains its square aspect').toBeLessThanOrEqual(1);
  const imageY=(await bezel.getAttribute('data-bezel-panel'))==='ampcd'?ampcdImageY:ddiImageY;
  for(const [side,numbers] of Object.entries(sideNumbers)){
    const rail=bezel.locator(`.bezel-${side}`),controls=rail.locator('[data-button-number]');
    expect(await controls.evaluateAll(items=>items.map(item=>Number(item.getAttribute('data-button-number'))))).toEqual(numbers);
    const railBounds=(await rail.boundingBox())!,buttonBounds=await Promise.all((await controls.all()).map(button=>button.boundingBox()));
    for(const [index,bounds] of buttonBounds.entries()){
      expect(bounds!.x,`${side} button ${numbers[index]} fits the rail`).toBeGreaterThanOrEqual(railBounds.x-1);
      expect(bounds!.y,`${side} button ${numbers[index]} fits the rail`).toBeGreaterThanOrEqual(railBounds.y-1);
      expect(bounds!.x+bounds!.width,`${side} button ${numbers[index]} fits the rail`).toBeLessThanOrEqual(railBounds.x+railBounds.width+1);
      expect(bounds!.y+bounds!.height,`${side} button ${numbers[index]} fits the rail`).toBeLessThanOrEqual(railBounds.y+railBounds.height+1);
      const vertical=side==='left'||side==='right';
      expect(vertical?bounds!.height:bounds!.width,`${side} button ${numbers[index]} along-rail touch size`).toBeGreaterThanOrEqual(minimumAlong);
      expect(vertical?bounds!.width:bounds!.height,`${side} button ${numbers[index]} across-rail touch size`).toBeGreaterThanOrEqual(44);
      const actualCentre=vertical?bounds!.y+bounds!.height/2:bounds!.x+bounds!.width/2;
      const expectedCentre=vertical?imageBounds.y+imageBounds.height*imageY[index]:imageBounds.x+imageBounds.width*imageX[index];
      expect(Math.abs(actualCentre-expectedCentre),`${side} button ${numbers[index]} aligns to its actual image option`).toBeLessThanOrEqual(1.25);
      if(index){
        const prior=buttonBounds[index-1]!;
        const gap=vertical?bounds!.y-prior.y-prior.height:bounds!.x-prior.x-prior.width;
        if(gap<.25){
          const diagnostic={viewport:bezel.page().viewportSize(),panel:await bezel.getAttribute('data-bezel-panel'),side,button:numbers[index],priorButton:numbers[index-1],gap,imageBounds,rail:await rail.evaluate(node=>{
            const style=getComputedStyle(node),bounds=node.getBoundingClientRect();
            return {bounds:{x:bounds.x,y:bounds.y,width:bounds.width,height:bounds.height},height:style.height,width:style.width,pitch:style.getPropertyValue('--bezel-pitch'),keys:[...node.querySelectorAll('button')].map(button=>{const css=getComputedStyle(button),box=button.getBoundingClientRect();return {number:button.dataset.buttonNumber,height:css.height,minHeight:css.minHeight,width:css.width,minWidth:css.minWidth,top:css.top,left:css.left,pitch:css.getPropertyValue('--bezel-pitch'),position:css.getPropertyValue('--bezel-position'),bounds:{x:box.x,y:box.y,width:box.width,height:box.height}};})};
          })};
          console.log('BEZEL_OVERLAP_DIAGNOSTIC',JSON.stringify(diagnostic));
          await test.info().attach('bezel-overlap-diagnostic',{body:JSON.stringify(diagnostic,null,2),contentType:'application/json'});
        }
        if(side==='left'||side==='right')expect(bounds!.y).toBeGreaterThanOrEqual(prior.y+prior.height+.25);
        else expect(bounds!.x).toBeGreaterThanOrEqual(prior.x+prior.width+.25);
      }
    }
  }
  const targets=await Promise.all((await bezel.locator('[data-button-number]').all()).map(button=>button.boundingBox()));
  for(let i=0;i<targets.length;i++)for(let j=i+1;j<targets.length;j++){
    const a=targets[i]!,b=targets[j]!;
    const overlapsX=Math.min(a.x+a.width,b.x+b.width)-Math.max(a.x,b.x)>.5;
    const overlapsY=Math.min(a.y+a.height,b.y+b.height)-Math.max(a.y,b.y)>.5;
    expect(overlapsX&&overlapsY,`touch targets ${i+1} and ${j+1} do not overlap`).toBe(false);
  }
  const centre=(await bezel.locator('.bezel-screen').boundingBox())!;
  expect(Math.abs(centre.width-centre.height)).toBeLessThanOrEqual(2);
  for(const side of ['left','right'] as const){
    const bounds=(await bezel.locator(`.bezel-${side}`).boundingBox())!;
    expect(bounds.y).toBeGreaterThanOrEqual(centre.y-2);
    expect(bounds.y+bounds.height).toBeLessThanOrEqual(centre.y+centre.height+2);
    if(side==='left')expect(bounds.x+bounds.width).toBeLessThanOrEqual(centre.x+1);
    else expect(bounds.x).toBeGreaterThanOrEqual(centre.x+centre.width-1);
  }
  for(const side of ['top','bottom'] as const){
    const bounds=(await bezel.locator(`.bezel-${side}`).boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(centre.x-2);
    expect(bounds.x+bounds.width).toBeLessThanOrEqual(centre.x+centre.width+2);
    if(side==='top')expect(bounds.y+bounds.height).toBeLessThanOrEqual(centre.y+1);
    else expect(bounds.y).toBeGreaterThanOrEqual(centre.y+centre.height-1);
  }
  // Source switches, freshness captions and status text belong outside the screen.
  await expect(bezel.locator('.display-feed-toolbar')).toHaveCount(0);
  await expect(bezel.locator('.display-feed-caption')).toHaveCount(0);
}

for(const viewport of [{width:2560,height:1440},{width:1440,height:1000},{width:1180,height:820}])test(`desktop Hornet displays follow cockpit order and perimeter geometry at ${viewport.width}px`,async({page})=>{
  await page.setViewportSize(viewport);
  const control=await mock(page,status(),true),cards=page.locator('.display-overview-grid .display-overview-card');
  await expect(cards).toHaveCount(3);
  expect(await cards.evaluateAll(items=>items.map(item=>item.getAttribute('data-display-id')))).toEqual(hornetOrder);
  await expect(cards.locator('img')).toHaveCount(3);
  const bounds=await Promise.all((await cards.all()).map(card=>card.boundingBox()));
  expect(Math.max(...bounds.map(box=>box!.y))-Math.min(...bounds.map(box=>box!.y))).toBeLessThanOrEqual(2);
  expect(bounds[0]!.x+bounds[0]!.width).toBeLessThan(bounds[1]!.x);
  expect(bounds[1]!.x+bounds[1]!.width).toBeLessThan(bounds[2]!.x);
  for(const card of await cards.all())await checkBezel(card.locator('.cockpit-display-bezel'));
  for(const button of await cards.locator('.remote-control').all())await expect(button).toBeDisabled();
  expect(control.writes).toEqual([]);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.locator('.display-overview').screenshot({path:`test-results/cockpit-layout/hornet-${viewport.width}.png`});
});

for(const viewport of phoneViewports)test(`phone overview includes three aligned cockpit bezels and touch targets at ${viewport.width}x${viewport.height}`,async({page})=>{
  await page.setViewportSize(viewport);
  const control=await mock(page,status(),true),cards=page.locator('.display-overview-grid .display-overview-card');
  await expect(cards).toHaveCount(3);
  expect(await cards.evaluateAll(items=>items.map(item=>item.getAttribute('data-display-id')))).toEqual(hornetOrder);
  await expect(cards.locator('img')).toHaveCount(3);
  await expect(cards.locator('.cockpit-display-bezel')).toHaveCount(3);
  await expect(cards.locator('.remote-control')).toHaveCount(60);
  for(const card of await cards.all())await checkBezel(card.locator('.cockpit-display-bezel'));
  for(const button of await cards.locator('.remote-control').all())await expect(button).toBeDisabled();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.screenshot({path:`test-results/cockpit-layout/hornet-${viewport.width}.png`,fullPage:true});
  await page.getByLabel('Remote panel style',{exact:true}).selectOption('cockpit');
  await page.getByRole('button',{name:'Open Left DDI controls',exact:true}).click();
  await checkBezel(page.locator('.remote-panel-slot .cockpit-display-bezel'));
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  expect(control.writes).toEqual([]);
});

for(const viewport of [{width:390,height:844},{width:1440,height:1000}])test(`fullscreen preserves DDI and AMPCD image option alignment at ${viewport.width}px`,async({page})=>{
  await page.setViewportSize(viewport);
  const control=await mock(page,status(),true);
  for(const label of ['Left DDI','AMPCD']){
    const display=page.getByRole('region',{name:`${label} display view`});
    await display.getByRole('button',{name:`Fullscreen ${label}`,exact:true}).click();
    await expect.poll(()=>page.evaluate(()=>Boolean(document.fullscreenElement))).toBe(true);
    await checkBezel(display.locator('.cockpit-display-bezel'));
    expect(await display.evaluate(node=>node.scrollWidth<=node.clientWidth+1)).toBe(true);
    await display.screenshot({path:`test-results/cockpit-layout/fullscreen-${label==='AMPCD'?'ampcd':'left-ddi'}-${viewport.width}.png`});
    await display.getByRole('button',{name:`Exit fullscreen ${label}`,exact:true}).click();
    await expect.poll(()=>page.evaluate(()=>document.fullscreenElement===null)).toBe(true);
  }
  expect(control.writes).toEqual([]);
});

for(const viewport of [{width:375,height:812},{width:1440,height:1000}])test(`overview bezel remains gated and an explicit tap sends its registered physical button ID once at ${viewport.width}px`,async({page})=>{
  await page.setViewportSize(viewport);
  const control=await mock(page,status(),true);
  const remote={status:'Ready',mode:'preview',armed:false,controller_connected:false,can_edit:true,busy:false,aircraft:'FA-18C_hornet',panels:['left_mdi','right_mdi','ampcd'].map(id=>({id,label:id,buttons:Array.from({length:20},(_,i)=>({id:`hornet.${id}.pb.${i+1}`,name:`${id} PB ${i+1}`,label:String(i+1),available:true}))})),workflows:[],scripts:[]};
  await page.route('**/api/remote**',route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(path.endsWith('/heartbeat'))remote.controller_connected=true;
    if(path.endsWith('/tap'))return route.fulfill({json:{id:request.postDataJSON().idempotency_key,status:'Completed',mode:'preview',step_index:1,step_count:1,message:'Preview complete. No inputs sent.'}});
    return route.fulfill({json:remote});
  });
  const button=page.locator('[data-display-id="left-ddi"] .bezel-left [data-button-number="5"]');
  await expect(button).toBeDisabled();
  await expect(button).toHaveAttribute('data-action-id','hornet.left_mdi.pb.5');
  expect(control.writes).toEqual([]);
  await page.getByRole('button',{name:'Connect controls',exact:true}).click();
  await expect(button).toBeEnabled();
  await button.click();
  await expect.poll(()=>control.writes.filter(item=>item.path==='/api/remote/tap').length).toBe(1);
  expect(control.writes.find(item=>item.path==='/api/remote/tap')!.body).toEqual({action_id:'hornet.left_mdi.pb.5',idempotency_key:expect.stringMatching(/^[a-f0-9-]{36}$/)});
  expect(control.writes.filter(item=>!['/api/remote/heartbeat','/api/remote/tap'].includes(item.path))).toEqual([]);
});

test('five exported displays from another aircraft keep their labels and text without Hornet controls or invented video',async({page})=>{
  await page.setViewportSize({width:1440,height:1000});
  const feed=status();feed.enabled=false;feed.status='Unavailable';
  const control=await mock(page,feed,true);
  control.feed.aircraft='FutureJet';control.feed.panels=[];
  const labels=['Pilot left display','Pilot right display','Pilot centre display','Aft left display','Aft right display'];
  await page.evaluate(labels=>{
    const snapshot=(window as any).__feedSnapshot;
    snapshot.health.aircraft='FutureJet';snapshot.health.session='future-jet-flight';
    snapshot.cockpit={aircraft:'FutureJet',status:'Available',displays:labels.map((label,index)=>({id:101+index,label,status:'Available',age_s:.1,elements:[{name:'Readout',value:`EXPORTED PANEL ${index+1}`}]}))};
    snapshot.cockpit.displays.push({id:106,label:'Unexported placeholder',status:'Unavailable',elements:[]});
  },labels);
  const cards=page.locator('.display-overview-grid .display-overview-card');
  await expect(cards).toHaveCount(5);
  for(const [index,label] of labels.entries()){
    await expect(cards.nth(index).getByRole('heading',{name:label,exact:true})).toBeVisible();
    await expect(cards.nth(index)).toContainText(`EXPORTED PANEL ${index+1}`);
    await expect(cards.nth(index)).toContainText('EXPORTED TEXT');
  }
  await expect(cards.locator('img')).toHaveCount(0);
  await expect(cards.locator('.remote-control')).toHaveCount(0);
  await expect(cards.locator('[data-action-id^="hornet."]')).toHaveCount(0);
  await expect(cards.getByRole('button',{name:'Live stream',exact:true})).toHaveCount(0);
  expect(control.writes).toEqual([]);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.locator('.display-overview').screenshot({path:'test-results/cockpit-layout/five-exported-displays.png'});
  await page.evaluate(()=>{for(const display of (window as any).__feedSnapshot.cockpit.displays)if(display.age_s!==undefined)display.age_s=20;});
  await expect(cards.locator('dd')).toHaveCount(0);
  await expect(cards).toHaveCount(5);
  await page.evaluate(()=>{
    const snapshot=(window as any).__feedSnapshot;
    for(const display of snapshot.cockpit.displays)if(display.age_s!==undefined)display.age_s=.1;
    snapshot.health.export_capabilities={cockpit:{status:'denied'}};
  });
  await expect(cards.locator('dd')).toHaveCount(0);
  for(const card of await cards.all())await expect(card).toContainText('Export denied');
  await page.evaluate(()=>{
    const snapshot=(window as any).__feedSnapshot;
    snapshot.health.export_capabilities={};snapshot.cockpit.aircraft='DifferentJet';
  });
  await expect(cards).toHaveCount(0);
  await expect(page.locator('.remote-readout dd')).toHaveCount(0);
  expect(control.writes).toEqual([]);
});
