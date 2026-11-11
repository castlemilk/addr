"""Tree-group split prototype, SIDECAR only (2026-10-03). Read-only over data/scenes; nothing is applied to a scene.

Given ONE tree/vegetation polygon (one SAM3 'tree canopy' mask component, see scene_extract.extract_objects), propose a
partition into crowns two ways and emit the parts in the editor split API's child shape (key, label, outline_pixels
{ring, holes}); the children are hypotheses for `preview_object_split`, never applied here.

  shape    distance transform of the rasterised polygon; crown peaks = local maxima of the distance that are at least
           `min_crown_radius_m` (1.5 m) high and `min_crown_radius_m` apart; priority-flood watershed from the peaks;
           two basins are MERGED when the saddle between them (the highest distance along their shared boundary) is above
           `saddle_merge_ratio` (0.7) x the smaller peak, i.e. the neck is too shallow to count as two crowns.
           Confidence per cut: saddle_m, depth_ratio = 1 - saddle / smaller peak, neck_width_m ~ 2 x saddle.
  texture  per pixel inside the polygon: CIE Lab + local grey std over a ~1 m window, box-smoothed over ~1 m inside the
           mask, standardised; k-means (k = 2) seeds; cleaned components >= the part floor become seeds; seeded region
           growing (priority = feature distance to the seed's centroid) assigns the rest. Confidence per cut: Fisher-type
           feature separation of the two parts, the share of that separation carried by L alone (a sun/shade reading, not
           a guard), and the shape saddle along the texture cut.
Guards (both variants): parts below `min_part_m2` (8 m2) are merged into the neighbour with the longest shared edge; the
vector partition is built by polygonizing the original outline with the simplified dividing lines, so the parts tile the
original polygon exactly (area conservation is measured and written, never assumed); every part records its variant.
Every figure is a count, an area or a geometry; none is accuracy.

Run from the repo root (no model inference):
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.tree_group_split \
        --target win-2fbb386c3b235ff4:b1e978e7c391f56b --target lot-29358:7c29c8c28d31c70f \
        --out data/reviews/eyes/experiments/tree-group-split-2026-10-03/prototype
"""
from __future__ import annotations

import argparse
import heapq
import json
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.cluster.vq import kmeans2
from shapely.geometry import Polygon, box
from shapely.ops import linemerge, polygonize, unary_union
from shapely.validation import make_valid

from ..segmentation import iter_polygons

DESIGN = {
    'min_crown_radius_m': 1.5,      # peaks below this inscribed radius are not crowns; also the peak separation
    'saddle_merge_ratio': 0.7,      # merge two basins when saddle > ratio x smaller peak
    'min_part_m2': 8.0,             # never emit a part below this; merged into the longest-edge neighbour
    'pad_px': 12,                   # crop padding around the polygon
    'simplify_px': 1.5,             # Douglas-Peucker tolerance on the dividing lines only (outline untouched)
    'texture': {'k': 2, 'k_alt': 3, 'std_window_m': 1.0, 'smooth_m': 1.0, 'majority_px': 7, 'seed': 7,
                'features': ['L', 'a', 'b', 'local_std'], 'shade_L_share': 0.8},
    'render_scale': 2,
}
PALETTE = [(31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40), (148, 103, 189), (140, 86, 75),
           (227, 119, 194), (127, 127, 127), (188, 189, 34), (23, 190, 207)]


# ----------------------------------------------------------------------------------------------- scene access (read-only)
def load_object(root: Path, scene_id: str, object_id: str):
    folder = root/'data/scenes'/scene_id
    model = json.load(open(folder/'latest.json'))
    review = json.load(open(folder/'review.json')) if (folder/'review.json').exists() else {'objects': []}
    live = next((o for o in review.get('objects', []) if o['id'] == object_id), None)
    base = next((o for o in model['objects'] if o['id'] == object_id), None)
    image = Image.open(folder/f"{model['image_sha256']}.png").convert('RGB')
    return model, live or base, base, image


def object_polygon(obj):
    poly = Polygon(obj['ring'], obj.get('holes') or [])
    valid = poly.is_valid
    if not valid:
        fixed = make_valid(poly)
        poly = max(iter_polygons(fixed), key=lambda p: p.area)
    return poly, valid


# ----------------------------------------------------------------------------------------------- raster helpers
def rasterise(poly: Polygon, ox: int, oy: int, shape):
    """Pixel (x, y) is the unit square centred on integer coordinate (x, y); cv2.fillPoly samples pixel centres."""
    mask = np.zeros(shape, np.uint8)
    ring = (np.asarray(poly.exterior.coords[:-1]) - [ox, oy])
    cv2.fillPoly(mask, [np.round(ring).astype(np.int32).reshape(-1, 1, 2)], 1)
    for hole in poly.interiors:
        pts = (np.asarray(hole.coords[:-1]) - [ox, oy])
        cv2.fillPoly(mask, [np.round(pts).astype(np.int32).reshape(-1, 1, 2)], 0)
    return mask.astype(bool)


def priority_flood(surface, markers, mask):
    """Marker-confined watershed by priority flooding (8-neighbour): pixels with the HIGHEST surface value flood first;
    every mask pixel ends in the basin of one marker. Labels 0 outside the mask, 1.. inside."""
    labels = np.zeros(mask.shape, np.int32)
    if markers.max() == 0:
        return labels
    h, w = mask.shape
    heap = []
    ys, xs = np.nonzero(markers)
    for y, x in zip(ys, xs, strict=True):
        labels[y, x] = markers[y, x]
        heapq.heappush(heap, (-float(surface[y, x]), int(y), int(x)))
    while heap:
        prio, y, x = heapq.heappop(heap)
        label = labels[y, x]
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx = y+dy, x+dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and labels[ny, nx] == 0:
                    labels[ny, nx] = label
                    heapq.heappush(heap, (max(prio, -float(surface[ny, nx])), ny, nx))
    return labels


def srg(features_z, centroids, markers, mask):
    """Seeded region growing on standardised features: priority = ||z(pixel) - centroid(label)||."""
    labels = np.zeros(mask.shape, np.int32)
    if markers.max() == 0:
        return labels
    h, w = mask.shape
    labels[markers > 0] = markers[markers > 0]
    heap = []

    def push_neighbours(y, x, label):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx = y+dy, x+dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and labels[ny, nx] == 0:
                    d = float(np.linalg.norm(features_z[ny, nx]-centroids[label-1]))
                    heapq.heappush(heap, (d, ny, nx, label))
    for y, x in zip(*np.nonzero(markers), strict=True):
        push_neighbours(int(y), int(x), int(markers[y, x]))
    while heap:
        _, y, x, label = heapq.heappop(heap)
        if labels[y, x] != 0:
            continue
        labels[y, x] = label
        push_neighbours(y, x, label)
    return labels


def region_areas_m2(labels, mask, m_per_px):
    vals = [int(v) for v in np.unique(labels[mask]) if v]
    return {v: float((labels == v).sum())*m_per_px*m_per_px for v in vals}


def shared_boundary_px(labels, a, b):
    """Count of 4-neighbour pixel pairs with labels (a, b)."""
    la, lb = labels == a, labels == b
    n = 0
    n += int((la[:, :-1] & lb[:, 1:]).sum()) + int((lb[:, :-1] & la[:, 1:]).sum())
    n += int((la[:-1, :] & lb[1:, :]).sum()) + int((lb[:-1, :] & la[1:, :]).sum())
    return n


def enforce_floor(labels, mask, m_per_px, floor_m2):
    """Merge every region below floor_m2 into the neighbouring region with the longest shared edge. Returns (labels, log)."""
    labels = labels.copy()
    log = []
    while True:
        areas = region_areas_m2(labels, mask, m_per_px)
        if len(areas) <= 1:
            break
        small = [v for v, a in sorted(areas.items(), key=lambda kv: kv[1]) if a < floor_m2]
        if not small:
            break
        v = small[0]
        others = [u for u in areas if u != v]
        best = max(others, key=lambda u: shared_boundary_px(labels, v, u))
        if shared_boundary_px(labels, v, best) == 0:
            best = max(others, key=lambda u: areas[u])     # isolated island: give it to the largest region
        log.append({'merged': v, 'into': best, 'area_m2': round(areas[v], 2), 'reason': f'below {floor_m2} m2 floor'})
        labels[labels == v] = best
    return labels, log


def relabel(labels):
    out = np.zeros_like(labels)
    for i, v in enumerate(sorted(v for v in np.unique(labels) if v), start=1):
        out[labels == v] = i
    return out


def labels_from_parts(parts, ox, oy, crop_shape, mask):
    """Rasterise the final vector parts back onto the crop (pixel = unit square on an integer coordinate); any mask pixel
    missed by every part (edge rounding) takes the label of its nearest labelled pixel."""
    labels = np.zeros(crop_shape, np.int32)
    for v, poly in sorted(parts.items()):
        labels[rasterise(poly, ox, oy, crop_shape) & mask] = v
    missing = mask & (labels == 0)
    if missing.any() and labels.max():
        _, (iy, ix) = ndimage.distance_transform_edt(labels == 0, return_indices=True)
        labels[missing] = labels[iy, ix][missing]
    return labels


def shape_cuts(labels, mask, dist):
    """Per adjacent pair of parts: saddle = highest min(dist) across the shared 4-neighbour boundary, the smaller of the two
    parts' largest inscribed radii, depth_ratio = 1 - saddle/smaller peak (0 = no neck, 1 = pinched to a point)."""
    vals = [int(v) for v in np.unique(labels[mask]) if v]
    cuts = []
    for i, a in enumerate(vals):
        for b in vals[i+1:]:
            la, lb = labels == a, labels == b
            pairs = []
            for (sa, sb, da, db) in [(la[:, :-1], lb[:, 1:], dist[:, :-1], dist[:, 1:]), (lb[:, :-1], la[:, 1:], dist[:, :-1], dist[:, 1:]),
                                     (la[:-1, :], lb[1:, :], dist[:-1, :], dist[1:, :]), (lb[:-1, :], la[1:, :], dist[:-1, :], dist[1:, :])]:
                sel = sa & sb
                if sel.any():
                    pairs.append(np.minimum(da, db)[sel])
            if not pairs:
                continue
            h = float(np.concatenate(pairs).max())
            smaller = min(float(dist[la].max()), float(dist[lb].max()))
            cuts.append({'parts': [a, b], 'saddle_m': round(h, 2), 'smaller_peak_m': round(smaller, 2),
                         'depth_ratio': round(1-h/max(smaller, 1e-6), 3), 'neck_width_m': round(2*h, 2),
                         'cut_length_px': int(sum(len(x) for x in pairs))})
    return cuts



def tile_outside(labels, mask):
    """Give every pixel outside the mask the label of its nearest mask pixel so the label raster tiles the crop."""
    if labels.max() == 0:
        return labels
    _, (iy, ix) = ndimage.distance_transform_edt(~mask, return_indices=True)
    full = labels[iy, ix]
    full[mask] = labels[mask]
    return full


# ----------------------------------------------------------------------------------------------- shape variant
def shape_variant(mask, m_per_px, design):
    t0 = time.time()
    dist = ndimage.distance_transform_edt(mask)*m_per_px
    r_m = design['min_crown_radius_m']
    r_px = max(1, int(round(r_m/m_per_px)))
    yy, xx = np.mgrid[-r_px:r_px+1, -r_px:r_px+1]
    footprint = (xx*xx+yy*yy) <= r_px*r_px
    peaks = (dist == ndimage.maximum_filter(dist, footprint=footprint)) & (dist >= r_m)
    plateau, n_peaks = ndimage.label(peaks)
    markers = np.zeros(mask.shape, np.int32)
    peak_height = {}
    for i in range(1, n_peaks+1):
        ys, xs = np.nonzero(plateau == i)
        cy, cx = ys.mean(), xs.mean()
        j = int(np.argmin((ys-cy)**2+(xs-cx)**2))
        markers[ys[j], xs[j]] = i
        peak_height[i] = float(dist[ys[j], xs[j]])
    info = {'max_inscribed_radius_m': round(float(dist.max()), 2), 'min_crown_radius_m': r_m, 'peaks_detected': int(n_peaks),
            'peak_radii_m': [round(peak_height[i], 2) for i in range(1, n_peaks+1)], 'merges': [], 'cuts': []}
    if n_peaks == 0:
        info['no_split_reason'] = f'largest inscribed radius {dist.max():.2f} m below the {r_m} m minimum crown radius'
        info['seconds'] = round(time.time()-t0, 2)
        return np.where(mask, 1, 0).astype(np.int32), dist, peaks, info
    basins = priority_flood(dist, markers, mask)
    # saddles between adjacent basins: the highest min(dist) across a 4-neighbour boundary pair
    saddle = {}
    for (sa, sb, da, db) in [(basins[:, :-1], basins[:, 1:], dist[:, :-1], dist[:, 1:]),
                             (basins[:-1, :], basins[1:, :], dist[:-1, :], dist[1:, :])]:
        diff = (sa != sb) & (sa > 0) & (sb > 0)
        low = np.minimum(da, db)[diff]
        for a, b, h in zip(sa[diff], sb[diff], low, strict=True):
            key = (int(min(a, b)), int(max(a, b)))
            if h > saddle.get(key, -1):
                saddle[key] = float(h)
    # iterative merge: highest saddle ratio first
    parent = {i: i for i in range(1, n_peaks+1)}
    peak = dict(peak_height)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    while True:
        best = None
        for (a, b), h in saddle.items():
            ra, rb = find(a), find(b)
            if ra == rb:
                continue
            ratio = h/min(peak[ra], peak[rb])
            if best is None or ratio > best[0]:
                best = (ratio, ra, rb, h)
        if best is None or best[0] <= design['saddle_merge_ratio']:
            break
        ratio, ra, rb, h = best
        keep, drop = (ra, rb) if peak[ra] >= peak[rb] else (rb, ra)
        parent[drop] = keep
        peak[keep] = max(peak[keep], peak[drop])
        info['merges'].append({'kept_peak': keep, 'merged_peak': drop, 'saddle_m': round(h, 2), 'ratio_to_smaller_peak': round(ratio, 3)})
    merged = np.zeros_like(basins)
    for i in range(1, n_peaks+1):
        merged[basins == i] = find(i)
    merged = relabel(merged)
    info['basin_cuts'] = shape_cuts(merged, mask, dist)
    group_peak = {int(v): float(dist[merged == v].max()) for v in np.unique(merged) if v}
    info['peaks_after_merge'] = int(merged.max())
    info['part_peak_radius_m'] = {int(k): round(v, 2) for k, v in group_peak.items()}
    info['seconds'] = round(time.time()-t0, 2)
    return merged, dist, peaks, info


# ----------------------------------------------------------------------------------------------- texture variant
def texture_features(rgb, mask, m_per_px, tdesign):
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    w = max(3, int(round(tdesign['std_window_m']/m_per_px)) | 1)
    mean = cv2.blur(grey, (w, w))
    sq = cv2.blur(grey*grey, (w, w))
    local_std = np.sqrt(np.maximum(sq-mean*mean, 0))
    feats = np.dstack([lab, local_std])                                  # L, a, b, std (raw units)
    # masked box smoothing (~smooth_m) so outside pixels do not leak in
    s = max(3, int(round(tdesign['smooth_m']/m_per_px)) | 1)
    m = mask.astype(np.float32)
    num = cv2.blur(feats*m[..., None], (s, s))
    den = cv2.blur(m, (s, s))[..., None]
    smooth = np.where(den > 1e-6, num/np.maximum(den, 1e-6), feats)
    return feats, smooth, w, s


def texture_variant(rgb, mask, dist, m_per_px, design):
    t0 = time.time()
    td = design['texture']
    raw, smooth, std_w, smooth_w = texture_features(rgb, mask, m_per_px, td)
    inside = smooth[mask]
    mu, sd = inside.mean(0), inside.std(0)+1e-6
    z_full = (smooth-mu)/sd
    z = z_full[mask]
    floor_px = design['min_part_m2']/(m_per_px*m_per_px)
    readings = {}
    chosen = None
    for k in (td['k'], td['k_alt']):
        rng = np.random.default_rng(td['seed'])
        centroids, lab_idx = kmeans2(z, k, minit='++', seed=rng)
        label_map = np.zeros(mask.shape, np.int32)
        label_map[mask] = lab_idx+1
        cleaned = label_map.copy()
        for value in range(1, k+1):
            vote = cv2.blur((label_map == value).astype(np.float32), (td['majority_px'], td['majority_px']))
            cleaned = np.where(mask & (vote > 0.5), value, cleaned)
        cleaned = np.where(mask, cleaned, 0)
        # seeds: components per cluster that reach the floor
        markers = np.zeros(mask.shape, np.int32)
        seed_cluster = {}
        nxt = 1
        comp_areas = []
        for value in range(1, k+1):
            comp, n = ndimage.label(cleaned == value)
            sizes = ndimage.sum(np.ones_like(comp), comp, range(1, n+1)) if n else []
            for i, sz in enumerate(sizes, start=1):
                comp_areas.append(round(float(sz)*m_per_px*m_per_px, 1))
                if sz >= floor_px:
                    markers[comp == i] = nxt
                    seed_cluster[nxt] = value
                    nxt += 1
        seed_centroids = np.array([centroids[seed_cluster[i]-1] for i in range(1, nxt)]) if nxt > 1 else centroids[:0]
        grown = srg(z_full, seed_centroids, markers, mask) if nxt > 1 else np.where(mask, 1, 0).astype(np.int32)
        if nxt == 1:
            grown = np.where(mask, 1, 0).astype(np.int32)
        cluster_stats = []
        for value in range(1, k+1):
            sel = label_map[mask] == value
            cluster_stats.append({'cluster': value, 'pixels': int(sel.sum()), 'area_m2': round(float(sel.sum())*m_per_px*m_per_px, 1),
                                  'centroid_L_a_b_std': [round(float(v), 1) for v in centroids[value-1]*sd+mu]})
        readings[k] = {'clusters': cluster_stats, 'seed_components_ge_floor': int(nxt-1),
                       'component_areas_m2_sorted': sorted(comp_areas, reverse=True)[:10], 'labels': grown}
        if k == td['k']:
            chosen = k
    grown = readings[chosen]['labels']
    info = {'k': chosen, 'std_window_px': std_w, 'smooth_px': smooth_w, 'feature_order': td['features'],
            'feature_mean': [round(float(v), 2) for v in mu], 'feature_sd': [round(float(v), 2) for v in sd],
            'kmeans': {k: {kk: vv for kk, vv in r.items() if kk != 'labels'} for k, r in readings.items()},
            'k_alt_regions_after_floor': None, 'cuts': []}
    # k_alt region count after the same floor, for information only
    alt, _ = enforce_floor(readings[td['k_alt']]['labels'], mask, m_per_px, design['min_part_m2'])
    info['k_alt_regions_after_floor'] = int(len(region_areas_m2(alt, mask, m_per_px)))
    info['seconds'] = round(time.time()-t0, 2)
    return grown, z_full, info


def texture_cut_confidence(labels, mask, z_full, dist, design):
    """Fisher-type separation between adjacent parts in standardised feature space; L share of the centroid gap; shape
    saddle along the texture cut."""
    td = design['texture']
    cuts = []
    vals = [int(v) for v in np.unique(labels[mask]) if v]
    stats = {v: (z_full[labels == v].mean(0), z_full[labels == v].var(0)) for v in vals}
    for i, a in enumerate(vals):
        for b in vals[i+1:]:
            n = shared_boundary_px(labels, a, b)
            if n == 0:
                continue
            ma, va = stats[a]
            mb, vb = stats[b]
            gap = ma-mb
            fisher = float(np.sqrt(np.sum(gap*gap/(0.5*(va+vb)+1e-6))))
            l_share = float(gap[0]**2/max(np.sum(gap*gap), 1e-9))
            # saddle along the cut
            la, lb = labels == a, labels == b
            pairs = []
            for (sa, sb, da, db) in [(la[:, :-1], lb[:, 1:], dist[:, :-1], dist[:, 1:]), (lb[:, :-1], la[:, 1:], dist[:, :-1], dist[:, 1:]),
                                     (la[:-1, :], lb[1:, :], dist[:-1, :], dist[1:, :]), (lb[:-1, :], la[1:, :], dist[:-1, :], dist[1:, :])]:
                sel = sa & sb
                if sel.any():
                    pairs.append(np.minimum(da, db)[sel])
            along = np.concatenate(pairs) if pairs else np.zeros(1)
            peak_a, peak_b = float(dist[la].max()), float(dist[lb].max())
            cuts.append({'parts': [a, b], 'feature_separation': round(fisher, 2), 'L_share_of_gap': round(l_share, 2),
                         'shade_only_reading': bool(l_share >= td['shade_L_share']),
                         'centroid_gap_z_L_a_b_std': [round(float(v), 2) for v in gap],
                         'shape_saddle_along_cut_m': round(float(along.max()), 2), 'smaller_peak_m': round(min(peak_a, peak_b), 2),
                         'shape_depth_ratio_along_cut': round(1-float(along.max())/max(min(peak_a, peak_b), 1e-6), 3),
                         'cut_length_px': int(n)})
    return cuts


# ----------------------------------------------------------------------------------------------- vector partition
def pixel_regions(labels_full, ox, oy):
    """Pixel-aligned region polygons (unit squares centred on integer coordinates) per label; they tile the crop."""
    regions = {}
    h, w = labels_full.shape
    for v in (int(u) for u in np.unique(labels_full) if u):
        boxes = []
        for y in range(h):
            row = labels_full[y] == v
            if not row.any():
                continue
            d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
            for x0, x1 in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0], strict=True):
                boxes.append(box(x0+ox-0.5, y+oy-0.5, x1+ox-0.5, y+oy+0.5))
        regions[v] = unary_union(boxes)
    return regions


def dividing_lines(regions, simplify_px):
    lines = []
    keys = sorted(regions)
    for i, a in enumerate(keys):
        for b in keys[i+1:]:
            shared = regions[a].boundary.intersection(regions[b].boundary)
            if shared.is_empty:
                continue
            merged = linemerge(shared) if shared.geom_type != 'LineString' else shared
            parts = [merged] if merged.geom_type == 'LineString' else [g for g in getattr(merged, 'geoms', []) if g.geom_type == 'LineString']
            for line in parts:
                lines.append(line.simplify(simplify_px, preserve_topology=False))
    return lines


def vector_partition(poly: Polygon, labels_full, ox, oy, m_per_px, design):
    """Partition `poly` along the simplified dividing lines of the label raster. Returns ({label: Polygon}, log)."""
    log = []
    vals = [int(v) for v in np.unique(labels_full) if v]
    if len(vals) <= 1:
        return {1: poly}, log
    regions = pixel_regions(labels_full, ox, oy)
    lines = dividing_lines(regions, design['simplify_px'])
    arrangement = unary_union([poly.boundary] + lines)
    faces = [f for f in polygonize(arrangement) if poly.contains(f.representative_point())]
    assigned = {}
    for f in faces:
        best = max(regions, key=lambda v: regions[v].intersection(f).area)
        assigned.setdefault(best, []).append(f)
    parts = {v: unary_union(fs) for v, fs in assigned.items()}
    # fix disconnected slivers: keep each label's largest polygon, give the rest to the longest-edge neighbour
    for _ in range(50):
        changed = False
        for v in list(parts):
            geom = parts[v]
            if geom.geom_type == 'Polygon':
                continue
            pieces = sorted(iter_polygons(geom), key=lambda p: p.area, reverse=True)
            parts[v] = pieces[0]
            for piece in pieces[1:]:
                others = [u for u in parts if u != v]
                target = max(others, key=lambda u: parts[u].boundary.intersection(piece.boundary).length)
                parts[target] = unary_union([parts[target], piece])
                log.append({'sliver_m2': round(piece.area*m_per_px*m_per_px, 3), 'from': v, 'to': target})
                changed = True
        if not changed:
            break
    # floor on the vector parts (the raster floor ran first; areas can differ by a few pixels at the edges)
    for _ in range(50):
        areas = {v: p.area*m_per_px*m_per_px for v, p in parts.items()}
        small = [v for v, a in sorted(areas.items(), key=lambda kv: kv[1]) if a < design['min_part_m2']]
        if not small or len(parts) <= 1:
            break
        v = small[0]
        others = [u for u in parts if u != v]
        target = max(others, key=lambda u: parts[u].boundary.intersection(parts[v].boundary).length)
        parts[target] = unary_union([parts[target], parts.pop(v)])
        log.append({'merged': v, 'into': target, 'area_m2': round(areas[v], 2), 'reason': 'vector part below floor'})
    for v in list(parts):
        if parts[v].geom_type != 'Polygon':
            parts[v] = max(iter_polygons(parts[v]), key=lambda p: p.area)
            log.append({'warning': f'part {v} still multipolygon after fixes; kept largest piece'})
    return {i: parts[v] for i, v in enumerate(sorted(parts), start=1)}, log


def conservation(poly: Polygon, parts, m_per_px):
    union = unary_union(list(parts.values()))
    sym = union.symmetric_difference(poly)
    total = sum(p.area for p in parts.values())
    return {'original_m2': round(poly.area*m_per_px*m_per_px, 3), 'parts_sum_m2': round(total*m_per_px*m_per_px, 3),
            'abs_diff_m2': round(abs(total-poly.area)*m_per_px*m_per_px, 6), 'rel_diff': round(abs(total-poly.area)/max(poly.area, 1e-9), 9),
            'symmetric_difference_px2': round(float(sym.area), 6), 'symmetric_difference_m2': round(float(sym.area)*m_per_px*m_per_px, 6),
            'pairwise_overlap_px2': round(float(sum(parts[a].intersection(parts[b]).area for a in parts for b in parts if a < b)), 6)}


def children_payload(parts, variant, m_per_px, extra):
    children = []
    for i, (v, poly) in enumerate(sorted(parts.items()), start=1):
        ring = [[round(float(x), 3), round(float(y), 3)] for x, y in poly.exterior.coords[:-1]]
        holes = [[[round(float(x), 3), round(float(y), 3)] for x, y in h.coords[:-1]] for h in poly.interiors]
        children.append({'key': f'{variant}-{i}', 'label': f'Crown hypothesis {i} ({variant} variant; not reviewed)', 'variant': variant,
                         'area_m2': round(poly.area*m_per_px*m_per_px, 2), 'area_px': round(poly.area, 1), 'vertices': len(ring),
                         'holes': len(holes), 'outline_pixels': {'ring': ring, 'holes': holes}, **extra.get(v, {})})
    return children


# ----------------------------------------------------------------------------------------------- rendering
def _draw_parts(panel, parts, scale, ox, oy, alpha=0.42):
    overlay = panel.copy()
    for i, (_v, poly) in enumerate(sorted(parts.items())):
        colour = PALETTE[i % len(PALETTE)]
        for p in iter_polygons(poly):
            ext = ((np.asarray(p.exterior.coords[:-1])-[ox, oy]+0.5)*scale).astype(np.int32).reshape(-1, 1, 2)
            cv2.fillPoly(overlay, [ext], colour)
            for h in p.interiors:
                hp = ((np.asarray(h.coords[:-1])-[ox, oy]+0.5)*scale).astype(np.int32).reshape(-1, 1, 2)
                cv2.fillPoly(overlay, [hp], (0, 0, 0))
    out = cv2.addWeighted(panel, 1-alpha, overlay, alpha, 0)
    for i, (v, poly) in enumerate(sorted(parts.items())):
        colour = PALETTE[i % len(PALETTE)]
        for p in iter_polygons(poly):
            ext = ((np.asarray(p.exterior.coords[:-1])-[ox, oy]+0.5)*scale).astype(np.int32).reshape(-1, 1, 2)
            cv2.polylines(out, [ext], True, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.polylines(out, [ext], True, colour, 1, cv2.LINE_AA)
            c = p.representative_point()
            cx, cy = int((c.x-ox+0.5)*scale), int((c.y-oy+0.5)*scale)
            cv2.putText(out, f'{v}', (cx-4, cy+4), cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(out, f'{v}', (cx-4, cy+4), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def _outline(panel, poly, scale, ox, oy, colour=(255, 255, 0)):
    out = panel.copy()
    ext = ((np.asarray(poly.exterior.coords[:-1])-[ox, oy]+0.5)*scale).astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(out, [ext], True, colour, 1, cv2.LINE_AA)
    for h in poly.interiors:
        hp = ((np.asarray(h.coords[:-1])-[ox, oy]+0.5)*scale).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(out, [hp], True, colour, 1, cv2.LINE_AA)
    return out


def _caption(panel, text):
    out = panel.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 16), (20, 20, 20), -1)
    cv2.putText(out, text, (4, 12), cv2.FONT_HERSHEY_SIMPLEX, .38, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def _scale_bar(panel, m_per_px, scale):
    width_m = panel.shape[1]*m_per_px/scale
    bar_m = 5.0 if width_m >= 12 else 2.0
    bar_px = int(round(bar_m/m_per_px*scale))
    h = panel.shape[0]
    out = panel.copy()
    cv2.rectangle(out, (8, h-14), (8+bar_px, h-10), (0, 0, 0), -1)
    cv2.rectangle(out, (9, h-13), (7+bar_px, h-11), (255, 255, 255), -1)
    cv2.putText(out, f'{bar_m:g} m', (8, h-18), cv2.FONT_HERSHEY_SIMPLEX, .38, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(out, f'{bar_m:g} m', (8, h-18), cv2.FONT_HERSHEY_SIMPLEX, .38, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def crop_rgb(arr, ox, oy, shape):
    """Crop with edge replication where the crop runs past the image."""
    h, w = arr.shape[:2]
    y0, y1 = max(oy, 0), min(oy+shape[0], h)
    x0, x1 = max(ox, 0), min(ox+shape[1], w)
    sub = arr[y0:y1, x0:x1]
    return np.ascontiguousarray(np.pad(sub, ((y0-oy, oy+shape[0]-y1), (x0-ox, ox+shape[1]-x1), (0, 0)), mode='edge'))


def render_object(rgb, poly, ox, oy, m_per_px, parts_shape, parts_texture, title, out_png, scale):
    base = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    raw = _scale_bar(base, m_per_px, scale)
    outline = _outline(base, poly, scale, ox, oy)
    shape_p = _draw_parts(base, parts_shape, scale, ox, oy)
    tex_p = _draw_parts(base, parts_texture, scale, ox, oy)
    panels = [_caption(raw, 'raw crop'), _caption(outline, 'original outline'),
              _caption(shape_p, f'shape split: {len(parts_shape)} part(s)'), _caption(tex_p, f'texture split: {len(parts_texture)} part(s)')]
    gap = np.full((panels[0].shape[0], 6, 3), 40, np.uint8)
    row = np.hstack([x for p in panels for x in (p, gap)][:-1])
    title_bar = np.full((22, row.shape[1], 3), 20, np.uint8)
    cv2.putText(title_bar, title, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1, cv2.LINE_AA)
    sheet = np.vstack([title_bar, row])
    Image.fromarray(sheet).save(out_png)
    return sheet


def contact_sheet(sheets, out_png, width=1600):
    rows = []
    for s in sheets:
        f = width/s.shape[1]
        rows.append(cv2.resize(s, (width, int(round(s.shape[0]*f))), interpolation=cv2.INTER_AREA))
        rows.append(np.full((8, width, 3), 60, np.uint8))
    Image.fromarray(np.vstack(rows[:-1])).save(out_png)


# ----------------------------------------------------------------------------------------------- driver
def run_object(root: Path, scene_id: str, object_id: str, out: Path, design, renders: Path):
    t0 = time.time()
    model, obj, base, image = load_object(root, scene_id, object_id)
    if obj is None:
        return {'scene': scene_id, 'object_id': object_id, 'error': 'object missing'}, None
    m_per_px = abs(float(model['pixel_to_meters'][0][0]))
    poly, valid = object_polygon(obj)
    pad = design['pad_px']
    minx, miny, maxx, maxy = poly.bounds
    # the crop may run past the image: an outline on the frame edge (x = width) needs the dividing lines to reach it
    ox, oy = int(np.floor(minx))-pad, int(np.floor(miny))-pad
    ex, ey = int(np.ceil(maxx))+pad, int(np.ceil(maxy))+pad
    crop_shape = (ey-oy+1, ex-ox+1)
    mask = rasterise(poly, ox, oy, crop_shape)
    rgb = crop_rgb(np.asarray(image), ox, oy, crop_shape)

    # shape variant
    shape_labels, dist, peaks, shape_info = shape_variant(mask, m_per_px, design)
    shape_labels, floor_log = enforce_floor(shape_labels, mask, m_per_px, design['min_part_m2'])
    shape_labels = relabel(shape_labels)
    shape_info['floor_merges'] = floor_log
    shape_info['raster_regions_after_floor'] = int(shape_labels.max())
    shape_full = tile_outside(shape_labels, mask)
    parts_shape, vlog_shape = vector_partition(poly, shape_full, ox, oy, m_per_px, design)
    final_shape = labels_from_parts(parts_shape, ox, oy, crop_shape, mask)
    shape_info['cuts'] = shape_cuts(final_shape, mask, dist)
    cons_shape = conservation(poly, parts_shape, m_per_px)
    peak_extra = {}
    for v in parts_shape:
        sub = final_shape == v
        peak_extra[v] = {'inscribed_radius_m': round(float(dist[sub].max()), 2) if sub.any() else None}

    # texture variant
    tex_labels, z_full, tex_info = texture_variant(rgb, mask, dist, m_per_px, design)
    tex_labels, tex_floor_log = enforce_floor(tex_labels, mask, m_per_px, design['min_part_m2'])
    tex_labels = relabel(tex_labels)
    tex_info['floor_merges'] = tex_floor_log
    tex_info['raster_regions_after_floor'] = int(tex_labels.max())
    tex_full = tile_outside(tex_labels, mask)
    parts_tex, vlog_tex = vector_partition(poly, tex_full, ox, oy, m_per_px, design)
    final_tex = labels_from_parts(parts_tex, ox, oy, crop_shape, mask)
    tex_info['cuts'] = texture_cut_confidence(final_tex, mask, z_full, dist, design) if len(parts_tex) > 1 else []
    cons_tex = conservation(poly, parts_tex, m_per_px)
    tex_extra = {}
    for v in parts_tex:
        sub = final_tex == v
        tex_extra[v] = {'mean_L_a_b_std': [round(float(x), 1) for x in (z_full[sub].mean(0)*np.array(tex_info['feature_sd'])+np.array(tex_info['feature_mean']))] if sub.any() else None}

    area_m2 = poly.area*m_per_px*m_per_px
    title = (f"{scene_id} {object_id} {obj['kind']} {obj.get('status')} {area_m2:.1f} m2 {m_per_px:.3f} m/px | "
             f"shape {len(parts_shape)} part(s), texture {len(parts_tex)} part(s) | x{design['render_scale']}")
    renders.mkdir(parents=True, exist_ok=True)
    sheet = render_object(rgb, poly, ox, oy, m_per_px, parts_shape, parts_tex, title,
                          renders/f'{scene_id}.{object_id}.png', design['render_scale'])
    edge = min(minx, miny, image.width-maxx, image.height-maxy)
    record = {
        'scene': scene_id, 'object_id': object_id, 'kind': obj['kind'], 'status_live': obj.get('status'), 'status_model': (base or {}).get('status'),
        'score': obj.get('score'), 'prompt': obj.get('prompt'), 'source': obj.get('source'), 'replaces': obj.get('replaces'),
        'vertices': len(obj['ring']), 'holes': len(obj.get('holes') or []), 'polygon_valid': valid, 'area_m2': round(area_m2, 2),
        'model_area_m2': (obj.get('model') or {}).get('area_m2'), 'mask_px': int(mask.sum()), 'm_per_px': round(m_per_px, 5),
        'frame_edge_px': round(float(edge), 1), 'crop_origin_px': [ox, oy], 'crop_shape_px': list(crop_shape),
        'design': design,
        'variants': {
            'shape': {'n_parts': len(parts_shape), 'split': len(parts_shape) > 1, 'part_areas_m2': [round(p.area*m_per_px*m_per_px, 2) for _, p in sorted(parts_shape.items())],
                      'diagnostics': shape_info, 'area_conservation': cons_shape, 'vector_log': vlog_shape,
                      'parts': children_payload(parts_shape, 'shape', m_per_px, peak_extra)},
            'texture': {'n_parts': len(parts_tex), 'split': len(parts_tex) > 1, 'part_areas_m2': [round(p.area*m_per_px*m_per_px, 2) for _, p in sorted(parts_tex.items())],
                        'diagnostics': tex_info, 'area_conservation': cons_tex, 'vector_log': vlog_tex,
                        'parts': children_payload(parts_tex, 'texture', m_per_px, tex_extra)},
        },
        'render': str(renders/f'{scene_id}.{object_id}.png'), 'seconds': round(time.time()-t0, 2),
        'applied': False,
        'note': 'Sidecar hypothesis only. Children are in the split API child shape (key, label, outline_pixels) for preview_object_split; '
                'nothing here was previewed or applied, and no scene file was written.',
    }
    return record, sheet


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--target', action='append', required=True, help='scene_id:object_id')
    ap.add_argument('--out', required=True)
    ap.add_argument('--root', default='.')
    ap.add_argument('--min-crown-radius-m', type=float, default=None)
    ap.add_argument('--min-part-m2', type=float, default=None)
    args = ap.parse_args()
    root, out = Path(args.root), Path(args.out)
    design = json.loads(json.dumps(DESIGN))
    if args.min_crown_radius_m is not None:
        design['min_crown_radius_m'] = args.min_crown_radius_m
    if args.min_part_m2 is not None:
        design['min_part_m2'] = args.min_part_m2
    out.mkdir(parents=True, exist_ok=True)
    renders = out/'renders'
    records, sheets = [], []
    for t in args.target:
        scene_id, object_id = t.split(':', 1)
        record, sheet = run_object(root, scene_id, object_id, out, design, renders)
        records.append(record)
        if sheet is not None:
            sheets.append(sheet)
        json.dump(record, open(out/f'{scene_id}.{object_id}.json', 'w'), indent=1)
        if 'error' in record:
            print(scene_id, object_id, record['error'])
            continue
        s, x = record['variants']['shape'], record['variants']['texture']
        print(f"{scene_id} {object_id} {record['kind']} {record['area_m2']} m2 | shape {s['n_parts']} {s['part_areas_m2']} "
              f"peaks {s['diagnostics'].get('peaks_detected')}->{s['diagnostics'].get('peaks_after_merge')} cuts {[(c['saddle_m'], c['depth_ratio']) for c in s['diagnostics']['cuts']]} | "
              f"texture {x['n_parts']} {x['part_areas_m2']} sep {[(c['feature_separation'], c['L_share_of_gap']) for c in x['diagnostics']['cuts']]} | "
              f"cons {s['area_conservation']['rel_diff']:.1e}/{x['area_conservation']['rel_diff']:.1e} | {record['seconds']}s")
    if sheets:
        contact_sheet(sheets, renders/'contact-sheet.png')
    summary = {'design': design, 'objects': [{k: r.get(k) for k in ('scene', 'object_id', 'kind', 'status_live', 'area_m2', 'm_per_px')} |
                                             ({'shape_parts': r['variants']['shape']['n_parts'], 'shape_part_areas_m2': r['variants']['shape']['part_areas_m2'],
                                               'shape_cuts': r['variants']['shape']['diagnostics']['cuts'],
                                               'shape_area_rel_diff': r['variants']['shape']['area_conservation']['rel_diff'],
                                               'shape_symdiff_m2': r['variants']['shape']['area_conservation']['symmetric_difference_m2'],
                                               'texture_parts': r['variants']['texture']['n_parts'], 'texture_part_areas_m2': r['variants']['texture']['part_areas_m2'],
                                               'texture_cuts': r['variants']['texture']['diagnostics']['cuts'],
                                               'texture_area_rel_diff': r['variants']['texture']['area_conservation']['rel_diff'],
                                               'texture_symdiff_m2': r['variants']['texture']['area_conservation']['symmetric_difference_m2'],
                                               'seconds': r['seconds']} if 'error' not in r else {'error': r['error']})
                                             for r in records]}
    json.dump(summary, open(out/'summary.json', 'w'), indent=1)


if __name__ == '__main__':
    main()
