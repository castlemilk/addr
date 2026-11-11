---
version: alpha
name: GeoAU roof review
colors:
  background: "#090f1c"
  surface: "#111b2b"
  raised: "#1b293d"
  border: "#2a3b51"
  foreground: "#edf3f8"
  muted: "#a0b1c5"
  primary: "#67d7e7"
typography:
  sans:
    fontFamily: "Geist, system-ui, sans-serif"
  mono:
    fontFamily: "Geist Mono, ui-monospace, monospace"
rounded:
  DEFAULT: "6px"
  panel: "8px"
spacing:
  workspace: "28px"
  panel-gap: "14px"
components:
  review-button:
    height: "36px"
    backgroundColor: "{colors.surface}"
    textColor: "{colors.foreground}"
---

# GeoAU design system

The processing workspace at `/scenes/processing` uses the same navy, cyan and
Geist palette. A single progress strip leads into inventory, coverage map and
queue views. Roof imagery anchors the table; a right-hand inspector opens only
when a roof or tile is selected. Keep tile completion, discovered roof counts,
model evidence and human review status distinct. On narrow screens, the
inspector moves above the results and wide tables scroll within their panel.

## Overview

An inspection workbench for Australian address and roof data. Preserve the existing dark navy studio and light map. The imagery and roof geometry carry the visual identity; surrounding controls stay quiet. The review queue, side-by-side aerial and 3D surfaces, and source labels organise an iterative correction workflow. Desktop is the primary inspection surface; smaller screens stack the same tools and retain all actions. English (Australia), metric measurements.

## Colors

Runtime ownership: `src/app/reviews/reviews.css`, `--review-*` variables, consumed by the review page. Global scrollbars and focus indicators live in `src/app/globals.css`. Cyan identifies captured boundaries and primary actions; amber marks correction work, green marks approval, rose marks rejection. Always pair colour with a text label. The map retains its established theme.

## Typography

Use the app's existing Geist variable for review UI, with system fallback. Address headings are 20–28px, section headings 14px, controls 12–13px. Tabular numerals aid measurement comparisons; monospace is reserved for actual coordinates or code. No promotional headlines in inspection workflows.

## Layout

A 256px review queue beside a flexible workspace. At smaller desktop widths the queue becomes 225px; below 650px it becomes a bounded list above the work area. The document scrolls naturally. Each viewport owns its canvas and horizontal toolbar overflow. Assessment notes and save actions stay in document flow.

## Elevation & Depth

Use borders to separate tools. Keep diagnostic overlays optional so the building remains visible. Avoid decorative shadows or glows.

## Shapes

Six-pixel controls and eight-pixel viewport corners. Status indications combine text with a small dot. Lucide supplies interface icons.

## Components

Review controls share `.review-button`; labels, input, select and textarea styles belong to the review shell. Native selects intentionally retain OS keyboard and popup behaviour. The existing map/CRM controls retain their owners. `RoofReviewCanvas` owns 2D inspection; `Roof3DPreview` owns model navigation.

## Do's and Don'ts

Show provenance next to geometry. Call colour-mask overlap an image check, never surveyed accuracy. Label monocular heights as estimates. Never fabricate property boundaries, neighbouring addresses, or verification status. Preserve focus rings, operable scrollbars, error recovery and reduced motion. Preserve incumbent map styling when improving the studio.

The review palette also includes semantic and interaction variants: success `#8ce2ba`, warning `#f4cc89` on `#2e281e`, danger `#fba5a9`, selected row `#1d3548`, hover border `#53718d`, cyan hover `#92e6f0`, cyan foreground `#092532`, quiet divider `#223046`, warning border `#756041`. Roof facet and roofline colours retain the existing diagram palette; they are data encodings, separate from control colours.

Manual outline replacement reuses the existing correction panel, native
checkboxes and numeric controls. Two inline steps separate fragment selection
from tracing. Selected sources stay cyan; the reviewer trace uses the editor's
white line/vertex encoding with a faint fill. All actions remain inline and
wrap at narrow widths. Draft protection and cancellation follow the existing
interior-gap workflow.

## App shell

`src/components/shell/WorkbenchNav.tsx` and `WorkbenchHeader.tsx` own the workspace
navigation; `src/components/shell/shell.css` owns every `.workbench-*` rule. One 52 px nav
row lists the five workspaces in fixed order: Map, Scene review, Review queues, Processing,
Training (roof review, model candidates and training coverage share the Training
workspace). The current workspace carries `aria-current="page"` and a 2 px cyan inset rule.
The dark tone consumes the `--review-*` tokens from `.review-shell`; the light tone scopes
the same token names to the map sidebar's established palette (#202124 text, #5f6368 muted,
#0e7490 accent). Geometry and type are identical in both tones. The header has no title
row: the object of work (address, scene, suburb, plan) is the page's only h1. Pages with
drafts pass their existing navigation guard through `onNavigate`; a `hrefs.map` override
returns to the captured location. The map canvas stays full-bleed; the nav lives inside the
sidebar header. Verification: `node scripts/test-workbench-nav.mjs`.

## Scene review deck

The scene route extends the existing navy/cyan inspection workspace. The
signature interaction is the aerial review card: an object stays registered to
its real image while a directional stamp previews a swipe decision. This is a
review-specific variant, with explicit buttons and keyboard equivalents. A left
swipe requests an edit; a right swipe confirms only the visible outline/class.
Keep the surrounding layer list and measurement inspector quiet. Reuse
`review-*` CSS tokens, native selects and inline save/error feedback. Geist is
the interface face; tabular/monospace measurements support the object inspector.
The 3D preview is a separate view of the same selection and visibility state.
Scene lighting uses a Z-up sky fill and an oblique key light so adjacent roof
slopes remain distinguishable. Selection tint stays subdued; it must not flatten
the shading used to assess ridges and end faces. Lighting is a view aid only.
Map capture reuses the same header, form controls and status treatment in a
single readable column. Geographic coordinates and width remain visible before
capture. The scene deck keeps swipe decisions separate from outline taps, and
new-object drawing starts with the entire image visible. Header actions wrap on
mobile without introducing horizontal page scrolling.
When an extraction changes, the recovery notice uses the existing inline notice
and button styles. Preview focuses a carried correction in the aerial comparison;
keeping edits creates an ordinary saved revision. Changed-source warnings remain
inline with access to a fresh review and retained history.
Roof proposal controls use a native disclosure within the object inspector.
Opening roof-point editing brings the enlarged roof into view, with a quiet
**Whole image** toggle for context. Marker sizes stay consistent between views.
The proposal source and unresolved image evidence remain visible beside its
controls; generating a shape never adopts approval styling.
Tree structure uses the same native disclosure, select and metric-input controls
in the inspector. 3D object focus sits beside existing rotation and zoom actions,
with pressed state and a **Whole scene** label for the return action.
**Isolate object** / **Show surroundings** uses the same toolbar and pressed state
to remove visual occlusion while inspecting an object, without changing its data.
Vehicle shape extends that disclosure with native body-style selection, decimal
heading/proportions and explicit front/axis buttons. A white aerial direction
arrow supplements the numeric bearing. Fit notices reuse the existing inline
correction treatment; estimated vehicle styles never use approval styling.
Vehicle, road/surface and tilted solar direction arrows end in a small white
and class-colour handle. Its transparent 40-pixel hit area includes the centre,
and keyboard focus uses the existing cyan accent. Dragging previews direction;
the hint distinguishes turning the handle from swiping the rest of the card.
No extra toolbar row is added. Prediction arrows retain their read-only display.
In the 3D view, the selected object's height uses a white dashed measurement
line and a cyan-ringed vertical handle with a 44-pixel hit area. The adjacent
label distinguishes thickness from height. Dragging previews the mesh with a
steady camera; releasing commits one edit. The numeric inspector remains the
precise alternative. Prediction and partial-footprint previews omit the handle.
Split/merge extends the same registered image editor. Cyan marks selected source
outlines, amber marks other candidates, and a dashed white line previews the cut.
Keep native checkboxes, numeric coordinates and a bounded choice list accessible.
Replacement provenance appears as an inline notice with links to the new objects.

Groups use the same registered imagery beside a bounded membership list.
Class colours identify outlines; stronger opacity and checked controls identify
members. Prediction comparison retains amber dashed outlines. Create/rename,
cross-layer selection and removal use existing native fields and review buttons;
all changes remain ordinary scene drafts. The layout stacks at smaller desktop
widths and on mobile. Group 3D reuses the existing scene camera toolbar, with
**Focus group** and **Isolate group** naming the scope of each action.

The scene training view uses a compact class-by-split matrix followed by image
rows from the actual captures. Matrix cells link to the corresponding review
work; image thumbnails keep coverage numbers grounded in their source. Numeric
columns use tabular figures and retain separate image/mask counts. On small
screens only the table scrolls horizontally; image actions and native filters
wrap into the existing single-column review layout.

The model-check panel sits with the selected class/split review list. It reuses
the review surface and compact table language, with explicit denominators,
neutral score text and a dash for undefined values. Method and per-image
provenance stay in native disclosures. Avoid green accuracy badges on seed
datasets; a high score with one image is still a small sample.

The scene review card leads the laptop viewport: compact address and filter
controls above it, image progress in the adjacent inspector, and the cross-image
queue directly below the card. At 1280 × 720 the source image and both swipe
buttons should be visible together. Aerial and 3D heights scale with the viewport;
long forms and mobile layouts keep natural document scrolling. The existing
palette, typography and registered swipe-card treatment remain the visual owners.
A quiet **Only review layer** checkbox hides unrelated outlines and meshes during
a class review; turning it off restores the chosen layer visibility.
On phones, **Jump to review** moves directly from setup controls to the same card;
it keeps the document scrollable and leaves the current object and draft intact.

Focused aerial views fit the selected object's bounds to the card's actual
aspect ratio. Long road surfaces should use the landscape width instead of
appearing in a small central square. Keep the existing card height so imagery
and swipe decisions remain visible together. Comparison layers share one frame;
outline editing holds that frame steady. Whole-scene view retains the complete
capture, and the existing zoom/focus buttons own navigation without another
toolbar or a new visual treatment.

Building roof observations live in the existing inspector under **Roof training
feedback**. Use a native roof-family select and one checkbox for image rooflines;
keep estimated heights visibly separate. Stale geometry uses the existing inline
notice. Training coverage shows two rows under Buildings using the established
split table, with saved-check details and exact-object links in a disclosure.
This adds no competing save or approval bar.

The 3D roof-point variant reuses the scene card's native select, metric input and
44-pixel vertical handle. A compact **3D edit handle** row appears above the mesh
for buildings with roof geometry; point choice reveals its numeric height beside
the selector. The row wraps on phones. The selected connected ridge uses the
same green as the aerial roof editor; a quiet line links the height handle to
its measurement. Viewport-edge handles stay reachable rather than disappearing
during an edit. Geometry feedback follows the existing inline notice treatment.

3D comparison extends the existing amber prediction encoding. Solid current
surfaces remain the primary geometry; amber X-ray edges and faint original
surfaces show the model estimate. A compact wrapping legend names both layers
and explains original-only or reviewer-added selections. It uses the existing
muted/amber colours and adds no save controls. The same treatment applies inside
group inspection. Comparison geometry is visual context, never an edit target.

Roof shape learning extends the Buildings coverage section using the existing
split table, native run selector and disclosure controls. Readiness and metrics
use neutral text with visible roof/end denominators. A fitted experimental
candidate does not receive an approval colour. Excluded training/validation
examples link to the ordinary object editor. Provenance and local fitting
instructions sit in a disclosure; the main flow remains visual review.

The main map includes a compact **Analysed scene layers** panel with a saved/model
source switch, class filter and explicit refresh. House-level enrolment belongs
in the selected address card. Scene review retains its full editing workspace;
its Map link returns to the captured location, and Re-analyse scene makes the
feedback loop visible. Object colours match the existing scene class legend.

## Focused roof line workspace

Roof-line editing follows `docs/design/roof-editing-workspace-v1.png`: a single
registered canvas beside a contextual inspector, with compact tools above and
one save bar below. The wireframe is a layout reference; generated roof imagery
is never used as data. `RoofLineEditor` owns line visibility, comparison,
selection and notes; `SceneReviewPage` owns scene selection and saving. The
existing `--review-*` variables in reviews.css remain the runtime token source;
scenes.css consumes them without creating a competing panel palette. Roof line
colours remain the existing data encodings. The scene header groups secondary
actions in a native disclosure, and the scene rail can reveal empty classes.

The inspector has a bounded scroller only on desktop. On narrow screens it
stacks below the canvas with natural document scrolling. The save bar stays
reachable at the viewport bottom within the editor; it never conceals the last
form field. Roof type counts, line numbers and lengths use tabular figures.
