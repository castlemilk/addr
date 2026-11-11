"""review_edit_diffs: model-outline vs reviewer-outline rows from a synthetic scene (read-only over data/scenes).

Runs in the main ML venv; nothing here imports torch.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image
from scripts.ml import review_edit_diffs as diffs
from scripts.ml.tiles import tile_xy_to_lonlat

SIZE = 400
PX_M = .25
TILE = (118442, 80417)


def frame():
    lon, lat = tile_xy_to_lonlat(TILE[0] + .5, TILE[1] + .5, 17)
    half = SIZE / 2 * PX_M
    return {'centroid': {'lon': lon, 'lat': lat}, 'image_width': SIZE, 'image_height': SIZE,
            'pixel_to_meters': [[PX_M, 0], [0, -PX_M], [-half, half]],
            'meters_to_pixel': [[1 / PX_M, 0], [0, -1 / PX_M], [SIZE / 2, SIZE / 2]]}


def square(x, y, size=40):
    return [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]


def obj(oid, ring, status='candidate', kind='building', **extra):
    return {'id': oid, 'kind': kind, 'label': kind.title(), 'ring': ring, 'status': status,
            'geometry_reviewed': False, 'source': 'sam3-text-onnx', 'score': .8, **extra}


def write_scene(root, scene_id, objects, review_objects=None, history=None, review_signature=None, revision=2):
    pixels = np.random.default_rng(1).integers(0, 255, (SIZE, SIZE, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format='PNG')
    data = buffer.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    scene = {'schema_version': 1, 'id': scene_id, **frame(), 'image_sha256': sha, 'objects': objects, 'groups': []}
    signature = hashlib.sha256(json.dumps(scene, sort_keys=True).encode()).hexdigest()
    scene['model_signature'] = signature
    folder = root / 'data/scenes' / scene_id
    (folder / 'models').mkdir(parents=True)
    (folder / f'{sha}.png').write_bytes(data)
    (folder / 'latest.json').write_text(json.dumps(scene))
    (folder / 'models' / f'{signature}.json').write_text(json.dumps(scene))
    if review_objects is not None:
        review = {'revision': revision, 'model_signature': review_signature or signature, 'objects': review_objects,
                  'updated_at': '2026-09-29T02:00:00Z', 'updated_by': 'local'}
        if history is not None:
            review['history'] = history
        (folder / 'review.json').write_text(json.dumps(review))
    return scene


HOLE = [[110, 130], [120, 130], [120, 140], [110, 140]]


def make_repo(root):
    model = [obj('conf', square(20, 20)), obj('moved', square(100, 20)), obj('holes', square(100, 120)),
             obj('machine', square(200, 20)), obj('bad', square(300, 20)), obj('bad_same', square(300, 120)),
             obj('sup', square(200, 120)), obj('cand', square(300, 220)), obj('tree', square(20, 300), kind='tree')]
    seeded = [obj('conf', square(20, 20), 'confirmed', geometry_reviewed=True),
              obj('moved', square(100, 20), 'candidate'),
              obj('holes', square(100, 120), 'confirmed', geometry_reviewed=True, holes=[HOLE]),
              obj('tree', square(20, 300), kind='tree')]
    review = [obj('conf', square(20, 20), 'confirmed', geometry_reviewed=True),
              obj('moved', square(104, 24), 'needs_adjustment'),
              obj('holes', square(100, 120), 'confirmed', geometry_reviewed=True, holes=[HOLE]),
              obj('machine', square(204, 24), 'confirmed', geometry_reviewed=True,
                  provenance={'source': 'machine:decision-policy-v2', 'human_confirmed': True}),
              obj('bad', square(304, 24), 'rejected'),
              obj('bad_same', square(300, 120), 'rejected'),
              obj('sup', square(204, 124), 'superseded'),
              obj('cand', square(304, 224), 'candidate'),
              obj('manual', square(20, 220), 'confirmed', geometry_reviewed=True),
              obj('tree', square(22, 302), 'needs_adjustment', kind='tree')]
    history = [{'revision': 1, 'objects': seeded, 'updated_at': '2026-09-29T01:00:00Z', 'updated_by': 'local'}]
    write_scene(root, 'scene-a', model, review, history=history)
    write_scene(root, 'scene-noreview', [obj('x', square(20, 20))])
    # The reviewer's model snapshot is gone and latest.json is another model: nothing can be diffed.
    write_scene(root, 'scene-unverified', [obj('u', square(20, 20))],
                [obj('u', square(24, 24), 'needs_adjustment')], review_signature='f' * 64)
    write_scene(root, 'qa_scratch', [obj('q', square(20, 20))], [obj('q', square(24, 24), 'needs_adjustment')])
    return root


def snapshot(root):
    return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in sorted((root / 'data/scenes').rglob('*')) if p.is_file()}


def test_rows_cover_human_tiers_and_flag_rejected_and_manual(tmp_path):
    root = make_repo(tmp_path)
    before = snapshot(root)
    rows, counters = diffs.build(root)
    assert snapshot(root) == before, 'data/scenes must be untouched'
    by_id = {(r['scene_id'], r['object_id']): r for r in rows}
    assert set(by_id) == {('scene-a', 'moved'), ('scene-a', 'holes'), ('scene-a', 'bad'), ('scene-a', 'manual'), ('scene-a', 'tree')}

    moved = by_id[('scene-a', 'moved')]
    assert (moved['tier'], moved['provenance'], moved['kind'], moved['review_status']) == ('T2', 'review:needs_adjustment_edited', 'building', 'needs_adjustment')
    assert moved['edit_kind'] == 'ring' and moved['ring_changed'] and not moved['holes_changed'] and not moved['rejected']
    assert moved['model_vertex_count'] == 4 and moved['human_vertex_count'] == 4 and moved['vertex_delta'] == 0
    assert abs(moved['iou'] - 1296 / 1904) < 1e-6
    assert abs(moved['area_m2_model'] - 100.) < 1e-6 and abs(moved['area_m2_human'] - 100.) < 1e-6 and abs(moved['area_delta_m2']) < 1e-6
    assert abs(moved['hausdorff_m'] - 4 * math.sqrt(2) * PX_M) < 1e-6
    assert abs(moved['boundary_distance_max_m'] - moved['hausdorff_m']) < 1e-6
    assert abs(moved['boundary_distance_median_m'] - 1.) < 1e-6          # most samples sit 4 px (1 m) from the other outline
    assert 1. <= moved['boundary_distance_p90_m'] <= moved['boundary_distance_max_m'] + 1e-9
    assert abs(moved['centroid_shift_m'] - math.sqrt(2)) < 1e-6
    assert abs(moved['pixel_size_m'] - PX_M) < 1e-9
    assert (moved['edited_at_revision'], moved['edited_by']) == (2, 'local') and moved['review_revision'] == 2

    holes = by_id[('scene-a', 'holes')]
    assert holes['tier'] == 'T1' and holes['edit_kind'] == 'holes_only' and not holes['ring_changed'] and holes['holes_changed']
    assert holes['human_hole_count'] == 1 and holes['model_hole_count'] == 0
    assert abs(holes['area_m2_human'] - (100. - 6.25)) < 1e-6 and holes['iou'] < 1.
    assert holes['edited_at_revision'] == 1, 'the hole was already in the first saved revision'

    bad = by_id[('scene-a', 'bad')]
    assert bad['rejected'] and bad['tier'] is None and bad['provenance'] == 'review_rejected' and bad['iou'] is not None

    manual = by_id[('scene-a', 'manual')]
    assert manual['tier'] == 'T1' and manual['edit_kind'] == 'manual' and not manual['model_present']
    assert manual['model_vertex_count'] is None and manual['iou'] is None and manual['geometry'] == 'model_absent'

    tree = by_id[('scene-a', 'tree')]
    assert tree['kind'] == 'tree' and tree['tier'] == 'T2' and tree['edited_at_revision'] == 2

    # Only scenes holding a review.json are walked; the hole-less scene never enters the count.
    assert counters['scenes_seen'] == 2 and counters['scenes_with_review'] == 2 and counters['scenes_model_unverified'] == 1
    assert counters['excluded:model_unverified'] == 1
    assert counters['excluded_changed:review:machine_label'] == 1
    assert counters['excluded_changed:review:superseded'] == 1
    assert counters['excluded_changed:review:candidate'] == 1
    assert counters['unchanged:T1'] == 1 and counters['unchanged:review_rejected'] == 1


def test_summary_denominators_and_wording(tmp_path):
    root = make_repo(tmp_path)
    rows, counters = diffs.build(root)
    summary = diffs.summarize(rows, counters)
    assert summary['n_rows'] == 5 and summary['n_human_tier_rows'] == 4 and summary['n_rejected_rows'] == 1
    assert summary['n_manual_rows'] == 1 and summary['n_human_tier_with_model_pair'] == 3
    assert summary['n_human_tier_ring_changed'] == 2 and summary['n_human_tier_holes_only_changed'] == 1
    assert summary['by_tier'] == {'None': 1, 'T1': 2, 'T2': 2}
    assert summary['by_kind'] == {'building': 4, 'tree': 1}
    assert summary['by_edit_kind'] == {'holes_only': 1, 'manual': 1, 'ring': 3}
    assert summary['unchanged_by_tier'] == {'T1': 1, 'review_rejected': 1}
    assert summary['excluded_changed_by_provenance'] == {'review:candidate': 1, 'review:machine_label': 1, 'review:superseded': 1}
    assert summary['excluded'] == {'model_unverified': 1}
    overall = summary['agreement_human_tier']
    # The added hole sits 10 px (2.5 m) inside its exterior, so the holes-only pair exceeds 0.45 m as well.
    assert overall['n'] == 3 and overall['n_hausdorff_ge_0_45_m'] == 3 and overall['n_human_vertex_count_4'] == 3
    assert summary['agreement_by_kind']['tree']['n'] == 1 and summary['agreement_by_tier']['T2']['n'] == 2
    assert summary['agreement_by_kind']['building']['iou_median'] is not None
    worst = summary['worst_by_boundary_distance_p90']
    assert [w['object_id'] for w in worst] == ['holes', 'moved', 'tree'], 'descending p90; the hole ring is 2.5 m from the model exterior'
    assert worst[0]['boundary_distance_p90_m'] >= worst[1]['boundary_distance_p90_m'] >= worst[2]['boundary_distance_p90_m']
    empty = diffs.agreement([])
    assert empty['n'] == 0 and empty['iou_median'] is None and empty['hausdorff_m_median'] is None
    assert 'not accuracy' in summary['note']
    assert not any('accuracy' in k for k in summary)
    assert not any('accuracy' in k for r in rows for k in r)


def test_cli_writes_dated_folder_and_filters_kind(tmp_path, capsys):
    root = make_repo(tmp_path)
    before = snapshot(root)
    assert diffs.main(['--root', str(root), '--kind', 'building']) == 0
    assert snapshot(root) == before
    printed = json.loads(capsys.readouterr().out)
    out = Path(printed['out'])
    assert out.parent == root / 'data/reviews/eyes/edit-diffs' and len(out.name) == 10
    rows = [json.loads(line) for line in (out / 'rows.jsonl').read_text().splitlines()]
    assert {r['object_id'] for r in rows} == {'moved', 'holes', 'bad', 'manual'}
    assert printed['by_kind'] == {'building': 4} and printed['excluded']['kind_filtered'] == 1
    written = json.loads((out / 'summary.json').read_text())
    assert written['files']['rows.jsonl'] == hashlib.sha256((out / 'rows.jsonl').read_bytes()).hexdigest()
    assert written['code_sha256'] == diffs.code_sha256() and written['version'] == diffs.VERSION
    assert not list(out.glob('*.tmp'))
    # An explicit --out and --scene-ids restrict the run; qa_ scenes never enter the default listing.
    assert diffs.main(['--root', str(root), '--out', str(tmp_path / 'custom'), '--scene-ids', 'scene-noreview', 'qa_scratch']) == 0
    custom = json.loads(capsys.readouterr().out)
    assert custom['n_rows'] == 1 and custom['n_scenes_seen'] == 2 and custom['n_scenes_with_review'] == 1
    assert (tmp_path / 'custom' / 'rows.jsonl').exists()
    assert os.path.isdir(tmp_path / 'custom')


def test_vertex_count_and_same_holes_helpers():
    assert diffs.vertex_count([[0, 0], [1, 0], [1, 1]]) == 3
    assert diffs.vertex_count([[0, 0], [1, 0], [1, 1], [0, 0]]) == 3
    assert diffs.vertex_count([]) == 0 and diffs.vertex_count(None) == 0
    assert diffs.same_holes(None, []) and diffs.same_holes([HOLE], [HOLE])
    assert not diffs.same_holes([HOLE], []) and not diffs.same_holes([HOLE], [[[0, 0], [1, 0], [1, 1]]])
