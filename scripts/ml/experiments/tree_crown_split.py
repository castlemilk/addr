"""Tree-group split SIDECAR probe (2026-10-03). Read-only over data/scenes; nothing is written to a scene.

Reviewer observation (verbatim, 2026-10-03 ~22:10 AEST): win-2fbb386c3b235ff4 object b1e978e7c391f56b "is a good example of a
vegation group could be split based off the tree pattern differences"; lot-29358 object 7c29c8c28d31c70f "also".

Question measured here: given ONE tree/vegetation polygon from the SAM3 'tree canopy' scan (one proposal mask component,
scene_extract.extract_objects), can a CPU step propose crowns inside it, and how many?
  shape    distance transform of the rasterised polygon (scipy.ndimage.distance_transform_edt); crowns = local maxima of the
           distance with a minimum separation `peak_sep_m` and a minimum radius `peak_min_m`; regions by watershed
           (scipy.ndimage.watershed_ift on the inverted distance, markers at the peaks). Also the erosion ladder: the number
           of connected components after a negative buffer of r metres, r in `erode_m`.
  texture  per pixel inside the polygon: CIE Lab (cv2) + local brightness std over a `std_win` window; k-means (scipy.cluster.vq
           .kmeans2, k in `k_values`, seeded) on the standardised features; label map cleaned by a majority filter; components
           per label >= `texture_min_m2`. Reported as cluster areas and component counts, and as a second watershed whose
           markers are the texture components (the "pattern" split the reviewer described).
Every figure is a count, an area or a geometry; none is accuracy. The optional proposed-split.json per object is in the shape
the editor's split API takes (children: key, label, outline_pixels {ring, holes}, role) and is NOT applied by this script.

Run from the repo root:
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.tree_crown_split \
        --target win-2fbb386c3b235ff4:b1e978e7c391f56b --target lot-29358:7c29c8c28d31c70f \
        --out data/reviews/eyes/experiments/tree-group-split-2026-10-03/probe

SIDECAR v1 (2026-10-07, card item 2(a) / 3(a), "yes to all defaults"): shape-only crown hypotheses for the windows, renders and
a MARKS list for the reviewer's right / wrong per cut. Read-only over data/scenes; writes only --out. No model inference.
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.tree_crown_split --sidecar-v1 \
        --out data/reviews/eyes/experiments/tree-group-split-2026-10-03/sidecar-v1 \
        --progress .run/session-2026-10-07-trees/progress.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.cluster.vq import kmeans2
from shapely.geometry import LineString, Polygon
from shapely.ops import linemerge, unary_union
from shapely.validation import make_valid

from ..scene_extract import APP_RING_RULES, finalise_app_ring
from ..scene_instances import partition_outlines
from ..segmentation import iter_polygons
from . import tree_group_split as tgs

DESIGN = {'peak_sep_m': 2.0, 'peak_min_m': 0.8, 'erode_m': [0.5, 1.0, 1.5, 2.0, 2.5, 3.0], 'std_win_px': 9,
          'k_values': [2, 3], 'texture_min_m2': 4.0, 'majority_px': 7, 'simplify_px': 1.0, 'seed': 7,
          'adaptive_sep': False, 'adaptive_sep_min_m': 0.8, 'adaptive_sep_ratio': 0.6}
ADAPTIVE = {**DESIGN, 'adaptive_sep': True}


def _scene(root, scene_id):
    folder = root/'data/scenes'/scene_id
    model = json.load(open(folder/'latest.json'))
    review = json.load(open(folder/'review.json')) if (folder/'review.json').exists() else {'objects': []}
    image = Image.open(folder/f"{model['image_sha256']}.png").convert('RGB')
    return model, review, image


def _object(model, review, object_id):
    live = next((o for o in review.get('objects', []) if o['id'] == object_id), None)
    base = next((o for o in model['objects'] if o['id'] == object_id), None)
    return live or base, base


def _mask(ring, holes, size):
    mask = np.zeros((size[1], size[0]), np.uint8)
    cv2.fillPoly(mask, [np.asarray(ring, np.int32).reshape(-1, 1, 2)], 1)
    for hole in holes or []:
        cv2.fillPoly(mask, [np.asarray(hole, np.int32).reshape(-1, 1, 2)], 0)
    return mask.astype(bool)


def _components(labels, label_value, min_px):
    lab, n = ndimage.label(labels == label_value)
    sizes = ndimage.sum(np.ones_like(lab), lab, range(1, n+1)) if n else []
    return int(sum(1 for s in sizes if s >= min_px)), [int(s) for s in sizes if s >= min_px]


def _regions_to_rings(labels, mask, simplify_px, min_px):
    """Label raster -> list of (label, polygon) per label (largest part each, union of all parts kept as geometry)."""
    out = []
    for value in sorted(set(np.unique(labels[mask])) - {0}):
        part = (labels == value) & mask
        if part.sum() < min_px:
            continue
        contours, _ = cv2.findContours(part.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        polys = [Polygon(c.reshape(-1, 2)) for c in contours if len(c) >= 3]
        polys = [p.buffer(0) for p in polys if p.is_valid or True]
        geom = unary_union([p for p in polys if not p.is_empty])
        if geom.is_empty:
            continue
        geom = geom.simplify(simplify_px, preserve_topology=True)
        best = max(iter_polygons(geom), key=lambda p: p.area, default=None)
        if best is not None and best.area >= min_px:
            out.append((int(value), best))
    return out


def _watershed(dist, markers, mask):
    """Marker-confined watershed on the inverted distance by priority flooding (heapq, 8-neighbour): the highest-distance
    pixels flood first, every mask pixel ends in the basin of one marker, labels 0 outside / 1.. inside. skimage.segmentation
    .watershed is not in the venv; scipy.ndimage.watershed_ift (uint16 cost) left a square plateau artefact and cv2.watershed
    floods on the surface GRADIENT, both wrong for a smooth distance ramp."""
    import heapq
    labels = np.zeros(mask.shape, np.int32)
    if markers.max() == 0:
        return labels
    h, w = mask.shape
    heap = []
    ys, xs = np.nonzero(markers)
    for y, x in zip(ys, xs, strict=True):
        labels[y, x] = markers[y, x]
        heapq.heappush(heap, (-float(dist[y, x]), int(y), int(x)))
    while heap:
        prio, y, x = heapq.heappop(heap)
        label = labels[y, x]
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx = y+dy, x+dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and labels[ny, nx] == 0:
                    labels[ny, nx] = label
                    heapq.heappush(heap, (max(prio, -float(dist[ny, nx])), ny, nx))
    return labels


def shape_probe(mask, m_per_px, design):
    dist = ndimage.distance_transform_edt(mask)*m_per_px
    # peak separation: the design metre value, or (adaptive) a fraction of the largest inscribed radius so a 6 m object
    # with two 3 m crowns is probed at the crown scale rather than at the 2 m separation that suits a 150 m2 group
    sep_m = design['peak_sep_m'] if not design.get('adaptive_sep') else max(design['adaptive_sep_min_m'], design['adaptive_sep_ratio']*float(dist.max()))
    sep_px = max(1, int(round(sep_m/m_per_px)))
    peaks = (dist == ndimage.maximum_filter(dist, size=2*sep_px+1)) & (dist >= design['peak_min_m'])
    # one marker per plateau
    marker_lab, n_peaks = ndimage.label(peaks)
    markers = np.zeros(mask.shape, np.int32)
    for i, centre in enumerate(ndimage.center_of_mass(peaks, marker_lab, range(1, n_peaks+1)), start=1):
        y, x = int(round(centre[0])), int(round(centre[1]))
        markers[y, x] = i
    ws = _watershed(dist, markers, mask)
    erosion = []
    for r in design['erode_m']:
        eroded = dist > r
        lab, n = ndimage.label(eroded)
        sizes = sorted((int(s) for s in ndimage.sum(np.ones_like(lab), lab, range(1, n+1))), reverse=True) if n else []
        erosion.append({'r_m': r, 'components': int(n), 'component_m2': [round(s*m_per_px*m_per_px, 1) for s in sizes[:8]]})
    areas = {int(v): round(float((ws == v).sum()*m_per_px*m_per_px), 1) for v in np.unique(ws) if v}
    return {'peaks': int(n_peaks), 'peak_sep_m': round(float(sep_m), 2), 'max_inscribed_radius_m': round(float(dist.max()), 2), 'watershed_regions': len(areas),
            'region_m2': areas, 'erosion': erosion}, dist, ws, peaks


def texture_probe(image, mask, m_per_px, design, shape_dist):
    rgb = np.asarray(image)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    w = design['std_win_px']
    mean = cv2.blur(grey, (w, w))
    sq = cv2.blur(grey*grey, (w, w))
    local_std = np.sqrt(np.maximum(sq-mean*mean, 0))
    feats = np.dstack([lab, local_std])[mask]                         # L, a, b, std
    mu, sd = feats.mean(0), feats.std(0)+1e-6
    z = (feats-mu)/sd
    rng = np.random.default_rng(design['seed'])
    min_px = design['texture_min_m2']/(m_per_px*m_per_px)
    results, best = {}, None
    for k in design['k_values']:
        centroids, labels = kmeans2(z, k, minit='++', seed=rng)
        label_map = np.zeros(mask.shape, np.int32)
        label_map[mask] = labels+1
        # majority filter to clean salt-and-pepper
        cleaned = label_map.copy()
        for value in range(1, k+1):
            vote = cv2.blur((label_map == value).astype(np.float32), (design['majority_px'], design['majority_px']))
            cleaned = np.where(mask & (vote > 0.5), value, cleaned)
        per_label = {}
        for value in range(1, k+1):
            n, sizes = _components(cleaned*mask, value, min_px)
            per_label[value] = {'area_m2': round(float((cleaned[mask] == value).sum()*m_per_px*m_per_px), 1), 'components_ge_min': n,
                                'component_m2': [round(s*m_per_px*m_per_px, 1) for s in sorted(sizes, reverse=True)[:8]],
                                'centroid_Lab_std': [round(float(v), 1) for v in centroids[value-1]*sd+mu]}
        results[k] = {'labels': per_label, 'mean_local_std': round(float(local_std[mask].mean()), 2)}
        if best is None:
            best = cleaned
    # texture-seeded watershed: markers = components of the k=2 cleaned map that reach the floor
    # markers: connected components PER LABEL VALUE (a whole-mask label would read the canopy as one component)
    markers = np.zeros(mask.shape, np.int32)
    next_id = 1
    for value in range(1, design['k_values'][0]+1):
        comp_lab, n = ndimage.label((best == value) & mask)
        sizes = ndimage.sum(np.ones_like(comp_lab), comp_lab, range(1, n+1)) if n else []
        for i, s in enumerate(sizes, start=1):
            if s >= min_px:
                markers[comp_lab == i] = next_id
                next_id += 1
    ws = _watershed(shape_dist, markers, mask)
    areas = {int(v): round(float((ws == v).sum()*m_per_px*m_per_px), 1) for v in np.unique(ws) if v}
    return {'kmeans': results, 'texture_seeded_watershed_regions': len(areas), 'region_m2': areas}, best, ws, local_std


def render(image, mask, ring, dist, peaks, ws_shape, kmeans_map, ws_texture, out_png, pad=24):
    ys, xs = np.where(mask)
    y0, y1 = max(ys.min()-pad, 0), min(ys.max()+pad, mask.shape[0])
    x0, x1 = max(xs.min()-pad, 0), min(xs.max()+pad, mask.shape[1])
    crop = np.asarray(image)[y0:y1, x0:x1].copy()
    pts = (np.asarray(ring)-[x0, y0]).astype(np.int32).reshape(-1, 1, 2)

    def panel(base, labels=None, title=''):
        p = base.copy()
        if labels is not None:
            colours = np.random.default_rng(3).integers(60, 255, (labels.max()+2, 3)).astype(np.uint8)
            lay = colours[labels[y0:y1, x0:x1]]
            lay[labels[y0:y1, x0:x1] == 0] = 0
            p = cv2.addWeighted(p, .55, lay, .45, 0)
        cv2.polylines(p, [pts], True, (255, 255, 0), 1)
        cv2.putText(p, title, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, .42, (255, 255, 255), 1, cv2.LINE_AA)
        return p
    d = (dist[y0:y1, x0:x1]/max(dist.max(), 1e-6)*255).astype(np.uint8)
    d = cv2.cvtColor(d, cv2.COLOR_GRAY2RGB)
    py, px = np.where(peaks[y0:y1, x0:x1])
    for y, x in zip(py, px, strict=True):
        cv2.circle(d, (int(x), int(y)), 3, (255, 0, 0), -1)
    panels = [panel(crop, None, 'image + outline'), panel(d, None, 'distance + peaks'),
              panel(crop, ws_shape, 'shape watershed'), panel(crop, kmeans_map, 'k-means k=2 (Lab+std)'),
              panel(crop, ws_texture, 'texture-seeded watershed')]
    h = max(p.shape[0] for p in panels)
    panels = [cv2.copyMakeBorder(p, 0, h-p.shape[0], 0, 4, cv2.BORDER_CONSTANT, value=(30, 30, 30)) for p in panels]
    sheet = np.hstack(panels)
    scale = max(1, int(1400/sheet.shape[1]))
    if scale > 1:
        sheet = cv2.resize(sheet, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    Image.fromarray(sheet).save(out_png)


def proposed_split(object_id, label_prefix, regions, holes_ok=True):
    children = []
    for i, (_value, poly) in enumerate(regions):
        ring = [[round(float(x), 2), round(float(y), 2)] for x, y in poly.exterior.coords[:-1]]
        holes = [[[round(float(x), 2), round(float(y), 2)] for x, y in h.coords[:-1]] for h in poly.interiors] if holes_ok else []
        children.append({'key': f'{label_prefix}-{i+1}', 'label': f'Crown hypothesis {i+1} ({label_prefix}; not reviewed)',
                         'outline_pixels': {'ring': ring, 'holes': holes}})
    return {'object_id': object_id, 'method': label_prefix, 'children': children, 'applied': False,
            'note': 'Sidecar hypothesis only: children are a partition proposal for the split API preview, never applied here.'}


def probe(root, scene_id, object_id, out, design):
    model, review, image = _scene(root, scene_id)
    obj, base = _object(model, review, object_id)
    if obj is None:
        return {'scene': scene_id, 'object': object_id, 'error': 'missing'}
    m_per_px = abs(float(model['pixel_to_meters'][0][0]))
    mask = _mask(obj['ring'], obj.get('holes'), image.size)
    poly = Polygon(obj['ring'], obj.get('holes'))
    shape, dist, ws_shape, peaks = shape_probe(mask, m_per_px, design)
    texture, kmap, ws_tex, _ = texture_probe(image, mask, m_per_px, design, dist)
    out.mkdir(parents=True, exist_ok=True)
    render(image, mask, obj['ring'], dist, peaks, ws_shape, kmap, ws_tex, out/f'{scene_id}-{object_id}.png')
    min_px = design['texture_min_m2']/(m_per_px*m_per_px)
    splits = {'shape': proposed_split(object_id, 'shape', _regions_to_rings(ws_shape, mask, design['simplify_px'], min_px)),
              'texture': proposed_split(object_id, 'texture', _regions_to_rings(ws_tex, mask, design['simplify_px'], min_px))}
    json.dump(splits, open(out/f'{scene_id}-{object_id}.proposed-split.json', 'w'), indent=1)
    edge = min(poly.bounds[0], poly.bounds[1], image.width-poly.bounds[2], image.height-poly.bounds[3])
    return {'scene': scene_id, 'object': object_id, 'kind': obj['kind'], 'status': obj.get('status'), 'score': obj.get('score'),
            'prompt': obj.get('prompt'), 'source': obj.get('source'), 'replaces': obj.get('replaces'), 'vertices': len(obj['ring']),
            'holes': len(obj.get('holes') or []), 'area_m2': obj['model'].get('area_m2'), 'm_per_px': round(m_per_px, 4),
            'frame_edge_px': round(float(edge), 1), 'mask_px': int(mask.sum()),
            'sam3_input_m_per_px': round(m_per_px*image.width/1008, 4), 'shape': shape, 'texture': texture,
            'proposed_children': {k: len(v['children']) for k, v in splits.items()},
            'render': str(out/f'{scene_id}-{object_id}.png')}


# ============================================================================================ SIDECAR v1 (2026-10-07)
# Card tree-group-split-2026-10-03, answered 2026-10-07 20:02 AEDT "yes to all defaults". Item 1(a): no detector or
# post-processing proposal; the reviewer's split tool (preview_object_split / apply_object_split) stays the only path to a
# split, and nothing here previews or applies one. Item 2(a): the parameters in SIDECAR_V1. Item 3(a): windows only, every
# window tree >= 50 m2 with >= 2 peaks plus 10 confirmed single-crown controls (the null reading), renders raw | outline |
# parts at native pixels, a MARKS list for right / wrong per cut. The geometry core is the 3 October prototype
# (tree_group_split.shape_variant / enforce_floor / vector_partition, unchanged); v1 adds the hole fill, the guards
# (confirmed, frame-cut), make_valid, the app's ring rules (finalise_app_ring with APP_RING_RULES, the capped rules the
# app's save applies), the split API's own partition check (scene_instances.partition_outlines) and per-child saddle_m /
# depth_ratio / uncertainty. DESIGN / ADAPTIVE and the probe above are untouched: the 3 October outputs stay reproducible.
SIDECAR_V1 = {
    'version': 'tree-crown-sidecar-v1',
    'min_crown_radius_m': 1.5,    # a crown peak is a local maximum of the inscribed radius over a disc of this radius, >= it
    'saddle_merge_ratio': 0.7,    # two basins merge when saddle > ratio x the smaller peak (the neck is too shallow)
    'min_part_m2': 8.0,           # a part under this merges into the neighbour with the longest shared edge; never dropped
    'fill_holes': True,           # holes filled before the distance transform (8bd1fd1c / a3064ab2 necks ran through holes)
    'frame_tol_px': 1.0,          # outline within this many px of the image frame = frame-cut (extract_objects clips to it)
    'simplify_px': 1.5,           # Douglas-Peucker on the dividing lines only; the source outline is never moved
    'pad_px': 12,
    'max_children': 8,            # partition_outlines accepts two to eight children
    'population': {'prefixes': ['win-', 'lot-'], 'layer': 'review', 'kinds': ['tree'], 'min_area_m2': 50.0, 'min_peaks': 2},
    'controls': {'n': 10, 'status': 'confirmed', 'order': 'area desc, then scene / id', 'frame_cut': 'excluded'},
    'named': [['win-2fbb386c3b235ff4', 'b1e978e7c391f56b'], ['lot-29358', '7c29c8c28d31c70f']],
    'render': {'target_side_px': 420, 'max_scale': 4, 'fill_alpha': 0.22, 'min_sheet_px': 960},
}
LIVE_EXCLUDED = ('rejected', 'superseded')
CUT_COLOUR = (255, 0, 255)
FRAME_COLOUR = (255, 48, 48)
_SHIFT = 4


def is_frame_cut(poly, width, height, tol):
    """True when the outline comes within `tol` px of the image frame: extract_objects intersects every mask polygon with
    box(0, 0, width, height), so a canopy the frame cuts has outline points on x = 0 / y = 0 / x = width / y = height."""
    minx, miny, maxx, maxy = poly.bounds
    return min(minx, miny, width-maxx, height-maxy) <= tol


def _v1_crop(poly, pad):
    minx, miny, maxx, maxy = poly.bounds
    ox, oy = int(math.floor(minx))-pad, int(math.floor(miny))-pad
    ex, ey = int(math.ceil(maxx))+pad, int(math.ceil(maxy))+pad
    return ox, oy, (ey-oy+1, ex-ox+1)


def _filled_mask(poly, design):
    """Crop mask of the outline with its holes filled (design['fill_holes']), in the prototype's pixel convention."""
    ox, oy, shape = _v1_crop(poly, design['pad_px'])
    filled = Polygon(poly.exterior) if design['fill_holes'] else poly
    return tgs.rasterise(filled, ox, oy, shape), ox, oy, shape


def count_peaks(ring, holes, m_per_px, design=SIDECAR_V1):
    """The population pre-pass: crown peaks under the v1 rule (tree_group_split.shape_variant's peak test on the hole-filled
    distance), without the watershed. Returns (peaks, largest inscribed radius in m)."""
    poly = Polygon(ring, holes or [])
    mask, *_ = _filled_mask(poly, design)
    dist = ndimage.distance_transform_edt(mask)*m_per_px
    r_m = design['min_crown_radius_m']
    r_px = max(1, int(round(r_m/m_per_px)))
    yy, xx = np.mgrid[-r_px:r_px+1, -r_px:r_px+1]
    peaks = (dist == ndimage.maximum_filter(dist, footprint=(xx*xx+yy*yy) <= r_px*r_px)) & (dist >= r_m)
    return int(ndimage.label(peaks)[1]), round(float(dist.max()), 2)


def _no_cut_reason(info, floor_log, design):
    n = info.get('peaks_detected', 0)
    if n == 0:
        return info.get('no_split_reason') or 'no crown peak'
    if n == 1:
        return f"one crown peak (largest inscribed radius {info['max_inscribed_radius_m']} m)"
    if info.get('merges') and info.get('peaks_after_merge', n) <= 1:
        m = ', '.join(f"saddle {x['saddle_m']} m = {x['ratio_to_smaller_peak']} x the smaller peak" for x in info['merges'])
        return f"{n} peaks merged by the saddle rule (> {design['saddle_merge_ratio']}): {m}"
    if floor_log:
        return f"parts under {design['min_part_m2']} m2 merged into the longest-edge neighbour: " + \
            ', '.join(f"{x['area_m2']} m2" for x in floor_log)
    return 'one part after the guards'


def _strength(depth):
    if depth is None:
        return 'no raster saddle'
    return 'deep neck' if depth >= .6 else 'clear neck' if depth >= .45 else 'shallow neck, near the 0.7 merge ratio'


def _uncertainty(child, my_cuts, record):
    bits = [f"shape-only crown hypothesis, largest inscribed radius {child['peak_radius_m']:.1f} m"]
    for c in my_cuts:
        if c['depth_ratio'] is None:
            bits.append(f"{c['cut']}: vector contact {c['length_m']} m with no raster saddle")
            continue
        bits.append(f"{c['cut']}: saddle {c['saddle_m']:.2f} m (neck ~{c['neck_width_m']:.1f} m wide), depth ratio "
                    f"{c['depth_ratio']:.2f}, {_strength(c['depth_ratio'])}")
    if record['holes_filled']:
        bits.append(f"{record['holes_filled']} outline hole(s) filled for the distance transform")
    if record['floor_merges']:
        bits.append(f"{len(record['floor_merges'])} part(s) under {record['min_part_m2']} m2 merged into neighbours in this object")
    if child['role'] != 'crown':
        bits.append('no crown peak inside this part (unresolved residual)')
    bits.append('no texture, height, species or detector evidence; a candidate until the reviewer confirms it')
    return '; '.join(bits)


def _lines_of(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'LineString':
        return [geometry]
    if geometry.geom_type == 'MultiLineString':
        merged = linemerge(geometry)
        return [merged] if merged.geom_type == 'LineString' else list(merged.geoms)
    return [g for part in getattr(geometry, 'geoms', []) for g in _lines_of(part)]


def crown_partition(ring, holes, m_per_px, width, height, design=SIDECAR_V1, *, status=None, control=False):
    """Card item 2(a) on ONE outline (pixel ring + holes as the scene stores them). Pure: no file is read or written.
    Guards first: a confirmed object is never split (control=True runs it as a null reading, never as a proposal); an invalid
    outline or a frame-cut one is skipped. Then: holes filled, distance transform, crown peaks (>= min radius, disc of the min
    radius), priority-flood basins, saddle merge, floor merge, the outline polygonized with the simplified dividing lines,
    make_valid, the app's ring rules per child, the split API's partition check. A refusal at any check proposes no cut."""
    holes = [h for h in (holes or [])]
    record = {'version': design['version'], 'status': status, 'control': bool(control), 'guard': None, 'refusal': None,
              'peaks_detected': None, 'n_parts': 1, 'cut': False, 'children': [], 'cuts': [], 'min_part_m2': design['min_part_m2']}
    if status == 'confirmed' and not control:
        return {**record, 'guard': 'confirmed', 'reason': 'confirmed objects are never split (card item 2(a))'}
    poly = Polygon(ring, holes)
    if poly.is_empty or not poly.is_valid:
        return {**record, 'guard': 'invalid_outline', 'reason': 'the stored outline is not one valid polygon'}
    if is_frame_cut(poly, width, height, design['frame_tol_px']):
        return {**record, 'guard': 'frame_cut', 'reason': f"outline within {design['frame_tol_px']} px of the image frame"}
    mask, ox, oy, shape = _filled_mask(poly, design)
    labels, dist, peaks, info = tgs.shape_variant(mask, m_per_px, design)
    after_saddle = int(labels.max())
    labels, floor_log = tgs.enforce_floor(labels, mask, m_per_px, design['min_part_m2'])
    labels = tgs.relabel(labels)
    plateau, n = ndimage.label(peaks)
    centres = ndimage.center_of_mass(peaks, plateau, range(1, n+1)) if n else []
    record.update({'peaks_detected': info['peaks_detected'], 'peak_radii_m': info['peak_radii_m'],
                   'peaks_px': [[round(ox+float(x), 1), round(oy+float(y), 1)] for y, x in centres],
                   'max_inscribed_radius_m': info['max_inscribed_radius_m'], 'saddle_merges': info['merges'],
                   'parts_after_saddle_merge': after_saddle, 'floor_merges': floor_log, 'holes_filled': len(holes),
                   'crop_origin_px': [ox, oy], 'crop_shape_px': list(shape)})
    if labels.max() <= 1:
        return {**record, 'no_cut_reason': _no_cut_reason(info, floor_log, design)}
    parts, vlog = tgs.vector_partition(poly, tgs.tile_outside(labels, mask), ox, oy, m_per_px, design)
    record['vector_log'] = vlog
    fixed = {}
    for v, p in sorted(parts.items()):
        q = p if p.is_valid else make_valid(p)
        if q.is_empty or q.geom_type != 'Polygon':
            return {**record, 'refusal': 'invalid_part', 'reason': f'part {v} is {q.geom_type} after make_valid'}
        fixed[v] = q
    if len(fixed) <= 1:
        return {**record, 'no_cut_reason': 'the vector floor merged every part into one'}
    if len(fixed) > design['max_children']:
        return {**record, 'refusal': 'over_max_children', 'n_parts_raw': len(fixed),
                'reason': f"{len(fixed)} parts; the split API takes two to {design['max_children']}"}
    finals = []
    for v, p in fixed.items():
        ring_c = [[float(x), float(y)] for x, y in p.exterior.coords[:-1]]
        holes_c = [[[float(x), float(y)] for x, y in h.coords[:-1]] for h in p.interiors]
        ring_f, holes_f, issues, repairs = finalise_app_ring(ring_c, holes_c, width, height, rules=APP_RING_RULES)
        if issues:
            return {**record, 'refusal': 'app_ring_rules', 'reason': f'part {v}: {issues}'}
        finals.append((v, ring_f, holes_f, repairs))
    source = {'ring': [[float(x), float(y)] for x, y in ring], 'holes': [[[float(x), float(y)] for x, y in h] for h in holes]}
    try:
        check = partition_outlines(source, [{'ring': r, 'holes': h} for _, r, h, _ in finals])
    except ValueError as error:
        return {**record, 'refusal': 'partition_check', 'reason': str(error)}
    final_parts = {v: Polygon(r, h) for v, r, h, _ in finals}
    final = tgs.labels_from_parts(final_parts, ox, oy, shape, mask)
    raster = {tuple(c['parts']): c for c in tgs.shape_cuts(final, mask, dist)}
    cuts, keys = [], sorted(final_parts)
    for i, a in enumerate(keys):
        for b in keys[i+1:]:
            lines = _lines_of(final_parts[a].boundary.intersection(final_parts[b].boundary))
            length = sum(line.length for line in lines)
            if length <= 0:
                continue
            rc = raster.get((a, b), {})
            cuts.append({'cut': f'c{len(cuts)+1}', 'parts': [a, b], 'saddle_m': rc.get('saddle_m'),
                         'smaller_peak_m': rc.get('smaller_peak_m'), 'depth_ratio': rc.get('depth_ratio'),
                         'neck_width_m': rc.get('neck_width_m'), 'length_m': round(length*m_per_px, 2),
                         'lines_px': [[[round(x, 2), round(y, 2)] for x, y in line.coords] for line in lines]})
    children = []
    for v, r, h, repairs in finals:
        sub = final == v
        peak = float(dist[sub].max()) if sub.any() else 0.
        mine = [c for c in cuts if v in c['parts']]
        saddles = [c['saddle_m'] for c in mine if c['saddle_m'] is not None]
        depths = [c['depth_ratio'] for c in mine if c['depth_ratio'] is not None]
        child = {'key': f'crown-{v}', 'label': f'Crown hypothesis {v} (shape sidecar v1; not reviewed)', 'kind': 'tree',
                 'role': 'crown' if peak >= design['min_crown_radius_m'] else 'unresolved-residual',
                 'area_m2': round(final_parts[v].area*m_per_px*m_per_px, 2), 'area_px': round(final_parts[v].area, 2),
                 'peak_radius_m': round(peak, 2), 'saddle_m': max(saddles) if saddles else None,
                 'depth_ratio': min(depths) if depths else None, 'cuts': [c['cut'] for c in mine], 'ring_repairs': repairs,
                 'outline_pixels': {'ring': r, 'holes': h}}
        child['uncertainty'] = _uncertainty(child, mine, record)
        children.append(child)
    return {**record, 'n_parts': len(children), 'cut': True, 'children': children, 'cuts': cuts,
            'partition_check': {k: check[k] for k in ('area_px', 'source_area_px', 'outside_area_px', 'overlap_area_px',
                                                      'missing_area_px', 'numeric_area_tolerance_px')},
            'conservation': tgs.conservation(poly, final_parts, m_per_px)}


# ------------------------------------------------------------------------------------------------------- v1 renders
def _pts(coords, ox, oy, k):
    """Ring coordinates -> panel points (pixel (x, y) is the unit square centred on (x, y), the prototype's convention),
    fixed point for cv2's shift."""
    return np.round((np.asarray(coords, float)-[ox, oy]+0.5)*k*(1 << _SHIFT)).astype(np.int32).reshape(-1, 1, 2)


def _text(img, text, org, scale=.42, colour=(255, 255, 255)):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 1, cv2.LINE_AA)


def _dashed(img, p, q, colour, thickness=2, dash=6.):
    p, q = np.asarray(p, float), np.asarray(q, float)
    length = float(np.hypot(*(q-p)))
    steps = max(1, int(length//dash))
    for i in range(0, steps, 2):
        a, b = p+(q-p)*i/steps, p+(q-p)*min(i+1, steps)/steps
        cv2.line(img, tuple(int(round(v)) for v in a), tuple(int(round(v)) for v in b), colour, thickness, cv2.LINE_AA)


def _frame_marks(panel, poly, ox, oy, k, width, height, tol):
    """Dashed red: the image frame where the crop runs past it, and every outline edge lying on the frame (a frame-cut edge,
    which is the frame and not a crown edge)."""
    h, w = panel.shape[:2]
    for x in (0., float(width)):
        px = (x-ox+.5)*k
        if 0 <= px < w:
            _dashed(panel, (px, 0), (px, h-1), FRAME_COLOUR, 1, 4.)
    for y in (0., float(height)):
        py = (y-oy+.5)*k
        if 0 <= py < h:
            _dashed(panel, (0, py), (w-1, py), FRAME_COLOUR, 1, 4.)
    for ring in [poly.exterior, *poly.interiors]:
        c = list(ring.coords)
        for a, b in zip(c, c[1:], strict=False):
            if any(abs(a[i]-edge) <= tol and abs(b[i]-edge) <= tol for i, edge in ((0, 0.), (0, width), (1, 0.), (1, height))):
                _dashed(panel, ((a[0]-ox+.5)*k, (a[1]-oy+.5)*k), ((b[0]-ox+.5)*k, (b[1]-oy+.5)*k), FRAME_COLOUR, 3, 5.)


def _caption(panel, text):
    out = panel.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 15), (20, 20, 20), -1)
    cv2.putText(out, text, (3, 11), cv2.FONT_HERSHEY_SIMPLEX, .36, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def render_v1(rgb, ring, holes, record, title_lines, out_png, m_per_px, width, height, design=SIDECAR_V1):
    """raw | outline (+ crown peaks) | parts (translucent fills, cut lines labelled c1..), native pixels, nearest-neighbour
    integer zoom only (no resampling), frame and frame-cut edges dashed red. Returns the sheet (RGB array)."""
    poly = Polygon(ring, holes or [])
    ox, oy, shape = _v1_crop(poly, design['pad_px'])
    rd = design['render']
    k = int(max(1, min(rd['max_scale'], round(rd['target_side_px']/max(shape)))))
    crop = tgs.crop_rgb(np.asarray(rgb), ox, oy, shape)
    base = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_NEAREST) if k > 1 else crop.copy()
    raw = tgs._scale_bar(base, m_per_px, k)
    outline = base.copy()
    cv2.polylines(outline, [_pts(ring, ox, oy, k)], True, (255, 255, 0), 1, cv2.LINE_AA, _SHIFT)
    for hole in holes or []:
        cv2.polylines(outline, [_pts(hole, ox, oy, k)], True, (0, 255, 255), 1, cv2.LINE_AA, _SHIFT)
    for (x, y), r in zip(record.get('peaks_px') or [], record.get('peak_radii_m') or [], strict=False):
        cx, cy = int((x-ox+.5)*k), int((y-oy+.5)*k)
        cv2.drawMarker(outline, (cx, cy), (255, 60, 60), cv2.MARKER_TILTED_CROSS, 9, 2)
        _text(outline, f'{r:.1f}', (cx+5, cy-4), .36)
    parts = base.copy()
    children = record.get('children') or []
    if children:
        overlay, covered = parts.copy(), np.zeros(parts.shape[:2], np.uint8)
        for i, child in enumerate(children):
            m = np.zeros(parts.shape[:2], np.uint8)
            cv2.fillPoly(m, [_pts(child['outline_pixels']['ring'], ox, oy, k)], 1, cv2.LINE_8, _SHIFT)
            for hole in child['outline_pixels']['holes']:
                cv2.fillPoly(m, [_pts(hole, ox, oy, k)], 0, cv2.LINE_8, _SHIFT)
            overlay[m > 0] = tgs.PALETTE[i % len(tgs.PALETTE)]
            covered |= m
        a = rd['fill_alpha']
        blend = cv2.addWeighted(parts, 1-a, overlay, a, 0)
        parts[covered > 0] = blend[covered > 0]
        for i, child in enumerate(children):
            cv2.polylines(parts, [_pts(child['outline_pixels']['ring'], ox, oy, k)], True, tgs.PALETTE[i % len(tgs.PALETTE)], 1,
                          cv2.LINE_AA, _SHIFT)
        for cut in record['cuts']:
            for line in cut['lines_px']:
                cv2.polylines(parts, [_pts(line, ox, oy, k)], False, (255, 255, 255), 3, cv2.LINE_AA, _SHIFT)
                cv2.polylines(parts, [_pts(line, ox, oy, k)], False, CUT_COLOUR, 1, cv2.LINE_AA, _SHIFT)
            longest = max(cut['lines_px'], key=lambda ln: LineString(ln).length)
            mid = LineString(longest).interpolate(.5, normalized=True)
            _text(parts, cut['cut'], (int((mid.x-ox+.5)*k)+4, int((mid.y-oy+.5)*k)-4), .45, (255, 160, 255))
        for i, child in enumerate(children):
            c = Polygon(child['outline_pixels']['ring'], child['outline_pixels']['holes']).representative_point()
            _text(parts, str(i+1), (int((c.x-ox+.5)*k)-4, int((c.y-oy+.5)*k)+4), .5)
    else:
        cv2.polylines(parts, [_pts(ring, ox, oy, k)], True, (255, 255, 0), 1, cv2.LINE_AA, _SHIFT)
    for panel in (outline, parts):
        _frame_marks(panel, poly, ox, oy, k, width, height, design['frame_tol_px'])
    n_parts = len(children) or 1
    panels = [_caption(raw, f'raw (native px x{k})'), _caption(outline, 'outline + crown peaks (m)'),
              _caption(parts, f'parts: {n_parts}' + (f", cuts {', '.join(c['cut'] for c in record['cuts'])}" if children else ' (no cut)'))]
    gap = np.full((panels[0].shape[0], 6, 3), 40, np.uint8)
    row = np.hstack([x for p in panels for x in (p, gap)][:-1])
    if row.shape[1] < rd['min_sheet_px']:
        row = np.hstack([row, np.full((row.shape[0], rd['min_sheet_px']-row.shape[1], 3), 24, np.uint8)])
    bar = np.full((8+16*len(title_lines), row.shape[1], 3), 20, np.uint8)
    for i, line in enumerate(title_lines):
        cv2.putText(bar, line, (6, 15+16*i), cv2.FONT_HERSHEY_SIMPLEX, .42, (255, 255, 255), 1, cv2.LINE_AA)
    sheet = np.vstack([bar, row])
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(sheet).save(out_png)
    return sheet


def contact_sheet_v1(sheets, out_png):
    """Stack per-object sheets at their native size (no rescale), padded to the widest."""
    width = max(s.shape[1] for s in sheets)
    rows = []
    for s in sheets:
        rows.append(np.hstack([s, np.full((s.shape[0], width-s.shape[1], 3), 24, np.uint8)]) if s.shape[1] < width else s)
        rows.append(np.full((10, width, 3), 70, np.uint8))
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.vstack(rows[:-1])).save(out_png)


# ------------------------------------------------------------------------------------------------------- v1 driver
def _beat(progress, text):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} sidecar-v1: {text}"
    if progress:
        with open(progress, 'a') as handle:
            handle.write(line+'\n')


def _scene_v1(root, scene_id):
    folder = root/'data/scenes'/scene_id
    model = json.load(open(folder/'latest.json'))
    layer = 'review' if (folder/'review.json').exists() else 'model'
    objects = json.load(open(folder/'review.json'))['objects'] if layer == 'review' else model['objects']
    m = np.asarray(model['pixel_to_meters'], float)
    m_per_px = math.sqrt(abs(float(np.linalg.det(m[:2]))))
    return model, objects, layer, m_per_px, int(model['image_width']), int(model['image_height'])


def _windows(root, design):
    return sorted(p.name for p in (root/'data/scenes').iterdir()
                  if p.is_dir() and p.name.startswith(tuple(design['population']['prefixes'])) and (p/'latest.json').exists())


def _title(n, scene_id, obj, area_m2, m_per_px, rec, tag):
    head = f"T{n} {tag}{scene_id} / {obj['id']}  {obj['kind']} {obj.get('status')}  {area_m2:.1f} m2  {m_per_px:.4f} m/px"
    if rec.get('cut'):
        tail = f"peaks {rec['peaks_detected']} -> parts {rec['n_parts']} | " + '  '.join(
            f"{c['cut']}: saddle {c['saddle_m']} m, depth {c['depth_ratio']}, {c['length_m']} m" for c in rec['cuts'])
    else:
        tail = f"peaks {rec.get('peaks_detected')} -> NO CUT: {rec.get('no_cut_reason') or rec.get('refusal') or rec.get('guard')}"
    return [head, tail[:150]]


def _mark_lines(entries):
    """MARKS lines, one per object, numbered by its T number (the number in its render title). A line with cuts lists them
    as c1, c2 ...; `T right` answers every cut on the line, `T cN wrong` one cut."""
    lines = []
    for e in entries:
        rec, tag = e['record'], e['tag']
        what = f"{e['kind']} {e['status']} {e['area_m2']:.1f} m2, peaks {rec.get('peaks_detected')}"
        render = f"`{e['render'].split('sidecar-v1/', 1)[-1]}`" if e.get('render') else '(no render)'
        if rec.get('cut'):
            areas = ' / '.join(f"{c['area_m2']}" for c in rec['children'])
            cuts = '; '.join(f"**{c['cut']}** parts {c['parts'][0]} | {c['parts'][1]}, saddle {c['saddle_m']} m, depth ratio "
                             f"{c['depth_ratio']}, cut {c['length_m']} m" for c in rec['cuts'])
            lines.append(f"{e['T']}. {tag}{e['scene']} / {e['object_id']}: {cuts}. {what}; parts {areas} m2. {render}")
        else:
            reason = rec.get('no_cut_reason') or rec.get('reason') or rec.get('refusal') or rec.get('guard')
            lines.append(f"{e['T']}. {tag}{e['scene']} / {e['object_id']}: **no cut**, {reason}. {what}. {render}")
    return lines


def run_sidecar_v1(root, out, *, targets=None, progress=None, controls=True, design=SIDECAR_V1):
    """The 2026-10-07 shape sidecar over the windows. Read-only over data/scenes; writes only `out`."""
    t0 = time.time()
    root, out = Path(root).resolve(), Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    targets = None if targets is None else {tuple(t) for t in targets}
    pop = design['population']
    named = [tuple(x) for x in design['named']]
    windows = _windows(root, design)
    _beat(progress, f'start: {len(windows)} windows, targets={len(targets) if targets else None}')
    rows, runs = [], []           # rows: every live window tree >= min area; runs: (tag, scene, obj, area, ...)
    confirmed_pool = []
    for wi, scene_id in enumerate(windows, start=1):
        model, objects, layer, m_per_px, width, height = _scene_v1(root, scene_id)
        for obj in objects:
            if obj.get('kind') not in pop['kinds'] or obj.get('status') in LIVE_EXCLUDED or len(obj.get('ring') or []) < 3:
                continue
            poly = Polygon(obj['ring'], obj.get('holes') or [])
            area_m2 = poly.area*m_per_px*m_per_px
            frame = is_frame_cut(poly, width, height, design['frame_tol_px'])
            key = (scene_id, obj['id'])
            if obj.get('status') == 'confirmed' and not frame and poly.is_valid:
                confirmed_pool.append((-area_m2, scene_id, obj['id'], area_m2))
            is_named = key in named
            if targets is not None and key not in targets:
                continue
            if area_m2 < pop['min_area_m2'] and not is_named and targets is None:
                continue
            row = {'scene': scene_id, 'object_id': obj['id'], 'kind': obj['kind'], 'status': obj.get('status'), 'layer': layer,
                   'area_m2': round(area_m2, 2), 'm_per_px': round(m_per_px, 5), 'frame_cut': frame, 'named': is_named,
                   'in_area_population': area_m2 >= pop['min_area_m2'], 'peaks': None, 'decision': None}
            if obj.get('status') == 'confirmed':
                row['decision'] = 'skip_confirmed'
            elif frame:
                row['decision'] = 'skip_frame_cut'
            elif not poly.is_valid:
                row['decision'] = 'skip_invalid_outline'
            else:
                row['peaks'], row['max_inscribed_radius_m'] = count_peaks(obj['ring'], obj.get('holes'), m_per_px, design)
                row['decision'] = 'run' if row['peaks'] >= pop['min_peaks'] else 'single_peak'
            if is_named or targets is not None or row['decision'] == 'run':
                runs.append(('NAMED ' if is_named else '', scene_id, obj, area_m2, row))
            rows.append(row)
        if wi % 6 == 0:
            _beat(progress, f'survey {wi}/{len(windows)} windows, trees {len(rows)}, to run {len(runs)}')
    control_runs = []
    if controls and targets is None:
        for _, scene_id, object_id, area_m2 in sorted(confirmed_pool)[:design['controls']['n']]:
            control_runs.append((scene_id, object_id, area_m2))
    # run, scene by scene so each image is opened once
    by_scene = defaultdict(list)
    for tag, scene_id, obj, area_m2, row in runs:
        by_scene[scene_id].append(('run', tag, obj['id'], area_m2, row))
    for scene_id, object_id, area_m2 in control_runs:
        by_scene[scene_id].append(('control', 'CONTROL ', object_id, area_m2, None))
    entries, done = [], 0
    for scene_id in sorted(by_scene):
        model, objects, layer, m_per_px, width, height = _scene_v1(root, scene_id)
        by_id = {o['id']: o for o in objects}
        for mode, tag, object_id, area_m2, row in by_scene[scene_id]:
            obj = by_id[object_id]
            t1 = time.time()
            rec = crown_partition(obj['ring'], obj.get('holes'), m_per_px, width, height, design,
                                  status=obj.get('status'), control=(mode == 'control'))
            entries.append({'mode': mode, 'tag': tag, 'scene': scene_id, 'object_id': object_id, 'kind': obj['kind'],
                            'status': obj.get('status'), 'area_m2': round(area_m2, 2), 'm_per_px': round(m_per_px, 5),
                            'layer': layer, 'named': (scene_id, object_id) in named,
                            'population_decision': (row or {}).get('decision'), 'record': rec,
                            'seconds': round(time.time()-t1, 2), 'render': None})
            done += 1
            if done % 10 == 0:
                _beat(progress, f'partitioned {done}/{len(runs)+len(control_runs)} objects')

    # order: named first (card order), then cuts by scene / area desc, then no-cut runs, then controls
    def order(e):
        if e['named'] and e['mode'] == 'run':
            return (0, named.index((e['scene'], e['object_id'])), '', 0.)
        if e['mode'] == 'control':
            return (3, 0, e['scene'], -e['area_m2'])
        return (1 if e['record'].get('cut') else 2, 0, e['scene'], -e['area_m2'])
    entries.sort(key=order)
    for n, e in enumerate(entries, start=1):
        e['T'] = n
    # renders, one image open per scene; T numbers in the titles match the MARKS list
    by_scene_entries = defaultdict(list)
    for e in entries:
        if e['record'].get('guard') in (None, 'frame_cut'):
            by_scene_entries[e['scene']].append(e)
    for scene_id in sorted(by_scene_entries):
        model, objects, layer, m_per_px, width, height = _scene_v1(root, scene_id)
        rgb = np.asarray(Image.open(root/'data/scenes'/scene_id/f"{model['image_sha256']}.png").convert('RGB'))
        by_id = {o['id']: o for o in objects}
        for e in by_scene_entries[scene_id]:
            obj = by_id[e['object_id']]
            path = out/'renders'/f"{scene_id}.{e['object_id']}.png"
            e['render'] = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            title = _title(e['T'], scene_id, obj, e['area_m2'], m_per_px, e['record'], e['tag'])
            e['sheet'] = render_v1(rgb, obj['ring'], obj.get('holes'), e['record'], title, path, m_per_px, width, height, design)
    _beat(progress, f'renders done ({sum(1 for e in entries if e.get("sheet") is not None)}); writing contact sheets / MARKS')
    # contact sheets: per window (its run objects), the controls, the named pair
    per_window = defaultdict(list)
    for e in entries:
        if e.get('sheet') is not None and e['mode'] == 'run':
            per_window[e['scene']].append(e['sheet'])
    for scene_id, sheets in sorted(per_window.items()):
        contact_sheet_v1(sheets, out/'contact'/f'{scene_id}.png')
    ctl = [e['sheet'] for e in entries if e['mode'] == 'control' and e.get('sheet') is not None]
    if ctl:
        contact_sheet_v1(ctl, out/'contact'/'controls.png')
    nm = [e['sheet'] for e in entries if e['named'] and e['mode'] == 'run' and e.get('sheet') is not None]
    if nm:
        contact_sheet_v1(nm, out/'contact'/'named.png')
    # split payloads (sidecar hypotheses in the split API's child shape; never previewed or applied here)
    for e in entries:
        if e['mode'] == 'run' and e['record'].get('cut'):
            payload = {'scene': e['scene'], 'object_id': e['object_id'], 'version': design['version'], 'applied': False,
                       'previewed': False, 'T': e['T'],
                       'children': [{k: c[k] for k in ('key', 'label', 'kind', 'role', 'outline_pixels')} for c in e['record']['children']],
                       'note': 'Sidecar hypothesis only (card item 3(a)): marked right / wrong by the reviewer first; a tally the '
                               'reviewer accepts gates any preview_object_split hypothesis, never applied by an agent.'}
            (out/'splits').mkdir(parents=True, exist_ok=True)
            json.dump(payload, open(out/'splits'/f"{e['scene']}.{e['object_id']}.json", 'w'), indent=1)
    with open(out/'records.jsonl', 'w') as handle:
        for e in entries:
            handle.write(json.dumps({k: v for k, v in e.items() if k != 'sheet'})+'\n')
    json.dump(rows, open(out/'population.json', 'w'), indent=1)
    numbers = _v1_numbers(rows, entries, design)
    summary = {'version': design['version'], 'generated': datetime.now(UTC).isoformat(timespec='seconds'),
               'code': 'scripts/ml/experiments/tree_crown_split.py', 'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'core': 'scripts/ml/experiments/tree_group_split.py',
               'core_sha256': hashlib.sha256(Path(tgs.__file__).read_bytes()).hexdigest(),
               'design': design, 'windows': len(windows), 'numbers': numbers, 'seconds': round(time.time()-t0, 1),
               'note': 'Every figure is a count or a geometry, never accuracy. Read-only over data/scenes; nothing previewed, '
                       'applied, counted or exported for training.'}
    json.dump(summary, open(out/'summary.json', 'w'), indent=1)
    write_marks_v1(out, entries, numbers, design, windows=len(windows))
    _beat(progress, f"done in {summary['seconds']} s: {json.dumps(numbers['headline'])}")
    return summary


def _v1_numbers(rows, entries, design):
    pop = [r for r in rows if r['in_area_population']]
    decisions = Counter(r['decision'] for r in pop)
    runs = [e for e in entries if e['mode'] == 'run']
    pop_runs = [e for e in runs if e['population_decision'] == 'run']
    ctl = [e for e in entries if e['mode'] == 'control']
    refusals = Counter(e['record'].get('refusal') for e in runs if e['record'].get('refusal'))
    return {
        'headline': {'window_trees_ge_50': len(pop), 'objects_run': len(pop_runs), 'objects_with_cut': sum(e['record'].get('cut', False) for e in pop_runs),
                     'cuts_proposed': sum(len(e['record'].get('cuts') or []) for e in pop_runs),
                     'parts_proposed': sum(e['record']['n_parts'] for e in pop_runs if e['record'].get('cut')),
                     'controls_run': len(ctl), 'controls_cut': sum(e['record'].get('cut', False) for e in ctl),
                     'control_cuts': sum(len(e['record'].get('cuts') or []) for e in ctl)},
        'population_decisions': dict(sorted(decisions.items())),
        'peaks_distribution_runnable': dict(sorted(Counter(r['peaks'] for r in pop if r['peaks'] is not None).items())),
        'refusals': dict(refusals),
        'no_cut_reasons': dict(Counter((e['record'].get('no_cut_reason') or '').split(':')[0].split('(')[0].strip()
                                       for e in pop_runs if not e['record'].get('cut') and not e['record'].get('refusal'))),
        'named': [{'scene': e['scene'], 'object_id': e['object_id'], 'area_m2': e['area_m2'], 'decision': e['population_decision'],
                   'peaks': e['record'].get('peaks_detected'), 'cut': e['record'].get('cut'), 'n_parts': e['record'].get('n_parts'),
                   'reason': e['record'].get('no_cut_reason') or e['record'].get('reason')} for e in runs if e['named']],
        'controls': [{'scene': e['scene'], 'object_id': e['object_id'], 'area_m2': e['area_m2'], 'peaks': e['record'].get('peaks_detected'),
                      'cut': e['record'].get('cut'), 'n_parts': e['record'].get('n_parts')} for e in ctl],
        'cut_depth_ratios': sorted(c['depth_ratio'] for e in pop_runs for c in (e['record'].get('cuts') or []) if c['depth_ratio'] is not None),
        'partition_checks_passed': sum(1 for e in runs if e['record'].get('partition_check')),
        'max_symmetric_difference_px2': max([e['record']['conservation']['symmetric_difference_px2'] for e in runs + ctl
                                             if e['record'].get('conservation')] or [0.]),
    }


def write_marks_v1(out, entries, numbers, design, windows=None):
    h = numbers['headline']
    named = [e for e in entries if e['named'] and e['mode'] == 'run']
    cuts = [e for e in entries if not e['named'] and e['mode'] == 'run' and e['record'].get('cut')]
    nocut = [e for e in entries if not e['named'] and e['mode'] == 'run' and not e['record'].get('cut')]
    ctl = [e for e in entries if e['mode'] == 'control']
    body = [
        '# MARKS: tree crown shape sidecar v1 (2026-10-07)', '',
        'Card: `data/reviews/eyes/experiments/tree-group-split-2026-10-03/DECISION-CARD.md`, answered 2026-10-07 20:02 AEDT '
        '"yes to all defaults". Shape-only crown hypotheses (item 2(a): min crown radius 1.5 m, saddle merge 0.7, parts under '
        '8 m2 merged into the longest-edge neighbour, holes filled before the distance transform, frame-cut objects skipped, '
        "confirmed objects never split; children an exact partition checked by the split API's own partition rule). Nothing "
        'was previewed, applied, counted or exported for training; data/scenes was only read.', '',
        f"Population: {h['window_trees_ge_50']} live window trees >= 50 m2 (review layer, {windows or '?'} windows); decisions "
        f"{numbers['population_decisions']}. Objects run (>= 2 peaks): {h['objects_run']}; cuts proposed: {h['cuts_proposed']} "
        f"on {h['objects_with_cut']} object(s); controls cut: {h['controls_cut']} of {h['controls_run']} (null reading, expected 0).",
        '',
        '## How to answer (in chat)', '',
        'One line per object; the line number is the T number in its render title. Reply with numbers and a word: `3 right`,',
        '`4 wrong`, `5-12 right`, `7 unclear`; for one cut on a line with several, `3 c2 wrong`. On a line with cuts: right =',
        'the cut follows a visible crown edge; wrong = it runs through one crown, or a part still holds two crowns. On a',
        '**no cut** line: right = one crown (or no shape cue) is the right reading; wrong = a crown edge was missed (say where',
        'if you can). Unmarked lines stay unmarked. Renders (paths under `sidecar-v1/`): raw | outline + crown peaks (inscribed',
        'radius, m) | parts (translucent fills, cut lines magenta with their cN labels); native pixels, integer nearest-neighbour',
        'zoom shown as xN; image frame and frame-cut edges dashed red. Per-window contact sheets: `contact/<window>.png`;',
        '`contact/named.png`, `contact/controls.png`.', '',
        '## The two named objects (run whatever the population filters say)', '']
    body += _mark_lines(named)
    body += ['', f"## Proposed cuts ({h['cuts_proposed']} cut(s) on {h['objects_with_cut']} object(s))", '']
    body += _mark_lines(cuts) or ['(none)']
    body += ['', f'## Run, no cut ({len(nocut)} objects with >= 2 peaks left whole by the saddle / floor rules or a check)', '']
    body += _mark_lines(nocut) or ['(none)']
    body += ['', f"## Controls: confirmed single-crown window trees, the 10 largest not frame-cut (null reading; "
             f"{h['controls_cut']} of {h['controls_run']} cut)", '']
    body += _mark_lines(ctl) or ['(none)']
    body += ['', '---', 'A tally you accept gates any preview_object_split hypothesis (previewed, never applied by an agent), then '
             'dated RULES / DESIGN notes; the 99 review-set scenes stay out without a separate approval. Records: '
             '`records.jsonl`; split payloads: `splits/`; numbers: `summary.json`; every tree >= 50 m2 and its decision: '
             '`population.json`.', '']
    (out/'MARKS-trees-v1.md').write_text('\n'.join(body))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--target', action='append', default=None, help='scene_id:object_id (probe: required; sidecar: optional subset)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--root', default='.')
    ap.add_argument('--adaptive', action='store_true', help='peak separation = max(.8 m, .6 x largest inscribed radius)')
    ap.add_argument('--sidecar-v1', action='store_true', help='the 2026-10-07 shape sidecar (card item 2(a)) over the windows')
    ap.add_argument('--progress', default=None, help='sidecar: append heartbeat lines to this file')
    ap.add_argument('--no-controls', action='store_true', help='sidecar: skip the confirmed single-crown controls')
    args = ap.parse_args()
    if args.sidecar_v1:
        targets = [tuple(t.split(':', 1)) for t in args.target] if args.target else None
        summary = run_sidecar_v1(Path(args.root), Path(args.out), targets=targets, progress=args.progress,
                                 controls=not args.no_controls)
        print(json.dumps(summary['numbers']))
        return
    if not args.target:
        ap.error('--target is required for the probe (or pass --sidecar-v1)')
    root, out = Path(args.root), Path(args.out)
    design = ADAPTIVE if args.adaptive else DESIGN
    results = [probe(root, *t.split(':', 1), out, design) for t in args.target]
    out.mkdir(parents=True, exist_ok=True)
    json.dump({'design': design, 'results': results}, open(out/'metrics.json', 'w'), indent=1)
    for r in results:
        if 'error' in r:
            print(r['scene'], r['object'], r['error'])
            continue
        print(f"{r['scene']} {r['object']} {r['kind']} {r['area_m2']} m2 replaces={r['replaces']} peaks={r['shape']['peaks']} "
              f"shape_regions={r['shape']['watershed_regions']} texture_regions={r['texture']['texture_seeded_watershed_regions']} "
              f"erosion={[(e['r_m'], e['components']) for e in r['shape']['erosion']]}")


if __name__ == '__main__':
    main()
