import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";

const code = ts.transpileModule(readFileSync("src/lib/annotations/google-identity.ts", "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const CLIENT = "1046530555412-3gs210m74r2bosv5sj3lu111g20sliqs.apps.googleusercontent.com";
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

// A fresh module (its once-per-page state) with a fake document and an optional Google library that "loads" on demand.
// `device` is the localStorage shared by page loads in the same browser; Chrome's silent-access flag is recorded in it too.
function page(device = new Map()) {
  const scripts = [], calls = [];
  globalThis.localStorage = { getItem: key => device.get(key) ?? null, setItem: (key, value) => device.set(key, String(value)), removeItem: key => device.delete(key) };
  Object.defineProperty(globalThis, "navigator", { configurable: true, writable: true, value: {
    credentials: { preventSilentAccess: async () => { device.set("chrome.preventSilentAccess", (device.get("chrome.preventSilentAccess") ?? 0) + 1); } },
  } });
  const gis = {
    initialize: config => calls.push(["initialize", config]), prompt: (...args) => calls.push(["prompt", args.length]),
    cancel: () => calls.push(["cancel"]), disableAutoSelect: () => calls.push(["disableAutoSelect"]),
  };
  globalThis.window = {};
  globalThis.document = {
    createElement: tag => ({ tag, remove() { this.removed = true; } }),
    head: { appendChild: script => scripts.push(script) },
  };
  const exports = {};
  new Function("exports", "require", code)(exports, id => { throw new Error(`unexpected import ${id}`); });
  const load = () => { window.google = { accounts: { id: gis } }; scripts.at(-1).onload(); };
  return { ...exports, scripts, calls, load, device, names: () => calls.map(([name]) => name) };
}

test("client IDs must be well-formed OAuth web client IDs", () => {
  const { googleClientId } = page();
  assert.equal(googleClientId(CLIENT), CLIENT);
  for (const bad of [undefined, null, 42, "", ` ${CLIENT}`, `${CLIENT}\n`, CLIENT.toUpperCase(), `${CLIENT}.evil.example`, "abc-def.apps.googleusercontent.com", `1-${"a".repeat(130)}.apps.googleusercontent.com`])
    assert.equal(googleClientId(bad), "", JSON.stringify(bad));
});

test("no client ID: the Google library is never requested", async () => {
  const identity = page();
  identity.offerQuickSignIn("", () => {})();
  identity.offerQuickSignIn("not-a-client", () => {})();
  await settle();
  assert.deepEqual(identity.scripts, []);
});

test("the library loads lazily, initialises with FedCM and auto-select, and prompts once per page load", async () => {
  const identity = page(), tokens = [];
  const stop = identity.offerQuickSignIn(CLIENT, token => tokens.push(token));
  assert.equal(identity.scripts.length, 1);
  assert.equal(identity.scripts[0].src, "https://accounts.google.com/gsi/client");
  identity.load(); await settle();
  const [[name, config]] = identity.calls;
  assert.equal(name, "initialize");
  assert.deepEqual({ ...config, callback: typeof config.callback }, {
    client_id: CLIENT, callback: "function", auto_select: true, use_fedcm_for_prompt: true, itp_support: true, context: "signin", cancel_on_tap_outside: false,
  });
  assert.deepEqual(identity.calls.slice(1), [["prompt", 0]], "prompt() takes no status callback");
  config.callback({ credential: "google-id-token" });
  config.callback({}); config.callback({ credential: 7 });
  assert.deepEqual(tokens, ["google-id-token"]);
  stop();
  assert.equal(identity.names().at(-1), "cancel", "leaving the login screen closes the prompt");
  config.callback({ credential: "late-token" });
  assert.deepEqual(tokens, ["google-id-token"], "a detached login screen receives nothing");
  // The login screen shows again (e.g. after a refused sign-in): the new screen gets credentials, but no second prompt.
  const again = [];
  identity.offerQuickSignIn(CLIENT, token => again.push(token));
  await settle();
  assert.equal(identity.names().filter(name => name === "prompt").length, 1);
  assert.equal(identity.names().filter(name => name === "initialize").length, 1);
  assert.equal(identity.scripts.length, 1);
  config.callback({ credential: "second" });
  assert.deepEqual(again, ["second"]);
});

test("a library that cannot load (blocked, offline) changes nothing and is not retried", async () => {
  const identity = page();
  identity.offerQuickSignIn(CLIENT, () => {});
  identity.scripts[0].onerror();
  assert.equal(await identity.loadGoogleIdentity(), null);
  assert.equal(identity.scripts[0].removed, true);
  identity.offerQuickSignIn(CLIENT, () => {})();
  await settle();
  assert.equal(identity.scripts.length, 1);
  assert.deepEqual(identity.calls, []);
});

test("a library that throws (refused origin) is contained", async () => {
  const identity = page();
  identity.offerQuickSignIn(CLIENT, () => {});
  window.google = { accounts: { id: { initialize() { throw new Error("The given origin is not allowed for the given client ID."); } } } };
  identity.scripts[0].onload();
  await settle(); // no unhandled rejection, nothing thrown to the page
});

test("explicit sign-out before the library loaded: it loads on the login screen to disable auto-select, without prompting", async () => {
  const identity = page();
  identity.googleSignedOut();
  identity.offerQuickSignIn(CLIENT, () => {});
  identity.load(); await settle();
  assert.deepEqual(identity.names(), ["initialize", "disableAutoSelect"]);
  assert.equal(identity.calls[0][1].auto_select, false);
  identity.offerQuickSignIn(CLIENT, () => {});
  await settle();
  assert.deepEqual(identity.names(), ["initialize", "disableAutoSelect"], "no prompt for the rest of this page");
});

test("explicit sign-out after the library loaded disables auto-select at once", async () => {
  const identity = page();
  identity.offerQuickSignIn(CLIENT, () => {});
  identity.load(); await settle();
  identity.googleSignedOut();
  assert.deepEqual(identity.names(), ["initialize", "prompt", "disableAutoSelect"]);
});

test("a refused saved sign-in disables auto-select but still offers the account chooser once", async () => {
  const identity = page();
  identity.googleSignInRefused();
  identity.offerQuickSignIn(CLIENT, () => {});
  identity.load(); await settle();
  assert.deepEqual(identity.names(), ["initialize", "disableAutoSelect", "prompt"]);
  assert.equal(identity.calls[0][1].auto_select, false);
});

test("explicit sign-out keeps automatic sign-in off on later page loads, until the next accepted sign-in", async () => {
  // Page 1: the reviewer signs out where the login screen never loads Google's library (unsaved draft, tab closed, load failed).
  const first = page();
  first.googleSignedOut();
  assert.deepEqual(first.scripts, []);
  assert.equal(first.device.get("fieldwork.google.signedOut"), "1");
  assert.equal(first.device.get("chrome.preventSilentAccess"), 1, "Chrome's own flag is set without the library");
  assert.equal(first.googleAutoSignInOff(), true);
  // Page 2, same browser: the login screen may offer the account, but only with a click.
  const second = page(first.device);
  second.offerQuickSignIn(CLIENT, () => {});
  second.load(); await settle();
  assert.deepEqual(second.names(), ["initialize", "prompt"]);
  assert.equal(second.calls[0][1].auto_select, false);
  // An accepted sign-in turns it back on for the next page load.
  second.googleSignedIn();
  assert.equal(first.device.has("fieldwork.google.signedOut"), false);
  const third = page(first.device);
  third.offerQuickSignIn(CLIENT, () => {});
  third.load(); await settle();
  assert.equal(third.calls[0][1].auto_select, true);
});

test("a refused account keeps automatic sign-in off on later page loads too", async () => {
  const first = page();
  first.googleSignInRefused();
  const second = page(first.device);
  second.offerQuickSignIn(CLIENT, () => {});
  second.load(); await settle();
  assert.equal(second.calls[0][1].auto_select, false);
});

test("blocked storage or a browser without the Credential Management API changes nothing visible", async () => {
  const identity = page();
  globalThis.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };
  Object.defineProperty(globalThis, "navigator", { configurable: true, writable: true, value: {} });
  identity.googleSignedOut();
  identity.googleSignedIn();
  assert.equal(identity.googleAutoSignInOff(), false);
  identity.offerQuickSignIn(CLIENT, () => {});
  identity.load(); await settle();
  assert.deepEqual(identity.names(), ["initialize", "disableAutoSelect"], "this page still keeps Google's automatic sign-in off");
});
