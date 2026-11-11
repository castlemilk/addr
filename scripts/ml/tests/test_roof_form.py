import json

import cv2
import numpy as np
from scripts.ml.roof_form import learned_choice, select_roof_ends
from scripts.ml.roof_objects import assign_objects
from scripts.ml.roof_structure import (
    audit_roof,
    gable_end_candidates,
    shared_edges,
    structural_prior,
)
from scripts.ml.train_roof_form import fit_logistic, metrics, train
from shapely.affinity import rotate
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union


class Frame:
    def metre_to_pixel(self, x, y):
        return x * 10 + 20, 120 - y * 10


def test_colour_step_and_continuous_ridge_favour_gable_but_uniform_colour_is_ambiguous():
    footprint = Polygon([(0, 0), (20, 0), (20, 8), (0, 8)])
    parts = structural_prior(footprint, 3, 6)
    lab = np.full((150, 250, 3), [150, 145, 145], dtype=np.uint8)
    lab[:80, :, 0] = 200
    image = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    gable, selection = select_roof_ends(parts, footprint, 3, image, Frame(), {})
    assert len(gable) == 2
    assert selection['gable_ends'] == 2
    assert all(d['image_choice'] == 'gable' for d in selection['ends'])
    unchanged, selection = select_roof_ends(parts, footprint, 3, np.full_like(image, 150), Frame(), {})
    assert len(unchanged) == 4
    assert selection['ambiguous_ends'] == 2
    reviewed, selection = select_roof_ends(parts, footprint, 3, np.full_like(image, 150), Frame(),
        {'boundary_source': 'crm', 'roof_form_source': 'reviewer', 'roof_type': 'gabled'})
    assert len(reviewed) == 2 and selection['source'] == 'reviewer'
    assert selection['ambiguous_ends'] == 2  # Override must not fabricate image evidence.


def test_complex_gable_preserves_outline_planes_seams_and_ridges_reach_the_end():
    polygon = Polygon([(0, 0), (16, 0), (16, 6), (6, 6), (6, 16), (0, 16)])
    for angle in (0, 31, 90):
        footprint = rotate(polygon, angle)
        parts = structural_prior(footprint, 3, 6, 'gabled', regularize_eaves=False)
        assert audit_roof(parts, footprint, 3)['passed']
        assert unary_union([Polygon(p['coords']) for p in parts]).symmetric_difference(footprint).area < 1e-6
        for edge in gable_end_candidates(footprint):
            end = LineString(edge)
            assert any(end.distance(LineString(line)) < 1e-5 for line in shared_edges(parts))
            assert any(end.distance(Polygon(p['coords']).centroid) < 10 for p in parts)
        # Two joined gables need four slopes, not the six faces of a hip roof.
        assert len(parts) == 4


def test_simple_gable_prior_does_not_override_hip_evidence_or_reviewer(monkeypatch):
    from scripts.ml import roof_form
    footprint = Polygon([(0, 0), (24, 0), (24, 7), (0, 7)])
    parts = structural_prior(footprint, 3, 5)
    evidence = [{'edge_meters': edge, 'hip_support': .8, 'gable_support': .1}
                for edge in gable_end_candidates(footprint)]
    monkeypatch.setattr(roof_form, 'roof_end_evidence', lambda *a, **kw: evidence)
    monkeypatch.setattr(roof_form, 'learned_choice', lambda *a: None)
    result, report = select_roof_ends(parts, footprint, 3, None, Frame(), {}, prefer_simple=True)
    assert result is parts
    assert all(d['selected'] == 'hip' and d['image_choice'] == 'hip' for d in report['ends'])
    for row in evidence:
        row.update(hip_support=0, gable_support=0)
    result, report = select_roof_ends(parts, footprint, 3, None, Frame(),
                                    {'boundary_source': 'crm', 'roof_form_source': 'reviewer', 'roof_type': 'hipped'},
                                    prefer_simple=True)
    assert result is parts and report['source'] == 'reviewer'


def test_square_roof_does_not_get_an_elongated_gable_prior():
    footprint = Polygon([(0, 0), (8, 0), (8, 8), (0, 8)])
    parts = structural_prior(footprint, 3, 5)
    result, report = select_roof_ends(parts, footprint, 3, np.full((150, 250, 3), 150, dtype=np.uint8),
                                    Frame(), {}, prefer_simple=True)
    assert result is parts and report['gable_ends'] == 0


def test_shallow_front_projection_can_be_a_gable_when_its_ridge_is_visible():
    footprint = Polygon([(0, 0), (8, 0), (8, -2), (14, -2), (14, 0), (24, 0), (24, 8), (0, 8)])
    front = LineString([(8, -2), (14, -2)])
    for angle in (0, 31, 90):
        for reverse in (False, True):
            ring = list(footprint.exterior.coords)[:-1]
            rotated = rotate(Polygon(ring[::-1] if reverse else ring), angle, origin=(0, 0))
            target = rotate(front, angle, origin=(0, 0))
            candidates = [LineString(edge) for edge in gable_end_candidates(rotated)]
            assert any(target.hausdorff_distance(edge) < 1e-6 for edge in candidates)
            assert len(candidates) == 3, 'Do not offer long side walls as roof ends'
    parts = structural_prior(footprint, 3, 6, regularize_eaves=False)
    lab = np.full((180, 300, 3), [150, 145, 145], dtype=np.uint8)
    lab[:, :130, 0] = 200  # Visible ridge at x=11, reaching the projecting front.
    gable, report = select_roof_ends(parts, footprint, 3, cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), Frame(), {})
    decision = next(d for d in report['ends'] if LineString(d['edge_meters']).equals(front))
    assert decision['image_choice'] == decision['selected'] == 'gable'
    assert audit_roof(gable, footprint, 3)['passed']
    assert any(front.distance(LineString(line)) < 1e-6 for line in shared_edges(gable))
    assert len(gable) == len(parts)-1
    # Geometry eligibility alone is not permission to assert a visible gable.
    _, blank = select_roof_ends(parts, footprint, 3, np.full((180, 300, 3), 150, dtype=np.uint8), Frame(), {}, prefer_simple=True)
    decision = next(d for d in blank['ends'] if LineString(d['edge_meters']).equals(front))
    assert decision['image_choice'] == 'ambiguous' and decision['selected'] == 'hip'


def test_solar_assignment_does_not_label_neighbours_or_duplicate_masks():
    roof = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    panel = Polygon([(2, 2), (4, 2), (4, 5), (2, 5)])
    neighbour = Polygon([(12, 2), (14, 2), (14, 5), (12, 5)])
    objects = assign_objects([('solar_panel', .9, panel), ('solar_panel', .8, panel), ('solar_panel', .95, neighbour)], roof, lambda x, y: (x, y))
    assert len(objects) == 1 and objects[0]['status'] == 'candidate'
    assert objects[0]['source'] == 'sam3-text-onnx'


def test_training_requires_independent_reviewed_roofs_in_both_classes(tmp_path):
    dataset = tmp_path / 'dataset.jsonl'
    dataset.write_text(json.dumps({'address_id': 1, 'split': 'train', 'reference': {'roof_family': 'gabled'},
        'prediction': {'roof_form_selection': {'ends': [{'hip_support': .1, 'gable_support': .8}] * 100}}}) + '\n')
    report = train(dataset, tmp_path / 'model')
    assert report['status'] == 'needs-reviewed-labels'
    assert report['reviewed_roofs']['train']['gabled'] == 1
    assert not (tmp_path / 'model/roof-form.json').exists()


def test_regularized_classifier_learns_from_labels_and_rejects_unvalidated_weights(tmp_path, monkeypatch):
    x = np.array([[.9, .1, -.8], [.1, .9, .8]] * 30)
    y = np.array([0, 1] * 30)
    weights = fit_logistic(x, y)
    assert metrics(x, y, weights)['balanced_accuracy'] == 1
    path = tmp_path / 'roof-form.json'
    path.write_text(json.dumps({'validated': False}))
    monkeypatch.setenv('ROOF_FORM_MODEL', str(path))
    assert learned_choice(.1, .9) is None
    path.write_text(json.dumps({'validated': True, 'feature_names': ['hip_support', 'gable_support', 'support_difference'],
                               'mean': [0, 0, 0], 'scale': [1, 1, 1], 'weights': weights.tolist()}))
    assert learned_choice(.1, .9) == 'gable'
    assert learned_choice(.9, .1) == 'hip'
    assert learned_choice(0, 0) is None


def test_review_set_keeps_units_and_image_tiles_together():
    from scripts.ml.build_review_set import build_review_set
    from scripts.ml.tiles import lonlat_to_tile_xy
    records = [{"address_id": i + 1, "centroid": {"lon": 145.3 + (i % 20) * .001, "lat": -37.8 + (i // 20) * .001},
                "roof_type": "gabled" if i % 3 else "hipped", "area_m2": 160, "ring_meters": [[0, 0], [20, 0], [20, 8], [0, 8]],
                "full_address": f"Roof {i}", "kind": "house"} for i in range(300)]
    records += [{**records[0], "address_id": 999}]
    cohort = build_review_set(records, count=250)
    assert len(cohort['entries']) == 250
    ids = [a for e in cohort['entries'] for a in e['shared_address_ids']]
    assert len(ids) == len(set(ids))
    by_id = {r['address_id']: r for r in records}
    tile_splits = {}
    for entry in cohort['entries']:
        center = by_id[entry['address_id']]['centroid']
        tile = tuple(map(int, lonlat_to_tile_xy(center['lon'], center['lat'], 18)))
        tile_splits.setdefault(tile, set()).add(entry['split'])
    assert all(len(splits) == 1 for splits in tile_splits.values())


def test_training_evaluates_spatial_splits_without_complex_roofs_dominating(tmp_path):
    rows = []
    for split, count in (("train", 10), ("validation", 3), ("test", 3)):
        for label in ("gabled", "hipped"):
            for i in range(count):
                gable = label == "gabled"
                rows.append({'address_id': len(rows) + 1, 'split': split, 'reference': {'roof_family': label},
                    'prediction': {'roof_form_selection': {'ends': [{'hip_support': .1 if gable else .9,
                        'gable_support': .9 if gable else .1}] * (10 if i == 0 else 1)}}})
    dataset = tmp_path / 'dataset.jsonl'
    dataset.write_text('\n'.join(json.dumps(row) for row in rows))
    report = train(dataset, tmp_path / 'model')
    assert report['status'] == 'trained-candidate'
    assert report['reviewed_roofs']['train'] == {'hipped': 10, 'gabled': 10}
    assert report['test']['balanced_accuracy'] == 1
    assert report['promotion_eligible']
    assert report['validation']['log_loss'] < report['validation_baseline']['log_loss']
    assert json.loads((tmp_path / 'model/roof-form.json').read_text())['validated']


def test_support_envelope_cannot_use_roof_lines_outside_visible_footprint():
    support = Polygon([(0, 0), (20, 0), (20, 8), (0, 8)])
    visible = Polygon([(5, 0), (15, 0), (15, 8), (5, 8)])
    parts = structural_prior(support, 3, 6)
    # Strong continuous gable evidence exists only at the hidden ends. The
    # observed middle roof is uniform. Hidden ends must remain ambiguous.
    lab = np.full((150, 250, 3), [150, 145, 145], dtype=np.uint8)
    lab[:80, :70, 0] = 200
    lab[:80, 170:, 0] = 200
    image = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    _, unmasked = select_roof_ends(parts, support, 3, image, Frame(), {})
    assert unmasked['gable_ends'] == 2
    unchanged, masked = select_roof_ends(parts, support, 3, image, Frame(), {}, evidence_footprint=visible)
    assert masked['gable_ends'] == 0 and masked['ambiguous_ends'] == 2
    assert len(unchanged) == len(parts)


def test_crop_continuation_cannot_create_a_hip_from_a_tiny_visible_end_sliver(monkeypatch):
    from scripts.ml import roof_form
    from shapely.geometry import box
    footprint = Polygon([(0, 0), (24, 0), (24, 8), (0, 8)])
    parts = structural_prior(footprint, 3, 5)
    bounds = box(2, -10, 40, 20)
    evidence = [{'edge_meters': edge, 'hip_support': .9, 'gable_support': 0}
                for edge in gable_end_candidates(footprint)]
    monkeypatch.setattr(roof_form, 'roof_end_evidence', lambda *a, **kw: evidence)
    monkeypatch.setattr(roof_form, 'learned_choice', lambda *a: None)
    selected, report = select_roof_ends(parts, footprint, 3, None, Frame(), {}, prefer_simple=True, image_bounds=bounds)
    assert len(selected) == 3
    hidden = next(d for d in report['ends'] if d['edge_meters'][0][0] == 0)
    assert hidden['reason'] == 'image-cut-continuation-prior'
    assert hidden['image_choice'] == 'ambiguous' and hidden['hip_support'] == .9
    visible = next(d for d in report['ends'] if d['edge_meters'][0][0] == 24)
    assert visible['selected'] == 'hip' and visible['image_choice'] == 'hip'
    reviewed, _ = select_roof_ends(parts, footprint, 3, None, Frame(),
        {'boundary_source': 'crm', 'roof_form_source': 'reviewer', 'roof_type': 'hipped'},
        prefer_simple=True, image_bounds=bounds)
    assert reviewed is parts


# --- flat-roof hypothesis helpers -----------------------------------------------------------------

def test_outline_directions_follow_wing_bearings_not_the_bounding_rectangle():
    from scripts.ml.roof_form import FLAT_RULES, _outline_directions
    l_shape = rotate(Polygon([(0, 0), (30, 0), (30, 10), (12, 10), (12, 24), (0, 24)]), 20, origin=(0, 0))
    directions, share = _outline_directions(l_shape, FLAT_RULES)
    assert len(directions) == 1 and abs(directions[0] - 20) < 1 and share == 1
    # A second wing at a different bearing adds a second direction; both must explain the perimeter.
    wing = rotate(Polygon([(30, 2), (48, 2), (48, 8), (30, 8)]), 40, origin=(30, 5))
    two_wings = unary_union([Polygon([(0, 0), (30, 0), (30, 10), (0, 10)]), wing])
    directions, share = _outline_directions(two_wings, FLAT_RULES)
    assert len(directions) == 2 and share >= .95
    assert any(abs(d - 0) < 3 or abs(d - 90) < 3 for d in directions) and any(abs(d - 40) < 3 for d in directions)
