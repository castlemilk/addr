"""training.edit_diff: model-estimate vs reviewer-outline rows from synthetic scenes (read-only over data/scenes).

Runs in the main ML venv; nothing here imports torch.
"""
import json
import math

from scripts.ml.tests.test_training_export import PX_M, TILE, building, square, write_scene
from scripts.ml.training import edit_diff

HOLE = [[110, 130], [120, 130], [120, 140], [110, 140]]


def make_repo(root):
    x, y = TILE
    model = [building('conf', square(20, 20)), building('moved', square(100, 20)), building('holes', square(100, 120)),
             building('machine', square(200, 20)), building('bad', square(300, 20)), building('sup', square(200, 120)),
             building('cand', square(300, 220))]
    review = [building('conf', square(20, 20), 'confirmed', geometry_reviewed=True),
              building('moved', square(104, 24), 'needs_adjustment'),
              building('holes', square(100, 120), 'needs_adjustment', holes=[HOLE]),
              building('machine', square(204, 24), 'confirmed', geometry_reviewed=True,
                       provenance={'source': 'machine:decision-policy-v2', 'human_confirmed': True}),
              building('manual', square(20, 220), 'confirmed', geometry_reviewed=True),
              building('bad', square(304, 24), 'rejected'),
              building('sup', square(204, 124), 'superseded'),
              building('cand', square(304, 224), 'candidate')]
    write_scene(root, 'scene-a', (x + .5, y + .5), model, review)
    write_scene(root, 'scene-noreview', (x + 1.5, y + .5), [building('x', square(20, 20))])
    # The snapshot the reviewer saw shows another image: nothing in this scene can be paired.
    scene = write_scene(root, 'scene-frame', (x + 2.5, y + .5), [building('f', square(20, 20))],
                        [building('f', square(24, 24), 'needs_adjustment')])
    snapshot = root / 'data/scenes/scene-frame/models' / f"{scene['model_signature']}.json"
    snapshot.write_text(json.dumps({**scene, 'image_sha256': 'b' * 64}))
    # The snapshot is gone and latest.json is another model: an unverified pair with null geometry.
    write_scene(root, 'scene-unverified', (x + 3.5, y + .5), [building('u', square(20, 20))],
                [building('u', square(24, 24), 'needs_adjustment')])
    review_path = root / 'data/scenes/scene-unverified/review.json'
    review_path.write_text(json.dumps({**json.loads(review_path.read_text()), 'model_signature': 'f' * 64}))
    write_scene(root, 'qa_scratch', (x + 4.5, y + .5), [building('q', square(20, 20))],
                [building('q', square(24, 24), 'needs_adjustment')])
    return root


def snapshot(root):
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in sorted((root / 'data/scenes').rglob('*')) if p.is_file()}


def test_rows_pair_every_reviewed_building_with_the_estimate_the_reviewer_saw(tmp_path):
    root = make_repo(tmp_path)
    before = snapshot(root)
    rows, counters = edit_diff.build(root)
    assert snapshot(root) == before, 'data/scenes must be untouched'
    by_id = {(r['scene_id'], r['object_id']): r for r in rows}
    assert set(by_id) == {('scene-a', 'conf'), ('scene-a', 'moved'), ('scene-a', 'holes'), ('scene-a', 'machine'),
                          ('scene-a', 'manual'), ('scene-unverified', 'u')}

    conf = by_id[('scene-a', 'conf')]
    assert (conf['tier'], conf['registry_provenance'], conf['pair'], conf['edit_kind']) == ('T1', 'review:confirmed', 'snapshot', 'none')
    assert conf['iou'] == 1. and conf['hausdorff_m'] == 0. and not conf['edited'] and conf['edited_at_revision'] is None
    assert conf['exceeds_vertex_tolerance'] is False and conf['is_rectangle'] and conf['boundary_f_0_5m'] == 1.

    moved = by_id[('scene-a', 'moved')]
    assert (moved['tier'], moved['registry_provenance'], moved['edit_kind']) == ('T2', 'review:needs_adjustment_edited', 'outline')
    assert moved['ring_changed'] and not moved['holes_changed'] and moved['edited']
    assert abs(moved['hausdorff_m'] - 4 * math.sqrt(2) * PX_M) < 1e-6 and abs(moved['iou'] - 1296 / 1904) < 1e-6
    assert moved['vertex_delta'] == 0 and moved['exceeds_vertex_tolerance'] is True and moved['edited_at_revision'] == 1
    assert abs(moved['centroid_shift_m'] - math.sqrt(2)) < 1e-6 and abs(moved['pixel_size_m'] - PX_M) < 1e-9
    assert moved['authority_blocker'] is None and not moved['human_confirmed'] and moved['geometry'] == 'ok'

    holes = by_id[('scene-a', 'holes')]
    assert (holes['tier'], holes['edit_kind']) == ('T2', 'holes_only') and not holes['ring_changed'] and holes['holes_changed']
    assert holes['n_holes_human'] == 1 and holes['n_holes_model'] == 0 and holes['iou'] < 1. and holes['edited_at_revision'] == 1

    machine = by_id[('scene-a', 'machine')]
    assert (machine['tier'], machine['registry_provenance'], machine['authority_blocker']) == ('T3', 'review:machine_label', 'machine_label')
    assert machine['human_confirmed'] is True and machine['provenance_source'] == 'machine:decision-policy-v2'

    manual = by_id[('scene-a', 'manual')]
    assert (manual['pair'], manual['edit_kind'], manual['tier']) == ('manual', 'manual', 'T1')
    assert manual['iou'] is None and manual['hausdorff_m'] is None and manual['n_holes_model'] is None
    assert manual['geometry'] == 'model_absent' and manual['is_rectangle'] is True

    unverified = by_id[('scene-unverified', 'u')]
    assert (unverified['pair'], unverified['tier'], unverified['registry_provenance']) == ('unverified', 'T3', 'review:needs_adjustment_unverified_model')
    assert unverified['iou'] is None and unverified['geometry'] == 'model_unverified'

    assert counters['n_scenes_seen'] == 4 and counters['n_scenes_with_review'] == 3 and counters['n_scenes_frame_mismatch'] == 1
    assert counters['excluded:rejected'] == 1 and counters['excluded:superseded'] == 1 and counters['excluded:candidate'] == 1
    assert all('accuracy' not in key for row in rows for key in row)


def test_summary_and_cli_write_only_under_out(tmp_path, capsys):
    root = make_repo(tmp_path)
    before = snapshot(root)
    assert edit_diff.main(['--root', str(root), '--out', 'data/reviews/eyes/training/edit-diff', '--summary']) == 0
    assert snapshot(root) == before
    out = root / 'data/reviews/eyes/training/edit-diff'
    summary = json.loads((out / 'summary.json').read_text())
    printed = json.loads(capsys.readouterr().out)
    assert printed['files'] == summary['files'] and 'not accuracy' in summary['note']
    lines = [json.loads(line) for line in (out / 'rows.jsonl').read_text().splitlines()]
    assert len(lines) == summary['n_rows'] == 6
    assert summary['by_tier'] == {'T1': 2, 'T2': 2, 'T3': 2}, 'conf and manual T1; moved and holes T2; machine and unverified T3'
    assert summary['by_pair'] == {'manual': 1, 'snapshot': 4, 'unverified': 1}
    assert summary['by_edit_kind'] == {'holes_only': 1, 'manual': 1, 'none': 1, 'outline': 3}
    assert summary['n_edited'] == 5 and summary['n_confirmed_as_drawn'] == 1 and summary['n_holes_only_changed'] == 1
    assert summary['n_machine_label_rows'] == 1 and summary['n_scenes_frame_mismatch'] == 1
    assert summary['excluded'] == {'candidate': 1, 'rejected': 1, 'superseded': 1}
    pairs = summary['edited_pairs']
    assert pairs['n'] == 2, 'moved and holes: human tiers with a verified pair; machine (T3), manual and unverified stay out'
    assert pairs['n_hausdorff_ge_0_45_m'] == 2 and pairs['n_iou_lt_0_9'] == 1   # the hole ring sits 10 px (2.5 m) from the model boundary
    assert pairs['iou_median'] is not None and pairs['hausdorff_m_median'] is not None
    assert [w['object_id'] for w in summary['worst_by_hausdorff']] == ['holes', 'moved']
    assert edit_diff.summarize([], {})['edited_pairs']['iou_median'] is None
    assert 'accuracy' not in json.dumps({k: v for k, v in summary.items() if k != 'note'})
    # A restricted run over a scene without review.json writes an empty row file, never a zero median.
    assert edit_diff.main(['--root', str(root), '--out', 'data/reviews/eyes/training/edit-diff-empty', '--scene-ids', 'scene-noreview']) == 0
    empty = json.loads((root / 'data/reviews/eyes/training/edit-diff-empty/summary.json').read_text())
    assert empty['n_rows'] == 0 and empty['edited_pairs'] == {'n': 0, 'iou_median': None, 'hausdorff_m_median': None,
                                                             'human_vertex_to_model_median_m_median': None,
                                                             'boundary_f_0_5m_median': None, 'vertex_delta_median': None,
                                                             'n_hausdorff_ge_0_45_m': 0, 'n_iou_lt_0_9': 0}
