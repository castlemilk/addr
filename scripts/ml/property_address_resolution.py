"""Property hierarchy and shared-roof associations, never physical roof splits.

Urban address points can be street-front offsets. Unit order, roof fixtures,
building colour and the nearest point cannot establish tenancy boundaries.
"""
from shapely.geometry import Polygon, box
from shapely.ops import unary_union


def image_cut(scene, obj):
    if not scene.get('image_width') or not scene.get('image_height') or not obj.get('ring'):
        return False
    return Polygon(obj['ring']).distance(box(0, 0, scene['image_width'], scene['image_height']).boundary) <= 1


def visible_only(evidence, clipped):
    if not clipped or (evidence.get('scope') not in ('unit', 'shared_roof')):
        return evidence
    result = {**evidence, 'status': 'ambiguous', 'partial': True, 'scope': 'visible_fragment',
              'reason': 'This image cuts through the roof. The listed properties describe its visible fragment, not a complete building/unit assignment.',
              'next_action': 'Inspect a fuller capture or a site plan before confirming the whole roof association.'}
    result.pop('full_address', None)
    return result


def is_unit(address):
    return bool(address.get('blg_unit_type') or address.get('blg_unit_id_1') is not None)


def is_vertical(address):
    return bool(address.get('floor_type') or address.get('floor_no_1') is not None)


def base(address):
    values = [str(address.get(k) or '').upper().strip() for k in ('num_road_address', 'locality_name', 'postcode')]
    # Vicmap num_road_address includes the structured unit prefix (3/208 HULL ROAD).
    if is_unit(address) and '/' in values[0]:
        values[0] = values[0].split('/', 1)[1]
    return tuple(values)


def describe(candidates, status):
    commercial = any(str(c.get('blg_unit_type', '')).upper() in ('SHOP', 'OFFICE', 'FACTORY', 'WAREHOUSE', 'SHOWROOM') for c in candidates)
    vertical = any(is_vertical(c) for c in candidates)
    units = any(is_unit(c) for c in candidates)
    complex_site = any(c.get('complex_site') == 'Y' or c.get('complex_name') for c in candidates)
    kind = 'commercial_address' if commercial else 'unit_address' if units else 'named_site' if complex_site else 'property'
    scope = 'multi_level' if vertical else 'unit' if units and status == 'matched' else 'site'
    action = ('Check the strata/floor or tenancy plan; a roof footprint cannot locate stacked units.' if vertical else
              'Use the site/tenancy plan or a verified entrance address to identify the building. Record the evidence below.' if status == 'ambiguous' else
              'This resolves the property association. Roof edges and business occupancy remain separate checks.')
    bases = {base(c) for c in candidates}
    site = next(iter(bases)) if len(bases) == 1 else None
    return {'scope': scope, 'address_kind': kind, 'next_action': action,
            'site_address': ' '.join(site) if site and site[0] and site[1] else None,
            'site_names': sorted({c[k] for c in candidates for k in ('building_name', 'complex_name') if c.get(k)})}


def refine(roof, overlaps, records, geometries):
    """Ignore a parent site only when structured addresses and containment agree."""
    parents = set()
    for _, parent in overlaps:
        pa = records[parent]['candidates']
        if not pa or any(is_unit(c) or is_vertical(c) for c in pa):
            continue
        for _, child in overlaps:
            ca = records[child]['candidates']
            pg, cg = geometries[parent], geometries[child]
            if parent == child or not ca or not all(is_unit(c) and not is_vertical(c) for c in ca):
                continue
            # Metadata absent or a different street/site cannot suppress a competing parcel.
            if not all(base(c)[0] and base(c) in {base(p) for p in pa} for c in ca):
                continue
            if pg.area >= cg.area*1.25 and pg.intersection(cg).area/cg.area >= .995:
                parents.add(parent)
                break
    active = [(ratio, i) for ratio, i in overlaps if i not in parents]
    if not active:
        return None
    addresses = [c for _, i in active for c in records[i]['candidates']]
    context = [c for _, i in overlaps if i in parents for c in records[i]['candidates']]
    ratio, first = active[0]
    dominant = ratio >= .9 and (len(active) == 1 or active[1][0] <= .1)
    unique = records[first]['candidates']
    if parents and dominant and len({c['full_address'] for c in unique}) == 1 and not any(is_vertical(c) for c in unique):
        return {'status': 'matched', 'method': 'unit-property', 'full_address': unique[0]['full_address'],
                'property_pfi': records[first]['property_pfi'], 'overlap': round(ratio, 4), 'candidates': unique,
                'site_context': context, **describe(unique, 'matched'),
                'reason': 'A specific unit property contains this roof; its enclosing site address is retained as context.'}
    # Keep the established property match when the second overlap is only a
    # small fringe. A boundary offset/overhang is not a shared unit roof.
    if not parents and ratio >= .85 and (len(active) == 1 or active[1][0] <= .15):
        return None
    # Associate a continuous physical roof with several disjoint addressed lots.
    # Do not partition overlapping/stacked title polygons or create unobserved walls.
    if len(active) < 2 or any(len(records[i]['candidates']) != 1 for _, i in active) or any(is_vertical(c) for c in addresses):
        return None
    # A small common-site fringe should not be advertised as another unit.
    units = [(r, i) for r, i in active if is_unit(records[i]['candidates'][0])]
    if len(units) >= 2:
        unit_bases = {base(records[i]['candidates'][0]) for _, i in units}
        fringe = [(r, i) for r, i in active if (r, i) not in units and r <= .15
                  and base(records[i]['candidates'][0]) in unit_bases]
        context.extend(c for _, i in fringe for c in records[i]['candidates'])
        active = [(r, i) for r, i in active if (r, i) not in fringe]
        addresses = [c for _, i in active for c in records[i]['candidates']]
    pieces = [roof.intersection(geometries[i]) for _, i in active]
    total = unary_union(pieces)
    overlap = (sum(p.area for p in pieces)-total.area)/roof.area
    coverage = total.area/roof.area
    if overlap > .01 or coverage < .9:
        return None
    return {'status': 'shared', 'method': 'property-allocation', 'scope': 'shared_roof',
            'candidates': addresses, 'site_context': context,
            'coverage': round(coverage, 4), 'overlap': round(overlap, 4),
            'allocations': [{'property_pfi': records[i]['property_pfi'], 'address_pfi': records[i]['candidates'][0]['address_pfi'],
                             'roof_fraction': round(ratio, 4)} for ratio, i in active],
            **{k: v for k, v in describe(addresses, 'shared').items() if k != 'scope'},
            'reason': 'This roof spans separate addressed properties. Associations are resolved as a shared roof; cadastral divisions are not roof folds or verified party walls.',
            'next_action': 'Review the unit/property allocation. Keep the physical roof continuous and use a plan to verify any tenancy or party-wall split.'}
