// Opens the CRM panel for an address, enters boundary edit mode, and
// screenshots the vertex handles + toolbar.
//
//   node scripts/crm-edit-shot.mjs --lat -37.79044 --lon 145.31252 --out /tmp/crm-edit.png
//
// Requires the web app (task start), a CRM editor token in CRM_TOKEN, and
// system Chrome. APP_URL/CHROME_PATH override the defaults.
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
const out = value("--out", "/tmp/crm-edit.png");
const token = process.env.CRM_TOKEN || "";
if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
  console.error("usage: node scripts/crm-edit-shot.mjs --lat LAT --lon LON [--out PATH]");
  process.exit(1);
}
if (!token) {
  console.error("CRM_TOKEN env var (editor key) is required");
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
  await page.evaluateOnNewDocument((key) => {
    window.localStorage.setItem("addr.crm.token", key);
  }, token);

  await page.goto(`${appUrl}/?lat=${lat}&lon=${lon}`, {
    waitUntil: "networkidle2",
    timeout: 120000,
  });
  await new Promise((resolve) => setTimeout(resolve, 6000));

  const clickedCrm = await page.evaluate(() => {
    const button = [...document.querySelectorAll("button")].find((element) =>
      element.textContent?.includes("Address CRM")
    );
    if (!button) return false;
    button.click();
    return true;
  });
  if (!clickedCrm) throw new Error("Address CRM button not found");
  await new Promise((resolve) => setTimeout(resolve, 4000));

  const clickedEdit = await page.evaluate(() => {
    const button = document.querySelector('button[title="Edit boundary vertices"]');
    if (!button) return false;
    button.click();
    return true;
  });
  if (!clickedEdit) throw new Error("edit button not found (is the CRM panel open with features?)");
  await new Promise((resolve) => setTimeout(resolve, 4000));

  const editing = await page.evaluate(() =>
    (document.body.textContent ?? "").includes("Editing boundary")
  );

  let dragged = false;
  if (args.includes("--drag")) {
    const handle = await page.$('[data-crm-vertex="0"]');
    if (handle) {
      const box = await handle.boundingBox();
      if (box) {
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width / 2 + 26, box.y + box.height / 2 + 18, {
          steps: 12,
        });
        await page.mouse.up();
        dragged = true;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const saved = await page.evaluate(() => {
      const button = [...document.querySelectorAll("button")].find((element) =>
        element.textContent?.includes("Save edit")
      );
      if (!button) return false;
      button.click();
      return true;
    });
    await new Promise((resolve) => setTimeout(resolve, 4000));
    console.log(JSON.stringify({ saved }));
  }

  mkdirSync(dirname(out), { recursive: true });
  await page.screenshot({ path: out });
  console.log(JSON.stringify({ out, editing, dragged }));
} finally {
  await browser.close();
}
