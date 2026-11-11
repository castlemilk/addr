// Captures what the CRM panel shows while the Go service is DOWN.
import puppeteer from "puppeteer-core";
const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browser = await puppeteer.launch({ executablePath: chromePath, headless: "new",
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1600,1000"] });
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  await page.goto(`${appUrl}/?lat=-37.79044&lon=145.31252&zoom=19`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.evaluate(() => window.localStorage.removeItem("addr.crm.token"));
  await page.reload({ waitUntil: "networkidle2", timeout: 120000 });
  await new Promise((r) => setTimeout(r, 9000));
  const clicked = await page.evaluate(() => {
    const el = [...document.querySelectorAll("button")].find((e) => (e.textContent || "").includes("Address CRM"));
    if (el) el.click();
    return !!el;
  });
  console.log("crm opened:", clicked);
  await new Promise((r) => setTimeout(r, 4000));
  const text = await page.evaluate(() => document.body.textContent);
  console.log("unreachable hint:", text.includes("unreachable"));
  console.log("key prompt:", text.includes("Paste API key"));
  console.log("no-key note:", text.includes("no key needed"));
  await page.screenshot({ path: "/tmp/crm-down.png" });
} finally { await browser.close(); }
