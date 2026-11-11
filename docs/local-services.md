# Local review services on macOS

On 25 September 2026 the review UI reported `Failed to fetch` because all three
shell-started services had exited. Port 3008 refused connections. The existing
built services are now registered with the current user's `launchd` session,
independent of the task's terminal lifetime. No login-startup plist was installed.

| Service label | Port | PID file | Log |
| --- | --- | --- | --- |
| `com.geoau.addr.web` | 3008 | `.run/web.pid` | `.run/web.log` |
| `com.geoau.addr.geo` | 8091 | `.run/server.pid` | `.run/server.log` |
| `com.geoau.addr.ml` | 18090 | `.run/ml.pid` | `.run/ml.log` |

Inspect `launchctl list LABEL` before a process action and verify its arguments
point to this checkout. Do not kill a PID and start a second copy: the job can
restart it. For a deliberate stop, remove only the appropriate registered job:

```sh
launchctl remove com.geoau.addr.web
launchctl remove com.geoau.addr.geo
launchctl remove com.geoau.addr.ml
```

Run only the relevant removal, not all three, for a web-only rebuild. The legacy
`task stop` kills processes; it does not unregister these jobs. After unregistering,
the regular Taskfile lifecycle works again. Submitted jobs last for the current
login session; these are not system daemons or a scheduled inference run.

The exact safe-to-inspect service arguments are in `.run/managed-services.json`.
Launch commands use absolute executable paths and the repository working
directory. The web service loads `.env.local`; the geo service explicitly uses
the ML sidecar on 18090. Existing local CRM development authentication settings
are retained.

Verification after recovery: both `/api/scenes/self-review` and
`/api/scenes/processing` return HTTP 200, and the browser review shortlist loads.
No roof models, saved reviews or training weights were changed by this recovery.

After a web rebuild, wait for the old job to disappear before submitting its
replacement. Registration does not mean the server is ready: verify its PID,
wait for the ready message in `.run/web.log`, then check HTTP 200 before opening
the page. The replacement took about 10 seconds to become ready on this machine.

A separate self-review preparation timeout was reproduced at 30 seconds.
Selection now decodes each shared scene once; start requests have a 90-second
preparation budget, with an explicit preparation indicator and timeout guidance.
The deployed endpoint successfully started a bounded 12-roof batch in 18.11
seconds. Regression checks and the build passed; the browser showed live
progress and saved visual notes. See
`data/roof-review-loop/start-recovery-2026-09-25.json` for verification details.

On 27 September, production builds in the full checkout spent excessive time
tracing runtime directories, including scene data and the Python environment.
The feedback-sync release was built from a temporary copy of the identical app
source, public files, build configuration and installed dependencies, without
runtime datasets. Normal Turbopack compilation took six seconds and TypeScript
passed. The resulting `.next` artifact was installed here, and the web service
was started from this repository so `process.cwd()` still resolves local data.
Both live CLI/MCP pulls and the scene feedback preview passed after restart. See
`docs/annotation/feedback-verification.json` for the build ID and evidence.
