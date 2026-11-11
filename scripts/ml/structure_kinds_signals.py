"""Extra per-object signals for structure sub-kinds (structure-kinds-signals-v1).

These address the error sources found in the 26 September agent review of the rule hints:
  oriented ribs      the crop is rotated to the outline's long axis before measuring, so the rib
                     period is read along and across the box instead of along image rows; the
                     axis-agnostic measurement returned None on most ribbed roofs.
  lab variance       the see-through ratio is recomputed in LAB on the original image; CLAHE inflates
                     the inside/outside variance ratio of solid roofs.
  roof continuity    for a candidate touching a larger roof: chromaticity (LAB a, b) difference across
                     the shared boundary and edge strength along it. A wing of the same roof has no
                     eave line and the same material; a garage against a house has both.
  multi-date         from the multidate-agreement-v1 sidecar when the tile has a second capture: the
                     outline's edge support on other dates. Objects unsupported on another date are
                     vehicles, caravans, boats or temporary structures far more often than garages.
  sam3 embedding     optional (--embed): the SAM3 vision encoder's finest FPN map (256 channels at
                     about 1 m per cell here) mean-pooled inside the outline mask, plus the same for a
                     2 m ring outside. One encode per tile, reused for every object. This is the input
                     to the classifier head in structure_kinds_model; SAM3 itself is not retrained.

Rows are written to the eyes index (object_quality, version structure-kinds-signals-v1) for the
objects that have a structure-kinds row; nothing here rewrites latest.json or another version.
All values are model estimates, not measurements of the building.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import eyes_index
from . import structure_kinds as sk
from .scene_object_quality import _row

VERSION = 'structure-kinds-signals-v1'
TABLE = 'object_quality'
MULTIDATE_VERSION = 'multidate-agreement-v1'
EMBED_DIR = Path('data/reviews/eyes/structure-kinds/embeddings')
PARAMS = {'erode_m': .5, 'ring_m': 2., 'boundary_band_m': .6, 'min_pixels': 12, 'rib_min_acf': .2,
          'continuity_chroma_same': 6., 'continuity_edge_eave': 25., 'multidate_absent_support': .25}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _num(value, digits=4):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


# --- oriented rib measurement ------------------------------------------------------------------------------

def oriented_crop(gray, mask, axis_deg_px, scale=1.):
    """Rotate `gray` and `mask` so the outline's long axis is horizontal; returns (gray_rot, mask_rot)."""
    h, w = gray.shape
    centre = (w/2., h/2.)
    matrix = cv2.getRotationMatrix2D(centre, axis_deg_px, scale)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w, new_h = int(h*sin+w*cos)+2, int(h*cos+w*sin)+2
    matrix[0, 2] += new_w/2.-centre[0]
    matrix[1, 2] += new_h/2.-centre[1]
    gray_rot = cv2.warpAffine(gray, matrix, (new_w, new_h), flags=cv2.INTER_LINEAR, borderValue=0)
    mask_rot = cv2.warpAffine(mask.astype(np.uint8), matrix, (new_w, new_h), flags=cv2.INTER_NEAREST, borderValue=0) > 0
    return gray_rot, mask_rot


def profile_period(values, weights, px_m, min_acf=PARAMS['rib_min_acf']):
    """Dominant period (m) of a 1-D mean profile via autocorrelation; (period_m, peak_acf) or (None, peak)."""
    valid = weights >= 3
    if valid.sum() < 8:
        return None, None
    profile = np.where(valid, values/np.maximum(weights, 1), np.nan)
    profile = np.interp(np.arange(len(profile)), np.nonzero(valid)[0], profile[valid])
    profile = profile-profile.mean()
    energy = float(np.dot(profile, profile))
    if energy <= 0:
        return None, None
    acf = np.correlate(profile, profile, 'full')[len(profile)-1:]/energy
    best = None
    for lag in range(2, len(acf)-1):
        if acf[lag] >= acf[lag-1] and acf[lag] >= acf[lag+1] and acf[lag-1] < acf[lag]:
            best = (lag, float(acf[lag]))
            break
    if best is None:
        return None, None
    return (_num(best[0]*px_m, 3) if best[1] >= min_acf else None), _num(best[1])


def oriented_ribs(gray, mask, axis_deg_px, px_m):
    """Rib period along and across the long axis, and gradient anisotropy along the axis-aligned directions."""
    gray_rot, mask_rot = oriented_crop(gray.astype(np.float32), mask, axis_deg_px)
    if mask_rot.sum() < PARAMS['min_pixels']:
        return {'period_along_m': None, 'period_across_m': None, 'acf_along': None, 'acf_across': None,
                'axis_gradient_share': None, 'orientation': 'too thin'}
    ys, xs = np.nonzero(mask_rot)
    along = np.bincount(xs, weights=gray_rot[ys, xs], minlength=gray_rot.shape[1])
    along_n = np.bincount(xs, minlength=gray_rot.shape[1])
    across = np.bincount(ys, weights=gray_rot[ys, xs], minlength=gray_rot.shape[0])
    across_n = np.bincount(ys, minlength=gray_rot.shape[0])
    p_along, a_along = profile_period(along, along_n, px_m)
    p_across, a_across = profile_period(across, across_n, px_m)
    gx = cv2.Sobel(gray_rot, cv2.CV_32F, 1, 0, ksize=3)[mask_rot]
    gy = cv2.Sobel(gray_rot, cv2.CV_32F, 0, 1, ksize=3)[mask_rot]
    total = float(np.abs(gx).sum()+np.abs(gy).sum())
    return {'period_along_m': p_along, 'period_across_m': p_across, 'acf_along': a_along, 'acf_across': a_across,
            'axis_gradient_share': _num(np.abs(gx).sum()/total) if total > 0 else None, 'orientation': 'long axis horizontal'}


# --- LAB variance and roof continuity ----------------------------------------------------------------------

def lab_variance_ratio(lab_l, inside, ring):
    if inside.sum() < PARAMS['min_pixels'] or ring.sum() < PARAMS['min_pixels']:
        return None
    outside = float(np.var(lab_l[ring]))
    return _num(float(np.var(lab_l[inside]))/outside) if outside > 0 else None


def roof_continuity(lab, gray, shape, neighbour, scene, origin, size):
    """Chromaticity step and edge strength across the boundary shared with an attached larger roof."""
    band = PARAMS['boundary_band_m']
    shared = shape.buffer(band).intersection(neighbour.buffer(band))
    if shared.is_empty:
        return None
    ours = sk._raster(sk.to_pixels(shared.intersection(shape.buffer(-.1)), scene), origin, size)
    theirs = sk._raster(sk.to_pixels(shared.intersection(neighbour.buffer(-.1)), scene), origin, size)
    seam = sk._raster(sk.to_pixels(shape.exterior.buffer(.3).intersection(neighbour.buffer(band)), scene), origin, size)
    if ours.sum() < 6 or theirs.sum() < 6:
        return None
    a_ours, b_ours = float(np.median(lab[..., 1][ours])), float(np.median(lab[..., 2][ours]))
    a_them, b_them = float(np.median(lab[..., 1][theirs])), float(np.median(lab[..., 2][theirs]))
    chroma = math.hypot(a_ours-a_them, b_ours-b_them)
    l_step = abs(float(np.median(lab[..., 0][ours]))-float(np.median(lab[..., 0][theirs])))
    grad = np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    edge = float(np.median(grad[seam])) if seam.sum() >= 6 else None
    same_material = chroma <= PARAMS['continuity_chroma_same']
    eave_line = edge is not None and edge >= PARAMS['continuity_edge_eave']
    return {'chroma_step': _num(chroma, 2), 'l_step': _num(l_step, 2), 'seam_edge_median': _num(edge, 2),
            'same_material': bool(same_material), 'eave_line': bool(eave_line),
            'continuous_roof_hint': bool(same_material and not eave_line), 'shared_boundary_pixels': int(seam.sum())}


# --- multi-date --------------------------------------------------------------------------------------------

def multidate_support(root, scene_id):
    """{object_id: {'dates': [...], 'other_date_support_min': x, 'saved_support': y}} from multidate-agreement-v1."""
    out = {}
    if not eyes_index.path(root).exists():
        return out
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = eyes_index.rows(db, 'multidate', scene_id=scene_id, version=MULTIDATE_VERSION)
    for r in rows:
        p = r.get('payload') or {}
        by_date = p.get('edge_support_by_date') or {}
        others = {d: v.get('support') for d, v in by_date.items() if d != 'saved' and isinstance(v, dict) and v.get('support') is not None}
        out[r['object_id']] = {'dates': sorted(by_date), 'saved_support': _num((by_date.get('saved') or {}).get('support')),
                               'other_date_support_min': _num(min(others.values())) if others else None,
                               'other_dates': sorted(others),
                               'absent_on_another_date': bool(others) and min(others.values()) < PARAMS['multidate_absent_support']}
    return out


# --- SAM3 embedding ----------------------------------------------------------------------------------------

class Embedder:
    """Mean-pools the SAM3 finest FPN map inside each object mask (and in a 2 m ring), one encode per tile."""

    def __init__(self, models_dir=sk.SAM3_MODELS):
        from .sam3 import Sam3TextSegmenter
        self.segmenter = Sam3TextSegmenter(Path(models_dir))
        self.timings = []

    def encode(self, image_rgb):
        started = time.monotonic()
        features = self.segmenter.encode_image(Image.fromarray(np.asarray(image_rgb, dtype=np.uint8)))
        self.timings.append(round(time.monotonic()-started, 2))
        return features[0][0]   # (256, H', W') finest FPN level

    @staticmethod
    def pool(fmap, mask_full):
        """Mean of the feature map over a full-frame boolean mask (mask resized to the map grid)."""
        grid = cv2.resize(mask_full.astype(np.uint8), (fmap.shape[2], fmap.shape[1]), interpolation=cv2.INTER_AREA) > 0
        if not grid.any():
            grid = cv2.resize(mask_full.astype(np.uint8)*255, (fmap.shape[2], fmap.shape[1]), interpolation=cv2.INTER_AREA) > 0
        if not grid.any():
            return None
        return fmap[:, grid].mean(axis=1).astype(np.float32)


# --- assessment ----------------------------------------------------------------------------------------------

def signals_for_scene(root, scene, image, keys, *, embedder=None, multidate=None):
    """Signal rows for the objects in `keys` (object ids); returns (rows, embeddings {object_id: vector})."""
    shapes = sk.building_shapes(scene)
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    px_m = sk.pixel_size_m(scene)
    multidate = multidate if multidate is not None else multidate_support(root, scene['id'])
    fmap = embedder.encode(image) if embedder is not None and image is not None else None
    lab_full = cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2LAB) if image is not None else None
    gray_full = cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2GRAY).astype(np.float32) if image is not None else None
    rows, embeddings = [], {}
    for obj in sk.live_buildings(scene):
        if obj['id'] not in keys or obj['id'] not in shapes:
            continue
        shape = shapes[obj['id']]
        geometry = sk.geometry_features(shape, vertex_count=len(obj.get('ring') or []))
        payload = {'version': VERSION, 'note': sk.NOTE, 'kind': 'building', 'area_m2': geometry['area_m2'],
                   'ribs_oriented': None, 'lab_variance_ratio_original': None, 'roof_continuity': None,
                   'multidate': multidate.get(obj['id']), 'embedding': None, 'image': 'unavailable',
                   'computed_at': stamp, 'code_sha256': code}
        if image is not None:
            outer = shape.buffer(PARAMS['ring_m']+PARAMS['boundary_band_m'])
            b = sk.to_pixels(outer, scene).bounds
            x0, y0 = max(int(b[0])-2, 0), max(int(b[1])-2, 0)
            x1, y1 = min(int(b[2])+3, image.shape[1]), min(int(b[3])+3, image.shape[0])
            if x1-x0 >= 3 and y1-y0 >= 3:
                origin, size = (x0, y0), (y1-y0, x1-x0)
                lab, gray = lab_full[y0:y1, x0:x1], gray_full[y0:y1, x0:x1]
                inner = shape.buffer(-PARAMS['erode_m'])
                inside = sk._raster(sk.to_pixels(inner if not inner.is_empty else shape, scene), origin, size)
                others = [s for oid, s in shapes.items() if oid != obj['id']]
                ring_geom = shape.buffer(PARAMS['ring_m']).difference(shape)
                for other in others:
                    if other.intersects(ring_geom):
                        ring_geom = ring_geom.difference(other)
                ring = sk._raster(sk.to_pixels(ring_geom, scene), origin, size)
                payload['image'] = 'original'
                if inside.sum() >= PARAMS['min_pixels'] and geometry.get('long_axis_deg') is not None:
                    axis_px = sk._pixel_axis_deg(geometry['long_axis_deg'], scene)
                    payload['ribs_oriented'] = oriented_ribs(gray, inside, axis_px, px_m)
                payload['lab_variance_ratio_original'] = lab_variance_ratio(lab[..., 0].astype(np.float32), inside, ring)
                attached = [(s.distance(shape), s) for s in others if s.area > shape.area and s.distance(shape) < sk.DESIGN['attached_m']]
                if attached:
                    payload['roof_continuity'] = roof_continuity(lab.astype(np.float32), gray, shape, min(attached, key=lambda item: item[0])[1], scene, origin, size)
            if fmap is not None:
                full = sk._raster(sk.to_pixels(shape, scene), (0, 0), image.shape[:2])
                ring_full = sk._raster(sk.to_pixels(shape.buffer(PARAMS['ring_m']).difference(shape), scene), (0, 0), image.shape[:2])
                inside_vec, ring_vec = Embedder.pool(fmap, full), Embedder.pool(fmap, ring_full)
                if inside_vec is not None:
                    vector = np.concatenate([inside_vec, ring_vec if ring_vec is not None else np.zeros_like(inside_vec)])
                    embeddings[obj['id']] = vector
                    payload['embedding'] = {'source': 'sam3 fpn0 mean-pooled inside + 2 m ring', 'dims': int(vector.size),
                                            'file': str(EMBED_DIR/f"{scene['id']}.npz")}
        rows.append(_row(scene, obj, sk.plain(payload), VERSION))
    return rows, embeddings


def keys_with_hints(root, scene_ids):
    """{scene_id: {object_id}} of objects that have a structure-kinds row (current VERSION)."""
    found = {}
    with closing(eyes_index.connection(root, readonly=True)) as db:
        for sid in dict.fromkeys(scene_ids):
            found[sid] = {r['object_id'] for r in eyes_index.rows(db, TABLE, scene_id=sid, version=sk.VERSION)}
    return found


def run(root=Path('.'), scene_ids=(), *, embed=False, write=True, log=None):
    root = Path(root)
    embedder = Embedder() if embed else None
    targets = keys_with_hints(root, scene_ids)
    rows = []
    for i, (sid, keys) in enumerate(targets.items()):
        if not keys:
            continue
        scene = sk.load_scene(root, sid)
        image = sk.load_image(root, scene)
        started = time.monotonic()
        scene_rows, embeddings = signals_for_scene(root, scene, image, keys, embedder=embedder)
        rows.extend(scene_rows)
        if embeddings:
            EMBED_DIR.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(root/EMBED_DIR/f'{sid}.npz', **{oid: vec for oid, vec in embeddings.items()},
                                __meta__=np.array(json.dumps({'scene_id': sid, 'model_signature': scene.get('model_signature'),
                                                              'image_sha256': scene.get('image_sha256'), 'version': VERSION})))
        if log:
            log(f'[{i+1}/{len(targets)}] {sid}: {len(scene_rows)} rows, {len(embeddings)} embeddings, {time.monotonic()-started:.1f}s')
    written = 0
    if write and rows:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            written = sk.replace_scene_rows(db, TABLE, VERSION, [s for s, k in targets.items() if k], rows)
    return rows, written


def main(argv=None):
    parser = argparse.ArgumentParser(description='Extra structure sub-kind signals (oriented ribs, LAB variance, roof continuity, '
                                                 'multi-date support, optional SAM3 embeddings). Estimates, not labels.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='+', required=True)
    parser.add_argument('--embed', action='store_true', help='Also pool SAM3 embeddings per object (about 7 s per tile).')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    log = None if args.quiet else (lambda t: print(t, file=sys.stderr, flush=True))
    rows, written = run(Path(args.root), args.scenes, embed=args.embed, write=not args.dry_run, log=log)
    ribs = [r['payload']['ribs_oriented'] for r in rows if r['payload'].get('ribs_oriented')]
    out = {'version': VERSION, 'rows': len(rows), 'written': written,
           'rib_period_measured': sum(1 for r in ribs if r.get('period_along_m') or r.get('period_across_m')),
           'roof_continuity_rows': sum(1 for r in rows if r['payload'].get('roof_continuity')),
           'continuous_roof_hints': sum(1 for r in rows if (r['payload'].get('roof_continuity') or {}).get('continuous_roof_hint')),
           'multidate_rows': sum(1 for r in rows if r['payload'].get('multidate')),
           'absent_on_another_date': sum(1 for r in rows if (r['payload'].get('multidate') or {}).get('absent_on_another_date')),
           'embeddings': sum(1 for r in rows if r['payload'].get('embedding'))}
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(out, indent=1))
    json.dump(out, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
