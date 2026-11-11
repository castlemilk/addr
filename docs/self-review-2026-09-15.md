# Self-review: per-address MCP review mechanism

Date: 2026-09-15
Reviewer: opencode (via `scripts/ml/address_review.py` / MCP `addr-review`)
Addresses sampled: 29790 (27 Cambridge Rd, house), 29788 (19-27 Cambridge Rd,
units), 28777 (5 Heath Close, units), plus earlier 237-239 Hull Rd (large).

## Mechanism

- `python -m scripts.ml.address_review --address "..."` returns metadata JSON and
  renders `data/reviews/<address_id>/site.png` + `3d.png`.
- MCP server `scripts/ml/mcp_server.py` (configured as `addr-review` in
  `.opencode/opencode.json`) exposes `search_addresses`, `address_metadata`,
  `site_image`, `image_3d` over stdio.
- Site image overlays: extracted roof boundary (red), extracted canopies
  (teal), CRM overrides (orange roof / green tree / blue object), neighbouring
  MVT footprints (grey), address pin (blue), scale bar.
- 3D image renders the Deep 3D Studio via `scripts/studio-shot.mjs`
  (`/?lat=&lon=&studio=1` deep link), screenshotting the 3D canvas.

## Validation of the tooling itself

- Overlay projection is correct: neighbouring MVT outlines hug rooftops in the
  same images, and G-NAF address pins land on their properties.
- Metadata joins are correct: CRM metadata/overrides/POIs created earlier via
  the RBAC API (`verified gable` roof, `Roof inspection` POI) appear in the
  review output for address 29790.

## Findings

1. **Extracted boundaries are misaligned/merged for many addresses** (the
   dominant issue, consistent with the earlier SAM3 review: mean best IoU 0.07,
   256/178 missed on tile 40208):
   - 29790 (house, 280 m²): ring covers the yard south of the actual house.
   - 29788 (units, 307 m²): ring merges one dwelling plus garden; the units
     block east of the pin is not covered; kind/roof type are derived from the
     wrong structure.
   - 28777 (units, 337 m²): ring is a strip straddling two separate unit blocks
     across a courtyard.
2. **SAM3 masks are materially tighter** on the same imagery (cyan boxes/green
   masks in `data/sam3-review/preview_16_59221_40208.png` hug roofs; see the
   `house 0.55 364m²` mask for the 27 Cambridge structure), but coverage is
   partial for some houses.
3. **Height estimates inherit merged footprints** and are inflated for large
   ones (237-239 Hull Rd: eave 14.5 m / ridge 27.9 m for a single-storey strip).

## Recommended actions

1. Rebuild the demo archive with `--sam3 --sam3-trees` (SAM3 boundaries),
   then regenerate `public/address-roofs.json` with `task addresses` and re-run
   this review to confirm alignment.
2. Until then, treat address-roof records in misaligned tiles as approximate;
   use the CRM boundary editor (roof/tree/object) for per-address corrections —
   the review images are the QA loop for those edits.
3. Consider adding a per-footprint alignment score (IoU against a SAM3 pass, or
   offset vs the satellite edge response) into the address records so the CRM
   can surface "boundary confidence" and prioritize manual review.

## Follow-up: SAM3 rebuild (executed)

1. **Rebuild with `--sam3 --sam3-trees`**: 12 tiles, 725 s, **2,779 buildings
   (was 2,762)** and **863 trees (was 614)**.
2. **Regenerated records**: `task addresses` → **675 records (was 657)**,
   **1,253 addresses matched (was 821)**; kinds: house 176, units 276,
   large 193, business 30.
3. **Sample re-review**: boundaries are tighter for some addresses, but the
   vegetation false positive at 29790 (27 Cambridge) persists — SAM3 refinement
   only replaces/extends detections, it cannot delete one whose area is
   supported by a (mis-fired) proposal. Confirmed at full zoom: the ring sits
   on verge canopy while the actual house is ~15 m north-west.
4. **False-positive filter experiment** (`--sam3-min-evidence 0.2`, drop
   detections whose best SAM3 overlap is below the threshold): detections fell
   2,779 → **792** and address coverage fell 675 → **352** (lost 323 addresses)
   while 29790's false positive survived. Not adopted; the flag remains
   available (default 0.0) for flagged-area review.

### Conclusion

The current archive is the SAM3-refined first rebuild. Boundary precision is
improved and canopy coverage is materially better, but vegetation false
positives remain a pipeline-level issue that neither SAM3 refinement nor a
blunt evidence threshold fixes. Until a smarter discriminator lands (e.g.
evidence from the depth/vegetation channels or a per-footprint alignment
confidence), the per-address CRM boundary editor plus this review loop is the
corrective path for individual bad footprints.


## Full review loop (executed)

Ran the mechanism end-to-end: render review images, diagnose, measure candidate
fixes, correct via CRM, regenerate records, re-render to verify.

1. **Confirmed 27 Cambridge (29790) is a false positive**: two independent
   imagery vintages (Google z19 offset-3 and ESRI) show the ring on a wooded
   lot; the G-NAF point itself sits in the yard.
2. **Measured and rejected two auto-fixes** (no archive rebuild needed):
   - vegetation-overlap filter at 0.7: would drop 85 real records; the three
     known bad rings score only 0.26–0.35 (brown/shadowed canopy is not
     ExG-vegetation) — does not target the failure.
   - clamped-eave filter (`<=2.5 m`): flags 109/675 records (16%), most of them
     legitimate low-eave units — too blunt.
   - edge-support score: bad samples land at p18/p19 but 29788 at p62 — not a
     reliable discriminator either.
3. **Root cause identified**: association matches the *nearest* building to the
   address point, and the false positive (15 m SW) is closer than the true
   dwelling (25 m NNE) because G-NAF points are placed in the yard. The archive
   does contain the real L-house polygon (300 m², eave 2.7) — the record just
   never used it.
4. **Correction applied via CRM**: auto-picking SAM3 proposals failed (chose a
   building 39 m up the street); selecting the archive's L-house polygon by
   area/bearing worked. `UpsertFeature` (roof, editor key) now stores the
   corrected ring.
5. **Feedback loop implemented and verified**: `addresses.py --apply-crm`
   (default on) substitutes CRM roof overrides into generated records and sets
   `boundary_source: "crm"`. `task addresses` applied 2 overrides; the
   29790 record re-rendered with the ring on the L-house roof (gabled 300 m²).

**Improvement candidates from this loop** (not yet implemented):
- Association scoring: prefer candidates within ~30 m that have non-clamped
  eave evidence over nearer clamped detections (29790: FP clamps at 2.4 m,
  L-house estimates 2.7 m).
- MCP tool to list low-confidence/overridden records so review can prioritise.


## Accuracy iteration 2 (executed)

1. **Evidence-aware association — measured and rejected.** Swapping clamped
   nearest matches for non-clamped candidates within a slack radius corrupts
   correct matches: e.g. 7/9 Kiloran Avenue's nearest unit block (d=1.3–3.0 m)
   would be replaced by the neighbouring block at 4.4–8.2 m. No variant of the
   rule (slack +4…+20 m) separated the 27 Cambridge failure from legitimate
   low-eave matches, so it was not adopted.
2. **Duplicate footprint removal — shipped.** `dedupe_footprints` in
   `footprints.py`, called by the pipeline and by `load_buildings`: 2,610 →
   2,519 buildings, 50 → 0 overlap pairs (IoU > 0.3), 81 → 11 nested
   centroids, 13 records materially changed, 675 records retained (no coverage
   loss). An MBTiles post-processor was prototyped but discarded: within-tile
   overlaps were already zero, all duplicates are cross-tile, and re-encoding
   risked property loss/round-trip drift.
3. **MCP `review_queue` tool shipped**: ranks records by over-merge
   (point-inside-large), clamped-eave, and CRM-corrected status.
4. **Remaining dominant defect: over-merge on dense unit blocks.** 5 Heath
   Close (336.7 m² blob spanning three unit roofs; only one footprint exists
   within 22 m — SAM3 never segmented the individual units) and 19-27 Cambridge
   (365.8 m² blob over two houses plus driveway; solidity 1.00). These need
   real segmentation improvement or interactive correction; heuristics tested
   (vegetation, clamped eave, edge support, solidity, evidence swaps) do not
   separate them safely.


## Isolation review (executed)

Goal: make it visible whether roofs separate cleanly from trees/obstacles.

1. **Shipped metrics.** `canopy_over_roof_m2/share`, `overlapping_trees`, and an
   `isolation` grade on every record; `scripts/ml/isolation.py` + `task
   isolation` produce a summary and diagnostic map (grey buildings, teal
   canopies, red conflicts); MCP gains `isolation_report`, `review_queue` now
   flags `canopy-over-roof`.
2. **State of the demo bbox**: 649 clean / 11 light / 5 moderate / 4 obstructed
   records; 266 of 670 canopies touch a building; 11 trees sit mostly inside
   one; 19 tree/tree overlap pairs. Worst cases: 3 Charlotte Place / 16
   Macmillan (355.6 m² blob with 40% canopy share — the detector merged the
   roof and adjacent canopy) and 11 Reay Road (0.24).
3. **Pipeline fix attempted and reverted.** `carve_contained_trees` subtracts
   >=80%-contained canopies from buildings. Rebuild: isolation counts unchanged
   (13 -> 13 obstructed/moderate records, 11 -> 11 contained trees) because the
   remaining conflicts are cross-tile slivers the per-tile carve cannot see,
   and 6 legitimate buildings (52-429 m²) were lost. Now opt-in
   (`--carve-contained-trees`), off by default.
4. **Next targets:** cross-tile conflict resolution; review-queue-driven CRM
   corrections for the 13 flagged records (3 Charlotte Place first — the
   footprint is visibly merged with the canopy).


## Isolation iteration 3: cross-tile refinement (executed)

1. **Fixed the re-encode corruption found in iteration 2.**
   `mapbox_vector_tile.decode` returns y-up screen coordinates while the
   pipeline encoder expects the flipped convention, so re-encoding decoded
   geometry flipped every tile vertically (median building shift 16.7 m,
   matched addresses jumped 675 -> 958). Added `wgs84_to_mvt_local` (documented
   flip) and a round-trip self-check inside the refinement tool; verified
   max coordinate delta 0.000.
2. **Global refinement shipped** (`scripts/ml/refine_conflicts.py`, auto-run by
   the pipeline when `--trees`): resolves conflicts across tile boundaries,
   subtracts canopies >= 60% inside a building, never drops a building below
   25% of its area.
3. **Measured on the demo bbox** (675 records, unchanged): mostly-inside trees
   11 -> 5, records with canopy over roof 13 -> 9, isolation grades now
   659 clean / 11 light / 5 moderate / 0 obstructed (was 4 obstructed).
   3 Charlotte Place was the worst case (356 m2 blob, 40% canopy): the refined
   ring now hugs the hip roof with the canopy carved out (verified in the
   review render).
4. **System instrumentation added**: encoder/decoder round-trip unit test,
   in-tool drift check that fails loudly above tolerance, and isolation
   metrics/grades on every record.


## Per-address measure + eyes (executed)

1. **Quality measure shipped**: `record_quality` gives every address a 0-100
   score, A-D grade, and reason flags (canopy, association distance, clamped
   eave, tiny footprint, CRM correction). CRM roof overrides now also carry
   optional height/roof properties so corrected records are scored honestly.
2. **Performance dashboard**: `task perf` writes `data/reviews/performance.json`
   and a 3x3 worst-cases contact sheet. First run: 675 records, average quality
   95.4, grades A 625 / B 43 / C 7, isolation clean 659 / light 11 / moderate 5.
3. **Findings from the sheet**: a cluster of 12-14 m2 detections matched to
   houses 8-10 m away (garden sheds). Fix: `classify_building` now returns
   `outbuilding` for single-address footprints under 25 m2 (6 records), so they
   are no longer presented as dwellings.
4. **MCP**: `performance_report` tool, `review_queue` ranks by quality score
   with flags, `address_metadata` includes the measure.


## Instrumentation iteration 5 (executed)

1. **Build-time tile stats**: the pipeline now persists `quality_json`
   (per-tile buildings/trees/avg area/avg height + totals) in the archive
   metadata. Verified with a one-tile build (12.5 s): metadata contains the
   tile record; `task perf` reads it and reports sparse tiles (none in the
   demo bbox so far).
2. **Quality gate shipped**: `task verify:quality` checks the latest
   perf/isolation reports against thresholds (average quality >= 90, C <= 15,
   D = 0, mostly-inside trees <= 10, canopy-over-roof records <= 30, tree/tree
   overlaps <= 40). Current demo state passes every check.
3. **Corrections review**: `task review:corrections` renders a contact sheet of
   CRM-corrected addresses. First run exposed a bad correction from the early
   headless UI test (25902's override sat on vegetation, not a roof); the
   feature was deleted so the record reverts to the extracted footprint. The
   review loop now inspects its own corrections.
4. **Map eyes confirmed**: `verify:ui` (`scripts/verify-map.mjs`) already
   captures map.png and studio.png, covering the browser surfaces.


## Final state (instrumentation iteration 5)

Fresh 12-tile rebuild (435 s) with the measured cross-tile refinement default
(containment 0.6) swapped in and re-measured:

- **669 records**, average quality **95.4**, grades **A 620 / B 42 / C 7**,
  zero D; coverage 0.534 (units share buildings).
- **Isolation**: 653 clean / 11 light / 5 moderate / **0 obstructed**;
  canopy-over-roof records 9; trees mostly inside buildings 5; tree/tree
  overlap pairs 19.
- **Build-time tile stats**: all 12 tiles carry `quality_json`
  (min buildings per tile 142, no sparse tiles).
- **Quality gate passes** every threshold; `task review:corrections` caught and
  removed a stale test correction, leaving only the verified 27 Cambridge
  override (which now carries eave/ridge properties).


## Score reflection + measure calibration (executed)

Flag-attribution of the 95.4 average showed most score loss came from proxies
that misrepresent legitimate conditions, so the measure was recalibrated with
evidence:

- `point-inside-large` removed: 49 of 59 flagged records had no other defect
  (G-NAF points legitimately fall inside large unit complexes).
- Distance thresholds now 12 m (mild) / 20 m (far): all 123 "distant" records
  were 8-12 m, i.e. normal property-frontage placement; only one record exceeds
  15 m.
- `clamped-eave` penalty reduced to -5: 2.4 m is a plausible single-storey
  eave; the clamp signals model uncertainty, not an error.
- Footprints < 25 m2 classify as `outbuilding` regardless of address count
  (13 records) and are exempt from the tiny-footprint penalty.
- Verified CRM corrections are not distance-penalised (placement was accepted
  by a reviewer).

Result: average quality **95.4 -> 99.1**, grades **A 662 / B 7 / C 0 / D 0**;
gate defaults tightened to 97 average, C <= 3.

Rejected by measurement: an oversized-footprint detector (area vs median
nearby) would have flagged 16-59 legitimate unit complexes and still missed the
known 19-27 Cambridge merge; over-merge detection remains a segmentation
problem, not a scoring one.

### What would boost the remaining scores (evidence-based)

The 7 B records are real canopy defects, not calibration:
- 30 Highland Crescent (x2), 241 Hull Road (x2), 11 Reay Road: canopy shares
  0.11-0.24, i.e. trees overlapping roofs below the 0.6 carving threshold.
  Fixes: tree-mask-aware carving (subtract the SAM3 tree mask rather than its
  polygon) or CRM corrections.
- 15-19/19 Manchester Road: likely false positive over a car park with
  scattered trees (canopy-light + multi-canopy + clamped eave); verify and
  correct or remove via CRM.
Invisible in scores but the largest real defect left: over-merged footprints
(19-27 Cambridge) and depth-model uncertainty on large low-relief blocks
(84 clamped records). Both need model/segmentation work, not measure changes.


## 27 Cambridge Road: address, map, and terrain fixes (executed)

1. **Wrong address label (154 Hull Road / 9 Greenville Street):** the Go
   store's `queryCandidateAddresses` applied `LIMIT 50` to raw rtree rows
   before distance ordering, so the true nearest address was often not in the
   candidate set (556 rows in the 300 m box). Fixed in both the prepared and
   ad-hoc queries; regression test `TestReverseGeocodeReturnsNearestAddress`
   asserts id 29790 (27 Cambridge) for the pin. Live endpoint now returns
   "27 CAMBRIDGE ROAD, Mooroolbark VIC 3138" at 0.3 m.
2. **Map showed the stale extracted footprint:** the roof mesh layer only read
   MVT and ignored CRM corrections. It now loads records, draws CRM-corrected
   roofs, and skips replaced footprints (matched via `suppressed_centroid` or
   centroid-inside-corrected-ring). Verified with map screenshots: the grey
   slab over the driveway is gone and the corrected L-house mesh renders.
3. **Studio terrain:** new `/depth` endpoint on the ML sidecar (65x65 relative
   relief), `/api/depth` proxy, low-frequency terrain blur, footprint carved
   flat at the local ground level (follows the slope), mesh seated on it with a
   1.5 m wall skirt, `Ground Level` metric, z17 base imagery, and wider
   framing. Verified with studio screenshots (ground level 1.51 m at the
   27 Cambridge site).


## Edit mode + training reference (executed)

1. **Vertex edit mode shipped**: CRM panel pencil action opens an edit overlay
   with draggable vertex handles (drag to move, edge-click to insert,
   right-click to remove), Save/Cancel toolbar, Enter/Escape shortcuts, and
   fit-to-feature framing. Verified headlessly: a scripted drag of vertex 0 and
   Save updated the CRM feature geometry (authorship recorded), and the panel
   restored with the new timestamp.
2. **Training reference shipped**: `task training:export` pairs each CRM
   correction's before/after geometry with context and authorship; the capture
   now stores the *original extracted* footprint before the override is
   applied (a first implementation captured the post-override ring, caught by
   checking areas: 299.0 vs the expected 325.6). MCP `list_corrections` exposes
   the dataset for future fine-tuning/evaluation. Verified: 27 Cambridge Road
   (extracted FP 325.6 m2 -> corrected 299.0 m2).
3. **Loops stay measure-first**: area checks on the export caught the capture
   ordering bug; the edit flow was verified end-to-end rather than by static
   review.


## Review-queue loop on B-grade records (executed)

Ran the queue: 7 B-grade records reviewed via site renders + archive candidates.

**Corrections applied (all now A/100):**
- 30 Highland Crescent Units 2/3: merged carport/shed/garden blob; carved the
  overlapping 407 m2 canopy from the 136.8 m2 ring (-> 121.6 m2) via the new
  `scripts/ml/carve_canopies.py`, stored through the CRM.
- 27 Cambridge Road: eave/ridge/roof properties restored after finding the CRM
  service dropped `properties` on geometry-only updates; regression test added.

**Two real bugs found by the loop:**
1. Record canopy metrics were computed before CRM overrides were applied, so
   corrected records kept stale canopy shares (carve showed 0 overlap in
   geometry but the metric still said 0.11). `apply_crm_roof_overrides` now
   recomputes canopy metrics/isolation with the new ring.
2. `UpsertFeature` update path overwrote stored properties with an empty map
   (the vertex-drag save wiped eave/ridge). Service now preserves existing
   properties when the request omits them; covered by
   `TestGeometryOnlyUpdatePreservesProperties`.

**Attempted and reverted:** 241 Hull Road "outbuildings" are 19-24 m2 brown
patches inside a road-verge canopy, i.e. false positives rather than sheds.
Carving their canopy made them score A/100 while still being non-buildings, so
the corrections were deleted. This exposes the capability gap: records cannot
be *suppressed*, only overridden, so false positives can be dressed up by a
correction. Next step: a CRM suppression marker honoured by `addresses.py`.

**Left flagged (no honest fix):** 15-19/19 Manchester Road (verge false
positive, no alternative building match within 32 m) and 11 Reay Road (the
"canopy" over the dark roof is likely a vegetation-mask false positive on a
dark roof, so carving would cut real roof).

**State after loop:** 669 records, average quality 99.2 -> 99.1 (five B remain,
all genuine review flags), isolation clean 655 / light 11 / moderate 3,
training dataset holds 3 corrections (27 Cambridge + 30 Highland units x2).


## Reflection: boosting boundaries, roofs, and 3D (executed + next levers)

**Shipped this round**
1. **Record suppression** (CRM `status = suppressed`, honoured by
   `addresses.py`): removed four confirmed false positives (Manchester Road and
   241 Hull Road verge/leaf-litter patches). Coverage 669 -> 665, average
   quality 99.1 -> 99.2, B flags 5 -> 1. This closes the gap where a correction
   could dress up a non-building; suppression is the honest action.
2. **Slope-aware studio pads**: the carve follows the terrain gradient (no
   plateau), the mesh seats at the lowest ground point under the footprint, and
   the wall skirt scales with the measured ground relief, so sloped sites no
   longer float or clip (verified at 27 Cambridge: ground 1.23 m, seated).
3. **Measured negative**: the hypothesis that 11 Reay Road's canopy flag was a
   dark-roof misdetection is wrong (the tree is real, 13.8 m tall, only 18%
   inside the building); it stays flagged rather than "fixed".

**Next levers, evidence-ranked**
- **Instance splitting for merged blobs** (19-27 Cambridge, 366 m2 over two
  houses + driveway; 30 Highland-type carport merges): SAM3 produced no masks
  for the individual units, so options are extra prompts ("roof", "unit"),
  higher-resolution windows, or splitting where the depth field shows separate
  roof planes. Biggest remaining boundary-accuracy lever.
- **Suppression UI**: surface suppress/unsuppress in the CRM panel (currently
  API-only) and list suppressed records in MCP review tooling.
- **Depth height calibration**: ~80 records still carry clamped eave estimates;
  neighbour-median imputation or a higher-resolution depth pass would improve
  roof proportions on both map meshes and studio views.
- **Roof-vs-canopy classification**: dark roofs read as canopy (and canopies
  merge into roofs). A depth-aware classifier (colour + height above local
  ground) would cut both error classes; tree/building conflicts are still the
  largest structural error source (5 contained trees, 19 tree/tree overlaps).
- **Real terrain data**: the studio terrain comes from monocular depth, so it is
  relative. An offline DEM source would make gradient modelling survey-grade.
- **Temporal agreement**: require detections to agree across imagery vintages
  (Google 2022 vs ESRI 2024) to filter transient false positives.

**State**: 665 records, average quality 99.2 (A 664 / B 1), isolation clean
655 / light 9 / moderate 1, 3 training corrections, all checks green.


## Roof meshes + roofline extraction assessment (executed)

1. **Boundary regularization measured and rejected**: DP simplification at 1.5 m
   collapses the 27 Cambridge ring from 299 to 122 m2 (cuts across
   concavities); a 2-rectangle covers over-cover by 67%. The ring is genuinely
   blobby, so the fix is detector-side, not fitting-side.
2. **Mesh cleanup shipped**: `buildRoofSections` applies DP simplification and a
   section cap; the studio renders one clean hip for blobby rings instead of a
   jagged multi-gable mesh (overhang trade-off documented).
3. **TU Delft rooflines ported**: `scripts/ml/rooflines.py` now implements the
   same pipeline stages in Python (LSD -> mean-shift angle/offset
   regularization -> ray propagation to the k-th intersection -> planar
   footprint partition -> sliver coalescing -> depth plane fits -> physical
   grouping -> rooflines + per-plane LoD2 surfaces). Measured on 27 Cambridge:
   13 segments -> 8 faces -> 8 planes (12-40 degree pitches), 8 rooflines,
   291.5/299 m2 covered; `task rooflines` + MCP `rooflines` expose it. Limits:
   k=1 approximation of the kinetic scheduler and monocular relative depth
   (heights anchored to eave/ridge), so it is a review/reconstruction aid, not
  yet wired into the production mesh. Plane-consistency remains a candidate
  footprint-quality signal.


## Roofline wiring + coherence gate (executed 2026-09-16)

`task addresses CLI_ARGS="--rooflines"` attached partitions to all 665
records (~1.3 KB/record; key is `lines`, not `rooflines`). Fresh extractor run
on 27 Cambridge gives 39 LSD segments -> 19 faces -> 14 groups -> 16 parts, 7
rooflines, 279/299 m2 covered. Measured over all records: 514/665 carry >= 1
roofline segment; the plane-reconciliation stage (shared vertices get one
height, boundary pinned to eave) works — an early "0/665 have rooflines"
reading was a wrong-key misquery (`rooflines` vs `lines`), corrected before
acting on it.

1. **Coherence gate shipped** (`shouldUseRooflineMesh` in
   `src/lib/address-roofs.ts`, applied in `findAddressRoof` and
   `RealRoofLayer`): per-plane mesh only when >= 2 parts, coverage 70-115% of
   the record area, and no plane steeper than 60 degrees (steep fits are
   wall/depth noise, not roofs). Measured: **565 pass / 100 fall back**
   (40 single-part, 56 steep-plane, 4 coverage). The 237-239 Hull Road
   industrial strip (16 addresses sharing one 3,798 m2 footprint, 262 parts,
   max slope 79.5 degrees, eave/ridge still inflated at 14.3/27.7 m) fails the
   gate — the pathological case falls back instead of rendering 262 planes.
2. **Studio default restored**: 27 Cambridge (16 parts, 71.8-degree plane)
   fails the gate and renders the clean single hip again
   (`gate-fallback-29790-studio.png`); passing houses render coherent
   per-plane meshes — 66 Ervin Road, 5 parts / 3 lines
   (`gate-pass-25096-studio.png`, one small edge notch) and 76 Cambridge
   Road, 6 parts / 11 lines (`gate-pass-25229-studio.png`, clean gable,
   visibly better than a single-hip fallback).
3. **Map default fixed (was a regression)**: the experimental toggle was wired
   default-ON (`useState(true)`) while the comment, README, and skill all say
   opt-in — now `useState(false)`. Follow-on hole fixed too: gated-out
   records used to suppress their tile prism while drawing nothing (and
   consumed `MAX_BUILDINGS` budget); the suppression grid and the counter now
   only cover records that actually draw (`gate-map-default.png` shows prisms
   + detector meshes, no holes; `task verify:ui` passes: 24 tile requests,
   studio opens).
4. **Lint fix**: the gate was named `useRooflineMesh`, which trips
   `react-hooks/rules-of-hooks` (plain predicate called in loops/class
   code). Renamed to `shouldUseRooflineMesh`; `tsc` clean, eslint 0 errors.

**Next levers**: per-plane meshes still depend on monocular-depth fits, so
low-relief roofs stay near-flat multi-part (e.g. 11 Kiloran Avenue: 4 parts,
0 lines, slopes 0-5 degrees — harmless but noise); the 25096-type edge
notches suggest the TS ear-clip vs Python shapely triangulation differ on
concave parts. Neither affects the default views now.


## Unit houses: 27 Cambridge vs Unit 4/29 + systemic handling (executed 2026-09-16)

User report confirmed with numbers: the "verified" 27 Cambridge CRM correction
sits on Unit 4/29's building (27-corrected vs 29-extracted IoU **0.991**; 27's
pin is 13.3 m from its own ring edge, 29's pin 8.5 m). The L-house is 27; the
mapped ring is the neighbour units'. The user is mid-fix via vertex dragging
(DB ring already moved to 198.5 m², IoU vs Unit 4 down to 0.352); the map mesh
still shows the old ring until the next `task addresses` regen, which also
re-exports the training pair. The doc entry above claiming the correction as
verified was wrong — the review render looked plausible but it was the wrong
roof. Lesson recorded in the pipeline: corrections now carry a placement
tripwire (below).

Shipped in `scripts/ml/addresses.py` + `export_training.py` (all covered by
tests, dry-run on the demo bbox, live on the next regen):

1. **Unit inheritance** (`inherit_unit_rings`, 30 m sibling radius): unmatched
   unit addresses adopt their street-number sibling's ring, flagged
   `inherited-ring` (informational, no penalty). Measured: 155/355 bbox units
   had no record; 75 have a matched sibling and **58 rescue within 30 m**
   (p50 19.7 m — beyond the 12 m association radius, which is what stranded
   them), including Unit 1/29 (30331, 15.6 m, units 95/A). Suppressed donors
   and non-unit addresses are excluded. Clones are added before the roofline
   pass so they get partitions too.
2. **Co-claimant context** (`shared_with`, IoU >= 0.9, computed on final rings
   after CRM overrides): every record lists the address ids claiming the same
   roof, exposed in `address_metadata`. Deliberately not a penalty — 580/727
   records share (unit complexes dominate) — but the next manual boundary pick
   sees competing claims instead of repeating the 27/29 error.
3. **Correction placement tripwire**: the training export now records
   `pin_to_corrected_centroid_m` per correction with `needs_review` above
   15 m, surfaced in MCP `list_corrections` and the manifest. Validated on the
   current three: 27 trips (27.1 m), the 30 Highland carves don't (4.7/15.0 m).


## Attribution eyes: audit + labelled pins (executed 2026-09-16)

The review renders "looked plausible" on the wrong roof, so evaluation itself
got instrumented rather than just the pipeline:

1. **Attribution audit** (`scripts/ml/attribution.py`, `task attribution`,
   MCP `attribution_audit`): per record, own-pin vs nearest-other-pin ring
   distance. Demo-bbox calibration: median 0.0 m, p99 8.9 m, max 13.3 m (27
   Cambridge). Threshold 10 m + tiering (cross-address vs same-development)
   yields exactly **one suspect: 27 Cambridge → Unit 4/29** — zero false
   positives. Audits the file state, so it re-verifies clean after each regen.
2. **Labelled neighbour pins on site renders**: `site.png` now draws nearby
   G-NAF pins as white dots with house numbers (`27`, `3/29`, `4/29`, `1/29`,
   `29`). On 27's render the failure is unmistakable: 4/29's pin sits inside
   the ring while 27's own (blue) pin sits outside in the yard.
3. **Boundary kept deliberately**: detection is automatic, corrections stay
   human-confirmed (Propose box / Draw roof / vertex edit). The incident that
   motivated this was a bad correction being accepted — auto-applying geometry
   would repeat that failure mode at machine speed.


## 27 Cambridge delineated by hand (executed 2026-09-16, verification pending)

The user kept vertex-dragging without converging, so the ring got delineated
from z19 imagery instead of more words: 7 vertices tracing the terracotta
L-house (339 m²), saved as feature 9 replacement. Evidence used: the east
tile shows the L ending at a corner with a driveway gap before Unit 4's dark
roof (separate buildings); the west edge follows the 19-27 blob's east
(party) line; the file polygon merged mass-east + link + dark building, the
user's drag had tightened onto the dark building only. Known uncertainties:
west party line invisible from above (±2 m), NE eave under tree cover, east
face at the corner. Full regen (`task addresses` + `--rooflines`, ~15 min,
backgrounded) picks it up with the 58 inherited units and shared_with;
attribution + site render verify after. Recipe reused: `ImageryWindow`
pixel→local→`tile_local_to_lonlat` round-trips exactly (0 px error), always
plot-verify the draft on-frame before saving.


## 27 Cambridge fix verified (executed 2026-09-16)

Regen complete (723 records: +58 inherited): 27 is now **339 m² house, 100/A,
`crm-corrected`, shared_with empty, pin 11.1 m from ring edge** (normal
frontage). Site render confirms the ring on the terracotta L with 4/29's pin
outside it. Attribution audit is **clean on 27** (was the lone max at 13.3).
Training pair updated: pin_shift 27.1 → **11.2 m, needs_review False**. Unit 1
29 (30331) rescued as units 95/A. Coverage 0.534 → 0.577, avg quality
99.2 → 98.5 (inherited distant-point penalties, honest), gate still green
across the board (0 C, 0 D).

The regen also exercised the audit→eyes loop on four NEW cross-address
suspects: two Hull Rd industrial records share one 464 m² shed across three
streets (over-merge class, no clean reassignment — left alone), Unit 9/82's
ring is a carpark false positive (detector-level, audit surfaces it each run),
and Unit 2/8 Felix inherited Unit 1/8's dark-duplex ring in a possible
cascade (needs ground-truth call + maybe delineation #2 — reported, not
touched). Rule confirmed: audit finds, eyes adjudicate, only provable misses
get fixed.


## 27 verification closed + one false alarm owned (executed 2026-09-16)

Full battery on 27, all green: DB ring == delineation (IoU 1.0, nobody
overwrote it), site render shows the ring on the L with 4/29's pin outside,
attribution clean, training pair pin_shift 11.2 (no review), 29's records
untouched (300.3, Q100, co-claimants symmetric), studio metrics sane
(339 m², volume 1,541 m³ ≈ area × mean height — consistent).

False alarm, owned: the rooflines debug overlay LOOKED 30 m misplaced, but
face bounds == ring bounds exactly — the extractor is correctly placed and
my geography reading was wrong (median-vintage render, anchored on the wrong
driveway). Numbers over eyeballs; that's why the bounds check exists.

Two cosmetic caveats (not accuracy blockers): the new partition covers 90%
(11 parts, gate passes) with sliver pinholes between parts, and the studio's
deterministic initial camera frames through the street trees — orbit to
inspect. Partition fill tuning is future work, not this loop.


## Partition pinholes closed: full tiling (executed 2026-09-16)

Prompted by the TU Delft `pipeline_all.jpg` reference (watertight LoD2, zero
gaps): measured where our 10% loss happened — 100% of it in
`_partition_faces`, which dropped arrangement cells under 3 m² or 55% cover
(35 m² lost on 27's 339 m² roof; zero loss at every later stage). Fix: keep
every non-empty inside-piece and let `_coalesce_slivers` absorb smalls into
their longest-edge neighbour (isolated crumbs survive as tiny flat faces —
no holes, ever). Measured after: coverage **1.000** on 27 + three controls
(300/257/366 m² roofs), sane face counts (12/18/5/22, no explosion), all 109
pytest green. 27 end-to-end: 12 faces → 12 groups → 12 parts, 1.000 coverage,
watertight mesh render (vs the holey 0.90 before). Full regen backgrounded so
all 723 records carry watertight partitions.

Second owned misread in one session: the debug overlay twice LOOKED 30 m
misplaced; twice the bounds check (faces == ring bounds exactly) plus a
same-frame overlay proved the extractor correctly placed and my geography
reading wrong. Rule now written down: overlay disputes get settled by
plotting both layers on one trusted frame, never by eyeballing crops.


## 27's west edge: the open L question (analysis, awaiting owner)

The tilted map view reads as one L because two rendered volumes touch: the
19-27 blob's MVT prism + 27's corrected mesh, adjacent at lon ≈ 145.31237
(0.7 m apart, zero overlap). Seam evidence: zero LSD segments cross the
party band (2742 detected); close-up shows two COMPLETE hip forms abutting
at a valley/gutter (red-car garage wing vs 27's mass). Attached duplex vs
separate houses is indistinguishable from above, so no unilateral move:
if the wing is 27's, the ring extends west (~+150 m²) and 19-27's record
shrinks in the same regen; if separate, the current bar stands and the "L"
is two houses touching. Either way the ring is on the right building —
only the west edge is in play.


## 3D-mode eyes: `studio_check` (executed 2026-09-16)

`task studio:check` (module `scripts/ml/studio_check.py`, headless
`scripts/studio-check.mjs` capturing the app's `[Deep3D]` tri counts, MCP
`studio_check`) returns PASS/REVIEW per address from shape metrics (solidity,
notch angles, coverage), mesh tri counts, and a canvas-fill tripwire plus the
shot itself. Calibration honesty enforced twice by measurement: binary reflex
counts flagged a 209° digitizer wobble as "concave", so the rule is now
solidity < 0.95 or a notch ≥ 225° (wobble ~209°, true notches 220–270°); pixel
edge-density was dropped from the verdict when I found UI chrome overlaps the
canvas box (tris carry it instead). Live matrix: 27 + `--expect concave` →
REVIEW with exactly one finding (convex bar vs L belief — the open question,
not a rubber stamp); 25096 + `--expect convex` → PASS (18/16 tris). 117
pytest green.


## 27 L locked in with the west wing (executed 2026-09-16)

Owner confirmed the red-car garage wing is 27's: 27's ring extended west
(union + spike-clean over 300°) to a **432 m² concave L** (10 verts, 231°
notch, solidity 0.845), saved as feature 9; 19-27's blob cut back to a
**307 m²** remainder (new feature 20, grey house + driveway). Regen verified:
27 = 432 m² house 100/A, 19-27 = 307 m² units 95/A, both shared_with empty,
both attribution-clean, abutting with zero overlap. `studio_check --expect
concave` flipped REVIEW → **PASS** (11 parts, coverage 1.0, 22/46 tris).
Training now holds 4 corrections (19-27's shift 5.3, no review); perf 723
records / 0.577 coverage / 98.5 avg, gate green. Awaiting owner sign-off on
the L; residual nits (±2 m west party, tree-hidden NE eave, ~15 m² driveway
garnish) are vertex-nudge grade.


## Red tile: roof material follows corrections (executed 2026-09-16)

Owner: "it's a red tiled roof" — but both 3D views rendered 27 slate grey.
Mesh colour was type-hashed (hipped → slate), so the correction fixed shape
but not colour. Fix, deliberately correction-scoped: CRM roof overrides may
carry `properties.roof_material` (`terracotta` set on feature 9, geometry
untouched); `apply_crm_roof_overrides` copies it to `record.roof_material`;
both renderers honour it via `roofColorFor()` / `ROOF_MATERIAL_COLORS` in the
shared `roof-geometry.ts` (map: per-colour material buckets disposed on
rebuild; studio: material wins over the type colour). Verified: record
carries terracotta, map mesh + studio per-plane roof both render red tile,
`studio_check --expect concave` PASS zero findings, `test_apply_crm_\
carries_roof_material` covers the plumbing. Pipeline-wide imagery-hue
classification is a separate project — not started.


## Phantom address: 19-27 Cambridge doesn't exist (executed, owner ground truth)

Owner: "theres no 19-27, theres just house 27 and units 1-4 29". 19-27 is a
G-NAF *range parcel* entry with no dwelling — yet it carried a full 307 m²
record (my own remainder correction, feature 20) and a map mesh. A range
address with a building on it is a standing trap: the association happily
matched 19-27's pin to the grey-house blob because nothing checks
dwelling-ness. Fix, all via CRM API: feature 20 deleted, 29788 suppressed
(`UpdateAddressMeta status=suppressed`, excluded at regen), and the grey
house reassigned to Unit 1 21-25 (new feature 21, same remainder geometry —
its pin sits 0.7 m from that ring vs 44 m+ from its own development, the only
plausible owner; flagged as inference in the label). This also resolves the
"extra house": 29786's stale full-blob ring (59 m² of 27's L + 84% of the
remainder) is replaced by the grey-house-only ring. Lesson: a G-NAF entry is
an address, not a building — range numbers deserve suspicion on sight.


## Phantom resolution verified (executed 2026-09-19)

Regen + full battery: 29788 gone (722 records), 29786 = 307 m² grey-house
ring 95/A with zero co-claimants, 27's L untouched at 432 m² 100/A,
attribution clean across the west side, `studio_check --expect concave`
PASS, map shot confirms the phantom cream mesh is gone (terracotta L +
legitimate 21-25/29 neighbours only), training holds 4 corrections, gate
8/8 green, 118 pytest + tsc + lint clean. The street now reads as the owner
describes it: house 27 plus units 1-4 at 29.
