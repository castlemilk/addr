
import numpy as np
from scripts.ml.ortho import align_to
from scripts.ml.pointcloud import build_point_cloud, fit_plane_robust, points_in_polygon
from scripts.ml.silhouette import split_candidates
from shapely.geometry import Polygon


def test_align_to_recovers_shift():
    rng = np.random.default_rng(0)
    reference = (rng.random((64, 64, 3)) * 255).astype(np.uint8)
    candidate = np.roll(reference, (3, -2), axis=(0, 1))
    aligned = align_to(reference.astype(np.float32), candidate.astype(np.float32))
    error = np.abs(aligned - reference).mean()
    original = np.abs(candidate - reference).mean()
    assert error < original


def test_fit_plane_robust_recovers_slope():
    rng = np.random.default_rng(1)
    x = rng.uniform(0, 10, 400)
    y = rng.uniform(0, 10, 400)
    z = 0.3 * x - 0.2 * y + 3.0 + rng.normal(0, 0.05, 400)
    points = np.column_stack([x, y, z])
    plane = fit_plane_robust(points)
    assert plane is not None
    assert abs(plane["a"] - 0.3) < 0.02
    assert abs(plane["b"] + 0.2) < 0.02
    assert plane["inlier_ratio"] > 0.9


def test_fit_plane_robust_rejects_outlier_cloud():
    rng = np.random.default_rng(2)
    x = rng.uniform(0, 5, 50)
    y = rng.uniform(0, 5, 50)
    z = np.full(50, 4.0) + rng.normal(0, 0.05, 50)
    outliers = np.column_stack([rng.uniform(0, 5, 20), rng.uniform(0, 5, 20), np.full(20, 9.0)])
    points = np.vstack([np.column_stack([x, y, z]), outliers])
    plane = fit_plane_robust(points)
    assert plane is not None
    assert abs(plane["c"] - 4.0) < 0.3
    assert plane["inlier_ratio"] > 0.6


class FakeFrame:
    def metre_to_pixel(self, x, y):
        return (x * 4.0 + 50.0, y * 4.0 + 50.0)


def test_build_point_cloud_within_polygon():
    depth = np.zeros((200, 200), dtype=np.float32)
    depth[40:120, 40:120] = np.linspace(0.2, 0.8, 80)[None, :]
    polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    cloud = build_point_cloud(depth, FakeFrame(), polygon, eave=3.0, ridge=6.0)
    assert cloud is not None and len(cloud) > 100
    assert cloud.points[:, 2].min() >= 3.0 - 1e-6
    assert cloud.points[:, 2].max() <= 6.0 + 1e-6
    subset = points_in_polygon(cloud, Polygon([(0, 0), (4, 0), (4, 4), (0, 4)]))
    assert 0 < len(subset) < len(cloud)


def test_split_candidates_detects_height_drop():
    parts = [
        {"plane": {"a": 0, "b": 0, "c": 6.0}, "centroid": (0.0, 0.0), "area_m2": 50.0},
        {"plane": {"a": 0, "b": 0, "c": 3.0}, "centroid": (5.0, 0.0), "area_m2": 20.0},
        {"plane": {"a": 0, "b": 0, "c": 2.9}, "centroid": (9.0, 0.0), "area_m2": 18.0},
    ]
    candidates = split_candidates(parts, min_drop_m=0.9)
    assert {(c["part_a"], c["part_b"]) for c in candidates} == {(0, 1), (0, 2)}


def test_robust_plane_reports_final_error_with_canopy_contamination():
    rng = np.random.default_rng(19)
    xy = rng.uniform(-8, 8, (400, 2))
    z = 0.45 * xy[:, 0] - 0.2 * xy[:, 1] + 5 + rng.normal(0, 0.04, 400)
    z[:100] += 8
    plane = fit_plane_robust(np.column_stack([xy, z]))
    assert plane is not None
    assert abs(plane["a"] - 0.45) < 0.02
    assert abs(plane["c"] - 5) < 0.05
    assert plane["rmse_m"] < 0.08
    assert 0.7 < plane["inlier_ratio"] < 0.8


def test_plane_rejects_degenerate_samples_and_ignores_nan():
    line = np.column_stack([np.arange(20), np.arange(20), np.ones(20)])
    assert fit_plane_robust(line) is None
    rng = np.random.default_rng(4)
    points = np.column_stack([rng.normal(size=(40, 2)), np.ones(40) * 3])
    points[0] = np.nan
    plane = fit_plane_robust(points)
    assert plane and plane["samples"] == 39 and plane["rmse_m"] == 0


def test_silhouette_preserves_corners_without_edge_evidence():
    from types import SimpleNamespace

    from PIL import Image
    from scripts.ml.silhouette import snap_ring

    window = SimpleNamespace(image=Image.new("RGB", (200, 200), "gray"))
    ring = [[0, 0], [10, 0], [10, 4], [4, 4], [4, 10], [0, 10]]
    result = snap_ring(ring, window, FakeFrame())
    assert result["ring"] == ring
    assert result["iou"] == 1
    assert result["moved"] == 0


def test_plane_rejects_clustered_canopy_at_roof_edge():
    rng = np.random.default_rng(29)
    xy = np.column_stack([
        np.r_[rng.uniform(-8, 5, 300), rng.uniform(5, 8, 100)],
        rng.uniform(-8, 8, 400),
    ])
    z = .45 * xy[:, 0] - .2 * xy[:, 1] + 5 + rng.normal(0, .04, 400)
    z[300:] += 6  # a canopy along one side rotates a least-squares seed
    plane = fit_plane_robust(np.column_stack([xy, z]))
    assert abs(plane["a"] - .45) < .01
    assert abs(plane["c"] - 5) < .02
    assert plane["inliers"] == 300
    assert plane["rmse_m"] < .05
