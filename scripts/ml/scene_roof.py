"""Audited roof proposals for any scene building, without changing its outline."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import Polygon

from .roof_form import flat_evidence, select_roof_ends
from .roof_line_fit import refine_roof_lines
from .roof_structure import _parts_from_faces, audit_roof, shared_edges, structural_prior
from .roof_topology import select_roof_topology
from .rooflines import _classify_roofline


def propose_roof(ring, eave, rise, form, *, image=None, meters_to_pixel=None, holes=None, evidence_views=None, context_hint=None,
                 optical_guidance=True):
    """context_hint (default None: image-only, unchanged): a building-class hint from scene_object_quality (an agent hint,
    not a label) that flat_evidence may use only to waive the gates named in FLAT_RULES['context']; it is recorded in
    form_evidence['context'] and never touches the pitched path.
    evidence_views (default None: unchanged): named HxW uint8 LSD views on the image grid, forwarded to
    refine_roof_lines/select_roof_topology; every view line still passes line_evidence on the original RGB.
    optical_guidance (default True) adds a bounded, original-RGB-gated second stage to new automatic proposals;
    False is the unchanged pre-guidance baseline for paired comparisons. No saved object is mutated."""
    if not isinstance(optical_guidance, bool):
        raise ValueError('optical_guidance must be a boolean')
    if form not in ('auto', 'gabled', 'hipped', 'flat'):
        raise ValueError('Choose an image-guided, gabled, hipped or flat roof.')
    view_kwargs = {}
    if evidence_views is not None:
        if image is None:
            raise ValueError('evidence_views require the registered image they were aligned to.')
        from .roof_line_fit import validate_extra_views
        validate_extra_views(evidence_views, image)
        view_kwargs = {'extra_views': evidence_views}
    points = np.asarray(ring, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or not 3 <= len(points) <= 150 or not np.isfinite(points).all():
        raise ValueError('Use a valid building outline with at most 150 corners.')
    if holes is not None and (not isinstance(holes, list) or len(holes) > 32):
        raise ValueError('Use at most 32 interior gaps.')
    for hole in holes or []:
        array = np.asarray(hole, dtype=float)
        if array.ndim != 2 or array.shape[1] != 2 or not 3 <= len(array) <= 200 or not np.isfinite(array).all():
            raise ValueError('Use valid interior gap points.')
    if len(points) + sum(map(len, holes or [])) > 2000:
        raise ValueError('Use at most 2,000 total outline points.')
    polygon = Polygon(points, holes)
    if not polygon.is_valid or polygon.area < 1:
        raise ValueError('Fix the building outline before proposing roof surfaces.')
    if not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in (eave, rise)) or not (0 <= eave <= 98 and .1 <= rise <= 30 and eave + rise <= 100):
        raise ValueError('Use an eave below 98 m and a roof rise from 0.1 to 30 m (apex at most 100 m).')
    # Pixel stair-steps should not each invent another pitched plane. Fit only
    # a bounded support envelope, then clip its planes to the exact input ring.
    # The captured/saved outline is never replaced by this modelling envelope.
    support, smoothed = polygon, False
    if len(points) > 12 and form != 'flat' and not polygon.interiors:
        # Fit broad planes to a bounded support envelope. The actual outline is
        # clipped back below, so real recesses are retained in the surface.
        for tolerance in (.25, .35, .45):
            simpler = polygon.simplify(tolerance, preserve_topology=True)
            envelope = simpler.buffer(tolerance, join_style='mitre')
            if (len(simpler.exterior.coords) < len(points) * .8 and abs(simpler.area / polygon.area - 1) <= .02
                    and envelope.geom_type == 'Polygon' and not envelope.interiors and envelope.is_valid
                    and envelope.covers(polygon) and envelope.area <= polygon.area * 1.16
                    and envelope.boundary.hausdorff_distance(polygon.boundary) <= .85):
                support, smoothed = envelope, True
    roof_type = 'hipped' if form == 'auto' else form
    parts = structural_prior(support, eave, eave + rise, roof_type, regularize_eaves=False)
    decisions, form_evidence = None, None
    if form == 'auto':
        matrix = np.asarray(meters_to_pixel, dtype=float)
        if image is None or matrix.shape != (3, 2) or not np.isfinite(matrix).all():
            raise ValueError('A registered image is required to compare roof ends.')
        # Flat-roof hypothesis on the original RGB, measured against the hipped prior's creases.
        # A level starting surface can be selected without claiming measured
        # flatness when the existing appearance rules leave the family unresolved.
        form_evidence = flat_evidence(polygon, image, meters_to_pixel, pitched_parts=parts, context_hint=context_hint)
        if form_evidence['decision'] == 'flat' or form_evidence.get('representation') == 'unresolved_coplanar':
            result = _flat_proposal(ring, eave, rise, holes, form_evidence)
            if optical_guidance:
                from .roof_optical_guidance import NOTE, REPORT_SCOPES, VERSION
                reason = 'roof-family-unresolved-coplanar' if result['form_uncertain'] and form_evidence['decision'] != 'flat' else 'flat-family-retained'
                result['optical_guidance'] = {'method': VERSION, **REPORT_SCOPES, 'accepted': False, 'reason': reason,
                                               'contradictions': [], 'valley_candidates': [], 'note': NOTE}
            return result

        class Frame:
            def metre_to_pixel(self, x, y):
                return np.array([x, y, 1.]) @ matrix

        parts, decisions = select_roof_ends(parts, support, eave, image, Frame(), {}, evidence_footprint=polygon, prefer_simple=True)
        selected = {d.get('selected') for d in decisions['ends']}
        roof_type = 'mixed' if {'gable', 'hip'} <= selected else 'gabled' if 'gable' in selected else 'hipped'
    if smoothed:
        clipped_faces = []
        for part in parts:
            clipped = Polygon(part['coords']).intersection(polygon)
            pieces = [clipped] if clipped.geom_type == 'Polygon' else list(getattr(clipped, 'geoms', []))
            for piece in pieces:
                if piece.geom_type != 'Polygon' or piece.area < 1e-8:
                    continue
                if piece.interiors:
                    raise ValueError('The roof proposal contains an unsupported hole. Adjust the outline.')
                xy = list(piece.exterior.coords[:-1])
                a, b, c = (part['plane'][key] for key in ('a', 'b', 'c'))
                clipped_faces.append((xy, [a*x + b*y + c for x, y in xy]))
        offset = min(z for _, heights in clipped_faces for z in heights) - eave
        parts = _parts_from_faces([(xy, [z-offset for z in heights]) for xy, heights in clipped_faces], eave, roof_type)
    line_refinement, topology, guidance = None, None, None
    candidate_pool = []
    if form == 'auto':
        parts, line_refinement = refine_roof_lines(parts, polygon, eave, image, meters_to_pixel, **view_kwargs)
        parts, topology = select_roof_topology(parts, polygon, eave, rise, image, meters_to_pixel, prefer_simple=True,
                                               **({} if evidence_views is None else {'evidence_views': evidence_views}),
                                               **({'candidate_pool': candidate_pool} if optical_guidance else {}))
        if topology['accepted']:
            # The support candidate is fitted before clipping, because an
            # occlusion-cut boundary is not a level structural eave.
            line_refinement = topology['line_refinement']
            roof_type = topology['roof_type']
            decisions = topology['end_decisions']
            smoothed = True
        if evidence_views is not None and line_refinement is not None:
            line_refinement.setdefault('view_origins', {})
            line_refinement['evidence_view_names'] = [name for name, _ in evidence_views]
    audit = audit_roof(parts, polygon, eave, pitched=form != 'flat')
    if not audit['passed']:
        raise ValueError('The proposed roof failed physical geometry checks. Adjust its outline or form.')
    if form != 'flat' and audit.get('drainage', {}).get('passed') is not True:
        raise ValueError('Too many unsupported roof faces to check runoff. Review the outline or choose a simpler roof form.')
    if form == 'auto' and optical_guidance:
        from .roof_optical_guidance import NOTE, REPORT_SCOPES, VERSION, guide_roof
        baseline_state = parts, roof_type, decisions, line_refinement, topology, audit
        baseline_detail = {key: (topology or {}).get(key) for key in ('selected_support', 'junction_refinement', 'wing_refinement')}
        baseline_detail.update(roof_type=roof_type, end_decisions=decisions, line_refinement=line_refinement)
        try:
            guided, guidance, selected = guide_roof(parts, polygon, eave, image, meters_to_pixel,
                                                   candidate_pool, baseline_detail=baseline_detail,
                                                   family_unresolved=bool((form_evidence or {}).get('flat_low_slope_preview')))
            if guidance['accepted']:
                parts = guided
                roof_type, decisions, line_refinement = selected['roof_type'], selected['end_decisions'], selected['line_refinement']
                topology = {**topology, **{k: v for k, v in selected.items() if k != 'optical_final_cost'},
                            'accepted': True, 'reason': 'original-supported-optical-gain',
                            'pre_guidance_after': topology.get('after'), 'after': selected['optical_final_cost'],
                            'after_evidence': 'frozen-original-plus-optical'}
                audit = audit_roof(parts, polygon, eave)
        except Exception as error:  # a diagnostic stage cannot destroy a physically valid baseline
            parts, roof_type, decisions, line_refinement, topology, audit = baseline_state
            guidance = {'method': VERSION, **REPORT_SCOPES, 'accepted': False, 'reason': 'guidance-unavailable-baseline-retained',
                        'error': f'{type(error).__name__}: {error}'[:200], 'contradictions': [], 'valley_candidates': [], 'note': NOTE}
    vertices, faces, indices = [], [], {}
    for part in parts:
        face = []
        for xy, z in zip(part['coords'], part['ring_z'], strict=True):
            key = tuple(round(v, 6) for v in xy)
            if key not in indices:
                indices[key] = len(vertices)
                vertices.append([*xy, z])
            face.append(indices[key])
        faces.append(face)
    edges, lines = [], []
    for line in shared_edges(parts):
        ends = [indices.get(tuple(round(v, 6) for v in p)) for p in line]
        if None in ends:
            continue
        kind = _classify_roofline(line, parts)
        edges.append({'a': ends[0], 'b': ends[1], 'kind': kind})
        lines.append({'line_meters': line, 'kind': kind})
    annotation = {'schema_version': 1, 'roof_type': roof_type, 'vertices': vertices, 'faces': faces, 'edges': edges}
    if len(vertices) > 1500 or len(faces) > 300 or any(len(f) > 150 for f in faces):
        raise ValueError('Simplify the building outline before creating editable roof points.')
    if any(p[2] > 100 for p in vertices):
        raise ValueError('The proposed apex exceeds 100 m. Reduce the eave height or choose a flatter roof.')
    uncertain = form == 'auto' and ((form_evidence or {}).get('decision') == 'uncertain' or not decisions['ends']
                                   or any(d.get('image_choice') not in ('gable', 'hip') for d in decisions['ends']))
    note = ('Roof form is uncertain in this image. Compare flat, low-slope and pitched hypotheses before reviewing its shape.' if uncertain else
            'A physically checked starting shape. Verify its form and apexes against imagery; heights remain estimates.')
    if smoothed:
        note += ' Small boundary stair-steps were ignored when fitting roof planes; the saved outline is unchanged.'
    if decisions and any(d.get('reason') == 'simple-gable-prior' for d in decisions['ends']):
        note += ' Long-wing gables are simplicity priors where imagery is ambiguous, not confirmed roof forms.'
    if decisions and any(d.get('reason') == 'image-cut-continuation-prior' for d in decisions['ends']):
        note += ' Roof slopes continue to the image cut; the off-image end shape is unknown.'
    if topology and topology['accepted']:
        description = 'An optical-guided construction hypothesis' if guidance and guidance['accepted'] else 'A simpler construction hypothesis'
        note += f" {description} uses {topology['after']['physical_planes']} roof planes and {topology['after']['crease_runs']} continuous fold lines; small silhouette bends do not each create a hip. Clipped boundary heights may vary; they are not measured eaves."
        if topology.get('junction_refinement'):
            note += ' Nearby apex junctions were joined by allowing wing pitches to differ; this is a construction prior, not measured framing.'
        if topology.get('wing_refinement'):
            note += ' Connected pitched wings allow ridges to continue through L/T junctions. Gable and hip alternatives are checked against image evidence; end forms remain estimates.'
    if audit.get('drainage', {}).get('status') == 'passed':
        note += f" Gravity runoff reached a roof boundary from all {audit['drainage']['sample_count']} sampled surface points. Gutters, loads and compliance are not assessed."
    if line_refinement and line_refinement['accepted']:
        note += f" {line_refinement['matched_edges']} roof edges were jointly fitted to registered image lines after contrast enhancement; the outline is unchanged. Unmatched lines remain structural estimates."
    elif line_refinement:
        note += ' Image-line evidence was insufficient for a safe refinement; structural estimates were retained.'
    if line_refinement and line_refinement.get('conflicting_matches_removed'):
        note += ' Conflicting uses of the same image feature were excluded; separate roof creases require separate line evidence.'
    ridge_priors = (line_refinement or {}).get('ridge_priors', {})
    if ridge_priors.get('centered_runs'):
        note += f" {ridge_priors['centered_runs']} ridge runs retain the midpoint between supported parallel, level eaves; short image fragments cannot tilt them. These are symmetry assumptions, not measured apexes."
    if guidance and guidance['accepted']:
        note += ' Original-RGB-supported optical cues improved line explanation without losing existing supported lines; inferred valleys and heights remain unverified.'
    comparison = None
    if (form_evidence or {}).get('flat_low_slope_preview'):
        level = propose_roof(ring, eave, rise, 'flat', holes=holes)
        comparison = {'status': 'family_unresolved', 'selected_representation': 'pitched_prior',
                      'alternative': {key: level[key] for key in ('annotation', 'parts', 'lines', 'audit', 'roof_type')},
                      'alternative_label': 'Level surface / flat, shed or low-pitch unresolved',
                      'alternative_selected': False, 'height_verified': False, 'drainage_assessed': False,
                      'reason': 'Area or incomplete family evidence prevents selecting flatness; this exact-outline comparison is not a label.'}
        note += ' A level-surface comparison is included. The pitched surface is a retained prior, not a confirmed family; area alone cannot decide flatness.'
    return {'annotation': annotation, 'parts': [{'ring_meters': p['coords'], 'ring_z': p['ring_z']} for p in parts],
            'lines': lines, 'audit': audit, 'source': 'image-guided-prior' if form == 'auto' else 'reviewer-template',
            'form_uncertain': uncertain, 'roof_type': roof_type,
            'line_refinement': line_refinement, 'topology': topology,
            'note': note, 'form_evidence': form_evidence,
            **({'form_comparison': comparison} if comparison else {}),
            **({'optical_guidance': guidance} if guidance is not None else {})}


def _flat_proposal(ring, eave, rise, holes, evidence):
    """The reviewer 'flat' template, built identically, carrying the versioned image evidence that chose it."""
    from .roof_form import FLAT_RULES
    proposal = propose_roof(ring, eave, rise, 'flat', holes=holes)
    if evidence.get('representation') == 'unresolved_coplanar':
        proposal.update(source='image-guided-prior', form_uncertain=True, form_evidence=evidence,
                        note='Unresolved roof family: this coplanar starting surface avoids inventing internal folds. '
                             'Flat, shed and low-pitch surfaces cannot be distinguished on this image. '
                             'The outline and gaps are retained; horizontality, height and drainage are unmeasured.')
        return proposal
    reasons = [r[0].lower() + r[1:] if i else r for i, r in enumerate(evidence['reasons'])]
    proposal.update(source='image-guided-prior', form_uncertain=evidence['score'] < FLAT_RULES['confident_score'],
                    form_evidence=evidence,
                    note=f"Flat roof: {'; '.join(reasons)}; heights remain estimates. Model estimate, not accuracy: "
                         f"compare the pitched options if the imagery shows folds.")
    return proposal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request)
        image = None
        if request['form'] == 'auto':
            image_path = Path(request['image_path'])
            if hashlib.sha256(image_path.read_bytes()).hexdigest() != request['image_sha256']:
                raise ValueError('The source image changed. Reload before proposing a roof.')
            image = Image.open(image_path).convert('RGB')
        result = propose_roof(request['ring'], request['eave'], request['rise'], request['form'], image=image,
                              meters_to_pixel=request.get('meters_to_pixel'), holes=request.get('holes'),
                              optical_guidance=request.get('optical_guidance', True))
        print(json.dumps(result), flush=True)
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(json.dumps({'error': str(error)}), flush=True)
        raise SystemExit(2) from error


if __name__ == '__main__':
    main()
