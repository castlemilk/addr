// Static checks for the Review queues workspace (/queues and /queues/checkpoints/[id]).
// Transpiles the page components with stubbed Next primitives and renders them with fixture
// payloads shaped exactly like the read-only queue APIs. Asserts the three band tables, status
// labels, denominators, the hidden anchor, sentence case, the loading/error wording and that
// nothing on these pages posts or mutates: rows only link into the canonical editor.
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const pathname = '/queues';
let params = { id: 'CP2' };
let parsedMarkdown = '';
const stubMarkdown = {
  parse: markdown => { parsedMarkdown = markdown; return markdown.split('\n').filter(Boolean); },
  render: (blocks, options) => React.createElement('div', { 'data-markdown-lines': blocks.length, 'data-heading-offset': options?.headingOffset }, blocks.some(line => line.startsWith('|')) ? React.createElement('table', null, React.createElement('tbody', null, React.createElement('tr', null, React.createElement('td', null, 'cell')))) : null),
};
// The stub is the default; the real-report checks below switch to the real src/lib/markdown-lite.ts.
let markdownImpl = stubMarkdown;
const markdownLite = { parse: (...a) => markdownImpl.parse(...a), render: (...a) => markdownImpl.render(...a) };
const icon = () => React.createElement('svg', { 'aria-hidden': 'true' });
const lucide = new Proxy({}, { get: () => icon });
const modules = {};
// page.tsx resolves React through this mutable copy so a test can swap useState/useEffect for fixture state.
const pageReact = { ...React };
const load = (file) => {
  if (modules[file]) return modules[file];
  const exports = {};
  const code = ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => {
    if (id.endsWith('.css')) return {};
    if (id === 'react' && file === 'src/app/queues/page.tsx') return pageReact;
    if (id === 'next/link') return { __esModule: true, default: ({ href, children, ...rest }) => React.createElement('a', { href, ...rest }, children) };
    if (id === 'next/navigation') return { usePathname: () => pathname, useParams: () => params };
    if (id === 'lucide-react') return lucide;
    if (id === '@/lib/markdown-lite') return markdownLite;
    if (id === '@/lib/queues') return load('src/lib/queues.ts');
    if (id === '@/components/shell/WorkbenchHeader') return load('src/components/shell/WorkbenchHeader.tsx');
    if (id === './WorkbenchNav') return load('src/components/shell/WorkbenchNav.tsx');
    if (id === '@/components/queues/QueueTable') return load('src/components/queues/QueueTable.tsx');
    if (id === '@/components/queues/CheckpointList') return load('src/components/queues/CheckpointList.tsx');
    return require(id);
  });
  modules[file] = exports;
  return exports;
};

const tables = load('src/components/queues/QueueTable.tsx');
const checkpoints = load('src/components/queues/CheckpointList.tsx');
const queuesPage = load('src/app/queues/page.tsx');
const checkpointPage = load('src/app/queues/checkpoints/[id]/page.tsx');

// ---- fixtures shaped like the API contracts ----
const bandSizes = { low: 30, medium: 31, high: 6 };
const statuses = ['pending', 'confirmed', 'rejected', 'needs_adjustment', 'stale'];
const rows = [];
for (const [band, count] of Object.entries(bandSizes)) {
  for (let i = 0; i < count; i++) {
    const status = band === 'low' ? statuses[i % statuses.length] : 'pending';
    rows.push({
      band, group: `z17:1184${40 + i}:80408`, scene_id: `mooroolbark-50011498-17-1184${40 + i}-80408`, object_id: `obj${band}${i}`,
      review_url: `/scenes?scene=mooroolbark-50011498-17-1184${40 + i}-80408&object=obj${band}${i}&kind=building&status=all&layer=model`,
      area_m2: 120 + i, detector_score: i % 4 === 0 ? null : 0.4 + i / 100, status,
      counted: status === 'confirmed' || status === 'rejected', success: status === 'confirmed' ? true : status === 'rejected' ? false : null,
      address: i % 3 === 0 ? null : i % 2 ? '19 LARNOOK CRESCENT, Mooroolbark VIC 3138' : '3A BLACKBURN ROAD MOOROOLBARK 3138', address_id: i % 3 === 0 ? null : 31114 + i,
    });
  }
}
const band = (groups_total, groups_reviewed, interval) => ({ groups_total, groups_reviewed, successes: groups_reviewed, failures: 0, interval, minimum_groups: 20 });
const calibration = {
  plan_id: '7cfed4d9e02f1c8a', policy: 'object-centroid core tile; calibration only; SAM3 encoded 64 px of neighbouring tiles',
  statement: 'sampled-roof protocol; boundary IoU >= 0.85 only; not apex/height/recall; assumes independent representative areas', iou_success: 0.85,
  bands: { low: band(30, 0, null), medium: band(31, 3, null), high: band(6, 0, null) }, rows,
};
const hidden = {
  version: 'cross-capture-v1', count: 55,
  rows: Array.from({ length: 55 }, (_, i) => ({
    scene_id: 'mooroolbark-50011498-17-118442-80417', object_id: `hidden${i}`, band: 'low', detector_score: 0.42, observed_segments: i % 5 === 0 ? null : 3,
    other_scene_id: 'house-30286', other_object_id: `shown${i}`, other_band: i % 10 === 0 ? 'high' : 'medium', other_detector_score: 0.71, iou: 0.9,
    hidden_url: `/scenes?scene=mooroolbark-50011498-17-118442-80417&object=hidden${i}&kind=building&status=all&layer=model`,
    shown_url: `/scenes?scene=house-30286&object=shown${i}&kind=building&status=all&layer=model`, address: i % 2 ? '27 CAMBRIDGE ROAD MOOROOLBARK 3138' : null,
  })),
};
const checkpointList = [
  { id: 'CP1', title: 'Eyes CP1, 27 Cambridge cluster', created_at: '2026-09-25T13:37:28+00:00', summary: '106 cluster roofs diagnosed.' },
  { id: 'CP2', title: 'Eyes CP2 · Mooroolbark suburb-wide', created_at: '2026-09-25T15:51:14+00:00', summary: '10,072 owned roofs diagnosed; calibration plan 67 roofs' },
  { id: 'CP2-CALIB', title: 'Eyes calibration sample, review queue 7cfed4d9e02f1c8a', created_at: '2026-09-25T15:47:21+00:00', summary: 'Sampled roof for the calibration protocol' },
];
const summary = {
  calibration: { plan_id: '7cfed4d9e02f1c8a', rows: 67, groups_reviewed_by_band: { low: 0, medium: 3, high: 0 }, intervals_by_band: { low: null, medium: null, high: null } },
  hidden: { count: 55 },
  self_review: { run: 'a1b2c3d4', status: 'complete', total: 12, outcomes: { no_safe_gain: 9, needs_evidence: 3, candidate_ready: 0 } },
  roof_review: { reviewed: 12, total: 250 },
  checkpoints: checkpointList,
};
const ready = data => ({ state: 'ready', data });
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const count = (html, re) => (html.match(re) || []).length;

// ---- /queues body with every payload loaded ----
let html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: ready(summary), calibration: ready(calibration), hidden: ready(hidden), onRetry: () => {} }));
for (const [name, size] of Object.entries(bandSizes)) {
  const table = html.match(new RegExp(`<table[^>]*data-band="${name}"[^>]*>.*?</table>`, 's'));
  assert(table, `a ${name} band table`);
  assert.equal(count(table[0], /<tbody>.*?<\/tbody>/s) && count(table[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g), size, `${size} rows in the ${name} table`);
  assert(table[0].includes('Detector (model estimate)'), 'the detector column says it is a model estimate');
}
assert.equal(count(html, /data-band="/g), 3, 'exactly three band tables');
assert(text(html).includes('Calibration sample, plan 7cfed4d9e02f1c8a'), 'calibration heading names the plan');
assert(text(html).includes('67 fixed-hash roofs: 30 low, 31 medium, 6 high. Review each boundary from the image, then confirm with geometry reviewed or reject. Intervals appear at 20 reviewed groups per band.'), 'plain-words calibration intro');
assert(text(html).includes('0 of 30 reviewed') && text(html).includes('3 of 31 reviewed') && text(html).includes('0 of 6 reviewed'), 'per-band progress carries denominators');
assert(text(html).includes('Interval not yet available'), 'unavailable interval wording');
assert(text(html).includes('6 groups cannot reach 20'), 'the high band explains why it can never reach an interval');
for (const label of ['Needs correction', 'Confirmed', 'Rejected', 'Pending', 'Feedback needs recovery']) assert(html.includes(`>${label}<`), `status label ${label}`);
assert(/class="queues-status queues-status-correction"[^>]*>Needs correction</.test(html) && /queues-status-approval"[^>]*>Confirmed</.test(html) && /queues-status-rejection"[^>]*>Rejected</.test(html) && /queues-status-correction"[^>]*>Feedback needs recovery</.test(html), 'semantic colour classes pair with the text label');
assert(html.includes('id="hidden"'), 'hidden section anchor');
assert(text(html).includes('The map shows only the highest-band capture of a roof. These 55 low-evidence scan roofs sit under a medium or high capture of the same roof; both are model estimates.'), 'hidden intro with denominator');
const hiddenTable = html.match(/<table[^>]*data-queue="hidden"[^>]*>.*?<\/table>/s);
assert(hiddenTable && count(hiddenTable[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g) === 55, '55 hidden rows');
assert(count(hiddenTable[0], />Open hidden<\/a>/g) === 55 && count(hiddenTable[0], />Open shown<\/a>/g) === 55, 'hidden and shown links per row');
assert(text(hiddenTable[0]).includes('no lines observed'), 'null observed segments are named, not zero');
assert(text(html).includes('12 of 250 reviewed') && html.includes('href="/reviews?cohort=roof_training_250"'), 'roof training review denominator and link');
assert(html.includes('href="/scenes/processing?view=self-review"') && text(html).includes('9 no safe gain, 3 need evidence, 0 candidate ready'), 'self-review line and link');
assert(text(html).includes('Pipeline checkpoints') && html.includes('href="/queues/checkpoints/CP1"') && html.includes('href="/queues/checkpoints/CP2"') && html.includes('href="/queues/checkpoints/CP2-CALIB"'), 'checkpoint links');
assert.equal(count(html, />Read report</g), 3, 'one Read report link per checkpoint');
assert(html.includes('<time dateTime="2026-09-25T15:51:14+00:00">25 Sep 2026</time>'), 'checkpoint date is formatted without locale dependence');
assert(!/<button|<form|<input|<select/.test(html), 'nothing on the loaded hub page is a control');
assert(!/[A-Z]{4,}/.test(text(html)), `no all-caps text: ${(text(html).match(/\S*[A-Z]{4,}\S*/g) || []).slice(0, 5).join(', ')}`);
assert(text(html).includes('19 Larnook Crescent, Mooroolbark VIC 3138') && text(html).includes('3A Blackburn Road Mooroolbark 3138'), 'addresses render in sentence case, keeping state codes and unit numbers');
assert(text(html).includes('No address match'), 'a missing address is named');
assert(/aria-label="Open roof objlow0 in the editor"/.test(html) && /aria-label="Open 19 Larnook Crescent, Mooroolbark VIC 3138 in the editor"/.test(html) && !/aria-label="Open No address match/.test(html), 'open links are named by address, or by roof id when no address matched');
assert(!text(html).includes(' · ') && !html.includes('→'), 'no dot joins or arrows in text');
assert(/href="\/scenes\?scene=mooroolbark-50011498-17-118440-80408&amp;object=objlow0&amp;kind=building&amp;status=all&amp;layer=model"/.test(html), 'open links use the review_url exactly');
const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map(m => m[1]);
assert(hrefs.every(h => /^\/(scenes|reviews|queues)?(\?|\/|$)/.test(h)), `every link stays inside the app: ${hrefs.find(h => !/^\/(scenes|reviews|queues)?(\?|\/|$)/.test(h))}`);

// ---- interval wording when a band has reached 20 reviewed groups ----
const withInterval = { ...calibration, bands: { ...calibration.bands, medium: band(31, 22, [0.7123, 0.9312]) } };
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: ready(summary), calibration: ready(withInterval), hidden: ready(hidden), onRetry: () => {} }));
assert(text(html).includes('Wilson 95%: 0.71–0.93 (boundary IoU >= 0.85, sampled roofs, not per-house accuracy)'), 'Wilson interval text names what it measures');
assert(text(html).includes('22 of 31 reviewed'), 'progress updates with the band');

// ---- loading states name what is being read ----
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: ready(summary), calibration: { state: 'loading' }, hidden: { state: 'loading' }, onRetry: () => {} }));
assert(text(html).includes('Reading plan 7cfed4d9e02f1c8a and 67 review files'), 'calibration loading names the plan and file count');
assert(text(html).includes('Reading 55 hidden roofs'), 'hidden loading names the count');
assert(!/<table/.test(html), 'no empty tables while loading');
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: { state: 'loading' }, calibration: { state: 'loading' }, hidden: { state: 'loading' }, onRetry: () => {} }));
assert(text(html).includes('Reading the queue summary'), 'summary loading wording');

// ---- no plan yet: the section says so instead of waiting forever ----
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: ready({ ...summary, calibration: null, hidden: { count: null } }), calibration: { state: 'loading' }, hidden: { state: 'loading' }, onRetry: () => {} }));
assert(text(html).includes('No calibration plan has been sampled yet'), 'a null calibration summary is explained');
assert(text(html).includes('Reading hidden roofs from the cross-capture index'), 'a null hidden count falls back to naming the index');
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointList, { checkpoints: [{ id: 'CP0', title: '', created_at: null, summary: null }] }));
assert(/<h3>CP0<\/h3>/.test(html) && text(html).includes('Date not recorded') && !/<time/.test(html), 'null checkpoint fields fall back without crashing');

// ---- errors explain and offer retry, and retry is the only control ----
let retried = [];
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: ready(summary), calibration: { state: 'error', message: 'progress.json is missing' }, hidden: ready(hidden), onRetry: part => retried.push(part) }));
assert(/role="alert"[^>]*>[^<]*progress\.json is missing/.test(html) && html.includes('>Retry calibration</button>'), 'calibration error explains and offers retry');
assert.equal(count(html, /<button/g), 1, 'the retry button is the only control');
assert(!/data-band=/.test(html), 'no band tables while the calibration read failed');

// ---- the full page: shell, header, h1, the read-only line, nothing mutating ----
html = renderToStaticMarkup(React.createElement(queuesPage.default));
assert(html.startsWith('<main class="review-shell queues-shell">') || /<main class="review-shell[^"]*queues-shell/.test(html), 'navy review shell');
assert(/<header class="review-header workbench-header">/.test(html), 'workbench header');
assert(/<a [^>]*aria-current="page"[^>]*>Review queues<\/a>/.test(html), 'the queues workspace is current in the nav');
assert.equal(count(html, /<h1/g), 1, 'one h1');
assert(/<h1[^>]*>Review queues<\/h1>/.test(html), 'h1 is Review queues');
assert(text(html).includes('Each row links into the editor. Nothing on this page approves, rejects or changes a roof.'), 'the read-only line');
assert(text(html).includes('Reading the queue summary'), 'initial render is the loading state');
for (const file of ['src/app/queues/page.tsx', 'src/app/queues/checkpoints/[id]/page.tsx', 'src/components/queues/QueueTable.tsx', 'src/components/queues/CheckpointList.tsx']) {
  const source = readFileSync(file, 'utf8');
  assert(!/method\s*:|POST|PUT|PATCH|DELETE/.test(source), `${file} never posts or mutates`);
  assert(!/·|→|text-transform/.test(source), `${file} has no dot joins, arrows or uppercasing`);
}
const css = readFileSync('src/app/queues/queues.css', 'utf8');
assert(!/text-transform\s*:\s*uppercase|letter-spacing|box-shadow\s*:\s*(?!inset|none)/.test(css), 'no uppercase, tracking or drop shadows in queues.css');
assert(!/font-size\s*:\s*(10|[0-9])px/.test(css), 'nothing below 11 px');
assert(/tabular-nums/.test(css) && /overflow-x\s*:\s*auto/.test(css), 'tabular figures and panel scrolling');

// ---- checkpoint report view ----
const markdown = '# EYES CP2 · Mooroolbark suburb-wide · 2026-09-26\n\nCheckpoint `CP2` | crm-checkpoint-v1\n\n| signal | value |\n| --- | --- |\n| roofs | 10072 |\n';
const entry = { ...checkpointList[1], address_text: '27 CAMBRIDGE ROAD MOOROOLBARK 3138', address_id: 29790, latitude: -37.79044, longitude: 145.31252, report_sha256: 'abc', review_path: 'data/references/eyes-checkpoints/CP2/REVIEW.md' };
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointReport, { id: 'CP2', entry, markdown, address: tables.displayAddress(entry.address_text) }));
assert(/<h1[^>]*>Eyes CP2, Mooroolbark suburb-wide<\/h1>/.test(html), 'h1 is the checkpoint title');
assert.equal(count(html, /<h1/g), 1, 'the report heading is not duplicated from the markdown');
assert(!parsedMarkdown.startsWith('# '), 'the leading markdown heading is stripped before rendering');
assert(parsedMarkdown.includes('| signal | value |'), 'the rest of the markdown reaches the renderer');
assert(/<a href="\/queues"[^>]*>[^<]*Back to queues/.test(html) || /<a href="\/queues">(<svg[^>]*><\/svg>)?Back to queues<\/a>/.test(html), 'Back to queues link');
assert(html.includes('href="/?lat=-37.79044&amp;lon=145.31252&amp;crm=1"') && text(html).includes('Open on map'), 'Open on map link with the anchor address');
assert(text(html).includes('27 Cambridge Road Mooroolbark 3138'), 'anchor address in sentence case');
assert(html.includes('<table>'), 'the rendered markdown is included');
assert(html.includes('data-heading-offset="0"'), 'report sections render as h2 under the page h1 once the markdown title is stripped');
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointReport, { id: 'CP2', entry, markdown: '# Title\n\nbody\n\n# Another top heading\n' }));
assert(html.includes('data-heading-offset="1"'), 'a second # heading keeps the default demotion so the page never has two h1s');
assert(!/<button|<form/.test(html), 'the report has no controls');
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointReport, { id: 'CP9', entry: null, markdown: 'No heading here\n' }));
assert(/<h1[^>]*>CP9<\/h1>/.test(html) && !text(html).includes('Open on map'), 'falls back to the id and omits the map link without coordinates');
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointReport, { id: 'CP9', entry: null, markdown: '# Heading from markdown\n\nbody' }));
assert(/<h1[^>]*>Heading from markdown<\/h1>/.test(html), 'falls back to the markdown heading when the list has no entry');

// ---- checkpoint page shell and loading state ----
html = renderToStaticMarkup(React.createElement(checkpointPage.default));
assert(/<header class="review-header workbench-header">/.test(html) && /aria-current="page"[^>]*>Review queues</.test(html), 'checkpoint page uses the queues workspace header');
assert(text(html).includes('Reading checkpoint CP2'), 'checkpoint loading names the checkpoint');
assert(html.includes('href="/queues"'), 'Back to queues is available while loading');

// ---- checkpoint list empty state ----
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointList, { checkpoints: [] }));
assert(text(html).includes('No checkpoints have been filed yet'), 'empty checkpoint list is explicit');

// ---- the hidden table names the hidden capture, and the self-review line says its outcomes are estimates ----
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: ready(summary), calibration: ready(calibration), hidden: ready(hidden), onRetry: () => {} }));
assert(/queues-secondary">mooroolbark-50011498-17-118442-80417 hidden0</.test(html), 'each hidden row names its own capture and object, not just the shown one');
assert(text(html).includes("Outcomes are the loop's own model estimates, not human reviews."), 'self-review outcomes are labelled model estimates');

// ---- a failed summary does not leave the calibration section waiting forever ----
retried = [];
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: { state: 'error', message: 'summary route 500' }, calibration: { state: 'loading' }, hidden: ready(hidden), onRetry: part => retried.push(part) }));
assert(!text(html).includes('Waiting for the queue summary'), 'no dead waiting state when the summary failed');
assert(/role="alert"[^>]*>The queue summary could not be read, so the calibration plan is unknown: summary route 500/.test(html), 'the calibration section explains the summary failure');
assert.equal(count(html, />Retry summary<\/button>/g), 2, 'retry summary is offered where the reader is looking (calibration section and summary sections)');

// ---- degenerate 200 payloads are refused by the coercers instead of crashing the page ----
for (const [name, coerce, url] of [['summary', tables.coerceSummary, '/api/queues/summary'], ['calibration', tables.coerceCalibration, '/api/queues/calibration?plan=x'], ['hidden', tables.coerceHidden, '/api/queues/hidden'], ['checkpoint', tables.coerceCheckpoint, '/api/checkpoints/CP2']]) {
  for (const bad of [null, undefined, {}, [], 'text', 42, '<html>']) assert.throws(() => coerce(bad, url), err => err.message.includes(url) && /unexpected shape/.test(err.message), `${name} refuses ${JSON.stringify(bad)}`);
}
assert.throws(() => tables.coerceSummary({ ...summary, self_review: undefined }, '/api/queues/summary'), /unexpected shape/, 'a summary without self_review is refused');
assert.throws(() => tables.coerceCalibration({ plan_id: 'x' }, '/api/queues/calibration?plan=x'), /unexpected shape/, 'a calibration payload without rows is refused');
assert.throws(() => tables.coerceCheckpoint({ id: 'CP2' }, '/api/checkpoints/CP2'), /unexpected shape/, 'a checkpoint without markdown text is refused');
assert.deepEqual(tables.coerceSummary(summary, '/api/queues/summary'), summary, 'a well-formed summary passes through unchanged');
assert.deepEqual(tables.coerceCalibration(calibration, '/api/queues/calibration?plan=x').rows, calibration.rows, 'well-formed calibration rows pass through unchanged');
assert.deepEqual(tables.coerceHidden(hidden, '/api/queues/hidden').rows, hidden.rows, 'well-formed hidden rows pass through unchanged');
assert.deepEqual(tables.coerceCheckpoint({ id: 'CP2', markdown: '# x', report: {} }, '/api/checkpoints/CP2'), { id: 'CP2', markdown: '# x' }, 'a checkpoint keeps its id and markdown');
assert.deepEqual(tables.coerceCheckpointList({ checkpoints: [{ id: 'CP1', title: 'T' }, { title: 'no id' }, 'junk'] }, '/api/checkpoints').map(c => c.id), ['CP1'], 'entries without an id are dropped');
assert.deepEqual(tables.coerceCheckpointList({}, '/api/checkpoints'), [], 'a list without checkpoints is empty, never a crash');
// Minimal payloads: missing counts default to zero, non-string addresses to null, missing urls to the canonical editor, and the render does not throw.
const sparseCalibration = tables.coerceCalibration({ plan_id: 'abcdefabcdefabcd', rows: [{ band: 'low', scene_id: 's-1-2', object_id: 'o1', address: 12, status: 'weird_status' }, { band: 'low' }, 'junk'], bands: { low: {}, medium: null } }, '/api/queues/calibration?plan=abcdefabcdefabcd');
assert.equal(sparseCalibration.rows.length, 1, 'rows without scene and object ids are dropped');
assert.equal(sparseCalibration.rows[0].address, null, 'a non-string address becomes null');
assert.equal(sparseCalibration.rows[0].review_url, '/scenes?scene=s-1-2&object=o1&kind=building&status=all&layer=model', 'a missing review_url becomes the canonical editor link');
assert.equal(sparseCalibration.iou_success, 0.85, 'iou_success defaults to the protocol value');
const sparseHidden = tables.coerceHidden({ rows: [{ scene_id: 'a', object_id: 'b', other_scene_id: 'c', other_object_id: 'd' }, {}] }, '/api/queues/hidden');
assert.equal(sparseHidden.count, 1, 'count falls back to the surviving rows');
assert.equal(sparseHidden.rows[0].shown_url, '/scenes?scene=c&object=d&kind=building&status=all&layer=model', 'a missing shown_url becomes the canonical editor link');
const sparseSummary = tables.coerceSummary({ calibration: null, hidden: {}, self_review: {}, roof_review: {}, checkpoints: 'nope' }, '/api/queues/summary');
assert.equal(sparseSummary.hidden.count, null, 'a missing hidden count is null (unknown), never zero');
assert.deepEqual(sparseSummary.checkpoints, [], 'a non-array checkpoint list is empty');
html = renderToStaticMarkup(React.createElement(tables.QueuesBody, { summary: ready(sparseSummary), calibration: ready(sparseCalibration), hidden: ready(sparseHidden), onRetry: () => {} }));
assert(text(html).includes('1 fixed-hash roof: 1 low, 0 medium, 0 high') && text(html).includes('No sampled roofs in the medium band.') && text(html).includes('No address match') && html.includes('>weird status<'), `sparse payloads render with named gaps instead of crashing: ${text(html).slice(0, 300)}`);
assert(text(html).includes('This 1 low-evidence scan roof sits under a medium or high capture'), 'a single hidden roof reads in the singular');
assert(text(html).includes('a b') && text(html).includes('band unknown, detector —, no lines observed'), 'unknown hidden bands and scores are named, never invented');
assert(text(html).includes('Interval not available: 0 groups cannot reach 20') && text(html).includes('No self-review batch has run yet.') && text(html).includes('0 of 0 reviewed'), 'zero counts keep their denominators');

// ---- titles from report.json arrive shouting; the list and report show them in sentence case ----
assert.equal(checkpoints.displayTitle('EYES CP2 · Mooroolbark suburb-wide · 2026-09-26', 'CP2'), 'Eyes CP2, Mooroolbark suburb-wide, 2026-09-26', 'all-caps words are sentence-cased and dot joins become commas');
assert.equal(checkpoints.displayTitle('EYES calibration sample · review queue 7cfed4d9e02f1c8a', 'CP2-CALIB'), 'Eyes calibration sample, review queue 7cfed4d9e02f1c8a');
assert.equal(checkpoints.displayTitle('CRM POI VIC CP2-CALIB SAM3', 'x'), 'CRM POI VIC CP2-CALIB SAM3', 'short codes, ids and alphanumerics are left alone');
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointList, { checkpoints: [{ id: 'CP1', title: 'EYES CP1 · 27 Cambridge cluster · 2026-09-25', created_at: '2026-09-25T13:37:28+00:00', summary: 'x' }] }));
assert(/<h3>Eyes CP1, 27 Cambridge cluster, 2026-09-25<\/h3>/.test(html) && !/[A-Z]{4,}/.test(text(html)), 'the real title shape renders without shouting');

// ---- a report whose REVIEW.md is missing says so instead of rendering an empty body ----
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointReport, { id: 'CP7', entry: { id: 'CP7', title: 'T', created_at: null, summary: null }, markdown: '' }));
assert(/role="status"[^>]*>Checkpoint CP7 has no REVIEW.md report on this machine; only its index entry above was filed\./.test(html), 'empty markdown is explained');
assert(!/<h1[^>]*>Checkpoint CP7<\/h1>/.test(html) && /<h1[^>]*>T<\/h1>/.test(html), 'the index title still leads');

// ---- a checkpoint id the URL layer could not decode does not crash the page ----
params = { id: '100%' };
html = renderToStaticMarkup(React.createElement(checkpointPage.default));
assert(/<h1[^>]*>Checkpoint 100%<\/h1>/.test(html), 'a malformed percent-encoding is shown as-is instead of throwing URIError');
params = { id: 'CP2' };

// ---- the real markdown reader on real-shaped reports: one h1, sections as h2, tables intact ----
markdownImpl = load('src/lib/markdown-lite.ts');
const realShaped = '# EYES CP2 · Mooroolbark suburb-wide · 2026-09-26\n\nCheckpoint `CP2` | crm-checkpoint-v1 | address 29790\n\n## What this checkpoint is\n\nSecond checkpoint.\n\n### Roof form\n\n- one\n- two\n\n| signal | value |\n| --- | --- |\n| roofs | 10072 |\n';
html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointReport, { id: 'CP2', entry, markdown: realShaped, address: tables.displayAddress(entry.address_text) }));
assert.equal(count(html, /<h1/g), 1, 'one h1 with the real reader');
assert(/<h1[^>]*>Eyes CP2, Mooroolbark suburb-wide<\/h1>/.test(html), 'index title in sentence case');
assert(count(html, /<h2/g) >= 2 && count(html, /<h3/g) === 0, 'report sections render as h2 directly under the page h1');
assert(/<table><thead><tr><th>signal<\/th><th>value<\/th>/.test(html) && html.includes('<td>10072</td>'), 'the report table survives');
assert(!/<h1[^>]*>Checkpoint/.test(html) && !text(html).includes('Checkpoint `CP2`'), 'the markdown title line is stripped and inline code renders as code');
for (const id of ['CP1', 'CP2', 'CP2-CALIB']) {
  const file = `data/references/eyes-checkpoints/${id}/REVIEW.md`;
  if (!existsSync(file)) continue;
  html = renderToStaticMarkup(React.createElement(checkpoints.CheckpointReport, { id, entry: null, markdown: readFileSync(file, 'utf8') }));
  assert.equal(count(html, /<h1/g), 1, `${id}: exactly one h1`);
  assert(!/[A-Z]{4,}/.test(html.match(/<h1[^>]*>(.*?)<\/h1>/)[1]), `${id}: the h1 is not shouting`);
  assert(!/<h1[^>]*>.* · /.test(html), `${id}: no dot joins in the h1`);
  assert(count(html, /<h2/g) >= 1 && count(html, /<h3/g) === 0, `${id}: sections are h2`);
}
markdownImpl = stubMarkdown;

// ---- building class hints on the hub page: badges per row, evidence tooltips, the Hide flagged filter ----
// The page owns these tables (QueueTable.tsx has no badge column); hooks are stubbed so the loaded state renders.
const q = load('src/lib/queues.ts');
const treeHint = { hints: [{ flag: 'tree-like', evidence: { exg_green_share: 0.62, solidity: 0.71, tree_overlap_iou: 0.35, texture: 1200, vertex_density: 11.6, rule: 'r' } }], class_hint: 'tree-like' };
const shedHint = { hints: [{ flag: 'secondary-structure', evidence: { area_m2: 63.379, largest_same_property_area: 258.1, ratio: 4.07, source: 'vicmap-property', property_pfi: '1348074', largest_object: 'x', rule: 'r' } }], class_hint: 'outbuilding' };
const shopHint = { hints: [{ flag: 'non-residential-context', evidence: { poi_within_30m: [{ name: 'Kiloran Park', category: 'park', distance_m: 16.47 }], landuse: [{ tag: 'leisure=park', name: 'Kiloran Park', distance_m: 0 }], landuse_status: 'available', address_kind: null, site_names: ['KILORAN PARK'], area_m2: 462.4, rule: 'r' } }], class_hint: 'commercial' };
const noHint = { hints: [], class_hint: null };
const hintedRows = rows.map(row => ({ ...row, ...(row.object_id === 'objlow0' ? treeHint : row.object_id === 'objlow1' ? shedHint : row.object_id === 'objmedium2' ? shopHint : noHint) }));
const hintedCalibration = { ...calibration, rows: hintedRows };
const hintedHidden = { ...hidden, rows: hidden.rows.map((row, i) => ({ ...row, ...(i === 0 ? treeHint : i === 1 ? shopHint : noHint) })) };
// Page state order: summary, calibration, hidden, attempts, hideFlagged, fragmentViews (per-table Show fragments), autoViews
// (per-table Hide auto tier), machine (the Machine labelled queue), machineAttempts; the view maps are {} when omitted, the
// machine queue is loading and its attempts 0.
const renderPage = states => {
  const all = [...states]; while (all.length < 7) all.push({}); if (all.length < 8) all.push({ state: 'loading' }); if (all.length < 9) all.push(0);
  let i = 0;
  pageReact.useState = () => [all[i++], () => {}];
  pageReact.useEffect = () => {};
  try { return renderToStaticMarkup(React.createElement(queuesPage.default)); } finally { pageReact.useState = React.useState; pageReact.useEffect = React.useEffect; }
};
const attempts = { summary: 0, calibration: 0, hidden: 0 };
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, false]);
for (const [name, size] of Object.entries(bandSizes)) {
  const table = html.match(new RegExp(`<table[^>]*data-band="${name}"[^>]*>.*?</table>`, 's'));
  assert(table, `a ${name} band table on the page`);
  assert.equal(count(table[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g), size, `${size} rows in the page's ${name} table with the filter off`);
  assert(table[0].includes('Agent hints (not labels)'), 'the hint column says what it is');
}
const hiddenOnPage = html.match(/<table[^>]*data-queue="hidden"[^>]*>.*?<\/table>/s);
assert(hiddenOnPage && count(hiddenOnPage[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g) === 55, '55 hidden rows on the page with the filter off');
for (const label of ['Tree-like', 'Secondary structure', 'Non-residential context', 'Commercial']) assert(html.includes(`>${label}</span>`), `badge ${label}`);
assert.equal(count(html, /class="queues-hint queues-hint-tree-like"/g), 2, 'one tree-like badge in calibration, one in hidden');
assert.equal(count(html, /class="queues-hint queues-hint-commercial"/g), 2, 'commercial badges follow the class hint');
assert(/queues-hint-tree-like" title="Agent hint, not a label\. green share 0\.62 of pixels, solidity 0\.71, tree overlap IoU 0\.35, texture 1200\."/.test(html), 'tree-like tooltip carries the evidence numbers');
assert(/queues-hint-secondary-structure" title="Agent hint, not a label\. 63 m² vs 258 m² largest roof on the same property \(Vicmap property 1348074\), ratio 4\.1\."/.test(html), 'secondary-structure tooltip names the partner roof and ratio');
assert(/queues-hint-non-residential-context" title="Agent hint, not a label\. Kiloran Park \(park\) 16 m; leisure=park Kiloran Park 0 m; site name Kiloran Park\."/.test(html), 'non-residential tooltip lists POI, land use and site name');
assert(/queues-hint-commercial" title="Agent hint, not a label\. Roof of 462 m² with non-residential context or a business\/large address kind\."/.test(html), 'commercial tooltip states the rule with the area');
assert(/<input type="checkbox"(?![^>]*checked)[^>]*\/?> Hide flagged<\/label>/.test(html), 'the Hide flagged checkbox is present and off by default');
assert.equal(count(html, /<input/g), 1, 'the filter is the only input');
assert(text(html).includes('Badges are agent hints, not labels'), 'the page says what badges are');
assert(!/[A-Z]{4,}/.test(text(html)), `no all-caps text on the hinted page: ${(text(html).match(/\S*[A-Z]{4,}\S*/g) || []).slice(0, 5).join(', ')}`);
assert(!text(html).includes(' · ') && !html.includes('→'), 'no dot joins or arrows on the hinted page');
assert(!html.includes('title="Agent hint, not a label. ."'), 'no empty evidence tooltips');
const hrefsHinted = [...html.matchAll(/href="([^"]+)"/g)].map(m => m[1]);
assert(hrefsHinted.every(h => /^\/(scenes|reviews|queues)?(\?|\/|$)/.test(h)), 'every link on the hinted page stays inside the app');
// Filter on: flagged rows leave, denominators say how many, statuses and links of the rest are untouched.
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, true]);
assert(/<input type="checkbox" checked=""[^>]*\/?> Hide flagged<\/label>/.test(html), 'the checkbox reflects the filter state');
const lowTable = html.match(/<table[^>]*data-band="low"[^>]*>.*?<\/table>/s), mediumTable = html.match(/<table[^>]*data-band="medium"[^>]*>.*?<\/table>/s), highTable = html.match(/<table[^>]*data-band="high"[^>]*>.*?<\/table>/s);
assert.equal(count(lowTable[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g), 28, 'two flagged low roofs are hidden');
assert.equal(count(mediumTable[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g), 30, 'one flagged medium roof is hidden');
assert.equal(count(highTable[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g), 6, 'the high band is untouched');
assert(text(html).includes('2 of 30 flagged rows hidden by the filter') && text(html).includes('1 of 31 flagged rows hidden by the filter'), 'the filter says how many rows it removed, with denominators');
assert(!text(html).includes('of 6 flagged rows'), 'no filter note where nothing was hidden');
assert(text(html).includes('0 of 30 reviewed') && text(html).includes('3 of 31 reviewed'), 'review progress keeps the full denominators');
assert(!/queues-hint-tree-like|queues-hint-secondary-structure|queues-hint-non-residential-context/.test(html), 'no badges remain once flagged rows are hidden');
const hiddenFiltered = html.match(/<table[^>]*data-queue="hidden"[^>]*>.*?<\/table>/s);
assert(hiddenFiltered && count(hiddenFiltered[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g) === 53, '53 hidden rows after two flagged ones leave');
assert(text(html).includes('These 55 low-evidence scan roofs sit under a medium or high capture of the same roof; both are model estimates. 2 of 55 flagged rows hidden by the filter.'), 'the hidden intro keeps its denominator and names the filter effect');
assert(html.includes('href="/scenes?scene=mooroolbark-50011498-17-118442-80408&amp;object=objlow2&amp;kind=building&amp;status=all&amp;layer=model"'), 'unflagged rows still link into the editor');
// Everything flagged in a band: the band says so instead of showing an empty table.
const allFlagged = { ...hintedCalibration, rows: hintedRows.map(row => (row.band === 'high' ? { ...row, ...treeHint } : row)) };
html = renderPage([ready(summary), ready(allFlagged), ready(hintedHidden), attempts, true]);
assert(text(html).includes('Every sampled roof in the high band is flagged and hidden by the filter.'), 'an emptied band explains itself');
assert(!/data-band="high"/.test(html), 'no empty high table');
// Loading and error states still render with the filter present, and nothing else is a control besides retry buttons.
html = renderPage([ready(summary), { state: 'error', message: 'plan.json is missing' }, { state: 'loading' }, attempts, false]);
assert(/role="alert"[^>]*>[^<]*plan\.json is missing/.test(html) && html.includes('>Retry calibration</button>'), 'calibration error on the page explains and offers retry');
assert(text(html).includes('Reading 55 hidden roofs'), 'hidden loading names the count on the page');
assert.equal(count(html, /<button/g), 1, 'retry is the only button');
// ---- structure-kind hints (structure-kinds-v3): one more quiet badge, a count line, Hide flagged covers them, fragments hide by default ----
const container = { sub_kind: 'shipping-container', confidence: 'moderate', rule: 'r', evidence: { geometry: { rect_width_m: 2.4, rect_length_m: 6.1 }, appearance: { rib_anisotropy: 0.42 }, context: { attached_to_larger_roof: false }, sam3: { prompts: { house: { best_iou: 0.31, score: 0.12 } }, image: 'clahe' } } };
const fragment = { sub_kind: 'fragment', confidence: 'weak', rule: 'r', evidence: { geometry: { area_m2: 3.1 }, context: { attached_to_larger_roof: true } } };
const shed = { ...container, sub_kind: 'shed', confidence: 'strong' };
const notSecondary = { ...container, sub_kind: 'not-secondary', confidence: 'strong' };
const stack = { sub_kind: 'container-stack', confidence: 'strong', rule: 'r', evidence: { geometry: { rect_width_m: 4.9, rect_length_m: 12.2, container_widths: 2 }, appearance: { rib_anisotropy: 0.4 }, context: { attached_to_larger_roof: false } } };
const poolCover = { sub_kind: 'pool-cover', confidence: 'moderate', rule: 'r', evidence: { geometry: { rect_width_m: 4, rect_length_m: 9 }, appearance: { blue_share: 0.72 }, context: { attached_to_larger_roof: false } } };
const carport = { sub_kind: 'carport', confidence: 'weak', rule: 'r', evidence: { geometry: { rect_width_m: 3, rect_length_m: 5.5 }, appearance: { driveway_like_share_3m: 0.45 }, context: { attached_to_larger_roof: true } } };
const classroom = { sub_kind: 'portable-classroom', confidence: 'strong', rule: 'r', evidence: { geometry: { rect_width_m: 7.2, rect_length_m: 14.4 } } };
// objlow1 already carries the secondary-structure flag and gains a container; objhigh0 is structure-only; objhigh1 is a
// neutral not-secondary (no badge, not flagged); two garages sit on medium rows; the v2 kinds sit on low and medium rows.
const structuredRows = hintedRows.map(row => row.object_id === 'objlow1' ? { ...row, structure: container }
  : row.object_id === 'objhigh0' ? { ...row, structure: fragment } : row.object_id === 'objhigh1' ? { ...row, structure: notSecondary }
  : row.object_id === 'objmedium3' || row.object_id === 'objmedium4' ? { ...row, structure: { ...container, sub_kind: 'garage' } }
  : row.object_id === 'objlow2' ? { ...row, structure: stack } : row.object_id === 'objlow3' ? { ...row, structure: poolCover }
  : row.object_id === 'objmedium5' ? { ...row, structure: carport } : row.object_id === 'objmedium6' ? { ...row, structure: classroom } : row);
const structuredCalibration = { ...calibration, rows: structuredRows };
const structuredHidden = { ...hintedHidden, rows: hintedHidden.rows.map((row, i) => (i === 2 ? { ...row, structure: shed } : i === 3 ? { ...row, structure: fragment } : row)) };
// Fragments (structure-kinds fragment rows) stay out of every table by default; the denominators state both counts.
html = renderPage([ready(summary), ready(structuredCalibration), ready(structuredHidden), attempts, false]);
const tableRows = (name, attribute = 'data-band') => { const table = html.match(new RegExp(`<table[^>]*${attribute}="${name}"[^>]*>.*?</table>`, 's')); return count(table[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g); };
assert.equal(tableRows('low'), 30, 'low band: structure hints hide nothing with the filter off');
assert.equal(tableRows('medium'), 31, 'medium band: structure hints hide nothing with the filter off');
assert.equal(tableRows('high'), 5, 'high band: the fragment row is out of view by default');
assert.equal(tableRows('hidden', 'data-queue'), 54, 'hidden table: its fragment row is out of view by default');
const highHeading = () => text(html.slice(html.indexOf('id="queues-band-high"'), html.indexOf('<table', html.indexOf('id="queues-band-high"'))));
assert(highHeading().includes('0 of 6 reviewed') && highHeading().includes('6 roofs, 1 fragment hidden'), `the high band heading states both counts: ${highHeading()}`);
assert(text(html).includes('These 55 low-evidence scan roofs sit under a medium or high capture of the same roof; both are model estimates. 55 roofs, 1 fragment hidden.'), 'the hidden intro states both counts');
assert(!/30 roofs|31 roofs/.test(text(html)), 'bands without fragments say nothing about them');
assert.equal(count(html, /Show fragments \(1\)<\/label>/g), 2, 'one quiet Show fragments checkbox per table that has fragments (high band and hidden), none elsewhere');
assert.equal(count(html, /<input type="checkbox"(?![^>]*checked)[^>]*\/?> Show fragments/g), 2, 'both fragment checkboxes are unchecked by default');
assert(!html.includes('objhigh0') && !html.includes('object=hidden3&'), 'fragment rows are absent from the markup, not just styled away');
assert(!/queues-hint-fragment/.test(html), 'no fragment badges while fragments are hidden');
for (const label of ['Shipping container', 'Container stack', 'Garage', 'Shed', 'Carport', 'Portable classroom', 'Pool cover']) assert(html.includes(`>${label}</span>`), `structure badge ${label}`);
assert.equal(count(html, /class="queues-hint queues-hint-shipping-container queues-hint-structure"/g), 1, 'structure badges carry the shared structure class');
assert.equal(count(html, /class="queues-hint queues-hint-garage queues-hint-structure"/g), 2);
assert.equal(count(html, /class="queues-hint queues-hint-container-stack queues-hint-structure"/g), 1);
assert.equal(count(html, /class="queues-hint queues-hint-pool-cover queues-hint-structure"/g), 1);
assert(/queues-hint-shipping-container queues-hint-structure" title="Agent hint, not a label\. Shipping container, moderate confidence: 2\.4 by 6\.1 m, rib anisotropy 0\.42, not attached to a larger roof, SAM3 house score 0\.12\."/.test(html), 'the structure tooltip carries confidence and plain-words evidence');
assert(/queues-hint-container-stack queues-hint-structure" title="Agent hint, not a label\. Container stack, strong confidence: 4\.9 by 12\.2 m, about 2 container widths, rib anisotropy 0\.40, not attached to a larger roof\."/.test(html), 'the container stack tooltip states the rectangle and container widths');
assert(/queues-hint-pool-cover queues-hint-structure" title="Agent hint, not a label\. Pool cover, moderate confidence: 4\.0 by 9\.0 m, blue share 0\.72 of pixels, not attached to a larger roof\."/.test(html), 'the pool cover tooltip states the blue share');
assert(/queues-hint-carport queues-hint-structure" title="Agent hint, not a label\. Carport, weak confidence: 3\.0 by 5\.5 m, driveway-like share 0\.45 within 3 m, attached to a larger roof\."/.test(html), 'the carport tooltip states the driveway share and attachment');
const lowRow = html.match(/<tr>(?:(?!<\/tr>).)*objlow1(?:(?!<\/tr>).)*<\/tr>/s)[0];
assert(/>Secondary structure<\/span>.*>Shipping container<\/span>/s.test(lowRow), 'the structure badge sits after the class badges in the same hint cell');
assert(text(html).includes('Structure hints: 1 shipping container, 1 container stack, 2 garages, 1 carport, 1 portable classroom, 1 pool cover, 1 fragment. Agent hints, not labels.'), `calibration count line: ${(text(html).match(/Structure hints:[^.]*\./g) || []).join(' | ')}`);
assert(text(html).includes('Structure hints: 1 shed, 1 fragment. Agent hints, not labels.'), 'hidden count line');
assert.equal(count(html, /queues-structure-count/g), 2, 'exactly two count lines');
assert(!/Structure hints: 0 /.test(text(html)), 'no zero counts');
assert(!/[A-Z]{4,}/.test(text(html)), 'no all-caps text on the structured page');
assert(!text(html).includes(' · ') && !html.includes('→'), 'no dot joins or arrows on the structured page');
assert(text(html).includes('structure kinds (shipping container, container stack, shed, garage, carport, pergola or shade, water tank, portable classroom, pool cover, fragment)'), 'the page introduction names the structure kinds as hints');
assert(text(html).includes('Fragments are pieces of an outline rather than buildings, so each table keeps them out of view until its Show fragments checkbox is ticked.'), 'the introduction explains the default');
// Show fragments on for the high band and the hidden table: the rows return, the denominators say "shown", the checkbox reflects it.
html = renderPage([ready(summary), ready(structuredCalibration), ready(structuredHidden), attempts, false, { high: true, hidden: true }]);
assert.equal(tableRows('high'), 6, 'the high band shows its fragment when asked');
assert.equal(tableRows('hidden', 'data-queue'), 55, 'the hidden table shows its fragment when asked');
assert(text(html).includes('6 roofs, 1 fragment shown') && text(html).includes('55 roofs, 1 fragment shown'), 'the denominators say the fragments are shown');
assert.equal(count(html, /<input type="checkbox" checked=""[^>]*\/?> Show fragments \(1\)<\/label>/g), 2, 'both fragment checkboxes reflect the shown state');
assert(html.includes('objhigh0') && html.includes('object=hidden3&'), 'the fragment rows are back');
assert.equal(count(html, /queues-hint-fragment/g), 2, 'their fragment badges render (one calibration, one hidden)');
assert(/queues-hint-fragment queues-hint-structure" title="Agent hint, not a label\. Fragment, weak confidence: 3 m², attached to a larger roof\."/.test(html), 'the fragment tooltip');
// Hide flagged with fragments hidden: flagged rows among the visible ones leave; a fragment is not counted twice.
html = renderPage([ready(summary), ready(structuredCalibration), ready(structuredHidden), attempts, true]);
assert.equal(tableRows('high'), 5, 'the high band keeps its five unflagged roofs; the not-secondary row stays');
assert(text(html).includes('6 roofs, 1 fragment hidden') && !text(html).includes('of 6 flagged rows'), 'a hidden fragment is not also reported as a flagged row');
assert(text(html).includes('4 of 30 flagged rows hidden by the filter') && text(html).includes('5 of 31 flagged rows hidden by the filter'), 'the low and medium bands say what the filter removed, including the v2 kinds');
assert(html.includes('objhigh1') && !html.includes('objhigh0'), 'objhigh1 (not-secondary) remains, objhigh0 (fragment) is out of view');
assert(!/queues-hint-structure/.test(html), 'no structure badges remain once flagged rows are hidden');
assert.equal(tableRows('hidden', 'data-queue'), 51, '55 hidden rows minus one fragment, two class-flagged and one structure-flagged');
assert(text(html).includes('55 roofs, 1 fragment hidden. 3 of 54 flagged rows hidden by the filter.'), 'the hidden intro states both counts with the filter denominator over the visible rows');
assert(text(html).includes('Structure hints: 1 shipping container, 1 container stack, 2 garages, 1 carport, 1 portable classroom, 1 pool cover, 1 fragment.'), 'the count line is an inventory of the queue, not of the filtered view');
// Hide flagged with fragments shown: a fragment is flagged, so the filter takes it and says so.
html = renderPage([ready(summary), ready(structuredCalibration), ready(structuredHidden), attempts, true, { high: true, hidden: true }]);
assert.equal(tableRows('high'), 5);
assert(text(html).includes('6 roofs, 1 fragment shown') && text(html).includes('1 of 6 flagged rows hidden by the filter'), 'the shown fragment is then hidden by the flagged filter, with the full denominator');
assert.equal(tableRows('hidden', 'data-queue'), 51);
assert(text(html).includes('4 of 55 flagged rows hidden by the filter'), 'the hidden table counts its fragment among the flagged rows once shown');
// A band whose only rows are fragments says so instead of "no sampled roofs".
const fragmentOnly = { ...calibration, rows: structuredRows.map(row => (row.band === 'high' ? { ...row, structure: fragment } : row)) };
html = renderPage([ready(summary), ready(fragmentOnly), ready(structuredHidden), attempts, false]);
assert(text(html).includes('Every sampled roof in the high band is a fragment, hidden until shown.'), 'the empty state names the fragments');
assert(text(html).includes('6 roofs, 6 fragments hidden') && html.includes('Show fragments (6)</label>'), 'plural fragments');
const fragmentCss = readFileSync('src/app/queues/queues.css', 'utf8');
assert(/\.queues-fragments/.test(fragmentCss), 'queues.css styles the fragment checkbox');
// No structure rows at all: no count line, nothing else changes.
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, false]);
assert.equal(count(html, /queues-structure-count/g), 0, 'no count line without structure hints');
assert.equal(count(html, /queues-hint-structure/g), 0);
assert(!html.includes('Show fragments ('), 'no fragment checkbox without fragments');
const structureCss = readFileSync('src/app/queues/queues.css', 'utf8');
assert(/\.queues-hint-structure/.test(structureCss) && /\.queues-hint-fragment/.test(structureCss), 'queues.css styles the structure and fragment badge classes');
assert(/structureSummary\(/.test(readFileSync('src/app/queues/page.tsx', 'utf8')), 'the page uses the shared structure summary');

// The page reads hints back off the raw API document even though the shared coercers drop them.
const rawCalibration = { ...calibration, rows: rows.map(row => (row.object_id === 'objlow0' ? { ...row, hints: treeHint.hints, class_hint: 'tree-like' } : row)) };
const picked = q.pickHints(rawCalibration);
assert.deepEqual([...picked.keys()], ['mooroolbark-50011498-17-118440-80408/objlow0']);
assert.deepEqual(picked.get('mooroolbark-50011498-17-118440-80408/objlow0'), treeHint);
assert(!('hints' in tables.coerceCalibration(rawCalibration, '/api/queues/calibration?plan=x').rows[0]), 'the shared coercer drops hints, so the page must re-attach them');
const pageSource = readFileSync('src/app/queues/page.tsx', 'utf8');
assert(/pickHints\(/.test(pageSource) && /hintBadges\(/.test(pageSource) && /isFlagged\(/.test(pageSource), 'the page uses the shared hint helpers rather than its own rules');
assert(!/text-transform|letter-spacing/.test(readFileSync('src/app/queues/queues.css', 'utf8')), 'badge styles do not shout');

// ---- decision-policy-v1 machine labels: a tier badge per decided row, a count line per table, Hide auto tier per table ----
const decisionOf = (tier, confidence, label = { kind: 'building', structure_sub_kind: 'garage', class_hint: 'outbuilding', roof_form: 'pitched', roof_type: 'gabled' }, reasons = ['small footprint beside the driveway'], extra = {}) => ({ tier, confidence, machine_label: label, reasons, interval: null, score_interval: null, review: null, ...extra });
const autoGarage = decisionOf('auto', 0.91), assistShed = decisionOf('assist', 0.62, { kind: 'building', structure_sub_kind: 'shed', class_hint: 'outbuilding', roof_form: null, roof_type: null }, []),
  reviewHouse = decisionOf('review', 0.35, { kind: 'building', structure_sub_kind: null, class_hint: 'house', roof_form: 'pitched', roof_type: 'hipped' }, ['two candidates overlap', 'weak evidence']);
// Low band: three auto (objlow2, objlow4, objlow5), one assist, one review; objlow1 keeps its container and gains a review decision.
// Medium band: one auto, the fragment-free high band has no decisions; the hidden table has two auto rows, one of them a fragment.
const decidedRows = structuredRows.map(row => row.object_id === 'objlow2' || row.object_id === 'objlow4' || row.object_id === 'objlow5' ? { ...row, decision: autoGarage }
  : row.object_id === 'objlow6' ? { ...row, decision: assistShed } : row.object_id === 'objlow1' ? { ...row, decision: reviewHouse }
  : row.object_id === 'objmedium7' ? { ...row, decision: autoGarage } : row);
const decidedCalibration = { ...calibration, rows: decidedRows };
const decidedHidden = { ...structuredHidden, rows: structuredHidden.rows.map((row, i) => (i === 3 || i === 5 ? { ...row, decision: autoGarage } : i === 6 ? { ...row, decision: reviewHouse } : row)) };
html = renderPage([ready(summary), ready(decidedCalibration), ready(decidedHidden), attempts, false]);
assert.equal(tableRows('low'), 30, 'auto rows stay in view by default: the reviewer sees them until they decide otherwise');
assert.equal(tableRows('medium'), 31); assert.equal(tableRows('high'), 5); assert.equal(tableRows('hidden', 'data-queue'), 54, 'only the fragments are out of view');
for (const label of ['Auto', 'Assist', 'Review']) assert(html.includes(`>${label}</span>`), `tier badge ${label}`);
assert.equal(count(html, /class="queues-hint queues-hint-tier-auto queues-hint-decision"/g), 5, 'five auto badges in view (three low, one medium, one hidden; the hidden fragment is out of view)');
assert.equal(count(html, /class="queues-hint queues-hint-tier-assist queues-hint-decision"/g), 1);
assert.equal(count(html, /class="queues-hint queues-hint-tier-review queues-hint-decision"/g), 2, 'one review badge in the low band, one in the hidden table');
assert(/queues-hint-tier-auto queues-hint-decision" title="Machine label, not a reviewer label\. Machine label: garage; auto tier, confidence 0\.91\. Reasons: small footprint beside the driveway\."/.test(html), 'the auto tooltip carries the machine label, tier, confidence and reasons');
assert(/queues-hint-tier-assist queues-hint-decision" title="Machine label, not a reviewer label\. Machine label: shed; assist tier, confidence 0\.62\."/.test(html), 'no Reasons clause without reasons');
assert(/queues-hint-tier-review queues-hint-decision" title="Machine label, not a reviewer label\. Machine label: house; review tier, confidence 0\.35\. Reasons: two candidates overlap, weak evidence\."/.test(html), 'the review tooltip');
const decidedLowRow = html.match(/<tr>(?:(?!<\/tr>).)*objlow1(?:(?!<\/tr>).)*<\/tr>/s)[0];
assert(/>Secondary structure<\/span>.*>Shipping container<\/span>.*>Review<\/span>/s.test(decidedLowRow), 'the tier badge sits last in the hint cell, after the class and structure badges');
// Count lines: one per table that has decisions, every tier with zeros, the rows without one, and the not-a-reviewer-label copy.
assert(text(html).includes('Machine labels: 3 auto, 1 assist, 1 review; 25 roofs without one. Machine labels, not reviewer labels.'), `low band count line: ${(text(html).match(/Machine labels:[^.]*\./g) || []).join(' | ')}`);
assert(text(html).includes('Machine labels: 1 auto, 0 assist, 0 review; 30 roofs without one. Machine labels, not reviewer labels.'), 'medium band count line keeps the zeros');
assert(text(html).includes('Machine labels: 2 auto, 0 assist, 1 review; 52 roofs without one. Machine labels, not reviewer labels.'), 'the hidden count line is an inventory of the queue, fragments included');
assert.equal(count(html, /queues-decision-count/g), 3, 'exactly three count lines: low, medium and hidden; none for the high band');
assert(!/Machine labels: 0 auto, 0 assist, 0 review/.test(text(html)), 'no count line without decisions');
// Hide auto tier: one quiet native checkbox per table with auto rows, unchecked by default, the count in its label.
assert.equal(count(html, /<input type="checkbox"(?![^>]*checked)[^>]*\/?> Hide auto tier \(3\)<\/label>/g), 1, 'the low band offers Hide auto tier (3), unchecked');
assert.equal(count(html, /Hide auto tier \(1\)<\/label>/g), 2, 'the medium band and the hidden table (its fragment out of view) each offer Hide auto tier (1)');
assert.equal(count(html, /Hide auto tier \(/g), 3, 'no checkbox where a table has no auto rows');
assert(/queues-auto-tier" title="Hides rows the decision policy placed in the auto tier\. A view choice only; nothing is decided by it\."/.test(html), 'the checkbox says it decides nothing');
assert.equal(count(html, /<input/g), 1 + 2 + 3, 'Hide flagged, two Show fragments and three Hide auto tier are the only inputs');
assert(text(html).includes('Tier badges (Auto, Assist, Review) carry the decision policy\'s machine label: the auto tier proceeds without a human, assist and review need one.'), 'the introduction explains the tiers');
assert(text(html).includes('applying one to an object happens only in the editor'), 'the introduction says where a machine label is applied');
assert(!/[A-Z]{4,}/.test(text(html)), 'no all-caps text on the decided page');
assert(!text(html).includes(' · ') && !html.includes('→'), 'no dot joins or arrows on the decided page');
assert.equal(count(html, /<button/g), 0, 'no button on the decided page: nothing here applies a machine label');
// Hide auto tier on for the low band and the hidden table: auto rows leave, the notes state the denominators, other tables are untouched.
html = renderPage([ready(summary), ready(decidedCalibration), ready(decidedHidden), attempts, false, {}, { low: true, hidden: true }]);
assert.equal(tableRows('low'), 27, 'three auto rows leave the low band');
assert.equal(tableRows('medium'), 31, 'the medium band keeps its auto row: the choice is per table');
assert.equal(tableRows('hidden', 'data-queue'), 53, 'one auto row leaves the hidden table; the auto fragment was already out of view');
assert(text(html).includes('3 auto-tier rows of 30 hidden'), `the low band heading states the auto denominator: ${(text(html).match(/\d+ auto-tier rows? of \d+ hidden/g) || []).join(' | ')}`);
assert(text(html).includes('55 roofs, 1 fragment hidden. 1 auto-tier row of 54 hidden.'), 'the hidden intro states the fragment and auto counts in order');
assert.equal(count(html, /<input type="checkbox" checked=""[^>]*\/?> Hide auto tier \(/g), 2, 'the two ticked checkboxes reflect the state');
assert(!html.includes('object=objlow2&amp;') && !html.includes('object=objlow4&amp;') && html.includes('object=objlow6&amp;') && html.includes('object=objlow1&amp;'), 'auto rows are absent from the markup; assist and review rows remain');
assert.equal(count(html, /queues-hint-tier-auto/g), 1, 'only the medium band still shows an auto badge');
assert(text(html).includes('Machine labels: 3 auto, 1 assist, 1 review; 25 roofs without one.'), 'the count line is an inventory of the table, not of the filtered view');
assert(text(html).includes('0 of 30 reviewed'), 'review progress keeps the full denominators');
// Hide auto tier with Show fragments in the hidden table: the auto fragment now counts and leaves with the other auto row.
html = renderPage([ready(summary), ready(decidedCalibration), ready(decidedHidden), attempts, false, { hidden: true }, { hidden: true }]);
assert.equal(tableRows('hidden', 'data-queue'), 53, '55 rows, fragment shown, two auto rows hidden');
assert(text(html).includes('55 roofs, 1 fragment shown. 2 auto-tier rows of 55 hidden.'), 'the checkbox count and the note agree once the fragment is shown');
assert(html.includes('Hide auto tier (2)</label>'), 'the hidden checkbox counts the shown fragment');
// Hide auto tier and Hide flagged together: the flagged denominator is what remained after the auto step.
html = renderPage([ready(summary), ready(decidedCalibration), ready(decidedHidden), attempts, true, {}, { low: true }]);
assert(text(html).includes('3 auto-tier rows of 30 hidden') && text(html).includes('3 of 27 flagged rows hidden by the filter'), `objlow2 (container stack) left with the auto step; the flagged note counts the other three over the remaining 27: ${(text(html).match(/\d+ of \d+ flagged rows hidden by the filter/g) || []).join(' | ')}`);
assert.equal(tableRows('low'), 24);
// A band that is entirely auto tier says so once hidden.
const allAuto = { ...calibration, rows: decidedRows.map(row => (row.band === 'high' ? { ...row, structure: undefined, decision: autoGarage } : row)) };
html = renderPage([ready(summary), ready(allAuto), ready(decidedHidden), attempts, false, {}, { high: true }]);
assert(text(html).includes('Every sampled roof in the high band is in the auto tier, hidden until shown.'), 'an emptied band names the auto tier');
assert(!/data-band="high"/.test(html), 'no empty high table');
assert(text(html).includes('Machine labels: 6 auto, 0 assist, 0 review. Machine labels, not reviewer labels.'), 'no "without one" clause when every row has a decision');
// Without decisions nothing changes: no badges, no count line, no checkbox.
html = renderPage([ready(summary), ready(structuredCalibration), ready(structuredHidden), attempts, false]);
assert.equal(count(html, /queues-hint-decision/g), 0); assert.equal(count(html, /queues-decision-count/g), 0); assert(!html.includes('Hide auto tier ('));
const decisionCss = readFileSync('src/app/queues/queues.css', 'utf8');
assert(/\.queues-hint-decision/.test(decisionCss) && /\.queues-hint-tier-auto/.test(decisionCss) && /\.queues-hint-tier-assist/.test(decisionCss) && /\.queues-hint-tier-review/.test(decisionCss), 'queues.css styles the tier badges');
assert(/\.queues-auto-tier/.test(decisionCss) && /\.queues-decision-count/.test(decisionCss), 'queues.css styles the checkbox and count line');
assert(/decisionSummary\(/.test(pageSource) && /isAutoTier\(/.test(pageSource), 'the page uses the shared decision helpers rather than its own rules');
// A v2 decision on a queue row: the badge title carries the model range and the human agreement sentence.
const v2Auto = decisionOf('auto', 0.91, undefined, ['small footprint beside the driveway'], { interval: { kind: 'wilson-95', stratum: 'auto/garage', confirmed: 6, judged: 9, lower: 0.3536, upper: 0.8791, note: '' }, score_interval: { lower: 0.8612, upper: 0.9488, note: '' }, review: { status: null } });
html = renderPage([ready(summary), ready({ ...calibration, rows: hintedRows.map(row => (row.object_id === 'objlow2' ? { ...row, decision: v2Auto } : row)) }), ready(hintedHidden), attempts, false]);
assert(/queues-hint-tier-auto queues-hint-decision" title="Machine label, not a reviewer label\. Machine label: garage; auto tier, confidence 0\.91 \(model range 0\.86 to 0\.95\)\. Human agreement for auto\/garage: 6 of 9 confirmed, 95% interval 0\.35 to 0\.88\. Reasons: small footprint beside the driveway\."/.test(html), 'the v2 tooltip carries the model range and the agreement interval');

// ---- the Machine labelled table: every auto-tier roof from the index, honest denominators, links only ----
const machineDecision = (confidence, extra = {}) => decisionOf('auto', confidence, undefined, ['auto: every primary-roof gate passed'], extra);
const agreement = { kind: 'wilson-95', stratum: 'auto/garage', confirmed: 6, judged: 9, lower: 0.3536, upper: 0.8791, note: '' }, range = { lower: 0.8612, upper: 0.9488, note: '' };
const machineRows = [
  { scene_id: 'house-25822', object_id: 'fa380ad7fdc1b515', version: 'decision-policy-v2', decision: machineDecision(0.9134, { interval: agreement, score_interval: range, review: { status: 'confirmed' } }), class_hint: 'outbuilding', hints: [], structure: { ...container, sub_kind: 'garage' } },
  { scene_id: 'house-25833', object_id: '5454bd0d9fe04dc6', version: 'decision-policy-v2', decision: machineDecision(0.8029, { interval: { ...agreement, stratum: 'auto/units', confirmed: 0, judged: 0, lower: null, upper: null }, score_interval: { lower: 0.7412, upper: 0.8633, note: '' }, review: { status: null } }, ) },
  { scene_id: 'mooroolbark-50011498-17-118442-80417', object_id: '648a1d7de50b52e0', version: 'decision-policy-v1', decision: machineDecision(0.85, { review: { status: 'candidate' } }) },
  { scene_id: 'house-25822', object_id: 'aa11', version: 'decision-policy-v2', decision: machineDecision(0.88, { interval: agreement, score_interval: range, review: { status: 'needs_adjustment' } }) },
  { scene_id: 'house-25822', object_id: 'bb22', version: 'decision-policy-v1', decision: machineDecision(0.81) },
];
machineRows[1].decision.machine_label = { kind: 'building', structure_sub_kind: 'not-secondary', class_hint: 'units', roof_form: 'pitched', roof_type: 'hipped' };
const machineQueue = q.coerceMachineLabelledQueue({ version: 'decision-policy-v2', versions: ['decision-policy-v2', 'decision-policy-v1'], rows: machineRows, note: q.MACHINE_LABELLED_NOTE, source: 'eyes-index' });
assert.equal(machineQueue.count, 5); assert.equal(machineQueue.reviewed_by_person, 2, 'confirmed by reviewer and needs edit count as reviewed by a person; candidate, machine-confirmed and no data do not');
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, false, {}, {}, ready({ ...machineQueue, source: 'eyes-index' }), 0]);
const machineSection = html.slice(html.indexOf('id="machine-labelled"'), html.indexOf('id="queues-calibration"'));
assert(machineSection.includes('>Machine labelled</h2>'), 'the section has its heading');
assert(text(machineSection).includes('5 machine-labelled roofs, 2 reviewed by a person.'), `the denominators: ${text(machineSection).slice(0, 200)}`);
assert(text(machineSection).includes('model estimates, not accuracy') && text(machineSection).includes('you still save the scene'), 'the intro states the estimate and where labels are applied');
const machineTable = machineSection.match(/<table[^>]*data-queue="machine-labelled"[^>]*>.*?<\/table>/s);
assert(machineTable, 'the machine-labelled table renders');
assert.equal(count(machineTable[0].match(/<tbody>.*?<\/tbody>/s)[0], /<tr/g), 5, 'one row per auto-tier roof');
for (const column of ['Roof', 'Machine label', 'Confidence (model range)', 'Human agreement', 'Review status', 'Open']) assert(machineTable[0].includes(`>${column}</th>`), `column ${column}`);
const machineRow = id => machineTable[0].match(new RegExp(`<tr>(?:(?!</tr>).)*${id}(?:(?!</tr>).)*</tr>`, 's'))[0];
let row = machineRow('fa380ad7fdc1b515');
assert(row.includes('href="/scenes?scene=house-25822&amp;object=fa380ad7fdc1b515&amp;kind=building&amp;status=all&amp;layer=model"'), 'the roof links into the editor with status=all');
assert.equal(count(row, /href="\/scenes\?scene=house-25822&amp;object=fa380ad7fdc1b515/g), 2, 'the roof cell and the Open link both open the editor');
assert(row.includes('>Open</a>') && row.includes('aria-label="Open roof fa380ad7fdc1b515 in the editor"'), 'a quiet Open link');
assert(text(row).includes('Garage') && row.includes('>Garage</span>'), 'the machine label and the garage structure badge');
assert(text(row).includes('0.91') && text(row).includes('model range 0.86 to 0.95'), 'confidence with the model range');
assert(text(row).includes('6 of 9 confirmed, 95% interval 0.35 to 0.88'), 'the human agreement interval');
assert(text(row).includes('Confirmed by reviewer'), 'the recorded review status');
row = machineRow('5454bd0d9fe04dc6');
assert(text(row).includes('Units') && text(row).includes('0.80') && text(row).includes('model range 0.74 to 0.86') && text(row).includes('not yet judged') && text(row).includes('Confirmed by machine'), `an unjudged, machine-confirmed row: ${text(row)}`);
row = machineRow('648a1d7de50b52e0');
assert(text(row).includes('0.85') && !text(row).includes('model range') && text(row).includes('not yet judged') && text(row).includes('Candidate'), `a v1 row has no model range and no interval: ${text(row)}`);
row = machineRow('aa11'); assert(text(row).includes('Needs edit'));
row = machineRow('bb22'); assert(!/Confirmed|Candidate|Needs/.test(text(row)), 'no review data recorded: the status cell is blank, not invented');
assert.equal(count(machineSection, /<input|<button|<select|<textarea|<form/g), 0, 'no mutating or filtering control in the machine-labelled section');
assert([...machineSection.matchAll(/href="([^"]+)"/g)].every(m => m[1].startsWith('/scenes?')), 'every link in the section opens the editor');
assert(!/[A-Z]{4,}/.test(text(machineSection)) && !text(machineSection).includes(' · ') && !machineSection.includes('→') && !machineSection.includes('–'), 'quiet copy in the machine-labelled section');
assert(html.indexOf('id="machine-labelled"') < html.indexOf('id="queues-calibration"'), 'the reviewer surface for auto labels comes first');
assert.equal(count(html, /<table/g), 5, 'three band tables, the hidden table and the machine-labelled table');
// Empty, unavailable, loading and error states name themselves; the error offers a retry and nothing else.
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, false, {}, {}, ready({ ...q.coerceMachineLabelledQueue({ rows: [] }), source: 'eyes-index' }), 0]);
assert(text(html).includes('0 machine-labelled roofs, 0 reviewed by a person.') && text(html).includes('No roof is in the auto tier.'), 'an empty auto tier says so with its denominators');
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, false, {}, {}, ready({ ...q.coerceMachineLabelledQueue({ rows: [] }), source: 'unavailable' }), 0]);
assert(text(html).includes('The eyes index has no decision policy rows on this machine yet.'), 'an unavailable index is named, not shown as an empty tier');
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, false]);
assert(text(html).includes('Reading machine-labelled roofs from the eyes index'), 'the loading state names what is being read');
html = renderPage([ready(summary), ready(hintedCalibration), ready(hintedHidden), attempts, false, {}, {}, { state: 'error', message: 'the index is locked' }, 0]);
assert(/role="alert"[^>]*>[^<]*the index is locked/.test(html) && html.includes('>Retry machine labelled</button>'), 'the error explains and offers retry');
assert(/coerceMachineLabelledQueue\(/.test(pageSource) && /machineLabelledSummary\(/.test(pageSource) && /agreementText\(/.test(pageSource) && pageSource.includes('"/api/queues/machine-labelled"'), 'the page reads the machine-labelled route through the shared coercer and copy helpers');

console.log('Queues UI: three band tables (30/31/6), status labels with semantic classes, denominators, Wilson wording, hidden anchor with 55 rows, checkpoint list and report, loading/error wording, sentence case, refused degenerate payloads, real-report headings, hint badges with evidence tooltips, structure-kind badges (v2 kinds) with a count line, fragments hidden by default with per-table Show fragments and honest denominators, the Hide flagged filter, decision-policy tier badges with a per-table count line and Hide auto tier (unchecked by default, honest denominators), the Machine labelled table (auto tier from the index, intervals, review status, links only), and no mutating controls passed.');
