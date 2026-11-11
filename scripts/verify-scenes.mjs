import assert from "node:assert/strict";
import puppeteer from "puppeteer-core";
import { readFile, writeFile, mkdir, cp, rm } from "node:fs/promises";
const base = process.env.APP_URL || "http://localhost:3008";
const id = `qa_browser_${Date.now()}`,
  directory = `data/scenes/${id}`;
const model = JSON.parse(
  await readFile("data/scenes/roof_training_250-29790/latest.json", "utf8"),
);
model.id = id;
await mkdir(directory, { recursive: true });
await writeFile(`${directory}/latest.json`, JSON.stringify(model));
await cp(
  `data/scenes/roof_training_250-29790/${model.image_sha256}.png`,
  `${directory}/${model.image_sha256}.png`,
);
const browser = await puppeteer.launch({
  executablePath:
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
  args: [
    "--no-sandbox",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
  ],
});
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.setViewport({ width: 1280, height: 720 });
async function button(text) {
  const button = await page.waitForSelector(`button::-p-text(${text})`);
  await button.click();
}
async function savedRevision(revision) {
  await page.waitForFunction(
    (r) =>
      document.body.textContent.includes(`Saved revision ${r}`) ||
      document.querySelector('.scene-status-space [role="alert"]')?.textContent,
    {},
    revision,
  );
  const failure = await page.$eval(
    "body",
    (e) => e.querySelector('.scene-status-space [role="alert"]')?.textContent,
  );
  assert(!failure, failure);
}
try {
  await page.goto(`${base}/scenes?scene=${id}&kind=car`, {
    waitUntil: "networkidle2",
    timeout: 120000,
  });
  await page.waitForSelector(".scene-svg");
  const carIds=model.objects.filter(o=>o.kind==='car').map(o=>o.id);
  const shownIds=()=>page.$$eval('.scene-svg path[data-object-id]',paths=>paths.map(p=>p.dataset.objectId));
  assert(await page.$eval('.scene-layer-scope input',e=>e.checked),'A class review should start with only that layer visible');
  assert((await shownIds()).every(id=>carIds.includes(id)),'Other classes must not crowd the car review');
  await page.click('input[aria-label="Show Trees"]');
  await page.click('.scene-layer-scope input');
  assert((await shownIds()).some(id=>model.objects.some(o=>o.id===id&&o.kind==='building')),'Leaving isolation restores visible context');
  assert(!(await shownIds()).some(id=>model.objects.some(o=>o.id===id&&o.kind==='tree')),'Leaving isolation preserves the reviewer’s hidden layers');
  await page.click('.scene-layer-scope input');
  assert(await page.$eval('.scene-save-bar .review-primary',e=>e.disabled),'View changes must not create annotation drafts');
  await button('Show all');
  assert.equal(await page.$eval('.scene-layer-scope input',e=>e.checked),false,'Show all exits temporary layer isolation');
  assert(await page.$eval('input[aria-label="Show Trees"]',e=>e.checked));
  await page.click('.scene-layer-scope input');
  await page.click(`.scene-svg path[data-object-id="${carIds[0]}"]`);
  assert.equal(await page.$eval('select[aria-label="Object layer filter"]',e=>e.value),'car','Tapping a same-class outline retains the review scope');
  await page.select('select[aria-label="Object review filter"]','candidate');
  const firstView=await page.evaluate(()=>({image:document.querySelector('.scene-image-scroll').getBoundingClientRect().toJSON(),actions:document.querySelector('.scene-decisions').getBoundingClientRect().toJSON(),height:innerHeight}));
  assert(firstView.image.top<360 && firstView.actions.bottom<=firstView.height,'The image and swipe buttons should fit together on a laptop viewport');
  await page.screenshot({path:'/tmp/addr-review-layout/desktop.png'});
  assert(await page.$eval('.scene-image-scroll',e=>e.scrollHeight<=e.clientHeight+1),'The initial review image must fit rather than hide its lower outline');
  // Real pointer gesture, on empty image space, approves the currently focused car.
  const svg = await page.$(".scene-svg");
  await svg.scrollIntoView();
  const bounds = await svg.boundingBox();
  const x = bounds.x + bounds.width * 0.35,
    y = bounds.y + Math.min(bounds.height * 0.25, 170);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 130, y + 3, { steps: 12 });
  await page.mouse.up();
  await savedRevision(1);
  let data = await (await fetch(`${base}/api/scenes?id=${id}`)).json();
  const car = data.review.objects.find((o) => o.kind === "car");
  assert.equal(car.status, "confirmed");
  assert.equal(car.model.height_verified, false);
  await button("Undo last save");
  await savedRevision(2);
  data = await (await fetch(`${base}/api/scenes?id=${id}`)).json();
  assert.equal(
    data.review.objects.find((o) => o.id === car.id).status,
    "candidate",
  );
  assert.equal(await page.$eval('select[aria-label="Object layer filter"]',e=>e.value),'car','Undoing the final swipe must retain the current layer');
  await button("Edit outline");
  await page.select('select[aria-label="Select outline point"]', "0");
  const xInput = await page.$('input[aria-label="Outline point X"]');
  const original = await xInput.evaluate((e) => e.value);
  const edited = Number(original) + 1.25;
  await xInput.focus();
  await xInput.evaluate((e) => e.select());
  await xInput.press("Backspace");
  await xInput.type(String(edited));
  await xInput.press("Tab");
  assert.equal(
    await xInput.evaluate((e) => e.value),
    String(edited),
    "Numeric entry should replace the selected coordinate and preserve decimals",
  );
  await button("Save scene");
  await savedRevision(3);
  await page.reload({ waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector(".scene-svg");
  data = await (await fetch(`${base}/api/scenes?id=${id}`)).json();
  assert.equal(
    data.review.objects.find((o) => o.id === car.id).ring[0][0],
    edited,
  );
  await page.select('select[aria-label="Object review filter"]', "all");
  const heightInput=await page.$('.scene-dimensions input');
  await heightInput.focus();await heightInput.evaluate(e=>e.select());await heightInput.press('Backspace');await heightInput.type('2.2');await heightInput.press('Tab');
  await page.setRequestInterception(true);
  let failNextSave=true;
  page.on('request',request=>{
    if(failNextSave&&request.method()==='POST'&&request.url()===`${base}/api/scenes`){failNextSave=false;void request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Test interruption. Your draft is still here.'})});}
    else void request.continue();
  });
  await button('Save scene');
  await page.waitForFunction(()=>document.querySelector('.scene-status-space [role="alert"]')?.textContent.includes('Test interruption'));
  assert.equal(await heightInput.evaluate(e=>e.value),'2.2','Failed saves must retain the edited height');
  await button('Save scene');await savedRevision(4);
  data=await (await fetch(`${base}/api/scenes?id=${id}`)).json();
  assert.equal(data.review.objects.find(o=>o.id===car.id).model.height_m,2.2);
  assert.equal(data.review.objects.find(o=>o.id===car.id).model.height_verified,false);
  // A newer detector snapshot must retain useful corrections without carrying approval.
  await mkdir(`${directory}/models`,{recursive:true});
  await writeFile(`${directory}/models/${model.model_signature}.json`,JSON.stringify(model));
  const updated={...model,model_signature:'f'.repeat(64)};
  await writeFile(`${directory}/models/${updated.model_signature}.json`,JSON.stringify(updated));
  await writeFile(`${directory}/latest.json`,JSON.stringify(updated));
  await page.reload({waitUntil:'networkidle2'});
  await button('Preview earlier edits');
  assert(await page.$eval('.review-notice',e=>e.innerText.includes('Carried outlines need another check')));
  assert.equal(await page.$eval('.scene-inspector input',e=>e.value),car.label,'Preview focuses a carried correction');
  await button('Carry edits forward');await savedRevision(5);
  data=await (await fetch(`${base}/api/scenes?id=${id}`)).json();
  const carried=data.review.objects.find(o=>o.id===car.id);
  assert.equal(carried.model.height_m,2.2);
  assert.equal(carried.ring[0][0],edited);
  assert.equal(carried.status,'needs_adjustment');
  assert.equal(carried.geometry_reviewed,false);
  assert.equal(data.stale,false);
  assert.equal(data.review.history.at(-1).model_signature,model.model_signature);
  await button("3D scene");
  await page.waitForSelector(".scene-3d-canvas canvas");
  const dimensions = await page.$eval(".scene-3d-canvas canvas", (e) => ({
    width: e.width,
    height: e.height,
  }));
  assert(dimensions.width > 100 && dimensions.height > 100);
  await page.screenshot({ path: "/tmp/addr-scenes-3d.png" });
  await button("Aerial");
  // Search, empty recovery, native selection, and narrow reflow.
  await page.type(
    'input[aria-label="Search scene objects"]',
    "no-object-matches",
  );
  await page.waitForFunction(() =>
    document.body.textContent.includes("This queue is clear"),
  );
  await page.click('button[aria-label="Clear object search"]');
  await page.select('select[aria-label="Object layer filter"]','car');
  await button('Your edits');
  await page.setViewport({ width: 390, height: 844 });
  await button('Layers and extraction');
  assert(await page.$eval('.scene-layer-list',e=>getComputedStyle(e).display!=='none'));
  await button('Hide layers and extraction');
  await page.click('.scene-layer-scope input');
  assert.equal(await page.$eval('.scene-layer-scope input',e=>e.checked),false,'Phone layer scope remains operable');
  await page.click('.scene-layer-scope input');
  await button('Jump to review');
  assert(await page.$eval('.scene-card',e=>document.activeElement===e),'Jump to review moves keyboard focus with the viewport');
  assert(await page.$eval('.scene-decisions',e=>e.getBoundingClientRect().top>=0&&e.getBoundingClientRect().bottom<=innerHeight),'Phone review should show the swipe buttons after jumping to the card');
  await page.screenshot({ path: "/tmp/addr-scenes-mobile.png" });
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
    "Mobile page must not overflow horizontally",
  );
  assert.deepEqual(errors, []);
  await page.setViewport({width:1500,height:1080});
  await page.select('select[aria-label="Object layer filter"]','solar_panel');
  await page.select('select[aria-label="Object review filter"]','all');
  await page.waitForSelector('.scene-placement-toggle input');
  assert(await page.$eval('.scene-placement-toggle input',e=>e.checked));
  await page.click('.scene-placement-toggle input');
  await button('Save scene');await savedRevision(6);
  await page.reload({waitUntil:'networkidle2'});
  await page.waitForSelector('.scene-placement-toggle input');
  assert.equal(await page.$eval('.scene-placement-toggle input',e=>e.checked),false,'Manual rooftop placement persists after reload');
  await page.click('.scene-placement-toggle input');
  await button('Save scene');await savedRevision(7);
  data=await (await fetch(`${base}/api/scenes?id=${id}`)).json();
  const solar=data.review.objects.find(o=>o.kind==='solar_panel');
  assert.equal(solar.model.placement,'roof');
  assert.equal(solar.model.height_verified,false);
  await page.select('select[aria-label="Object layer filter"]','tree');
  await page.waitForSelector('.scene-tree-controls');
  await page.select('select[aria-label="Crown shape"]','conical');
  const crownInput=await page.$('input[aria-label="Crown base above ground (m)"]');
  await crownInput.focus();await crownInput.evaluate(e=>e.select());await crownInput.press('Backspace');await crownInput.type('1.25');await crownInput.press('Tab');
  await button('Save scene');await savedRevision(8);
  data=await (await fetch(`${base}/api/scenes?id=${id}`)).json();
  const tree=data.review.objects.find(o=>o.kind==='tree');
  assert.equal(tree.model.tree.crown_profile,'conical');
  assert(Math.abs(tree.model.tree.crown_base_ratio*tree.model.height_m-1.25)<1e-6);
  assert.equal(tree.geometry_reviewed,false);
  await page.reload({waitUntil:'networkidle2'});
  assert.equal(await page.$eval('select[aria-label="Crown shape"]',e=>e.value),'conical');
  assert.equal(await page.$eval('input[aria-label="Crown base above ground (m)"]',e=>e.value),'1.25');
  await button('Aerial');
  await page.select('.scene-inspector select','car');
  await button('Compare');
  assert.equal(await page.$$eval(`.scene-svg path[data-object-id="${tree.id}"]`,paths=>paths.length),2,'A class correction must retain its original outline in comparison');
  await button('Discard draft');
  await button('Your edits');
  await button('3D scene');await page.waitForSelector('.scene-3d-canvas canvas');
  assert(await page.$eval('.scene-3d .scene-inline-actions button',e=>e.textContent==='Whole scene'),'A scoped 3D review starts focused on the selected object');
  await button('Whole scene');
  await button('Focus object');
  assert(await page.$eval('.scene-3d .scene-inline-actions button',e=>e.getAttribute('aria-pressed')==='true'&&e.textContent==='Whole scene'));
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:'/tmp/addr-tree-focused.png'});
  await button('Whole scene');
  assert.equal(await page.$eval('.scene-3d .scene-inline-actions button',e=>e.getAttribute('aria-pressed')),'false');
  assert.deepEqual(errors, []);
  console.log(
    "Scene browser: laptop image/actions, class isolation and restored visibility, same-class selection, reclassification comparison, swipe, undo, outline/height edits, save recovery, extraction feedback recovery, rooftop placement, crown edits/reload, 3D object focus, search and mobile jump/layers passed.",
  );
} finally {
  await browser.close();
  await rm(directory, { recursive: true, force: true });
}
