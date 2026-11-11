"""Sub-kind hints for small or secondary building candidates (structure-kinds-v2).

The user's queue review asked for secondary structures finer than "secondary
structure": shipping container, shed, garage, carport, pergola or shade, water
tank, and slivers or fragments. This module is both property enrichment and a
pre-roof analysis: a candidate whose sub-kind is not a primary roof should not
be fitted as a house roof. Every live building candidate in a scene that is
small (area below 150 m²) or carries the building-flags-v2 'secondary-structure'
flag gets one row in the eyes index (table object_quality, version
structure-kinds-v2; the v1 rows stay) with the sub-kind, its confidence, the rule that fired and
every measured value. Rows are agent hints, never labels; nothing here rewrites
latest.json or any production artifact, and other index versions are untouched.

Feature groups (independent evidence; confidence counts how many agree):
  geometry    outline in local metres: minimum rotated rectangle width/length,
              aspect, rectilinearity (area / rectangle area), circularity
              (4πA/P²), vertex count.
  appearance  a CLAHE-enhanced crop (the editor's Enhanced contrast recipe:
              LAB, clipLimit 3.0, tile 12x12 on L): gradient-orientation
              anisotropy (ribbed corrugation), rib period along the axis
              perpendicular to the ribs, rib direction relative to the long
              axis, interior variance relative to a 2 m ring outside
              (see-through carports and pergolas show the ground through them),
              median brightness and saturation, the share of bright uniform
              (driveway-like) surface in a 3 m ring, and the share of interior
              pixels in the blue-cyan hue range with real saturation (pool water
              or a pool cover).
  context     other buildings in the scene: attachment to a larger roof, distance
              to the nearest larger and the largest roof within 40 m, the
              building-flags-v2 secondary and non-residential-context flags, the
              class hint and the same-property largest area, image-edge contact.
  sam3        optional (--sam3): per prompt the best-IoU SAM3 proposal for the
              object and its score. The useful signal measured so far is the
              negative one: a container gets no 'house' / 'building roof'
              proposal while the main house does. A 'swimming pool' proposal
              with IoU >= 0.5 is a positive pool-cover signal.

Version 2 (26 September 2026) adds four sub-kinds, each a rule with design values
stated before the suburb was re-run and not tuned on it:
  container-stack     two, three or four shipping containers side by side: the
                      rectangle width within 0.3 m of 2, 3 or 4 container widths
                      (4.5-5.1, 6.9-7.5, 9.3-9.9 m) AND a standard container
                      length, 20 ft or 40 ft within 0.5 m (5.6-6.6 or
                      11.7-12.7 m), rectilinearity >= 0.90. Ribs only add
                      confidence: at 4 px/m a container's 0.28 m corrugation is
                      about one pixel and rib_anisotropy aliases (median 0.263
                      over all eligible objects), so it cannot carry the rule.
                      A box whose width matches but whose length is not a
                      container length gets a note in the evidence and falls
                      through to garage as before.
  portable-classroom  a single-width demountable (2.8-4.2 x 6-13 m, rectilinearity
                      >= 0.85) whose building-flags-v2 payload carries the
                      'non-residential-context' flag or a 'commercial' /
                      'non-residential' class hint; runs before shipping-container
                      because the dimensions overlap and the context decides.
  pool-cover          blue-cyan share of the interior (OpenCV hue 85-110,
                      saturation >= 110 and value >= 130 of 255, so dark
                      blue-grey Colorbond roofs do not count) >= 0.5 with
                      10-60 m² and rectilinearity >= 0.7, or a SAM3 'swimming
                      pool' proposal with IoU >= 0.5. An uncovered pool detected as a building
                      also lands here; the evidence (pool_blue_share, the SAM3
                      prompt) says which.
  carport (new rule)  12-45 m², rectilinearity >= 0.7, attached to a larger roof
                      or with a larger roof within 2 m, and driveway_like_share_3m
                      >= 0.15 (bright, low-saturation, low-gradient paving in the
                      3 m ring; the share never reached 0.35 in the suburb, max
                      0.311, p95 0.10). Always weak: it is a context guess. The
                      v1 see-through carport branch is gone; pergola-or-shade
                      keeps the see-through rule alone.

Design values live in DESIGN and the rule texts in RULES. They were stated
before the evaluation sample was classified. Three rules were then corrected
after a visual review of a random per-kind sample of the suburb rows on
26 September 2026, before first publication (the version string is unchanged):
water-tank now needs circularity >= 0.90, at least 8 vertices and
rectilinearity <= 0.85 (square pyramid-roof sheds passed the 0.8 gate);
shipping-container dropped the double-width branch and the rib requirement
(ordinary 37-45 m² sheds passed as doubles while smooth white 2.4 x 12 m units
were called sheds) and now takes precedence over shed on width 2.0-3.2 m,
length 5.0-13.5 m and rectilinearity >= 0.80, with ribs only raising
confidence; carport and pergola-or-shade fired only on
interior_relative_variance >= 1.2 with brightness_median < 170 and
rectilinearity >= 0.7, always weak, because the variance ratio at 0.6 was
labelling solid roofs as see-through (in v2 that see-through rule is
pergola-or-shade's alone). Those observations are agent observations, not
labels.

Rule order (v2): fragment, water-tank, pool-cover, portable-classroom,
shipping-container, container-stack, carport, pergola-or-shade, shed, garage,
outbuilding / not-secondary. Confidence counts the independent feature groups
that agree, as before.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
import warnings
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon

from . import eyes_index
from .scene_object_quality import BUILDING_VERSION, _row, local_polygon, replace_scene_rows
from .scene_self_review import GEOMETRY_ERRORS, validate_id

VERSION = 'structure-kinds-v3'
NOTE = 'agent hint, not a label'
TABLE = 'object_quality'
REPORT_DIR = Path('data/reviews/eyes/structure-kinds')
SUB_KINDS = ('shipping-container', 'container-stack', 'portable-classroom', 'pool-cover', 'shed', 'garage', 'carport',
             'attached-cover', 'pergola-or-shade', 'water-tank', 'fragment', 'outbuilding', 'not-secondary')
LABELS = {'shipping-container': 'Shipping container', 'container-stack': 'Container stack', 'portable-classroom': 'Portable classroom',
          'pool-cover': 'Pool cover', 'shed': 'Shed', 'garage': 'Garage', 'carport': 'Carport', 'attached-cover': 'Patio or carport cover',
          'pergola-or-shade': 'Pergola or shade',
          'water-tank': 'Water tank', 'fragment': 'Fragment', 'outbuilding': 'Outbuilding', 'not-secondary': 'Not secondary'}
CONFIDENCES = ('weak', 'moderate', 'strong')
GROUPS = ('geometry', 'appearance', 'context', 'sam3')
GROUP_SHORT = {'geometry': 'geo', 'appearance': 'app', 'context': 'ctx', 'sam3': 'sam3'}
SAM3_PROMPTS = ('house', 'building roof', 'shipping container', 'garden shed', 'garage', 'carport', 'water tank', 'swimming pool')
SAM3_ROOF_PROMPTS = ('house', 'building roof')
SAM3_POSITIVE_PROMPT = {'shipping-container': 'shipping container', 'container-stack': 'shipping container', 'shed': 'garden shed',
                        'garage': 'garage', 'carport': 'carport', 'water-tank': 'water tank', 'pool-cover': 'swimming pool'}
NON_RESIDENTIAL_FLAG = 'non-residential-context'
NON_RESIDENTIAL_CLASS_HINTS = ('commercial', 'non-residential')
SAM3_MODELS = Path('scripts/ml/models/sam3')
DEAD_STATUSES = ('rejected', 'superseded')
CLAHE = {'clip_limit': 3.0, 'tile_grid': (12, 12)}

# Design values: stated before the evaluation, not tuned on it. Lengths in metres, areas in m², angles in degrees,
# brightness on the 0-255 L scale of the enhanced crop, saturation on 0-1.
DESIGN = {
    'eligible_area_m2': 150.,
    'erode_m': .5, 'ring_m': 2., 'driveway_ring_m': 3., 'attached_m': .5, 'largest_within_m': 40.,
    'orientation_bins': 36, 'rib_window_deg': 10., 'rib_period_min_acf': .2, 'min_pixels': 12,
    'fragment': {'area_m2': 6., 'rect_width_m': 1.5},
    # 0.96: a visual review of all 20 hits at 0.90 found only pyramid-roof sheds with rounded SAM outlines (0.90-0.95).
    'water-tank': {'circularity': .96, 'vertex_count': 8, 'area_m2': (3., 40.), 'rectilinearity': .85},
    'shipping-container': {'width_m': (2., 3.2), 'length_m': (5., 13.5), 'rectilinearity': .8},
    # v2. Widths: 2, 3 or 4 units of 2.4 m within 0.3 m (4.5-5.1, 6.9-7.5, 9.3-9.9). Lengths: 20 ft (6.1 m) or 40 ft (12.2 m)
    # within 0.5 m (5.6-6.6, 11.7-12.7). Ribs are not required (rib_anisotropy aliases at 4 px/m); they only add confidence.
    'container-stack': {'unit_width_m': 2.4, 'units': (2, 3, 4), 'width_tolerance_m': .3, 'standard_lengths_m': (6.1, 12.2),
                        'length_tolerance_m': .5, 'rectilinearity': .90},
    'portable-classroom': {'width_m': (2.8, 4.2), 'length_m': (6., 13.), 'rectilinearity': .85},
    # OpenCV hue on 0-179 (85-110 is cyan to blue), saturation on 0-255 of the HSV crop.
    'pool-cover': {'blue_share': .5, 'hue': (85, 110), 'saturation': 110, 'value': 130, 'area_m2': (10., 60.), 'rectilinearity': .7,
                   'sam3_iou': .5},
    'carport': {'area_m2': (12., 45.), 'rectilinearity': .7, 'near_larger_m': 2., 'driveway_share': .15},
    'see-through': {'interior_relative_variance': 1.2, 'brightness': 170., 'rectilinearity': .7, 'area_m2': (10., 60.)},
    # v3 (user review of house-25822, 27 Sep): SAM outlines of small structures are wobbly (a 28.6 m² shed traced with 36
    # vertices fills 0.69 of its rectangle), so the rectangle-fill floor for sheds and garages drops to 0.65; and a small roof
    # attached to the primary roof is a patio or carport cover (an attachment), not a shed.
    'shed': {'area_m2': (4., 36.), 'rectilinearity': .65},
    'garage': {'area_m2': (30., 120.), 'rectilinearity': .65},
    'attached-cover': {'area_m2': (6., 30.), 'rectilinearity': .55},
    'ribbed': .35,
    'driveway': {'brightness': 140., 'saturation': .22, 'gradient': 30.},
    'primary_area_m2': 90.,
    'sam3': {'no_house_iou': .3, 'positive_iou': .5, 'threshold': .05, 'max_proposals': 64},
}
RULES = {
    'fragment': 'area_m2 < 6 or rect_width_m < 1.5',
    'water-tank': 'circularity >= 0.96 and vertex_count >= 8 and rectilinearity <= 0.85 and 3 <= area_m2 <= 40',
    'pool-cover': ('(pool_blue_share >= 0.5 [OpenCV hue 85-110, saturation >= 110, value >= 130] and 10 <= area_m2 <= 60 and rectilinearity >= 0.7) '
                   "or SAM3 'swimming pool' best_iou >= 0.5; a pool itself detected as a building also lands here (see the evidence)"),
    'portable-classroom': ('2.8 <= rect_width_m <= 4.2 and 6 <= rect_length_m <= 13 and rectilinearity >= 0.85 and the building-flags-v2 '
                           "payload has the 'non-residential-context' flag or class_hint 'commercial'/'non-residential' (before shipping-container)"),
    'shipping-container': ('2.0 <= rect_width_m <= 3.2 and 5.0 <= rect_length_m <= 13.5 and rectilinearity >= 0.80 '
                           '(before shed; ribs only raise confidence)'),
    'container-stack': ('rect_width_m within 0.3 m of 2, 3 or 4 container widths (4.5-5.1, 6.9-7.5, 9.3-9.9 m) and rect_length_m within 0.5 m '
                        'of a 20 ft or 40 ft container (5.6-6.6 or 11.7-12.7 m) and rectilinearity >= 0.90 (before garage; ribs only raise confidence)'),
    'attached-cover': '6 <= area_m2 < 30 and rectilinearity >= 0.55 and attached to a larger roof (nearest larger roof within 0.5 m): a patio, veranda or carport cover, not a shed',
    'carport': ('12 <= area_m2 <= 45 and rectilinearity >= 0.7 and (attached_to_larger_roof or nearest_larger_roof_m <= 2) '
                'and driveway_like_share_3m >= 0.15; always weak (a context guess)'),
    'pergola-or-shade': ('interior_relative_variance >= 1.2 and brightness_median < 170 and rectilinearity >= 0.7 and 10 <= area_m2 <= 60 '
                         '(see-through) and the carport rule did not fire; always weak'),
    'shed': '4 <= area_m2 <= 36 and rectilinearity >= 0.75; an attached 30-36 m² box is a garage',
    'garage': '30 <= area_m2 <= 120 and rectilinearity >= 0.75 and (attached_to_larger_roof or secondary_structure_flag)',
    'outbuilding': 'secondary_structure_flag set (or SAM3: no house/roof proposal and a secondary prompt >= 0.5) and no finer rule fired',
    'not-secondary': 'no rule fired and no secondary flag: treated as a primary roof',
    'confidence': 'strong when >= 3 independent feature groups agree, moderate for 2, weak for 1',
}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# --- Scene access -------------------------------------------------------------------------------------------

def load_scene(root, scene_id):
    return json.loads((Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json').read_text())


def load_image(root, scene):
    """The scene PNG (<image_sha256[:8]>*.png) as an RGB uint8 array; None when missing or the wrong size."""
    digest = str(scene.get('image_sha256') or '')
    if len(digest) < 8:
        return None
    folder = Path(root)/'data/scenes'/validate_id(scene['id'])
    for candidate in sorted(folder.glob(f'{digest[:8]}*.png')):
        try:
            with Image.open(candidate) as opened:
                image = opened.convert('RGB')
        except OSError:
            continue
        if image.size == (scene['image_width'], scene['image_height']):
            return np.asarray(image)
    return None


def live_buildings(scene):
    return [o for o in scene.get('objects', []) if o.get('kind') == 'building' and o.get('status') not in DEAD_STATUSES]


def building_shapes(scene):
    shapes = {}
    for obj in live_buildings(scene):
        try:
            shape = local_polygon(obj, scene)
        except GEOMETRY_ERRORS:
            continue
        if shape is not None and not shape.is_empty and shape.area > 0:
            shapes[obj['id']] = shape
    return shapes


def load_flags(root, scene_ids):
    """{(scene_id, object_id): building-flags-v2 payload} from the eyes index; empty when the index is absent."""
    flags = {}
    if not eyes_index.path(root).exists():
        return flags
    with closing(eyes_index.connection(root, readonly=True)) as db:
        for scene_id in dict.fromkeys(scene_ids):
            for row in eyes_index.rows(db, TABLE, scene_id=scene_id, version=BUILDING_VERSION):
                if isinstance(row.get('payload'), dict):
                    flags[(scene_id, row['object_id'])] = row['payload']
    return flags


def flag_evidence(payload):
    """(secondary flag set, largest_same_property_area) from a building-flags-v2 payload (or None)."""
    hints = (payload or {}).get('class_hints') or {}
    flags = {f.get('flag') for f in hints.get('flags') or [] if isinstance(f, dict)}
    largest = ((hints.get('features') or {}).get('secondary_structure') or {}).get('largest_same_property_area')
    return 'secondary-structure' in flags, largest


def non_residential_evidence(payload):
    """(non-residential-context flag set, class_hint) from a building-flags-v2 payload (or None)."""
    hints = (payload or {}).get('class_hints') or {}
    flags = {f.get('flag') for f in hints.get('flags') or [] if isinstance(f, dict)}
    hint = hints.get('class_hint')
    return NON_RESIDENTIAL_FLAG in flags, str(hint) if hint else None


# --- Geometry -----------------------------------------------------------------------------------------------

def _num(value, digits=4):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def geometry_features(shape, vertex_count=None):
    """Minimum rotated rectangle width/length, aspect, rectilinearity, circularity and the long-axis angle."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)  # GEOS oriented_envelope emits a spurious invalid-value warning
        rect = shape.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)[:4] if rect.geom_type == 'Polygon' else []
    if len(coords) == 4:
        sides = [math.dist(coords[i], coords[(i+1) % 4]) for i in range(2)]
        long_index = 0 if sides[0] >= sides[1] else 1
        a, b = coords[long_index], coords[long_index+1]
        axis_deg = math.degrees(math.atan2(b[1]-a[1], b[0]-a[0])) % 180.
        width, length = min(sides), max(sides)
    else:
        width = length = axis_deg = None
    perimeter = shape.length
    return {'area_m2': _num(shape.area, 3), 'rect_width_m': _num(width, 3), 'rect_length_m': _num(length, 3),
            'aspect': _num(length/width) if width else None,
            'rectilinearity': _num(shape.area/(width*length)) if width and length else None,
            'circularity': _num(4*math.pi*shape.area/perimeter**2) if perimeter > 0 else None,
            'vertex_count': int(vertex_count if vertex_count is not None else len(shape.exterior.coords)-1) if shape.geom_type == 'Polygon' else None,
            'long_axis_deg': _num(axis_deg, 2)}


# --- Appearance ---------------------------------------------------------------------------------------------

def enhance(rgb):
    """CLAHE on the LAB L channel (clipLimit 3.0, tile 12x12), the editor's Enhanced contrast recipe."""
    data = np.ascontiguousarray(np.asarray(rgb, dtype=np.uint8))
    lab = cv2.cvtColor(data, cv2.COLOR_RGB2LAB)
    clahe = cv2.createCLAHE(clipLimit=CLAHE['clip_limit'], tileGridSize=CLAHE['tile_grid'])
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return np.ascontiguousarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


def pixel_size_m(scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    return float(math.sqrt(abs(np.linalg.det(frame[:2, :2]))))


def to_pixels(shape, scene):
    f = np.asarray(scene['meters_to_pixel'], dtype=float)
    return affine_transform(shape, [f[0, 0], f[1, 0], f[0, 1], f[1, 1], f[2, 0], f[2, 1]])


def _raster(geom, origin, size):
    """Boolean mask of a (Multi)Polygon in pixel space, drawn into a crop of `size` (h, w) whose top-left is `origin`."""
    mask = np.zeros(size, dtype=np.uint8)
    if geom is None or geom.is_empty:
        return mask.astype(bool)
    parts = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    ox, oy = origin
    for part in parts:
        if part.geom_type != 'Polygon' or part.is_empty:
            continue
        pts = np.asarray(part.exterior.coords, dtype=np.float64) - [ox, oy]
        cv2.fillPoly(mask, [np.round(pts).astype(np.int32)], 1)
        for hole in part.interiors:
            pts = np.asarray(hole.coords, dtype=np.float64) - [ox, oy]
            cv2.fillPoly(mask, [np.round(pts).astype(np.int32)], 0)
    return mask.astype(bool)


def _orientation_histogram(gx, gy, mask, bins):
    magnitude = np.hypot(gx, gy)[mask]
    angle = (np.degrees(np.arctan2(gy, gx))[mask]) % 180.
    hist, _ = np.histogram(angle, bins=bins, range=(0., 180.), weights=magnitude)
    return hist, angle, magnitude


def _rib_period(gray, mask, theta_deg, px_m):
    """Dominant spatial period (m) of the mean profile along the gradient direction; None when the peak is weak."""
    ys, xs = np.nonzero(mask)
    if len(xs) < DESIGN['min_pixels']:
        return None
    u = np.array([math.cos(math.radians(theta_deg)), math.sin(math.radians(theta_deg))])
    position = xs*u[0] + ys*u[1]
    index = np.round(position-position.min()).astype(int)
    sums = np.bincount(index, weights=gray[ys, xs])
    counts = np.bincount(index)
    valid = counts >= 3
    if valid.sum() < 8:
        return None
    profile = np.where(valid, sums/np.maximum(counts, 1), np.nan)
    profile = np.interp(np.arange(len(profile)), np.nonzero(valid)[0], profile[valid])
    profile = profile - profile.mean()
    energy = float(np.dot(profile, profile))
    if energy <= 0:
        return None
    acf = np.correlate(profile, profile, 'full')[len(profile)-1:]/energy
    for lag in range(2, len(acf)-1):
        if acf[lag] >= acf[lag-1] and acf[lag] >= acf[lag+1] and acf[lag] >= DESIGN['rib_period_min_acf'] and acf[lag-1] < acf[lag]:
            return _num(lag*px_m, 3)
    return None


def appearance_features(enhanced, shape, scene, geometry, others=()):
    """Appearance measurements on the enhanced image inside the eroded outline and in rings outside it."""
    features = {'brightness_median': None, 'saturation_median': None, 'rib_anisotropy': None, 'rib_period_m': None,
                'rib_axis_alignment': None, 'interior_relative_variance': None, 'driveway_like_share_3m': None,
                'pool_blue_share': None, 'interior_pixels': 0, 'image': 'unavailable'}
    if enhanced is None:
        return features
    px_m = pixel_size_m(scene)
    height, width = enhanced.shape[:2]
    inner = shape.buffer(-DESIGN['erode_m'])
    if inner.is_empty:
        inner = shape
    outer = shape.buffer(DESIGN['driveway_ring_m'])
    ring2 = shape.buffer(DESIGN['ring_m']).difference(shape)
    ring3 = outer.difference(shape)
    for other in others:
        if other.intersects(outer):
            ring2, ring3 = ring2.difference(other), ring3.difference(other)
    bounds = to_pixels(outer, scene).bounds
    x0, y0 = max(int(math.floor(bounds[0]))-2, 0), max(int(math.floor(bounds[1]))-2, 0)
    x1, y1 = min(int(math.ceil(bounds[2]))+3, width), min(int(math.ceil(bounds[3]))+3, height)
    if x1-x0 < 3 or y1-y0 < 3:
        features['image'] = 'outline outside the frame'
        return features
    size, origin = (y1-y0, x1-x0), (x0, y0)
    crop = enhanced[y0:y1, x0:x1]
    inside = _raster(to_pixels(inner, scene), origin, size)
    ring2_px = _raster(to_pixels(ring2, scene), origin, size)
    ring3_px = _raster(to_pixels(ring3, scene), origin, size)
    features['interior_pixels'] = int(inside.sum())
    features['image'] = 'clahe'
    if features['interior_pixels'] < DESIGN['min_pixels']:
        features['image'] = 'outline too thin for interior statistics'
        return features
    lab = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_RGB2LAB)
    hsv = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_RGB2HSV)
    lum = lab[:, :, 0].astype(np.float32)
    sat = hsv[:, :, 1].astype(np.float32)/255.
    features['brightness_median'] = _num(np.median(lum[inside]), 2)
    features['saturation_median'] = _num(np.median(sat[inside]), 4)
    features['pool_blue_share'] = _num(pool_blue_share(hsv, inside))
    gray = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_RGB2GRAY).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    hist, angle, magnitude = _orientation_histogram(gx, gy, inside, DESIGN['orientation_bins'])
    total = float(magnitude.sum())
    if total > 0:
        bin_width = 180./DESIGN['orientation_bins']
        dominant = (int(np.argmax(hist))+.5)*bin_width
        delta = np.abs((angle-dominant+90.) % 180.-90.)
        features['rib_anisotropy'] = _num(magnitude[delta <= DESIGN['rib_window_deg']].sum()/total)
        features['rib_period_m'] = _rib_period(gray, inside, dominant, px_m)
        rib_direction = (dominant+90.) % 180.   # ribs run perpendicular to their gradient
        if geometry.get('long_axis_deg') is not None:
            axis_px = _pixel_axis_deg(geometry['long_axis_deg'], scene)
            features['rib_axis_alignment'] = _num(abs((rib_direction-axis_px+90.) % 180.-90.), 2)
    if ring2_px.sum() >= DESIGN['min_pixels']:
        outside_var = float(np.var(lum[ring2_px]))
        if outside_var > 0:
            features['interior_relative_variance'] = _num(float(np.var(lum[inside]))/outside_var)
    if ring3_px.sum() >= DESIGN['min_pixels']:
        grad = np.hypot(gx, gy)
        d = DESIGN['driveway']
        driveway = (lum >= d['brightness']) & (sat <= d['saturation']) & (grad <= d['gradient'])
        features['driveway_like_share_3m'] = _num(driveway[ring3_px].mean())
    return features


def pool_blue_share(hsv, mask):
    """Share of `mask` pixels whose OpenCV hue is in the pool-cover blue-cyan band with saturation at or above the design value."""
    pool = DESIGN['pool-cover']
    hue, sat, val = hsv[:, :, 0][mask], hsv[:, :, 1][mask], hsv[:, :, 2][mask]
    if hue.size == 0:
        return None
    blue = (hue >= pool['hue'][0]) & (hue <= pool['hue'][1]) & (sat >= pool['saturation']) & (val >= pool['value'])
    return float(blue.mean())


def _pixel_axis_deg(axis_deg_m, scene):
    """A direction given in local metres, expressed in image pixel space (the frame may flip y)."""
    f = np.asarray(scene['meters_to_pixel'], dtype=float)[:2, :2]
    v = np.array([math.cos(math.radians(axis_deg_m)), math.sin(math.radians(axis_deg_m))]) @ f
    return math.degrees(math.atan2(v[1], v[0])) % 180.


# --- Context ------------------------------------------------------------------------------------------------

def context_features(obj, shape, shapes, scene, flag_payload=None):
    others = [(oid, s) for oid, s in shapes.items() if oid != obj['id']]
    nearest_larger = min((s.distance(shape) for _, s in others if s.area > shape.area), default=None)
    within = [(oid, s, s.distance(shape)) for oid, s in others if s.distance(shape) <= DESIGN['largest_within_m']]
    largest = max(within, key=lambda item: (item[1].area, item[0])) if within else None
    ring = np.asarray(obj.get('ring') or [], dtype=float).reshape(-1, 2)
    touches = bool(len(ring) and (ring[:, 0].min() <= 1 or ring[:, 1].min() <= 1 or
                                  ring[:, 0].max() >= scene['image_width']-2 or ring[:, 1].max() >= scene['image_height']-2))
    flagged, largest_property = flag_evidence(flag_payload)
    non_residential, class_hint = non_residential_evidence(flag_payload)
    return {'attached_to_larger_roof': bool(nearest_larger is not None and nearest_larger < DESIGN['attached_m']),
            'nearest_larger_roof_m': _num(nearest_larger, 3),
            'distance_to_largest_roof_m': _num(largest[2], 3) if largest else None,
            'largest_roof_within_40m_area': _num(largest[1].area, 3) if largest else None,
            'largest_roof_within_40m': largest[0] if largest else None,
            'largest_same_property_area': _num(largest_property, 3),
            'secondary_structure_flag': bool(flagged), 'non_residential_context_flag': bool(non_residential),
            'class_hint': class_hint, 'flags_row_present': flag_payload is not None,
            'touches_image_edge': touches}


# --- SAM3 ---------------------------------------------------------------------------------------------------

class Sam3Scorer:
    """Encodes a tile once and scores every object polygon against the best-IoU proposal of each prompt."""

    def __init__(self, models_dir=SAM3_MODELS, prompts=SAM3_PROMPTS):
        from .sam3 import Sam3TextSegmenter
        self.segmenter = Sam3TextSegmenter(Path(models_dir))
        self.prompts = tuple(prompts)
        self.timings = []

    def score_scene(self, image_rgb, masks):
        """masks: {object_id: full-frame bool mask}; returns {object_id: {'prompts': {...}, 'image': 'original'}}."""
        started = time.monotonic()
        pil = Image.fromarray(np.asarray(image_rgb, dtype=np.uint8))
        embedding = self.segmenter.encode_image(pil)
        encode_s = time.monotonic()-started
        areas = {oid: int(m.sum()) for oid, m in masks.items()}
        results = {oid: {'prompts': {}, 'image': 'original'} for oid in masks}
        for prompt in self.prompts:
            proposals = self.segmenter.proposals(embedding, prompt, pil.size, threshold=DESIGN['sam3']['threshold'],
                                                 max_proposals=DESIGN['sam3']['max_proposals'])
            for oid, mask in masks.items():
                best = {'best_iou': 0., 'score': None}
                for proposal in proposals:
                    pm = _fit_mask(proposal.mask, mask.shape)
                    inter = int(np.count_nonzero(pm & mask))
                    if inter == 0:
                        continue
                    iou = inter/(int(np.count_nonzero(pm))+areas[oid]-inter)
                    if iou > best['best_iou']:
                        best = {'best_iou': _num(iou), 'score': _num(proposal.score)}
                results[oid]['prompts'][prompt] = best
        self.timings.append({'encode_s': round(encode_s, 2), 'total_s': round(time.monotonic()-started, 2), 'objects': len(masks)})
        return results


def _fit_mask(mask, shape):
    mask = np.asarray(mask)
    if mask.shape == shape:
        return mask.astype(bool)
    resized = cv2.resize(mask.astype(np.uint8), (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    return resized.astype(bool)


def sam3_signals(sam3):
    """Derived booleans from the SAM3 evidence: no house/roof proposal, and which secondary prompts fit well."""
    if not sam3 or not sam3.get('prompts'):
        return {'available': False, 'no_house': None, 'house_fit': None, 'positive': set(), 'pool': False}
    prompts = sam3['prompts']
    roof = [prompts.get(p, {}).get('best_iou') or 0. for p in SAM3_ROOF_PROMPTS if p in prompts]
    house_iou = max(roof, default=None)
    positive = {kind for kind, prompt in SAM3_POSITIVE_PROMPT.items()
                if (prompts.get(prompt, {}).get('best_iou') or 0.) >= DESIGN['sam3']['positive_iou']}
    pool_iou = prompts.get(SAM3_POSITIVE_PROMPT['pool-cover'], {}).get('best_iou') or 0.
    return {'available': True, 'no_house': None if house_iou is None else house_iou < DESIGN['sam3']['no_house_iou'],
            'house_fit': None if house_iou is None else house_iou >= DESIGN['sam3']['positive_iou'], 'positive': positive,
            'pool': pool_iou >= DESIGN['pool-cover']['sam3_iou']}


# --- Rules --------------------------------------------------------------------------------------------------

def _between(value, bounds):
    return value is not None and bounds[0] <= value <= bounds[1]


def confidence_for(groups):
    n = len(set(groups))
    return 'strong' if n >= 3 else 'moderate' if n == 2 else 'weak'


def classify(geometry, appearance, context, sam3=None):
    """(sub_kind, confidence, rule text, agreeing groups) from the feature groups; see RULES and DESIGN."""
    g, a, c, d = geometry, appearance, context, DESIGN
    s = sam3_signals(sam3)
    area, width, length = g.get('area_m2') or 0., g.get('rect_width_m'), g.get('rect_length_m')
    rect, circ = g.get('rectilinearity'), g.get('circularity')
    aniso, irv = a.get('rib_anisotropy'), a.get('interior_relative_variance')
    ribbed = aniso is not None and aniso >= d['ribbed']
    st = d['see-through']
    see_through = (irv is not None and irv >= st['interior_relative_variance'] and a.get('brightness_median') is not None
                   and a['brightness_median'] < st['brightness'] and rect is not None and rect >= st['rectilinearity'])
    flagged, attached = c.get('secondary_structure_flag'), c.get('attached_to_larger_roof')
    near_larger = c.get('largest_roof_within_40m_area') is not None and c['largest_roof_within_40m_area'] > area
    detached_secondary = flagged or (near_larger and not attached)

    def sam3_group(kind):
        return s['available'] and (s['no_house'] or kind in s['positive'])

    def result(kind, groups):
        groups = [x for x in groups if x]
        return kind, confidence_for(groups), RULES[kind], sorted(set(groups))

    if area < d['fragment']['area_m2'] or (width is not None and width < d['fragment']['rect_width_m']):
        return result('fragment', ['geometry', 'context' if attached or c.get('touches_image_edge') else None,
                                   'sam3' if s['available'] and s['no_house'] else None])
    tank = d['water-tank']
    if (circ is not None and circ >= tank['circularity'] and (g.get('vertex_count') or 0) >= tank['vertex_count']
            and _between(area, tank['area_m2']) and rect is not None and rect <= tank['rectilinearity']):
        smooth = aniso is not None and not ribbed and not see_through
        return result('water-tank', ['geometry', 'appearance' if smooth else None, 'context' if detached_secondary else None,
                                     'sam3' if sam3_group('water-tank') else None])
    pool = d['pool-cover']
    blue = a.get('pool_blue_share') is not None and a['pool_blue_share'] >= pool['blue_share']
    pool_geometry = _between(area, pool['area_m2']) and rect is not None and rect >= pool['rectilinearity']
    if (blue and pool_geometry) or (s['available'] and s['pool']):
        return result('pool-cover', ['appearance' if blue else None, 'geometry' if pool_geometry else None,
                                     'context' if detached_secondary else None, 'sam3' if s['available'] and s['pool'] else None])
    room = d['portable-classroom']
    non_residential = bool(c.get('non_residential_context_flag')) or c.get('class_hint') in NON_RESIDENTIAL_CLASS_HINTS
    if (non_residential and _between(width, room['width_m']) and _between(length, room['length_m'])
            and rect is not None and rect >= room['rectilinearity']):
        return result('portable-classroom', ['geometry', 'context', 'appearance' if aniso is not None and not ribbed else None,
                                             'sam3' if s['available'] and s['no_house'] else None])
    box = d['shipping-container']
    if _between(width, box['width_m']) and _between(length, box['length_m']) and rect is not None and rect >= box['rectilinearity']:
        return result('shipping-container', ['geometry', 'appearance' if ribbed else None, 'context' if detached_secondary else None,
                                             'sam3' if sam3_group('shipping-container') else None])
    stack = d['container-stack']
    if (container_stack_width(width) and container_stack_length(length) is not None and rect is not None
            and rect >= stack['rectilinearity']):
        return result('container-stack', ['geometry', 'appearance' if ribbed else None, 'context' if detached_secondary else None,
                                          'sam3' if sam3_group('container-stack') else None])
    port = d['carport']
    near_2m = c.get('nearest_larger_roof_m') is not None and c['nearest_larger_roof_m'] <= port['near_larger_m']
    driveway = a.get('driveway_like_share_3m') is not None and a['driveway_like_share_3m'] >= port['driveway_share']
    if (_between(area, port['area_m2']) and rect is not None and rect >= port['rectilinearity'] and (attached or near_2m) and driveway):
        kind, _, rule, groups = result('carport', ['geometry', 'appearance', 'context', 'sam3' if sam3_group('carport') else None])
        return kind, 'weak', rule, groups
    if see_through and _between(area, st['area_m2']):
        return _see_through(a, c, s, result)
    cover = d['attached-cover']
    if attached and _between(area, cover['area_m2']) and rect is not None and rect >= cover['rectilinearity']:
        # An attached small solid roof is an extension of the primary roof (patio, veranda, carport cover); with a
        # driveway beside it the carport rule above already fired, and a see-through frame is a pergola. Sheds and
        # garages are detached buildings.
        return result('attached-cover', ['geometry', 'context', 'appearance' if a.get('brightness_median') is not None else None,
                                         'sam3' if s['available'] and s['no_house'] else None])
    garage_fits = _between(area, d['garage']['area_m2']) and rect is not None and rect >= d['garage']['rectilinearity']
    if garage_fits and attached:   # in the 30-36 m² overlap with the shed rule, an attached box is a garage
        return result('garage', ['geometry', 'context', 'appearance' if ribbed else None, 'sam3' if sam3_group('garage') else None])
    if _between(area, d['shed']['area_m2']) and rect is not None and rect >= d['shed']['rectilinearity']:
        return result('shed', ['geometry', 'appearance' if ribbed else None, 'context' if detached_secondary else None,
                               'sam3' if sam3_group('shed') else None])
    if garage_fits and flagged:
        return result('garage', ['geometry', 'context', 'appearance' if ribbed else None, 'sam3' if sam3_group('garage') else None])
    if flagged:
        return result('outbuilding', ['context', 'geometry' if area < d['primary_area_m2'] else None,
                                      'sam3' if s['available'] and s['no_house'] else None])
    if s['available'] and s['no_house'] and s['positive']:
        return result('outbuilding', ['sam3'])
    return result('not-secondary', ['geometry' if area >= d['primary_area_m2'] else None,
                                    'context' if not near_larger else None,
                                    'sam3' if s['available'] and s['house_fit'] else None])


def container_stack_width(width):
    """Number of container widths (2, 3 or 4) the rectangle width matches within the design tolerance, else None."""
    if width is None:
        return None
    stack = DESIGN['container-stack']
    for units in stack['units']:
        if abs(width-units*stack['unit_width_m']) <= stack['width_tolerance_m']+1e-9:
            return units
    return None


def container_stack_length(length):
    """The standard container length (6.1 or 12.2 m) the rectangle length matches within the design tolerance, else None."""
    if length is None:
        return None
    stack = DESIGN['container-stack']
    for standard in stack['standard_lengths_m']:
        if abs(length-standard) <= stack['length_tolerance_m']+1e-9:
            return standard
    return None


def _see_through(a, c, s, result):
    """pergola-or-shade for a see-through frame the carport rule did not claim; always weak (see the docstring)."""
    kind, _, rule, groups = result('pergola-or-shade', ['geometry', 'appearance', 'context' if c.get('secondary_structure_flag') else None])
    return kind, 'weak', rule, groups


def notes_for(geometry, appearance, sub_kind):
    """Reviewer notes for measurements that point one way while the rules decide another."""
    notes = []
    aniso, width = appearance.get('rib_anisotropy'), geometry.get('rect_width_m')
    box, length, rect = DESIGN['shipping-container'], geometry.get('rect_length_m'), geometry.get('rectilinearity')
    units = container_stack_width(width)
    if sub_kind != 'container-stack' and units and length is not None:
        if container_stack_length(length) is None:
            notes.append(f'{units} container widths but {length} m is not a 20 ft or 40 ft length; not a container-stack')
        elif rect is not None and rect < DESIGN['container-stack']['rectilinearity']:
            notes.append(f'{units} container widths and a container length but rectilinearity {rect} is below '
                         f'{DESIGN["container-stack"]["rectilinearity"]}; not a container-stack')
    if (sub_kind not in ('shipping-container', 'container-stack') and aniso is not None and aniso >= DESIGN['ribbed']
            and width is not None and width > box['width_m'][1] and not units):
        notes.append(f'ribbed (rib_anisotropy {aniso}) but rect_width_m {width} exceeds a single container width ({box["width_m"][1]} m) '
                     'and matches neither 2, 3 nor 4 side by side; the geometry rules decide')
    irv = appearance.get('interior_relative_variance')
    if sub_kind not in ('carport', 'pergola-or-shade') and irv is not None and irv >= DESIGN['see-through']['interior_relative_variance']:
        notes.append(f'interior_relative_variance {irv} is high but brightness or rectilinearity kept the see-through rule from firing')
    blue = appearance.get('pool_blue_share')
    if sub_kind == 'pool-cover' and blue is not None:
        notes.append(f'pool_blue_share {blue}: a strongly blue, saturated interior may be the pool water itself rather than a cover; '
                     'the evidence records the share and any SAM3 swimming-pool fit, the sub-kind covers both')
    elif blue is not None and blue >= DESIGN['pool-cover']['blue_share']:
        notes.append(f'pool_blue_share {blue} is at or above the pool-cover threshold but area or rectilinearity kept the rule from firing')
    return notes


# --- Scene assessment ---------------------------------------------------------------------------------------

def plain(value):
    """JSON-plain copy: numpy scalars/arrays and sets become Python numbers, lists and sorted lists."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


def eligible(shape, flagged):
    return shape.area < DESIGN['eligible_area_m2'] or bool(flagged)


def assess_scene(scene, image=None, flags=None, *, sam3=None, object_ids=None, eligible_only=True):
    """One structure-kinds-v2 row per eligible live building of the scene (pure apart from the optional SAM3 model).

    image: RGB array (None -> appearance unavailable). flags: {(scene_id, object_id): building-flags-v2 payload}.
    sam3: a Sam3Scorer or None. object_ids restricts the population; eligible_only=False assesses every live building.
    """
    flags = flags or {}
    shapes = building_shapes(scene)
    enhanced = enhance(image) if image is not None else None
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    selected = []
    for obj in live_buildings(scene):
        shape = shapes.get(obj['id'])
        if shape is None or (object_ids is not None and obj['id'] not in object_ids):
            continue
        payload = flags.get((scene['id'], obj['id']))
        flagged, _ = flag_evidence(payload)
        if eligible_only and not eligible(shape, flagged):
            continue
        selected.append((obj, shape, payload))
    sam3_by_object = {}
    if sam3 is not None and image is not None and selected:
        frame = (scene['image_height'], scene['image_width'])
        masks = {obj['id']: _raster(to_pixels(shape, scene), (0, 0), frame) for obj, shape, _ in selected}
        sam3_by_object = sam3.score_scene(image, masks)
    rows = []
    for obj, shape, payload in selected:
        geometry = geometry_features(shape, vertex_count=len(obj.get('ring') or []))
        others = [s for oid, s in shapes.items() if oid != obj['id']]
        appearance = appearance_features(enhanced, shape, scene, geometry, others)
        context = context_features(obj, shape, shapes, scene, payload)
        sam3_evidence = sam3_by_object.get(obj['id'])
        sub_kind, confidence, rule, groups = classify(geometry, appearance, context, sam3_evidence)
        out = {'version': VERSION, 'note': NOTE, 'kind': 'building', 'area_m2': geometry['area_m2'],
               'sub_kind': sub_kind, 'confidence': confidence, 'rule': rule, 'agreeing_groups': groups,
               'evidence': {'geometry': geometry, 'appearance': appearance, 'context': context, 'sam3': sam3_evidence,
                            'notes': notes_for(geometry, appearance, sub_kind)},
               'eligibility': {'small': shape.area < DESIGN['eligible_area_m2'], 'secondary_structure_flag': context['secondary_structure_flag']},
               'status': obj.get('status'), 'score': obj.get('score'), 'computed_at': stamp, 'code_sha256': code}
        rows.append(_row(scene, obj, plain(out), VERSION))
    return rows


def run(root=Path('.'), scene_ids=(), *, objects=None, sam3=None, write=True, eligible_only=True, log=None):
    """Assess scenes (or SCENE:OBJECT pairs) and write structure-kinds-v2 rows; returns (rows, written)."""
    root = Path(root)
    targets = {}
    for scene_id in scene_ids:
        targets.setdefault(validate_id(scene_id), None)
    for scene_id, object_id in objects or ():
        targets.setdefault(validate_id(scene_id), set())
        if targets[scene_id] is not None:
            targets[scene_id].add(object_id)
    flags = load_flags(root, targets)
    rows, whole_scenes = [], []
    for index, (scene_id, object_ids) in enumerate(targets.items()):
        scene = load_scene(root, scene_id)
        image = load_image(root, scene)
        started = time.monotonic()
        scene_rows = assess_scene(scene, image, flags, sam3=sam3, object_ids=object_ids, eligible_only=eligible_only)
        rows.extend(scene_rows)
        if object_ids is None:
            whole_scenes.append(scene_id)
        if log:
            log(f'[{index+1}/{len(targets)}] {scene_id}: {len(scene_rows)} rows, image={"yes" if image is not None else "no"}, {time.monotonic()-started:.1f}s')
    written = 0
    if write and rows:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            whole = [r for r in rows if r['scene_id'] in whole_scenes]
            partial = [r for r in rows if r['scene_id'] not in whole_scenes]
            written += replace_scene_rows(db, TABLE, VERSION, whole_scenes, whole)
            if partial:
                written += eyes_index.replace_rows(db, TABLE, partial)  # per-object upsert, same version only
    return rows, written


# --- Report and contact sheet -------------------------------------------------------------------------------

def summarise(rows, *, sam3=False, written=0):
    kinds, confidences, pairs = Counter(), Counter(), Counter()
    scenes = {}
    for row in rows:
        p = row['payload']
        kinds[p['sub_kind']] += 1
        confidences[p['confidence']] += 1
        pairs[f"{p['sub_kind']}/{p['confidence']}"] += 1
        scenes.setdefault(row['scene_id'], []).append({'object_id': row['object_id'], 'area_m2': p['area_m2'], 'sub_kind': p['sub_kind'],
                                                       'confidence': p['confidence'], 'rule': p['rule'], 'agreeing_groups': p['agreeing_groups']})
    return {'version': VERSION, 'note': NOTE, 'computed_at': datetime.now(UTC).isoformat(), 'sam3': bool(sam3), 'design': plain(DESIGN),
            'rules': RULES, 'objects': len(rows), 'written': written, 'counts': {'sub_kind': dict(kinds), 'confidence': dict(confidences),
                                                                                  'sub_kind_confidence': dict(pairs)},
            'scenes': [{'scene_id': sid, 'objects': objs} for sid, objs in scenes.items()]}


def render_sheet(root, rows, path, *, columns=4, scale=3, pad_m=3., cell=(330, 330), font_size=22):
    """Contact sheet of every classified object (crop at `scale`x with its outline) with large labels."""
    root = Path(root)
    font = ImageFont.load_default(size=font_size)
    small = ImageFont.load_default(size=font_size-4)
    line_h = font_size+6
    label_h = 6*line_h+12
    cell_w, cell_h = cell[0], cell[1]+label_h
    n = max(len(rows), 1)
    rows_n = math.ceil(n/columns)
    sheet = Image.new('RGB', (columns*cell_w, rows_n*cell_h), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    scenes, images = {}, {}
    for index, row in enumerate(rows):
        sid = row['scene_id']
        if sid not in scenes:
            scenes[sid] = load_scene(root, sid)
            images[sid] = load_image(root, scenes[sid])
        scene, image = scenes[sid], images[sid]
        cx, cy = (index % columns)*cell_w, (index//columns)*cell_h
        p = row['payload']
        obj = next((o for o in scene['objects'] if o['id'] == row['object_id']), None)
        if image is not None and obj is not None:
            ring = np.asarray(obj['ring'], dtype=float)
            pad = pad_m/pixel_size_m(scene)
            x0, y0 = max(int(ring[:, 0].min()-pad), 0), max(int(ring[:, 1].min()-pad), 0)
            x1, y1 = min(int(ring[:, 0].max()+pad)+1, image.shape[1]), min(int(ring[:, 1].max()+pad)+1, image.shape[0])
            crop = Image.fromarray(image[y0:y1, x0:x1])
            crop = crop.resize((crop.width*scale, crop.height*scale), Image.Resampling.LANCZOS)
            fit = min(cell[0]/crop.width, cell[1]/crop.height, 1.)
            if fit < 1:
                crop = crop.resize((max(1, int(crop.width*fit)), max(1, int(crop.height*fit))), Image.Resampling.LANCZOS)
            k = scale*fit
            outline = [((x-x0)*k, (y-y0)*k) for x, y in ring]
            ImageDraw.Draw(crop).polygon(outline, outline=(255, 64, 200), width=3)
            sheet.paste(crop, (cx+(cell_w-crop.width)//2, cy+(cell[1]-crop.height)//2))
        else:
            draw.text((cx+10, cy+10), 'image unavailable', fill=(200, 200, 200), font=small)
        geo = (p.get('evidence') or {}).get('geometry') or {}
        dims = f' {geo["rect_width_m"]:.1f}x{geo["rect_length_m"]:.1f}' if geo.get('rect_width_m') and geo.get('rect_length_m') else ''
        lines = [f'#{index}  {sid.rsplit("-", 2)[-2]}-{sid.rsplit("-", 1)[-1]}', f'{row["object_id"][:8]} {p["area_m2"]:.1f}m2{dims}',
                 p['sub_kind'], p['confidence'], ' '.join(GROUP_SHORT.get(g, g) for g in p.get('agreeing_groups') or []) or '-']
        s3 = (p.get('evidence') or {}).get('sam3')
        if s3 and s3.get('prompts'):
            house = max((v.get('best_iou') or 0.) for k, v in s3['prompts'].items() if k in SAM3_ROOF_PROMPTS)
            best = max(((k, v.get('best_iou') or 0.) for k, v in s3['prompts'].items() if k not in SAM3_ROOF_PROMPTS), key=lambda kv: kv[1])
            lines.append(f'sam3 house {house:.2f} {best[0][:9]} {best[1]:.2f}')
        colour = {'weak': (250, 204, 21), 'moderate': (134, 239, 172), 'strong': (96, 165, 250)}[p['confidence']]
        for i, text in enumerate(lines):
            draw.text((cx+8, cy+cell[1]+6+i*line_h), text, fill=colour if i in (2, 3) else (235, 235, 235), font=font if i < 5 else small)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path


def sample_rows(rows, cap=None, seed=11):
    """A seeded random sample of at most `cap` rows (all rows when cap is None or not exceeded), in a stable order."""
    if cap is None or len(rows) <= cap:
        return list(rows)
    picked = random.Random(seed).sample(range(len(rows)), cap)
    return [rows[i] for i in sorted(picked)]


def _parse_objects(values):
    pairs = []
    for value in values or ():
        scene_id, _, object_id = value.partition(':')
        if not object_id:
            raise ValueError(f'--objects expects SCENE:OBJECT, got {value!r}')
        pairs.append((scene_id, object_id))
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sub-kind hints for small/secondary building candidates (agent hints, not labels).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='*', default=[], help='Scene ids (every eligible live building).')
    parser.add_argument('--objects', nargs='*', default=[], help='SCENE:OBJECT pairs (per-object upsert).')
    parser.add_argument('--sam3', action='store_true', help='Also score SAM3 prompts (about 13 s per tile plus 1.5 s per prompt).')
    parser.add_argument('--sam3-models', default=str(SAM3_MODELS))
    parser.add_argument('--all-buildings', action='store_true', help='Assess every live building, not only eligible ones.')
    parser.add_argument('--dry-run', action='store_true', help='Compute without writing to the eyes index.')
    parser.add_argument('--report', help='JSON summary path (under data/reviews/eyes/structure-kinds/).')
    parser.add_argument('--sheet', help='Contact sheet PNG path.')
    parser.add_argument('--rows', help='Also dump the full rows (with evidence) as JSON to this path.')
    parser.add_argument('--sheet-kinds', nargs='*', default=None, help='Restrict --sheet to these sub-kinds.')
    parser.add_argument('--sheet-cap', type=int, default=None, help='Random sample of at most this many rows on the sheet.')
    parser.add_argument('--sheet-seed', type=int, default=11, help='Seed for --sheet-cap sampling.')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    if not args.scenes and not args.objects:
        parser.error('give --scenes and/or --objects')
    root = Path(args.root)
    scorer = Sam3Scorer(Path(args.sam3_models)) if args.sam3 else None
    log = None if args.quiet else (lambda text: print(text, file=sys.stderr, flush=True))
    rows, written = run(root, args.scenes, objects=_parse_objects(args.objects), sam3=scorer, write=not args.dry_run,
                        eligible_only=not args.all_buildings, log=log)
    out = summarise(rows, sam3=args.sam3, written=written)
    if scorer is not None:
        out['sam3_timings'] = scorer.timings
    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(out, indent=1))
    if args.rows:
        Path(args.rows).parent.mkdir(parents=True, exist_ok=True)
        Path(args.rows).write_text(json.dumps(rows, indent=1))
    if args.sheet:
        shown = rows if args.sheet_kinds is None else [r for r in rows if r['payload']['sub_kind'] in args.sheet_kinds]
        render_sheet(root, sample_rows(shown, args.sheet_cap, args.sheet_seed), args.sheet)
    json.dump({k: out[k] for k in ('version', 'objects', 'written', 'counts', 'sam3')}, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
