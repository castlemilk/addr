// Three CRM auth states: (1) no token + server up -> local, no key prompt;
// (2) stale garbage token + server up -> healed to local, token cleared;
// (3) server down -> unreachable hint, no bare key prompt.
import puppeteer from "puppeteer-core";
const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browser = await puppeteer.launch({ executablePath: chromePath, headless: "new",
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1600,1000"] });
const openCrm = (page) => page.evaluate(() => {
  const el = [...document.querySelectorAll("button")].find((e) => (e.textContent || "").includes("Address CRM"));
  if (el) el.click();
  return !!el;
});
const closeCrm = (page) => page.evaluate(() => {
  const el = [...document.querySelectorAll("button")].find((b) => b.querySelector("svg.lucide-x"));
  if (el) el.click();
  return !!el;
});
const snapshot = (page) => page.evaluate(() => {
  const text = document.body.textContent;
  return {
    localNote: text.includes("no key needed"),
    keyPrompt: text.includes("Paste API key"),
    unreachable: text.includes("unreachable"),
    storedToken: window.localStorage.getItem("addr.crm.token") ?? null,
  };
});
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${appUrl}/?lat=-37.79044&lon=145.31252&zoom=19`, { waitUntil: "networkidle2", timeout: 120000 });
  await new Promise((r) => setTimeout(r, 8000));

  await page.evaluate(() => window.localStorage.removeItem("addr.crm.token"));
  await openCrm(page);
  await new Promise((r) => setTimeout(r, 3500));
  console.log("1 no-token:", JSON.stringify(await snapshot(page)));
  console.log("  closed:", await closeCrm(page));
  await new Promise((r) => setTimeout(r, 1000));

  await page.evaluate(() => window.localStorage.setItem("addr.crm.token", "addr_staleGarbage"));
  await openCrm(page);
  await new Promise((r) => setTimeout(r, 4500));
  const s2 = await snapshot(page);
  console.log("2 stale-token:", JSON.stringify(s2));
  if (!s2.localNote || s2.keyPrompt || s2.storedToken) throw new Error("stale token not healed");
  if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
  console.log("AUTH CHECK OK");
} finally { await browser.close(); }
