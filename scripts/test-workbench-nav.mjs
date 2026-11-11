// Renders the app-shell nav with stubbed Next primitives and checks the workspace contract:
// five links in order, aria-current on exactly the current workspace, href overrides, and
// the page's navigation guard wired to every link.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
let pathname = '/';
const load = (file, extra = {}) => {
  const exports = {};
  const code = ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => {
    if (id.endsWith('.css')) return {};
    if (id === 'next/link') return { __esModule: true, default: ({ href, children, onClick, ...rest }) => React.createElement('a', { href, 'data-guarded': onClick ? 'yes' : 'no', ...rest }, children) };
    if (id === 'next/navigation') return { usePathname: () => pathname };
    if (id === 'lucide-react') return { Layers3: () => React.createElement('svg') };
    if (id === './WorkbenchNav') return extra.nav;
    return require(id);
  });
  return exports;
};
const nav = load('src/components/shell/WorkbenchNav.tsx');
const header = load('src/components/shell/WorkbenchHeader.tsx', { nav });

const links = html => [...html.matchAll(/<a ([^>]*)>([^<]*)<\/a>/g)].map(m => ({ attrs: m[1], text: m[2] }));
const current = html => links(html).filter(l => l.attrs.includes('aria-current="page"')).map(l => l.text);

let html = renderToStaticMarkup(React.createElement(nav.WorkbenchNav));
assert.deepEqual(links(html).map(l => l.text), ['Map', 'Scene review', 'Review queues', 'Processing', 'Training'], 'five workspaces in nav order');
assert.deepEqual(current(html), ['Map'], 'the map is current at /');
assert(html.includes('aria-label="Workspaces"') && html.includes('GeoAU'), 'nav landmark and wordmark');
assert(links(html).every(l => l.attrs.includes('data-guarded="no"')), 'no guard unless the page passes one');

for (const [path, expected] of [['/scenes?scene=x', 'Scene review'], ['/scenes/new', 'Scene review'], ['/scenes/imagery', 'Scene review'], ['/scenes/processing', 'Processing'], ['/scenes/training', 'Training'], ['/scenes/candidates', 'Training'], ['/reviews', 'Training'], ['/queues/checkpoints/CP2', 'Review queues']]) {
  pathname = path.split('?')[0];
  assert.deepEqual(current(renderToStaticMarkup(React.createElement(nav.WorkbenchNav))), [expected], `${path} -> ${expected}`);
}
pathname = '/scenes';
html = renderToStaticMarkup(React.createElement(nav.WorkbenchNav, { hrefs: { map: '/?lat=-37.79&lon=145.31' }, onNavigate: () => {}, current: 'training', tone: 'light' }));
assert(links(html)[0].attrs.includes('href="/?lat=-37.79&amp;lon=145.31"'), 'href override for the map link');
assert.deepEqual(current(html), ['Training'], 'explicit current wins over the pathname');
assert(links(html).every(l => l.attrs.includes('data-guarded="yes"')), 'the page guard is wired to every link');
assert(html.includes('workbench-nav-light'), 'light tone class');

html = renderToStaticMarkup(React.createElement(header.WorkbenchHeader, { actions: React.createElement('button', null, 'Export') }));
assert(html.startsWith('<header class="review-header workbench-header">'), 'header keeps the review-header owner class');
assert(/<div class="review-header-actions"><button>Export<\/button><\/div>/.test(html), 'actions render in the existing actions slot');
assert(!/<h1/.test(html), 'the shell never renders a heading');
console.log('Workbench nav: five workspaces, aria-current per route, href overrides, guard wiring and header slot passed.');
