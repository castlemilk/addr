"""Recapture carry path, dry run: the georeferenced frame transform between two scene snapshots, its measured image
residual against the pre-registered design, a byte-level verifier, an offline plan over a cohort and a pilot on frame
pairs that already exist.

Identity. A recaptured lot is a new scene lot-<address_id> (scene_lot.lot_source --carry-from); the source scene, its
latest.json and its review.json are never written (save_scene recomputes model_signature from content, so any write
to an old latest.json would stale its review). Object ids hash the image (scene_extract:
sha256(f'{image_hash}:{kind}:{raw_coordinates}')[:16]), so no id survives a new image and every match across frames is
geometric: best IoU per kind.

frame_transform(source, target) is pure. metre_offset is the source's local-metre origin (its centroid) expressed in
the target's local metres: Web-Mercator scaled by cos(target centroid latitude), exactly the metre step of
scene_addresses.image_projection(target). pixel_affine maps source px to target px with the repo's row-vector 3x2
convention ([x, y, 1] @ [[a, b], [c, d], [tx, ty]]) and equals target.meters_to_pixel o (+metre_offset) o
source.pixel_to_meters. project_object moves metre fields by +metre_offset (z unchanged) and pixel fields through
pixel_affine. Two approximations are measured and recorded by the pilot, never hidden: the translation neglects the
cos(lat) ratio between the two centroids (about 1e-5 relative per 100 m of centroid separation near -37.8 deg), and
cohort frames (registration 'fitted', roof_training_250-*) carry affines fitted by least squares to rooflines.Frame
metres, an equirectangular frame at 111,320 m per degree that differs from Web-Mercator local metres by about 1e-5
relative over 30 m (projection_note).

residual(source_png, target_png, transform, target_m_per_px) warps the source PNG into the target frame (PIL affine,
bilinear; a Gaussian anti-alias of half a target pixel first when the warp shrinks), takes the 2 px blurred Sobel
gradient magnitude of both (teacher_judges.registration_image), locates the integer shift by a phase correlation on the
overlap (eroded by EDGE_PX so the warp's own frame edge is never correlated; Hann window; whitening regularised) and
refines it at the gradient-NCC peak within +-REFINE_PX (matchTemplate + parabola, as teacher_judges.ncc_shift) for
shift_px (the target's content lies at the transform's prediction plus shift_px, in target px) and shift_m
(|shift_px| times the target pixel spacing), and measures ncc, the zero-mean normalised cross-correlation of the two
magnitudes on the overlap after removing the measured shift (DESIGN.json amendment of 12:39:58Z, before any run). verdict() applies DESIGN.json: overlap.fraction >= min_overlap_fraction and shift_m <= max_shift_m and
ncc >= min_ncc. Every figure is a measured agreement between two frames against pre-registered values: never
accuracy, never a proof of identical pixels.

Subject recaptures (DESIGN.json subject_recapture, dated amendment of 2026-10-01; reviewer authorisation "yes to
defaults", item 5). A scene_focus window is centred on the house/lot the frame-cut object belongs to, so the share of
SOURCE pixels inside it is often small although the overlapping region is large: when a transform carries role
SUBJECT_ROLE ('frame_cut_target'; frame_transform / measured_transform role=...), verdict() replaces the fraction rule
by an absolute floor on the overlapping REGION: overlap.region_px.side_px (the source footprint inside the target frame,
eroded by EDGE_PX exactly as residual() erodes its cover mask; the smaller side of its bounding box, in TARGET px) must
reach subject_recapture.min_overlap_px; shift_m and ncc keep their rules; the fraction is recorded beside the region;
the transform's design block then carries subject_recapture and the sha of the amended DESIGN.json. A transform with no
role (scene_lot --carry-from, the pilot pairs) keeps the fraction rule unchanged, and a design without the floor never
verifies a subject recapture (no fallback). overlap() records region_px for every transform.

CLI (no network, no SAM3; nothing under data/scenes/ is ever written):
  --verify-source data/scenes/<src>/models/<sig>.json --verify-target data/scenes/<dst>/models/<sig>.json
      re-hashes both snapshots and both PNGs, recomputes the transform, re-warps and re-measures the residual from the
      bytes; exit 1 with stderr 'Recaptured frame does not agree with the source frame' (then 'reason:' lines).
  plan --cohort roof_training_250 --offline --out data/reviews/eyes/recapture/plan-<date>.jsonl
      one row per cohort scene (lot_frame offline, parcels_unavailable when nothing is frozen, residual 'unmeasured');
      every row repeats plan_totals, computed once at write time, so the file stays one row per scene.
  pilot --pairs auto --out data/reviews/eyes/recapture/pilot-<date>.json
      the 8 cohort->house z20 pairs (model objects of the two frames; no cohort review exists on them) and every cohort
      scene holding a human decision whose centroid lies inside a registered mooroolbark-* z19 frame; every reviewed
      cohort scene left out is listed in selection.excluded with its reason.

A human decision is a saved confirmed, needs_adjustment or rejected status on the reviewer's own object (no
review_authority blocker, no assistant note). A candidate is undecided and a superseded member is lineage: the plan's
human_decisions and the pilot's target_human_status never include either (statuses_excluding_machine_pending_agent and
target_review_status keep the raw tallies). Two machine-only rules coexist and both are named: the counting rule's
machine_only (review_authority_blocker 'machine_label') and the carry's drop (the review app's machineOnly: machine:* and
not human_confirmed); machine_confirmed_by_human counts where they differ.

Task lines for the integrator (not added here): recapture:plan and recapture:pilot run the two subcommands above
(read-only); recapture:capture = `scene_lot --address <id> --capture --carry-from <scene>` guarded by CONFIRM=1.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import JOIN_STYLE, Polygon, box

from . import design_chain, scene_lot
from .review_authority import review_authority_blocker
from .scene_addresses import WORLD
from .scene_lot_tags import registration
from .scene_region import region_frame
from .scene_self_review import GEOMETRY_ERRORS, validate_id
from .tiles import lonlat_to_tile_xy

VERSION = 'frame-transform-v1'
METHOD = 'georeferenced-affine'
RESIDUAL_METHOD = 'gradient-ncc+phase-correlation'
FAILURE = 'Recaptured frame does not agree with the source frame'
DESIGN_PATH = Path('data/reviews/eyes/recapture/DESIGN.json')
DESIGN_VERSION = 'recapture-design-v1'
DESIGN_KEYS = ('max_shift_m', 'min_ncc', 'min_overlap_fraction', 'match_iou', 'close_iou')
SUBJECT_KEY = 'subject_recapture'     # DESIGN.json block (dated amendment of 2026-10-01): the absolute overlap floor for subject windows
SUBJECT_ROLE = 'frame_cut_target'     # frame_transform.role of a scene_focus window: the floor applies; no role = the fraction rule
PLAN_VERSION = 'recapture-plan-v1'
PILOT_VERSION = 'recapture-pilot-v1'
STATUSES = ('candidate', 'confirmed', 'needs_adjustment', 'rejected', 'superseded')
DECISIONS = ('confirmed', 'needs_adjustment', 'rejected')
AGENT_NOTES = re.compile(r'^Assistant (visually traced|image review)', re.I)   # an assistant's review: an observation
METRE_FIELDS = ('roof_parts', 'roof_lines', 'roof_feedback', 'roof_annotation', 'roof_line_edits')
BLUR_PX = 2.        # teacher_judges.REG['blur_px']: gradient magnitude softened by 2 px
EDGE_PX = 10        # overlap erosion: Sobel + the 2 px blur (radius 8) never see the warp's frame edge
MIN_SIDE_PX = 24    # a smaller overlap is unmeasured, not verified
REFINE_PX = 3       # the NCC refinement searches +-3 px around the phase-correlation peak
AGREE = 1e-6        # recorded and recomputed transforms agree to this (W4-TS uses the same tolerance)
EQUIRECT_M_PER_DEG = 111_320.   # rooflines.Frame / addresses.METERS_PER_DEGREE
COHORT = 'roof_training_250'
REGISTERED_PREFIX = 'mooroolbark-'
HOUSE_PAIRS = (29772, 30227, 30237, 30280, 30288, 30289, 30295, 30296)
FIELDWORK_MIRROR = Path('data/fieldwork-sync/mooroolbark/mirror.json')
WORDING = {
    'agreement': ("Every figure here is agreement between two frames' objects, or between a re-projected human outline "
                  'and a detection, never accuracy.'),
    'residual': ('The residual is a measured agreement against the pre-registered design values in DESIGN.json, never a '
                 'proof of identical pixels.'),
    'z19': ('z19 (0.236 m/px) residuals from the cohort->mooroolbark pairs do not transfer 1:1 to a z20 (0.118 m/px) '
            'recapture: that grid is twice as fine and a new capture may come from another flight.'),
    'house': ('The 8 house pairs compare model objects of the two frames only (no cohort review.json exists on them); the '
              'house-side human status (target_human_status) is a saved human decision on the matched house object, and a '
              'matched candidate is undecided and reads null (target_review_status keeps the raw saved status).'),
    'decisions': ('A human decision is a saved confirmed, needs_adjustment or rejected status with no review_authority blocker '
                  '(machine_label, fieldwork_pending) and no assistant note; a candidate is undecided and a superseded member '
                  'is lineage, never a decision.'),
    'cohort': ('The cohort crops are a Lanczos upsample of a z19 composite (median of up to two providers by design; 0.236 m/px '
               'native): their fine pixel spacing is not native detail. Cohort latest.json records no imagery provenance and '
               'rooflines falls back to a single provider when the composite fails, so the provider count is the pipeline '
               'design, not a per-scene fact.'),
    'signed': ('shift_east_north_m is where the registered frame shows the content relative to where the transform puts it '
               "(metres east, north, read through the target frame's own axes); a direction shared across pairs is consistent "
               'with a registration offset between the fitted cohort frames and the registered frames (or a pixel-centre '
               'convention difference); its cause is not established here: reported, never corrected (the design is not '
               'tuned on the pilot).'),
    'nothing': ('Nothing was captured, carried or promoted and nothing was written under data/scenes/; the design was '
                'written before this pilot and is not tuned on it; hints are never labels.'),
}
DEFINITIONS = {
    'human_decisions': ('objects whose status is confirmed, needs_adjustment or rejected, outside machine_only, '
                        'fieldwork_pending and agent_authored; candidates and superseded members are never counted'),
    'statuses_excluding_machine_pending_agent': ('all five statuses over the objects outside machine_only, fieldwork_pending '
                                                 'and agent_authored: a reconciliation tally, not a decision count'),
    'machine_only': ("the counting rule: review_authority_blocker == 'machine_label' (any provenance.source machine:*, "
                     'provenance.human_confirmed ignored)'),
    'machine_confirmed_by_human': ('machine_only objects with provenance.human_confirmed set: the carry follows the review '
                                   "app's rule (src/lib/scene-feedback.ts machineOnly: machine:* and not human_confirmed), so "
                                   'these are carried as needs_adjustment while the counting rule keeps them in machine_only'),
    'dropped_machine_only': "changed objects the carry drops under the review app's machineOnly rule",
    'target_human_status': 'the matched target object\'s saved status only when it is a human decision (see human_decisions)',
    'target_review_status': 'the matched target object\'s raw saved status, whoever wrote it',
    'target_review_authority': "why the matched saved object is not the reviewer's own (machine_label, fieldwork_pending, "
                               'agent_authored) or null',
}
# Reporting-only changes after DESIGN.json was written: none changes a DESIGN value, threshold, verdict rule or measured
# field. Times are UTC tool-call timestamps from the implementing and fixing sessions (the implementer's session log,
# subagents/workflows/wf_b44b0ddb-aa6/agent-ae3f2a2d8e8f70a03.jsonl, and /private/tmp/w4_smoke*.py file birth times).
REPORTING_CHANGES = (
    {'at': '2026-09-30T12:43:55Z', 'change': 'kind_not_scanned_in_target: an object whose kind the target never scanned or '
     'holds carries no IoU',
     'after': ('a read-only smoke after the DESIGN amendment of 12:39:58Z: 12:42:26Z /private/tmp/w4_smoke.py pilot_pair '
               'roof_training_250-30280 -> house-30280 and roof_training_250-30371 -> mooroolbark-50011498-17-118442-80417 '
               '(overlap 0: not the frame holding 30371) plus plan_row on 30371 and 25007; 12:42:54Z /private/tmp/w4_smoke2.py '
               'roof_training_250-30371 -> mooroolbark-50011498-17-118443-80417 (the only registered frame holding its '
               "centroid); 12:43:12Z that frame's scans read building, tree and solar_panel only. The only earlier "
               'scene_recapture measurements (12:35:45Z, 12:38:10Z) used synthetic test frames')},
    {'at': '2026-09-30T12:54:44Z', 'change': 'shift_east_north_m, its medians and IQR, per-row target_status_of_matched',
     'after': 'run 1 (pilot computed 2026-09-30T12:49:51Z) showed one shift direction across pairs'},
    {'at': '2026-09-30T13:41:49Z', 'change': ('verifier findings: human_decisions counts confirmed / needs_adjustment / '
                                              'rejected only (the five-status tally is statuses_excluding_machine_pending_agent); '
                                              'target_human_status only for a saved human decision (target_review_status and '
                                              'target_review_authority keep the raw facts); selection.excluded with reasons; '
                                              'machine_confirmed_by_human; iou_median_unscanned_as_zero beside iou_median; '
                                              'shift_east_north_m through the target affine (identical for region targets); '
                                              'cohort and signed wording; replaces_run on the plan'),
     'after': ('runs 1 and 2 (pilots computed 2026-09-30T12:49:51Z and 12:55:48Z); with the DESIGN counting_rule amendment of '
               '2026-09-30T13:41:36Z (previous sha ed1ec8ac..., thresholds unchanged)')},
    {'at': '2026-09-30T13:46:39Z', 'change': ('wording only: the two computed sentences read "label: number" (no plural slips), '
                                              'name what they count (cohort model objects, re-projected human outlines) and say '
                                              'so when every kind was scanned'),
     'after': 'run 3 (pilot computed 2026-09-30T13:43:37Z), whose figures are unchanged'},
)
MODULE_COPIES = ("run 1's module (code_sha256 e2c390682397...) is not preserved, only its outputs and log "
                 "(.run/w4py-backup/*run1*); run 2's module is .run/w4py-backup/scene_recapture.run2-819a4e6a.py with "
                 "scene_lot.run1-run2-3ba94f69.py; run 3's is scene_recapture.run3-af1bbfaa.py with scene_lot.run3-05fb139e.py; "
                 'every later run copies both modules beside its outputs (.run/w4py-backup/*.run<N>-<sha8>.py) before it runs')


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def scene_lot_sha256():
    """sha256 of scene_lot.py, whose lot_frame and cohort wording this module's outputs embed."""
    return hashlib.sha256(Path(scene_lot.__file__).read_bytes()).hexdigest()


def _now():
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _median(values):
    values = [v for v in values if v is not None]
    return round(float(np.median(values)), 4) if values else None


def _share(count, total):
    return round(count/total, 4) if total else None


# --- affines --------------------------------------------------------------------------------------------------------

def _affine(matrix):
    array = np.asarray(matrix, dtype=float)
    if array.shape != (3, 2) or not np.isfinite(array).all():
        raise ValueError('an affine is a finite 3x2 matrix [[a, b], [c, d], [tx, ty]]')
    return array


def apply_affine(points, affine):
    """[x, y, 1] @ affine for every point, element-wise (the same arithmetic whatever the array size)."""
    p = np.asarray(points, dtype=float).reshape(-1, 2)
    a = _affine(affine)
    return np.stack([p[:, 0]*a[0, 0]+p[:, 1]*a[1, 0]+a[2, 0], p[:, 0]*a[0, 1]+p[:, 1]*a[1, 1]+a[2, 1]], axis=1)


def invert_affine(affine):
    full = np.eye(3)
    full[:, :2] = _affine(affine)
    return np.linalg.inv(full)[:, :2]


def compose(first, second):
    """The affine that applies `first`, then `second` (row vectors)."""
    a, b = _affine(first), _affine(second)
    return np.vstack([a[:2] @ b[:2], a[2] @ b[:2]+b[2]])


def pixel_spacing(scene):
    """Metres per image pixel of a frame (the square root of the affine's area scale)."""
    return math.sqrt(abs(float(np.linalg.det(_affine(scene['pixel_to_meters'])[:2]))))


def east_north(shift_px, scene):
    """A displacement in a frame's pixels as metres (east, north) through that frame's own axes: the linear part of its
    pixel_to_meters, so the sign follows the frame (region frames: pixel y points south; a fitted frame may be flipped
    or turned). Its length equals |shift_px| times pixel_spacing for any conformal frame (every region frame)."""
    if shift_px is None:
        return None
    east, north = np.asarray(shift_px, dtype=float) @ _affine(scene['pixel_to_meters'])[:2]
    return [_round(east), _round(north)]


# --- frame transform --------------------------------------------------------------------------------------------

def local_metres(scene, lon, lat):
    """(x east, y north) of a lon/lat in a scene's local metres: scene_addresses.image_projection before its affine."""
    lon0, lat0 = scene['centroid']['lon'], scene['centroid']['lat']
    scale = WORLD*math.cos(math.radians(lat0))
    y0 = math.asinh(math.tan(math.radians(lat0)))/(2*math.pi)
    return (lon-lon0)/360*scale, (math.asinh(math.tan(math.radians(lat)))/(2*math.pi)-y0)*scale


def metre_offset(source, target):
    """The source's local-metre origin (its centroid) in the target's local metres."""
    x, y = local_metres(target, source['centroid']['lon'], source['centroid']['lat'])
    return [float(x), float(y)]


def _footprint_inside(source, target, affine):
    """The source frame's footprint through the affine, clipped to the target frame (target px)."""
    w, h = source['image_width'], source['image_height']
    footprint = Polygon(apply_affine([[0, 0], [w, 0], [w, h], [0, h]], affine))
    return footprint.intersection(box(0, 0, target['image_width'], target['image_height']))


def overlap_region(source, target, affine, erode_px=EDGE_PX):
    """The overlapping region residual() measures on, in TARGET px: the source footprint inside the target frame eroded by
    erode_px (as residual() erodes its cover mask by EDGE_PX), its bounding-box side (the smaller of width and height) and
    its area. side_px is what DESIGN.json subject_recapture.min_overlap_px gates for a subject recapture."""
    inside = _footprint_inside(source, target, affine)
    eroded = inside.buffer(-erode_px, join_style=JOIN_STYLE.mitre) if not inside.is_empty else inside
    if eroded.is_empty or eroded.area <= 0:
        return {'side_px': 0., 'area_px': 0, 'eroded_by_px': erode_px}
    x0, y0, x1, y1 = eroded.bounds
    return {'side_px': round(float(min(x1-x0, y1-y0)), 1), 'area_px': int(round(eroded.area)), 'eroded_by_px': erode_px}


def overlap(source, target, affine):
    """Source pixels whose footprint lands inside the target frame, their share of the source frame (fraction: the
    same-address rule) and the eroded overlapping region in target px (region_px: the subject rule)."""
    w, h = source['image_width'], source['image_height']
    inside = _footprint_inside(source, target, affine).area
    scale = abs(float(np.linalg.det(_affine(affine)[:2])))
    pixels = inside/scale if scale > 0 else 0.
    return {'source_px_inside_target': int(round(pixels)), 'fraction': round(min(1., pixels/(w*h)), 6),
            'region_px': overlap_region(source, target, affine)}


def frame_transform(source, target, *, role=None):
    """The georeferenced transform from source to target frame (pure; residual unmeasured, not verified). role marks a
    subject recapture (SUBJECT_ROLE: scene_focus windows, gated by the region floor); None is a same-address recapture."""
    if role not in (None, SUBJECT_ROLE):
        raise ValueError(f'a frame_transform role is {SUBJECT_ROLE!r} or None (got {role!r})')
    offset = metre_offset(source, target)
    affine = compose(compose(source['pixel_to_meters'], [[1., 0.], [0., 1.], offset]), target['meters_to_pixel'])
    return {'version': VERSION, 'method': METHOD, 'source_scene_id': source['id'],
            'source_model_signature': source.get('model_signature'), 'source_image_sha256': source.get('image_sha256'),
            'source_size': [int(source['image_width']), int(source['image_height'])],
            'source_registration': registration(source), 'metre_offset': offset, 'pixel_affine': affine.tolist(),
            'overlap': overlap(source, target, affine), 'residual': None, 'design': None, 'verified': False, 'role': role}


def projection_note(source, target=None):
    """Measured size of the two approximations the translation-only transform makes, over the source frame's corners."""
    w, h = source['image_width'], source['image_height']
    corners = apply_affine([[0, 0], [w, 0], [w, h], [0, h]], source['pixel_to_meters'])
    reach = float(np.hypot(corners[:, 0], corners[:, 1]).max())
    note = {'source_reach_m': round(reach, 2)}
    if registration(source) == 'fitted':
        lon0, lat0 = source['centroid']['lon'], source['centroid']['lat']
        worst_rel, worst_abs = 0., 0.
        for x, y in corners:
            lon = lon0+x/(EQUIRECT_M_PER_DEG*math.cos(math.radians(lat0)))
            mx, my = local_metres(source, lon, lat0+y/EQUIRECT_M_PER_DEG)
            gap = math.hypot(mx-x, my-y)
            worst_abs, worst_rel = max(worst_abs, gap), max(worst_rel, gap/max(math.hypot(x, y), 1e-9))
        note['fitted_equirectangular_vs_web_mercator'] = {'max_relative': float(f'{worst_rel:.2e}'), 'max_m': float(f'{worst_abs:.2e}')}
    if target is not None:
        ratio = math.cos(math.radians(target['centroid']['lat']))/math.cos(math.radians(source['centroid']['lat']))-1
        note['centroid_scale_ratio_minus_1'] = float(f'{ratio:.2e}')
        note['neglected_scale_max_m'] = float(f'{abs(ratio)*reach:.2e}')
    return note


# --- objects ----------------------------------------------------------------------------------------------------

def _px(points, affine):
    return apply_affine([p[:2] for p in points], affine).tolist() if len(points) else []


def _m(points, offset):
    return [[p[0]+offset[0], p[1]+offset[1], *p[2:]] for p in points]


def project_object(obj, transform):
    """A copy of obj in the target frame: pixel fields through pixel_affine, metre fields by +metre_offset (z kept).

    The contract's fields: pixel ring, holes, roof_feedback.outline, roof_line_edits.outline,
    boundary_refinement.raw_ring|raw_holes; metre roof_line_edits.vertices, roof_annotation.vertices,
    roof_parts[].ring_meters|ring, roof_lines[].line_meters. Beyond that list, because real scenes carry them and a
    stale copy would contradict the moved ring: pixel roof_parts[].ring_pixels, roof_lines[].line_pixels,
    boundary_refinement.assembly.components[].ring|holes; metre roof_parts[].coords, roof_parts[].triangles ([x, y, z]),
    roof_parts[].gable_ends (edges), roof_line_edits.baseline.vertices, roof_feedback.linework.vertices; the roof plane
    z = a x + b y + c keeps every height (c' = c - a dx - b dy); model.footprint_area_px scales by the affine's area
    factor. Lon/lat, heights and metre dimensions are frame-free and stay. Status and every decision field stay too: the
    status policy belongs to the carry, not to the projection.
    """
    out = copy.deepcopy(obj)
    affine, offset = transform['pixel_affine'], transform['metre_offset']
    model = out.get('model')
    if isinstance(model, dict) and isinstance(model.get('footprint_area_px'), (int, float)) and not isinstance(model['footprint_area_px'], bool):
        model['footprint_area_px'] = round(model['footprint_area_px']*abs(float(np.linalg.det(_affine(affine)[:2]))), 2)
    if out.get('ring') is not None:
        out['ring'] = _px(out['ring'], affine)
    if out.get('holes') is not None:
        out['holes'] = [_px(h, affine) for h in out['holes']]
    for field in ('roof_feedback', 'roof_line_edits'):
        value = out.get(field)
        if not isinstance(value, dict):
            continue
        outline = value.get('outline')
        if isinstance(outline, dict):
            if outline.get('ring') is not None:
                outline['ring'] = _px(outline['ring'], affine)
            if outline.get('holes') is not None:
                outline['holes'] = [_px(h, affine) for h in outline['holes']]
        for holder in (value, value.get('baseline'), value.get('linework')):
            if isinstance(holder, dict) and holder.get('vertices') is not None:
                holder['vertices'] = _m(holder['vertices'], offset)
    annotation = out.get('roof_annotation')
    if isinstance(annotation, dict) and annotation.get('vertices') is not None:
        annotation['vertices'] = _m(annotation['vertices'], offset)
    for part in out.get('roof_parts') or []:
        for key in ('ring_meters', 'ring', 'coords'):
            if part.get(key) is not None:
                part[key] = _m(part[key], offset)
        for key in ('triangles', 'gable_ends'):
            if part.get(key) is not None:
                part[key] = [_m(points, offset) for points in part[key]]
        plane = part.get('plane')
        if isinstance(plane, dict) and all(isinstance(plane.get(k), (int, float)) for k in 'abc'):
            plane['c'] = plane['c']-plane['a']*offset[0]-plane['b']*offset[1]
        if part.get('ring_pixels') is not None:
            part['ring_pixels'] = _px(part['ring_pixels'], affine)
    for line in out.get('roof_lines') or []:
        if line.get('line_meters') is not None:
            line['line_meters'] = _m(line['line_meters'], offset)
        if line.get('line_pixels') is not None:
            line['line_pixels'] = _px(line['line_pixels'], affine)
    refinement = out.get('boundary_refinement')
    if isinstance(refinement, dict):
        if refinement.get('raw_ring') is not None:
            refinement['raw_ring'] = _px(refinement['raw_ring'], affine)
        if refinement.get('raw_holes') is not None:
            refinement['raw_holes'] = [_px(h, affine) for h in refinement['raw_holes']]
        assembly = refinement.get('assembly')
        for component in (assembly.get('components') or []) if isinstance(assembly, dict) else []:
            if isinstance(component, dict) and component.get('ring') is not None:
                component['ring'] = _px(component['ring'], affine)
            if isinstance(component, dict) and component.get('holes') is not None:
                component['holes'] = [_px(h, affine) for h in component['holes']]
    return out


def _polygon(obj):
    try:
        polygon = Polygon(obj['ring'], obj.get('holes') or [])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except GEOMETRY_ERRORS:
        return None
    return None if polygon.is_empty or polygon.area <= 0 else polygon


def _by_kind(objects):
    """{kind: [(id, polygon)]} built once per frame."""
    shapes = {}
    for obj in objects:
        polygon = _polygon(obj)
        if polygon is not None:
            shapes.setdefault(obj.get('kind'), []).append((obj['id'], polygon))
    return shapes


def _best(polygon, candidates):
    """(best IoU, id) of a polygon against (id, polygon) candidates; (0, None) when nothing overlaps."""
    best, best_id = 0., None
    for oid, other in candidates:
        if not polygon.intersects(other):
            continue
        union = polygon.union(other).area
        iou = polygon.intersection(other).area/union if union > 0 else 0.
        if iou > best:
            best, best_id = iou, oid
    return best, best_id


def _agent(obj):
    return bool(AGENT_NOTES.match(str(obj.get('notes') or '')))


def _human(obj):
    """A reviewer's own object: no review_authority blocker and no assistant-authored note."""
    return review_authority_blocker(obj) is None and not _agent(obj)


def _decided(obj):
    """A saved human decision: confirmed, needs_adjustment or rejected on a reviewer's own object. A candidate is
    undecided and a superseded member is lineage: neither is ever reported as a decision."""
    return obj.get('status') in DECISIONS and _human(obj)


def _authority(obj):
    """Why a saved object is not the reviewer's own: the review_authority blocker, 'agent_authored', or None."""
    return review_authority_blocker(obj) or ('agent_authored' if _agent(obj) else None)


def _machine_only(obj):
    """The review app's machine-only rule (scene-feedback.ts): machine provenance nobody confirmed; dropped by any carry.

    The counting rule differs on purpose (DESIGN counting_rule and its amendment): machine_only counts every
    review_authority_blocker 'machine_label' object, so a machine label a reviewer confirmed is counted there and carried
    here; the plan reports those objects as machine_confirmed_by_human.
    """
    provenance = obj.get('provenance')
    return (isinstance(provenance, dict) and str(provenance.get('source') or '').startswith('machine:')
            and not provenance.get('human_confirmed'))


def _machine_confirmed(obj):
    """A machine label (review_authority_blocker 'machine_label') that a reviewer confirmed (provenance.human_confirmed)."""
    provenance = obj.get('provenance')
    return review_authority_blocker(obj) == 'machine_label' and isinstance(provenance, dict) and bool(provenance.get('human_confirmed'))


# --- residual ---------------------------------------------------------------------------------------------------

def _bytes(value):
    return bytes(value) if isinstance(value, (bytes, bytearray)) else Path(value).read_bytes()


def _gray(image):
    import cv2
    return cv2.cvtColor(np.asarray(image.convert('RGB'), dtype=np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)


def _magnitude(gray):
    """teacher_judges.registration_image: Sobel gradient magnitude softened by a BLUR_PX Gaussian."""
    import cv2
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.GaussianBlur(np.hypot(gx, gy), (0, 0), BLUR_PX)


def warp_into(source_image, affine, size):
    """The source image resampled onto the target grid (PIL affine, bilinear) and the mask of target pixels it covers."""
    from PIL import ImageFilter
    inverse = invert_affine(affine)          # target px -> source px; PIL samples the input at (a x + b y + c, d x + e y + f)
    data = (inverse[0, 0], inverse[1, 0], inverse[2, 0], inverse[0, 1], inverse[1, 1], inverse[2, 1])
    shrink = math.sqrt(abs(float(np.linalg.det(_affine(affine)[:2]))))    # target px per source px
    image = source_image.convert('RGB')
    if shrink < 1:
        image = image.filter(ImageFilter.GaussianBlur(radius=.5/shrink))   # half a target pixel of anti-alias
    warped = image.transform(size, Image.Transform.AFFINE, data, resample=Image.Resampling.BILINEAR)
    cover = Image.new('L', source_image.size, 255).transform(size, Image.Transform.AFFINE, data, resample=Image.Resampling.NEAREST)
    return warped, np.asarray(cover) > 0


def _phase_peak(a, b):
    """Integer translation of b's content relative to a's: the peak of a phase correlation whose whitening is
    regularised where the blurred magnitudes carry no signal (cv2.phaseCorrelate's 5x5 centroid read a planted 2 px
    shift as 1.35-1.52 px on these images: its unregularised spectrum is noise above the blur's pass band)."""
    import cv2
    h, w = a.shape
    window = cv2.createHanningWindow((w, h), cv2.CV_64F)
    cross = np.fft.fft2((b-b.mean())*window)*np.conj(np.fft.fft2((a-a.mean())*window))
    magnitude = np.abs(cross)
    surface = np.fft.ifft2(cross/(magnitude+1e-3*magnitude.max())).real
    iy, ix = np.unravel_index(int(np.argmax(surface)), surface.shape)
    return (int(ix-w) if ix > w//2 else int(ix)), (int(iy-h) if iy > h//2 else int(iy)), float(surface[iy, ix])


def _parabolic(values, index):
    """Sub-pixel peak offset from a 3-point parabola (teacher_judges._parabolic), clamped to +-0.5."""
    if index <= 0 or index >= len(values)-1:
        return 0.
    left, centre, right = float(values[index-1]), float(values[index]), float(values[index+1])
    denominator = left-2*centre+right
    return 0. if abs(denominator) < 1e-12 else float(np.clip(.5*(left-right)/denominator, -.5, .5))


def _ncc_peak(a, b, coarse, refine=REFINE_PX):
    """The gradient-NCC peak within +-refine px of the coarse shift, to sub-pixel (matchTemplate + parabola), or None."""
    import cv2
    h, w = a.shape
    reach = max(abs(coarse[0]), abs(coarse[1]))+refine
    if min(h, w)-2*reach < MIN_SIDE_PX:
        return None
    template = np.ascontiguousarray(a[reach:h-reach, reach:w-reach], dtype=np.float32)
    region = np.ascontiguousarray(b, dtype=np.float32)
    result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)      # result[j, i]: template moved by (i-reach, j-reach)
    x0, y0 = coarse[0]+reach-refine, coarse[1]+reach-refine
    local = result[y0:y0+2*refine+1, x0:x0+2*refine+1]
    iy, ix = np.unravel_index(int(np.argmax(local)), local.shape)
    return (coarse[0]-refine+ix+_parabolic(local[iy, :], ix), coarse[1]-refine+iy+_parabolic(local[:, ix], iy))


def residual(source_png, target_png, transform, target_m_per_px, *, detail=False):
    """Measured image agreement of the transform: the shift (phase-correlation peak refined at the gradient-NCC peak),
    in target px and metres, and the NCC after removing it."""
    import cv2
    source_bytes, target_bytes = _bytes(source_png), _bytes(target_png)
    with Image.open(io.BytesIO(source_bytes)) as src, Image.open(io.BytesIO(target_bytes)) as dst:
        source_image, target_image = src.convert('RGB'), dst.convert('RGB')
    result = {'method': RESIDUAL_METHOD, 'warped_from': _sha(source_bytes), 'shift_px': None, 'shift_m': None, 'ncc': None, 'samples': 0}
    extra = {'response': None, 'box_px': None, 'ncc_at_zero_shift': None}
    warped, cover = warp_into(source_image, transform['pixel_affine'], target_image.size)
    kernel = np.ones((2*EDGE_PX+1, 2*EDGE_PX+1), np.uint8)
    core = cv2.erode(cover.astype(np.uint8), kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    ys, xs = np.nonzero(core)
    if not len(xs) or min(ys.max()+1-ys.min(), xs.max()+1-xs.min()) < MIN_SIDE_PX:
        result['samples'] = int(core.sum())
        return (result, extra) if detail else result
    y0, y1, x0, x1 = int(ys.min()), int(ys.max())+1, int(xs.min()), int(xs.max())+1
    inside = core[y0:y1, x0:x1]
    a = _magnitude(_gray(warped))[y0:y1, x0:x1]
    b = _magnitude(_gray(target_image))[y0:y1, x0:x1]
    extra['box_px'] = [x0, y0, x1, y1]
    result['samples'] = int(inside.sum())
    if a[inside].std() < 1e-3 or b[inside].std() < 1e-3:       # featureless: nothing to register
        return (result, extra) if detail else result
    a = np.where(inside, a, a[inside].mean()).astype(np.float32)
    b = np.where(inside, b, b[inside].mean()).astype(np.float32)
    coarse_x, coarse_y, response = _phase_peak(a.astype(np.float64), b.astype(np.float64))
    peak = _ncc_peak(a, b, (coarse_x, coarse_y))
    extra['phase_peak_px'] = [coarse_x, coarse_y]
    if peak is None or not all(math.isfinite(v) for v in (*peak, response)):
        return (result, extra) if detail else result
    dx, dy = peak                                              # b's content = a's shifted by (dx, dy)

    def ncc(first, keep):
        u, v = first[keep].astype(np.float64), b[keep].astype(np.float64)
        u, v = u-u.mean(), v-v.mean()
        denominator = math.sqrt(float((u*u).sum())*float((v*v).sum()))
        return float((u*v).sum()/denominator) if denominator > 0 else None
    margin = int(math.ceil(max(abs(dx), abs(dy))))+1
    keep = cv2.erode(inside.astype(np.uint8), np.ones((2*margin+1, 2*margin+1), np.uint8),
                     borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    extra.update(response=_round(response), ncc_at_zero_shift=_round(ncc(a, inside)))
    if keep.sum() < MIN_SIDE_PX**2:                           # the shift leaves too little overlap to judge
        return (result, extra) if detail else result
    shifted = cv2.warpAffine(a, np.float32([[1, 0, dx], [0, 1, dy]]), (a.shape[1], a.shape[0]), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
    agreement = ncc(shifted, keep)
    if agreement is None:
        return (result, extra) if detail else result
    result.update(shift_px=[_round(dx), _round(dy)], shift_m=_round(math.hypot(dx, dy)*target_m_per_px),
                  ncc=_round(agreement), samples=int(keep.sum()))
    return (result, extra) if detail else result


# --- design and verdict -----------------------------------------------------------------------------------------

def load_design(root=Path('.')):
    """(values, sha256) of the pre-registered DESIGN.json under root; refuses a missing or foreign design."""
    path = Path(root)/DESIGN_PATH
    try:
        data = path.read_bytes()
        values = json.loads(data)
    except (OSError, ValueError) as error:
        raise ValueError(f'{DESIGN_PATH} is missing or unreadable: the recapture design is pre-registered before any '
                         'measurement or capture') from error
    if values.get('version') != DESIGN_VERSION or any(isinstance(values.get(k), bool) or not isinstance(values.get(k), (int, float))
                                                        for k in DESIGN_KEYS):
        raise ValueError(f'{DESIGN_PATH} is not a {DESIGN_VERSION} design with {", ".join(DESIGN_KEYS)}')
    if SUBJECT_KEY in values and subject_floor(values) is None:
        raise ValueError(f'{DESIGN_PATH} {SUBJECT_KEY} must carry a positive integer min_overlap_px (target px) when present')
    return values, _sha(data)


def subject_floor(values):
    """The pre-registered subject-recapture floor of a design ({min_overlap_px, applies_to}), or None when it has none."""
    block = values.get(SUBJECT_KEY)
    floor = block.get('min_overlap_px') if isinstance(block, dict) else None
    if isinstance(floor, bool) or not isinstance(floor, int) or floor <= 0:
        return None
    return {'min_overlap_px': floor, 'applies_to': block.get('applies_to')}


def design_block(design, role=None):
    """The design values a transform records: the five pre-registered keys and the design sha; for a subject recapture
    (role SUBJECT_ROLE) also the applied subject_recapture floor, which must be pre-registered."""
    values, digest = design
    block = {**{key: values[key] for key in DESIGN_KEYS}, 'design_sha256': digest}
    if role == SUBJECT_ROLE:
        floor = subject_floor(values)
        if floor is None:
            raise ValueError(f'{DESIGN_PATH} has no {SUBJECT_KEY}.min_overlap_px: the subject overlap floor is pre-registered '
                             '(dated amendment) before any subject plan or capture')
        block[SUBJECT_KEY] = floor
    return block


def design_sha_accepted(design, recorded_sha):
    """The acceptance of a recorded design sha256 under a loaded design (values, sha): design_chain.match over
    design_chain.accepted_shas, i.e. {'match': 'design_sha_current', ...} for the file's own sha, {'match': 'design_sha_via_chain',
    ...hop} for the previous_sha256 of a dated amendment reached from the newest entry through thresholds_changed-false entries,
    None when refused (behind a threshold change, or unknown)."""
    values, digest = design
    return design_chain.match(design_chain.accepted_shas(values, digest), recorded_sha)


def _without_sha(block):
    return {k: v for k, v in block.items() if k != 'design_sha256'}


def verdict(transform, values, role=None):
    """(verified, reasons) against the design values: overlap, shift and ncc, each with its own reason. The overlap rule
    follows the role (the caller's, else the transform's): a subject recapture needs overlap.region_px.side_px >=
    subject_recapture.min_overlap_px; any other needs overlap.fraction >= min_overlap_fraction."""
    reasons = []
    role = role if role is not None else transform.get('role')
    block = transform.get('overlap') or {}
    fraction = block.get('fraction') or 0.
    if role == SUBJECT_ROLE:
        floor = subject_floor(values)
        side = (block.get('region_px') or {}).get('side_px')
        if floor is None:
            reasons.append(f'{SUBJECT_KEY}.min_overlap_px is not pre-registered in DESIGN.json: a subject recapture has no overlap floor')
        elif side is None:
            reasons.append('overlap region unmeasured (no region_px on the transform)')
        elif side < floor['min_overlap_px']:
            reasons.append(f"overlap region side {side:g} px < min_overlap_px {floor['min_overlap_px']} (fraction {fraction:.3f})")
    elif fraction < values['min_overlap_fraction']:
        reasons.append(f"overlap {fraction:.3f} < min_overlap_fraction {values['min_overlap_fraction']:g}")
    measured = transform.get('residual') or {}
    shift, ncc = measured.get('shift_m'), measured.get('ncc')
    if shift is None or ncc is None:
        reasons.append('residual unmeasured (featureless image or too small an overlap)')
    else:
        if shift > values['max_shift_m']:
            reasons.append(f"shift_m {shift:.3f} > max_shift_m {values['max_shift_m']:g}")
        if ncc < values['min_ncc']:
            reasons.append(f"ncc {ncc:.3f} < min_ncc {values['min_ncc']:g}")
    return not reasons, reasons


def measured_transform(source, target, source_png, target_png, design, *, role=None):
    """frame_transform plus the residual measured from both images' bytes, the design block and the verdict; role
    SUBJECT_ROLE applies the subject floor (and refuses a design without one before any measurement)."""
    block = design_block(design, role)
    source_bytes, target_bytes = _bytes(source_png), _bytes(target_png)
    if _sha(source_bytes) != source.get('image_sha256'):
        raise ValueError(f"the source image bytes do not hash to {source['id']}'s image_sha256")
    if _sha(target_bytes) != target.get('image_sha256'):
        raise ValueError("the target image bytes do not hash to the target's image_sha256")
    ft = frame_transform(source, target, role=role)
    ft['residual'] = residual(source_bytes, target_bytes, ft, pixel_spacing(target))
    ft['design'] = block
    ft['verified'] = verdict(ft, design[0])[0]
    return ft


# --- snapshots --------------------------------------------------------------------------------------------------

def content_signature(scene):
    """save_scene's model_signature: sha256 of the sorted-key JSON of everything but the signature itself."""
    return _sha(json.dumps({k: v for k, v in scene.items() if k != 'model_signature'}, sort_keys=True).encode())


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def carry_source(root, scene_id):
    """(snapshot, png bytes) of the frame a recapture replaces, checked before any capture.

    The snapshot is the one the saved review sits on (models/<review model_signature>.json) when that file exists,
    else latest.json's; it must hash to its signature and its PNG to image_sha256.
    """
    scene_id = validate_id(scene_id)
    folder = Path(root)/'data/scenes'/scene_id
    latest = _read_json(folder/'latest.json')
    if not isinstance(latest, dict) or not latest.get('model_signature'):
        raise ValueError(f'carry_from {scene_id}: no saved scene at data/scenes/{scene_id}/latest.json')
    review = _read_json(folder/'review.json')
    signature = latest['model_signature']
    if isinstance(review, dict) and (folder/'models'/f"{review.get('model_signature')}.json").exists():
        signature = review['model_signature']
    snapshot = _read_json(folder/'models'/f'{signature}.json')
    if not isinstance(snapshot, dict) or snapshot.get('model_signature') != signature or content_signature(snapshot) != signature:
        raise ValueError(f'carry_from {scene_id}: models/{signature}.json is missing or does not hash to its signature')
    try:
        data = (folder/f"{snapshot['image_sha256']}.png").read_bytes()
    except (OSError, KeyError) as error:
        raise ValueError(f'carry_from {scene_id}: the source image is missing') from error
    if _sha(data) != snapshot['image_sha256']:
        raise ValueError(f'carry_from {scene_id}: the source image bytes do not hash to image_sha256')
    return snapshot, data


def _scene_dir(path):
    path = Path(path).resolve()
    return path.parent.parent if path.parent.name == 'models' else path.parent


def verify(source_path, target_path, root=None):
    """(ok, reasons, measured residual) for a recaptured frame against its source, from bytes only; never writes."""
    source, target = _read_json(source_path), _read_json(target_path)
    if not isinstance(source, dict) or not isinstance(target, dict):
        return False, ['a snapshot is missing or unreadable'], None
    reasons = []
    for name, scene, path in (('source', source, Path(source_path)), ('target', target, Path(target_path))):
        if scene.get('model_signature') != content_signature(scene):
            reasons.append(f'{name} snapshot content does not hash to its model_signature')
        if path.parent.name == 'models' and path.stem != scene.get('model_signature'):
            reasons.append(f'{name} snapshot file name is not its model_signature')
    ft = target.get('frame_transform')
    if not isinstance(ft, dict) or ft.get('version') != VERSION or ft.get('method') != METHOD:
        return False, [*reasons, f'the target carries no {VERSION} {METHOD} frame_transform'], None
    role = ft.get('role')
    if role not in (None, SUBJECT_ROLE):
        return False, [*reasons, f'recorded role {role!r} is not {SUBJECT_ROLE!r} or null'], None
    try:
        for key, expected in (('source_scene_id', source.get('id')), ('source_model_signature', source.get('model_signature')),
                              ('source_image_sha256', source.get('image_sha256')),
                              ('source_size', [source.get('image_width'), source.get('image_height')]),
                              ('source_registration', registration(source))):
            if ft.get(key) != expected:
                reasons.append(f'recorded {key} does not match the source snapshot')
        images = {}
        for name, scene, path in (('source', source, source_path), ('target', target, target_path)):
            png = _scene_dir(path)/f"{scene.get('image_sha256')}.png"
            data = png.read_bytes() if png.exists() else b''
            if _sha(data) != scene.get('image_sha256'):
                reasons.append(f'{name} image bytes do not hash to its image_sha256')
                continue
            with Image.open(io.BytesIO(data)) as image:
                if image.size != (scene.get('image_width'), scene.get('image_height')):
                    reasons.append(f'{name} image size does not match the snapshot')
                    continue
            images[name] = data
        recomputed = frame_transform(source, target, role=role)
        for key in ('metre_offset', 'pixel_affine'):
            recorded = np.asarray(ft.get(key), dtype=float)
            if recorded.shape != np.asarray(recomputed[key]).shape or np.abs(recorded-np.asarray(recomputed[key])).max() > AGREE:
                reasons.append(f'recorded {key} does not match the one the two snapshots give')
        recorded_overlap = ft.get('overlap') or {}
        if abs(float(recorded_overlap.get('fraction', -1))-recomputed['overlap']['fraction']) > AGREE:
            reasons.append('recorded overlap does not match the one the two snapshots give')
        if role == SUBJECT_ROLE and (recorded_overlap.get('region_px') or {}).get('side_px') != recomputed['overlap']['region_px']['side_px']:
            reasons.append('recorded overlap region does not match the one the two snapshots give')
    except (TypeError, ValueError, KeyError) as error:
        return False, [*reasons, f'malformed snapshot or frame_transform: {error}'], None
    root = Path(root) if root else _scene_dir(target_path).parent.parent.parent
    try:
        design = load_design(root)
        expected_block = design_block(design, role)
    except ValueError as error:
        return False, [*reasons, str(error)], None
    recorded_block = ft.get('design') if isinstance(ft.get('design'), dict) else {}
    accepted = design_sha_accepted(design, recorded_block.get('design_sha256'))
    if accepted is None:
        reasons.append(f"recorded design sha {str(recorded_block.get('design_sha256'))[:12]} is neither {DESIGN_PATH}'s current sha "
                       f'{design[1][:12]} nor the previous_sha256 of a dated amendment reached through thresholds_changed false '
                       'entries: re-measure under the current design')
    elif _without_sha(recorded_block) != _without_sha(expected_block):
        reasons.append(f'recorded design values differ from {DESIGN_PATH} (sha {design[1][:12]}; the recorded sha is accepted as '
                       f"{accepted['match']}): re-measure under the current design")
    measured = None
    if len(images) == 2:
        measured = residual(images['source'], images['target'], recomputed, pixel_spacing(target))
        recorded = ft.get('residual') or {}
        same = (recorded.get('warped_from') == measured['warped_from'] and recorded.get('samples') == measured['samples']
                and all((recorded.get(k) is None) == (measured[k] is None) for k in ('shift_px', 'ncc')))
        if same and measured['ncc'] is not None:
            same = (np.abs(np.subtract(recorded['shift_px'], measured['shift_px'])).max() <= 1e-3
                    and abs(recorded['ncc']-measured['ncc']) <= 1e-3)
        if not same:
            reasons.append('recorded residual does not match the one re-measured from the image bytes')
        reasons += verdict({**recomputed, 'residual': measured}, design[0])[1]
    if ft.get('verified') is not True:
        reasons.append('recorded verified is not true')
    return not reasons, reasons, measured


# --- shared readers ---------------------------------------------------------------------------------------------

def _scene(root, scene_id):
    return _read_json(Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json')


def _review(root, scene_id):
    review = _read_json(Path(root)/'data/scenes'/validate_id(scene_id)/'review.json')
    return review if isinstance(review, dict) else None


def _image(root, scene):
    data = (Path(root)/'data/scenes'/scene['id']/f"{scene['image_sha256']}.png").read_bytes()
    if _sha(data) != scene['image_sha256']:
        raise ValueError(f"{scene['id']}: image bytes do not hash to image_sha256")
    return data


def frame_facts(scene):
    """Ids, signatures and pixel sizes of a frame, with the cohort's upsampled-composite wording."""
    spacing = pixel_spacing(scene)
    facts = {'scene_id': scene['id'], 'model_signature': scene.get('model_signature'), 'image_sha256': scene.get('image_sha256'),
             'size': [scene['image_width'], scene['image_height']], 'registration': registration(scene), 'm_per_px': round(spacing, 4)}
    region = scene.get('region') or {}
    if facts['registration'] == 'region':
        zoom = region.get('imagery_zoom')
        native = region.get('native_meters_per_pixel') or scene_lot.native_m_per_px(scene['centroid']['lat'], zoom)
        facts.update(provider=region.get('provider'), imagery_zoom=zoom, native_m_per_px=round(float(native), 4))
    else:
        native = scene_lot.native_m_per_px(scene['centroid']['lat'], scene_lot.COHORT_NATIVE_ZOOM)
        facts.update(imagery_zoom=scene_lot.COHORT_NATIVE_ZOOM, native_m_per_px=round(native, 4),
                     wording=scene_lot.cohort_wording(spacing, native))
    return facts


# --- pilot ------------------------------------------------------------------------------------------------------

def comparable_kinds(scene):
    """Kinds a frame looked for (scans complete or partial) or holds: an object of any other kind has nothing to agree with."""
    scans = scene.get('scans') or {}
    return ({kind for kind, scan in scans.items() if isinstance(scan, dict) and scan.get('status') in ('complete', 'partial')}
            | {o.get('kind') for o in scene.get('objects', [])})


def _compare(obj, ft, target, detections, decisions, target_review, values, comparable):
    entry = {'id': obj['id'], 'kind': obj.get('kind'), 'status': obj.get('status')}
    if obj.get('kind') not in comparable:
        return {**entry, 'reason': 'kind_not_scanned_in_target'}
    try:
        polygon = _polygon(project_object(obj, ft))
    except GEOMETRY_ERRORS:
        polygon = None
    if polygon is None:
        return {**entry, 'reason': 'invalid outline'}
    clipped = polygon.intersection(box(0, 0, target['image_width'], target['image_height']))
    if clipped.is_empty or clipped.area <= 0:
        return {**entry, 'reason': 'outside_new_frame'}
    entry['clipped_by_new_frame'] = bool(clipped.area < polygon.area*(1-1e-9))
    iou, matched = _best(clipped, detections.get(obj.get('kind'), []))
    entry.update(iou=round(iou, 4), matched_id=matched if iou >= values['match_iou'] else None,
                 matched=iou >= values['match_iou'], close=iou >= values['close_iou'])
    saved = target_review.get(entry['matched_id']) if entry['matched_id'] else None
    entry['target_review_status'] = saved.get('status') if saved else None
    entry['target_review_authority'] = _authority(saved) if saved else None
    entry['target_human_status'] = saved.get('status') if saved and _decided(saved) else None
    decision_iou, decision_id = _best(clipped, decisions.get(obj.get('kind'), []))
    if decision_id is not None:
        entry.update(decision_iou=round(decision_iou, 4), decision_id=decision_id,
                     decision_status=target_review[decision_id].get('status'))
    return entry


def pilot_pair(root, group, source_id, target_id, design, *, source=None, target=None):
    """One measured frame pair: transform, residual, verdict and per-object IoU agreement in the target frame."""
    root = Path(root)
    source = source or _scene(root, source_id)
    target = target or _scene(root, target_id)
    values = design[0]
    ft = measured_transform(source, target, _image(root, source), _image(root, target), design)
    ok, reasons = verdict(ft, values)
    shift_px = ft['residual']['shift_px']
    row = {'group': group, 'address_id': source.get('address_id'), 'source': frame_facts(source), 'target': frame_facts(target),
           'overlap_fraction': ft['overlap']['fraction'], 'shift_px': shift_px, 'shift_m': ft['residual']['shift_m'],
           'shift_east_north_m': east_north(shift_px, target),
           'ncc': ft['residual']['ncc'], 'samples': ft['residual']['samples'], 'verified': ok, 'reasons': reasons,
           'approximations': projection_note(source, target), 'frame_transform': ft}
    target_review = _review(root, target['id']) or {}
    by_id = {o['id']: o for o in target_review.get('objects', []) if isinstance(o, dict) and 'id' in o}
    row['target_review'] = ({'exists': True, 'revision': target_review.get('revision'),
                             'fresh': target_review.get('model_signature') == target.get('model_signature')} if target_review else {'exists': False})
    if group == 'house':
        row['compares'] = 'model objects of the two frames'
        objects = [o for o in source['objects'] if o.get('status') != 'superseded']
    else:
        row['compares'] = "the cohort's human confirmed + geometry_reviewed outlines, re-projected, against the target's detections"
        review = _review(root, source['id']) or {}
        objects = [o for o in review.get('objects', []) if o.get('status') == 'confirmed' and o.get('geometry_reviewed') and _human(o)]
    detections = _by_kind(o for o in target['objects'] if o.get('status') != 'superseded')
    decisions = _by_kind(o for o in by_id.values() if _decided(o))
    comparable = comparable_kinds(target)
    row['objects'] = [_compare(o, ft, target, detections, decisions, by_id, values, comparable) for o in objects]
    ious = [o['iou'] for o in row['objects'] if 'iou' in o]
    unscanned = sum(o.get('reason') == 'kind_not_scanned_in_target' for o in row['objects'])
    row.update(iou_median=_median(ious), iou_median_unscanned_as_zero=_median(ious+[0.]*unscanned),
               matched_share=_share(sum(o.get('matched', False) for o in row['objects']), len(ious)),
               close_share=_share(sum(o.get('close', False) for o in row['objects']), len(ious)),
               outside_new_frame=sum(o.get('reason') == 'outside_new_frame' for o in row['objects']),
               not_comparable=unscanned, **_matched_statuses(row['objects']))
    return row


def _matched_statuses(objects):
    """Saved statuses of the matched target objects: human decisions only, then the raw status and authority of any writer."""
    matched = [o for o in objects if o.get('matched')]
    return {'target_status_of_matched': dict(sorted(Counter(o['target_human_status'] for o in matched
                                                            if o.get('target_human_status')).items())),
            'target_review_status_of_matched': dict(sorted(Counter(o['target_review_status'] for o in matched
                                                                   if o.get('target_review_status')).items())),
            'target_review_authority_of_matched': dict(sorted(Counter(o['target_review_authority'] for o in matched
                                                                      if o.get('target_review_authority')).items()))}


def registered_frames(root, prefix=REGISTERED_PREFIX):
    """Frame geometry of every registered scene with region.bounds under a prefix (the mooroolbark-* z19 tiles)."""
    frames = []
    for path in sorted((Path(root)/'data/scenes').glob(f'{prefix}*/latest.json')):
        scene = _read_json(path)
        bounds = ((scene or {}).get('region') or {}).get('bounds')
        if bounds:
            frames.append({key: scene[key] for key in ('id', 'centroid', 'image_width', 'image_height', 'pixel_to_meters',
                                                       'meters_to_pixel', 'region')})
    return frames


def pilot_pairs(root):
    """(pairs, selection facts): the 8 house pairs plus the human-decided cohort scenes inside a registered frame."""
    root = Path(root)
    pairs = [('house', f'{COHORT}-{a}', f'house-{a}') for a in HOUSE_PAIRS]
    frames = registered_frames(root)
    facts = Counter(dict.fromkeys(('cohort_scenes', 'with_review', 'with_human_decision', 'centroid_inside_registered_frame',
                                   'mooroolbark_pairs'), 0))
    choices = Counter()
    excluded = []
    for path in sorted((root/'data/scenes').glob(f'{COHORT}-*/latest.json')):
        facts['cohort_scenes'] += 1
        scene_id = path.parent.name
        review = _review(root, scene_id)
        if review is None:
            continue
        facts['with_review'] += 1
        decided = any(isinstance(o, dict) and _decided(o) for o in review.get('objects', []))
        scene = _read_json(path)
        lon, lat = scene['centroid']['lon'], scene['centroid']['lat']
        hits = [f for f in frames if f['region']['bounds'][0] <= lon <= f['region']['bounds'][2]
                and f['region']['bounds'][1] <= lat <= f['region']['bounds'][3]]
        facts['with_human_decision'] += decided
        facts['centroid_inside_registered_frame'] += bool(hits)
        if not (decided and hits):
            # every reviewed cohort scene left out, with the first rule it fails and both facts
            excluded.append({'scene_id': scene_id, 'reason': 'no_human_decision' if not decided else 'outside_every_registered_frame',
                             'human_decision': decided, 'inside_registered_frame': bool(hits)})
            continue
        ranked = sorted(hits, key=lambda f: (-frame_transform(scene, f)['overlap']['fraction'],
                                             math.hypot(*metre_offset(scene, f)), f['id']))
        choices[len(hits)] += 1
        facts['mooroolbark_pairs'] += 1
        pairs.append(('mooroolbark', scene_id, ranked[0]['id']))
    facts['registered_frames'] = len(frames)
    return pairs, {**facts, 'candidate_frames_per_pair': dict(sorted(choices.items())), 'excluded': excluded}


def _group_summary(rows, values):
    measured = [r for r in rows if 'frame_transform' in r]
    n = len(measured)
    objects = [o for r in measured for o in r['objects']]
    ious = [o['iou'] for o in objects if 'iou' in o]
    kinds = sorted({o['kind'] for o in objects if 'iou' in o})
    summary = {
        'pairs': n, 'verified': sum(r['verified'] for r in measured), 'verified_share': _share(sum(r['verified'] for r in measured), n),
        'pass_share': {'overlap': _share(sum(r['overlap_fraction'] >= values['min_overlap_fraction'] for r in measured), n),
                       'shift': _share(sum(r['shift_m'] is not None and r['shift_m'] <= values['max_shift_m'] for r in measured), n),
                       'ncc': _share(sum(r['ncc'] is not None and r['ncc'] >= values['min_ncc'] for r in measured), n)},
        'unmeasured': sum(r['shift_m'] is None for r in measured),
        'median_overlap_fraction': _median([r['overlap_fraction'] for r in measured]),
        'median_shift_m': _median([r['shift_m'] for r in measured]),
        'median_shift_px': _median([None if r['shift_px'] is None else math.hypot(*r['shift_px']) for r in measured]),
        'median_ncc': _median([r['ncc'] for r in measured]),
        'median_shift_east_north_m': [_median([r['shift_east_north_m'][i] for r in measured if r['shift_east_north_m']]) for i in (0, 1)],
        'shift_east_north_m_iqr': [[_round(np.percentile(values, 25)), _round(np.percentile(values, 75))] if values else None
                                   for values in ([r['shift_east_north_m'][i] for r in measured if r['shift_east_north_m']] for i in (0, 1))],
        'objects': len(objects), 'objects_compared': len(ious), 'outside_new_frame': sum(o.get('reason') == 'outside_new_frame' for o in objects),
        'kind_not_scanned_in_target': sum(o.get('reason') == 'kind_not_scanned_in_target' for o in objects),
        'iou_median': _median(ious),
        'iou_median_unscanned_as_zero': _median(ious+[0.]*sum(o.get('reason') == 'kind_not_scanned_in_target' for o in objects)),
        'matched_share': _share(sum(o.get('matched', False) for o in objects), len(ious)),
        'close_share': _share(sum(o.get('close', False) for o in objects), len(ious)),
        'iou_median_by_kind': {k: {'objects': sum(o['kind'] == k for o in objects if 'iou' in o),
                                   'iou_median': _median([o['iou'] for o in objects if o['kind'] == k and 'iou' in o])} for k in kinds},
        'verified_pair_iou_median': _median([o['iou'] for r in measured if r['verified'] for o in r['objects'] if 'iou' in o]),
    }
    summary.update(_matched_statuses(objects))
    decision_ious = [o['decision_iou'] for o in objects if 'decision_iou' in o]
    summary['decision_agreement'] = {'objects_overlapping_a_human_decision': len(decision_ious), 'iou_median': _median(decision_ious),
                                     'by_status': dict(sorted(Counter(o['decision_status'] for o in objects if 'decision_iou' in o).items()))}
    return summary


def _computed_wording(summary):
    """Sentences whose numbers are read from this run's summary at write time (never typed)."""
    nouns = {'house': 'cohort model objects', 'mooroolbark': 're-projected human outlines (confirmed, geometry_reviewed)'}
    lines = []
    for group, noun in nouns.items():
        s = summary[group]
        if not s['pairs']:
            continue
        unscanned = s['kind_not_scanned_in_target']
        reach = (f"the {s['objects_compared']} of {s['objects']} {noun} of kinds the target scanned (of other kinds, with no "
                 f"IoU: {unscanned}; median {s['iou_median_unscanned_as_zero']} when they count as 0)" if unscanned
                 else f"all {s['objects']} {noun} (each of their kinds was scanned in the target)")
        decided = ', '.join(f'{k} {v}' for k, v in s['target_status_of_matched'].items()) or 'none'
        lines.append(f"{group}: the IoU median {s['iou_median']} is over {reach}. Matched target objects with a saved status: "
                     f"{sum(s['target_review_status_of_matched'].values())}; human decisions among them: "
                     f"{sum(s['target_status_of_matched'].values())} ({decided}); the rest are undecided candidates or not "
                     "the reviewer's own.")
    return lines


def pilot(root=Path('.'), out=None):
    root = Path(root)
    design = load_design(root)
    values = design[0]
    pairs, selection = pilot_pairs(root)
    rows = []
    for group, source_id, target_id in pairs:
        source, target = _scene(root, source_id), _scene(root, target_id)
        if source is None or target is None:
            rows.append({'group': group, 'source_id': source_id, 'target_id': target_id, 'missing': True})
            continue
        rows.append(pilot_pair(root, group, source_id, target_id, design, source=source, target=target))
    summary = {group: _group_summary([r for r in rows if r['group'] == group], values) for group in ('house', 'mooroolbark')}
    summary['all'] = _group_summary(rows, values)
    result = {
        'version': PILOT_VERSION, 'computed_at': _now(), 'code_sha256': code_sha256(), 'scene_lot_sha256': scene_lot_sha256(),
        'design_file': str(DESIGN_PATH), 'design': design_block(design), 'design_written_at': values.get('written_at'),
        'pairs_rule': (f'the {len(HOUSE_PAIRS)} cohort->house z20 pairs {list(HOUSE_PAIRS)} (model objects of the two frames; '
                       'house-side human decision on the matched object) plus every cohort scene whose review.json holds at '
                       'least one human confirmed / rejected / needs_adjustment decision (no machine_label or fieldwork_pending '
                       'blocker, no assistant note) and whose centroid lies inside a registered mooroolbark-* region.bounds '
                       '(target: the frame holding most of the cohort frame, then the nearest centre); every reviewed cohort '
                       'scene left out is listed in selection.excluded with its reason'),
        'selection': selection, 'method': {'residual': RESIDUAL_METHOD, 'blur_px': BLUR_PX, 'edge_px': EDGE_PX,
                                           'min_side_px': MIN_SIDE_PX, 'refine_px': REFINE_PX,
                                           'shift': 'phase-correlation peak (regularised whitening) refined at the gradient-NCC peak',
                                           'ncc': 'after removing the measured shift',
                                           'iou': ('best IoU per kind in target px; an object whose kind the target never scanned '
                                                   'or holds is kind_not_scanned_in_target and carries no IoU: iou_median is over '
                                                   'the compared objects and iou_median_unscanned_as_zero counts the others as 0. '
                                                   'Rule added at 2026-09-30T12:43:55Z after a read-only smoke (12:42:26Z '
                                                   'roof_training_250-30280 -> house-30280 and roof_training_250-30371 -> '
                                                   'mooroolbark-50011498-17-118442-80417; 12:42:54Z roof_training_250-30371 -> '
                                                   'mooroolbark-50011498-17-118443-80417) showed the mooroolbark tiles scan '
                                                   'building, tree and solar_panel only; DESIGN values untouched'),
                                           'signed_offset': ('shift_east_north_m (through the target affine), its medians and IQR, '
                                                             'and the per-row target_status_of_matched were added after the '
                                                             'first full run (2026-09-30T12:49:51Z) showed one direction across '
                                                             'pairs; a description only: no threshold, verdict or IoU rule changed'),
                                           'reporting_changes': list(REPORTING_CHANGES), 'module_copies': MODULE_COPIES},
        'definitions': {k: DEFINITIONS[k] for k in ('target_human_status', 'target_review_status', 'target_review_authority')},
        'design_amendments': values.get('amendments', []),
        'summary': summary,
        'wording': [*(WORDING[k] for k in ('agreement', 'residual', 'z19', 'house', 'decisions', 'cohort', 'signed', 'nothing')),
                    *_computed_wording(summary)],
        'rows': rows}
    if out is not None:
        previous = Path(out)
        if previous.exists():                       # say which run this one replaces (same pairs, same design unless the sha says so)
            earlier = _read_json(previous) or {}
            result['replaces_run'] = {'computed_at': earlier.get('computed_at'), 'sha256': _sha(previous.read_bytes()),
                                      'code_sha256': earlier.get('code_sha256'),
                                      'design_sha256': (earlier.get('design') or {}).get('design_sha256')}
        _atomic(out, json.dumps(result, indent=1)+'\n', root)
    return result


# --- plan -------------------------------------------------------------------------------------------------------

def _js(value, default):
    """JavaScript `value || default`: [] and {} are truthy there."""
    falsy = value is None or value is False or value == '' or (isinstance(value, (int, float)) and not isinstance(value, bool)
                                                               and (value == 0 or value != value))
    return default if falsy else value


def feedback_key(obj):
    """The review app's edit-comparison fields (src/lib/scene-feedback.ts `feedback`): a changed key set is carried."""
    model = obj.get('model') or {}
    return {'kind': obj.get('kind'), 'label': obj.get('label'), 'ring': obj.get('ring'), 'holes': _js(obj.get('holes'), []),
            'status': obj.get('status'), 'checked': obj.get('geometry_reviewed'), 'notes': _js(obj.get('notes'), ''),
            'group': _js(obj.get('group_id'), ''), 'visibility': _js(obj.get('visibility'), None),
            'review_issues': _js(obj.get('review_issues'), []), 'parent': _js(obj.get('parent_id'), ''),
            'roof': _js(obj.get('roof_annotation'), None), 'parts': _js(obj.get('roof_parts'), None),
            'lines': _js(obj.get('roof_lines'), None), 'form': _js(obj.get('roof_type'), ''),
            'roof_source': _js(obj.get('roof_geometry_source'), ''), 'roof_note': _js(obj.get('roof_geometry_note'), ''),
            'roof_feedback': _js(obj.get('roof_feedback'), None), 'roof_line_edits': _js(obj.get('roof_line_edits'), None),
            'height': model.get('height_m'), 'base': model.get('base_m'), 'color': model.get('color'),
            'height_source': model.get('height_source'), 'height_verified': model.get('height_verified'),
            'placement': _js(model.get('placement'), 'roof'), 'tree': _js(model.get('tree'), None), 'car': _js(model.get('car'), None),
            'surface': _js(model.get('surface'), None), 'solar': _js(model.get('solar'), None),
            'derived_from': _js(obj.get('derived_from'), None), 'replacement_ids': _js(obj.get('replacement_ids'), None)}


def _tally(objects):
    counts = dict.fromkeys(STATUSES, 0)
    for obj in objects:
        key = obj.get('status') if obj.get('status') in STATUSES else 'other'
        counts[key] = counts.get(key, 0)+1
    return counts


def _decision_tally(objects):
    """Human decisions by status: confirmed, needs_adjustment and rejected on the reviewer's own objects, nothing else."""
    counts = dict.fromkeys(DECISIONS, 0)
    for obj in objects:
        if _decided(obj):
            counts[obj['status']] += 1
    return counts


def _subset(objects):
    return {'count': len(objects), 'ids': sorted(o['id'] for o in objects),
            'by_status': dict(sorted(Counter(o.get('status') for o in objects).items()))}


def _lineage_sets(objects, place):
    """Connected derived_from / replacement_ids sets with their fate in the new frame (carried whole or refused)."""
    known = {o['id'] for o in objects}
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for obj in objects:
        for other in [*(obj.get('derived_from') or []), *(obj.get('replacement_ids') or [])]:
            parent[find(obj['id'])] = find(other)
    groups = {}
    for member in parent:
        groups.setdefault(find(member), set()).add(member)
    sets = []
    for members in sorted(groups.values(), key=lambda m: sorted(m)):
        entry = {'members': sorted(members)}
        missing = sorted(members-known)
        if missing:
            entry.update(status='missing_member', missing=missing)
        elif place is None:
            entry['status'] = 'unplanned'
        else:
            fates = {place.get(m) for m in members}
            entry['status'] = ('whole_inside' if fates == {'inside'} else 'all_outside' if fates == {'outside'}
                               else 'split_by_frame')
        sets.append(entry)
    return sets


def fieldwork_mirror(root):
    """Items of the local Fieldwork mirror (the mooroolbark project) by scene id; a read of the local file only."""
    path = Path(root)/FIELDWORK_MIRROR
    if not path.exists():
        return {'path': str(FIELDWORK_MIRROR), 'available': False, 'items': {}}
    text = path.read_text()
    decoder = json.JSONDecoder()
    items = {}
    for match in re.finditer(r'"id":"addr-scene-([A-Za-z0-9_-]+)","metadata":', text):
        try:
            meta, _ = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        addr = meta.get('addr') or {}
        items[match.group(1)] = {'review_revision': addr.get('sourceReviewRevision'), 'review_model_signature': addr.get('reviewModelSignature'),
                                 'review_updated_at': addr.get('reviewUpdatedAt'), 'stale': addr.get('stale'),
                                 'snapshot_id': addr.get('snapshotId'), 'imported_at': addr.get('importedAt')}
    head = re.search(r'"origin":"([^"]+)"', text[:4096])
    return {'path': str(FIELDWORK_MIRROR), 'available': True, 'origin': head.group(1) if head else None, 'items': items}


def capture_tiles(bounds, zoom):
    """Tiles capture_region would fetch for a frame (the same arithmetic, no fetch)."""
    west, south, east, north = bounds
    left, top = np.array(lonlat_to_tile_xy(west, north, zoom))*256
    right, bottom = np.array(lonlat_to_tile_xy(east, south, zoom))*256
    return (math.ceil(right/256)-math.floor(left/256))*(math.ceil(bottom/256)-math.floor(top/256))


def _planned_frame(plan):
    frame = region_frame(plan['requested_bounds'], plan['provider'])
    return {'id': f"lot-{plan['address_id']}", 'centroid': frame['centroid'], 'region': frame['region'],
            'image_width': plan['frame_px'], 'image_height': plan['frame_px'],
            'pixel_to_meters': frame['pixel_to_meters'], 'meters_to_pixel': frame['meters_to_pixel']}


def _placement(obj, ft, target):
    try:
        polygon = _polygon(project_object(obj, ft))
    except GEOMETRY_ERRORS:
        polygon = None
    if polygon is None:
        return 'invalid'
    frame = box(0, 0, target['image_width'], target['image_height'])
    if frame.covers(polygon):
        return 'inside'
    return 'partial' if polygon.intersects(frame) and polygon.intersection(frame).area > 0 else 'outside'


def plan_row(root, scene, design, mirror):
    """One cohort scene's dry-run carry plan (no pixels read, no network, nothing written)."""
    root = Path(root)
    values = design[0]
    scene_id = scene['id']
    review = _review(root, scene_id)
    objects = [o for o in (review or {}).get('objects', []) if isinstance(o, dict) and 'id' in o]
    machine = [o for o in objects if review_authority_blocker(o) == 'machine_label']
    pending = [o for o in objects if review_authority_blocker(o) == 'fieldwork_pending']
    agent = [o for o in objects if _agent(o) and review_authority_blocker(o) is None]
    human = [o for o in objects if _human(o)]
    row = {'version': PLAN_VERSION, 'scene_id': scene_id, 'address_id': scene.get('address_id'),
           'model_signature': scene.get('model_signature'), 'image_sha256': scene.get('image_sha256'),
           'image_size': [scene.get('image_width'), scene.get('image_height')], 'registration': registration(scene)}
    if review is None:
        row['review'] = {'exists': False}
    else:
        row['review'] = {'exists': True, 'revision': review.get('revision'), 'model_signature': review.get('model_signature'),
                         'model_signature_matches': review.get('model_signature') == scene.get('model_signature'),
                         'updated_at': review.get('updated_at')}
    row.update(statuses=_tally(objects), machine_only=_subset(machine),
               machine_confirmed_by_human=_subset([o for o in machine if _machine_confirmed(o)]), agent_authored=_subset(agent),
               fieldwork_pending=_subset(pending), human_decisions=_decision_tally(human),
               statuses_excluding_machine_pending_agent=_tally(human),
               metre_fields={f: sum(1 for o in objects if o.get(f)) for f in METRE_FIELDS})
    # What a carry would take: the review app's rule (an object is carried when its feedback key set differs from the
    # model object, or it has none: human-drawn); machine-only labels are dropped; unedited candidates stay behind.
    previous = scene
    if review and review.get('model_signature') != scene.get('model_signature'):
        previous = _read_json(root/'data/scenes'/scene_id/'models'/f"{review.get('model_signature')}.json") or scene
    originals = {o['id']: o for o in previous.get('objects', [])}
    changed = [o for o in objects if o['id'] not in originals or feedback_key(o) != feedback_key(originals[o['id']])]
    carried = [o for o in changed if not _machine_only(o)]
    carried_ids = {o['id'] for o in carried}
    row['carry'] = {'ids': sorted(carried_ids), 'dropped_machine_only': sorted(o['id'] for o in changed if _machine_only(o)),
                    'unedited_candidates': sum(o.get('status') == 'candidate' and o['id'] not in carried_ids and not _machine_only(o)
                                               for o in objects),
                    'inside': [], 'partial': [], 'outside': []}
    # The lot frame (offline: frozen parcels only; no Vicmap, no tile provider)
    plan, place, target = None, None, None
    try:
        plan = scene_lot.lot_frame(scene['address_id'], root=root, source_scene=scene, offline=True)
    except GEOMETRY_ERRORS as error:
        message = str(error)
        row['parcels'] = 'parcels_unavailable' if 'unavailable' in message else 'lot_frame_error'
        row['lot_frame_error'] = message
    row['lot_frame'] = plan
    spacing = pixel_spacing(scene)
    facts = frame_facts(scene)
    row['m_per_px'] = {'source': round(spacing, 4), 'source_native': facts['native_m_per_px'], 'source_zoom': facts['imagery_zoom'],
                       'source_wording': facts.get('wording') or f"Google z{facts['imagery_zoom']} ({facts['native_m_per_px']:.3f} m/px native)",
                       'target': None, 'target_native': None, 'target_zoom': None, 'comparison': None}
    row['frame_transform_predicted'] = None
    row['cost'] = {'capture_tiles': None, 'provider': scene_lot.DESIGN['provider'], 'sam3_extractions': 1,
                   'note': ('one tile capture and one SAM3 extraction later (POST /api/scenes/extract {scene_id}, one SAM3 process '
                            'at a time); nothing is captured or extracted by this plan')}
    if plan is not None:
        row['parcels'] = plan['parcels_source']
        target = _planned_frame(plan)
        ft = frame_transform(scene, target)
        row['frame_transform_predicted'] = {k: ft[k] for k in ('metre_offset', 'pixel_affine', 'overlap', 'source_registration')}
        row['m_per_px'].update(target=plan['m_per_px'], target_native=plan['native_m_per_px'], target_zoom=plan['zoom'],
                               comparison=scene_lot.native_comparison(plan['native_m_per_px'], facts['native_m_per_px']))
        row['cost']['capture_tiles'] = capture_tiles(plan['bounds'], plan['zoom'])
        place = {}
        for obj in objects:
            place[obj['id']] = _placement(obj, ft, target)
        for oid in sorted(carried_ids):
            row['carry'].setdefault(place[oid], []).append(oid)
    row['lineage_sets'] = _lineage_sets(objects, place)
    decisions = [o for o in carried if o.get('status') != 'superseded' and (place is None or place[o['id']] != 'outside')]
    superseded = [o for o in carried if o.get('status') == 'superseded']
    row['predicted_post_carry'] = {
        'carried': len(decisions), 'status': {'needs_adjustment': len(decisions)}, 'confirmed': 0, 'rejected': 0,
        'geometry_reviewed': 0, 'height_verified': 0, 'all_needs_adjustment': True,
        'superseded_lineage_members': len(superseded),
        'dropped_outside_new_frame': 0 if place is None else sum(place[o['id']] == 'outside' for o in carried if o.get('status') != 'superseded'),
        'split_lineage_sets': sum(s['status'] == 'split_by_frame' for s in row['lineage_sets']),
        'policy': values.get('status_policy')}
    row['residual'] = 'unmeasured'
    row['training_impact'] = ('none on the untouched source: its latest.json, review.json and confirmed outlines stay as they are '
                              'in every export; a lot scene is a new id whose carried objects are all needs_adjustment')
    item = mirror['items'].get(scene_id)
    state = {'project': 'mooroolbark', 'item_id': f'addr-scene-{scene_id}', 'mirror_available': mirror['available'],
             'mirrored': item is not None}
    if item is not None:
        state.update(mirror_review_revision=item['review_revision'], mirror_review_model_signature=item['review_model_signature'],
                     mirror_stale=item['stale'],
                     matches_local_review=bool(review) and item['review_model_signature'] == review.get('model_signature')
                     and item['review_revision'] == review.get('revision'))
    state['lot_scene'] = 'a lot-<address_id> scene reaches Fieldwork only as a new item when the user pushes (live push is user-only)'
    row['fieldwork_mirror'] = state
    return row


def _plan_totals(rows, design):
    def total(key):
        out = Counter()
        for row in rows:
            out.update(row[key])
        return dict(out)
    statuses = dict.fromkeys(STATUSES, 0) | total('statuses')
    human = dict.fromkeys(DECISIONS, 0) | total('human_decisions')
    remaining = dict.fromkeys(STATUSES, 0) | total('statuses_excluding_machine_pending_agent')
    carried = sum(r['predicted_post_carry']['carried'] for r in rows)
    lineage = Counter(s['status'] for r in rows for s in r['lineage_sets'])
    return {
        'version': PLAN_VERSION, 'computed_at': _now(), 'code_sha256': code_sha256(), 'scene_lot_sha256': scene_lot_sha256(),
        'design_sha256': design[1], 'counting_rule': design[0].get('counting_rule'),
        'counting_rule_amendments': [a for a in design[0].get('amendments', []) if a.get('field') == 'counting_rule'],
        'definitions': dict(DEFINITIONS), 'scenes': len(rows),
        'scenes_with_review': sum(r['review']['exists'] for r in rows), 'statuses': statuses,
        'machine_only': sum(r['machine_only']['count'] for r in rows),
        'machine_confirmed_by_human': sum(r['machine_confirmed_by_human']['count'] for r in rows),
        'agent_authored': sum(r['agent_authored']['count'] for r in rows),
        'fieldwork_pending': sum(r['fieldwork_pending']['count'] for r in rows), 'human_decisions': human,
        'statuses_excluding_machine_pending_agent': remaining,
        'metre_fields': dict.fromkeys(METRE_FIELDS, 0) | total('metre_fields'),
        'carried': carried, 'predicted_post_carry': {'needs_adjustment': carried, 'confirmed': 0, 'rejected': 0},
        'dropped_machine_only': sum(len(r['carry']['dropped_machine_only']) for r in rows),
        'dropped_outside_new_frame': sum(r['predicted_post_carry']['dropped_outside_new_frame'] for r in rows),
        'unedited_candidates': sum(r['carry']['unedited_candidates'] for r in rows),
        'lot_frames': {'planned': sum(r['lot_frame'] is not None for r in rows),
                       'parcels_unavailable': sum(r.get('parcels') == 'parcels_unavailable' for r in rows),
                       'lot_frame_error': sum(r.get('parcels') == 'lot_frame_error' for r in rows)},
        'lineage_sets': dict(sorted(lineage.items())),
        'fieldwork_mirrored': sum(r['fieldwork_mirror']['mirrored'] for r in rows),
        'capture_tiles_planned': sum(r['cost']['capture_tiles'] or 0 for r in rows), 'sam3_extractions_later': len(rows),
        'residual': 'unmeasured (no scene is captured by the plan)'}


def plan(cohort, *, root=Path('.'), out=None, offline=True):
    """Dry-run carry plan for every scene of a cohort; offline only (never Vicmap, never a tile provider)."""
    if not offline:
        raise ValueError('the recapture plan runs offline only: it never reads Vicmap or a tile provider')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,80}', cohort):
        raise ValueError('Invalid cohort name')
    root = Path(root)
    design = load_design(root)
    mirror = fieldwork_mirror(root)
    rows = []
    for path in sorted((root/'data/scenes').glob(f'{cohort}-*/latest.json')):
        scene = _read_json(path)
        if isinstance(scene, dict) and scene.get('id') == path.parent.name:
            rows.append(plan_row(root, scene, design, mirror))
    totals = _plan_totals(rows, design)
    if out is not None and Path(out).exists():     # say which file this one replaces (its bytes are gone after the write)
        previous = Path(out).read_bytes()
        try:
            earlier = json.loads(previous.split(b'\n', 1)[0]).get('plan_totals') or {}
        except ValueError:
            earlier = {}
        totals['replaces_run'] = {'computed_at': earlier.get('computed_at'), 'sha256': _sha(previous),
                                  'code_sha256': earlier.get('code_sha256'), 'design_sha256': earlier.get('design_sha256')}
    for row in rows:
        row['plan_totals'] = totals
    if out is not None:
        _atomic(out, ''.join(json.dumps(row, sort_keys=True)+'\n' for row in rows), root)
    return rows


# --- output -----------------------------------------------------------------------------------------------------

def _refuse_scenes(path, root):
    path = Path(path).resolve()
    parts = path.parts
    inside = any(parts[i:i+2] == ('data', 'scenes') for i in range(len(parts)-1))
    if inside or path.is_relative_to((Path(root)/'data/scenes').resolve()):
        raise SystemExit(f'refusing to write under data/scenes/: {path}')


def _atomic(path, text, root):
    _refuse_scenes(path, root)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(text)
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Recapture carry path (dry run): verify a recaptured frame, plan the cohort, '
                                                 'or measure the pilot pairs. Never captures, never runs SAM3, never writes data/scenes/.')
    parser.add_argument('--verify-source', type=Path, help='data/scenes/<source>/models/<sig>.json')
    parser.add_argument('--verify-target', type=Path, help='data/scenes/<recapture>/models/<sig>.json')
    parser.add_argument('--root', type=Path, help='repository root (default: inferred from the verify paths, else .)')
    commands = parser.add_subparsers(dest='command')
    planner = commands.add_parser('plan', help='offline dry-run plan, one JSONL row per cohort scene')
    planner.add_argument('--cohort', default=COHORT)
    planner.add_argument('--offline', action='store_true', help='required: frozen parcels only, never Vicmap')
    planner.add_argument('--out', type=Path, required=True)
    planner.add_argument('--root', dest='command_root', type=Path)
    piloting = commands.add_parser('pilot', help='measure the existing frame pairs against DESIGN.json')
    piloting.add_argument('--pairs', choices=['auto'], default='auto')
    piloting.add_argument('--out', type=Path, required=True)
    piloting.add_argument('--root', dest='command_root', type=Path)
    args = parser.parse_args(argv)
    if args.command is None:
        if not (args.verify_source and args.verify_target):
            parser.error('give --verify-source and --verify-target, or a plan / pilot command')
        ok, reasons, measured = verify(args.verify_source, args.verify_target, args.root)
        if not ok:
            sys.stderr.write(FAILURE+'\n'+''.join(f'reason: {reason}\n' for reason in reasons))
            raise SystemExit(1)
        target = _read_json(args.verify_target)
        design = load_design(args.root or _scene_dir(args.verify_target).parent.parent.parent)
        recorded = target['frame_transform']['design']['design_sha256']
        json.dump({'verified': True, 'source_scene_id': target['frame_transform']['source_scene_id'], 'target_scene_id': target['id'],
                   'overlap_fraction': target['frame_transform']['overlap']['fraction'], 'shift_px': measured['shift_px'],
                   'shift_m': measured['shift_m'], 'ncc': measured['ncc'], 'samples': measured['samples'],
                   'design_sha256': recorded, 'design_sha_current': design[1], 'design_sha_match': design_sha_accepted(design, recorded)},
                  sys.stdout)
        sys.stdout.write('\n')
        return 0
    if args.verify_source or args.verify_target:
        parser.error('--verify-source / --verify-target stand alone')
    root = args.command_root or args.root or Path('.')
    if args.command == 'plan':
        if not args.offline:
            parser.error('the plan runs offline only: pass --offline (it never reads Vicmap or a tile provider)')
        _refuse_scenes(args.out, root)
        rows = plan(args.cohort, root=root, out=args.out, offline=True)
        totals = rows[0]['plan_totals'] if rows else {}
        json.dump({'rows': len(rows), 'out': str(args.out), **{k: totals.get(k) for k in ('computed_at', 'design_sha256', 'scenes_with_review',
                   'statuses', 'human_decisions', 'machine_only', 'machine_confirmed_by_human', 'agent_authored', 'metre_fields',
                   'predicted_post_carry', 'lot_frames', 'replaces_run')}}, sys.stdout, indent=1)
    else:
        _refuse_scenes(args.out, root)
        result = pilot(root, args.out)
        json.dump({'out': str(args.out), 'computed_at': result['computed_at'], 'design_sha256': result['design']['design_sha256'],
                   'selection': result['selection'], 'replaces_run': result.get('replaces_run'),
                   'summary': {g: {k: s[k] for k in ('pairs', 'verified_share', 'median_shift_m', 'median_ncc', 'iou_median',
                                                     'iou_median_unscanned_as_zero', 'target_status_of_matched')}
                               for g, s in result['summary'].items()}}, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
