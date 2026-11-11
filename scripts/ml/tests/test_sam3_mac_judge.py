"""Round-3 label and judge side: prepare_coco cap order and manifest, dataset-v2 trainclean, reference_eval summaries from
rows, r3_postfilter equivalence and r3_calibrate. No model is loaded: inference is faked where the row builder is tested."""
import json
import os

import numpy as np
import pytest
from PIL import Image
from scripts.ml.training.sam3_mac import make_v2_trainclean as tc
from scripts.ml.training.sam3_mac import prepare_coco
from scripts.ml.training.sam3_mac import r3_calibrate as cal
from scripts.ml.training.sam3_mac import r3_postfilter as pf
from scripts.ml.training.sam3_mac import reference_eval as ev


def _dataset(root, name, anns_by_image, px=.236, size=64, centres=None, split='train'):
    folder = root / name
    (folder / 'images').mkdir(parents=True, exist_ok=True)
    images, anns, k = [], [], 1
    for i, areas in enumerate(anns_by_image, start=1):
        Image.new('RGB', (size, size), (i * 20, 90, 90)).save(folder / 'images' / f'{name}-{i}.png')
        row = {'id': i, 'file_name': f'images/{name}-{i}.png', 'width': size, 'height': size, 'pixel_size_m': px, 'scene_id': f'scene-{name}-{i}'}
        if centres:
            row['centre_lonlat'] = centres[i - 1]
        images.append(row)
        for area in areas:
            side = int(round(area ** .5))
            anns.append({'id': k, 'image_id': i, 'category_id': 1, 'bbox': [0, 0, side, side], 'area': area, 'iscrowd': 0,
                         'segmentation': [[0, 0, side, 0, side, side, 0, side]], 'weight': 1., 'provenance': 'hires-teacher',
                         'area_m2': round(area * px * px, 2)})
            k += 1
    json.dump({'images': images, 'annotations': anns, 'categories': [{'id': 1, 'name': 'building_roof'}]}, open(folder / f'{split}.json', 'w'))
    return folder


def test_prepare_coco_sorts_by_area_before_cap_and_writes_density(tmp_path):
    a = _dataset(tmp_path, 'distill-a', [[100, 900, 400], [50]])
    out = tmp_path / 'coco'
    manifest = prepare_coco.convert([a], out, max_ann_per_image=2)
    train = json.load(open(out / 'train' / '_annotations.coco.json'))
    kept = sorted(x['area'] for x in train['annotations'] if x['image_id'] == 1)
    assert kept == [400, 900], 'the cap keeps the two largest, whatever the export order'
    t = manifest['splits']['train']
    assert t['flipped_to_background'] == {'ann_low_weight': 0, 'ann_over_cap': 1, 'ann_tiny': 0, 'total': 1}
    assert t['images_at_cap'] == 1 and t['images_over_fork_max'] == 0 and t['annotations_per_image_max'] == 2
    per = {p['id']: p for p in t['per_image']}
    ha = (64 * .236) ** 2 / 1e4
    assert per[1]['annotations'] == 2 and abs(per[1]['density_per_ha'] - 2 / ha) < .01 and per[1]['median_area_m2'] == round(900 * .236 ** 2, 1)   # upper median of two values, like annotations_per_image_median
    assert per[2]['annotations'] == 1 and t['per_source']['distill-a']['ann_over_cap'] == 1
    assert t['density_per_ha_median'] is not None and t['median_area_m2_median'] is not None
    assert prepare_coco.gate(out) == (True, {'train_images': 2, 'annotations': 3, 'max_annotations_per_image': 2, 'images_over': 0})


def test_prepare_coco_distance_check_flags_train_images_near_judged_scenes(tmp_path):
    scenes = tmp_path / 'scenes'
    for sid, bounds in (('j-near', [145.0000, -37.8000, 145.0020, -37.7985]), ('j-nobounds', None)):
        (scenes / sid).mkdir(parents=True)
        json.dump({'region': {'bounds': bounds} if bounds else {}}, open(scenes / sid / 'latest.json', 'w'))
    judge = tmp_path / 'dataset-j'
    judge.mkdir()
    json.dump({'images': [{'id': 1, 'scene_id': 'j-near', 'file_name': 'x', 'width': 8, 'height': 8},
                          {'id': 2, 'scene_id': 'j-nobounds', 'file_name': 'y', 'width': 8, 'height': 8}], 'annotations': [], 'categories': []},
              open(judge / 'test.json', 'w'))
    # train image 1 sits 100 m east of the judged tile, image 2 sits 5 km away
    near = [145.0020 + 100 / (111320 * np.cos(np.radians(37.8))) + 64 * .236 / 2 / (111320 * np.cos(np.radians(37.8))), -37.79925]
    far = [145.06, -37.79925]
    a = _dataset(tmp_path, 'distill-a', [[400], [400]], centres=[near, far])
    manifest = prepare_coco.convert([a], tmp_path / 'coco', judge_datasets=[judge], scenes=scenes)
    check = manifest['splits']['train']['distance_check']
    assert check['judged_scenes'] == 2 and check['judged_scenes_without_bounds'] == 1 and check['train_images_checked'] == 2
    assert check['train_images_flagged'] == 1 and check['flagged'][0]['judged_scene'] == 'j-near' and 80 < check['flagged'][0]['distance_m'] < 120
    assert check['min_distance_m_per_image']['2'] > 4000 if '2' in check['min_distance_m_per_image'] else check['min_distance_m_per_image'][2] > 4000
    assert prepare_coco.bounds_distance_m((0, 0, 1, 1), (.5, .5, 2, 2)) == 0.


def test_make_v2_trainclean_keeps_clean_train_tiles_and_checks_disjointness(tmp_path):
    ds = tmp_path / 'dataset-v2'
    (ds / 'images').mkdir(parents=True)
    imgs = []
    for i, (tti, ignore) in enumerate([(0, 0), (1, 0), (0, 64 * 64 // 10)], start=1):
        Image.new('RGB', (64, 64)).save(ds / 'images' / f't{i}.png')
        imgs.append({'id': i, 'file_name': f'images/t{i}.png', 'width': 64, 'height': 64, 'pixel_size_m': .236, 'scene_id': f'moor-{i}',
                     'tile_group': f'z17:{i}:0', 'sha256': f'sha{i}', 'test_tiles_ignored': tti, 'ignore_pixels': ignore})
    anns = [{'id': i, 'image_id': i, 'provenance': 'machine_pseudo', 'area_m2': 100. + i, 'segmentation': [[0, 0, 4, 0, 4, 4]]} for i in (1, 2, 3)]
    json.dump({'images': imgs, 'annotations': anns, 'categories': [{'id': 1, 'name': 'building_roof'}]}, open(ds / 'train.json', 'w'))
    json.dump({'images': [{'id': 9, 'scene_id': 'house-1', 'tile_group': 'z17:9:9', 'sha256': 'other'}], 'annotations': []}, open(ds / 'test.json', 'w'))
    json.dump({'version': 'x'}, open(ds / 'manifest.json', 'w'))
    out = tmp_path / 'dataset-v2-trainclean'
    manifest = tc.build(ds, out, max_ignore_share=.01, other_tests=[])
    kept = json.load(open(out / 'train.json'))
    assert [i['id'] for i in kept['images']] == [1] and [a['id'] for a in kept['annotations']] == [1]
    assert manifest['counts'] == {**manifest['counts'], 'kept_images': 1, 'excluded': {'test_tiles_ignored': 1, 'ignore_share': 1}}
    assert manifest['disjointness_ok'] is True and manifest['disjointness']['dataset-v2/test']['sha256']['overlap'] == 0
    assert os.path.islink(out / 'images') and (out / 'images' / 't1.png').exists()
    assert 'fold input only' in manifest['statement'] and not (out / 'val.json').exists() and not (out / 'references.json').exists()
    # a shared tile_group with the test split is reported as an overlap
    json.dump({'images': [{'id': 9, 'scene_id': 'house-1', 'tile_group': 'z17:1:0', 'sha256': 'other'}], 'annotations': []}, open(ds / 'test.json', 'w'))
    assert tc.build(ds, out, other_tests=[])['disjointness_ok'] is False


# --- reference_eval from rows ------------------------------------------------------------------------------------------

def _context():
    images = {'1': {'scene_id': 's1', 'pixel_size_m': .236, 'width': 1152, 'height': 1152, 'hectares': round((1152 * .236) ** 2 / 1e4, 4),
                    'pixel_band': '0_236', 'pseudo_ids': [101, 102], 'footprints_in_tile': 3, 'references': 2, 'tile_group': 'g1'},
              '2': {'scene_id': 's2', 'pixel_size_m': .1, 'width': 512, 'height': 512, 'hectares': round((512 * .1) ** 2 / 1e4, 4),
                    'pixel_band': '0_08_0_15', 'pseudo_ids': [201], 'footprints_in_tile': 0, 'references': 1, 'tile_group': 'g2'}}
    refs = {'1:1': {'image_id': 1, 'scene_id': 's1', 'ann_id': 1, 'ref_id': None, 'object_id': 'o1', 'tier': 'T1', 'provenance': 'human_T1', 'area_m2': 20.,
                    'area_band': 'lt_25', 'image_identity': 'same_image', 'pixel_band': '0_236', 'segmentation_sha256': 'a', 'vertices': 4,
                    'right_angle_share': 1., 'edit_diff': {'hausdorff_m': .9, 'stratum': 'moved_ge_0_45'}},
            '1:2': {'image_id': 1, 'scene_id': 's1', 'ann_id': 2, 'ref_id': None, 'object_id': 'o2', 'tier': 'T2', 'provenance': 'human_T2', 'area_m2': 80.,
                    'area_band': '50_150', 'image_identity': 'same_image', 'pixel_band': '0_236', 'segmentation_sha256': 'b', 'vertices': 6,
                    'right_angle_share': .8, 'edit_diff': None},
            '2:3': {'image_id': 2, 'scene_id': 's2', 'ann_id': 3, 'ref_id': 'r3', 'object_id': 'ref-r3', 'tier': 'T1', 'provenance': 'human_T1', 'area_m2': 400.,
                    'area_band': 'ge_300', 'image_identity': 'different_image', 'pixel_band': '0_08_0_15', 'segmentation_sha256': 'c', 'vertices': 8,
                    'right_angle_share': .5, 'edit_diff': None}}
    return {'version': 'test', 'dataset': '/x/dataset-v2', 'split': 'test', 'images': images, 'refs': refs,
            'arms': ['base', 'ctrl', 'tuned'], 'arm_specs': ['base', 'ctrl=c.pt', 'tuned=t.pt'], 'pairs': ['tuned:base', 'tuned:ctrl', 'ctrl:base'],
            'meta': {'rows_collected_at_threshold': .2}}


def _pred(arm, image_id, index, score, area_m2, kind, backed=None, ref_overlaps=(), pseudo_overlaps=(), vertices=4, right=1.):
    best = max((o['iou'] for o in ref_overlaps), default=0.)
    matched = next((o['ref'] for o in ref_overlaps if o['iou'] >= .5), None)
    return {'arm': arm, 'image_id': image_id, 'scene_id': f's{image_id}', 'index': index, 'pixel_size_m': .236 if image_id == 1 else .1,
            'image_ha': 7.39 if image_id == 1 else .262, 'score': score, 'area_px': 100, 'area_m2': area_m2, 'kind': kind, 'footprint_backed': backed,
            'matched_ref_id': matched, 'best_iou': best, 'ref_overlaps': list(ref_overlaps), 'pseudo_overlaps': list(pseudo_overlaps),
            'vertices': vertices, 'corners': vertices, 'right_corners': int(vertices * right), 'right_angle_share': right}


def _predictions():
    rows = []
    # base: matches ref 1:1 weakly, 1:2 well, misses 2:3; one novel unbacked small detection at low score
    rows += [_pred('base', 1, 0, .9, 21., 'human', True, [{'ref': '1:1', 'iou': .55, 'boundary_m': .3, 'area_ratio': 1.05}], [{'id': 101, 'iou': .6}]),
             _pred('base', 1, 1, .8, 82., 'human', True, [{'ref': '1:2', 'iou': .9, 'boundary_m': .2, 'area_ratio': 1.}], [{'id': 102, 'iou': .7}]),
             _pred('base', 1, 2, .3, 8., 'novel', False),
             _pred('base', 2, 0, .7, 380., 'human', None, [{'ref': '2:3', 'iou': .4}], [{'id': 201, 'iou': .55}])]
    # ctrl: same as base but the 1:1 match improves and the novel one scores high
    rows += [_pred('ctrl', 1, 0, .9, 21., 'human', True, [{'ref': '1:1', 'iou': .7, 'boundary_m': .25, 'area_ratio': 1.02}], [{'id': 101, 'iou': .6}]),
             _pred('ctrl', 1, 1, .8, 82., 'human', True, [{'ref': '1:2', 'iou': .9, 'boundary_m': .2, 'area_ratio': 1.}], [{'id': 102, 'iou': .7}]),
             _pred('ctrl', 1, 2, .6, 8., 'novel', False),
             _pred('ctrl', 2, 0, .7, 380., 'human', None, [{'ref': '2:3', 'iou': .4}], [{'id': 201, 'iou': .55}])]
    # tuned: matches everything at 0.2 but the 2:3 match sits at score 0.4 (lost at 0.45); two novel detections, one backed
    rows += [_pred('tuned', 1, 0, .9, 21., 'human', True, [{'ref': '1:1', 'iou': .8, 'boundary_m': .2, 'area_ratio': 1.}], [{'id': 101, 'iou': .6}]),
             _pred('tuned', 1, 1, .8, 82., 'human', True, [{'ref': '1:2', 'iou': .85, 'boundary_m': .22, 'area_ratio': .98}], [{'id': 102, 'iou': .7}]),
             _pred('tuned', 1, 2, .7, 9., 'novel', False), _pred('tuned', 1, 3, .5, 30., 'novel', True),
             _pred('tuned', 2, 0, .4, 380., 'human', None, [{'ref': '2:3', 'iou': .6, 'boundary_m': .4, 'area_ratio': 1.1}], [{'id': 201, 'iou': .55}])]
    return rows


def test_image_bootstrap_weights_images_by_draw_count():
    diffs = {'A': [.1], 'B': [.1], 'C': [-.5, -.5, -.5, -.5]}
    rng = np.random.default_rng(7)
    pick = rng.choice(3, 3, replace=True)
    idx, counts = np.unique(pick, return_counts=True)
    sums = np.array([.1, .1, -2.])
    ns = np.array([1., 1., 4.])
    expected = float((sums[idx] * counts).sum() / (ns[idx] * counts).sum())
    lo, hi = ev.image_bootstrap(diffs, seed=7, draws=1)
    assert lo == hi == round(expected, 4)
    if len(set(pick.tolist())) < 3:
        naive = float(sums[idx].sum() / ns[idx].sum())        # the old set(pick) reading loses multiplicity
        assert round(naive, 4) != lo
    lo, hi = ev.image_bootstrap(diffs, seed=1, draws=500)
    assert -.5 <= lo <= hi <= .1
    assert ev.image_bootstrap({}) is None


def test_build_summary_strata_pairs_negatives_and_postfilter_equivalence(tmp_path):
    context, preds = _context(), _predictions()
    arms, pairs = context['arms'], [tuple(p.split(':')) for p in context['pairs']]
    raw = ev.build_summary(context, preds, arms, pairs, .2)
    assert raw['base']['matched_at_iou_0_5'] == 2 and raw['ctrl']['matched_at_iou_0_5'] == 2 and raw['tuned']['matched_at_iou_0_5'] == 3
    assert raw['tuned']['different_image'] == {'references': 1, 'matched': 1, 'best_iou_median': .6}
    assert set(raw['pairs']) == {'tuned:base', 'tuned:ctrl', 'ctrl:base'} and raw['paired'] is raw['pairs']['tuned:base']
    tb = raw['pairs']['tuned:base']
    assert tb['second_minus_first'] == 'tuned - base' and tb['references'] == 3 and tb['per_reference'] == {'higher': 2, 'lower': 1, 'equal': 0}
    assert abs(tb['best_iou_delta_mean'] - ((.8 - .55) + (.85 - .9) + (.6 - .4)) / 3) < 1e-4   # reported to 4 decimals
    assert tb['jointly_matched'] == 2 and tb['boundary_worse'] == 1 and tb['boundary_better'] == 1 and tb['matched_delta'] == 1
    assert tb['same_image_only']['references'] == 2 and tb['different_image_only']['references'] == 1
    assert len(tb['best_iou_delta_ci95_image_bootstrap']) == 2
    assert raw['by_area_band']['lt_25']['references'] == 1 and raw['by_area_band']['ge_300']['tuned']['matched'] == 1
    assert raw['pairs_by_area_band']['50_150']['tuned:base']['best_iou_delta_mean'] == round(.85 - .9, 4)
    assert raw['by_tier']['T1']['references'] == 2 and raw['by_pixel_band']['0_08_0_15']['references'] == 1
    neg = raw['negatives']
    ha = context['images']['1']['hectares'] + context['images']['2']['hectares']
    assert neg['base']['predictions'] == 4 and abs(neg['base']['predictions_per_ha'] - 4 / ha) < 1e-3
    assert neg['tuned']['novel'] == 2 and abs(neg['tuned']['novel_per_ha'] - 2 / ha) < 1e-3
    assert neg['tuned']['novel_footprint_backed'] == 1 and neg['tuned']['novel_no_footprint'] == 1 and neg['tuned']['novel_no_footprint_share_of_predictions'] == .2
    assert neg['tuned']['novel_area_histogram'] == {'lt_12': 1, '12_25': 0, '25_50': 1, '50_150': 0, 'ge_150': 0} and neg['tuned']['novel_share_under_12m2'] == .5
    assert neg['base']['pseudo_recall_at_0_5'] == round(3 / 3, 4) and neg['base']['footprint_covered_tiles'] == 1
    assert neg['base']['covered_tiles']['novel_no_footprint_share_of_predictions'] == round(1 / 3, 4)
    assert raw['shape_agreement']['tuned']['matched'] == 3 and raw['shape_agreement']['tuned']['edit_diff_strata']['moved_ge_0_45']['matched'] == 1
    assert 'accuracy' not in raw['note'].replace('not calibrated accuracy', '')
    # the 0.45 view from the same rows: tuned loses the 2:3 match (score 0.4) and base loses its low-score novel row
    view = ev.build_summary(context, preds, arms, pairs, .45)
    assert view['tuned']['matched_at_iou_0_5'] == 2 and view['negatives']['base']['novel'] == 0 and view['negatives']['ctrl']['novel'] == 1
    # r3_postfilter rebuilds exactly that view from the folder
    folder = tmp_path / 'judge'
    folder.mkdir()
    ev.write_jsonl(folder / 'predictions.jsonl', preds)
    ev.write_jsonl(folder / 'timings.jsonl', [{'arm': a, 'image_id': i, 'predictions': 2, 'seconds': .5, 'footprints_in_tile': 0} for a in arms for i in (1, 2)])
    json.dump(context, open(folder / 'context.json', 'w'))
    json.dump({'version': 'x', 'computed_at': 'y'}, open(folder / 'summary.json', 'w'))
    payload = pf.postfilter(folder / 'predictions.jsonl', .45, tmp_path / 'judge-t045')
    direct = ev.build_summary(context, [p for p in preds if p['score'] > .45], arms, pairs, .45,
                              [{'arm': a, 'image_id': i, 'predictions': 2, 'seconds': .5, 'footprints_in_tile': 0} for a in arms for i in (1, 2)])
    assert payload['summary'] == direct and payload['threshold'] == .45 and payload['view_threshold'] == .45
    assert json.load(open(tmp_path / 'judge-t045' / 'summary.json'))['summary']['tuned']['matched_at_iou_0_5'] == 2
    assert payload['rows'][0]['arm'] == 'base' and {t['arm'] for t in payload['tiles']} == set(arms)
    with pytest.raises(SystemExit):
        pf.postfilter(folder / 'predictions.jsonl', .1)


def test_postfilter_per_arm_cuts_build_the_matched_view_from_calibration(tmp_path):
    context, preds = _context(), _predictions()
    arms, pairs = context['arms'], [tuple(p.split(':')) for p in context['pairs']]
    run = tmp_path / 'r3-floor12-sq-ledger-c200-seed1'
    folder = run / 'reference-eval-v2'
    folder.mkdir(parents=True)
    ev.write_jsonl(folder / 'predictions.jsonl', preds)
    json.dump(context, open(folder / 'context.json', 'w'))
    cuts = {'base': .45, 'ctrl': .55, 'tuned': .6}
    payload = pf.postfilter(folder / 'predictions.jsonl', .6, tmp_path / 'by-hand', arm_thresholds=cuts)
    direct = ev.build_summary(context, [p for p in preds if p['score'] > cuts[p['arm']]], arms, pairs, .45)
    assert payload['summary'] == direct and payload['threshold'] == .45 and payload['arm_thresholds'] == cuts and payload['view'] == 'matched_density'
    assert payload['arm_thresholds_source'] == '--arm-threshold'
    s = payload['summary']
    assert s['base']['matched_at_iou_0_5'] == 2 and s['negatives']['base']['novel'] == 0, 'base at its 0.45 reference cut'
    assert s['ctrl']['matched_at_iou_0_5'] == 2 and s['negatives']['ctrl']['novel'] == 1, 'ctrl at its own t* 0.55 keeps the 0.6 novel'
    assert s['tuned']['matched_at_iou_0_5'] == 2 and s['negatives']['tuned']['novel'] == 1, 'tuned at 0.6 loses the 0.5 novel and the 0.4 match'
    assert s['pairs']['tuned:base']['second_minus_first'] == 'tuned - base', 'pairs compare each arm at its own cut'
    # the runner passes the single t* and an --out ending in -matched: calibration.json beside the judge folder supplies the per-arm cuts
    json.dump({'rule': {'base_arm': 'base', 'reference_cut': .45}, 'primary_arm': 'tuned', 't_star': .6,
               'arms': {'ctrl': {'t_star': .55}, 'tuned': {'t_star': .6}}}, open(run / 'calibration.json', 'w'))
    auto = pf.postfilter(folder / 'predictions.jsonl', .6, run / 'reference-eval-v2-matched')
    assert auto['summary'] == direct and auto['arm_thresholds'] == cuts and 'calibration.json' in auto['arm_thresholds_source']
    assert json.load(open(run / 'reference-eval-v2-matched' / 'summary.json'))['arm_thresholds'] == cuts
    plain = pf.postfilter(folder / 'predictions.jsonl', .45, run / 'reference-eval-v2-t045')
    assert 'arm_thresholds' not in plain and plain['threshold'] == .45, 'the 0.45 view is untouched'
    with pytest.raises(SystemExit):   # the runner's t* must be the calibration's primary t*
        pf.postfilter(folder / 'predictions.jsonl', .7, run / 'reference-eval-v1-matched')
    with pytest.raises(SystemExit):   # a per-arm cut below the collection threshold has no rows
        pf.postfilter(folder / 'predictions.jsonl', .6, arm_thresholds={'base': .1})
    with pytest.raises(SystemExit):
        pf.postfilter(folder / 'predictions.jsonl', .6, arm_thresholds={'ghost': .5})
    # an arm the calibration never saw is cut at the view threshold and noted
    json.dump({'rule': {'base_arm': 'base', 'reference_cut': .45}, 'primary_arm': 'tuned', 't_star': .6, 'arms': {'tuned': {'t_star': .6}}},
              open(run / 'calibration.json', 'w'))
    noted = pf.postfilter(folder / 'predictions.jsonl', .6, run / 'reference-eval-v1-matched')
    assert noted['arm_thresholds'] == {'base': .45, 'ctrl': .6, 'tuned': .6} and noted['arm_threshold_notes'][0].startswith('ctrl: no t*')
    assert pf.parse_arm_thresholds(['base=0.45', 'tuned=0.6']) == {'base': .45, 'tuned': .6}
    assert pf.main(['--predictions', str(folder / 'predictions.jsonl'), '--threshold', '0.6', '--out', str(tmp_path / 'cli'),
                    '--arm-threshold', 'base=0.45', '--arm-threshold', 'ctrl=0.55']) == 0
    assert json.load(open(tmp_path / 'cli' / 'summary.json'))['arm_thresholds'] == cuts


def test_parse_pairs_and_bands():
    assert ev.parse_pairs(['tuned:base', 'tuned:ctrl', 'ctrl:base'], ['base', 'ctrl', 'tuned']) == [('tuned', 'base'), ('tuned', 'ctrl'), ('ctrl', 'base')]
    assert ev.parse_pairs(None, ['base', 'tuned']) == [('tuned', 'base')]
    assert ev.parse_pairs(None, ['base', 'ctrl', 'tuned']) == []
    with pytest.raises(SystemExit):
        ev.parse_pairs(['tuned:nope'], ['base', 'tuned'])
    assert [ev.band_of(v, ev.AREA_BANDS) for v in (10, 25, 149.9, 300, 5000)] == ['lt_25', '25_50', '50_150', 'ge_300', 'ge_300']
    assert [ev.band_of(v, ev.PIXEL_BANDS) for v in (.047, .1, .2, .236, .35)] == ['le_0_08', '0_08_0_15', '0_15_0_23', '0_236', '0_236']
    assert ev.band_of(None, ev.AREA_BANDS) is None


def test_evaluate_arm_rows_carry_the_schema_with_a_fake_model(tmp_path, monkeypatch):
    ds = tmp_path / 'dataset-v2'
    (ds / 'images').mkdir(parents=True)
    Image.new('RGB', (64, 64)).save(ds / 'images' / 'a.png')
    ref = {'id': 1, 'image_id': 1, 'provenance': 'human_T1', 'boundary_reference': True, 'segmentation': [[10, 10, 30, 10, 30, 30, 10, 30]],
           'area_m2': 22.3, 'tier': 'T1', 'image_identity': 'same_image', 'object_id': 'o1', 'scene_id': 'nowhere'}
    pseudo = {'id': 2, 'image_id': 1, 'provenance': 'machine_pseudo', 'segmentation': [[40, 40, 60, 40, 60, 60, 40, 60]]}
    images = [{'id': 1, 'file_name': 'images/a.png', 'width': 64, 'height': 64, 'pixel_size_m': .236, 'scene_id': 'nowhere', 'tile_group': 'g'}]
    json.dump({'images': images, 'annotations': [ref, pseudo], 'categories': []}, open(ds / 'test.json', 'w'))
    json.dump({'images': images, 'annotations': [pseudo], 'categories': []}, open(ds / 'val.json', 'w'))
    assert [len(r) for _, r, p in ev.human_references(ds, 'test')] == [1]
    assert [len(r) for _, r, p in ev.human_references(ds, 'val')] == [0], 'a split without human outlines is judged on every image'
    m_ref, m_pseudo, m_novel = (np.zeros((64, 64), bool) for _ in range(3))
    m_ref[10:30, 10:30] = True                   # matches the reference
    m_pseudo[40:60, 40:60] = True                # pseudo only
    m_novel[0:6, 50:60] = True                   # novel
    monkeypatch.setattr(ev.smoke, 'load_torch_model', lambda *a, **k: (None, type('P', (), {'confidence_threshold': .45})(), .1))
    monkeypatch.setattr(ev.smoke, 'run_torch', lambda *a, **k: ([m_ref, m_pseudo, m_novel], [.9, .6, .3], {'total_s': .2}))
    rows_images = ev.human_references(ds, 'test')
    fp_cache, ref_polys = {}, {}
    preds, timings, load_s = ev.evaluate_arm('base', 'w.pt', None, 'cpu', 'fp32', rows_images, 'building roof', None, .2, ds, fp_cache, ref_polys)
    assert [p['kind'] for p in preds] == ['human', 'pseudo_only', 'novel'] and [p['score'] for p in preds] == [.9, .6, .3]
    required = {'arm', 'image_id', 'scene_id', 'pixel_size_m', 'image_ha', 'score', 'area_m2', 'kind', 'footprint_backed', 'matched_ref_id', 'best_iou',
                'ref_overlaps', 'pseudo_overlaps', 'vertices', 'right_angle_share'}
    assert all(required <= set(p) for p in preds) and preds[0]['matched_ref_id'] == '1:1' and preds[0]['best_iou'] > .9
    assert preds[0]['ref_overlaps'][0]['boundary_m'] is not None and abs(preds[0]['ref_overlaps'][0]['area_ratio'] - 1.) < .15
    assert preds[1]['pseudo_overlaps'][0]['id'] == 2 and preds[2]['footprint_backed'] is None
    assert abs(preds[0]['image_ha'] - (64 * .236) ** 2 / 1e4) < 1e-4 and timings[0]['predictions'] == 3
    context = ev.build_context(ds, 'test', rows_images, fp_cache, ref_polys, {})
    assert context['refs']['1:1']['area_band'] == 'lt_25' and context['images']['1']['pseudo_ids'] == [2]
    summary = ev.build_summary(context, preds, ['base'], [], .45)
    assert summary['base']['matched_at_iou_0_5'] == 1 and summary['negatives']['base']['predictions'] == 2 and summary['negatives']['base']['novel'] == 0
    assert summary['negatives']['base']['pseudo_recall_at_0_5'] == 1.


def test_calibrate_picks_the_smallest_qualifying_threshold(tmp_path):
    images = {str(i): {'scene_id': f'v{i}', 'pixel_size_m': .236, 'width': 1152, 'height': 1152, 'hectares': 7.39, 'pixel_band': '0_236',
                       'pseudo_ids': [1, 2], 'footprints_in_tile': 1, 'references': 0, 'tile_group': f'g{i}'} for i in (1, 2)}
    context = {'version': 't', 'dataset': '/x/dataset-v2', 'split': 'val', 'images': images, 'refs': {}, 'arms': ['base', 'tuned'],
               'arm_specs': ['base', 'tuned=t.pt'], 'pairs': ['tuned:base'], 'meta': {'rows_collected_at_threshold': .2}}
    preds = []
    for i in (1, 2):
        preds += [_pred('base', i, 0, .9, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 1, 'iou': .8}]),
                  _pred('base', i, 1, .8, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 2, 'iou': .8}])]
        preds += [_pred('tuned', i, 0, .9, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 1, 'iou': .8}]),
                  _pred('tuned', i, 1, .8, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 2, 'iou': .8}]),
                  _pred('tuned', i, 2, .58, 8., 'novel', False), _pred('tuned', i, 3, .52, 7., 'novel', False), _pred('tuned', i, 4, .3, 6., 'novel', False)]
    folder = tmp_path / 'v2val'
    folder.mkdir()
    ev.write_jsonl(folder / 'predictions.jsonl', preds)
    json.dump(context, open(folder / 'context.json', 'w'))
    rules = tmp_path / 'RULES.json'
    json.dump({'calibration': {'density_ratio_max': 1.10, 'recall_slack': .02}}, open(rules, 'w'))
    out = cal.calibrate(rules, folder / 'predictions.jsonl', tmp_path / 'calibration.json')
    assert out['t_star'] == .6 and out['flags'] == [] and out['primary_arm'] == 'tuned' and out['human_references_on_val'] == 0
    table = {r['threshold']: r for r in out['arms']['tuned']['table']}
    assert table[.45]['qualifies'] is False and table[.55]['qualifies'] is False and table[.6]['qualifies'] is True
    assert table[.6]['pseudo_recall_at_0_5'] == 1. and out['base_reference']['predictions_per_ha'] == round(4 / (2 * 7.39), 3)
    assert json.load(open(tmp_path / 'calibration.json'))['t_star'] == .6
    # a tuned arm that stays dense at every cut falls back to the top of the grid, flagged
    dense = preds + [_pred('tuned', i, 5 + k, .95, 5., 'novel', False) for i in (1, 2) for k in range(3)]
    ev.write_jsonl(folder / 'predictions.jsonl', dense)
    out = cal.calibrate(rules, folder / 'predictions.jsonl')
    assert out['t_star'] == .8 and out['flags'] == ['density_unmatched']


def test_calibrate_chooses_t_star_on_non_adjacent_val_images_for_flagged_runs(tmp_path):
    images = {'1': {'scene_id': 'mooroolbark-50011498-17-118440-80414', 'pixel_size_m': .236, 'width': 1152, 'height': 1152, 'hectares': 7.39,
                    'pixel_band': '0_236', 'pseudo_ids': [1, 2], 'footprints_in_tile': 1, 'references': 0, 'tile_group': 'z17:118440:80414'},
              '2': {'scene_id': 'roof_training_250-25018', 'pixel_size_m': .1, 'width': 512, 'height': 512, 'hectares': .262,
                    'pixel_band': '0_08_0_15', 'pseudo_ids': [1, 2], 'footprints_in_tile': 0, 'references': 0, 'tile_group': 'z17:1:1'}}
    context = {'version': 't', 'dataset': '/x/dataset-v2', 'split': 'val', 'images': images, 'refs': {}, 'arms': ['base', 'tuned'],
               'arm_specs': ['base', 'tuned=t.pt'], 'pairs': ['tuned:base'], 'meta': {'rows_collected_at_threshold': .2}}
    preds = []
    for i in (1, 2):
        preds += [_pred('base', i, 0, .9, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 1, 'iou': .8}]),
                  _pred('base', i, 1, .8, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 2, 'iou': .8}]),
                  _pred('tuned', i, 0, .9, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 1, 'iou': .8}]),
                  _pred('tuned', i, 1, .8, 100., 'pseudo_only', True, pseudo_overlaps=[{'id': 2, 'iou': .8}])]
    preds += [_pred('tuned', 1, 2 + k, .95, 5., 'novel', False) for k in range(6)]          # dense next door at every cut
    preds += [_pred('tuned', 2, 2, .58, 8., 'novel', False), _pred('tuned', 2, 3, .52, 7., 'novel', False)]   # matches base density from 0.6
    rules = tmp_path / 'RULES.json'
    json.dump({'calibration': {'val_adjacent_to_train': {'r3-mixv2-sq-c200-seed1': {'exclude_scene_prefix': 'mooroolbark-', 'reason': 'buffer'}}}},
              open(rules, 'w'))
    for run in ('r3-mixv2-sq-c200-seed1', 'r3-ctrl-sq-c200-seed1'):
        folder = tmp_path / run / 'reference-eval-v2val'
        folder.mkdir(parents=True)
        ev.write_jsonl(folder / 'predictions.jsonl', preds)
        json.dump(context, open(folder / 'context.json', 'w'))
    mix = cal.calibrate(rules, tmp_path / 'r3-mixv2-sq-c200-seed1' / 'reference-eval-v2val' / 'predictions.jsonl', tmp_path / 'mix.json')
    assert mix['t_star'] == .6 and mix['flags'] == ['val_adjacent_to_train', 'calibrated_on_val_subset'] and mix['images'] == 1
    assert mix['val_subset'] == {'run': 'r3-mixv2-sq-c200-seed1', 'excluded_scene_prefix': 'mooroolbark-', 'images_used': 1, 'images_excluded': 1,
                                 'reason': 'buffer', 'scene_ids_used': ['roof_training_250-25018']}
    assert mix['full_val']['t_star'] == .8 and mix['full_val']['flags'] == ['density_unmatched'] and mix['full_val']['images'] == 2
    assert mix['base_reference']['predictions_per_ha'] == round(2 / .262, 3), 'base\'s reference density is read on the same subset'
    assert json.load(open(tmp_path / 'mix.json'))['flags'] == mix['flags']
    ctrl = cal.calibrate(rules, tmp_path / 'r3-ctrl-sq-c200-seed1' / 'reference-eval-v2val' / 'predictions.jsonl')
    assert ctrl['t_star'] == .8 and ctrl['flags'] == ['density_unmatched'] and ctrl['images'] == 2 and 'val_subset' not in ctrl and 'full_val' not in ctrl
    forced = cal.calibrate(rules, tmp_path / 'r3-ctrl-sq-c200-seed1' / 'reference-eval-v2val' / 'predictions.jsonl', run='r3-mixv2-sq-c200-seed1')
    assert forced['t_star'] == .6 and 'val_adjacent_to_train' in forced['flags']
    assert cal.run_name_for('/r/r3-x/reference-eval-v2val/predictions.jsonl') == 'r3-x'
