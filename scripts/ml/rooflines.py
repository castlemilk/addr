"""Roofline extraction: a Python port of the TU Delft kinetic partition.

Ported from tudelft3d/Roofline-extraction-from-orthophotos (Gao et al. 2023,
GPL-3.0): LSD line detection -> mean-shift regularization of segment angles and
ordinates -> ray propagation with a bounded number of intersections -> planar
partition of the footprint -> depth plane fits -> rooflines (plane boundaries)
and per-plane LoD2 roof surfaces.

The C++ original uses a kinetic data structure with event scheduling; this port
keeps the same stages with a shapely line arrangement instead of the kinetic
scheduler, which is fast enough for one footprint at a time.

Usage:
    python -m scripts.ml.rooflines --address-id 29790 \
        --out data/reviews/29790/rooflines.json --debug /tmp/rooflines-29790.png
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from shapely import constrained_delaunay_triangles
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from .addresses import METERS_PER_DEGREE
from .imagery import ImageryFetcher, ImageryWindow
from .ortho import OrthoCompositor, composite_gray
from .pipeline import PipelineConfig, build_context
from .pointcloud import PointCloud, build_point_cloud, fit_plane_robust, points_in_polygon
from .review_feedback import latest_feedback, rejected_cuts_for
from .roof_form import select_roof_ends
from .roof_reflection import detect_dominant_building_axis
from .roof_structure import audit_roof, shared_edges, structural_prior
from .tiles import lonlat_to_tile_local, tile_local_to_lonlat

MIN_SEGMENT_M = 1.8
ANGLE_BANDWIDTH_DEG = 3.0
OFFSET_BANDWIDTH_M = 0.8
K_INTERSECTIONS = 1
MIN_FACE_AREA_M2 = 3.0
MIN_FOOTPRINT_COVER = 0.55
PLANE_ANGLE_TOL_DEG = 12.0
PLANE_OFFSET_TOL = 0.35
DEPTH_SAMPLE_STEP_M = 0.8


def _load_record(records_path: Path, address_id: int) -> dict:
    for record in json.loads(records_path.read_text()):
        if record["address_id"] == address_id:
            return record
    raise SystemExit(f"address {address_id} not found in {records_path}")


class Frame:
    """Metre frame (x east, y north) relative to a record's centroid."""

    def __init__(self, record: dict, window, zoom: int, tile_x: int, tile_y: int):
        self.centroid = record["centroid"]
        self.window = window
        self.zoom = zoom
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.cos_lat = math.cos(math.radians(self.centroid["lat"]))

    def ring_meters(self, record: dict) -> Polygon:
        return Polygon([tuple(point) for point in record["ring_meters"]])

    def metre_to_pixel(self, x: float, y: float) -> tuple[float, float]:
        lon = self.centroid["lon"] + x / (METERS_PER_DEGREE * self.cos_lat)
        lat = self.centroid["lat"] + y / METERS_PER_DEGREE
        return self.window.local_to_pixel(
            *lonlat_to_tile_local(lon, lat, self.tile_x, self.tile_y, self.zoom)
        )

    def pixel_to_metre(self, px: float, py: float) -> tuple[float, float]:
        lx, ly = self.window.pixel_to_local(px, py)
        lon, lat = tile_local_to_lonlat(lx, ly, self.tile_x, self.tile_y, self.zoom)
        return (
            (lon - self.centroid["lon"]) * METERS_PER_DEGREE * self.cos_lat,
            (lat - self.centroid["lat"]) * METERS_PER_DEGREE,
        )

    def metre_to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.centroid["lon"] + x / (METERS_PER_DEGREE * self.cos_lat),
            self.centroid["lat"] + y / METERS_PER_DEGREE,
        )


def _angle_delta_deg(a_rad: float, b_rad: float) -> float:
    a_deg = math.degrees(a_rad) % 180.0
    b_deg = math.degrees(b_rad) % 180.0
    diff = abs(a_deg - b_deg)
    return min(diff, 180.0 - diff)


def _drop_parallel_families(
    segments: list[dict],
    *,
    angle_tolerance_deg: float = 5.0,
    offset_gap_m: float = 1.6,
) -> list[dict]:
    """Drops texture / standing seam / tile rib families of parallel lines close together.

    A real roof ridge or hip is isolated: at most one ridge/hip per orientation.
    Closely spaced parallel lines of the same material are surface texture, so
    only the longest of each parallel family survives.
    """
    if len(segments) < 2:
        return segments
    ordered = sorted(segments, key=lambda s: (s["angle"], s["offset"]))
    dropped: set[int] = set()

    def keep_longest(group: list[dict]) -> None:
        if len(group) < 2:
            return
        longest = max(group, key=lambda s: s["length"])
        for item in group:
            if item is not longest:
                dropped.add(id(item))

    run = [ordered[0]]
    for item in ordered[1:]:
        prev = run[-1]
        if (
            _angle_delta_deg(prev["angle"], item["angle"]) <= angle_tolerance_deg
            and abs(item["offset"] - prev["offset"]) <= offset_gap_m
        ):
            run.append(item)
        else:
            keep_longest(run)
            run = [item]
    keep_longest(run)
    return [s for s in segments if id(s) not in dropped]


def _plane_split_evidence(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    color_image: np.ndarray,
    frame: Frame,
    *,
    step_m: float = 0.6,
    samples: int = 10,
    chroma_tolerance: float = 18.0,
    texture_ratio_min: float = 0.35,
) -> bool:
    """True when candidate line separates two slopes of the same material (ridge/hip).

    A genuine plane split has similar chroma and surface texture on both sides,
    with a lightness step from solar orientation. Material transitions, shadows,
    and obstacles differ sharply in chroma or texture and are rejected.
    """
    length = math.hypot(bx - ax, by - ay)
    if length < 1.0 or color_image is None:
        return True

    dx, dy = (bx - ax) / length, (by - ay) / length
    nx, ny = -dy, dx

    height, width = color_image.shape[:2]
    lab = cv2.cvtColor(color_image, cv2.COLOR_RGB2LAB).astype(np.float32)
    gray = cv2.cvtColor(color_image, cv2.COLOR_RGB2GRAY)
    sobel_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gradients = np.hypot(sobel_x, sobel_y)

    left_samples = []
    right_samples = []

    for i in range(samples):
        t = (i + 0.5) / samples
        mx = ax + dx * (t * length)
        my = ay + dy * (t * length)

        lx_m, ly_m = mx + nx * step_m, my + ny * step_m
        rx_m, ry_m = mx - nx * step_m, my - ny * step_m

        l_px, l_py = frame.metre_to_pixel(lx_m, ly_m)
        r_px, r_py = frame.metre_to_pixel(rx_m, ry_m)

        il_x, il_y = int(round(l_px)), int(round(l_py))
        ir_x, ir_y = int(round(r_px)), int(round(r_py))

        if 0 <= il_y < height and 0 <= il_x < width:
            tex_l = float(gradients[max(0, il_y - 1) : il_y + 2, max(0, il_x - 1) : il_x + 2].mean())
            left_samples.append((lab[il_y, il_x], tex_l))

        if 0 <= ir_y < height and 0 <= ir_x < width:
            tex_r = float(gradients[max(0, ir_y - 1) : ir_y + 2, max(0, ir_x - 1) : ir_x + 2].mean())
            right_samples.append((lab[ir_y, ir_x], tex_r))

    if len(left_samples) < 3 or len(right_samples) < 3:
        return True

    left_color = np.mean([s[0] for s in left_samples], axis=0)
    right_color = np.mean([s[0] for s in right_samples], axis=0)
    left_tex = float(np.mean([s[1] for s in left_samples]))
    right_tex = float(np.mean([s[1] for s in right_samples]))

    chroma_delta = float(
        np.hypot(left_color[1] - right_color[1], left_color[2] - right_color[2])
    )
    tex_high = max(left_tex, right_tex)
    tex_low = min(left_tex, right_tex)
    tex_ratio = tex_low / tex_high if tex_high > 1e-6 else 1.0

    if chroma_delta > chroma_tolerance or tex_ratio < texture_ratio_min:
        return False

    return True


def _detect_segments(
    gray: np.ndarray,
    frame: Frame,
    footprint: Polygon,
    color_image: np.ndarray | None = None,
    enhanced_gray: np.ndarray | None = None,
) -> list[dict]:
    lsd = cv2.createLineSegmentDetector()
    lines_raw = lsd.detect(gray)[0]
    all_lines: list[np.ndarray] = []
    if lines_raw is not None:
        all_lines.extend(np.asarray(lines_raw).reshape(-1, 4))
    if enhanced_gray is not None:
        lines_enh = lsd.detect(enhanced_gray)[0]
        if lines_enh is not None:
            all_lines.extend(np.asarray(lines_enh).reshape(-1, 4))

    segments: list[dict] = []
    if not all_lines:
        return segments

    for line in all_lines:
        x1, y1, x2, y2 = (float(v) for v in line)
        ax, ay = frame.pixel_to_metre(x1, y1)
        bx, by = frame.pixel_to_metre(x2, y2)
        length = math.hypot(bx - ax, by - ay)
        if length < MIN_SEGMENT_M:
            continue
        midpoint = Point((ax + bx) / 2, (ay + by) / 2)
        if not footprint.buffer(1.5).contains(midpoint):
            continue

        if color_image is not None and not _plane_split_evidence(
            ax, ay, bx, by, color_image, frame
        ):
            continue

        angle = math.atan2(by - ay, bx - ax) % math.pi
        normal = np.array([-math.sin(angle), math.cos(angle)])
        offset = float(np.dot(normal, [ax, ay]))
        segments.append(
            {
                "mid": ((ax + bx) / 2, (ay + by) / 2),
                "angle": angle,
                "offset": offset,
                "length": length,
            }
        )
    return _drop_parallel_families(segments)


def _angle_mean_shift(segments: list[dict]) -> list[float]:
    """Mean-shift on segment angles (mod pi), weighted by segment length."""
    angles = np.array([segment["angle"] for segment in segments])
    weights = np.array([segment["length"] for segment in segments])
    bandwidth = math.radians(ANGLE_BANDWIDTH_DEG)
    shifted = angles.copy()
    for _ in range(40):
        # circular distance helper on the doubled angle to handle mod-pi wrap
        delta = shifted[:, None] - angles[None, :]
        delta = (delta + math.pi / 2) % math.pi - math.pi / 2
        kernel = np.exp(-0.5 * (delta / bandwidth) ** 2) * weights[None, :]
        new = (kernel * angles[None, :]).sum(axis=1) / kernel.sum(axis=1)
        # keep the mean in the same mod-pi branch as the current value
        new = shifted + ((new - shifted + math.pi / 2) % math.pi - math.pi / 2)
        if np.max(np.abs(new - shifted)) < 1e-6:
            shifted = new
            break
        shifted = new
    return [float(value) for value in shifted]


def _offset_mean_shift(segments: list[dict], angles: list[float]) -> list[float]:
    """Mean-shift on line offsets within each angle cluster."""
    offsets = np.array([segment["offset"] for segment in segments])
    weights = np.array([segment["length"] for segment in segments])
    angle_array = np.array(angles)
    shifted = offsets.copy()
    bandwidth = OFFSET_BANDWIDTH_M
    for _ in range(40):
        changed = False
        for index in range(len(shifted)):
            mask = np.abs(np.sin(angle_array - angle_array[index])) < math.sin(
                math.radians(ANGLE_BANDWIDTH_DEG * 1.5)
            )
            if mask.sum() < 2:
                continue
            delta = shifted[index] - offsets[mask]
            kernel = np.exp(-0.5 * (delta / bandwidth) ** 2) * weights[mask]
            if kernel.sum() <= 0:
                continue
            new = float((kernel * offsets[mask]).sum() / kernel.sum())
            if abs(new - shifted[index]) > 1e-6:
                changed = True
                shifted[index] = new
        if not changed:
            break
    return [float(value) for value in shifted]


def _snap_to_architectural_grid(
    angles: list[float],
    dominant_axis_rad: float | None,
    snap_tolerance_rad: float = math.radians(7.5),
) -> list[float]:
    """Snaps angles close to the dominant building axis (or its 90/45 deg multiples) to exact grid orientations."""
    if dominant_axis_rad is None:
        return angles
    d0 = dominant_axis_rad % (math.pi / 2)
    canonical = [
        d0 % math.pi,
        (d0 + math.pi / 4) % math.pi,
        (d0 + math.pi / 2) % math.pi,
        (d0 + 3 * math.pi / 4) % math.pi,
    ]
    snapped = []
    for ang in angles:
        best_target = ang
        min_diff = snap_tolerance_rad
        for c in canonical:
            diff = abs((ang - c + math.pi / 2) % math.pi - math.pi / 2)
            if diff < min_diff:
                min_diff = diff
                best_target = c
        snapped.append(best_target)
    return snapped


def _regularize(
    segments: list[dict],
    dominant_axis_rad: float | None = None,
) -> tuple[list[dict], list[float]]:
    if not segments:
        return [], []
    angles = _angle_mean_shift(segments)
    if dominant_axis_rad is not None:
        angles = _snap_to_architectural_grid(angles, dominant_axis_rad)
    offsets = _offset_mean_shift(segments, angles)
    regularized = []
    for segment, angle, offset in zip(segments, angles, offsets, strict=True):
        normal = np.array([-math.sin(angle), math.cos(angle)])
        direction = np.array([math.cos(angle), math.sin(angle)])
        anchor = normal * offset
        # keep the segment where it was along the line: project its midpoint
        midpoint = np.array(segment["mid"])
        center = anchor + direction * float(np.dot(direction, midpoint - anchor))
        half = segment["length"] / 2.0
        regularized.append(
            {
                "p1": tuple(center - direction * half),
                "p2": tuple(center + direction * half),
                "angle": angle,
                "anchor": tuple(center),
                "direction": tuple(direction),
                "length": segment["length"],
            }
        )
    return regularized, angles


def _intersections_for(segment: dict, others: list[dict]) -> list[float]:
    """Distances (signed, along the segment direction) to intersections."""
    p = np.array(segment["anchor"])
    d = np.array(segment["direction"])
    hits: list[float] = []
    for other in others:
        if other is segment:
            continue
        q = np.array(other["anchor"])
        e = np.array(other["direction"])
        denominator = d[0] * e[1] - d[1] * e[0]
        if abs(denominator) < 1e-9:
            continue
        delta = q - p
        t = (delta[0] * e[1] - delta[1] * e[0]) / denominator
        hits.append(float(t))
    return hits


def _propagate(segments: list[dict], domain: Polygon, k: int) -> list[LineString]:
    """Extend each regularized segment to its k-th intersection, clipped to the domain."""
    minx, miny, maxx, maxy = domain.bounds
    big = max(maxx - minx, maxy - miny) * 4.0
    rays: list[LineString] = []
    for segment in segments:
        p = np.array(segment["anchor"])
        d = np.array(segment["direction"])
        hits = sorted(_intersections_for(segment, segments))
        forward = [value for value in hits if value > 1e-6]
        backward = [-value for value in hits if value < -1e-6]
        limit_forward = forward[k - 1] if len(forward) >= k else big
        limit_backward = backward[k - 1] if len(backward) >= k else big
        a = p - d * min(limit_backward, big)
        b = p + d * min(limit_forward, big)
        line = LineString([tuple(a), tuple(b)])
        clipped = line.intersection(domain.buffer(1.0))
        # A roofline crossing a concave wing can produce several segments.
        # Keep each rather than discarding all evidence on multi-wing buildings.
        pieces = [clipped] if isinstance(clipped, LineString) else getattr(clipped, "geoms", [])
        rays.extend(piece for piece in pieces if isinstance(piece, LineString) and piece.length > 0.5)
    return rays


def _partition_faces(
    rays: list[LineString], footprint: Polygon
) -> list[Polygon]:
    """Tiles the footprint with arrangement cells, dropping nothing.

    Every non-empty inside-piece survives (split multis included): small cells
    are absorbed into their longest-edge neighbour by _coalesce_slivers, and
    isolated crumbs render as tiny flat faces. Dropping small/low-cover cells
    here punched holes in the mesh instead — measured 35 m² lost on one
    339 m² roof (304/339 coverage) with zero loss at any later stage.
    """
    boundary = footprint.exterior
    arrangement = unary_union(rays + [LineString(boundary.coords)])
    pieces: list[Polygon] = []
    for face in polygonize(arrangement):
        intersection = face.intersection(footprint)
        if intersection.is_empty:
            continue
        geoms = (
            list(getattr(intersection, "geoms", [intersection]))
            if intersection.geom_type == "MultiPolygon"
            else [intersection]
        )
        for part in geoms:
            if part.geom_type != "Polygon" or part.area <= 0:
                continue
            pieces.append(part.buffer(0))
    return [piece for piece in pieces if piece.geom_type == "Polygon"]


def _coalesce_slivers(faces: list[Polygon], min_area: float = 6.0) -> list[Polygon]:
    """Absorb small arrangement slivers into the neighbour they share most edge with."""
    faces = list(faces)
    merged = True
    while merged:
        merged = False
        for index, face in enumerate(faces):
            if face.area >= min_area:
                continue
            best, best_shared = None, 0.0
            for other_index, other in enumerate(faces):
                if other_index == index:
                    continue
                shared = face.intersection(other)
                shared_length = shared.length if not shared.is_empty else 0.0
                if shared_length > best_shared:
                    best, best_shared = other_index, shared_length
            if best is None:
                continue
            faces[best] = faces[best].union(face)
            faces.pop(index)
            merged = True
            break
    return faces


def _plane_fit(depth: np.ndarray | None, frame: Frame, polygon: Polygon) -> dict | None:
    if depth is None:
        return None
    minx, miny, maxx, maxy = polygon.bounds
    step = DEPTH_SAMPLE_STEP_M
    samples: list[tuple[float, float, float]] = []
    gy = miny
    while gy <= maxy:
        gx = minx
        while gx <= maxx:
            point = Point(gx, gy)
            if polygon.contains(point):
                px, py = frame.metre_to_pixel(gx, gy)
                ix, iy = int(round(px)), int(round(py))
                if 0 <= iy < depth.shape[0] and 0 <= ix < depth.shape[1]:
                    samples.append((gx, gy, float(depth[iy, ix])))
            gx += step
        gy += step
    if len(samples) < 8:
        return None
    matrix = np.array([[x, y, 1.0] for x, y, _ in samples])
    values = np.array([v for _, _, v in samples])
    coeffs, *_ = np.linalg.lstsq(matrix, values, rcond=None)
    a, b, c = (float(v) for v in coeffs)
    return {
        "a": a,
        "b": b,
        "c": c,
        "slope_deg": round(math.degrees(math.atan(math.hypot(a, b))), 1),
        "mean_depth": round(float(values.mean()), 4),
        "samples": len(samples),
    }


def _plane_value(plane: dict, x: float, y: float) -> float:
    return plane["a"] * x + plane["b"] * y + plane["c"]


def _group_planes(faces: list[dict]) -> list[list[int]]:
    """Union-find over adjacent faces with compatible plane fits."""
    parent = list(range(len(faces)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, face_a in enumerate(faces):
        for j in range(i + 1, len(faces)):
            face_b = faces[j]
            shared = face_a["polygon"].boundary.intersection(face_b["polygon"].boundary)
            if shared.length < 0.1:
                continue
            plane_a, plane_b = face_a["plane"], face_b["plane"]
            if plane_a is None or plane_b is None:
                continue
            normal_a = np.array([-plane_a["a"], -plane_a["b"], 1.0])
            normal_b = np.array([-plane_b["a"], -plane_b["b"], 1.0])
            normal_a /= np.linalg.norm(normal_a)
            normal_b /= np.linalg.norm(normal_b)
            angle = math.degrees(math.acos(min(1.0, abs(float(np.dot(normal_a, normal_b))))))
            # Evaluate both planes at the SAME points. Comparing separate
            # centroids incorrectly splits a continuous sloping roof.
            probes = [shared.interpolate(f, normalized=True) for f in (0, 0.5, 1)]
            offset_delta = max(abs(
                _plane_value(plane_a, point.x, point.y)
                - _plane_value(plane_b, point.x, point.y)
            ) for point in probes)
            if angle <= PLANE_ANGLE_TOL_DEG and offset_delta <= PLANE_OFFSET_TOL:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(faces)):
        groups[find(index)].append(index)
    return list(groups.values())


def _rooflines(faces: list[dict], groups: list[list[int]], footprint: Polygon) -> list[dict]:
    group_of = {}
    for group_index, members in enumerate(groups):
        for member in members:
            group_of[member] = group_index
    lines: dict[tuple[float, float, float, float], dict] = {}
    for i, face_a in enumerate(faces):
        for j in range(i + 1, len(faces)):
            if group_of.get(i) == group_of.get(j):
                continue
            shared = face_a["polygon"].intersection(faces[j]["polygon"])
            if shared.is_empty or shared.length < 0.5:
                continue
            if shared.geom_type == "MultiLineString":
                parts = list(shared.geoms)
            elif shared.geom_type == "LineString":
                parts = [shared]
            else:
                continue
            for part in parts:
                key = tuple(
                    round(value, 3)
                    for value in (list(part.coords[0]) + list(part.coords[-1]))
                )
                lines[key] = {"coords": [list(part.coords[0]), list(part.coords[-1])]}
    return list(lines.values())


def _triangulate_z(
    coords: list[tuple[float, float]], z_by_key: dict[tuple[float, float], float]
) -> list[list[list[float]]]:
    """Triangulates a part and assigns each vertex its reconciled plane height."""
    if len(coords) < 3:
        return []
    polygon = Polygon(coords)
    triangles = []
    for triangle in constrained_delaunay_triangles(polygon).geoms:
        if not polygon.buffer(1e-8).covers(triangle):
            continue
        points = list(triangle.exterior.coords)[:3]
        tri = []
        for x, y in points:
            z = z_by_key.get((round(x, 2), round(y, 2)))
            if z is None:
                tri = []
                break
            tri.append([float(x), float(y), z])
        if tri:
            triangles.append(tri)
    return triangles


def _reconcile_part_heights(
    parts: list[dict],
    footprint: Polygon,
    eave: float,
    ridge: float,
    iterations: int = 3,
) -> None:
    """Share vertex heights without repeatedly damping the measured planes.

    Node T-junctions before reconciliation. A gable ridge can terminate on the
    footprint boundary; only eave vertices, not those supported ridge endpoints,
    are pinned down. Preserve steps only with confident, nearly parallel fits.
    ``iterations`` remains accepted for callers of the previous implementation.
    """
    del iterations
    original_planes = [dict(p["plane"]) if p["plane"] else None for p in parts]
    all_vertices = {tuple(point) for part in parts for point in part["coords"]}
    for part in parts:
        coords = part["coords"]
        noded = []
        for start, end in zip(coords, coords[1:] + coords[:1], strict=True):
            edge = LineString([start, end])
            if edge.length < 1e-6:
                continue
            intermediate = [v for v in all_vertices
                            if edge.distance(Point(v)) < 1e-6
                            and 1e-6 < edge.project(Point(v)) < edge.length - 1e-6]
            noded.extend([start, *sorted(intermediate, key=lambda v: edge.project(Point(v)))])
        part["coords"] = noded
        part["ring_meters"] = [[float(x), float(y)] for x, y in noded]

    samples: dict[tuple[float, float], list[tuple[int, float]]] = defaultdict(list)
    for index, part in enumerate(parts):
        plane = original_planes[index]
        for x, y in part["coords"]:
            value = _plane_value(plane, x, y) if plane else eave
            samples[(round(x, 2), round(y, 2))].append((index, min(max(value, eave), ridge)))

    heights = [{} for _ in parts]
    for key, values in samples.items():
        planes = [original_planes[i] for i, _ in values if original_planes[i] is not None]
        z = float(np.median([v for _, v in values]))
        on_boundary = footprint.boundary.distance(Point(key)) < 0.025
        # Opposing roof gradients support a real ridge/apex at a gable end.
        ridge_end = any(
            p["a"] * q["a"] + p["b"] * q["b"] < -0.01
            for i, p in enumerate(planes) for q in planes[i + 1:]
        ) and min(v for _, v in values) > eave + 0.35
        step = len(planes) > 1 and max(v for _, v in values) - min(v for _, v in values) > 0.9
        step = step and all(p.get("inlier_ratio", 0) >= 0.8 for p in planes)
        step = step and all(math.hypot(p["a"] - planes[0]["a"], p["b"] - planes[0]["b"]) < 0.15 for p in planes)
        for index, value in values:
            heights[index][key] = value if step else (eave if on_boundary and not ridge_end else z)

    for index, part in enumerate(parts):
        part["ring_z"] = [round(heights[index][(round(x, 2), round(y, 2))], 2)
                          for x, y in part["coords"]]
        matrix = np.array([[x, y, 1.0] for x, y in part["coords"]])
        if len(matrix) >= 3 and np.linalg.matrix_rank(matrix) == 3:
            coefficients, *_ = np.linalg.lstsq(matrix, part["ring_z"], rcond=None)
            a, b, c = (float(v) for v in coefficients)
            part["plane"] = {**(original_planes[index] or {}), "a": a, "b": b, "c": c}


def _triangulate(face: Polygon, heights) -> list[list[list[float]]]:
    triangles = []
    for triangle in constrained_delaunay_triangles(face).geoms:
        if not face.contains(triangle.representative_point()):
            continue
        coords = []
        for x, y in list(triangle.exterior.coords)[:3]:
            z = heights(x, y)
            coords.append([round(x, 2), round(y, 2), round(z, 3)])
        triangles.append(coords)
    return triangles


class RooflineExtractor:
    """Reusable roofline extractor: one imagery fetcher and depth model per process.

    The depth model is loaded lazily on first use and reused for every record.
    """

    def __init__(
        self,
        *,
        zoom: int = 18,
        provider: str = "google",
        imagery_offset: int = 1,
        out_px: int = 576,
        margin_px: int = 256,
        use_ortho: bool = True,
        use_pointcloud: bool = True,
    ):
        self.zoom = zoom
        self.fetcher = ImageryFetcher(
            provider=provider,
            imagery_offset=imagery_offset,
            out_px=out_px,
            margin_px=margin_px,
        )
        self.use_ortho = use_ortho
        self.use_pointcloud = use_pointcloud
        self.compositor = (
            OrthoCompositor(
                zoom=zoom, out_px=out_px, margin_px=margin_px, imagery_offset=imagery_offset
            )
            if use_ortho
            else None
        )
        self._depth_estimator = None
        self._depth_attempted = False
        self.feedback = latest_feedback()

    def _depth(self, window) -> np.ndarray | None:
        if not self._depth_attempted:
            self._depth_attempted = True
            try:
                config = PipelineConfig(
                    zooms=[self.zoom],
                    out_path=Path("/tmp/rooflines-unused.mbtiles"),
                    with_heights=True,
                )
                context = build_context(config)
                self._depth_estimator = context.depth_estimator
            except Exception as error:  # noqa: BLE001
                print(f"depth unavailable: {error}", file=sys.stderr)
        if self._depth_estimator is None:
            return None
        return self._depth_estimator.estimate(window.image)

    def extract(
        self,
        record: dict,
        *,
        debug_path: Path | None = None,
        depth: np.ndarray | None = None,
        rejected_lines: list[dict] | None = None,
    ) -> dict:
        if rejected_lines is None:
            rejected_lines = rejected_cuts_for(record, self.feedback.get(int(record["address_id"]), {}))
        return _extract_rooflines(
            self,
            record,
            debug_path=debug_path,
            depth=depth,
            rejected_lines=rejected_lines,
        )


def _part_edge_height(part: dict, point) -> float:
    """Use emitted vertex heights so diagnostic ridges sit on the rendered mesh."""
    coords, heights = part["coords"], part.get("ring_z", [])
    best_distance, best_height = math.inf, None
    if len(coords) == len(heights):
        for i, start in enumerate(coords):
            end = coords[(i + 1) % len(coords)]
            edge = LineString([start, end])
            if edge.length < 1e-9:
                continue
            distance = edge.distance(Point(point))
            if distance < best_distance:
                t = edge.project(Point(point)) / edge.length
                best_distance = distance
                best_height = heights[i] * (1 - t) + heights[(i + 1) % len(coords)] * t
    if best_height is not None and best_distance < 0.25:
        return best_height
    return _plane_value(part["plane"], *point)


def _classify_roofline(
    coords: list[list[float]],
    parts: list[dict],
) -> str:
    """Classifies a line separating parts into structural types:
    ridge, hip, step_eave, valley, or eave.
    """
    p1, p2 = coords[0], coords[1]
    mid_x = (p1[0] + p2[0]) / 2.0
    mid_y = (p1[1] + p2[1]) / 2.0
    mid_point = Point(mid_x, mid_y)

    adj = []
    edge = LineString(coords)
    for part in parts:
        poly = Polygon(part["coords"])
        if poly.boundary.distance(mid_point) < .025 and edge.intersection(poly.boundary.buffer(.025)).length > edge.length * .95:
            adj.append(part)

    if len(adj) < 2:
        return "eave"

    part_a, part_b = adj[0], adj[1]
    plane_a = part_a.get("plane")
    plane_b = part_b.get("plane")
    if not plane_a or not plane_b:
        return "ridge"

    za = plane_a["a"] * mid_x + plane_a["b"] * mid_y + plane_a["c"]
    zb = plane_b["a"] * mid_x + plane_b["b"] * mid_y + plane_b["c"]

    if abs(za - zb) > 0.65:
        return "step_eave"

    z_mid = (za + zb) / 2.0

    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return "ridge"
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux

    z1 = (
        plane_a["a"] * p1[0] + plane_a["b"] * p1[1] + plane_a["c"]
        + plane_b["a"] * p1[0] + plane_b["b"] * p1[1] + plane_b["c"]
    ) / 2.0
    z2 = (
        plane_a["a"] * p2[0] + plane_a["b"] * p2[1] + plane_a["c"]
        + plane_b["a"] * p2[0] + plane_b["b"] * p2[1] + plane_b["c"]
    ) / 2.0
    slope_deg = math.degrees(math.atan2(abs(z2 - z1), length))

    # Dihedral concavity test: sample points offset perpendicular to line
    eps = 0.2
    q1 = Point(mid_x + nx * eps, mid_y + ny * eps)
    poly_a = Polygon(part_a["coords"])
    if poly_a.contains(q1):
        qa = (mid_x + nx * eps, mid_y + ny * eps)
        qb = (mid_x - nx * eps, mid_y - ny * eps)
    else:
        qa = (mid_x - nx * eps, mid_y - ny * eps)
        qb = (mid_x + nx * eps, mid_y + ny * eps)

    z_qa = plane_a["a"] * qa[0] + plane_a["b"] * qa[1] + plane_a["c"]
    z_qb = plane_b["a"] * qb[0] + plane_b["b"] * qb[1] + plane_b["c"]
    z_adj = (z_qa + z_qb) / 2.0

    if z_mid < z_adj - 0.01:
        return "valley"
    elif slope_deg <= 5.0:
        return "ridge"
    else:
        return "hip"


def _cambridge_architectural_parts_and_lines(
    frame: Frame,
) -> tuple[list[dict], list[dict], Polygon]:
    """Manually authored demonstration template for 27 Cambridge Road.

    Retained for explicit reference/visual regression use only. This is not an
    inferred roof or a surveyed height label; automatic extraction never selects
    it by address ID. Callers must request manual_template='cambridge-demo-v1'.
    """
    theta_deg = 35.0
    theta_rad = math.radians(theta_deg)
    cos_t = math.cos(theta_rad)
    sin_t = math.sin(theta_rad)

    x0 = -10.5
    y0 = 10.8

    def uv_to_xy(u: float, v: float) -> list[float]:
        xm = x0 + u * cos_t + v * sin_t
        ym = y0 - u * sin_t + v * cos_t
        return [round(xm, 2), round(ym, 2)]

    L_main = 20.2
    w_north = 4.3
    w_south = 4.3

    west_nw = uv_to_xy(-3.6, w_north)
    west_sw = uv_to_xy(-3.6, -w_south)
    west_ridge_apex = uv_to_xy(0.0, 0.0)

    main_ridge_east = uv_to_xy(L_main, 0.0)

    L_east = 9.4
    w_east_in = 4.3
    w_east_out = 4.3
    east_ridge_apex = uv_to_xy(L_main, L_east)
    east_inner_nw = uv_to_xy(L_main - w_east_in, L_east + 3.2)
    east_outer_ne = uv_to_xy(L_main + w_east_out, L_east + 3.2)
    court_notch = uv_to_xy(L_main - w_east_in, w_north)
    east_outer_se = uv_to_xy(L_main + w_east_out, -w_south)

    u_bay_start = 7.0
    u_bay_end = 11.8
    u_bay_mid = (u_bay_start + u_bay_end) / 2
    bay_depth = 3.4

    bay_notch_w = uv_to_xy(u_bay_start, -w_south)
    bay_sw = uv_to_xy(u_bay_start, -w_south - bay_depth)
    bay_se = uv_to_xy(u_bay_end, -w_south - bay_depth)
    bay_notch_e = uv_to_xy(u_bay_end, -w_south)
    bay_ridge_apex = uv_to_xy(u_bay_mid, -w_south - bay_depth + 1.8)
    main_ridge_bay_cross = uv_to_xy(u_bay_mid, 0.0)

    # Garage pavilion:
    u_g_west = -6.2
    u_g_east = -1.0
    u_g_mid = -3.6
    v_g_south = -11.5
    v_g_apex_s = -9.0
    v_g_apex_n = -6.8
    v_g_north = -w_south

    garage_sw = uv_to_xy(u_g_west, v_g_south)
    garage_se = uv_to_xy(u_g_east, v_g_south)
    garage_nw = uv_to_xy(u_g_west, v_g_north)
    garage_court_notch = uv_to_xy(u_g_east, -w_south)
    garage_ridge_s = uv_to_xy(u_g_mid, v_g_apex_s)
    garage_ridge_n = uv_to_xy(u_g_mid, v_g_apex_n)
    garage_step = west_sw

    facets = [
        # Garage facets
        {"name": "Garage South Hip", "kind": "hip_end", "pitch_deg": 23.0, "azimuth_deg": 215.0, "coords": [garage_se, garage_ridge_s, garage_sw], "z": [2.7, 3.9, 2.7]},
        {"name": "Garage North Hip", "kind": "hip_end", "pitch_deg": 23.0, "azimuth_deg": 35.0, "coords": [garage_nw, garage_ridge_n, garage_court_notch], "z": [2.7, 3.9, 2.7]},
        {"name": "Garage West Pitch", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 305.0, "coords": [garage_sw, garage_ridge_s, garage_ridge_n, garage_nw], "z": [2.7, 3.9, 3.9, 2.7]},
        {"name": "Garage East Pitch", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 125.0, "coords": [garage_se, garage_court_notch, garage_ridge_n, garage_ridge_s], "z": [2.7, 2.7, 3.9, 3.9]},
        # Main house facets
        {"name": "West Hip End", "kind": "hip_end", "pitch_deg": 22.5, "azimuth_deg": 305.0, "coords": [garage_step, west_nw, west_ridge_apex], "z": [2.7, 2.7, 4.8]},
        {"name": "Main Wing North", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 35.0, "coords": [west_nw, court_notch, main_ridge_east, west_ridge_apex], "z": [2.7, 2.7, 4.8, 4.8]},
        {"name": "Main Wing South-West", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 215.0, "coords": [garage_court_notch, garage_step, west_ridge_apex, main_ridge_bay_cross, bay_notch_w], "z": [2.7, 2.7, 4.8, 4.8, 2.7]},
        {"name": "Front Bay West", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 305.0, "coords": [bay_notch_w, main_ridge_bay_cross, bay_ridge_apex, bay_sw], "z": [2.7, 4.8, 4.8, 2.7]},
        {"name": "Front Bay South Hip", "kind": "hip_end", "pitch_deg": 23.0, "azimuth_deg": 215.0, "coords": [bay_sw, bay_ridge_apex, bay_se], "z": [2.7, 4.8, 2.7]},
        {"name": "Front Bay East", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 125.0, "coords": [bay_se, bay_ridge_apex, main_ridge_bay_cross, bay_notch_e], "z": [2.7, 4.8, 4.8, 2.7]},
        {"name": "Main Wing South-East", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 215.0, "coords": [bay_notch_e, main_ridge_bay_cross, main_ridge_east, east_outer_se], "z": [2.7, 4.8, 4.8, 2.7]},
        {"name": "East Wing Inner (Courtyard)", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 305.0, "coords": [court_notch, east_inner_nw, east_ridge_apex, main_ridge_east], "z": [2.7, 2.7, 4.8, 4.8]},
        {"name": "East Wing North Hip", "kind": "hip_end", "pitch_deg": 23.0, "azimuth_deg": 35.0, "coords": [east_inner_nw, east_outer_ne, east_ridge_apex], "z": [2.7, 2.7, 4.8]},
        {"name": "East Wing Outer", "kind": "main_pitch", "pitch_deg": 22.5, "azimuth_deg": 125.0, "coords": [main_ridge_east, east_ridge_apex, east_outer_ne, east_outer_se], "z": [4.8, 4.8, 2.7, 2.7]},
    ]

    parts = []
    for i, f in enumerate(facets):
        coords = f["coords"]
        z = f["z"]
        poly = Polygon(coords)
        pts = [[c[0], c[1], 1.0] for c in coords]
        coeffs, *_ = np.linalg.lstsq(np.array(pts), np.array(z), rcond=None)
        a, b, c = [float(v) for v in coeffs]

        z_map = {(round(c[0], 2), round(c[1], 2)): zv for c, zv in zip(coords, z, strict=True)}
        tris = []
        for tri in constrained_delaunay_triangles(poly).geoms:
            if poly.contains(tri.representative_point()):
                tri_pts = []
                for tx, ty in list(tri.exterior.coords)[:3]:
                    tz = z_map.get((round(tx, 2), round(ty, 2)), a * tx + b * ty + c)
                    tri_pts.append([round(tx, 2), round(ty, 2), round(tz, 2)])
                tris.append(tri_pts)

        p_deg = f["pitch_deg"]
        pitch_rad = math.radians(p_deg)
        a2d = round(poly.area, 1)
        a3d = round(a2d / math.cos(pitch_rad), 1)
        rise = round(math.tan(pitch_rad) * 12.0, 1)
        lonlat = [frame.metre_to_lonlat(x, y) for x, y in coords]

        parts.append(
            {
                "index": i,
                "name": f["name"],
                "kind": f["kind"],
                "area_m2": a2d,
                "area_3d_m2": a3d,
                "pitch_deg": p_deg,
                "slope_deg": p_deg,
                "pitch_ratio": f"{rise}:12",
                "azimuth_deg": f["azimuth_deg"],
                "coords": coords,
                "ring": [[round(x, 2), round(y, 2)] for x, y in coords],
                "ring_meters": [[round(x, 2), round(y, 2)] for x, y in coords],
                "ring_lonlat": [[round(lon, 7), round(lat, 7)] for lon, lat in lonlat],
                "ring_z": z,
                "z": z,
                "z_range": [min(z), max(z)],
                "triangles": tris,
                "plane": {"a": a, "b": b, "c": c},
                "eave_m": 2.7,
                "ridge_m": 4.8 if "Garage" not in f["name"] else 3.9,
            }
        )

    lines_def = [
        # Main ridges
        {"kind": "ridge", "p1": west_ridge_apex, "p2": main_ridge_east, "z": [4.8, 4.8]},
        {"kind": "ridge", "p1": main_ridge_east, "p2": east_ridge_apex, "z": [4.8, 4.8]},
        {"kind": "ridge", "p1": main_ridge_bay_cross, "p2": bay_ridge_apex, "z": [4.8, 4.8]},
        {"kind": "ridge", "p1": garage_ridge_n, "p2": garage_ridge_s, "z": [3.9, 3.9]},
        # Main house hips
        {"kind": "hip", "p1": west_nw, "p2": west_ridge_apex, "z": [2.7, 4.8]},
        {"kind": "hip", "p1": garage_step, "p2": west_ridge_apex, "z": [2.7, 4.8]},
        {"kind": "hip", "p1": bay_sw, "p2": bay_ridge_apex, "z": [2.7, 4.8]},
        {"kind": "hip", "p1": bay_se, "p2": bay_ridge_apex, "z": [2.7, 4.8]},
        {"kind": "hip", "p1": east_inner_nw, "p2": east_ridge_apex, "z": [2.7, 4.8]},
        {"kind": "hip", "p1": east_outer_ne, "p2": east_ridge_apex, "z": [2.7, 4.8]},
        {"kind": "hip", "p1": east_outer_se, "p2": main_ridge_east, "z": [2.7, 4.8]},
        # Garage hips
        {"kind": "hip", "p1": garage_sw, "p2": garage_ridge_s, "z": [2.7, 3.9]},
        {"kind": "hip", "p1": garage_se, "p2": garage_ridge_s, "z": [2.7, 3.9]},
        {"kind": "hip", "p1": garage_nw, "p2": garage_ridge_n, "z": [2.7, 3.9]},
        {"kind": "hip", "p1": garage_court_notch, "p2": garage_ridge_n, "z": [2.7, 3.9]},
        # Valleys
        {"kind": "valley", "p1": court_notch, "p2": main_ridge_east, "z": [2.7, 4.8]},
        {"kind": "valley", "p1": bay_notch_w, "p2": main_ridge_bay_cross, "z": [2.7, 4.8]},
        {"kind": "valley", "p1": bay_notch_e, "p2": main_ridge_bay_cross, "z": [2.7, 4.8]},
    ]
    lines_out = []
    for line_item in lines_def:
        p1, p2 = line_item["p1"], line_item["p2"]
        z1, z2 = line_item["z"][0], line_item["z"][1]
        l2d = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        l3d = math.sqrt(l2d**2 + (z2 - z1)**2)
        slope = math.degrees(math.atan2(abs(z2 - z1), max(1e-6, l2d)))
        lonlat = [frame.metre_to_lonlat(p1[0], p1[1]), frame.metre_to_lonlat(p2[0], p2[1])]
        lines_out.append(
            {
                "kind": line_item["kind"],
                "line_meters": [
                    [round(p1[0], 2), round(p1[1], 2)],
                    [round(p2[0], 2), round(p2[1], 2)],
                ],
                "line_lonlat": [
                    [round(lon, 7), round(lat, 7)] for lon, lat in lonlat
                ],
                "line_z": [round(z1, 2), round(z2, 2)],
                "length_2d_m": round(l2d, 2),
                "length_3d_m": round(l3d, 2),
                "slope_deg": round(slope, 1),
            }
        )

    footprint_coords = [
        west_nw,
        court_notch,
        east_inner_nw,
        east_outer_ne,
        east_outer_se,
        bay_notch_e,
        bay_se,
        bay_sw,
        bay_notch_w,
        garage_court_notch,
        garage_se,
        garage_sw,
        garage_nw,
        garage_step,
    ]
    footprint = Polygon(footprint_coords)

    return parts, lines_out, footprint


def _matches_rejected_line(coords, rejected_lines) -> bool:
    """Match direction and overlap, not midpoint: crossing ridges can share a midpoint."""
    line = LineString(coords)
    if line.length < 1e-6:
        return False
    start, end = np.array(coords[0]), np.array(coords[-1])
    direction = (end - start) / np.linalg.norm(end - start)
    for rejected in rejected_lines or []:
        points = rejected.get("line_meters", rejected) if isinstance(rejected, dict) else rejected
        if len(points) != 2:
            continue
        other = LineString(points)
        vector = np.array(points[1]) - np.array(points[0])
        length = float(np.linalg.norm(vector))
        if length < 1e-6 or abs(float(np.dot(direction, vector / length))) < math.cos(math.radians(10)):
            continue
        overlap = other.intersection(line.buffer(0.6, cap_style=2)).length
        if overlap >= 0.6 * min(line.length, other.length):
            return True
    return False


def _extract_rooflines(
    extractor: RooflineExtractor,
    record: dict,
    *,
    debug_path: Path | None = None,
    depth: np.ndarray | None = None,
    rejected_lines: list[dict] | None = None,
) -> dict:
    zoom = extractor.zoom
    centroid = record["centroid"]
    n = 2**zoom
    tile_x = int((centroid["lon"] + 180) / 360 * n)
    lat_rad = math.radians(centroid["lat"])
    tile_y = int(
        (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n
    )
    window: ImageryWindow
    if extractor.compositor is not None:
        try:
            window, _ = extractor.compositor.window(tile_x, tile_y)
        except Exception as error:  # noqa: BLE001
            print(f"ortho composite failed, using single provider: {error}", file=sys.stderr)
            window = extractor.fetcher.window(zoom, tile_x, tile_y)
    else:
        window = extractor.fetcher.window(zoom, tile_x, tile_y)
    frame = Frame(record, window, zoom, tile_x, tile_y)
    footprint = frame.ring_meters(record)
    if not footprint.is_valid:
        footprint = footprint.buffer(0)

    color_np = np.asarray(window.image.convert("RGB"))
    gray = composite_gray(window.image)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced_gray = clahe.apply(gray)

    footprint_override = None
    if record.get("manual_template") == "cambridge-demo-v1":
        part_results, rooflines_out, footprint_override = _cambridge_architectural_parts_and_lines(frame)
        if rejected_lines:
            rooflines_out = [line for line in rooflines_out
                             if not _matches_rejected_line(line["line_meters"], rejected_lines)]
        face_results = [{"polygon": Polygon(p["coords"]), "plane": p["plane"]} for p in part_results]
        groups = [[i] for i in range(len(part_results))]
        faces = [p["polygon"] for p in face_results]
        regularized = []
        rays = []
        segments = []
    else:
        # Orient this building from its own image neighbourhood, not the road
        # or an unrelated roof near the centre of the stitched tile.
        pixels = np.asarray([frame.metre_to_pixel(x, y) for x, y in footprint.exterior.coords])
        lo, hi = pixels.min(axis=0), pixels.max(axis=0)
        dom_axis_px = detect_dominant_building_axis(color_np, region_box=(int(lo[0]), int(lo[1]), int(hi[0]), int(hi[1])))
        dom_axis_rad = (math.pi - math.radians(dom_axis_px)) % (math.pi / 2)
        segments = _detect_segments(
            gray, frame, footprint, color_image=color_np, enhanced_gray=enhanced_gray
        )
        regularized, angles = _regularize(segments, dominant_axis_rad=dom_axis_rad)

        if rejected_lines:
            regularized = [segment for segment in regularized
                           if not _matches_rejected_line([segment["p1"], segment["p2"]], rejected_lines)]

        rays = _propagate(regularized, footprint, K_INTERSECTIONS)
        if rejected_lines:
            rays = [ray for ray in rays if not _matches_rejected_line(list(ray.coords), rejected_lines)]
        faces = _coalesce_slivers(_partition_faces(rays, footprint))

        if depth is None:
            depth = extractor._depth(window)

        # Height mapping: anchor the footprint's depth range to its eave/ridge.
        eave = float(record.get("eave_height") or 3.0)
        ridge = float(record.get("ridge_height") or max(eave + 1.5, 6.0))

        cloud: PointCloud | None = None
        if extractor.use_pointcloud:
            cloud = build_point_cloud(depth, frame, footprint, eave, ridge)

        legacy_fits: dict[int, dict | None] = {}
        if cloud is None:
            # Fallback: relative-depth planes scaled to metres using the footprint range.
            for index, face in enumerate(faces):
                legacy_fits[index] = _plane_fit(depth, frame, face)
            values = [fit["mean_depth"] for fit in legacy_fits.values() if fit]
            depth_low = min(values) if values else 0.0
            depth_high = max(values) if values else 1.0
            span = max(1e-6, depth_high - depth_low)
            for fit in legacy_fits.values():
                if not fit:
                    continue
                fit["a"] = fit["a"] / span * (ridge - eave)
                fit["b"] = fit["b"] / span * (ridge - eave)
                fit["c"] = (fit["c"] - depth_low) / span * (ridge - eave) + eave

        face_results = []
        for index, face in enumerate(faces):
            plane = None
            if cloud is not None:
                plane = fit_plane_robust(points_in_polygon(cloud, face))
            if plane is None:
                plane = legacy_fits.get(index)
            face_results.append({"polygon": face, "plane": plane})
        groups = _group_planes(face_results)

        part_results = []
        for group_index, members in enumerate(groups):
            member_polys = [face_results[index]["polygon"] for index in members]
            union = unary_union(member_polys)
            polygons = (
                [union]
                if union.geom_type == "Polygon"
                else [
                    geom for geom in getattr(union, "geoms", []) if geom.geom_type == "Polygon"
                ]
            )
            plane = next(
                (face_results[index]["plane"] for index in members if face_results[index]["plane"]),
                None,
            )
            for polygon in polygons:
                if polygon.is_empty or polygon.area <= 0:
                    continue
                if cloud is not None:
                    plane = fit_plane_robust(points_in_polygon(cloud, polygon)) or plane
                coords = [(float(x), float(y)) for x, y in polygon.exterior.coords[:-1]]
                lonlat = [frame.metre_to_lonlat(x, y) for x, y in coords]
                part_results.append(
                    {
                        "index": group_index,
                        "area_m2": round(polygon.area, 1),
                        "ring_meters": [[float(x), float(y)] for x, y in coords],
                        "coords": coords,
                        "ring_lonlat": [[round(lon, 7), round(lat, 7)] for lon, lat in lonlat],
                        "plane": dict(plane) if plane else None,
                        "eave_m": round(eave, 2),
                        "ridge_m": round(ridge, 2),
                    }
                )

        # Reconcile plane heights so adjacent parts meet at shared vertices.
        _reconcile_part_heights(part_results, footprint, eave, ridge)
        # Noding can collapse a numerical sliver to two vertices. Let the
        # coverage audit request a prior instead of crashing the whole roof.
        part_results = [p for p in part_results if len(p["coords"]) >= 3
                        and Polygon(p["coords"]).is_valid and Polygon(p["coords"]).area > 1e-8]
        for part in part_results:
            part["ring_lonlat"] = [[round(lon, 7), round(lat, 7)]
                                   for lon, lat in (frame.metre_to_lonlat(x, y) for x, y in part["coords"])]
            z_by_key = {
                (round(x, 2), round(y, 2)): z
                for (x, y), z in zip(part["coords"], part["ring_z"], strict=True)
            }
            part["triangles"] = _triangulate_z(part["coords"], z_by_key)
            plane = part["plane"]
            part["slope_deg"] = (
                round(math.degrees(math.atan(math.hypot(plane["a"], plane["b"]))), 1)
                if plane
                else 0.0
            )
            slope = part["slope_deg"] or 0.0
            area = part["area_m2"]
            if slope <= 8.0:
                part["kind"] = "flat_surface"
            elif area < 20.0 and (part["ridge_m"] - part["eave_m"]) < 1.8:
                part["kind"] = "extension"
            else:
                part["kind"] = "main_pitch"

            pitch_deg = part["slope_deg"]
            part["pitch_deg"] = pitch_deg
            pitch_rad = math.radians(pitch_deg)
            cos_p = max(0.2, math.cos(pitch_rad))
            part["area_3d_m2"] = round(part["area_m2"] / cos_p, 1)
            rise = round(math.tan(pitch_rad) * 12.0, 1)
            part["pitch_ratio"] = f"{rise}:12"
            if plane and math.hypot(plane["a"], plane["b"]) > 0.01:
                az = (math.degrees(math.atan2(-plane["a"], -plane["b"])) + 360.0) % 360.0
                part["azimuth_deg"] = round(az, 1)
            else:
                part["azimuth_deg"] = 0.0
            rz = part.get("ring_z", [])
            part["z_range"] = [round(min(rz), 2), round(max(rz), 2)] if rz else [round(eave, 2), round(ridge, 2)]
            part["ring"] = part["ring_meters"]
            part["z"] = part.get("ring_z", [])

        rooflines = _rooflines(face_results, groups, footprint)
        rooflines_out = []
        for line in rooflines:
            lonlat = [frame.metre_to_lonlat(x, y) for x, y in line["coords"]]
            line_kind = _classify_roofline(line["coords"], part_results)
            p1 = line["coords"][0]
            p2 = line["coords"][1]
            l2d = math.hypot(p2[0] - p1[0], p2[1] - p1[1])

            z1_vals = []
            z2_vals = []
            for p in part_results:
                poly = Polygon(p["coords"])
                if poly.distance(Point(p1[0], p1[1])) < 0.25:
                    pl = p.get("plane")
                    if pl:
                        z1_vals.append(_part_edge_height(p, p1))
                if poly.distance(Point(p2[0], p2[1])) < 0.25:
                    pl = p.get("plane")
                    if pl:
                        z2_vals.append(_part_edge_height(p, p2))

            z1 = round(float(np.mean(z1_vals)), 2) if z1_vals else round(eave, 2)
            z2 = round(float(np.mean(z2_vals)), 2) if z2_vals else round(eave, 2)
            l3d = round(math.sqrt(l2d**2 + (z2 - z1)**2), 2)
            slope = round(math.degrees(math.atan2(abs(z2 - z1), max(1e-6, l2d))), 1)

            rooflines_out.append(
                {
                    "kind": line_kind,
                    "line_meters": [
                        [round(p1[0], 2), round(p1[1], 2)],
                        [round(p2[0], 2), round(p2[1], 2)],
                    ],
                    "line_lonlat": [
                        [round(lon, 7), round(lat, 7)] for lon, lat in lonlat
                    ],
                    "line_z": [z1, z2],
                    "length_2d_m": round(l2d, 2),
                    "length_3d_m": l3d,
                    "slope_deg": slope,
                }
            )

    if footprint_override is not None:
        footprint = footprint_override
    structure_audit = audit_roof(part_results, footprint, float(record.get("eave_height") or 3),
                                pitched=record.get("roof_type") != "flat")
    image_structure_audit = structure_audit
    geometry_source = "manual-template" if footprint_override is not None else "image-depth-estimate"
    prior_error = None
    roof_form_selection = None
    if footprint_override is None:
        # Depth values and independent vertex averages can yield twisted faces
        # or an entirely flat pitched roof. Keep those diagnostics, but use a
        # clearly labelled, validated roof-form hypothesis for the preview.
        try:
            reviewed_form = record.get("boundary_source") == "crm" and record.get("roof_form_source") == "reviewer"
            use_prior = not structure_audit["passed"] or reviewed_form
            candidate = structural_prior(footprint, float(record.get("eave_height") or 3),
                                         float(record.get("ridge_height") or 6),
                                         "flat" if record.get("roof_type") == "flat" else "hipped",
                                         regularize_eaves=record.get("boundary_source") != "crm") if use_prior else part_results
            if record.get("roof_type") != "flat":
                candidate, roof_form_selection = select_roof_ends(candidate, footprint, float(record.get("eave_height") or 3), color_np, frame, record)
                # Preserve an existing rectangular gable classification when
                # the imagery cannot distinguish the two families.
                if (use_prior and not reviewed_form and record.get("roof_type") == "gabled"
                        and not roof_form_selection["gable_ends"] and len(footprint.exterior.coords) == 5):
                    candidate = structural_prior(footprint, float(record.get("eave_height") or 3),
                                                 float(record.get("ridge_height") or 6), "gabled", regularize_eaves=False)
                    roof_form_selection["source"] = "existing-roof-type"
                    roof_form_selection["gable_ends"] = 2
                    for end in roof_form_selection["ends"]:
                        end["selected"] = "gable"
                        end["reason"] = "existing-roof-type"
            candidate_audit = audit_roof(candidate, footprint, float(record.get("eave_height") or 3),
                                         pitched=record.get("roof_type") != "flat")
            if not candidate_audit["passed"]:
                raise ValueError(f"prior failed geometry checks: {candidate_audit['issues']}")
            candidate_lines = list(shared_edges(candidate))
            if rejected_lines and any(_matches_rejected_line(line, rejected_lines) for line in candidate_lines):
                raise ValueError("prior conflicts with rejected reviewer rooflines")
            for part in candidate:
                part["ring_lonlat"] = [list(frame.metre_to_lonlat(x, y)) for x, y in part["coords"]]
                part["triangles"] = _triangulate_z(part["coords"], {
                    (round(x, 2), round(y, 2)): z for (x, y), z in zip(part["coords"], part["ring_z"], strict=True)
                })
            rooflines_out = []
            for line in candidate_lines:
                heights = []
                for point in line:
                    adjacent = [p for p in candidate if Polygon(p["coords"]).boundary.distance(Point(point)) < .001]
                    heights.append(float(np.mean([_part_edge_height(p, point) for p in adjacent])))
                length = math.dist(*line)
                rise = abs(heights[1] - heights[0])
                rooflines_out.append({"kind": _classify_roofline(line, candidate), "line_meters": line,
                                      "line_lonlat": [list(frame.metre_to_lonlat(*p)) for p in line],
                                      "line_z": heights, "length_2d_m": round(length, 2),
                                      "length_3d_m": round(math.hypot(length, rise), 2),
                                      "slope_deg": round(math.degrees(math.atan2(rise, length)), 1)})
            part_results = candidate
            structure_audit = candidate_audit
            geometry_source = "structural-prior" if use_prior else "image-depth-estimate"
        except Exception as error:  # noqa: BLE001
            prior_error = str(error)
    # A plane partition must not expand, smooth or replace the captured outline.
    # Centered building crop coordinates for pixel-perfect overlays
    footprint_coords = list(footprint.exterior.coords)
    footprint_px = [frame.metre_to_pixel(x, y) for x, y in footprint_coords]
    min_px_x = min(p[0] for p in footprint_px)
    max_px_x = max(p[0] for p in footprint_px)
    min_px_y = min(p[1] for p in footprint_px)
    max_px_y = max(p[1] for p in footprint_px)

    cx = (min_px_x + max_px_x) / 2.0
    cy = (min_px_y + max_px_y) / 2.0
    span = max(max_px_x - min_px_x, max_px_y - min_px_y)
    pad = max(35.0, span * 0.35)
    radius = span / 2.0 + pad

    w_img, h_img = window.image.size
    side = int(round(radius * 2.0))
    side = min(side, w_img, h_img)
    left = int(round(cx - side / 2.0))
    top = int(round(cy - side / 2.0))
    if left < 0:
        left = 0
    elif left + side > w_img:
        left = w_img - side
    if top < 0:
        top = 0
    elif top + side > h_img:
        top = h_img - side
    right = left + side
    bottom = top + side
    crop_w = side
    crop_h = side

    def to_crop_pixel(x_m: float, y_m: float) -> list[float]:
        px, py = frame.metre_to_pixel(x_m, y_m)
        u = round((px - left) / crop_w * 512.0, 2)
        v = round((py - top) / crop_h * 512.0, 2)
        return [u, v]


    ring_pixels = [to_crop_pixel(x, y) for x, y in footprint_coords[:-1]]
    for part in part_results:
        part["ring_pixels"] = [to_crop_pixel(x, y) for x, y in part["coords"]]
    for line in rooflines_out:
        p1 = to_crop_pixel(*line["line_meters"][0])
        p2 = to_crop_pixel(*line["line_meters"][1])
        line["line_pixels"] = [p1, p2]

    if debug_path is not None:
        clean_crop = window.image.crop((left, top, right, bottom)).resize(
            (512, 512), Image.Resampling.LANCZOS
        )
        clean_path = debug_path.parent / "site-clean.png"
        clean_crop.save(clean_path)

        overlay_crop = clean_crop.copy().convert("RGBA")
        draw = ImageDraw.Draw(overlay_crop, "RGBA")
        palette = [
            (56, 189, 248, 70),
            (251, 191, 36, 70),
            (167, 139, 250, 70),
            (248, 113, 113, 70),
            (52, 211, 153, 70),
            (96, 165, 250, 70),
            (245, 158, 11, 70),
            (168, 85, 247, 70),
        ]
        for index, part in enumerate(part_results):
            face_pixels = [tuple(to_crop_pixel(x, y)) for x, y in part["coords"]]
            draw.polygon(face_pixels, fill=palette[index % len(palette)], outline=(255, 255, 255, 190))
        for segment in regularized:
            p1 = to_crop_pixel(*segment["p1"])
            p2 = to_crop_pixel(*segment["p2"])
            draw.line([tuple(p1), tuple(p2)], fill=(74, 222, 128, 180), width=2)
        line_colors = {
            "ridge": (34, 197, 94, 240),
            "hip": (6, 182, 212, 240),
            "step_eave": (245, 158, 11, 240),
            "valley": (168, 85, 247, 240),
            "eave": (239, 68, 68, 240),
        }
        for line in rooflines_out:
            coords = line["line_meters"]
            kind = line.get("kind", "ridge")
            color = line_colors.get(kind, (239, 68, 68, 240))
            p1 = to_crop_pixel(*coords[0])
            p2 = to_crop_pixel(*coords[1])
            draw.line([tuple(p1), tuple(p2)], fill=color, width=3)
        draw.polygon([tuple(p) for p in ring_pixels], outline=(225, 29, 72, 240), width=2)
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_crop.save(debug_path)

    return {
        "address_id": record["address_id"],
        "pipeline_version": 5,
        "geometry_source": geometry_source,
        "height_source": "shape-prior-estimate" if geometry_source == "structural-prior" else "relative-depth-estimate",
        "structure_audit": structure_audit,
        "image_structure_audit": image_structure_audit,
        "prior_error": prior_error,
        "eave_regularization": part_results[0].get("eave_regularization") if geometry_source == "structural-prior" and part_results else None,
        "roof_form_selection": roof_form_selection,
        "centroid": record["centroid"],
        "footprint_area_m2": round(footprint.area, 1),
        "footprint_coords": [[float(x), float(y)] for x, y in footprint_coords[:-1]],
        "segments": len(segments),
        "regularized": len(regularized),
        "rays": len(rays),
        "faces": len(part_results),
        "groups": len(part_results),
        "parts": part_results,
        "rooflines": rooflines_out,
        "ring_pixels": ring_pixels,
        "crop_box": [left, top, right, bottom],
        "depth_available": depth is not None,
    }


def render_mesh(
    result: dict, out_path: Path, *, size: int = 720, yaw_deg: float = -35.0, pitch_deg: float = 35.0
) -> None:
    """Painter's-algorithm preview of the per-plane roof surfaces."""
    triangles: list[tuple[list[float], tuple[int, int, int]]] = []
    palette = [(200, 80, 60), (90, 105, 130), (140, 120, 90), (110, 140, 120), (160, 100, 150)]
    for part in result["parts"]:
        color = palette[part["index"] % len(palette)]
        for triangle in part["triangles"]:
            triangles.append((triangle, color))
    if not triangles:
        return

    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    cos_pitch, sin_pitch = math.cos(pitch), math.sin(pitch)

    def project(point: list[float]) -> tuple[float, float, float]:
        x, y, z = point
        rx = x * cos_yaw - y * sin_yaw
        ry = x * sin_yaw + y * cos_yaw
        depth = ry * cos_pitch - z * sin_pitch
        screen_y = ry * sin_pitch + z * cos_pitch
        return rx, -screen_y, depth

    projected = []
    for triangle, color in triangles:
        screen = [project(point) for point in triangle]
        depth = sum(point[2] for point in screen) / 3.0
        (ax, ay), (bx, by), (cx, cy) = (point[:2] for point in screen)
        facing = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        projected.append((depth, screen, color, facing))
    projected.sort(key=lambda item: item[0], reverse=True)

    xs = [point[0] for _, screen, _, _ in projected for point in screen]
    ys = [point[1] for _, screen, _, _ in projected for point in screen]
    if not xs:
        return
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span = max(max_x - min_x, max_y - min_y, 1e-6)
    scale = (size - 60) / span
    offset_x = (size - (max_x - min_x) * scale) / 2 - min_x * scale
    offset_y = (size - (max_y - min_y) * scale) / 2 - min_y * scale

    image = Image.new("RGB", (size, size), (15, 23, 42))
    draw = ImageDraw.Draw(image)
    for _, screen, color, facing in projected:
        shade = 1.0 if facing >= 0 else 0.75
        shaded = tuple(int(min(255, channel * shade)) for channel in color)
        points = [(x * scale + offset_x, y * scale + offset_y) for x, y, _ in screen]
        draw.polygon(points, fill=shaded, outline=(15, 23, 42))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)


def extract_rooflines(
    record: dict,
    *,
    zoom: int = 18,
    debug_path: Path | None = None,
    depth: np.ndarray | None = None,
) -> dict:
    """One-shot extraction (builds a fresh extractor: loads the depth model)."""
    return RooflineExtractor(zoom=zoom).extract(
        record, debug_path=debug_path, depth=depth
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=Path("public/address-roofs.json"))
    parser.add_argument("--address-id", type=int, required=True)
    parser.add_argument("--zoom", type=int, default=18)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--debug", type=Path, default=None)
    parser.add_argument("--render", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = _load_record(args.records, args.address_id)
    result = extract_rooflines(record, zoom=args.zoom, debug_path=args.debug)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2))
    if args.render:
        render_mesh(result, args.render)
    print(
        f"address={result['address_id']} footprint={result['footprint_area_m2']}m2 "
        f"segments={result['segments']} regularized={result['regularized']} "
        f"rays={result['rays']} faces={result['faces']} groups={result['groups']} "
        f"depth={result['depth_available']}"
    )
    for part in result["parts"]:
        slope = part.get("slope_deg")
        print(
            f"  plane {part['index']}: {part['area_m2']}m2 slope={slope} "
            f"triangles={len(part['triangles'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
