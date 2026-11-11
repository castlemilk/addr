# Writing address-scene feedback

`addr-scene` preserves an immutable imported image/model/review frame and stores
Fieldwork feedback as sparse per-object overrides. Use `GetInfo` (MCP
`get_info`) and inspect the adapter's `geometryTypes`, `supportsCrop`, and
`writeHints` before writing. The same validation applies to Connect, REST, MCP,
and the CLI.

Read the current item first. `SaveAnnotation` replaces one complete annotation;
`SaveItem` replaces the complete item. Preserve fields you are not changing and
send the current **parent item revision** as `expectedRevision`. Use the returned
canonical item/revision for the next write. On a revision conflict, reload and
reapply the intended edit; do not overwrite a newer revision blindly.

| Intent | Field or operation |
| --- | --- |
| Object notes | `annotation.properties.addr.overrides.notes` (string; `""` explicitly clears a source or previous note) |
| Scene-level notes | `item.notes` |
| Effective object classification | `properties.addr.overrides.kind` |
| Object display name | `properties.addr.overrides.label` |
| Object review state | `properties.addr.overrides.status` |
| Remove an object from the active view | Set the override status to `rejected`; do not delete the annotation |
| Geometry | `annotation.geometry`, a normalized `Polygon` including holes |
| Geometry review flag | `properties.addr.overrides.geometry_reviewed` (boolean) |
| Visibility | `properties.addr.overrides.visibility` |

Nonempty top-level `annotation.notes` is rejected because it is not the scene
object's note field. An empty top-level value does not clear object notes; use
`overrides.notes: ""`. Omitting the notes override restores the immutable source
note. Existing `annotation.labelId` is an immutable **source-class label**; it
does not determine an object's edited classification. Source binding summaries
such as `properties.addr.status` are also immutable; write the corresponding
field under `overrides`.

For example, after `GetItem`, modify the returned annotation in memory:

```js
const annotation = structuredClone(item.annotations.find(a => a.id === objectId));
annotation.properties.addr.overrides = {
  ...annotation.properties.addr.overrides,
  notes: "Recheck the west eave against the original image",
};
// POST /api/v1/rpc/SaveAnnotation, or the equivalent Connect/MCP operation:
const request = { itemId: item.id, expectedRevision: item.revision, annotation };
```

Send `request` as the JSON body to the RPC endpoint. For MCP `save_annotation`,
use those same fields as the tool arguments. To clear the note, repeat the
read-modify-write with `notes: ""`. To reject the object, keep its annotation and
set `overrides.status: "rejected"`. Rejected and superseded objects remain in
the preserved data and can be inspected with the UI's **Rejected / superseded**
filter. `DeleteAnnotation` and removing an existing annotation from `SaveItem`
are rejected for this adapter.

Supported classes are `building`, `tree`, `road`, `car`, `solar_panel`, `skylight`,
`chimney`, `pool`, `driveway`, `footpath`, `vegetation`, `water`, and `other`.
Object statuses are `candidate`, `confirmed`, `needs_adjustment`, `rejected`, and
`superseded`. Visibility is `full`, `occluded`, `truncated`, or
`occluded_truncated`. The generic item status is a separate work queue state.

## Geometry and roof state

`annotation.id` must equal `properties.addr.objectId`. Annotation geometry uses
image coordinates in `[0, 1]`: X divided by immutable image width and Y divided
by immutable image height. Each polygon ring is closed; the first is the outer
ring and the rest are holes. `annotation.geometry` is authoritative, including
when legacy pixel-ring overrides are present. Do not write `overrides.ring` or
`overrides.holes` to change the outline. Points, lines, and crop are unsupported
for `addr-scene`; use the generic image/map adapters for those workflows.

Changing polygon geometry or effective classification clears `geometry_reviewed`
and `model.height_verified`, removes derived roof parts/lines, and clears the
derived roof annotation. The server records removal markers so baseline roofs
cannot reappear when the item is reconstructed. The object becomes
`needs_adjustment`, unless its new status is `rejected` or `superseded`. Any
subsequent approval is a separate revision after checking the saved outline.
Equivalent JSON number spelling or object-key order does not invalidate roofs.

Existing outline-bound roof feedback is retained with its original binding;
it is never rebound to a changed outline. A change away from building also
removes that building-only feedback from the effective object while retaining
the immutable source. Source model/review/history assets are never rewritten.

New objects need explicit matching IDs and complete `overrides` containing
`kind`, `label`, `source`, `status`, `geometry_reviewed`, and `model`. The model
requires `height_m`, `base_m`, `height_source`, `height_verified`, `width_m`,
`length_m`, `area_m2`, `rotation_deg`, and `color`; use the Fieldwork UI to create
an initial object when these scene semantics are unfamiliar. Its geometry still
comes from the normalized polygon. The original model and review remain
independently inspectable.

Fieldwork feedback does not promote source predictions or machine decisions to
human-reviewed training data. Preserve source provenance and use the original
application's explicit review/export rules for training eligibility.
