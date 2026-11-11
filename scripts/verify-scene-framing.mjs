import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, cp, rm } from 'node:fs/promises';
import puppeteer from 'puppeteer-core';

const base=process.env.APP_URL||'http://localhost:3008',id=`qa_framing_${Date.now()}`,directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-26317/latest.json','utf8'));
const object=(id,kind,ring)=>({id,kind,label:`Framing ${kind}`,ring,status:'candidate',geometry_reviewed:false,visibility:'full',source:'manual',
  model:{height_m:kind==='road'?.08:1.7,base_m:0,height_source:'class-prior',height_verified:false,width_m:4,length_m:8,area_m2:32,rotation_deg:0,color:'#7e7974'}});
const model={...source,id,title:'Outline framing QA',groups:[],objects:[
  object('road','road',[[45,65],[455,65],[455,140],[45,140]]),
  object('car','car',[[383,382],[413,382],[413,397],[383,397]])]};
await mkdir(directory);await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await cp(`data/scenes/roof_training_250-26317/${model.image_sha256}.png`,`${directory}/${model.image_sha256}.png`);
let browser,page;
try {
  const review={revision:0,model_signature:model.model_signature,groups:[],reviewed_classes:['road','car'],objects:model.objects.map(o=>({...o,status:'confirmed',geometry_reviewed:true,
    ...(o.id==='road'?{ring:[[50,90],[460,90],[460,150],[50,150]]}:{})}))};
  const seed=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review})});
  assert.equal(seed.status,200,await seed.clone().text());
  browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
  page=await browser.newPage();await page.setViewport({width:1280,height:720});
  const errors=[];let posts=0;page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/scenes')posts++;});
  const button=async name=>(await page.waitForSelector(`button::-p-text(${name})`)).click();
  const frame=()=>page.$eval('.scene-svg',e=>e.getAttribute('viewBox'));
  const at=point=>page.$eval('.scene-svg',(svg,p)=>{const q=new DOMPoint(...p).matrixTransform(svg.getScreenCTM());return{x:q.x,y:q.y};},point);
  const center=()=>page.$eval('.scene-image-scroll',el=>{const r=el.getBoundingClientRect(),svg=el.querySelector('svg');const p=new DOMPoint(r.left+el.clientWidth/2,r.top+el.clientHeight/2).matrixTransform(svg.getScreenCTM().inverse());return{x:p.x,y:p.y};});
  const input=async(label,value)=>{const el=await page.waitForSelector(`input[aria-label="${label}"]`);await el.focus();await el.evaluate(e=>e.select());await el.press('Backspace');await el.type(String(value));await el.press('Tab');};
  const field=label=>page.$eval(`input[aria-label="${label}"]`,e=>Number(e.value));
  const saved=revision=>page.waitForFunction(r=>document.body.innerText.includes(`Saved revision ${r}`),{},revision);
  await page.goto(`${base}/scenes?scene=${id}&kind=road&status=all&object=road`,{waitUntil:'networkidle2'});
  await page.waitForFunction(()=>{const f=document.querySelector('.scene-svg')?.viewBox.baseVal;return f&&f.width/f.height>2.7;});
  const originalFrame=await frame();
  const width=await page.$eval('.scene-svg [data-object-id="road"]',e=>e.getBoundingClientRect().width);
  assert(width>550,'A wide road should use the desktop card width, rather than a 264px square');
  assert(await page.$eval('.scene-decisions',e=>e.getBoundingClientRect().bottom<=innerHeight),'Improved framing keeps swipe decisions in the laptop viewport');
  for(const name of ['Prediction','Your edits','Compare']){await button(name);assert.equal(await frame(),originalFrame,'Layer comparison must not move the image');}
  assert.equal(posts,0,'View controls do not save annotations');
  const beforeZoom=await center();await page.click('[aria-label="Zoom image in"]');
  await page.waitForFunction(()=>document.querySelector('.scene-swipe-surface')?.style.width==='150%');
  const afterZoom=await center();assert(Math.hypot(beforeZoom.x-afterZoom.x,beforeZoom.y-afterZoom.y)<1,'Zoom should retain the visible image centre');
  await page.click('[aria-label="Reset image zoom"]');assert.equal(await frame(),originalFrame);
  await button('Whole scene');assert.equal(await frame(),'0 0 512 512');await button('Focus object');assert.equal(await frame(),originalFrame);
  await page.click('[aria-label="Zoom image in"]');await page.select('[aria-label="Object layer filter"]','car');
  await page.waitForSelector('.scene-direction-handle[aria-label="Vehicle direction handle"]');
  const carCenter=await at([398,389.5]);
  assert(await page.$eval('.scene-image-scroll',(el,p)=>{const r=el.getBoundingClientRect();return p.x>r.left&&p.x<r.right&&p.y>r.top&&p.y<r.bottom;},carCenter),'Selection changes centre the new object at the retained zoom');
  await page.select('[aria-label="Object layer filter"]','road');await page.waitForSelector('.scene-svg [data-object-id="road"]');await page.click('[aria-label="Reset image zoom"]');
  await button('Your edits');await button('Edit outline');const editingFrame=await frame();
  const from=await at([50,90]),to=await at([25,60]);
  await page.mouse.move(from.x,from.y);await page.mouse.down();await page.mouse.move(to.x,to.y,{steps:3});
  assert.equal(await frame(),editingFrame,'Outline drag cannot reframe the image under the pointer');
  await page.mouse.up();await page.select('[aria-label="Select outline point"]','0');
  assert(Math.abs(await field('Outline point X')-25)<.2&&Math.abs(await field('Outline point Y')-60)<.2,'Pointer coordinates remain registered in an aspect-fitted crop');
  await input('Outline point X',18);await input('Outline point Y',50);assert.equal(await frame(),editingFrame,'Numeric edits keep the same image frame');
  await page.$eval('[aria-label="Outline point 1"]',e=>e.focus());
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')),'Outline point 1');
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(()=>Number(document.querySelector('[aria-label="Outline point X"]').value)===19);
  assert.equal(await frame(),editingFrame);
  await button('Whole scene');await button('Focus object');assert.notEqual(await frame(),editingFrame,'Explicit refocus can fit the revised outline');
  assert.equal(posts,0,'Point edits remain a draft and cannot trigger a swipe decision');
  await button('Save scene');await saved(2);
  const data=await(await fetch(`${base}/api/scenes?id=${id}`)).json();assert.deepEqual(data.review.objects[0].ring[0],[19,50]);assert.deepEqual(data.model.objects[0].ring,model.objects[0].ring);
  assert.equal(data.review.objects[0].status,'needs_adjustment');assert.equal(data.review.objects[1].status,'confirmed');
  await page.reload({waitUntil:'networkidle2'});await button('Add object');assert.equal(await frame(),'0 0 512 512','New objects always start in the whole registered image');await button('Cancel drawing');
  await page.screenshot({path:'/tmp/addr-scene-framing/desktop.png'});
  await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});await page.waitForSelector('.scene-svg');await button('Jump to review');
  assert(await page.$eval('.scene-decisions',e=>e.getBoundingClientRect().top>=0&&e.getBoundingClientRect().bottom<=innerHeight),'Phone jump still exposes review actions');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await button('Edit outline');await page.$eval('.scene-svg',e=>e.scrollIntoView({block:'center'}));
  const mobileFrame=await frame(),start=await at([19,50]),end=await at([35,68]);
  const client=await page.createCDPSession();
  await client.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:start.x,y:start.y}]});
  await client.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:end.x,y:end.y}]});
  await client.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  assert.equal(await frame(),mobileFrame,'Touch editing keeps the image steady');await page.select('[aria-label="Select outline point"]','0');
  assert(Math.abs(await field('Outline point X')-35)<1&&Math.abs(await field('Outline point Y')-68)<1,'Touch drag maps back to registered image coordinates');
  assert.equal(posts,1,'Touch editing neither saves nor approves');
  await page.screenshot({path:'/tmp/addr-scene-framing/mobile.png'});assert.deepEqual(errors,[]);
  console.log('Scene framing: landscape detail, unclipped selection, aligned comparison, centred zoom, selection navigation, steady mouse/touch/numeric/keyboard edits, whole-image drawing, save/reload, unchanged predictions and laptop/mobile actions passed.');
}catch(error){if(page){await page.screenshot({path:'/tmp/addr-scene-framing/failure.png',fullPage:true});console.error(await page.$eval('body',e=>e.innerText.slice(-2200)));}throw error;}
finally{await browser?.close();await rm(directory,{recursive:true,force:true});}
