# Roof surface refinement

The building extraction path now treats small same-material segmentation gaps
as possible roof texture artifacts. It retains the original detector ring and
holes in each prediction's `boundary_refinement` audit. Large openings, regular
openings above 0.3 m² and contrasting gaps remain for review. This is an image
and geometry prior after SAM3 inference; it does not update neural weights or
establish measured boundary accuracy.

Exterior cleanup moves the boundary by at most 0.25 m / three image pixels and
allows at most 2% symmetric area difference. It preserves concave eave recesses.
Building suppression processes large candidates first so an early tiny fragment
cannot discard a complete roof. Components below 1 m² are ignored for buildings
only. Raw mask identities keep same-image feedback associated with re-analysis.
Sections more than 95% contained in a retained whole roof are suppressed;
adjacent buildings and separate roofs inside genuine courtyards remain.

Roof fitting can simplify moderate stair-step noise for its support envelope;
the final planar faces are clipped back to the exact footprint. Courtyards stay
open. Saved roof graphs drive the aerial ridge, hip and valley overlay as well
as the 3D surfaces. Apex editing and independent geometry validation still use
the same registered metre frame.

## Reference scene

For `roof_training_250-29789`, detector object `267b0570989eeeb5` changed from
45 exterior corners / 25 texture holes to 31 corners / no holes. Broad-plane
fitting produces 16 faces rather than 31, with complete footprint coverage,
zero overlap and no reported seam or planarity errors. These are geometry
checks, not an accuracy benchmark against surveyed data.

Scene review revision 3 retains a traced main roof with 15 corners, preserving its
front recess and side changes. It replaces the overlapping detector and
pipeline proposals, including three duplicate roof sections, through the
canonical instance/review APIs. Re-analysis then carried the correction forward
without changing its outline or roof graph. Lineage and immutable snapshots
remain available, and all 103 pre-existing scene reviews are unchanged. The
left edge is partly occluded by vegetation. Its 15-face roof graph remains an
editable estimate with outline and 3D-shape review flags; neither roof-line
observations nor heights were approved.

Regression coverage lives in `test_roof_surface.py`, `test_scene_roof.py` and
`test-scene-geometry.mjs`: material-gap decisions, real openings, concavity,
fragment suppression, stable IDs, exact roof coverage, planar joins and
registered aerial graph selection. New corrections must pass the existing
human review and geographic holdout gates before becoming training targets.
