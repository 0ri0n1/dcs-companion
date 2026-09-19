import { test, expect, type Page } from '@playwright/test';
import type { Snapshot } from '../src/types';

const storageKey = 'dcs-copilot-display-v1';
const themes = ['classic','hornet','raptor','eagle','lightning','daylight','contrast'] as const;
const views = ['Navigation Scope','Controls','Guide','Sensors','Cockpit','Flight data','Data Health'] as const;

// Deliberately synthetic browser fixtures; no fixture data reaches the running DCS service.
function fixture(aircraft = 'F-22A'): Snapshot {
  return {
    server:{dry_run:true,lan_enabled:false,connected_clients:1,version:'theme-test'},
    health:{status:'Live',aircraft,terrain:'Caucasus',mission:'theme-test-mission',session:'theme-test-session',telemetry_age_s:.1,display_age_s:.1,telemetry_fresh:true,displays_fresh:true,model_advancing:true,dcs_running:true,dcs_focused:true,sensor_status:'Unverified'},
    mission:{status:'Current',name:'Synthetic theme readability check',theatre:'Caucasus',age_s:.1},
    aircraft:{ground_speed_kt:300},
    navigation:{terrain:'Caucasus',status:'Available',available_terrains:['Caucasus'],ownship:{lat:42,lon:42,heading_true_deg:40,track_true_deg:43,ground_speed_kt:300,altitude_ft:5000},airfields:[{id:'theme-field',name:'Theme Fixture Airfield',lat:42.1,lon:42.1,runway_geometry_status:'Unknown',runways:[],frequencies:[{value:131,unit:'MHz',modulation:'AM',purpose:'Tower',band:'VHF'}],navaids:[]}],navaids:[],route:[],provenance:{dcs_version:'test',schema_version:1,sources:[]}},
    bindings:{aircraft,status:'Current',source_fingerprint:'synthetic-theme-fingerprint',devices:[{id:'keyboard',name:'Keyboard',type:'keyboard'},{id:'hotas',name:'HOTAS',type:'hotas'}],actions:[{id:'theme-nav',name:'Next navigation point',device_id:'keyboard',combos:['1'],category:['Navigation']},{id:'theme-unbound',name:'Unbound fixture action',device_id:'hotas',combos:[],category:['Flight']}],physical_input:{status:'Unavailable'}},
    guide:{actions:[{id:'theme.guide',title:'Next navigation point',purpose:'A synthetic guide instruction for a readability check.',binding:'1',location:'Keyboard',expected_result:'Navigation selection advances',observable:false,risk:'benign',enabled:true}]},
    cockpit:{aircraft,status:'Available',displays:[{id:'0',label:'Synthetic display',status:'Available',age_s:.1,elements:[{name:'Navigation range',value:'80 NM'},{name:'Display field label',value:'READABILITY FIXTURE'}]}]},
    export_data:{aircraft,session:'theme-test-session',status:'Available',groups:[{id:'flight',label:'Synthetic flight packet',status:'Available',age_s:.1,data:{altitude:5000},source:'Test fixture'}],recording:{status:'Unavailable'}},
    queue:{state:'Idle',history:[]},adapters:[],
  };
}

async function mock(page: Page, state = fixture()) {
  let commandPosts = 0;
  page.on('request', request => {
    if(request.method()==='POST' && new URL(request.url()).pathname.startsWith('/api/commands')) commandPosts++;
  });
  await page.addInitScript(snapshot => {
    (window as any).__themeState = snapshot;
    (window as any).__themeStreams = [];
    (window as any).EventSource = class {
      onmessage: any; onerror: any; timer: any;
      constructor() {
        (window as any).__themeStreams.push(this);
        this.timer = setInterval(() => {
          if(!(window as any).__themeSilence) this.onmessage?.({data:JSON.stringify((window as any).__themeState)});
        }, 150);
      }
      addEventListener() {}
      close() { clearInterval(this.timer); }
    };
  }, state);
  await page.route('**/api/session', route => route.fulfill({json:{ok:true}}));
  await page.route('**/api/state', async route => route.fulfill({json:await page.evaluate(() => (window as any).__themeState)}));
  await page.route('**/api/commands**', route => route.fulfill({status:400,json:{detail:'Appearance must not send aircraft commands'}}));
  return () => commandPosts;
}

async function appearance(page: Page, open: boolean) {
  if(await page.getByRole('combobox', {name:'Color theme',exact:true}).isVisible() !== open) {
    await page.getByRole('button', {name:open?'Appearance':'Done',exact:true}).click();
  }
  if(open) await expect(page.getByRole('combobox', {name:'Color theme',exact:true})).toBeVisible();
}

async function choose(page: Page, theme: string, size?: string) {
  await appearance(page, true);
  await page.getByRole('combobox', {name:'Color theme',exact:true}).selectOption(theme);
  if(size) await page.getByRole('combobox', {name:'Text size',exact:true}).selectOption(size);
  await appearance(page, false);
}

async function setAircraft(page: Page, aircraft: string, fresh = true) {
  await page.evaluate(({aircraft,fresh}) => Object.assign((window as any).__themeState.health, {
    aircraft,telemetry_fresh:fresh,dcs_running:fresh,status:fresh?'Live':'Waiting for cockpit',session:`theme-${aircraft}`,
  }), {aircraft,fresh});
}

test('automatic theme follows live aircraft, retains the last palette in menus and falls back on a cold unknown aircraft', async ({page}) => {
  const posts = await mock(page, fixture('Unknown aircraft'));
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme','contrast');
  await expect(page.locator('html')).toHaveAttribute('data-readability','comfortable');
  for(const [aircraft,palette] of [['FA-18C_hornet','hornet'],['F-22A','raptor'],['F-23','raptor'],['F-15C','eagle'],['F-15ESE','eagle'],['F-35A','lightning'],['F-35B','lightning']]) {
    await setAircraft(page,aircraft);
    await expect(page.locator('html')).toHaveAttribute('data-theme',palette);
  }
  await setAircraft(page,'NO AIRCRAFT',false);
  await expect(page.getByText('Waiting for cockpit',{exact:true})).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-theme','lightning');
  await page.route('**/api/state', route => route.abort());
  await page.evaluate(() => {(window as any).__themeSilence=true;(window as any).__themeStreams.at(-1).onerror();});
  await expect(page.getByText('Live connection unavailable.',{exact:false})).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-theme','lightning');
  expect(posts()).toBe(0);
});

test('manual palettes and larger text survive reload and aircraft changes without storing flight data', async ({page}) => {
  const posts = await mock(page);
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme','raptor');
  await choose(page,'daylight','large');
  await expect(page.locator('html')).toHaveAttribute('data-theme','daylight');
  await expect(page.locator('html')).toHaveAttribute('data-readability','large');
  await setAircraft(page,'FA-18C_hornet');
  await expect(page.locator('.session-identity')).toContainText('FA-18C_hornet');
  await expect(page.locator('html')).toHaveAttribute('data-theme','daylight');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme','daylight');
  await expect(page.locator('html')).toHaveAttribute('data-readability','large');
  await appearance(page,true);
  await expect(page.getByRole('combobox',{name:'Color theme',exact:true})).toHaveValue('daylight');
  await expect(page.getByRole('combobox',{name:'Text size',exact:true})).toHaveValue('large');
  const saved = await page.evaluate(key => JSON.parse(localStorage.getItem(key)!),storageKey);
  expect(saved.theme).toBe('daylight');expect(saved.textSize).toBe('large');
  expect(Object.keys(saved).sort()).toEqual(['view','brightness','orientation','range','rings','airfields','navaids','route','mission','enemies','friendlies','theme','textSize'].sort());
  expect(JSON.stringify(saved)).not.toMatch(/theme-test-mission|theme-test-session|Synthetic|FA-18C|F-22A/);
  await page.getByRole('combobox',{name:'Color theme',exact:true}).selectOption('auto');
  await expect(page.locator('html')).toHaveAttribute('data-theme','raptor');
  expect(posts()).toBe(0);
});

test('invalid appearance preferences revert to valid defaults and discard arbitrary stored keys', async ({page}) => {
  const posts = await mock(page);
  await page.addInitScript(key => localStorage.setItem(key,JSON.stringify({theme:'javascript:invalid',textSize:999,aircraft:'FA-18C_hornet',contacts:[{lat:42,lon:42}],mode:'Assist'})), storageKey);
  await page.goto('/');await appearance(page,true);
  await expect(page.getByRole('combobox',{name:'Color theme',exact:true})).toHaveValue('auto');
  await expect(page.getByRole('combobox',{name:'Text size',exact:true})).toHaveValue('comfortable');
  await expect(page.locator('html')).toHaveAttribute('data-theme','raptor');
  await expect(page.locator('html')).toHaveAttribute('data-readability','comfortable');
  const saved=await page.evaluate(key => JSON.parse(localStorage.getItem(key)!),storageKey);
  expect(saved).not.toHaveProperty('aircraft');expect(saved).not.toHaveProperty('contacts');expect(saved).not.toHaveProperty('mode');
  expect(posts()).toBe(0);
});

test('appearance works from the keyboard and reset restores readable defaults without altering flight state', async ({page}) => {
  const posts=await mock(page);await page.goto('/');
  const trigger=page.getByRole('button',{name:'Appearance',exact:true});
  await trigger.focus();await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog',{name:'Appearance',exact:true})).toBeVisible();
  await page.getByRole('combobox',{name:'Color theme',exact:true}).selectOption('eagle');
  await page.getByRole('combobox',{name:'Text size',exact:true}).selectOption('large');
  await page.getByLabel('Appearance brightness',{exact:true}).focus();await page.keyboard.press('Home');
  await expect(page.getByLabel('Appearance brightness',{exact:true})).toHaveValue('35');
  await page.getByRole('button',{name:'Reset appearance',exact:true}).click();
  await expect(page.getByRole('combobox',{name:'Color theme',exact:true})).toHaveValue('auto');
  await expect(page.getByRole('combobox',{name:'Text size',exact:true})).toHaveValue('comfortable');
  await expect(page.getByLabel('Appearance brightness',{exact:true})).toHaveValue('100');
  await expect(page.locator('html')).toHaveAttribute('data-theme','raptor');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog',{name:'Appearance',exact:true})).not.toBeVisible();
  await expect(trigger).toBeFocused();
  expect(await page.evaluate(()=>(window as any).__themeState.health.session)).toBe('theme-test-session');
  expect(posts()).toBe(0);
});

/** Checks actual rendered ink against its composited solid surface, not palette declarations. */
async function textContrasts(page: Page, selector: string) {
  return page.locator(selector).evaluateAll(elements => {
    const canvas=document.createElement('canvas');canvas.width=1;canvas.height=1;
    const context=canvas.getContext('2d',{willReadFrequently:true})!;
    function color(value: string): number[] {
      context.clearRect(0,0,1,1);context.fillStyle=value;context.fillRect(0,0,1,1);
      const rgba=context.getImageData(0,0,1,1).data;
      return [rgba[0],rgba[1],rgba[2],rgba[3]/255];
    }
    function over(front:number[],back:number[]):number[] {
      const alpha=front[3]+back[3]*(1-front[3]);
      return [...[0,1,2].map(i=>alpha?(front[i]*front[3]+back[i]*back[3]*(1-front[3]))/alpha:0),alpha];
    }
    function surface(element:Element):number[] {
      const chain:Element[]=[];let node:Element|null=element;
      while(node){chain.unshift(node);node=node.parentElement;}
      return chain.reduce((background,item)=>over(color(getComputedStyle(item).backgroundColor),background),[255,255,255,1]);
    }
    function luminance(rgb:number[]):number {
      const linear=rgb.slice(0,3).map(channel=>{const c=channel/255;return c<=.04045?c/12.92:((c+.055)/1.055)**2.4;});
      return .2126*linear[0]+.7152*linear[1]+.0722*linear[2];
    }
    return elements.filter(element => element.getClientRects().length && getComputedStyle(element).visibility!=='hidden').map(element => {
      const style=getComputedStyle(element),background=surface(element);
      // SVG labels can sit over either end of the scope's radial gradient.
      const backgrounds=[background,...(element instanceof SVGElement?Array.from(element.closest('svg')?.querySelectorAll('radialGradient stop')??[]).map(stop=>color(getComputedStyle(stop).stopColor)):[])];
      const ratios=backgrounds.map(back=>{
        const foreground=over(color(element instanceof SVGElement?style.fill:style.color),back);
        const [high,low]=[luminance(foreground),luminance(back)].sort((a,b)=>b-a);
        return (high+.05)/(low+.05);
      });
      return {element:`${element.tagName}.${element.className instanceof SVGAnimatedString?element.className.baseVal:element.className}`,text:element.textContent?.trim().slice(0,70),ratio:Math.min(...ratios),fontSize:parseFloat(style.fontSize)};
    });
  });
}

for(const theme of themes) test(`${theme} keeps principal scope text and controls readable`, async ({page}) => {
  await page.setViewportSize({width:2560,height:1440});
  const posts=await mock(page);await page.goto('/');await choose(page,theme);
  await page.getByRole('button',{name:'Select Theme Fixture Airfield',exact:true}).click();
  const rows=await textContrasts(page,'.session-identity strong,.mission-identity strong,.eyebrow,.view-tabs button,.badge,.metric>div,.field-card .muted,.field-card .micro,.airfield-item strong,.airfield-item small,.scope-corner,.scope-toolbar select,.compass-label,.ring-label,.field-symbol text');
  expect(rows.length).toBeGreaterThan(30);
  expect(rows.filter(row=>row.ratio<4.5),JSON.stringify(rows.filter(row=>row.ratio<4.5),null,2)).toEqual([]);
  await appearance(page,true);
  for(const label of ['Color theme','Text size']) {
    const bounds=await page.getByRole('combobox',{name:label,exact:true}).boundingBox();expect(bounds?.height).toBeGreaterThanOrEqual(44);
  }
  await page.getByRole('combobox',{name:'Text size',exact:true}).selectOption('large');
  await appearance(page,false);
  const larger=await textContrasts(page,'.field-card .muted,.field-card .micro,.eyebrow');
  expect(Math.min(...larger.map(row=>row.fontSize))).toBeGreaterThanOrEqual(14);
  expect(posts()).toBe(0);
});

for(const viewport of [{width:2560,height:1440},{width:1180,height:820},{width:820,height:1180}]) {
  for(const theme of ['daylight','contrast']) test(`${theme} and large text remain usable across seven views at ${viewport.width}x${viewport.height}`, async ({page}) => {
    await page.setViewportSize(viewport);const posts=await mock(page);const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto('/');await choose(page,theme,'large');
    for(const view of views) {
      await page.getByRole('button',{name:view,exact:true}).click();
      await expect(page.locator('main h1:visible')).toBeVisible();
      const dimensions=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth}));
      expect(dimensions.scrollWidth,`${view} horizontal overflow`).toBeLessThanOrEqual(dimensions.width+1);
      const copy=await textContrasts(page,'main .page-title p,main .step-details dt,main .step-details dd,main .cockpit-readout dt,main .cockpit-readout dd,main .binding-row strong,main .binding-row span:not(.badge)');
      expect(copy.filter(row=>row.ratio<4.5),`${view}: ${JSON.stringify(copy.filter(row=>row.ratio<4.5))}`).toEqual([]);
      if(['Navigation Scope','Controls','Cockpit'].includes(view)) await page.screenshot({path:`test-results/themes/${theme}-large-${view.replaceAll(' ','-').toLowerCase()}-${viewport.width}.png`,fullPage:true});
    }
    expect(errors).toEqual([]);expect(posts()).toBe(0);
  });
}
