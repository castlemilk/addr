"""Corner judge J2 and the GSD-aware register / boundary / ladder paths (stream W2).

Synthetic ground: two rectangular roofs rendered at 7.5 cm (A, 480 px) and at 5 cm (B, 720 px) with a 1.5 px Gaussian
edge; B's ground is shifted by a planted (0.12, -0.09) m = 0.15 m. Every figure is agreement between two samplings
of the same (synthetic) ground, never accuracy."""
import hashlib
import json

import numpy as np
import pytest
from PIL import Image
from scipy.special import ndtr
from scripts.ml.training import common
from scripts.ml.training import hires_teacher as ht
from scripts.ml.training import teacher_corners as tc
from scripts.ml.training import teacher_judges as tj
from scripts.ml.training import teacher_sources as ts
from shapely import affinity
from shapely.geometry import Point, Polygon, box

A_ID, B_ID, C_ID = 'linz-chc-2025-0075', 'linz-chc-2025-005', 'linz-chc-2020-0075'
GSD_A, GSD_B = .075, .05
WINDOW_M = 36.
SHIFT_M = (.12, -.09)                                            # A -> B, |shift| = 0.15 m
ROOFS = ((6., 6., 18., 15.), (22.5, 21., 30., 30.))              # x0, y0, x1, y1 in metres from the window's top-left
ROOF1_A_PX = box(80, 80, 240, 200)                               # roof 1 on A's pixel grid (pixel edges at integers)
J1_DEFAULT = dict(tj.J1)


@pytest.fixture(autouse=True)
def _restore_module_state():
    """boundary() mutates J1['peak_tol_m'] and every judge rebinds hires_teacher's source globals: undo both."""
    yield
    tj.J1.clear()
    tj.J1.update(J1_DEFAULT)
    ht.set_source('com-2018')


def render(gsd, shift=(0., 0.), sigma_px=1.5):
    """Grey RGB window: flat ground, bright roofs whose edges are Gaussian-blurred by sigma_px of this grid."""
    n = int(round(WINDOW_M / gsd))
    centres = (np.arange(n) + .5) * gsd                         # pixel c covers [c, c + 1) x gsd: centre at c + 0.5
    sigma = sigma_px * gsd
    image = np.full((n, n), 60.)
    for x0, y0, x1, y1 in ROOFS:
        fx = ndtr((centres - x0 - shift[0]) / sigma) - ndtr((centres - x1 - shift[0]) / sigma)
        fy = ndtr((centres - y0 - shift[1]) / sigma) - ndtr((centres - y1 - shift[1]) / sigma)
        image += 130. * fy[:, None] * fx[None, :]
    grey = np.clip(np.rint(image), 0, 255).astype(np.uint8)
    return np.repeat(grey[..., None], 3, axis=2)


def _teacher_row(index, mask):
    piece = ht.piece_from_mask(mask, score=.9)
    h, w = mask.shape
    return {'index': index, 'score': .9, 'rle_native': ht.rle_encode(piece, h, w),
            'bbox_native': [piece.x0, piece.y0, int(piece.mask.shape[1]), int(piece.mask.shape[0])]}


def _window(root, source_id, gsd, rgb):
    spec = ts.SOURCES[source_id]
    wid = f"{spec['window_prefix']}-r001-c001"
    rel = f"{spec['cache']}/windows/{wid}.png"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(root / rel)
    n = rgb.shape[0]
    return {'id': wid, 'row': 1, 'col': 1, 'native_file': rel, 'shape': [n, n, 3], 'inside_fraction': 1.0,
            'bounds': [1572091.2, 5181323.4 - WINDOW_M, 1572091.2 + WINDOW_M, 5181323.4],
            'native_affine': [1572091.2, gsd, 0., 5181323.4, 0., -gsd]}


def build_world(root, other_id=B_ID, other_gsd=GSD_B):
    """A (7.5 cm) crops + teacher record + outline rows; B crops at other_gsd with the planted shift."""
    a_spec, b_spec = ts.SOURCES[A_ID], ts.SOURCES[other_id]
    window_a = _window(root, A_ID, GSD_A, render(GSD_A))
    n_a = window_a['shape'][0]
    rows = []
    for index, (x0, y0, x1, y1) in enumerate(ROOFS, start=1):
        mask = np.zeros((n_a, n_a), bool)
        mask[int(round(y0 / GSD_A)):int(round(y1 / GSD_A)), int(round(x0 / GSD_A)):int(round(x1 / GSD_A))] = True
        rows.append(_teacher_row(index, mask))
    teacher_a = root / a_spec['out'] / 'teacher'
    (teacher_a / 'windows' / window_a['id']).mkdir(parents=True)
    (teacher_a / 'crops.json').write_text(json.dumps({'native_window_px': n_a, 'gsd_m': GSD_A, 'native_gsd_m': GSD_A,
                                                     'windows': [window_a]}))
    (teacher_a / 'windows' / window_a['id'] / 'record.json').write_text(json.dumps({'window': window_a, 'teacher': rows,
                                                                                    'control': []}))
    judges = teacher_a / 'judges'
    judges.mkdir()
    with open(judges / 'outlines_rows.jsonl', 'w') as handle:
        for row in rows:
            handle.write(json.dumps({'window': window_a['id'], 'arm': 'teacher', 'index': row['index'], 'kind': 'matched'}) + '\n')
    window_b = _window(root, other_id, other_gsd, render(other_gsd, SHIFT_M))
    teacher_b = root / b_spec['out'] / 'teacher'
    teacher_b.mkdir(parents=True)
    (teacher_b / 'crops.json').write_text(json.dumps({'native_window_px': window_b['shape'][0], 'gsd_m': other_gsd,
                                                     'native_gsd_m': other_gsd, 'windows': [window_b]}))
    return {'judges': judges, 'window_a': window_a['id'], 'window_b': window_b['id']}


def listing(root):
    """Every file under root with its sha256: the write-nothing witness."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}


def rows_of(path):
    return [json.loads(line) for line in open(path)]


# --- register ------------------------------------------------------------------------------------------------------

def test_register_resamples_the_finer_capture_onto_the_coarser_grid_and_recovers_the_shift(tmp_path):
    world = build_world(tmp_path)
    result = tj.register(tmp_path, A_ID, B_ID)
    summary = result['summary']
    assert summary['windows'] == 1 and summary['other_gsd_m'] == .05 and summary['scale'] == 1.5
    assert summary['resampled_other'] is True and summary['ncc_same_grid_median'] > .9
    assert summary['settings']['gsd_m'] == .075
    ok = [r for r in rows_of(world['judges'] / f'register_{B_ID}_rows.jsonl') if r['status'] == 'ok']
    assert len(ok) == 2
    for r in ok:
        assert abs(r['dx_m'] - SHIFT_M[0]) < .02 and abs(r['dy_m'] - SHIFT_M[1]) < .02
        assert abs(r['dx_px'] * GSD_A - r['dx_m']) < 1e-3 and abs(r['dy_px'] * GSD_A - r['dy_m']) < 1e-3   # A pixels kept
        assert r['ncc'] > .9
    payload = json.loads((world['judges'] / f'register_{B_ID}.json').read_text())
    assert payload['windows'][0]['ncc_same_grid'] > .9 and payload['summary']['ncc_same_grid']['n'] == 1


def test_register_at_equal_pixel_runs_the_unchanged_path(tmp_path, monkeypatch):
    world = build_world(tmp_path, other_id=C_ID, other_gsd=GSD_A)
    monkeypatch.setattr(tj, '_resample_to_grid', lambda *args, **kwargs: pytest.fail('resampled at equal pixel'))
    result = tj.register(tmp_path, A_ID, C_ID)
    assert set(result['summary']) == {'windows', 'buildings', 'ncc_ok', 'ncc_ok_share', 'global_shift_m', 'residual_m_ncc_ok',
                                      'ncc', 'settings'}
    ok = [r for r in rows_of(world['judges'] / f'register_{C_ID}_rows.jsonl') if r['status'] == 'ok']
    assert ok and all(set(r) == {'window', 'index', 'status', 'dx_px', 'dy_px', 'residual_m', 'ncc', 'area_m2'} for r in ok)
    assert all(abs(r['dx_px'] * GSD_A - SHIFT_M[0]) < .02 and abs(r['dy_px'] * GSD_A - SHIFT_M[1]) < .02 for r in ok)
    payload = json.loads((world['judges'] / f'register_{C_ID}.json').read_text())
    assert set(payload['windows'][0]) == {'window', 'global_dx_m', 'global_dy_m', 'global_shift_m', 'phase_response',
                                          'buildings_ncc_ok'}


@pytest.mark.parametrize('other_id, other_gsd', [(C_ID, GSD_A), (B_ID, GSD_B)])
def test_register_check_only_writes_nothing(tmp_path, monkeypatch, capsys, other_id, other_gsd):
    build_world(tmp_path, other_id=other_id, other_gsd=other_gsd)
    before = listing(tmp_path)
    monkeypatch.setattr(common, 'write_json_atomic', lambda *args, **kwargs: pytest.fail('check_only wrote a file'))
    result = tj.register(tmp_path, A_ID, other_id, limit=1, check_only=True)
    assert listing(tmp_path) == before
    assert result['check_only'] is True and result['summary']['windows'] == 1 and len(result['rows']) == 2
    for r in result['rows']:
        assert {'window', 'index', 'status'} <= set(r)
        if r['status'] == 'ok':
            assert {'dx_px', 'dy_px', 'ncc'} <= set(r)
    assert tj.main(['register', '--root', str(tmp_path), '--source', A_ID, '--other', other_id, '--limit', '1', '--check-only']) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['rows'] == result['rows'] and printed['check_only'] is True
    assert listing(tmp_path) == before


def test_check_only_and_null_shift_are_refused_where_they_mean_nothing(tmp_path):
    with pytest.raises(SystemExit, match='--check-only'):
        tj.main(['ladder', '--root', str(tmp_path), '--other', B_ID, '--check-only'])
    with pytest.raises(SystemExit, match='--null-shift-m'):
        tj.main(['register', '--root', str(tmp_path), '--other', B_ID, '--null-shift-m', '1.0'])


# --- boundary J1 on B's native grid, ladder prefix, cross-capture refusal --------------------------------------------

def test_j1_support_on_the_finer_grid_matches_the_coarser_grid():
    shift_px = (SHIFT_M[0] / GSD_A, SHIFT_M[1] / GSD_A)
    coarse_rgb, fine_rgb = render(GSD_A, SHIFT_M), render(GSD_B, SHIFT_M)
    gx, gy, mag = tj.sobel(tj.gray_f32(coarse_rgb))
    coarse = tj.j1_support(ROOF1_A_PX, gx, gy, mag, coarse_rgb, GSD_A, shift_px)
    s = GSD_A / GSD_B
    gx, gy, mag = tj.sobel(tj.gray_f32(fine_rgb))
    fine = tj.j1_support(affinity.scale(ROOF1_A_PX, s, s, origin=(0, 0)), gx, gy, mag, fine_rgb, GSD_B, (shift_px[0] * s, shift_px[1] * s))
    assert coarse['samples'] > 100 and fine['samples'] > 100
    assert abs(fine['supported'] / fine['samples'] - coarse['supported'] / coarse['samples']) <= .02
    assert coarse['corner_samples'] > 0 and fine['corner_samples'] > 0


def test_ladder_dispatch_writes_ladder_files_scored_on_the_finer_grid(tmp_path, monkeypatch):
    world = build_world(tmp_path)
    tj.register(tmp_path, A_ID, B_ID)
    calls, real = [], tj.j1_support

    def spy(polygon, gx, gy, mag, rgb, gsd, shift_px=(0., 0.), settings=tj.J1):
        calls.append((gsd, mag.shape, polygon.bounds))
        return real(polygon, gx, gy, mag, rgb, gsd, shift_px, settings)

    monkeypatch.setattr(tj, 'j1_support', spy)
    assert tj.main(['ladder', '--root', str(tmp_path), '--source', A_ID, '--other', B_ID, '--cells', 'all', '--peak-tol-m', '0.10']) == 0
    tag = f'{B_ID}_all_tol10cm'
    assert (world['judges'] / f'ladder_{tag}.json').exists() and (world['judges'] / f'ladder_{tag}_rows.jsonl').exists()
    assert not list(world['judges'].glob('boundary_*'))
    payload = json.loads((world['judges'] / f'ladder_{tag}.json').read_text())
    assert payload['variants'] == list(tj.LADDER) and payload['other_gsd_m'] == .05 and payload['scale'] == 1.5
    assert payload['summary']['buildings'] == 2
    assert calls and all(gsd == GSD_B and shape == (720, 720) for gsd, shape, _ in calls)
    assert any(abs(bounds[0] - 80 * 1.5) < 1e-6 and abs(bounds[3] - 200 * 1.5) < 1e-6 for _, _, bounds in calls)
    summary = payload['summary']
    assert summary['raw']['j1_pooled'] > .8 and summary['offset:0.15']['j1_pooled'] < summary['raw']['j1_pooled']


def test_plain_boundary_keeps_the_boundary_prefix(tmp_path):
    world = build_world(tmp_path)
    tj.register(tmp_path, A_ID, B_ID)
    assert tj.main(['boundary', '--root', str(tmp_path), '--source', A_ID, '--other', B_ID, '--cells', 'all', '--variants', 'raw']) == 0
    assert (world['judges'] / f'boundary_{B_ID}_all_tol10cm.json').exists()
    assert not list(world['judges'].glob('ladder_*'))


def test_cross_capture_refuses_unequal_pixels(tmp_path):
    build_world(tmp_path)
    before = listing(tmp_path)
    with pytest.raises(SystemExit) as refused:
        tj.cross_capture_judge(tmp_path, A_ID, B_ID)
    assert str(refused.value) == 'cross-capture needs equal pixel sizes; 5 cm teacher records do not exist yet'
    assert listing(tmp_path) == before


# --- corner locator --------------------------------------------------------------------------------------------------

@pytest.fixture(scope='module')
def captures():
    return {'A': tc.capture(render(GSD_A)), 'B': tc.capture(render(GSD_B, SHIFT_M))}


def _roof1_corners():
    corners = tc.outline_corners(ROOF1_A_PX, GSD_A)
    assert len(corners) == 4 and all(c['convex'] for c in corners)
    return corners


def _locate(captures, corner, polygon=ROOF1_A_PX, which='AB'):
    bounds_m = tuple(v * GSD_A for v in polygon.bounds)
    out = {}
    if 'A' in which:
        out['A'] = tc.image_corner(captures['A'], GSD_A, corner, tc.building_reference(captures['A'], bounds_m, (0., 0.), GSD_A),
                                   (0., 0.))
    if 'B' in which:
        out['B'] = tc.image_corner(captures['B'], GSD_B, corner, tc.building_reference(captures['B'], bounds_m, SHIFT_M, GSD_B),
                                   SHIFT_M)
    return out


def test_outline_corners_filter_turn_and_edge_length():
    corners = _roof1_corners()
    assert sorted(tuple(np.round(c['vertex_m'], 6)) for c in corners) == [(6., 6.), (6., 15.), (18., 6.), (18., 15.)]
    roof_m = affinity.scale(ROOF1_A_PX, GSD_A, GSD_A, origin=(0, 0))
    for c in corners:
        assert 65 <= c['turn_deg'] <= 115 and min(c['edge_m']) >= 1.2
        assert all(len(d) >= 6 and d[0] == pytest.approx(.4) and d[-1] <= 2. + 1e-9 for d in c['d'])
        for u, n, length in zip(c['u'], c['n'], c['edge_m'], strict=True):             # normals point out of the roof
            middle = np.asarray(c['vertex_m']) + u * length / 2
            assert not roof_m.contains(Point(*(middle + .1 * n))) and roof_m.contains(Point(*(middle - .1 * n)))
    notch = box(0, 0, 160, 120).difference(box(150, 110, 160, 120))   # a 0.75 m notch: its edges are too short
    kept = tc.outline_corners(notch, GSD_A)
    assert all(min(c['edge_m']) >= 1.2 for c in kept) and len(kept) < len(np.asarray(notch.exterior.coords)) - 1
    octagon = box(0, 0, 100, 100).buffer(30, join_style=3)             # 45 degree bevels: no corner in [65, 115]
    assert tc.outline_corners(octagon, GSD_A) == []


def test_fit_edge_line_recovers_a_line_and_refuses_weak_evidence():
    d = np.arange(.4, 2. + 1e-9, .1)
    t = .03 + .02 * d
    fit = tc.fit_edge_line(d, t)
    assert abs(fit['a'] - .03) < 1e-9 and abs(fit['b'] - .02) < 1e-9 and fit['inliers'] == len(d) and fit['rms_m'] < 1e-9
    spiked = t.copy()
    spiked[3] += .3
    robust = tc.fit_edge_line(d, spiked)
    assert robust['inliers'] == len(d) - 1 and abs(robust['a'] - .03) < 1e-6 and abs(robust['b'] - .02) < 1e-6
    assert tc.fit_edge_line(d[:4], t[:4]) is None                                   # fewer than 5 inliers
    noise = np.random.default_rng(3).normal(0., .1, len(d))
    assert np.sqrt(np.mean(noise ** 2)) > .07
    assert tc.fit_edge_line(d, .03 + noise) is None                                 # RMS above 0.05 m after the MAD pass
    two_lines = .03 + np.where(np.arange(len(d)) % 2, .08, -.08)                    # 9 samples on one line, 8 on another
    majority = tc.fit_edge_line(d, two_lines)                                       # one MAD pass keeps the majority line
    assert majority['inliers'] == 9 and abs(majority['a'] + .05) < 1e-9
    assert tc.fit_edge_line(d[:7], two_lines[:7]) is None                           # 4 vs 3: 4 inliers left, fewer than 5



def test_both_grids_recover_each_edge_and_agree_on_the_apex(captures):
    for corner in _roof1_corners():
        located = _locate(captures, corner)
        for side in ('A', 'B'):
            assert located[side]['status'] == 'ok', (side, located[side])
            for line in located[side]['lines']:                                       # the outline is the true edge here
                assert abs(line['a']) + 2. * abs(line['b']) <= .02, (side, line)
                assert line['rms_m'] <= .05 and line['inliers'] >= 5
        vx, vy = corner['vertex_m']
        for side in ('A', 'B'):
            assert np.hypot(located[side]['C'][0] - vx, located[side]['C'][1] - vy) <= .02
        assert np.hypot(located['B']['C'][0] - located['A']['C'][0], located['B']['C'][1] - located['A']['C'][1]) <= .02


def test_apex_gaps_follow_the_ladder_geometry_on_either_grid(captures):
    variants = tc.variant_polygons_m(ROOF1_A_PX, GSD_A)
    assert set(variants) == set(tj.LADDER)
    for corner in _roof1_corners():
        located = _locate(captures, corner)
        gaps = {side: tc.corner_gaps(located[side]['C'], corner, variants) for side in ('A', 'B')}
        for side in ('A', 'B'):
            g = gaps[side]
            assert abs(g['raw']) <= .03
            assert abs(g['round:0.15'] + .062) <= .02                               # 0.15 (sqrt 2 - 1)
            assert abs(g['offset:0.075'] - .106) <= .02                             # 0.075 sqrt 2
            assert g['offset:0.15'] > g['offset:0.075'] > g['raw'] > g['round:0.15'] > g['round:0.30']
            assert g['offset:-0.075'] < 0 and g['offset:-0.15'] < g['offset:-0.075']
        for name in tj.LADDER:
            assert abs(gaps['A'][name] - gaps['B'][name]) <= .02, name              # invariant to the grid


def _rotated_roof(gsd, shift=(0., 0.), angle_deg=30., sigma_px=1.5):
    """A 10 x 6 m roof rotated about (18, 18) m: RGB on this grid and the true corners in metres (unshifted frame)."""
    n = int(round(WINDOW_M / gsd))
    theta = np.radians(angle_deg)
    centres = (np.arange(n) + .5) * gsd
    xx, yy = np.meshgrid(centres - 18. - shift[0], centres - 18. - shift[1])
    along, across = xx * np.cos(theta) + yy * np.sin(theta), -xx * np.sin(theta) + yy * np.cos(theta)
    sigma = sigma_px * gsd
    inside = (ndtr((along + 5.) / sigma) - ndtr((along - 5.) / sigma)) * (ndtr((across + 3.) / sigma) - ndtr((across - 3.) / sigma))
    grey = np.clip(np.rint(60. + 130. * inside), 0, 255).astype(np.uint8)
    local = np.array([(-5., -3.), (5., -3.), (5., 3.), (-5., 3.)])
    corners = np.stack([18. + local[:, 0] * np.cos(theta) - local[:, 1] * np.sin(theta),
                        18. + local[:, 0] * np.sin(theta) + local[:, 1] * np.cos(theta)], axis=1)
    return np.repeat(grey[..., None], 3, axis=2), corners


def test_a_rotated_roof_is_located_on_both_grids(monkeypatch):
    rgb_a, truth = _rotated_roof(GSD_A)
    rgb_b, _ = _rotated_roof(GSD_B, SHIFT_M)
    caps = {'A': tc.capture(rgb_a), 'B': tc.capture(rgb_b)}
    outline = Polygon(truth / GSD_A)                                                # the outline on A's pixels
    corners = tc.outline_corners(outline, GSD_A)
    assert len(corners) == 4 and all(c['convex'] and abs(c['turn_deg'] - 90.) < 1e-6 for c in corners)
    variants = tc.variant_polygons_m(outline, GSD_A)
    for corner in corners:
        located = _locate(caps, corner, polygon=outline)
        nearest = min(truth, key=lambda p, c=corner: np.hypot(*(p - c['vertex_m'])))
        for side in ('A', 'B'):
            assert located[side]['status'] == 'ok'
            assert np.hypot(*(located[side]['C'] - nearest)) <= .02
            gaps = tc.corner_gaps(located[side]['C'], corner, variants)
            assert abs(gaps['raw']) <= .03 and abs(gaps['round:0.15'] + .062) <= .02 and abs(gaps['offset:0.075'] - .106) <= .02
        assert np.hypot(*(located['B']['C'] - located['A']['C'])) <= .02


def test_corner_locator_reads_its_tolerance_from_corners_not_j1(captures, monkeypatch):
    """An outline edge drawn 0.15 m outside the true edge: beyond J1's 0.10 m default, inside CORNERS' own window."""
    assert tc.CORNERS['peak_tol_m'] > .15 > tj.J1['peak_tol_m']
    wide = box(78, 80, 240, 200)                                                    # left edge 2 A px = 0.15 m out
    corner = next(c for c in tc.outline_corners(wide, GSD_A) if np.allclose(c['vertex_m'], (78 * GSD_A, 6.)))
    monkeypatch.setitem(tj.J1, 'peak_tol_m', .01)
    located = _locate(captures, corner, polygon=wide, which='A')['A']
    assert located['status'] == 'ok' and abs(located['C'][0] - 6.) <= .02 and abs(located['C'][1] - 6.) <= .02
    monkeypatch.setitem(tc.CORNERS, 'peak_tol_m', .10)
    assert _locate(captures, corner, polygon=wide, which='A')['A']['status'] != 'ok'


# --- the judge -------------------------------------------------------------------------------------------------------

def _judge_world(root):
    world = build_world(root)
    tj.register(root, A_ID, B_ID)
    return world


def test_corners_judge_refuses_without_the_pre_registered_rules(tmp_path):
    world = _judge_world(tmp_path)
    with pytest.raises(SystemExit, match='corners_rules.json'):
        tc.corners_judge(tmp_path, A_ID, B_ID, 'all')
    assert not list(world['judges'].glob('corners_*'))


def test_corners_judge_refuses_when_j1_was_mutated_in_this_process(tmp_path):
    world = _judge_world(tmp_path)
    tc.write_rules(world['judges'] / tc.RULES_NAME)
    tj.J1['peak_tol_m'] = .05                                                       # what `ladder --peak-tol-m 0.05` leaves behind
    with pytest.raises(AssertionError, match='peak_tol_m'):
        tc.corners_judge(tmp_path, A_ID, B_ID, 'all')
    assert not list(world['judges'].glob(f'corners_{B_ID}*'))


def test_write_rules_never_overwrites(tmp_path):
    path = tmp_path / tc.RULES_NAME
    tc.write_rules(path)
    first = path.read_bytes()
    with pytest.raises(SystemExit, match='amend'):
        tc.write_rules(path)
    assert path.read_bytes() == first
    rules = json.loads(first)
    assert set(rules['rules']) == {'R0', 'R1', 'R2', 'R3', 'R4', 'R5'} and rules['amendments'] == []
    assert rules['settings'] == json.loads(json.dumps(tc.CORNERS)) and rules['written_at']


def test_corners_judge_resolves_the_synthetic_corners_and_the_null_does_not(tmp_path):
    world = _judge_world(tmp_path)
    tc.write_rules(world['judges'] / tc.RULES_NAME)
    sha = common.sha256_file(world['judges'] / tc.RULES_NAME)
    result = tc.corners_judge(tmp_path, A_ID, B_ID, 'all')
    assert tj.main(['corners', '--root', str(tmp_path), '--source', A_ID, '--other', B_ID, '--cells', 'all', '--null-shift-m', '1.0']) == 0
    main_path, null_path = world['judges'] / f'corners_{B_ID}_all.json', world['judges'] / f'corners_{B_ID}_all_null1m.json'
    assert main_path.exists() and null_path.exists() and (world['judges'] / f'corners_{B_ID}_all_rows.jsonl').exists()
    summary = result['summary']
    assert summary['corners'] == 8 and summary['buildings'] == 2 and summary['resolved'] == 8 and summary['resolved_share'] == 1.
    assert summary['repeat_m']['median'] <= .02
    assert abs(summary['ladder']['raw']['mean']) <= .03 and abs(summary['ladder']['round:0.15']['mean'] + .062) <= .02
    assert abs(summary['paired_vs_raw']['round:0.15']['median_delta'] + .062) <= .02
    null = json.loads(null_path.read_text())
    assert null['null_shift_m'] == 1. and null['summary']['resolved_share'] < .2
    payload = json.loads(main_path.read_text())
    assert payload['rules_sha256'] == sha == null['rules_sha256']
    assert set(payload['rules']) == {'R0', 'R1', 'R2', 'R3', 'R4', 'R5'}
    assert all(r['result'] in ('pass', 'fail') and 'inputs' in r for r in payload['rules'].values())
    assert {k: payload['rules'][k]['result'] for k in ('R0', 'R1', 'R2', 'R4', 'R5')} == dict.fromkeys(('R0', 'R1', 'R2', 'R4', 'R5'), 'pass')
    assert payload['rules'] == null['rules']                                        # both runs judge the same two passes
    rows = rows_of(world['judges'] / f'corners_{B_ID}_all_rows.jsonl')
    assert len(rows) == 8
    for r in rows:
        assert {'window', 'index', 'corner_index', 'area_m2', 'C_B', 'C_A', 'repeat_m', 'resolved', 'rms_m', 'gaps'} <= set(r)
        assert set(r['gaps']) == set(tj.LADDER) and r['resolved'] is True


def test_evaluate_rules_is_mechanical():
    rules = tc.rules_document()
    good = {'register_global_shift_m_median': .1, 'register_ncc_ok_share': .8, 'register_ncc_same_grid_median': .97,
            'repeat_m_median': .03, 'repeat_m_median_ci95_building': [.02, .04], 'resolved_share': .8, 'null_resolved_share': .02,
            'raw_gap_mean_ci95_building': [-.01, .03], 'raw_gap_mad_m': .05}
    verdict = tc.evaluate_rules(good, rules)
    assert {k: v['result'] for k, v in verdict.items()} == dict.fromkeys(('R0', 'R1', 'R2', 'R3', 'R4', 'R5'), 'pass')
    assert verdict['R2']['inputs'] == {'resolved_share': .8, 'null_resolved_share': .02, 'resolved_minus_null': .78}
    assert verdict['R5']['wording'] == 'finer-sampling judge of one sortie'
    bad = {**good, 'register_global_shift_m_median': .31, 'repeat_m_median': .051, 'resolved_share': .55, 'null_resolved_share': .2,
           'raw_gap_mean_ci95_building': [-.04, .03], 'register_ncc_same_grid_median': .94}
    verdict = tc.evaluate_rules(bad, rules)
    assert {k: v['result'] for k, v in verdict.items()} == dict.fromkeys(('R0', 'R1', 'R2', 'R3', 'R4', 'R5'), 'fail')
    assert 'cross-capture stability' not in json.dumps({k: v.get('wording') for k, v in verdict.items()})
    assert tc.evaluate_rules({**good, 'raw_gap_mad_m': .16}, rules)['R3']['result'] == 'fail'
    assert tc.evaluate_rules({**good, 'register_ncc_ok_share': .49}, rules)['R0']['result'] == 'fail'
    missing = tc.evaluate_rules({}, rules)
    assert all(v['result'] == 'fail' for v in missing.values())
