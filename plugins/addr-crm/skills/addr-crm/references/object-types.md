# Choose the visible boundary for the class

Read the selected class's row before tracing. Preserve source class, physical
parent and object identity in a boundary-only edit. A correction to XY does not
verify class, height, placement, ownership or individual instance count.

| Class | Default target | Common false boundary / useful finer segmentation |
| --- | --- | --- |
| `building` | Visible roof or cover perimeter, including supported attached sections and real courtyard gaps. | Wall projections, cast shadows and drives; material seams and ridges are internal. A fabric membrane is a `shade-sail` subtype, not evidence for a rigid hipped roof; see [shade-sails.md](shade-sails.md). Separate detached roofs only with image evidence. Use [boundary-fit.md](boundary-fit.md) for rigid eaves and corners. |
| `solar_panel` | Visible module-bank envelope and its supported frame edge. | Surrounding roof, mounting shadows and glare. Distinct banks separated by exposed roof can be separate parts; do not bridge roof channels or divide every module by default. Parent roof/height needs separate review. |
| `tree` | Visible foliage-crown envelope. | Cast shadow, inferred trunk/ground footprint and adjacent crown ownership. Preserve organic edges. Lobes or multiple highlights do not establish individual trees. See [vegetation.md](vegetation.md). |
| `vegetation` | The named visible foliage/ground-cover patch: hedge, shrub clump, grass or mixed planting. | Shade, soil, paths, fences and unrelated plants. Separate supported types/patches without inventing species or heights; hedge/bush are descriptive labels. See [vegetation.md](vegetation.md). |
| `car` | Visible vehicle silhouette, including resolvable body projections. | Cast shadow, parking lines and neighbouring vehicles. Separate distinguishable vehicles; do not invent an occluded bonnet or infer a complete chassis from a partial view. |
| `road` | Visible carriageway/paved surface within the captured frame. | Sidewalks, verges, shadows, lane markings and parcel lines. A lane line is normally internal texture; a requested allocation split is not a physical road edge. Name whether parking bays belong to the target. |
| `driveway` | Visible driveway paving and supported branches/aprons. | Lawn, road, footpath and shaded vegetation. Distinguish a shared driveway boundary from property allocation; preserve paved connectors and genuine planted islands. |
| `footpath` | Visible walkable paved strip and junctions. | Verge, road and tree shadows. Preserve sustained bends/width changes; a shaded continuation is uncertain. Do not bridge an actual break in paving. |
| `pool` | State the target: visible water basin by default; coping/pool structure only if requested. | Deck, coping, pool cover, fence and shadow. Dark water is not a hole. A covered basin may require target clarification rather than following its colour. |
| `water` | Visible water surface and supported shoreline at capture time. | Shadow, damp paving, reflections and vegetation. Retain genuine islands; vegetation over water creates occlusion, not automatically an interior water gap. |
| `skylight` | Visible skylight/frame footprint on the roof. | Glare, flashing, roof seams and shadow. Tiny mixed pixels may not support tighter corners; separate distinct units only when their outlines are resolvable. |
| `chimney` | Visible chimney structure in the image plane. | Long cast shadow or an inferred ground footprint. A cap, side wall and displaced base can differ in oblique imagery; state the traced surface and leave mounting/height unverified. |
| `other` | A specifically named visible object and its interpretation. | An arbitrary texture patch. Describe what the polygon covers before fitting it; unresolved class is a class-review finding, not permission to relabel automatically. |

## Shared decisions

Use context to identify the surface, then original pixels to place its boundary.
Smooth display, colour differences and a successful polygon validator do not
establish the target. Inspect both false inclusion and omitted object pixels.
Do not replace organic foliage with a convenient rectangle or import roof
right-angle assumptions into cars, vegetation or water.

Interior gaps need visible non-object evidence. A dark patch can be shade or
texture; a pale patch can be glare. A fully enclosed real gap is a hole; a gap
open to the exterior is a notch or separate component. The scene stores one
connected polygon per object plus holes, so disconnected components need
separate objects linked through a flat group or lineage.

Choose granularity based on the requested task and available detail. A solar
bank, continuous hedge or mixed garden may be the right aggregate object.
Finer segmentation can distinguish supported parts, but it should not imply
species, stems, units or measurements that the imagery cannot resolve.

For every proposed boundary, retain the reason for each move and the unresolved
limits directly in saved notes as well as the case audit. Keep source model
predictions for comparison and show the registered current/proposed overlay.
