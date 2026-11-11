// Machine labels in the browser (decision-policy-v2, v1 rows read as a fallback). Opens the editor for a known
// secondary structure with status=all and checks that the "Agent hints" line carries the machine label sentence,
// with its model range and human agreement interval when the row is v2, that "Use machine label" changes the Label
// field without saving, that /queues shows tier badges and the per-table count line when any queued roof has a
// decision, and that the Machine labelled table renders one row per auto-tier roof in the index. Each part skips
// with a message when the index holds no such row yet, because the decision-policy module writes those rows
// separately. Nothing here saves a scene or writes to the index, and nothing is screenshotted. Run against a fresh build.
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';
const base = process.env.APP_URL || 'http://localhost:3008';
const target = { scene: 'mooroolbark-50011498-17-118440-80408', object: '7fdd138f819cb06c' };
const tierLabels = { auto: 'Auto', assist: 'Assist', review: 'Review' };
const json = async url => { const response = await fetch(url, { cache: 'no-store' }); return { status: response.status, body: response.status === 200 ? await response.json() : null }; };
const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, protocolTimeout: 240000, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage(), errors = [], posts = [];
let hub = null;
// Applying a machine label leaves an unsaved draft; the editor's leave guard raises a beforeunload dialog on the next navigation.
page.on('dialog', dialog => dialog.accept().catch(() => {}));
page.on('pageerror', e => errors.push(e.message));
page.on('request', request => { if (request.method() !== 'GET') posts.push(`${request.method()} ${request.url()}`); });
await page.setViewport({ width: 1400, height: 1000 });
const skipped = [];
try {
  // What the route says about the target decides how much the editor must show.
  const hints = await json(`${base}/api/scenes/hints?scene=${target.scene}&object=${target.object}`);
  assert.equal(hints.status, 200, `the hints route answers for the known secondary structure (got ${hints.status}; is the build fresh?)`);
  assert.equal(hints.body.source, 'eyes-index', 'the eyes index is readable on this machine');
  if (!('decision_version' in hints.body)) throw new Error('The running build predates decision_version in the hints route; rebuild before verifying machine labels.');
  assert.equal(hints.body.decision_version, 'decision-policy-v2', `the running build serves decision_version ${hints.body.decision_version}; rebuild before verifying the v2 intervals and the Machine labelled table`);
  const decision = hints.body.decision;

  await page.goto(`${base}/scenes?scene=${target.scene}&object=${target.object}&kind=building&status=all`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForSelector('.scene-svg', { timeout: 60000 });
  await page.waitForFunction(id => location.href.includes(id), { timeout: 30000 }, target.object);
  await page.waitForSelector('.scene-agent-hints', { timeout: 30000 });
  const readLine = () => page.evaluate(() => {
    const hint = document.querySelector('.scene-agent-hints');
    const button = document.querySelector('.scene-use-machine-label');
    const fieldset = hint?.closest('fieldset');
    const labelInput = [...fieldset?.querySelectorAll('label') ?? []].find(label => label.textContent.trim().startsWith('Label'))?.querySelector('input');
    return { text: hint?.textContent || '', count: document.querySelectorAll('.scene-use-machine-label').length, buttonDisabled: button ? button.disabled : null, buttonTitle: button?.title || '',
      buttonInLine: !!button && !!hint && hint.contains(button), label: labelInput ? labelInput.value : null, saveEnabled: [...document.querySelectorAll('button')].some(b => /^Save scene/.test(b.textContent.trim()) && !b.disabled) };
  });
  let line = await readLine();
  assert(line.text.startsWith('Agent hints: '), `the line names itself: ${line.text}`);
  if (decision) {
    const tier = decision.tier, confidence = Number(decision.confidence).toFixed(2);
    const range = decision.score_interval ? ` (model range ${Number(decision.score_interval.lower).toFixed(2)} to ${Number(decision.score_interval.upper).toFixed(2)})` : '';
    assert(line.text.includes(`Machine label: `) && line.text.includes(`, ${tier} tier, confidence ${confidence}${range}.`), `the machine label sentence appears with its tier, confidence${range ? ' and model range' : ''}: ${line.text}`);
    if (decision.interval) {
      const agreement = decision.interval.judged && decision.interval.lower !== null && decision.interval.upper !== null
        ? `${decision.interval.confirmed} of ${decision.interval.judged} confirmed, 95% interval ${Number(decision.interval.lower).toFixed(2)} to ${Number(decision.interval.upper).toFixed(2)}.` : 'not yet judged by a reviewer.';
      assert(line.text.includes(`Human agreement for ${decision.interval.stratum || `${tier}/`}`) && line.text.includes(agreement), `the human agreement sentence appears: ${line.text}`);
    } else console.log('The target still has a decision-policy-v1 row (no intervals); the interval copy is checked in the node tests only.');
    assert(!line.text.includes('–'), 'ranges say "to", never a dash');
    assert(!line.text.includes(' · ') && !line.text.includes('→') && !/[A-Z]{4,}/.test(line.text.replace('Use machine label', '')), `quiet copy: ${line.text}`);
    assert.equal(line.count, 1, 'exactly one Use machine label button');
    assert(line.buttonInLine, 'the button sits in the Agent hints line');
    assert.equal(line.buttonTitle, "Applies the machine label to this object's Label and roof type; you still save the scene.");
    assert.equal(line.buttonDisabled, false, 'the button is enabled on the edited layer for an active object');
    const before = line.label;
    await page.click('.scene-use-machine-label');
    await page.waitForFunction(previous => {
      const fieldset = document.querySelector('.scene-agent-hints')?.closest('fieldset');
      const input = [...fieldset?.querySelectorAll('label') ?? []].find(label => label.textContent.trim().startsWith('Label'))?.querySelector('input');
      return input && input.value !== previous;
    }, { timeout: 10000 }, before);
    line = await readLine();
    assert.notEqual(line.label, before, `Use machine label changes the Label field (was ${JSON.stringify(before)})`);
    assert(/^[A-Z][a-z]/.test(line.label) && !/[A-Z]{2,}/.test(line.label), `the label is in sentence case: ${line.label}`);
    assert.deepEqual(posts.filter(p => /\/api\/scenes(\?|$)/.test(p)), [], 'the button saved nothing; the reviewer still presses Save scene');
    console.log(`Editor: "${line.text.replace('Use machine label', '').trim()}"; Use machine label set the Label to "${line.label}" without saving.`);
    // The model layer disables the button: the machine label applies to the reviewer's edits, never to the prediction.
    await page.goto(`${base}/scenes?scene=${target.scene}&object=${target.object}&kind=building&status=all&layer=model`, { waitUntil: 'networkidle2', timeout: 120000 });
    await page.waitForSelector('.scene-agent-hints', { timeout: 30000 });
    const onModel = await readLine();
    assert.equal(onModel.count, 1, 'the button is still offered on the model layer');
    assert.equal(onModel.buttonDisabled, true, 'and disabled there');
  } else {
    skipped.push('editor machine label (the hints route returns no decision for the target yet)');
    assert.equal(line.count, 0, 'no Use machine label button without a decision');
    console.log('Skipped the editor machine-label check: the index holds no decision-policy-v1 row for the target yet.');
  }

  // /queues: tier badges and the count line appear exactly when a queued roof has a decision.
  const [summary, hidden] = await Promise.all([json(`${base}/api/queues/summary`), json(`${base}/api/queues/hidden`)]);
  const planId = summary.body?.calibration?.plan_id;
  const calibration = planId ? await json(`${base}/api/queues/calibration?plan=${encodeURIComponent(planId)}`) : { status: 0, body: null };
  const queued = [...(calibration.body?.rows ?? []), ...(hidden.body?.rows ?? [])];
  const decided = queued.filter(row => row.decision && tierLabels[row.decision.tier]);
  const visibleDecided = decided.filter(row => row.structure?.sub_kind !== 'fragment');
  // The editor now holds an unsaved draft (the applied machine label); its leave guard would block navigation, so use a fresh tab.
  hub = await browser.newPage();
  hub.on('dialog', dialog => dialog.accept().catch(() => {}));
  await hub.goto(`${base}/queues`, { waitUntil: 'domcontentloaded', timeout: 120000 });
  await hub.waitForFunction(() => /machine-labelled roofs/i.test(document.body.innerText), { timeout: 120000 });
  // Machine labelled, three calibration bands and the hidden table load from separate routes; count badges only once all five are on the page.
  await hub.waitForFunction(() => document.querySelectorAll('table.queues-table').length >= 5, { timeout: 120000 });
  await new Promise(resolve => setTimeout(resolve, 1000));
  const badges = await hub.evaluate(() => ({
    tiers: [...document.querySelectorAll('.queues-hint-decision')].map(b => ({ label: b.textContent, title: b.title })),
    counts: [...document.querySelectorAll('.queues-decision-count')].map(p => p.textContent),
    toggles: [...document.querySelectorAll('.queues-auto-tier input')].map(i => ({ checked: i.checked, text: i.parentElement.textContent.trim() })),
  }));
  if (decided.length) {
    assert.equal(badges.tiers.length, visibleDecided.length, `one tier badge per queued roof with a decision in view (${decided.length} decided, ${decided.length - visibleDecided.length} fragments out of view)`);
    for (const badge of badges.tiers) {
      assert(Object.values(tierLabels).includes(badge.label), `badge label ${badge.label}`);
      assert(badge.title.startsWith('Machine label, not a reviewer label. Machine label: '), badge.title);
    }
    assert(badges.counts.length >= 1 && badges.counts.every(c => /^Machine labels: \d+ auto, \d+ assist, \d+ review/.test(c)), `count lines: ${badges.counts.join(' | ')}`);
    assert(badges.toggles.every(t => !t.checked && /^Hide auto tier \(\d+\)$/.test(t.text)), `Hide auto tier is offered unchecked: ${JSON.stringify(badges.toggles)}`);
    console.log(`Queues: ${badges.tiers.length} tier badges; ${badges.counts.join(' | ')}; toggles ${badges.toggles.map(t => t.text).join(', ') || 'none (no auto rows)'}`);
  } else {
    skipped.push('/queues tier badges (no queued roof has a decision yet)');
    assert.equal(badges.tiers.length, 0, 'no tier badge without a decision row');
    assert.equal(badges.counts.length, 0, 'no count line without a decision row');
    console.log('Skipped the /queues tier badge check: no queued roof has a decision-policy-v1 row yet.');
  }
  // The Machine labelled table: one row per auto-tier roof the index holds, with the denominators, or an honest empty state.
  const machine = await json(`${base}/api/queues/machine-labelled`);
  assert.equal(machine.status, 200, 'the machine-labelled route answers');
  await hub.waitForFunction(() => document.querySelector('.queues-machine-summary') || document.querySelector('#machine-labelled .queues-state'), { timeout: 60000 });
  const table = await hub.evaluate(() => ({
    summary: document.querySelector('.queues-machine-summary')?.textContent || '',
    rows: document.querySelectorAll('table[data-queue="machine-labelled"] tbody tr').length,
    firstRow: [...document.querySelectorAll('table[data-queue="machine-labelled"] tbody tr')].slice(0, 1).map(tr => [...tr.querySelectorAll('td')].map(td => td.textContent.trim())),
    links: [...document.querySelectorAll('#machine-labelled a')].map(a => a.getAttribute('href')),
    controls: document.querySelectorAll('#machine-labelled input, #machine-labelled button, #machine-labelled select').length,
    empty: document.querySelector('#machine-labelled .queues-state')?.textContent || '',
  }));
  if (machine.body.count > 0) {
    assert(table.rows >= 1, 'the Machine labelled table renders with at least one row');
    assert.equal(table.rows, machine.body.count, 'one row per auto-tier roof the route returns');
    assert(table.summary.startsWith(`${machine.body.count.toLocaleString('en-AU')} machine-labelled roof`) && table.summary.includes(`${machine.body.reviewed_by_person} reviewed by a person`), `the denominators: ${table.summary}`);
    assert(table.links.length && table.links.every(href => /^\/scenes\?scene=.+&object=.+&kind=building&status=all/.test(href)), 'every link opens the editor with status=all');
    assert.equal(table.controls, 0, 'no control in the Machine labelled section');
    assert.equal(table.firstRow[0]?.length, 6, 'six cells: roof, machine label, confidence, human agreement, review status, open');
    assert(/^\d\.\d\d/.test(table.firstRow[0][2]), `confidence first in its cell: ${table.firstRow[0][2]}`);
    assert(/confirmed, 95% interval|not yet judged/.test(table.firstRow[0][3]), `human agreement or not yet judged: ${table.firstRow[0][3]}`);
    console.log(`Machine labelled: ${table.summary.split('.')[0]}; first row ${JSON.stringify(table.firstRow[0])}.`);
  } else {
    skipped.push('Machine labelled table rows (the index has no auto-tier row yet)');
    assert.equal(table.rows, 0);
    assert(/No roof is in the auto tier|no decision policy rows/.test(table.empty), `the empty state names itself: ${table.empty}`);
    console.log('Skipped the Machine labelled row check: the index holds no auto-tier row yet.');
  }
  assert.deepEqual(errors, [], 'no page errors');
  console.log(`Machine labels: ${skipped.length ? `skipped ${skipped.join('; ')}. ` : ''}Passed.`);
} catch (error) {
  console.error(await (hub || page).evaluate(() => document.body.innerText.slice(0, 2000)).catch(() => ''));
  throw error;
} finally { await browser.close(); }
