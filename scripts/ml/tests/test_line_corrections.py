"""line_corrections: reviewer roof-line edits vs the editor's seeded baseline, from synthetic scenes.

Every number asserted here is agreement between the model's starting estimate and the reviewer's lines, never
accuracy. Runs in the main ML venv; nothing here imports torch.
"""
import copy
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scripts.ml import eyes_index
from scripts.ml import line_corrections as lc
from scripts.ml.tests.test_review_edit_diffs import frame, square

SIZE = 400
RING = square(160, 160, 80)   # metres x, y in [-10, 10] through frame()

HIP_V = [[-5., 0.], [5., 0.], [-9., 4.], [9., 4.], [9., -4.], [-9., -4.]]
HIP_E = [(0, 1, 'ridge'), (2, 0, 'hip'), (5, 0, 'hip'), (3, 1, 'hip'), (4, 1, 'hip')]
FAN_V = [[0., 0.], [-8., 8.], [8., 8.], [8., -8.], [-8., -8.]]
FAN_E = [(0, 1, 'hip'), (0, 2, 'hip'), (0, 3, 'hip'), (0, 4, 'hip')]


def graph(vertices, edges):
    return {'vertices': [list(v) for v in vertices], 'edges': [{'a': a, 'b': b, 'kind': k} for a, b, k in edges]}


def annotation(g):
    return {'schema_version': 1, 'roof_type': 'hipped', 'vertices': [[x, y, 3.] for x, y in g['vertices']], 'faces': [],
            'edges': g['edges']}


def edits(baseline, current=None, checked=False, notes='', outline_ring=None, holes=None):
    current = baseline if current is None else current
    return {'schema_version': 1, 'outline': {'ring': outline_ring or RING, 'holes': holes or []}, 'baseline': baseline,
            'vertices': current['vertices'], 'edges': current['edges'], 'checked': checked, 'notes': notes}


def obj(oid, ring=RING, status='candidate', kind='building', **extra):
    return {'id': oid, 'kind': kind, 'label': kind.title(), 'ring': ring, 'status': status, 'geometry_reviewed': False,
            'source': 'sam3-text-onnx', 'score': .8, **extra}


def model_obj(oid, g, **extra):
    return obj(oid, roof_annotation=annotation(g), roof_lines=[{'line_meters': [g['vertices'][e['a']], g['vertices'][e['b']]],
                                                                 'kind': e['kind']} for e in g['edges']],
               roof_type='hipped', roof_geometry_source='image-guided-prior',
               roof_geometry_note='Roof form is uncertain in this image. 3 roof edges were jointly fitted to registered image lines.',
               **extra)


def write_scene(root, scene_id, objects, review_objects=None, history=None, revision=2, confidence=None):
    pixels = np.random.default_rng(1).integers(0, 255, (SIZE, SIZE, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format='PNG')
    data = buffer.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    scene = {'schema_version': 1, 'id': scene_id, **frame(), 'image_sha256': sha, 'objects': objects, 'groups': [],
             'analysis': {'pipeline': 'sam3-scene-roof-test'}}
    signature = hashlib.sha256(json.dumps(scene, sort_keys=True).encode()).hexdigest()
    scene['model_signature'] = signature
    folder = root / 'data/scenes' / scene_id
    (folder / 'models').mkdir(parents=True, exist_ok=True)
    (folder / f'{sha}.png').write_bytes(data)
    (folder / 'latest.json').write_text(json.dumps(scene))
    (folder / 'models' / f'{signature}.json').write_text(json.dumps(scene))
    if review_objects is not None:
        review = {'revision': revision, 'model_signature': signature, 'objects': review_objects,
                  'updated_at': f'2026-09-29T0{revision}:00:00Z', 'updated_by': 'local', 'history': history or []}
        (folder / 'review.json').write_text(json.dumps(review))
    if confidence is not None:
        (folder / 'confidence.json').write_text(json.dumps({'version': 'roof-evidence-v1', 'model_signature': signature,
                                                            'image_sha256': sha, 'objects': confidence}))
    return scene


HIP = graph(HIP_V, HIP_E)
FAN = graph(FAN_V, FAN_E)
FAN_TO_RIDGE = graph([*FAN_V, [0., -6.], [0., 6.]], [(5, 6, 'ridge')])       # 5 orphan vertices, one component
HIP_MINUS_ONE = graph(HIP_V, HIP_E[:-1])


def make_repo(root):
    model = [model_obj('same', HIP), model_obj('fan', FAN), model_obj('stale', HIP), model_obj('diffbase', HIP),
             model_obj('plain', HIP), obj('tree', square(20, 300), kind='tree')]
    seeded = [obj('same', roof_line_edits=edits(HIP)), obj('fan')]
    review = [obj('same', roof_line_edits=edits(HIP)),
              obj('fan', status='needs_adjustment', roof_line_edits=edits(FAN, FAN_TO_RIDGE, checked=True, notes='one ridge')),
              obj('stale', roof_line_edits=edits(HIP, outline_ring=square(20, 20))),
              obj('diffbase', roof_line_edits=edits(HIP_MINUS_ONE)),
              obj('plain'),
              obj('tree', square(20, 300), kind='tree')]
    history = [{'revision': 1, 'objects': seeded, 'updated_at': '2026-09-29T01:00:00Z', 'updated_by': 'local'}]
    confidence = [{'object_id': 'fan', 'band': 'medium',
                   'reasons': ['Many folds, short junctions or tiny planes warrant a simpler candidate comparison.', 'unknown text']}]
    write_scene(root, 'scene-a', model, review, history=history, confidence=confidence)
    write_scene(root, 'scene-noreview', [model_obj('x', HIP)])
    write_scene(root, 'qa_scratch', [model_obj('q', HIP)], [obj('q', roof_line_edits=edits(HIP))])
    return root


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted((root / 'data/scenes').rglob('*')) if p.is_file()}


def by_object(rows):
    return {(r['scene_id'], r['object_id']): r for r in rows}


def test_identical_graphs_agree_fully_and_a_fan_against_one_ridge_scores_zero(tmp_path):
    root = make_repo(tmp_path)
    before = snapshot(root)
    rows, skipped = lc.build(root)
    assert snapshot(root) == before, 'data/scenes must be untouched'
    found = by_object(rows)
    assert set(found) == {('scene-a', 'same'), ('scene-a', 'fan'), ('scene-a', 'stale'), ('scene-a', 'diffbase')}, 'qa_* and edit-less objects are not rows'
    same = found[('scene-a', 'same')]
    assert (same['line_precision'], same['line_recall'], same['line_kind_precision']) == (1., 1., 1.)
    assert (same['n_model_lines'], same['n_ref_edges'], same['count_ratio']) == (5, 5, 1.)
    assert same['distance_sweep_m']['0.35']['n_matched_model_lines'] == 5 and same['distance_sweep_m']['1.0']['line_recall'] == 1.
    assert same['ref_length_within_1m_share'] == 1. and same['model_length_within_1m_share'] == 1.
    assert same['by_kind_matched']['ridge'] == {'n_model': 1, 'n_model_matched': 1, 'n_ref': 1, 'n_ref_matched': 1}
    assert same['by_kind_matched']['hip']['n_model_matched'] == 4 and same['by_kind_matched']['valley']['n_ref'] == 0
    assert same['line_match_rule'] == {'distance_m': .5, 'angle_deg': 10., 'source': 'reference-agreement-v1'}
    assert same['pair'] == 'snapshot' and same['review_tier'] == 'T2' and same['review_provenance'] == 'review:roof_line_edits'
    assert same['pipeline'] == 'sam3-scene-roof-test' and same['roof_type'] == 'hipped' and same['snapshot_roof_lines_n'] == 5
    assert same['note_flags'] == {'form_uncertain': True, 'jointly_fitted_edges': 3}
    assert same['version'] == lc.VERSION and 'never accuracy' in same['note'] and len(same['code_sha256']) == 64

    fan = found[('scene-a', 'fan')]
    assert (fan['line_precision'], fan['line_recall'], fan['line_kind_precision']) == (0., 0., 0.)
    assert (fan['n_model_lines'], fan['n_ref_edges'], fan['count_ratio']) == (4, 1, 4.)
    assert fan['distance_sweep_m']['1.0'] == {'line_precision': 0., 'line_recall': 0., 'n_matched_model_lines': 0, 'n_matched_ref_edges': 0}
    assert fan['by_kind_matched']['hip'] == {'n_model': 4, 'n_model_matched': 0, 'n_ref': 0, 'n_ref_matched': 0}
    assert fan['by_kind_matched']['ridge'] == {'n_model': 0, 'n_model_matched': 0, 'n_ref': 1, 'n_ref_matched': 0}
    # The ridge crosses the fan centre: its middle lies within 1 m of the hips, its ends do not.
    assert 0. < fan['ref_length_within_1m_share'] < 1. and 0. < fan['model_length_within_1m_share'] < 1.
    assert fan['confidence_issue_codes'] == ['complex_folds'] and fan['confidence_band'] == 'medium'
    assert fan['confidence_signature_matches_review'] is True
    assert same['confidence_issue_codes'] is None and same['confidence_band'] is None


def test_topology_counts_orphans_components_degrees_and_short_lines():
    model = lc.graph_topology(FAN)
    assert model['n_edges'] == 4 and model['n_vertices_stored'] == 5 and model['n_vertices_referenced'] == 5
    assert model['n_orphan_vertices'] == 0 and model['degree_histogram'] == {'1': 4, '4': 1} and model['n_components'] == 1
    assert model['by_kind'] == {'ridge': 0, 'hip': 4, 'valley': 0, 'other': 0} and model['n_under_1_5m'] == 0
    assert abs(model['total_length_m'] - 4 * 8 * 2 ** .5) < 1e-3 and abs(model['median_length_m'] - 8 * 2 ** .5) < 1e-3

    ref = lc.graph_topology(FAN_TO_RIDGE)
    assert ref['n_edges'] == 1 and ref['n_vertices_stored'] == 7 and ref['n_vertices_referenced'] == 2
    assert ref['n_orphan_vertices'] == 5 and ref['degree_histogram'] == {'1': 2} and ref['n_components'] == 1
    assert ref['total_length_m'] == 12. and ref['min_length_m'] == 12. and ref['by_kind']['ridge'] == 1

    # Two separate segments, a duplicate node at 1e-6 m that merges, a short line and a malformed edge.
    g = graph([[0, 0], [4, 0], [4.0000005, 0.], [4, 1], [20, 20], [21, 20], [9, 9]],
              [(0, 1, 'ridge'), (2, 3, 'valley'), (4, 5, 'hip')])
    g['edges'].append({'a': 0, 'b': 99, 'kind': 'ridge'})
    g['edges'].append({'a': -1, 'b': 0, 'kind': 'ridge'})
    g['edges'].append('nonsense')
    topology = lc.graph_topology(g)
    assert topology['n_edges'] == 3 and topology['n_malformed_edges'] == 3
    assert topology['n_vertices_stored'] == 7 and topology['n_vertices_referenced'] == 6 and topology['n_orphan_vertices'] == 1
    assert topology['n_components'] == 2, 'coincident nodes within 1e-5 m merge; the far segment stays apart'
    assert topology['degree_histogram'] == {'1': 6} and topology['n_under_1_5m'] == 2
    assert topology['min_length_m'] == 1. and topology['by_kind'] == {'ridge': 1, 'hip': 1, 'valley': 1, 'other': 0}
    edges, malformed = lc.graph_edges(g)
    assert len(edges) == 3 and malformed == 3 and edges[0][0].tolist() == [[0., 0.], [4., 0.]] and edges[0][1] == 'ridge'
    assert lc.graph_topology({})['n_edges'] == 0 and lc.graph_topology({'vertices': [[0, 0]], 'edges': []})['n_orphan_vertices'] == 1


def test_coverage_share_is_a_length_share_at_a_loose_tolerance():
    ridge = [(np.array([[0., 0.], [10., 0.]]), 'ridge')]
    assert lc.coverage_share(ridge, ridge) == 1.
    assert lc.coverage_share(ridge, [(np.array([[0., 5.], [10., 5.]]), 'ridge')]) == 0.
    assert lc.coverage_share(ridge, []) == 0. and lc.coverage_share([], ridge) is None
    half = lc.coverage_share(ridge, [(np.array([[0., .5], [5., .5]]), 'ridge')])
    assert .45 <= half <= .6, half
    assert lc.coverage_share(ridge, [(np.array([[3., 0.], [7., 0.]]), 'ridge')], tol_m=.01, step_m=.01) < .45


def test_edited_at_revision_comes_from_history_and_status_flags_propagate(tmp_path):
    rows, _ = lc.build(make_repo(tmp_path))
    found = by_object(rows)
    same, fan, stale, diffbase = (found[('scene-a', k)] for k in ('same', 'fan', 'stale', 'diffbase'))
    assert (same['edited_at_revision'], same['edited_at'], same['edited_by']) == (1, '2026-09-29T01:00:00Z', 'local')
    assert (fan['edited_at_revision'], fan['edited_by']) == (2, 'local'), 'lines first saved in the current revision'
    assert all(r['review_revision'] == 2 and r['review_updated_by'] == 'local' for r in rows)
    assert same['ref_checked'] is False and fan['ref_checked'] is True
    assert (same['notes_len'], fan['notes_len']) == (0, 9)
    assert same['object_status'] == 'candidate' and fan['object_status'] == 'needs_adjustment'
    assert all(r['geometry_reviewed'] is False and r['authority_blocker'] is None for r in rows)
    assert same['outline_matches_object'] is True and stale['outline_matches_object'] is False
    assert all(r['outline_changed_since_model'] is False for r in rows)
    assert same['baseline_matches_snapshot'] is True and diffbase['baseline_matches_snapshot'] is False
    assert diffbase['line_precision'] == 1. and diffbase['n_model_lines'] == 4, 'metrics pair against the baseline, not the snapshot'
    assert same['topology_ref']['n_orphan_vertices'] == 0 and fan['topology_ref']['n_orphan_vertices'] == 5
    assert fan['topology_model']['degree_histogram'] == {'1': 4, '4': 1} and fan['topology_ref']['n_components'] == 1
    assert same['edits_sha256'] != fan['edits_sha256'] and same['baseline_sha256'] == stale['baseline_sha256']


def test_object_without_roof_line_edits_yields_no_row_and_a_reason(tmp_path):
    root = make_repo(tmp_path)
    rows, skipped = lc.build(root, objects=['scene-a:plain', 'scene-a:same'])
    assert [r['object_id'] for r in rows] == ['same']
    assert skipped['no_roof_line_edits'] == 1 and skipped['scenes_seen'] == 1
    rows, skipped = lc.build(root, scene_ids=['scene-noreview', 'scene-a'])
    assert skipped['review_unreadable'] == 1 and skipped['scenes_seen'] == 2 and len(rows) == 4
    assert lc.build(root, objects=['qa_scratch:q'])[0][0]['scene_id'] == 'qa_scratch', 'explicit ids may name a scratch scene'
    assert lc.parse_objects(['s:o', 's:p']) == {'s': {'o', 'p'}}
    try:
        lc.parse_objects(['no-colon'])
    except ValueError as exc:
        assert 'SCENE:OBJECT' in str(exc)
    else:
        raise AssertionError('malformed --objects must be refused')


def test_dry_run_prints_rows_and_writes_nothing(tmp_path, capsys):
    root = make_repo(tmp_path)
    before = snapshot(root)
    assert lc.main(['--root', str(root), '--dry-run', '--summary', '--objects', 'scene-a:fan']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] is True and out['n_rows'] == 1 and out['rows'][0]['object_id'] == 'fan'
    assert out['summary']['n_rows'] == 1 and out['summary']['by_ref_checked'] == {'true': 1}
    assert not eyes_index.path(root).exists() and not (root / 'data/reviews').exists()
    assert snapshot(root) == before


def test_build_is_idempotent_readable_and_keyed_by_review_revision(tmp_path, capsys):
    root = make_repo(tmp_path)
    for _ in range(2):
        assert lc.main(['--root', str(root), '--build', '--summary', '--scene-ids', 'scene-a']) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary['n_rows'] == 4 and summary['eyes_rows_written'] == 4
    out = Path(summary['out'])
    assert out.parent == root / lc.DEFAULT_OUT_ROOT and (out / 'rows.jsonl').exists() and (out / 'summary.json').exists()
    assert len((out / 'rows.jsonl').read_text().splitlines()) == 4 and not list(out.glob('*.tmp'))
    assert summary['medians']['line_precision'] == {'median': 1., 'n': 4} and summary['medians']['count_ratio']['median'] == 1.
    assert summary['by_pair'] == {'snapshot': 4} and summary['by_ref_checked'] == {'false': 3, 'true': 1}
    assert summary['n_baseline_matches_snapshot'] == 3 and summary['n_outline_matches_object'] == 3 and summary['n_count_ratio_gt_2'] == 1
    assert summary['skipped'] == {'no_roof_line_edits': 1}
    with eyes_index.connection(root, readonly=True) as db:
        stored = eyes_index.rows(db, 'line_corrections', scene_id='scene-a', object_id='same')
    assert len(stored) == 1 and stored[0]['review_revision'] == 2 and stored[0]['version'] == lc.VERSION
    assert stored[0]['payload']['line_recall'] == 1. and stored[0]['model_signature'] == stored[0]['payload']['model_signature']
    fetched = eyes_index.fetch_object(root, 'scene-a', 'fan')
    assert len(fetched['line_corrections']) == 1 and fetched['line_corrections'][0]['payload']['count_ratio'] == 4.

    # The reviewer saves again: a new revision with different lines is a second row; the old one stays.
    path = root / 'data/scenes/scene-a/review.json'
    review = json.loads(path.read_text())
    review['history'].append(copy.deepcopy({k: review[k] for k in ('revision', 'objects', 'updated_at', 'updated_by')}))
    review.update(revision=3, updated_at='2026-09-29T03:00:00Z')
    for o in review['objects']:
        if o['id'] == 'same':
            o['roof_line_edits'] = edits(HIP, HIP_MINUS_ONE)
    path.write_text(json.dumps(review))
    rows, _ = lc.build(root, objects=['scene-a:same'])
    assert rows[0]['review_revision'] == 3 and rows[0]['edited_at_revision'] == 3 and rows[0]['line_recall'] == 1. and rows[0]['line_precision'] == .8
    lc.write(root, rows, lc.summarize(rows))
    with eyes_index.connection(root, readonly=True) as db:
        stored = sorted(eyes_index.rows(db, 'line_corrections', scene_id='scene-a', object_id='same'), key=lambda r: r['review_revision'])
    assert [r['review_revision'] for r in stored] == [2, 3]
    assert [r['payload']['line_precision'] for r in stored] == [1., .8]
    latest = {(r['scene_id'], r['object_id']): r for r in lc.stored_rows(root)}
    assert latest[('scene-a', 'same')]['review_revision'] == 3 and len(latest) == 4
    assert lc.main(['--root', str(root), '--summary', '--objects', 'scene-a:same']) == 0
    stored_summary = json.loads(capsys.readouterr().out)
    assert stored_summary['source'] == 'eyes.sqlite' and stored_summary['n_rows'] == 1 and stored_summary['medians']['line_precision']['median'] == .8


def test_first_revision_with_lines_and_baseline_snapshot_helpers():
    current = obj('o', roof_line_edits=edits(HIP, FAN_TO_RIDGE))
    review = {'revision': 5, 'updated_at': 't5', 'updated_by': 'b',
              'history': [{'revision': 3, 'updated_at': 't3', 'updated_by': 'a', 'objects': [obj('o', roof_line_edits=edits(HIP, FAN_TO_RIDGE))]},
                          {'revision': 2, 'updated_at': 't2', 'updated_by': 'a', 'objects': [obj('o', roof_line_edits=edits(HIP))]},
                          {'revision': 4, 'objects': 'not a list'}],
              'objects': [current]}
    assert lc.first_revision_with_lines(review, current) == (3, 't3', 'a'), 'history is ordered by revision, not by position'
    assert lc.first_revision_with_lines({'revision': 1, 'objects': []}, obj('o')) == (None, None, None)
    assert lc.baseline_matches_snapshot(lc.graph_edges(HIP)[0], model_obj('m', HIP)) is True
    assert lc.baseline_matches_snapshot(lc.graph_edges(HIP_MINUS_ONE)[0], model_obj('m', HIP)) is False
    assert lc.baseline_matches_snapshot(lc.graph_edges(HIP)[0], obj('m')) is None, 'no roof_annotation graph: unknown, not False'
    with_eave = model_obj('m', HIP)
    with_eave['roof_annotation']['vertices'].append([0., 9., 3.])
    with_eave['roof_annotation']['edges'].append({'a': 2, 'b': 6, 'kind': 'eave'})
    assert lc.baseline_matches_snapshot(lc.graph_edges(HIP)[0], with_eave) is True, 'the editor seeds ridge/hip/valley only'
    assert lc.confidence_codes({'issues': [{'code': 'b'}, {'code': 'a'}, 'x']}) == ['a', 'b']
    assert lc.confidence_codes({'reasons': ['Some eaves are inferred behind tree canopy.']}) == ['hidden_eaves']
    assert lc.confidence_codes({'band': 'low'}) is None and lc.confidence_codes(None) is None
    assert lc.note_flags(None) == {'form_uncertain': False, 'jointly_fitted_edges': None}
    assert lc.note_flags('1 roof edge was jointly fitted to registered image lines.') == {'form_uncertain': False, 'jointly_fitted_edges': 1}
