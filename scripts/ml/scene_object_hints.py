"""Object hints (object-hints-v2): one-sentence, evidence-backed reasons a reviewer may want to look twice at a
scene object. Agent hints, never labels; nothing is decided or applied.

Every hint is recomputed from latest.json and the scene PNG only (review.json is never read), carries its
evidence numbers, and lands as a sidecar row in the eyes index keyed by the current model_signature. The
thresholds in DESIGN are design values frozen before the first scored run; any change is a new version.
object-hints-v2 (2026-09-30 evening) adds one code, solar_fixture_like (T8), after two reviewer class errors on
roof_training_250-29761; its design values were chosen with those two objects visible. Every v1 code, threshold and
sentence is unchanged; any further change is object-hints-v3.
Population flag rates (teacher_checks.py) are the only over-flag measure until reviewer negatives accumulate.
Lot codes (T1/T2) are copied from scene_lot_tags (lot-tags-v1) with their sentences. Sentence wording (never a
threshold or a code) may change after a scored run: SENTENCES_VERSION counts those changes and every row records the
number it was written with (rows without the field predate 2).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Polygon

from . import eyes_index, scene_lot_tags
from .roof_reflection import audit_polygon_geometry
from .scene_object_quality import (
    BUILDING_VERSION,
    _mask,
    _rgb_array,
    _row,
    load_image,
    local_polygon,
    replace_scene_rows,
)
from .scene_self_review import GEOMETRY_ERRORS, validate_id
from .structure_kinds import VERSION as STRUCTURE_VERSION
from .structure_kinds import geometry_features
from .training.common import ORTHO, orthogonalise

VERSION = 'object-hints-v2'
# Earlier versions whose rows may still sit in the eyes index until a scene is rewritten (write_scene_rows then drops them for that
# scene). Readers (the hints route, teacher_checks) serve them only where no current row exists and report them stale: they predate
# the current rules (v1 has no solar_fixture_like).
PREVIOUS_VERSIONS = ('object-hints-v1',)
# Wording changes after the first scored run (thresholds and codes untouched): 2 = lot_incomplete_in_frame blames the frame
# only when the ring touches it (lot-tags-v1 edge_px) and names the object's kind, the twin of property-parcels.ts parcelLotSentence.
SENTENCES_VERSION = 2
TABLE = 'object_quality'
NOTE = 'agent hint, not a label'
DEAD_STATUSES = ('rejected', 'superseded')
CANOPY = ('tree', 'vegetation')
SEVERITIES = ('context', 'review')
# Design values (metres, m², degrees, 0-255 grey levels, shares as fractions), frozen before the first scored run.
DESIGN = {
    'canopy_fragment_inside': {'area_m2': 1., 'cover': .90, 'container_ratio': 10.},
    'pool_not_blue': {'green_share': .5, 'blue_share': .2, 'exg': 20, 'hue': (80, 125), 'saturation': 60},
    'building_blends_into_surroundings': {'area_m2': 4., 'grey_std': 15., 'brightness_delta': 10., 'ring_m': 2.},
    'tiny_area_for_kind': {'building': 4., 'tree': 1., 'vegetation': 1., 'pool': 2.},
    'degenerate_outline': {'area_m2': 1., 'rect_width_m': .5},
    'secondary_by_lot': {'member_share': .5, 'ratio': .5},
    'noisy_outline': {'area_m2': 20., 'simplify_m': .3, 'svpm': .45, 'wonky_share': .85, 'axis_tolerance_deg': 4.5, 'min_edge_m': .2},
    'roof_fit_on_fragment': {'area_m2': 4.},
    'squared_candidate': {'area_m2': 20., **ORTHO},
    # object-hints-v2. A clean panel array fills its minimum rectangle; a single panel is ~1.7 m². The brightness ceiling is the
    # scene's own 60th-percentile grey rather than an absolute 150: at 150 the reviewer's hot-water unit (median grey 148) stays quiet.
    'solar_fixture_like': {'rect_fill': .70, 'luminance_percentile': 60, 'area_m2': 1.2},
}
RULES = {
    'frame_cut': 'lot-tags-v1: ring within 1 px of the frame, or clipped_by_frame with no live replacement',
    'lot_incomplete_in_frame': 'lot-tags-v1: the dominant lot polygon leaves the frame (context)',
    'lot_unassociated': 'lot-tags-v1: dominant lot is not the address lot nor a lot of a unit-site address plan, and leaves the frame',
    'spans_lots': 'lot-tags-v1: >= 2 numbered lots with distinct polygons each covering >= 10% of the object',
    'common_property': 'lot-tags-v1: dominant lot is CM (context)',
    'unit_site': 'lot-tags-v1: the address plan holds >= 2 numbered lots plus a CM lot (scene sentence on every object, context)',
    'unit_title_mismatch': 'lot-tags-v1: the title says Unit n but the CRM seed roof sits on another lot (context, on the seed roof)',
    'canopy_fragment_inside': 'tree|vegetation < 1 m² with >= 90% of its area inside a live tree|vegetation object >= 10x its area',
    'pool_not_blue': 'pool with ExG(2G-R-B > 20) share >= 0.5 and bluish (OpenCV hue 80-125, S >= 60) share < 0.2',
    'building_blends_into_surroundings': 'building < 4 m², grey std < 15 and |brightness - 2 m ring brightness| < 10',
    'tiny_area_for_kind': 'building < 4 m², tree or vegetation < 1 m², pool < 2 m²',
    'degenerate_outline': 'any kind: area < 1 m² or minimum-rectangle width < 0.5 m',
    'secondary_by_lot': 'building with a lot tag whose area is <= 0.5 of the largest live building >= 0.5 inside the same lot polygon; '
                        'without parcels, the building-flags-v2 secondary-structure flag',
    'noisy_outline': 'building >= 20 m²: vertices of the 0.3 m-simplified ring per metre of perimeter >= 0.45 and share of raw edges '
                     '(>= 0.2 m) more than 4.5 deg off the minimum-rectangle axes (mod 90) >= 0.85',
    'roof_fit_on_fragment': 'roof_parts present on an object < 4 m² or with structure hint fragment',
    'squared_candidate': 'building >= 20 m²: training.common.orthogonalise with ORTHO gates (IoU >= 0.85, Hausdorff <= 1.2 m); a candidate, never applied',
    'solar_fixture_like': 'solar_panel with rectangle fill (area / minimum rotated rectangle area) < 0.70, or median grey inside the outline above '
                          "the scene's 60th-percentile grey (image only), or area < 1.2 m²",
}
LOT_CODES = {'frame_cut': 'review', 'lot_incomplete_in_frame': 'context', 'lot_unassociated': 'review', 'spans_lots': 'review',
             'common_property': 'context'}
LOT_SENTENCE_PREFIX = {'frame_cut': ('Touches', 'Cut by'), 'lot_unassociated': ('On neighbouring',), 'spans_lots': ('Spans',),
                       'common_property': ('On common property',)}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def hint(code, severity, sentence, evidence):
    if severity not in SEVERITIES:
        raise ValueError(f'Unknown severity {severity!r}')
    return {'code': code, 'severity': severity, 'sentence': sentence, 'evidence': evidence}


def _num(value, digits=3):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _fmt_area(area):
    return f'{area:.1f} m²' if area >= 1 else f'{area:.2f} m²'


def lot_name(dominant):
    if not dominant:
        return 'this lot'
    return f"{scene_lot_tags.lot_label([dominant.get('lot') or ''])} {dominant.get('plan') or ''}".strip()


# --- Geometry -------------------------------------------------------------------------------------------------

def _pixel_scale(scene):
    return abs(float(np.linalg.det(np.asarray(scene['pixel_to_meters'], dtype=float)[:2])))**.5


def _live(scene):
    return [o for o in scene.get('objects', []) if o.get('status') != 'superseded']


def outline_metrics(shape, ring_count):
    """Vertices per metre after 0.3 m simplification and the share of raw edges off the minimum-rectangle axes."""
    design = DESIGN['noisy_outline']
    simplified = shape.simplify(design['simplify_m'], preserve_topology=True)
    perimeter = float(shape.exterior.length)  # exterior ring only, like `vertices` and the editor's outline-hints twin; holes add no edge
    simplified_vertices = len(simplified.exterior.coords)-1 if simplified.geom_type == 'Polygon' and not simplified.is_empty else ring_count
    geometry = geometry_features(shape, ring_count)
    axis = geometry.get('long_axis_deg') or 0.
    ring = list(shape.exterior.coords)[:-1]
    n = len(ring)
    edges = sum(1 for i in range(n) if math.dist(ring[i], ring[(i+1) % n]) >= design['min_edge_m'])
    _, parallelism, wonky, _ = audit_polygon_geometry(ring, axis, design['axis_tolerance_deg'])
    return {'vertices': int(ring_count), 'simplified_vertices': int(simplified_vertices), 'perimeter_m': _num(perimeter, 2),
            'svpm': _num(simplified_vertices/perimeter, 3) if perimeter > 0 else None, 'off_axis': int(wonky), 'edges_counted': int(edges),
            'wonky_share': _num(wonky/edges, 3) if edges else None, 'parallel_share': _num(parallelism, 3),
            'rectilinearity': geometry.get('rectilinearity'), 'rect_width_m': geometry.get('rect_width_m'),
            'rect_length_m': geometry.get('rect_length_m'), 'long_axis_deg': geometry.get('long_axis_deg')}


def squared_candidate(obj, scene):
    """training.common.orthogonalise on the pixel ring, gated; {ring, iou, hausdorff_m} or {refused: reason}."""
    try:
        polygon = Polygon(obj['ring'], obj.get('holes') or [])
        if not polygon.is_valid or polygon.is_empty:
            return {'refused': 'invalid outline'}
        if polygon.interiors:
            return {'refused': 'outline has holes'}
        px_m = _pixel_scale(scene)
        candidate = orthogonalise(polygon, px_m)
    except GEOMETRY_ERRORS:
        return {'refused': 'geometry error'}
    if candidate is None:
        return {'refused': f"gates refused (IoU < {ORTHO['min_iou']} or Hausdorff > {ORTHO['max_hausdorff_m']} m, or no axis pair)"}
    iou = candidate.intersection(polygon).area/max(candidate.union(polygon).area, 1e-9)
    return {'ring': [[round(float(x), 2), round(float(y), 2)] for x, y in candidate.exterior.coords[:-1]],
            'iou': _num(iou, 4), 'hausdorff_m': _num(candidate.hausdorff_distance(polygon)*px_m, 3)}


# --- Colour -----------------------------------------------------------------------------------------------------

def _ring_mask(obj, shape_px, ring_px):
    """Boolean mask of a ring_px-wide band around the outline (buffer minus the outline), full-frame."""
    height, width = shape_px
    polygon = Polygon(obj['ring'], obj.get('holes') or [])
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    band = polygon.buffer(ring_px).difference(polygon)
    canvas = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(canvas)
    for part in ([band] if band.geom_type == 'Polygon' else list(getattr(band, 'geoms', []))):
        if part.geom_type != 'Polygon' or part.is_empty:
            continue
        draw.polygon([tuple(p) for p in part.exterior.coords], fill=1)
        for hole in part.interiors:
            draw.polygon([tuple(p) for p in hole.coords], fill=0)
    return np.asarray(canvas, dtype=bool)


def colour_features(obj, scene, image, grey=None):
    """ExG green share, bluish share, grey std and brightness inside; brightness of the 2 m ring outside."""
    if image is None:
        return None
    height, width = image.shape[:2]
    mask, (y0, x0) = _mask(obj, (height, width))
    if mask is None or not mask.any():
        return None
    crop = image[y0:y0+mask.shape[0], x0:x0+mask.shape[1]]
    rgb = crop[mask].astype(float)
    design = DESIGN['pool_not_blue']
    exg = 2*rgb[:, 1]-rgb[:, 0]-rgb[:, 2]
    hsv = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_RGB2HSV)[mask]
    bluish = (hsv[:, 0] >= design['hue'][0]) & (hsv[:, 0] <= design['hue'][1]) & (hsv[:, 1] >= design['saturation'])
    grey = cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2GRAY).astype(float) if grey is None else grey
    inside = grey[y0:y0+mask.shape[0], x0:x0+mask.shape[1]][mask]
    ring_px = DESIGN['building_blends_into_surroundings']['ring_m']/_pixel_scale(scene)
    ring = _ring_mask(obj, (height, width), ring_px)
    outside = grey[ring] if ring.any() else np.asarray([])
    return {'green_share': _num((exg > design['exg']).mean()), 'blue_share': _num(bluish.mean()),
            'grey_std': _num(inside.std(), 2), 'brightness': _num(inside.mean(), 1),
            'ring_brightness': _num(outside.mean(), 1) if outside.size else None, 'pixels': int(mask.sum())}


# --- Solar panels (T8, object-hints-v2) ---------------------------------------------------------------------------

SOLAR_WORDS = {'bright': 'bright', 'non_rectangular': 'non-rectangular', 'small': 'small'}


def luminance_ceiling(grey):
    """The scene's DESIGN['solar_fixture_like'] percentile grey level (0-255) over the whole frame; None without an image."""
    if grey is None or not grey.size:
        return None
    return float(np.percentile(grey, DESIGN['solar_fixture_like']['luminance_percentile']))


def solar_measure(obj, shape, image, grey, ceiling):
    """Quiet measurements of one solar_panel and the solar_fixture_like reasons they meet.

    Rectangle fill (area / minimum rotated rectangle), the outline's off-axis edge share (outline_metrics, as for buildings),
    vertices per metre of exterior edge and, with an image, the median grey and median blue-minus-red (0-255) inside the
    outline against the scene's percentile grey. Panels read dark and slightly blue or black; hot-water units, air-conditioners
    and skylights read bright grey or white. Without an image brightness is not judged (median_luminance None).
    """
    design = DESIGN['solar_fixture_like']
    vertices = len(obj.get('ring') or [])
    geometry = geometry_features(shape, vertices)
    polygon = shape.geom_type == 'Polygon'
    perimeter = float(shape.exterior.length if polygon else shape.length)
    try:
        off_axis = outline_metrics(shape, vertices).get('wonky_share') if polygon else None
    except (*GEOMETRY_ERRORS, AttributeError):
        off_axis = None
    area, fill = float(shape.area), geometry.get('rectilinearity')
    median = blue_minus_red = None
    pixels = 0
    if image is not None and grey is not None:
        mask, (y0, x0) = _mask(obj, image.shape[:2])
        if mask is not None and mask.any():
            window = (slice(y0, y0+mask.shape[0]), slice(x0, x0+mask.shape[1]))
            rgb = image[window][mask].astype(float)
            median = float(np.median(grey[window][mask]))
            blue_minus_red = float(np.median(rgb[:, 2]-rgb[:, 0]))
            pixels = int(mask.sum())
    reasons = []
    if median is not None and ceiling is not None and median > ceiling:
        reasons.append('bright')
    if fill is not None and fill < design['rect_fill']:
        reasons.append('non_rectangular')
    if area < design['area_m2']:
        reasons.append('small')
    return {'area_m2': _num(area), 'rect_fill': fill, 'off_axis_share': off_axis, 'vertices': vertices, 'perimeter_m': _num(perimeter, 2),
            'vertices_per_m': _num(vertices/perimeter, 3) if perimeter > 0 else None, 'median_luminance': _num(median, 1),
            'luminance_ceiling': _num(ceiling, 1) if median is not None else None, 'blue_minus_red': _num(blue_minus_red, 1),
            'pixels': pixels, 'reasons': reasons}


def solar_sentence(measure):
    """'Bright for a solar panel (median grey 148, above the image's 60th percentile of 120); check for ...' from the met reasons."""
    reasons = measure['reasons']
    words = [SOLAR_WORDS[r] for r in reasons]
    head = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"
    details = []
    if 'bright' in reasons:
        median, ceiling = measure['median_luminance'], measure['luminance_ceiling']
        digits = 1 if f'{median:.0f}' == f'{ceiling:.0f}' else 0   # never "120, above ... of 120"
        details.append(f"median grey {median:.{digits}f}, above the image's "
                       f"{DESIGN['solar_fixture_like']['luminance_percentile']}th percentile of {ceiling:.{digits}f}")
    if 'non_rectangular' in reasons:
        details.append(f"fills {measure['rect_fill']*100:.0f}% of its rectangle")
    if 'small' in reasons:
        details.append(f"{_fmt_area(measure['area_m2'])}, smaller than one panel")
    return f"{head[0].upper()}{head[1:]} for a solar panel ({'; '.join(details)}); check for a hot-water unit, air-conditioner or skylight."


# --- Lot tag adapters ---------------------------------------------------------------------------------------------

def lot_sentence(code, tag):
    """The lot-tags-v1 sentence for a code (its sentences list is not one-to-one with its flags)."""
    prefixes = LOT_SENTENCE_PREFIX.get(code)
    if prefixes:
        for sentence in tag.get('sentences') or []:
            if sentence.startswith(prefixes):
                return sentence
    if code == 'lot_incomplete_in_frame':
        # Twin of property-parcels.ts parcelLotSentence: the frame is blamed only when the ring touches it (lot-tags-v1 edge_px
        # within DESIGN['edge_px']); otherwise the lot simply continues past the image and nothing here says the object was cut.
        edge_px = tag.get('edge_px')
        touches = edge_px is not None and float(edge_px) <= scene_lot_tags.DESIGN['edge_px']
        noun = scene_lot_tags.KIND_NOUNS.get(tag.get('kind'), 'object')
        ending = f'the frame cut it, not the {noun}.' if touches else 'only part of the lot is visible.'
        return f"Sits on {lot_name(tag.get('dominant'))}, which leaves this image; {ending}"
    return code.replace('_', ' ')


def lot_hints(tag):
    """T1 hints copied from a lot-tags-v1 object tag."""
    out = []
    for code in tag.get('flags') or []:
        severity = LOT_CODES.get(code)
        if not severity:
            continue
        evidence = {'edge_px': tag.get('edge_px'), 'lot': lot_name(tag.get('dominant')) if tag.get('dominant') else None,
                    'lot_share': (tag.get('dominant') or {}).get('share'), 'address_plan': tag.get('address_plan'),
                    'lot_complete_in_image': tag.get('lot_complete_in_image')}
        if code == 'spans_lots':
            evidence['lots'] = [{k: lot.get(k) for k in ('lot', 'plan', 'share')} for lot in tag.get('lots') or []
                                if scene_lot_tags.is_numbered(lot.get('lot') or '') and (lot.get('share') or 0) >= scene_lot_tags.DESIGN['spans_lot_share']]
        if code == 'frame_cut' and tag.get('clipped_by_frame'):
            evidence['clipped_by_frame'] = tag['clipped_by_frame']
        out.append(hint(code, severity, lot_sentence(code, tag), evidence))
    return out


# --- Hints ------------------------------------------------------------------------------------------------------

def hints_for_scene(scene, image=None, *, parcels=None, lot_tags=None, scene_tag=None, structure_rows=None, flags=None,
                    parcels_sha256=None, root=Path('.'), kinds=None):
    """One object-hints row (VERSION) per live object: hints [{code, severity, sentence, evidence}], squared_candidate, outline, solar.

    image: RGB array/PIL or None (colour codes then record image_available false). parcels: a Vicmap payload used
    to compute lot tags (scene_lot_tags.analyse) when lot_tags/scene_tag are not given. structure_rows:
    {object_id: structure-kinds payload}; flags: {object_id: building-flags-v2 payload} (secondary fallback without
    parcels). kinds: rows only for objects of these kinds (teacher_checks' solar pass); every live object still counts as
    context (canopy containers, the scene's luminance percentile). Never reads review.json; never writes.
    """
    image = _rgb_array(image)
    if lot_tags is None and parcels:
        lot_tags, scene_tag = scene_lot_tags.analyse(scene, parcels, seed=scene_lot_tags.address_seed(scene, root))
        parcels_sha256 = parcels_sha256 or scene_lot_tags.signature(parcels)
    lot_tags, scene_tag = lot_tags or {}, scene_tag or {}
    structure_rows, flags = structure_rows or {}, flags or {}
    px_m = _pixel_scale(scene)
    px_area = px_m*px_m
    grey = cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2GRAY).astype(float) if image is not None else None
    live = _live(scene)
    ceiling = luminance_ceiling(grey) if grey is not None and any(o.get('kind') == 'solar_panel' for o in live) else None
    seed_id = f"roof-{scene.get('address_id')}"
    shapes, pixels = {}, {}
    for obj in live:
        try:
            shapes[obj['id']] = local_polygon(obj, scene)
            pixels[obj['id']] = Polygon(obj['ring'], obj.get('holes') or [])
        except GEOMETRY_ERRORS:
            shapes[obj['id']] = None
            pixels[obj['id']] = None
    canopy = [(o, pixels[o['id']]) for o in live if o.get('kind') in CANOPY and o.get('status') not in DEAD_STATUSES
              and pixels[o['id']] is not None and pixels[o['id']].is_valid and pixels[o['id']].area > 0]
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    rows = []
    for obj in live:
        kind, oid = obj.get('kind'), obj['id']
        if kinds is not None and kind not in kinds:
            continue
        shape = shapes.get(oid)
        area = float(shape.area) if shape is not None and not shape.is_empty else None
        hints, outline, squared = [], None, None
        tag = lot_tags.get(oid) or {}
        # T1/T2: lot tags copied with their sentences; the unit-site sentence on every object of a unit site.
        hints.extend(lot_hints(tag))
        if scene_tag.get('unit_site') and scene_tag.get('unit_site_sentence'):
            hints.append(hint('unit_site', 'context', scene_tag['unit_site_sentence'], {'address_plan': scene_tag.get('address_plan')}))
        if scene_tag.get('title_mismatch_sentence') and oid == seed_id:
            hints.append(hint('unit_title_mismatch', 'context', scene_tag['title_mismatch_sentence'],
                              {'address_lot': scene_tag.get('address_lot'), 'address_plan': scene_tag.get('address_plan')}))
        if area is None or area <= 0:
            payload = {'version': VERSION, 'sentences_version': SENTENCES_VERSION, 'note': NOTE, 'kind': kind, 'status': obj.get('status'), 'area_m2': None, 'error': 'invalid outline',
                       'hints': hints, 'codes': [h['code'] for h in hints], 'review_count': sum(h['severity'] == 'review' for h in hints),
                       'squared_candidate': None, 'outline': None, 'solar': None, 'lot_ref': parcels_sha256, 'image_available': image is not None,
                       'code_sha256': code, 'computed_at': stamp}
            rows.append(_row(scene, obj, payload, VERSION))
            continue
        geometry = geometry_features(shape, len(obj.get('ring') or []))
        # T3: a canopy piece inside a much larger canopy object.
        if kind in CANOPY and area < DESIGN['canopy_fragment_inside']['area_m2'] and pixels.get(oid) is not None and pixels[oid].area > 0:
            design = DESIGN['canopy_fragment_inside']
            piece = pixels[oid]
            for other, container in canopy:
                if other['id'] == oid or container.area < design['container_ratio']*piece.area:
                    continue
                try:
                    cover = piece.intersection(container).area/piece.area
                except GEOMETRY_ERRORS:
                    continue
                if cover >= design['cover']:
                    container_m2 = container.area*px_area
                    hints.append(hint('canopy_fragment_inside', 'review',
                                      f"A {area:.1f} m² {kind} piece inside the {container_m2:.0f} m² {other['kind']} canopy; the model split one bush.",
                                      {'area_m2': _num(area), 'container_id': other['id'], 'container_kind': other['kind'],
                                       'container_area_m2': _num(container_m2, 1), 'cover': _num(cover)}))
                    break
        # T4: colour and size sanity.
        colour = None
        if kind == 'pool' and image is not None:
            colour = colour_features(obj, scene, image, grey)
            design = DESIGN['pool_not_blue']
            if colour and colour['green_share'] is not None and colour['green_share'] >= design['green_share'] and (colour['blue_share'] or 0) < design['blue_share']:
                hints.append(hint('pool_not_blue', 'review',
                                  f"Model says pool; the interior reads green {colour['green_share']*100:.0f}%, blue {colour['blue_share']*100:.0f}%, like grass. Check the image.",
                                  {'green_share': colour['green_share'], 'blue_share': colour['blue_share'], 'area_m2': _num(area)}))
        if kind == 'building' and image is not None and area < DESIGN['building_blends_into_surroundings']['area_m2']:
            colour = colour_features(obj, scene, image, grey)
            design = DESIGN['building_blends_into_surroundings']
            if (colour and colour['grey_std'] is not None and colour['ring_brightness'] is not None and colour['grey_std'] < design['grey_std']
                    and abs(colour['brightness']-colour['ring_brightness']) < design['brightness_delta']):
                hints.append(hint('building_blends_into_surroundings', 'review',
                                  f"{area:.1f} m², flat grey and no brighter than the ground around it; likely pavement.",
                                  {'area_m2': _num(area), 'grey_std': colour['grey_std'], 'brightness': colour['brightness'],
                                   'ring_brightness': colour['ring_brightness'], 'brightness_delta': _num(abs(colour['brightness']-colour['ring_brightness']), 2)}))
        tiny = DESIGN['tiny_area_for_kind'].get(kind)
        if tiny is not None and area < tiny:
            hints.append(hint('tiny_area_for_kind', 'review', f"{_fmt_area(area)} is small for a {kind.replace('_', ' ')}; check whether it is a piece of something else.",
                              {'area_m2': _num(area), 'threshold_m2': tiny}))
        width = geometry.get('rect_width_m')
        design = DESIGN['degenerate_outline']
        if area < design['area_m2'] or (width is not None and width < design['rect_width_m']):
            hints.append(hint('degenerate_outline', 'review',
                              f"{(width or 0):.2f} x {(geometry.get('rect_length_m') or 0):.2f} m outline ({_fmt_area(area)}); too thin or small to be a {kind.replace('_', ' ')} outline.",
                              {'area_m2': _num(area), 'rect_width_m': width, 'rect_length_m': geometry.get('rect_length_m'), 'vertices': geometry.get('vertex_count')}))
        # T5: the largest roof inside the lot polygon is primary; much smaller members are secondary.
        if kind == 'building' and obj.get('status') not in DEAD_STATUSES:
            design = DESIGN['secondary_by_lot']
            members = tag.get('lot_members') or []
            largest = members[0] if members else None
            mine = next((m for m in members if m['object_id'] == oid), None)
            if largest and mine and largest['object_id'] != oid and largest['area_m2'] > 0 and mine['area_m2']/largest['area_m2'] <= design['ratio']:
                ratio = mine['area_m2']/largest['area_m2']
                size = 'a shed-sized' if area < 10 else 'a garage-sized' if area < 60 else 'a smaller'
                lot = lot_name(tag.get('dominant'))
                hints.append(hint('secondary_by_lot', 'review',
                                  f"{area:.1f} m² beside the {largest['area_m2']:.1f} m² main roof on {lot} (ratio {ratio:.2f}); {size} secondary roof.",
                                  {'area_m2': _num(area), 'lot_primary': largest['object_id'], 'lot_primary_area_m2': largest['area_m2'], 'ratio': _num(ratio),
                                   'lot': lot, 'source': 'lot-tags-v1 lot_members'}))
            elif not members and flags.get(oid):
                payload = flags[oid].get('class_hints') or {}
                secondary = next((f for f in payload.get('flags') or [] if isinstance(f, dict) and f.get('flag') == 'secondary-structure'), None)
                if secondary:
                    evidence = secondary.get('evidence') or {}
                    largest_area = evidence.get('largest_same_property_area')
                    hints.append(hint('secondary_by_lot', 'review',
                                      f"{area:.1f} m² beside a {largest_area:.1f} m² roof on the same property; a secondary roof (building-flags-v2, no parcels here)."
                                      if isinstance(largest_area, (int, float)) else f"{area:.1f} m² flagged as a secondary structure (building-flags-v2, no parcels here).",
                                      {'area_m2': _num(area), 'largest_same_property_area': largest_area, 'source': 'building-flags-v2 secondary-structure'}))
        # T6: noisy outline and the squared candidate (quiet counts on every building >= 20 m²).
        if kind == 'building' and area >= DESIGN['noisy_outline']['area_m2']:
            try:
                outline = outline_metrics(shape, len(obj.get('ring') or []))
            except GEOMETRY_ERRORS:
                outline = None
            squared = squared_candidate(obj, scene) if area >= DESIGN['squared_candidate']['area_m2'] else None
            design = DESIGN['noisy_outline']
            if (outline and outline['svpm'] is not None and outline['wonky_share'] is not None
                    and outline['svpm'] >= design['svpm'] and outline['wonky_share'] >= design['wonky_share']):
                gate = 'passed' if squared and 'ring' in squared else 'refused'
                hints.append(hint('noisy_outline', 'review',
                                  f"{outline['vertices']} corners along {outline['perimeter_m']:.0f} m of edge, {outline['off_axis']} of them off the roof's axes; "
                                  f"roof edges are usually straight. Check the shadow side.",
                                  {**{k: outline[k] for k in ('vertices', 'perimeter_m', 'svpm', 'off_axis', 'edges_counted', 'wonky_share', 'rectilinearity')},
                                   'orthogonalise_gate': gate}))
        # T7: a roof model fitted on a fragment.
        structure = structure_rows.get(oid) or {}
        parts = obj.get('roof_parts') or []
        if parts and (area < DESIGN['roof_fit_on_fragment']['area_m2'] or structure.get('sub_kind') == 'fragment'):
            reason = f"{area:.1f} m²" if area < DESIGN['roof_fit_on_fragment']['area_m2'] else f"a {area:.1f} m² outline the structure hint calls a fragment"
            hints.append(hint('roof_fit_on_fragment', 'review',
                              f"A {obj.get('roof_type') or 'roof'} roof with {len(parts)} plane{'s' if len(parts) != 1 else ''} was fitted on {reason}; too small for a roof model.",
                              {'area_m2': _num(area), 'roof_parts': len(parts), 'roof_type': obj.get('roof_type'), 'structure_hint': structure.get('sub_kind')}))
        # T8 (object-hints-v2): a solar_panel that reads like a rooftop fixture. Measured on every solar panel (the quiet numbers ride
        # along as payload['solar']); the hint fires on any met reason and suggests nothing: the reviewer decides.
        solar = None
        if kind == 'solar_panel':
            solar = solar_measure(obj, shape, image, grey, ceiling)
            if solar['reasons']:
                hints.append(hint('solar_fixture_like', 'review', solar_sentence(solar), {**solar, 'reasons': list(solar['reasons'])}))
        payload = {'version': VERSION, 'sentences_version': SENTENCES_VERSION, 'note': NOTE, 'kind': kind, 'status': obj.get('status'), 'label': obj.get('label'), 'area_m2': _num(area),
                   'hints': hints, 'codes': [h['code'] for h in hints], 'review_count': sum(h['severity'] == 'review' for h in hints),
                   'squared_candidate': squared, 'outline': outline, 'colour': colour, 'solar': solar,
                   'lot': {k: tag.get(k) for k in ('dominant', 'lot_members', 'lot_primary', 'primary_ratio', 'address_plan', 'on_address_lot', 'lot_complete_in_image')} if tag else None,
                   'structure_hint': structure.get('sub_kind'), 'lot_ref': parcels_sha256, 'image_available': image is not None,
                   'code_sha256': code, 'computed_at': stamp}
        rows.append(_row(scene, obj, payload, VERSION))
    return rows


# --- Index access and CLI -------------------------------------------------------------------------------------

def load_scene(root, scene_id):
    return json.loads((Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())


def load_index_payloads(root, scene_ids, version, *, current_only=True):
    """{(scene_id, object_id): payload} for one version; current_only keeps rows whose model_signature matches latest.json."""
    root = Path(root)
    found = {}
    if not eyes_index.path(root).exists():
        return found
    signatures = {}
    for scene_id in dict.fromkeys(scene_ids):
        try:
            signatures[scene_id] = load_scene(root, scene_id).get('model_signature')
        except (OSError, ValueError):
            signatures[scene_id] = None
    with closing(eyes_index.connection(root, readonly=True)) as db:
        for scene_id in signatures:
            for row in eyes_index.rows(db, TABLE, scene_id=scene_id, version=version):
                if current_only and row.get('model_signature') != signatures[scene_id]:
                    continue
                if isinstance(row.get('payload'), dict):
                    found[(scene_id, row['object_id'])] = row['payload']
    return found


def stored_lot_tags(payloads, scene_id):
    """(lot_tags, scene_tag, parcels_sha256) from lot-tags-v1 index payloads of one scene."""
    tags, scene_tag, digest = {}, None, None
    for (sid, oid), payload in payloads.items():
        if sid != scene_id:
            continue
        digest = digest or payload.get('parcels_sha256')
        if oid == scene_lot_tags.SCENE_OBJECT_ID:
            scene_tag = payload.get('scene') or {}
        elif isinstance(payload.get('tag'), dict):
            tags[oid] = payload['tag']
    return tags, scene_tag, digest


def write_scene_rows(db, scene_ids, rows):
    """Replace the scenes' VERSION rows with `rows` and drop their PREVIOUS_VERSIONS rows in the same transaction: a scene rewritten
    under VERSION has no use for its older rows (readers would only fall back to them). Other scenes are untouched.
    Returns (written, removed_previous)."""
    removed = 0
    for scene_id in scene_ids:
        for version in PREVIOUS_VERSIONS:
            removed += db.execute(f'DELETE FROM "{TABLE}" WHERE scene_id=? AND version=?', (scene_id, version)).rowcount
    return replace_scene_rows(db, TABLE, VERSION, scene_ids, rows), removed  # replace_rows commits the deletes and inserts together


def run(scene_ids, root=Path('.'), *, write=False, offline=True, log=None):
    """Compute hints for scenes (lot tags from the index when current, else recomputed from frozen parcels) and, when
    write=True, replace the scenes' VERSION rows and drop their PREVIOUS_VERSIONS rows. Returns a summary dict."""
    root = Path(root)
    scene_ids = list(dict.fromkeys(validate_id(s) for s in scene_ids))
    structure = load_index_payloads(root, scene_ids, STRUCTURE_VERSION)
    flags = load_index_payloads(root, scene_ids, BUILDING_VERSION)
    stored = load_index_payloads(root, scene_ids, scene_lot_tags.VERSION)
    summary, rows = {'version': VERSION, 'note': NOTE, 'scenes': {}}, []
    for scene_id in scene_ids:
        scene = load_scene(root, scene_id)
        image = load_image(root, scene)
        tags, scene_tag, digest = stored_lot_tags(stored, scene_id)
        source = 'lot-tags-v1 index rows'
        if not tags:
            parcels, digest, source = scene_lot_tags.parcels_for_scene(scene, root, offline=offline)
            if parcels:
                tags, scene_tag = scene_lot_tags.analyse(scene, parcels, seed=scene_lot_tags.address_seed(scene, root))
            else:
                tags, scene_tag = {}, None
        scene_rows = hints_for_scene(scene, image, lot_tags=tags, scene_tag=scene_tag, parcels_sha256=digest,
                                     structure_rows={oid: p for (sid, oid), p in structure.items() if sid == scene_id},
                                     flags={oid: p for (sid, oid), p in flags.items() if sid == scene_id}, root=root)
        counts = {}
        for row in scene_rows:
            for code_ in row['payload']['codes']:
                counts[code_] = counts.get(code_, 0)+1
        summary['scenes'][scene_id] = {'objects': len(scene_rows), 'image': image is not None, 'lot_tags': source, 'flags': counts,
                                       'squared_candidates': sum(1 for r in scene_rows if (r['payload'].get('squared_candidate') or {}).get('ring'))}
        rows.extend(scene_rows)
        if log:
            log(f'{scene_id}: {len(scene_rows)} objects, image={"yes" if image is not None else "no"}, lot tags {source}, {counts}')
    if write and rows:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            summary['written'], summary['removed_previous'] = write_scene_rows(db, scene_ids, rows)
    else:
        summary['written'], summary['removed_previous'] = 0, 0
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=f'Object hints ({VERSION}): evidence-backed sentences per scene object; agent hints, never labels.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='+', required=True)
    parser.add_argument('--write', action='store_true', help=f'Write {VERSION} rows to the eyes index, replacing the scenes\' {VERSION} rows and dropping their '
                                                             f'{", ".join(PREVIOUS_VERSIONS)} rows.')
    parser.add_argument('--fetch-parcels', action='store_true', help='Allow one bounded Vicmap read per scene without lot tags or frozen parcels.')
    parser.add_argument('--report', help='Also write the summary here (under data/reviews/eyes).')
    args = parser.parse_args(argv)
    root = Path(args.root)
    out = run(args.scenes, root, write=args.write, offline=not args.fetch_parcels, log=lambda line: print(line, file=sys.stderr))
    if args.report:
        path = scene_lot_tags._report_path(root, args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1))
    json.dump(out, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
