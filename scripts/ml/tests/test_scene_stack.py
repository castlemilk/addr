"""scene_stack: Esri Wayback dates registered onto the saved scene grid.

Fixture expectations are frozen on synthetic data (register() is seeded), never on
live Wayback results, because acceptance flipped between code versions in the pilot.
"""
import hashlib
import io
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import requests
from PIL import Image
from scripts.ml import scene_stack

SCENE = 'synthetic-stack-scene'
# Four "roofs": bright rectangles with a dark outline and a ridge line on a textured ground.
RECTS = [(60, 70, 200, 180, 210), (260, 90, 420, 190, 180), (80, 300, 230, 440, 160), (300, 280, 460, 450, 230)]
RINGS = [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]] for x0, y0, x1, y1, _ in RECTS]
OWNED = ['roof-0', 'roof-1', 'roof-3']


def _scene_image(seed=7):
    rng = np.random.default_rng(seed)
    a = rng.integers(40, 120, (512, 512, 3), dtype=np.uint8)
    a = cv2.GaussianBlur(a, (5, 5), 1.2)
    for x0, y0, x1, y1, value in RECTS:
        a[y0:y1, x0:x1] = value
        noise = rng.integers(-8, 8, (y1 - y0, x1 - x0, 3))
        a[y0:y1, x0:x1] = np.clip(a[y0:y1, x0:x1].astype(int) + noise, 0, 255).astype(np.uint8)
        cv2.rectangle(a, (x0, y0), (x1, y1), (30, 30, 30), 2)
        cv2.line(a, ((x0 + x1)//2, y0), ((x0 + x1)//2, y1), (90, 90, 90), 2)
    return a


def _warped(base, dx, dy, deg, seed):
    """A dated capture: rotated/shifted, brighter, noisier, reflected borders."""
    matrix = cv2.getRotationMatrix2D((256, 256), deg, 1.)
    matrix[:, 2] += (dx, dy)
    out = cv2.warpAffine(base, matrix, (512, 512), borderMode=cv2.BORDER_REFLECT)
    rng = np.random.default_rng(seed)
    return np.clip(out.astype(int) + rng.integers(-6, 6, out.shape) + 12, 0, 255).astype(np.uint8)


def _warped_black_border(base, dx, dy, deg, seed):
    """Like _warped but black outside the warp footprint, as imagery_dates.capture + register() produce."""
    matrix = cv2.getRotationMatrix2D((256, 256), deg, 1.)
    matrix[:, 2] += (dx, dy)
    out = cv2.warpAffine(base, matrix, (512, 512), borderMode=cv2.BORDER_CONSTANT)
    rng = np.random.default_rng(seed)
    return np.clip(out.astype(int) + rng.integers(-6, 6, out.shape) + 12, 0, 255).astype(np.uint8)


def _scrambled(base, seed=99):
    rng = np.random.default_rng(seed)
    return base.reshape(-1, 3)[rng.permutation(base.shape[0]*base.shape[1])].reshape(base.shape)


def _png(array):
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format='PNG')
    return buffer.getvalue()


def _write_scene(root, array, owned=OWNED):
    png = _png(array)
    sha = hashlib.sha256(png).hexdigest()
    height, width = array.shape[:2]
    objects = [{'id': f'roof-{i}', 'kind': 'building', 'label': 'building', 'status': 'candidate', 'ring': ring,
                'model': {'height_m': 3.}} for i, ring in enumerate(RINGS)]
    objects.append({'id': 'tree-0', 'kind': 'tree', 'label': 'tree', 'status': 'candidate',
                    'ring': [[10, 10], [30, 10], [30, 30], [10, 30]]})
    scene = {'id': SCENE, 'title': 'synthetic', 'schema_version': 1, 'model_signature': 'sig-1', 'image_sha256': sha,
             'image_width': width, 'image_height': height,
             'pixel_to_meters': [[.25, 0], [0, -.25], [-64, 64]], 'meters_to_pixel': [[4, 0], [0, -4], [256, 256]],
             'centroid': {'lon': 145.312, 'lat': -37.7913},
             'region': {'provider': 'google', 'imagery_zoom': 19, 'acquisition_date': None,
                        'bounds': [145.3105, -37.7926, 145.3136, -37.7901]},
             'objects': objects}
    if owned is not None:
        scene['owned_object_ids'] = [*owned, 'tree-0']
    folder = root/'data/scenes'/SCENE
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    (folder/f'{sha}.png').write_bytes(png)
    return scene


def _record(acquisition_id, date, provider='Vantor', resolution=.3):
    return {'acquisition_id': acquisition_id, 'acquisition_date': date, 'provider': provider, 'source': 'WV03',
            'resolution_m': resolution, 'accuracy_m': 5., 'release': '12345', 'release_title': f'World Imagery ({date})',
            'imagery_zoom': 19, 'attribution': 'Esri World Imagery Wayback / '+provider}


def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f'network access attempted: {args[:1]}')
    monkeypatch.setattr(requests, 'get', refuse)


def _patch_fetchers(monkeypatch, records, images, skipped=None):
    """Replace the Wayback discovery/capture helpers; any real request fails the test."""
    _no_network(monkeypatch)
    calls = {'discover': [], 'capture': []}

    def discover(scene, cache, count=3):
        calls['discover'].append((Path(cache), count))
        return records[:count], skipped or []

    def capture(scene, record, cache):
        calls['capture'].append((record['acquisition_id'], Path(cache)))
        return Image.fromarray(images[record['acquisition_id']])
    monkeypatch.setattr(scene_stack, 'discover_acquisitions', discover)
    monkeypatch.setattr(scene_stack, 'fetch_capture', capture)
    return calls


def _snapshot(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


def _registration(report, acquisition_id):
    return next(c for c in report['captures'] if c['acquisition_id'] == acquisition_id)['registration']


def test_build_registers_dates_onto_saved_grid_and_writes_bound_assets(tmp_path, monkeypatch):
    base = _scene_image()
    scene = _write_scene(tmp_path, base)
    before = _snapshot(tmp_path/'data/scenes'/SCENE)
    images = {'a2025': _warped(base, 4, -3, .4, 11), 'b2023': _warped(base, -6, 5, -.6, 12), 'c2021': _scrambled(base)}
    records = [_record('a2025', '2025-11-06'), _record('b2023', '2023-12-20', 'Maxar', .31), _record('c2021', '2021-03-01')]
    calls = _patch_fetchers(monkeypatch, records, images, skipped=[{'release': '1', 'reason': 'Mixed metadata'}])

    report = scene_stack.build(SCENE, tmp_path)

    assert report['status'] == 'available' and report['n_accepted'] == 2 and report['n_views'] == 3
    assert report['version'] == 'scene-stack-v1' and report['code_sha256'] == scene_stack.CODE_SHA256
    assert report['computed_at'] and report['scene_id'] == SCENE
    assert report['source_image_sha256'] == scene['image_sha256']
    assert report['discovery'] == {'status': 'ok', 'requested': 3, 'found': 3,
                                   'skipped': [{'release': '1', 'reason': 'Mixed metadata'}]}
    # Discovery and tiles are cached exactly where scene_temporal caches them; nothing else is fetched.
    cache = tmp_path/'data/roof-review-loop/multidate/cache'
    assert calls['discover'] == [(cache, 3)] and [c[1] for c in calls['capture']] == [cache/'tiles']*3
    # The saved scene is the registration reference: rotations/shifts are recovered per capture.
    a, b, c = (_registration(report, k) for k in ('a2025', 'b2023', 'c2021'))
    assert a['accepted'] and b['accepted'] and not c['accepted']
    assert [x['status'] for x in report['captures']] == ['accepted', 'accepted', 'rejected']
    assert c['reason'].startswith('Too few independent registration matches')
    for reg, deg in ((a, .4), (b, -.6)):
        assert reg['status'] == 'accepted' and reg['median_residual_px'] <= 2.
        assert abs(reg['rotation_deg'] - deg) < .1 and abs(reg['scale'] - 1) < .01 and reg['shift_px'] > 3
        assert reg['holdout_inliers'] >= 10 and 0 < reg['holdout_ratio'] <= 1 and reg['coverage'] >= .2
        assert reg['fit_inliers'] >= 25 and len(reg['transform']) == 2
    captures = {c['acquisition_id']: c for c in report['captures']}
    assert captures['b2023']['sensor'] == 'Maxar' and captures['b2023']['resolution_m'] == .31
    assert captures['b2023']['date'] == '2023-12-20'
    assert captures['a2025']['combined_fraction'] > .9 and captures['c2021']['combined_fraction'] is None
    # Assets under the multidate stacks folder only, each bound by sha256 in the report.
    folder = tmp_path/'data/roof-review-loop/multidate/stacks'/report['stack_id']
    assert folder.is_dir() and (folder/'report.json').exists()
    assert set(scene_stack.STACK_ASSETS) <= set(report['assets'])
    for name, asset in report['assets'].items():
        data = (folder/name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == asset['sha256'], name
        assert Image.open(io.BytesIO(data)).size == (512, 512)
    saved_gray = cv2.cvtColor(base, cv2.COLOR_RGB2GRAY).astype(float)
    median = np.asarray(Image.open(folder/'stack-median.png'))
    count = np.asarray(Image.open(folder/'valid-count.png'))
    consensus = np.asarray(Image.open(folder/'edge-consensus.png'))
    assert median.dtype == np.uint8 and median.ndim == 2
    assert count.max() == 3 and count.min() >= 1 and (count == 3).mean() > .9
    assert np.abs(median[count == 3] - saved_gray[count == 3]).mean() < 4   # brightness-normalised, not brightened
    assert consensus.max() == 3 and consensus[100:170, 140:190].max() == 0   # flat roof interior (off the ridge)
    assert consensus[70, 100:160].max() == 3                                 # the shared roof outline
    assert json.loads((folder/'report.json').read_text())['stack_id'] == report['stack_id']
    assert report['stack_id'] == scene_stack.stack_id(scene['image_sha256'], ['b2023', 'a2025'])
    # Per-roof ECC refinement is recorded for owned buildings x accepted captures, never applied.
    assert len(report['per_roof_local']) == len(OWNED)*2
    assert {(r['object_id'], r['capture']) for r in report['per_roof_local']} == {(o, k) for o in OWNED for k in ('a2025', 'b2023')}
    for row in report['per_roof_local']:
        assert row['local_corr'] > .9 and row['local_shift_norm_px'] < 1 and row['within_budget']
        assert len(row['local_shift_px']) == 2 and row['valid_fraction'] > .5
    assert report['gates'] == {'median_residual_px_max': 2., 'local_shift_px_max': 3., 'min_accepted': 2,
                               'existing': 'temporal_evidence.register'}
    # Production scene files are byte-identical; nothing landed under data/scenes.
    assert _snapshot(tmp_path/'data/scenes'/SCENE) == before
    assert not (tmp_path/'data/scenes'/SCENE/'stack-median.png').exists()
    # Report is plain JSON (no numpy scalars) and round-trips.
    assert json.loads(json.dumps(report)) == report


def test_median_residual_gate_rejects_alignment_that_passes_existing_gates(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    images = {'a': _warped(base, 4, -3, .4, 11), 'b': _warped(base, -6, 5, -.6, 12)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('b', '2023-12-20')], images)
    real = scene_stack.register

    def loose(reference, candidate):
        aligned, valid, report = real(reference, candidate)
        return aligned, valid, {**report, 'check_median_residual_px': 2.4}
    monkeypatch.setattr(scene_stack, 'register', loose)

    report = scene_stack.build(SCENE, tmp_path)

    assert report['status'] == 'not_available' and report['n_accepted'] == 0
    for capture in report['captures']:
        assert capture['registration']['accepted'] is False and capture['registration']['status'] == 'accepted'
        assert capture['status'] == 'rejected'   # final decision, distinct from register()'s own verdict
        assert capture['registration']['median_residual_px'] == 2.4
        assert capture['registration']['reason'] == 'Median hold-out residual 2.4 px exceeds 2.0 px'
    folder = tmp_path/'data/roof-review-loop/multidate/stacks'/report['stack_id']
    assert sorted(p.name for p in folder.iterdir()) == ['report.json']


def test_fewer_than_three_views_is_not_available_and_writes_registration_report_only(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    images = {'a': _warped(base, 4, -3, .4, 11), 'c': _scrambled(base)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('c', '2021-03-01')], images)

    report = scene_stack.build(SCENE, tmp_path)

    assert report['status'] == 'not_available' and report['n_accepted'] == 1 and report['n_views'] == 2
    assert 'reason' in report and '2' in report['reason']
    assert _registration(report, 'a')['accepted'] and not _registration(report, 'c')['accepted']
    assert report['assets'] == {} and report['per_roof_local'] and report['stack'] is None
    folder = tmp_path/'data/roof-review-loop/multidate/stacks'/report['stack_id']
    assert sorted(p.name for p in folder.iterdir()) == ['report.json']
    assert scene_stack.views(report, tmp_path) == [] and scene_stack.views(report['stack_id'], tmp_path) == []


def test_duplicate_capture_pixels_count_once(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    same = _warped(base, 4, -3, .4, 11)
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('b', '2025-06-01')], {'a': same, 'b': same.copy()})

    report = scene_stack.build(SCENE, tmp_path)

    assert report['n_accepted'] == 1 and report['status'] == 'not_available'
    b = _registration(report, 'b')
    assert b['status'] == 'duplicate' and not b['accepted'] and 'Repeated source pixels' in b['reason']
    assert [c['status'] for c in report['captures']] == ['accepted', 'duplicate']


def test_discovery_failure_is_recorded_as_unavailable(tmp_path, monkeypatch):
    _write_scene(tmp_path, _scene_image())
    _no_network(monkeypatch)

    def offline(scene, cache, count=3):
        raise requests.ConnectionError('name resolution failed')
    monkeypatch.setattr(scene_stack, 'discover_acquisitions', offline)

    report = scene_stack.build(SCENE, tmp_path)

    assert report['status'] == 'unavailable' and report['captures'] == [] and report['n_accepted'] == 0
    assert report['discovery']['status'] == 'unavailable' and 'name resolution failed' in report['discovery']['error']
    folder = tmp_path/'data/roof-review-loop/multidate/stacks'/report['stack_id']
    assert sorted(p.name for p in folder.iterdir()) == ['report.json'] and report['assets'] == {}


def test_fetch_false_uses_the_real_discovery_without_touching_the_network(tmp_path, monkeypatch):
    _write_scene(tmp_path, _scene_image())
    _no_network(monkeypatch)   # would raise AssertionError, not a graceful 'unavailable'

    report = scene_stack.build(SCENE, tmp_path, fetch=False)

    assert report['status'] == 'unavailable'
    assert 'fetch=False' in report['discovery']['error']
    assert not (tmp_path/'data/roof-review-loop/multidate/cache').exists() or \
        not any((tmp_path/'data/roof-review-loop/multidate/cache').glob('*.json'))


def test_capture_download_failure_is_a_failed_capture_not_a_crash(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    images = {'a': _warped(base, 4, -3, .4, 11)}
    calls = _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('x', '2024-01-01')], images)
    good = scene_stack.fetch_capture

    def flaky(scene, record, cache):
        if record['acquisition_id'] == 'x':
            raise requests.HTTPError('503 Service Unavailable')
        return good(scene, record, cache)
    monkeypatch.setattr(scene_stack, 'fetch_capture', flaky)

    report = scene_stack.build(SCENE, tmp_path)

    x = _registration(report, 'x')
    assert x['status'] == 'failed' and not x['accepted'] and '503' in x['reason']
    assert [c['status'] for c in report['captures']] == ['accepted', 'failed']
    assert report['n_accepted'] == 1 and calls['discover']


def test_views_verify_asset_hashes_and_scale_edge_consensus(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    images = {'a': _warped(base, 4, -3, .4, 11), 'b': _warped(base, -6, 5, -.6, 12)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('b', '2023-12-20')], images)
    report = scene_stack.build(SCENE, tmp_path)
    assert report['status'] == 'available'

    by_id = scene_stack.views(report['stack_id'], tmp_path)
    by_report = scene_stack.views(report, tmp_path)
    assert [name for name, _ in by_id] == ['stack-median', 'edge-consensus']
    for (name, array), (other, again) in zip(by_id, by_report, strict=True):
        assert name == other and array.dtype == np.uint8 and array.shape == (512, 512)
        assert np.array_equal(array, again)
    consensus = dict(by_id)['edge-consensus']
    assert consensus.max() == 255 and set(np.unique(consensus)) <= {0, 85, 170, 255}
    folder = tmp_path/'data/roof-review-loop/multidate/stacks'/report['stack_id']
    tampered = np.asarray(Image.open(folder/'stack-median.png')).copy()
    tampered[0, 0] ^= 1
    Image.fromarray(tampered).save(folder/'stack-median.png')
    with pytest.raises(ValueError, match='sha256'):
        scene_stack.views(report['stack_id'], tmp_path)
    with pytest.raises(ValueError):
        scene_stack.views('not-a-stack-id', tmp_path)


def test_local_refinement_reports_translation_and_correlation_without_applying_it():
    gray = cv2.cvtColor(_scene_image(), cv2.COLOR_RGB2GRAY)
    moved = np.roll(np.roll(gray, 2, axis=1), 1, axis=0)   # roof appears 2 px right, 1 px down in the capture
    ring = RINGS[0]
    row = scene_stack.local_refinement(gray, moved, ring)
    assert row['local_corr'] > .99 and row['within_budget'] and row['error'] is None
    assert np.allclose(row['local_shift_px'], [2, 1], atol=.1) and abs(row['local_shift_norm_px'] - 5**.5) < .1
    assert row['bbox_px'] == [52, 62, 208, 188]   # ring bbox padded by 8 px
    far = scene_stack.local_refinement(gray, np.roll(gray, 5, axis=1), ring)
    assert far['error'] is None and not far['within_budget'] and abs(far['local_shift_px'][0] - 5) < .3
    flat = scene_stack.local_refinement(gray, np.full_like(gray, 90), ring)
    assert flat['local_corr'] is None and flat['error'] and flat['within_budget'] is None
    tiny = scene_stack.local_refinement(gray, moved, [[5, 5], [9, 5], [9, 9], [5, 9]])
    assert tiny['error'] == 'Roof too small for local refinement' and tiny['local_corr'] is None
    partial = scene_stack.local_refinement(gray, moved, ring, valid=np.zeros_like(gray, bool))
    assert partial['valid_fraction'] == 0.
    edge = scene_stack.local_refinement(gray, moved, [[500, 500], [520, 500], [520, 520], [500, 520]])
    assert edge['bbox_px'][2] == 512 and edge['bbox_px'][3] == 512


def test_stack_statistics_normalise_brightness_and_mask_validity():
    gray = cv2.cvtColor(_scene_image(), cv2.COLOR_RGB2GRAY)
    brighter = np.clip(gray.astype(int) + 10, 0, 255).astype(np.uint8)
    brighter[100:140, 100:140] = 255                      # repainted roof: real disagreement
    darker = np.clip(gray.astype(int) - 10, 0, 255).astype(np.uint8)
    valid_b = np.ones(gray.shape, bool)
    valid_b[:40] = False                                  # no local registration support in the top band
    stats = scene_stack.stack_statistics(gray, [brighter, darker], [np.ones(gray.shape, bool), valid_b])
    assert set(stats) >= {'median', 'min', 'max', 'ptp', 'count', 'offsets'}
    assert stats['count'][60, 60] == 3 and stats['count'][10, 10] == 2 and stats['count'].min() == 2  # saved always counts
    assert np.abs(stats['offsets'][0] + 10) < 1.5 and np.abs(stats['offsets'][1] - 10) < 1.5
    outside = np.ones(gray.shape, bool)
    outside[95:145, 95:145] = False
    assert np.abs(stats['median'][outside].astype(int) - gray[outside].astype(int)).max() <= 1
    assert stats['ptp'][outside].max() <= 1 and stats['ptp'][110:130, 110:130].min() > 20
    assert (stats['max'] >= stats['median']).all() and (stats['median'] >= stats['min']).all()
    for key in ('median', 'min', 'max', 'ptp', 'count'):
        assert stats[key].dtype == np.uint8 and stats[key].shape == gray.shape


def test_edge_consensus_counts_views_with_an_edge_within_one_pixel():
    base = _scene_image()
    views = [Image.fromarray(base), Image.fromarray(_warped(base, 0, 0, 0, 5)), Image.fromarray(base)]
    valid = [np.ones(base.shape[:2], bool)]*3
    valid[1] = valid[1].copy()
    valid[1][:100] = False
    consensus = scene_stack.edge_consensus(views, valid)
    assert consensus.dtype == np.uint8 and consensus.shape == base.shape[:2]
    assert consensus[100:170, 140:190].max() == 0            # flat interior of roof 0, off the ridge at x=130
    assert consensus[130, 60:62].max() == 3 and consensus[130, 165] == 0
    assert consensus[70, 100:160].max() == 2 and consensus[:100].max() <= 2   # view 1 masked above row 100
    edges = scene_stack.edge_map(Image.fromarray(base))
    assert edges.dtype == np.uint8 and set(np.unique(edges)) <= {0, 1}
    assert edges[69:72, 120].all()                           # 1 px dilation spans both sides of the outline


def test_stack_id_binds_image_sorted_accepted_dates_and_code():
    a = scene_stack.stack_id('img', ['x', 'y'])
    assert a == scene_stack.stack_id('img', ['y', 'x']) and len(a) == 16 and a != scene_stack.stack_id('img', ['x'])
    assert a != scene_stack.stack_id('other', ['x', 'y'])
    expected = hashlib.sha256(json.dumps(['img', ['x', 'y'], scene_stack.CODE_SHA256], sort_keys=True).encode()).hexdigest()[:16]
    assert a == expected


def test_registration_gate_combines_existing_status_with_median_residual():
    accepted = {'status': 'accepted', 'check_median_residual_px': 1.7}
    assert scene_stack.registration_gate(accepted) == (True, 'Existing register() gates and median hold-out residual 1.7 px <= 2.0 px')
    assert scene_stack.registration_gate({**accepted, 'check_median_residual_px': 2.001})[0] is False
    assert scene_stack.registration_gate({**accepted, 'check_median_residual_px': None}) == (False, 'Median hold-out residual unavailable')
    assert scene_stack.registration_gate({'status': 'rejected', 'reason': 'x'}) == (False, 'x')
    assert scene_stack.registration_gate({'status': 'duplicate', 'reason': 'Identical source pixels'}) == (False, 'Identical source pixels')


def test_build_refuses_bad_scene_ids_and_changed_source_pixels(tmp_path, monkeypatch):
    base = _scene_image()
    scene = _write_scene(tmp_path, base)
    _no_network(monkeypatch)
    with pytest.raises(ValueError, match='valid scene'):
        scene_stack.build('../evil', tmp_path)
    (tmp_path/'data/scenes'/SCENE/f'{scene["image_sha256"]}.png').write_bytes(_png(_scrambled(base)))
    with pytest.raises(ValueError, match='identity'):
        scene_stack.build(SCENE, tmp_path)


def test_scene_without_owned_ids_refines_every_building(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base, owned=None)
    images = {'a': _warped(base, 4, -3, .4, 11), 'b': _warped(base, -6, 5, -.6, 12)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('b', '2023-12-20')], images)
    report = scene_stack.build(SCENE, tmp_path, max_dates=2)
    assert report['discovery']['requested'] == 2
    assert {r['object_id'] for r in report['per_roof_local']} == {f'roof-{i}' for i in range(4)}


def test_cli_prints_compact_json(tmp_path, monkeypatch, capsys):
    base = _scene_image()
    _write_scene(tmp_path, base)
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06')], {'a': _warped(base, 4, -3, .4, 11)})
    scene_stack.main([SCENE, '--root', str(tmp_path), '--max-dates', '1'])
    printed = json.loads(capsys.readouterr().out)
    assert printed['status'] == 'not_available' and printed['n_accepted'] == 1
    assert 'transform' not in json.dumps(printed) and printed['captures'][0]['registration']['accepted'] is True
    assert 'sole evidence gate' in printed['note']   # the not-evidence copy survives the compact CLI view


def test_edge_consensus_ignores_the_black_warp_border(tmp_path, monkeypatch):
    """Aligned captures are black outside the warp footprint; Canny fires along that boundary. Those pixels never vote."""
    base = _scene_image()
    _write_scene(tmp_path, base)
    images = {'a': _warped_black_border(base, 6, -4, .3, 11), 'b': _warped_black_border(base, -9, 7, -.5, 12)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('b', '2023-12-20')], images)

    report = scene_stack.build(SCENE, tmp_path)

    assert report['status'] == 'available' and report['stack']['edge_border_px'] == 3
    folder = tmp_path/'data/roof-review-loop/multidate/stacks'/report['stack_id']
    consensus = np.asarray(Image.open(folder/'edge-consensus.png')).astype(int)
    saved_edges = scene_stack.edge_map(Image.fromarray(base)).astype(int)
    for capture in report['captures']:
        i = capture['index']
        transform = np.asarray(capture['registration']['transform'], np.float32)
        footprint = cv2.warpAffine(np.ones((512, 512), np.uint8), transform, (512, 512), flags=cv2.INTER_NEAREST).astype(bool)
        valid = np.asarray(Image.open(folder/f'valid-{i}.png')) > 0
        band = footprint & ~cv2.erode(footprint.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool) & valid
        assert band.sum() > 1000
        aligned = Image.open(folder/f'aligned-{i}.png')
        raw = scene_stack.edge_consensus([aligned], [valid])
        masked = scene_stack.edge_consensus([aligned], [scene_stack.edge_mask(valid, footprint)])
        assert raw[band].mean() > .5                       # the artifact is real on the raw registration mask
        assert masked[band].max() == 0                     # and gone once the footprint border is excluded
        extra = np.clip(consensus - saved_edges, 0, None)  # votes from aligned views in the build's raster
        assert extra[band].mean() < .2
    # edge_mask only trims a thin border along the warp footprint: nearly every registration-valid pixel still votes.
    valid = np.asarray(Image.open(folder/'valid-0.png')) > 0
    transform = np.asarray(report['captures'][0]['registration']['transform'], np.float32)
    footprint = cv2.warpAffine(np.ones((512, 512), np.uint8), transform, (512, 512), flags=cv2.INTER_NEAREST).astype(bool)
    kept = scene_stack.edge_mask(valid, footprint)
    assert kept.sum() < valid.sum() and kept.sum() > .97*valid.sum()
    assert np.array_equal(scene_stack.edge_mask(valid, np.ones((512, 512), bool)), valid)   # no footprint border, no trim
    assert 'valid-count.png >= 2' in report['stack']['disagreement_defined_where']


def test_helpers_reject_mismatched_shapes_and_empty_input():
    gray = cv2.cvtColor(_scene_image(), cv2.COLOR_RGB2GRAY)
    small = gray[:256, :256]
    ones = np.ones(gray.shape, bool)
    with pytest.raises(ValueError, match='saved image grid'):
        scene_stack.stack_statistics(gray, [small], [ones])
    with pytest.raises(ValueError, match='saved image grid'):
        scene_stack.stack_statistics(gray, [gray], [ones[:256, :256]])
    with pytest.raises(ValueError, match='HxW'):
        scene_stack.stack_statistics(_scene_image(), [gray], [ones])
    with pytest.raises(ValueError, match='at least one view'):
        scene_stack.edge_consensus([], [])
    with pytest.raises(ValueError, match='match its view'):
        scene_stack.edge_consensus([Image.fromarray(_scene_image())], [ones[:256, :256]])
    with pytest.raises(ValueError, match='same grid'):
        scene_stack.local_refinement(gray, small, RINGS[0])
    with pytest.raises(ValueError, match='same grid'):
        scene_stack.local_refinement(gray, gray, RINGS[0], valid=ones[:256, :256])
    # All-invalid masks are not an error: the saved view always counts, nothing else does.
    stats = scene_stack.stack_statistics(gray, [gray, gray], [np.zeros(gray.shape, bool)]*2)
    assert stats['count'].max() == 1 and np.array_equal(stats['median'], gray) and stats['ptp'].max() == 0
    assert stats['offsets'] == [0., 0.]


def test_registration_gate_treats_non_numeric_residual_as_unavailable():
    for bad in ('1.2', float('nan'), float('inf'), [1.], {}, True):
        assert scene_stack.registration_gate({'status': 'accepted', 'check_median_residual_px': bad}) == \
            (False, 'Median hold-out residual unavailable')
    assert scene_stack.registration_gate({'status': 'accepted', 'check_median_residual_px': 2}) == \
        (True, 'Existing register() gates and median hold-out residual 2 px <= 2.0 px')


def test_views_reject_missing_stack_id_and_assets_outside_the_folder(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    images = {'a': _warped(base, 4, -3, .4, 11), 'b': _warped(base, -6, 5, -.6, 12)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('b', '2023-12-20')], images)
    report = scene_stack.build(SCENE, tmp_path)
    with pytest.raises(ValueError, match='Invalid stack id'):
        scene_stack.views({}, tmp_path)
    with pytest.raises(ValueError, match='Invalid stack id'):
        scene_stack.views(None, tmp_path)
    folder = tmp_path/'data/roof-review-loop/multidate/stacks'/report['stack_id']
    tampered = json.loads((folder/'report.json').read_text())
    tampered['assets']['stack-median.png']['path'] = '../../../../scenes/x.png'
    (folder/'report.json').write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match='stack folder'):
        scene_stack.views(report['stack_id'], tmp_path)
