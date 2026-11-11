// Screenshots the map (3D roof meshes) centred on a coordinate.
//
//   node scripts/map-shot.mjs --lat -37.79044 --lon 145.31252 [--zoom 19] [--pitch 0] [--out /tmp/map.png]
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
const zoom = Number.parseFloat(value("--zoom", "19"));
const pitch = Number.parseFloat(value("--pitch", "0"));
const out = value("--out", "/tmp/addr-map.png");
if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
  console.error("usage: node scripts/map-shot.mjs --lat LAT --lon LON [--out PATH]");
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

  await page.goto(`${appUrl}/?lat=${lat}&lon=${lon}&zoom=${zoom}&pitch=${Math.max(pitch, 0)}`, {
    waitUntil: "networkidle2",
    timeout: 120000,
  });
  await new Promise((resolve) => setTimeout(resolve, 12000));
  // Deterministic camera via the tooling hook; falls back to wheel zoom.
  const jumped = await page.evaluate(
    ({ lat, lon, zoom, pitch }) => {
      const map = window.__addrMap;
      if (!map) return false;
      map.jumpTo({ center: [lon, lat], zoom, pitch, bearing: 0 });
      return true;
    },
    { lat, lon, zoom, pitch }
  );
  if (!jumped) {
    await page.mouse.move(800, 500);
    for (let i = 0; i < 3; i++) {
      await page.mouse.wheel({ deltaY: -600 });
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
  }
  await new Promise((resolve) => setTimeout(resolve, 8000));
  mkdirSync(dirname(out), { recursive: true });
  await page.screenshot({ path: out });
  if (errors.length > 0) console.error("page errors:", errors.slice(0, 3));
  console.log(out);
} finally {
  await browser.close();
}
