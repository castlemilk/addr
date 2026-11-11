import assert from 'node:assert/strict';
import {readdir,mkdir,writeFile,cp,rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008';
const existing=new Set(await readdir('data/scenes'));
const fixture=`qa_region_${Date.now()}`,folder=`data/scenes/${fixture}`;
let captured;
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1400,height:1000});
const click=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
try{
  await page.goto(base,{waitUntil:'networkidle2',timeout:120000});
  await page.waitForSelector('.maplibregl-canvas');
  await page.click('button[aria-label="Select a scene area for object review"]');
  const canvas=await page.$('.maplibregl-canvas'),box=await canvas.boundingBox();
  const x=box.x+box.width*.6,y=box.y+box.height*.48;
  await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x+75,y+65,{steps:10});await page.mouse.up();
  await page.waitForFunction(()=>location.pathname==='/scenes/new');
  await page.waitForSelector('input[aria-label="Scene latitude"]');
  const bounds=new URL(page.url()).searchParams.get('bounds').split(',').map(Number);assert.equal(bounds.length,4);assert(bounds[0]<bounds[2]&&bounds[1]<bounds[3]);
  const center=await page.$eval('input[aria-label="Scene latitude"]',e=>Number(e.value));assert(Math.abs(center-(bounds[1]+bounds[3])/2)<1e-6);
  await page.screenshot({path:'/tmp/addr-region-form.png'});
  // An interrupted capture keeps the chosen patch and unlocks the same controls.
  await page.setRequestInterception(true);
  let heldRequest;
  const pendingRequest=new Promise(resolve=>{heldRequest=resolve;});
  let interrupt=true;
  page.on('request',request=>{
    if(interrupt&&request.method()==='POST'&&request.url()===`${base}/api/scenes/extract`){interrupt=false;heldRequest(request);}
    else void request.continue();
  });
  await click('Capture for manual annotation');
  const request=await pendingRequest;
  assert(await page.$eval('fieldset',e=>e.disabled),'Prevent duplicate captures while pending');
  await request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Test capture interruption. Retry your selected area.'})});
  await page.waitForFunction(()=>document.querySelector('[role="alert"]')?.textContent.includes('Test capture interruption'));
  assert.equal(await page.$eval('input[aria-label="Scene latitude"]',e=>Number(e.value)),center);
  assert.equal(await page.$eval('fieldset',e=>e.disabled),false);
  await click('Capture for manual annotation');
  await page.waitForFunction(()=>location.pathname==='/scenes'&&location.search.includes('scene=map-'),{timeout:180000});
  captured=new URL(page.url()).searchParams.get('scene');
  const data=await (await fetch(`${base}/api/scenes?id=${captured}`)).json();assert.equal(data.model.address_id,null);assert(data.model.region);assert.equal(data.model.objects.length,0);
  // Keep all subsequent model/annotation writes in an isolated review fixture.
  await mkdir(folder);const model={...data.model,id:fixture};await writeFile(`${folder}/latest.json`,JSON.stringify(model));
  await cp(`data/scenes/${captured}/${model.image_sha256}.png`,`${folder}/${model.image_sha256}.png`);
  await page.goto(`${base}/scenes?scene=${fixture}`,{waitUntil:'networkidle2'});
  await page.waitForSelector('.scene-svg');await page.click('#scene-extraction summary');
  await click('Find objects');
  await page.waitForFunction(()=>document.body.textContent.includes('Object proposals are ready for review.')||document.querySelector('.scene-status-space [role="alert"]')?.textContent,{timeout:240000});
  const error=await page.$eval('body',e=>e.querySelector('.scene-status-space [role="alert"]')?.textContent);assert(!error,error);
  const extracted=await (await fetch(`${base}/api/scenes?id=${fixture}`)).json();assert(extracted.model.objects.length>0);assert(extracted.model.objects.every(o=>o.status==='candidate'&&!o.geometry_reviewed));
  await click('Whole scene');
  await (await page.$('.scene-svg')).scrollIntoView();
  const tap=await page.$$eval('.scene-svg [data-object-id]',polygons=>{
    // Hit-test visible polygons so overlapping proposals cannot redirect the tap.
    for(const polygon of polygons.slice(1)){
      const box=polygon.getBoundingClientRect();
      for(let x=box.left+3;x<box.right;x+=7)for(let y=box.top+3;y<box.bottom;y+=7)
        if(document.elementFromPoint(x,y)===polygon)return {x,y,id:polygon.dataset.objectId};
    }
  });
  assert(tap,'An outline must be tappable');await page.mouse.click(tap.x,tap.y);
  const label=extracted.model.objects.find(o=>o.id===tap.id).label;
  await page.waitForFunction(expected=>document.querySelector('.scene-inspector input')?.value===expected,{},label);
  await click('Add object');
  assert.equal(await page.$eval('.scene-svg',e=>e.getAttribute('viewBox')),'0 0 512 512','A missed object can be drawn anywhere in the image');
  const points=await page.$eval('.scene-svg',svg=>[[100,100],[140,100],[140,140],[100,140]].map(([x,y])=>{const p=new DOMPoint(x,y).matrixTransform(svg.getScreenCTM());return {x:p.x,y:p.y};}));
  for(const point of points)await page.mouse.click(point.x,point.y);
  await click('Finish outline');await click('Save scene');
  await page.waitForFunction(()=>document.body.textContent.includes('Saved revision 1')||document.querySelector('.scene-status-space [role="alert"]')?.textContent);
  const saved=await (await fetch(`${base}/api/scenes?id=${fixture}`)).json();assert.equal(saved.review?.objects.length,extracted.model.objects.length+1);
  assert.equal(saved.review.objects.at(-1).source,'reviewer');
  assert.deepEqual(errors,[]);
  console.log(`Map capture: rectangle selection, coordinate form, real aerial capture, ${extracted.model.objects.length} live object proposals, manual annotation and persistence passed.`);
}catch(error){
  console.error(await page.$eval('body',e=>e.innerText.slice(-5000)));
  await page.screenshot({path:'/tmp/addr-region-failure.png',fullPage:true});
  throw error;
}finally{
  await browser.close();await rm(folder,{recursive:true,force:true});
  if(captured&&!existing.has(captured))await rm(`data/scenes/${captured}`,{recursive:true,force:true});
}
