import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml.annotation_check import check_annotation
from scripts.ml.scene_roof import propose_roof
from shapely.geometry import Polygon
from shapely.ops import unary_union

RECT = [[0, 0], [12, 0], [12, 8], [0, 8]]


@pytest.mark.parametrize('form', ['gabled', 'hipped', 'flat', 'auto'])
@pytest.mark.parametrize('reverse', [False, True])
def test_courtyard_stays_open_in_roof_faces_and_independent_audit(form, reverse):
    ring = [[0, 0], [20, 0], [20, 20], [0, 20]]
    hole = [[6, 6], [14, 6], [14, 14], [6, 14]]
    if reverse:
        ring, hole = ring[::-1], hole[::-1]
    result = propose_roof(ring, 3, 2, form, holes=[hole], image=Image.new('RGB', (512, 512), (130, 80, 60)),
                          meters_to_pixel=[[20, 0], [0, -20], [30, 450]])
    assert result['audit']['passed']
    assert check_annotation(result['annotation'], ring, 3, [hole])['passed']
    covered = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert covered.symmetric_difference(Polygon(ring, [hole])).area < 1e-6
    assert covered.intersection(Polygon(hole)).area < 1e-6
    # Filled courtyards cannot pass the independent saved-annotation audit.
    filled = propose_roof(ring, 3, 2, 'flat')
    assert not check_annotation(filled['annotation'], ring, 3, [hole])['passed']


@pytest.mark.parametrize(('form', 'faces'), [('gabled', 2), ('hipped', 4), ('flat', 1)])
def test_proposals_preserve_outline_and_pass_independent_geometry_audit(form, faces):
    result = propose_roof(RECT, 3, 2, form)
    assert result['audit']['passed']
    assert result['source'] == 'reviewer-template'
    assert len(result['annotation']['faces']) == faces
    covered = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert covered.symmetric_difference(Polygon(RECT)).area < 1e-6
    z = [p[2] for p in result['annotation']['vertices']]
    assert min(z) == 3 and max(z) == (3 if form == 'flat' else 5)
    if form == 'gabled':
        assert [e['kind'] for e in result['annotation']['edges']] == ['ridge']


def test_concave_gable_proposal_preserves_wings_and_height_frame():
    ring = [[0, 0], [14, 0], [14, 5], [5, 5], [5, 14], [0, 14]]
    result = propose_roof(ring, 2.7, 2, 'gabled')
    assert result['audit']['passed']
    coverage = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert coverage.symmetric_difference(Polygon(ring)).area < 1e-6


def test_uniform_colour_cannot_confirm_a_roof_form():
    result = propose_roof(RECT, 3, 2, 'auto', image=Image.new('RGB', (512, 512), (130, 80, 60)),
                          meters_to_pixel=[[20, 0], [0, -20], [100, 350]])
    assert result['audit']['passed']
    assert result['form_uncertain']
    assert result['source'] == 'image-guided-prior'
    assert 'uncertain' in result['note']


def test_registered_wing_ridge_stays_centred_and_keeps_outer_valley(monkeypatch):
    from scripts.ml import roof_line_fit, roof_topology
    from shapely.geometry import LineString, Point
    fixture = json.loads((Path(__file__).parent/'fixtures/parallel-wing-ridges.json').read_text())
    observations = [{k: np.array(v) if isinstance(v, list) else v for k, v in row.items()} for row in fixture['observations']]
    monkeypatch.setattr(roof_line_fit, 'image_segments', lambda *args: observations)
    monkeypatch.setattr(roof_topology, 'image_segments', lambda *args: observations)
    ring = fixture['ring_meters']
    before = json.dumps(ring)
    result = propose_roof(ring, 3.2, 2, 'auto', image=Image.new('RGB', (1024, 1024), (130, 120, 110)),
                          meters_to_pixel=[[7.876704, 0], [0, -7.876704], [512, 512]])
    assert result['audit']['passed'] and result['audit']['drainage']['passed']
    assert check_annotation(result['annotation'], ring, 3.2)['passed']
    assert json.dumps(ring) == before
    footprint = Polygon(ring)
    covered = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert covered.symmetric_difference(footprint).area < 1e-6
    # Right-hand wing: equal distances to its two long, visible parallel eaves.
    inner, outer = LineString([ring[12], ring[13]]), LineString([ring[9], ring[10]])
    ridges = [LineString(line['line_meters']) for line in result['lines'] if line['kind'] == 'ridge']
    wing = max((line for line in ridges if line.centroid.x > -12), key=lambda line: line.length)
    for t in (.25, .5, .75):
        point = wing.interpolate(t, normalized=True)
        assert abs(inner.distance(point)-outer.distance(point)) < .2
    direction = np.asarray(wing.coords[1])-wing.coords[0]
    eave = np.array(ring[13])-ring[12]
    cosine = abs(direction @ eave / np.linalg.norm(direction) / np.linalg.norm(eave))
    assert np.rad2deg(np.arccos(np.clip(cosine, -1, 1))) < .5
    # The junction also needs a valley towards the outside eave: it cannot
    # disappear just because its exit is not a concave footprint corner.
    valleys = [LineString(line['line_meters']) for line in result['lines'] if line['kind'] == 'valley']
    outside = LineString([ring[8], ring[9]])
    assert any(outside.distance(line) < .02 and line.length > 2 and
               min(Point(end).distance(wing) for end in line.coords) < .02 for line in valleys)


def test_long_uniform_roof_prefers_one_ridge_without_claiming_image_evidence():
    ring = [[0, 0], [24, 0], [24, 7], [0, 7]]
    result = propose_roof(ring, 3, 2, 'auto', image=Image.new('RGB', (512, 512), (130, 80, 60)),
                          meters_to_pixel=[[15, 0], [0, -15], [30, 350]])
    assert len(result['parts']) == 2
    assert [e['kind'] for e in result['annotation']['edges']] == ['ridge']
    assert result['form_uncertain']
    assert 'simplicity priors' in result['note']
    assert result['audit']['drainage']['passed']


def test_small_opening_is_cut_from_broad_planes_without_inventing_hips():
    ring = [[0, 0], [24, 0], [24, 7], [0, 7]]
    hole = [[6, 1], [7, 1], [7, 2], [6, 2]]
    result = propose_roof(ring, 3, 2, 'auto', holes=[hole], image=Image.new('RGB', (512, 512), (130, 80, 60)),
                          meters_to_pixel=[[15, 0], [0, -15], [30, 350]])
    assert result['topology']['accepted']
    assert len(result['parts']) <= 4
    assert all(e['kind'] == 'ridge' for e in result['annotation']['edges'])
    assert check_annotation(result['annotation'], ring, 3, [hole])['passed']
    coverage = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert coverage.symmetric_difference(Polygon(ring, [hole])).area < 1e-6


def test_image_cut_and_digitized_eaves_do_not_become_hundreds_of_folds():
    fixture = json.loads((Path(__file__).parent/'fixtures/clipped-stepped-roof.json').read_text())
    frame = np.asarray(fixture['pixel_to_meters'])
    def local(points):
        return (np.c_[points, np.ones(len(points))] @ frame).tolist()
    ring, holes = local(fixture['ring']), [local(h) for h in fixture['holes']]
    result = propose_roof(ring, 3.2, 2, 'auto', holes=holes,
                          image=Image.new('RGB', (fixture['image_width'], fixture['image_height']), (130, 80, 60)),
                          meters_to_pixel=fixture['meters_to_pixel'])
    assert result['topology']['accepted']
    assert len(result['parts']) < 15 and len(result['lines']) < 25
    # Wing hypotheses can continue a ridge through a junction as well as the
    # two free ends recognised by the earlier hip-to-gable conversion.
    if result['topology'].get('wing_refinement'):
        assert result['topology']['wing_refinement']['hip_ends'] == 0
        assert not any(line['kind'] == 'hip' for line in result['lines'])
    else:
        assert result['topology']['end_decisions']['gable_ends'] == 2
    assert result['form_uncertain']
    assert result['audit']['drainage']['passed']
    assert check_annotation(result['annotation'], ring, 3.2, holes)['passed']
    coverage = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert coverage.symmetric_difference(Polygon(ring, holes)).area < 1e-6


def test_pixel_stair_steps_do_not_each_create_a_roof_plane():
    rectangle = [[0, 0], [20, 0], [20, 10], [0, 10]]
    ring = []
    for i, a in enumerate(rectangle):
        a, b = np.array(a), np.array(rectangle[(i+1) % 4])
        normal = np.array([-(b-a)[1], (b-a)[0]]) / np.linalg.norm(b-a)
        for step in range(20):
            ring.append((a + (b-a)*step/20 + normal*(.1 if step % 2 else 0)).tolist())
    result = propose_roof(ring, 3, 2, 'hipped')
    assert len(result['parts']) < 10
    assert result['audit']['passed']
    assert 'saved outline is unchanged' in result['note']
    covered = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert covered.symmetric_difference(Polygon(ring)).area < 1e-6


@pytest.mark.parametrize(('ring', 'eave', 'rise', 'form'), [
    ([[0, 0], [10, 10], [0, 10], [10, 0]], 3, 2, 'gabled'),
    (RECT, 3, np.nan, 'hipped'), (RECT, 3, -2, 'gabled'),
    (RECT, 3, 2, 'unsupported'), (RECT, 99, 2, 'gabled'),
])
def test_invalid_input_never_produces_a_roof(ring, eave, rise, form):
    with pytest.raises(ValueError):
        propose_roof(ring, eave, rise, form)


def test_moderately_jagged_concave_roof_fits_broad_planes_without_filling_recess():
    # Pixel-scale noise on an irregular roof must not define one plane per edge.
    ring = [[3.28, 11.36], [2.08, 15.52], [0.0, 17.6], [0.0, 18.72], [0.64, 19.36], [1.12, 19.12], [1.44, 19.68], [4.64, 21.6], [4.96, 22.16], [5.52, 22.32], [5.92, 22.0], [7.28, 19.36], [7.68, 19.2], [7.84, 18.08], [9.12, 17.84], [11.84, 19.44], [15.04, 13.68], [15.68, 11.92], [15.84, 9.84], [16.96, 8.16], [17.6, 8.08], [18.88, 6.4], [18.48, 4.0], [15.68, 2.8], [14.0, 3.12], [11.04, 1.2], [10.56, 0.64], [10.72, -0.08], [10.16, 0.0], [6.0, 8.64], [4.56, 10.56]]
    result = propose_roof(ring, 3.2, 2, 'hipped')
    assert len(result['parts']) < len(ring) * .75
    assert result['audit']['passed']
    assert check_annotation(result['annotation'], ring, 3.2)['passed']
    covered = unary_union([Polygon(p['ring_meters']) for p in result['parts']])
    assert covered.symmetric_difference(Polygon(ring)).area < 1e-6
    assert result['audit']['max_planarity_error_m'] < 1e-6
    assert result['audit']['max_seam_error_m'] < 1e-6


# --- CP3 evidence views: default byte-identical, view origins recorded when used ----------------

def _strip_views(result):
    copy_ = json.loads(json.dumps(result, default=list))
    refinement = copy_.get('line_refinement') or {}
    for key in ('view_origins', 'extra_views', 'evidence_view_names'):
        refinement.pop(key, None)
    topology = copy_.get('topology') or {}
    topology.pop('evidence_views', None)
    (topology.get('line_refinement') or {}).pop('view_origins', None)
    (topology.get('line_refinement') or {}).pop('extra_views', None)
    return copy_


def test_propose_roof_threads_evidence_views_and_records_view_origins(monkeypatch):
    from scripts.ml import roof_line_fit, roof_topology
    calls = []
    original_fit, original_topo = roof_line_fit.image_segments, roof_topology.image_segments

    def spy(original):
        def wrapper(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)
        return wrapper
    monkeypatch.setattr(roof_line_fit, 'image_segments', spy(original_fit))
    monkeypatch.setattr(roof_topology, 'image_segments', spy(original_topo))
    image = Image.new('RGB', (512, 512), (130, 80, 60))
    frame = [[20, 0], [0, -20], [100, 350]]
    default = propose_roof(RECT, 3, 2, 'auto', image=image, meters_to_pixel=frame)
    assert calls and all(call == {} for call in calls), 'the default call never passes extra_views'
    assert 'view_origins' not in default['line_refinement']
    calls.clear()
    view = np.full((512, 512), 90, np.uint8)
    view[270:] = 200                       # a hard horizontal step through the roof, unsupported by the RGB
    views = [('stack-median', view)]
    result = propose_roof(RECT, 3, 2, 'auto', image=image, meters_to_pixel=frame, evidence_views=views)
    assert calls and all(call == {'extra_views': views} for call in calls)
    assert result['line_refinement']['view_origins'] == {}
    assert result['line_refinement']['evidence_view_names'] == ['stack-median']
    assert result['topology']['evidence_views']['names'] == ['stack-median']
    assert _strip_views(result) == _strip_views(default), 'ungated view lines leave the proposal unchanged'


def test_propose_roof_rejects_views_off_the_image_grid():
    image = Image.new('RGB', (512, 512), (130, 80, 60))
    with pytest.raises(ValueError, match='extra_views'):
        propose_roof(RECT, 3, 2, 'auto', image=image, meters_to_pixel=[[20, 0], [0, -20], [100, 350]],
                     evidence_views=[('stack-median', np.zeros((64, 64), np.uint8))])


# --- flat-roof hypothesis (roof-form-flat-v1): evidence recorded, template only when it fires ----

def test_auto_proposal_carries_versioned_flat_evidence_and_templates_do_not():
    from scripts.ml.roof_form import FLAT_VERSION
    result = propose_roof(RECT, 3, 2, 'auto', image=Image.new('RGB', (512, 512), (130, 80, 60)),
                          meters_to_pixel=[[20, 0], [0, -20], [100, 350]])
    evidence = result['form_evidence']
    assert evidence['version'] == FLAT_VERSION == 'roof-form-flat-v3'
    assert evidence['decision'] in ('pitched', 'uncertain'), 'a 96 m² dark uniform roof is below every flat gate'
    assert evidence['features']['area_m2'] == 96
    assert result['roof_type'] != 'flat'
    for form in ('gabled', 'hipped', 'flat'):
        assert propose_roof(RECT, 3, 2, form)['form_evidence'] is None
