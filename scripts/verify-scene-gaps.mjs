import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';

const base=process.env.APP_URL||'http://localhost:3008';
const id=`qa_gaps_${Date.now()}`,directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-29790/latest.json','utf8'));
const ring=[[64,64],[448,64],[448,448],[64,448]],gap=[[200,200],[312,200],[312,312],[200,312]];
const object={...source.objects[0],id:'courtyard-roof',label:'Courtyard roof',ring,holes:[gap],status:'candidate',geometry_reviewed:false,
  roof_annotation:null,roof_parts:[],roof_lines:[],model:{...source.objects[0].model,height_m:3,base_m:0}};
const model={...source,id,groups:[],objects:[object,{...object,id:'second-roof',label:'Second roof',ring:[[460,20],[500,20],[500,50],[460,50]],holes:[]}]};
await mkdir(directory);
await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await cp(`data/scenes/roof_training_250-29790/${model.image_sha256}.png`,`${directory}/${model.image_sha256}.png`);
const post=review=>fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review})});
const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,
  args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1440,height:1000});
const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
const input=async(label,value)=>{
  const handle=await page.waitForSelector(`input[aria-label="${label}"]`);
  await handle.focus();await handle.evaluate(e=>e.select());await handle.press('Backspace');await handle.type(String(value));await handle.press('Tab');
};
const saved=async revision=>{
  await button('Save scene');
  await page.waitForFunction(r=>document.body.innerText.includes(`Saved revision ${r}`)||document.querySelector('.scene-status-space [role="alert"]')?.textContent,{},revision);
  assert(!await page.$eval('body',e=>e.querySelector('.scene-status-space [role="alert"]')?.textContent));
};
try {
  const response=await post({revision:0,model_signature:model.model_signature,objects:model.objects.map(o=>({...o,status:'confirmed',geometry_reviewed:true})),groups:[],reviewed_classes:['building']});
  assert.equal(response.status,200,await response.clone().text());
  await page.goto(`${base}/scenes?scene=${id}&status=all&object=courtyard-roof`,{waitUntil:'networkidle2'});
  await page.waitForSelector('.scene-svg [data-object-id="courtyard-roof"]');
  assert.equal(await page.$eval('.scene-svg [data-object-id="courtyard-roof"]',e=>e.getAttribute('fill-rule')),'evenodd');
  await button('Edit outline');await page.select('select[aria-label="Outline boundary"]','0');
  await page.select('select[aria-label="Select outline point"]','0');await input('Outline point X','190.25');
  await saved(2);
  let data=await load(),roof=data.review.objects[0];
  assert.equal(roof.holes[0][0][0],190.25);assert.equal(roof.status,'needs_adjustment');assert(!roof.geometry_reviewed);
  assert.deepEqual(data.review.reviewed_classes,[]);assert.deepEqual(data.model.objects[0].holes,[gap]);
  await page.reload({waitUntil:'networkidle2'});await button('Edit outline');await page.select('select[aria-label="Outline boundary"]','0');
  await button('Fill selected gap');
  assert.equal(await page.$eval('.scene-svg [data-object-id="courtyard-roof"]',e=>e.getAttribute('d').match(/M /g).length),1);
  await button('Undo edit');
  assert.equal(await page.$eval('.scene-svg [data-object-id="courtyard-roof"]',e=>e.getAttribute('d').match(/M /g).length),2);
  await page.select('select[aria-label="Outline boundary"]','0');await button('Fill selected gap');await saved(3);
  assert.deepEqual((await load()).review.objects[0].holes,[]);
  await button('Add interior gap');
  for(const label of ['Next scene object','Object layer filter','Object review filter'])
    assert.equal(await page.$eval(`[aria-label="${label}"]`,e=>e.disabled),true,`${label} must retain the unfinished drawing`);
  assert.equal(await page.$eval('button::-p-text(Save scene)',e=>e.disabled),true);
  for(const [x,y] of [[20,20],[100,20],[100,100],[20,100]]){
    await input('New gap corner X',x);await input('New gap corner Y',y);await button('Add gap corner');
  }
  await button('Finish interior gap');
  assert.match(await page.$eval('.scene-inline-actions [role="alert"]',e=>e.textContent),/inside the outer outline/);
  assert.equal((await load()).review.revision,3,'An invalid draft does not write');
  await button('Clear drawing');
  // Draw in actual image coordinates while another object exists beneath the cursor.
  const svg=await page.$('.scene-svg');await svg.scrollIntoView();
  for(const p of gap){
    const at=await svg.evaluate((e,p)=>{const q=new DOMPoint(...p).matrixTransform(e.getScreenCTM());return {x:q.x,y:q.y};},p);
    await page.mouse.click(at.x,at.y);
  }
  await button('Finish interior gap');await saved(4);
  data=await load();roof=data.review.objects[0];
  assert.equal(roof.holes.length,1);
  for(let i=0;i<4;i++)for(let j=0;j<2;j++)assert(Math.abs(roof.holes[0][i][j]-gap[i][j])<.6);
  assert(!roof.roof_parts?.length&&!roof.roof_annotation,'Changing a gap invalidates retained roof planes');
  // Roof proposal and apex editor must also respect the courtyard.
  const proposalResponse=await fetch(`${base}/api/scenes/roof`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    id,revision:4,model_signature:model.model_signature,object:roof,form:'hipped',rise:2,
  })});
  assert.equal(proposalResponse.status,200,await proposalResponse.clone().text());
  const {proposal}=await proposalResponse.json();assert(proposal.audit.passed);
  const review=structuredClone(data.review);
  Object.assign(review.objects[0],{roof_annotation:proposal.annotation,roof_parts:proposal.parts,roof_lines:proposal.lines,roof_type:proposal.roof_type});
  const saveRoof=await post(review);assert.equal(saveRoof.status,200,await saveRoof.clone().text());
  await page.reload({waitUntil:'networkidle2'});await button('Edit roof apexes');
  await page.waitForSelector('[aria-label="Interior gap 1"]');
  await button('3D scene');await page.waitForSelector('.scene-3d-canvas canvas');
  await page.screenshot({path:'/tmp/addr-scene-gaps/3d.png'});
  await button('Aerial');await button('Edit outline');await button('Add interior gap');await button('Cancel gap');
  assert.equal(await page.$eval('button::-p-text(Finish editing)',e=>e.disabled),false);
  await page.setViewport({width:390,height:844});
  await page.screenshot({path:'/tmp/addr-scene-gaps/mobile.png',fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Gap controls must fit mobile width');
  assert.deepEqual(errors,[]);
  console.log('Scene gaps: compound outlines, numeric and pointer edits, validation, approval reset, fill/undo, save/reload/history, draft navigation guards, courtyard roof proposal/apex overlay, 3D and mobile passed.');
}catch(error){
  console.error(await page.$eval('body',e=>e.innerText.slice(-4500)));await page.screenshot({path:'/tmp/addr-scene-gaps/failure.png',fullPage:true});throw error;
}finally{await browser.close();await rm(directory,{recursive:true,force:true});}
