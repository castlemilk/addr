// Browser verification for the Review queues workspace against a running build:
//   APP_URL=http://localhost:3008 node scripts/verify-queues.mjs
// /queues: navy shell + workbench header, one h1, the calibration tables hold 67 rows across the
// low/medium/high bands, the hidden table holds 55 rows behind #hidden, the checkpoint list links
// CP1, CP2 and CP2-CALIB, and no control on the page can approve, reject or change a roof.
// /queues/checkpoints/CP2 renders a <table> from the report markdown with Back to queues and Open on map.
// Both pages fit a 390 px phone without horizontal document scrolling, and neither logs a console error.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import puppeteer from "puppeteer-core";

const base = (process.env.APP_URL || "http://localhost:3008").replace(/\/$/, "");
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const outDir = process.env.OUT_DIR || "/tmp/addr-queues";
const EXPECTED = { calibrationRows: 67, bands: { low: 30, medium: 31, high: 6 }, hiddenRows: 55, checkpoints: ["CP1", "CP2", "CP2-CALIB"] };
await mkdir(outDir, { recursive: true });

const browser = await puppeteer.launch({ executablePath: chromePath, headless: true, args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage();
const errors = [];
// The last step deliberately requests a missing checkpoint (CP99 -> 404); resource errors logged during
// that probe are expected and excluded from the "no errors" check.
let probingMissing = false;
page.on("pageerror", e => errors.push("page: " + String(e.message).slice(0, 200)));
page.on("console", m => { if (m.type() === "error" && !(probingMissing && /404/.test(m.text()))) errors.push("console: " + m.text().slice(0, 200)); });
const noHorizontalScroll = async label => assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${label} must not scroll horizontally at ${await page.evaluate(() => window.innerWidth)} px`);
const texts = selector => page.$$eval(selector, nodes => nodes.map(n => (n.textContent || "").trim()));

try {
  // ---- /queues on a laptop ----
  await page.setViewport({ width: 1280, height: 800 });
  await page.goto(`${base}/queues`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForFunction(() => document.querySelectorAll('table[data-band] tbody tr').length > 0 && document.querySelectorAll('table[data-queue="hidden"] tbody tr').length > 0 && document.querySelectorAll('.queues-checkpoint').length > 0, { timeout: 120000 });
  assert.equal(await page.title(), "Review queues · GeoAU", "layout metadata title");
  assert.equal(await page.$eval("main", m => m.className), "review-shell queues-shell", "navy review shell");
  assert(await page.$("header.review-header.workbench-header"), "workbench header");
  assert.deepEqual(await texts('nav[aria-label="Workspaces"] a[aria-current="page"]'), ["Review queues"], "the queues workspace is current");
  assert.deepEqual(await texts("h1"), ["Review queues"], "the page's only h1");
  assert((await texts(".queues-title p"))[0].includes("Nothing on this page approves, rejects or changes a roof."), "the read-only line");
  assert(await page.evaluate(() => getComputedStyle(document.querySelector("h1")).fontFamily.toLowerCase().includes("geist")), "Geist on the h1");

  const bands = await page.$$eval("table[data-band]", tables => Object.fromEntries(tables.map(t => [t.dataset.band, t.querySelectorAll("tbody tr").length])));
  assert.deepEqual(bands, EXPECTED.bands, "low/medium/high tables hold the plan's groups");
  assert.equal(Object.values(bands).reduce((a, b) => a + b, 0), EXPECTED.calibrationRows, "67 calibration rows in total");
  assert((await texts("h2")).some(t => /^Calibration sample, plan [0-9a-f]{16}$/.test(t)), "calibration heading names the plan id");
  const progress = await texts(".queues-progress");
  assert(progress.includes("0 of 30 reviewed") || progress.some(t => /^\d+ of 30 reviewed$/.test(t)), `low band progress carries its denominator: ${progress.join(" | ")}`);
  assert(progress.some(t => /^\d+ of 31 reviewed$/.test(t)) && progress.some(t => /^\d+ of 6 reviewed$/.test(t)), "medium and high denominators");
  assert(progress.some(t => /^\d+ of 250 reviewed$/.test(t)), "roof training review out of 250");
  const intervals = await texts(".queues-interval");
  assert(intervals.every(t => /^(Interval not yet available|Interval not available: |Wilson 95%: )/.test(t)), `interval wording: ${intervals.join(" | ")}`);
  assert((await texts("table[data-band] thead th")).includes("Detector (model estimate)"), "detector column is labelled a model estimate");
  const statusLabels = await texts("table[data-band] .queues-status");
  assert(statusLabels.length === EXPECTED.calibrationRows && statusLabels.every(t => ["Pending", "Confirmed", "Rejected", "Needs correction", "Feedback needs recovery"].includes(t)), `every row has a text status label: ${[...new Set(statusLabels)].join(", ")}`);
  const openLinks = await page.$$eval("table[data-band] tbody a", links => links.map(a => a.getAttribute("href")));
  assert.equal(openLinks.length, EXPECTED.calibrationRows, "one Open link per calibration row");
  assert(openLinks.every(h => /^\/scenes\?scene=.+&object=.+&kind=building&status=all&layer=model$/.test(h)), "every calibration link opens the canonical editor");

  assert(await page.$("section#hidden"), "hidden section anchor");
  assert.equal(await page.$$eval('table[data-queue="hidden"] tbody tr', rows => rows.length), EXPECTED.hiddenRows, "55 hidden low roofs");
  assert((await texts("#hidden .queues-intro"))[0].startsWith(`The map shows only the highest-band capture of a roof. These ${EXPECTED.hiddenRows} low-evidence scan roofs`), "hidden intro with denominator");
  const hiddenLinks = await page.$$eval('table[data-queue="hidden"] tbody a', links => links.map(a => a.textContent.trim()));
  assert.equal(hiddenLinks.filter(t => t === "Open hidden").length, EXPECTED.hiddenRows, "Open hidden per row");
  assert.equal(hiddenLinks.filter(t => t === "Open shown").length, EXPECTED.hiddenRows, "Open shown per row");

  const checkpointHrefs = await page.$$eval(".queues-checkpoint a", links => links.map(a => a.getAttribute("href")));
  for (const id of EXPECTED.checkpoints) assert(checkpointHrefs.includes(`/queues/checkpoints/${id}`), `checkpoint ${id} is listed with a report link`);
  assert(await page.$('a[href="/scenes/processing?view=self-review"]') && await page.$('a[href="/reviews?cohort=roof_training_250"]'), "self-review and roof review links");

  const controls = await page.$$eval("button, form, input, select, textarea", nodes => nodes.map(n => `${n.tagName.toLowerCase()}${n.type ? `:${n.type}` : ""}${n.checked ? ":checked" : ""}`));
  assert(controls.length >= 1 && controls.every(c => c === "input:checkbox"), `the only controls on the hub are unchecked view-filter checkboxes (Hide flagged, Show fragments, Hide auto tier): rows only link into the editor and nothing mutates; got ${controls.join(", ")}`);
  const filterLabels = await page.$$eval("label", nodes => nodes.map(n => n.textContent.trim()).filter(t => /^(Hide flagged|Show fragments|Hide auto tier)/.test(t)));
  assert.equal(filterLabels.length, controls.length, `every checkbox is one of the three view filters: ${filterLabels.join(" | ")}`);
  assert.equal((await texts("label.queues-filter"))[0], "Hide flagged", "the filter label");
  assert((await texts("table[data-band] thead th")).includes("Agent hints (not labels)") && (await texts('table[data-queue="hidden"] thead th')).includes("Agent hints (not labels)"), "the hint column is labelled as agent hints, not labels");
  assert(await page.$$eval(".queues-hint", badges => badges.every(b => /^(Agent hint, not a label|Machine label, not a reviewer label)\. .+\.$/.test(b.getAttribute("title") || "") && !/[A-Z]{4,}/.test(b.textContent || ""))), "every badge tooltip opens with the not-a-label copy and carries evidence; labels do not shout");
  const authored = await page.$$eval("h1, h2, h3, th, .queues-title p, .queues-intro, .queues-line, .queues-progress, .queues-interval, .queues-status, .queues-checkpoint h3, .queues-checkpoint a, .queues-table a", nodes => nodes.map(n => n.textContent).join("\n"));
  assert(!/ · |→/.test(authored), "no dot joins or arrows in authored text (titles are shown with commas)");
  const headings = await texts("h1, h2, h3, th");
  assert(headings.every(t => !/[A-Z]{4,}/.test(t)), `no shouting in headings (report titles arrive as EYES CP2 and are shown as Eyes CP2): ${headings.filter(t => /[A-Z]{4,}/.test(t)).join(" | ")}`);
  const hiddenCaptures = await page.$$eval('table[data-queue="hidden"] tbody td:first-child .queues-secondary', nodes => nodes.map(n => n.textContent.trim()));
  assert(hiddenCaptures.length === EXPECTED.hiddenRows && hiddenCaptures.every(t => /^\S+ \S+$/.test(t)), "each hidden row names its own capture and object id");
  const selfReview = (await texts(".queues-line"))[0] || "";
  assert(!selfReview.includes("Run ") || selfReview.includes("Outcomes are the loop's own model estimates, not human reviews."), `self-review outcomes are labelled model estimates: ${selfReview}`);
  assert((await texts(".queues-intro")).some(t => /^\d+ fixed-hash roofs: \d+ low, \d+ medium, \d+ high\. Review each boundary from the image, then confirm with geometry reviewed or reject\. Intervals appear at \d+ reviewed groups per band\.$/.test(t)), "plain-words calibration intro with denominators");
  assert(!(await page.$$eval("main *", nodes => nodes.some(n => getComputedStyle(n).textTransform === "uppercase" || parseFloat(getComputedStyle(n).fontSize) < 11 && n.textContent.trim()))), "no uppercase transforms and nothing below 11 px");
  assert(await page.$$eval(".queues-panel", panels => panels.every(p => getComputedStyle(p).overflowX === "auto" && getComputedStyle(p).boxShadow === "none")), "panels scroll within themselves and carry borders, not shadows");
  await page.screenshot({ path: `${outDir}/queues-desktop.png`, fullPage: false });

  // ---- /queues on a phone ----
  await page.setViewport({ width: 390, height: 844 });
  await page.reload({ waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForFunction(() => document.querySelectorAll('table[data-band] tbody tr').length > 0 && document.querySelectorAll('table[data-queue="hidden"] tbody tr').length > 0, { timeout: 120000 });
  await noHorizontalScroll("/queues");
  assert(await page.$$eval(".queues-panel", panels => panels.length > 0 && panels.every(p => p.getBoundingClientRect().right <= window.innerWidth + 1 && p.scrollWidth >= p.clientWidth)), "every table panel stays inside the phone viewport and scrolls within itself");
  await page.screenshot({ path: `${outDir}/queues-mobile.png`, fullPage: false });

  // ---- one checkpoint report ----
  await page.setViewport({ width: 1280, height: 800 });
  await page.goto(`${base}/queues/checkpoints/CP2`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector(".queues-report-body table", { timeout: 120000 });
  assert.deepEqual(await texts('nav[aria-label="Workspaces"] a[aria-current="page"]'), ["Review queues"], "the report stays in the queues workspace");
  const h1 = await texts("h1");
  assert.equal(h1.length, 1, "one h1 on the report");
  assert(h1[0] && h1[0] !== "Review queues" && !/^Checkpoint CP2$/.test(h1[0]), `h1 is the checkpoint title: ${h1[0]}`);
  assert(!/ · |→|[A-Z]{4,}/.test(h1[0]), `the report title is shown in sentence case with commas: ${h1[0]}`);
  assert((await page.$$eval(".queues-report-body h1", nodes => nodes.length)) === 0 && (await page.$$eval(".queues-report-body h2", nodes => nodes.length)) >= 1, "report sections render as h2 under the page h1");
  assert((await texts(".queues-report-meta"))[0].includes("model estimates, not accuracy"), "the report meta line says bands and agreement are model estimates");
  assert.equal(await page.$eval('.queues-report-actions a[href="/queues"]', a => a.textContent.trim()), "Back to queues", "Back to queues link");
  const mapHref = await page.$$eval(".queues-report-actions a", links => links.map(a => a.getAttribute("href")).find(h => h.startsWith("/?lat=")));
  assert(mapHref && /^\/\?lat=-?\d+(\.\d+)?&lon=-?\d+(\.\d+)?&crm=1$/.test(mapHref), `Open on map carries the anchor coordinates: ${mapHref}`);
  assert.equal(await page.$$eval("button, form", nodes => nodes.length), 0, "the report has no controls");
  await page.screenshot({ path: `${outDir}/checkpoint-desktop.png`, fullPage: false });
  await page.setViewport({ width: 390, height: 844 });
  await page.reload({ waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector(".queues-report-body table", { timeout: 120000 });
  await noHorizontalScroll("/queues/checkpoints/CP2");
  await page.screenshot({ path: `${outDir}/checkpoint-mobile.png`, fullPage: false });

  // ---- a missing checkpoint explains itself and offers retry ----
  probingMissing = true;
  await page.goto(`${base}/queues/checkpoints/CP99`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector('[role="alert"]', { timeout: 60000 });
  assert((await texts('[role="alert"]'))[0].startsWith("Checkpoint CP99 could not be read:"), "missing checkpoint error names the checkpoint");
  assert.equal(await page.$eval('[role="alert"] button', b => b.textContent.trim()), "Retry", "retry is offered");

  assert.deepEqual(errors, [], "no page or console errors");
  console.log(`Queues browser: /queues with ${EXPECTED.calibrationRows} calibration rows (${Object.entries(bands).map(([b, n]) => `${b} ${n}`).join(", ")}), ${EXPECTED.hiddenRows} hidden rows, checkpoints ${EXPECTED.checkpoints.join("/")}, no controls; /queues/checkpoints/CP2 table, back and map links; phone widths; no console errors passed. Screenshots in ${outDir}.`);
} finally {
  await browser.close();
}
