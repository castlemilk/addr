import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008';
const response=await fetch(`${base}/api/scenes/training`);
assert.equal(response.status,200,await response.clone().text());
const live=await response.json();assert.equal(live.classes.length,13);
assert.equal(live.evaluation.dataset_version,live.version);
// Exercise the real write guard without changing the versioned dataset.
const before=await readFile('data/training/scenes/latest.json','utf8');
const denied=await fetch(`${base}/api/scenes/training`,{method:'POST',headers:{Origin:'https://another-site.invalid'}});
assert.equal(denied.status,403);assert.equal(await readFile('data/training/scenes/latest.json','utf8'),before);
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1440,height:1000});
let mode='live',failRead=false,failExport=false,hold=true,held=null,posts=0;
await page.setRequestInterception(true);
page.on('request',request=>{void(async()=>{
 if(new URL(request.url()).pathname!=='/api/scenes/training')return request.continue();
 if(request.method()==='POST') {
  posts++;
  if(failExport){failExport=false;return request.respond({status:403,contentType:'application/json',body:JSON.stringify({error:'Object editing access is required to export the scene dataset.'})});}
  return request.respond({status:200,contentType:'application/json',body:JSON.stringify({...live,export_current:true,latest_export:{...live.latest_export,version:live.version}})});
 }
 if(hold){held=request;return;}
 if(failRead){failRead=false;return request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Coverage could not be verified. Please retry.'})});}
 const result=structuredClone(live);
 if(mode==='empty'){result.scenes=0;result.reviewed_objects=0;result.classes=result.classes.map(c=>({...c,layers:[],references:0,pending:0,withheld:0,splits:Object.fromEntries(Object.keys(c.splits).map(s=>[s,{images:0,masks:0,groups:0,empty_images:0}]))}));result.latest_export=null;}
 if(mode==='unscanned-trees'){
  const tree=result.classes.find(c=>c.kind==='tree');tree.layers=tree.layers.map(l=>({...l,scan:'not_scanned',scan_count:0}));
  const evaluation=result.evaluation.classes.find(c=>c.kind==='tree');evaluation.layers=[];
  evaluation.splits=Object.fromEntries(['train','validation','test'].map(s=>[s,{images:0,groups:0,empty_images:0,predictions:0,references:0,matched:0,extra:0,missed:0,invalid_predictions:0,precision:null,recall:null,mean_outline_iou:null}]));
 }
 return request.respond({status:200,contentType:'application/json',body:JSON.stringify(result)});
})().catch(e=>errors.push(e.message));});
const button=async label=>(await page.waitForSelector(`button::-p-text(${label})`)).click();
const ready=async()=>page.waitForFunction(()=>document.querySelector('.training-metrics')&&!document.querySelector('.training-heading button').disabled);
const choose=async(index,value)=>{
 const selects=await page.$$('.training-filters select');await selects[index].select(value);
 await page.waitForFunction((i,v)=>new URL(location.href).searchParams.get(['kind','split','work'][i])===v,{},index,value);
 // Native select input is synchronous; the external URL store renders afterwards.
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
};
try{
 await page.goto(`${base}/scenes/training`,{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>document.body.textContent.includes('Checking saved labels'));
 assert(await page.$eval('.training-heading button',e=>e.disabled));
 for(let i=0;i<30&&!held;i++)await new Promise(resolve=>setTimeout(resolve,50));assert(held);
 hold=false;await held.respond({status:200,contentType:'application/json',body:JSON.stringify(live)});await ready();
 assert.equal(await page.$$eval('.training-table tbody tr',rows=>rows.length),13);
 assert.equal(await page.$eval('.training-metrics dd',e=>Number(e.textContent)),live.scenes);
 const solarEvaluation=live.evaluation.classes.find(c=>c.kind==='solar_panel');
 assert(await page.$eval('.training-evaluation h3',e=>e.textContent.includes('Solar arrays')));
 for(const split of ['train','validation','test']){
  const m=solarEvaluation.splits[split],row=await page.$(`tr[data-evaluation-split="${split}"]`);
  assert(row);const cells=await row.$$eval('td',es=>es.map(e=>e.textContent));
  assert.deepEqual(cells,[`${m.matched} / ${m.references}`,String(m.extra),String(m.missed),...[m.precision,m.recall,m.mean_outline_iou].map(v=>v===null?'—':`${(v*100).toFixed(1)}%`)]);
 }
 await page.click('.evaluation-method summary');assert.match(await page.$eval('.evaluation-method',e=>e.textContent),/not COCO average precision/);
 await page.click('.evaluation-images summary');
 assert.equal((await page.$$('.evaluation-images li')).length,solarEvaluation.layers.length);
 for(const layer of solarEvaluation.layers)assert.equal(await page.$eval(`[data-evaluation-scene="${layer.scene_id}"] a`,e=>e.getAttribute('href')),layer.review_url);
 await page.focus('button[aria-label^="Solar arrays, Validation:"]');await page.keyboard.press('Enter');
 await page.waitForFunction(()=>document.activeElement===document.querySelector('.training-layer-section'));
 assert.equal(new URL(page.url()).searchParams.get('split'),'validation');
 const validationLayers=live.classes.find(c=>c.kind==='solar_panel').layers.filter(l=>l.split==='validation');
 if(validationLayers.length){await page.waitForFunction(n=>document.querySelectorAll('.training-layer-list li').length===n,{},validationLayers.length);assert.equal(await page.$$eval('.training-layer-list li',items=>items.length),validationLayers.length);}
 else assert(await page.$eval('.training-empty',e=>e.textContent.includes('No validation scenes')));
 if(validationLayers.length){await choose(1,'all');await choose(2,'all');}else await button('Show all layers');assert.equal(await page.$$eval('.training-layer-list li',items=>items.length),live.scenes);
 await choose(0,'tree');await choose(1,'train');
 const treeMetrics=live.evaluation.classes.find(c=>c.kind==='tree').splits.train;
 if(treeMetrics.images){
  const cells=await page.$$eval('tr[data-evaluation-split="train"] td',es=>es.map(e=>e.textContent));
  assert.deepEqual(cells,[`${treeMetrics.matched} / ${treeMetrics.references}`,String(treeMetrics.extra),String(treeMetrics.missed),...[treeMetrics.precision,treeMetrics.recall,treeMetrics.mean_outline_iou].map(v=>v===null?'—':`${(v*100).toFixed(1)}%`)]);
 }
 // Exercise the empty evaluation independently of the growing real dataset.
 // Manually checked layers can be exported even before detector scanning.
 mode='unscanned-trees';await button('Refresh coverage');await ready();
 assert.match(await page.$eval('.training-evaluation',e=>e.textContent),/No fully checked, scanned image layers qualify/);
 assert(await page.$eval('.training-layer-list',e=>e.textContent.includes('Not scanned for this class')));
 assert.equal(new URL(page.url()).searchParams.get('kind'),'tree');
 mode='live';
 await page.reload({waitUntil:'networkidle2'});await ready();
 assert.equal(await page.$eval('.training-filters select',e=>e.value),'tree');
 await choose(0,'solar_panel');await choose(1,'test');await choose(2,'pending');
 const geographyBlocked=live.classes.find(c=>c.kind==='solar_panel').layers.filter(l=>l.split==='test'&&l.blockers.includes('image_geography'));
 for(const layer of geographyBlocked){
  const row=await page.$(`li[data-scene="${layer.scene_id}"]`);assert(row);
  assert((await row.evaluate(e=>e.textContent)).includes('Image geography — withheld'));
  await (await row.$('summary')).click();
  assert((await row.evaluate(e=>e.innerText)).includes(layer.image_geography.reason));
 }
 await choose(1,'all');await choose(2,'pending');
 const expected=live.classes.find(c=>c.kind==='solar_panel').layers.filter(l=>!l.eligible);
 assert.equal(await page.$$eval('.training-layer-list li',items=>items.length),expected.length);
 const target=expected.find(l=>new URL(l.review_url,base).searchParams.get('object'));
 const link=await page.$(`li[data-scene="${target.scene_id}"] a`);
 assert.equal(await link.evaluate(e=>e.getAttribute('href')),target.review_url);
 const savedURL=page.url();await Promise.all([page.waitForNavigation({waitUntil:'networkidle2'}),link.click()]);
 assert.equal(new URL(page.url()).searchParams.get('object'),new URL(target.review_url,base).searchParams.get('object'));
 await page.waitForSelector('.scene-inspector input');
 assert(await page.$('a[href="/scenes/training"]'));
 await page.goto(savedURL,{waitUntil:'networkidle2'});await ready();
 failRead=true;await button('Refresh coverage');await page.waitForSelector('[role="alert"]');
 assert(await page.$eval('[role="alert"]',e=>e.textContent.includes('last successful refresh')));
 assert(await page.$eval('.training-heading button',e=>e.disabled));
 assert.equal(await page.$$eval('.training-layer-list li',items=>items.length),expected.length);
 await button('Retry coverage');await ready();assert(!await page.$('[role="alert"]'));
 failExport=true;await button('Export reviewed dataset');await page.waitForSelector('[role="alert"]');
 assert(await page.$eval('[role="alert"]',e=>e.textContent.includes('Object editing access')));
 assert.equal(new URL(page.url()).searchParams.get('kind'),'solar_panel');
 await button('Retry coverage');await ready();await button('Export reviewed dataset');
 await page.waitForFunction(()=>document.querySelector('.training-feedback')?.textContent.includes('Saved labels exported'));
 assert.equal(posts,2);assert(await page.$eval('.training-export',e=>e.textContent.includes('Matches these saved reviews')));
 await page.setViewport({width:390,height:844});await page.reload({waitUntil:'networkidle2'});await ready();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.$eval('.training-filters',e=>e.scrollIntoView());
 await page.screenshot({path:'/tmp/addr-scene-training-mobile.png'});
 await page.focus('[aria-label="Model check by dataset split"]');await page.keyboard.press('ArrowRight');
 await page.waitForFunction(()=>document.querySelector('[aria-label="Model check by dataset split"]').scrollLeft>0);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Only the score table scrolls horizontally');
 // Native select remains keyboard-operable at narrow widths.
 await page.focus('.training-filters select');await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
 await choose(0,'solar_panel');await choose(2,'ready');
 assert.equal(await page.$$eval('.training-layer-list li',items=>items.length),live.scenes-expected.length);
 mode='empty';await button('Refresh coverage');await page.waitForFunction(()=>document.body.textContent.includes('No scenes captured yet'));
 assert(await page.$eval('.training-heading button',e=>e.disabled));assert(await page.$('a[href="/scenes/new"]'));
 mode='live';failRead=true;await page.reload({waitUntil:'networkidle2'});await page.waitForSelector('[role="alert"]');
 assert(!await page.$('.training-metrics'));await button('Retry coverage');await ready();
 await page.setViewport({width:1440,height:1000});await page.evaluate(()=>window.scrollTo(0,0));
 await page.screenshot({path:'/tmp/addr-scene-training-desktop.png',fullPage:true});
 assert.deepEqual(errors,[]);assert.equal(await readFile('data/training/scenes/latest.json','utf8'),before);
 await writeFile('/tmp/addr-scene-training-ui.json',JSON.stringify({passed:true,scenes:live.scenes,classes:13,checks:['loading','split coverage','keyboard filtering','exact-object navigation','refresh failure and retry','permission failure and retry','export state','empty dataset','initial load failure','mobile containment','source preserved']},null,2));
 console.log('Scene training coverage: real API/read-only checks and origin guard, split filters, exact-object links, loading/empty/error states, permission retry, export feedback, keyboard and mobile passed.');
}catch(error){console.error(page.url());console.error(await page.$eval('body',e=>e.innerText.slice(-4500)));await page.screenshot({path:'/tmp/addr-scene-training-failure.png',fullPage:true});throw error;}
finally{await browser.close();}
