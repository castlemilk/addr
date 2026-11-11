// Isolated browser verification. Serves build output and synthetic API responses only.
// No production project, credential, Google account or CRM record is accessed.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { resolve, extname } from "node:path";
import puppeteer from "puppeteer-core";

const root = resolve("apps/annotation-web/dist");
const token = "isolated-ui-fixture-token";
let enabled = false, unavailable = false;
const server = createServer(async (req, res) => {
  const path = new URL(req.url, "http://fixture.invalid").pathname;
  function json(status, data) { res.writeHead(status, {"Content-Type":"application/json"}); res.end(JSON.stringify(data)); }
  if (path === "/api/auth/config") return json(200, enabled ? {enabled:true,firebase:{
    apiKey:"fake-browser-key-for-isolated-test",appId:"1:123:web:fixture",projectId:"fieldwork-fixture",authDomain:"fieldwork-fixture.firebaseapp.com",
  }} : {enabled:false});
  if (path.startsWith("/annotation.v1.AnnotationService/")) {
    if (unavailable) return json(503,{code:"unavailable",message:"Fixture temporarily unavailable"});
    if (req.headers.authorization !== `Bearer ${token}`) return json(401,{code:"unauthenticated",message:"Fixture requires sign-in"});
    return json(200,{data:path.endsWith("GetInfo") ? {adapters:[],authRequired:true,storage:"fixture"} : {projects:[]}});
  }
  try {
    const file = resolve(root, `.${path === "/" ? "/index.html" : path}`);
    if (!file.startsWith(`${root}/`)) return json(404,{});
    const bytes = await readFile(file);
    res.writeHead(200,{"Content-Type":({".html":"text/html",".js":"text/javascript",".css":"text/css",".svg":"image/svg+xml",".woff2":"font/woff2"})[extname(file)] || "application/octet-stream"});
    res.end(bytes);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0,"127.0.0.1",resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
if (process.env.FIELDWORK_LOGIN_PREVIEW === "1") {
  enabled = true;
  console.log(`Isolated login preview: ${origin}`);
  await new Promise(()=>{}); // The HTTP server keeps this optional visual-review mode alive.
}
let browser;
try {
  browser = await puppeteer.launch({executablePath:process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",headless:true,args:["--no-sandbox"]});
  for (const configured of [false,true]) {
    enabled = configured;
    const context = await browser.createBrowserContext(), page = await context.newPage();
    const errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    // The browser fixture must never contact Google or a hosted CRM.
    await page.setRequestInterception(true);
    page.on("request",request=>request.url().startsWith(origin) || request.url().startsWith("data:") ? request.continue() : request.abort());
    await page.setViewport({width:1280,height:900});
    await page.goto(origin);
    await page.waitForFunction(()=>document.body.innerText.includes("Sign in to Fieldwork"));
    await page.waitForFunction(configured=>document.body.innerText.includes(configured ? "Continue with Google" : "Google sign-in is not configured"),{},configured);
    assert.equal(await page.$(".fw-queue"),null,"Unauthenticated users see a dedicated login, not an empty editor");
    if (configured) {
      assert.equal(await page.$eval(".fw-login-google",button=>button.disabled),false);
      await mkdir(".run",{recursive:true});
      await page.screenshot({path:".run/fieldwork-login-desktop.png"});
      await page.click('.fw-login-motion');
      await page.waitForSelector('button[aria-label="Play ASCII animation"]');
      const paused = await page.$eval("canvas",canvas=>canvas.toDataURL());
      await new Promise(resolve=>setTimeout(resolve,200));
      assert.ok(await page.$eval("canvas",canvas=>canvas.toDataURL()) === paused,"Paused ASCII stays still");
      await page.click('.fw-login-motion');
      await page.waitForFunction(before=>document.querySelector("canvas").toDataURL() !== before,{timeout:5000},paused);
      await page.emulateMediaFeatures([{name:"prefers-reduced-motion",value:"reduce"}]);
      await page.waitForSelector('button[aria-label="Animation paused for reduced motion"]:disabled');
      const reduced = await page.$eval("canvas",canvas=>canvas.toDataURL());
      await new Promise(resolve=>setTimeout(resolve,200));
      assert.ok(await page.$eval("canvas",canvas=>canvas.toDataURL()) === reduced,"Reduced motion stops ASCII rendering");
      await page.setViewport({width:375,height:812});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true,"Mobile login has no horizontal overflow");
      await page.screenshot({path:".run/fieldwork-login-mobile.png",fullPage:true});
      await page.setViewport({width:1280,height:900});
    }
    await page.click(".fw-login-settings");
    await page.waitForSelector(".fw-dialog[open]");
    await page.click(".fw-token-connection summary");
    await page.type('input[name="token"]',token);
    await page.click('.fw-token-connection button:not([type="button"])');
    await page.waitForFunction(()=>document.body.innerText.includes("Create a project") && !document.body.innerText.includes("Sign in to Fieldwork"));
    await page.reload();
    await page.waitForFunction(()=>document.body.innerText.includes("Create a project") && !document.body.innerText.includes("Sign in to Fieldwork"));
    assert.deepEqual(errors,[]);
    console.log(`PASS ${configured ? "configured Google button" : "unconfigured fallback"}, workspace token login and reload`);
    await context.close();
  }
  const context = await browser.createBrowserContext(), page = await context.newPage();
  unavailable = true; enabled = false;
  await page.goto(origin);
  await page.waitForFunction(()=>document.body.innerText.includes("The workspace could not be reached"));
  unavailable = false;
  await page.click(".fw-login-unavailable button");
  await page.waitForFunction(()=>document.body.innerText.includes("Google sign-in is not configured"));
  console.log("PASS connection outage and retry, native ASCII pause/resume, reduced motion and mobile layout");
  await context.close();
} finally { await browser?.close(); await new Promise(resolve=>server.close(resolve)); }
