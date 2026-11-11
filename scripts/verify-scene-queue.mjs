import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008',stamp=Date.now(),ids=['a','b','c'].map(s=>`qa_queue_${s}_${stamp}`);
const source=JSON.parse(await readFile('data/scenes/map-a31010822250fb97/latest.json','utf8'));
const cars=source.objects.filter(o=>o.kind==='car'),tree=source.objects.find(o=>o.kind==='tree');
const item=(object,label,status)=>({...object,label,status,geometry_reviewed:status==='confirmed',parent_id:undefined,group_id:undefined});
const groups=[
 [item(cars[0],'Reviewed red','confirmed'),item(cars[1],'Target driveway car','needs_adjustment'),item(cars[2],'Rejected item','rejected'),item(tree,'Garden tree','candidate')],
 [item(tree,'Garden tree','candidate'),item(cars[0],'Spare vehicle','needs_adjustment'),item(cars[1],'Target white','needs_adjustment')],
 [item(cars[0],'Target finished','confirmed'),item(tree,'Garden tree','candidate')],
];
const models=[];
const load=async id=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
for(let i=0;i<ids.length;i++) {
 const id=ids[i],directory=`data/scenes/${id}`,model={...source,id,title:`Queue image ${'ABC'[i]}`,groups:[],objects:groups[i].map(o=>({...o,status:'candidate',geometry_reviewed:false}))};models.push(model);
 await mkdir(directory);await writeFile(`${directory}/latest.json`,JSON.stringify(model));await cp(`data/scenes/map-a31010822250fb97/${source.image_sha256}.png`,`${directory}/${source.image_sha256}.png`);
 const response=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,objects:groups[i],groups:[],reviewed_classes:[]}})});
 assert.equal(response.status,200,await response.clone().text());
}
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1500,height:1080});
// Only this fixture browser sees the temporary images in its list. Production
// listing/export still excludes qa_ directories; object loads/saves use the API.
let failSave=false,failQueue=false,heldRequest=null;
const listings=async url=>{
 const kind=url.searchParams.get('kind')||'all',status=url.searchParams.get('status')||'candidate',q=(url.searchParams.get('q')||'').trim().toLowerCase();
 const scenes=[];
 for(const id of ids){const data=await load(id),objects=data.review.objects,counts={candidate:0,needs_adjustment:0,confirmed:0,rejected:0,superseded:0};for(const o of objects)counts[o.status]++;
  const active=counts.candidate+counts.needs_adjustment+counts.confirmed,total=active+counts.rejected;
  const queue=objects.filter(o=>(kind==='all'||o.kind===kind)&&(status==='all'||o.status===status)&&`${o.label} ${o.kind} ${o.id}`.toLowerCase().includes(q));
  scenes.push({id,title:data.model.title,address_id:null,objects:total,reviewed:counts.confirmed+counts.rejected,progress:{...counts,total,active,resolved:counts.confirmed+counts.rejected},stale:false,queue_count:queue.length,next_object:queue[0]?{id:queue[0].id,label:queue[0].label}:null});
 }return {scenes};
};
await page.setRequestInterception(true);
page.on('request',request=>{void (async()=>{
 const url=new URL(request.url());
 if(url.pathname==='/api/scenes'&&request.method()==='GET'&&!url.searchParams.has('id')) {
  if(failQueue&&url.searchParams.has('kind')){failQueue=false;return request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Temporary image queue interruption.'})});}
  if(url.searchParams.get('q')==='slow'){heldRequest=request;return;}
  return request.respond({status:200,contentType:'application/json',body:JSON.stringify(await listings(url))});
 }
 if(failSave&&url.pathname==='/api/scenes'&&request.method()==='POST'){failSave=false;return request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Temporary save interruption. Draft retained.'})});}
 return request.continue();
 })().catch(()=>{});});
const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
const input=async(selector,value)=>{const field=await page.$(selector);await field.focus();await field.evaluate(e=>e.select());await field.press('Backspace');await field.type(value);await field.press('Tab');};
const nextReady=async title=>page.waitForFunction(t=>document.querySelector('.scene-image-queue')?.textContent.includes(`Next: ${t}`)&&!document.querySelector('.scene-image-queue-actions button:last-child')?.disabled,{},title);
const saved=async revision=>{await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`),{},revision);assert(!await page.$('.scene-status-space [role="alert"]'));};
try {
 await page.goto(`${base}/scenes?scene=${ids[0]}&kind=car&status=needs_adjustment&q=Target&object=${cars[1].id}`,{waitUntil:'networkidle2'});
 await nextReady('Queue image B');
 assert.equal(await page.$eval('.scene-inspector input',e=>e.value),'Target driveway car');
 assert.deepEqual(await page.$$eval('.scene-review-progress dl > div',items=>Object.fromEntries(items.map(e=>[e.dataset.status,Number(e.querySelector('dd').textContent)]))),{candidate:1,needs_adjustment:1,confirmed:1,rejected:1});
 assert(await page.$('.scene-layer-row button[title="2 active · 1 rejected · 0 replaced"]'));
 await input('.scene-inspector input','Target driveway car adjusted');
 assert(await page.$eval('#scene-choice',e=>e.disabled));assert(await page.$eval('.scene-image-queue-actions button:last-child',e=>e.disabled));
 assert(await page.$eval('.scene-progress-caption',e=>e.textContent.includes('Unsaved counts')));
 failSave=true;await button('Save scene');await page.waitForSelector('.scene-status-space [role="alert"]');
 assert.equal(new URL(page.url()).searchParams.get('scene'),ids[0]);assert(await page.$eval('.scene-image-queue-actions button:last-child',e=>e.disabled));
 await button('Discard draft');await nextReady('Queue image B');
 assert.equal(await page.$eval('select[aria-label="Object review filter"]',e=>e.value),'needs_adjustment','Discard preserves a filter that still includes the restored object');
 await page.focus('.scene-image-queue-actions button:last-child');await page.keyboard.press('Enter');
 await page.waitForFunction(id=>new URL(location.href).searchParams.get('scene')===id,{},ids[1]);
 await page.waitForSelector('.scene-inspector input');assert.equal(await page.$eval('.scene-inspector input',e=>e.value),'Target white');
 assert.equal(new URL(page.url()).searchParams.get('object'),cars[1].id);assert.equal(new URL(page.url()).searchParams.get('q'),'Target');
 await page.reload({waitUntil:'networkidle2'});assert.equal(await page.$eval('.scene-inspector input',e=>e.value),'Target white','A deep link must reopen the same filtered object');
 await button('Looks right');await saved(2);
 assert.equal(await page.$eval('[data-status="needs_adjustment"] dd',e=>e.textContent),'1','Unresolved objects remain pending after another object is confirmed');
 assert.equal(await page.$eval('[data-status="confirmed"] dd',e=>e.textContent),'1');
 await nextReady('Queue image A');
 await button('Undo last save');await saved(3);
 assert.equal(await page.$eval('[data-status="needs_adjustment"] dd',e=>e.textContent),'2');assert.equal(await page.$eval('[data-status="confirmed"] dd',e=>e.textContent),'0');
 await page.select('select[aria-label="Object review filter"]','confirmed');await nextReady('Queue image C');
 await input('input[aria-label="Search scene objects"]','nothing-matches');
 await page.waitForFunction(()=>document.querySelector('.scene-image-queue')?.textContent.includes('No other images match these filters'));
 assert(await page.$eval('.scene-image-queue-actions button:last-child',e=>e.disabled));
 await page.select('select[aria-label="Object review filter"]','needs_adjustment');failQueue=true;
 await input('input[aria-label="Search scene objects"]','Target');await page.waitForSelector('.scene-image-queue [role="alert"]');
 assert.equal(await page.$eval('.scene-inspector input',e=>e.value),'Target white','Queue failure must leave the current review usable');
 await page.click('button[aria-label="Retry image queue"]');await nextReady('Queue image A');
 await input('input[aria-label="Search scene objects"]','slow');
 for(let attempt=0;attempt<30&&!heldRequest;attempt++)await new Promise(resolve=>setTimeout(resolve,50));assert(heldRequest,'The slow request must be observed before changing filters');
 await input('input[aria-label="Search scene objects"]','Target');await nextReady('Queue image A');
 await heldRequest.respond({status:200,contentType:'application/json',body:JSON.stringify({scenes:[]})}).catch(()=>{});
 await nextReady('Queue image A');
 await page.setViewport({width:390,height:844});await page.$eval('.scene-review-progress',e=>e.scrollIntoView());await page.screenshot({path:'/tmp/addr-scene-queue-mobile.png'});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 assert.deepEqual(errors,[]);assert.equal((await load(ids[0])).review.revision,1,'A failed/discarded edit must not save to the prior scene');
 console.log('Scene review queue: separate progress, active layer counts, filtered cross-image keyboard navigation, exact-object links/reload, draft and failed-save protection, save/undo counts, empty/error recovery, stale-response protection and mobile passed.');
}catch(error){console.error(await page.$eval('body',e=>e.innerText.slice(-5000)));await page.screenshot({path:'/tmp/addr-scene-queue-failure.png',fullPage:true});throw error;}
finally{await browser.close();for(const id of ids)await rm(`data/scenes/${id}`,{recursive:true,force:true});}
