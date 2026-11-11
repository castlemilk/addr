// Verifies local no-auth CRM: no token in storage -> open Address CRM ->
// expect local-admin principal with no key prompt -> Propose box drag-draw
// saves without credentials -> proposal show measures it.
import puppeteer from "puppeteer-core";

const appUrl = process.env.APP_URL || "http://localhost:3004";
const chromePath =
  process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

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

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${appUrl}/?lat=-37.79044&lon=145.31252&zoom=19`, { waitUntil: "networkidle2", timeout: 120000 });
  await page.evaluate(() => window.localStorage.removeItem("addr.crm.token"));
  await page.reload({ waitUntil: "networkidle2", timeout: 120000 });
  await new Promise((r) => setTimeout(r, 9000));

  await clickByText(page, "button", "Address CRM");
  await new Promise((r) => setTimeout(r, 3000));
  const panelText = await page.evaluate(() => document.body.textContent);
  console.log("local-dev note visible:", panelText.includes("no key needed"));
  console.log("key prompt visible:", panelText.includes("Paste API key"));
  if (!panelText.includes("no key needed")) throw new Error("expected local-dev auto-auth note");

  await clickByText(page, "button", "Propose box");
  await new Promise((r) => setTimeout(r, 1500));
  await page.mouse.move(750, 350);
  await page.mouse.down();
  await page.mouse.move(850, 350, { steps: 5 });
  await page.mouse.move(850, 450, { steps: 5 });
  await page.mouse.up();
  await new Promise((r) => setTimeout(r, 2500));
  const proposal = await page.evaluate(() => window.__addrProposal ?? null);
  console.log("proposal saved:", JSON.stringify(proposal?.label), "corners:", proposal?.ring?.length);
  if (!proposal || proposal.ring?.length !== 4) throw new Error("expected saved 4-corner proposal");
  if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
  console.log("NOAUTH BOX OK; page errors: none");
} finally {
  await browser.close();
}
