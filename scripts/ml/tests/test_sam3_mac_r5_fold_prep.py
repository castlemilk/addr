"""Round-5 fold preparation tests (r5_fold_prep: keep set from ledger_v2 rows, hard negatives with the frozen-tile leak
check, the derived-dataset negatives mechanism, the prepare_coco dry run). Synthetic folders only; nothing under data/ is
read or written. Wording: model estimate, agreement, reference; nothing promotes anything."""
import json
import os
from pathlib import Path

import pytest
from PIL import Image
from scripts.ml.training.sam3_mac import r5_fold_prep as fp

REPO = Path(__file__).resolve().parents[3]
PREP = REPO / 'data/reviews/eyes/training/sam3-mac/round5/fold-prep-2026-10-06'
RECIPE = REPO / 'data/reviews/eyes/training/sam3-mac/round5/RECIPE.json'


def _export(root, name, windows, anns_per_window, policy='none', sha_prefix='s', drop=()):
    """dataset-v1-format teacher export: one window per image per split, annotations object_id teacher-<index>."""
    folder = root / name
    (folder / 'images').mkdir(parents=True)
    json.dump({'policy': policy, 'version': 'roof-outline-distill-v1'}, open(folder / 'manifest.json', 'w'))
    for split, wids in windows.items():
        images, anns, aid = [], [], 0
        for i, wid in enumerate(wids, 1):
            Image.new('RGB', (64, 64), (i * 20, 90, 90)).save(folder / 'images' / f'{wid}.png')
            images.append({'id': i, 'file_name': f'images/{wid}.png', 'width': 64, 'height': 64, 'pixel_size_m': .236, 'scene_id': wid,
                           'tile_group': f'grid:{wid}', 'sha256': f'{sha_prefix}-{wid}', 'split': split})
            for index, area in anns_per_window:
                if (wid, index) in drop:
                    continue
                aid += 1
                anns.append({'id': aid, 'image_id': i, 'category_id': 1, 'bbox': [4, 4, 20, 20], 'area': area, 'iscrowd': 0,
                             'segmentation': [[4, 4, 24, 4, 24, 24, 4, 24]], 'weight': 1., 'provenance': 'hires-teacher-x', 'tier': 'T3',
                             'area_m2': round(area * .236 * .236, 2), 'object_id': f'teacher-{index}', 'scene_id': wid})
        json.dump({'images': images, 'annotations': anns, 'categories': [{'id': 1, 'name': 'building_roof'}]}, open(folder / f'{split}.json', 'w'))
    return folder


def _ledger(path, rows):
    with open(path, 'w') as handle:
        for window, index, v1, v2, cls in rows:
            handle.write(json.dumps({'window': window, 'index': index, 'recommendation': v1, 'recommendation_v2': v2, 'ndsm_class': cls,
                                     'area_m2': 5., 'band': 'lt_50m2'}) + '\n')
    return path


def test_keep_set_drops_only_v2_ignore_and_reports_delta_and_floors(tmp_path):
    windows = {'train': ['w1', 'w2'], 'val': ['w3'], 'test': ['w4']}
    anns = [(1, 400), (2, 100), (3, 20), (4, 10)]
    rows = [(w, 1, 'keep', 'keep', 'elevated') for w in 'w1 w2 w3 w4'.split()]
    rows += [(w, 2, 'review', 'review', 'mixed') for w in 'w1 w2 w3 w4'.split()]
    rows += [(w, 3, 'ignore', 'review', 'elevated') for w in 'w1 w2 w3 w4'.split()]      # demoted under v2: kept now
    rows += [(w, 4, 'ignore', 'ignore', 'flat') for w in 'w1 w2 w3 w4'.split()]          # still ignore: dropped
    source = _export(tmp_path, 'distill-x-squared', windows, anns)
    v1 = _export(tmp_path, 'distill-x-squared-ledger', windows, anns, policy='ledger-v1',
                 drop={(w, i) for w in 'w1 w2 w3 w4'.split() for i in (3, 4)})
    ledger = _ledger(tmp_path / 'ledger_v2_rows.jsonl', rows)
    out = tmp_path / 'distill-x-squared-ledger-v2'
    m = fp.keep_set(source, ledger, out, v1_export=v1)
    assert m['ledger_rows']['drop_keys'] == 4 and m['ledger_rows']['by_value'] == {'keep': 4, 'review': 8, 'ignore': 4}
    train = m['splits']['train']
    assert (train['annotations_in'], train['dropped'], train['annotations_out']) == (8, 2, 6)
    assert train['by_class'] == {'keep': 2, 'review': 4} and train['dropped_by_ndsm_class'] == {'flat': 2}
    assert train['by_window'] == {'w1': {'keep': 1, 'review': 2}, 'w2': {'keep': 1, 'review': 2}}
    assert train['delta_vs_v1'] == {'v1_annotations': 4, 'keep_set_annotations': 6, 'added': 2, 'removed': 0, 'images_equal': True,
                                    'added_keys': [['w1', 3], ['w2', 3]], 'added_floor_counts': {'16': 2, '72': 0, '216': 0}}
    # the lever: at the 16 px floor the keep set adds the two demoted rows; at 72 and 216 px nothing changes
    assert train['floor_counts'] == {'keep_set': {'16': 6, '72': 4, '216': 2}, 'v1_export': {'16': 4, '72': 4, '216': 2}}
    assert m['splits']['test']['dropped'] == 1 and m['splits']['val']['annotations_out'] == 3
    written = json.load(open(out / 'train.json'))
    assert [a['object_id'] for a in written['annotations']] == ['teacher-1', 'teacher-2', 'teacher-3'] * 2
    assert {a['ledger_v2'] for a in written['annotations']} == {'keep', 'review'}
    assert os.path.islink(out / 'images') and (out / 'images' / 'w1.png').exists()
    assert fp.keep_set(source, ledger, out, v1_export=v1)['splits']['train']['dropped'] == 2    # idempotent: symlink reused
    (tmp_path / 'other').mkdir()
    (tmp_path / 'clash' / 'images').mkdir(parents=True)
    with pytest.raises(FileExistsError):
        fp.link_images(source, tmp_path / 'clash')
    assert fp.teacher_key({'object_id': 'obj-1', 'scene_id': 'w'}) is None and fp.teacher_key({'object_id': 'teacher-7', 'scene_id': 'w'}) == ('w', 7)


def _frozen(root, name, split, scenes, with_ann=True):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    images = [{'id': i, 'file_name': f'images/{s}.png', 'width': 64, 'height': 64, 'pixel_size_m': .078, 'scene_id': s, 'tile_group': f'z17:{s}',
               'sha256': f'sha-{s}'} for i, s in enumerate(scenes, 1)]
    anns = [{'id': 100 + i, 'image_id': i, 'category_id': 1, 'bbox': [1, 1, 5, 5], 'area': 800., 'iscrowd': 0, 'area_m2': 5.1,
             'segmentation': [[1, 1, 6, 1, 6, 6, 1, 6]], 'object_id': f'obj-{s}', 'scene_id': s, 'provenance': 'machine_pseudo'}
            for i, s in enumerate(scenes, 1)] if with_ann else []
    json.dump({'images': images, 'annotations': anns}, open(folder / f'{split}.json', 'w'))
    return folder


def test_negatives_from_block_refuse_frozen_test_tiles_and_read_chat_kind(tmp_path):
    training = tmp_path / 'training'
    _frozen(training, 'dataset-v2', 'test', ['tileA', 'tileB'])
    _frozen(training, 'dataset-v2', 'val', ['tileV'])
    _frozen(training, 'dataset-v1', 'test', ['tileA'])
    judged = fp.judged_index(training)
    assert set(judged) == {'dataset-v2 test', 'dataset-v2 val', 'dataset-v1 test'}
    scenes = tmp_path / 'scenes'
    (scenes / 'win-1').mkdir(parents=True)
    json.dump({'revision': 3, 'objects': [{'id': 'obj-tileA', 'kind': 'building', 'status': 'rejected',
                                           'notes': '[reviewer 2026-10-04 chat, pond] "is more likely a pond": not a roof.'}]},
              open(scenes / 'win-1' / 'review.json', 'w'))
    block = {'dataset': str(training / 'dataset-v2'), 'split': 'test', 'batch_id': 'b1',
             'facts': [{'class': 'not_roof', 'scene_id': 'win-1', 'object_id': 'obj-tileA', 'image_id': 1, 'ann_id': 101, 'path': 'P',
                        'provenance_kind': 'chat', 'status': 'rejected', 'snapshot_area_m2': 5.1, 'arms': {'base': {'detected': True}}},
                       {'class': 'not_roof', 'scene_id': 'tileB', 'object_id': 'obj-tileB', 'image_id': 2, 'ann_id': 102, 'path': 'P',
                        'provenance_kind': 'ui', 'status': 'rejected', 'snapshot_area_m2': 5.1, 'arms': {'base': {'detected': False}}},
                       {'class': 'roof', 'scene_id': 'tileB', 'object_id': 'x', 'image_id': 2, 'ann_id': 102, 'arms': {}}],
             'unjoined': [{'object_id': 'u1', 'scene_id': 'house-9', 'status': 'rejected', 'provenance_kind': 'chat', 'review_area_m2': 1.4,
                           'unjoined_reasons': ['not_judged_image', 'under_min_area']},
                          {'object_id': 'u2', 'scene_id': 'win-2', 'status': 'confirmed', 'unjoined_reasons': ['not_judged_image']}]}
    block_file = tmp_path / 'v2-t045.json'
    json.dump(block, open(block_file, 'w'))
    rows = fp.negatives_from_block(block_file, judged, scenes_root=scenes, training=training)
    assert [r['object_id'] for r in rows] == ['obj-tileA', 'obj-tileB', 'u1']
    a, b, u = rows
    assert a['kind'] == 'pond' and a['kind_tag'].startswith('[reviewer') and a['ring'] == [1, 1, 6, 1, 6, 6, 1, 6] and a['area_px'] == 800.
    assert a['frozen_key'] == ['tileA', 'obj-tileA'] and a['detected_by_base_at_045'] is True
    assert not a['admissible'] and sorted(a['refused']) == ['frozen_judged_tile:dataset-v1 test:scene_id+tile_group+sha256',
                                                            'frozen_judged_tile:dataset-v2 test:scene_id+tile_group+sha256']
    assert b['kind'] == 'unstated_ui_decision' and b['refused'] == ['frozen_judged_tile:dataset-v2 test:scene_id+tile_group+sha256']
    assert u['refused'] == ['unjoined:not_judged_image', 'unjoined:under_min_area'] and not u['admissible']
    summary = fp.negatives_summary(rows, judged)
    assert (summary['rows'], summary['admissible'], summary['refused'], summary['on_frozen_v2_test']) == (3, 0, 3, 2)
    assert summary['by_kind'] == {'pond': 1, 'unstated_ui_decision': 1, 'unstated_unjoined': 1}
    assert fp.kind_from_notes('[reviewer 2026-10-04 chat, shade] "is just shade of a tree"') == ('tree_shadow', '[reviewer 2026-10-04 chat, shade]')
    assert fp.kind_from_notes('Carried from map-x as a frame-cut target') == (None, None)


def test_apply_negatives_removes_only_admissible_train_annotations_and_records_them(tmp_path):
    train = _frozen(tmp_path, 'dataset-v2-trainclean', 'train', ['t1', 't2'])
    (train / 'images').mkdir()
    json.dump({}, open(train / 'manifest.json', 'w'))
    negatives = [{'admissible': True, 'split': 'train', 'source_dataset': 'dataset-v2-trainclean', 'frozen_key': ['t1', 'obj-t1'], 'kind': 'road'},
                 {'admissible': False, 'split': 'train', 'source_dataset': 'dataset-v2-trainclean', 'frozen_key': ['t2', 'obj-t2'], 'kind': 'pond',
                  'refused': ['frozen_judged_tile:dataset-v2 test:scene_id']},
                 {'admissible': True, 'split': 'train', 'source_dataset': 'other', 'frozen_key': ['t2', 'obj-t2'], 'kind': 'pond'},
                 {'admissible': True, 'split': 'train', 'source_dataset': 'dataset-v2-trainclean', 'frozen_key': ['t9', 'nope'], 'kind': 'tree'}]
    m = fp.apply_negatives(train, negatives, tmp_path / 'dataset-v2-trainclean-neg')
    assert (m['requested'], m['removed'], m['annotations_in'], m['annotations_out']) == (2, 1, 2, 1)
    assert m['removed_rows'][0]['object_id'] == 'obj-t1' and m['removed_rows'][0]['kind'] == 'road'
    assert m['not_found'] == [['t9', 'nope']] and m['images_left_empty'] == [1]
    written = json.load(open(tmp_path / 'dataset-v2-trainclean-neg' / 'train.json'))
    assert [a['object_id'] for a in written['annotations']] == ['obj-t2']
    assert os.path.islink(tmp_path / 'dataset-v2-trainclean-neg' / 'images')


def test_dry_run_builds_fold_checks_leaks_and_frozen_shas(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.prepare_coco, 'SCENES', tmp_path / 'no-scenes')
    training = tmp_path / 'training'
    teacher = _export(training, 'distill-x-squared-ledger-v2', {'train': ['w1', 'w2'], 'val': ['w3'], 'test': ['w4']}, [(1, 400), (2, 100), (3, 10)])
    student = _export(training, 'dataset-v2-trainclean', {'train': ['s1', 's2']}, [(1, 300), (2, 30)], sha_prefix='t')
    v2 = _frozen(training, 'dataset-v2', 'test', ['j1'])
    _frozen(training, 'dataset-v2', 'val', ['j2'])
    _frozen(training, 'dataset-v1', 'test', ['j3'])
    judged = fp.judged_index(training)
    summary = fp.dry_run([teacher, student], tmp_path / 'coco-dry', [v2], {'distill-x-squared-ledger-v2': 216}, 16, 200, negatives=[
        {'admissible': True, 'split': 'train', 'kind': 'road'}, {'admissible': False, 'split': 'test', 'kind': 'pond'}], judged=judged, training=training,
        expected_split_sha={'dataset-v2 test': fp.sha256_file(v2 / 'test.json')[:8]})
    assert summary['ok'] and summary['leak_check'] == {'train_images_checked': 4, 'leaks': 0, 'leak_rows': [], 'ok': True}
    assert summary['splits']['train']['images'] == 4 and summary['splits']['train']['annotations'] == 2 + 4   # 216 floor on the teacher: 400 only; student 300 + 30
    assert summary['splits']['train']['negatives_applied'] == 1 and summary['splits']['train']['ignore_regions'] == 0
    assert summary['negatives'] == {'applied': 1, 'refused': 1, 'ignore_regions_note': 'SAM3 instance training has no ignore region; 0 by construction'}
    assert summary['fold_gate']['ok'] and summary['fold_gate']['images_zero_annotations'] == 0
    assert all(v['unchanged'] and v['matches_expected'] for v in summary['frozen_splits_unchanged'].values())
    assert (tmp_path / 'coco-dry' / 'dry-run-summary.json').exists() and (tmp_path / 'coco-dry' / 'train' / '_annotations.coco.json').exists()
    # a train tile that is also a frozen judged tile is a leak
    leaky = _export(training, 'leaky', {'train': ['j1']}, [(1, 400)], sha_prefix='sha')
    json.dump({'images': [{**i, 'sha256': 'sha-j1'} for i in json.load(open(leaky / 'train.json'))['images']],
               'annotations': json.load(open(leaky / 'train.json'))['annotations']}, open(leaky / 'train.json', 'w'))
    bad = fp.dry_run([leaky], tmp_path / 'coco-bad', [v2], judged=judged, training=training)
    assert not bad['ok'] and bad['leak_check']['leaks'] == 1 and bad['leak_check']['leak_rows'][0]['refusal'].startswith('frozen_judged_tile:dataset-v2 test')
    assert fp.main(['dry-run', '--dataset', str(teacher), '--dataset', str(student), '--out', str(tmp_path / 'coco-cli'), '--judge-dataset', str(v2),
                    '--min-area-px-for', 'distill-x-squared-ledger-v2=216', '--training', str(training)]) == 0


def test_round5_recipe_and_prep_outputs_are_consistent():
    if not RECIPE.exists() or not PREP.exists():
        pytest.skip('round-5 recipe / fold prep outputs absent (data/ not checked out)')
    recipe = json.load(open(RECIPE))
    assert recipe['version'] == 'sam3-round5-recipe-v1' and recipe['status']['training_started'] is False
    seeds = {arm['run']: arm['seed'] for arm in recipe['arms']}
    for base in {r.rsplit('-seed', 1)[0] for r in seeds}:
        assert {seeds[f'{base}-seed1'], seeds[f'{base}-seed2']} == {1, 2}, f'{base}: a seed pair from the start'
    fold = recipe['fold']
    floors = fold['floor_px_per_source']
    assert set(floors) == {Path(s).name for s in fold['sources']} and floors['dataset-v2-trainclean'] == 16
    keep = fold['keep_set']
    assert keep['path'].endswith('distill-linz-chc-2025-0075-squared-ledger-v2') and keep['ledger_rows']['key'] == 'recommendation_v2'
    km = json.load(open(REPO / keep['path'] / 'manifest.json'))
    assert km['ledger_rows']['sha256'] == keep['ledger_rows']['sha256'] and km['splits']['train']['delta_vs_v1']['removed'] == 0
    neg = json.load(open(REPO / fold['negatives']['summary']))
    assert neg['admissible'] == fold['negatives']['admissible'] and neg['on_frozen_v2_test'] == fold['negatives']['refused_on_frozen_v2_test']
    dry = json.load(open(REPO / fold['dry_run']['summary']))
    assert dry['ok'] and dry['leak_check']['leaks'] == 0 and dry['fold_gate']['images_zero_annotations'] == 0
    assert recipe['memory_gate']['budget_mb'] >= max(recipe['memory_gate']['measured_round4']['max_driver_mb_per_run'].values())
    assert 'accuracy' not in json.dumps({k: v for k, v in recipe.items() if k != 'wording'}).lower()
