# Fieldwork login

Implemented and deployed 9 October 2026 (Melbourne).

The [concept](fieldwork-login-concept-v1.png) was generated with built-in imagegen using the [saved prompt](fieldwork-login-prompt-v1.md). The implemented screen uses native text rendering rather than the raster mockup as a background. The cool-white sign-in panel and surveying-blue illustration plate follow the generated composition; Avenir/system humanist typography and monospace roof geometry give it a restrained geospatial identity.

`FieldworkLogin.tsx` presents Google as the primary action, with connection/token settings secondary. `GoogleSignIn.tsx` shares the existing Firebase integration between this screen and the Connection dialog. Loading, disabled, current-account, retry and sign-in errors remain explicit. The editor stays available when an expired session has unsaved work or a recovery draft; a clean signed-out workspace gets the dedicated login.

`AsciiNeighbourhood.tsx` rasterises simple orthographic house, roof, tree and street geometry into monospace characters. Geometry is rendered to two offscreen canvases once per motion-state change. A throttled animation composites an illuminated scan; it pauses in hidden tabs, supports a Pause control, and respects reduced motion. It is decorative and does not represent live predictions. At mobile widths the login remains first and the illustration follows below it.

Validation:

- `pnpm build:annotations`, frontend TypeScript and targeted ESLint passed.
- `node scripts/verify-fieldwork-auth.mjs` passed configured/unconfigured Google UI, workspace token login/reload, outage/retry, actual animation, pause/resume, reduced motion and mobile overflow checks. The browser fixture uses synthetic data and never accesses CRM records.
- Deployment typecheck and Wrangler dry-run passed.
- Live `/readyz` and authenticated GetInfo returned 200; anonymous and invalid credentials returned 401. Public Firebase configuration remains enabled. Live JavaScript matched the local build byte-for-byte.
- The full Google-account callback was not exercised in this UI release; the earlier outstanding live sign-in check remains documented in `docs/annotation-google-login.md`.

Release: Worker `8f2a48d1-8f9d-432d-bceb-587a1cade64f`, frontend `/assets/index-CWTbGQVy.js`, SHA-256 `7c191ff3271333066ab3f06129483a44d2438a07eda92041083d0a5284707c98`. Deployment used `--containers-rollout=none`. No backend image, credential, CRM document or review was changed. Local verification receipts/screenshots are in `.run/fieldwork-login-*`.
