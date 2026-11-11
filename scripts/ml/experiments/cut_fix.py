"""Cut-fix prototype (2026-10-08): PROPOSALS ONLY. Read-only over data/scenes; no model is loaded, nothing is re-inferred.

Reviewer (chat, 8 Oct ~08:45 AEDT, verbatim): "still seeing cuts randomly which then impactd the roof boundsary or tree boundary:
  http://localhost:3008/scenes?scene=win-3dd37155248c04ef&status=all&frame=all&object=1bac44c96bf45847&layer=edited
  http://localhost:3008/scenes?scene=win-3dd37155248c04ef&status=all&frame=all&object=55ca4c5c041e3646&layer=edited"
Census (object-cuts-2026-10-08/census, cut_census.py): nothing in the window pipeline cuts one object out of another. The cuts
come from the window frame (scene_extract.py:1212), the detector's mask stopping at a canopy that hides part of a roof, and
frame notches that straightening sharpens into spikes.

Population: the 54 windows (data/scenes/win-*, lot-* with latest.json); layer = review.json objects (what the editor shows as
layer=edited; latest.json when a window has no review); live = status not rejected / superseded.

Proposal families (each a sidecar record holding a proposed ring; nothing is applied, nothing under data/scenes is written):

  canopy_completion   (un-carve, roofs) A roof outline that stops along a tree canopy. On the exterior ring simplified at
                      `simplify_m` (cut_census CORNER), an edge runs ALONG a canopy when >= `canopy_edge_share` of its length lies
                      within max(`canopy_near_px`, `canopy_near_m`) of a live tree. A maximal run of such edges (not on the frame)
                      between two ARMS is a canopy cut. Both arms must be visible eave lines on either side of the canopy:
                      >= `arm_min_m` long, >= `arm_visible_share` outside the canopy buffered `canopy_buffer_m`, median RGB step
                      >= `arm_step_rgb` (cut_census rgb_step, roof_boundary_guidance's 18). Straight-edge completion along the
                      roof's own eave lines: 'corner' when the arms meet at `corner_angle_deg` outside the roof (extension of each
                      arm <= `arm_extension_max_m`); 'straight' when the arms are one eave line (direction within
                      `straight_angle_deg`, lateral offset <= `straight_offset_m`). The run's interior vertices are replaced by the
                      corner (or removed). Sources are tried in the brief's order of evidence: stored pre-carve geometry (same id
                      in latest.json, models/*.json, boundary_refinement.raw_ring) when it covers >= `precarve_cover` of the
                      completion; else the straight-edge completion; an overlapping frame's copy (below) is recorded beside it as
                      corroboration (copy_cover of the completion) and becomes the source only when the straight-edge completion
                      is refused or impossible.
  copy_uncarve        (un-carve, roofs and trees) The same object detected in another registered frame (window or not), found by
                      georeferenced IoU >= `copy_iou_min` inside this window, moved through scene_recapture.measured_transform
                      (role frame_cut_target, DESIGN.json verdict; the lot-29362 / section 29-31 path) and VERIFIED. The part of
                      the projected copy inside the hidden zone (the canopy / partner buffered `canopy_buffer_m`, within
                      `hidden_max_m` of the cut run, outside the object, inside the window by 1 px), opened by `copy_open_px`, is
                      added when >= `copy_min_m2`. Trees 'carved by a roof' use the roof as the partner.
  spike_fill          (frame notches) cut_census.find_spikes' notch: an off-frame vertex run between two on-frame vertices of one
                      side, chord < 1.5 m, depth >= 3 px and >= half its width. The run's off-frame vertices are removed (the edge
                      returns to the frame line). A 'tab' (the run encloses object area) and a protrusion are HELD (a narrow
                      on-frame touch is mostly a roof corner clipped by the frame: removing it deletes observed outline).
                      A notch another live object explains (any kind; for roofs not a solar panel or skylight) is a real boundary
                      and is held; a tree / vegetation fill must also match the object's material and stay <= `soft_notch_max_m2`,
                      and a vegetation notch whose walls lie on a strong image step (>= `arm_step_rgb`) traces a real object (a
                      shrub cut out of a lawn) and is held; a roof fill is exempt (1bac's slit is a bright roof fixture).
  body_carry          (frame-cut roofs) Every live building with >= 0.5 m of outline on the window frame: the buildings of other
                      registered frames that hold the roof WHOLE (no vertex within 1 px of their own frame), cover >= `body_cover_listed`
                      of it and reach beyond this window's frame (>= `body_beyond_min` of its area) are listed; the best
                      `body_measure_top` by georeferenced cover are measured (verified transform as above). A carry is PROPOSED when
                      the transform verifies, the body covers >= `body_cover_whole` of the window outline, its in-frame part has
                      IoU >= `body_inframe_iou_min` with it and its pixels are no coarser than `body_resolution_max` x the window's
                      (the census: coarser bodies give worse outlines) and its in-frame part drops no more than
                      max(`body_drop_max_m2`, `body_drop_max_share` x the object) of the current outline (a body that drops
                      observed outline replaces a sharper in-frame trace). The proposal ring is the projected body clipped to the
                      window (the app keeps outlines inside the image); the whole projected ring and the holder frame are recorded
                      (continues_beyond_frame), with the area the in-frame part drops from and adds to the current outline.
                      Carried copies (slices) are left to the slice-body census (section 29-31). A copy un-carve uses copies
                      no coarser than `body_resolution_max` x the window's pixels only (a coarse copy's edge is blur).

Guards (every family): the proposal never leaves the window (box(0, 0, W, H); a completion corner stays >= `frame_margin_px`
inside); a completion / fill / copy part may not cover another live object of the blocker kinds by more than
max(`blocker_m2`, `blocker_share` x its area) (roofs: building, road, driveway, footpath, pool, water, car, vegetation; trees:
building; vegetation: building, road, driveway, footpath, pool, water, car); a roof completion must lie >= `completion_canopy_share`
under the canopy; its visible part (outside the canopy) must match the roof's own material (roof_surface.refine_roof_surface's
chroma / brightness / green rule) and the removed run may not carry a strong image step (>= `arm_step_rgb`) outside the canopy over
>= `visible_edge_m` (a visible roof edge); the ring passes make_valid (one valid Polygon) and the app's outline rules after the two
lossless repairs (scene_extract.finalise_app_ring with APP_RING_RULES and the image size). Confirmed / needs_adjustment /
geometry-reviewed objects and reviewer-edited rings (review ring != model ring) and carried copies get proposals only:
lineage replaces_on_accept = [id]; an undecided candidate gets replaces = [id]; derived_from = [id] always (road-corridor lineage).
A body carry also records carried_from {scene_id, model_signature, object_id, status, geometry_reviewed, iou_with_new, matched_id,
method, role 'whole_roof_body'} as scene_focus did for lot-29362.

Every figure is a count, an area, a length or a measured transform agreement; none is accuracy.

v2 (DESIGN_V2, `--design v2`; v1 = DESIGN stays the default and reproduces prototype/ byte for byte apart from code sha,
time and runtime). The by-eye skeptic of v1 (.run/session-2026-10-08-cuts-veg/eye-cuts.md: 79 lines read, 58 right / 8 wrong /
13 unclear) found three wrong-line groups; v2 adds one guard per group and leaves every other rule as v1:
  copy_uncarve        refused ('adds_spike_or_sliver') when the RESULT ring has an excursion the current ring does not have
                      (spike_guard): cut_census.find_spikes on the result (frame notch / tab / protrusion narrower than
                      `notch_max_m` 1 m); the same notch test anchored on any chord of the ring instead of the frame
                      (ring_excursions: a run between two ring points whose chord is < 1 m and that goes deeper than its chord and
                      >= 3 px = SPIKE_MIN_DEPTH_PX from it, a notch outside or a protrusion inside; a part narrower than
                      `part_max_m` 0.5 m from >= 1.5 px deep); and the skeptic's slit scan (a vertex sharper than 25 deg with both
                      arms >= 1.5 px). New = the tip lies on the change (result xor current, 1 px) and no current-ring feature of
                      the same test lies within 2 px. Copies keep v1's resolution rule (`copy_resolution_max` 1.25).
  canopy_completion   stops at the occluding canopy's footprint (the run's partner trees): the completed corner must lie inside
                      the partner canopy buffered `corner_buffer_m` 0.3 m (else 'eaves_meet_outside_canopy') and the completion's
                      depth beyond the current outline may not exceed the depth that canopy (buffered 0.3 m) reaches inside the
                      completion (else 'completion_deeper_than_canopy').
  body_carry          only from bodies no coarser than the window (m/px ratio <= 1.0), verified transform, cover >= 0.95, in-frame
                      IoU >= 0.9 (the skeptic's fix for 105 / 106) and never a body the skeptic marked wrong.
  skeptic holds       the 8 v1 lines the skeptic marked wrong (SKEPTIC_WRONG_V1) are never proposed again with the same ring
                      (body carry: the same holder body): 'skeptic_marked_wrong_v1'. The mechanical guards are reported first;
                      the hold is the backstop (and the only thing that holds canopy completions 19 and 26, whose corners lie
                      0.49 m and 0.66 m INSIDE their partner tree outlines: the footprint guard cannot see them).

Run from the repo root:
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.cut_fix [--design v1|v2] [--windows ...] [--skip-renders]
        [--skip-frames] [--out DIR] [--run DIR]
v1 outputs ONLY under data/reviews/eyes/experiments/object-cuts-2026-10-08/prototype/ (proposals.json, proposals.jsonl,
MARKS-cuts-v1.md, renders/<window>/<n>-<type>-<object>.png, renders/contact-sheet.png) and heartbeat lines in
.run/session-2026-10-08-cuts-veg/cutfix/progress.txt; v2 under prototype-v2/ (the same files, MARKS-cuts-v2.md, changes.json =
every v1 line kept, changed or removed) and .run/session-2026-10-08-cuts-veg/cuts-v2/progress.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import shapely
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree
from shapely import make_valid
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .. import scene_recapture as sr
from ..roof_surface import _pixels
from ..scene_extract import APP_RING_RULES, _ring_details, finalise_app_ring
from .cut_census import (
    FRAME_TOL_PX,
    HAS_FRAME_M,
    LIVE_EXCLUDED,
    NAMED,
    SCENES,
    SCRATCH_PREFIX,
    SPIKE_AREA_SHARE,
    SPIKE_MAX_WIDTH_M,
    SPIKE_MIN_ASPECT,
    SPIKE_MIN_DEPTH_PX,
    WINDOW_PREFIXES,
    clean_ring,
    dashed,
    file_sha,
    find_spikes,
    font,
    frame_distance,
    load_json,
    mercator_origin,
    object_polygon,
    pixels_to_mercator,
    polygonal,
    population,
    rgb_step,
    ring_samples,
    shoelace,
    side_depth,
    sides_of,
    spacing,
)

VERSION = 'cut-fix-v1'
ROOT = Path(__file__).resolve().parents[3]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
OUT = ROOT/'data/reviews/eyes/experiments/object-cuts-2026-10-08/prototype'
CENSUS = ROOT/'data/reviews/eyes/experiments/object-cuts-2026-10-08/census/objects.jsonl'
RUN = ROOT/'.run/session-2026-10-08-cuts-veg/cutfix'
KINDS = ('building', 'tree', 'vegetation')
DESIGN = {
    'version': VERSION,
    'simplify_m': .35, 'canopy_buffer_m': .35, 'canopy_near_px': 4., 'canopy_near_m': .35, 'canopy_edge_share': .6,
    'arm_min_m': 1.5, 'arm_visible_share': .8, 'arm_step_rgb': 18.,
    'corner_angle_deg': (60., 120.), 'straight_angle_deg': 12., 'straight_offset_m': .35, 'arm_extension_max_m': 4.,
    'hidden_max_m': 2., 'completion_min_m2': .15, 'completion_max_m2': 25., 'completion_max_share': .25,
    'completion_canopy_share': .6, 'visible_check_m2': .25, 'material': {'chroma': 14., 'brightness': 35., 'green': .12},
    'visible_edge_m': 1., 'precarve_cover': .5,
    'blockers': {'building': ('building', 'road', 'driveway', 'footpath', 'pool', 'water', 'car', 'vegetation'),
                 'tree': ('building',), 'vegetation': ('building', 'road', 'driveway', 'footpath', 'pool', 'water', 'car')},
    'blocker_m2': .1, 'blocker_share': .1, 'frame_margin_px': 1.,
    'spike_blocker_skip': {'building': ('solar_panel', 'skylight')}, 'soft_notch_max_m2': 1.,
    'decided_statuses': ('confirmed', 'needs_adjustment'),
    'copy_iou_min': .5, 'copy_min_m2': .3, 'copy_open_px': 1., 'copy_simplify_px': .5,
    'body_cover_whole': .8, 'body_cover_listed': .5, 'body_beyond_min': .02, 'body_resolution_max': 1.25,
    'body_inframe_iou_min': .6, 'body_measure_top': 3, 'body_drop_max_m2': 1., 'body_drop_max_share': .05,
    'spike_max_loss_share': .25, 'ring_decimals': 2,
}
VERSION_V2 = 'cut-fix-v2'
OUT_V2 = ROOT/'data/reviews/eyes/experiments/object-cuts-2026-10-08/prototype-v2'
RUN_V2 = ROOT/'.run/session-2026-10-08-cuts-veg/cuts-v2'
DESIGN_V2 = {
    **DESIGN, 'version': VERSION_V2,
    # copy un-carve: v1's copy resolution rule kept (it read body_resolution_max), the result ring may add no spike / sliver
    'copy_resolution_max': 1.25,
    'copy_spike_guard': {'notch_max_m': 1., 'min_depth_px': SPIKE_MIN_DEPTH_PX, 'part_max_m': .5, 'part_min_depth_px': 1.5,
                         'min_aspect': 1., 'sharp_deg': 25., 'sharp_arm_px': 1.5, 'sharp_dedupe_px': .3, 'new_tol_px': 2.,
                         'change_px': 1., 'step_px': 1., 'max_arc_m': 20.},
    # canopy completion: the corner inside the occluding canopy + 0.3 m, no deeper than that canopy reaches
    'canopy_footprint': {'corner_buffer_m': .3, 'depth_buffer_m': .3, 'depth_tol_m': .05, 'sample_px': .5},
    # body carry: at the window's resolution or finer, cover >= 0.95, in-frame IoU >= 0.9 (eye-cuts.md group 3)
    # ratio <= 1.0 up to 0.5 %: the same zoom read at another latitude gives 1.00004-1.0009 here (Mercator scale), and 1.0
    # itself can come out a few ulp above 1
    'body_resolution_max': 1., 'body_resolution_tol': .005, 'body_cover_whole': .95, 'body_inframe_iou_min': .9,
    'hold_skeptic_wrong': True,
}
# The v1 lines the by-eye skeptic marked WRONG (eye-cuts.md, 2026-10-08 11:15). Key = (type, window, object, cut_fix index);
# ring = sha256 of json [ring, holes] (compact separators), first 16 hex; body carries are keyed on the holder body instead.
SKEPTIC_WRONG_V1 = (
    {'v1': 19, 'type': 'canopy_completion', 'window': 'win-687c9f594f1601ac', 'object_id': 'bc85622bd35a17c0', 'index': 1,
     'ring': '4e84fc874bacecc1', 'why': 'the roof corner is visible at the current line; the corner goes ~0.9 m into shadow and hedge'},
    {'v1': 26, 'type': 'canopy_completion', 'window': 'win-caec3328e2a28dd0', 'object_id': '1862bbab7cb26b6b', 'index': 0,
     'ring': '536f51b68c9b7b0f', 'why': 'the roof end is visible with its cast shadow; the triangle leaves the visible top eave'},
    {'v1': 34, 'type': 'copy_uncarve', 'window': 'lot-29385', 'object_id': '6d25443197b562ca', 'index': 0,
     'ring': '3a45b35e7f479c67', 'why': 'adds a self-touching sliver'},
    {'v1': 36, 'type': 'copy_uncarve', 'window': 'win-2d422aac4db9f40d', 'object_id': '4442111549782f27', 'index': 0,
     'ring': '0877f05c39bec6b6', 'why': 'adds a ~0.5 m needle into the canopy'},
    {'v1': 37, 'type': 'copy_uncarve', 'window': 'win-2fbb386c3b235ff4', 'object_id': 'a7e9087b1dbb0705', 'index': 0,
     'ring': 'ede5419aab577b43', 'why': 'adds a Z-shaped wedge'},
    {'v1': 44, 'type': 'copy_uncarve', 'window': 'win-caec3328e2a28dd0', 'object_id': '2a264151c382d10b', 'index': 0,
     'ring': 'e8869b5b82077864', 'why': 'adds a zig-zag slit at the tip'},
    {'v1': 105, 'type': 'body_carry', 'window': 'win-445423ab39e6ad49', 'object_id': '8501365c9d388031', 'index': 0,
     'holder': ('win-17b5c9b30a5d3ff9', '1acd4674e474d848'), 'why': 'jagged bump over the wall inside the frame (IoU 0.879, +2.4 m2)'},
    {'v1': 106, 'type': 'body_carry', 'window': 'win-445423ab39e6ad49', 'object_id': '92ad2a1cfe89385c', 'index': 0,
     'holder': ('win-17b5c9b30a5d3ff9', 'a5909e85ad18d9fd'), 'why': 'jagged shape round a rooftop unit inside the frame (IoU 0.845, +5.2 m2)'},
)
TYPES = ('canopy_completion', 'copy_uncarve', 'spike_fill', 'spike_tab_cut', 'body_carry')
RENDER_LONG_SIDE = 560
COLOURS = {'before': (255, 230, 0), 'after': (0, 235, 255), 'completion': (255, 0, 255), 'canopy': (40, 255, 80),
           'frame': (200, 200, 200), 'text': (255, 255, 255), 'box': (0, 0, 0), 'body_out': (0, 160, 255)}


# --- io ---------------------------------------------------------------------------------------------------------------

def heartbeat(message, run=RUN):
    run.mkdir(parents=True, exist_ok=True)
    with open(run/'progress.txt', 'a') as handle:
        handle.write(f'{datetime.now().strftime("%H:%M:%S")} {message}\n')


def r(value, digits=3):
    return None if value is None else round(float(value), digits)


def rel(path):
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


# --- small geometry helpers -------------------------------------------------------------------------------------------

def largest(geometry):
    """The largest polygon of a geometry (or None)."""
    geometry = polygonal(geometry) if geometry is not None else None
    if geometry is None:
        return None
    if geometry.geom_type == 'Polygon':
        return geometry
    parts = [g for g in geometry.geoms if g.geom_type == 'Polygon']
    return max(parts, key=lambda g: g.area) if parts else None


def area_parts(geometry):
    g = polygonal(geometry) if geometry is not None and not geometry.is_empty else None
    return 0. if g is None else float(g.area)


def polyline_samples(pts, step=.5):
    """Midpoints, normals and lengths of equal sub-pieces of an OPEN polyline."""
    pts = np.asarray(pts, dtype=float)
    if len(pts) < 2:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    seg = pts[1:]-pts[:-1]
    length = np.hypot(seg[:, 0], seg[:, 1])
    count = np.maximum(1, np.ceil(length/step).astype(int))
    edge = np.repeat(np.arange(len(seg)), count)
    offset = np.concatenate([(np.arange(c)+.5)/c for c in count])
    points = pts[:-1][edge]+offset[:, None]*seg[edge]
    safe = np.maximum(length, 1e-12)
    normal = np.stack([-seg[:, 1]/safe, seg[:, 0]/safe], axis=1)[edge]
    return points, normal, (length/count)[edge]


def finish_ring(ring, holes, width, height, decimals=DESIGN['ring_decimals']):
    """A proposed pixel ring made ready for the app: make_valid to ONE polygon, rounding, finalise_app_ring with the app's capped
    rules and the image size. Returns (ring, holes, polygon, issues, repairs); issues non-empty = refused."""
    try:
        polygon = Polygon(ring, [h for h in holes or [] if len(h) >= 3])
    except (ValueError, TypeError):
        return None, None, None, ['shapely: not a polygon'], None
    if not polygon.is_valid:
        fixed = make_valid(polygon)
        if fixed.geom_type != 'Polygon':
            return None, None, None, ['make_valid: not one polygon'], None
        polygon = fixed
    if polygon.is_empty or polygon.area <= 0:
        return None, None, None, ['empty'], None
    out_ring = [[round(float(x), decimals), round(float(y), decimals)] for x, y in polygon.exterior.coords[:-1]]
    out_holes = [[[round(float(x), decimals), round(float(y), decimals)] for x, y in h.coords[:-1]] for h in polygon.interiors]
    out_ring, out_holes, issues, repairs = finalise_app_ring(out_ring, out_holes, width, height, rules=APP_RING_RULES,
                                                             drop_collapsed_holes=True)
    if issues:
        return out_ring, out_holes, None, issues, repairs
    final = Polygon(out_ring, out_holes)
    if not final.is_valid or final.is_empty:
        return out_ring, out_holes, None, ['shapely: invalid after rounding'], repairs
    return out_ring, out_holes, final, [], repairs


def material_match(image, region, reference, rule=DESIGN['material']):
    """roof_surface.refine_roof_surface's same-material rule on two pixel regions; None when either has < 8 pixels."""
    inside = _pixels(region, image) if region is not None and not region.is_empty else np.zeros((0, 3))
    outside = _pixels(reference, image) if reference is not None and not reference.is_empty else np.zeros((0, 3))
    if len(inside) < 8 or len(outside) < 8:
        return None, {'pixels': [int(len(inside)), int(len(outside))]}
    a, b = np.median(inside, axis=0), np.median(outside, axis=0)
    chroma = float(np.linalg.norm((a-a.mean())-(b-b.mean())))

    def green(rgb):
        return float((rgb[1]-(rgb[0]+rgb[2])/2)/max(30., rgb.mean()))
    bright = float(abs(a.mean()-b.mean()))
    gdiff = abs(green(a)-green(b))
    ok = chroma <= rule['chroma'] and bright <= rule['brightness'] and gdiff <= rule['green']
    return bool(ok), {'chroma': r(chroma, 2), 'brightness': r(bright, 2), 'green': r(gdiff, 3), 'pixels': [int(len(inside)), int(len(outside))]}


# --- window context -----------------------------------------------------------------------------------------------------

def window_context(path):
    latest = load_json(path/'latest.json')
    review = load_json(path/'review.json') if (path/'review.json').exists() else None
    image = Image.open(path/f'{latest["image_sha256"]}.png').convert('RGB')
    return context_from(latest, review, image, path)


def context_from(latest, review, image, path):
    """The per-window context every family reads (review layer when present); built in memory by the tests."""
    layer = 'review' if review else 'model'
    objects = (review or latest).get('objects', [])
    width, height = int(latest['image_width']), int(latest['image_height'])
    s = spacing(latest['pixel_to_meters'])
    array = np.asarray(image, dtype=np.float32)
    channels = [np.ascontiguousarray(array[..., c]) for c in range(3)]
    live = [o for o in objects if o.get('status') not in LIVE_EXCLUDED and len(o.get('ring') or []) >= 3]
    polys = {}
    for o in live:
        p = object_polygon(o)
        if p is not None:
            polys[o['id']] = p
    model_by_id = {o['id']: o for o in latest.get('objects', [])}
    return {'window': Path(path).name, 'path': Path(path), 'latest': latest, 'review': review, 'layer': layer, 'W': width, 'H': height, 's': s,
            'image': image, 'channels': channels, 'live': live, 'polys': polys, 'by_id': {o['id']: o for o in live},
            'model_by_id': model_by_id, 'affine': np.asarray(latest['pixel_to_meters'], dtype=float)}


def canopy_of(ctx, exclude=()):
    trees = [(o['id'], ctx['polys'][o['id']]) for o in ctx['live'] if o['kind'] == 'tree' and o['id'] in ctx['polys'] and o['id'] not in exclude]
    return trees


def decided_info(obj, ctx, design=DESIGN):
    model = ctx['model_by_id'].get(obj['id'])
    edited = bool(ctx['layer'] == 'review' and model is not None
                  and (obj.get('ring') != model.get('ring') or (obj.get('holes') or []) != (model.get('holes') or [])))
    reasons = []
    if obj.get('status') in design['decided_statuses']:
        reasons.append(f"status_{obj['status']}")
    if obj.get('geometry_reviewed'):
        reasons.append('geometry_reviewed')
    if edited:
        reasons.append('reviewer_edited_ring')
    if isinstance(obj.get('carried_from'), dict):
        reasons.append('carried_copy')
    return {'decided': bool(reasons), 'reasons': reasons, 'edited_vs_model': edited}


def blocker_overlap(region, obj, ctx, design=DESIGN, exclude=(), kinds=None):
    """Live objects of the blocker kinds (other than obj and `exclude`) that the region covers by more than the guard.
    kinds None = design['blockers'][obj kind]; 'any' = every kind except design['spike_blocker_skip'][obj kind]."""
    if region is None or region.is_empty:
        return []
    s2 = ctx['s']**2
    skip = design['spike_blocker_skip'].get(obj['kind'], ()) if kinds == 'any' else ()
    kinds = design['blockers'].get(obj['kind'], ()) if kinds is None else kinds
    out = []
    total = region.area
    for o in ctx['live']:
        if o['id'] == obj['id'] or o['id'] in exclude or o['kind'] in skip or (kinds != 'any' and o['kind'] not in kinds):
            continue
        p = ctx['polys'].get(o['id'])
        if p is None or not p.intersects(region):
            continue
        a = area_parts(p.intersection(region))
        if a*s2 > design['blocker_m2'] and a > design['blocker_share']*total:
            out.append({'id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': r(a*s2, 3)})
    return out


def frame_length_m(ring, width, height, s):
    pts = clean_ring(ring)
    if len(pts) < 3:
        return 0.
    points, _, lengths, _ = ring_samples(pts)
    return float(lengths[frame_distance(points, width, height) <= FRAME_TOL_PX].sum()*s)


# --- lineage --------------------------------------------------------------------------------------------------------------

def proposal_id(kind, window, object_id, index, version=VERSION):
    return hashlib.sha256(f'{version}:{kind}:{window}:{object_id}:{index}'.encode()).hexdigest()[:16]


def ring_sha(ring, holes=None):
    """First 16 hex of sha256 over json [ring, holes] (compact): the identity SKEPTIC_WRONG_V1 holds a ring by."""
    return hashlib.sha256(json.dumps([ring, holes or []], separators=(',', ':')).encode()).hexdigest()[:16]


def skeptic_hold(ptype, window, object_id, index, ring=None, holes=None, holder=None, design=DESIGN):
    """The SKEPTIC_WRONG_V1 entry this line repeats (same key and same ring; body carry: same holder body), else None.
    Only when design['hold_skeptic_wrong'] (v2)."""
    if not design.get('hold_skeptic_wrong'):
        return None
    for entry in SKEPTIC_WRONG_V1:
        if (entry['type'], entry['window'], entry['object_id'], entry['index']) != (ptype, window, object_id, index):
            continue
        if 'holder' in entry:
            if holder is not None and tuple(holder) == tuple(entry['holder']):
                return entry
        elif ring is not None and ring_sha(ring, holes) == entry['ring']:
            return entry
    return None


def proposal_object(obj, ctx, ring, holes, ptype, index, extra=None, design=DESIGN):
    info = decided_info(obj, ctx, design)
    model = _ring_details([tuple(p) for p in ring], holes, ctx['affine'], obj['kind'])
    if (obj.get('model') or {}).get('color'):
        model['color'] = obj['model']['color']
    version = design.get('version', VERSION)
    out = {'id': proposal_id(ptype, ctx['window'], obj['id'], index, version), 'kind': obj['kind'], 'label': obj.get('label'),
           'ring': ring, 'holes': holes, 'status': 'proposal', 'source': version, 'geometry_reviewed': False,
           'score': obj.get('score'), 'model': model,
           'replaces': [] if info['decided'] else [obj['id']], 'replaces_on_accept': [obj['id']] if info['decided'] else [],
           'derived_from': [obj['id']], 'cut_fix': {'type': ptype, 'index': index}}
    if extra:
        out.update(extra)
    return out, info


# --- canopy completion (straight-edge) ------------------------------------------------------------------------------------

def edge_profile(R, s, width, height, canopy, canopy_b, channels, design=DESIGN):
    """Per edge of a closed ring R (n x 2 px): length_m, canopy_share, visible_share, frame_share, step (median RGB step)."""
    pts, normals, lengths, edge = ring_samples(R)
    near_px = max(design['canopy_near_px'], design['canopy_near_m']/s)
    if canopy is not None and not canopy.is_empty:
        dist = shapely.distance(shapely.points(pts), canopy)
        near = dist <= near_px
        covered = shapely.intersects_xy(canopy_b, pts[:, 0], pts[:, 1])
    else:
        near = np.zeros(len(pts), dtype=bool)
        covered = np.zeros(len(pts), dtype=bool)
    framed = frame_distance(pts, width, height) <= FRAME_TOL_PX
    steps = rgb_step(channels, pts, normals) if channels is not None else np.full(len(pts), 255.)
    out = []
    for i in range(len(R)):
        m = edge == i
        total = float(lengths[m].sum()) or 1e-12
        out.append({'length_m': total*s, 'canopy_share': float(lengths[m & near].sum())/total,
                    'visible_share': float(lengths[m & ~covered].sum())/total, 'frame_share': float(lengths[m & framed].sum())/total,
                    'step': float(np.median(steps[m])) if m.any() else 0.})
    return out


def canopy_runs(prof, design=DESIGN):
    """Maximal cyclic runs of canopy edges (not on the frame); [] when none or every edge is a canopy edge."""
    n = len(prof)
    flag = [p['canopy_share'] >= design['canopy_edge_share'] and p['frame_share'] < .5 for p in prof]
    if not any(flag) or all(flag):
        return []
    start = flag.index(False)
    runs, current = [], []
    for t in range(n):
        i = (start+t) % n
        if flag[i]:
            current.append(i)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def arm_reasons(p, design=DESIGN):
    out = []
    if p['frame_share'] >= .5:
        out.append('arm_on_frame')
    if p['length_m'] < design['arm_min_m']:
        out.append('arm_short')
    if p['visible_share'] < design['arm_visible_share']:
        out.append('arm_under_canopy')
    if p['step'] < design['arm_step_rgb']:
        out.append('arm_not_a_visible_edge')
    return out


def eave_completion(a0, a1, b0, b1, s, design=DESIGN):
    """('corner', point) | ('straight', None) | (None, reason) for arm a = a0 -> a1 (ends at the cut) and arm b = b0 -> b1."""
    da, db = np.asarray(a1, float)-a0, np.asarray(b1, float)-b0
    la, lb = float(np.hypot(*da)), float(np.hypot(*db))
    if la < 1e-9 or lb < 1e-9:
        return None, 'degenerate_arm'
    ua, ub = da/la, db/lb
    direction = math.degrees(math.acos(max(-1., min(1., float(ua @ ub)))))
    if direction <= design['straight_angle_deg']:
        off_b = abs(float(ua[0]*(b0[1]-a1[1])-ua[1]*(b0[0]-a1[0])))*s
        off_a = abs(float(ub[0]*(a1[1]-b0[1])-ub[1]*(a1[0]-b0[0])))*s
        if max(off_a, off_b) <= design['straight_offset_m']:
            return 'straight', None
        return None, 'parallel_offset_eaves'
    interior = math.degrees(math.acos(max(-1., min(1., float((-ua) @ ub)))))
    lo, hi = design['corner_angle_deg']
    if not lo <= interior <= hi:
        return None, 'corner_angle'
    try:
        t, u = np.linalg.solve(np.array([da, -db]).T, np.asarray(b0, float)-a0)
    except np.linalg.LinAlgError:
        return None, 'corner_angle'
    if t <= 1 or u >= 0:
        return None, 'arms_diverge'
    if max((t-1)*la*s, -u*lb*s) > design['arm_extension_max_m']:
        return None, 'extension_too_long'
    return 'corner', np.asarray(a0, float)+t*da


def stored_index(P, v):
    d = np.hypot(P[:, 0]-v[0], P[:, 1]-v[1])
    i = int(np.argmin(d))
    return i if d[i] < 1e-6 else None


def precarve_candidates(obj, ctx):
    """Stored geometry of the same object id that predates the outline shown: the model ring (when the review ring differs),
    every models/*.json snapshot ring of the same id, and boundary_refinement.raw_ring of both layers."""
    out = []
    model = ctx['model_by_id'].get(obj['id'])
    if model is not None and model.get('ring') != obj.get('ring'):
        out.append(('model_ring', model.get('ring')))
    for source in (obj, model):
        raw = ((source or {}).get('boundary_refinement') or {}).get('raw_ring')
        if raw:
            out.append(('raw_ring', raw))
    folder = ctx['path']/'models'
    for snap in sorted(folder.glob('*.json')) if folder.exists() else []:
        try:
            data = load_json(snap)
        except (OSError, ValueError):
            continue
        for o in data.get('objects', []) if isinstance(data, dict) else []:
            if o.get('id') == obj['id'] and o.get('ring') and o.get('ring') != obj.get('ring'):
                out.append((f'models/{snap.name[:12]}', o['ring']))
    return out


def canopy_completions(obj, ctx, design=DESIGN):
    """Every canopy cut of one building: [{run record, proposal fields or refusal}]."""
    polygon = ctx['polys'].get(obj['id'])
    if polygon is None or polygon.geom_type != 'Polygon':
        return []
    s, W, H = ctx['s'], ctx['W'], ctx['H']
    trees = [(tid, tp) for tid, tp in canopy_of(ctx) if tp.distance(polygon) <= design['hidden_max_m']/s+1]
    if not trees:
        return []
    canopy = unary_union([tp for _, tp in trees])
    canopy_b = canopy.buffer(design['canopy_buffer_m']/s)
    P = clean_ring(obj['ring'])
    simple = polygon.exterior.simplify(design['simplify_m']/s)
    R = np.asarray(simple.coords)[:-1]
    if len(R) < 4:
        return []
    prof = edge_profile(R, s, W, H, canopy, canopy_b, ctx['channels'], design)
    out = []
    for index, run in enumerate(canopy_runs(prof, design)):
        n = len(R)
        ka, kb = (run[0]-1) % n, (run[-1]+1) % n
        a0, a1, b0, b1 = R[ka], R[(ka+1) % n], R[kb], R[(kb+1) % n]
        run_pts = [R[(run[0]+t) % n] for t in range(len(run)+1)]
        run_line = LineString(run_pts)
        partners = sorted(tid for tid, tp in trees if tp.distance(run_line) <= max(design['canopy_near_px'], design['canopy_near_m']/s))
        rec = {'run_index': index, 'run_edges': len(run), 'run_m': r(sum(prof[i]['length_m'] for i in run)),
               'at_px': [r(float(np.mean([p[0] for p in run_pts])), 1), r(float(np.mean([p[1] for p in run_pts])), 1)],
               'run_px': [[r(p[0], 2), r(p[1], 2)] for p in run_pts], 'partners': partners,
               'arm_a': {k: r(v) for k, v in prof[ka].items()}, 'arm_b': {k: r(v) for k, v in prof[kb].items()},
               'refusals': [], 'geometry': None}
        if ka == kb:
            rec['refusals'].append('one_arm')
            out.append(rec)
            continue
        reasons = [f'a:{x}' for x in arm_reasons(prof[ka], design)]+[f'b:{x}' for x in arm_reasons(prof[kb], design)]
        shape, corner = eave_completion(a0, a1, b0, b1, s, design)
        if shape is None:
            reasons.append(corner)
        rec['geometry'] = shape
        ia, ib = stored_index(P, a1), stored_index(P, b0)
        if ia is None or ib is None:
            reasons.append('ring_not_stored_order')
        if reasons:
            rec['refusals'] = reasons
            out.append(rec)
            continue
        kept = [P[(ib+t) % len(P)] for t in range((ia-ib) % len(P)+1)]
        new_ring = [list(map(float, p)) for p in kept]
        if shape == 'corner':
            new_ring.append([float(corner[0]), float(corner[1])])
            rec['corner_px'] = [r(corner[0], 2), r(corner[1], 2)]
        holes = [list(map(list, h)) for h in (obj.get('holes') or []) if len(h) >= 3]
        occluder = unary_union([tp for tid, tp in trees if tid in partners]) if partners else None
        rec.update(evaluate_completion(obj, ctx, polygon, new_ring, holes, canopy, canopy_b, run_pts, corner, design,
                                       occluder=occluder))
        out.append(rec)
    return out


def max_distance(region, reference, step_px=.5):
    """Largest distance (px) from a point of `region` (boundary sampled every step_px) to `reference`; 0 when empty."""
    region = polygonal(region) if region is not None and not region.is_empty else None
    if region is None:
        return 0.
    parts = [region] if region.geom_type == 'Polygon' else [g for g in region.geoms if g.geom_type == 'Polygon']
    pts = np.concatenate([np.asarray(g.exterior.segmentize(step_px).coords) for g in parts])
    return float(shapely.distance(shapely.points(pts), reference).max())


def footprint_check(polygon, completion, corner, occluder, s, rule, reference=None):
    """v2 canopy footprint (DESIGN_V2['canopy_footprint']): (refusals, metrics). The completed corner must lie inside the
    occluding canopy buffered corner_buffer_m, and the completion's depth may not exceed the depth the canopy (buffered
    depth_buffer_m) reaches inside the completion (+ depth_tol_m for the boundary sampling). Depth = distance from `reference`:
    the chord between the cut run's two ends (the chop line of a corner, the eave of a straight join); default the outline."""
    refusals, metrics = [], {}
    if occluder is None or occluder.is_empty:
        return ['no_occluding_canopy'], metrics
    if corner is not None:
        point = Point(float(corner[0]), float(corner[1]))
        inside = occluder.covers(point)
        metrics['corner_in_canopy_m'] = r((1 if inside else -1)*point.distance(occluder.boundary)*s)
        if not occluder.buffer(rule['corner_buffer_m']/s).covers(point):
            refusals.append('eaves_meet_outside_canopy')
    reference = polygon if reference is None else reference
    depth = max_distance(completion, reference, rule['sample_px'])*s
    covered = polygonal(completion.intersection(occluder.buffer(rule['depth_buffer_m']/s))) if completion is not None else None
    canopy_depth = max_distance(covered, reference, rule['sample_px'])*s
    metrics.update(completion_depth_m=r(depth), canopy_depth_m=r(canopy_depth))
    if depth > canopy_depth+rule['depth_tol_m']:
        refusals.append('completion_deeper_than_canopy')
    return refusals, metrics


def evaluate_completion(obj, ctx, polygon, new_ring, holes, canopy, canopy_b, run_pts, corner, design=DESIGN, occluder=None):
    """Guards on one straight-edge completion. Returns {'refusals': [...], metrics, and when accepted 'ring', 'holes'}.
    `occluder` = the run's partner canopy (read by the v2 footprint guard only)."""
    s, W, H = ctx['s'], ctx['W'], ctx['H']
    s2 = s*s
    refusals = []
    ring, rholes, final, issues, repairs = finish_ring(new_ring, holes, W, H, design['ring_decimals'])
    if issues:
        return {'refusals': ['app_ring_rules' if final is None and ring is not None else 'invalid'], 'app_issues': issues}
    completion = polygonal(final.difference(polygon))
    lost = area_parts(polygon.difference(final))
    added = area_parts(completion)
    metrics = {'completion_m2': r(added*s2), 'lost_m2': r(lost*s2), 'object_m2': r(polygon.area*s2, 2),
               'hidden_depth_m': r(final.boundary.hausdorff_distance(polygon.boundary)*s), 'vertices_before': len(obj['ring']),
               'vertices_after': len(ring), 'app_repairs': repairs}
    if corner is not None:
        margin = min(corner[0], corner[1], W-corner[0], H-corner[1])
        metrics['corner_frame_margin_px'] = r(margin, 2)
        if margin < design['frame_margin_px']:
            refusals.append('leaves_window')
    if not box(0, 0, W, H).covers(final):
        refusals.append('leaves_window')
    if added*s2 < design['completion_min_m2']:
        refusals.append('trivial')
    if added*s2 > min(design['completion_max_m2'], design['completion_max_share']*polygon.area*s2):
        refusals.append('too_large')
    if metrics['hidden_depth_m'] > design['hidden_max_m']:
        refusals.append('hidden_too_deep')
    if lost*s2 > max(design['completion_min_m2'], .05*added*s2):
        refusals.append('removes_observed_roof')
    if completion is not None and added > 0:
        share = area_parts(completion.intersection(canopy_b))/added
        metrics['completion_canopy_share'] = r(share)
        if share < design['completion_canopy_share']:
            refusals.append('completion_not_under_canopy')
        visible = polygonal(completion.difference(canopy_b))
        vis_m2 = area_parts(visible)*s2
        metrics['visible_m2'] = r(vis_m2)
        if vis_m2 >= design['visible_check_m2']:
            near = polygonal(polygon.intersection(completion.buffer(1.5/s)).difference(canopy_b))
            if near is None or area_parts(near) < 8:
                near = polygonal(polygon.buffer(-.5/s))
            ok, detail = material_match(ctx['image'], visible, near, design['material'])
            metrics['visible_material'] = detail
            if ok is False:
                refusals.append('visible_part_not_roof')
        blockers = blocker_overlap(completion, obj, ctx, design)
        metrics['blockers'] = blockers
        if blockers:
            refusals.append('over_' + '_'.join(sorted({b['kind'] for b in blockers})))
    # A visible roof edge on the removed run: strong image step outside the canopy over >= visible_edge_m.
    pts, normals, lengths = polyline_samples(run_pts)
    if len(pts):
        steps = rgb_step(ctx['channels'], pts, normals)
        outside = ~shapely.intersects_xy(canopy_b, pts[:, 0], pts[:, 1])
        strong = float(lengths[outside & (steps >= design['arm_step_rgb'])].sum()*s)
        metrics['run_visible_strong_m'] = r(strong)
        if strong >= design['visible_edge_m']:
            refusals.append('visible_roof_edge_on_cut')
    rule = design.get('canopy_footprint')
    if rule and completion is not None and added > 0:
        chord = LineString([run_pts[0], run_pts[-1]]) if len(run_pts) >= 2 else None
        more, footprint = footprint_check(polygon, completion, corner, occluder, s, rule, chord)
        refusals += more
        metrics['canopy_footprint'] = footprint
    out = {'refusals': sorted(set(refusals)), **metrics}
    if not out['refusals']:
        out.update(ring=ring, holes=rholes)
    out['_completion'] = completion
    out['_final'] = final
    return out


def precarve_check(obj, ctx, completion):
    """Stored pre-carve geometry covering the completion (share of the completion area), best first."""
    if completion is None or completion.is_empty:
        return None
    best = None
    for label, ring in precarve_candidates(obj, ctx):
        try:
            p = Polygon(ring)
            p = p if p.is_valid else polygonal(make_valid(p))
        except (ValueError, TypeError):
            continue
        if p is None:
            continue
        cover = area_parts(p.intersection(completion))/completion.area
        if best is None or cover > best['cover']:
            best = {'source': label, 'cover': r(cover), '_ring': ring}
    return best


# --- frame spikes -----------------------------------------------------------------------------------------------------------

def notch_runs(pts, polygon, width, height, s):
    """cut_census.find_spikes' notch / tab test with the vertex indices kept: [(i, j, side, chord_m, depth_m, shape)]."""
    n = len(pts)
    sides = [sides_of(p, width, height) for p in pts]
    on = np.array([bool(x) for x in sides])
    q = np.flatnonzero(on)
    out = []
    if len(q) < 2 or polygon is None:
        return out
    limit_px = SPIKE_MAX_WIDTH_M/s
    for k in range(len(q)):
        i, j = int(q[k]), int(q[(k+1) % len(q)])
        gap = (j-i) % n
        if gap <= 1:
            continue
        common = sides[i] & sides[j]
        if not common:
            continue
        chord = float(np.hypot(*(pts[j]-pts[i])))
        if chord <= 1e-9 or chord >= limit_px:
            continue
        idx = [(i+t) % n for t in range(gap+1)]
        run = pts[idx]
        if shoelace(run) >= SPIKE_AREA_SHARE*polygon.area:
            continue
        side = sorted(common)[0]
        depth = float(side_depth(run, side, width, height).max())
        if depth < SPIKE_MIN_DEPTH_PX or depth < SPIKE_MIN_ASPECT*chord:
            continue
        try:
            probe = Polygon(run)
            probe = probe if probe.is_valid else polygonal(make_valid(probe))
            inside = probe is not None and polygon.contains(probe.representative_point())
        except (ValueError, shapely.errors.GEOSException):
            inside = False
        out.append({'i': i, 'j': j, 'side': side, 'width_m': r(chord*s), 'depth_m': r(depth*s), 'shape': 'tab' if inside else 'notch',
                    'at_px': [r(run[int(np.argmax(side_depth(run, side, width, height)))][0], 2),
                              r(run[int(np.argmax(side_depth(run, side, width, height)))][1], 2)]})
    return out


def spike_fixes(obj, ctx, design=DESIGN):
    polygon = ctx['polys'].get(obj['id'])
    if polygon is None or polygon.geom_type != 'Polygon':
        return [], []
    s, W, H = ctx['s'], ctx['W'], ctx['H']
    pts = clean_ring(obj['ring'])
    census_spikes, _ = find_spikes(pts, polygon, W, H, s)
    held = [{'shape': sp['shape'], 'side': sp['side'], 'width_m': sp['width_m'], 'depth_m': sp['depth_m'], 'at_px': sp['at_px'],
             'reason': 'protrusion_held'} for sp in census_spikes if sp['shape'] == 'protrusion']
    holes = [list(map(list, h)) for h in (obj.get('holes') or []) if len(h) >= 3]
    out = []
    for index, sp in enumerate(notch_runs(pts, polygon, W, H, s)):
        i, j, n = sp['i'], sp['j'], len(pts)
        drop = {(i+t) % n for t in range(1, (j-i) % n)}
        rec = {'spike_index': index, **{k: v for k, v in sp.items() if k not in ('i', 'j')}, 'refusals': []}
        if sp['shape'] != 'notch':
            # A tab encloses observed object area at the frame; by eye (2026-10-08, win-738c41fc d70b413e) the one tab found
            # was roof surface the outline traced poorly, so cutting it deletes observed roof: held, as protrusions are.
            rec.update(type='spike_tab_cut', refusals=['tab_held'])
            out.append(rec)
            continue
        new_ring = [list(map(float, p)) for t, p in enumerate(pts) if t not in drop]
        ring, rholes, final, issues, repairs = finish_ring(new_ring, holes, W, H)
        ptype = 'spike_fill'
        rec['type'] = ptype
        if issues:
            rec['refusals'] = ['app_ring_rules']
            rec['app_issues'] = issues
            out.append(rec)
            continue
        added = polygonal(final.difference(polygon))
        removed = polygonal(polygon.difference(final))
        rec.update(added_m2=r(area_parts(added)*s*s), removed_m2=r(area_parts(removed)*s*s), vertices_before=len(obj['ring']),
                   vertices_after=len(ring), app_repairs=repairs)
        # A notch another object explains (a shrub cut out of a lawn, a second crown) is a real boundary: any kind blocks,
        # except what sits ON a roof (solar panel, skylight). Trees and vegetation also need the fill to look like the
        # object (refine_roof_surface's material rule); a roof fill is exempt (1bac's slit is a bright roof fixture).
        blockers = blocker_overlap(added, obj, ctx, design, kinds='any')
        rec['blockers'] = blockers
        if blockers:
            rec['refusals'].append('over_' + '_'.join(sorted({b['kind'] for b in blockers})))
        if obj['kind'] != 'building' and added is not None and added.area > 0:
            near = polygonal(polygon.intersection(added.buffer(max(3., .5/s))))
            ok, detail = material_match(ctx['image'], added, near, design['material'])
            rec['fill_material'] = detail
            if ok is False:
                rec['refusals'].append('fill_not_same_material')
            if area_parts(added)*s*s > design['soft_notch_max_m2']:
                rec['refusals'].append('notch_too_large_for_artefact')
            if obj['kind'] == 'vegetation':
                # Lawn is low-texture: notch walls on a strong image step trace a real object (a shrub, a garden item).
                walls = pts[[(sp['i']+t) % n for t in range((sp['j']-sp['i']) % n+1)]]
                wp, wn, _ = polyline_samples(walls)
                step = float(np.median(rgb_step(ctx['channels'], wp, wn))) if len(wp) else 0.
                rec['wall_step_rgb'] = r(step, 1)
                if step >= design['arm_step_rgb']:
                    rec['refusals'].append('notch_on_visible_edge')
        if area_parts(removed)*s*s > .05:
            rec['refusals'].append('removes_observed_area')
        if not box(0, 0, W, H).covers(final):
            rec['refusals'].append('leaves_window')
        if not rec['refusals']:
            rec.update(ring=ring, holes=rholes)
        rec['_completion'] = added
        rec['_final'] = final
        out.append(rec)
    return out, held


# --- other frames: index, transforms ---------------------------------------------------------------------------------------

class FrameIndex:
    """Live building / tree outlines of every region-registered scene whose frame meets a window (Web-Mercator units). The
    review layer when a scene has one (rejected / superseded and carried copies skipped), else latest.json; qa_* skipped."""

    def __init__(self, window_frames, root=SCENES):
        area = unary_union(list(window_frames.values()))
        self.entries, self.scanned, self.scenes = [], 0, {}
        for path in sorted(root.glob('*/latest.json')):
            if path.parent.name.startswith(SCRATCH_PREFIX):
                continue
            scene = load_json(path)
            self.scanned += 1
            if not isinstance(scene, dict) or not (scene.get('region') or {}).get('bounds'):
                continue
            w, h = scene['image_width'], scene['image_height']
            origin = mercator_origin(scene)
            frame = Polygon(pixels_to_mercator([[0, 0], [w, 0], [w, h], [0, h]], scene, origin))
            if not frame.intersects(area):
                continue
            review_path = path.parent/'review.json'
            review = load_json(review_path) if review_path.exists() else None
            layer = 'review' if isinstance(review, dict) and review.get('objects') is not None else 'model'
            objects = review['objects'] if layer == 'review' else scene.get('objects', [])
            self.scenes[scene['id']] = {'m_per_px': spacing(scene['pixel_to_meters']), 'frame': frame, 'layer': layer,
                                        'model_signature': scene.get('model_signature'), 'window': scene['id'].startswith(WINDOW_PREFIXES)}
            for o in objects:
                if o.get('kind') not in ('building', 'tree') or o.get('status') in LIVE_EXCLUDED or len(o.get('ring') or []) < 3:
                    continue
                if isinstance(o.get('carried_from'), dict):
                    continue
                ring = np.asarray(o['ring'], dtype=float)
                try:
                    geo = Polygon(pixels_to_mercator(ring, scene, origin))
                except (ValueError, TypeError):
                    continue
                if not geo.is_valid:
                    geo = polygonal(make_valid(geo))
                if geo is None or geo.area <= 0:
                    continue
                self.entries.append({'scene_id': scene['id'], 'object_id': o['id'], 'kind': o['kind'], 'geo': geo,
                                     'touches_own_frame': bool((frame_distance(ring, w, h) <= FRAME_TOL_PX).any()),
                                     'status': o.get('status'), 'geometry_reviewed': bool(o.get('geometry_reviewed')),
                                     'ring': o['ring'], 'holes': o.get('holes') or [], 'vertices': len(o['ring'])})
            del scene
        self.tree = STRtree([e['geo'] for e in self.entries]) if self.entries else None

    def near(self, geo, kind, exclude_scene):
        if self.tree is None:
            return []
        out = []
        for k in self.tree.query(geo):
            e = self.entries[int(k)]
            if e['kind'] == kind and e['scene_id'] != exclude_scene:
                out.append(e)
        return out


class Measurer:
    """scene_recapture.measured_transform per (source scene, window), role frame_cut_target, cached (slice_body_census.Measurer)."""

    def __init__(self, design):
        self.design = design
        self.scenes, self.images, self.transforms = {}, {}, {}
        self.measured = 0

    def scene(self, scene_id):
        if scene_id not in self.scenes:
            self.scenes[scene_id] = sr._scene(ROOT, scene_id)
        return self.scenes[scene_id]

    def image_bytes(self, scene):
        if scene['id'] not in self.images:
            self.images[scene['id']] = sr._image(ROOT, scene)
        return self.images[scene['id']]

    def transform(self, source_id, target):
        key = (source_id, target['id'])
        if key not in self.transforms:
            try:
                source = self.scene(source_id)
                ft = sr.measured_transform(source, target, self.image_bytes(source), self.image_bytes(target), self.design,
                                           role=sr.SUBJECT_ROLE)
                ok, reasons = sr.verdict(ft, self.design[0])
                self.transforms[key] = {'ft': ft, 'verified': ok, 'reasons': reasons, 'error': None}
            except Exception as error:  # noqa: BLE001 - recorded per candidate
                self.transforms[key] = {'ft': None, 'verified': False, 'reasons': [repr(error)], 'error': repr(error)}
            self.measured += 1
        return self.transforms[key]

    def release(self, keep=()):
        """Drop cached image bytes and scenes except `keep` (memory: one window at a time)."""
        self.images = {k: v for k, v in self.images.items() if k in keep}
        self.scenes = {k: v for k, v in self.scenes.items() if k in keep}


def transform_summary(measured):
    ft = measured.get('ft') or {}
    res = ft.get('residual') or {}
    return {'verified': measured['verified'], 'reasons': measured['reasons'], 'shift_m': res.get('shift_m'), 'ncc': res.get('ncc'),
            'overlap_side_px': ((ft.get('overlap') or {}).get('region_px') or {}).get('side_px'),
            'design_sha256': (ft.get('design') or {}).get('design_sha256')}


def project(entry, measured):
    ft = measured['ft']
    ring = sr.apply_affine([p[:2] for p in entry['ring']], ft['pixel_affine'])
    holes = [sr.apply_affine([p[:2] for p in h], ft['pixel_affine']) for h in entry.get('holes') or [] if len(h) >= 3]
    p = Polygon(ring, holes)
    if not p.is_valid:
        p = polygonal(make_valid(p))
    return p, ring


def object_geo(obj, ctx):
    return Polygon(pixels_to_mercator(clean_ring(obj['ring']), ctx['latest']))


# --- v2 spike / sliver guard (copy un-carve results) ------------------------------------------------------------------------

def densify(pts, step_px):
    """A closed ring's points with extra points every <= step_px along each edge (the vertices kept)."""
    out = []
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i+1) % n]
        k = max(1, int(math.ceil(float(np.hypot(*(b-a)))/step_px)))
        out.extend(a+(b-a)*t/k for t in range(k))
    return np.asarray(out, dtype=float)


def segment_distance(points, a, b):
    ab = b-a
    length2 = float(ab @ ab)
    if length2 < 1e-12:
        return np.hypot(*(points-a).T)
    t = np.clip(((points-a) @ ab)/length2, 0., 1.)
    return np.hypot(*(points-(a+t[:, None]*ab)).T)


def ring_excursions(ring, polygon, s, rules, step_px=1., max_arc_m=20.):
    """cut_census.find_spikes' notch / tab test with the window frame replaced by a chord between ANY two points of the ring
    (densified every step_px): the ring run between them is an excursion when the chord is shorter than a rule's
    chord_max_px, the run reaches max(min_depth_px, min_aspect x chord) or more from the chord (deeper than wide at
    min_aspect 1, the census protrusion rule; a 90-degree corner is not one) and the run encloses < SPIKE_AREA_SHARE of the
    object. rules = {name: (chord_max_px, min_depth_px, min_aspect)}. Returns [{rule, shape, chord_px, depth_px, tip}], the
    deepest per rule and tip point; shape 'protrusion' when the run's polygon lies inside the object, else 'notch'."""
    pts = clean_ring(ring)
    if len(pts) < 3 or polygon is None or polygon.is_empty:
        return []
    Q = densify(pts, step_px)
    n = len(Q)
    cum = np.r_[0., np.cumsum(np.hypot(*(np.roll(Q, -1, axis=0)-Q).T))]
    perimeter = cum[-1]
    chord_max = max(v[0] for v in rules.values())
    depth_floor = min(v[1] for v in rules.values())
    aspect_floor = min(v[2] for v in rules.values())
    found = {}
    for a, b in sorted(cKDTree(Q).query_pairs(chord_max)):
        chord = float(np.hypot(*(Q[b]-Q[a])))
        # a run that reaches depth d from a chord c is at least 2 hypot(d, c / 2) long (ellipse): cheap pre-filter
        shortest = 2*math.hypot(max(depth_floor, aspect_floor*chord), chord/2)-1e-9
        for i, j in ((a, b), (b, a)):
            arc = (cum[j]-cum[i]) % perimeter
            if arc < shortest or arc*s > max_arc_m:
                continue
            idx = (i+np.arange((j-i) % n+1)) % n
            run = Q[idx]
            dist = segment_distance(run, Q[i], Q[j])
            k = int(np.argmax(dist))
            depth = float(dist[k])
            hits = [name for name, (cmax, dmin, aspect) in rules.items() if chord < cmax and depth >= max(dmin, aspect*chord)]
            if not hits or shoelace(run) >= SPIKE_AREA_SHARE*polygon.area:
                continue
            tip = int(idx[k])
            for name in hits:
                if (name, tip) not in found or found[(name, tip)]['depth_px'] < depth:
                    found[(name, tip)] = {'rule': name, 'chord_px': chord, 'depth_px': depth,
                                          'tip': (float(Q[tip][0]), float(Q[tip][1])), '_run': run}
    out = []
    for rec in found.values():
        run = rec.pop('_run')
        try:
            probe = Polygon(run)
            probe = probe if probe.is_valid else polygonal(make_valid(probe))
            inside = probe is not None and polygon.contains(probe.representative_point())
        except (ValueError, shapely.errors.GEOSException):
            inside = False
        rec['shape'] = 'protrusion' if inside else 'notch'
        out.append(rec)
    return out


def sharp_vertices(ring, max_deg=25., arm_px=1.5, dedupe_px=.3):
    """The by-eye skeptic's slit scan (.run/session-2026-10-08-cuts-veg/eye-cuts/slit_scan.py): vertices (a point closer than
    dedupe_px to the last kept one dropped) whose angle is below max_deg with both arms >= arm_px. [(x, y, angle_deg)]."""
    q = []
    for v in clean_ring(ring):
        if not q or math.dist(q[-1], v) > dedupe_px:
            q.append((float(v[0]), float(v[1])))
    out = []
    for i in range(len(q)):
        a, b, c = q[i-1], q[i], q[(i+1) % len(q)]
        u, w = (a[0]-b[0], a[1]-b[1]), (c[0]-b[0], c[1]-b[1])
        lu, lw = math.hypot(*u), math.hypot(*w)
        if lu < arm_px or lw < arm_px:
            continue
        angle = math.degrees(math.acos(max(-1., min(1., (u[0]*w[0]+u[1]*w[1])/(lu*lw)))))
        if angle < max_deg:
            out.append((b[0], b[1], angle))
    return out


def spike_guard(before, before_ring, after, after_ring, width, height, s, rule):
    """v2 copy un-carve guard (DESIGN_V2['copy_spike_guard']): excursions the RESULT ring has and the current ring does not.
    Three tests: cut_census.find_spikes on both rings (frame notch / tab / protrusion narrower than notch_max_m);
    ring_excursions (the same notch test on any chord: narrower than notch_max_m from min_depth_px deep, a part narrower than
    part_max_m from part_min_depth_px deep); sharp_vertices (the skeptic's < sharp_deg scan). A feature is NEW when it lies
    within change_px of the change (result xor current) and no current-ring feature of the same test (and shape) lies within
    new_tol_px. Returns the new features ([] = clean)."""
    change = polygonal(after.symmetric_difference(before))
    if change is None:
        return []
    near = change.buffer(rule['change_px'])
    tol = rule['new_tol_px']
    features = []
    old, _ = find_spikes(clean_ring(before_ring), before, width, height, s)
    new, _ = find_spikes(clean_ring(after_ring), after, width, height, s)
    for sp in new:
        if sp['shape'] == 'shallow_dent' or sp['width_m'] >= rule['notch_max_m']:
            continue
        if any(o['shape'] == sp['shape'] and math.dist(o['at_px'], sp['at_px']) <= tol for o in old):
            continue
        if near.distance(Point(*sp['at_px'])) > max(tol, rule['notch_max_m']/s):
            continue
        features.append({'test': 'census_frame_spike', 'shape': sp['shape'], 'side': sp['side'], 'width_m': sp['width_m'],
                         'depth_m': sp['depth_m'], 'at_px': sp['at_px']})
    rules = {'narrower_than_1m': (rule['notch_max_m']/s, rule['min_depth_px'], rule['min_aspect']),
             'part_narrower_than_0.5m': (rule['part_max_m']/s, rule['part_min_depth_px'], rule['min_aspect'])}
    old = ring_excursions(before_ring, before, s, rules, rule['step_px'], rule['max_arc_m'])
    by_tip = {}
    for e in sorted(ring_excursions(after_ring, after, s, rules, rule['step_px'], rule['max_arc_m']), key=lambda e: e['rule']):
        if not near.covers(Point(*e['tip'])):
            continue
        if any(o['rule'] == e['rule'] and o['shape'] == e['shape'] and math.dist(o['tip'], e['tip']) <= tol for o in old):
            continue
        key = (e['shape'], round(e['tip'][0], 2), round(e['tip'][1], 2))
        if key in by_tip:                     # one excursion caught by both widths: one feature, both tests listed
            by_tip[key]['tests'].append(e['rule'])
            continue
        by_tip[key] = {'test': e['rule'], 'tests': [e['rule']], 'shape': e['shape'], 'width_m': r(e['chord_px']*s),
                       'depth_m': r(e['depth_px']*s), 'at_px': [r(e['tip'][0], 2), r(e['tip'][1], 2)]}
    features += list(by_tip.values())
    old = sharp_vertices(before_ring, rule['sharp_deg'], rule['sharp_arm_px'], rule['sharp_dedupe_px'])
    for v in sharp_vertices(after_ring, rule['sharp_deg'], rule['sharp_arm_px'], rule['sharp_dedupe_px']):
        if not near.covers(Point(v[0], v[1])) or any(math.dist(v[:2], o[:2]) <= tol for o in old):
            continue
        features.append({'test': 'sharp_vertex', 'angle_deg': r(v[2], 1), 'at_px': [r(v[0], 2), r(v[1], 2)]})
    features.sort(key=lambda f: (f['test'], f['at_px']))
    return features


# --- copies (un-carve from another frame) -----------------------------------------------------------------------------------

def copy_candidates(obj, polygon, ctx, index, measurer, design=DESIGN, limit=4):
    """Verified copies of the same object from other frames, projected into the window: [(entry, projected polygon, record)]."""
    geo = object_geo(obj, ctx)
    if not geo.is_valid:
        geo = polygonal(make_valid(geo))
    if geo is None or geo.area <= 0:
        return []
    frame_box = box(0, 0, ctx['W'], ctx['H'])
    found = []
    for e in index.near(geo, obj['kind'], ctx['window']):
        inter = e['geo'].intersection(geo).area
        if inter <= 0:
            continue
        geo_iou = inter/e['geo'].union(geo).area
        if geo_iou < design['copy_iou_min']*.6:
            continue
        found.append((geo_iou, e))
    found.sort(key=lambda x: (-x[0], index.scenes[x[1]['scene_id']]['m_per_px'], x[1]['scene_id'], x[1]['object_id']))
    out = []
    for geo_iou, e in found[:limit]:
        measured = measurer.transform(e['scene_id'], ctx['latest'])
        rec = {'scene_id': e['scene_id'], 'object_id': e['object_id'], 'geo_iou': r(geo_iou), 'transform': transform_summary(measured),
               'source_m_per_px': r(index.scenes[e['scene_id']]['m_per_px'], 4),
               'resolution_ratio': r(index.scenes[e['scene_id']]['m_per_px']/ctx['s'], 3), 'source_status': e['status'],
               'source_geometry_reviewed': e['geometry_reviewed'], 'touches_own_frame': e['touches_own_frame']}
        if not measured['verified']:
            out.append((e, None, rec))
            continue
        proj, _ = project(e, measured)
        if proj is None:
            out.append((e, None, rec))
            continue
        inside = proj.intersection(frame_box)
        union = area_parts(inside.union(polygon))
        rec['iou_in_window'] = r(area_parts(inside.intersection(polygon))/union) if union else 0.
        out.append((e, proj, rec))
    return out


def copy_uncarve(obj, polygon, ctx, zone, copies, partners, design=DESIGN, index=0):
    """The part of a verified same-object copy inside the hidden zone, added to the object. Returns a record."""
    s, W, H = ctx['s'], ctx['W'], ctx['H']
    best = None
    coarse = 0
    for e, proj, rec in copies:
        if proj is None or (rec.get('iou_in_window') or 0) < design['copy_iou_min']:
            continue
        if rec['resolution_ratio'] > design.get('copy_resolution_max', design['body_resolution_max']):
            coarse += 1          # a coarser copy's edge is blur, not evidence of a hidden outline (census: coarse bodies)
            continue
        part = polygonal(proj.intersection(zone))
        if part is None:
            continue
        part = polygonal(part.buffer(-design['copy_open_px'], join_style='mitre').buffer(design['copy_open_px'], join_style='mitre'))
        part = polygonal(part.simplify(design['copy_simplify_px'], preserve_topology=True)) if part is not None else None
        a = area_parts(part)*s*s
        key = (a, -rec['resolution_ratio'])
        if best is None or key > best[0]:
            best = (key, e, proj, rec, part)
    if best is None:
        why = 'only_coarser_copies' if coarse else 'no_verified_copy_reaching_the_zone'
        return {'refusals': [why], 'copies': [c[2] for c in copies]}
    _, e, proj, rec, part = best
    out = {'copy': rec, 'copies': [c[2] for c in copies], 'copy_part_m2': r(area_parts(part)*s*s), 'refusals': []}
    if area_parts(part)*s*s < design['copy_min_m2']:
        out['refusals'].append('copy_adds_too_little')
        return out
    # A 0.5 px closing seals slivers between the copy's part and the outline; union slivers under 0.25 m2 become no holes.
    merged = largest(polygon.union(part).buffer(.5, join_style='mitre').buffer(-.5, join_style='mitre'))
    if merged is None or not merged.contains(polygon.representative_point()):
        out['refusals'].append('invalid')
        return out
    merged = Polygon(merged.exterior, [h for h in merged.interiors if Polygon(h).area*s*s >= .25])
    ring, rholes, final, issues, repairs = finish_ring([list(p) for p in merged.exterior.coords[:-1]],
                                                      [list(map(list, h.coords[:-1])) for h in merged.interiors], W, H)
    if issues:
        out['refusals'].append('app_ring_rules')
        out['app_issues'] = issues
        return out
    added = polygonal(final.difference(polygon))
    added_m2, removed_m2 = area_parts(added)*s*s, area_parts(polygon.difference(final))*s*s
    out.update(completion_m2=r(added_m2), removed_m2=r(removed_m2),
               vertices_before=len(obj['ring']), vertices_after=len(ring), app_repairs=repairs, partners=partners)
    if added_m2 < design['copy_min_m2']:
        out['refusals'].append('copy_adds_too_little')
    if removed_m2 > max(design['completion_min_m2'], .05*added_m2):
        out['refusals'].append('removes_observed_area')
    guard = design.get('copy_spike_guard')
    if guard:
        features = spike_guard(polygon, obj['ring'], final, ring, W, H, s, guard)
        out['spike_guard'] = {'count': len(features), 'by_test': dict(Counter(f['test'] for f in features)), 'features': features[:8]}
        if features:
            out['refusals'].append('adds_spike_or_sliver')
    if obj['kind'] == 'building' and added is not None and added.area > 0:
        partner = unary_union([ctx['polys'][p] for p in partners if p in ctx['polys']])
        share = area_parts(added.intersection(partner.buffer(design['canopy_buffer_m']/s)))/added.area if not partner.is_empty else 0.
        out['completion_canopy_share'] = r(share)
        if share < design['completion_canopy_share']:
            out['refusals'].append('completion_not_under_canopy')
    blockers = blocker_overlap(added, obj, ctx, design)
    out['blockers'] = blockers
    if blockers:
        out['refusals'].append('over_' + '_'.join(sorted({b['kind'] for b in blockers})))
    if not box(0, 0, W, H).covers(final):
        out['refusals'].append('leaves_window')
    if obj['kind'] == 'building' and added is not None and added.area > 0:
        canopy = unary_union([tp for _, tp in canopy_of(ctx)])
        canopy_b = canopy.buffer(design['canopy_buffer_m']/s) if not canopy.is_empty else canopy
        visible = polygonal(added.difference(canopy_b)) if not canopy.is_empty else added
        vis_m2 = area_parts(visible)*s*s
        out['visible_m2'] = r(vis_m2)
        if vis_m2 >= design['visible_check_m2']:
            near = polygonal(polygon.intersection(added.buffer(1.5/s)))
            ok, detail = material_match(ctx['image'], visible, near, design['material'])
            out['visible_material'] = detail
            if ok is False:
                out['refusals'].append('visible_part_not_roof')
    if not out['refusals']:
        out.update(ring=ring, holes=rholes)
    out['_completion'] = added
    out['_final'] = final
    out['_copy'] = proj
    return out


# --- body carry (frame-cut roofs) ------------------------------------------------------------------------------------------

def body_candidates(obj, polygon, ctx, index, design=DESIGN):
    """Georeferenced bodies: whole in their own frame, cover >= body_cover_listed of the window outline, reaching beyond it."""
    geo = object_geo(obj, ctx)
    if not geo.is_valid:
        geo = polygonal(make_valid(geo))
    if geo is None or geo.area <= 0:
        return []
    w, h = ctx['W'], ctx['H']
    frame = Polygon(pixels_to_mercator([[0, 0], [w, 0], [w, h], [0, h]], ctx['latest']))
    out = []
    for e in index.near(geo, 'building', ctx['window']):
        if e['touches_own_frame']:
            continue
        cover = e['geo'].intersection(geo).area/geo.area
        beyond = e['geo'].difference(frame).area/geo.area
        if cover < design['body_cover_listed'] or beyond < design['body_beyond_min']:
            continue
        m = index.scenes[e['scene_id']]['m_per_px']
        out.append({'entry': e, 'geo_cover': cover, 'beyond_share': beyond, 'resolution_ratio': m/ctx['s'], 'm_per_px': m,
                    'window': index.scenes[e['scene_id']]['window']})
    coarse = design['body_resolution_max']+design.get('body_resolution_tol', 0.)
    out.sort(key=lambda c: (c['geo_cover'] < design['body_cover_whole'], c['resolution_ratio'] > coarse,
                            -c['geo_cover'], c['resolution_ratio'], c['entry']['scene_id'], c['entry']['object_id']))
    return out


def body_carry(obj, polygon, ctx, index, measurer, design=DESIGN):
    cands = body_candidates(obj, polygon, ctx, index, design)
    listed = [{'scene_id': c['entry']['scene_id'], 'object_id': c['entry']['object_id'], 'geo_cover': r(c['geo_cover']),
               'beyond_share': r(c['beyond_share']), 'resolution_ratio': r(c['resolution_ratio']), 'window': c['window'],
               'status': c['entry']['status'], 'geometry_reviewed': c['entry']['geometry_reviewed']} for c in cands]
    rec = {'listed': listed, 'measured': [], 'refusals': []}
    if not cands:
        rec['refusals'].append('no_whole_body')
        return rec
    s, W, H = ctx['s'], ctx['W'], ctx['H']
    frame_box = box(0, 0, W, H)
    coarse = design['body_resolution_max']+design.get('body_resolution_tol', 0.)
    best = None
    for c in cands[:design['body_measure_top']]:
        e = c['entry']
        measured = measurer.transform(e['scene_id'], ctx['latest'])
        m = {'scene_id': e['scene_id'], 'object_id': e['object_id'], 'transform': transform_summary(measured),
             'resolution_ratio': r(c['resolution_ratio']), 'window': c['window'], 'source_status': e['status'],
             'source_geometry_reviewed': e['geometry_reviewed'], 'reasons': []}
        if not measured['verified']:
            m['reasons'].append('transform_not_verified')
            rec['measured'].append(m)
            continue
        proj, ring = project(e, measured)
        if proj is None or proj.area <= 0:
            m['reasons'].append('degenerate_body')
            rec['measured'].append(m)
            continue
        inside = largest(proj.intersection(frame_box))
        cover = area_parts(proj.intersection(polygon))/polygon.area
        iou = area_parts(inside.intersection(polygon))/area_parts(inside.union(polygon)) if inside is not None else 0.
        m.update(cover=r(cover), inframe_iou=r(iou), beyond_share=r(area_parts(proj.difference(frame_box))/polygon.area),
                 drops_m2=r(area_parts(polygon.difference(proj))*s*s, 2),
                 adds_in_frame_m2=r(area_parts(inside.difference(polygon))*s*s, 2) if inside is not None else None,
                 body_m2=r(proj.area*s*s, 2), body_vertices=len(ring))
        if cover < design['body_cover_whole']:
            m['reasons'].append('cover_below_whole')
        if iou < design['body_inframe_iou_min']:
            m['reasons'].append('inframe_iou_low')
        if c['resolution_ratio'] > coarse:
            m['reasons'].append('coarser_than_window')
        if m['drops_m2'] > max(design['body_drop_max_m2'], design['body_drop_max_share']*polygon.area*s*s):
            # By eye (win-c2a076d2 ddcaadc4, win-4cc07d69 b65019b4): a body that drops observed outline inside the frame
            # replaces a sharper in-frame trace with the holder's mask; the frame cut is not fixed by it either.
            m['reasons'].append('drops_observed_outline')
        hold = skeptic_hold('body_carry', ctx['window'], obj['id'], 0, holder=(e['scene_id'], e['object_id']), design=design)
        if hold is not None:
            m['reasons'].append('skeptic_marked_wrong_v1')
            m['skeptic_v1'] = hold['v1']
        if inside is not None:
            fr, fh, final, issues, repairs = finish_ring([list(p) for p in inside.exterior.coords[:-1]],
                                                         [list(map(list, h.coords[:-1])) for h in inside.interiors], W, H)
            if issues:
                m['reasons'].append('app_ring_rules')
                m['app_issues'] = issues
        else:
            m['reasons'].append('no_inframe_part')
            fr = fh = final = repairs = None
        rec['measured'].append(m)
        key = (not m['reasons'], cover >= design['body_cover_whole'], c['resolution_ratio'] <= coarse, iou, cover)
        if best is None or key > best[0]:
            best = (key, c, measured, proj, ring, fr, fh, final, repairs, m)
    if best is None:
        rec['refusals'].append('no_verified_body')
        return rec
    _, c, measured, proj, ring, fr, fh, final, repairs, m = best
    rec['best'] = m
    rec['refusals'] = list(m['reasons'])
    if not m['reasons']:
        e = c['entry']
        rec.update(ring=fr, holes=fh, whole_ring=[[r(x, 2), r(y, 2)] for x, y in ring], app_repairs=repairs,
                   carried_from={'scene_id': e['scene_id'], 'model_signature': index.scenes[e['scene_id']]['model_signature'],
                                 'object_id': e['object_id'], 'status': e['status'], 'geometry_reviewed': e['geometry_reviewed'],
                                 'iou_with_new': m['inframe_iou'], 'matched_id': obj['id'], 'method': sr.METHOD, 'role': 'whole_roof_body',
                                 'transform': {'pixel_affine': measured['ft']['pixel_affine'], 'metre_offset': measured['ft']['metre_offset'],
                                               **transform_summary(measured)}})
        rec['_final'] = final
        rec['_whole'] = proj
        rec['_completion'] = polygonal(proj.difference(polygon))
        rec['_source'] = e['scene_id']
    return rec


# --- per-window driver -----------------------------------------------------------------------------------------------------

def census_rows(path=CENSUS):
    rows = {}
    if not path.exists():
        return rows
    with open(path) as handle:
        for line in handle:
            x = json.loads(line)
            if x['layer'] == 'review':
                rows[(x['window'], x['id'])] = x
    return rows


def hidden_zone(polygon, run_line, partner_poly, ctx, design=DESIGN):
    s = ctx['s']
    zone = run_line.buffer(design['hidden_max_m']/s).intersection(partner_poly.buffer(design['canopy_buffer_m']/s))
    zone = zone.difference(polygon).intersection(box(1, 1, ctx['W']-1, ctx['H']-1))
    return polygonal(zone)


def analyse_window(path, index, measurer, census, design=DESIGN, frames=True):
    ctx = window_context(path)
    W, H, s = ctx['W'], ctx['H'], ctx['s']
    records, held, tallies = [], [], Counter()
    copy_cache = {}

    def copies_of(obj, polygon):
        if obj['id'] not in copy_cache:
            copy_cache[obj['id']] = copy_candidates(obj, polygon, ctx, index, measurer, design)
        return copy_cache[obj['id']]
    for obj in sorted(ctx['live'], key=lambda o: o['id']):
        if obj['kind'] not in KINDS:
            continue
        polygon = ctx['polys'].get(obj['id'])
        if polygon is None:
            continue
        info = decided_info(obj, ctx, design)
        base = {'window': ctx['window'], 'object_id': obj['id'], 'kind': obj['kind'], 'status': obj.get('status'),
                'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'decided': info['decided'], 'decided_reasons': info['reasons'],
                'apply_tier': 'proposal_only' if info['decided'] else 'undecided_object', 'layer': ctx['layer'],
                'named': (ctx['window'], obj['id']) in NAMED, 'before_ring': obj['ring'], 'before_holes': obj.get('holes') or [],
                'object_m2': r(polygon.area*s*s, 2), 'carried': isinstance(obj.get('carried_from'), dict)}
        crow = census.get((ctx['window'], obj['id'])) or {}
        # 1 un-carve: roofs along a canopy
        if obj['kind'] == 'building':
            cuts = canopy_completions(obj, ctx, design)
            for cut in cuts:
                tallies['canopy_cuts'] += 1
                completion = cut.pop('_completion', None)
                final = cut.pop('_final', None)
                pre = precarve_check(obj, ctx, completion) if completion is not None else None
                cut['stored_precarve'] = {k: v for k, v in (pre or {}).items() if not k.startswith('_')} or None
                partner_poly = unary_union([ctx['polys'][t] for t in cut['partners']]) if cut['partners'] else None
                copy_rec = None
                if frames and partner_poly is not None:
                    zone = hidden_zone(polygon, LineString(cut['run_px']), partner_poly, ctx, design)
                    copies = copies_of(obj, polygon)
                    if zone is not None and copies:
                        copy_rec = copy_uncarve(obj, polygon, ctx, zone, copies, cut['partners'], design, cut['run_index'])
                    same = [c for c in copies if c[1] is not None and (c[2].get('iou_in_window') or 0) >= design['copy_iou_min']]
                    if completion is not None and completion.area > 0 and same:
                        best = max(same, key=lambda c: (c[2]['iou_in_window'], -c[2]['resolution_ratio']))
                        cut['copy_cover_of_completion'] = r(area_parts(best[1].intersection(completion))/completion.area)
                        cut['copy_for_cover'] = {k: best[2][k] for k in ('scene_id', 'object_id', 'resolution_ratio', 'iou_in_window')}
                    cut['copies_checked'] = len(copies)
                    cut['copies_verified_same_object'] = len(same)
                if design.get('hold_skeptic_wrong'):
                    hold = skeptic_hold('canopy_completion', ctx['window'], obj['id'], cut['run_index'], cut.get('ring'),
                                        cut.get('holes'), design=design) if not cut['refusals'] and 'ring' in cut else None
                    if hold is not None:      # the whole cut is held: no copy route in its place either
                        cut['refusals'] = ['skeptic_marked_wrong_v1']
                        cut['skeptic_v1'] = hold['v1']
                        cut.pop('ring', None)
                        cut.pop('holes', None)
                        if copy_rec is not None:
                            copy_rec['refusals'].append('skeptic_marked_wrong_v1')
                    elif copy_rec is not None and not copy_rec['refusals']:
                        hold = skeptic_hold('copy_uncarve', ctx['window'], obj['id'], cut['run_index'], copy_rec.get('ring'),
                                            copy_rec.get('holes'), design=design)
                        if hold is not None:
                            copy_rec['refusals'].append('skeptic_marked_wrong_v1')
                            copy_rec['skeptic_v1'] = hold['v1']
                if pre is not None and pre['cover'] >= design['precarve_cover'] and not cut['refusals']:
                    tallies['precarve_found'] += 1
                if not cut['refusals'] and 'ring' in cut:
                    source = 'stored_precarve' if (pre and pre['cover'] >= design['precarve_cover']) else 'straight_edge'
                    ring, holes = cut.pop('ring'), cut.pop('holes')
                    prop, _ = proposal_object(obj, ctx, ring, holes, 'canopy_completion', cut['run_index'],
                                              {'cut_fix': {'type': 'canopy_completion', 'index': cut['run_index'], 'source': source,
                                                           'geometry': cut['geometry'], 'partners': cut['partners']}}, design)
                    records.append({**base, 'type': 'canopy_completion', 'source': source, 'detail': cut, 'proposal': prop,
                                    '_completion': completion, '_final': final, '_partners': cut['partners']})
                    tallies['canopy_completion'] += 1
                elif copy_rec is not None and not copy_rec['refusals']:
                    comp, fin, cp = copy_rec.pop('_completion'), copy_rec.pop('_final'), copy_rec.pop('_copy')
                    ring, holes = copy_rec.pop('ring'), copy_rec.pop('holes')
                    prop, _ = proposal_object(obj, ctx, ring, holes, 'copy_uncarve', cut['run_index'],
                                              {'cut_fix': {'type': 'copy_uncarve', 'index': cut['run_index'], 'source': 'other_frame_copy',
                                                           'copy': copy_rec['copy'], 'partners': cut['partners']}}, design)
                    records.append({**base, 'type': 'copy_uncarve', 'source': 'other_frame_copy', 'detail': {**cut, 'copy_route': copy_rec},
                                    'proposal': prop, '_completion': comp, '_final': fin, '_partners': cut['partners'], '_copy': cp})
                    tallies['copy_uncarve'] += 1
                else:
                    if copy_rec is not None:
                        for k in ('_completion', '_final', '_copy', 'ring', 'holes'):
                            copy_rec.pop(k, None)
                        cut['copy_route'] = copy_rec
                    held.append({**{k: v for k, v in base.items() if not k.startswith('before')}, 'type': 'canopy_cut_held',
                                 'detail': cut, 'census_flags': crow.get('flags')})
        # trees 'carved by a roof' (census carve run with a building partner): copy route only
        if obj['kind'] == 'tree' and frames:
            runs = [x for x in (crow.get('runs') or []) if x['class'] == 'carve' and x['partner_kind'] == 'building']
            for k, run in enumerate(runs):
                tallies['tree_carved_by_roof'] += 1
                partner = ctx['polys'].get(run['partner_id'])
                if partner is None:
                    continue
                shared = polygon.exterior.intersection(partner.buffer(1.5))
                zone = hidden_zone(polygon, shared if not shared.is_empty else polygon.exterior, partner, ctx, design)
                copies = copies_of(obj, polygon)
                rec = copy_uncarve(obj, polygon, ctx, zone, copies, [run['partner_id']], design, k) if (zone is not None and copies) \
                    else {'refusals': ['no_verified_copy' if not copies else 'no_zone'], 'copies': [c[2] for c in copies]}
                hold = skeptic_hold('copy_uncarve', ctx['window'], obj['id'], k, rec.get('ring'), rec.get('holes'), design=design) \
                    if not rec['refusals'] else None
                if hold is not None:
                    rec['refusals'].append('skeptic_marked_wrong_v1')
                    rec['skeptic_v1'] = hold['v1']
                if not rec['refusals']:
                    comp, fin, cp = rec.pop('_completion'), rec.pop('_final'), rec.pop('_copy')
                    ring, holes = rec.pop('ring'), rec.pop('holes')
                    prop, _ = proposal_object(obj, ctx, ring, holes, 'copy_uncarve', k,
                                              {'cut_fix': {'type': 'copy_uncarve', 'index': k, 'source': 'other_frame_copy',
                                                           'copy': rec['copy'], 'partners': [run['partner_id']]}}, design)
                    records.append({**base, 'type': 'copy_uncarve', 'source': 'other_frame_copy', 'detail': rec, 'proposal': prop,
                                    '_completion': comp, '_final': fin, '_partners': [run['partner_id']], '_copy': cp})
                    tallies['copy_uncarve'] += 1
                else:
                    for key in ('_completion', '_final', '_copy', 'ring', 'holes'):
                        rec.pop(key, None)
                    held.append({**{k2: v for k2, v in base.items() if not k2.startswith('before')}, 'type': 'tree_carve_held',
                                 'detail': {'partner_id': run['partner_id'], 'length_m': run['length_m'], **rec}})
        # 2 frame spikes
        fixes, protrusions = spike_fixes(obj, ctx, design)
        for p in protrusions:
            held.append({**{k: v for k, v in base.items() if not k.startswith('before')}, 'type': 'protrusion_held', 'detail': p})
            tallies['protrusion_held'] += 1
        for fx in fixes:
            tallies[f"{fx.get('type', 'spike')}_candidates"] += 1
            comp, fin = fx.pop('_completion', None), fx.pop('_final', None)
            if not fx['refusals']:
                ring, holes = fx.pop('ring'), fx.pop('holes')
                prop, _ = proposal_object(obj, ctx, ring, holes, fx['type'], fx['spike_index'],
                                          {'cut_fix': {'type': fx['type'], 'index': fx['spike_index'], 'side': fx['side'],
                                                       'width_m': fx['width_m'], 'depth_m': fx['depth_m']}}, design)
                records.append({**base, 'type': fx['type'], 'source': 'frame_notch', 'detail': fx, 'proposal': prop,
                                '_completion': comp, '_final': fin, '_partners': []})
                tallies[fx['type']] += 1
            else:
                held.append({**{k: v for k, v in base.items() if not k.startswith('before')}, 'type': 'spike_held', 'detail': fx})
        # 3 body carry for frame-cut roofs
        if obj['kind'] == 'building' and frames:
            fl = frame_length_m(obj['ring'], W, H, s)
            if fl >= HAS_FRAME_M:
                tallies['frame_cut_roofs'] += 1
                if base['carried']:
                    held.append({**{k: v for k, v in base.items() if not k.startswith('before')}, 'type': 'body_carry_held',
                                 'detail': {'refusals': ['carried_slice_left_to_slice_body_census'], 'frame_m': r(fl)}})
                    continue
                rec = body_carry(obj, polygon, ctx, index, measurer, design)
                rec['frame_m'] = r(fl)
                rec['frame_sides'] = sorted({side for p in clean_ring(obj['ring']) for side in sides_of(p, W, H)})
                tallies[f"body_listed_{'yes' if rec['listed'] else 'no'}"] += 1
                if not rec['refusals'] and 'ring' in rec:
                    final, whole, comp, src = rec.pop('_final'), rec.pop('_whole'), rec.pop('_completion'), rec.pop('_source')
                    ring, holes = rec.pop('ring'), rec.pop('holes')
                    prop, _ = proposal_object(obj, ctx, ring, holes, 'body_carry', 0,
                                              {'carried_from': rec['carried_from'],
                                               'cut_fix': {'type': 'body_carry', 'index': 0, 'source': 'whole_roof_body'},
                                               'continues_beyond_frame': {'sides': rec['frame_sides'], 'whole_in': {
                                                   'scene_id': rec['carried_from']['scene_id'], 'object_id': rec['carried_from']['object_id']},
                                                   'whole_ring_px': rec['whole_ring']}}, design)
                    records.append({**base, 'type': 'body_carry', 'source': 'whole_roof_body', 'detail': rec, 'proposal': prop,
                                    '_completion': comp, '_final': final, '_partners': [], '_whole': whole, '_source': src})
                    tallies['body_carry'] += 1
                else:
                    for key in ('_final', '_whole', '_completion', '_source', 'ring', 'holes'):
                        rec.pop(key, None)
                    held.append({**{k: v for k, v in base.items() if not k.startswith('before')}, 'type': 'body_carry_held', 'detail': rec})
    return ctx, records, held, tallies


# --- renders ---------------------------------------------------------------------------------------------------------------

def _draw_poly(draw, geom, tr, colour, width):
    for g in ([geom] if geom.geom_type == 'Polygon' else [x for x in getattr(geom, 'geoms', []) if x.geom_type == 'Polygon']):
        draw.line(tr(list(g.exterior.coords)), fill=colour, width=width)
        for h in g.interiors:
            draw.line(tr(list(h.coords)), fill=colour, width=max(1, width-1))


def render_record(rec, ctx, out_path, number, measurer=None):
    """Raw panel left; annotated right: before yellow, after cyan, completion magenta at 25 %, partner canopy green. Imagery
    beyond the window frame is grey, or for a body carry the holder frame's image warped through the measured transform."""
    W, H = ctx['W'], ctx['H']
    before = Polygon(rec['before_ring'], [h for h in rec['before_holes'] if len(h) >= 3])
    before = before if before.is_valid else polygonal(make_valid(before))
    prop = rec['proposal']
    after = Polygon(prop['ring'], prop.get('holes') or [])
    whole = rec.get('_whole')
    geoms = [before, after]+([whole] if whole is not None else [])
    bx0 = min(g.bounds[0] for g in geoms)
    by0 = min(g.bounds[1] for g in geoms)
    bx1 = max(g.bounds[2] for g in geoms)
    by1 = max(g.bounds[3] for g in geoms)
    focus = rec.get('_completion')
    if rec['type'] in ('spike_fill', 'spike_tab_cut', 'canopy_completion', 'copy_uncarve') and focus is not None and not focus.is_empty:
        # zoom on the fix but keep the whole object in view when it is small
        fx0, fy0, fx1, fy1 = focus.bounds
        span = max(bx1-bx0, by1-by0)
        if span > 260:
            cx, cy = (fx0+fx1)/2, (fy0+fy1)/2
            half = max(70., 2.5*max(fx1-fx0, fy1-fy0))
            bx0, by0, bx1, by1 = cx-half, cy-half, cx+half, cy+half
    pad = max(18., .15*max(bx1-bx0, by1-by0))
    margin = 40 if whole is None else 10_000
    x0, y0 = int(math.floor(max(-margin, bx0-pad))), int(math.floor(max(-margin, by0-pad)))
    x1, y1 = int(math.ceil(min(W+margin, bx1+pad))), int(math.ceil(min(H+margin, by1+pad)))
    base = Image.new('RGB', (x1-x0, y1-y0), (70, 70, 70))
    if whole is not None and measurer is not None and rec.get('_source'):
        measured = measurer.transforms.get((rec['_source'], ctx['window']))
        if measured and measured.get('ft'):
            src = measurer.scene(rec['_source'])
            import io
            with Image.open(io.BytesIO(measurer.image_bytes(src))) as im:
                affine = sr.compose(measured['ft']['pixel_affine'], [[1., 0.], [0., 1.], [-x0, -y0]])
                warped, cover = sr.warp_into(im, affine, (x1-x0, y1-y0))
            arr = np.asarray(base).copy()
            arr[cover] = np.asarray(warped)[cover]
            base = Image.fromarray(arr)
    ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    base.paste(ctx['image'].crop((ix0, iy0, ix1, iy1)), (ix0-x0, iy0-y0))
    scale = max(1., min(8., RENDER_LONG_SIDE/max(x1-x0, y1-y0)))
    size = (max(1, int((x1-x0)*scale)), max(1, int((y1-y0)*scale)))
    raw = base.resize(size, Image.LANCZOS)

    def tr(pts):
        return [((p[0]-x0)*scale, (p[1]-y0)*scale) for p in pts]
    frame_line = tr([(0, 0), (W, 0), (W, H), (0, H), (0, 0)])
    dashed(ImageDraw.Draw(raw), frame_line, COLOURS['frame'], 1, 6, 4)
    ann = raw.copy().convert('RGBA')
    overlay = Image.new('RGBA', size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    if focus is not None and not focus.is_empty:
        for g in ([focus] if focus.geom_type == 'Polygon' else [x for x in getattr(focus, 'geoms', []) if x.geom_type == 'Polygon']):
            od.polygon(tr(list(g.exterior.coords)), fill=(*COLOURS['completion'], 64))
    ann = Image.alpha_composite(ann, overlay).convert('RGB')
    draw = ImageDraw.Draw(ann)
    dashed(draw, frame_line, COLOURS['frame'], 1, 6, 4)
    for pid in rec.get('_partners') or []:
        p = ctx['polys'].get(pid)
        if p is not None:
            _draw_poly(draw, p, tr, COLOURS['canopy'], 2)
    if focus is not None and not focus.is_empty:
        _draw_poly(draw, focus, tr, COLOURS['completion'], 1)
        fx0, fy0, fx1, fy1 = focus.bounds
        if max(fx1-fx0, fy1-fy0)*scale < 30:       # a small change gets a magenta ring so it can be found
            (cx, cy), = tr([((fx0+fx1)/2, (fy0+fy1)/2)])
            rad = max(fx1-fx0, fy1-fy0)*scale/2+14
            draw.ellipse([cx-rad, cy-rad, cx+rad, cy+rad], outline=COLOURS['completion'], width=2)
    _draw_poly(draw, before, tr, COLOURS['before'], 2)
    if whole is not None:
        outside = whole.difference(box(0, 0, W, H))
        for g in ([outside] if outside.geom_type == 'Polygon' else [x for x in getattr(outside, 'geoms', []) if x.geom_type == 'Polygon']):
            dashed(draw, tr(list(g.exterior.coords)), COLOURS['after'], 2, 6, 4)
    _draw_poly(draw, after, tr, COLOURS['after'], 2)
    banner = 62
    canvas = Image.new('RGB', (size[0]*2+8, size[1]+banner), COLOURS['box'])
    canvas.paste(raw, (0, banner))
    canvas.paste(ann, (size[0]+8, banner))
    d = ImageDraw.Draw(canvas)
    f = font(13)
    d.text((4, 3), f"#{number} {rec['type']} {ctx['window']} {rec['object_id']} ({rec['kind']}, status {rec['status']}"
                   f"{', geometry reviewed' if rec['geometry_reviewed'] else ''}; {rec['apply_tier']})", fill=COLOURS['text'], font=f)
    d.text((4, 21), describe(rec), fill=COLOURS['text'], font=f)
    d.text((4, 39), 'left raw | right: before yellow, after cyan, change magenta 25 %, partner canopy green, window frame grey dashed'
                    + ('; body beyond the frame cyan dashed over the holder frame image' if whole is not None else ''),
           fill=COLOURS['text'], font=f)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path


def describe(rec):
    d = rec['detail']
    if rec['type'] == 'canopy_completion':
        return (f"{d['geometry']} completion under canopy {', '.join(x[:8] for x in d['partners'])}: +{d['completion_m2']} m2, "
                f"hidden depth {d['hidden_depth_m']} m, canopy share {d.get('completion_canopy_share')}, "
                f"copy cover {d.get('copy_cover_of_completion', '-')}; source {rec['source']}")
    if rec['type'] == 'copy_uncarve':
        c = d.get('copy_route', d)
        cp = c['copy']
        return (f"copy {cp['scene_id']}/{cp['object_id'][:8]} (x{cp['resolution_ratio']} px, shift {cp['transform']['shift_m']} m, "
                f"ncc {cp['transform']['ncc']}): +{c['completion_m2']} m2")
    if rec['type'] in ('spike_fill', 'spike_tab_cut'):
        return (f"{d['shape']} on the {d['side']} frame, {d['width_m']} m wide, {d['depth_m']} m deep: +{d['added_m2']} m2 "
                f"-{d['removed_m2']} m2, vertices {d['vertices_before']}->{d['vertices_after']}")
    b = d['best']
    return (f"whole roof in {b['scene_id']}/{b['object_id'][:8]} (x{b['resolution_ratio']} px; cover {b['cover']}, in-frame IoU "
            f"{b['inframe_iou']}, shift {b['transform']['shift_m']} m, ncc {b['transform']['ncc']}); frame {d['frame_m']} m "
            f"({', '.join(d['frame_sides'])})")


def contact_sheet(paths, out, cell=420, columns=4):
    if not paths:
        return None
    tiles = []
    for p in paths:
        im = Image.open(p)
        im.thumbnail((cell*2, cell))
        tiles.append(im.copy())
        im.close()
    w = max(t.width for t in tiles)
    h = max(t.height for t in tiles)
    rows = math.ceil(len(tiles)/columns)
    sheet = Image.new('RGB', (w*columns, h*rows), (20, 20, 20))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % columns)*w, (i//columns)*h))
    sheet.save(out)
    return out


# --- report ----------------------------------------------------------------------------------------------------------------

ORDER = {'canopy_completion': 0, 'copy_uncarve': 1, 'spike_fill': 2, 'spike_tab_cut': 3, 'body_carry': 4}
URL = 'http://localhost:3008/scenes?scene={w}&status=all&frame=all&object={o}&layer=edited'


def number_records(records):
    named = [x for x in records if x['named']]
    rest = [x for x in records if not x['named']]
    named.sort(key=lambda x: ([n[1] for n in NAMED].index(x['object_id']), ORDER[x['type']]))
    rest.sort(key=lambda x: (ORDER[x['type']], x['window'], x['object_id'], x['proposal']['cut_fix']['index']))
    for i, x in enumerate(named+rest, start=1):
        x['number'] = i
    return named+rest


def marks_line(x, folder='prototype', note=''):
    d = x['detail']
    link = URL.format(w=x['window'], o=x['object_id'])
    head = f"{x['number']}. `{x['window']}` {x['kind']} `{x['object_id']}` ({x['status']}{', geometry reviewed' if x['geometry_reviewed'] else ''})"
    lineage = (f"replaces {', '.join(x['proposal']['replaces']) or '-'}; replaces on accept "
               f"{', '.join(x['proposal']['replaces_on_accept']) or '-'}")
    if x['type'] == 'canopy_completion':
        what = (f"**canopy completion ({d['geometry']})**: the roof's two visible eave lines continued under tree "
                f"{', '.join(t[:8] for t in d['partners'])}; adds {d['completion_m2']} m2 (hidden depth {d['hidden_depth_m']} m, "
                f"{int(round((d.get('completion_canopy_share') or 0)*100))} % under the canopy)"
                + (f"; another frame's copy covers {int(round(d['copy_cover_of_completion']*100))} % of the added corner"
                   if d.get('copy_cover_of_completion') is not None else '; no verified copy in another frame'))
    elif x['type'] == 'copy_uncarve':
        c = d.get('copy_route', d)
        what = (f"**copy un-carve**: the same object in `{c['copy']['scene_id']}` ({c['copy']['resolution_ratio']}x the window's "
                f"pixel size), moved through a verified transform, adds {c['completion_m2']} m2 under the partner")
    elif x['type'] == 'spike_fill':
        what = (f"**frame notch filled**: a {d['width_m']} m wide, {d['depth_m']} m deep slit on the {d['side']} frame edge is "
                f"closed (+{d['added_m2']} m2)")
    elif x['type'] == 'spike_tab_cut':
        what = f"**frame tab cut**: a {d['width_m']} m tab on the {d['side']} frame edge removed (-{d['removed_m2']} m2)"
    else:
        b = d['best']
        what = (f"**body carry**: the whole roof is outlined in `{b['scene_id']}` object `{b['object_id']}` "
                f"({'a review window' if b['window'] else 'a registered frame'}, {b['resolution_ratio']}x the window's pixel size; "
                f"covers {int(round(b['cover']*100))} % of this outline, in-frame IoU {b['inframe_iou']}; inside the frame it drops "
                f"{b['drops_m2']} m2 of the current outline and adds {b['adds_in_frame_m2']} m2); the proposal keeps its in-frame part "
                f"here and records where the roof is whole (the window cannot hold an outline beyond its image)")
    return (f"{head}: {what}. {lineage}. Render `{folder}/{x['render']}`. Editor: {link}.{note} Mark: ____")


def write_marks(path, numbered, counts, named_notes):
    lines = ['# MARKS: object cuts v1 (sidecar proposals, 2026-10-08)', '',
             'Reviewer ask (8 Oct ~08:45 AEDT, verbatim): "still seeing cuts randomly which then impactd the roof boundsary or tree '
             'boundary" (win-3dd37155248c04ef objects 1bac44c96bf45847 and 55ca4c5c041e3646).', '',
             'Each numbered line is ONE proposed outline change. Nothing is applied: no scene, review or count was written. A change '
             'to a confirmed, needs-adjustment, geometry-reviewed or edited outline happens only if you accept it ("replaces on '
             'accept"); a candidate outline would be replaced by an accepted proposal ("replaces").', '',
             '## How to answer (in chat)', '',
             'One word per line number: `1 right, 2 wrong, 3 unclear`; ranges work (`5-14 right`). **right** = the cyan outline is '
             'the object (accept the change); **wrong** = keep the yellow outline (say why if you can: "corner is not under the '
             'tree", "the notch is a real gap", "the carried roof is a different building"); **unclear** = cannot tell from the '
             'render. Unmarked lines stay as they are. Two lines on the same object (for example #2 and #3) are separate '
             'proposals against the same current outline: mark each one; if both are right, an apply step would take the carry '
             'and then the completion.', '',
             'Renders: left = imagery; right = before (yellow), proposed (cyan), the change (magenta at 25 %), the tree canopy the '
             'roof stops at (green), the window frame (grey dashed). Body-carry renders also show the roof beyond the frame (cyan '
             'dashed) over the holder frame\'s own image, warped through the measured transform.', '',
             '## What each kind of line proposes', '',
             '- **canopy completion**: a roof outline that stops along a tree canopy is continued along the roof\'s own two visible eave '
             'lines until they meet (or join, for one straight eave). Only when both eave lines are visible on either side of the '
             'canopy, the added corner is mostly under the canopy, the visible part looks like the same roof, there is no visible '
             'roof edge where the outline stopped, nothing else (another building, a road, a pool, a car, lawn) is under it, and '
             'it stays inside the window.',
             '- **copy un-carve**: the same roof detected in another frame (no coarser than this one), moved through a verified '
             'image registration, reaches further under the tree; the part under the tree is added.',
             '- **frame notch filled**: a narrow slit where the outline dips in from the window frame and comes back (the 1bac '
             'spike) is closed back onto the frame line. A notch that another object fills, or that looks like a different '
             'material (a shrub cut out of a lawn), is left alone, and so are tabs and narrow touches on the frame (mostly a '
             'real roof corner the frame clips).',
             '- **body carry**: the roof is cut by this window\'s frame, and another frame holds the whole roof. The outline here '
             'can only cover the part inside this image; the proposal takes that part from the whole-roof outline and records '
             'where the whole roof is.', '',
             '## Counts', '']
    for k, v in counts.items():
        text = ', '.join(f"{a.replace('_', ' ')} {b}" for a, b in v.items()) if isinstance(v, dict) else v
        lines.append(f"- {k.replace('_', ' ')}: {text}")
    lines += ['', '## The two named objects', '']
    lines += named_notes
    lines += ['', '## Proposals', '']
    current = None
    for x in numbered:
        section = 'Named objects' if x['named'] else {'canopy_completion': 'Canopy completions', 'copy_uncarve': 'Copy un-carves',
                                                         'spike_fill': 'Frame notches', 'spike_tab_cut': 'Frame tabs',
                                                         'body_carry': 'Body carries'}[x['type']]
        if section != current:
            lines += ['', f'### {section}', '']
            current = section
        lines.append(marks_line(x))
    lines.append('')
    path.write_text('\n'.join(lines))


HELD_WORDS = {
    'coarser_than_window': 'the only whole-roof outlines are in coarser imagery',
    'no_whole_body': 'no other frame holds the whole roof',
    'inframe_iou_low': "the other frame's outline disagrees with this one inside the frame",
    'drops_observed_outline': "the other frame's outline leaves out part of this one inside the frame",
    'cover_below_whole': "the other frame's outline covers too little of this one",
    'corner_angle': 'the two eave lines do not meet at a roof corner',
    'arms_diverge': 'the two eave lines do not meet under the canopy',
    'extension_too_long': 'the eave lines would have to run more than 4 m under the canopy',
    'parallel_offset_eaves': 'the two eaves are parallel but offset (a step, not one eave)',
    'only_coarser_copies': 'the only copies in other frames are in coarser imagery',
}


def held_words(reasons):
    out = []
    for reason in reasons:
        side, _, base = reason.partition(':') if reason[:2] in ('a:', 'b:') else ('', '', reason)
        text = HELD_WORDS.get(base, base.replace('_', ' '))
        out.append(f'{text} ({"eave " + side + ", " if side else ""}{base})')
    return '; '.join(out)


def named_notes(numbered, held):
    """The two named objects in words: each proposal by number, everything held with its reason, and what no proposal fixes."""
    notes = []
    for w, oid in NAMED:
        props = [x for x in numbered if x['window'] == w and x['object_id'] == oid]
        hs = [x for x in held if x['window'] == w and x['object_id'] == oid]
        notes.append(f'**`{oid}`** ({URL.format(w=w, o=oid)})')
        notes.append('')
        for x in props:
            notes.append(f"- #{x['number']} {x['type'].replace('_', ' ')}: see the line below.")
        for x in hs:
            d = x['detail']
            if x['type'] == 'body_carry_held':
                m = d.get('best') or (d.get('measured') or [{}])[0]
                where = f" (best: `{m.get('scene_id')}` object `{m.get('object_id')}`, {m.get('resolution_ratio')}x the window's pixel size)" if m else ''
                notes.append(f"- body carry held: {held_words(d.get('refusals') or [])}{where}.")
            elif x['type'] == 'canopy_cut_held':
                notes.append(f"- outline run along tree {', '.join(t[:8] for t in d.get('partners') or [])} at px {d.get('at_px')} "
                             f"({d.get('run_m')} m) held: {held_words(d.get('refusals') or [])}.")
            else:
                notes.append(f"- {x['type'].replace('_', ' ')}: {held_words(d.get('refusals') or [d.get('reason') or '-'])}.")
        notes.append('- Not fixed by any line here: the straight TOP edge is the window frame (the image ends there). An outline '
                     'cannot leave the image, so inside this window the roof stays cut at the frame; a body carry records where '
                     'the whole roof is, and the census option 1 (frame edges drawn dashed in the editor) makes that edge read as '
                     'the frame rather than a cut.')
        notes.append('')
    return notes


def build_marks(out, numbered, held, counts):
    write_marks(out/'MARKS-cuts-v1.md', numbered, {k: v for k, v in counts.items() if k in (
        'windows', 'proposals', 'proposals_by_type', 'proposals_by_tier', 'held_by_type')}, named_notes(numbered, held))


def marks_only(out):
    """Rewrite MARKS-cuts-v1.md from an existing proposals.json (wording changes need no recompute)."""
    data = load_json(out/'proposals.json')
    build_marks(out, data['proposals'], data['held'], data['counts'])


# --- v2 report: every v1 line kept, changed or removed ----------------------------------------------------------------------

# The by-eye skeptic's marks on v1 lines (eye-cuts.md 'Full tally by line'; R right, W wrong, U unclear; absent = not read).
SKEPTIC_MARKS_V1 = {**dict.fromkeys((1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 22, 23, 25, 27, 28, 29), 'right'),
                    **dict.fromkeys((6, 16, 17, 18, 20, 21, 24, 30), 'unclear'), 19: 'wrong', 26: 'wrong',
                    **dict.fromkeys((31, 32, 33, 35, 39, 40, 41, 42, 43), 'right'), 38: 'unclear',
                    **dict.fromkeys((34, 36, 37, 44), 'wrong'),
                    **dict.fromkeys((45, 46, 47, 49, 50, 51, 52, 53, 54, 55, 57, 58, 59, *range(61, 73)), 'right'),
                    48: 'unclear', 56: 'unclear',
                    **dict.fromkeys((73, 85, 91, 116), 'right'), 105: 'wrong', 106: 'wrong', 111: 'unclear', 148: 'unclear'}
V2_WORDS = {
    'adds_spike_or_sliver': 'the result ring adds a spike or sliver the current outline does not have',
    'eaves_meet_outside_canopy': 'the two eave lines meet outside the tree canopy (+0.3 m)',
    'completion_deeper_than_canopy': 'the completion reaches deeper than the canopy that hides it',
    'skeptic_marked_wrong_v1': 'the by-eye skeptic marked this line wrong in v1 and v2 would propose it again unchanged',
    'coarser_than_window': 'the best whole-roof outline is in coarser imagery than this window (m/px ratio > 1.0)',
    'cover_below_whole': "the other frame's outline covers less than 95 % of this one",
    'inframe_iou_low': "the other frame's outline disagrees with this one inside the frame (in-frame IoU < 0.9)",
}


def line_key(x):
    return (x['type'], x['window'], x['object_id'], x['proposal']['cut_fix']['index'])


def v2_refusals(v1rec, held):
    """Why v2 does not propose a v1 line: the refusals of the matching v2 held record (canopy cut by run index, copy un-carve
    by the cut's copy route or the tree-carve record, body carry by object, notch by spike index)."""
    t, w, o, i = line_key(v1rec)
    for h in held:
        if h['window'] != w or h['object_id'] != o:
            continue
        d = h['detail']
        if t == 'canopy_completion' and h['type'] == 'canopy_cut_held' and d.get('run_index') == i:
            return d.get('refusals') or [], d
        if t == 'copy_uncarve' and v1rec['kind'] == 'building' and h['type'] == 'canopy_cut_held' and d.get('run_index') == i:
            return (d.get('copy_route') or {}).get('refusals') or ['no_copy_route'], d.get('copy_route') or {}
        if t == 'copy_uncarve' and h['type'] == 'tree_carve_held' and \
                d.get('partner_id') in (v1rec['proposal']['cut_fix'].get('partners') or []):
            return d.get('refusals') or [], d
        if t == 'body_carry' and h['type'] == 'body_carry_held':
            return d.get('refusals') or [], d
        if t in ('spike_fill', 'spike_tab_cut') and h['type'] == 'spike_held' and d.get('spike_index') == i:
            return d.get('refusals') or [], d
    return ['not_found_in_v2'], {}


def ring_change(old, new, object_m2, before_ring):
    """(IoU, added m2, dropped m2) of a new proposal ring against the v1 ring of the same line."""
    a, b = Polygon(old['ring'], old.get('holes') or []), Polygon(new['ring'], new.get('holes') or [])
    a, b = (a if a.is_valid else polygonal(make_valid(a))), (b if b.is_valid else polygonal(make_valid(b)))
    base = Polygon(before_ring)
    s2 = object_m2/base.area if base.area > 0 else 0.
    return r(area_parts(a.intersection(b))/area_parts(a.union(b))), r(area_parts(b.difference(a))*s2, 2), r(area_parts(a.difference(b))*s2, 2)


def guard_words(refusals, detail):
    words = '; '.join(V2_WORDS.get(x, HELD_WORDS.get(x, x.replace('_', ' '))) + f' ({x})' for x in refusals)
    feats = (detail.get('spike_guard') or {}).get('features') or []
    if feats:
        bits = []
        for f in feats[:3]:
            if f['test'] == 'sharp_vertex':
                bits.append(f"a {f['angle_deg']} deg vertex at px {f['at_px']}")
            elif f['test'] == 'census_frame_spike':
                bits.append(f"a frame {f['shape']} {f['width_m']} m wide, {f['depth_m']} m deep at px {f['at_px']}")
            else:
                bits.append(f"a {f['shape']} {f['width_m']} m wide, {f['depth_m']} m deep at px {f['at_px']}")
        words += ' - ' + ', '.join(bits)
    fp = detail.get('canopy_footprint') or {}
    if fp and any(x in refusals for x in ('eaves_meet_outside_canopy', 'completion_deeper_than_canopy')):
        words += f" (corner {fp.get('corner_in_canopy_m')} m inside the canopy, depth {fp.get('completion_depth_m')} m vs canopy {fp.get('canopy_depth_m')} m)"
    best = detail.get('best') or {}
    if best and refusals and any(x in refusals for x in ('coarser_than_window', 'cover_below_whole', 'inframe_iou_low', 'skeptic_marked_wrong_v1')):
        words += (f" (best body `{best.get('scene_id')}`/{str(best.get('object_id'))[:8]}: x{best.get('resolution_ratio')} px, "
                  f"cover {best.get('cover')}, in-frame IoU {best.get('inframe_iou')})")
    return words


def v1_body_verdict(old, detail, design=DESIGN_V2):
    """v2's reasons against the v1 holder body of a removed body carry: its own measured record when v2 measured it again
    (same transform cache key), else v1's measured cover / in-frame IoU / ratio read against the v2 thresholds."""
    b = (old.get('detail') or {}).get('best') or {}
    for m in detail.get('measured') or []:
        if (m.get('scene_id'), m.get('object_id')) == (b.get('scene_id'), b.get('object_id')):
            return list(m.get('reasons') or []), m
    reasons = []
    if (b.get('cover') or 0) < design['body_cover_whole']:
        reasons.append('cover_below_whole')
    if (b.get('inframe_iou') or 0) < design['body_inframe_iou_min']:
        reasons.append('inframe_iou_low')
    if (b.get('resolution_ratio') or 0) > design['body_resolution_max']+design.get('body_resolution_tol', 0.):
        reasons.append('coarser_than_window')
    if skeptic_hold('body_carry', old['window'], old['object_id'], 0, holder=(b.get('scene_id'), b.get('object_id')), design=design):
        reasons.append('skeptic_marked_wrong_v1')
    return reasons, b


def compare_v1(numbered, held, path=OUT/'proposals.json', design=DESIGN_V2):
    """Every v1 line: same ring, changed ring or removed (with the v2 reason); every v2 line new to v2. Sets x['v1']."""
    if not Path(path).exists():
        return None
    v1 = load_json(path)
    old_by_key = {line_key(x): x for x in v1['proposals']}
    rows, seen = [], set()
    for x in numbered:
        k = line_key(x)
        old = old_by_key.get(k)
        base = {'type': x['type'], 'window': x['window'], 'object_id': x['object_id'], 'index': k[3], 'v2': x['number']}
        if old is None:
            x['v1'] = {'status': 'new'}
            rows.append({**base, 'v1': None, 'status': 'new', 'skeptic_v1': None})
            continue
        seen.add(k)
        same = old['proposal']['ring'] == x['proposal']['ring'] and \
            (old['proposal'].get('holes') or []) == (x['proposal'].get('holes') or [])
        row = {**base, 'v1': old['number'], 'status': 'same' if same else 'changed', 'skeptic_v1': SKEPTIC_MARKS_V1.get(old['number'])}
        if not same:
            row['ring_iou_vs_v1'], row['adds_vs_v1_m2'], row['drops_vs_v1_m2'] = ring_change(old['proposal'], x['proposal'],
                                                                                           x['object_m2'], x['before_ring'])
            if x['type'] == 'body_carry':
                was, now = old['proposal'].get('carried_from') or {}, x['proposal'].get('carried_from') or {}
                row['holder_v1'] = f"{was.get('scene_id')}/{was.get('object_id')}"
                row['holder_v2'] = f"{now.get('scene_id')}/{now.get('object_id')}"
        x['v1'] = {'number': old['number'], 'status': row['status']}
        rows.append(row)
    for k, old in old_by_key.items():
        if k in seen:
            continue
        refusals, detail = v2_refusals(old, held)
        row = {'type': k[0], 'window': k[1], 'object_id': k[2], 'index': k[3], 'v2': None, 'v1': old['number'],
               'status': 'removed', 'skeptic_v1': SKEPTIC_MARKS_V1.get(old['number']), 'v2_refusals': refusals,
               'why': guard_words(refusals, detail)}
        if k[0] == 'body_carry':
            # the reason that matters is v2's verdict on the v1 holder body (v2's best alternative may be another body)
            reasons, numbers = v1_body_verdict(old, detail, design)
            row.update(v1_holder=f"{numbers.get('scene_id')}/{numbers.get('object_id')}", v1_holder_v2_reasons=reasons,
                       v2_refusals=reasons or refusals)
            row['why'] = (f"v1's body `{numbers.get('scene_id')}`/{str(numbers.get('object_id'))[:8]} (x{numbers.get('resolution_ratio')} px, "
                          f"cover {numbers.get('cover')}, in-frame IoU {numbers.get('inframe_iou')}): "
                          + '; '.join(V2_WORDS.get(x, HELD_WORDS.get(x, x.replace('_', ' '))) + f' ({x})' for x in reasons)
                          + ('' if (detail.get('best') or {}).get('scene_id') == numbers.get('scene_id') else
                             f"; no other body passes either (best alternative `{(detail.get('best') or {}).get('scene_id')}`: "
                             f"{', '.join(refusals)})"))
        rows.append(row)
    rows.sort(key=lambda row: (row['v1'] is None, row['v1'] or 0, row['v2'] or 0))
    summary = {'v1_lines': len(old_by_key), 'by_status': dict(Counter(row['status'] for row in rows)),
               'removed_by_type': dict(Counter(row['type'] for row in rows if row['status'] == 'removed')),
               'changed_by_type': dict(Counter(row['type'] for row in rows if row['status'] == 'changed')),
               'new_by_type': dict(Counter(row['type'] for row in rows if row['status'] == 'new')),
               'removed_by_skeptic_mark': dict(Counter(row['skeptic_v1'] or 'not read' for row in rows if row['status'] == 'removed')),
               'changed_by_skeptic_mark': dict(Counter(row['skeptic_v1'] or 'not read' for row in rows if row['status'] == 'changed')),
               'v1_code_sha256': v1.get('code_sha256'), 'v1_generated_at': v1.get('generated_at'), 'v1_path': rel(path)}
    return {'summary': summary, 'rows': rows}


def change_line(row):
    head = f"v1 #{row['v1']}" if row['v1'] else 'new'
    what = f"{row['type'].replace('_', ' ')} `{row['window']}` `{row['object_id']}`"
    mark = f" (skeptic on v1: {row['skeptic_v1'] or 'not read'})" if row['v1'] else ''
    if row['status'] == 'removed':
        return f"- {head} {what}{mark}: **removed**: {row['why']}."
    if row['status'] == 'changed':
        extra = (f"; holder `{row['holder_v1']}` -> `{row['holder_v2']}`" if row.get('holder_v1') and row['holder_v1'] != row['holder_v2'] else '')
        return (f"- {head} -> v2 #{row['v2']} {what}{mark}: **ring changed** (IoU with the v1 ring {row['ring_iou_vs_v1']}, "
                f"+{row['adds_vs_v1_m2']} / -{row['drops_vs_v1_m2']} m2{extra}).")
    return f"- new -> v2 #{row['v2']} {what}: **new in v2** (no v1 line with this key)."


V2_INTRO = [
    'v2 of the cut-fix proposals. The by-eye skeptic read 79 of the 157 v1 lines (.run/session-2026-10-08-cuts-veg/eye-cuts.md: '
    '58 right, 8 wrong, 13 unclear; the 3 named lines right). v2 adds one guard per group of wrong lines and changes nothing '
    'else (the 3 named lines are unchanged):',
    '',
    '- **copy un-carve**: refused when the result ring adds a spike or sliver the current outline does not have: a notch or '
    'protrusion narrower than 1 m (deeper than wide, >= 3 px deep; cut_census.find_spikes on the frame, the same test on any '
    'chord of the ring elsewhere), a part narrower than 0.5 m (>= 1.5 px deep), or a vertex sharper than 25 deg (the '
    "skeptic's slit scan).",
    '- **canopy completion**: the completed corner must lie inside the occluding tree canopy buffered 0.3 m, and the completion '
    'may reach no deeper than that canopy reaches inside it.',
    '- **body carry**: only from whole-roof outlines at this window\'s pixel size or finer (m/px ratio <= 1.0), through a '
    'verified transform, covering >= 95 % of this outline, with in-frame IoU >= 0.9, and never the two bodies the skeptic '
    'marked wrong. Equal pixel size counts as "at this resolution" up to 0.5 % (the same zoom read at another latitude).',
    '- **skeptic marks**: the 8 v1 lines the skeptic marked wrong are never proposed again with the same ring (body carry: the '
    'same holder body). The guards above are checked first; the mark is the backstop.',
]


def guard_hits(held):
    """How often each v2 guard refused anything over the whole run (not only v1 lines), and the footprint figures of the
    canopy cuts held by a skeptic mark."""
    hits = Counter()
    skeptic_corners = []
    for h in held:
        d = h['detail']
        if h['type'] == 'canopy_cut_held':
            for code in ('eaves_meet_outside_canopy', 'completion_deeper_than_canopy'):
                hits[code] += code in (d.get('refusals') or [])
            hits['copy:adds_spike_or_sliver'] += 'adds_spike_or_sliver' in ((d.get('copy_route') or {}).get('refusals') or [])
            if d.get('skeptic_v1'):
                skeptic_corners.append((d['skeptic_v1'], (d.get('canopy_footprint') or {}).get('corner_in_canopy_m')))
        elif h['type'] == 'tree_carve_held':
            hits['copy:adds_spike_or_sliver'] += 'adds_spike_or_sliver' in (d.get('refusals') or [])
    return hits, sorted(skeptic_corners)


def effect_lines(changes, held=()):
    """Per route: v1 lines removed by v2, grouped by the skeptic's v1 mark and by the first v2 refusal; then each guard's hits."""
    if not changes:
        return []
    out = ['', 'Effect on the v1 lines (removed = v2 does not propose it; the reason is the first v2 refusal):', '']
    for ptype in ('canopy_completion', 'copy_uncarve', 'spike_fill', 'body_carry'):
        rows = [row for row in changes['rows'] if row['type'] == ptype and row['v1']]
        gone = [row for row in rows if row['status'] == 'removed']
        if not rows:
            continue
        marks = Counter(row['skeptic_v1'] or 'not read' for row in gone)
        reasons = Counter((row['v2_refusals'] or ['-'])[0] for row in gone)
        wrong = [row['v1'] for row in rows if row['skeptic_v1'] == 'wrong']
        kept_wrong = [row['v1'] for row in rows if row['skeptic_v1'] == 'wrong' and row['status'] != 'removed']
        out.append(f"- {ptype.replace('_', ' ')}: {len(gone)} of {len(rows)} v1 lines removed"
                   + (f" (skeptic marks: {', '.join(f'{k} {v}' for k, v in sorted(marks.items()))}; first reason: "
                      f"{', '.join(f'{k} {v}' for k, v in reasons.most_common())})" if gone else '')
                   + (f"; skeptic-wrong v1 lines {wrong}: {'all removed' if not kept_wrong else f'still proposed {kept_wrong}'}" if wrong else '')
                   + '.')
    hits, corners = guard_hits(held)
    out += ['', f"Guard hits over every cut in the 54 windows: copy spike / sliver guard {hits['copy:adds_spike_or_sliver']}; canopy "
                f"footprint {hits['eaves_meet_outside_canopy']} (corner outside the canopy) + {hits['completion_deeper_than_canopy']} "
                '(deeper than the canopy). '
                + (('The footprint guard refuses nothing here: every completion corner lies inside its tree outline, including the '
                    'two the skeptic marked wrong (' + ', '.join(f'v1 #{n}: corner {c} m inside' for n, c in corners)
                    + '); those two are held by the skeptic\'s mark alone.')
                   if not hits['eaves_meet_outside_canopy'] and not hits['completion_deeper_than_canopy'] and corners else '')]
    return out


def write_marks_v2(path, numbered, counts, named, changes, held=()):
    named_lines = [x for x in numbered if x['named']]
    rest = [x for x in numbered if not x['named']]

    def line(x):
        ref = (x.get('v1') or {})
        note = (f" v1 #{ref['number']} ({'same ring' if ref['status'] == 'same' else 'ring changed'})" if ref.get('number')
                else ' New in v2.')
        if ref.get('number'):
            note = note + '.'
        return marks_line(x, folder='prototype-v2', note=note)
    lines = ['# MARKS: object cuts v2 (sidecar proposals, 2026-10-08)', '',
             'Reviewer ask (8 Oct ~08:45 AEDT, verbatim): "still seeing cuts randomly which then impactd the roof boundsary or tree '
             'boundary" (win-3dd37155248c04ef objects 1bac44c96bf45847 and 55ca4c5c041e3646).', '', *V2_INTRO, '',
             'Each numbered line is ONE proposed outline change. Nothing is applied: no scene, review or count was written. A change '
             'to a confirmed, needs-adjustment, geometry-reviewed or edited outline happens only if you accept it ("replaces on '
             'accept"); a candidate outline would be replaced by an accepted proposal ("replaces"). Numbers are new: "v1 #N" '
             'on a line is its number in MARKS-cuts-v1.md.', '',
             *effect_lines(changes, held), '',
             '## How to answer (in chat)', '',
             'One word per line number: `1 right, 2 wrong, 3 unclear`; ranges work (`5-14 right`). **right** = the cyan outline is '
             'the object (accept the change); **wrong** = keep the yellow outline (say why if you can: "corner is not under the '
             'tree", "the notch is a real gap", "the carried roof is a different building", "adds a spike"); **unclear** = cannot '
             'tell from the render. Unmarked lines stay as they are. Two lines on the same object (for example #2 and #3) are '
             'separate proposals against the same current outline: mark each one (no apply step composes them yet). To disagree '
             'with a removal below, quote its v1 number (`v1 #31 should stay`).', '',
             'Renders (prototype-v2/renders/): left = imagery; right = before (yellow), proposed (cyan), the change (magenta at '
             '25 %), the tree canopy the roof stops at (green), the window frame (grey dashed). Body-carry renders also show the '
             "roof beyond the frame (cyan dashed) over the holder frame's own image, warped through the measured transform.", '',
             '## The three named lines (the reviewer\'s two objects)', '']
    lines += [line(x) for x in named_lines]
    lines += ['', *[n.replace('see the line below', 'see the line above') for n in named],
              '## Changes vs v1 (every v1 line changed or removed, and every new line)', '']
    if changes:
        sm = changes['summary']
        lines.append(f"v1 had {sm['v1_lines']} lines (`{sm['v1_path']}`, code {str(sm['v1_code_sha256'])[:12]}). Status: "
                     + ', '.join(f'{k} {v}' for k, v in sorted(sm['by_status'].items())) + '. Removed by type: '
                     + (', '.join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(sm['removed_by_type'].items())) or 'none')
                     + '; removed lines by the skeptic\'s v1 mark: '
                     + (', '.join(f'{k} {v}' for k, v in sorted(sm['removed_by_skeptic_mark'].items())) or 'none') + '.')
        for status, title in (('removed', 'Removed'), ('changed', 'Ring changed'), ('new', 'New in v2')):
            group = [row for row in changes['rows'] if row['status'] == status]
            lines += ['', f'### {title} ({len(group)})', '']
            lines += [change_line(row) for row in group] or ['- none']
        same = [row for row in changes['rows'] if row['status'] == 'same']
        lines += ['', f'### Kept with the same ring ({len(same)}; v1 # -> v2 #)', '',
                  ', '.join(f"{row['v1']}->{row['v2']}" for row in same) or 'none']
    lines += ['', '## Counts', '']
    for k, v in counts.items():
        text = ', '.join(f"{a.replace('_', ' ')} {b}" for a, b in v.items()) if isinstance(v, dict) else v
        lines.append(f"- {k.replace('_', ' ')}: {text}")
    lines += ['', '## What each kind of line proposes', '',
              '- **canopy completion**: a roof outline that stops along a tree canopy is continued along the roof\'s own two visible '
              'eave lines until they meet (or join, for one straight eave), only inside the canopy that hides the corner (v2).',
              '- **copy un-carve**: the same roof detected in another frame (no coarser than 1.25x this one), moved through a verified '
              'image registration, reaches further under the tree; the part under the tree is added, and only when the result adds '
              'no spike or sliver (v2).',
              '- **frame notch filled**: a narrow slit where the outline dips in from the window frame and comes back (the 1bac '
              'spike) is closed back onto the frame line (unchanged from v1).',
              '- **body carry**: the roof is cut by this window\'s frame, and another frame at this pixel size or finer holds the '
              'whole roof; the proposal takes the in-frame part from the whole-roof outline and records where the whole roof is.',
              '', '## Proposals']
    current = None
    for x in rest:
        section = {'canopy_completion': 'Canopy completions', 'copy_uncarve': 'Copy un-carves', 'spike_fill': 'Frame notches',
                   'spike_tab_cut': 'Frame tabs', 'body_carry': 'Body carries'}[x['type']]
        if section != current:
            lines += ['', f'### {section}', '']
            current = section
        lines.append(line(x))
    lines.append('')
    path.write_text('\n'.join(lines))


def build_marks_v2(out, numbered, held, counts, changes):
    write_marks_v2(out/'MARKS-cuts-v2.md', numbered, {k: v for k, v in counts.items() if k in (
        'windows', 'proposals', 'proposals_by_type', 'proposals_by_tier', 'held_by_type')}, named_notes(numbered, held), changes,
        [strip(x) for x in held])


def strip(rec):
    return {k: v for k, v in rec.items() if not k.startswith('_')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--windows', nargs='*')
    parser.add_argument('--skip-renders', action='store_true')
    parser.add_argument('--skip-frames', action='store_true', help='no other-frame index, copies or body carry')
    parser.add_argument('--design', choices=('v1', 'v2'), default='v1', help='v1 = DESIGN (reproducible), v2 = DESIGN_V2')
    parser.add_argument('--out', type=Path, default=None, help='default prototype/ (v1) or prototype-v2/ (v2)')
    parser.add_argument('--run', type=Path, default=None, help='heartbeat folder; default cutfix/ (v1) or cuts-v2/ (v2)')
    parser.add_argument('--v1', type=Path, default=OUT/'proposals.json', help='v2 only: the v1 proposals.json to compare with')
    parser.add_argument('--marks-only', action='store_true', help='rewrite MARKS-cuts-<design>.md from proposals.json only')
    args = parser.parse_args(argv)
    v2 = args.design == 'v2'
    design = DESIGN_V2 if v2 else DESIGN
    version = design['version']
    out = args.out or (OUT_V2 if v2 else OUT)
    run = args.run or (RUN_V2 if v2 else RUN)
    if args.marks_only:
        if v2:
            data = load_json(out/'proposals.json')
            build_marks_v2(out, data['proposals'], data['held'], data['counts'], data.get('changes_vs_v1'))
        else:
            marks_only(out)
        return
    warnings.filterwarnings('ignore', category=RuntimeWarning)
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def beat(message):
        heartbeat(message, run=run)
    beat(f'{version} start')
    windows = [p for p in population() if not args.windows or p.name in args.windows]
    census = census_rows()
    index = measurer = None
    if not args.skip_frames:
        frames = {}
        for p in windows:
            latest = load_json(p/'latest.json')
            w, h = latest['image_width'], latest['image_height']
            frames[p.name] = Polygon(pixels_to_mercator([[0, 0], [w, 0], [w, h], [0, h]], latest))
        index = FrameIndex(frames)
        measurer = Measurer(sr.load_design(ROOT))
        beat(f'frame index: scanned {index.scanned}, scenes {len(index.scenes)}, outlines {len(index.entries)} '
             f'({time.time()-started:.0f}s)')
    all_records, all_held, tallies, inputs = [], [], Counter(), []
    contexts = {}
    for i, path in enumerate(windows):
        ctx, records, held, t = analyse_window(path, index, measurer, census, design=design, frames=not args.skip_frames)
        all_records.extend(records)
        all_held.extend(held)
        tallies.update(t)
        inputs.append({'window': ctx['window'], 'latest_sha256': file_sha(path/'latest.json'),
                       'review_sha256': file_sha(path/'review.json') if (path/'review.json').exists() else None,
                       'review_revision': (ctx['review'] or {}).get('revision'), 'layer': ctx['layer']})
        contexts[ctx['window']] = path
        beat(f'window {i+1}/{len(windows)} {ctx["window"]} proposals {len(records)} held {len(held)} '
             f'measured {measurer.measured if measurer else 0} ({time.time()-started:.0f}s)')
        if measurer is not None:
            measurer.release()
    numbered = number_records(all_records)
    changes = compare_v1(numbered, [strip(x) for x in all_held], args.v1, design) if v2 else None
    render_paths = []
    if not args.skip_renders:
        by_window = defaultdict(list)
        for x in numbered:
            by_window[x['window']].append(x)
        for k, (w, items) in enumerate(sorted(by_window.items())):
            ctx = window_context(contexts[w])
            for x in items:
                p = out/'renders'/w/f"{x['number']:03d}-{x['type']}-{x['object_id']}.png"
                render_record(x, ctx, p, x['number'], measurer)
                x['render'] = str(p.relative_to(out))
                render_paths.append(p)
            if measurer is not None:
                measurer.release()
            if k % 5 == 0:
                beat(f'renders {len(render_paths)}/{len(numbered)} ({time.time()-started:.0f}s)')
        contact_sheet(render_paths, out/'renders'/'contact-sheet.png')
        named_paths = [out/x['render'] for x in numbered if x['named']]
        contact_sheet(named_paths, out/'renders'/'contact-named.png', columns=2)
    for x in numbered:
        x.setdefault('render', None)
    counts = {
        'windows': len(windows),
        'proposals': len(numbered),
        'proposals_by_type': dict(Counter(x['type'] for x in numbered)),
        'proposals_by_tier': dict(Counter(x['apply_tier'] for x in numbered)),
        'held_by_type': dict(Counter(x['type'] for x in all_held)),
        'held_reasons': {t: dict(Counter(reason for x in all_held if x['type'] == t
                                         for reason in (x['detail'].get('refusals') or [x['detail'].get('reason')])).most_common())
                         for t in sorted({x['type'] for x in all_held})},
        'tallies': dict(sorted(tallies.items())),
        'transforms_measured': measurer.measured if measurer else 0,
        'transforms_verified': sum(1 for v in (measurer.transforms.values() if measurer else []) if v['verified']),
        'frame_index': {'scenes_scanned': index.scanned, 'registered_scenes_meeting_windows': len(index.scenes),
                        'outlines': len(index.entries)} if index else None,
    }
    header = {'version': version, 'code_sha256': CODE_SHA256, 'generated_at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'runtime_s': round(time.time()-started, 1), 'design': design, 'inputs': inputs,
              'census': rel(CENSUS) if CENSUS.exists() else None, 'counts': counts}
    if v2:
        header.update(skeptic_wrong_v1=SKEPTIC_WRONG_V1, skeptic_marks_v1=SKEPTIC_MARKS_V1, changes_vs_v1=changes)
        if changes is not None:
            (out/'changes.json').write_text(json.dumps(changes, indent=1, default=str))
    (out/'proposals.json').write_text(json.dumps({**header, 'proposals': [strip(x) for x in numbered],
                                                  'held': [strip(x) for x in all_held]}, indent=1, default=str))
    with open(out/'proposals.jsonl', 'w') as handle:
        for x in numbered:
            row = {'number': x['number'], 'type': x['type'], 'window': x['window'], 'object_id': x['object_id'],
                   'proposal': x['proposal'], 'render': x['render']}
            if v2:
                row['v1'] = x.get('v1')
            handle.write(json.dumps(row, default=str)+'\n')
    if v2:
        build_marks_v2(out, numbered, all_held, counts, changes)
    else:
        build_marks(out, numbered, all_held, counts)
    beat(f'{version} done {time.time()-started:.0f}s proposals {len(numbered)} held {len(all_held)}')
    print(json.dumps({'version': version, 'windows': len(windows), 'proposals': len(numbered), 'by_type': counts['proposals_by_type'],
                      'held': counts['held_by_type'], 'changes': (changes or {}).get('summary', {}).get('by_status'),
                      'runtime_s': round(time.time()-started, 1)}))

if __name__ == '__main__':
    main()
