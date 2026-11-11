"""Judged rows -> the per-mode, per-domain figures, the base-harness comparison and the spend.

Ungraded rows are counted and excluded from every mean. Against the machine start outline, an
outline case is improved, neutral or regressed by the bands in judge.outcome; against the base
harness, paired per-case deltas carry a bootstrap 95% interval (training.metrics.paired_delta).
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from ..training.metrics import paired_delta
from .schedule import MODE_NAMES, Namespace

OUTLINE_KEYS = ('judge_iou', 'judge_boundary_f', 'judge_hausdorff_m', 'start_iou', 'start_boundary_f',
                'delta_iou', 'delta_boundary_f', 'delta_hausdorff_m')
DETECT_KEYS = ('judge_decision_accuracy', 'keep_all_accuracy', 'delta_accuracy', 'reject_precision',
               'reject_recall', 'addition_recall')
PAIRED = {'judge_iou': 'iou', 'judge_boundary_f': 'boundary_f', 'judge_hausdorff_m': 'hausdorff_m',
          'judge_decision_accuracy': 'iou', 'judge_agrees': 'iou'}


def _mean(values):
    values = [v for v in values if isinstance(v, int | float)]
    return round(statistics.fmean(values), 4) if values else None


def _median(values):
    values = [v for v in values if isinstance(v, int | float)]
    return round(statistics.median(values), 4) if values else None


def summarise(rows):
    """One block per metric family: n, means, outcome counts and kept-start counts."""
    graded = [r for r in rows if r.get('graded')]
    out = {'n': len(graded), 'n_ungraded': len(rows) - len(graded)}
    outline = [r for r in graded if r.get('metric') == 'outline']
    if outline:
        out['outline'] = {'n': len(outline), **{f'mean_{k}': _mean(r.get(k) for r in outline) for k in OUTLINE_KEYS},
                          'median_judge_hausdorff_m': _median(r.get('judge_hausdorff_m') for r in outline),
                          'outcomes': dict(Counter(r['outcome'] for r in outline)),
                          'kept_start': sum(1 for r in outline if r.get('kept_start')),
                          'by_change': {change: {'n': len(group), 'mean_delta_iou': _mean(r.get('delta_iou') for r in group),
                                                 'mean_delta_boundary_f': _mean(r.get('delta_boundary_f') for r in group),
                                                 'outcomes': dict(Counter(r['outcome'] for r in group))}
                                        for change, group in _group(outline, 'change').items()},
                          'by_reference': {origin: {'n': len(group), 'mean_judge_iou': _mean(r.get('judge_iou') for r in group),
                                                    'mean_delta_iou': _mean(r.get('delta_iou') for r in group),
                                                    'mean_delta_boundary_f': _mean(r.get('delta_boundary_f') for r in group)}
                                           for origin, group in _group(outline, 'reference_origin').items()}}
    review = [r for r in graded if r.get('metric') == 'review']
    if review:
        codex = [r for r in review if r.get('codex_agrees') is not None]
        out['review'] = {'n': len(review), 'agreement': _mean(r['judge_agrees'] for r in review),
                         'coverage': _mean(r['judge_decided'] for r in review),
                         'agreement_when_decided': _mean(r.get('judge_agrees_decided') for r in review),
                         'outcomes': dict(Counter(r['outcome'] for r in review)),
                         'codex_n': len(codex), 'codex_agreement': _mean(r['codex_agrees'] for r in codex),
                         'codex_coverage': _mean(float(r['codex_decision'] != 'uncertain') for r in codex),
                         'agreement_on_codex_items': _mean(r['judge_agrees'] for r in codex),
                         'by_human_decision': {k: dict(Counter(r['agent_decision'] for r in group))
                                               for k, group in _group(review, 'human_decision').items()}}
    detect = [r for r in graded if r.get('metric') == 'detect']
    if detect:
        out['detect'] = {'n': len(detect), **{f'mean_{k}': _mean(r.get(k) for r in detect) for k in DETECT_KEYS},
                         'outcomes': dict(Counter(r['outcome'] for r in detect))}
    return out


def _group(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row.get(key)].append(row)
    return dict(sorted(groups.items(), key=lambda item: str(item[0])))


def compare_bases(rows, base_rows):
    """Paired adopted-minus-base deltas per case, against the first graded base rollout of each case."""
    first = {}
    for row in sorted(base_rows, key=lambda r: r.get('label', '')):
        if row.get('graded'):
            first.setdefault(row['task_id'], row)
    pairs = [(r, first[r['task_id']]) for r in rows if r.get('graded') and r['task_id'] in first]
    out = {}
    for key, alias in PAIRED.items():
        usable = [(a, b) for a, b in pairs if a.get(key) is not None and b.get(key) is not None]
        if usable:
            out[key] = paired_delta([{alias: a[key]} for a, _ in usable], [{alias: b[key]} for _, b in usable], key=alias)
    return out


def spend(runs_root, ns):
    """Rollout spend from meta.json files and improver spend from ledgers and selections."""
    root = Path(runs_root)/str(ns)
    rollouts = [json.loads(p.read_text()).get('cost_usd') or 0.0 for p in root.glob('*/rollouts/*/meta.json')]
    sessions = 0.0
    agg = Path(runs_root)/str(Namespace(ns.adapter, ns.executor, 'agg', ns.k, ns.seed, ns.tag))
    for path in agg.glob('*/summary.json'):
        summary = json.loads(path.read_text())
        mode = (summary.get('modes') or {}).get(ns.mode) or {}
        sessions += ((mode.get('ledger') or {}).get('usd') or 0.0) + ((summary.get('selections') or {})
                                                                      .get(ns.mode) or {}).get('usd', 0.0)
    return {'rollout_usd': round(sum(rollouts), 4), 'n_rollouts': len(rollouts), 'improver_usd': round(sessions, 4)}


def build(runs_root, ns):
    root = Path(runs_root)/str(ns)
    rows = json.loads((root/'judged.json').read_text()) if (root/'judged.json').is_file() else []
    base_rows = json.loads((root/'base_judged.json').read_text()) if (root/'base_judged.json').is_file() else []
    report = {'namespace': str(ns), 'mode': MODE_NAMES[ns.mode], 'k': ns.k, 'adopted': summarise(rows),
              'by_domain': {domain: summarise(group) for domain, group in _group(rows, 'domain').items()},
              'spend': spend(runs_root, ns)}
    if base_rows:
        report['base'] = summarise(base_rows)
        report['adopted_vs_base'] = compare_bases(rows, base_rows)
    return report


def render(report):
    lines = [f'{report["namespace"]}  ({report["mode"]} k={report["k"]})', '=' * 60]

    def block(title, summary):
        lines.append(f'{title}: n={summary["n"]}' + (f' (ungraded {summary["n_ungraded"]})' if summary['n_ungraded'] else ''))
        if 'outline' in summary:
            o = summary['outline']
            lines.append(f'  outline  IoU {o["mean_judge_iou"]} (start {o["mean_start_iou"]}, Δ {o["mean_delta_iou"]})'
                         f' | boundary F@0.5m {o["mean_judge_boundary_f"]} (Δ {o["mean_delta_boundary_f"]})'
                         f' | median Hausdorff {o["median_judge_hausdorff_m"]} m')
            lines.append(f'           outcomes {o["outcomes"]} | kept start {o["kept_start"]}/{o["n"]}')
            for change, group in o['by_change'].items():
                lines.append(f'           {change}: n={group["n"]} ΔIoU {group["mean_delta_iou"]} '
                             f'ΔBF {group["mean_delta_boundary_f"]} {group["outcomes"]}')
            for origin, group in o.get('by_reference', {}).items():
                lines.append(f'           reference {origin}: n={group["n"]} IoU {group["mean_judge_iou"]} '
                             f'ΔIoU {group["mean_delta_iou"]} ΔBF {group["mean_delta_boundary_f"]}')
        if 'review' in summary:
            v = summary['review']
            lines.append(f'  review   agreement {v["agreement"]} (coverage {v["coverage"]}, when decided '
                         f'{v["agreement_when_decided"]}) | {v["outcomes"]}')
            if v['codex_n']:
                lines.append(f'           on the {v["codex_n"]} items Codex reviewed: harness {v["agreement_on_codex_items"]} '
                             f'vs Codex {v["codex_agreement"]} (Codex decided {v["codex_coverage"]}, the rest uncertain)')
            lines.append(f'           agent decisions by human decision: {v["by_human_decision"]}')
        if 'detect' in summary:
            d = summary['detect']
            lines.append(f'  detect   accuracy {d["mean_judge_decision_accuracy"]} (keep-all '
                         f'{d["mean_keep_all_accuracy"]}, Δ {d["mean_delta_accuracy"]}) | reject P/R '
                         f'{d["mean_reject_precision"]}/{d["mean_reject_recall"]} | {d["outcomes"]}')

    block('adopted harness', report['adopted'])
    for domain, summary in report['by_domain'].items():
        block(f'  {domain}', summary)
    if 'base' in report:
        block('base harness (every base rollout)', report['base'])
        for key, delta in report['adopted_vs_base'].items():
            lines.append(f'  adopted - base {key}: median Δ {delta["median_delta"]} CI95 {delta["ci95"]} '
                         f'(better {delta.get("n_improved")}, worse {delta.get("n_worse")}, n={delta["n"]})')
    s = report['spend']
    lines.append(f'spend: rollouts ${s["rollout_usd"]} over {s["n_rollouts"]} | improver ${s["improver_usd"]}')
    return '\n'.join(lines)
