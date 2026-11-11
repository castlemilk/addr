"""Dwelling units from Vicmap parcels: does one detected roof outline span several numbered lots, and where are the lot lines?

Reviewer 2026-10-12 (Corners queue 2 grouped townhouses / terraces / side-by-side houses): "get this properly fixed as the
property boundaries from vic maps etc would have shown them as separate". A roof is 'several' units when two or more numbered
lots each hold a real share of it; common property (CM), reserves (RES) and roads never count as a unit. Slivers over a lot
line (cadastre vs imagery registration, eaves) stay 'one'. Lot lines are cadastral, not measured party walls: a split piece
is a proposal for review, never a confirmed outline. Pure functions; parcels come from scene_property_roofs.parcel_input.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon, shape
from shapely.ops import transform

from .scene_addresses import WORLD, image_projection

VERSION = 'parcel-units-v2'  # v2 2026-10-13: units strictly inside their own lot (v1 shared leftover roof by nearest unit)
# A lot is a unit of this roof when it holds at least this share of the roof AND this much roof area. Terraces of ten
# units give each lot ~10 %, a 0.3-0.5 m registration sliver gives a neighbour lot 1-4 %.
MIN_SHARE, MIN_M2 = 0.06, 12.0
# ... or this much roof area on its own (a strata shop or townhouse under one big roof holds a small share but a whole unit)
UNIT_M2 = 40.0
# One lot holding this share settles 'one' even when other lots touch the roof.
ONE_SHARE = 0.85
# Strata (apartments, strata shops): unit lots that overlap each other by more than this share of the smaller lot are titles
# stacked over one building, not side-by-side units.
STRATA_OVERLAP = 0.2
NOT_A_UNIT = re.compile(r'^(CM|RES|ROAD|R\d|CP)', re.I)


def _inverse(scene):
    """Pixel -> (lon, lat) for the scene frame (inverse of scene_addresses.image_projection)."""
    lon0, lat0 = scene['centroid']['lon'], scene['centroid']['lat']
    scale = WORLD * math.cos(math.radians(lat0))
    y0 = math.asinh(math.tan(math.radians(lat0))) / (2 * math.pi)
    m = np.asarray(scene['meters_to_pixel'], dtype=float)
    a = np.array([[m[0][0], m[1][0]], [m[0][1], m[1][1]]])
    ai = np.linalg.inv(a)

    def unproject(px, py):
        mx, my = ai @ (np.array([px, py]) - m[2])
        lon = lon0 + mx / scale * 360
        lat = math.degrees(math.atan(math.sinh((my / scale + y0) * 2 * math.pi)))
        return lon, lat
    return unproject


def scene_bounds(scene):
    """[west, south, east, north] of the image corners; for scenes saved without region.bounds."""
    if scene.get('region', {}).get('bounds'):
        return scene['region']['bounds']
    inv = _inverse(scene)
    w, h = scene['image_width'], scene['image_height']
    pts = [inv(x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


def scene_parcels(scene, root=Path('.')):
    """Frozen Vicmap parcels for the scene (fetched once, cached in data/scene-parcels); derives bounds when missing."""
    from .scene_property_roofs import parcel_input
    if not scene.get('region', {}).get('bounds'):
        scene = {**scene, 'region': {**scene.get('region', {}), 'bounds': scene_bounds(scene)}}
    return parcel_input(root, scene)


def _area_m2(scene):
    return abs(float(np.linalg.det(np.asarray(scene['pixel_to_meters'], dtype=float)[:2])))


def alignment(scene):
    """The scene's lot-line alignment record (parcel_align), or None when it was never aligned."""
    from .parcel_align import load
    rec = load(scene.get('id', ''))
    return rec if rec and rec.get('image_sha256') == scene.get('image_sha256') else None


def held(scene):
    """True when the scene's lot lines still do not line up with its photo after alignment: lot-based work skips it."""
    rec = alignment(scene)
    return bool(rec) and rec.get('status') == 'held'


def _lots(scene, parcels, raw=False):
    from .parcel_align import aligned_projection
    project, _ = aligned_projection(scene) if scene.get('id') and not raw else (image_projection(scene), None)
    out = []
    for f in (parcels or {}).get('features', []):
        props = f.get('properties', {})
        if props.get('status', 'approved') != 'approved':
            continue
        geo = transform(project, shape(f['geometry']))
        if not geo.is_valid:
            geo = geo.buffer(0)
        if not geo.is_empty:
            out.append((props, geo))
    return out


def lot_shares(scene, ring, parcels, raw=False):
    """Every lot the roof touches: lot, plan, pfi, share of the roof, roof m2 inside the lot, unit (bool)."""
    roof = Polygon(ring)
    if not roof.is_valid:
        roof = roof.buffer(0)
    if roof.is_empty or roof.area <= 0:
        return []
    m2 = _area_m2(scene)
    rows = []
    for props, geo in _lots(scene, parcels, raw):
        part = geo.intersection(roof)
        if part.is_empty or part.area <= 0:
            continue
        lot = str(props.get('lot') or '')
        rows.append({'lot': lot, 'plan': props.get('plan'), 'pfi': props.get('pfi'), 'share': part.area / roof.area,
                     'm2': part.area * m2, 'unit': bool(lot) and not NOT_A_UNIT.match(lot), '_geo': geo})
    return sorted(rows, key=lambda r: -r['share'])


def _qualifies(r):
    # (UNIT_M2 is no longer a unit by itself: Haiku review 2026-10-13 - shop tenancies under one commercial roof are one building)
    return r['unit'] and r['m2'] >= MIN_M2 and r['share'] >= MIN_SHARE


def _several(rows):
    units = [r for r in rows if _qualifies(r)]
    return len(units) >= 2 and not _stacked(units) and not any(r['unit'] and r['share'] >= ONE_SHARE for r in rows)


def _stacked(units):
    for i, a in enumerate(units):
        for b in units[i + 1:]:
            small = min(a['_geo'].area, b['_geo'].area)
            if small > 0 and a['_geo'].intersection(b['_geo']).area / small > STRATA_OVERLAP:
                return True
    return False


def unit_verdict(scene, ring, parcels):
    """{'units': 'one' | 'several' | 'unknown', 'lots': [...qualifying unit lots...], 'version'}."""
    if held(scene):
        return {'units': 'unknown', 'lots': [], 'held': True, 'version': VERSION}
    rows = lot_shares(scene, ring, parcels) if parcels else []
    clean = lambda r: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k != '_geo'}
    if not rows:
        return {'units': 'unknown', 'lots': [], 'version': VERSION}
    units = [r for r in rows if _qualifies(r)]
    if _stacked(units):
        return {'units': 'one', 'strata': True, 'lots': [clean(r) for r in units[:1]], 'version': VERSION}
    if len(units) >= 2 and not any(r['unit'] and r['share'] >= ONE_SHARE for r in rows):
        # Several only when the published Vicmap lines agree (reviewer 2026-10-13, Units 003): a fitted shift of 1-2 m moves a
        # wall-line lot boundary into the neighbouring house and invents a unit that neither the title nor the photo supports.
        if alignment(scene) and not _several(lot_shares(scene, ring, parcels, raw=True)):
            return {'units': 'one', 'lots': [clean(r) for r in units[:1]], 'raw_disagrees': True, 'version': VERSION}
        return {'units': 'several', 'lots': [clean(r) for r in sorted(units, key=lambda r: str(r['lot']))], 'version': VERSION}
    if units or any(r['unit'] for r in rows):
        return {'units': 'one', 'lots': [clean(r) for r in units[:1]], 'version': VERSION}
    return {'units': 'unknown', 'lots': [], 'version': VERSION}


def split_by_lots(scene, ring, parcels):
    """For a 'several' roof: one piece per unit lot (roof ∩ lot). Roof area outside every unit lot (common property,
    a neighbour's lot) belongs to no unit and is dropped (outside_unit_lots_m2). [] when the roof is not 'several'."""
    if unit_verdict(scene, ring, parcels)['units'] != 'several':
        return []
    roof = Polygon(ring)
    if not roof.is_valid:
        roof = roof.buffer(0)
    rows = [r for r in lot_shares(scene, ring, parcels) if _qualifies(r)]
    pieces = [[r, r['_geo'].intersection(roof)] for r in rows]
    covered = pieces[0][1]
    for _, g in pieces[1:]:
        covered = covered.union(g)
    # Units are bounded by their own legal lot, always (reviewer 2026-10-13: "it needs to always be bounded based off the actual
    # legal property boundaries"). Roof outside every unit lot (common property, a neighbour's lot) belongs to no unit: dropped and
    # reported on each piece, never shared out.
    outside_m2 = round(roof.difference(covered).area * _area_m2(scene), 1)
    out = []
    for r, g in pieces:
        g = g.intersection(roof).intersection(r['_geo'])  # inside its roof and inside its own lot
        g = max(getattr(g, 'geoms', [g]), key=lambda p: p.area) if g.geom_type != 'Polygon' else g
        g = g.buffer(0)
        if g.geom_type != 'Polygon':
            g = max(g.geoms, key=lambda p: p.area)
        out.append({'lot': r['lot'], 'plan': r['plan'], 'pfi': r['pfi'], 'ring': [list(p) for p in g.exterior.coords[:-1]],
                    'm2': round(g.area * _area_m2(scene), 1), 'outside_unit_lots_m2': outside_m2, 'source': VERSION})
    return sorted(out, key=lambda p: str(p['lot']))


def bound_to_lot(scene, ring, parcels, pfi=None, pfis=None, protect=None):
    """The outline clipped to its legal parcel: the parcel named by pfi, else the parcel holding most of it - together with the
    common property (CM / CP) of the same plan that it touches (a strata site: shops or garages on common property, eaves over a
    shared driveway), never another owner's numbered lot. Returns (ring, info) with info = {'pfi', 'lot', 'plan', 'clipped_m2'};
    (ring, None) when no parcel touches it or the scene's lot lines are held. The largest piece is kept if the clip leaves several."""
    rows = lot_shares(scene, ring, parcels) if parcels and not held(scene) else []
    if not rows:
        return ring, None
    pick = next((r for r in rows if r['pfi'] == pfi), None) if pfi else None
    pick = pick or rows[0]  # rows are sorted by share: the parcel holding most of the outline, of any kind
    # pfis: one building on several titles (no party wall along the lot lines, 2026-10-17): bounded by all of its own lots
    common = re.compile(r'^(CM|CP)', re.I)
    area = pick['_geo']
    for r in rows:
        if pfis and r['pfi'] in pfis:
            area = area.union(r['_geo'])
    for r in rows:
        if r is not pick and r['plan'] and r['plan'] == pick['plan'] and common.match(str(r['lot'])):
            area = area.union(r['_geo'])
    roof0 = Polygon(ring).buffer(0)
    for r in rows:  # a same-plan lot lying mostly under this roof is a tenancy of this building, not a neighbour's property
        if r is not pick and r['plan'] and r['plan'] == pick['plan'] and r['_geo'].area > 0 \
                and r['_geo'].intersection(roof0).area / r['_geo'].area >= 0.5:
            area = area.union(r['_geo'])
    if alignment(scene):  # remove only roof that is outside the lot under both the published and the aligned lines
        for r in lot_shares(scene, ring, parcels, raw=True):
            if r['pfi'] == pick['pfi'] or (r['plan'] and r['plan'] == pick['plan'] and common.match(str(r['lot']))):
                area = area.union(r['_geo'])
    roof = Polygon(ring)
    if not roof.is_valid:
        roof = roof.buffer(0)
    if protect is not None and not protect.is_empty:
        # layers card 2026-10-17: the photo comes first - the lot may only remove roof area the photo layer does not support
        # (photo_roof), and never area on another owner's numbered lot that is NOT this roof's own (that stays clipped below)
        area = area.union(protect.intersection(roof))
    g = roof.intersection(area)
    if g.is_empty or g.area <= 0:
        return ring, None
    if g.geom_type != 'Polygon':
        g = max((p for p in getattr(g, 'geoms', []) if p.geom_type == 'Polygon'), key=lambda p: p.area)
    out = [list(p) for p in g.exterior.coords[:-1]]
    return out, {'pfi': pick['pfi'], 'lot': pick['lot'], 'plan': pick['plan'],
                 'clipped_m2': round((roof.area - g.area) * _area_m2(scene), 2)}
