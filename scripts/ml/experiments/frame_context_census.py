"""Read-only frame-context census (frame-context-census-v1): what the 54 recapture windows still cut at their frame edge,
and what four framing rules would change, from geometry alone (no capture, no detector, no network).

REVIEWER (chat, 8 Oct 2026 ~09:15 AEDT), after reporting frame cuts on win-3dd37155248c04ef (1bac44c96bf45847 and
55ca4c5c041e3646: candidate roofs cut by the 512 px window's top edge, one with a spike at the edge): "i just want to avoid
cutting frames like it does, when its clearly not the entire permiter of the property etc".

Population: the windows named by data/reviews/eyes/recapture/windows-2026-10-01.jsonl (window_scene_id; 54). Per window the
REVIEW layer (data/scenes/<window>/review.json objects, the editor's edited layer): every live (status not superseded and not
rejected) building, tree or vegetation object whose ring reaches the frame by scene_focus.touched_edges (its 1 px tolerance;
one vertex within tolerance of an edge is enough).

Per cut object:
  class        subject (building only: a carried frame-cut target copy, a body_carry / takeover replacer of one, or the
               window's own building that covers the subject target ring or a planner body by half / IoU .3), same_lot
               (largest parcel share >= share_min .2 on the subject lot), neighbouring_lot (another parcel), no_parcel (parcel
               data covers the spot but no parcel holds .2 of it: road reserve, plan-block or oversize parcels), or
               parcels_unknown (no parcel data there). The subject lot is window_frame.lots; when the plan had none
               (reason no_parcels / plan_block_only) it is DERIVED now from the parcels on disk: the usable parcel holding the
               largest share (>= .2) of the subject geometry (target rings plus planner bodies), plus one numbered lot above .15.
  distance     from the window centre: centroid and nearest point (metres).
  whole copy   an object of the same kind group (building; tree/vegetation) in ANOTHER scene on disk (region-registered first:
               source scenes, overlapping windows, map-/house-/mooroolbark- frames; cohort fitted frames second, flagged)
               projected into the window's metres by scene_recapture.frame_transform (pure georeferenced affine, read-only)
               that covers >= .5 of the cut outline, agrees with it inside the frame (IoU >= .3) and does not itself touch its
               own frame (scene_focus.touching_frame, 1 px). Its overshoot beyond the frame (metres), share outside, and the
               half-span a centred window needs to hold it. Without a whole copy: unknown, with a LOWER bound from the union of
               the cut copies.

Rules simulated on the same windows (same centre as today; spans in the window's local metres):
  R0   today (window_frame.window).
  R1   always 1024 px at native z20: span = scene_focus.native_span_m(lat, 1024) (about 120.8 m).
  R2   grow-to-fit: grow the span until every building that intersects today's window and has a known whole copy lies inside
       with edge_margin_m (5 m), capped at max_span_m (120 m); frame px by scene_focus.frame_px_for. R2c (variant) repeats the
       test on buildings the grown frame newly meets (closure).
  R3   core + context ring: today's window is the core, plus a fixed ring (15 / 25 / 30 m) of extra capture; objects are
       judged by whether they intersect the core. Above 120.8 m a native-detail frame needs more than 1024 px
       (scene_region.region_frame accepts 512 / 1024 only): px_native and the m/px of a 1024 px frame are both recorded.
  R4   (extra, not asked) lot-fit: R2 plus the whole subject lot inside with 5 m margin, capped at 120 m.
  R7   (extra, not asked) property-fit re-centred, FIXED 2026-10-08 (frame-context-census-v2; see property_frame and
       .run/session-2026-10-08-frames/fix): v1 trusted a development assembled from the parcels on disk. For
       win-3dd37155248c04ef lot 6 of PS408840 (pfi 52589560) was on no frozen parcel payload at census time (it arrived with
       the pilot scratch scene's own parcel freeze), so v1 said 'development inside' and the captured pilot frame cut it at
       the top. v2: (a) the lot and development are COMPLETE only when frozen parcel payloads (their query bounds) cover them
       grown by 1 m (a parcel touching them anywhere would then be on disk); otherwise R7 is 'parcels_open' and its frame is
       a lower bound (fetch the parcels before capturing); (b) the property also holds every live object on it (review
       layer, cut or not, and other scenes' objects beyond today's frame whose largest parcel share is the lot or the
       development), not only the whole copies of cut ones; (c) above the 120 m cap the frame stays 120 m, centred on the
       property but shifted only as far as needed to keep the subject and its lot inside with 5 m; what still crosses the
       edge is reported (capped). The axis convention is unchanged and tested: window metres are x east, y north; cx, cy
       are in those metres (pixel y runs down, so the window's pixel_to_meters has a negative y scale).
       More frozen parcel payloads are read: data/reviews/eyes/units/parcels-*.json, data/reviews/eyes/parcels/*.json,
       data/roof-review-loop/runs/*/parcels-*.json and --extra-parcels DIR (pilot lookups).
Per rule: windows changed, frame px, cut objects that intersect the core (still cut / resolved / unknown), objects cut anywhere
(core objects plus objects of other scenes the new edge crosses; a lower bound where no registered imagery covers the new edge,
boundary coverage recorded), frame-cut subject objects remaining, subject lots wholly inside, and the imagery to fetch (z20
Google tiles over the frame bounds, already cached under data/imagery/google/20 or not).

Run (nice, read-only): scripts/ml/.venv/bin/python -m scripts.ml.experiments.frame_context_census [--only WIN ...] [--no-render]
Writes ONLY data/reviews/eyes/experiments/frame-context-2026-10-08/census/ (census.json, census.md, objects.jsonl,
renders/*.png) and .run/session-2026-10-08-frames/census/ (progress.txt, census.log). Never writes data/scenes, the windows
registry, DESIGN / RULES / WINDOW, src or docs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon, box, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from .. import scene_recapture as sr
from ..scene_addresses import WORLD
from ..scene_focus import (
    WINDOW,
    frame_polygon,
    frame_px_for,
    native_span_m,
    parcel_class,
    touched_edges,
    touching_frame,
)
from ..scene_lot import native_m_per_px
from ..scene_lot_tags import is_numbered, lot_text, registration
from ..tiles import lonlat_to_tile_xy

ROOT = Path('.')
OUT_DIR = Path('data/reviews/eyes/experiments/frame-context-2026-10-08/census')
RUN_DIR = Path('.run/session-2026-10-08-frames/census')
REGISTRY = Path('data/reviews/eyes/recapture/windows-2026-10-01.jsonl')
PARCELS_DIR = Path('data/scene-parcels')
TILE_DIR = Path('data/imagery/google/20')
COUNTER = Path('.run/session-2026-10-07-roofjoin/B/counter-after.json')
VERSION = 'frame-context-census-v2'
GROUP = {'building': 'building', 'tree': 'green', 'vegetation': 'green'}
DEAD = ('superseded', 'rejected')
TARGET_ROLES = ('frame_cut_target', 'whole_roof_body')
TOL_PX = WINDOW['frame_tolerance_px']
MARGIN_M = WINDOW['edge_margin_m']
CAP_M = WINDOW['max_span_m']
SHARE_MIN = WINDOW['share_min']
SECOND_SHARE = WINDOW['second_lot_share']
RINGS_M = (15., 25., 30.)
MATCH_COVER = .5
MATCH_IOU = .3
CLUSTER_OVERLAP = .5     # two copies are one object when their intersection holds half the smaller (partial copies of one roof)
SUBJECT_COVER = .5
SUBJECT_IOU = .3
AOI_EXTRA_M = 40.
ZOOM = 20
RULES = ('R0', 'R1', 'R2', 'R2c', 'R3_15', 'R3_25', 'R3_30', 'R4', 'R5', 'R6', 'R7')
RULE_TEXT = {
    'R0': 'today: window_frame.window as captured',
    'R1': 'always 1024 px at native z20, span native_span_m(lat, 1024) (~120.8 m), centred as today',
    'R2': 'grow-to-fit: span grows until every building meeting today\'s window with a known whole copy is inside with 5 m, cap 120 m',
    'R2c': 'R2 closure variant: also fits the whole-copy buildings the grown frame newly meets, repeated, cap 120 m',
    'R3_15': 'core + 15 m context ring (span + 30 m); judged on objects meeting the core',
    'R3_25': 'core + 25 m context ring (span + 50 m); judged on objects meeting the core',
    'R3_30': 'core + 30 m context ring (span + 60 m); judged on objects meeting the core',
    'R4': 'extra (not asked): R2 plus the whole subject lot inside with 5 m, cap 120 m',
    'R5': 'extra (not asked): R4 plus the whole subject development (every parcel of a subject plan that has common property) '
          'inside with 5 m, cap 120 m',
    'R6': 'extra (not asked): property-fit: the subject lot, its development and the whole copies of cut subject / same-lot / '
          'same-development objects (buildings and trees) inside with 5 m, cap 120 m; neighbours are not grown for',
    'R7': 'extra (not asked): R6 re-centred (v2, fixed 2026-10-08): when the property (subject, lot, development, every live object on '
          'them: review layer, whole copies of its cut objects, other scenes\' objects beyond today\'s frame) does not fit today\'s frame '
          'with 5 m, the frame is centred on the property bounding box with half its longer side + 5 m (at least today\'s span, cap 120 m; '
          'over the cap it stays 120 m and is shifted only as far as needed to keep the subject and its lot inside with 5 m); '
          'parcels_open when frozen parcel payloads do not cover the lot / development grown by 1 m (the frame is then a lower bound); '
          'objects the moved frame no longer meets are left_frame',
}
CLASSES = ('subject', 'same_lot', 'same_development', 'neighbouring_lot', 'no_parcel', 'parcels_unknown')
PROPERTY = ('subject', 'same_lot', 'same_development')
OPEN_TOL_M = 1.          # lot / development complete on disk only when frozen payload bounds cover it grown by this much
OPEN_MIN_M2 = .1         # uncovered area below this is float noise
EPS_M = 1e-6             # 'inside with 5 m' when the edge sits exactly 5 m out (a frame sized to the property): float tolerance
CP_PLAN_PREFIXES = ('PS', 'RP', 'SP')   # plans that carry common property (CM / CP lots) on disk; TP / LP / CP / PC never do
EXTRA_PARCEL_GLOBS = ('data/reviews/eyes/units/parcels-*.json', 'data/reviews/eyes/parcels/*.json',
                      'data/roof-review-loop/runs/*/parcels-*.json')   # more frozen Vicmap payloads ({'data': FeatureCollection})


def _now():
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _r(value, digits=2):
    return None if value is None else round(float(value), digits)


class Log:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg, beat=False):
        line = f'{_now()} {msg}'
        with open(self.run_dir/'census.log', 'a') as h:
            h.write(line+'\n')
        if beat:
            with open(self.run_dir/'progress.txt', 'a') as h:
                h.write(line+'\n')


# --- geometry ------------------------------------------------------------------------------------------------------

def valid(poly):
    if poly.is_empty:
        return poly
    if not poly.is_valid:
        poly = make_valid(poly)
        if poly.geom_type not in ('Polygon', 'MultiPolygon'):
            poly = unary_union([g for g in getattr(poly, 'geoms', ()) if g.geom_type in ('Polygon', 'MultiPolygon')])
    return poly


def ring_poly(ring, holes, affine):
    """A pixel ring (and holes) through a 3x2 affine as a valid shapely polygon (empty when degenerate)."""
    if not ring or len(ring) < 3:
        return Polygon()
    try:
        outer = sr.apply_affine([p[:2] for p in ring], affine)
        inner = [sr.apply_affine([p[:2] for p in h], affine) for h in holes or () if len(h) >= 3]
        return valid(Polygon(outer, inner))
    except (ValueError, TypeError, IndexError):
        return Polygon()


def lonlat_to_m(scene, lon, lat):
    """Vectorised scene_recapture.local_metres: lon/lat to a scene's local metres (x east, y north)."""
    lon0, lat0 = scene['centroid']['lon'], scene['centroid']['lat']
    scale = WORLD*math.cos(math.radians(lat0))
    y0 = math.asinh(math.tan(math.radians(lat0)))/(2*math.pi)
    lon, lat = np.asarray(lon, float), np.asarray(lat, float)
    return (lon-lon0)/360*scale, (np.arcsinh(np.tan(np.radians(lat)))/(2*math.pi)-y0)*scale


def m_to_lonlat(scene, x, y):
    lon0, lat0 = scene['centroid']['lon'], scene['centroid']['lat']
    scale = WORLD*math.cos(math.radians(lat0))
    y0 = math.asinh(math.tan(math.radians(lat0)))/(2*math.pi)
    x, y = np.asarray(x, float), np.asarray(y, float)
    return lon0+x/scale*360, np.degrees(np.arctan(np.sinh(2*math.pi*(y0+y/scale))))


def geo_to_m(scene, geo):
    """A WGS84 polygon (or multipolygon) in a scene's local metres."""
    def conv(coords):
        arr = np.asarray(coords, float)
        x, y = lonlat_to_m(scene, arr[:, 0], arr[:, 1])
        return np.c_[x, y]
    parts = list(geo.geoms) if geo.geom_type == 'MultiPolygon' else [geo]
    return valid(unary_union([Polygon(conv(p.exterior.coords), [conv(i.coords) for i in p.interiors]) for p in parts]))


def square(half):
    return box(-half, -half, half, half)


def half_extent(poly):
    minx, miny, maxx, maxy = poly.bounds
    return max(abs(minx), abs(miny), abs(maxx), abs(maxy))


def cut_by(poly, half, tol):
    """True when the polygon meets the square of this half-size and is not strictly inside it by more than tol."""
    if poly.is_empty or not poly.intersects(square(half)):
        return False
    return half_extent(poly) > half-tol


def frame_edges_beyond(poly, half):
    minx, miny, maxx, maxy = poly.bounds
    out = []
    for edge, beyond in (('left', -minx-half), ('top', maxy-half), ('right', maxx-half), ('bottom', -miny-half)):
        if beyond > 0:
            out.append(edge)
    return out


# --- scenes on disk ------------------------------------------------------------------------------------------------

def compact_objects(objects):
    out = []
    for o in objects or []:
        if not isinstance(o, dict) or o.get('kind') not in GROUP or o.get('status') in DEAD:
            continue
        ring = o.get('ring') or []
        if len(ring) < 3:
            continue
        out.append({'id': o.get('id'), 'kind': o['kind'], 'status': o.get('status'), 'ring': ring, 'holes': o.get('holes') or []})
    return out


def scan_scenes(root, aoi, log, extra_parcel_dirs=()):
    """One pass over data/scenes/*/latest.json (qa_* skipped): frames meeting the AOI with their live building / tree /
    vegetation objects (review layer when review.json exists, else latest.json; latest kept by id for planner bodies), and
    every parcel feature on disk by pfi with the payload bounds that say where parcel data is complete."""
    paths = sorted(p for p in (Path(root)/'data/scenes').glob('*/latest.json') if not p.parent.name.startswith('qa_'))
    scenes, parcels, payload_boxes = {}, {}, []
    stats = Counter()
    for i, path in enumerate(paths):
        if i % 60 == 0:
            log(f'scan {i}/{len(paths)} scenes kept {len(scenes)} parcels {len(parcels)}', beat=True)
        scene = _read(path)
        if not isinstance(scene, dict) or 'pixel_to_meters' not in scene or 'centroid' not in scene:
            stats['unreadable_or_frameless'] += 1
            continue
        payload = (scene.get('property_roofs') or {}).get('parcels') or {}
        feats = payload.get('features') or []
        if feats:
            add_parcels(payload, parcels, payload_boxes, aoi, f'scene:{scene.get("id")}')
        try:
            frame = frame_polygon(scene)
        except Exception:  # noqa: BLE001 - a frame we cannot place is skipped and counted
            frame = None
        if frame is None or not frame.intersects(aoi):
            stats['outside_aoi'] += 1
            continue
        reg = registration(scene)
        stats[f'kept_{reg}'] += 1
        review = _read(path.parent/'review.json')
        latest_objs = compact_objects(scene.get('objects'))
        if isinstance(review, dict) and isinstance(review.get('objects'), list):
            layer, layer_objs = 'review', compact_objects(review['objects'])
        else:
            layer, layer_objs = 'latest', latest_objs
        scenes[scene['id']] = {
            'id': scene['id'], 'centroid': scene['centroid'], 'pixel_to_meters': scene['pixel_to_meters'],
            'meters_to_pixel': scene['meters_to_pixel'], 'image_width': scene['image_width'], 'image_height': scene['image_height'],
            'image_sha256': scene.get('image_sha256'), 'model_signature': scene.get('model_signature'), 'region': scene.get('region'),
            'registration': reg, 'mpp': sr.pixel_spacing(scene), 'frame_geo': frame, 'layer': layer, 'layer_objs': layer_objs,
            'latest_by_id': {o['id']: o for o in latest_objs}, 'is_window': scene['id'].startswith(('win-', 'lot-'))}
    for path in sorted((Path(root)/PARCELS_DIR).glob('*.json')):
        saved = _read(path)
        data = saved.get('data') if isinstance(saved, dict) else None
        if isinstance(data, dict) and data.get('features'):
            add_parcels(data, parcels, payload_boxes, aoi, f'scene-parcels:{path.name[:12]}')
    extra = [p for pattern in EXTRA_PARCEL_GLOBS for p in sorted(Path(root).glob(pattern))]
    extra += [p for d in extra_parcel_dirs for p in sorted((Path(d) if Path(d).is_absolute() else Path(root)/d).glob('*.json'))]
    for path in extra:
        saved = _read(path)
        data = saved.get('data') if isinstance(saved, dict) else None
        if isinstance(data, dict) and data.get('features'):
            add_parcels(data, parcels, payload_boxes, aoi, f'extra:{path.parent.name[:16]}/{path.name[:16]}')
    log(f'scan done: {len(paths)} scenes, kept {len(scenes)} ({dict(stats)}); parcels {len(parcels)}; payload boxes {len(payload_boxes)}', beat=True)
    return scenes, parcels, payload_boxes, stats


def add_parcels(payload, parcels, payload_boxes, aoi, origin):
    bounds = payload.get('bounds')
    if bounds and len(bounds) == 4:
        b = box(*bounds)
        if b.intersects(aoi):
            payload_boxes.append(b)
    for feature in payload.get('features') or []:
        props = feature.get('properties') or {}
        pfi = str(props.get('pfi'))
        if not pfi or pfi in parcels or props.get('status', 'approved') != 'approved':
            continue
        try:
            geo = shape(feature['geometry'])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if geo.is_empty or not geo.intersects(aoi):
            continue
        parcels[pfi] = {'pfi': pfi, 'lot': lot_text(props), 'plan': props.get('plan'), 'geo': valid(geo), 'origin': origin}


# --- per window ----------------------------------------------------------------------------------------------------

def window_ids(registry):
    seen = []
    for line in Path(registry).open():
        row = json.loads(line)
        wid = row.get('window_scene_id')
        if wid and wid not in seen:
            seen.append(wid)
    return sorted(seen)


def aoi_half(h0, lat):
    return max(h0+max(RINGS_M), native_span_m(lat, 1024)/2, CAP_M/2)+AOI_EXTRA_M


def window_aoi(latest):
    mpp = sr.pixel_spacing(latest)
    h0 = latest['image_width']*mpp/2
    half = aoi_half(h0, latest['centroid']['lat'])
    lon, lat = m_to_lonlat(latest, [-half, half], [-half, half])
    return box(float(lon[0]), float(lat[0]), float(lon[1]), float(lat[1]))


def to_m_affine(source, target):
    """(S px -> target local metres affine, frame_transform record) through scene_recapture.frame_transform (pure)."""
    ft = sr.frame_transform(source, target)
    return sr.compose(ft['pixel_affine'], target['pixel_to_meters']), ft


def snapshot_object(root, scene_id, object_id, signature):
    folder = Path(root)/'data/scenes'/scene_id
    snap = _read(folder/'models'/f'{signature}.json') if signature else None
    if not isinstance(snap, dict):
        snap = _read(folder/'latest.json')
    if not isinstance(snap, dict):
        return None, None
    obj = next((o for o in snap.get('objects') or [] if o.get('id') == object_id), None)
    return snap, obj


def classify_parcels(poly_m, usable):
    """[(share, pfi)] of the polygon's area in each usable parcel, largest first."""
    if poly_m.is_empty or poly_m.area <= 0:
        return []
    shares = []
    for pfi, p in usable.items():
        if p['m'].intersects(poly_m):
            inter = p['m'].intersection(poly_m).area
            if inter > 0:
                shares.append((inter/poly_m.area, pfi))
    return sorted(shares, reverse=True)


def rule_px(span, lat):
    """(frame_px recorded, px for native detail, standard px at or finer than native, m/px at 1024)."""
    native = native_m_per_px(lat, ZOOM)
    px_native = math.ceil(span/native-1e-9)
    standard = 512 if span <= native_span_m(lat, 512)+1e-9 else 1024 if span <= native_span_m(lat, 1024)+1e-9 else 2048
    return px_native, standard, span/1024


def tiles_for(latest, half, cached, cx=0., cy=0.):
    lon, lat = m_to_lonlat(latest, [cx-half, cx+half], [cy-half, cy+half])
    x0, y1 = lonlat_to_tile_xy(float(lon[0]), float(lat[0]), ZOOM)
    x1, y0 = lonlat_to_tile_xy(float(lon[1]), float(lat[1]), ZOOM)
    tiles = {(x, y) for x in range(math.floor(x0), math.ceil(x1)) for y in range(math.floor(y0), math.ceil(y1))}
    return tiles, sum(1 for t in tiles if t in cached)


def analyse_window(root, wid, scenes, parcels, payload_boxes, cached_tiles, asked, log):
    latest = sr._scene(root, wid)
    review = sr._review(root, wid) or {'objects': []}
    wf = latest.get('window_frame') or {}
    lat = latest['centroid']['lat']
    mpp = sr.pixel_spacing(latest)
    w_px = int(latest['image_width'])
    h0 = w_px*mpp/2
    p2m = np.asarray(latest['pixel_to_meters'], float)
    checks = {'square': latest['image_width'] == latest['image_height'],
              'centred_axis_aligned': bool(abs(p2m[2, 0]+h0) < 1e-6 and abs(p2m[2, 1]-h0) < 1e-6 and p2m[0, 1] == 0 and p2m[1, 0] == 0)}
    ident = sr.frame_transform(latest, latest)['pixel_affine']
    checks['self_transform_identity'] = bool(np.allclose(np.asarray(ident), [[1, 0], [0, 1], [0, 0]], atol=1e-9))
    core = square(h0)
    aoi_h = aoi_half(h0, lat)
    aoi_m = square(aoi_h)
    native = native_m_per_px(lat, ZOOM)

    # other scenes near this window, in its metres
    near = []
    for sid, s in scenes.items():
        if sid == wid:
            continue
        frame_m = geo_to_m(latest, s['frame_geo'])
        if not frame_m.intersects(aoi_m):
            continue
        aff, ft = to_m_affine(s, latest)
        near.append({'scene': s, 'aff': aff, 'frame_m': frame_m, 'overlap_fraction': ft['overlap']['fraction']})
    region_frames_m = unary_union([n['frame_m'] for n in near if n['scene']['registration'] == 'region']+[core])

    copies = []
    for n in near:
        s = n['scene']
        for o in s['layer_objs']:
            poly = ring_poly(o['ring'], o['holes'], n['aff'])
            if poly.is_empty or poly.area <= 0 or not poly.intersects(aoi_m):
                continue
            own_touch = touching_frame(o['ring'], int(s['image_width']), int(s['image_height']))
            copies.append({'scene_id': s['id'], 'object_id': o['id'], 'kind': o['kind'], 'group': GROUP[o['kind']], 'status': o['status'],
                           'poly': poly, 'whole': not own_touch, 'own_touch': own_touch, 'registration': s['registration'],
                           'mpp': s['mpp'], 'layer': s['layer'], 'is_window': s['is_window']})
    tree = STRtree([c['poly'] for c in copies]) if copies else None

    # parcels in metres; usable = not plan_block / oversize
    usable, excluded = {}, {}
    for pfi, p in parcels.items():
        pm = geo_to_m(latest, p['geo'])
        if pm.is_empty or not pm.intersects(aoi_m):
            continue
        minx, miny, maxx, maxy = pm.bounds
        klass = parcel_class(p['lot'], p['plan'], max(maxx-minx, maxy-miny))
        entry = {**p, 'm': pm, 'class': klass}
        (excluded if klass in ('plan_block', 'oversize') else usable)[pfi] = entry
    coverage_m = unary_union([geo_to_m(latest, b) for b in payload_boxes if b.intersects(geo_m_box(latest, aoi_h))]) if payload_boxes else Polygon()

    # subject geometry: carried target copies (any status) + planner bodies, else the frozen source rings
    objs = review.get('objects') or []
    carried = {o['id']: o for o in objs if (o.get('carried_from') or {}).get('role') == 'frame_cut_target'}
    subjects = wf.get('subjects') or []
    frozen = {(s['scene_id'], s['object_id']) for s in subjects}
    target_m, body_m, subject_sources = [], [], []
    have = set()
    for o in carried.values():
        cf = o['carried_from']
        if (cf.get('scene_id'), cf.get('object_id')) in frozen:
            poly = ring_poly(o.get('ring'), o.get('holes'), p2m)
            if not poly.is_empty:
                target_m.append(poly)
                have.add((cf.get('scene_id'), cf.get('object_id')))
    for key in sorted(frozen-have):
        snap, obj = snapshot_object(root, key[0], key[1], wf.get('source_model_signature') if key[0] == wf.get('source_scene_id') else None)
        if snap and obj:
            aff, _ = to_m_affine(snap, latest)
            poly = ring_poly(obj.get('ring'), obj.get('holes'), aff)
            if not poly.is_empty:
                target_m.append(poly)
                subject_sources.append(f'{key[0]}/{key[1]} from snapshot')
    for s in subjects:
        for b in s.get('bodies') or []:
            src = scenes.get(b['scene_id'])
            obj = (src or {}).get('latest_by_id', {}).get(b['object_id']) or next((o for o in (src or {}).get('layer_objs', []) if o['id'] == b['object_id']), None)
            if src and obj:
                aff, _ = to_m_affine(src, latest)
                poly = ring_poly(obj['ring'], obj['holes'], aff)
                if not poly.is_empty:
                    body_m.append(poly)
    subject_geo = valid(unary_union(target_m+body_m)) if (target_m or body_m) else Polygon()

    # subject lot
    lots_wf = [lot.get('pfi') for lot in wf.get('lots') or []]
    if lots_wf:
        subject_lots, lot_source = [p for p in lots_wf if p in usable], 'window_frame'
        missing_lot_geo = [p for p in lots_wf if p not in usable]
    else:
        shares = classify_parcels(subject_geo, usable) if not subject_geo.is_empty else []
        subject_lots, missing_lot_geo = [], []
        if shares and shares[0][0] >= SHARE_MIN:
            subject_lots.append(shares[0][1])
            second = [pfi for share, pfi in shares[1:] if share > SECOND_SHARE and is_numbered(usable[pfi]['lot'])]
            subject_lots += second[:1]
        lot_source = 'derived_from_parcels_on_disk' if subject_lots else 'none'
    lot_geo = valid(unary_union([usable[p]['m'] for p in subject_lots])) if subject_lots else Polygon()
    every = {**usable, **excluded}
    subject_plans = {usable[p]['plan'] for p in subject_lots if usable[p]['plan']}
    cp_plans = {plan for plan in subject_plans if any(q['plan'] == plan and str(q['lot'] or '').upper().startswith(('CM', 'CP')) for q in every.values())}
    dev_plans, large_dev = set(), {}
    for plan in sorted(cp_plans):
        geo = valid(unary_union([q['m'] for q in every.values() if q['plan'] == plan and q['class'] not in ('plan_block', 'oversize')]))
        minx, miny, maxx, maxy = geo.bounds
        if max(maxx-minx, maxy-miny) <= CAP_M:
            dev_plans.add(plan)
        else:
            large_dev[plan] = _r(max(maxx-minx, maxy-miny), 1)   # an estate wider than the cap: its sibling lots stay neighbours
    dev_parcels = [q for q in every.values() if q['plan'] in dev_plans and q['class'] not in ('plan_block', 'oversize')]
    dev_geo = valid(unary_union([q['m'] for q in dev_parcels])) if dev_parcels else Polygon()

    source_ids = {wf.get('source_scene_id')} | {s.get('scene_id') for s in subjects}
    body_scene_ids = {b.get('scene_id') for s in subjects for b in s.get('bodies') or []}
    # the review-layer population (editor_only: the editor's touchesFrame says cut, touched_edges does not)
    rows = []
    editor_only = 0
    subject_live, subject_edge_margin = Counter(), []
    for o in objs:
        if o.get('kind') in GROUP and o.get('status') not in DEAD and len(o.get('ring') or []) >= 3:
            if editor_cut(o['ring'], w_px, int(latest['image_height'])) and not touched_edges(o['ring'], latest):
                editor_only += 1
            if o['kind'] == 'building':
                live_poly = ring_poly(o['ring'], o.get('holes'), p2m)
                if not live_poly.is_empty and live_poly.area > 0:
                    st = subject_subtype(o, live_poly, carried, target_m, body_m)
                    if st:
                        subject_live[st] += 1
                        subject_edge_margin.append(round(h0-half_extent(live_poly), 2))
        if o.get('kind') not in GROUP or o.get('status') in DEAD or len(o.get('ring') or []) < 3:
            continue
        edges = touched_edges(o['ring'], latest)
        if not edges:
            continue
        poly = ring_poly(o['ring'], o.get('holes'), p2m)
        if poly.is_empty or poly.area <= 0:
            continue
        cf = o.get('carried_from') or {}
        subtype = subject_subtype(o, poly, carried, target_m, body_m)
        shares = classify_parcels(poly, usable)
        best_share, best_pfi = shares[0] if shares else (0., None)
        if subtype:
            klass = 'subject'
        elif best_share >= SHARE_MIN:
            klass = ('same_lot' if best_pfi in subject_lots else 'same_development' if usable[best_pfi]['plan'] in dev_plans
                     else 'neighbouring_lot')
        else:
            on_excluded = any(e['m'].intersects(poly) and e['m'].intersection(poly).area/poly.area >= SHARE_MIN for e in excluded.values())
            klass = 'no_parcel' if (on_excluded or (not coverage_m.is_empty and coverage_m.covers(poly.representative_point()))) else 'parcels_unknown'
        centroid = poly.centroid
        row = {'window': wid, 'id': o['id'], 'kind': o['kind'], 'group': GROUP[o['kind']], 'status': o.get('status'),
               'geometry_reviewed': bool(o.get('geometry_reviewed')), 'source': o.get('source'), 'edges': sorted(edges),
               'edge_vertices': sum(len(v) for v in edges.values()),
               'class': klass, 'subject_subtype': subtype, 'asked_2026_10_07': (wid, o['id']) in asked,
               'carried_from': [cf.get('scene_id'), cf.get('object_id')] if cf else None,
               'parcel': None if best_pfi is None else {'pfi': best_pfi, 'lot': usable[best_pfi]['lot'], 'plan': usable[best_pfi]['plan'],
                                                        'share': _r(best_share, 3), 'class': usable[best_pfi]['class']},
               'same_plan_as_subject': bool(best_pfi and best_share >= SHARE_MIN and usable[best_pfi]['plan'] in subject_plans),
               'area_in_frame_m2': _r(poly.area, 1), 'dist_centroid_m': _r(math.hypot(centroid.x, centroid.y), 1),
               'dist_nearest_m': _r(poly.distance(Point(0, 0)), 1),
               '_poly': poly}
        whole_copy(row, poly, copies, tree, h0, core)
        if row['whole_copy']:
            sid = row['whole_copy']['scene_id']
            row['whole_copy']['origin'] = ('window_source' if sid in source_ids else 'planner_body_scene' if sid in body_scene_ids
                                           else 'other_window' if sid.startswith(('win-', 'lot-')) else f"other_{row['whole_copy']['registration']}")
        rows.append(row)

    # objects of other scenes beyond today's frame (for the 'anywhere' counts and the R2 closure)
    outside = outside_clusters(copies, core)
    # diagnostic: whole region-registered building copies the core edge cuts that match no cut building of the review layer
    tol0 = mpp*TOL_PX
    bpolys = [r['_poly'] for r in rows if r['group'] == 'building']
    unmatched = cluster([c for c in copies if c['group'] == 'building' and c['whole'] and c['registration'] == 'region'
                         and cut_by(c['poly'], h0, tol0) and not any(c['poly'].intersects(b) for b in bpolys)
                         and c['poly'].intersection(core).area >= 2.])

    # rule frames
    native1024 = native_span_m(lat, 1024)
    rules = {'R0': {'half': h0, 'frame_px': w_px}}
    rules['R1'] = {'half': native1024/2, 'frame_px': 1024}
    need = h0
    for row in rows:
        if row['group'] == 'building' and row['whole_copy']:
            need = max(need, row['whole_copy']['half_extent_m']+MARGIN_M)
    r2_half = max(h0, min(CAP_M/2, need))
    rules['R2'] = {'half': r2_half, 'frame_px': frame_px_for(2*r2_half, lat), 'need_half_m': _r(need)}
    half = r2_half
    for _ in range(20):
        grow = half
        for c in outside:
            if c['group'] == 'building' and c['whole'] and c['poly'].intersects(square(half)):
                grow = max(grow, half_extent(c['poly'])+MARGIN_M)
        grow = max(h0, min(CAP_M/2, grow))
        if grow <= half+1e-6:
            break
        half = grow
    rules['R2c'] = {'half': half, 'frame_px': frame_px_for(2*half, lat)}
    for ring_m in RINGS_M:
        rules[f'R3_{int(ring_m)}'] = {'half': h0+ring_m, 'frame_px': None}
    lot_need = need
    if not lot_geo.is_empty:
        lot_need = max(lot_need, half_extent(lot_geo)+MARGIN_M)
    r4_half = max(h0, min(CAP_M/2, lot_need))
    rules['R4'] = {'half': r4_half, 'frame_px': frame_px_for(2*r4_half, lat), 'lot_need_half_m': _r(lot_need)}
    prop_need = h0
    for row in rows:
        if row['class'] in ('subject', 'same_lot', 'same_development') and row['whole_copy']:
            prop_need = max(prop_need, row['whole_copy']['half_extent_m']+MARGIN_M)
    for geo in (lot_geo, dev_geo):
        if not geo.is_empty:
            prop_need = max(prop_need, half_extent(geo)+MARGIN_M)
    r6_half = max(h0, min(CAP_M/2, prop_need))
    rules['R6'] = {'half': r6_half, 'frame_px': frame_px_for(2*r6_half, lat), 'property_need_half_m': _r(prop_need)}
    # R7 v2: the property is the subject, its lot, its development and every live object on them; complete only when frozen
    # parcel payloads cover the lot and development grown by OPEN_TOL_M
    lot_open, dev_open = open_area(lot_geo, coverage_m), open_area(dev_geo, coverage_m)

    def on_property(poly, subtype=None):
        if subtype:
            return True
        shares = classify_parcels(poly, usable)
        if not shares or shares[0][0] < SHARE_MIN:
            return False
        return shares[0][1] in subject_lots or usable[shares[0][1]]['plan'] in dev_plans

    prop_objs = [{'id': row['id'], 'group': row['group'], 'source': 'cut_row', 'whole': row['_whole'] is not None,
                  'poly': row['_whole'] if row['_whole'] is not None else row['_lower']} for row in rows if row['class'] in PROPERTY]
    cut_ids = {row['id'] for row in rows}
    for o in objs:
        if (o.get('kind') not in GROUP or o.get('status') in DEAD or len(o.get('ring') or []) < 3 or o.get('id') in cut_ids):
            continue
        poly = ring_poly(o['ring'], o.get('holes'), p2m)
        if not poly.is_empty and poly.area > 0 and on_property(poly, subject_subtype(o, poly, carried, target_m, body_m)):
            prop_objs.append({'id': o['id'], 'group': GROUP[o['kind']], 'source': 'review_layer', 'whole': True, 'poly': poly})
    for c in outside:
        if on_property(c['poly']):
            prop_objs.append({'id': c['members'][0], 'group': c['group'], 'source': 'other_scene', 'whole': c['whole'], 'poly': c['poly']})
    own_geos = [g for g in (subject_geo, lot_geo, dev_geo) if not g.is_empty]+[p['poly'] for p in prop_objs if not p['poly'].is_empty]
    prop = valid(unary_union(own_geos)) if own_geos else Polygon()
    keep = valid(unary_union([g for g in (subject_geo, lot_geo) if not g.is_empty])) if not (subject_geo.is_empty and lot_geo.is_empty) else Polygon()
    r7 = property_frame(prop, h0, keep)
    land = [g for g in (subject_geo, lot_geo, dev_geo) if not g.is_empty]
    land_bounds = valid(unary_union(land)).bounds if land else None
    beyond = []
    if land_bounds:
        lx0, ly0, lx1, ly1 = land_bounds
        for p in prop_objs:
            if p['poly'].is_empty:
                continue
            px0, py0, px1, py1 = p['poly'].bounds
            over = max(lx0-px0, py1-ly1, px1-lx1, ly0-py0)
            if over > .01:
                beyond.append({'id': p['id'], 'group': p['group'], 'source': p['source'], 'whole': p['whole'], 'beyond_land_m': _r(over)})
    beyond.sort(key=lambda b: -b['beyond_land_m'])
    # a lot alone is whole (Vicmap returns it whole); it matters only while it could still belong to a development whose common
    # property is not on disk: a PS / RP / SP plan with no development recognised (and none wider than the cap)
    lot_may_join = dev_geo.is_empty and not large_dev and any(str(p).upper().startswith(CP_PLAN_PREFIXES) for p in subject_plans)
    complete = dev_open < OPEN_MIN_M2 and (lot_open < OPEN_MIN_M2 or not lot_may_join)
    r7_px = w_px if not r7['recentred'] else frame_px_for(2*r7['half'], lat)
    rules['R7'] = {**r7, 'frame_px': r7_px, 'property_complete': complete,
                   'status': ('no_lot' if lot_geo.is_empty else 'parcels_open' if not complete else 'unchanged' if not r7['recentred']
                              else 'capped' if r7['capped'] else 'recentred'),
                   'lot_open_m2': _r(lot_open, 1), 'lot_may_join_development': lot_may_join, 'development_open_m2': _r(dev_open, 1),
                   'property_objects': dict(Counter(p['source'] for p in prop_objs)),
                   'property_objects_extent_unknown': sum(1 for p in prop_objs if not p['whole']),
                   'land_need_half_m': None if not land_bounds else _r(max(land_bounds[2]-land_bounds[0], land_bounds[3]-land_bounds[1])/2+MARGIN_M),
                   'objects_beyond_land': len(beyond), 'objects_beyond_land_top': beyond[:3]}
    dev_need = lot_need if dev_geo.is_empty else max(lot_need, half_extent(dev_geo)+MARGIN_M)
    r5_half = max(h0, min(CAP_M/2, dev_need))
    rules['R5'] = {'half': r5_half, 'frame_px': frame_px_for(2*r5_half, lat), 'development_need_half_m': _r(dev_need)}

    tile_area = (256*native)**2
    r0_tiles, _ = tiles_for(latest, h0, cached_tiles)
    per_rule = {}
    for name, rule in rules.items():
        span = 2*rule['half']
        cx, cy = rule.get('cx', 0.), rule.get('cy', 0.)

        def at(geo, cx=cx, cy=cy):
            return geo if (cx == 0. and cy == 0.) or geo.is_empty else translate(geo, -cx, -cy)
        px_native, standard, mpp1024 = rule_px(span, lat)
        frame_px = rule['frame_px'] if rule['frame_px'] is not None else standard
        tol = span/frame_px*TOL_PX
        status = Counter()
        for row in rows:
            st = object_status(row, rule['half'], tol, name, at)
            row.setdefault('rules', {})[name] = st
            status[(row['group'], row['class'], st)] += 1
        out_known = out_possible = 0
        out_ids = []
        if name != 'R0':
            for c in outside:
                cp = at(c['poly'])
                if cut_by(cp, rule['half'], tol):
                    if c['whole'] or half_extent(cp) > rule['half']-tol:
                        out_known += 1
                        out_ids.append((c['group'], c['members'][0]))
                elif not c['whole'] and cp.intersects(square(rule['half'])):
                    out_possible += 1
        hh = rule['half']
        boundary = LineString([(cx-hh, cy-hh), (cx+hh, cy-hh), (cx+hh, cy+hh), (cx-hh, cy+hh), (cx-hh, cy-hh)])
        cover = boundary.intersection(region_frames_m).length/boundary.length
        tiles, cached_n = tiles_for(latest, rule['half'], cached_tiles, cx, cy)
        per_rule[name] = {
            'span_m': _r(span), 'frame_px': frame_px, 'px_native': px_native, 'standard_px': standard,
            'm_per_px': _r(span/frame_px, 4), 'm_per_px_at_1024': _r(mpp1024, 4), 'needs_over_1024': standard > 1024,
            'changed': abs(span-2*h0) > .01 or frame_px != w_px or abs(cx) > .01 or abs(cy) > .01,
            'centre_offset_m': _r(math.hypot(cx, cy)),
            'subject_inside_5m': None if subject_geo.is_empty else bool(at(subject_geo).within(square(rule['half']-MARGIN_M+EPS_M))),
            'core_status': {f'{g}|{k}|{s}': n for (g, k, s), n in sorted(status.items())},
            'outside_cut_known': out_known, 'outside_cut_possible': out_possible,
            'outside_cut_by_group': dict(Counter(g for g, _ in out_ids)),
            'boundary_registered_cover': _r(cover, 3),
            'subject_lot_inside': None if lot_geo.is_empty else bool(at(lot_geo).within(square(rule['half']))),
            'subject_lot_inside_margin': None if lot_geo.is_empty else bool(at(lot_geo).within(square(rule['half']-MARGIN_M+EPS_M))),
            'development_inside': None if dev_geo.is_empty else bool(at(dev_geo).within(square(rule['half']))),
            'development_inside_margin': None if dev_geo.is_empty else bool(at(dev_geo).within(square(rule['half']-MARGIN_M+EPS_M))),
            'property_inside': None if prop.is_empty else bool(at(prop).within(square(rule['half']))),
            'property_objects_cut': sum(1 for p in prop_objs if p['whole'] and cut_by(at(p['poly']), rule['half'], tol)),
            'property_objects_maybe_cut': sum(1 for p in prop_objs if not p['whole'] and at(p['poly']).intersects(square(rule['half']))
                                              and not at(p['poly']).within(square(rule['half']-tol))),
            'area_m2': _r(span*span, 0), 'extra_area_m2': _r(span*span-(2*h0)**2, 0),
            'tiles': len(tiles), 'tiles_cached': cached_n, 'tiles_to_fetch': len(tiles)-cached_n,
            'fetch_area_m2': _r((len(tiles)-cached_n)*tile_area, 0), 'new_tiles_vs_r0': len(tiles-r0_tiles),
            **{k: (_r(v) if k in ('cx', 'cy') else v) for k, v in rule.items() if k not in ('half', 'frame_px')}}

    window = {
        'window': wid, 'rule': (wf.get('window') or {}).get('rule'), 'reasons': sorted({str(s.get('reason')) for s in subjects}),
        'span_m': _r(2*h0, 3), 'frame_px': w_px, 'm_per_px': _r(mpp, 4), 'lat': lat, 'lon': latest['centroid']['lon'],
        'flags': wf.get('flags') or [], 'source_scene_id': wf.get('source_scene_id'), 'subjects': len(subjects),
        'subject_targets_found': len(target_m), 'subject_bodies_found': len(body_m), 'subject_sources_from_snapshot': subject_sources,
        'subject_lot_source': lot_source, 'subject_lots': [{'pfi': p, 'lot': usable[p]['lot'], 'plan': usable[p]['plan'], 'class': usable[p]['class']} for p in subject_lots],
        'subject_lot_geometry_missing': missing_lot_geo, 'subject_plans': sorted(subject_plans), 'development_plans': sorted(dev_plans),
        'development_plans_wider_than_cap': large_dev,
        'development_parcels': len(dev_parcels), 'development_half_extent_m': None if dev_geo.is_empty else _r(half_extent(dev_geo)),
        'subject_lot_half_extent_m': None if lot_geo.is_empty else _r(half_extent(lot_geo)),
        'near_scenes': len(near), 'near_region': sum(1 for n in near if n['scene']['registration'] == 'region'),
        'near_fitted': sum(1 for n in near if n['scene']['registration'] != 'region'), 'copies_considered': len(copies),
        'parcels_usable': len(usable), 'parcels_excluded': len(excluded), 'outside_clusters': len(outside),
        'core_cut_buildings_elsewhere_not_in_review': len(unmatched),
        'subject_buildings_live': dict(subject_live), 'subject_min_edge_margin_m': min(subject_edge_margin) if subject_edge_margin else None, 'editor_cut_not_touched_edges': editor_only,
        'cut_objects': len(rows), 'cut_by_group_class': dict(Counter(f"{r['group']}|{r['class']}" for r in rows)),
        'checks': checks, 'rules': per_rule}
    window['parcels_complete'] = {'lot_open_m2': _r(lot_open, 1), 'lot_may_join_development': lot_may_join,
                                  'development_open_m2': _r(dev_open, 1), 'complete': complete}
    return window, rows, {'latest': latest, 'near': near, 'rows': rows, 'rules': rules, 'lot_geo': lot_geo, 'dev_geo': dev_geo, 'outside': outside,
                          'prop': prop, 'prop_objs': prop_objs, 'keep': keep, 'coverage': coverage_m,
                          'subject_geo': subject_geo}


def open_area(geo, coverage, tol=OPEN_TOL_M):
    """m2 of geo grown by tol that no frozen parcel payload's query bounds cover. Vicmap returns every parcel that meets the
    query box whole, so when this is ~0 every parcel touching geo is on disk (a lot / development assembled from parcels on
    disk is complete); otherwise a sibling lot or common property can be missing (win-3dd37155248c04ef: lot 6, v1)."""
    if geo.is_empty:
        return 0.
    grown = geo.buffer(tol)
    return float(grown.area if coverage is None or coverage.is_empty else grown.difference(coverage).area)


def _clamp_centre(c, lo_edge, hi_edge, half, margin):
    """Shift a frame centre along one axis only as far as needed to keep [lo_edge, hi_edge] inside with margin (when it fits)."""
    lo, hi = hi_edge+margin-half, lo_edge-margin+half
    return min(max(c, lo), hi) if lo <= hi else (lo_edge+hi_edge)/2


def property_frame(prop, h0, keep=None, margin=MARGIN_M, cap=CAP_M):
    """R7 frame for a property geometry in the window's local metres (x east, y north, origin at the window centre).
    Unchanged (today's centred frame) when the property fits it with margin; else centred on the property bounding box with
    half = max(today's half, longer side / 2 + margin), capped at cap / 2. cx, cy are window metres (y north): a property north
    of the window gives cy > 0, and m_to_lonlat(latest, cx, cy) puts the new centre north. Over the cap the frame stays
    cap wide and is shifted only as far as needed to keep `keep` (subject + lot) inside with margin; the rest of the property
    crosses the edge (capped True, property_fits False)."""
    if prop.is_empty or half_extent(prop)+margin <= h0:
        return {'half': h0, 'cx': 0., 'cy': 0., 'recentred': False, 'property_fits': True, 'capped': False,
                'property_need_half_m': None if prop.is_empty else _r(half_extent(prop)+margin)}
    minx, miny, maxx, maxy = prop.bounds
    need = max(maxx-minx, maxy-miny)/2+margin
    half = max(h0, min(cap/2, need))
    cx, cy = (minx+maxx)/2, (miny+maxy)/2
    capped = need > cap/2+1e-9
    if capped and keep is not None and not keep.is_empty:
        kminx, kminy, kmaxx, kmaxy = keep.bounds
        cx, cy = _clamp_centre(cx, kminx, kmaxx, half, margin), _clamp_centre(cy, kminy, kmaxy, half, margin)
    return {'half': half, 'cx': cx, 'cy': cy, 'recentred': True, 'property_fits': not capped, 'capped': capped,
            'property_need_half_m': _r(need)}


def subject_subtype(o, poly, carried, target_m, body_m):
    """Why a review-layer building is the subject: its carried target copy, a body carry, a takeover replacer of a carried copy, or
    the window's own detection covering a target ring / planner body (half of either, or IoU .3); None otherwise."""
    if o.get('kind') != 'building':
        return None
    cf = o.get('carried_from') or {}
    if cf.get('role') in TARGET_ROLES or o.get('source') == 'carried-body-v1':
        return 'target_copy' if cf.get('role') == 'frame_cut_target' else 'body_carry'
    if set(o.get('replaces') or []) & set(carried):
        return 'takeover_replacer'
    for label, geos in (('target_detection', target_m), ('planner_body', body_m)):
        for g in geos:
            inter = g.intersection(poly).area
            if inter > 0 and (inter/poly.area >= SUBJECT_COVER or inter/g.area >= SUBJECT_COVER or inter/(g.area+poly.area-inter) >= SUBJECT_IOU):
                return label
    return None


def editor_cut(ring, width, height, tol=TOL_PX):
    """src/lib/outline-hints.ts touchesFrame: any vertex whose edge distance (negative beyond the frame) is <= tol px."""
    return any(min(float(p[0]), float(p[1]), width-float(p[0]), height-float(p[1])) <= tol for p in ring)


def geo_m_box(latest, half):
    lon, lat = m_to_lonlat(latest, [-half, half], [-half, half])
    return box(float(lon[0]), float(lat[0]), float(lon[1]), float(lat[1]))


def whole_copy(row, poly, copies, tree, h0, core):
    """The best whole copy of a cut object in another scene (region before fitted, then IoU in frame, then finer m/px) and the
    lower-bound extent from the union of every matching copy."""
    matches = []
    if tree is not None:
        for idx in tree.query(poly):
            c = copies[int(idx)]
            if c['group'] != row['group']:
                continue
            inter = c['poly'].intersection(poly).area
            if inter <= 0:
                continue
            cover = inter/poly.area
            c_in = c['poly'].intersection(core)
            union = c_in.area+poly.area-inter
            iou_in = inter/union if union > 0 else 0.
            if cover >= MATCH_COVER and iou_in >= MATCH_IOU:
                matches.append((c, cover, iou_in))
    wholes = sorted((m for m in matches if m[0]['whole']), key=lambda m: (m[0]['registration'] != 'region', -m[2], m[0]['mpp']))
    row['copies_matched'] = len(matches)
    row['copies_whole'] = len(wholes)
    row['copies_scenes'] = sorted({m[0]['scene_id'] for m in matches})
    lower = max([poly]+[m[0]['poly'] for m in matches], key=half_extent)
    row['lower_bound_half_extent_m'] = _r(half_extent(lower))
    row['lower_bound_overshoot_m'] = _r(max(0., half_extent(lower)-h0))
    row['lower_bound_from'] = None if lower is poly else next(f"{m[0]['scene_id']}/{m[0]['object_id']}" for m in matches if m[0]['poly'] is lower)
    row['_lower'] = lower
    if not wholes:
        row['whole_copy'] = None
        row['_whole'] = None
        return
    c, cover, iou_in = wholes[0]
    he = half_extent(c['poly'])
    inside = c['poly'].intersection(core).area
    row['whole_copy'] = {'scene_id': c['scene_id'], 'object_id': c['object_id'], 'registration': c['registration'], 'layer': c['layer'],
                         'status': c['status'], 'm_per_px': _r(c['mpp'], 4), 'cover_of_cut': _r(cover, 3), 'iou_in_frame': _r(iou_in, 3),
                         'area_m2': _r(c['poly'].area, 1), 'half_extent_m': _r(he), 'overshoot_m': _r(max(0., he-h0)),
                         'outside_share': _r(1-inside/c['poly'].area, 3), 'edges_beyond': frame_edges_beyond(c['poly'], h0),
                         'span_needed_m': _r(2*he), 'other_whole_copies': len(wholes)-1}
    row['_whole'] = c['poly']


def object_status(row, half, tol, name, at=lambda g: g):
    """still_cut / resolved / unknown for a cut object under a frame of this half-size (centred by `at`); 'left_frame' when a
    re-centred frame no longer meets the object at all."""
    if name == 'R0':
        return 'still_cut'
    if not at(row['_poly']).intersects(square(half)) and (row['_whole'] is None or not at(row['_whole']).intersects(square(half))):
        return 'left_frame'
    if row['_whole'] is not None:
        return 'still_cut' if cut_by(at(row['_whole']), half, tol) else 'resolved'
    if half_extent(at(row['_lower'])) > half-tol:
        return 'still_cut'
    if cut_by(at(row['_poly']), half, tol):
        return 'still_cut'
    return 'unknown'


def outside_clusters(copies, core):
    """Copies wholly outside today's frame, deduplicated by kind group and IoU >= .3; a cluster's outline is its largest whole
    copy, else the union of its cut copies (flag whole False: its true extent is unknown beyond its own frame)."""
    return cluster([c for c in copies if not c['poly'].intersects(core)])


def cluster(pool):
    pool = sorted(pool, key=lambda c: -c['poly'].area)
    if not pool:
        return []
    tree = STRtree([c['poly'] for c in pool])
    taken, clusters = set(), []
    for i, c in enumerate(pool):
        if i in taken:
            continue
        members = [i]
        taken.add(i)
        for j in tree.query(c['poly']):
            j = int(j)
            if j in taken or pool[j]['group'] != c['group']:
                continue
            inter = pool[j]['poly'].intersection(c['poly']).area
            smaller = min(pool[j]['poly'].area, c['poly'].area)
            if smaller > 0 and inter/smaller >= CLUSTER_OVERLAP:
                members.append(j)
                taken.add(j)
        wholes = [pool[m] for m in members if pool[m]['whole']]
        if wholes:
            poly, whole = max(wholes, key=lambda m: m['poly'].area)['poly'], True
        else:
            poly, whole = valid(unary_union([pool[m]['poly'] for m in members])), False
        clusters.append({'group': c['group'], 'whole': whole, 'poly': poly, 'members': [f"{pool[m]['scene_id']}/{pool[m]['object_id']}" for m in members]})
    return clusters


# --- renders -------------------------------------------------------------------------------------------------------

def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def pil_affine(affine):
    inv = sr.invert_affine(affine)
    return (inv[0, 0], inv[1, 0], inv[2, 0], inv[0, 1], inv[1, 1], inv[2, 1])


def render_window(root, ctx, window, path, cached_tiles):
    latest, rules = ctx['latest'], ctx['rules']
    extent = 2*(max(r['half']+max(abs(r.get('cx', 0.)), abs(r.get('cy', 0.))) for r in rules.values())+12.)
    n = 1200
    c = extent/n
    canvas_frame = {'id': 'canvas', 'centroid': latest['centroid'], 'image_width': n, 'image_height': n,
                    'pixel_to_meters': [[c, 0], [0, -c], [-extent/2, extent/2]], 'meters_to_pixel': [[1/c, 0], [0, -1/c], [n/2, n/2]],
                    'region': {'bounds': [0, 0, 0, 0]}}
    canvas = Image.new('RGB', (n, n), (40, 40, 44))
    layers = []
    # cached z20 tiles (bottom)
    lon, lat = m_to_lonlat(latest, [-extent/2, extent/2], [-extent/2, extent/2])
    x0, y1 = lonlat_to_tile_xy(float(lon[0]), float(lat[0]), ZOOM)
    x1, y0 = lonlat_to_tile_xy(float(lon[1]), float(lat[1]), ZOOM)
    tx0, ty0, tx1, ty1 = math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)
    mosaic = Image.new('RGBA', ((tx1-tx0)*256, (ty1-ty0)*256), (0, 0, 0, 0))
    tiles_used = 0
    for tx in range(tx0, tx1):
        for ty in range(ty0, ty1):
            if (tx, ty) in cached_tiles:
                try:
                    tile = Image.open(Path(root)/TILE_DIR/f'{tx}_{ty}.jpg').convert('RGBA')
                    mosaic.paste(tile, ((tx-tx0)*256, (ty-ty0)*256))
                    tiles_used += 1
                except OSError:
                    pass
    if tiles_used:
        grid = np.array([[i, j] for i in (0, n/2, n) for j in (0, n/2, n)], float)
        mx = grid[:, 0]*c-extent/2
        my = extent/2-grid[:, 1]*c
        glon, glat = m_to_lonlat(latest, mx, my)
        tile_px = np.array([lonlat_to_tile_xy(float(a), float(b), ZOOM) for a, b in zip(glon, glat, strict=True)])*256-[tx0*256, ty0*256]
        design = np.c_[grid, np.ones(len(grid))]
        coef, *_ = np.linalg.lstsq(design, tile_px, rcond=None)
        data = (coef[0, 0], coef[1, 0], coef[2, 0], coef[0, 1], coef[1, 1], coef[2, 1])
        warped = mosaic.transform((n, n), Image.Transform.AFFINE, data, Image.Resampling.BILINEAR)
        canvas.paste(warped, (0, 0), warped)
        layers.append(f'{tiles_used} cached z20 tiles')
    # registered scene frames placed by their affines (coarse first, the window last)
    order = sorted((nb for nb in ctx['near'] if nb['scene']['registration'] == 'region'), key=lambda nb: -nb['scene']['mpp'])
    for nb in order+[{'scene': {**latest, 'mpp': sr.pixel_spacing(latest), 'registration': 'region'}, 'self': True}]:
        s = nb['scene']
        file = Path(root)/'data/scenes'/s['id']/f"{s['image_sha256']}.png"
        if not file.exists():
            continue
        ft = sr.frame_transform(s, canvas_frame)
        if not Polygon(sr.apply_affine([[0, 0], [s['image_width'], 0], [s['image_width'], s['image_height']], [0, s['image_height']]],
                                       ft['pixel_affine'])).intersects(box(0, 0, n, n)):
            continue
        img = Image.open(file).convert('RGBA')
        warped = img.transform((n, n), Image.Transform.AFFINE, pil_affine(ft['pixel_affine']), Image.Resampling.BILINEAR)
        canvas.paste(warped, (0, 0), warped)
        layers.append(s['id'])
    draw = ImageDraw.Draw(canvas)

    def px(x, y):
        return (x/c+n/2, n/2-y/c)

    def poly_lines(poly, colour, width):
        parts = list(poly.geoms) if poly.geom_type == 'MultiPolygon' else [poly]
        for p in parts:
            if p.is_empty:
                continue
            draw.line([px(*q) for q in p.exterior.coords], fill=colour, width=width)

    def dashed_square(half, colour, width, dash=10, gap=7):
        pts = [(-half, -half), (half, -half), (half, half), (-half, half), (-half, -half)]
        for a, b in zip(pts, pts[1:], strict=False):
            pa, pb = px(*a), px(*b)
            length = math.hypot(pb[0]-pa[0], pb[1]-pa[1])
            ux, uy = (pb[0]-pa[0])/length, (pb[1]-pa[1])/length
            pos = 0.
            while pos < length:
                end = min(pos+dash, length)
                draw.line([(pa[0]+ux*pos, pa[1]+uy*pos), (pa[0]+ux*end, pa[1]+uy*end)], fill=colour, width=width)
                pos += dash+gap

    if not ctx['dev_geo'].is_empty:
        poly_lines(ctx['dev_geo'], (170, 210, 255), 1)
    if not ctx['lot_geo'].is_empty:
        poly_lines(ctx['lot_geo'], (80, 160, 255), 2)
    for row in ctx['rows']:
        if row['_whole'] is not None:
            poly_lines(row['_whole'], (60, 235, 90), 2)
    for row in ctx['rows']:
        colour = ((255, 40, 40) if row['class'] == 'subject' else (255, 80, 230) if row['class'] in ('same_lot', 'same_development')
                  else (255, 150, 30) if row['group'] == 'building' else (255, 240, 120))
        poly_lines(row['_poly'], colour, 2 if row['group'] == 'building' else 1)
    styles = {'R1': ((0, 230, 255), 2, False), 'R2': ((60, 235, 90), 3, False), 'R3_15': ((255, 230, 0), 2, True),
              'R3_25': ((255, 160, 0), 2, True), 'R3_30': ((255, 60, 220), 2, True), 'R4': ((80, 160, 255), 2, True),
              'R5': ((170, 210, 255), 2, True), 'R6': ((170, 90, 255), 3, False)}
    for name, (colour, width, dashed) in styles.items():
        half = rules[name]['half']
        if dashed:
            dashed_square(half, colour, width)
        else:
            draw.rectangle([px(-half, half), px(half, -half)], outline=colour, width=width)
    r7 = rules['R7']
    if r7.get('recentred'):
        draw.rectangle([px(r7['cx']-r7['half'], r7['cy']+r7['half']), px(r7['cx']+r7['half'], r7['cy']-r7['half'])], outline=(255, 255, 255), width=1)
    draw.rectangle([px(-rules['R0']['half'], rules['R0']['half']), px(rules['R0']['half'], -rules['R0']['half'])], outline=(255, 255, 255), width=3)
    font, small = _font(18), _font(14)
    rr = window['rules']
    lines = [f"{window['window']} · today {window['span_m']} m {window['frame_px']} px ({window['rule']}, {','.join(window['reasons'])}) · "
             f"{window['cut_objects']} cut (bldg {sum(1 for r in ctx['rows'] if r['group'] == 'building')})",
             'white R0 today · cyan R1 {:.1f} m · green R2 grow-to-fit {:.1f} m · yellow / orange / magenta dashed R3 core + 15 / 25 / 30 m ring'.format(
                 rr['R1']['span_m'], rr['R2']['span_m']),
             'blue dashed R4 lot-fit {:.1f} m · pale blue dashed R5 development-fit {:.1f} m · purple R6 property-fit {:.1f} m · thin white R7 re-centred {:.1f} m{}'.format(
                 rr['R4']['span_m'], rr['R5']['span_m'], rr['R6']['span_m'], rr['R7']['span_m'], '' if rules['R7'].get('recentred') else ' (= today)'),
             'red = subject cut · magenta = same lot / development cut · orange = neighbour cut building · pale = cut tree/veg · green = whole copy · blue = subject lot, pale blue = development']
    still = {name: sum(1 for r in ctx['rows'] if r['group'] == 'building' and r['rules'][name] == 'still_cut') for name in ('R0', 'R1', 'R2', 'R3_30', 'R6', 'R7')}
    unknown = {name: sum(1 for r in ctx['rows'] if r['group'] == 'building' and r['rules'][name] == 'unknown') for name in ('R1', 'R2', 'R3_30', 'R6', 'R7')}
    lines.append('cut buildings still cut: ' + ' · '.join(f'{k} {v}' + (f' (+{unknown[k]} unknown)' if unknown.get(k) else '') for k, v in still.items()))
    pad = 6
    heights = [24, 18, 18, 18, 18]
    total = sum(heights)+pad*2
    draw.rectangle([0, 0, n, total], fill=(0, 0, 0))
    y = pad
    for i, line in enumerate(lines):
        draw.text((pad, y), line, fill=(255, 255, 255), font=font if i == 0 else small)
        y += heights[i]
    draw.rectangle([0, n-22, n, n], fill=(0, 0, 0))
    draw.text((pad, n-19), f"imagery: {len(layers)} layers ({', '.join(layers[:4])}{'…' if len(layers) > 4 else ''}) · canvas {extent:.0f} m at {c:.3f} m/px",
              fill=(220, 220, 220), font=small)
    canvas.save(path)
    return {'path': str(path), 'layers': layers, 'extent_m': _r(extent, 1), 'm_per_px': _r(c, 4)}


# --- aggregate -----------------------------------------------------------------------------------------------------

def aggregate(windows, objects):
    out = {}
    for name in RULES:
        agg = Counter()
        px = Counter()
        by_group = defaultdict(Counter)
        subject = Counter()
        by_class = defaultdict(Counter)
        for w in windows:
            r = w['rules'][name]
            px[r['frame_px']] += 1
            agg['windows_changed'] += r['changed']
            agg['windows_over_1024px_for_native'] += r['needs_over_1024']
            agg['area_m2'] += r['area_m2']
            agg['extra_area_m2'] += r['extra_area_m2']
            agg['tiles'] += r['tiles']
            agg['tiles_cached'] += r['tiles_cached']
            agg['tiles_to_fetch'] += r['tiles_to_fetch']
            agg['fetch_area_m2'] += r['fetch_area_m2']
            agg['new_tiles_vs_r0'] += r['new_tiles_vs_r0']
            agg['outside_cut_known'] += r['outside_cut_known']
            agg['outside_cut_possible'] += r['outside_cut_possible']
            for g, v in r['outside_cut_by_group'].items():
                agg[f'outside_cut_known_{g}'] += v
            if r['subject_lot_inside'] is not None:
                agg['windows_with_subject_lot'] += 1
                agg['subject_lot_inside'] += r['subject_lot_inside']
                agg['subject_lot_inside_margin'] += r['subject_lot_inside_margin']
            agg['boundary_cover_sum'] += r['boundary_registered_cover']
            agg['subject_inside_5m'] += bool(r['subject_inside_5m'])
            agg['recentred'] += bool(r.get('recentred'))
            if r['development_inside'] is not None:
                agg['windows_with_development'] += 1
                agg['development_inside'] += r['development_inside']
                agg['development_inside_margin'] += r['development_inside_margin']
            if r['property_inside'] is not None:
                agg['windows_with_property'] += 1
                agg['property_inside'] += r['property_inside']
            agg['property_objects_cut'] += r['property_objects_cut']
            agg['property_objects_maybe_cut'] += r['property_objects_maybe_cut']
            agg['windows_property_objects_cut'] += r['property_objects_cut'] > 0
            if 'status' in r:
                agg[f"status_{r['status']}"] += 1
            agg['capped'] += bool(r.get('capped'))
        for o in objects:
            st = o['rules'][name]
            by_group[o['group']][st] += 1
            by_class[o['class']][st] += 1
            if o['class'] == 'subject':
                subject[st] += 1
        n = len(windows)
        out[name] = {'rule': RULE_TEXT[name], 'windows_changed': agg['windows_changed'], 'frame_px': dict(sorted(px.items())),
                     'windows_needing_over_1024px_for_native': agg['windows_over_1024px_for_native'],
                     'windows_subject_inside_5m': agg['subject_inside_5m'], 'windows_recentred': agg['recentred'],
                     'core_cut_objects': {g: dict(v) for g, v in sorted(by_group.items())},
                     'core_cut_by_class': {k: dict(v) for k, v in sorted(by_class.items())},
                     'subject_cut': dict(subject),
                     'cut_anywhere': {'core_still_cut': sum(v.get('still_cut', 0) for v in by_group.values()),
                                      'core_unknown': sum(v.get('unknown', 0) for v in by_group.values()),
                                      'new_edge_known': agg['outside_cut_known'],
                                      'new_edge_known_building': agg['outside_cut_known_building'],
                                      'new_edge_known_green': agg['outside_cut_known_green'],
                                      'new_edge_possible_partial_copies': agg['outside_cut_possible'],
                                      'mean_new_edge_registered_cover': _r(agg['boundary_cover_sum']/n, 3) if n else None},
                     'subject_lot': {'windows_with_subject_lot': agg['windows_with_subject_lot'], 'lot_inside': agg['subject_lot_inside'],
                                     'lot_inside_5m': agg['subject_lot_inside_margin'], 'windows_with_development': agg['windows_with_development'],
                                     'development_inside': agg['development_inside'], 'development_inside_5m': agg['development_inside_margin']},
                     'property': {'windows_with_property': agg['windows_with_property'], 'property_inside': agg['property_inside'],
                                  'property_objects_cut': agg['property_objects_cut'],
                                  'property_objects_maybe_cut': agg['property_objects_maybe_cut'],
                                  'windows_with_property_objects_cut': agg['windows_property_objects_cut'], 'capped_at_120m': agg['capped'],
                                  'status': {k[7:]: v for k, v in sorted(agg.items()) if k.startswith('status_')}},
                     'imagery': {'frame_area_m2': agg['area_m2'], 'extra_area_vs_r0_m2': agg['extra_area_m2'], 'z20_tiles': agg['tiles'],
                                 'tiles_cached': agg['tiles_cached'], 'tiles_to_fetch': agg['tiles_to_fetch'],
                                 'fetch_area_m2': agg['fetch_area_m2'], 'tiles_new_vs_r0': agg['new_tiles_vs_r0']}}
    return out


def strip(row):
    return {k: v for k, v in row.items() if not k.startswith('_')}


def _sc(agg, name, group=None, klass=None):
    a = agg[name]
    if group:
        src = [a['core_cut_objects'].get(group, {})]
    elif klass:
        src = [a['core_cut_by_class'].get(k, {}) for k in klass]
    else:
        src = list(a['core_cut_objects'].values())
    total = Counter()
    for d in src:
        total.update(d)
    return total


def reading(summary, windows):
    """The census read in numbers (generated, so a re-run keeps text and figures together)."""
    agg, pop = summary['rules'], summary['population']
    nb = pop['by_group'].get('building', 0)
    own_classes = ('same_lot', 'same_development')
    own0 = sum(_sc(agg, 'R0', klass=own_classes).values())
    lot_out = [w['window'] for w in windows if w['rules']['R0']['subject_lot_inside'] is False]
    lines = ['## Reading', '',
             f"1. The asked targets are not what is cut. {sum(pop['subject_buildings_live_not_cut'].values())} live subject buildings "
             f"(carried target copies, body carries, takeover replacers, the window's own detections of the target) sit in the 54 windows; "
             f"none reaches the frame (closest {pop['subject_min_edge_margin_m']} m from an edge). The plan's acceptance (subject inside with "
             '5 m) holds.',
             f"2. What the reviewer sees cut is everything else: {nb} buildings and {pop['by_group'].get('green', 0)} trees / vegetation in "
             f"{pop['windows_with_cuts']} windows. Buildings: {pop['building_by_class']['neighbouring_lot']} on a neighbouring lot, "
             f"{pop['building_by_class']['no_parcel']} on no parcel, {pop['building_by_class']['same_development']} on another lot of the subject's "
             f"own development (a plan with common property, at most 120 m across) and {pop['building_by_class']['same_lot']} on the subject lot "
             f"itself. Own-property cut objects of every kind: {own0}. None of the cut objects is in the asked list of the 7 Oct counter "
             f"snapshot ({COUNTER}).",
             f"3. The property itself is not inside today's frame in {len(lot_out)} of {agg['R0']['subject_lot']['windows_with_subject_lot']} "
             f"windows with a resolvable lot ({', '.join(lot_out)}); the subject's development is wholly inside in "
             f"{agg['R0']['subject_lot']['development_inside']} of {agg['R0']['subject_lot']['windows_with_development']}. "
             f"{pop['lot_sources'].get('derived_from_parcels_on_disk', 0)} windows were planned with no lot (no_parcels with parcels_source unavailable, or plan_block_only: "
             'the plan read no usable parcel) and their lot is derived here from parcels on disk now; '
             f"{sum(1 for w in windows if w['window'] in lot_out and w['subject_lot_source'] == 'derived_from_parcels_on_disk')} of the "
             f"{len(lot_out)} lot misses are among them, {sum(1 for w in windows if w['window'] in lot_out and w['rule'] == 'subject_no_body')} "
             'are subject_no_body windows (40-54 m, sized from the slice alone).',
             f"4. Whole copies elsewhere: {pop['building_whole_known']} of {nb} cut buildings and {pop['green_whole_known']} of "
             f"{pop['by_group'].get('green', 0)} cut trees / vegetation (origins {pop['whole_copy_origin']}). Cut buildings overshoot today's "
             f"frame by median {pop['building_overshoot_median_m']} m (p90 {pop['building_overshoot_p90_m']} m, max {pop['building_overshoot_max_m']} m). "
             f"A centred frame holding them needs median {pop['building_span_needed_median_m']} m, p90 {pop['building_span_needed_p90_m']} m; "
             f"{pop['building_span_needed_over_120m']} cannot fit a centred 120 m frame with 5 m.",
             f"5. A bigger frame moves the cut, it does not remove it: R1 resolves {_sc(agg, 'R1', 'building')['resolved']} cut buildings and its new "
             f"edge cuts {agg['R1']['cut_anywhere']['new_edge_known_building']} other buildings ({agg['R1']['cut_anywhere']['new_edge_known']} objects "
             'of every kind, a lower bound: only where another scene has the object). Suburban blocks put a roof on almost every 15 m of '
             'edge, so "no cut anywhere" is not reachable by size; it is reachable only for a chosen set of objects.',
             '', '### Options (numbers from the table above)', '']
    def bline(name):
        b = _sc(agg, name, 'building')
        o = _sc(agg, name, klass=own_classes)
        a = agg[name]
        return (f"cut buildings meeting the core still cut {b.get('still_cut', 0)} / resolved {b.get('resolved', 0)} / unknown {b.get('unknown', 0)}"
                + (f" / left the frame {b.get('left_frame', 0)}" if b.get('left_frame') else '')
                + f"; own-property objects still cut {o.get('still_cut', 0)} / unknown {o.get('unknown', 0)} of {own0}; "
                f"lot inside {a['subject_lot']['lot_inside']}/{a['subject_lot']['windows_with_subject_lot']}, development "
                f"{a['subject_lot']['development_inside']}/{a['subject_lot']['windows_with_development']}; windows changed {a['windows_changed']}; "
                f"frame area {a['imagery']['frame_area_m2']/1e3:,.0f}k m2 (+{a['imagery']['extra_area_vs_r0_m2']/1e3:,.0f}k); z20 tiles to fetch "
                f"{a['imagery']['tiles_to_fetch']} of {a['imagery']['z20_tiles']}; new-edge cuts {a['cut_anywhere']['new_edge_known']}")
    for name, head in (('R1', 'R1 always 1024 px native (120.8 m)'), ('R2', 'R2 grow-to-fit (cap 120 m)'),
                       ('R3_15', 'R3 core + 15 m ring'), ('R3_25', 'R3 core + 25 m ring'), ('R3_30', 'R3 core + 30 m ring'),
                       ('R6', 'R6 property-fit, centred (extra)'), ('R7', 'R7 property-fit, re-centred on the property (extra)')):
        extra = ''
        if name.startswith('R3'):
            extra = (f"; {agg[name]['windows_needing_over_1024px_for_native']} windows exceed 120.8 m and need a 2048 px frame for native "
                     'detail (scene_region.region_frame takes 512 / 1024 only: a code change, or a coarser m/px)')
        lines.append(f'- **{head}**: {bline(name)}{extra}.')
    lines += ['', 'R3 only works if the editor and the queue judge objects by the core: the ring objects are cut at the outer edge by '
              'construction. R6 / R7 fix the property and leave neighbours cut: the queue would need to treat a cut neighbour as context '
              '(it already hides frame-cut objects under the default frame=skip; the canvas still draws them). Unknown = no whole copy on '
              'disk; capture would tell.', '']
    return lines


def write_md(path, summary, windows, objects, renders, meta):
    agg = summary['rules']
    lines = [f'# Frame-context census, 8 Oct 2026 ({VERSION})', '',
             'Reviewer (chat, 8 Oct ~09:15 AEDT), after the top-edge cuts on win-3dd37155248c04ef: "i just want to avoid cutting frames '
             'like it does, when its clearly not the entire permiter of the property etc".', '',
             f"Read-only. Population: the review layer of the {len(windows)} windows in windows-2026-10-01.jsonl; live building, tree and "
             'vegetation objects whose ring reaches the frame (scene_focus.touched_edges, 1 px). Whole copies come from other scenes on '
             'disk through scene_recapture.frame_transform (pure georeferenced affine). Rules are geometry only: nothing was captured.', '',
             '## Population', '']
    pop = summary['population']
    lines += [f"- Cut objects: **{pop['cut_objects']}** ({pop['by_group'].get('building', 0)} buildings, {pop['by_group'].get('green', 0)} tree/vegetation) "
              f"in {pop['windows_with_cuts']} of {len(windows)} windows.",
              f"- Buildings by class: {', '.join(f'{k} {v}' for k, v in pop['building_by_class'].items())}.",
              f"- Tree/vegetation by class: {', '.join(f'{k} {v}' for k, v in pop['green_by_class'].items())}.",
              f"- Whole copy known: buildings {pop['building_whole_known']} of {pop['by_group'].get('building', 0)}, "
              f"tree/vegetation {pop['green_whole_known']} of {pop['by_group'].get('green', 0)} (fitted-frame copies: {pop['whole_from_fitted']}).",
              f"- Cut buildings' whole copies overshoot today's frame by median {pop['building_overshoot_median_m']} m "
              f"(p90 {pop['building_overshoot_p90_m']} m, max {pop['building_overshoot_max_m']} m); span a centred window needs to hold them: "
              f"median {pop['building_span_needed_median_m']} m, p90 {pop['building_span_needed_p90_m']} m.",
              f"- Subject lot from: {', '.join(f'{k} {v}' for k, v in pop['lot_sources'].items())}; subject lot wholly inside today's frame: {agg['R0']['subject_lot']['lot_inside']} of "
              f"{agg['R0']['subject_lot']['windows_with_subject_lot']} windows with a lot.",
              f"- Distance of cut buildings from the window centre (centroid of the visible part): median {pop['building_dist_median_m']} m.", '']
    lines += reading(summary, windows)
    lines += ['## Rules', '', '| rule | windows changed | frame px | cut buildings meeting core: still / resolved / unknown | cut tree+veg: still / resolved / unknown '
              '| subject cut: still / unknown | same lot+development cut (all kinds): still / unknown | new-edge cuts known (bldg) | subject lot inside | development inside '
              '| tiles to fetch (of z20 tiles) | fetch area m² |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for name in RULES:
        a = agg[name]
        b = a['core_cut_objects'].get('building', {})
        g = a['core_cut_objects'].get('green', {})
        s = a['subject_cut']
        own = Counter(a['core_cut_by_class'].get('same_lot', {}))+Counter(a['core_cut_by_class'].get('same_development', {}))
        px = ', '.join(f'{k}:{v}' for k, v in a['frame_px'].items())
        lines.append(f"| {name} | {a['windows_changed']} | {px} | {b.get('still_cut', 0)} / {b.get('resolved', 0)} / {b.get('unknown', 0)} | "
                     f"{g.get('still_cut', 0)} / {g.get('resolved', 0)} / {g.get('unknown', 0)} | {s.get('still_cut', 0)} / {s.get('unknown', 0)} | "
                     f"{own.get('still_cut', 0)} / {own.get('unknown', 0)} | "
                     f"{a['cut_anywhere']['new_edge_known']} ({a['cut_anywhere']['new_edge_known_building']}) | "
                     f"{a['subject_lot']['lot_inside']}/{a['subject_lot']['windows_with_subject_lot']} | "
                     f"{a['subject_lot']['development_inside']}/{a['subject_lot']['windows_with_development']} | "
                     f"{a['imagery']['tiles_to_fetch']} ({a['imagery']['z20_tiles']}) | {a['imagery']['fetch_area_m2']:,.0f} |")
    lines += ['', 'Rules: ' + '; '.join(f'**{k}** {v}' for k, v in RULE_TEXT.items()) + '.', '',
              '"Still cut" = the whole copy (or, without one, the farthest-reaching matched copy, itself cut at its own frame, so a lower bound) still crosses the rule\'s frame; '
              '"unknown" = no whole copy and the cut copies do not reach the new edge. "New-edge cuts known" = objects of other scenes, '
              'wholly outside today\'s frame, that the new frame edge would cut (a lower bound: mean registered-imagery cover of the new '
              'edge is given in census.json cut_anywhere.mean_new_edge_registered_cover). R3 frames over 120.8 m need more than 1024 px '
              'for native detail (scene_region.region_frame accepts only 512 and 1024).', '']
    lines += ['## R3 detail and cost', '']
    for name in ('R1', 'R2', 'R3_15', 'R3_25', 'R3_30', 'R4'):
        a = agg[name]
        lines.append(f"- {name}: windows needing >1024 px for native detail {a['windows_needing_over_1024px_for_native']}; frame area "
                     f"{a['imagery']['frame_area_m2']:,.0f} m² (+{a['imagery']['extra_area_vs_r0_m2']:,.0f} vs R0); new tiles vs R0 "
                     f"{a['imagery']['tiles_new_vs_r0']}; mean registered cover of the new edge {a['cut_anywhere']['mean_new_edge_registered_cover']}.")
    lines += ['', '## Per window', '', '| window | rule/reason | span m / px | cut bldg (subject) | cut green | bldg whole known | max overshoot m '
              '| R2 span | R2 still cut bldg | R3_30 still cut bldg | lot inside R0 / R2 / R4 | development inside R0 / R5 | R7 span / offset m |', '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for w in windows:
        rows = [o for o in objects if o['window'] == w['window']]
        b = [o for o in rows if o['group'] == 'building']
        over = [o['whole_copy']['overshoot_m'] for o in b if o['whole_copy']]
        r = w['rules']

        def lot(name, rr=r, key='subject_lot_inside'):
            v = rr[name][key]
            return '-' if v is None else 'yes' if v else 'no'
        lines.append(f"| {w['window']} | {w['rule']}/{','.join(w['reasons'])} | {w['span_m']} / {w['frame_px']} | {len(b)} ({sum(1 for o in b if o['class'] == 'subject')}) | "
                     f"{len(rows)-len(b)} | {sum(1 for o in b if o['whole_copy'])} | {max(over) if over else '-'} | {r['R2']['span_m']} | "
                     f"{sum(1 for o in b if o['rules']['R2'] == 'still_cut')} | {sum(1 for o in b if o['rules']['R3_30'] == 'still_cut')} | "
                     f"{lot('R0')} / {lot('R2')} / {lot('R4')} | {lot('R0', key='development_inside')} / {lot('R5', key='development_inside')} | "
                     f"{r['R7']['span_m']} / {r['R7']['centre_offset_m']} |")
    lines += ['', '## win-3dd37155248c04ef (the reported window)', '']
    for o in objects:
        if o['window'] == 'win-3dd37155248c04ef' and o['group'] == 'building':
            wc = o['whole_copy']
            lines.append(f"- {o['id']} {o['status']} {o['class']} edges {','.join(o['edges'])}, {o['area_in_frame_m2']} m² visible, "
                         f"{o['dist_centroid_m']} m from centre; " + (f"whole copy {wc['scene_id']}/{wc['object_id']} ({wc['registration']}), "
                         f"{wc['area_m2']} m², overshoot {wc['overshoot_m']} m, {wc['outside_share']:.0%} outside, span needed {wc['span_needed_m']} m"
                         if wc else f"no whole copy (lower bound overshoot {o['lower_bound_overshoot_m']} m)")
                         + '; rules: ' + ', '.join(f'{k} {v}' for k, v in o['rules'].items()))
    lines += ['', '## Renders', ''] + [f"- {r['path']} ({len(r['layers'])} imagery layers, {r['extent_m']} m canvas)" for r in renders]
    lines += ['', '## Inputs and checks', '', f"- Scenes scanned: {meta['scan']}", f"- Checks: {meta['checks']}",
              f"- Written {meta['written_at']} by scripts/ml/experiments/frame_context_census.py; code sha256 {meta['code_sha256'][:16]}.", '']
    Path(path).write_text('\n'.join(lines))


def quantile(values, q):
    if not values:
        return None
    return _r(float(np.quantile(np.asarray(values, float), q)))


def population(windows, objects):
    b = [o for o in objects if o['group'] == 'building']
    g = [o for o in objects if o['group'] == 'green']
    over = [o['whole_copy']['overshoot_m'] for o in b if o['whole_copy']]
    need = [o['whole_copy']['span_needed_m'] for o in b if o['whole_copy']]
    return {'cut_objects': len(objects), 'windows_with_cuts': len({o['window'] for o in objects}),
            'by_group': dict(Counter(o['group'] for o in objects)), 'by_kind': dict(Counter(o['kind'] for o in objects)),
            'building_by_class': {k: sum(1 for o in b if o['class'] == k) for k in CLASSES},
            'green_by_class': {k: sum(1 for o in g if o['class'] == k) for k in CLASSES},
            'subject_subtypes': dict(Counter(o['subject_subtype'] for o in b if o['subject_subtype'])),
            'building_by_status': dict(Counter(o['status'] for o in b)),
            'building_whole_known': sum(1 for o in b if o['whole_copy']), 'green_whole_known': sum(1 for o in g if o['whole_copy']),
            'whole_from_fitted': sum(1 for o in objects if o['whole_copy'] and o['whole_copy']['registration'] != 'region'),
            'whole_copy_origin': dict(Counter(o['whole_copy']['origin'] for o in objects if o['whole_copy'])),
            'building_overshoot_median_m': quantile(over, .5), 'building_overshoot_p90_m': quantile(over, .9),
            'building_overshoot_max_m': max(over) if over else None,
            'building_span_needed_median_m': quantile(need, .5), 'building_span_needed_p90_m': quantile(need, .9),
            'building_span_needed_over_120m': sum(1 for v in need if v+2*MARGIN_M > CAP_M),
            'building_dist_median_m': quantile([o['dist_centroid_m'] for o in b], .5),
            'asked_2026_10_07_cut': sum(1 for o in objects if o['asked_2026_10_07']),
            'subject_buildings_live_not_cut': dict(sum((Counter(w['subject_buildings_live']) for w in windows), Counter())),
            'subject_min_edge_margin_m': min((w['subject_min_edge_margin_m'] for w in windows if w['subject_min_edge_margin_m'] is not None), default=None),
            'windows_with_development': sum(1 for w in windows if w['development_plans']),
            'development_plans_wider_than_cap': sum(len(w['development_plans_wider_than_cap']) for w in windows),
            'lot_sources': dict(Counter(w['subject_lot_source'] for w in windows))}


def pick_renders(windows, objects, first='win-3dd37155248c04ef', n=6):
    chosen = [first] if any(w['window'] == first for w in windows) else []
    score = {}
    for w in windows:
        b = [o for o in objects if o['window'] == w['window'] and o['group'] == 'building']
        score[w['window']] = (sum(1 for o in b if o['class'] == 'subject'), len(b))
    wanted = [('rule', 'lot', 1024), ('rule', 'lot', 512), ('rule', 'subject', 1024), ('rule', 'subject_no_body', None), ('rule', 'subject', 512)]
    for _, rule, px in wanted:
        pool = [w for w in windows if w['window'] not in chosen and w['rule'] == rule and (px is None or w['frame_px'] == px)]
        pool.sort(key=lambda w: score[w['window']], reverse=True)
        if pool and len(chosen) < n:
            chosen.append(pool[0]['window'])
    for w in sorted(windows, key=lambda w: score[w['window']], reverse=True):
        if len(chosen) >= n:
            break
        if w['window'] not in chosen:
            chosen.append(w['window'])
    return chosen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--only', nargs='*', default=None)
    parser.add_argument('--no-render', action='store_true')
    parser.add_argument('--out-dir', default=str(OUT_DIR), help='output folder (default the v1 census folder)')
    parser.add_argument('--run-dir', default=str(RUN_DIR), help='log / heartbeat folder')
    parser.add_argument('--extra-parcels', nargs='*', default=[], help='folders of frozen parcel payloads ({key, sha256, data}) to read too')
    parser.add_argument('--renders', nargs='*', default=None, help='windows to render (default: win-3dd37155248c04ef + 5 picked)')
    args = parser.parse_args(argv)
    log = Log(args.run_dir)
    out_dir = Path(args.out_dir)
    (out_dir/'renders').mkdir(parents=True, exist_ok=True)
    log(f'start {VERSION}', beat=True)
    wids = window_ids(REGISTRY)
    if args.only:
        wids = [w for w in wids if w in args.only]
    latests = {w: sr._scene(ROOT, w) for w in wids}
    aoi = unary_union([window_aoi(latests[w]) for w in wids])
    del latests
    scenes, parcels, payload_boxes, scan_stats = scan_scenes(ROOT, aoi, log, args.extra_parcels)
    cached = {tuple(map(int, p.stem.split('_'))) for p in (ROOT/TILE_DIR).glob('*.jpg')}
    counter = _read(COUNTER) or {}
    asked = {(s['scene_id'], o['id']) for s in counter.get('next_scenes') or [] for o in s.get('objects') or []}
    windows, objects, contexts = [], [], {}
    for i, wid in enumerate(wids):
        window, rows, ctx = analyse_window(ROOT, wid, scenes, parcels, payload_boxes, cached, asked, log)
        windows.append(window)
        objects.extend(rows)
        contexts[wid] = ctx
        log(f'window {i+1}/{len(wids)} {wid}: cut {len(rows)} (bldg {sum(1 for r in rows if r["group"] == "building")}), near {window["near_scenes"]}', beat=True)
    renders = []
    if not args.no_render:
        chosen = args.renders or pick_renders(windows, objects)
        for wid in chosen:
            if wid in contexts:
                window = next(w for w in windows if w['window'] == wid)
                renders.append({'window': wid, **render_window(ROOT, contexts[wid], window, out_dir/'renders'/f'{wid}.png', cached)})
                log(f'render {wid}', beat=True)
    checks = {'windows': len(windows), 'square_centred_frames': sum(1 for w in windows if all(w['checks'].values())),
              'subjects_with_target_ring': sum(1 for w in windows if w['subject_targets_found']),
              'objects_cut_rows': len(objects),
              'r0_all_still_cut': all(o['rules']['R0'] == 'still_cut' for o in objects),
              'whole_copy_short_of_edge_gt_1m': sum(1 for o in objects if o['whole_copy'] and o['whole_copy']['half_extent_m'] < next(
                  w for w in windows if w['window'] == o['window'])['span_m']/2-1),
              'editor_cut_not_touched_edges': sum(w['editor_cut_not_touched_edges'] for w in windows),
              'rule_spans_never_below_r0': all(w['rules'][n]['span_m'] >= w['rules']['R0']['span_m']-.01 for w in windows for n in RULES)}
    summary = {'population': population(windows, objects), 'rules': aggregate(windows, objects)}
    code = Path(__file__).read_bytes()
    meta = {'version': VERSION, 'written_at': _now(), 'code_sha256': _sha(code), 'registry': str(REGISTRY),
            'registry_sha256': _sha(REGISTRY.read_bytes()), 'scan': dict(scan_stats), 'scenes_in_aoi': len(scenes), 'parcels_in_aoi': len(parcels),
            'cached_z20_tiles': len(cached), 'counter_hint': str(COUNTER) if counter else None, 'checks': checks,
            'constants': {'tolerance_px': TOL_PX, 'edge_margin_m': MARGIN_M, 'cap_m': CAP_M, 'rings_m': RINGS_M, 'match_cover': MATCH_COVER,
                          'match_iou': MATCH_IOU, 'cluster_overlap_of_smaller': CLUSTER_OVERLAP, 'subject_cover': SUBJECT_COVER, 'subject_iou': SUBJECT_IOU,
                          'share_min': SHARE_MIN, 'second_lot_share': SECOND_SHARE}}
    payload = {**meta, 'reviewer': 'i just want to avoid cutting frames like it does, when its clearly not the entire permiter of the property etc',
               'summary': summary, 'windows': windows, 'renders': renders, 'objects_file': str(out_dir/'objects.jsonl')}
    suffix = '' if not args.only else '-only'
    (out_dir/f'census{suffix}.json').write_text(json.dumps(payload, indent=1, default=str))
    with open(out_dir/f'objects{suffix}.jsonl', 'w') as h:
        for o in objects:
            h.write(json.dumps(strip(o), default=str)+'\n')
    write_md(out_dir/f'census{suffix}.md', summary, windows, [strip(o) for o in objects], renders, meta)
    log(f'done: windows {len(windows)} objects {len(objects)} renders {len(renders)}', beat=True)
    print(json.dumps({'windows': len(windows), 'objects': len(objects), 'renders': [r['path'] for r in renders], 'checks': checks}))


if __name__ == '__main__':
    main()
