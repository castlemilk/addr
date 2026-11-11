"""Topology that follows the visible roof lines (roof-topology-support-v1).

Synthetic roofs only: a long rectangle with a V notch whose straight skeleton invents
valleys, painted with a single brightness step along the long axis. The simpler
single-ridge hypothesis must win only when the image supports it and must lose when
the observed lines belong to the complex roof. No scene data, no labels.
"""
import numpy as np
import pytest
import shapely
from PIL import Image
from scripts.ml import roof_topology
from scripts.ml.roof_structure import audit_roof, shared_edges, structural_prior
from scripts.ml.roof_topology import (
    TOPOLOGY_SUPPORT_VERSION,
    clip_support,
    crease_support,
    minimal_supports,
    select_roof_topology,
    topology_cost,
)
from scripts.ml.rooflines import _classify_roofline
from shapely.affinity import rotate
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

FRAME = [[10, 0], [0, -10], [180, 330]]
SIZE = (512, 512)
# 24 x 8 m rectangle with a 2.5 m deep V notch: the axis envelope cannot absorb the
# notch (same-axis adjacent groups), so the existing candidate generator yields
# nothing, while the minimum rotated rectangle still covers 98% of the area.
NOTCHED = [[0, 0], [10, 0], [11.5, 2.5], [13, 0], [24, 0], [24, 8], [0, 8]]
RIDGE = [[0, 4], [24, 4]]


def metres_grid(frame=FRAME, size=SIZE):
    width, height = size
    xs, ys = np.meshgrid(np.arange(width, dtype=float), np.arange(height, dtype=float))
    inverse = np.linalg.inv(np.c_[np.asarray(frame, float), [0, 0, 1]])
    points = np.c_[xs.ravel(), ys.ravel(), np.ones(xs.size)] @ inverse
    return points[:, 0].reshape(height, width), points[:, 1].reshape(height, width)


def two_tone(polygon, ridge, *, bright=150, dark=110, background=130):
    """Brightness step along `ridge` inside the roof only; uniform ground outside."""
    x, y = metres_grid()
    inside = shapely.contains_xy(polygon, x, y)
    a, b = np.asarray(ridge, float)
    side = (b[0]-a[0])*(y-a[1])-(b[1]-a[1])*(x-a[0]) > 0
    gray = np.full(x.shape, background, np.uint8)
    gray[inside & side] = bright
    gray[inside & ~side] = dark
    return Image.fromarray(np.dstack([gray]*3))


def shaded_by_plane(parts, *, background=130):
    """Each roof plane gets a brightness by its aspect, so every true crease is a step."""
    x, y = metres_grid()
    gray = np.full(x.shape, background, np.uint8)
    levels = {0: 150, 1: 110, 2: 135, 3: 95}
    for part in parts:
        gray[shapely.contains_xy(Polygon(part['coords']), x, y)] = levels[int(part['azimuth_deg'] // 90) % 4]
    return Image.fromarray(np.dstack([gray]*3))


def ridge_runs(parts):
    lines = [line for line in shared_edges(parts) if _classify_roofline(line, parts) == 'ridge']
    return unary_union([LineString(line) for line in lines]).geom_type if lines else None, lines


@pytest.mark.parametrize('angle', [0, 31])
def test_minimal_support_is_the_rotated_rectangle_only_when_it_covers_the_outline(angle):
    notched = rotate(Polygon(NOTCHED), angle, origin=(0, 0))
    supports = minimal_supports(notched)
    assert len(supports) == 1
    support, detail = supports[0]
    assert detail['minimal_support'] and detail['rectangle_coverage'] > .95
    assert support.buffer(1e-7).covers(notched) and len(support.exterior.coords) == 5
    # An L covers far less than 85% of its rotated rectangle: no single ridge. Since v2 it gets
    # the two-rectangle L support instead (tests/test_topology_lt_supports.py); never a 4-corner one.
    L = Polygon([[0, 0], [24, 0], [24, 8], [8, 8], [8, 20], [0, 20]])
    assert [d['support_corners'] for _, d in minimal_supports(L)] == [6]
    courtyard = Polygon([[0, 0], [24, 0], [24, 12], [0, 12]], [[[8, 3], [16, 3], [16, 9], [8, 9]]])
    assert minimal_supports(courtyard) == []


def test_unsupported_valleys_rank_below_a_supported_single_ridge():
    footprint = Polygon(NOTCHED)
    image = two_tone(footprint, RIDGE)
    complex_parts = structural_prior(footprint, 3, 5, regularize_eaves=False)
    kinds = {_classify_roofline(line, complex_parts) for line in shared_edges(complex_parts)}
    assert 'valley' in kinds, 'the notch must create valleys in the skeleton'
    simple_parts = clip_support(structural_prior(footprint.minimum_rotated_rectangle, 3, 5, 'gabled'), footprint, 3, 'gabled')
    observations = roof_topology.image_segments(image, footprint, FRAME)
    assert observations, 'the brightness step must be observed as a line'
    complex_support = crease_support(complex_parts, footprint, observations, image, FRAME)
    simple_support = crease_support(simple_parts, footprint, observations, image, FRAME)
    assert simple_support['method'] == TOPOLOGY_SUPPORT_VERSION
    assert simple_support['unsupported_share'] == 0 and simple_support['creases'] == 1
    assert complex_support['unsupported_share'] > .3
    assert complex_support['unsupported_crease_length_m'] > 5
    # Contrast alone supports the ridge pieces of the complex roof; hips and valleys have no step.
    assert any(c['delta_l'] is not None and c['delta_l'] >= 6 for c in complex_support['per_crease'])
    folds = [c for c in complex_support['per_crease'] if c['kind'] in ('hip', 'valley')]
    assert folds and all(c['delta_l'] is None or c['delta_l'] < 6 for c in folds if c['kind'] == 'valley')
    # Hips ending on the ridge see a little of the step in their last metre; by length most stay below it.
    assert sum(c['length_m'] for c in folds if not c['supported']) > sum(c['length_m'] for c in folds if c['supported'])
    plain = topology_cost(complex_parts, observations)
    assert 'support_penalty' not in plain, 'without support the cost is byte-identical to before'
    complex_cost = topology_cost(complex_parts, observations, support=complex_support)
    simple_cost = topology_cost(simple_parts, observations, support=simple_support)
    assert complex_cost['score'] == plain['score']
    assert complex_cost['support_penalty'] > simple_cost['support_penalty'] == 0
    assert complex_cost['supported_score'] > simple_cost['supported_score']
    assert complex_cost['support_method'] == TOPOLOGY_SUPPORT_VERSION


@pytest.mark.parametrize('angle', [0, 31])
@pytest.mark.parametrize('reverse', [False, True])
def test_long_rectangle_with_a_central_ridge_step_becomes_one_ridge_two_planes(angle, reverse):
    footprint = rotate(Polygon(NOTCHED[::-1] if reverse else NOTCHED), angle, origin=(0, 0))
    image = two_tone(footprint, np.asarray(rotate(LineString(RIDGE), angle, origin=(0, 0)).coords))
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    parts, report = select_roof_topology(prior, footprint, 3, 2, image, FRAME, prefer_simple=True)
    assert report['accepted'] and report['reason'] == 'simpler-image-supported-roof', report['support']
    assert report['method'] == TOPOLOGY_SUPPORT_VERSION
    assert report['support']['accepted'] and report['support']['minimal_candidates'] == 1
    assert report['selected_support']['minimal_support']
    planes = {tuple(round(p['plane'][key], 4) for key in ('a', 'b', 'c')) for p in parts}
    assert len(planes) == 2
    assert report['after']['crease_runs'] == 1 and report['after']['physical_planes'] == 2
    assert report['after']['unsupported_share'] == 0
    assert report['roof_type'] == 'gabled'
    geom, ridges = ridge_runs(parts)
    assert geom == 'LineString' and ridges
    audit = audit_roof(parts, footprint, 3)
    assert audit['passed'] and audit['drainage']['passed']
    assert unary_union([Polygon(p['coords']) for p in parts]).symmetric_difference(footprint).area < 1e-6
    # The stage that ran before is still recorded, byte for byte, as 'before'.
    assert report['before']['physical_planes'] > 2 and 'support_penalty' not in report['before']


def test_the_veto_keeps_the_complex_roof_when_its_creases_are_the_observed_lines(monkeypatch):
    footprint = Polygon(NOTCHED)
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
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
    monkeypatch.setattr(roof_topology, 'image_segments', lambda *args, **kwargs: observations)
    blank = Image.new('RGB', SIZE, (130, 130, 130))
    parts, report = select_roof_topology(prior, footprint, 3, 2, blank, FRAME, prefer_simple=True)
    support = report['support']
    assert support['minimal_candidates'] == 1 and support['candidates'], 'the single ridge was generated, then refused'
    minimal = next(c for c in support['candidates'] if c.get('minimal_support'))
    assert not minimal['gates']['residual'] or not minimal['gates']['supported_share']
    assert not support['accepted'] and report['reason'] != 'simpler-image-supported-roof'
    assert report['method'] == roof_topology.VERSION
    assert support['current']['unsupported_share'] == 0, 'every current crease is an observed line'
    assert len({tuple(round(p['plane'][k], 4) for k in ('a', 'b', 'c')) for p in parts}) > 2


def test_without_image_evidence_nothing_fires_and_the_current_topology_is_kept():
    footprint = Polygon(NOTCHED)
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    blank = Image.new('RGB', SIZE, (130, 130, 130))
    parts, report = select_roof_topology(prior, footprint, 3, 2, blank, FRAME, prefer_simple=True)
    support = report['support']
    assert support['method'] == TOPOLOGY_SUPPORT_VERSION and not support['accepted']
    assert support['reason'] == 'no-supported-simpler-candidate'
    assert report['method'] == roof_topology.VERSION and report['reason'] != 'simpler-image-supported-roof'
    assert 'support_penalty' not in report['after']
    assert len({tuple(round(p['plane'][k], 4) for k in ('a', 'b', 'c')) for p in parts}) > 2


def test_crease_contrast_is_measured_inside_the_roof_only():
    footprint = Polygon(NOTCHED)
    image = two_tone(footprint, RIDGE, background=250)
    ridge = crease_support(structural_prior(footprint.minimum_rotated_rectangle, 3, 5, 'gabled'), footprint, [], image, FRAME)
    assert ridge['per_crease'][0]['delta_l'] > 30
    # A crease 0.4 m inside the eave: its outer band is mostly ground (250) but only roof pixels count.
    eave_line = [{'coords': [[2, 0], [22, 0], [22, .4], [2, .4]], 'ring_z': [3, 3, 3.2, 3.2],
                  'plane': {'a': 0, 'b': .5, 'c': 3}},
                 {'coords': [[2, .4], [22, .4], [22, 4], [2, 4]], 'ring_z': [3.2, 3.2, 3.2, 3.2],
                  'plane': {'a': 0, 'b': 0, 'c': 3.2}}]
    measured = crease_support(eave_line, footprint, [], image, FRAME)
    assert measured['per_crease'][0]['delta_l'] is not None and measured['per_crease'][0]['delta_l'] < 6


@pytest.mark.parametrize('broken', ['lab_lightness', '_footprint_mask'])
def test_a_failing_support_measurement_never_breaks_the_v5_selection(monkeypatch, broken):
    """Review regression: the stage's own image preparation is inside the guard, so a cv2 or
    geometry failure records 'support-measurement-failed' and returns the v5 result unchanged."""
    import cv2

    def explode(*args, **kwargs):
        raise cv2.error('synthetic colour-conversion failure')
    footprint = Polygon(NOTCHED)
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    image = two_tone(footprint, RIDGE)
    expected, _ = select_roof_topology(prior, footprint, 3, 2, image, FRAME, prefer_simple=True)
    monkeypatch.setattr(roof_topology, broken, explode)
    parts, report = select_roof_topology(prior, footprint, 3, 2, image, FRAME, prefer_simple=True)
    assert report['support']['reason'] == 'support-measurement-failed'
    assert report['support']['error'].startswith('error:')
    assert report['method'] == roof_topology.VERSION and 'support_penalty' not in report['after']
    # On this image the healthy stage fires (one ridge, two planes); the guarded failure must
    # instead hand back the v5 skeleton with its valleys, exactly as if the stage did not exist.
    assert len(expected) == 2
    assert len({tuple(round(p['plane'][k], 4) for k in ('a', 'b', 'c')) for p in parts}) > 2
    assert report['after'] == report['before'] or report['reason'] == 'simpler-supported-roof'
