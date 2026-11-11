import copy

import pytest
from scripts.ml.roof_physics import drainage_audit
from scripts.ml.roof_structure import _parts_from_faces, audit_roof, structural_prior
from shapely.geometry import Polygon


def butterfly(fall):
    # Each face is planar and joins continuously. A level, internal valley
    # passes a vertex-only basin test because its endpoints lie at the eaves.
    faces = []
    for xy in ([[0, 0], [5, 0], [5, 10], [0, 10]], [[5, 0], [10, 0], [10, 10], [5, 10]]):
        faces.append((xy, [2+abs(x-5)*.4+fall*(10-y) for x, y in xy]))
    return _parts_from_faces(faces, 1)


def test_level_valley_is_trapped_even_when_its_end_vertices_are_on_boundary():
    footprint = Polygon([[0, 0], [10, 0], [10, 10], [0, 10]])
    report = drainage_audit(butterfly(0), footprint)
    assert not report['passed'] and report['trapped_samples'] == report['sample_count']
    audit = audit_roof(butterfly(0), footprint, 1)
    assert 'trapped-roof-runoff' in audit['issues']
    assert 'interior-low-point' not in audit['issues']


def test_sloping_valley_routes_water_to_boundary():
    footprint = Polygon([[0, 0], [10, 0], [10, 10], [0, 10]])
    parts = butterfly(.1)
    original = copy.deepcopy(parts)
    report = drainage_audit(parts, footprint)
    assert report['passed'] and report['boundary_exits'] == report['sample_count']
    assert parts == original


@pytest.mark.parametrize('form', ['hipped', 'gabled'])
def test_concave_roof_routes_faces_and_valleys_without_false_basins(form):
    footprint = Polygon([[0, 0], [14, 0], [14, 5], [5, 5], [5, 14], [0, 14]])
    parts = structural_prior(footprint, 3, 5, form)
    assert drainage_audit(parts, footprint)['passed']


def test_courtyard_exits_and_flat_roof_scope_are_explicit():
    footprint = Polygon([[0, 0], [20, 0], [20, 20], [0, 20]], [[[6, 6], [14, 6], [14, 14], [6, 14]]])
    assert drainage_audit(structural_prior(footprint, 3, 5), footprint)['passed']
    report = drainage_audit(structural_prior(footprint, 3, 3, 'flat'), footprint, pitched=False)
    assert report['status'] == 'not-assessed-flat-roof' and report['sample_count'] == 0


def test_checks_geometry_not_claimed_plane_metadata():
    footprint = Polygon([[0, 0], [10, 0], [10, 10], [0, 10]])
    parts = butterfly(0)
    for part in parts:
        part['plane'] = {'a': 1, 'b': 1, 'c': 10}
    assert not drainage_audit(parts, footprint)['passed']
