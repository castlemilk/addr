# Roof editing workspace — September 2026

The built-in imagegen tool produced [the design wireframe](roof-editing-workspace-v1.png)
using [this prompt](roof-editing-workspace-prompt.txt). The generated roof is an
illustrative layout reference only. The application uses the original registered
imagery, current saved objects and existing data encodings.

## Review of the saved edit

Scene `roof_training_250-29789`, object `33e97eb2-b3e1-4184-ab4f-8fbf22d0f974`,
revision 6, saved 2026-09-23T11:53:08.237Z. Retrieved through the canonical roof
feedback API. It contains 15 checked lines compared with 9 in the starting
estimate: one relocated line plus six added segments. Point 12 moved 4.171 m
in the registered image frame. All segments are inside the captured outline.
The single free endpoint at point 36 remains about 0.431 m from the eave.
These distances describe the image coordinates, not surveyed measurements.

The edited upper junction and additional branches provide a much more explicit
reference for the next fit than the original broad-plane estimate. The added
segments are all labelled ridge; their geometric positions and semantic types
need to be considered separately. A branch ending at an inward eave corner may
be a valley, while an outward falling intersection may be a hip. The saved
labels have not been automatically reclassified, fitted into a new 3D mesh or
promoted to training ground truth. The user was asked whether the added types
were intentional. The original outline/shape review flags remain intact.

## Implemented design

- One focused roof-line workspace replaces the review card while editing.
- The canvas, line inspector, contextual tools and one save bar are grouped.
- The inspector exposes per-type counts/visibility, selection by line list,
  length and type guidance, shared-junction/delete actions, comparison layers,
  notes and the explicit image check.
- Less frequent reset/clear controls use a native disclosure; empty scene
  layers and scene actions are similarly collapsed without removing access.
- Small screens stack the same controls with natural document scrolling.
- Canonical scene saves, revision conflicts, draft retention, history, source
  images and manual labels keep their existing owners.

## Design reconciliation

| Prior issue | Implementation |
| --- | --- |
| Duplicate image and save controls below the review page | Focused workspace with one active editor and save bar |
| Row of unrelated editing buttons | Contextual inspector plus an advanced disclosure |
| Hard-coded editor panel colours | Existing `--review-*` surface, border and text tokens |
| Selecting short branches requires precise pointer placement | Keyboard-accessible line list with type and length |

The design preserves the established dark navy review identity, Geist UI,
Lucide icons, metric units, native selects, data colours, visible scrollbars and
inline save/error feedback. It does not copy invented dates, roof geometry,
scene thumbnails or accuracy claims from the generated wireframe.

## Verification

The production build, `task test`, `task lint`, `task verify:ui WEB_PORT=3008`,
scene browser checks and `task verify:scene-roof WEB_PORT=3008` passed. The roof
browser check covers line drawing, movement, classification, junctions,
undo/redo, visibility, save/reload/API retrieval, failed-save draft retention
and retry, and mobile overflow. Desktop and mobile screenshots were reviewed.
The strict design audit passed. Lint retains 30 existing warnings and no
errors; the design document lint has three existing token-reference warnings.

Final canonical API retrieval still returns revision 6 with 15 lines. SHA-256
checks of all 104 existing non-QA review files match the pre-change baseline.
No model weights or saved user annotations changed during this UI work.
