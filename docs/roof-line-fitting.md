# Image-guided roof-line fitting

Automatic scene roof proposals previously used image evidence to compare some
hip/gable ends, while the main ridge graph followed the footprint skeleton.
Small bends in an outline could therefore place internal roof lines poorly.

`scripts/ml/roof_line_fit.py` now follows the structural proposal with a
registered image fit:

1. Run line-segment detection on the original grayscale image and a denoised,
   contrast-limited adaptive histogram equalization (CLAHE) view.
2. Clip detections to the roof interior. Check continuity and material on the
   original RGB image; suppress duplicates, vegetation transitions and dense
   repeating seam families.
3. Match nearby, similarly directed roof edges to supported image segments.
4. Jointly fit roof vertices and face planes, preserving all footprint corner
   coordinates and heights. Gable apexes on straight boundary edges can slide
   along those edges. Positive projected triangles discourage inverted faces.
5. Accept only useful alignment gains with exact footprint coverage and passing
   independent planarity, seam, drainage and pitch checks. Otherwise retain the
   structural prior. Shifts are bounded; large graphs use the existing prior.

This refines the existing topology. It cannot establish hidden roof geometry,
surveyed heights or a new roof family from ambiguous pixels. It does not train
new weights. Image-matching residuals are diagnostics, not surveyed error or a
held-out accuracy benchmark. Roof-family and linework approval remain separate
human feedback gates.

## Review controls

**Enhance roof detail** switches both the aerial canvas and apex editor to a
photometric view of the same image at the same dimensions. **Show original
imagery** restores the original. No geometric warp, generated detail, coordinate
change or annotation save occurs. The image endpoint verifies the original
SHA-256 and caches at most eight derived images. The original file is untouched.

The scene roof API runs the same refinement as extraction. Its `line_refinement`
diagnostics describe the proposal; the saved geometry note records the number
of fitted edges and retains uncertainty for unmatched structural estimates.

## Reference scene

In `roof_training_250-29789`, revision 4 updates only the roof graph of
`33e97eb2-b3e1-4184-ab4f-8fbf22d0f974`, preserving its 15-corner outline, eaves,
review flags and earlier revisions. Eight graph edges matched ten retained image
segments. The support-weighted endpoint residual decreased from 0.6949 m to
0.2862 m; the largest junction movement was 2.0942 m. The independent audit
reported complete coverage, no overlap, a 0.0021 m maximum plane residual and
a 0.0041 m maximum seam residual. These quantify this fit only. The roof is
still marked for review, with no verified roof-line or height labels.

Tests cover a known displaced synthetic ridge, both windings, boundary-constrained
gable apexes, fixed eave heights, exact coverage, uniform imagery, vegetation,
repeating tile lines, failure fallback and invalid registration. The browser
roof test covers enhancement toggling, unchanged PNG dimensions and original
bytes, shared aerial/apex registration, persistence and 3D rendering.

## Method references

- [OpenCV CLAHE documentation](https://docs.opencv.org/4.13.0/d5/daf/tutorial_py_histogram_equalization.html)
- [OpenCV line-segment detector documentation](https://docs.opencv.org/4.13.0/db/d73/classcv_1_1LineSegmentDetector.html)

Automatic scene proposals also compare simpler structural topologies before
final acceptance. Support roofs are fitted before clipping so occlusion cuts do
not force extra eaves or constrain fictitious gutter heights. Gravity/runoff
checks independently audit the emitted surfaces. See [roof-physics.md](roof-physics.md)
for selection costs, scope and the six-plane reference correction.
## Independent image evidence for each crease

`registered-roof-lines-v6` prevents one detected image segment from pulling
separate model creases onto the same feature. When provisional matches compete,
the fitter keeps the continuous crease with the strongest supported overlap.
Connected collinear subdivisions share evidence; disconnected or angled runs do
not. Unmatched creases retain their structural priors. This changes correspondence
selection before the joint plane fit, not the saved outline or reviewer graph.

The September 24 Cambridge feedback exposed a short hip cluster borrowing the
long neighbouring valley's image evidence. Removing that conflicting match
improved the short end apex without importing reviewer coordinates. The frozen
`independent-crease-evidence.json` regression contains only automatic seed
geometry and registered image observations. Existing parallel-eave, courtyard,
seam, coverage and sampled runoff checks still apply. This is an inference
change, not retrained neural weights or verified roof heights.
