# Mooroolbark inventory — 27 September 2026

This is the initial source inventory and migration design. Its application-gap
assessment records the pre-import implementation. See the
[parity report](mooroolbark-parity.md) and [sync guide](sync.md) for the delivered
adapters, upload verification and revision-checked MCP writes.

The exact geographic scope is **506 current scene frames** intersecting the saved official Mooroolbark polygon (PFI 205409730), including all **257 completed frozen-run tiles**. Preserve complete scenes and their context; do not clip the underlying images, erase halo detections, or confuse scan completion with review approval. The machine-readable inventory lists every selected/excluded scene, schemas, relationships and measured counts.

| Data | Measured scope |
| --- | --- |
| Selected scene files | 1,986 files; 1,823,384,197 bytes |
| Images inside selected scene directories | 507 PNGs; 785,502,790 bytes |
| Immutable model snapshots | 540; 441,058,753 bytes |
| Current model objects | 43,297, all saved as `candidate` |
| Current review files | 143; 160,162,088 bytes; 74 historical revisions |
| Objects in current reviews | 12,124: 11,583 candidate, 296 confirmed, 90 needs_adjustment, 35 superseded, 120 rejected |
| Review authorship | 74 files `machine:decision-policy-v2`; 69 files `local` (not proof of human authorship) |
| Reviewed geometry flags | 296; retain the source provenance, not a blanket human-approved interpretation |
| Selected scene families | 257 suburb, 201 roof-training cohort, 33 house, 15 map |
| Frozen-run owned objects | 20,109: 10,072 buildings, 7,874 trees, 2,163 solar panels |
| Confidence index | 290 scenes; 10,531 roofs (includes overlapping additional captures) |
| Eyes diagnostics DB | 353,914,880 bytes; versioned model/reference/address/decision sidecars |
| Local geo DB | 32,179 addresses +17,643 places across five downloaded regions; not a complete national dataset |
| Mooroolbark geo subset | 4,434 records labelled Mooroolbark; 4,433 points inside the official boundary; 120 locality-labelled places vs 452 boundary-contained places |
| CRM | 17 Mooroolbark metadata rows, 5 Mooroolbark features (of 8 total), 11 Mooroolbark POIs |
| Per-address roof display export | 872 Mooroolbark-address records of 964 total; 14,678,972-byte source file |
| Legacy MBTiles | Two 12-tile z16 overlays, 290,816 and 192,512 bytes; not the full suburb scene layer |
| Disposable imagery cache | 1,549,886 files; 25,467,280,057 bytes |

**The import cannot be a plain conversion to generic approved polygons.** Selected reviews contain 475 gapped objects, 6 roof-line edit objects, 27 independent roof-feedback objects, 35 replacement records, 8 derived objects, 1,089 parent links and 5,086 group memberships. The 3D state includes 15,791 model roof annotations, roof faces/lines, heights, placement, trees, cars, solar and surface settings. Image `ring`/`holes` coordinates are pixels; normalize only the editable projection. Preserve original pixel↔metre matrices, centroid, local meter/Z roof geometry and all raw JSON.

## Canonical import scope

1. Import all 506 selected scenes with `latest.json`, every immutable `models/<signature>.json`, the entire revisioned `review.json`, hash-bound source PNGs and `confidence.json`. Current source-image hashes verified for all 506; none of the 143 current reviews has a stale model signature. One selected scene has an additional historical PNG.
2. Preserve `data/suburbs/mooroolbark/` boundary, active pointer, plan/run results, frozen code/weight identities, captures and consistent confidence snapshot. Run `500114985968d40b` completed 257/257 tiles; raw detections total 30,552, while only 20,109 objects own a suburb core. Ownership and display deduplication must remain separate from raw records.
3. Preserve all connected analysis: eyes SQLite and raw sidecars, roof-review-loop candidates/inspections/multi-date stacks, scene self-reviews, batches, cohorts, references/checkpoints, training exports/evaluation/experiments, address evidence and legacy vector overlays. Mixed-area datasets must retain their outside-area validation/test references as explicitly labelled **context**; do not claim those are Mooroolbark edits.
4. Export a consistent sanitized geo/CRM snapshot: geographic/address-linked records plus metadata/features/POIs, excluding credential and identity tables. The three remaining CRM features refer to Croydon/Kilsyth; retain them only when referenced by connected experiment provenance. Owner contact fields in the 17 metadata rows are empty. Preserve existing original address IDs and stable upstream identifiers; do not allocate new IDs silently.
5. Keep `public/address-roofs.json` as a derived display artifact, filtered to the chosen geographic/address scope with its origin recorded. It is not a substitute for scene model/review history.
6. Hash and deduplicate source bytes, retain original relative paths in a manifest, and chunk files exceeding transport limits. Preserve hashes of original whole files so restoration can verify exact reassembly. Images and raw snapshots require protected asset storage; several individual reviews and datasets exceed Fieldwork's 2 MiB document limit.

## Evidence semantics and reconciliation

The confidence index has 8,529 medium, 1,971 low and 31 high evidence rows; these are **not accuracy probabilities**. Its saved calibration artifact has zero eligible validation layers and no published intervals. The eyes registry contains T1=50, T2=8, T3=5,420, T4=12 across the broader connected corpus. Tier 3 agent observations and machine decisions must stay separate from human-reference claims. Source `confirmed`, `geometry_reviewed` and `reviewed_classes` flags must remain unchanged and labelled with their original actor/model/image/revision bindings.

The ten model classes in the selected images are building, tree, road, car, solar_panel, driveway, footpath, vegetation, pool and water. Current complete-class checks exist for 14 solar, 18 road, 14 car and 3 tree layers; there are **no saved complete building-layer checks** in the selected review files. A locally confirmed outline does not verify heights, apexes, roof form or complete-scene negatives.

Reconcile independent source counts for current model objects, current review objects, statuses, holes, parents/groups, replacement lineage, model signatures, image hashes, review revisions/history, snapshot bytes, diagnostic table versions and owned vs context objects. Restore the manifest into a new directory and compare original file hashes. Compare the canonical processing inventory on the frozen source tree; do not rerun inference or manufacture absent labels to obtain parity.

## Current application surfaces and gaps

The local service layout is Next on `:3008`, Go geo/CRM on `:8091`, ML on `:18090`, as documented in `docs/local-services.md`. Route definitions inspected include scene read/save, image, map, processing, confidence, eyes, self-review, stacks, temporal, address resolution, training, candidates/roof feedback; review queues and pipeline checkpoints also read saved state. The JSON inventory lists exact source routes. This was source inspection, not an authenticated production test.

Fieldwork currently provides authenticated generic projects/items/labels, atomic revisioned annotations, export/change feed, Connect and read-only MCP. Reproducing local behavior requires an `addr-scene` adapter with protected raw model/review/image references, source-frame-safe comparison, sparse overrides and 3D editing; an `addr-report` adapter must expose the processing/confidence/analysis/CRM evidence without converting it to labels. Candidate inference, training and pipeline mutation actions remain separate capabilities from browsing their frozen outputs.

Current Fieldwork limits: 2 MiB document, 4 MiB request, 8 MiB list/change page and 32 MiB project snapshot. The planned content-addressed asset layer and chunked manifest avoid placing 5–20 MiB reviews, 38 MiB datasets or 354 MiB SQLite files inside an item. Original signed/credential URLs, credential tables, environment files, operational WAL/SHM/PID/locks and the unreferenced 25.5 GB tile cache must be excluded.

## Inventory integrity

No inference, source database mutation, production upload or credential-value query occurred. Database schemas/counts were read from private temporary copies including WAL state; source size/mtime stayed unchanged during each copy. Current PNG content hashes were verified. The corpus was not globally quiesced: one review-sidecar file appeared between the two filesystem counts, so final migration must generate and verify its own frozen manifest. The selected scene read pass reported zero errors.

The official boundary extent is `[145.29816708861,-37.8027267302211,145.353503804185,-37.7584240648359]`. The 56 nonintersecting current scene directories are explicitly enumerated in the JSON. Supplementary artifacts retain cross-area context only where relationships require it.
