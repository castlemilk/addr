import copy
import hashlib
import io
import json
from contextlib import closing

import pytest
from PIL import Image
from scripts.ml import scene_confidence as c
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_region import region_frame
from shapely.geometry import Polygon


def source(root, scene_id='test-confidence', score=.9):
    image = Image.new('RGB', (512, 512), '#777777')
    out = io.BytesIO()
    image.save(out, format='PNG')
    data = out.getvalue()
    scene = {**region_frame([145.31, -37.7905, 145.311, -37.7895]), 'id': scene_id,
             'analysis': {'inference_profile': 'f'*64},
             'title': 'Test', 'image_width': 512, 'image_height': 512, 'image_sha256': hashlib.sha256(data).hexdigest(),
             'objects': [{'id': 'roof', 'kind': 'building', 'status': 'candidate', 'label': 'Roof', 'score': score,
                          'ring': [[100, 100], [200, 100], [200, 200], [100, 200]], 'model': {}}]}
    return save_scene(scene, data, root)


def audit(*args):
    return [{'object_id': 'roof', 'kind': 'building', 'issues': [], 'geometry_passed': True,
             'cost': {'observed_segments': 8, 'image_line_mismatch_m': .2}}]


def test_evidence_is_conservative_and_not_probability():
    row = audit()[0]
    assert c.evidence({'score': .94}, row)['band'] == 'high'
    assert c.evidence({'score': .7}, row)['band'] == 'medium'
    assert c.evidence({'score': .6}, row)['band'] == 'low'
    for bad in (None, float('nan'), 'high', 1.1, True):
        assert c.evidence({'score': bad}, row)['band'] == 'unassessed'
    row['issues'] = [{'code': 'image_cut', 'message': 'Clipped'}]
    assert c.evidence({'score': .99}, row)['band'] == 'low'


def test_index_deduplicates_before_filter_and_invalidates_changed_models(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'audit_scene', audit)
    source(tmp_path, 'one')
    source(tmp_path, 'two', .6)
    for sid in ('one', 'two'):
        c.assess(sid, tmp_path)
    bounds = [145.30, -37.80, 145.32, -37.78]
    result = c.query(bounds, tmp_path)
    assert len(result['features']) == 1
    assert result['duplicates'] == 1
    assert result['counts'] == {'high': 1, 'medium': 0, 'low': 0, 'unassessed': 0}
    assert result['features'][0]['properties']['interval'] is None
    # Additive: the kept roof records what it hid, without changing counts or filtering.
    assert result['features'][0]['properties']['hidden_overlaps'] == [
        {'scene_id': 'two', 'object_id': 'roof', 'band': 'low', 'detector_score': .6, 'image_line_mismatch_m': .2,
         'observed_segments': 8, 'clipped': False, 'iou': 1.0}]
    assert not c.query(bounds, tmp_path, 'low')['features']
    path = tmp_path/'data/scenes/one/latest.json'
    path.write_text(path.read_text()+' ')
    stale = c.query(bounds, tmp_path)
    assert stale['features'][0]['properties']['band'] == 'low'
    # A kept roof that suppressed nothing carries no hidden_overlaps key at all.
    assert 'hidden_overlaps' not in stale['features'][0]['properties']
    assert stale['duplicates'] == 0
    # The prediction and image are not changed by assessment.
    before = (tmp_path/'data/scenes/two/latest.json').read_bytes()
    c.assess('two', tmp_path)
    assert (tmp_path/'data/scenes/two/latest.json').read_bytes() == before


def test_owned_roofs_only_and_image_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'audit_scene', audit)
    scene = source(tmp_path)
    folder = tmp_path/'data/scenes/test-confidence'
    scene['owned_object_ids'] = []
    (folder/'latest.json').write_text(json.dumps(scene))
    c.assess(scene['id'], tmp_path)
    with closing(c.connection(tmp_path)) as db:
        assert db.execute('SELECT COUNT(*) FROM roofs').fetchone()[0] == 0
    (folder/f"{scene['image_sha256']}.png").write_bytes(b'changed')
    with pytest.raises(ValueError, match='imagery changed'):
        c.assess(scene['id'], tmp_path)


def test_wilson_requires_counts():
    assert c.wilson(0, 0) is None
    assert c.wilson(20, 20) == pytest.approx([.8388748419, 1])
    assert c.wilson(0, 20) == pytest.approx([0, .1611251581])
    with pytest.raises(ValueError):
        c.wilson(2, 1)


def test_calibration_requires_independent_validation_groups(tmp_path, monkeypatch):
    scene = source(tmp_path)
    monkeypatch.setattr(c, 'audit_scene', audit)
    c.assess(scene['id'], tmp_path)
    row = {'scene_id': scene['id'], 'model_signature': scene['model_signature'], 'spatial_group': 'z17:one',
           'prediction': scene['objects'], 'reference': copy.deepcopy(scene['objects'])}
    layer = {'scene_id': scene['id'], 'kind': 'building', 'split': 'validation', 'eligible': True}
    coverage = {'version': 'test', 'classes': [{'layers': [layer]}]}
    # Even many duplicate observations from the same geography are one sample.
    monkeypatch.setattr(c, 'collect_scenes', lambda root: ('\n'.join([json.dumps(row)]*25), None, coverage))
    result = c.calibrate(tmp_path)
    band = next(iter(result['strata'].values()))
    assert band['groups'] == band['successes'] == 1
    assert band['interval'] is None
    layer['split'] = 'train'
    assert c.calibrate(tmp_path)['strata'] == {}


def _feature(scene_id, object_id, band, ring, clipped=False):
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]},
            'properties': {'scene_id': scene_id, 'object_id': object_id, 'band': band, 'clipped': clipped}}


def _box(x, y, w=1, h=1):
    return [[x, y], [x+w, y], [x+w, y+h], [x, y+h], [x, y]]


def test_suppressed_overlaps_pairs_kept_with_hidden_across_scenes_only():
    features = [_feature('a', 'kept', 'high', _box(0, 0)),
                _feature('a', 'same-scene', 'medium', _box(0, 0)),       # same scene: never suppressed
                _feature('b', 'dup', 'low', _box(.1, 0, 1, 1)),           # IoU .818 -> hidden by a/kept
                _feature('b', 'weak', 'low', _box(.5, 0, 1, 1)),          # IoU .333 -> kept
                _feature('c', 'far', 'low', _box(5, 5))]                  # no overlap
    features.sort(key=c.dedupe_key)
    assert [f['properties']['object_id'] for f in features] == ['kept', 'same-scene', 'dup', 'weak', 'far']
    polygons = [Polygon(f['geometry']['coordinates'][0]) for f in features]
    pairs = c.suppressed_overlaps(features, polygons)
    assert [(k, s) for k, s, _ in pairs] == [(0, 2)]
    assert pairs[0][2] == pytest.approx(.9/1.1)


def test_suppressed_overlaps_first_hit_only_and_clipped_sorts_last():
    # Two same-scene roofs are both kept; a roof overlapping both is recorded once (first-hit break).
    features = [_feature('a', 'left', 'high', _box(0, 0, 2, 1)),
                _feature('a', 'right', 'high', _box(.02, 0, 2, 1)),
                _feature('c', 'both', 'medium', _box(.05, 0, 2, 1)),
                _feature('d', 'cut', 'high', _box(.05, 0, 2, 1), clipped=True)]
    features.sort(key=c.dedupe_key)
    assert [f['properties']['object_id'] for f in features] == ['left', 'right', 'both', 'cut']
    polygons = [Polygon(f['geometry']['coordinates'][0]) for f in features]
    pairs = c.suppressed_overlaps(features, polygons)
    suppressed = [s for _, s, _ in pairs]
    assert sorted(suppressed) == [2, 3] and len(suppressed) == 2
    assert all(k in (0, 1) for k, _, _ in pairs)
    assert {i for i in range(len(features)) if i not in suppressed} == {0, 1}
