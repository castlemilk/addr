// App-shell adoption on the training-workspace and imagery pages, in a real browser after a rebuild:
//   APP_URL=http://localhost:3008 node scripts/verify-shell.mjs
// Per page: exactly one nav[aria-label="Workspaces"], exactly one h1 (the object of work), aria-current
// on the expected workspace, the 52px shell row (reviews.css and shell.css style .review-header at equal
// specificity, so a wrong CSS chunk order shows up here as a taller header), the nav outside <main>, no
// ALL-CAPS text inside header/main/aside, the document title unchanged, the Map link where the page says
// it goes, and a header that fits a 390px phone viewport. Page errors fail the run.
// Known exclusion: the reviews canvas owners (RoofReviewCanvas/Roof3DPreview inside .review-canvas-wrap)
// still carry Tailwind "uppercase" layer labels; they are outside this workstream and are reported, not asserted.
import assert from "node:assert/strict";
import puppeteer from "puppeteer-core";

const base = process.env.APP_URL || "http://localhost:3008";
const chrome = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const pages = [
  { path: "/reviews", workspace: "Training", title: /^Roof review/, ready: "h1", map: "/", headerText: "Roof review" },
  { path: "/scenes/training", workspace: "Training", title: /^Training coverage/, ready: ".training-metrics, .training-empty, .training-feedback [role=alert]" },
  { path: "/scenes/candidates", workspace: "Training", title: /^Model candidates/, ready: ".candidate-filters, .training-empty, .review-notice" },
  { path: "/scenes/new", workspace: "Scene review", title: /^Scene review/, ready: ".scene-capture form", map: /^\/\?lat=-?[\d.]+&lon=-?[\d.]+$/ },
  { path: "/scenes/imagery?scene=mooroolbark-50011498-17-118441-80414", workspace: "Scene review", title: /^Imagery dates/, ready: ".temporal-heading h1", h1: "Imagery dates" },
];

const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push(`${page.url()}: ${e.message}`));

try {
  for (const spec of pages) {
    await page.setViewport({ width: 1440, height: 900 });
    await page.goto(base + spec.path, { waitUntil: "networkidle2", timeout: 120000 });
    await page.waitForSelector('nav[aria-label="Workspaces"]', { timeout: 30000 });
    // Data-dependent content may take a while (coverage reads up to 70 s); the shell checks do not depend on it.
    await page.waitForSelector(spec.ready, { timeout: 90000 }).catch(() => console.log(`${spec.path}: content still loading; checking the shell anyway`));
    if (spec.path === "/reviews") await page.waitForFunction(() => !document.body.textContent.includes("Loading roof reviews"), { timeout: 60000 }).catch(() => {});

    const state = await page.evaluate(() => ({
      navs: document.querySelectorAll('nav[aria-label="Workspaces"]').length,
      headings: [...document.querySelectorAll("h1")].map(h => h.textContent.trim()),
      current: [...document.querySelectorAll('nav[aria-label="Workspaces"] a[aria-current="page"]')].map(a => a.textContent.trim()),
      links: [...document.querySelectorAll('nav[aria-label="Workspaces"] a')].map(a => a.textContent.trim()),
      headerHeight: document.querySelector("header.workbench-header")?.getBoundingClientRect().height ?? 0,
      headerInMain: Boolean(document.querySelector("main header.workbench-header")),
      headerText: document.querySelector("header.workbench-header")?.textContent ?? "",
      mapHref: document.querySelector('nav[aria-label="Workspaces"] a')?.getAttribute("href") ?? "",
      uppercase: [...document.querySelectorAll("header *, main *, aside *")].filter(e => e.textContent.trim() && getComputedStyle(e).textTransform === "uppercase")
        .map(e => ({ text: e.textContent.trim().slice(0, 40), known: Boolean(e.closest(".review-canvas-wrap")) })),
      fonts: [...new Set([...document.querySelectorAll("header, main, main h1, main h2, main p, main button")].map(e => getComputedStyle(e).fontFamily.split(",")[0].trim().replace(/"/g, "")))],
    }));
    const title = await page.title();
    assert.equal(state.navs, 1, `${spec.path}: exactly one workspace nav`);
    assert.equal(state.headings.length, 1, `${spec.path}: exactly one h1 (found ${JSON.stringify(state.headings)})`);
    assert.deepEqual(state.links, ["Map", "Scene review", "Review queues", "Processing", "Training"], `${spec.path}: five workspaces in order`);
    assert.deepEqual(state.current, [spec.workspace], `${spec.path}: ${spec.workspace} is the current workspace`);
    assert.match(title, spec.title, `${spec.path}: document title unchanged`);
    if (spec.h1) assert.equal(state.headings[0], spec.h1, `${spec.path}: h1 names the object of work`);
    // 52px nominal; anything past 60px means reviews.css's 68px/16px-padding .review-header beat the shell row.
    assert(state.headerHeight > 0 && state.headerHeight <= 60, `${spec.path}: the shell header is the 52px row (measured ${state.headerHeight}px)`);
    assert(!state.headerInMain, `${spec.path}: the shell header (nav landmark) sits outside <main>`);
    if (spec.headerText) assert(state.headerText.includes(spec.headerText), `${spec.path}: header carries "${spec.headerText}"`);
    if (spec.map) assert(typeof spec.map === "string" ? state.mapHref === spec.map : spec.map.test(state.mapHref), `${spec.path}: Map link goes where the page says (found "${state.mapHref}")`);
    const caps = state.uppercase.filter(u => !u.known);
    assert.equal(caps.length, 0, `${spec.path}: no ALL-CAPS text inside header/main: ${JSON.stringify(caps)}`);
    const known = state.uppercase.filter(u => u.known);
    if (known.length) console.log(`${spec.path}: known uppercase labels inside the review canvas owners (outside this workstream): ${JSON.stringify(known.map(k => k.text))}`);
    assert(state.fonts.every(f => /Geist|__geist/i.test(f)), `${spec.path}: header and main render in Geist (found ${JSON.stringify(state.fonts)})`);

    // Tablet: the ~510px nav plus page actions must wrap into a second row rather than overlap or overflow.
    await page.setViewport({ width: 900, height: 700 });
    await new Promise(r => setTimeout(r, 400));
    const tablet = await page.evaluate(() => {
      const header = document.querySelector("header.workbench-header"), actions = header?.querySelector(".review-header-actions");
      const box = header.getBoundingClientRect(), slot = actions?.getBoundingClientRect();
      const links = [...header.querySelectorAll("nav a")].map(a => a.getBoundingClientRect());
      const overlaps = slot ? links.filter(l => l.right > slot.left + 1 && l.left < slot.right - 1 && l.bottom > slot.top + 1 && l.top < slot.bottom - 1).length : 0;
      return { scrollWidth: document.documentElement.scrollWidth, innerWidth, inside: links.every(l => l.right <= box.right + 1 && l.left >= box.left - 1), overlaps, height: box.height };
    });
    assert(tablet.scrollWidth <= tablet.innerWidth + 1, `${spec.path}: no horizontal page scroll at 900px (scrollWidth ${tablet.scrollWidth})`);
    assert(tablet.inside && tablet.overlaps === 0, `${spec.path}: nav links stay inside the header and clear of the actions at 900px (${tablet.overlaps} overlapping, header ${tablet.height}px)`);

    await page.setViewport({ width: 390, height: 844 });
    await new Promise(r => setTimeout(r, 400));
    const header = await page.$eval("header.workbench-header", e => ({ right: e.getBoundingClientRect().right, navs: e.querySelectorAll("nav").length }));
    assert(header.right <= 390 + 1, `${spec.path}: the shell header fits a 390px viewport (right edge ${header.right})`);
    assert.equal(header.navs, 1, `${spec.path}: header keeps one nav on mobile`);
    console.log(`${spec.path}: nav, h1 "${state.headings[0]}", current ${spec.workspace}, title "${title}", mobile header fits.`);
  }
  assert.deepEqual(errors, [], "no page errors");
  console.log("Shell adoption verified on reviews, training, candidates, capture and imagery dates.");
} catch (error) {
  console.error(page.url());
  console.error(await page.$eval("body", e => e.innerText.slice(0, 2500)).catch(() => ""));
  await page.screenshot({ path: "/tmp/addr-shell-failure.png", fullPage: true }).catch(() => {});
  throw error;
} finally {
  await browser.close();
}
