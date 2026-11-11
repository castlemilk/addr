import json
from contextlib import closing

import numpy as np
import pytest
from scripts.ml import eyes_index, paired_compare
from scripts.ml.scene_confidence import geographic
from scripts.ml.scene_region import region_frame
from shapely.geometry import mapping

FRAME = region_frame([145.31, -37.7905, 145.311, -37.7895])


def square(x, y, size):
    return [[x, y], [x+size, y], [x+size, y+size], [x, y+size]]


def metres(points):
    return (np.c_[points, np.ones(len(points))] @ np.asarray(FRAME['pixel_to_meters'])).tolist()


def roof(oid, ring, score, ridge=None, raw=None):
    obj = {'id': oid, 'kind': 'building', 'status': 'candidate', 'label': 'Roof', 'score': score, 'ring': ring, 'model': {}}
    if ridge:
        obj['roof_lines'] = [{'kind': 'ridge', 'line_meters': metres(ridge)}]
    if raw:
        obj['boundary_refinement'] = {'raw_ring': raw}
    return obj


def write_scene(root, sid, objects, signature, bands, *, sha='a'*64, owned=None, confidence_signature=None):
    folder = root/'data/scenes'/sid
    folder.mkdir(parents=True, exist_ok=True)
    scene = {**FRAME, 'id': sid, 'title': 'T', 'image_sha256': sha, 'image_width': 512, 'image_height': 512,
             'objects': objects, 'model_signature': signature}
    if owned is not None:
        scene['owned_object_ids'] = owned
    (folder/'latest.json').write_text(json.dumps(scene))
    rows = [{'object_id': k, 'band': b, 'detector_score': s, 'observed_segments': n, 'image_line_mismatch_m': m}
            for k, (b, s, n, m) in bands.items()]
    (folder/'confidence.json').write_text(json.dumps({'version': 'roof-evidence-v1', 'model_signature': confidence_signature or signature,
                                                      'objects': rows}))
    return scene


def wgs84_line(pixels):
    lon_lat = geographic({'ring': [*pixels, [0, 0]]}, FRAME).exterior.coords[:2]
    return [list(p) for p in lon_lat]


@pytest.fixture
def roots(tmp_path):
    base, cand = tmp_path/'baseline', tmp_path/'candidate'
    ridge = [[110, 130], [150, 130]]
    tree = {'id': 't', 'kind': 'tree', 'status': 'candidate', 'ring': square(10, 10, 20)}
    unowned = roof('e', square(20, 400, 50), .9)
    write_scene(base, 's1', [roof('a', square(100, 100, 60), .9, ridge), roof('b', square(250, 100, 60), .7, raw=square(250, 100, 60)),
                             roof('c', square(400, 300, 40), .6), tree, unowned], 'base-sig',
                {'a': ('medium', .9, 4, .5), 'b': ('low', .7, 2, 1.5), 'c': ('low', .6, 0, None), 'e': ('high', .9, 5, .2)},
                owned=['a', 'b', 'c', 't'])
    write_scene(cand, 's1', [roof('a', square(103, 100, 60), .92, [[113, 130], [153, 130]]),
                             roof('b2', square(250, 100, 60), .75, raw=square(251, 100, 60)),
                             roof('d', square(400, 30, 40), .5), tree, unowned], 'cand-sig',
                {'a': ('high', .92, 5, .3), 'b2': ('medium', .75, 4, .4), 'd': ('low', .5, 1, 1.5), 'e': ('high', .9, 5, .2)},
                owned=['a', 'b2', 'd', 't'])
    # Different input bytes: never paired.
    write_scene(base, 's2', [roof('x', square(100, 100, 60), .9)], 'b2', {'x': ('low', .9, 0, None)})
    write_scene(cand, 's2', [roof('x', square(100, 100, 60), .9)], 'c2', {'x': ('high', .9, 5, .1)}, sha='b'*64)
    reference = {'ref_id': 'crm-9', 'tier': 'T2', 'source_kind': 'crm-roof',
                 'payload': {'geometry_wgs84': mapping(geographic({'ring': square(100, 100, 60)}, FRAME)),
                             'edges_wgs84': [{'kind': 'ridge', 'line': wgs84_line(ridge)}]}}
    agent = {'ref_id': 'crm-23', 'tier': 'T3', 'source_kind': 'agent-imagery-trace',
             'payload': {'geometry_wgs84': mapping(geographic({'ring': square(250, 100, 60)}, FRAME))}}
    far = {'ref_id': 'r-far', 'tier': 'T1', 'source_kind': 'review',
           'payload': {'geometry_wgs84': mapping(geographic({'ring': square(470, 470, 30)}, FRAME))}}
    return base, cand, [reference, agent, far]


def test_paired_roofs_bands_deltas_and_references(roots):
    base, cand, registry = roots
    report = paired_compare.compare(base, cand, ['s1', 's2'], registry_rows=registry)
    assert report['counts'] == {'paired': 2, 'gained': 1, 'lost': 1}
    assert report['population']['excluded_unowned'] == 1
    roofs = {(r['baseline_object_id'], r['candidate_object_id']): r for r in report['roofs']}
    a = roofs[('a', 'a')]
    assert (a['band_before'], a['band_after']) == ('medium', 'high')
    assert a['score_delta'] == pytest.approx(.02)
    assert a['mismatch_delta'] == pytest.approx(-.2)
    assert a['segments_delta'] == 1
    assert a['iou_before_after'] == pytest.approx(57/63, abs=1e-3)
    assert a['reference']['ref_id'] == 'crm-9' and a['reference']['tier'] == 'T2'
    assert a['reference_iou_before'] == pytest.approx(1, abs=1e-6)
    assert a['reference_iou_after'] == pytest.approx(57/63, abs=1e-3)
    assert a['line_pr_before']['precision'] == a['line_pr_before']['recall'] == 1
    assert a['line_pr_after']['recall'] == 0, 'A 3 px (~0.65 m) ridge shift exceeds the 0.5 m tolerance'
    b = roofs[('b', 'b2')]
    assert b['match'] == 'iou'
    assert b['mismatch_delta'] is None, 'Mismatch needs >= 3 observed segments on both sides'
    assert b['segments_delta'] == 2
    assert b['reference'] is None, 'Tier-3 agent traces never serve as references'
    assert roofs[('c', None)]['match'] == 'lost' and roofs[(None, 'd')]['match'] == 'gained'
    assert report['band_transition_matrix'] == {'medium': {'high': 1}, 'low': {'medium': 1}}
    s2, = [s for s in report['scenes'] if s['scene_id'] == 's2']
    assert s2['paired'] is False and 'input' in s2['reason']
    assert report['sign_counts']['reference_iou_delta'] == {'improved': 0, 'worse': 1, 'within_tolerance': 0, 'n': 1, 'tolerance': .02}
    assert any('n referenced roofs = 1: anecdotal' in line for line in report['cannot_claim'])
    assert any('2 paired roofs' in line for line in report['claims'])
    assert any('s2' in line for line in report['cannot_claim'])
    assert report['version'] == paired_compare.VERSION and len(report['code_sha256']) == 64


def test_bootstrap_ci_shape_and_determinism():
    values = [.1, -.05, .2, .15, .0, .3, .12]
    first = paired_compare.bootstrap_median(values, seed=3)
    assert set(first) >= {'n', 'median', 'ci95', 'resamples', 'seed'}
    assert first['n'] == 7 and first['resamples'] == 2000 and len(first['ci95']) == 2
    assert first['ci95'][0] <= first['median'] <= first['ci95'][1]
    assert paired_compare.bootstrap_median(values, seed=3) == first
    assert paired_compare.bootstrap_median([.4])['ci95'] is None
    assert paired_compare.bootstrap_median([])['median'] is None


def test_report_bootstrap_block_and_stale_confidence(roots, tmp_path):
    base, cand, registry = roots
    report = paired_compare.compare(base, cand, ['s1'], registry_rows=registry, seed=1)
    for metric in ('score_delta', 'mismatch_delta', 'segments_delta', 'iou_before_after', 'reference_iou_delta'):
        assert report['bootstrap'][metric]['resamples'] == 2000
    assert report['bootstrap']['score_delta']['n'] == 2
    assert report['bootstrap']['mismatch_delta']['n'] == 1
    # A confidence report for another model generation is not evidence for this one.
    stale = json.loads((cand/'data/scenes/s1/confidence.json').read_text())
    stale['model_signature'] = 'older'
    (cand/'data/scenes/s1/confidence.json').write_text(json.dumps(stale))
    report = paired_compare.compare(base, cand, ['s1'])
    a, = [r for r in report['roofs'] if r['baseline_object_id'] == 'a']
    assert a['band_after'] is None and a['mismatch_delta'] is None
    assert report['scenes'][0]['confidence_after'] == 'stale'
    # A caller-provided reader (e.g. sqlite-backed) replaces confidence.json.
    report = paired_compare.compare(base, cand, ['s1'], confidence_reader=lambda root, sid, scene: {
        o['id']: {'band': 'unassessed', 'observed_segments': 0} for o in scene['objects']})
    assert report['band_transition_matrix'] == {'unassessed': {'unassessed': 2}}


def test_cli_reads_registry_from_eyes_index(roots, tmp_path, capsys):
    base, cand, registry = roots
    eyes = tmp_path/'eyes-root'
    with closing(eyes_index.connection(eyes)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'references', [{**r, 'version': 'reference-registry-v1'} for r in registry])
    code = paired_compare.main(['--baseline-root', str(base), '--candidate-root', str(cand), '--scenes', 's1',
                                '--registry-root', str(eyes)])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report['counts']['paired'] == 2
    a, = [r for r in report['roofs'] if r['baseline_object_id'] == 'a']
    assert a['reference']['ref_id'] == 'crm-9'


REAL_TILE = paired_compare.REPO/'data/scenes/mooroolbark-50011498-17-118442-80417'


@pytest.mark.skipif(not (REAL_TILE/'confidence.json').exists(), reason='real 27 Cambridge scan tile not present')
def test_real_scan_tile_self_comparison_is_a_null_experiment():
    """Read-only: real latest.json/confidence.json shapes, owned-roof population, the hidden low roof."""
    report = paired_compare.compare(paired_compare.REPO, paired_compare.REPO, [REAL_TILE.name])
    scene = json.loads((REAL_TILE/'latest.json').read_text())
    owned = {o['id'] for o in scene['objects'] if o['kind'] == 'building' and o.get('status') != 'superseded'
             and o['id'] in scene['owned_object_ids']}
    assert report['counts'] == {'paired': len(owned), 'gained': 0, 'lost': 0}
    assert report['scenes'][0]['confidence_before'] == 'current'
    assert all(after == before for before, row in report['band_transition_matrix'].items() for after in row)
    cambridge, = [r for r in report['roofs'] if r['baseline_object_id'] == '44c724fd3480c167']
    assert cambridge['band_before'] == cambridge['band_after'] == 'low' and cambridge['match'] == 'id'


def registry_edge(pixels, kind='ridge'):
    """reference_registry format: edges[{a_lonlat, b_lonlat, kind}] (no edges_wgs84 key)."""
    a, b = wgs84_line(pixels)
    return {'a_xy_m': None, 'b_xy_m': None, 'a_lonlat': a, 'b_lonlat': b, 'kind': kind}


def test_registry_edges_format_gives_line_precision_recall(roots):
    base, cand, _ = roots
    ridge = [[110, 130], [150, 130]]
    ref = {'ref_id': 'rev-1', 'tier': ' t2 ', 'source_kind': 'review',
           'payload': json.dumps({'geometry_wgs84': mapping(geographic({'ring': square(100, 100, 60)}, FRAME)),
                                  'edges': [registry_edge(ridge), {'a_lonlat': None, 'b_lonlat': None, 'kind': 'hip'}, 'junk']})}
    report = paired_compare.compare(base, cand, ['s1'], registry_rows=[ref])
    a, = [r for r in report['roofs'] if r['baseline_object_id'] == 'a']
    assert a['reference']['tier'] == 'T2' and a['n_references'] == 1
    assert a['line_pr_before'] == {'precision': 1., 'recall': 1., 'n_model_lines': 1, 'n_ref_edges': 1}
    assert a['line_pr_after']['recall'] == 0
    assert paired_compare._tier({'tier': 2}) == 'T2' and paired_compare._tier({'tier': None}) not in ('T1', 'T2')
    assert paired_compare._payload({'payload': '{not json'}) == {}
    assert paired_compare.references([{'tier': 'T1', 'payload': '{bad'}]) == []


def test_non_finite_values_never_enter_bootstrap_or_sign_counts():
    nan, inf = float('nan'), float('inf')
    boot = paired_compare.bootstrap_median([.1, nan, .2, inf, None], resamples=50)
    assert boot['n'] == 2 and boot['median'] == pytest.approx(.15)
    assert all(np.isfinite(boot['ci95']))
    assert paired_compare.sign_counts([nan, .1, -inf]) == {'improved': 1, 'worse': 0, 'within_tolerance': 0, 'n': 1, 'tolerance': .02}


def test_profiles_are_recorded_and_mixed_profiles_cannot_be_pooled(tmp_path):
    from scripts.ml.scene_confidence import profile
    base, cand = tmp_path/'baseline', tmp_path/'candidate'
    objects = [roof('a', square(100, 100, 60), .9)]
    before = write_scene(base, 's1', objects, 'sig-b', {'a': ('medium', .9, 4, .5)})
    after = write_scene(cand, 's1', objects, 'sig-c', {'a': ('medium', .9, 4, .5)})
    analysis = {'pipeline': 'sam3-scene-roof-v12', 'roof_model': 'colour-line-heuristic', 'input_geometry': 'imagery-only'}
    for root, scene, extra in ((base, before, {'inference_profile': 'frozen-plan'}), (cand, after, {})):
        scene['analysis'] = {**analysis, **extra}
        (root/'data/scenes/s1/latest.json').write_text(json.dumps(scene))
    report = paired_compare.compare(base, cand, ['s1'])
    entry, = report['scenes']
    assert entry['profile_before'] == profile(before) and entry['profile_after'] == profile(after)
    assert entry['profile_before'] != entry['profile_after']
    assert report['population']['profiles'] == {'before': [entry['profile_before']], 'after': [entry['profile_after']]}
    assert any('profile' in line and 's1' in line for line in report['cannot_claim'])
    # Same profile on both sides: no such caveat.
    (cand/'data/scenes/s1/latest.json').write_text(json.dumps({**after, 'analysis': {**analysis, 'inference_profile': 'frozen-plan'}}))
    report = paired_compare.compare(base, cand, ['s1'])
    assert report['scenes'][0]['profile_before'] == report['scenes'][0]['profile_after']
    assert not any('profile differs' in line for line in report['cannot_claim'])


def test_line_matching_shares_reference_agreement_rule():
    """One line rule across the eyes modules: paired_compare defers to reference_agreement.lines_match."""
    from scripts.ml import reference_agreement
    long = (np.array([0., 0.]), np.array([10., 0.]), 'ridge')
    short_on_line = (np.array([4., .1]), np.array([6., .1]), 'ridge')
    shifted = (np.array([0., .7]), np.array([10., .7]), 'ridge')
    tilted = (np.array([0., 0.]), np.array([10., 2.5]), 'ridge')
    for a, b in ((long, short_on_line), (long, shifted), (long, tilted), (short_on_line, shifted)):
        expected = reference_agreement.lines_match([a[0], a[1]], [b[0], b[1]], distance=.5, angle=10.)
        assert paired_compare.segments_match(a, b) is expected, (a, b)
    assert paired_compare.segments_match(long, short_on_line) is True, 'A short reference edge lying on a model line matches'
    assert paired_compare.segments_match(long, shifted) is False
    obj = {'roof_lines': [{'kind': 'ridge', 'line_meters': [[0, 0], [10, 0]]}]}
    assert paired_compare.line_pr(obj, [short_on_line]) == {'precision': 1., 'recall': 1., 'n_model_lines': 1, 'n_ref_edges': 1}
    nan = float('nan')
    ref = {'payload': {'edges': [{'a_lonlat': [145.31, nan], 'b_lonlat': [145.311, -37.79], 'kind': 'hip'}]}}
    assert paired_compare.reference_edges(ref, FRAME) == [], 'Non-finite reference coordinates are skipped'
