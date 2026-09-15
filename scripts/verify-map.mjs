import puppeteer from "puppeteer-core";
import { mkdirSync } from "node:fs";

const url = process.env.APP_URL || "http://localhost:3004";
const chrome =
  process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const outDir = process.env.SHOT_DIR || "/tmp/addr-verify";

mkdirSync(outDir, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: "new",
  args: [
    "--no-sandbox",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--window-size=1400,900",
  ],
});

const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 900, deviceScaleFactor: 1 });

const errors = [];
const tileRequests = [];
page.on("pageerror", (error) => errors.push(`[pageerror] ${error.message}`));
page.on("console", (message) => {
  if (message.type() === "error") errors.push(`[console] ${message.text()}`);
});
page.on("request", (request) => {
  if (request.url().includes("/tiles/")) tileRequests.push(request.url());
});

await page.goto(url, { waitUntil: "networkidle2", timeout: 120000 });
await new Promise((resolve) => setTimeout(resolve, 8000));

await page.evaluate(() => {
  [...document.querySelectorAll("button")]
    .find((button) => button.textContent?.includes("Satellite"))
    ?.click();
});
await new Promise((resolve) => setTimeout(resolve, 14000));
await page.screenshot({ path: `${outDir}/map.png` });

const openedStudio = await page.evaluate(() => {
  const button = [...document.querySelectorAll("button")].find((item) =>
    item.textContent?.includes("LoD2 3D")
  );
  button?.click();
  return Boolean(button);
});
if (openedStudio) {
  await page.waitForFunction(
    () => document.body.textContent?.includes("Deep 3D Satellite Studio") ?? false,
    { timeout: 30000 }
  );
  await new Promise((resolve) => setTimeout(resolve, 12000));
  await page.screenshot({ path: `${outDir}/studio.png` });
}

await browser.close();

console.log(`tile requests: ${tileRequests.length}`);
console.log(`studio opened: ${openedStudio}`);
if (errors.length > 0) {
  console.log("errors:");
  for (const error of errors.slice(0, 10)) console.log(`  ${error}`);
}
if (tileRequests.length === 0) {
  console.log("FAIL: no vector tile requests were made");
  process.exit(1);
}
if (errors.length > 0) {
  console.log("FAIL: page errors detected");
  process.exit(1);
}
console.log(`OK: screenshots in ${outDir}`);
