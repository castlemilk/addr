import copy
import json
import subprocess

import numpy as np
import pytest
from PIL import ImageDraw
from scripts.ml import roof_candidate_review as gates
from scripts.ml import roof_review_loop as loop
from scripts.ml.roof_structure import _parts_from_faces
from scripts.ml.scene_training import atomic_json
from scripts.ml.tests.test_scene_self_review import scene as scene
from shapely.geometry import Polygon
from shapely.ops import unary_union


def metrics():
    before = {'geometry_passed': True, 'fixed_observations': 5, 'boundary_edge_distance_px': 1.,
              'cost': {'comparison_score': 4., 'construction_complexity': 2.,
                       'image_line_mismatch_m': .4, 'supported_fold_fraction': .4}}
    after = {**copy.deepcopy(before), 'retained_observation_fraction': 1., 'outline_iou': 1., 'outline_shift_m': 0.}
    after['cost'].update(comparison_score=2., construction_complexity=1., supported_fold_fraction=.65)
    return before, after


@pytest.mark.parametrize('field,value', [('geometry_passed', False), ('fixed_observations', 2),
                                      ('retained_observation_fraction', .9), ('outline_iou', .8),
                                      ('outline_shift_m', .7), ('boundary_edge_distance_px', 1.5)])
def test_a_better_score_cannot_override_physics_or_drop_evidence(field, value):
    before, after = metrics()
    assert gates.candidate_gate(before, after)[0]
    after[field] = value
    assert not gates.candidate_gate(before, after)[0]


@pytest.mark.parametrize('field,value', [('image_line_mismatch_m', .8), ('supported_fold_fraction', .1),
                                      ('construction_complexity', 3.), ('comparison_score', 3.9)])
def test_extra_folds_or_worse_image_fit_cannot_game_candidate_acceptance(field, value):
    before, after = metrics()
    after['cost'][field] = value
    assert not gates.candidate_gate(before, after)[0]


def test_tiny_overlap_with_different_heights_is_detected():
    first = [[0, 0], [4, 0], [4, 3], [0, 3]]
    second = [[3.99, 0], [8, 0], [8, 3], [3.99, 3]]
    parts = _parts_from_faces([(first, [3]*4), (second, [3.05]*4)], 3, 'flat')
    audit = gates.overlap_height(parts)
    assert audit['max_height_difference_m'] == pytest.approx(.05)
    assert audit['conflicting_area_m2'] == pytest.approx(.03)


def test_duplicate_image_segments_cannot_increase_fold_support(monkeypatch):
    monkeypatch.setattr(gates, 'shared_edges', lambda _: [[[0, 0], [10, 0]], [[0, 4], [10, 4]]])
    observation = {'points': np.array([[0., 0.], [5., 0.]]), 'tangent': np.array([1., 0.])}
    assert gates.fold_support([], [observation]) == gates.fold_support([], [observation]*10)
    assert gates.fold_support([], [observation])['supported_fold_fraction'] == pytest.approx(.25)


def test_blank_imagery_defers_without_fabricating_a_candidate(scene):
    _, model, image, _ = scene
    obj = model['objects'][0]
    original = copy.deepcopy(obj)
    result, candidate = gates.improve(obj, model, image)
    assert result['status'] == 'needs_evidence' and candidate is None
    assert obj == original


@pytest.mark.parametrize('protection', ['review', 'reference', 'test', 'edited', 'crm'])
def test_saved_feedback_and_held_out_inputs_cannot_enter_search(scene, monkeypatch, protection):
    root, model, _, _ = scene
    monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'verified'})
    obj = model['objects'][0]
    if protection == 'review':
        (root/'data/scenes'/model['id']/'review.json').write_text('{}')
    elif protection == 'reference':
        model['id'] = 'house-30286'
    elif protection == 'test':
        monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'cross_split'})
    elif protection == 'edited':
        obj['roof_line_edits'] = {'edges': []}
    else:
        obj['source'] = 'crm-roof-pipeline'
    assert loop.protected_reason(root, model, obj)


def prepared(scene, monkeypatch):
    from scripts.ml import scene_processing
    root, model, _, _ = scene
    monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'verified'})
    p = {'band': 'low', 'scene_id': model['id'], 'object_id': 'roof', 'model_signature': model['model_signature'],
         'key': 'test-house/roof', 'area_m2': 72, 'reasons': ['Many folds'], 'label': 'Test', 'capture_title': 'Test capture',
         'review_url': '/scenes?scene=test-house&object=roof'}
    monkeypatch.setattr(scene_processing, 'inventory', lambda _: {'features': [{'properties': p}]})
    run = loop.prepare(root, 12)
    return root, model, run, loop.run_path(root, run)


def test_unchanged_inputs_are_not_reprocessed_after_a_completed_attempt(scene, monkeypatch):
    root, _, run, folder = prepared(scene, monkeypatch)
    report = json.loads((folder/'report.json').read_text())
    report['rows'][0]['status'] = 'needs_evidence'
    report['status'] = 'complete'
    atomic_json(folder/'report.json', report)
    next_run = loop.prepare(root, 12)
    assert next_run != run
    assert loop.status(root)['total'] == 0


def test_selection_decodes_a_shared_capture_once_without_losing_holdout_protection(scene, monkeypatch):
    from scripts.ml import scene_processing
    root, model, _, _ = scene
    seed = model['objects'][0]
    model['objects'] = [{**copy.deepcopy(seed), 'id': f'roof-{i}'} for i in range(8)]
    path = root/'data/scenes'/model['id']/'latest.json'
    atomic_json(path, model)
    raw = path.read_bytes()
    reads = []
    decode = json.loads

    def counted(value, *args, **kwargs):
        if value == raw:
            reads.append(1)
        return decode(value, *args, **kwargs)

    props = [{'band': 'low', 'scene_id': model['id'], 'object_id': o['id'],
              'model_signature': model['model_signature'], 'key': o['id'],
              'area_m2': 72, 'reasons': [], 'label': 'Test', 'capture_title': 'Test',
              'review_url': '/scenes'} for o in model['objects']]
    monkeypatch.setattr(scene_processing, 'inventory', lambda _: {'features': [{'properties': p} for p in props]})
    monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'cross_split'})
    monkeypatch.setattr(loop.json, 'loads', counted)
    run = loop.prepare(root, 12)
    report = loop.status(root, run)
    assert len(reads) == 1
    assert report['total'] == 0
    assert report['selection_skips']['Held-out, mixed-split or invalid image frame'] == 8
    assert path.read_bytes() == raw


def test_source_change_rejects_old_candidate_without_writing_scene(scene, monkeypatch):
    root, model, _, folder = prepared(scene, monkeypatch)
    path = root/'data/scenes'/model['id']/'latest.json'
    changed = copy.deepcopy(model)
    changed['objects'][0]['ring'][0][0] += 2
    atomic_json(path, changed)
    original = path.read_bytes()
    result = loop.review_one(root, folder, 0)
    assert result['status'] == 'skipped'
    assert 'without a new model' in result['reason']
    assert path.read_bytes() == original


def test_timeout_is_visible_and_does_not_replace_a_model(scene, monkeypatch):
    root, model, run, folder = prepared(scene, monkeypatch)
    original = (root/'data/scenes'/model['id']/'latest.json').read_bytes()
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('candidate', 120)
    monkeypatch.setattr(loop.subprocess, 'run', timeout)
    loop.work(root, run)
    assert loop.status(root)['counts']['failed'] == 1
    assert (root/'data/scenes'/model['id']/'latest.json').read_bytes() == original
    assert not (root/'data/scenes'/model['id']/'review.json').exists()


def test_bad_run_id_cannot_escape_artifact_directory(tmp_path):
    with pytest.raises(ValueError):
        loop.run_path(tmp_path, '../../scenes')


def test_visible_ridge_selects_a_simpler_candidate_without_changing_the_source(scene):
    from scripts.ml.scene_roof import propose_roof
    _, model, image, _ = scene
    obj = model['objects'][0]
    roof = propose_roof([[2, 2], [14, 2], [14, 8], [2, 8]], 3, 2, 'hipped')
    obj.update(roof_annotation=roof['annotation'], roof_parts=roof['parts'], roof_lines=roof['lines'], roof_type=roof['roof_type'])
    original = copy.deepcopy(obj)
    # Three separated visible stretches of the same straight ridge.
    draw = ImageDraw.Draw(image)
    for start, end in [(20, 53), (63, 96), (106, 139)]:
        draw.line((start, 50, end, 50), fill=(240, 230, 220), width=2)
    result, candidate = gates.improve(obj, model, image)
    assert result['status'] == 'candidate_ready'
    assert result['after']['geometry_passed']
    assert result['after']['cost']['crease_runs'] < result['before']['cost']['crease_runs']
    assert result['after']['cost']['supported_fold_fraction'] > .7
    assert candidate['ring'] == obj['ring'] and not candidate['geometry_reviewed']
    assert obj == original


def test_broad_planes_keep_chamfered_outline_coverage(scene):
    _, model, image, _ = scene
    obj = model['objects'][0]
    obj['ring'] = [[20, 20], [135, 20], [140, 25], [140, 80], [20, 80]]
    proposal = gates.broad_plane_proposal(obj, model, image, 2, 'gabled')
    footprint = Polygon(gates.local_outline(obj, model)[0])
    surface = unary_union([Polygon(p['ring_meters']) for p in proposal['parts']])
    assert len(proposal['parts']) == 2
    assert footprint.symmetric_difference(surface).area < 1e-6


def test_clipped_roof_requests_more_context(scene):
    _, model, image, _ = scene
    obj = model['objects'][0]
    obj['ring'][0][0] = 0
    result, candidate = gates.improve(obj, model, image)
    assert result['status'] == 'needs_evidence'
    assert 'image edge' in result['reason'] and candidate is None


def test_image_inverse_must_match_before_candidate_search(scene, monkeypatch):
    root, model, _, _ = scene
    monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'verified'})
    model['meters_to_pixel'][2][0] += 5
    assert loop.protected_reason(root, model, model['objects'][0]) == 'Invalid image registration'


@pytest.mark.parametrize('change', ['input', 'image', 'code'])
def test_frozen_inputs_and_reviewer_identity_are_checked(scene, monkeypatch, change):
    root, model, _, folder = prepared(scene, monkeypatch)
    if change == 'input':
        (folder/'input-0.json').write_text('{}')
    elif change == 'image':
        (root/'data/scenes'/model['id']/f'{model["image_sha256"]}.png').write_bytes(b'changed')
    else:
        monkeypatch.setattr(loop, 'fingerprint', lambda _: 'new-code')
    result = loop.review_one(root, folder, 0)
    assert result['status'] == 'skipped'
    assert not (folder/'object-0.json').exists()


# --- Opt-in core split policy (object-centroid z17 tile) -----------------------------------

def owned_cross_split(scene, monkeypatch):
    """A frame straddling two splits whose single owned candidate sits in a train core tile."""
    root, model, _, _ = scene
    monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'cross_split'})
    model['owned_object_ids'] = [model['objects'][0]['id']]
    return root, model, model['objects'][0]


def test_default_policy_still_uses_the_frame_audit(scene, monkeypatch):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    assert loop.protected_reason(root, model, obj) == 'Held-out, mixed-split or invalid image frame'
    assert loop.protected_reason(root, model, obj, split_policy='frame') == 'Held-out, mixed-split or invalid image frame'


def test_core_policy_admits_an_owned_train_core_candidate_in_a_cross_split_frame(scene, monkeypatch):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    assert loop.object_core_group(obj, model) == 'z17:118442:80417'
    assert loop.group_split(loop.object_core_group(obj, model)) == 'train'
    assert loop.protected_reason(root, model, obj, split_policy='core') is None


@pytest.mark.parametrize('held_out', ['validation', 'test'])
def test_core_policy_rejects_held_out_core_tiles(scene, monkeypatch, held_out):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    monkeypatch.setattr(loop, 'group_split', lambda key: held_out)
    assert loop.protected_reason(root, model, obj, split_policy='core') == 'Held-out core tile'


def test_core_policy_rejects_objects_outside_the_owned_core(scene, monkeypatch):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    model['owned_object_ids'] = ['someone-else']
    assert loop.protected_reason(root, model, obj, split_policy='core') == 'Not owned by this tile core'


def test_core_policy_falls_back_to_the_frame_audit_without_ownership_data(scene, monkeypatch):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    del model['owned_object_ids']
    reason = loop.protected_reason(root, model, obj, split_policy='core')
    assert reason.startswith('Held-out, mixed-split or invalid image frame')
    assert 'owned_object_ids' in reason
    monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'verified'})
    assert loop.protected_reason(root, model, obj, split_policy='core') is None


@pytest.mark.parametrize('protection', ['review', 'reference', 'edited', 'crm', 'registration'])
def test_core_policy_keeps_every_other_protection(scene, monkeypatch, protection):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    if protection == 'review':
        (root/'data/scenes'/model['id']/'review.json').write_text('{}')
    elif protection == 'reference':
        model['id'] = 'house-30286'
    elif protection == 'edited':
        obj['roof_line_edits'] = {'edges': []}
    elif protection == 'crm':
        obj['source'] = 'crm-roof-pipeline'
    else:
        model['meters_to_pixel'][2][0] += 5
    frame = loop.protected_reason(root, model, obj)
    core = loop.protected_reason(root, model, obj, split_policy='core')
    assert core
    if protection != 'registration':
        assert core == frame  # Only the split step differs between the policies.
    else:
        assert core == 'Invalid image registration'


def test_unknown_split_policy_is_rejected(scene):
    root, model, _, _ = scene
    with pytest.raises(ValueError):
        loop.protected_reason(root, model, model['objects'][0], split_policy='tile')


def test_core_batch_is_recorded_and_reviewed_under_the_same_policy(scene, monkeypatch):
    from scripts.ml import scene_processing
    root, model, obj = owned_cross_split(scene, monkeypatch)
    atomic_json(root/'data/scenes'/model['id']/'latest.json', model)
    p = {'band': 'low', 'scene_id': model['id'], 'object_id': obj['id'], 'model_signature': model['model_signature'],
         'key': 'test-house/roof', 'area_m2': 72, 'reasons': [], 'label': 'Test', 'capture_title': 'Test capture',
         'review_url': '/scenes?scene=test-house&object=roof'}
    monkeypatch.setattr(scene_processing, 'inventory', lambda _: {'features': [{'properties': p}]})
    frame_run = loop.prepare(root, 12)
    frame_report = json.loads((loop.run_path(root, frame_run)/'report.json').read_text())
    assert frame_report['total'] == 0 and 'split_policy' not in frame_report
    assert frame_report['selection_skips'] == {'Held-out, mixed-split or invalid image frame': 1}
    core_run = loop.prepare(root, 12, split_policy='core')
    folder = loop.run_path(root, core_run)
    core_report = json.loads((folder/'report.json').read_text())
    assert core_run != frame_run
    assert core_report['split_policy'] == 'core' and core_report['total'] == 1
    assert core_report['fingerprint'] == frame_report['fingerprint']
    assert loop.status(root, core_run)['split_policy'] == 'core'
    with pytest.raises(ValueError):
        loop.check_input(root, folder, 0, core_report['rows'][0])
    _, checked, _ = loop.check_input(root, folder, 0, core_report['rows'][0], split_policy='core')
    assert checked['id'] == obj['id']

    class Reached(Exception):
        pass

    def reached(*args, **kwargs):
        raise Reached

    monkeypatch.setattr(gates, 'improve', reached)
    with pytest.raises(Reached):
        loop.review_one(root, folder, 0)  # The frozen row passed check_input under the recorded policy.


# --- Reviewer regressions: the core policy must not weaken any non-split protection --------

@pytest.mark.parametrize('field,value', [('lat', 95.), ('width', 40000)])
def test_core_policy_still_rejects_an_invalid_image_frame(scene, monkeypatch, field, value):
    root, model, _, _ = scene
    obj = model['objects'][0]
    model['owned_object_ids'] = [obj['id']]
    if field == 'lat':
        model['centroid']['lat'] = value
    else:
        model['image_width'] = value
    assert loop.image_split_audit(model, 'train')['status'] == 'invalid_frame'
    assert loop.protected_reason(root, model, obj) == loop.FRAME_REASON
    assert loop.protected_reason(root, model, obj, split_policy='core') == 'Invalid image registration'


@pytest.mark.parametrize('owned', [None, 'roof', {'roof': True}])
def test_core_policy_treats_a_missing_or_malformed_ownership_list_as_absent(scene, monkeypatch, owned):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    model['owned_object_ids'] = owned
    reason = loop.protected_reason(root, model, obj, split_policy='core')
    assert reason.startswith(loop.FRAME_REASON) and 'owned_object_ids' in reason
    monkeypatch.setattr(loop, 'image_split_audit', lambda *args: {'status': 'verified'})
    assert loop.protected_reason(root, model, obj, split_policy='core') is None


def test_core_policy_reports_unrepresentable_geometry_instead_of_raising(scene, monkeypatch):
    root, model, obj = owned_cross_split(scene, monkeypatch)
    obj['ring'] = [[float('nan'), 0], [1, 0], [1, 1]]
    assert loop.protected_reason(root, model, obj, split_policy='core') == 'Invalid image registration'
    anonymous = {k: v for k, v in model['objects'][0].items() if k != 'id'}
    assert loop.protected_reason(root, model, anonymous, split_policy='core') == 'Not owned by this tile core'


@pytest.mark.parametrize('flag', ['--resume', '--compare'])
def test_cli_split_policy_applies_to_new_batches_only(tmp_path, monkeypatch, flag):
    monkeypatch.setattr(loop.sys, 'argv', ['roof_review_loop', '--root', str(tmp_path), '--start', flag, 'a'*16, '--split-policy', 'core'])
    with pytest.raises(SystemExit) as failure:
        loop.main()
    assert failure.value.code == 2
    assert not (tmp_path/'data/roof-review-loop').exists()
