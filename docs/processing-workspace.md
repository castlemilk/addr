# Suburb processing workspace

Open `/scenes/processing`, or use **Processing workspace** in the map's analysed
scene layers panel. Scene review and training coverage also link to it.

- **Roof inventory** uses TanStack Table with sorting, confidence/review filters,
  search, pagination and registered aerial/roof-line thumbnails.
- Select a roof for its evidence reasons, footprint area, review state, aerial
  view and the existing 3D renderer. The review action opens the exact scene and
  object on its model layer. Capture titles remain image context, not addresses
  assigned to neighbouring roofs.
- **Coverage map** shows the same filtered inventory and the official suburb
  boundary. Select roofs or processing tiles; switch streets/aerial or hide the
  tile grid. **Full suburb** resets the camera.
- **Processing queue** shows every planned tile, including pending, running,
  complete, failed and interrupted states, attempts and measured tile duration.
  Completed captures link to scene review. Failed items retain their error.
- **Self-review** starts a bounded batch of 12 low-evidence roofs. Select a row
  for original/candidate aerial, roof-line and 3D comparisons, diagnostic
  changes and rejection reasons. Contrast enhancement preserves registration.
  Recommended next steps explain the likely evidence bottleneck. Clipped roofs
  can link to possible fuller cached observations for identity checking.
  Rejected hypotheses stay inspectable and clearly labelled as not applied.
  Candidates are separate artifacts; this view does not replace the frozen
  scan, approve labels or update neural weights. See [the loop documentation](roof-review-loop.md).

The read-only `/api/scenes/processing` feed refreshes every 20 seconds while the
page is visible and on focus. Errors keep the last successful snapshot visible.
Turning off live updates pauses this page's polling, **not** the worker. The
frozen batch continues independently when the browser closes. Completion is
reported as tiles, with observed roof and solar counts; the house denominator
is unknown until detection finishes. Stage detail within inference is unavailable
from the frozen worker and isn't invented.

Confidence is a model-evidence band, not a per-roof probability. The inventory
uses the existing geographically deduplicated confidence query, limited to roofs
whose centroids fall inside the suburb, including earlier analysed scenes. The
batch counts are owned detections from completed batch tiles, so they can differ
from the inventory total. Stale reviewer approvals are labelled for recovery.

`/api/scenes/preview?id=...&object=...&version=...` serves one model object in its
original image frame. It rejects a model-version mismatch with 409. A bounded
server cache is invalidated by model mtime. Predictions, saved reviews, the
confidence model and the active run's frozen snapshot are not modified.
