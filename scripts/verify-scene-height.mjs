import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, cp, rm } from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_height_${Date.now()}`,directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-25218/latest.json','utf8'));
const make=(kind,height,extra={})=>({id:kind,kind,label:`Height QA ${kind}`,ring:[[210,210],[300,210],[300,290],[210,290]],status:'candidate',geometry_reviewed:false,visibility:'full',source:'manual',
 model:{height_m:height,base_m:1,height_source:'class-prior',height_verified:false,width_m:8,length_m:9,area_m2:72,rotation_deg:0,color:'#8ce2ba',...extra}});
const building=make('building',4),a=source.pixel_to_meters;
const xyz=(x,y,z)=>[x*a[0][0]+y*a[1][0]+a[2][0],x*a[0][1]+y*a[1][1]+a[2][1],z];
building.roof_annotation={schema_version:1,roof_type:'gabled',vertices:[[210,210,4],[300,210,4],[300,290,4],[210,290,4],[255,210,6],[255,290,6]].map(p=>xyz(...p)),faces:[[0,4,5,3],[4,1,2,5]],edges:[{a:4,b:5,kind:'ridge'}]};
const model={...source,id,title:'3D height handle QA',groups:[],objects:[
 make('tree',6,{tree:{crown_profile:'spreading',crown_base_ratio:.3}}),
 make('car',1.7,{car:{profile:'suv',heading_deg:90,cabin_length_ratio:.7,cabin_width_ratio:.8,cabin_offset_ratio:-.1}}),
 make('road',.15,{surface:{profile:'crowned',heading_deg:60,grade_percent:4,crossfall_percent:2}}),
 make('solar_panel',.15,{placement:'absolute',solar:{tilt_deg:25,azimuth_deg:120,clearance_m:.12}}),building,
]};
await mkdir(directory);await writeFile(`${directory}/latest.json`,JSON.stringify(model));await cp(`data/scenes/roof_training_250-25218/${source.image_sha256}.png`,`${directory}/${source.image_sha256}.png`);
let browser,page;
try {
 const classes=model.objects.map(o=>o.kind);
 const seed=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,groups:[],reviewed_classes:classes,objects:model.objects.map(o=>({...o,status:'confirmed',geometry_reviewed:true}))}})});
 assert.equal(seed.status,200,await seed.clone().text());
 browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});page=await browser.newPage();await page.setViewport({width:1280,height:720});
 const errors=[];let posts=0;page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/scenes')posts++;});
 const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
 const expect=(height,label='Height (m)')=>page.waitForFunction((height,label)=>Math.abs(Number(document.querySelector(`input[aria-label="${label}"]`)?.value)-height)<.001,{},height,label);
 const ready=()=>page.waitForFunction(()=>{const h=document.querySelector('.scene-height-handle');return h&&!h.hidden&&h.style.top;});
 const handleValue=()=>page.$eval('.scene-height-handle',e=>Number(e.getAttribute('aria-valuenow')));
 const center=()=>page.$eval('.scene-height-handle',e=>{e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};});
 const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
 const save=async revision=>{await button('Save scene');await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`),{},revision);};
 await page.goto(`${base}/scenes?scene=${id}&kind=tree&status=all`,{waitUntil:'networkidle2'});await button('3D scene');await ready();await expect(6);
 assert.equal(await page.$eval('.scene-height-handle',e=>Math.round(e.getBoundingClientRect().width)),44);
 let p=await center();await page.mouse.move(p.x,p.y);await page.mouse.down();await page.mouse.move(p.x,p.y-30,{steps:8});
 const preview=await handleValue();assert(preview>6);await expect(6);
 assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled));assert(await page.$eval('.scene-camera-controls',e=>e.disabled));
 assert(await page.$eval('button::-p-text(Prediction)',e=>e.disabled),'Layer changes cannot interrupt a preview');assert.equal(posts,0);
 await page.mouse.up();await expect(preview);assert.equal(posts,0,'A height edit cannot save or approve');await button('Undo edit');await expect(6);
 assert(await page.$eval('button::-p-text(Undo edit)',e=>e.disabled),'A drag creates exactly one undo step');
 p=await center();await page.mouse.move(p.x,p.y);await page.mouse.down();await page.mouse.move(p.x,p.y-25,{steps:4});await page.keyboard.press('Escape');await page.mouse.up();await expect(6);
 assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled));
 await page.focus('button::-p-text(Add object)');await page.keyboard.press('Tab');
 assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')),'3D height handle','Tab navigation reaches the 3D slider');
 assert.equal(await page.$eval('.scene-height-handle',e=>getComputedStyle(e).outlineWidth),'2px','Keyboard focus must remain visible');
 await page.keyboard.down('Shift');await page.keyboard.press('ArrowUp');await page.keyboard.up('Shift');await expect(7);
 await save(2);await page.reload({waitUntil:'networkidle2'});await button('3D scene');await ready();await expect(7);
 let data=await load(),tree=data.review.objects.find(o=>o.id==='tree');assert.equal(tree.model.height_source,'reviewer-estimate');assert.equal(tree.model.height_verified,false);assert.equal(tree.model.base_m,1);assert.deepEqual(tree.model.tree,model.objects[0].model.tree);assert.equal(tree.status,'confirmed');assert(tree.geometry_reviewed);assert.deepEqual(data.review.reviewed_classes,classes);assert.deepEqual(data.model,model);
 await button('Prediction');assert(!await page.$('.scene-height-handle'));assert(!await page.$('[aria-label="3D comparison legend"]'));await expect(6);await button('Compare');await ready();await expect(7);
 assert(await page.$('[aria-label="3D comparison legend"]'),'Compare must expose original predictions as well as current surfaces');
 await button('Your edits');assert(!await page.$('[aria-label="3D comparison legend"]'));await expect(7);await button('Compare');await ready();await expect(7);
 await page.click('input[aria-label="Show Trees"]');assert(!await page.$('.scene-height-handle'));await page.click('input[aria-label="Show Trees"]');await ready();
 await page.select('select[aria-label="Object visibility"]','truncated');assert(!await page.$('.scene-height-handle'));await button('Undo edit');await ready();
 await button('Rotate right');await ready();p=await center();await page.mouse.move(p.x,p.y);await page.mouse.down();await page.mouse.move(p.x,p.y-20,{steps:5});assert(await handleValue()>7);await page.mouse.up();await button('Undo edit');await expect(7);
 await page.select('select[aria-label="Object layer filter"]','building');await ready();await page.focus('.scene-height-handle');await page.keyboard.press('ArrowUp');await expect(4.1);await save(3);
 data=await load();const house=data.review.objects.find(o=>o.id==='building');for(let i=0;i<6;i++)assert(Math.abs(house.roof_annotation.vertices[i][2]-building.roof_annotation.vertices[i][2]-.1)<1e-8);assert.equal(house.model.base_m,1);
 assert.equal(Number(await page.$eval('.scene-height-handle',e=>e.getAttribute('aria-valuemax'))),98);
 await page.select('select[aria-label="Object layer filter"]','road');await ready();await page.focus('.scene-height-handle');await page.keyboard.press('ArrowUp');await expect(.16,'Thickness (m)');await button('Undo edit');await expect(.15,'Thickness (m)');
 await page.select('select[aria-label="Object layer filter"]','solar_panel');await ready();await page.focus('.scene-height-handle');await page.keyboard.press('ArrowUp');await expect(.16,'Thickness (m)');await save(4);
 data=await load();assert.deepEqual(data.review.objects.find(o=>o.id==='solar_panel').model.solar,model.objects.find(o=>o.kind==='solar_panel').model.solar);assert.deepEqual(data.review.reviewed_classes,classes);
 await page.select('select[aria-label="Object layer filter"]','car');await ready();await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});await page.waitForSelector('.scene-card');await button('3D scene');await ready();await expect(1.7);
 await button('Compare');await ready();
 assert(await page.$('[aria-label="3D comparison legend"]'),'Mobile comparison must retain both layers');
 assert(await page.$eval('[aria-label="3D comparison legend"]',e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;}),'The mobile legend must stay inside the viewport');
 p=await center();const cdp=await page.createCDPSession(),touch=(type,p)=>cdp.send('Input.dispatchTouchEvent',{type,touchPoints:p?[{x:p.x,y:p.y,id:1}]:[]});
 await touch('touchStart',p);await touch('touchMove',{x:p.x,y:p.y-35});const touchHeight=await handleValue();assert(touchHeight>1.7);await touch('touchEnd');await expect(touchHeight);assert.equal(posts,3);await button('Undo edit');await expect(1.7);
 p=await center();await touch('touchStart',p);await touch('touchMove',{x:p.x,y:p.y-20});await touch('touchCancel');await expect(1.7);assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled));
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:'/tmp/addr-height/mobile.png',fullPage:true});
 await page.setViewport({width:1280,height:720,isMobile:false,hasTouch:false});await page.waitForSelector('.scene-card');await button('3D scene');await ready();await center();await page.screenshot({path:'/tmp/addr-height/desktop.png'});
 assert.deepEqual(errors,[]);console.log('3D heights: mouse/touch preview, one-step undo, cancellation, keyboard, rotated camera, read-only/hidden/partial gates, class approvals, roof-relative apexes, road/solar thickness, unchanged bases/profiles, save/reload and responsive layout passed.');
} catch(error){if(page){console.error(await page.$eval('body',e=>e.innerText.slice(-4500)));await page.screenshot({path:'/tmp/addr-height/failure.png',fullPage:true});}throw error;}
finally{await browser?.close();await rm(directory,{recursive:true,force:true});}
