# Roof expansion and self-review

New neighbourhood captures use `scene_house_batch`: it freezes inference code,
weights and a reference scene before extracting image-only proposals. Existing
scene models and saved reviews are skipped. Captures overlap, so counts describe
roof observations, not distinct properties or independently labelled examples.

```sh
scripts/ml/.venv/bin/python -m scripts.ml.scene_house_batch \
  --baseline house-30296 --addresses 29406 25823 25915 --self-review --repair

task scene:self-review CLI_ARGS="--batch BATCH_ID --repair --max-repairs 12"
# Or audit existing scenes without publishing any changes:
task scene:self-review CLI_ARGS="--scenes house-30286 house-30296"
```

The batch ID is printed by extraction. `--self-review` runs the audit after
extraction; the standalone task can also review an existing batch. `--repair` is explicit; otherwise the
review is read-only. The bounded process is:

1. Verify the source image checksum, dimensions, registration and whole-image
   geography. Recompute coverage, planarity, shared heights and sampled drainage
   from both the rendered mesh and editable graph. Compare their height fields.
2. Flag missing/invalid surfaces, excessive folds and tiny planes, weak or
   conflicting image lines, crop cuts, inferred hidden eaves and questionable
   solar-to-roof associations. Complexity and image findings are review cues,
   not proof that a roof is wrong. Texture, shadows and solar can create lines.
3. For unreviewed SAM roof proposals in training geography, try two alternate
   rise hypotheses (1.5 m and 3 m) through the existing general image-guided roof
   fitter. Keep the exact boundary and eave height. Skip clipped roofs, saved
   review scenes, protected baselines and validation/test/mixed geography.
4. Independently audit each candidate again. A valid existing roof may change
   only with a meaningful complexity/cost gain and no material image-evidence
   regression, using the same image observations. A missing/invalid surface
   needs sufficient image evidence before a replacement can be published.
5. Recheck the model/image identity while holding the API's review mutex. Abort
   publication if a review appeared, an edit is in progress, or inputs changed.
   Keep immutable original model versions and record before/candidate snapshots,
   code hashes, attempts and the accepted model signature.

Results live in `data/scene-self-reviews/RUN_ID/report.json`, with a linked
`REVIEW.md` queue. Scene titles refer to captures and can include neighbouring
houses. The queue prioritises hard defects and full-view roofs; image-edge
observations need a larger capture. Unresolved findings remain visible in the
report. Re-running after a model change creates a new provenance record.

This is automated inference validation and bounded hypothesis selection. It
does not update neural weights, approve human labels, infer measured heights,
or establish construction compliance. Export and training keep their separate
review and geographic leakage gates. Models that pass these checks still need
image review and held-out evaluation before an accuracy improvement is claimed.
