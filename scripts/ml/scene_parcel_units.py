"""Plan (read-only) splitting saved building outlines that span several numbered Vicmap lots into one outline per unit.

Units card 2026-10-12 (reviewer: "yes to defaults"):
  1. Unreviewed detector outlines (status candidate, untouched) are split at the lot lines by a pipeline step; each original is
     kept as superseded with replacement_ids -> its units, each unit has derived_from -> the original. A 50-scene sample is
     rendered for the reviewer BEFORE any scene write: this module only plans and renders. The writer is a separate step,
     built on scene_road_promote's discipline (model + review layer, app validation, lock, revision + 1), once the sample is
     approved.
  2. Reviewer-touched outlines (confirmed / needs_adjustment / geometry_reviewed / edited) are never split here: they go to the
     Fieldwork Units queue as proposals (plan rows with route 'queue').

A unit's outline is the original outline cut at the lot line (parcel_units.split_by_lots): the lot line is the title boundary,
not a measured party wall (cadastre vs imagery registration is ~0.5 m). Units under MIN_UNIT_M2 are absorbed (the split is
refused when fewer than two units are left). Strata (overlapping lots) are one building and never split.
"""
from __future__ import annotations

import argparse
import colorsys
import glob
import hashlib
import json
from pathlib import Path

from . import parcel_units as pu

VERSION = 'scene-parcel-units-v4'  # v4 2026-10-19: roof > lot lines; split only when the reviewer confirmed it
_V3 = 'scene-parcel-units-v3'  # v3 2026-10-17: no visible wall -> one building on several titles  # v2: aligned lot lines, units strictly inside their own lot
MIN_UNIT_M2 = 15.0
# A split is automatic only when every cut runs along the roof's own axes (within CUT_AXIS_DEG; a lot line slanting across one
# roof is a title quirk, not a party wall) and the units are comparable (smallest >= MIN_UNIT_RATIO of the largest; a carport on
# an access strip or a rear garage is the doubtful case). Otherwise the split goes to the reviewer's Units queue.
CUT_AXIS_DEG, MIN_UNIT_RATIO = 12.0, 0.35
CUT_EVIDENCE_MIN = 1.0  # see cut_evidence
SPLIT_DECISIONS = Path('data/reviews/eyes/experiments/parcel-units-2026-10-12/split-decisions.json')


def touched(o):
    # boundary_refinement is the model's own envelope step (roof-envelope-v5), not a reviewer edit
    return (o.get('status') in ('confirmed', 'needs_adjustment') or o.get('geometry_reviewed') or o.get('roof_line_edits')
            or o.get('roof_feedback'))


def _axis(ring):
    """Length-weighted dominant edge direction modulo 90 degrees (radians)."""
    import math
    sx = sy = 0.0
    for a, b in zip(ring, ring[1:] + ring[:1]):
        L = math.dist(a, b)
        t = math.atan2(b[1] - a[1], b[0] - a[0])
        sx += L * math.cos(4 * t); sy += L * math.sin(4 * t)
    return math.atan2(sy, sx) / 4


def split_check(ring, units):
    """(automatic, reason): see CUT_AXIS_DEG / MIN_UNIT_RATIO."""
    import math
    from shapely.geometry import Polygon
    areas = [u['m2'] for u in units]
    if min(areas) < MIN_UNIT_RATIO * max(areas):
        return False, f'small piece ({min(areas):.0f} of {max(areas):.0f} m2: a carport, garage or access strip?)'
    roof = Polygon(ring).buffer(0)
    box0 = list(roof.minimum_rotated_rectangle.exterior.coords)[:4]
    e0 = max(((box0[k], box0[k + 1]) for k in range(3)), key=lambda ab: math.dist(*ab))
    axis = math.atan2(e0[1][1] - e0[0][1], e0[1][0] - e0[0][0])  # the roof's rectangle, not its ragged edges
    polys = [Polygon(u['ring']).buffer(0) for u in units]
    on = off = 0.0
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            shared = polys[i].boundary.intersection(polys[j].buffer(0.75))
            for line in getattr(shared, 'geoms', [shared]):
                if line.is_empty or line.geom_type != 'LineString':
                    continue
                pts = list(line.coords)
                for a, b in zip(pts, pts[1:]):
                    L = math.dist(a, b)
                    d = (math.atan2(b[1] - a[1], b[0] - a[0]) - axis) % (math.pi / 2)
                    if math.degrees(min(d, math.pi / 2 - d)) > CUT_AXIS_DEG:
                        off += L
                    else:
                        on += L
    if off > 0.4 * (on + off):  # the cut, measured segment by segment, mostly slants across the roof's axes
        return False, 'diagonal cut: the lot line slants across the roof'
    return True, ''


def cut_evidence(folder, latest, ring, units, _cache={}):
    """Median photo edge strength along the cuts between units (inside the roof, 1 m in from its outline), over the roof's own
    median: a party wall or gutter shows as a line (ratio > 1), a lot line through one hip roof does not (Haiku review 2026-10-14:
    cuts it called wrong median 0.85, right / partly right 1.6-2.1). None when the cut is under 2 m long."""
    import cv2
    import numpy as np
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    key = str(folder)
    if key not in _cache:
        g = cv2.imread(str(folder / f"{latest['image_sha256']}.png"), cv2.IMREAD_GRAYSCALE).astype(np.float32)
        g = cv2.GaussianBlur(g, (0, 0), 1.0)
        m = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
        _cache.clear(); _cache[key] = cv2.dilate(m, np.ones((5, 5), np.uint8))
    g = _cache[key]
    mpp = abs(float(np.linalg.det(np.asarray(latest['pixel_to_meters'], dtype=float)[:2]))) ** 0.5
    inner = Polygon(ring).buffer(0).buffer(-1.0 / mpp)
    if inner.is_empty:
        return None
    cut = unary_union([Polygon(u['ring']).buffer(0).boundary for u in units]).intersection(inner)
    if cut.is_empty or cut.length * mpp < 2:
        return None
    h, w = g.shape
    pts = [cut.interpolate(t).coords[0] for t in np.linspace(0, cut.length, max(int(cut.length), 2))]
    vals = [g[int(round(y)), int(round(x))] for x, y in pts if 0 <= x < w and 0 <= y < h]
    mask = np.zeros(g.shape, np.uint8)
    for poly in getattr(inner, 'geoms', [inner]):
        cv2.fillPoly(mask, [np.round(np.asarray(poly.exterior.coords)).astype(np.int32)], 1)
    base = float(np.median(g[mask > 0])) if mask.any() else 0.0
    return round(float(np.median(vals)) / base, 3) if vals and base > 0 else None


def open_ring(r):
    r = [list(map(float, p)) for p in r]
    return r[:-1] if len(r) > 1 and r[0] == r[-1] else r


def confirmed_splits(root=Path('.'), _cache={}):
    """(scene, object_id) the reviewer answered Right in a Splits queue (latest pull decisions in SPLIT_DECISIONS)."""
    if 'v' not in _cache:
        out = set()
        p = root / SPLIT_DECISIONS
        if p.exists():
            d = json.loads(p.read_text())
            for x in d.get('decisions', d) if isinstance(d, dict) else d:
                if x.get('family') == 'split' and x.get('word') in ('right', 'edited'):
                    out.add((x['sceneId'], x['objectIds'][0]))
        _cache['v'] = out
    return _cache['v']


def plan_scene(scene_id, root=Path('.')):
    folder = root / 'data/scenes' / scene_id
    latest = json.loads((folder / 'latest.json').read_text())
    review = json.loads((folder / 'review.json').read_text())
    parcels = pu.scene_parcels(latest, root)
    rows = []
    if not pu.alignment(latest):
        raise ValueError(f'{scene_id}: lot lines not aligned yet (run scripts.ml.parcel_align)')
    if pu.held(latest):  # lot lines still do not line up with the photo: nothing lot-based happens here
        return {'scene': scene_id, 'image_sha256': latest['image_sha256'], 'held': True, 'version': VERSION, 'rows': []}
    for o in review.get('objects', []):
        if o.get('kind') != 'building' or o.get('status') in ('rejected', 'superseded') or not o.get('ring'):
            continue
        ring = open_ring(o['ring'])
        verdict = pu.unit_verdict(latest, ring, parcels)
        if verdict['units'] != 'several':
            continue
        units = [u for u in pu.split_by_lots(latest, ring, parcels) if u['m2'] >= MIN_UNIT_M2]
        row = {'scene': scene_id, 'object_id': o['id'], 'status': o.get('status'), 'source': o.get('source'),
               'route': 'queue' if touched(o) else 'pipeline', 'lots': verdict['lots'], 'ring': ring,
               'units': [{**u, 'id': hashlib.sha256(f"{scene_id}/{o['id']}/{u['pfi']}".encode()).hexdigest()[:16]} for u in units]}
        if len(units) < 2:
            row.update(route='refused', why=f'{len(units)} unit(s) of at least {MIN_UNIT_M2} m2')
        elif row['route'] == 'pipeline':
            ok, why = split_check(ring, units)
            if ok:
                ev = cut_evidence(folder, latest, ring, units)
                row['cut_evidence'] = ev
                if ev is not None and ev < CUT_EVIDENCE_MIN:
                    ok, why = False, f'no wall or gutter visible along the cut (edge ratio {ev:.2f})'
            if not ok:
                # reviewer 2026-10-17 (Units 003, "massively wrong"): a cut with no wall in the photo is one building on several
                # titles - never split, never put to the reviewer as units; bounded by the union of its own lots instead
                ev = row.get('cut_evidence') if 'cut_evidence' in row else cut_evidence(folder, latest, ring, units)
                row['cut_evidence'] = ev
                if ev is None or ev < CUT_EVIDENCE_MIN:
                    row.update(route='one-building', why=f'one building on {len(units)} titles: {why}',
                               title_pfis=sorted(u['pfi'] for u in units))
            if not ok and row['route'] != 'one-building':
                row.update(route='queue', why=why)
        if row['route'] == 'queue' and not row.get('why'):  # reviewer-touched: the same wall test before asking
            ev = cut_evidence(folder, latest, ring, row['units'])
            row['cut_evidence'] = ev
            if ev is None or ev < CUT_EVIDENCE_MIN:
                row.update(route='one-building', why=f"one building on {len(row['units'])} titles: no wall along the lot line",
                           title_pfis=sorted(u['pfi'] for u in row['units']))
        # reviewer 2026-10-19: "roof boundary > property boundary lines stored by government". A lot line running through a roof is
        # an outdated line for that roof: it is excluded (no automatic split, no clip). A split happens only where the reviewer
        # confirmed it (Splits queue: Right); everything else keeps the roof whole.
        if row['route'] in ('pipeline', 'queue', 'one-building'):
            if (scene_id, o['id']) in confirmed_splits(root):
                row['route'] = 'pipeline'
                row['why'] = 'reviewer confirmed the split'
            else:
                row.update(route='one-building', stale_lines=sorted(u['pfi'] for u in row['units']),
                           why='lot line(s) run through the roof: outdated cadastre, roof outline kept')
        rows.append(row)
    return {'scene': scene_id, 'image_sha256': latest['image_sha256'], 'review_revision': review.get('revision'),
            'model_signature': latest.get('model_signature'), 'version': VERSION, 'rows': rows}


def render(plan, root=Path(".")):
    from PIL import Image, ImageDraw
    latest = json.loads((root / 'data/scenes' / plan['scene'] / 'latest.json').read_text())
    img = Image.open(root / 'data/scenes' / plan['scene'] / f"{latest['image_sha256']}.png").convert('RGB')
    tiles = []
    for row in plan['rows']:
        if row['route'] != 'pipeline':  # the sample is what the pipeline would write; queue rows go to Fieldwork
            continue
        xs = [p[0] for p in row['ring']]; ys = [p[1] for p in row['ring']]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        half = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.7 + 8
        box = (cx - half, cy - half, cx + half, cy + half)
        T = 320; k = T / (2 * half)
        f = lambda r: [((a - box[0]) * k, (b - box[1]) * k) for a, b in r]
        before = img.crop(tuple(int(round(v)) for v in box)).resize((T, T), Image.BICUBIC)
        after = before.copy()
        d0, d1 = ImageDraw.Draw(before), ImageDraw.Draw(after)
        q = f(row['ring']); d0.line(q + q[:1], fill=(0, 230, 255), width=2)
        for j, u in enumerate(row['units']):
            c = tuple(int(255 * v) for v in colorsys.hsv_to_rgb((j * 0.17) % 1, 0.9, 1))
            q = f(u['ring']); d1.line(q + q[:1], fill=c, width=2)
        tile = Image.new('RGB', (2 * T + 6, T + 16), (0, 0, 0))
        tile.paste(before, (0, 0)); tile.paste(after, (T + 6, 0))
        label = f"{plan['scene'][-14:]} {row['object_id'][:8]} {row['status']} -> {len(row['units'])} units ({row['route']})"
        ImageDraw.Draw(tile).text((3, T + 2), label, fill=(230, 230, 230))
        tiles.append(tile)
    return tiles


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--census', default='data/reviews/eyes/experiments/parcel-units-2026-10-12/census.json')
    ap.add_argument('--out', required=True)
    ap.add_argument('--sample', type=int, default=50, help='scenes to render (sha256-ordered, deterministic)')
    args = ap.parse_args()
    rows = json.load(open(args.census))['rows']
    scenes = sorted({r['scene'] for r in rows if r['units'] == 'several'})
    out = Path(args.out); (out / 'plans').mkdir(parents=True, exist_ok=True)
    plans = [plan_scene(s) for s in scenes]
    for p in plans:
        (out / 'plans' / f"{p['scene']}.json").write_text(json.dumps(p, indent=1))
    routes = {}
    for p in plans:
        for r in p['rows']:
            routes[r['route']] = routes.get(r['route'], 0) + 1
    sample = sorted(scenes, key=lambda s: hashlib.sha256(s.encode()).hexdigest())[:args.sample]
    from PIL import Image
    tiles = [t for p in plans if p['scene'] in sample for t in render(p)]
    per = 12
    for i in range(0, len(tiles), per):
        chunk = tiles[i:i + per]
        W, H = chunk[0].size
        sheet = Image.new('RGB', (2 * W, ((len(chunk) + 1) // 2) * H), (0, 0, 0))
        for j, t in enumerate(chunk):
            sheet.paste(t, ((j % 2) * W, (j // 2) * H))
        sheet.save(out / f'sample-{i // per + 1:02d}.png')
    summary = {'version': VERSION, 'scenes': len(scenes), 'routes': routes,
               'units': sum(len(r['units']) for p in plans for r in p['rows'] if r['route'] == 'pipeline'),
               'sample_scenes': sample, 'sample_outlines': len(tiles), 'sheets': (len(tiles) + per - 1) // per}
    (out / 'summary.json').write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != 'sample_scenes'}))


if __name__ == '__main__':
    main()
