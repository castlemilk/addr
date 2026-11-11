// Agent hints in the browser: the scene editor's read-only "Agent hints" line under the Class select for a
// known secondary structure, and structure-kind badges on /queues when the index holds structure rows for
// any queued roof. Both are agent hints, not labels; the check changes nothing. Run against a fresh build.
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';
const base = process.env.APP_URL || 'http://localhost:3008';
const target = { scene: 'mooroolbark-50011498-17-118440-80408', object: '7fdd138f819cb06c' };
const structureLabels = { 'shipping-container': 'Shipping container', 'container-stack': 'Container stack', shed: 'Shed', garage: 'Garage', carport: 'Carport', 'attached-cover': 'Patio or carport cover', 'pergola-or-shade': 'Pergola or shade', 'water-tank': 'Water tank', 'portable-classroom': 'Portable classroom', 'pool-cover': 'Pool cover', fragment: 'Fragment' };
const json = async url => { const response = await fetch(url, { cache: 'no-store' }); return { status: response.status, body: response.status === 200 ? await response.json() : null }; };
const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage(), errors = [];
page.on('pageerror', e => errors.push(e.message));
await page.setViewport({ width: 1400, height: 1000 });
try {
  // What the index says about the target right now decides how much the editor line must show.
  const hints = await json(`${base}/api/scenes/hints?scene=${target.scene}&object=${target.object}`);
  assert.equal(hints.status, 200, `the hints route answers for the known secondary structure (got ${hints.status}; is the build fresh?)`);
  assert.equal(hints.body.source, 'eyes-index', 'the eyes index is readable on this machine');
  const expectedKind = hints.body.structure ? structureLabels[hints.body.structure.sub_kind] ?? null : null;

  await page.goto(`${base}/scenes?scene=${target.scene}&object=${target.object}&kind=building&status=all`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForSelector('.scene-svg', { timeout: 60000 });
  await page.waitForFunction(id => location.href.includes(id), { timeout: 30000 }, target.object);
  await page.waitForSelector('.scene-agent-hints', { timeout: 30000 });
  const line = await page.evaluate(() => {
    const hint = document.querySelector('.scene-agent-hints');
    const fieldset = hint?.closest('fieldset');
    const classSelect = [...fieldset?.querySelectorAll('label') ?? []].find(label => label.textContent.trim().startsWith('Class'));
    return { text: hint?.textContent || '', afterClass: !!classSelect && !!(classSelect.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING), muted: hint?.classList.contains('review-muted'),
      heading: fieldset?.querySelector('h2')?.textContent || '', count: document.querySelectorAll('.scene-agent-hints').length };
  });
  assert.equal(line.count, 1, 'exactly one Agent hints line');
  assert.equal(line.heading, 'Object details', 'the line lives in the Object details panel');
  assert(line.afterClass, 'the line follows the Class select');
  assert(line.muted, 'the line uses the existing muted type');
  assert(line.text.startsWith('Agent hints: '), `the line names itself: ${line.text}`);
  assert(line.text.includes('Secondary structure'), `the class hint is spelled out: ${line.text}`);
  assert(line.text.includes('Not labels; change the Class above if you disagree.'), `the line says it is not a label: ${line.text}`);
  assert(!line.text.includes(' · ') && !line.text.includes('→') && !/[A-Z]{4,}/.test(line.text), `quiet copy: ${line.text}`);
  if (expectedKind) assert(line.text.includes(`${expectedKind} (${hints.body.structure.confidence})`), `the structure sub-kind and confidence appear: ${line.text}`);
  else console.log('No structure-kinds-v1 row for the target yet; the editor line shows the class hint only.');
  await page.screenshot({ path: '/tmp/addr-scene-hints-editor.png' });

  // /queues: structure badges appear exactly when a queued roof has a resolved structure hint.
  const [summary, hidden] = await Promise.all([json(`${base}/api/queues/summary`), json(`${base}/api/queues/hidden`)]);
  const planId = summary.body?.calibration?.plan_id;
  const calibration = planId ? await json(`${base}/api/queues/calibration?plan=${encodeURIComponent(planId)}`) : { status: 0, body: null };
  const queued = [...(calibration.body?.rows ?? []), ...(hidden.body?.rows ?? [])];
  // Fragment rows are hidden by default (Show fragments is off), so their badges are not on the page.
  const expectedBadges = queued.filter(row => row.structure && structureLabels[row.structure.sub_kind] && row.structure.sub_kind !== 'fragment').length;
  await page.goto(`${base}/queues`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForFunction(() => document.querySelectorAll('table.queues-table').length >= 5, { timeout: 120000 });
  await new Promise(resolve => setTimeout(resolve, 1000));
  const badges = await page.evaluate(() => ({
    // The Machine labelled table repeats the hints of its own 100-odd rows; count only the calibration and hidden tables.
    structure: [...document.querySelectorAll('.queues-hint-structure')].filter(b => !b.closest('#machine-labelled')).map(b => ({ label: b.textContent, title: b.title, fragment: b.classList.contains('queues-hint-fragment') })),
    counts: [...document.querySelectorAll('.queues-structure-count')].map(p => p.textContent),
  }));
  if (expectedBadges > 0) {
    assert(badges.structure.length >= 1, `at least one structure badge when ${expectedBadges} queued roofs carry a structure hint`);
    assert.equal(badges.structure.length, expectedBadges, 'one badge per queued roof with a resolved structure hint');
    for (const badge of badges.structure) {
      assert(Object.values(structureLabels).includes(badge.label), `badge label ${badge.label}`);
      assert(badge.title.startsWith('Agent hint, not a label. '), badge.title);
    }
    assert(badges.counts.length >= 1 && badges.counts.every(c => c.startsWith('Structure hints: ')), `count lines: ${badges.counts.join(' | ')}`);
    console.log(`Queues: ${badges.structure.length} structure badges (${badges.structure.filter(b => b.fragment).length} fragments); ${badges.counts.join(' | ')}`);
  } else {
    assert.equal(badges.structure.length, 0, 'no structure badge without a structure row');
    console.log('Skipped the /queues badge check: the index has no structure-kinds-v1 rows for any queued roof yet.');
  }
  await page.screenshot({ path: '/tmp/addr-scene-hints-queues.png', fullPage: true });
  assert.deepEqual(errors, [], 'no page errors');
  console.log(`Scene hints: "${line.text}" under the Class select; queues badges ${expectedBadges > 0 ? 'checked' : 'skipped (no rows yet)'}. Passed.`);
} catch (error) {
  await page.screenshot({ path: '/tmp/addr-scene-hints-failure.png', fullPage: true });
  console.error(await page.evaluate(() => document.body.innerText.slice(0, 2000)));
  throw error;
} finally { await browser.close(); }
