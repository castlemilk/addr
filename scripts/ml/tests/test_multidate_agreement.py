"""multidate_agreement: detector-free edge/ridge consistency across registered dates (not accuracy)."""
import hashlib
import io
import json
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import eyes_index
from scripts.ml import multidate_agreement as mda

P2M = [[.25, 0], [0, -.25], [-32, 32]]
M2P = [[4, 0], [0, -4], [128, 128]]
RECT_PX = [[64, 80], [192, 80], [192, 176], [64, 176]]        # 128 x 96 px ring, perimeter 448 px
SMALL_PX = [[20, 200], [60, 200], [60, 240], [20, 240]]
# Ridge at local x=0 -> pixel column 128 (the brightness step); hip on a blank stretch (pixel row 128, x 80..112).
LINES = [{'line_meters': [[0, -10], [0, 10]], 'kind': 'ridge'}, {'line_meters': [[-12, 0], [-4, 0]], 'kind': 'hip'}]


def saved_pixels(offset=0):
    pixels = np.full((256, 256, 3), 60, np.uint8)
    pixels[80:176, 64:128] = 200
    pixels[80:176, 128:192] = 150
    return np.clip(pixels.astype(int)+offset, 0, 255).astype(np.uint8)


def occluded_pixels():
    pixels = saved_pixels()
    pixels[60:200, 120:220] = 60           # the east side (and the ridge) hidden on this date
    return pixels


def _png(array):
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format='PNG')
    return buffer.getvalue()


def _building(object_id, ring, lines=None):
    return {'id': object_id, 'kind': 'building', 'status': 'candidate', 'ring': ring, 'model': {'height_m': 3.2},
            **({'roof_lines': lines} if lines else {})}


def scene_fixture():
    image = saved_pixels()
    objects = [_building('b1', RECT_PX, LINES), _building('b2', SMALL_PX),
               {'id': 't1', 'kind': 'tree', 'status': 'candidate', 'ring': SMALL_PX, 'model': {}}]
    scene = {'id': 'synthetic-multidate', 'title': 'synthetic', 'schema_version': 1, 'model_signature': 'sig-md-1',
             'image_sha256': hashlib.sha256(_png(image)).hexdigest(), 'image_width': 256, 'image_height': 256,
             'pixel_to_meters': P2M, 'meters_to_pixel': M2P, 'centroid': {'lon': 145.31, 'lat': -37.79},
             'objects': objects, 'owned_object_ids': ['b1', 't1']}
    return scene, Image.fromarray(image)


def stack_fixture():
    half = np.ones((256, 256), bool)
    half[:, :128] = False
    report = {'stack_id': 'stack-fixture', 'scene_id': 'synthetic-multidate', 'grid_reference': 'saved-png',
              'dates': [{'date': '2025-11-06', 'status': 'accepted', 'registration': {'check_median_residual_px': 1.2}},
                        {'date': '2023-12-20', 'status': 'accepted', 'registration': {'check_median_residual_px': 1.7}},
                        {'date': '2025-09-28', 'status': 'rejected', 'registration': {'check_median_residual_px': 3.4}},
                        {'date': '2024-06-01', 'status': 'accepted', 'registration': {'check_median_residual_px': .9}},
                        {'date': '2021-01-01', 'status': 'accepted', 'registration': {'check_median_residual_px': 1.}}]}
    views = [{'date': '2025-11-06', 'image': saved_pixels(10), 'valid': np.ones((256, 256), bool)},
             {'date': '2023-12-20', 'image': occluded_pixels(), 'valid': np.ones((256, 256), bool)},
             {'date': '2025-09-28', 'image': saved_pixels(), 'valid': np.ones((256, 256), bool)},
             {'date': '2024-06-01', 'image': saved_pixels(), 'valid': half},
             {'date': '2022-01-01', 'image': saved_pixels(), 'valid': None},
             {'date': '2021-01-01', 'image': np.zeros((64, 64), np.uint8), 'valid': None}]
    return report, views


def test_rows_cover_owned_buildings_only_with_versioned_payloads():
    scene, image = scene_fixture()
    report, views = stack_fixture()
    rows = mda.assess_scene(scene, image, report, views)
    assert [r['object_id'] for r in rows] == ['b1']
    row = rows[0]
    assert row['scene_id'] == 'synthetic-multidate' and row['model_signature'] == 'sig-md-1' and row['version'] == mda.VERSION
    payload = row['payload']
    assert payload['version'] == mda.VERSION == 'multidate-agreement-v1'
    assert payload['code_sha256'] == hashlib.sha256(Path(mda.__file__).read_bytes()).hexdigest()
    assert payload['computed_at'] and payload['note'] == 'consistency across dates on a shared orthomosaic grid; not accuracy'
    assert payload['stack_id'] == 'stack-fixture' and payload['image_sha256'] == scene['image_sha256']
    assert json.dumps(payload)  # plain JSON, no numpy scalars


def test_edge_support_by_date_marks_the_occluded_side_and_masks_invalid_samples():
    scene, image = scene_fixture()
    report, views = stack_fixture()
    payload = mda.assess_scene(scene, image, report, views)[0]['payload']
    assert payload['dates'] == ['saved', '2025-11-06', '2023-12-20', '2024-06-01']
    assert {s['date']: s['reason'] for s in payload['skipped_dates']} == {
        '2025-09-28': 'rejected in stack report', '2022-01-01': 'not in stack report', '2021-01-01': 'grid mismatch'}
    support = payload['edge_support_by_date']
    assert support['saved']['per_edge'] == [1., 1., 1., 1.] and support['saved']['support'] == 1.
    assert support['2025-11-06']['support'] == 1. and support['2025-11-06']['valid_fraction'] == 1.
    occluded = support['2023-12-20']
    assert occluded['per_edge'][1] == 0. and occluded['per_edge'][3] == 1.
    assert .35 < occluded['per_edge'][0] < .5 and .35 < occluded['per_edge'][2] < .5
    assert .4 < occluded['support'] < .5
    half = support['2024-06-01']
    assert .45 < half['valid_fraction'] < .55 and half['support'] == 1., 'invalid samples are excluded, not counted as misses'
    assert half['per_edge'][3] is None, 'an edge with no valid sample reports None'
    assert payload['dates_with_support'] == 3 and payload['support_threshold'] == .5
    assert payload['registration_residual_px'] == {'saved': 0., '2025-11-06': 1.2, '2023-12-20': 1.7, '2024-06-01': .9}


def test_boundary_edge_consensus_counts_the_saved_image_as_a_date():
    scene, image = scene_fixture()
    report, views = stack_fixture()
    full = mda.assess_scene(scene, image, report, views)[0]['payload']
    assert full['boundary_edge_consensus'] == 1., 'saved + one clean date already agree everywhere'
    only_occluded = {**report, 'dates': [report['dates'][1]]}
    partial = mda.assess_scene(scene, image, only_occluded, [views[1]])[0]['payload']
    assert partial['dates'] == ['saved', '2023-12-20']
    assert abs(partial['boundary_edge_consensus']-partial['edge_support_by_date']['2023-12-20']['support']) < 1e-9
    assert .4 < partial['boundary_edge_consensus'] < .5
    saved_only = mda.assess_scene(scene, image, {**report, 'dates': []}, [])[0]['payload']
    assert saved_only['dates'] == ['saved'] and saved_only['boundary_edge_consensus'] == 0.
    assert saved_only['dates_with_support'] == 1 and saved_only['n_dates'] == 1


def test_roof_line_support_per_kind_and_date():
    scene, image = scene_fixture()
    report, views = stack_fixture()
    payload = mda.assess_scene(scene, image, report, views)[0]['payload']
    lines = payload['roof_line_support_by_date']
    assert lines['saved']['by_kind'] == {'ridge': 1., 'hip': 0.}
    assert lines['saved']['per_line'] == [1., 0.]
    assert lines['2023-12-20']['by_kind']['ridge'] == 0., 'the ridge is hidden on the occluded date'
    # On the half-valid date the hip lies in the invalid half and the ridge sits on the mask border
    # (pixel column 128, inside the BORDER_PX exclusion band): neither can be assessed on that date.
    assert lines['2024-06-01']['per_line'] == [None, None] and lines['2024-06-01']['by_kind'] == {'ridge': None, 'hip': None}
    # Aggregate over every stored roof line (ridge 81 samples supported + hip 33 unsupported on the saved image).
    ridge = payload['ridge_support_by_date']
    assert .7 < ridge['saved'] < .72 and ridge['saved'] == ridge['2025-11-06'] == lines['saved']['all']
    assert ridge['2023-12-20'] == 0.
    assert ridge['2024-06-01'] is None, 'no assessable roof-line sample on the half-valid date'
    assert payload['n_roof_lines'] == 2
    no_lines = {**scene, 'objects': [_building('b1', RECT_PX)], 'owned_object_ids': ['b1']}
    bare = mda.assess_scene(no_lines, image, report, views)[0]['payload']
    assert bare['n_roof_lines'] == 0 and bare['ridge_support_by_date'] == {d: None for d in bare['dates']}


def _write_scene(root, scene, image):
    folder = root/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/f"{scene['image_sha256']}.png").write_bytes(_png(np.asarray(image)))
    (folder/'latest.json').write_text(json.dumps(scene))


def _write_temporal_run(root, run_id, scene_id):
    """scene_temporal layout: report.json captures[] with index/status, aligned-<i>.png, valid-<i>.png."""
    folder = root/'data/roof-review-loop/multidate/runs'/run_id
    folder.mkdir(parents=True)
    captures = [{'index': 0, 'acquisition_date': '2025-11-06', 'status': 'accepted', 'registration': {'status': 'accepted'}},
                {'index': 1, 'acquisition_date': '2025-09-28', 'status': 'rejected', 'registration': {'check_median_residual_px': 1.6}},
                {'index': 2, 'acquisition_date': '2023-12-20', 'status': 'accepted', 'registration': {'check_median_residual_px': 1.7}}]
    (folder/'report.json').write_text(json.dumps({'run': run_id, 'scene_id': scene_id, 'status': 'complete', 'captures': captures}))
    Image.fromarray(saved_pixels(10)).save(folder/'aligned-0.png')
    Image.fromarray(np.full((256, 256), 255, np.uint8)).save(folder/'valid-0.png')
    Image.fromarray(occluded_pixels()).save(folder/'aligned-2.png')
    Image.fromarray(np.full((256, 256), 255, np.uint8)).save(folder/'valid-2.png')
    return folder


def test_run_reads_a_temporal_run_layout_and_writes_the_multidate_table(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    _write_temporal_run(tmp_path, 'a1b2c3d4e5f60718', scene['id'])
    summary = mda.run(tmp_path, scene['id'], 'a1b2c3d4e5f60718')
    assert summary['status'] == 'ok' and summary['rows'] == 1 and summary['version'] == mda.VERSION
    assert summary['dates'] == ['saved', '2025-11-06', '2023-12-20'] and summary['grid_reference'] == 'wayback-reference'
    assert summary['skipped_dates'] == [{'date': '2025-09-28', 'reason': 'rejected in stack report'}]
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = eyes_index.rows(db, 'multidate', scene_id=scene['id'])
    assert len(rows) == 1 and rows[0]['object_id'] == 'b1' and rows[0]['model_signature'] == 'sig-md-1'
    payload = rows[0]['payload']
    assert payload['version'] == mda.VERSION and payload['stack_id'] == 'a1b2c3d4e5f60718'
    assert payload['grid_reference'] == 'wayback-reference'
    assert payload['edge_support_by_date']['2023-12-20']['per_edge'][1] == 0.
    assert payload['registration_residual_px'] == {'saved': 0., '2025-11-06': None, '2023-12-20': 1.7}
    written = json.loads((tmp_path/'data/reviews/eyes/multidate'/scene['id']/'a1b2c3d4e5f60718.json').read_text())
    assert written['rows'] == 1 and written['code_sha256'] == payload['code_sha256']
    assert (tmp_path/'data/scenes'/scene['id']/'latest.json').read_text() == json.dumps(scene), 'production untouched'


def test_run_reads_a_stack_layout_and_dry_run_writes_nothing(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    folder = tmp_path/'data/roof-review-loop/multidate/stacks/stack-0001'
    folder.mkdir(parents=True)
    Image.fromarray(saved_pixels(10)).save(folder/'aligned-2025-11-06.png')
    Image.fromarray(np.full((256, 256), 255, np.uint8)).save(folder/'valid-2025-11-06.png')
    (folder/'stack.json').write_text(json.dumps({
        'stack_id': 'stack-0001', 'scene_id': scene['id'], 'grid_reference': 'saved-png',
        'dates': [{'date': '2025-11-06', 'status': 'accepted', 'registration': {'check_median_residual_px': 1.1},
                   'aligned': 'aligned-2025-11-06.png', 'valid': 'valid-2025-11-06.png'},
                  {'date': '2024-01-01', 'status': 'accepted', 'aligned': 'missing.png', 'valid': 'missing.png'}]}))
    summary = mda.run(tmp_path, scene['id'], 'stack-0001', dry_run=True)
    assert summary['status'] == 'ok' and summary['rows'] == 1 and summary['dry_run'] is True
    assert summary['dates'] == ['saved', '2025-11-06'] and summary['grid_reference'] == 'saved-png'
    assert summary['skipped_dates'] == [{'date': '2024-01-01', 'reason': 'unavailable'}]
    assert summary['registration_residual_px'] == {'saved': 0., '2025-11-06': 1.1}
    assert not eyes_index.path(tmp_path).exists() and not (tmp_path/'data/reviews').exists()


def test_missing_stack_or_foreign_scene_is_recorded_as_unavailable(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    summary = mda.run(tmp_path, scene['id'], 'a1b2c3d4e5f60718')
    assert summary['status'] == 'unavailable' and summary['rows'] == 0 and 'reason' in summary
    assert not eyes_index.path(tmp_path).exists()
    _write_temporal_run(tmp_path, 'a1b2c3d4e5f60718', 'another-scene')
    with pytest.raises(ValueError, match='scene'):
        mda.run(tmp_path, scene['id'], 'a1b2c3d4e5f60718')
    with pytest.raises(ValueError):
        mda.run(tmp_path, scene['id'], '../escape')


def test_valid_mask_border_edges_are_excluded_not_counted_as_support():
    """A registered capture is black outside its valid mask (warpAffine fill). That step is a strong Canny
    edge that is not image content: samples within BORDER_PX of an invalid pixel are excluded, never supported."""
    saved = np.full((256, 256, 3), 120, np.uint8)                        # no edges anywhere
    dated = saved.copy()
    dated[:, :128] = 0
    valid = np.zeros((256, 256), bool)
    valid[:, 128:] = True
    ring = [[128, 80], [200, 80], [200, 176], [128, 176]]                 # west side exactly on the mask border
    lines = [{'line_meters': [[0, -10], [0, 10]], 'kind': 'ridge'}]       # ridge on the border column too
    scene = {**scene_fixture()[0], 'objects': [_building('b1', ring, lines)], 'owned_object_ids': ['b1']}
    report = {'stack_id': 'st', 'grid_reference': 'saved-png',
              'dates': [{'date': 'd1', 'status': 'accepted', 'registration': {'check_median_residual_px': 1.}}]}
    payload = mda.assess_scene(scene, Image.fromarray(saved), report,
                               [{'date': 'd1', 'image': dated, 'valid': valid}])[0]['payload']
    d1 = payload['edge_support_by_date']['d1']
    assert d1['per_edge'][3] is None, 'the side on the mask border cannot be assessed on that date'
    assert d1['per_edge'][1] == 0. and d1['support'] == 0., 'no real edge on that date: nothing is supported'
    assert .6 < d1['valid_fraction'] < .75 and payload['valid_fraction_frame']['d1'] < .5
    assert payload['ridge_support_by_date'] == {'saved': 0., 'd1': None}
    assert payload['dates_with_support'] == 0 and payload['boundary_edge_consensus'] == 0.
    assert payload['border_px'] == mda.BORDER_PX == 3


def test_duplicate_undated_and_reserved_stack_entries_are_skipped():
    scene, image = scene_fixture()
    report, views = stack_fixture()
    clean = report['dates'][0]
    doubled = {**report, 'dates': [clean, dict(clean), {'status': 'accepted'}, {'date': 'saved', 'status': 'accepted'}]}
    supplied = [views[0], {'date': 'saved', 'image': saved_pixels(), 'valid': None}]
    payload = mda.assess_scene(scene, image, doubled, supplied)[0]['payload']
    assert payload['dates'] == ['saved', '2025-11-06'] and payload['n_dates'] == 2
    reasons = {(s['date'], s['reason']) for s in payload['skipped_dates']}
    assert {('2025-11-06', 'duplicate date in stack report'), (None, 'undated'), ('saved', 'reserved date name')} <= reasons
    assert payload['registration_residual_px'] == {'saved': 0., '2025-11-06': 1.2}
    # One date listed twice must not count as two agreeing dates.
    twice = {**report, 'dates': [report['dates'][1], dict(report['dates'][1])]}
    partial = mda.assess_scene(scene, image, twice, [views[1], views[1]])[0]['payload']
    assert partial['dates'] == ['saved', '2023-12-20'] and .4 < partial['boundary_edge_consensus'] < .5


def test_residual_gate_and_scene_stack_accepted_flag_are_honoured():
    scene, image = scene_fixture()
    report, views = stack_fixture()
    loose = {**report, 'dates': [{**report['dates'][0], 'registration': {'check_median_residual_px': 2.01}},
                                 {**report['dates'][1], 'registration': {'median_residual_px': 1.9}}]}
    payload = mda.assess_scene(scene, image, loose, views[:2])[0]['payload']
    assert payload['dates'] == ['saved', '2023-12-20']
    assert payload['skipped_dates'] == [{'date': '2025-11-06', 'reason': 'residual above gate'}]
    assert payload['registration_residual_px'] == {'saved': 0., '2023-12-20': 1.9}
    assert payload['residual_gate_px'] == mda.RESIDUAL_GATE_PX == 2.
    # scene_stack keeps register()'s raw status and adds its own gate result as `accepted`.
    gated = {**report, 'dates': [{'date': '2025-11-06', 'registration': {'accepted': False, 'status': 'accepted',
                                                                          'median_residual_px': 2.4}}]}
    payload = mda.assess_scene(scene, image, gated, views[:1])[0]['payload']
    assert payload['dates'] == ['saved']
    assert payload['skipped_dates'] == [{'date': '2025-11-06', 'reason': 'rejected in stack report'}]


def _write_stack_report(root, stack_id, scene_id, image_sha256, *, available=True):
    """scene_stack.build layout: stacks/<16 hex>/report.json with captures[] (date, sensor, registration
    {accepted, status, reason, median_residual_px}) and sha-recorded aligned-<index>.png / valid-<index>.png assets."""
    folder = root/'data/roof-review-loop/multidate/stacks'/stack_id
    folder.mkdir(parents=True)

    def asset(name, array):
        Image.fromarray(array).save(folder/name)
        return {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}
    captures = [{'index': 0, 'acquisition_id': 'a0', 'date': '2025-11-06', 'sensor': 'Vantor',
                 'registration': {'accepted': True, 'status': 'accepted', 'reason': 'gates passed', 'median_residual_px': 1.3}},
                {'index': 1, 'acquisition_id': 'a1', 'date': '2025-09-28', 'sensor': 'Vantor',
                 'registration': {'accepted': False, 'status': 'accepted',
                                  'reason': 'Median hold-out residual 2.4 px exceeds 2.0 px', 'median_residual_px': 2.4}},
                {'index': 2, 'acquisition_id': 'a2', 'date': '2023-12-20', 'sensor': 'Maxar',
                 'registration': {'accepted': True, 'status': 'accepted', 'reason': 'gates passed', 'median_residual_px': 1.7}}]
    assets = {}
    if available:
        assets['aligned-0.png'] = asset('aligned-0.png', saved_pixels(10))
        assets['valid-0.png'] = asset('valid-0.png', np.full((256, 256), 255, np.uint8))
        assets['aligned-2.png'] = asset('aligned-2.png', occluded_pixels())
        assets['valid-2.png'] = asset('valid-2.png', np.full((256, 256), 255, np.uint8))
    report = {'version': 'scene-stack-v1', 'stack_id': stack_id, 'status': 'available' if available else 'not_available',
              'scene_id': scene_id, 'source_image_sha256': image_sha256, 'captures': captures, 'assets': assets,
              'n_accepted': 2 if available else 0}
    (folder/'report.json').write_text(json.dumps(report))
    return folder


def test_run_reads_the_scene_stack_report_layout_and_verifies_assets(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    folder = _write_stack_report(tmp_path, 'dde125b138d603c4', scene['id'], scene['image_sha256'])
    summary = mda.run(tmp_path, scene['id'], 'dde125b138d603c4', dry_run=True)
    assert summary['status'] == 'ok' and summary['grid_reference'] == 'saved-png' and summary['stack_status'] == 'available'
    assert summary['dates'] == ['saved', '2025-11-06', '2023-12-20'] and summary['rows'] == 1
    assert summary['skipped_dates'] == [{'date': '2025-09-28', 'reason': 'rejected in stack report'}]
    assert summary['registration_residual_px'] == {'saved': 0., '2025-11-06': 1.3, '2023-12-20': 1.7}
    assert summary['stack_source_image_sha256'] == scene['image_sha256']
    report, views = mda.load_stack(tmp_path, 'dde125b138d603c4')
    payload = mda.assess_scene(scene, image, report, views)[0]['payload']
    assert payload['edge_support_by_date']['2023-12-20']['per_edge'][1] == 0.
    assert payload['stack_source_image_sha256'] == scene['image_sha256'] and payload['grid_reference'] == 'saved-png'
    Image.fromarray(saved_pixels(20)).save(folder/'aligned-0.png')
    with pytest.raises(ValueError, match='sha256'):
        mda.run(tmp_path, scene['id'], 'dde125b138d603c4', dry_run=True)


def test_scene_stack_without_accepted_captures_yields_saved_only_rows(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    _write_stack_report(tmp_path, 'dde125b138d603c4', scene['id'], scene['image_sha256'], available=False)
    summary = mda.run(tmp_path, scene['id'], 'dde125b138d603c4', dry_run=True)
    assert summary['status'] == 'ok' and summary['stack_status'] == 'not_available' and summary['dates'] == ['saved']
    assert {s['date']: s['reason'] for s in summary['skipped_dates']} == {
        '2025-11-06': 'unavailable', '2025-09-28': 'rejected in stack report', '2023-12-20': 'unavailable'}
    assert summary['dates_with_support_histogram'] == {'1': 1} and summary['boundary_edge_consensus_median'] == 0.


def test_stack_registered_to_a_different_saved_image_is_refused(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    _write_stack_report(tmp_path, 'dde125b138d603c4', scene['id'], 'f'*64)
    with pytest.raises(ValueError, match='image'):
        mda.run(tmp_path, scene['id'], 'dde125b138d603c4', dry_run=True)
    assert not eyes_index.path(tmp_path).exists() and not (tmp_path/'data/reviews').exists()


def _write_pair_stack_report(root, stack_id, scene_id, image_sha256, *, aligned=None):
    """scene_local_stack pair layout: status 'pair', ONE accepted capture with sha-recorded aligned-0/valid-0 assets
    (plus the two-view edge-consensus, valid-count and enhanced-pair-rgb assets), stack.views == 2, no stack-median."""
    folder = root/'data/roof-review-loop/multidate/stacks'/stack_id
    folder.mkdir(parents=True)

    def asset(name, array):
        Image.fromarray(array).save(folder/name)
        return {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}
    captures = [{'index': 0, 'acquisition_id': 'a0', 'date': '2025-11-06', 'sensor': 'Vantor', 'status': 'accepted',
                 'registration': {'accepted': True, 'method': 'local-phase-correlation-v1', 'status': 'accepted',
                                  'reason': '40/121 windows agree', 'holdout_residual_px': .8, 'median_residual_px': .8,
                                  'coverage': .72}},
                {'index': 1, 'acquisition_id': 'a1', 'date': '2025-09-28', 'sensor': 'Vantor', 'status': 'rejected',
                 'registration': {'accepted': False, 'method': 'local-phase-correlation-v1', 'status': 'rejected',
                                  'reason': 'valid coverage 0.08 below 0.35', 'holdout_residual_px': .76, 'median_residual_px': .76}},
                {'index': 2, 'acquisition_id': 'a2', 'date': '2023-12-20', 'sensor': 'Maxar', 'status': 'rejected',
                 'registration': {'accepted': False, 'method': 'local-phase-correlation-v1', 'status': 'rejected',
                                  'reason': 'valid coverage 0.19 below 0.35', 'holdout_residual_px': .54, 'median_residual_px': .54}}]
    assets = {'aligned-0.png': asset('aligned-0.png', occluded_pixels() if aligned is None else aligned),
              'valid-0.png': asset('valid-0.png', np.full((256, 256), 255, np.uint8)),
              'shift-field-0.png': asset('shift-field-0.png', np.zeros((256, 256, 3), np.uint8)),
              'edge-consensus.png': asset('edge-consensus.png', np.zeros((256, 256), np.uint8)),
              'valid-count.png': asset('valid-count.png', np.full((256, 256), 2, np.uint8)),
              'enhanced-pair-rgb.png': asset('enhanced-pair-rgb.png', saved_pixels())}
    report = {'version': 'scene-local-stack-v1', 'stack_id': stack_id, 'status': 'pair',
              'reason': '1 accepted capture registered window by window onto the saved scene grid; two-view pair',
              'scene_id': scene_id, 'source_image_sha256': image_sha256, 'captures': captures, 'assets': assets,
              'n_accepted': 1, 'n_views': 2, 'stack': {'views': 2, 'mode': 'pair'}}
    (folder/'report.json').write_text(json.dumps(report))
    return folder


def test_pair_stack_yields_saved_plus_one_date_rows_and_records_the_mode(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    _write_pair_stack_report(tmp_path, 'ab12ab12ab12ab12', scene['id'], scene['image_sha256'])
    summary = mda.run(tmp_path, scene['id'], 'ab12ab12ab12ab12', dry_run=True)
    assert summary['status'] == 'ok' and summary['stack_status'] == 'pair' and summary['mode'] == 'pair'
    assert summary['dates'] == ['saved', '2025-11-06'] and summary['rows'] == 1 and summary['grid_reference'] == 'saved-png'
    assert summary['skipped_dates'] == [{'date': '2025-09-28', 'reason': 'rejected in stack report'},
                                        {'date': '2023-12-20', 'reason': 'rejected in stack report'}]
    assert summary['registration_residual_px'] == {'saved': 0., '2025-11-06': .8}
    report, views = mda.load_stack(tmp_path, 'ab12ab12ab12ab12')
    assert report['status'] == 'pair' and report['mode'] == 'pair' and report['mode_note'] == 'two views only'
    assert report['stack_version'] == 'scene-local-stack-v1' and report['source_image_sha256'] == scene['image_sha256']
    assert [r['status'] for r in mda.date_records(report)] == ['accepted', 'rejected', 'rejected']
    assert [r['residual_px'] for r in mda.date_records(report)] == [.8, .76, .54]
    assert [v['image'] is None for v in views] == [False, True, True] and views[0]['image'].shape == (256, 256, 3)
    payload = mda.assess_scene(scene, image, report, views)[0]['payload']
    assert payload['mode'] == 'pair' and payload['mode_note'] == 'two views only'
    assert payload['dates'] == ['saved', '2025-11-06'] and payload['n_dates'] == 2
    # Two views: boundary consensus (>= 2 dates) is exactly the date's own support; only the saved PNG clears the
    # support threshold on the occluded date.
    assert payload['dates_with_support'] == 1 and .4 < payload['boundary_edge_consensus'] < .5
    assert abs(payload['boundary_edge_consensus'] - payload['edge_support_by_date']['2025-11-06']['support']) < 1e-9
    assert summary['dates_with_support_histogram'] == {'1': 1}
    assert json.dumps(payload)
    # Written rows carry the mode; the summary lands under data/reviews/eyes only.
    written = mda.run(tmp_path, scene['id'], 'ab12ab12ab12ab12')
    assert written['status'] == 'ok' and written['mode'] == 'pair'
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = eyes_index.rows(db, 'multidate', scene_id=scene['id'])
    assert len(rows) == 1 and rows[0]['payload']['mode'] == 'pair' and rows[0]['payload']['stack_id'] == 'ab12ab12ab12ab12'
    saved = json.loads((tmp_path/'data/reviews/eyes/multidate'/scene['id']/'ab12ab12ab12ab12.json').read_text())
    assert saved['mode'] == 'pair' and saved['stack_status'] == 'pair'
    assert (tmp_path/'data/scenes'/scene['id']/'latest.json').read_text() == json.dumps(scene), 'production untouched'


def test_pair_stack_with_a_clean_date_supports_both_views_everywhere(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    _write_pair_stack_report(tmp_path, 'cd34cd34cd34cd34', scene['id'], scene['image_sha256'], aligned=saved_pixels(10))
    summary = mda.run(tmp_path, scene['id'], 'cd34cd34cd34cd34', dry_run=True)
    assert summary['mode'] == 'pair' and summary['dates'] == ['saved', '2025-11-06']
    assert summary['dates_with_support_histogram'] == {'2': 1} and summary['boundary_edge_consensus_median'] == 1.
    # dates_with_support on a pair is bounded by the two views.
    report, views = mda.load_stack(tmp_path, 'cd34cd34cd34cd34')
    payload = mda.assess_scene(scene, image, report, views)[0]['payload']
    assert payload['dates_with_support'] == 2 and payload['boundary_edge_consensus'] == 1. and payload['mode'] == 'pair'


def test_non_pair_stacks_carry_no_mode_keys(tmp_path):
    scene, image = scene_fixture()
    _write_scene(tmp_path, scene, image)
    _write_stack_report(tmp_path, 'dde125b138d603c4', scene['id'], scene['image_sha256'])
    summary = mda.run(tmp_path, scene['id'], 'dde125b138d603c4', dry_run=True)
    assert 'mode' not in summary and summary['stack_status'] == 'available'
    report, views = mda.load_stack(tmp_path, 'dde125b138d603c4')
    assert 'mode' not in report and 'mode_note' not in report
    payload = mda.assess_scene(scene, image, report, views)[0]['payload']
    assert 'mode' not in payload and 'mode_note' not in payload
    fixture_report, fixture_views = stack_fixture()
    assert 'mode' not in mda.assess_scene(scene, image, fixture_report, fixture_views)[0]['payload']
