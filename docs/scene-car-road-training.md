# Car and road feedback, 23 September 2026

Seven existing scene reviews gained 15 image-reviewed outlines: 12 cars/vehicles
and three visible road surfaces. Ten clear false detections were rejected,
including roofs predicted as cars and private drives predicted as through roads.
The vehicle at the eastern edge of 22 Greenville Street was retraced to terminate
at the image boundary and marked clipped. Other accepted outlines were checked
against registered original-image crops. Three ambiguous driveway objects remain
needs-edit; they are neither confirmed cars nor confirmed false positives.

The reviews use the normal revisioned API. Original model files, unrelated
annotations, heights and 3D profiles were preserved. The audit is
`data/references/scene-car-road-review-2026-09-23.json`.

Dataset `cc35a7cf733dc1f2` contains 87 confirmed references and 69 rejected
detections. Complete image-layer checks permit 52 segmentation masks across 35
layers, up from 39 masks across 26 layers. The car training split now has 13
masks in six images/four area groups; road training has four masks in six
images/five groups, including two checked empty images. Uncertain classes and
incomplete layers remain withheld from COCO. These are image references, not
surveyed 3D labels.

## Prompt comparisons and fitted candidates

Car experiment `0fcb12e73c20145d` compared `car`, `parked car` and `vehicle`.
The original `car` wording retained all 13 training and five validation matches.
The alternatives changed masks and reduced some training extras, but did not
improve validation detection counts. Validation remains just one positive image.

Road experiment `48b1fdd2335f1984` compared `road`, `street road surface` and
`road carriageway`. `street road surface` retained all four training and six
validation matches, with seven training extras instead of sixteen and four
validation extras instead of five in the saved baseline. `road carriageway`
missed one reference in each split. Mean outline overlap and detection counts
are separate measures; fewer extras does not establish better boundaries.

Regularized quality filters were fitted using only training cases. No test
images were used for prompt selection, fitting or regularization selection.

| Candidate | Validation before filter | Validation after filter | Assessment |
| --- | --- | --- | --- |
| Cars `f186b9a3cc8e6e0f` | 5/5 matched, 2 extras | 4/5 matched, 1 extra | Loses an occluded car; F1 falls from 0.833 to 0.800 |
| Roads `3e1ef0f257e7cd87` | 6/6 matched, 4 extras | 5/6 matched, 1 extra | Loses a clipped/occluded road component despite higher F1 |

Both remain experimental. The car loss is `15825800d1b296f5` at 45 Marion
Avenue; the road loss is `3baa0c262754fc14` at 59 Ervin Road. Current annotations
remain intact. These failures show the need for more partial and occluded
positive training examples, plus validation in additional areas. Neither filter
was applied to production inference, saved objects or training labels.

Model candidates now shows the match loss beside the reduction in extras.
**Inspect lost matches** opens the affected image, selects the filtered mask and
identifies its frozen reference. Existing annotation links still open the live
editor; historical evaluation is not silently recomputed from later corrections.

## Reproduce

```sh
task scene:experiment CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --kind car --prompt 'car' --prompt 'parked car' --prompt 'vehicle'"
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --experiment data/experiments/scenes/0fcb12e73c20145d --prompt 'car'"
task scene:experiment CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --kind road --prompt 'road' --prompt 'street road surface' --prompt 'road carriageway'"
task scene:train-proposals CLI_ARGS="--dataset data/training/scenes/cc35a7cf733dc1f2 --experiment data/experiments/scenes/48b1fdd2335f1984 --prompt 'street road surface'"
```

## Verification

`task test` passed, including 284 Python tests and the scene geometry, feedback,
storage and queue checks. `task lint` passed with 31 existing frontend warnings;
the production build and strict UI audit passed. The candidate and training
browser checks passed, alongside `task verify:ui WEB_PORT=3008`.

The final production build was checked again with `verify:scene-candidates`.
The live candidate screen opened the road's lost reference at 59 Ervin Road,
selected its filtered proposal and linked to the existing annotation editor.
Candidate inspection and verification did not modify the saved reference.
