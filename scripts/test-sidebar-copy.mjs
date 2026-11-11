// Light map shell: renders the sidebar with stubbed Next/Lucide/Connect primitives and checks
// the accepted design against the strings other verify scripts pin. Static checks cover the map
// chips, the analysed-layers panel link and popup, the ?crm=1 deep link and the body font.
// Degenerate payloads (no match, non-finite metres, partial stats) must render honestly, not crash.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const read = file => readFileSync(file, 'utf8');
const kebab = name => name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
const lucide = new Proxy({}, { get: (_, name) => props => React.createElement('svg', { className: `lucide lucide-${kebab(String(name))} ${props?.className ?? ''}`.trim(), 'aria-hidden': 'true', 'data-icon': String(name) }) });
const load = (file, stubs = {}) => {
  const exports = {};
  const code = ts.transpileModule(read(file), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => {
    if (id.endsWith('.css')) return {};
    if (id === 'next/link') return { __esModule: true, default: ({ href, children, onClick, ...rest }) => React.createElement('a', { href, ...rest }, children) };
    if (id === 'next/navigation') return { usePathname: () => '/', useRouter: () => ({ push() {} }) };
    if (id === 'lucide-react') return lucide;
    if (id in stubs) return stubs[id];
    return require(id);
  });
  return exports;
};

const EMOJI = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}]/u;
const sidebarSource = read('src/components/Sidebar.tsx');
const mapSource = read('src/components/MapComponent.tsx');
const pageSource = read('src/app/page.tsx');
const overlaySource = read('src/components/scenes/SceneMapOverlay.tsx');
const globals = read('src/app/globals.css');

// --- Sidebar: static -------------------------------------------------------------------------
assert(/import \{ WorkbenchNav \} from "@\/components\/shell\/WorkbenchNav"/.test(sidebarSource), 'the sidebar builds on the shared WorkbenchNav');
assert(!EMOJI.test(sidebarSource), 'no emoji code points in Sidebar.tsx (Lucide only)');
assert(!/[✕✖×]/.test(sidebarSource), 'no typographic close glyphs in Sidebar.tsx (Lucide X)');
assert(!/\buppercase\b/.test(sidebarSource), 'no ALL-CAPS text-transform in the sidebar');
assert(!/\btracking-(wider|widest|tight|tighter)\b/.test(sidebarSource), 'no letter-spacing tracking in the sidebar');
assert(!/\bshadow-(2xl|xl|lg|md)\b/.test(sidebarSource), 'no decorative shadows in the sidebar (borders only)');
assert(!/ · |→/.test(sidebarSource), "no ' · ' joins or arrows in sidebar text");
assert(!/text-\[(?:[0-9]|10)px\]/.test(sidebarSource), 'nothing below 11 px in the sidebar');
for (const gone of ['GeoAU Offline', 'G-NAF + POI', '100% Offline', 'Jump:', 'Map Layers', 'Nearby Places', 'RBAC', '3D RELIEF', 'POIs & Places', 'G-NAF Points']) {
  assert(!sidebarSource.includes(gone), `sidebar no longer says "${gone}"`);
}
for (const kept of ['Address CRM', 'Inspect in Deep 3D Studio', 'G-NAF Rooftop Address', 'POI + G-NAF Address', 'Fallback data']) {
  assert(sidebarSource.includes(kept), `sidebar keeps the pinned string "${kept}"`);
}

// --- Sidebar: render -------------------------------------------------------------------------
const nav = load('src/components/shell/WorkbenchNav.tsx');
const sidebar = load('src/components/Sidebar.tsx', {
  '@/components/shell/WorkbenchNav': nav,
  '@/lib/connect-client': { geoClient: { getStats: async () => ({ regions: [] }), search: async () => ({ results: [] }) } },
  '@/components/scenes/HouseReviewLink': { __esModule: true, default: ({ addressId }) => React.createElement('section', { 'data-house-review-link': String(addressId) }, 'Review analysed layers') },
});
const nearby = Array.from({ length: 8 }, (_, i) => ({ id: i + 1, uuid: `p${i}`, name: `Place ${i + 1}`, category: 'cafe', lat: -37.79, lon: 145.31, distance_meters: 40 + i }));
const address = { id: 30286, uuid: 'a1', unit: null, number: '27', street: 'CAMBRIDGE ROAD', suburb: 'MOOROOLBARK', state: 'VIC', postcode: '3138', full_address: '27 CAMBRIDGE ROAD', lat: -37.79044, lon: 145.31252, distance_meters: 0.3 };
const render = (props = {}) => renderToStaticMarkup(React.createElement(sidebar.default, {
  selectedLocation: { lat: -37.79044, lon: 145.31252 },
  reverseResult: { query: { lat: -37.79044, lon: 145.31252 }, match_type: 'address_only', place: null, address, nearby_places: nearby, nearby_addresses: [], execution_time_ms: 12 },
  isLoadingReverse: false, onSelectCoordinates() {}, showPois: true, setShowPois() {}, showAddresses: false, setShowAddresses() {},
  isDarkMode: false, onToggleTheme() {}, onOpenDeep3D() {}, onOpenCrm() {}, usingFallbackGeo: false, ...props,
}));
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const count = (html, re) => (html.match(re) || []).length;

let html = render();
let plain = text(html);
assert(html.includes('<nav aria-label="Workspaces" class="workbench-nav workbench-nav-light">'), 'light-tone workspace nav in the sidebar header');
const currentLinks = [...html.matchAll(/<a ([^>]*aria-current="page"[^>]*)>([^<]*)<\/a>/g)].map(m => m[2]);
assert.deepEqual(currentLinks, ['Map'], 'Map is the only current workspace');
assert.equal(count(html, /href="\/scenes\/training"/g), 1, 'Training is reached only through the nav (no separate pill)');
assert(html.includes('GeoAU') && !plain.includes('GeoAU Offline'), 'the nav carries the wordmark');
assert(html.includes('aria-label="Switch to dark map"') && html.includes('aria-label="Manage offline index"'), 'theme and index are labelled icon buttons');
assert.equal(count(html, /<h1[\s>]/g), 1, 'exactly one h1: the address');
assert(/<h1[^>]*>27 CAMBRIDGE ROAD<\/h1>/.test(html), 'the address heading is printed as stored');
assert(plain.includes('MOOROOLBARK VIC 3138'), 'locality line under the heading');
assert(plain.includes('G-NAF Rooftop Address, 0.3 m from the address point'), 'one provenance line with the pinned match label and the metres');
assert(html.includes('data-icon="MapPin"'), 'the provenance line carries the MapPin icon');
assert(plain.includes('12 ms lookup'), 'lookup time stays as a labelled count');
assert(plain.includes('Go to') && !plain.includes('Jump'), 'sentence-case Go to label');
assert(html.includes('data-house-review-link="30286"'), 'HouseReviewLink stays the primary action');
assert(/<button[^>]*>(?:(?!<\/button>).)*Address CRM(?:(?!<\/button>).)*<\/button>/s.test(html), 'Address CRM button text kept');
assert(/<button[^>]*>(?:(?!<\/button>).)*Inspect in Deep 3D Studio(?:(?!<\/button>).)*<\/button>/s.test(html), 'Deep 3D Studio button text kept');
assert(/font-mono[^>]*>[^<]*<span>-37\.790440, 145\.312520<\/span>/.test(html) || /-37\.790440, 145\.312520/.test(plain), 'coordinates remain visible');
assert(plain.includes('Copy'), 'coordinates keep the Copy action');
assert(/<h2[^>]*>(?:(?!<\/h2>).)*Map layers(?:(?!<\/h2>).)*<\/h2>/s.test(html), 'Map layers is an h2');
assert.equal(count(html, /<input type="checkbox"/g), 2, 'two native layer checkboxes');
assert(/<input type="checkbox" checked=""[^>]*\/>(?:(?!<\/label>).)*Places<\/label>/s.test(html), 'Places checkbox reflects the on state');
assert(/<input type="checkbox"(?! checked)[^>]*\/>(?:(?!<\/label>).)*Address points<\/label>/s.test(html), 'Address points checkbox reflects the off state');
assert(html.includes('bg-amber-500') && html.includes('bg-emerald-500'), 'amber/green layer dots stay beside their labels');
assert(/<h2[^>]*>Nearby places \(8\)<\/h2>/.test(html), 'Nearby places heading with the count');
assert(!EMOJI.test(html), 'no emoji in the rendered sidebar');
assert(!/uppercase/.test(html), 'no uppercase class in the rendered sidebar');
assert(!plain.includes('Fallback data'), 'fallback badge only when the fallback served the lookup');

html = render({ usingFallbackGeo: true });
assert(text(html).includes('Fallback data'), 'fallback badge keeps its pinned text');

html = render({ reverseResult: { query: { lat: 0, lon: 0 }, match_type: 'poi_and_address', place: { id: 9, uuid: 'x', name: 'LKPT Fitness', category: 'gym', brand: null, address_text: '1 Main St', suburb: null, state: null, postcode: null, lat: 0, lon: 0, phone: '03 9000 0000', website: 'https://example.com', confidence: null, distance_meters: 4 }, address: { ...address, distance_meters: 12 }, nearby_places: [], nearby_addresses: [], execution_time_ms: 3 } });
plain = text(html);
assert.equal(count(html, /<h1[\s>]/g), 1, 'still one h1 when a place is matched');
assert(/<h1[^>]*>27 CAMBRIDGE ROAD<\/h1>/.test(html) && /<h2[^>]*>LKPT Fitness<\/h2>/.test(html), 'address stays the h1; the place is an h2');
assert(html.indexOf('<h1') < html.indexOf('<h2'), 'the address heading comes before the place heading');
assert(plain.includes('POI + G-NAF Address, 12 m from the address point'), 'the joined-match label stays pinned for the live/poi-join checks');
assert(plain.includes('4.0 m from the pin'), 'place distance names its reference');

html = render({ reverseResult: null, isLoadingReverse: true });
plain = text(html);
assert.equal(count(html, /<h1[\s>]/g), 0, 'no heading before an address is read');
assert(plain.includes('Reading the nearest G-NAF address point'), 'loading state names what is being read');

html = render({ reverseResult: { query: { lat: 0, lon: 0 }, match_type: 'address_only', place: null, address: null, nearby_places: [], nearby_addresses: [], execution_time_ms: 2 } });
assert(text(html).includes('No G-NAF address point within 300 m of the pin'), 'empty state names the missing source and radius');

html = render({ isDarkMode: true });
assert(html.includes('workbench-nav-dark') && html.includes('aria-label="Switch to light map"'), 'dark map keeps a readable dark-tone nav and a labelled toggle');

// --- Sidebar: degenerate payloads --------------------------------------------------------------
const empty = { query: { lat: 0, lon: 0 }, place: null, address: null, nearby_places: [], nearby_addresses: [], execution_time_ms: 2 };
for (const match_type of ['none', 'approximate']) {
  html = render({ reverseResult: { ...empty, match_type } });
  plain = text(html);
  assert.equal(count(html, /<h1[\s>]/g), 0, `${match_type}: no h1 without an address`);
  assert(plain.includes('No G-NAF address point within 300 m of the pin'), `${match_type}: the empty state names the missing source`);
  assert(!/Nearest map point|G-NAF Rooftop Address|POI/.test(plain), `${match_type}: no match label is claimed when nothing matched`);
  assert(!html.includes('data-icon="MapPin"'), `${match_type}: no provenance pin beside the empty state`);
  assert(plain.includes('2 ms lookup'), `${match_type}: the lookup time is still reported`);
}
html = render({ reverseResult: { ...empty, match_type: 'address_only', address: { ...address, distance_meters: Number.NaN }, execution_time_ms: Number.NaN } });
plain = text(html);
assert(plain.includes('G-NAF Rooftop Address') && !plain.includes('NaN'), 'a non-finite address distance drops the metres, never prints NaN');
const noDistancePlace = { id: 9, uuid: 'x', name: 'LKPT Fitness', category: null, brand: null, address_text: null, suburb: null, state: null, postcode: null, lat: 0, lon: 0, phone: null, website: null, confidence: null, distance_meters: Number.NaN };
html = render({ reverseResult: { ...empty, match_type: 'poi_and_address', address, place: noDistancePlace, nearby_places: [{ ...nearby[0], distance_meters: undefined }] } });
plain = text(html);
assert(!plain.includes('NaN') && !plain.includes('undefined'), 'non-finite place and nearby distances render nothing');
assert(/<h2[^>]*>Nearby places \(1\)<\/h2>/.test(html) && /<h2[^>]*>LKPT Fitness<\/h2>/.test(html), 'the place and nearby headings survive missing distances');
html = render({ isLoadingReverse: true });
assert(/<section aria-label="Selected address" aria-busy="true"/.test(html) && /<h1[^>]*>27 CAMBRIDGE ROAD<\/h1>/.test(html), 'a re-lookup keeps the last address visible and marks the card busy');
assert.equal(sidebar.formatCount(undefined), '…', 'missing counts show a placeholder');
assert.equal(sidebar.formatCount(Number.NaN), '…', 'non-finite counts show a placeholder');
assert.equal(sidebar.formatCount(1234).replace(/\D/g, ''), '1234', 'counts format as numbers');
assert(sidebarSource.includes('formatCount(stats?.placeCount)'), 'the footer uses the crash-safe count formatter');

// --- Map chips -------------------------------------------------------------------------------
const chips = mapSource.slice(mapSource.indexOf('Quick Category Filter Chips'), mapSource.indexOf('Floating Satellite Vintage Timeline Bar'));
assert(chips.length > 200, 'found the chip block');
assert(!EMOJI.test(chips), 'no emoji in the map chip labels');
for (const icon of ['Utensils', 'Coffee', 'ShoppingBag', 'Trees', 'MapPin']) {
  assert(chips.includes(`icon: ${icon}`), `chip block uses the ${icon} Lucide icon`);
  assert(new RegExp(`\\b${icon}\\b`).test(mapSource.slice(0, mapSource.indexOf('} from "lucide-react"'))), `${icon} is imported from lucide-react`);
}
for (const label of ['Restaurants', 'Cafes', 'Shopping', 'Parks', 'Mooroolbark', 'Melbourne']) assert(chips.includes(`label: "${label}"`), `chip label "${label}" kept`);
assert(chips.includes('filter: "restaurant"') && chips.includes('filter: "cafe"') && chips.includes('filter: "shop"') && chips.includes('filter: "park"'), 'chip filters unchanged');

// --- Analysed layers panel ---------------------------------------------------------------------
assert(/<a href="\/scenes\/processing"[^>]*>Open processing<\/a>/.test(overlaySource), 'processing link reads Open processing with the same href');
assert(!/→/.test(overlaySource.slice(overlaySource.indexOf('return <details'))), 'no arrows in the panel JSX');
assert(!/text-\[(?:[0-9]|10)px\]/.test(overlaySource), 'nothing below 11 px in the panel');
assert(!/` · `|" · "| · \$\{/.test(overlaySource.slice(overlaySource.indexOf('return <details'))), "no ' · ' joins in the panel JSX");
assert(overlaySource.includes('Heights are model estimates'), 'heights are labelled as model estimates');
const popup = overlaySource.slice(overlaySource.indexOf('const select ='), overlaySource.indexOf('map.on("click", select)'));
assert(popup.length > 100 && !/→| · /.test(popup), 'the map popup uses sentences, no glyph arrows or mid-dot joins');
assert(popup.includes('"Review this object"'), 'popup link text kept without the arrow');
assert(popup.includes('var(--font-geist-sans)'), 'the map popup text renders in Geist');
assert(popup.includes('String(p.status ?? "")'), 'a popup feature without a status does not throw');

// --- Deep link + font --------------------------------------------------------------------------
assert(/params\.get\("crm"\) === "1"/.test(pageSource), 'page reads ?crm=1 like studio=1 / selectScene=1');
assert(/finally \{[\s\S]{0,400}crmDeepLink\.current = false/.test(pageSource), 'a failed or empty lookup consumes the ?crm=1 deep link so a later click never opens the panel');
assert(/if \(!address \|\| !Number\.isFinite\(address\.id\)\) return;/.test(pageSource), 'the deep link only opens the CRM for an address with a usable id');
assert(!/•|Connect-Go|reverse-geocode|Loading Offline Map Engine/.test(pageSource), 'map badge and loading copy are plain sentences that name the job');
const verifySource = read('scripts/verify-map-shell.mjs');
assert(verifySource.includes(`.filter(a => !a.closest('nav[aria-label="Workspaces"]'))`), "verify-map-shell excludes the nav's own Processing link from the Open processing check");
assert(/body\s*\{[^}]*font-family:\s*var\(--font-geist-sans\),\s*system-ui,\s*sans-serif;/s.test(globals), 'body uses Geist with system fallback');
console.log('Sidebar copy: light nav row, single address h1, provenance line, degenerate payloads, native layer checkboxes, Lucide chips, Open processing link and popup, ?crm=1 and Geist body passed.');
