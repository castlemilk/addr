"""Agreement between the model's starting roof-line estimate and the reviewer's saved lines for the same outline.

The reviewer graph is a draft while ref_checked is false. Agreement, never accuracy; not a label.

For every review.json building (current revision only) whose roof_line_edits is non-empty, one row pairs
roof_line_edits.baseline (the graph the editor seeded from the model snapshot the reviewer saw) with
roof_line_edits.vertices/edges (what the reviewer saved): the repo line rule (0.5 m / 10 deg through
reference_agreement.line_metrics), a distance sweep, per-kind matches, length coverage shares at 1 m, graph
topology on both sides and the review revision at which the saved lines first appeared. Objects without
roof_line_edits are counted as skipped, not emitted. Nothing here derives a pattern string or a decision.

Read-only over data/scenes. `--dry-run` prints rows as JSON to stdout and writes nothing. `--build` writes the
eyes.sqlite `line_corrections` table (INSERT OR REPLACE keyed by (scene, object, review_revision, version): idempotent,
never deletes) and data/reviews/eyes/line-corrections/<UTC date>/ (rows.jsonl, summary.json).

    python -m scripts.ml.line_corrections [--root .] [--scene-ids ID ...] [--objects SCENE:OBJECT ...]
                                          [--dry-run] [--build] [--summary]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.ops import unary_union

from . import eyes_index
from . import reference_agreement as agreement
from . import reference_registry as registry
from .review_authority import review_authority_blocker
from .training.edit_diff import pair_model

VERSION = 'line-corrections-v1'
DEFAULT_OUT_ROOT = Path('data/reviews/eyes/line-corrections')
SHORT_LINE_M = 1.5
MERGE_TOLERANCE_M = 1e-5          # coincident graph nodes count as one node for the component count
COVERAGE_TOL_M = 1.
COVERAGE_STEP_M = .1
DISTANCE_SWEEP_M = {'0.35': .35, '0.5': .5, '1.0': 1.}
KINDS = ('ridge', 'hip', 'valley')
VERIFIED_PAIRS = ('snapshot', 'latest')
LINE_MATCH_RULE = {'distance_m': agreement.LINE_DISTANCE_M, 'angle_deg': agreement.LINE_ANGLE_DEG, 'source': agreement.VERSION}
NOTE = ("Agreement between the model's starting roof-line estimate and the reviewer's saved lines for the same outline. "
        'The reviewer graph is a draft while ref_checked is false. Agreement, never accuracy; not a label.')
SKIPPABLE = registry.SKIPPABLE
JOINTLY_FITTED = re.compile(r'(\d+) roof edges? (?:were|was) jointly fitted', re.IGNORECASE)
# confidence.json stores the scene_self_review reason messages; these are the issue codes that produced them.
CONFIDENCE_CODES = {
    'Roof touches the image edge; capture more context before changing its end.': 'image_cut',
    'Some eaves are inferred behind tree canopy.': 'hidden_eaves',
    'The outline has no complete editable roof surface.': 'missing_surface',
    'Surface or editable graph failed coverage, planarity, seam or runoff checks.': 'invalid_surface',
    'Rendered roof and editable vertices disagree.': 'editor_mesh_mismatch',
    'Many folds, short junctions or tiny planes warrant a simpler candidate comparison.': 'complex_folds',
    'Too few usable image lines to verify the roof topology.': 'weak_image_evidence',
    'Detected image lines and roof creases disagree; texture or solar panels may contribute.': 'image_line_disagreement',
}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _index_edges(graph):
    """(a, b, kind) for every well-formed edge of a {vertices, edges} graph, plus the malformed-edge count."""
    vertices = graph.get('vertices') if isinstance(graph, dict) else None
    edges = graph.get('edges') if isinstance(graph, dict) else None
    vertices = vertices if isinstance(vertices, list) else []
    found, malformed = [], 0
    for edge in edges if isinstance(edges, list) else []:
        a, b = (edge.get('a'), edge.get('b')) if isinstance(edge, dict) else (None, None)
        ok = all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(vertices) for i in (a, b))
        try:
            points = [[float(vertices[i][0]), float(vertices[i][1])] for i in (a, b)] if ok else None
        except (IndexError, KeyError, TypeError, ValueError):
            points = None
        if points is None or not all(math.isfinite(v) for p in points for v in p):
            malformed += 1
            continue
        found.append((a, b, edge.get('kind')))
    return found, malformed


def graph_edges(graph):
    """([(np.array([[ax, ay], [bx, by]]), kind), ...], n_malformed_edges) resolving vertex indices in local metres.

    Local helper: roof_recompute_eval._review_edges reads review.json from disk and cannot take a history revision.
    3-D vertices (roof_annotation) keep their first two coordinates.
    """
    indexed, malformed = _index_edges(graph)
    vertices = graph['vertices']
    return [(np.array([[float(vertices[a][0]), float(vertices[a][1])], [float(vertices[b][0]), float(vertices[b][1])]]), kind)
            for a, b, kind in indexed], malformed


def _components(vertices, indexed):
    """Connected components over referenced vertices; nodes within MERGE_TOLERANCE_M merge into one."""
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent.setdefault(x, x)
        parent.setdefault(y, y)
        parent[find(x)] = find(y)

    referenced = sorted({i for a, b, _ in indexed for i in (a, b)})
    for i in referenced:
        parent.setdefault(i, i)
    for a, b, _ in indexed:
        union(a, b)
    digits = max(int(round(-math.log10(MERGE_TOLERANCE_M))), 0)
    by_position = {}
    for i in referenced:
        key = (round(float(vertices[i][0]), digits), round(float(vertices[i][1]), digits))
        if key in by_position:
            union(i, by_position[key])
        else:
            by_position[key] = i
    return len({find(i) for i in referenced})


def graph_topology(graph):
    """Counts and lengths describing one roof-line graph (model baseline or reviewer edits); not a score."""
    indexed, malformed = _index_edges(graph)
    vertices = graph.get('vertices') if isinstance(graph, dict) and isinstance(graph.get('vertices'), list) else []
    referenced = {i for a, b, _ in indexed for i in (a, b)}
    degree = Counter()
    for a, b, _ in indexed:
        degree[a] += 1
        degree[b] += 1
    lengths = [float(LineString([vertices[a][:2], vertices[b][:2]]).length) for a, b, _ in indexed]
    kinds = Counter(kind for _, _, kind in indexed)
    return {'n_edges': len(indexed), 'n_malformed_edges': malformed, 'n_vertices_stored': len(vertices),
            'n_vertices_referenced': len(referenced), 'n_orphan_vertices': len(vertices) - len(referenced),
            'degree_histogram': {str(k): v for k, v in sorted(Counter(degree.values()).items())},
            'n_components': _components(vertices, indexed) if indexed else 0,
            'total_length_m': _round(sum(lengths)) if lengths else 0.,
            'median_length_m': _round(statistics.median(lengths)) if lengths else None,
            'min_length_m': _round(min(lengths)) if lengths else None,
            'n_under_1_5m': sum(length < SHORT_LINE_M for length in lengths), 'short_line_m': SHORT_LINE_M,
            'by_kind': {**{kind: kinds.get(kind, 0) for kind in KINDS},
                        'other': sum(v for k, v in kinds.items() if k not in KINDS)}}


def _lines_key(edits):
    return json.dumps([edits.get('vertices'), edits.get('edges')], sort_keys=True)


def first_revision_with_lines(review, obj):
    """(revision, updated_at, updated_by) of the oldest saved review whose same-id object already carried these lines.

    Compares roof_line_edits vertices + edges as JSON across history, oldest first, then the current review
    (review_edit_diffs.first_revision_with_outline does the same for the outline).
    """
    edits = registry.line_edits(obj)
    if not edits:
        return None, None, None
    target = _lines_key(edits)
    history = [h for h in (review.get('history') or []) if isinstance(h, dict)]
    history.sort(key=lambda h: (h.get('revision') is None, h.get('revision') if isinstance(h.get('revision'), int | float) else 0))
    for entry in [*history, review]:
        objects = entry.get('objects')
        if not isinstance(objects, list):
            continue
        match = next((o for o in objects if isinstance(o, dict) and o.get('id') == obj.get('id')), None)
        if match is not None and registry.line_edits(match) and _lines_key(registry.line_edits(match)) == target:
            return entry.get('revision'), entry.get('updated_at'), entry.get('updated_by')
    return None, None, None


def coverage_share(a_edges, b_edges, tol_m=COVERAGE_TOL_M, step_m=COVERAGE_STEP_M):
    """Share of a's total length whose samples (every step_m along each segment) lie within tol_m of any b segment.

    A coverage share for reading placement at a loose tolerance; not the repo line rule (no angle test, no
    one-to-many pairing) and never a label. None when a has no length; 0 when b is empty.
    """
    lines = [LineString(segment) for segment, _ in a_edges]
    lines = [line for line in lines if line.length > 0]
    if not lines:
        return None
    others = [LineString(segment) for segment, _ in b_edges]
    others = [line for line in others if line.length > 0]
    if not others:
        return 0.
    target = unary_union(others)
    covered, total = 0., 0.
    for line in lines:
        n = max(int(math.ceil(line.length / step_m)) + 1, 2)
        points = shapely.line_interpolate_point(line, np.linspace(0., line.length, n))
        inside = shapely.distance(points, target) <= tol_m
        covered += float(inside.sum()) * line.length / n
        total += line.length
    return _round(covered / total)


def _segment_key(segment):
    return tuple(sorted(((round(float(segment[0][0]), 4), round(float(segment[0][1]), 4)),
                         (round(float(segment[1][0]), 4), round(float(segment[1][1]), 4)))))


def baseline_matches_snapshot(baseline_edges, model_obj):
    """True when the editor's seeded baseline has exactly the snapshot roof_annotation's ridge/hip/valley segments
    (4-dp endpoint keys), False when they differ, None when the snapshot carries no roof_annotation graph."""
    graph = model_obj.get('roof_annotation') if isinstance(model_obj, dict) else None
    if not isinstance(graph, dict) or not isinstance(graph.get('edges'), list):
        return None
    snapshot, _ = graph_edges(graph)
    snapshot = [(segment, kind) for segment, kind in snapshot if kind in KINDS]
    return Counter(_segment_key(s) for s, _ in baseline_edges) == Counter(_segment_key(s) for s, _ in snapshot)


def _matches(model_edges, ref_edges, distance=agreement.LINE_DISTANCE_M, angle=agreement.LINE_ANGLE_DEG):
    return [[agreement.lines_match(m, r, distance, angle) for r, _ in ref_edges] for m, _ in model_edges]


def by_kind_matched(model_edges, ref_edges):
    """Per kind: model lines and reviewer edges of that kind, and how many of each have any match at the repo rule."""
    matches = _matches(model_edges, ref_edges)
    found = {}
    for kind in KINDS:
        model_i = [i for i, (_, k) in enumerate(model_edges) if k == kind]
        ref_j = [j for j, (_, k) in enumerate(ref_edges) if k == kind]
        found[kind] = {'n_model': len(model_i), 'n_model_matched': sum(any(matches[i]) for i in model_i),
                       'n_ref': len(ref_j), 'n_ref_matched': sum(any(row[j] for row in matches) for j in ref_j)}
    return found


def note_flags(note):
    text = str(note or '')
    fitted = JOINTLY_FITTED.search(text)
    return {'form_uncertain': 'uncertain' in text.lower(), 'jointly_fitted_edges': int(fitted.group(1)) if fitted else None}


def confidence_rows(folder):
    """{object_id: confidence row} from confidence.json plus the file's model_signature, or ({}, None) when absent."""
    report = registry._load(Path(folder) / 'confidence.json')
    objects = report.get('objects') if isinstance(report, dict) else None
    rows = {o.get('object_id'): o for o in objects if isinstance(o, dict)} if isinstance(objects, list) else {}
    return rows, report.get('model_signature') if isinstance(report, dict) else None


def confidence_codes(row):
    """Issue codes for one confidence.json object row: explicit issue codes when stored, else the known reason messages."""
    if not isinstance(row, dict):
        return None
    issues = row.get('issues')
    if isinstance(issues, list):
        return sorted({i.get('code') for i in issues if isinstance(i, dict) and isinstance(i.get('code'), str)})
    reasons = row.get('reasons')
    if isinstance(reasons, list):
        return sorted({CONFIDENCE_CODES[r] for r in reasons if isinstance(r, str) and r in CONFIDENCE_CODES})
    return None


def object_row(scene_id, scene, review, obj, model_obj, pair, confidence=None):
    """Measured payload for one building's roof_line_edits against its baseline; every number is agreement."""
    edits = registry.line_edits(obj)
    baseline = edits.get('baseline') if isinstance(edits.get('baseline'), dict) else {}
    outline = edits.get('outline') if isinstance(edits.get('outline'), dict) else {}
    ref_edges, ref_malformed = graph_edges(edits)
    model_edges, model_malformed = graph_edges(baseline)
    model_ring = model_obj.get('ring') if isinstance(model_obj, dict) else None
    model_holes = (model_obj.get('holes') or []) if model_ring is not None else None
    verified = pair in VERIFIED_PAIRS
    tier, provenance, _ = registry.review_tier(obj, model_ring, verified, model_holes=model_holes)
    revision, edited_at, edited_by = first_revision_with_lines(review, obj)
    metrics = agreement.line_metrics(model_edges, ref_edges)
    sweep = {}
    for label, distance in DISTANCE_SWEEP_M.items():
        swept = agreement.line_metrics(model_edges, ref_edges, distance=distance)
        sweep[label] = {k: swept[k] for k in ('line_precision', 'line_recall', 'n_matched_model_lines', 'n_matched_ref_edges')}
    source = model_obj if isinstance(model_obj, dict) else obj
    rows, confidence_signature = confidence or ({}, None)
    confidence_row = rows.get(obj.get('id'))
    roof_lines = source.get('roof_lines')
    return {'scene_id': scene_id, 'object_id': obj.get('id'), 'model_signature': review.get('model_signature'),
            'image_sha256': scene.get('image_sha256'), 'pair': pair,
            'review_revision': review.get('revision'), 'review_updated_at': review.get('updated_at'),
            'review_updated_by': review.get('updated_by'),
            'edited_at_revision': revision, 'edited_at': edited_at, 'edited_by': edited_by,
            'object_status': obj.get('status'), 'geometry_reviewed': bool(obj.get('geometry_reviewed')),
            'ref_checked': edits.get('checked') is True, 'notes_len': len(str(edits.get('notes') or '')),
            'authority_blocker': review_authority_blocker(obj) or None,
            'review_tier': tier, 'review_provenance': provenance,
            'outline_matches_object': bool(registry._same_ring(outline.get('ring'), obj.get('ring'))
                                           and registry._same_holes(outline.get('holes'), obj.get('holes'))),
            'outline_changed_since_model': bool(registry.outline_edited(obj, model_ring, model_holes)),
            'baseline_matches_snapshot': baseline_matches_snapshot(model_edges, model_obj),
            'snapshot_roof_lines_n': len(roof_lines) if isinstance(roof_lines, list) else None,
            **metrics, 'line_match_rule': dict(LINE_MATCH_RULE), 'distance_sweep_m': sweep,
            'by_kind_matched': by_kind_matched(model_edges, ref_edges),
            'topology_model': graph_topology(baseline), 'topology_ref': graph_topology(edits),
            'n_malformed_edges': {'model': model_malformed, 'ref': ref_malformed},
            'count_ratio': _round(len(model_edges) / len(ref_edges)) if ref_edges else None,
            'ref_length_within_1m_share': coverage_share(ref_edges, model_edges),
            'model_length_within_1m_share': coverage_share(model_edges, ref_edges),
            'coverage_rule': {'tol_m': COVERAGE_TOL_M, 'step_m': COVERAGE_STEP_M, 'note': 'length coverage share, not the repo line rule'},
            'pipeline': (scene.get('analysis') or {}).get('pipeline') if isinstance(scene.get('analysis'), dict) else None,
            'roof_type': source.get('roof_type'), 'roof_geometry_source': source.get('roof_geometry_source'),
            'note_flags': note_flags(source.get('roof_geometry_note')),
            'confidence_issue_codes': confidence_codes(confidence_row),
            'confidence_band': confidence_row.get('band') if isinstance(confidence_row, dict) else None,
            'confidence_signature_matches_review': (None if confidence_signature is None
                                                    else confidence_signature == review.get('model_signature')),
            'edits_sha256': hashlib.sha256(json.dumps(edits, sort_keys=True).encode()).hexdigest(),
            'baseline_sha256': hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest(),
            'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': datetime.now(UTC).isoformat(), 'note': NOTE}


def eyes_row(payload):
    return {'scene_id': payload['scene_id'], 'object_id': payload['object_id'], 'review_revision': payload['review_revision'],
            'model_signature': payload['model_signature'], 'version': VERSION, 'payload': payload}


def scene_rows(root, scene_id, skipped, only_objects=None):
    """Rows for one scene's current review revision; `skipped` receives every exclusion reason."""
    folder = Path(root) / 'data/scenes' / scene_id
    review = registry._load(folder / 'review.json')
    if not isinstance(review, dict):
        skipped['review_unreadable'] += 1
        return []
    scene = registry._load(folder / 'latest.json')
    if not isinstance(scene, dict):
        skipped['scene_unreadable'] += 1
        return []
    objects = review.get('objects')
    if not isinstance(objects, list):
        skipped['review_objects_not_list'] += 1
        return []
    buildings = [o for o in objects if isinstance(o, dict) and o.get('kind') == 'building' and o.get('id')
                 and (not only_objects or o['id'] in only_objects)]
    model_objects, pair = pair_model({'scene': scene, 'review': review, 'folder': folder})
    if model_objects is None:
        skipped[f'{pair}:objects'] += len(buildings)
        return []
    confidence = confidence_rows(folder)
    rows = []
    for obj in buildings:
        if not registry.line_edits(obj):
            skipped['no_roof_line_edits'] += 1
            continue
        if not isinstance(review.get('revision'), int) or isinstance(review.get('revision'), bool):
            skipped['review_revision_missing'] += 1
            continue
        model_obj = model_objects.get(obj['id'])
        try:
            rows.append(object_row(scene_id, scene, review, obj, model_obj, pair if model_obj is not None else 'manual', confidence))
        except SKIPPABLE as exc:
            skipped[f'row_error:{type(exc).__name__}'] += 1
    return rows


def list_scene_ids(root, scene_ids=()):
    if scene_ids:
        return [s for s in dict.fromkeys(scene_ids) if registry.SCENE_ID.fullmatch(s)]
    return sorted(p.parent.name for p in (Path(root) / 'data/scenes').glob('*/review.json')
                  if registry.SCENE_ID.fullmatch(p.parent.name) and not p.parent.name.startswith('qa_'))


def parse_objects(pairs):
    """{scene_id: {object_id, ...}} from SCENE:OBJECT strings."""
    wanted = {}
    for pair in pairs or ():
        scene_id, _, object_id = str(pair).partition(':')
        if not scene_id or not object_id or not registry.SCENE_ID.fullmatch(scene_id):
            raise ValueError(f'--objects wants SCENE:OBJECT, got {pair!r}')
        wanted.setdefault(scene_id, set()).add(object_id)
    return wanted


def build(root=Path('.'), scene_ids=(), objects=()):
    """(rows, skipped) over every scene with a review.json, or the given scene ids / SCENE:OBJECT pairs. Reads only."""
    root, skipped, rows = Path(root), Counter(), []
    wanted = parse_objects(objects)
    ids = list_scene_ids(root, [*scene_ids, *wanted]) if (scene_ids or wanted) else list_scene_ids(root)
    for scene_id in ids:
        skipped['scenes_seen'] += 1
        rows.extend(scene_rows(root, scene_id, skipped, wanted.get(scene_id) if wanted and scene_id in wanted else None))
    return rows, skipped


def _median(values):
    values = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    return (_round(statistics.median(values)), len(values)) if values else (None, 0)


def summarize(rows, skipped=None):
    """Counts and medians with explicit denominators; empty sets are None, never 0."""
    skipped = skipped or Counter()
    medians = {}
    for key in ('line_precision', 'line_recall', 'count_ratio', 'ref_length_within_1m_share', 'model_length_within_1m_share'):
        value, n = _median(r.get(key) for r in rows)
        medians[key] = {'median': value, 'n': n}
    return {'version': VERSION, 'note': NOTE, 'n_rows': len(rows),
            'n_objects': len({(r['scene_id'], r['object_id']) for r in rows}),
            'n_scenes_seen': skipped.get('scenes_seen', 0),
            'skipped': {k: v for k, v in sorted(skipped.items()) if k != 'scenes_seen'},
            'by_pair': dict(sorted(Counter(str(r.get('pair')) for r in rows).items())),
            'by_ref_checked': dict(sorted(Counter(str(bool(r.get('ref_checked'))).lower() for r in rows).items())),
            'by_review_tier': dict(sorted(Counter(str(r.get('review_tier')) for r in rows).items())),
            'by_object_status': dict(sorted(Counter(str(r.get('object_status')) for r in rows).items())),
            'n_baseline_matches_snapshot': sum(r.get('baseline_matches_snapshot') is True for r in rows),
            'n_outline_matches_object': sum(bool(r.get('outline_matches_object')) for r in rows),
            'n_count_ratio_gt_2': sum(isinstance(r.get('count_ratio'), float) and r['count_ratio'] > 2 for r in rows),
            'medians': medians, 'line_match_rule': dict(LINE_MATCH_RULE), 'distance_sweep_m': list(DISTANCE_SWEEP_M.values()),
            'coverage_tol_m': COVERAGE_TOL_M, 'code_sha256': code_sha256(), 'computed_at': datetime.now(UTC).isoformat()}


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    with os.fdopen(handle, 'w') as out:
        out.write(text)
    os.replace(temporary, path)


def default_out(root):
    return Path(root) / DEFAULT_OUT_ROOT / datetime.now(UTC).strftime('%Y-%m-%d')


def write(root, rows, summary, out=None):
    """INSERT OR REPLACE the rows into eyes.sqlite `line_corrections`, then rows.jsonl + summary.json under `out`."""
    root = Path(root)
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        written = eyes_index.replace_rows(db, 'line_corrections', [eyes_row(r) for r in rows])
    out = Path(out) if out else default_out(root)
    rows_path = out / 'rows.jsonl'
    _write_atomic(rows_path, ''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    summary = {**summary, 'out': str(out), 'eyes_rows_written': written, 'eyes_path': str(eyes_index.path(root)),
               'files': {'rows.jsonl': hashlib.sha256(rows_path.read_bytes()).hexdigest()}}
    _write_atomic(out / 'summary.json', json.dumps(summary, indent=2, sort_keys=True))
    return summary


def stored_rows(root):
    """Stored payloads of this version, highest review_revision per (scene, object)."""
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = eyes_index.rows(db, 'line_corrections', version=VERSION) if 'line_corrections' in eyes_index.tables_present(db) else []
    latest = {}
    for row in rows:
        key = (row['scene_id'], row['object_id'])
        revision = row.get('review_revision') if isinstance(row.get('review_revision'), int) else -1
        if key not in latest or revision > latest[key][0]:
            latest[key] = (revision, row['payload'])
    return [payload for _, payload in latest.values() if isinstance(payload, dict)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--scene-ids', nargs='*', default=[], help='Restrict to these scene ids.')
    parser.add_argument('--objects', nargs='*', default=[], metavar='SCENE:OBJECT', help='Restrict to these objects.')
    parser.add_argument('--dry-run', action='store_true', help='Print the rows as JSON; write nothing.')
    parser.add_argument('--build', action='store_true', help='Write eyes.sqlite line_corrections and the dated folder.')
    parser.add_argument('--summary', action='store_true', help='Print the summary (stored rows when neither --dry-run nor --build).')
    parser.add_argument('--out', help='Output folder for --build (default <root>/data/reviews/eyes/line-corrections/<UTC date>).')
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        wanted = parse_objects(args.objects)
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run or args.build:
        rows, skipped = build(root, tuple(args.scene_ids), tuple(args.objects))
        if args.dry_run:
            report = {'version': VERSION, 'dry_run': True, 'n_rows': len(rows), 'skipped': dict(sorted(skipped.items())),
                      'rows': rows, 'note': NOTE}
            if args.summary:
                report['summary'] = summarize(rows, skipped)
            json.dump(report, sys.stdout, sort_keys=True, indent=1)
            sys.stdout.write('\n')
            return 0
        summary = write(root, rows, summarize(rows, skipped), args.out)
        json.dump(summary if args.summary else {k: summary[k] for k in ('version', 'n_rows', 'eyes_rows_written', 'out', 'skipped')},
                  sys.stdout, sort_keys=True, indent=1)
        sys.stdout.write('\n')
        return 0
    if args.summary:
        if not eyes_index.path(root).exists():
            parser.error('No eyes index yet; run --build first')
        rows = stored_rows(root)
        if wanted:
            rows = [r for r in rows if r.get('object_id') in wanted.get(r.get('scene_id'), ())]
        elif args.scene_ids:
            rows = [r for r in rows if r.get('scene_id') in set(args.scene_ids)]
        json.dump({**summarize(rows), 'source': 'eyes.sqlite'}, sys.stdout, sort_keys=True, indent=1)
        sys.stdout.write('\n')
        return 0
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
