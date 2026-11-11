# Fabric shade-sail classification — 1 October 2026

Objects `7ddfb55acd70b9fa` and `4e615c2e23cb5015` in `lot-29772` are now
labelled **Fabric shade sail**, with `kind: building` and the explicit
`structure_kind: shade-sail` subtype. The wider original image shows tensioned
fabric membranes, curved sides and corner anchors, consistent with the user's
canvas shade-sail identification. Textile composition and suspension heights
are unmeasured.

The local editor exposes this subtype in Structure kind. Shared consumers and
canonical save validation clear inherited hipped-roof parts, ridge/hip/valley
lines, roof annotations and rigid-roof checks. The scene and MapLibre readers
show only a footprint. They do not create building walls, a pitched surface,
height/apex handles or solar/skylight/chimney mounting support for fabric sails.
Rigid-roof API proposals and roof-line mutations explicitly refuse the subtype;
generic boundary editing remains available for eligible bound objects.

## Saved state and remaining review

| Store | Previous revision | Saved revision | Change |
| --- | ---: | ---: | --- |
| Local `lot-29772` | 1 | 2 | Both subtype/label corrections; incompatible roof estimates removed |
| Linked local `house-29785` | 1 | 2 | Same corrections in the source review |
| Fieldwork `addr-scene-house-29785-me373cb0a9d78-r0` | 2 | 3 | Mutable labels/notes/status and machine audit corrected |

Every ring and hole, source model/PNG, source binding and unrelated object was
preserved and checked after reload. Existing review history remains intact.
Fieldwork's immutable source generation was retained; its supported mutable
feedback describes the corrected type. A future canonical push can carry the
structured local subtype into a new generation.

Both masks contain only the portion visible in the earlier cropped frame.
Their straight northern edges are internal crop cuts in the wider lot image.
Classification does not complete those traces: both remain `needs_adjustment`,
with an outline issue, geometry unchecked and height unverified. No fabric
surface or complete membrane boundary is claimed. Observations are machine/T3;
human approval and training eligibility remain false.

The first lot save correctly refused missing group entries in the pre-existing
carried review. A subsequent read found those source groups restored without
other object/source changes; the refreshed revision-bound save preserved them.
The earlier broken-group snapshot remains in the case directory for audit.

## Verification and reusable guidance

The shade-sail regression covers footprint-only rendering, stale rigid priors,
mounting refusal, subtype recovery, canonical permissions/history, API refusal
before inference, and consecutive generic boundary refinements. Existing
geometry, roof-feedback, roof-line, object polygon and split regressions passed.
TypeScript checks passed; `task lint` passed with 29 existing warnings. Native
browser inspection verifies the saved subtype, pending outline and absent rigid
roof controls on both user-provided URLs. Before/after eyes guards passed and
the frozen pipeline code and source run remained unchanged.

The tracing skill now describes membrane boundaries, corner anchors, fabric
versus rigid roof evidence, crop fragments, separate outline/type/height
approval, and supported local versus Fieldwork subtype updates:
[shade-sail guidance](/Users/benebsworth/projects/addr/.opencode/skills/addr-roof-tracing/references/shade-sails.md).

[Canonical save receipts](/Users/benebsworth/projects/addr/data/reviews/eyes/shade-sails-2026-10-01/report.json)
include revisions, preserved source hashes and unresolved outline coverage.
The case directory also contains clean/outlined native-image crops, exact API
requests, responses, reloaded reviews and historical notes.
