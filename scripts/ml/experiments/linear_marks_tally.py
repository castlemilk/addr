"""Marks tally for the linear-continuity experiment: the reviewer's 2026-10-03 19:40 "yes to defaults" on the marks card against
run-v2/MARKS-2026-10-03.md, line by line, with each line's outcome in a later run (default run-v3, the 19:40 defaults).

Writes <marks-dir>/MARKS-TALLY-<date>.json (a new file beside the marked sheet; nothing in run-v2 is rewritten) and prints a
short table. Outcomes per line: 'present' (a feature with the same id - the same member set - is in the later run),
'refused:<reason>' (the later run refused the member set; the reason of the refusal that covers the line's members),
'changed:<feature id>' (the members ended in a different feature), 'absent' (no feature and no refusal names the members).

    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_marks_tally
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_marks_tally --run run-v3 --write
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXP = ROOT/'data/reviews/eyes/experiments/linear-continuity-2026-10-03'
MARKS_DATE = '2026-10-03'
LAYER = 'review'
# The card (docs/handover-2026-10-01.md section 17 "Card for the reviewer", item 1) and the reviewer's answer, verbatim.
AUTHORISATION = {
    'time_local': '2026-10-03 19:40 AEST', 'reviewer_said': 'yes to defaults',
    'card': 'docs/handover-2026-10-01.md section 17 "Card for the reviewer" (defaults in bold)',
    'adopted_reading': 'Default reading from the skeptic: right 3, 4, 7; wrong 2, 5, 6, 8; unclear 1.',
    'sheet': 'data/reviews/eyes/experiments/linear-continuity-2026-10-03/run-v2/MARKS-2026-10-03.md'}
MARKS = {1: 'unclear', 2: 'wrong', 3: 'right', 4: 'right', 5: 'wrong', 6: 'wrong', 7: 'right', 8: 'wrong'}
# The card's defaults the reviewer accepted with the marks (items 2-6), the design values each one set.
DEFAULTS_ADOPTED = {
    '2_result_floor': 'a merged feature must reach 1 m2 AND its largest member must reach 1 m2, else refuse (result_floor_m2 1.0); '
                      'the single-piece 1 m2 metric floor stays deferred',
    '3_material_edge_adjacency': 'line 6 accepted as "apron of the lane"; no surface test',
    '4_blockers': 'blocker_kinds gain water and building',
    '5_vertex_cap': 'merged rings over 150 vertices are simplified at 2 px as the extraction path does; still over 200 is reported',
    '6_promotion': 'dated RULES / DESIGN notes naming d, the guards, the floor and the blockers; the join enabled for WINDOW '
                   're-extracts only; the 99 review-set scenes untouched; then the 53 windows re-extracted with lineage'}
LINE = re.compile(r'^(\d+)\. (\w+) ([0-9a-f]{16}) = ([0-9a-f, ]+?) · gap ([\d.]+) m · bridge ([\d.]+) m2 · step ([\d+]+) ·')


def parse_marks(path):
    """Numbered lines of a marks sheet -> {n: {kind, feature_id, members, gap_m, bridge_m2, steps, scene}}."""
    lines, scene = {}, None
    for raw in path.read_text().splitlines():
        if raw.startswith('## '):
            scene = raw[3:].split(' — ')[0].strip()
        m = LINE.match(raw)
        if m:
            n = int(m.group(1))
            lines[n] = {'scene': scene, 'kind': m.group(2), 'feature_id': m.group(3), 'members': m.group(4).split(', '),
                        'gap_m': float(m.group(5)), 'bridge_m2': float(m.group(6)), 'steps': m.group(7),
                        'hint': raw.split('hint: ')[1].split(' · [')[0] if 'hint: ' in raw else None}
    return lines


def outcome(line, result):
    """One marks line against a later run's <scene>.<layer>.json."""
    members = set(line['members'])
    for f in result['features']:
        if f['id'] == line['feature_id']:
            return {'outcome': 'present', 'feature_id': f['id'], 'area_m2': f['area_m2'], 'bridge_m2': f['bridge_recomputed_m2'],
                    'largest_member_m2': f.get('largest_member_m2'), 'vertices': f['vertices'], 'vertex_cap': f.get('vertex_cap', False)}
    for f in result['features']:
        hit = members & {m['id'] for m in f['members']}
        if hit:
            return {'outcome': f"changed:{f['id']}", 'feature_id': f['id'], 'members_now': [m['id'] for m in f['members']],
                    'shared_members': sorted(hit), 'area_m2': f['area_m2']}
    covering = [r for r in result['refusals'] if r['kind'] == line['kind'] and set(r.get('ids') or [*r.get('a_ids', []), *r.get('b_ids', [])]) <= members]
    if covering:
        exact = [r for r in covering if set(r.get('ids') or []) == members]
        reasons = sorted({r['reason'] for r in covering})
        chosen = (exact or covering)[0]
        detail = {k: chosen[k] for k in ('feature_m2', 'largest_member_m2', 'floor_m2', 'bridge_m2', 'blockers', 'gap_m') if k in chosen}
        return {'outcome': f"refused:{chosen['reason']}", 'refusal_reasons_on_members': reasons, 'refusals': len(covering), **detail}
    return {'outcome': 'absent'}


def build(run, marks_dir=EXP/'run-v2', date=MARKS_DATE):
    sheet = marks_dir/f'MARKS-{date}.md'
    lines = parse_marks(sheet)
    run_dir = EXP/run
    metrics = json.loads((run_dir/'metrics.json').read_text()) if (run_dir/'metrics.json').exists() else None
    rows = {}
    for n, line in sorted(lines.items()):
        path = run_dir/f"{line['scene']}.{LAYER}.json"
        later = outcome(line, json.loads(path.read_text())) if path.exists() else {'outcome': 'no-run'}
        rows[str(n)] = {'mark': MARKS[n], 'scene': line['scene'], 'kind': line['kind'], 'feature_id': line['feature_id'],
                        'members': line['members'], 'run_v2': {'gap_m': line['gap_m'], 'bridge_m2': line['bridge_m2'], 'steps': line['steps'],
                                                              'hint': line['hint']},
                        run.replace('-', '_'): later}
    by_mark = {}
    for n, row in rows.items():
        by_mark.setdefault(row['mark'], []).append(int(n))
    tally = {'marks': {k: sorted(v) for k, v in by_mark.items()},
             'counts': {k: len(v) for k, v in by_mark.items()},
             f'{run.replace("-", "_")}_outcomes': {n: row[run.replace('-', '_')]['outcome'] for n, row in rows.items()},
             'wrong_lines_removed': sorted(int(n) for n, row in rows.items() if row['mark'] == 'wrong' and row[run.replace('-', '_')]['outcome'] != 'present'),
             'right_lines_kept': sorted(int(n) for n, row in rows.items() if row['mark'] == 'right' and row[run.replace('-', '_')]['outcome'] == 'present')}
    return {'version': f'linear-marks-tally-{date}', 'authorisation': AUTHORISATION, 'defaults_adopted': DEFAULTS_ADOPTED,
            'marked_sheet': str(sheet.relative_to(ROOT)), 'later_run': run, 'later_run_version': metrics and metrics.get('version'),
            'later_run_computed_at': metrics and metrics.get('computed_at'), 'layer': LAYER, 'tally': tally, 'lines': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--run', default='run-v3', help='later run directory name under the experiment (default run-v3)')
    parser.add_argument('--marks-dir', type=Path, default=EXP/'run-v2')
    parser.add_argument('--write', action='store_true', help='write <marks-dir>/MARKS-TALLY-<date>.json (default: print only)')
    args = parser.parse_args(argv)
    tally = build(args.run, args.marks_dir)
    for n, row in tally['lines'].items():
        later = row[args.run.replace('-', '_')]
        print(f"{n}. {row['mark']:7s} {row['kind']:8s} {row['feature_id']} {row['scene']} -> {later['outcome']}")
    print(json.dumps(tally['tally'], indent=None))
    if args.write:
        out = args.marks_dir/f'MARKS-TALLY-{MARKS_DATE}.json'
        out.write_text(json.dumps(tally, indent=1)+'\n')
        print('written:', out)


if __name__ == '__main__':
    main()
