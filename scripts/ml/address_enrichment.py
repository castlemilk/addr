"""Per-address aggregation of allocated roofs and structures (agent hints, not labels).

Reads the ``address_allocation`` rows of the eyes index and, for every G-NAF
``address_id`` with an allocated roof (status matched or suggested), writes one
document under ``data/reviews/eyes/address-enrichment/addresses/<address_id>.json``
with EXACTLY the published schema (see ``SCHEMA``), an ``index.json`` and one row per
address in the eyes index table ``address_enrichment`` (this version's rows replaced).

primary_roof is the largest allocated roof whose class hint is not outbuilding and whose
structure sub-kind is not secondary; other allocated buildings are structures. Solar
panels, trees and pools are the scene's SAM objects whose centroid falls in the lot
(the deciding parcel or Vicmap property polygon), de-duplicated across overlapping
captures; counts are null when no lot polygon is known. Nothing under data/scenes is
written; every number is a model estimate.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import Point

from . import eyes_index
from .address_allocation import (
    CONFIDENCES,
    CONTAINMENT,
    SECONDARY_KINDS,
    STATUSES,
    Gnaf,
    Lots,
    Scenes,
    code_sha256,
    load_rows,
    lot_addresses,
    review_url,
)
from .building_addresses import load_index
from .scene_confidence import geographic
from .scene_object_quality import local_polygon
from .scene_suburb import progress

VERSION = 'address-enrichment-v1'
TABLE = 'address_enrichment'
REPORT_DIR = Path('data/reviews/eyes/address-enrichment')
NOTE = 'agent hints and model estimates, not labels or accuracy'
ALLOCATED = ('matched', 'suggested')
ENSURE = (29790, 30286, 30296)  # 27 Cambridge Road and its neighbours always get a document
DEDUPE_M = 1.5
DUPLICATE_IOU = .3  # the same roof seen in two captures; the inventory only hides near-identical footprints
OBJECT_KINDS = ('solar_panel', 'tree', 'pool')
POOL_KINDS = frozenset({'pool', 'pool-cover'})

# The published shape: top-level keys and the type of each. The web team codes against this.
SCHEMA = {
    'version': str, 'note': str, 'address_id': int, 'full_address': str, 'property_pfi': (str, type(None)),
    'lot': (dict, type(None)), 'allocation': dict, 'primary_roof': (dict, type(None)), 'structures': list,
    'solar': dict, 'trees': dict, 'pool': dict, 'roofs_total': int, 'computed_at': str,
}
PRIMARY_KEYS = ('scene_id', 'object_id', 'area_m2', 'roof_type', 'band', 'review_url')
STRUCTURE_KEYS = ('scene_id', 'object_id', 'sub_kind', 'confidence', 'area_m2', 'review_url')


def validate(document):
    """Raise ValueError when a document departs from SCHEMA; return it unchanged otherwise."""
    if set(document) != set(SCHEMA):
        raise ValueError(f'keys differ from schema: extra {sorted(set(document)-set(SCHEMA))}, missing {sorted(set(SCHEMA)-set(document))}')
    for key, kind in SCHEMA.items():
        value = document[key]
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ValueError(f'{key}: expected {kind}, got {type(value).__name__}')
    if document['version'] != VERSION:
        raise ValueError('version')
    if document['lot'] is not None and (set(document['lot']) != {'plan', 'lot'} or not all(v is None or isinstance(v, str) for v in document['lot'].values())):
        raise ValueError('lot must be {plan, lot} of str|null')
    allocation = document['allocation']
    if set(allocation) != {'status', 'method', 'confidence', 'reason', 'alternatives'}:
        raise ValueError('allocation keys')
    if allocation['status'] not in STATUSES or allocation['confidence'] not in CONFIDENCES:
        raise ValueError('allocation status/confidence')
    if not isinstance(allocation['method'], str) or not isinstance(allocation['reason'], str):
        raise ValueError('allocation method/reason')
    for alt in allocation['alternatives']:
        if set(alt) != {'address_id', 'full_address'} or not (alt['address_id'] is None or isinstance(alt['address_id'], int)) or not isinstance(alt['full_address'], str):
            raise ValueError('alternative shape')
    if document['primary_roof'] is not None and set(document['primary_roof']) != set(PRIMARY_KEYS):
        raise ValueError('primary_roof keys')
    for structure in document['structures']:
        if set(structure) != set(STRUCTURE_KEYS):
            raise ValueError('structure keys')
    if set(document['solar']) != {'count', 'area_m2'} or not isinstance(document['solar']['count'], int) or not isinstance(document['solar']['area_m2'], (int, float)):
        raise ValueError('solar')
    if set(document['trees']) != {'count_on_lot'} or not (document['trees']['count_on_lot'] is None or isinstance(document['trees']['count_on_lot'], int)):
        raise ValueError('trees')
    if set(document['pool']) != {'present'} or not (document['pool']['present'] is None or isinstance(document['pool']['present'], bool)):
        raise ValueError('pool')
    return document


# --- Objects per scene -------------------------------------------------------------------------------------

class SceneObjects:
    """Live SAM objects of a scene (solar panels, trees, pools) as WGS84 centroids with local areas, cached per scene."""

    def __init__(self, scenes):
        self.scenes = scenes
        self.cache = {}

    def get(self, scene_id):
        if scene_id in self.cache:
            return self.cache[scene_id]
        scene = self.scenes.get(scene_id)
        found = []
        for obj in (scene or {}).get('objects', []):
            if obj.get('kind') not in OBJECT_KINDS or obj.get('status') in ('superseded', 'rejected') or not obj.get('ring'):
                continue
            try:
                centroid = geographic(obj, scene).centroid
                area = float(local_polygon(obj, scene).area)
            except (ValueError, TypeError, KeyError, ZeroDivisionError):
                continue
            if centroid.is_empty or not math.isfinite(area):
                continue
            found.append({'kind': obj['kind'], 'object_id': obj['id'], 'centroid': centroid, 'area_m2': round(area, 2)})
        self.cache[scene_id] = found
        return found


def dedupe(objects, radius_m=DEDUPE_M):
    """Drop repeats of one object seen in overlapping captures: same kind, centroids within radius_m."""
    kept = []
    for obj in sorted(objects, key=lambda o: (o['kind'], -o['area_m2'], o['object_id'])):
        lat = obj['centroid'].y
        dlat, dlon = radius_m/111320., radius_m/(111320.*max(math.cos(math.radians(lat)), 1e-6))
        if any(k['kind'] == obj['kind'] and abs(k['centroid'].x-obj['centroid'].x) <= dlon and abs(k['centroid'].y-obj['centroid'].y) <= dlat for k in kept):
            continue
        kept.append(obj)
    return kept


# --- Lot polygon -------------------------------------------------------------------------------------------------

def lot_polygon(rows, lots, gnaf, gnaf_row):
    """The polygon that stands for the address's lot: the deciding lot, its property, a holding parcel, or the property owning its point."""
    by_identity = {(e['source'], e['pfi']): e for e in lots.entries}
    for row in rows:
        evidence = row.get('evidence') or {}
        for pointer in (evidence.get('containment'), evidence.get('unit_lot')):
            if pointer and (pointer.get('source'), pointer.get('pfi')) in by_identity:
                entry = by_identity[(pointer['source'], pointer['pfi'])]
                return entry['geometry'], entry
        if row.get('property_pfi') and ('vicmap-property', str(row['property_pfi'])) in by_identity:
            entry = by_identity[('vicmap-property', str(row['property_pfi']))]
            return entry['geometry'], entry
        for lot in evidence.get('lots') or []:
            if lot['share'] >= CONTAINMENT and lot['source'] == 'vicmap-parcel' and (lot['source'], lot['pfi']) in by_identity:
                entry = by_identity[(lot['source'], lot['pfi'])]
                return entry['geometry'], entry
    if gnaf_row is not None and lots.tree is not None:
        point = Point(gnaf_row['lon'], gnaf_row['lat'])
        for index in lots.tree.query(point, predicate='within'):
            entry = lots.entries[int(index)]
            if entry['source'] == 'vicmap-property' and any(a['address_id'] == gnaf_row['address_id'] for a in lot_addresses(entry, gnaf)[0]):
                return entry['geometry'], entry
    return None, None


# --- Documents -----------------------------------------------------------------------------------------------------

def _roof_type(scenes, row):
    obj = scenes.obj(row['scene_id'], row['object_id']) or {}
    roof = obj.get('roof') if isinstance(obj.get('roof'), dict) else {}
    value = obj.get('roof_type') or roof.get('roof_type') or roof.get('type') or (obj.get('model') or {}).get('roof_type')
    return str(value) if value else None


def _is_secondary(row):
    hints = (row.get('evidence') or {}).get('hints') or {}
    return hints.get('sub_kind') in SECONDARY_KINDS or hints.get('class_hint') == 'outbuilding' or 'secondary-structure' in (hints.get('flags') or [])


def _primary_choice(rows):
    """Largest roof that is neither an outbuilding hint nor a secondary sub-kind; else the largest roof."""
    ordered = sorted(rows, key=lambda r: (-(r.get('area_m2') or 0), r['scene_id'], r['object_id']))
    for row in ordered:
        if not _is_secondary(row):
            return row
    return ordered[0] if ordered else None


def _allocation_block(row):
    return {'status': row['status'], 'method': row['method'], 'confidence': row['confidence'], 'reason': row['reason'],
            'alternatives': [{'address_id': a.get('address_id'), 'full_address': str(a.get('full_address') or '')} for a in row.get('alternatives') or []]}


def _empty_allocation():
    return {'status': 'unmatched', 'method': 'none', 'confidence': 'weak',
            'reason': 'No roof in the current suburb inventory is allocated to this address.', 'alternatives': []}


def _structure(row):
    hints = (row.get('evidence') or {}).get('hints') or {}
    sub_kind = hints.get('sub_kind') or hints.get('class_hint') or 'building'
    return {'scene_id': row['scene_id'], 'object_id': row['object_id'], 'sub_kind': sub_kind,
            'confidence': hints.get('sub_confidence'), 'area_m2': row.get('area_m2'),
            'review_url': row.get('review_url') or review_url(row['scene_id'], row['object_id'])}


def _roof_geometry(scenes, row):
    obj, scene = scenes.obj(row['scene_id'], row['object_id']), scenes.get(row['scene_id'])
    if not obj or not scene or not obj.get('ring'):
        return None
    try:
        geometry = geographic(obj, scene)
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return None
    return geometry if geometry.is_valid and not geometry.is_empty else geometry.buffer(0)


def distinct_roofs(rows, scenes, threshold=DUPLICATE_IOU):
    """Largest-first greedy: a roof overlapping an already kept roof above the IoU threshold is the same roof in another capture."""
    kept, dropped = [], []
    for row in sorted(rows, key=lambda r: (-(r.get('area_m2') or 0), r['scene_id'], r['object_id'])):
        geometry = _roof_geometry(scenes, row)
        duplicate = None
        if geometry is not None:
            for other, other_geometry in kept:
                if other_geometry is None:
                    continue
                union = geometry.union(other_geometry).area
                if union > 0 and geometry.intersection(other_geometry).area/union > threshold:
                    duplicate = other
                    break
        if duplicate is None:
            kept.append((row, geometry))
        else:
            dropped.append((row, duplicate))
    return [row for row, _ in kept], dropped


def _crm_lot(lots, address_id):
    for entry in lots.entries:
        if entry['source'] == 'crm-boundary' and entry.get('address_id') == address_id and (entry.get('plan') or entry.get('lot')):
            return {'plan': entry.get('plan'), 'lot': entry.get('lot')}
    return None


def build_document(address_id, gnaf_row, rows, lots, gnaf, scenes, objects, stamp, stats=None):
    rows, duplicates = distinct_roofs(rows, scenes)
    if stats is not None:
        stats['duplicate_roofs_dropped'] += len(duplicates)
    primary = _primary_choice(rows)
    polygon, entry = lot_polygon(rows, lots, gnaf, gnaf_row)
    scene_ids = list(dict.fromkeys(r['scene_id'] for r in rows))  # no allocated roof: no scene objects attributed
    seen = dedupe([o for sid in scene_ids for o in objects.get(sid)])
    on_lot = [o for o in seen if polygon is not None and polygon.covers(o['centroid'])]
    solar = [o for o in on_lot if o['kind'] == 'solar_panel']
    trees = [o for o in on_lot if o['kind'] == 'tree']
    structures = [_structure(r) for r in rows if primary is None or (r['scene_id'], r['object_id']) != (primary['scene_id'], primary['object_id'])]
    pool_structure = any(s['sub_kind'] in POOL_KINDS for s in structures)
    pool_object = any(o['kind'] == 'pool' for o in on_lot)
    pool = True if (pool_structure or pool_object) else (False if polygon is not None else None)
    document = {
        'version': VERSION, 'note': NOTE, 'address_id': int(address_id),
        'full_address': str(gnaf_row['full_address'] if gnaf_row else (primary or {}).get('full_address') or ''),
        'property_pfi': (str(primary['property_pfi']) if primary and primary.get('property_pfi') else (str(entry['property_pfi']) if entry and entry.get('property_pfi') else None)),
        'lot': (primary.get('lot') if primary else None) or _crm_lot(lots, int(address_id))
               or ({'plan': entry.get('plan'), 'lot': entry.get('lot')} if entry and entry['source'] == 'vicmap-parcel' and (entry.get('plan') or entry.get('lot')) else None),
        'allocation': _allocation_block(primary) if primary else _empty_allocation(),
        'primary_roof': ({'scene_id': primary['scene_id'], 'object_id': primary['object_id'], 'area_m2': primary.get('area_m2'),
                          'roof_type': _roof_type(scenes, primary), 'band': primary.get('band'),
                          'review_url': primary.get('review_url') or review_url(primary['scene_id'], primary['object_id'])} if primary else None),
        'structures': structures,
        'solar': {'count': len(solar), 'area_m2': round(sum(o['area_m2'] for o in solar), 2)},
        'trees': {'count_on_lot': len(trees) if polygon is not None else None},
        'pool': {'present': pool},
        'roofs_total': len(rows),
        'computed_at': stamp,
    }
    return validate(document)


# --- Writes --------------------------------------------------------------------------------------------------------

def write_documents(root, documents, report_dir):
    folder = Path(report_dir)/'addresses'
    folder.mkdir(parents=True, exist_ok=True)
    current = {f'{d["address_id"]}.json' for d in documents}
    for stale in folder.glob('*.json'):
        if stale.name not in current:
            stale.unlink()
    for document in documents:
        (folder/f'{document["address_id"]}.json').write_text(json.dumps(document, indent=1, sort_keys=True))
    kinds = Counter()
    index = {'version': VERSION, 'note': NOTE, 'computed_at': datetime.now(UTC).isoformat(), 'code_sha256': code_sha256(),
             'addresses': {}}
    for document in documents:
        structure_kinds = Counter(s['sub_kind'] for s in document['structures'])
        kinds.update(structure_kinds)
        index['addresses'][str(document['address_id'])] = {
            'full_address': document['full_address'], 'roofs_total': document['roofs_total'],
            'structure_kinds': dict(sorted(structure_kinds.items())), 'status': document['allocation']['status'],
            'confidence': document['allocation']['confidence'], 'has_primary': document['primary_roof'] is not None}
    index['counts'] = {'addresses': len(documents), 'with_primary': sum(1 for d in documents if d['primary_roof'] is not None),
                       'structure_kinds': dict(sorted(kinds.items())),
                       'solar_addresses': sum(1 for d in documents if d['solar']['count']),
                       'pool_addresses': sum(1 for d in documents if d['pool']['present']),
                       'trees_known': sum(1 for d in documents if d['trees']['count_on_lot'] is not None),
                       'status': dict(sorted(Counter(d['allocation']['status'] for d in documents).items()))}
    (Path(report_dir)/'index.json').write_text(json.dumps(index, indent=1, sort_keys=True))
    return index


def write_index(root, documents):
    rows = [{'address_id': d['address_id'], 'full_address': d['full_address'], 'version': VERSION, 'payload': d} for d in documents]
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        db.execute(f'DELETE FROM "{TABLE}" WHERE version=?', (VERSION,))
        return eyes_index.replace_rows(db, TABLE, rows)


def run(root=Path('.'), report_dir=None, ensure=ENSURE, write=True, log=print):
    root = Path(root)
    report_dir = Path(report_dir) if report_dir else root/REPORT_DIR
    started = time.time()
    rows = load_rows(root)
    if not rows:
        raise ValueError('No address_allocation rows in the eyes index; run address_allocation --run first.')
    suburb = progress(root)
    lots = Lots.load(root, load_index(root))
    if suburb:
        bounds = suburb['bounds']
    else:
        xs = [e['geometry'].bounds for e in lots.entries]
        bounds = (min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs), max(b[3] for b in xs))
    pad = .003
    gnaf = Gnaf.load(root, (bounds[0]-pad, bounds[1]-pad, bounds[2]+pad, bounds[3]+pad))
    scenes = Scenes(root)
    objects = SceneObjects(scenes)
    by_address = defaultdict(list)
    for row in rows:
        if row.get('status') in ALLOCATED and isinstance(row.get('address_id'), int):
            by_address[row['address_id']].append(row)
    for address_id in ensure or ():
        by_address.setdefault(int(address_id), [])
    stamp = datetime.now(UTC).isoformat()
    documents, stats = [], Counter()
    for address_id in sorted(by_address):
        gnaf_row = gnaf.by_id.get(address_id)
        if gnaf_row is None:
            with closing(sqlite3.connect(f'file:{root/"data/australia_geo.db"}?mode=ro', uri=True)) as db:
                found = db.execute('SELECT id AS address_id, full_address, unit, number, street, suburb, lon, lat FROM addresses WHERE id=?', (address_id,)).fetchone()
            if found:
                gnaf_row = dict(zip(('address_id', 'full_address', 'unit', 'number', 'street', 'suburb', 'lon', 'lat'), found, strict=True))
        if gnaf_row is None and not by_address[address_id]:
            log(f'address {address_id}: unknown to the local G-NAF table and no allocated roof; skipped')
            continue
        documents.append(build_document(address_id, gnaf_row, by_address[address_id], lots, gnaf, scenes, objects, stamp, stats))
    index = write_documents(root, documents, report_dir)
    index['counts']['duplicate_roofs_dropped'] = stats['duplicate_roofs_dropped']
    index['counts']['allocated_rows'] = sum(len(v) for v in by_address.values())
    written = write_index(root, documents) if write else 0
    index['index_rows_written'] = written
    index['elapsed_seconds'] = round(time.time()-started, 1)
    (Path(report_dir)/'index.json').write_text(json.dumps(index, indent=1, sort_keys=True))
    log(f"{len(documents)} address documents, {written} index rows, counts {index['counts']}, {index['elapsed_seconds']} s")
    return index, documents


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--run', action='store_true', help='aggregate every allocated address and write documents, index.json and index rows')
    parser.add_argument('--report', help=f'output directory (default {REPORT_DIR})')
    parser.add_argument('--address', type=int, action='append', default=[], help='address_id that must get a document even without a roof (repeatable)')
    parser.add_argument('--no-index', action='store_true', help='write documents only, no eyes index rows')
    parser.add_argument('--show', type=int, action='append', default=[], help='print these address documents after the run')
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 2
    index, documents = run(Path(args.root), args.report, ensure=tuple(ENSURE)+tuple(args.address), write=not args.no_index)
    shown = {d['address_id']: d for d in documents}
    for address_id in args.show:
        json.dump(shown.get(address_id, {'address_id': address_id, 'missing': True}), sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write('\n')
    json.dump({k: v for k, v in index.items() if k != 'addresses'}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
