// Quick reject and one-key relabel in the scene editor: the decision bar shows Needs edit, Not a roof (Backspace), Skip
// and Looks right; a building object gets ten quick-kind chips (1 to 9 and 0) with the current kind highlighted; the
// note explains the keys; typing a digit inside an input never saves. Read-only: nothing is clicked that writes.
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer-core';
const base = process.env.APP_URL || 'http://localhost:3008';
const target = { scene: 'mooroolbark-50011498-17-118448-80406', object: 'a9ac352312f48ff9' };
const browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-sandbox'], protocolTimeout: 240000 });
const page = await browser.newPage(), errors = [], writes = [];
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => { if (r.method() === 'POST' && r.url().includes('/api/scenes')) writes.push(r.url()); });
await page.setViewport({ width: 1400, height: 1000 });
try {
  await page.goto(`${base}/scenes?scene=${target.scene}&object=${target.object}&kind=building&status=all`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForSelector('.scene-svg', { timeout: 60000 });
  await page.waitForFunction(id => location.href.includes(id), { timeout: 30000 }, target.object);
  await page.waitForSelector('[data-testid=scene-reject-decision]', { timeout: 30000 });
  const bar = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll('.scene-decisions button')].map(b => ({ text: b.textContent.replace(/\s+/g, ' ').trim(), disabled: b.disabled, cls: b.className }));
    const chips = [...document.querySelectorAll('.scene-quick-kinds .scene-quick-kind')].map(b => ({ key: b.querySelector('kbd')?.textContent, text: b.textContent.replace(/\s+/g, ' ').trim(), current: b.classList.contains('is-current'), disabled: b.disabled }));
    const note = document.querySelector('.scene-decision-note')?.textContent || '';
    const select = document.querySelector('select[aria-label="Structure kind"]');
    return { buttons, chips, note, kind: select ? select.value : null };
  });
  assert.equal(bar.buttons.length, 4, `four controls in the decision bar: ${JSON.stringify(bar.buttons)}`);
  assert.deepEqual(bar.buttons.map(b => b.text.split(' ')[0]), ['Needs', 'Not', 'Skip', 'Looks'], `order Needs edit, Not a roof, Skip, Looks right: ${bar.buttons.map(b => b.text).join(' | ')}`);
  const rejectButton = bar.buttons[1];
  assert(rejectButton.text.includes('Not a roof') && rejectButton.text.includes('Backspace'), `the reject control names its key: ${rejectButton.text}`);
  assert(!rejectButton.disabled, 'Not a roof is enabled on an open candidate in the edited layer');
  assert.equal(bar.chips.length, 10, `ten quick-kind chips: ${bar.chips.length}`);
  assert.deepEqual(bar.chips.map(c => c.key), ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'], 'chips are keyed 1 to 9 and 0');
  assert(bar.chips.every(c => !c.disabled), 'chips are enabled on an open candidate');
  const currentChips = bar.chips.filter(c => c.current);
  assert(currentChips.length <= 1, 'at most one chip is current');
  if (bar.kind) assert.equal(currentChips.length, 1, `the current structure kind (${bar.kind}) is highlighted`);
  assert(bar.note.includes('Backspace') && bar.note.includes('Digits'), `the note explains the keys: ${bar.note}`);
  // a digit typed inside a text field must not relabel or save; only press when focus is verifiably inside a field,
  // because a key that reaches the page body is a command and would write to a live scene
  const focused = await page.evaluate(() => {
    const field = [...document.querySelectorAll('input[type=text], input:not([type]), textarea')].find(el => !el.disabled && el.offsetParent !== null);
    if (!field) return false;
    field.focus();
    const active = document.activeElement;
    return !!active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA');
  });
  if (focused) {
    const activeTag = await page.evaluate(() => document.activeElement?.tagName);
    assert(['INPUT', 'TEXTAREA'].includes(activeTag), 'focus is inside a text field before the key test');
    await page.keyboard.press('1');
    await new Promise(r => setTimeout(r, 800));
  } else console.log('No focusable text field found; the in-field key test was skipped.');
  assert.equal(writes.length, 0, `no scene write happened during the check (saw ${writes.length})`);
  await page.screenshot({ path: '/tmp/addr-scene-reject.png' });
  assert.equal(errors.length, 0, `no page errors: ${errors.join('; ')}`);
  console.log('Scene reject and quick relabel: Passed.', JSON.stringify({ kind: bar.kind, chips: bar.chips.map(c => c.text) }));
} finally {
  await browser.close();
}
