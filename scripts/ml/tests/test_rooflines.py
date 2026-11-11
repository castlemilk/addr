import math

from scripts.ml.rooflines import (
    _coalesce_slivers,
    _group_planes,
    _partition_faces,
    _propagate,
    _regularize,
    _rooflines,
)
from shapely.geometry import Polygon


def make_segment(p1, p2):
    (ax, ay), (bx, by) = p1, p2
    length = math.hypot(bx - ax, by - ay)
    angle = math.atan2(by - ay, bx - ax) % math.pi
    normal = (-math.sin(angle), math.cos(angle))
    return {
        "mid": ((ax + bx) / 2, (ay + by) / 2),
        "angle": angle,
        "offset": normal[0] * ax + normal[1] * ay,
        "length": length,
    }


def test_regularize_snaps_near_parallel_angles():
    segments = [
        make_segment((0, 0), (10, 0)),
        make_segment((0, 5), (10, 5)),
        make_segment((0, 10), (9.5, 10.6)),
    ]
    regularized, _ = _regularize(segments)
    angles = [segment["angle"] for segment in regularized]
    assert max(angles) - min(angles) < math.radians(1.0)


def test_regularize_merges_near_collinear_offsets():
    segments = [
        make_segment((0, 0), (5, 0)),
        make_segment((6, 0.5), (10, 0.5)),
    ]
    regularized, _ = _regularize(segments)
    offsets = [segment["anchor"][1] for segment in regularized]
    assert abs(offsets[0] - offsets[1]) < 0.3


def test_propagate_stops_at_kth_intersection():
    domain = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    horizontal = make_segment((0, 5), (10, 5))
    vertical_a = make_segment((3, 0), (3, 10))
    vertical_b = make_segment((7, 0), (7, 10))
    regularized, _ = _regularize([horizontal, vertical_a, vertical_b])
    rays = _propagate(regularized, domain, k=1)
    horizontal_rays = [
        ray for ray in rays if ray.bounds[3] - ray.bounds[1] < 0.5
    ]
    assert len(horizontal_rays) == 1
    middle = horizontal_rays[0]
    assert middle.bounds[0] > 2.5 and middle.bounds[2] < 7.5


def test_partition_splits_square_into_faces():
    domain = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    segments = [make_segment((0, 5), (10, 5))]
    regularized, _ = _regularize(segments)
    rays = _propagate(regularized, domain, k=1)
    faces = _partition_faces(rays, domain)
    assert len(faces) == 2
    assert sum(face.area for face in faces) > 90


def test_propagate_clips_rays_to_domain():
    domain = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    segments = [make_segment((-5, 5), (-1, 5))]
    regularized, _ = _regularize(segments)
    rays = _propagate(regularized, domain, k=1)
    assert rays
    for ray in rays:
        minx, miny, maxx, maxy = ray.bounds
        assert minx >= -1.1 and miny >= -1.1 and maxx <= 11.1 and maxy <= 11.1


def test_group_planes_merges_coplanar_faces():
    left = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])
    right = Polygon([(5, 0), (10, 0), (10, 5), (5, 5)])
    sloped = Polygon([(0, 5), (5, 5), (5, 10), (0, 10)])
    faces = [
        {"polygon": left, "plane": {"a": 0.02, "b": 0.0, "c": 0.5, "mean_depth": 0.50}},
        {"polygon": right, "plane": {"a": 0.02, "b": 0.0, "c": 0.5, "mean_depth": 0.51}},
        {"polygon": sloped, "plane": {"a": 0.6, "b": 0.0, "c": 0.2, "mean_depth": 0.55}},
    ]
    groups = _group_planes(faces)
    sizes = sorted(len(group) for group in groups)
    assert sizes == [1, 2]


def test_rooflines_between_different_groups():
    left = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])
    right = Polygon([(5, 0), (10, 0), (10, 5), (5, 5)])
    faces = [
        {"polygon": left, "plane": None},
        {"polygon": right, "plane": None},
    ]
    groups = [[0], [1]]
    lines = _rooflines(faces, groups, Polygon([(0, 0), (10, 0), (10, 5), (0, 5)]))
    assert len(lines) == 1


def test_coalesce_absorbs_slivers():
    large = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    sliver = Polygon([(10, 0), (11, 0), (11, 0.5), (10, 0.5)])
    faces = _coalesce_slivers([large, sliver], min_area=6.0)
    assert len(faces) == 1
    assert faces[0].area == 100.5


def test_drop_parallel_families():
    from scripts.ml.rooflines import _drop_parallel_families

    # Three parallel metal ribs close together: 0.5m apart
    s1 = make_segment((0, 0), (10, 0))
    s2 = make_segment((0, 0.5), (6, 0.5))
    s3 = make_segment((0, 1.0), (4, 1.0))
    # Another perpendicular line
    s4 = make_segment((5, -5), (5, 5))

    kept = _drop_parallel_families([s1, s2, s3, s4], offset_gap_m=1.2)
    # The longest (s1) of the parallel family should survive along with s4
    assert len(kept) == 2
    lengths = [s["length"] for s in kept]
    assert 10.0 in lengths


def test_classify_roofline_detects_step_eave_and_ridge():
    from scripts.ml.rooflines import _classify_roofline

    part_low = {
        "coords": [(0, 0), (5, 0), (5, 5), (0, 5)],
        "plane": {"a": 0.0, "b": 0.0, "c": 3.0},
    }
    part_high = {
        "coords": [(5, 0), (10, 0), (10, 5), (5, 5)],
        "plane": {"a": 0.0, "b": 0.0, "c": 6.5},  # Step difference > 0.65m
    }
    line = [[5.0, 0.0], [5.0, 5.0]]
    kind = _classify_roofline(line, [part_low, part_high])
    assert kind == "step_eave"

    # Opposing slopes meeting at same ridge height
    part_slope_a = {
        "coords": [(0, 0), (5, 0), (5, 5), (0, 5)],
        "plane": {"a": 0.4, "b": 0.0, "c": 4.0},
    }
    part_slope_b = {
        "coords": [(5, 0), (10, 0), (10, 5), (5, 5)],
        "plane": {"a": -0.4, "b": 0.0, "c": 8.0},
    }
    ridge_kind = _classify_roofline(line, [part_slope_a, part_slope_b])
    assert ridge_kind == "ridge"


def test_classify_roofline_detects_valley_and_hip():
    from scripts.ml.rooflines import _classify_roofline

    # Valley: concave fold where line is lower than adjacent slopes
    part_valley_a = {
        "coords": [(0, 0), (5, 0), (5, 5), (0, 5)],
        "plane": {"a": -0.4, "b": 0.0, "c": 4.0},
    }
    part_valley_b = {
        "coords": [(5, 0), (10, 0), (10, 5), (5, 5)],
        "plane": {"a": 0.4, "b": 0.0, "c": 0.0},
    }
    valley_line = [[5.0, 0.0], [5.0, 5.0]]
    assert _classify_roofline(valley_line, [part_valley_a, part_valley_b]) == "valley"

    # Hip: convex crease sloping down to eave corner with slope > 5 degrees
    part_hip_a = {
        "coords": [(0, 0), (5, 0), (5, 5)],
        "plane": {"a": 0.0, "b": 0.4, "c": 0.0},
    }
    part_hip_b = {
        "coords": [(0, 0), (5, 5), (0, 5)],
        "plane": {"a": 0.4, "b": 0.0, "c": 0.0},
    }
    hip_line = [[0.0, 0.0], [5.0, 5.0]]
    assert _classify_roofline(hip_line, [part_hip_a, part_hip_b]) == "hip"


def test_cambridge_architectural_model_is_an_explicit_reference_fixture():
    from scripts.ml.rooflines import _cambridge_architectural_parts_and_lines

    class FixtureFrame:
        def metre_to_lonlat(self, x, y):
            return x / 100000, y / 100000

    parts, lines, _ = _cambridge_architectural_parts_and_lines(FixtureFrame())
    res = {"parts": parts, "rooflines": lines}
    assert len(res["parts"]) == 14
    assert len(res["rooflines"]) == 18

    # Verify pitch angles and ratios are architectural terracotta standard (22-23 deg)
    pitches = [p["pitch_deg"] for p in res["parts"]]
    assert all(21.0 <= p <= 24.0 for p in pitches)

    # Verify 3D surface area > 2D footprint area (A_3D = A_2D / cos(pitch))
    for p in res["parts"]:
        assert p["area_3d_m2"] > p["area_m2"]
        assert "pitch_ratio" in p
        assert "azimuth_deg" in p

    # Verify roofline kinds include ridge, valley, and hip
    line_kinds = {line["kind"] for line in res["rooflines"]}
    assert "ridge" in line_kinds
    assert "valley" in line_kinds
    assert "hip" in line_kinds


def test_regularize_snaps_to_architectural_grid():
    # Primary axis at 0.5 rad (~28.6 deg)
    dom_axis = 0.5
    # Segments slightly deviating by ~2.5 deg (0.04 rad)
    seg_parallel = make_segment(
        (0, 0), (10 * math.cos(dom_axis + 0.04), 10 * math.sin(dom_axis + 0.04))
    )
    perp_axis = dom_axis + math.pi / 2
    seg_perp = make_segment(
        (0, 0), (10 * math.cos(perp_axis - 0.04), 10 * math.sin(perp_axis - 0.04))
    )
    diagonal_axis = dom_axis + math.pi / 4
    seg_diag = make_segment(
        (0, 0), (10 * math.cos(diagonal_axis + 0.04), 10 * math.sin(diagonal_axis + 0.04))
    )

    regularized, angles = _regularize(
        [seg_parallel, seg_perp, seg_diag], dominant_axis_rad=dom_axis
    )

    # Snapped to exact mathematical orientations
    assert abs(angles[0] - dom_axis) < 1e-6
    assert abs(angles[1] - perp_axis) < 1e-6
    assert abs(angles[2] - diagonal_axis) < 1e-6




def test_group_planes_compares_heights_at_shared_edge():
    faces = [
        {"polygon": Polygon([(0, 0), (5, 0), (5, 5), (0, 5)]),
         "plane": {"a": 0.5, "b": 0, "c": 3}},
        {"polygon": Polygon([(5, 0), (10, 0), (10, 5), (5, 5)]),
         "plane": {"a": 0.5, "b": 0, "c": 3}},
    ]
    assert _group_planes(faces) == [[0, 1]]


def test_reconcile_preserves_gable_apex_and_shared_heights():
    from scripts.ml.rooflines import _reconcile_part_heights

    footprint = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    parts = [
        {"coords": [(0, 0), (5, 0), (5, 10), (0, 10)],
         "plane": {"a": 0.6, "b": 0, "c": 3}},
        {"coords": [(5, 0), (10, 0), (10, 10), (5, 10), (5, 5)],
         "plane": {"a": -0.6, "b": 0, "c": 9}},
    ]
    _reconcile_part_heights(parts, footprint, 3, 6)
    for part in parts:
        for (x, _y), z in zip(part["coords"], part["ring_z"], strict=True):
            assert abs(z - (6 if x == 5 else 3)) < 0.01
    assert (5, 5) in parts[0]["coords"]  # T-junction noded on both faces


def test_rejected_cut_does_not_remove_crossing_ridge():
    from scripts.ml.rooflines import _matches_rejected_line

    rejected = [[[-5, 0], [5, 0]]]
    assert _matches_rejected_line([[-3, .1], [3, .1]], rejected)
    assert _matches_rejected_line([[3, .1], [-3, .1]], rejected)
    assert not _matches_rejected_line([[0, -5], [0, 5]], rejected)
    assert not _matches_rejected_line([[15, 0], [20, 0]], rejected)


def test_propagation_keeps_both_wings_of_concave_roof():
    domain = Polygon([(0, 0), (12, 0), (12, 12), (8, 12), (8, 4), (4, 4), (4, 12), (0, 12)])
    regularized, _ = _regularize([make_segment((0, 8), (12, 8))])
    rays = _propagate(regularized, domain, k=1)
    assert len(rays) == 2
    assert all(ray.length > 4 for ray in rays)


def test_roofline_height_uses_emitted_mesh_vertices():
    from scripts.ml.rooflines import _part_edge_height
    part = {"coords": [(0, 0), (10, 0), (0, 10)], "ring_z": [3, 6, 3],
            "plane": {"a": 0, "b": 0, "c": 4}}
    assert _part_edge_height(part, (5, 0)) == 4.5
    assert _part_edge_height(part, (10, 0)) == 6


def test_extraction_only_uses_feedback_for_current_boundary(tmp_path):
    import json

    from scripts.ml.review_feedback import boundary_signature, latest_feedback, rejected_cuts_for
    record = {"centroid": {"lon": 145.3, "lat": -37.8}, "ring_meters": [[0, 0], [10, 0], [0, 10]]}
    cut = [[[0, 0], [10, 0]]]
    feedback = {"boundary_signature": boundary_signature(record), "rejected_cuts": cut, "updated_at": "2026-09-22"}
    path = tmp_path / "feedback.json"
    path.write_text(json.dumps({"benchmark:27": feedback, "older:27": {"updated_at": "2026-01-01"}}))
    assert rejected_cuts_for(record, latest_feedback(path)[27]) == cut
    record["centroid"]["lon"] += .001
    assert rejected_cuts_for(record, feedback) == []
