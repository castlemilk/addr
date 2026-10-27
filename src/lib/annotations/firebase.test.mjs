import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";

const transpile = path => ts.transpileModule(readFileSync(path, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const firebaseCode = transpile("src/lib/annotations/firebase.ts");
const identityCode = transpile("src/lib/annotations/google-identity.ts");
const CLIENT = "1046530555412-3gs210m74r2bosv5sj3lu111g20sliqs.apps.googleusercontent.com";
const ORIGIN = "https://fieldwork.example";
const user = (uid, email) => ({ uid, email, getIdToken: async (force = false) => `id-token-${uid}${force ? "-fresh" : ""}` });
const OFF = "fieldwork.google.signedOut";

// firebase.ts with a fake Firebase SDK, fake fetch and storage, and spies on the Google Identity hooks.
function fixture({ config = { googleClientId: CLIENT }, remembered = null, session = 200 } = {}) {
  const state = { init: [], getAuth: 0, setPersistence: 0, providers: [], credentials: [], signOuts: 0, signOutFails: false, fetches: [], hooks: [], next: null, session };
  const records = new Map(), tab = new Map();
  const storage = map => ({ getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, String(value)), removeItem: key => map.delete(key) });
  globalThis.window = { location: { origin: ORIGIN } };
  globalThis.localStorage = storage(records);
  globalThis.sessionStorage = storage(tab);
  globalThis.fetch = async (url, init) => {
    state.fetches.push({ url, authorization: init?.headers?.Authorization });
    if (url.endsWith("/api/auth/config")) return Response.json({ enabled: true, firebase: { apiKey: "k", authDomain: "fieldwork.example", projectId: "fieldwork-test", appId: "1:2:web:3" }, ...config });
    if (url.endsWith("/api/auth/session")) {
      if (state.session === "offline") throw new TypeError("Failed to fetch");
      return new Response("{}", { status: state.session, headers: { "Content-Type": "application/json" } });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  const auth = { currentUser: remembered, authStateReady: async () => {} };
  class GoogleAuthProvider {
    constructor() { this.parameters = undefined; state.providers.push(this); }
    setCustomParameters(parameters) { this.parameters = parameters; return this; }
    static credential(idToken) { return { providerId: "google.com", idToken }; }
  }
  const sdk = {
 onAuthStateChanged:(_auth,callback)=>{state.authChanged=callback;return()=>{};},
    indexedDBLocalPersistence: { name: "indexedDB" }, browserLocalPersistence: { name: "local" },
    browserSessionPersistence: { name: "session" }, browserPopupRedirectResolver: { name: "resolver" },
    initializeAuth: (app, deps) => { state.init.push(deps); return auth; },
    getAuth: () => { state.getAuth++; return auth; },
    setPersistence: async () => { state.setPersistence++; },
    GoogleAuthProvider,
    signInWithPopup: async () => { auth.currentUser = state.next; return { user: state.next }; },
    signInWithCredential: async (_, credential) => { state.credentials.push(credential); auth.currentUser = state.next; return { user: state.next }; },
    signOut: async () => { state.signOuts++; if (state.signOutFails) throw new Error("storage failed"); auth.currentUser = null; },
  };
  const identity = {};
  new Function("exports", "require", identityCode)(identity, () => { throw new Error("no imports"); });
  const exports = {};
  new Function("exports", "require", firebaseCode)(exports, id => {
    // The real hooks (they keep the automatic sign-in flag in localStorage), recorded.
    if (id === "./google-identity") return {
      ...identity,
      googleSignedOut: () => { state.hooks.push("signedOut"); identity.googleSignedOut(); },
      googleSignInRefused: () => { state.hooks.push("refused"); identity.googleSignInRefused(); },
      googleSignedIn: () => { state.hooks.push("signedIn"); identity.googleSignedIn(); },
    };
    if (id === "firebase/app") return { getApps: () => [], initializeApp: (options, name) => ({ options, name }) };
    if (id === "firebase/auth") return sdk;
    throw new Error(`unexpected import ${id}`);
  });
  return { ...exports, state, auth, records, tab };
}

test("sign-in is remembered on this device: IndexedDB first, localStorage as the fallback, never tab-only", async () => {
  const firebase = fixture();
  const connection = await firebase.firebaseConnection();
  assert.equal(firebase.state.init.length, 1);
  assert.deepEqual(firebase.state.init[0].persistence.map(entry => entry.name), ["indexedDB", "local"]);
  assert.equal(firebase.state.init[0].popupRedirectResolver.name, "resolver");
  assert.equal(firebase.state.setPersistence, 0);
  assert.equal(firebase.state.getAuth, 0);
  assert.equal(connection.googleClientId, CLIENT);
  assert.equal(await firebase.firebaseConnection(), connection, "one connection per server");
});

test("a tab-only sign-in from the previous release is handed to the remembered store once", async () => {
  const key = `firebase:authUser:k:fieldwork-${ORIGIN}`;
  const firebase = fixture();
  firebase.tab.set(key, "{\"uid\":\"tab-user\"}");
  firebase.tab.set("unrelated", "kept");
  await firebase.firebaseConnection();
  assert.equal(firebase.records.get(key), "{\"uid\":\"tab-user\"}", "Firebase moves it from localStorage into IndexedDB at start-up");
  assert.equal(firebase.tab.has(key), false);
  assert.equal(firebase.tab.get("unrelated"), "kept");
  const remembered = fixture();
  remembered.records.set(key, "{\"uid\":\"remembered\"}");
  remembered.tab.set(key, "{\"uid\":\"tab-user\"}");
  await remembered.firebaseConnection();
  assert.equal(remembered.records.get(key), "{\"uid\":\"remembered\"}", "an account already remembered wins");
  assert.equal(remembered.tab.has(key), false);
  // After an explicit sign-out (or a refused account) on this device, an old tab's copy is dropped, not adopted.
  const signedOut = fixture();
  signedOut.records.set(OFF, "1");
  signedOut.tab.set(key, "{\"uid\":\"tab-user\"}");
  await signedOut.firebaseConnection();
  assert.equal(signedOut.records.has(key), false, "the signed-out device stays signed out");
  assert.equal(signedOut.tab.has(key), false);
});

test("quick sign-in is offered only for a well-formed client ID from the server", async () => {
  for (const googleClientId of [undefined, "", "evil.example", `${CLIENT}.evil.example`]) {
    const connection = await fixture({ config: googleClientId === undefined ? {} : { googleClientId } }).firebaseConnection();
    assert.equal(connection.googleClientId, "", JSON.stringify(googleClientId));
  }
});

test("a remembered account's token goes on API calls without a new sign-in", async () => {
  const firebase = fixture({ remembered: user("reviewer", "reviewer@example.com") });
  assert.equal(await firebase.firebaseToken(""), "id-token-reviewer");
  assert.equal(firebase.state.providers.length, 0);
});

test("the popup hints the last account; Switch Google account asks for the chooser; the hint is stored after the workspace accepts", async () => {
  const firebase = fixture();
  firebase.records.set(OFF, "1");
  const connection = await firebase.firebaseConnection();
  firebase.state.next = user("one", "reviewer@example.com");
  await firebase.signInGoogle(connection);
  assert.deepEqual(firebase.state.providers[0].parameters, { prompt: "select_account" }, "no hint (first sign-in, after sign-out): Google's account chooser");
  assert.equal(firebase.records.has(OFF), false, "an accepted sign-in turns automatic sign-in back on for later visits");
  assert.deepEqual(firebase.state.hooks, ["signedIn"]);
  assert.equal(firebase.lastGoogleEmail(), "reviewer@example.com");
  assert.equal(firebase.records.get("fieldwork.google.lastEmail"), "reviewer@example.com");
  assert.deepEqual(firebase.state.fetches.at(-1), { url: `${ORIGIN}/api/auth/session`, authorization: "Bearer id-token-one" });
  await firebase.signInGoogle(connection);
  assert.deepEqual(firebase.state.providers[1].parameters, { login_hint: "reviewer@example.com" });
  await firebase.signInGoogle(connection, true);
  assert.deepEqual(firebase.state.providers[2].parameters, { prompt: "select_account" });
});

test("an account the workspace refuses is signed out again, keeps the previous hint and stops silent sign-in", async () => {
  const firebase = fixture({ session: 403 });
  firebase.records.set("fieldwork.google.lastEmail", "reviewer@example.com");
  const connection = await firebase.firebaseConnection();
  firebase.state.next = user("other", "other@example.com");
  await assert.rejects(() => firebase.signInGoogle(connection, true), /not been granted access/);
  assert.equal(firebase.state.signOuts, 1);
  assert.equal(firebase.auth.currentUser, null);
  assert.equal(firebase.lastGoogleEmail(), "reviewer@example.com");
  assert.deepEqual(firebase.state.hooks, ["refused"]);
  assert.equal(firebase.records.get(OFF), "1", "the refused account is not signed in again without a click");
  firebase.state.session = 503;
  await assert.rejects(() => firebase.signInGoogle(connection), /could not verify/);
  assert.equal(firebase.state.signOuts, 2);
  assert.deepEqual(firebase.state.hooks, ["refused"], "an outage is not a refusal");
});

test("quick sign-in turns Google's ID token into a Firebase sign-in, then runs the same workspace check", async () => {
  const firebase = fixture();
  const connection = await firebase.firebaseConnection();
  firebase.state.next = user("quick", "reviewer@example.com");
  await firebase.signInWithGoogleIdToken(connection, "google-id-token");
  assert.deepEqual(firebase.state.credentials, [{ providerId: "google.com", idToken: "google-id-token" }]);
  assert.deepEqual(firebase.state.fetches.at(-1), { url: `${ORIGIN}/api/auth/session`, authorization: "Bearer id-token-quick" });
  assert.equal(firebase.lastGoogleEmail(), "reviewer@example.com");
  firebase.state.session = 401;
  await assert.rejects(() => firebase.signInWithGoogleIdToken(connection, "google-id-token"), /not been granted access/);
  assert.equal(firebase.auth.currentUser, null);
  assert.equal(firebase.state.signOuts, 1);
});

test("explicit sign-out forgets the account, the hint and Google's automatic sign-in, kept across page loads", async () => {
  const firebase = fixture({ remembered: user("reviewer", "reviewer@example.com") });
  firebase.records.set("fieldwork.google.lastEmail", "reviewer@example.com");
  await firebase.signOutGoogle(await firebase.firebaseConnection());
  assert.equal(firebase.auth.currentUser, null);
  assert.equal(firebase.records.has("fieldwork.google.lastEmail"), false);
  assert.equal(firebase.lastGoogleEmail(), "");
  assert.deepEqual(firebase.state.hooks, ["signedOut"]);
  assert.equal(firebase.records.get(OFF), "1", "stored on the device, not only in this page");
  // Firebase's sign-out fails: the hint and the automatic sign-in are already off.
  const failing = fixture({ remembered: user("reviewer", "reviewer@example.com") });
  failing.records.set("fieldwork.google.lastEmail", "reviewer@example.com");
  failing.state.signOutFails = true;
  const failingConnection = await failing.firebaseConnection();
  await assert.rejects(() => failing.signOutGoogle(failingConnection), /storage failed/);
  assert.equal(failing.lastGoogleEmail(), "");
  assert.equal(failing.records.get(OFF), "1");
  assert.deepEqual(failing.state.hooks, ["signedOut"]);
});

test("a refused saved sign-in is looked at once more, then signed out once, with a reason, only while still signed in", async () => {
  const firebase = fixture({ remembered: user("reviewer", "reviewer@example.com"), session: 401 });
  firebase.records.set("fieldwork.google.lastEmail", "reviewer@example.com");
  const token = await firebase.firebaseToken("");
  assert.equal(await firebase.recheckRefusedSignIn("", "never-issued"), "");
  // Two requests failed with the same token: one second look, one sign-out.
  const [first, second] = await Promise.all([firebase.recheckRefusedSignIn("", token), firebase.recheckRefusedSignIn("", token)]);
  assert.deepEqual([first, second], ["", ""]);
  assert.equal(await firebase.recheckRefusedSignIn("", token), "", "a later failed request does not sign out again");
  assert.deepEqual(firebase.state.fetches.filter(entry => entry.url.endsWith("/api/auth/session")),
    [{ url: `${ORIGIN}/api/auth/session`, authorization: "Bearer id-token-reviewer-fresh" }], "the second look uses a refreshed token");
  assert.equal(firebase.state.signOuts, 1);
  assert.equal(firebase.auth.currentUser, null);
  assert.match(firebase.signInNotice(), /did not accept the saved sign-in for reviewer@example\.com/);
  assert.deepEqual(firebase.state.hooks, ["refused"]);
  assert.equal(firebase.lastGoogleEmail(), "", "the refused account is not offered as Continue as …");
  assert.equal(firebase.records.get(OFF), "1");
  // Another account signed in meanwhile: an old token's refusal leaves it alone.
  firebase.auth.currentUser = user("first", "first@example.com");
  const old = await firebase.firebaseToken("");
  firebase.auth.currentUser = user("second", "second@example.com");
  assert.equal(await firebase.recheckRefusedSignIn("", old), "");
  assert.equal(firebase.auth.currentUser.uid, "second");
  assert.equal(firebase.state.signOuts, 1);
  // The next successful sign-in clears the reason.
  firebase.state.session = 200;
  firebase.state.next = user("second", "second@example.com");
  await firebase.signInGoogle(await firebase.firebaseConnection());
  assert.equal(firebase.signInNotice(), "");
});

test("a passing refusal keeps the remembered sign-in: accepted on the second look, or the look itself fails", async () => {
  const firebase = fixture({ remembered: user("reviewer", "reviewer@example.com") });
  firebase.records.set("fieldwork.google.lastEmail", "reviewer@example.com");
  // Accepted on the second look: the fresh token comes back for one retry.
  const fresh = await firebase.recheckRefusedSignIn("", await firebase.firebaseToken(""));
  assert.equal(fresh, "id-token-reviewer-fresh");
  // Offline, or the Worker could not check (5xx): nothing is signed out.
  for (const session of ["offline", 503, 500]) {
    firebase.state.session = session;
    firebase.auth.currentUser = user(`reviewer-${session}`, "reviewer@example.com");
    assert.equal(await firebase.recheckRefusedSignIn("", await firebase.firebaseToken("")), "", String(session));
  }
  assert.equal(firebase.state.signOuts, 0);
  assert.equal(firebase.signInNotice(), "");
  assert.equal(firebase.lastGoogleEmail(), "reviewer@example.com");
  assert.equal(firebase.records.has(OFF), false);
  assert.deepEqual(firebase.state.hooks, []);
  // The refreshed token is known too: if it is refused later, it gets its own second look.
  firebase.state.session = 401;
  firebase.auth.currentUser = user("reviewer", "reviewer@example.com");
  assert.equal(await firebase.recheckRefusedSignIn("", fresh), "");
  assert.equal(firebase.state.signOuts, 1);
});

test("a stored hint that is not an email address is ignored", () => {
  const firebase = fixture();
  for (const value of ["", "no-at-sign", "two words@example.com", `${"x".repeat(65)}@example.com`]) {
    firebase.records.set("fieldwork.google.lastEmail", value);
    assert.equal(firebase.lastGoogleEmail(), "", value);
  }
  globalThis.localStorage = { getItem() { throw new Error("blocked"); } };
  assert.equal(firebase.lastGoogleEmail(), "");
});
