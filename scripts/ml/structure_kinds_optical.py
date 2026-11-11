"""Optical stages for structure candidates (structure-kinds-optical-v1): hi-res crops, deskew, OCR, vehicles, heights.

A cascaded pipeline, never run on the whole tile:
  stage 1  roof localisation: the scene's live building outlines (SAM3, already refined) are the detections.
  stage 2  oriented crop and deskew: the minimum rotated rectangle gives the angle; the padded crop is rotated
           so the long axis is horizontal before any text or rib reading (upright text, fewer OCR errors).
  stage 3  resolution and contrast: the crop is re-fetched from the imagery provider at its maximum zoom
           (Google z20 here, about 0.12 m/px, 2x the saved scene) using the cached ImageryFetcher child tiles,
           then CLAHE on LAB L (the editor recipe) and a 2x Lanczos upscale. No learned super-resolution is
           used: reconstructed edges would be evidence that was never observed.
  stage 4  scene-text recognition: RapidOCR (DBNet detection + CRNN recognition, ONNX, CPU) on the deskewed
           crop at 0 and 180 degrees; detections carry text, score and box. At 0.12 m/px only roof-painted
           lettering of about 1 m or more is resolvable; the stage records what it found and its timings.
  stage 5  batching: one hi-res fetch set and one SAM3 encode per tile, crops processed per tile; ONNX INT8
           weights for SAM3 are already the runtime; the OCR models are FP32 ONNX (see report timings).

Two more signals that the rule hints lacked:
  vehicles  SAM3 open-vocabulary prompts ('car', 'caravan', 'boat', 'truck') against a 'building roof' contrast
            prompt: the candidate's mask IoU with vehicle proposals must beat its roof IoU (vehicle_like), plus the
            count of car proposals within 5 m. Containers, caravans and boats share dimensions; this is the
            separating cue. The first design (any vehicle prompt >= 0.4, including 'trailer') flagged 22 of 48
            reviewed sheds/garages on 27 September because SAM3 returns a high-IoU proposal for almost any box
            under almost any prompt. With the roof contrast the call is selective but insensitive: on the
            same day 1 of 44 reviewed non-vehicles and 0 of 4 observed caravans/boats were flagged, because
            SAM3 also returns a 'building roof' proposal (IoU about 0.9) for caravans and boats. Both IoUs and
            scores are recorded per object for a later learned head; the boolean is a conservative hint.
  heights   relative shadow heights. The tile's sun-shadow direction is estimated from the luminance drop
            around every building; each object's shadow length is read along that direction; the object's
            height is its shadow length relative to the tile's median primary-roof shadow, times a 3 m eave
            prior. This is a relative estimate with a stated prior, not a measured height.

Rows go to the eyes index (object_quality, version structure-kinds-optical-v1); hi-res child tiles are cached
under data/imagery/google/20 by the existing fetcher. Nothing rewrites latest.json or another version.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
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
from .tiles import lonlat_to_tile_xy

VERSION = 'structure-kinds-optical-v1'
TABLE = 'object_quality'
REPORT_DIR = Path('data/reviews/eyes/structure-kinds/optical')
VEHICLE_PROMPTS = ('car', 'caravan', 'boat', 'truck')
ROOF_CONTRAST_PROMPT = 'building roof'   # SAM3 matches many boxes to any prompt; a vehicle call needs the roof prompt to lose
WATERMARK_WORDS = ('google', 'maxar', 'airbus', 'cnes', 'landsat', 'copernicus', 'imagery', 'data')
YEAR_PATTERN = re.compile(r'^[o0c©(]*\s*(19|20)\d\d\s*$|^\d{2,4}$')
PARAMS = {'pad_m': 2.5, 'hires_zoom': 20, 'upscale': 2, 'ocr_min_score': .5, 'ocr_angles': (0, 180),
          'vehicle_iou': .4, 'vehicle_roof_max': .3, 'car_near_m': 5., 'shadow_drop_l': 12., 'shadow_max_m': 8., 'shadow_min_m': .3,
          'primary_area_m2': 90., 'eave_prior_m': 3., 'sam3_threshold': .05, 'sam3_max_proposals': 64}
CLAHE = {'clip_limit': 3.0, 'tile_grid': (12, 12)}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _num(v, d=4):
    if v is None:
        return None
    v = float(v)
    return round(v, d) if math.isfinite(v) else None


# --- stage 3a: hi-res re-fetch -------------------------------------------------------------------------------

def frame_tile_mapping(scene, zoom=None):
    """Linear map from scene pixels to fractional Web-Mercator tile coordinates at `zoom` (default: the scene's
    analysis zoom, or 17). Uses region.bounds only, so per-address scenes without an analysis tile work too.
    Validated on tile scenes by `tile_corner_px` (the analysis tile corner lands on the 64 px margin)."""
    z = zoom if zoom is not None else (scene.get('analysis_tile') or [17])[0]
    w, h = scene['image_width'], scene['image_height']
    west, south, east, north = scene['region']['bounds']
    tx0, ty0 = lonlat_to_tile_xy(west, north, z)
    tx1, ty1 = lonlat_to_tile_xy(east, south, z)
    return {'z': z, 'tx0': tx0, 'ty0': ty0, 'sx': (tx1-tx0)/w, 'sy': (ty1-ty0)/h, 'tile': tuple((scene.get('analysis_tile') or [z, None, None])[1:])}


def tile_corner_px(scene):
    """Scene-pixel position of the analysis tile's north-west corner under the linear frame map (expected margin)."""
    m = frame_tile_mapping(scene)
    x, y = m['tile']
    if x is None:
        return None
    return (x-m['tx0'])/m['sx'], (y-m['ty0'])/m['sy']


def pixel_to_tilexy(scene, px, py, zoom):
    m = frame_tile_mapping(scene, zoom)
    return m['tx0']+px*m['sx'], m['ty0']+py*m['sy']


class HiResSource:
    """Stitches provider child tiles at `zoom` for a pixel box of the scene; caches children via ImageryFetcher."""

    def __init__(self, provider='google', zoom=PARAMS['hires_zoom'], fetcher=None):
        from .imagery import PROVIDER_MAX_ZOOM, ImageryFetcher
        self.zoom = min(zoom, PROVIDER_MAX_ZOOM.get(provider, zoom))
        self.fetcher = fetcher or ImageryFetcher(provider)
        self.children = {}
        self.timings = []

    def child(self, tx, ty):
        key = (tx, ty)
        if key not in self.children:
            started = time.monotonic()
            self.children[key] = self.fetcher._child(self.zoom, tx, ty)
            self.timings.append(round(time.monotonic()-started, 2))
        return self.children[key]

    def crop(self, scene, x0, y0, x1, y1):
        """Hi-res RGB array covering scene pixels [x0,x1)x[y0,y1) and its metres per pixel; None when unavailable."""
        ax, ay = pixel_to_tilexy(scene, x0, y0, self.zoom)
        bx, by = pixel_to_tilexy(scene, x1, y1, self.zoom)
        left, top, right, bottom = ax*256, ay*256, bx*256, by*256
        tx0, ty0, tx1, ty1 = math.floor(left/256), math.floor(top/256), math.floor((right-1)/256), math.floor((bottom-1)/256)
        canvas = Image.new('RGB', (int(round(right-left)), int(round(bottom-top))), (0, 0, 0))
        missing = 0
        for ty in range(ty0, ty1+1):
            for tx in range(tx0, tx1+1):
                child = self.child(tx, ty)
                if child is None:
                    missing += 1
                    continue
                canvas.paste(child, (int(round(tx*256-left)), int(round(ty*256-top))))
        if missing == (tx1-tx0+1)*(ty1-ty0+1):
            return None, None
        lat = scene['centroid']['lat']
        mpp = 40075016.686*math.cos(math.radians(lat))/(256*2**self.zoom)
        return np.asarray(canvas), mpp


# --- stage 2: oriented crop and deskew ------------------------------------------------------------------------

def deskew(image, ring_px, angle_deg, pad_px, upscale=1):
    """Crop the padded ring box, rotate so the long axis is horizontal, optionally upscale (Lanczos)."""
    ring = np.asarray(ring_px, dtype=np.float64)
    x0, y0 = max(int(ring[:, 0].min()-pad_px), 0), max(int(ring[:, 1].min()-pad_px), 0)
    x1, y1 = min(int(ring[:, 0].max()+pad_px)+1, image.shape[1]), min(int(ring[:, 1].max()+pad_px)+1, image.shape[0])
    if x1-x0 < 4 or y1-y0 < 4:
        return None
    crop = np.ascontiguousarray(image[y0:y1, x0:x1])
    if upscale > 1:
        crop = cv2.resize(crop, (crop.shape[1]*upscale, crop.shape[0]*upscale), interpolation=cv2.INTER_LANCZOS4)
    h, w = crop.shape[:2]
    matrix = cv2.getRotationMatrix2D((w/2., h/2.), angle_deg, 1.)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    nw, nh = int(h*sin+w*cos)+2, int(h*cos+w*sin)+2
    matrix[0, 2] += nw/2.-w/2.
    matrix[1, 2] += nh/2.-h/2.
    rotated = cv2.warpAffine(crop, matrix, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    pts = (ring-[x0, y0])*upscale
    pts = np.c_[pts, np.ones(len(pts))] @ matrix.T
    return {'image': rotated, 'ring': pts, 'origin': (x0, y0), 'angle_deg': angle_deg, 'upscale': upscale}


def enhance(rgb):
    lab = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2LAB)
    lab[:, :, 0] = cv2.createCLAHE(clipLimit=CLAHE['clip_limit'], tileGridSize=CLAHE['tile_grid']).apply(lab[:, :, 0])
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    blur = cv2.GaussianBlur(out, (0, 0), 1.0)
    return cv2.addWeighted(out, 1.5, blur, -.5, 0)   # light unsharp mask


# --- stage 4: scene text ---------------------------------------------------------------------------------------

def watermark_like(text):
    """Provider watermark text ('©2026 Google', 'Maxar', years) read off hi-res tiles; never evidence about the roof."""
    cleaned = re.sub(r'[^a-z0-9]', '', str(text).lower())
    if not cleaned or YEAR_PATTERN.match(str(text).strip()):
        return True
    for word in WATERMARK_WORDS:
        if word in cleaned or difflib.SequenceMatcher(None, cleaned, word).ratio() >= .6:
            return True
    return False


class SceneText:
    """RapidOCR (DBNet + CRNN, ONNX) wrapper; unavailable is recorded, never raised. Watermarks are filtered."""

    def __init__(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR()
            self.available = True
        except Exception as error:   # optional dependency
            self.engine, self.available, self.error = None, False, str(error)
        self.timings = []

    def read(self, rgb, angles=PARAMS['ocr_angles'], min_score=PARAMS['ocr_min_score']):
        if not self.available:
            return {'available': False, 'detections': [], 'error': getattr(self, 'error', None)}
        started = time.monotonic()
        detections, watermarks = [], []
        for angle in angles:
            image = rgb if angle == 0 else cv2.rotate(rgb, cv2.ROTATE_180)
            result, _ = self.engine(cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2BGR))
            for box, text, score in result or []:
                if float(score) >= min_score and str(text).strip():
                    item = {'text': str(text).strip(), 'score': _num(score), 'angle': angle, 'box': [[_num(x, 1), _num(y, 1)] for x, y in box]}
                    (watermarks if watermark_like(item['text']) else detections).append(item)
        self.timings.append(round(time.monotonic()-started, 3))
        return {'available': True, 'detections': detections, 'watermarks_filtered': watermarks, 'text_found': bool(detections)}


# --- vehicles via SAM3 prompts ---------------------------------------------------------------------------------

class VehicleScorer:
    def __init__(self, models_dir=sk.SAM3_MODELS, prompts=VEHICLE_PROMPTS):
        from .sam3 import Sam3TextSegmenter
        self.segmenter = Sam3TextSegmenter(Path(models_dir))
        self.prompts = tuple(prompts)+(ROOF_CONTRAST_PROMPT,)
        self.timings = []

    def score(self, image_rgb, masks, centroids_px, px_m):
        started = time.monotonic()
        pil = Image.fromarray(np.asarray(image_rgb, dtype=np.uint8))
        embedding = self.segmenter.encode_image(pil)
        proposals = {p: self.segmenter.proposals(embedding, p, pil.size, threshold=PARAMS['sam3_threshold'],
                                                 max_proposals=PARAMS['sam3_max_proposals']) for p in self.prompts}
        out = {}
        for oid, mask in masks.items():
            area = int(mask.sum())
            best = {}
            for prompt, items in proposals.items():
                top = {'best_iou': 0., 'score': None}
                for pr in items:
                    pm = sk._fit_mask(pr.mask, mask.shape)
                    inter = int(np.count_nonzero(pm & mask))
                    if inter:
                        iou = inter/(int(np.count_nonzero(pm))+area-inter)
                        if iou > top['best_iou']:
                            top = {'best_iou': _num(iou), 'score': _num(pr.score)}
                best[prompt] = top
            cx, cy = centroids_px[oid]
            near = 0
            for pr in proposals.get('car', []):
                pm = sk._fit_mask(pr.mask, mask.shape)
                ys, xs = np.nonzero(pm)
                if len(xs) and math.hypot(xs.mean()-cx, ys.mean()-cy)*px_m <= PARAMS['car_near_m'] and not (pm & mask).any():
                    near += 1
            vehicle_iou = max((v['best_iou'] for p, v in best.items() if p != ROOF_CONTRAST_PROMPT), default=0.)
            roof_iou = best.get(ROOF_CONTRAST_PROMPT, {}).get('best_iou') or 0.
            out[oid] = {'prompts': best, 'vehicle_iou': _num(vehicle_iou), 'roof_iou': _num(roof_iou),
                        'vehicle_like': bool(vehicle_iou >= PARAMS['vehicle_iou'] and roof_iou < PARAMS['vehicle_roof_max'] and vehicle_iou > roof_iou),
                        'rule': f"max(car, caravan, boat, truck) >= {PARAMS['vehicle_iou']} and 'building roof' < {PARAMS['vehicle_roof_max']} and vehicle > roof",
                        'cars_within_5m': near}
        self.timings.append(round(time.monotonic()-started, 2))
        return out


# --- relative shadow heights -------------------------------------------------------------------------------------

def _ray_dark_length(lum, mask_all, own, x, y, dx, dy, dark, max_px, lead_px=3):
    """Dark run (px) along the ray from an edge sample.

    The object's own boundary pixels are skipped first, then up to `lead_px` bright pixels are tolerated (the
    outline usually sits just inside the eave, whose lit edge precedes the shadow); the run ends at the first
    bright pixel after it started, at another object, or at max_px.
    """
    n, started, lead = 0, False, 0
    for step in range(1, max_px+lead_px+8):
        px, py = int(round(x+dx*step)), int(round(y+dy*step))
        if not (0 <= px < lum.shape[1] and 0 <= py < lum.shape[0]):
            break
        if not started and own[py, px] and step <= 6:
            continue
        if mask_all[py, px] and not own[py, px]:
            break
        if lum[py, px] > dark:
            if n == 0 and lead < lead_px:
                lead += 1
                continue
            break
        started = True
        n += 1
        if n >= max_px:
            break
    return n


def shadow_direction(lum, shapes_px, mask_all, px_m, directions=16):
    """Sun-shadow direction (deg, image axes) as the direction with the largest luminance drop just outside outlines."""
    best, best_drop = None, -1.
    for k in range(directions):
        theta = 2*math.pi*k/directions
        dx, dy = math.cos(theta), math.sin(theta)
        drops = []
        for ring in shapes_px:
            inside = float(np.median(lum[ring['inside']])) if ring['inside'].any() else None
            if inside is None:
                continue
            samples = []
            for x, y in ring['edge'][::4]:
                px, py = int(round(x+dx*1.2/px_m)), int(round(y+dy*1.2/px_m))
                if 0 <= px < lum.shape[1] and 0 <= py < lum.shape[0] and not mask_all[py, px]:
                    samples.append(lum[py, px])
            if len(samples) >= 5:
                drops.append(inside-float(np.median(samples)))
        if drops:
            drop = float(np.median(drops))
            if drop > best_drop:
                best, best_drop = math.degrees(theta), drop
    return best, _num(best_drop, 2)


def shadow_lengths(lum, objects, mask_all, direction_deg, px_m):
    """Median shadow length (m) per object along the shadow direction from its down-sun edge samples.

    Shadows in this imagery are soft, so "dark" is relative to the object's own up-sun surroundings: the median
    luminance 1.2 m outside its up-sun edges minus PARAMS['shadow_drop_l'] (LAB L units).
    """
    dx, dy = math.cos(math.radians(direction_deg)), math.sin(math.radians(direction_deg))
    out = {}
    max_px = int(PARAMS['shadow_max_m']/px_m)
    off = 1.2/px_m
    for oid, ring in objects.items():
        up, lengths = [], []
        facing = [nx*dx+ny*dy for nx, ny in ring['normals']]   # outward normal against the shadow direction
        for (x, y), f in zip(ring['edge'][::2], facing[::2], strict=True):
            if f < -.5:                       # up-sun faces: lit ground reference
                px, py = int(round(x-dx*off)), int(round(y-dy*off))
                if 0 <= px < lum.shape[1] and 0 <= py < lum.shape[0] and not mask_all[py, px]:
                    up.append(lum[py, px])
        if len(up) < 4:
            out[oid] = None
            continue
        dark = float(np.median(up))-PARAMS['shadow_drop_l']
        for (x, y), f in zip(ring['edge'][::2], facing[::2], strict=True):
            if f <= .5:                       # only faces turned away from the sun cast the visible shadow
                continue
            lengths.append(_ray_dark_length(lum, mask_all, ring['mask'], x, y, dx, dy, dark, max_px)*px_m)
        out[oid] = _num(float(np.median(lengths)), 3) if len(lengths) >= 4 else None
    return out


def relative_heights(lengths, areas):
    primaries = [v for oid, v in lengths.items() if v and areas.get(oid, 0) >= PARAMS['primary_area_m2'] and v >= PARAMS['shadow_min_m']]
    reference = float(np.median(primaries)) if len(primaries) >= 3 else None
    heights = {}
    for oid, v in lengths.items():
        if v is None or reference is None or reference <= 0:
            heights[oid] = {'shadow_m': v, 'height_estimate_m': None, 'reference_shadow_m': _num(reference, 3),
                            'note': 'no primary-roof reference shadow in this tile' if reference is None else 'no shadow measured'}
        else:
            heights[oid] = {'shadow_m': v, 'height_estimate_m': _num(v/reference*PARAMS['eave_prior_m'], 2),
                            'reference_shadow_m': _num(reference, 3), 'ratio_to_primary': _num(v/reference, 3),
                            'note': f"relative shadow height; primary roofs assumed {PARAMS['eave_prior_m']} m at the eave"}
    return heights


# --- assessment --------------------------------------------------------------------------------------------------

def _object_rasters(scene, shapes, frame):
    objects, mask_all = {}, np.zeros(frame, bool)
    for oid, shape in shapes.items():
        full = sk._raster(sk.to_pixels(shape, scene), (0, 0), frame)
        mask_all |= full
        inner = shape.buffer(-.5)
        inside = sk._raster(sk.to_pixels(inner if not inner.is_empty else shape, scene), (0, 0), frame)
        ring_geom = sk.to_pixels(shape, scene)
        ring_px = np.asarray(ring_geom.exterior.coords)
        ccw = ring_geom.exterior.is_ccw
        edge, normals = [], []
        for (ax, ay), (bx, by) in zip(ring_px[:-1], ring_px[1:], strict=True):
            n = max(int(math.hypot(bx-ax, by-ay)), 1)
            length = math.hypot(bx-ax, by-ay) or 1.
            nx, ny = ((by-ay)/length, -(bx-ax)/length) if ccw else (-(by-ay)/length, (bx-ax)/length)
            edge += [(ax+(bx-ax)*t/n, ay+(by-ay)*t/n) for t in range(n)]
            normals += [(nx, ny)]*n
        c = ring_geom.centroid
        objects[oid] = {'mask': full, 'inside': inside, 'edge': edge, 'normals': normals, 'centroid': (c.x, c.y), 'area': shape.area}
    return objects, mask_all


def assess_scene(scene, image, keys, *, hires=None, ocr=None, vehicles=None, heights=True, save_crops=None):
    shapes = sk.building_shapes(scene)
    frame = (scene['image_height'], scene['image_width'])
    px_m = sk.pixel_size_m(scene)
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    objects, mask_all = _object_rasters(scene, shapes, frame)
    lum = cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_RGB2LAB)[:, :, 0].astype(np.float32) if image is not None else None
    shadow = {'direction_deg': None, 'drop': None}
    height_rows = {}
    if heights and lum is not None and objects:
        direction, drop = shadow_direction(lum, list(objects.values()), mask_all, px_m)
        shadow = {'direction_deg': _num(direction, 1), 'drop': drop}
        if direction is not None:
            lengths = shadow_lengths(lum, objects, mask_all, direction, px_m)
            height_rows = relative_heights(lengths, {oid: o['area'] for oid, o in objects.items()})
    vehicle_rows = {}
    if vehicles is not None and image is not None:
        selected = {oid: objects[oid]['mask'] for oid in keys if oid in objects}
        vehicle_rows = vehicles.score(image, selected, {oid: objects[oid]['centroid'] for oid in selected}, px_m)
    rows = []
    for oid in keys:
        if oid not in shapes:
            continue
        shape, obj = shapes[oid], next(o for o in scene['objects'] if o['id'] == oid)
        geometry = sk.geometry_features(shape, vertex_count=len(obj.get('ring') or []))
        axis_px = sk._pixel_axis_deg(geometry['long_axis_deg'], scene) if geometry.get('long_axis_deg') is not None else 0.
        payload = {'version': VERSION, 'note': sk.NOTE, 'kind': 'building', 'area_m2': geometry['area_m2'],
                   'stages': {'localisation': 'scene outline', 'deskew_angle_deg': _num(axis_px, 2)},
                   'hires': None, 'ocr': None, 'vehicles': vehicle_rows.get(oid), 'height': height_rows.get(oid),
                   'shadow_direction_deg': shadow['direction_deg'], 'computed_at': stamp, 'code_sha256': code}
        ring = np.asarray(obj['ring'], dtype=float)
        pad_px = PARAMS['pad_m']/px_m
        source, source_mpp, source_ring, source_pad = image, px_m, ring, pad_px
        if hires is not None:
            x0, y0 = max(int(ring[:, 0].min()-pad_px), 0), max(int(ring[:, 1].min()-pad_px), 0)
            x1, y1 = min(int(ring[:, 0].max()+pad_px)+1, frame[1]), min(int(ring[:, 1].max()+pad_px)+1, frame[0])
            hi, hi_mpp = hires.crop(scene, x0, y0, x1, y1)
            if hi is not None:
                scale = px_m/hi_mpp
                source, source_mpp = hi, hi_mpp
                source_ring, source_pad = (ring-[x0, y0])*scale, 0.
                payload['hires'] = {'zoom': hires.zoom, 'meters_per_pixel': _num(hi_mpp, 4), 'scale_vs_scene': _num(scale, 3),
                                    'shape': list(hi.shape[:2])}
            else:
                payload['hires'] = {'zoom': hires.zoom, 'error': 'no child tiles'}
        if ocr is not None and source is not None:
            started = time.monotonic()
            desk = deskew(source, source_ring, axis_px, source_pad, upscale=PARAMS['upscale'])
            if desk is not None:
                prepared = enhance(desk['image'])
                result = ocr.read(prepared)
                result.update({'crop_shape': list(prepared.shape[:2]), 'effective_m_per_px': _num(source_mpp/PARAMS['upscale'], 4),
                               'preprocess': 'deskew + CLAHE(3.0, 12x12) + 2x Lanczos + unsharp', 'seconds': round(time.monotonic()-started, 3)})
                payload['ocr'] = result
                if save_crops is not None:
                    save_crops.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(prepared).save(save_crops/f"{scene['id']}--{oid}.png")
            else:
                payload['ocr'] = {'available': ocr.available, 'detections': [], 'error': 'crop too small'}
        rows.append(_row(scene, obj, sk.plain(payload), VERSION))
    return rows


def run(root=Path('.'), scene_ids=(), objects=None, *, hires=False, ocr=False, vehicles=False, heights=True,
        write=True, save_crops=None, log=None):
    root = Path(root)
    targets = {}
    for sid in scene_ids:
        targets.setdefault(sid, None)
    for sid, oid in objects or ():
        targets.setdefault(sid, set())
        if targets[sid] is not None:
            targets[sid].add(oid)
    hires_src = HiResSource() if hires else None
    reader = SceneText() if ocr else None
    scorer = VehicleScorer() if vehicles else None
    rows, whole = [], []
    with closing(eyes_index.connection(root, readonly=True)) as db:
        hinted = {sid: {r['object_id'] for r in eyes_index.rows(db, TABLE, scene_id=sid, version=sk.VERSION)} for sid in targets}
    for i, (sid, wanted) in enumerate(targets.items()):
        scene = sk.load_scene(root, sid)
        image = sk.load_image(root, scene)
        keys = sorted(hinted[sid] if wanted is None else wanted)
        if wanted is None:
            whole.append(sid)
        started = time.monotonic()
        scene_rows = assess_scene(scene, image, keys, hires=hires_src, ocr=reader, vehicles=scorer, heights=heights, save_crops=save_crops)
        rows.extend(scene_rows)
        if log:
            log(f'[{i+1}/{len(targets)}] {sid}: {len(scene_rows)} rows, {time.monotonic()-started:.1f}s')
    written = 0
    if write and rows:
        with closing(eyes_index.connection(root)) as db:
            eyes_index.ensure_schema(db)
            written += sk.replace_scene_rows(db, TABLE, VERSION, whole, [r for r in rows if r['scene_id'] in whole])
            partial = [r for r in rows if r['scene_id'] not in whole]
            if partial:
                written += eyes_index.replace_rows(db, TABLE, partial)
    timings = {'hires_child_fetch_s': (hires_src.timings if hires_src else []), 'ocr_s': (reader.timings if reader else []),
               'sam3_vehicle_s': (scorer.timings if scorer else [])}
    return rows, written, timings


def summarise(rows, timings):
    ocr_rows = [r['payload']['ocr'] for r in rows if r['payload'].get('ocr')]
    veh = [r['payload']['vehicles'] for r in rows if r['payload'].get('vehicles')]
    heights = [r['payload']['height'] for r in rows if r['payload'].get('height')]
    def med(xs):
        return _num(float(np.median(xs)), 3) if xs else None
    return {'version': VERSION, 'rows': len(rows),
            'ocr': {'rows': len(ocr_rows), 'available': sum(1 for o in ocr_rows if o.get('available')),
                    'text_found': sum(1 for o in ocr_rows if o.get('text_found')),
                    'detections': sum(len(o.get('detections') or []) for o in ocr_rows), 'median_s': med(timings['ocr_s'])},
            'hires': {'rows': sum(1 for r in rows if (r['payload'].get('hires') or {}).get('meters_per_pixel')),
                      'child_fetches': len(timings['hires_child_fetch_s']), 'median_fetch_s': med(timings['hires_child_fetch_s'])},
            'vehicles': {'rows': len(veh), 'vehicle_like': sum(1 for v in veh if v.get('vehicle_like')),
                         'with_cars_within_5m': sum(1 for v in veh if v.get('cars_within_5m')), 'median_s_per_tile': med(timings['sam3_vehicle_s'])},
            'heights': {'rows': len(heights), 'estimated': sum(1 for h in heights if h.get('height_estimate_m') is not None),
                        'median_estimate_m': med([h['height_estimate_m'] for h in heights if h.get('height_estimate_m') is not None])}}


def _parse_objects(values):
    pairs = []
    for value in values or ():
        sid, _, oid = value.partition(':')
        if not oid:
            raise ValueError(f'--objects expects SCENE:OBJECT, got {value!r}')
        pairs.append((sid, oid))
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description='Optical stages for structure candidates: hi-res crops, deskew, OCR, SAM3 vehicles, '
                                                 'relative shadow heights. Estimates, not labels.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='*', default=[])
    parser.add_argument('--objects', nargs='*', default=[])
    parser.add_argument('--hires', action='store_true', help='Re-fetch crops at the provider maximum zoom (network).')
    parser.add_argument('--ocr', action='store_true', help='Scene-text recognition on deskewed crops.')
    parser.add_argument('--vehicles', action='store_true', help='SAM3 vehicle prompts (one encode per tile).')
    parser.add_argument('--no-heights', action='store_true')
    parser.add_argument('--save-crops', help='Directory for the deskewed, enhanced OCR crops.')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    if not args.scenes and not args.objects:
        parser.error('give --scenes and/or --objects')
    log = None if args.quiet else (lambda t: print(t, file=sys.stderr, flush=True))
    rows, written, timings = run(Path(args.root), args.scenes, _parse_objects(args.objects), hires=args.hires, ocr=args.ocr,
                                 vehicles=args.vehicles, heights=not args.no_heights, write=not args.dry_run,
                                 save_crops=Path(args.save_crops) if args.save_crops else None, log=log)
    out = {**summarise(rows, timings), 'written': written}
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({**out, 'rows': rows}, indent=1))
    json.dump(out, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
