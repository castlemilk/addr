import puppeteer from "puppeteer-core";
import { mkdirSync } from "node:fs";

const url = process.env.APP_URL || "http://localhost:3008/reviews";
const chrome = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
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
    "--window-size=1600,1000",
  ],
});

const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });

console.log("Navigating to", url);
await page.goto(url, { waitUntil: "networkidle2", timeout: 60000 });
await new Promise((resolve) => setTimeout(resolve, 2000));

// Wait for the SVG to be rendered
await page.waitForSelector("svg polygon.cursor-pointer", { timeout: 10000 });

// Click on the first roof polygon (Facet #1)
const polygons = await page.$$("svg polygon.cursor-pointer");
console.log(`Found ${polygons.length} clickable facet polygons`);
if (polygons.length > 0) {
  await polygons[1].click(); // click second polygon
  await new Promise((resolve) => setTimeout(resolve, 500));
  await page.screenshot({ path: `${outDir}/facet-selected.png` });
  console.log("Saved facet selection popover to", `${outDir}/facet-selected.png`);
}

// Click on a roofline
const lineTargets = await page.$$("svg line.cursor-pointer");
console.log(`Found ${lineTargets.length} clickable line targets`);
if (lineTargets.length > 0) {
  await lineTargets[3].click();
  await new Promise((resolve) => setTimeout(resolve, 500));
  await page.screenshot({ path: `${outDir}/line-selected.png` });
  console.log("Saved line selection popover to", `${outDir}/line-selected.png`);
}

await browser.close();
console.log("Interaction test completed successfully!");
