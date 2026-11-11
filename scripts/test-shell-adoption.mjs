// Static contract for the training-workspace and imagery pages that adopt the app shell.
// The pages import heavy client modules, so this checks their source rather than rendering:
// every page renders <WorkbenchHeader>, no ad-hoc brand block or back link remains, the page
// CSS has no ALL-CAPS eyebrows, tracking or sub-11 px type, and JSX text carries no glyph
// arrows or ' · ' joins (DESIGN.md). imagery.css must consume the review tokens and Geist.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = file => readFileSync(file, 'utf8');
const pages = {
  'src/app/reviews/page.tsx': { h1: null, current: 'training' },
  'src/app/scenes/training/page.tsx': { h1: 'Training coverage', current: 'training' },
  'src/app/scenes/candidates/page.tsx': { h1: 'Model candidates', current: 'training' },
  'src/app/scenes/new/page.tsx': { h1: 'Capture a map area', current: 'scenes' },
  'src/components/scenes/TemporalWorkspace.tsx': { h1: 'Imagery dates', current: 'scenes' },
};
const styles = ['src/app/scenes/candidates/candidates.css', 'src/app/scenes/training/training.css', 'src/app/scenes/imagery/imagery.css'];

for (const [file, expected] of Object.entries(pages)) {
  const source = read(file);
  assert(source.includes('<WorkbenchHeader'), `${file} renders the shell header`);
  assert(/import \{[^}]*WorkbenchHeader[^}]*\} from "@\/components\/shell\/WorkbenchHeader"/.test(source), `${file} imports WorkbenchHeader from the shell`);
  assert(!source.includes('review-brand'), `${file} has no ad-hoc brand block`);
  assert(!source.includes('review-map-link'), `${file} has no ad-hoc back link; the nav owns workspace navigation`);
  assert(!/[→←↑↓↗]/.test(source), `${file} has no glyph arrows in JSX text (use Lucide icons or words)`);
  assert(!source.includes(' · '), `${file} has no ' · ' joins in JSX text`);
  assert(!/\p{Extended_Pictographic}/u.test(source), `${file} has no emoji`);
  // shell.css and reviews.css style .review-header at equal specificity: the shell import must follow the page CSS.
  const cssImports = [...source.matchAll(/^import "[^"]+\.css";/gm)];
  if (cssImports.length) assert(source.indexOf('from "@/components/shell/WorkbenchHeader"') > cssImports.at(-1).index, `${file} imports the shell after its CSS so the 52px shell row wins`);
  if (expected.h1) assert(new RegExp(`<h1[^>]*>\\s*${expected.h1}`).test(source) || source.includes(`"${expected.h1}"`), `${file} names the object of work: ${expected.h1}`);
}

// Existing browser pins: verify-reviews.mjs waits for body text "Roof review".
assert(read('src/app/reviews/page.tsx').includes('>Roof review<'), 'reviews keeps the quiet "Roof review" workspace label pinned by verify-reviews.mjs');
// Drafts guard stays wired to the shell nav, not a separate back link.
assert(/<WorkbenchHeader[^>]*onNavigate=\{guardExit\}/.test(read('src/app/reviews/page.tsx')), 'reviews passes its drafts guard to the nav');
// Candidates keeps navigation disabled during draft submission.
assert(/<WorkbenchHeader[^>]*onNavigate=/.test(read('src/app/scenes/candidates/page.tsx')), 'candidates passes its saving guard to the nav');
// Imagery dates is a scene-workspace view and links the map back to the scene.
const temporal = read('src/components/scenes/TemporalWorkspace.tsx');
assert(/<WorkbenchHeader[^>]*current="scenes"/.test(temporal), 'imagery dates is presented as scene review work');
assert(/hrefs=\{\{\s*map:/.test(temporal), 'imagery dates map link returns to the scene location when known');
const imageryPage = read('src/app/scenes/imagery/page.tsx');
assert(imageryPage.includes('reviews.css'), 'the imagery page loads the --review-* tokens');
assert(imageryPage.indexOf('imagery.css') < imageryPage.indexOf('TemporalWorkspace"'), 'the imagery page loads its CSS before the workspace so shell.css follows reviews.css');

// reviews.css and shell.css style .review-header at equal specificity and Next merges CSS per route in import
// order, which differs between adopters (scenes/queues import the shell first), so pages with their own CSS
// pin the 52px shell row with a scoped rule (same pattern as processing.css).
for (const [file, shell] of [['src/app/scenes/training/training.css', '.training-shell'], ['src/app/scenes/imagery/imagery.css', '.temporal-workspace']])
  assert(new RegExp(`${shell.replace('.', '\\.')} \\.review-header \\{[^}]*min-height: 52px;[^}]*padding: 0 24px;`).test(read(file)), `${file} pins the 52px shell row with a scoped .review-header rule`);
// Header-outside-<main> and per-state h1/nav checks render for real in scripts/test-shell-pages.mjs.

for (const file of styles) {
  const css = read(file);
  assert(!/text-transform:\s*uppercase/.test(css), `${file} has no ALL-CAPS text`);
  assert(!/letter-spacing:\s*\.?\d/.test(css), `${file} has no positive letter-spacing tracking`);
  for (const [, size] of css.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)) assert(Number(size) >= 11, `${file} has no type below 11px (found ${size}px)`);
  for (const [, size] of css.matchAll(/clamp\(\s*(\d+)px/g)) assert(Number(size) >= 11, `${file} clamp floor is at least 11px`);
}
const imagery = read('src/app/scenes/imagery/imagery.css');
for (const [, value] of imagery.matchAll(/\bfont(?:-family)?:\s*([^;]+);/g)) if (value.includes('system-ui')) assert(value.trim().startsWith('var(--font-geist-sans)'), `imagery.css uses Geist via var(--font-geist-sans), with system-ui only as its fallback (found "${value.trim()}")`);
assert(imagery.includes('var(--font-geist-sans)'), 'imagery.css sets the Geist face');
assert(!/#[0-9a-fA-F]{3,8}\b/.test(imagery.replace(/var\([^)]*\)/g, '')), 'imagery.css colours come from --review-* tokens (hex only as var() fallbacks)');
assert(imagery.includes('var(--review-'), 'imagery.css consumes the review tokens');

console.log('Shell adoption: header on every training/imagery page, no brand blocks or back links, no caps/tracking/sub-11px type, no glyph arrows or mid-dot joins, imagery on review tokens.');
