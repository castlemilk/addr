"""Learned roof corners (roof corners card 2026-10-10, item 3): a small U-Net that reads the imagery around one roof plus
today's mask and predicts (a) the roof mask, (b) a corner heatmap and (c) an edge-direction field modulo 90 degrees
(encoded as cos 4t, sin 4t). The outline is assembled from those predictions: corner peaks on the predicted boundary, in
boundary order; each side between two corners is snapped to the field's direction where the field says the side is square
to it, positioned on the predicted boundary, and the corners become the intersections of neighbouring sides.

Subcommands (all outputs under --work; never writes scenes, review files or any server):
  prep    crop patches for the approved set (train / test split by whole scene) and for 200 other confirmed roofs
  train   train (--val holds out train scenes with sha256 % 5 == 1 for tuning; --full trains on all 80 train roofs)
  tune    pick assembly thresholds on the validation roofs (needs a --val checkpoint)
  eval    score train + test (or the 200 other roofs) and write predictions
  sheets  contact sheets (cyan = model, grey = today, thin yellow = approved target where there is one)

Run with scripts/ml/training/.venv-sam3/bin/python (torch, cv2, shapely, skimage)."""
from __future__ import annotations

import argparse, glob, hashlib, json, math, random, statistics, time
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.validation import make_valid

APPROVED = 'data/reviews/eyes/experiments/roof-corners-2026-10-10/approved-set-v1.json'
S = 128            # crop side (px)
FILL = 72.0        # today's roof extent maps to this many crop px
PATCH_K = 3.4      # stored patch side = PATCH_K * extent (covers any rotation / scale jitter)
SIGMA = 1.6        # corner gaussian (crop px)


# ---------------------------------------------------------------- geometry helpers
def split_of(scene):
    return 'test' if int(hashlib.sha256(scene.encode()).hexdigest(), 16) % 5 == 0 else 'train'


def is_val(scene):
    return int(hashlib.sha256(scene.encode()).hexdigest(), 16) % 5 == 1


def open_ring(r):
    r = [list(map(float, p)) for p in r]
    return r[:-1] if len(r) > 1 and r[0] == r[-1] else r


def poly(r):
    p = Polygon(r)
    if not p.is_valid:
        p = make_valid(p)
    return p


def iou(a, b):
    pa, pb = poly(a), poly(b)
    u = pa.union(pb).area
    return pa.intersection(pb).area / u if u else 0.0


def hausdorff(a, b):
    pa, pb = Polygon(a), Polygon(b)
    return pa.exterior.hausdorff_distance(pb.exterior)


def roof_frame(ring):
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    c = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    e = max(max(xs) - min(xs), max(ys) - min(ys), 4.0)
    return c, e


def crop_affine(c, e, angle=0.0, scale=1.0, flip=False, shift=(0.0, 0.0)):
    """2x3 affine scene px -> crop px: centre c to the crop centre, extent e to FILL*scale px, rotate by angle (rad)."""
    k = FILL * scale / e
    ca, sa = math.cos(angle) * k, math.sin(angle) * k
    M = np.array([[ca, -sa, 0.0], [sa, ca, 0.0]])
    if flip:
        M[0] *= -1
    M[:, 2] = np.array([S / 2 + shift[0], S / 2 + shift[1]]) - M[:, :2] @ np.array(c)
    return M


def apply(M, ring):
    a = np.asarray(ring, float)
    return a @ M[:, :2].T + M[:, 2]


def invert(M):
    return cv2.invertAffineTransform(M)


def fill_mask(ring_c, jitter=0.0, rng=None):
    """Area-weighted mask (0..1): rasterised at 4x and averaged, so the 0.5 level sits on the polygon edge
    (a plain fillPoly includes every touched pixel and inflates the mask by ~half a pixel)."""
    m = np.zeros((S * 4, S * 4), np.uint8)
    pts = np.asarray(ring_c, float)
    if jitter and rng is not None:
        pts = pts + rng.normal(0, jitter, pts.shape)
    hr = pts * 4 + 1.5  # crop pixel i centre -> 4x pixel 4i + 1.5
    cv2.fillPoly(m, [np.round(hr * 16).astype(np.int32)], 255, lineType=cv2.LINE_8, shift=4)
    return cv2.resize(m, (S, S), interpolation=cv2.INTER_AREA).astype(np.float32) / 255


def targets(ring_c):
    """Target mask, corner heatmap and direction field (cos4t, sin4t, weight) in crop px."""
    m = fill_mask(ring_c)
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    heat = np.zeros((S, S), np.float32)
    for x, y in ring_c:
        heat = np.maximum(heat, np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * SIGMA ** 2)))
    lab = np.zeros((S, S), np.int32)
    dist = np.full((S, S), 1e9, np.float32)
    pts = np.asarray(ring_c, float)
    n = len(pts)
    ang = np.zeros(n)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        ang[i] = math.atan2(b[1] - a[1], b[0] - a[0])
        ab = b - a
        L2 = max(float(ab @ ab), 1e-9)
        t = np.clip(((xx - a[0]) * ab[0] + (yy - a[1]) * ab[1]) / L2, 0, 1)
        d = np.hypot(xx - (a[0] + t * ab[0]), yy - (a[1] + t * ab[1]))
        sel = d < dist
        dist[sel] = d[sel]
        lab[sel] = i
    w = (dist <= 3.0).astype(np.float32)
    th = ang[lab]
    field = np.stack([np.cos(4 * th), np.sin(4 * th), w]).astype(np.float32)
    return m.astype(np.float32), heat.astype(np.float32), field


# ---------------------------------------------------------------- data prep
def scene_meta(scene, cache={}):
    if scene not in cache:
        L = json.load(open(f'data/scenes/{scene}/latest.json'))
        cache[scene] = L
    return cache[scene]


def make_patch(scene, saved):
    L = scene_meta(scene)
    img = cv2.imread(f"data/scenes/{scene}/{L['image_sha256']}.png", cv2.IMREAD_COLOR)[:, :, ::-1]
    c, e = roof_frame(saved)
    side = int(math.ceil(max(PATCH_K * e, 48)))
    x0, y0 = int(round(c[0] - side / 2)), int(round(c[1] - side / 2))
    M = np.array([[1.0, 0, -x0], [0, 1.0, -y0]])
    patch = cv2.warpAffine(img, M, (side, side), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REFLECT)
    return patch, (x0, y0), L


def lot_bounded(scene, ring, pfi=None):
    """(ring clipped to its aligned legal lot, '') ; (None, reason) for a held or never-aligned scene."""
    from scripts.ml import parcel_units as pu
    several_units(scene, ring)  # fills the parcel cache
    L, parcels = several_units.__defaults__[0][scene]
    if not parcels:
        return ring, ''
    if not pu.alignment(L):
        return None, 'lot lines not aligned'
    if pu.held(L):
        return None, 'lot lines held'
    from scripts.ml.photo_roof import guarded_bound
    clipped, info = guarded_bound(L, open_ring(ring), parcels, pfi)
    return (clipped if info else ring), ''


def _confirmed(scene, ring):
    """True when this outline overlaps a roof whose split the reviewer confirmed."""
    from shapely.geometry import Polygon
    from scripts.ml.scene_parcel_units import confirmed_splits
    objs = {o['id']: o for o in json.load(open(f'data/scenes/{scene}/review.json'))['objects']}
    me = Polygon(open_ring(ring)).buffer(0)
    return any(s == scene and oid in objs and Polygon(objs[oid]['ring']).buffer(0).intersection(me).area > 0.5 * me.area
               for s, oid in confirmed_splits())


def several_units(scene, ring, cache={}):
    """True when the outline spans several numbered Vicmap lots (parcel_units); unknown parcels never gate."""
    from scripts.ml import parcel_units as pu
    if scene not in cache:
        L = scene_meta(scene)
        try:
            cache[scene] = (L, pu.scene_parcels(L))
        except (OSError, ValueError, KeyError) as error:
            cache[scene] = (L, None)
            print(f'parcels unavailable for {scene}: {error}')
    L, parcels = cache[scene]
    # 2026-10-19 roof > lot lines: a roof is several units only when the reviewer confirmed the split
    from scripts.ml.scene_parcel_units import confirmed_splits
    return bool(parcels) and pu.unit_verdict(L, open_ring(ring), parcels)['units'] == 'several' \
        and any(s == scene for s, _ in confirmed_splits()) and _confirmed(scene, ring)


def cmd_prep(args):
    work = Path(args.work)
    (work / 'patches').mkdir(parents=True, exist_ok=True)
    ex = json.load(open(args.approved or APPROVED))['examples']
    rows, gated = [], []
    in_set = set()
    for x in ex:
        # a unit of a merged outline (parcel_units split): its lot piece of today's outline is the input, not the whole terrace
        saved, target = open_ring(x.get('unit_piece_ring') or x['saved_ring']), open_ring(x['target_ring'])
        if several_units(x['scene'], target):  # reviewer 2026-10-12: one target must never span several Vicmap lots
            gated.append(x['scene'] + '/' + x['object_id']); continue
        # reviewer 2026-10-13: always bounded by the legal lot (aligned Vicmap); held scenes (lot lines that do not line up) are left out
        target, why = lot_bounded(x['scene'], target, (x.get('unit_of') or {}).get('pfi'))
        if target is None:
            gated.append(x['scene'] + '/' + x['object_id'] + ' ' + why); continue
        patch, off, L = make_patch(x['scene'], saved)
        key = f"{x['scene']}__{x['object_id']}"
        in_set.add((x['scene'], x['object_id']))
        np.save(work / 'patches' / f'{key}.npy', patch)
        rows.append({'key': key, 'scene': x['scene'], 'object_id': x['object_id'], 'split': x.get('split') or split_of(x['scene']),
                     'val': is_val(x['scene']), 'offset': off, 'saved': saved, 'target': target,
                     'm_per_px': L['pixel_to_meters'][0][0]})
    (work / 'approved.json').write_text(json.dumps(rows))
    # 200 other roofs: every confirmed roof first, then candidates (review.json, sam3 drawn, not frame-cut, no holes, 25-600 m2),
    # round-robin over scenes
    pools = {'confirmed': {}, 'candidate': {}}
    for f in sorted(glob.glob('data/scenes/*/review.json')):
        scene = f.split('/')[-2]
        if scene.startswith('qa_'):
            continue
        rev = json.load(open(f))
        L = scene_meta(scene)
        if not L.get('pixel_to_meters'):
            continue
        W, H = L['image_width'], L['image_height']
        mpp = L['pixel_to_meters'][0][0]
        for o in rev.get('objects', []):
            if o.get('kind') != 'building' or o.get('status') not in pools or o.get('clipped_by_frame') or o.get('holes'):
                continue
            if (scene, o['id']) in in_set or not str(o.get('source', '')).startswith('sam3'):
                continue
            R = open_ring(o['ring'])
            if len(R) < 3 or any(px <= 1 or py <= 1 or px >= W - 2 or py >= H - 2 for px, py in R):
                continue
            area = poly(R).area * mpp * mpp
            if not 25 <= area <= 600 or several_units(scene, R):
                continue
            pools[o['status']].setdefault(scene, []).append((o['id'], R))
    rng = random.Random(20261011)
    pool_sizes = {k: sum(map(len, v.values())) for k, v in pools.items()}
    order = []
    for status in ('confirmed', 'candidate'):
        pool = pools[status]
        for v in pool.values():
            rng.shuffle(v)
        scenes = sorted(pool); rng.shuffle(scenes)
        while any(pool.values()):
            for s in scenes:
                if pool[s]:
                    oid, R = pool[s].pop()
                    order.append((s, oid, R, status))
    others = []
    for scene, oid, R, status in order[:args.n_other]:
        patch, off, L = make_patch(scene, R)
        key = f'{scene}__{oid}'
        np.save(work / 'patches' / f'{key}.npy', patch)
        others.append({'key': key, 'scene': scene, 'object_id': oid, 'split': 'other', 'status': status, 'offset': off, 'saved': R,
                       'm_per_px': L['pixel_to_meters'][0][0]})
    (work / 'others.json').write_text(json.dumps(others))
    print(json.dumps({'approved': len(rows), 'gated_several_units': gated, 'train': sum(r['split'] == 'train' for r in rows),
                      'test': sum(r['split'] == 'test' for r in rows), 'val_in_train': sum(r['val'] and r['split'] == 'train' for r in rows),
                      'pool': pool_sizes, 'others': len(others),
                      'other_confirmed': sum(o['status'] == 'confirmed' for o in others), 'other_scenes': len({o['scene'] for o in others})}))


# ---------------------------------------------------------------- dataset / augmentation
def local_ring(row):
    ox, oy = row['offset']
    return lambda r: [[p[0] - ox, p[1] - oy] for p in r]


def render(row, patch, M, rng=None, aug=False, mask_drop=0.0, edit=None):
    img = cv2.warpAffine(patch, M, (S, S), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT).astype(np.float32) / 255
    loc = local_ring(row)
    saved_c = apply(M, loc(row['saved']))
    if edit is not None:
        img, notch = edit(img)
    else:
        notch = None
    if aug:
        # colour jitter, blur, noise
        img = img * rng.uniform(0.75, 1.25) + rng.uniform(-0.08, 0.08)
        g = img.mean(2, keepdims=True)
        img = g + (img - g) * rng.uniform(0.6, 1.4)
        img = img * rng.uniform(0.92, 1.08, (1, 1, 3))
        if rng.random() < 0.3:
            img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.3, 1.2))
        img = img + rng.normal(0, rng.uniform(0, 0.03), img.shape)
        img = np.clip(img, 0, 1).astype(np.float32)
        m = fill_mask(saved_c, jitter=rng.uniform(0, 0.8), rng=rng)
        if notch is not None and rng.random() < 0.5:
            m = m * (1 - notch)  # sometimes today's outline already shows the notch
        r = rng.random()
        if r < 0.2:
            m = cv2.dilate(m, np.ones((3, 3), np.uint8))
        elif r < 0.4:
            m = cv2.erode(m, np.ones((3, 3), np.uint8))
        if rng.random() < mask_drop:
            # hide today's outline (channel 0 = unknown) so the outline has to come from the imagery
            x = np.concatenate([(img - 0.45) / 0.22, np.zeros((S, S, 1), np.float32)], 2).transpose(2, 0, 1)
            return np.ascontiguousarray(x, dtype=np.float32), saved_c
    else:
        m = fill_mask(saved_c)
    x = np.concatenate([(img - 0.45) / 0.22, m[:, :, None].astype(np.float32) * 2 - 1], 2).transpose(2, 0, 1)
    return np.ascontiguousarray(x, dtype=np.float32), saved_c


def synth_notch(tgt_c, rng):
    """Synthetic L-shape (model v3): cut a rectangular notch out of one corner of a four-corner target and fill it with
    ground copied from elsewhere in the crop. Returns (edit fn for render, new six-corner ring) or (None, None)."""
    q = np.asarray(open_ring(tgt_c), np.float64)
    if len(q) != 4:
        return None, None
    i = int(rng.integers(4))
    a, nxt, prv = q[i], q[(i + 1) % 4], q[i - 1]
    u, v = nxt - a, prv - a
    Lu, Lv = np.hypot(*u), np.hypot(*v)
    if min(Lu, Lv) < 16:
        return None, None
    u, v = u / Lu, v / Lv
    fu, fv = rng.uniform(0.25, 0.5) * Lu, rng.uniform(0.25, 0.5) * Lv
    cut = [a + fv * v, a + fu * u + fv * v, a + fu * u]
    ring = [list(map(float, p)) for p in list(q[:i]) + cut + list(q[i + 1:])]
    if not Polygon(ring).is_valid:
        return None, None
    nq = np.array([a, a + fu * u, a + fu * u + fv * v, a + fv * v])
    N = np.zeros((S, S), np.float32)
    cv2.fillPoly(N, [np.round(nq).astype(np.int32)], 1.0)
    T = np.zeros((S, S), np.uint8)
    cv2.fillPoly(T, [np.round(q).astype(np.int32)], 1)
    T = cv2.dilate(T, np.ones((7, 7), np.uint8))
    ys, xs = np.nonzero(N)
    if len(ys) == 0:
        return None, None
    for _ in range(40):  # a shift that lands the notch on pixels outside the roof
        dx, dy = (int(t) for t in rng.integers(-S // 2, S // 2, 2))
        sy, sx = ys + dy, xs + dx
        if sy.min() < 0 or sx.min() < 0 or sy.max() >= S or sx.max() >= S or T[sy, sx].any():
            continue
        alpha = cv2.GaussianBlur(N, (0, 0), 0.7)[:, :, None]

        def edit(img, dx=dx, dy=dy, alpha=alpha):
            src = np.roll(np.roll(img, -dy, 0), -dx, 1)
            return img * (1 - alpha) + src * alpha, N
        return edit, ring
    return None, None


def sample(row, patch, rng, aug=True, mask_drop=0.0, synth=0.0):
    loc = local_ring(row)
    c, e = roof_frame(loc(row['saved']))
    if aug:
        M = crop_affine(c, e, angle=rng.uniform(0, 2 * math.pi), scale=rng.uniform(0.75, 1.2), flip=rng.random() < 0.5,
                        shift=(rng.uniform(-6, 6), rng.uniform(-6, 6)))
    else:
        M = crop_affine(c, e)
    tgt_c = apply(M, loc(row['target']))
    edit = None
    if aug and synth > 0 and len(row['target']) == 4 and rng.random() < synth:
        edit, ring = synth_notch(tgt_c, rng)
        if edit is not None:
            tgt_c = ring
    x, _ = render(row, patch, M, rng, aug, mask_drop, edit)
    tm, th, tf = targets(tgt_c)
    return x, tm, th, tf


# ---------------------------------------------------------------- model
def build_model():
    import torch
    from torch import nn

    def block(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1, bias=False), nn.BatchNorm2d(o), nn.ReLU(inplace=True),
                             nn.Conv2d(o, o, 3, padding=1, bias=False), nn.BatchNorm2d(o), nn.ReLU(inplace=True))

    class UNet(nn.Module):
        def __init__(self, ch=(32, 64, 128, 256)):
            super().__init__()
            self.enc = nn.ModuleList()
            prev = 4
            for c in ch:
                self.enc.append(block(prev, c)); prev = c
            self.mid = block(ch[-1], ch[-1] * 2)
            self.up = nn.ModuleList(); self.dec = nn.ModuleList()
            prev = ch[-1] * 2
            for c in reversed(ch):
                self.up.append(nn.ConvTranspose2d(prev, c, 2, stride=2))
                self.dec.append(block(2 * c, c)); prev = c
            self.head = nn.Conv2d(ch[0], 4, 1)  # mask logit, corner logit, cos4t, sin4t

        def forward(self, x):
            skips = []
            for e in self.enc:
                x = e(x); skips.append(x); x = torch.nn.functional.max_pool2d(x, 2)
            x = self.mid(x)
            for u, d, s in zip(self.up, self.dec, reversed(skips)):
                x = d(torch.cat([u(x), s], 1))
            return self.head(x)

    m = UNet()
    with torch.no_grad():
        m.head.bias[1] = -4.0
    return m


def losses(out, tm, th, tf):
    import torch
    import torch.nn.functional as F
    ml = F.binary_cross_entropy_with_logits(out[:, 0], tm)
    p = torch.sigmoid(out[:, 0])
    dice = 1 - (2 * (p * tm).sum((1, 2)) + 1) / (p.sum((1, 2)) + tm.sum((1, 2)) + 1)
    # CenterNet focal loss on the corner heatmap
    hp = torch.sigmoid(out[:, 1]).clamp(1e-4, 1 - 1e-4)
    pos = (th > 0.97).float()
    npos = pos.sum().clamp(min=1)
    pl = -(torch.log(hp) * (1 - hp) ** 2 * pos).sum()
    nl = -(torch.log(1 - hp) * hp ** 2 * (1 - th) ** 4 * (1 - pos)).sum()
    cl = (pl + nl) / npos
    w = tf[:, 2]
    dl = (((out[:, 2] - tf[:, 0]) ** 2 + (out[:, 3] - tf[:, 1]) ** 2) * w).sum() / w.sum().clamp(min=1)
    return ml + dice.mean() + cl + 0.5 * dl, {'mask': ml.item(), 'dice': dice.mean().item(), 'corner': cl.item(), 'dir': dl.item()}


def load_rows(work, which):
    rows = json.load(open(Path(work) / which))
    for r in rows:
        r['patch'] = np.load(Path(work) / 'patches' / f"{r['key']}.npy")
    return rows


def cmd_train(args):
    import torch
    torch.set_num_threads(args.threads)
    work = Path(args.work)
    rows = [r for r in load_rows(work, 'approved.json') if r['split'] == 'train']
    if args.val:
        rows = [r for r in rows if not r['val']]
    tag = 'val' if args.val else 'full'
    dev = torch.device(args.device)
    torch.manual_seed(args.seed); rng = np.random.default_rng(args.seed)
    model = build_model().to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.iters, pct_start=0.05)
    ck = work / f'ckpt-{tag}.pt'
    start = 0
    if ck.exists() and args.resume:
        st = torch.load(ck, map_location='cpu')
        model.load_state_dict(st['model']); opt.load_state_dict(st['opt']); sched.load_state_dict(st['sched']); start = st['iter']
        rng = np.random.default_rng(args.seed + start)
    log = open(work / f'train-{tag}.log', 'a')
    log.write(f'# start {time.strftime("%FT%T")} rows={len(rows)} iters={args.iters} from={start} device={dev} {json.dumps(vars(args))}\n')
    t0 = time.time(); acc = {}
    model.train()
    stop = min(args.stop_at or args.iters, args.iters)
    # multi-corner targets (L-shapes and up) drawn `oversample` times as often as four-corner ones
    wts = np.array([args.oversample if len(r['target']) > 4 else 1.0 for r in rows]); wts /= wts.sum()
    for it in range(start, stop):
        batch = [sample(rows[i], rows[i]['patch'], rng, mask_drop=args.mask_drop, synth=args.synth) for i in rng.choice(len(rows), args.batch, p=wts)]
        x, tm, th, tf = (torch.from_numpy(np.stack(v)).to(dev) for v in zip(*batch))
        out = model(x)
        loss, parts = losses(out, tm, th, tf)
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        for k, v in parts.items():
            acc[k] = acc.get(k, 0) + v
        if (it + 1) % 100 == 0:
            msg = f'it {it + 1} ' + ' '.join(f'{k} {v / 100:.4f}' for k, v in acc.items()) + f' lr {sched.get_last_lr()[0]:.2e} t {time.time() - t0:.0f}s'
            print(msg, flush=True); log.write(msg + '\n'); log.flush(); acc = {}
        if (it + 1) % 500 == 0 or it + 1 == stop:
            torch.save({'model': model.state_dict(), 'opt': opt.state_dict(), 'sched': sched.state_dict(), 'iter': it + 1}, ck)
    log.write(f'# done {time.strftime("%FT%T")} {time.time() - t0:.0f}s\n')


# ---------------------------------------------------------------- inference + assembly
def predict(model, row, dev):
    """8-fold dihedral test-time augmentation. Returns mask prob, corner prob, field (cos4t, sin4t), crop affine."""
    import torch
    loc = local_ring(row)
    c, e = roof_frame(loc(row['saved']))
    M = crop_affine(c, e)
    x, saved_c = render(row, row['patch'], M)
    xs = []
    for k in range(4):
        for f in (False, True):
            a = np.rot90(x, k, axes=(1, 2))
            if f:
                a = a[:, :, ::-1]
            xs.append(np.ascontiguousarray(a))
    with torch.no_grad():
        out = model(torch.from_numpy(np.stack(xs)).to(dev)).float().cpu().numpy()
    acc = np.zeros((4, S, S), np.float64)
    i = 0
    for k in range(4):
        for f in (False, True):
            o = out[i].copy(); i += 1
            o[0] = 1 / (1 + np.exp(-o[0])); o[1] = 1 / (1 + np.exp(-o[1]))
            # undo: flip (x -> -x reflects t -> pi - t, so 4t -> -4t), then rot90 by -k (4t shifts by 4 * 90 deg = no change)
            if f:
                o = o[:, :, ::-1]; o[3] = -o[3]
            o = np.rot90(o, -k, axes=(1, 2))
            # np.rot90 on image rows/cols with y down: rotating the image by 90 deg rotates directions by 90, 4t unchanged
            acc += o
    acc /= 8
    return acc[0], acc[1], acc[2:4], M, saved_c


def subpixel_contour(prob):
    from skimage import measure
    cs = measure.find_contours(np.pad(prob, 1), 0.5)
    if not cs:
        return None
    best = max(cs, key=lambda c: Polygon(c[:, ::-1]).area if len(c) >= 4 else 0)
    pts = best[:, ::-1] - 1  # (x, y)
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    if Polygon(pts).exterior.is_ccw:  # keep a consistent orientation
        pts = pts[::-1]
    return pts


def peaks(heat, thr, rad=2):
    from scipy.ndimage import maximum_filter
    mx = maximum_filter(heat, size=2 * rad + 1)
    ys, xs = np.nonzero((heat == mx) & (heat >= thr))
    out = []
    for y, x in zip(ys, xs):
        y0, y1, x0, x1 = max(y - 1, 0), min(y + 2, S), max(x - 1, 0), min(x + 2, S)
        w = heat[y0:y1, x0:x1]
        gy, gx = np.mgrid[y0:y1, x0:x1]
        out.append((float((gx * w).sum() / w.sum()), float((gy * w).sum() / w.sum()), float(heat[y, x])))
    return out


def field_angle(field, pts):
    """Field direction (rad, modulo pi/2) averaged over boundary points."""
    xi = np.clip(np.round(pts[:, 0]).astype(int), 0, S - 1); yi = np.clip(np.round(pts[:, 1]).astype(int), 0, S - 1)
    c, s = field[0][yi, xi], field[1][yi, xi]
    return math.atan2(s.sum(), c.sum()) / 4, float(np.hypot(c.sum(), s.sum()) / max(len(pts), 1))


def ang_diff90(a, b):
    d = (a - b) % (math.pi / 2)
    return min(d, math.pi / 2 - d)


def line_intersect(p1, d1, p2, d2):
    A = np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]])
    if abs(np.linalg.det(A)) < 1e-6:
        return None
    t = np.linalg.solve(A, np.asarray(p2) - np.asarray(p1))
    return np.asarray(p1) + t[0] * np.asarray(d1)


def assemble(mask, heat, field, P):
    """Outline from the model outputs. P: thresholds. Returns (ring in crop px, info)."""
    B = subpixel_contour(mask)
    info = {}
    if B is None or len(B) < 8:
        return None, {'fail': 'no mask'}
    n = len(B)
    gtheta, gcoh = field_angle(field, B)
    pk = peaks(heat, P['corner_thr'])
    # attach each peak to its nearest boundary point; keep those near the boundary
    att = []
    for x, y, v in pk:
        d = np.hypot(B[:, 0] - x, B[:, 1] - y)
        i = int(d.argmin())
        if d[i] <= P['attach_px']:
            att.append([i, x, y, v])
    att.sort()
    # merge peaks close along the boundary (keep the stronger)
    merged = []
    for a in att:
        if merged and np.hypot(a[1] - merged[-1][1], a[2] - merged[-1][2]) < P['merge_px']:
            if a[3] > merged[-1][3]:
                merged[-1] = a
            continue
        merged.append(a)
    if len(merged) > 2 and np.hypot(merged[0][1] - merged[-1][1], merged[0][2] - merged[-1][2]) < P['merge_px']:
        if merged[0][3] < merged[-1][3]:
            merged[0] = merged[-1]
        merged.pop()
    info['peaks'] = len(merged)
    # missed corners (assembly v2): where the predicted boundary between two corner peaks bends away from the straight line
    # joining them by more than split_px, the heatmap missed a corner; add one at the farthest boundary point
    added = 0
    for _ in range(8):
        if len(merged) < 2:
            break
        changed = False
        k = len(merged)
        for j in range(k):
            i0, i1 = merged[j][0], merged[(j + 1) % k][0]
            idx = np.arange(i0, i1 + (n if i1 <= i0 else 0)) % n
            if len(idx) < 6:
                continue
            a, b = B[idx[0]], B[idx[-1]]
            ab = b - a
            L = math.hypot(*ab)
            if L < 1e-6:
                continue
            dev = np.abs((B[idx, 0] - a[0]) * ab[1] - (B[idx, 1] - a[1]) * ab[0]) / L
            q = int(dev.argmax())
            if dev[q] > P.get('split_px', 1e9):
                bi = int(idx[q])
                merged.insert(j + 1, [bi, float(B[bi, 0]), float(B[bi, 1]), 0.0])
                merged.sort(key=lambda t: t[0])
                added += 1; changed = True
                break
        if not changed:
            break
    info['added_corners'] = added
    if len(merged) < 3:
        # too few corners found: the field-oriented bounding rectangle of the predicted mask
        info['fallback'] = 'rect'
        c, s = math.cos(gtheta), math.sin(gtheta)
        R = B @ np.array([[c, -s], [s, c]])
        x0, y0 = R.min(0); x1, y1 = R.max(0)
        rect = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]) @ np.array([[c, s], [-s, c]])
        return rect, info
    raw = np.array([[m[1], m[2]] for m in merged])
    if not P.get('snap', True):
        return raw, info
    # sides between consecutive corners: boundary points strictly between the two attachment indices
    k = len(merged)
    sides = []
    for j in range(k):
        i0, i1 = merged[j][0], merged[(j + 1) % k][0]
        idx = np.arange(i0, i1 + (n if i1 <= i0 else 0)) % n
        seg = B[idx]
        inner = seg[len(seg) // 6: len(seg) - len(seg) // 6] if len(seg) >= 6 else seg
        a, b = raw[j], raw[(j + 1) % k]
        chord = math.atan2(b[1] - a[1], b[0] - a[0])
        fth, coh = field_angle(field, inner if len(inner) else seg)
        if ang_diff90(fth, gtheta) < math.radians(P['global_deg']):
            fth = gtheta
        snapped = ang_diff90(chord, fth) < math.radians(P['snap_deg']) and coh > P['coh_min']
        if snapped:
            cands = [fth + q * math.pi / 2 for q in range(4)]
            th = min(cands, key=lambda t: abs(math.atan2(math.sin(t - chord), math.cos(t - chord))))
        else:
            th = chord
        d = np.array([math.cos(th), math.sin(th)])
        nrm = np.array([-d[1], d[0]])
        pts = inner if len(inner) >= 2 else np.array([a, b])
        off = float(np.median(pts @ nrm))
        p0 = nrm * off
        sides.append({'d': d, 'p': p0, 'snapped': snapped, 'th': th % math.pi, 'pts': seg})
    # merge neighbouring sides that are parallel (a spurious corner on a straight side)
    changed = True
    while changed and len(sides) > 3:
        changed = False
        for j in range(len(sides)):
            s1, s2 = sides[j], sides[(j + 1) % len(sides)]
            if ang_diff90(s1['th'], s2['th']) < math.radians(8) and abs(math.sin(s1['th'] - s2['th'])) < math.sin(math.radians(20)):
                pts = np.concatenate([s1['pts'], s2['pts']])
                nrm = np.array([-s1['d'][1], s1['d'][0]])
                s1['p'] = nrm * float(np.median(pts @ nrm)); s1['pts'] = pts
                sides.pop((j + 1) % len(sides)); changed = True
                break
    info['merged_sides'] = k - len(sides)
    if P.get('square', False):
        sides = square_sides(sides, mask, P, info)
    ring = ring_of(sides, P)
    if len(ring) < 3 or not Polygon(ring).is_valid:
        info['fallback'] = 'raw'
        return raw, info
    return ring, info


def ring_of(sides, P):
    """Corners where neighbouring sides meet (falls back to the side's first boundary point when the meeting point is far)."""
    ring = []
    for j in range(len(sides)):
        s1, s2 = sides[j - 1], sides[j]
        q = line_intersect(s1['p'], s1['d'], s2['p'], s2['d'])
        start = s2['pts'][0]
        if q is None or np.hypot(*(q - start)) > P['max_move_px']:
            q = start
        ring.append(q)
    return np.array(ring)


def mask_iou(ring, mask):
    from shapely import contains_xy
    pg = Polygon(ring)
    if len(ring) < 3 or not pg.is_valid:
        return -1.0
    ys, xs = np.mgrid[0:S, 0:S]
    a = contains_xy(pg, xs + 0.5, ys + 0.5)
    b = mask >= 0.5
    return float((a & b).sum() / max((a | b).sum(), 1))


def square_sides(sides, mask, P, info):
    """Assembly v3: square off sides that are off the roof's main axes but sit between two sides on them.
    The main axes are the length-weighted mean side direction modulo 90 degrees (a side snapped to the local direction
    field can still be off them, e.g. a cut-off corner).
    Between perpendicular neighbours the side is a cut-off corner: drop it so the neighbours meet.
    Between parallel neighbours it is a step drawn as a diagonal: replace it with a perpendicular side through one of
    its ends (whichever fits the predicted mask better). Either is kept only when the mask fit drops by < square_tol."""
    squared = 0
    for _ in range(len(sides)):
        lens = [float(np.hypot(*(s['pts'][-1] - s['pts'][0]))) for s in sides]
        dom = math.atan2(sum(w * math.sin(4 * s['th']) for w, s in zip(lens, sides)),
                         sum(w * math.cos(4 * s['th']) for w, s in zip(lens, sides))) / 4
        on = [ang_diff90(s['th'], dom) < math.radians(P.get('axis_deg', 10.0)) for s in sides]
        if len(sides) <= 4 and all(on):
            break
        base = mask_iou(ring_of(sides, P), mask)
        best = None
        for j in range(len(sides)):
            s0, s, s1 = sides[j - 1], sides[j], sides[(j + 1) % len(sides)]
            if on[j] and on[(j + 1) % len(sides)] and len(sides) > 4 and abs(math.sin(s['th'] - s1['th'])) < math.sin(math.radians(30)):
                # two neighbouring sides on the same axis: one bent side, so merge them on the longer one's direction
                d = s['d'] if lens[j] >= lens[(j + 1) % len(sides)] else s1['d']
                pts = np.concatenate([s['pts'], s1['pts']])
                nrm = np.array([-d[1], d[0]])
                m = {'d': d, 'p': nrm * float(np.median(pts @ nrm)), 'snapped': True, 'th': math.atan2(d[1], d[0]) % math.pi, 'pts': pts}
                c = [m if i == j else t for i, t in enumerate(sides) if i != (j + 1) % len(sides)]
                fit = mask_iou(ring_of(c, P), mask)
                if fit >= base - P.get('square_tol', 0.01) and (best is None or fit > best[0]):
                    best = (fit, c)
            if on[j] or not (on[j - 1] and on[(j + 1) % len(sides)]) or len(sides) <= 3:
                continue
            cands = []
            if abs(math.sin(s0['th'] - s1['th'])) > math.sin(math.radians(60)):
                cands.append(sides[:j] + sides[j + 1:])  # perpendicular neighbours: drop the cut (outer corner)
                a = line_intersect(s0['p'], s0['d'], s['p'], s['d'])
                b = line_intersect(s['p'], s['d'], s1['p'], s1['d'])
                if a is not None and b is not None:  # or an inner corner: the diagonal across an L's notch
                    n1, n0 = np.array([-s1['d'][1], s1['d'][0]]), np.array([-s0['d'][1], s0['d'][0]])
                    t1 = {'d': s1['d'], 'p': n1 * float(a @ n1), 'snapped': True, 'th': s1['th'], 'pts': s['pts'][:max(len(s['pts']) // 2, 1)]}
                    t2 = {'d': s0['d'], 'p': n0 * float(b @ n0), 'snapped': True, 'th': s0['th'], 'pts': s['pts'][len(s['pts']) // 2:]}
                    cands.append(sides[:j] + [t1, t2] + sides[j + 1:])
            elif abs(math.sin(s0['th'] - s1['th'])) < math.sin(math.radians(30)):
                d = np.array([-s0['d'][1], s0['d'][0]])  # parallel neighbours: a perpendicular step
                nrm = np.array([-d[1], d[0]])
                for end in (line_intersect(s0['p'], s0['d'], s['p'], s['d']), line_intersect(s['p'], s['d'], s1['p'], s1['d'])):
                    if end is None:
                        continue
                    step = {'d': d, 'p': nrm * float(end @ nrm), 'snapped': True, 'th': math.atan2(d[1], d[0]) % math.pi,
                            'pts': s['pts']}
                    cands.append(sides[:j] + [step] + sides[j + 1:])
            for c in cands:
                fit = mask_iou(ring_of(c, P), mask)
                if fit >= base - P.get('square_tol', 0.01) and (best is None or fit > best[0]):
                    best = (fit, c)
        if best is None:
            break
        sides = best[1]; squared += 1
    info['squared'] = squared
    return sides


DEFAULT_P = {'corner_thr': 0.3, 'attach_px': 4.0, 'merge_px': 5.0, 'snap_deg': 20.0, 'global_deg': 6.0, 'coh_min': 0.2,
             'max_move_px': 12.0, 'snap': True}


def to_scene(row, M, ring_c):
    Mi = invert(M)
    ox, oy = row['offset']
    return [[float(x + ox), float(y + oy)] for x, y in apply(Mi, ring_c)]


def load_model(path, dev):
    import torch
    m = build_model()
    m.load_state_dict(torch.load(path, map_location='cpu')['model'])
    return m.to(dev).eval()


def run_rows(model, rows, P, dev, raw_cache=None):
    out = []
    for r in rows:
        if raw_cache is not None and r['key'] in raw_cache:
            mask, heat, field, M = raw_cache[r['key']]
        else:
            mask, heat, field, M, _ = predict(model, r, dev)
            if raw_cache is not None:
                raw_cache[r['key']] = (mask, heat, field, M)
        ring_c, info = assemble(mask, heat, field, P)
        ring = to_scene(r, M, ring_c) if ring_c is not None else r['saved']
        out.append({'key': r['key'], 'scene': r['scene'], 'object_id': r['object_id'], 'split': r['split'], 'ring': ring,
                    'info': info})
    return out


def score(rows, preds):
    by = {p['key']: p for p in preds}
    res = {}
    for name, get in (('today', lambda r: r['saved']), ('model', lambda r: by[r['key']]['ring'])):
        cs, ex, ious, hd, iou_s = [], [], [], [], []
        for r in rows:
            g = get(r)
            cs.append(len(g)); ex.append(len(g) == len(r['target']))
            ious.append(iou(g, r['target'])); hd.append(hausdorff(g, r['target'])); iou_s.append(iou(g, r['saved']))
        res[name] = {'n': len(rows), 'corners_median': statistics.median(cs), 'corners_mean': round(statistics.mean(cs), 2),
                     'exact_corner_share': round(sum(ex) / len(ex), 3), 'iou_vs_target_median': round(statistics.median(ious), 4),
                     'iou_vs_target_mean': round(statistics.mean(ious), 4),
                     'hausdorff_px_median': round(statistics.median(hd), 2), 'iou_vs_today_median': round(statistics.median(iou_s), 4)}
    res['target_corners_median'] = statistics.median(len(r['target']) for r in rows)
    return res


def cmd_tune(args):
    import torch
    dev = torch.device(args.device)
    work = Path(args.work)
    rows = [r for r in load_rows(work, 'approved.json') if r['split'] == 'train' and r['val']]
    model = load_model(work / 'ckpt-val.pt', dev)
    cache = {}
    results = []
    for thr in (0.1, 0.15, 0.2, 0.3, 0.4):
        for snap in (12.0, 20.0, 30.0, 40.0):
            for merge in (4.0, 6.0, 8.0):
                for split in (1e9, 3.0, 4.5, 6.0):
                    P = dict(DEFAULT_P, corner_thr=thr, snap_deg=snap, merge_px=merge, split_px=split)
                    s = score(rows, run_rows(model, rows, P, dev, cache))['model']
                    # objective: corner-count exactness first, then fit
                    results.append((s['exact_corner_share'] + s['iou_vs_target_mean'], P, s))
    results.sort(key=lambda t: -t[0])
    best = results[0]
    (work / 'tuned.json').write_text(json.dumps({'n_val': len(rows), 'params': best[1], 'val_score': best[2],
                                                 'top5': [{'obj': round(o, 4), 'params': p, 'score': s} for o, p, s in results[:5]]}, indent=1))
    print(json.dumps({'n_val': len(rows), 'best': best[1], 'score': best[2]}, indent=1))


def cmd_eval(args):
    import torch
    dev = torch.device(args.device)
    work = Path(args.work)
    P = json.load(open(work / 'tuned.json'))['params'] if (work / 'tuned.json').exists() else DEFAULT_P
    model = load_model(work / args.ckpt, dev)
    if args.others:
        rows = load_rows(work, 'others.json')
        preds = run_rows(model, rows, P, dev)
        (work / 'pred-others.json').write_text(json.dumps(preds))
        mc = [len(p['ring']) for p in preds]; tc = [len(r['saved']) for r in rows]
        dist = {}
        for c in mc:
            dist[c] = dist.get(c, 0) + 1
        iou_s = [iou(p['ring'], r['saved']) for p, r in zip(preds, rows)]
        summ = {'n': len(rows), 'scenes': len({r['scene'] for r in rows}), 'today_corners_median': statistics.median(tc),
                'model_corners_median': statistics.median(mc), 'model_corner_distribution': dict(sorted(dist.items())),
                'model_share_4': round(sum(c == 4 for c in mc) / len(mc), 3), 'model_share_le6': round(sum(c <= 6 for c in mc) / len(mc), 3),
                'iou_vs_today_median': round(statistics.median(iou_s), 4), 'iou_vs_today_p10': round(float(np.percentile(iou_s, 10)), 4),
                'fallbacks': sum(1 for p in preds if p['info'].get('fallback'))}
        (work / 'score-others.json').write_text(json.dumps(summ, indent=1))
        print(json.dumps(summ, indent=1))
        return
    rows = load_rows(work, 'approved.json')
    out = {'checkpoint': args.ckpt, 'params': P}
    allp = []
    for sp in ('train', 'test'):
        rs = [r for r in rows if r['split'] == sp]
        preds = run_rows(model, rs, P, dev)
        allp += preds
        out[sp] = score(rs, preds)
        Pr = dict(P, snap=False)
        out[sp]['model_corner_peaks_unsnapped'] = score(rs, run_rows(model, rs, Pr, dev))['model']
        out[sp]['fallbacks'] = sum(1 for p in preds if p['info'].get('fallback'))
    (work / f'pred-approved-{Path(args.ckpt).stem}.json').write_text(json.dumps(allp))
    (work / f'score-{Path(args.ckpt).stem}.json').write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


# ---------------------------------------------------------------- contact sheets
def cmd_sheets(args):
    from PIL import Image, ImageDraw
    work = Path(args.work)
    if args.others:
        rows = json.load(open(work / 'others.json'))
        preds = {p['key']: p for p in json.load(open(work / 'pred-others.json'))}
        rows = rows[:args.limit]
        name = 'sheet-others'
    else:
        rows = [r for r in json.load(open(work / 'approved.json')) if r['split'] == args.split]
        preds = {p['key']: p for p in json.load(open(work / f'pred-approved-{args.ckpt_stem}.json'))}
        name = f'sheet-{args.split}'
    T, Z = 240, 3.0  # tile px; crop covers Z x extent
    cols = 5
    per = cols * 5
    for si in range(0, len(rows), per):
        chunk = rows[si:si + per]
        sheet = Image.new('RGB', (cols * T, math.ceil(len(chunk) / cols) * (T + 18)), (20, 20, 20))
        for i, r in enumerate(chunk):
            L = scene_meta(r['scene'])
            img = Image.open(f"data/scenes/{r['scene']}/{L['image_sha256']}.png").convert('RGB')
            c, e = roof_frame(r['saved'])
            half = Z * e / 2
            box = (c[0] - half, c[1] - half, c[0] + half, c[1] + half)
            tile = img.crop(tuple(int(round(v)) for v in box)).resize((T, T), Image.BICUBIC)
            k = T / (box[2] - box[0])
            f = lambda ring: [((x - box[0]) * k, (y - box[1]) * k) for x, y in ring]
            d = ImageDraw.Draw(tile)
            d.polygon(f(r['saved']), outline=(150, 150, 150), width=2)
            if r.get('target'):
                d.polygon(f(r['target']), outline=(255, 220, 0), width=1)
            p = preds[r['key']]['ring']
            d.polygon(f(p), outline=(0, 230, 255), width=2)
            for x, y in f(p):
                d.ellipse((x - 2.5, y - 2.5, x + 2.5, y + 2.5), fill=(0, 230, 255))
            x0, y0 = (i % cols) * T, (i // cols) * (T + 18)
            sheet.paste(tile, (x0, y0))
            lab = f"{si + i + 1} {r['scene'][-10:]} t{len(r['saved'])} m{len(p)}" + (f" g{len(r['target'])}" if r.get('target') else '')
            ImageDraw.Draw(sheet).text((x0 + 3, y0 + T + 3), lab, fill=(230, 230, 230))
        sheet.save(work / f'{name}-{si // per + 1}.png')
    print('wrote', name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['prep', 'train', 'tune', 'eval', 'sheets'])
    ap.add_argument('--work', default='data/reviews/eyes/experiments/roof-corners-2026-10-10/model-v1')
    ap.add_argument('--n-other', dest='n_other', type=int, default=200)
    ap.add_argument('--approved', help='approved-set file (default approved-set-v1)')
    ap.add_argument('--val', action='store_true')
    ap.add_argument('--iters', type=int, default=4000)
    ap.add_argument('--batch', type=int, default=16)
    ap.add_argument('--lr', type=float, default=2e-3)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--threads', type=int, default=4)
    ap.add_argument('--device', default='cpu')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--mask-drop', dest='mask_drop', type=float, default=0.0)
    ap.add_argument('--oversample', type=float, default=1.0)
    ap.add_argument('--synth', type=float, default=0.0)
    ap.add_argument('--stop-at', dest='stop_at', type=int, default=0)
    ap.add_argument('--ckpt', default='ckpt-full.pt')
    ap.add_argument('--ckpt-stem', dest='ckpt_stem', default='ckpt-full')
    ap.add_argument('--others', action='store_true')
    ap.add_argument('--split', default='test')
    ap.add_argument('--limit', type=int, default=50)
    args = ap.parse_args()
    globals()['cmd_' + args.cmd](args)


if __name__ == '__main__':
    main()
