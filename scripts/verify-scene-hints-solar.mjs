// Solar-panel hints in the browser, read-only: the two solar_panel detections the reviewer rejected on roof_training_250-29761 on the
// evening of 2026-09-30 (5bac1a267bec9d1f, "a hot water or aircon system, not solar"; ca848734b13d480d, "a skylight or something") carry
// the object-hints-v2 solar_fixture_like hint on the scene editor's Agent hints line and in its Why-flagged list once the eyes index holds
// v2 rows for the scene (teacher_checks --write-index). The hint suggests nothing: it does not pre-highlight the Not a roof button and
// nothing is clicked. Where a solar panel elsewhere is still served from an object-hints-v1 row, the line says the solar-panel check is not
// included. The scene's review.json is hashed before and after, and the index is opened read-only. Hints are agent hints, never labels.
// Run against the dev server on :3008 (APP_URL overrides).
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import puppeteer from 'puppeteer-core';

const base = process.env.APP_URL || 'http://localhost:3008';
const scene = 'roof_training_250-29761';
const cases = ['5bac1a267bec9d1f', 'ca848734b13d480d'];
const NOTICE = 'Object hints predate the current rules (object-hints-v1): the solar-panel check is not included.';
const sha = file => createHash('sha256').update(readFileSync(file)).digest('hex');
const reviewBefore = sha(`data/scenes/${scene}/review.json`);
const signatureOf = id => JSON.parse(readFileSync(`data/scenes/${id}/latest.json`, 'utf8')).model_signature;
const json = async url => { const response = await fetch(url, { cache: 'no-store' }); return { status: response.status, body: response.status === 200 ? await response.json() : null }; };
// A live solar panel elsewhere that the index still serves from an object-hints-v1 row (none once every scene is rewritten); read-only.
const index = new (process.getBuiltinModule('node:sqlite').DatabaseSync)('data/reviews/eyes/eyes.sqlite', { readOnly: true });
const olderRows = index.prepare(`SELECT scene_id, object_id FROM object_quality o WHERE version = 'object-hints-v1' AND json_extract(payload, '$.kind') = 'solar_panel'
  AND NOT EXISTS (SELECT 1 FROM object_quality n WHERE n.version = 'object-hints-v2' AND n.scene_id = o.scene_id AND n.object_id = o.object_id) ORDER BY scene_id, object_id`).all();
const v2Rows = index.prepare("SELECT count(*) AS n FROM object_quality WHERE version = 'object-hints-v2' AND scene_id = ?").get(scene).n;
index.close();
const liveIn = id => new Set(JSON.parse(readFileSync(`data/scenes/${id}/latest.json`, 'utf8')).objects.filter(o => o.kind === 'solar_panel' && o.status !== 'superseded').map(o => o.id));
const older = olderRows.find(row => { try { return liveIn(row.scene_id).has(row.object_id); } catch { return false; } }) ?? null;

const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage(), errors = [];
page.on('pageerror', e => errors.push(e.message));
await page.setViewport({ width: 1400, height: 1000 });
/** Open one object and read the hint surfaces. Reads only: nothing is clicked and no key is pressed. */
const open = async (sceneId, object) => {
  await page.goto(`${base}/scenes?scene=${sceneId}&object=${object}&status=all&layer=edited`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForSelector('.scene-svg', { timeout: 60000 });
  await page.waitForFunction(id => location.href.includes(id), { timeout: 30000 }, object);
  await page.waitForSelector('.scene-agent-hints[data-hints-served="true"]', { timeout: 45000 });
  return page.evaluate(() => {
    const line = document.querySelector('.scene-agent-hints'), details = document.querySelector('[data-testid="scene-object-hints"]');
    const reject = document.querySelector('[data-testid="scene-reject-decision"]');
    return {
      line: line?.textContent || '', stale: line?.getAttribute('data-hint-stale'), reasons: line?.getAttribute('data-hint-stale-reasons') || '',
      servedVersion: line?.getAttribute('data-hints-served-version') || '', lines: document.querySelectorAll('.scene-agent-hints').length,
      codes: (details?.getAttribute('data-hint-codes') || '').split(' ').filter(Boolean),
      items: [...details?.querySelectorAll('li') ?? []].map(li => ({ code: li.getAttribute('data-hint-code'), severity: li.getAttribute('data-hint-severity'), text: li.textContent })),
      rejectBy: reject?.getAttribute('data-suggested-by') || '',
    };
  });
};
try {
  assert(v2Rows > 0, `the index holds object-hints-v2 rows for ${scene} (run teacher_checks --write-index on the evening set first)`);
  const signature = signatureOf(scene);
  for (const object of cases) {
    const route = await json(`${base}/api/scenes/hints?scene=${scene}&object=${object}&signature=${signature}`);
    assert.equal(route.status, 200, `the hints route answers for ${object} (got ${route.status})`);
    assert.equal(route.body.object_hints_served_version, 'object-hints-v2', `${object} is served from a v2 row`);
    assert(!route.body.stale_reasons.includes('object_hints_version'), `current rules: ${route.body.stale_reasons}`);
    const solar = route.body.object_hints.hints.find(h => h.code === 'solar_fixture_like');
    assert(solar, `${object} carries solar_fixture_like (${route.body.object_hints.hints.map(h => h.code).join(', ')})`);
    assert.equal(solar.severity, 'review');
    assert.match(solar.sentence, / for a solar panel \(.*\); check for a hot-water unit, air-conditioner or skylight\.$/, solar.sentence);
    const seen = await open(scene, object);
    assert.equal(seen.lines, 1, 'exactly one Agent hints line');
    assert(seen.line.startsWith('Agent hints: ') && seen.line.includes(solar.sentence), `the solar sentence is on the line: ${seen.line}`);
    assert(seen.line.includes('Not labels; change the Class above if you disagree.'), `the line says it is not a label: ${seen.line}`);
    assert.equal(seen.servedVersion, 'object-hints-v2'); assert(!seen.line.includes('Object hints predate the current rules'), `no rules notice on current rows: ${seen.line}`);
    assert(seen.codes.includes('solar_fixture_like'), `solar_fixture_like is among the Why-flagged codes (${seen.codes.join(' ')})`);
    const item = seen.items.find(i => i.code === 'solar_fixture_like');
    assert(item && item.severity === 'review' && item.text.includes('eyes index'), `its list item is review severity from the eyes index: ${JSON.stringify(item)}`);
    assert.notEqual(seen.rejectBy, 'solar_fixture_like', 'the hint pre-highlights nothing; the reviewer decides');
    await page.screenshot({ path: `/tmp/addr-scene-hints-solar-${object}.png` });
    console.log(`${object}: served ${seen.servedVersion}, codes [${seen.codes.join(' ')}]; line "${seen.line.slice(0, 170)}${seen.line.length > 170 ? '…' : ''}"`);
  }
  if (older) {
    const route = await json(`${base}/api/scenes/hints?scene=${older.scene_id}&object=${older.object_id}&signature=${signatureOf(older.scene_id)}`);
    assert.equal(route.status, 200); assert.equal(route.body.object_hints_served_version, 'object-hints-v1'); assert(route.body.stale_reasons.includes('object_hints_version'));
    const seen = await open(older.scene_id, older.object_id);
    assert.equal(seen.stale, 'true'); assert(seen.line.includes(NOTICE), `a solar panel served from a v1 row says the check is missing: ${seen.line}`);
    assert(seen.reasons.split(' ').includes('object_hints_version') && seen.servedVersion === 'object-hints-v1', `reasons ${seen.reasons}, served ${seen.servedVersion}`);
    console.log(`${older.scene_id}/${older.object_id} (solar panel, v1 row): "${seen.line.slice(0, 170)}${seen.line.length > 170 ? '…' : ''}"`);
  } else console.log('No live solar panel is served from an object-hints-v1 row any more; the rules notice has nothing to show.');
  assert.equal(sha(`data/scenes/${scene}/review.json`), reviewBefore, 'review.json is unchanged by reading');
  assert.deepEqual(errors, [], 'no page errors');
  console.log('Scene solar hints: solar_fixture_like on the Agent hints line and in Why flagged for both reviewer cases, nothing pre-highlighted, the v1 rules notice on solar panels elsewhere. Passed (read-only).');
} catch (error) {
  await page.screenshot({ path: '/tmp/addr-scene-hints-solar-failure.png', fullPage: true });
  console.error(await page.evaluate(() => document.body.innerText.slice(0, 1500)));
  throw error;
} finally { await browser.close(); }
