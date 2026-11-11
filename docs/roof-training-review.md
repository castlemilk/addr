# Roof review and reconstruction changes — 22 September 2026

The review workspace at `/reviews` now manages the benchmark cohort, all captured roofs, and CRM corrections. It provides a searchable 20-row queue, status filters, deep links, side-by-side imagery and model inspection, a boundary-check acknowledgement, correction categories, reviewer notes, and reviewed-boundary snapshot export. Drafts survive address navigation. Failed saves preserve input; revision conflicts prevent another reviewer's work from being overwritten.

## Findings addressed

| Finding | Change |
|---|---|
| The review API displayed invented parcel, vegetation and neighbouring-address overlays for one address | Context layers require source data; property boundaries also require source provenance |
| The default example bypassed inference through an address-specific roof template and changed the input centroid | Every address follows the general extractor; the template remains available only through explicit `manual_template: cambridge-demo-v1` and is labelled as such |
| Saved feedback dropped the submitted rejected-cut array | Validated, atomic, cohort-scoped feedback storage saves and restores the complete array |
| Review writes bypassed CRM permissions | Save verifies the existing CRM roof-write scope; local no-auth development remains supported by the CRM service |
| A failed cohort fetch left an endless spinner | Distinct load, retry, empty and no-result states |
| Address review links ignored their selected ID | URL-backed cohort, selection, search, status and pagination; map links use the complete captured-roof set |
| Boundary snapping shrank corners on blank imagery | Removed unconditional corner averaging; capped total movement across passes and included the unchanged vertex as a candidate |
| Clustered canopy depth samples tilted roof planes | Deterministic consensus fit followed by robust refitting, with finite/rank checks and diagnostics computed from the final fit |
| Coplanar faces were compared at different spatial positions | Compare heights at shared-edge positions and refit merged faces from their samples |
| Eave constraints flattened gable endpoints and repeated fitting damped apex heights | Preserve supported ridge endpoints, reconcile shared vertices from original evidence and node T-junctions |
| Roofline diagnostics could float above/below emitted geometry | Interpolate their heights from the emitted mesh vertices |
| The partition rewrote the captured outer boundary | Preserve the supplied outline, including concave wings |
| Rounded/repeated vertices caused missing 3D roof triangles | Use the existing Three.js triangulator with vertex-index preservation in the shared geometry module |
| Training before/after pairs used the wrong coordinate origin | Restore the suppressed footprint using its own suppressed centroid |

## Review and training contract

A status of **Approved** requires the boundary imagery check, no unresolved issue flags and no rejected cuts. Approval is tied to the captured boundary signature. A later boundary change requires a fresh review. No corrections were automatically approved during this work.

The UI export is a reviewed snapshot of the captured records. It does not imply live CRM edits have already been re-extracted. For the training reference dataset, use `task training:export`: it checks the current CRM geometry as well as the approval/signature, rejects malformed polygons, marks training eligibility, and emits IoU, boundary precision/recall/F1 at 0.5 m, Hausdorff distance and centroid shift. Spatial groups assign neighbouring samples to the same split. Unreviewed corrections remain in the audit export with `training_eligible: false`; filter on that field for training. Heights are explicitly excluded as measured height labels.

Reviewer cut rejections are consumed by both the cohort evaluator and ordinary per-address roofline extraction, only while the saved boundary signature still matches. Line matching checks direction and overlap so rejecting one ridge does not remove a perpendicular ridge at the same midpoint.

The available dataset contains four CRM roof corrections. None had a completed human review, so zero are currently eligible for supervised training. Model weights have **not** been fine-tuned. These changes improve the inference, reconstruction and feedback pipeline; real-world accuracy gains still require evaluation against independently reviewed references.

## Validation

- `task test`: Go suites, Python tests, TypeScript checking, review-store regression checks and shared mesh regression checks.
- `task lint`: Go vet, ESLint and Ruff. Existing frontend warnings remain; no lint errors.
- `pnpm build`: production build.
- `task verify:ui`: 24 tile requests, map and Deep 3D Studio opened, no runtime errors.
- `task verify:reviews`: aerial overlay alignment, image checks, no runtime errors; isolated API tests cover save/restore, invalid approval, stale revisions and stale boundary rejection. Test annotations are removed afterward.
- Browser checks: search/clear, 20-row queue, URL selection, draft retention, save failure recovery, desktop and 390px mobile layout without horizontal document overflow.
- Strict design/interaction audit: no findings. DESIGN.md lint: no errors. Design detector: advisory palette findings only; semantic variants are documented in DESIGN.md.
- Rebuilt all 12 benchmark roofs using general extraction, preserving a copy of the prior report/images under `/tmp/addr-improvement/cohort-before`.

A deterministic stress test places canopy outliers 6 m above one edge of a known roof plane (25% of 400 samples). The previous estimator reports a 41.4° pitch; the updated estimator recovers the known 26.2° pitch with 0.038 m inlier RMSE. This is a synthetic regression result, not a surveyed-field accuracy claim. A separate gable test preserves the 6 m apex and 3 m eaves. Concave mesh tests retain all 64 m² of a known footprint with duplicate vertices and both winding orders.

## Running locally

### Government parcel reference layer (24 September 2026)

The map's **Property boundaries** switch and the aerial scene review now use the
public [Vicmap Parcel service](https://discover.data.vic.gov.au/dataset/vicmap-property-rest-api),
attributed to the State of Victoria (Department of Transport and Planning), under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Amber dashed outlines are
land parcels; cyan outlines remain roof/building predictions or reviewed edits.
The source publishes weekly. Retrieval time and per-feature source uncertainty
are exposed separately; neither certifies aerial-image alignment.

`GET /api/parcels?bounds=west,south,east,north` returns WGS84 GeoJSON, lot/plan/SPI/PFI,
geometry-record creation date where supplied, uncertainty, retrieval timestamp and
source/license metadata. The public, read-only endpoint accepts Victorian viewports
up to 2 km across. It requests approved parcel and polygon records (`A`) at ground
or surface level (`G`, `S`), retaining holes and multipolygons. Codes were verified
against the service's STATUS and Z_LEVEL reference tables. Complete pagination is
required; errors/truncation return an explicit unavailable response. The map waits
until zoom 17 and a viewport no more than 700 m across to avoid fetching thousands
of parcels for an overview or a wide/pitched display. Server caching
is bounded to 64 requests for six hours, with failed requests evicted for retry.

In registered scenes, coordinates use the original local Mercator transform.
**Fit parcel** uses a uniquely nearby parcel to frame the existing surrounding
imagery; it is available only when the parcel fits inside that image coverage.
Proximity uses outline edge samples and deliberately leaves overlapping matches
unassigned. It is a viewing hint, not a verified address/ownership association.
Parcel paths are non-interactive so they cannot intercept outline editing.

For the current scene, the approved lot `2\PS332475` (PFI `60984`) contains the
27 Cambridge Road address point and is the unique nearby parcel for its selected
outline. The property service also returns an overlapping group property, which
is why a generic nearest-property assignment would be unsafe. No address-specific
rule or geometry is embedded in code.

Parcel data is kept outside model snapshots, saved annotations and training
exports. This feature changes viewing context only: it does not clip roofs to
parcels, rerun the detector, reframe saved images or promote land boundaries into
roof training labels. `scripts/test-property-parcels.mjs` covers bounded queries,
source provenance, projection, multipart/holes, ambiguous matches, approved-level
filtering, pagination, no-coverage/failure handling and retry/deduplication.

**Analyse surrounding area** now explicitly expands inference beyond the old
crop. It creates a new immutable 1024px model/image snapshot, retaining the exact
original 512px RGB pixels at (256,256) and the same local metre origin. SAM scans
the full enlarged image, seeded with prior model proposals only. Existing review
geometry is never fed into this detector pass. In 27 Cambridge Road's scene this
discovered the garage as a separate 49.52 m² building candidate.

The sole cross-image recovery exception is this proven integer padding operation:
the server checks both image hashes, the preserved central pixels, the lineage and
both coordinate transforms before offering recovery. Pixel outlines and their
feedback bindings shift together; roof-line vertices/heights in metres remain
unchanged. The canonical review API keeps previous revisions, resets outline/layer
approval and retains the garage as an unreviewed model estimate. Expanded captures
are withheld from training splits until spatial assignment is reassessed. Tests:
`test_scene_expand.py` and `test-scene-feedback.mjs`, including a tampered-image veto.

The default port 8085 was occupied by another Docker service during validation. The tested app runs on `http://localhost:3008`, with its CRM/geo service on port 8086. Start the web process with `NEXT_PUBLIC_GEO_SERVICE_URL=http://localhost:8086` at build time and `GEO_SERVICE_URL=http://localhost:8086` at runtime, or use a matching free port of your choice. The local CRM is started with the repository's existing development no-auth mode. No unrelated service was stopped.

## Connected house and garage roofs (24 September 2026)

The wider capture exposed a second issue: the garage and house were overlapping
SAM instances, while the original main-roof seed still ended at the old image
cut. `roof_assembly.py` now tests raw building-mask junctions before duplicate
suppression and roof fitting. Only a positive overlap, substantial shared contact
(at least 1.5 m), and compatible local material colour permit an assembly. Detached
roofs, corner contacts, nested masks and contrasting materials stay separate.
Property boundaries do not establish roof continuity.

Raw component masks and junction evidence remain in each model snapshot. The
combined raw envelope is simplified once within the existing 0.45 m / 3% outer
boundary limits; a bounded chord alternative avoids retaining every mask step
when whole-edge fitting fails. Original openings remain subject to the existing
image-conditioned opening checks. Only tiny holes newly trapped between joined
masks receive the assembly-specific cleanup. A fuller prediction can replace a
clipped SAM model seed, with child references remapped only to emitted objects.
CRM geometry and scene feedback are never used in this model assembly step.

Running detector-only inference on `house-30286` produces one connected
330.67 m² house-and-garage candidate (`37ff5c02f77869d5`), fitted as one shared
12-face roof. It passes coverage, seam, overlap and sampled-runoff checks; these
are geometry checks, not measured accuracy or building compliance. This change
updates inference priors, not neural weights. The canonical review API saved
revision 9, preserving the prior outlines and all 24 edited apex lines on the
replaced main-roof object. The new candidate remains unapproved; previous apex
feedback is not silently rebound to the enlarged outline.

`test_roof_assembly.py` covers connected surfaces, detached/corner/contrasting
cases, courtyards, deterministic identity, clipped seeds and child references.
The full suite passed with 423 Python tests plus the Go and TypeScript checks.

## Method references

The reconstruction follows the separation of image-derived rooflines and height estimation described by [TU Delft's roofline extraction project](https://github.com/tudelft3d/Roofline-extraction-from-orthophotos). The installed monocular depth path normalizes relative depth; [Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2) distinguishes relative models from metric-depth fine-tuning. Relative depth is not an independent roof-height survey.
