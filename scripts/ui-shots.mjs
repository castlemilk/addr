// UI audit screenshots: every page at desktop (1440x900) and key pages at mobile (390x844).
//   APP_URL=http://localhost:3008 OUT_DIR=/tmp/addr-eyes/ui node scripts/ui-shots.mjs
// Prints one JSON line per page: title, page/console errors, visible links.
import puppeteer from "puppeteer-core";
const appUrl = process.env.APP_URL || "http://localhost:3008";
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const pages = [
  ["home-map", "/"],
  ["home-studio", "/?lat=-37.79044&lon=145.31252&studio=1"],
  ["reviews", "/reviews?cohort=roof_training_250"],
  ["scenes-hub", "/scenes"],
  ["scene-editor", "/scenes?scene=house-30286&object=37ff5c02f77869d5&kind=building&status=all&layer=edited"],
  ["scene-editor-model", "/scenes?scene=mooroolbark-50011498-17-118442-80417&object=44c724fd3480c167&kind=building&status=all&layer=model"],
  ["processing", "/scenes/processing"],
  ["processing-self-review", "/scenes/processing?view=self-review"],
  ["candidates", "/scenes/candidates"],
  ["imagery", "/scenes/imagery?scene=mooroolbark-50011498-17-118441-80414"],
  ["training", "/scenes/training"],
  ["scenes-new", "/scenes/new"],
  ["queues", "/queues"],
  ["queues-checkpoint", "/queues/checkpoints/CP2"],
];
const browser = await puppeteer.launch({ executablePath: chromePath, headless: "new", args: ["--no-sandbox","--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader","--window-size=1440,900"] });
const shoot = async (name, path, vw, vh, tag) => {
  const page = await browser.newPage();
  await page.setViewport({ width: vw, height: vh, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", e => errors.push(String(e.message).slice(0, 160)));
  page.on("console", m => { if (m.type() === "error") errors.push("console: " + m.text().slice(0, 160)); });
  try {
    await page.goto(appUrl + path, { waitUntil: "networkidle2", timeout: 45000 });
  } catch (e) { errors.push("goto: " + e.message.slice(0, 100)); }
  await new Promise(r => setTimeout(r, name.startsWith("processing") ? 12000 : 5000));
  const file = `${process.env.OUT_DIR || "/tmp/addr-eyes/ui"}/${name}-${tag}.png`;
  await page.screenshot({ path: file, fullPage: false });
  const links = await page.evaluate(() => Array.from(document.querySelectorAll("a[href]")).map(a => (a.textContent || "").trim().slice(0, 40) + " -> " + a.getAttribute("href")).filter(x => x.length < 90).slice(0, 25));
  const title = await page.title();
  console.log(JSON.stringify({ name, tag, title, errors: errors.slice(0, 5), links }));
  await page.close();
};
for (const [name, path] of pages) await shoot(name, path, 1440, 900, "desktop");
for (const [name, path] of pages.filter(([n]) => ["home-map","scenes-hub","scene-editor","processing","queues"].includes(n))) await shoot(name, path, 390, 844, "mobile");
await browser.close();
