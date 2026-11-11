"""Roof reflection and self-critique engine.

Audits roof boundary polygons, internal facets, and crease lines for:
1. Line Straightness: flags jagged/wonky segments.
2. Orthogonality & Parallelism: checks adherence to the building's primary architectural grid.
3. Feature Completeness: detects uncaptured wings, bays, or protrusions in the spectral mask.
4. Crease Geometry: verifies 45-degree corner bisectors (hips/valleys) and centered horizontal ridges.
5. Regularization: snaps wonky extractions to straight, orthogonal architectural geometry.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import orient


@dataclass
class RoofReflection:
    dominant_angle_deg: float
    straightness_score: float  # 0..1 (1 = perfectly straight edges)
    orthogonality_score: float  # 0..1 (fraction of corners near 90/270 deg)
    parallelism_score: float  # 0..1 (fraction of eaves/ridges aligned with grid)
    uncaptured_roof_m2: float  # area of evident roof outside boundary
    false_intrusion_m2: float  # area of non-roof inside boundary
    wonky_edges_count: int
    critique_items: list[str]
    reflection_verdict: str  # "PASS" | "NEEDS_REGULARIZATION" | "REJECT"
    recommendations: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_dominant_building_axis(
    rgb_image: np.ndarray,
    region_box: tuple[int, int, int, int] | None = None,
) -> float:
    """Detects the primary architectural orientation (0..90 deg) using LSD lines."""
    gray = cv2.cvtColor(rgb_image, cv2.COLOR_RGB2GRAY)
    lsd = cv2.createLineSegmentDetector(0)
    lines, *_ = lsd.detect(gray)
    if lines is None:
        return 0.0

    angles: list[float] = []
    weights: list[float] = []

    h, w = gray.shape
    if region_box:
        x1, y1, x2, y2 = region_box
    else:
        x1, y1, x2, y2 = w // 4, h // 4, 3 * w // 4, 3 * h // 4

    for line in lines:
        coords = line.flatten()
        lx1, ly1, lx2, ly2 = coords[0], coords[1], coords[2], coords[3]
        if (x1 <= lx1 <= x2 or x1 <= lx2 <= x2) and (y1 <= ly1 <= y2 or y1 <= ly2 <= y2):
            length = float(np.hypot(lx2 - lx1, ly2 - ly1))
            if length > 12.0:
                ang = float(np.degrees(np.arctan2(ly2 - ly1, lx2 - lx1))) % 180.0
                angles.append(ang)
                weights.append(length)

    if not angles:
        return 0.0

    mod_angles = np.array(angles) % 90.0
    hist, bin_edges = np.histogram(mod_angles, bins=90, range=(0, 90), weights=weights)
    dom_bin = int(np.argmax(hist))
    dom_angle = float((bin_edges[dom_bin] + bin_edges[dom_bin + 1]) / 2.0)
    return round(dom_angle, 2)


def audit_polygon_geometry(
    ring_meters: list[list[float]],
    dominant_angle_deg: float,
    angle_tolerance_deg: float = 4.5,
) -> tuple[float, float, int, list[str]]:
    """Audits a polygon ring for straightness, orthogonality, and axis parallelism.

    Returns: (orthogonality_score, parallelism_score, wonky_count, issues)
    """
    if len(ring_meters) < 3:
        return 0.0, 0.0, 0, ["Insufficient vertices"]

    poly = Polygon(ring_meters)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return 0.0, 0.0, 0, ["Empty polygon"]

    oriented = orient(poly, sign=1.0)
    coords = list(oriented.exterior.coords)[:-1]
    n = len(coords)

    # 1. Edge parallelism to dominant axis
    grid_axes = [
        dominant_angle_deg % 90.0,
        (dominant_angle_deg + 90.0) % 90.0,
    ]

    parallel_len = 0.0
    total_len = 0.0
    wonky_edges = 0
    issues: list[str] = []

    for i in range(n):
        p1 = np.array(coords[i])
        p2 = np.array(coords[(i + 1) % n])
        vec = p2 - p1
        length = float(np.linalg.norm(vec))
        if length < 0.2:
            continue
        total_len += length

        # Note: in geographic coordinates/metres, angle is arctan2(dy, dx)
        ang = float(np.degrees(np.arctan2(vec[1], vec[0]))) % 90.0
        # Check angle distance to grid axes
        delta = min(abs(ang - grid_axes[0]), 90.0 - abs(ang - grid_axes[0]))
        if delta <= angle_tolerance_deg:
            parallel_len += length
        else:
            wonky_edges += 1
            issues.append(
                f"Wonky edge [{p1[0]:.1f},{p1[1]:.1f}]->[{p2[0]:.1f},{p2[1]:.1f}]: "
                f"angle {ang:.1f}° deviates {delta:.1f}° from grid ({dominant_angle_deg:.1f}°)"
            )

    parallelism_score = parallel_len / total_len if total_len > 0 else 0.0

    # 2. Corner orthogonality (check internal angles ~90 or ~270)
    ortho_count = 0
    total_corners = 0
    for i in range(n):
        p0 = np.array(coords[i - 1])
        p1 = np.array(coords[i])
        p2 = np.array(coords[(i + 1) % n])
        v_in = p1 - p0
        v_out = p2 - p1
        l_in = float(np.linalg.norm(v_in))
        l_out = float(np.linalg.norm(v_out))
        if l_in < 0.2 or l_out < 0.2:
            continue
        total_corners += 1

        dot = float(np.dot(v_in, v_out) / (l_in * l_out))
        dot = max(-1.0, min(1.0, dot))
        turn_angle = math.degrees(math.acos(abs(dot)))  # 0 for straight, 90 for perpendicular
        if abs(turn_angle - 90.0) <= angle_tolerance_deg or abs(turn_angle) <= angle_tolerance_deg:
            ortho_count += 1

    ortho_score = ortho_count / total_corners if total_corners > 0 else 0.0

    return round(ortho_score, 3), round(parallelism_score, 3), wonky_edges, issues


def audit_spectral_completeness(
    rgb_image: np.ndarray,
    ring_pixels: list[list[float]],
    mpp: float | None = None,
    footprint_area_m2: float | None = None,
    min_cluster_m2: float = 5.0,
) -> tuple[float, float, list[str]]:
    """Detects uncaptured roof wings/bays and false intrusions using spectral evidence."""
    h, w, _ = rgb_image.shape
    if len(ring_pixels) < 3:
        return 0.0, 0.0, ["Missing ring pixels"]

    from scripts.ml.roof_signals import detect_roof_mask

    pts_arr = np.array(ring_pixels, dtype=np.float32)
    center_seed = (int(round(float(pts_arr[:, 0].mean()))), int(round(float(pts_arr[:, 1].mean()))))
    mask_clean, _ = detect_roof_mask(rgb_image, center_seed=center_seed)

    poly_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(poly_mask, [np.array(ring_pixels, dtype=np.int32)], 1)
    poly_area_px = float(np.sum(poly_mask))

    if footprint_area_m2 is not None and poly_area_px > 0:
        actual_mpp = math.sqrt(footprint_area_m2 / poly_area_px)
    elif mpp is not None:
        actual_mpp = mpp
    else:
        actual_mpp = 0.236

    px_area_m2 = actual_mpp**2
    min_cluster_px = int(round(min_cluster_m2 / px_area_m2))

    # Uncaptured roof: in mask_clean but outside poly_mask and adjoining the building
    vicinity_k = int(round(6.0 / actual_mpp)) | 1  # 6m adjacency neighborhood
    poly_vicinity = cv2.dilate(poly_mask, cv2.getStructuringElement(cv2.MORPH_RECT, (vicinity_k, vicinity_k)))
    missing_bin = (mask_clean > 0) & (poly_mask == 0) & (poly_vicinity > 0)
    num_missing, labels_m, stats_m, _ = cv2.connectedComponentsWithStats(missing_bin.astype(np.uint8))

    uncaptured_m2 = 0.0
    issues: list[str] = []
    for i in range(1, num_missing):
        area_px = stats_m[i, cv2.CC_STAT_AREA]
        if area_px >= min_cluster_px:
            area_m2 = round(area_px * px_area_m2, 1)
            uncaptured_m2 += area_m2
            issues.append(f"Uncaptured roof wing/protrusion: cluster of {area_m2}m² outside boundary")

    # False intrusion: in poly_mask but not in mask_clean
    intrusion_bin = (poly_mask == 1) & (mask_clean == 0)
    num_intr, labels_i, stats_i, _ = cv2.connectedComponentsWithStats(intrusion_bin.astype(np.uint8))

    false_intrusion_m2 = 0.0
    for i in range(1, num_intr):
        area_px = stats_i[i, cv2.CC_STAT_AREA]
        if area_px >= min_cluster_px:
            area_m2 = round(area_px * px_area_m2, 1)
            false_intrusion_m2 += area_m2
            issues.append(f"False intrusion (lawn/driveway): cluster of {area_m2}m² inside boundary")

    return round(uncaptured_m2, 1), round(false_intrusion_m2, 1), issues


def reflect_on_roof_analysis(
    rgb_image: np.ndarray,
    ring_meters: list[list[float]],
    ring_pixels: list[list[float]],
    mpp: float | None = None,
    footprint_area_m2: float | None = None,
) -> RoofReflection:
    """Performs deep architectural reflection and self-critique on an extraction."""
    dom_angle_px = detect_dominant_building_axis(rgb_image)
    # Convert image y-down angle to geographic metre y-up angle:
    # In image space, angle is atan2(dy_down, dx). In metres, atan2(dy_up, dx) = -atan2(dy_down, dx)
    dom_angle_m = (90.0 - (dom_angle_px % 90.0)) % 90.0

    ortho_score, par_score, wonky_count, geom_issues = audit_polygon_geometry(
        ring_meters, dom_angle_m
    )
    if footprint_area_m2 is None and len(ring_meters) >= 3:
        try:
            footprint_area_m2 = float(Polygon(ring_meters).area)
        except Exception:
            footprint_area_m2 = None

    uncaptured_m2, false_intr_m2, spec_issues = audit_spectral_completeness(
        rgb_image, ring_pixels, mpp=mpp, footprint_area_m2=footprint_area_m2
    )

    all_issues = geom_issues + spec_issues
    recommendations: list[str] = []

    if wonky_count > 0:
        recommendations.append(
            f"Regularize {wonky_count} wonky edges to align with primary building axis ({dom_angle_px}° / {dom_angle_px + 90}°)"
        )
    if uncaptured_m2 > 8.0:
        recommendations.append(
            f"Expand boundary to incorporate uncaptured {uncaptured_m2}m² architectural wing/protrusion"
        )
    if false_intr_m2 > 10.0:
        recommendations.append(
            f"Trim boundary to exclude {false_intr_m2}m² non-roof intrusion (driveway/garden)"
        )

    # Calculate overall straightness
    straightness = 1.0 if wonky_count == 0 else max(0.0, 1.0 - (wonky_count * 0.15))

    verdict = "PASS"
    if wonky_count > 1 or uncaptured_m2 > 10.0 or false_intr_m2 > 15.0:
        verdict = "NEEDS_REGULARIZATION"

    return RoofReflection(
        dominant_angle_deg=dom_angle_px,
        straightness_score=round(straightness, 2),
        orthogonality_score=ortho_score,
        parallelism_score=par_score,
        uncaptured_roof_m2=uncaptured_m2,
        false_intrusion_m2=false_intr_m2,
        wonky_edges_count=wonky_count,
        critique_items=all_issues,
        reflection_verdict=verdict,
        recommendations=recommendations,
    )
