import copy
import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml import roof_optical_guidance as guidance
from scripts.ml.roof_line_fit import image_segments, refine_roof_lines
from scripts.ml.roof_structure import audit_roof, structural_prior
from scripts.ml.roof_wings import wing_candidates
from scripts.ml.scene_roof import propose_roof
from scripts.ml.tests.test_roof_line_fit import FRAME, ROOF, roof_image
from scripts.ml.tests.test_roof_wings import L


def segment(points, support=.7):
    points = np.asarray(points, dtype=float)
    vector = points[1]-points[0]
    length = float(np.linalg.norm(vector))
    tangent = vector/length
    return {'points': points, 'tangent': tangent, 'normal': np.array([-tangent[1], tangent[0]]),
            'length': length, 'support': support, 'view_origin': 'dog'}


def bundle(original, additional):
    return {'original': original, 'additional': additional, 'combined': original+additional,
            'report': {'original_segments': len(original), 'additional_segments': len(additional)}, 'gate_decisions': []}


def test_every_original_survives_capacity_and_layers_are_not_duplicate_votes(monkeypatch):
    originals = [segment([[0, i], [2, i]], .2+i/200) for i in range(100)]
    added = segment([[4, 4], [9, 4]])
    before = copy.deepcopy(originals)
    calls = []

    def detect(*args, **kwargs):
        calls.append(kwargs)
        return originals if not kwargs else [added]

    monkeypatch.setattr(guidance, 'image_segments', detect)
    evidence = guidance.collect_evidence(roof_image(), ROOF, FRAME)
    assert len(evidence['combined']) == 101 and len(evidence['additional']) == 1
    assert all(evidence['combined'][i] is s for i, s in enumerate(originals))
    assert all(np.array_equal(s['points'], b['points']) and s['support'] == b['support'] for s, b in zip(originals, before, strict=True))
    report = evidence['report']
    assert report['original_segment_ids'] == report['preserved_original_segment_ids']
    assert report['original_segments'] == report['preserved_original_segments'] == 100
    assert all(call['views_only'] and call['layer_evidence'] is False for call in calls[1:])
    retained = json.loads(json.dumps(report))['additional_segment_evidence']
    assert len(retained) == 1
    assert retained[0]['points_meters'] == added['points'].tolist()
    assert retained[0]['original_rgb_support'] == added['support'] and retained[0]['view_origin'] == 'dog'


def test_bandpass_edges_cannot_validate_themselves_on_uniform_rgb(monkeypatch):
    image = Image.new('RGB', roof_image().size, (140, 130, 120))
    view = np.full((image.height, image.width), 40, np.uint8)
    view[100:] = 220
    monkeypatch.setattr(guidance, 'layer_arrays', lambda *args, **kwargs: {'dog': view, 'clahe12': view})
    evidence = guidance.collect_evidence(image, ROOF, FRAME)
    assert evidence['original'] == evidence['additional'] == evidence['combined'] == []
    assert evidence['report']['original_gate_refusals'] > 0
    assert all(not row['original_gate_passed'] for row in evidence['gate_decisions'])


def test_frozen_cues_reach_real_fitting_and_geometry_checked_acceptance(monkeypatch):
    image = roof_image()
    prior = structural_prior(ROOF, 3, 5, 'hipped', regularize_eaves=False)
    observations = [{**s, 'view_origin': 'test-optical-discovery'} for s in image_segments(image, ROOF, FRAME)]
    evidence = bundle([], observations)
    monkeypatch.setattr(guidance, 'collect_evidence', lambda *args: evidence)
    result, report, selected = guidance.guide_roof(prior, ROOF, 3, image, FRAME, [],
                                                  baseline_detail={'roof_type': 'hipped', 'end_decisions': {'ends': []}})
    assert report['accepted'], report
    assert report['after']['additional_explained_length_m'] > report['before']['additional_explained_length_m']
    assert all(report['candidates'][0]['gates'].values())
    assert result != prior and selected['roof_type'] == 'hipped'
    assert audit_roof(result, ROOF, 3)['passed']


def test_improvement_elsewhere_cannot_hide_loss_of_an_original_ridge():
    prior = structural_prior(ROOF, 3, 5, 'hipped', regularize_eaves=False)
    candidate, fitted = refine_roof_lines(prior, ROOF, 3, roof_image(), FRAME)
    assert fitted['accepted']
    original = [segment([[5, 4], [7, 4]], support=.12)]
    evidence = bundle(original, image_segments(roof_image(), ROOF, FRAME))
    before, after = guidance._metrics(prior, evidence), guidance._metrics(candidate, evidence)
    assert after['combined_mismatch_m'] < before['combined_mismatch_m']
    gates = guidance.acceptance_gates(prior, candidate, ROOF, 3, before, after, evidence)
    assert gates['geometry'] and not gates['original_individual_lines'] and not gates['original_explained']


def test_valley_requires_concave_planes_and_can_identify_a_missing_valley():
    parts, _ = wing_candidates(L, 3, 2)[0]
    assert audit_roof(parts, L, 3)['passed']
    observations = [segment([[8, 6], [4, 3]]), segment([[4, 3], [0, 6]])]
    flat = structural_prior(L, 3, 5, 'flat', regularize_eaves=False)
    evidence = bundle([], observations)
    rows = guidance._valleys(parts, evidence, candidate_id='wing', baseline_parts=flat)
    assert len(rows) == 2 and all(row['status'] == 'valley_supported' and row['potential_missing_valley'] for row in rows)
    assert all(row['kind_source'] == 'concave_adjoining_model_planes' for row in rows)
    assert guidance._valleys(flat, evidence, candidate_id='flat', baseline_parts=flat) == []
    ambiguous = guidance._contradictions(flat, evidence, [])
    assert all(row['kind'] == 'ambiguous_image_crease' for row in ambiguous)


def test_zero_evidence_and_clipped_source_remain_uncertain():
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    prior = structural_prior(ROOF, 3, 5, 'hipped')
    clipped_frame = [[20, 0], [0, -20], [0, 200]]
    result, report, selected = guidance.guide_roof(prior, ROOF, 3, image, clipped_frame, [],
                                                  baseline_detail={'roof_type': 'hipped'})
    assert result is prior and selected is None and not report['accepted']
    assert report['reason'] == 'clipped-source-needs-context'
    assert report['before']['original_mismatch_m'] is report['before']['combined_mismatch_m'] is None


def test_default_guidance_and_explicit_ablation_keep_a_safe_baseline_on_failure(monkeypatch):
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    ring = list(ROOF.exterior.coords[:-1])
    baseline = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME, optical_guidance=False)
    assert 'optical_guidance' not in baseline

    def fail(*args, **kwargs):
        raise ValueError('candidate numerical failure')

    monkeypatch.setattr(guidance, 'guide_roof', fail)
    default = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    assert default['annotation'] == baseline['annotation'] and default['lines'] == baseline['lines']
    assert default['optical_guidance']['reason'] == 'guidance-unavailable-baseline-retained'
    with pytest.raises(ValueError, match='boolean'):
        propose_roof(ring, 3, 2, 'auto', optical_guidance='yes')
