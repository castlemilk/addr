// With the Go service DOWN: sidebar must show address_only (no LKPT header)
// plus the Fallback data badge.
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
  await new Promise((r) => setTimeout(r, 12000));
  const text = await page.evaluate(() => document.body.textContent);
  const fallback = text.includes("Fallback data");
  const joined = text.includes("POI + G-NAF Address");
  const rooftop = text.includes("G-NAF Rooftop Address");
  const headline27 = text.includes("27 CAMBRIDGE ROAD");
  console.log("fallback badge:", fallback, "| joined badge:", joined, "| rooftop badge:", rooftop, "| 27 headline:", headline27);
  await page.screenshot({ path: "/tmp/fallback-check.png" });
  if (!fallback) throw new Error("fallback badge missing");
  if (joined) throw new Error("distant POI still joined on fallback path");
  if (!rooftop || !headline27) throw new Error("expected address_only 27 headline");
  if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
  console.log("FALLBACK CHECK OK");
} finally { await browser.close(); }
