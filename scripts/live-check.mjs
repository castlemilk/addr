// With all services UP: no fallback badge, address_only 27 headline.
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
  console.log("fallback badge:", fallback, "| joined badge:", joined);
  if (fallback || joined) throw new Error("unexpected state on live path");
  if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
  console.log("LIVE CHECK OK");
} finally { await browser.close(); }
