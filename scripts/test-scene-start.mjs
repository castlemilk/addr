// Renders the /scenes hub (SceneStart) with stubbed Next primitives and checks its contract:
// the h1 is 'Open a scene' in every state, loading/empty/error states name what is being read,
// scenes are grouped by id prefix, every row links into the canonical editor, suburb tiles are
// listed only against a typed id, and nothing on the hub is a decision control.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const load = (file, stubs = {}) => {
  const exports = {};
  const code = ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => {
    if (id.endsWith('.css')) return {};
    if (id === 'next/link') return { __esModule: true, default: ({ href, children, ...rest }) => React.createElement('a', { href, ...rest }, children) };
    if (id === 'lucide-react') return new Proxy({}, { get: () => () => React.createElement('svg') });
    if (id in stubs) return stubs[id];
    return require(id);
  });
  return exports;
};
const recentScenes = load('src/lib/recent-scenes.ts');
// The hub reads recents on mount; the test controls what that read returns.
let recentFixture = [];
const recentStub = { ...recentScenes, readRecentScenes: () => recentFixture };
const sceneQueue = { sceneTitle: scene => scene.property_title || (scene.address_id ? scene.title.split(',')[0] : scene.title) };
const start = load('src/components/scenes/SceneStart.tsx', { '@/lib/scene-queue': sceneQueue, '@/lib/recent-scenes': recentStub });
const { default: SceneStart, groupScenes, filterTiles, TILE_ROW_LIMIT } = start;

const scene = (id, title, objects = 10, reviewed = 0, extra = {}) => ({ id, title, address_id: id.startsWith('house-') ? Number(id.slice(6)) : null, objects, reviewed, stale: false, ...extra });
const scenes = [
  scene('roof_training_250-29790', '27 Cambridge Road, Mooroolbark VIC 3138', 12, 12),
  scene('mooroolbark-50011498-17-118442-80417', 'Mooroolbark tile 118442-80417', 127, 3),
  scene('house-30286', '29 CAMBRIDGE ROAD, Mooroolbark VIC 3138', 30, 0, { stale: true }),
  scene('house-25822', '7 GREENVILLE STREET, Mooroolbark VIC 3138', 109, 0),
  scene('map-102e9a8088525f6a', 'Map capture 145.31, -37.79', 40, 40),
  scene('mooroolbark-50011498-17-118441-80414', 'Mooroolbark tile 118441-80414', 90, 90),
  scene('qa-fixture', 'Fixture scene', 1, 0),
];
const render = props => renderToStaticMarkup(React.createElement(SceneStart, props));
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
const h1s = html => [...html.matchAll(/<h1[^>]*>([^<]*)<\/h1>/g)].map(m => m[1]);
const hrefs = html => [...html.matchAll(/<a [^>]*href="([^"]*)"/g)].map(m => m[1].replace(/&amp;/g, '&'));

// Grouping is by id prefix, sorted for reading, with nothing dropped.
const groups = groupScenes(scenes);
assert.deepEqual(groups.houses.map(s => s.id), ['house-25822', 'house-30286'], 'houses sort by address');
assert.deepEqual(groups.tiles.map(s => s.id), ['mooroolbark-50011498-17-118441-80414', 'mooroolbark-50011498-17-118442-80417']);
assert.deepEqual(groups.cohort.map(s => s.id), ['roof_training_250-29790']);
assert.deepEqual(groups.captures.map(s => s.id), ['map-102e9a8088525f6a']);
assert.deepEqual(groups.other.map(s => s.id), ['qa-fixture'], 'unknown prefixes stay reachable');
assert.equal(Object.values(groups).flat().length, scenes.length);
assert.deepEqual(filterTiles(groups.tiles, ''), [], 'an empty query lists no tiles');
assert.deepEqual(filterTiles(groups.tiles, ' 80417 ').map(s => s.id), ['mooroolbark-50011498-17-118442-80417'], 'tile search is a trimmed substring match');
assert.deepEqual(filterTiles(groups.tiles, 'MOOROOLBARK').length, 2, 'tile search is case-insensitive');
assert.equal(TILE_ROW_LIMIT, 40);

// Loading: the h1 and actions are present before the list arrives, and the state names what is read.
let html = render({ scenes: null, loading: true });
assert.deepEqual(h1s(html), ['Open a scene']);
assert(text(html).includes('Listing saved scenes'), 'loading state names the list being read');
assert(!html.includes('Loading scene'), 'the hub never shows the editor spinner text');
assert(hrefs(html).includes('/scenes/new') && hrefs(html).includes('/?selectScene=1'), 'capture and map actions are available while loading');
assert(text(html).includes('Capture map area') && text(html).includes('Choose on the map'));
assert(!/scene-start-section/.test(html), 'no groups render before the list is known');

// Empty result.
html = render({ scenes: [], loading: false });
assert.deepEqual(h1s(html), ['Open a scene']);
assert(text(html).includes('No saved scenes yet. Capture a map area to start.'));
assert(!text(html).includes('Listing saved scenes'));

// Error with retry.
html = render({ scenes: null, loading: false, error: 'Could not load scenes.', onRetry: () => {} });
assert(/role="alert"/.test(html) && text(html).includes('Could not load scenes.') && text(html).includes('Retry listing'));
assert.deepEqual(h1s(html), ['Open a scene']);

// Listed.
html = render({ scenes, loading: false });
assert.deepEqual(h1s(html), ['Open a scene'], 'exactly one h1 and it is the hub heading');
const body = text(html);
assert(body.includes('7 saved scenes.'), 'total carries its count');
for (const heading of ['Recently opened', 'Houses', 'Suburb tiles', 'Cohort roofs', 'Map captures', 'Other scenes']) assert(new RegExp(`<h2[^>]*>${heading}</h2>`).test(html), `${heading} section`);
assert(body.includes('2 scenes captured per address') && body.includes('2 tiles from the suburb scan') && body.includes('1 scene from the roof training cohort'));
assert(body.includes('29 CAMBRIDGE ROAD') && body.includes('7 GREENVILLE STREET'), 'house rows use the address as the title');
assert(body.includes('0 of 30 reviewed') && body.includes('12 of 12 reviewed'), 'reviewed counts carry their denominators');
assert(body.includes('30 objects') && body.includes('1 object'), 'object counts are pluralised');
assert(body.includes('Feedback needs recovery'), 'stale scenes carry the amber recovery label');
assert.equal((html.match(/Feedback needs recovery/g) || []).length, 1, 'only the stale scene is labelled');
const links = hrefs(html);
for (const id of ['house-30286', 'house-25822', 'roof_training_250-29790', 'map-102e9a8088525f6a', 'qa-fixture']) assert(links.includes(`/scenes?scene=${id}`), `${id} links into the editor`);
assert(!links.some(href => href.includes('mooroolbark-')), 'suburb tiles are not listed without a search');
assert(links.includes('/scenes/processing'), 'tiles link to processing instead of listing 257 rows');
assert(links.includes('/reviews?cohort=roof_training_250'), 'cohort roofs link to the roof review cohort');
assert(/aria-label="Find a suburb tile by id"/.test(html), 'tile search box');
assert(body.includes('Scenes you open are listed here.'), 'empty recents name what will appear');
assert(!/<button/.test(html.replace(/Retry listing/g, '')), 'the hub has no decision buttons');
assert(!/[·→]/.test(body) && !/[\u{1F300}-\u{1FAFF}]/u.test(body), 'no dot joins, arrows or emoji in hub copy');
assert(!/[A-Z]{4,}/.test(body.replace(/[A-Z0-9 ]+ROAD|[A-Z ]+STREET|VIC|Mooroolbark/g, '')), 'no all-caps eyebrows (addresses come from the data)');
assert(!/letter-spacing|text-transform/.test(html));
// Recent rows fall back to the stored title when the scene is no longer listed.
html = renderToStaticMarkup(React.createElement(SceneStart, { scenes: [], loading: false }));
assert(text(html).includes('No saved scenes yet.'));

// Recently opened rows never invent counts. While the list is still loading they show the stored
// title and when it was opened only; once listed, counts come from the list, and a scene that is no
// longer listed says so instead of reading '0 of 0 reviewed'.
recentFixture = [
  { id: 'gone-scene', title: 'Removed capture', opened: '2026-09-25T09:30:00.000Z' },
  { id: 'house-30286', title: '29 CAMBRIDGE ROAD', opened: '2026-09-26T10:00:00.000Z' },
];
html = render({ scenes: null, loading: true });
let recentText = text(html);
assert(/<h2[^>]*>Recently opened<\/h2>/.test(html) && recentText.includes('Removed capture') && /Opened 25 Sept? 2026/.test(recentText), 'recents render from storage while the list loads');
assert(!recentText.includes('0 objects') && !recentText.includes('0 of 0 reviewed'), 'no fabricated counts while the list is unknown');
assert(!recentText.includes('Not in the saved list'), 'a scene is not called missing before the list is known');
assert(!/scene-start-houses/.test(html), 'grouped sections wait for the list');
assert(hrefs(html).includes('/scenes?scene=gone-scene') && hrefs(html).includes('/scenes?scene=house-30286'), 'recent rows link into the editor');
html = render({ scenes, loading: false });
recentText = text(html);
assert.equal((recentText.match(/0 of 30 reviewed/g) || []).length, 2, 'a listed recent scene shows the list counts (once under Houses, once under Recently opened)');
assert(recentText.includes('Not in the saved list'), 'a recent scene missing from the list is labelled');
assert(!recentText.includes('0 of 0 reviewed'), 'a missing scene never reads as an empty review');
assert.equal((html.match(/Feedback needs recovery/g) || []).length, 2, 'the stale label follows the listed scene into Recently opened');
recentFixture = [];

// Static pins on the page that hosts the hub (its imports are too heavy to render here).
const page = readFileSync('src/app/scenes/page.tsx', 'utf8');
assert(page.includes('<SceneUrlSync onChange={applyUrl} />') && /<Suspense fallback=\{null\}><SceneUrlSync/.test(page) && page.includes('useSearchParams()'), 'the page follows ?scene= changes without a remount (hub rows, Scene review nav link, Back/Forward)');
assert(page.includes('if (urlScene.current === null) return;') && page.includes('if (requested === urlScene.current || requested === id) return;'), 'the URL sync ignores the initial mount and its own replaceState');
assert(page.includes('setUrlRestore((n) => n + 1);'), 'a blocked URL change keeps the scene and restores the URL');
const cssImports = [...page.matchAll(/^import "[^"]+\.css";/gm)];
assert(cssImports.length && page.indexOf('from "@/components/shell/WorkbenchHeader"') > cssImports.at(-1).index, 'the shell is imported after the page CSS so the 52px shell row wins');
assert(page.includes('{!id && scenes.length > 0 && <option value="">Choose a scene</option>}'), 'the rail names the unselected state instead of showing the first scene as chosen');
assert(page.includes('listLoading ? "Loading scenes…" : listError ? "Scene list unavailable" : "No extracted scenes"'), 'the rail distinguishes loading, failed and empty lists');
assert(page.includes('error={listError}'), 'the hub shows list errors, not editor errors');
assert(!page.includes('Review your surroundings'), 'no workspace slogan');

console.log('Scene start: grouping, tile search, loading/empty/error states, denominators, stale label, editor links, honest recent rows, page URL sync and rail pins, and no decision controls passed.');
