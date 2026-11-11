# Property blocks and straight flat-roof proposals

Building `324dd58f35837dfa` was isolated by the self-review UI even though the
other white row, `fc87bd6099540f00`, was already detected in the same capture.
Vicmap plan PS823073 contains both roof rows, eight numbered lots and one
common-property parcel. Their footprint overlap with the block is 99.56% and
100%, respectively. This establishes useful cadastral grouping context, not
ownership, measured wall locations or independently verified dwelling counts.

## General inference and review changes

`roof_block_context.py` fetches approved surface parcels and freezes the exact
response with each experiment. It groups the connected part of a subdivision
plan when common property is present; ordinary individual parcels work within
larger subdivisions too. Adjacent different plans, disconnected same-plan lots,
parcel holes and ambiguous overlapping plans cannot silently enlarge the group.
Large groups are bounded; incomplete imagery/lookup coverage is labelled.

The review defaults to **Whole property block**, with separate roof surfaces,
the yellow block perimeter, optional cadastral lot divisions and links to review
each roof. The same group appears in 3D. The driveway remains empty. Reported
parcel-position uncertainty here is 1.47 m; land boundaries are not roof folds.

The flat comparison now fits straight construction runs from retained raw
detector outlines, preserving supported setbacks and holes. A dominant eave
direction can survive noisy chamfers; offset parallel sides receive explicit
step corners instead of being averaged into a diagonal. The candidate is rebuilt
as an audited flat surface over the proposed outline.

The selected row changes from 16 to 8 boundary vertices; the opposite row changes
from 19 to 14. These are general inference rules, without site coordinates in
model code. There is no neural-weight retraining or automatic publication.

## Limits and checks

The explicit comparison allows up to 1.25 m maximum visible movement, 0.7 m at
the 95th percentile and 7% visible area difference from the raw detector. These
are hypothesis limits, not accuracy intervals. Original-image edge residuals
must stay within 0.15 pixels of the starting outline and below 1.5 pixels;
blank imagery abstains. The strict extraction path retains its existing limits.
The two proposals change roughly 6.7% of raw-mask area and have slightly worse
edge residuals, so straightness alone is not reported as an accuracy gain.
Saved edits and roof-line feedback are excluded from this refitting.

Final paired run: `f0ab3f493492f683` (same 12 source roofs; frozen parcel data
reused from `9eac40f1d6c1f3a1`). Seven block lookups resolve, two need more capture
context, and three remain ambiguous/unavailable. Pitched candidate outcomes are
unchanged: seven no-safe-gain and five need-evidence. No new model replaces an
existing prediction. Height steps and actual party-wall placement still need
review or additional evidence.

98 targeted tests pass, including parcel membership, courtyards, incomplete
blocks, ambiguous plans, image-bound snapshots, rotated straight-edge cases,
blank-image rejection and the existing canopy/feedback regressions. The web
build, TypeScript and targeted lint checks pass. All 257 suburb model hashes and
four Cambridge Road model/review hashes are unchanged.

Browser checks confirm both roof rows in aerial and 3D views, the block/selected
roof switch, cadastral divisions and links to each source roof. No console errors
were reported. After a slow cold start, the detail endpoint returned HTTP 200 in
0.337 seconds. The comparison is left open on the flat proposals and lot overlay.

Artifacts:

- `data/roof-review-loop/runs/f0ab3f493492f683/property-block-comparison.png`
- `data/roof-review-loop/runs/f0ab3f493492f683/parcels-0.json`
- `data/roof-review-loop/property-block-verification-2026-09-25.json`

Cadastral source: [Vicmap Parcel](https://discover.data.vic.gov.au/dataset/vicmap-property-rest-api),
© State of Victoria (Department of Transport and Planning), CC BY 4.0.
