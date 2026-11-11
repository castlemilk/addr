import type { Auth, User } from "firebase/auth";
import { googleAutoSignInOff, googleClientId, googleSignedIn, googleSignedOut, googleSignInRefused } from "./google-identity";

// googleClientId: the OAuth web client for quick sign-in with the browser's Google account ("" when not offered).
type FirebaseConnection = { auth: Auth; sdk: typeof import("firebase/auth"); origin: string; googleClientId: string; sharedWorkspaces:boolean };
const connections = new Map<string, Promise<FirebaseConnection | null>>();
const lastEmailKey = "fieldwork.google.lastEmail";
// Google ID tokens this page sent, with the account that issued them (a few recent ones).
const issued = new Map<string, string>();
// Second looks at refused tokens, shared by every request that failed with the same token.
const rechecks = new Map<string, Promise<string>>();
let notice = "";

export function firebaseConnection(server = ""): Promise<FirebaseConnection | null> {
  const origin = new URL(server || window.location.origin).origin;
  const existing = connections.get(origin);
  if (existing) return existing;
  const loading = (async () => {
    const response = await fetch(`${origin}/api/auth/config`, {cache:"no-store",redirect:"error"});
    if (response.status === 404) return null; // Standalone local token-only server.
    if (!response.ok) throw new Error("Sign-in settings could not be loaded. Retry the connection.");
    if (!response.headers.get("Content-Type")?.includes("application/json")) return null;
    const config = await response.json();
    if (!config.enabled) return null;
    const [appSdk, sdk] = await Promise.all([import("firebase/app"), import("firebase/auth")]);
    const name = `fieldwork-${origin}`;
    if (typeof config.firebase?.apiKey === "string") adoptTabOnlySignIn(`firebase:authUser:${config.firebase.apiKey}:${name}`);
    const app = appSdk.getApps().find(app=>app.name === name) || appSdk.initializeApp(config.firebase,name);
    // Remembered on this device until sign-out: survives reloads, new tabs and browser restarts, shared by every tab.
    // IndexedDB first, localStorage when IndexedDB is unavailable. Same options on every call, so a retry reuses this Auth.
    const auth = sdk.initializeAuth(app,{
      persistence:[sdk.indexedDBLocalPersistence,sdk.browserLocalPersistence],
      popupRedirectResolver:sdk.browserPopupRedirectResolver,
    });
    await auth.authStateReady();
    let previousUID=auth.currentUser?.uid??"";
    // A different identity must never inherit mounted editors, queued saves or
    // recovered drafts. Reload also aborts in-flight requests from the old UI.
    sdk.onAuthStateChanged(auth,user=>{
      const next=user?.uid??"";
      if(previousUID && next!==previousUID){window.location.reload();}
      previousUID=next;
    });
    return { auth, sdk, origin, googleClientId: googleClientId(config.googleClientId),sharedWorkspaces:config.sharedWorkspaces===true };
  })();
  connections.set(origin,loading);
  loading.catch(()=>connections.delete(origin));
  return loading;
}

// Until 9 Oct 2026 a sign-in was kept for the tab only (sessionStorage). Hand such a sign-in to the remembered store once:
// Firebase finds it in localStorage at start-up and moves it into IndexedDB. A sign-in already remembered (IndexedDB is
// read first) wins. After an explicit sign-out or a refused account on this device, an old tab's copy is dropped instead,
// so reloading that tab cannot sign the device back in.
function adoptTabOnlySignIn(key: string) {
  try {
    const saved = sessionStorage.getItem(key);
    if (!saved) return;
    if (!googleAutoSignInOff() && !localStorage.getItem(key)) localStorage.setItem(key,saved);
    sessionStorage.removeItem(key);
  } catch { /* Storage blocked: the account signs in again. */ }
}

export async function firebaseToken(server: string): Promise<string> {
  const connection = await firebaseConnection(server);
  const user = connection?.auth.currentUser;
  if (!user) return "";
  const token = await user.getIdToken();
  if (connection.auth.currentUser !== user) throw new Error("Your signed-in account changed. Retry this action.");
  remember(token,user.uid);
  return token;
}
function remember(token: string, uid: string) {
  issued.delete(token); issued.set(token,uid);
  if (issued.size > 8) issued.delete(issued.keys().next().value!);
}

/** Why this device was signed out (the workspace refused the saved sign-in), until the next sign-in or sign-out. */
export function signInNotice(): string { return notice; }

/**
 * The workspace answered 401/403 to `token`, a Google ID token this page sent. Look once more before forgetting anything:
 * refresh the token and ask /api/auth/session. Accepted (a passing failure): resolves the fresh token, and the caller
 * retries its request once. Refused again (401/403) while that account is still the signed-in one: signs it out on this
 * device so the login page shows with a reason, and resolves "". Offline, Google unreachable or a 5xx answer: keeps the
 * sign-in and resolves "". One look per token, however many requests failed with it.
 */
export function recheckRefusedSignIn(server: string, token: string): Promise<string> {
  const pending = rechecks.get(token);
  if (pending) return pending;
  const uid = issued.get(token);
  if (!uid) return Promise.resolve("");
  issued.delete(token);
  const recheck = (async () => {
    const connection = await firebaseConnection(server).catch(() => null);
    const user = connection?.auth.currentUser;
    if (!connection || !user || user.uid !== uid) return "";
    let fresh = "", status = 0;
    try {
      fresh = await user.getIdToken(true);
      status = (await fetch(`${connection.origin}/api/auth/session`, {
        headers:{Authorization:`Bearer ${fresh}`},cache:"no-store",redirect:"error",
      })).status;
    } catch { return ""; }
    if (connection.auth.currentUser !== user) return "";
    if (status >= 200 && status < 300) { remember(fresh,uid); return fresh; }
    if (status !== 401 && status !== 403) return "";
    notice = `Signed out: the workspace did not accept the saved sign-in${user.email ? ` for ${user.email}` : ""}. Sign in again, or ask the workspace owner for access.`;
    // Do not offer the refused account again as "Continue as …"; the popup then shows Google's account chooser.
    if (user.email && lastGoogleEmail() === user.email) storeLastEmail(null);
    googleSignInRefused();
    await connection.sdk.signOut(connection.auth);
    return "";
  })();
  rechecks.set(token,recheck);
  if (rechecks.size > 8) rechecks.delete(rechecks.keys().next().value!);
  return recheck;
}

/** The last Google account that signed in here (a sign-in hint), until an explicit sign-out. */
export function lastGoogleEmail(): string {
  try {
    const email = localStorage.getItem(lastEmailKey) ?? "";
    return /^[^\s@]{1,64}@[^\s@]{1,255}$/.test(email) ? email : "";
  } catch { return ""; }
}
function storeLastEmail(email: string | null) {
  try { if (email) localStorage.setItem(lastEmailKey,email); else localStorage.removeItem(lastEmailKey); }
  catch { /* Storage blocked: the hint is only a convenience. */ }
}

// The workspace must accept the new Google sign-in (/api/auth/session) before it counts; otherwise sign out again.
async function confirmSession(connection: FirebaseConnection, user: User, googleAccount=true): Promise<User> {
  try {
    const response = await fetch(`${connection.origin}/api/auth/session`, {
      headers:{Authorization:`Bearer ${await user.getIdToken()}`},cache:"no-store",redirect:"error",
    });
    if (response.status === 401 || response.status === 403) {
      googleSignInRefused();
      throw new Error("This Google account has not been granted access to this workspace.");
    }
    if (!response.ok) throw new Error("The workspace could not verify your sign-in. Try again.");
    notice = "";
    if(googleAccount){storeLastEmail(user.email);googleSignedIn();}
    return user;
  } catch(error) {
    await connection.sdk.signOut(connection.auth);
    throw error;
  }
}

/**
 * Popup sign-in. Hints the last account (login_hint), so Google can skip its account chooser. Without a hint (first
 * sign-in, after sign-out or a refused account) and for `switchAccount`, Google shows the chooser (prompt=select_account).
 */
export async function signInGoogle(connection: FirebaseConnection, switchAccount = false): Promise<User> {
  const provider = new connection.sdk.GoogleAuthProvider();
  const hint = switchAccount ? "" : lastGoogleEmail();
  provider.setCustomParameters(hint ? {login_hint:hint} : {prompt:"select_account"});
  const result = await connection.sdk.signInWithPopup(connection.auth,provider);
  return confirmSession(connection,result.user);
}

export async function signInEmail(connection:FirebaseConnection,email:string,password:string,create=false):Promise<User>{
  if(!connection.sharedWorkspaces)throw new Error("Email sign-in is not enabled for this server.");
  const result=create?await connection.sdk.createUserWithEmailAndPassword(connection.auth,email,password):await connection.sdk.signInWithEmailAndPassword(connection.auth,email,password);
  return confirmSession(connection,result.user,false);
}

/** Quick sign-in: Google's ID token from the browser's account (Google Identity Services) becomes a Firebase sign-in. */
export async function signInWithGoogleIdToken(connection: FirebaseConnection, idToken: string): Promise<User> {
  const result = await connection.sdk.signInWithCredential(connection.auth,connection.sdk.GoogleAuthProvider.credential(idToken));
  return confirmSession(connection,result.user);
}

/**
 * Explicit sign-out: forget the account on this device, its sign-in hint, and Google's automatic sign-in for this site.
 * The hint and the automatic sign-in are switched off first, and even if Firebase's sign-out fails, so neither depends on
 * the login screen loading afterwards.
 */
export async function signOutGoogle(connection: FirebaseConnection): Promise<void> {
  notice = "";
  storeLastEmail(null);
  googleSignedOut();
  await connection.sdk.signOut(connection.auth);
}

export function googleSignInError(error: unknown): string {
  const code = (error as {code?:string})?.code;
  if (code === "auth/popup-closed-by-user" || code === "auth/cancelled-popup-request") return "Sign-in was cancelled. Your saved work is unchanged.";
  if (code === "auth/popup-blocked") return "Allow the Google sign-in popup for this site, then try again.";
  if (code === "auth/unauthorized-domain") return "This site needs to be added to the Firebase project's authorized domains.";
  if (code === "auth/operation-not-allowed") return "Google sign-in needs to be enabled in Firebase Authentication.";
  return error instanceof Error && !code ? error.message : "Google sign-in could not finish. Try again.";
}
