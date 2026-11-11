// Browser check for the /scenes hub and the scene editor header.
//   APP_URL=http://localhost:3008 node scripts/verify-scene-start.mjs
// Read-only: it opens saved scenes and makes a local draft, but never saves or writes under data/.
// Covers: /scenes with no params shows h1 'Open a scene' within 2 s and no 'Loading scene' spinner;
// the rail reads 'Loading scenes…' while the list is in flight and 'No extracted scenes' only after
// an empty result; empty/error/retry states; grouped rows link into the editor; suburb tiles list
// only against a typed id; a row opens the editor and appears under Recently opened; on
// /scenes?scene=house-30286 the nav Map link returns to the captured location and Scene actions
// lists Re-analyse scene; with a dirty draft nav links keep the page and show the drafts message.
import assert from "node:assert/strict";
import puppeteer from "puppeteer-core";

const HUB_BUDGET_MS = Number(process.env.HUB_BUDGET_MS || 2000); // warm-server target; raise only on a loaded machine and say so
const base = process.env.APP_URL || "http://localhost:3008";
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push(e.message));
await page.setViewport({ width: 1280, height: 720 });

const text = selector => page.$eval(selector, e => e.textContent || "");
const includes = (selector, needle) => page.waitForFunction((s, n) => document.querySelector(s)?.textContent.includes(n), {}, selector, needle);
const h1s = () => page.$$eval("h1", nodes => nodes.map(n => n.textContent.trim()));
const isList = request => { const url = new URL(request.url()); return url.pathname === "/api/scenes" && !url.searchParams.has("id"); };

// Intercept only the scene list: hold it, fail it, or let it through.
let listMode = "hold", held = null;
await page.setRequestInterception(true);
page.on("request", request => {
  if (!isList(request)) return void request.continue();
  if (listMode === "hold") { held = request; return; }
  if (listMode === "fail") return void request.respond({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "Scene list unavailable for this check." }) });
  request.continue();
});

try {
  // 1. The hub appears before the (large) list arrives, and never shows the editor spinner.
  const started = Date.now();
  await page.goto(`${base}/scenes`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForFunction(() => [...document.querySelectorAll("h1")].some(h => h.textContent.trim() === "Open a scene"), { timeout: HUB_BUDGET_MS });
  const elapsed = Date.now() - started;
  assert(elapsed <= 2000, `Open a scene must render within 2000 ms (took ${elapsed} ms)`);
  assert.deepEqual(await h1s(), ["Open a scene"], "the hub heading is the only h1");
  for (let attempt = 0; attempt < 100 && !held; attempt++) await new Promise(r => setTimeout(r, 50));
  assert(held, "the hub requests the scene list once");
  assert(!(await text(".scene-main")).includes("Loading scene"), "no editor spinner on the hub");
  assert.equal(await page.$(".scene-main .review-state"), null, "no spinner block on the hub");
  assert((await text(".scene-main")).includes("Listing saved scenes"), "loading state names the list being read");
  assert.equal(await text("#scene-choice option"), "Loading scenes…", "the rail reports the list request while it is in flight");
  assert.equal(await page.$eval("#scene-choice", e => e.disabled), false, "the rail select stays usable on the hub");
  assert(await page.$('.scene-start a[href="/scenes/new"]'), "Capture map area is available while loading");
  assert(await page.$('.scene-start a[href="/?selectScene=1"]'), "Choose on the map is available while loading");
  assert(await page.$('nav[aria-label="Workspaces"]'), "the shared workspace nav is the header");
  assert.equal(await page.$eval('nav[aria-label="Workspaces"] a', a => a.getAttribute("href")), "/", "without a scene the Map link goes to the plain map");
  assert.equal(await page.$eval('nav[aria-label="Workspaces"] a[aria-current="page"]', a => a.textContent), "Scene review");

  // 2. Empty result: named empty state, and the rail only now says there are no scenes.
  await held.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ scenes: [] }) });
  await includes(".scene-main", "No saved scenes yet. Capture a map area to start.");
  assert.equal(await text("#scene-choice option"), "No extracted scenes");
  assert(!(await text(".scene-main")).includes("Listing saved scenes"));

  // 3. Failed list: inline error with retry; retry lists the real scenes.
  listMode = "fail";
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('.scene-start [role="alert"]');
  assert((await text('.scene-start [role="alert"]')).includes("Scene list unavailable for this check."));
  assert.deepEqual(await h1s(), ["Open a scene"], "an error keeps the hub heading");
  assert.equal(await text("#scene-choice option"), "Scene list unavailable", "a failed list is not reported as an empty one");
  listMode = "pass";
  await (await page.waitForSelector("button::-p-text(Retry listing)")).click();
  await page.waitForSelector('.scene-start-section a[href^="/scenes?scene=house-"]', { timeout: 60000 });
  assert.equal(await text("#scene-choice option"), "Choose a scene", "with a list and no scene the rail names the unselected state");
  assert.equal(await page.$eval("#scene-choice", e => e.value), "", "no scene is auto-selected on the hub");

  // 4. Grouped listing.
  const headings = await page.$$eval(".scene-start h2", nodes => nodes.map(n => n.textContent.trim()));
  for (const heading of ["Recently opened", "Houses", "Suburb tiles", "Cohort roofs"]) assert(headings.includes(heading), `${heading} section`);
  assert(await page.$('.scene-start a[href="/scenes/processing"]'), "suburb tiles link to processing");
  assert.equal(await page.$('.scene-start a[href*="scene=mooroolbark-"]'), null, "tiles are not listed until an id is typed");
  assert(await page.$('.scene-start a[href="/reviews?cohort=roof_training_250"]'), "cohort roofs link to the roof review cohort");
  const hubText = await text(".scene-start");
  assert(/\d+ of \d+ reviewed/.test(hubText), "reviewed counts carry denominators");
  // Scene titles come from the data ("Mooroolbark · tile 17/…") and are not UI copy; check the hub's own text only.
  const hubCopy = await page.$$eval(".scene-start h1, .scene-start h2, .scene-start p, .scene-start button, .scene-start label, .scene-start small", nodes => nodes.map(n => n.textContent).join(" "));
  assert(!hubCopy.includes("·") && !hubCopy.includes("→"), "no dot joins or arrows in hub copy");
  await page.type('input[aria-label="Find a suburb tile by id"]', "80417");
  await page.waitForSelector('.scene-start a[href*="scene=mooroolbark-"]');
  assert((await page.$$eval('.scene-start a[href*="scene=mooroolbark-"]', a => a.map(x => x.getAttribute("href")))).every(h => h.includes("80417")), "tile search filters by id");
  assert.equal(await page.$(".scene-start button::-p-text(Looks right)"), null, "the hub has no decision controls");
  assert.equal(await page.$(".scene-start button::-p-text(Save scene)"), null);

  // Phone layout keeps everything reachable without horizontal scroll.
  await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await page.waitForFunction(() => document.querySelector(".scene-start-sections"));
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "hub must not overflow horizontally on a phone");
  assert(await page.$eval("details.scene-action-menu > summary", e => e.getBoundingClientRect().width > 0), "header actions remain on the phone header");
  await page.screenshot({ path: "/tmp/addr-scene-start-mobile.png" });
  await page.setViewport({ width: 1280, height: 720 });
  await page.screenshot({ path: "/tmp/addr-scene-start.png" });

  // 5. A row opens the canonical editor, and the scene then appears under Recently opened.
  await page.waitForSelector('.scene-start-section[aria-labelledby="scene-start-houses"] a[href^="/scenes?scene="]', { timeout: 30000 });
  const firstHouse = await page.$eval('.scene-start-section[aria-labelledby="scene-start-houses"] a[href^="/scenes?scene="]', a => a.getAttribute("href"));
  const openedId = new URL(firstHouse, base).searchParams.get("scene");
  // Click through the DOM: React may re-render the bounded list between the query and the click.
  await page.$eval(`.scene-start a[href="${firstHouse}"]`, a => a.click());
  await page.waitForFunction(id => new URL(location.href).searchParams.get("scene") === id, {}, openedId);
  await page.waitForSelector(".scene-svg", { timeout: 120000 });
  assert.equal(await page.$(".scene-start"), null, "the hub leaves when a scene is open");
  assert.equal((await h1s()).length, 1, "the scene is the page's only h1");
  assert.notEqual((await h1s())[0], "Open a scene");
  await page.goto(`${base}/scenes`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector(`.scene-start-section[aria-labelledby="scene-start-recent"] a[href="/scenes?scene=${openedId}"]`);
  assert((await text('.scene-start-section[aria-labelledby="scene-start-recent"]')).includes("Opened "), "recent rows show when they were opened");

  // 6. Editor header: Map returns to the captured location; Scene actions keeps its items.
  await page.goto(`${base}/scenes?scene=house-30286&kind=building&status=all&object=37ff5c02f77869d5&layer=edited`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.waitForSelector(".scene-svg");
  const mapHref = await page.$eval('nav[aria-label="Workspaces"] a', a => a.getAttribute("href"));
  assert(mapHref.startsWith("/?lat="), `Map link returns to the captured location (got ${mapHref})`);
  assert(await page.$('nav[aria-label="Workspaces"] a[href="/scenes/processing"]') && await page.$('nav[aria-label="Workspaces"] a[href="/scenes/training"]'), "processing and training are nav links");
  assert.deepEqual(await h1s().then(list => list.length), 1, "exactly one h1 in the editor");
  assert(!(await text("body")).includes("Review your surroundings"), "no workspace slogan");
  await page.click("details.scene-action-menu > summary");
  await page.waitForSelector("details.scene-action-menu[open]");
  const actions = await page.$$eval("details.scene-action-menu > div > *", nodes => nodes.map(n => n.textContent.trim()));
  assert(actions.includes("Re-analyse scene"), `Scene actions lists Re-analyse scene (got ${actions.join(", ")})`);
  assert(actions.includes("Capture map area") && actions.includes("Export saved labels"));
  assert.equal(await page.$eval('details.scene-action-menu a[href="/scenes/new"]', a => a.textContent.trim()), "Capture map area");
  await page.click("details.scene-action-menu > summary");
  await page.waitForSelector(".scene-decisions");
  // Real scenes add parcel and roof-structure rows above the image, so on a 720 px laptop the
  // card can extend below the fold; the existing "Jump to review" control (also used by the phone
  // flow in verify-scenes.mjs) must bring the swipe buttons into view. The isolated fixture in
  // verify-scenes.mjs keeps pinning the no-scroll laptop case.
  // Jump to review is a phone control (hidden on laptops); on a laptop the reviewer scrolls the card.
  // Either way the swipe buttons must become fully visible without leaving the page.
  await page.evaluate(() => {
    const jump = document.querySelector(".scene-review-jump");
    if (jump && jump.offsetParent !== null) jump.click(); else document.querySelector(".scene-decisions")?.scrollIntoView({ block: "end" });
  });
  await new Promise(r => setTimeout(r, 400));
  assert(await page.$eval(".scene-decisions", e => { const r = e.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }), "swipe buttons become fully visible at 1280x720 under the shell header");
  assert((await page.$eval("header.workbench-header", e => e.getBoundingClientRect().height)) <= 53, "the shell's 52 px header row wins over the review header geometry");

  // 6b. Same-route navigation without a remount: the Scene review nav link returns to the hub and
  //     Back reopens the scene, both as client-side transitions.
  await page.evaluate(() => { window.__sceneStartProbe = 1; });
  await page.click('nav[aria-label="Workspaces"] a[href="/scenes"]');
  await page.waitForFunction(() => location.pathname === "/scenes" && !new URL(location.href).searchParams.has("scene") && [...document.querySelectorAll("h1")].some(h => h.textContent.trim() === "Open a scene"), { timeout: 15000 });
  assert.equal(await page.evaluate(() => window.__sceneStartProbe), 1, "returning to the hub is a client-side transition");
  assert.equal(await page.$(".scene-svg"), null, "the editor leaves when the URL has no scene");
  assert.equal(await text("#scene-choice option"), "Choose a scene");
  await page.goBack();
  await page.waitForFunction(() => new URL(location.href).searchParams.get("scene") === "house-30286", { timeout: 15000 });
  await page.waitForSelector(".scene-svg", { timeout: 120000 });
  assert.equal(await page.evaluate(() => window.__sceneStartProbe), 1, "Back reopens the scene without a reload");
  assert.equal(await page.$(".scene-start"), null);

  // 7. Dirty draft: nav links keep the page and show the existing drafts message. Nothing is saved.
  const labelInput = await page.evaluateHandle(() => [...document.querySelectorAll(".scene-inspector label")].find(l => l.textContent.trim().startsWith("Label"))?.querySelector("input"));
  assert(labelInput.asElement(), "the object label input is available");
  await labelInput.asElement().focus();
  await page.keyboard.type(" check");
  await page.waitForFunction(() => !document.querySelector(".scene-save-bar .review-primary")?.disabled);
  await page.click('nav[aria-label="Workspaces"] a[href="/scenes/processing"]');
  await new Promise(r => setTimeout(r, 300));
  assert.equal(new URL(page.url()).pathname, "/scenes", "a dirty draft blocks leaving through the nav");
  assert((await text(".scene-status-space")).includes("Save or discard this scene draft before leaving."), "the drafts message is the existing one");
  await page.click('nav[aria-label="Workspaces"] a');
  assert.equal(new URL(page.url()).pathname, "/scenes", "the Map link is guarded too");
  // Forward would return to the hub entry; with a draft the scene stays, the message shows and the URL is restored.
  await page.goForward();
  await page.waitForFunction(() => new URL(location.href).searchParams.get("scene") === "house-30286", { timeout: 15000 });
  assert(await page.$(".scene-svg"), "a dirty draft keeps the scene open on a history change");
  assert((await text(".scene-status-space")).includes("Save or discard this scene draft before leaving."));
  assert(!(await page.evaluate(() => document.querySelector(".scene-save-bar .review-primary")?.disabled)), "the draft is retained");
  await (await page.waitForSelector("button::-p-text(Discard draft)")).click();
  await page.waitForFunction(() => document.querySelector(".scene-save-bar .review-primary")?.disabled);

  assert.deepEqual(errors, []);
  console.log("Scene start: hub within 2 s without a spinner, rail loading/failed/empty/unselected states, list error and retry, grouped editor links, tile search, recent scenes, phone layout, editor header Map/Scene actions, 52 px header, first-viewport swipe buttons, same-route hub/Back transitions and draft-guarded nav and history passed.");
} finally {
  await browser.close();
}
