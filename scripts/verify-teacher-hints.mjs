// Teacher hints end to end on scratch copies (Step 11.2): qa_teacher_<ts>_a copies roof_training_250-30371 (a cohort frame with no
// region) and qa_teacher_<ts>_b copies house-25898, both with the id rewritten and review.json / confidence.json removed. The two
// hint modules write their sidecar rows for the copies, the hints route serves them under the copies' model signatures, and the
// scene editor shows the fitted-frame boundary notice, the lot-frame sentence, the Why-flagged codes, the lot sentences and the
// reject / secondary suggestions. Then the scratch folders and their eyes rows are removed and the four real scenes' latest.json
// and review.json hashes are shown unchanged. Real scenes are read only; no key is pressed; hints are agent hints, never labels.
// Run against the dev server on :3008 (APP_URL overrides); Python from scripts/ml/.venv.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import puppeteer from 'puppeteer-core';

const base = process.env.APP_URL || 'http://localhost:3008';
const PYTHON = 'scripts/ml/.venv/bin/python';
const INDEX = 'data/reviews/eyes/eyes.sqlite';
const REAL = ['roof_training_250-30371', 'house-25898', 'roof_training_250-29501', 'house-25910'];
const stamp = Date.now();
const scratch = { a: { id: `qa_teacher_${stamp}_a`, from: 'roof_training_250-30371' }, b: { id: `qa_teacher_${stamp}_b`, from: 'house-25898' } };
const ids = [scratch.a.id, scratch.b.id];
const sha = file => createHash('sha256').update(readFileSync(file)).digest('hex');
const baseline = () => Object.fromEntries(REAL.flatMap(id => ['latest.json', 'review.json'].filter(f => existsSync(`data/scenes/${id}/${f}`)).map(f => [`${id}/${f}`, sha(`data/scenes/${id}/${f}`)])));
const json = async url => { const r = await fetch(url, { cache: 'no-store' }); return { status: r.status, body: r.status === 200 ? await r.json() : null }; };
const python = (args, label) => {
  const started = Date.now();
  const result = spawnSync(PYTHON, ['-m', ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  assert.equal(result.status, 0, `${label} exited ${result.status}: ${(result.stderr || '').slice(-1200)}`);
  console.log(`${label}: ${((Date.now() - started) / 1000).toFixed(1)} s`);
  return result.stdout;
};
/** Copy a real scene into a scratch id: same image, affines and model signature; no review, no confidence; the id rewritten. */
const makeScratch = ({ id, from }) => {
  const folder = `data/scenes/${id}`;
  cpSync(`data/scenes/${from}`, folder, { recursive: true });
  for (const file of ['review.json', 'confidence.json']) rmSync(`${folder}/${file}`, { force: true });
  const latest = JSON.parse(readFileSync(`${folder}/latest.json`, 'utf8'));
  latest.id = id;
  writeFileSync(`${folder}/latest.json`, JSON.stringify(latest));
  if (existsSync(`${folder}/models`)) for (const name of readdirSync(`${folder}/models`).filter(n => n.endsWith('.json'))) {
    const model = JSON.parse(readFileSync(`${folder}/models/${name}`, 'utf8'));
    if (model && model.id === from) { model.id = id; writeFileSync(`${folder}/models/${name}`, JSON.stringify(model)); }
  }
  return latest;
};
const removeScratchRows = () => {
  const { DatabaseSync } = process.getBuiltinModule('node:sqlite');
  if (!existsSync(INDEX)) return { deleted: 0, remaining: 0 };
  const db = new DatabaseSync(INDEX);
  try {
    db.exec('PRAGMA busy_timeout = 15000');
    const before = db.prepare('SELECT count(*) AS n FROM object_quality WHERE scene_id IN (?, ?)').get(...ids).n;
    db.prepare('DELETE FROM object_quality WHERE scene_id IN (?, ?)').run(...ids);
    const remaining = db.prepare('SELECT count(*) AS n FROM object_quality WHERE scene_id IN (?, ?)').get(...ids).n;
    return { deleted: before, remaining };
  } finally { db.close(); }
};

const before = baseline();
console.log(`Baseline: ${Object.entries(before).map(([k, v]) => `${k} ${v.slice(0, 16)}`).join('; ')}`);
const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage(), errors = [];
page.on('pageerror', e => errors.push(e.message));
await page.setViewport({ width: 1400, height: 1000 });
/** Open one object of one scene and read the hint surfaces. Reads only: nothing is clicked that saves and no key is pressed. */
const open = async (scene, object) => {
  await page.goto(`${base}/scenes?scene=${scene}${object ? `&object=${object}` : ''}&status=all&layer=edited`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForSelector('.scene-svg', { timeout: 60000 });
  if (object) {
    await page.waitForFunction(id => location.href.includes(id), { timeout: 30000 }, object);
    await page.waitForSelector('.scene-agent-hints[data-hints-served="true"]', { timeout: 45000 });
  }
  return page.evaluate(() => {
    const line = document.querySelector('.scene-agent-hints'), details = document.querySelector('[data-testid="scene-object-hints"]');
    const reject = document.querySelector('[data-testid="scene-reject-decision"]');
    return {
      line: line?.textContent || '', stale: line?.getAttribute('data-hint-stale'),
      codes: (details?.getAttribute('data-hint-codes') || '').split(' ').filter(Boolean),
      items: [...details?.querySelectorAll('li') ?? []].map(li => ({ code: li.getAttribute('data-hint-code'), severity: li.getAttribute('data-hint-severity'), text: li.textContent })),
      rejectSuggested: !!reject?.classList.contains('not-a-roof') && !!reject?.classList.contains('suggested'), rejectTitle: reject?.getAttribute('title') || '', rejectBy: reject?.getAttribute('data-suggested-by') || '',
      quickSuggested: [...document.querySelectorAll('.scene-quick-kind.suggested')].map(b => b.textContent.trim()),
      secondaryNote: document.querySelector('[data-testid="scene-secondary-suggestion"]')?.textContent || '',
      status: document.querySelector('.scene-inspector')?.textContent?.match(/needs adjustment|candidate|confirmed|rejected/i)?.[0] || '',
      parcelControls: !!document.querySelector('.scene-parcel-controls'), registration: document.querySelector('.scene-parcel-controls')?.getAttribute('data-parcel-registration') || '',
      parcelStatus: document.querySelector('.scene-parcel-controls [role="status"]')?.textContent || '',
      fittedNote: document.querySelector('[data-testid="scene-parcel-fitted-note"]')?.textContent || '',
      lotSentence: document.querySelector('[data-testid="scene-lot-sentence"]')?.textContent || '',
      lotFrame: document.querySelector('[data-testid="scene-lot-frame"]')?.textContent || '',
    };
  });
};
/** Wait until the boundary layer has settled (loaded or unavailable), never leaving it at "Loading Vicmap…". */
const settledParcels = async () => {
  await page.waitForFunction(() => { const s = document.querySelector('.scene-parcel-controls [role="status"]')?.textContent || ''; return s && !s.includes('Loading Vicmap'); }, { timeout: 60000 });
  return page.evaluate(() => document.querySelector('.scene-parcel-controls [role="status"]')?.textContent || '');
};

let created = false;
try {
  // 1. Scratch copies.
  const latestA = makeScratch(scratch.a), latestB = makeScratch(scratch.b); created = true;
  assert(!latestA.region && latestB.region, 'scene a is the region-less cohort frame, scene b the registered capture');
  // 2. Sidecar rows for the copies only (explicit --scenes; parcels from frozen caches, no Vicmap read).
  python(['scripts.ml.scene_lot_tags', '--write', '--offline', '--quiet', '--scenes', ...ids], 'scene_lot_tags --write --offline');
  python(['scripts.ml.scene_object_hints', '--write', '--scenes', ...ids], 'scene_object_hints --write');

  // 3. Scene a: the served canopy hint, the fitted-frame notice, the lot-frame sentence and the frame-cut object.
  const canopy = await json(`${base}/api/scenes/hints?scene=${scratch.a.id}&object=35f242ae81062884&signature=${latestA.model_signature}`);
  assert.equal(canopy.status, 200, `hints route answers for the scratch canopy piece (got ${canopy.status})`);
  assert.equal(canopy.body.source, 'eyes-index'); assert.equal(canopy.body.stale, false, 'rows computed under the copy\'s model signature');
  const canopyCodes = canopy.body.object_hints?.hints?.map(h => h.code) ?? [];
  assert(canopyCodes.includes('canopy_fragment_inside'), `35f242ae81062884 serves canopy_fragment_inside (${canopyCodes.join(', ')})`);
  assert.equal(typeof canopy.body.lot_frame, 'string', 'the scene row\'s lot-frame sentence rides along');
  assert.match(canopy.body.lot_frame, /^A lot frame for 30371 would be \d+ m at 0\.\d+ m\/px/, canopy.body.lot_frame);
  const sceneRow = await json(`${base}/api/scenes/hints?scene=${scratch.a.id}&object=_scene`);
  assert.equal(sceneRow.status, 200, 'the _scene row is reachable on its own'); assert.equal(sceneRow.body.lot_frame, canopy.body.lot_frame);

  let seen = await open(scratch.a.id, 'd7f7ceb84a6d0db4');
  assert(seen.parcelControls && seen.registration === 'fitted', `the boundaries control renders on the region-less scene as fitted (${seen.registration})`);
  const statusA = await settledParcels();
  assert(!statusA.includes('Boundaries unavailable'), `Vicmap answered for the cohort frame: ${statusA}`);
  assert(statusA.includes('Boundaries placed from the fitted cohort frame; alignment unverified.'), `the fitted-frame notice is on the status line: ${statusA}`);
  seen = await open(scratch.a.id, 'd7f7ceb84a6d0db4');
  assert(seen.fittedNote.includes('fitted from the CRM roof by least squares'), `the Source details carry the fitted note: ${seen.fittedNote}`);
  for (const code of ['frame_cut', 'lot_unassociated']) assert(seen.codes.includes(code), `d7f7ceb84a6d0db4: ${code} among the fired codes (${seen.codes.join(' ')})`);
  for (const code of ['frame_cut', 'lot_unassociated']) { const li = seen.items.find(x => x.code === code); assert(li && li.severity === 'review' && /eyes index/.test(li.text), `${code} is a review item from the eyes index: ${li?.text}`); }
  assert(seen.line.includes('leaves this image'), `the lot sentence says the lot leaves the image: ${seen.line}`);
  assert(seen.line.includes('Touches the image edge; the frame cut it, not the building.'), `the frame_cut sentence is on the line: ${seen.line}`);
  assert(seen.lotFrame.startsWith('A lot frame for 30371 would be'), `the Step 3.3 lot-frame sentence is appended under the lot sentence on the cohort frame: "${seen.lotFrame}"`);
  assert(!seen.lotFrame.includes('button') && !/Capture|Refit/.test(seen.lotFrame), 'text only, no capture control');
  assert.equal(seen.stale, null, 'no stale notice under the copy\'s signature');
  console.log(`a/d7f7ceb84a6d0db4: codes [${seen.codes.join(' ')}]; parcels "${statusA.slice(0, 90)}"; lot frame "${seen.lotFrame.slice(0, 80)}…"`);
  seen = await open(scratch.a.id, '35f242ae81062884');
  assert(seen.codes.includes('canopy_fragment_inside'), `35f242ae81062884 shows canopy_fragment_inside (${seen.codes.join(' ')})`);
  assert(seen.rejectSuggested && seen.rejectTitle.includes('Reject only if you agree.'), `the canopy piece pre-highlights Not a roof as a suggestion only: ${seen.rejectTitle}`);
  console.log(`a/35f242ae81062884: codes [${seen.codes.join(' ')}]; reject suggested by ${seen.rejectBy}`);

  // 4. Scene b: pavement suggestion, the secondary suggestion with an unchanged status, and the frame-cut sentence.
  seen = await open(scratch.b.id, '924c83ad298eef05');
  assert(seen.codes.includes('building_blends_into_surroundings'), `924c83ad298eef05 fires building_blends_into_surroundings (${seen.codes.join(' ')})`);
  assert(seen.rejectSuggested, '924c83ad298eef05 pre-highlights .not-a-roof.suggested');
  assert(seen.rejectTitle.includes('pavement') && seen.rejectTitle.includes('Reject only if you agree.'), `the title carries the pavement sentence: ${seen.rejectTitle}`);
  console.log(`b/924c83ad298eef05: codes [${seen.codes.join(' ')}]; title "${seen.rejectTitle.slice(0, 120)}"`);
  seen = await open(scratch.b.id, '689ea9979a45b7e3');
  const statusAtLoad = seen.status;
  assert(seen.codes.includes('secondary_by_lot'), `689ea9979a45b7e3 fires secondary_by_lot (${seen.codes.join(' ')})`);
  assert(/secondary roof/.test(seen.line), `the secondary sentence is on the line: ${seen.line}`);
  assert(seen.quickSuggested.length === 1, `exactly one suggested quick-kind chip (${seen.quickSuggested.join(' | ')})`);
  assert(seen.secondaryNote.includes('A hint, not a label'), `the chip note says it is a hint: ${seen.secondaryNote}`);
  // Wording: this garage sits 160-200 px from the frame, so the lot sentence must not claim the frame cut it.
  const lotIncomplete = seen.items.find(x => x.code === 'lot_incomplete_in_frame');
  assert(lotIncomplete && lotIncomplete.text.includes('only part of the lot is visible') && !lotIncomplete.text.includes('the frame cut it'), `no frame-cut claim without a measured touch: ${lotIncomplete?.text}`);
  assert(!seen.codes.includes('frame_cut'), 'and frame_cut itself does not fire');
  await new Promise(resolve => setTimeout(resolve, 1500));
  const statusAfter = await page.evaluate(() => document.querySelector('.scene-inspector')?.textContent?.match(/needs adjustment|candidate|confirmed|rejected/i)?.[0] || '');
  assert.equal(statusAfter, statusAtLoad, 'the status text is unchanged after load (suggestions change nothing)');
  // Loading and reading saved nothing: the scratch copy still has no review and the object keeps its model status.
  const savedB = await json(`${base}/api/scenes?id=${scratch.b.id}`);
  assert.equal(savedB.status, 200); assert.equal(savedB.body.review, null, 'no review.json was created by loading the scratch scene');
  assert.equal(savedB.body.model.objects.find(o => o.id === '689ea9979a45b7e3')?.status, 'candidate', 'the object keeps its model status');
  assert(!existsSync(`data/scenes/${scratch.b.id}/review.json`), 'no review file on disk');
  console.log(`b/689ea9979a45b7e3: codes [${seen.codes.join(' ')}]; chip [${seen.quickSuggested.join(' | ')}]; status "${statusAtLoad}"; lot "${lotIncomplete.text.slice(0, 110)}"`);
  seen = await open(scratch.b.id, '593036a5bbb24b18');
  assert(seen.codes.includes('frame_cut'), `593036a5bbb24b18 fires frame_cut (${seen.codes.join(' ')})`);
  assert(seen.line.includes('Touches the image edge; the frame cut it, not the building.'), `the frame_cut sentence is on the line: ${seen.line}`);
  assert.equal(seen.lotFrame, '', 'a registered capture (region present) prints no lot-frame sentence');
  console.log(`b/593036a5bbb24b18: codes [${seen.codes.join(' ')}]`);

  // Read-only real-scene checks: the served secondary hint and its wording on the real house scene, the boundary control on a cohort frame.
  const realSignature = JSON.parse(readFileSync('data/scenes/house-25898/latest.json', 'utf8')).model_signature;
  const real = await json(`${base}/api/scenes/hints?scene=house-25898&object=689ea9979a45b7e3&signature=${realSignature}`);
  assert.equal(real.status, 200, `the real house-25898/689ea9979a45b7e3 row is served (got ${real.status})`);
  const realCodes = real.body.object_hints?.hints?.map(h => h.code) ?? [];
  assert(realCodes.includes('secondary_by_lot'), `house-25898/689ea9979a45b7e3 serves secondary_by_lot (${realCodes.join(', ')})`);
  const realLot = real.body.object_hints.hints.find(h => h.code === 'lot_incomplete_in_frame');
  assert(realLot && realLot.sentence.includes('only part of the lot is visible'), `the served real row carries the measured wording: ${realLot?.sentence}`);
  assert(!real.body.stale_reasons.includes('model_signature'), `the real row was computed on the current image version: ${real.body.stale_reasons}`);
  if (real.body.object_hints_served_version === 'object-hints-v2') assert.equal(real.body.stale, false, 'the real row is current');
  else assert.deepEqual(real.body.stale_reasons, ['object_hints_version'], 'served from an object-hints-v1 row until the scene is rewritten: stale for the rules only');
  seen = await open('roof_training_250-29501', null);
  assert(seen.parcelControls && seen.registration === 'fitted', 'roof_training_250-29501 shows the boundaries control (fitted cohort frame)');
  const status29501 = await settledParcels();
  assert(!status29501.includes('Loading Vicmap'), `the boundary layer settles: ${status29501}`);
  console.log(`real: house-25898/689ea9979a45b7e3 [${realCodes.join(' ')}]; roof_training_250-29501 parcels "${status29501.slice(0, 90)}"`);

  // 5. No page errors; nothing was typed or pressed.
  assert.deepEqual(errors, [], 'no page errors');
  console.log('Teacher hints on scratch copies: canopy row served with stale false, fitted-frame notice and lot-frame sentence on the cohort frame, frame_cut + lot_unassociated details, pavement reject suggestion, secondary chip with unchanged status, frame_cut sentence, measured lot wording; real scenes read-only. Passed.');
} catch (error) {
  try { await page.screenshot({ path: '/tmp/addr-teacher-hints-failure.png', fullPage: true }); console.error(await page.evaluate(() => document.body.innerText.slice(0, 1500))); } catch { /* page gone */ }
  throw error;
} finally {
  await browser.close();
  // 6. Scratch cleanup and the baseline re-check.
  const rows = created ? removeScratchRows() : { deleted: 0, remaining: 0 };
  for (const id of ids) rmSync(`data/scenes/${id}`, { recursive: true, force: true });
  const after = baseline();
  assert.deepEqual(after, before, 'the real scenes\' latest.json and review.json are byte-identical to the baseline');
  assert.equal(rows.remaining, 0, 'no scratch rows remain in the eyes index');
  assert(ids.every(id => !existsSync(`data/scenes/${id}`)), 'scratch folders removed');
  console.log(`Cleanup: ${rows.deleted} scratch eyes rows deleted, ${rows.remaining} remaining; ${Object.keys(after).length} baseline hashes unchanged.`);
}
