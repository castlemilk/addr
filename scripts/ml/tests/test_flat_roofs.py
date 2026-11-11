"""Original-RGB family evidence, explicit uncertainty, and exact-outline surfaces."""
import cv2
import numpy as np
import pytest
from PIL import Image
from scripts.ml.roof_form import FLAT_RULES, FLAT_VERSION, flat_evidence
from scripts.ml.roof_structure import structural_prior
from scripts.ml.scene_roof import propose_roof
from shapely.geometry import Polygon

SCALE = 4.24                                  # px per metre, as the saved Mooroolbark scenes
FRAME = [[SCALE, 0], [0, -SCALE], [40, 480]]  # local metres -> pixels (y up in metres, down in pixels)
SIZE = (512, 512)


def _pixels(points):
    return (np.c_[np.asarray(points, float), np.ones(len(points))] @ np.asarray(FRAME)).round().astype(np.int32)


def _canvas(background=70):
    return np.full((SIZE[1], SIZE[0], 3), background, np.uint8)


def _fill(canvas, ring, value):
    cv2.fillPoly(canvas, [_pixels(ring)], (int(value),) * 3)


def _equipment(canvas, boxes, value=110):
    for x, y, w, h in boxes:
        _fill(canvas, [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], value)


def _image(canvas):
    return Image.fromarray(canvas)


def flat_rectangle(width=30, depth=12, brightness=205):
    ring = [[0, 0], [width, 0], [width, depth], [0, depth]]
    canvas = _canvas()
    _fill(canvas, ring, brightness)
    _equipment(canvas, [(4, 3, 1.6, 1.6), (9, 7, 2.2, 1.4), (18, 4, 1.5, 2.5), (24, 8, 1.6, 1.6)])
    return ring, _image(canvas)


def gable_rectangle(width=30, depth=12):
    ring = [[0, 0], [width, 0], [width, depth], [0, depth]]
    canvas = _canvas()
    _fill(canvas, [[0, 0], [width, 0], [width, depth / 2], [0, depth / 2]], 205)
    _fill(canvas, [[0, depth / 2], [width, depth / 2], [width, depth], [0, depth]], 140)
    return ring, _image(canvas)


def l_shaped_hip():
    ring = [[0, 0], [24, 0], [24, 10], [12, 10], [12, 20], [0, 20]]
    parts = structural_prior(Polygon(ring), 3, 5, 'hipped', regularize_eaves=False)
    canvas = _canvas()
    for part in parts:   # each slope lit by its facing direction, as directional sun does
        a, b = part['plane']['a'], part['plane']['b']
        norm = max(1e-9, float(np.hypot(a, b)))
        value = 170 + 35 * a / norm - 25 * b / norm
        _fill(canvas, part['coords'], np.clip(value, 60, 250))
    return ring, _image(canvas)


def shed(width=10, depth=6):
    ring = [[0, 0], [width, 0], [width, depth], [0, depth]]
    canvas = _canvas()
    _fill(canvas, ring, 210)
    return ring, _image(canvas)


def _prior(ring):
    return structural_prior(Polygon(ring), 3, 5, 'hipped', regularize_eaves=False)


def test_bright_uniform_rectangle_with_axis_aligned_equipment_is_flat():
    ring, image = flat_rectangle()
    evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    assert evidence['version'] == FLAT_VERSION == 'roof-form-flat-v3'
    assert evidence['decision'] == 'flat'
    assert evidence['score'] >= .8
    features = evidence['features']
    assert features['area_m2'] == pytest.approx(360)
    assert features['rectilinearity'] == pytest.approx(1, abs=.02)
    assert features['right_angle_share'] == 1
    assert features['interior_line_axis_share'] is None or features['interior_line_axis_share'] >= .7
    assert features['brightness_median'] >= 170
    assert features['pitched_crease_contrast_share'] <= .45
    assert evidence['reasons'] and all(r[0].isupper() for r in evidence['reasons'])


def test_two_tone_gable_with_a_central_ridge_stays_pitched():
    ring, image = gable_rectangle()
    evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    assert evidence['decision'] != 'flat'
    assert evidence['features']['uniformity'] > 15


def test_l_shaped_house_texture_stays_pitched():
    ring, image = l_shaped_hip()
    evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    assert evidence['decision'] != 'flat'
    assert evidence['features']['area_m2'] == pytest.approx(360)
    assert evidence['features']['rectilinearity'] < .8


def test_small_bright_uniform_shed_gets_only_a_comparison_at_the_area_gate():
    ring, image = shed()
    evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    assert evidence['decision'] == 'uncertain' and evidence['flat_low_slope_preview']
    assert evidence['representation'] == 'pitched_prior'
    assert evidence['features']['area_m2'] == pytest.approx(60)
    assert any('area' in r.lower() for r in evidence['reasons'])
    proposal = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    assert proposal['roof_type'] != 'flat' and proposal['form_uncertain']
    comparison = proposal['form_comparison']
    assert not comparison['alternative_selected'] and comparison['alternative']['audit']['passed']
    assert comparison['alternative']['roof_type'] == 'flat' and not comparison['alternative']['lines']
    assert proposal['optical_guidance']['reason'] == 'roof-family-unresolved'
    assert not proposal['optical_guidance']['valley_candidates']


def test_flat_evidence_works_without_a_pitched_hypothesis_and_records_the_gap():
    ring, image = flat_rectangle()
    evidence = flat_evidence(Polygon(ring), image, FRAME)
    assert evidence['features']['pitched_crease_contrast_share'] is None
    assert evidence['decision'] in ('flat', 'pitched', 'uncertain')
    assert evidence['version'] == FLAT_VERSION


def test_auto_proposal_uses_the_flat_template_when_evidence_fires():
    ring, image = flat_rectangle()
    result = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    template = propose_roof(ring, 3, 2, 'flat')
    assert result['roof_type'] == 'flat'
    assert result['source'] == 'image-guided-prior'
    assert result['annotation'] == template['annotation']
    assert result['parts'] == template['parts']
    assert result['audit']['passed'] and result['audit'].get('drainage') in (None, {}) or 'drainage' not in result['audit'] or result['audit']['drainage'].get('status') != 'passed'
    assert result['form_evidence']['decision'] == 'flat'
    assert result['form_evidence']['version'] == FLAT_VERSION
    assert result['form_uncertain'] is (result['form_evidence']['score'] < .8)
    assert result['note'].startswith('Flat roof: ') and 'heights remain estimates' in result['note']
    assert result['line_refinement'] is None and result['topology'] is None


def test_auto_proposal_is_unchanged_when_evidence_does_not_fire(monkeypatch):
    from scripts.ml import scene_roof
    ring, image = gable_rectangle()
    result = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    assert result['roof_type'] != 'flat'
    assert result['form_evidence']['decision'] != 'flat'
    # Forcing the evidence to 'pitched' must leave the pitched path byte-identical.
    monkeypatch.setattr(scene_roof, 'flat_evidence', lambda *a, **k: {'decision': 'pitched', 'score': 0, 'features': {}, 'reasons': [], 'version': FLAT_VERSION})
    forced = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    strip = lambda r: {k: v for k, v in r.items() if k != 'form_evidence'}  # noqa: E731
    assert strip(forced) == strip(result)


def test_reviewer_templates_do_not_carry_flat_evidence():
    ring, _ = flat_rectangle()
    assert propose_roof(ring, 3, 2, 'hipped')['form_evidence'] is None


# --- ridge veto, wing directions, very-large decks, informational decisions ----------------------

def long_gable(width=40, depth=12, left=205, right=193):
    """A bright, nearly uniform long roof whose two halves differ by a faint step: a gable ridge, not a flat deck."""
    ring = [[0, 0], [width, 0], [width, depth], [0, depth]]
    canvas = _canvas()
    _fill(canvas, [[0, 0], [width, 0], [width, depth / 2], [0, depth / 2]], left)
    _fill(canvas, [[0, depth / 2], [width, depth / 2], [width, depth], [0, depth]], right)
    return ring, _image(canvas)


def test_faint_centred_ridge_run_vetoes_a_flat_decision_on_a_bright_uniform_long_roof():
    ring, image = long_gable()
    evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    features = evidence['features']
    assert features['brightness_median'] >= 170 and features['uniformity'] <= 10, 'texture alone looks flat'
    assert features['ridge_run_share'] >= .35 and features['ridge_run_centred'] >= .8
    assert evidence['decision'] == 'pitched'
    assert any('ridge run' in r for r in evidence['reasons'])


def rotated_l_flat(angle_deg=20):
    from shapely.affinity import rotate
    shape = rotate(Polygon([[0, 0], [30, 0], [30, 10], [12, 10], [12, 24], [0, 24]]), angle_deg, origin=(15, 12))
    ring = [list(p) for p in shape.exterior.coords[:-1]]
    canvas = _canvas()
    _fill(canvas, ring, 210)
    for box in ([[3, 3], [5, 3], [5, 4.6], [3, 4.6]], [[20, 4], [22.4, 4], [22.4, 6], [20, 6]], [[4, 16], [6, 16], [6, 18.5], [4, 18.5]]):
        _fill(canvas, [list(p) for p in rotate(Polygon(box), angle_deg, origin=(15, 12)).exterior.coords[:-1]], 120)
    return ring, _image(canvas)


def test_rotated_l_shaped_deck_is_flat_through_outline_directions_not_bounding_rectangle_fill():
    ring, image = rotated_l_flat()
    evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    features = evidence['features']
    assert features['rectilinearity'] < .8, 'an L-shape never fills its bounding rectangle'
    assert features['outline_axis_share'] >= .95 and len(features['outline_directions_deg']) == 1
    assert evidence['decision'] == 'flat' and evidence['rule'] == 'large'


def sectioned_deck(width, depth):
    """Two membrane sections of different brightness plus equipment: a multi-section commercial deck."""
    ring = [[0, 0], [width, 0], [width, depth], [0, depth]]
    canvas = _canvas()
    _fill(canvas, [[0, 0], [width * .45, 0], [width * .45, depth], [0, depth]], 215)
    _fill(canvas, [[width * .45, 0], [width, 0], [width, depth], [width * .45, depth]], 185)
    _equipment(canvas, [(4, 3, 2, 2), (width * .6, depth * .3, 2.4, 1.6), (width * .8, depth * .7, 1.8, 1.8)])
    return ring, _image(canvas)


def test_sectioned_deck_keeps_the_pitched_path_at_any_scale_because_sections_read_as_lit_planes():
    """Membrane sections and pitched facets both show as a per-plane brightness spread; without a way to tell them
    apart on the saved image, the conservative choice is the unchanged pitched path, even at 2,400 m²."""
    for width, depth, area in ((60, 40, 2400), (30, 20, 600)):
        ring, image = sectioned_deck(width, depth)
        evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
        assert evidence['features']['area_m2'] == pytest.approx(area)
        assert evidence['features']['pitched_plane_l_spread'] > 12
        assert evidence['decision'] != 'flat'
        assert any('planes differ in brightness' in r for r in evidence['reasons'])
    assert 'very_large' not in evidence['rules'], 'no size-only exemption from the plane-spread gate'


def test_uncertain_evidence_marks_family_without_altering_pitched_geometry(monkeypatch):
    from scripts.ml import scene_roof
    ring, image = gable_rectangle()
    result = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    monkeypatch.setattr(scene_roof, 'flat_evidence', lambda *a, **k: {'decision': 'uncertain', 'score': .77, 'features': {}, 'reasons': [], 'version': FLAT_VERSION})
    forced = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    assert all(forced[key] == result[key] for key in ('annotation', 'parts', 'lines', 'roof_type'))
    assert forced['form_evidence']['decision'] == 'uncertain' and forced['roof_type'] != 'flat'
    assert forced['form_uncertain']


def test_equipment_halo_is_shared_by_plane_brightness_measurements():
    from scripts.ml.roof_form import _flat_frame, _flat_mask, _plane_spread, _texture_samples

    polygon = Polygon([[0, 0], [12, 0], [12, 8], [0, 8]])
    canvas = _canvas()
    _fill(canvas, list(polygon.exterior.coords), 205)
    _equipment(canvas, [(1.2, 1.5, 1.6, 5)], value=110)
    gray = cv2.cvtColor(canvas, cv2.COLOR_RGB2GRAY)
    lightness = cv2.cvtColor(canvas, cv2.COLOR_RGB2LAB)[:, :, 0].astype(float)
    frame = _flat_frame(FRAME)
    interior = _flat_mask(polygon.buffer(-1), frame, gray.shape)
    _, _, blobs, material, _ = _texture_samples(gray, interior, frame, FLAT_RULES)
    parts = [{'coords': [[x, 1], [x+2, 1], [x+2, 7], [x, 7]]} for x in (1, 5, 8)]
    raw, raw_count = _plane_spread(parts, interior, frame, lightness, polygon, FLAT_RULES)
    clean, clean_count = _plane_spread(parts, material, frame, lightness, polygon, FLAT_RULES)
    assert blobs == 1 and raw_count == 3 and raw > 50
    assert clean_count == 2 and clean == 0


def test_crease_samples_exclude_background_and_holes_and_report_unavailable():
    from scripts.ml.roof_form import _flat_frame, _flat_mask, _pitched_crease_share

    ring = [[0, 0], [12, 0], [12, 8], [0, 8]]
    polygon = Polygon(ring, [[[2, 2], [4, 2], [4, 5], [2, 5]]])
    frame = _flat_frame(FRAME)
    mask = _flat_mask(polygon, frame, (SIZE[1], SIZE[0]))
    lightness = np.full(mask.shape, 15.)
    lightness[mask] = 205
    details = {}
    share, measured = _pitched_crease_share(_prior(ring), frame, lightness, FLAT_RULES,
                                           sample_mask=mask, diagnostics=details)
    assert measured > 0 and share == 0
    assert 0 < details['valid_roof_sample_pairs'] < details['possible_sample_pairs']
    share, measured = _pitched_crease_share(_prior(ring), frame, lightness, FLAT_RULES,
                                           sample_mask=np.zeros_like(mask), diagnostics=details)
    assert share is None and measured == details['valid_roof_sample_pairs'] == 0


def test_complementary_rule_gap_uses_uncertain_coplanar_surface_with_holes(monkeypatch):
    from scripts.ml import roof_form
    from shapely.ops import unary_union

    ring, image = flat_rectangle(width=20, depth=10)
    holes = [[[8, 4], [10, 4], [10, 6], [8, 6]]]
    between_rules = (FLAT_RULES['large']['plane_spread']+FLAT_RULES['bright']['plane_spread'])/2
    monkeypatch.setattr(roof_form, '_plane_spread', lambda *args: (between_rules, 4))
    proposal = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME, holes=holes)
    assert proposal['form_evidence']['representation'] == 'unresolved_coplanar'
    assert proposal['form_evidence']['decision'] == 'uncertain' and proposal['form_uncertain']
    assert proposal['roof_type'] == 'flat' and not proposal['lines'] and not proposal['annotation']['edges']
    assert proposal['audit']['passed']
    surface = unary_union([Polygon(part['ring_meters']) for part in proposal['parts']])
    assert surface.symmetric_difference(Polygon(ring, holes)).area < 1e-8
    assert proposal['optical_guidance']['reason'] == 'roof-family-unresolved-coplanar'


def test_flat_evidence_output_is_plain_json_with_denominators():
    ring, image = flat_rectangle()
    evidence = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    import json
    json.dumps(evidence)   # no numpy scalars or arrays leak into the persisted payload
    assert evidence['rules']['large']['area_m2'] == 250 and evidence['rules']['bright']['area_m2'] == 120
    assert evidence['note'] == 'Model estimate on the saved image, not accuracy or a label.'
    assert any(' of ' in r for r in evidence['reasons']), 'counts carry their denominators'


# --- adversarial-review regressions: unseen pixels are not evidence; payload copy --------------------------

def test_roof_cut_by_the_image_edge_keeps_the_pitched_path():
    """Half the deck lies off the image: the visible half reads flat, but unseen pixels are not evidence."""
    ring = [[0, 0], [30, 0], [30, 12], [0, 12]]
    cut_frame = [[SCALE, 0], [0, -SCALE], [SIZE[0] - 15 * SCALE, 480]]     # x = 15 m sits on the right image edge
    canvas = _canvas()
    cv2.fillPoly(canvas, [(np.c_[np.asarray(ring, float), np.ones(4)] @ np.asarray(cut_frame)).round().astype(np.int32)], (205,) * 3)
    evidence = flat_evidence(Polygon(ring), _image(canvas), cut_frame, pitched_parts=_prior(ring))
    assert .4 <= evidence['features']['interior_visible_share'] <= .6
    assert evidence['decision'] == 'pitched'
    assert any('inside the image' in r for r in evidence['reasons'])
    assert evidence['rules']['min_visible_share'] == .9
    result = propose_roof(ring, 3, 2, 'auto', image=_image(canvas), meters_to_pixel=cut_frame)
    assert result['roof_type'] != 'flat' and result['form_evidence']['decision'] == 'pitched'
    # The same deck fully inside the image is measured completely and is flat.
    full = flat_evidence(*flat_rectangle(), FRAME, pitched_parts=_prior(ring))
    assert full['features']['interior_visible_share'] >= .95 and full['decision'] == 'flat'


def test_flat_evidence_emits_no_runtime_warnings():
    import warnings
    ring, image = flat_rectangle()
    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))


def test_flat_reasons_spell_out_an_unmeasured_plane_spread():
    from scripts.ml.roof_form import FLAT_RULES, _flat_reasons
    features = {'area_m2': 400.0, 'rectilinearity': 1.0, 'outline_directions_deg': [0.0], 'outline_axis_share': 1.0,
                'interior_line_axis_share': None, 'interior_axis_line_count': 0, 'interior_line_count': 0, 'diagonal_line_length_m': 0.0,
                'pitched_crease_contrast_share': 0.0, 'pitched_crease_lines_measured': 4, 'pitched_plane_l_spread': None, 'pitched_planes_measured': 1,
                'brightness_median': 210.0, 'uniformity': 3.0, 'equipment_blob_count': 0, 'ridge_run_share': 0.0}
    reasons = _flat_reasons(features, 'flat', 'large', FLAT_RULES, [])
    assert any('plane brightness spread not measured' in r for r in reasons)
    assert all(r[0].isupper() for r in reasons) and 'None' not in ' '.join(reasons)


WAIVING = {**FLAT_RULES, 'context': {**FLAT_RULES['context'], 'waived_gates': ['planes']}}   # the evaluated, not adopted, variant


def test_default_rules_record_a_commercial_hint_but_waive_nothing():
    """flat-v2 default: the hint is recorded in the evidence and the decision is the image-only one."""
    ring, image = sectioned_deck(60, 40)
    plain = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    hinted = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='commercial')
    assert FLAT_RULES['context']['waived_gates'] == []
    assert plain['decision'] == hinted['decision'] != 'flat'
    assert plain['context'] == {'class_hint': None, 'recognised': False, 'source': 'building-flags-v2', 'waivable_gates': [], 'waived_failed_gates': [], 'withheld': None, 'structure_flat': False}
    assert hinted['context'] == {'class_hint': 'commercial', 'recognised': True, 'source': 'building-flags-v2', 'waivable_gates': [], 'waived_failed_gates': [], 'withheld': None, 'structure_flat': False}
    assert hinted['features']['context_hint'] == 'commercial'
    same = {k: v for k, v in hinted['features'].items() if k != 'context_hint'}
    assert same == {k: v for k, v in plain['features'].items() if k != 'context_hint'}


def test_a_configured_waiver_lifts_only_the_named_gate_for_recognised_hints():
    """The mechanism under test with an explicit rule set: a 'commercial' hint waives the plane-spread gate and the
    2,400 m² sectioned deck becomes flat with the waiver recorded; other gates and other hints are untouched."""
    ring, image = sectioned_deck(60, 40)
    hinted = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='commercial', rules=WAIVING)
    assert hinted['decision'] == 'flat' and hinted['rule'] == 'large'
    assert hinted['context']['recognised'] and hinted['context']['waived_failed_gates'] == ['planes'] and hinted['context']['waivable_gates'] == ['planes']
    assert any("hint 'commercial'" in r and 'waives the planes gate' in r for r in hinted['reasons'])
    for hint in ('house', 'non-residential', 'units', 'outbuilding', 'tree-like', 'banana', ''):
        other = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint=hint, rules=WAIVING)
        assert other['decision'] != 'flat' and other['context']['recognised'] is False and other['context']['waived_failed_gates'] == []


def test_a_configured_waiver_never_overrides_the_ridge_veto_or_the_other_gates():
    ring, image = long_gable()          # bright, uniform, a faint centred ridge run: the veto must hold
    hinted = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='commercial', rules=WAIVING)
    assert hinted['decision'] == 'pitched' and any('ridge run' in r for r in hinted['reasons'])
    ring, image = gable_rectangle()     # two-tone gable: crease contrast and planes both fail; only planes is waivable
    assert flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='commercial', rules=WAIVING)['decision'] != 'flat'
    ring, image = l_shaped_hip()        # lit hip planes: diagonal interior lines and crease contrast still block
    assert flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='commercial', rules=WAIVING)['decision'] != 'flat'


def test_propose_roof_forwards_the_hint_and_records_it_without_changing_the_default_decision():
    ring, image = sectioned_deck(60, 40)
    plain = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME)
    hinted = propose_roof(ring, 3, 2, 'auto', image=image, meters_to_pixel=FRAME, context_hint='commercial')
    strip = lambda r: {k: v for k, v in r.items() if k != 'form_evidence'}  # noqa: E731
    assert strip(hinted) == strip(plain) and plain['roof_type'] != 'flat'
    assert plain['form_evidence']['context']['class_hint'] is None
    assert hinted['form_evidence']['context'] == {'class_hint': 'commercial', 'recognised': True, 'source': 'building-flags-v2',
                                                  'waivable_gates': [], 'waived_failed_gates': [], 'withheld': None, 'structure_flat': False}


def test_equipment_blob_halo_removes_blob_edges_from_the_texture_measure():
    """A blob's soft 0.5 m edge deviates less than the blob threshold: it is texture without the halo (a quarter of the
    interior, so it sets the 10th percentile) and removed with it."""
    from scripts.ml.roof_form import FLAT_RULES, _flat_frame, _flat_mask, _texture
    ring = [[0, 0], [12, 0], [12, 8], [0, 8]]
    canvas = _canvas()
    _fill(canvas, ring, 205)
    for x, y in ((2, 2), (5, 3), (8, 2)):
        _fill(canvas, [[x - .5, y - .5], [x + 2.5, y - .5], [x + 2.5, y + 2.5], [x - .5, y + 2.5]], 178)   # 27 L edge: below the 35 blob threshold
        _fill(canvas, [[x, y], [x + 2, y], [x + 2, y + 2], [x, y + 2]], 110)                               # the 4 m² blob itself
    gray = cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2GRAY)
    frame = _flat_frame(FRAME)
    mask = _flat_mask(Polygon(ring).buffer(-1), frame, gray.shape).astype(bool)
    _, with_halo, blobs = _texture(gray, mask, frame, FLAT_RULES)
    _, without_halo, _ = _texture(gray, mask, frame, {**FLAT_RULES, 'blob_halo_m': 0})
    assert blobs == 3 and without_halo > 8, 'without the halo the soft edges read as roof texture'
    assert with_halo < without_halo and with_halo <= 2, 'a uniform membrane with three haloed blobs reads as uniform'


def test_commercial_hint_is_withheld_on_a_dark_sectioned_roof():
    """Below the 170 median the waiver does not apply: dark lit hip planes and dark membrane sections are indistinguishable."""
    ring = [[0, 0], [60, 0], [60, 40], [0, 40]]
    canvas = _canvas()
    _fill(canvas, [[0, 0], [36, 0], [36, 40], [0, 40]], 168)
    _fill(canvas, [[36, 0], [60, 0], [60, 40], [36, 40]], 152)
    _equipment(canvas, [(4, 3, 2, 2), (36, 12, 2.4, 1.6), (48, 28, 1.8, 1.8)], value=90)
    image = _image(canvas)
    hinted = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='commercial', rules=WAIVING)
    assert 150 <= hinted['features']['brightness_median'] < 170
    assert hinted['context']['recognised'] and hinted['context']['waived_failed_gates'] == []
    assert hinted['context']['withheld'].startswith('median gray') and 'below the 170' in hinted['context']['withheld']
    assert hinted['decision'] != 'flat'


def ribbed_box(width=2.4, length=12.0):
    """A shipping container: a small ribbed grey box on grass, far too small and dark for the flat rules."""
    ring = [[0, 0], [length, 0], [length, width], [0, width]]
    canvas = _canvas()
    _fill(canvas, ring, 150)
    for x in np.arange(.3, length, .6):
        _fill(canvas, [[x, 0], [x + .25, 0], [x + .25, width], [x, width]], 125)
    return ring, _image(canvas)


def test_structure_hint_selects_the_flat_template_for_containers_and_tanks():
    ring, image = ribbed_box()
    plain = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring))
    assert plain['decision'] != 'flat', 'image rules alone never call a 29 m² box flat'
    hinted = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='shipping-container')
    assert hinted['decision'] == 'flat' and hinted['rule'] == 'structure' and hinted['context']['structure_flat'] is True
    assert any("Structure hint 'shipping-container'" in r and 'flat top' in r for r in hinted['reasons'])
    assert flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='shed')['decision'] != 'flat', 'sheds keep the image path'
    proposal = propose_roof(ring, 2.6, 2, 'auto', image=image, meters_to_pixel=FRAME, context_hint='shipping-container')
    assert proposal['roof_type'] == 'flat' and proposal['form_evidence']['rule'] == 'structure'


def test_structure_hint_never_overrides_the_ridge_veto():
    ring, image = long_gable()
    hinted = flat_evidence(Polygon(ring), image, FRAME, pitched_parts=_prior(ring), context_hint='shipping-container')
    assert hinted['decision'] == 'pitched' and hinted['context']['structure_flat'] is False
