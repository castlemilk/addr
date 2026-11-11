// Light map shell (WS2): headless Chrome checks against a rebuilt web server.
//   APP_URL=http://localhost:3008 node scripts/verify-map-shell.mjs
// Asserts: workspace nav with aria-current on Map inside the sidebar, at most one h1 (the
// address), Geist body font, no uppercase text-transform in the sidebar, the pinned Satellite /
// LoD2 3D / Address CRM buttons, native layer checkboxes, Lucide chips, the Open processing
// link, and that /?crm=1 opens the Address CRM panel for the resolved address.
import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import puppeteer from "puppeteer-core";

const url = process.env.APP_URL || "http://localhost:3008";
const chrome = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const outDir = process.env.SHOT_DIR || "/tmp/addr-verify";
mkdirSync(outDir, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: "new",
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1400,900"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 900, deviceScaleFactor: 1 });
const errors = [];
page.on("pageerror", e => errors.push(`[pageerror] ${e.message}`));
page.on("console", m => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

const EMOJI = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/u;
try {
  await page.goto(`${url}/?lat=-37.79044&lon=145.31252`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector('aside[aria-label="Map sidebar"] nav[aria-label="Workspaces"]', { timeout: 60000 });
  await page.waitForFunction(() => document.querySelector("h1")?.textContent?.includes("CAMBRIDGE") ?? false, { timeout: 60000 });
  await new Promise(r => setTimeout(r, 3000));

  const shell = await page.evaluate(() => {
    const aside = document.querySelector('aside[aria-label="Map sidebar"]');
    const nav = aside.querySelector('nav[aria-label="Workspaces"]');
    const links = [...nav.querySelectorAll("a")].map(a => ({ text: a.textContent.trim(), href: a.getAttribute("href"), current: a.getAttribute("aria-current") }));
    const mapLink = nav.querySelector('a[aria-current="page"]');
    const mapRect = mapLink.getBoundingClientRect(), asideRect = aside.getBoundingClientRect();
    const h1s = [...document.querySelectorAll("h1")].map(h => ({ text: h.textContent.trim(), size: getComputedStyle(h).fontSize, weight: getComputedStyle(h).fontWeight }));
    const upper = [...aside.querySelectorAll("*")].filter(el => getComputedStyle(el).textTransform === "uppercase").map(el => el.textContent.trim().slice(0, 40));
    const small = [...aside.querySelectorAll("*")].filter(el => el.childNodes.length && [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) && parseFloat(getComputedStyle(el).fontSize) < 11).map(el => `${el.textContent.trim().slice(0, 30)} @ ${getComputedStyle(el).fontSize}`);
    const buttons = [...document.querySelectorAll("button")].map(b => b.textContent?.trim() ?? "");
    const checkboxes = [...aside.querySelectorAll('input[type="checkbox"]')].map(i => ({ label: i.closest("label")?.textContent.trim(), checked: i.checked }));
    const h2s = [...aside.querySelectorAll("h2")].map(h => ({ text: h.textContent.trim(), size: getComputedStyle(h).fontSize }));
    const chips = [...document.querySelectorAll("button")].filter(b => ["Restaurants", "Cafes", "Shopping", "Parks"].includes(b.textContent.trim())).map(b => ({ text: b.textContent.trim(), svg: !!b.querySelector("svg") }));
    // The workspace nav also links to /scenes/processing ("Processing"); only the analysed-layers panel link is under test here.
    const processing = [...document.querySelectorAll('a[href="/scenes/processing"]')].filter(a => !a.closest('nav[aria-label="Workspaces"]')).map(a => a.textContent.trim());
    const provenance = aside.querySelector('section[aria-label="Selected address"]')?.textContent ?? "";
    // At the desktop verify width the whole row must show: no hidden workspaces behind a scroll.
    const wrap = nav.parentElement, indexLeft = aside.querySelector('button[aria-label="Manage offline index"]').getBoundingClientRect().left;
    const hidden = [...nav.querySelectorAll("a")].filter(a => { const r = a.getBoundingClientRect(); return r.left < asideRect.left || r.right > Math.min(asideRect.right, indexLeft); }).map(a => a.textContent.trim());
    return {
      bodyFont: getComputedStyle(document.body).fontFamily,
      navFont: getComputedStyle(nav).fontFamily,
      navOverflow: wrap.scrollWidth - wrap.clientWidth, hidden,
      links, mapVisible: mapRect.left >= asideRect.left && mapRect.right <= asideRect.right && mapRect.width > 0,
      h1s, upper, small, buttons, checkboxes, h2s, chips, processing, provenance,
      asideText: aside.textContent,
      icons: { theme: !!aside.querySelector('button[aria-label^="Switch to"]'), index: !!aside.querySelector('button[aria-label="Manage offline index"]') },
      shadows: [...aside.querySelectorAll("*")].filter(el => el.className && /\bshadow-(xl|md|lg|2xl)\b/.test(el.className)).length,
    };
  });
  await page.screenshot({ path: `${outDir}/map-shell.png` });

  assert.deepEqual(shell.links.map(l => l.text), ["Map", "Scene review", "Review queues", "Processing", "Training"], "five workspaces in the sidebar nav");
  assert.deepEqual(shell.links.filter(l => l.current === "page").map(l => l.text), ["Map"], "aria-current on Map only");
  assert(shell.mapVisible, "the current Map link is visible inside the sidebar");
  assert(shell.navOverflow <= 0 && shell.hidden.length === 0, `all five workspaces are visible in one row at 1400px (overflow ${shell.navOverflow}px, hidden ${JSON.stringify(shell.hidden)})`);
  assert(shell.icons.theme && shell.icons.index, "theme toggle and Index are labelled icon buttons");
  assert(/Geist/.test(shell.bodyFont), `body font includes Geist (got ${shell.bodyFont})`);
  assert(/Geist/.test(shell.navFont), `nav font includes Geist (got ${shell.navFont})`);
  assert(shell.h1s.length <= 1, `at most one h1 (got ${JSON.stringify(shell.h1s)})`);
  assert.equal(shell.h1s.length, 1, "the sidebar address heading is the page h1");
  assert(shell.h1s[0].text.includes("27 CAMBRIDGE ROAD") && shell.h1s[0].size === "20px", `address h1 at 20px as stored (got ${JSON.stringify(shell.h1s[0])})`);
  assert.deepEqual(shell.upper, [], `no uppercase text-transform in the sidebar (got ${JSON.stringify(shell.upper)})`);
  assert.deepEqual(shell.small, [], `nothing below 11px in the sidebar (got ${JSON.stringify(shell.small)})`);
  assert.equal(shell.shadows, 0, "no decorative shadow classes in the sidebar");
  assert(shell.provenance.includes("G-NAF Rooftop Address") && shell.provenance.includes("from the address point"), `provenance line keeps the pinned label and names the address point (got ${shell.provenance.slice(0, 200)})`);
  assert(!EMOJI.test(shell.asideText), "no emoji in the sidebar text");
  for (const text of ["Satellite", "LoD2 3D", "Address CRM", "Inspect in Deep 3D Studio"]) assert(shell.buttons.some(b => b.includes(text)), `button "${text}" present`);
  assert.deepEqual(shell.checkboxes.map(c => c.label), ["Places", "Address points"], "native layer checkboxes labelled Places / Address points");
  assert(shell.checkboxes.every(c => c.checked), "both layers start on");
  assert(shell.h2s.some(h => h.text === "Map layers" && h.size === "14px"), `Map layers is a 14px h2 (got ${JSON.stringify(shell.h2s)})`);
  assert(shell.h2s.some(h => /^Nearby places \(\d+\)$/.test(h.text)), "Nearby places heading carries its count");
  assert.equal(shell.chips.length, 4, "four category chips");
  assert(shell.chips.every(c => c.svg), "category chips use Lucide icons");
  assert.deepEqual(shell.processing, ["Open processing"], `processing link reads Open processing (got ${JSON.stringify(shell.processing)})`);

  // Layer checkbox toggles the same state the old buttons did.
  await page.click('aside[aria-label="Map sidebar"] input[type="checkbox"]');
  assert.equal(await page.$eval('aside[aria-label="Map sidebar"] input[type="checkbox"]', i => i.checked), false, "Places checkbox turns the layer off");
  await page.click('aside[aria-label="Map sidebar"] input[type="checkbox"]');

  // Nav row stays a single row at the sidebar width: the icon buttons share the nav's line.
  const row = await page.evaluate(() => {
    const nav = document.querySelector('aside[aria-label="Map sidebar"] nav[aria-label="Workspaces"]').getBoundingClientRect();
    const index = document.querySelector('button[aria-label="Manage offline index"]').getBoundingClientRect();
    return { navTop: nav.top, navBottom: nav.bottom, indexCentre: index.top + index.height / 2 };
  });
  assert(row.indexCentre > row.navTop && row.indexCentre < row.navBottom, `Index button sits on the nav row (${JSON.stringify(row)})`);

  // ?crm=1 opens the Address CRM panel for the resolved address.
  await page.goto(`${url}/?lat=-37.79044&lon=145.31252&crm=1`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForFunction(() => [...document.querySelectorAll("div,h2,h3")].some(el => el.textContent.trim() === "Address CRM" && !el.closest("button") && el.getBoundingClientRect().width > 0), { timeout: 60000 });
  const crm = await page.evaluate(() => {
    const heading = [...document.querySelectorAll("div,h2,h3")].find(el => el.textContent.trim() === "Address CRM" && !el.closest("button"));
    const panel = heading.closest("aside, section, div[class*='fixed'], div[class*='absolute']") ?? heading.parentElement;
    return { heading: heading.textContent.trim(), panelText: panel.textContent.slice(0, 400), h1s: document.querySelectorAll("h1").length };
  });
  assert.equal(crm.heading, "Address CRM", "CRM panel heading visible via ?crm=1");
  assert(crm.panelText.includes("27 CAMBRIDGE ROAD"), `CRM panel is for the resolved address (got ${crm.panelText.slice(0, 160)})`);
  assert(crm.h1s <= 1, "opening the CRM panel adds no second h1");
  await page.screenshot({ path: `${outDir}/map-shell-crm.png` });

  const benign = errors.filter(e => !/favicon|ERR_BLOCKED_BY_CLIENT/.test(e));
  assert.deepEqual(benign, [], `page errors: ${benign.join(" | ")}`);
  console.log(`Map shell: light nav row with Map current, single 20px address h1, Geist body, no uppercase, pinned buttons, native layer checkboxes, Lucide chips, Open processing and ?crm=1 passed. Screenshots in ${outDir}`);
} catch (error) {
  await page.screenshot({ path: `${outDir}/map-shell-failure.png`, fullPage: true }).catch(() => {});
  console.error(error.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
