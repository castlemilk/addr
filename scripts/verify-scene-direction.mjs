import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, cp, rm } from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_direction_${Date.now()}`,directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-25218/latest.json','utf8'));
const make=(kind,ring,extra)=>({id:kind,kind,label:`Direction QA ${kind}`,ring,status:'candidate',geometry_reviewed:false,visibility:'full',source:'manual',
 model:{height_m:kind==='car'?1.7:.15,base_m:0,height_source:'class-prior',height_verified:false,width_m:4,length_m:8,area_m2:32,rotation_deg:0,color:'#97b8ff',...extra}});
const model={...source,id,title:'Direction handle QA',groups:[],objects:[
 make('car',[[210,240],[302,240],[302,278],[210,278]],{car:{profile:'van',heading_deg:90,cabin_length_ratio:.7,cabin_width_ratio:.8,cabin_offset_ratio:-.1}}),
 make('road',[[110,210],[390,210],[390,290],[110,290]],{surface:{profile:'crowned',heading_deg:60,grade_percent:4,crossfall_percent:2}}),
 make('solar_panel',[[195,200],[285,200],[285,245],[195,245]],{placement:'absolute',solar:{tilt_deg:25,azimuth_deg:120,clearance_m:.12}}),
]};
await mkdir(directory);await writeFile(`${directory}/latest.json`,JSON.stringify(model));await cp(`data/scenes/roof_training_250-25218/${source.image_sha256}.png`,`${directory}/${source.image_sha256}.png`);
let browser,page;
try {
 const seed=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,groups:[],reviewed_classes:['car','road','solar_panel'],objects:model.objects.map(o=>({...o,status:'confirmed',geometry_reviewed:true}))}})});
 assert.equal(seed.status,200,await seed.clone().text());
 browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});page=await browser.newPage();await page.setViewport({width:1280,height:720});
 const errors=[];let posts=0;page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/scenes')posts++;});
 const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
 const field=label=>`input[aria-label="${label}"]`;
 const value=async(label)=>(await page.$eval(field(label),e=>Number(e.value)));
 const expect=async(label,target)=>page.waitForFunction((label,target)=>Math.abs(Number(document.querySelector(`input[aria-label="${label}"]`)?.value)-target)<.011,{},label,target);
 const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
 const saved=async revision=>{await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`),{},revision);};
 const handle=()=>page.$('.scene-direction-handle');
 const geometry=()=>page.$eval('.scene-direction-handle',g=>{
  const svg=g.ownerSVGElement,ctm=svg.getScreenCTM(),circle=g.querySelector('circle'),line=svg.querySelector('g[aria-label$=" degrees"] line');
  const point=(x,y)=>{const p=new DOMPoint(x,y).matrixTransform(ctm);return {x:p.x,y:p.y};};
  const inverse=ctm.inverse();return {end:point(Number(circle.getAttribute('cx')),Number(circle.getAttribute('cy'))),center:point(Number(line.getAttribute('x1')),Number(line.getAttribute('y1'))),inverse:[inverse.a,inverse.b,inverse.c,inverse.d,inverse.e,inverse.f]};
 });
 const expectedHeading=(g,end)=>{
  const pixel=p=>[g.inverse[0]*p.x+g.inverse[2]*p.y+g.inverse[4],g.inverse[1]*p.x+g.inverse[3]*p.y+g.inverse[5]];
  const start=pixel(g.center),finish=pixel(end),dx=finish[0]-start[0],dy=finish[1]-start[1],a=model.pixel_to_meters;
  return (Math.atan2(dx*a[0][0]+dy*a[1][0],dx*a[0][1]+dy*a[1][1])*180/Math.PI+360)%360;
 };
 await page.goto(`${base}/scenes?scene=${id}&kind=car&status=all`,{waitUntil:'networkidle2'});await page.waitForSelector('.scene-direction-handle');
 await expect('Vehicle front bearing',90);
 const hitSize=()=>page.waitForFunction(()=>{const r=document.querySelector('.scene-direction-handle')?.getBoundingClientRect();return r&&Math.abs(r.width-40)<1&&Math.abs(r.height-40)<1;});
 await hitSize();await page.click('button[aria-label="Zoom image in"]');await hitSize();await button('150%');await hitSize();
 let g=await geometry(),end={x:g.center.x-80,y:g.center.y-45};
 assert(await page.evaluate(p=>!!document.elementFromPoint(p.x,p.y)?.closest('.scene-direction-handle'),g.end),'The centre of the visible handle must receive the gesture');
 await page.mouse.move(g.end.x,g.end.y);await page.mouse.down();await page.mouse.move(end.x,end.y,{steps:12});
 assert(Math.abs(Number(await page.$eval('.scene-direction-handle',e=>e.getAttribute('aria-valuenow')))-expectedHeading(g,end))<.02);
 assert.equal(await value('Vehicle front bearing'),90,'Dragging previews locally before committing');assert.equal(posts,0);
 await page.mouse.up();await expect('Vehicle front bearing',expectedHeading(g,end));assert.equal(posts,0,'A direction drag cannot swipe or save');
 await button('Undo edit');await expect('Vehicle front bearing',90);
 assert(await page.$eval('button::-p-text(Undo edit)',e=>e.disabled),'One drag is exactly one undo step');
 g=await geometry();await page.mouse.move(g.end.x,g.end.y);await page.mouse.down();await page.mouse.move(g.center.x+90,g.center.y-40,{steps:5});await page.keyboard.press('Escape');await page.mouse.up();
 await expect('Vehicle front bearing',90);assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled),'Escape discards the preview');
 await page.focus('button[aria-label="Zoom image in"]');await page.keyboard.press('Tab');await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')),'Vehicle direction handle','Tab navigation reaches the handle');await page.keyboard.down('Shift');await page.keyboard.press('ArrowRight');await page.keyboard.up('Shift');await expect('Vehicle front bearing',100);assert.equal(posts,0,'Handle keyboard input cannot trigger a review decision');
 await button('Undo edit');await expect('Vehicle front bearing',90);
 await page.$eval('.scene-direction-handle',e=>e.focus());await page.keyboard.press('Home');await expect('Vehicle front bearing',0);await page.keyboard.press('ArrowLeft');await expect('Vehicle front bearing',359);await page.keyboard.press('ArrowRight');await expect('Vehicle front bearing',0);
 await button('Save scene');await saved(2);await page.reload({waitUntil:'networkidle2'});await expect('Vehicle front bearing',0);
 let data=await load(),car=data.review.objects.find(o=>o.id==='car');assert.equal(car.status,'confirmed');assert(car.geometry_reviewed);assert.deepEqual(data.review.reviewed_classes,['car','road','solar_panel']);assert.deepEqual(car.ring,model.objects[0].ring);assert.equal(car.model.car.cabin_length_ratio,.7);assert.deepEqual(data.model,JSON.parse(JSON.stringify(model)));
 await button('Prediction');assert(!await handle());await expect('Vehicle front bearing',90);await button('Compare');await expect('Vehicle front bearing',0);assert(await handle());
 await page.click('input[aria-label="Show Cars"]');assert(!await handle());await page.click('input[aria-label="Show Cars"]');assert(await handle());
 await button('Edit outline');assert(!await handle());await button('Finish editing');assert(await handle());
 await page.select('select[aria-label="Object visibility"]','truncated');assert(!await handle());await button('Undo edit');assert(await handle());
 await page.select('select[aria-label="Object layer filter"]','road');await page.waitForSelector('.scene-direction-handle[aria-label="Surface direction handle"]');
 await page.$eval('.scene-direction-handle',e=>e.focus());await page.keyboard.press('ArrowRight');await expect('Surface direction',61);await button('Save scene');await saved(3);
 data=await load();assert.deepEqual(data.review.objects.find(o=>o.id==='road').model.surface,{profile:'crowned',heading_deg:61,grade_percent:4,crossfall_percent:2});assert.deepEqual(data.review.reviewed_classes,['car','road','solar_panel']);
 await page.select('select[aria-label="Object layer filter"]','solar_panel');await page.waitForSelector('.scene-direction-handle[aria-label="Solar direction handle"]');
 await page.$eval('.scene-direction-handle',e=>e.focus());await page.keyboard.press('ArrowDown');await expect('Solar downhill direction',119);await button('Save scene');await saved(4);
 data=await load();assert.deepEqual(data.review.objects.find(o=>o.id==='solar_panel').model.solar,{tilt_deg:25,azimuth_deg:119,clearance_m:.12});
 await button('Level bank');assert(!await handle(),'A level bank has no downhill handle');await button('Undo edit');await expect('Solar tilt',25);
 await page.select('select[aria-label="Object layer filter"]','car');await page.waitForSelector('.scene-direction-handle');await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});
 await page.waitForSelector('.scene-direction-handle');await hitSize();await button('Jump to review');g=await geometry();end={x:g.center.x+65,y:g.center.y+35};
 const cdp=await page.createCDPSession();
 const touch=async(type,p)=>cdp.send('Input.dispatchTouchEvent',{type,touchPoints:p?[{x:p.x,y:p.y,id:1}]:[]});
 await touch('touchStart',g.end);await touch('touchMove',end);await touch('touchEnd');await expect('Vehicle front bearing',expectedHeading(g,end));
 assert.equal(posts,3,'A phone drag must not produce an approval request');
 await button('Undo edit');await expect('Vehicle front bearing',0);
 await button('Jump to review');g=await geometry();await touch('touchStart',g.end);await touch('touchMove',{x:g.center.x-65,y:g.center.y+35});await touch('touchCancel');await expect('Vehicle front bearing',0);
 assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled),'Touch cancellation leaves no draft');
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:'/tmp/addr-direction-mobile.png'});
 await page.setViewport({width:1280,height:720,isMobile:false,hasTouch:false});await page.waitForSelector('.scene-direction-handle');await page.$eval('.scene-card',e=>e.scrollIntoView({block:'center'}));await page.$eval('.scene-direction-handle',e=>e.focus());await page.screenshot({path:'/tmp/addr-direction-desktop.png'});
 assert.deepEqual(errors,[]);console.log('Scene directions: geographic pointer bearings, live preview, one-step undo, Escape/touch cancel, keyboard and swipe separation, immutable prediction, partial/hidden/read-only gates, approval preservation, save/reload, road/solar settings and mobile touch passed.');
} catch(error){if(page){console.error(await page.$eval('body',e=>e.innerText.slice(-5500)));await page.screenshot({path:'/tmp/addr-direction-failure.png',fullPage:true});}throw error;}
finally{await browser?.close();await rm(directory,{recursive:true,force:true});}
