"""Round-4 orchestration tests (round4.sh, r3_record --ref / --against / select-replicate, round3_report round-4 rendering,
the pre-registered RULES.json). Kept apart from test_sam3_mac.py so the trainer-side edits there and these runner-side
tests never collide. Wording: agreement, model estimate, reference; nothing here promotes anything."""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
R4 = REPO / 'data/reviews/eyes/training/sam3-mac/round4'
ARM_KEYS = ('matched_at_iou_0_5', 'best_iou_median', 'boundary_distance_m_median_matched')


def _arm(matched, best, boundary, n=62):
    return {'references': n, 'matched_at_iou_0_5': matched, 'recall_at_0_5': round(matched / n, 4), 'best_iou_median': best,
            'boundary_distance_m_median_matched': boundary, 'predictions_per_image_median': 12., 'seconds_per_image_median': .5}


def _neg(novel, unbacked, total, per_ha, tiles=45):
    return {'tiles': tiles, 'predictions': total, 'predictions_per_tile_median': total / tiles, 'novel': novel, 'novel_no_footprint': unbacked,
            'novel_no_footprint_share_of_predictions': round(unbacked / total, 4), 'novel_share': round(novel / total, 4),
            'novel_per_ha': per_ha, 'novel_area_m2_median': 9.1}


def _pair(second, first, delta, ci, higher, lower, b_delta=.0, worse=20, better=30):
    return {'second_minus_first': f'{second} - {first}', 'second': second, 'first': first, 'references': 62, 'best_iou_delta_mean': delta,
            'best_iou_delta_ci95_image_bootstrap': ci, 'per_reference': {'higher': higher, 'lower': lower, 'equal': 62 - higher - lower},
            'boundary_distance_delta_median_m': b_delta, 'boundary_worse': worse, 'boundary_better': better}


def _full_judge(threshold=.45, tuned=(61, .9012, .236), combo=(61, .8930, .239), ctrl=(62, .9009, .257), base=(59, .9021, .224),
                unbacked_tuned=.10, per_ha_tuned=3.0):
    """A reference_eval summary.json in the round-4 four-arm shape (base + ctrl + combo + tuned, pairs incl. tuned:combo, combo:ctrl)."""
    pairs = {'tuned:base': _pair('tuned', 'base', .0192, [-.0108, .0606], 30, 31),
             'tuned:ctrl': _pair('tuned', 'ctrl', -.0040, [-.0164, .0082], 32, 30),
             'tuned:combo': _pair('tuned', 'combo', .0082, [-.0051, .0213], 33, 28, b_delta=-.003, worse=21, better=29),
             'combo:ctrl': _pair('combo', 'ctrl', -.0144, [-.0464, .0063], 29, 33),
             'ctrl:base': _pair('ctrl', 'base', .0233, [-.0099, .0656], 30, 32)}
    for p in pairs.values():
        p['same_image_only'] = {**p, 'references': 56}
    summary = {'base': _arm(*base), 'ctrl': _arm(*ctrl), 'combo': _arm(*combo), 'tuned': _arm(*tuned), 'pairs': pairs, 'paired': pairs['tuned:base'],
               'same_image_only': {**{k: v['same_image_only'] for k, v in pairs.items()},
                                   **{f'paired_vs_{f}': pairs[f'tuned:{f}']['same_image_only'] for f in ('base', 'ctrl', 'combo')}},
               'negatives': {'base': _neg(123, 54, 918, 2.508), 'ctrl': _neg(600, 420, 1290, 13.34), 'combo': _neg(300, 160, 980, 5.716),
                             'tuned': _neg(250, int(unbacked_tuned * 960), 960, per_ha_tuned), 'note': 'novel = ...'},
               'note': 'agreement with the listed human outlines, not a calibrated error rate'}
    return {'version': 'sam3-mac-reference-eval-v1', 'computed_at': '2026-09-30T00:00:00+00:00', 'device': 'mps', 'dtype': 'fp32', 'prompt': 'building roof',
            'threshold': threshold, 'images': 45, 'arms': {a: {'spec': a} for a in ('base', 'ctrl', 'combo', 'tuned')}, 'model_load_s': {},
            'summary': summary, 'rows': [], 'tiles': []}


def _fold(root, name, per_image=(150, 200), zero_images=0, options=None):
    fold = root / name
    (fold / 'train').mkdir(parents=True)
    images = [{'id': i + 1, 'file_name': f'/x/{i}.png', 'width': 64, 'height': 64} for i in range(len(per_image) + zero_images)]
    anns, k = [], 1
    for img, n in zip(images, per_image, strict=False):
        for _ in range(n):
            anns.append({'id': k, 'image_id': img['id'], 'category_id': 1, 'bbox': [0, 0, 4, 4], 'area': 16, 'iscrowd': 0, 'segmentation': [[0, 0, 4, 0, 4, 4]]})
            k += 1
    json.dump({'images': images, 'annotations': anns, 'categories': [{'id': 1, 'name': 'building roof'}]}, open(fold / 'train' / '_annotations.coco.json', 'w'))
    json.dump({'version': 'sam3-mac-coco-v1', 'sources': ['a'], 'options': options or {},
               'splits': {'train': {'images': len(images), 'annotations': len(anns), 'annotations_per_image_median': 150, 'dropped_or_notes': {}}}},
              open(fold / 'manifest.json', 'w'))
    return fold


def _run_dir(root, run, judge, run_args=None, views=True):
    """Every raw judge block; views=True also writes the v1 / v2 matched views and calibration.json (a judge at 0.45 is its own 0.45
    view), as round4.sh's calibrate / views steps do, so that record can finalise the arm."""
    run_dir = root / run
    for block in ('v1', 'v2', 'v2val'):
        (run_dir / f'reference-eval-{block}').mkdir(parents=True)
        json.dump(judge, open(run_dir / f'reference-eval-{block}' / 'summary.json', 'w'))
    if views:
        for block in ('v1', 'v2'):
            (run_dir / f'reference-eval-{block}-matched').mkdir()
            json.dump(judge, open(run_dir / f'reference-eval-{block}-matched' / 'summary.json', 'w'))
        json.dump({'t_star': .6, 'flags': [], 'arms': {'ctrl': {'t_star': .55}, 'tuned': {'t_star': .6}}}, open(run_dir / 'calibration.json', 'w'))
    if run_args is not None:
        json.dump({'args': run_args}, open(run_dir / 'run.json', 'w'))
    return run_dir


# --- r3_record: --ref / --against / objective -----------------------------------------------------------------------

def test_r3_record_reads_refs_and_verdict_against_combo(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    root = tmp_path / 'round4'
    fold = _fold(root, 'coco-r4-combo-f12-ledger-mixv2-c200-v1',
                 options={'min_area_px': 16, 'min_area_px_for': {'distill-v1-squared': 216}})
    run = 'r4-combo-posw3-seed1'
    _run_dir(root, run, _full_judge(), run_args={'pos_weight': 3.0, 'presence_weight': 20.0, 'o2m_weight': 2.0, 'seed': 1})
    row = rec.build_row(root, run, fold, arm_rule='H6', judge_mode='full', ctrl_run='r3-ctrl-sq-c200-seed1', refs=('combo',), against='combo',
                        extra={'phase': 'phase2', 'arm_flag': '--pos-weight 3'})
    assert row['version'] == 'sam3-mac-round3-record-v2' and row['against'] == 'combo' and row['refs'] == ['combo']
    assert row['combo']['matched_at_iou_0_5'] == 61 and row['paired_vs_combo']['second_minus_first'] == 'tuned - combo'
    assert row['paired']['second_minus_first'] == 'tuned - base' and row['paired_vs_ctrl']['second_minus_first'] == 'tuned - ctrl'
    assert row['combo_vs_ctrl']['second_minus_first'] == 'combo - ctrl'
    assert row['blocks']['v2']['view_045']['paired_vs_combo']['best_iou_delta_mean'] == .0082
    assert row['objective']['pos_weight'] == 3.0 and row['objective']['o2m_weight'] == 2.0 and row['objective']['presence_weight'] == 20.0
    assert row['objective']['source'] == 'run.json args' and row['objective']['recorded_keys'] == ['o2m_weight', 'pos_weight', 'presence_weight']
    assert row['fold']['min_area_px'] == 16 and row['fold']['min_area_px_for'] == {'distill-v1-squared': 216} and row['fold']['images_zero_annotations'] == 0
    v = row['verdict']
    assert v['against'] == 'combo' and v['against_requested'] == 'combo'
    assert v['keeps_matched'] is True, 'tuned 61 >= combo 61 - 1'
    assert v['keeps_boundary'] is True and v['boundary_method'].startswith('paired')
    assert v['agreement'] == 'non_inferior' and v['agreement_ci'] == [-.0051, .0213] and v['agreement_scope'] == 'same_image refs'
    assert v['removes_over_detection'] is True and v['over_detection_inputs']['unbacked_share_base'] == round(54 / 918, 4), 'base-relative'
    vc = row['verdict_vs_ctrl']
    assert vc['against'] == 'ctrl' and vc['removes_over_detection'] is True, 'the over-detection flag is identical in both reads (base-relative)'
    assert vc['keeps_matched'] is True and vc['agreement'] == 'inconclusive' and vc['agreement_ci'] == [-.0164, .0082]
    assert 'accuracy' not in json.dumps(row).lower()
    head = rec.headline(row)
    assert head['against'] == 'combo' and head['paired_vs_combo'] == .0082 and head['matched']['combo'] == 61
    assert head['verdict_vs_ctrl']['agreement'] == 'inconclusive' and head['objective']['pos_weight'] == 3.0
    # the CLI path writes the same keys
    results = root / 'results.jsonl'
    args = ['record', '--root', str(root), '--run', run, '--coco', str(fold), '--judge-mode', 'full', '--ctrl', 'r3-ctrl-sq-c200-seed1',
            '--ref', 'combo', '--against', 'combo', '--extra', '{"phase": "phase2"}']
    assert rec.main(args) == 0
    rows = rec.read_rows(results)
    assert len(rows) == 1 and rows[0]['verdict']['against'] == 'combo' and rows[0]['verdict_vs_ctrl']['against'] == 'ctrl' and 'paired_vs_combo' in rows[0]
    assert rows[0]['code_sha256'].get('round4.sh'), 'round4.sh sha recorded'
    assert rec.main(args) == 0 and len(rec.read_rows(results)) == 1, 'never duplicated'


def test_r3_record_verdict_against_absent_arm_reports_it(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    view = rec.block_from_judge(_full_judge(), refs=('combo',))
    v = rec.verdict(view, against='twin')
    assert v['against'] is None and v['keeps_matched'] is None and any('twin arm absent' in n for n in v['notes'])
    assert v['removes_over_detection'] is True, 'base-relative flag still evaluated'
    v_ctrl = rec.verdict(view)
    assert v_ctrl['against'] == 'ctrl' and v_ctrl['keeps_matched'] is True, 'default read is unchanged from round 3'
    assert rec.block_from_judge(_full_judge(), refs=('base', 'ctrl', 'tuned')).get('paired_vs_tuned') is None, 'fixed arms are not duplicated'


def test_r3_record_objective_defaults_match_finetune_when_flags_absent(tmp_path):
    from scripts.ml.training.sam3_mac import finetune_mps as ft
    from scripts.ml.training.sam3_mac import r3_record as rec
    assert rec.OBJECTIVE_DEFAULTS == ft.LOSS_DEFAULTS == {'presence_weight': 20., 'pos_weight': 10., 'o2m_weight': 2.}
    run = tmp_path / 'r3-ctrl-sq-c200-seed1'
    run.mkdir()
    json.dump({'args': {'seed': 1, 'lr': 8e-5}}, open(run / 'run.json', 'w'))   # a round-3 run.json: no loss flags
    o = rec.objective(tmp_path, 'r3-ctrl-sq-c200-seed1')
    assert (o['presence_weight'], o['pos_weight'], o['o2m_weight']) == (20., 10., 2.) and o['source'].startswith('defaults (run.json lacks')
    o = rec.objective(tmp_path, 'absent-run')
    assert (o['presence_weight'], o['pos_weight'], o['o2m_weight']) == (20., 10., 2.) and o['source'] == 'defaults (run.json absent)'
    json.dump({'args': {'o2m_weight': 0.0}}, open(run / 'run.json', 'w'))
    o = rec.objective(tmp_path, 'r3-ctrl-sq-c200-seed1')
    assert o['o2m_weight'] == 0.0 and o['pos_weight'] == 10. and o['recorded_keys'] == ['o2m_weight'], 'a recorded 0.0 is a value, not an absence'


def test_r3_record_gate_zero_annotation_images(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    fold = _fold(tmp_path, 'coco-zero', per_image=(150, 200), zero_images=1)
    gate = rec.fold_gate(fold, cap=200)
    assert gate['ok'] is True and gate['images_zero_annotations'] == 1 and gate['zero_annotation_gate'] is False, 'round-3 gate unchanged'
    gate = rec.fold_gate(fold, cap=200, require_annotations=True)
    assert gate['ok'] is False and any('zero-annotation gate' in r for r in gate['reasons'])
    assert rec.main(['gate', '--coco', str(fold)]) == 0
    assert rec.main(['gate', '--coco', str(fold), '--no-zero-annotation-images']) == 1
    good = _fold(tmp_path, 'coco-good', per_image=(150, 200))
    assert rec.main(['gate', '--coco', str(good), '--cap', '200', '--no-zero-annotation-images']) == 0


# --- r3_record select-replicate ---------------------------------------------------------------------------------------

def _row(run, rod, km, against='combo', primary='v2'):
    return {'run': run, 'primary_block': primary, 'verdict': {'removes_over_detection': rod, 'keeps_matched': km, 'against': against}}


def test_r3_record_select_replicate_rule(tmp_path, capsys):
    from scripts.ml.training.sam3_mac import r3_record as rec
    cands = ['r4-combo-seed1', 'r4-combo-posw3-seed1', 'r4-combo-o2m0-seed1']
    out = rec.select_replicate([], cands, 'r4-combo-seed1')
    assert out['chosen'] == 'r4-combo-seed1' and 'no complete results row' in out['reason'] and [c['row_present'] for c in out['candidates']] == [False] * 3
    rows = [_row('r4-combo-seed1', False, True, 'ctrl'), _row('r4-combo-posw3-seed1', False, True), _row('r4-combo-o2m0-seed1', None, None)]
    out = rec.select_replicate(rows, cands, 'r4-combo-seed1')
    assert out['chosen'] == 'r4-combo-seed1' and 'no candidate clears' in out['reason']
    rows = [_row('r4-combo-seed1', False, True, 'ctrl'), _row('r4-combo-posw3-seed1', True, None), _row('r4-combo-o2m0-seed1', True, True)]
    out = rec.select_replicate(rows, cands, 'r4-combo-seed1')
    assert out['chosen'] == 'r4-combo-posw3-seed1', 'keeps_matched None is "not false"; the second candidate clears first'
    rows = [_row('r4-combo-seed1', True, False, 'ctrl'), {'run': 'r4-combo-posw3-seed1_error', 'error': 'fold_gate_failed',
                                                            'verdict': {'removes_over_detection': True, 'keeps_matched': True}},
            _row('r4-combo-o2m0-seed1', True, True)]
    out = rec.select_replicate(rows, cands, 'r4-combo-seed1')
    assert out['chosen'] == 'r4-combo-o2m0-seed1', 'keeps_matched false blocks the first; error rows are ignored'
    assert out['candidates'][1]['row_present'] is False
    # a later complete row for the same run wins
    rows = [_row('r4-combo-seed1', True, True, 'ctrl'), _row('r4-combo-seed1', False, True, 'ctrl')]
    assert rec.select_replicate(rows, cands, 'r4-combo-seed1')['candidates'][0]['clears'] is False
    # a verdict read on the dataset-v1 continuity block (or on no block) never clears, whatever its flags say
    rows = [_row('r4-combo-seed1', True, True, 'ctrl', primary='v1'), _row('r4-combo-posw3-seed1', True, True, primary=None), _row('r4-combo-o2m0-seed1', True, True)]
    out = rec.select_replicate(rows, cands, 'r4-combo-seed1')
    assert out['chosen'] == 'r4-combo-o2m0-seed1' and [c['clears'] for c in out['candidates']] == [False, False, True]
    assert [c['primary_block'] for c in out['candidates']] == ['v1', None, 'v2'] and 'primary_block v2' in out['reason']
    rows = [_row('r4-combo-seed1', True, True, 'ctrl', primary='v1')]
    out = rec.select_replicate(rows, cands, 'r4-combo-seed1')
    assert out['chosen'] == 'r4-combo-seed1' and 'dataset-v2 row' in out['reason'] and out['candidates'][0]['clears'] is False
    # CLI: JSON on stdout, exit 0, nothing written
    root = tmp_path / 'round4'
    root.mkdir()
    assert rec.main(['select-replicate', '--root', str(root), '--candidate', 'r4-combo-seed1', '--candidate', 'r4-combo-posw3-seed1', '--default', 'r4-combo-seed1']) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed['chosen'] == 'r4-combo-seed1' and printed['default'] == 'r4-combo-seed1' and not (root / 'results.jsonl').exists()
    assert 'promoted' in printed['note'] and 'accuracy' not in json.dumps(printed).lower()


# --- round3_report renders round-4 rows -----------------------------------------------------------------------------

def test_round3_report_renders_round4_pairing_columns(tmp_path):
    from scripts.ml.training.sam3_mac import r3_record as rec
    from scripts.ml.training.sam3_mac import round3_report as rep
    assert rep.infer_round('data/reviews/eyes/training/sam3-mac/round4/results.jsonl') == '4' and rep.infer_round(str(tmp_path / 'x.jsonl')) == '3'
    root = tmp_path / 'round4'
    fold = _fold(root, 'coco-r4-combo-f12-ledger-mixv2-c200-v1', options={'min_area_px': 16})
    _run_dir(root, 'r4-combo-posw3-seed1', _full_judge(), run_args={'pos_weight': 3.0})
    row = rec.build_row(root, 'r4-combo-posw3-seed1', fold, arm_rule='H6 pos_weight 10 -> 3', judge_mode='full', ctrl_run='r3-ctrl-sq-c200-seed1',
                        refs=('combo',), against='combo', extra={'phase': 'phase2', 'arm_flag': '--pos-weight 3'})
    twin_row = {**row, 'run': 'r4-combo-posw3-seed2', 'seed': 2, 'against': 'twin', 'verdict': {**row['verdict'], 'against': 'twin', 'against_requested': 'twin'},
                'twin': row['combo'], 'paired_vs_twin': row['paired_vs_combo'], 'refs': ['twin', 'combo'], 'arm_flag': '--seed 2',
                'replicate_selection': {'chosen': 'r4-combo-posw3-seed1', 'reason': 'first candidate clearing the density rule'}}
    for b in twin_row['blocks'].values():
        for which in ('view_raw', 'view_045'):
            if b.get(which):
                b[which] = {**b[which], 'twin': row['combo'], 'paired_vs_twin': row['paired_vs_combo']}
    results = root / 'results.jsonl'
    for r in (row, twin_row, rec.error_row(root, 'r4-combo-o2m0-seed1', 'train_gate_failed:first_300_steps')):
        rec.append_row(results, r)
    rows, errors = rep.load(results)
    text = rep.render(rows, errors, str(results), today='2026-09-30')
    assert text.startswith('# SAM3 teacher loop, round 4: results ledger (2026-09-30)')
    assert 'round-5 base fold and the seed-2 replicate; nothing is promoted' in text
    assert 'pairing arm: matched / boundary m / paired best-IoU vs it' in text
    assert '| combo: 61 / 0.239 / +0.0082 [-0.0051, +0.0213] 33/28/1 |' in text
    assert '| twin: 61 / 0.239 / +0.0082 [-0.0051, +0.0213] 33/28/1 |' in text
    assert 'vs ctrl: over-det / matched / boundary / agreement' in text and '| yes / yes / yes / inconclusive |' in text
    assert '20.0 / 3.0 / 2.0' in text, 'objective column presence / pos / o2m'
    assert 'replicate of r4-combo-posw3-seed1' in text and 'flag: --pos-weight 3' in text
    assert 'r4-combo-o2m0-seed1_error' in text and 'train_gate_failed' in text
    assert 'accuracy' not in text.lower() and 'Nothing is promoted' in text
    out = tmp_path / 'REPORT.md'
    assert rep.main(['--results', str(results), '--out', str(out), '--round', '4']) == 0 and out.read_text().startswith('# SAM3 teacher loop, round 4')
    # round-3 rows (no against / objective) still render with the neutral cells
    r3 = {**row, 'run': 'r3-ctrl-sq-c200-seed1', 'against': 'ctrl', 'objective': None, 'verdict': {**row['verdict'], 'against': 'ctrl'}}
    r3.pop('verdict_vs_ctrl')
    t3 = rep.render([r3], [], str(tmp_path / 'round3' / 'results.jsonl'), today='2026-09-30')
    assert t3.startswith('# SAM3 teacher loop, round 3') and '| ctrl |' in t3 and 'same (judged against ctrl)' in t3 and 'round-4 base fold' in t3


# --- RULES.json (pre-registered, written before run 1's fold gate) --------------------------------------------------

def test_round4_rules_json_is_pre_registered():
    from scripts.ml.training.sam3_mac import r3_record as rec
    path = R4 / 'RULES.json'
    if not path.exists():
        pytest.skip('round-4 RULES.json absent (data/ not checked out)')
    rules = json.load(open(path))
    r3 = json.load(open(R4.parent / 'round3' / 'RULES.json'))
    assert rules['version'] == 'sam3-round4-rules-v1' and rules['amendments'] == [] and rules['copied_from']['version'] == r3['version']
    for k, v in rec.RULES_DEFAULT.items():
        assert rules['decision_rules'][k] == v == r3['decision_rules'][k], k
    assert rules['decision_rules']['composite'].startswith('selects only the round-5 base fold')
    assert 'r4-combo-seed1' in rules['decision_rules']['read_on'] and 'r3-ctrl-sq-c200-seed1' in rules['decision_rules']['read_on']
    adj = rules['calibration']['val_adjacent_to_train']
    for run in ('r4-combo-seed1', 'r4-combo-posw3-seed1', 'r4-combo-o2m0-seed1', 'r4-combo-seed2', 'r4-combo-posw3-seed2', 'r4-combo-o2m0-seed2'):
        assert adj[run]['exclude_scene_prefix'] == 'mooroolbark-' and '107 of the 173' in adj[run]['reason']
    assert rules['calibration']['rule'] == r3['calibration']['rule'] and rules['calibration']['grid'] == r3['calibration']['grid']
    assert rules['replicate_selection']['default'] == 'r4-combo-seed1' and 'removes_over_detection == true' in rules['replicate_selection']['rule']
    od = rules['objective_defaults']
    assert (od['loss_ce'], od['presence_loss'], od['pos_weight'], od['o2m_weight']) == (20, 20, 10, 2) and od['o2m_matcher'] == {'alpha': .3, 'threshold': .4, 'topk': 4}
    assert 'presence target is 1 on every training image' in od['note']
    assert rules['transfer_caveat'] == r3['transfer_caveat'] and rules['wording'] == r3['wording']
    assert 'accuracy' not in json.dumps({k: v for k, v in rules.items() if k != 'wording'}).lower(), 'only the wording rule names the banned word'


# --- round4.sh ------------------------------------------------------------------------------------------------------

def _dry(script, phase, root):
    env = {**os.environ, 'DRY_RUN': '1', 'ROUND4_ROOT': str(root)}
    return subprocess.run(['sh', str(script), phase], capture_output=True, text=True, env=env, cwd=REPO)


def test_round4_sh_syntax_and_dry_run(tmp_path):
    script = REPO / 'scripts/ml/training/sam3_mac/round4.sh'
    assert subprocess.run(['sh', '-n', str(script)], capture_output=True).returncode == 0
    if not (REPO / 'scripts/ml/training/.venv-sam3/bin/python').exists() or not (REPO / 'scripts/ml/.venv/bin/python').exists():
        pytest.skip('venvs absent: dry run needs both interpreters')
    root = tmp_path / 'round4'
    bad = subprocess.run(['sh', str(script), 'bogus'], capture_output=True, text=True, env={**os.environ, 'DRY_RUN': '1', 'ROUND4_ROOT': str(root)}, cwd=REPO)
    assert bad.returncode == 2 and 'usage' in bad.stderr
    p1 = _dry(script, 'phase1', root)
    assert p1.returncode == 0, p1.stderr
    t = p1.stdout
    assert 'DRY RUN' in t and '(no run started)' in t and 'arm r4-combo-seed1' in t
    assert 'finetune_mps' in t and '--dtype bf16' in t and '--lr 8e-5' in t and '--seed 1' in t and '--save-every 1500' in t and '--warmup 50' in t and '--steps 3000' in t
    assert 'PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.7' in t and 'PYTORCH_MPS_LOW_WATERMARK_RATIO=0.5' in t and 'nohup' in t
    assert '--arm ctrl=data/reviews/eyes/training/sam3-mac/round3/r3-ctrl-sq-c200-seed1/trainable-final.pt' in t
    assert '--arm mixv2=data/reviews/eyes/training/sam3-mac/round3/r3-mixv2-sq-c200-seed1/trainable-final.pt' in t
    assert '--arm floor12=data/reviews/eyes/training/sam3-mac/round3/r3-floor12-sq-ledger-c200-seed1/trainable-final.pt' in t
    assert '--pair tuned:base --pair tuned:ctrl --pair tuned:mixv2 --pair tuned:floor12 --pair ctrl:base' in t and '--pair combo:ctrl' not in t
    assert '--threshold 0.2' in t and '--dataset data/reviews/eyes/training/dataset-v2 --split test' in t and '--split val' in t
    assert 'r3_calibrate' in t and f'--rule {root}/RULES.json' in t and 'r3_postfilter' in t and '--threshold 0.45' in t
    assert 'r3_record record' in t and '--against ctrl' in t and '--ref mixv2 --ref floor12' in t and '--ctrl r3-ctrl-sq-c200-seed1' in t
    assert '--no-zero-annotation-images' in t and '"floor_scope": "teacher sets only (--min-area-px-for)"' in t
    assert '--pos-weight' not in t.split('would run:')[1].split('\n')[0], 'run 1 is the round-3 line'
    assert 'fold missing' in p1.stderr and 'RULES.json absent' in p1.stderr, 'the dry run reports absent inputs without writing an error row'
    p2 = _dry(script, 'phase2', root)
    assert p2.returncode == 0, p2.stderr
    t = p2.stdout
    assert 'would wait until results rows exist for: r4-combo-seed1' in t
    assert 'arm r4-combo-posw3-seed1' in t and 'arm r4-combo-o2m0-seed1' in t and 'arm r4-combo-o2m1-seed1' not in t
    assert '--pos-weight 3' in t and '--o2m-weight 0' in t and '--against combo' in t and '--ref combo' in t
    assert f'--arm combo={root}/r4-combo-seed1/trainable-final.pt' in t and '--pair tuned:combo --pair ctrl:base --pair combo:ctrl' in t
    assert 'training gate: the first 300 steps' in t and 'r4-combo-o2m1-seed1' in t and "'--o2m-weight 1'" in t
    posw3_line = [line for line in t.splitlines() if 'would run' in line and 'r4-combo-posw3-seed1' in line][0]
    assert posw3_line.rstrip().endswith('--pos-weight 3 >> ' + f'{root}/r4-combo-posw3-seed1.train.log 2>&1 &') and '--seed 1' in posw3_line
    o2m_line = [line for line in t.splitlines() if 'would run' in line and 'r4-combo-o2m0-seed1' in line][0]
    assert '--o2m-weight 0' in o2m_line and '--pos-weight' not in o2m_line
    p3 = _dry(script, 'phase3', root)
    assert p3.returncode == 0, p3.stderr
    t = p3.stdout
    assert 'would wait until results rows exist for: r4-combo-seed1 r4-combo-posw3-seed1 r4-combo-o2m0-seed1' in t
    assert 'replicate selection: {' in t and '"chosen": "r4-combo-seed1"' in t and 'no complete results row' in t, 'default choice with no ledger'
    assert 'arm r4-combo-seed2' in t and '--seed 2' in t and '--against twin' in t and '--ref twin' in t and '--ref combo' not in t
    assert f'--arm twin={root}/r4-combo-seed1/trainable-final.pt' in t and '--pair tuned:twin --pair ctrl:base' in t and '--pair combo:ctrl' not in t
    assert '"arm_flag": "--seed 2"' in t and '"replicate_selection": {' in t
    assert 'f16 decision' in t and 'queued for round 5' in t
    for name in ('round4.lock', 'results.jsonl', 'round4.log'):
        assert not (root / name).exists(), f'a dry run never writes {name}'
    assert not list(root.glob('*.train.*')) and not list(root.glob('r4-*'))
