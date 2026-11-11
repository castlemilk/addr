// Outline hints in the browser, read-only: the scene editor's Agent hints line and its "Why flagged" details for the two
// reviewer-flagged outlines on roof_training_250-29501 (4fb089fc5f936f20, 54 noisy corners; 587e522a2986ccea, a 0.04 m² speck),
// the "No sub-kind" quick chip, the squared-candidate controls when the index carries a candidate, and the hints route's
// signature parameter. Nothing is clicked that saves and no key is pressed; the check reads the page and the route only.
// Served rows win over the editor's own measurement (mergeObjectHints), so each case pins the served wording when the index
// carries the row and the editor twin's wording otherwise. Hints are agent hints, never labels. Run against the dev server
// on :3008 (APP_URL overrides).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import puppeteer from 'puppeteer-core';
const base = process.env.APP_URL || 'http://localhost:3008';
const scene = 'roof_training_250-29501';
const cases = [
  // sentence: the editor twin (outline-hints.ts); served: the object-hints row (v2; the outline rules are unchanged since v1) (exterior-ring perimeter, 43 m, as the twin measures).
  { object: '4fb089fc5f936f20', codes: ['noisy_outline'], sentence: /54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight\. Check the shadow side\./,
    served: /54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight\. Check the shadow side\./, rejectWords: null },
  { object: '587e522a2986ccea', codes: ['degenerate_outline', 'tiny_area_for_kind'], sentence: /0\.04 m² over 4 corners, 0\.24 by 0\.26 m; too small to be the outline of anything\./,
    served: /0\.24 x 0\.26 m outline \(0\.04 m²\); too thin or small to be a building outline\./, rejectWords: { editor: 'too small to be the outline', served: 'too thin or small to be a building outline' } },
];
const structureLabels = { 'shipping-container': 'Shipping container', 'container-stack': 'Container stack', shed: 'Shed', garage: 'Garage', carport: 'Carport', 'attached-cover': 'Patio or carport cover', 'pergola-or-shade': 'Pergola or shade', 'water-tank': 'Water tank', 'portable-classroom': 'Portable classroom', 'pool-cover': 'Pool cover', fragment: 'Fragment' };
const json = async url => { const response = await fetch(url, { cache: 'no-store' }); return { status: response.status, body: response.status === 200 ? await response.json() : null }; };
const latest = JSON.parse(readFileSync(`data/scenes/${scene}/latest.json`, 'utf8'));
const signature = latest.model_signature;
// Until the scene's rows are rewritten under object-hints-v2 the route serves the object-hints-v1 row and marks the answer stale with the
// reason object_hints_version (the row predates the current hint rules). These objects are buildings, whose hints v2 did not change, so the
// editor shows no rules notice for them and names the served version in the Why-flagged footer. Whether a current row exists is read from
// the index itself, read-only.
const index = new (process.getBuiltinModule('node:sqlite').DatabaseSync)('data/reviews/eyes/eyes.sqlite', { readOnly: true });
const currentRow = object => !!index.prepare("SELECT 1 FROM object_quality WHERE version = 'object-hints-v2' AND scene_id = ? AND object_id = ? AND model_signature = ?").get(scene, object, signature);
const current = Object.fromEntries(cases.map(item => [item.object, currentRow(item.object)]));
index.close();
const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage(), errors = [];
page.on('pageerror', e => errors.push(e.message));
await page.setViewport({ width: 1400, height: 1000 });
try {
  // The route names the object-hints and lot-tags versions and judges staleness against the editor's model signature.
  const route = await json(`${base}/api/scenes/hints?scene=${scene}&object=${cases[0].object}&signature=${signature}`);
  assert.equal(route.status, 200, `the hints route answers for ${cases[0].object} (got ${route.status})`);
  assert.equal(route.body.object_hints_version, 'object-hints-v2'); assert.equal(route.body.lot_tags_version, 'lot-tags-v1');
  assert.equal(typeof route.body.stale, 'boolean', 'stale is judged');
  assert.equal(route.body.stale, route.body.stale_reasons.length > 0, 'stale is true exactly when a reason is named');
  if (current[cases[0].object]) assert.equal(route.body.object_hints_served_version, 'object-hints-v2', 'the served rows were computed under the current hint rules');
  else if (route.body.object_hints) {
    assert.equal(route.body.object_hints_served_version, 'object-hints-v1', 'only an object-hints-v1 row is in the index: served, and named as such');
    assert(route.body.stale_reasons.includes('object_hints_version'), `an older object-hints version is a stale reason: ${route.body.stale_reasons}`);
  }
  const servedCodes = route.body.object_hints?.hints?.map(h => h.code) ?? [];
  console.log(`Route: source ${route.body.source}, object-hints row ${route.body.object_hints ? `present (${servedCodes.join(', ') || 'no codes'})` : 'absent (the editor measures the outline itself)'}, squared candidate ${route.body.object_hints?.squared_candidate?.ring ? 'ring' : route.body.object_hints?.squared_candidate?.refused ? 'refused' : 'none'}.`);

  for (const item of cases) {
    await page.goto(`${base}/scenes?scene=${scene}&object=${item.object}&status=all&layer=edited`, { waitUntil: 'networkidle2', timeout: 120000 });
    await page.waitForSelector('.scene-svg', { timeout: 60000 });
    await page.waitForFunction(id => location.href.includes(id), { timeout: 30000 }, item.object);
    await page.waitForSelector('[data-testid="scene-object-hints"]', { timeout: 30000 });
    // The editor-measured hints render at once; the served rows arrive after a debounced fetch. Wait for them when the route has any.
    const served = await json(`${base}/api/scenes/hints?scene=${scene}&object=${item.object}&signature=${signature}`);
    if (served.status === 200) await page.waitForSelector('.scene-agent-hints[data-hints-served="true"]', { timeout: 30000 });
    const seen = await page.evaluate(() => {
      const line = document.querySelector('.scene-agent-hints');
      const details = document.querySelector('[data-testid="scene-object-hints"]');
      const fieldset = line?.closest('fieldset');
      const classSelect = [...fieldset?.querySelectorAll('label') ?? []].find(label => label.textContent.trim().startsWith('Class'));
      return {
        line: line?.textContent || '', muted: !!line?.classList.contains('review-muted'), stale: line?.getAttribute('data-hint-stale'),
        reasons: line?.getAttribute('data-hint-stale-reasons') || '', servedVersion: line?.getAttribute('data-hints-served-version') || '',
        footer: [...details?.querySelectorAll('p') ?? []].map(p => p.textContent).join(' '),
        afterClass: !!classSelect && !!line && !!(classSelect.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING),
        codes: details?.getAttribute('data-hint-codes') || '', summary: details?.querySelector('summary')?.textContent || '',
        items: [...details?.querySelectorAll('li') ?? []].map(li => ({ code: li.getAttribute('data-hint-code'), severity: li.getAttribute('data-hint-severity'), text: li.textContent })),
        open: details?.open ?? null, lines: document.querySelectorAll('.scene-agent-hints').length,
        quick: [...document.querySelectorAll('.scene-quick-kind')].map(b => b.textContent.trim()),
        rejectTitle: document.querySelector('[data-testid="scene-reject-decision"]')?.getAttribute('title') || '',
        rejectSuggested: !!document.querySelector('[data-testid="scene-reject-decision"].suggested'),
        candidateControls: !!document.querySelector('[data-testid="scene-squared-candidate-controls"]'),
        candidateDrawn: !!document.querySelector('[data-testid="scene-squared-candidate"]'),
        status: document.querySelector('.scene-inspector')?.textContent?.match(/needs adjustment|candidate|confirmed|rejected/i)?.[0] || '',
      };
    });
    assert.equal(seen.lines, 1, 'exactly one Agent hints line');
    assert(seen.afterClass && seen.muted, `the line keeps its place under the Class select and the muted type (${item.object})`);
    assert(seen.line.startsWith('Agent hints: '), `the line names itself: ${seen.line}`);
    const servedRow = served.status === 200 && served.body.object_hints?.hints?.some(h => h.code === item.codes[0]);
    assert.match(seen.line, servedRow ? item.served : item.sentence, `the ${servedRow ? 'served' : 'editor-measured'} outline sentence is on the line for ${item.object}: ${seen.line}`);
    assert(seen.line.includes('Not labels; change the Class above if you disagree.'), `the line says it is not a label: ${seen.line}`);
    if (served.status === 200 && served.body.structure && structureLabels[served.body.structure.sub_kind]) assert(seen.line.includes(`${structureLabels[served.body.structure.sub_kind]} (${served.body.structure.confidence})`), `the served structure hint stays on the line beside the outline sentence: ${seen.line}`);
    if (served.status === 200 && served.body.decision) assert(seen.line.includes('Machine label: '), `the machine label sentence follows: ${seen.line}`);
    assert(!seen.line.includes(' · ') && !seen.line.includes('→') && !/[A-Z]{4,}/.test(seen.line), `quiet copy: ${seen.line}`);
    const reasons = served.status === 200 ? served.body.stale_reasons ?? [] : [];
    assert.equal(seen.reasons, reasons.join(' '), `the line carries the served stale reasons (${item.object})`);
    assert(!seen.line.includes('or with earlier hint rules'), `no blanket notice: ${seen.line}`);
    if (reasons.includes('model_signature')) assert(seen.line.includes('Hints computed on an earlier image version.'), `the image-version notice: ${seen.line}`);
    if (reasons.includes('object_hints_version')) {
      assert.equal(seen.servedVersion, served.body.object_hints_served_version, 'the line names the version served');
      assert(!seen.line.includes('Object hints predate the current rules'), `a building's v1 hints are what v2 would say: no rules notice (${seen.line})`);
      assert(seen.footer.includes(`served from ${served.body.object_hints_served_version} rows`), `the Why-flagged footer names the served version: ${seen.footer}`);
    }
    if (!reasons.length) assert.equal(seen.stale, null, 'no stale notice while the served rows match the model signature and the hint rules');
    const fired = seen.codes.split(' ').filter(Boolean);
    for (const code of item.codes) assert(fired.includes(code), `${item.object}: ${code} is among the fired codes (${seen.codes})`);
    assert.match(seen.summary, /^Why flagged \(\d+\)$/, `the details summary counts the hints: ${seen.summary}`);
    assert.equal(seen.open, false, 'the details start closed; nothing shouts');
    for (const code of item.codes) {
      const li = seen.items.find(x => x.code === code);
      assert(li, `${code} has its own list item`); assert.equal(li.severity, 'review', `${code} is review severity`);
      assert(/measured in the editor from the stored outline|eyes index/.test(li.text), `${code} names its source: ${li.text}`);
    }
    assert(seen.quick.some(t => /No sub-kind/.test(t)) && !seen.quick.some(t => /Primary roof/.test(t)), `the empty quick kind reads "No sub-kind": ${seen.quick.join(' | ')}`);
    if (item.codes.includes('degenerate_outline')) {
      assert(seen.rejectSuggested, 'a degenerate outline pre-highlights the Not a roof button (suggestion, not a click)');
      assert(seen.rejectTitle.includes('Reject only if you agree.') && seen.rejectTitle.includes(servedRow ? item.rejectWords.served : item.rejectWords.editor), `the button title carries the sentence: ${seen.rejectTitle}`);
    } else assert(!seen.rejectSuggested, 'a noisy outline suggests no rejection');
    const candidate = route.body.object_hints?.squared_candidate;
    if (item.object === cases[0].object && candidate) assert(seen.candidateControls, 'the squared-candidate controls appear when the index carries a candidate or a refusal');
    assert(!seen.candidateDrawn, 'the squared candidate overlay stays off by default');
    await page.screenshot({ path: `/tmp/addr-scene-hints-outline-${item.object}.png` });
    console.log(`${item.object}: codes [${seen.codes}]; ${servedRow ? 'served row' : 'editor measurement'} shown; line "${seen.line.slice(0, 160)}${seen.line.length > 160 ? '…' : ''}"`);
  }
  // Status text is unchanged by loading and reading: the reviewer's decisions are exactly what review.json held before.
  const before = JSON.parse(readFileSync(`data/scenes/${scene}/review.json`, 'utf8'));
  for (const item of cases) assert.equal(before.objects.find(o => o.id === item.object)?.status, 'needs_adjustment', `${item.object} keeps its recorded status`);
  assert.deepEqual(errors, [], 'no page errors');
  console.log('Scene outline hints: noisy_outline and degenerate_outline sentences on the Agent hints line, Why-flagged details with codes and sources, No sub-kind chip, reject suggestion on the speck only, overlay off by default, stale reasons on the line, no rules notice on buildings (the footer names the served version). Passed (read-only).');
} catch (error) {
  await page.screenshot({ path: '/tmp/addr-scene-hints-outline-failure.png', fullPage: true });
  console.error(await page.evaluate(() => document.body.innerText.slice(0, 1500)));
  throw error;
} finally { await browser.close(); }
