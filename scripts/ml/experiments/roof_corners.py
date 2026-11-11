"""Right-angle redraws of confirmed roofs (roof corners card 2026-10-10, item 2): proposals for the reviewer's Corners queue.

Each pick is a confirmed, model-drawn building (not frame-cut). Its outline is redrawn as the simplest right-angle shape that still
covers it: the rectangle on the roof's dominant edge direction, else a rectilinear trace at 0.5-2 m steps with short steps pruned.
The fewest-corner candidate with IoU >= MIN_IOU against the saved outline wins; roofs no right-angle shape fits are skipped (listed).
Writes an items file the Fieldwork stage reads ({'corner': [...]}) plus a summary. Never writes scenes."""
import argparse, glob, hashlib, json, math, statistics
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.validation import make_valid

MIN_IOU, STEPS_M, PRUNE_M, PX_PER_M_RASTER = 0.92, (0.5, 1.0, 1.5, 2.0), 1.0, 20
PRUNE_HARD_M = 0.0  # queue 2 sets 2.5 (with --prune-hard-m): real wings are metres long; shorter steps copy a ragged outline


def ring_sha(ring, holes=None):
    return hashlib.sha256(json.dumps([ring, holes or []], separators=(',', ':')).encode()).hexdigest()[:16]


def dominant_angle(ring):
    hist = {}
    for a, b in zip(ring, ring[1:] + ring[:1]):
        length = math.dist(a, b)
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 90
        k = round(ang) % 90
        for d in (-2, -1, 0, 1, 2):
            hist[(k + d) % 90] = hist.get((k + d) % 90, 0) + length * (3 - abs(d))
    k = max(hist, key=hist.get)
    num = den = 0.0  # refine: length-weighted mean of edge angles within 3 deg of k
    for a, b in zip(ring, ring[1:] + ring[:1]):
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 90
        d = (ang - k + 45) % 90 - 45
        if abs(d) <= 3:
            w = math.dist(a, b); num += d * w; den += w
    return math.radians(k + (num / den if den else 0))


def rot(points, theta):
    c, s = math.cos(theta), math.sin(theta)
    return [[x * c + y * s, -x * s + y * c] for x, y in points]


def unrot(points, theta):
    return rot(points, -theta)


def rect_candidate(rr):
    xs, ys = [p[0] for p in rr], [p[1] for p in rr]
    return [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]


def merge_collinear(poly):
    out = []
    n = len(poly)
    for i in range(n):
        a, b, c = poly[i - 1], poly[i], poly[(i + 1) % n]
        if (abs(a[0] - b[0]) < 1e-9 and abs(b[0] - c[0]) < 1e-9) or (abs(a[1] - b[1]) < 1e-9 and abs(b[1] - c[1]) < 1e-9):
            continue
        out.append(b)
    return out


def rectilinear_candidate(rr, step):
    xs, ys = [p[0] for p in rr], [p[1] for p in rr]
    x0, y0 = min(xs) - step, min(ys) - step
    nx, ny = int((max(xs) - x0) / step) + 2, int((max(ys) - y0) / step) + 2
    sub = PX_PER_M_RASTER
    hi = np.zeros((ny * int(step * sub), nx * int(step * sub)), np.uint8)
    pts = np.array([[(p[0] - x0) * sub, (p[1] - y0) * sub] for p in rr], np.int32)
    cv2.fillPoly(hi, [pts], 1)
    cell = int(step * sub)
    grid = hi.reshape(ny, cell, nx, cell).mean(axis=(1, 3)) >= 0.5
    if not grid.any():
        return None
    from shapely.geometry import box
    from shapely.ops import unary_union
    cells = [box(x0 + i * step, y0 + j * step, x0 + (i + 1) * step, y0 + (j + 1) * step) for j, i in zip(*np.nonzero(grid))]
    merged = unary_union(cells)
    if merged.geom_type == 'MultiPolygon':
        merged = max(merged.geoms, key=lambda g: g.area)
    poly = [[round(x / step) * step if abs(x / step - round(x / step)) < 1e-6 else x, y] for x, y in list(merged.exterior.coords)[:-1]]
    poly = merge_collinear(dedupe(poly))
    return poly if len(poly) >= 4 else None


def prune_steps(poly, min_len, target):
    """Remove a short edge (a stair step) by snapping its two ends together, keeping the polygon rectilinear, while IoU holds."""
    best = poly
    changed = True
    while changed and len(best) > 4:
        changed = False
        n = len(best)
        order = sorted(range(n), key=lambda i: math.dist(best[i], best[(i + 1) % n]))
        for i in order:
            if math.dist(best[i], best[(i + 1) % n]) >= min_len:
                break
            for keep_first in (True, False):
                a, b = best[i], best[(i + 1) % n]
                p = [q[:] for q in best]
                if abs(a[0] - b[0]) < 1e-9:  # vertical step: move b's x line? move the shorter neighbour edge
                    dy = b[1] - a[1]
                    if keep_first: p[(i + 1) % n][1] = a[1]; p[(i + 2) % n][1] = a[1]
                    else: p[i][1] = b[1]; p[i - 1][1] = b[1]
                else:
                    if keep_first: p[(i + 1) % n][0] = a[0]; p[(i + 2) % n][0] = a[0]
                    else: p[i][0] = b[0]; p[i - 1][0] = b[0]
                p = merge_collinear(dedupe(p))
                if len(p) >= 4 and valid(p):
                    p = refine(p, target)
                if len(p) >= 4 and valid(p) and iou(p, target) >= MIN_IOU:
                    best, changed = p, True
                    break
            if changed:
                break
    return best


def densify(ring, spacing=0.1):
    out = []
    for a, b in zip(ring, ring[1:] + ring[:1]):
        n = max(1, int(math.dist(a, b) / spacing))
        out += [[a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n] for k in range(n)]
    return out


def refine(poly, target, reach=1.5, rounds=3):
    """Move each axis-aligned edge to the median position of the saved outline's boundary points nearest to it."""
    samples = densify(target)
    p = [q[:] for q in poly]
    for _ in range(rounds):
        n = len(p)
        edges = [(i, abs(p[i][1] - p[(i + 1) % n][1]) < 1e-9) for i in range(n)]
        buckets = {i: [] for i in range(n)}
        for x, y in samples:
            best, bd = None, reach
            for i, horiz in edges:
                a, b = p[i], p[(i + 1) % n]
                if horiz:
                    lo, hi = sorted((a[0], b[0]))
                    d = abs(y - a[1]) if lo - 0.3 <= x <= hi + 0.3 else 1e9
                else:
                    lo, hi = sorted((a[1], b[1]))
                    d = abs(x - a[0]) if lo - 0.3 <= y <= hi + 0.3 else 1e9
                if d < bd:
                    best, bd = i, d
            if best is not None:
                buckets[best].append(y if edges[best][1] else x)
        for i, horiz in edges:
            v = buckets[i]
            if len(v) < 5:
                continue
            m = statistics.median(v)
            j = (i + 1) % n
            if horiz:
                p[i][1] = p[j][1] = m
            else:
                p[i][0] = p[j][0] = m
    return p if valid(p) else poly


def dedupe(p):
    out = []
    for q in p:
        if not out or math.dist(out[-1], q) > 1e-9:
            out.append(q)
    if len(out) > 1 and math.dist(out[0], out[-1]) < 1e-9:
        out.pop()
    return out


def valid(p):
    try:
        return Polygon(p).is_valid and Polygon(p).area > 0
    except Exception:
        return False


def iou(a, b):
    pa, pb = Polygon(a), Polygon(b)
    if not pa.is_valid: pa = make_valid(pa)
    if not pb.is_valid: pb = make_valid(pb)
    inter = pa.intersection(pb).area
    union = pa.union(pb).area
    return inter / union if union else 0.0


def fit(ring_m):
    theta = dominant_angle(ring_m)
    rr = rot(ring_m, theta)
    cands = [('rectangle', refine(rect_candidate(rr), rr))]
    for s in STEPS_M:
        c = rectilinear_candidate(rr, s)
        if c:
            cands.append((f'rectilinear-{s}m', prune_steps(refine(c, rr), PRUNE_M, rr)))
            if PRUNE_HARD_M > PRUNE_M:  # queue 2: also try dropping every step shorter than PRUNE_HARD_M
                cands.append((f'rectilinear-{s}m-pruned', prune_steps(refine(c, rr), PRUNE_HARD_M, rr)))
    scored = [(len(c), -iou(c, rr), name, c) for name, c in cands if c and valid(c) and iou(c, rr) >= MIN_IOU]
    if not scored:
        best_iou = max((iou(c, rr) for _, c in cands if c and valid(c)), default=0)
        return None, {'best_iou': round(best_iou, 3)}
    n, neg, name, c = min(scored)
    return unrot(c, theta), {'method': name, 'iou': round(-neg, 4), 'corners': n, 'angle_deg': round(math.degrees(theta) % 90, 2)}


def to_m(ring, m):
    a, b, c, d, e, f = m  # pixel_to_meters affine as [a,b,c,d,e,f]: x' = a x + b y + c, y' = d x + e y + f
    return [[a * x + b * y + c, d * x + e * y + f] for x, y in ring]


def lot_bound(scene, ring):
    from scripts.ml import parcel_units as pu
    several(scene, ring)
    L, parcels = several.__defaults__[0][scene]
    if not parcels:
        return ring
    if not pu.alignment(L) or pu.held(L):
        return None
    from scripts.ml.photo_roof import guarded_bound
    clipped, info = guarded_bound(L, ring, parcels)
    return clipped if info else ring


def several(scene, ring, cache={}):
    from scripts.ml import parcel_units as pu
    if scene not in cache:
        L = json.load(open(f'data/scenes/{scene}/latest.json'))
        try:
            cache[scene] = (L, pu.scene_parcels(L))
        except (OSError, ValueError, KeyError):
            cache[scene] = (L, None)
    L, parcels = cache[scene]
    from scripts.ml.scene_parcel_units import confirmed_splits  # 2026-10-19 roof > lot lines
    return bool(parcels) and pu.unit_verdict(L, ring, parcels)['units'] == 'several' and any(s == scene for s, _ in confirmed_splits())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--n', type=int, default=100)
    ap.add_argument('--statuses', default='confirmed', help='comma list of object statuses to draw from')
    ap.add_argument('--exclude', action='append', default=[], help='items.json whose (scene, object) pairs are skipped')
    ap.add_argument('--line-start', dest='line_start', type=int, default=1, help='first line number (queue 2 continues after queue 1)')
    ap.add_argument('--holdout', type=int, default=0, help='queue 2: mark this many picks split=test (whole scenes, 6+ corners first)')
    ap.add_argument('--corner-set', dest='corner_set', default='', help='prefix:tag for the Fieldwork items of this queue')
    ap.add_argument('--min-iou', dest='min_iou', type=float, default=0.92)
    ap.add_argument('--prune-hard-m', dest='prune_hard_m', type=float, default=0.0)
    ap.add_argument('--parcel-units', dest='parcel_units', choices=('skip', 'off'), default='skip',
                    help='skip roofs that span several numbered Vicmap lots (parcel_units; reviewer 2026-10-12). '
                         'Split them with roof_corners_units.py instead')
    ap.add_argument('--max-corners', dest='max_corners', type=int, default=99, help='skip redraws with more corners than this')
    ap.add_argument('--multi-share', dest='multi_share', type=float, default=0.0,
                    help='queue 2: at least this share of picks have 6-8 corners (L-shapes and steps)')
    args = ap.parse_args()
    globals().update(MIN_IOU=args.min_iou, PRUNE_HARD_M=args.prune_hard_m)
    statuses = set(args.statuses.split(','))
    done = {(x['scene'], x['object_id']) for f in args.exclude for x in json.load(open(f))['corner']}
    picks, skipped, pool = [], [], []
    for f in sorted(glob.glob('data/scenes/*/review.json')):
        scene = f.split('/')[-2]
        if scene.startswith('qa_'):
            continue
        rev = json.load(open(f))
        model = json.load(open(f.replace('review.json', 'latest.json')))
        p2m = model.get('pixel_to_meters')
        if not p2m:
            continue
        aff = [p2m[0][0], p2m[1][0], p2m[2][0], p2m[0][1], p2m[1][1], p2m[2][1]]  # x' = a x + b y + c, y' = d x + e y + f
        for o in rev.get('objects', []):
            if o.get('kind') != 'building' or o.get('status') not in statuses or (scene, o['id']) in done or o.get('clipped_by_frame') or o.get('holes'):
                continue
            if not str(o.get('source', '')).startswith('sam3'):
                continue
            R = o['ring'][:-1] if o['ring'][0] == o['ring'][-1] else o['ring']
            W, H = model.get('image_width'), model.get('image_height')
            if any(x <= 1 or y <= 1 or x >= W - 2 or y >= H - 2 for x, y in R):
                continue
            Rm = to_m(R, aff)
            area = Polygon(Rm).area
            if not 25 <= area <= 600:
                continue
            pool.append((scene, o, R, Rm, aff, area))
    # spread across scenes: round-robin, roofs with the most corners first in each scene
    by_scene = {}
    for x in pool:
        by_scene.setdefault(x[0], []).append(x)
    for v in by_scene.values():
        v.sort(key=lambda x: -len(x[2]))
    order = []
    while any(by_scene.values()):
        for s in sorted(by_scene):
            if by_scene[s]:
                order.append(by_scene[s].pop(0))
    seen_obj = set()
    n_multi = math.ceil(args.multi_share * args.n)
    fitted = []
    for scene, o, R, Rm, aff, area in order:
        if o['id'] in seen_obj:
            continue
        seen_obj.add(o['id'])
        fitted.append((scene, o, R, Rm, aff, area, *fit(Rm)))
    multi = [x for x in fitted if x[6] is not None and 6 <= x[7]['corners'] <= min(8, args.max_corners) and x[7]['corners'] < len(x[2])]
    keep = {id(x) for x in multi[:n_multi]}
    rest = [x for x in fitted if id(x) not in keep]
    order2 = [x for x in fitted if id(x) in keep] + rest
    for scene, o, R, Rm, aff, area, shape_m, info in order2:
        if len(picks) >= args.n:
            break
        if shape_m is None:
            skipped.append({'scene': scene, 'object': o['id'], 'why': 'no right-angle shape fits', **info})
            continue
        if args.parcel_units == 'skip' and several(scene, R):
            skipped.append({'scene': scene, 'object': o['id'], 'why': 'spans several Vicmap lots (split with roof_corners_units.py)'})
            continue
        if info['corners'] > args.max_corners:
            skipped.append({'scene': scene, 'object': o['id'], 'why': f'more than {args.max_corners} corners', 'corners': info['corners']})
            continue
        if info['corners'] >= len(R):
            skipped.append({'scene': scene, 'object': o['id'], 'why': 'already as simple', 'corners': len(R)})
            continue
        a, b, c, d, e, f = aff  # invert the affine to pixels
        det = a * e - b * d
        inv = lambda X, Y: [(e * (X - c) - b * (Y - f)) / det, (-d * (X - c) + a * (Y - f)) / det]
        shape_px = [[round(v, 3) for v in inv(x, y)] for x, y in shape_m]
        bounded = lot_bound(scene, shape_px)  # reviewer 2026-10-13: always inside the legal lot (aligned Vicmap)
        if bounded is None:
            skipped.append({'scene': scene, 'object': o['id'], 'why': 'lot lines not aligned or held'}); continue
        shape_px = [[round(v, 3) for v in p] for p in bounded]
        pid = hashlib.sha256(json.dumps([scene, o['id'], shape_px]).encode()).hexdigest()[:16]
        picks.append({'line': len(picks) + 1, 'scene': scene, 'object_id': o['id'], 'kind': 'building', 'status_at_compute': o['status'],
                      'current_ring': R, 'current_ring_sha': None, 'current_corners': len(R), 'proposed_ring': shape_px,
                      'proposed_corners': info['corners'], 'iou': info['iou'], 'method': info['method'], 'area_m2': round(area, 1),
                      'proposal_id': pid})
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    picks.sort(key=lambda p: (p['scene'], p['object_id']))  # interleave shapes in the queue rather than all L-shapes first
    for i, p in enumerate(picks):
        p['line'] = args.line_start + i
    if args.holdout:
        # a fresh test set: whole scenes (no scene in both), scenes with the most 6+ corner picks first, by sha256 for ties
        by = {}
        for p in picks:
            by.setdefault(p['scene'], []).append(p)
        order_s = sorted(by, key=lambda sc: (-sum(q['proposed_corners'] >= 6 for q in by[sc]), hashlib.sha256(sc.encode()).hexdigest()))
        n_test = 0
        for sc in order_s:
            if n_test >= args.holdout:
                break
            for p in by[sc]:
                p['split'] = 'test'
            n_test += len(by[sc])
        for p in picks:
            p.setdefault('split', 'train')
    doc = {'corner': picks}
    if args.corner_set:
        prefix, tag = args.corner_set.split(':')
        doc['corner_set'] = {'prefix': prefix, 'tag': tag}
    (out / 'items.json').write_text(json.dumps(doc, indent=1))
    summ = {'picked': len(picks), 'pool': len(pool), 'statuses': sorted(statuses), 'excluded': len(done),
            'test_picked': sum(p.get('split') == 'test' for p in picks), 'multi_picked': sum(p['proposed_corners'] >= 6 for p in picks), 'multi_available': len(multi), 'scenes': len({p['scene'] for p in picks}), 'skipped': len(skipped),
            'corners_before_median': statistics.median(p['current_corners'] for p in picks) if picks else None,
            'corners_after_median': statistics.median(p['proposed_corners'] for p in picks) if picks else None,
            'iou_median': statistics.median(p['iou'] for p in picks) if picks else None,
            'methods': {m: sum(1 for p in picks if p['method'] == m) for m in sorted({p['method'] for p in picks})},
            'skipped_list': skipped[:200]}
    (out / 'summary.json').write_text(json.dumps(summ, indent=1))
    print(json.dumps({k: v for k, v in summ.items() if k != 'skipped_list'}))


if __name__ == '__main__':
    main()
