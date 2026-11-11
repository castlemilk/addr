import copy

import numpy as np
import pytest
from PIL import Image
from scripts.ml import roof_topology
from scripts.ml.roof_structure import audit_roof, shared_edges, structural_prior
from scripts.ml.roof_topology import select_roof_topology, support_envelopes
from shapely.affinity import rotate
from shapely.geometry import Polygon
from shapely.ops import unary_union

# A long L with small eave deviations, a chamfer and an occlusion-like notch.
# No scene/address rules are involved in candidate generation.
RING = [[0, 0], [8, 0], [8, 6], [12, 6], [12, 13], [11.6, 15],
        [10.8, 17], [11, 20], [12, 23], [10, 25], [7, 24.6],
        [5.8, 23.5], [.6, 24], [.5, 24.9], [0, 24]]
RING = (np.asarray(RING)*.9).tolist()
FRAME = [[10, 0], [0, -10], [180, 330]]
IMAGE = Image.new('RGB', (512, 512), (130, 120, 110))


@pytest.mark.parametrize('angle', [0, 31, 87])
@pytest.mark.parametrize('reverse', [False, True])
def test_small_bends_do_not_force_extra_hips_and_the_L_remains(angle, reverse):
    polygon = rotate(Polygon(RING[::-1] if reverse else RING), angle)
    prior = structural_prior(polygon, 3, 5, regularize_eaves=False)
    original = copy.deepcopy(prior)
    parts, report = select_roof_topology(prior, polygon, 3, 2, IMAGE, FRAME)
    assert report['accepted'], report
    # Clipping a notch may split one plane into disconnected surface pieces.
    planes = {tuple(round(p['plane'][key], 5) for key in ('a', 'b', 'c')) for p in parts}
    assert len(planes) == 6 and len(parts) < len(prior)
    assert audit_roof(parts, polygon, 3)['passed']
    covered = unary_union([Polygon(p['coords']) for p in parts])
    assert covered.symmetric_difference(polygon).area < 1e-6
    assert not covered.equals(polygon.convex_hull)
    assert prior == original


def test_real_courtyard_and_polygonal_ends_are_not_squared_off():
    courtyard = Polygon([[0, 0], [20, 0], [20, 20], [0, 20]], [[[5, 5], [15, 5], [15, 15], [5, 15]]])
    assert not support_envelopes(courtyard)
    angles = np.linspace(0, 2*np.pi, 8, endpoint=False)
    octagon = Polygon(np.c_[np.cos(angles), np.sin(angles)]*10)
    assert not support_envelopes(octagon)


def test_clear_observed_folds_veto_a_simpler_candidate(monkeypatch):
    polygon = Polygon(RING)
    prior = structural_prior(polygon, 3, 5, regularize_eaves=False)
    observations = []
    for line in shared_edges(prior):
        points = np.asarray(line)
        vector = points[1]-points[0]
        length = np.linalg.norm(vector)
        if length < .5:
            continue
        tangent = vector/length
        observations.append({'points': points, 'tangent': tangent, 'normal': np.array([-tangent[1], tangent[0]]),
                             'length': length, 'support': 1.})
    monkeypatch.setattr(roof_topology, 'image_segments', lambda *args: observations)
    # Isolate topology selection with strong known line observations. A blank
    # image does not permit the alternate candidate to move those observations.
    parts, report = select_roof_topology(prior, polygon, 3, 2, IMAGE, FRAME)
    assert report['candidates']
    assert not report['accepted'] and parts is prior
    assert report['before']['image_line_mismatch_m'] == 0


def test_full_rectangle_has_no_reason_to_change_topology():
    polygon = Polygon([[0, 0], [12, 0], [12, 8], [0, 8]])
    prior = structural_prior(polygon, 3, 5)
    parts, report = select_roof_topology(prior, polygon, 3, 2, IMAGE, FRAME)
    assert parts is prior and not report['accepted']


def test_simpler_wings_cannot_replace_a_clearly_observed_hip_with_a_gable(monkeypatch):
    polygon = Polygon([(0, 0), (24, 0), (24, 6), (15, 6), (15, 16), (9, 16), (9, 6), (0, 6)])
    hip_end = [[9, 16], [15, 16]]
    monkeypatch.setattr(roof_topology, 'select_roof_ends', lambda parts, *args, **kwargs: (parts, {
        'ends': [{'edge_meters': hip_end, 'selected': 'hip', 'image_choice': 'hip'}]}))
    prior = structural_prior(polygon, 3, 5, regularize_eaves=False)
    parts, report = select_roof_topology(prior, polygon, 3, 2, IMAGE, FRAME, prefer_simple=True)
    assert report['accepted'] and report['wing_refinement']['hip_ends'] == 1
    from scripts.ml.rooflines import _classify_roofline
    from shapely.geometry import LineString
    ridges = [LineString(line) for line in shared_edges(parts) if _classify_roofline(line, parts) == 'ridge']
    assert min(line.distance(LineString(hip_end)) for line in ridges) > 1


# --- CP3 evidence views are forwarded to both image_segments and refine_roof_lines ---------

def test_evidence_views_are_forwarded_only_when_given_and_gated_by_the_original_rgb(monkeypatch):
    from scripts.ml import roof_line_fit
    seen = {'segments': [], 'refine': []}
    segments, refine = roof_topology.image_segments, roof_topology.refine_roof_lines

    def spy_segments(*args, **kwargs):
        seen['segments'].append(kwargs)
        return segments(*args, **kwargs)

    def spy_refine(*args, **kwargs):
        seen['refine'].append({k: v for k, v in kwargs.items() if k != 'evidence_footprint'})
        return refine(*args, **kwargs)
    monkeypatch.setattr(roof_topology, 'image_segments', spy_segments)
    monkeypatch.setattr(roof_topology, 'refine_roof_lines', spy_refine)
    polygon = Polygon(RING)
    prior = structural_prior(polygon, 3, 5, regularize_eaves=False)
    parts, default = select_roof_topology(prior, polygon, 3, 2, IMAGE, FRAME)
    assert seen['segments'] == [{}] and all(call == {} for call in seen['refine'])
    assert 'evidence_views' not in default
    seen['segments'].clear()
    seen['refine'].clear()
    # A view full of hard edges over a uniform saved image: every view line fails the RGB gate.
    view = np.full((512, 512), 90, np.uint8)
    view[::24] = 220
    view[:, ::40] = 30
    views = [('stack-median', view)]
    parts_v, report = select_roof_topology(prior, polygon, 3, 2, IMAGE, FRAME, evidence_views=views)
    assert seen['segments'] == [{'extra_views': views}]
    assert seen['refine'] and all(call == {'extra_views': views} for call in seen['refine'])
    assert report['evidence_views'] == {'names': ['stack-median'], 'method': roof_line_fit.EXTRA_VIEWS_VERSION,
                                        'view_origins': {}}
    strip = {k: v for k, v in report.items() if k != 'evidence_views'}
    strip['line_refinement'] = {k: v for k, v in report['line_refinement'].items() if k not in ('extra_views', 'view_origins')}
    assert report['line_refinement']['view_origins'] == {} and report['line_refinement']['extra_views']['names'] == ['stack-median']
    assert strip == default, 'ungated view lines change nothing'
    assert [p['coords'] for p in parts_v] == [p['coords'] for p in parts]


# --- roof-topology-support-v1: image-supported simplification ------------------------------

def test_l_roof_with_two_shaded_ridges_keeps_both_wings_and_gets_no_single_ridge_candidate():
    from scripts.ml.rooflines import _classify_roofline
    from scripts.ml.tests.test_topology_image_support import shaded_by_plane
    from shapely.geometry import LineString
    L = Polygon([[0, 0], [24, 0], [24, 8], [8, 8], [8, 20], [0, 20]])
    prior = structural_prior(L, 3, 5, regularize_eaves=False)
    image = shaded_by_plane(prior)
    parts, report = select_roof_topology(prior, L, 3, 2, image, FRAME, prefer_simple=True)
    support = report['support']
    assert support['method'] == roof_topology.TOPOLOGY_SUPPORT_VERSION
    # support-v2 may offer a two-rectangle L candidate here; a single-ridge (4-corner) rectangle is never offered for an L.
    assert not any(c.get('support_corners') == 4 for c in support['candidates']), 'an L is not a single-ridge roof'
    ridges = [LineString(line) for line in shared_edges(parts) if _classify_roofline(line, parts) == 'ridge']
    along_x = [r for r in ridges if abs(r.coords[1][1]-r.coords[0][1]) < 1e-6]
    along_y = [r for r in ridges if abs(r.coords[1][0]-r.coords[0][0]) < 1e-6]
    assert along_x and along_y, 'both wing ridges are retained'
    assert unary_union([Polygon(p['coords']) for p in parts]).symmetric_difference(L).area < 1e-6
    assert audit_roof(parts, L, 3)['passed']


def test_support_stage_is_recorded_but_silent_on_the_uniform_image_l():
    polygon = Polygon(RING)
    prior = structural_prior(polygon, 3, 5, regularize_eaves=False)
    parts, report = select_roof_topology(prior, polygon, 3, 2, IMAGE, FRAME)
    assert report['accepted'] and report['reason'] == 'simpler-supported-roof'
    assert report['method'] == roof_topology.VERSION
    assert report['support']['method'] == roof_topology.TOPOLOGY_SUPPORT_VERSION
    assert not report['support']['accepted']
    assert 'support_penalty' not in report['after'] and 'support_penalty' not in report['before']
