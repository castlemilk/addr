import puppeteer from "puppeteer-core";
import { mkdirSync } from "node:fs";

const url = process.env.APP_URL || "http://localhost:3008/reviews";
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
    "--window-size=1600,1000",
  ],
});

const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });

const errors = [];
page.on("pageerror", (error) => errors.push(`[pageerror] ${error.message}`));
page.on("console", (message) => {
  if (message.type() === "error") errors.push(`[console] ${message.text()}`);
});

console.log("Navigating to", url);
const response = await page.goto(url, { waitUntil: "networkidle2", timeout: 60000 });
console.log("Response status:", response.status());
await new Promise((resolve) => setTimeout(resolve, 3000));

await page.waitForFunction(
  () => document.body.textContent?.includes("Roof review") ?? false,
  { timeout: 15000 }
);

// Validate Image and SVG Alignment
const alignmentReport = await page.evaluate(() => {
  const img = document.querySelector("img[alt='Aerial orthophoto site view']");
  const svg = document.querySelector("svg[viewBox='0 0 512 512']");
  const boundaryPoly = svg?.querySelector("polygon[stroke='#06b6d4']") || svg?.querySelector("polygon[stroke='#e11d48']");
  const facePolys = svg?.querySelectorAll("polygon[fill^='rgba']");
  const lines = svg?.querySelectorAll("line[stroke]:not([stroke='transparent'])");

  if (!img) return { error: "Site image not found" };
  if (!svg) return { error: "SVG overlay with viewBox 0 0 512 512 not found" };
  if (!boundaryPoly) return { error: "Boundary polygon not found in SVG" };

  const pointsStr = boundaryPoly.getAttribute("points") || "";
  const points = pointsStr.trim().split(/\s+/).map((pair) => pair.split(",").map(Number));

  if (points.length < 3) return { error: "Boundary polygon has fewer than 3 points" };

  let sumX = 0, sumY = 0;
  for (const [x, y] of points) {
    sumX += x;
    sumY += y;
  }
  const centroidX = sumX / points.length;
  const centroidY = sumY / points.length;

  // Validate Accuracy Signals in DOM
  const signalsText = document.body.textContent || "";
  const hasGrade = signalsText.includes("Image checks:");
  const hasSignalsBar = signalsText.includes("IoU") && signalsText.includes("Purity") && signalsText.includes("Coverage");

  return {
    imageSrc: img.src,
    imageNaturalWidth: img.naturalWidth,
    imageNaturalHeight: img.naturalHeight,
    viewBox: svg.getAttribute("viewBox"),
    centroidX: Math.round(centroidX * 10) / 10,
    centroidY: Math.round(centroidY * 10) / 10,
    distanceFromCenter: Math.round(Math.hypot(centroidX - 256, centroidY - 256) * 10) / 10,
    faceCount: facePolys ? facePolys.length : 0,
    rooflineCount: lines ? lines.length : 0,
    hasGrade,
    hasSignalsBar,
  };
});

console.log("Alignment report:", JSON.stringify(alignmentReport, null, 2));

if (alignmentReport.error) {
  console.error("ALIGNMENT ERROR:", alignmentReport.error);
  process.exit(1);
}

if (alignmentReport.distanceFromCenter > 50) {
  console.error(`WARNING: Centroid is too far from center (${alignmentReport.distanceFromCenter}px > 50px)`);
  process.exit(1);
}

if (!alignmentReport.hasSignalsBar) {
  console.error("ACCURACY SIGNALS BAR missing from UI");
  process.exit(1);
}

await page.screenshot({ path: `${outDir}/reviews-studio-fixed.png` });
console.log("Saved full screenshot to", `${outDir}/reviews-studio-fixed.png`);

// Crop just the canvas area to visually inspect the alignment
const canvasElem = await page.$("div.relative.transition-transform");
if (canvasElem) {
  await canvasElem.screenshot({ path: `${outDir}/canvas-overlay-closeup.png` });
  console.log("Saved canvas closeup to", `${outDir}/canvas-overlay-closeup.png`);
}

// Toggle Evidence Mask layer
const maskButton = await page.$("button::-p-text(Image mask)");
if (maskButton) {
  await maskButton.click();
  await new Promise((resolve) => setTimeout(resolve, 600));
  if (canvasElem) {
    await canvasElem.screenshot({ path: `${outDir}/canvas-mask-closeup.png` });
    console.log("Saved canvas mask closeup to", `${outDir}/canvas-mask-closeup.png`);
  }
}

await browser.close();

if (errors.length > 0) {
  console.error("Console errors encountered:", errors);
  process.exitCode = 1;
} else {
  console.log("OK: Alignment & accuracy signals validated successfully with 0 errors!");
}
