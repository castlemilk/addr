import copy
import json

import pytest
from scripts.ml import roof_candidate_review as gates
from scripts.ml import roof_review_loop as loop
from scripts.ml.roof_observation_checks import narrow_connections
from scripts.ml.scene_training import atomic_json
from scripts.ml.tests.test_roof_review_loop import prepared
from scripts.ml.tests.test_scene_self_review import scene as scene
from shapely import affinity
from shapely.geometry import box
from shapely.ops import unary_union


def joined(width=.8):
    return unary_union([box(0, 0, 8, 8), box(11, 0, 17, 6), box(7, 3-width/2, 12, 3+width/2)])


def test_narrow_neck_finds_substantial_lobes_independent_of_orientation():
    assert narrow_connections(joined())['substantial_lobes'] == 2
    assert narrow_connections(affinity.rotate(joined(), 37))['substantial_lobes'] == 2


def test_neck_guard_preserves_legitimate_simple_concave_and_courtyard_roofs():
    assert narrow_connections(box(0, 0, 15, 10)) is None
    assert narrow_connections(joined(4)) is None
    assert narrow_connections(box(0, 0, 1, 30)) is None
    assert narrow_connections(box(0, 0, 20, 20).difference(box(5, 5, 15, 15))) is None
    assert narrow_connections(unary_union([box(0, 0, 8, 8), box(9, 3, 10, 4), box(7, 3, 9.5, 3.5)])) is None


def test_ambiguous_connection_defers_without_splitting_or_fitting(scene, monkeypatch):
    _, model, image, _ = scene
    obj = model['objects'][0]
    polygon = affinity.translate(joined(), 1, 1)
    obj['ring'] = [[x*10, y*10] for x,y in list(polygon.exterior.coords)[:-1]]
    original = copy.deepcopy(obj)
    monkeypatch.setattr(gates, 'strategies', lambda *args: (_ for _ in ()).throw(AssertionError('Must not fit merged hypotheses')))
    result, candidate = gates.improve(obj, model, image)
    assert result['status'] == 'needs_evidence' and candidate is None
    assert result['connection_check']['substantial_lobes'] == 2
    assert obj == original


def save_review(root, model, obj, signature=None):
    atomic_json(root/'data/scenes'/model['id']/'review.json', {
        'model_signature': signature or model['model_signature'], 'revision': 2,
        'objects': [obj], 'updated_at': '2026-09-25T00:00:00Z'})


def test_saving_an_unchanged_scene_does_not_count_as_roof_feedback(scene, monkeypatch):
    root, model, _, _ = prepared(scene, monkeypatch)
    obj = {**model['objects'][0], 'holes': [], 'review_issues': [], 'notes': ''}
    save_review(root, model, obj)
    assert loop.status(root)['rows'][0]['feedback']['state'] == 'awaiting_review'


def test_feedback_tracks_only_this_roof_and_does_not_approve_it(scene, monkeypatch):
    root, model, run, folder = prepared(scene, monkeypatch)
    source = (root/'data/scenes'/model['id']/'latest.json').read_bytes()
    obj = copy.deepcopy(model['objects'][0])
    obj['notes'] = 'These buildings are separate'
    save_review(root, model, obj)
    row = loop.status(root)['rows'][0]
    assert row['feedback']['state'] == 'feedback_saved'
    assert row['feedback']['changed_fields'] == ['notes']
    assert loop.detail(root, run, 0)['row']['feedback'] == row['feedback']
    assert not obj['geometry_reviewed']
    assert (root/'data/scenes'/model['id']/'latest.json').read_bytes() == source
    obj['id'] = 'another-roof'
    save_review(root, model, obj)
    assert loop.status(root)['rows'][0]['feedback']['state'] == 'awaiting_review'


def test_stale_feedback_and_changed_model_are_not_counted(scene, monkeypatch):
    root, model, _, _ = prepared(scene, monkeypatch)
    obj = copy.deepcopy(model['objects'][0])
    obj['notes'] = 'A correction'
    save_review(root, model, obj, 'stale')
    assert loop.status(root)['rows'][0]['feedback']['state'] == 'needs_recovery'
    path = root/'data/scenes'/model['id']/'latest.json'
    model['objects'][0]['ring'][0][0] += 1
    atomic_json(path, model)
    assert loop.status(root)['rows'][0]['feedback']['state'] == 'source_changed'


def test_reflection_notes_must_match_frozen_identity(scene, monkeypatch):
    root, _, _, folder = prepared(scene, monkeypatch)
    row = json.loads((folder/'report.json').read_text())['rows'][0]
    note = {**{k:row[k] for k in ('scene_id','object_id','source_model','input_sha256')},
            'title':'Check the connection','observed':'A narrow neck','question':'One roof or two?',
            'next_experiment':'Review segmentation','category':'data'}
    atomic_json(folder/'reflections.json', {'schema_version':1,'rows':[note]})
    assert loop.status(root)['rows'][0]['reflection']['question'] == 'One roof or two?'
    note['input_sha256'] = 'wrong'
    atomic_json(folder/'reflections.json', {'schema_version':1,'rows':[note]})
    assert 'reflection' not in loop.status(root)['rows'][0]
    (folder/'reflections.json').write_text('broken')
    assert loop.status(root)['rows'][0]['reflection_warning']


def test_paired_comparison_freezes_same_inputs_and_rejects_new_feedback(scene, monkeypatch):
    root, model, run, folder = prepared(scene, monkeypatch)
    report = json.loads((folder/'report.json').read_text())
    report['status'] = 'complete'
    atomic_json(folder/'report.json', report)
    with pytest.raises(ValueError, match='unchanged'):
        loop.prepare_comparison(root, run)
    monkeypatch.setattr(loop, 'fingerprint', lambda _: 'changed-reviewer')
    compared = loop.prepare_comparison(root, run)
    comparison = loop.run_path(root, compared)
    assert (comparison/'input-0.json').read_bytes() == (folder/'input-0.json').read_bytes()
    assert json.loads((comparison/'report.json').read_text())['comparison_run'] == run
    assert loop.prepare_comparison(root, run) == compared
    save_review(root, model, model['objects'][0])
    with pytest.raises(ValueError, match='Saved feedback'):
        loop.prepare_comparison(root, run)
