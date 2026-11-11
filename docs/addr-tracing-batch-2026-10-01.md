# Low-confidence roof and object tracing — 1 October 2026

Reviewed nine objects across seven eligible source scenes: five roofs, three solar detections and one tree crown. Saved four polygon corrections (three roofs and one solar bank) as revision 1 unchecked machine drafts. Kept the other five geometries unchanged and recorded why.

Each current perimeter was inspected against the original PNG; each saved candidate was also inspected with its revision-bound MCP preview and native-pixel numbered outline. Saved geometry was reloaded through the canonical API and matches the exact preview. All edits remain tier 3: geometry review, height verification, human approval and training eligibility are false. The three roof corrections are partial where the image leaves an edge unresolved.

The queue indexed 2268 low-evidence roofs and supplied 24 eligible captures after excluding existing/protected feedback and held-out or invalid frames. Detector scores describe model evidence, not measured boundary accuracy. No imagery, extraction, weights or protected reference data were edited.

| Case | Class / detector score | Outcome | Finding |
| --- | --- | --- | --- |
| [roof0](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80409&kind=building&status=all&object=cc22cc433bafe31a&layer=overlay) · `cc22cc433bafe31a` | building / 0.5158 | Saved partial draft | Trimmed south wall/shadow and east paving spill; retained the supported roof steps. Northwest bevel/fascia remains mixed within about 1–2 original pixels. |
| [roof1](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118440-80414&kind=building&status=all&object=103bd23746abddfc&layer=overlay) · `103bd23746abddfc` | building / 0.6052 | Kept / deferred | Kept the broadly supported outer envelope; recorded the interior roof-section/crease problem. West vegetation contacts and east section joins; 3D topology and heights remain unverified. |
| [roof3](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80408&kind=building&status=all&object=e85b42e0b6cecac6&layer=overlay) · `e85b42e0b6cecac6` | building / 0.6110 | Saved partial draft | Tightened canopy and garage south edges and removed the west projected-shadow step. North attachment/material strip and narrow canopy/garage junction remain ambiguous. |
| [roof5](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118438-80409&kind=building&status=all&object=df1d6b05f4476fb8&layer=overlay) · `df1d6b05f4476fb8` | building / 0.5002 | Kept / deferred | Kept the existing outline because the dark adjoining areas may be covered attachments. Dark southeast/southwest surfaces could be roofs or hardstand; removing them would guess the target. |
| [roof8](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118456-80415&kind=building&status=all&object=6fa45265f1f9d8f3&layer=overlay) · `6fa45265f1f9d8f3` | building / 0.5147 | Saved partial draft | Tightened the visible lower bright appendage and removed a facade corner; preserved tan cover. Most of the main perimeter remains under foliage; the draft is explicitly partial. |
| [tree0](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118452-80420&kind=tree&status=all&object=7ab378073bb77562&layer=overlay) · `7ab378073bb77562` | tree / 0.4511 | Kept / deferred | Kept a broadly supported foliage-crown outline. North/south contacts with adjoining crowns prevent exact object ownership. |
| [solar7](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118445-80410&kind=solar_panel&status=all&object=a4ee017682e5494f&layer=overlay) · `a4ee017682e5494f` | solar_panel / 0.4756 | Saved visible-bank draft | Replaced northern bare-roof mask steps with the four sustained visible module-bank edges. Module/frame/roof mixing is about 1–2 original pixels; mounting height and grouping beyond this visible bank are unapproved. |
| [solar0](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118446-80409&kind=solar_panel&status=all&object=c7e53ad78b6001c1&layer=overlay) · `c7e53ad78b6001c1` | solar_panel / 0.4502 | Kept / deferred | Kept the small array because the proposed precision is comparable to image blur. The bank spans roughly 12×17 original pixels; module, frame, glare and roof are mixed. |
| [solar6](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118445-80410&kind=solar_panel&status=all&object=6914cb690d0b890f&layer=overlay) · `6914cb690d0b890f` | solar_panel / 0.4746 | Kept / deferred | Recorded a bad north spur but deferred a whole-array replacement. Staggered banks and visible roof channels require a bank/gap interpretation; a broad quadrilateral would fill roof. |

## Saved comparisons

The three panels show original RGB, the former outline in cyan, and the applied proposal in amber. These are deterministic overlays on the original image; display magnification adds no source detail.

### roof0 — `cc22cc433bafe31a`

Trimmed south wall/shadow and east paving spill; retained the supported roof steps. Remaining limit: Northwest bevel/fascia remains mixed within about 1–2 original pixels.

![roof0 original, prior and applied polygon](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof0-comparison.png)

[Complete audit](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roofs-a/case-0-audit.md) · [Preview receipt](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof0-final/preview_roof_polygon-20261001T005758551033Z-8773e44b35f9.json) · [Native proposed inspection](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof0-final/inspect_roof_boundary-20261001T005933839638Z-70e215e759e7.json) · [Saved API reload](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof0-final/object_feedback-20261001T010410035923Z-9665df135677.json)

### roof3 — `e85b42e0b6cecac6`

Tightened canopy and garage south edges and removed the west projected-shadow step. Remaining limit: North attachment/material strip and narrow canopy/garage junction remain ambiguous.

![roof3 original, prior and applied polygon](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof3-comparison.png)

[Complete audit](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roofs-b/3/audit.json) · [Preview receipt](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof3-final/preview_roof_polygon-20261001T005804245288Z-1c089c388233.json) · [Native proposed inspection](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof3-final/inspect_roof_boundary-20261001T005934923712Z-6cc767b0a457.json) · [Saved API reload](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof3-final/object_feedback-20261001T010410345413Z-668ab073526c.json)

### roof8 — `6fa45265f1f9d8f3`

Tightened the visible lower bright appendage and removed a facade corner; preserved tan cover. Remaining limit: Most of the main perimeter remains under foliage; the draft is explicitly partial.

![roof8 original, prior and applied polygon](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof8-comparison.png)

[Complete audit](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roofs-b/8/audit.json) · [Preview receipt](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof8-final/preview_roof_polygon-20261001T005800053398Z-cc78fa66e57c.json) · [Native proposed inspection](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof8-final/inspect_roof_boundary-20261001T005933501140Z-63bf79a093a3.json) · [Saved API reload](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/roof8-final/object_feedback-20261001T010409803202Z-10c66f44768c.json)

### solar7 — `a4ee017682e5494f`

Replaced northern bare-roof mask steps with the four sustained visible module-bank edges. Remaining limit: Module/frame/roof mixing is about 1–2 original pixels; mounting height and grouping beyond this visible bank are unapproved.

![solar7 original, prior and applied polygon](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/solar7-comparison.png)

[Complete audit](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/solar7-audit.json) · [Preview receipt](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/solar7-final/preview_object_polygon-20261001T005802488045Z-b252ea42ccc2.json) · [Native proposed inspection](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/solar7-final/inspect_object_boundary-20261001T005933834936Z-72073eb054f0.json) · [Saved API reload](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/solar7-final/object_feedback-20261001T010409854526Z-d7cc51715e2a.json)

## Lessons added to the reusable skill

Solar review traces the visible module-bank envelope and distinguishes roof channels or separate banks from perimeter fit. Tree review traces visible foliage, preserving organic contours and naming ambiguous contacts with adjacent crowns. Both use the same complete current/proposed audit as roofs. Blur-sized edits and unresolved grouping decisions are deferred rather than presented as accurate replacements.

The generic MCP path now provides `object_feedback`, `inspect_object_boundary`, `preview_object_polygon` and `apply_object_polygon`. Kind, image hash, source model, current outline and saved revision remain bound to the preview. The API requires replacement geometry, cannot change class through this tool, preserves protected/imported feedback, and reopens only the edited class. Building roof-line edits retain their building-only contract.

The installed skill points to the project copy, so these lessons are available on the next invocation. Remaining perimeter limits must appear directly in saved notes as well as the audit: the preview’s separate uncertainty field remains in its diagnostic sidecar and is not automatically copied into CMS notes.

## Verification and evidence

The focused Python/tooling suite passed 90 tests. Roof/object parser and live API tests, TypeScript, targeted ESLint and Ruff checks passed. Four real SDK saves and subsequent reads confirm exact proposed geometry and unchecked provenance. Full lint passed (Go vet, ESLint with 29 existing warnings and no errors, and Ruff), using a writable temporary Go cache. Independent fresh API verification passed: all source model/PNG hashes are unchanged, all five retained review targets are unchanged, and geometry/provenance plus non-derived fields were preserved for 461 unrelated objects across the changed scenes. Saved revision 1 and the edited notes were also verified in the roof and solar CMS views.

[Structured report](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/report.json) · [Independent final verification](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/batch-final-verification.json) · [Before-source snapshot](/Users/benebsworth/projects/addr/data/reviews/eyes/tracing-batch-2026-10-01/sources-before.json) · [Guard before](/Users/benebsworth/projects/addr/data/reviews/eyes/guard-tracing-batch-before.json) · [Guard after](/Users/benebsworth/projects/addr/data/reviews/eyes/guard-tracing-batch-after.json)

T3 observations for roof1 and roof5 retain their unresolved geometry/topology findings without saving guessed replacements. Other retained-object audits are linked in the structured report.
