// Property boundaries must render for unit sites: several lot outlines and their lot labels on the
// row of units flagged by the user, and an honest "stacked lots" summary where Vicmap has no unit cuts.
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008';
const minParcels=Number(process.env.PARCEL_MIN||5), minLabels=Number(process.env.PARCEL_LABEL_MIN||3);
const unitRow={scene:'mooroolbark-50011498-17-118443-80417',object:'e13e7a2dc92197ab'};
const stacked={scene:'mooroolbark-50011498-17-118443-80413',object:'324d3af96371f418'};
// The flagged objects are needs_adjustment/candidate; status=all keeps them selectable from a link.
const url=({scene,object})=>`${base}/scenes?scene=${scene}&object=${object}&status=all`;
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1400,height:1000});
const overlay=()=>page.evaluate(()=>{
  const svg=document.querySelector('.scene-svg')?.getBoundingClientRect();
  const onScreen=el=>{const b=el.getBoundingClientRect();return !!svg&&b.width+b.height>0&&b.right>svg.left&&b.left<svg.right&&b.bottom>svg.top&&b.top<svg.bottom;};
  const paths=[...document.querySelectorAll('.scene-svg path.scene-parcel')], labels=[...document.querySelectorAll('.scene-svg text.scene-parcel-label')];
  return {url:location.href,checked:document.querySelector('.scene-parcel-controls input[type=checkbox]')?.checked,
    status:document.querySelector('.scene-parcel-controls [role=status]')?.textContent||'',
    paths:paths.length,visiblePaths:paths.filter(onScreen).length,highlighted:paths.filter(p=>p.dataset.parcelHighlighted==='true').length,
    labels:labels.filter(onScreen).map(t=>t.textContent),dashed:paths.every(p=>p.getAttribute('stroke-dasharray'))};
});
const open=async target=>{
  await page.goto(url(target),{waitUntil:'networkidle2',timeout:120000});
  await page.waitForSelector('.scene-svg',{timeout:60000});
  await page.waitForFunction(id=>location.href.includes(id),{timeout:30000},target.object);
  await page.waitForFunction(()=>document.querySelectorAll('.scene-svg path.scene-parcel').length>0&&!/Loading/.test(document.querySelector('.scene-parcel-controls [role=status]')?.textContent||''),{timeout:60000});
  await new Promise(resolve=>setTimeout(resolve,500));
  return overlay();
};
try{
  const row=await open(unitRow);
  assert.equal(row.checked,true,'Property boundaries are on by default');
  assert(row.dashed,'Lot outlines are dashed land boundaries, distinct from roof outlines');
  assert(row.visiblePaths>=minParcels,`At least ${minParcels} parcel outlines render on the unit row (got ${row.visiblePaths} of ${row.paths})`);
  assert(row.highlighted>=minParcels,`Lots under the outline are highlighted (got ${row.highlighted})`);
  assert.match(row.status,/^\d+ lots touch this outline · PS735592$/,`Status names the lots under the outline: ${row.status}`);
  const lotLabels=row.labels.filter(t=>/^Lot \d+/.test(t));
  assert(lotLabels.length>=minLabels,`At least ${minLabels} lot labels are visible (got ${row.labels.length}: ${row.labels.join('; ')})`);
  // Only lots sharing at least 2 % of the outline are listed; the status count and the visible lot labels must agree.
  const touching=Number(row.status.match(/^(\d+) lots/)[1]);
  assert(lotLabels.length<=touching&&touching>=minParcels,`Labelled lots (${lotLabels.length}) never exceed the ${touching} lots the status reports`);
  assert(row.labels.every(t=>/^(Lot \S+|Common property \S+|Lots .* \(stacked\))$/.test(t)),`Labels are lot numbers, common property or a stacked-lot summary: ${row.labels.join('; ')}`);
  await page.screenshot({path:'/tmp/addr-scene-parcels-units.png'});
  // Toggling the checkbox removes and restores the overlay without touching the review state.
  await page.click('.scene-parcel-controls input[type=checkbox]');
  await page.waitForFunction(()=>document.querySelectorAll('.scene-svg path.scene-parcel').length===0);
  await page.click('.scene-parcel-controls input[type=checkbox]');
  await page.waitForFunction(n=>document.querySelectorAll('.scene-svg path.scene-parcel').length>=n,{},minParcels);

  const strata=await open(stacked);
  assert.match(strata.status,/^5 stacked lots share one boundary · PS907511 · no unit cuts in Vicmap$/,`Stacked strata lots are reported honestly: ${strata.status}`);
  assert(strata.visiblePaths>=1,'The shared site boundary renders once');
  assert.equal(strata.highlighted,1,'Five coincident lots draw as one highlighted polygon');
  assert(strata.labels.includes('Lots 1, 2, 3, 4 + CM1 (stacked)'),`Stacked lots share one label: ${strata.labels.join('; ')}`);
  await page.screenshot({path:'/tmp/addr-scene-parcels-stacked.png'});
  assert.deepEqual(errors,[]);
  console.log(`Scene parcels: ${row.visiblePaths} lot outlines and ${lotLabels.length} lot labels on the unit row (${row.status}); stacked strata site reported as one boundary (${strata.status}). Passed.`);
}catch(error){
  await page.screenshot({path:'/tmp/addr-scene-parcels-failure.png',fullPage:true});
  console.error(await page.evaluate(()=>document.body.innerText.slice(0,3000)));
  throw error;
}finally{await browser.close();}
