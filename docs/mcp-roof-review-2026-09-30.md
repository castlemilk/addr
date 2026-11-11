# Roof review through the MCP extension — 30 September 2026

The real MCP stdio server was exercised using the official `ClientSession`:
discovery, composer mention search, scene resource reads, the protected roof
queue, native source/overlay cards, immutable observations and observation reads.
The chat had no connected addr tool namespace, so these calls used the SDK client
against the local server. Native app UI checks used the synthetic protocol host;
the actual CMS was separately checked against a real reviewed roof.

Three low-evidence roofs were visually inspected in both the registered card
and full original RGB. The queue contained 26 eligible captures after excluding
saved reviews, protected references and held-out/mixed image frames. This is a
small diagnostic sample, not an accuracy estimate or a model comparison.

| Source scene / object | Visible state | Useful next step |
| --- | --- | --- |
| `mooroolbark-50011498-17-118446-80409` / `cc22cc433bafe31a` | Small blue-grey roof; detected lines mainly follow the perimeter. The internal model line and right-hand hip/valley cluster are hard to support in original RGB. | Compare simple surface hypotheses, but keep family, shadowed edge and heights uncertain; inspect another date before changing the junctions. |
| `mooroolbark-50011498-17-118441-80416` / `a932e3e3eb4e2790` | Bright upper/lower sections surround a darker rectangle with equipment-like features. Modelled diagonal hips and a connecting ridge are unclear in RGB. | Separate material/level breaks and equipment edges from physical roof creases; compare flat, shed and pitched hypotheses. |
| `mooroolbark-50011498-17-118440-80414` / `103bd23746abddfc` | Long residential block with repeated diagonal roof patterns and solar arrays. Broad model divisions miss several visible interior patterns. | Compare a multi-wing fit and trace supported ridge/valley runs. Blanket complexity reduction would discard evidence here. |

Capture titles give image context, not individual address assignments. Cached
geometric diagnostics were bound to older models in all three cases; the model
and image identities of the review cards themselves were verified. These stale
rows cannot establish current geometry quality. Solar/equipment lines also make
a line-count or mismatch score insufficient for deciding roof form.

![Small roof: RGB, model and detected segments](../data/reviews/eyes/roof-review/29afc000b6a383de262b6e7bd5be01f33eaf6a40482a0b0858209509dd57b10d/card.png)

![White roof: RGB, model and detected segments](../data/reviews/eyes/roof-review/b373cf6e4f13f54ef8db80ce4646182f78e8cbe209ec2c45bb284f87e4806571/card.png)

![Repeated roof wings: RGB, model and detected segments](../data/reviews/eyes/roof-review/ae290d20a810e6862cbc26de4a5c9040898a69b209af28a47f240f679232a50f/card.png)

Each case has an immutable tier-3 observation separating visible evidence,
hypothesis, ambiguity and next action. They remain outside human reviews and
training exports. Full source hashes, review IDs, observation IDs and verified
unchanged input hashes are retained in
`data/reviews/eyes/mcp-roof-review-2026-09-30.json`.

## Workflow improvements implemented

- The native extension now offers a roof queue, building selector, native card,
  cached diagnostic binding states, saved feedback revision, agent notes and an
  explicit observation form. Its source-editor link enables editing.
- Roof tools return direct structured content and output schemas with a JSON
  fallback and native image blocks. Expected conflicts are actionable MCP tool
  errors instead of generic crash messages.
- The CMS shows the same source-bound agent notes in Object details through a
  scoped read API, with refresh and safe error states. A source change excludes
  old notes; payload tampering is refused.
  The reader avoids loading the vision stack; its final HTTP check returned in
  0.21 seconds after an intermittent cold-start timeout was found and fixed.
- `update_draft_roof_feedback` and `PATCH /api/scenes/roof-feedback` provide bounded
  notes, issues and XY roof-line edits. Saved revision, model/image hashes and
  current outline are required. The transaction checks actual image bytes,
  preserves history, baselines, provenance, pending imports and unrelated
  objects, and reopens geometric edits as unchecked drafts.
- The old `apply_roof_feedback` file writer was replaced by a canonical cohort
  API client. `cohort_roof_feedback` exposes write prerequisites. Unknown actions,
  stale bindings, corrupt responses and approval statuses are refused.

These changes improve review and editing access. They do not demonstrate an
improvement in generated roof geometry. A modelling iteration should compare
the same frozen roofs plus counterexamples, with independent outline, junction,
coverage and shared-height checks before accepting any candidate.

## Verification

- 84 focused Python MCP/roof tests passed; full Python Ruff and changed-file
  ESLint passed; TypeScript passed.
- Actual MCP draft-save integration and HTTP PATCH tests passed on temporary
  `qa_` scenes, removed afterward. They checked history, source bindings, stale
  revisions, concurrent saves, unchecked states and preserved imports/objects.
- Native app Browser checks passed selection, cards, notes, context/deep links,
  source conflicts, response races, text fallback and mobile layout. The real
  CMS displayed the recorded note with no browser warnings/errors.
- `task test:reviews`, Go tests and `task verify:ui` passed. UI smoke verification
  requested 21 tiles and opened the studio.
- The full suite had 1,818 Python passes and one skipped test; its only failure
  was the existing real-data eyes snapshot timing limit (7.13 s versus 5 s).
  The isolated rerun also exceeded the limit (8.99 s). Its code was not changed.
- Full repository lint still reports the existing 55 errors and 4,163 warnings,
  including bundled Python-environment JavaScript. Changed code is clean.
- Protected-data guards passed before and after. Reviewed model/image hashes
  remain unchanged; no production geometry, human reviews or labels were edited.

Tool contracts and setup are documented in [mcp-extensions.md](mcp-extensions.md).
Reconnect the MCP client to load the updated tool discovery and bundled app.
