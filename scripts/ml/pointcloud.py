"""Monocular point cloud for roof heights.

The paper's LoD2 step never detects planes in the point cloud: rooflines come
from the orthophoto, and the cloud only supplies *height* per roof part. This
module builds the closest thing we have to a dense photogrammetric cloud - a
per-building cloud unprojected from the depth model in the imagery frame - and
fits each partition part with a robust least-squares plane (median/MAD inlier
gating) purely as a height/attitude estimator.

Everything is in metres: x east, y north (record-local frame), z up, anchored
between the record's eave and ridge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from shapely import contains_xy
from shapely.geometry import Polygon

POINT_STEP_M = 0.25
PLANE_TOLERANCE_M = 0.35


@dataclass
class PointCloud:
    points: np.ndarray  # (N, 3) x, y, z in metres
    eave: float
    ridge: float

    def __len__(self) -> int:
        return int(self.points.shape[0])


def build_point_cloud(
    depth: np.ndarray | None,
    frame,
    polygon: Polygon,
    eave: float,
    ridge: float,
    step_m: float = POINT_STEP_M,
) -> PointCloud | None:
    """Unprojects depth inside the footprint into a metre-space cloud."""
    if depth is None or polygon.is_empty:
        return None
    minx, miny, maxx, maxy = polygon.bounds
    xs = np.arange(minx, maxx + step_m, step_m)
    ys = np.arange(miny, maxy + step_m, step_m)
    grid_x, grid_y = np.meshgrid(xs, ys)
    grid_x = grid_x.ravel()
    grid_y = grid_y.ravel()
    inside = contains_xy(polygon, grid_x, grid_y)
    grid_x = grid_x[inside]
    grid_y = grid_y[inside]
    if grid_x.size < 8:
        return None
    samples = np.full(grid_x.shape, np.nan)
    for index in range(grid_x.size):
        px, py = frame.metre_to_pixel(float(grid_x[index]), float(grid_y[index]))
        ix, iy = int(round(px)), int(round(py))
        if 0 <= iy < depth.shape[0] and 0 <= ix < depth.shape[1]:
            samples[index] = depth[iy, ix]
    valid = np.isfinite(samples)
    if valid.sum() < 8:
        return None
    values = samples[valid]
    low, high = np.percentile(values, 2), np.percentile(values, 98)
    span = max(1e-6, float(high - low))
    z = eave + (values - low) / span * (ridge - eave)
    z = np.clip(z, eave, ridge)
    points = np.column_stack([grid_x[valid], grid_y[valid], z])
    return PointCloud(points=points, eave=eave, ridge=ridge)


def points_in_polygon(cloud: PointCloud, polygon: Polygon) -> np.ndarray:
    if polygon.is_empty:
        return np.empty((0, 3))
    mask = contains_xy(polygon, cloud.points[:, 0], cloud.points[:, 1])
    return cloud.points[mask]


def fit_plane_robust(
    points: np.ndarray,
    *,
    tolerance: float = PLANE_TOLERANCE_M,
    min_points: int = 12,
) -> dict | None:
    """Robust least-squares plane (z = ax + by + c) with MAD inlier gating."""
    # A few canopy/chimney samples can rotate an ordinary least-squares plane
    # before MAD gating. Seed with deterministic consensus, then refit inliers.
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        return None
    points = points[np.isfinite(points).all(axis=1)]
    if points.shape[0] < min_points:
        return None
    matrix = np.column_stack([points[:, 0], points[:, 1], np.ones(points.shape[0])])
    if np.linalg.matrix_rank(matrix) < 3:
        return None
    values = points[:, 2]
    coefficients, *_ = np.linalg.lstsq(matrix, values, rcond=None)
    best_score = (-1, -math.inf)
    rng = np.random.default_rng(0)
    for attempt in range(65):
        if attempt:
            sample = rng.choice(len(points), 3, replace=False)
            if np.linalg.matrix_rank(matrix[sample]) < 3:
                continue
            candidate = np.linalg.solve(matrix[sample], values[sample])
        else:
            candidate = coefficients
        residual = np.abs(values - matrix @ candidate)
        candidate_inliers = residual <= tolerance
        score = (int(candidate_inliers.sum()), -float(np.median(residual)))
        if score > best_score:
            best_score = score
            coefficients = candidate
    for _ in range(3):
        residuals = values - matrix @ coefficients
        center = float(np.median(residuals))
        mad = float(np.median(np.abs(residuals - center)))
        gate = max(tolerance, min(2 * tolerance, 2.5 * 1.4826 * mad))
        inliers = np.abs(residuals) <= gate
        if inliers.sum() < min_points or np.linalg.matrix_rank(matrix[inliers]) < 3:
            return None
        coefficients, *_ = np.linalg.lstsq(matrix[inliers], values[inliers], rcond=None)
    # Diagnostics must describe the final refit, not the initial biased plane.
    residuals = values - matrix @ coefficients
    inliers = np.abs(residuals) <= gate
    if inliers.sum() < min_points:
        return None
    a, b, c = (float(value) for value in coefficients)
    slope_deg = math.degrees(math.atan(math.hypot(a, b)))
    return {
        "a": a,
        "b": b,
        "c": c,
        "slope_deg": round(slope_deg, 1),
        "inliers": int(inliers.sum()),
        "samples": int(points.shape[0]),
        "inlier_ratio": round(float(inliers.sum()) / points.shape[0], 3),
        "rmse_m": round(float(np.sqrt(np.mean(residuals[inliers] ** 2))), 3)
        if inliers.any()
        else None,
        "eave_m": round(float(np.percentile(values[inliers], 5)), 2)
        if inliers.any()
        else round(float(values.min()), 2),
        "ridge_m": round(float(np.percentile(values[inliers], 95)), 2)
        if inliers.any()
        else round(float(values.max()), 2),
    }


def split_candidates(
    planes: list[dict],
    adjacency_height_drop_m: float = 0.8,
) -> list[tuple[int, int]]:
    """Pairs of adjacent parts whose medians differ enough to be separate roofs."""
    pairs: list[tuple[int, int]] = []
    for i, first in enumerate(planes):
        if first is None:
            continue
        for j in range(i + 1, len(planes)):
            second = planes[j]
            if second is None:
                continue
            drop = abs(first["eave_m"] - second["eave_m"])
            if drop >= adjacency_height_drop_m:
                pairs.append((i, j))
    return pairs
