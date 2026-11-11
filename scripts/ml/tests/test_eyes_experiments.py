import json
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from scripts.ml import eyes_experiments as ex
from scripts.ml import eyes_index, scene_isolated
from scripts.ml.scene_confidence import geographic
from scripts.ml.scene_region import region_frame
from shapely.geometry import mapping

FRAME = region_frame([145.31, -37.7905, 145.311, -37.7895])
TILE = 'mooroolbark-50011498-17-118442-80417'
PROFILE = ex.PROFILE_PREFIX + 'f' * (64 - len(ex.PROFILE_PREFIX))


def square(x, y, size):
    return [[x, y], [x+size, y], [x+size, y+size], [x, y+size]]


def metres(points):
    return (np.c_[points, np.ones(len(points))] @ np.asarray(FRAME['pixel_to_meters'])).tolist()


def roof(oid, ring, score, ridge=None):
    obj = {'id': oid, 'kind': 'building', 'status': 'candidate', 'label': 'Roof', 'score': score, 'ring': ring, 'model': {}}
    if ridge:
        obj['roof_lines'] = [{'kind': 'ridge', 'line_meters': metres(ridge)}]
    return obj


def write_scene(root, sid, objects, signature, bands, *, sha='a'*64, owned=None, analysis=None):
    folder = Path(root)/'data/scenes'/sid
    folder.mkdir(parents=True, exist_ok=True)
    scene = {**FRAME, 'id': sid, 'title': 'T', 'image_sha256': sha, 'image_width': 512, 'image_height': 512,
             'objects': objects, 'model_signature': signature, 'analysis': analysis or {'pipeline': 'p', 'roof_model': 'r'}}
    if owned is not None:
        scene['owned_object_ids'] = owned
    (folder/'latest.json').write_text(json.dumps(scene))
    rows = [{'object_id': k, 'band': b, 'detector_score': s, 'observed_segments': n, 'image_line_mismatch_m': m}
            for k, (b, s, n, m) in bands.items()]
    (folder/'confidence.json').write_text(json.dumps({'version': 'roof-evidence-v1', 'model_signature': signature, 'objects': rows}))
    return scene


def wgs84_line(pixels):
    lon_lat = geographic({'ring': [*pixels, [0, 0]]}, FRAME).exterior.coords[:2]
    return [list(p) for p in lon_lat]


RIDGE = [[110, 130], [150, 130]]


def t2_reference(ref_id='crm-9', ring=None):
    return {'ref_id': ref_id, 'tier': 'T2', 'source_kind': 'crm', 'version': 'reference-registry-v1',
            'payload': {'geometry_wgs84': mapping(geographic({'ring': ring or square(100, 100, 60)}, FRAME)),
                        'edges_wgs84': [{'kind': 'ridge', 'line': wgs84_line(RIDGE)}]}}


def seed_registry(root, rows):
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'references', rows)


def stub_guard(root, *, write_baseline=True):
    assert write_baseline is False, 'experiments never record a guard baseline'
    return {'ok': True, 'version': 'eyes-guard-v1', 'checks': [{'name': 'stub', 'ok': True, 'detail': {}}]}


def confidence_index(root, scenes):
    """confidence.sqlite shaped like production: scenes -> {band: count} under the frozen profile."""
    path = Path(root)/'data/suburbs/mooroolbark/confidence.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE scenes(id TEXT PRIMARY KEY, signature TEXT, mtime INTEGER, bounds TEXT)')
        db.execute('CREATE TABLE roofs(scene_id TEXT, object_id TEXT, west REAL, south REAL, east REAL, north REAL, payload TEXT, PRIMARY KEY(scene_id,object_id))')
        for sid, bands in scenes.items():
            db.execute('INSERT INTO scenes VALUES(?,?,?,?)', (sid, 's', 1, '[]'))
            n = 0
            for band, count in bands.items():
                for _ in range(count):
                    n += 1
                    feature = {'type': 'Feature', 'geometry': None,
                               'properties': {'scene_id': sid, 'object_id': f'o{n}', 'band': band, 'profile': PROFILE}}
                    db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', (sid, f'o{n}', 0, 0, 1, 1, json.dumps(feature)))
            (Path(root)/'data/scenes'/sid).mkdir(parents=True, exist_ok=True)
            (Path(root)/'data/scenes'/sid/'latest.json').write_text('{}')


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, 'REPO', tmp_path)
    monkeypatch.setattr(ex.eyes_guard, 'check', stub_guard)
    (tmp_path/'data/scenes').mkdir(parents=True)
    return tmp_path


def fake_runner_factory(calls, *, reproduce=False):
    def runner(scene_id, out_root, **kwargs):
        calls.append((scene_id, Path(out_root), kwargs))
        base, cand = Path(out_root)/'baseline', Path(out_root)
        write_scene(base, scene_id, [roof('a', square(100, 100, 60), .9, RIDGE), roof('b', square(250, 100, 60), .7),
                                     roof('u', square(20, 400, 40), .9)], 'base-sig',
                    {'a': ('medium', .9, 4, .5), 'b': ('low', .7, 4, 1.2), 'u': ('high', .9, 5, .2)}, owned=['a', 'b'])
        objects = [roof('a', square(103, 100, 60), .92, [[113, 130], [153, 130]]), roof('c', square(400, 30, 40), .5),
                   roof('u', square(20, 400, 40), .9)]
        signature = 'base-sig' if reproduce else 'cand-sig'
        write_scene(cand, scene_id, objects, signature, {'a': ('high', .92, 5, .3), 'c': ('low', .5, 1, 1.5), 'u': ('high', .9, 5, .2)},
                    owned=['a', 'c'], analysis={'pipeline': 'p', 'roof_model': 'r-candidate'})
        return {'version': 'scene-isolated-v1', 'scene_id': scene_id, 'baseline_model_signature': 'base-sig',
                'candidate_model_signature': signature, 'signature_reproduced': reproduce,
                'variables': {'evidence_views': None if 'evidence_views' not in kwargs else
                              {'requested': len(kwargs['evidence_views']), 'applied': False},
                              'image_variant': None if kwargs.get('image_variant') is None else {'shape': list(kwargs['image_variant'].shape)}},
                'warnings': [], 'elapsed': .1, 'assess_error': None}
    return runner


# --- plan -----------------------------------------------------------------------------------------

def test_plan_cluster_sample_is_hash_fixed_and_stratified(root):
    scenes = {}
    for i in range(12):
        scenes[f'mooroolbark-50011498-17-1000{i:02d}-80400'] = {'high': 1, 'medium': 5, 'low': 1}
    for i in range(12):
        scenes[f'mooroolbark-50011498-17-1001{i:02d}-80400'] = {'medium': 5, 'low': 4}
    for i in range(12):
        scenes[f'mooroolbark-50011498-17-1002{i:02d}-80400'] = {'medium': 9, 'low': 1}
    scenes[TILE] = {'medium': 10, 'low': 5}
    confidence_index(root, scenes)
    first = ex.plan('code_drift', [TILE], sample='cluster+10', root=root)
    second = ex.plan('code_drift', [TILE], sample='cluster+10', root=root)
    assert first == {**second, 'computed_at': first['computed_at']}
    assert first['version'] == ex.VERSION and len(first['code_sha256']) == 64
    assert first['scene_ids'][0] == TILE and len(first['scene_ids']) == 11 and len(set(first['scene_ids'])) == 11
    assert first['sample'] == {**first['sample'], 'mode': 'cluster+10', 'requested': 10, 'added': 10}
    assert set(first['sample']['strata']) == {'has_high', 'low_heavy', 'medium_heavy'}
    assert sum(first['sample']['strata'].values()) == 10 and min(first['sample']['strata'].values()) >= 1
    assert all(first['sample']['strata'][k] <= 4 for k in first['sample']['strata']), 'proportional allocation over equal strata'
    assert first['variable'] == ex.EXPERIMENTS['code_drift']['variable'] and first['mode'] == 'reinference'
    run_root = Path(first['roots']['out_root'])
    assert run_root == root/'data/reviews/eyes/experiments/code_drift'/first['run_id']
    assert first['roots']['baseline_root'] == str(run_root/'baseline') and first['roots']['candidate_root'] == str(run_root)
    assert not run_root.exists(), 'plan() is pure; the CLI writes spec.json'
    cluster = ex.plan('code_drift', [TILE], sample='cluster', root=root)
    assert cluster['scene_ids'] == [TILE] and cluster['sample']['added'] == 0 and cluster['run_id'] != first['run_id']
    other = ex.plan('band_v2', [TILE], root=root)
    assert other['mode'] == 'join' and other['run_id'] != cluster['run_id']
    with pytest.raises(KeyError):
        ex.plan('unknown', [TILE], root=root)
    with pytest.raises(ValueError):
        ex.plan('code_drift', ['../evil'], root=root)
    with pytest.raises(ValueError):
        ex.plan('code_drift', [TILE], sample='everything', root=root)


def test_sample_allocation_is_proportional_with_floor_one():
    assert ex.allocate({'a': 30, 'b': 10, 'c': 1}, 10) == {'a': 7, 'b': 2, 'c': 1}
    assert ex.allocate({'a': 3, 'b': 0}, 10) == {'a': 3, 'b': 0}
    assert ex.allocate({}, 10) == {}
    assert ex.band_mix_stratum({'high': 1, 'low': 9}) == 'has_high'
    assert ex.band_mix_stratum({'medium': 3, 'low': 1}) == 'low_heavy'
    assert ex.band_mix_stratum({'medium': 9, 'low': 1}) == 'medium_heavy'


# --- run (re-inference with an injected runner) ---------------------------------------------------

def test_run_reinference_with_injected_runner_writes_report_and_lineage(root):
    seed_registry(root, [t2_reference()])
    spec = ex.plan('code_drift', ['s1', 's2'], root=root)
    calls = []
    report = ex.run(spec, runner=fake_runner_factory(calls), root=root)
    out_root = Path(spec['roots']['out_root'])
    assert [c[0] for c in calls] == ['s1', 's2'] and all(c[1] == out_root and c[2] == {} for c in calls)
    assert report['version'] == ex.VERSION and len(report['code_sha256']) == 64 and report['computed_at']
    assert report['experiment_id'] == 'code_drift' and report['run_id'] == spec['run_id']
    assert json.loads((out_root/'report.json').read_text()) == report
    assert json.loads((out_root/'spec.json').read_text()) == spec
    population = report['population']
    assert population['roofs'] == {'paired': 2, 'gained': 2, 'lost': 2}
    assert population['scenes_compared'] == 2 and population['excluded_unowned'] == 2
    assert len(population['profiles']['before']) == 1 and population['profiles']['before'] != population['profiles']['after']
    assert [s['status'] for s in population['scenes']] == ['compared', 'compared']
    assert all(s['signature_reproduced'] is False for s in population['scenes'])
    metrics = report['metrics']
    assert metrics['band_transition_matrix'] == {'medium': {'high': 2}}
    assert set(metrics['median_deltas']) >= {'score_delta', 'mismatch_delta', 'segments_delta', 'iou_before_after', 'reference_iou_delta'}
    assert metrics['median_deltas']['score_delta']['n'] == 2 and metrics['median_deltas']['score_delta']['ci95']
    assert metrics['referenced']['n'] == 2 and len(metrics['referenced']['roofs']) == 2
    assert metrics['referenced']['roofs'][0]['ref_id'] == 'crm-9' and metrics['referenced']['roofs'][0]['tier'] == 'T2'
    assert metrics['line_pr']['n'] == 2 and metrics['line_pr']['recall_delta']['median'] == pytest.approx(-1)
    assert metrics['sign_counts']['reference_iou_delta']['worse'] == 2
    assert report['lineage']['id_stability_rate'] == pytest.approx(2/3) and report['lineage']['paired'] == 4
    assert report['lineage']['gained'] == 2 and report['lineage']['lost'] == 2 and report['lineage']['baseline_objects'] == 6
    assert report['registration'] == {'status': 'not_applicable', 'reason': 'code_drift changes no imagery input.'}
    assert report['protected_reference_checks'] == {'ok': True, 'version': 'eyes-guard-v1', 'checks': [{'name': 'stub', 'ok': True}]}
    assert any('n referenced roofs = 2: anecdotal' in line for line in report['cannot_claim'])
    assert any('profiles differ: candidate is another model generation' in line for line in report['cannot_claim'])
    assert any('agreement is not accuracy' in line for line in report['cannot_claim'])
    assert any('heights are class priors' in line for line in report['cannot_claim'])
    assert any('signature_reproduced' in line and '0 of 2' in line for line in report['claims'])
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = eyes_index.rows(db, 'lineage', experiment_id=report['lineage_experiment_id'])
    assert len(rows) == 8 and {r['scene_id'] for r in rows} == {'s1', 's2'}
    assert all(r['payload']['candidate_model_signature'] == 'cand-sig' for r in rows)
    assert sorted(p.name for p in (root/'data').iterdir()) == ['reviews', 'scenes'], 'writes stay under the root'
    assert not (root/'data/scenes/s1').exists(), 'the production scene folder is never created by the experiment'


def test_run_records_reproduced_signatures_and_null_deltas(root):
    spec = ex.plan('code_drift', ['s1'], root=root)
    report = ex.run(spec, runner=fake_runner_factory([], reproduce=True), root=root)
    assert report['population']['scenes'][0]['signature_reproduced'] is True
    assert any('signature_reproduced' in line and '1 of 1' in line for line in report['claims'])
    assert any('n referenced roofs = 0' in line for line in report['cannot_claim'])


def test_extra_views_without_a_stack_is_recorded_as_unavailable_not_run(root):
    spec = ex.plan('extra_views', ['s1'], root=root)
    calls = []
    report = ex.run(spec, runner=fake_runner_factory(calls), root=root, stack_loader=lambda root, sid: None)
    assert calls == [], 'no re-inference without the experiment variable'
    scene, = report['population']['scenes']
    assert scene['status'] == 'unavailable' and 'stack' in scene['reason']
    assert report['population']['roofs'] == {'paired': 0, 'gained': 0, 'lost': 0}
    assert report['registration'] == {'status': 'unavailable', 'scenes': {'s1': 'unavailable'}}
    assert any('s1' in line and 'unavailable' in line for line in report['cannot_claim'])
    assert (Path(spec['roots']['out_root'])/'report.json').exists()


def test_extra_views_and_median_pass_stack_inputs_to_the_runner(root):
    median = np.zeros((512, 512, 3), dtype=np.uint8)
    registration = {'accepted_dates': ['2025-11-06', '2023-12-20'], 'rejected_dates': ['2025-09-28'],
                    'holdout_residual_px': {'median': 1.7}, 'fused_fraction': .029}
    def loader(root_, sid):
        return {'views': ['stack-median-gray', 'edge-consensus'], 'median': median, 'registration': registration}
    calls = []
    report = ex.run(ex.plan('extra_views', ['s1'], root=root), runner=fake_runner_factory(calls), root=root, stack_loader=loader)
    assert calls[0][2] == {'evidence_views': ['stack-median-gray', 'edge-consensus']}
    assert report['registration'] == {'status': 'available', 'scenes': {'s1': registration}}
    assert report['population']['scenes'][0]['variable_applied'] is False
    assert any('evidence_views' in line and 'not applied' in line for line in report['cannot_claim'])
    calls.clear()
    report = ex.run(ex.plan('sam3_on_median', ['s1'], root=root), runner=fake_runner_factory(calls), root=root, stack_loader=loader)
    assert list(calls[0][2]) == ['image_variant'] and np.array_equal(calls[0][2]['image_variant'], median)
    assert report['population']['scenes'][0]['variable_applied'] is True
    assert report['registration']['status'] == 'available'


def stack_folder(root, scene_id, status, *, sha='a'*64, stack_id='0123456789abcdef',
                 computed_at='2026-09-26T00:00:00+00:00'):
    import hashlib

    from PIL import Image as PILImage
    folder = root/'data/roof-review-loop/multidate/stacks'/stack_id
    folder.mkdir(parents=True)
    capture = {'index': 0, 'acquisition_id': 'acq-1', 'date': '2025-11-06', 'sensor': 'Vantor', 'combined_fraction': .03,
               'registration': {'accepted': status == 'available', 'status': 'accepted', 'reason': 'ok', 'median_residual_px': 1.7,
                                'holdout_ratio': .9, 'coverage': .8, 'shift_px': 2.1, 'transform': [[1, 0, 0], [0, 1, 0]]}}
    report = {'version': 'scene-stack-v1', 'code_sha256': 'c'*64, 'computed_at': computed_at, 'stack_id': stack_id,
              'status': status, 'reason': 'ok' if status == 'available' else '0 accepted capture(s)', 'scene_id': scene_id,
              'source_image_sha256': sha, 'gates': {'min_accepted': 2}, 'discovery': {'status': 'ok', 'found': 3},
              'captures': [capture, {**capture, 'index': 1, 'acquisition_id': 'acq-2'}], 'n_accepted': 2, 'n_views': 3,
              'per_roof_local': [{'object_id': 'a', 'local_shift_norm_px': 1.2, 'within_budget': True}], 'stack': {'views': 3}, 'assets': {}}
    if status == 'available':
        gray = np.full((512, 512), 90, dtype=np.uint8)
        gray[100:180, 100:220] = 200
        consensus = np.zeros((512, 512), dtype=np.uint8)
        consensus[100, 100:220] = 3
        for name, array in (('stack-median.png', gray), ('edge-consensus.png', consensus)):
            PILImage.fromarray(array).save(folder/name)
            report['assets'][name] = {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}
    (folder/'report.json').write_text(json.dumps(report))
    return report


def test_default_stack_loader_reads_scene_stack_reports_without_network(root):
    pytest.importorskip('scripts.ml.scene_stack')
    write_scene(root, 's1', [roof('a', square(100, 100, 60), .9)], 'sig', {})
    assert ex.default_stack_loader(root, 's1') == {'error': 'no scene_stack report for this scene (run scene_stack --build first)'}
    stack_folder(root, 's1', 'not_available', stack_id='1111111111111111')
    unavailable = ex.default_stack_loader(root, 's1')
    assert 'not_available' in unavailable['error'] and unavailable['registration']['n_accepted'] == 2
    assert unavailable['registration']['captures'][0]['median_residual_px'] == 1.7 and 'transform' not in json.dumps(unavailable)
    stack_folder(root, 's1', 'available', stack_id='2222222222222222')
    stack_folder(root, 's1', 'available', sha='b'*64, stack_id='3333333333333333')  # another image: never this scene's stack
    inputs = ex.default_stack_loader(root, 's1')
    assert inputs['stack_id'] == '2222222222222222' and inputs['registration']['status'] == 'available'
    assert [v[0] for v in inputs['views']] == ['stack-median', 'edge-consensus'] and all(v[2] == '2222222222222222' for v in inputs['views'])
    assert inputs['views'][0][1].shape == (512, 512) and inputs['views'][0][1].dtype == np.uint8
    assert inputs['views'][1][1].max() == 255, 'edge consensus is rescaled to 0..255 by scene_stack.views'
    assert inputs['median'].shape == (512, 512, 3) and inputs['median'].dtype == np.uint8
    assert np.array_equal(inputs['median'][:, :, 0], inputs['median'][:, :, 2]) and inputs['median'][150, 150, 1] == 200
    calls = []
    report = ex.run(ex.plan('sam3_on_median', ['s1'], root=root), runner=fake_runner_factory(calls), root=root)
    assert np.array_equal(calls[0][2]['image_variant'], inputs['median'])
    assert report['registration']['status'] == 'available' and report['registration']['scenes']['s1']['stack_id'] == '2222222222222222'
    report = ex.run(ex.plan('extra_views', ['s2'], root=root), runner=fake_runner_factory([]), root=root)
    scene, = report['population']['scenes']
    assert scene['status'] == 'unavailable' and 'no scene_stack report' in scene['reason']


def test_stack_not_available_keeps_its_registration_facts(root):
    def loader(root_, sid):
        return {'error': 'stack 1111 not_available: 0 accepted capture(s)', 'registration': {'status': 'not_available', 'n_accepted': 0}}
    report = ex.run(ex.plan('extra_views', ['s1'], root=root), runner=fake_runner_factory([]), root=root, stack_loader=loader)
    assert report['registration'] == {'status': 'unavailable', 'scenes': {'s1': {'status': 'not_available', 'n_accepted': 0}}}
    assert '0 accepted' in report['population']['scenes'][0]['reason']


def test_busy_lock_and_runner_failures_are_recorded_per_scene(root):
    def runner(scene_id, out_root, **kwargs):
        if scene_id == 's1':
            raise scene_isolated.ExtractionBusy('busy')
        raise ValueError('Source imagery changed')
    report = ex.run(ex.plan('code_drift', ['s1', 's2'], root=root), runner=runner, root=root)
    assert [(s['scene_id'], s['status']) for s in report['population']['scenes']] == [('s1', 'busy'), ('s2', 'failed')]
    assert 'imagery changed' in report['population']['scenes'][1]['reason']
    assert report['population']['roofs'] == {'paired': 0, 'gained': 0, 'lost': 0}
    assert any('no scene was compared' in line for line in report['cannot_claim'])


def test_guard_drift_becomes_a_cannot_claim(root, monkeypatch):
    monkeypatch.setattr(ex.eyes_guard, 'check', lambda root, *, write_baseline=True: {
        'ok': False, 'version': 'eyes-guard-v1', 'checks': [{'name': 'frozen_run', 'ok': False, 'detail': {}}]})
    report = ex.run(ex.plan('code_drift', ['s1'], root=root), runner=fake_runner_factory([]), root=root)
    assert report['protected_reference_checks']['ok'] is False
    assert any('protected reference' in line and 'frozen_run' in line for line in report['cannot_claim'])


# --- claims from denominators ---------------------------------------------------------------------

def bootstrap(n, median=None, ci=None):
    return {'n': n, 'median': median, 'ci95': ci, 'resamples': 2000, 'seed': 0, 'unit': 'roof'}


def test_statements_follow_denominators():
    compare = {'counts': {'paired': 30, 'gained': 1, 'lost': 0},
               'population': {'profiles': {'before': ['p'], 'after': ['q']}, 'excluded_unowned': 0},
               'bootstrap': {'reference_iou_delta': bootstrap(2, .01, [-.02, .03]), 'score_delta': bootstrap(30, .01, [.005, .02])},
               'claims': [], 'cannot_claim': []}
    scenes = [{'scene_id': 's', 'status': 'compared', 'signature_reproduced': False, 'variable_applied': True}]
    claims, cannot = ex.statements('code_drift', compare, scenes, {'n': 2, 'precision_delta': bootstrap(2), 'recall_delta': bootstrap(2)},
                                   {'ok': True, 'checks': []})
    assert any('n referenced roofs = 2: anecdotal' in line for line in cannot)
    assert any('profiles differ: candidate is another model generation' in line for line in cannot)
    assert any('agreement is not accuracy' in line for line in cannot)
    assert any('heights are class priors' in line for line in cannot)
    assert any('line precision/recall' in line and 'n = 2' in line for line in cannot)
    assert not any('median reference IoU' in line for line in claims)
    compare['bootstrap']['reference_iou_delta'] = bootstrap(25, .03, [.01, .05])
    compare['population']['profiles'] = {'before': ['p'], 'after': ['p']}
    claims, cannot = ex.statements('code_drift', compare, scenes, {'n': 25, 'precision_delta': bootstrap(25, .1, [.05, .2]),
                                                                    'recall_delta': bootstrap(25, 0, [-.1, .1])}, {'ok': True, 'checks': []})
    assert any('median reference IoU delta +0.03' in line and 'n = 25' in line for line in claims)
    assert any('line precision delta +0.1' in line for line in claims)
    assert any('line recall' in line and 'CI includes 0' in line for line in cannot)
    assert not any('profiles differ' in line for line in cannot)
    assert not any('anecdotal' in line for line in cannot)


# --- summarise an existing isolated run directory -------------------------------------------------

def isolated_run_dir(root, *, reproduce=True):
    run_dir = root/'data/reviews/eyes/isolated/drift-test'
    base_objects = [roof('a', square(100, 100, 60), .9, RIDGE), roof('b', square(250, 100, 60), .7), roof('u', square(20, 400, 40), .9)]
    # Production: frozen bands; the run's baseline copy is re-assessed by current code (one band moved).
    production = write_scene(root, TILE, base_objects, 'prod-sig', {'a': ('medium', .9, 4, .5), 'b': ('low', .7, 4, 1.2)}, owned=['a', 'b'])
    write_scene(run_dir/'baseline', TILE, base_objects, 'prod-sig', {'a': ('medium', .9, 4, .5), 'b': ('medium', .7, 4, 1.2)}, owned=['a', 'b'])
    (run_dir/'baseline/data/scenes'/TILE/'confidence.production.json').write_bytes((root/'data/scenes'/TILE/'confidence.json').read_bytes())
    signature = 'prod-sig' if reproduce else 'cand-sig'
    cand_objects = base_objects if reproduce else [roof('a', square(101, 100, 60), .9, RIDGE), roof('n', square(400, 30, 40), .6), roof('u', square(20, 400, 40), .9)]
    write_scene(run_dir, TILE, cand_objects, signature, {'a': ('medium', .9, 4, .5), 'b': ('medium', .7, 4, 1.2), 'n': ('low', .6, 1, 1.5)},
                owned=['a', 'b'] if reproduce else ['a', 'n'])
    record = {'version': 'scene-isolated-v1', 'scene_id': TILE, 'image_sha256': 'a'*64, 'baseline_model_signature': 'prod-sig',
              'candidate_model_signature': signature, 'signature_reproduced': reproduce,
              'baseline_root': str(run_dir/'baseline'), 'candidate_root': str(run_dir), 'variables': {'detector_only': True},
              'baseline_confidence': {'status': 'reassessed', 'production': {'compared': 2, 'band_changes': 1}},
              'warnings': [], 'elapsed': 1.5, 'computed_at': '2026-09-26T00:00:00+00:00'}
    (run_dir/'isolated-run.json').write_text(json.dumps(record))
    return run_dir, production


def test_summarise_isolated_on_a_synthetic_run_dir(root):
    seed_registry(root, [t2_reference()])
    run_dir, _ = isolated_run_dir(root, reproduce=False)
    report = ex.summarise_isolated(run_dir, root=root)
    assert json.loads((run_dir/'experiment-report.json').read_text()) == report
    assert report['version'] == ex.VERSION and report['experiment_id'] == 'code_drift' and report['scene_id'] == TILE
    headline = report['headline']
    assert headline['paired'] == 1 and headline['gained'] == 1 and headline['lost'] == 1
    assert headline['id_stability_rate'] == pytest.approx(2/3) and headline['signature_reproduced'] is False
    assert headline['band_transitions'] == {'medium': {'medium': 1}}
    assert headline['band_transitions_vs_production'] == {'medium': {'medium': 1}}
    assert report['isolated_run']['baseline_confidence']['production']['band_changes'] == 1
    assert report['production_compare']['note'].startswith('Bands on the production side')
    assert report['metrics']['referenced']['n'] == 1
    assert any('n referenced roofs = 1: anecdotal' in line for line in report['cannot_claim'])
    assert any('signature_reproduced' in line for line in report['claims'])
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = eyes_index.rows(db, 'lineage', experiment_id=report['lineage_experiment_id'])
    assert len(rows) == 4 and report['lineage_experiment_id'] == 'isolated-drift-test'
    assert not (root/'data/scenes'/TILE/'experiment-report.json').exists()


def test_summarise_isolated_reproduced_run_is_a_null_experiment(root):
    run_dir, _ = isolated_run_dir(root, reproduce=True)
    report = ex.summarise_isolated(run_dir, root=root)
    headline = report['headline']
    assert headline == {**headline, 'paired': 2, 'gained': 0, 'lost': 0, 'id_stability_rate': 1., 'signature_reproduced': True}
    assert headline['band_transitions'] == {'medium': {'medium': 2}}
    assert headline['band_transitions_vs_production'] == {'medium': {'medium': 1}, 'low': {'medium': 1}}
    assert any('assess-code drift' in line for line in report['cannot_claim'])
    with pytest.raises(FileNotFoundError):
        ex.summarise_isolated(root/'data/reviews/eyes/isolated/nothing', root=root)
    with pytest.raises(ValueError):
        ex.summarise_isolated(root/'data/scenes'/TILE, root=root)


# --- band_v2 and split_policy_core joins (no re-inference) ----------------------------------------

def shadow_row(sid, oid, v1, v2, scope='owned_object_ids', signature='sig'):
    return {'scene_id': sid, 'object_id': oid, 'model_signature': signature, 'version': 'roof-evidence-v2-shadow',
            'payload': {'band_v1': v1, 'band_v2': v2, 'flip': v1 != v2, 'transition': f'{v1}->{v2}', 'drainage_status': 'passed',
                        'population_scope': scope, 'mismatch_saturated': False, 'observed_segments': 4}}


def agreement_row(ref_id, sid, oid, iou, signature='sig', version='reference-agreement-v1'):
    return {'ref_id': ref_id, 'scene_id': sid, 'object_id': oid, 'model_signature': signature, 'version': version,
            'payload': {'iou': iou, 'hausdorff_m': 1.}}


def test_band_v2_report_joins_shadow_rows_to_human_references(root):
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'shadow_bands', [shadow_row('s1', 'o1', 'low', 'low'), shadow_row('s1', 'o2', 'low', 'medium'),
                                                     shadow_row('s1', 'o3', 'medium', 'medium'), shadow_row('s2', 'o4', 'medium', 'high'),
                                                     shadow_row('h1', 'x1', 'medium', 'high', scope='all_live')])
        eyes_index.replace_rows(db, 'references', [{**t2_reference('r1'), 'scene_id': None}, {**t2_reference('r3'), 'tier': 'T3'},
                                                   {**t2_reference('r4'), 'tier': 'T1'}])
        eyes_index.replace_rows(db, 'agreement', [agreement_row('r1', 's1', 'o2', .9), agreement_row('r1', 's1', 'o1', .4),
                                                  agreement_row('r3', 's1', 'o3', .95), agreement_row('r4', 's2', 'o4', .7),
                                                  agreement_row('r4', 's2', 'o4', .95, signature='other', version='stale'),
                                                  agreement_row('r1', 's1', 'o1', .99, signature='other', version='stale')])
    report = ex.band_v2_report(root)
    assert report['version'] == ex.VERSION and report['experiment_id'] == 'band_v2' and len(report['code_sha256']) == 64
    matrix = report['metrics']['flip_matrix']
    assert matrix['counts'] == {'low->low': 1, 'low->medium': 1, 'medium->high': 1, 'medium->medium': 1}
    assert matrix['denominator'] == 4 and report['population']['excluded_non_owned_rows'] == 1
    assert [f['object_id'] for f in matrix['flips']] == ['o2', 'o4']
    assert report['population']['scenes'] == ['s1', 's2'] and report['population']['roofs'] == 4
    iou = report['metrics']['reference_iou']
    assert iou['flipped'] == {**iou['flipped'], 'n': 2, 'median': pytest.approx(.8), 'min': .7, 'max': .9}
    assert iou['non_flipped'] == {**iou['non_flipped'], 'n': 1, 'median': .4}
    assert iou['tiers'] == ['T1', 'T2'] and iou['rule'].startswith('best (max) IoU')
    assert iou['signature_mismatched'] == 0, 'rows with a same-signature agreement row are joined; stale rows are ignored'
    assert any('2 of 4 owned roofs flip' in line for line in report['claims'])
    assert any('n referenced flipped roofs = 2: anecdotal' in line for line in report['cannot_claim'])
    assert any('rule change, not accuracy' in line for line in report['cannot_claim'])
    assert report['protected_reference_checks']['ok'] is True
    scoped = ex.band_v2_report(root, scene_ids=['s1'])
    assert scoped['metrics']['flip_matrix']['denominator'] == 3 and scoped['population']['scenes'] == ['s1']
    empty = ex.band_v2_report(root, scene_ids=['nothing'])
    assert empty['metrics']['flip_matrix']['denominator'] == 0 and any('no shadow_bands rows' in line for line in empty['cannot_claim'])


def test_band_v2_report_without_an_index_is_unavailable(root):
    report = ex.band_v2_report(root)
    assert report['population']['roofs'] == 0 and report['status'] == 'unavailable'


def test_split_policy_report_uses_injected_counter(root):
    counted = {'version': 'eyes-eligibility-v1', 'computed_at': 'now', 'code_sha256': 'c'*64, 'profile_prefix': ex.PROFILE_PREFIX,
               'denominators': {'owned_roofs': 100, 'low_roofs': 20, 'scenes_with_low_roofs': 4, 'scenes_loaded': 4, 'scenes_unavailable': 0,
                                'indexed_roofs': 110, 'unreadable_index_rows': 0, 'indexed_scenes': 5},
               'frame': {'eligible': 2, 'reasons': {'Held-out, mixed-split or invalid image frame': 18}},
               'core': {'eligible': 13, 'reasons': {'Held-out core tile': 7}},
               'by_core_split': {'train': 13, 'validation': 4, 'test': 3, 'unresolved': 0}, 'held_out_core_roofs': 7}
    report = ex.split_policy_report(root, counter=lambda root_: counted)
    assert report['experiment_id'] == 'split_policy_core' and report['version'] == ex.VERSION
    metrics = report['metrics']
    assert metrics['eligible'] == {'frame': 2, 'core': 13} and metrics['eligible_delta'] == 11
    assert metrics['reach_rate'] == {'frame': .1, 'core': .65, 'denominator': 20, 'population': 'low-band owned scan roofs'}
    assert report['population']['low_roofs'] == 20 and report['population']['owned_roofs'] == 100
    assert any('13 of 20' in line and '2 of 20' in line for line in report['claims'])
    assert any('reach' in line and 'not accuracy' in line for line in report['cannot_claim'])
    assert any('7' in line and 'held-out' in line.lower() for line in report['cannot_claim'])
    assert report['eligibility'] == counted


def test_split_policy_report_without_an_index_is_unavailable(root):
    report = ex.split_policy_report(root)
    assert report['status'] == 'unavailable' and report['metrics'] == {} and any('unavailable' in line for line in report['cannot_claim'])


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_plan_writes_spec_and_run_refuses_inference_without_flag(root, monkeypatch, capsys):
    monkeypatch.setattr(scene_isolated, 'run', lambda *a, **k: pytest.fail('SAM3 re-inference must not start'))
    assert ex.main(['--root', str(root), '--plan', 'code_drift', '--scenes', 's1']) == 0
    spec = json.loads(capsys.readouterr().out)
    spec_path = Path(spec['roots']['out_root'])/'spec.json'
    assert json.loads(spec_path.read_text()) == spec
    assert ex.main(['--root', str(root), '--run', str(spec_path)]) == 2
    assert 'allow-inference' in capsys.readouterr().err
    assert not (Path(spec['roots']['out_root'])/'report.json').exists()


def test_cli_join_experiments_run_without_the_inference_flag(root, capsys):
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'shadow_bands', [shadow_row('s1', 'o1', 'low', 'low')])
    assert ex.main(['--root', str(root), '--band-v2']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['metrics']['flip_matrix']['denominator'] == 1
    assert Path(report['path']).is_relative_to(root/'data/reviews/eyes/experiments/band_v2')
    assert ex.main(['--root', str(root), '--plan', 'band_v2', '--scenes', 's1']) == 0
    spec = json.loads(capsys.readouterr().out)
    assert ex.main(['--root', str(root), '--run', str(Path(spec['roots']['out_root'])/'spec.json')]) == 0
    assert json.loads(capsys.readouterr().out)['experiment_id'] == 'band_v2'
    assert ex.main(['--root', str(root), '--split-policy']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'unavailable'


def test_cli_summarise_isolated(root, capsys):
    run_dir, _ = isolated_run_dir(root)
    assert ex.main(['--root', str(root), '--summarise-isolated', str(run_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['headline']['signature_reproduced'] is True and (run_dir/'experiment-report.json').exists()


REAL_INDEX = ex.REPO/'data/reviews/eyes/eyes.sqlite'


@pytest.mark.skipif(not REAL_INDEX.exists(), reason='real eyes index not present')
def test_real_band_v2_join_is_read_only(monkeypatch, tmp_path):
    """Read-only against the repo index: the Cambridge cluster rows exist; nothing is written.

    Other sidecar writers may be appending to the shared index while this runs, so the
    assertion is about this module's own outputs: no content-addressed report folder appears.
    """
    monkeypatch.setattr(ex.eyes_guard, 'check', stub_guard)
    own_path = ex._join_path(ex.REPO, 'band_v2', [TILE])
    existed = own_path.exists()
    report = ex.band_v2_report(ex.REPO, scene_ids=[TILE], write=False)
    assert report['status'] == 'ok' and report['metrics']['flip_matrix']['denominator'] >= 1 and 'path' not in report
    assert report['population']['scenes'] == [TILE]
    assert own_path.exists() == existed, 'write=False adds nothing under data/reviews/eyes/experiments'
    assert not (ex.REPO/'data/scenes'/TILE/'report.json').exists()


# --- reviewer regressions: production-write hazards, write=False, degenerate stack inputs ----------

def test_join_mode_refuses_an_out_root_under_data_scenes(root):
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'shadow_bands', [shadow_row('s1', 'o1', 'low', 'low')])
    for tampered in (root/'data/scenes/evil', root, Path('relative/out')):
        spec = ex.plan('band_v2', ['s1'], root=root)
        spec['roots']['out_root'] = str(tampered)
        with pytest.raises(ValueError):
            ex.run(spec, root=root)
    assert not (root/'data/scenes/evil').exists()
    assert not (root/'report.json').exists() and not (root/'spec.json').exists()
    assert sorted(p.name for p in (root/'data').iterdir()) == ['reviews', 'scenes']
    spec = ex.plan('code_drift', ['s1'], root=root)
    spec['roots']['out_root'] = str(root/'data/scenes/evil')
    with pytest.raises(ValueError):
        ex.run(spec, runner=fake_runner_factory([]), root=root)


def test_run_refuses_a_spec_planned_for_another_root(root, tmp_path_factory):
    other = tmp_path_factory.mktemp('other-root')
    (other/'data/scenes').mkdir(parents=True)
    spec = ex.plan('code_drift', ['s1'], root=other)
    with pytest.raises(ValueError, match='planned for root'):
        ex.run(spec, runner=fake_runner_factory([]), root=root)
    assert not eyes_index.path(root).exists() and not eyes_index.path(other).exists()


def test_write_false_never_creates_or_touches_the_eyes_index(root):
    spec = ex.plan('code_drift', ['s1'], root=root)
    report = ex.run(spec, runner=fake_runner_factory([]), root=root, write=False)
    assert report['lineage']['paired'] == 2 and report['lineage']['write_errors'] == {}
    assert not eyes_index.path(root).exists(), 'write=False: lineage stays in the report only'
    assert not (Path(spec['roots']['out_root'])/'report.json').exists()
    run_dir, _ = isolated_run_dir(root)
    report = ex.summarise_isolated(run_dir, root=root, write=False)
    assert report['headline']['paired'] == 2 and report['lineage']['paired'] == 3
    assert not eyes_index.path(root).exists() and not (run_dir/'experiment-report.json').exists()
    ex.summarise_isolated(run_dir, root=root)
    with closing(eyes_index.connection(root, readonly=True)) as db:
        assert len(eyes_index.rows(db, 'lineage', experiment_id='isolated-drift-test')) == 3


def test_locked_shared_index_is_recorded_per_scene_not_fatal(root, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(ex.scene_lineage, 'write', locked)
    spec = ex.plan('code_drift', ['s1'], root=root)
    report = ex.run(spec, runner=fake_runner_factory([]), root=root)
    scene, = report['population']['scenes']
    assert scene['status'] == 'compared' and 'database is locked' in scene['lineage_write_error']
    assert report['lineage']['write_errors'] == {'s1': scene['lineage_write_error']}
    assert report['lineage']['paired'] == 2, 'the rows are still in the report'
    assert any('lineage rows for scene s1 were not written' in line for line in report['cannot_claim'])
    assert (Path(spec['roots']['out_root'])/'report.json').exists()
    run_dir, _ = isolated_run_dir(root)
    report = ex.summarise_isolated(run_dir, root=root)
    assert 'database is locked' in report['population']['scenes'][0]['lineage_write_error']


def test_default_runner_receives_the_run_root(root, monkeypatch):
    seen = []
    def fake_isolated_run(scene_id, out_root, **kwargs):
        seen.append((scene_id, Path(out_root), kwargs))
        raise scene_isolated.ExtractionBusy('busy')
    monkeypatch.setattr(scene_isolated, 'run', fake_isolated_run)
    spec = ex.plan('code_drift', ['s1'], root=root)
    report = ex.run(spec, root=root)
    assert seen == [('s1', Path(spec['roots']['out_root']), {'repo': root})]
    assert report['population']['scenes'][0]['status'] == 'busy'


def test_band_v2_skips_malformed_rows_instead_of_crashing(root):
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'shadow_bands', [
            shadow_row('s1', 'o1', 'low', 'medium'),
            {'scene_id': 's1', 'object_id': 'bad', 'model_signature': 'sig', 'version': 'roof-evidence-v2-shadow', 'payload': 'not json {'},
            {'scene_id': 's1', 'object_id': 'bad2', 'model_signature': 'sig', 'version': 'roof-evidence-v2-shadow',
             'payload': {'population_scope': 'owned_object_ids'}}])
        eyes_index.replace_rows(db, 'references', [t2_reference('r1')])
        eyes_index.replace_rows(db, 'agreement', [agreement_row('r1', 's1', 'o1', .9),
                                                  {'ref_id': 'r1', 'scene_id': 's1', 'object_id': 'o1', 'model_signature': 'sig',
                                                   'version': 'broken', 'payload': 'garbage'}])
    report = ex.band_v2_report(root, write=False)
    assert report['status'] == 'ok' and report['metrics']['flip_matrix']['denominator'] == 1
    assert report['population']['unreadable_rows'] == 2 and report['population']['rows_read'] == 3
    assert report['metrics']['reference_iou']['flipped']['n'] == 1 and report['metrics']['reference_iou']['flipped']['median'] == .9
    assert any('2 shadow_bands row(s) had no readable band_v1/band_v2' in line for line in report['cannot_claim'])


def test_stack_loader_rejects_a_stack_on_another_grid(root):
    pytest.importorskip('scripts.ml.scene_stack')
    write_scene(root, 's1', [roof('a', square(100, 100, 60), .9)], 'sig', {})
    report = stack_folder(root, 's1', 'available', stack_id='2222222222222222')
    path = root/'data/roof-review-loop/multidate/stacks/2222222222222222/report.json'
    path.write_text(json.dumps({**report, 'image_width': 1024, 'image_height': 1024}))
    inputs = ex.default_stack_loader(root, 's1')
    assert 'views' not in inputs and 'grid 1024x1024 differs from the saved PNG 512x512' in inputs['error']
    assert inputs['registration']['stack_id'] == '2222222222222222'
    # A report claiming the right grid while its assets are on another one is caught on the arrays themselves.
    path.write_text(json.dumps(report))
    (root/'data/scenes/s1/latest.json').write_text(json.dumps({**json.loads((root/'data/scenes/s1/latest.json').read_text()),
                                                               'image_width': 600, 'image_height': 512}))
    inputs = ex.default_stack_loader(root, 's1')
    assert 'views' not in inputs and 'stack assets unreadable' in inputs['error'] and 'not the saved PNG grid 600x512' in inputs['error']
    calls = []
    result = ex.run(ex.plan('extra_views', ['s1'], root=root), runner=fake_runner_factory(calls), root=root, write=False)
    assert calls == [] and result['population']['scenes'][0]['status'] == 'unavailable'


def test_extra_views_with_an_empty_view_list_is_unavailable(root):
    calls = []
    loader = lambda root_, sid: {'views': [], 'median': None, 'registration': {'status': 'available', 'n_views': 1}}  # noqa: E731
    report = ex.run(ex.plan('extra_views', ['s1'], root=root), runner=fake_runner_factory(calls), root=root, stack_loader=loader, write=False)
    scene, = report['population']['scenes']
    assert calls == [] and scene['status'] == 'unavailable' and 'no views' in scene['reason']
    assert report['registration'] == {'status': 'unavailable', 'scenes': {'s1': {'status': 'available', 'n_views': 1}}}


def test_sam3_on_median_without_detector_substitution_is_not_applied(root):
    median = np.zeros((512, 512, 3), dtype=np.uint8)
    loader = lambda root_, sid: {'views': [], 'median': median, 'registration': {'status': 'available'}, 'stack_id': 'abcd',  # noqa: E731
                                 'median_source': 'gray median replicated'}
    base = fake_runner_factory([])
    def runner(scene_id, out_root, **kwargs):
        record = base(scene_id, out_root, **kwargs)
        record['variables']['detector_frame_substitutions'] = 0
        return record
    report = ex.run(ex.plan('sam3_on_median', ['s1'], root=root), runner=runner, root=root, stack_loader=loader, write=False)
    scene, = report['population']['scenes']
    assert scene['status'] == 'compared' and scene['variable_applied'] is False
    assert scene['stack_input'] == {'key': 'median', 'kwarg': 'image_variant', 'stack_id': 'abcd', 'shape': [512, 512, 3],
                                    'median_source': 'gray median replicated'}
    assert any('image_variant was never substituted' in line and 's1' in line for line in report['cannot_claim'])
    assert ex.variable_applied('sam3_on_median', {'variables': {'image_variant': {'shape': [1, 1, 3]}, 'detector_frame_substitutions': 1}})
    assert ex.variable_applied('sam3_on_median', {'variables': {'image_variant': {'shape': [1, 1, 3]}}}), 'older records without the counter'
    assert not ex.variable_applied('sam3_on_median', {'variables': {'image_variant': None}})


def test_extra_views_stack_input_records_view_names_not_arrays(root):
    views = [('stack-median', np.zeros((512, 512), np.uint8), 'abcd'), ('edge-consensus', np.zeros((512, 512), np.uint8), 'abcd')]
    loader = lambda root_, sid: {'views': views, 'median': None, 'registration': {'status': 'available'}, 'stack_id': 'abcd'}  # noqa: E731
    report = ex.run(ex.plan('extra_views', ['s1'], root=root), runner=fake_runner_factory([]), root=root, stack_loader=loader, write=False)
    scene, = report['population']['scenes']
    assert scene['stack_input'] == {'key': 'views', 'kwarg': 'evidence_views', 'stack_id': 'abcd',
                                    'views': [{'name': 'stack-median', 'shape': [512, 512]}, {'name': 'edge-consensus', 'shape': [512, 512]}]}
    json.dumps(report)  # arrays never reach the report


def test_stale_spec_code_is_recorded_as_a_cannot_claim(root):
    spec = ex.plan('code_drift', ['s1'], root=root)
    report = ex.run(spec, runner=fake_runner_factory([]), root=root, write=False)
    assert report['planned_code_sha256'] == ex.CODE_SHA256
    assert not any('planned under eyes_experiments code' in line for line in report['cannot_claim'])
    stale = {**spec, 'code_sha256': 'e' * 64}
    report = ex.run(stale, runner=fake_runner_factory([]), root=root, write=False)
    assert report['planned_code_sha256'] == 'e' * 64
    assert report['cannot_claim'][0].startswith('spec was planned under eyes_experiments code eeeeeeeeeeee')
    assert spec['run_id'] in report['cannot_claim'][0]


def pair_stack_folder(root, scene_id, *, sha='a'*64, stack_id='4444444444444444', computed_at='2026-09-26T01:00:00+00:00'):
    """scene_local_stack pair layout: status 'pair', one accepted date (aligned-0/valid-0), two-view edge consensus,
    valid-count and enhanced-pair-rgb assets, stack.views == 2 and NO stack-median."""
    import hashlib

    from PIL import Image as PILImage
    folder = root/'data/roof-review-loop/multidate/stacks'/stack_id
    folder.mkdir(parents=True)
    accepted = {'index': 0, 'acquisition_id': 'acq-1', 'date': '2025-11-06', 'sensor': 'Vantor', 'combined_fraction': .72,
                'status': 'accepted',
                'registration': {'accepted': True, 'method': 'local-phase-correlation-v1', 'status': 'accepted',
                                 'reason': '40/121 windows agree', 'median_residual_px': .8, 'holdout_residual_px': .8,
                                 'coverage': .72, 'shift_px': 6.7}}
    rejected = {**accepted, 'index': 1, 'acquisition_id': 'acq-2', 'date': '2023-12-20', 'sensor': 'Maxar',
                'combined_fraction': None, 'status': 'rejected',
                'registration': {**accepted['registration'], 'accepted': False, 'status': 'rejected',
                                 'reason': 'valid coverage 0.19 below 0.35', 'coverage': .19}}
    report = {'version': 'scene-local-stack-v1', 'code_sha256': 'd'*64, 'computed_at': computed_at, 'stack_id': stack_id,
              'status': 'pair', 'reason': '1 accepted capture; two-view pair', 'scene_id': scene_id, 'source_image_sha256': sha,
              'image_width': 512, 'image_height': 512, 'gates': {'min_accepted': 1}, 'discovery': {'status': 'ok', 'found': 3},
              'captures': [accepted, rejected], 'n_accepted': 1, 'n_views': 2,
              'per_roof_local': [{'object_id': 'a', 'capture': 'acq-1', 'local_shift_norm_px': .4, 'within_budget': True}],
              'stack': {'views': 2, 'mode': 'pair', 'combined_fraction': .5, 'edge_consensus_fraction_both': .01}, 'assets': {}}
    aligned = np.full((512, 512, 3), 90, np.uint8)
    aligned[100:180, 100:220] = 200
    valid = np.zeros((512, 512), np.uint8)
    valid[:, 256:] = 255
    consensus = np.zeros((512, 512), np.uint8)
    consensus[100, 100:220] = 1
    consensus[179, 300:400] = 2
    count = np.where(valid > 0, 2, 1).astype(np.uint8)
    for name, array in (('aligned-0.png', aligned), ('valid-0.png', valid), ('shift-field-0.png', np.zeros((512, 512, 3), np.uint8)),
                        ('edge-consensus.png', consensus), ('valid-count.png', count), ('enhanced-pair-rgb.png', aligned)):
        PILImage.fromarray(array).save(folder/name)
        report['assets'][name] = {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}
    (folder/'report.json').write_text(json.dumps(report))
    return report


def test_default_stack_loader_accepts_a_pair_report_with_views_but_no_median(root):
    pytest.importorskip('scripts.ml.scene_local_stack')
    write_scene(root, 's1', [roof('a', square(100, 100, 60), .9)], 'sig', {})
    stack_folder(root, 's1', 'not_available', stack_id='1111111111111111')          # an older honest negative
    pair_stack_folder(root, 's1')
    inputs = ex.default_stack_loader(root, 's1')
    assert inputs['stack_id'] == '4444444444444444' and inputs['mode'] == 'pair' and inputs['median'] is None
    assert [v[0] for v in inputs['views']] == ['edge-consensus', 'aligned-date-2025-11-06']
    assert all(v[2] == '4444444444444444' for v in inputs['views'])
    consensus, gray = inputs['views'][0][1], inputs['views'][1][1]
    assert consensus.dtype == np.uint8 and consensus[100, 150] == 128 and consensus[179, 350] == 255, 'scaled 0..255 by two views'
    assert gray.dtype == np.uint8 and gray.shape == (512, 512) and gray[150, 210] == 0 and gray[150, 300] == 90, 'gray masked to valid'
    assert inputs['median_source'] is None and 'needs >= 3 views' in inputs['median_unavailable']
    registration = inputs['registration']
    assert registration['status'] == 'pair' and registration['mode'] == 'pair' and registration['n_views'] == 2
    assert registration['captures'][0]['accepted'] is True and registration['captures'][1]['accepted'] is False
    assert registration['stack']['views'] == 2 and 'transform' not in json.dumps(registration)
    calls = []
    report = ex.run(ex.plan('extra_views', ['s1'], root=root), runner=fake_runner_factory(calls), root=root, write=False)
    assert [v[0] for v in calls[0][2]['evidence_views']] == ['edge-consensus', 'aligned-date-2025-11-06']
    scene, = report['population']['scenes']
    assert scene['status'] == 'compared'
    assert scene['stack_input'] == {'key': 'views', 'kwarg': 'evidence_views', 'stack_id': '4444444444444444', 'mode': 'pair',
                                    'views': [{'name': 'edge-consensus', 'shape': [512, 512]},
                                              {'name': 'aligned-date-2025-11-06', 'shape': [512, 512]}]}
    assert report['registration']['status'] == 'available' and report['registration']['scenes']['s1']['mode'] == 'pair'
    json.dumps(report)
    calls.clear()
    report = ex.run(ex.plan('sam3_on_median', ['s1'], root=root), runner=fake_runner_factory(calls), root=root, write=False)
    scene, = report['population']['scenes']
    assert calls == [] and scene['status'] == 'unavailable' and 'needs >= 3 views' in scene['reason']
    assert report['registration'] == {'status': 'unavailable', 'scenes': {'s1': registration}}
    # Ranking: an available (>= 3 views) stack outranks a newer pair; a pair outranks not_available.
    stack_folder(root, 's1', 'available', stack_id='2222222222222222')
    inputs = ex.default_stack_loader(root, 's1')
    assert inputs['stack_id'] == '2222222222222222' and 'mode' not in inputs and inputs['median'] is not None
    assert [v[0] for v in inputs['views']] == ['stack-median', 'edge-consensus']


def test_registration_summary_handles_pair_reports(root):
    report = pair_stack_folder(root, 's1')
    summary = ex.registration_summary(report)
    assert summary['status'] == 'pair' and summary['mode'] == 'pair' and summary['n_accepted'] == 1 and summary['n_views'] == 2
    assert summary['captures'][0]['median_residual_px'] == .8 and summary['captures'][0]['coverage'] == .72
    assert summary['per_roof_local'] == {'rows': 1, 'within_budget': 1, 'median_shift_px': .4}
    plain = ex.registration_summary({**report, 'status': 'available', 'stack': {'views': 3}})
    assert 'mode' not in plain


def test_stack_reports_rank_available_over_pair_over_the_rest_and_newest_within_a_rank(root):
    """The pre-pair rule (an available stack beats any other status, newest first within a status) is unchanged; a
    pair slots between them: it never outranks an available stack, however old, and it beats a not_available
    report however new (so a later honest negative from another code version does not hide the pair)."""
    import shutil
    pytest.importorskip('scripts.ml.scene_local_stack')
    write_scene(root, 's1', [roof('a', square(100, 100, 60), .9)], 'sig', {})
    stack_folder(root, 's1', 'available', stack_id='2222222222222222', computed_at='2026-09-26T00:00:00+00:00')
    stack_folder(root, 's1', 'not_available', stack_id='1111111111111111', computed_at='2026-09-26T02:00:00+00:00')
    _, reports = ex.stack_reports(root, 's1')
    assert [r['stack_id'] for r in reports] == ['2222222222222222', '1111111111111111'], 'available first, as before'
    inputs = ex.default_stack_loader(root, 's1')
    assert inputs['stack_id'] == '2222222222222222' and 'mode' not in inputs and inputs['median'] is not None
    pair_stack_folder(root, 's1', stack_id='4444444444444444', computed_at='2026-09-26T03:00:00+00:00')
    _, reports = ex.stack_reports(root, 's1')
    assert [r['stack_id'] for r in reports] == ['2222222222222222', '4444444444444444', '1111111111111111']
    assert ex.default_stack_loader(root, 's1')['stack_id'] == '2222222222222222', 'a newer pair never beats a full stack'
    shutil.rmtree(root/'data/roof-review-loop/multidate/stacks/2222222222222222')
    stack_folder(root, 's1', 'not_available', stack_id='5555555555555555', computed_at='2026-09-26T04:00:00+00:00')
    _, reports = ex.stack_reports(root, 's1')
    assert [r['stack_id'] for r in reports] == ['4444444444444444', '5555555555555555', '1111111111111111']
    inputs = ex.default_stack_loader(root, 's1')
    assert inputs['stack_id'] == '4444444444444444' and inputs['mode'] == 'pair' and inputs['median'] is None
    assert [v[0] for v in inputs['views']] == ['edge-consensus', 'aligned-date-2025-11-06']
    # Two pairs: the newer one wins within the rank.
    pair_stack_folder(root, 's1', stack_id='6666666666666666', computed_at='2026-09-26T05:00:00+00:00')
    _, reports = ex.stack_reports(root, 's1')
    assert [r['stack_id'] for r in reports][:2] == ['6666666666666666', '4444444444444444']
