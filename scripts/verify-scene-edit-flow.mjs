// Post-edit save flow in the scene editor, on a scratch scene (qa_editflow_*): after an outline edit the decision bar
// offers "Save outline · Looks right" (Enter) and "Save outline · Still needs work" (Shift+Enter); the save stores the
// reviewer's outline, advances to the next queue object without widening the queue filter, keeps the earlier revision in
// history and leaves the model estimate (latest.json) untouched; vertex handles keep a screen-constant size at 100 % and
// 400 % zoom with no scaled focus ring. Wording is measurement-first: the saved ring is the reviewer's outline, the model
// ring is the estimate it replaced; nothing here judges accuracy.
// Writes only the scratch scene directory it creates and removes in `finally`; keys are pressed only with focus verified on
// a vertex handle, inside a field, or on the body of the scratch scene's page.
import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import puppeteer from 'puppeteer-core';

const base=process.env.APP_URL||'http://localhost:3008';
const id=`qa_editflow_${Date.now()}_${randomBytes(3).toString('hex')}`,directory=`data/scenes/${id}`;
const shots='/tmp/addr-scene-edit-flow';
const sourceDir='data/scenes/roof_training_250-29790';
const source=JSON.parse(await readFile(`${sourceDir}/latest.json`,'utf8'));
const rings={A:[[64,64],[200,64],[200,200],[64,200]],B:[[260,64],[400,64],[400,200],[260,200]],C:[[64,260],[200,260],[200,400],[64,400]]};
const template={...source.objects[0]};
delete template.provenance;delete template.roof_type;
const building=key=>({...template,id:key,label:`Roof ${key}`,ring:rings[key],holes:[],status:'candidate',geometry_reviewed:false,
  roof_annotation:null,roof_parts:[],roof_lines:[],model:{...template.model,height_m:3,base_m:0}});
const model={...source,id,groups:[],objects:['A','B','C'].map(building)};
const seededSnapshot=JSON.stringify(model);
await mkdir(directory);await mkdir(shots,{recursive:true});
await writeFile(`${directory}/latest.json`,seededSnapshot);
await cp(`${sourceDir}/${model.image_sha256}.png`,`${directory}/${model.image_sha256}.png`);
const post=review=>fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review})});
const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
const object=(review,key)=>review.objects.find(o=>o.id===key);
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,
  args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];let posts=0;
page.on('pageerror',e=>errors.push(e.message));
page.on('dialog',d=>void d.accept());
page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/scenes')posts++;});
await page.setViewport({width:1440,height:1000});
const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
const input=async(label,value)=>{
  const handle=await page.waitForSelector(`input[aria-label="${label}"]`);
  await handle.evaluate(e=>{e.focus();e.select();});await handle.press('Backspace');await handle.type(String(value));await handle.press('Tab');
};
const value=selector=>page.$eval(selector,e=>e.value);
const text=selector=>page.$eval(selector,e=>e.textContent.replace(/\s+/g,' ').trim());
const disabled=selector=>page.$eval(selector,e=>e.disabled);
const pending=()=>page.$eval('.scene-decisions',e=>e.getAttribute('data-outline-pending'));
const counter=()=>text('.scene-card-step span');
const filter=()=>value('select[aria-label="Object review filter"]');
const bar=()=>page.$$eval('.scene-decisions button',buttons=>buttons.map(e=>e.textContent.replace(/\s+/g,' ').trim()));
const message=t=>page.waitForFunction(t=>document.body.innerText.includes(t),{timeout:15000},t);
const width=selector=>page.$eval(selector,e=>e.getBoundingClientRect().width);
const options=()=>page.$$eval('select[aria-label="Select outline point"] option',o=>o.length);
const pointX=()=>page.$eval('input[aria-label="Outline point X"]',e=>Number(e.value));
const blurToBody=async()=>{
  await page.evaluate(()=>document.activeElement?.blur());
  assert(await page.evaluate(()=>document.activeElement===document.body),'focus must be on the body before a page-level key');
};
const focusHandle=async(selector='.scene-vertex-handle')=>{
  await page.$eval(selector,e=>e.focus());  // SVG handles are focusable but not HTMLElements, which page.focus() rejects
  assert(await page.evaluate(()=>document.activeElement?.classList.contains('scene-vertex-handle')),'focus must be on a vertex handle before a key');
};
const editPoint=async(index,x)=>{
  await button('Edit outline');await page.waitForSelector('.scene-vertex-handle');
  await page.select('select[aria-label="Select outline point"]',String(index));await input('Outline point X',x);
};
const imageToScreen=(x,y)=>page.$eval('.scene-svg',(e,x,y)=>{const q=new DOMPoint(x,y).matrixTransform(e.getScreenCTM());return {x:q.x,y:q.y};},x,y);
const dragPoint=async(from,to)=>{
  const a=await imageToScreen(...from),b=await imageToScreen(...to);
  await page.mouse.move(a.x,a.y);await page.mouse.down();
  await page.mouse.move((a.x+b.x)/2,(a.y+b.y)/2);await page.mouse.move(b.x,b.y);
  await page.mouse.up();
};
const selectedDot='.scene-vertex-handle.is-selected .scene-vertex-dot',otherDot='.scene-vertex-handle:not(.is-selected) .scene-vertex-dot';
// Enter on a focused <summary> means "open this panel": it must toggle the details and never write a decision.
const summarySelector='details.scene-action-menu > summary';
const enterOnSummary=async label=>{
  await page.$eval(summarySelector,e=>{e.closest('details').open=false;e.focus();});
  assert.equal(await page.evaluate(()=>document.activeElement?.tagName),'SUMMARY',`${label}: focus must be on the summary before Enter`);
  const postsBefore=posts;await page.keyboard.press('Enter');await new Promise(r=>setTimeout(r,800));
  assert.equal(posts,postsBefore,`${label}: Enter on a focused summary must not save a decision`);
  assert.equal(await page.$eval(summarySelector,e=>e.closest('details').open),true,`${label}: Enter on the summary still opens the panel`);
  await page.$eval(summarySelector,e=>{e.closest('details').open=false;e.blur();});
};
try {
  const seeded=await post({revision:0,model_signature:model.model_signature,objects:model.objects,groups:[],reviewed_classes:[]});
  assert.equal(seeded.status,200,await seeded.clone().text());
  assert.equal((await load()).review.revision,1);
  // Default candidate filter on purpose: an edit turns the object needs_adjustment in the draft, and the card must keep it.
  await page.goto(`${base}/scenes?scene=${id}&kind=building&object=A`,{waitUntil:'networkidle2'});
  await page.waitForSelector('.scene-decisions');
  // a. Default bar.
  assert.deepEqual((await bar()).map(t=>t.split(' ')[0]),['Needs','Not','Skip','Looks'],(await bar()).join(' | '));
  assert.equal(await pending(),'false');assert.equal(await counter(),'1 / 3');assert.equal(await filter(),'candidate');
  // b. Edit outline keeps the queue filter.
  await button('Edit outline');await page.waitForSelector('.scene-vertex-handle');
  assert.equal(await filter(),'candidate','Edit outline must not widen the queue filter');assert.equal(await counter(),'1 / 3');
  // c. Move point 1 of A by pointer, then set it exactly through the field; the bar turns into the save decisions.
  await page.$eval('.scene-svg',e=>e.scrollIntoView({block:'center'}));
  await dragPoint(rings.A[0],[66,64]);
  assert.equal(await value('select[aria-label="Select outline point"]'),'0','pointer down selects the dragged point');
  const dragged=await pointX();
  assert(Math.abs(dragged-66)<3,`pointer drag moves point 1 to about x=66 in image pixels (within 3 px: the canvas places the point at the pointer, and the check maps image to screen through a rounded bounding box), got ${dragged}`);
  await input('Outline point X',66);assert.equal(await pointX(),66);await input('Outline point Y',64);  // the drag lands at the pointer; pin both coordinates so later expectations do not depend on the screen mapping
  assert.equal(await pending(),'true');
  assert((await text('[data-testid=scene-looks-right-decision]')).includes('Save outline · Looks right'),await text('[data-testid=scene-looks-right-decision]'));
  assert((await text('[data-testid=scene-needs-edit-decision]')).includes('Still needs work'),await text('[data-testid=scene-needs-edit-decision]'));
  assert.equal(await disabled('button.scene-skip'),true,'Skip must not carry an unsaved outline to a later save');
  assert.equal(await disabled('[aria-label="Next scene object"]'),true);assert.equal(await disabled('[aria-label="Previous scene object"]'),true);
  assert.equal(await counter(),'2 left');assert.equal(await filter(),'candidate');
  assert.equal(await disabled('button::-p-text(Save scene)'),false);
  assert((await text('.scene-decision-note')).includes('not saved yet'),await text('.scene-decision-note'));
  await page.screenshot({path:`${shots}/pending.png`});
  // d. Handles: 10 px selected dot, 7 px unselected, the same at 400 % zoom; no UA focus ring; keys on a focused handle.
  const w1=await width(selectedDot),u1=await width(otherDot);
  assert(Math.abs(w1-10)<=1.5,`selected dot ${w1} px wide at 100 % (expected 10 ± 1.5)`);
  assert(Math.abs(u1-7)<=1.5,`unselected dot ${u1} px wide at 100 % (expected 7 ± 1.5)`);
  for(let i=0;i<6;i++)await page.click('button[aria-label="Zoom image in"]');
  await page.waitForFunction(()=>document.querySelector('button[aria-label="Reset image zoom"]')?.textContent.trim()==='400%');
  await page.waitForFunction((selector,w)=>Math.abs(document.querySelector(selector).getBoundingClientRect().width-w)<=1,{timeout:5000},selectedDot,w1)
    .catch(async()=>{throw new Error(`selected dot ${await width(selectedDot)} px at 400 % vs ${w1} px at 100 %: handles must not scale with zoom`);});
  const w4=await width(selectedDot),u4=await width(otherDot);
  assert(Math.abs(w4-w1)<=1&&Math.abs(u4-u1)<=1,`handle sizes at 400 %: ${w4}/${u4} px vs ${w1}/${u1} px at 100 %`);
  await page.screenshot({path:`${shots}/zoom.png`});
  await focusHandle();
  assert.equal(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle),'none','the UA focus outline scaled with the image; the dot carries the focus stroke');
  await page.keyboard.press('ArrowRight');
  assert.equal(await pointX(),67,'ArrowRight on a focused handle nudges one image pixel');
  const before=await options();
  await button('Insert point');assert.equal(await options(),before+1);
  await focusHandle('.scene-vertex-handle.is-selected');await page.keyboard.press('Backspace');
  assert.equal(await options(),before,'Backspace on the focused handle removes the point');
  await button('Undo edit');await button('Undo edit');
  assert.equal(await options(),before);
  await page.select('select[aria-label="Select outline point"]','0');assert.equal(await pointX(),67);
  assert.equal(await filter(),'candidate','Undo during an outline edit keeps the queue filter');
  assert.equal(await pending(),'true');
  // e. Enter with focus on a handle: Save outline · Looks right, then the next queue object.
  await focusHandle();await page.keyboard.press('Enter');
  await message('Outline saved as the reference outline.');
  let data=await load();const a=object(data.review,'A');
  assert.equal(a.status,'confirmed');assert.equal(a.geometry_reviewed,true);
  assert.deepEqual(a.ring,[[67,64],...rings.A.slice(1)],'the reviewer outline is stored');
  assert.equal(data.review.revision,2);assert.equal(data.review.history.length,1);
  assert.deepEqual(object(data.review.history[0],'A').ring,rings.A,'the earlier revision keeps the seeded ring');
  assert.equal(object(data.review.history[0],'A').status,'candidate');
  assert.deepEqual(data.model.objects.map(o=>o.ring),[rings.A,rings.B,rings.C],'the model estimate is untouched');
  assert.equal(await readFile(`${directory}/latest.json`,'utf8'),seededSnapshot,'latest.json is byte-for-byte the seeded snapshot');
  await page.waitForFunction(()=>location.search.includes('object=B'));
  await page.waitForSelector('button::-p-text(Edit outline)');
  assert.equal(await filter(),'candidate');assert.equal(await counter(),'1 / 2');assert.equal(await pending(),'false');
  assert((await text('[data-testid=scene-looks-right-decision]')).startsWith('Looks right'));
  // e2. Review mode, Enter on a focused summary: the panel opens, B stays candidate, nothing is posted.
  await enterOnSummary('review mode');
  assert.equal(object((await load()).review,'B').status,'candidate');assert.equal((await load()).review.revision,2);
  // f. Shift+Enter from the body: Save outline · Still needs work, object stays open, next queue object.
  await editPoint(0,262);assert.equal(await pending(),'true');
  // f0. Edit mode with a pending outline, Enter on a focused summary: still pending, nothing posted.
  await enterOnSummary('edit mode with a pending outline');
  assert.equal(await pending(),'true');assert.equal((await load()).review.revision,2);
  await blurToBody();
  await page.keyboard.down('Shift');await page.keyboard.press('Enter');await page.keyboard.up('Shift');
  await message('Outline saved; still open for another look.');
  data=await load();const b=object(data.review,'B');
  assert.equal(b.status,'needs_adjustment');assert.equal(b.geometry_reviewed,false);assert.equal(b.ring[0][0],262);
  assert.equal(data.review.revision,3);assert.equal(data.review.history.length,2);
  await page.waitForFunction(()=>location.search.includes('object=C'));
  assert.equal(await counter(),'1 / 1');assert.equal(await filter(),'candidate');
  // g. Escape finishes editing without saving; the bar still offers the save decisions; Discard draft restores.
  await editPoint(0,66);await blurToBody();await page.keyboard.press('Escape');
  await page.waitForSelector('button::-p-text(Edit outline)');
  assert.equal(await pending(),'true','Escape keeps the draft, so the bar still says the outline is unsaved');
  await button('Discard draft');
  await page.waitForFunction(()=>document.querySelector('.scene-decisions')?.getAttribute('data-outline-pending')==='false');
  assert((await text('[data-testid=scene-looks-right-decision]')).startsWith('Looks right'));
  data=await load();assert.equal(object(data.review,'C').status,'candidate');assert.equal(data.review.revision,3);
  // h. Enter is inert without a pending outline.
  await button('Edit outline');await page.waitForSelector('.scene-vertex-handle');await blurToBody();
  const postsBefore=posts;await page.keyboard.press('Enter');await new Promise(r=>setTimeout(r,800));
  assert.equal(posts,postsBefore,'Enter without a pending outline must not save');
  assert.equal(await pending(),'false');
  await page.keyboard.press('Escape');await page.waitForSelector('button::-p-text(Edit outline)');
  assert.equal(await disabled('button::-p-text(Save scene)'),true);
  assert.equal((await load()).review.revision,3);
  // i. Mobile width with a pending edit.
  await editPoint(0,66);assert.equal(await pending(),'true');
  await page.setViewport({width:390,height:844});await page.waitForSelector('.scene-decisions[data-outline-pending="true"]');
  await page.screenshot({path:`${shots}/mobile.png`,fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'the pending decision bar must fit mobile width');
  await button('Discard draft');
  assert.deepEqual(errors,[]);
  console.log('Scene edit flow: filter kept on edit, pointer/field/arrow edits, screen-constant handles at 100 % and 400 %, Enter saves the reviewer outline as the reference and advances, Enter on a focused summary opens the panel and writes nothing (review and edit mode), Shift+Enter saves and keeps open, Escape/Discard leave nothing saved, inert Enter without an edit, history and model snapshot intact, mobile width passed.');
} catch (error) {
  console.error(await page.$eval('body',e=>e.innerText.slice(-4500)).catch(()=>''));
  await page.screenshot({path:`${shots}/failure.png`,fullPage:true}).catch(()=>{});
  throw error;
} finally {
  await browser.close();
  await rm(directory,{recursive:true,force:true});
}
