"""Roof -> address allocation over the suburb inventory (agent hints, not labels).

Every inventory roof (``scene_processing.inventory``) gets one allocation row with a
G-NAF ``address_id`` where the evidence supports one, applying rules in order of
strength:

1. saved link (``obj.address_id``)                                  -> strong
2. Vicmap property match with one local G-NAF address               -> strong
3. containment: a lot/property holding >= 0.85 of the roof with
   exactly one address (G-NAF point inside, or its numbered Vicmap
   address); parcel lots first, Vicmap property polygons otherwise   -> moderate
4. secondary structures inherit the largest primary roof allocated
   on the same lot/property                                          -> one step below the primary
5. unit sites: per-lot candidates from unit_split_candidates, or the
   unit lot holding the roof centroid with one address point         -> moderate 'suggested'
6. one address point inside the roof with no parcel evidence         -> weak 'suggested'
otherwise ambiguous / shared / unmatched with the candidates listed.

Shared common-property lots are never decided by distance; a roof the property
resolver called ``shared`` stays shared unless a saved link exists. Rows are written to
the eyes index table ``address_allocation`` (this version's rows replaced) and a report
under ``data/reviews/eyes/address-allocation/``. Nothing under data/scenes is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from shapely import make_valid
from shapely.errors import GEOSException
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from . import eyes_index
from .building_addresses import load_index
from .scene_addresses import image_projection
from .scene_processing import inventory as live_inventory

VERSION = 'address-allocation-v1'
TABLE = 'address_allocation'
REPORT_DIR = Path('data/reviews/eyes/address-allocation')
NOTE = 'agent hints and model estimates, not labels or accuracy'
CONTAINMENT = .85
OVERLAP_MIN = .05
CENTROID_SHARE = .5
CONFIDENCES = ('strong', 'moderate', 'weak')
STATUSES = ('matched', 'suggested', 'ambiguous', 'shared', 'unmatched')
SECONDARY_KINDS = frozenset({'garage', 'shed', 'outbuilding', 'fragment', 'carport', 'pergola-or-shade',
                             'shipping-container', 'water-tank'})
HINT_VERSIONS = {'flags': ('building-flags-v2', 'building-flags-v1'), 'structure': ('structure-kinds-v3', 'structure-kinds-v2', 'structure-kinds-v1')}
COMMON_LOT = re.compile(r'^(CM|CP)', re.I)
NUMBERED = re.compile(r'^(?:(?P<unit>[\w-]+)/)?(?P<number>\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)\s+(?P<street>.+?)$')
REVIEW_URL = '/scenes?scene={scene_id}&object={object_id}&kind=building&status=all&layer=model'


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def step_down(confidence):
    index = CONFIDENCES.index(confidence) if confidence in CONFIDENCES else len(CONFIDENCES)-1
    return CONFIDENCES[min(index+1, len(CONFIDENCES)-1)]


def review_url(scene_id, object_id):
    return REVIEW_URL.format(scene_id=scene_id, object_id=object_id)


# --- G-NAF ------------------------------------------------------------------------------------------------

def address_key(unit, number, street, locality=None):
    """(unit number or None, street number, street, locality) normalised for Vicmap <-> G-NAF matching."""
    unit = str(unit or '').strip().upper()
    if unit:
        unit = unit.split()[-1]
    number = str(number or '').strip().upper()
    street = re.sub(r'\s+', ' ', str(street or '').strip().upper())
    locality = re.sub(r'\s+', ' ', str(locality or '').strip().upper()) or None
    if not number or not street:
        return None
    return (unit or None, number, street, locality)


def vicmap_key(candidate):
    """Key of a Vicmap candidate from its structured road address; None when it has no street number."""
    text = str(candidate.get('num_road_address') or '').strip().upper()
    locality = str(candidate.get('locality_name') or '').strip().upper()
    if not text:
        full = str(candidate.get('full_address') or '').upper()
        text = full.split(' '+locality)[0] if locality and ' '+locality in full else ''
    match = NUMBERED.match(text)
    if not match:
        return None
    unit = candidate.get('blg_unit_id_1')
    if unit is None:
        unit = match.group('unit')
    elif candidate.get('blg_unit_id_2') is not None:
        unit = f"{unit}-{candidate['blg_unit_id_2']}"
    return address_key(unit, match.group('number'), match.group('street'), locality)


class Gnaf:
    """Local G-NAF points (data/australia_geo.db) inside a bounded extent, keyed for name lookups."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.by_id = {r['address_id']: r for r in self.rows}
        self.by_key = defaultdict(list)
        for row in self.rows:
            row['point'] = Point(row['lon'], row['lat'])
            row['key'] = address_key(row.get('unit'), row.get('number'), row.get('street'), row.get('suburb'))
            if row['key']:
                self.by_key[row['key']].append(row)
        self.tree = STRtree([r['point'] for r in self.rows]) if self.rows else None

    @classmethod
    def load(cls, root, bounds):
        w, s, e, n = bounds
        with sqlite3.connect(f'file:{Path(root)/"data/australia_geo.db"}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('''SELECT a.id AS address_id, a.full_address, a.unit, a.number, a.street, a.suburb, a.lon, a.lat
                FROM addresses a JOIN addresses_rtree r ON r.id=a.id
                WHERE r.minX<=? AND r.maxX>=? AND r.minY<=? AND r.maxY>=? LIMIT 20001''', (e, w, n, s)).fetchall()
        if len(rows) > 20000:
            raise ValueError('Address coverage exceeds the bounded lookup')
        return cls(rows)

    def inside(self, geometry):
        if self.tree is None or geometry.is_empty:
            return []
        return [self.rows[int(i)] for i in self.tree.query(geometry, predicate='contains')]

    def by_name(self, candidate):
        key = vicmap_key(candidate)
        return list(self.by_key.get(key, [])) if key else []

    def entry(self, row):
        return {'address_id': row['address_id'], 'full_address': row['full_address']}


# --- Lots: Vicmap parcels (cached), CRM boundaries, Vicmap property polygons -------------------------------

def _valid(geometry):
    geometry = make_valid(geometry) if not geometry.is_valid else geometry
    parts = [g for g in getattr(geometry, 'geoms', [geometry]) if g.geom_type in ('Polygon', 'MultiPolygon')]
    if not parts:
        return None
    result = unary_union(parts)
    return result if not result.is_empty and result.area > 0 else None


def parcel_caches(root):
    """Frozen Vicmap Parcel payloads: units cache (per scene) and scene-parcels (per image key)."""
    root = Path(root)
    found = []
    for path in sorted((root/'data/reviews/eyes/units').glob('parcels-*.json')):
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        scene_id = saved.get('scene_id') or path.stem.removeprefix('parcels-')
        if saved.get('data'):
            found.append((scene_id, saved['data']))
    for path in sorted((root/'data/scene-parcels').glob('*.json')):
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if saved.get('data'):
            found.append((None, saved['data']))
    return found


def crm_boundaries(root):
    """CRM 'boundary' features (user-entered cadastral outlines) with their address_id."""
    path = Path(root)/'data/australia_geo.db'
    if not path.exists():
        return []
    rows = []
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            for feature_id, address_id, geometry, properties in db.execute(
                    "SELECT id, address_id, geometry_json, properties FROM crm_features WHERE kind='boundary'"):
                try:
                    props = json.loads(properties or '{}')
                    rows.append({'id': feature_id, 'address_id': address_id, 'geometry': shape(json.loads(geometry)), 'props': props})
                except (ValueError, TypeError, AttributeError):
                    continue
    except sqlite3.Error:
        return []
    return rows


class Lots:
    """Polygons that can hold a roof: parcel lots (when frozen), CRM boundaries and Vicmap property polygons.

    Each entry: {source, pfi, plan, lot, property_pfi, candidates (Vicmap addresses), address_id (CRM), geometry, scene_ids}.
    """

    def __init__(self, entries):
        self.entries = [e for e in entries if e.get('geometry') is not None]
        self.tree = STRtree([e['geometry'] for e in self.entries]) if self.entries else None
        self.parcel_scenes = {sid for e in self.entries if e['source'] == 'vicmap-parcel' for sid in e.get('scene_ids', ())}
        self.has_parcels = any(e['source'] == 'vicmap-parcel' for e in self.entries)

    @classmethod
    def load(cls, root, property_index=None):
        entries, seen = [], {}
        for scene_id, data in parcel_caches(root):
            for feature in data.get('features', []):
                props = feature.get('properties') or {}
                if props.get('status') not in (None, 'approved'):
                    continue
                pfi = str(props.get('pfi') or '')
                if pfi in seen:
                    if scene_id:
                        seen[pfi]['scene_ids'].add(scene_id)
                    continue
                try:
                    geometry = _valid(shape(feature['geometry']))
                except (ValueError, TypeError, KeyError):
                    continue
                entry = {'source': 'vicmap-parcel', 'pfi': pfi, 'plan': str(props.get('plan') or '') or None,
                         'lot': str(props.get('lot') or '') or None, 'property_pfi': None, 'candidates': [],
                         'address_id': None, 'geometry': geometry, 'scene_ids': {scene_id} if scene_id else set()}
                seen[pfi] = entry
                entries.append(entry)
        for row in crm_boundaries(root):
            geometry = _valid(row['geometry'])
            entries.append({'source': 'crm-boundary', 'pfi': f"crm-{row['id']}", 'plan': row['props'].get('plan'),
                            'lot': row['props'].get('lot'), 'property_pfi': None, 'candidates': [],
                            'address_id': row['address_id'], 'geometry': geometry, 'scene_ids': set()})
        if property_index is not None:
            for record, geometry in zip(property_index.records, property_index.geometries, strict=True):
                entries.append({'source': 'vicmap-property', 'pfi': record['property_pfi'], 'plan': None, 'lot': None,
                                'property_pfi': record['property_pfi'], 'candidates': record['candidates'],
                                'address_id': None, 'geometry': geometry, 'scene_ids': set()})
        return cls(entries)

    def overlapping(self, roof, minimum=OVERLAP_MIN):
        """[(entry, share of roof area)] sorted by share, share > minimum."""
        if self.tree is None or roof.is_empty or roof.area <= 0:
            return []
        found = []
        for index in self.tree.query(roof, predicate='intersects'):
            entry = self.entries[int(index)]
            try:
                share = roof.intersection(entry['geometry']).area/roof.area
            except (GEOSException, ValueError):  # topology errors on odd rings must not stop the run
                continue
            if share > minimum:
                found.append((entry, float(share)))
        return sorted(found, key=lambda item: (-item[1], item[0]['source'], item[0]['pfi']))


def lot_key(source, pfi):
    """One key per polygon identity so primaries and secondaries meet on the same property/lot."""
    return ('property', str(pfi)) if source == 'vicmap-property' else (source, str(pfi))


def is_common_lot(entry):
    return bool(entry.get('lot') and COMMON_LOT.match(str(entry['lot'])))


def lot_addresses(entry, gnaf):
    """Addresses a lot vouches for, as (G-NAF rows, source) with source 'gnaf-point' | 'crm-boundary' | 'vicmap-name' | None.

    Parcel lots vouch through exactly one G-NAF point inside. CRM boundaries vouch for their linked
    address. Numbered Vicmap properties vouch through one point inside (on the same street) or, with
    no point inside, through their unique numbered address. Unnumbered sites (road reserves, parent
    sites of units) and common-property lots vouch for nothing; several points inside vouch for nothing.
    """
    if is_common_lot(entry):
        return [], None
    if entry['source'] == 'crm-boundary':
        row = gnaf.by_id.get(entry['address_id'])
        return ([row], 'crm-boundary') if row else ([], None)
    inside = gnaf.inside(entry['geometry'])
    if entry['source'] == 'vicmap-parcel':
        return ([inside[0]], 'gnaf-point') if len(inside) == 1 else ([], None)
    numbered = [c for c in entry.get('candidates', []) if vicmap_key(c)]
    if not numbered:
        return [], None
    keys = {vicmap_key(c) for c in numbered}
    named = {r['address_id']: r for c in numbered for r in gnaf.by_name(c)}
    own = [p for p in inside if p['address_id'] in named]
    if len(own) == 1:
        return own, 'gnaf-point'  # the property's own address point, whatever other site points fall inside
    if len(own) > 1:
        return [], None
    if len(inside) == 1 and not named:
        point = inside[0]
        streets = {k[2] for k in keys}
        return ([point], 'gnaf-point') if point['key'] is not None and point['key'][2] in streets else ([], None)
    if len(keys) == 1 and len(named) == 1:
        return list(named.values()), 'vicmap-name'
    return [], None


# --- Hints (eyes index, read-only) --------------------------------------------------------------------------

def load_hints(root):
    """{(scene_id, object_id): {class_hint, flags, sub_kind, sub_confidence, hint_version, structure_version}}."""
    hints = {}
    if not eyes_index.path(root).exists():
        return hints
    with closing(eyes_index.connection(root, readonly=True)) as db:
        db.execute('PRAGMA busy_timeout=2000')
        for versions, kind in ((HINT_VERSIONS['flags'], 'flags'), (HINT_VERSIONS['structure'], 'structure')):
            for version in reversed(versions):  # older first so the newest version overwrites
                cursor = db.execute(
                    "SELECT scene_id, object_id, json_extract(payload, '$.class_hints.class_hint'), json_extract(payload, '$.class_hints.flags'), "
                    "json_extract(payload, '$.sub_kind'), json_extract(payload, '$.confidence') FROM object_quality WHERE version=?", (version,))
                for sid, oid, class_hint, flags, sub_kind, confidence in cursor:
                    entry = hints.setdefault((sid, oid), {'class_hint': None, 'flags': [], 'sub_kind': None, 'sub_confidence': None,
                                                          'hint_version': None, 'structure_version': None})
                    if kind == 'flags':
                        names = []
                        try:
                            names = [f.get('flag') for f in json.loads(flags or '[]') if isinstance(f, dict)]
                        except ValueError:
                            pass
                        entry.update(class_hint=class_hint, flags=names, hint_version=version)
                    else:
                        entry.update(sub_kind=sub_kind, sub_confidence=confidence, structure_version=version)
    return hints


def is_secondary(hint):
    hint = hint or {}
    return hint.get('sub_kind') in SECONDARY_KINDS or 'secondary-structure' in (hint.get('flags') or []) or hint.get('class_hint') == 'outbuilding'


def unit_split_reports(root):
    """{(scene_id, object_id): report} for unit_split_candidates outputs with per-lot candidates."""
    reports = {}
    folder = Path(root)/'data/reviews/eyes/units'
    if not folder.exists():
        return reports
    for path in folder.glob('*/*.json'):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if report.get('status') == 'split' and report.get('scene_id') and report.get('object_id'):
            reports[(report['scene_id'], report['object_id'])] = report
    return reports


# --- Scenes ---------------------------------------------------------------------------------------------------

class Scenes:
    """latest.json per scene, loaded once and never written."""

    def __init__(self, root):
        self.root = Path(root)
        self.cache = {}

    def get(self, scene_id):
        if scene_id not in self.cache:
            path = self.root/'data/scenes'/scene_id/'latest.json'
            try:
                self.cache[scene_id] = json.loads(path.read_text())
            except (OSError, ValueError):
                self.cache[scene_id] = None
        return self.cache[scene_id]

    def obj(self, scene_id, object_id):
        scene = self.get(scene_id)
        if not scene:
            return None
        return next((o for o in scene.get('objects', []) if o.get('id') == object_id), None)


# --- Allocation -------------------------------------------------------------------------------------------------

def _entry(row):
    return {'address_id': row['address_id'], 'full_address': row['full_address']}


def _vicmap_alternatives(before, gnaf):
    """Vicmap candidates as {address_id|null, full_address}, G-NAF id where the name maps uniquely."""
    seen, out = set(), []
    for candidate in before.get('candidates') or []:
        named = gnaf.by_name(candidate)
        address_id = named[0]['address_id'] if len(named) == 1 else None
        full = named[0]['full_address'] if len(named) == 1 else candidate.get('full_address')
        key = (address_id, full)
        if full and key not in seen:
            seen.add(key)
            out.append({'address_id': address_id, 'full_address': full})
    return out


def _dominant_property(before):
    pfi = before.get('property_pfi')
    if pfi:
        return str(pfi)
    pfis = {str(c.get('property_pfi')) for c in before.get('candidates') or [] if c.get('property_pfi')}
    return next(iter(pfis)) if len(pfis) == 1 else None


def _lot_block(entry):
    if entry is None or entry['source'] not in ('vicmap-parcel', 'crm-boundary') or not (entry.get('plan') or entry.get('lot')):
        return None
    return {'plan': entry.get('plan'), 'lot': entry.get('lot')}


def _base_row(feature, before, hint):
    props = feature['properties']
    return {'scene_id': props['scene_id'], 'object_id': props['object_id'], 'version': VERSION, 'note': NOTE,
            'address_id': None, 'full_address': None, 'property_pfi': _dominant_property(before), 'lot': None,
            'status': None, 'method': None, 'confidence': None, 'reason': None, 'alternatives': [],
            'area_m2': props.get('area_m2'), 'band': props.get('band'), 'review_url': props.get('review_url') or review_url(props['scene_id'], props['object_id']),
            'evidence': {'before': {k: before.get(k) for k in ('status', 'method', 'full_address', 'address_kind', 'scope', 'overlap')},
                         'before_candidates': [c.get('full_address') for c in before.get('candidates') or []],
                         'hints': {k: hint.get(k) for k in ('class_hint', 'flags', 'sub_kind', 'sub_confidence')} if hint else None,
                         'secondary': is_secondary(hint), 'clipped': bool(props.get('clipped')),
                         'hidden_overlaps': len(props.get('hidden_overlaps') or []), 'lots': [], 'parcel_evidence': None,
                         'points_inside_roof': None, 'gnaf_lookup': None, 'rule': None}}


def _decide(row, *, status, method, confidence, reason, address=None, full_address=None, rule):
    row.update(status=status, method=method, confidence=confidence, reason=reason)
    row['evidence']['rule'] = rule
    if address is not None:
        row['address_id'], row['full_address'] = address['address_id'], address['full_address']
    elif full_address is not None:
        row['full_address'] = full_address
    row['alternatives'] = [a for a in row['alternatives'] if a.get('address_id') != row['address_id'] or row['address_id'] is None]
    return row


class Allocator:
    def __init__(self, gnaf, lots, hints, scenes=None, splits=None, use_saved_links=True):
        self.gnaf, self.lots, self.hints, self.scenes = gnaf, lots, hints, scenes
        self.splits = splits or {}
        self.use_saved_links = use_saved_links

    # rules 1-3 -------------------------------------------------------------------------------------------------
    def primary_pass(self, feature, allow_saved=True):
        props = feature['properties']
        before = props.get('property_address') or {}
        hint = self.hints.get((props['scene_id'], props['object_id']))
        row = _base_row(feature, before, hint)
        row['alternatives'] = _vicmap_alternatives(before, self.gnaf)
        try:
            roof = _valid(shape(feature['geometry']))
        except (ValueError, TypeError, KeyError):
            roof = None
        row['evidence']['roof_valid'] = roof is not None
        # Rule 1: saved link.
        saved_id = before.get('address_id') if before.get('status') == 'linked' else None
        if saved_id is None and self.scenes is not None:
            obj = self.scenes.obj(props['scene_id'], props['object_id'])
            saved_id = (obj or {}).get('address_id')
        if allow_saved and self.use_saved_links and saved_id is not None:
            address = self.gnaf.by_id.get(saved_id)
            if address:
                return roof, _decide(row, status='matched', method='saved-link', confidence='strong', address=_entry(address),
                                     reason='Address linked to this roof in the saved scene.', rule=1)
            row['evidence']['saved_link_missing'] = saved_id
        if before.get('status') == 'reviewed' and len(before.get('candidates') or []) == 1:
            named = self.gnaf.by_name(before['candidates'][0])
            if len(named) == 1:
                return roof, _decide(row, status='matched', method='reviewer-evidence', confidence='strong', address=_entry(named[0]),
                                     reason='Reviewer-confirmed address decision for this roof.', rule=1)
        # Lot evidence shared by rules 2-6.
        overlaps = self.lots.overlapping(roof) if roof is not None else []
        containing = [(e, s) for e, s in overlaps if s >= CONTAINMENT]
        row['evidence']['lots'] = [{'source': e['source'], 'pfi': e['pfi'], 'plan': e.get('plan'), 'lot': e.get('lot'),
                                    'share': round(s, 4), 'points_inside': len(self.gnaf.inside(e['geometry'])) if e['source'] != 'crm-boundary' else None}
                                   for e, s in overlaps[:8]]
        has_parcel = any(e['source'] == 'vicmap-parcel' for e, _ in overlaps)
        row['evidence']['parcel_evidence'] = 'vicmap-parcel' if has_parcel else 'no parcel evidence'
        parcel_lot = next((e for e, s in containing if e['source'] == 'vicmap-parcel'), None)
        row['lot'] = _lot_block(parcel_lot)
        # Rule 2: Vicmap property match with one local G-NAF record.
        if before.get('status') == 'matched' and before.get('full_address'):
            names = {vicmap_key(c) for c in before.get('candidates') or [] if vicmap_key(c)}
            named = {r['address_id']: r for c in before.get('candidates') or [] for r in self.gnaf.by_name(c)}
            row['evidence']['gnaf_lookup'] = 'unique' if len(named) == 1 and len(names) == 1 else ('none' if not named else 'multiple')
            if len(named) == 1 and len(names) == 1:
                return roof, _decide(row, status='matched', method=before.get('method') or 'vicmap-property', confidence='strong',
                                     address=_entry(next(iter(named.values()))),
                                     reason='Roof overlaps one addressed Vicmap property whose address is one local G-NAF record. Site identity, not a surveyed roof boundary.', rule=2)
        # Shared roofs keep the property resolver's decision (never re-cut by distance or centroid).
        if before.get('status') == 'shared':
            return roof, _decide(row, status='shared', method=before.get('method') or 'property-allocation', confidence='moderate',
                                 reason=before.get('reason') or 'This roof spans separate addressed properties.', rule=0)
        # Rule 3: containment.
        if roof is not None and containing:
            decided = self._containment(row, containing)
            if decided is not None:
                return roof, decided
        if before.get('status') == 'matched' and before.get('full_address'):
            return roof, _decide(row, status='matched', method=before.get('method') or 'vicmap-property', confidence='moderate',
                                 full_address=before['full_address'],
                                 reason='Roof overlaps one addressed Vicmap property, but its address has no unique record in the local G-NAF table (address_id unknown).', rule=2)
        return roof, row  # undecided: rules 4-6 in the second pass

    def _containment(self, row, containing):
        vouched, sources = {}, {}
        for entry, share in containing:
            addresses, source = lot_addresses(entry, self.gnaf)
            for address in addresses:
                vouched.setdefault(address['address_id'], (address, entry, share))
                sources[address['address_id']] = source
        if len(vouched) == 1:
            address, entry, share = next(iter(vouched.values()))
            source = sources[address['address_id']]
            method = 'parcel-containment' if entry['source'] == 'vicmap-parcel' else ('crm-boundary' if entry['source'] == 'crm-boundary' else 'property-containment')
            row['property_pfi'] = entry.get('property_pfi') or row['property_pfi']
            row['lot'] = row['lot'] or _lot_block(entry)
            row['evidence']['containment'] = {'source': entry['source'], 'pfi': entry['pfi'], 'share': round(share, 4), 'address_source': source}
            evidence = {'gnaf-point': 'one address point inside it', 'vicmap-name': 'one numbered Vicmap address', 'crm-boundary': 'a CRM-linked address'}[source]
            what = 'Vicmap parcel lot' if entry['source'] == 'vicmap-parcel' else ('CRM boundary' if entry['source'] == 'crm-boundary' else 'Vicmap property polygon')
            return _decide(row, status='matched', method=method, confidence='moderate', address=_entry(address),
                           reason=f'A {what} holds {share:.0%} of the roof and has {evidence}; other overlapping polygons vouch for no separate address.', rule=3)
        if len(vouched) > 1:
            row['alternatives'] = list({(a['address_id'], a['full_address']): _entry(a) for a, _, _ in vouched.values()}.values()) + [
                alt for alt in row['alternatives'] if alt['address_id'] not in vouched]
            row['evidence']['containment'] = {'conflict': sorted(vouched)}
        return None

    # rules 4-6 -------------------------------------------------------------------------------------------------
    def secondary_pass(self, undecided, allocated):
        """undecided: [(feature, roof, row)]; allocated: primary rows already holding an address."""
        primaries = defaultdict(list)  # property/lot key -> [(area, row)]
        for row in allocated:
            if row['address_id'] is None or row['evidence'].get('secondary'):
                continue
            keys = {('property', row['property_pfi'])} if row['property_pfi'] else set()
            keys |= {lot_key(lot['source'], lot['pfi']) for lot in row['evidence'].get('lots', []) if lot['share'] >= CONTAINMENT}
            for key in keys:
                primaries[key].append((row.get('area_m2') or 0, row))
        for feature, roof, row in undecided:
            before = feature['properties'].get('property_address') or {}
            if row['status'] is not None:
                continue
            if row['evidence'].get('secondary') and self._inherit(row, primaries):
                continue
            if self._unit_site(feature, roof, row, before):
                continue
            if self._address_point(roof, row):
                continue
            status = before.get('status') if before.get('status') in ('ambiguous', 'shared', 'unmatched') else 'ambiguous'
            if status == 'unmatched' and row['alternatives']:
                status = 'ambiguous'
            reason = before.get('reason') or 'No unique lot, property or address-point evidence for this roof.'
            if status == 'unmatched' and not row['alternatives']:
                reason = 'No addressed property, lot or address point overlaps this roof.' if row['evidence'].get('roof_valid') else 'Roof geometry is unusable for address evidence.'
            _decide(row, status=status, method=before.get('method') or 'unresolved', confidence='weak', reason=reason, rule=None)

    def _inherit(self, row, primaries):
        keys = {lot_key(lot['source'], lot['pfi']) for lot in row['evidence'].get('lots', [])}
        if row['property_pfi']:
            keys.add(('property', row['property_pfi']))
        candidates = {}
        for key in keys:
            for area, primary in primaries.get(key, []):
                if primary['scene_id'] == row['scene_id'] and primary['object_id'] == row['object_id']:
                    continue
                current = candidates.get(primary['address_id'])
                if current is None or area > current[0]:
                    candidates[primary['address_id']] = (area, primary)
        if len(candidates) != 1:
            if candidates:
                row['evidence']['inherit_conflict'] = sorted(candidates)
            return False
        area, primary = next(iter(candidates.values()))
        row['evidence']['inherited_from'] = {'scene_id': primary['scene_id'], 'object_id': primary['object_id'],
                                             'address_id': primary['address_id'], 'area_m2': area, 'confidence': primary['confidence']}
        row['property_pfi'] = row['property_pfi'] or primary['property_pfi']
        kind = (row['evidence'].get('hints') or {}).get('sub_kind') or 'secondary structure'
        _decide(row, status='matched', method='secondary-inherits', confidence=step_down(primary['confidence']),
                address={'address_id': primary['address_id'], 'full_address': primary['full_address']},
                reason=f'Secondary structure ({kind}) on the same property as the largest allocated primary roof ({area:.0f} m2); inherits its address one confidence step lower.', rule=4)
        return True

    def _unit_site(self, feature, roof, row, before):
        props = feature['properties']
        report = self.splits.get((props['scene_id'], props['object_id']))
        if report:
            per_lot = []
            by_pfi = {e['pfi']: e for e in self.lots.entries if e['source'] == 'vicmap-parcel'}
            for lot in report.get('lots', []):
                entry = by_pfi.get(str(lot.get('pfi') or ''))
                addresses, _ = lot_addresses(entry, self.gnaf) if entry else ([], None)
                per_lot.append({'lot': lot.get('lot'), 'plan': lot.get('plan'), 'area_m2': lot.get('area_m2'),
                                'addresses': [_entry(a) for a in addresses]})
            row['evidence']['unit_split'] = {'status': report['status'], 'lots': per_lot}
            row['alternatives'] = list({(a['address_id'], a['full_address']): a for lot in per_lot for a in lot['addresses']}.values()) or row['alternatives']
            _decide(row, status='shared', method='unit-split-candidates', confidence='moderate',
                    reason=f"Unit site: {len(per_lot)} per-lot candidate outlines from unit_split_candidates; the roof spans several lots and no single address applies.", rule=5)
            return True
        unit_site = before.get('address_kind') == 'unit_address' or any(c.get('blg_unit_id_1') is not None for c in before.get('candidates') or [])
        if not unit_site or roof is None or before.get('status') == 'shared':
            return False
        centroid = roof.centroid
        holders = [(e, s) for e, s in self.lots.overlapping(roof) if e['geometry'].covers(centroid) and not is_common_lot(e)
                   and (e['source'] == 'vicmap-parcel' or any(c.get('blg_unit_id_1') is not None for c in e.get('candidates', [])))]
        if len(holders) != 1 or holders[0][1] < CENTROID_SHARE:
            return False
        entry, share = holders[0]
        addresses, source = lot_addresses(entry, self.gnaf)
        if len(addresses) != 1:
            return False
        row['property_pfi'] = entry.get('property_pfi') or row['property_pfi']
        row['lot'] = row['lot'] or _lot_block(entry)
        row['evidence']['unit_lot'] = {'source': entry['source'], 'pfi': entry['pfi'], 'share': round(share, 4), 'address_source': source}
        _decide(row, status='suggested', method='unit-lot-centroid', confidence='moderate', address=_entry(addresses[0]),
                reason=f'Unit site: the roof centroid falls in one unit lot holding {share:.0%} of the roof with one address; the lot boundary is not a party wall. Confirm before saving.', rule=5)
        return True

    def _address_point(self, roof, row):
        if roof is None:
            return False
        inside = self.gnaf.inside(roof)
        row['evidence']['points_inside_roof'] = len(inside)
        # A common-property lot or a multi-address site holding the roof is parcel evidence against a single point.
        site = any(lot['share'] >= CONTAINMENT and ((lot['points_inside'] or 0) > 1 or (lot['lot'] and COMMON_LOT.match(str(lot['lot']))))
                   for lot in row['evidence'].get('lots', []))
        if len(inside) == 1 and not site:
            _decide(row, status='suggested', method='address-point', confidence='weak', address=_entry(inside[0]),
                    reason='One address point is inside this roof and no lot evidence decides it. Parcel ownership is unverified; confirm before saving.', rule=6)
            return True
        if site:
            row['evidence']['site_lot'] = True
        if len(inside) > 1:
            row['alternatives'] = list({(a['address_id'], a['full_address']): _entry(a) for a in inside}.values()) + [
                alt for alt in row['alternatives'] if alt['address_id'] not in {a['address_id'] for a in inside}]
        return False

    def allocate(self, features):
        stamp, code = datetime.now(UTC).isoformat(), code_sha256()
        pending, decided = [], []
        for feature in features:
            roof, row = self.primary_pass(feature)
            (decided if row['status'] is not None else pending).append((feature, roof, row))
        self.secondary_pass(pending, [row for _, _, row in decided])
        rows = [row for _, _, row in decided+pending]
        for row in rows:
            row.update(computed_at=stamp, code_sha256=code)
            if row['status'] == 'shared':
                row['address_id'] = None
                row['full_address'] = None
            assert row['status'] in STATUSES and row['confidence'] in CONFIDENCES, row
        order = {(f['properties']['scene_id'], f['properties']['object_id']): i for i, f in enumerate(features)}
        rows.sort(key=lambda r: order[(r['scene_id'], r['object_id'])])
        return rows


# --- Index writes ----------------------------------------------------------------------------------------------

def replace_version_rows(db, table, version, rows, scene_ids=None):
    """Replace this version's rows (all of them, or only the given scenes) and insert the new ones atomically."""
    if scene_ids is None:
        db.execute(f'DELETE FROM "{table}" WHERE version=?', (version,))
    else:
        for scene_id in dict.fromkeys(scene_ids):
            db.execute(f'DELETE FROM "{table}" WHERE scene_id=? AND version=?', (scene_id, version))
    return eyes_index.replace_rows(db, table, rows)  # commits deletes and inserts together


def index_rows(rows):
    return [{'scene_id': r['scene_id'], 'object_id': r['object_id'], 'version': VERSION, 'payload': r} for r in rows]


def write_index(root, rows):
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        return replace_version_rows(db, TABLE, VERSION, index_rows(rows))


def load_rows(root, version=VERSION):
    """Allocation payloads from the eyes index (read-only), or [] when absent."""
    if not eyes_index.path(root).exists():
        return []
    with closing(eyes_index.connection(root, readonly=True)) as db:
        return [r['payload'] for r in eyes_index.rows(db, TABLE, version=version) if isinstance(r.get('payload'), dict)]


# --- Agreement checks ------------------------------------------------------------------------------------------

def _iou(a, b):
    union = a.union(b).area
    return float(a.intersection(b).area/union) if union > 0 else 0.


def crm_roof_links(root, features, minimum_iou=.5):
    """{(scene_id, object_id): {crm_feature_id, address_id, iou}} for CRM roof features paired to inventory roofs by IoU."""
    path = Path(root)/'data/australia_geo.db'
    links = {}
    if not path.exists():
        return links
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        crm = [(fid, aid, shape(json.loads(g))) for fid, aid, g in db.execute("SELECT id, address_id, geometry_json FROM crm_features WHERE kind='roof'")]
    if not crm:
        return links
    geoms = [shape(f['geometry']) for f in features]
    tree = STRtree(geoms)
    for fid, aid, geometry in crm:
        best = None
        for index in tree.query(geometry, predicate='intersects'):
            iou = _iou(geometry, geoms[int(index)])
            if iou >= minimum_iou and (best is None or iou > best[0]):
                best = (iou, int(index))
        if best:
            props = features[best[1]]['properties']
            key = (props['scene_id'], props['object_id'])
            if key not in links or links[key]['iou'] < best[0]:
                links[key] = {'crm_feature_id': fid, 'address_id': aid, 'iou': round(best[0], 4)}
    return links


def reference_links(root):
    """{(scene_id, object_id): address_id} for T1/T2 references with an address, via the agreement table."""
    links = {}
    if not eyes_index.path(root).exists():
        return links
    try:
        with closing(eyes_index.connection(root, readonly=True)) as db:
            for sid, oid, address_id in db.execute(
                    'SELECT a.scene_id, a.object_id, r.address_id FROM agreement a JOIN "references" r ON r.ref_id=a.ref_id '
                    "WHERE r.tier IN ('T1','T2') AND r.address_id IS NOT NULL"):
                links[(sid, oid)] = address_id
    except sqlite3.Error:
        pass
    return links


def agreement(rule_based, truth, name):
    """Agreement of rule-based rows (no saved links) with an external address per roof; denominators explicit."""
    counts = Counter()
    details = []
    for key, expected in truth.items():
        row = rule_based.get(key)
        if row is None:
            counts['not_in_inventory'] += 1
            continue
        if row['address_id'] is None:
            counts['no_allocation'] += 1
            outcome = 'no_allocation'
        elif row['address_id'] == expected:
            counts['agree'] += 1
            outcome = 'agree'
        else:
            counts['disagree'] += 1
            outcome = 'disagree'
        details.append({'scene_id': key[0], 'object_id': key[1], 'expected_address_id': expected, 'allocated_address_id': row['address_id'],
                        'method': row['method'], 'status': row['status'], 'outcome': outcome, 'review_url': row['review_url']})
    denominator = counts['agree']+counts['disagree']+counts['no_allocation']
    return {'source': name, 'denominator': denominator, 'agree': counts['agree'], 'disagree': counts['disagree'],
            'no_allocation': counts['no_allocation'], 'not_in_inventory': counts['not_in_inventory'],
            'agreement_rate': round(counts['agree']/denominator, 4) if denominator else None,
            'note': 'agreement with a human-entered address, not accuracy; denominators are tiny', 'details': details}


# --- Report -----------------------------------------------------------------------------------------------------

def _counts(values):
    """Counter as a JSON object; None keys become 'null' so reports sort and diff cleanly."""
    return {('null' if k is None else str(k)): v for k, v in sorted(Counter(values).items(), key=lambda item: str(item[0]))}


def summarise(features, rows, checks, examples, sheet_path, elapsed):
    before = {f['properties']['key'] if 'key' in f['properties'] else f"{f['properties']['scene_id']}/{f['properties']['object_id']}":
              (f['properties'].get('property_address') or {}) for f in features}
    by_key = {f"{r['scene_id']}/{r['object_id']}": r for r in rows}
    transitions = Counter()
    for key, pa in before.items():
        after = by_key.get(key)
        transitions[f"{pa.get('status')}->{after['status'] if after else 'missing'}"] += 1
    return {
        'version': VERSION, 'note': NOTE, 'computed_at': datetime.now(UTC).isoformat(), 'code_sha256': code_sha256(),
        'roofs': len(rows), 'elapsed_seconds': round(elapsed, 1),
        'before': {'status': _counts(pa.get('status') for pa in before.values()),
                   'method': _counts(pa.get('method') for pa in before.values()),
                   'with_address_id': sum(1 for pa in before.values() if pa.get('address_id') is not None)},
        'after': {'status': _counts(r['status'] for r in rows), 'method': _counts(r['method'] for r in rows),
                  'confidence': _counts(r['confidence'] for r in rows),
                  'status_confidence': _counts(f"{r['status']}/{r['confidence']}" for r in rows),
                  'with_address_id': sum(1 for r in rows if r['address_id'] is not None),
                  'distinct_address_ids': len({r['address_id'] for r in rows if r['address_id'] is not None}),
                  'rule': _counts(r['evidence'].get('rule') for r in rows),
                  'parcel_evidence': _counts(r['evidence'].get('parcel_evidence') for r in rows),
                  'secondary_structures': sum(1 for r in rows if r['evidence'].get('secondary'))},
        'transitions': dict(sorted(transitions.items())),
        'agreement': checks,
        'examples_ambiguous_to_matched': examples,
        'contact_sheet': str(sheet_path) if sheet_path else None,
    }


def ambiguous_to_matched(features, rows, limit=24):
    before = {f"{f['properties']['scene_id']}/{f['properties']['object_id']}": (f['properties'].get('property_address') or {}) for f in features}
    out = []
    for row in rows:
        key = f"{row['scene_id']}/{row['object_id']}"
        if before[key].get('status') == 'ambiguous' and row['status'] == 'matched':
            out.append(row)
    # Deterministic spread over methods and scenes: sort by a hash of the key.
    out.sort(key=lambda r: hashlib.sha256(f"{r['scene_id']}/{r['object_id']}".encode()).hexdigest())
    return out[:limit]


def example_entry(row, before_map):
    pa = before_map.get(f"{row['scene_id']}/{row['object_id']}", {})
    return {'scene_id': row['scene_id'], 'object_id': row['object_id'], 'review_url': row['review_url'],
            'before': {'status': pa.get('status'), 'candidates': [c.get('full_address') for c in pa.get('candidates') or []]},
            'after': {'status': row['status'], 'method': row['method'], 'confidence': row['confidence'],
                      'address_id': row['address_id'], 'full_address': row['full_address'], 'reason': row['reason']},
            'area_m2': row['area_m2']}


def _mapper(project, box, scale):
    def to_tile(lon, lat):
        x, y = project(lon, lat)
        return ((x-box[0])*scale, (y-box[1])*scale)

    def px_to_tile(x, y):
        return ((x-box[0])*scale, (y-box[1])*scale)
    return to_tile, px_to_tile


def contact_sheet(root, rows, lots, gnaf, scenes, path, tile=256, columns=6):
    """Crops of the saved scene PNGs with roof (cyan), deciding lot (yellow) and address point (red), for spot checks."""
    from PIL import Image, ImageDraw
    root = Path(root)
    tiles = []
    for row in rows:
        scene = scenes.get(row['scene_id'])
        obj = scenes.obj(row['scene_id'], row['object_id']) if scene else None
        if not scene or not obj or not obj.get('ring'):
            continue
        image_path = root/'data/scenes'/row['scene_id']/f"{scene['image_sha256']}.png"
        if not image_path.exists():
            continue
        try:
            image = Image.open(image_path).convert('RGB')
        except OSError:
            continue
        project = image_projection(scene)
        ring = [(float(x), float(y)) for x, y in obj['ring']]
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        span = max(xs)-min(xs), max(ys)-min(ys)
        half = max(span)*.9+24
        cx, cy = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
        box = (int(cx-half), int(cy-half), int(cx+half), int(cy+half))
        crop = image.crop(box).resize((tile, tile))
        scale = tile/(2*half)
        to_tile, px_to_tile = _mapper(project, box, scale)
        draw = ImageDraw.Draw(crop)
        containment = row['evidence'].get('containment') or row['evidence'].get('unit_lot') or {}
        entry = next((e for e in lots.entries if e['pfi'] == containment.get('pfi') and e['source'] == containment.get('source')), None)
        if entry is not None:
            for poly in getattr(entry['geometry'], 'geoms', [entry['geometry']]):
                pts = [to_tile(x, y) for x, y in poly.exterior.coords]
                draw.line(pts, fill=(250, 204, 21), width=2)
        draw.polygon([px_to_tile(*p) for p in ring], outline=(34, 211, 238), width=2)
        address = gnaf.by_id.get(row['address_id']) if row['address_id'] is not None else None
        if address is not None:
            px, py = to_tile(address['lon'], address['lat'])
            if -8 <= px <= tile+8 and -8 <= py <= tile+8:
                draw.ellipse((px-4, py-4, px+4, py+4), fill=(239, 68, 68), outline=(255, 255, 255))
        label = f"{row['object_id'][:8]} {row['method']}/{row['confidence']}"
        draw.rectangle((0, tile-30, tile, tile), fill=(0, 0, 0))
        draw.text((3, tile-29), label[:40], fill=(255, 255, 255))
        draw.text((3, tile-15), (row['full_address'] or '')[:40], fill=(255, 255, 255))
        tiles.append(crop)
    if not tiles:
        return None
    rows_n = (len(tiles)+columns-1)//columns
    sheet = Image.new('RGB', (columns*tile, rows_n*tile), (20, 20, 20))
    for i, crop in enumerate(tiles):
        sheet.paste(crop, ((i % columns)*tile, (i//columns)*tile))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path


def run(root=Path('.'), report_dir=None, write=True, sheet=True, log=print):
    root = Path(root)
    report_dir = Path(report_dir) if report_dir else root/REPORT_DIR
    started = time.time()
    inv = live_inventory(root)
    features = inv['features']
    if not features:
        raise ValueError('The suburb inventory is empty; nothing to allocate.')
    bounds = inv['suburb']['bounds']
    pad = .003
    gnaf = Gnaf.load(root, (bounds[0]-pad, bounds[1]-pad, bounds[2]+pad, bounds[3]+pad))
    lots = Lots.load(root, load_index(root))
    hints = load_hints(root)
    scenes = Scenes(root)
    splits = unit_split_reports(root)
    log(f'inventory {len(features)} roofs, G-NAF {len(gnaf.rows)} points, lots {len(lots.entries)} '
        f'(parcel scenes {len(lots.parcel_scenes)}), hints {len(hints)}, unit-split reports {len(splits)}')
    rows = Allocator(gnaf, lots, hints, scenes, splits).allocate(features)
    # Rule-based rows without saved links for the agreement checks.
    rule_rows = {(r['scene_id'], r['object_id']): r for r in Allocator(gnaf, lots, hints, scenes, splits, use_saved_links=False).allocate(features)}
    saved = {(r['scene_id'], r['object_id']): r['address_id'] for r in rows if r['method'] == 'saved-link'}
    crm = crm_roof_links(root, features)
    checks = [agreement(rule_rows, saved, 'saved-link'),
              agreement(rule_rows, {k: v['address_id'] for k, v in crm.items()}, 'crm-roof-feature (IoU >= 0.5)'),
              agreement(rule_rows, {k: v for k, v in reference_links(root).items() if k in rule_rows}, 'T1/T2 reference with address_id')]
    checks[2]['note'] = ('reference address_id is the training capture address (image context), not a verified roof address; '
                         'on unit sites it can name a neighbouring unit. Not accuracy.')
    for row in rows:
        link = crm.get((row['scene_id'], row['object_id']))
        if link:
            row['evidence']['crm_roof_feature'] = link
    written = write_index(root, rows) if write else 0
    before_map = {f"{f['properties']['scene_id']}/{f['properties']['object_id']}": (f['properties'].get('property_address') or {}) for f in features}
    picks = ambiguous_to_matched(features, rows, 24)
    examples = [example_entry(r, before_map) for r in picks[:20]]
    sheet_path = contact_sheet(root, picks, lots, gnaf, scenes, report_dir/'ambiguous-to-matched.png') if sheet and picks else None
    summary = summarise(features, rows, checks, examples, sheet_path, time.time()-started)
    summary['index_rows_written'] = written
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir/'summary.json').write_text(json.dumps(summary, indent=1, sort_keys=True))
    (report_dir/'rows.jsonl').write_text(''.join(json.dumps(r, sort_keys=True)+'\n' for r in rows))
    log(f"wrote {written} index rows; before {summary['before']['status']}; after {summary['after']['status']}; "
        f"confidence {summary['after']['confidence']}; {summary['elapsed_seconds']} s")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--run', action='store_true', help='allocate every inventory roof, write index rows and the report')
    parser.add_argument('--report', help=f'report directory (default {REPORT_DIR})')
    parser.add_argument('--no-index', action='store_true', help='compute the report without writing eyes index rows')
    parser.add_argument('--no-sheet', action='store_true', help='skip the contact sheet')
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 2
    summary = run(Path(args.root), args.report, write=not args.no_index, sheet=not args.no_sheet)
    json.dump({k: v for k, v in summary.items() if k not in ('examples_ambiguous_to_matched',)} | {'agreement': [
        {k: v for k, v in c.items() if k != 'details'} for c in summary['agreement']]}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
