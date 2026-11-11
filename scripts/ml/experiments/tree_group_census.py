"""Read-only census of tree / vegetation ("canopy") grouping in the 53 windows and the review set (tree-group-split-2026-10-03).

Reviewer observation (2026-10-03 22:10 AEST): win-2fbb386c3b235ff4 object b1e978e7c391f56b and lot-29358 object 7c29c8c28d31c70f are
"a vegetation group [that] could be split based off the tree pattern differences". This script measures how canopy objects are shaped
and grouped today, so a decision card can say whether the detector / post-processing should propose the split or whether the editor's
object-split API stays the only route. Every figure is a count or a geometry; nothing is accuracy; nothing is promoted; nothing under
data/scenes is written.

Layers: "model" = data/scenes/<id>/latest.json objects (the extraction snapshot the editor shows as the model layer); "review" =
data/scenes/<id>/review.json objects (the reviewer's layer). Groups: windows = win-* and lot-* (53); review_set = house-* and
roof_training_250-* that carry a review.json (99 at the time of writing). Image path = <scene>/<image_sha256>.png (the way the 2 October
QA renders and scripts/test-object-split-api.mjs find it).

Per canopy object: area m2, vertices, convexity (area / convex hull area), lobes by morphological opening (buffer -r then +r in metres,
r = 1.0 / 1.5 / 2.0; the 1.5 m figure is the headline), significant concavities (components of hull - polygon >= 1 m2), the canopy
merge lineage (`replaces`: fragments merge_canopy_fragments folded into this container; scene_extract.py:89-141), the reviewer's status,
distance to the frame edge. Aggregates by layer x group x kind x area band (<20, 20-60, 60-150, >=150 m2) and per scene; the 20 largest
tree objects; every figure for the two named objects; and an inference-free image-texture reading on the two named objects: k-means
(k = 2..4, numpy) on colour (RGB, ExG) plus a local-variance texture channel inside the polygon, with cluster areas and a spatial
separation reading (adjacency mixing ratio against a random-label null, connected components per cluster).

Run from the repo root: scripts/ml/.venv/bin/python -m scripts.ml.experiments.tree_group_census [--skip-texture]
Outputs ONLY under data/reviews/eyes/experiments/tree-group-split-2026-10-03/: census.json, census.md, objects.jsonl, *-texture.png.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely import make_valid
from shapely.affinity import affine_transform
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[3]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCENES = ROOT/'data/scenes'
OUT_DIR = ROOT/'data/reviews/eyes/experiments/tree-group-split-2026-10-03'
WINDOW_PREFIXES = ('win-', 'lot-')
REVIEW_SET_PREFIXES = ('house-', 'roof_training_250-')
CANOPY_KINDS = ('tree', 'vegetation')
LIVE_EXCLUDED = ('rejected', 'superseded')
BANDS = (('<20', 0., 20.), ('20-60', 20., 60.), ('60-150', 60., 150.), ('>=150', 150., math.inf))
OPEN_RADII_M = (1.0, 1.5, 2.0)
HEADLINE_RADIUS_M = 1.5
CONCAVITY_MIN_M2 = 1.0
LOBE_MIN_M2 = 1.0           # opening pieces smaller than this are slivers of the buffer, not lobes
REL_RADIUS = (0.3, 0.5, 4.0)  # scale-relative opening radius: clamp(0.3 * sqrt(area_m2), 0.5 m, 4.0 m)
SWEEP_RADII_M = [round(0.2+0.1*i, 1) for i in range(29)]   # 0.2 .. 3.0 m, named objects only
SMOOTH_SIGMA_M = 1.0        # region-scale variant: Gaussian-smooth the channels before k-means
NAMED = (('win-2fbb386c3b235ff4', 'b1e978e7c391f56b'), ('lot-29358', '7c29c8c28d31c70f'))
TEXTURE_WINDOW_M = 1.0      # local-variance window (odd pixel count nearest this many metres, clipped to 5..21 px)
KMEANS_KS = (2, 3, 4)
COMPONENT_MIN_PX = 16       # cluster components under this many pixels count as speckle, not regions
CONTROLS = 6                # confirmed single-lobe trees (15..250 m2) given the same texture reading as a baseline


def load_json(path):
    with open(path) as handle:
        return json.load(handle)


def population(root=SCENES):
    """(scene_dir, group) for the windows and every review-set scene with a review.json; sorted, read-only."""
    rows = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not (path/'latest.json').exists():
            continue
        if path.name.startswith(WINDOW_PREFIXES):
            rows.append((path, 'windows'))
        elif path.name.startswith(REVIEW_SET_PREFIXES) and (path/'review.json').exists():
            rows.append((path, 'review_set'))
    return rows


def affine_params(pixel_to_meters):
    m = np.asarray(pixel_to_meters, dtype=float)      # 3x2: [x y 1] @ m = [X Y]
    return [m[0, 0], m[1, 0], m[0, 1], m[1, 1], m[2, 0], m[2, 1]]


def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type == 'Polygon':
        return geometry
    parts = [g for g in getattr(geometry, 'geoms', []) if g.geom_type in ('Polygon', 'MultiPolygon') and not g.is_empty]
    if not parts:
        return None
    merged = unary_union(parts)
    return merged if not merged.is_empty else None


def object_polygon(obj):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None
    try:
        polygon = Polygon(ring, [h for h in (obj.get('holes') or []) if len(h) >= 3])
    except (ValueError, TypeError):
        return None
    if not polygon.is_valid:
        polygon = polygonal(make_valid(polygon))
    return polygon if polygon is not None and polygon.area > 0 else None


def parts_of(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == 'Polygon':
        return [geometry]
    return [g for g in getattr(geometry, 'geoms', []) if g.geom_type == 'Polygon' and not g.is_empty]


def band_of(area_m2):
    for name, lo, hi in BANDS:
        if lo <= area_m2 < hi:
            return name
    return BANDS[-1][0]


def frame_distance_px(polygon, width, height):
    minx, miny, maxx, maxy = polygon.bounds
    return float(min(minx, miny, width-maxx, height-maxy))


def lobes(metric, radius_m):
    """Components after a morphological opening (buffer -r, +r) in metres; (count, areas sorted desc)."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        opened = metric.buffer(-radius_m).buffer(radius_m)
    areas = sorted((p.area for p in parts_of(opened) if p.area >= LOBE_MIN_M2), reverse=True)
    return len(areas), [round(a, 2) for a in areas]


def relative_radius(area_m2):
    return float(min(REL_RADIUS[2], max(REL_RADIUS[1], REL_RADIUS[0]*math.sqrt(max(area_m2, 0.)))))


def split_radius_sweep(metric):
    """For each opening radius: lobe count; plus the first radius that yields >= 2 lobes (None when none does)."""
    counts = {}
    first = None
    for radius in SWEEP_RADII_M:
        count, _ = lobes(metric, radius)
        counts[f'{radius:.1f}'] = count
        if first is None and count >= 2:
            first = radius
    return {'first_split_radius_m': first, 'lobes_by_radius_m': counts}


def concavities(metric):
    hull = metric.convex_hull
    pieces = [p.area for p in parts_of(hull.difference(metric)) if p.area >= CONCAVITY_MIN_M2]
    return len(pieces), [round(a, 2) for a in sorted(pieces, reverse=True)]


def measure(obj, polygon, params, pixel_area, width, height, review_status=None, absorbed_by=None):
    metric = affine_transform(polygon, params)
    hull = metric.convex_hull
    area_m2 = metric.area
    perimeter = metric.length
    row = {
        'kind': obj.get('kind'), 'status': obj.get('status'), 'review_status': review_status,
        'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'source': obj.get('source'), 'prompt': obj.get('prompt'),
        'score': obj.get('score'), 'area_m2': round(area_m2, 2), 'band': band_of(area_m2),
        'vertices': len(obj.get('ring') or []), 'hole_count': len([h for h in (obj.get('holes') or []) if len(h) >= 3]),
        'hole_vertices': sum(len(h) for h in (obj.get('holes') or []) if len(h) >= 3),
        'convexity': round(area_m2/hull.area, 4) if hull.area > 0 else None,
        'compactness': round(4*math.pi*area_m2/(perimeter*perimeter), 4) if perimeter > 0 else None,
        'bbox_m': [round(v, 2) for v in (metric.bounds[2]-metric.bounds[0], metric.bounds[3]-metric.bounds[1])],
        'frame_distance_px': round(frame_distance_px(polygon, width, height), 1),
        'replaces_count': len(obj.get('replaces') or []), 'absorbed_by': absorbed_by,
        'has_derived_from': bool(obj.get('derived_from')), 'has_replacement_ids': bool(obj.get('replacement_ids')),
        'group_id': obj.get('group_id'), 'visibility': obj.get('visibility'), 'clipped_by_frame': bool(obj.get('clipped_by_frame')),
        'review_issues': len(obj.get('review_issues') or []), 'has_notes': bool(obj.get('notes')),
        'model_area_m2': (obj.get('model') or {}).get('area_m2'),
    }
    count, pieces = concavities(metric)
    row['concavities_ge1m2'] = count
    row['concavity_areas_m2'] = pieces[:12]
    for radius in OPEN_RADII_M:
        count, areas = lobes(metric, radius)
        key = f'lobes_{radius:.1f}m'.replace('.', 'p')
        row[key] = count
        if radius == HEADLINE_RADIUS_M:
            row['lobe_areas_1p5m_m2'] = areas[:12]
            row['largest_lobe_share_1p5m'] = round(areas[0]/sum(areas), 3) if areas else None
    rel = relative_radius(area_m2)
    row['rel_radius_m'] = round(rel, 2)
    row['lobes_rel'], row['lobe_areas_rel_m2'] = (lambda c, a: (c, a[:12]))(*lobes(metric, rel))
    return row


def layer_rows(scene_id, group, layer, objects, meta, review_status_by_id):
    params = affine_params(meta['pixel_to_meters'])
    pixel_area = abs(float(np.linalg.det(np.asarray(meta['pixel_to_meters'], dtype=float)[:2])))
    width, height = meta.get('image_width') or 1024, meta.get('image_height') or 1024
    absorbed = {}
    for obj in objects:
        for fragment in obj.get('replaces') or []:
            absorbed[fragment] = obj['id']
    rows, skipped = [], 0
    for obj in objects:
        if obj.get('kind') not in CANOPY_KINDS:
            continue
        polygon = object_polygon(obj)
        if polygon is None:
            skipped += 1
            continue
        row = measure(obj, polygon, params, pixel_area, width, height,
                      review_status=review_status_by_id.get(obj['id']) if layer == 'model' else obj.get('status'),
                      absorbed_by=absorbed.get(obj['id']))
        row.update({'scene': scene_id, 'group': group, 'layer': layer, 'id': obj['id'],
                    'meters_per_pixel': round(math.sqrt(pixel_area), 4)})
        rows.append(row)
    return rows, skipped


def summarise(rows):
    """Count / geometry aggregate for a bucket of rows."""
    if not rows:
        return {'n': 0}
    lobes_hist = Counter(min(r['lobes_1p5m'], 4) for r in rows)
    convexity = [r['convexity'] for r in rows if r['convexity'] is not None]
    out = {
        'n': len(rows),
        'area_m2_total': round(sum(r['area_m2'] for r in rows), 1),
        'area_m2_median': round(float(np.median([r['area_m2'] for r in rows])), 2),
        'vertices_median': float(np.median([r['vertices'] for r in rows])),
        'vertices_max': max(r['vertices'] for r in rows),
        'convexity_median': round(float(np.median(convexity)), 3) if convexity else None,
        'convexity_lt_0p7': sum(1 for c in convexity if c < .7),
        'lobes_1p5m_hist': {('4+' if k == 4 else str(k)): lobes_hist[k] for k in sorted(lobes_hist)},
        'lobes_1p5m_ge2': sum(1 for r in rows if r['lobes_1p5m'] >= 2),
        'lobes_1p0m_ge2': sum(1 for r in rows if r['lobes_1p0m'] >= 2),
        'lobes_2p0m_ge2': sum(1 for r in rows if r['lobes_2p0m'] >= 2),
        'lobes_rel_ge2': sum(1 for r in rows if r['lobes_rel'] >= 2),
        'concavities_ge1m2_median': float(np.median([r['concavities_ge1m2'] for r in rows])),
        'concavities_ge2': sum(1 for r in rows if r['concavities_ge1m2'] >= 2),
        'with_replaces': sum(1 for r in rows if r['replaces_count'] > 0),
        'replaces_total': sum(r['replaces_count'] for r in rows),
        'replaces_max': max(r['replaces_count'] for r in rows),
        'with_holes': sum(1 for r in rows if r['hole_count'] > 0),
        'touching_frame': sum(1 for r in rows if r['frame_distance_px'] <= 1.0),
        'status': dict(Counter(r['status'] for r in rows)),
        'review_status': dict(Counter(str(r['review_status']) for r in rows)),
    }
    return out


def aggregate(rows):
    live = [r for r in rows if r['status'] not in LIVE_EXCLUDED]
    out = {'excluded_not_live': len(rows)-len(live), 'by_layer_group_kind_band': {}, 'by_layer_group_kind': {}, 'by_layer_group': {}}
    for (layer, group, kind, band), bucket in _groupby(live, lambda r: (r['layer'], r['group'], r['kind'], r['band'])).items():
        out['by_layer_group_kind_band'][f'{layer}|{group}|{kind}|{band}'] = summarise(bucket)
    for (layer, group, kind), bucket in _groupby(live, lambda r: (r['layer'], r['group'], r['kind'])).items():
        out['by_layer_group_kind'][f'{layer}|{group}|{kind}'] = summarise(bucket)
    for (layer, group), bucket in _groupby(live, lambda r: (r['layer'], r['group'])).items():
        out['by_layer_group'][f'{layer}|{group}'] = summarise(bucket)
    return out


def _groupby(rows, key):
    buckets = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    return dict(sorted(buckets.items(), key=lambda kv: str(kv[0])))


def per_scene(rows):
    out = {}
    live = [r for r in rows if r['status'] not in LIVE_EXCLUDED]
    for (scene, layer), bucket in _groupby(live, lambda r: (r['scene'], r['layer'])).items():
        trees = [r for r in bucket if r['kind'] == 'tree']
        out.setdefault(scene, {'group': bucket[0]['group']})[layer] = {
            'tree': len(trees), 'vegetation': len(bucket)-len(trees),
            'tree_ge150': sum(1 for r in trees if r['band'] == '>=150'), 'tree_60_150': sum(1 for r in trees if r['band'] == '60-150'),
            'tree_lobes_1p5m_ge2': sum(1 for r in trees if r['lobes_1p5m'] >= 2),
            'tree_ge60_lobes_ge2': sum(1 for r in trees if r['area_m2'] >= 60 and r['lobes_1p5m'] >= 2),
            'tree_lobes_rel_ge2': sum(1 for r in trees if r['lobes_rel'] >= 2),
            'tree_concav_ge2': sum(1 for r in trees if r['concavities_ge1m2'] >= 2),
            'canopy_with_replaces': sum(1 for r in bucket if r['replaces_count'] > 0),
            'replaces_total': sum(r['replaces_count'] for r in bucket),
            'needs_adjustment': sum(1 for r in bucket if r['status'] == 'needs_adjustment'),
            'confirmed': sum(1 for r in bucket if r['status'] == 'confirmed'),
            'largest_tree_m2': round(max((r['area_m2'] for r in trees), default=0.), 1),
        }
    return out


# ---------------------------------------------------------------- texture reading (no inference) -----------------------------------

def rasterise(polygon, size):
    mask = Image.new('L', size)
    draw = ImageDraw.Draw(mask)
    for part in parts_of(polygon):
        draw.polygon([tuple(p) for p in part.exterior.coords], fill=255)
        for hole in part.interiors:
            draw.polygon([tuple(p) for p in hole.coords], fill=0)
    return np.asarray(mask) > 0


def local_std(channel, window, mask):
    """Local standard deviation over the pixels of `mask` only (normalised box filter), so the polygon rim is not read against the ground."""
    weight = ndimage.uniform_filter(mask.astype(float), window, mode='constant')
    weight = np.where(weight > 1e-6, weight, 1.)
    mean = ndimage.uniform_filter(channel*mask, window, mode='constant')/weight
    mean_sq = ndimage.uniform_filter(channel*channel*mask, window, mode='constant')/weight
    return np.sqrt(np.clip(mean_sq-mean*mean, 0, None))


def masked_gaussian(channel, sigma, mask):
    weight = ndimage.gaussian_filter(mask.astype(float), sigma, mode='constant')
    weight = np.where(weight > 1e-6, weight, 1.)
    return ndimage.gaussian_filter(channel*mask, sigma, mode='constant')/weight


def kmeans(features, k, iters=40, restarts=6, seed=0):
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(restarts):
        centers = [features[rng.integers(len(features))]]
        for _ in range(1, k):
            d2 = np.min(((features[:, None, :]-np.asarray(centers)[None])**2).sum(-1), axis=1)
            centers.append(features[rng.choice(len(features), p=d2/d2.sum())] if d2.sum() > 0 else features[rng.integers(len(features))])
        centers = np.asarray(centers, dtype=float)
        for _ in range(iters):
            labels = np.argmin(((features[:, None, :]-centers[None])**2).sum(-1), axis=1)
            updated = np.array([features[labels == j].mean(0) if (labels == j).any() else centers[j] for j in range(k)])
            if np.allclose(updated, centers):
                break
            centers = updated
        inertia = float(((features-centers[labels])**2).sum())
        if best is None or inertia < best[0]:
            best = (inertia, labels, centers)
    return best


def spatial_separation(label_image, inside, k, pixel_area):
    """Adjacency mixing ratio (observed differing-label neighbour pairs / random-label expectation) and components per cluster."""
    labels = np.where(inside, label_image, -1)
    pairs_total = pairs_diff = 0
    for shift in ((0, 1), (1, 0)):
        a = labels[:labels.shape[0]-shift[0], :labels.shape[1]-shift[1]]
        b = labels[shift[0]:, shift[1]:]
        valid = (a >= 0) & (b >= 0)
        pairs_total += int(valid.sum())
        pairs_diff += int((valid & (a != b)).sum())
    shares = np.array([(labels == j).sum() for j in range(k)], dtype=float)
    shares /= shares.sum()
    expected = float(1.-(shares**2).sum())
    observed = pairs_diff/pairs_total if pairs_total else None
    clusters = []
    for j in range(k):
        member = labels == j
        components, n = ndimage.label(member, structure=np.ones((3, 3)))
        sizes = np.bincount(components.ravel())[1:] if n else np.array([])
        significant = sizes[sizes >= COMPONENT_MIN_PX]
        ys, xs = np.nonzero(member)
        clusters.append({
            'pixels': int(member.sum()), 'area_m2': round(float(member.sum())*pixel_area, 2), 'share': round(float(shares[j]), 3),
            'components_ge16px': int(len(significant)), 'components_all': int(n),
            'largest_component_share': round(float(sizes.max()/member.sum()), 3) if member.sum() and n else None,
            'centroid_px': [round(float(xs.mean()), 1), round(float(ys.mean()), 1)] if member.any() else None,
        })
    return {'adjacency_mixing_observed': round(observed, 4) if observed is not None else None,
            'adjacency_mixing_expected_random': round(expected, 4),
            'adjacency_mixing_ratio': round(observed/expected, 3) if observed is not None and expected > 0 else None,
            'clusters': clusters}


def texture_reading(scene_dir, meta, obj, polygon, out_dir):
    image_path = scene_dir/f"{meta['image_sha256']}.png"
    image = np.asarray(Image.open(image_path).convert('RGB'), dtype=float)
    inside = rasterise(polygon, (image.shape[1], image.shape[0]))
    pixel_area = abs(float(np.linalg.det(np.asarray(meta['pixel_to_meters'], dtype=float)[:2])))
    mpp = math.sqrt(pixel_area)
    window = int(min(21, max(5, round(TEXTURE_WINDOW_M/mpp)//2*2+1)))
    rgb = image/255.
    lum = 0.299*rgb[..., 0]+0.587*rgb[..., 1]+0.114*rgb[..., 2]
    exg = (2*rgb[..., 1]-rgb[..., 0]-rgb[..., 2])/(rgb.sum(-1)+1e-3)
    texture = local_std(lum, window, inside)
    texture_coarse = local_std(lum, window*2+1, inside)
    sigma_px = SMOOTH_SIGMA_M/mpp
    smooth = lambda c: masked_gaussian(c, sigma_px, inside)   # noqa: E731
    channels = {'R': rgb[..., 0], 'G': rgb[..., 1], 'B': rgb[..., 2], 'ExG': exg, 'lum': lum,
                f'std{window}': texture, f'std{window*2+1}': texture_coarse,
                'sR': smooth(rgb[..., 0]), 'sG': smooth(rgb[..., 1]), 'sB': smooth(rgb[..., 2]), 'sExG': smooth(exg),
                f'sstd{window}': smooth(texture)}
    stacked = np.stack([channels[c][inside] for c in channels], axis=1)
    mean, std = stacked.mean(0), stacked.std(0)+1e-6
    z = (stacked-mean)/std
    names = list(channels)
    colour_idx = [names.index(c) for c in ('R', 'G', 'B', 'ExG')]
    texture_idx = [names.index(c) for c in (f'std{window}', f'std{window*2+1}')]
    smooth_idx = [names.index(c) for c in ('sR', 'sG', 'sB', 'sExG', f'sstd{window}')]
    variants = {'colour_only': colour_idx, 'colour_plus_texture': colour_idx+texture_idx, 'texture_only': texture_idx+[names.index('lum')],
                'smoothed_1m_colour_plus_texture': smooth_idx}
    ys, xs = np.nonzero(inside)
    result = {
        'image': str(image_path.relative_to(ROOT)), 'meters_per_pixel': round(mpp, 4), 'texture_window_px': window,
        'texture_window_m': round(window*mpp, 2), 'smooth_sigma_px': round(sigma_px, 2), 'pixels_inside': int(inside.sum()), 'area_m2': round(float(inside.sum())*pixel_area, 2),
        'channel_stats_inside': {c: {'mean': round(float(channels[c][inside].mean()), 4), 'std': round(float(channels[c][inside].std()), 4),
                                     'p10': round(float(np.percentile(channels[c][inside], 10)), 4),
                                     'p90': round(float(np.percentile(channels[c][inside], 90)), 4)} for c in names},
        'variants': {},
    }
    renders = []
    for variant, idx in variants.items():
        features = z[:, idx]
        total = float(((features-features.mean(0))**2).sum())
        result['variants'][variant] = {'channels': [names[i] for i in idx], 'k': {}}
        for k in KMEANS_KS:
            inertia, labels, centers = kmeans(features, k)
            label_image = np.full(inside.shape, -1, dtype=int)
            label_image[ys, xs] = labels
            separation = spatial_separation(label_image, inside, k, pixel_area)
            # order clusters by size so the report reads the same each run
            order = np.argsort([-c['pixels'] for c in separation['clusters']])
            means = [{names[i]: round(float(centers[j][n]*std[i]+mean[i]), 4) for n, i in enumerate(idx)} for j in order]
            entry = {'inertia_share_of_total': round(inertia/total, 4) if total else None,
                     'clusters': [separation['clusters'][j] | {'channel_means': means[n]} for n, j in enumerate(order)],
                     'adjacency_mixing_observed': separation['adjacency_mixing_observed'],
                     'adjacency_mixing_expected_random': separation['adjacency_mixing_expected_random'],
                     'adjacency_mixing_ratio': separation['adjacency_mixing_ratio']}
            entry['spatially_separated'] = bool(entry['adjacency_mixing_ratio'] is not None and entry['adjacency_mixing_ratio'] <= .4
                                                and all((c['largest_component_share'] or 0) >= .6 for c in entry['clusters']))
            result['variants'][variant]['k'][str(k)] = entry
            if variant == 'colour_plus_texture' or (variant.startswith('smoothed') and k <= 3):
                renders.append((f'k={k} {"smoothed 1 m" if variant.startswith("smoothed") else "colour+texture"}', k, label_image.copy()))
    render_path = out_dir/f"{scene_dir.name}-{obj['id']}-texture.png"
    render(image, inside, polygon, renders, texture, render_path)
    result['render'] = str(render_path.relative_to(ROOT))
    return result


PALETTE = [(230, 70, 70), (70, 120, 230), (240, 200, 40), (60, 200, 120)]


def render(image, inside, polygon, renders, texture, path):
    minx, miny, maxx, maxy = polygon.bounds
    pad = 24
    x0, y0 = max(0, int(minx)-pad), max(0, int(miny)-pad)
    x1, y1 = min(image.shape[1], int(maxx)+pad), min(image.shape[0], int(maxy)+pad)
    crop = image[y0:y1, x0:x1].astype(np.uint8)
    panels = []
    base = Image.fromarray(crop).convert('RGB')
    draw = ImageDraw.Draw(base)
    for part in parts_of(polygon):
        draw.line([(x-x0, y-y0) for x, y in part.exterior.coords], fill=(255, 255, 0), width=2)
    panels.append(('image + outline', base))
    tex = texture[y0:y1, x0:x1]
    tex = np.clip(tex/(np.percentile(tex[inside[y0:y1, x0:x1]], 98)+1e-6), 0, 1)
    tex_img = Image.fromarray((tex*255).astype(np.uint8)).convert('RGB')
    panels.append(('local std (texture)', tex_img))
    for title, k, label_image in renders:
        overlay = crop.copy()
        sub = label_image[y0:y1, x0:x1]
        for j in range(k):
            overlay[sub == j] = (0.35*overlay[sub == j]+0.65*np.array(PALETTE[j])).astype(np.uint8)
        panels.append((title, Image.fromarray(overlay)))
    scale = max(1, int(360/max(1, crop.shape[0])))
    width = sum(p.width*scale for _, p in panels)+8*(len(panels)-1)
    height = max(p.height*scale for _, p in panels)+16
    sheet = Image.new('RGB', (width, height), (20, 20, 20))
    x = 0
    for title, panel in panels:
        resized = panel.resize((panel.width*scale, panel.height*scale), Image.NEAREST)
        sheet.paste(resized, (x, 16))
        ImageDraw.Draw(sheet).text((x+2, 2), title, fill=(255, 255, 255))
        x += resized.width+8
    sheet.save(path)


# ---------------------------------------------------------------- report ---------------------------------------------------------------

def band_table(agg, layer, group, kind):
    lines = ['| band | n | median m2 | median vertices | median convexity | convexity<0.7 | lobes(1.5 m)>=2 | lobes 1/2/3/4+ | lobes(rel)>=2 | concav>=2 | with replaces | replaces total | needs_adjustment | confirmed |',
             '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for name, _, _ in BANDS:
        s = agg['by_layer_group_kind_band'].get(f'{layer}|{group}|{kind}|{name}', {'n': 0})
        if s['n'] == 0:
            lines.append(f'| {name} | 0 | | | | | | | | | | | | |')
            continue
        hist = s['lobes_1p5m_hist']
        hist_text = '/'.join(str(hist.get(k, 0)) for k in ('1', '2', '3', '4+'))+(f" (0 lobes: {hist['0']})" if hist.get('0') else '')
        st = s['review_status'] if layer == 'model' else s['status']
        lines.append(f"| {name} | {s['n']} | {s['area_m2_median']} | {s['vertices_median']:.0f} | {s['convexity_median']} | {s['convexity_lt_0p7']} | "
                     f"{s['lobes_1p5m_ge2']} | {hist_text} | {s['lobes_rel_ge2']} | {s['concavities_ge2']} | {s['with_replaces']} | {s['replaces_total']} | "
                     f"{st.get('needs_adjustment', 0)} | {st.get('confirmed', 0)} |")
    return lines


def write_markdown(census, path):
    pop = census['population']
    agg = census['aggregates']
    lines = [
        '# Tree / vegetation grouping census (tree-group-split-2026-10-03)', '',
        f"Generated {census['generated_at']} by `scripts/ml/experiments/tree_group_census.py` (sha256 {CODE_SHA256[:12]}). Read-only on "
        'data/scenes. Every figure is a count or a geometry, never accuracy; nothing is promoted.', '',
        '## Population', '',
        f"- Windows (win-*, lot-*): {pop['windows']} scenes; review set (house-*, roof_training_250-*) with a review.json: {pop['review_set']} scenes.",
        f"- Canopy objects measured: {pop['rows_total']} rows ({pop['rows_by_layer']}); {pop['skipped_invalid']} skipped (ring < 3 points or empty after make_valid); "
        f"{agg['excluded_not_live']} rejected/superseded rows excluded from the aggregates.",
        f"- Layers: model = latest.json, review = review.json. Lobes = components of the polygon after buffer -1.5 m / +1.5 m (metres via pixel_to_meters), "
        f"pieces >= {LOBE_MIN_M2} m2. Concavities = components of (convex hull - polygon) >= {CONCAVITY_MIN_M2} m2. Convexity = area / hull area.", '',
        '## Headline counts (live objects)', '']
    for key in sorted(agg['by_layer_group_kind']):
        s = agg['by_layer_group_kind'][key]
        lines.append(f"- `{key}`: n {s['n']}, median {s['area_m2_median']} m2, median convexity {s['convexity_median']}, lobes(1.5 m)>=2 in {s['lobes_1p5m_ge2']} "
                     f"({100*s['lobes_1p5m_ge2']/s['n']:.0f}%), lobes(1.0 m)>=2 {s['lobes_1p0m_ge2']}, lobes(2.0 m)>=2 {s['lobes_2p0m_ge2']}, lobes(rel 0.3*sqrt(A))>=2 {s['lobes_rel_ge2']}, "
                     f"with replaces {s['with_replaces']} (fragments {s['replaces_total']}, max {s['replaces_max']}), status {s['status']}.")
    lines += ['', '## By area band']
    for layer in ('review', 'model'):
        for group in ('windows', 'review_set'):
            for kind in CANOPY_KINDS:
                if f'{layer}|{group}|{kind}' not in agg['by_layer_group_kind']:
                    continue
                lines += ['', f'### {layer} layer, {group}, kind {kind}', '']
                lines += band_table(agg, layer, group, kind)
    lines += ['', '## 20 largest tree objects (review layer; windows + review set)', '',
              '| # | scene | object | m2 | vertices | convexity | lobes 1.0/1.5/2.0 m | lobe areas (1.5 m) | concav>=1 m2 | replaces | status | score |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for n, r in enumerate(census['largest_trees'], 1):
        lines.append(f"| {n} | {r['scene']} | {r['id']} | {r['area_m2']} | {r['vertices']} | {r['convexity']} | {r['lobes_1p0m']}/{r['lobes_1p5m']}/{r['lobes_2p0m']} | "
                     f"{r['lobe_areas_1p5m_m2']} | {r['concavities_ge1m2']} | {r['replaces_count']} | {r['status']} | {r['score']} |")
    lines += ['', '## The two named objects', '']
    for key, entry in census['named_objects'].items():
        lines.append(f'### {key}')
        lines.append('')
        for layer in ('review', 'model'):
            row = entry.get(layer)
            if not row:
                lines.append(f'- {layer} layer: object absent')
                continue
            lines.append(f"- {layer} layer: kind {row['kind']}, status {row['status']}, score {row['score']}, source {row['source']}, prompt '{row['prompt']}', "
                         f"{row['area_m2']} m2 (model.area_m2 {row['model_area_m2']}), {row['vertices']} vertices, {row['hole_count']} holes, "
                         f"convexity {row['convexity']}, compactness {row['compactness']}, bbox {row['bbox_m']} m, frame distance {row['frame_distance_px']} px, "
                         f"{row['meters_per_pixel']} m/px; lobes 1.0/1.5/2.0 m = {row['lobes_1p0m']}/{row['lobes_1p5m']}/{row['lobes_2p0m']}, "
                         f"lobe areas (1.5 m) {row['lobe_areas_1p5m_m2']}, largest lobe share {row['largest_lobe_share_1p5m']}, lobes at relative radius {row['rel_radius_m']} m = {row['lobes_rel']}; "
                         f"concavities >= 1 m2: {row['concavities_ge1m2']} {row['concavity_areas_m2']}; replaces {row['replaces_count']}, absorbed_by {row['absorbed_by']}, "
                         f"group_id {row['group_id']}, visibility {row['visibility']}, clipped_by_frame {row['clipped_by_frame']}, review_issues {row['review_issues']}, notes {row['has_notes']}.")
        sweep = entry.get('split_radius_sweep')
        if sweep:
            lines.append(f"- Opening-radius sweep 0.2..3.0 m: first radius with >= 2 lobes = {sweep['first_split_radius_m']}; lobes by radius "
                         f"{ {k: v for k, v in sweep['lobes_by_radius_m'].items() if v != 1} } (radii with exactly 1 lobe omitted).")
        tex = entry.get('texture')
        if tex:
            lines.append(f"- Texture reading: image `{tex['image']}`, {tex['pixels_inside']} px inside = {tex['area_m2']} m2, local-std window {tex['texture_window_px']} px "
                         f"({tex['texture_window_m']} m); render `{tex['render']}`.")
            for variant, v in tex['variants'].items():
                for k, e in v['k'].items():
                    areas = ', '.join(f"{c['area_m2']} m2 ({c['components_ge16px']} comp, largest {c['largest_component_share']})" for c in e['clusters'])
                    lines.append(f"  - {variant} k={k}: clusters [{areas}]; mixing ratio {e['adjacency_mixing_ratio']} (observed {e['adjacency_mixing_observed']} vs random "
                                 f"{e['adjacency_mixing_expected_random']}); inertia share {e['inertia_share_of_total']}; spatially separated: {e['spatially_separated']}.")
        lines.append('')
    lines += ['## Controls: confirmed single-lobe trees given the same texture reading', '',
              'The k-means always returns k clusters and the 1 m smoothing makes any brightness gradient spatially coherent, so the named objects are read '
              'against confirmed trees of the review layer (status confirmed, 1 lobe at 1.5 m, 15..250 m2; windows first, one per scene).', '',
              '| scene | object | m2 | convexity | concav>=1 m2 | smoothed k=2 areas | k=2 mixing ratio | k=2 inertia share | separated | pixel k=2 mixing | pixel k=2 separated | render |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for entry in census.get('controls', []):
        row, tex = entry['row'], entry['texture']
        sm = tex['variants']['smoothed_1m_colour_plus_texture']['k']['2']
        px = tex['variants']['colour_plus_texture']['k']['2']
        lines.append(f"| {row['scene']} | {row['id']} | {row['area_m2']} | {row['convexity']} | {row['concavities_ge1m2']} | "
                     f"{[c['area_m2'] for c in sm['clusters']]} | {sm['adjacency_mixing_ratio']} | {sm['inertia_share_of_total']} | {sm['spatially_separated']} | "
                     f"{px['adjacency_mixing_ratio']} | {px['spatially_separated']} | `{Path(tex['render']).name}` |")
    for key, entry in census['named_objects'].items():
        tex = entry.get('texture')
        if tex:
            sm = tex['variants']['smoothed_1m_colour_plus_texture']['k']['2']
            px = tex['variants']['colour_plus_texture']['k']['2']
            lines.append(f"| NAMED {key} | | {tex['area_m2']} | | | {[c['area_m2'] for c in sm['clusters']]} | {sm['adjacency_mixing_ratio']} | "
                         f"{sm['inertia_share_of_total']} | {sm['spatially_separated']} | {px['adjacency_mixing_ratio']} | {px['spatially_separated']} | `{Path(tex['render']).name}` |")
    lines += ['', '## Per scene (review layer; scenes sorted by tree objects >= 150 m2, top 25; all in census.json)', '',
              '| scene | group | trees | veg | tree >=150 | tree 60-150 | trees lobes(1.5 m)>=2 | trees lobes(rel)>=2 | trees concav>=2 | canopy with replaces | fragments | needs_adj | confirmed | largest tree m2 |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    ranked = sorted(((s, v) for s, v in census['per_scene'].items() if 'review' in v),
                    key=lambda kv: (-kv[1]['review']['tree_ge150'], -kv[1]['review']['tree_60_150']))[:25]
    for scene, v in ranked:
        r = v['review']
        lines.append(f"| {scene} | {v['group']} | {r['tree']} | {r['vegetation']} | {r['tree_ge150']} | {r['tree_60_150']} | {r['tree_lobes_1p5m_ge2']} | {r['tree_lobes_rel_ge2']} | {r['tree_concav_ge2']} | "
                     f"{r['canopy_with_replaces']} | {r['replaces_total']} | {r['needs_adjustment']} | {r['confirmed']} | {r['largest_tree_m2']} |")
    lines += ['', '## Method notes', '',
              '- Geometry in metres via the scene pixel_to_meters affine; pixel area = |det|. Statuses from the layer; model-layer rows also carry the review status by id.',
              "- `replaces` is written by scene_extract.merge_canopy_fragments (fragments < 1 m2 covered >= 0.9 by a canopy polygon >= 10x larger); it records "
              'fragment absorption, not a tree-count. `absorbed_by` marks rows that another live object in the same layer lists in its replaces.',
              '- Lobes at the relative radius use r = clamp(0.3 * sqrt(area), 0.5 m, 4 m), so a 18 m2 shrub pair is opened at 1.3 m and a 157 m2 group at 3.8 m.',
              '- Texture k-means: numpy k-means++ (6 restarts, 40 iterations) on z-scored channels inside the polygon. The local-std and the smoothed '
              'channels are masked (normalised) filters over canopy pixels only, so the rim is not read against the ground outside the polygon; the smoothed '
              'variant blurs every channel with sigma = 1 m first so the clusters read tree-scale pattern rather than leaf-scale speckle. Spatial separation = observed share of '
              '4-neighbour pixel pairs with different labels divided by the random-label expectation 1 - sum(p_i^2); ratio near 1 = salt-and-pepper mixing, '
              'well under 1 = coherent regions. "spatially_separated" is the descriptive rule ratio <= 0.4 and every cluster keeps >= 60% of its pixels in one component.',
              '- No model inference was run; nothing under data/scenes was written.']
    path.write_text('\n'.join(lines)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--skip-texture', action='store_true', help='skip the k-means texture reading on the named objects')
    parser.add_argument('--out-dir', type=Path, default=OUT_DIR)
    args = parser.parse_args()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes = population()
    rows, skipped_total, scene_meta = [], 0, {}
    groups = Counter(group for _, group in scenes)
    for scene_dir, group in scenes:
        meta = load_json(scene_dir/'latest.json')
        review = load_json(scene_dir/'review.json') if (scene_dir/'review.json').exists() else None
        review_status = {o['id']: o.get('status') for o in (review or {}).get('objects', [])}
        scene_meta[scene_dir.name] = (scene_dir, meta, review)
        for layer, objects in (('model', meta.get('objects', [])), ('review', (review or {}).get('objects', []))):
            if layer == 'review' and review is None:
                continue
            layer_rows_, skipped = layer_rows(scene_dir.name, group, layer, objects, meta, review_status)
            rows.extend(layer_rows_)
            skipped_total += skipped
    aggregates = aggregate(rows)
    live_review_trees = [r for r in rows if r['layer'] == 'review' and r['kind'] == 'tree' and r['status'] not in LIVE_EXCLUDED]
    largest = sorted(live_review_trees, key=lambda r: -r['area_m2'])[:20]
    named = {}
    for scene_id, object_id in NAMED:
        entry = {}
        for layer in ('review', 'model'):
            match = [r for r in rows if r['scene'] == scene_id and r['id'] == object_id and r['layer'] == layer]
            entry[layer] = match[0] if match else None
        if not args.skip_texture and scene_id in scene_meta:
            scene_dir, meta, review = scene_meta[scene_id]
            source = review if review and any(o['id'] == object_id for o in review['objects']) else meta
            obj = next((o for o in source['objects'] if o['id'] == object_id), None)
            polygon = object_polygon(obj) if obj else None
            if polygon is not None:
                entry['texture'] = texture_reading(scene_dir, meta, obj, polygon, out_dir)
        if scene_id in scene_meta:
            scene_dir, meta, review = scene_meta[scene_id]
            source = review if review and any(o['id'] == object_id for o in review['objects']) else meta
            obj = next((o for o in source['objects'] if o['id'] == object_id), None)
            polygon = object_polygon(obj) if obj else None
            if polygon is not None:
                entry['split_radius_sweep'] = split_radius_sweep(affine_transform(polygon, affine_params(meta['pixel_to_meters'])))
        named[f'{scene_id}/{object_id}'] = entry
    controls = []
    if not args.skip_texture:
        seen = set()
        pool = sorted((r for r in live_review_trees if r['status'] == 'confirmed' and 15 <= r['area_m2'] <= 250 and r['lobes_1p5m'] == 1),
                      key=lambda r: (r['group'] != 'windows', -r['area_m2']))
        for row in pool:
            if row['scene'] in seen or len(controls) >= CONTROLS:
                continue
            scene_dir, meta, review = scene_meta[row['scene']]
            obj = next((o for o in review['objects'] if o['id'] == row['id']), None)
            polygon = object_polygon(obj) if obj else None
            if polygon is None:
                continue
            seen.add(row['scene'])
            controls.append({'row': row, 'texture': texture_reading(scene_dir, meta, obj, polygon, out_dir)})
    census = {
        'experiment': 'tree-group-split-2026-10-03', 'generated_at': datetime.now(UTC).isoformat(timespec='seconds'),
        'code_sha256': CODE_SHA256, 'read_only': True, 'inference': False,
        'population': {'windows': groups['windows'], 'review_set': groups['review_set'], 'rows_total': len(rows),
                       'rows_by_layer': dict(Counter(r['layer'] for r in rows)), 'skipped_invalid': skipped_total,
                       'scene_ids': {'windows': [d.name for d, g in scenes if g == 'windows'],
                                     'review_set': [d.name for d, g in scenes if g == 'review_set']}},
        'parameters': {'bands_m2': [b[0] for b in BANDS], 'opening_radii_m': OPEN_RADII_M, 'headline_radius_m': HEADLINE_RADIUS_M,
                       'relative_radius': REL_RADIUS, 'sweep_radii_m': SWEEP_RADII_M, 'smooth_sigma_m': SMOOTH_SIGMA_M,
                       'lobe_min_m2': LOBE_MIN_M2, 'concavity_min_m2': CONCAVITY_MIN_M2, 'texture_window_m': TEXTURE_WINDOW_M,
                       'kmeans_ks': KMEANS_KS, 'component_min_px': COMPONENT_MIN_PX},
        'aggregates': aggregates, 'per_scene': per_scene(rows), 'largest_trees': largest, 'named_objects': named, 'controls': controls,
        'objects_file': 'objects.jsonl',
    }
    with open(out_dir/'objects.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row)+'\n')
    (out_dir/'census.json').write_text(json.dumps(census, indent=1)+'\n')
    write_markdown(census, out_dir/'census.md')
    print(json.dumps({'rows': len(rows), 'windows': groups['windows'], 'review_set': groups['review_set'], 'skipped': skipped_total,
                      'out': str(out_dir.relative_to(ROOT))}))


if __name__ == '__main__':
    main()
