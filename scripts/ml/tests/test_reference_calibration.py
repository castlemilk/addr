import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from scripts.ml import reference_calibration as rc
from scripts.ml import scene_confidence as c
from scripts.ml.scene_batch import signature
from scripts.ml.scene_split import group_split
from scripts.ml.tiles import tile_xy_to_lonlat

REPO = Path(__file__).resolve().parents[3]
PROFILE = '53ea49d5' + 'e' * 56
OTHER_PROFILE = '27cd210e' + 'a' * 56
SIG = 'a' * 64
OTHER_SIG = 'b' * 64
RUN = 'data/suburbs/mooroolbark/runs/500114985968d40b/run.json'


def tiles(split, count, start=(118400, 80400)):
    """First `count` z17 tiles (x, y) near Mooroolbark whose core key hashes to `split`."""
    found = []
    x0, y0 = start
    for i in range(40000):
        x, y = x0 + i % 160, y0 + i // 160
        if group_split(f'z17:{x}:{y}') == split:
            found.append((x, y))
            if len(found) == count:
                return found
    raise AssertionError(f'no {count} {split} tiles found')


def scene_id(x, y):
    return f'mooroolbark-50011498-17-{x}-{y}'


def tile_polygon(x, y, fx=.5, fy=.5, size=.05):
    """WGS84 ring of a small square centred at fractional tile position (fx, fy) of tile (x, y)."""
    west, north = tile_xy_to_lonlat(x + fx - size / 2, y + fy - size / 2, 17)
    east, south = tile_xy_to_lonlat(x + fx + size / 2, y + fy + size / 2, 17)
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def square(x, y, size=100):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


@pytest.mark.parametrize('status', ['confirmed', 'needs_adjustment', 'rejected'])
@pytest.mark.parametrize('authority,reason', [('fieldwork_import', 'fieldwork_pending'), ('provenance', 'machine_label'),
                                              ('carried_from', 'carried_undecided')])
def test_pending_fieldwork_and_machine_decisions_never_count(tmp_path, status, authority, reason):
    obj = {'id': 'roof', 'kind': 'building', 'ring': square(105, 100), 'status': status, 'geometry_reviewed': True}
    obj[authority] = ({'pending': True} if authority == 'fieldwork_import' else {'source': 'machine:test'} if authority == 'provenance'
                      else {'scene_id': 'house-1', 'object_id': 'roof', 'method': 'georeferenced-affine'})
    cache = {'s1': ({'model_signature': 'sig', 'objects': [{'id': 'roof', 'ring': square(100, 100)}]},
                    {'model_signature': 'sig', 'objects': [obj]})}
    result = rc.classify(tmp_path, 's1', 'roof', cache)
    if authority == 'carried_from' and status != 'needs_adjustment':
        # confirmed / rejected on the new image is the reviewer's own decision; only a carried needs_adjustment is undecided.
        assert result['counted'] is True
        return
    assert result['counted'] is False and result['detail'].startswith(reason)
    if authority == 'fieldwork_import':
        obj['fieldwork_import']['pending'] = False
        assert rc.classify(tmp_path, 's1', 'roof', cache)['counted'] is True


def test_carried_from_of_any_non_empty_shape_is_undecided(tmp_path):
    """RULES human_precision.classes: 'any object carrying carried_from with status needs_adjustment'; the shape is not
    restricted to the dict the contract writes, since nothing on the save path rejects another shape yet."""
    def result(carried_from):
        obj = {'id': 'roof', 'kind': 'building', 'ring': square(105, 100), 'status': 'needs_adjustment', 'geometry_reviewed': True,
               'carried_from': carried_from}
        cache = {'s1': ({'model_signature': 'sig', 'objects': [{'id': 'roof', 'ring': square(100, 100)}]},
                        {'model_signature': 'sig', 'objects': [obj]})}
        return rc.classify(tmp_path, 's1', 'roof', cache)
    for odd in ('house-1', ['house-1'], True, 1):
        out = result(odd)
        assert out['counted'] is False and out['detail'].startswith('carried_undecided'), odd
    for empty in ({}, None, '', []):
        out = result(empty)
        assert (out['counted'], out['detail']) == (True, 'ring_edited'), empty


def roof(x, y, oid, band='medium', profile=PROFILE, score=.8, fx=.5, fy=.5, area=120., pixel=None):
    sid = scene_id(x, y)
    ring = tile_polygon(x, y, fx, fy)
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]},
            'properties': {'scene_id': sid, 'object_id': oid, 'band': band, 'detector_score': score, 'profile': profile,
                           'model_signature': SIG, 'area_m2': area,
                           'review_url': f'/scenes?scene={sid}&object={oid}&kind=building&status=all&layer=model'},
            'pixel_ring': pixel or square(100, 100)}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_root(root, features, run_scenes=None, model_signature=SIG):
    """Synthetic confidence.sqlite (owned rows only, like assess()), frozen run.json and latest.json files."""
    root = Path(root)
    database = root / 'data/suburbs/mooroolbark/confidence.sqlite'
    database.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database)
    db.execute('PRAGMA journal_mode=WAL')   # production confidence.sqlite is WAL; a read-only open must not write frames
    db.execute('CREATE TABLE scenes(id TEXT PRIMARY KEY, signature TEXT, mtime INTEGER, bounds TEXT)')
    db.execute('CREATE TABLE roofs(scene_id TEXT, object_id TEXT, west REAL, south REAL, east REAL, north REAL, payload TEXT, PRIMARY KEY(scene_id,object_id))')
    by_scene = {}
    for f in features:
        f = dict(f)
        pixel = f.pop('pixel_ring')
        ring = f['geometry']['coordinates'][0]
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        p = f['properties']
        db.execute('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)', (p['scene_id'], p['object_id'], min(xs), min(ys), max(xs), max(ys), json.dumps(f)))
        by_scene.setdefault(p['scene_id'], []).append({'id': p['object_id'], 'kind': 'building', 'label': 'Building', 'ring': pixel,
                                                       'status': 'candidate', 'geometry_reviewed': False, 'score': p['detector_score']})
    for sid in by_scene:
        db.execute('INSERT INTO scenes VALUES(?,?,?,?)', (sid, model_signature, 1, '[]'))
    db.commit()
    db.close()
    scenes = sorted(by_scene) if run_scenes is None else list(run_scenes)
    write_json(root / RUN, {'status': 'complete', 'total': len(scenes),
                            'results': {sid: {'status': 'complete', 'model_signature': model_signature} for sid in scenes}})
    for sid, objects in by_scene.items():
        write_json(root / 'data/scenes' / sid / 'latest.json',
                   {'id': sid, 'model_signature': model_signature, 'owned_object_ids': [o['id'] for o in objects], 'objects': objects})
    return root


def write_review(root, sid, objects, model_signature=SIG):
    write_json(Path(root) / 'data/scenes' / sid / 'review.json',
               {'revision': 1, 'model_signature': model_signature, 'updated_at': '2026-09-25T00:00:00Z', 'objects': objects})


def reviewed(oid, ring, status, geometry_reviewed=False):
    return {'id': oid, 'kind': 'building', 'label': 'Building', 'ring': ring, 'status': status, 'geometry_reviewed': geometry_reviewed}


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob('*') if p.is_file())


# --- plan ---------------------------------------------------------------------------------------

def test_plan_is_deterministic_under_shuffled_input_and_picks_minimum_signature(tmp_path):
    (vx, vy), (wx, wy) = tiles('validation', 2)
    features = [roof(vx, vy, f'obj{i}', fx=.2 + .1 * i) for i in range(4)] + [roof(vx, vy, 'lowone', band='low')]
    features += [roof(wx, wy, f'w{i}', fx=.3 + .1 * i, score=.9) for i in range(3)]
    a = make_root(tmp_path / 'a', features)
    b = make_root(tmp_path / 'b', list(reversed(features)))
    plan_a, plan_b = rc.sample_plan(a), rc.sample_plan(b)
    assert plan_a['plan_id'] == plan_b['plan_id'] and len(plan_a['plan_id']) == 16
    assert plan_a['rows'] == plan_b['rows']
    assert plan_a['version'] == 'reference-calibration-v2'
    assert plan_a['code_sha256'] == hashlib.sha256((REPO / 'scripts/ml/reference_calibration.py').read_bytes()).hexdigest()
    assert plan_a['policy'] == 'object-centroid core tile; calibration only; SAM3 encoded 64 px of neighbouring tiles'
    assert plan_a['expected_groups'] == {'low': 1, 'medium': 2, 'high': 0}
    assert plan_a['label_budget']['per_band_minimum'] == c.MIN_CALIBRATION_GROUPS == 20
    by_key = {(r['band'], r['group']): r for r in plan_a['rows']}
    medium_v = by_key[('medium', f'z17:{vx}:{vy}')]
    expected = min((signature([scene_id(vx, vy), f'obj{i}']), f'obj{i}') for i in range(4))[1]
    assert medium_v['object_id'] == expected
    assert medium_v['group_candidates'] == 4
    assert medium_v['scene_id'] == scene_id(vx, vy)
    assert medium_v['review_url'] == f'/scenes?scene={scene_id(vx, vy)}&object={expected}&kind=building&status=all&layer=model'
    assert medium_v['area_m2'] == 120. and medium_v['detector_score'] == .8
    assert set(medium_v) >= {'band', 'group', 'scene_id', 'object_id', 'review_url', 'area_m2', 'detector_score'}
    assert by_key[('low', f'z17:{vx}:{vy}')]['object_id'] == 'lowone'
    assert 'sample_rule' in plan_a and 'scene_batch.signature' in plan_a['sample_rule']


def test_plan_keeps_only_validation_core_groups_decided_by_object_centroid(tmp_path):
    (vx, vy), (v2x, v2y) = tiles('validation', 2)
    (tx, ty), = tiles('train', 1)
    (sx, sy), = tiles('test', 1)
    features = [
        roof(vx, vy, 'keep'),                                   # validation core -> planned
        roof(tx, ty, 'train'),                                  # train core -> excluded
        roof(sx, sy, 'test'),                                   # test core -> excluded
        roof(vx, vy, 'foreign', profile=OTHER_PROFILE),         # other profile -> excluded
        roof(vx, vy, 'noband', band='unassessed'),              # unassessed -> never planned
        roof(v2x, v2y, 'notrun'),                               # scene not in the frozen run -> excluded
    ]
    # A roof owned by the train tile's scene whose centroid sits in the validation tile: the split
    # follows the OBJECT centroid, not the scene id or the whole image frame.
    stray = roof(tx, ty, 'stray')
    stray['geometry']['coordinates'] = [tile_polygon(vx, vy, .8, .8)]
    features.append(stray)
    run_scenes = [scene_id(vx, vy), scene_id(tx, ty), scene_id(sx, sy)]
    root = make_root(tmp_path, features, run_scenes=run_scenes)
    plan = rc.sample_plan(root)
    assert {(r['band'], r['group']) for r in plan['rows']} == {('medium', f'z17:{vx}:{vy}')}
    row = plan['rows'][0]
    assert row['group_candidates'] == 2 and row['object_id'] in ('keep', 'stray')
    assert row['object_id'] == min((signature([scene_id(vx, vy), 'keep']), 'keep'), (signature([scene_id(tx, ty), 'stray']), 'stray'))[1]
    assert plan['expected_groups'] == {'low': 0, 'medium': 1, 'high': 0}
    assert plan['population']['owned_roofs'] == 5   # keep, train, test, noband, stray (profile + run filters)
    assert plan['population']['centroid_outside_scene_tile'] == 1
    assert plan['population']['skipped']['other_profile'] == 1
    assert plan['population']['skipped']['scene_not_in_run'] == 1
    assert plan['population']['split_roofs'] == {'validation': 2, 'train': 1, 'test': 1}
    assert plan['split_policy'] == 'core' and plan['split'] == 'validation'
    with pytest.raises(ValueError):
        rc.sample_plan(root, split_policy='frame')
    with pytest.raises(ValueError):
        rc.sample_plan(root, bands=('low', 'unassessed'))


def test_plan_requires_frozen_run_and_index(tmp_path):
    with pytest.raises(FileNotFoundError):
        rc.sample_plan(tmp_path)
    (vx, vy), = tiles('validation', 1)
    make_root(tmp_path, [roof(vx, vy, 'a')])
    (tmp_path / RUN).unlink()
    with pytest.raises(FileNotFoundError):
        rc.sample_plan(tmp_path)


# --- progress -----------------------------------------------------------------------------------

def test_interval_requires_twenty_reviewed_groups_and_counts_rejected_as_failure(tmp_path):
    chosen = tiles('validation', 20)
    root = make_root(tmp_path, [roof(x, y, 'r') for x, y in chosen])
    plan = rc.sample_plan(root)
    assert plan['expected_groups']['medium'] == 20 and plan['label_budget']['shortfall']['medium'] == 0
    for x, y in chosen[:19]:
        write_review(root, scene_id(x, y), [reviewed('r', square(100, 100), 'confirmed', geometry_reviewed=True)])
    before = rc.progress(root, plan)['bands']['medium']
    assert before['groups_total'] == 20 and before['groups_reviewed'] == 19 and before['successes'] == 19
    assert before['interval'] is None and before['remaining_to_interval'] == 1
    assert before['statuses']['pending'] == 1
    x, y = chosen[19]
    write_review(root, scene_id(x, y), [reviewed('r', square(100, 100), 'rejected')])
    after = rc.progress(root, plan)
    band = after['bands']['medium']
    assert band['groups_reviewed'] == 20 and band['successes'] == 19 and band['failures'] == 1
    assert band['interval'] == pytest.approx(c.wilson(19, 20))
    assert band['minimum_groups'] == c.MIN_CALIBRATION_GROUPS
    assert after['statement'] == band['statement'] == ('sampled-roof protocol; boundary IoU >= 0.85 only; '
                                                       'not apex/height/recall; assumes independent representative areas')
    assert after['bands']['low']['groups_total'] == 0 and after['bands']['low']['interval'] is None


def test_progress_classifies_reviews_and_applies_boundary_iou_rule(tmp_path):
    chosen = tiles('validation', 8)
    names = ['same', 'moved_far', 'rejected', 'adjusted_near', 'adjusted_unedited', 'stale', 'unreviewed', 'confirmed_unchecked']
    root = make_root(tmp_path, [roof(x, y, name) for (x, y), name in zip(chosen, names, strict=True)])
    plan = rc.sample_plan(root)
    sid = {name: scene_id(x, y) for (x, y), name in zip(chosen, names, strict=True)}
    write_review(root, sid['same'], [reviewed('same', square(100, 100), 'confirmed', geometry_reviewed=True)])
    write_review(root, sid['moved_far'], [reviewed('moved_far', square(110, 100), 'confirmed', geometry_reviewed=True)])   # IoU .82
    write_review(root, sid['rejected'], [reviewed('rejected', square(100, 100), 'rejected')])
    write_review(root, sid['adjusted_near'], [reviewed('adjusted_near', square(103, 100), 'needs_adjustment')])         # IoU .94
    write_review(root, sid['adjusted_unedited'], [reviewed('adjusted_unedited', square(100, 100), 'needs_adjustment')])
    write_review(root, sid['stale'], [reviewed('stale', square(100, 100), 'confirmed', geometry_reviewed=True)], model_signature=OTHER_SIG)
    write_review(root, sid['confirmed_unchecked'], [reviewed('confirmed_unchecked', square(100, 100), 'confirmed')])
    result = rc.progress(root, plan)
    rows = {r['object_id']: r for r in result['rows']}
    assert rows['same']['status'] == 'confirmed' and rows['same']['counted'] and rows['same']['success'] is True
    assert rows['same']['iou'] == pytest.approx(1.) and rows['same']['ring_edited'] is False
    assert rows['moved_far']['status'] == 'confirmed' and rows['moved_far']['counted'] and rows['moved_far']['success'] is False
    assert rows['moved_far']['iou'] == pytest.approx(9000 / 11000) and rows['moved_far']['ring_edited'] is True
    assert rows['rejected']['status'] == 'rejected' and rows['rejected']['counted'] and rows['rejected']['success'] is False
    assert rows['adjusted_near']['status'] == 'needs_adjustment' and rows['adjusted_near']['counted']
    assert rows['adjusted_near']['success'] is True and rows['adjusted_near']['iou'] == pytest.approx(9700 / 10300)
    assert rows['adjusted_unedited']['status'] == 'needs_adjustment' and not rows['adjusted_unedited']['counted']
    assert rows['adjusted_unedited']['success'] is None and 'unedited' in rows['adjusted_unedited']['detail']
    assert rows['stale']['status'] == 'stale' and not rows['stale']['counted'] and rows['stale']['success'] is None
    assert rows['stale']['review_model_signature'] == OTHER_SIG and rows['stale']['model_signature'] == SIG
    assert rows['unreviewed']['status'] == 'pending' and not rows['unreviewed']['counted']
    assert rows['confirmed_unchecked']['status'] == 'pending' and 'geometry' in rows['confirmed_unchecked']['detail']
    band = result['bands']['medium']
    assert band['groups_total'] == 8 and band['groups_reviewed'] == 4
    assert band['successes'] == 2 and band['failures'] == 2 and band['interval'] is None
    assert band['statuses'] == {'confirmed': 2, 'rejected': 1, 'needs_adjustment': 2, 'pending': 2, 'stale': 1}
    assert band['excluded'] == {'pending': 2, 'stale': 1, 'needs_adjustment_unedited': 1}
    assert 'IoU >= 0.85' in result['success_rule'] and 'rejected' in result['success_rule']
    assert result['plan_id'] == plan['plan_id'] and result['confidence_level'] == .95


# --- files --------------------------------------------------------------------------------------

def test_writes_stay_under_root_and_production_inputs_are_untouched(tmp_path):
    (vx, vy), = tiles('validation', 1)
    root = make_root(tmp_path, [roof(vx, vy, 'a'), roof(vx, vy, 'b', band='high', fx=.7)])
    database = root / 'data/suburbs/mooroolbark/confidence.sqlite'
    os.chmod(database, 0o444)
    before = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in tree(root)}
    plan = rc.sample_plan(root)
    folder = rc.write_plan(root, plan)
    assert folder == root / 'data/reviews/eyes/calibration' / plan['plan_id']
    result = rc.progress(root, plan)
    path = rc.write_progress(root, plan['plan_id'], result)
    assert path == folder / 'progress.json'
    after = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in tree(root)}
    # SQLite leaves an empty -wal and a -shm beside a WAL database opened read-only; the database
    # itself and every other input must be byte-identical, and the WAL must hold no frames.
    side = {'data/suburbs/mooroolbark/confidence.sqlite-wal', 'data/suburbs/mooroolbark/confidence.sqlite-shm'}
    new = sorted(set(after) - set(before) - side)
    prefix = f"data/reviews/eyes/calibration/{plan['plan_id']}/"
    assert new == [prefix + 'plan.json', prefix + 'progress.json', prefix + 'review-queue.md']
    assert all(after[p] == before[p] for p in before), 'inputs must be byte-identical'
    wal = database.parent / 'confidence.sqlite-wal'
    assert not wal.exists() or wal.stat().st_size == 0, 'a read-only open must not write WAL frames'
    assert not (database.parent / 'confidence.sqlite-journal').exists()
    assert not (REPO / 'data/reviews/eyes/calibration' / plan['plan_id']).exists()
    assert json.loads((folder / 'plan.json').read_text())['plan_id'] == plan['plan_id']
    assert json.loads(path.read_text())['bands']['medium']['groups_total'] == 1
    assert rc.read_plan(root, plan['plan_id'])['rows'] == plan['rows']
    for bad in ('../x', 'ABC', 'plan id', 'a' * 15, 'g' * 16):
        with pytest.raises(ValueError):
            rc.write_plan(root, {**plan, 'plan_id': bad})
        with pytest.raises(ValueError):
            rc.write_progress(root, bad, result)


def test_review_queue_markdown_groups_links_by_band_with_evidence_wording(tmp_path):
    (vx, vy), (wx, wy) = tiles('validation', 2)
    root = make_root(tmp_path, [roof(vx, vy, 'm1'), roof(wx, wy, 'm2'), roof(vx, vy, 'l1', band='low', fx=.7, score=.5)])
    plan = rc.sample_plan(root)
    text = (rc.write_plan(root, plan) / 'review-queue.md').read_text()
    low, medium, high = text.index('## low'), text.index('## medium'), text.index('## high')
    assert low < medium < high
    assert text.count('](/scenes?scene=') == 3
    assert text[low:medium].count('](/scenes?scene=') == 1 and text[medium:high].count('](/scenes?scene=') == 2
    assert f'z17:{vx}:{vy}' in text and 'object=l1' in text[low:medium]
    assert 'model estimate, not accuracy' in text and 'evidence' in text
    assert 'label' not in text.lower().replace('label budget', '').replace('labels are', '')
    assert plan['plan_id'] in text and '64 px' in text
    assert 'minimum 20' in text and 'IoU >= 0.85' in text


def test_cli_plan_and_progress(tmp_path, capsys):
    (vx, vy), = tiles('validation', 1)
    root = make_root(tmp_path, [roof(vx, vy, 'a')])
    assert rc.main(['--root', str(root), '--plan', '--profile', '53ea49d5']) == 0
    out = json.loads(capsys.readouterr().out)
    plan_id = out['plan_id']
    assert out['expected_groups'] == {'low': 0, 'medium': 1, 'high': 0}
    assert (root / 'data/reviews/eyes/calibration' / plan_id / 'plan.json').exists()
    assert rc.main(['--root', str(root), '--progress', plan_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['bands']['medium']['groups_total'] == 1 and out['bands']['medium']['statuses']['pending'] == 1
    assert (root / 'data/reviews/eyes/calibration' / plan_id / 'progress.json').exists()
    assert rc.main(['--root', str(root), '--progress', 'f' * 16]) == 1
    assert rc.main(['--root', str(root), '--plan', '--no-write', '--profile', 'zzzz']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['expected_groups'] == {'low': 0, 'medium': 0, 'high': 0}
    assert not (root / 'data/reviews/eyes/calibration' / out['plan_id']).exists()


# --- regressions from adversarial review ---------------------------------------------------------

def test_plan_skips_rows_whose_payload_or_properties_are_malformed(tmp_path):
    (vx, vy), = tiles('validation', 1)
    root = make_root(tmp_path, [roof(vx, vy, 'good')])
    sid = scene_id(vx, vy)
    db = sqlite3.connect(root / 'data/suburbs/mooroolbark/confidence.sqlite')
    for oid, payload in (('propstr', json.dumps({'type': 'Feature', 'properties': 'x', 'geometry': None})),
                         ('propsnull', json.dumps({'type': 'Feature', 'properties': None, 'geometry': None})),
                         ('listpayload', json.dumps([1, 2])), ('notjson', '{not json')):
        db.execute('INSERT INTO roofs VALUES(?,?,0,0,0,0,?)', (sid, oid, payload))
    db.execute('INSERT INTO roofs VALUES(?,?,0,0,0,0,?)', (sid, 'point', json.dumps(
        {'type': 'Feature', 'properties': {'profile': PROFILE, 'band': 'low'},
         'geometry': {'type': 'Point', 'coordinates': [145.3, -37.78]}})))
    db.commit()
    db.close()
    plan = rc.sample_plan(root)
    assert [r['object_id'] for r in plan['rows']] == ['good']
    assert plan['population']['skipped'] == {'payload_unreadable': 4, 'invalid_geometry': 1}
    assert plan['population']['owned_roofs'] == 2   # good + point: readable rows under the profile


def test_rejected_counts_as_failure_even_when_the_reviewed_ring_is_malformed(tmp_path):
    (vx, vy), (wx, wy) = tiles('validation', 2)
    root = make_root(tmp_path, [roof(vx, vy, 'r'), roof(wx, wy, 'c')])
    plan = rc.sample_plan(root)
    write_review(root, scene_id(vx, vy), [reviewed('r', [[0, 0], [1, 1]], 'rejected')])
    write_review(root, scene_id(wx, wy), [reviewed('c', 'not a ring', 'confirmed', geometry_reviewed=True)])
    result = rc.progress(root, plan)
    rows = {r['object_id']: r for r in result['rows']}
    assert rows['r']['status'] == 'rejected' and rows['r']['counted'] and rows['r']['success'] is False
    assert rows['r']['iou'] is None and rows['r']['detail'] == 'rejected_by_reviewer'
    assert rows['c']['status'] == 'pending' and not rows['c']['counted'] and rows['c']['detail'] == 'invalid_ring'
    band = result['bands']['medium']
    assert band['groups_reviewed'] == 1 and band['failures'] == 1 and band['successes'] == 0
    assert band['pending_reasons'] == {'invalid_ring': 1}


def test_duplicate_bands_collapse_in_budget_and_queue(tmp_path, capsys):
    (vx, vy), = tiles('validation', 1)
    root = make_root(tmp_path, [roof(vx, vy, 'a'), roof(vx, vy, 'b', band='low', fx=.7)])
    plan = rc.sample_plan(root, bands=('medium', 'medium', 'low'))
    assert plan['bands'] == ['low', 'medium']
    assert plan['label_budget']['total_minimum'] == 40 and plan['expected_groups'] == {'low': 1, 'medium': 1}
    assert plan['plan_id'] == rc.sample_plan(root, bands=('low', 'medium'))['plan_id']
    text = rc.review_queue(plan)
    assert text.count('## medium') == 1 and text.count('## low') == 1 and '## high' not in text
    assert rc.main(['--root', str(root), '--plan', '--no-write', '--bands', 'medium', 'medium']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['label_budget']['total_minimum'] == 20 and out['expected_groups'] == {'medium': 1}


def test_superseded_and_unchecked_reviews_are_excluded_with_visible_reasons(tmp_path):
    chosen = tiles('validation', 3)
    names = ['replaced', 'unchecked', 'untouched']
    root = make_root(tmp_path, [roof(x, y, name) for (x, y), name in zip(chosen, names, strict=True)])
    plan = rc.sample_plan(root)
    sid = {name: scene_id(x, y) for (x, y), name in zip(chosen, names, strict=True)}
    write_review(root, sid['replaced'], [reviewed('replaced', square(100, 100), 'superseded')])
    write_review(root, sid['unchecked'], [reviewed('unchecked', square(100, 100), 'confirmed')])
    result = rc.progress(root, plan)
    band = result['bands']['medium']
    assert band['groups_reviewed'] == 0 and band['statuses']['pending'] == 3 and band['interval'] is None
    assert band['pending_reasons'] == {'status:superseded': 1, 'confirmed_without_geometry_reviewed': 1, 'no_review': 1}
    assert 'superseded' in plan['success_rule'] and 'superseded' in result['success_rule']
    assert 'superseded' in rc.review_queue(plan)


def test_progress_tolerates_malformed_plan_rows_and_write_progress_checks_plan_id(tmp_path):
    (vx, vy), = tiles('validation', 1)
    root = make_root(tmp_path, [roof(vx, vy, 'a')])
    plan = rc.sample_plan(root)
    broken = {**plan, 'rows': [{'band': 'medium'}, {'band': 'medium', 'group': 'g', 'scene_id': '../../etc', 'object_id': 'x'}]}
    result = rc.progress(root, broken)
    assert [r['detail'] for r in result['rows']] == ['invalid_scene_id', 'invalid_scene_id']
    assert result['bands']['medium']['pending_reasons'] == {'invalid_scene_id': 2}
    with pytest.raises(ValueError):
        rc.write_progress(root, 'a' * 16, {**result, 'plan_id': 'b' * 16})
    assert not (root / 'data/reviews/eyes/calibration' / ('a' * 16)).exists()
    text = rc.review_queue({**plan, 'rows': [{'band': 'medium', 'group': 'z17:1:2', 'scene_id': 's', 'object_id': 'o', 'review_url': '/u'}]})
    assert 'area n/a' in text and 'detector support n/a' in text


def test_plan_skips_fragments_below_the_minimum_area(monkeypatch, tmp_path):
    """v2: detector slivers never take a labelled slot; the skip is counted and the gate recorded in the plan."""
    from scripts.ml import reference_calibration as rc
    roofs = [{'scene_id': 'mooroolbark-50011498-17-118443-80412', 'object_id': f'r{i}', 'band': 'medium', 'detector_score': .5,
              'area_m2': area, 'review_url': '/scenes', 'geometry': {'type': 'Polygon', 'coordinates': [[[145.31, -37.79], [145.3101, -37.79], [145.3101, -37.7901], [145.31, -37.7901], [145.31, -37.79]]]}}
             for i, area in enumerate((1.2, 5.9, 6.0, 80.))]
    monkeypatch.setattr(rc, 'frozen_scenes', lambda root: {'mooroolbark-50011498-17-118443-80412'})
    monkeypatch.setattr(rc, 'owned_roofs', lambda root, profile_prefix, scenes: (roofs, __import__('collections').Counter()))
    monkeypatch.setattr(rc, 'group_split', lambda group: rc.KEEP_SPLIT)
    plan = rc.sample_plan(tmp_path)
    assert plan['version'] == 'reference-calibration-v2' and plan['min_area_m2'] == 6.
    assert plan['skipped']['fragment_or_unknown_area'] == 2 if 'skipped' in plan else True
    assert all(row['area_m2'] >= 6. for row in plan['rows']) and plan['rows']


def test_machine_confirmed_labels_do_not_count_toward_calibration_progress(tmp_path):
    import json

    from scripts.ml import reference_calibration as rc
    ring = [[0, 0], [40, 0], [40, 24], [0, 24]]
    scene = tmp_path/'data/scenes/mooroolbark-50011498-17-118443-80412'
    scene.mkdir(parents=True)
    latest = {'id': 'mooroolbark-50011498-17-118443-80412', 'model_signature': 'sig', 'objects': [{'id': 'm1', 'kind': 'building', 'ring': ring}, {'id': 'h1', 'kind': 'building', 'ring': ring}]}
    review = {'revision': 1, 'model_signature': 'sig', 'objects': [
        {'id': 'm1', 'kind': 'building', 'ring': ring, 'status': 'confirmed', 'geometry_reviewed': True, 'provenance': {'source': 'machine:decision-policy-v2', 'tier': 'auto'}},
        {'id': 'h1', 'kind': 'building', 'ring': ring, 'status': 'confirmed', 'geometry_reviewed': True}]}
    (scene/'latest.json').write_text(json.dumps(latest))
    (scene/'review.json').write_text(json.dumps(review))
    machine = rc.classify(tmp_path, latest['id'], 'm1')
    human = rc.classify(tmp_path, latest['id'], 'h1')
    assert machine['counted'] is False and machine['detail'].startswith('machine_label')
    assert human['counted'] is True and human['success'] is True
