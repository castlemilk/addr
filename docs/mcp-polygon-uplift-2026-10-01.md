# Polygon draft uplift through MCP — 1 October 2026

Three objects were inspected using actual local MCP SDK calls, registered review
cards and original RGB. Two conservative boundary corrections were previewed,
visually inspected and saved through `apply_roof_polygon` and the canonical
scene API. These are unchecked agent drafts, not measured accuracy, human
approvals, roof-family decisions, height checks or training labels.

## Applied edits

**White roof — `mooroolbark-50011498-17-118441-80416` /
`a932e3e3eb4e2790`.** Three southern vertices moved onto the visible white-roof
to dark-wall/shadow transition. The former outline extended below the roof into
the dark band. Northern and side vertices were retained. The southeast fascia
and exact eave placement remain uncertain at the original resolution.

![Original RGB, previous outline in cyan, saved draft polygon in amber](../data/reviews/eyes/polygon-preview/145a8a0445866e99763b9d5428be7e2fa505f34abe9ce175ccf253d4ac29535a/card.png)

[Open the white roof comparison](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118441-80416&object=a932e3e3eb4e2790&kind=building&status=all&layer=overlay).

**Smaller roof — `mooroolbark-50011498-17-118442-80415` /
`d8a5bbb6f6aaaf81`.** The southern boundary was tightened to exclude the dark
wall/shadow fringe. The middle-right bulge moved inward onto the visible light
roof/fascia edge. Northern vertices were retained because the stepped/occluded
edge is ambiguous. No roof family or relative height was inferred.

![Original RGB, previous outline in cyan, saved draft polygon in amber](../data/reviews/eyes/polygon-preview/73a3827db36a8faebe1e022e71278b465ba486effa31798b5bb6f537b7361456/card.png)

[Open the smaller roof comparison](http://localhost:3008/scenes?scene=mooroolbark-50011498-17-118442-80415&object=d8a5bbb6f6aaaf81&kind=building&status=all&layer=overlay).

The third object (`mooroolbark-50011498-17-118441-80415` /
`c49cce79e93e7ad3`) was deferred: its complex roof-line structure needs stronger
evidence, and there was no equally clear conservative boundary correction.
The earlier small blue roof and long residential block were also left unchanged.

## Editing tools and safeguards

- `preview_roof_polygon` accepts source-pixel outer rings and holes, checks
  polygon validity, and returns a native original/current/proposed card with
  exact crop transforms, source identities, saved revision and uncertainty.
- `apply_roof_polygon(preview_id)` verifies the immutable preview and current
  source/revision, then saves one unchecked draft through the canonical API.
- Old roof parts, model lines and roof annotations tied to the changed outline
  are cleared from the draft. The immutable source prediction remains available
  in the CMS's Prediction/Compare views; unrelated object outlines and review
  fields are preserved. The canonical saver normalizes footprint dimensions and
  rotation metadata on all objects, as it does for ordinary scene saves.
- Server-owned machine provenance keeps agent polygons and XY roof-line edits
  at tier 3. Actual reference-registry and calibration checks confirm neither
  applied polygon is counted as a human reference or calibration decision.
- A fingerprint binds subsequent automatic edits to the last saved agent draft.
  Intervening CMS edits invalidate that binding. Checked, imported and protected
  reviewer work is refused, as are stale revisions/source hashes and malformed
  polygons. History and baselines are preserved.

The white roof was initially saved at revision 1 before the fingerprint safeguard
was added. Its exact save receipt and absence of intervening edits/imports were
verified, then only its authorship fingerprint was added through a canonical
revision-checked save at revision 2. The smaller roof is at revision 1.

## Verification

- 55 focused Python MCP/polygon checks passed after the final changes.
- Pure patch checks, both live API fixture tests, actual MCP draft integration,
  TypeScript and the review test suite passed. Temporary QA fixtures were removed.
- `task lint` passed with 29 warnings; Python Ruff passed.
- `task verify:ui` passed: 21 tile requests and the studio opened.
- The full `task test` run had 1,951 Python passes and one skip; six unrelated
  tests failed with the host's temporary `Too many open files in system` error.
  All six passed on targeted rerun. Go tests passed; TypeScript and review checks
  were run separately because that full task stopped at the Python failures.
- Real CMS reads show the saved drafts and notes. Original model/image hashes
  and the before/after protected-data guard passed.

Full coordinates, source identities, preview/save receipts, authority checks
and unchanged-source evidence are in
[`report.json`](../data/reviews/eyes/polygon-uplift-2026-10-01/report.json).
Tool contracts are documented in [mcp-extensions.md](mcp-extensions.md).
