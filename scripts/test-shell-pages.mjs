// Renders the shell-adopting pages (reviews, training coverage, model candidates, map capture,
// imagery dates) with stubbed Next primitives, stubbed heavy canvases and positional React state
// presets, then checks the shell contract in every state: one nav, one h1 (the object of work),
// aria-current on the expected workspace, the nav outside <main>, no ' · ' joins or glyph arrows,
// and no crash on degenerate API payloads ({} bodies, reports without captures/rows). Effects can
// run synchronously against a stubbed fetch to prove the fetch handlers reject malformed payloads
// with the page's own error state instead of handing them to render.
import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const root = process.cwd();
const env = { pathname: '/' };
// Positional state presets: useState call N returns presets[N] when set. Setter calls are logged.
const hooks = { presets: {}, index: 0, setters: [], runEffects: false };
const fakeReact = {
  ...React,
  useState(initial) {
    const index = hooks.index++;
    const value = Object.hasOwn(hooks.presets, index) ? hooks.presets[index] : typeof initial === 'function' ? initial() : initial;
    return [value, next => hooks.setters.push({ index, value: next })];
  },
  useEffect(effect) { if (hooks.runEffects) effect(); },
  useRef(value) { return { current: value ?? null }; },
  useMemo(factory) { return factory(); },
  useCallback(callback) { return callback; },
  useSyncExternalStore(subscribe, read, readServer) { return (readServer || read)(); },
};
const nullComponent = { __esModule: true, default: () => null };
const stubs = {
  '@/lib/crm-client': { getCrmToken: () => '' },
  '@/components/reviews/RoofReviewCanvas': nullComponent,
  '@/components/reviews/Roof3DPreview': nullComponent,
  '@/components/reviews/RoofAnnotationEditor': nullComponent,
  '@/components/reviews/TrainingStatus': nullComponent,
  '@/components/reviews/MetricInput': { __esModule: true, default: props => React.createElement('input', { 'aria-label': props['aria-label'], defaultValue: props.value }) },
  '@/components/scenes/SceneEvaluation': nullComponent,
  '@/components/scenes/SceneRoofTraining': nullComponent,
  '@/components/scenes/SceneRoofRuns': nullComponent,
};
const cache = new Map();
function load(file) {
  const absolute = path.resolve(root, file);
  if (cache.has(absolute)) return cache.get(absolute);
  const record = { exports: {} };
  cache.set(absolute, record.exports);
  const code = ts.transpileModule(readFileSync(absolute, 'utf8'), {
    fileName: absolute,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  new Function('exports', 'require', 'module', code)(record.exports, id => resolve(id, absolute), record);
  cache.set(absolute, record.exports);
  return record.exports;
}
function resolve(id, from) {
  if (id.endsWith('.css')) return {};
  if (id === 'react') return fakeReact;
  if (id === 'next/link') return { __esModule: true, default: ({ href, children, onClick, ...rest }) => React.createElement('a', { href, 'data-guarded': onClick ? 'yes' : 'no', ...rest }, children) };
  if (id === 'next/navigation') return { usePathname: () => env.pathname, useRouter: () => ({ push() {} }) };
  if (id === 'next/dynamic') return { __esModule: true, default: () => () => null };
  if (id === 'lucide-react') return new Proxy({}, { get: (_, name) => name === '__esModule' ? true : () => React.createElement('svg', { 'data-icon': String(name) }) });
  if (stubs[id]) return stubs[id];
  const base = id.startsWith('@/') ? path.join(root, 'src', id.slice(2)) : id.startsWith('.') ? path.resolve(path.dirname(from), id) : null;
  if (!base) return require(id);
  if (base.endsWith('/src/lib/scene-roof-mount')) return { outlineSupport: () => 0 }; // pulls in three; candidates only need assessment helpers here
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`]) if (existsSync(candidate) && statSync(candidate).isFile()) return load(candidate);
  throw new Error(`Unresolved import ${id} from ${from}`);
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ');
const links = html => [...html.matchAll(/<a ([^>]*)>(.*?)<\/a>/g)].map(m => ({ attrs: m[1], text: m[2].replace(/<[^>]+>/g, '') }));
const navLinks = html => links(html.slice(html.indexOf('<nav'), html.indexOf('</nav>')));
function render(file, { pathname, presets = {}, props = {}, effects = false, workspace, h1 }) {
  env.pathname = pathname;
  hooks.presets = presets; hooks.index = 0; hooks.setters = []; hooks.runEffects = effects;
  const Page = load(file).default;
  const html = renderToStaticMarkup(React.createElement(Page, props));
  const label = `${file} ${JSON.stringify(Object.keys(presets))}`;
  assert.equal([...html.matchAll(/<h1[\s>]/g)].length, 1, `${label}: exactly one h1`);
  assert.equal([...html.matchAll(/aria-label="Workspaces"/g)].length, 1, `${label}: exactly one workspace nav`);
  assert.deepEqual(navLinks(html).filter(l => l.attrs.includes('aria-current="page"')).map(l => l.text), [workspace], `${label}: ${workspace} is current`);
  assert(html.indexOf('</header>') < html.indexOf('<main'), `${label}: the shell header (nav landmark) sits outside <main>`);
  assert(!/<main[^>]*>[\s\S]*<header/.test(html), `${label}: no header inside main`);
  const body = text(html);
  assert(!body.includes(' · '), `${label}: no ' · ' joins in rendered text`);
  assert(!/[→←↑↓↗]/.test(body), `${label}: no glyph arrows in rendered text`);
  if (h1) assert.match(html, new RegExp(`<h1[^>]*>${h1}</h1>`), `${label}: h1 is ${h1}`);
  return { html, body };
}
const flush = () => new Promise(resolve => setTimeout(resolve, 30));
// Effects run against a stubbed fetch and a minimal window (URL state, popstate listeners, history).
async function withFetch(payload, run) {
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => payload });
  globalThis.window = { location: { search: '', href: 'http://localhost/' }, addEventListener() {}, removeEventListener() {}, history: { replaceState() {} }, dispatchEvent() {} };
  try { await run(); } finally { globalThis.fetch = original; delete globalThis.window; }
}

// Reviews. useState order: 0 cohort, 1 ready, 2 data, 3 loading, 4 error, 5 reload, 6 id, 7 query, 8 filter, 9 page, 10 view, 11 layer, 12 editGeometry, 13 drafts, 14 saving, 15 message.
const entry = (address_id, full_address, feedback) => ({ address_id, full_address, category: 'house', tags: [], area_m2: 120.4, face_count: 4, roofline_count: 4, roof_type: 'hipped',
  ring_meters: [], reference_ring_meters: [], parts: [], lines: [], site_image_url: '/site.png', boundary_source: 'model', boundary_signature: 'b', geometry_source: 'model', height_source: 'prior',
  stale_review: false, imagery_available: true, training_eligible: Boolean(feedback), model_signature: 'm', feedback });
const cohort = { cohort: 'suburban_benchmark', title: 'Suburban benchmark', description: '', total_roofs: 2, cohorts: [{ name: 'suburban_benchmark', title: 'Suburban benchmark', count: 2 }],
  entries: [entry(101, '1 Test Street, Mooroolbark VIC 3138', { status: 'approved', boundary_verified: true, revision: 1 }), entry(102, '2 Test Street, Mooroolbark VIC 3138')] };
{
  const reviews = 'src/app/reviews/page.tsx', pathname = '/reviews', workspace = 'Training';
  let { body } = render(reviews, { pathname, workspace, h1: 'Loading roof reviews' });
  assert(body.includes('Roof review'), 'reviews: the quiet workspace label pinned by verify-reviews.mjs is present while loading');
  assert(body.includes('0 of 0 reviewed'), 'reviews: the reviewed count carries its denominator while loading');
  render(reviews, { pathname, workspace, h1: 'Reviews could not load', presets: { 1: true, 3: false, 4: 'Could not load reviews.' } });
  render(reviews, { pathname, workspace, h1: 'No captured roofs in this set', presets: { 1: true, 2: {}, 3: false } });
  ({ body } = render(reviews, { pathname, workspace, h1: '1 Test Street', presets: { 1: true, 2: cohort, 3: false, 6: 101 } }));
  assert(body.includes('1 of 2 reviewed') && body.includes('Export reviewed (1)'), 'reviews: header counts read "1 of 2 reviewed" and "Export reviewed (1)"');
  assert(body.includes('Save review') && body.includes('Discard draft'), 'reviews: the save bar stays in the page');
  const nav = navLinks(render(reviews, { pathname, workspace, presets: { 1: true, 2: cohort, 3: false, 6: 101 } }).html);
  assert(nav.every(l => l.attrs.includes('data-guarded="yes"')) && nav[0].attrs.includes('href="/"'), 'reviews: drafts guard on every nav link, Map link is "/"');
  await withFetch({}, async () => {
    render(reviews, { pathname, workspace, presets: { 1: true }, effects: true });
    await flush();
    assert(hooks.setters.some(s => s.index === 4 && /Could not load reviews/.test(s.value)), 'reviews: a payload without entries becomes the error state, not a render crash');
    assert(!hooks.setters.some(s => s.index === 2 && s.value && !Array.isArray(s.value.entries)), 'reviews: malformed payloads never reach state');
  });
}

// Training coverage. useState order: 0 coverage, 1 pending, 2 error, 3 message.
const layer = { scene_id: 'scene-1', kind: 'solar_panel', title: 'Scene 1', split: 'train', spatial_group: 'g1', image_url: '/scene-1.png', review_url: '/scenes?scene=scene-1&kind=solar_panel', revision: 2,
  image_geography: { status: 'ok', reason: '', groups: [], splits: [] }, scan: 'not_scanned', scan_count: 0, confirmed: 2, pending: 1, rejected: 0, complete: false, eligible: false, blockers: ['pending'] };
const counts = (images, masks, groups) => ({ images, masks, empty_images: 0, groups });
const coverage = { version: 'v1', scenes: 1, reviewed_objects: 2, false_positives: 0, export_current: true, latest_export: { version: 'v1', reviewed_objects: 2, dataset: 'data/training/scenes/v1', class_exports: {} },
  classes: [{ kind: 'solar_panel', references: 3, pending: 1, withheld: 0, splits: { train: counts(1, 2, 1), validation: counts(0, 0, 0), test: counts(0, 0, 0) }, layers: [layer] }] };
{
  const training = 'src/app/scenes/training/page.tsx', pathname = '/scenes/training', workspace = 'Training', h1 = 'Training coverage';
  let { body, html } = render(training, { pathname, workspace, h1 });
  assert(body.includes('Checking saved labels and image coverage') && body.includes('Export reviewed dataset'), 'training: loading state names what is being read');
  ({ body } = render(training, { pathname, workspace, h1, presets: { 1: null, 2: 'Coverage is unavailable. Please retry.' } }));
  assert(body.includes('Retry coverage'), 'training: error state keeps Retry coverage');
  ({ body, html } = render(training, { pathname, workspace, h1, presets: { 0: {}, 1: null } }));
  assert(body.includes('No scenes captured yet') && html.includes('href="/scenes/new"'), 'training: an empty payload renders the empty state with the capture link');
  ({ body, html } = render(training, { pathname, workspace, h1, presets: { 0: coverage, 1: null, 3: 'Coverage refreshed from saved reviews.' } }));
  assert(html.includes('data-scene="scene-1"') && body.includes('Not scanned for this class') && body.includes('Refresh coverage'), 'training: populated coverage lists image layers');
  assert(body.includes('1 matching image'), 'training: layer count is shown');
  await withFetch({}, async () => {
    render(training, { pathname, workspace, h1, effects: true });
    await flush();
    assert(hooks.setters.some(s => s.index === 2 && /Coverage is unavailable/.test(s.value)), 'training: a payload without classes becomes the retryable error, not a render crash');
    assert(!hooks.setters.some(s => s.index === 0 && s.value && !Array.isArray(s.value.classes)), 'training: malformed coverage never reaches state');
  });
}

// Model candidates. useState order: 0 catalog, 1 catalogError, 2 attempt, 3 loaded, 4 outlines, 5 annotations, 6 focused, 7 imageError, 8 saving, 9 saveError.
const metrics = (matched, extra) => ({ images: 2, groups: 2, references: 5, matched, extra, missed: 5 - matched });
const candidate = { id: 'cand0001abcd', kind: 'solar_panel', threshold: 0.5, prompt: 'solar panel', dataset_version: 'v1', training_proposals: { usable: 8, negative: 19, images: 10, groups: 7 },
  before: { validation: metrics(4, 3) }, after: { validation: metrics(4, 1) }, scenes: [], checks: [] };
{
  const candidates = 'src/app/scenes/candidates/page.tsx', pathname = '/scenes/candidates', workspace = 'Training', h1 = 'Model candidates';
  let { body } = render(candidates, { pathname, workspace, h1 });
  assert(body.includes('Loading model candidates'), 'candidates: loading state names what is being read');
  ({ body } = render(candidates, { pathname, workspace, h1, presets: { 1: 'Candidate data is unavailable. Please retry.' } }));
  assert(body.includes('Retry candidates'), 'candidates: error state keeps Retry candidates');
  ({ body } = render(candidates, { pathname, workspace, h1, presets: { 0: {} } }));
  assert(body.includes('No model candidates available'), 'candidates: an empty payload renders the empty state');
  ({ body } = render(candidates, { pathname, workspace, h1, presets: { 0: { candidates: [], unavailable: 0 } } }));
  assert(body.includes('No model candidates available'), 'candidates: no candidates renders the empty state');
  ({ body } = render(candidates, { pathname, workspace, h1, presets: { 0: { candidates: [candidate], unavailable: 0 } } }));
  assert(body.includes('4 of 5 before filtering, 4 of 5 after'), 'candidates: validation matches carry their denominators');
  assert(body.includes('These counts do not establish general accuracy'), 'candidates: model numbers are labelled as not accuracy');
  await withFetch({}, async () => {
    render(candidates, { pathname, workspace, h1, effects: true });
    await flush();
    assert(hooks.setters.some(s => s.index === 1 && /Candidate data is unavailable/.test(s.value)), 'candidates: a payload without candidates becomes the retryable error, not a render crash');
  });
}

// Map capture. useState order: 0 lat, 1 lon, 2 size, 3 picked, 4 provider, 5 prompt, 6 busy, 7 error.
{
  const capture = 'src/app/scenes/new/page.tsx', pathname = '/scenes/new', workspace = 'Scene review', h1 = 'Capture a map area';
  const { html, body } = render(capture, { pathname, workspace, h1 });
  assert(navLinks(html)[0].attrs.includes('href="/?lat=-37.79044&amp;lon=145.31252"'), 'capture: the Map link returns to the area being captured');
  assert(body.includes('Choose an area on the map') && body.includes('Capture for manual annotation') && body.includes('Capture and find objects'), 'capture: page actions stay in the body');
  assert(render(capture, { pathname, workspace, h1, presets: { 7: 'Choose a smaller area.' } }).html.includes('role="alert"'), 'capture: errors render as alerts');
}

// Imagery dates. useState order: 0 run, 1 report, 2 pending, 3 error, 4 view, 5 kind, 6 threeD, 7 snapshot, 8 objects, 9 showOutlines, 10 selected, 11 overlayError, 12 overlayKey, 13 refresh.
const report = { run: 'r', scene_id: 'scene-1', title: 'Mooroolbark tile 17/118441/80414', status: 'complete', stage: 'Comparison ready', stale: false, reference_date: '2025-11-06', image_width: 1152, image_height: 1152,
  captures: [{ index: 0, acquisition_date: '2025-11-06', release_title: 'x', provider: 'Vantor', source: 'LG06', resolution_m: 0.34, status: 'accepted' },
    { index: 1, acquisition_date: '2024-01-01', release_title: 'y', provider: 'Maxar', source: 'W', resolution_m: 0.3, status: 'failed', reason: 'registration failed' }],
  rows: [{ object_id: 'abcdef1234', kind: 'building', status: 'consistent', reason: 'None', observations: [{ date: '2025-11-06', iou: 0.9, local_registration_fraction: 0.95, ambiguous: false }] }],
  summary: { consistent: 3, needs_review: 1, roof_surfaces_fitted: 0, roof_surfaces_queued: 0 } };
{
  const imagery = 'src/components/scenes/TemporalWorkspace.tsx', pathname = '/scenes/imagery', workspace = 'Scene review';
  let { body, html } = render(imagery, { pathname, workspace, h1: 'Imagery dates', props: { scene: '', initialRun: '' } });
  assert(body.includes('Open this view from a scene to compare its imagery dates') && navLinks(html)[0].attrs.includes('href="/"'), 'imagery: without a scene the page says what to open');
  ({ body } = render(imagery, { pathname, workspace, h1: 'Imagery dates', props: { scene: 'scene-1', initialRun: '' } }));
  assert(body.includes('Scene scene-1. No imagery comparison has run yet') && body.includes('Compare imagery dates'), 'imagery: a scene without a run names the scene');
  ({ body } = render(imagery, { pathname, workspace, h1: 'Imagery dates', props: { scene: 'scene-1', initialRun: 'abcd' }, presets: { 1: { status: 'complete', stage: 'Comparison ready' } } }));
  assert(body.includes('0 of 0 dates registered'), 'imagery: a report without captures or rows renders with zero denominators instead of crashing');
  ({ body, html } = render(imagery, { pathname, workspace, h1: report.title, props: { scene: 'scene-1', initialRun: 'r' }, presets: { 1: report, 7: { centroid: { lat: -37.8, lon: 145.3 }, objects: [] } } }));
  assert(navLinks(html)[0].attrs.includes('href="/?lat=-37.8&amp;lon=145.3"'), 'imagery: the Map link returns to the scene centroid once the frame is known');
  assert(body.includes('1 of 2 dates registered, 3 consistent objects, 1 to inspect, 0 roof surfaces fitted'), 'imagery: registered dates carry their denominator');
  assert(body.includes('Agreement is an evidence check, not a calibrated accuracy percentage'), 'imagery: agreement is labelled as evidence, not accuracy');
  assert(html.includes('<th scope="col">Object</th>'), 'imagery: table headers carry scope');
  assert(body.includes('Self-review queue'), 'imagery: the self-review action stays in the header slot');
}
console.log('Shell pages: one nav and one h1 in loading, error, empty, degenerate and populated states; nav outside main; fetch handlers reject malformed payloads; denominators and estimate copy present.');
