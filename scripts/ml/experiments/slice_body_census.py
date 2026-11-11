"""Read-only census of every carried frame-cut slice the reviewer still sees as a slice, and whether a whole-roof body exists for it.

Reviewer (verbatim, 2026-10-06): "great that is now fixed, haveyou applied this to all items?" The fix was the lot-29362 body carry
(handover sections 28-29; .run/session-2026-10-06-body): a whole-roof body detected in a neighbouring frame was carried into the
window through a verified georeferenced transform and superseded the carried slice. This census answers the question for every other
slice: the review.json objects of the 54 windows (data/scenes/lot-*, win-*) with a non-empty carried_from and status != superseded.

Per slice: window, id, frozen key, area, status, the reviewer's decision if any, whether the frozen key is one of the frozen TARGETS
(r5_precision targets pool; the live counter's carried_rows class), the window's own best building detection cover (latest.json,
takeover_plan's reason), the Codex hint from WINDOW-WORKLIST-2026-10-02.md, and the candidate whole-roof bodies: building detections in
any other registered frame (scene_focus.RegisteredIndex, the planner's body search) whose georeferenced footprint covers the slice's
frozen source ring, plus the planner's own bodies list (window_frame.subjects[].bodies). Each body is measured SOURCE FRAME -> WINDOW
with scene_recapture.measured_transform (role frame_cut_target, DESIGN verdict), projected with project_object, and recorded with cover
of the slice (projected body ∩ slice / slice area), inside-frame (takeover frame tolerance, 1 px), body area, NCC / shift, whether the
body is cut at its own source frame, Lot parcel overlap and scene_extract.finalise_app_ring issues. Ranking follows the lot-29362
carry: verified, wholly inside the window frame, highest cover, then Lot overlap; a body cut at its own frame edge is recorded but is
not a whole body. 'none' when no body verifies.

Nothing is decided and nothing is written under data/scenes, the registry, RULES / DESIGN / WINDOW or the eyes index. No inference.

Run from the repo root: scripts/ml/.venv/bin/python -m scripts.ml.experiments.slice_body_census
Outputs ONLY under data/reviews/eyes/recapture/slice-body-census-2026-10-06/: census.json, census.md, renders/<window>.<slice>.png,
renders/contact-sheet.png, bodies/<window>.<slice>.json (projected rings and transform figures for a later apply step).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon, box
from shapely.ops import transform as shp_transform

from .. import scene_recapture as sr
from ..scene_extract import finalise_app_ring
from ..scene_focus import (
    TAKEOVER,
    WINDOW,
    RegisteredIndex,
    _geo,
    _polygon,
    load_window_rule,
    metre_frame,
    takeover_plan,
    touching_frame,
)
from ..scene_lot_tags import pad_bounds, registration

ROOT = Path('.')
OUT_DIR = Path('data/reviews/eyes/recapture/slice-body-census-2026-10-06')
RUN_DIR = Path('.run/session-2026-10-06-slices')
WORKLIST = Path('data/references/eyes-checkpoints/WINDOW-WORKLIST-2026-10-02.md')
FIELDWORK_DECISIONS = Path('data/reviews/eyes/fieldwork-review-2026-10-01/decisions.json')
REGISTRY = Path('data/reviews/eyes/recapture/windows-2026-10-01.jsonl')
RULES_PATH = Path('data/reviews/eyes/training/sam3-mac/round5/RULES.json')
WINDOW_PREFIXES = ('lot-', 'win-')
SCRATCH_PREFIX = 'qa_'
VERSION = 'slice-body-census-v1'
GEO_COVER_MIN = .5       # a body from the index is measured when its footprint covers at least this share of the frozen source ring
COVER_HIGH, COVER_HALF = TAKEOVER['cover_min'], .5
RANKING_RULE = ('verified transform (DESIGN subject floor, role frame_cut_target), ring wholly inside the window frame (no vertex within '
                f"{TAKEOVER['frame_tolerance_px']:g} px of an edge), not cut at its own source frame edge, app ring accepted; then highest "
                'slice cover, then Lot parcel overlap, then finer source m/px, then scene id')
HINT_LINE = re.compile(r'scene=([\w-]+)&status=all&layer=edited&object=(\w{16})\s+·\s+slice (\w{16}) · ([\d.]+) m² · from ([\w-]+)/(\w{16}) · '
                       r'(.*?)(?: · Codex \(old slice, hint only\): \*\*([A-Z-]+)\*\*: (.*))?$', re.M)
URL = 'http://localhost:3008/scenes?scene={scene}&status=all&layer=edited&object={oid}'
CONSEQUENCE = {
    'target_undecided': ('frozen TARGET, undecided (asked on the window). Superseding it with a carried body would make the target '
                         "'superseded' (excluded) and the body an undecided carried row that takeover_links refuses (replacer_carried): "
                         'the target leaves the population without a decision. Needs a dated RULES/WINDOW amendment letting the hop '
                         'follow a verified body_carry replacer. Do not act.'),
    'target_decided': ('frozen TARGET already decided on the window (counted as a primary row). The takeover convention leaves a '
                       'decided copy alone (copy_decided); superseding it would turn a counted decision into an excluded superseded '
                       'row (N moves by -1 unless the decision is re-made on the body). Do not act.'),
    'target_excluded': ('frozen TARGET currently excluded from asking ({reason}). Superseding it with a carried body changes the exclusion '
                        "to 'superseded' and adds an undecided carried body row: still no decision; the key would not be asked anywhere "
                        'without an amendment. Do not act.'),
    'non_target': 'not a frozen target: nothing in the count moves (the lot-29362 case).',
}


def _now():
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _r(value, digits=4):
    return None if value is None else round(float(value), digits)


# --- frames, rings, geometry --------------------------------------------------------------------------------------

def px_to_m_area(ring, scene):
    """Area in m2 of a pixel ring through the scene's pixel_to_meters (what the body carry recorded as body_area_m2)."""
    pts = np.asarray(ring, float)
    if len(pts) < 3:
        return 0.
    xy = np.c_[pts[:, :2], np.ones(len(pts))] @ np.asarray(scene['pixel_to_meters'], float)
    poly = Polygon(xy)
    return poly.area if poly.is_valid else poly.buffer(0).area


def lonlat_to_px(scene, lon, lat):
    m2p = np.asarray(scene['meters_to_pixel'], float)
    x, y = sr.local_metres(scene, lon, lat)
    return [x*m2p[0, 0]+y*m2p[1, 0]+m2p[2, 0], x*m2p[0, 1]+y*m2p[1, 1]+m2p[2, 1]]


def geo_to_px_polygon(scene, geo):
    """A WGS84 polygon in a scene's pixels."""
    def ring(coords):
        return [lonlat_to_px(scene, lon, lat) for lon, lat in coords]
    if geo.geom_type == 'MultiPolygon':
        parts = [Polygon(ring(g.exterior.coords), [ring(i.coords) for i in g.interiors]) for g in geo.geoms]
        from shapely.ops import unary_union
        return unary_union(parts)
    return Polygon(ring(geo.exterior.coords), [ring(i.coords) for i in geo.interiors])


def min_edge_margin(ring, side):
    return min(min(p[0], side-p[0], p[1], side-p[1]) for p in ring)


def decision_of(obj):
    """The reviewer's decision on a carried copy: confirmed / rejected, or needs_adjustment with geometry reviewed; None = the carry
    default (needs_adjustment, geometry_reviewed false) still stands."""
    status = obj.get('status')
    if status in ('confirmed', 'rejected'):
        return status
    if status == 'needs_adjustment' and obj.get('geometry_reviewed'):
        return status
    return None


# --- the counter's frozen pool ----------------------------------------------------------------------------------

def frozen_pool(log):
    """(pool keys, exclusion reasons per key, exclusion reasons per copy (window, id), remaining (window, id)) from r5_precision's own
    functions (reads only). Falls back to the count JSON shape when the module's internals are unavailable."""
    try:
        from ..training.sam3_mac import r5_precision as r5
        context = json.load(open(Path(r5.COUNT_RUN)/'reference-eval-v2'/'context.json'))
        split_path = r5.split_path_for(context, r5.REPO)
        prior = r5._prior_items(r5.PRIOR_SET)
        judged = {v.get('scene_id') for v in context['images'].values()}
        decisions = r5.load_decisions(r5.SCENES, extra_live=judged | {s for s, _, _ in prior})
        lineage = r5.load_lineage(r5.RECAPTURE_DIR)
        joined = r5.join(context, split_path, decisions, lineage=lineage)
        pool, remaining, excluded = r5.targets(joined, decisions, lineage)
    except Exception as error:  # noqa: BLE001 - the census must still run; the counter JSON is the fallback
        log(f'frozen_pool: r5_precision internals unavailable ({error!r}); using the count JSON only')
        return None
    keys = {(p['scene_id'], p['id']) for p in pool}
    key_reasons, copy_reasons = {}, {}
    for item, reason in excluded:
        if 'source_object_id' in item:
            copy_reasons.setdefault((item['scene_id'], item['id']), []).append(reason)
        elif 'frozen_keys' in item:
            for key in item['frozen_keys']:
                key_reasons.setdefault(tuple(key), []).append(f'detection:{reason}')
        else:
            key_reasons.setdefault((item['scene_id'], item['id']), []).append(reason)
    asked = {(t['scene_id'], t['id']) for t in remaining}
    return {'pool': keys, 'pool_size': len(pool), 'remaining': len(remaining), 'key_reasons': key_reasons, 'copy_reasons': copy_reasons,
            'asked': asked}


def run_counter_json(path):
    """The live count read once (scripts.ml.training.sam3_mac.r5_precision count --json --block v2) to a file; None when it fails."""
    import subprocess
    cmd = [sys.executable, '-m', 'scripts.ml.training.sam3_mac.r5_precision', 'count', '--json', '--block', 'v2']
    with open(path, 'w') as out, open(str(path)+'.err', 'w') as err:
        code = subprocess.call(cmd, stdout=out, stderr=err)
    return _read_json(path) if code == 0 else None


# --- hints -------------------------------------------------------------------------------------------------------

def worklist_hints(text):
    hints = {}
    for scene, _oid, slice_id, area, src_scene, src_oid, left, cls, hint in HINT_LINE.findall(text):
        hints[(scene, slice_id)] = {'class': cls or None, 'text': hint or None, 'left_alone': left, 'frozen_key': [src_scene, src_oid],
                                    'worklist_area_m2': float(area), 'source': WORKLIST.name}
    return hints


def fieldwork_hints(payload):
    out = {}
    for item in (payload or {}).get('objects') or []:
        out[(item.get('scene_id'), item.get('object_id'))] = {'class': {'roof': 'ROOF', 'not-roof': 'NOT-ROOF', 'uncertain': 'UNCERTAIN'}.get(item.get('decision'), item.get('decision')),
                                                               'text': item.get('rationale'), 'source': FIELDWORK_DECISIONS.name}
    return out


# --- bodies --------------------------------------------------------------------------------------------------------

class Measurer:
    """measured_transform per (source scene, window), cached; the window PNG bytes read once."""

    def __init__(self, root, design, log):
        self.root, self.design, self.log = root, design, log
        self.scenes, self.images, self.transforms, self.reviews = {}, {}, {}, {}

    def scene(self, scene_id):
        if scene_id not in self.scenes:
            self.scenes[scene_id] = sr._scene(self.root, scene_id)
        return self.scenes[scene_id]

    def review(self, scene_id):
        if scene_id not in self.reviews:
            self.reviews[scene_id] = sr._review(self.root, scene_id)
        return self.reviews[scene_id]

    def image(self, scene):
        if scene['id'] not in self.images:
            self.images[scene['id']] = sr._image(self.root, scene)
        return self.images[scene['id']]

    def transform(self, source_id, target):
        key = (source_id, target['id'])
        if key not in self.transforms:
            source = self.scene(source_id)
            try:
                ft = sr.measured_transform(source, target, self.image(source), self.image(target), self.design, role=sr.SUBJECT_ROLE)
                ok, reasons = sr.verdict(ft, self.design[0])
                ok0, reasons0 = sr.verdict({**ft, 'role': None}, self.design[0], role=None)
                self.transforms[key] = {'ft': ft, 'verified': ok, 'reasons': reasons, 'verified_no_role': ok0, 'reasons_no_role': reasons0, 'error': None}
            except Exception as error:  # noqa: BLE001 - recorded per candidate
                self.transforms[key] = {'ft': None, 'verified': False, 'reasons': [repr(error)], 'verified_no_role': False, 'reasons_no_role': [], 'error': repr(error)}
        return self.transforms[key]


def measure_body(measurer, window, slice_px, lot_px, body_scene_id, body_obj, origin):
    """One candidate body measured into the window: transform, projection, cover, frame position, app ring, source facts."""
    target = window
    side_w, side_h = int(target['image_width']), int(target['image_height'])
    source = measurer.scene(body_scene_id)
    row = {'source_scene_id': body_scene_id, 'object_id': body_obj['id'], 'origin': sorted(origin),
           'source_model_signature': source.get('model_signature'), 'source_image_sha256': source.get('image_sha256'),
           'source_m_per_px': _r(sr.pixel_spacing(source), 5), 'source_registration': registration(source),
           'source_area_m2': (body_obj.get('model') or {}).get('area_m2'), 'source_kind': body_obj.get('kind'), 'source_status': body_obj.get('status'),
           'source_touching_frame': touching_frame(body_obj.get('ring') or (), int(source['image_width']), int(source['image_height']))}
    rv = measurer.review(body_scene_id)
    rv_obj = next((o for o in (rv or {}).get('objects') or [] if o.get('id') == body_obj['id']), None) if rv else None
    row['source_review_status'] = rv_obj.get('status') if rv_obj else None
    row['source_review_geometry_reviewed'] = bool(rv_obj.get('geometry_reviewed')) if rv_obj else None
    row['source_review_ring_equals_model_ring'] = (rv_obj.get('ring') == body_obj.get('ring')) if rv_obj else None
    measured = measurer.transform(body_scene_id, target)
    row.update(verified_subject_role=measured['verified'], reasons_subject_role=measured['reasons'], verified_no_role=measured['verified_no_role'],
               reasons_no_role=measured['reasons_no_role'], transform_error=measured['error'])
    ft = measured['ft']
    if ft is None:
        row.update(eligible=False, ineligible_because=['transform_error'])
        return row
    row.update(overlap=ft['overlap'], residual=ft['residual'], metre_offset=ft['metre_offset'], pixel_affine=ft['pixel_affine'],
               design_sha256=ft['design']['design_sha256'])
    proj = sr.project_object(body_obj, ft)
    ring, holes = proj['ring'], proj.get('holes') or []
    body = _polygon(proj)
    row.update(projected_ring=[[round(v, 3) for v in p] for p in ring], projected_holes=[[[round(v, 3) for v in p] for p in h] for h in holes],
               vertices=len(ring), holes_n=len(holes), body_valid=bool(body.is_valid and body.area > 0))
    row['touching_frame'] = touching_frame(ring, side_w, side_h)
    row['inside_frame'] = not row['touching_frame'] and all(0 <= p[0] <= side_w and 0 <= p[1] <= side_h for p in ring)
    margin_px = min_edge_margin(ring, min(side_w, side_h)) if ring else None
    row['min_edge_margin_px'] = _r(margin_px, 2)
    row['min_edge_margin_m'] = _r(margin_px*sr.pixel_spacing(target), 2) if margin_px is not None else None
    row['inside_with_edge_margin_m'] = bool(margin_px is not None and margin_px*sr.pixel_spacing(target) >= WINDOW['edge_margin_m'])
    row['body_area_px'] = _r(body.area, 2)
    row['body_area_m2'] = _r(px_to_m_area(ring, target), 2)
    inter = body.intersection(slice_px).area if body.area > 0 else 0.
    row['slice_cover'] = _r(inter/slice_px.area) if slice_px.area else None
    row['body_share_in_slice'] = _r(inter/body.area) if body.area else None
    row['lot_overlap_of_body'] = _r(body.intersection(lot_px).area/body.area) if (lot_px is not None and body.area) else None
    rounded = [[round(v, 2) for v in p] for p in ring]
    fr, fh, issues, repairs = finalise_app_ring(rounded, [[[round(v, 2) for v in p] for p in h] for h in holes], side_w, side_h)
    row.update(app_ring_issues=issues, app_ring_repairs=repairs, finalised_vertices=len(fr))
    why = []
    if not row['verified_subject_role']:
        why.append('transform_not_verified')
    if not row['inside_frame']:
        why.append('touches_window_frame')
    if row['source_touching_frame']:
        why.append('cut_at_source_frame')
    if not row['body_valid']:
        why.append('degenerate_body')
    if issues:
        why.append('app_ring_issues')
    if not row['slice_cover']:
        why.append('no_cover')
    row.update(eligible=not why, ineligible_because=why)
    return row


def body_tier(best):
    """'whole' = a verified in-frame whole body covering >= 0.8 of the slice (the takeover's cover_min); 'partial' = 0.5-0.8; 'low' =
    below 0.5 (a neighbouring roof that overlaps the slice, not the slice's roof); 'none' = no eligible body."""
    if best is None:
        return 'none'
    cover = best.get('slice_cover') or 0.
    return 'whole' if cover >= COVER_HIGH else 'partial' if cover >= COVER_HALF else 'low'


def rank_key(c):
    return (-(c.get('slice_cover') or 0.), -(c.get('lot_overlap_of_body') if c.get('lot_overlap_of_body') is not None else -1.),
            c.get('source_m_per_px') or 9., c['source_scene_id'], c['object_id'])


# --- renders --------------------------------------------------------------------------------------------------------

def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_ring(draw, ring, offset, colour, width):
    pts = [(p[0]-offset[0], p[1]-offset[1]) for p in ring]
    if len(pts) >= 2:
        draw.line([*pts, pts[0]], fill=colour, width=width, joint='curve')


def draw_dotted(draw, ring, offset, colour, dash=4, gap=5, width=1):
    pts = [(p[0]-offset[0], p[1]-offset[1]) for p in ring]
    for a, b in zip(pts, [*pts[1:], pts[0]], strict=True):
        length = math.hypot(b[0]-a[0], b[1]-a[1])
        if length <= 0:
            continue
        ux, uy = (b[0]-a[0])/length, (b[1]-a[1])/length
        pos = 0.
        while pos < length:
            end = min(pos+dash, length)
            draw.line([(a[0]+ux*pos, a[1]+uy*pos), (a[0]+ux*end, a[1]+uy*end)], fill=colour, width=width)
            pos += dash+gap


def render_slice(image, row, best, own, lot_px, path):
    """Native-resolution crop: slice thin red, own best detection thin cyan, best body bold green, Lot parcel dotted, titled."""
    geoms = [Polygon(row['ring_px'])]
    if best:
        geoms.append(Polygon(best['projected_ring']))
    if own and own.get('ring'):
        geoms.append(Polygon(own['ring']))
    bounds = np.array([g.bounds for g in geoms if not g.is_empty])
    x0, y0 = bounds[:, 0].min(), bounds[:, 1].min()
    x1, y1 = bounds[:, 2].max(), bounds[:, 3].max()
    pad = 40
    w, h = image.size
    cx0, cy0 = max(0, int(math.floor(x0-pad))), max(0, int(math.floor(y0-pad)))
    cx1, cy1 = min(w, int(math.ceil(x1+pad))), min(h, int(math.ceil(y1+pad)))
    crop = image.crop((cx0, cy0, cx1, cy1)).convert('RGB')
    draw = ImageDraw.Draw(crop)
    offset = (cx0, cy0)
    if lot_px is not None and not lot_px.is_empty:
        polys = list(lot_px.geoms) if lot_px.geom_type == 'MultiPolygon' else [lot_px]
        for poly in polys:
            draw_dotted(draw, list(poly.exterior.coords), offset, (255, 230, 80), width=1)
    if own and own.get('ring'):
        draw_ring(draw, own['ring'], offset, (0, 220, 255), 1)
        for hole in own.get('holes') or []:
            draw_ring(draw, hole, offset, (0, 220, 255), 1)
    draw_ring(draw, row['ring_px'], offset, (255, 40, 40), 1)
    if best:
        draw_ring(draw, best['projected_ring'], offset, (40, 230, 60), 3)
        for hole in best.get('projected_holes') or []:
            draw_ring(draw, hole, offset, (40, 230, 60), 2)
    lines = [f"{row['window']} · slice {row['slice_id']} · {row['area_m2']} m2 · {row['status']}"
             + (f" · decision {row['decision']}" if row['decision'] else '') + (' · TARGET' if row['is_frozen_target'] else ' · non-target')
             + (f" · slice touches window {','.join(row['slice_touching_window_frame'])}" if row.get('slice_touching_window_frame') else ''),
             (f"body {best['source_scene_id']}/{best['object_id']} · tier {row['body_tier']} · cover {best['slice_cover']:.3f} · {best['body_area_m2']} m2 · "
              f"shift {best['residual']['shift_m']} m · ncc {best['residual']['ncc']} · lot {best['lot_overlap_of_body']} · v{best['vertices']}") if best else 'body: none',
             ((f"own best detection {own['id']} cover {own['cover']:.3f}" + (f" touches {own['touching_frame']}" if own.get('touching_frame') else '')) if own else 'own detection: none')
             + (f" · hint {row['hint']['class']}" if row.get('hint') and row['hint'].get('class') else ''),
             'legend: red thin = slice · green bold = best carried body · cyan thin = own best detection · dotted = Lot parcel']
    font = _font(13)
    hd0 = ImageDraw.Draw(crop)
    width = int(max(crop.width, 640, max(hd0.textlength(t, font=font) for t in lines)+12))
    header = 18*len(lines)+6
    canvas = Image.new('RGB', (width, crop.height+header), (18, 18, 18))
    canvas.paste(crop, (0, header))
    hd = ImageDraw.Draw(canvas)
    for i, text in enumerate(lines):
        hd.text((6, 3+18*i), text, fill=(235, 235, 235) if i < 3 else (160, 160, 160), font=font)
    canvas.save(path)
    return {'path': str(path), 'crop': [cx0, cy0, cx1, cy1], 'size': [canvas.width, canvas.height]}


def contact_sheet(paths, out, cell=420, columns=4):
    if not paths:
        return None
    thumbs = []
    for p in paths:
        im = Image.open(p).convert('RGB')
        scale = cell/im.width
        thumbs.append(im.resize((cell, max(1, int(im.height*scale)))))
    row_h = max(t.height for t in thumbs)
    rows = math.ceil(len(thumbs)/columns)
    sheet = Image.new('RGB', (cell*columns, row_h*rows), (10, 10, 10))
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % columns)*cell, (i//columns)*row_h))
    sheet.save(out)
    return str(out)


# --- the census -----------------------------------------------------------------------------------------------------

def census(root=ROOT, out_dir=OUT_DIR, run_dir=RUN_DIR, *, render=True, counter_json=None):
    out_dir, run_dir = Path(out_dir), Path(run_dir)
    (out_dir/'renders').mkdir(parents=True, exist_ok=True)
    (out_dir/'bodies').mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir/'census.log'
    log_lines = []

    def log(msg):
        line = f'{_now()} {msg}'
        log_lines.append(line)
        with open(log_path, 'a') as h:
            h.write(line+'\n')

    design = sr.load_design(root)
    values, design_sha = design
    window_rule, window_sha = load_window_rule(root)
    rules_sha = _sha(Path(RULES_PATH).read_bytes()) if Path(RULES_PATH).exists() else None
    worklist_text = WORKLIST.read_text() if WORKLIST.exists() else ''
    hints = worklist_hints(worklist_text)
    fw_hints = fieldwork_hints(_read_json(FIELDWORK_DECISIONS))
    registry_rows = [json.loads(line) for line in REGISTRY.open()] if REGISTRY.exists() else []
    registry_by_window = {}
    for r in registry_rows:
        registry_by_window.setdefault(r.get('window_scene_id'), []).append(r)

    counter_path = run_dir/'counter.json'
    counter = counter_json or _read_json(counter_path) or run_counter_json(counter_path)
    if counter is None:
        log('counter unavailable: carried_rows figures will be null')
    counter_rows = {(r['scene_id'], r['id']): r for r in (counter or {}).get('carried_rows') or []}
    counter_asked = {(s['scene_id'], o['id']) for s in (counter or {}).get('next_scenes') or [] for o in s['objects']}
    counter_counted = {(r['scene_id'], r['id']): r for r in (counter or {}).get('already_counted') or []}
    pool = frozen_pool(log)

    measurer = Measurer(root, design, log)
    scenes_dir = root/'data/scenes'
    windows = sorted(p.name for p in scenes_dir.iterdir() if p.name.startswith(WINDOW_PREFIXES) and (p/'latest.json').exists())
    log(f'windows {len(windows)}; design {design_sha[:16]}; window rule {window_sha[:16]}; rules {str(rules_sha)[:16]}')

    # 1. population
    rows, bodies_applied = [], []
    for wid in windows:
        latest = measurer.scene(wid)
        review = measurer.review(wid)
        if not isinstance(latest, dict) or not isinstance(review, dict):
            continue
        stale = review.get('model_signature') != latest.get('model_signature')
        plan = takeover_plan(review, latest)
        left = {entry['copy_id']: entry for entry in plan['left_alone']}
        for obj in review.get('objects') or []:
            cf = obj.get('carried_from')
            if not isinstance(cf, dict) or not cf or obj.get('status') == 'superseded':
                continue
            entry = {'window': wid, 'slice_id': obj['id'], 'kind': obj.get('kind'), 'carried_role': cf.get('role'),
                     'frozen_key': [cf.get('scene_id'), cf.get('object_id')], 'frozen_model_signature': cf.get('model_signature'),
                     'area_m2': (obj.get('model') or {}).get('area_m2'), 'ring_area_m2': _r(px_to_m_area(obj.get('ring') or [], latest), 2),
                     'status': obj.get('status'), 'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'decision': decision_of(obj),
                     'agent_note': bool(sr.AGENT_NOTES.match(obj.get('notes') or '')), 'notes_head': (obj.get('notes') or '')[:120],
                     'replaces': obj.get('replaces') or [], 'replacement_ids': obj.get('replacement_ids') or [],
                     'review_revision': review.get('revision'), 'review_updated_by': review.get('updated_by'), 'review_model_signature': review.get('model_signature'),
                     'latest_model_signature': latest.get('model_signature'), 'stale_review': stale, 'url': URL.format(scene=wid, oid=obj['id']),
                     'ring_px': obj.get('ring'), 'holes_px': obj.get('holes') or [], 'takeover_left_alone': left.get(obj['id']),
                     'slice_touching_window_frame': touching_frame(obj.get('ring') or (), int(latest['image_width']), int(latest['image_height']))}
            if cf.get('role') == 'whole_roof_body' or obj.get('source') == 'carried-body-v1':
                bodies_applied.append(entry)
            else:
                rows.append(entry)
    log(f'slices {len(rows)}; carried bodies already applied {len(bodies_applied)}')

    # 2. the registered index around every slice's frozen source ring
    slice_geos = {}
    for row in rows:
        src_id, src_oid = row['frozen_key']
        snapshot = None
        folder = scenes_dir/src_id
        if row['frozen_model_signature']:
            snapshot = _read_json(folder/'models'/f"{row['frozen_model_signature']}.json")
        if not isinstance(snapshot, dict):
            snapshot = measurer.scene(src_id)
            row['source_ring_from'] = 'latest.json'
        else:
            row['source_ring_from'] = f"models/{row['frozen_model_signature'][:12]}.json"
        src_obj = next((o for o in (snapshot or {}).get('objects') or [] if o.get('id') == src_oid), None) if snapshot else None
        geo = _geo(src_obj, snapshot) if src_obj else None
        if geo is None:
            geo = _geo({'ring': row['ring_px'], 'holes': row['holes_px']}, measurer.scene(row['window']))
            row['source_ring_from'] = 'window copy (source object not found)'
        row['source_touching_frame'] = touching_frame(src_obj.get('ring') or (), int(snapshot['image_width']), int(snapshot['image_height'])) if src_obj and snapshot else None
        slice_geos[(row['window'], row['slice_id'])] = geo
    pad = WINDOW['index_pad_deg']/4
    boxes = [box(*pad_bounds(list(g.bounds), pad)) for g in slice_geos.values() if g is not None]
    index = RegisteredIndex(root).load(boxes)
    log(f'index frames {len(index.frames)} with {sum(len(f["buildings"]) for f in index.frames)} building outlines')
    parcels = {}
    for path in sorted(scenes_dir.glob('*/latest.json')):
        if path.parent.name.startswith(SCRATCH_PREFIX):
            continue
        scene = measurer.scenes.get(path.parent.name) or _read_json(path)
        feats = (((scene or {}).get('property_roofs') or {}).get('parcels') or {}).get('features') or []
        for f in feats:
            pfi = (f.get('properties') or {}).get('pfi')
            if pfi and pfi not in parcels:
                try:
                    from shapely.geometry import shape
                    parcels[pfi] = shape(f['geometry'])
                except Exception:  # noqa: BLE001
                    pass
    log(f'parcel outlines by pfi {len(parcels)}')

    # 3. per slice: own detection, hints, counter, bodies
    renders = []
    for row in rows:
        wid, sid = row['window'], row['slice_id']
        latest = measurer.scene(wid)
        side_w, side_h = int(latest['image_width']), int(latest['image_height'])
        slice_px = _polygon({'ring': row['ring_px'], 'holes': row['holes_px']})
        # own detections of the window's model (latest.json buildings)
        own_best = None
        for det in latest.get('objects') or []:
            if det.get('kind') != 'building' or det.get('status') in ('rejected', 'superseded'):
                continue
            poly = _polygon(det)
            if poly.area <= 0 or slice_px.area <= 0:
                continue
            cover = poly.intersection(slice_px).area/slice_px.area
            if cover > 0 and (own_best is None or cover > own_best['cover']):
                own_best = {'id': det['id'], 'cover': cover, 'area_m2': (det.get('model') or {}).get('area_m2'),
                            'touching_frame': touching_frame(det.get('ring') or (), side_w, side_h), 'ring': det.get('ring'), 'holes': det.get('holes') or [],
                            'in_review': any(o.get('id') == det['id'] for o in (measurer.review(wid) or {}).get('objects') or [])}
        row['own_best_detection'] = None if own_best is None else {k: (_r(v) if k == 'cover' else v) for k, v in own_best.items() if k not in ('ring', 'holes')}
        row['own_detections_n'] = sum(1 for d in latest.get('objects') or [] if d.get('kind') == 'building')
        # hints
        hint = hints.get((wid, sid))
        if hint is None:
            fw = fw_hints.get(tuple(row['frozen_key']))
            hint = {**fw, 'left_alone': None, 'fallback': True} if fw else None
        row['hint'] = hint
        # counter
        crow = counter_rows.get((wid, sid))
        row['counter'] = None if crow is None else {k: crow.get(k) for k in ('class', 'status', 'primary', 'refusals', 'fresh_state', 'copies', 'not_counted_reason', 'snapshot_area_m2')}
        row['counter_asked'] = (wid, sid) in counter_asked
        row['counter_counted'] = (wid, sid) in counter_counted
        key = tuple(row['frozen_key'])
        if pool is not None:
            row['is_frozen_target'] = key in pool['pool']
            row['target_key_exclusions'] = pool['key_reasons'].get(key, [])
            row['target_copy_exclusions'] = pool['copy_reasons'].get((wid, sid), [])
            row['target_asked_now'] = (wid, sid) in pool['asked']
        else:
            row['is_frozen_target'] = bool(crow and crow.get('class') != 'excluded' or row['counter_asked'] or row['counter_counted'])
            row['target_key_exclusions'], row['target_copy_exclusions'], row['target_asked_now'] = None, None, row['counter_asked']
        if not row['is_frozen_target']:
            row['counting_consequence_class'] = 'non_target'
            row['counting_consequence'] = CONSEQUENCE['non_target']
        elif row['decision'] in ('confirmed', 'rejected'):
            row['counting_consequence_class'] = 'target_decided'
            row['counting_consequence'] = CONSEQUENCE['target_decided']
        elif row['target_asked_now'] or (pool is None and row['counter_asked']):
            row['counting_consequence_class'] = 'target_undecided'
            row['counting_consequence'] = CONSEQUENCE['target_undecided']
        else:
            reasons = row['target_copy_exclusions'] or ([crow['class']] if crow else ['unknown'])
            row['counting_consequence_class'] = 'target_excluded'
            row['counting_consequence'] = CONSEQUENCE['target_excluded'].format(reason=', '.join(map(str, reasons)))
        # lot parcel
        lots = (latest.get('window_frame') or {}).get('lots') or []
        lot_px, lot_label = None, None
        if lots:
            pfi = lots[0].get('pfi')
            if pfi in parcels:
                lot_px = geo_to_px_polygon(latest, parcels[pfi])
                lot_label = {'pfi': pfi, 'lot': lots[0].get('lot'), 'plan': lots[0].get('plan'), 'share': lots[0].get('share')}
        row['lot'] = lot_label
        # candidate bodies: index (geo cover of the frozen source ring) + the planner's bodies for this subject
        geo = slice_geos[(wid, sid)]
        candidates = {}
        if geo is not None:
            lon, lat = geo.centroid.x, geo.centroid.y
            project, _ = metre_frame(lon, lat)
            slice_m = shp_transform(project, geo)
            exclude = {wid, row['frozen_key'][0]}
            for body_scene, body_id, body_geo in index.buildings_near(pad_bounds(list(geo.bounds), pad), row['frozen_key'][0]):
                if body_scene in exclude:
                    continue
                body_m = shp_transform(project, body_geo)
                if body_m.area <= 0 or slice_m.area <= 0:
                    continue
                geo_cover = body_m.intersection(slice_m).area/slice_m.area
                if geo_cover >= GEO_COVER_MIN:
                    candidates.setdefault((body_scene, body_id), {'origin': set(), 'geo_cover': geo_cover})['origin'].add('index')
            row['index_bodies_intersecting'] = sum(1 for s, _, g in index.buildings_near(pad_bounds(list(geo.bounds), pad), row['frozen_key'][0])
                                                   if s not in exclude and shp_transform(project, g).intersects(slice_m))
        for subject in (latest.get('window_frame') or {}).get('subjects') or []:
            if subject.get('object_id') != row['frozen_key'][1] or subject.get('scene_id') != row['frozen_key'][0]:
                continue
            for b in subject.get('bodies') or []:
                if b['scene_id'] == wid:
                    continue
                candidates.setdefault((b['scene_id'], b['object_id']), {'origin': set(), 'geo_cover': None})['origin'].add('planner')
        measured = []
        for (body_scene, body_id), meta in sorted(candidates.items()):
            source = measurer.scene(body_scene)
            if not isinstance(source, dict):
                measured.append({'source_scene_id': body_scene, 'object_id': body_id, 'origin': sorted(meta['origin']), 'transform_error': 'source scene missing', 'eligible': False, 'ineligible_because': ['source_missing']})
                continue
            body_obj = next((o for o in source.get('objects') or [] if o.get('id') == body_id), None)
            if body_obj is None:
                measured.append({'source_scene_id': body_scene, 'object_id': body_id, 'origin': sorted(meta['origin']), 'transform_error': 'object not in source latest.json', 'eligible': False, 'ineligible_because': ['object_missing']})
                continue
            try:
                c = measure_body(measurer, latest, slice_px, lot_px, body_scene, body_obj, meta['origin'])
            except Exception as error:  # noqa: BLE001
                log(f'{wid}/{sid} body {body_scene}/{body_id}: {error!r}')
                c = {'source_scene_id': body_scene, 'object_id': body_id, 'origin': sorted(meta['origin']), 'transform_error': repr(error), 'eligible': False, 'ineligible_because': ['measure_error']}
            c['geo_cover_of_source_ring'] = _r(meta['geo_cover'])
            measured.append(c)
        eligible = sorted((c for c in measured if c.get('eligible')), key=rank_key)
        best = eligible[0] if eligible else None
        row['bodies'] = {'candidates_measured': len(measured), 'verified': sum(1 for c in measured if c.get('verified_subject_role')),
                         'inside_frame': sum(1 for c in measured if c.get('inside_frame')), 'eligible': len(eligible),
                         'cut_at_source_frame': sum(1 for c in measured if c.get('source_touching_frame')),
                         'eligible_cover_ge_0_8': sum(1 for c in eligible if (c.get('slice_cover') or 0) >= COVER_HIGH),
                         'eligible_cover_ge_0_5': sum(1 for c in eligible if (c.get('slice_cover') or 0) >= COVER_HALF),
                         'best': None if best is None else {k: best.get(k) for k in ('source_scene_id', 'object_id', 'slice_cover', 'body_area_m2', 'source_area_m2', 'vertices',
                                                                                     'inside_frame', 'min_edge_margin_px', 'min_edge_margin_m', 'lot_overlap_of_body', 'app_ring_issues',
                                                                                     'source_review_status', 'source_m_per_px', 'origin', 'geo_cover_of_source_ring', 'body_share_in_slice')},
                         'ranking': [[c['source_scene_id'], c['object_id'], c.get('slice_cover'), c.get('lot_overlap_of_body')] for c in eligible],
                         'not_eligible': [[c['source_scene_id'], c['object_id'], c.get('slice_cover'), c.get('ineligible_because')] for c in measured if not c.get('eligible')]}
        if best is not None:
            row['bodies']['best'].update(shift_m=best['residual']['shift_m'], ncc=best['residual']['ncc'], region_side_px=best['overlap']['region_px']['side_px'],
                                        overlap_fraction=best['overlap']['fraction'])
        row['best_body'] = 'none' if best is None else f"{best['source_scene_id']}/{best['object_id']}"
        row['body_tier'] = body_tier(best)
        body_file = out_dir/'bodies'/f'{wid}.{sid}.json'
        body_file.write_text(json.dumps({'version': VERSION, 'written_at': _now(), 'window': wid, 'slice_id': sid, 'frozen_key': row['frozen_key'],
                                         'window_model_signature': latest.get('model_signature'), 'window_image_sha256': latest.get('image_sha256'),
                                         'review_revision': row['review_revision'], 'review_model_signature': row['review_model_signature'],
                                         'design_sha256': design_sha, 'window_sha256': window_sha, 'ranking_rule': RANKING_RULE,
                                         'slice_ring_px': row['ring_px'], 'slice_holes_px': row['holes_px'], 'slice_area_px': _r(slice_px.area, 2),
                                         'lot': lot_label, 'best': None if best is None else [best['source_scene_id'], best['object_id']],
                                         'candidates': measured}, indent=1))
        row['bodies_file'] = str(body_file)
        if render and best is not None:
            try:
                image = Image.open(scenes_dir/wid/f"{latest['image_sha256']}.png")
                info = render_slice(image, row, best, own_best, lot_px, out_dir/'renders'/f'{wid}.{sid}.png')
                row['render'] = info['path']
                renders.append(info['path'])
            except Exception as error:  # noqa: BLE001
                log(f'{wid}/{sid} render failed: {error!r}')
                row['render'] = None
        else:
            row['render'] = None
        log(f"{wid}/{sid} target={row['is_frozen_target']} status={row['status']} own={row['own_best_detection'] and row['own_best_detection']['cover']} "
            f"bodies measured={len(measured)} eligible={len(eligible)} best={row['best_body']} cover={best and best['slice_cover']}")

    sheet = contact_sheet(renders, out_dir/'renders'/'contact-sheet.png') if render else None

    # 4. summary
    by_status = Counter(r['status'] for r in rows)
    by_decision = Counter(str(r['decision']) for r in rows)
    targets_n = sum(1 for r in rows if r['is_frozen_target'])
    with_body = [r for r in rows if r['bodies']['best']]
    hint_classes = Counter((r['hint'] or {}).get('class') or 'none' for r in rows)
    stale_rows = [r for r in rows if r['stale_review']]
    summary = {
        'slices_total': len(rows), 'by_status': dict(by_status), 'by_decision': dict(by_decision),
        'targets': targets_n, 'non_targets': len(rows)-targets_n,
        'by_consequence': dict(Counter(r['counting_consequence_class'] for r in rows)),
        'with_verified_in_frame_body': len(with_body),
        'with_body_cover_ge_0_8': sum(1 for r in with_body if r['bodies']['best']['slice_cover'] >= COVER_HIGH),
        'with_body_cover_ge_0_5': sum(1 for r in with_body if r['bodies']['best']['slice_cover'] >= COVER_HALF),
        'targets_with_body_cover_ge_0_8': sum(1 for r in with_body if r['is_frozen_target'] and r['bodies']['best']['slice_cover'] >= COVER_HIGH),
        'non_targets_with_body_cover_ge_0_8': sum(1 for r in with_body if not r['is_frozen_target'] and r['bodies']['best']['slice_cover'] >= COVER_HIGH),
        'hint_classes': dict(hint_classes),
        'body_tiers': dict(Counter(r['body_tier'] for r in rows)),
        'slices_touching_window_frame': [[r['window'], r['slice_id'], r['slice_touching_window_frame']] for r in rows if r['slice_touching_window_frame']],
        'own_detection': {'none': sum(1 for r in rows if r['own_best_detection'] is None),
                          'below_0_8': sum(1 for r in rows if r['own_best_detection'] and r['own_best_detection']['cover'] < COVER_HIGH),
                          'ge_0_8': sum(1 for r in rows if r['own_best_detection'] and r['own_best_detection']['cover'] >= COVER_HIGH)},
        'stale_windows': sorted({r['window'] for r in stale_rows}), 'stale_window_slices': [[r['window'], r['slice_id'], r['status'], r['best_body']] for r in stale_rows],
        'bodies_already_applied': [[b['window'], b['slice_id'], b['frozen_key'], b['status'], b['replaces']] for b in bodies_applied],
        'renders': len(renders), 'contact_sheet': sheet,
    }
    header = {'version': VERSION, 'written_at': _now(), 'windows': len(windows), 'design_sha256': design_sha, 'window_sha256': window_sha,
              'rules_sha256': rules_sha, 'design_values': {k: values[k] for k in sr.DESIGN_KEYS}, 'subject_floor': sr.subject_floor(values),
              'takeover_rule': TAKEOVER, 'geo_cover_min_for_index_bodies': GEO_COVER_MIN, 'ranking_rule': RANKING_RULE,
              'counter': None if counter is None else {k: counter.get(k) for k in ('computed_at', 'primary_n', 'targets_total', 'targets_remaining', 'rules_sha256', 'code_sha256')},
              'counter_json': str(counter_path), 'frozen_pool': None if pool is None else {'pool_size': pool['pool_size'], 'remaining': pool['remaining']},
              'worklist': {'path': str(WORKLIST), 'sha256': _sha(worklist_text.encode()) if worklist_text else None, 'hint_lines': len(hints)},
              'registry_rows': len(registry_rows), 'registry_roles': dict(Counter(r.get('role') for r in registry_rows)),
              'index_frames': len(index.frames), 'wording': ('counts and geometry only; a Codex hint is never a label; a verified body is a candidate '
                                                            'for a future carry, not a decision; nothing written under data/scenes')}
    slim = []
    for r in rows:
        slim.append({k: v for k, v in r.items() if k not in ('ring_px', 'holes_px')})
    payload = {'header': header, 'summary': summary, 'slices': slim, 'bodies_already_applied': [{k: v for k, v in b.items() if k not in ('ring_px', 'holes_px')} for b in bodies_applied]}
    (out_dir/'census.json').write_text(json.dumps(payload, indent=1, default=str))
    (out_dir/'census.md').write_text(render_md(header, summary, slim, bodies_applied))
    log(f"written {out_dir/'census.json'} and census.md; renders {len(renders)}")
    return payload


def render_md(header, summary, rows, bodies_applied):
    def f(v, d=3):
        return '-' if v is None else (f'{v:.{d}f}' if isinstance(v, float) else str(v))
    lines = [f"# Carried slice body census, 2026-10-06 ({header['written_at']})", '',
             f"Population: review.json objects of the {header['windows']} windows (data/scenes/lot-*, win-*) with a non-empty carried_from and status "
             f"!= superseded, role frame_cut_target: **{summary['slices_total']} slices** ({', '.join(f'{k} {v}' for k, v in sorted(summary['by_status'].items()))}); "
             f"frozen targets {summary['targets']}, non-targets {summary['non_targets']}. Carried bodies already applied: {len(bodies_applied)} "
             f"({'; '.join(f'{b[0]}/{b[1]} from {b[2][0]}/{b[2][1]} ({b[3]}, replaces {b[4]})' for b in summary['bodies_already_applied']) or 'none'}).", '',
             f"DESIGN {header['design_sha256'][:16]} ({header['design_values']}, subject floor {header['subject_floor']}); WINDOW {header['window_sha256'][:16]}; "
             f"RULES {str(header['rules_sha256'])[:16]}; takeover rule {header['takeover_rule']}; counter computed_at {(header['counter'] or {}).get('computed_at')} "
             f"(N {(header['counter'] or {}).get('primary_n')}, targets {(header['counter'] or {}).get('targets_total')}, remaining {(header['counter'] or {}).get('targets_remaining')}); "
             f"registered frames in the index {header['index_frames']}; worklist hint lines {header['worklist']['hint_lines']}.", '',
             '## Summary', '',
             f"- slices total {summary['slices_total']}; by status {summary['by_status']}; by decision {summary['by_decision']}",
             f"- frozen targets {summary['targets']} / non-targets {summary['non_targets']}; counting consequence classes {summary['by_consequence']}",
             f"- with a verified, in-frame, whole (not cut at its own frame) body: {summary['with_verified_in_frame_body']}; of these cover >= 0.8: "
             f"{summary['with_body_cover_ge_0_8']} (targets {summary['targets_with_body_cover_ge_0_8']}, non-targets {summary['non_targets_with_body_cover_ge_0_8']}); cover >= 0.5: {summary['with_body_cover_ge_0_5']}",
             f"- the window's own best building detection over the slice: none {summary['own_detection']['none']}, below 0.8 {summary['own_detection']['below_0_8']}, >= 0.8 {summary['own_detection']['ge_0_8']}",
             f"- body tiers (whole >= 0.8 cover, partial 0.5-0.8, low < 0.5 = a neighbouring roof overlapping the slice, none): {summary['body_tiers']}",
             f"- slices whose ring still touches the window's own frame (takeover tolerance): {summary['slices_touching_window_frame'] or 'none'}",
             f"- Codex hint classes (hints only, never labels): {summary['hint_classes']}",
             f"- stale windows (review on an older snapshot than latest.json): {summary['stale_windows']}; their slices: {summary['stale_window_slices']}",
             f"- renders {summary['renders']} (every slice with a verified body) + contact sheet {summary['contact_sheet']}", '',
             f"Ranking rule: {header['ranking_rule']}.", '',
             '## Slices (one row each)', '',
             '| window | slice | frozen key | m2 | status | decision | target | counter class | own best det (cover) | hint | best body | tier | verified | inside | cover | body m2 | shift m | ncc | lot ov | app issues | consequence |',
             '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        b = r['bodies']['best']
        own = r['own_best_detection']
        hint = (r['hint'] or {}).get('class') or '-'
        lines.append('| ' + ' | '.join([
            r['window'], f"[{r['slice_id']}]({r['url']})", f"{r['frozen_key'][0]}/{r['frozen_key'][1]}", f(r['area_m2'], 2), r['status'] + (' (stale)' if r['stale_review'] else ''),
            r['decision'] or '-', 'yes' if r['is_frozen_target'] else 'no', (r['counter'] or {}).get('class') or '-',
            f"{own['id']} ({own['cover']:.3f}{', touches ' + ','.join(own['touching_frame']) if own['touching_frame'] else ''})" if own else 'none',
            hint, r['best_body'], r['body_tier'], 'yes' if b else '-', ('yes' if b['inside_frame'] else 'no') if b else '-', f(b['slice_cover']) if b else '-',
            f(b['body_area_m2'], 2) if b else '-', f(b['shift_m'], 4) if b else '-', f(b['ncc'], 4) if b else '-',
            f(b['lot_overlap_of_body']) if b and b['lot_overlap_of_body'] is not None else '-', ('ok' if not b['app_ring_issues'] else '; '.join(b['app_ring_issues'])) if b else '-',
            r['counting_consequence_class']]) + ' |')
    lines += ['', '## Per slice', '']
    for r in rows:
        b = r['bodies']
        lines.append(f"### {r['window']} · {r['slice_id']} · {f(r['area_m2'], 2)} m2 · {r['status']} · body tier {r['body_tier']}" + (' · STALE review' if r['stale_review'] else '')
                     + (f" · slice touches window frame {r['slice_touching_window_frame']}" if r['slice_touching_window_frame'] else ''))
        lines.append(f"- frozen key {r['frozen_key'][0]}/{r['frozen_key'][1]} (source ring from {r.get('source_ring_from')}; cut at source edges {r.get('source_touching_frame')}); "
                     f"review revision {r['review_revision']} by '{r['review_updated_by']}'; decision {r['decision'] or 'none (carry default needs_adjustment)'}; agent note {r['agent_note']}")
        lines.append(f"- target: {'yes' if r['is_frozen_target'] else 'no'}; counter {r['counter']}; asked now {r.get('target_asked_now')}; counted {r['counter_counted']}; key exclusions {r.get('target_key_exclusions')}; copy exclusions {r.get('target_copy_exclusions')}")
        own = r['own_best_detection']
        lines.append(f"- own detection: {own if own else 'none'}; takeover left alone: {r['takeover_left_alone']}; building detections in latest.json {r['own_detections_n']}")
        h = r['hint']
        lines.append(f"- hint: {h['class'] if h else '-'}: {(h.get('text') or '-')[:200] if h else '-'}" + (f" (left alone: {h.get('left_alone')})" if h and h.get('left_alone') else '') + (' [fallback: Fieldwork decisions.json]' if h and h.get('fallback') else ''))
        lines.append(f"- lot: {r['lot']}; bodies: measured {b['candidates_measured']} (index bodies intersecting the source ring {r.get('index_bodies_intersecting')}), verified {b['verified']}, inside {b['inside_frame']}, "
                     f"cut at own frame {b['cut_at_source_frame']}, eligible {b['eligible']} (cover >= 0.8: {b['eligible_cover_ge_0_8']}, >= 0.5: {b['eligible_cover_ge_0_5']})")
        if b['best']:
            bb = b['best']
            lines.append(f"- BEST {bb['source_scene_id']}/{bb['object_id']}: cover {f(bb['slice_cover'])}, body {bb['body_area_m2']} m2 (source {bb['source_area_m2']}), {bb['vertices']} vertices, "
                         f"inside (margin {bb['min_edge_margin_px']} px / {bb['min_edge_margin_m']} m), shift {bb['shift_m']} m, ncc {bb['ncc']}, region side {bb['region_side_px']} px, "
                         f"fraction {bb['overlap_fraction']}, lot overlap {bb['lot_overlap_of_body']}, app issues {bb['app_ring_issues'] or 'none'}, source review {bb['source_review_status']}, "
                         f"origin {bb['origin']}, source m/px {bb['source_m_per_px']}; render {r.get('render')}")
            if len(b['ranking']) > 1:
                lines.append(f"- ranking: {b['ranking']}")
        else:
            lines.append('- BEST: none')
        if b['not_eligible']:
            lines.append(f"- not eligible: {b['not_eligible']}")
        lines.append(f"- consequence: {r['counting_consequence']}")
        lines.append(f"- bodies file: {r['bodies_file']}")
        lines.append('')
    lines += ['## Wording', '', header['wording'], '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--out-dir', type=Path, default=OUT_DIR)
    parser.add_argument('--run-dir', type=Path, default=RUN_DIR)
    parser.add_argument('--no-render', action='store_true')
    args = parser.parse_args(argv)
    try:
        payload = census(ROOT, args.out_dir, args.run_dir, render=not args.no_render)
    except Exception:
        traceback.print_exc()
        return 1
    print(json.dumps(payload['summary'], indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
