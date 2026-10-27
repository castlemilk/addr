# Boundary fit and the perimeter audit

The default target is the visible roof surface's outer eave/fascia perimeter.
Exclude projected side walls, cast shadows, drives and yard. Internal material
seams, ridges, solar panels and equipment are not outer boundaries. If the user
wants a visible-only segmentation mask or a ground-level footprint, name that
different target before tracing.

## Read the evidence

Establish the object with clean original RGB and wider context. Vertex/grid
labels solve coordinate placement; they do not prove the roof interpretation.
Native pixels and nearest-neighbour magnification reveal what a smooth display
can obscure. Check `display_scale`: if either factor is below one, the card is
downsampled. Inspect a smaller edge region or original source pixels before
placing its corners. Enhancement can help locate an edge but adds no independent view.

Look for a sustained roof-to-background transition along each edge. A parallel
dark boundary beyond a bright roof can be wall or cast shadow; do not follow it
just because its gradient is stronger. At corners, inspect both adjacent eaves
and their meeting. Retain visible steps. Do not contour around leaf texture as
though it were an eave. Missing image context is not a physical roof end. Gaps
need their own pass; roof color alone does not establish a courtyard hole.

In oblique imagery, facade ledges or balconies can produce several parallel
bright and dark strips beside the roof. A bright strip is not necessarily the
roof deck, and the farthest silhouette can belong to a wall. Follow the roof's
visible edge through each adjoining roof section, checking both ends of any
setback. Do not move an entire side inward by a uniform amount when the roof
has supported setbacks. Conversely, an intermittent wall strip does not justify
adding a step: preserve a turn only when clean RGB supports the roof transition;
otherwise name that segment unresolved.

For a supported straight eave, check several points and both corner intersections.
Use a rough native-pixel offset/uncertainty band if useful; do not invent subpixel
confidence. Adding unsupported bends is not tighter fit. Removing vertices
without checking missing roof coverage is not improvement.

## Edge/corner audit

Outer edge `i` joins vertex `i` to `(i+1) mod n`; holes have separate rings.
Keep a compact table or structured equivalent:

| Segment / corner | Evidence in original RGB | Mismatch | Decision / uncertainty |
| --- | --- | --- | --- |
| Edge 0 and its ends | Bright roof ends above a dark band | Wall/shadow included | Move to visible transition; fascia blur remains |
| Vertex 5 | Background lies beyond a straight eave | Unsupported outward point | Intersect supported eaves |

These are examples, not assumed findings. Give each proposed segment a state:
`supported`, `needs_refinement`, `unresolved`, or `uninspected`. A finished
perimeter review has no `uninspected` segments; a claimed tight visible fit also
has no resolvable `needs_refinement` segments. A valid partial draft can retain
explicitly shown unresolved segments.

Rebuild the audit for the proposed ring: vertex deletion changes edge indices.
Check top, bottom, both sides, every step/re-entrant corner and holes actually
present; these are coverage reminders, not rectangular-shape assumptions.
Resolve contradictions between wider context and close-ups before saving.

## Feedback and evidence

Keep source hash/frame, revision, inspection/preview artifacts, candidate points
and each edit's visible reason. On user feedback, inspect the identified residual
mismatch in raw pixels, update its edge finding, then rerun the whole proposed
perimeter pass. Store feedback separately from the agent's inference. 'Looks
good' does not set human checks or create a surveyed reference.
