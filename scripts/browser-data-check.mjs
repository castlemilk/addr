// What does the browser actually load? Fetches /address-roofs.json in-page
// (reveals HTTP caching) and reports 27/29786/29788 state + cache headers.
import puppeteer from "puppeteer-core";
const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browser = await puppeteer.launch({ executablePath: chromePath, headless: "new",
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1600,1000"] });
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  await page.goto(`${appUrl}/?lat=-37.79044&lon=145.31252&zoom=19`, { waitUntil: "networkidle2", timeout: 120000 });
  await new Promise((r) => setTimeout(r, 8000));
  const out = await page.evaluate(async () => {
    const res = await fetch("/address-roofs.json");
    const headers = {};
    ["age", "cache-control", "etag", "last-modified"].forEach((h) => { headers[h] = res.headers.get(h); });
    const all = await res.json();
    const byId = {};
    for (const r of all) if ([29790, 29786, 29788].includes(r.address_id)) byId[r.address_id] = { area: r.area_m2, src: r.boundary_source, npts: r.ring_meters.length };
    return { total: all.length, records: byId, headers };
  });
  console.log(JSON.stringify(out, null, 1));
} finally { await browser.close(); }
