import numpy as np
from scripts.ml.training import teacher_judges as tj
from scripts.ml.training import teacher_outlines as to
from shapely.geometry import Point, box


def test_classify_against_outlines_kinds():
    outlines = [box(0, 0, 10, 10), box(20, 0, 30, 10)]
    assert tj.classify_against_outlines(box(0.5, 0.5, 9.5, 9.5), outlines)['kind'] == 'matched'
    part = tj.classify_against_outlines(box(1, 1, 4, 4), outlines)          # inside one outline, a tenth of it
    assert part['kind'] == 'part' and part['inside_best'] == 1.
    merged = tj.classify_against_outlines(box(-1, -1, 31, 11), outlines)    # covers both outlines
    assert merged['kind'] == 'merged' and merged['outlines_covered'] == 2
    assert tj.classify_against_outlines(box(50, 50, 55, 55), outlines)['kind'] == 'unmatched'
    touching = tj.classify_against_outlines(box(8, 8, 18, 18), outlines)    # 4 percent inside: below the touch floor
    assert touching['kind'] == 'unmatched'
    overlapping = tj.classify_against_outlines(box(5, 0, 15, 10), outlines)  # half inside, not a part, not matched
    assert overlapping['kind'] == 'overlapping'


def test_window_bootstrap_share_brackets_the_pooled_share():
    num = [5, 6, 4, 5, 7, 5, 6, 4]
    den = [10] * 8
    lo, hi = tj.window_bootstrap_share(num, den, seed=1)
    pooled = sum(num) / sum(den)
    assert lo <= pooled <= hi and hi - lo < .2
    assert tj.window_bootstrap_share([0, 0], [0, 0]) is None


def test_area_bands():
    assert tj._band(10.) == 'lt_50m2' and tj._band(50.) == '50_200m2' and tj._band(500.) == 'ge_200m2'


def test_boundary_support_counts_samples_near_edges():
    distance = np.full((100, 100), 50., dtype=float)
    distance[:, 40:60] = 0.                       # a vertical band of strong edges through the middle
    square = box(45, 20, 55, 30)                  # whole boundary inside the band
    assert tj.boundary_support(square, distance, band_px=1., step_px=1.) == 1.
    far = box(0, 0, 10, 10)                       # nothing near an edge
    assert tj.boundary_support(far, distance, band_px=1., step_px=1.) == 0.
    half = box(30, 20, 50, 30)                    # right half of the boundary in the band
    value = tj.boundary_support(half, distance, band_px=1., step_px=1.)
    assert .3 < value < .7


def test_outline_pixels_from_native_affine():
    affine = [1000., .075, 0., 5000., 0., -.075]   # E0, px, 0, N0, 0, -px
    (poly,) = to.to_window_pixels([box(1000., 4999.25, 1000.75, 5000.)], affine)
    minx, miny, maxx, maxy = poly.bounds
    assert abs(minx - 0.) < 1e-9 and abs(maxx - 10.) < 1e-9
    assert abs(miny - 0.) < 1e-9 and abs(maxy - 10.) < 1e-9


def test_envelope_padding(monkeypatch):
    monkeypatch.setattr(to.ts, 'to_lonlat', lambda crs, xs, ys: ([172.60, 172.61], [-43.55, -43.54]))
    w, s, e, n = to.envelope_lonlat({'crs': 'EPSG:2193'}, (0., 0., 1., 1.))
    assert w < 172.60 and e > 172.61 and s < -43.55 and n > -43.54


def test_edge_distance_map_marks_strong_gradients():
    image = np.zeros((64, 64, 3), np.uint8)
    image[:, 32:] = 200
    distance, threshold = tj.edge_distance_map(image, percentile=90.)
    assert distance[10, 31] <= 1. and distance[10, 32] <= 1.
    assert distance[10, 5] > 5. and threshold >= 0.      # a flat image has a zero percentile; only true edges are strong
    circle = Point(32, 32).buffer(10., 32)
    assert tj.boundary_support(circle, distance, band_px=1., step_px=1.) < .3


def _edge_image(size=200, shift=(0, 0), value=180):
    """Dark ground with one bright square roof at a known place (RGB uint8)."""
    image = np.full((size, size, 3), 60, np.uint8)
    x0, y0 = 60 + shift[0], 70 + shift[1]
    image[y0:y0 + 50, x0:x0 + 60] = value
    return image


def test_global_shift_recovers_a_known_translation():
    a = _edge_image()
    b = _edge_image(shift=(8, -4))
    dx, dy, response = tj.global_shift(tj.gray_f32(a), tj.gray_f32(b), downsample=2)
    assert abs(dx - 8) <= 1.5 and abs(dy + 4) <= 1.5 and response > 0


def test_ncc_shift_recovers_a_known_translation_with_subpixel_peak():
    a = _edge_image()
    b = _edge_image(shift=(5, 3))
    _, _, mag_a = tj.sobel(tj.gray_f32(a))
    _, _, mag_b = tj.sobel(tj.gray_f32(b))
    result = tj.ncc_shift(mag_a, mag_b, (60, 70, 120, 120), pad_px=6, search_px=12)
    assert result is not None
    dx, dy, peak = result
    assert abs(dx - 5) < .6 and abs(dy - 3) < .6 and peak > .9
    assert tj.ncc_shift(mag_a, mag_b, (0, 0, 20, 20), pad_px=6, search_px=12) is None    # patch leaves the window
    assert tj._parabolic(np.array([0., 1., 0.]), 1) == 0.
    assert 0. < tj._parabolic(np.array([0., 1., .5]), 1) <= .5


def test_ring_samples_normals_point_outward():
    square = box(10, 10, 30, 30)
    points, normals = tj.ring_samples(square, step_px=2.)
    assert len(points) >= 8
    centre = np.array([20., 20.])
    outward = ((points - centre) * normals).sum(axis=1)
    assert (outward > 0).all()


def test_j1_support_high_on_the_true_edge_and_low_when_shifted():
    image = _edge_image()
    gx, gy, mag = tj.sobel(tj.gray_f32(image))
    gsd = .075
    roof = box(60, 70, 120, 120)                       # the exact roof edge (pixel edges at integer coordinates)
    on = tj.j1_support(roof, gx, gy, mag, image, gsd)
    off = tj.j1_support(roof, gx, gy, mag, image, gsd, shift_px=(4., 0.))   # 0.3 m off: beyond the 0.10 m tolerance
    assert on['samples'] > 0 and on['supported'] / on['samples'] > .8
    assert off['supported'] / off['samples'] < .5
    assert on['corner_samples'] > 0 and on['veg_excluded'] == 0
    # a 0.3 m rounding keeps the arc within 0.09 m of the true edge, inside J1's 0.10 m tolerance: not detectable at
    # this pixel; a 0.6 m rounding departs by 0.18 m and must lose corner support (the ladder measures this floor)
    rounded = roof.buffer(-8).buffer(8)
    rnd = tj.j1_support(rounded, gx, gy, mag, image, gsd)
    assert rnd['corner_supported'] / max(rnd['corner_samples'], 1) < on['corner_supported'] / on['corner_samples']


def test_j1_excludes_vegetation_samples():
    image = _edge_image()
    image[:, :, 1] = 200                               # everything green: ExG > 0.06 everywhere
    gx, gy, mag = tj.sobel(tj.gray_f32(image))
    result = tj.j1_support(box(60, 70, 120, 120), gx, gy, mag, image, .075)
    assert result['samples'] == 0 and result['veg_excluded'] > 0


def test_variant_polygons_offsets_and_rounding():
    poly = box(0, 0, 100, 60)
    variants = tj._variant_polygons(poly, .075, ['raw', 'offset:0.075', 'offset:-0.15', 'round:0.30'])
    assert variants['raw'] is poly
    assert variants['offset:0.075'].area > poly.area > variants['offset:-0.15'].area
    assert variants['round:0.30'].area < poly.area and len(variants['round:0.30'].exterior.coords) > 5


def test_ledger_recommendations_follow_the_policy():
    assert tj.recommend({'outline_kind': 'matched'}) == 'keep'
    assert tj.recommend({'outline_kind': 'unmatched', 'cross_capture': 'matched', 'edge_support': .9, 'area_m2': 8.}) == 'keep'
    assert tj.recommend({'outline_kind': 'part', 'area_m2': 30.}) == 'part'
    assert tj.recommend({'outline_kind': 'unmatched', 'area_m2': 8., 'cross_capture': 'only_here'}) == 'ignore'
    assert tj.recommend({'outline_kind': 'unmatched', 'area_m2': 40., 'cross_capture': None}) == 'review'
    assert tj.recommend({'outline_kind': 'overlapping', 'area_m2': 8.}) == 'review'
