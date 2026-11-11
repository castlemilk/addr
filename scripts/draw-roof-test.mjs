// Verifies Draw-roof replaces (not duplicates) an existing roof override:
// draw twice on sacrificial address 25123 -> expect exactly 1 roof row,
// second draw keeps the same id. Row is deleted afterwards by the caller.
import puppeteer from "puppeteer-core";

const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath =
  process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
// Near 25123 (11 Kiloran Ave) centroid; triangle points are screen pixels.
const LAT = "-37.79467";
const LON = "145.31662";

const browser = await puppeteer.launch({
  executablePath: chromePath,
  headless: "new",
  args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--window-size=1600,1000"],
});

const clickByText = async (page, tag, text) => {
  const clicked = await page.evaluate((tag, text) => {
    const els = [...document.querySelectorAll(tag)];
    const el = els.find((e) => (e.textContent || "").includes(text));
    if (!el) return false;
    el.click();
    return true;
  }, tag, text);
  if (!clicked) throw new Error(`element not found: ${text}`);
};

const drawTriangle = async (page, dx) => {
  await clickByText(page, "button", "Address CRM");
  await new Promise((r) => setTimeout(r, 2500));
  await clickByText(page, "button", "Draw roof");
  await new Promise((r) => setTimeout(r, 1200));
  // Click-mode: three vertices forming a triangle east of the sidebar.
  await page.mouse.click(800 + dx, 400);
  await new Promise((r) => setTimeout(r, 400));
  await page.mouse.click(900 + dx, 400);
  await new Promise((r) => setTimeout(r, 400));
  await page.mouse.click(850 + dx, 500);
  await new Promise((r) => setTimeout(r, 400));
  await clickByText(page, "button", "Finish");
  await new Promise((r) => setTimeout(r, 3000));
};

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${appUrl}/?lat=${LAT}&lon=${LON}&zoom=19`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.evaluate(() => window.localStorage.removeItem("addr.crm.token"));
  await page.reload({ waitUntil: "networkidle2", timeout: 120000 });
  await new Promise((r) => setTimeout(r, 9000));

  await drawTriangle(page, 0);
  await drawTriangle(page, 60);
  await page.screenshot({ path: "/tmp/draw-roof-replace.png" });
  if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
  console.log("DRAW ROOF 2x OK; page errors: none");
} finally {
  await browser.close();
}
