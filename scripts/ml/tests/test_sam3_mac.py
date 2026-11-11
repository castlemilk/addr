import json

from PIL import Image
from scripts.ml.training.sam3_mac import prepare_coco


def _dataset(root, name, n_images=2, weight=1., provenance='machine_pseudo'):
    folder = root / name
    (folder / 'images').mkdir(parents=True)
    images, anns = [], []
    for i in range(1, n_images + 1):
        Image.new('RGB', (64, 64), (i * 20, 90, 90)).save(folder / 'images' / f'{name}-{i}.png')
        images.append({'id': i, 'file_name': f'images/{name}-{i}.png', 'width': 64, 'height': 64, 'pixel_size_m': .236,
                       'scene_id': f'scene-{i}'})
        anns.append({'id': 10 * i, 'image_id': i, 'category_id': 1, 'bbox': [4, 4, 20, 20], 'area': 400, 'iscrowd': 0,
                     'segmentation': [[4, 4, 24, 4, 24, 24, 4, 24]], 'weight': weight, 'provenance': provenance,
                     'tier': 'T3', 'area_m2': 22.3, 'object_id': f'obj-{i}', 'label_index': i})
        anns.append({'id': 10 * i + 1, 'image_id': i, 'category_id': 1, 'bbox': [30, 30, 2, 2], 'area': 4, 'iscrowd': 0,
                     'segmentation': [[30, 30, 32, 30, 32, 32, 30, 32]], 'weight': weight, 'provenance': provenance})
    payload = {'images': images, 'annotations': anns, 'categories': [{'id': 1, 'name': 'building_roof', 'supercategory': 'building'}]}
    for split in ('train', 'val'):
        json.dump(payload, open(folder / f'{split}.json', 'w'))
    return folder


def test_prepare_coco_merges_renumbers_and_renames_prompt(tmp_path):
    a = _dataset(tmp_path, 'dataset-x', 2)
    b = _dataset(tmp_path, 'distill-y', 3, provenance='hires-teacher')
    out = tmp_path / 'coco'
    manifest = prepare_coco.convert([a, b], out)
    train = json.load(open(out / 'train' / '_annotations.coco.json'))
    assert train['categories'] == [{'id': 1, 'name': 'building roof', 'supercategory': 'building'}]
    assert len(train['images']) == 5
    assert [img['id'] for img in train['images']] == [1, 2, 3, 4, 5]
    assert len(train['annotations']) == 5, 'tiny 4 px annotations are dropped'
    assert all(set(a) <= {'id', 'image_id', 'category_id', 'bbox', 'area', 'iscrowd', 'segmentation'} for a in train['annotations'])
    assert [a['id'] for a in train['annotations']] == [1, 2, 3, 4, 5]
    assert all(isinstance(a['segmentation'], list) for a in train['annotations'])
    sidecar = json.load(open(out / 'train' / 'sidecar.json'))
    assert sidecar[0]['source_dataset'] == 'dataset-x' and sidecar[-1]['provenance'] == 'hires-teacher'
    assert manifest['splits']['train']['annotations'] == 5
    assert manifest['splits']['train']['dropped_or_notes']['ann_tiny'] == 5
    assert manifest['splits']['test']['images'] == 0
    assert (out / 'manifest.json').exists()


def test_prepare_coco_copy_mode_and_weight_floor(tmp_path):
    a = _dataset(tmp_path, 'dataset-x', 1, weight=.5)
    out = tmp_path / 'coco'
    manifest = prepare_coco.convert([a], out, copy_images=True, drop_weight_below=.9)
    train = json.load(open(out / 'train' / '_annotations.coco.json'))
    assert train['images'][0]['file_name'].startswith('images/')
    assert (out / 'train' / train['images'][0]['file_name']).exists()
    assert manifest['splits']['train']['annotations'] == 0
    assert manifest['splits']['train']['dropped_or_notes']['ann_low_weight'] == 2


def test_prepare_coco_per_dataset_floor_applies_to_the_named_source_only(tmp_path):
    a = _dataset(tmp_path, 'dataset-x', 2)                       # every image: one 400 px and one 4 px annotation
    b = _dataset(tmp_path, 'distill-y', 3, provenance='hires-teacher')
    out = tmp_path / 'coco'
    manifest = prepare_coco.convert([a, b], out, min_area_px=1, per_dataset_floor={'distill-y': 216})
    train = manifest['splits']['train']
    assert train['per_source']['dataset-x']['annotations'] == 4 and 'ann_tiny' not in train['per_source']['dataset-x'], \
        'the unnamed source keeps its 4 px annotations at the global floor of 1'
    assert train['per_source']['distill-y']['annotations'] == 3 and train['per_source']['distill-y']['ann_tiny'] == 3
    assert train['dropped_or_notes']['ann_tiny'] == 3 and train['flipped_to_background']['ann_tiny'] == 3
    assert manifest['options']['min_area_px'] == 1 and manifest['options']['min_area_px_for'] == {'distill-y': 216}
    assert manifest['floor_px_per_source'] == {'dataset-x': 1, 'distill-y': 216}
    coco = json.load(open(out / 'train' / '_annotations.coco.json'))
    assert len(coco['annotations']) == 7
    # a name that is not one of the datasets is refused (a typo would otherwise silently apply no floor)
    import pytest
    with pytest.raises(ValueError):
        prepare_coco.convert([a, b], tmp_path / 'coco2', per_dataset_floor={'distill-z': 216})
    assert prepare_coco.parse_floor_spec(['distill-y=216', 'a=b=16']) == {'distill-y': 216, 'a=b': 16}
    for bad in (['distill-y'], ['distill-y=12.5'], ['=216'], ['distill-y=216', 'distill-y=287']):
        with pytest.raises(ValueError):
            prepare_coco.parse_floor_spec(bad)
    # the CLI path: the same flag through argparse, manifest written with the floor recorded
    assert prepare_coco.main(['--dataset', str(a), '--dataset', str(b), '--min-area-px', '1', '--min-area-px-for', 'distill-y=216',
                              '--out', str(tmp_path / 'coco-cli')]) == 0
    written = json.load(open(tmp_path / 'coco-cli' / 'manifest.json'))
    assert written['options']['min_area_px_for'] == {'distill-y': 216} and written['splits']['train']['annotations'] == 7
    with pytest.raises(SystemExit):
        prepare_coco.main(['--dataset', str(a), '--min-area-px-for', 'nope=216', '--out', str(tmp_path / 'coco-bad')])
    # no per-dataset floor: identical to the round-3 behaviour (global floor on every source)
    plain = prepare_coco.convert([a, b], tmp_path / 'coco-plain')
    assert plain['options']['min_area_px_for'] == {} and plain['floor_px_per_source'] == {'dataset-x': 16, 'distill-y': 16}
    assert plain['splits']['train']['dropped_or_notes']['ann_tiny'] == 5


def test_finetune_helpers_are_importable_without_torch_device():
    from scripts.ml.training.sam3_mac import finetune_mps as ft
    assert ft.lr_at(0, 10, 100, 1.) < ft.lr_at(9, 10, 100, 1.) <= 1.
    assert abs(ft.lr_at(100, 10, 100, 1.) - .05) < 1e-6
    assert ft.FREEZE_DEFAULT == ('backbone.vision_backbone.*', 'backbone.language_backbone.*')


def test_finetune_loss_flags_default_to_the_round_3_constants():
    """The three round-4 loss flags default to today's hard-coded weights, so every round 1-3 run line reproduces."""
    from scripts.ml.training.sam3_mac import finetune_mps as ft
    args = ft.build_parser().parse_args(['--coco', 'x'])
    assert (args.presence_weight, args.pos_weight, args.o2m_weight) == (20.0, 10.0, 2.0)
    assert ft.LOSS_DEFAULTS == {'presence_weight': 20., 'pos_weight': 10., 'o2m_weight': 2.}
    assert all(isinstance(v, float) for v in (args.presence_weight, args.pos_weight, args.o2m_weight))
    arm = ft.build_parser().parse_args(['--coco', 'x', '--pos-weight', '3', '--o2m-weight', '0'])
    assert (arm.presence_weight, arm.pos_weight, arm.o2m_weight) == (20.0, 3.0, 0.0)
    assert 'presence_weight' in vars(arm) and 'pos_weight' in vars(arm) and 'o2m_weight' in vars(arm), 'run.json records vars(args)'
    import inspect
    defaults = inspect.signature(ft.build_loss).parameters
    assert (defaults['presence_weight'].default, defaults['pos_weight'].default, defaults['o2m_weight'].default) == (20.0, 10.0, 2.0)


def test_finetune_build_loss_wiring_under_the_sam3_venv():
    """Only the SAM3 venv can import the fork (iopath etc.); the export venv skips. Run it with:
    scripts/ml/training/.venv-sam3/bin/python -c "from scripts.ml.training.sam3_mac.finetune_mps import build_loss as b; ..."""
    import pytest
    pytest.importorskip('iopath')
    from scripts.ml.training.sam3_mac import finetune_mps as ft
    w = ft.build_loss()
    f = w.loss_fns_find[1]
    assert (w.o2m_weight, f.pos_weight, f.weight_dict) == (2.0, 10.0, {'loss_ce': 20.0, 'presence_loss': 20.0})
    w = ft.build_loss(pos_weight=3., o2m_weight=0.)
    assert (w.loss_fns_find[1].pos_weight, w.o2m_weight, w.loss_fns_find[1].weight_dict['presence_loss']) == (3.0, 0.0, 20.0)


def test_sweep_next_picks_best_qualifying_lr():
    from scripts.ml.training.sam3_mac import sweep_next
    rows = [
        {'run': 's1-raw-lr8e-5-seed1', 'lr': 8e-5, 'skipped_batches': 0, 'paired': {'best_iou_delta_mean': .01},
         'negatives': {'base': {'novel_share': .2}, 'tuned': {'novel_share': .25}}},
        {'run': 's1-raw-lr3e-4-seed1', 'lr': 3e-4, 'skipped_batches': 0, 'paired': {'best_iou_delta_mean': .03},
         'negatives': {'base': {'novel_share': .2}, 'tuned': {'novel_share': .22}}},
        {'run': 's1-raw-lr1e-3-seed1', 'lr': 1e-3, 'skipped_batches': 0, 'paired': {'best_iou_delta_mean': .05},
         'negatives': {'base': {'novel_share': .2}, 'tuned': {'novel_share': .45}}},      # novel share rose by 0.25: out
        {'run': 's2-raw-lr3e-4-seed1', 'lr': 3e-4, 'paired': {'best_iou_delta_mean': .9}},  # not a stage-1 run
    ]
    assert sweep_next.best(rows) == 3e-4
    assert sweep_next.best([{'run': 's1-x', 'lr': 1e-4, 'eval_error': 'boom'}]) is None
    tie = [{'run': 's1-a', 'lr': 3e-4, 'skipped_batches': 0, 'paired': {'best_iou_delta_mean': .02}, 'negatives': {}},
           {'run': 's1-b', 'lr': 8e-5, 'skipped_batches': 0, 'paired': {'best_iou_delta_mean': .02}, 'negatives': {}}]
    assert sweep_next.best(tie) == 8e-5


def test_export_rejects_unknown_label_target_and_reads_ledger(tmp_path):
    import pytest
    from scripts.ml.training import distill_dataset as d
    with pytest.raises(SystemExit):
        d.export(tmp_path, label_target='sharp')
    judges = tmp_path / 'judges'
    judges.mkdir()
    (judges / 'ledger_rows.jsonl').write_text(json.dumps({'window': 'w', 'index': 3, 'recommendation': 'ignore'}) + '\n'
                                              + json.dumps({'window': 'w', 'index': 4, 'recommendation': 'keep'}) + '\n')
    assert d._ledger_ignored(tmp_path) == {('w', 3)}
    assert d._ledger_ignored(tmp_path / 'missing') == set()


# --- round 3 orchestration (round3.sh, r3_record.py, round3_report.py) -----------------------------------------------

def _legacy_judge(base_matched=60, tuned_matched=63, ctrl=False, share_tuned=.22):
    """A reference_eval summary.json in today's two-arm shape (base+tuned, or ctrl+tuned when ctrl=True)."""
    first = 'ctrl' if ctrl else 'base'
    arm = lambda m, b, s: {'references': 63, 'matched_at_iou_0_5': m, 'recall_at_0_5': round(m / 63, 4), 'best_iou_median': b,  # noqa: E731
                           'boundary_distance_m_median_matched': s, 'predictions_per_image_median': 12., 'seconds_per_image_median': .5}
    neg = lambda novel, unbacked, total: {'tiles': 44, 'predictions': total, 'predictions_per_tile_median': total / 44, 'novel': novel,  # noqa: E731
                                          'novel_no_footprint': unbacked, 'novel_no_footprint_share_of_predictions': round(unbacked / total, 4),
                                          'novel_share': round(novel / total, 4), 'novel_area_m2_median': 9.1}
    summary = {first: arm(base_matched, .9047, .22), 'tuned': arm(tuned_matched, .9017, .235),
               'paired': {'second_minus_first': f'tuned - {first}', 'references': 63, 'best_iou_delta_mean': .0271,
                          'best_iou_delta_ci95_image_bootstrap': [-.0029, .0492], 'per_reference': {'higher': 32, 'lower': 31, 'equal': 0},
                          'recall_delta': .0476},
               'negatives': {first: neg(123, 54, 918), 'tuned': neg(509, int(share_tuned * 1271), 1271), 'note': 'novel = ...'},
               'note': 'agreement with the listed human outlines, not calibrated accuracy'}
    return {'version': 'sam3-mac-reference-eval-v1', 'computed_at': '2026-09-30T00:00:00+00:00', 'device': 'mps', 'dtype': 'fp32',
            'prompt': 'building roof', 'threshold': .45, 'images': 44, 'arms': {first: {'spec': first}, 'tuned': {'spec': 'tuned=x.pt'}},
            'model_load_s': {}, 'summary': summary, 'rows': [], 'tiles': []}


def _coco_fold(root, name, per_image=(150, 201), manifest=True):
    fold = root / name
    (fold / 'train').mkdir(parents=True)
    images = [{'id': i + 1, 'file_name': f'/x/{i}.png', 'width': 64, 'height': 64} for i in range(len(per_image))]
    anns, k = [], 1
    for img, n in zip(images, per_image, strict=True):
        for _ in range(n):
            anns.append({'id': k, 'image_id': img['id'], 'category_id': 1, 'bbox': [0, 0, 4, 4], 'area': 16, 'iscrowd': 0, 'segmentation': [[0, 0, 4, 0, 4, 4]]})
            k += 1
    json.dump({'images': images, 'annotations': anns, 'categories': [{'id': 1, 'name': 'building roof'}]}, open(fold / 'train' / '_annotations.coco.json', 'w'))
    if manifest:
        json.dump({'version': 'sam3-mac-coco-v1', 'sources': ['a', 'b'],
                   'splits': {'train': {'images': len(images), 'annotations': len(anns), 'annotations_per_image_median': 150,
                                        'dropped_or_notes': {'ann_over_cap': 2064, 'ann_tiny': 3, 'prov:hires-teacher-com-2018': 4980},
                                        # prepare_coco's real key names (STEP 8 fold provenance)
                                        'density_per_ha_median': 24.5, 'per_source': {'a': {'annotations': 10, 'images': 1}},
                                        'distance_check': {'near_m': 300., 'train_images_checked': 2, 'train_images_flagged': 1, 'flagged': [{'image_id': 1}]}}}},
                  open(fold / 'manifest.json', 'w'))
    return fold


def test_r3_record_fold_gate_flags_images_over_cap(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    fold = _coco_fold(tmp_path, 'coco-bad', per_image=(150, 201))
    gate = rec.fold_gate(fold, cap=200)
    assert gate['ok'] is False and gate['images_over_cap'] == 1 and gate['max_annotations_per_image'] == 201
    assert any('more than 200' in r for r in gate['reasons'])
    assert rec.fold_gate(fold, cap=300)['ok'] is True
    assert rec.fold_gate(tmp_path / 'absent', cap=200)['reasons'] == ['train/_annotations.coco.json missing']
    good = _coco_fold(tmp_path, 'coco-good', per_image=(150, 200))
    stats = rec.fold_stats(good, cap=200)
    assert stats['images_over_200'] == 0 and stats['ann_over_cap'] == 2064 and stats['flipped_to_background'] == 2067
    assert stats['per_source'] == {'hires-teacher-com-2018': 4980}
    assert stats['density_median_per_ha'] == 24.5 and stats['distance_check']['train_images_flagged'] == 1, 'read from splits.train, not the manifest root'
    assert stats['per_source_provenance'] == {'a': {'annotations': 10, 'images': 1}}
    assert rec.main(['gate', '--coco', str(fold)]) == 1 and rec.main(['gate', '--coco', str(good)]) == 0


def test_r3_record_record_refuses_to_finalise_a_missing_judge_block(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    root = tmp_path / 'round3'
    fold = _coco_fold(root, 'coco-r3-sq-c200-v1', per_image=(150, 200))
    run = 'r3-ctrl-sq-c200-seed1'
    run_dir = root / run
    for folder in ('reference-eval-v1', 'reference-eval-v1-matched'):   # v1 complete (raw judge at 0.45 is its own 0.45 view); calibration done
        (run_dir / folder).mkdir(parents=True)
        json.dump(_legacy_judge(), open(run_dir / folder / 'summary.json', 'w'))
    json.dump({'t_star': .6, 'flags': []}, open(run_dir / 'calibration.json', 'w'))
    results = root / 'results.jsonl'   # v2 and v2val judges failed: no summary.json
    args = ['record', '--root', str(root), '--run', run, '--coco', str(fold), '--judge-mode', 'full', '--extra', '{"phase": "phase1"}']
    assert rec.main(args) == 1, 'a failed judge must not finalise the arm'
    rows = rec.read_rows(results)
    assert len(rows) == 1 and rows[0]['run'] == f'{run}_error' and rows[0]['error'] == 'judge_missing:v2,v2val' and rows[0]['phase'] == 'phase1'
    assert 'summary.json missing' in rows[0]['judge_missing']['v2'] and rec.has_row(results, run) is False
    assert rec.main(['done', '--root', str(root), '--run', run]) == 1, 'the re-run re-judges from trainable-final.pt'
    assert rec.missing_judge_blocks({'v1': {'view_raw': {}}, 'v2': {'status': 'not_run'}, 'v2val': {'status': 'not_run'}}, 'legacy') == []
    for name in ('v2', 'v2val'):
        (run_dir / f'reference-eval-{name}').mkdir()
        json.dump(_legacy_judge(), open(run_dir / f'reference-eval-{name}' / 'summary.json', 'w'))
    assert rec.main(args) == 1, 'raw summaries alone do not finalise: the v2 matched view (r3_postfilter at t*) is still missing'
    rows = rec.read_rows(results)
    assert len(rows) == 2 and rows[1]['error'] == 'judge_missing:v2' and 'matched' in rows[1]['judge_missing']['v2'] and rec.has_row(results, run) is False
    (run_dir / 'reference-eval-v2-matched').mkdir()
    json.dump(_legacy_judge(), open(run_dir / 'reference-eval-v2-matched' / 'summary.json', 'w'))
    assert rec.main(args) == 0
    rows = rec.read_rows(results)
    assert len(rows) == 3 and rec.completed_runs(rows) == {run} and rows[2]['primary_block'] == 'v2'
    assert rec.main(args) == 0 and len(rec.read_rows(results)) == 3, 'a complete row is never duplicated'


def test_r3_record_record_refuses_raw_02_summaries_without_views_or_calibration(tmp_path):
    """Finding (f): three raw 0.2 summaries with no -t045 / -matched views and no calibration.json used to finalise a complete
    row with an empty verdict (or, with only v1's -t045, a verdict silently read from the dataset-v1 continuity block)."""
    from scripts.ml.training.sam3_mac import r3_record as rec
    root = tmp_path / 'round4'
    fold = _coco_fold(root, 'coco-r4-combo-f12-ledger-mixv2-c200-v1', per_image=(150, 200))
    run = 'r4-combo-seed1'
    run_dir = root / run
    raw = {**_legacy_judge(), 'threshold': .2}
    for name in ('v1', 'v2', 'v2val'):
        (run_dir / f'reference-eval-{name}').mkdir(parents=True)
        json.dump(raw, open(run_dir / f'reference-eval-{name}' / 'summary.json', 'w'))
    results = root / 'results.jsonl'
    args = ['record', '--root', str(root), '--run', run, '--coco', str(fold), '--judge-mode', 'full', '--extra', '{"phase": "phase1"}']
    assert rec.main(args) == 1, 'raw 0.2 summaries alone never finalise the arm'
    rows = rec.read_rows(results)
    assert len(rows) == 1 and rows[0]['error'] == 'judge_missing:v1,v2,v2val,calibration' and rec.has_row(results, run) is False
    assert 't045' in rows[0]['judge_missing']['v2'] and 'calibration.json' in rows[0]['judge_missing']['calibration'] and rows[0]['phase'] == 'phase1'
    assert rec.main(['done', '--root', str(root), '--run', run]) == 1
    # only v1's views and the calibration: still no dataset-v2 0.45 view -> error row, never a dataset-v1 verdict
    for folder in ('reference-eval-v1-t045', 'reference-eval-v1-matched'):
        (run_dir / folder).mkdir()
        json.dump(_legacy_judge(), open(run_dir / folder / 'summary.json', 'w'))
    json.dump({'t_star': .6, 'flags': []}, open(run_dir / 'calibration.json', 'w'))
    assert rec.main(args) == 1
    rows = rec.read_rows(results)
    assert len(rows) == 2 and rows[1]['error'] == 'judge_missing:v2,v2val' and rec.has_row(results, run) is False
    assert rec.select_replicate(rows, [run], 'default')['chosen'] == 'default', 'error rows never feed the replicate choice'
    # v2's 0.45 view without its matched view: still not finalised
    (run_dir / 'reference-eval-v2-t045').mkdir()
    json.dump(_legacy_judge(), open(run_dir / 'reference-eval-v2-t045' / 'summary.json', 'w'))
    (run_dir / 'reference-eval-v2val-t045').mkdir()
    json.dump(_legacy_judge(), open(run_dir / 'reference-eval-v2val-t045' / 'summary.json', 'w'))
    assert rec.main(args) == 1 and rec.read_rows(results)[-1]['error'] == 'judge_missing:v2'
    (run_dir / 'reference-eval-v2-matched').mkdir()
    json.dump(_legacy_judge(), open(run_dir / 'reference-eval-v2-matched' / 'summary.json', 'w'))
    assert rec.main(args) == 0, 'every view and the calibration present: exactly one complete row'
    rows = rec.read_rows(results)
    assert len(rows) == 4 and rec.completed_runs(rows) == {run} and rows[-1]['primary_block'] == 'v2'
    assert rows[-1]['blocks']['v2']['view_045_source'] == 'r3_postfilter' and rows[-1]['calibration']['t_star'] == .6
    assert rec.main(['done', '--root', str(root), '--run', run]) == 0


def test_r3_record_latest_checkpoint_done_and_error_rows(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    run = tmp_path / 'r3-ctrl-sq-c200-seed1'
    run.mkdir()
    assert rec.latest_checkpoint(run) is None
    for step in (1500, 3000):
        (run / f'trainable-step{step:06d}.pt').write_bytes(b'x')
    (run / 'trainable-final.pt').write_bytes(b'x')
    assert rec.latest_checkpoint(run).endswith('trainable-step003000.pt'), 'newest step checkpoint, final.pt is the caller\'s business'
    results = tmp_path / 'results.jsonl'
    assert rec.has_row(results, 'r3-ctrl-sq-c200-seed1') is False
    rec.append_row(results, rec.error_row(tmp_path, 'r3-ctrl-sq-c200-seed1', 'loader_errors'))
    assert rec.has_row(results, 'r3-ctrl-sq-c200-seed1') is False, 'an error row does not complete the arm'
    rec.append_row(results, {'run': 'r3-ctrl-sq-c200-seed1', 'base': {}})
    assert rec.has_row(results, 'r3-ctrl-sq-c200-seed1') is True
    rows = rec.read_rows(results)
    assert rows[0]['run'] == 'r3-ctrl-sq-c200-seed1_error' and rows[0]['arm'] == 'r3-ctrl-sq-c200-seed1' and rec.is_error_row(rows[0])
    assert rec.completed_runs(rows) == {'r3-ctrl-sq-c200-seed1'}
    assert rec.main(['done', '--root', str(tmp_path), '--run', 'r3-ctrl-sq-c200-seed1']) == 0
    assert rec.main(['done', '--root', str(tmp_path), '--run', 'r3-floor12-sq-ledger-c200-seed1']) == 1


def test_r3_record_build_row_from_legacy_judge_and_verdict(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    root = tmp_path / 'round3'
    fold = _coco_fold(root, 'coco-r3-sq-c200-v1', per_image=(150, 200))
    run = 'r3-floor12-sq-ledger-c200-seed1'
    run_dir = root / run
    (run_dir / 'reference-eval-v1').mkdir(parents=True)
    (run_dir / 'reference-eval-v1-vs-ctrl').mkdir()
    json.dump({'started_at': '2026-09-30T00:00:00+00:00', 'finished_at': '2026-09-30T01:50:00+00:00', 'steps': 3000, 'skipped_batches': 0,
               'loss_first': 1063.1, 'loss_median_last_10': 1496.4, 'seconds_per_step_median': 2.016, 'train_images': 151},
              open(run_dir / 'summary.json', 'w'))
    json.dump({'args': {'resume': str(run_dir / 'trainable-step001500.pt')}}, open(run_dir / 'run.json', 'w'))
    json.dump(_legacy_judge(), open(run_dir / 'reference-eval-v1' / 'summary.json', 'w'))
    json.dump(_legacy_judge(ctrl=True, base_matched=63), open(run_dir / 'reference-eval-v1-vs-ctrl' / 'summary.json', 'w'))
    (root / f'{run}.train.log').write_text('step 1\nstep 2\n')
    (root / f'{run}.train.wall').write_text('3600\n3000\n')
    row = rec.build_row(root, run, fold, arm_rule='H1', label_target='squared_gated', policy='ledger-v1', floor_px=216, judge_mode='legacy',
                        ctrl_run='r3-ctrl-sq-c200-seed1', extra={'phase': 'phase1'})
    assert row['run'] == run and row['coco_manifest_sha'] and row['fold']['images_over_200'] == 0 and row['phase'] == 'phase1'
    assert row['train']['wall_seconds'] == 6600 and row['train']['wall_segments'] == [3600, 3000] and row['train']['resumed_from'].endswith('step001500.pt')
    assert row['loader_errors'] == 0
    assert row['primary_block'] == 'v1' and row['blocks']['v2']['status'] == 'not_run'
    assert row['base']['matched_at_iou_0_5'] == 60 and row['tuned']['matched_at_iou_0_5'] == 63 and row['ctrl']['matched_at_iou_0_5'] == 63
    assert row['paired']['second_minus_first'] == 'tuned - base' and row['paired_vs_ctrl']['second_minus_first'] == 'tuned - ctrl'
    assert row['negatives']['ctrl']['predictions'] == 918
    assert 'reference_eval.py' in row['code_sha256'] and 'round3.sh' in row['code_sha256'] and row['judge_version_sha']
    assert row['judge_version'] == 'sam3-mac-reference-eval-v1'
    assert row['rules_source'].startswith('built-in defaults') and row['rules_sha'] is None
    v = row['verdict']
    assert v['against'] == 'ctrl' and v['keeps_matched'] is True
    assert v['removes_over_detection'] is None and any('novel_per_ha' in n for n in v['notes'])
    assert v['keeps_boundary'] is True and v['boundary_method'].startswith('unpaired medians')
    assert v['agreement'] == 'non_inferior' and v['agreement_ci'] == [-.0029, .0492]
    assert 'accuracy' not in row['note']
    results = root / 'results.jsonl'
    rec.append_row(results, row)
    assert len(results.read_text().splitlines()) == 1
    # the CLI refuses to write a second row for the same run
    assert rec.main(['record', '--root', str(root), '--run', run, '--coco', str(fold), '--judge-mode', 'legacy']) == 0
    assert len(results.read_text().splitlines()) == 1


def test_r3_record_verdict_reads_full_judge_shape():
    from scripts.ml.training.sam3_mac import r3_record as rec
    view = {'base': {'matched_at_iou_0_5': 60, 'boundary_distance_m_median_matched': .22},
            'ctrl': {'matched_at_iou_0_5': 62, 'boundary_distance_m_median_matched': .23},
            'tuned': {'matched_at_iou_0_5': 61, 'boundary_distance_m_median_matched': .24},
            'paired_vs_ctrl': {'best_iou_delta_mean': .012, 'best_iou_delta_ci95_image_bootstrap': [.001, .03],
                               'boundary_distance_delta_median_m': .01, 'boundary_worse': 20, 'boundary_better': 30},
            'same_image_only': {'paired_vs_ctrl': {'best_iou_delta_mean': .011, 'best_iou_delta_ci95_image_bootstrap': [-.02, .03]}},
            'negatives': {'base': {'novel_no_footprint_share_of_predictions': .06, 'novel_per_ha': 1.},
                          'tuned': {'novel_no_footprint_share_of_predictions': .10, 'novel_per_ha': 1.4}}}
    v = rec.verdict(view, rules={'unbacked_share_margin': .05, 'ignored_key': 1})
    assert v['removes_over_detection'] is True and v['keeps_matched'] is True and v['keeps_boundary'] is True
    assert v['boundary_method'].startswith('paired') and v['agreement'] == 'inconclusive' and v['agreement_scope'] == 'same_image refs'
    assert 'ignored_key' not in v['rules'] and v['rules']['unbacked_share_margin'] == .05
    pairs = {'pairs': {'tuned:ctrl': {'best_iou_delta_mean': .5}}, 'paired': [{'second_minus_first': 'ctrl - base', 'best_iou_delta_mean': .1}]}
    assert rec.find_pair(pairs, 'tuned', 'ctrl') == {'best_iou_delta_mean': .5}
    assert rec.find_pair(pairs, 'ctrl', 'base')['best_iou_delta_mean'] == .1, 'list entries are returned whole'
    assert rec.find_pair(pairs, 'tuned', 'base') is None
    assert rec.verdict(None)['notes'] == ['no 0.45 view available']


def test_round3_report_renders_synthetic_results(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    from scripts.ml.training.sam3_mac import round3_report as rep
    results = tmp_path / 'results.jsonl'
    legacy_view = rec.block_from_judge(_legacy_judge())
    ctrl_row = {'run': 'r3-ctrl-sq-c200-seed1', 'arm_rule': 'H0 control', 'coco': 'x', 'judge_mode': 'legacy', 'judge_version': 'sam3-mac-reference-eval-v1',
                'judge_version_sha': 'a' * 64, 'rules_sha': None, 'rules_source': 'built-in defaults', 'code_sha256': {'round3.sh': 'b' * 64},
                'coco_manifest_sha': 'c' * 64, 'footprint_cache_shas': {'data/elevation/derived/ms-buildings/bbox.geojsonl.gz': 'd' * 64},
                'fold': {'images': 151, 'annotations': 22968, 'ann_over_cap': 2064, 'flipped_to_background': 2067, 'images_over_200': 0},
                'train': {'loss_median_last_10': 1496.4, 'skipped_batches': 0, 'seconds_per_step_median': 2.016, 'wall_seconds': 6600, 'resumed_from': None},
                'loader_errors': 0, 'calibration': {'status': 'not_run'}, 'primary_block': 'v1',
                'blocks': {'v1': {'view_045': legacy_view, 'view_matched': None}, 'v2': {'status': 'not_run', 'reason': 'legacy judge'}},
                'verdict': {'removes_over_detection': None, 'keeps_matched': None, 'keeps_boundary': None, 'agreement': None, 'against': None,
                            'notes': ['ctrl arm absent']}}
    full_view = dict(legacy_view, ctrl=legacy_view['base'], paired_vs_ctrl=dict(legacy_view['paired_vs_base'], second_minus_first='tuned - ctrl'))
    full_view['negatives'] = {**legacy_view['negatives'], 'ctrl': legacy_view['negatives']['base']}
    full_view['negatives']['base'] = {**full_view['negatives']['base'], 'novel_per_ha': 1.2}
    full_view['negatives']['tuned'] = {**full_view['negatives']['tuned'], 'novel_per_ha': 3.9}
    floor_row = {**ctrl_row, 'run': 'r3-floor12-sq-ledger-c200-seed1', 'arm_rule': 'H1 floor 12 m2', 'judge_mode': 'full', 'primary_block': 'v2',
                 'train': {**ctrl_row['train'], 'resumed_from': '/x/trainable-step001500.pt'},
                 'calibration': {'t_star': .6, 'flags': ['density_unmatched']},
                 'blocks': {'v1': {'view_045': full_view, 'view_matched': full_view}, 'v2': {'view_045': full_view, 'view_matched': full_view},
                            'v2val': {'view_045': full_view}},
                 'verdict': {'removes_over_detection': False, 'keeps_matched': True, 'keeps_boundary': True, 'agreement': 'non_inferior',
                             'against': 'ctrl', 'notes': [], 'boundary_method': 'unpaired medians', 'agreement_scope': 'all refs'}}
    for row in (dict(ctrl_row, train={**ctrl_row['train'], 'loss_median_last_10': 1.}), floor_row,
                rec.error_row(tmp_path, 'r3-mixv2-sq-c200-seed1', 'fold_gate_failed'), ctrl_row):
        rec.append_row(results, row)
    rows, errors = rep.load(results)
    assert [r['run'] for r in rows] == ['r3-ctrl-sq-c200-seed1', 'r3-floor12-sq-ledger-c200-seed1'], 'deduped by run, first-seen order'
    assert rows[0]['train']['loss_median_last_10'] == 1496.4, 'the last row for a run wins'
    assert [e['run'] for e in errors] == ['r3-mixv2-sq-c200-seed1_error']
    text = rep.render(rows, errors, str(results), today='2026-09-30')
    assert text.count('| `r3-ctrl-sq-c200-seed1` |') >= 3 and 'r3-mixv2-sq-c200-seed1_error' in text and 'fold_gate_failed' in text
    assert '63 / 60 / 60 (of 63)' in text, 'matched tuned / ctrl / base on the full-shape row'
    assert '63 / n/a / 60 (of 63)' in text, 'ctrl absent on the legacy row'
    assert '+0.0271 [-0.0029, +0.0492] 32/31/0' in text
    assert '5.9% -> 21.9%' in text and '1.20 -> 3.90' in text and '1.83 h' in text   # 54/918 and 279/1271
    assert 'not_run: legacy judge' in text and '0.60 (density_unmatched)' in text
    assert 'matched-density view at t*' in text and 'CONTINUITY: dataset-v1' in text and 'PRIMARY: dataset-v2' in text
    assert 'accuracy' not in text.lower() and 'agreement' in text and 'Nothing is promoted' in text
    assert 'house-25822' in text and 'production ONNX cuts the sigmoid at 0.45' in text
    out = tmp_path / 'REPORT.md'
    assert rep.main(['--results', str(results), '--out', str(out)]) == 0 and out.read_text().startswith('# SAM3 teacher loop, round 3')
    assert rep.render([], [], 'none').endswith('No rows yet: the waiter has not recorded an arm.\n')


def test_round3_sh_syntax_and_dry_run(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    script = repo / 'scripts/ml/training/sam3_mac/round3.sh'
    assert subprocess.run(['sh', '-n', str(script)], capture_output=True).returncode == 0
    if not (repo / 'scripts/ml/training/.venv-sam3/bin/python').exists() or not (repo / 'scripts/ml/.venv/bin/python').exists():
        import pytest
        pytest.skip('venvs absent: dry run needs both interpreters')
    env = {**os.environ, 'DRY_RUN': '1', 'ROUND3_ROOT': str(tmp_path / 'round3')}
    bad = subprocess.run(['sh', str(script), 'bogus'], capture_output=True, text=True, env=env, cwd=repo)
    assert bad.returncode == 2 and 'usage' in bad.stderr
    run = subprocess.run(['sh', str(script), 'phase1'], capture_output=True, text=True, env=env, cwd=repo)
    assert run.returncode == 0, run.stderr
    text = run.stdout
    assert 'DRY RUN' in text and '(no run started)' in text
    for name in ('r3-ctrl-sq-c200-seed1', 'r3-floor12-sq-ledger-c200-seed1', 'r3-mixv2-sq-c200-seed1'):
        assert f'arm {name}' in text
    assert 'finetune_mps' in text and '--dtype bf16' in text and '--lr 8e-5' in text and '--seed 1' in text and '--save-every 1500' in text
    assert 'PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.7' in text and 'PYTORCH_MPS_LOW_WATERMARK_RATIO=0.5' in text and 'nohup' in text
    assert 'reference_eval' in text and 'r3_record record' in text
    assert 'fold missing' in run.stderr, 'the dry run reports absent folds without writing an error row'
    assert not (tmp_path / 'round3' / 'round3.lock').exists() and not (tmp_path / 'round3' / 'results.jsonl').exists()
    assert not (tmp_path / 'round3' / 'round3.log').exists(), 'a dry run leaves the ledger log untouched'
