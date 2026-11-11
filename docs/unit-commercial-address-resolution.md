# Unit and commercial building address resolution

The app now has two stages: a property-based automatic resolver, and an audited
reviewer decision when authoritative geometry cannot identify an individual
unit or tenancy. Roof geometry and address associations remain separate.

## Automatic evidence

`building_addresses.py` retains structured Vicmap unit types/numbers, floors,
building/complex names and full site addresses in its downloaded snapshot.
`property_address_resolution.py` applies these rules:

- Prefer an individual unit property over an enclosing site only when the
  structured base street address, locality and postcode match; the parent
  contains at least 99.5% of the child and is materially larger. The unit must
  cover at least 90% of the roof, with at most a 10% competing overlap. Keep
  the enclosing site address as context.
- A shared roof can be associated with multiple separately addressed properties
  when their intersections cover at least 90% of the roof and overlap by at most
  1%. Retain each address and its roof-overlap fraction. These are cadastral
  allocations, not new roof surfaces, party-wall measurements or occupancy proof.
- Small neighboring boundary fringes do not turn an existing single-property
  match into a shared roof. Small common-site fringes are retained as site
  context instead of being advertised as extra units.
- Overlapping unit titles, vertical/floor addresses, different street identities
  and insufficient coverage stay unresolved for individual tenancy purposes.
  An additional 34 otherwise promising unit/shared associations remain in review
  because the capture cuts through the roof; visible fragments cannot establish
  the complete building assignment.
- Commercial SHOP address records remain distinct. A shopping building can have
  several addressed premises; the app never assigns the whole building to the
  nearest shop. Generic UNIT records do not establish residential use.
- Air conditioners, exhausts, repeated bays and imagery can support review of
  physical form, but they cannot assign legal unit numbers. Vicmap urban address
  points are generally offset from road frontages, so point proximity is not used
  to resolve these cases.

## Review and feedback

Open **Resolve units / site** in self-review, the inventory/map inspector or the
scene's Property address section. Select official candidate addresses, choose
individual unit, shared roof or site, and record the supporting plan, entrance
evidence or direct local knowledge. Save the association, defer pending better
evidence, or clear a previous decision. No inference run creates a human decision.

`GET /api/scenes/address-resolution?scene=SCENE&object=OBJECT` exposes current
evidence, binding, revision, latest decision, stale state and history count.
`POST /api/scenes/address-resolution` accepts:

```json
{
  "scene_id": "SCENE",
  "object_id": "OBJECT",
  "binding": "VALUE_FROM_GET",
  "revision": 0,
  "action": "confirm",
  "scope": "unit",
  "address_keys": ["PROPERTY_PFI/ADDRESS_PFI"],
  "source_kind": "title_or_site_plan",
  "note": "Specific evidence supporting the association."
}
```

Other actions: `defer`, `clear`. Other scopes: `shared_roof`, `site`. Other
source kinds: `verified_entrance`, `local_knowledge`. Saving requires the existing
`crm.address.write` permission and same-origin validation. The server supplies
the reviewer identity. Unknown candidate IDs, multiple selections for one unit,
unsupported evidence types, stale revisions and changed source bindings fail.

Decisions live separately in `data/address-evidence/decisions/`, with process
locking, atomic writes, monotonic revisions and retained history. They bind to
the roof geometry, model signature, official dataset snapshot and resolver
version. Source changes reopen the decision; saved roof lines, model outputs
and training approvals are never overwritten. This conservative policy can also
request rechecking after a refresh elsewhere in the address snapshot. Decisions
are scoped to the selected scene/object, not silently transferred to similar
or overlapping detections. Existing numeric CRM links are edited through the
scene editor, not replaced with Vicmap provider IDs.

Inventory associations are evaluated against model geometry. A decision on a
different edited outline can therefore be stale in inventory while valid in
the scene editor. This prevents reviewed addresses being silently transferred
onto changed roof footprints.

## Current result and verification

The 25 September 2026 pass adds 141 specific-unit matches and 187 shared-roof
associations, reducing ambiguous records from 2,313 to 1,985. The 10,339-roof
inventory now has 8,161 automatic single-property/unit matches, one saved CRM
link, 187 shared roofs, 1,985 ambiguous cases and five unmatched cases. These
are evidence classifications, not a measured address-accuracy percentage.

Real regression fixtures include Unit 3/208 Hull Road, five shops at
191–195 Hull Road, and the shared site at 118 Hayrick Lane. Tests cover parent
containment, wrong-street conflicts, shared surfaces, stacked units, boundary
fringes, commercial premises, saved decisions, stale geometry/data, authorization,
revision conflicts and clear/defer history. Frozen suburb and Cambridge reference
model/review hashes remain unchanged.

Sources: [Vicmap Address technical detail](https://www.land.vic.gov.au/maps-and-spatial/spatial-data/vicmap-catalogue/vicmap-address)
and [Vicmap Property Address](https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/arcgis/rest/services/Vicmap_Property_Address/FeatureServer/0).
