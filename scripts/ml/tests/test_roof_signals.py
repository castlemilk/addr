import numpy as np
from scripts.ml.roof_signals import (
    compute_topology_signals,
    detect_roof_mask,
    evaluate_roof_accuracy,
)


def test_topology_convex_rect():
    rect = [[10, 10], [50, 10], [50, 40], [10, 40]]
    is_concave, reflex_angles, solidity, match = compute_topology_signals(rect, "convex")
    assert not is_concave
    assert len(reflex_angles) == 0
    assert solidity >= 0.98
    assert match is True


def test_topology_concave_l_shape():
    # L-shaped polygon
    l_shape = [
        [10, 10],
        [60, 10],
        [60, 30],
        [30, 30],  # reflex courtyard corner
        [30, 60],
        [10, 60],
    ]
    is_concave, reflex_angles, solidity, match = compute_topology_signals(l_shape, "concave")
    assert is_concave
    assert any(angle >= 250.0 for angle in reflex_angles)
    assert solidity < 0.85
    assert match is True


def test_detect_roof_mask_synthetic_warm():
    # 100x100 synthetic image with a warm terracotta patch in center
    img = np.full((100, 100, 3), [80, 120, 70], dtype=np.uint8)  # greenish lawn background
    img[30:70, 30:70] = [160, 95, 80]  # terracotta roof patch

    mask, grad = detect_roof_mask(img, center_seed=(50, 50), min_roof_area_px=100)
    assert mask.shape == (100, 100)
    assert np.sum(mask[35:65, 35:65]) > 800
    assert np.sum(mask[:20, :20]) == 0


def test_evaluate_roof_accuracy_synthetic():
    img = np.full((100, 100, 3), [80, 120, 70], dtype=np.uint8)
    img[30:70, 30:70] = [160, 95, 80]

    ring = [[30, 30], [70, 30], [70, 70], [30, 70]]
    signals = evaluate_roof_accuracy(
        img,
        ring,
        expected_topology="convex",
        footprint_area_m2=160.0,
    )
    assert signals.purity >= 0.80
    assert signals.coverage >= 0.80
    assert signals.iou >= 0.70
    assert signals.topology_match is True
    assert signals.quality_grade in ("A", "B")
    assert len(signals.warnings) == 0


def test_audit_polygon_geometry_detects_wonky_edges():
    from scripts.ml.roof_reflection import audit_polygon_geometry

    # Perfectly orthogonal box aligned with 0 deg grid
    straight_box = [[0, 0], [10, 0], [10, 5], [0, 5]]
    ortho, par, wonky, issues = audit_polygon_geometry(straight_box, 0.0)
    assert ortho == 1.0
    assert par == 1.0
    assert wonky == 0
    assert len(issues) == 0

    # Wonky polygon with 20 deg slanted edge
    wonky_poly = [[0, 0], [10, 3.64], [10, 8], [0, 8]]  # ~20 deg slant
    ortho_w, par_w, wonky_w, issues_w = audit_polygon_geometry(wonky_poly, 0.0)
    assert wonky_w >= 1
    assert any("Wonky edge" in iss for iss in issues_w)

