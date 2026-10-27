// Quick sign-in with the Google account already in the browser (Chrome profile), through Google Identity Services (GIS)
// with FedCM. Optional: when the library cannot load or Google refuses this page's origin for the OAuth client, nothing is
// shown and the "Continue with Google" popup button stays the way in.

type CredentialResponse = { credential?: unknown };
type IdConfiguration = {
  client_id: string; callback: (response: CredentialResponse) => void; auto_select: boolean; use_fedcm_for_prompt: boolean;
  itp_support: boolean; context: "signin"; cancel_on_tap_outside: boolean;
};
export type GoogleAccountsId = { initialize(config: IdConfiguration): void; prompt(): void; cancel(): void; disableAutoSelect(): void };

export const GOOGLE_IDENTITY_SCRIPT = "https://accounts.google.com/gsi/client";
const clientIdPattern = /^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$/;

/** The OAuth web client ID from /api/auth/config, or "" when it is missing or malformed. */
export function googleClientId(value: unknown): string {
  return typeof value === "string" && value.length <= 128 && clientIdPattern.test(value) ? value : "";
}

let loading: Promise<GoogleAccountsId | null> | undefined;
let initializedFor = "";
let prompted = false; // one prompt per page load
let promptsOff = false; // explicit sign-out: no prompt again from this page
let autoSelectOff = false; // explicit sign-out or a refused saved sign-in: no silent sign-in from this page
let disablePending = false; // disableAutoSelect waiting for the library
let handler: ((idToken: string) => void) | null = null;

// Kept across page loads: this device was signed out on purpose (or its account was refused), so Google must not sign it
// back in without a click until the next accepted sign-in. Google's own disableAutoSelect lasts only 24 hours and needs
// the library loaded, which happens only on the login screen.
const signedOutKey = "fieldwork.google.signedOut";

/** True after an explicit sign-out or a refused account on this device, until the next accepted sign-in. */
export function googleAutoSignInOff(): boolean {
  try { return localStorage.getItem(signedOutKey) === "1"; } catch { return false; }
}
function storeAutoSignInOff(off: boolean) {
  try { if (off) localStorage.setItem(signedOutKey,"1"); else localStorage.removeItem(signedOutKey); }
  catch { /* Storage blocked: Google's own 24-hour setting and Chrome's silent-access flag still apply. */ }
}

function loaded(): GoogleAccountsId | undefined {
  return (window as unknown as { google?: { accounts?: { id?: GoogleAccountsId } } }).google?.accounts?.id;
}

/** Loads the GIS library once. Resolves null (and stays null for this page) when it cannot load. */
export function loadGoogleIdentity(): Promise<GoogleAccountsId | null> {
  const ready = loaded();
  if (ready) return Promise.resolve(ready);
  loading ??= new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = GOOGLE_IDENTITY_SCRIPT; script.async = true;
    script.onload = () => resolve(loaded() ?? null);
    script.onerror = () => { script.remove(); resolve(null); };
    document.head.appendChild(script);
  });
  return loading;
}

function quietly(run: () => void) {
  try { run(); } catch { /* Quick sign-in is optional; the popup button stays. */ }
}

/**
 * Offers the browser's Google account on the login screen. The library loads only here, prompt() runs at most once per
 * page load, and never on the page where the reviewer signed out. After an explicit sign-out or a refused account, Google
 * may not sign in without a click (auto_select off) until the next accepted sign-in, on this and later page loads.
 * `onCredential` receives Google's ID token. Returns a cleanup that detaches `onCredential` and closes a prompt still on
 * screen.
 */
export function offerQuickSignIn(clientId: string, onCredential: (idToken: string) => void): () => void {
  const id = googleClientId(clientId);
  if (!id || (promptsOff && !disablePending)) return () => {};
  handler = onCredential;
  let active = true;
  void loadGoogleIdentity().then((api) => {
    if (!api || !active) return;
    quietly(() => {
      if (initializedFor !== id) {
        api.initialize({
          client_id: id,
          callback: (response) => { if (typeof response?.credential === "string" && response.credential) handler?.(response.credential); },
          auto_select: !autoSelectOff && !googleAutoSignInOff(), use_fedcm_for_prompt: true, itp_support: true, context: "signin", cancel_on_tap_outside: false,
        });
        initializedFor = id;
      }
      if (disablePending) { disablePending = false; api.disableAutoSelect(); }
      if (!prompted && !promptsOff) { prompted = true; api.prompt(); }
    });
  });
  return () => {
    active = false;
    if (handler === onCredential) handler = null;
    if (prompted) { const api = loaded(); if (api) quietly(() => api.cancel()); }
  };
}

function disableAutoSelect() {
  autoSelectOff = true;
  storeAutoSignInOff(true);
  // Chrome's own flag for this site: FedCM will not sign in without a click until the next sign-in with one.
  quietly(() => { void navigator.credentials?.preventSilentAccess?.()?.catch(() => {}); });
  const api = loaded();
  if (api && initializedFor) quietly(() => api.disableAutoSelect()); else disablePending = true;
}

/** Explicit sign-out: Google must not sign this browser back in silently, and no prompt shows again from this page. */
export function googleSignedOut() { promptsOff = true; disableAutoSelect(); }

/** The workspace refused the account: stop silent sign-in so the login page cannot loop. The chooser may still show. */
export function googleSignInRefused() { disableAutoSelect(); }

/** The workspace accepted a sign-in: automatic sign-in may be offered again on later visits. */
export function googleSignedIn() { autoSelectOff = false; storeAutoSignInOff(false); }
