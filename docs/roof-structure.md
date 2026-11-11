# Roof geometry: observations, priors and checks

The estimator combines an observed outline, image/depth evidence and a general
roof-form prior. A plausible roof is not necessarily the actual roof. Keep these
three sources distinguishable in the CRM and training exports.

## General pitched-roof constraints

- Each ordinary roof face is planar. A quad whose vertices are independently
  adjusted can twist even if every vertex looks reasonable on its own.
- Neighbouring faces share the same elevation along their whole junction.
  Real steps require a separate roof level and a connecting wall/flashing;
  disconnected mesh heights alone are not evidence of a step.
- Faces cover the roof outline without gaps, outside triangles or overlaps.
- Ridges and hips are convex intersections; valleys are concave intersections.
  Rooflines are derived from the final shared face edges, not unrelated overlay
  strokes. Junctions must agree in both position and elevation.
- Pitched roofs need positive rise and drainage. An interior low point requires
  evidence of an internal drain. It must not silently appear from noisy depth.

These are modelling constraints, not certification of structural adequacy,
building-code compliance, flashing design or gutter capacity. Flat roofs,
mansards, curved roofs, dormers, different eave levels and internal drains need
additional roof families and evidence. Material-specific minimum pitch is not
inferred from colour. A tile/material label is insufficient to know its product
specification or local design requirements.

## Implementation

`scripts/ml/roof_structure.py` audits **emitted vertex geometry** independently
of the original plane-fit metadata. It checks planarity and seam error at 5 cm
numerical tolerance, 99.5% footprint coverage, face overlaps, below-eave vertices,
missing apexes and suspicious interior low points. These thresholds check mesh
consistency; they are not a claim of 5 cm positional or height accuracy.

The image/depth reconstruction is retained if it passes. Otherwise a validated
equal-eave/equal-pitch straight-skeleton hypothesis is attempted. Convex and
concave hipped footprints are supported; exactly rectangular gabled footprints
use a two-plane gable. Complex footprints use a hipped hypothesis. No address IDs
or individually authored roof templates select this prior. Holes and very complex
footprints are rejected rather than silently filled. Rejected reviewer cuts
prevent a conflicting prior from being accepted.

The prior is scaled using the existing estimated eave/ridge height. Its pitch is
bounded to 8–60 degrees as a configurable-in-code modelling assumption, not a
universal allowable roof pitch. The output is explicitly `structural-prior` with
`shape-prior-estimate` heights. Its geometry audit and the rejected image estimate's
audit are persisted and shown in the review screen. Passing an audit does not
approve a training label. Do not evaluate this prior against the very footprint
used to construct it and describe that as held-out ML accuracy.

The current feature detector still needs stronger evidence for multi-level
extensions, dormers and gable ends. Existing imagery cannot corroborate absolute
heights or structural loads. No neural-network weights were retrained in this pass.

### Straight eaves instead of tiny hip fans

Pipeline version 4 includes a conservative eave-corner proposal. A short convex
bevel between two longer, near-perpendicular eaves can be a rounded image mask,
not evidence for another roof plane. The proposal extends those supported eaves
to their intersection. On a simple wing this produces a straight terminal eave
and one triangular hip face, rather than a fan of small hips. It does not infer
a gable end.

The construction grid needs at least 70% angular coherence. Supporting edges must
be at least 2.4 m and within 10 degrees of that grid; corner turns must be
75–105 degrees. Bevels are limited to 2.75 m and 82% of the shorter supporting
edge. The complete proposal may deviate at most 1.5 m from the original outline
and add at most 4% area. These are explicit modelling tolerances, not measured
accuracy. Concave recesses, large chamfers and polygonal roofs are preserved.

For unreviewed captures, only the fallback roof-form hypothesis uses this
proposal: its planes are clipped back to the captured outline and independently
audited. Clipped corners may have varying edge heights. CRM outlines retain all
their authored corners. `eave_regularization` records the hypothesis's changes
in extraction, address payloads and cohort reports. Deduplication includes the
boundary source so a reviewed roof cannot inherit an unreviewed hypothesis.

The garage and L-wing ends at 27 Cambridge Road were explicitly corrected after
the user confirmed straight eaves with a single triangular hip face. The
before/after geometry and imagery provenance are retained in
`data/references/roof-eave-corrections-2026-09-22.json`; this remains an imagery
reference, with estimated ridge heights, rather than surveyed ground truth.

## Coordinate correction and imagery references (22 September 2026)

The Python vector-tile decoder previously used its default Cartesian y-up
coordinates as native y-down MVT coordinates, mirroring outlines across each
tile. All Python readers now request `y_coord_down=True`; the WGS84 encoder uses
the same convention. Regression tests encode an asymmetric northern roof and
verify its actual geographic position, including non-default layer extents.

Four visible roof outlines were traced from existing review imagery: 27 Cambridge
Road, 53 and 55 Ervin Road, and 78 Hawthory Road. The last correction separates
the dwelling and attached covered area from neighbouring buildings previously
merged into its footprint. The source hashes, pixel vertices, affine transforms,
WGS84 geometry and review limitations are in
`data/references/roof-outlines-2026-09-22.json`. These are agent-reviewed image
references, not survey measurements or independently held-out labels. CRM writes
preserve existing roof properties and use the authenticated/audited feature API.

## Sources

- [CGAL straight-skeleton user manual](https://doc.cgal.org/latest/Straight_skeleton_2/index.html):
  polygon wavefronts, planar roof faces and outward-draining roof construction.
- [Ren et al., Intuitive and Efficient Roof Modeling for Reconstruction and Synthesis](https://arxiv.org/abs/2109.07683):
  roof graphs and explicit planarity constraints for reconstruction.
- [ICON's py_straight_skeleton](https://github.com/iconbuild/py_straight_skeleton):
  the BSD-licensed implementation used for the generic hipped hypothesis.

Validation: `task test`, `task lint`, `task roof:cohort CLI_ARGS="--cohort suburban_benchmark"`,
`task verify:ui` and `task verify:reviews`.

## Gable clarification and image comparison

The user subsequently clarified that the ends should be gables (a triangular
prism / Toblerone form), superseding the earlier hip-end interpretation. Pipeline
version 5 compares hip caps against adjoining slopes extended to a gable end.
Complex L-shaped roofs retain their connecting valleys. Supporting RGB evidence
uses material colour continuity, local brightness steps and ridge-cap highlights;
a uniform tile colour does not distinguish the families. Ambiguous imagery is
reported explicitly and reviewer roof-family corrections are stored separately.
Gable walls follow the roof boundary heights through the ridge in all three
renderers. See `roof-training.md` for review graphs and learned model selection.

The roof-family distinction follows the diagrams in [FEMA's sloped-roof systems
guide](https://www.fema.gov/sites/default/files/documents/fema_p-2181-fact-sheet-3-3-1-roof-systems-sloped-roofs.pdf).

## Topology selection and sampled runoff

Automatic scene proposals now compare bounded construction-grid alternatives to
the detailed silhouette skeleton. The geometry audit additionally traces gravity
across faces and valleys, catching flat internal channels missed by a vertex-only
basin check. See [roof-physics.md](roof-physics.md) for thresholds, scope, reference
results and the distinction between physical validity and reconstruction accuracy.
