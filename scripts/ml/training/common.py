"""Shared helpers: hashing, scene frames, z17 tile groups, rasterisation, JSONL logging."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.validation import make_valid

from ..tiles import WORLD_SPAN_M, lonlat_to_tile_xy

VERSION = 'roof-outline-training-v1'
PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parents[2]
DEFAULT_OUT = Path('data/reviews/eyes/training')
CATEGORY = {'id': 1, 'name': 'building_roof', 'supercategory': 'building'}
IGNORE = 65535
ZOOM = 17
# Per-instance sampling weights by provenance tier. Human outlines are the only crisp corner signal;
# pseudo-labels are SAM3's own (chamfered) outlines and carry weight 1.
PROVENANCE_WEIGHTS = {'human_T1': 10., 'human_T2': 8., 'human_T2_lines_only': 3., 'machine_pseudo': 1.}
HUMAN_PROVENANCES = ('human_T1', 'human_T2', 'human_T2_lines_only')
# Outlines a person drew or confirmed; lines-only rows keep SAM3's outline and never anchor a boundary metric.
BOUNDARY_PROVENANCES = ('human_T1', 'human_T2')
NOTE = ('Reference agreement with the listed human outlines only, not calibrated accuracy. Validation scores are '
        'agreement with SAM3 pseudo-labels.')


def now_iso():
    return datetime.now(UTC).isoformat()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def code_sha256():
    """Hash of every .py file in this package, so outputs bind to the code that made them."""
    digest = hashlib.sha256()
    for path in sorted(PACKAGE.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    with os.fdopen(handle, 'w') as out:
        json.dump(payload, out, indent=1, sort_keys=True)
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text())


def write_tarball(folder):
    """<folder>.tar next to the folder (PNGs are already compressed) plus its sha256; returns both."""
    folder = Path(folder)
    target = folder.with_suffix('.tar')
    with tarfile.open(target, 'w') as tar:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                tar.add(path, arcname=str(Path(folder.name) / path.relative_to(folder)))
    digest = sha256_file(target)
    target.with_suffix('.tar.sha256').write_text(f'{digest}  {target.name}\n')
    return {'path': str(target), 'sha256': digest, 'bytes': target.stat().st_size}


class JsonlLogger:
    """Append-only JSON lines with a monotonic elapsed clock; every record carries the run id."""

    def __init__(self, path, run_id):
        self.path, self.run_id, self.start = Path(path), run_id, time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event, **fields):
        record = {'event': event, 'run_id': self.run_id, 'elapsed_s': round(time.monotonic() - self.start, 3),
                  'at': now_iso(), **fields}
        with open(self.path, 'a') as handle:
            handle.write(json.dumps(record, sort_keys=True, default=_json_default) + '\n')
        return record


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f'not JSON serialisable: {type(value).__name__}')


# --- scene frames -----------------------------------------------------------------------------------

def pixel_size_m(scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    return float(math.sqrt(abs(np.linalg.det(frame[:2]))))


def pixel_to_local(points, scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    return np.c_[points, np.ones(len(points))] @ frame


def local_to_pixel(points, scene):
    inverse = np.asarray(scene['meters_to_pixel'], dtype=float)
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    return np.c_[points, np.ones(len(points))] @ inverse


def _tile_scale(scene):
    centre = scene['centroid']
    origin = np.array(lonlat_to_tile_xy(centre['lon'], centre['lat'], ZOOM), dtype=float)
    metres_per_tile = WORLD_SPAN_M * math.cos(math.radians(centre['lat'])) / (1 << ZOOM)
    return origin, metres_per_tile


def local_to_tile_xy(points_m, scene):
    origin, metres_per_tile = _tile_scale(scene)
    return origin + np.asarray(points_m, dtype=float).reshape(-1, 2) * np.array([1., -1.]) / metres_per_tile


def tile_xy_to_local(points_xy, scene):
    origin, metres_per_tile = _tile_scale(scene)
    return (np.asarray(points_xy, dtype=float).reshape(-1, 2) - origin) * metres_per_tile * np.array([1., -1.])


def lonlat_to_pixel(points_lonlat, scene):
    """[lon, lat] -> scene pixels through the scene's own Mercator frame (scene_confidence.geographic inverse)."""
    tiles = np.array([lonlat_to_tile_xy(lon, lat, ZOOM) for lon, lat in np.asarray(points_lonlat, dtype=float).reshape(-1, 2)])
    return local_to_pixel(tile_xy_to_local(tiles, scene), scene)


def pixel_group(point_px, scene):
    """z17 tile holding one scene pixel."""
    x, y = local_to_tile_xy(pixel_to_local([point_px], scene), scene)[0]
    return f'z{ZOOM}:{math.floor(x)}:{math.floor(y)}'


def tile_group(lon, lat):
    x, y = lonlat_to_tile_xy(lon, lat, ZOOM)
    return f'z{ZOOM}:{math.floor(x)}:{math.floor(y)}'


def centre_group(scene):
    return tile_group(scene['centroid']['lon'], scene['centroid']['lat'])


def parse_group(group):
    _, x, y = group.split(':')
    return int(x), int(y)


def frame_tile_groups(scene):
    """Every z17 tile the pixel frame covers with positive area (a 64 px strip counts)."""
    width, height = scene['image_width'], scene['image_height']
    corners = pixel_to_local([[0, 0], [width, 0], [width, height], [0, height]], scene)
    polygon = Polygon(local_to_tile_xy(corners, scene))
    xmin, ymin, xmax, ymax = polygon.bounds
    groups = []
    for x in range(math.floor(xmin), math.ceil(xmax)):
        for y in range(math.floor(ymin), math.ceil(ymax)):
            if polygon.intersection(box(x, y, x + 1, y + 1)).area > 1e-9:
                groups.append(f'z{ZOOM}:{x}:{y}')
    return groups


def tile_polygon_pixels(group, scene):
    """A z17 tile's footprint in the scene's pixel frame."""
    x, y = parse_group(group)
    corners = tile_xy_to_local([[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1]], scene)
    return Polygon(local_to_pixel(corners, scene))


# --- geometry ---------------------------------------------------------------------------------------

def valid_polygon(polygon):
    """Largest valid Polygon part of `polygon`, or None when nothing with area remains."""
    if polygon is None or polygon.is_empty:
        return None
    if not polygon.is_valid:
        polygon = make_valid(polygon)
    parts = []
    stack = [polygon]
    while stack:
        geom = stack.pop()
        if geom.geom_type == 'Polygon':
            parts.append(geom)
        elif hasattr(geom, 'geoms'):
            stack.extend(geom.geoms)
    parts = [p for p in parts if p.area > 0]
    return max(parts, key=lambda p: p.area) if parts else None


def ring_polygon(ring, holes=None):
    if ring is None or len(ring) < 3:
        return None
    return valid_polygon(Polygon([tuple(map(float, p[:2])) for p in ring],
                                 [[tuple(map(float, p[:2])) for p in h] for h in (holes or []) if len(h) >= 3]))


def _ring_edges(rings):
    edges = []
    for ring in rings:
        points = np.asarray(ring, dtype=float).reshape(-1, 2)
        if len(points) > 1 and np.allclose(points[0], points[-1]):
            points = points[:-1]
        if len(points) >= 3:
            edges.append(np.c_[points, np.roll(points, -1, axis=0)])
    return np.concatenate(edges) if edges else np.zeros((0, 4))


def fill_rings(rings, width, height):
    """Even-odd fill that samples pixel centres: pixel (i, j) is inside when (i + .5, j + .5) is.

    This is the exact inverse of segmentation.mask_to_polygons (pixel i covers [i, i + 1)), so a mask survives
    polygonise -> rasterise unchanged. PIL's polygon fill includes both boundary pixels and would bias every
    outline by one pixel on its right and bottom edges (about 0.3 m of Hausdorff at 0.236 m/px).
    """
    mask = np.zeros((height, width), dtype=bool)
    edges = _ring_edges(rings)
    if not len(edges):
        return mask
    x0, y0, x1, y1 = edges.T
    top = max(int(np.floor(min(y0.min(), y1.min()))), 0)
    bottom = min(int(np.ceil(max(y0.max(), y1.max()))), height)
    if bottom <= top:
        return mask
    centres = np.arange(top, bottom, dtype=float)[:, None] + .5
    crosses = (centres >= np.minimum(y0, y1)) & (centres < np.maximum(y0, y1))
    with np.errstate(divide='ignore', invalid='ignore'):
        xs = np.where(crosses, x0 + (centres - y0) * (x1 - x0) / (y1 - y0), np.inf)
    xs.sort(axis=1)
    for row, (count, line) in enumerate(zip(crosses.sum(axis=1), xs, strict=True)):
        for a, b in zip(line[0:count:2], line[1:count:2], strict=True):
            start, stop = max(int(np.ceil(a - .5)), 0), min(int(np.ceil(b - .5)), width)
            if stop > start:
                mask[top + row, start:stop] = True
    return mask


def polygon_rings(polygon):
    return [polygon.exterior.coords, *(h.coords for h in polygon.interiors)]


def rasterise(polygon, width, height, fill=1, into=None):
    """Fill `polygon` (holes cleared) into a uint16 label map with pixel-centre sampling; returns the array."""
    if into is None:
        into = np.zeros((height, width), dtype=np.uint16)
    into[fill_rings(polygon_rings(polygon), width, height)] = fill
    return into


def polygon_mask(polygon, width, height):
    return fill_rings(polygon_rings(polygon), width, height)


def flat_coords(ring):
    return [round(float(v), 2) for xy in ring for v in xy]


def polygon_to_coco(polygon):
    exterior = list(polygon.exterior.coords)[:-1]
    holes = [list(h.coords)[:-1] for h in polygon.interiors]
    minx, miny, maxx, maxy = polygon.bounds
    return {'segmentation': [flat_coords(exterior)], 'holes': [flat_coords(h) for h in holes],
            'bbox': [round(minx, 2), round(miny, 2), round(maxx - minx, 2), round(maxy - miny, 2)],
            'area': round(float(polygon.area), 2)}


def coco_to_polygon(annotation):
    ring = np.asarray(annotation['segmentation'][0], dtype=float).reshape(-1, 2)
    holes = [np.asarray(h, dtype=float).reshape(-1, 2) for h in annotation.get('holes') or []]
    return ring_polygon(ring, holes)


def min_rectangle(polygon):
    with np.errstate(divide='ignore', invalid='ignore'):   # GEOS warns on exact axis-aligned rectangles
        return polygon.minimum_rotated_rectangle


def rectilinearity(polygon):
    rectangle = min_rectangle(polygon)
    return float(polygon.area / rectangle.area) if rectangle.area > 0 else 0.


# --- orthogonal regularisation (opt-in training target) -----------------------------------------------

ORTHO = {'simplify_m': .1, 'min_run_m': .6, 'min_iou': .85, 'max_hausdorff_m': 1.2}


def dominant_angle(polygon):
    """Edge-length-weighted mean orientation modulo 90 degrees (radians)."""
    coords = np.asarray(polygon.exterior.coords, dtype=float)
    delta = np.diff(coords, axis=0)
    lengths = np.hypot(delta[:, 0], delta[:, 1])
    angles = np.arctan2(delta[:, 1], delta[:, 0])
    return float(np.arctan2((lengths * np.sin(4 * angles)).sum(), (lengths * np.cos(4 * angles)).sum()) / 4)


def _runs(points, axis_tolerance_deg=25.):
    """Cyclic runs of axis-classified edges: [axis ('H'|'V'), coordinate, length, weight].

    The coordinate is a weighted mean over the run's edges; edges more than axis_tolerance_deg off the axis
    (chamfers, rounded corners) keep 5% weight so they do not drag a wall inward.
    """
    n = len(points)
    edges = [(points[i], points[(i + 1) % n]) for i in range(n)]
    axes = ['H' if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else 'V' for a, b in edges]
    start = next((i for i in range(n) if axes[i] != axes[i - 1]), None)
    if start is None:
        return []
    runs = []
    for (a, b), axis in zip(edges[start:] + edges[:start], axes[start:] + axes[:start], strict=True):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        length = float(np.hypot(dx, dy))
        deviation = np.degrees(np.arctan2(dy, dx) if axis == 'H' else np.arctan2(dx, dy))
        weight = length if deviation <= axis_tolerance_deg else .05 * length
        value = (a[1] + b[1]) / 2 if axis == 'H' else (a[0] + b[0]) / 2
        run = [axis, value, length, weight]
        runs[-1:] = [_merge(runs[-1], run)] if runs and runs[-1][0] == axis else [*runs[-1:], run]
    return runs


def _merge(first, second):
    weight = first[3] + second[3]
    value = (first[1] * first[3] + second[1] * second[3]) / weight if weight > 0 else (first[1] + second[1]) / 2
    return [first[0], value, first[2] + second[2], weight]


def _rectangle_angle(polygon):
    corners = np.asarray(min_rectangle(polygon).exterior.coords, dtype=float)
    edge = corners[1] - corners[0]
    return float(np.arctan2(edge[1], edge[0]))


def orthogonalise(polygon, px_m, *, simplify_m=ORTHO['simplify_m'], min_run_m=ORTHO['min_run_m'],
                  min_iou=ORTHO['min_iou'], max_hausdorff_m=ORTHO['max_hausdorff_m']):
    """Snap an outline to its dominant axis pair (sharp 90 degree corners), or None when the gates refuse.

    Two axis candidates (edge-weighted orientation of a 1 m-simplified copy, so pixel staircases do not pull it to
    0 degrees, and the minimum rotated rectangle); for each: rotate, classify every edge as along/across, merge
    runs, drop runs shorter than min_run_m (neighbours merge), intersect consecutive runs, rotate back. The
    candidate with the higher IoU wins if it passes the gates: valid, IoU >= min_iou, Hausdorff <= max_hausdorff_m.
    Holes refuse. A machine prior for pseudo-labels, never a human label.
    """
    if polygon is None or polygon.is_empty or polygon.interiors:
        return None
    coarse = polygon.simplify(1. / px_m, preserve_topology=True)
    angles = {dominant_angle(coarse if coarse.geom_type == 'Polygon' and not coarse.is_empty else polygon),
              _rectangle_angle(polygon)}
    best, best_iou = None, 0.
    for theta in sorted(angles):
        candidate = _orthogonal_at(polygon, px_m, theta, simplify_m, min_run_m)
        if candidate is None:
            continue
        iou = candidate.intersection(polygon).area / max(candidate.union(polygon).area, 1e-9)
        if iou > best_iou:
            best, best_iou = candidate, iou
    if best is None or best_iou < min_iou or best.hausdorff_distance(polygon) * px_m > max_hausdorff_m:
        return None
    return best


def _orthogonal_at(polygon, px_m, theta, simplify_m, min_run_m):
    centre = polygon.centroid
    rotated = affinity.rotate(polygon, -theta, origin=centre, use_radians=True)
    simple = rotated.simplify(simplify_m / px_m, preserve_topology=True)
    if simple.geom_type != 'Polygon' or simple.is_empty:
        return None
    runs = _runs(np.asarray(simple.exterior.coords, dtype=float)[:-1])
    while len(runs) > 4:
        lengths = [r[2] for r in runs]
        index = int(np.argmin(lengths))
        if lengths[index] * px_m >= min_run_m:
            break
        runs = _cyclic_fix([r for i, r in enumerate(runs) if i != index])   # its two neighbours now merge
    runs = _cyclic_fix(runs)
    if len(runs) < 4 or len(runs) % 2:
        return None
    vertices = []
    for i, run in enumerate(runs):
        nxt = runs[(i + 1) % len(runs)]
        vertices.append((nxt[1], run[1]) if run[0] == 'H' else (run[1], nxt[1]))
    candidate = Polygon(vertices)
    if not candidate.is_valid or candidate.area <= 0:
        return None
    return affinity.rotate(candidate, theta, origin=centre, use_radians=True)


def _cyclic_fix(runs):
    """Merge equal-axis neighbours anywhere, including across the wrap-around."""
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i in range(len(runs)):
            j = (i + 1) % len(runs)
            if i != j and runs[i][0] == runs[j][0]:
                merged = _merge(runs[i], runs[j])
                runs = [r for k, r in enumerate(runs) if k not in (i, j)]
                runs.insert(min(i, len(runs)), merged)
                changed = True
                break
    return runs
