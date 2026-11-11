// Checks the sidebar headline for the default 27 Cambridge pin:
// expect address_only (no POI header), LKPT only under Nearby Places.
import puppeteer from "puppeteer-core";
const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browser = await puppeteer.launch({ executablePath: chromePath, headless: "new",
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1600,1000"] });
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${appUrl}/?lat=-37.79044&lon=145.31252&zoom=19`, { waitUntil: "networkidle2", timeout: 120000 });
  await new Promise((r) => setTimeout(r, 9000));
  const text = await page.evaluate(() => document.body.textContent);
  console.log("badge G-NAF Rooftop Address:", text.includes("G-NAF Rooftop Address"));
  console.log("badge POI + G-NAF Address:", text.includes("POI + G-NAF Address"));
  console.log("headline 27 CAMBRIDGE ROAD:", text.includes("27 CAMBRIDGE ROAD"));
  console.log("LKPT anywhere (nearby list):", text.includes("LKPT Fitness"));
  if (!text.includes("G-NAF Rooftop Address")) throw new Error("expected address_only badge");
  if (text.includes("POI + G-NAF Address")) throw new Error("distant POI still joined");
  if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
  console.log("POI JOIN CHECK OK");
} finally { await browser.close(); }
