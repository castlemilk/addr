// Reproduce the user's exact view: satellite base + pitched 3D, same framing.
import puppeteer from "puppeteer-core";
const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browser = await puppeteer.launch({ executablePath: chromePath, headless: "new",
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1600,1000"] });
const clickByText = (page, tag, text) => page.evaluate((tag, text) => {
  const el = [...document.querySelectorAll(tag)].find((e) => (e.textContent || "").includes(text));
  if (el) el.click();
  return !!el;
}, tag, text);
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${appUrl}/?lat=-37.79044&lon=145.31252&zoom=19`, { waitUntil: "networkidle2", timeout: 120000 });
  await new Promise((r) => setTimeout(r, 9000));
  await clickByText(page, "button", "Satellite");
  await new Promise((r) => setTimeout(r, 4000));
  await page.evaluate(() => {
    const map = window.__addrMap;
    if (map) { map.jumpTo({ center: [145.31252, -37.79044], zoom: 19, pitch: 60, bearing: 0 }); }
  });
  await new Promise((r) => setTimeout(r, 8000));
  await page.screenshot({ path: "/tmp/user-view-repro.png" });
  if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
  console.log("USER VIEW REPRO OK");
} finally { await browser.close(); }
