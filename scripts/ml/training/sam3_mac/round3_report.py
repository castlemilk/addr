"""Render a teacher-loop results ledger as a measurement-first markdown report (sam3-mac-round3-report-v2).

Reads data/reviews/eyes/training/sam3-mac/round3/results.jsonl (rows written by r3_record.py), dedupes by run (the
last row wins), lists *_error rows apart, and writes REPORT.md beside the ledger (or --out). Numbers are agreement
between model estimates and human boundary references, or agreement between two models (footprints); no column is a
calibrated error rate. Nothing in the report promotes anything.

Round 4 (same renderer): the round label is read from a roundN folder in --results (or --round); rows judged against
another arm (r3_record --against combo / twin) get a 'pairing arm' column (its matched count, boundary and the paired
best-IoU vs it) and a 'vs ctrl' column from verdict_vs_ctrl; the objective (presence / pos / o2m weights) is listed per arm.

  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.round3_report
  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.round3_report --out docs/sam3-round3-report-2026-09-30.md
  scripts/ml/.venv/bin/python -m scripts.ml.training.sam3_mac.round3_report --results data/reviews/eyes/training/sam3-mac/round4/results.jsonl \
      --out data/reviews/eyes/training/sam3-mac/round4/REPORT.md
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, date
from pathlib import Path

from . import r3_record as rec

VERSION = 'sam3-mac-round3-report-v2'
DEFAULT_ROUND = '3'
REPO = rec.REPO
ROOT = REPO / 'data/reviews/eyes/training/sam3-mac/round3'
BLOCK_TITLES = (('v2', 'PRIMARY: dataset-v2 test (62 placements, 45 tiles)'),
                ('v1', 'CONTINUITY: dataset-v1 test (63 references, 44 tiles)'))
ASYMMETRY = ('v1-only references: house-25822 (three refs of 28.6 / 40.4 / 18.8 m2, losers in every stage-2 arm); v2-only: '
             '887c2d078f693396 (1,456 m2, T1) and a seventh cross-image placement of 5021840e3b49d07d. dataset-v2 alone reads '
             'kinder to tuned arms for a non-model reason; never pool v1 and v2 in one paired interval.')
TRANSFER = ('The judge threshold is the fork\'s presence-weighted score; production ONNX cuts the sigmoid at 0.45. No value '
            'transfers 1:1 and no production claim follows. t* is a fairness device for comparing arms at matched density: in the '
            'matched view base is cut at its 0.45 reference cut and each other arm at its own val t* (per-arm cuts shown in the '
            'threshold column); a matched view showing one cut for all arms was built before per-arm cuts existed and compares '
            'the arms at tuned\'s t*, not at matched density.')


def _get(d, *keys, default=None):
    for key in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
    return default if d is None else d


def fmt(value, nd=3, signed=False):
    if value is None:
        return 'n/a'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, int):
        return f'{value:+d}' if signed else f'{value:,d}'
    if isinstance(value, float):
        return f'{value:+.{nd}f}' if signed else f'{value:.{nd}f}'
    return str(value)


def fmt_hours(seconds):
    return 'n/a' if seconds is None else f'{seconds / 3600:.2f} h'


def fmt_pair(pair):
    """'+0.027 [-0.003, +0.049] 32/31/0' (delta, image-bootstrap interval, higher/lower/equal)."""
    if not isinstance(pair, dict):
        return 'n/a'
    delta = pair.get('best_iou_delta_mean')
    ci = rec._ci(pair)
    counts = pair.get('per_reference') or {k: pair.get(k) for k in ('higher', 'lower', 'equal') if pair.get(k) is not None}
    text = fmt(delta, 4, signed=True)
    if ci:
        text += f' [{fmt(ci[0], 4, signed=True)}, {fmt(ci[1], 4, signed=True)}]'
    if counts:
        text += f" {counts.get('higher', '?')}/{counts.get('lower', '?')}/{counts.get('equal', '?')}"
    return text


def fmt_share(neg, arm):
    v = _get(neg, arm, 'novel_no_footprint_share_of_predictions')
    return 'n/a' if v is None else f'{100 * v:.1f}%'


def infer_round(source):
    """'4' for .../round4/results.jsonl (an exact roundN path component, nearest the file wins), else DEFAULT_ROUND."""
    for part in reversed(Path(str(source)).parts):
        m = re.fullmatch(r'round(\d+)', part)
        if m:
            return m.group(1)
    return DEFAULT_ROUND


def pairing_arm(row):
    """The arm the row's verdict was read against when it is not ctrl (round 4: combo / twin), else None."""
    against = row.get('against') or _get(row, 'verdict', 'against_requested') or _get(row, 'verdict', 'against')
    return against if against and against not in ('ctrl', 'base') else None


def fmt_objective(row):
    o = row.get('objective')
    if not isinstance(o, dict):
        return 'n/a'
    return ' / '.join(fmt(o.get(k), 1) for k in ('presence_weight', 'pos_weight', 'o2m_weight'))


def fmt_pairing_cell(row, view):
    arm = pairing_arm(row)
    if arm is None:
        return 'ctrl'
    a = view.get(arm) or {}
    return (f"{arm}: {fmt(a.get('matched_at_iou_0_5'))} / {fmt(a.get('boundary_distance_m_median_matched'))} / "
            f"{fmt_pair(view.get(f'paired_vs_{arm}'))}")


def fmt_vs_ctrl(row):
    vc = row.get('verdict_vs_ctrl')
    if not isinstance(vc, dict):
        return 'same (judged against ctrl)' if pairing_arm(row) is None else 'n/a'
    return f"{fmt(vc.get('removes_over_detection'))} / {fmt(vc.get('keeps_matched'))} / {fmt(vc.get('keeps_boundary'))} / {vc.get('agreement') or 'n/a'}"


def load(path):
    """(complete rows deduped by run in first-seen order, last row wins; error rows in file order)."""
    rows = rec.read_rows(path)
    complete, errors, order = {}, [], []
    for row in rows:
        if rec.is_error_row(row):
            errors.append(row)
            continue
        run = row.get('run')
        if run not in complete:
            order.append(run)
        complete[run] = row
    return [complete[r] for r in order], errors


def _view(row, block, which='view_045'):
    return _get(row, 'blocks', block, which) or {}


def _status(row, block):
    b = _get(row, 'blocks', block) or {}
    if b.get('status'):
        return f"{b['status']}: {b.get('reason', '')}".strip()
    return None


def arms_table(rows):
    head = ['run', 'arm rule', 'fold images / anns', 'over-cap dropped', 'flipped to background', 'images > 200',
            'loss median (last 10)', 'skipped', 's/step', 'train wall', 'loader errors', 'resumed', 'seed',
            'objective presence / pos / o2m']
    lines = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    for r in rows:
        fold, train = r.get('fold') or {}, r.get('train') or {}
        lines.append('| ' + ' | '.join([
            f"`{r.get('run')}`", str(r.get('arm_rule') or ''),
            f"{fmt(fold.get('images'))} / {fmt(fold.get('annotations'))}", fmt(fold.get('ann_over_cap')),
            fmt(fold.get('flipped_to_background')), fmt(fold.get('images_over_200')),
            fmt(train.get('loss_median_last_10'), 1), fmt(train.get('skipped_batches')), fmt(train.get('seconds_per_step_median'), 3),
            fmt_hours(train.get('wall_seconds')), fmt(r.get('loader_errors')),
            'yes' if train.get('resumed_from') else 'no', fmt(r.get('seed')), fmt_objective(r)]) + ' |')
    return lines


def _threshold_text(view, which):
    cuts = view.get('arm_thresholds')
    if cuts:
        return 'per arm: ' + ', '.join(f'{arm} {fmt(t, 2)}' for arm, t in sorted(cuts.items()))
    if which == 'view_matched':
        return f"{fmt(view.get('threshold'), 2)} (all arms at tuned's t*)"
    return fmt(view.get('threshold'), 2)


def block_table(rows, block, which):
    head = ['run', 'matched tuned / ctrl / base (of N)', 'best-IoU median tuned / base', 'boundary m tuned / ctrl / base',
            'paired best-IoU vs base [95% CI] h/l/e', 'paired best-IoU vs ctrl [95% CI] h/l/e',
            'pairing arm: matched / boundary m / paired best-IoU vs it [95% CI] h/l/e', 'unbacked novel share base -> tuned',
            'novel per ha base -> tuned', 'predictions per tile base -> tuned', 'threshold']
    lines = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    any_row = False
    for r in rows:
        view = _view(r, block, which)
        if not view:
            status = _status(r, block) or f'{which} absent'
            lines.append(f"| `{r.get('run')}` | {status} |" + ' |' * (len(head) - 2))
            continue
        any_row = True
        base, ctrl, tuned, neg = view.get('base') or {}, view.get('ctrl') or {}, view.get('tuned') or {}, view.get('negatives') or {}
        n = tuned.get('references') or base.get('references')
        lines.append('| ' + ' | '.join([
            f"`{r.get('run')}`",
            f"{fmt(tuned.get('matched_at_iou_0_5'))} / {fmt(ctrl.get('matched_at_iou_0_5'))} / {fmt(base.get('matched_at_iou_0_5'))} (of {fmt(n)})",
            f"{fmt(tuned.get('best_iou_median'), 4)} / {fmt(base.get('best_iou_median'), 4)}",
            f"{fmt(tuned.get('boundary_distance_m_median_matched'))} / {fmt(ctrl.get('boundary_distance_m_median_matched'))} / {fmt(base.get('boundary_distance_m_median_matched'))}",
            fmt_pair(view.get('paired_vs_base')), fmt_pair(view.get('paired_vs_ctrl')), fmt_pairing_cell(r, view),
            f"{fmt_share(neg, 'base')} -> {fmt_share(neg, 'tuned')}",
            f"{fmt(_get(neg, 'base', 'novel_per_ha'), 2)} -> {fmt(_get(neg, 'tuned', 'novel_per_ha'), 2)}",
            f"{fmt(_get(neg, 'base', 'predictions_per_tile_median'), 1)} -> {fmt(_get(neg, 'tuned', 'predictions_per_tile_median'), 1)}",
            _threshold_text(view, which)]) + ' |')
    return lines, any_row


def verdict_table(rows):
    head = ['run', 't*', 'removes over-detection (base-relative)', 'keeps matched', 'keeps boundary', 'agreement vs pairing arm',
            'judged against', 'vs ctrl: over-det / matched / boundary / agreement', 'notes']
    lines = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    for r in rows:
        v = r.get('verdict') or {}
        notes = list(v.get('notes') or [])
        if v.get('boundary_method'):
            notes.append(f"boundary: {v['boundary_method']}")
        if v.get('agreement_scope'):
            notes.append(f"agreement: {v['agreement_scope']}")
        if isinstance(r.get('replicate_selection'), dict):
            notes.append(f"replicate of {r['replicate_selection'].get('chosen')} ({r['replicate_selection'].get('reason')})")
        if r.get('arm_flag'):
            notes.append(f"flag: {r['arm_flag']}")
        cal = r.get('calibration') or {}
        t_star = cal.get('t_star')
        flags = cal.get('flags') or []
        t_text = fmt(t_star, 2) + (f" ({', '.join(flags)})" if flags else '') if t_star is not None else str(cal.get('status') or 'n/a')
        lines.append('| ' + ' | '.join([f"`{r.get('run')}`", t_text, fmt(v.get('removes_over_detection')), fmt(v.get('keeps_matched')),
                                        fmt(v.get('keeps_boundary')), str(v.get('agreement') or 'n/a'), str(v.get('against') or 'n/a'),
                                        fmt_vs_ctrl(r), '; '.join(notes) or '']) + ' |')
    return lines


def provenance(rows):
    lines = []
    seen = set()
    for r in rows:
        key = (r.get('judge_version'), r.get('judge_version_sha'), r.get('rules_sha'), r.get('judge_mode'))
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- runs with judge `{r.get('judge_version') or 'n/a'}` (reference_eval.py sha256 `{(r.get('judge_version_sha') or 'n/a')[:16]}`), "
                     f"judge mode `{r.get('judge_mode') or 'n/a'}`, rules sha `{(r.get('rules_sha') or 'absent')[:16]}` ({r.get('rules_source') or 'n/a'})")
    for r in rows:
        shas = r.get('code_sha256') or {}
        fps = r.get('footprint_cache_shas') or {}
        lines.append(f"- `{r.get('run')}`: fold manifest sha `{(r.get('coco_manifest_sha') or 'n/a')[:16]}`; code "
                     + ', '.join(f'{k} `{v[:12]}`' for k, v in sorted(shas.items())) + '; footprint caches '
                     + (', '.join(f'{Path(k).name} `{v[:12]}`' for k, v in sorted(fps.items())) or 'none'))
    return lines


def render(rows, errors, source, today=None, round_label=None):
    today = today or date.today().isoformat()
    round_label = str(round_label or infer_round(source))
    try:
        next_round = str(int(round_label) + 1)
    except ValueError:
        next_round = 'next'
    out = [f'# SAM3 teacher loop, round {round_label}: results ledger ({today})', '',
           f'Source: `{source}` ({len(rows)} complete arm rows, {len(errors)} error rows). Rendered by `{VERSION}`.', '',
           'Every number is agreement between model estimates and the human boundary references of the named dataset '
           'split, or agreement between two models where footprints are involved (Microsoft footprints are ML outputs). '
           'Human rejections are never counted as roof false positives. Paired deltas are tuned minus base or tuned minus '
           'ctrl (or tuned minus the arm\'s pairing arm: combo for the round-4 objective arms, the seed-1 twin for the '
           'replicate) with the image-bootstrap 95% interval and higher/lower/equal reference counts; read the interval and the '
           'sign counts, never a single delta (one seed per arm). Nothing is promoted by this report; production '
           'latest.json / review.json / confidence are never written.', '']
    if not rows and not errors:
        out += ['No rows yet: the waiter has not recorded an arm.', '']
        return '\n'.join(out)
    out += ['## Arms, folds and training diagnostics', '', 'Losses are training diagnostics. "Flipped to background" counts '
            'every annotation the fold build dropped (floor, cap, weight, policy): each is a background label for SAM3, including real roofs.', '']
    out += arms_table(rows) + ['']
    for block, title in BLOCK_TITLES:
        for which, label in (('view_045', 'threshold 0.45 view'), ('view_matched', 'matched-density view at t*')):
            lines, any_row = block_table(rows, block, which)
            if not any_row and which == 'view_matched':
                continue
            out += [f'## {title}, {label}', ''] + lines + ['']
    out += ['Asymmetry note: ' + ASYMMETRY, '', 'Threshold note: ' + TRANSFER, '']
    out += [f'## Pre-registered rules per arm (read on the primary 0.45 view; decision_rules of the round-{round_label} RULES.json)', '',
            'Flags are decision aids against the arm\'s pairing arm (the "judged against" column: ctrl in round 3; combo for the '
            'round-4 objective arms and the seed-1 twin for the replicate, with the ctrl read kept in the "vs ctrl" column); '
            'removes over-detection is base-relative in every row; a flag of n/a means the judge output lacked the input. '
            f'The composite selects only the round-{next_round} base fold and the seed-2 replicate; nothing is promoted on any outcome.', '']
    out += verdict_table(rows) + ['']
    if errors:
        out += ['## Error rows (skipped by consumers; the arm is retried on the next waiter run)', '']
        out += [f"- `{e.get('run')}`: {e.get('error')} (recorded {e.get('recorded_at', 'n/a')})" for e in errors] + ['']
    out += ['## Provenance', ''] + provenance(rows) + ['']
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--results', default=str(ROOT / 'results.jsonl'))
    parser.add_argument('--out', default=None, help='markdown path (default: REPORT.md beside the ledger)')
    parser.add_argument('--round', default=None, help='round label for the title (default: the roundN folder in --results, else 3)')
    args = parser.parse_args(argv)
    results = Path(args.results)
    rows, errors = load(results)
    from datetime import datetime
    text = render(rows, errors, str(results), datetime.now(UTC).date().isoformat(), args.round)
    out = Path(args.out) if args.out else results.parent / 'REPORT.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f'{out}: {len(rows)} arm rows, {len(errors)} error rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
