import numpy as np
import pytest
from scripts.ml.roof_structure import audit_roof, shared_edges
from scripts.ml.roof_wings import roof_rectangles, wing_candidates
from scripts.ml.rooflines import _classify_roofline
from shapely.affinity import rotate
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

L = Polygon([(0, 0), (24, 0), (24, 6), (8, 6), (8, 22), (0, 22)])
T = Polygon([(0, 0), (24, 0), (24, 6), (15, 6), (15, 16), (9, 16), (9, 6), (0, 6)])


@pytest.mark.parametrize('shape', [L, T])
@pytest.mark.parametrize('angle', [0, 31, 87])
@pytest.mark.parametrize('reverse', [False, True])
def test_connected_wings_have_continuous_planar_surfaces(shape, angle, reverse):
    p = rotate(shape, angle)
    if reverse:
        p = Polygon(list(p.exterior.coords)[::-1])
    candidates = wing_candidates(p, 3, 2)
    assert candidates
    parts, info = candidates[0]
    assert info['hip_ends'] == 0 and info['branch_ends'] == 1
    audit = audit_roof(parts, p, 3)
    assert audit['passed'] and audit['drainage']['passed'], audit
    assert audit['max_seam_error_m'] < 1e-5
    planes = {tuple(round(part['plane'][k], 5) for k in ('a', 'b', 'c')) for part in parts}
    assert len(planes) == 4
    assert unary_union([Polygon(part['coords']) for part in parts]).symmetric_difference(p).area < 1e-5
    assert all(Polygon(part['coords']).area > 1e-6 for part in parts)


def test_t_branch_joins_one_through_ridge_without_a_four_way_spur():
    parts, _ = wing_candidates(T, 3, 2)[0]
    ridges = unary_union([LineString(line) for line in shared_edges(parts) if _classify_roofline(line, parts) == 'ridge'])
    expected = unary_union([LineString([(0, 3), (24, 3)]), LineString([(12, 3), (12, 16)])])
    assert ridges.hausdorff_distance(expected) < 1e-6
    assert abs(ridges.length-expected.length) < 1e-5


@pytest.mark.parametrize('angle', [0, 32, 87])
def test_l_junction_keeps_both_valleys_including_the_straight_eave_exit(angle):
    # The second valley reaches the middle of a straight boundary, not a
    # concave outline corner. Dropping it leaves the through-wing incomplete.
    shape = rotate(L, angle, origin=(0, 0))
    parts, _ = wing_candidates(shape, 3, 2)[0]
    valleys = unary_union([LineString(line) for line in shared_edges(parts) if _classify_roofline(line, parts) == 'valley'])
    expected = rotate(unary_union([LineString([(8, 6), (4, 3)]), LineString([(4, 3), (0, 6)])]), angle, origin=(0, 0))
    assert valleys.hausdorff_distance(expected) < 1e-6
    assert abs(valleys.length-expected.length) < 1e-6
    audit = audit_roof(parts, shape, 3)
    assert audit['passed'] and audit['drainage']['passed']


def test_hip_alternatives_retain_shared_height_and_runoff():
    candidates = wing_candidates(T, 3, 2)
    hips = [(parts, info) for parts, info in candidates if info['hip_ends']]
    assert len(hips) == 3
    for parts, _ in hips:
        audit = audit_roof(parts, T, 3)
        assert audit['passed'] and audit['drainage']['passed']
        assert any(_classify_roofline(line, parts) == 'hip' for line in shared_edges(parts))


def test_real_courtyards_and_non_rectangular_footprints_use_existing_model():
    courtyard = Polygon(box(0, 0, 20, 20).exterior.coords, [box(5, 5, 15, 15).exterior.coords])
    angles = np.linspace(0, 2*np.pi, 8, endpoint=False)
    octagon = Polygon(np.c_[np.cos(angles), np.sin(angles)]*10)
    for p in [courtyard, octagon, box(0, 0, 12, 8)]:
        assert not roof_rectangles(p)
        assert not wing_candidates(p, 3, 2)
