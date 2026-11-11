// The map sidebar's "Property structures" section must appear for an address that has an enrichment document
// (data/reviews/eyes/address-enrichment/addresses/<id>.json), with the allocation line and the "not labels" note,
// and must be absent for an address without one. Skips with a message when the document for the address is missing.
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import puppeteer from 'puppeteer-core';
const base = process.env.APP_URL || 'http://localhost:3008';
const addressId = Number(process.env.ADDRESS_ID || 29790);
// 27 CAMBRIDGE ROAD, Mooroolbark (G-NAF id 29790): the map resolves the address from the pin, so the deep link carries its point.
const point = { lat: Number(process.env.ADDRESS_LAT || -37.79044), lon: Number(process.env.ADDRESS_LON || 145.31252) };
const file = `data/reviews/eyes/address-enrichment/addresses/${addressId}.json`;
if (!existsSync(file)) { console.log(`Address enrichment: ${file} does not exist yet, so the sidebar section cannot be checked live. Skipped.`); process.exit(0); }
const doc = JSON.parse(readFileSync(file, 'utf8'));
const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage(), errors = [], enrichmentCalls = [];
page.on('pageerror', e => errors.push(e.message));
page.on('response', r => { if (r.url().includes('/api/address/enrichment')) enrichmentCalls.push(`${r.status()} ${new URL(r.url()).search}`); });
await page.setViewport({ width: 1400, height: 1000 });
const section = () => page.evaluate(() => {
  const el = document.querySelector('section[aria-label="Property structures"]');
  return el ? { heading: el.querySelector('h2')?.textContent || '', text: el.innerText, items: [...el.querySelectorAll('li')].map(li => li.innerText), links: [...el.querySelectorAll('a')].map(a => a.getAttribute('href')) } : null;
});
try {
  await page.goto(`${base}/?lat=${point.lat}&lon=${point.lon}`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForFunction(() => /<h1/.test(document.querySelector('section[aria-label="Selected address"]')?.innerHTML || ''), { timeout: 60000 });
  const heading = await page.evaluate(() => document.querySelector('section[aria-label="Selected address"] h1')?.textContent || '');
  assert(heading && doc.full_address && doc.full_address.toUpperCase().startsWith(heading.split(',')[0].toUpperCase()), `the pin resolves the enriched address (sidebar: ${heading}; document: ${doc.full_address})`);
  await page.waitForSelector('section[aria-label="Property structures"]', { timeout: 30000 });
  const found = await section();
  assert.equal(found.heading, 'Property structures', 'the section heading is sentence case');
  assert(found.text.includes('Agent hints, not labels'), `the not-labels note is present once: ${found.text}`);
  assert.equal((found.text.match(/not labels/g) || []).length, 1, 'the note appears once');
  if (doc.primary_roof) assert(found.items.some(item => /roof/i.test(item) && /m²/.test(item)), `the primary roof line carries a type and area: ${found.items.join(' | ')}`);
  if (doc.primary_roof?.band) assert(found.text.includes(`${doc.primary_roof.band} band (model estimate)`), 'the band reads as a model estimate');
  for (const structure of doc.structures || []) if (typeof structure.area_m2 === 'number') assert(found.text.includes(`${Math.round(structure.area_m2)} m²`), `structure ${structure.sub_kind} lists its area`);
  if (doc.allocation) assert(/Allocated to this address|Suggested for this address|Allocation is ambiguous|Shared with a neighbouring address|No roof allocated/.test(found.text), `the allocation reads in plain words: ${found.text}`);
  assert(found.links.every(href => href.startsWith('/scenes')), `review links stay inside the editor: ${found.links.join(', ')}`);
  assert(!/ · |→/.test(found.text), 'no middle dots or arrows');
  assert(enrichmentCalls.some(call => call.startsWith(`200 ?address=${addressId}`)), `the route answered 200 for the address: ${enrichmentCalls.join('; ')}`);
  await page.screenshot({ path: '/tmp/addr-address-enrichment.png' });
  // A pin far from any enriched address: the section renders nothing (404 from the route or no address at all).
  const control = { lat: Number(process.env.CONTROL_LAT || -37.7855), lon: Number(process.env.CONTROL_LON || 145.3245) };
  await page.goto(`${base}/?lat=${control.lat}&lon=${control.lon}`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForFunction(() => !/Reading the nearest/.test(document.querySelector('section[aria-label="Selected address"]')?.textContent || ''), { timeout: 60000 });
  await new Promise(resolve => setTimeout(resolve, 1500));
  const controlSection = await section();
  const controlCall = enrichmentCalls.at(-1) || '';
  if (controlCall.startsWith('200')) console.log(`Control pin also has enrichment (${controlCall}); the absence check is not informative here.`);
  else assert.equal(controlSection, null, `no Property structures section without a document (${controlCall || 'no call: no address at the pin'})`);
  assert.deepEqual(errors, []);
  console.log(`Address enrichment: Property structures rendered for address ${addressId} (${found.items.length} lines, allocation in plain words, one not-labels note); control pin ${controlSection ? 'also enriched' : 'shows nothing'}. Passed.`);
} catch (error) {
  await page.screenshot({ path: '/tmp/addr-address-enrichment-failure.png', fullPage: true });
  console.error(await page.evaluate(() => document.body.innerText.slice(0, 3000)));
  throw error;
} finally { await browser.close(); }
