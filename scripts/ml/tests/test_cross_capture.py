import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import shapely
import shapely.affinity
from scripts.ml import cross_capture as cc
from scripts.ml import eyes_index
from scripts.ml import scene_confidence as c
from shapely.geometry import Polygon

REPO = Path(__file__).resolve().parents[3]
CAMBRIDGE = (145.3115, -37.7912, 145.3135, -37.7895)


def _ring(west, south, east, north):
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def _feature(scene_id, object_id, band, ring, score=.8, mismatch=.5, segments=6, clipped=False):
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]},
            'properties': {'scene_id': scene_id, 'object_id': object_id, 'band': band, 'detector_score': score,
                           'image_line_mismatch_m': mismatch, 'observed_segments': segments, 'clipped': clipped,
                           'area_m2': 100., 'model_signature': 'm'*64, 'profile': 'p'*64, 'reasons': []}}


def _index(root, features):
    """Write a synthetic confidence.sqlite plus current latest.json stamps, like assess() does."""
    with closing(c.connection(root)) as db, db:
        for f in features:
            ring = f['geometry']['coordinates'][0]
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            db.execute('INSERT OR REPLACE INTO roofs VALUES(?,?,?,?,?,?,?)',
                       (f['properties']['scene_id'], f['properties']['object_id'], min(xs), min(ys), max(xs), max(ys), json.dumps(f)))
        for sid in {f['properties']['scene_id'] for f in features}:
            folder = root/'data/scenes'/sid
            folder.mkdir(parents=True, exist_ok=True)
            (folder/'latest.json').write_text('{}')
            db.execute('INSERT OR REPLACE INTO scenes VALUES(?,?,?,?)',
                       (sid, 'm'*64, (folder/'latest.json').stat().st_mtime_ns, json.dumps([0, 0, 1, 1])))


@pytest.fixture
def cluster(tmp_path):
    # Two captures of the same roof (tile low, house medium), one roof nobody overlaps, one weak overlap.
    features = [
        _feature('tile', 'low-roof', 'low', _ring(145.3120, -37.7905, 145.3124, -37.7902), score=.58, mismatch=1.35, segments=4),
        _feature('house', 'med-roof', 'medium', _ring(145.31201, -37.79051, 145.31241, -37.79021), score=.80, mismatch=.9, segments=10),
        _feature('tile', 'alone', 'high', _ring(145.3130, -37.7905, 145.3134, -37.7902), score=.95),
        _feature('house', 'weak', 'high', _ring(145.3133, -37.7905, 145.3137, -37.7902), score=.95),
    ]
    _index(tmp_path, features)
    return tmp_path


def test_collect_reads_readonly_and_mirrors_query_pairs(cluster):
    records, meta = cc.collect(cluster, CAMBRIDGE)
    assert meta['rows'] == 4 and meta['current_rows'] == 4 and meta['stale_scenes'] == []
    assert len(records) == 1
    row = records[0]
    assert (row['scene_id'], row['object_id']) == ('tile', 'low-roof')
    assert (row['other_scene_id'], row['other_object_id']) == ('house', 'med-roof')
    assert row['version'] == cc.VERSION == 'cross-capture-v1'
    payload = row['payload']
    assert payload['iou'] == pytest.approx(.39*.29/(2*.12-.39*.29), abs=1e-4)  # analytic box IoU .8913
    assert 0 < payload['hausdorff_m'] < 3
    assert payload['band'] == 'low' and payload['other_band'] == 'medium'
    assert payload['score'] == .58 and payload['other_score'] == .8
    assert payload['mismatch'] == 1.35 and payload['other_mismatch'] == .9
    assert payload['segments'] == 4 and payload['other_segments'] == 10
    assert payload['hidden_by'] == 'other' and payload['band_disagreement'] is True and payload['same_mosaic_caveat'] is True
    assert payload['code_sha256'] == hashlib.sha256(Path(cc.__file__).read_bytes()).hexdigest()
    assert payload['computed_at']
    # Same decisions as the production query(): its kept feature hides exactly this roof.
    shown = c.query([145.30, -37.80, 145.32, -37.78], cluster)
    hidden = [(f['properties']['scene_id'], f['properties']['object_id'], h['scene_id'], h['object_id'])
              for f in shown['features'] for h in f['properties'].get('hidden_overlaps', [])]
    assert hidden == [('house', 'med-roof', 'tile', 'low-roof')]
    kept = next(f['properties'] for f in shown['features'] if f['properties']['object_id'] == 'med-roof')
    assert kept['hidden_overlaps'][0]['iou'] == payload['iou']
    assert shown['duplicates'] == len(records) and len(shown['features']) == meta['current_rows']-len(records)


def test_build_writes_eyes_rows_and_build_record(cluster):
    index = cluster/'data/suburbs/mooroolbark/confidence.sqlite'
    before = index.read_bytes()
    result = cc.build(cluster, CAMBRIDGE)
    assert result['pairs'] == 1 and result['version'] == 'cross-capture-v1'
    with closing(eyes_index.connection(cluster, readonly=True)) as db:
        rows = eyes_index.rows(db, 'cross_capture', version='cross-capture-v1')
    assert len(rows) == 1 and rows[0]['object_id'] == 'low-roof' and rows[0]['other_object_id'] == 'med-roof'
    record = json.loads((cluster/'data/reviews/eyes/cross-capture/build.json').read_text())
    assert record['pairs'] == 1 and record['rows'] == 4 and record['bounds'] == list(CAMBRIDGE)
    assert record['version'] == 'cross-capture-v1' and record['code_sha256'] == rows[0]['payload']['code_sha256']
    # The production confidence database is byte-identical (a mode=ro WAL reader may only recreate -shm/-wal).
    assert index.read_bytes() == before
    assert (cluster/'data/reviews/eyes/eyes.sqlite').exists()


def test_rebuild_replaces_pairs_for_considered_roofs(cluster):
    cc.build(cluster, CAMBRIDGE)
    # Move the house roof away: the earlier pair must disappear on rebuild, not linger.
    with closing(c.connection(cluster)) as db, db:
        f = _feature('house', 'med-roof', 'medium', _ring(145.3140, -37.7905, 145.3144, -37.7902))
        db.execute('UPDATE roofs SET west=?, south=?, east=?, north=?, payload=? WHERE scene_id=? AND object_id=?',
                   (145.3140, -37.7905, 145.3144, -37.7902, json.dumps(f), 'house', 'med-roof'))
    assert cc.build(cluster, CAMBRIDGE)['pairs'] == 0
    with closing(eyes_index.connection(cluster, readonly=True)) as db:
        assert eyes_index.rows(db, 'cross_capture') == []


def test_stale_scenes_are_excluded_like_query(cluster):
    path = cluster/'data/scenes/house/latest.json'
    path.write_text(path.read_text()+' ')
    records, meta = cc.collect(cluster, CAMBRIDGE)
    assert records == [] and meta['stale_scenes'] == ['house'] and meta['current_rows'] == 2


def test_bounds_default_to_suburb_boundary_and_are_validated(cluster):
    folder = cluster/'data/suburbs/mooroolbark'
    (folder/'boundary.geojson').write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {}, 'geometry': {'type': 'Polygon', 'coordinates': [_ring(145.30, -37.80, 145.32, -37.78)]}}]}))
    assert cc.default_bounds(cluster) == [145.30, -37.80, 145.32, -37.78]
    assert cc.build(cluster)['pairs'] == 1
    for bad in ([1, 2, 3], [145.32, -37.80, 145.30, -37.78], [145.30, -37.78, 145.32, -37.80], [float('nan'), 0, 1, 1]):
        with pytest.raises(ValueError):
            cc.collect(cluster, bad)
    assert cc.collect(cluster, [145.0, -37.0, 145.1, -36.9])[0] == []


def test_collect_without_index_or_boundary_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        cc.collect(tmp_path, CAMBRIDGE)
    with pytest.raises(FileNotFoundError):
        cc.default_bounds(tmp_path)


def test_summary_reports_rates_with_denominators(cluster):
    assert cc.summary(cluster)['pairs'] == 0
    cc.build(cluster, CAMBRIDGE)
    out = cc.summary(cluster)
    assert out['version'] == 'cross-capture-v1'
    assert out['pairs'] == 1 and out['hidden_roofs'] == 1 and out['roofs_with_overlap'] == 2
    assert out['band_disagreement_pairs'] == 1 and out['hidden_low_under_higher'] == 1
    assert out['roofs_considered'] == 4 and out['roofs_with_overlap_rate'] == pytest.approx(.5)
    assert out['band_matrix'] == {'low->medium': 1}
    assert any('not accuracy' in d for d in out['denominators'])
    assert any('4 indexed' in d for d in out['denominators'])


def test_cli_build_and_summary(cluster, capsys):
    assert cc.main(['--root', str(cluster), '--build', '--bounds', *map(str, CAMBRIDGE), '--summary']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['build']['pairs'] == 1 and out['summary']['pairs'] == 1


@pytest.mark.skipif(not (REPO/'data/suburbs/mooroolbark/confidence.sqlite').exists(), reason='real suburb index absent')
def test_real_cambridge_cluster_hidden_low_is_recorded_readonly():
    # Read-only over the production index; nothing is written or even attempted.
    records, meta = cc.collect(REPO, CAMBRIDGE)
    hidden = {(r['scene_id'], r['object_id']): r for r in records}
    key = ('mooroolbark-50011498-17-118442-80417', '44c724fd3480c167')
    if list(key) not in meta['considered']:
        pytest.skip('cluster roof not indexed as current in this checkout')
    # Considered and current: it must be recorded as hidden by the house capture (a regression fails, never skips).
    row = hidden[key]
    assert (row['other_scene_id'], row['other_object_id']) == ('house-30286', '37ff5c02f77869d5')
    assert row['payload']['iou'] >= .65 and row['payload']['band'] == 'low' and row['payload']['other_band'] == 'medium'


def test_connection_is_read_only_on_the_index(cluster):
    with closing(cc.connection(cluster)) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO scenes VALUES('x','y',0,'[]')")


def test_mismatch_is_null_aware_for_zero_segment_roofs(tmp_path):
    # observed_segments == 0 stores image_line_mismatch_m 0.0 in the index: that is "no lines", not a perfect fit.
    _index(tmp_path, [
        _feature('tile', 'none', 'low', _ring(145.3120, -37.7905, 145.3124, -37.7902), score=.5, mismatch=0., segments=0),
        _feature('house', 'seen', 'medium', _ring(145.31201, -37.79051, 145.31241, -37.79021), mismatch=.4, segments=5)])
    [row], _ = cc.collect(tmp_path, CAMBRIDGE)
    p = row['payload']
    assert p['segments'] == 0 and p['mismatch'] is None and p['mismatch_raw'] == 0.
    assert p['other_mismatch'] == .4 and p['other_mismatch_raw'] == .4


def test_rebuild_drops_pairs_whose_roofs_went_stale_or_vanished(cluster):
    assert cc.build(cluster, CAMBRIDGE)['pairs'] == 1
    # The hidden roof's scene goes stale: query() stops showing it, so the pair must not linger in the sidecar.
    path = cluster/'data/scenes/tile/latest.json'
    path.write_text(path.read_text()+' ')
    assert cc.build(cluster, CAMBRIDGE)['pairs'] == 0
    with closing(eyes_index.connection(cluster, readonly=True)) as db:
        assert eyes_index.rows(db, 'cross_capture') == []


def test_rebuild_elsewhere_drops_pairs_whose_shown_roof_vanished(cluster):
    assert cc.build(cluster, CAMBRIDGE)['pairs'] == 1
    with closing(c.connection(cluster)) as db, db:
        db.execute("DELETE FROM roofs WHERE scene_id='house' AND object_id='med-roof'")
    # A build over unrelated bounds still garbage-collects rows whose roofs are no longer current anywhere.
    cc.build(cluster, [145.0, -37.0, 145.1, -36.9])
    with closing(eyes_index.connection(cluster, readonly=True)) as db:
        assert eyes_index.rows(db, 'cross_capture') == []


def test_summary_denominator_matches_the_last_build_population(cluster):
    far = [_feature('tile', 'far-low', 'low', _ring(145.3180, -37.7905, 145.3184, -37.7902), score=.5),
           _feature('house', 'far-high', 'high', _ring(145.31801, -37.79051, 145.31841, -37.79021), score=.95)]
    with closing(c.connection(cluster)) as db, db:
        for f in far:
            ring = f['geometry']['coordinates'][0]
            db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', ('tile' if f is far[0] else 'house', f['properties']['object_id'],
                       ring[0][0], ring[0][1], ring[2][0], ring[2][1], json.dumps(f)))
    wide = [145.30, -37.80, 145.32, -37.78]
    assert cc.build(cluster, wide)['pairs'] == 2
    small = cc.build(cluster, CAMBRIDGE)
    assert small['pairs'] == 1 and small['current_rows'] == 4
    out = cc.summary(cluster)
    # Pair stats describe the last build's population; rows kept from earlier builds are reported separately.
    assert out['pairs'] == 1 and out['roofs_considered'] == 4 and out['roofs_with_overlap'] == 2
    assert out['roofs_with_overlap_rate'] == pytest.approx(.5)
    assert out['index_pairs'] == 2
    assert any('index_pairs: 2' in d for d in out['denominators'])


def test_hausdorff_matches_a_known_metre_offset():
    lat0 = -37.79
    metre_lon = 360/(cc.WORLD_SPAN_M*math.cos(math.radians(lat0)))
    a = Polygon(_ring(145.312, lat0, 145.312+10*metre_lon, lat0+10*metre_lon*math.cos(math.radians(lat0))))
    b = shapely.affinity.translate(a, xoff=1.5*metre_lon)
    assert cc.hausdorff_m(a, b) == pytest.approx(1.5, abs=.01)
    assert cc.hausdorff_m(a, a) == pytest.approx(0, abs=1e-9)


def test_uri_special_characters_in_root_are_quoted(tmp_path):
    root = tmp_path/'odd #dir?mode=rw%20'
    _index(root, [_feature('tile', 'low-roof', 'low', _ring(145.3120, -37.7905, 145.3124, -37.7902)),
                  _feature('house', 'med-roof', 'medium', _ring(145.31201, -37.79051, 145.31241, -37.79021))])
    assert cc.build(root, CAMBRIDGE)['pairs'] == 1
    with closing(cc.connection(root)) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO scenes VALUES('x','y',0,'[]')")


def test_default_bounds_accepts_feature_or_bare_geometry(tmp_path):
    folder = tmp_path/'data/suburbs/mooroolbark'
    folder.mkdir(parents=True)
    geometry = {'type': 'Polygon', 'coordinates': [_ring(145.30, -37.80, 145.32, -37.78)]}
    for data in (geometry, {'type': 'Feature', 'properties': {}, 'geometry': geometry}):
        (folder/'boundary.geojson').write_text(json.dumps(data))
        assert cc.default_bounds(tmp_path) == [145.30, -37.80, 145.32, -37.78]


# --- --hidden-low: the read-only queue behind GET /api/queues/hidden --------------------------------

def _pair(hidden, shown, band, other_band, iou=.9, version=cc.VERSION):
    return {'scene_id': hidden[0], 'object_id': hidden[1], 'other_scene_id': shown[0], 'other_object_id': shown[1],
            'version': version, 'payload': {'band': band, 'other_band': other_band, 'score': .58, 'other_score': .8,
                                            'segments': 4, 'other_segments': 10, 'iou': iou, 'hausdorff_m': 1.2}}


def test_hidden_low_lists_low_roofs_hidden_under_higher_bands(tmp_path):
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'cross_capture', [
            _pair(('tile', 'b'), ('house', 'm'), 'low', 'medium'),
            _pair(('tile', 'a'), ('house', 'h'), 'low', 'high', iou=.95),
            _pair(('tile', 'c'), ('house', 'x'), 'medium', 'high'),        # hidden roof is not low
            _pair(('tile', 'd'), ('house', 'y'), 'low', 'low'),            # shown roof is not higher
            _pair(('tile', 'e'), ('house', 'z'), 'low', 'high', version='cross-capture-v0'),  # other version
        ])
    index = tmp_path/'data/reviews/eyes/eyes.sqlite'
    before = index.read_bytes()
    out = cc.hidden_low(tmp_path)
    assert out['version'] == 'cross-capture-v1' and out['count'] == 2 and out['index_available'] is True
    assert [(r['scene_id'], r['object_id']) for r in out['rows']] == [('tile', 'a'), ('tile', 'b')]
    assert out['rows'][1] == {
        'scene_id': 'tile', 'object_id': 'b', 'band': 'low', 'detector_score': .58, 'observed_segments': 4,
        'other_scene_id': 'house', 'other_object_id': 'm', 'other_band': 'medium', 'other_detector_score': .8, 'iou': .9,
        'hidden_url': '/scenes?scene=tile&object=b&kind=building&status=all&layer=model',
        'shown_url': '/scenes?scene=house&object=m&kind=building&status=all&layer=model'}
    assert 'not accuracy' in out['note']
    assert index.read_bytes() == before, 'listing never writes to the eyes index'
    assert not (tmp_path/'data/suburbs').exists(), 'the confidence index is never opened or created'


def test_hidden_low_matches_the_built_pairs(cluster):
    cc.build(cluster, CAMBRIDGE)
    out = cc.hidden_low(cluster)
    assert out['count'] == 1 and len(out['rows']) == 1
    row = out['rows'][0]
    assert (row['scene_id'], row['object_id'], row['other_scene_id'], row['other_object_id']) == ('tile', 'low-roof', 'house', 'med-roof')
    assert row['band'] == 'low' and row['other_band'] == 'medium'
    assert row['detector_score'] == .58 and row['other_detector_score'] == .8 and row['observed_segments'] == 4
    assert row['iou'] == pytest.approx(.8913, abs=1e-4)


def test_hidden_low_without_an_index_is_an_honest_empty_document(tmp_path):
    out = cc.hidden_low(tmp_path)
    assert out['count'] == 0 and out['rows'] == [] and out['index_available'] is False
    assert out['version'] == 'cross-capture-v1'
    assert not (tmp_path/'data').exists(), 'an absent index is never created by a read'


def test_hidden_low_cli_prints_the_document_and_runs_alone(tmp_path, capsys):
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'cross_capture', [_pair(('tile', 'a'), ('house', 'h'), 'low', 'high')])
    assert cc.main(['--root', str(tmp_path), '--hidden-low']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['count'] == 1 and out['rows'][0]['object_id'] == 'a' and set(out) >= {'version', 'count', 'rows', 'note'}
    with pytest.raises(SystemExit):
        cc.main(['--root', str(tmp_path), '--hidden-low', '--summary'])
    # Existing flags keep their nested output document.
    assert cc.main(['--root', str(tmp_path), '--summary']) == 0
    assert set(json.loads(capsys.readouterr().out)) == {'summary'}
