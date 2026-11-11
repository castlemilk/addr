from contextlib import closing

import pytest
from scripts.ml import eyes_index, scene_lineage


def square(x, y=0., size=10.):
    return [[x, y], [x+size, y], [x+size, y+size], [x, y+size]]


def obj(oid, ring, kind='building', raw=None, status='candidate'):
    item = {'id': oid, 'kind': kind, 'status': status, 'ring': ring}
    if raw is not None:
        item['boundary_refinement'] = {'raw_ring': raw}
    return item


def by_match(rows, match):
    return [r for r in rows if r['match'] == match]


def test_id_equality_wins_before_geometry():
    base = {'objects': [obj('a', square(0))]}
    cand = {'objects': [obj('a', square(3)), obj('x', square(0))]}
    rows = scene_lineage.link(base, cand)
    paired, = by_match(rows, 'id')
    assert paired['baseline_object_id'] == paired['candidate_object_id'] == 'a'
    assert paired['id_stable'] is True and paired['kind'] == 'building'
    assert paired['raw_ring_iou'] == pytest.approx(7/13, abs=1e-4), 'IoU is still reported for id matches'
    gained, = by_match(rows, 'gained')
    assert gained['candidate_object_id'] == 'x' and gained['baseline_object_id'] is None


def test_raw_ring_iou_pairs_renamed_objects_and_ignores_refined_ring():
    # Final rings differ (refinement changed); the raw detector masks agree.
    base = {'objects': [obj('a', square(0), raw=square(0))]}
    cand = {'objects': [obj('b', square(4), raw=square(.2))]}
    row, = scene_lineage.link(base, cand)
    assert row['match'] == 'iou' and row['id_stable'] is False
    assert row['baseline_object_id'] == 'a' and row['candidate_object_id'] == 'b'
    assert row['raw_ring_iou'] >= .8
    # Identical final rings but disagreeing raw masks are not the same detection.
    base = {'objects': [obj('a', square(0), raw=square(0))]}
    cand = {'objects': [obj('b', square(0), raw=square(3))]}
    rows = scene_lineage.link(base, cand)
    assert {r['match'] for r in rows} == {'lost', 'gained'}


def test_hungarian_assignment_beats_greedy_best_first():
    # IoU of equal squares shifted by d is (10-d)/(10+d): .5 -> .905, 1 -> .818, 2.5 -> .6.
    base = {'objects': [obj('b1', square(0)), obj('b2', square(1.5))]}
    cand = {'objects': [obj('c1', square(.5)), obj('c2', square(-1))]}
    rows = scene_lineage.link(base, cand)
    pairs = {(r['baseline_object_id'], r['candidate_object_id']) for r in by_match(rows, 'iou')}
    assert pairs == {('b1', 'c2'), ('b2', 'c1')}, 'Greedy b1-c1 would leave b2 lost'
    assert not by_match(rows, 'lost') and not by_match(rows, 'gained')


def test_kinds_never_cross_and_superseded_objects_are_excluded():
    base = {'objects': [obj('t', square(0), kind='tree'), obj('old', square(20), status='superseded')]}
    cand = {'objects': [obj('b', square(0), kind='building'), obj('old', square(20))]}
    rows = scene_lineage.link(base, cand)
    assert len(rows) == 3
    assert sorted((r['match'], r['kind']) for r in rows) == [('gained', 'building'), ('gained', 'building'), ('lost', 'tree')]
    assert all(r['baseline_object_id'] != 'old' for r in rows)


def test_invalid_rings_are_lost_or_gained_not_crashing():
    bowtie = [[0, 0], [10, 10], [10, 0], [0, 10]]
    base = {'objects': [obj('a', [[0, 0], [1, 1]]), obj('b', bowtie)]}
    cand = {'objects': [obj('c', square(0))]}
    rows = scene_lineage.link(base, cand)
    assert {r['match'] for r in rows} <= {'lost', 'gained', 'iou'}
    assert len(by_match(rows, 'lost')) >= 1


def test_stats_carry_denominators():
    base = {'objects': [obj('a', square(0)), obj('b', square(20)), obj('c', square(40))]}
    cand = {'objects': [obj('a', square(0)), obj('z', square(20.2)), obj('n', square(80))]}
    stats = scene_lineage.stats(scene_lineage.link(base, cand))
    assert stats['paired'] == 2 and stats['paired_by_id'] == 1 and stats['paired_by_iou'] == 1
    assert stats['gained'] == 1 and stats['lost'] == 1
    assert stats['baseline_objects'] == 3 and stats['candidate_objects'] == 3
    assert stats['id_stability_rate'] == pytest.approx(1/3)
    assert stats['by_kind']['building']['paired'] == 2
    assert scene_lineage.stats([])['id_stability_rate'] is None


def test_write_replaces_rows_for_experiment_and_scene(tmp_path):
    base = {'model_signature': 'm0', 'objects': [obj('a', square(0)), obj('b', square(20))]}
    cand = {'model_signature': 'm1', 'objects': [obj('a', square(0)), obj('n', square(80))]}
    rows = scene_lineage.link(base, cand)
    assert scene_lineage.write(tmp_path, 'exp-1', 'scene-1', rows) == 3
    assert scene_lineage.write(tmp_path, 'exp-1', 'scene-1', rows) == 3
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        stored = eyes_index.rows(db, 'lineage', experiment_id='exp-1', scene_id='scene-1')
    assert len(stored) == 3, 'Re-linking replaces rows; missing sides never duplicate'
    lost, = [r for r in stored if r['payload']['match'] == 'lost']
    assert lost['baseline_object_id'] == 'b' and lost['candidate_object_id'] == ''
    assert lost['payload']['candidate_object_id'] is None
    assert lost['version'] == scene_lineage.VERSION
    assert len(lost['payload']['code_sha256']) == 64 and lost['payload']['computed_at']
    assert scene_lineage.write(tmp_path, 'exp-1', 'scene-1', rows[:1]) == 1
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        assert len(eyes_index.rows(db, 'lineage', experiment_id='exp-1', scene_id='scene-1')) == 1
    with pytest.raises(ValueError):
        scene_lineage.write(tmp_path, '../bad', 'scene-1', rows)
