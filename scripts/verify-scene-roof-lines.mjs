// Roof line workspace on a scratch scene (qa_rooflines_*): box selection (both endpoints inside the box), Shift-click
// toggles, one bulk delete = one undo step, Escape cancels a box, a compacted save (every stored vertex referenced), then
// "Edit outline" from the line workspace: the pending line edits follow the edited ring, a line that leaves the roof is
// dropped and reported, the page undo restores ring and line together, and Enter saves ring + re-anchored lines in one
// revision. Wording is measurement-first: the saved lines are the reviewer's lines, the baseline is the model estimate.
// Writes only the scratch scene directory it creates and removes in `finally`; house-25822 and every live scene are never
// touched. Keys are pressed only with focus verified on the line canvas, a vertex handle or the page body.
import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import puppeteer from 'puppeteer-core';

const base=process.env.APP_URL||'http://localhost:3008';
const id=`qa_rooflines_${Date.now()}_${randomBytes(3).toString('hex')}`,directory=`data/scenes/${id}`;
const shots='/tmp/addr-roof-lines';
const sourceDir='data/scenes/roof_training_250-25218';
const source=JSON.parse(await readFile(`${sourceDir}/latest.json`,'utf8'));
// Same operand order as transformPoint, so hip ends on ring corners are bit-identical in metres.
const matrix=source.pixel_to_meters,xyz=(x,y,z)=>[x*matrix[0][0]+y*matrix[1][0]+matrix[2][0],x*matrix[0][1]+y*matrix[1][1]+matrix[2][1],z];
const ring=[[200,200],[500,200],[500,400],[200,400]];
// 0-3 eave corners, 4-5 ridge ends, 6-7 a short valley that touches nothing (connectRoofLines adds no junction).
const pixels=[[200,200,4],[500,200,4],[500,400,4],[200,400,4],[300,300,6],[400,300,6],[230,300,5],[260,330,5]];
const graph={schema_version:1,roof_type:'hipped',vertices:pixels.map(p=>xyz(...p)),faces:[[0,1,5,4],[1,2,5],[2,3,4,5],[3,0,4]],
  // Order matters for the line numbers below: 1 ridge, 2 hip TL, 3 hip BL, 4 hip TR, 5 hip BR, 6 valley.
  edges:[{a:4,b:5,kind:'ridge'},{a:0,b:4,kind:'hip'},{a:3,b:4,kind:'hip'},{a:1,b:5,kind:'hip'},{a:2,b:5,kind:'hip'},{a:6,b:7,kind:'valley'}]};
const building={id:'building',kind:'building',label:'Roof lines QA',ring,holes:[],status:'candidate',geometry_reviewed:false,visibility:'full',source:'test',roof_annotation:graph,
  model:{height_m:4,base_m:1,height_source:'class-prior',height_verified:false,width_m:14,length_m:10,area_m2:140,rotation_deg:0,color:'#b06d48'}};
const model={...source,id,title:'Roof lines QA',groups:[],objects:[building]};
const seededSnapshot=JSON.stringify(model);
await mkdir(directory);await mkdir(shots,{recursive:true});
await writeFile(`${directory}/latest.json`,seededSnapshot);
await cp(`${sourceDir}/${source.image_sha256}.png`,`${directory}/${source.image_sha256}.png`);
const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
const feedback=async()=>(await fetch(`${base}/api/scenes/roof-feedback?id=${id}&object=building`)).json();
let browser,page;
try{
  const seeded=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,objects:[building],groups:[],reviewed_classes:[]}})});
  assert.equal(seeded.status,200,await seeded.clone().text());
  assert.equal((await load()).review.revision,1);
  browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,
    args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  page=await browser.newPage();const errors=[];let posts=0;
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/scenes')posts++;});
  await page.setViewport({width:1440,height:1000});
  page.setDefaultTimeout(60000);  // dev server: pages compile on first visit
  const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
  const named=async label=>(await page.waitForSelector(`button[aria-label="${label}"]`)).click();
  const text=selector=>page.$eval(selector,e=>e.textContent.replace(/\s+/g,' ').trim());
  const count=()=>page.$$eval('.roof-line-image line[role="button"]',l=>l.length);
  const pressed=()=>page.$$eval('.roof-line-image line[role="button"][aria-pressed="true"]',l=>l.length);
  const pending=()=>page.$eval('.scene-decisions',e=>e.getAttribute('data-outline-pending'));
  const imagePoint=(x,y)=>page.$eval('.roof-line-image',(e,p)=>{const v=new DOMPoint(...p).matrixTransform(e.getScreenCTM());return {x:v.x,y:v.y};},[x,y]);
  const lineCenter=async index=>{
    const line=await page.waitForSelector(`.roof-line-image line[aria-label^="Roof line ${index}:"]`);
    return line.evaluate(e=>{const m=e.ownerSVGElement.getScreenCTM(),a=new DOMPoint((Number(e.getAttribute('x1'))+Number(e.getAttribute('x2')))/2,(Number(e.getAttribute('y1'))+Number(e.getAttribute('y2')))/2).matrixTransform(m);return {x:a.x,y:a.y};});
  };
  const shiftClick=async index=>{const p=await lineCenter(index);await page.keyboard.down('Shift');await page.mouse.click(p.x,p.y);await page.keyboard.up('Shift');};
  const boxFrom=async(from,to)=>{
    const a=await imagePoint(...from),b=await imagePoint(...to);
    await page.mouse.move(a.x,a.y);await page.mouse.down();await page.mouse.move((a.x+b.x)/2,(a.y+b.y)/2,{steps:3});
    await page.waitForSelector('[data-testid=roof-line-marquee]');
    return b;
  };
  const saved=async revision=>{
    await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`)||document.querySelector('.scene-status-space [role="alert"]')?.textContent,{},revision);
    const alert=await page.$eval('body',e=>e.querySelector('.scene-status-space [role="alert"]')?.textContent);assert(!alert,alert);
  };
  const nudgeCorner=async()=>{
    // Corner 2 (500,200) five pixels left in ONE change: hip TR's eave end leaves the roof. The X field commits every
    // keystroke, so typing a value would be several outline changes with intermediate rings.
    await page.select('select[aria-label="Select outline point"]','1');
    await page.$eval('.scene-vertex-handle.is-selected',e=>e.focus());
    assert(await page.evaluate(()=>document.activeElement?.classList.contains('scene-vertex-handle')),'focus must be on the vertex handle before a key');
    await page.keyboard.down('Shift');await page.keyboard.press('ArrowLeft');await page.keyboard.up('Shift');
    await page.waitForSelector('.scene-decisions[data-outline-pending="true"]');
    // MetricInput syncs its text in an effect one render after the ring changes: wait for the value rather than read once.
    await page.waitForFunction(()=>Number(document.querySelector('input[aria-label="Outline point X"]')?.value)===495,{timeout:5000})
      .catch(async()=>{throw new Error(`Shift+ArrowLeft on the focused handle should put corner 2 at x=495, got ${await page.$eval('input[aria-label="Outline point X"]',e=>e.value)}`);});
  };
  const newRing=[[200,200],[495,200],[500,400],[200,400]];
  const imageToScreen=(x,y)=>page.$eval('.scene-svg',(e,x,y)=>{const q=new DOMPoint(x,y).matrixTransform(e.getScreenCTM());return {x:q.x,y:q.y};},x,y);
  // Pointer drag of an outline corner through an intermediate position (pattern verify-scene-edit-flow.mjs dragPoint).
  const dragPoint=async(from,via,to)=>{
    await page.$eval('.scene-svg',e=>e.scrollIntoView({block:'center'}));
    const a=await imageToScreen(...from),m=await imageToScreen(...via),b=await imageToScreen(...to);
    await page.mouse.move(a.x,a.y);await page.mouse.down();
    await page.mouse.move(m.x,m.y,{steps:6});await page.mouse.move(b.x,b.y,{steps:6});
    await page.mouse.up();
  };
  const bodyText=()=>page.$eval('body',e=>e.innerText);
  const undoToClean=async()=>{
    await button('Undo edit');
    await page.waitForFunction(()=>document.querySelector('.scene-decisions')?.getAttribute('data-outline-pending')==='false');
  };
  await page.goto(`${base}/scenes`,{waitUntil:'networkidle2',timeout:300000});  // warm-up: first compile
  await page.goto(`${base}/scenes?scene=${id}&kind=building&status=all&object=building`,{waitUntil:'networkidle2',timeout:300000});
  await button('Edit roof lines');await page.waitForSelector('.roof-line-image');
  assert.equal(await count(),6);
  await page.$eval('.roof-line-image',e=>e.scrollIntoView({block:'center'}));
  // Act 1a. Box on empty roof from (190,190) to (310,410): hip TL, hip BL and the valley lie fully inside; the ridge and
  // right hips cross the box or lie outside and are not selected.
  const corner=await boxFrom([190,190],[310,410]);
  await page.mouse.move(corner.x,corner.y,{steps:3});await page.mouse.up();
  await page.waitForFunction(()=>!document.querySelector('[data-testid=roof-line-marquee]'));
  assert.equal(await pressed(),3,'three lines lie fully inside the box');
  assert.equal(await text('[data-testid=roof-line-selection-count]'),'3 lines selected');
  assert.equal(await text('[data-testid=roof-line-delete]'),'Delete 3 lines');
  assert.equal(await page.evaluate(()=>document.activeElement?.dataset.testid),'roof-line-canvas','the canvas takes focus after a box selection, so Delete needs no focused handle');
  await page.keyboard.press('Delete');
  assert.equal(await count(),3,'Delete removes the box selection in one step');
  await named('Undo line edit');assert.equal(await count(),6,'one undo restores every deleted line together');
  await named('Redo line edit');assert.equal(await count(),3,'one redo deletes them together again');
  await named('Undo line edit');assert.equal(await count(),6);
  assert.equal(posts,0,'selection, delete, undo and redo save nothing (the seed was posted outside the browser)');
  // Act 1b. Shift-click toggles one line at a time.
  await shiftClick(1);await shiftClick(5);
  assert.equal(await text('[data-testid=roof-line-selection-count]'),'2 lines selected');
  await shiftClick(5);assert.equal(await pressed(),1,'Shift-click on a selected line removes it');
  await shiftClick(5);assert.equal(await pressed(),2,'Shift-click adds it back');
  // A click on empty roof without moving clears the selection.
  const empty=await imagePoint(350,380);await page.mouse.click(empty.x,empty.y);assert.equal(await pressed(),0);
  // Act 1c. Start a box, then Escape: the rectangle disappears, nothing is selected, nothing is posted.
  await boxFrom([190,190],[310,410]);
  await page.keyboard.press('Escape');
  await page.waitForFunction(()=>!document.querySelector('[data-testid=roof-line-marquee]'));
  await page.mouse.up();
  assert.equal(await page.$('[data-testid=roof-line-marquee]'),null);assert.equal(await pressed(),0);assert.equal(posts,0);
  // Act 1d. Re-select lines 1 (ridge) and 5 (hip BR), delete with the button, save.
  await shiftClick(1);await shiftClick(5);assert.equal(await text('[data-testid=roof-line-delete]'),'Delete 2 lines');
  assert.equal(await page.$eval('[data-testid=roof-line-delete]',e=>e.getAttribute('aria-label')),'Delete selected lines');
  await page.click('[data-testid=roof-line-delete]');assert.equal(await count(),4);
  await button('Save roof line feedback');await saved(2);
  let data=await load();const edits=data.review.objects[0].roof_line_edits;
  assert.equal(edits.edges.length,4);assert.equal(edits.vertices.length,7,'the two deleted lines took their unshared vertices with them');
  assert(edits.vertices.every((_,i)=>edits.edges.some(e=>e.a===i||e.b===i)),'every stored vertex is referenced');
  assert.equal(edits.baseline.edges.length,6,'the model estimate stays as the baseline');assert.equal(edits.checked,false);
  assert.equal(data.review.revision,2);assert.equal(data.review.history.length,1);
  assert.equal(data.review.history[0].revision,1);assert.equal(data.review.history[0].objects[0].roof_line_edits,undefined,'history keeps the seed');
  let handoff=await feedback();
  assert.equal(handoff.objects[0].feedback.changes.removed_or_moved,2);assert.equal(handoff.objects[0].feedback_status,'draft');
  assert.deepEqual(data.model,JSON.parse(seededSnapshot),'the model estimate is untouched');
  assert.equal(await readFile(`${directory}/latest.json`,'utf8'),seededSnapshot,'latest.json is byte-identical');
  // Act 2a. Edit outline from the line workspace.
  await page.click('[data-testid=roof-line-edit-outline]');
  await page.waitForSelector('.scene-vertex-handle');
  assert.equal(await page.$('.scene-line-workspace'),null,'the line workspace closes');
  await page.waitForSelector('button::-p-text(Finish editing)');
  assert.equal(await pending(),'false');
  // Act 2a-i. A vertex drag is ONE change. Corner 3 (500,400) passes (300,250), where hip TR and the valley would lie
  // outside the ring, and lands at (500,380). The canvas previews the ring and commits once on pointer-up, so the pending
  // lines are re-anchored against the final ring only: all four survive, no drop is reported, and one Undo restores the ring.
  const postsBeforeDrags=posts;
  await dragPoint([500,400],[300,250],[500,380]);
  await page.waitForSelector('.scene-decisions[data-outline-pending="true"]');
  assert(!(await bodyText()).includes('fell outside'),'a position the pointer only passes through must not drop a line');
  await button('Edit roof lines');await page.waitForSelector('.roof-line-image');
  assert.equal(await count(),4,'lines that were only passingly outside during the drag survive');
  await button('Back to review');
  await undoToClean();  // one undo entry per drag: a single Undo returns the ring to the saved one
  // Act 2a-ii. A drag that ends with hip TR's eave end outside the ring: corner 2 (500,200) to (420,260). The drop is
  // reported once, on release; the report stays on screen after the pointer moves on; one Undo restores ring and line.
  await button('Edit outline');await page.waitForSelector('.scene-vertex-handle');
  await dragPoint([500,200],[460,230],[420,260]);
  await page.waitForFunction(()=>document.body.innerText.includes('1 roof line fell outside the edited outline and was removed from your pending line edits · Undo restores it.'));
  const idle=await imageToScreen(350,380);await page.mouse.move(idle.x,idle.y,{steps:5});await new Promise(r=>setTimeout(r,400));
  assert((await bodyText()).includes('1 roof line fell outside the edited outline'),'the drop report persists after the pointer is released and moved');
  await button('Edit roof lines');await page.waitForSelector('.roof-line-image');assert.equal(await count(),3,'the drag dropped exactly the line whose end left the roof');
  await button('Back to review');
  await undoToClean();
  await button('Edit roof lines');await page.waitForSelector('.roof-line-image');assert.equal(await count(),4,'one Undo restores the ring and the dropped line together');
  await button('Back to review');
  assert.equal(posts,postsBeforeDrags,'drags, drops and undos save nothing');
  // Act 2a-iii. Corner 2 five pixels left, in one change, for the exact ring the save below asserts.
  await button('Edit outline');await page.waitForSelector('.scene-vertex-handle');
  assert.equal(await pending(),'false');
  await nudgeCorner();
  await page.waitForFunction(()=>document.body.innerText.includes('fell outside the edited outline'));
  const status=await page.$eval('body',e=>e.innerText);
  assert(status.includes('1 roof line fell outside the edited outline and was removed from your pending line edits · Undo restores it.'),status.slice(-800));
  await button('Edit roof lines');await page.waitForSelector('.roof-line-image');
  assert.equal(await count(),3,'the line outside the edited outline is gone; the others stay pending');
  assert(!(await page.$eval('.roof-line-editor',e=>e.innerText)).includes('different outline'),'the pending lines are bound to the edited outline');
  assert((await text('.roof-line-heading-state')).includes('Draft feedback'));
  // Act 2b. Page undo restores the ring and the dropped line together.
  await button('Back to review');
  await button('Undo edit');
  await page.waitForFunction(()=>document.querySelector('.scene-decisions')?.getAttribute('data-outline-pending')==='false');
  await button('Edit roof lines');await page.waitForSelector('.roof-line-image');assert.equal(await count(),4,'undo restores the dropped line with the ring');
  await button('Back to review');
  // Act 2c. Redo the point edit, then Enter from the page body: ring and re-anchored lines are saved in one revision.
  await button('Edit outline');await page.waitForSelector('.scene-vertex-handle');
  await nudgeCorner();
  await page.evaluate(()=>document.activeElement?.blur());
  assert(await page.evaluate(()=>document.activeElement===document.body),'focus must be on the body before a page-level key');
  const before=posts;
  await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.body.innerText.includes('Outline saved as the reference outline.'));
  assert.equal(posts,before+1,'exactly one save');
  data=await load();const object=data.review.objects[0];
  assert.equal(object.status,'confirmed');assert.equal(object.geometry_reviewed,true);
  assert.deepEqual(object.ring,newRing);assert.deepEqual(object.roof_line_edits.outline.ring,newRing,'the lines are bound to the saved outline');
  assert.equal(object.roof_line_edits.edges.length,3);assert.equal(object.roof_line_edits.checked,false,'no human ticked the line attestation');
  assert.equal(data.review.revision,3);assert.equal(data.review.history.length,2);
  const earlier=data.review.history[1];
  assert.equal(earlier.revision,2);assert.deepEqual(earlier.objects[0].ring,ring);
  assert.deepEqual(earlier.objects[0].roof_line_edits.outline.ring,ring);assert.equal(earlier.objects[0].roof_line_edits.edges.length,4,'history keeps the old ring with the old outline and lines');
  assert.equal(await readFile(`${directory}/latest.json`,'utf8'),seededSnapshot,'latest.json is byte-identical');
  handoff=await feedback();
  assert.equal(handoff.revision,3);assert.equal(handoff.objects[0].feedback_status,'draft','saved lines follow the saved outline (not stale-outline)');
  // Mobile width.
  await page.setViewport({width:390,height:844});
  await page.goto(`${base}/scenes?scene=${id}&kind=building&status=all&object=building`,{waitUntil:'networkidle2'});
  await button('Edit roof lines');await page.waitForSelector('.roof-line-image');
  await page.$eval('.roof-line-image',e=>e.scrollIntoView({block:'center'}));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'the line workspace must not overflow mobile width');
  await page.screenshot({path:`${shots}/mobile.png`});
  assert.deepEqual(errors,[]);
  console.log('Roof line workspace: box selection (both endpoints inside), Shift-click toggles, one-step bulk delete/undo/redo, Escape cancels a box, compacted save, Edit outline from the line workspace with re-anchored pending lines and a reported drop, page undo restores ring and line, Enter saves ring and lines in one revision, history intact, model snapshot untouched, mobile width passed.');
}catch(error){
  if(page){console.error(await page.$eval('body',e=>e.innerText.slice(-4500)).catch(()=>''));await page.screenshot({path:`${shots}/failure.png`,fullPage:true}).catch(()=>{});}
  throw error;
}finally{
  await browser?.close();
  await rm(directory,{recursive:true,force:true});
}
