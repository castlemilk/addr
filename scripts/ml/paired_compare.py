"""Paired before/after comparison of two predictions from identical input bytes.

Roofs join by lineage (id equality, else raw-ring IoU >= 0.8). Bands, detector
scores, image-line mismatch and segment counts are diagnostics, not
labels/accuracy. Only T1/T2 registry references anchor IoU and line
precision/recall; small reference counts are reported as anecdotal. Every
statement carries its denominator. Reads model files (latest.json) and
confidence.json, never only the sqlite index (it holds owned roofs only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from shapely.errors import GEOSException
from shapely.geometry import Polygon
from shapely.geometry import shape as geojson_shape
from shapely.strtree import STRtree

from . import eyes_index, scene_lineage
from .reference_agreement import lines_match
from .scene_confidence import geographic, profile
from .scene_self_review import validate_id
from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy

VERSION = 'paired-compare-v1'
RESAMPLES = 2000
SIGN_TOLERANCE = .02
MIN_CLAIM_N = 20  # Same floor as scene_confidence.MIN_CALIBRATION_GROUPS.
REFERENCE_MIN_IOU = .3
REFERENCE_TIERS = ('T1', 'T2')
LINE_TOLERANCE_M = .5
LINE_TOLERANCE_DEG = 10.
REPO = Path(__file__).resolve().parents[2]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
METRICS = ('score_delta', 'mismatch_delta', 'segments_delta', 'iou_before_after', 'reference_iou_delta')


def load_scene(root, scene_id):
    path = Path(root)/'data/scenes'/validate_id(scene_id)/'latest.json'
    return json.loads(path.read_text()) if path.exists() else None


def read_confidence(root, scene_id, scene):
    """confidence.json rows for this exact model generation, with a status."""
    path = Path(root)/'data/scenes'/scene_id/'confidence.json'
    if not path.exists():
        return None, 'missing'
    report = json.loads(path.read_text())
    if report.get('model_signature') != scene.get('model_signature'):
        return None, 'stale'
    return {r['object_id']: r for r in report.get('objects', [])}, 'current'


def to_local(points, scene):
    """Inverse of scene_confidence.geographic: WGS84 -> the scene's local metres."""
    center = scene['centroid']
    origin = np.array(lonlat_to_tile_xy(center['lon'], center['lat'], 0))
    scale = WORLD_SPAN_M*math.cos(math.radians(center['lat']))
    tiles = np.array([lonlat_to_tile_xy(float(p[0]), float(p[1]), 0) for p in points])
    return (tiles - origin) * scale * np.array([1, -1])


def _polygon(value):
    try:
        if isinstance(value, dict):
            polygon = geojson_shape(value)
        else:
            polygon = Polygon(value)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except (ValueError, TypeError, KeyError, AttributeError, GEOSException):
        return None
    return polygon if not polygon.is_empty and polygon.area > 0 else None


def _tier(row):
    tier = str(row.get('tier', '') or '').strip().upper()
    return tier if tier.startswith('T') else f'T{tier}'


def _payload(row):
    """Registry payload as a dict; malformed JSON is treated as empty (the row then anchors nothing)."""
    payload = row.get('payload') or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _finite(values):
    return [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]


def references(registry_rows):
    """Only human references (T1/T2) with a usable WGS84 polygon; T3/T4 never anchor accuracy."""
    kept = []
    for row in registry_rows or []:
        if _tier(row) not in REFERENCE_TIERS:
            continue
        polygon = _polygon(_payload(row).get('geometry_wgs84'))
        if polygon is not None:
            kept.append((row, polygon))
    return kept


def _object_shape(obj, scene=None):
    try:
        polygon = geographic(obj, scene) if scene is not None else Polygon(obj['ring'], obj.get('holes'))
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except (ValueError, TypeError, KeyError, GEOSException):
        return None
    return polygon if not polygon.is_empty and polygon.area > 0 else None


def reference_edges(row, scene):
    """Reference roof lines as local-metre segments.

    Accepts the reference_registry format payload['edges'] = [{a_lonlat, b_lonlat, kind}]
    and payload['edges_wgs84'] = [[lon, lat], [lon, lat]] | {'line': [...], 'kind'}.
    Edges without two WGS84 points are skipped.
    """
    payload = _payload(row)
    edges = []
    for edge in [*(payload.get('edges') or []), *(payload.get('edges_wgs84') or [])]:
        kind = edge.get('kind') if isinstance(edge, dict) else None
        if isinstance(edge, dict) and edge.get('a_lonlat') is not None and edge.get('b_lonlat') is not None:
            coords = [edge['a_lonlat'], edge['b_lonlat']]
        elif isinstance(edge, dict):
            coords = edge.get('line') or edge.get('coordinates')
        else:
            coords = edge
        try:
            if not coords or len(coords) < 2:
                continue
            local = to_local([c[:2] for c in coords], scene)
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if np.isfinite(local).all():
            edges.append((local[0], local[-1], kind))
    return edges


def model_segments(obj):
    for line in obj.get('roof_lines') or []:
        points = np.asarray(line.get('line_meters') or [], dtype=float)
        if points.ndim == 2 and len(points) >= 2:
            yield points[0, :2], points[-1, :2], line.get('kind')


def segments_match(a, b):
    """reference_agreement.lines_match is the one line rule across the eyes modules (<= 0.5 m, <= 10 deg)."""
    return bool(lines_match([a[0], a[1]], [b[0], b[1]], distance=LINE_TOLERANCE_M, angle=LINE_TOLERANCE_DEG))


def line_pr(obj, edges):
    """Any-match line precision/recall under reference_agreement.lines_match (<= 0.5 m, <= 10 deg)."""
    if obj is None or not edges:
        return None
    model = list(model_segments(obj))
    matched_model = sum(any(segments_match(m, r) for r in edges) for m in model)
    matched_ref = sum(any(segments_match(m, r) for m in model) for r in edges)
    return {'precision': matched_model/len(model) if model else None, 'recall': matched_ref/len(edges),
            'n_model_lines': len(model), 'n_ref_edges': len(edges)}


def _iou(a, b):
    if a is None or b is None:
        return None
    overlap = a.intersection(b).area
    union = a.area + b.area - overlap
    return round(overlap/union, 6) if union > 0 else 0.


def _delta(before, after):
    if before is None or after is None:
        return None
    return round(float(after) - float(before), 6)


def _score(obj):
    value = (obj or {}).get('score')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def bootstrap_median(values, *, resamples=RESAMPLES, seed=0):
    """Percentile bootstrap of the median over roofs (seeded, chunked)."""
    data = np.asarray(_finite(values), dtype=float)
    out = {'n': int(len(data)), 'median': None, 'ci95': None, 'resamples': resamples, 'seed': seed, 'unit': 'roof'}
    if not len(data):
        return out
    out['median'] = round(float(np.median(data)), 6)
    if len(data) >= 2:
        rng = np.random.default_rng(seed)
        medians, step = [], max(1, 2_000_000 // len(data))
        for start in range(0, resamples, step):
            size = min(step, resamples-start)
            medians.append(np.median(data[rng.integers(0, len(data), size=(size, len(data)))], axis=1))
        lo, hi = np.percentile(np.concatenate(medians), [2.5, 97.5])
        out['ci95'] = [round(float(lo), 6), round(float(hi), 6)]
    return out


def sign_counts(values, tolerance=SIGN_TOLERANCE):
    data = _finite(values)
    return {'improved': sum(v > tolerance for v in data), 'worse': sum(v < -tolerance for v in data),
            'within_tolerance': sum(abs(v) <= tolerance for v in data), 'n': len(data), 'tolerance': tolerance}


def _roof(sid, link, before, after, sides, refs, tree):
    (base, conf_b), (cand, conf_a) = sides
    cb = (conf_b or {}).get(link['baseline_object_id']) if before else None
    ca = (conf_a or {}).get(link['candidate_object_id']) if after else None
    seg_b, seg_a = (cb or {}).get('observed_segments'), (ca or {}).get('observed_segments')
    mis_b, mis_a = (cb or {}).get('image_line_mismatch_m'), (ca or {}).get('image_line_mismatch_m')
    row = {'scene_id': sid, 'baseline_object_id': link['baseline_object_id'], 'candidate_object_id': link['candidate_object_id'],
           'match': link['match'], 'raw_ring_iou': link['raw_ring_iou'],
           'band_before': (cb or {}).get('band'), 'band_after': (ca or {}).get('band'),
           'score_before': _score(before), 'score_after': _score(after), 'score_delta': _delta(_score(before), _score(after)),
           'segments_before': seg_b, 'segments_after': seg_a, 'segments_delta': _delta(seg_b, seg_a),
           'mismatch_before': mis_b, 'mismatch_after': mis_a,
           # Zero/few-segment mismatch is saturated or undefined; never average it.
           'mismatch_delta': _delta(mis_b, mis_a) if (seg_b or 0) >= 3 and (seg_a or 0) >= 3 else None,
           'iou_before_after': _iou(_object_shape(before), _object_shape(after)) if before and after else None,
           'reference': None, 'n_references': 0, 'reference_iou_before': None, 'reference_iou_after': None,
           'reference_iou_delta': None, 'line_pr_before': None, 'line_pr_after': None}
    geo_b = _object_shape(before, base) if before else None
    geo_a = _object_shape(after, cand) if after else None
    probe = [g for g in (geo_b, geo_a) if g is not None]
    if not refs or not probe:
        return row
    best = None
    for index in sorted({int(i) for g in probe for i in tree.query(g, predicate='intersects')}):
        ref, polygon = refs[index]
        ib, ia = _iou(geo_b, polygon), _iou(geo_a, polygon)
        score = max(v for v in (ib, ia) if v is not None)
        if score >= REFERENCE_MIN_IOU:
            row['n_references'] += 1
            if best is None or score > best[0]:
                best = (score, ref, ib, ia)
    if best:
        _, ref, ib, ia = best
        # The best-overlapping reference anchors the row; n_references counts every T1/T2 overlap >= threshold.
        row.update(reference={'ref_id': ref.get('ref_id'), 'tier': _tier(ref), 'source_kind': ref.get('source_kind')},
                   reference_iou_before=ib, reference_iou_after=ia, reference_iou_delta=_delta(ib, ia),
                   line_pr_before=line_pr(before, reference_edges(ref, base)) if before else None,
                   line_pr_after=line_pr(after, reference_edges(ref, cand)) if after else None)
    return row


def statements(counts, boot, roofs, scenes, population):
    """Claims only where denominators support them; everything else is cannot_claim."""
    skipped = [s for s in scenes if not s.get('paired')]
    paired = [r for r in roofs if r['match'] in ('id', 'iou')]
    n_scenes = len({r['scene_id'] for r in roofs})
    claims = [f"{counts['paired']} paired roofs across {n_scenes} scene(s) with identical input bytes; "
              f"{counts['gained']} gained, {counts['lost']} lost (lineage: id equality, else raw-ring IoU >= 0.8)."]
    banded = [r for r in paired if r['band_before'] and r['band_after']]
    if banded:
        moved = sum(r['band_before'] != r['band_after'] for r in banded)
        claims.append(f'{moved} of {len(banded)} paired roofs with bands on both sides changed evidence band (diagnostic, not accuracy).')
    cannot = []
    for metric, label in (('score_delta', 'detector score'), ('mismatch_delta', 'image-line mismatch (m, >= 3 segments both sides)'),
                          ('segments_delta', 'observed segments')):
        b = boot[metric]
        lo, hi = b['ci95'] or (None, None)
        if b['n'] >= MIN_CLAIM_N and lo is not None and (lo > 0 or hi < 0):
            claims.append(f"median {label} delta {b['median']:+.4g} (95% CI {lo:+.4g}..{hi:+.4g}, n = {b['n']} paired roofs): "
                          'diagnostic shift, not accuracy.')
        else:
            why = f"n = {b['n']} < {MIN_CLAIM_N}" if b['n'] < MIN_CLAIM_N else f"n = {b['n']}, 95% CI includes 0"
            cannot.append(f'{label}: {why}; no directional claim.')
    ref = boot['reference_iou_delta']
    referenced = [r for r in paired if r['reference_iou_delta'] is not None]
    if not ref['n']:
        cannot.append('n referenced roofs = 0: no reference-anchored (T1/T2) statement is possible.')
    elif ref['n'] < MIN_CLAIM_N:
        cannot.append(f"n referenced roofs = {ref['n']}: anecdotal; reference IoU changes are case notes, not accuracy.")
        claims += [f"case: {r['scene_id']} {r['baseline_object_id']} -> {r['candidate_object_id']}: reference IoU "
                   f"{r['reference_iou_before']:.3f} -> {r['reference_iou_after']:.3f} vs {r['reference']['tier']} "
                   f"{r['reference']['ref_id']} (anecdotal)." for r in referenced]
    elif ref['ci95'] and (ref['ci95'][0] > 0 or ref['ci95'][1] < 0):
        claims.append(f"median reference IoU delta {ref['median']:+.4g} (95% CI {ref['ci95'][0]:+.4g}..{ref['ci95'][1]:+.4g}) "
                      f"over n = {ref['n']} T1/T2-referenced paired roofs.")
    else:
        cannot.append(f"reference IoU: n = {ref['n']}, 95% CI includes 0; no directional claim.")
    cannot.append('Bands, detector scores, mismatch and segment counts are diagnostics; no accuracy change follows from them.')
    cannot.append('Roofs within one tile share imagery and are not independent; the bootstrap resamples roofs, so intervals are optimistic.')
    if counts['gained'] or counts['lost']:
        cannot.append(f"{counts['gained']} gained / {counts['lost']} lost roofs have no paired delta; they are counted, not scored.")
    if population['excluded_unowned']:
        cannot.append(f"{population['excluded_unowned']} roofs outside owned_object_ids on both sides are excluded from every count.")
    cannot += [f"scene {s['scene_id']} excluded: {s['reason']}" for s in skipped]
    cannot += [f"scene {s['scene_id']}: inference profile differs ({s['profile_before'][:16]} -> {s['profile_after'][:16]}); "
               'the candidate is another model generation and is never pooled with production population counts.'
               for s in scenes if s.get('paired') and s.get('profile_before') != s.get('profile_after')]
    return claims, cannot


def compare(baseline_root, candidate_root, scene_ids, *, registry_rows=None, confidence_reader=None,
            owned_only=True, seed=0, resamples=RESAMPLES):
    """Compare roofs of two roots. confidence_reader(root, scene_id, scene) -> {object_id: row} | None."""
    refs = references(registry_rows)
    tree = STRtree([polygon for _, polygon in refs]) if refs else None
    scenes, roofs, excluded = [], [], 0
    for sid in scene_ids:
        base, cand = load_scene(baseline_root, sid), load_scene(candidate_root, sid)
        entry = {'scene_id': sid, 'baseline_model_signature': (base or {}).get('model_signature'),
                 'candidate_model_signature': (cand or {}).get('model_signature'), 'image_sha256': (base or {}).get('image_sha256')}
        scenes.append(entry)
        if base is None or cand is None:
            entry.update(paired=False, reason='latest.json is missing in the baseline or candidate root.')
            continue
        # scene_confidence.profile: the frozen inference components; candidates usually differ from production.
        entry.update(profile_before=profile(base), profile_after=profile(cand))
        frame = ('image_sha256', 'image_width', 'image_height', 'pixel_to_meters', 'centroid')
        if any(base.get(k) != cand.get(k) for k in frame):
            entry.update(paired=False, reason='input bytes or image frame differ; not a paired comparison.')
            continue
        sides = []
        for root, scene, key in ((baseline_root, base, 'confidence_before'), (candidate_root, cand, 'confidence_after')):
            if confidence_reader is None:
                rows, status = read_confidence(root, sid, scene)
            else:
                rows = confidence_reader(root, sid, scene)
                status = 'reader' if rows is not None else 'missing'
            entry[key] = status
            sides.append((scene, rows))
        owned_b = set(base.get('owned_object_ids', [o['id'] for o in base['objects']]))
        owned_a = set(cand.get('owned_object_ids', [o['id'] for o in cand['objects']]))
        before, after = scene_lineage.current(base), scene_lineage.current(cand)
        links = scene_lineage.link(base, cand)
        entry.update(paired=True, lineage=scene_lineage.stats(links))
        for link in links:
            if link['kind'] != 'building':
                continue
            if owned_only and link['baseline_object_id'] not in owned_b and link['candidate_object_id'] not in owned_a:
                excluded += 1
                continue
            roofs.append(_roof(sid, link, before.get(link['baseline_object_id']), after.get(link['candidate_object_id']),
                               sides, refs, tree))
    counts = {'paired': sum(r['match'] in ('id', 'iou') for r in roofs),
              'gained': sum(r['match'] == 'gained' for r in roofs), 'lost': sum(r['match'] == 'lost' for r in roofs)}
    paired = [r for r in roofs if r['match'] in ('id', 'iou')]
    matrix = {}
    for r in paired:
        before_band, after_band = r['band_before'] or 'missing', r['band_after'] or 'missing'
        matrix.setdefault(before_band, {}).setdefault(after_band, 0)
        matrix[before_band][after_band] += 1
    boot = {m: bootstrap_median([r[m] for r in paired], resamples=resamples, seed=seed) for m in METRICS}
    ious = _finite(r['iou_before_after'] for r in paired)
    signs = {'reference_iou_delta': sign_counts([r['reference_iou_delta'] for r in paired]),
             'iou_before_after': {'changed': sum(v < 1-SIGN_TOLERANCE for v in ious), 'unchanged': sum(v >= 1-SIGN_TOLERANCE for v in ious),
                                  'n': len(ious), 'tolerance': SIGN_TOLERANCE}}
    population = {'kind': 'building', 'owned_only': owned_only, 'excluded_unowned': excluded,
                  'baseline_roofs': counts['paired'] + counts['lost'], 'candidate_roofs': counts['paired'] + counts['gained'],
                  'profiles': {'before': sorted({s['profile_before'] for s in scenes if s.get('paired')}),
                               'after': sorted({s['profile_after'] for s in scenes if s.get('paired')})},
                  'denominator': 'paired roofs (lineage id equality, else raw-ring IoU >= 0.8), not superseded'}
    claims, cannot = statements(counts, boot, roofs, scenes, population)
    return {'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': datetime.now(UTC).isoformat(),
            'baseline_root': str(baseline_root), 'candidate_root': str(candidate_root), 'scene_ids': list(scene_ids),
            'settings': {'reference_tiers': list(REFERENCE_TIERS), 'reference_min_iou': REFERENCE_MIN_IOU,
                         'line_tolerance_m': LINE_TOLERANCE_M, 'line_tolerance_deg': LINE_TOLERANCE_DEG,
                         'lineage_iou': scene_lineage.IOU_THRESHOLD, 'min_claim_n': MIN_CLAIM_N},
            'scenes': scenes, 'population': population, 'counts': counts, 'band_transition_matrix': matrix,
            'bootstrap': boot, 'sign_counts': signs, 'roofs': roofs, 'claims': claims, 'cannot_claim': cannot,
            'note': 'Paired diagnostics, not labels/accuracy, except T1/T2 reference IoU and line precision/recall.'}


def load_registry(root):
    with closing(eyes_index.connection(Path(root), readonly=True)) as db:
        return eyes_index.rows(db, 'references')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--candidate-root', type=Path, required=True)
    parser.add_argument('--scenes', nargs='+', required=True)
    parser.add_argument('--registry-root', type=Path, help="Read T1/T2 references from this root's eyes index (read-only).")
    parser.add_argument('--all-objects', action='store_true', help='Include roofs outside owned_object_ids.')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--out', type=Path, help='Also write the report (inside the repo only under data/reviews/eyes).')
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry_root) if args.registry_root else None
        report = compare(args.baseline_root, args.candidate_root, args.scenes, registry_rows=registry,
                         owned_only=not args.all_objects, seed=args.seed)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.out:
        out = args.out.resolve()
        if out.is_relative_to(REPO) and not out.is_relative_to(REPO/'data/reviews/eyes'):
            parser.error('Write compare reports under data/reviews/eyes or outside the repository.')
        from .scene_training import atomic_json
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(out, report)
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
