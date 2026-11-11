import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';
// Editor imagery views on the 27 Cambridge cluster tile: the Imagery select lists the original, the sharp contrast
// view and the pair stack's aligned date + composite; choosing a view swaps only the canvas image href (outlines
// stay put) and the contrast view loads within 5 s. Read-only: nothing is saved and no scene file is written.
const base=process.env.APP_URL||'http://localhost:3008';
const scene=process.env.SCENE_ID||'mooroolbark-50011498-17-118442-80417',object=process.env.OBJECT_ID||'44c724fd3480c167';
const expectedDate=process.env.ALIGNED_LABEL||'Aligned 2025-11-06 (Vantor)';
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1400,height:1000});
const data=await (await fetch(`${base}/api/scenes?id=${scene}`)).json();
assert(data.model,`scene ${scene} must exist`);
const revisionBefore=data.review?.revision??null;
const stacks=await (await fetch(`${base}/api/scenes/stacks?scene=${scene}`)).json();
const usable=stacks.stacks.find(s=>s.status==='pair'||s.status==='available');
assert(usable,`a pair/available stack for the current saved image is expected for ${scene}`);
const href=()=>page.$eval('.scene-svg image',e=>e.getAttribute('href'));
// Object outlines are SVG paths (scenePathData) carrying data-object-id; parcels and markers are excluded.
const outlineBox=()=>page.$$eval('.scene-svg path[data-object-id]',items=>items.slice(0,40).map(p=>p.getAttribute('d')));
const options=()=>page.$$eval('select[aria-label="Imagery"] option',items=>items.map(o=>o.textContent));
try{
  await page.goto(`${base}/scenes?scene=${scene}&object=${object}&status=all`,{waitUntil:'networkidle2'});
  await page.waitForSelector('select[aria-label="Imagery"]',{timeout:30000});
  const original=await href();
  assert(original.includes(`version=${data.model.image_sha256}`)&&!original.includes('view='),'The canvas starts on the original saved image');
  const listed=await options();
  assert.equal(listed[0],'Original imagery');assert.equal(listed[1],'Enhanced contrast');
  assert(listed.includes(expectedDate),`Imagery lists ${expectedDate}: ${listed.join(' | ')}`);
  assert(listed.includes('Two-view composite')||listed.includes('Multi-date median composite'),`Imagery lists a composite: ${listed.join(' | ')}`);
  const boxBefore=await outlineBox();
  assert(boxBefore.length>0,'outlines are drawn');
  // Enhanced contrast: swaps the href to view=roof-detail-v1 and loads within 5 s (the old Python path timed out at 10 s).
  const started=Date.now();
  await page.select('select[aria-label="Imagery"]','roof-detail-v1');
  await page.waitForFunction(()=>document.querySelector('.scene-svg image')?.getAttribute('href')?.includes('view=roof-detail-v1'),{timeout:5000});
  const contrastMs=Date.now()-started;
  const contrast=await href();
  assert(contrast.endsWith('&view=roof-detail-v1'),contrast);
  const contrastResponse=await fetch(`${base}${contrast}`);
  assert.equal(contrastResponse.status,200);
  const bytes=Buffer.from(await contrastResponse.arrayBuffer());
  assert.equal(bytes.readUInt32BE(16),data.model.image_width);assert.equal(bytes.readUInt32BE(20),data.model.image_height);
  assert(await page.$eval('body',e=>e.innerText.includes('Display view for inspection; roof lines and saves still use the original image')),'the display-view note is shown');
  assert.deepEqual(await outlineBox(),boxBefore,'outlines stay in place under the contrast view');
  assert(await page.$('button::-p-text(Show original imagery)'),'the shortcut button reflects the contrast view');
  // Aligned date: view=multidate-aligned&stack=&index=.
  const alignedValue=await page.$$eval('select[aria-label="Imagery"] option',(items,label)=>items.find(o=>o.textContent===label)?.value,expectedDate);
  await page.select('select[aria-label="Imagery"]',alignedValue);
  await page.waitForFunction(()=>document.querySelector('.scene-svg image')?.getAttribute('href')?.includes('view=multidate-aligned'),{timeout:10000});
  const aligned=await href();
  assert(aligned.includes(`view=multidate-aligned&stack=${usable.stack_id}&index=`),aligned);
  const alignedResponse=await fetch(`${base}${aligned}`);
  assert.equal(alignedResponse.status,200);assert.equal(alignedResponse.headers.get('x-image-role'),'display-view');
  assert.deepEqual(await outlineBox(),boxBefore,'outlines stay in place under the aligned date');
  // Composite, then back to the original.
  const compositeValue=await page.$$eval('select[aria-label="Imagery"] option',items=>items.find(o=>/composite/i.test(o.textContent))?.value);
  await page.select('select[aria-label="Imagery"]',compositeValue);
  await page.waitForFunction(()=>document.querySelector('.scene-svg image')?.getAttribute('href')?.includes('view=multidate-composite'),{timeout:10000});
  assert.deepEqual(await outlineBox(),boxBefore,'outlines stay in place under the composite');
  await page.select('select[aria-label="Imagery"]','original');
  await page.waitForFunction(o=>document.querySelector('.scene-svg image')?.getAttribute('href')===o,{timeout:5000},original);
  assert(await page.$('button::-p-text(Enhance roof detail)'),'the shortcut button is back to Enhance roof detail');
  const after=await (await fetch(`${base}/api/scenes?id=${scene}`)).json();
  assert.equal(after.review?.revision??null,revisionBefore,'changing imagery views never saves');
  assert.deepEqual(errors,[]);
  await page.screenshot({path:'/tmp/addr-scene-imagery.png'});
  console.log(`Scene imagery: select lists ${listed.length} views (${listed.join(' | ')}); contrast view swapped in ${contrastMs} ms; aligned/composite hrefs bound to stack ${usable.stack_id}; outlines unchanged; no save.`);
}catch(error){console.error(await page.$eval('body',e=>e.innerText.slice(-3000)).catch(()=>''));await page.screenshot({path:'/tmp/addr-scene-imagery-failure.png',fullPage:true}).catch(()=>{});throw error;}
finally{await browser.close();}
