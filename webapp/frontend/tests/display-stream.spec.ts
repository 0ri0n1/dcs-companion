import {test,expect} from '@playwright/test';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {DisplayLineReader,frameRecord,MAX_LINE} from '../src/display-stream';
import {mock,status,showFeed,setup} from './display-fixture';

test('stream frame updates preserve the surrounding workspace without redrawing its controls each frame',async({page})=>{
  await page.addInitScript(()=>{
    const win=window as any;let workspaceType:any,lastHooks:any,lastProps:any;win.__workspaceRenders=0;
    win.__REACT_DEVTOOLS_GLOBAL_HOOK__={supportsFiber:true,inject:()=>1,onCommitFiberRoot:(_id:number,root:any)=>{
      const visit=(fiber:any):void=>{if(!fiber)return;if(typeof fiber.type==='function'&&(fiber.type===workspaceType||!workspaceType&&String(fiber.type).includes('remote-workspace'))){workspaceType=fiber.type;if(fiber.memoizedState!==lastHooks||fiber.memoizedProps!==lastProps){win.__workspaceRenders++;lastHooks=fiber.memoizedState;lastProps=fiber.memoizedProps;}return;}for(let child=fiber.child;child;child=child.sibling)visit(child);};visit(root.current);
    },onCommitFiberUnmount:()=>{}};
  });
  await page.setViewportSize({width:390,height:844});const control=await mock(page,status(),true);await expect(page.locator('.display-overview-card img')).toHaveCount(3);await page.waitForTimeout(300);
  const before=await page.locator('.display-overview-card img').evaluateAll(images=>images.map(image=>Number(image.getAttribute('data-sequence'))));await page.evaluate(()=>(window as any).__workspaceRenders=0);await page.waitForTimeout(2000);
  const workspaceRenders=await page.evaluate(()=>(window as any).__workspaceRenders),after=await page.locator('.display-overview-card img').evaluateAll(images=>images.map(image=>Number(image.getAttribute('data-sequence'))));
  const evidence={workspace_renders_in_2s:workspaceRenders,frame_advances:after.map((value,index)=>value-before[index]),stream_connections:control.connections.length};
  const build=readFileSync('dist/sw.js','utf8').match(/shell-(v\d+)/)?.[1]??'unknown';mkdirSync('test-results/display-stream',{recursive:true});writeFileSync(`test-results/display-stream/render-profile-${build}.json`,JSON.stringify(evidence,null,2));
  expect(evidence.frame_advances.every(count=>count>=10)).toBe(true);expect(workspaceRenders).toBeGreaterThan(0);expect(workspaceRenders).toBeLessThanOrEqual(12);expect(control.writes).toEqual([]);
});

test('NDJSON reader bounds split records and rejects oversized or truncated lines',()=>{
  const reader=new DisplayLineReader();expect(reader.push('{"type":')).toEqual([]);expect(reader.push('"status"}\n{"a":1}\n')).toEqual([{type:'status'},{a:1}]);reader.finish();expect(()=>new DisplayLineReader().push('x'.repeat(MAX_LINE+1))).toThrow('too large');const truncated=new DisplayLineReader();truncated.push('{"a":');expect(()=>truncated.finish()).toThrow('during a frame');
});
test('frame metadata rejects old, unwanted, nonmonotonic, oversized and wrong-context records',()=>{
  const now=Date.now(),frame={type:'frame',panel:'left_mdi',revision:'r',sequence:2,captured_epoch:now/1000,width:512,height:512,jpeg_base64:'/9j/AAAA'};const panels=new Set(['left_mdi']);expect(frameRecord(frame,'r',panels,1,now)).toEqual(frame);for(const patch of [{revision:'old'},{panel:'ampcd'},{sequence:1},{sequence:1.2},{captured_epoch:(now-2000)/1000},{width:1024},{jpeg_base64:'x'.repeat(3*1024*1024)}])expect(frameRecord({...frame,...patch},'r',panels,1,now)).toBeUndefined();
});
for(const viewport of [{width:390,height:844},{width:844,height:390},{width:1180,height:820},{width:2560,height:1440}])test(`All displays continuously shares one connection at ${viewport.width}x${viewport.height}`,async({page})=>{
  await page.setViewportSize(viewport);const control=await mock(page,status(),true);await expect(page.getByRole('button',{name:'All displays',exact:true})).toHaveAttribute('aria-pressed','true');await expect(page.locator('.display-overview-card')).toHaveCount(3);await expect(page.locator('.display-overview-card img')).toHaveCount(3);
  const sequences=await page.locator('.display-overview-card img').evaluateAll(images=>images.map(image=>Number(image.getAttribute('data-sequence'))));await page.waitForTimeout(700);for(const [index,image] of (await page.locator('.display-overview-card img').all()).entries()){expect(Number(await image.getAttribute('data-sequence'))).toBeGreaterThan(sequences[index]+2);expect(await image.evaluate((node:HTMLImageElement)=>node.naturalWidth)).toBe(512);}
  expect(control.connections).toEqual(['left_mdi,right_mdi,ampcd']);expect(await page.evaluate(()=>(window as any).__streamMaxActive)).toBe(1);expect(control.httpFrames).toBe(0);expect(control.writes).toEqual([]);expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);await page.screenshot({path:`test-results/display-stream/all-${viewport.width}.png`,fullPage:true});
});
test('source choice survives overview and controls; all Text closes the shared stream',async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page,status(),true);await expect(page.locator('.display-overview-card img')).toHaveCount(3);const left=page.getByRole('region',{name:'Left DDI display view'});await left.getByRole('button',{name:'Text',exact:true}).click();await expect(left).toContainText('EXPORTED TEXT FALLBACK');await page.getByRole('button',{name:'Open Left DDI controls',exact:true}).click();await expect(left.getByRole('button',{name:'Text',exact:true})).toHaveAttribute('aria-pressed','true');await expect.poll(()=>page.evaluate(()=>(window as any).__streamActive)).toBe(0);await page.getByRole('button',{name:'All displays',exact:true}).click();await expect(left.getByRole('button',{name:'Text',exact:true})).toHaveAttribute('aria-pressed','true');for(const label of ['Right DDI','AMPCD'])await page.getByRole('region',{name:`${label} display view`}).getByRole('button',{name:'Text',exact:true}).click();await expect.poll(()=>page.evaluate(()=>(window as any).__streamActive)).toBe(0);expect(control.writes).toEqual([]);
});
test('silent heartbeat removes frames within two seconds and reconnects GET without commands',async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page),feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();control.silent=true;await expect(feed.getByRole('img')).toHaveCount(0,{timeout:2300});await expect.poll(()=>control.connections.length).toBeGreaterThan(1);control.silent=false;await expect(feed.getByRole('img')).toBeVisible();expect(control.writes).toEqual([]);
});

test('heartbeat recovery does not wait for a stalled reader or cancellation promise',async({page})=>{
  const control=await mock(page,status(),true);await expect(page.locator('.display-overview-card img')).toHaveCount(3);
  const connections=control.connections.length;
  await page.evaluate(()=>{(window as any).__ignoreStreamAbort=true;(window as any).__stallStreamCancel=true;});control.silent=true;
  await expect(page.locator('.display-overview-card img')).toHaveCount(0,{timeout:2300});
  await expect.poll(()=>control.connections.length,{timeout:2000}).toBe(connections+1);
  control.silent=false;await expect(page.locator('.display-overview-card img')).toHaveCount(3);
  expect(await page.evaluate(()=>(window as any).__streamActive)).toBe(1);expect(await page.evaluate(()=>(window as any).__streamMaxActive)).toBe(1);expect(control.writes).toEqual([]);
});

test('closed streams recover even when browser cancellation never settles',async({page})=>{
  const control=await mock(page,status(),true);await expect(page.locator('.display-overview-card img')).toHaveCount(3);
  const connections=control.connections.length;await page.evaluate(()=>(window as any).__stallStreamCancel=true);control.closeStream=true;
  await expect(page.locator('.display-overview-card img')).toHaveCount(0,{timeout:600});control.closeStream=false;
  await expect(page.locator('.display-overview-card img')).toHaveCount(3,{timeout:2000});expect(control.connections.length).toBe(connections+1);expect(control.writes).toEqual([]);
});

test('a request stuck before response headers cannot hold the reconnect watchdog open',async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page),feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();const connections=control.connections.length;
  await feed.getByRole('button',{name:'Text',exact:true}).click();await page.evaluate(()=>(window as any).__stallStreamFetch=1);
  await feed.getByRole('button',{name:'Live stream',exact:true}).click();await expect(feed.getByRole('img')).toHaveCount(0);
  await expect.poll(()=>control.connections.length,{timeout:3500}).toBe(connections+2);await expect(feed.getByRole('img')).toBeVisible();
  expect(await page.evaluate(()=>(window as any).__streamMaxActive)).toBe(1);expect(control.writes).toEqual([]);
});

for(const event of ['pageshow','online'])test(`${event} recovers visible streams without a page reload or visibility change`,async({page})=>{
  const control=await mock(page,status(),true);await expect(page.locator('.display-overview-card img')).toHaveCount(3);
  const connections=control.connections.length;control.silent=true;
  await page.evaluate(name=>window.dispatchEvent(name==='pageshow'?new PageTransitionEvent('pageshow',{persisted:true}):new Event('online')),event);
  await expect(page.locator('.display-overview-card img')).toHaveCount(0,{timeout:600});
  await expect.poll(()=>control.connections.length,{timeout:600}).toBe(connections+1);control.silent=false;
  await expect(page.locator('.display-overview-card img')).toHaveCount(3);expect(await page.evaluate(()=>(window as any).__streamMaxActive)).toBe(1);expect(control.writes).toEqual([]);
});

test('ordinary pageshow does not interrupt an already healthy stream',async({page})=>{
  const control=await mock(page,status(),true);await expect(page.locator('.display-overview-card img')).toHaveCount(3);const connections=control.connections.length;
  await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:false})));await page.waitForTimeout(600);
  expect(control.connections.length).toBe(connections);await expect(page.locator('.display-overview-card img')).toHaveCount(3);expect(control.writes).toEqual([]);
});
test('a transient unavailable empty revision automatically recovers the same stream without refreshing setup',async({page})=>{
  await page.setViewportSize({width:390,height:844});let statusReads=0;page.on('request',request=>{if(new URL(request.url()).pathname==='/api/display-feed'&&request.method()==='GET')statusReads++;});
  const control=await mock(page),feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();const revision=control.feed.revision,reads=statusReads,connections=control.connections.length;
  control.feed.status='Unavailable';control.feed.revision='';await expect(feed.getByRole('img')).toHaveCount(0,{timeout:600});
  control.feed.status='Ready';control.feed.revision=revision;await expect(feed.getByRole('img')).toBeVisible({timeout:2500});
  expect(control.connections.length).toBeGreaterThan(connections);expect(statusReads).toBe(reads);expect(control.writes).toEqual([]);expect(control.httpFrames).toBe(0);
});
test('continuing status or duplicate sequences never renew the original frame expiry',async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page),feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();control.duplicate=true;const original=await feed.getByRole('img').getAttribute('src');await expect(feed.getByRole('img')).toHaveCount(0,{timeout:2300});expect(await page.evaluate(url=>(window as any).__revokedUrls.includes(url),original)).toBe(true);control.duplicate=false;await expect(feed.getByRole('img')).toBeVisible();
});
for(const flag of ['stale','wrongRevision','badDimensions','badJpeg'] as const)test(`invalid ${flag} frames cannot replace independently fresh displays`,async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page),feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();control[flag]=true;await expect(feed.getByRole('img')).toHaveCount(0,{timeout:2300});control[flag]=false;await expect(feed.getByRole('img')).toBeVisible();expect(control.writes).toEqual([]);
});
test('stream closure immediately removes old images, and malformed lines do not keep a static frame',async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page),feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();control.closeStream=true;await expect(feed.getByRole('img')).toHaveCount(0,{timeout:600});control.closeStream=false;await expect(feed.getByRole('img')).toBeVisible();control.malformed=true;await expect(feed.getByRole('img')).toHaveCount(0,{timeout:600});control.malformed=false;await expect(feed.getByRole('img')).toBeVisible();
});
test('split chunks work and oversized lines abort without retaining previous frames',async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page);control.split=true;const feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();control.oversized=true;await expect(feed.getByRole('img')).toHaveCount(0,{timeout:800});control.oversized=false;await expect(feed.getByRole('img')).toBeVisible();
});
test('hidden pages close streams and revoke frames; resumed pages create a fresh connection',async({page})=>{
  const control=await mock(page,status(),true);await expect(page.locator('.display-overview-card img')).toHaveCount(3);await page.evaluate(()=>{Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.dispatchEvent(new Event('visibilitychange'));});await expect(page.locator('.display-overview-card img')).toHaveCount(0);expect(await page.evaluate(()=>(window as any).__streamActive)).toBe(0);expect(await page.evaluate(()=>(window as any).__revokedUrls.length)).toBeGreaterThan(0);await page.evaluate(()=>{Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});document.dispatchEvent(new Event('visibilitychange'));});await expect(page.locator('.display-overview-card img')).toHaveCount(3);expect(control.writes).toEqual([]);
});
test('revision and session changes clear old frames before new context can draw',async({page})=>{
  await page.setViewportSize({width:390,height:844});const control=await mock(page),feed=await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();const old=await feed.getByRole('img').getAttribute('src');control.noFrames=true;control.feed.revision='new-revision';await expect(feed.getByRole('img')).toHaveCount(0,{timeout:600});await setup(page);await page.getByRole('button',{name:'Refresh display setup',exact:true}).click();control.noFrames=false;await showFeed(page);await expect(feed.getByRole('img')).toBeVisible();expect(await feed.getByRole('img').getAttribute('src')).not.toBe(old);control.noFrames=true;await page.evaluate(()=>{(window as any).__feedSnapshot.health.session='new-session';});await expect(feed.getByRole('img')).toHaveCount(0,{timeout:800});expect(control.writes).toEqual([]);
});
