import copy

import numpy as np
import pytest
from PIL import Image
from scripts.ml import roof_junctions, roof_topology
from scripts.ml.roof_junctions import construction_complexity, junction_candidates
from scripts.ml.roof_line_fit import _graph
from scripts.ml.roof_structure import _parts_from_faces, audit_roof, structural_prior
from scripts.ml.roof_topology import select_roof_topology
from shapely.affinity import rotate
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import split, unary_union

RING = [(0, 0), (24, 0), (24, 20), (17, 20), (17, 8), (0, 8)]
IMAGE = Image.new('RGB', (512, 512), (130, 120, 110))
FRAME = [[10, 0], [0, -10], [50, 330]]


@pytest.mark.parametrize('angle', [0, 31, 87])
@pytest.mark.parametrize('reverse', [False, True])
def test_unequal_wings_join_ridges_without_changing_outline_or_free_hips(angle, reverse):
    footprint = rotate(Polygon(RING[::-1] if reverse else RING), angle)
    parts = structural_prior(footprint, 3, 5, regularize_eaves=False)
    original = copy.deepcopy(parts)
    candidates = junction_candidates(parts, footprint, 3)
    assert candidates
    candidate, report = candidates[0]
    assert parts == original
    before, after = construction_complexity(parts), construction_complexity(candidate)
    assert before['physical_planes'] == after['physical_planes'] == 6
    assert before['junctions'] == after['junctions']+1
    assert before['short_junction_links'] == 1 and after['short_junction_links'] == 0
    assert after['construction_complexity'] < before['construction_complexity']
    assert report['removed_junction_nodes'] == 1
    assert report['max_shift_m'] < .5
    audit = audit_roof(candidate, footprint, 3)
    assert audit['passed'] and audit['drainage']['passed']
    assert audit['max_planarity_error_m'] < .001 and audit['max_seam_error_m'] < .001
    covered = unary_union([Polygon(p['coords']) for p in candidate])
    assert covered.symmetric_difference(footprint).area < 1e-6
    for corner in footprint.exterior.coords[:-1]:
        vertices = [(xy, z) for p in candidate for xy, z in zip(p['coords'], p['ring_z'], strict=True)
                    if np.linalg.norm(np.asarray(xy)-corner) < 1e-6]
        assert vertices and all(z == 3 for _, z in vertices)
    # Unequal widths need unequal pitches to meet at a continuous level ridge.
    assert len({round(p['slope_deg']) for p in candidate}) > 1
    v, _, edges = _graph(candidate)
    free_hips = [(a, b) for a, b in edges if min(v[[a, b], 2]) == 3 and max(v[[a, b], 2]) > 3]
    assert len(free_hips) == 6, 'The four free-end hips, outer-corner hip and L valley remain'


def test_straight_L_is_a_candidate_even_without_a_new_support_envelope():
    footprint = Polygon(RING)
    parts = structural_prior(footprint, 3, 5, regularize_eaves=False)
    selected, report = select_roof_topology(parts, footprint, 3, 2, IMAGE, FRAME)
    assert report['accepted'] and report['selected_support']['unchanged_support']
    assert len(selected) == len(parts), 'Equal face counts can still have simpler junctions'
    assert report['junction_refinement']['removed_junction_nodes'] == 1


def test_complexity_ignores_collinear_vertices_and_coplanar_storage_fragments():
    parts = structural_prior(Polygon(RING), 3, 5, regularize_eaves=False)
    densified = []
    for part in parts:
        xy, z = np.asarray(part['coords']), np.asarray(part['ring_z'])
        points, heights = [], []
        for a, b, c, d in zip(xy, np.roll(xy, -1, axis=0), z, np.roll(z, -1), strict=True):
            points.extend([a, (a+b)/2])
            heights.extend([c, (c+d)/2])
        densified.append((points, heights))
    assert construction_complexity(_parts_from_faces(densified, 3)) == construction_complexity(parts)
    # Split all planes into pieces using an unrelated storage seam.
    fragmented = []
    for part in parts:
        abc = np.array([part['plane'][key] for key in ('a', 'b', 'c')])
        for piece in split(Polygon(part['coords']), LineString([[-1, 6], [30, 6]])).geoms:
            xy = np.asarray(piece.exterior.coords[:-1])
            fragmented.append((xy, np.c_[xy, np.ones(len(xy))] @ abc))
    assert len(fragmented) > len(parts)
    assert construction_complexity(_parts_from_faces(fragmented, 3)) == construction_complexity(parts)


def test_clear_image_evidence_for_separate_junctions_vetoes_the_prior(monkeypatch):
    footprint = Polygon([(0, 0), (24, 0), (24, 20), (17, 20), (17, 10), (0, 10)])
    parts = structural_prior(footprint, 3, 5, regularize_eaves=False)
    vertices, _, edges = _graph(parts)
    observations = []
    for a, b in edges:
        line = vertices[[a, b], :2]
        if min(vertices[[a, b], 2]) > 3 and .1 < np.linalg.norm(line[1]-line[0]) < 2.5:
            vector = line[1]-line[0]
            length = np.linalg.norm(vector)
            tangent = vector/length
            observations.append({'points': line, 'tangent': tangent, 'normal': np.array([-tangent[1], tangent[0]]),
                                 'length': length, 'support': 1.})
    assert observations and junction_candidates(parts, footprint, 3)
    monkeypatch.setattr(roof_topology, 'image_segments', lambda *args: observations)
    selected, report = select_roof_topology(parts, footprint, 3, 2, IMAGE, FRAME)
    assert report['candidates']
    assert selected is parts and not report['accepted']


def test_courtyards_distinct_levels_and_simple_roofs_are_not_flattened():
    rectangle = Polygon([(0, 0), (20, 0), (20, 8), (0, 8)])
    assert not junction_candidates(structural_prior(rectangle, 3, 5), rectangle, 3)
    courtyard = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)], [[(7, 7), (13, 7), (13, 13), (7, 13)]])
    assert not junction_candidates(structural_prior(courtyard, 3, 5), courtyard, 3)
    footprint = Polygon(RING)
    parts = structural_prior(footprint, 3, 5, regularize_eaves=False)
    # An independent lower roof level must not be lifted to the main ridge.
    vertices, _, _ = _graph(parts)
    lower = next(i for i, v in enumerate(vertices) if v[2] > 3 and footprint.boundary.distance(Point(v[:2])) > .1)
    for part in parts:
        for i, xy in enumerate(part['coords']):
            if np.linalg.norm(np.asarray(xy)-vertices[lower, :2]) < 1e-6:
                part['ring_z'][i] = 3.3
    assert not junction_candidates(parts, footprint, 3)


@pytest.mark.parametrize('failure', [ValueError, GEOSException])
def test_unstable_solver_falls_back_without_mutating_input(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure('Unstable fit')
    footprint = Polygon(RING)
    parts = structural_prior(footprint, 3, 5, regularize_eaves=False)
    original = copy.deepcopy(parts)
    monkeypatch.setattr(roof_junctions, 'least_squares', fail)
    assert not junction_candidates(parts, footprint, 3)
    assert parts == original
