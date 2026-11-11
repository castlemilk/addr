# Google login for Fieldwork

Deployed on 9 October 2026 as Worker version `6d0c1333-bd53-4018-a72b-5bd054ef90fb`. Firebase project `addr-fieldwork-auth` (Fieldwork CRM), Web app `1:1046530555412:web:82bea164cbe2626fc93101`, uses Google as its only enabled login provider. The initial allowed account is the existing CRM owner, `ben.ebsworth@gmail.com`. The issued configuration is saved in the ignored deployment environment and the two Firebase Worker secrets.

The project uses Firebase's Spark plan. No billing account was linked, no service-account private key was created, and existing database/administrator credentials were not changed. Firebase's authorized domains include the hosted CRM, the default Firebase app domains, and localhost. The separate hosted MCP OAuth configuration is unchanged.

Live checks passed for public login configuration, anonymous/invalid-token denial, hostile-origin denial, private-asset denial, CORS preflight, readiness and the existing administrator token against PostgreSQL. The deployed Google button opens the correct Google account/consent flow. **A completed Google session is not yet verified:** popup attempts reported cancellation, and the browser tabs disappeared during further callback tracing. The underlying cause was not established. A fresh CRM Connection dialog is left open for the owner's manual sign-in; the Firebase user list remained empty at verification time. Do not describe the full Google callback as tested successfully yet.

For Google Cloud operations, pass `--account=ben.ebsworth@gmail.com` and `--project=addr-fieldwork-auth` explicitly where supported. Other work on this computer can change the active gcloud account. Do not change the shared default configuration or reuse another app's Firebase project.

## Configuration

1. Select a dedicated Firebase project, or create one for Fieldwork. Register a Web app in that project and copy its public web configuration. Do not supply a service-account private key; the Worker verifies Firebase ID tokens using Google's public signing keys.
2. In Firebase Authentication, enable the Google provider and configure its support email. Add `fieldwork-annotations.ben-ebsworth.workers.dev` to Authentication's authorized domains. Add a local development hostname only if local Google login is needed.
3. Set these in the ignored, mode-0600 `deploy/annotation/.dev.vars` file:

   ```dotenv
   FIREBASE_WEB_CONFIG='{"apiKey":"WEB_API_KEY","authDomain":"PROJECT_ID.firebaseapp.com","projectId":"PROJECT_ID","appId":"WEB_APP_ID"}'
   FIREBASE_ALLOWED_EMAILS='reviewer@example.com,another@example.com'
   ```

   The example values do not work. Use the config issued for the selected Firebase Web app and actual approved accounts. Emails are exact, case-insensitive matches; no wildcards or domain-wide signup. Every allowed account has the existing workspace-wide access, without separate roles.
4. Build and verify from the repository root:

   ```sh
   pnpm build:annotations
   node --test src/lib/annotations/*.test.mjs
   npm --prefix deploy/annotation test
   npm --prefix deploy/annotation run typecheck
   node scripts/verify-fieldwork-auth.mjs
   npm --prefix deploy/annotation run check
   ```

5. When the chosen credentials are ready, upload them using `npm --prefix deploy/annotation run secrets`, then deploy with `npm --prefix deploy/annotation run deploy`. The existing uploader preserves the database/token workflow and sends the additional Firebase values via stdin. Secret updates are live Cloudflare deployments. Never put administrator credentials into frontend environment variables or URLs.
6. On the hosted CRM, open **Connection → Continue with Google**. Verify that an allowed account can list projects and load a private image, an unlisted account is denied, and sign-out returns to the access screen. Use read-only operations on real projects. The local fixture tests do not establish that a real Google OAuth configuration works.

## Authentication contract

- `/api/auth/config` returns only public Web app fields, never the access list or workspace token. Login is disabled when configuration or the allowlist is missing.
- Sign-in is remembered on this device until sign-out: Firebase keeps it in IndexedDB (localStorage when IndexedDB is unavailable), so it survives reloads, new tabs and browser restarts, and all tabs share it. API and private-image requests fetch a current ID token from Firebase, including automatic refresh. Tokens are sent in Authorization headers. On a shared computer, sign out when done.
- With a remembered account, the page shows "Opening Fieldwork" and "Opening your workspace…" until the first API calls answer; it never says "Sign in" while opening, and the sign-in buttons do not flash. When the workspace cannot be reached, the page says "Workspace unavailable" with a retry button.
- If the Worker answers 401 or 403 to the remembered account, the browser looks once more before forgetting anything: it refreshes the ID token and asks `/api/auth/session`. If the account is accepted this time (a passing failure), the failed request is retried once with the fresh token. A 401 means the request was not carried out, so the retry is safe for saves. If the account is refused again (for example after it was removed from the allowlist), the browser signs it out on this device and shows the login page once with a short reason. If the second look cannot finish (offline, Google unreachable, a 5xx answer), the sign-in is kept and the page shows the usual error. Nothing is retried more than once.
- When the Worker cannot load Google's signing keys (timeout, network error, bad answer from Google), it answers 503 `unavailable` with `Retry-After: 5`, not 401, so a short problem on Google's side never signs anyone out. A token that names an unknown key is still refused (401).
- The Worker verifies RS256 signatures, issuer, audience, expiry, issue/authentication time, Google provider and verified allowed email. It substitutes the existing private workspace credential when forwarding to the Go backend or private asset handler.
- Sign-out and connection changes invalidate the frontend connection generation, so pending operations cannot continue under newly selected credentials. Existing draft-navigation protection still applies before opening Connection.
- The backend's shared `workspace` audit actor is unchanged. Google login is access control, not per-user audit attribution. Agent restrictions on projects ending in `-review` still apply.
- Existing administrator tokens and the separate hosted MCP OAuth endpoint continue to work. **Advanced · workspace access token** remains available for CLI-compatible and local connections.
- Firebase ID tokens normally last one hour. This implementation checks signatures/expiry and the current allowlist; it does not call the Admin SDK's token-revocation check. Remove an email from the Worker allowlist for immediate denial on its next network request. Already downloaded content cannot be recalled.
- Exact origins are enforced. If a custom frontend domain is added, configure it in both Firebase authorized domains and `ANNOTATION_ALLOWED_ORIGINS`. CORS preflights and readable authentication errors are supported for allowed origins.

## Verification

The auth tests use ephemeral RSA keys and synthetic identities to exercise accepted and rejected tokens, project/provider boundaries, origin checks, upstream credential substitution and POST-body preservation. API tests exercise token refresh and cancellation across connection changes. `scripts/verify-fieldwork-auth.mjs` serves the built SPA against an isolated mock API and verifies configured/unconfigured login UI plus legacy-token login and reload; all off-origin browser requests are blocked. It never signs into Google or writes CRM data.

References: [Firebase Google sign-in](https://firebase.google.com/docs/auth/web/google-signin), [server-side ID token verification](https://firebase.google.com/docs/auth/admin/verify-id-tokens), [Firebase session/token lifetime](https://firebase.google.com/docs/auth/admin/manage-sessions).

## Same-origin sign-in helper (9 October 2026)

`signInWithPopup` with the default `<project>.firebaseapp.com` authDomain failed live: Google's page loaded, but the result never
reached Fieldwork and the popup ended as `auth/popup-closed-by-user` ("Sign-in was cancelled"). Chrome partitions the storage of
the third-party `firebaseapp.com` iframe, so the popup's result is lost. The "Cross-Origin-Opener-Policy policy would block the
window.closed call" console lines are report-only warnings from Google's pages and are not the cause.

The fix follows Firebase's "proxy auth requests" option:

- The Worker serves `GET`/`HEAD` `/__/auth/*` and `/__/firebase/init.json` from `https://<projectId>.firebaseapp.com`
  (`firebaseHelperRoute`; no credentials or cookies forwarded, no `Set-Cookie` returned). Both wrangler configs list these
  paths in `run_worker_first`.
- `/api/auth/config` returns this Worker's host as `authDomain`. Set the Worker var `FIREBASE_AUTH_DOMAIN_MODE=firebase` to
  hand out the stored authDomain again.
- **Required once, in Google Cloud Console:** the project's OAuth web client
  (`1046530555412-3gs210m74r2bosv5sj3lu111g20sliqs.apps.googleusercontent.com`, project `addr-fieldwork-auth`) must list
  `https://fieldwork-annotations.ben-ebsworth.workers.dev/__/auth/handler` under Authorized redirect URIs. Until it does, Google
  shows "Access blocked: This app's request is invalid" (redirect_uri_mismatch). Check with
  `.run/session-2026-10-09-fieldwork-ui/check-google-redirect.sh` (read-only).

Verified live on Worker 06806074: a simulated Google return reaches the page in under a second (`signInWithIdp` is called), where
before it was lost and reported as cancelled. Probe: `.run/session-2026-10-09-fieldwork-ui/google-return-probe.mjs`.

## Remembered sign-in and quick sign-in (9 October 2026)

The reviewer asked for a login that is remembered and uses the Chrome profile. Three parts:

1. **Remembered on this device.** See the authentication contract above. The previous release kept a sign-in for the tab
   only (sessionStorage); a tab still holding one hands it to the remembered store when it first loads this release, so
   nobody has to sign in again.
2. **Quick sign-in with the browser's Google account.** On the login page only, Fieldwork loads Google Identity Services
   (`https://accounts.google.com/gsi/client`) and asks once per page load whether to continue with the Google account already
   in Chrome (FedCM, with automatic sign-in when Chrome allows it). Google's ID token becomes a Firebase sign-in, then the same
   `/api/auth/session` check as the popup runs; an account the Worker refuses is signed out and the error is shown.
   - Explicit sign-out turns Google's automatic sign-in off on this device until the next accepted sign-in: the browser
     stores `fieldwork.google.signedOut=1` in localStorage, and later login pages ask Google with `auto_select: false`, so
     the Chrome prompt needs a click. It also calls Chrome's `navigator.credentials.preventSilentAccess()` and Google's
     `disableAutoSelect` (which on its own lasts only 24 hours). This happens in the sign-out itself, so it holds even when
     the login page never loads afterwards (unsaved draft kept, tab closed). No prompt shows again on the page where the
     reviewer signed out. A tab-only sign-in left by the previous release is dropped, not adopted, while the flag is set.
   - A refused account turns automatic sign-in off in the same way, so the login page cannot loop. Right after a refused
     saved sign-in, the login page shows the reason without the Chrome prompt, and if the refused account was the
     remembered "Continue as" email, that email is forgotten.
   - A login page left open in another tab follows a sign-in made in any tab and opens the workspace, keeping its deep link.
   - The Worker hands the browser the OAuth web client ID from the plain var `GOOGLE_OAUTH_CLIENT_ID` (in both
     `wrangler.jsonc` and `wrangler.pinned.jsonc`) as `googleClientId` in `/api/auth/config`. A value that is not a well-formed
     client ID is left out, and then quick sign-in is not offered. Remove the var to turn quick sign-in off.
   - **Required once, in Google Cloud Console (done):** the OAuth web client
     (`1046530555412-3gs210m74r2bosv5sj3lu111g20sliqs.apps.googleusercontent.com`, project `addr-fieldwork-auth`) must list
     `https://fieldwork-annotations.ben-ebsworth.workers.dev` under **Authorized JavaScript origins** (origin only, no path).
     Google accepts the origin as of 9 October 2026 (re-checked read-only: the FedCM client check below returns 200 with
     `"name":"ben-ebsworth.workers.dev"`, and the probe shows `gsi 200 …/gsi/button`). Chrome's prompt names the site
     "ben-ebsworth.workers.dev", not "Fieldwork". If the origin is ever removed, Google refuses it ("The given origin is not
     allowed for the given client ID"), quick sign-in cannot finish, and the "Continue with Google" button stays the way in;
     Chrome may then show its own account chooser followed by its own error. Run the check below before each deploy that
     ships `GOOGLE_OAUTH_CLIENT_ID`.
   - Check (read-only; serves a throwaway page at that origin inside a headless browser, so nothing reaches the Worker):
     `node .run/session-2026-10-09-fieldwork-ui/gis-origin-probe.mjs`. A refused origin prints the "origin is not allowed"
     console line and `gsi 403 …/gsi/button`; an allowed origin prints `gsi 200`. Compare with the allowed control
     `node .run/session-2026-10-09-fieldwork-ui/gis-origin-probe.mjs https://addr-fieldwork-auth.firebaseapp.com`.
     A quicker check without a browser (Google's FedCM client check; an undocumented endpoint, so treat it as a hint):

     ```sh
     o=https://fieldwork-annotations.ben-ebsworth.workers.dev
     curl -s -A 'Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36' \
       -H "Origin: $o" -H 'Sec-Fetch-Dest: webidentity' \
       "https://accounts.google.com/gsi/fedcm/clientmetadata?client_id=1046530555412-3gs210m74r2bosv5sj3lu111g20sliqs.apps.googleusercontent.com&top_frame_origin=$o"
     ```

     `{"error":"INVALID_ORIGIN"}` means the origin is still missing; a JSON object with `"name"` means Google accepts it.
3. **Faster popup.** After a successful sign-in the browser keeps the account's email in localStorage
   (`fieldwork.google.lastEmail`) until explicit sign-out (or until that account is refused). The button then reads
   "Continue as <email>" and the popup passes it as `login_hint`, so Google skips the account chooser. Without a remembered
   email (first sign-in, after sign-out) and for "Switch Google account", the popup asks Google for the chooser
   (`prompt=select_account`), so a reviewer who signed out can pick another account.
   - Accepted trade-off: Firebase puts `login_hint` into the `/__/auth/handler` URL, which this Worker serves, so the
     reviewer's email appears in the Worker's Cloudflare invocation logs (observability is on) and in browser history. The
     owner of those logs already holds the allowlist. To keep emails out of the logs, turn off invocation logs
     (`observability.logs.invocation_logs: false`) or drop the hint.

**Security notes.**

- Remembered sign-ins have no maximum age: Firebase keeps the refresh token until sign-out, and the Worker checks
  `auth_time` only for sanity. To cut off a lost or shared device, remove the email from `FIREBASE_ALLOWED_EMAILS` (refused
  on its next request; this ends the account's access on every device), or disable the user in Firebase Authentication (its
  ID token stops refreshing within the hour). A forced re-sign-in after N days would need an `auth_time` limit in the
  Worker; it is not set.
- The SPA's HTML is served with `Content-Security-Policy: frame-ancestors 'self'` and `X-Frame-Options: SAMEORIGIN`
  (`apps/annotation-web/public/_headers`, copied into `dist` by the build), so other sites cannot frame the remembered
  workspace.

Tests: `src/lib/annotations/firebase.test.mjs` (persistence, tab-only carry-over and its sign-out guard, hint and chooser,
second look before sign-out, refusal, sign-out kept across page loads), `google-identity.test.mjs` (lazy load, prompt once,
refused origin, disableAutoSelect, automatic sign-in off across page loads), `api.test.mjs` (a 401/403 gets one second look;
a passing refusal retries once; workspace tokens never do) and `deploy/annotation/src/firebase-auth.test.mjs`
(`googleClientId` only when well formed; 503 when Google's keys cannot be loaded).

