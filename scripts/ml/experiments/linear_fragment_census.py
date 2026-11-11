"""Read-only census of road / driveway / footpath fragmentation in the recapture windows (linear-continuity-2026-10-03).

Reads data/scenes/{win-*,lot-*}/latest.json and the frame PNG; writes ONE json under the experiment folder (diagnosis-census.json). Every figure is a
count or an agreement; nothing is accuracy and nothing is written under data/scenes. Pieces that share (kind, score) are the
connected components of one SAM3 proposal mask (scene_extract.extract_objects emits one object per polygon that
segmentation.mask_to_polygons returns for a mask, with detection.score copied onto each).
Run from the repo root: scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_fragment_census
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

LINEAR = ('road', 'driveway', 'footpath')
OCCLUDERS = ('car', 'tree', 'building', 'vegetation')
# diagnosis-census.json since 2026-10-03 PM (decision card item 3): this script and linear_continuity_census.py both wrote
# census.json, so whichever ran second overwrote the other. census.json now belongs to linear_continuity_census.py alone.
OUT = Path('data/reviews/eyes/experiments/linear-continuity-2026-10-03/diagnosis-census.json')
FLOOR_M2 = 1.0            # what-if metric floor (buildings already use max(12 px, 1 m2) at scene_extract.py:183)
JOIN_M = 1.0              # what-if same-kind proximity join distance


def luminance(px):
    return 0.299*px[..., 0]+0.587*px[..., 1]+0.114*px[..., 2]


def region_stats(geom, image, size):
    mask = Image.new('L', size)
    draw = ImageDraw.Draw(mask)
    parts = geom.geoms if geom.geom_type in ('MultiPolygon', 'GeometryCollection') else [geom]
    for part in parts:
        if part.is_empty or part.geom_type != 'Polygon':
            continue
        draw.polygon([tuple(p) for p in part.exterior.coords], fill=255)
        for hole in part.interiors:
            draw.polygon([tuple(p) for p in hole.coords], fill=0)
    selected = np.asarray(mask) > 0
    if not selected.any():
        return None
    px = image[selected]
    exg = (2*px[:, 1]-px[:, 0]-px[:, 2])/(px.sum(1)+1)
    return {'n': int(selected.sum()), 'lum': float(np.median(luminance(px))), 'exg': float(np.median(exg))}


def classify_gap(a_stats, b_stats, corridor_stats, occupants):
    if occupants:
        return 'occluder_detected'
    if corridor_stats is None:
        return 'no_corridor'
    piece_lum = np.mean([a_stats['lum'], b_stats['lum']])
    if corridor_stats['exg'] > 0.05:
        return 'green_undetected'
    if corridor_stats['lum'] < piece_lum-25:
        return 'dark_undetected'
    if abs(corridor_stats['lum']-piece_lum) <= 15:
        return 'same_surface'
    return 'other'


def main():
    scenes = sorted(glob.glob('data/scenes/win-*')+glob.glob('data/scenes/lot-*'))
    totals = Counter()
    masks = Counter()
    multi_masks = Counter()
    comp_hist = Counter()
    sub_floor = Counter()
    near_px_floor = Counter()
    frame_touch = Counter()
    nverts = defaultdict(list)
    gap_class = Counter()
    gap_class_by_kind = defaultdict(Counter)
    gap_m = []
    cross_dup = Counter()
    whatif = Counter()
    per_scene = []
    for folder in scenes:
        path = os.path.join(folder, 'latest.json')
        if not os.path.exists(path):
            continue
        scene = json.loads(Path(path).read_text())
        W, H = scene['image_width'], scene['image_height']
        to_m = np.asarray(scene['pixel_to_meters'], dtype=float)
        pixel_area = abs(float(np.linalg.det(to_m[:2])))
        mpp = pixel_area**.5
        image = np.asarray(Image.open(os.path.join(folder, f"{scene['image_sha256']}.png")).convert('RGB')).astype(float)
        live = [o for o in scene['objects'] if o.get('status') != 'superseded' and o.get('source') == 'sam3-text-onnx']
        polys = {o['id']: Polygon(o['ring'], o.get('holes') or []) for o in live}
        occluders = [(o['kind'], polys[o['id']]) for o in live if o['kind'] in OCCLUDERS]
        frame = box(0, 0, W, H).exterior
        row = {'scene': os.path.basename(folder), 'frame_px': W, 'm_per_px': round(mpp, 4)}
        for kind in LINEAR:
            objs = [o for o in live if o['kind'] == kind]
            totals[kind] += len(objs)
            groups = defaultdict(list)
            for o in objs:
                groups[o['score']].append(o)
            masks[kind] += len(groups)
            row[f'{kind}_pieces'] = len(objs)
            row[f'{kind}_masks'] = len(groups)
            for members in groups.values():
                comp_hist[min(len(members), 10)] += 1
                if len(members) > 1:
                    multi_masks[kind] += 1
                    whatif[f'{kind}_pieces_removed_by_same_mask_union'] += len(members)-1
                for i in range(len(members)):
                    for j in range(i+1, len(members)):
                        A, B = polys[members[i]['id']], polys[members[j]['id']]
                        dist = A.distance(B)
                        if dist*mpp > 8.0:
                            continue
                        corridor = unary_union([A, B]).convex_hull.difference(A).difference(B)
                        hits = sorted({k for k, p in occluders if corridor.area > 0 and p.intersection(corridor).area/corridor.area > .15})
                        label = classify_gap(region_stats(A, image, (W, H)), region_stats(B, image, (W, H)), region_stats(corridor, image, (W, H)), hits)
                        gap_class[label] += 1
                        gap_class_by_kind[kind][label] += 1
                        gap_m.append(dist*mpp)
            for o in objs:
                P = polys[o['id']]
                if P.area*pixel_area < FLOOR_M2:
                    sub_floor[kind] += 1
                if P.area < 30:
                    near_px_floor[kind] += 1
                if P.distance(frame) < 1.0:
                    frame_touch[kind] += 1
                nverts[kind].append(len(o['ring']))
            # what-if: same-kind proximity join within JOIN_M metres (any score)
            if len(objs) > 1:
                buffered = unary_union([polys[o['id']].buffer(JOIN_M/mpp/2) for o in objs])
                n_after = len(buffered.geoms) if buffered.geom_type == 'MultiPolygon' else 1
                whatif[f'{kind}_pieces_removed_by_{JOIN_M:g}m_same_kind_join'] += len(objs)-n_after
        roads = [polys[o['id']] for o in live if o['kind'] == 'road']
        paths = [polys[o['id']] for o in live if o['kind'] == 'footpath']
        drives = [polys[o['id']] for o in live if o['kind'] == 'driveway']
        for name, A_list, B_list in (('road_footpath', roads, paths), ('road_driveway', roads, drives), ('driveway_footpath', drives, paths)):
            for A in A_list:
                for B in B_list:
                    if A.intersection(B).area/max(.01, min(A.area, B.area)) > .8:
                        cross_dup[name] += 1
        per_scene.append(row)
    verts = {k: {'n': len(v), 'max': int(max(v)), 'p50': float(np.percentile(v, 50)), 'p90': float(np.percentile(v, 90)), 'p99': float(np.percentile(v, 99)), 'ge_150': int(sum(x >= 150 for x in v)), 'ge_190': int(sum(x >= 190 for x in v))} for k, v in nverts.items()}
    report = {
        'version': 'linear-fragment-census-v1', 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
        'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
        'scenes': len(per_scene), 'read_only': True, 'wrote': str(OUT),
        'definitions': {'mask': 'pieces sharing (kind, score) = connected components of one SAM3 proposal mask',
                        'gap': 'pair of components of one mask within 8 m; corridor = convex hull minus the pieces',
                        'occluder_detected': 'a car/tree/building/vegetation model object covers > 15% of the corridor',
                        'same_surface': 'no occluder and corridor median luminance within 15 of the pieces (pavement continues)',
                        'dark_undetected': 'no occluder, corridor darker by > 25 (shadow or undetected object)',
                        'green_undetected': 'no occluder, corridor ExG > 0.05 (undetected vegetation)',
                        'what_if': 'counts of pieces a post-processing rule would remove; nothing applied, nothing promoted'},
        'pieces': dict(totals), 'masks': dict(masks), 'masks_with_several_components': dict(multi_masks),
        'components_per_mask_hist(10=10+)': dict(sorted(comp_hist.items())),
        f'pieces_under_{FLOOR_M2:g}_m2': dict(sub_floor), 'pieces_under_30_px': dict(near_px_floor), 'pieces_touching_frame': dict(frame_touch),
        'vertices': verts, 'gap_class': dict(gap_class), 'gap_class_by_kind': {k: dict(v) for k, v in gap_class_by_kind.items()},
        'gap_m': {'n': len(gap_m), 'p25': float(np.percentile(gap_m, 25)), 'p50': float(np.percentile(gap_m, 50)), 'p75': float(np.percentile(gap_m, 75))} if gap_m else {},
        'cross_class_duplicates(>0.8 of smaller)': dict(cross_dup), 'what_if': dict(whatif), 'per_scene': per_scene}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != 'per_scene'}, indent=1))


if __name__ == '__main__':
    main()
