// Frame-cut hint and no ghost outlines after a context expansion (read-only): on house-25822's edited layer a carried
// seed flagged clipped_by_frame shows the frame-cut note; the two tree ids the reviewer reported as cut no longer exist;
// nothing is clicked that writes. Usage: APP_URL=http://localhost:3008 node scripts/verify-frame-clip.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import puppeteer from 'puppeteer-core';
const base = process.env.APP_URL || 'http://localhost:3008', scene = 'house-25822';
const model = JSON.parse(readFileSync(`data/scenes/${scene}/latest.json`, 'utf8'));
const flagged = model.objects.find(o => o.clipped_by_frame && o.kind === 'tree') || model.objects.find(o => o.clipped_by_frame);
assert(flagged, 'the scene carries at least one clipped_by_frame seed');
for (const id of ['63a9ee5da10ccc41', '5c8a7960115526ec']) assert(!model.objects.some(o => o.id === id), `${id} is no longer a model object`);
const api = await (await fetch(`${base}/api/scenes?id=${scene}`)).json();
assert(api.model.model_signature === model.model_signature, 'the server serves the current model');
const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-sandbox'], protocolTimeout: 240000 });
const page = await browser.newPage(), errors = [], writes = [];
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => { if (r.method() === 'POST' && r.url().includes('/api/scenes')) writes.push(r.url()); });
await page.setViewport({ width: 1400, height: 1000 });
try {
  await page.goto(`${base}/scenes?scene=${scene}&status=all&object=${flagged.id}&layer=edited`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForSelector('.scene-svg', { timeout: 60000 });
  await page.waitForSelector('[data-testid=scene-frame-clip]', { timeout: 60000 });
  const note = await page.$eval('[data-testid=scene-frame-clip]', el => el.textContent);
  assert(note.includes('512 px frame'), `the note names the earlier frame: ${note}`);
  // the edited layer draws the saved review when it matches the model, otherwise the server's recovery preview
  const edited = api.review && api.review.model_signature === api.model.model_signature ? api.review : api.recovery?.review;
  assert(edited, 'the edited layer has a review or a recovery preview');
  const ghosts = ['63a9ee5da10ccc41', '5c8a7960115526ec'].filter(id => edited.objects.some(o => o.id === id));
  assert.deepEqual(ghosts, [], 'no clipped ghost outline survives in the edited layer');
  const inheritors = edited.objects.filter(o => (api.model.objects.find(m => m.id === o.id)?.replaces || []).length && o.status !== 'candidate');
  assert(inheritors.length >= 1, 'decisions moved onto the detections that took over their seeds');
  await page.screenshot({ path: '/tmp/addr-frame-clip.png' });
  assert.equal(writes.length, 0, 'read-only check');
  assert.equal(errors.length, 0, `no page errors: ${errors.join('; ')}`);
  console.log('Frame-cut hint and no ghost outlines: Passed.', JSON.stringify({ flagged: flagged.id, kind: flagged.kind, frame: flagged.clipped_by_frame }));
} finally { await browser.close(); }
