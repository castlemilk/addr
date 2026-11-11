"""window-readiness-v1: the render-and-look gate on synthetic windows (decision card 2026-10-06 item 4).

Synthetic frame-cut windows written under tmp_path/data/scenes: a flat PNG whose sha256 the latest.json names, a model layer,
a review with carried copies and their replacers. Nothing here reads or writes the repository's data/scenes/.
"""
import hashlib
import json

import pytest
from PIL import Image
from scripts.ml import scene_focus as F
from scripts.ml import scene_window_ready as W

SIDE = 512


def _box(minx, miny, maxx, maxy):
    return [[float(minx), float(miny)], [float(maxx), float(miny)], [float(maxx), float(maxy)], [float(minx), float(maxy)]]


def _building(object_id, ring, **extra):
    return {'id': object_id, 'kind': 'building', 'label': 'Building', 'ring': ring, 'status': 'candidate',
            'geometry_reviewed': False, 'source': 'sam3-text-onnx', 'model': {'area_m2': 80., 'height_verified': False}, **extra}


def _slice(object_id, ring, status='needs_adjustment', **extra):
    return {**_building(object_id, ring), 'status': status,
            'carried_from': {'scene_id': 'roof_training_250-1', 'object_id': object_id, 'method': 'georeferenced-affine',
                             'role': 'frame_cut_target', 'status': 'candidate', 'geometry_reviewed': False, 'iou_with_new': 0},
            **extra}


def _body(object_id, ring, slice_id, **extra):
    return {**_building(object_id, ring), 'source': 'carried-body-v1', 'replaces': [slice_id], 'derived_from': [slice_id],
            'carried_from': {'scene_id': 'house-2', 'object_id': object_id, 'method': 'georeferenced-affine',
                             'role': 'whole_roof_body', 'status': 'candidate', 'geometry_reviewed': False, 'iou_with_new': 0},
            **extra}


def _window(root, window_id, *, model, review, side=SIDE, stale=False):
    """A frame-cut window on disk: PNG + latest.json (window_frame of the pipeline's version) + review.json."""
    folder = root/'data/scenes'/window_id
    folder.mkdir(parents=True)
    image = Image.new('RGB', (side, side), (90, 110, 70))
    path = folder/'tmp.png'
    image.save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    path.rename(folder/f'{sha}.png')
    latest = {'schema_version': 1, 'id': window_id, 'image_sha256': sha, 'image_width': side, 'image_height': side,
              'objects': model, 'groups': [], 'window_frame': {'version': F.VERSION}, 'model_signature': 'sig-' + window_id}
    (folder/'latest.json').write_text(json.dumps(latest))
    review_doc = {'revision': 1, 'model_signature': ('old-' if stale else 'sig-') + window_id, 'objects': review, 'groups': [],
                  'updated_by': 'scripts.ml.scene_focus carry-forward'}
    (folder/'review.json').write_text(json.dumps(review_doc))
    return latest, review_doc


SLICE_RING = _box(200, 200, 260, 230)             # the carried frame-cut slice, well inside the window frame
ROOF_RING = _box(190, 150, 270, 240)              # the whole roof over it
FAR_RING = _box(400, 400, 460, 450)               # another building of the model, unrelated


@pytest.fixture
def windows(tmp_path):
    """Five windows: slice-only, slice superseded by a carried body (model empty), empty model with no body, a replacer ring on the
    frame edge, and a normal takeover window (slice superseded by the window's own detection)."""
    _window(tmp_path, 'win-slice-only', model=[_building('d1', FAR_RING), _building('d2', _box(180, 190, 230, 235))],
            review=[_slice('s1', SLICE_RING), _building('d1', FAR_RING), _building('d2', _box(180, 190, 230, 235))])
    _window(tmp_path, 'lot-body', model=[],
            review=[_slice('s1', SLICE_RING, status='superseded', replacement_ids=['b1']), _body('b1', ROOF_RING, 's1')])
    _window(tmp_path, 'lot-empty', model=[], review=[_slice('s1', SLICE_RING)])
    _window(tmp_path, 'win-edge', model=[_building('d1', _box(190, 0.5, 270, 240))],
            review=[_slice('s1', SLICE_RING, status='superseded', replacement_ids=['d1']),
                    _building('d1', _box(190, 0.5, 270, 240), replaces=['s1'], derived_from=['s1'])])
    _window(tmp_path, 'win-normal', model=[_building('d1', ROOF_RING), _building('d2', FAR_RING)],
            review=[_slice('s1', SLICE_RING, status='superseded', replacement_ids=['d1']),
                    _building('d1', ROOF_RING, replaces=['s1'], derived_from=['s1']), _building('d2', FAR_RING)])
    return tmp_path


def test_slice_only_is_not_ready(windows):
    verdict = W.judge_window(windows, 'win-slice-only', windows/'out')
    assert verdict['ready'] is False and verdict['reason_names'] == ['slice_only']
    [reason] = verdict['reasons']
    assert reason['copy_id'] == 's1' and reason['frozen_key'] == ['roof_training_250-1', 's1']
    assert verdict['model_buildings'] == 2 and verdict['copies'] == 1 and verdict['nothing_pending'] is False
    [key] = verdict['keys']
    assert key['pending'] is True and key['decided'] is False and key['replacers'] == [] and key['asked'] == ['s1']
    assert [a['role'] for a in verdict['asked']] == ['slice_pending']


def test_slice_superseded_by_a_carried_body_is_ready_even_with_an_empty_model(windows):
    verdict = W.judge_window(windows, 'lot-body', windows/'out')
    assert verdict['ready'] is True and verdict['reasons'] == [] and verdict['model_buildings'] == 0
    [key] = verdict['keys']
    assert key['replacers'] == ['b1'] and key['body_replacer'] is True and key['replacer_reciprocal'] is True
    assert key['pending'] is True                        # the body is a candidate: the reviewer still decides it
    roles = {a['id']: a['role'] for a in verdict['asked']}
    assert roles == {'s1': 'slice_superseded', 'b1': 'body'}
    assert W._line(verdict).startswith('- lot-body: READY (body b1); render ')


def test_empty_model_with_no_body_is_not_ready_for_both_reasons(windows):
    verdict = W.judge_window(windows, 'lot-empty', windows/'out')
    assert verdict['ready'] is False and verdict['reason_names'] == ['model_empty', 'slice_only']
    model_empty = next(r for r in verdict['reasons'] if r['reason'] == 'model_empty')
    assert model_empty['copy_ids'] == ['s1']


def test_a_replacer_ring_on_the_frame_edge_is_not_ready(windows):
    verdict = W.judge_window(windows, 'win-edge', windows/'out')
    assert verdict['ready'] is False and verdict['reason_names'] == ['ring_on_frame']
    [reason] = verdict['reasons']
    assert reason['object_id'] == 'd1' and reason['role'] == 'replacer' and reason['edges'] == ['top']
    assert verdict['frame_tolerance_px'] == F.TAKEOVER['frame_tolerance_px'] == 1.0


def test_a_normal_takeover_window_is_ready(windows):
    verdict = W.judge_window(windows, 'win-normal', windows/'out')
    assert verdict['ready'] is True and verdict['reasons'] == []
    [key] = verdict['keys']
    assert key['replacers'] == ['d1'] and key['body_replacer'] is False
    assert W._line(verdict).startswith('- win-normal: READY (own detection d1); render ')
    # the render is the native frame and the verdict sits beside it
    out = windows/'out'
    with Image.open(out/'win-normal.png') as image:
        assert image.size == (SIDE, SIDE)
    saved = json.loads((out/'win-normal.json').read_text())
    assert saved['ready'] is True and saved['version'] == W.VERSION and saved['render'] == str(out/'win-normal.png')


def test_a_decided_slice_stands_and_a_stale_review_is_noted_not_refused(tmp_path):
    _window(tmp_path, 'win-decided', model=[_building('d1', FAR_RING)],
            review=[_slice('s1', SLICE_RING, status='rejected'), _slice('s2', SLICE_RING, status='confirmed', geometry_reviewed=True)],
            stale=True)
    verdict = W.judge_window(tmp_path, 'win-decided', tmp_path/'out')
    assert verdict['ready'] is True and verdict['nothing_pending'] is True and verdict['stale_review'] is True
    assert all(k['decided'] for k in verdict['keys'])
    assert 'stale review' in W._line(verdict) and 'nothing pending' in W._line(verdict)
    # the same decided slices beside an undecided one: the undecided key refuses the window
    _window(tmp_path, 'win-mixed', model=[_building('d1', FAR_RING)],
            review=[_slice('s1', SLICE_RING, status='rejected'), _slice('s2', _box(300, 300, 340, 330))])
    verdict = W.judge_window(tmp_path, 'win-mixed', tmp_path/'out')
    assert verdict['ready'] is False and [r['copy_id'] for r in verdict['reasons']] == ['s2']


def test_assess_is_pure_and_frame_checks_pending_slices_and_bodies_too():
    latest = {'image_width': 512, 'image_height': 512, 'objects': [_building('d1', FAR_RING)]}
    review = {'model_signature': None, 'objects': [_slice('s1', _box(0, 100, 60, 130))]}
    verdict = W.assess(review, latest)
    assert verdict['reason_names'] == ['ring_on_frame', 'slice_only']
    edge = next(r for r in verdict['reasons'] if r['reason'] == 'ring_on_frame')
    assert edge['role'] == 'slice_pending' and edge['edges'] == ['left']
    # a superseded slice whose replacer vanished from the review is a slice with no replacer
    review = {'objects': [_slice('s1', SLICE_RING, status='superseded', replacement_ids=['gone'])]}
    verdict = W.assess(review, latest)
    assert verdict['ready'] is False and verdict['reasons'][0]['reason'] == 'slice_only'
    assert verdict['reasons'][0]['detail'].startswith('superseded but no replacer')
    # a body on the bottom edge refuses even though it replaces the slice
    body = _body('b1', _box(190, 150, 270, 511.5), 's1')
    review = {'objects': [_slice('s1', SLICE_RING, status='superseded', replacement_ids=['b1']), body]}
    verdict = W.assess(review, {'image_width': 512, 'image_height': 512, 'objects': []})
    assert verdict['reason_names'] == ['ring_on_frame'] and verdict['reasons'][0]['edges'] == ['bottom']


def test_batch_writes_one_line_per_window_and_refuses_to_rewrite_a_dated_report(windows):
    stem = windows/'data/reviews/eyes/recapture/window-readiness-2026-10-06'
    header, verdicts = W.batch(windows, F.window_scenes(windows), stem)
    assert header['windows'] == 5 and header['ready'] == 2 and header['not_ready'] == 3
    assert header['by_reason'] == {'slice_only': 2, 'model_empty': 1, 'ring_on_frame': 1}
    assert header['ready_with_body'] == 1 and header['frame_tolerance_px'] == 1.0
    report = json.loads(stem.with_suffix('.json').read_text())
    assert [v['window_id'] for v in report['windows']] == ['lot-body', 'lot-empty', 'win-edge', 'win-normal', 'win-slice-only']
    text = stem.with_suffix('.md').read_text()
    lines = [line for line in text.splitlines() if line.startswith('- ')]
    assert len(lines) == 5 and sum('NOT READY' in line for line in lines) == 3
    assert all(f'render {stem}/' in line for line in lines)
    for wid in ('lot-body', 'lot-empty', 'win-edge', 'win-normal', 'win-slice-only'):
        assert (stem/f'{wid}.png').exists() and (stem/f'{wid}.json').exists()
    with pytest.raises(FileExistsError):
        W.batch(windows, F.window_scenes(windows), stem)
    header2, _ = W.batch(windows, F.window_scenes(windows), stem, overwrite=True)
    assert header2['by_reason'] == header['by_reason']


def test_cli_single_window_exits_one_when_not_ready_and_zero_when_ready(windows, capsys):
    assert W.main(['--root', str(windows), '--scene', 'win-slice-only', '--out', str(windows/'cli')]) == 1
    assert 'NOT READY: slice_only s1' in capsys.readouterr().out
    assert W.main(['--root', str(windows), '--scene', 'win-normal', '--out', str(windows/'cli'), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['ready'] is True
