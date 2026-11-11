import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_groups_${Date.now()}`,directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-29790/latest.json','utf8'));
const item=(id,kind,ring,group_id,parent_id)=>({id,kind,label:id,ring,group_id,parent_id,status:'candidate',geometry_reviewed:false,source:'fixture',
  model:{...source.objects[0].model,height_m:kind==='building'?3:kind==='solar_panel'?.15:kind==='road'?.08:kind==='tree'?7:1.5,base_m:0,height_verified:false}});
const model={...source,id,title:'Group workflow fixture',groups:[{id:'site',label:'Home site'},{id:'garden',label:'Front garden'},{id:'empty',label:'Empty group'}],objects:[
  item('roof','building',[[64,70],[240,70],[240,250],[64,250]],'site'),
  item('panel','solar_panel',[[120,130],[160,130],[160,150],[120,150]],'site','roof'),
  item('tree','tree',[[320,70],[390,70],[390,160],[320,160]],'garden'),
  item('car','car',[[320,280],[380,280],[380,310],[320,310]]),
  item('road','road',[[15,380],[480,380],[480,430],[15,430]],'garden'),
  item('false-tree','tree',[[20,450],[40,450],[40,470],[20,470]],'garden'),
]};
await mkdir(`${directory}/models`,{recursive:true});await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await writeFile(`${directory}/models/${model.model_signature}.json`,JSON.stringify(model));
await cp(`data/scenes/roof_training_250-29790/${model.image_sha256}.png`,`${directory}/${model.image_sha256}.png`);
const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
const post=review=>fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review})});
const initial={revision:0,model_signature:model.model_signature,groups:model.groups,reviewed_classes:['building','solar_panel','tree','car','road'],
  objects:model.objects.map(o=>({...o,status:o.id==='false-tree'?'rejected':'confirmed',geometry_reviewed:o.id!=='false-tree'}))};
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,
  args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1440,height:1000});
const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
const input=async(label,value)=>{const h=await page.waitForSelector(`input[aria-label="${label}"]`);await h.focus();await h.evaluate(e=>e.select());await h.press('Backspace');await h.type(value);};
const count=async n=>page.waitForFunction(n=>document.querySelector('.scene-group-count')?.textContent.includes(`${n} active members`),{},n);
const save=async revision=>{
  await button('Save scene');await page.waitForFunction(r=>document.body.innerText.includes(`Saved revision ${r}`)||document.querySelector('.scene-status-space [role="alert"]')?.textContent,{},revision);
  assert(!await page.$eval('body',e=>e.querySelector('.scene-status-space [role="alert"]')?.textContent));
};
try {
  assert.equal((await post(initial)).status,200);
  await page.goto(`${base}/scenes?scene=${id}&status=all&object=roof`,{waitUntil:'networkidle2'});
  await button('Groups');await page.waitForSelector('.scene-group-editor');await count(2);
  await button('Prediction');
  assert.equal(await page.$eval('[data-member-id="panel"] input',e=>e.disabled),true);
  assert.equal(await page.$eval('input[aria-label="New group name"]',e=>e.disabled),true);
  await button('Your edits');await input('Group name','House and roof equipment');await button('Rename group');
  await page.select('select[aria-label="Inspect group"]','empty');await count(0);
  await button('Group 3D');await page.waitForSelector('.scene-group-empty');await button('Choose objects in image');
  await input('New group name','Southern garden');await button('Create group');
  const created=await page.$eval('select[aria-label="Inspect group"]',e=>e.value);
  await page.select('select[aria-label="Group object class"]','tree');await button('Add 1 listed objects');await count(1);
  await page.select('select[aria-label="Group object class"]','car');
  const checkbox=await page.$('[data-member-id="car"] input');await checkbox.focus();await checkbox.press('Space');await count(2);
  await page.select('select[aria-label="Group object class"]','all');
  await page.click('[data-group-object-id="road"]');await count(3);
  await button('Prediction');await count(0);
  assert.equal(await page.$eval('select[aria-label="Inspect group"]',e=>e.value),created,'A manual group must not switch to an unrelated prediction');
  assert.match(await page.$eval('.scene-group-editor',e=>e.textContent),/has no model group/);
  assert.equal(await page.$eval('[data-member-id="road"] input',e=>e.disabled),true);
  await button('Your edits');await count(3);
  await save(2);
  let data=await load();assert.deepEqual(data.review.reviewed_classes,initial.reviewed_classes);
  for(const before of initial.objects){
    const after=data.review.objects.find(o=>o.id===before.id);
    for(const key of ['status','geometry_reviewed','parent_id','ring','kind'])assert.deepEqual(after[key],before[key],`Grouping preserves ${key}`);
    assert.equal(after.model.height_m,before.model.height_m);
  }
  assert.equal(data.review.objects.find(o=>o.id==='panel').parent_id,'roof');
  assert(data.review.objects.filter(o=>['tree','car','road'].includes(o.id)).every(o=>o.group_id===created));
  assert.deepEqual(data.model,JSON.parse(JSON.stringify(model)));
  await button('Group 3D');await page.waitForSelector('.scene-3d-canvas canvas');
  await button('Compare');assert(await page.$('[aria-label="3D comparison legend"]'),'Group comparison includes original model geometry');
  await button('Your edits');assert(!await page.$('[aria-label="3D comparison legend"]'));await button('Compare');
  assert.equal(await page.$eval('button::-p-text(Show surroundings)',e=>e.getAttribute('aria-pressed')),'true');
  await button('Show surroundings');await button('Isolate group');
  await page.screenshot({path:'/tmp/addr-scene-groups/group-3d.png'});
  await button('Group image');await page.click('[aria-label="Inspect tree · tree"]');
  await page.waitForSelector('.scene-inspector');
  const notes=await page.$('.scene-inspector textarea');await notes.type('Group inspection draft');
  await button('Return to group');await count(3);await button('Undo edit');
  assert.equal((await load()).review.revision,2);
  await input('Search group objects','no-such-object');
  assert.match(await page.$eval('.scene-group-members',e=>e.textContent),/No matching objects/);
  await page.click('button[aria-label="Clear group search"]');assert.equal(await page.$eval('input[aria-label="Search group objects"]',e=>document.activeElement===e),true);
  await page.select('select[aria-label="Group membership filter"]','members');
  assert.equal((await page.$$('[data-member-id]')).length,3);
  await button('Remove group');await button('Undo edit');await count(3);
  await page.setViewport({width:390,height:844});await page.screenshot({path:'/tmp/addr-scene-groups/mobile.png',fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Group tools fit mobile width');
  await page.setViewport({width:1440,height:1000});
  await page.select('select[aria-label="Inspect group"]','garden');await count(0);
  assert.match(await page.$eval('.scene-group-count',e=>e.textContent),/1 inactive history references/);
  await button('Remove group');await save(3);
  data=await load();assert.equal(data.review.objects.find(o=>o.id==='false-tree').group_id,undefined);
  assert.equal(data.review.objects.find(o=>o.id==='false-tree').status,'rejected');
  await page.select('select[aria-label="Inspect group"]',created);await button('Remove group');await save(4);
  await page.reload({waitUntil:'networkidle2'});await button('Groups');
  assert.equal((await page.$$('select[aria-label="Inspect group"] option')).length,2);
  await input('Group name','Final reviewed site');await button('Rename group');
  let failSave=true;await page.setRequestInterception(true);
  page.on('request',request=>{
    if(failSave&&request.method()==='POST'&&request.url()===`${base}/api/scenes`)
      request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Fixture save interruption. Retry preserves the group draft.'})});
    else request.continue();
  });
  await button('Save scene');await page.waitForSelector('.scene-status-space [role="alert"]');
  assert.equal(await page.$eval('input[aria-label="Group name"]',e=>e.value),'Final reviewed site');
  assert.equal((await load()).review.revision,4);
  failSave=false;await save(5);
  data=await load();assert.equal(data.review.groups.find(g=>g.id==='site').label,'Final reviewed site');
  assert.equal((await post({...data.review,revision:4})).status,409);
  const invalid=structuredClone(data.review);invalid.groups[0].label='';assert.equal((await post(invalid)).status,400);
  const updated={...model,model_signature:'a'.repeat(64)};
  await writeFile(`${directory}/models/${updated.model_signature}.json`,JSON.stringify(updated));await writeFile(`${directory}/latest.json`,JSON.stringify(updated));
  data=await load();assert(data.stale&&data.recovery);
  assert(!data.recovery.review.groups.some(g=>g.id==='garden'));assert(!data.recovery.review.objects.some(o=>o.group_id==='garden'));
  assert.equal(data.recovery.review.groups.find(g=>g.id==='site').label,'Final reviewed site');
  assert.equal(data.recovery.review.objects.find(o=>o.id==='panel').parent_id,'roof');
  assert.deepEqual(errors,[]);
  console.log('Scene groups: model/edited separation, image/keyboard/bulk membership, rename/create/remove, preserved approvals/parents, 3D isolation, inspect/return, no-results/clear, mobile, save/reload/undo, failure retry, stale CAS and group recovery passed.');
}catch(error){console.error(await page.$eval('body',e=>e.innerText.slice(-6000)));await page.screenshot({path:'/tmp/addr-scene-groups/failure.png',fullPage:true});throw error;}
finally{await browser.close();await rm(directory,{recursive:true,force:true});}
