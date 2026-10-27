# Trees, hedges, bushes and mixed vegetation

The target is visible foliage in the source image, not an inferred stem count,
parcel planting inventory, ground footprint or measured height. Start with
clean RGB and wider planting context. Review both the outer foliage fringe and
potential internal boundaries before deciding the object granularity.
Verify that the selected pixels are actually vegetation: detector kind/prompt,
green tint, a long narrow outline or its location beside a roof is not enough.
Inspect `clipped_by_frame` and the source frame; an old crop cut can make a
crown appear to have a hedge-like straight side inside a larger current image.
Include neighbouring detector fragments in that context check: an unflagged
small clump beside a flagged crop fragment can still have ambiguous ownership.

## Trees and crown patches

Trace sustained leaf/crown-to-background transitions and meaningful crown
indentations. Preserve organic shape without following every leaf highlight.
Cast shadow often extends beyond the crown; shading within a crown is not a
hole. Do not invent a complete circular crown around an occluded section.
Keep crop cuts and overhanging/obscured segments explicitly uncertain.
An unresolved class or crop defect should be recorded before finer splitting;
partitioning a false-positive roof strip does not improve vegetation detection.

Touching crowns need separate exterior-fit and ownership findings. A broad
foliage envelope may be clear even when a seam between trees is hidden.
Possible internal seams should be sustained in the original image, supported
by context such as a visible gap, separate crown margins or a persistent
texture/colour transition. One bright apex, dark groove, concavity or height
prior is insufficient by itself: a single crown can be lobed or multistemmed.

When the image supports distinguishable canopy regions but not biological
instances, label children `Crown patch A`, `Crown patch B`, and record that tree
count is unresolved. Use `Tree crown A/B` only as an unchecked instance
hypothesis with the supporting evidence named. Do not label every bright lobe
as a tree or mark an aggregate crown as fully visible to enable a 3D renderer.

## Hedges and screens

Trace the visible foliage strip, its terminal ends and supported bends/width
changes. A trimmed hedge may have straight sustained sides; its end caps and
ragged edges still follow foliage. Fence lines, top/wall projections and shadow
are separate features. A continuous hedge usually stays one object even if
individual bushes form it; this imagery may not resolve the stems.

Separate hedge stretches where a gate, path or visible non-foliage break
interrupts them. Keep the stretches linked by a logical group rather than
filling the opening. A division for inspection or property allocation needs a
label such as `Hedge section A — allocation`, not a claimed physical foliage
break. Preserve the full visible strip across any purely administrative cut.
If the current mask wrongly fills an exterior-opening gate/path, the correction
may need a disconnected multi-object replacement; a coverage-preserving split
alone cannot remove that background. Follow the explicit operation limit in
[object-segmentation.md](object-segmentation.md) instead of claiming the gate was fixed.

## Bushes, shrubs and planting beds

Trace a distinct visible shrub clump where it separates from soil, lawn, paving
or neighbouring crowns. Low rounded foliage alone does not prove a bush rather
than a young tree or a shaded crown. Distinguish the selected foliage from the
bed's mulch/soil border; a planting-bed outline is a different target.

Split clearly separated shrub clumps when individual review is useful. If
crowns touch without resolvable ownership, keep a `Shrub cluster` or named
foliage patch. Retain visible interior soil/paving gaps; do not carve holes out
of shaded leaves. Avoid species, stem counts and measured height claims.

## Ground cover and mixed planting

State whether the target is grass/ground cover, a hedge, shrub foliage or a
mixed vegetated area. Preserve paths, exposed soil and other supported gaps.
When finer type separation is supported, name each patch and its evidence;
mixed material remains mixed where the image cannot resolve its ownership.
Different shade or sun exposure is not by itself a new vegetation type.

## Class and relationship representation

Current scene classes include `tree` and `vegetation`, not `hedge`, `bush`,
`shrub` or plant species. Preserve the source kind in a polygon/split operation.
Describe the hypothesised subtype in label/notes. If a detected tree actually
looks like a hedge, record the class issue separately; tightening its outline
does not silently convert class. Subtype text does not create a trained label.

`group_id` links flat logical collections such as garden patches or hedge
sections. `derived_from`/`replacement_ids` retain split genealogy. `parent_id`
represents physical attachment/support and must not be repurposed to create a
taxonomy of tree parts. See [object-segmentation.md](object-segmentation.md).

## Finer segmentation decision and review

1. Name the intended parts: biological crown hypotheses, visible foliage
   patches, continuous hedge sections, shrub clumps or allocation regions.
2. Inspect a possible seam in clean original RGB, at both exterior junctions
   and through its full length. Explain conflicting evidence and blur. If only
   the external fringe is supported, refine it and retain the aggregate.
3. Preview the children together over their current source. Inspect every
   child's perimeter/holes and every new internal seam. Supported shared seams
   meet exactly; no hidden gaps, overlap slivers or lost source coverage.
4. Record unresolved ownership, visibility and class issues in saved notes.
   Applying a split keeps all children unchecked and preserves original lineage.
   Reassess 3D estimates independently; splitting creates no measured heights.

There is no universal metre threshold or pixel band proving two trees. Native
blur and occlusion determine whether a proposed seam can be resolved. If several
equally plausible cuts remain, leave the aggregate or show an explicitly
uncertain proposal; arbitrary equal-area cuts are not image-supported instances.
