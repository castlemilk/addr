// Renders the Deep 3D Studio for a coordinate and screenshots the 3D canvas.
//
//   node scripts/studio-shot.mjs --lat -37.79044 --lon 145.31252 --out data/reviews/1/3d.png
//
// Requires the web app (task start) and system Chrome. APP_URL/CHROME_PATH env
// vars override the defaults.
import puppeteer from "puppeteer-core";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

const args = process.argv.slice(2);
const value = (flag, fallback = null) => {
  const index = args.indexOf(flag);
  return index >= 0 && index + 1 < args.length ? args[index + 1] : fallback;
};

const lat = Number.parseFloat(value("--lat", "NaN"));
const lon = Number.parseFloat(value("--lon", "NaN"));
const out = value("--out", "/tmp/addr-studio.png");
if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
  console.error("usage: node scripts/studio-shot.mjs --lat LAT --lon LON [--out PATH]");
  process.exit(1);
}

const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath =
  process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const browser = await puppeteer.launch({
  executablePath: chromePath,
  headless: "new",
  args: [
    "--no-sandbox",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--window-size=1600,1000",
  ],
});

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto(`${appUrl}/?lat=${lat}&lon=${lon}&studio=1`, {
    waitUntil: "networkidle2",
    timeout: 120000,
  });
  await page.waitForFunction(
    () => document.body.textContent?.includes("Deep 3D Satellite Studio") ?? false,
    { timeout: 60000 }
  );
  // Wait for the heightfield pass to finish and the mesh to be rebuilt
  await page.waitForFunction(
    () => !(document.body.textContent ?? "").includes("Computing Heightfield"),
    { timeout: 120000 }
  );
  await new Promise((resolve) => setTimeout(resolve, 9000));

  const canvas = await page.$("canvas:not(.maplibregl-canvas)");
  if (!canvas) throw new Error("studio canvas not found");
  mkdirSync(dirname(out), { recursive: true });
  await canvas.screenshot({ path: out });
  if (errors.length > 0) console.error("page errors:", errors.slice(0, 3));
  console.log(out);
} finally {
  await browser.close();
}
