import json
import sqlite3
from datetime import UTC, datetime

import pytest
from scripts.ml import eyes_eligibility as eligibility
from scripts.ml import scene_confidence
from scripts.ml.scene_split import group_split, image_split_audit
from scripts.ml.scene_training import atomic_json
from scripts.ml.tiles import tile_xy_to_lonlat

PROFILE = 'abc12345' + 'f'*56
OTHER = 'ffffffff' + '0'*56


def tile_pair():
    """A train z17 tile whose eastern neighbour is validation, near the scan."""
    for tx in range(118420, 118480):
        for ty in range(80400, 80430):
            if group_split(f'z17:{tx}:{ty}') == 'train' and group_split(f'z17:{tx+1}:{ty}') == 'validation':
                return tx, ty
    raise AssertionError('no adjacent train/validation pair')


def candidate(object_id, ring, **extra):
    return {'id': object_id, 'kind': 'building', 'label': 'Building', 'status': 'candidate', 'geometry_reviewed': False,
            'source': 'sam3-text-onnx', 'score': .5, 'ring': ring, **extra}


def scene(scene_id, origin, metres_per_pixel, objects, owned):
    lon, lat = origin
    m = metres_per_pixel
    return {'id': scene_id, 'title': scene_id, 'image_sha256': '0'*64, 'image_width': 200, 'image_height': 200,
            'pixel_to_meters': [[m, 0], [0, m], [0, 0]], 'meters_to_pixel': [[1/m, 0], [0, 1/m], [0, 0]],
            'centroid': {'lon': lon, 'lat': lat}, 'model_signature': 'sig-'+scene_id, 'objects': objects, 'groups': [],
            'owned_object_ids': owned}


@pytest.fixture
def root(tmp_path):
    tx, ty = tile_pair()
    # Scene A: a 20 m frame inside the train tile, so the frame audit verifies 'train'.
    a = scene('tile-a', tile_xy_to_lonlat(tx+.3, ty+.6, 17), .1,
              [candidate('a1', [[20, 20], [140, 20], [140, 80], [20, 80]]),
               candidate('a2', [[20, 100], [140, 100], [140, 160], [20, 160]], geometry_reviewed=True)], ['a1', 'a2'])
    # Scene B: a 200 m frame from the train tile's south-east into the validation neighbour (cross_split).
    b = scene('tile-b', tile_xy_to_lonlat(tx+.9, ty+.9, 17), 1.,
              [candidate('b1', [[2, 2], [14, 2], [14, 8], [2, 8]]),
               candidate('b2', [[100, 2], [120, 2], [120, 8], [100, 8]]),
               candidate('b3', [[2, 100], [14, 100], [14, 108], [2, 108]])], ['b1', 'b2', 'b3'])
    assert image_split_audit(a, 'train')['status'] == 'verified'
    assert image_split_audit(b, 'train')['status'] == 'cross_split'
    for s in (a, b):
        (tmp_path/'data/scenes'/s['id']).mkdir(parents=True)
        atomic_json(tmp_path/'data/scenes'/s['id']/'latest.json', s)
    rows = [('tile-a', 'a1', 'low', PROFILE), ('tile-a', 'a2', 'low', PROFILE), ('tile-a', 'a3', 'low', OTHER),
            ('tile-a', 'gone', 'low', PROFILE), ('tile-b', 'b1', 'low', PROFILE), ('tile-b', 'b2', 'low', PROFILE),
            ('tile-b', 'b3', 'medium', PROFILE)]
    with scene_confidence.connection(tmp_path) as db:
        for sid, oid, band, profile in rows:
            feature = {'type': 'Feature', 'geometry': None,
                       'properties': {'scene_id': sid, 'object_id': oid, 'band': band, 'profile': profile, 'model_signature': 'sig-'+sid}}
            db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', (sid, oid, 0, 0, 0, 0, json.dumps(feature)))
        db.execute('INSERT INTO scenes VALUES(?,?,?,?)', ('tile-a', 'sig-tile-a', 0, '[]'))
        db.execute('INSERT INTO scenes VALUES(?,?,?,?)', ('tile-b', 'sig-tile-b', 0, '[]'))
    return tmp_path


def test_frame_and_core_policies_are_counted_over_the_same_low_roofs(root, monkeypatch):
    loads = []
    original = eligibility.load_scene
    monkeypatch.setattr(eligibility, 'load_scene', lambda r, sid: loads.append(sid) or original(r, sid))
    result = eligibility.count(root, profile_prefix='abc12345')
    assert result['version'] == 'eyes-eligibility-v1'
    assert result['denominators'] == {'owned_roofs': 6, 'low_roofs': 5, 'scenes_with_low_roofs': 2, 'scenes_loaded': 2,
                                      'scenes_unavailable': 0, 'indexed_roofs': 7, 'unreadable_index_rows': 0,
                                      'indexed_scenes': 2}
    assert sorted(loads) == ['tile-a', 'tile-b']
    assert result['frame'] == {'eligible': 1, 'reasons': {'Not an untouched detector candidate': 1, 'Object changed': 1,
                                                          'Held-out, mixed-split or invalid image frame': 2}}
    assert result['core'] == {'eligible': 2, 'reasons': {'Not an untouched detector candidate': 1, 'Object changed': 1,
                                                         'Held-out core tile': 1}}
    assert result['by_core_split'] == {'train': 3, 'validation': 1, 'test': 0, 'unresolved': 1}
    assert result['profile_prefix'] == 'abc12345'
    assert 'not accuracy' in result['scope']


def test_missing_index_is_reported_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        eligibility.count(tmp_path)
    assert not (tmp_path/'data/suburbs').exists()


def test_index_is_opened_read_only(root, monkeypatch):
    opened = []
    real = sqlite3.connect

    def spy(*args, **kwargs):
        opened.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(eligibility.sqlite3, 'connect', spy)
    eligibility.count(root, profile_prefix='abc12345')
    assert opened and all('mode=ro' in a[0] and k.get('uri') for a, k in opened)


def test_cli_count_writes_a_dated_report_under_reviews_eyes(root, capsys):
    eligibility.main(['--root', str(root), '--count', '--profile', 'abc12345'])
    date = datetime.now(UTC).date().isoformat()
    path = root/'data/reviews/eyes'/f'eligibility-{date}.json'
    written = json.loads(path.read_text())
    assert written['frame']['eligible'] == 1 and written['core']['eligible'] == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed['path'] == str(path.relative_to(root))
    assert printed['core']['eligible'] == 2


def test_malformed_index_rows_and_missing_scenes_are_counted_not_fatal(root):
    def low(sid, oid):
        return json.dumps({'type': 'Feature', 'geometry': None, 'properties': {
            'scene_id': sid, 'object_id': oid, 'band': 'low', 'profile': PROFILE, 'model_signature': 'sig-'+sid}})
    extra = [('tile-a', 'raw', 'not json'), ('tile-a', 'bare', json.dumps({'type': 'Feature'})),
             ('../tile-a', 'escape', low('../tile-a', 'escape')), ('tile-gone', 'g1', low('tile-gone', 'g1'))]
    with scene_confidence.connection(root) as db:
        for sid, oid, payload in extra:
            db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', (sid, oid, 0, 0, 0, 0, payload))
    result = eligibility.count(root, profile_prefix='abc12345')
    assert result['denominators'] == {'owned_roofs': 7, 'low_roofs': 6, 'scenes_with_low_roofs': 3, 'scenes_loaded': 2,
                                      'scenes_unavailable': 1, 'indexed_roofs': 11, 'unreadable_index_rows': 3,
                                      'indexed_scenes': 2}
    assert result['frame']['eligible'] == 1 and result['core']['eligible'] == 2
    assert result['frame']['reasons']['Scene unavailable'] == 1 and result['core']['reasons']['Scene unavailable'] == 1
    assert result['by_core_split'] == {'train': 3, 'validation': 1, 'test': 0, 'unresolved': 2}
    assert not (root/'data/scenes/tile-gone').exists() and not (root.parent/'tile-a').exists()
    assert eligibility.indexed_roofs(root, 'abc12345')[:1] == (7,)  # Prior 4-tuple shape is kept.
