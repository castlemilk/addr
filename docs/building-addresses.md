# Building property addresses

For the subsequent unit/commercial resolver and saved decision workflow, see
[Unit and commercial address resolution](unit-commercial-address-resolution.md).

The review queue, roof inventory, coverage-map inspector and scene address
controls now resolve roof addresses against the official **Vicmap Property
Address** joined property/address layer. The old local address-point catalog
has coverage gaps; neither capture titles nor nearest pins establish a roof's
address.

`scripts/ml/building_addresses.py` downloads a complete paginated snapshot into
`data/address-evidence/vicmap-property-address.json`. Refresh it explicitly with:

```sh
scripts/ml/.venv/bin/python -m scripts.ml.building_addresses --refresh-plan data/suburbs/mooroolbark/runs/500114985968d40b/plan.json
```

The query includes a small buffer around the suburb. Full property geometries
are retained, with active surface/ground records only. Count changes, empty
pages, duplicate records and malformed geometry fail the refresh before the
previous snapshot is replaced. The source, fetch time and content signature
remain attached to evidence. This is a downloaded snapshot, not a live
guarantee that government records have not changed.

Matching uses a spatial index and the registered roof footprint. One property
must cover at least 85% of the roof, with no second property covering more than
15%, and only one distinct address for an automatic **property address match**.
This is a site address: multiple detached buildings on a school, business or
residential property may legitimately share it. It does not identify individual
tenancies or prove ownership. Several unit addresses or intersecting properties
stay ambiguous, with all candidates available. Missing coverage stays unresolved;
no nearest-address fallback is used. Existing saved local address links win.

Vicmap PFIs are provider identities, never local CRM address IDs. Resolving an
address does not rewrite predictions, reviewed outlines, training labels or roof
confidence. Matching is recalculated from current geometry on each reader call,
and the in-process source index is invalidated when its snapshot changes. The
scene endpoint's existing one-minute response cache also keys on model and review
revision. Self-review reports are enriched when read, without changing their
frozen inputs. Inventory addresses refer to the model footprint; an edited
outline is checked independently in scene review.

## Verified 25 September 2026

- Building `cc22cc433bafe31a` maps to **118 HAYRICK LANE MOOROOLBARK 3138**,
  property PFI `1339330`, with complete roof/property overlap. The official
  record and original registered roof are retained in a regression fixture.
- 24,952 source records downloaded across Mooroolbark and its border buffer.
- 10,339 inventory roofs: 8,020 property matches, 1 existing saved link,
  2,313 ambiguous cases and 5 unresolved cases. These count roofs, not houses
  or unique postal addresses.
- Address search and the address-status filter work in inventory and map views.
  Self-review details expose source provenance and multiple candidates.
- 257 frozen suburb model hashes and four Cambridge Road model/review hashes
  are unchanged. See `data/address-evidence/verification-2026-09-25.json`.
- 40 targeted Python regressions, frontend typecheck, targeted ESLint/Ruff,
  review regression scripts, saved-address API tests and production build passed.

Source: [Vicmap Property Address](https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/arcgis/rest/services/Vicmap_Property_Address/FeatureServer/0),
State of Victoria, CC BY 4.0. Address associations remain distinct from roof
boundary measurements and roof-shape evidence.
