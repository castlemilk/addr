"""Bounded optical guidance for automatic roofs, using only original RGB evidence.

DoG and CLAHE discover lines on the saved grid; neither validates its own output
or supplies an independent vote. Frozen original detections survive unchanged.
Only already generated physical roof alternatives are reconsidered. Geometry,
individual original lines and their explanation remain acceptance gates.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
from shapely.errors import GEOSException
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .optical_layers import LAYER_PARAMS, layer_arrays
from .roof_line_fit import EVIDENCE_GATE, image_segments, refine_roof_lines
from .roof_structure import audit_roof, shared_edges
from .rooflines import _classify_roofline

VERSION = 'roof-optical-guidance-v1'
LAYERS = ('dog', 'clahe12')
RULES = {'additional_segments': 24, 'plan_comparisons': 6, 'guided_refits': 2,
         'line_distance_m': .5, 'line_angle_deg': 10., 'line_overlap_m': 1., 'line_overlap_share': .5,
         'original_residual_tolerance_m': .03, 'mean_residual_tolerance_m': .01,
         'minimum_gain_m': .08, 'minimum_new_explained_m': .5, 'minimum_segments': 3,
         'maximum_extra_planes': 2}
NOTE = ('Photometric views share the source pixels; they are discovery aids, not independent evidence. '
        'Valleys are image-supported plane hypotheses; heights and accuracy are unmeasured.')
REPORT_SCOPES = {'contradictions_scope': 'comparison_baseline',
                 'valley_candidates_scope': 'pre_refit_candidate_plans',
                 'selected_valleys_scope': 'selected_proposal'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def segment_id(segment):
    return _digest({'points': np.asarray(segment['points']).tolist(),
                    'length': float(segment['length']), 'support': float(segment['support'])})[:16]


def _duplicate(segment, previous):
    return any(abs(segment['tangent'] @ old['tangent']) > .995
               and np.max(np.abs((segment['points']-old['points'][0]) @ old['normal'])) < .18
               and LineString(segment['points']).distance(LineString(old['points'])) < .18 for old in previous)


def collect_evidence(image, footprint, meters_to_pixel):
    """One original set plus independently filtered, original-RGB-gated additions."""
    original = image_segments(image, footprint, meters_to_pixel)
    scale = float(np.sqrt(abs(np.linalg.det(np.asarray(meters_to_pixel)[:2]))))
    layers = layer_arrays(image, pixels_per_meter=scale, names=LAYERS)
    discovered, decisions = [], []
    for name in LAYERS:
        # Separate filtering and capacity: enhanced seam families cannot remove
        # or displace even the hundredth surviving original segment.
        found = image_segments(image, footprint, meters_to_pixel, extra_views=[(name, layers[name])],
                               views_only=True, layer_evidence=False, diagnostics=decisions)
        discovered.extend(found)
    additional = []
    for segment in sorted(discovered, key=lambda s: s['length']*s['support'], reverse=True):
        if not _duplicate(segment, original+additional):
            additional.append(segment)
        if len(additional) == RULES['additional_segments']:
            break
    combined = [*original, *additional]
    rgb = np.ascontiguousarray(np.asarray(image.convert('RGB')))
    ids = [segment_id(s) for s in original]
    report = {'method': VERSION, 'source_rgb_sha256': hashlib.sha256(rgb.tobytes()).hexdigest(),
              'source_rgb_shape': list(rgb.shape), 'meters_to_pixel': np.asarray(meters_to_pixel).tolist(),
              'layers': [{'name': name, 'array_sha256': hashlib.sha256(layers[name].tobytes()).hexdigest(),
                          'params': LAYER_PARAMS[name], 'role': 'discovery_only'} for name in LAYERS],
              'original_gate': dict(EVIDENCE_GATE), 'original_segments': len(original),
              'preserved_original_segments': len(original), 'original_segment_ids': ids,
              'preserved_original_segment_ids': [segment_id(s) for s in combined[:len(original)]],
              'additional_segments': len(additional), 'combined_segments': len(combined),
              'additional_segment_evidence': [
                  {'segment_id': segment_id(s), 'points_meters': np.asarray(s['points']).tolist(),
                   'length_m': float(s['length']), 'original_rgb_support': float(s['support']),
                   'view_origin': s['view_origin']} for s in additional],
              'original_gate_refusals': sum(not row['original_gate_passed'] for row in decisions),
              'note': NOTE}
    return {'original': original, 'additional': additional, 'combined': combined, 'report': report,
            'gate_decisions': decisions}


def _lines(parts):
    return [np.asarray(line, dtype=float) for line in shared_edges(parts)]


def _residuals(parts, observations):
    """Finite line residual per observation; keep individual vetoes visible."""
    lines = np.asarray(_lines(parts), dtype=float)
    if not len(lines):
        return [1.5]*len(observations)
    vectors = lines[:, 1]-lines[:, 0]
    lengths = np.linalg.norm(vectors, axis=1)
    result = []
    for segment in observations:
        aligned = np.abs((vectors/np.maximum(lengths[:, None], 1e-12)) @ segment['tangent']) >= np.cos(np.deg2rad(RULES['line_angle_deg']))
        offsets = np.asarray(segment['points'])[None, :, :]-lines[:, None, 0, :]
        along = np.sum(offsets*vectors[:, None, :], axis=2)/np.maximum(lengths[:, None]**2, 1e-12)
        nearest = lines[:, None, 0, :]+np.clip(along, 0, 1)[:, :, None]*vectors[:, None, :]
        distances = np.linalg.norm(np.asarray(segment['points'])[None, :, :]-nearest, axis=2).mean(axis=1)
        result.append(round(min(1.5, float(distances[aligned].min()) if aligned.any() else 1.5), 5))
    return result


def _mean(residuals, observations):
    if not observations:
        return None
    return round(float(np.average(residuals, weights=[s['length']*s['support'] for s in observations])), 5)


def _signature(parts):
    return _digest([{'coords': p['coords'], 'ring_z': p['ring_z']} for p in parts])


def _metrics(parts, evidence):
    from .roof_topology import _line_supported_length
    original = _residuals(parts, evidence['original'])
    additional = _residuals(parts, evidence['additional'])
    combined = original+additional
    def explained(rs, observations):
        return round(sum(float(s['length']) for r, s in zip(rs, observations, strict=True)
                         if r <= RULES['line_distance_m']), 4)
    lines = _lines(parts)
    unsupported = sum(_line_supported_length(line, evidence['combined'], RULES) <
                      RULES['line_overlap_share']*np.linalg.norm(line[1]-line[0]) for line in lines)
    return {'original_mismatch_m': _mean(original, evidence['original']),
            'combined_mismatch_m': _mean(combined, evidence['combined']),
            'original_residuals_m': original, 'original_explained_count': sum(r <= RULES['line_distance_m'] for r in original),
            'original_explained_length_m': explained(original, evidence['original']),
            'additional_explained_length_m': explained(additional, evidence['additional']),
            'unsupported_creases': int(unsupported), 'planes': len(parts), 'creases': len(lines)}


def _valleys(parts, evidence, *, candidate_id, baseline_parts):
    from .roof_topology import _line_supported_length
    baseline = [line for line in _lines(baseline_parts) if _classify_roofline(line, baseline_parts) == 'valley']
    rows = []
    for line in _lines(parts):
        if _classify_roofline(line, parts) != 'valley':
            continue
        length = float(np.linalg.norm(line[1]-line[0]))
        covered = _line_supported_length(line, evidence['combined'], RULES)
        extra = _line_supported_length(line, evidence['additional'], RULES)
        supported = covered >= max(RULES['line_overlap_m'], length*RULES['line_overlap_share'])
        same = any(LineString(line).hausdorff_distance(LineString(old)) <= .5 for old in baseline)
        rows.append({'candidate_id': candidate_id, 'points_meters': line.tolist(), 'length_m': round(length, 3),
                     'status': 'valley_supported' if supported else 'valley_geometry_only',
                     'kind_source': 'concave_adjoining_model_planes', 'geometry_checked': True,
                     'original_rgb_gated_coverage_m': round(covered, 3), 'additional_coverage_m': round(extra, 3),
                     'potential_missing_valley': not same, 'note': 'Unmeasured plane hypothesis; an image line alone does not determine fold type.'})
    return rows


def _contradictions(parts, evidence, valleys):
    from .roof_topology import _line_supported_length
    result = []
    for segment, residual in zip(evidence['additional'], _residuals(parts, evidence['additional']), strict=True):
        if residual > RULES['line_distance_m']:
            result.append({'code': 'unexplained_optical_crease', 'points_meters': segment['points'].tolist(),
                           'view_origin': segment['view_origin'], 'original_rgb_support': float(segment['support']),
                           'model_residual_m': residual, 'kind': 'ambiguous_image_crease'})
    for line in _lines(parts):
        length = float(np.linalg.norm(line[1]-line[0]))
        covered = _line_supported_length(line, evidence['combined'], RULES)
        if covered < length*RULES['line_overlap_share']:
            result.append({'code': 'model_fold_without_line_support', 'points_meters': line.tolist(),
                           'model_kind': _classify_roofline(line, parts), 'covered_m': round(covered, 3),
                           'note': 'Weak line evidence is uncertainty, not proof that this fold is absent.'})
    for row in valleys:
        if row['potential_missing_valley'] and row['status'] == 'valley_supported':
            result.append({'code': 'potential_missing_valley', 'points_meters': row['points_meters'],
                           'candidate_id': row['candidate_id'], 'kind': 'valley_hypothesis',
                           'note': 'An existing audited alternative has a concave fold supported by original-RGB-gated image lines.'})
    for row in sorted(evidence['gate_decisions'], key=lambda r: r['length_m'], reverse=True):
        if not row['original_gate_passed']:
            result.append({'code': 'optical_line_failed_original_gate', 'points_meters': row['points_meters'],
                           'view_origin': row['view_origin'], 'original_rgb': row['original_rgb'],
                           'kind': 'ambiguous_image_crease', 'eligible_for_fitting': False})
            if sum(r['code'] == 'optical_line_failed_original_gate' for r in result) >= 8:
                break
    return result[:48]


def acceptance_gates(baseline_parts, candidate, footprint, eave, before, after, evidence):
    audit = audit_roof(candidate, footprint, eave)
    exact = unary_union([Polygon(p['coords']) for p in candidate]).symmetric_difference(footprint).area < 1e-5
    individually = all(a <= b+RULES['original_residual_tolerance_m']
                       for b, a in zip(before['original_residuals_m'], after['original_residuals_m'], strict=True)
                       if b <= RULES['line_distance_m'])
    mean_ok = (before['original_mismatch_m'] is None or
               after['original_mismatch_m'] <= before['original_mismatch_m']+RULES['mean_residual_tolerance_m'])
    gain = (before['combined_mismatch_m'] is not None and after['combined_mismatch_m'] is not None and
            after['combined_mismatch_m'] <= before['combined_mismatch_m']-RULES['minimum_gain_m'])
    return {'changed_geometry': _signature(candidate) != _signature(baseline_parts),
            'geometry': bool(audit['passed'] and exact), 'runoff': audit.get('drainage', {}).get('passed') is True,
            'original_individual_lines': bool(individually), 'original_mean': bool(mean_ok),
            'original_explained': after['original_explained_count'] >= before['original_explained_count'] and
                                  after['original_explained_length_m'] >= before['original_explained_length_m']-.001,
            'combined_gain': bool(gain),
            'additional_explained': after['additional_explained_length_m'] >= before['additional_explained_length_m']+RULES['minimum_new_explained_m'],
            'unsupported_folds': after['unsupported_creases'] <= before['unsupported_creases'],
            'bounded_planes': len(candidate) <= len(baseline_parts)+RULES['maximum_extra_planes'],
            'enough_evidence': len(evidence['combined']) >= RULES['minimum_segments']}


def guide_roof(parts, footprint, eave, image, meters_to_pixel, candidate_pool, *, baseline_detail, family_unresolved=False):
    """Reuse a bounded slice of existing candidates, returning parts + diagnostics + selected metadata."""
    evidence = collect_evidence(image, footprint, meters_to_pixel)
    if family_unresolved:
        return parts, {'method': VERSION, **REPORT_SCOPES, 'accepted': False, 'reason': 'roof-family-unresolved',
                       'observations': evidence['report'], 'valley_candidates': [],
                       'contradictions': [{'code': 'roof_family_unresolved_crease', 'kind': 'ambiguous_image_crease',
                                           **line, 'note': 'This image line may be a material seam, cap, level change or fold; roof family is unresolved.'}
                                          for line in evidence['report'].get('additional_segment_evidence', [])],
                       'note': NOTE}, None
    if len(parts) > 24:
        return parts, {'method': VERSION, **REPORT_SCOPES, 'accepted': False, 'reason': 'complexity-limit-baseline-retained',
                       'observations': evidence['report'], 'contradictions': [], 'valley_candidates': [],
                       'note': NOTE}, None
    before = _metrics(parts, evidence)
    candidates, seen = [(parts, baseline_detail)], {_signature(parts)}
    for candidate, _, detail in sorted(candidate_pool, key=lambda row: row[1]['score']):
        if len(candidate) > 24:
            continue
        key = _signature(candidate)
        if key not in seen:
            candidates.append((candidate, detail))
            seen.add(key)
        if len(candidates) >= RULES['plan_comparisons']:
            break
    valleys, ranked = [], []
    for index, (candidate, detail) in enumerate(candidates):
        cid = f'plan-{index}'
        metrics = before if index == 0 else _metrics(candidate, evidence)
        valleys.extend(_valleys(candidate, evidence, candidate_id=cid, baseline_parts=parts))
        ranked.append((metrics['combined_mismatch_m'] if metrics['combined_mismatch_m'] is not None else 1.5,
                       index, candidate, detail, metrics))
    pixel_ring = np.c_[np.asarray(footprint.exterior.coords), np.ones(len(footprint.exterior.coords))] @ np.asarray(meters_to_pixel)
    width, height = image.size
    clipped = bool((pixel_ring[:, 0] <= 1).any() or (pixel_ring[:, 1] <= 1).any() or
                   (pixel_ring[:, 0] >= width-2).any() or (pixel_ring[:, 1] >= height-2).any())
    report = {'method': VERSION, **REPORT_SCOPES, 'accepted': False, 'reason': 'no-safe-optical-gain', 'observations': evidence['report'],
              'rules': dict(RULES), 'before': before, 'after': before, 'clipped': clipped,
              'valley_candidates': valleys[:32], 'valley_candidates_total': len(valleys),
              'contradictions': _contradictions(parts, evidence, valleys), 'candidates': [], 'note': NOTE}
    if clipped or not evidence['additional'] or len(evidence['combined']) < RULES['minimum_segments']:
        report['reason'] = ('clipped-source-needs-context' if clipped else 'no-additional-original-supported-lines'
                            if not evidence['additional'] else 'insufficient-image-evidence')
        return parts, report, None
    chosen, chosen_detail, best_gain = parts, None, 0.
    for _, index, seed, detail, preview in sorted(ranked, key=lambda row: (row[0], row[1]))[:RULES['guided_refits']]:
        try:
            candidate, fit = refine_roof_lines(seed, footprint, eave, image, meters_to_pixel, observations=evidence['combined'])
            after = _metrics(candidate, evidence)
            gates = acceptance_gates(parts, candidate, footprint, eave, before, after, evidence)
        except (ValueError, ArithmeticError, RuntimeError, GEOSException) as error:
            report['candidates'].append({'candidate_id': f'plan-{index}', 'accepted': False,
                                         'reason': 'candidate-fit-unavailable', 'error': f'{type(error).__name__}: {error}'[:200]})
            continue
        report['candidates'].append({'candidate_id': f'plan-{index}', 'roof_type': detail.get('roof_type'),
                                     'preview': preview, 'after': after, 'gates': gates,
                                     'failed_gates': [name for name, passed in gates.items() if not passed],
                                     'line_refinement_reason': fit.get('reason')})
        gain = (before['combined_mismatch_m'] or 0)-(after['combined_mismatch_m'] or 0)
        if all(gates.values()) and gain > best_gain:
            chosen, best_gain = candidate, gain
            chosen_detail = {**detail, 'line_refinement': fit}
            report.update(accepted=True, reason='original-supported-optical-gain', after=after, selected_candidate=f'plan-{index}')
    if report['accepted']:
        from .roof_topology import topology_cost
        chosen_detail['optical_final_cost'] = topology_cost(chosen, evidence['combined'])
        report['selected_valleys'] = _valleys(chosen, evidence, candidate_id=report['selected_candidate'], baseline_parts=parts)
    return chosen, report, chosen_detail


def compact_report(report):
    """Keep actionable provenance/contradictions in new scene objects, without candidate meshes."""
    return {key: report[key] for key in ('method', *REPORT_SCOPES, 'accepted', 'reason', 'observations', 'before', 'after', 'clipped',
                                       'valley_candidates', 'valley_candidates_total', 'selected_valleys',
                                       'contradictions', 'candidates', 'note') if key in report}
