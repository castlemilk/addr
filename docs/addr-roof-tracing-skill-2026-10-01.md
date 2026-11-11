# Reusable roof tracing skill

The user's feedback was that the first polygon improvements still left edges
that did not tightly fit the roof. The missed decision was keeping unchanged
model vertices without checking the entire perimeter against original pixels.

The new [addr-roof-tracing skill](../.opencode/skills/addr-roof-tracing/SKILL.md)
is maintained in this checkout. Its Codex installation at
`~/.codex/skills/addr-roof-tracing` links to that directory, so later improvements
use the same source. Invoke it with `$addr-roof-tracing`. AGENTS.md and the
addr-3d skill also point roof tracing work to this workflow.

The workflow binds the current saved object and original image/model, checks
every edge and corner, distinguishes roof omissions from background spill,
previews a candidate, repeats the perimeter audit, and reloads the exact saved
draft. Named unresolved segments stay partial; agent edits do not become human
checks or training labels. Case-specific coordinates stay in evidence data.

## Inspection support

`inspect_roof_boundary` returns clean original RGB, a source-pixel grid and
numbered vertices/edges with exact crop transforms. Its optional `preview_id`
inspects the proposed polygon before applying it, using nearest-neighbour
display and clearly labelled unchecked proposal geometry. The saved outline
remains in `binding`; `outline_role` distinguishes current and proposed.
Altered, foreign or stale previews are rejected.

The skill includes an official SDK fallback helper that retains structured
receipts and native MCP images. Its `--out-dir` changes client artifact storage;
server diagnostic sidecars remain under `data/reviews/eyes/`. It uses canonical
MCP/API saves and never writes review files directly.

## Worked improvements

- Small roof `d8a5bbb6f6aaaf81`: removed the northern yard apex and restored
  omitted northwest roof. A second complete original-pixel review fit four
  sustained main-roof eaves instead of keeping tiny unsupported intermediate
  bends. The adjoining tan area along the lower west remains unresolved as
  ground/apron versus an attached cover, explicitly recorded in the saved notes.
- White roof `a932e3e3eb4e2790`: excluded the farther-right wall/balcony strip
  and followed the supported lower roof setback. An unresolved one-pixel upper
  zigzag was simplified to an approximate junction. East roof junctions and
  west foliage contact remain explicit partial segments.

Both final objects are saved revision 3, unchecked machine tier 3 drafts.
The original source PNG and model bytes match their before hashes. The other
126/111 objects retain geometry and review fields, excluding recalculated model
dimension/area/rotation metrics. Protected capture/frozen-run guards pass.

Evidence and exact before/after rings:
[case report](../data/reviews/eyes/roof-tracing-2026-10-01/report.json),
[small comparison](../data/reviews/eyes/roof-tracing-2026-10-01/small-comparison.png),
[white comparison](../data/reviews/eyes/roof-tracing-2026-10-01/white-comparison.png).
Per-edge audits, MCP previews/inspections, save/reload receipts, CMS screenshots
and the independent test are in the same case directory.

## Validation and improvement loop

An independent read-only forward test reproduced the missed north edge and
identified adjoining-area uncertainty. It also exposed the missing proposed
pixel-grid/index view, which is now supported by MCP. Its stale revision was
recognized and its candidate was kept offline. Read-only test output was
preserved separately from the applied drafts.

82 focused Python tests pass, including current/proposed inspection and
stale/tampered/foreign preview rejection. `task lint` passes with 29 existing
ESLint warnings; targeted Ruff and skill metadata validation pass. Actual MCP
discovery, native images, guarded saves and exact API reloads were exercised.
The CMS displays saved revision 3 for the examples. No UI implementation changed.

For the next critique, retain the raw evidence and the failed edge decision,
update a narrow reusable rule/tool, and verify it independently. A visually
tighter main roof must not conceal unresolved appendages or occluded corners.
