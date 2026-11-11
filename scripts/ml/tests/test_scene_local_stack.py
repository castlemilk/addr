"""scene_local_stack: Wayback dates registered onto the saved grid per window, emitting the scene_stack contract.

Fixtures are synthetic: a textured scene, dates that are locally warped copies (smooth sinusoidal
shift field, gamma change, one contradictory region) and a scrambled date. Nothing here touches the
network or any production folder.
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
from scripts.ml import imagery_dates, scene_stack
from scripts.ml import multidate_agreement as mda
from scripts.ml import scene_local_stack as sls

SCENE = 'synthetic-local-stack-scene'
SIZE = 512
RECTS = [(60, 70, 200, 180, 210), (260, 90, 420, 190, 180), (80, 300, 230, 440, 160), (300, 280, 460, 450, 230),
         (20, 470, 120, 505, 120), (440, 20, 500, 60, 200)]
RINGS = [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]] for x0, y0, x1, y1, _ in RECTS[:4]]
OWNED = ['roof-0', 'roof-1', 'roof-3']
KNOBS = {'window_px': 128, 'stride_px': 64}     # 7 x 7 = 49 windows on a 512 px scene
CONTRADICTION = (300, 300, 470, 470)             # one date shows different content here


def _scene_image(seed=7):
    rng = np.random.default_rng(seed)
    a = rng.integers(40, 120, (SIZE, SIZE, 3), dtype=np.uint8)
    a = cv2.GaussianBlur(a, (5, 5), 1.2)
    # Roads: two bright bands with dark kerbs so every window has structure.
    a[240:256, :] = (150, 150, 145)
    a[:, 236:250] = (140, 142, 140)
    cv2.line(a, (0, 240), (SIZE, 240), (40, 40, 40), 1)
    cv2.line(a, (236, 0), (236, SIZE), (40, 40, 40), 1)
    for x0, y0, x1, y1, value in RECTS:
        a[y0:y1, x0:x1] = value
        noise = rng.integers(-8, 8, (y1 - y0, x1 - x0, 3))
        a[y0:y1, x0:x1] = np.clip(a[y0:y1, x0:x1].astype(int) + noise, 0, 255).astype(np.uint8)
        cv2.rectangle(a, (x0, y0), (x1, y1), (30, 30, 30), 2)
        cv2.line(a, ((x0 + x1)//2, y0), ((x0 + x1)//2, y1), (90, 90, 90), 2)
    return a


def _field(ax=4., ay=3., period_x=1400., period_y=1100., phase=.3):
    """Smooth sinusoidal shift field s(x, y): saved(x) appears in the capture at x + s(x)."""
    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    dx = ax*np.sin(2*np.pi*y/period_y + phase) + .5
    dy = ay*np.cos(2*np.pi*x/period_x + phase) - .4
    return dx.astype(np.float32), dy.astype(np.float32)


def _locally_warped(base, dx, dy, *, gamma=1., seed=11, contradiction=None):
    """capture(u) = saved(u - s(u)): a locally displaced copy with a gamma change, noise, and optionally
    a region whose content contradicts the saved image."""
    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    out = cv2.remap(base, x - dx, y - dy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    rng = np.random.default_rng(seed)
    out = 255.*(out.astype(float)/255.)**gamma + rng.normal(0, 4, out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    if contradiction:
        x0, y0, x1, y1 = contradiction
        block = out[y0:y1, x0:x1]
        out[y0:y1, x0:x1] = block.reshape(-1, 3)[rng.permutation(block.shape[0]*block.shape[1])].reshape(block.shape)
    return out


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
             'objects': objects, 'owned_object_ids': [*owned, 'tree-0']}
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
    """Replace the Wayback discovery/capture helpers on scene_local_stack; any real request fails the test."""
    _no_network(monkeypatch)
    calls = {'discover': [], 'capture': []}

    def discover(scene, cache, count=3):
        calls['discover'].append((Path(cache), count))
        return records[:count], skipped or []

    def capture(scene, record, cache):
        calls['capture'].append((record['acquisition_id'], Path(cache)))
        return Image.fromarray(images[record['acquisition_id']])
    monkeypatch.setattr(sls, 'discover_acquisitions', discover)
    monkeypatch.setattr(sls, 'fetch_capture', capture)
    return calls


def _snapshot(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


def _repo_stacks():
    folder = Path.cwd()/scene_stack.STACKS
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _capture(report, acquisition_id):
    return next(c for c in report['captures'] if c['acquisition_id'] == acquisition_id)


def _window_errors(rows, dx, dy):
    """|recovered - true| at accepted window centres."""
    errors = []
    for row in rows:
        if not row['accepted']:
            continue
        cx, cy = (int(round(v)) for v in row['centre_px'])
        errors.append(float(np.hypot(row['dx'] - dx[cy, cx], row['dy'] - dy[cy, cx])))
    return np.array(errors)


def _two_good_dates(base):
    dx_a, dy_a = _field()
    dx_b, dy_b = _field(ax=-3.5, ay=5., period_x=1200., period_y=1600., phase=1.1)
    images = {'a2025': _locally_warped(base, dx_a, dy_a, gamma=1.3, seed=11),
              'b2023': _locally_warped(base, dx_b, dy_b, gamma=.8, seed=12, contradiction=CONTRADICTION)}
    return images, {'a2025': (dx_a, dy_a), 'b2023': (dx_b, dy_b)}


def test_build_recovers_local_shift_fields_and_writes_the_scene_stack_contract(tmp_path, monkeypatch):
    base = _scene_image()
    scene = _write_scene(tmp_path, base)
    before, repo_before = _snapshot(tmp_path/'data/scenes'/SCENE), _repo_stacks()
    images, fields = _two_good_dates(base)
    images['c2021'] = _scrambled(base)
    records = [_record('a2025', '2025-11-06'), _record('b2023', '2023-12-20', 'Maxar', .31), _record('c2021', '2021-03-01')]
    calls = _patch_fetchers(monkeypatch, records, images, skipped=[{'release': '1', 'reason': 'Mixed metadata'}])

    report = sls.build(SCENE, tmp_path, **KNOBS)

    assert report['status'] == 'available' and report['n_accepted'] == 2 and report['n_views'] == 3
    assert report['version'] == 'scene-local-stack-v1' and report['code_sha256'] == sls.CODE_SHA256
    assert report['computed_at'] and report['scene_id'] == SCENE and report['method'] == 'local-phase-correlation-v1'
    assert report['source_image_sha256'] == scene['image_sha256']
    assert report['image_width'] == SIZE and report['image_height'] == SIZE
    assert report['discovery'] == {'status': 'ok', 'requested': 3, 'found': 3, 'skipped': [{'release': '1', 'reason': 'Mixed metadata'}]}
    cache = tmp_path/'data/roof-review-loop/multidate/cache'
    assert calls['discover'] == [(cache, 3)] and [c[1] for c in calls['capture']] == [cache/'tiles']*3
    # Same top-level keys as a scene_stack report (consumers read them unchanged), plus per_window and method.
    scene_stack_keys = {'version', 'code_sha256', 'computed_at', 'stack_id', 'status', 'reason', 'scene_id',
                        'source_image_sha256', 'image_width', 'image_height', 'reference', 'gates', 'discovery',
                        'captures', 'n_accepted', 'n_views', 'per_roof_local', 'stack', 'assets', 'note', 'elapsed_seconds'}
    assert scene_stack_keys <= set(report)
    assert 'not' in report['note'].lower() and 'evidence' in report['note'].lower()
    # Per-window registration: both warped dates accepted, the scrambled one rejected on response.
    a, b, c = (_capture(report, k) for k in ('a2025', 'b2023', 'c2021'))
    assert [x['status'] for x in report['captures']] == ['accepted', 'accepted', 'rejected']
    assert a['registration']['accepted'] and b['registration']['accepted'] and not c['registration']['accepted']
    assert c['registration']['windows_accepted'] < sls.MIN_WINDOWS and 'window' in c['registration']['reason']
    assert all(not w['accepted'] for w in c['windows']) and all('response' in w['reason'] for w in c['windows'])
    for row, key in ((a, 'a2025'), (b, 'b2023')):
        reg = row['registration']
        assert reg['method'] == 'local-phase-correlation-v1' and reg['status'] == 'accepted'
        assert reg['windows_total'] == 49 and reg['windows_accepted'] >= sls.MIN_WINDOWS
        assert reg['coverage'] > .8 and reg['holdout_residual_px'] <= 2.
        assert reg['median_shift_px'] > 1 and reg['p95_shift_px'] >= reg['median_shift_px']
        assert reg['median_response'] is not None and reg['median_response_accepted'] >= .25
        assert reg['window_px'] == 128 and reg['stride_px'] == 64
        errors = _window_errors(row['windows'], *fields[key])
        assert len(errors) == reg['windows_accepted'] and np.median(errors) < .7, (key, np.median(errors))
        assert row['combined_fraction'] > .8
        # Whole-tile similarity registration is recorded for comparison, never used for the warp.
        assert isinstance(row['registration_global'], dict) and row['registration_global']['status'] in ('accepted', 'rejected')
        assert 'accepted' in row['registration_global']
    # The contradictory region: windows centred inside it are rejected (low response or neighbour outlier).
    x0, y0, x1, y1 = CONTRADICTION
    inside = [w for w in b['windows'] if x0 + 32 <= w['centre_px'][0] <= x1 - 32 and y0 + 32 <= w['centre_px'][1] <= y1 - 32]
    assert inside and not any(w['accepted'] for w in inside)
    for w in a['windows']:
        assert set(w) >= {'row', 'col', 'centre_px', 'dx', 'dy', 'shift_px', 'response', 'accepted', 'reason', 'code'}
    # Assets: the scene_stack set, aligned/valid per accepted date, the RGB display median and shift fields; all sha-bound.
    folder = tmp_path/scene_stack.STACKS/report['stack_id']
    expected = {*scene_stack.STACK_ASSETS, 'enhanced-median-rgb.png', 'aligned-0.png', 'valid-0.png', 'aligned-1.png',
                'valid-1.png', 'shift-field-0.png', 'shift-field-1.png'}
    assert set(report['assets']) == expected
    assert sorted(p.name for p in folder.iterdir()) == sorted([*expected, 'report.json'])
    for name, asset in report['assets'].items():
        data = (folder/name).read_bytes()
        assert asset['path'] == name and hashlib.sha256(data).hexdigest() == asset['sha256'], name
        assert Image.open(io.BytesIO(data)).size == (SIZE, SIZE)
    saved_gray = cv2.cvtColor(base, cv2.COLOR_RGB2GRAY)
    count = np.asarray(Image.open(folder/'valid-count.png'))
    median = np.asarray(Image.open(folder/'stack-median.png'))
    assert median.ndim == 2 and count.max() == 3 and count.min() >= 1 and (count == 3).mean() > .7
    # Aligned dates sit on the saved grid: no residual whole-frame shift against the saved image.
    hann = cv2.createHanningWindow((SIZE, SIZE), cv2.CV_32F)
    for index, key in ((0, 'a2025'), (1, 'b2023')):
        aligned = np.asarray(Image.open(folder/f'aligned-{index}.png'))
        valid = np.asarray(Image.open(folder/f'valid-{index}.png')) > 127
        assert aligned.shape == (SIZE, SIZE, 3) and valid.mean() > .8
        (rx, ry), _ = cv2.phaseCorrelate(sls.matching_signal(base), sls.matching_signal(aligned), hann)
        assert np.hypot(rx, ry) < .5, (key, rx, ry)
        raw = cv2.cvtColor(images[key], cv2.COLOR_RGB2GRAY)
        aligned_gray = cv2.cvtColor(aligned, cv2.COLOR_RGB2GRAY)
        # Registration brings the date closer to the saved image than the raw capture was (brightness-normalised).
        def normalised(gray, mask):
            return gray.astype(float) - np.median(gray[mask].astype(float) - saved_gray[mask])
        assert (np.abs(normalised(aligned_gray, valid) - saved_gray)[valid].mean()
                < np.abs(normalised(raw, valid) - saved_gray)[valid].mean())
        assert not (aligned[~valid] > 0).all()
    # The display composite: RGB, saved pixels where fewer than three views, close to the saved image elsewhere.
    enhanced = np.asarray(Image.open(folder/'enhanced-median-rgb.png'))
    assert enhanced.shape == (SIZE, SIZE, 3)
    assert np.array_equal(enhanced[count < 3], base[count < 3])
    assert np.abs(enhanced[count == 3].astype(int) - base[count == 3].astype(int)).mean() < 12
    stack = report['stack']
    assert stack['views'] == 3 and stack['gray_only'] is True and stack['registration_method'] == 'local-phase-correlation-v1'
    assert set(stack) >= {'combined_fraction', 'full_fraction', 'disagreement_median', 'disagreement_p90',
                          'edge_consensus_fraction_full', 'edge_consensus_fraction_any', 'canny', 'edge_dilate_px',
                          'edge_border_px', 'brightness_offsets', 'enhanced_median_rgb'}
    assert stack['combined_fraction'] > .8 and stack['enhanced_median_rgb']['composited_fraction'] > .7
    assert stack['enhanced_median_rgb']['note'] == 'display/review view; not an evidence image; no resolution increase; no inpainting'
    # Per-roof ECC on the aligned grays is recorded (owned roofs x accepted dates) and stays near zero.
    assert {(r['object_id'], r['capture']) for r in report['per_roof_local']} == {(o, k) for o in OWNED for k in ('a2025', 'b2023')}
    converged = [r for r in report['per_roof_local'] if r['local_shift_norm_px'] is not None]
    assert converged and np.median([r['local_shift_norm_px'] for r in converged]) < 1.
    assert report['per_window']['windows_total'] == 49 and report['per_window']['grid'] == [7, 7]
    assert set(report['per_window']['by_capture']) == {'a2025', 'b2023', 'c2021'}
    # Content address binds the saved image, the sorted accepted ids and THIS module (never scene_stack's id space).
    expected_id = hashlib.sha256(json.dumps([scene['image_sha256'], ['a2025', 'b2023'], sls.CODE_SHA256], sort_keys=True).encode()).hexdigest()[:16]
    assert report['stack_id'] == expected_id != scene_stack.stack_id(scene['image_sha256'], ['a2025', 'b2023'])
    assert json.loads((folder/'report.json').read_text())['stack_id'] == report['stack_id']
    assert report['gates']['min_response'] == .25 and report['gates']['max_shift_px'] == 14
    assert report['gates']['neighbour_tolerance_px'] == 2.5 and report['gates']['min_coverage'] == .35
    assert report['gates']['min_windows'] == 6 and report['gates']['holdout_residual_px_max'] == 2.
    # Nothing outside tmp_path; production scene bytes unchanged; report is plain JSON.
    assert _snapshot(tmp_path/'data/scenes'/SCENE) == before and _repo_stacks() == repo_before
    assert json.loads(json.dumps(report)) == report


def test_contract_is_consumed_unchanged_by_scene_stack_views_multidate_agreement_and_eyes_experiments(tmp_path, monkeypatch):
    base = _scene_image()
    scene = _write_scene(tmp_path, base)
    images, _ = _two_good_dates(base)
    _patch_fetchers(monkeypatch, [_record('a2025', '2025-11-06'), _record('b2023', '2023-12-20', 'Maxar')], images)
    report = sls.build(SCENE, tmp_path, **KNOBS)
    assert report['status'] == 'available'

    views = scene_stack.views(report['stack_id'], tmp_path)
    assert [name for name, _ in views] == ['stack-median', 'edge-consensus']
    for _, array in views:
        assert array.dtype == np.uint8 and array.shape == (SIZE, SIZE)
    assert np.array_equal(dict(views)['edge-consensus'], dict(scene_stack.views(report, tmp_path))['edge-consensus'])

    loaded, aligned = mda.load_stack(tmp_path, report['stack_id'])
    assert loaded['scene_id'] == SCENE and loaded['grid_reference'] == 'saved-png' and loaded['status'] == 'available'
    assert loaded['source_image_sha256'] == scene['image_sha256'] and loaded['stack_version'] == 'scene-local-stack-v1'
    assert [d['date'] for d in loaded['dates']] == ['2025-11-06', '2023-12-20']
    assert all(d['registration']['accepted'] for d in loaded['dates'])
    assert [v['image'].shape for v in aligned] == [(SIZE, SIZE, 3)]*2 and all(v['valid'].dtype == bool for v in aligned)
    records = mda.date_records(loaded)
    assert [r['status'] for r in records] == ['accepted', 'accepted']
    assert all(r['residual_px'] is not None and r['residual_px'] <= 2. for r in records)   # holdout_residual_px is read
    summary = mda.run(tmp_path, SCENE, report['stack_id'], dry_run=True)
    assert summary['status'] == 'ok' and summary['dates'] == ['saved', '2025-11-06', '2023-12-20'] and summary['skipped_dates'] == []
    assert summary['rows'] == len(OWNED) and summary['stack_status'] == 'available'
    assert summary['boundary_edge_consensus_median'] is not None and summary['dates_with_support_histogram']
    assert not (tmp_path/'data/reviews').exists()

    from scripts.ml import eyes_experiments
    inputs = eyes_experiments.default_stack_loader(tmp_path, SCENE)
    assert inputs['stack_id'] == report['stack_id'] and [v[0] for v in inputs['views']] == ['stack-median', 'edge-consensus']
    assert inputs['median'].shape == (SIZE, SIZE, 3) and inputs['registration']['status'] == 'available'
    assert inputs['registration']['captures'][0]['accepted'] is True
    # registration_summary reads scene_stack's key names: the local report carries the same facts under them too.
    for capture, summary in zip(report['captures'], inputs['registration']['captures'], strict=True):
        reg = capture['registration']
        assert reg['median_residual_px'] == reg['holdout_residual_px'] and reg['shift_px'] == reg['median_shift_px']
        assert summary['median_residual_px'] == reg['holdout_residual_px'] and summary['shift_px'] == reg['median_shift_px']
        assert summary['coverage'] == reg['coverage'] and summary['accepted'] is True


def test_scrambled_date_is_rejected_and_fewer_than_three_views_is_not_available(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    dx, dy = _field()
    images = {'a': _locally_warped(base, dx, dy, gamma=1.2), 'c': _scrambled(base)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('c', '2021-03-01')], images)

    report = sls.build(SCENE, tmp_path, **KNOBS)

    assert report['status'] == 'not_available' and report['n_accepted'] == 1 and report['n_views'] == 2
    assert '2' in report['reason'] and report['assets'] == {} and report['stack'] is None
    a, c = _capture(report, 'a'), _capture(report, 'c')
    assert a['registration']['accepted'] and a['status'] == 'accepted'
    assert not c['registration']['accepted'] and c['status'] == 'rejected' and c['registration']['windows_accepted'] == 0
    assert c['combined_fraction'] is None
    folder = tmp_path/scene_stack.STACKS/report['stack_id']
    assert sorted(p.name for p in folder.iterdir()) == ['report.json']
    assert scene_stack.views(report['stack_id'], tmp_path) == []
    assert report['per_roof_local'] and {r['capture'] for r in report['per_roof_local']} == {'a'}


def test_discovery_failure_duplicate_pixels_and_download_failure_follow_scene_stack(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    _no_network(monkeypatch)

    def offline(scene, cache, count=3):
        raise requests.ConnectionError('name resolution failed')
    monkeypatch.setattr(sls, 'discover_acquisitions', offline)
    report = sls.build(SCENE, tmp_path, **KNOBS)
    assert report['status'] == 'unavailable' and report['captures'] == [] and report['assets'] == {}
    assert 'name resolution failed' in report['discovery']['error']

    dx, dy = _field()
    same = _locally_warped(base, dx, dy)
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('b', '2025-06-01'), _record('x', '2024-01-01')],
                    {'a': same, 'b': same.copy()})
    good = sls.fetch_capture

    def flaky(scene, record, cache):
        if record['acquisition_id'] == 'x':
            raise requests.HTTPError('503 Service Unavailable')
        return good(scene, record, cache)
    monkeypatch.setattr(sls, 'fetch_capture', flaky)
    report = sls.build(SCENE, tmp_path, **KNOBS)
    assert [c['status'] for c in report['captures']] == ['accepted', 'duplicate', 'failed']
    assert 'Repeated source pixels' in _capture(report, 'b')['registration']['reason']
    assert '503' in _capture(report, 'x')['registration']['reason']
    assert report['status'] == 'not_available' and report['n_accepted'] == 1

    monkeypatch.setattr(sls, 'discover_acquisitions', imagery_dates.discover)   # the real discovery, empty cache
    report = sls.build(SCENE, tmp_path, fetch=False, **KNOBS)
    assert report['status'] == 'unavailable' and 'fetch=False' in report['discovery']['error']
    assert not any((tmp_path/'data/roof-review-loop/multidate/cache').glob('*.json'))


def test_window_shifts_follow_the_remap_convention_and_reject_neighbour_outliers():
    base = _scene_image()
    moved = np.roll(np.roll(base, 3, axis=1), 2, axis=0)          # content 3 px right, 2 px down in the capture
    saved_signal, capture_signal = sls.matching_signal(base), sls.matching_signal(moved)
    rows = sls.window_shifts(saved_signal, capture_signal, window_px=128, stride_px=64, min_response=.25,
                             max_shift_px=14., neighbour_tolerance_px=2.5)
    assert len(rows) == 49 and sum(r['accepted'] for r in rows) >= 40
    accepted = np.array([[r['dx'], r['dy']] for r in rows if r['accepted']])
    assert np.allclose(np.median(accepted, axis=0), [3, 2], atol=.15)
    dx, dy, coverage = sls.dense_field(rows, base.shape[:2], 128)
    assert coverage.mean() > .9 and np.nanmedian(dx[coverage]) == pytest.approx(3, abs=.15)
    aligned, footprint, valid = sls.warp_capture(moved, dx, dy, coverage)
    assert aligned.shape == base.shape and valid.dtype == bool and footprint.dtype == bool
    assert np.abs(aligned[valid].astype(int) - base[valid].astype(int)).mean() < 2
    assert valid.sum() < (coverage & footprint).sum()        # eroded by 3 px against warp-border artefacts
    holdout, residuals = sls.holdout_residual(rows)
    assert holdout < .5 and len(residuals) == sum(r['accepted'] for r in rows)

    # Neighbour outlier rejection on hand-built rows: a 5 x 5 grid at (2, 1) with one deviant and one isolated window.
    grid = [{'row': r, 'col': c, 'centre_px': [64 + 64*c, 64 + 64*r], 'dx': 2., 'dy': 1., 'shift_px': 5**.5,
             'response': .6, 'accepted': True, 'reason': 'ok', 'code': 'ok', 'neighbour_deviation_px': None}
            for r in range(5) for c in range(5)]
    deviant = next(g for g in grid if (g['row'], g['col']) == (2, 2))
    deviant.update(dx=9., dy=1., shift_px=9.06)
    for g in grid:
        if g['row'] == 0 and g['col'] in (0, 1):
            g.update(accepted=False, reason='response 0.1 below 0.25', code='low_response')
        if g['row'] == 1 and g['col'] in (0, 1):
            g.update(accepted=False, reason='response 0.1 below 0.25', code='low_response')
    corner = next(g for g in grid if (g['row'], g['col']) == (0, 0))
    corner.update(accepted=True, reason='ok', code='ok')     # accepted, but every neighbour is rejected
    sls.reject_outliers(grid, 2.5)
    assert not deviant['accepted'] and deviant['code'] == 'neighbour_outlier' and 'neighbours' in deviant['reason']
    assert deviant['neighbour_deviation_px'] == pytest.approx(7., abs=.01)
    assert not corner['accepted'] and corner['code'] == 'isolated'
    assert sum(g['accepted'] for g in grid) == 25 - 3 - 1 - 1   # minus 3 low-response, the isolated corner, the deviant
    # Coverage: only pixels within 1.5 window radii of an accepted centre are interpolated.
    sparse = [dict(g, accepted=(g['row'], g['col']) == (4, 4)) for g in grid]
    dx, dy, coverage = sls.dense_field(sparse, (512, 512), 128)
    assert coverage[320, 320] and not coverage[0, 0] and np.isnan(dx[0, 0]) and dx[320, 320] == pytest.approx(2.)
    assert sls.holdout_residual(sparse) == (None, [])
    with pytest.raises(ValueError):
        sls.window_shifts(saved_signal, capture_signal[:-1], window_px=128, stride_px=64, min_response=.25,
                          max_shift_px=14., neighbour_tolerance_px=2.5)


def test_enhanced_median_is_a_display_composite_with_saved_pixels_where_views_are_missing():
    base = _scene_image()
    brighter = np.clip(base.astype(int) + (12, 6, 0), 0, 255).astype(np.uint8)
    darker = np.clip(base.astype(int) - 10, 0, 255).astype(np.uint8)
    darker[100:140, 100:140] = (250, 250, 250)               # a repainted roof on one date: the median ignores it
    valid_b = np.ones(base.shape[:2], bool)
    valid_b[:40] = False
    composite, info = sls.enhanced_median_rgb(base, [brighter, darker], [np.ones(base.shape[:2], bool), valid_b])
    assert composite.dtype == np.uint8 and composite.shape == base.shape
    assert np.array_equal(composite[:40], base[:40])           # only two views there: saved pixels, no invention
    assert np.abs(composite[110:130, 110:130].astype(int) - base[110:130, 110:130].astype(int)).max() <= 2
    assert np.abs(composite[200:230, 20:60].astype(int) - base[200:230, 20:60].astype(int)).max() <= 2
    assert info['note'] == 'display/review view; not an evidence image; no resolution increase; no inpainting'
    assert info['min_views'] == 3 and info['composited_fraction'] == pytest.approx(1 - 40/512, abs=1e-3)
    assert np.allclose(info['offsets'][0], [-12, -6, 0], atol=1.5) and np.allclose(info['offsets'][1], [10, 10, 10], atol=1.5)
    with pytest.raises(ValueError):
        sls.enhanced_median_rgb(base, [brighter[:-1]], [np.ones(base.shape[:2], bool)])


def test_cli_prints_compact_json_without_window_rows(tmp_path, monkeypatch, capsys):
    base = _scene_image()
    _write_scene(tmp_path, base)
    images, _ = _two_good_dates(base)
    _patch_fetchers(monkeypatch, [_record('a2025', '2025-11-06'), _record('b2023', '2023-12-20')], images)

    code = sls.main([SCENE, '--root', str(tmp_path), '--window-px', '128', '--stride-px', '64', '--no-fetch'])

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['status'] == 'available' and printed['version'] == 'scene-local-stack-v1'
    assert 'per_roof_local' not in printed and printed['per_roof_local_summary']['rows'] == len(OWNED)*2
    for capture in printed['captures']:
        assert 'windows' not in capture and 'transform' not in (capture['registration_global'] or {})
        assert capture['registration']['method'] == 'local-phase-correlation-v1'
    assert printed['per_window']['windows_total'] == 49
    assert len(json.dumps(printed)) < 40000


def test_build_refuses_bad_scene_ids_and_changed_source_pixels(tmp_path, monkeypatch):
    base = _scene_image()
    scene = _write_scene(tmp_path, base)
    _no_network(monkeypatch)
    with pytest.raises(ValueError):
        sls.build('../etc', tmp_path)
    with pytest.raises(ValueError, match='window_px'):
        sls.build(SCENE, tmp_path, window_px=8)
    with pytest.raises(ValueError, match='stride_px'):
        sls.build(SCENE, tmp_path, window_px=128, stride_px=0)
    png = tmp_path/'data/scenes'/SCENE/f'{scene["image_sha256"]}.png'
    png.write_bytes(_png(np.roll(base, 1, axis=0)))
    with pytest.raises(ValueError, match='identity'):
        sls.build(SCENE, tmp_path)


def _full_grid(dx=2., dy=1.):
    return [{'row': r, 'col': c, 'centre_px': [64 + 64*c, 64 + 64*r], 'dx': dx, 'dy': dy, 'shift_px': float(np.hypot(dx, dy)),
             'response': .6, 'accepted': True, 'reason': 'ok', 'code': 'ok', 'neighbour_deviation_px': None}
            for r in range(7) for c in range(7)]


def test_non_finite_or_missing_shifts_never_vote_interpolate_or_become_valid():
    base = _scene_image()
    rows = _full_grid()
    rows[24]['dx'] = float('nan')                 # an accepted row with a NaN shift (hand-built or a foreign producer)
    rows[10]['dy'] = None
    rows[30]['dx'] = np.float32(2.)               # numpy scalars are ordinary finite shifts
    sls.reject_outliers(rows, 2.5)
    assert not rows[24]['accepted'] and rows[24]['code'] == 'no_peak' and rows[24]['dx'] is None and rows[24]['shift_px'] is None
    assert not rows[10]['accepted'] and rows[10]['code'] == 'no_peak'
    assert rows[30]['accepted'] and sum(r['accepted'] for r in rows) == 47
    # dense_field / holdout_residual handed rows that still carry NaN: the NaN window is ignored, the field stays finite.
    raw = _full_grid()
    raw[24]['dx'] = float('nan')
    dx, dy, coverage = sls.dense_field(raw, (512, 512), 128)
    assert coverage.mean() > .9 and np.isfinite(dx[coverage]).all() and np.isfinite(dy[coverage]).all()
    assert np.allclose(dx[coverage], 2.) and np.allclose(dy[coverage], 1.)
    holdout, residuals = sls.holdout_residual(raw)
    assert holdout == 0 and len(residuals) == 48
    # warp_capture never samples through a non-finite shift, even when the field itself carries one.
    dx[200:260, 200:260] = np.nan
    aligned, footprint, valid = sls.warp_capture(base, dx, dy, coverage)
    assert not footprint[200:260, 200:260].any() and not valid[200:260, 200:260].any()
    assert (aligned[200:260, 200:260] == 0).all() and valid[300:400, 300:400].all()
    assert np.abs(aligned[valid].astype(int) - np.roll(np.roll(base, -2, axis=1), -1, axis=0)[valid].astype(int)).mean() < 2


def test_numpy_integer_knobs_are_accepted_and_reported_as_plain_ints(tmp_path, monkeypatch):
    base = _scene_image()
    grid = sls.window_grid((512, 512), np.int64(128), np.int32(64))
    assert len(grid) == 49 and all(type(v) is int for cell in grid for v in cell)
    for bad in (128., '128', True, np.float64(128)):
        with pytest.raises(ValueError, match='window_px'):
            sls.window_grid((512, 512), bad, 64)
    with pytest.raises(ValueError, match='stride_px'):
        sls.window_grid((512, 512), 128, np.int64(0))
    result = sls.register_local(base, np.roll(base, 3, axis=1), window_px=np.int64(128), stride_px=np.int64(64))
    assert type(result['registration']['window_px']) is int and result['registration']['window_px'] == 128
    assert result['registration']['accepted'] and json.loads(json.dumps(result['registration']))['stride_px'] == 64
    _write_scene(tmp_path, base)
    images, _ = _two_good_dates(base)
    _patch_fetchers(monkeypatch, [_record('a2025', '2025-11-06'), _record('b2023', '2023-12-20')], images)
    report = sls.build(SCENE, tmp_path, window_px=np.int64(128), stride_px=np.int64(64))
    assert report['status'] == 'available' and report['gates']['window_px'] == 128 and report['per_window']['stride_px'] == 64
    assert json.loads(json.dumps(report)) == report
    with pytest.raises(ValueError, match='window_px'):
        sls.build(SCENE, tmp_path, window_px=128., stride_px=64)


def test_build_rejects_a_capture_that_is_not_on_the_saved_scene_grid(tmp_path, monkeypatch):
    base = _scene_image()
    _write_scene(tmp_path, base)
    dx, dy = _field()
    images = {'a': _locally_warped(base, dx, dy), 'small': np.ascontiguousarray(base[:400, :400])}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('small', '2024-05-05', 'Maxar')], images)

    report = sls.build(SCENE, tmp_path, **KNOBS)

    small = _capture(report, 'small')
    assert small['status'] == 'rejected' and small['registration']['reason'] == 'Capture is not on the saved scene grid'
    assert small['registration']['accepted'] is False and small['registration']['method'] == 'local-phase-correlation-v1'
    assert small['windows'] == [] and small['registration_global'] is None and small['pixel_sha256'] is None
    assert report['per_window']['by_capture']['small'] == {'windows_total': 0, 'windows_accepted': 0, 'rejections': None}
    assert report['status'] == 'not_available' and report['n_accepted'] == 1
    assert _capture(report, 'a')['status'] == 'accepted'
    loaded, _ = mda.load_stack(tmp_path, report['stack_id'])
    assert [r['status'] for r in mda.date_records(loaded)] == ['accepted', 'rejected']
    assert json.loads(json.dumps(report)) == report


PAIR_NOTE = ('two-view pair composite for display/review; not an evidence image; no median (needs 3 views); '
             'no resolution increase; no inpainting')


def test_allow_pair_emits_a_two_view_pair_stack_and_never_available(tmp_path, monkeypatch):
    base = _scene_image()
    scene = _write_scene(tmp_path, base)
    dx, dy = _field()
    images = {'a': _locally_warped(base, dx, dy, gamma=1.2), 'c': _scrambled(base)}
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('c', '2021-03-01')], images)

    default = sls.build(SCENE, tmp_path, **KNOBS)           # option absent: the honest negative is unchanged
    assert default['status'] == 'not_available' and default['assets'] == {} and default['gates']['min_accepted'] == 2
    assert sls.stack_mode(default) is None and sls.views(default['stack_id'], tmp_path) == []
    report = sls.build(SCENE, tmp_path, min_accepted=1, **KNOBS)

    assert report['status'] == 'pair' and report['n_accepted'] == 1 and report['n_views'] == 2
    assert report['gates']['min_accepted'] == 1 and 'pair' in report['reason'] and sls.stack_mode(report) == 'pair'
    # The content address is the same rule (saved image + sorted accepted ids + this module): the pair report
    # replaces the not_available report for the same accepted set instead of forking a second folder.
    assert report['stack_id'] == default['stack_id'] == sls.stack_id(scene['image_sha256'], ['a'])
    expected = {'aligned-0.png', 'valid-0.png', 'shift-field-0.png', 'edge-consensus.png', 'valid-count.png',
                'enhanced-pair-rgb.png'}
    assert set(report['assets']) == expected
    assert not {'stack-median.png', 'stack-min.png', 'stack-max.png', 'disagreement.png', 'enhanced-median-rgb.png'} & set(report['assets'])
    folder = tmp_path/scene_stack.STACKS/report['stack_id']
    assert sorted(p.name for p in folder.iterdir()) == sorted([*expected, 'report.json'])
    for name, asset in report['assets'].items():
        data = (folder/name).read_bytes()
        assert asset['path'] == name and hashlib.sha256(data).hexdigest() == asset['sha256'], name
        assert Image.open(io.BytesIO(data)).size == (SIZE, SIZE)
    aligned = np.asarray(Image.open(folder/'aligned-0.png'))
    valid = np.asarray(Image.open(folder/'valid-0.png')) > 127
    count = np.asarray(Image.open(folder/'valid-count.png'))
    consensus = np.asarray(Image.open(folder/'edge-consensus.png'))
    enhanced = np.asarray(Image.open(folder/'enhanced-pair-rgb.png'))
    # The accepted date is exactly what register_local produces; valid-count is saved (1) + the date where valid.
    local = sls.register_local(base, images['a'], **KNOBS)
    assert np.array_equal(aligned, local['aligned']) and np.array_equal(valid, local['valid']) and valid.mean() > .8
    assert count.dtype == np.uint8 and count.min() == 1 and count.max() == 2 and np.array_equal(count == 2, valid)
    # Edge consensus over the two views reuses scene_stack.edge_consensus with the eroded mask and 3 px border exclusion.
    expected_consensus = scene_stack.edge_consensus(
        [Image.fromarray(base), Image.fromarray(local['aligned'])],
        [np.ones((SIZE, SIZE), bool), scene_stack.edge_mask(local['valid'], local['footprint'])])
    assert np.array_equal(consensus, expected_consensus) and consensus.max() == 2
    saved_edges = scene_stack.edge_map(Image.fromarray(base))
    assert np.array_equal(consensus[~valid], saved_edges[~valid]) and (consensus == 2).sum() > .3*saved_edges[valid].sum()
    # The composite: mean of saved and the brightness-normalised date where the date is valid, saved pixels elsewhere.
    assert enhanced.shape == (SIZE, SIZE, 3) and np.array_equal(enhanced[~valid], base[~valid])
    saved_gray = cv2.cvtColor(base, cv2.COLOR_RGB2GRAY).astype(float)
    aligned_gray = cv2.cvtColor(aligned, cv2.COLOR_RGB2GRAY).astype(float)
    offset = float(np.median(saved_gray[valid] - aligned_gray[valid]))
    mean = (base.astype(float) + np.clip(aligned.astype(float) + offset, 0, 255))/2
    assert np.abs(enhanced[valid].astype(float) - mean[valid]).max() <= 1
    assert offset > 3, 'the gamma-darkened date is lifted to the saved median gray before the mean'
    stack = report['stack']
    assert stack['views'] == 2 and stack['mode'] == 'pair' and stack['registration_method'] == 'local-phase-correlation-v1'
    assert stack['combined_fraction'] == pytest.approx(valid.mean(), abs=1e-3)
    assert stack['edge_consensus_fraction_both'] == pytest.approx((consensus == 2).mean(), abs=1e-3)
    assert stack['edge_consensus_fraction_any'] == pytest.approx((consensus >= 1).mean(), abs=1e-3)
    assert stack['enhanced_pair_rgb']['note'] == PAIR_NOTE and stack['enhanced_pair_rgb']['views'] == 2
    assert stack['enhanced_pair_rgb']['composited_fraction'] == pytest.approx(valid.mean(), abs=1e-3)
    assert stack['enhanced_pair_rgb']['offset_gray'] == pytest.approx(offset, abs=.51)
    assert not {'disagreement_median', 'disagreement_p90', 'full_fraction', 'enhanced_median_rgb'} & set(stack)
    assert '3 views' in stack['note'] and 'pair' in report['note'] and 'evidence' in report['note']
    assert {r['capture'] for r in report['per_roof_local']} == {'a'} and _capture(report, 'c')['status'] == 'rejected'
    assert json.loads((folder/'report.json').read_text())['status'] == 'pair' and json.loads(json.dumps(report)) == report
    # Views for the line fitter: edge consensus scaled 0..255 by the two views and the aligned date's gray masked to
    # valid; never a 'stack-median' (needs 3 views). scene_stack.views itself stays untouched: pair is not 'available'.
    views = sls.pair_views(report, tmp_path)
    assert [n for n, _ in views] == ['edge-consensus', 'aligned-date-2025-11-06']
    scaled, gray = dict(views)['edge-consensus'], dict(views)['aligned-date-2025-11-06']
    assert scaled.dtype == np.uint8 and set(np.unique(scaled)) <= {0, 128, 255} and scaled.max() == 255
    assert np.array_equal(scaled, np.rint(consensus.astype(float)*255/2).astype(np.uint8))
    assert gray.dtype == np.uint8 and gray.shape == (SIZE, SIZE) and (gray[~valid] == 0).all()
    assert np.array_equal(gray[valid], cv2.cvtColor(aligned, cv2.COLOR_RGB2GRAY)[valid])
    dispatched = sls.views(report['stack_id'], tmp_path)
    assert [n for n, _ in dispatched] == [n for n, _ in views]
    assert all(np.array_equal(a, b) for (_, a), (_, b) in zip(dispatched, views, strict=True))
    assert scene_stack.views(report, tmp_path) == []       # the same folder now holds the pair report (one content address)
    # A tampered asset is refused.
    Image.fromarray(np.zeros((SIZE, SIZE), np.uint8)).save(folder/'edge-consensus.png')
    with pytest.raises(ValueError, match='sha256'):
        sls.pair_views(report['stack_id'], tmp_path)
    with pytest.raises(ValueError, match='min_accepted'):
        sls.build(SCENE, tmp_path, min_accepted=0, **KNOBS)
    with pytest.raises(ValueError, match='min_accepted'):
        sls.build(SCENE, tmp_path, min_accepted=1.5, **KNOBS)


def test_allow_pair_leaves_two_accepted_dates_available_and_the_cli_flag_maps_to_min_accepted(tmp_path, monkeypatch, capsys):
    base = _scene_image()
    _write_scene(tmp_path, base)
    images, _ = _two_good_dates(base)
    _patch_fetchers(monkeypatch, [_record('a2025', '2025-11-06'), _record('b2023', '2023-12-20')], images)

    report = sls.build(SCENE, tmp_path, min_accepted=1, **KNOBS)

    assert report['status'] == 'available' and report['n_views'] == 3 and 'mode' not in report['stack']
    assert set(report['assets']) == {*sls.STACK_ASSETS, 'aligned-0.png', 'valid-0.png', 'aligned-1.png', 'valid-1.png',
                                     'shift-field-0.png', 'shift-field-1.png'}
    assert report['gates']['min_accepted'] == 1 and sls.stack_mode(report) == 'stack'
    dispatched = sls.views(report, tmp_path)
    assert [n for n, _ in dispatched] == ['stack-median', 'edge-consensus']
    assert all(np.array_equal(a, b) for (_, a), (_, b) in zip(dispatched, scene_stack.views(report, tmp_path), strict=True))
    assert sls.pair_views(report, tmp_path) == []

    dx, dy = _field()
    _patch_fetchers(monkeypatch, [_record('a', '2025-11-06'), _record('c', '2021-03-01')],
                    {'a': _locally_warped(base, dx, dy, gamma=1.2), 'c': _scrambled(base)})
    code = sls.main([SCENE, '--root', str(tmp_path), '--window-px', '128', '--stride-px', '64', '--no-fetch', '--allow-pair'])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['status'] == 'pair' and printed['stack']['mode'] == 'pair' and printed['gates']['min_accepted'] == 1
    assert set(printed['assets']) == {'aligned-0.png', 'valid-0.png', 'shift-field-0.png', 'edge-consensus.png',
                                      'valid-count.png', 'enhanced-pair-rgb.png'}
    assert 'per_roof_local' not in printed and all('windows' not in c for c in printed['captures'])
    code = sls.main([SCENE, '--root', str(tmp_path), '--window-px', '128', '--stride-px', '64', '--no-fetch'])
    assert code == 0 and json.loads(capsys.readouterr().out)['status'] == 'not_available'


def test_enhanced_pair_rgb_is_a_mean_of_two_views_with_saved_pixels_where_the_date_is_invalid():
    base = _scene_image()
    darker = np.clip(base.astype(int) - 20, 0, 255).astype(np.uint8)
    darker[100:140, 100:140] = 250                 # a repainted roof on the date: a two-view mean cannot ignore it
    valid = np.ones(base.shape[:2], bool)
    valid[:40] = False
    composite, info = sls.enhanced_pair_rgb(base, darker, valid)
    assert composite.dtype == np.uint8 and composite.shape == base.shape
    assert np.array_equal(composite[:40], base[:40]), 'no date there: saved pixels, no invention'
    assert np.abs(composite[200:230, 20:60].astype(int) - base[200:230, 20:60].astype(int)).max() <= 1
    assert composite[110:130, 110:130].mean() > base[110:130, 110:130].mean() + 20, 'a mean, not a median'
    assert info['offset_gray'] == pytest.approx(20, abs=1.5) and info['views'] == 2 and info['note'] == PAIR_NOTE
    assert info['composited_fraction'] == pytest.approx(1 - 40/512, abs=1e-3) and 'mean' in info['rule']
    with pytest.raises(ValueError):
        sls.enhanced_pair_rgb(base, darker[:-1], valid)
    with pytest.raises(ValueError):
        sls.enhanced_pair_rgb(base, darker, valid[:-1])
    empty, info = sls.enhanced_pair_rgb(base, darker, np.zeros(base.shape[:2], bool))
    assert np.array_equal(empty, base) and info['offset_gray'] == 0. and info['composited_fraction'] == 0.
