# Focus recapture for frame-cut small-roof targets: operator runbook (2026-10-01)

Executes the final design in `.run/focus-recapture-design-2026-10-01.md` (10 sections; `.run/` is gitignored) once the
reviewer pauses the current re-marking batch and the memory gate opens. Reviewer direction, verbatim: "we need proper
centering and accurate windowing for the given house and object". Written 2026-10-01T04:14Z (14:14 AEST). Every number
below was read from a file or command at write time and is sourced beside it; nothing here was executed beyond
read-only reads, read-only counter runs (section 0) and `jobgate` probes. Repaired 2026-10-01T05:03Z after a skeptic pass:
sections 1.1, 1.4, 1.5, 1.6, 2 (conventions, steps 1-5, 7, 8, 12), 5, 6.2, 7 and 8 were re-read against the tree; every
figure in them is marked "at repair time" where it was read then. Refreshed 2026-10-01T05:34Z (15:34 AEST) after a second
skeptic pass: every line citation was re-read by symbol (grep), not by number; the claim that `task` echoes into stdout
was retested and withdrawn (section 1.1); the landed `WINDOW.json` and `register` subcommand replace the PROPOSED
pre-registration (sections 1.5, 2 step 2, 7); the plan figures are those of `plan-2026-10-01.v2.jsonl` (step 3); the
counter was re-run once (section 0); one wording fix landed in `scene_focus.py` and its test (section 1.6). Figures
read then are marked "at refresh time". **Refreshed against the EXECUTED state 2026-10-01T07:38Z (17:38 AEST)**: the
capture-only run B of 06:43-07:11Z has happened (section 0b records it from `.run/focus-recapture/capture-2026-10-01.log`,
the dated plan, the registry, the counter snapshots and the push / pull receipts); step 3's gate is no longer the 0.6
fraction but DESIGN `subject_recapture.min_overlap_px` 128 (dated amendment 06:11:15Z); step 7's carry is no longer
PROPOSED (`scene_focus carry` / `task focus:carry`); the identity-reservation rule and the two `lot-*` collisions are in
section 6.4; what remains is section 7. Figures read then are marked "at executed-state refresh". Two streams were still
writing while this refresh was read (section 0b, last rows): numbers there are marked with their read time.
**Refreshed a second time against the EXECUTED state 2026-10-01T08:42Z (18:42 AEST; reads 08:36-08:48Z)**: streams T, H and
C have finished (section 0c records them from `.run/focus-recapture/capture-2026-10-01.log:205-276`, the 2026-10-01b plan,
the registry, `counter-H-after.json` / `counter-C-{before,after,final}.json`, the push / pull receipts and the applied-*
records): the held subject was captured as `win-6dbb766c398af77e` in run H, so 49 of 49 windows are captured, verified,
carried and pushed (registry 84 rows, 80 copies); the hold list `frame-cut-hold-2026-10-01.md` EXISTS (07:46:34Z; section
1.2); the DESIGN `identity` amendment is APPLIED as dated entry 4 (08:24:41Z) under the design-sha chain rule
(`scripts/ml/design_chain.py`, stated by RULES entries 14-15 at 08:28:04Z; sections 0c, 1.5); every line citation in
sections 1.6 and 2 (steps 3-7) was re-grepped by symbol at the current module shas and is cited with the sha it was read
at. Figures read then are marked "at the second executed-state refresh" or "08:42Z". Nothing was writing during the 08:36-08:48Z
reads (the newest file under `data/reviews/eyes/recapture/` was DESIGN.json, 18:24:41 AEST, and the newest
`.run/focus-recapture/` record `counter-C-final.json`, 18:28); at 08:49-08:54Z, AFTER those reads, a further operator stream (G,
`scene_focus carry --repair-groups`) and, at 08:54:38-40Z, a Codex script of ANOTHER session (`data/reviews/eyes/shade-sails-2026-10-01/save_classifications.py`: two POSTs to the local app, then a LIVE Fieldwork `SaveItem`) wrote again — recorded from the files at 08:57Z, corrected 09:24Z, as the last row of section 0c
and item 11 of section 7.0b; the 08:42Z figures stand as read then.

What this runbook does NOT do: the document itself builds nothing, captures nothing, writes no scene, touches no frozen
file, no `src/`, no `scripts/*.mjs`, no Fieldwork item (the document writes none; of the streams it records, only the live `task push` runs of B and H created items, sections 0b / 0c; OUTSIDE every stream of this runbook, the shade-sail Codex session's `save_classifications.py` wrote LIVE to Fieldwork at 08:54:40Z — item `addr-scene-house-29785-me373cb0a9d78-r0` revision 2 -> 3, flagged in section 0c's last row and section 7.0b item 11); it records what the operator streams did. At the executed-state
refresh NOTHING in it is PROPOSED any more: `scripts/ml/scene_focus.py` has `register`, `plan`, `capture` and `carry`
(section 1.6; `scripts/ml/scene_window.py` never existed and is not needed), the RULES entries were appended by the dated
A1 script (06:03:14Z) and the DESIGN / WINDOW entries by the dated A2 script (06:11:15Z), the DESIGN `identity` entry and the RULES chain entries by stream C's dated scripts (08:24:41Z and
08:28:04Z; sections 1.5, 0c), and the capture-only run B captured 48 of the 49 planned windows, carried 78 copies and pushed
the 48 windows (section 0b); run H captured the 49th (`win-6dbb766c398af77e`), carried 2 more copies and pushed it (section 0c). The design's "never"
list binds every further step: no write to an existing scene under `data/scenes/` (latest.json, review.json, models/) — and
the 49 `lot-*` / `win-*` windows ARE existing scenes now (48 from run B, `win-6dbb766c398af77e` from run H): their latest.json / review.json are the reviewer's, never
rewritten by this path; a new window directory is created only by `scene_focus capture` / `carry` under a NEW dated plan
name (the held `lot-30381` re-plan is `window-plan-2026-10-01b.jsonl`, section 6.4); no rewrite of RULES.json / DESIGN.json /
WINDOW.json body text (dated `amendments` entries only), dataset-v* splits, plan-/pilot-<date> files, CP*.json or anything
under `data/references/` except the worklist review surfaces under `data/references/eyes-checkpoints/` (the new
`WINDOW-WORKLIST-2026-10-01.md`, written 07:10:06Z by `write-worklist-b.py`, a script that refuses when the file exists, and REWRITTEN since by a writer the file does not name — live at 09:24Z: 66,185 bytes, mtime 09:04:37Z, sha `e72130c5…`, a repair note at line 3 and a 19:05 AEST note at line 7; `d9b71970…` was the 07:10:06Z state only; section 0b "Review surface" row); the dated plan `window-plan-2026-10-01.jsonl` is
never rewritten and the registry `windows-2026-10-01.jsonl` is append-only; no reviewer decision is ever fabricated or copied
(a carried copy is a target, `carried_from.status` is a hint); agreement is never accuracy; nothing is promoted.

## 0. State at write time (read, not typed; items marked "refresh" were re-read at 2026-10-01T05:34Z)

| item | value | source |
|---|---|---|
| Host load | 162.13 / 152.60 / 134.90 at 14:14 AEST (95.54 at 14:07, 134.80 at 14:09); refresh: 192.17 / 160.43 / 146.24 at 15:19 AEST | `uptime` |
| Swap | total 36,864 MB, used 35,842.25 MB, free 1,021.75 MB; refresh: used 35,594.25 MB, free 1,269.75 MB | `sysctl -n vm.swapusage` 14:09; 15:19 AEST |
| `jobgate 7500` | `swap_free_mb=1021.75 pages_free_mb=8506.02 budget_mb=7500 need_mb=8524` -> exit 0 (OPEN by memory at 04:09:20Z); the gate has no load term, and load was 134.80 | `. .run/jobgate.sh && jobgate 7500` |
| `jobgate 1500` | exit 0 at 04:09:20Z (`need_mb=2524`) | same |
| `jobgate` at refresh | `jobgate 7500` and `jobgate 1500` both exit 1: `GATE CLOSED: a job is running: 81119 node .../node_modules/.bin/tsc --noEmit` (a GitHub-runner `tsc` under `~/.cuttlefish/`, matched by the gate's `pgrep` pattern `node_modules/(\.bin\|typescript/bin)/tsc`, `.run/jobgate.sh:4`): the process term closes the gate whatever the memory, so a stray `tsc` must end before any gated step | `. .run/jobgate.sh && jobgate 7500; jobgate 1500` at 15:19 AEST |
| Earlier gate reads | 7500 CLOSED at 2026-09-30T23:41:51Z (`swap_free_mb=784.81 pages_free_mb=100.172`); "jobgate 7500 CLOSED all morning (swap free 1.3 GB at open, 0.77 GB at close)" | `.run/round5-readiness-2026-10-01.md:247-248`; `docs/handover-2026-10-01.md:127` |
| Live counter (refresh) | `computed_at 2026-10-01T05:21:21Z`: PRIMARY N = 7 (roof 2, not_roof 5) on 2 scenes (7 `primary_rows`); `n_rule` {30 / 8 / 6, `passes` false}; `targets_total` 170, `targets_remaining` 3; `target_exclusions` {agent_noted 17, decided 7, fieldwork_pending 143}; `reviews_read` 200; `rules_sha256 d7613249...`, `split_file_sha256 1ba87eed...`, `code_sha256 f6496653...` (the current `r5_precision.py`, section 1.6); `lineage` {design_sha256 29fba5a3..., registry_files [], registry_rows 0, carried_copies 0, carried_joined 0, every `carried_refused_by_reason` 0, carried_primary 0, duplicate_decision_on_recapture 0, ambiguous_carry 0}; `carried_rows` []. The 04:13:25Z snapshot (`counter-runbook-20261001T041307Z.json`, code `dca5927d...`) has the same gated fields: section 6.2 snippet on that pair, six SAME, exit 0 | `.run/focus-recapture/counter-runbook-20261001T052115Z.json` (from `scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision count --json --block v2`) |
| Counter footprint | 18.28 s real; maximum resident set size 706,723,840 bytes (674 MB) under load 134-162; refresh: 6.03 s real, maximum resident set size 2,088,976,384 bytes (1,992 MB) under load 192 (three times the 04:13Z RSS; read, not explained here) | `.run/focus-recapture/counter-runbook-20261001T041307Z.time`; `counter-runbook-20261001T052115Z.time` (`/usr/bin/time -l`) |
| Counter at 09:26 AEST | N = 7, targets_remaining 161, exclusions {agent_noted, decided} only | `.run/counter-after-registry-fix.json` |
| Frame-cut targets | 32 scenes / 86 objects: 18 `map-*` / 48, 12 `roof_training_250-*` / 35, `house-29785` / 2, `mooroolbark-50011498-17-118442-80416` / 1 | `/tmp/frame-cut-targets.json` (ephemeral; per-scene counts listed in section 5 and the design §1) |
| Example scene | `map-3c7647c8dbada27f`: 512 px, z20, span 35.996 m, 15 objects, review present, model_signature `96de16c55f84...`, image `ea920e00d3bf...`; cut targets `995244b3e1216ea7`, `4e9ac665bfacf31f`, `511d1fdbfb1ec4b5` | `data/scenes/map-3c7647c8dbada27f/latest.json`; `/tmp/frame-cut-targets.json` |
| Other cut scenes | `house-29785` 1024 px z20 130.004 m 193 objects (targets `7ddfb55acd70b9fa`, `4e615c2e23cb5015`); `house-29772` 1024 px z20 130.004 m 211 objects, NO review.json (covers the 29785 slices' lot; recorded, never reused); `mooroolbark-50011498-17-118442-80416` 1152 px z19 271.827 m 147 objects (target `2ed8fca6388e27f0`) | the scenes' `latest.json` |
| Scene dirs | 562; no `lot-*`, `win-*` or `qa_*` (same at refresh) | `ls data/scenes \| wc -l`; `ls -d data/scenes/lot-* ...` -> no matches (write time and refresh) |
| Frozen shas | DESIGN.json `29fba5a30d9465ebed06554b4cdd389bdc2ec21cdab878c5c40d5ad60b506de7`; RULES.json `d7613249422f715c6b2dea321dd08c71f0c07e599b9cc443a0a695ffb2d4c214`; plan-2026-09-30.jsonl `f476448f9ead594d812bdc33289d89c2d1175ab4b667af807b850cb5903d7202`; pilot-2026-09-30.json `08ad5ffccdd33e792b95536349bad65e5a59d86d69dc5c22a140c2d08e9c4acb`; offset-diagnosis-2026-10-01.md `560b525047c6d0e8fa5ff14ff329450c950ba619617f689e7bd7be99b6f42e9e` (all five unchanged at refresh); WINDOW.json (landed 05:02:09Z, section 1.5) `2d7c4a5bbb1bb8982b86fff15ef88aba2554eff8673ca2d7278ddb93e8dbffd4` | `shasum -a 256` at write time and at refresh |
| DESIGN values | max_shift_m 0.7, min_ncc 0.35, min_overlap_fraction 0.6, match_iou 0.5, close_iou 0.9; identity "new scene id lot-<address_id>; source untouched; forward pointer only under data/reviews/eyes/recapture/"; 2 amendments (12:39:58Z residual_method, 13:41:36Z counting_rule), both `thresholds_changed: false` | `data/reviews/eyes/recapture/DESIGN.json` |
| RULES | `sam3-round5-rules-v1`; `human_precision.lineage` null; n_rule 30 / 8 per class / 6 scenes; 7 amendments all `2026-09-30T13:33:07Z` (same at refresh; `lineage` still null); `fresh_marker_regex` `^\s*\[reviewer\b[^\]]*\]\s*(?P<cls>not[ -](?:a )?roof|roof)\b` | `data/reviews/eyes/training/sam3-mac/round5/RULES.json` |
| ML tile server | pid 88854 `scripts.ml.server --port 18090 --heights`, RSS 48,976 KB, up 17:32 h (refresh: RSS 50,464 KB, up 18:40 h); `/stats` generated 0, queue 0, concurrency 2 (thread semaphore, `server.py:118`; same at refresh): the model is not resident, so no live SAM3 footprint can be read from it | `ps -o rss -p 88854`; `curl 127.0.0.1:18090/stats` |
| Fieldwork | push run `20261001T015439Z-c7cd42ab` status `pushed` (14 created, 2 marked); pull at cursor 590 status `conflicts` (34 applied, 6 field conflicts on `addr-scene-roof_training_250-30371`) | `.run/fieldwork-path/push-live-14.stdout.json`, `pull-live-2.json` |
| Git | `docs/*.md` and most of `scripts/ml/*.py` untracked (`git ls-files docs` = 0, `scripts/ml` = 29 of many; same at refresh); "unchanged" claims rest on sha manifests, never `git diff` | `git ls-files`; `.opencode/skills/addr-3d/SKILL.md:884` |

## 0b. Executed state: the capture-only run B of 2026-10-01 (read 2026-10-01T07:38Z; every figure from the named file)

| item | value | source |
|---|---|---|
| RULES amendment A1 | 2026-10-01T06:03:14Z, 5 entries appended (7 -> 12): `human_precision.join_order`, `population_primary`, `targets` (the carried_from hop texts), `agent_fresh_decision` (the accepted-import route), `classes_order`; sha `d7613249…` -> `df42a3e51bf7f61e2b991960f22c5e1c3b22c8bf2a63ff3b3e0979e3a60ac436`; `human_precision.lineage` stays null (the hop text lives in the three fields, not a new key); `r5_precision.py` `f6496653…` -> `9b4c363c…` (`accepted_import`, `classify`, `_target_exclusion`); 33 tests passed, ruff clean; authorisation "yes to defaults" items 1 and 4 | `.run/focus-recapture/applied-rules-2026-10-01.json` (`written_at` 06:06:40Z); `RULES.json` `amendments` (12 at batch time) |
| Counter across A1 | before 05:56:24Z N = 7 (roof 2 / not_roof 5, 2 scenes) -> after 06:05:39Z N = 13 (roof 8 / not_roof 5, 3 scenes); `primary_rows_added` = the 6 accepted-import confirmations on `map-3c7647c8dbada27f` (`f18f05e5…`, `995244b3…`, `1617b498…`, `d9407498…`, `527bea10…`, `c8ee3ba1…`, all P); `target_exclusions` decided 7 -> 13, agent_noted 17 -> 11; `targets_remaining` 3 -> 3; counter deltas only, no attribution of any reviewer action | `.run/focus-recapture/counter-A1-before.json`, `counter-A1-after.json`; `applied-rules-2026-10-01.json` `counter.deltas` |
| DESIGN amendment A2 | 2026-10-01T06:11:15Z: new body block `subject_recapture` {`min_overlap_px` 128, unit TARGET px = smaller side of the eroded overlap region's bounding box, `applies_to` `frame_transform.role == 'frame_cut_target'`}; `thresholds_changed` TRUE for subject recaptures only (same-address recaptures keep `min_overlap_fraction` 0.6; `max_shift_m` 0.7 and `min_ncc` 0.35 unchanged); DESIGN sha `29fba5a3…` -> `da322027d77ec6ec9424cb9b64cc69753a09680b0c1aab82178942b0d48f1541` (3 amendments); WINDOW.json gained its first `amendments` entry at the same instant (body text unchanged), sha `2d7c4a5b…` -> `4353815256a35b42125dd5626da491de9d185065327d11e94ee9f9c85aee1ade`; floor derived from `EDGE_PX` 10 / `MIN_SIDE_PX` 24 and the 90 read-only pilot pairs (smallest eroded side 84 px, 18 pairs below 128 px, every one verified) BEFORE the v3 plan ran; authorisation "yes to defaults" item 5 (= design §9 decision 1 option b) | `.run/focus-recapture/applied-design-2026-10-01.json` (06:15:54Z); `amend-2026-10-01.stdout.json`; `DESIGN.json` `amendments[2]`; `WINDOW.json` `amendments[0]` |
| Dated plan | `data/reviews/eyes/recapture/window-plan-2026-10-01.jsonl`, `written_at` 2026-10-01T06:43:30Z, OFFLINE (`task focus:plan TARGETS=/tmp/frame-cut-targets.json DATE=2026-10-01`), `host_load` [73.36, 74.02, 101.9], sha `4f198b26022f240c0568e952e1aee7aae7bef9ab658c93bcfb697f9b4b0fbe6a`, 50 lines = header + 49 windows; `targets_sha256` `d2136295…` (/tmp file; the v3 dry run read the reconstructed `.run/focus-recapture/frame-cut-targets-2026-10-01.json`, `b2cf02e5…`, same 86 pairs, header `totals` equal); `design_sha256` `da322027…`, `window_sha256` `43538152…` on the header and every row; `totals`: targets 86, subjects 85, windows 49, `overlap_gate_ok` 49, `below_design_overlap` 0, `below_fraction_rule` 29 (= `passes_px_floor_not_fraction` 29), `frame_px` {1024: 29, 512: 20}, zoom 20 for all, identity {lot 8, win 41}, `uncarried_targets` 4, `unresolved_lot` 37, `lots_resolved` 12, `plan_block_subjects` 3, `multi_lot_windows` 3, `kept_apart_subjects` 10, `exists_skip` 0; `rules` {lot 12 / subject 28 / subject_no_body 9; fitted 23 / region 29}; `skipped` 1 `not_frame_cut` (`map-806de95f666a01c7` / `815d84c355fa7b73`); `spans_m` 32.479 / 69.174 / 120.004; header `overlap_gate` {rule `subject_recapture.min_overlap_px`, `min_overlap_px` 128, `eroded_by_px` 10, `min_overlap_fraction` 0.6 recorded beside it} | the plan's header row; `.run/focus-recapture/capture-2026-10-01.log:3-16`; `plan-dated-2026-10-01.stderr` (the `--offline` command line) |
| Rehearsal B | `qa_focus_b_1790837065` of `win-2076df0d53bc99b2` (cache-only): verified, overlap fraction 0.88494, region side 505.7 px, shift 0.0002 m, ncc 0.9991, cache 9 hits / 0 misses; `carry --registry none` wrote `review.json` revision 1 with 2 copies (`4e9ac665bfacf31f`, `511d1fdbfb1ec4b5`) and listed `995244b3e1216ea7` `decided_on_source` (an accepted-import confirmation counted on the source: source wins); editor 200; API revision 1, `stale` false, `recovery` None; scripted POST save -> 200, revision 2, `carried_from` + `role` kept; scratch removed (`qa_*` dirs 0) | `capture-2026-10-01.log:17-28`; `rehearsal-b-qa_focus_b_1790837065.{capture,carry,api}.json` |
| Batch | start 06:46:34Z, end 06:57:33Z; gate-ok windows 49; `HOLD=(lot-30381)` (identity reserved: address 30381 is cohort scene `roof_training_250-30381`'s own; section 6.4); 48 captured, 48 `--verify-source/--verify-target` exit 0, 48 carries exit 0; per window: `jobgate 1500` header -> `task focus:capture … CONFIRM=1` -> `scene_recapture --verify-…` -> `task focus:carry WINDOW=… CONFIRM=1`; 1-9 s each; the gate closed 3 times on the runner `tsc` before the first window (:31-36) and 5 times on memory during the batch (:51, :67-71, :99; 60 s waits, never 10 in a row); frame_px 1024: 28 / 512: 20 (the held row was the 29th 1024) | `.run/focus-recapture/run-captures-b.sh`; `capture-2026-10-01.log:29-156`; `captures/<window>.{capture,verify,carry}.json` (288 files) |
| Measured frames (48) | shift_m min / median / max 0.0002 / 0.00615 / 0.1731; ncc 0.7569 / 0.9868 / 0.9999 (worst `win-ab373041656385bf`); eroded region side px 161.2 (`lot-29385`) / 327.9 / 625.5 (`lot-29772`); source fraction 0.028104 (`win-f94c545af3c4961a`, the mooroolbark tile) / 0.527 / 1.0 — 29 windows below 0.6, every one verified on the px floor; edge margin m 13.12 / 20.705 / 34.73; 21 fitted cohort sources carry `frame_transform.source_displacement` {method `imagery-paste-rounding`, `native_px` [0.4444, 0.4444], `native_zoom` 19, `applied` FALSE, `approx_m` 0.1049, source doc sha `560b5250…`} — recorded, never composed into `pixel_affine`; region-source scenes (e.g. `lot-29772`) carry none | `capture-2026-10-01.log:154-156`; `captures/win-3fa3eb10672b7241.capture.json` and `captures/lot-29772.capture.json` `frame_transform` |
| Carry | 82 registry rows = 78 `carried_source` + 4 `uncarried_source`; 1 `decided_on_source` (`win-2076df0d53bc99b2` / `995244b3e1216ea7`); uncarried: `win-2d422aac4db9f40d` <- `roof_training_250-29790/ed49f721661acdee`, `win-b6901e4edcbc5495` <- `map-6cdb42c641f32b33/370978a0297b05d2`, `win-e1f7b7ed873a7999` <- `map-f76d09b5752bb716/025cbc995dfed1f0`, `/ae3b556491e54311` (second source scenes sharing a window: design §2 grouping); `renamed` [] (every copy keeps its source id; the model layer is empty, `model objects 0`) | `capture-2026-10-01.log:151-153`, `:53`, `:189` |
| Registry | `data/reviews/eyes/recapture/windows-2026-10-01.jsonl`: 82 rows, sha `1eb6dee8a7010fefb1fdd74ce3885246f982dabd6611d4638385a6e7315e674b`, `version` `windows-registry-v1`, 48 windows, 32 source scenes, 0 `withdrawn`, every row `frame_transform_verified` true, `design_sha256` `da322027…`, `window_sha256` `43538152…`, `written_at` 06:49:37Z-06:57:32Z, no frozen key in two rows; row keys `source_scene_id, object_id, window_scene_id, carried_object_id, role, frame_transform_verified, design_sha256, window_sha256, source_model_signature, source_image_sha256, written_at` | the registry file (`json.loads` per line) |
| Counter across B | before 06:38:45Z (N 13, `targets_remaining` 3, exclusions {agent_noted 11, decided 13, fieldwork_pending 143}, `lineage.registry_files` []) -> after 07:05:46Z and final 07:11:06Z (N 13, roof 8 / not_roof 5, 3 scenes, `read` "n/a (N=13, roof=8, not_roof=5, scenes=3)"; `targets_total` 170; `targets_remaining` 80 = 78 carried copies asked once on their windows + 2 old slices on `roof_training_250-30371` (`133736ada6ace260` 13.6 m², `5d1c61a0f7ef49d9` 10.78 m², the held `lot-30381`'s sources); `target_exclusions` {agent_noted 8, decided 13, fieldwork_pending 69, `recaptured:<window>` 48 keys summing 78}; `lineage` {registry_files [windows-2026-10-01.jsonl], registry_rows 82, carried_copies 78, carried_joined 78, every `carried_refused_by_reason` 0, carried_primary 0, duplicate_decision_on_recapture 0, ambiguous_carry 0}; `carried_rows` 78 (each `carried_undecided`, path P, `primary` false); `primary_rows` identical before / after; `rules_sha256` `df42a3e5…`, `code_sha256` `9b4c363c…`, `split_file_sha256` `1ba87eed…`, `reviews_read` 248, `next_scenes` 49 entries summing 80) | `.run/focus-recapture/counter-B-before.json`, `counter-B-after.json`, `counter-B-final.json`; `capture-2026-10-01.log:157-170`, `:202-204` |
| Fieldwork push | dry run `20261001T070620Z-d4ab79b9` 07:06:13Z (`status` planned, `counts.create` 48, `labelsToCreate` 0, `mirrorCursor` 590, `bytesToUpload` 59,714,059) then the LIVE run `20261001T070709Z-5da0381e` 07:07:07Z (`FIELDWORK_PUSH_ALLOW_LIVE=1 task push -- --scenes <48 ids>`; `status` pushed, `created` 48, `uploadedAssets` 144, `uploadedBytes` 59,714,059, `marked` 0, `markConflicts` 0, `skippedIdentical` 0, `supersedes` [] on every scene); items `addr-scene-<window>-m<sig[:12]>-r1` (e.g. `addr-scene-lot-25641-m164206520024-r1`), title "Recapture window · <source scene> · <address> · <n> frame-cut target(s)" (read: "Recapture window · roof_training_250-25598 · 16A GREENVILLE STREET, Mooroolbark VIC 3138 · 1 frame-cut target"), tags `["mooroolbark", "pushed", "gen", "review", "outside-inventory"]` — NO `lot-window` tag and `metadata.addr.generation.windowOf` ABSENT (section 4 expected both; read from the request document, recorded, not reconciled); `push:status`: items 64, markedItems 2, runs 4, 48 receipts | `.run/focus-recapture/push-dry-run-b.stdout.json`, `push-live-b.stdout.json`, `push-status-b.json`; `data/fieldwork-sync/mooroolbark/push/runs/20261001T070709Z-5da0381e/{plan,result}.json`, `items/addr-scene-lot-25641-m164206520024-r1.request.json`; `capture-2026-10-01.log:171-187`, `:201` |
| Fieldwork pull | 07:09:10Z `task pull`: `status` "conflicts" (PRE-EXISTING: the same `addr-scene-roof_training_250-30371` item as `.run/fieldwork-path/pull-live-2.json`, 6 field conflicts = notes + status of `d7f7ceb84a6d0db4`, `5d1c61a0f7ef49d9`, `133736ada6ace260`), cursor 590 -> 638, `counts` {changes 48, items 35, appliedScenes 0, alreadyAppliedScenes 0, receiptOnly 0, conflicts 6}, `reviewUrls` []; `nextStep` "resolve the reported conflicts, then rerun task pull" | `.run/focus-recapture/pull-b.stdout.json`; `capture-2026-10-01.log:175-184` |
| Integrity 07:10:44Z | RULES `df42a3e5…`, DESIGN `da322027…`, WINDOW `43538152…`, pilot `08ad5ffc…`, plan-2026-09-30 `f476448f…` unchanged; `REMARK-WORKLIST-2026-10-01.md` `77cb0c5e…` (same at 07:38Z); `src/` or `scripts/*.mjs` newer than the B baseline: 0 and 0; `qa_*` dirs 0; `lot-*` / `win-*` dirs 48 (7 `lot-*`); scene dirs 610 (562 + 48) | `capture-2026-10-01.log:190-200`; `ls data/scenes` at 07:38Z |
| Review surface | NEW `data/references/eyes-checkpoints/WINDOW-WORKLIST-2026-10-01.md` (written 07:10:06Z by `.run/focus-recapture/write-worklist-b.py`, a script that refuses when the file exists; AS WRITTEN THEN 64,460 bytes, sha `d9b71970a679cf93e7a7f9ff9148388a0e5517cc13900ef595dde9dd7536e301`. CORRECTION 09:24Z: the file was REWRITTEN after 07:10Z by a writer it does not name — live 66,185 bytes, 500 lines, mtime 09:04:37Z, sha `e72130c53cd355ac485eb0bd3cca2fe8393aada5a64c96f14f81d0452b29a069`, with a repair note at line 3 (dated 08:50:42-08:50:45Z, citing `counter-G-after.json`) and a "Note 19:05 AEST" at line 7 (the two `lot-29772` copies saved at 18:54 by another session with machine provenance; the counter excludes them as machine labels); an intermediate state reported to this correction (08:52:37Z, 65,688 bytes, sha `8b0ceb2f…`) is on no file on disk and is NOT verified here; `d9b71970…` names the 07:10:06Z state only; "Windows refused or held" still lists `lot-30381` at :487-491): per window the title, frame, measured transform, registry rows and one editor URL per carried copy with the source status hint and Codex's OLD-slice call marked "hint only"; its item 4 carries the hold-list sentence for the old slices; "Windows refused or held" lists `lot-30381` | the worklist file (head and lines 483-487 at 07:38Z; `stat`, `shasum`, `wc`, lines 1-10 and 487-491 at 09:24Z) |
| Hold list file | ABSENT at 07:38Z (`test -e` then; the instruction had reached the reviewer through the worklist above, item 4). WRITTEN at 07:46:34Z by stream H (`.run/focus-recapture/write-hold-list-h.py`, mode `x`, reads only): `data/reviews/eyes/recapture/frame-cut-hold-2026-10-01.md`, 27,347 bytes, sha `cee08c915652ac8e7a3ddc56d0c33e1f2fb54bb6360956de053f19df591af6b2`; header "86 objects on 32 scenes touch their saved frame edge", read from `window-plan-2026-10-01.jsonl` (`4f198b26…`), `window-plan-2026-10-01b.jsonl` (`d177906d…`), `windows-2026-10-01.jsonl` (`a0073fc3…`, 84 rows, 49 windows) and the carry outputs; "Rows: carried_source 80, uncarried_source 4, decided_on_source 1, not_frame_cut 1, other 0"; 86 table rows (`grep -c '^| '` = 87 with the column header); its identity note records the `lot-30381` hold / `win-6dbb766c398af77e` re-plan and the `lot-29772` collision (sections 0c, 1.2) | the hold-list file (head, `shasum`, `grep -c`) at 08:42Z |
| In flight at 07:36-07:38Z (read, moving) | stream T appended RULES amendment 13 at 07:36:04Z (`human_precision.agent_fresh_decision` narrowed: `accepted_by` local and accepted AFTER the import's pull), RULES sha `df42a3e5…` -> `738ed82a81bec20bcf732f9dc0281f077b58baf5515f8cee54a34b50b23c348d`, `r5_precision.py` `9b4c363c…` -> `020df9da…`; `counter-T-after.json` 07:36:44Z N 13, `targets_remaining` 80, lineage unchanged. Stream H re-planned the held window under the identity-reservation rule: `scene_focus.py` sha `ed9bcc4e…` (1459 lines, mtime 17:36:17 AEST; pre-H copy `scene_focus.pre-h-9ccebab4.py`), 38 tests passed, ruff clean; NEW dated plan `window-plan-2026-10-01b.jsonl` 07:37:16Z (sha `d177906d…`, targets `.run/focus-recapture/targets-held-30381.json` = `{roof_training_250-30371: [133736ada6ace260, 5d1c61a0f7ef49d9]}`, 2 lines: 1 window `win-6dbb766c398af77e`, identity `win`, flags `address_reserved_for_scene`, `reserved_by` [`roof_training_250-30381`], gate ok, 1024 px, span 78.671 m, region 413.0 px); its capture at 07:37:55Z met `GATE CLOSED: memory` three times (`swap_free_mb` 770.75, `pages_free_mb` 584-809) and stopped: NOT captured at 07:38:39Z. SUPERSEDED by section 0c: run H captured, verified, carried and pushed `win-6dbb766c398af77e` at 07:45:43Z-07:47:35Z, and stream C landed the design-sha chain, the DESIGN `identity` entry and RULES entries 14-15 at 08:21-08:28Z | `.run/focus-recapture/applied-rules-T-2026-10-01.json`, `counter-T-after.json`; `capture-2026-10-01.log:205-214`; `plan-dated-2026-10-01b.stderr`; the 2026-10-01b plan file; `shasum` at 07:38:39Z |

## 0c. Executed state: run H, the design-sha chain and the identity amendment (read 2026-10-01T08:36-08:48Z; every figure from the named file)

| item | value | source |
|---|---|---|
| Run H plan | `window-plan-2026-10-01b.jsonl`, `written_at` 2026-10-01T07:37:16Z, OFFLINE (`task focus:plan TARGETS=.run/focus-recapture/targets-held-30381.json DATE=2026-10-01b`; `targets_sha256` `ac8ac4db…` = `{roof_training_250-30371: [133736ada6ace260, 5d1c61a0f7ef49d9]}`), sha `d177906d6ca45ba440fb93aa3edc8b9fbeba62afb9146c15cfd5ae455f5dc7ac`, 2 lines = header + 1 row; `host_load` [40.64, 32.52, 32.95]; `design_sha256` `da322027…`, `window_sha256` `43538152…`; `totals` {targets 2, subjects 2, windows 1, `overlap_gate_ok` 1, `below_design_overlap` 0, `below_fraction_rule` 0, `frame_px` {1024: 1}, `lots_resolved` 1, `address_reserved_windows` 1}; `rules` {rule lot 1, registration fitted 1, identity win 1, flags `address_reserved_for_scene` 1}; `spans_m` 78.671; row `win-6dbb766c398af77e`: identity `win`, `carry_from` `roof_training_250-30371`, 1024 px, flags [`address_reserved_for_scene`], `reserved_by` [`roof_training_250-30381`], `overlap_gate` ok; a dated plan, never rewritten | the plan file (header and row, `json.loads`); `capture-2026-10-01.log:206` |
| Run H capture | `task focus:capture PLAN=…/window-plan-2026-10-01b.jsonl WINDOW=win-6dbb766c398af77e CONFIRM=1` first met `GATE CLOSED: memory` 3x at 07:37:55-07:38:35Z (`swap_free_mb` 770.75, `pages_free_mb` 584-809) and stopped (`:207-214`); the background wait from 07:39:42Z met 14 more memory closures and 1 process closure (the GitHub-runner `tsc`, pid 56439) before the gate opened at 07:45:43Z (`pages_free_mb` 2914.91; `:215-240`); captured in 5 s: verified, `overlap.fraction` 1.0, region side 413.0 px (`area_px` 170,609, `eroded_by_px` 10), shift_m 0.141, ncc 0.8494, edge margin 21.26 m, identity `win`, `frame_transform.design.design_sha256` `da322027…`, `source_displacement` recorded (`applied` false, `approx_m` 0.1049); `data/scenes/win-6dbb766c398af77e/` = latest.json, one PNG (`5e276d02…`), `models/19fdaf1d14e0f5c59087476dff6640e92aa03bd350de47c4f4a7ff4a14de6bb7.json`, review.json (17:45:46-47 AEST); the only files under `data/scenes/` newer than 17:40 AEST are these four (`find -newermt`) | `capture-2026-10-01.log:207-242`; `captures/win-6dbb766c398af77e.capture.json` (`frame_transform`); `ls -la` of the window dir; `find data/scenes -type f -newermt "2026-10-01 17:40:00"` |
| Run H verify | batch-time `scene_recapture --verify-source … --verify-target …` exit 0: `{"verified": true, source roof_training_250-30371, target win-6dbb766c398af77e, overlap_fraction 1.0, shift_px [-1.1686, -1.4153], shift_m 0.141, ncc 0.8494, samples 165649, design_sha256 da322027…}`. RE-RUN read-only at 08:48Z under the chain code (`scripts/ml/.venv/bin/python -m scripts.ml.scene_recapture --verify-source data/scenes/roof_training_250-30371/models/d4a85b0d….json --verify-target data/scenes/win-6dbb766c398af77e/models/19fdaf1d….json --root .`): exit 0, the same figures, plus `design_sha_current` `2f48e429…` and `design_sha_match` {match `design_sha_via_chain`, hops_back 1, amendment_index 4, amendment_at 08:24:41Z, amendment_field `identity`}; 0.38 s real, max RSS 183,353,344 bytes | `captures/win-6dbb766c398af77e.verify.json`; my re-run (`/usr/bin/time -l`) |
| Run H carry | `task focus:carry WINDOW=win-6dbb766c398af77e CONFIRM=1` exit 0: `review.json` revision 1 with 2 copies (`133736ada6ace260` 13.6 m², `5d1c61a0f7ef49d9` 10.78 m², hint `needs_adjustment`), `decided_on_source` [], `uncarried` [], `renamed` [], `rows` 2 appended to `windows-2026-10-01.jsonl` at 07:45:47Z (role `carried_source`, `frame_transform_verified` true, `design_sha256` `da322027…`, `window_sha256` `43538152…`; no `design_sha_match` key: the chain landed later) | `captures/win-6dbb766c398af77e.carry.json`; the registry's last 2 rows; `capture-2026-10-01.log:241`; `run-capture-h.status` "done carry_rc=0" |
| Registry at 08:42Z | 84 rows = 80 `carried_source` + 4 `uncarried_source`, 0 `withdrawn`, 49 windows, 32 source scenes, every row `frame_transform_verified` true, `design_sha256` `da322027…` (= the `previous_sha256` of DESIGN amendment 4: accepted `design_sha_via_chain`, hop 1) and `window_sha256` `43538152…`; `written_at` 06:49:37Z-07:45:47Z; sha `a0073fc30b0c68958af46e16b8fa5cc2389602f076480a8a8f8a6299f99f0288`, mtime 17:45:47 AEST (as read at 08:42Z: nothing appended since run H; stream C recorded no before / after sha of it, so "untouched by C" rests on that mtime and the 84 rows. At 08:50:42-45Z stream G appended 8 `carry_repair` rows: last row of this table) | the registry file (`json.loads` per line, `shasum`, `ls -la`) |
| Run H push | dry run `20261001T074633Z-0ea5a165` 07:46:26Z (`counts.create` 1, `labelsToCreate` 0, `mirrorCursor` 638, `bytesToUpload` 1,703,789, `status` planned) then LIVE `20261001T074717Z-cd457ee0` 07:47:13Z (`FIELDWORK_PUSH_ALLOW_LIVE=1 task push -- --scenes win-6dbb766c398af77e`; `status` pushed, `created` 1, `uploadedAssets` 3, `existingAssets` 1, `uploadedBytes` 1,703,789, `marked` 0, `markConflicts` 0, `skippedIdentical` 0, `conflictIdReuse` 0, `startedAt` 07:47:21Z, `finishedAt` 07:47:35Z); item `addr-scene-win-6dbb766c398af77e-m19fdaf1d14e0-r1`, `reviewRevision` 1, `supersedes` [], `pushedAt` 07:47:35Z; `push:status` after: items 65, markedItems 2, runs 6 (`20261001T014321Z-17b1f7fe`, `20261001T015439Z-c7cd42ab`, `20261001T070620Z-d4ab79b9`, `20261001T070709Z-5da0381e`, `20261001T074633Z-0ea5a165`, `20261001T074717Z-cd457ee0`). Both live runs together: 49 items created, 0 marks | `.run/focus-recapture/push-dry-run-h.stdout.json`, `push-live-h.stdout.json`, `push-status-h.json`; `data/fieldwork-sync/mooroolbark/push/runs/20261001T074717Z-cd457ee0/result.json` (2 item files) and `…/20261001T070709Z-5da0381e/result.json` (`created` 48, `marked` 0); `capture-2026-10-01.log:243-245` |
| Run H pull | 07:47:36Z `task pull`: `status` "conflicts" (the SAME pre-existing `addr-scene-roof_training_250-30371` item, 6 field conflicts: notes + status of `d7f7ceb84a6d0db4`, `5d1c61a0f7ef49d9`, `133736ada6ace260`, "local and Fieldwork edits overlap"), cursor 638 -> 639, `counts` {changes 1, items 35, appliedScenes 0, alreadyAppliedScenes 0, receiptOnly 0, conflicts 6}, `reviewUrls` []; `data/fieldwork-sync/mooroolbark/feedback.json` `cursor` "639" at 08:42Z | `.run/focus-recapture/pull-h.stdout.json`; `feedback.json`; `capture-2026-10-01.log:246-247` |
| Counter across H | `counter-T-after.json` 07:36:44Z -> `counter-H-after.json` 07:48:24Z: the six section 6.2 fields SAME (N 13, roof 8 / not_roof 5, 3 scenes, `targets_total` 170; re-run here); `targets_remaining` 80 -> 80 (the 2 old slices of `roof_training_250-30371` left for their 2 copies on `win-6dbb766c398af77e`); `target_exclusions` gained `recaptured:win-6dbb766c398af77e` 2 (49 keys summing 80; agent_noted 8 / decided 13 / fieldwork_pending 69 unchanged); `lineage` {registry_rows 82 -> 84, carried_copies / carried_joined 78 -> 80, every refusal 0, carried_primary 0}; `reviews_read` 248 -> 249; `rules_sha256` `738ed82a…` and `code_sha256` `020df9da…` (T) in both | the two counter files; `capture-2026-10-01.log:248-266`; my section 6.2 comparison |
| Probe H7 (why the identity entry waited) | the counter run with `--recapture-dir` on a TEMP copy of DESIGN.json carrying the drafted identity entry (real file untouched, 07:49:09Z): `lineage.design_sha256` `0301a27e…`, `carried_copies` 80, `carried_joined` 0, `carry_design_mismatch` 80, `carry_unregistered` 80, `targets_remaining` 3 (80 on the real file), `primary_rows` identical — the hop pinned the WHOLE-FILE sha, so a no-threshold amendment would have refused every carried copy; the entry stayed DRAFTED (`design-amendment-identity-2026-10-01.json`, 07:38:36Z) until the chain landed | `capture-2026-10-01.log:267-276`; `counter-H-design-probe.json` |
| Design-sha chain (stream C, files 18:21 AEST) | NEW `scripts/ml/design_chain.py` (74 lines, sha `742bac9b36bebb69f66e5d9606f70c0f5221b5eeca595c43f34a6ae46797166a`): `accepted_shas(values, current_sha)` :31 = the current sha (`design_sha_current`, hops 0) plus the `previous_sha256` of each amendment reached by walking `amendments` newest-first while `thresholds_changed` is exactly `False` and a 64-character `previous_sha256` exists (`design_sha_via_chain`, hop k, `amendment_index` 1-based, `amendment_at`, `amendment_field`); the walk stops at the first entry whose flag is not `False` (true, absent, string, 0), so a sha behind a THRESHOLD change or an unknown sha is refused; `match(accepted, sha)` :51, `tally` :57, `read(path)` :65 -> (values, sha, accepted), `(None, None, {})` for a missing file. Wired into `scene_recapture.design_sha_accepted` :666 (used by `verify` :827-833 — which also refuses when the recorded design VALUES differ beside an accepted sha — and by the verify CLI JSON line :1474-1479 `design_sha_current` / `design_sha_match`), `scene_focus.capture_window` :1166 (`design_sha_accepted` call :1172 on the plan row; records `window_frame.plan_design_sha_match` :1216) and `carry_window` :1346 (call :1371; registry row `design_sha_match` :1457) at the live scene_focus sha `563c26f2…` (re-pinned 09:24Z; the 08:42Z read `c3d50d3a…` had :1156, :1200, :1288, :1372), `r5_precision.load_lineage` :375 (`design_accepted` :384, :398), `registration` :410 (:422), `registered` :428, `carry_refusals` :439 (:452), `_hops` :465 (per-row `design_sha_match` :484), lineage summary `design_accepted` :1043 and `carried_design_sha_match` :1047. Live probe at 08:42Z (`design_chain.read` on DESIGN.json): accepted {`2f48e429…` current hop 0, `da322027…` via_chain hop 1 amendment 4 `identity`}; `29fba5a3…` (behind amendment 3, `thresholds_changed` true), `ed1ec8ac…` and `None` refused; a missing path -> `(None, None, {})` | `scripts/ml/design_chain.py`; `grep -n` of the three callers at shas `e5dccfca…`, `563c26f2…` (scene_focus, live; read at `c3d50d3a…` 08:42Z), `9c4dd2e1…` (section 1.6); my read-only probe |
| DESIGN amendment 4 (identity) | 2026-10-01T08:24:41Z, field `identity`, `thresholds_changed` false, `previous_sha256` `da322027…`, `from` = the old one-line identity text, `to` = the two-family rule (`lot-<address_id>` only for the single numbered lot of one resolved address when no `roof_training_250-<address_id>` / `house-<address_id>` scene exists and no source has that address, else `win-<hash16>`; `frame_px` 512 / 1024 by WINDOW.json `frame_px_rule`; `carried_from.role` and `frame_transform.role` `frame_cut_target`; the `windows-<date>.jsonl` registry, appended, never rewritten, rollback = `withdrawn` rows), top-level `identity` set to the same text; `recorded_collisions` {held: `lot-30381` -> `win-6dbb766c398af77e`; kept: `lot-29772`, "captured, carried and pushed 2026-10-01T06:49Z before the rule; left as it is"}; `after` names both dated plans, the 84-row registry and both push runs; `code_sha256` of the four modules; `drafted_at` 07:38:36Z, `draft_file` named; sha `da322027…` -> `2f48e4290c422de3f8c17e92fdeaa99be37255f0d4b0bd6dd85f4066445fcddb` (4 amendments; key order and the first three entries unchanged); byte copy before, `DESIGN.before-identity-2026-10-01.json`, hashes to `da322027…`; authorisation "yes to defaults" (stands; identity facts already in use, no threshold / verdict / count change) | `DESIGN.json` `amendments[3]`, `identity`; `.run/focus-recapture/applied-design-identity-2026-10-01.json` (08:24:41Z; `accepted_shas_after`, `counter_control_before`, `registry_at_write_time`, `plans_at_write_time`, `pushes_at_write_time`); `shasum` |
| RULES amendments 14-15 (chain rule) | 2026-10-01T08:28:04Z, two entries at one instant (the 06:03:14Z convention): `human_precision.join_order` (the hop accepts a transform's and a registry row's recorded design sha "under the design-sha chain rule", names `design_chain.py` and the three readers, records `design_sha_match` on the row; `carry_design_mismatch` redefined as "neither DESIGN.json's current sha256 nor accepted under the design-sha chain rule") and `human_precision.targets` (the registry-row parenthetical likewise), both `previous_sha256` `738ed82a…`, with a `code_sha256` key the earlier entries lack; the join_order entry's `reason` cites probe H7; 13 -> 15 entries, body otherwise equal; sha `738ed82a…` -> `97590a2a706f050df65c49a4da8aa46cb5e9c9cb8b3d2f84bc84e22d2adb2e92`; byte copy before, `RULES.before-chain-2026-10-01.json`, hashes to `738ed82a…`. A FIRST write at 08:27:10Z (sha `5c2e6c73…`; record kept as `applied-rules-chain-2026-10-01.first-write-withdrawn.json`) was withdrawn by restoring the byte copy and re-applied 54 s later: the only difference besides `at` is one phrase in the join_order entry's `reason` (the banned agreement-vs-… word replaced by "never a calibrated rate"); the file holds the 08:28:04Z entries only, and the banned word occurs once in RULES.json, in the pre-existing wording. `human_precision.lineage` stays null; `recapture_design.identity` STILL holds the old one-line text (a later dated entry with `previous_sha256` `97590a2a…` is needed to mirror DESIGN amendment 4; section 7.0b). `r5_precision.load_rules` :1210 accepts the file (the C counters ran) | `RULES.json` `amendments[13:15]`, `recapture_design.identity`; `.run/focus-recapture/applied-rules-chain-2026-10-01.json` and `…first-write-withdrawn.json` (entries compared key by key); `shasum`; `grep -c` of the banned word |
| Counter across C | `counter-C-before.json` 08:24:41Z (chain code `9c4dd2e1…` under the old DESIGN sha `da322027…`: `design_accepted` 1 sha; `carried_design_sha_match` transform and registry {current 80, via_chain 0, refused 0}) -> `counter-C-after.json` 08:25:16Z (DESIGN `2f48e429…`: `design_accepted` 2 shas; transform and registry {current 0, via_chain 80, refused 0}; every carried row's hop `{match design_sha_via_chain, hops_back 1, amendment_index 4, amendment_at 08:24:41Z, amendment_field identity}`) -> `counter-C-final.json` 08:28:11Z (`rules_sha256` `738ed82a…` -> `97590a2a…`): the six section 6.2 fields SAME across all three and against `counter-H-after.json` (re-run here); N 13 (roof 8 / not_roof 5, 3 scenes), `read` "n/a (N=13, roof=8, not_roof=5, scenes=3)", `targets_total` 170, `targets_remaining` 80, `target_exclusions` {agent_noted 8, decided 13, fieldwork_pending 69, `recaptured:<window>` 49 keys summing 80}; `lineage` {registry_files [windows-2026-10-01.jsonl], registry_rows 84, carried_copies 80, carried_joined 80, every `carried_refused_by_reason` 0, carried_primary 0, duplicate_decision_on_recapture 0, ambiguous_carry 0}; `carried_rows` 80 (each `needs_adjustment`, `primary` false; 49 windows, `win-3fa3eb10672b7241` with 7 copies the most); `next_scenes` 49 summing 80; `unjoined_by_reason` {under_min_area 2, not_judged_image 2, every carry refusal 0}; `code_sha256` `9c4dd2e1…` in all three; `split_file_sha256` `1ba87eed…`; `reviews_read` 249; C-after vs C-final differ only in `computed_at`, `computed_at_local`, `rules_sha256`; `/usr/bin/time -l`: 4.36 s real / max RSS 2,079,604,736 bytes (after), 5.39 s / 2,090,041,344 (final) | the three counter files and their `.stderr`; my re-run of the section 6.2 comparison on (T-after, H-after), (H-after, C-before), (C-before, C-after), (C-after, C-final): six SAME, exit 0 each |
| Tests and lint (C) | recorded by the applied-* records and the skeptic's re-run: `test_scene_recapture.py` 37 passed (26 `def test_`, parametrised), `test_scene_focus.py` 39 passed (39 `def test_`), `test_sam3_mac_precision.py` 36 passed (36 `def test_`), one process each; `ruff check` on the seven files "All checks passed!" — re-run here at 08:42Z: clean. pytest was NOT re-run by this refresh (no Python file touched) | `grep -c '^def test_'`; `ruff check` at 08:42Z; the C records |
| Scenes and frozen files at 08:42Z | `data/scenes/`: 611 dirs = 562 + 49 (`lot-*` / `win-*` 49, 7 `lot-*`), 0 `qa_*`; each window dir holds exactly ONE `models/*.json` (the capture's: no detection has run; no `detect-*` log exists under `.run/focus-recapture/`); `captures/` 294 files = 49 x 6; no file under `data/scenes/` is newer than 18:11 AEST (stream C's first write was 18:21); WINDOW.json `43538152…` unchanged (1 amendment); plans `4f198b26…` / `d177906d…` unchanged; `WINDOW-WORKLIST-2026-10-01.md` `d9b71970…` and `REMARK-WORKLIST-2026-10-01.md` `77cb0c5e…` unchanged AS READ AT 08:42Z (the worklist still lists `lot-30381` under "Windows refused or held", superseded by the hold list and this section). CORRECTION 09:24Z: the window worklist WAS rewritten after that read — mtime 09:04:37Z, 66,185 bytes, sha `e72130c5…`, notes at lines 3 and 7 (section 0b "Review surface" row); `d9b71970…` is the 07:10:06Z state only. Files with mtimes after 18:11 outside every stream's list: `src/lib/scene-visibility.ts` (18:11:15), `roof-feedback-patch.ts` (18:19:40), `scene-instances.ts` (18:11:15), `roof-line-edits.ts` (18:14:55), `scripts/test-shade-sail.mjs` (18:56:40 AEST) — no file records the reviewer as their author; their mtimes fall inside the shade-sail Codex session's window (`data/reviews/eyes/shade-sails-2026-10-01/`, files 18:00-18:59:59 AEST), whose `report.json` records `verification` {`shade_sail_regression` passed, `typescript` passed, `task_lint`, `focused_eslint`, `git_diff_check` passed} at 08:59:59Z (`Taskfile.yml:103` runs `node scripts/test-shade-sail.mjs`; `roof-feedback-patch.ts:165` names "Fabric shade sails"); `report.json` names no source file, so they are attributed to that session's window on this evidence only, never to the reviewer (corrected 09:24Z) | `ls`, `shasum`, `find -newermt` at 08:42Z; `stat` of the five files and `report.json` at 09:24Z |
| Modules at 08:42Z | `scene_focus.py` 1467 lines sha `c3d50d3add2ba63951ed0173159a2a516314a8b633f596f7f29fe06afec0dffd`; `scene_recapture.py` 1506 lines `e5dccfcac38f436b4aeea4f4a864030d52e5cbdd8004f99a9dd1f33cd6c61402`; `r5_precision.py` 1644 lines `9c4dd2e120ad985ffc6d047d6f1d9ded5eab3ba1474b153eaf59b156b065d1ca`; `design_chain.py` 74 lines `742bac9b…`; all four mtime 18:21 AEST and equal to the `code_sha256` recorded in DESIGN amendment 4 and RULES entries 14-15; `Taskfile.yml` sha `e52a6380…` (`focus:plan` :968-973, `focus:capture` :975-980, `focus:carry` :982-987, `recapture:capture` :962-966; the three `focus:*` `desc` strings name `DESIGN.json subject_recapture.min_overlap_px` as the window gate and mention the 0.6 fraction only as "the same-address rule, recorded beside it and never gating a window" / "recorded, not gating" / "never the 0.6 fraction": no edit needed). `scene_focus.py` moved again at 18:49:29 AEST (stream G, last row): 1626 lines, sha `563c26f29508cd5f…`; every scene_focus line number in this runbook is pinned to the sha it names; at 09:24Z every `c3d50d3a…` cite was RE-PINNED by symbol to the live sha `563c26f29508cd5f1b2a5230cf855ed2c615345cee6e2abdffa9798840ccbcba` (1626 lines, mtime 18:49:29 AEST) because no file on disk carries `c3d50d3a…` any more; cites of the older reads (`67afcbca…`, `9ccebab4…`, `ed9bcc4e…`) keep their labels | `shasum`, `wc -l`, `grep -n` at 08:42Z, 08:57Z and 09:24Z |
| After these reads: stream G and the shade-sail Codex session's saves (read 08:57Z; corrected 09:24Z) | `scene_focus.py` at sha `563c26f2…` (1626 lines, mtime 18:49:29 AEST; `test_scene_focus.py` 44 `def test_`, 18:49:47) adds `carry --repair-groups` (`repair_groups` :1460, `REPAIR_ROLE = 'carry_repair'` :1242; docstring :84-98): the editor's `validateSceneReview` requires every object `group_id` to name a group of the review's own `groups` array, and a window review with `groups` [] made the editor's save return 400 "Choose an existing object group."; the repair patches an EXISTING window `review.json` ONLY while it is still revision 1 and `updated_by` is the carry, adds the missing group records, keeps revision 1, holds the app's review.lock, and appends one registry row `{role carry_repair, window_scene_id, groups_added, at, previous_review_sha256, new_review_sha256}`. Run at 08:50:42-45Z on 8 windows (`lot-29772` groups [`7ddfb55acd70b9fa`, `4e615c2e23cb5015`], `lot-30300` [`d156546363202dca`], `win-03fbd418bd2874f7` (2), `win-042a73e003673806` (1), `win-738c41fc99393f59` (1), `win-a5e79c92508d190f` (2), `win-c947796546604caa` (1), `win-f94c545af3c4961a` [`2ed8fca6388e27f0`]; `dropped_pointers` [] on the one record read): their `review.json` files were REWRITTEN (mtimes 18:50:43-45) — the only writes to existing scenes by any operator stream on this path, outside this runbook's "never" list, recorded here, not judged; the registry `windows-2026-10-01.jsonl` is now 92 rows (80 `carried_source`, 4 `uncarried_source`, 8 `carry_repair`), sha `8ee2f63b642ba831…`, mtime 18:50:45. Save proof on scratch copies (`groupfix-save-proof.json`, 08:51:41Z): 4 `qa_groupfix_085141_*` scenes (`lot-29772` and `win-a5e79c92508d190f`, one `confirmed` and one `rejected` each): GET 200 revision 1 `stale` false, POST 200 revision 2, `carried_from_intact` true, scratch removed, 0 leftover `qa_*` dirs (0 at 08:57Z). `counter-G-after.json` 08:52:13Z: the six gated fields SAME against `counter-C-final.json` (re-run here); `targets_remaining` 80; `lineage.registry_rows` 84 -> 92, `carried_copies` / `carried_joined` 80 / 80, every refusal 0, `carried_design_sha_match` via_chain 80 / 80 (the hop ignores `carry_repair` rows); `rules_sha256` `97590a2a…`, `code_sha256` `9c4dd2e1…`; every carried row `needs_adjustment`, `primary` false. Then two saves the app stamped `updated_by` "local" at 08:54:38-39Z: `data/scenes/lot-29772/review.json` revision 2 (2 objects, 2 groups; both copies still `needs_adjustment`, `geometry_reviewed` false, `carried_from.role` `frame_cut_target` intact; `history[0]` revision 1 `updated_by` `scripts.ml.scene_focus carry` 08:50:42Z) and `data/scenes/house-29785/review.json` revision 2 (193 objects, 19 groups) — CORRECTED 09:24Z: NOT the reviewer's. Both were made by a Codex script of ANOTHER session, `data/reviews/eyes/shade-sails-2026-10-01/save_classifications.py` (mtime 08:53:29Z; `--apply`; POST `http://localhost:3008/api/scenes` for `lot-29772` then `house-29785`): on the two copies `7ddfb55acd70b9fa` / `4e615c2e23cb5015` in BOTH files it set `structure_kind` `shade-sail`, `label` "Fabric shade sail", `review_issues` [`outline`], `provenance` {`source` `machine:codex-shade-sail-classification`, `tier` T3, `human_confirmed` false, `reviewed_at` 08:54:38.185Z} and a note beginning "Assistant image review · Codex · 2026-10-01: Classification correction", dropped the 16 rigid-roof fields named by `report.json` `rigid_roof_fields_removed`, rings and `status` unchanged (`report.json` `local`: before_revision 1 -> saved_revision 2 for both, `polygons_unchanged` true). The SAME script then wrote LIVE to Fieldwork: `SaveItem` on `addr-scene-house-29785-me373cb0a9d78-r0` with `expectedRevision` 2 (`fieldwork-request.json`; `fieldwork-before.json` revision 2, `updatedAt` 02:20:07Z) -> revision 3, `updatedAt` 2026-10-01T08:54:40.38Z (`fieldwork-response.json`, `fieldwork-after.json`; `report.json` `fieldwork` before_revision 2, saved_revision 3) — a Fieldwork write OUTSIDE every stream of this runbook, recorded here, not judged. The counter now classes the two `lot-29772` copies `machine_label`: a fresh `r5_precision count --json --block v2` at 09:15:00Z gives `targets_remaining` 78 (`counter-G-after.json` 08:52:13Z: 80), `target_exclusions` + {`machine_label` 2} (the only key that differs; `recaptured:lot-29772` still 2), N 13 (roof 8 / not_roof 5, 3 scenes), `read` "n/a (N=13, roof=8, not_roof=5, scenes=3)", `targets_total` 170, `reviews_read` 249 SAME, `lineage` {registry_rows 92, carried_copies 80, carried_joined 80}, `rules_sha256` `97590a2a…`, `code_sha256` `9c4dd2e1…`, `split_file_sha256` `1ba87eed…` SAME. The Taskfile (`e52a6380…`, mtime 18:14:55) has no `focus:repair` wrapper | `scripts/ml/scene_focus.py` at 08:57Z (`grep -n`, docstring); `.run/focus-recapture/groupfix-<window>.json` (8), `groupfix-save-proof.json`, `counter-G-after.json`; the registry (`json.loads`, `shasum`, `ls -lT`); `find data/scenes -type f -newermt "2026-10-01 18:11:00"` (9 files); the two review.json files (read-only: `provenance`, `history`); `data/reviews/eyes/shade-sails-2026-10-01/{save_classifications.py, report.json, fieldwork-before.json, fieldwork-request.json, fieldwork-response.json, fieldwork-after.json, lot-29772-after.json, house-29785-after.json}` and `counter-G-after.json` vs a fresh `r5_precision count` at 09:15:00Z, read at 09:24Z |

## 1. Preconditions (all must hold before step 3 of section 2)

### 1.1 The re-marking batch is counted

The reviewer is re-marking Codex drafts on the old frames right now. Counter deltas only, with no attribution of any
reviewer action: the counter after the 12:39 AEST pull listed `target_exclusions` `fieldwork_pending` 149 and
`agent_noted` 11 (`docs/handover-2026-10-01.md:217-221`); the 04:13Z counter lists `fieldwork_pending` 143 and
`agent_noted` 17 (`.run/focus-recapture/counter-runbook-20261001T041307Z.json`), i.e. 149 -> 143 and 11 -> 17.
"Counted" means: the reviewer says the batch is paused, and a fresh `count --json --block v2` (what
`task eyes:small-roofs` wraps, Taskfile.yml:934-937 at repair time, :935-938 at 08:42Z; reads only) has been saved as the BEFORE snapshot for every gate
below. Either form writes valid JSON: go-task 3.53.1 (`task --version`) echoes its command line (`task: [name] ...`) to
STDERR, not stdout, so `task eyes:small-roofs > file.json` IS JSON. (An earlier revision of this runbook claimed the
opposite and that `Taskfile.yml` has no `silent:` key; both were wrong: `silent: true` is at Taskfile.yml:30 and :414,
and a scratch Taskfile under /tmp proved the echo goes to stderr: `task -t /tmp/x.yml j 2>/dev/null | python3 -c
'import json,sys; json.load(sys.stdin)'` exit 0, `task -t /tmp/x.yml j > /tmp/x.out.json` then `json.load` exit 0,
while `2>&1 >/dev/null (bash; under zsh MULTIOS prints both streams, so prefer `2>/dev/null | json.load` or `>o 2>e`)` shows only the `task: [j] ...` line.) This runbook still writes snapshots with the direct
module call, so the saved log names the interpreter and module that ran:

```
cd /Users/benebsworth/projects/addr
/Users/benebsworth/projects/addr/scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision count --json --block v2 \
    > .run/focus-recapture/counter-before-$(date -u +%Y%m%dT%H%M%SZ).json
```

Any `[reviewer] roof` / `[reviewer] not a roof` decision the reviewer already made on a frame-cut slice is a direct
decision: it counts via the P path today and WINS over any later window copy (design §4 status policy, §5
one-decision rule); that frozen key is not carried.

### 1.2 The hold list exists (design step 0; no code, no scene write) — WRITTEN at 07:46:34Z by stream H (`.run/focus-recapture/write-hold-list-h.py`), after the worklist had carried the sentence

At the 07:38Z read the file was ABSENT (`test -e`) and the hold instruction had reached the reviewer only as item 4 of
`data/references/eyes-checkpoints/WINDOW-WORKLIST-2026-10-01.md` (07:10:06Z, section 0b): "The old slices stay on their
source frames as pending Codex drafts (hold list item 2: leave them undecided there)". At the second executed-state refresh
(08:42Z) `data/reviews/eyes/recapture/frame-cut-hold-2026-10-01.md` EXISTS: written 2026-10-01T07:46:34Z by stream H's
`.run/focus-recapture/write-hold-list-h.py` (mode `x`, reads only; its last line: "this file is new and never rewritten: a
later list is a new dated file"), 27,347 bytes, sha `cee08c915652ac8e7a3ddc56d0c33e1f2fb54bb6360956de053f19df591af6b2`. Its
header reads "86 objects on 32 scenes touch their saved frame edge (scene_expand.touches_frame, 1 px;
/tmp/frame-cut-targets.json, sha256 d2136295c8a6). Leave each undecided on its OLD frame: its centred window is listed here
… A marked decision made on the old frame anyway still counts (P path) and wins over the window copy", names its inputs
(`window-plan-2026-10-01.jsonl` `4f198b26…`, `window-plan-2026-10-01b.jsonl` `d177906d…`, `windows-2026-10-01.jsonl`
`a0073fc3…` with 84 rows and 49 windows, the carry outputs under `.run/focus-recapture/captures/`) and counts "Rows:
carried_source 80 (a copy on the window asks the decision once), uncarried_source 4 (in a window whose carry_from is another
scene: not yet carried), decided_on_source 1, not_frame_cut 1, other 0"; one table row per frame-cut slice (86) with source
scene, object, window scene, carried copy id, the OLD-frame editor URL and a note (registry row time and plan, or the reason
there is no copy); its identity note records the `lot-30381` hold -> `win-6dbb766c398af77e` re-plan and the `lot-29772`
collision (section 6.4). Every one of the 80 carried copies is the asked target and its old slice is excluded
`recaptured:<window>` (counter, section 0c); the 4 `uncarried_source` keys, the 1 `decided_on_source` key and the 1
`not_frame_cut` key stay on their source frames. The generator below (this runbook's draft of 04:14Z: 86 rows and the one
sentence) was NOT the one run; it is kept as the record of what was planned.

New file `data/reviews/eyes/recapture/frame-cut-hold-2026-10-01.md` listing the 86 (scene, object, editor URL, area)
rows with the sentence "leave undecided on this frame; a centred window is coming". Never the frozen
`data/references/eyes-checkpoints/REMARK-WORKLIST-2026-10-01.md`. Generator (reads only; URL pattern is
`r5_precision.URL`, py:95 at refresh, py:98 at the executed-state refresh, :100 at sha `9c4dd2e1…`: `http://localhost:3008/scenes?scene={scene}&status=all&layer=edited&object={object}`):

```
cd /Users/benebsworth/projects/addr && scripts/ml/.venv/bin/python - <<'PY'
import json, datetime
cut = json.load(open('/tmp/frame-cut-targets.json'))            # {scene_id: [object ids]} (recompute with scene_expand.touches_frame if /tmp is gone)
URL = 'http://localhost:3008/scenes?scene={scene}&status=all&layer=edited&object={object}'
rows = sum(len(v) for v in cut.values())
out = ['# Frame-cut hold list (2026-10-01)', '',
       f'{rows} objects on {len(cut)} scenes touch their saved frame edge (scene_expand.touches_frame, 1 px). '
       'Leave each undecided on this frame; a centred window is coming (docs/focus-recapture-2026-10-01.md). '
       'A marked decision made here anyway still counts and wins over any later window copy.', '',
       '| scene | object | editor |', '|---|---|---|']
for scene in sorted(cut):
    for oid in cut[scene]:
        out.append(f'| {scene} | {oid} | {URL.format(scene=scene, object=oid)} |')
out.append(''); out.append(f'written {datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")} from /tmp/frame-cut-targets.json')
open('data/reviews/eyes/recapture/frame-cut-hold-2026-10-01.md', 'x').write('\n'.join(out))   # 'x': never overwrites
PY
```

### 1.3 Quiet window (needed ONLY for step 9, the TypeScript decision-carry wiring)

Capture, target carry and detection write only NEW directories, so they are safe beside the reviewer. The wiring edits
hot-reloaded files and waits for the window the reviewer declares in chat: no `review.json` save for 10 minutes,
reviewer confirmed idle, the MCP polygon stream idle (`provenance.source machine:mcp-polygon-draft-v1` saves make a
`review.json` mtime meaningless, handover §8 lines 163-168), a counter snapshot before, editor 200 and PRIMARY rows
unchanged after each file, revert on failure (docs/recapture-carry-2026-09-30.md:159-161).

### 1.4 Memory gate budgets, from measured sources

`.run/jobgate.sh` opens when `swap_free + pages_free >= budget + 1024 MB` and no heavy job matches its `pgrep`
pattern; it prints a header line to copy into the job log (`jobgate ... | tee` returns tee's status: test the gate's
own exit first, SKILL.md:667-668).

What is MEASURED about SAM3 scene extraction on this host:

| figure | value | source |
|---|---|---|
| House expansion batch (SAM3 on 1024 px, 10 prompts, `scene_extract --expand-context`) | 30 scenes 2026-09-29T01:01:11Z to 02:24:23Z; wall per scene median 153.5 s over the 30 intervals (the 29 gaps between consecutive `expanding` lines plus the last `expanding` to `expansion batch done`, 124 s; 155.0 s is the median of the 29 gaps alone), min 64 s (`house-29695`), max 331 s (`house-29846`); `house-29785` 157 s, `house-29772` 231 s; 2 crashes exit 134 (`recursive_mutex lock failed`, `house-29888`, `house-30235`) | `.run/expand-house-scenes.log` (the 30 `expanding` timestamps and the `expansion batch done` line; recomputed at repair time) |
| Memory of that batch | NOT recorded: `grep -E 'rss|memory|MB|GB|peak|footprint'` over `.run/expand-house-*.log` returns only shapely warnings | the logs |
| ML tile server (SAM3 per z16 tile, two threads, under load) | 43.90 to 83.01 s per tile, 2026-09-30 20:36-20:38 | `.run/ml.log` tail |
| Only `/usr/bin/time -l` max RSS figures on disk | full pytest 3,891,118,080 bytes (3,711 MB) (`.run/w4int-pytest.log:134`; SKILL.md:665 "1.49 GB footprint, 3.89 GB max RSS"); counter 706,723,840 bytes (section 0); teacher crop 1,104,117,760 (`.run/w2-crop-limit1.log:10`); rejudge 724,877,312 (`.run/w1-rejudge.log:6`) | those logs |
| Where "7500" comes from | the round-5 fine-tune check: MPS driver 6,491.7 to 7,323.7 MB at the last step of the four round-4 arms, `jobgate 7500` run read-only at 23:41Z (closed) | `.run/round5-readiness-2026-10-01.md:235-262` |

Consequence: there is NO measured RSS for a `scene_extract` run of a single 512/1024 px window. The design's 7500 MB
is the fine-tune's envelope borrowed as a conservative budget. Rule for this runbook: the FIRST window detection runs
behind `jobgate 7500` under `/usr/bin/time -l`; its "maximum resident set size" is written into the detect log and
into the next capture log (the plan row has no footprint field, `window_row` scene_focus.py:829-902 at refresh, :903 at the live sha `563c26f2…`, and a dated plan is never
rewritten); from the second window on, the budget is that measured
max RSS in MB rounded up to the next 500 MB, never below 2500 (the full-pytest budget, SKILL.md:664-667) and never
above 7500 unless measured higher. Capture-only, carry, plan and the counter run behind `jobgate 1500`
(`scene_region.capture_region` is network plus PIL on at most a 1024 px mosaic; `recapture:plan` / `recapture:pilot`
are documented at 1500, Taskfile.yml:950, :956).

### 1.5 Pre-registration (design §5-6, step 2): WINDOW.json LANDED; RULES / DESIGN entries LANDED at the executed-state refresh (A1 06:03:14Z, A2 06:11:15Z); DESIGN `identity` entry 08:24:41Z and RULES chain entries 08:28:04Z at the second refresh

Before ANY plan or capture: `data/reviews/eyes/recapture/WINDOW.json` (frame-cut-window-v1; values in design §2 and
§6 step 2), the three dated RULES `amendments` entries (fields `human_precision.join_order`,
`human_precision.population_primary`, `human_precision.targets`; exact texts in design §5; `from` re-read at write
time; `at` set at write time; the file otherwise byte-identical), and the dated DESIGN.json `identity` entry
(`win-<hash16>`, `frame_px` 512/1024, `carried_from.role frame_cut_target`, the registry file,
`thresholds_changed: false`), mirrored in RULES `recapture_design.identity`.

LANDED at refresh time: `data/reviews/eyes/recapture/WINDOW.json` exists, 7,743 bytes, sha256
`2d7c4a5bbb1bb8982b86fff15ef88aba2554eff8673ca2d7278ddb93e8dbffd4`, `written_at` 2026-10-01T05:02:09Z, `written_by`
"scripts.ml.scene_focus.write_window_rule (register): constants are the module WINDOW dict; spans are derived from
scene_region.region_frame's zoom term at write time, never typed", `version` frame-cut-window-v1, `written_before`
"any non-qa capture", `design_sha256` `29fba5a3…` (the current DESIGN.json), `frame_px` [512, 1024], `zoom_max` 20,
`provider` google, `body_distance_m` 0.7, `constants` = the 18 keys of the module `WINDOW` dict (scene_focus.py:114),
`overlap_gate` {`min_overlap_fraction` 0.6, source "DESIGN.json min_overlap_fraction"}, plus `identity`, `frame_px_rule`,
`span_rule`, `subject_rule`, `grouping`, `parcel_classes`, `acceptance` and `zoom` (native z20 0.11797905618694492 m/px,
512 px span 60.4052767677158 m, 1024 px span 120.8105535354316 m at latitude -37.79) as text. It was written by the
`register` subcommand (scene_focus.py:1135-1136, dispatch :1153-1157; `write_window_rule` :771 refuses when the file
exists, :781: "the window rule is pre-registered once and never rewritten"). `plan` refuses without it
(`load_window_rule` :786, called at :912; missing :793; constants differing from the module's `WINDOW` :796-797,
`window_rule_mismatch`) and records its sha as `window_sha256` on the header and every row; `capture` refuses a row
planned under another WINDOW.json (:1071-1074, `window_rule_mismatch`). It is now a frozen file of this path: never
rewritten; a changed rule is a new dated file and a re-plan.

At refresh time (05:34Z) the three RULES entries existed as drafted TEXT only
(`.run/focus-recapture/rules-amendment-2026-10-01.json`, `written_at` 04:22:34Z, `revised_at` 04:57:17Z). LANDED at the
executed-state refresh, before the first non-qa capture (06:49:35Z):

- RULES (A1, 2026-10-01T06:03:14Z, `.run/focus-recapture/applied-rules-2026-10-01.json`): the three drafted entries
  (`human_precision.join_order`, `population_primary`, `targets`; one full stop inserted at the seam where the draft
  concatenated old text and new sentence) plus `agent_fresh_decision` (the accepted-import route, from
  `.run/rules-amendment-accepted-imports-2026-10-01.PROPOSED.json`) and `classes_order`; 7 -> 12 amendments; sha
  `d7613249…` -> `df42a3e5…`; the body apart from those fields and `amendments` equal; byte copy before the edit
  `.run/focus-recapture/RULES.before-A1.json`. `human_precision.lineage` is STILL null: the hop text sits in the three fields
  (`join_order` names `carried_from` and the frozen key), no `lineage` key was added. The counter across A1 moved N 7 -> 13
  (section 0b) because the accepted-import route counts six earlier accepted imports on `map-3c7647c8dbada27f`; the hop itself
  moved nothing (no registry existed). At 07:36:04Z stream T appended a 13th entry narrowing that route (section 0b, last row).
- DESIGN (A2, 2026-10-01T06:11:15Z, `.run/focus-recapture/applied-design-2026-10-01.json`): the `subject_recapture` block
  (`min_overlap_px` 128; section 0b) as the third amendment, `thresholds_changed` true for subject recaptures only; sha
  `29fba5a3…` -> `da322027…`; "reduces to the frozen sha when the added block and entry are removed" true. The design §6 step
  2 `identity` entry (`win-<hash16>`, `frame_px`, `carried_from.role`) was NOT part of A2, and at 07:38Z DESIGN's `identity`
  text was still "new scene id lot-<address_id>; source untouched; forward pointer only under data/reviews/eyes/recapture/"
  (the two-family rule lived in WINDOW.json `identity` and the module). It was DRAFTED at 07:38:36Z
  (`.run/focus-recapture/design-amendment-identity-2026-10-01.json`) and APPLIED at 2026-10-01T08:24:41Z as DESIGN amendment 4
  (field `identity`, `thresholds_changed` false, `previous_sha256` `da322027…`; sha ->
  `2f48e4290c422de3f8c17e92fdeaa99be37255f0d4b0bd6dd85f4066445fcddb`, 4 amendments; section 0c) once the three readers accepted
  a recorded design sha through the dated chain (`scripts/ml/design_chain.py`): without that, probe H7 of 07:49Z showed the
  whole-file pin would have refused all 80 carried copies (`carry_design_mismatch` 80). The mirror in RULES
  `recapture_design.identity` is NOT yet written (still the old one-line text; section 7.0b).
- WINDOW.json: first `amendments` entry at the same 06:11:15Z (the overlap gate now reads `subject_recapture.min_overlap_px`;
  the pre-registered body text is unchanged); sha `2d7c4a5b…` -> `43538152…`. `plan` and `capture` compare this sha.

Reviewer decisions that gated the first capture (design §9) were covered by the reviewer's "yes to defaults" of
2026-10-01 ~15:50 AEST (quoted verbatim in both applied-* records): 1 -> option (b), the px floor (A2); 2 -> bare copies
(`carry` writes no Codex text, `CARRY_NOTE` only); 3 -> the hop and one-decision rule (A1); 4 -> the two id families; 7 ->
hold the 86 slices (carried by the worklist, section 0b); 9 -> capture-only first, then the Fieldwork push of the windows
(done, run `20261001T070709Z-5da0381e`); plus single-lot in a two-lot window. Decisions 5 (displacement: recorded, not
applied), 8 (detection) and 11 (TS wiring) stay open (section 7).

### 1.6 Code landed, light, one pytest at a time (design step 1) — landed in part at refresh time (05:34Z); landed in FULL at the executed-state refresh (the "Landed SINCE the refresh" list below)

Landed (every line number and sha read at refresh time, each symbol grepped):

- `scripts/ml/scene_region.py` (sha256 `222822c0fc410c8722c7b8b0b15edecdaae3b076f551a604dcf9b8c5c54bd7a4`): `frame_px`
  kwarg on `region_frame` (:18; 512 or 1024 only, :22-23) and `capture_region` (:68); the zoom term is `frame_px/256/span`
  (:46); the 512 px identity string is unchanged and a 1024 px frame of the same bounds hashes with `frame_px` appended
  (:60), so clicked `map-*` captures stay byte-identical; `pixel_to_meters` / `meters_to_pixel` use `frame_px` (:64-65).
- `scripts/ml/training/sam3_mac/r5_precision.py` (1577 lines; sha256 `f6496653a6382984db0243300b44ad76cd7a015525aa259f7592c17798ecf529`):
  the lineage hop. `load_lineage` :344 reads `<recapture_dir>/DESIGN.json` and `windows-*.jsonl` (`REGISTRY_GLOB` :88,
  `RECAPTURE_DIR` :87); `registered` :368; `registry_windows` :381; `carry_refusals` :387 naming `CARRY_REFUSALS` :85
  (`carry_unverified`, `carry_design_mismatch`, `carry_snapshot_mismatch`, `carry_unregistered`, `carry_method`); `_hops`
  :411; `join(..., lineage=)` :431; `_target_exclusion` :843; `_copy_item` :863; `targets(..., lineage)` :871 with the
  `recaptured:<window>` exclusion :920; `NOT_COUNTED` :86 carries `duplicate_decision_on_recapture` and `ambiguous_carry`;
  `N_RULE` :78; `--recapture-dir` on `count` / `guide` (:1526) and `rejudge` (:1541), default `data/reviews/eyes/recapture`.
  The counter's `code_sha256` is `sha256_file(__file__)` (:1027), i.e. this module's sha. It has changed twice since the
  04:13Z counter of section 0: `dca5927d91c6e16ed243fa8b9e4e4f65ad6359026b10e5dc35bc21903041e190` (`counter-before.json`
  04:08:11Z) -> `fa1b37622bc65b7cf1b2214b13a979b612e5a2d9466cd424daf04b16613518b5` (`counter-after.json` 04:21:47Z) ->
  `f6496653…` (`counter-after2.json` 04:56:44Z and the refresh counter 05:21:21Z; `shasum -a 256` at refresh agrees).
- NEW `scripts/ml/scene_focus.py` (1193 lines; sha256 `67afcbca12f717549a12d4aced32db59e670bc8c4b6029498259b7386652a8ef`
  after this refresh's wording edit, below; the module the design called `scene_window.py`, which does not exist):
  `frame-cut-window-v1` (`VERSION` :101), subcommands `register` (`--root`; :1135-1136; writes WINDOW.json once, section
  1.5), `plan` (`--root`, `--targets` or `--scene` + `--object`, `--offline`, `--out`; :1137-1143) and `capture` (`--root`,
  `--plan`, `--window`, `--allow-unverified`, `--cache-only`, `--scratch-id`; :1144-1150). No `carry`, `registry`,
  `preregister` (RULES / DESIGN amendment writer) or hold-list subcommand exists AT THIS 05:34Z READ, so step 7 and the
  remainder of step 2 were PROPOSED then (superseded: `carry` landed, the amendments ran as dated scripts; see "Landed
  SINCE the refresh" below and section 0b). Taskfile wrappers: `focus:plan` (Taskfile.yml:967-972; `TARGETS=`, `DATE=`, `[ONLINE=1]` else
  `--offline`; writes `data/reviews/eyes/recapture/window-plan-DATE.jsonl`) and `focus:capture` (:974-979; `PLAN=`,
  `WINDOW=`, `CONFIRM=1`, `[CACHE_ONLY=1]`, `[SCRATCH_ID=qa_focus_...]`); there is no `focus:register` wrapper (grep).
  Wording edit at refresh (no behaviour change): the docstring (:14-15) and `parcel_class` (:166-167) said the CP153157
  plan-block parcel (pfi 204419087) is "310-390 m across"; its bounding span measured with the module's own
  `metre_frame` (:207) + `_span` (:247) over
  `data/scene-parcels/3720833bebf70009951c235ac6006adde841a5f5862c56259f17cb1d65a41cf6.json` is 311.28 m (x 311.28,
  y 288.21; `parcel_class` -> `plan_block`), so both now say "bounding span 311.28 m in the local metre frame" with that
  source; the same sentence in `test_scene_focus.py:215-217` was changed alike.
- Tests beside them: `scripts/ml/tests/test_scene_focus.py` (29 tests by `grep -c '^def test_'`; `pytest -q` at refresh
  "29 passed in 3.27s": `frame_px` and zoom from the native z20 span :116, a plan-block or oversize parcel never the
  lot :214, the window rule pre-registered once with its sha on every row :299, the 1024 px frame :352, the window id
  :362, plan refusing an existing file :457, capture writing only the window scene :528, refusing a failed design :560
  and a below-design predicted overlap :574, the source-snapshot and scratch-id checks :587, `CacheOnlyFetcher` :603,
  CLI flags :613); `test_sam3_mac_precision.py` (31 tests by `grep -c '^def test_'` and `pytest --collect-only -q` "31
  tests collected"; the hop at :884, :901, :916, the named refusals :961, the `recaptured:` exclusion :1046, gate fields
  identical with an empty registry and without one :1173). `test_scene_region.py` (dated 22 Sep, 5 test functions, 14 collected by `pytest --collect-only -q` (parametrised)) has no
  `frame_px` / 1024 case; the 1024 px region round trip is `test_scene_recapture.py:118`.

Landed SINCE the refresh (executed-state refresh; the line numbers above are those of the 05:34Z read and have moved; cite
by symbol):

- `scripts/ml/scene_recapture.py` (1483 lines; sha `7830c4608d88f549c480c0f37eddd52b9e7ec528efcfbacf287ff08e4e7e4b3e`,
  unchanged since the A2 record): `SUBJECT_ROLE = 'frame_cut_target'` :100, `EDGE_PX` 10 :108, `MIN_SIDE_PX` 24 :109,
  `overlap_region` :288 and `overlap` :300 (the eroded region in target px), `frame_transform(..., role=)` :311,
  `verdict(transform, values, role=)` :666 (the px floor when `role` is the subject role, the fraction otherwise),
  `measured_transform(..., role=)` :697, `carry_source` :727, `verify` :759. Tests: `test_scene_recapture.py` "35 passed in
  59.39s" (A2 record; 24 `def test_`, parametrised), including the dated pilot verdicts reproduced for three pairs under the
  amended design.
- `scripts/ml/scene_focus.py` — the `carry` subcommand (A2/B; batch-time sha `9ccebab4064609d691b66a6c8ca3ebed44eec851140e3bf77b575a3b7091ec86`,
  1427 lines, `capture-2026-10-01.log:37`): `carry_window(root, window_id, *, registry=None, now=None)` writes ONLY
  `data/scenes/<window>/review.json` (revision 1, `updated_by` "scripts.ml.scene_focus carry") and appends one row per frozen
  key of the window to the dated registry (`registry_path` = `data/reviews/eyes/recapture/windows-<UTC date>.jsonl`;
  `REGISTRY_VERSION` `windows-registry-v1`). Refusals, each a `ValueError` "Window not carried: …" with NOTHING written: no
  saved window scene; a registry of `None` for a non-`qa_` scene; `carry_unverified`, `carry_design_mismatch`, `carry_method`,
  `window_frame_mismatch`, `window_rule_mismatch` (the `frame_transform` / `window_frame` of latest.json against the current
  DESIGN and WINDOW shas); `carry_snapshot_mismatch` (the source snapshot's `model_signature` / `image_sha256` no longer the
  transform's); `ambiguous_carry` (the frozen key already registered to ANOTHER window); a registry row naming this window
  while no review.json exists ("withdraw it before carrying again"); an object missing from the source snapshot; a copy
  projecting outside the frame; a carried id already used by the window model. An existing `review.json` returns
  `skipped: review_exists` (CLI exit 3); a window with nothing to carry returns `skipped: nothing_to_carry`. Per key:
  `decided_on_source(obj, revision)` = `r5_precision.classify(...)[0] in CLASSES` -> listed, not carried (source wins); a key
  whose source scene is not the window's `carry_from` -> `uncarried_source` row, no copy; else `project_object(model_obj,
  transform)`, `DROPPED_FIELDS` (`provenance`, `fieldwork_import`, `replaces`) removed, status `needs_adjustment`,
  `geometry_reviewed` false, `model.height_verified` false, `notes` = `CARRY_NOTE` ("Carried from <source> through the
  georeferenced frame as a frame-cut target: the earlier frame cut this outline at its edge; decide roof / not a roof on this
  image."), `carried_from` {scene_id, model_signature, object_id, status (hint), geometry_reviewed, iou_with_new 0, matched_id
  null, method `georeferenced-affine`, role `frame_cut_target`}, id = the source id unless the window model already uses it
  (`<id>-carried`). `read_registry` honours `withdrawn: true` rows (removes that pointer); the hop reads the same files
  (`r5_precision.REGISTRY_GLOB` :91, `load_lineage` :373). CLI: `carry --root --scene <id> [--registry <jsonl> | none]`
  (`none` allowed for `qa_*` only). Taskfile wrapper `focus:carry` (Taskfile.yml:981-986; `WINDOW=`, `CONFIRM=1`,
  `[REGISTRY=]`). At 07:37:42Z the module read 1459 lines, sha `ed9bcc4e1603d93da2178c627562e5b9f3c320070f6e876ad5d2d66bffd34d47`,
  mtime 17:36:17 AEST — stream H's identity-reservation edit (section 6.4): `RESERVED_PREFIXES` :218, `IDENTITY_RULE` :219,
  `reserved_by` :227, `window_row` identity branch :890-903, `capture_window` :1145 (design sha :1153, window sha :1157,
  scratch prefix :1158-1159, `subject_exceeds_window` :1164-1165 and :1196, overlap gate :1167, snapshot :1176,
  `source_displacement` :1181, `save_scene` :1201), `carry_window` :1256, `main` :1388-1456; it may have moved again by
  the time this is read.
- `scripts/ml/training/sam3_mac/r5_precision.py` (A1; 1597 lines at sha `9b4c363c5436b729eb32e8699982245bb94d3a8c28bafdefd8cbbd3560f5830c`,
  the batch-time `code_sha256`): `accepted_import(obj)` :215 (`fieldwork_import.pending` false with `accepted_at` and
  `accepted_by` set), `classify` :249 (`fresh_state` `accepted_import` on the no-marker path; an accepted import with
  `needs_adjustment` is undecided, never roof_part), `_target_exclusion` :872 (a decided accepted import is `decided`, not
  `agent_noted`), `load_lineage` :373, `registered` :397, `registry_windows` :410, `carry_refusals` :416, `_hops` :440,
  `join` :460, `targets` :903 (`recaptured:` :952), `count` :1076, `main` :1547 (`--recapture-dir` :1558, :1573);
  `test_sam3_mac_precision.py` 33 passed (A1 record; 34 `def test_` at 07:38Z). Stream T's 07:36Z edit moved it to
  `020df9da…` (section 0b).
- Tests at the executed-state refresh (`grep -c '^def test_'`, 07:38Z): `test_scene_focus.py` 38 (`pytest` "38 passed" in
  the H header, `capture-2026-10-01.log:205`; "32 passed in 12.89s" at A2), `test_scene_recapture.py` 24 (35 collected),
  `test_sam3_mac_precision.py` 34, `test_scene_region.py` 5. ruff clean at A1, A2 and H (the records).

Landed at the second executed-state refresh (stream C; the four modules' mtime 18:21 AEST; every line number below read by
`grep -n` at the sha named; the 07:38Z numbers above have moved):

- NEW `scripts/ml/design_chain.py` (74 lines; sha `742bac9b…`): `CURRENT` / `VIA_CHAIN` / `REFUSED` :21-23, `accepted_shas` :31,
  `match` :51, `tally` :57, `read` :65 (the rule: section 0c). Its docstring says the callers compare the recorded design VALUES
  beside the sha; only `scene_recapture.verify` does (:833 "recorded design values differ"); `r5_precision.registration` /
  `carry_refusals` and `scene_focus.capture_window` / `carry_window` accept on the sha alone, i.e. they trust the human-written
  `thresholds_changed: false` flag of the amendment they hop through (a residual, not a refutation; section 7.0b).
- `scripts/ml/scene_recapture.py` (1506 lines; sha `e5dccfca…`): `METHOD` :93, `FAILURE` :95, `SUBJECT_ROLE` :100, `EDGE_PX` :108,
  `MIN_SIDE_PX` :109, `AGREE` :111, `overlap_region` :288, `overlap` :300, `frame_transform` :311, `project_object` :357, `residual`
  :570, `load_design` :626, NEW `design_sha_accepted(design, recorded_sha)` :666, `verdict` :679, `measured_transform` :710,
  `carry_source` :740, `verify` :772 (snapshot and image hashes :779-805, affine / overlap recomputed to `AGREE` :811-817, the
  recorded design sha through the chain :827-830 else a named reason, the design values compared beside it :833, the residual
  re-measured :837-846), `main` :1447 (`--verify-source` :1450, `--verify-target` :1451; exit 1 :1468-1470; the exit-0 JSON line
  :1474-1479 now carries `design_sha256`, `design_sha_current` and `design_sha_match`).
- `scripts/ml/scene_focus.py` (1626 lines; live sha `563c26f29508cd5f1b2a5230cf855ed2c615345cee6e2abdffa9798840ccbcba`, mtime 18:49:29 AEST;
  every position below re-grepped by symbol at this sha at 09:24Z — the 08:42Z read at `c3d50d3a…` (1467 lines) exists in no file on disk
  any more): docstring :1-97 (identity and the two recorded collisions :37-46, the chain :50-52, the floor :60), `VERSION` :151,
  `PLAN_VERSION` :152, `FRAME_PX` :155, `SCRATCH_PREFIX` :156, `WINDOW_PATH` :158, `WINDOW` :164, `DISPLACEMENT` :172, `parcel_class` :212,
  `window_id` :231, `RESERVED_PREFIXES` :241, `IDENTITY_RULE` :242, `reserved_by` :248, `touched_edges` :282, `resolve_subject` :455,
  `group_subjects` :675, `write_window_rule` :845 (exists refusal :855), `load_window_rule` :860, `window_row` :903, `plan` :1000
  (`--out` refusal :1005 "a plan file is never rewritten", `load_window_rule` :1009), `CacheOnlyFetcher` :1125, `capture_window` :1166
  (plan-row design sha through the chain :1172-1176 `carry_design_mismatch`, WINDOW sha :1177-1180 `window_rule_mismatch`, scratch
  prefix :1181-1182, existing directory :1185-1186, `subject_exceeds_window` :1187-1188 and :1219, overlap gate :1190-1195, snapshot
  :1197-1199 `carry_snapshot_mismatch`, `capture_region` :1201, `measured_transform(..., role=SUBJECT_ROLE)` :1202,
  `source_displacement` :1204, `window_frame.plan_design_sha_match` :1216, unverified -> `scene_lot.UnverifiedRecapture` :1221-1222,
  `save_scene` :1224), `REGISTRY_VERSION` :1231, `CARRY_NOTE` :1232, `DROPPED_FIELDS` :1234, `REPAIR_ROLE` :1242, `registry_path` :1246,
  `read_registry` :1251, `decided_on_source` :1269, `_registry_row` :1276, `carry_groups` :1312, `carry_window` :1346 (`review_exists` :1361,
  refusals :1368-1381 with the design sha through the chain :1371-1373, `carry_snapshot_mismatch` :1385, `ambiguous_carry` :1401,
  withdraw-first :1405, `nothing_to_carry` :1435, the registry row with `design_sha_match` :1457), `repair_groups` :1460, `main` :1546
  (argparse `register` :1549, `plan` :1551, `capture` :1558-1564, `carry` :1565, `--repair-groups` :1571).
- `scripts/ml/training/sam3_mac/r5_precision.py` (1644 lines; sha `9c4dd2e1…`, the `code_sha256` of the C counters): `N_RULE` :83,
  `LINEAGE_METHOD` :89, `CARRY_REFUSALS` :90, `NOT_COUNTED` :91, `RECAPTURE_DIR` :92, `REGISTRY_GLOB` :93, `CLASSES` :96, `URL` :100,
  `sha256_file` :145, `accepted_import` :217, `classify` :251, `load_lineage` :375 (`design_chain.read` :384, `design_accepted` :398),
  NEW `registration` :410 (`design_chain.match` :422), `registered` :428, `registry_windows` :433, `carry_refusals` :439 (:452),
  `_hops` :465 (per-row `design_sha_match` :484), `join` :491, `_target_exclusion` :903, `_copy_item` :926, `targets` :934
  (`recaptured:` :983), lineage summary `design_accepted` :1043 and `carried_design_sha_match` :1047, `code_sha256 =
  sha256_file(__file__)` :1094, `count` :1111, `load_rules` :1210, `main` :1582 (`--recapture-dir` :1593, :1608).
- Tests (`grep -c '^def test_'` at 08:42Z): `test_scene_focus.py` 39 (sha `f028e71c…`), `test_scene_recapture.py` 26 (37 collected,
  parametrised; sha `e0a57a00…`), `test_sam3_mac_precision.py` 36 (sha `b5768600…`); the C records and the skeptic's re-run: 39 /
  37 / 36 passed, one process each; `ruff check` on the seven files clean (re-run at 08:42Z). Not re-run by this refresh: pytest.

Verification, one process at a time (the files that exist today; `test_scene_window.py` does not):

```
cd /Users/benebsworth/projects/addr
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_scene_region.py -q
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_scene_recapture.py -q
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_scene_focus.py -q
scripts/ml/.venv/bin/python -m pytest scripts/ml/tests/test_sam3_mac_precision.py -q
cd scripts/ml && .venv/bin/ruff check .
```

Gate after landing (counter BEFORE and AFTER with the section 6.2 fields byte-identical; no window exists, so the hop
changes nothing on the current state): recorded as `.run/focus-recapture/counter-before.json` (`computed_at`
2026-10-01T04:08:11Z, code `dca5927d…`), `counter-after.json` (04:21:47Z, code `fa1b3762…`) and, after the second code
edit, `counter-after2.json` (04:56:44Z, code `f6496653…`). The section 6.2 comparison re-run at refresh time on
(before, after), (after2, refresh 05:21:21Z) and (04:13Z, refresh): all six fields SAME, exit 0 each time;
`rules_sha256` `d7613249…` throughout; the AFTER files add the keys `lineage` (`registry_files` [], `registry_rows` 0,
every `carried_refused_by_reason` 0, `carried_primary` 0) and `carried_rows` []. Never feed a 1024 px window to
`scene_context.context_image` (refuses non-512 sources, scene_context.py:32-33 at refresh).

## 2. Commands in order, each with its gate and rollback

Conventions: run from `/Users/benebsworth/projects/addr`; `PY=scripts/ml/.venv/bin/python`; every job log starts with
the `jobgate` header line; `CONFIRM=1` is the pattern `task recapture:capture` and `task focus:capture` use
(Taskfile.yml:961-965, :974-979 at refresh; :962-966, :975-980 at 08:42Z); a dated output file is never rewritten (`scene_focus
plan` refuses an existing `--out`, scene_focus.py:909 at refresh, `plan` :1000 (`--out` refusal :1005) at the live sha `563c26f2…`; the Taskfile `plan-DATE` /
`pilot-DATE` checks refuse only when the dated file EXISTS, :952, :958 at refresh, :953, :959 at 08:42Z);
counter snapshots call the module directly so the log names the interpreter and module (section 1.1; `task
eyes:small-roofs > file` would also be valid JSON, the echo goes to stderr).

### Step 0. Hold list — section 1.2. EXECUTED 2026-10-01T07:46:34Z by stream H (`.run/focus-recapture/write-hold-list-h.py` -> `data/reviews/eyes/recapture/frame-cut-hold-2026-10-01.md`, sha `cee08c91…`, 86 rows; section 0c). Gate: none. Rollback: delete the one new file (not done; it stands).

### Step 1. Code — section 1.6. LANDED in full at the executed-state refresh: `scene_region.frame_px`, the `r5_precision` hop and accepted-import route, `scene_recapture` `SUBJECT_ROLE` / `overlap_region` / `verdict(role=)`, and `scene_focus` `register` / `plan` / `capture` / `carry` with the Taskfile wrappers `focus:plan` / `focus:capture` / `focus:carry`. Gate: tests green one at a time, ruff clean, counter fields identical (the recorded snapshots `counter-before.json` / `counter-after.json` / `counter-after2.json` pass, section 1.6; A1 moved N by its RULES text, not by code: section 0b). Rollback for further edits: saved copies of the edited modules (`cp` before editing; git cannot show the diff, section 0): `.run/focus-recapture/scene_focus.pre-a2-67afcbca.py`, `scene_recapture.pre-a2-f831b925.py`, `scene_focus.pre-b-01d84d72.py`, `scene_focus.pre-h-9ccebab4.py`, `Taskfile.pre-b.yml`, `Taskfile.pre-h.yml`, `test_scene_focus.pre-{a2,b,h}.py`, `test_scene_recapture.pre-a2.py`.

### Step 2. Pre-register — section 1.5. ALL LANDED. WINDOW.json: `$PY -m scripts.ml.scene_focus register [--root .]` ran once, 2026-10-01T05:02:09Z (a second run is refused: `write_window_rule`, "pre-registered once and never rewritten"). The RULES / DESIGN part was NOT a `preregister` subcommand (none exists; `main` defines `register`, `plan`, `capture`, `carry`): it ran as two dated, all-or-nothing scripts kept under `.run/focus-recapture/` — A1 (`applied-rules-2026-10-01.json`, RULES 7 -> 12 amendments at 06:03:14Z, body otherwise equal, byte copy `RULES.before-A1.json`) and A2 (`apply-design-amendment-2026-10-01.py` -> `applied-design-2026-10-01.json`, DESIGN 2 -> 3 amendments and the `subject_recapture` block, WINDOW.json 0 -> 1 amendments, both at 06:11:15Z). The gate below was run by both (shas before / after recorded in each file). Stream T's 13th RULES entry (07:36:04Z, `applied-rules-T-2026-10-01.json`) used the same convention. So did stream C (section 0c): DESIGN amendment 4 (`identity`, 08:24:41Z; `apply-design-identity-amendment-2026-10-01.py` -> `applied-design-identity-2026-10-01.json`, DESIGN 3 -> 4 amendments, byte copy `DESIGN.before-identity-2026-10-01.json`) and RULES entries 14-15 (08:28:04Z; `apply-rules-amendment-chain-2026-10-01.py` -> `applied-rules-chain-2026-10-01.json`, RULES 13 -> 15, byte copy `RULES.before-chain-2026-10-01.json`; a first write at 08:27:10Z, sha `5c2e6c73…`, was withdrawn by restoring that byte copy before the 08:28:04Z write — recorded in section 0c and 7.0b as a frozen-file write outside the convention). Shas at 08:42Z: DESIGN `2f48e429…` (4), RULES `97590a2a…` (15), WINDOW `43538152…` (1).

Gate (real, run before and after):

```
shasum -a 256 data/reviews/eyes/recapture/DESIGN.json data/reviews/eyes/training/sam3-mac/round5/RULES.json
$PY - <<'PY'
import json
for p in ('data/reviews/eyes/training/sam3-mac/round5/RULES.json', 'data/reviews/eyes/recapture/DESIGN.json'):
    d = json.load(open(p)); print(p, 'amendments', len(d['amendments']), 'last at', d['amendments'][-1]['at'])
PY
```

The body (every key except `amendments`) must compare equal before and after (`json.load` both copies and compare
with `amendments` popped). Rollback: none needed if the gate fails (the writer is all-or-nothing); a wrong entry is
retired by a further dated entry, never by editing (design §8).

### Step 3. Plan (Vicmap network only with `ONLINE=1`; no tiles, no SAM3) — EXECUTED: the dated plan `window-plan-2026-10-01.jsonl` was written OFFLINE at 06:43:30Z (section 0b); gate = DESIGN `subject_recapture.min_overlap_px` 128, not the 0.6 fraction

```
. .run/jobgate.sh && jobgate 1500 | tee -a .run/focus-recapture/plan-$(date -u +%F).log
task focus:plan TARGETS=/tmp/frame-cut-targets.json DATE=<date> [ONLINE=1]       # Taskfile.yml:967-972 at refresh, :968-973 at 08:42Z; --offline unless ONLINE=1
# = $PY -m scripts.ml.scene_focus plan --targets /tmp/frame-cut-targets.json [--offline] --out data/reviews/eyes/recapture/window-plan-<date>.jsonl
```

`plan` refuses an existing `--out` (scene_focus.py:909 at refresh, :1005 at the live sha `563c26f2…`: "a plan file is never rewritten") and
refuses to run without WINDOW.json (`load_window_rule` :844, called at :993 at that sha; :912 at refresh; section 1.5); it
resolves subjects offline first (`scene_lot_tags.parcels_for_scene` order, scene_lot_tags.py:219 at 08:42Z) and, without
`--offline`, makes ONE bounded Vicmap read per unresolved window frozen as a NEW file under `data/reviews/eyes/parcels/`
(`scene_lot_tags.fetch_frozen`, scene_lot_tags.py:212);
`--offline` reads frozen parcels only. It writes one header row then one row per window (`window_row`,
scene_focus.py:829-902 at the 05:34Z read, :903 at the live sha `563c26f2…`): `window_id`, `identity` (`lot` / `win`), `carry_from`, the window (`centre`, `span_m`, `zoom`,
`frame_px`, `m_per_px`, `native_m_per_px`, `bounds`, `centre_shift_m`, `spatial_group`, `split`), `sources` with their
object ids, lots and shares, `subjects` (slice plus body), grouping, `covered_by_existing`, `uncarried_sources`, the
PREDICTED overlap per source frame (`scene_recapture.frame_transform(..., role='frame_cut_target')` is pure) summarised as
`overlap_gate`: since the A2 amendment (06:11:15Z) a row is `ok` when the PREDICTED eroded overlap REGION of the `carry_from`
frame in the planned window (the source footprint inside the frame, eroded by `EDGE_PX` 10 as `residual()` erodes its cover
mask; the smaller side of its bounding box, in TARGET px, `predicted_region_px`) reaches DESIGN `subject_recapture.min_overlap_px`
128, else `below_design` and never captured; the source-pixel FRACTION is still computed and recorded beside it
(`overlap_gate_detail` {`fraction`, `passes_fraction_rule`, `passes_px_floor`, `region_side_px`, `region_area_px`, `rule`}) but
does not gate a subject window (the 0.6 fraction remains the same-address rule of `scene_lot --carry-from` and the pilot
pairs). A design without the `subject_recapture` block refuses the plan (`scene_focus.py` docstring :54-58 at the
executed-state refresh, :60 at the live sha `563c26f2…` — "A design without the floor refuses the plan"). Then `dedupe` (`new` / `exists_skip`), `flags`, `reserved_by` (stream H, section 6.4),
`design_sha256` and `window_sha256`; the header carries `host_load`, `targets_sha256`, `design_sha256`, `window_sha256`,
`window_path`, `offline`, `totals` (now also `below_fraction_rule`, `passes_px_floor_not_fraction`, `overlap_gate_ok`,
`address_reserved_windows`), `rules`, `skipped`, `spans_m` and `overlap_gate` {rule, `min_overlap_px`, `eroded_by_px`,
`min_overlap_fraction` recorded}. The cohort displacement note is NOT a header field (no key of the dated header matches
`displac`/`cohort`/`note`; it is written per capture into `frame_transform.source_displacement`, section 5). There is no
`detection_footprint` field and a dated plan is never rewritten: the step 8 footprint goes into the detect log (section 1.4).

DRY RUNS before the dated plan, all OFFLINE, all under `.run/`; the first three with `targets_sha256` `d2136295…` (=
`shasum -a 256 /tmp/frame-cut-targets.json`) and `design_sha256` `29fba5a3…` (the 0.6 gate); the fourth (v3) under the
amended design; none is the dated `data/` plan:

- `.run/focus-recapture/plan-2026-10-01.run1.jsonl` (04:28:01Z; 38 windows, `below_design_overlap` 20) and
  `plan-2026-10-01.jsonl` (04:31:00Z; `host_load` [98.85, 99.04, 112.39]; 43 lines = header + 42 windows; `totals`
  targets 86, subjects 85, windows 42, `frame_px` {1024: 24, 512: 18}, identity {lot 3, win 39}, `below_design_overlap`
  23, `uncarried_targets` 7, `unresolved_lot` 34; `spans_m` 39.999 / 68.239 / 120.006; flags `lot_exceeds_window` 2):
  the FIRST plan, written BEFORE WINDOW.json existed (`window_sha256` null on its header), so `capture` now refuses
  every row of it (`window_rule_mismatch`; scene_focus.py:1071-1074 then, `capture_window` :1177-1180 at the live sha `563c26f2…`). It let the CP153157 plan-block parcel act as a lot
  (`win-d73376483fb1949f`; `parcel_class`, scene_focus.py:166-167 then, :212 at the live sha `563c26f2…`); superseded.
- `.run/focus-recapture/plan-2026-10-01.v2.jsonl`, the CURRENT dry run (`written_at` 2026-10-01T05:02:27Z, 18 s after
  WINDOW.json; `host_load` [101.21, 144.53, 142.75]; `window_sha256` `2d7c4a5b…`; 50 lines = header + 49 windows; its
  `.stderr` "real 9.55 user 5.63 sys 0.45"). Its `totals`: targets 86, subjects 85, windows 49, `captures_after_dedupe`
  49, `frame_px` {1024: 29, 512: 20}, zoom 20 for all 49, identity {lot 8, win 41}, `below_design_overlap` 29,
  `uncarried_targets` 4, `unresolved_lot` 37, `lots_resolved` 12, `plan_block_subjects` 3, `plan_block_as_lot` 0,
  `multi_lot_windows` 3, `multi_lot_outside_two_lot_rule` 0, `kept_apart_subjects` 10, `exists_skip` 0,
  `duplicate_in_plan` 0; `rules` {rule: lot 12 / subject 28 / subject_no_body 9; registration: fitted 23 / region 29};
  `skipped` one target as `not_frame_cut` (`map-806de95f666a01c7` / `815d84c355fa7b73`); `spans_m` min 32.479, median
  69.174, max 120.004; flags `plan_block` 3 (no `lot_exceeds_window` row). Its eight `lot` identities and their
  `carry_from`: `lot-29772` (house-29785), `lot-30300` (map-6d0b154271f7d6a6), `lot-25647` and `lot-25641`
  (roof_training_250-25598), `lot-29385` and `lot-29358` (roof_training_250-29353), `lot-30381` (roof_training_250-30371),
  `lot-30372` (roof_training_250-30381): each is a NEIGHBOUR's address, none the carry source's own; note that
  `lot-30381` is also the address of cohort scene `roof_training_250-30381`, whose own-lot recapture
  (`task recapture:capture ADDRESS=30381`) would want the same directory name (read from the plan, not reconciled here).

- `.run/focus-recapture/plan-2026-10-01.v3.jsonl` (`written_at` 2026-10-01T06:14:38Z, 3 min after A2; sha `fd30a62e…`;
  `design_sha256` `da322027…`, `window_sha256` `43538152…`; `targets_sha256` `b2cf02e5…` = the reconstructed
  `frame-cut-targets-2026-10-01.json`, because `/tmp/frame-cut-targets.json` was gone at that moment; `host_load` [321.4,
  166.4, 136.7]; 15.38 s real, max RSS 236,666,880 bytes): the SAME 49 window ids as v2 and the same `kept_apart_subjects`
  10; `gates` {px_floor_ok 49, px_floor_below_design 0, fraction_rule_0_6_pass 20, fraction_rule_0_6_fail 29,
  pass_px_floor_fail_fraction 29, fail_px_floor_pass_fraction 0, fail_both 0}; predicted `region_side_px` min 161.2
  (`lot-29385`, 1.26x the floor) / median 334.5 / max 625.5. Versus v2: 29 `gate_changes`, every one `below_design` ->
  `ok`; nothing else moved (`applied-design-2026-10-01.json` `plan_v3`, `versus_v2`).

The dated `data/` plan WAS written at 06:43:30Z — OFFLINE, not with `ONLINE=1` (the `--offline` flag is on the recorded
command line, `.run/focus-recapture/plan-dated-2026-10-01.stderr`; the 37 `unresolved_lot` windows stay subject-centred
with lot unknown, design §9 decision 10 as the default): `data/reviews/eyes/recapture/window-plan-2026-10-01.jsonl`,
sha `4f198b26…`, header + 49 rows, `targets_sha256` `d2136295…` (the /tmp file was back), `host_load` [73.36, 74.02,
101.9]; its header `totals` equal v3's, row order differs (targets dict order), the same 49 ids, row-level differences
beyond `planned_at` none (`capture-2026-10-01.log:7-16`); `overlap_gate` ok on all 49, `dedupe` new on all 49. Steps 4-5
took it as `PLAN=`. A second dated plan, `window-plan-2026-10-01b.jsonl` (07:37:16Z), holds only the re-planned
`lot-30381` group as `win-6dbb766c398af77e`, captured, verified, carried and pushed in run H at 07:45:43Z-07:47:35Z (sections
0c, 6.4). A dated plan is never rewritten.

Reference for how a lot-frame row looks (REAL, but seeded from the scene's ADDRESS, not the object, so the WRONG subject
for frame-cut targets): read the frozen `data/reviews/eyes/recapture/plan-2026-09-30.jsonl` (sha `f476448f…`, section
0). Do NOT run `task recapture:plan` for this: its guard refuses only when the dated file EXISTS (Taskfile.yml:952 at refresh, :953 at 08:42Z), and
`plan-2026-10-01.jsonl` does not exist under `data/reviews/eyes/recapture/`, so the command would CREATE a new dated plan
with the address-seeded subject.

Gate: a row whose `overlap_gate` is `below_design` is NOT captured (`capture` refuses it unless `--allow-unverified`;
`capture_window` overlap check). Design §9 decision 1 is RESOLVED as option (b): the dated DESIGN entry of 06:11:15Z
(`subject_recapture.min_overlap_px` 128, section 0b and 1.5), authorised by "yes to defaults" item 5; no `centre_shift_m`
(option a) was used and no capture ran unverified (option c). Under the floor every one of the 49 rows is `ok` (29 of them
below the 0.6 fraction). The `mooroolbark-50011498-17-118442-80416` target's row `win-f94c545af3c4961a` (1024 px, span
85.913 m; predicted fraction 0.028, bounded by (85.913 / 271.827)^2 = 0.100 against its 271.827 m source frame) passed the
floor and was CAPTURED at 06:56:02Z: measured fraction 0.028104, eroded region side 461.8 px, shift 0.0075 m, ncc 0.8819,
verified, 1 copy carried (`capture-2026-10-01.log:101`). Rollback: the plan file is a record; frozen Vicmap files stay
(they are inputs, named by key).

### Step 4. Rehearsal on a `qa_focus_*` scene (one group, e.g. `map-3c7647c8dbada27f`'s north slices) — EXECUTED (capture + carry + headless checks, 06:44Z, scratch removed)

```
. .run/jobgate.sh && jobgate 1500 | tee -a .run/focus-recapture/rehearsal.log
task focus:capture PLAN=<plan jsonl> WINDOW=<window_id> CONFIRM=1 CACHE_ONLY=1 SCRATCH_ID=qa_focus_$(date +%s)   # Taskfile.yml:974-979 at refresh, :975-980 at 08:42Z
# = $PY -m scripts.ml.scene_focus capture --plan <plan> --window <window_id> --cache-only --scratch-id qa_focus_<ts>
```

`--scratch-id` must start with `qa_` (`SCRATCH_PREFIX`, scene_focus.py:106 and :1075-1076 at refresh; :156 and `capture_window` :1181-1182 at the live sha `563c26f2…`) and the planned id is recorded as
`planned_window_id`; `--cache-only` serves tiles from `data/imagery` and refuses on a miss (`CacheOnlyFetcher` :1021 at refresh, :1125 at the live sha `563c26f2…`). In
both dry runs the `map-3c7647c8dbada27f` group is `win-2076df0d53bc99b2` (3 targets, 1024 px, span 62.044 m,
`overlap_gate` `ok`, predicted overlap 0.885). One rehearsal capture is already recorded:
`.run/focus-recapture/rehearsal-qa_focus_3c7647c8_north.json` (file mtime 14:31 AEST = 04:31Z, i.e. from the FIRST plan,
before WINDOW.json existed; `captured` `qa_focus_3c7647c8_north`, `frame_transform.verified` true, `overlap.fraction`
0.88494, cache hits 9 / misses 0); no `data/scenes/qa_*` directory exists at refresh time (562 scene dirs;
`ls -d data/scenes/qa_*` matches nothing), so that scratch scene was removed. A new rehearsal must read a row of
the dated `data/` plan: rows of the first plan carry no `window_sha256` and `capture` refuses them. The carry onto the
scratch scene is REAL: `$PY -m scripts.ml.scene_focus carry --scene qa_focus_<ts> --registry none` (`none` is accepted for
a `qa_*` id only; a non-scratch scene without a registry is refused).

EXECUTED rehearsal B (`capture-2026-10-01.log:17-28`; `rehearsal-b-qa_focus_b_1790837065.{capture,carry,api}.json`):
`qa_focus_b_1790837065` from the dated plan's `win-2076df0d53bc99b2` row, cache-only: capture exit 0, verified, overlap
fraction 0.88494, region side 505.7 px, shift 0.0002 m, ncc 0.9991, cache 9 hits / 0 misses; `carry --registry none` exit
0: `review.json` revision 1 with 2 copies (`4e9ac665bfacf31f` 9.29 m², `511d1fdbfb1ec4b5` 4.6 m², hint `needs_adjustment`),
`995244b3e1216ea7` listed `decided_on_source` (status confirmed via the accepted-import route: source wins), `rows` 0,
`registry_path` None; editor GET 200; `/api/scenes` keys `address_names, image_url, model, recovery, review, stale`, review
revision 1, `stale` false, `recovery` None, each copy `needs_adjustment`, `geometry_reviewed` false, `carried_from` present
with the lead sentence; scripted POST save 200, revision 2, `carried_from` + `role` kept; `rm -rf` of the scratch dir,
`qa_*` dirs 0 afterwards. The counter BEFORE / AFTER pair for the rehearsal is `counter-B-before.json` (06:38:45Z) against
itself: the batch followed within two minutes, so no separate post-rehearsal snapshot was written (the RULES skip `qa_*`
and the first batch counter, 07:05:46Z, kept every section 6.2 field; section 0b).

Real checks on the scratch scene:

```
$PY -m scripts.ml.scene_recapture --verify-source data/scenes/map-3c7647c8dbada27f/models/<review model_signature>.json \
    --verify-target data/scenes/qa_focus_<ts>/models/<model_signature>.json --root .      # exit 0 and a JSON line, or exit 1 "Recaptured frame does not agree with the source frame"
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:3008/scenes?scene=qa_focus_<ts>&status=all"   # 200 (handover §1: the detached next dev on :3008)
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:3008/api/scenes?id=qa_focus_<ts>"            # 200; loadScene must return stale false (review.model_signature == model.model_signature, scene-store.ts:79)
```

Scripted save: `POST /api/scenes` with `{id, review}` (src/app/api/scenes/route.ts:27-32 calls
`saveScene(body.id, body.review, principal)`); the review body is the scratch `review.json` with one carried copy set
to `confirmed` and `revision` 2; the response must be 200 and the saved object must keep `carried_from` and `role`
(validateSceneReview spreads `{...o}`, scene-store.ts:400-409). Then the counter AFTER, written with the direct command
`/Users/benebsworth/projects/addr/scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision count --json --block v2 > .run/focus-recapture/counter-rehearsal-after-<ts>.json`:
the section 6.2 fields identical to BEFORE (RULES skip `qa_*` scenes) and the hop rows listed as `undecided` / unjoined
with their named reason. Remove the scratch scene: `rm -rf data/scenes/qa_focus_<ts>`; confirm `ls -d data/scenes/qa_* `
matches nothing. Gate for the real captures: every check above green. Rollback: the `rm -rf` above; nothing else was
written.

### Step 5. Capture-only per subject (tiles, no detector; one window at a time) — EXECUTED: 48 of 49 captured 06:49:35Z-06:57:30Z in run B, 1 held (`lot-30381`; section 0b); the 49th, re-planned as `win-6dbb766c398af77e`, captured 07:45:43Z in run H (section 0c); 0 refused, 0 verify failures

```
. .run/jobgate.sh && jobgate 1500 | tee -a .run/focus-recapture/capture-<window>.log
task focus:capture PLAN=data/reviews/eyes/recapture/window-plan-<date>.jsonl WINDOW=<window_id> CONFIRM=1    # Taskfile.yml:974-979 at refresh, :975-980 at 08:42Z
# = $PY -m scripts.ml.scene_focus capture --plan <plan> --window <window_id>     (flags: --root, --plan, --window, --allow-unverified, --cache-only, --scratch-id; argparse `capture` scene_focus.py:1144-1150 at refresh, :1558-1564 at the live sha `563c26f2…`)
```

The carry source is the plan row's `carry_from` (there is no `--carry-from` flag; `task recapture:capture` still
hard-wires `--carry-from roof_training_250-ADDRESS`, Taskfile.yml:965 at refresh, :966 at 08:42Z, the wrong subject here). Behaviour
(`capture_window`, scene_focus.py:1062-1115 at refresh; :1166-1224 at the live sha `563c26f2…`, re-grepped 09:24Z — the numbers below are
the current ones): a row planned under a DESIGN.json sha that is neither the current one nor accepted through the dated chain is
refused (:1172-1176, `carry_design_mismatch`; `scene_recapture.design_sha_accepted`, section 0c — the 49 rows planned under
`da322027…` are accepted `design_sha_via_chain` today) and so is one planned under another WINDOW.json sha (:1177-1180,
`window_rule_mismatch`); a scratch id must start with `qa_` (:1181-1182); an existing directory of the window id is refused
(:1185-1186; the plan marks it `exists_skip` and the batch skips it); a `subject_exceeds_window` row is refused (:1187-1188, and
:1219 after the measurement); a row whose `overlap_gate` is not `ok` is refused unless `--allow-unverified` (:1190-1195, an
`UnverifiedRecapture` naming the region side and the floor); the source snapshot the row names is read and must still be the
row's `source_model_signature` (:1197-1199, `carry_snapshot_mismatch`); `capture_region(requested_bounds, 'google',
frame_px=512|1024)` (:1201); `scene_recapture.measured_transform(..., role=SUBJECT_ROLE)` (:1202; scene_recapture.py:710) under
DESIGN.json; a failed `verdict` (scene_recapture.py:679) raises `scene_lot.UnverifiedRecapture` (:1221-1222; scene_lot.py:77), the
CLI prints the stable sentence and exits 1, and NOTHING is saved; for a fitted cohort source `frame_transform.source_displacement`
is recorded (:1204; section 5); the accepted plan-row hop is recorded as `window_frame.plan_design_sha_match` (:1216); on success
`scene_extract.save_scene` (scene_extract.py:327) writes the new `lot-<address_id>` / `win-<hash16>` directory with
`frame_transform` and `window_frame` (:1224). `--allow-unverified` keeps a failed frame with `verified` false: no carry can use it; never used
on this path.

Real analogue for an OWN-lot recapture only (not for frame-cut targets): `task recapture:capture ADDRESS=<address_id> CONFIRM=1`
= `$PY -m scripts.ml.scene_lot --address <id> --capture --carry-from roof_training_250-<id>`; flags grepped from
scene_lot.py:331-342 at repair time (:331-341 at 08:42Z): `--root`, `--address` (int, required), `--source-scene`, `--group auto|lot|plan-block`,
`--offline`, `--dry-run`, `--capture`, `--carry-from`, `--allow-unverified` (keeps a failed frame with `verified`
false: no carry can use it; never used on this path).

Gate: section 1.5 complete (it was, A1 06:03Z and A2 06:11Z); plan row `overlap_gate` `ok` under the px floor (decision 1
is resolved as option b; every row of the dated plan is `ok`); jobgate 1500 open; order = the plan's row order with the
batch driver's `HOLD` list applied (`run-captures-b.sh`: `HOLD=(lot-30381)`; an existing directory is `exists_skip`; a
refused capture is logged and the loop continues; a failed verify removes the new directory). Rollback when a capture
fails: nothing was saved (the refusal is before `save_scene`); when a capture succeeded but must be withdrawn before step 7
ran: `rm -rf data/scenes/<new id>` (the registry has no row yet). The 32 source scenes and `house-29772` were never written
(integrity 07:10:44Z, section 0b). As executed: 48 captures in run B, each 1-9 s, plus `win-6dbb766c398af77e` in run H (5 s, after 3 + 14 memory closures and 1
process closure of `jobgate 1500`; section 0c) = 49, every `frame_transform.verified` true; 22 fitted cohort sources carry
`source_displacement` (`applied` false): 21 in run B (`capture-2026-10-01.log:154-156`) + the H window
(`captures/win-6dbb766c398af77e.capture.json`); the per-window stdout / stderr are
`.run/focus-recapture/captures/<window>.capture.{json,stderr}` (49 x 6 = 294 files with the verify and carry outputs).

### Step 6. Frame verification (real, read-only) — EXECUTED for all 49 windows: exit 0 and a JSON line each (`captures/<window>.verify.json`; `capture-2026-10-01.log:151` "verify exit-0 JSON verified: 48" for run B; `captures/win-6dbb766c398af77e.verify.json` for run H: verified true, shift_m 0.141, ncc 0.8494, samples 165,649; re-run read-only at 08:48Z with the same figures plus `design_sha_match` via_chain, section 0c)

```
$PY -m scripts.ml.scene_recapture --verify-source data/scenes/<source>/models/<review model_signature>.json \
    --verify-target data/scenes/<window>/models/<model_signature>.json --root . | tee -a .run/focus-recapture/capture-<window>.log
```

Exit 0 prints `{"verified": true, source_scene_id, target_scene_id, overlap_fraction, shift_px, shift_m, ncc, samples,
design_sha256, design_sha_current, design_sha_match}` (`main`, scene_recapture.py:1474-1479 at sha `e5dccfca…`; the last two keys
are new with the chain; py:1369-1377 at the executed-state refresh printed up to `design_sha256`); exit 1 prints the stable
sentence and `reason:` lines (:1468-1470; py:1370-1371 then). `verify` (:772) re-hashes both snapshots and images (:779-805),
recomputes `metre_offset` / `pixel_affine` / overlap to `AGREE` 1e-6 (:811-817; py:721-725 then), accepts the recorded design sha
when it is the current one or is reached through the dated chain (:827-830, else a named reason; py:735 then compared by equality)
AND compares the recorded design values beside it (:833 "recorded design values differ"), then re-measures the residual
(:837-846). The
`--verify-source` snapshot MUST be the one `carried_from.model_signature` names (the review's `models/<sig>.json` when
present, else latest; `carry_source`). Gate: exit 0. Rollback on exit 1: treat as step 5's failure (remove the new
directory; no registry row yet).

### Step 7. Target carry and registry row (right after step 6; writes ONLY the new window's `review.json` and appends one registry row per target) — LANDED (`scene_focus carry`, `task focus:carry`) and EXECUTED for all 49 windows (48 in run B, 1 in run H)

```
task focus:carry WINDOW=<window_id> CONFIRM=1 [REGISTRY=<jsonl>]        # Taskfile.yml:981-986 at refresh, :982-987 at 08:42Z
# = $PY -m scripts.ml.scene_focus carry --scene <window_id> [--registry <jsonl>]   (default registry: data/reviews/eyes/recapture/windows-<UTC date>.jsonl; 'none' for qa_* only)
```

Behaviour (`carry_window`, scene_focus.py; symbols and refusals listed in section 1.6): per frozen target of the window's
own `carry_from` scene, `project_object(model_obj, frame_transform)` (status untouched by projection) then DESIGN
`status_policy` (`needs_adjustment`, `geometry_reviewed` false, `model.height_verified` false), one lead sentence in `notes`
(`CARRY_NOTE`: "Carried from <source> through the georeferenced frame as a frame-cut target: the earlier frame cut this
outline at its edge; decide roof / not a roof on this image."), `carried_from` = {scene_id, model_signature, object_id,
status (hint), geometry_reviewed, iou_with_new 0, matched_id null, method `georeferenced-affine`, role `frame_cut_target`};
`provenance`, `fieldwork_import` and `replaces` dropped; `revision` 1; `model_signature` = the window's latest.json;
`updated_by` "scripts.ml.scene_focus carry". A frozen key the counter already classifies roof / not_roof on its source
(`decided_on_source` -> `r5_precision.classify`) is listed and NOT carried (source wins); a key whose source scene is not the
window's `carry_from` gets an `uncarried_source` row and no copy. Skip when `review.json` exists (exit 3). Registry row
(append, dated file): `{version windows-registry-v1, source_scene_id, object_id, window_scene_id, carried_object_id, role
carried_source|uncarried_source, frame_transform_verified, design_sha256, window_sha256, source_model_signature,
source_image_sha256, written_at}`; a second window for the same frozen key is refused (`ambiguous_carry`); a key with a
registry row naming this window but no `review.json` is refused until withdrawn.

Gate: step 6 exit 0; `frame_transform.verified` true; the transform's `design_sha256` is the current DESIGN.json sha or is
accepted through the dated amendment chain (`carry_window` :1346, its `design_sha_accepted` call :1371 at the live sha `563c26f2…`, else
`carry_design_mismatch` :1290; the hop is written on the registry row as `design_sha_match` :1372 — the 84 rows written before
the chain landed carry no such key; the WINDOW sha is compared exactly :1296; before 08:21Z the DESIGN sha was compared by
equality); no `review.json` in the window directory. Rollback: `rm
data/scenes/<window>/review.json` (a new scene's file only) and APPEND a `{..., withdrawn: true, written_at}` row naming the
window (`read_registry` and `load_lineage` drop that pointer); the dated registry file is never rewritten (design §8). Note
the executed rows carry `role` `carried_source` / `uncarried_source` and the withdrawal marker is the `withdrawn` key, not a
`role: 'withdrawn'` value as this step first drafted.

EXECUTED (section 0b): 48 carries, exit 0 each, 06:49:37Z-06:57:32Z; `data/reviews/eyes/recapture/windows-2026-10-01.jsonl`
82 rows (78 `carried_source`, 4 `uncarried_source`), sha `1eb6dee8…`, 0 withdrawn, no frozen key twice; 1 `decided_on_source`
(`win-2076df0d53bc99b2` / `995244b3e1216ea7`); `renamed` [] (the model layer is empty, so every copy keeps its source id);
the per-window carry outputs are `captures/<window>.carry.json`. The hop then read the registry: counter `lineage`
{registry_rows 82, carried_copies 78, carried_joined 78, every refusal 0, carried_primary 0} and 78 `recaptured:<window>`
exclusions (`counter-B-after.json`). Every carried copy is `carried_undecided` at 07:11:06Z: no reviewer decision on a
window existed when this was read, and none is asserted here. Run H (section 0c): the 49th carry at 07:45:47Z appended 2
`carried_source` rows for `win-6dbb766c398af77e`; the registry at 08:42Z: 84 rows (80 `carried_source`, 4 `uncarried_source`), sha
`a0073fc3…`, 0 withdrawn, no frozen key twice, every row `design_sha256` `da322027…` (accepted `design_sha_via_chain` since
08:24:41Z); counter `lineage` {registry_rows 84, carried_copies 80, carried_joined 80, every refusal 0, carried_primary 0} and 80
`recaptured:<window>` exclusions over 49 keys (`counter-C-after.json`, 08:25:16Z). Every carried copy is still `needs_adjustment`
/ `primary` false at 08:25:16Z: no reviewer decision on a window is recorded in any counter read here, and none is asserted.

### Step 8. Detection (optional for the count; one window; behind the gate; never beside a web build) — NOT RUN at the executed-state refresh: every window's model layer is empty (`model objects 0`, `capture-2026-10-01.log:53`, `:189`; the worklist says "No detector has run on the window yet"); `jobgate 7500` was not probed during run B (the batch ran behind 1500 and met memory closures even there, section 0b). Still NOT RUN at 08:42Z: each of the 49 window dirs holds exactly one `models/*.json` (the capture's) and no `detect-*` log exists under `.run/focus-recapture/`; run H alone met 17 `jobgate 1500` closures (section 0c)

```
. .run/jobgate.sh && jobgate 7500 | tee -a .run/focus-recapture/detect-<window>.log; test "${pipestatus[1]}" = 0 || exit 1    # zsh: jobgate's own status, not tee's
pgrep -fl "next build|scene_extract|finetune_mps|hires_teacher" ; curl -s 127.0.0.1:18090/stats                        # nothing heavy; tile server queue 0
/usr/bin/time -l $PY -m scripts.ml.scene_extract --scene-id <window id> 2>&1 | tee -a .run/focus-recapture/detect-<window>.log
```

`--scene-id` loads `data/scenes/<id>/latest.json`, requires `region` (window scenes have it, as every
`capture_region` scene does; scene_extract.py:456-463), runs `analyse_scene` with the default `--classes` (PROMPTS)
and `save_scene` (py:475-479); the lock `data/scenes/.extraction.lock` exits 75 when another extraction runs
(py:435-438). Optional flags grepped from py:374-390: `--classes`, `--prompt`, `--roof-models`, `--detector-only`,
`--provider`, `--out-root`. Write the `maximum resident set size` line into the detect log and the next capture log
and use it for the next budget (section 1.4; the plan row has no footprint field and a dated plan is never rewritten).

Ordering with step 7: `save_scene` recomputes `model_signature` (py:327-343), so a revision-1 carry written BEFORE
detection reads stale afterwards (identical image; same-image recovery applies). Prefer detection BEFORE the carry
when the gate opens the same day; else carry first and accept the recovery. Rollback: detection adds a
`models/<new sig>.json` and rewrites the window's latest.json only (a new scene); source scenes untouched. A crash
(exit 134 `recursive_mutex`, seen twice in the September batch) leaves the pre-detection latest.json in place.

### Step 9. Review carry preview (read-only) and the deferred wiring

Read-only preview of what the DECISION carry (B) would produce for one window, printing to stdout and writing
nothing (real functions: `carry_source`, `frame_transform`, `project_object`, `verdict`):

```
$PY - <<'PY'
import json
from scripts.ml.scene_recapture import carry_source, frame_transform, project_object, load_design, verdict
src, _ = carry_source('.', '<source scene id>'); win = json.load(open('data/scenes/<window id>/latest.json'))
ft = win.get('frame_transform') or frame_transform(src, win)
print('verified', ft.get('verified'), 'overlap', ft['overlap']['fraction'], 'verdict', verdict(ft, load_design('.')[0]))
rev = json.load(open('data/scenes/<source scene id>/review.json'))
for o in rev['objects']:
    if o.get('status') in ('confirmed', 'rejected', 'needs_adjustment'):
        p = project_object(o, ft); print(o['id'], o['status'], 'ring in window', [[round(x, 1), round(y, 1)] for x, y in p['ring']][:4], '...')
PY
node scripts/test-scene-recapture.mjs          # the TS module on fixtures in a tmpdir; writes nothing under data/
```

The wiring itself (design step 8; docs/recapture-carry-2026-09-30.md §6 lines 135-157) waits for the quiet window of
section 1.3 and is not on the count's critical chain. `task verify:scene-recapture` (Taskfile.yml:996-999;
`scripts/verify-scene-recapture.mjs`, env `RECAP_SOURCE` / `RECAP_TARGET` / `RECAP_JOBGATE_MB` default 1500) runs only
after the wiring exists and refuses a closed gate.

### Step 10. Fieldwork push (user-only), dry run first

```
task push -- --dry-run --scenes <win-...>,<lot-...>                          # read-only against Fieldwork: ListChanges/GetItem/GetInfo/HEAD; writes a local plan file only
FIELDWORK_PUSH_ALLOW_LIVE=1 task push -- --scenes <win-...>,<lot-...>         # USER runs this; refused without the variable (fieldwork_push.py:1-7; Taskfile.yml:677-680)
task push:status                                                             # receipts only, no network (Taskfile.yml:682-685)
```

Gate: step 7 done (the pushed item is "the model estimate plus the local review state as of publication"); reviewer
decision 9 recorded; dry-run `counts.create` equals the number of windows and the plan lists no `supersedes` for them
(section 4). Rollback: a pushed item is immutable (`DeleteItem` tombstones forever, local-to-fieldwork.md); do not
push until the reviewer agrees.

EXECUTED (authorised by "yes to defaults": capture-only first, then the Fieldwork push of the windows; section 0b):
dry run `20261001T070620Z-d4ab79b9` at 07:06:13Z (`status` planned, `counts.create` 48 = the 48 captured windows,
`labelsToCreate` 0, `mirrorCursor` 590, `bytesToUpload` 59,714,059, `supersedes` [] on every scene, blocked none); LIVE
run `20261001T070709Z-5da0381e` at 07:07:07Z (`status` pushed, `created` 48, `uploadedAssets` 144, `uploadedBytes`
59,714,059, `marked` 0, `markConflicts` 0, `skippedIdentical` 0, `conflictIdReuse` 0; plan `options` {reviewHistory omit,
markSuperseded true, allowUnrecovered false}); item ids `addr-scene-<window>-m<modelSignature[:12]>-r1` (48 request /
response pairs under `runs/20261001T070709Z-5da0381e/items/`); `push:status` afterwards: items 64, markedItems 2, runs 4
(`20261001T014321Z-17b1f7fe`, `20261001T015439Z-c7cd42ab`, the dry run, the live run), 48 receipts (example
`lot-25641`: `reviewRevision` 1, `pushedAt` 2026-10-01T07:07:36Z). What was pushed for each window: title "Recapture
window · <source scene id> · <address | lot …> · <n> frame-cut target(s)" (as section 3 specified; read from the request
document), tags `mooroolbark, pushed, gen, review, outside-inventory`, `metadata.addr.generation` {modelSignature,
reviewRevision 1, reviewHeadSha256, imageSha256, width, height, supersedes [] …} — the `lot-window` tag and
`generation.windowOf` that section 4 expected are NOT present (recorded, not reconciled; the source items are untouched
either way, `marked` 0). The held `lot-30381` was never pushed (never captured); its re-plan `win-6dbb766c398af77e` WAS pushed in run H (section 0c): dry
run `20261001T074633Z-0ea5a165` (07:46:26Z, `counts.create` 1, `mirrorCursor` 638, `bytesToUpload` 1,703,789) then LIVE
`20261001T074717Z-cd457ee0` (07:47:13Z; `status` pushed, `created` 1, `uploadedAssets` 3, `uploadedBytes` 1,703,789, `marked` 0,
`markConflicts` 0, `conflictIdReuse` 0; item `addr-scene-win-6dbb766c398af77e-m19fdaf1d14e0-r1`, `reviewRevision` 1, `supersedes`
[]); `push:status` afterwards: items 65, markedItems 2, runs 6. Pushed in total: 49 window items by two live runs, 0 marks.

### Step 11. Pull

```
task pull -- --dry-run        # download and preview only (fieldwork_feedback.py:403)
task pull                     # applies as fieldwork_import.pending drafts on the WINDOW scene; excluded until accepted locally
```

The `addr-scene-roof_training_250-30371` conflict (6 field conflicts on `d7f7ceb84a6d0db4`, `5d1c61a0f7ef49d9`,
`133736ada6ace260`) is reported on every run until resolved (`.run/fieldwork-path/pull-live-2.json`); it does not
block other scenes.

EXECUTED at 07:09:10Z (`task pull`, `.run/focus-recapture/pull-b.stdout.json`; section 0b): `status` "conflicts" — the SAME
pre-existing item and the same 6 field conflicts (notes + status of those three objects: "local and Fieldwork edits
overlap"), cursor 590 -> 638, `counts` {changes 48, items 35, appliedScenes 0, alreadyAppliedScenes 0, receiptOnly 0,
conflicts 6}, `reviewUrls` [], `nextStep` "Conflicting feedback and any completed saves are preserved; resolve the reported
conflicts, then rerun task pull with the same options". The 48 changes are the push's own items coming back through the
mirror; no window scene received a `fieldwork_import` draft (appliedScenes 0). The counter after the pull
(`counter-B-final.json`, 07:11:06Z) differs from the pre-pull one only in `computed_at` (`capture-2026-10-01.log:202`). Run H pulled again at 07:47:36Z
(`.run/focus-recapture/pull-h.stdout.json`; section 0c): `status` "conflicts" (the same item, the same 6 reasons), cursor 638 ->
639, `counts` {changes 1, items 35, appliedScenes 0, alreadyAppliedScenes 0, receiptOnly 0, conflicts 6};
`data/fieldwork-sync/mooroolbark/feedback.json` reads `cursor` "639" at 08:42Z. No window scene has received a `fieldwork_import`
draft (`appliedScenes` 0 in both pulls).

### Step 12. Count

```
cd /Users/benebsworth/projects/addr
/Users/benebsworth/projects/addr/scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.r5_precision count --json --block v2 \
    > .run/focus-recapture/counter-after-$(date -u +%Y%m%dT%H%M%SZ).json
```

(The direct command, so the log names the interpreter and module; `task eyes:small-roofs > file` would also be valid JSON because go-task echoes its command line to stderr, section 1.1.)

Window decisions enter the count only through the hop (section 1.5 entries present, step 1 code present). Below the N
rule (30 / 8 per class / 6 scenes) the counter prints `n/a` and the per-object table; `rejudge` / `report`
(`task training:sam3-mac:human-precision`, Taskfile.yml:939-942) only when the rule passes; nothing promoted.

EXECUTED (section 0b): `counter-B-before.json` 06:38:45Z -> `counter-B-after.json` 07:05:46Z -> `counter-B-final.json`
07:11:06Z (after the pull). The section 6.2 gated fields (`primary_rows`, `primary_n` 13, roof 8, not_roof 5, `scenes` 3,
`targets_total` 170) are identical across the three; `read` "n/a (N=13, roof=8, not_roof=5, scenes=3)" throughout: no window
decision exists, so nothing moved by the hop (`carried_primary` 0). What moved, explained row by row: `targets_remaining`
3 -> 80 (the 78 carried copies are now the asked targets, each once on its window via `carried_rows` / `next_scenes`, plus
the 2 old slices of `roof_training_250-30371` whose window was held); `target_exclusions` `agent_noted` 11 -> 8,
`fieldwork_pending` 143 -> 69 (those old slices left their old exclusion for `recaptured:<window>`: 48 keys summing 78),
`decided` 13 -> 13; `lineage.registry_files` [] -> [`windows-2026-10-01.jsonl`], `registry_rows` 0 -> 82, `carried_copies` /
`carried_joined` 0 -> 78, every `carried_refused_by_reason` 0, `duplicate_decision_on_recapture` 0, `ambiguous_carry` 0;
`reviews_read` 200 -> 248; `rules_sha256` `df42a3e5…` and `code_sha256` `9b4c363c…` unchanged across B (stream T moved
both at 07:36Z: `counter-T-after.json` 07:36:44Z keeps N 13 and `targets_remaining` 80). The accepted-import route of A1,
not the hop, is what moved N 7 -> 13 earlier (section 0b, "Counter across A1"); that is a counter delta read from the
files, with no attribution of any reviewer action.

Later counters (section 0c): `counter-H-after.json` 07:48:24Z (after run H's carry, push and pull; `rules_sha256` `738ed82a…`,
`code_sha256` `020df9da…`), `counter-C-before.json` 08:24:41Z (the chain code `9c4dd2e1…` under the old DESIGN sha),
`counter-C-after.json` 08:25:16Z (under DESIGN `2f48e429…`) and `counter-C-final.json` 08:28:11Z (under RULES `97590a2a…`): the six
gated fields SAME throughout (my re-run of the section 6.2 comparison on each adjacent pair, exit 0); `targets_remaining` 80 = the
80 carried copies asked once on their 49 windows (the 2 old slices of `roof_training_250-30371` moved to
`recaptured:win-6dbb766c398af77e`); `target_exclusions` {agent_noted 8, decided 13, fieldwork_pending 69, 49 `recaptured:*` keys
summing 80}; `lineage` {registry_rows 84, carried_copies 80, carried_joined 80, every refusal 0}; from C-after on,
`carried_design_sha_match` transform / registry {current 0, via_chain 80, refused 0} with the hop (amendment 4, `identity`) on
every carried row; `reviews_read` 249. No window decision exists in any of them (`carried_primary` 0).

### Step 13. Takeover: the whole roof becomes the asked object (2 October, 23:33Z to 00:22Z; EXECUTED)

Reviewer direction (verbatim, 2026-10-02): "ok so from these images, its all still happening, the organge boundig box cuts off
so the object boundary looks bad". In the QA renders of step 12's windows the orange dashed line was the projected OLD tile
edge and the red outline the frozen slice carried as-is, so the asked object still showed the truncated boundary by
construction. Remedy: the TAKEOVER RULE, pre-registered as the WINDOW.json dated amendment `takeover_rule`
(2026-10-01T23:44:55Z, `94fcb1b7…` -> `ce94015b…`, 3 amendments; thresholds and constants unchanged; cover_min 0.8,
other_cover_max 0.5, frame_tolerance_px 1.0), counter support as four RULES entries (2026-10-01T23:33:16Z, `97590a2a…` ->
`aa7bceb0…`) and one mirror entry (2026-10-02T00:21:02Z, `aa7bceb0…` -> `89e92c5e…`, 20 amendments; `human_precision.join_order`).

```
scripts/ml/.venv/bin/python -m scripts.ml.scene_focus takeover                       # dry run over every lot-*/win-* window; writes nothing
scripts/ml/.venv/bin/python -m scripts.ml.scene_focus register-takeover --expect-sha <WINDOW.json sha>   # the dated WINDOW amendment, once
task focus:takeover CONFIRM=1                                                           # --write: revision-1 carry/carry-forward reviews only
```

Rule, in one sentence: a carried copy C is taken over by building detection D of the window's own model when D is the single
building detection covering at least 0.8 of C's area, D's ring lies wholly inside the frame (no vertex within 1 px of an edge)
and no other building detection covers C at 0.5 or more; then C is `superseded` with `replacement_ids [D]` (kept for lineage),
D carries `replaces` and `derived_from` (the same sorted list) and stays `candidate`. Nothing becomes confirmed or rejected.

EXECUTED (`.run/focus-recapture/takeover-2026-10-02.log`, run record `takeover-write-2026-10-02.json`, pre-takeover copies of
all 51 reviews under `takeover-before-reviews/`): 53 windows, 51 planned, 39 written (58 slices -> 51 whole-roof detections, 4
detections taking over several slices, one 5), 12 windows nothing to take over (review.json byte-identical), 2 refused by name
(`lot-29772` revision 2, `win-2076df0d53bc99b2` revision 6: other-authored, still the editor's "Keep earlier objects" path), 23
copies left alone (below_cover 14, no_detection 9; 0 shared_cover, 0 detection_touches_frame). Every written review differs from
its pre-takeover copy only in copy status / replacement_ids, D replaces / derived_from, and top-level updated_at
(2026-10-01T23:45:55Z) / updated_by `scripts.ml.scene_focus takeover`; revision 1 everywhere (skeptic: reverting those fields
reproduces the registry's previous_review_sha256 39/39). Registry 148 -> 187 rows (39 `takeover`), first 148 rows byte-identical.

Counter: `counter-T2-before.json` 23:44:55Z -> `counter-T2-after.json` 23:48:04Z -> `counter-T2-final.json` 00:21:57Z (after the
mirror entry and the `replacement_mismatch` tally): primary_n 15 (roof 10 / not_roof 5, 4 scenes), `primary_rows` identical,
targets_total 170, targets_remaining 81 -> 74 = 23 slices left alone + 51 whole-roof detections asked once each (no non-window
target left: both `roof_training_250-30371` slices were taken over); lineage takeover_rows 58, takeover_joined 58,
ambiguous_takeover 0, superseded_by_takeover 58, replacement_mismatch 0, carried 85 / 85, every refusal 0; `rules_sha256`
`89e92c5e…`, `code_sha256` `f2717aa4…`.

Two decisions recorded with the mirror entry (skeptic findings, not defects): (1) D.derived_from = D.replaces is a deviation from
the computed task text, forced by the editor's save validator (`src/lib/scene-instances.ts` lineageIssue: a superseded object
needs a non-empty replacement_ids and every named object must point back through derived_from), acknowledged in the rule text.
(2) The counter's hop reads D.replaces alone; the copy's replacement_ids and D.derived_from are bookkeeping, never read to hold
or refuse a link. A `copy_replacement_mismatch` refusal was tried and withdrawn before any write: it would resolve a two-claimant
copy in favour of the detection the copy names, changing the pre-registered ambiguous_takeover semantics. Instead
`lineage.replacement_mismatch` tallies held links whose copy does not name D back (0 of 58); a rise is repaired by hand.

Fieldwork: dry run `20261001T235409Z-e01abff9` ("current 39"), live `20261001T235547Z-1320abe0` up_to_date, created 0, marked
0; pull cursor 746, 0 changes, the pre-existing `roof_training_250-30371` conflict only. The pusher keys generation items on
model signature + review revision and classifies a revision-1 content edit as current, so the 39 edited windows' Fieldwork items
still show the pre-takeover state (copies needs_adjustment, D without replaces). DECISION RECORDED: window decisions are made in
the LOCAL editor from the worklist links until the pusher keys on a review content hash (a code change for a quiet window); a
Fieldwork decision on a slice there would collide with the local `superseded` status on pull.

Reviewer surfaces: `data/references/eyes-checkpoints/WINDOW-WORKLIST-2026-10-02.md` (39 windows with whole-roof links, 12 with
slices only, 2 other-authored; the 1 October worklist kept) and `data/reviews/eyes/recapture/window-qa-2026-10-02-takeover/`
(53 renders, 4 contact sheets, QA.md, measurements.json: asked target red, superseded slices thin grey, other detections thin
cyan, old tile edge a thin dotted labelled line; 0 whole-roof targets touch the frame, min margin 3.17 m on
`win-1dff35d6922a4132`, median 20.48 m). Visual check by the orchestrator over all four sheets: every whole-roof target is a
complete roof inside its frame; the 23 slices left alone still show the truncated piece (best cover per slice is in the run
record: 0.79, 0.78, 0.75, 0.71, 0.70, 0.69, 0.62, 0.51, 0.47, 0.21, 0.15, 0.14, 0.03, 0.00 and 9 with no detection under them).

Addendum (10:50 AEST): neighbour roofs the window frame cuts (362 of 1,008 building detections across the 53 windows) are
not targets; lot-tags-v1 / object-hints-v2 sidecar rows were written for all 53 windows (offline, frozen parcels; 1,474
`frame_cut` flags) so the editor shows "Touches the image edge; the frame cut it, not the building." on them:

```
ids=$(ls -d data/scenes/lot-* data/scenes/win-* | xargs -n1 basename)      # zsh: pass ${=ids}, not $ids
scripts/ml/.venv/bin/python -m scripts.ml.scene_lot_tags --scenes ${=ids} --offline --write --quiet --report data/reviews/eyes/recapture/lot-tags-windows-<date>.json
scripts/ml/.venv/bin/python -m scripts.ml.scene_object_hints --scenes ${=ids} --write --report data/reviews/eyes/recapture/object-hints-windows-<date>.json
```

Open (quiet window, code): editor advance order skipping `frame_cut`; `export_dataset` IGNORE for `frame_cut` on window scenes
(windows enter the export through the latest.json glob; only `clipped_by_frame` is IGNORE today). The first whole-roof decision
(lot-30300 / 8130e74685e42d9e, revision 2) reached the count through the hop: `counter-T2-user1.json` N 16, takeover_primary 1.

Addendum (19:15 AEST, "try now"): the editor now carries a third filter "Frame filter" (Skip frame-cut by default; URL
`frame=all|only`) applied to the queue and the Right-arrow/Backspace advance, and a LOCAL `frame_cut` hint from the ring
(`src/lib/outline-hints.ts`, edge_px 1 = `scene_lot_tags.DESIGN['edge_px']`), so a cut neighbour is never the next object and is
labelled even without an index row. `export_dataset` treats frame-cut objects on window scenes (`window_frame` dict) as IGNORE on
both the human and pseudo paths (`frame_cut_human` / `frame_cut`); non-window scenes export byte-identically. Correction to the
10:50 addendum: the 10 confirmed frame-touching objects on lot-30300 were solar_panel / driveway / vegetation, not buildings
(confirmed frame-cut buildings: 0 on lot-30300, 0 of 8 across the windows). Handover section 11 has the file:line detail.

Addendum (20:10 AEST, "simplify things"): a fourth editor filter "Lot filter" (This address's land, default; This lot only; All
objects; URL `lots=lot|all`) scopes a window's queue and advance to the address plan using the eyes-index lot tags through the
new read-only `GET /api/scenes/lots`. On lot-30300 the default pair (skip frame-cut + this address's land) leaves 14 objects of
43. Inert (shows "Lot unknown: all objects") on the 41 subject windows whose address has no lot. Handover section 11 has detail.

## 3. What the reviewer sees and does per scene

Local editor: `http://localhost:3008/scenes?scene=<window id>&status=all&layer=edited&object=<source object id>`
(the carried copy keeps the SOURCE object id unless the new model already uses it, then `<id>-carried`;
scene-frame-transform.ts:561-567, :474-478). The window opens at review revision 1, not stale, showing the whole house
centred at 0.059-0.118 m/px with each carried slice outline as `needs_adjustment` and its one-sentence note naming the
source scene. If detection ran, the new model's objects stand beside them under their own ids (never counted in round
5: `not_judged_image`). Decision: Right arrow = confirmed + geometry reviewed (roof), Backspace = rejected (not a
roof), then Save (handover §8 lines 222-224). Because the carried copy carries no assistant text, NO `[reviewer]`
marker is needed for it to count (design §4; `agent_fresh_decision` applies only to agent-noted objects). The first
save is revision 2. One decision per frozen key: if the reviewer also marked the old slice, the old decision counts
and the copy is listed `duplicate_decision_on_recapture`; copies in two windows count nowhere (`ambiguous_carry`).

The deferred TypeScript wiring (decision carry B, docs/recapture-carry-2026-09-30.md §6 lines 137-143), four files
written whole in one save each:

1. `src/lib/scene-types.ts`: `frame_transform?` on the snapshot, `carried_from?` on the object, `recovery.method`
   gains `georeferenced-affine`.
2. `src/lib/scene-feedback.ts`: one branch in `recoverSceneReview` before the identical-frame check, delegating to
   `scene-frame-transform.ts`; `carried_from` in the edit-comparison serializer.
3. `src/lib/scene-store.ts` `loadScene`: resolve the source snapshot from `data/scenes/<source_scene_id>/models/<sig>.json`
   after validating the id, run the Python verifier behind a promise cache, name the failed step in
   `recovery_reason`; `validateSceneReview` shape-checks `carried_from`.
4. `src/app/scenes/page.tsx`: "a third banner sentence for a recapture, never 'The image is unchanged.'" Today the
   notice at page.tsx:1235 chooses between "The original image pixels were preserved inside a larger capture. Earlier
   edits keep their geographic positions." and "The image is unchanged." The contract the headless check codes
   against (`scripts/verify-scene-recapture.mjs:245-249`): the banner must match
   `/re-projected through the georeferenced frame/`, `/residual [\d.]+ px/` and
   `/every carried decision returns for a recheck/i`, and must not contain "The image is unchanged." or
   "original image pixels were preserved".

Until wired: the TARGET carry (step 7) needs none of this, because it writes a plain `review.json` on the window's own
model_signature; the editor shows it as any saved review. A DECISION carry is previewed read-only with the step 9
snippet (stdout only) or `node scripts/test-scene-recapture.mjs` (fixtures). When wired, the decision carry must skip
every frozen key the window review already carries (design §4 B) so nothing appears twice and no Codex sentence heads
a carried copy's notes.

In Fieldwork (after the user's push): the item is found by its title only, so the title is
"Recapture window · <source scene id> · <address | lot pfi | lot unknown> · <n> frame-cut targets"; the object by
pasting the source object id into "Find object, class or status…"; a decision is the Inspector's Object review status
(`confirmed` = roof, `rejected` = not a roof) plus Geometry reviewed, saved with Save changes (handover §8 lines
191-194); it reaches the count only after `task pull` and local acceptance. As pushed (runs `20261001T070709Z-5da0381e` and `20261001T074717Z-cd457ee0`,
sections 0b, 0c): the 49 items carry that title form (read: "Recapture window · roof_training_250-25598 · 16A GREENVILLE
STREET, Mooroolbark VIC 3138 · 1 frame-cut target" on `addr-scene-lot-25641-m164206520024-r1`), `reviewRevision` 1 (the
carried copies as `needs_adjustment`), tags `mooroolbark, pushed, gen, review, outside-inventory`, and the local editor
URLs of every copy are listed in `data/references/eyes-checkpoints/WINDOW-WORKLIST-2026-10-01.md`. A Fieldwork decision on
a window item would arrive through `task pull` as a `fieldwork_import.pending` draft on the WINDOW scene and would count
only once accepted locally under the A1 route as narrowed by T (section 1.5); none existed at 07:11Z or 07:47Z (`appliedScenes` 0 in both pulls, section 0c).

## 4. Fieldwork plan

Read-only dry run for the new scene ids (never run in this document): `task push -- --dry-run --scenes <ids>`. Shape
of the output, read from the 14-scene run of this morning (`.run/fieldwork-path/push-dry-run-14.stdout.txt`):
`{"bytesToUpload": ..., "counts": {"create": N, "needs_label": ...}, "dryRun": true, "labelsToCreate": ...,
"mirrorCursor": "...", "planFile": "data/fieldwork-sync/mooroolbark/push/runs/<runId>/plan.json", "projectId":
"mooroolbark", "runId": "...", "status": "planned"}`. Note the dry run writes that local plan file (and syncs the
mirror); it writes nothing to Fieldwork. Expected for windows: `counts.create` = number of window scenes, each a NEW
generation item `addr-scene-<window id>-m<modelSignature[:12]>-r1` (local-to-fieldwork.md:83-85; `r1` after the
target carry, `r0` without a review), tags `gen` plus `lot-window`, `metadata.addr.generation.windowOf
{source_scene_id, object_ids, frame_transform.verified, window_scene_id}`; the supersede mark applies only to older
items of the SAME scene (local-to-fieldwork.md:42-45), so a window marks nothing and `supersedes` is empty: the
source item stays live and decidable, consistent with "source decision wins". The live run is the user's
(`FIELDWORK_PUSH_ALLOW_LIVE=1 task push -- --scenes <ids>`); the token file path is `deploy/annotation/admin-token.txt`
(fieldwork_feedback.py:22; present; never printed). Read-only inspection of items stays with
`go run ./cmd/annotate get|items|changes` (cmd/annotate/main.go:133-158).

As executed (section 0b; `.run/focus-recapture/push-dry-run-b.stdout.json`, `push-live-b.stdout.json`, the live run's
`plan.json` and one `items/*.request.json`): `counts.create` 48 = the window scenes of run B (expected form held; run H's dry run `counts.create` 1 for the 49th, section 0c); item ids
`addr-scene-<window>-m<sig[:12]>-r1` (held: `r1`, the target carry preceded the push); `supersedes` [] on all 48 (and on the 49th in run H) and
`marked` 0 (held: no source item was marked); `bytesToUpload` 59,714,059 for 144 assets. NOT as expected: the tags read
`["mooroolbark", "pushed", "gen", "review", "outside-inventory"]` with no `lot-window`, and `metadata.addr.generation` has
no `windowOf` key (its keys: actorKind, contextExpansion, height, imageSha256, inventory, modelSignature, reviewActor,
reviewHeadSha256, reviewRevision, reviewUpdatedAt, supersedes, width). The window's source scene and targets are
therefore findable in Fieldwork by the TITLE only; the local registry (section 6.4) remains the forward pointer. This is
recorded, not reconciled (the pusher is `scripts/ml/fieldwork_push.py`; what it would take to add the tag and the
`windowOf` block was not read here, and a pushed item is immutable).

What Codex's overrides on the OLD items mean after recapture: Codex wrote `properties.addr.overrides` on all 161
review-set targets (147 confirmed, 5 rejected, 9 candidate; notes open "Assistant image review · Codex · 2026-10-01";
`fieldworkReview.origin machine:codex-fieldwork-review`; items at revision 2; `data/reviews/eyes/fieldwork-review-2026-10-01/REVIEW.md`).
They are agent observations under the round-5 RULES (`agent_text_regex`), already pulled as 149
`fieldwork_import.pending` drafts (handover §8 lines 217-222). A recapture changes none of that: the old items and
their overrides stay where they are; a window item has a new sceneId, so no override transfers to it; the 79 pending
drafts among the 86 targets are never carried (design §4) and are asked to stay undecided on the old frames (hold
list). If another assistant pass touched the window items, their objects would become agent-noted and need the
`[reviewer]` marker again (design risks); push only after the reviewer agrees, and keep the titles explicit.

## 5. Cohort scenes path (12 scenes, 35 targets)

Scenes and target counts (`/tmp/frame-cut-targets.json`): 25098 / 7, 25222 / 3, 25598 / 4, 28530 / 3, 28731 / 1,
29350 / 3, 29353 / 4, 29545 / 2, 29582 / 1, 29790 / 3, 30371 / 3, 30381 / 1. Same steps as section 2 with three
differences (design §7):

1. Subject: the cut object is usually the NEIGHBOUR's roof (a cohort crop is about 33 m fitted around the CRM roof,
   scene_lot.py:3-5). The window is seeded by the object (slice plus body), never by the scene's address, so
   `lot-<own address_id>` stays reserved for an own-lot recapture (`task recapture:capture`). Plan rows of
   `plan-2026-09-30.jsonl` (sha above) show 7 of the 12 as `parcels_unavailable` offline (25098, 28530, 29790, 25222,
   29545, 28731, 29582): step 3's bounded Vicmap read is the normal case; 5 have parcel caches (25598 49.0 m z20;
   30381 59.28 m z20; 30371 87.85 m z20; 29350 and 29353 250 m z18, the plan-block / common-property case the design's
   CM/CP rule avoids). The 3 reviewer-marked `needs_adjustment` hints on `roof_training_250-30371`
   (`d7f7ceb84a6d0db4`, `5d1c61a0f7ef49d9`, `133736ada6ace260`) are hints, not decisions: their keys are carried as
   bare copies and the chat note stays on the source. `b65905d342a307d5` on the same scene is a counted rejection and
   is not a frame-cut target.
2. Displacement: every cohort window and fitted affine sits +0.4444 native z19 px (about 0.105 m) east and south of
   its georeferencing (`imagery.py:144-146` rounding; `offset-diagnosis-2026-10-01.md` §3, §6; medians (-0.0925,
   +0.1062) m -> (+0.0116, +0.0000) m and median |shift| 0.1491 -> 0.0437 m with it removed, :122). The capture
   RECORDS `frame_transform.source_displacement = {method: 'imagery-paste-rounding', native_px: [0.4444, 0.4444],
   source: offset-diagnosis-2026-10-01.md, sha256 560b5250...}` and never composes it into `pixel_affine` (verify
   recomputes the affine to 1e-6, py:98, :721-725). The measured residual absorbs it (about 0.15 m under 0.7). A later
   "correct" is a frame-transform-v2 re-measure (reviewer decision 5 / handover decision 4), never an edit.
3. Overlap: a 33 m crop against a 35-120 m neighbour-lot window is where the 0.6 FRACTION bit (designer C, not
   re-measured: 4 of 12 at the lot window, 9 of 12 at 120 m, common property never). Reviewer decision 1 was resolved as
   option (b) at 06:11:15Z (DESIGN `subject_recapture.min_overlap_px` 128, section 0b), so the gate for a subject window is
   the eroded overlap REGION in target px, and the fraction is recorded beside it. Under the floor every cohort row was
   `ok`: the smallest predicted and measured region was `lot-29385` (161.2 px, 1.26x the floor; source fraction 0.123082);
   the 12 cohort scenes' 20 run-B windows all captured (section 0b; `capture-2026-10-01.log:103-148`), with measured shift_m
   0.1188-0.1731 and ncc 0.7569-0.8954 on the fitted sources, and the 21st (`win-6dbb766c398af77e`, the held
   `roof_training_250-30371` pair) in run H with shift_m 0.141 and ncc 0.8494 (section 0c) (the displacement of item 2 sits inside those residuals, all
   under `max_shift_m` 0.7). The window is finer in native detail (z20 0.118 m/px) than the crop's z19 composite (0.236 m/px
   native, Lanczos upsampled; scene_lot.py:22-28).

`house-29785` (1024 px, z20) followed the registered path; its lot is covered by `house-29772` (recorded as
`covered_by_existing` [`house-29772`, `house-29846`, `mooroolbark-50011498-17-118442-80416`], not reused: reuse would write
into an existing scene while the reviewer is live). The dated plan's first row is `lot-29772` with `carry_from`
`house-29785`, 2 targets, 1024 px, span 100.288 m, predicted fraction 0.276707 against its 130.004 m source frame but
predicted region 625.5 px -> `ok`; it was CAPTURED first, at 06:49:35Z (measured fraction 0.276707, region 625.5 px, shift
0.0014 m, ncc 0.997, verified, 2 copies carried, editor 200, API revision 1 `stale` false; `capture-2026-10-01.log:41`,
`:52-53`) and pushed. Its directory NAME collides with the own-lot recapture of address 29772 (section 6.4). The
mooroolbark tile target's z20 window `win-f94c545af3c4961a` (1024 px, span 85.913 m; predicted fraction 0.028, bounded by
(85.913 / 271.827)^2 = 0.100) passed the floor (region 461.8 px) and was captured at 06:56:02Z: verified, ncc 0.8819, 1 copy
carried (`:101`). Decision 1 (c) — unverified captures with no carry and no count — was not needed for any row.

## 6. Verification checklist (run before the first capture and after each step that writes)

### 6.1 Frozen files preserved by sha

```
shasum -a 256 data/reviews/eyes/recapture/DESIGN.json data/reviews/eyes/training/sam3-mac/round5/RULES.json \
  data/reviews/eyes/recapture/plan-2026-09-30.jsonl data/reviews/eyes/recapture/pilot-2026-09-30.json \
  data/reviews/eyes/recapture/offset-diagnosis-2026-10-01.md data/reviews/eyes/recapture/WINDOW.json
```

Expected at write time: `29fba5a3...`, `d7613249...`, `f476448f...`, `08ad5ffc...`, `560b5250...` (full values in
section 0; all five re-read equal at refresh) and, since 05:02:09Z, `2d7c4a5b...` for WINDOW.json (never rewritten;
section 1.5). DESIGN.json and RULES.json change ONLY at step 2 (appended `amendments`; body equal). Also sha the 32 source
scenes' `latest.json`, `review.json` and `models/*.json` and `house-29772/latest.json` before step 5 and after every
capture (they must not change; a `review.json` change there is the reviewer's own save, printed as a note, never
caused by this path). Dataset-v2 splits: `split_file_sha256` in the counter output stays
`1ba87eed3544fc19e220effd47f1b86d0c7459899d0e17701dacdfece0d8827c`.

Expected at the executed-state refresh (read 07:38:39Z): DESIGN.json `da322027d77ec6ec9424cb9b64cc69753a09680b0c1aab82178942b0d48f1541`
(3 amendments; A2), WINDOW.json `4353815256a35b42125dd5626da491de9d185065327d11e94ee9f9c85aee1ade` (1 amendment; A2),
RULES.json `738ed82a81bec20bcf732f9dc0281f077b58baf5515f8cee54a34b50b23c348d` (13 amendments; T at 07:36:04Z — the batch and
its three B counters ran at `df42a3e51bf7f61e2b991960f22c5e1c3b22c8bf2a63ff3b3e0979e3a60ac436`, 12 amendments, A1), plan-2026-09-30.jsonl
`f476448f…`, pilot-2026-09-30.json `08ad5ffc…`, offset-diagnosis-2026-10-01.md `560b5250…` (all three unchanged); the dated
plans `window-plan-2026-10-01.jsonl` `4f198b26022f240c0568e952e1aee7aae7bef9ab658c93bcfb697f9b4b0fbe6a` and
`window-plan-2026-10-01b.jsonl` `d177906d6ca45ba440fb93aa3edc8b9fbeba62afb9146c15cfd5ae455f5dc7ac` (never rewritten); the
registry `windows-2026-10-01.jsonl` `1eb6dee8a7010fefb1fdd74ce3885246f982dabd6611d4638385a6e7315e674b` (append-only: its sha
moves with every carry or withdrawal row; compare row counts, not the sha, after a further carry); `split_file_sha256`
`1ba87eed…` in `counter-B-final.json`; `REMARK-WORKLIST-2026-10-01.md` `77cb0c5e…` unchanged; `WINDOW-WORKLIST-2026-10-01.md`
`d9b71970…` (new at 07:10:06Z; REWRITTEN later by a writer the file does not name — live sha `e72130c5…`, mtime 09:04:37Z, 66,185 bytes; section 0b "Review surface" row). The 32 source scenes and `house-29772` were not written by run B (integrity step
07:10:44Z: 0 `src/` / `.mjs` files newer than the baseline; `capture-2026-10-01.log:190-200`); the 49 windows are now
existing scenes and their `review.json` may legitimately change by the reviewer's saves only.

Expected at the second executed-state refresh (read 08:42Z; section 0c): DESIGN.json
`2f48e4290c422de3f8c17e92fdeaa99be37255f0d4b0bd6dd85f4066445fcddb` (4 amendments; the `identity` entry of 08:24:41Z; byte copy
before, `.run/focus-recapture/DESIGN.before-identity-2026-10-01.json`, hashes to `da322027…`), RULES.json
`97590a2a706f050df65c49a4da8aa46cb5e9c9cb8b3d2f84bc84e22d2adb2e92` (15 amendments; entries 14-15 of 08:28:04Z; byte copy before,
`RULES.before-chain-2026-10-01.json`, hashes to `738ed82a…`), WINDOW.json `43538152…` unchanged, plan-2026-09-30.jsonl /
pilot-2026-09-30.json / offset-diagnosis-2026-10-01.md unchanged (`f476448f…`, `08ad5ffc…`, `560b5250…` per the C records; not
re-hashed here), the dated plans unchanged (`4f198b26…`, `d177906d…`), the registry
`a0073fc30b0c68958af46e16b8fa5cc2389602f076480a8a8f8a6299f99f0288` (84 rows, mtime 17:45:47 AEST), the hold list `cee08c91…` (new,
written once), both worklists unchanged AS READ AT 08:42Z (`d9b71970…`, `77cb0c5e…`; the window worklist has since been rewritten — live sha `e72130c5…`, mtime 09:04:37Z; section 0b "Review surface" row), `split_file_sha256` `1ba87eed…` in `counter-C-final.json`. The
transforms and registry rows recorded under `da322027…` are accepted through the dated chain, not by equality (section 0c).
Nothing under `data/scenes/` other than the NEW `win-6dbb766c398af77e` (17:45:46-47 AEST) was written by streams H and C: at
08:42Z no file there was newer than 18:11 AEST (`find -newermt`), and stream C's first write was 18:21. AFTER that read, at
08:50:43-45Z, stream G's `carry --repair-groups` REWROTE 8 window `review.json` files (group records added, revision 1 kept) and
appended 8 `carry_repair` rows to the registry (now 92 rows, sha `8ee2f63b…`), and at 08:54:38-39Z the app saved
`lot-29772/review.json` and `house-29785/review.json` (`updated_by` "local", revision 2; the POSTs came from the shade-sail Codex session's `save_classifications.py`, not the reviewer — corrected 09:24Z) — section 0c last row, section 7.0b item
11; the registry's row count is the comparison, never its sha.

### 6.2 Counter gated fields

The handover (line 140) calls them "the counter's six gated fields" without listing them in any file found at write
time; the pre-registered gate script `.run/w1fix-triple-gate.py` compares PRIMARY rows, `primary_n`,
`primary_by_class.roof`, `primary_by_class.not_roof` and `scenes`. This runbook pins the set to those plus
`targets_total`: `primary_rows` (sorted), `primary_n`, `primary_by_class.roof`, `primary_by_class.not_roof`,
`scenes`, `targets_total`, all byte-identical BEFORE and AFTER steps 1, 2, 4 and 9 (no window decision exists, so
nothing may move). `targets_remaining` and `target_exclusions` legitimately change after step 7 (old targets excluded
as `recaptured:<window>`, copies listed on the window) and must be explained row by row, never silently.

```
$PY - .run/focus-recapture/counter-before-<ts>.json .run/focus-recapture/counter-after-<ts>.json <<'PY'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])          # the two paths on the command line above (python - FILE FILE)
keys = (('primary_rows', lambda d: sorted(map(tuple, d['primary_rows']))), ('primary_n', lambda d: d['primary_n']),
        ('roof', lambda d: d['primary_by_class']['roof']), ('not_roof', lambda d: d['primary_by_class']['not_roof']),
        ('scenes', lambda d: d['scenes']), ('targets_total', lambda d: d['targets_total']))
ok = True
for name, f in keys:
    same = f(a) == f(b); ok &= same; print(('SAME ' if same else 'DIFF ') + name, f(a) if not same else '')
print('rules', a['rules_sha256'][:12], b['rules_sha256'][:12], 'code', a['code_sha256'][:12], b['code_sha256'][:12])
sys.exit(0 if ok else 1)
PY
```

Both snapshots are written with the direct command of section 1.1 (`... r5_precision count --json --block v2 > file.json`;
`task eyes:small-roofs > file.json` would also parse, section 1.1). Run at refresh time on three real pairs, `sys.argv[1:3]`
receiving both paths each time: `.run/focus-recapture/counter-before.json` / `counter-after.json`: six SAME, exit 0,
`rules d7613249422f d7613249422f code dca5927d91c6 fa1b37622bc6`; `counter-after2.json` /
`counter-runbook-20261001T052115Z.json`: six SAME, exit 0, `code f6496653a638 f6496653a638`;
`counter-runbook-20261001T041307Z.json` / `counter-runbook-20261001T052115Z.json`: six SAME, exit 0, `code dca5927d91c6
f6496653a638`. `rules_sha256` changes exactly once (step 2, not yet); `code_sha256` has changed twice so far
(`dca5927d…` -> `fa1b3762…` -> `f6496653…`, section 1.6), each a code edit named here; any further change is a new code
edit to name in the log.

At the executed-state refresh: `rules_sha256` has changed TWICE, not once — `d7613249…` -> `df42a3e5…` (A1, 06:03:14Z;
`counter-A1-before` / `counter-A1-after`) and `df42a3e5…` -> `738ed82a…` (T, 07:36:04Z; `counter-T-after.json`); each is a
dated `amendments` append named in its applied-* record. `code_sha256` has changed twice more: `f6496653…` -> `9b4c363c…`
(A1 code, the accepted-import route) -> `020df9da…` (T). The section 6.2 fields across A1 did NOT stay identical:
`primary_n` 7 -> 13, roof 2 -> 8, `scenes` 2 -> 3, `primary_rows` +6 (`applied-rules-2026-10-01.json` `counter.deltas`,
`primary_rows_added`) — that is the RULES text change the step-2 gate anticipated ("rules_sha256 changes exactly once"),
explained row by row there, not a silent move; `targets_total` 170 stayed. Across B (`counter-B-before` 06:38:45Z,
`counter-B-after` 07:05:46Z, `counter-B-final` 07:11:06Z) all six fields are SAME (`primary_rows identical: True`,
`capture-2026-10-01.log:168`); `targets_remaining` 3 -> 80 and `target_exclusions` moved exactly as step 7 foresaw (78 old
slices `recaptured:<window>`, 78 copies asked on the windows, 2 old slices left; step 12). Across T (`counter-T-after`
07:36:44Z) N 13 and `targets_remaining` 80 are unchanged.

At the second executed-state refresh (08:42Z): `rules_sha256` has changed a THIRD time, `738ed82a…` -> `97590a2a…` (C, 08:28:04Z,
entries 14-15; `counter-C-after` -> `counter-C-final`), and `code_sha256` once more, `020df9da…` -> `9c4dd2e1…` (the chain code;
`counter-H-after` -> `counter-C-before`). The comparison above, re-run here on (T-after, H-after), (H-after, C-before), (C-before,
C-after) and (C-after, C-final): six SAME, exit 0 each. What else moved, named: H added `recaptured:win-6dbb766c398af77e` 2 and
moved `lineage` 82 / 78 -> 84 / 80 (step 7); C-before added `lineage.design_accepted` and `carried_design_sha_match` (all 80
`design_sha_current` under the old sha); C-after moved `lineage.design_sha256` to `2f48e429…` and the 80 hops to
`design_sha_via_chain`; C-final changed `rules_sha256` only (section 0c).

### 6.3 Guard and app

```
task eyes:guard                                    # exit 0, "guard ok True failing []" (scripts/ml/eyes_guard.py; --write NAME stores a report)
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:3008/queues/small-roofs"      # 200
curl -s 127.0.0.1:18090/stats                      # queue and prefetched small
ls -d data/scenes/qa_* 2>/dev/null | wc -l | tr -d ' '                                   # 0 after the rehearsal
```

### 6.4 Registry and identity

Every row of `data/reviews/eyes/recapture/windows-2026-10-01.jsonl` names a window directory that exists with
`frame_transform.verified` true and a `design_sha256` that is DESIGN.json's current sha or is accepted through the dated
amendment chain (`design_chain`, section 0c; before 08:21Z the rule was equality with the current sha); no frozen key appears in
two window scenes (else `ambiguous_carry`); ids match `[a-zA-Z0-9_-]{1,100}` (scene_self_review.py:178-181 at repair time, :179 at
08:42Z). As read at 07:38Z: 82 rows, 48 windows, all 48 directories exist (`ls -d data/scenes/lot-* data/scenes/win-*` = 48),
every row `frame_transform_verified` true and `design_sha256` `da322027…` = the then-current DESIGN sha, no key twice (section
0b). As read at 08:42Z: 84 rows, 49 windows, all 49 directories exist (`ls -d …` = 49), every row `frame_transform_verified` true
and `design_sha256` `da322027…` = the `previous_sha256` of DESIGN amendment 4 (accepted `design_sha_via_chain`, hop 1, by
`design_chain.read` on the live file), no key twice (section 0c).

**Identity-reservation rule** (added at the executed-state refresh; coded by stream H in `scripts/ml/scene_focus.py` at
sha `ed9bcc4e…`, 07:36 AEST, `RESERVED_PREFIXES` / `IDENTITY_RULE` / `reserved_by` and the `window_row` identity branch;
tested by `test_scene_focus.py::test_lot_identity_is_reserved_when_a_cohort_or_house_scene_of_that_address_exists`, 38
passed): a window takes the `lot-<address_id>` name ONLY when it is the single numbered lot of one resolved address AND no
scene `roof_training_250-<address_id>` or `house-<address_id>` exists (`data/scenes/<id>/latest.json` on disk, or such an
id injected into the plan) AND no source scene in the window has that address as its own; otherwise it is `win-<hash16>`
and the row records `reserved_by` (the reserving scene ids) with the flag `address_reserved_for_scene` (or
`address_reserved_for_source`); the address, lots, rule and title stay recorded inside the row whatever the id family;
the header counts `address_reserved_windows`. Why: those two scene families' OWN-lot recapture (`scene_lot.lot_source`;
`task recapture:capture ADDRESS=<id>` hard-wires `--carry-from roof_training_250-<id>`, Taskfile.yml:961-965) writes
`lot-<address_id>`, and a window of a NEIGHBOUR's address must never take the name that recapture would need (design §3
"lot-<own address_id> stays reserved for an own-lot recapture"; section 5 item 1). Before H the module checked only the
sources inside the window (`address_reserved_for_source`), so a cohort scene of that address that was NOT a source of the
window did not reserve it. Two collisions of the dated plan `window-plan-2026-10-01.jsonl` (written 06:43:30Z, before the
rule) are recorded:

- `lot-30381` — HELD, not captured. Row: identity `lot`, `carry_from` `roof_training_250-30371`, `address_id` 30381 ("65
  CAMBRIDGE ROAD, Mooroolbark VIC 3138", lot pfi 52440577), 1024 px, span 78.671 m, `overlap_gate` ok (fraction 1.0, region
  413.0 px), sources `roof_training_250-30371` / [`133736ada6ace260`, `5d1c61a0f7ef49d9`], `covered_by_existing`
  [`map-aed38945f12844b2`, `mooroolbark-50011498-17-118443-80417`], flags []. Address 30381 is the OWN address of cohort scene
  `roof_training_250-30381` (exists; itself the `carry_from` of `lot-30372`, the neighbouring unit's lot) — a `lot-30381`
  window of 30371's targets would have blocked `task recapture:capture ADDRESS=30381`. The batch driver held it
  (`run-captures-b.sh` `HOLD=(lot-30381)`; `capture-2026-10-01.log:29`, `:144`), so its two targets are the 2 old slices
  still asked on `roof_training_250-30371` (`targets_remaining` 80 = 78 + 2). Stream H re-planned the same two targets
  under a NEW dated plan name (`task focus:plan TARGETS=.run/focus-recapture/targets-held-30381.json DATE=2026-10-01b`,
  offline, 07:37:16Z): `window-plan-2026-10-01b.jsonl`, one row `win-6dbb766c398af77e`, identity `win`, flags
  [`address_reserved_for_scene`], `reserved_by` [`roof_training_250-30381`], the same frame (1024 px, span 78.671 m, region
  413.0 px, gate ok), `dedupe` new. Its capture (07:37:55Z) met `GATE CLOSED: memory` three times and stopped
  (`capture-2026-10-01.log:207-214`); the background wait from 07:39:42Z met 14 more memory closures and one process closure (the
  runner `tsc`) before `jobgate 1500` opened at 07:45:43Z (`:215-240`), and the window was CAPTURED (5 s; verified, fraction 1.0,
  region 413.0 px, shift_m 0.141, ncc 0.8494, margin 21.26 m), VERIFIED (exit 0, `captures/win-6dbb766c398af77e.verify.json`; re-run
  read-only at 08:48Z with `design_sha_match` via_chain) and CARRIED at 07:45:47Z (2 `carried_source` rows appended to
  `windows-2026-10-01.jsonl`, `review.json` revision 1 with the 2 copies, `decided_on_source` []), then PUSHED (dry run
  `20261001T074633Z-0ea5a165`, live `20261001T074717Z-cd457ee0`: 1 created, 0 marked) and PULLED (cursor 638 -> 639);
  `counter-H-after.json` 07:48:24Z: N 13, `targets_remaining` 80 = the 80 carried copies, `recaptured:win-6dbb766c398af77e` 2
  (section 0c). The 2 old slices are no longer asked on `roof_training_250-30371`. The pre-existing pull conflict on
  `addr-scene-roof_training_250-30371` is the same scene's old item and is unaffected (reported again at 07:47Z).
- `lot-29772` — CAPTURED, carried and pushed; stays as it is. Row: identity `lot`, `carry_from` `house-29785`, `address_id`
  29772 ("11 CAMBRIDGE ROAD, Mooroolbark VIC 3138", lot pfi 1329882), 1024 px, span 100.288 m, `covered_by_existing`
  [`house-29772`, `house-29846`, `mooroolbark-50011498-17-118442-80416`]. BOTH `roof_training_250-29772` and `house-29772`
  exist under `data/scenes/` (the pilot's pair `roof_training_250-29772 -> house-29772` is in `pilot-2026-09-30.json`), so
  under the H rule this window would have been `win-*` with `reserved_by` [`house-29772`, `roof_training_250-29772`]. It
  was not: the plan predates the rule and the check at batch time saw only `house-29785` (address 29785) as a source. The
  directory `data/scenes/lot-29772/` therefore holds a window whose carry source is `house-29785`'s two frame-cut slices
  (`7ddfb55acd70b9fa`, `4e615c2e23cb5015`; verified, fraction 0.276707, region 625.5 px, shift 0.0014 m, ncc 0.997; pushed as
  `addr-scene-lot-29772-m…-r1` in run `20261001T070709Z-5da0381e`). Consequence: an own-lot recapture of address 29772
  (`task recapture:capture ADDRESS=29772`, carry-from `roof_training_250-29772`) can no longer take that name — the
  wrapper "never overwrites a scene" (Taskfile.yml:962) and `scene_focus` marks an existing directory `exists_skip`. The
  module's docstring (:43-46 at sha `ed9bcc4e…`, :45-46 at the live sha `563c26f2…`) records exactly this: "lot-29772 (house-29772 exists) was captured and
  pushed and stays as it is". Nothing is renamed or rewritten: the window is an existing scene with the reviewer's
  `review.json`, its registry rows and its Fieldwork item name it, and the count does not depend on the id family (design
  §9 decision 4: "the count does not depend on it, the registry does"). Whether a `lot-29772` own-lot recapture is ever
  wanted (its source `roof_training_250-29772` is a pilot pair against the existing `house-29772`, not a frame-cut target)
  is open and is listed in section 7, not decided here. At the second executed-state refresh it is RECORDED AS A REVIEWER
  NOTE: DESIGN amendment 4 `recorded_collisions.kept` ("captured, carried and pushed 2026-10-01T06:49Z before the rule; left as it
  is"), the hold list's identity note and this section; the identity does not affect the count (the registry rows and the
  Fieldwork item name the directory as it is; design §9 decision 4); whether to leave it or to withdraw and re-plan it under a
  `win-*` name is the reviewer's decision, not made here (section 7.0b item 7).

## 7. What is done, what is not, and why (re-read against the tree at refresh time; executed state appended at 07:38Z)

### 7.0 Executed state (2026-10-01T07:38Z): what remained then (superseded where section 7.0b says so)

Done by the operator streams A1, A2, B (section 0b records every figure and file): RULES entries (12 amendments at
`df42a3e5…`), DESIGN `subject_recapture.min_overlap_px` 128 and WINDOW amendment (`da322027…`, `43538152…`); the dated
plan `window-plan-2026-10-01.jsonl` (49 windows, offline); rehearsal B; 48 windows captured, verified and carried (78
copies, 4 uncarried, 1 decided_on_source; registry 82 rows); counters before / after / final (N 13 unchanged, 80
targets); Fieldwork push run `20261001T070709Z-5da0381e` (48 created, 0 marked) and pull (cursor 638, pre-existing
conflicts); the window worklist. Remaining, in order of the count path:

1. (DONE in run H at 07:45:43Z, section 0c.) The held `lot-30381` group: `win-6dbb766c398af77e` of `window-plan-2026-10-01b.jsonl` is planned, not captured (gate
   closed on memory 3x at 07:38Z); capture -> verify -> carry -> counter -> push of that one new window (section 6.4). Until
   then the 2 old slices stay asked on `roof_training_250-30371` and `targets_remaining` reads 80.
2. Reviewer decisions on the 78 carried copies: none existed at 07:11Z (`carried_primary` 0; every `carried_rows` entry
   `carried_undecided`). The count stays "n/a (N=13, roof=8, not_roof=5, scenes=3)" against 30 / 8 / 6 until decisions are
   saved; the worklist lists one editor URL per copy. Nothing here predicts how many will be decided or how.
3. Detection on the windows (step 8): not run; every window's model layer is empty; it needs `jobgate 7500` open, no
   `tsc` / web build, `/usr/bin/time -l` for the first footprint (section 1.4). Optional for the count (the carried slices
   are decided on imagery alone, design §9 decision 8 default).
4. TS decision-carry wiring (step 9, section 3): deferred to the quiet window of section 1.3; not on the count path; the
   Python target carry sufficed for the 48 windows (0 `src/` or `.mjs` files changed, integrity 07:10:44Z).
5. Cohort displacement: RECORDED, not applied — 21 fitted sources carry `frame_transform.source_displacement` {method
   `imagery-paste-rounding`, `native_px` [0.4444, 0.4444], `native_zoom` 19, `applied` false, `approx_m` 0.1049}; no
   `pixel_affine` was composed with it; a correction would be a frame-transform-v2 re-measure (design §9 decision 5).
6. The pre-existing pull conflict on `addr-scene-roof_training_250-30371` (6 fields of `d7f7ceb84a6d0db4`,
   `5d1c61a0f7ef49d9`, `133736ada6ace260`): reported on every pull, resolution is the reviewer's; one of those three
   (`d7f7ceb84a6d0db4`) now also has a carried copy on `win-687c9f594f1601ac`, the other two are the held group's targets.
7. Fieldwork item shape: the pushed items carry the title form but no `lot-window` tag and no `generation.windowOf`
   (section 4); recorded, not reconciled; items are immutable.
8. The `lot-29772` identity collision (section 6.4): recorded; the window stays; whether an own-lot recapture of 29772 is
   wanted is open.
9. (DONE: WRITTEN at 07:46:34Z by stream H, sections 1.2 and 0c.) The hold-list file of section 1.2 was never generated as of 07:38Z (the worklist carries the sentence); 69 old slices remain
   `fieldwork_pending` drafts on their source frames, excluded from the count unless accepted locally.
10. Stream T's RULES narrowing (07:36:04Z, `738ed82a…`) and stream H's `scene_focus.py` edit (`ed9bcc4e…`) landed after run
    B; the batch-time shas are recorded beside them; the next counter snapshot will carry the new `rules_sha256` and
    `code_sha256`, each named here.

### 7.0b Executed state (2026-10-01T08:42Z; read from the files named in section 0c): what remains

Done since 07:38Z: run H (the 49th window `win-6dbb766c398af77e` captured, verified, carried, pushed `20261001T074717Z-cd457ee0`,
pulled to cursor 639; registry 84 rows / 80 copies / 49 windows); the hold list `frame-cut-hold-2026-10-01.md` (07:46:34Z);
stream C's design-sha chain (`scripts/ml/design_chain.py`, wired into `scene_recapture`, `scene_focus` and `r5_precision`; 37 /
39 / 36 tests, ruff clean), DESIGN amendment 4 `identity` (08:24:41Z, `2f48e429…`) and RULES entries 14-15 (08:28:04Z,
`97590a2a…`); counters H and C (N 13 unchanged, 80 targets, all 80 copies joined, from C-after through `design_sha_via_chain`).
Items 1 and 9 of section 7.0 are DONE; item 10's shas are recorded in section 0c. Remaining, in order of the count path:

1. Reviewer decisions on the 80 carried copies: none is recorded in any counter read here (`carried_primary` 0; every
   `carried_rows` entry `needs_adjustment`, `primary` false at 08:25:16Z). The count stays "n/a (N=13, roof=8, not_roof=5,
   scenes=3)" against 30 / 8 / 6 until decisions are saved; the worklist lists one editor URL per copy and the hold list one per
   old slice. Nothing here predicts how many will be decided or how.
2. Detection on the windows (step 8): not run (one `models/*.json` per window dir, no detect log); it needs `jobgate 7500` open,
   no `tsc` / web build, `/usr/bin/time -l` for the first footprint (section 1.4). Optional for the count (design §9 decision 8
   default); run H alone waited through 17 closures of the smaller 1500 MB gate.
3. TS decision-carry wiring (step 9, section 3): deferred to the quiet window of section 1.3; not on the count path.
   `src/lib/scene-visibility.ts`, `roof-feedback-patch.ts`, `scene-instances.ts`, `roof-line-edits.ts` and
   `scripts/test-shade-sail.mjs` have mtimes after 18:11 AEST (18:11:15, 18:19:40, 18:11:15, 18:14:55, 18:56:40): outside every stream's file list;
   no file records the reviewer as their author. They fall inside the shade-sail Codex session's window (`data/reviews/eyes/shade-sails-2026-10-01/`,
   files 18:00-18:59:59 AEST), whose `report.json` records `shade_sail_regression` and `typescript` passed at 08:59:59Z (`Taskfile.yml:103` runs
   `scripts/test-shade-sail.mjs`); `report.json` names no source file, so they are attributed to that session's window on this evidence only,
   not to the reviewer (corrected 09:24Z).
4. Cohort displacement: RECORDED, not applied — 22 fitted sources (21 in run B + `win-6dbb766c398af77e`) carry
   `frame_transform.source_displacement` {method `imagery-paste-rounding`, `native_px` [0.4444, 0.4444], `native_zoom` 19,
   `applied` false, `approx_m` 0.1049}; no `pixel_affine` was composed with it; a correction would be a frame-transform-v2
   re-measure (design §9 decision 5).
5. The pre-existing pull conflict on `addr-scene-roof_training_250-30371` (6 fields of `d7f7ceb84a6d0db4`, `5d1c61a0f7ef49d9`,
   `133736ada6ace260`): reported on both pulls; resolution is the reviewer's; all three keys now have carried copies
   (`d7f7ceb84a6d0db4` on `win-687c9f594f1601ac`, the other two on `win-6dbb766c398af77e`).
6. Fieldwork item shape: the 49 pushed items carry the title form but no `lot-window` tag and no `generation.windowOf` (section
   4); recorded, not reconciled; items are immutable.
7. The `lot-29772` identity collision: recorded as a reviewer note (DESIGN amendment 4 `recorded_collisions.kept`, the hold list,
   section 6.4); the window was captured, carried and pushed before the reservation rule; the identity does not affect the
   count; the reviewer decides whether to leave it.
8. RULES `recapture_design.identity` still holds the old one-line text while DESIGN `identity` carries the two-family rule: a
   later dated RULES entry (`previous_sha256` `97590a2a…`) is needed to mirror DESIGN amendment 4; not written here.
9. Design-sha chain residuals (recorded, not refutations): (a) `design_chain`'s docstring says the callers compare the design
   values beside the sha, but only `scene_recapture.verify` does — `r5_precision.registration` / `carry_refusals` and
   `scene_focus.capture_window` / `carry_window` accept on the sha alone, so the counter hop trusts the human-written
   `thresholds_changed: false` flag of the amendment it hops through (a mis-flagged amendment that moved a threshold would be
   caught by `verify`, not by the counter); (b) RULES entries 14-15 add a `code_sha256` key the earlier entries lack
   (`load_rules` accepts it); (c) RULES.json was written twice by stream C (08:27:10Z, sha `5c2e6c73…`, withdrawn by restoring the
   byte copy `738ed82a…`; 08:28:04Z, `97590a2a…`): the file holds the second write only and the chain `previous_sha256` is the
   frozen sha, but the first write and its restore are a frozen-file write outside the dated convention, recorded here
   (`applied-rules-chain-2026-10-01.first-write-withdrawn.json`); (d) the registry has no before / after sha pair across stream
   C — "untouched" rests on its mtime (17:45:47 AEST, before C's first write at 18:21) and its 84 rows.
10. The 69 old slices still `fieldwork_pending` on their source frames are excluded from the count unless accepted locally;
    the hold list asks the reviewer to leave the 80 recaptured ones undecided there. The worklist of 07:10Z still lists
    `lot-30381` as held (:487-491 of the live file — which WAS rewritten after 07:10Z: notes at lines 3 and 7, sha `e72130c5…`, mtime 09:04:37Z; the held entry stands); the hold list and section 0c are the current record.
11. (Read 08:57Z, after this refresh's main reads.) Stream G's `scene_focus carry --repair-groups` (sha `563c26f2…`, 18:49:29
    AEST) rewrote 8 existing window `review.json` files at 08:50:43-45Z to add the group records the editor's save validator
    requires (a window review with `groups` [] returned 400 "Choose an existing object group."), keeping revision 1 and appending
    8 `carry_repair` registry rows (registry 92 rows); `counter-G-after.json` 08:52:13Z: six gated fields SAME, 80 copies joined,
    `targets_remaining` 80. These are writes to EXISTING scenes by an operator stream — outside this runbook's "never" list and
    outside its own file list; recorded here with their sources (section 0c last row), not judged. Then, at 08:54:38-39Z, a Codex script of ANOTHER session —
    `data/reviews/eyes/shade-sails-2026-10-01/save_classifications.py`, POSTing to the local app, which stamps `updated_by` "local" — saved
    `lot-29772` and `house-29785` at revision 2; NOT the reviewer (corrected 09:24Z; the earlier wording here was wrong): on the two copies it set
    `structure_kind` `shade-sail`, `review_issues` [`outline`], `provenance.source` `machine:codex-shade-sail-classification` and a Codex note, rings and
    `status` unchanged; both `lot-29772` copies read `needs_adjustment` at 08:57Z. The SAME script then wrote LIVE to Fieldwork — item
    `addr-scene-house-29785-me373cb0a9d78-r0` revision 2 -> 3 at 08:54:40Z (`fieldwork-request.json` / `fieldwork-response.json` /
    `fieldwork-after.json`; `report.json` `fieldwork` before_revision 2, saved_revision 3) — outside every stream of this runbook. A fresh counter at
    09:15:00Z classes the two copies `machine_label`: `targets_remaining` 78 (`counter-G-after.json` 08:52:13Z: 80), N / read / `targets_total`
    otherwise SAME (section 0c last row). A window
    review saved by the reviewer (revision above 1) is refused by the repair by name, so the remaining 41 windows' reviews can
    be repaired only while unsaved; whether to run the repair on them is the operator's call, not made here.

### 7.1 As read at refresh time (05:34Z; superseded where section 0b says so)

Landed (section 1.6 has line numbers and shas):

- `scene_region.frame_px` (512 default, 1024 allowed); the `r5_precision` lineage hop, one-decision rule,
  `recaptured:<window>` exclusion and `--recapture-dir`; `scripts/ml/scene_focus.py` with `register`, `plan` and
  `capture`, and the Taskfile wrappers `focus:plan` / `focus:capture`; `test_scene_focus.py` (29 tests) and the hop tests
  in `test_sam3_mac_precision.py` (31). The step-1 gate snapshots exist: `.run/focus-recapture/counter-before.json`
  (04:08:11Z) / `counter-after.json` (04:21:47Z) / `counter-after2.json` (04:56:44Z), six fields identical, `code_sha256`
  `dca5927d…` -> `fa1b3762…` -> `f6496653…`, `rules_sha256` unchanged, N = 7 and `targets_remaining` 3 in all three and
  in the refresh counter (05:21:21Z).
- The window rule is pre-registered: `data/reviews/eyes/recapture/WINDOW.json` (05:02:09Z, sha `2d7c4a5b…`, written by
  `scene_focus register`; section 1.5). `plan` refuses without it and `capture` refuses a row planned under another sha.
- Three OFFLINE plan DRY RUNS under `.run/focus-recapture/`: `plan-2026-10-01.run1.jsonl` (04:28:01Z, 38 windows),
  `plan-2026-10-01.jsonl` (04:31:00Z; 42 windows for 86 targets, 23 `below_design`; written before WINDOW.json, no
  `window_sha256`, so no row of it is capturable now) and the current `plan-2026-10-01.v2.jsonl` (05:02:27Z; 49 windows for
  86 targets, 29 `below_design`, 4 `uncarried_targets`, lot 8 / win 41, `frame_px` 512: 20 / 1024: 29; totals in step
  3). Every one is a record under `.run/`, not the dated `data/` plan steps 4-5 read.
- One rehearsal capture on `qa_focus_3c7647c8_north` (`.run/focus-recapture/rehearsal-qa_focus_3c7647c8_north.json`,
  file mtime 14:31 AEST, from the first plan: verified true, overlap 0.88494, cache-only); no `data/scenes/qa_*`
  directory exists now (562 scene dirs at refresh).
- The three RULES amendment TEXTS are drafted, not applied: `.run/focus-recapture/rules-amendment-2026-10-01.json`
  (`written_at` 04:22:34Z, file mtime 14:57 AEST; `from` read from RULES.json at sha `d7613249…`, 7 amendments; the file
  states it "does not edit RULES.json"). RULES.json's sha is unchanged at refresh.
- The CP153157 wording in `scene_focus.py` (:14-15, :166-167) and `test_scene_focus.py` (:215-217) now cites the measured
  311.28 m bounding span and its source (section 1.6); `pytest scripts/ml/tests/test_scene_focus.py -q` 29 passed, `ruff
  check .` clean at refresh.

Not done AT 05:34Z (every item below is now DONE or superseded by sections 7.0 / 7.0b, except the TS wiring, the detection footprint
and the "six gated fields" note; kept as the record of that read):

- No carry, registry or RULES / DESIGN amendment writer: `scene_focus.py` has no `carry`, `registry` or `preregister`
  subcommand (:1132-1150 define `register`, `plan`, `capture`) and `scripts/ml/scene_window.py` does not exist; step 7
  and the RULES / DESIGN part of step 2 stay PROPOSED. `human_precision.lineage` in RULES.json is still null: the hop
  CODE is landed, the RULES text that lets a carried decision join is not.
- No RULES entries and no DESIGN identity entry written: `data/reviews/eyes/recapture/` holds DESIGN.json (2
  amendments), offset-diagnosis-2026-10-01.md, pilot-2026-09-30.json, plan-2026-09-30.jsonl and WINDOW.json at refresh
  (`ls`); the RULES / DESIGN entries wait for reviewer decisions 1-3 and must precede the first capture (WINDOW.json,
  which `plan` needs, is already in place).
- No hold list (`frame-cut-hold-2026-10-01.md` absent): section 1.2 gives the generator; it is the first act once the
  reviewer agrees (decision 7).
- No dated `data/reviews/eyes/recapture/window-plan-*.jsonl`, no real capture (no `lot-*` / `win-*` directory under
  `data/scenes`), no detection, no carry, no registry row (`windows-*.jsonl` absent), no push, no pull: every one is
  gated above.
- No measured SAM3 extraction footprint exists on this host (section 1.4): the first detection measures it.
- The counter's "six gated fields" are not defined in any file found; section 6.2 names the set this runbook uses and
  says so.
- Overlap refusals (design risk 1) are now counted, not assumed: 29 of 49 windows are `below_design` in the v2 dry run
  (23 of 42 in the first), the mooroolbark tile and `lot-29772` among them; they wait for decision 1. The plan also lists one target as `not_frame_cut`
  (`map-806de95f666a01c7` / `815d84c355fa7b73`), so the 86-row hold list of section 1.2 (from `/tmp/frame-cut-targets.json`)
  and the plan's 85 subjects differ by that row; the plan's reading is the module's (`touched_edges`, scene_focus.py:213,
  with `WINDOW['frame_tolerance_px']`) and is recorded here, not reconciled.
- The TS decision-carry wiring stays deferred to the quiet window; it is not on the count path (the Python target
  carry suffices).
- Designer-reported figures (C's overlap counts 6/18 and 12/18, cohort 4/12 and 9/12; B's bodies 60/86, lots 28/86,
  0 local addresses for the 11 inner-Melbourne frames) were not re-measured here; the offline plan's `totals` (step 3)
  are the module's own counts.

## 8. Sources (file:line; read at write time, re-read at repair time and at refresh time where marked)

- `.run/focus-recapture-design-2026-10-01.md` §1-10 (the design); `.run/round5-readiness-2026-10-01.md:235-262`
  (fine-tune MPS driver 6,492-7,324 MB; `jobgate 7500` closed at 23:41Z); `.run/jobgate.sh` (gate rule and `pgrep`
  pattern); `.run/expand-house-scenes.log`, `.run/expand-house-29785.log` (no memory lines); `.run/ml.log` tail;
  `.run/w4int-pytest.log:134`; `.run/w1fix-triple-gate.py` (gate fields); `.run/fieldwork-path/push-dry-run-14.stdout.txt`,
  `push-live-14.stdout.json`, `pull-live-2.json`; `.run/focus-recapture/counter-runbook-20261001T041307Z.{json,time}`;
  at repair time: `.run/focus-recapture/counter-before.json` (04:08:11Z), `counter-after.json` (04:21:47Z),
  `plan-2026-10-01.jsonl` + `.summary.json` (04:31:00Z), `rehearsal-qa_focus_3c7647c8_north.json`,
  `rules-amendment-2026-10-01.json` (04:22:34Z); at refresh time: `counter-after2.json` (04:56:44Z),
  `plan-2026-10-01.run1.jsonl` (04:28:01Z), `plan-2026-10-01.v2.jsonl` + `.summary.json` + `.stderr` (05:02:27Z),
  `counter-runbook-20261001T052115Z.{json,time}` (05:21:21Z); `docs/handover-2026-10-01.md:217-221` (149 / 11 exclusions after the pull).
- `Taskfile.yml` (repair time): `eyes:guard` :230-233, `eyes:small-roofs` :934-937, `training:sam3-mac:human-precision`
  :939-942, `recapture:plan` :949-953 (guard :952), `recapture:pilot` :955-959 (guard :958), `recapture:capture` :961-965,
  `focus:plan` :967-972, `focus:capture` :974-979, `verify:scene-recapture` :996-999, `pull` :661-665, `push` :677-680,
  `push:status` :682-685, `scene:extract` :526-529, `WEB_PORT` :8 (all re-read at refresh); `silent: true` at :30 and :414 only; go-task 3.53.1 echoes each command line to stderr (section 1.1).
- `scripts/ml/scene_recapture.py` (repair time): `METHOD` :82, `FAILURE` :84, `EDGE_PX` / `MIN_SIDE_PX` :95-96, `AGREE`
  :98, `overlap` :268, `frame_transform` :278, `project_object` :321, `residual` :534, `load_design` :590, `verdict`
  :610, `measured_transform` :628, `carry_source` :656, `verify` :688, CLI :1349-1364 (`--verify-source`,
  `--verify-target`, `--root`; `plan --cohort --offline --out`; `pilot --pairs auto --out`), dispatch :1365-1403 (1405
  lines), plan carry predicate :1195-1206 (write time).
- `scripts/ml/scene_lot.py` (repair time): docstring :1-28, `GROUPS`/`FRAME_PX`/`DESIGN` :66-69, `PARCELS_DIR` :70,
  `COHORT_NATIVE_ZOOM` :74, `UnverifiedRecapture` :77, `lot_source` :289, CLI :328-341.
- `scripts/ml/scene_region.py` (repair time, `frame_px` landed): `region_frame` :18-65 (`frame_px` check :22-23, zoom
  `frame_px/256/span` :46, identity :60, frame matrices :64-65), `capture_region` :68-94 (`frame_px` :68, :84, :91).
- `scripts/ml/scene_focus.py` (refresh time, 1193 lines, sha `67afcbca…`): docstring :1-53, `VERSION` / `PLAN_VERSION`
  :101-102, `FRAME_PX` / `SCRATCH_PREFIX` :105-106, `WINDOW_PATH` :108, `WINDOW` :114, `DISPLACEMENT` :122, `parcel_class`
  :162, `metre_frame` :207, `touched_edges` :213, `_span` :247, `write_window_rule` :771 (exists refusal :781),
  `load_window_rule` :786 (missing :793, mismatch :796-797), `window_row` :829-902 (`overlap_gate` :873, `dedupe` :880,
  shas :900), `plan` :904 (`--out` refusal :909, `load_window_rule` :912), `CacheOnlyFetcher` :1021, `capture_window`
  :1062-1115 (design sha :1068-1070, window sha :1071-1074, scratch prefix :1075-1076, exists :1079-1080,
  `subject_exceeds_window` :1081-1082, overlap gate :1084, snapshot :1088-1090, `source_displacement` :1095, `save_scene`
  :1115), `main` :1132-1189 (argparse :1135-1150: `register` :1135-1136, `plan` :1137-1143, `capture` :1144-1150;
  `register` dispatch :1153-1157), `if __name__` :1192.
- `scripts/ml/scene_extract.py` (repair time): CLI :373-392 (`--scene-id` :381, `--capture-only` :383), lock :432,
  `save_scene` :327-332 (recomputes `model_signature`); `--scene-id` :456-463, `--capture-only` :465-468, run :470-479
  (write time).
- `scripts/ml/fieldwork_push.py:1-7`, :146-158 (push flags); `scripts/ml/fieldwork_feedback.py:20-22` (defaults), :397-412 (pull flags).
- `scripts/ml/training/sam3_mac/r5_precision.py` (refresh time; 1577 lines; sha256 `f6496653…`): docstring :1-47
  (lineage paragraph :19-35), constants :66-101 (`N_RULE` :78, `LINEAGE_METHOD` :84, `CARRY_REFUSALS` :85, `NOT_COUNTED`
  :86, `RECAPTURE_DIR` :87, `REGISTRY_GLOB` :88, `URL` :95), `sha256_file` :140, `load_lineage` :344, `registered` :368,
  `registry_windows` :381, `carry_refusals` :387, `_hops` :411, `join` :431, `_target_exclusion` :843, `_copy_item`
  :863, `targets` :871 (`recaptured:` :920), `code_sha256` = `sha256_file(__file__)` :1027, `count` :1044, CLI `main`
  :1515 (`--recapture-dir` :1526, :1541), `if __name__` :1576.
- `scripts/ml/tests/test_scene_focus.py` (29 tests at refresh; :116, :214, :299, :352, :362, :457, :528, :560, :574, :587,
  :603, :613); `scripts/ml/tests/test_sam3_mac_precision.py` (31 tests; :884, :901, :916, :961, :1046, :1173);
  `scripts/ml/tests/test_scene_region.py` (5 test functions, 14 collected by `pytest --collect-only -q` (parametrised), 22 Sep); `scripts/ml/tests/test_scene_recapture.py:118`.
- `scripts/ml/review_authority.py` (blockers, `carried` :21, `carried_undecided` :31); `scripts/ml/scene_lot_tags.py`
  (`registration` :120, `fetch_frozen` :212, `parcels_for_scene` :219); `scripts/ml/scene_expand.py:64` (`touches_frame`);
  `scripts/ml/scene_context.py:32-33` (512 only); `scripts/ml/eyes_guard.py:1-15`, :305 (`--write`).
- `src/app/api/scenes/route.ts:12-45` (GET by id; POST `saveScene(body.id, body.review, principal)`);
  `src/app/scenes/page.tsx:1235` (today's two banner sentences); `scripts/verify-scene-recapture.mjs:14-35`, :245-249;
  `scripts/test-scene-recapture.mjs:1-30`.
- `docs/recapture-carry-2026-09-30.md` §2 :38-64 (contract), §3 :66-80, §4 :82-106, §5 :108-133, §6 :135-176;
  `docs/handover-2026-10-01.md` §1 :10-21, §5 :81-90, §8 :124-226; `docs/annotation/local-to-fieldwork.md:30-140`;
  `docs/annotation/feedback.md:256-270`; `data/reviews/eyes/fieldwork-review-2026-10-01/REVIEW.md`;
  `.opencode/skills/addr-3d/SKILL.md:660-668`, :872-884.
- `data/reviews/eyes/recapture/DESIGN.json`; `data/reviews/eyes/recapture/WINDOW.json` (refresh; sha `2d7c4a5b…`);
  `data/scene-parcels/3720833bebf70009951c235ac6006adde841a5f5862c56259f17cb1d65a41cf6.json` (the CP153157 span, 311.28 m);
  `data/reviews/eyes/training/sam3-mac/round5/RULES.json`
  (`human_precision` fields read; `amendments` fields: ndsm_leg..., written_before_note, agent_text_regex,
  fresh_marker_regex, agent_fresh_decision, joined_not_counted, outputs).

Executed-state refresh (read 2026-10-01T07:34-07:38Z; every figure in sections 0b, 1.2, 1.5, 1.6 "Landed SINCE", 2 steps
1-12, 3, 4, 5, 6.1, 6.2, 6.4 and 7.0 comes from one of these):

- `.run/focus-recapture/capture-2026-10-01.log` (214 lines at 07:38Z: run B header :1-2, dated plan :3-16, rehearsal :17-28,
  batch :29-156, counter :157-170, push :171-187, headless check :188-189, integrity :190-200, receipts :201, final counter
  :202-204, run H :205-214); `run-captures-b.sh`, `post-batch-b.sh`, `write-worklist-b.py`; `captures/<window>.{capture,
  verify,carry}.{json,stderr}` (288 files); `rehearsal-b-qa_focus_b_1790837065.{capture,carry,api}.json`, `rehearsal-b.qa_id`.
- `.run/focus-recapture/applied-rules-2026-10-01.json` (A1, 06:06:40Z; `RULES.before-A1.json`), `counter-A1-before.json`
  (05:56:24Z), `counter-A1-after.json` (06:05:39Z); `apply-design-amendment-2026-10-01.py`, `amend-2026-10-01.stdout.json`,
  `applied-design-2026-10-01.json` (A2, 06:15:54Z), `a2-step{1,3-recapture,4-focus}.log`; `plan-2026-10-01.v3.{jsonl,summary.json,stderr}`
  (06:14:38Z), `frame-cut-targets-2026-10-01.json`; `plan-dated-2026-10-01.{stdout.json,stderr}` (06:43:30Z);
  `counter-B-before.json` (06:38:45Z), `counter-B-after.json` (07:05:46Z), `counter-B-final.json` (07:11:06Z);
  `push-dry-run-b.stdout.json`, `push-live-b.stdout.json`, `push-status-b.json`, `pull-b.stdout.json`, `window-ids-b.txt`;
  `api-lot-29772.json`, `api-win-2076df0d53bc99b2.json`; `applied-rules-T-2026-10-01.json` (07:36:04Z; `RULES.before-T.json`,
  `apply-rules-amendment-T-2026-10-01.py`), `counter-T-after.json` (07:36:44Z); `targets-held-30381.json`,
  `plan-dated-2026-10-01b.{stdout.json,stderr}` (07:37:16Z); module copies `scene_focus.pre-a2-67afcbca.py`,
  `scene_focus.pre-b-01d84d72.py`, `scene_focus.pre-h-9ccebab4.py`, `scene_recapture.pre-a2-f831b925.py`, `Taskfile.pre-{b,h}.yml`,
  `test_scene_focus.pre-{a2,b,h}.py`, `test_scene_recapture.pre-a2.py`.
- `data/reviews/eyes/recapture/window-plan-2026-10-01.jsonl` (header + 49 rows; sha `4f198b26…`), `window-plan-2026-10-01b.jsonl`
  (header + 1 row; sha `d177906d…`), `windows-2026-10-01.jsonl` (82 rows; sha `1eb6dee8…`), `DESIGN.json` (`da322027…`, 3
  amendments, `subject_recapture` block), `WINDOW.json` (`43538152…`, 1 amendment), `data/reviews/eyes/training/sam3-mac/round5/RULES.json`
  (`738ed82a…`, 13 amendments at 07:38Z; `df42a3e5…`, 12 during run B); `data/scenes/` (610 dirs; 48 `lot-*` / `win-*`, 7 `lot-*`,
  0 `qa_*`; `roof_training_250-29772`, `roof_training_250-30381`, `house-29772` exist; `lot-30381` absent).
- `data/fieldwork-sync/mooroolbark/push/runs/20261001T070620Z-d4ab79b9/` and `20261001T070709Z-5da0381e/{plan.json,result.json,
  progress.ndjson,items/*.request.json,items/*.response.json}` (96 item files); `data/fieldwork-sync/mooroolbark/push/scenes/` (62
  receipts in total, 48 of them run B's).
- `data/references/eyes-checkpoints/WINDOW-WORKLIST-2026-10-01.md` (07:10:06Z; sha `d9b71970…` at that read; lines 1-23 and 483-492 read; rewritten since — live `e72130c5…`, mtime 09:04:37Z, section 0b);
  `REMARK-WORKLIST-2026-10-01.md` (sha `77cb0c5e…`, unchanged).
- `scripts/ml/scene_focus.py` at sha `9ccebab4…` (1427 lines; `carry_window` :1224, `main` :1349-1427, `registry_path` :1183,
  `read_registry` :1188, `decided_on_source` :1206, `_registry_row` :1213, `CARRY_NOTE` :1177, `DROPPED_FIELDS` :1179) and at
  `ed9bcc4e…` (1459 lines, 07:37:42Z; docstring :36-46, `RESERVED_PREFIXES` :218, `IDENTITY_RULE` :219, `reserved_by` :227,
  identity branch :890-903, row `reserved_by` :958, `plan` :979 (`--out` refusal :984, `load_window_rule` :988), totals :1049-1080,
  `capture_window` :1145-1201, `carry_window` :1256, argparse `carry` :1388-1393); `scripts/ml/scene_recapture.py` (sha `7830c460…`,
  1483 lines; `METHOD` :93, `SUBJECT_ROLE` :100, `EDGE_PX` :108, `MIN_SIDE_PX` :109, `overlap_region` :288, `overlap` :300,
  `frame_transform` :311, `project_object` :357, `residual` :570, `load_design` :626, `verdict` :666, `measured_transform` :697,
  `carry_source` :727, `verify` :759); `scripts/ml/training/sam3_mac/r5_precision.py` (sha `9b4c363c…`, 1597 lines; `N_RULE` :81,
  `LINEAGE_METHOD` :87, `CARRY_REFUSALS` :88, `NOT_COUNTED` :89, `RECAPTURE_DIR` :90, `REGISTRY_GLOB` :91, `CLASSES` :94, `URL` :98,
  `accepted_import` :215, `classify` :249, `load_lineage` :373, `registered` :397, `registry_windows` :410, `carry_refusals` :416,
  `_hops` :440, `join` :460, `_target_exclusion` :872, `_copy_item` :895, `targets` :903, `recaptured:` :952, `code_sha256` :1059,
  `count` :1076, `main` :1547, `--recapture-dir` :1558 / :1573); `Taskfile.yml` (sha `41191e6e…`; `focus:plan` :967-972,
  `focus:capture` :974-979, `focus:carry` :981-986, `recapture:capture` :961-965, `verify:scene-recapture` :1003); tests
  `test_scene_focus.py` (38 `def test_`; `test_identity_is_reserved_for_the_source_address…` :423,
  `test_lot_identity_is_reserved_when_a_cohort_or_house_scene_of_that_address_exists` :435), `test_scene_recapture.py` (24),
  `test_sam3_mac_precision.py` (34), `test_scene_region.py` (5).
- `.run/focus-recapture-design-2026-10-01.md` §9 (decisions 1-11, lines 433-461) for the decision numbering used above.

Second executed-state refresh (read 2026-10-01T08:36-08:48Z; every figure in the header, sections 0b (two rows), 0c, 1.1, 1.2,
1.4, 1.5, 1.6 "Landed at the second…", 2 (conventions, steps 0, 2-8, 10-12), 3, 4, 5, 6.1, 6.2, 6.4 and 7.0b comes from one of
these):

- `.run/focus-recapture/capture-2026-10-01.log` (276 lines; run H :205-276 — header :205, plan :206, capture :207-242, push
  :243-245, pull :246-247, counter :248-266, probe H7 :267-272, tests :273-275, end :276); `run-capture-h.sh`, `run-capture-h.status`
  ("done carry_rc=0"), `write-hold-list-h.py`, `targets-held-30381.json`, `plan-dated-2026-10-01b.{stdout.json,stderr}`;
  `captures/win-6dbb766c398af77e.{capture,verify,carry}.{json,stderr}` (294 files in all); `push-dry-run-h.stdout.json`,
  `push-live-h.stdout.json`, `push-status-h.json`, `pull-h.stdout.json`; `counter-H-after.json`, `counter-H-design-probe.json`;
  `design-amendment-identity-2026-10-01.json` (the draft, 07:38:36Z).
- Stream C: `apply-design-identity-amendment-2026-10-01.py` -> `applied-design-identity-2026-10-01.json` (08:24:41Z),
  `DESIGN.before-identity-2026-10-01.json`; `apply-rules-amendment-chain-2026-10-01.py` -> `applied-rules-chain-2026-10-01.json`
  (08:28:04Z) and `applied-rules-chain-2026-10-01.first-write-withdrawn.json` (08:27:10Z), `RULES.before-chain-2026-10-01.json`;
  `counter-C-before.json` (08:24:41Z), `counter-C-after.json` (08:25:16Z), `counter-C-final.json` (08:28:11Z) and their `.stderr`
  (`/usr/bin/time -l`).
- `data/reviews/eyes/recapture/`: `DESIGN.json` (`2f48e429…`, 4 amendments; `identity`, `amendments[3]`),
  `window-plan-2026-10-01.jsonl` (`4f198b26…`), `window-plan-2026-10-01b.jsonl` (`d177906d…`), `windows-2026-10-01.jsonl`
  (`a0073fc3…`, 84 rows), `frame-cut-hold-2026-10-01.md` (`cee08c91…`), `WINDOW.json` (`43538152…`);
  `data/reviews/eyes/training/sam3-mac/round5/RULES.json` (`97590a2a…`, 15 amendments; `recapture_design.identity`,
  `human_precision.join_order` / `targets`); `data/fieldwork-sync/mooroolbark/feedback.json` (`cursor` 639),
  `push/runs/20261001T070709Z-5da0381e/result.json` and `20261001T074717Z-cd457ee0/result.json`; `data/scenes/` (611 dirs; 49
  `lot-*` / `win-*`; `win-6dbb766c398af77e/{latest.json,review.json,models/19fdaf1d….json}`);
  `data/references/eyes-checkpoints/WINDOW-WORKLIST-2026-10-01.md` (`d9b71970…` at the 08:42Z read; lines 10 and 480-492 re-read; at 09:24Z the file is `e72130c5…`, 66,185 bytes, mtime 09:04:37Z, notes at lines 3 and 7) and
  `REMARK-WORKLIST-2026-10-01.md` (`77cb0c5e…`).
- `scripts/ml/design_chain.py` (`742bac9b…`), `scene_recapture.py` (`e5dccfca…`), `scene_focus.py` (`c3d50d3a…` at 08:42Z; live `563c26f2…`, every cite re-pinned 09:24Z),
  `training/sam3_mac/r5_precision.py` (`9c4dd2e1…`), `scene_lot.py` (:77, :331-341), `scene_extract.py` (:327, :381),
  `scene_lot_tags.py` (:212, :219), `scene_self_review.py` (:179); `Taskfile.yml` (`e52a6380…`; `focus:plan` :968-973,
  `focus:capture` :975-980, `focus:carry` :982-987, `recapture:capture` :962-966, `recapture:plan` :950-955 (guard :953),
  `recapture:pilot` :956-961 (guard :959), `eyes:small-roofs` :935, `training:sam3-mac:human-precision` :940,
  `verify:scene-recapture` :1004, `pull` :662, `push` :678, `push:status` :683, `eyes:guard` :231, `scene:extract` :527); tests
  `test_scene_focus.py` (39 `def test_`), `test_scene_recapture.py` (26), `test_sam3_mac_precision.py` (36); `ruff check` at 08:42Z.
- My own read-only runs at 08:36-08:48Z: `design_chain.read` on the live DESIGN.json; the section 6.2 six-field comparison on four
  adjacent counter pairs; `scene_recapture --verify-source … --verify-target …` on `win-6dbb766c398af77e` under `/usr/bin/time -l`;
  `shasum -a 256`, `wc -l`, `grep -n` / `grep -c`, `ls -la`, `find -newermt`, `json.loads` over the registry, both plans and the
  counter files.
