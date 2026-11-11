// Static render of the processing workspace with stubbed Next, Lucide and map primitives:
// the shell header contract (one h1, nav current = Processing, no eyebrow or slogan), the three
// eyes groups as open h3 sections with one-line summaries, and the server-paged table copy.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const Null = () => null;
const modules = new Map();
function load(file) {
  const resolved = path.resolve(file);
  if (modules.has(resolved)) return modules.get(resolved);
  const exports = {};
  modules.set(resolved, exports);
  const code = ts.transpileModule(readFileSync(resolved, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => {
    if (id.endsWith('.css')) return {};
    if (id === 'next/link') return { __esModule: true, default: ({ href, children, ...rest }) => React.createElement('a', { href, ...rest }, children) };
    if (id === 'next/navigation') return { usePathname: () => '/scenes/processing' };
    if (id === 'next/dynamic') return { __esModule: true, default: () => Null };
    if (id === 'lucide-react') return new Proxy({}, { get: () => () => React.createElement('svg', { 'aria-hidden': 'true' }) });
    if (id === './BuildingAddressSummary' || id === './ProcessingPreview' || id === './SelfReviewPanel') return { __esModule: true, default: Null };
    if (id.startsWith('./')) return load(path.join(path.dirname(resolved), id) + existing(path.join(path.dirname(resolved), id)));
    if (id.startsWith('@/')) return load(path.join('src', id.slice(2) + existing(path.join('src', id.slice(2)))));
    return require(id);
  });
  return exports;
}
function existing(base) {
  for (const ext of ['.tsx', '.ts']) { try { readFileSync(base + ext); return ext; } catch { /* try next */ } }
  throw new Error(`cannot resolve ${base}`);
}
const workspace = load('src/components/scenes/ProcessingWorkspace.tsx');
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

// Initial paint: the shell header, the suburb as the only h1 and a loading state that names what is read.
let html = renderToStaticMarkup(React.createElement(workspace.default, { initial: {} }));
assert.equal((html.match(/<h1/g) || []).length, 1, 'the suburb is the only h1');
assert(/<h1>Mooroolbark<\/h1>/.test(html));
assert(html.startsWith('<div class="processing-shell"><header class="review-header workbench-header">'), 'the page builds on the shell header');
assert(/aria-current="page"[^>]*>Processing<\/a>/.test(html) || /<a[^>]*aria-current="page"[^>]*>Processing</.test(html), 'Processing is the current workspace');
assert(html.includes('Reading suburb progress'), 'the loading state names what is being read');
for (const banned of ['SUBURB WORKSPACE', 'Follow the scan', 'ROOF INSPECTOR', 'MODEL EVIDENCE', 'processing-topbar', 'processing-eyebrow', ' · ', '→', '…']) {
  assert(!html.includes(banned), `initial render must not contain ${JSON.stringify(banned)}`);
}
assert(/Live updates/.test(html) && /aria-label="Refresh processing workspace"/.test(html), 'live toggle and refresh live in the header actions slot');

// Eyes groups: three open h3 sections, each led by a one-line summary; copy keeps the model-estimate caveat.
const roof = {
  type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[145, -37], [145.001, -37], [145.001, -37.001], [145, -37]]] },
  properties: {
    key: 'mooroolbark-50011498-17-118442-80417/44c724fd3480c167', scene_id: 'mooroolbark-50011498-17-118442-80417', object_id: '44c724fd3480c167', model_signature: 'sig',
    label: 'Building 12', capture_title: '27 Cambridge Road', band: 'low', band_label: 'Low evidence', color: '#df796a', reasons: ['Detector support below 0.7'],
    area_m2: 212.4, detector_score: 0.58, geometry_passed: true, clipped: false, observed_segments: 4, image_line_mismatch_m: 1.352, image_line_mismatch_display: 1.352,
    review_status: 'candidate', issue_count: 0, review_url: '/scenes?scene=x&object=y&kind=building&status=all&layer=model', interval: null, calibration_groups: 0,
    property_address: { status: 'matched', full_address: '27 CAMBRIDGE ROAD MOOROOLBARK 3138', candidates: [], reason: '' },
    hidden_overlaps: [{ scene_id: 'house-30286', object_id: '37ff5c02f77869d5', band: 'medium', iou: 0.9017, detector_score: 0.9, image_line_mismatch_m: 0.5, observed_segments: 9, clipped: false }],
    eyes: {
      reference: { tier: 'T2', iou: 0.821, hausdorff_m: 1.988, roof_type_match: false, line_precision: 0.123, line_recall: 0.5, image_identity: 'different_image' },
      diagnostics: { planes: 27, crease_runs: 51, short_junction_links: 16, matched_edges: 3, detected_segments: 7, line_refinement_reason: 'complexity-limit', crease_contrast_share: 0.53, edge_unsupported_fraction: 0, recomputed_matches_stored: true },
      shadow_band_v2: 'low',
      cross_capture: { n_pairs: 1, bands: [{ scene_id: 'house-30286', object_id: '37ff5c02f77869d5', band: 'medium', iou: 0.902 }], hidden_by: ['house-30286/37ff5c02f77869d5'] },
    },
  },
};
html = renderToStaticMarkup(React.createElement(workspace.EyesGroups, { roof }));
const headings = [...html.matchAll(/<h3[^>]*>([^<]*)<\/h3>/g)].map(m => m[1]);
assert.deepEqual(headings, ['Model evidence', 'Reference agreement', 'Other captures'], 'three sentence-case h3 groups in order');
const summaries = [...html.matchAll(/<p class="processing-group-summary">([^<]*)<\/p>/g)].map(m => m[1]);
assert.deepEqual(summaries, [
  'Detector 0.58, 4 lines observed, mismatch 1.35 m',
  'IoU 0.82 with a tier 2 human reference, different imagery',
  'Also seen as medium in house-30286',
], 'each group leads with a computed one-line summary');
assert(!html.includes('<details'), 'groups are left open, not collapsed');
assert(html.includes('model estimate, not accuracy'), 'the caveat survives the copy sweep');
assert(!text(html).includes(' · ') && !html.includes('→'), 'no middle-dot joins or arrows');
assert(/<dt>Planes<\/dt><dd>27<\/dd>/.test(html) && /<dt>Crease runs<\/dt><dd>51<\/dd>/.test(html), 'joined diagnostics became separate rows');
assert(!/[A-Z]{4,}[A-Z ]*<\/(span|h3|p)>/.test(html.replace(/CAMBRIDGE ROAD MOOROOLBARK/g, '')), 'no all-caps labels');

const bare = { ...roof, properties: { ...roof.properties, eyes: undefined, hidden_overlaps: undefined, detector_score: null, observed_segments: 0, image_line_mismatch_display: null } };
html = renderToStaticMarkup(React.createElement(workspace.EyesGroups, { roof: bare }));
assert.deepEqual([...html.matchAll(/<p class="processing-group-summary">([^<]*)<\/p>/g)].map(m => m[1]), [
  'No detector score, no lines observed',
  'No tier 1 or tier 2 human reference is recorded for this roof',
  'No overlapping capture is recorded for this roof',
], 'missing eyes still yield three honest summaries');

// Server-paged table: rows from the page, the Showing a–b of n line, page counter and aria-sort.
const rows = Array.from({ length: 50 }, (_, i) => ({ ...roof, properties: { ...roof.properties, key: `s/o${i}`, object_id: `o${i}` } }));
const page = { rows, total: 10339, page: 1, size: 50, pages: 207, sort: 'evidence', counts: {}, address_counts: {}, fetched_at: 1, inventory: 'ready' };
html = renderToStaticMarkup(React.createElement(workspace.RoofTable, { data: page, loading: false, error: '', sort: 'evidence', onSort() {}, onPage() {}, selected: null, onSelect() {}, clearFilters() {}, filtered: false }));
assert.equal((html.match(/<tr/g) || []).length, 51, '50 rows plus the header row');
assert(text(html).includes('Showing 1–50 of 10,339 roofs'), 'the denominator is the whole inventory');
assert(text(html).includes('Page 1 of 207'));
assert(/aria-sort="ascending"[^>]*><button[^>]*>Confidence/.test(html), 'the active sort is announced');
assert(/aria-label="Previous page"[^>]*disabled/.test(html) || /disabled[^>]*aria-label="Previous page"/.test(html), 'first page cannot go back');
html = renderToStaticMarkup(React.createElement(workspace.RoofTable, { data: { ...page, page: 207, total: 10339, rows: rows.slice(0, 39) }, loading: true, error: '', sort: '-area', onSort() {}, onPage() {}, selected: null, onSelect() {}, clearFilters() {}, filtered: true }));
assert(text(html).includes('Showing 10,301–10,339 of 10,339 matching roofs'), 'the last page and a filtered denominator');
assert(/aria-sort="descending"[^>]*><button[^>]*>Area/.test(html));
assert(html.includes('aria-busy="true"') && html.includes('Updating'), 'a refetch keeps the current rows and says so');
html = renderToStaticMarkup(React.createElement(workspace.RoofTable, { data: null, loading: true, error: '', sort: 'evidence', onSort() {}, onPage() {}, selected: null, onSelect() {}, clearFilters() {}, filtered: false }));
assert(html.includes('Reading the roof inventory'), 'the empty state names what is being read');
html = renderToStaticMarkup(React.createElement(workspace.RoofTable, { data: { ...page, total: 0, rows: [] }, loading: false, error: '', sort: 'evidence', onSort() {}, onPage() {}, selected: null, onSelect() {}, clearFilters() {}, filtered: true }));
assert(html.includes('No matching roofs') && html.includes('Clear filters'));
console.log('Processing workspace: shell header with one h1, three open eyes groups with summaries, and server-paged table copy passed.');
