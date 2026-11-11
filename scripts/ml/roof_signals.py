"""Roof accuracy signals: quantitative and visual evaluation of roof boundaries.

Computes multi-modal accuracy signals for a building footprint against aerial imagery
and height data:
1. Purity (Precision): fraction of the polygon that is genuine roof vs non-roof (lawn, driveway, street, vehicles).
2. Coverage (Recall): fraction of the visually/spectrally evident roof structure captured.
3. Boundary IoU: intersection-over-union with the multi-spectral/gradient roof mask.
4. Edge Gradient Adherence: fraction of the boundary perimeter aligned with physical image gradients.
5. Topology & Shape Concordance: notch angle detection, concavity, and L-shape / wing classification.
6. Elevation Discontinuity: step-down height drop along perimeter eaves.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import orient


@dataclass
class RoofSignals:
    iou: float
    purity: float  # Precision
    coverage: float  # Recall
    edge_score: float  # Gradient adherence 0..1
    is_concave: bool
    notch_angles_deg: list[float]
    solidity: float
    topology_match: bool
    area_m2: float
    quality_grade: str  # "A" | "B" | "C" | "D" | "F"
    quality_score: int  # 0..100
    straightness: float = 1.0  # 0..1 (1 = all straight edges)
    orthogonality: float = 1.0  # 0..1 (1 = all corners 90/270 deg)
    wonky_lines: int = 0  # number of wonky segments
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_roof_mask(
    rgb_image: np.ndarray,
    depth_image: np.ndarray | None = None,
    center_seed: tuple[int, int] = (256, 256),
    min_roof_area_px: int = 350,
) -> tuple[np.ndarray, np.ndarray]:
    """Extracts binary roof mask and gradient magnitude from imagery.

    Uses adaptive spectral signature sampled from the central building zone:
    - Distinguishes warm terracotta/tile from vegetation and dark asphalt
    - Distinguishes cool slate/metal from lawn and concrete
    - Refines connected component closest to building center
    """
    h, w, _ = rgb_image.shape
    r = rgb_image[:, :, 0].astype(float)
    g = rgb_image[:, :, 1].astype(float)
    b = rgb_image[:, :, 2].astype(float)

    # 1. Sample central zone to determine dominant roof spectral class
    cx, cy = center_seed
    cw = max(5, min(30, min(h, w) // 10))
    sample = rgb_image[max(0, cy - cw) : min(h, cy + cw), max(0, cx - cw) : min(w, cx + cw)]
    if sample.size > 0:
        sr = float(sample[:, :, 0].mean())
        sg = float(sample[:, :, 1].mean())
        sb = float(sample[:, :, 2].mean())
    else:
        sr, sg, sb = 120.0, 100.0, 90.0

    is_warm = (sr > sg + 8) and (sr > sb + 12)

    if is_warm:
        # Terracotta, brick tile, warm colorbond
        # Higher saturation and brightness to differentiate from brown mulch/soil
        roof_cand = (r > g + 12) & (r > b + 16) & (r > 75) & ((r - g) + (r - b) > 32)
    else:
        # Neutral slate, dark colorbond, zinc
        lab = cv2.cvtColor(rgb_image, cv2.COLOR_RGB2LAB)
        L, A, _ = cv2.split(lab)
        not_vegetation = A > 124  # green veg has A < 122
        roof_cand = (np.abs(r - g) < 22) & (np.abs(g - b) < 22) & (r > 55) & not_vegetation

    # 2. Exclude asphalt driveway, street, and parked saturated red/blue cars
    asphalt = (np.abs(r - g) < 8) & (np.abs(g - b) < 8) & (r < 110)
    red_car = (r > 130) & (b < 60) & (g < 70)
    roof_cand = roof_cand & (~asphalt) & (~red_car)

    # 3. Morphological close to bridge tile seams and skylights
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    closed = cv2.morphologyEx(roof_cand.astype(np.uint8), cv2.MORPH_CLOSE, kernel)

    # 4. Connected component matching central building
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(closed)
    target_comp = 0
    min_dist = float("inf")

    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area >= min_roof_area_px:
            dist = float(np.linalg.norm(centroids[i] - [cx, cy]))
            if dist < min_dist:
                min_dist = dist
                target_comp = i

    if target_comp > 0:
        comp_bin = (labels == target_comp).astype(np.uint8)
        contours, _ = cv2.findContours(comp_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        roof_mask = np.zeros((h, w), dtype=bool)
        cv2.drawContours(roof_mask.view(np.uint8), contours, -1, 1, thickness=cv2.FILLED)
    else:
        roof_mask = np.zeros((h, w), dtype=bool)

    # 5. Gradient magnitude for edge alignment
    gray = cv2.cvtColor(rgb_image, cv2.COLOR_RGB2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    grad_mag = np.hypot(gx, gy)
    if grad_mag.max() > 0:
        grad_mag /= grad_mag.max()

    return roof_mask, grad_mag



def compute_edge_gradient_adherence(
    ring_pixels: list[list[float]],
    grad_mag: np.ndarray,
    sample_radius_px: int = 3,
) -> float:
    """Measures what fraction of the boundary perimeter sits on strong image edges."""
    if len(ring_pixels) < 3:
        return 0.0

    edge_samples: list[float] = []
    h, w = grad_mag.shape
    n = len(ring_pixels)

    for i in range(n):
        p1 = np.array(ring_pixels[i])
        p2 = np.array(ring_pixels[(i + 1) % n])
        dist = np.linalg.norm(p2 - p1)
        if dist < 1.0:
            continue
        num_steps = max(2, int(round(dist / 2.0)))
        for s in range(num_steps):
            t = s / float(num_steps)
            pt = p1 * (1 - t) + p2 * t
            ix = int(round(pt[0]))
            iy = int(round(pt[1]))
            if sample_radius_px <= ix < w - sample_radius_px and sample_radius_px <= iy < h - sample_radius_px:
                patch = grad_mag[
                    iy - sample_radius_px : iy + sample_radius_px + 1,
                    ix - sample_radius_px : ix + sample_radius_px + 1,
                ]
                edge_samples.append(float(np.max(patch)))

    if not edge_samples:
        return 0.0
    return float(np.mean(edge_samples))


def compute_topology_signals(
    ring_pixels: list[list[float]],
    expected_topology: str | None = None,
) -> tuple[bool, list[float], float, bool]:
    """Computes concavity, notch angles, solidity, and topology match."""
    if len(ring_pixels) < 3:
        return False, [], 0.0, False

    poly = Polygon(ring_pixels)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return False, [], 0.0, False

    oriented = orient(poly, sign=1.0)
    coords = list(oriented.exterior.coords)[:-1]
    reflex_angles: list[float] = []

    for i in range(len(coords)):
        p0 = coords[i - 1]
        p1 = coords[i]
        p2 = coords[(i + 1) % len(coords)]
        in_vec = (p1[0] - p0[0], p1[1] - p0[1])
        out_vec = (p2[0] - p1[0], p2[1] - p1[1])
        cross = in_vec[0] * out_vec[1] - in_vec[1] * out_vec[0]
        dot = in_vec[0] * out_vec[0] + in_vec[1] * out_vec[1]
        bend = math.degrees(math.atan2(abs(cross), dot))
        if cross < 0:  # Right turn on CCW = reflex (concave) notch
            reflex_angles.append(round(180.0 + bend, 1))

    hull = poly.convex_hull
    solidity = float(poly.area / hull.area) if hull.area > 0 else 0.0
    # True notch is >= 225 deg (digitizer wobble sits ~209 deg)
    has_true_notch = any(a >= 225.0 for a in reflex_angles)
    is_concave = solidity < 0.95 or has_true_notch

    topology_match = True
    if expected_topology == "concave":
        topology_match = is_concave
    elif expected_topology == "convex":
        topology_match = not is_concave

    return is_concave, reflex_angles, round(solidity, 3), topology_match


def evaluate_roof_accuracy(
    rgb_image: np.ndarray,
    ring_pixels: list[list[float]],
    depth_image: np.ndarray | None = None,
    expected_topology: str | None = None,
    footprint_area_m2: float | None = None,
    mpp: float = 0.236,
) -> RoofSignals:
    """Evaluates the geometric, spectral, and topological accuracy of a roof footprint."""
    h, w, _ = rgb_image.shape
    center_seed = (w // 2, h // 2)
    if len(ring_pixels) >= 3:
        pts = np.array(ring_pixels, dtype=np.float32)
        center_seed = (int(round(float(pts[:, 0].mean()))), int(round(float(pts[:, 1].mean()))))

    roof_mask, grad_mag = detect_roof_mask(rgb_image, depth_image, center_seed=center_seed)

    poly_mask = np.zeros((h, w), dtype=bool)
    if len(ring_pixels) >= 3:
        pts_int = np.array(ring_pixels, dtype=np.int32)
        cv2.fillPoly(poly_mask.view(np.uint8), [pts_int], 1)

    intersection = poly_mask & roof_mask
    union = poly_mask | roof_mask
    poly_area = float(np.sum(poly_mask))
    roof_area = float(np.sum(roof_mask))

    iou = float(np.sum(intersection) / np.sum(union)) if np.sum(union) > 0 else 0.0
    purity = float(np.sum(intersection) / poly_area) if poly_area > 0 else 0.0
    coverage = float(np.sum(intersection) / roof_area) if roof_area > 0 else 0.0

    edge_score = compute_edge_gradient_adherence(ring_pixels, grad_mag)
    is_concave, reflex_angles, solidity, top_match = compute_topology_signals(
        ring_pixels, expected_topology
    )

    # Geometric straightness and orthogonality audit
    from scripts.ml.roof_reflection import audit_polygon_geometry, detect_dominant_building_axis
    dom_ang = detect_dominant_building_axis(rgb_image)
    ortho_score, par_score, wonky_count, geom_issues = audit_polygon_geometry(
        ring_pixels, dom_ang
    )
    straightness = 1.0 if wonky_count == 0 else max(0.0, 1.0 - (wonky_count * 0.12))

    warnings: list[str] = []
    if wonky_count > 0:
        warnings.append(f"Wonky edges: {wonky_count} segments deviate from building grid ({dom_ang:.1f}°)")
    if purity < 0.70:
        warnings.append(f"High non-roof intrusion: {(1 - purity) * 100:.0f}% non-roof area inside boundary")
    if coverage < 0.65:
        warnings.append(f"Significant roof clipped: {(1 - coverage) * 100:.0f}% of roof area omitted")
    if not top_match and expected_topology:
        warnings.append(f"Topology mismatch: expected {expected_topology} but detected {'concave' if is_concave else 'convex'}")
    if edge_score < 0.22:
        warnings.append("Weak edge alignment: boundary does not follow image eaves")

    # Composite quality score (0..100)
    score = int(round(
        iou * 35.0 +
        purity * 20.0 +
        coverage * 15.0 +
        straightness * 15.0 +
        ortho_score * 5.0 +
        min(1.0, edge_score / 0.3) * 5.0 +
        (5.0 if top_match else 0.0)
    ))
    score = max(0, min(100, score))

    if score >= 85:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 55:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    area_m2 = footprint_area_m2 if footprint_area_m2 is not None else (poly_area * (mpp**2))

    return RoofSignals(
        iou=round(iou, 3),
        purity=round(purity, 3),
        coverage=round(coverage, 3),
        edge_score=round(edge_score, 3),
        is_concave=is_concave,
        notch_angles_deg=reflex_angles,
        solidity=solidity,
        topology_match=top_match,
        area_m2=round(area_m2, 1),
        quality_grade=grade,
        quality_score=score,
        straightness=round(straightness, 2),
        orthogonality=ortho_score,
        wonky_lines=wonky_count,
        warnings=warnings,
    )
