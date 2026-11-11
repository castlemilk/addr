// Studio 3D eyes: opens the Deep 3D Studio for a site, waits for the model
// pass to finish, screenshots the studio viewport, and writes mesh stats
// parsed from the app's own [Deep3D] console line plus any page errors.
//
// NOTE: the shot is clipped to the WebGL canvas box, but the studio's UI
// panels overlap that region, so screenshots include chrome. Pixel analysis
// downstream treats UI as background-ish noise; the deterministic empty-mesh
// signal is the tri counts, not the pixels.
//
//   node scripts/studio-check.mjs --lat LAT --lon LON --out /tmp/shot.png [--stats /tmp/shot.json]
//
// Requires the web app (task start) and system Chrome. APP_URL/CHROME_PATH env
// vars override the defaults.
import puppeteer from "puppeteer-core";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const args = process.argv.slice(2);
const value = (flag, fallback = null) => {
  const index = args.indexOf(flag);
  return index >= 0 && index + 1 < args.length ? args[index + 1] : fallback;
};

const lat = Number.parseFloat(value("--lat", "NaN"));
const lon = Number.parseFloat(value("--lon", "NaN"));
const out = value("--out", "/tmp/addr-studio-check.png");
const statsOut = value("--stats", null);
if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
  console.error("usage: node scripts/studio-check.mjs --lat LAT --lon LON [--out PATH] [--stats PATH]");
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
  let meshLine = null;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    const text = message.text();
    if (text.includes("[Deep3D] roof mesh:")) meshLine = text;
    if (message.type() === "error") errors.push(text);
  });

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
  const wallTris = meshLine ? Number(/(\d+) wall tris/.exec(meshLine)?.[1] ?? 0) : 0;
  const roofTris = meshLine ? Number(/(\d+) roof tris/.exec(meshLine)?.[1] ?? 0) : 0;
  const stats = { wallTris, roofTris, meshLine, errors: errors.slice(0, 5), shot: out };
  if (statsOut) {
    mkdirSync(dirname(statsOut), { recursive: true });
    writeFileSync(statsOut, JSON.stringify(stats, null, 1));
  }
  if (errors.length > 0) console.error("page errors:", errors.slice(0, 3));
  console.log(JSON.stringify(stats));
} finally {
  await browser.close();
}
