"""Quality signals for scene objects: non-building plausibility and building classification hints.

These are diagnostics, not labels or accuracy: detector support tiers describe
model confidence, size z-scores describe class typicality, and coverage or
overlap fractions describe geometric plausibility. Building rows (version
building-flags-v2, opt-in) carry class hints (tree-like, secondary structure,
non-residential context, commercial) with their evidence so a reviewer can skip
or reclassify; every hint is an agent hint, not a label, and nothing is decided
automatically. Rows are sidecars in the eyes index; nothing here rewrites
latest.json or any production artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely import make_valid
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from . import context_landuse, eyes_index
from .reference_agreement import lonlat_to_local
from .scene_confidence import geographic
from .scene_self_review import GEOMETRY_ERRORS, validate_id

VERSION = 'object-quality-v1'
SUPPORT_TIER_NOTE = 'detector support, not accuracy'
SUPPORT_TIERS = (('<0.5', .5), ('0.5-0.65', .65), ('0.65-0.8', .8), ('>=0.8', math.inf))
THRESHOLDS = {'giant_m2': {'solar_panel': 200., 'tree': 2000.}, 'tiny_m2': {'tree': 1., 'solar_panel': .5},
              'unparented_coverage': .10, 'low_support': .5, 'heavy_same_class_overlap': .5, 'edge_px': 1.}
MAD_SCALE = 1.4826  # consistent with a normal standard deviation


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def support_tier(score):
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
        return None
    return next(name for name, upper in SUPPORT_TIERS if score < upper)


def local_polygon(obj, scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    def convert(points):
        return (np.c_[points, np.ones(len(points))] @ frame).tolist()
    shape = Polygon(convert(obj['ring']), [convert(h) for h in obj.get('holes', [])])
    if shape.is_valid:
        return shape
    # make_valid keeps every lobe of a self-intersecting ring (buffer(0) can drop one); keep polygonal parts only.
    fixed = make_valid(shape)
    parts = [g for g in getattr(fixed, 'geoms', [fixed]) if g.geom_type in ('Polygon', 'MultiPolygon')]
    return unary_union(parts) if parts else Polygon()


def _fraction(shape, other):
    return float(shape.intersection(other).area/shape.area) if other is not None and not other.is_empty and shape.area > 0 else 0.


def _iou(a, b):
    union = a.union(b).area
    return float(a.intersection(b).area/union) if union > 0 else 0.


def _live(scene):
    return [o for o in scene['objects'] if o.get('status') != 'superseded']


def _owned_ids(scene, only_owned):
    """Owned ids, or None (every live object) when not filtering or the scene has no ownership list."""
    if only_owned and scene.get('owned_object_ids') is not None:
        return set(scene['owned_object_ids'])
    return None


def _low_support(score):
    return isinstance(score, (int, float)) and not isinstance(score, bool) and score < THRESHOLDS['low_support']


def _log_area_z(area, stats):
    if not stats or area is None or area <= 0:
        return None
    spread = stats.get('mad_log_area') or 0
    if spread <= 0:
        return None
    return round((math.log(area)-stats['median_log_area'])/(MAD_SCALE*spread), 4)


def _statistics(areas_by_kind):
    stats = {}
    for kind, areas in areas_by_kind.items():
        logs = np.log([a for a in areas if a > 0])
        if len(logs) == 0:
            continue
        median = float(np.median(logs))
        stats[kind] = {'median_log_area': round(median, 6), 'mad_log_area': round(float(np.median(np.abs(logs-median))), 6),
                       'count': int(len(logs))}
    return stats


def _scene_areas(scene, only_owned=True):
    owned = _owned_ids(scene, only_owned)
    areas = {}
    for obj in _live(scene):
        if obj['kind'] == 'building' or (owned is not None and obj['id'] not in owned):
            continue
        try:
            area = local_polygon(obj, scene).area
        except GEOMETRY_ERRORS:
            continue
        areas.setdefault(obj['kind'], []).append(area)
    return areas


def class_statistics(root=Path('.'), scene_ids=(), only_owned=True):
    """Per-class median/MAD of log area over the owned non-building objects of the given scenes."""
    areas = {}
    for scene_id in dict.fromkeys(scene_ids):
        scene = json.loads((Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())
        for kind, values in _scene_areas(scene, only_owned).items():
            areas.setdefault(kind, []).extend(values)
    return _statistics(areas)


def assess_scene(scene, *, only_owned=True, class_stats=None, buildings=False, image=None, context=None):
    """One sidecar row per live non-building object; pure function of the scene dict.

    With buildings=True, building rows (version building-flags-v2) are appended; they need the scene
    image for the tree-like features and a Context for property, POI and land-use evidence. The
    non-building rows are unchanged by that option.
    """
    owned = _owned_ids(scene, only_owned)
    if class_stats is None:
        class_stats = _statistics(_scene_areas(scene, only_owned))
    live = _live(scene)
    roofs = {}
    for obj in live:
        if obj['kind'] == 'building':
            try:
                roofs[obj['id']] = local_polygon(obj, scene)
            except GEOMETRY_ERRORS:
                continue
    footprint = unary_union(list(roofs.values())) if roofs else None
    frame = box(0, 0, scene['image_width'], scene['image_height']).boundary
    subjects = [o for o in live if o['kind'] != 'building']
    shapes = {}
    for obj in subjects:
        try:
            shapes[obj['id']] = local_polygon(obj, scene)
        except GEOMETRY_ERRORS:
            shapes[obj['id']] = None
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    scope = 'owned_object_ids' if owned is not None else 'all_live'
    rows = []
    for obj in subjects:
        if owned is not None and obj['id'] not in owned:
            continue
        kind, score = obj['kind'], obj.get('score')
        payload = {'kind': kind, 'label': obj.get('label'), 'score': score, 'support_tier': support_tier(score),
                   'support_tier_note': SUPPORT_TIER_NOTE, 'status': obj.get('status'), 'parent_id': obj.get('parent_id'),
                   'population_scope': scope, 'version': VERSION, 'code_sha256': code, 'computed_at': stamp}
        shape = shapes[obj['id']]
        if shape is None or shape.is_empty or shape.area <= 0:
            payload.update({'area_m2': None, 'log_area_z': None, 'parent_coverage': None, 'parent_source': None, 'unparented': None,
                            'building_overlap_fraction': None, 'same_class_overlap_max': None, 'same_class_overlap_partner': None,
                            'edge_touch': None, 'error': 'invalid outline',
                            'plausibility_flags': ['invalid_geometry'] + (['low_support'] if _low_support(score) else [])})
            rows.append(_row(scene, obj, payload))
            continue
        area = float(shape.area)
        parent = roofs.get(obj.get('parent_id'))
        if parent is not None:
            coverage, parent_source = _fraction(shape, parent), 'parent_id'
        else:
            coverage = max((_fraction(shape, b) for b in roofs.values()), default=0.)
            parent_source = 'best_covering' if not obj.get('parent_id') else 'best_covering_parent_missing'
        over_buildings = _fraction(shape, footprint)
        partner, overlap = None, 0.
        for other in subjects:
            if other['id'] == obj['id'] or other['kind'] != kind or shapes.get(other['id']) is None or not shape.intersects(shapes[other['id']]):
                continue
            iou = _iou(shape, shapes[other['id']])
            if iou > overlap:
                partner, overlap = other['id'], iou
        edge_touch = bool(Polygon(obj['ring']).distance(frame) <= THRESHOLDS['edge_px'])
        unparented = over_buildings < THRESHOLDS['unparented_coverage'] if kind == 'solar_panel' else None
        flags = []
        giant = THRESHOLDS['giant_m2'].get(kind)
        if giant is not None and area > giant:
            flags.append('giant_solar' if kind == 'solar_panel' else 'giant_tree')
        tiny = THRESHOLDS['tiny_m2'].get(kind)
        if tiny is not None and area < tiny:
            flags.append('tiny_object')
        if unparented:
            flags.append('unparented_solar')
        if _low_support(score):
            flags.append('low_support')
        if overlap > THRESHOLDS['heavy_same_class_overlap']:
            flags.append('heavy_same_class_overlap')
        payload.update({'area_m2': round(area, 3), 'log_area_z': _log_area_z(area, class_stats.get(kind)),
                        'parent_coverage': round(coverage, 4), 'parent_source': parent_source, 'unparented': unparented,
                        'building_overlap_fraction': round(over_buildings, 4), 'same_class_overlap_max': round(overlap, 4),
                        'same_class_overlap_partner': partner, 'edge_touch': edge_touch, 'plausibility_flags': flags})
        rows.append(_row(scene, obj, payload))
    if buildings:
        rows.extend(assess_buildings(scene, image=image, only_owned=only_owned, context=context))
    return rows


def _row(scene, obj, payload, version=VERSION):
    return {'scene_id': scene['id'], 'object_id': obj['id'], 'model_signature': scene.get('model_signature'),
            'version': version, 'payload': payload}


def replace_scene_rows(db, table, version, scene_ids, rows):
    """A run defines each scene's current population: drop its older rows (stale model_signature or wider scope)."""
    for scene_id in scene_ids:
        db.execute(f'DELETE FROM "{table}" WHERE scene_id=? AND version=?', (scene_id, version))
    return eyes_index.replace_rows(db, table, rows)  # commits the deletes and inserts together


def run(root=Path('.'), scene_ids=(), only_owned=True, buildings=False, context=None, offline=False, refresh_context=False):
    """Assess scenes with shared class statistics and write object_quality sidecar rows (replacing each scene's rows).

    buildings=True also writes building-flags-v2 rows (only_owned respected) using the scene PNG when its
    sha256 matches and a Context (built once per run unless passed); both versions are replaced per scene.
    """
    root, scene_ids = Path(root), list(dict.fromkeys(scene_ids))
    stats = class_statistics(root, scene_ids, only_owned)
    if buildings and context is None:
        context = Context.load(root, scene_ids, offline=offline, refresh=refresh_context)
    rows, building_rows = [], []
    for scene_id in scene_ids:
        scene = json.loads((root/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())
        rows.extend(assess_scene(scene, only_owned=only_owned, class_stats=stats))
        if buildings:
            building_rows.extend(assess_buildings(scene, image=load_image(root, scene), only_owned=only_owned, context=context))
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        written = replace_scene_rows(db, 'object_quality', VERSION, scene_ids, rows)
        if buildings:
            written += replace_scene_rows(db, 'object_quality', BUILDING_VERSION, scene_ids, building_rows)
        return written


# --- Building classification hints (building-flags-v2) -------------------------------------------------------
# Every flag carries its evidence numbers; class_hint is an agent hint for the reviewer, never a label.

BUILDING_VERSION = 'building-flags-v2'
HINT_NOTE = 'agent hint, not a label'
CLASS_HINTS = ('house', 'units', 'commercial', 'outbuilding', 'tree-like', 'non-residential')
BUILDING_THRESHOLDS = {'exg': 20, 'exg_green_share': .45, 'solidity': .80, 'tree_overlap_iou': .3,
                       'secondary_area_m2': 90., 'secondary_ratio': 3., 'secondary_ratio_property': 2., 'proximity_m': 25.,
                       'poi_m': 30., 'landuse_m': 20., 'commercial_area_m2': 400.}
RULES = {'tree-like': 'exg_green_share >= 0.45 and (solidity <= 0.80 or tree_overlap_iou >= 0.3)',
         'secondary-structure': 'area_m2 < 90 and largest_same_property_area / area_m2 >= 3 (25 m proximity proxy) or >= 2 (same Vicmap property)',
         'non-residential-context': 'OSM land use within 20 m, a POI within 30 m, a business address kind or a park/school-like Vicmap site name',
         'commercial': 'area_m2 >= 400 and (non-residential context or address kind business/large)'}
NON_RESIDENTIAL_ADDRESS_KINDS = ('business',)
COMMERCIAL_ADDRESS_KINDS = ('business', 'large')
SITE_NAME_PATTERN = re.compile(r'\b(PARK|RESERVE|OVAL|SCHOOL|COLLEGE|KINDER\w*|CHURCH|CHAPEL|CENTRE|CENTER|CLUB|HALL|STATION|'
                               r'SHOPPING|PLAZA|ARCADE|LIBRARY|HOSPITAL|CLINIC|SURGERY|PAVILION|SCOUT\w*|POOL)\b')
INVENTORY_CACHE = Path('data/reviews/eyes/context/inventory-properties.json')
INVENTORY_VERSION = 'inventory-properties-v1'
ADDRESS_ROOFS = Path('public/address-roofs.json')
GEO_DB = Path('data/australia_geo.db')


def normalise_address(text):
    if not isinstance(text, str):
        return None
    text = re.sub(r'[,]', ' ', text.upper())
    text = re.sub(r'\bVIC\b', ' ', text)
    return re.sub(r'\s+', ' ', text).strip() or None


def scene_lonlat_bounds(scene):
    """WGS84 bounds of the scene frame from its pixel corners (west, south, east, north)."""
    w, h = scene['image_width'], scene['image_height']
    return geographic({'ring': [[0, 0], [w, 0], [w, h], [0, h]]}, scene).bounds


def load_image(root, scene):
    """The scene PNG as an RGB array when its sha256 matches; None (recorded, never raised) otherwise."""
    digest = scene.get('image_sha256', '')
    try:
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            return None
        source = (Path(root)/'data/scenes'/validate_id(scene['id'])/f'{digest}.png').read_bytes()
        if hashlib.sha256(source).hexdigest() != digest:
            return None
        import io as _io
        with Image.open(_io.BytesIO(source)) as opened:
            image = opened.convert('RGB')
        if image.size != (scene['image_width'], scene['image_height']):
            return None
        return np.asarray(image)
    except (OSError, ValueError, KeyError):
        return None


class Context:
    """Property, address, POI and land-use evidence shared by every building of a run.

    inventory: {"scene/object": {property_pfi, area_m2, full_address, site_names, address_kind, ...}} from
    scene_processing.inventory (cached once per run); address_kinds: normalised G-NAF address -> kind from
    public/address-roofs.json; pois: [{id, name, category, lon, lat}] from the places table (mode=ro);
    landuse: context_landuse.Landuse. Everything is optional and its absence is recorded in the evidence.
    """

    def __init__(self, inventory=None, address_kinds=None, pois=(), landuse=None, notes=None, coverage=None):
        self.inventory = inventory
        self.address_kinds = address_kinds or {}
        self.pois = list(pois)
        self.landuse = landuse if landuse is not None else context_landuse.Landuse({'status': 'unavailable', 'features': []})
        # WGS84 box the POIs and land use were loaded for; scenes outside it get landuse_status 'outside coverage'.
        self.coverage = tuple(coverage) if coverage else self.landuse.bbox
        self.notes = notes or {}
        self.by_property = {}
        for key, entry in (inventory or {}).items():
            pfi = entry.get('property_pfi')
            if pfi and isinstance(entry.get('area_m2'), (int, float)):
                self.by_property.setdefault(str(pfi), []).append((key, float(entry['area_m2'])))
        self._scene_pois = {}

    def describe(self):
        return {'inventory': 'available' if self.inventory is not None else 'unavailable', 'inventory_features': len(self.inventory or {}),
                'address_kinds': len(self.address_kinds), 'pois': len(self.pois), 'landuse': self.landuse.status,
                'landuse_features': len(self.landuse.features), 'coverage': list(self.coverage) if self.coverage else None, **self.notes}

    def covers(self, scene):
        """Whether the scene frame lies inside the box the POIs and land use were loaded for (unknown box -> True)."""
        if not self.coverage:
            return True
        try:
            bounds = scene_lonlat_bounds(scene)
        except (KeyError, TypeError, ValueError):
            return False      # a scene without a geographic frame cannot be inside the loaded box
        return context_landuse.Landuse({'bbox': list(self.coverage)}).covers(bounds)

    def landuse_status(self, scene):
        """'available', 'unavailable' (offline) or 'outside coverage' (the scene lies beyond the fetched box)."""
        if self.landuse.status == 'available' and not self.covers(scene):
            return 'outside coverage'
        return self.landuse.status

    def pois_for(self, scene):
        """POIs as (local-metre point, poi) within 60 m of the scene frame, cached per scene id."""
        key = scene.get('id')
        if key not in self._scene_pois:
            try:
                west, south, east, north = scene_lonlat_bounds(scene)
            except (KeyError, TypeError, ValueError):
                self._scene_pois[key] = []      # no geographic frame: no POIs can be placed, recorded as none within 30 m
                return self._scene_pois[key]
            pad = .001
            corners = np.c_[[[0, 0], [scene['image_width'], scene['image_height']]], [1, 1]] @ np.asarray(scene['pixel_to_meters'], dtype=float)
            frame = box(corners[:, 0].min(), corners[:, 1].min(), corners[:, 0].max(), corners[:, 1].max()).buffer(60)
            found = []
            for poi in self.pois:
                lon, lat = poi.get('lon'), poi.get('lat')
                if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                    continue
                if not (west-pad <= lon <= east+pad and south-pad <= lat <= north+pad):
                    continue
                point = Point(lonlat_to_local([[lon, lat]], scene)[0])
                if frame.contains(point):
                    found.append((point, poi))
            self._scene_pois[key] = found
        return self._scene_pois[key]

    def address_kind(self, entry):
        """Kind from public/address-roofs.json for the property's address (or any candidate), if any."""
        if not entry:
            return None
        names = [entry.get('full_address')] + [c.get('full_address') for c in entry.get('candidates', []) if isinstance(c, dict)]
        for name in names:
            kind = self.address_kinds.get(normalise_address(name))
            if kind:
                return kind
        return None

    @classmethod
    def load(cls, root=Path('.'), scene_ids=(), *, offline=False, refresh=False, fetch=None):
        """Build the context from disk: cached inventory, address-roofs kinds, POIs (mode=ro) and OSM land use."""
        root = Path(root)
        notes = {}
        inventory = load_inventory_properties(root, refresh=refresh, notes=notes)
        kinds = load_address_kinds(root)
        bounds = suburb_bounds(root)
        scenes = []
        for scene_id in dict.fromkeys(scene_ids):
            try:
                scenes.append(json.loads((root/'data/scenes'/validate_id(scene_id)/'latest.json').read_text()))
            except (OSError, ValueError):
                continue
        if bounds is None and scenes:
            boxes = np.array([scene_lonlat_bounds(s) for s in scenes])
            bounds = (float(boxes[:, 0].min()), float(boxes[:, 1].min()), float(boxes[:, 2].max()), float(boxes[:, 3].max()))
        pois = load_pois(root, bounds) if bounds else []
        landuse_fetch = None if offline else (fetch or context_landuse.fetch)
        doc = context_landuse.load(root, bounds, fetch=landuse_fetch, refresh=refresh) if bounds else {'status': 'unavailable', 'features': []}
        if doc.get('error'):
            notes['landuse_error'] = doc['error']
        return cls(inventory=inventory, address_kinds=kinds, pois=pois, landuse=context_landuse.Landuse(doc), notes=notes, coverage=bounds)


def suburb_bounds(root):
    try:
        from .scene_suburb import progress
        suburb = progress(Path(root))
    except Exception:  # noqa: BLE001 - no suburb run means no suburb bounds
        return None
    bounds = suburb.get('bounds') if suburb else None
    return tuple(float(v) for v in bounds) if bounds and len(bounds) == 4 else None


def load_address_kinds(root):
    path = Path(root)/ADDRESS_ROOFS
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    kinds = {}
    for entry in entries if isinstance(entries, list) else []:
        key = normalise_address(entry.get('full_address')) if isinstance(entry, dict) else None
        if key and entry.get('kind'):
            kinds[key] = entry['kind']
    return kinds


def load_pois(root, bounds, pad=.002):
    """Places (POIs) inside the padded bounds from the production geo database, opened read-only."""
    path = Path(root)/GEO_DB
    if not path.exists():
        return []
    west, south, east, north = bounds
    try:
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT id, name, category, lon, lat FROM places WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?',
                              (west-pad, east+pad, south-pad, north+pad)).fetchall()
    except sqlite3.Error:
        return []
    return [{'id': r[0], 'name': r[1], 'category': r[2], 'lon': r[3], 'lat': r[4]} for r in rows]


def load_inventory_properties(root, refresh=False, notes=None):
    """{"scene/object": property fields} from scene_processing.inventory, cached once under data/reviews/eyes/context."""
    root = Path(root)
    cache = root/INVENTORY_CACHE
    notes = notes if notes is not None else {}
    if cache.exists() and not refresh:
        try:
            doc = json.loads(cache.read_text())
            if doc.get('version') == INVENTORY_VERSION and isinstance(doc.get('features'), dict):
                notes['inventory_cached_at'] = doc.get('computed_at')
                return doc['features']
        except (OSError, ValueError):
            pass
    try:
        from .scene_processing import inventory
        found = inventory(root)
    except Exception as error:  # noqa: BLE001 - the inventory is optional evidence; the proxy takes over
        notes['inventory_error'] = str(error)
        return None
    if not found.get('suburb'):
        notes['inventory_error'] = 'no suburb run'
        return None
    features = {}
    for feature in found.get('features', []):
        props = feature.get('properties', {})
        address = props.get('property_address') or {}
        key = f"{props.get('scene_id')}/{props.get('object_id')}"
        features[key] = {'property_pfi': address.get('property_pfi'), 'area_m2': props.get('area_m2'), 'status': address.get('status'),
                         'method': address.get('method'), 'full_address': address.get('full_address'), 'address_kind': address.get('address_kind'),
                         'site_names': address.get('site_names') or [], 'address_id': address.get('address_id'),
                         'candidates': [{'full_address': c.get('full_address')} for c in address.get('candidates', []) if isinstance(c, dict)][:8]}
    doc = {'version': INVENTORY_VERSION, 'computed_at': datetime.now(UTC).isoformat(), 'inventory_fetched_at': found.get('fetched_at'),
           'features': features, 'note': 'Vicmap property candidates per roof, read from scene_processing.inventory; site identity, not a roof label.'}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(doc, separators=(',', ':')))
    return features


def _mask(obj, shape_px):
    """Boolean mask of the ring minus holes, cropped to the ring's pixel bounding box: (mask, (y0, x0))."""
    ring = np.asarray(obj['ring'], dtype=float)
    height, width = shape_px
    x0, y0 = max(int(np.floor(ring[:, 0].min()))-1, 0), max(int(np.floor(ring[:, 1].min()))-1, 0)
    x1, y1 = min(int(np.ceil(ring[:, 0].max()))+2, width), min(int(np.ceil(ring[:, 1].max()))+2, height)
    if x1 <= x0 or y1 <= y0:
        return None, (0, 0)
    canvas = Image.new('L', (x1-x0, y1-y0), 0)
    draw = ImageDraw.Draw(canvas)
    draw.polygon([(x-x0, y-y0) for x, y in ring], fill=1)
    for hole in obj.get('holes', []) or []:
        if len(hole) >= 3:
            draw.polygon([(x-x0, y-y0) for x, y in hole], fill=0)
    return np.asarray(canvas, dtype=bool), (y0, x0)


def tree_like_features(obj, shape, scene, image, trees):
    """ExG green share and Laplacian texture inside the outline (original image only), solidity, vertex density, tree IoU."""
    hull = shape.convex_hull.area
    features = {'exg_green_share': None, 'texture': None,
                'solidity': round(float(shape.area/hull), 4) if hull > 0 else None,
                'vertex_density': round(len(obj['ring'])/shape.area*100, 3) if shape.area > 0 else None,
                'tree_overlap_iou': round(max((_iou(shape, t) for t in trees if t is not None and shape.intersects(t)), default=0.), 4)}
    if image is None:
        features['image'] = 'unavailable'
        return features
    mask, (y0, x0) = _mask(obj, image.shape[:2])
    if mask is None or not mask.any():
        features['image'] = 'outline outside the frame'
        return features
    crop = image[y0:y0+mask.shape[0], x0:x0+mask.shape[1]].astype(np.int16)
    if crop.shape[:2] != mask.shape:
        mask = mask[:crop.shape[0], :crop.shape[1]]
    exg = 2*crop[..., 1]-crop[..., 0]-crop[..., 2]
    features['exg_green_share'] = round(float(np.mean(exg[mask] > BUILDING_THRESHOLDS['exg'])), 4)
    gray = crop.astype(np.float32) @ np.array([.299, .587, .114], dtype=np.float32)
    interior = mask.copy()
    interior[[0, -1], :] = False
    interior[:, [0, -1]] = False
    interior[1:-1, 1:-1] &= mask[:-2, 1:-1] & mask[2:, 1:-1] & mask[1:-1, :-2] & mask[1:-1, 2:]
    if interior.any():
        lap = np.zeros_like(gray)
        lap[1:-1, 1:-1] = 4*gray[1:-1, 1:-1]-gray[:-2, 1:-1]-gray[2:, 1:-1]-gray[1:-1, :-2]-gray[1:-1, 2:]
        features['texture'] = round(float(np.var(lap[interior])), 2)
    else:
        features['texture'] = 0.
    return features


def secondary_structure_features(key, obj, shape, scene, buildings, context):
    """Largest other roof on the same Vicmap property (inventory) or, failing that, within 25 m (proxy)."""
    area = float(shape.area)
    entry = (context.inventory or {}).get(key) if context else None
    pfi = str(entry['property_pfi']) if entry and entry.get('property_pfi') else None
    features = {'area_m2': round(area, 3), 'largest_same_property_area': None, 'largest_object': None, 'ratio': None,
                'source': None, 'property_pfi': pfi, 'same_property_buildings': None}
    if pfi and context.by_property.get(pfi):
        siblings = context.by_property[pfi]
        others = [(k, a) for k, a in siblings if k != key]
        features['same_property_buildings'] = len(siblings)
        features['source'] = 'vicmap-property'
        if others:
            largest_key, largest = max(others, key=lambda item: (item[1], item[0]))
            features.update(largest_same_property_area=round(largest, 3), largest_object=largest_key)
    else:
        features['source'] = 'proximity-25m'
        centroid = shape.centroid
        near = [(oid, s) for oid, s in buildings.items() if oid != obj['id'] and s is not None and not s.is_empty
                and s.centroid.distance(centroid) <= BUILDING_THRESHOLDS['proximity_m']]
        if near:
            largest_id, largest_shape = max(near, key=lambda item: (item[1].area, item[0]))
            features.update(largest_same_property_area=round(float(largest_shape.area), 3), largest_object=largest_id)
    if features['largest_same_property_area'] is not None and area > 0:
        features['ratio'] = round(features['largest_same_property_area']/area, 4)
    return features


def non_residential_features(key, shape, scene, context):
    entry = (context.inventory or {}).get(key) if context else None
    pois = []
    if context:
        for point, poi in context.pois_for(scene):
            distance = float(shape.distance(point))
            if distance <= BUILDING_THRESHOLDS['poi_m']:
                pois.append({'name': poi.get('name'), 'category': poi.get('category'), 'distance_m': round(distance, 2)})
    pois.sort(key=lambda p: (p['distance_m'], str(p['name'])))
    landuse = []
    status = context.landuse_status(scene) if context else 'unavailable'
    if context and status == 'available':
        landuse = [{'tag': f"{m['tag_key']}={m['tag_value']}", 'name': m['name'], 'distance_m': m['distance_m'], 'osm_id': m['osm_id']}
                   for m in context.landuse.near(shape, scene, within_m=BUILDING_THRESHOLDS['landuse_m'])]
    site_names = [str(n) for n in (entry or {}).get('site_names') or []]
    return {'poi_within_30m': pois, 'landuse': landuse, 'landuse_status': status,
            'address_kind': context.address_kind(entry) if context else None, 'site_names': site_names,
            'site_name_match': any(SITE_NAME_PATTERN.search(n.upper()) for n in site_names),
            'property_address_kind': (entry or {}).get('address_kind')}


def class_hint(flags, features):
    """Rule order: tree-like, commercial, non-residential, outbuilding, units, house. An agent hint, not a label."""
    area = features['secondary_structure'].get('area_m2') or 0
    nonres = features['non_residential']
    if 'tree-like' in flags:
        return 'tree-like'
    if area >= BUILDING_THRESHOLDS['commercial_area_m2'] and ('non-residential-context' in flags or nonres.get('address_kind') in COMMERCIAL_ADDRESS_KINDS):
        return 'commercial'
    if 'non-residential-context' in flags:
        return 'non-residential'
    if 'secondary-structure' in flags:
        return 'outbuilding'
    if nonres.get('address_kind') == 'units' or nonres.get('property_address_kind') == 'unit_address':
        return 'units'
    return 'house'


def building_hints(features):
    """Flags with evidence from the three feature groups; thresholds in BUILDING_THRESHOLDS, rules in RULES."""
    t, s, n = features['tree_like'], features['secondary_structure'], features['non_residential']
    flags = []
    green = t.get('exg_green_share')
    if green is not None and green >= BUILDING_THRESHOLDS['exg_green_share'] and (
            (t.get('solidity') is not None and t['solidity'] <= BUILDING_THRESHOLDS['solidity']) or (t.get('tree_overlap_iou') or 0) >= BUILDING_THRESHOLDS['tree_overlap_iou']):
        flags.append({'flag': 'tree-like', 'evidence': {**{k: t.get(k) for k in ('exg_green_share', 'solidity', 'tree_overlap_iou', 'texture', 'vertex_density')},
                                                        'rule': RULES['tree-like']}})
    # Same-property evidence (Vicmap) is stronger than the 25 m proximity proxy, so a lower size ratio suffices (v2).
    ratio_needed = BUILDING_THRESHOLDS['secondary_ratio_property'] if s.get('source') == 'vicmap-property' else BUILDING_THRESHOLDS['secondary_ratio']
    if s.get('ratio') is not None and s['area_m2'] < BUILDING_THRESHOLDS['secondary_area_m2'] and s['ratio'] >= ratio_needed:
        flags.append({'flag': 'secondary-structure', 'evidence': {**{k: s.get(k) for k in ('area_m2', 'largest_same_property_area', 'ratio', 'source', 'property_pfi', 'largest_object')},
                                                                  'rule': RULES['secondary-structure']}})
    if n['landuse'] or n['poi_within_30m'] or n.get('address_kind') in NON_RESIDENTIAL_ADDRESS_KINDS or n.get('site_name_match'):
        flags.append({'flag': 'non-residential-context', 'evidence': {**{k: n.get(k) for k in ('poi_within_30m', 'landuse', 'landuse_status', 'address_kind', 'site_names')},
                                                                      'area_m2': s.get('area_m2'), 'rule': RULES['non-residential-context']}})
    return flags


def _rgb_array(image):
    """An HxWx3 uint8-like array from a PIL image or any array (grey replicated, alpha dropped); None when unusable."""
    if image is None:
        return None
    if isinstance(image, Image.Image):
        return np.asarray(image.convert('RGB'))
    array = np.asarray(image)
    if array.ndim == 2:
        return np.stack([array]*3, axis=-1)
    if array.ndim == 3 and array.shape[2] >= 3:
        return array[..., :3]
    return None


def assess_buildings(scene, *, image=None, only_owned=True, context=None):
    """One building-flags-v2 row per live (owned) building; image may be None (tree-like then cannot fire)."""
    context = context if context is not None else Context()
    owned = _owned_ids(scene, only_owned)
    live = _live(scene)
    image = _rgb_array(image)
    buildings, trees = {}, []
    for obj in live:
        try:
            shape = local_polygon(obj, scene)
        except GEOMETRY_ERRORS:
            shape = None
        if obj['kind'] == 'building':
            buildings[obj['id']] = shape if shape is not None and not shape.is_empty and shape.area > 0 else None
        elif obj['kind'] == 'tree' and shape is not None and not shape.is_empty:
            trees.append(shape)
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    scope = 'owned_object_ids' if owned is not None else 'all_live'
    rows = []
    for obj in live:
        if obj['kind'] != 'building' or (owned is not None and obj['id'] not in owned):
            continue
        score = obj.get('score')
        payload = {'kind': 'building', 'label': obj.get('label'), 'score': score, 'support_tier': support_tier(score),
                   'support_tier_note': SUPPORT_TIER_NOTE, 'status': obj.get('status'), 'population_scope': scope,
                   'version': BUILDING_VERSION, 'code_sha256': code, 'computed_at': stamp, 'image_available': image is not None}
        shape = buildings.get(obj['id'])
        key = f"{scene['id']}/{obj['id']}"
        if shape is None:
            payload.update({'area_m2': None, 'error': 'invalid outline',
                            'class_hints': {'version': BUILDING_VERSION, 'note': HINT_NOTE, 'class_hint': None, 'flags': [], 'features': None}})
            rows.append(_row(scene, obj, payload, BUILDING_VERSION))
            continue
        features = {'tree_like': tree_like_features(obj, shape, scene, image, trees),
                    'secondary_structure': secondary_structure_features(key, obj, shape, scene, buildings, context),
                    'non_residential': non_residential_features(key, shape, scene, context)}
        flags = building_hints(features)
        payload.update({'area_m2': round(float(shape.area), 3),
                        'class_hints': {'version': BUILDING_VERSION, 'note': HINT_NOTE, 'class_hint': class_hint([f['flag'] for f in flags], features),
                                        'flags': flags, 'features': features}})
        rows.append(_row(scene, obj, payload, BUILDING_VERSION))
    return rows


def _building_payloads(root, scene_ids):
    stored = []
    if eyes_index.path(root).exists():
        with closing(eyes_index.connection(root, readonly=True)) as db:
            for scene_id in dict.fromkeys(scene_ids):
                stored.extend(eyes_index.rows(db, 'object_quality', scene_id=scene_id, version=BUILDING_VERSION))
    return [r for r in stored if isinstance(r.get('payload'), dict) and r['payload'].get('kind') == 'building']


def building_rates(root=Path('.'), scene_ids=()):
    """Per-flag counts over the stored building rows with denominators; agent hints, not labels or accuracy."""
    rows = _building_payloads(root, scene_ids)
    payloads = [r['payload'] for r in rows]
    hints = [p.get('class_hints') or {} for p in payloads]
    flag_names = ('tree-like', 'secondary-structure', 'non-residential-context')
    counts = {name: sum(1 for h in hints if any(f['flag'] == name for f in h.get('flags', []))) for name in flag_names}
    n = len(payloads)
    classes, sources, landuse = {}, {}, {}
    for h in hints:
        if h.get('class_hint'):
            classes[h['class_hint']] = classes.get(h['class_hint'], 0)+1
        features = h.get('features') or {}
        source = (features.get('secondary_structure') or {}).get('source')
        if source:
            sources[source] = sources.get(source, 0)+1
        status = (features.get('non_residential') or {}).get('landuse_status')
        if status:
            landuse[status] = landuse.get(status, 0)+1
    evidence, poi_categories, landuse_tags = {'denominator': counts['non-residential-context'], 'landuse': 0, 'poi_within_30m': 0, 'address_kind': 0, 'site_name': 0}, {}, {}
    for h in hints:
        for flag in h.get('flags', []):
            if flag['flag'] != 'non-residential-context':
                continue
            e = flag['evidence']
            evidence['landuse'] += bool(e.get('landuse'))
            evidence['poi_within_30m'] += bool(e.get('poi_within_30m'))
            evidence['address_kind'] += e.get('address_kind') in NON_RESIDENTIAL_ADDRESS_KINDS
            evidence['site_name'] += any(SITE_NAME_PATTERN.search(str(name).upper()) for name in e.get('site_names') or [])
            for poi in e.get('poi_within_30m') or []:
                poi_categories[str(poi.get('category'))] = poi_categories.get(str(poi.get('category')), 0)+1
            for match in e.get('landuse') or []:
                landuse_tags[match['tag']] = landuse_tags.get(match['tag'], 0)+1
    return {'version': BUILDING_VERSION, 'note': HINT_NOTE, 'scenes': len(set(r['scene_id'] for r in rows)), 'buildings': n,
            'with_image': sum(1 for p in payloads if p.get('image_available')), 'invalid_geometry': sum(1 for p in payloads if p.get('area_m2') is None),
            'flags': {name: _ratio(counts[name], n) for name in flag_names},
            'any_flag': _ratio(sum(1 for h in hints if h.get('flags')), n),
            'class_hints': dict(sorted(classes.items())), 'secondary_source': dict(sorted(sources.items())), 'landuse_status': dict(sorted(landuse.items())),
            'non_residential_evidence': evidence,
            'poi_categories': dict(sorted(poi_categories.items(), key=lambda kv: (-kv[1], kv[0]))[:25]),
            'landuse_tags': dict(sorted(landuse_tags.items(), key=lambda kv: (-kv[1], kv[0])))}


def examples(root=Path('.'), scene_ids=(), n=20, seed=0):
    """A seeded random sample of flagged buildings (scene, object, area, flags, hint, editor link) for rendering."""
    flagged = []
    for row in _building_payloads(root, scene_ids):
        hints = row['payload'].get('class_hints') or {}
        if hints.get('flags'):
            flagged.append({'scene_id': row['scene_id'], 'object_id': row['object_id'], 'area_m2': row['payload'].get('area_m2'),
                            'flags': [f['flag'] for f in hints['flags']], 'class_hint': hints.get('class_hint'),
                            'review_url': f"/scenes?scene={row['scene_id']}&object={row['object_id']}&kind=building&status=all&layer=model"})
    flagged.sort(key=lambda e: (e['scene_id'], e['object_id']))
    return random.Random(seed).sample(flagged, min(n, len(flagged)))


def _ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator, 'rate': round(numerator/denominator, 4) if denominator else None}


def rates(root=Path('.'), scene_ids=()):
    """Scene-set rates with explicit, null-aware denominators, read from the sidecar index (never recomputed).

    Objects without a measurable outline leave geometry denominators; objects without a detector score leave the
    support denominator. These are diagnostics, not accuracy.
    """
    scene_ids = list(dict.fromkeys(scene_ids))
    stored = []
    if eyes_index.path(root).exists():
        with closing(eyes_index.connection(root, readonly=True)) as db:
            for scene_id in scene_ids:
                stored.extend(eyes_index.rows(db, 'object_quality', scene_id=scene_id, version=VERSION))
    payloads = [(r['scene_id'], r['object_id'], r['payload']) for r in stored]
    measured = [p for _, _, p in payloads if p.get('area_m2') is not None]
    solar = [p for _, _, p in payloads if p['kind'] == 'solar_panel' and p.get('unparented') is not None]
    trees = [p for _, _, p in payloads if p['kind'] == 'tree' and p.get('support_tier') is not None]
    giants = {}
    for p in measured:
        for flag in p['plausibility_flags']:
            if flag.startswith('giant_'):
                giants[flag] = giants.get(flag, 0)+1
    pairs = set()
    for scene_id, object_id, p in payloads:
        if (p.get('same_class_overlap_max') or 0) > THRESHOLDS['heavy_same_class_overlap'] and p.get('same_class_overlap_partner'):
            pairs.add((scene_id, *sorted((object_id, p['same_class_overlap_partner']))))
    by_kind, by_scene = {}, {}
    for row, (_, _, p) in zip(stored, payloads, strict=True):
        by_kind[p['kind']] = by_kind.get(p['kind'], 0)+1
        entry = by_scene.setdefault(row['scene_id'], {'objects': 0, 'model_signatures': set(), 'population_scope': set()})
        entry['objects'] += 1
        entry['model_signatures'].add(row.get('model_signature'))
        entry['population_scope'].add(p.get('population_scope'))
    return {'version': VERSION, 'note': 'Detector support and plausibility diagnostics, not accuracy.',
            'population': {'scenes': len(scene_ids), 'objects': len(payloads), 'by_kind': dict(sorted(by_kind.items())),
                           'invalid_geometry': len(payloads)-len(measured),
                           'no_score': sum(1 for _, _, p in payloads if p.get('support_tier') is None)},
            'by_scene': {k: {'objects': v['objects'], 'model_signatures': sorted(v['model_signatures'], key=str),
                             'population_scope': sorted(v['population_scope'], key=str)} for k, v in sorted(by_scene.items())},
            'solar_unparented_rate': _ratio(sum(1 for p in solar if p['unparented']), len(solar)),
            'tree_low_score_share': _ratio(sum(1 for p in trees if p['support_tier'] == '<0.5'), len(trees)),
            'giant_object_counts': {'numerator': sum(giants.values()), 'denominator': len(measured), 'by_flag': dict(sorted(giants.items()))},
            'same_class_overlap_pairs': {'numerator': len(pairs), 'denominator': len(measured),
                                         'pairs': [list(pair) for pair in sorted(pairs)]},
            'building_flags': building_rates(root, scene_ids)}


def load_context_cached(root=Path('.'), scene_ids=()):
    """Context from on-disk caches only: cached inventory, address kinds, read-only POIs and the cached OSM land use.

    Nothing is built or fetched, so this is safe inside the inference pipeline and in tests; every missing
    source is recorded in the notes and simply yields fewer hints.
    """
    root = Path(root)
    notes = {}
    inventory = load_inventory_properties(root, notes=notes) if (root/INVENTORY_CACHE).exists() else None
    if inventory is None:
        notes.setdefault('inventory_error', 'no cached inventory')
    bounds = suburb_bounds(root)
    if bounds is None:
        boxes = []
        for scene_id in dict.fromkeys(scene_ids):
            try:
                boxes.append(scene_lonlat_bounds(json.loads((root/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if boxes:
            arr = np.array(boxes)
            bounds = (float(arr[:, 0].min()), float(arr[:, 1].min()), float(arr[:, 2].max()), float(arr[:, 3].max()))
    pois = load_pois(root, bounds) if bounds else []
    doc = context_landuse.load(root, bounds, fetch=None) if bounds else {'status': 'unavailable', 'features': []}
    return Context(inventory=inventory, address_kinds=load_address_kinds(root), pois=pois, landuse=context_landuse.Landuse(doc),
                   notes={**notes, 'mode': 'cached-only'}, coverage=bounds)


_CONTEXT_CACHE = {}


def class_hints_for_scene(scene, *, image=None, root=Path('.'), context=None, only_owned=False):
    """{'version', 'note', 'context', 'hints': {object_id: class_hint}} for the scene's live buildings.

    Agent hints (tree-like, commercial, non-residential, outbuilding, units, house), never labels. Without an
    explicit `context` the cached-only context for `root` is used, memoised per root and inventory-cache mtime.
    """
    if context is None:
        root = Path(root)
        cache = root/INVENTORY_CACHE
        key = (str(root.resolve()), cache.stat().st_mtime_ns if cache.exists() else None)
        if key not in _CONTEXT_CACHE:
            try:
                _CONTEXT_CACHE[key] = load_context_cached(root, (scene.get('id'),) if scene.get('id') else ())
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                _CONTEXT_CACHE[key] = Context(notes={'mode': 'empty', 'context_error': 'cached context unavailable'})
        context = _CONTEXT_CACHE[key]
    hints, payloads = {}, {}
    for row in assess_buildings(scene, image=image, only_owned=only_owned, context=context):
        payloads[row['object_id']] = row.get('payload') or {}
        hint = (payloads[row['object_id']].get('class_hints') or {}).get('class_hint')
        if hint:
            hints[row['object_id']] = hint
    return {'version': BUILDING_VERSION, 'note': HINT_NOTE, 'context': context.describe(), 'hints': hints, 'flag_payloads': payloads}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Object quality sidecars: non-building plausibility and, with --buildings, '
                                                 'building class hints (agent hints, not labels).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='+', required=True)
    parser.add_argument('--all-objects', action='store_true', help='Include objects outside owned_object_ids.')
    parser.add_argument('--rates', action='store_true')
    parser.add_argument('--buildings', action='store_true', help='Also write building-flags-v2 rows (class hints with evidence).')
    parser.add_argument('--offline', action='store_true', help='Never fetch OSM land use; report it unavailable.')
    parser.add_argument('--refresh-context', action='store_true', help='Rebuild the inventory-properties and land-use caches.')
    parser.add_argument('--examples', type=int, default=0, help='Random flagged building examples to list (seeded).')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--report', help='Also write the JSON output to this file (under data/reviews/eyes).')
    args = parser.parse_args(argv)
    root = Path(args.root)
    context = None
    if args.buildings:
        context = Context.load(root, args.scenes, offline=args.offline, refresh=args.refresh_context)
    out = {'version': VERSION, 'written': run(root, args.scenes, only_owned=not args.all_objects, buildings=args.buildings, context=context)}
    if args.buildings:
        out['building_version'] = BUILDING_VERSION
        out['context'] = context.describe()
    if args.rates:
        out['rates'] = rates(root, args.scenes)
    if args.examples:
        out['examples'] = examples(root, args.scenes, n=args.examples, seed=args.seed)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(out, indent=1))
    json.dump(out, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
