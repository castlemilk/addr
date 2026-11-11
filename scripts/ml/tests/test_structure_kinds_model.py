"""Signals and classifier head: oriented ribs, LAB variance, continuity, tile-grouped evaluation, label policy."""
import json
import math
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from scripts.ml import eyes_index
from scripts.ml import structure_kinds as sk
from scripts.ml import structure_kinds_model as model
from scripts.ml import structure_kinds_review as review
from scripts.ml import structure_kinds_signals as signals

sys.path.insert(0, str(Path(__file__).parent))
from test_structure_kinds import build_scene  # noqa: E402


@pytest.fixture
def indexed(tmp_path):
    scene, canvas, png = build_scene()
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    (folder/f"{scene['image_sha256']}.png").write_bytes(png)
    sk.run(tmp_path, [scene['id']])
    return scene, canvas


def test_oriented_ribs_read_the_period_along_a_rotated_box():
    px_m = 1/4.24
    gray = np.zeros((160, 160), np.float32)
    yy, xx = np.mgrid[0:160, 0:160]
    theta = math.radians(30)
    u = xx*math.cos(theta)+yy*math.sin(theta)          # coordinate along the box's long axis
    v = -xx*math.sin(theta)+yy*math.cos(theta)
    mask = (np.abs(u-100) < 45) & (np.abs(v-20) < 8)    # 21 m long, 3.8 m wide box at 30 degrees
    gray[:] = 150+50*np.sin(2*math.pi*u*px_m/1.0)      # 1 m ribs along the long axis
    out = signals.oriented_ribs(gray, mask, 30., px_m)
    assert out['period_along_m'] is not None and abs(out['period_along_m']-1.0) < .25
    assert out['axis_gradient_share'] > .8


def test_lab_variance_ratio_and_multidate_shape():
    inside = np.zeros((20, 20), bool)
    inside[5:15, 5:15] = True
    ring = ~inside
    lum = np.random.default_rng(0).normal(100, 10, (20, 20)).astype(np.float32)
    lum[inside] = 120.
    assert signals.lab_variance_ratio(lum, inside, ring) == 0.
    assert signals.multidate_support(Path('/nonexistent'), 'x') == {}


def test_signals_rows_follow_hint_rows_and_keep_other_versions(tmp_path, indexed):
    scene, canvas = indexed
    rows, written = signals.run(tmp_path, [scene['id']])
    hinted = {r['object_id'] for r in review.hint_rows(tmp_path)}
    assert written == len(rows) == len(hinted) and {r['object_id'] for r in rows} == hinted
    by = {r['object_id']: r['payload'] for r in rows}
    assert by['container']['ribs_oriented']['period_along_m'] is not None
    assert by['garage']['roof_continuity'] is not None and 'continuous_roof_hint' in by['garage']['roof_continuity']
    assert by['shed']['roof_continuity'] is None and by['shed']['image'] == 'original'
    assert by['pergola']['lab_variance_ratio_original'] > by['shed']['lab_variance_ratio_original']
    with closing(sqlite3.connect(eyes_index.path(tmp_path))) as db:
        counts = dict(db.execute('SELECT version, COUNT(*) FROM object_quality GROUP BY version').fetchall())
    assert counts == {sk.VERSION: len(hinted), signals.VERSION: len(hinted)}


def test_feature_vector_marks_missing_values_and_is_stable():
    hint = {'evidence': {'geometry': {'area_m2': 20., 'rect_width_m': None}, 'context': {'attached_to_larger_roof': True}}}
    x, names = model.feature_vector(hint, None)
    assert len(x) == len(names) == 2*len(model.NUMERIC)+len(model.BOOLEAN)+2*len(model.SIGNAL_NUMERIC)+len(model.SIGNAL_BOOLEAN)
    lookup = dict(zip(names, x, strict=True))
    assert lookup['geometry.area_m2'] == 20. and lookup['geometry.area_m2.missing'] == 0.
    assert lookup['geometry.rect_width_m.missing'] == 1. and lookup['context.attached_to_larger_roof'] == 1.
    x2, _ = model.feature_vector(hint, None, np.ones(4))
    assert len(x2) == len(x)+4


def test_softmax_head_separates_synthetic_classes():
    rng = np.random.default_rng(1)
    X = np.vstack([rng.normal(0, 1, (40, 3)), rng.normal(4, 1, (40, 3))])
    y = ['a']*40+['b']*40
    head = model.SoftmaxHead(['a', 'b']).fit(X, y)
    probs = head.predict_proba(X)
    assert (np.asarray(['a', 'b'])[probs.argmax(axis=1)] == np.asarray(y)).mean() > .95
    again = model.SoftmaxHead.from_json(head.to_json())
    assert np.allclose(again.predict_proba(X), probs)


def test_dataset_excludes_agent_labels_unless_allowed_and_evaluation_needs_tiles(tmp_path, indexed):
    scene, _ = indexed
    observations = [{'scene_id': scene['id'], 'object_id': o, 'observed': k} for o, k in
                    (('container', 'shipping-container'), ('shed', 'shed'), ('garage', 'garage'), ('tank', 'water-tank'))]
    review.record(tmp_path, observations, reviewer='agent:test', tier='agent')
    assert len(model.dataset(tmp_path)['y']) == 0
    smoke = model.dataset(tmp_path, allow_agent=True)
    assert len(smoke['y']) == 4 and smoke['label_policy'] == 'smoke'
    report = model.evaluate(smoke)
    assert report['status'] == 'insufficient data for cross-validation' and report['tiles'] == 1
    review.record(tmp_path, observations[:1], reviewer='human:x', tier='human-confirmed')
    human = model.dataset(tmp_path)
    assert human['y'] == ['shipping-container'] and human['label_policy'] == 'human'


def test_grouped_folds_keep_a_tile_in_one_fold():
    groups = ['t1']*3+['t2']*2+['t3']*4+['t4']
    fold = model.grouped_folds(groups, 2)
    for tile in set(groups):
        assert len({f for f, g in zip(fold, groups, strict=True) if g == tile}) == 1
