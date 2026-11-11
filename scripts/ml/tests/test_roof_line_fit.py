import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw
from scripts.ml import roof_line_fit
from scripts.ml.roof_line_fit import enhance_roof_detail, image_segments, refine_roof_lines
from scripts.ml.roof_structure import audit_roof, structural_prior
from shapely.geometry import Polygon
from shapely.ops import unary_union

FRAME = np.array([[20., 0.], [0., -20.], [20., 200.]])
ROOF = Polygon([[0, 0], [12, 0], [12, 8], [0, 8]])


def roof_image():
    image = Image.new('RGB', (280, 240), (45, 95, 40))
    draw = ImageDraw.Draw(image)
    faces = [([[0, 0], [12, 0], [8, 5], [4, 5]], (140, 130, 120)),
             ([[12, 0], [12, 8], [8, 5]], (150, 140, 130)),
             ([[12, 8], [0, 8], [4, 5], [8, 5]], (170, 160, 150)),
             ([[0, 8], [0, 0], [4, 5]], (155, 145, 135))]
    for ring, color in faces:
        points = np.c_[ring, np.ones(len(ring))] @ FRAME
        draw.polygon([tuple(p) for p in points], fill=color)
    return image


def test_contrast_view_keeps_pixels_registered_and_source_immutable():
    image = roof_image()
    original = image.tobytes()
    view = enhance_roof_detail(image)
    assert view.shape == (image.height, image.width)
    assert view.dtype == np.uint8
    assert image.tobytes() == original
    assert not np.array_equal(view, np.array(image.convert('L')))


@pytest.mark.parametrize('reverse', [False, True])
def test_observed_offset_ridge_moves_with_connected_planar_faces(reverse):
    footprint = Polygon(list(ROOF.exterior.coords)[::-1]) if reverse else ROOF
    prior = structural_prior(footprint, 3, 5, 'hipped', regularize_eaves=False)
    original = copy.deepcopy(prior)
    fitted, report = refine_roof_lines(prior, footprint, 3, roof_image(), FRAME)
    assert report['accepted'], report
    assert report['weighted_residual_after_m'] < report['weighted_residual_before_m']*.5
    assert prior == original
    apexes = [xy for part in fitted for xy, z in zip(part['coords'], part['ring_z'], strict=True) if z > 4.5]
    assert apexes and abs(np.mean([p[1] for p in apexes])-5) < .25
    assert audit_roof(fitted, footprint, 3)['passed']
    coverage = unary_union([Polygon(p['coords']) for p in fitted])
    assert coverage.symmetric_difference(footprint).area < 1e-6
    for part in fitted:
        for xy, z in zip(part['coords'], part['ring_z'], strict=True):
            if any(np.linalg.norm(np.array(xy)-corner) < 1e-6 for corner in footprint.exterior.coords):
                assert z == 3, 'Following image lines must not tilt the eave corners'


@pytest.mark.parametrize('colour', [(130, 130, 130), (35, 95, 40)])
def test_uniform_or_vegetation_imagery_cannot_move_roof_points(colour):
    prior = structural_prior(ROOF, 3, 5, 'hipped')
    fitted, report = refine_roof_lines(prior, ROOF, 3, Image.new('RGB', (280, 240), colour), FRAME)
    assert fitted is prior and not report['accepted']


def test_gable_ridge_apex_moves_only_along_the_saved_boundary():
    prior = structural_prior(ROOF, 3, 5, 'gabled')
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    ImageDraw.Draw(image).rectangle((0, 0, 279, 99), fill=(170, 160, 150))
    fitted, report = refine_roof_lines(prior, ROOF, 3, image, FRAME)
    assert report['accepted'], report
    apexes = [xy for part in fitted for xy, z in zip(part['coords'], part['ring_z'], strict=True) if z > 4.5]
    assert all(abs(p[0]) < 1e-8 or abs(p[0]-12) < 1e-8 for p in apexes)
    assert abs(np.mean([p[1] for p in apexes])-5) < .25
    assert audit_roof(fitted, ROOF, 3)['passed']


def test_invalid_registration_rejected():
    with pytest.raises(ValueError, match='registered'):
        image_segments(roof_image(), ROOF, np.zeros((3, 2)))


def test_contrast_enhanced_vegetation_edge_is_not_a_roof_line():
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    ImageDraw.Draw(image).rectangle((0, 0, 279, 99), fill=(35, 110, 40))
    assert not image_segments(image, ROOF, FRAME)


def test_repeated_tile_lines_are_not_independent_ridges():
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    draw = ImageDraw.Draw(image)
    for y in range(50, 191, 8):
        draw.line([(25, y), (255, y)], fill=(100, 90, 80), width=2)
    prior = structural_prior(ROOF, 3, 5, 'hipped')
    fitted, report = refine_roof_lines(prior, ROOF, 3, image, FRAME)
    assert fitted is prior and not report['accepted']


def test_failed_joint_fit_retains_the_audited_prior(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError('Unstable numerical fit')
    monkeypatch.setattr(roof_line_fit, 'least_squares', fail)
    prior = structural_prior(ROOF, 3, 5, 'hipped')
    fitted, report = refine_roof_lines(prior, ROOF, 3, roof_image(), FRAME)
    assert fitted is prior and report['reason'] == 'unstable-fit'


def test_t_junction_shared_vertices_move_together_and_plane_fragments_stay_tied():
    from scripts.ml.roof_wings import wing_candidates
    footprint = Polygon([(0, 0), (24, 0), (24, 6), (15, 6), (15, 16), (9, 16), (9, 6), (0, 6)])
    prior, _ = wing_candidates(footprint, 3, 2)[0]
    frame = np.array([[15., 0.], [0., -15.], [20., 300.]])
    image = Image.new('RGB', (400, 330), (140, 130, 120))
    # Observe the main ridge offset slightly from the symmetric starting shape.
    ImageDraw.Draw(image).rectangle((0, 0, 399, 249), fill=(175, 165, 155))
    fitted, report = refine_roof_lines(prior, footprint, 3, image, frame)
    assert report['accepted'], report
    assert report['physical_plane_parameters'] == 4 < len(prior)
    assert report['weighted_residual_after_m'] < report['weighted_residual_before_m']
    assert audit_roof(fitted, footprint, 3)['passed']
    assert unary_union([Polygon(p['coords']) for p in fitted]).symmetric_difference(footprint).area < 1e-6
    # No independently moving copy of the branch/main apex may remain.
    vertices, faces, _ = roof_line_fit._graph(fitted)
    shared_apex = [i for i, v in enumerate(vertices) if 10 < v[0] < 14 and 2 < v[1] < 5 and v[2] > 4]
    assert len(shared_apex) == 1
    assert sum(shared_apex[0] in f for f in faces) == 5


def test_coplanar_storage_seam_does_not_become_a_fold_after_image_fitting():
    from scripts.ml.roof_junctions import construction_complexity
    from scripts.ml.roof_structure import _parts_from_faces
    from shapely.geometry import LineString
    from shapely.ops import split
    prior = structural_prior(ROOF, 3, 5, 'gabled')
    faces = []
    for i, part in enumerate(prior):
        polygon = Polygon(part['coords'])
        pieces = list(split(polygon, LineString([(6, -1), (6, 9)])).geoms) if i == 0 else [polygon]
        for p in pieces:
            xy = np.asarray(p.exterior.coords[:-1])
            abc = np.array([part['plane'][k] for k in ('a', 'b', 'c')])
            faces.append((xy, np.c_[xy, np.ones(len(xy))] @ abc))
    prior = _parts_from_faces(faces, 3, 'gabled')
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    ImageDraw.Draw(image).rectangle((0, 0, 279, 99), fill=(170, 160, 150))
    fitted, report = refine_roof_lines(prior, ROOF, 3, image, FRAME)
    assert report['accepted'], report
    assert report['physical_plane_parameters'] == 2
    complexity = construction_complexity(fitted)
    assert complexity['physical_planes'] == 2 and complexity['crease_runs'] == 1
    assert audit_roof(fitted, ROOF, 3)['passed']


def test_clipped_slope_corner_height_is_estimated_while_level_eaves_stay_fixed():
    from scripts.ml.roof_topology import clip_support
    footprint = Polygon([(0, 0), (12, 0), (12, 8), (2, 8), (2, 6), (0, 6)])
    prior = clip_support(structural_prior(ROOF, 3, 5, 'gabled'), footprint, 3, 'gabled')
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    ImageDraw.Draw(image).rectangle((0, 0, 279, 99), fill=(170, 160, 150))
    fitted, report = refine_roof_lines(prior, footprint, 3, image, FRAME)
    assert report['accepted'], report
    before = {tuple(xy): z for p in prior for xy, z in zip(p['coords'], p['ring_z'], strict=True)}
    after = {tuple(xy): z for p in fitted for xy, z in zip(p['coords'], p['ring_z'], strict=True)}
    assert abs(after[(2., 6.)]-before[(2., 6.)]) > .15
    for corner in footprint.exterior.coords:
        if before[corner] == 3:
            assert after[corner] == 3
    assert audit_roof(fitted, footprint, 3)['passed']
    assert unary_union([Polygon(p['coords']) for p in fitted]).symmetric_difference(footprint).area < 1e-6


@pytest.mark.parametrize('angle', [0, 32, 87])
@pytest.mark.parametrize('reverse', [False, True])
def test_short_skewed_image_fragment_cannot_tilt_or_decenter_a_symmetric_ridge(monkeypatch, angle, reverse):
    from shapely.affinity import rotate
    rotation = np.array([[np.cos(np.deg2rad(angle)), -np.sin(np.deg2rad(angle))],
                         [np.sin(np.deg2rad(angle)), np.cos(np.deg2rad(angle))]])
    footprint = rotate(ROOF, angle, origin=(0, 0))
    if reverse:
        footprint = Polygon(list(footprint.exterior.coords)[::-1])
    prior = structural_prior(footprint, 3, 5, 'gabled', regularize_eaves=False)
    # A short, plausible-looking tile/shadow fragment previously dragged the
    # whole ridge and both end apexes towards its off-centre, skewed line.
    points = np.array([[3., 4.8], [6., 5.1]]) @ rotation.T
    vector = points[1]-points[0]
    tangent = vector/np.linalg.norm(vector)
    observation = {'points': points, 'tangent': tangent, 'normal': np.array([-tangent[1], tangent[0]]),
                   'length': float(np.linalg.norm(vector)), 'support': .8}
    monkeypatch.setattr(roof_line_fit, 'image_segments', lambda *args: [observation])
    fitted, report = refine_roof_lines(prior, footprint, 3, roof_image(), FRAME)
    assert report['ridge_priors']['centered_runs'] == 1
    apexes = np.array([xy for part in fitted for xy, z in zip(part['coords'], part['ring_z'], strict=True) if z > 4.5]) @ rotation
    assert np.max(np.abs(apexes[:, 1]-4.)) < .025
    assert np.ptp(apexes[:, 1]) < .025
    assert audit_roof(fitted, footprint, 3)['passed']


def test_unequal_seed_pitches_are_not_forced_to_the_midpoint(monkeypatch):
    from scripts.ml.roof_structure import _parts_from_faces
    prior = _parts_from_faces([([[0, 0], [12, 0], [12, 5], [0, 5]], [3, 3, 5, 5]),
                              ([[0, 5], [12, 5], [12, 8], [0, 8]], [5, 5, 3, 3])], 3, 'gabled')
    points = np.array([[1., 5.2], [11., 5.2]])
    observation = {'points': points, 'tangent': np.array([1., 0.]), 'normal': np.array([0., 1.]), 'length': 10., 'support': .8}
    monkeypatch.setattr(roof_line_fit, 'image_segments', lambda *args: [observation])
    fitted, report = refine_roof_lines(prior, ROOF, 3, roof_image(), FRAME)
    assert report['ridge_priors']['parallel_runs'] == 0
    assert report['accepted']
    apexes = [xy[1] for p in fitted for xy,z in zip(p['coords'], p['ring_z'], strict=True) if z > 4.5]
    assert min(apexes) > 5.1


@pytest.mark.parametrize('angle', [0, 37, 103])
@pytest.mark.parametrize('reverse', [False, True])
def test_image_evidence_cannot_pull_two_different_creases_together(angle, reverse):
    radians = np.deg2rad(angle)
    rotation = np.array([[np.cos(radians), -np.sin(radians)], [np.sin(radians), np.cos(radians)]])
    xy = np.array([[0, 0], [3, 0], [8, 0], [0, .8], [1.5, .8], [10, 0], [11, 0]]) @ rotation.T
    points = np.array([[.1, 0], [7.9, 0]]) @ rotation.T
    observation = {'points': points, 'tangent': rotation[:, 0], 'normal': rotation[:, 1], 'length': 7.8, 'support': .9}
    # A subdivided continuous crease owns the evidence. Neither the nearby
    # parallel branch nor a disconnected collinear segment may reuse it.
    matches = [(0, 1, observation), (1, 2, observation), (3, 4, observation), (5, 6, observation)]
    if reverse:
        matches = [(b, a, q) for a, b, q in matches[::-1]]
    selected = roof_line_fit._independent_matches(matches, np.c_[xy, np.ones(len(xy))])
    assert {tuple(sorted(m[:2])) for m in selected} == {(0, 1), (1, 2)}


def test_distinct_image_features_can_support_neighbouring_creases():
    xy = np.array([[0, 0], [6, 0], [0, 1], [6, 1]])
    observations = [{'points': xy[[a, b]], 'tangent': np.array([1., 0.]), 'normal': np.array([0., 1.]),
                     'length': 6., 'support': .8} for a, b in [(0, 1), (2, 3)]]
    matches = [(0, 1, observations[0]), (2, 3, observations[1])]
    assert roof_line_fit._independent_matches(matches, xy) == matches


def test_short_hip_end_uses_its_own_image_evidence_without_changing_outline(monkeypatch):
    fixture = json.loads((Path(__file__).parent/'fixtures/independent-crease-evidence.json').read_text())
    observations = [{k: np.array(v) if isinstance(v, list) else v for k, v in row.items()} for row in fixture['observations']]
    monkeypatch.setattr(roof_line_fit, 'image_segments', lambda *args: observations)
    prior = fixture['parts']
    original = copy.deepcopy(prior)
    footprint = Polygon(fixture['ring_meters'])
    fitted, report = refine_roof_lines(prior, footprint, fixture['eave'], Image.new('RGB', (512, 512)), FRAME)
    assert report['accepted'] and report['conflicting_matches_removed'] == 1
    assert prior == original
    # Endpoint of an independently detected hip segment, not a reviewer point.
    cap_endpoint = observations[7]['points'][0]
    apexes = [xy for part in fitted for xy, z in zip(part['coords'], part['ring_z'], strict=True) if z > 3.5]
    assert min(np.linalg.norm(np.array(xy)-cap_endpoint) for xy in apexes) < .3
    assert report['max_shift_m'] < 1.
    assert audit_roof(fitted, footprint, fixture['eave'])['passed']
    assert report['geometry_audit']['drainage']['passed']
    assert unary_union([Polygon(p['coords']) for p in fitted]).symmetric_difference(footprint).area < 1e-6


# --- CP3 extra evidence views (default off; original-RGB gate intact) -----------------------

def _segment_rows(segments):
    return [{k: (np.round(v, 6).tolist() if isinstance(v, np.ndarray) else round(float(v), 6))
             for k, v in s.items()} for s in segments]


def soft_fold_image(width_px=16, step=40, base=(140, 130, 120)):
    """A gradual brightness ramp along y=4 m: real roof contrast for line_evidence, invisible to LSD."""
    pixels = np.full((240, 280, 3), base, np.uint8).astype(float)
    ramp = np.clip((np.arange(240)-(120-width_px/2))/width_px, 0, 1)
    pixels += (ramp*step)[:, None, None]
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def synthetic_view(rows=(120,), columns=None, size=(240, 280)):
    """An HxW uint8 'view' with hard edges: rows -> horizontal steps, columns -> vertical steps."""
    view = np.full(size, 120, np.uint8)
    for row in rows:
        view[row:] = 200 if view[row:].mean() < 160 else 120
    if columns:
        for column in columns:
            view[:, column:] = np.where(view[:, column:] > 160, 60, 220).astype(np.uint8)
    return view


def test_default_image_segments_match_frozen_snapshot_and_carry_no_view_origin():
    fixture = json.loads((Path(__file__).parent/'fixtures/roof-image-segments-default.json').read_text())
    segments = image_segments(roof_image(), ROOF, FRAME)
    assert _segment_rows(segments) == fixture['segments']
    assert all('view_origin' not in s for s in segments)
    assert _segment_rows(image_segments(roof_image(), ROOF, FRAME, extra_views=None)) == fixture['segments']
    assert roof_line_fit.VERSION == 'registered-roof-lines-v6'
    assert roof_line_fit.EXTRA_VIEWS_VERSION != roof_line_fit.VERSION


def test_extra_view_line_supported_by_original_rgb_is_kept_with_its_origin():
    image = soft_fold_image()
    assert image_segments(image, ROOF, FRAME) == [], 'the soft fold is invisible to LSD on gray and CLAHE'
    view = synthetic_view(rows=(120,))
    segments = image_segments(image, ROOF, FRAME, extra_views=[('stack-median', view)])
    assert len(segments) == 1
    found = segments[0]
    assert found['view_origin'] == 'stack-median'
    assert abs(found['points'][:, 1].mean()-4) < .15 and abs(found['tangent'][1]) < .02
    assert found['support'] >= .12
    assert set(found) == {'points', 'tangent', 'normal', 'length', 'support', 'view_origin'}


def test_extra_view_line_without_original_rgb_support_is_dropped_by_the_gate():
    uniform = Image.new('RGB', (280, 240), (140, 130, 120))
    view = synthetic_view(rows=(120,), columns=(140,))
    assert image_segments(uniform, ROOF, FRAME, extra_views=[('stack-median', view)]) == []
    # A supported fold plus an unsupported one: only the fold the saved RGB shows survives.
    segments = image_segments(soft_fold_image(), ROOF, FRAME, extra_views=[('stack-median', view)])
    assert [s['view_origin'] for s in segments] == ['stack-median']
    assert all(abs(s['tangent'][1]) < .02 for s in segments), 'the vertical view-only line was dropped'


def test_extra_views_record_origins_for_existing_views_and_dedupe_against_them():
    image = roof_image()
    default = image_segments(image, ROOF, FRAME)
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    with_views = image_segments(image, ROOF, FRAME, extra_views=[('gray-copy', gray)])
    assert {s['view_origin'] for s in with_views} <= {'gray', 'clahe', 'gray-copy'}
    assert _segment_rows([{k: v for k, v in s.items() if k != 'view_origin'} for s in with_views]) == _segment_rows(default), \
        'A repeated view adds no new evidence: duplicates are removed exactly as CLAHE duplicates are'
    assert all(s['view_origin'] in ('gray', 'clahe') for s in with_views)


@pytest.mark.parametrize('bad', [[('v', np.zeros((10, 10), np.uint8))], [('v', np.zeros((240, 280, 3), np.uint8))],
                                 [('v', np.zeros((240, 280), np.float32))], [np.zeros((240, 280), np.uint8)], ['clahe'],
                                 [('', np.zeros((240, 280), np.uint8))]])
def test_extra_views_must_be_named_uint8_views_on_the_image_grid(bad):
    with pytest.raises(ValueError, match='extra_views'):
        image_segments(roof_image(), ROOF, FRAME, extra_views=bad)


def test_refine_roof_lines_threads_extra_views_and_reports_origins_only_when_used(monkeypatch):
    calls = []
    original = roof_line_fit.image_segments

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(roof_line_fit, 'image_segments', spy)
    prior = structural_prior(ROOF, 3, 5, 'hipped', regularize_eaves=False)
    _, default = refine_roof_lines(prior, ROOF, 3, roof_image(), FRAME)
    assert calls == [{}], 'the default call passes no extra_views keyword at all'
    assert 'view_origins' not in default and 'extra_views' not in default and default['method'] == roof_line_fit.VERSION
    gray = cv2.cvtColor(np.asarray(roof_image().convert('RGB')), cv2.COLOR_RGB2GRAY)
    _, report = refine_roof_lines(prior, ROOF, 3, roof_image(), FRAME, extra_views=[('gray-copy', gray)])
    assert calls[-1] == {'extra_views': [('gray-copy', gray)]}
    assert report['method'] == roof_line_fit.VERSION
    assert report['extra_views'] == {'method': roof_line_fit.EXTRA_VIEWS_VERSION, 'names': ['gray-copy']}
    assert sum(report['view_origins'].values()) == report['detected_segments']
    assert set(report['view_origins']) <= {'gray', 'clahe', 'gray-copy'}


# --- optical-layers-v1 layer evidence (v8; only 'layer:' views may self-evidence) -------------

def test_layer_evidence_version_and_prefix_rules():
    assert roof_line_fit.EXTRA_VIEWS_VERSION == 'registered-roof-lines-v8-layer-evidence'
    assert roof_line_fit.VERSION == 'registered-roof-lines-v6'
    assert roof_line_fit.is_layer_view('layer:bilateral') and not roof_line_fit.is_layer_view('stack-median')
    assert not roof_line_fit.is_layer_view('layer:') and not roof_line_fit.is_layer_view(None)
    assert roof_line_fit.EVIDENCE_GATE == {'min_score': .12, 'min_samples': 6}


def test_view_line_the_original_refuses_passes_only_when_the_view_is_a_declared_layer():
    uniform = Image.new('RGB', (280, 240), (140, 130, 120))
    view = synthetic_view(rows=(120,))
    assert image_segments(uniform, ROOF, FRAME, extra_views=[('stack-median', view)]) == [], 'unprefixed views keep the RGB gate'
    assert image_segments(uniform, ROOF, FRAME, extra_views=[('layer:test', view)], layer_evidence=False) == []
    segments = image_segments(uniform, ROOF, FRAME, extra_views=[('layer:test', view)])
    assert len(segments) == 1
    found = segments[0]
    assert found['view_origin'] == 'layer:test' and found['evidence_layer'] == 'layer:test'
    assert found['support'] >= .12 and abs(found['points'][:, 1].mean()-4) < .15
    assert set(found) == {'points', 'tangent', 'normal', 'length', 'support', 'view_origin', 'evidence_layer'}


def test_layer_evidence_keeps_the_original_gate_for_gray_clahe_and_unprefixed_views():
    image = roof_image()
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    unprefixed = image_segments(image, ROOF, FRAME, extra_views=[('gray-copy', gray)])
    assert unprefixed and all('evidence_layer' not in s for s in unprefixed), 'v7 shape is byte-identical without a layer view'
    mixed = image_segments(image, ROOF, FRAME, extra_views=[('gray-copy', gray), ('layer:gray', gray)])
    assert mixed and all(s['evidence_layer'] == 'original' for s in mixed)
    assert all(s['view_origin'] in ('gray', 'clahe') for s in mixed), 'a repeated view is deduplicated, not re-credited'
    assert _segment_rows([{k: v for k, v in s.items() if k not in ('view_origin', 'evidence_layer')} for s in mixed]) \
        == _segment_rows(image_segments(image, ROOF, FRAME))
    assert all('evidence_layer' not in s and 'view_origin' not in s for s in image_segments(image, ROOF, FRAME))


def test_layer_evidence_cannot_bypass_the_vegetation_veto():
    # Grass on one side of the fold: the original RGB discards every sample, so no layer may vouch for it.
    image = Image.new('RGB', (280, 240), (140, 130, 120))
    ImageDraw.Draw(image).rectangle((0, 0, 279, 119), fill=(35, 110, 40))
    view = synthetic_view(rows=(120,))
    assert image_segments(image, ROOF, FRAME) == []
    assert image_segments(image, ROOF, FRAME, extra_views=[('layer:test', view)]) == []
    # The same fold between two roof colours the original barely separates: the layer may vouch.
    faint = Image.new('RGB', (280, 240), (140, 130, 120))
    ImageDraw.Draw(faint).rectangle((0, 0, 279, 119), fill=(143, 133, 123))
    assert image_segments(faint, ROOF, FRAME) == []
    segments = image_segments(faint, ROOF, FRAME, extra_views=[('layer:test', view)])
    assert [s['evidence_layer'] for s in segments] == ['layer:test']


def test_refine_roof_lines_reports_evidence_layers_only_when_a_layer_view_is_present(monkeypatch):
    calls = []
    original = roof_line_fit.image_segments

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(roof_line_fit, 'image_segments', spy)
    prior = structural_prior(ROOF, 3, 5, 'hipped', regularize_eaves=False)
    image = roof_image()
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    _, plain = refine_roof_lines(prior, ROOF, 3, image, FRAME, extra_views=[('gray-copy', gray)])
    assert calls[-1] == {'extra_views': [('gray-copy', gray)]}
    assert 'evidence_layers' not in plain and 'layer_evidence' not in plain['extra_views']
    view = synthetic_view(rows=(120,))
    _, report = refine_roof_lines(prior, ROOF, 3, image, FRAME, extra_views=[('layer:test', view)])
    assert calls[-1] == {'extra_views': [('layer:test', view)]}
    assert report['extra_views'] == {'method': roof_line_fit.EXTRA_VIEWS_VERSION, 'names': ['layer:test'], 'layer_evidence': ['layer:test']}
    assert sum(report['evidence_layers'].values()) == report['detected_segments'] == sum(report['view_origins'].values())
    assert set(report['evidence_layers']) <= {'original', 'layer:test'}
    _, off = refine_roof_lines(prior, ROOF, 3, image, FRAME, extra_views=[('layer:test', view)], layer_evidence=False)
    assert calls[-1] == {'extra_views': [('layer:test', view)], 'layer_evidence': False}
    assert 'evidence_layers' not in off and 'layer_evidence' not in off['extra_views']
    _, default = refine_roof_lines(prior, ROOF, 3, image, FRAME)
    assert calls[-1] == {} and 'evidence_layers' not in default and 'extra_views' not in default
    assert roof_line_fit.evidence_layer_counts([{'evidence_layer': 'layer:a'}, {}, {'evidence_layer': 'original'}]) == {'layer:a': 1, 'original': 2}
