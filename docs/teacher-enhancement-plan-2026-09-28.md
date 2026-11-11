# Enhancing the hi-res SAM3 teacher: ranked plan and first results (28 September 2026)

A design panel (three independent designs, two judges, one synthesis) produced a six-item plan. Its binding rule:
every enhancement is scored by a judge that is not the shape prior it optimises. Everything below is agreement or
evidence between model runs and captures, never accuracy. Machine-readable judge outputs live under
`data/reviews/eyes/training/distill-<source>/teacher/judges/`.

## The ranked plan

| rank | item | status tonight |
|---|---|---|
| 1 | Two-capture judge kit: cell-keyed split, clipped outline judge, image-only registration, J1 boundary judge on the other capture's pixels, perturbation ladder | built and run (`teacher_judges.py`: outlines, edge-support, register, ladder, boundary, cross-capture, ledger) |
| 2 | Per-instance evidence ledger with epoch-matched nDSM and a pre-registered keep / ignore / drop policy | ledger built without nDSM; policy recorded per instance, not applied |
| 3 | Corners below SAM3's 0.3 m logit cell: zoom pilot, seam restitch, guided upsampling | deferred: the ladder shows the corner-zone judge cannot resolve 0.3 m rounding at 7.5 cm, so this needs a finer judge first |
| 4 | One consolidated SAM3 re-pass: raw-piece and logit cache, hflip view, prompt ensemble, negative prompts | deferred (about 2.5 h of SAM3 CPU per capture) |
| 5 | Orthogonalisation gated by edge evidence on the native outline; verdict on the shipped `segmentation_regularised` field | measured: see below |
| 6 | Student transfer harness: head-ceiling test, Vicmap tree-point judge, Google-like degradation, paired arms | deferred |

Rejected by the judges, with reasons recorded in the workflow journal: graded keep-weights (the weight only reaches
the mask loss), registration by mask agreement (circular), colour transfer from Google tiles into CC BY data
(licence), finer-sensor super-teachers judged on their own pixels, YouTube video.

## What the judges say about the Christchurch teacher (2025, 60 windows)

- Outlines (LINZ NZ Building Outlines, clipped to the window, instances of 10 m² and more judged): teacher
  whole-building 64 percent, part 16 percent, unmatched 13 percent; control 84 / 8 / 4. Outline coverage: teacher
  91 percent (0.90 to 0.93), control 68 percent (0.66 to 0.70). The teacher's extra detections are mostly pieces of
  mapped roofs and small structures the outlines do not carry.
- Edge support on the teacher's own pixels: 0.88 median for the teacher, 0.53 for the control, 0.27 for random
  circles; the teacher wins on every one of the 60 windows (paired median +0.33).
- Registration between the 2025 and 2020-21 captures is image-only (phase correlation per window, correlation of
  blurred gradient magnitude per building). Window shifts are 0.27 m median; per-building residuals against the
  window shift are 0.39 m median, so roofs really move between captures and a per-building offset is required.
  Raw-gradient correlation matched under 1 percent of buildings; blurred-gradient correlation matches 56 percent.
- Cross-capture (same cells taught from 2020-21), with each 2025 instance moved by its image-derived shift before
  matching: 74.5 percent of 2025 teacher instances have a one-to-one match in 2020-21 at IoU 0.5 (0.72 to 0.77 by
  window bootstrap), median IoU 0.89, median boundary distance 0.21 m; sheds under 50 m² match at IoU 0.82, houses at
  0.92, large roofs at 0.93. Without registration the same match rate was 72 percent at median IoU 0.78 and 0.45 m,
  so about half of the apparent cross-capture disagreement was the captures' own misregistration. The 2020-21
  teacher's own premise replicates 2025 (right-corner share 0.486 against 0.109).

## Perturbation ladder (what the J1 judge can see)

J1 is the share of an outline's boundary samples whose gradient profile, taken along the normal in the OTHER
capture's pixels at the registered offset, peaks within the tolerance on a strong, aligned edge. Train cells only,
2,682 outline-matched buildings of 20 m² and more:

| variant of the raw 2025 outline | J1 at 0.10 m tolerance | corner zone |
|---|---|---|
| raw | 0.298 (0.285 to 0.311) | 0.194 |
| shifted 0.075 m in or out | 0.258 / 0.260 | 0.167 / 0.183 |
| shifted 0.15 m in or out | 0.165 / 0.177 | 0.110 / 0.140 |
| corners rounded 0.15 m | 0.306 | 0.203 |
| corners rounded 0.30 m | 0.316 | 0.209 |

A 0.075 m shift separates from raw with non-overlapping intervals, so the minimum detectable boundary offset is at
or below one native pixel. Rounding the corners by 0.3 m does not lower the corner-zone score (it rises slightly,
because smoothing removes staircase noise), so this judge cannot decide corner sharpness at 7.5 cm. Item 3 waits for
a finer judge (the plan's J2 line-segment corners, or a finer second capture).

## Gated orthogonalisation and the shipped regularised field (item 5)

Judge cells (18 windows, 1,375 buildings), outlines drawn on 2025, scored on 2020-21 pixels at the registered offset:

| outline | buildings | J1 pooled (0.10 m) | corner zone | per-building higher / lower than raw |
|---|---|---|---|---|
| raw | 1,375 | 0.301 (0.283 to 0.318) | 0.195 | reference |
| `common.orthogonalise` (the shipped `segmentation_regularised`) | 1,099 accepted by its own gates | 0.380 (0.361 to 0.398) | 0.265 | 837 / 262 |
| gated on the 2025 pixels' own edge support | 519 accepted | 0.408 (0.381 to 0.435) | 0.293 | 435 / 84 |

On the buildings the gate refuses but ungated orthogonalisation accepts (580), the squared outline still scores
0.353 against 0.307 raw (402 higher, 178 lower). At 0.20 m tolerance the ordering is the same (0.48 raw, 0.60
ungated, 0.63 gated). The gain of squaring (+0.08) is several times the effect of a 0.075 m shift (-0.04), so it sits
well above the judge's floor, and the corner zone rises where rounding would not move it. Verdict: the shipped
`segmentation_regularised` target is supported by the second capture; the gate trades coverage for fewer
regressions (16 percent of accepted buildings score lower against 24 percent ungated). The role-swapped replication
(2020-21 outlines judged on 2025 pixels, 1,344 buildings, same 18 cells) gives the same ordering: raw 0.306 (0.292 to
0.320), ungated 0.387 (854 higher / 193 lower), gated 0.413 (462 / 65). Both directions pass the plan's rule, so
squaring is adopted as the default label variant for the next export once the fair same-building comparison
between gated and ungated is written into the dataset card.

## Ledger (item 2, recommendations only)

`teacher_judges ledger` writes one row per teacher instance joining SAM3 score, area, nesting in a larger instance,
window-edge cut, outline class, edge support, registration correlation and cross-capture presence, and a
recommendation under `ledger-policy-v1` (keep / part / ignore / review). On the 12,679 Christchurch 2025 instances:
keep 75 percent, part 6 percent, ignore 6 percent (touching no outline, under 12 m², absent from 2020-21), review
13 percent. Houses of 50 to 200 m² are 91 percent keep; instances under 50 m² are 60 percent keep, 12 percent
ignore, 20 percent review. Nothing in the exports changes until a policy is switched on and judged by a signal it
never used (the plan's nDSM leg is not built yet).

## Next

1. Switch the export's default target to the squared outline where the gate accepts it (raw otherwise), with the
   fair same-building comparison in the dataset card; both Christchurch captures now share one cell-keyed split
   (0 of 60 cells differ) so they can be pooled.
2. nDSM reader for the LINZ 1 m DSM to give the ledger its second leg; then judge the keep / ignore policy
   leave-one-signal-out on the 18 judge cells.
3. Item 3 needs a judge that resolves corners: LSD-corner distance (J2) on the other capture, or the 5 cm 2021 and
   2025 Christchurch captures as a finer second capture.
