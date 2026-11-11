"""Shadow-trim sidecar v1 (tree-shadow-2026-10-08, lens D): propose tree / vegetation outlines without the tree's cast shadow.

Reviewer (chat, 8 Oct ~08:20 AEDT), on win-69468b2b783cd964 / 547f6490e66df5d0 (a tree confirmed 7 Oct): "i think ... is picking
up shadows for example, if it can be refined". SAM3 masks are modal: a 'tree canopy' prompt returns crown + cast shadow as one
blob. The census (census/census.md) found cast shadow in about 1 in 7 tree outlines. This sidecar PROPOSES a trimmed outline;
nothing is applied, nothing under data/scenes is written, no model runs.

Population: the 54 recapture windows; review layer (live review.json objects, status not rejected / superseded) gets proposals,
renders and MARKS; the model layer (latest.json) is counted only. Classes tree and vegetation. Eligible = the census row
(census/objects.jsonl, same scene / layer / id) has shadow share >= 0.10 with >= 70 % of it on the sun-opposite side (the level
census.md reports at 92 % pooled sun-opposite and 12 of 15 tree renders right by eye), OR shadow-side excess >= 0.20 (the
census's threshold-free measure; the reviewer's own tree scores 7 % share but 32 % excess because its dappled shadow fails the
census's smooth-texture test).

Pixel classes (census thresholds from census/calibration.json, fitted on the census's 5 labelled examples, never the reviewer's
window; frame features exactly as shadow_census.frame_features):
  dark       V_rel <= t_v
  lit green  ExG >= t_g and not dark                     (sunlit foliage; ~40 % of a crown's pixels are dark self-shade / gaps)
  blue-dark  dark and C <= t_c_chroma and Bdom >= t_b    (the census shadow test WITHOUT its smooth-texture test, which dappled
                                                          shadow fails; in the 5 labelled examples blue-dark covers 0-3 % of
                                                          crown boxes and 72-97 % of shadow boxes)
Per object (pixel corner convention: pixel (i, j) is the square [i, i+1] x [j, j+1]; it is inside the outline when its centre is):
  1 crown body = lit-green pixels inside the outline, closed with a disc of crown_close_m (bridges the dark gaps between lit
    leaves), holes filled (a dark crown interior is crown), opened by 1 px, components >= crown_min_px. Never removed.
  2 shadow side = the crown body swept along the window's shadow bearing +- fan_deg up to sweep_max_m (a sector dilation); the
    sun side = the same sweep towards the sun. Candidates = outline pixels that are not crown body, on the shadow side and NOT on
    the sun side (a pixel with crown beyond it in the shadow direction lies between crowns, not in a cast shadow).
  3 a candidate component (8-connected) is removed when it reaches the outline's edge (a non-frame outline pixel), its blue-dark
    share >= blue_dark_share_min and its blue-dark area >= blue_dark_min_m2. Shadow on grass is green-tinted and fails the
    blue-dark test, so lawn under a shadow is never trimmed out of a vegetation outline.
  4 greedy, largest first: a component is skipped when removing it would split the crown-bearing part of the outline
    (4-connected); removed set closed then opened by morph_px (1 px), never touching crown body; non-crown islands cut off
    by the removal are removed with it (orphans); holes the removal would leave below hole_fill_max_m2 are filled back.
  5 vector: original outline minus the removed pixel squares (plus the outside pixel squares bordering them, so no sliver of
    the old lobe survives); a vector closing of cut_close_px (0.75 px: a 1-px staircase becomes the line through its outer
    corners; unioned with the stepped cut so it only adds area) clipped to the original; Douglas-Peucker on the new cut vertices only at the largest of
    simplify_px that keeps every crown-body pixel centre inside and <= cut_slack_share (5 %) of the removed pixel centres inside;
    the part holding the crown; holes below hole_fill_max_m2 dropped; ring rounded to 2 dp, scene_extract.finalise_app_ring
    with APP_RING_RULES (the app's capped save rules) and make_valid. Untouched outline vertices are kept exactly.
Guards: window sun azimuth not reliable in census/sun.json -> no trim ('sun_uncertain'); no crown body -> flag ('no_crown');
removed share > max_trim_share (60 %) -> flag ('over_60pct', geometry recorded, not proposed); removed < min_trim_m2 (0.1 m2)
or < min_trim_share (3 %) -> no trim;
a decided object (confirmed / needs_adjustment / geometry_reviewed) gets a proposal only, lineage 'replaces_on_accept';
an undecided one 'replaces'. Frame-cut edges: policy 'drop_shadow_only_edge' (default) refuses a component that touches a
frame-cut side unless (a) the crown body touches NO frame side (the whole visible crown is inside the frame, so the off-frame
continuation cannot be this crown) and (b) the outline's frame band on that side is no longer than the crown body's largest
extent (a crown's cast shadow is no wider than the crown); then the edge lies wholly in the shadow and is dropped with its
component (recorded as frame_cut_dropped). Policy 'keep_always' (the brief's literal guard) refuses any component touching a
frame-cut side; the run records what it gives for every proposal that drops a frame-cut edge. Lineage as the road bands: replaces / replaces_on_accept / derived_from.

Run from the repo root (nice -n 19 on the shared Mac; read-only on data/scenes, no inference):
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.shadow_trim run      # records + summary -> prototype/run-v1/
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.shadow_trim render   # renders, contact sheets, MARKS-shadow-v1.md
Outputs only under data/reviews/eyes/experiments/tree-shadow-2026-10-08/prototype/ (+ heartbeat lines in
.run/session-2026-10-08-shadow/proto/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import shapely
from PIL import Image
from scipy import ndimage
from scipy.signal import fftconvolve
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union
from shapely.validation import make_valid

from ..scene_extract import APP_RING_RULES, _ring_details, finalise_app_ring
from ..segmentation import iter_polygons
from . import shadow_census as sc

VERSION = 'shadow-trim-v1'
ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT = ROOT/'data/reviews/eyes/experiments/tree-shadow-2026-10-08'
CENSUS_DIR = EXPERIMENT/'census'
OUT_DIR = EXPERIMENT/'prototype/run-v1'
RUN_DIR = ROOT/'.run/session-2026-10-08-shadow/proto'
HAND_TRACE = EXPERIMENT/'example/case.json'
REVIEWER_EXAMPLE = ('win-69468b2b783cd964', '547f6490e66df5d0')
APP_URL = 'http://localhost:3008/scenes?scene={scene}&status=all&frame=all&object={id}&layer=edited'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

DESIGN = {
    'version': VERSION,
    'kinds': ('tree', 'vegetation'),
    'eligible': {'share_min': 0.10, 'sun_opposite_min': 0.70, 'excess_min': 0.20},
    'crown_close_m': 0.3,          # closing radius on lit-green pixels: dark gaps up to ~0.6 m between lit leaves are crown
    'crown_min_px': 30,            # crown-body components below this are specks (chroma bleed, green-tinted flecks)
    'fan_deg': 30.0,               # census: windows of one area agree within ~20-30 deg; tree-vs-building median 14 deg
    'fan_area_union': True,        # the arc also covers the area-mean bearing +- fan_deg (added after the example, see notes)
    'area_union_max_deg': 60.0,
    'sweep_max_m': 10.0,           # longest cast shadow searched for
    'blue_dark_share_min': 0.15,   # labelled crowns 0-3 %, labelled shadows 72-97 %; dappled shadow dilutes it with lit flecks
    'blue_dark_min_m2': 0.05,
    'crown_min_share_of_largest': 0.05,  # crown-body parts below 5 % of the largest are lit specks, not crowns
    'max_trim_share': 0.60,
    'min_trim_m2': 0.10,
    'min_trim_share': 0.03,        # a trim under 3 % of the outline is not worth a reviewer's look
    'morph_px': 1,
    'hole_fill_max_m2': 0.25,
    'cut_close_px': 0.75,          # vector closing of the stepped cut: a 1-px staircase becomes the line through its outer corners
    'simplify_px': (0.5, 0.35, 0.25, 0.0),
    'cut_slack_share': 0.05,       # the smoothed cut may keep <= this share of the removed pixel centres (crown: none lost)
    'frame_tol_px': 1.0,
    'frame_cut_policy': 'drop_shadow_only_edge',   # or 'keep_always'
    'decided_statuses': ('confirmed', 'needs_adjustment'),
    'pad_px': 3,
}
LIVE_EXCLUDED = ('rejected', 'superseded')
DISCLOSURES = [
    'Pixel thresholds are the census ones (5 labelled examples, never the reviewer window). New knobs: blue_dark_share_min '
    '0.15 (label boxes: crowns 0-3 % blue-dark, shadows 72-97 %); crown_close_m 0.3 (crown-body coverage of the labelled crown '
    'boxes 0.77-0.98, but 0.25 on the dark inner crown I2: dark crowns get little crown body); fan_deg 30 (census spread).',
    'Changed after looking at the reviewer example (its IoU with the hand trace is therefore not an independent test): '
    '(1) the shadow arc also covers the area-mean bearing (the window reads 107 deg, the area 142, the tree\'s own lobe ~140); '
    '(2) the sun-side exclusion sweeps the crown core (crown body eroded by crown_close_m), because a chroma-bleed tail of the '
    'crown blocked the SW half of the lobe; (3) the vector closing + cut-only Douglas-Peucker (45 -> 28 vertices on the example). '
    'Example IoU vs the hand trace along the way: 0.750 (first cut), 0.757 (+1), 0.804 (+2), 0.797 (+3), 0.726 (dark-green '
    'barrier, below).',
    'Changed after looking at three rejected non-example objects (win-b6901e4edcbc5495/ffa25cc4e6c86a23, '
    'win-17b5c9b30a5d3ff9/b19b4fbb9aa0305a, win-2d422aac4db9f40d/675f4192b3778f3d): a component-level guard (reject when > 35 % '
    'of the component is dark but not blue-shifted) refused 74 of 98 non-trims, mostly crowns whose shaded half merged with '
    'the cast shadow; it was replaced by the per-pixel dark-green barrier. That keeps a dark band under the example crown '
    '(which the hand trace called shadow, listed there as unresolved +-2 px) and lowered the example IoU from 0.80 to 0.73.',
    'min_trim_share 3 % was added after the first full run (three proposals removed under 3 % of a large outline).',
    'Frame-cut edges: the brief said keep them. The default drops one only when the crown body touches no frame side and the '
    'band is no longer than the crown; the reviewer example needs this (its frame-cut bottom edge lies wholly in the shadow, as '
    'the hand trace found). Under keep_always the example gets no proposal. A first, looser rule (crown not on that side) '
    'dropped the right edge of win-17b5c9b30a5d3ff9/b19b4fbb9aa0305a whose crown runs off the top; the rule was tightened.',
    'Known failure modes: non-green crown parts (brown fronds, dead or autumn foliage) attached to a shadow component are cut '
    'with it; the crown body follows lit foliage, so cuts are ragged and vertex counts grow on big trees (cap 200).',
]
CROSS = ndimage.generate_binary_structure(2, 1)
SQUARE = ndimage.generate_binary_structure(2, 2)


def now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def heartbeat(message):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_DIR/'progress.txt', 'a') as handle:
        handle.write(f'{datetime.now().strftime("%H:%M:%S")} {message}\n')


def r(value, digits=4):
    return None if value is None else round(float(value), digits)


# ------------------------------------------------------------------------------------------------------------- pixel helpers
def disk(radius):
    yy, xx = np.mgrid[-radius:radius+1, -radius:radius+1]
    return xx*xx+yy*yy <= radius*radius+1e-9


def arc_kernel(lin, start_deg, span_deg, radius_px):
    """Offsets d (pixels, x right / y down) with |d| <= radius_px whose compass bearing (through lin = pixel_to_meters[:2])
    lies in the clockwise arc [start_deg, start_deg + span_deg]; the origin is included."""
    lin = np.asarray(lin, dtype=float)[:2]
    radius = max(1, int(math.ceil(radius_px)))
    yy, xx = np.mgrid[-radius:radius+1, -radius:radius+1].astype(np.float64)
    east, north = xx*lin[0, 0]+yy*lin[1, 0], xx*lin[0, 1]+yy*lin[1, 1]
    rel = (np.degrees(np.arctan2(east, north))-start_deg) % 360.
    dist = np.hypot(xx, yy)
    return (dist <= radius_px+1e-9) & ((rel <= span_deg+1e-9) | (dist == 0))


def shadow_arc(window_bearing, area_bearing, design=DESIGN):
    """(start, span) of the shadow-direction arc: the window bearing +- fan_deg, widened to also cover the area mean +- fan_deg
    when the two are within area_union_max_deg (census: windows of one area agree only within ~20-30 deg)."""
    fan = design['fan_deg']
    if area_bearing is None or not design['fan_area_union']:
        return (window_bearing-fan) % 360., 2*fan
    diff = (area_bearing-window_bearing+180.) % 360.-180.
    if abs(diff) > design['area_union_max_deg']:
        return (window_bearing-fan) % 360., 2*fan
    lo, hi = min(0., diff)-fan, max(0., diff)+fan
    return (window_bearing+lo) % 360., hi-lo


def sweep(mask, kernel):
    """Dilation of mask by the offsets in kernel (kernel centre = offset 0): out[p] = any(mask[p - d] for d in kernel)."""
    if not mask.any():
        return np.zeros_like(mask)
    return fftconvolve(mask.astype(np.float32), kernel.astype(np.float32), mode='same') > 0.5


def pixel_classes(feats, th):
    dark = feats['V_rel'] <= th['t_v']
    blue_dark = dark & (feats['C'] <= th['t_c_chroma']) & (feats['Bdom'] >= th['t_b'])
    rgb = feats['rgb']
    green_dom = (rgb[..., 1] >= rgb[..., 0]) & (rgb[..., 1] >= rgb[..., 2])
    return {'dark': dark, 'lit_green': (feats['ExG'] >= th['t_g']) & ~dark, 'blue_dark': blue_dark,
            'dark_green': dark & ~blue_dark & green_dom}


def object_polygon(obj):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None, False
    holes = [h for h in (obj.get('holes') or []) if len(h) >= 3]
    try:
        poly = Polygon(ring, holes)
    except (ValueError, TypeError):
        return None, False
    if poly.is_valid:
        return (poly if poly.area > 0 else None), True
    parts = sorted(iter_polygons(make_valid(poly)), key=lambda p: -p.area)
    return (parts[0] if parts else None), False


def crop_box(poly, width, height, pad):
    minx, miny, maxx, maxy = poly.bounds
    return (max(0, int(math.floor(minx))-pad), max(0, int(math.floor(miny))-pad),
            min(width, int(math.ceil(maxx))+pad), min(height, int(math.ceil(maxy))+pad))


def rasterise(poly, x0, y0, x1, y1):
    """Pixel-centre test in the corner convention: pixel (i, j) inside when (i + .5, j + .5) is inside the outline."""
    yy, xx = np.mgrid[y0:y1, x0:x1]
    return shapely.contains_xy(poly, xx+.5, yy+.5)


def squares(mask, x0, y0):
    """Union of the pixel squares of mask (crop origin x0, y0) as a shapely geometry, one box per row run."""
    boxes = []
    for j in range(mask.shape[0]):
        row = mask[j]
        if not row.any():
            continue
        padded = np.concatenate([[False], row, [False]])
        edges = np.flatnonzero(padded[1:] != padded[:-1])
        for a, b in zip(edges[::2], edges[1::2], strict=True):
            boxes.append(box(x0+a, y0+j, x0+b, y0+j+1))
    return unary_union(boxes) if boxes else None


def frame_cut_sides(poly, width, height, tol):
    """Frame sides ('left', 'top', 'right', 'bottom') an outline edge lies on (both ends within tol of that side)."""
    sides = set()
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        for a, b in zip(c, c[1:], strict=False):
            for name, axis, edge in (('left', 0, 0.), ('right', 0, float(width)), ('top', 1, 0.), ('bottom', 1, float(height))):
                if abs(a[axis]-edge) <= tol and abs(b[axis]-edge) <= tol and math.dist(a, b) > 0:
                    sides.add(name)
    return sorted(sides)


def side_band(name, x0, y0, shape, width, height):
    """Crop-sized mask of the outermost frame pixel row / column on one frame side."""
    band = np.zeros(shape, bool)
    h, w = shape
    if name == 'left' and x0 == 0:
        band[:, 0] = True
    elif name == 'right' and x0+w == width:
        band[:, w-1] = True
    elif name == 'top' and y0 == 0:
        band[0, :] = True
    elif name == 'bottom' and y0+h == height:
        band[h-1, :] = True
    return band


def components_with(labels, n, marker):
    return {int(v) for v in np.unique(labels[marker]) if v} if n else set()


def crown_parts(keep, crown):
    labels, n = ndimage.label(keep, structure=CROSS)
    return len(components_with(labels, n, crown & keep))


# ------------------------------------------------------------------------------------------------------------- core trim
def trim_object(poly, feats, th, *, lin, width, height, shadow_bearing, area_bearing=None, design=DESIGN, policy=None):
    """The raster + vector trim of one outline. Returns a dict: outcome ('proposal' | 'flag' | 'no_trim'), reasons, metrics,
    the crop masks (crop, mask, crown, removed) and, for a proposal or an over-share flag, the trimmed ring / holes."""
    policy = policy or design['frame_cut_policy']
    lin = np.asarray(lin, dtype=float)[:2]
    pixel_area = abs(float(np.linalg.det(lin)))
    mpp = math.sqrt(pixel_area)
    x0, y0, x1, y1 = crop_box(poly, width, height, design['pad_px'])
    shape = (y1-y0, x1-x0)
    mask = rasterise(poly, x0, y0, x1, y1)
    out = {'outcome': None, 'reasons': [], 'crop': [x0, y0, x1, y1], 'policy': policy}
    n_px = int(mask.sum())
    out['mask_px'] = n_px
    if n_px == 0:
        return {**out, 'outcome': 'no_trim', 'reasons': ['empty_mask']}
    cls = {k: v[y0:y1, x0:x1] for k, v in pixel_classes(feats, th).items()}
    # 1 crown body
    rad = max(1, int(round(design['crown_close_m']/mpp)))
    lg = cls['lit_green'] & mask
    padded = np.pad(lg, rad+1)
    closed = ndimage.binary_closing(padded, structure=disk(rad))[rad+1:-rad-1, rad+1:-rad-1]
    crown = ndimage.binary_fill_holes(closed & mask) & mask
    crown = ndimage.binary_opening(crown, structure=CROSS, iterations=design['morph_px'])
    labels, n = ndimage.label(crown, structure=SQUARE)
    if n:
        sizes = ndimage.sum(crown, labels, index=np.arange(1, n+1))
        floor = max(design['crown_min_px'], design['crown_min_share_of_largest']*sizes.max())
        crown = np.isin(labels, 1+np.flatnonzero(sizes >= floor))
    # dark green (shaded foliage) never enters a candidate; its blobs >= crown_min_px count as foliage for connectivity
    dark_green = cls['dark_green'] & mask & ~crown
    blobs = ndimage.binary_opening(dark_green, structure=CROSS, iterations=design['morph_px'])
    labels, n = ndimage.label(blobs, structure=SQUARE)
    if n:
        sizes = ndimage.sum(blobs, labels, index=np.arange(1, n+1))
        blobs = np.isin(labels, 1+np.flatnonzero(sizes >= design['crown_min_px']))
    foliage = crown | blobs
    out['crown_px'] = int(crown.sum())
    out['dark_green_blob_px'] = int(blobs.sum())
    out['crown_share'] = r(crown.sum()/n_px)
    out['mask'], out['crown'] = mask, crown
    if not crown.any():
        return {**out, 'outcome': 'flag', 'reasons': ['no_crown']}
    # 2 shadow side / sun side
    start, span = shadow_arc(shadow_bearing, area_bearing, design)
    out['shadow_arc_deg'] = [r(start, 1), r((start+span) % 360., 1)]
    reach = min(design['sweep_max_m']/mpp, math.hypot(*shape))
    kernel = arc_kernel(lin, start, span, reach)
    shadow_side = sweep(crown, kernel)
    core = ndimage.binary_erosion(crown, structure=disk(rad), border_value=1)
    sun_side = sweep(core, kernel[::-1, ::-1])
    cand = mask & ~crown & ~dark_green & shadow_side & ~sun_side
    edge = mask & ~ndimage.binary_erosion(mask, structure=CROSS, border_value=1)
    # frame-cut sides and their bands
    sides = frame_cut_sides(poly, width, height, design['frame_tol_px'])
    bands = {s: side_band(s, x0, y0, shape, width, height) & mask for s in sides}
    crown_sides = [s for s in sides if (bands[s] & crown).any()]
    out['frame_cut_sides'], out['frame_cut_crown_sides'] = sides, crown_sides
    cy, cx = np.nonzero(crown)
    extent = max(cx.max()-cx.min()+1, cy.max()-cy.min()+1)
    droppable = [] if crown_sides else [s for s in sides if bands[s].sum() <= extent]
    out['frame_cut_droppable'] = droppable
    # 3 candidate components
    labels, n = ndimage.label(cand, structure=SQUARE)
    comps = []
    for k in range(1, n+1):
        comp = labels == k
        px = int(comp.sum())
        bd = int((cls['blue_dark'] & comp).sum())
        rec = {'px': px, 'm2': r(px*pixel_area, 3), 'blue_dark_share': r(bd/px), 'blue_dark_m2': r(bd*pixel_area, 3),
               'dark_share': r((cls['dark'] & comp).sum()/px), 'lit_green_share': r((cls['lit_green'] & comp).sum()/px),
               'on_outline': bool((edge & comp).any()), 'frame_sides': [s for s in sides if (bands[s] & comp).any()]}
        if not rec['on_outline']:
            rec['skip'] = 'not_on_outline'
        elif rec['blue_dark_share'] < design['blue_dark_share_min'] or bd*pixel_area < design['blue_dark_min_m2']:
            rec['skip'] = 'weak_shadow'
        elif rec['frame_sides'] and policy == 'keep_always':
            rec['skip'] = 'frame_cut_kept'
        elif any(s not in droppable for s in rec['frame_sides']):
            rec['skip'] = 'frame_cut_kept'
        comps.append((rec, comp))
    # 4 greedy acceptance keeping the crown-bearing part whole
    base_parts = crown_parts(mask, foliage)
    removed = np.zeros(shape, bool)
    for rec, comp in sorted(comps, key=lambda c: -c[0]['px']):
        if 'skip' in rec:
            continue
        trial = removed | comp
        if crown_parts(mask & ~trial, foliage) > base_parts:
            rec['skip'] = 'would_split_crown'
            continue
        rec['accepted'] = True
        removed = trial
    out['components'] = [c[0] for c in sorted(comps, key=lambda c: -c[0]['px'])]
    if not removed.any():
        return {**out, 'outcome': 'no_trim', 'reasons': ['no_shadow_component'], 'removed': removed}
    accepted_union = removed.copy()
    m = design['morph_px']
    smoothed = ndimage.binary_closing(np.pad(removed, m+1), structure=CROSS, iterations=m)[m+1:-m-1, m+1:-m-1] & mask & ~foliage
    smoothed = ndimage.binary_opening(smoothed, structure=CROSS, iterations=m)
    if smoothed.any() and crown_parts(mask & ~smoothed, foliage) <= base_parts:
        removed = smoothed
    # orphans: islands without crown body or dark-green blobs that the removal cut off
    keep = mask & ~removed
    labels, n = ndimage.label(keep, structure=CROSS)
    with_crown = components_with(labels, n, foliage)
    orphan = keep & ~np.isin(labels, sorted(with_crown)) if n else np.zeros(shape, bool)
    out['orphan_px'] = int(orphan.sum())
    removed |= orphan
    # tiny holes the removal would leave are filled back
    keep = mask & ~removed
    holes = ndimage.binary_fill_holes(keep) & ~keep & removed
    labels, n = ndimage.label(holes, structure=CROSS)
    if n:
        sizes = ndimage.sum(holes, labels, index=np.arange(1, n+1))
        small = np.isin(labels, 1+np.flatnonzero(sizes*pixel_area <= design['hole_fill_max_m2']))
        removed &= ~small
    removed &= ~foliage
    out['removed'] = removed
    rpx = int(removed.sum())
    out['removed_px'] = rpx
    out['removed_px_share'] = r(rpx/n_px)
    out['removed_blue_dark_share'] = r((cls['blue_dark'] & removed).sum()/max(rpx, 1))
    out['removed_dark_share'] = r((cls['dark'] & removed).sum()/max(rpx, 1))
    out['removed_lit_green_share'] = r((cls['lit_green'] & removed).sum()/max(rpx, 1))
    out['removed_dark_green_share'] = r((cls['dark_green'] & removed).sum()/max(rpx, 1))
    out['accepted_px_before_morphology'] = int(accepted_union.sum())
    dropped = [s for s in sides if (bands[s] & removed).any()]
    out['frame_cut_dropped'] = dropped
    out['frame_cut_dropped_px'] = int(sum((bands[s] & removed).sum() for s in dropped))
    # 5 vector
    vec = vectorise(poly, mask, crown, removed, x0, y0, width, height, pixel_area, design)
    out.update({k: v for k, v in vec.items() if k != 'polygon'})
    final = vec.get('polygon')
    if final is None:
        return {**out, 'outcome': 'no_trim', 'reasons': [vec.get('fail', 'vector_failed')]}
    before_m2 = poly.area*pixel_area
    after_m2 = final.area*pixel_area
    out['area_before_m2'] = r(before_m2, 3)
    out['area_after_m2'] = r(after_m2, 3)
    out['removed_m2'] = r(before_m2-after_m2, 3)
    out['removed_share'] = r((before_m2-after_m2)/before_m2)
    out['iou_before_after'] = r(final.intersection(poly).area/final.union(poly).area)
    if before_m2-after_m2 < design['min_trim_m2'] or (before_m2-after_m2)/before_m2 < design['min_trim_share']:
        return {**out, 'outcome': 'no_trim', 'reasons': ['below_min_trim']}
    if (before_m2-after_m2)/before_m2 > design['max_trim_share']:
        return {**out, 'outcome': 'flag', 'reasons': ['over_60pct']}
    return {**out, 'outcome': 'proposal'}


def smooth_cut(part, original, tol):
    """Douglas-Peucker on the cut only: every run of exterior vertices that are not vertices of the original outline is
    simplified between the original vertices that bound it, so untouched outline vertices stay exactly where they were."""
    coords = [(round(x, 6), round(y, 6)) for x, y in part.exterior.coords[:-1]]
    keep = [c in original for c in coords]
    if all(keep):
        return part
    if not any(keep):
        return Polygon(LineString(coords+[coords[0]]).simplify(tol).coords, list(part.interiors))
    start = keep.index(True)
    coords, keep = coords[start:]+coords[:start], keep[start:]+keep[:start]
    out, i, n = [], 0, len(coords)
    while i < n:
        if keep[i]:
            out.append(coords[i])
            i += 1
            continue
        j = i
        while j < n and not keep[j]:
            j += 1
        chain = LineString([coords[i-1], *coords[i:j], coords[j % n]]).simplify(tol, preserve_topology=False)
        out.extend(list(chain.coords)[1:-1])
        i = j
    return Polygon(out, list(part.interiors)) if len(out) >= 3 else part


def vectorise(poly, mask, crown, removed, x0, y0, width, height, pixel_area, design=DESIGN):
    """Original outline minus the removed pixel squares (and the outside squares bordering them, so no sliver of the old lobe
    survives); the part holding the crown; the cut smoothed at the largest simplify_px that keeps every crown-body pixel
    centre inside and <= cut_slack_share of the removed pixel centres inside; holes below hole_fill_max_m2 dropped; app ring rules."""
    border = ndimage.binary_dilation(removed, structure=SQUARE) & ~mask
    cut = squares(removed | border, x0, y0)
    if cut is None:
        return {'fail': 'nothing_to_cut'}
    cy, cx = np.nonzero(crown)
    cxs, cys = cx+x0+.5, cy+y0+.5
    ry, rx = np.nonzero(removed)
    rxs, rys = rx+x0+.5, ry+y0+.5
    parts = [p for p in iter_polygons(poly.difference(cut)) if p.area > 1e-6]
    holding = [p for p in parts if shapely.contains_xy(p, cxs, cys).any()]
    if len(holding) != 1:
        return {'fail': 'vector_split'}
    stepped = holding[0]
    dropped_m2 = (sum(p.area for p in parts)-stepped.area)*pixel_area
    original = {(round(x, 6), round(y, 6)) for ring in [poly.exterior, *poly.interiors] for x, y in ring.coords}
    hole_max_px2 = design['hole_fill_max_m2']/pixel_area
    c = design['cut_close_px']
    closing = unary_union([stepped, stepped.buffer(c, quad_segs=1).buffer(-c, quad_segs=1)]) if c else None
    closed = [p for p in iter_polygons(poly.intersection(closing)) if shapely.contains_xy(p, cxs, cys).any()] if c else []
    bases = ([('closed', closed[0])] if len(closed) == 1 and closed[0].is_valid else [])+[('stepped', stepped)]
    for (base_name, base), tol in ((b, t) for b in bases for t in design['simplify_px']):
        cand = smooth_cut(base, original, tol) if tol else base
        if not cand.is_valid:
            continue
        pieces = [p for p in iter_polygons(poly.intersection(cand)) if shapely.contains_xy(p, cxs, cys).any()]
        if len(pieces) != 1:
            continue
        part = Polygon(pieces[0].exterior, [h for h in pieces[0].interiors if Polygon(h).area > hole_max_px2])
        ring = [[min(max(round(float(x), 2), 0.), float(width)), min(max(round(float(y), 2), 0.), float(height))]
                for x, y in part.exterior.coords[:-1]]
        holes = [[[round(float(x), 2), round(float(y), 2)] for x, y in h.coords[:-1]] for h in part.interiors]
        ring, holes, issues, repairs = finalise_app_ring(ring, holes, width, height, rules=APP_RING_RULES)
        if issues:
            continue
        final = Polygon(ring, holes)
        made_valid = False
        if not final.is_valid:
            fixed = sorted(iter_polygons(make_valid(final)), key=lambda p: -p.area)
            if not fixed:
                continue
            final, made_valid = fixed[0], True
            ring = [[round(float(x), 2), round(float(y), 2)] for x, y in final.exterior.coords[:-1]]
            holes = [[[round(float(x), 2), round(float(y), 2)] for x, y in h.coords[:-1]] for h in final.interiors]
            ring, holes, issues, repairs = finalise_app_ring(ring, holes, width, height, rules=APP_RING_RULES)
            if issues:
                continue
            final = Polygon(ring, holes)
        kept_removed = int(shapely.contains_xy(final, rxs, rys).sum())
        if not shapely.contains_xy(final, cxs, cys).all() or kept_removed > design['cut_slack_share']*len(rxs):
            continue
        return {'polygon': final, 'ring': ring, 'holes': holes, 'simplify_px_used': tol, 'cut_base': base_name, 'app_issues': issues,
                'ring_repairs': repairs, 'made_valid': made_valid, 'dropped_parts_m2': r(dropped_m2, 3),
                'removed_centres_kept': kept_removed}
    return {'fail': 'vector_failed'}


# ------------------------------------------------------------------------------------------------------------- proposal
def is_decided(obj, design=DESIGN):
    return bool(obj.get('status') in design['decided_statuses'] or obj.get('geometry_reviewed'))


def ring_sha(obj):
    return hashlib.sha256(json.dumps([obj.get('ring'), obj.get('holes') or []], separators=(',', ':')).encode()).hexdigest()[:16]


def build_proposal(scene_id, obj, res, affine, design=DESIGN):
    decided = is_decided(obj, design)
    lineage = [obj['id']]
    pid = hashlib.sha256(f"{VERSION}:{scene_id}:{obj['id']}".encode()).hexdigest()[:16]
    model = _ring_details([tuple(p) for p in res['ring']], [[tuple(p) for p in h] for h in res['holes']],
                          np.asarray(affine, float), obj['kind']) if affine is not None else {}
    src = obj.get('model') or {}
    for key in ('height_m', 'base_m', 'height_source', 'height_verified', 'color'):
        if key in src:
            model[key] = src[key]
    return {'id': pid, 'kind': obj['kind'], 'label': obj.get('label'), 'ring': res['ring'], 'holes': res['holes'],
            'status': 'proposal', 'source': VERSION, 'geometry_reviewed': False, 'score': obj.get('score'),
            'prompt': obj.get('prompt'), 'model': model,
            'replaces': [] if decided else lineage, 'replaces_on_accept': lineage if decided else [], 'derived_from': lineage,
            'derived_ring_sha': ring_sha(obj),
            'trim': {'removed_m2': res['removed_m2'], 'removed_share': res['removed_share'],
                     'frame_cut_dropped': res['frame_cut_dropped'], 'policy': res['policy']}}


def eligibility(row, design=DESIGN):
    if row is None:
        return None
    e = design['eligible']
    routes = []
    if (row.get('shadow_share') or 0) >= e['share_min'] and (row.get('sun_opposite_share') or 0) >= e['sun_opposite_min']:
        routes.append('share')
    if (row.get('shadow_side_excess') or 0) >= e['excess_min']:
        routes.append('excess')
    return routes


def process_object(scene_id, obj, feats, th, meta, sun, census_row, area_bearing=None, design=DESIGN, keep_masks=False):
    """One object: eligibility, guards, trim (default policy, plus keep_always when a frame-cut edge is dropped)."""
    rec = {'scene': scene_id, 'id': obj['id'], 'kind': obj['kind'], 'status': obj.get('status'),
           'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'decided': is_decided(obj, design),
           'vertices_before': len(obj.get('ring') or []), 'ring_sha': ring_sha(obj)}
    routes = eligibility(census_row, design)
    if routes is None:
        return {**rec, 'outcome': 'skip', 'reasons': ['not_in_census']}
    rec['census'] = {k: census_row.get(k) for k in ('shadow_share', 'sun_opposite_share', 'shadow_side_excess', 'crown_share',
                                                     'raster_area_m2', 'vertices')}
    rec['census_geometry_changed'] = census_row.get('vertices') != rec['vertices_before']
    rec['eligible_routes'] = routes
    if not routes:
        return {**rec, 'outcome': 'skip', 'reasons': ['not_eligible']}
    rec['sun'] = {'reliable': bool(sun and sun.get('reliable')), 'shadow_bearing': sun.get('shadow_bearing_used') if sun else None,
                  'source': sun.get('shadow_bearing_source') if sun else None,
                  'bootstrap_std_deg': sun.get('bootstrap_circ_std_deg_B') if sun else None, 'fan_deg': design['fan_deg'],
                  'area_mean_bearing': area_bearing}
    if not rec['sun']['reliable'] or rec['sun']['shadow_bearing'] is None:
        return {**rec, 'outcome': 'guard', 'reasons': ['sun_uncertain']}
    poly, valid = object_polygon(obj)
    rec['source_valid'] = valid
    if poly is None:
        return {**rec, 'outcome': 'guard', 'reasons': ['no_polygon']}
    lin = np.asarray(meta['pixel_to_meters'], float)[:2]
    W, H = meta['image_width'], meta['image_height']
    res = trim_object(poly, feats, th, lin=lin, width=W, height=H, shadow_bearing=rec['sun']['shadow_bearing'],
                      area_bearing=area_bearing, design=design)
    rec.update(summarise(res))
    if res['outcome'] == 'proposal' and res.get('frame_cut_dropped'):
        strict = trim_object(poly, feats, th, lin=lin, width=W, height=H, shadow_bearing=rec['sun']['shadow_bearing'],
                             area_bearing=area_bearing, design=design, policy='keep_always')
        rec['keep_always'] = {k: strict.get(k) for k in ('outcome', 'reasons', 'removed_m2', 'removed_share')}
        if strict.get('ring') is not None and strict['outcome'] == 'proposal':
            rec['keep_always']['ring'] = strict['ring']
    if res['outcome'] in ('proposal', 'flag') and res.get('ring') is not None:
        rec['ring_after'] = res['ring']
        rec['holes_after'] = res['holes']
    if res['outcome'] == 'proposal':
        rec['proposal'] = build_proposal(scene_id, obj, res, meta.get('pixel_to_meters'), design)
    if keep_masks:
        rec['_masks'] = {k: res.get(k) for k in ('crop', 'mask', 'crown', 'removed')}
    return rec


def summarise(res):
    keys = ('outcome', 'reasons', 'policy', 'crop', 'mask_px', 'crown_px', 'crown_share', 'removed_px', 'removed_px_share',
            'removed_blue_dark_share', 'removed_dark_share', 'removed_lit_green_share', 'removed_dark_green_share',
            'dark_green_blob_px', 'orphan_px', 'frame_cut_sides',
            'frame_cut_crown_sides', 'frame_cut_dropped', 'frame_cut_dropped_px', 'area_before_m2', 'area_after_m2',
            'removed_m2', 'removed_share', 'iou_before_after', 'shadow_arc_deg', 'simplify_px_used', 'cut_base', 'removed_centres_kept', 'made_valid', 'dropped_parts_m2',
            'ring_repairs', 'components')
    out = {k: res.get(k) for k in keys if k in res}
    if res.get('ring') is not None:
        out['vertices_after'] = len(res['ring'])
    return out


# ------------------------------------------------------------------------------------------------------------- run
def census_rows():
    rows = {}
    with open(CENSUS_DIR/'objects.jsonl') as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[(row['scene'], row['layer'], row['id'])] = row
    return rows


def hand_trace_iou(rec):
    case = json.load(open(HAND_TRACE))
    hand = Polygon(case['rings']['after_round2'])
    before = Polygon(case['rings']['before_rev56'])
    out = {'hand_trace': 'example/case.json rings.after_round2 (preview 2a9d3cec, 22 vertices)',
           'hand_area_px2': r(hand.area, 2), 'before_area_px2': r(before.area, 2), 'iou_before_vs_hand': r(iou(before, hand))}
    ring = rec.get('ring_after')
    if ring:
        prop = Polygon(ring, rec.get('holes_after') or [])
        out.update({'proposal_area_px2': r(prop.area, 2), 'iou_proposal_vs_hand': r(iou(prop, hand)),
                    'proposal_minus_hand_px2': r(prop.difference(hand).area, 2), 'hand_minus_proposal_px2': r(hand.difference(prop).area, 2)})
    strict = (rec.get('keep_always') or {}).get('ring')
    if strict:
        out['iou_keep_always_vs_hand'] = r(iou(Polygon(strict), hand))
    return out


def iou(a, b):
    u = a.union(b).area
    return a.intersection(b).area/u if u > 0 else 0.


def run(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    th = sc.load_thresholds()
    sun_doc = json.load(open(CENSUS_DIR/'sun.json'))
    sun_all = sun_doc['windows']
    area_means = {a: v.get('shadow_bearing_mean_reliable') for a, v in sun_doc['areas'].items()}
    rows = census_rows()
    scenes = sc.window_ids() if not args.scenes else args.scenes
    review_records, model_records, scene_meta, example_window_only = [], [], {}, None
    heartbeat(f'run start {len(scenes)} windows')
    for i, scene_id in enumerate(scenes):
        scene_dir, meta, review = sc.load_scene(scene_id)
        _, _, mpp = sc.pixel_geometry(meta)
        feats = sc.frame_features(scene_dir/f"{meta['image_sha256']}.png", mpp)
        sun = sun_all.get(scene_id)
        scene_meta[scene_id] = {'review_revision': (review or {}).get('revision'), 'review_updated_at': (review or {}).get('updated_at'),
                                'image_sha256': meta['image_sha256'], 'area': sc.area_group(meta), 'm_per_px': r(mpp, 5),
                                'sun_reliable': bool(sun and sun.get('reliable')),
                                'shadow_bearing': sun.get('shadow_bearing_used') if sun else None}
        for layer, objects, sink in (('review', sc.live_review_objects(review), review_records),
                                     ('model', meta.get('objects') or [], model_records)):
            for obj in objects:
                if obj.get('kind') not in DESIGN['kinds']:
                    continue
                rec = process_object(scene_id, obj, feats, th, meta, sun, rows.get((scene_id, layer, obj['id'])),
                                     area_bearing=area_means.get(sc.area_group(meta)))
                if layer == 'review' and (scene_id, obj['id']) == REVIEWER_EXAMPLE:
                    alt = process_object(scene_id, obj, feats, th, meta, sun, rows.get((scene_id, layer, obj['id'])),
                                         design={**DESIGN, 'fan_area_union': False})
                    example_window_only = {k: alt.get(k) for k in ('outcome', 'reasons', 'removed_m2', 'removed_share',
                                                                   'shadow_arc_deg', 'ring_after', 'holes_after')}
                rec['layer'] = layer
                rec['area'] = scene_meta[scene_id]['area']
                sink.append(rec)
        del feats
        heartbeat(f'{i+1}/{len(scenes)} {scene_id} review {len(review_records)} model {len(model_records)}')
    with open(OUT_DIR/'records.jsonl', 'w') as handle:
        for rec in review_records:
            handle.write(json.dumps(rec, separators=(',', ':'))+'\n')
    with open(OUT_DIR/'model-records.jsonl', 'w') as handle:
        for rec in model_records:
            slim = {k: v for k, v in rec.items() if k not in ('components', 'ring_after', 'holes_after', 'proposal')}
            handle.write(json.dumps(slim, separators=(',', ':'))+'\n')
    with open(OUT_DIR/'proposals.jsonl', 'w') as handle:
        for rec in review_records:
            if rec.get('proposal'):
                handle.write(json.dumps({'scene': rec['scene'], **rec['proposal']}, separators=(',', ':'))+'\n')
    example = next((x for x in review_records if (x['scene'], x['id']) == REVIEWER_EXAMPLE), None)
    summary = {'version': VERSION, 'computed_at': now(), 'code_sha256': CODE_SHA256, 'census_code_sha256': sc.CODE_SHA256,
               'design': DESIGN, 'thresholds': th, 'disclosures': DISCLOSURES, 'windows': len(scenes), 'scene_meta': scene_meta,
               'counts': {'review': counts(review_records), 'model': counts(model_records)},
               'example': {'record': {k: v for k, v in (example or {}).items() if k not in ('components',)},
                           'vs_hand_trace': hand_trace_iou(example) if example else None,
                           'window_bearing_only': {**(example_window_only or {}),
                                                   'vs_hand_trace': hand_trace_iou(example_window_only) if example_window_only else None}},
               'note': 'Sidecar proposals only; nothing applied; read-only on data/scenes; no inference. Counts and geometry, not accuracy.'}
    (OUT_DIR/'summary.json').write_text(json.dumps(summary, indent=1))
    heartbeat('run done')
    print(json.dumps({'review': summary['counts']['review']['outcomes'], 'model': summary['counts']['model']['outcomes'],
                      'example': summary['example']['vs_hand_trace']}, indent=None)[:1500])


def counts(records):
    out = {'objects': len(records), 'outcomes': dict(Counter(x['outcome'] for x in records)),
           'reasons': dict(Counter(f"{x['outcome']}:{x['reasons'][0]}" for x in records if x.get('reasons')))}
    by = defaultdict(Counter)
    m2 = defaultdict(float)
    for x in records:
        by[f"{x['kind']}/{x['area']}"][x['outcome']] += 1
        if x['outcome'] == 'proposal':
            m2[x['kind']] += x['removed_m2']
            m2['all'] += x['removed_m2']
    out['by_kind_area'] = {k: dict(v) for k, v in sorted(by.items())}
    props = [x for x in records if x['outcome'] == 'proposal']
    out['proposals'] = len(props)
    out['proposals_by_kind'] = dict(Counter(x['kind'] for x in props))
    out['proposals_by_decision'] = dict(Counter(x['status'] for x in props))
    out['proposals_by_route'] = dict(Counter('+'.join(x['eligible_routes']) for x in props))
    out['removed_m2'] = {k: r(v, 2) for k, v in m2.items()}
    out['area_before_m2_proposals'] = r(sum(x['area_before_m2'] for x in props), 2)
    shares = sorted(x['removed_share'] for x in props)
    out['removed_share_quartiles'] = [r(np.percentile(shares, q), 3) for q in (0, 25, 50, 75, 100)] if shares else []
    out['frame_cut_dropped'] = sum(1 for x in props if x.get('frame_cut_dropped'))
    out['frame_cut_dropped_keep_always'] = dict(Counter((x.get('keep_always') or {}).get('outcome') for x in props
                                                        if x.get('frame_cut_dropped')))
    out['eligible'] = sum(1 for x in records if x.get('eligible_routes'))
    out['eligible_by_route'] = dict(Counter('+'.join(x['eligible_routes']) for x in records if x.get('eligible_routes')))
    out['census_geometry_changed_eligible'] = sum(1 for x in records if x.get('eligible_routes') and x.get('census_geometry_changed'))
    return out


# ------------------------------------------------------------------------------------------------------------- renders
MAGENTA, YELLOW, CYAN, FRAME_RED = (255, 0, 255), (255, 230, 0), (0, 255, 255), (255, 48, 48)
_SHIFT = 4


def _pts(ring, ox, oy, k):
    return np.array([[int(round((x-ox)*k*(1 << _SHIFT))), int(round((y-oy)*k*(1 << _SHIFT)))] for x, y in ring], np.int32)


def _dashed(img, p, q, colour, thickness=1, dash=5.):
    p, q = np.asarray(p, float), np.asarray(q, float)
    steps = max(1, int(np.hypot(*(q-p))//dash))
    for i in range(0, steps, 2):
        a, b = p+(q-p)*i/steps, p+(q-p)*min(i+1, steps)/steps
        cv2.line(img, tuple(int(round(v)) for v in a), tuple(int(round(v)) for v in b), colour, thickness, cv2.LINE_AA)


def _text(img, text, org, scale=.42, colour=(255, 255, 255)):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 1, cv2.LINE_AA)


def render_record(rgb, rec, meta, n, out_png, margin=14, target=420, max_k=6):
    """raw (left) | annotated (right): before outline yellow, after outline cyan, removed area magenta 25 %, the direction the
    light travels (white arrow from a sun disc) at the top-left, frame-cut outline edges dashed red. Native pixels, integer
    nearest-neighbour zoom."""
    W, H = meta['image_width'], meta['image_height']
    before, _ = object_polygon({'ring': rec['_ring_before'], 'holes': rec.get('_holes_before') or []})
    after = Polygon(rec['ring_after'], rec.get('holes_after') or []) if rec.get('ring_after') else None
    minx, miny, maxx, maxy = before.bounds
    ox, oy = max(0, int(minx)-margin), max(0, int(miny)-margin)
    ex, ey = min(W, int(math.ceil(maxx))+margin), min(H, int(math.ceil(maxy))+margin)
    k = int(max(1, min(max_k, round(target/max(ex-ox, ey-oy)))))
    crop = np.ascontiguousarray(rgb[oy:ey, ox:ex])
    base = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_NEAREST) if k > 1 else crop.copy()
    raw = base.copy()
    ann = base.copy()
    removed = before.difference(after) if after is not None else None
    if removed is not None and not removed.is_empty:
        fill = np.zeros(ann.shape[:2], np.uint8)
        for p in iter_polygons(removed):
            cv2.fillPoly(fill, [_pts(list(p.exterior.coords), ox, oy, k)], 1, cv2.LINE_8, _SHIFT)
            for h in p.interiors:
                cv2.fillPoly(fill, [_pts(list(h.coords), ox, oy, k)], 0, cv2.LINE_8, _SHIFT)
        tint = ann.copy()
        tint[fill > 0] = MAGENTA
        blend = cv2.addWeighted(ann, .75, tint, .25, 0)
        ann[fill > 0] = blend[fill > 0]
    cv2.polylines(ann, [_pts(list(before.exterior.coords), ox, oy, k)], True, YELLOW, 1, cv2.LINE_AA, _SHIFT)
    if after is not None:
        cv2.polylines(ann, [_pts(list(after.exterior.coords), ox, oy, k)], True, CYAN, 2 if k >= 3 else 1, cv2.LINE_AA, _SHIFT)
        for h in after.interiors:
            cv2.polylines(ann, [_pts(list(h.coords), ox, oy, k)], True, CYAN, 1, cv2.LINE_AA, _SHIFT)

    c = list(before.exterior.coords)
    for a, b in zip(c, c[1:], strict=False):
        if any(abs(a[i]-edge) <= 1 and abs(b[i]-edge) <= 1 for i, edge in ((0, 0.), (0, W), (1, 0.), (1, H))):
            _dashed(ann, ((a[0]-ox)*k, (a[1]-oy)*k), ((b[0]-ox)*k, (b[1]-oy)*k), FRAME_RED, 3, 5.)
    # light direction: from a sun disc towards the shadow bearing
    lin = np.asarray(meta['pixel_to_meters'], float)[:2]
    u = sc.pixel_vector_of_bearing(lin, rec['sun']['shadow_bearing'])
    c0 = np.array([30., 34.])
    tail, head = c0-u*20, c0+u*20
    cv2.circle(ann, tuple(int(v) for v in tail), 6, (255, 200, 0), -1, cv2.LINE_AA)
    cv2.arrowedLine(ann, tuple(int(v) for v in tail), tuple(int(v) for v in head), (255, 255, 255), 2, cv2.LINE_AA, tipLength=.35)
    _text(ann, f"light -> {rec['sun']['shadow_bearing']:.0f} deg", (4, 70), .38)
    gap = np.full((raw.shape[0], 6, 3), 40, np.uint8)
    row = np.hstack([raw, gap, ann])
    if row.shape[1] < 900:
        row = np.hstack([row, np.full((row.shape[0], 900-row.shape[1], 3), 24, np.uint8)])
    keep_always = rec.get('keep_always')
    if after is None:
        lines = [f"#{n}  {rec['scene']} / {rec['id']}  {rec['kind']}  status {rec['status']}  (raw native px x{k} | annotated)",
                 f"FLAG {rec['reasons'][0]}: no lit-foliage crown body inside the outline ({rec.get('mask_px')} px); nothing proposed",
                 f"census share {pct(rec['census']['shadow_share'])}, excess {pct(rec['census']['shadow_side_excess'])}; "
                 f"light -> {rec['sun']['shadow_bearing']:.0f} deg (window)",
                 'yellow = saved outline, red dashes = outline edge on the image frame']
        return _finish_sheet(row, lines, out_png)
    lines = [f"#{n}  {rec['scene']} / {rec['id']}  {rec['kind']}  status {rec['status']}  (raw native px x{k} | annotated)",
             f"before {rec['area_before_m2']:.2f} m2 ({rec['vertices_before']} v) -> after {rec['area_after_m2']:.2f} m2 "
             f"({rec.get('vertices_after')} v); removed {rec['removed_m2']:.2f} m2 = {100*rec['removed_share']:.0f} %",
             f"removed px: blue-dark {100*rec['removed_blue_dark_share']:.0f} %, dark {100*rec['removed_dark_share']:.0f} %, "
             f"lit green {100*rec['removed_lit_green_share']:.0f} %; census share {pct(rec['census']['shadow_share'])}, "
             f"excess {pct(rec['census']['shadow_side_excess'])}",
             f"light -> {rec['sun']['shadow_bearing']:.0f} deg (window); shadow arc searched "
             f"{rec['shadow_arc_deg'][0]:.0f}-{rec['shadow_arc_deg'][1]:.0f} deg (window +- 30, area mean {rec['sun']['area_mean_bearing']})"
             + (f"; frame-cut edge dropped: {', '.join(rec['frame_cut_dropped'])} (keep_always -> {keep_always.get('outcome')})"
                if rec.get('frame_cut_dropped') and keep_always else ''),
             'yellow = saved outline, cyan = proposed, magenta = removed (shadow), red dashes = outline edge on the image frame']
    return _finish_sheet(row, lines, out_png)


def pct(value):
    return '-' if value is None else f'{100*value:.0f} %'


def _finish_sheet(row, lines, out_png):
    bar = np.full((8+16*len(lines), row.shape[1], 3), 20, np.uint8)
    for i, line in enumerate(lines):
        cv2.putText(bar, line, (6, 15+16*i), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1, cv2.LINE_AA)
    sheet = np.vstack([bar, row])
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(sheet).save(out_png)
    return sheet


def contact_sheets(paths, out_dir, per_sheet=6):
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for s in range(0, len(paths), per_sheet):
        imgs = [np.asarray(Image.open(p).convert('RGB')) for p in paths[s:s+per_sheet]]
        width = max(i.shape[1] for i in imgs)
        rows = [np.hstack([i, np.full((i.shape[0], width-i.shape[1], 3), 24, np.uint8)]) if i.shape[1] < width else i for i in imgs]
        sep = np.full((6, width, 3), 60, np.uint8)
        sheet = np.vstack([x for row in rows for x in (row, sep)][:-1])
        path = out_dir/f'contact-{s//per_sheet+1:02d}.png'
        Image.fromarray(sheet).save(path)
        made.append(path)
    return made


def mark_order(records):
    props = [x for x in records if x['outcome'] == 'proposal']
    first = [x for x in props if (x['scene'], x['id']) == REVIEWER_EXAMPLE]
    rest = [x for x in props if (x['scene'], x['id']) != REVIEWER_EXAMPLE]
    rest.sort(key=lambda x: (not x['decided'], -x['removed_m2'], x['scene'], x['id']))
    return first+rest


def render(args):
    records = [json.loads(line) for line in open(OUT_DIR/'records.jsonl') if line.strip()]
    order = mark_order(records)
    flags = sorted([x for x in records if x['outcome'] == 'flag'], key=lambda x: (x['reasons'][0], x['scene'], x['id']))
    by_scene = defaultdict(list)
    for n, rec in enumerate(order, 1):
        by_scene[rec['scene']].append((n, rec, 'renders'))
    for n, rec in enumerate(flags, 1):
        by_scene[rec['scene']].append((n, rec, 'renders-flagged'))
    paths, flag_paths = {}, {}
    for folder in ('renders', 'renders-flagged', 'contact', 'contact-flagged'):
        for old in (OUT_DIR/folder).glob('*.png'):
            old.unlink()
    heartbeat(f'render start {len(order)} proposals, {len(flags)} flagged with geometry')
    for scene_id in sorted(by_scene):
        scene_dir, meta, review = sc.load_scene(scene_id)
        rgb = np.asarray(Image.open(scene_dir/f"{meta['image_sha256']}.png").convert('RGB'))
        live = {o['id']: o for o in sc.live_review_objects(review)}
        for n, rec, folder in by_scene[scene_id]:
            obj = live.get(rec['id'])
            if obj is None or ring_sha(obj) != rec['ring_sha']:
                rec['stale'] = True
                continue
            rec['_ring_before'], rec['_holes_before'] = obj['ring'], obj.get('holes') or []
            name = f"{n:03d}-{scene_id}-{rec['id']}.png"
            render_record(rgb, rec, meta, n if folder == 'renders' else f'F{n}', OUT_DIR/folder/name)
            (paths if folder == 'renders' else flag_paths)[(scene_id, rec['id'])] = OUT_DIR/folder/name
    ordered = [paths[(x['scene'], x['id'])] for x in order if (x['scene'], x['id']) in paths]
    sheets = contact_sheets(ordered, OUT_DIR/'contact')
    flag_sheets = contact_sheets([flag_paths[k] for k in sorted(flag_paths)], OUT_DIR/'contact-flagged') if flag_paths else []
    write_marks(order, paths, records, [x for x in flags if (x['scene'], x['id']) in flag_paths], flag_paths)
    heartbeat(f'render done {len(ordered)} renders, {len(sheets)} sheets, {len(flag_paths)} flagged renders')
    print(json.dumps({'renders': len(ordered), 'contact_sheets': len(sheets), 'flagged_renders': len(flag_paths),
                      'flag_sheets': len(flag_sheets), 'stale': sum(1 for x in order if x.get('stale'))}))


def write_marks(order, paths, records, flags=(), flag_paths=None):
    summary = json.load(open(OUT_DIR/'summary.json'))
    ex = summary['example']['vs_hand_trace'] or {}
    c = summary['counts']['review']
    lines = ['# MARKS: shadow trim v1 (tree-shadow-2026-10-08)', '',
             f"Sidecar proposals from `scripts/ml/experiments/shadow_trim.py` ({VERSION}, computed {summary['computed_at']}). Nothing is "
             'applied: each item is a proposed tree / vegetation outline with the cast shadow cut away. The saved outline is unchanged.',
             '', '**How to answer (in chat):** the item number and one word, for example `1 right, 2 wrong, 3 unclear`.',
             '- **right**: the magenta part is shadow (or ground) and the cyan outline keeps the whole crown.',
             '- **wrong**: the cut takes crown, or leaves most of the shadow in. A few words on which helps (e.g. `2 wrong: cuts crown`).',
             '- **unclear**: you can not tell from the image.', '',
             'Each render: left = raw image at native pixels (integer zoom); right = yellow saved outline, cyan proposed outline, '
             'magenta (25 %) removed area, white arrow = the direction the light travels (from the window\'s building shadows), '
             'red dashes = an outline edge on the image frame.', '',
             f"Proposals: {c['proposals']} of {c['eligible']} eligible review-layer objects "
             f"({c['proposals_by_kind']}); removed {c['removed_m2'].get('all', 0)} m2 in total. "
             f"Reviewer example vs the hand trace (preview 2a9d3cec): IoU {ex.get('iou_proposal_vs_hand')}.", '']
    for n, rec in enumerate(order, 1):
        path = paths.get((rec['scene'], rec['id']))
        if path is None:
            continue
        tag = ' (your example)' if (rec['scene'], rec['id']) == REVIEWER_EXAMPLE else ''
        frame = (f"; drops the frame-cut edge on the {', '.join(rec['frame_cut_dropped'])} (it lies in the shadow)"
                 if rec.get('frame_cut_dropped') else '')
        lines += [f"{n}. **{rec['kind']}** {rec['scene']} / `{rec['id']}`{tag}: {rec['status']}"
                  f"{' (proposal only, replaces on accept)' if rec['decided'] else ''}; {rec['area_before_m2']:.2f} -> "
                  f"{rec['area_after_m2']:.2f} m2, removes {rec['removed_m2']:.2f} m2 ({100*rec['removed_share']:.0f} %){frame}. "
                  f"[open]({APP_URL.format(scene=rec['scene'], id=rec['id'])}) | render `{path.relative_to(OUT_DIR)}`"]
    if flags:
        lines += ['', '## Flagged, nothing proposed', '',
                  '`over_60pct`: the trim would remove more than 60 % of the outline. `no_crown`: no sunlit-foliage crown inside '
                  'the outline at all. Both look mostly shadow to the rule. Answer `F1 right` if the outline is indeed mostly '
                  'shadow, `F1 wrong` if it is mostly tree.', '']
        for n, rec in enumerate(flags, 1):
            path = flag_paths[(rec['scene'], rec['id'])]
            what = (f"the rule would remove {rec['removed_m2']:.2f} of {rec['area_before_m2']:.2f} m2 ({100*rec['removed_share']:.0f} %)"
                    if rec.get('removed_m2') is not None else f"no crown body in {rec['census'].get('raster_area_m2')} m2")
            lines += [f"F{n}. **{rec['kind']}** {rec['scene']} / `{rec['id']}`: {rec['status']}; `{rec['reasons'][0]}`, {what}. "
                      f"[open]({APP_URL.format(scene=rec['scene'], id=rec['id'])}) | render `{path.relative_to(OUT_DIR)}`"]
    (OUT_DIR/'MARKS-shadow-v1.md').write_text('\n'.join(lines)+'\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_run = sub.add_parser('run')
    p_run.add_argument('--scenes', nargs='*')
    sub.add_parser('render')
    args = parser.parse_args(argv)
    {'run': run, 'render': render}[args.cmd](args)


if __name__ == '__main__':
    main()
