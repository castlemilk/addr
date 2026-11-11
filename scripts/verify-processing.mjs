// Processing workspace against a running, rebuilt web server (APP_URL, default :3008):
//   summary-first paint, server-side paging, the shell header contract, the three eyes groups
//   in the inspector, the hidden-low footer link, type-scale floors and a 2 MB response ceiling.
//   APP_URL=http://localhost:3008 node scripts/verify-processing.mjs
import assert from "node:assert/strict";
import puppeteer from "puppeteer-core";

const base = process.env.APP_URL || "http://localhost:3008";
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const LIMIT = 2 * 1024 * 1024;

// Warm the route caches first so the timing below measures the warm path the page is designed for.
for (const part of ["summary", "roofs&page=1&size=50", "hidden-low"]) {
  const response = await fetch(`${base}/api/scenes/processing?part=${part}`);
  assert.equal(response.status, 200, `warm-up of part=${part} returned ${response.status}`);
}
const summary = await (await fetch(`${base}/api/scenes/processing?part=summary`)).json();
assert(summary.suburb && /^[a-f0-9]{16}$/.test(summary.suburb.run), "summary carries the frozen run");
const firstPage = await (await fetch(`${base}/api/scenes/processing?part=roofs&page=1&size=50`)).json();
assert(firstPage.rows.length <= 50 && firstPage.total >= firstPage.rows.length, "roofs part pages at 50");
assert(firstPage.rows.every(r => !("search_text" in r.properties)), "rows never carry search_text");
const hidden = await (await fetch(`${base}/api/scenes/processing?part=hidden-low`)).json();

const browser = await puppeteer.launch({ executablePath: chromePath, headless: true, args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });
const errors = [], oversized = [], sizes = [];
page.on("pageerror", e => errors.push(String(e.message).slice(0, 200)));
page.on("response", response => {
  response.buffer().then(buffer => {
    sizes.push([response.url().replace(base, ""), buffer.length]);
    if (buffer.length > LIMIT) oversized.push(`${response.url().replace(base, "")} ${buffer.length} bytes`);
  }).catch(() => undefined);
});
try {
  const started = Date.now();
  await page.goto(`${base}/scenes/processing`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForSelector(".processing-progress", { visible: true, timeout: 10000 });
  const progressAt = Date.now() - started;
  assert(progressAt <= 2000, `the progress strip took ${progressAt} ms to appear on a warm server (limit 2000 ms)`);

  // Shell contract: one nav row, the suburb is the only h1, the frozen scan line sits under it.
  assert.equal(await page.$$eval("h1", hs => hs.length), 1, "exactly one h1");
  assert.equal(await page.$eval("h1", e => e.textContent.trim()), "Mooroolbark");
  assert.equal(await page.$eval('.workbench-header .workbench-link[aria-current="page"]', e => e.textContent.trim()), "Processing");
  assert.match(await page.$eval(".processing-title-line", e => e.textContent.trim()), /^Frozen scan [a-f0-9]{16}, [\d,]+ tiles$/);
  assert.match(await page.$eval(".processing-progress-numbers", e => e.textContent), /of [\d,]+ tiles/, "tile progress carries its denominator");

  // First table page: at most 100 rows, a Showing a–b of n line and a page counter.
  await page.waitForSelector(".processing-table tbody tr", { timeout: 30000 });
  const rows = await page.$$eval(".processing-table tbody tr", rs => rs.length);
  assert(rows >= 1 && rows <= 100, `first page rendered ${rows} rows`);
  const pagination = await page.$eval(".processing-pagination", e => e.textContent);
  assert.match(pagination, /Showing 1–[\d,]+ of [\d,]+ roofs/, `pagination text: ${pagination}`);
  assert.match(pagination, /Page 1 of [\d,]+/);
  await page.click('.processing-pagination button[aria-label="Next page"]');
  await page.waitForFunction(() => /Showing 51–/.test(document.querySelector(".processing-pagination")?.textContent || ""), { timeout: 15000 });
  await page.click('.processing-pagination button[aria-label="Previous page"]');
  await page.waitForFunction(() => /Showing 1–/.test(document.querySelector(".processing-pagination")?.textContent || ""), { timeout: 15000 });

  // Selecting a roof opens the inspector with the three eyes groups as open h3 sections.
  await page.click(".processing-table tbody tr .processing-roof-name");
  await page.waitForSelector(".processing-inspector", { timeout: 10000 });
  await page.waitForFunction(() => {
    const heads = [...document.querySelectorAll(".processing-inspector h3")].map(h => h.textContent.trim());
    return ["Model evidence", "Reference agreement", "Other captures"].every(t => heads.includes(t));
  }, { timeout: 15000 });
  const summaries = await page.$$eval(".processing-eyes-group .processing-group-summary", ps => ps.map(p => p.textContent.trim()));
  assert.equal(summaries.length, 3, "each group leads with a one-line summary");
  assert(summaries.every(Boolean));
  const inspectorText = await page.$eval(".processing-inspector", e => e.innerText);
  assert(inspectorText.includes("model estimate, not accuracy"), "the inspector keeps the model-estimate caveat");
  assert(await page.$('.processing-inspector a.processing-primary[href^="/scenes?"]'), "the only action is the link into the canonical editor");

  // Copy rules on the surfaces this workspace owns: no middle-dot joins, no arrows in link text, no text under 11 px.
  for (const selector of [".workbench-header", ".processing-heading", ".processing-progress", ".processing-scan-note", ".processing-viewbar", ".processing-filters", ".processing-pagination", ".processing-eyes-group", ".processing-footer"]) {
    const text = await page.$$eval(selector, els => els.map(e => e.innerText).join("\n"));
    assert(!text.includes(" · "), `${selector} joins text with a middle dot`);
  }
  const arrowLinks = await page.$$eval(".processing-shell a", as => as.filter(a => /[→←]/.test(a.textContent)).length);
  assert.equal(arrowLinks, 0, "no arrows in link text");
  const tiny = await page.$$eval(".processing-shell *", els => els
    .filter(e => e.childElementCount === 0 && e.textContent.trim() && getComputedStyle(e).display !== "none" && !e.closest(".maplibregl-map"))
    .map(e => [e.className || e.tagName, parseFloat(getComputedStyle(e).fontSize)])
    .filter(([, size]) => size < 11).slice(0, 8));
  assert.deepEqual(tiny, [], `text below 11 px: ${JSON.stringify(tiny)}`);
  const caps = await page.$$eval(".processing-shell *", els => els
    .filter(e => e.childElementCount === 0 && /^[A-Z][A-Z ]{5,}$/.test(e.textContent.trim()) && !e.closest(".processing-roof-name, .processing-inspector h2, .building-address-summary, .maplibregl-map"))
    .map(e => e.textContent.trim()).slice(0, 5));
  assert.deepEqual(caps, [], `all-caps eyebrows remain: ${caps}`);

  // Footer: the hidden-low count links into the queues hub; nothing here approves or changes a roof.
  const footerLink = await page.$eval('.processing-footer a[href="/queues#hidden"]', e => e.textContent.trim());
  assert.match(footerLink, /^Low roofs hidden under higher bands \([\d,]+\)$/, `footer link: ${footerLink}`);
  assert.equal(footerLink, `Low roofs hidden under higher bands (${hidden.total.toLocaleString("en-AU")})`);
  const approveButtons = await page.$$eval(".processing-shell button", bs => bs.map(b => b.textContent.trim()).filter(t => /approve|reject|confirm/i.test(t)));
  assert.deepEqual(approveButtons, [], "no hub-level approve/reject controls");

  // Network ceiling: nothing over 2 MB in the first 30 seconds, including the polls.
  const remaining = 30000 - (Date.now() - started);
  if (remaining > 0) await new Promise(r => setTimeout(r, remaining));
  await new Promise(r => setTimeout(r, 500));
  assert.deepEqual(oversized, [], `responses over 2 MB: ${oversized.join("; ")}`);
  assert.deepEqual(errors, [], `page errors: ${errors.join("; ")}`);
  const largest = sizes.toSorted((a, b) => b[1] - a[1]).slice(0, 3);
  console.log(JSON.stringify({ ok: true, progressAt, rows, total: firstPage.total, hiddenLow: hidden.total, responses: sizes.length, largest }));
} finally {
  await browser.close();
}
