import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';

const base=process.env.APP_URL||'http://localhost:3008',id=`qa_trace_${Date.now()}`,directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-29790/latest.json','utf8'));
const parent={...source.objects[0],id:'parent',label:'Supporting roof',ring:[[70,70],[300,70],[300,300],[70,300]],holes:[],roof_parts:[],roof_lines:[],roof_annotation:null};
const fragments=Array.from({length:12},(_,i)=>{
 const x=100+i%6*20,y=100+Math.floor(i/6)*20;
 return {id:`fragment-${i}`,label:`Panel fragment ${i+1}`,kind:'solar_panel',ring:[[x,y],[x+12,y],[x+12,y+12],[x,y+12]],
  status:'candidate',geometry_reviewed:false,source:'fixture',parent_id:'parent',group_id:'bank',score:.8,
  model:{...parent.model,height_m:.15,height_verified:false,solar:{tilt_deg:20,azimuth_deg:90}}};
});
const unrelated={...fragments[0],id:'unrelated',label:'Separate array',group_id:undefined,ring:[[250,250],[270,250],[270,270],[250,270]]};
const model={...source,id,objects:[parent,...fragments,unrelated],groups:[{id:'bank',label:'One array'}]};
await mkdir(directory);await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await cp(`data/scenes/roof_training_250-29790/${model.image_sha256}.png`,`${directory}/${model.image_sha256}.png`);
const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
const post=(route,body)=>fetch(`${base}/api/scenes${route}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const review={revision:0,model_signature:model.model_signature,groups:model.groups,reviewed_classes:['solar_panel'],
 objects:model.objects.map(o=>o.kind==='solar_panel'?{...o,status:'confirmed',geometry_reviewed:true,visibility:'full'}:o)};
const setup=await post('',{id,review});assert.equal(setup.status,200,await setup.clone().text());
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,
 args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1400,height:1000});
const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
const input=async(label,value)=>{
 const h=await page.waitForSelector(`input[aria-label="${label}"]`);await h.focus();await h.evaluate(e=>e.select());
 await h.press('Backspace');await h.type(String(value));await h.press('Tab');
};
const outline=[[96,96],[220,96],[220,140],[96,140]];
const numericTrace=async()=>{
 for(const [x,y] of outline){await input('Replacement corner X',x);await input('Replacement corner Y',y);await button('Add outline corner');}
};
const openTrace=async()=>{
 await button('Correct outlines');await button('Trace replacement');await button('Select this group');
 assert.equal(await page.$$eval('.scene-merge-choices input:checked',a=>a.length),11);
 await button('2. Draw replacement outline');
};
const ready=async()=>page.waitForFunction(()=>document.body.textContent.includes('Replacement outlines are ready'));
try {
 // Without an object deep link the filtered card can differ from the stored
 // initial selection. Beginning a trace must pin that visible card.
 await page.goto(`${base}/scenes?scene=${id}&kind=solar_panel&status=all`,{waitUntil:'networkidle2'});
 await openTrace();
 const svg=await page.$('.scene-instance-editor svg');await svg.scrollIntoView();
 for(const point of outline){
  const at=await svg.evaluate((e,p)=>{const q=new DOMPoint(...p).matrixTransform(e.getScreenCTM());return {x:q.x,y:q.y};},point);
  await page.mouse.click(at.x,at.y);
 }
 assert(await page.$('[aria-label="Traced outline with 4 corners"]'));
 for(const name of ['Next scene object','Object layer filter','Object review filter'])
  assert.equal(await page.$eval(`[aria-label="${name}"]`,e=>e.disabled),true,`${name} must preserve the unfinished trace`);
 assert.equal(await page.$eval('button::-p-text(Save scene)',e=>e.disabled),true);
 await page.click('a[href="/scenes/training"]');
 assert(new URL(page.url()).pathname==='/scenes','An unfinished trace must guard header navigation');
 assert.match(await page.$eval('.scene-status-space',e=>e.textContent),/replacement trace/);
 await button('Undo last corner');assert(await page.$('[aria-label="Traced outline with 3 corners"]'));
 await button('Clear drawing');assert.equal(await page.$('[aria-label="Traced outline with 3 corners"]'),null);
 await numericTrace();
 await button('1. Select fragments');
 await page.click('.scene-merge-choices label::-p-text(Separate array) input');
 await button('2. Draw replacement outline');await button('Create replacement draft');
 await page.waitForSelector('.scene-instance-editor [role="alert"]');
 assert.match(await page.$eval('.scene-instance-editor [role="alert"]',e=>e.textContent),/overlap every selected/);
 assert(await page.$('[aria-label="Traced outline with 4 corners"]'),'Failed requests preserve trace corners');
 assert.equal((await load()).review.revision,1);
 await button('1. Select fragments');await page.click('.scene-merge-choices label::-p-text(Separate array) input');
 await button('2. Draw replacement outline');
 await page.setViewport({width:390,height:844});
 await page.screenshot({path:'/tmp/addr-scene-trace-mobile.png',fullPage:true});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Trace controls fit mobile width');
 await button('Create replacement draft');await ready();
 assert.equal((await load()).review.revision,1,'Replacement preview is not a save');
 await button('Undo edit');assert.equal(await page.$eval('button::-p-text(Save scene)',e=>e.disabled),true);
 await page.setViewport({width:1400,height:1000});
 await openTrace();await numericTrace();await button('Create replacement draft');await ready();
 await button('Save scene');await page.waitForFunction(()=>document.body.textContent.includes('Saved revision 2'));
 let data=await load();const result=data.review.objects.find(o=>o.source==='reviewer-trace');
 assert(result);assert.equal(result.derived_from.length,12);assert.equal(result.parent_id,'parent');assert.equal(result.group_id,'bank');
 assert.deepEqual(result.ring,outline);assert.deepEqual(result.holes,[]);assert.equal(result.status,'needs_adjustment');
 assert.equal(result.geometry_reviewed,false);assert.equal(result.model.height_verified,false);assert.equal(result.model.solar,undefined);
 assert.equal(result.visibility,undefined);assert.deepEqual(data.review.reviewed_classes,[]);
 assert(data.review.objects.filter(o=>o.id.startsWith('fragment-')).every(o=>o.status==='superseded'&&o.replacement_ids[0]===result.id));
 assert.equal(data.review.objects.find(o=>o.id==='unrelated').status,'confirmed');
 assert.deepEqual(data.model,JSON.parse(JSON.stringify(model)));
 await page.reload({waitUntil:'networkidle2'});await page.select('select[aria-label="Object review filter"]','superseded');
 await page.waitForFunction(()=>document.body.textContent.includes('This outline was replaced'));
 await button(`View ${result.label}`);await button('3D scene');await page.waitForSelector('.scene-3d-canvas canvas');
 await page.screenshot({path:'/tmp/addr-scene-trace-desktop.png',fullPage:true});
 const payload={id,review:data.review,operation:'trace',ids:[result.id],outline:{ring:outline}};
 assert.equal((await post('/instances',{...payload,review:{...data.review,revision:1}})).status,409);
 assert.equal((await post('/instances',{...payload,ids:['fragment-0']})).status,400);
 assert.equal((await post('/instances',{...payload,outline:{ring:[[-1,0],[20,0],[20,20]]}})).status,400);
 assert.equal((await post('/instances',{...payload,outline:{ring:[[0,0],[20,20],[0,20],[20,0]]}})).status,400);
 assert.equal((await post('/instances',{...payload,ids:[result.id,'parent']})).status,400);
 assert.equal((await post('/instances',{...payload,ids:Array.from({length:65},(_,i)=>`fragment-${i}`)})).status,400);
 await page.reload({waitUntil:'networkidle2'});await button('Correct outlines');await button('Trace replacement');
 await button('2. Draw replacement outline');await input('Replacement corner X',100);await input('Replacement corner Y',100);await button('Add outline corner');
  await button('Cancel trace');assert.equal(await page.$('.scene-instance-editor'),null);
  await page.waitForFunction(()=>!document.querySelector('select[aria-label="Object layer filter"]').disabled);
 assert.equal(await page.$eval('select[aria-label="Object layer filter"]',e=>e.disabled),false,'Cancel releases the navigation guard');
 assert.equal((await load()).review.revision,2,'Cancel must preserve saved feedback');
 assert.deepEqual(errors,[]);
 console.log('Scene trace: 12-fragment selection, pointer/numeric boundary, mobile, draft guards, errors/retry, undo, save/reload, history, unchanged originals, 3D, cancellation and API validation passed.');
}catch(error){console.error(await page.$eval('body',e=>e.innerText.slice(-6000)));await page.screenshot({path:'/tmp/addr-scene-trace-failure.png',fullPage:true});throw error;}
finally{await browser.close();await rm(directory,{recursive:true,force:true});}
