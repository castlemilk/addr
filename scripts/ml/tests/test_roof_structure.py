from copy import deepcopy

import numpy as np
import pytest
from scripts.ml.roof_structure import (
    audit_roof,
    regularize_eave_corners,
    shared_edges,
    structural_prior,
)
from shapely.affinity import rotate
from shapely.geometry import Polygon
from shapely.ops import unary_union


def test_skeleton_library_failure_is_a_rejected_candidate(monkeypatch):
    import py_straight_skeleton
    def fail(**kwargs):
        raise RuntimeError('Ran to an original node not part of the initiating edge')
    monkeypatch.setattr(py_straight_skeleton, 'compute_skeleton', fail)
    with pytest.raises(ValueError, match='Roof skeleton could not be constructed'):
        structural_prior(Polygon([(0, 0), (12, 0), (12, 8), (0, 8)]), 3, 5)


@pytest.mark.parametrize("ring,kind,face_count", [
    ([(0, 0), (12, 0), (12, 8), (0, 8)], "hipped", 4),
    ([(0, 0), (12, 0), (12, 8), (0, 8)], "gabled", 2),
    ([(0, 0), (16, 0), (16, 6), (6, 6), (6, 16), (0, 16)], "hipped", 6),
])
def test_priors_have_planar_faces_closed_seams_and_complete_coverage(ring, kind, face_count):
    footprint = Polygon(ring)
    parts = structural_prior(footprint, 3, 6, kind)
    assert len(parts) == face_count
    assert audit_roof(parts, footprint, 3)["passed"]
    union = unary_union([Polygon(p["coords"]) for p in parts])
    assert union.symmetric_difference(footprint).area < 1e-6
    assert list(shared_edges(parts))
    for p in parts:
        z = np.array(p["ring_z"])
        xy = np.array(p["coords"])
        plane = p["plane"]
        assert np.max(np.abs(xy @ [plane["a"], plane["b"]] + plane["c"] - z)) < 1e-6
        from scripts.ml.rooflines import _triangulate_z
        triangles = _triangulate_z(p["coords"], {(round(x, 2), round(y, 2)): z for (x, y), z in zip(p["coords"], p["ring_z"], strict=True)})
        assert sum(Polygon([(x, y) for x, y, _ in t]).area for t in triangles) == pytest.approx(p["area_m2"], abs=.02)


def test_hip_prior_runs_downhill_to_its_own_eave():
    footprint = Polygon([(0, 0), (16, 0), (16, 6), (6, 6), (6, 16), (0, 16)])
    for part in structural_prior(footprint, 3, 6):
        a, b = np.array(part["coords"][:2])
        tangent = b - a
        plane = part["plane"]
        uphill = np.array([plane["a"], plane["b"]])
        assert abs(np.dot(tangent, uphill)) < 1e-6
        assert np.dot(uphill, [-tangent[1], tangent[0]]) > 0
        assert part["ring_z"][:2] == pytest.approx([3, 3])


def test_audit_catches_twisted_quad_disconnected_peak_and_missing_face():
    footprint = Polygon([(0, 0), (12, 0), (12, 8), (0, 8)])
    original = structural_prior(footprint, 3, 6)
    parts = deepcopy(original)
    parts[0]["ring_z"][2] += 1.0
    issues = audit_roof(parts, footprint, 3)["issues"]
    assert "non-planar-face" in issues
    assert "disconnected-heights" in issues
    assert "footprint-coverage" in audit_roof(original[:-1], footprint, 3)["issues"]
    assert "overlapping-faces" in audit_roof(original + original[:1], footprint, 3)["issues"]


def test_pitched_roof_cannot_silently_collapse_to_a_flat_cap():
    footprint = Polygon([(0, 0), (12, 0), (12, 8), (0, 8)])
    parts = structural_prior(footprint, 3, 3, "flat")
    assert "missing-apex" in audit_roof(parts, footprint, 3)["issues"]
    assert audit_roof(parts, footprint, 3, pitched=False)["passed"]


def test_interior_basin_needs_explicit_drain_evidence():
    ring = [(0, 0), (10, 0), (10, 10), (0, 10)]
    parts = [{"coords": [ring[i], ring[(i + 1) % 4], (5, 5)], "ring_z": [6, 6, 4]} for i in range(4)]
    assert "interior-low-point" in audit_roof(parts, Polygon(ring), 3)["issues"]


def test_serialization_does_not_turn_close_junctions_into_self_intersections():
    from scripts.ml.addresses import _roofline_payload
    from scripts.ml.rooflines import _reconcile_part_heights

    ring = [(0, 0), (4, 0), (4, 4), (2.001, 4), (2.004, 3.5), (2, 3.501), (2, 4), (0, 4)]
    footprint = Polygon(ring)
    assert footprint.is_valid
    # Centimetre rounding collapses the narrow notch into a self-touching edge.
    assert not Polygon([(round(x, 2), round(y, 2)) for x, y in ring]).is_valid
    parts = [{"coords": ring, "plane": {"a": 0, "b": 0, "c": 3}, "area_m2": footprint.area, "slope_deg": 0}]
    _reconcile_part_heights(parts, footprint, 3, 3)
    payload = _roofline_payload({"parts": parts, "rooflines": []})
    stored = payload["parts"][0]
    assert Polygon(stored["ring"]).is_valid
    assert Polygon(stored["ring"]).symmetric_difference(footprint).area < 1e-9


def test_prior_preserves_rotated_footprint_and_courtyard_holes():
    from shapely.affinity import rotate

    p = rotate(Polygon([(0, 0), (12, 0), (12, 8), (0, 8)]), 37)
    assert audit_roof(structural_prior(p, 3, 6, "gabled"), p, 3)["passed"]
    p = Polygon([(0, 0), (12, 0), (12, 8), (0, 8)], [[(4, 3), (4, 5), (6, 5), (6, 3)]])
    parts = structural_prior(rotate(p, 37), 3, 6)
    assert audit_roof(parts, rotate(p, 37), 3)["passed"]


def test_no_image_evidence_uses_labelled_prior_without_moving_outline():
    from types import SimpleNamespace

    from PIL import Image
    from scripts.ml.imagery import ImageryWindow
    from scripts.ml.rooflines import _extract_rooflines
    from scripts.ml.tiles import lonlat_to_tile_xy

    lon, lat, zoom = 145.3125, -37.7903, 18
    x, y = map(int, lonlat_to_tile_xy(lon, lat, zoom))
    window = ImageryWindow(Image.new("RGB", (576, 576), "gray"), zoom, x, y, 19, 512, 32, 1., 512, 120.)
    extractor = SimpleNamespace(zoom=zoom, compositor=None, use_pointcloud=True,
                                fetcher=SimpleNamespace(window=lambda *_: window), _depth=lambda _: None)
    ring = [[-8, -4], [8, -4], [8, 4], [-8, 4]]
    result = _extract_rooflines(extractor, {"address_id": 123, "centroid": {"lon": lon, "lat": lat},
                                          "ring_meters": ring, "eave_height": 3, "ridge_height": 6,
                                          "roof_type": "gabled"})
    assert result["geometry_source"] == "structural-prior"
    assert result["height_source"] == "shape-prior-estimate"
    assert result["structure_audit"]["passed"]
    assert not result["image_structure_audit"]["passed"]
    assert result["footprint_coords"] == ring
    assert len(result["parts"]) == 2
    assert len(result["rooflines"]) == 1
    assert result["rooflines"][0]["kind"] == "ridge"


@pytest.mark.parametrize("angle", [0, 31, 119])
@pytest.mark.parametrize("reverse", [False, True])
def test_small_bevels_propose_one_triangular_hip_at_a_straight_end(angle, reverse):
    # A long wing whose far two corners were truncated by a blurry mask.
    ring = [(0, 0), (18, 0), (20, 1), (20, 7), (18, 8), (0, 8)]
    if reverse:
        ring.reverse()
    expected = rotate(Polygon([(0, 0), (20, 0), (20, 8), (0, 8)]), angle, origin=(0, 0))
    footprint = rotate(Polygon(ring), angle, origin=(0, 0))
    for offset in range(len(ring)):
        vertices = list(footprint.exterior.coords[:-1])
        shifted = Polygon(vertices[offset:] + vertices[:offset])
        proposal = regularize_eave_corners(shifted)
        assert proposal["removed_bevels"] == 2
        assert proposal["polygon"].symmetric_difference(expected).area < 1e-8
    parts = structural_prior(proposal["polygon"], 3, 6)
    assert len(parts) == 4
    assert sum(len(p["coords"]) == 3 for p in parts) == 2
    assert audit_roof(parts, proposal["polygon"], 3)["passed"]


def test_regularized_planes_keep_the_captured_outline_and_consistent_heights():
    footprint = Polygon([(0, 0), (18, 0), (20, 1), (20, 7), (18, 8), (0, 8)])
    parts = structural_prior(footprint, 3, 6)
    assert len(parts) == 4
    assert parts[0]["eave_regularization"]["removed_bevels"] == 2
    assert audit_roof(parts, footprint, 3)["passed"]
    assert unary_union([Polygon(p["coords"]) for p in parts]).symmetric_difference(footprint).area < 1e-8
    # Explicitly reviewed footprints can retain genuine chamfered hips.
    exact = structural_prior(footprint, 3, 6, regularize_eaves=False)
    assert len(exact) == 6
    assert exact[0]["eave_regularization"]["removed_bevels"] == 0


def test_regularization_preserves_polygonal_roofs_notches_and_large_chamfers():
    octagon = Polygon([(5 * np.cos(a), 5 * np.sin(a)) for a in np.arange(8) * np.pi / 4])
    notch = Polygon([(0, 0), (20, 0), (20, 8), (12, 8), (12, 6), (11, 5), (8, 5), (8, 8), (0, 8)])
    chamfer = Polygon([(0, 0), (14, 0), (20, 6), (20, 12), (0, 12)])
    for footprint in (octagon, notch, chamfer):
        proposal = regularize_eave_corners(footprint)
        assert proposal["removed_bevels"] == 0
        assert proposal["polygon"].equals(footprint)
    small = Polygon([(0, 0), (18, 0), (20, 1), (20, 7), (18, 8), (0, 8)])
    assert regularize_eave_corners(small, max_deviation_m=.5)["removed_bevels"] == 0


def test_reviewed_l_wing_and_garage_have_simple_hip_ends_without_bevel_fans():
    import json
    from pathlib import Path

    from shapely.geometry import LineString

    reference = json.loads((Path(__file__).resolve().parents[3] / "data/references/roof-eave-corrections-2026-09-22.json").read_text())
    before = reference["before_ring_meters"]
    proposal = regularize_eave_corners(Polygon(before))
    assert proposal["removed_bevels"] == 3
    assert proposal["polygon"].symmetric_difference(Polygon(reference["after_ring_meters"])).area < 1e-8
    parts = structural_prior(proposal["polygon"], 2.7, 4.8, regularize_eaves=False)
    assert len(parts) == 14
    for i in (3, 12, 15):
        eave = LineString([before[i], before[(i + 1) % len(before)]])
        faces = [p for p in parts if Polygon(p["coords"]).boundary.buffer(1e-6).intersection(eave).length > .95 * eave.length]
        assert len(faces) == 1
        # Free ends are triangles. At the attached garage junction the shared
        # main roof intersects this one plane, so its boundary is a quad.
        if i in (3, 12):
            assert len(faces[0]["coords"]) == 3


@pytest.mark.parametrize("source,expected_faces", [("crm", 6), ("extracted", 4)])
def test_extraction_respects_reviewed_eaves_and_persists_prior_assumptions(source, expected_faces):
    from types import SimpleNamespace

    from PIL import Image
    from scripts.ml.addresses import _roofline_payload
    from scripts.ml.imagery import ImageryWindow
    from scripts.ml.rooflines import _extract_rooflines
    from scripts.ml.tiles import lonlat_to_tile_xy

    lon, lat, zoom = 145.3125, -37.7903, 18
    x, y = map(int, lonlat_to_tile_xy(lon, lat, zoom))
    window = ImageryWindow(Image.new("RGB", (576, 576), "gray"), zoom, x, y, 19, 512, 32, 1., 512, 120.)
    extractor = SimpleNamespace(zoom=zoom, compositor=None, use_pointcloud=True,
                                fetcher=SimpleNamespace(window=lambda *_: window), _depth=lambda _: None)
    ring = [(0, 0), (18, 0), (20, 1), (20, 7), (18, 8), (0, 8)]
    result = _extract_rooflines(extractor, {"address_id": 123, "centroid": {"lon": lon, "lat": lat},
                                          "ring_meters": ring, "eave_height": 3, "ridge_height": 6,
                                          "roof_type": "hipped", "boundary_source": source})
    assert result["geometry_source"] == "structural-prior"
    assert result["structure_audit"]["passed"]
    assert Polygon(result["footprint_coords"]).equals(Polygon(ring))
    assert len(result["parts"]) == expected_faces
    payload = _roofline_payload(result)
    assert payload["eave_regularization"]["removed_bevels"] == 6 - expected_faces


def test_shared_fold_recovers_rounding_gaps_even_when_another_fragment_is_exact():
    from scripts.ml.roof_structure import shared_edges
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    first = {'coords': [(0, 0), (2, 0), (8, 0), (10, 0), (10, 5), (0, 5)],
             'plane': {'a': 0, 'b': -.5, 'c': 6}}
    second = {'coords': [(0, 0), (0, -5), (10, -5), (10, 0), (8, 0), (7, -1e-9), (3, -1e-9), (2, 0)],
              'plane': {'a': 0, 'b': .5, 'c': 6}}
    folds = unary_union([LineString(line) for line in shared_edges([first, second])])
    assert folds.buffer(1e-6).covers(LineString([(0, 0), (10, 0)]))
    assert abs(folds.length-10) < 1e-5
    assert list(shared_edges([first, {**second, 'plane': first['plane']}])) == []


@pytest.mark.parametrize('origin', [0, 1000])
def test_numerical_plane_seams_are_not_roof_folds_but_real_pitch_changes_are(origin):
    first = {'coords': [(origin, 0), (origin+5, 0), (origin+5, 5), (origin, 5)],
             'plane': {'a': .2, 'b': .3, 'c': 3-.2*origin}}
    second = {'coords': [(origin+5, 0), (origin+10, 0), (origin+10, 5), (origin+5, 5)],
              'plane': {'a': .20001, 'b': .3, 'c': 3-.20001*origin-.00005}}
    assert list(shared_edges([first, second])) == []
    second['plane'] = {'a': .21, 'b': .3, 'c': 3-.21*origin-.05}
    assert list(shared_edges([first, second])), 'Retain actual pitch changes and their shared ridge elevation'
