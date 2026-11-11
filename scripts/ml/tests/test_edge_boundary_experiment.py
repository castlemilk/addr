"""Variant frame builders, the experimental edge-snap and the pairing/summary helpers of the edge-boundary experiment."""
import numpy as np
import pytest
from scripts.ml import edge_boundary_experiment as exp
from shapely.geometry import Polygon

SCENE = {'pixel_to_meters': [[0.1, 0], [0, -0.1], [-20., 20.]], 'meters_to_pixel': [[10., 0], [0, -10.], [200., 200.]]}


def step_image(size=160, low=60, high=200, x0=20, x1=120, y0=20, y1=120):
    """Flat background with one brighter square whose edges sit exactly on pixel columns/rows."""
    rgb = np.full((size, size, 3), low, np.uint8)
    rgb[y0:y1, x0:x1] = high
    return rgb


def rect(x0, y0, x1, y1):
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


# --------------------------------------------------------------------------- variant frames

@pytest.mark.parametrize('name', sorted(exp.VARIANTS))
def test_variant_builders_preserve_shape_dtype_and_are_deterministic(name):
    rng = np.random.default_rng(1)
    rgb = rng.integers(40, 220, (96, 128, 3), dtype=np.uint8)
    build = exp.VARIANTS[name]
    first, second = build(rgb), build(rgb)
    assert first.shape == rgb.shape and first.dtype == np.uint8
    assert np.array_equal(first, second)
    assert first is not rgb  # never the caller's buffer


def test_original_is_an_untouched_copy_and_wrong_dtype_raises():
    rgb = step_image()
    assert np.array_equal(exp.frame_original(rgb), rgb)
    for build in exp.VARIANTS.values():
        with pytest.raises(ValueError):
            build(rgb.astype(np.float32))
        with pytest.raises(ValueError):
            build(rgb[:, :, 0])


def test_clahe_raises_luminance_spread_on_a_low_contrast_frame():
    rng = np.random.default_rng(0)
    low = rng.normal(120, 6, (96, 96, 3)).clip(0, 255).astype(np.uint8)
    out = exp.frame_clahe(low, tile_grid=(2, 2))
    assert out.mean(axis=2).std() > low.mean(axis=2).std()


def test_edges_frame_darkens_only_pixels_next_to_the_step():
    rgb = step_image()
    out = exp.frame_edges(rgb)
    diff = rgb.astype(int) - out.astype(int)
    assert (diff >= 0).all()                       # edges are darkened, never brightened
    assert diff[60, 18:23].max() > 0               # around the left step (x=20) something got darker
    assert diff[60, 40:100].max() == 0             # the flat roof interior is untouched
    assert diff[5:15, 5:15].max() == 0             # flat background is untouched


def test_unsharp_increases_the_step_contrast():
    rgb = step_image()
    out = exp.frame_unsharp(rgb)
    before = int(rgb[60, 21, 0]) - int(rgb[60, 18, 0])
    after = int(out[60, 21, 0]) - int(out[60, 18, 0])
    assert after > before
    assert np.array_equal(out[5:15, 5:15], rgb[5:15, 5:15])  # flat areas are unchanged


def test_edge_distance_map_is_zero_on_edges_and_inf_when_flat():
    distance = exp.edge_distance_map(step_image())
    assert distance.shape == (160, 160)
    assert distance[60, 19:22].min() == 0.
    assert distance[60, 70] > 10
    flat = exp.edge_distance_map(np.full((32, 32, 3), 90, np.uint8))
    assert np.isinf(flat).all()


# --------------------------------------------------------------------------- outlines and metrics

def test_starting_outline_prefers_the_detector_raw_ring():
    obj = {'ring': [[0, 0], [10, 0], [10, 10], [0, 10]], 'boundary_refinement': {'raw_ring': [[0, 0], [12, 0], [12, 12], [0, 12]], 'raw_holes': []}}
    polygon, kind = exp.starting_outline(obj)
    assert kind == 'raw_ring' and polygon.area == 144
    polygon, kind = exp.starting_outline({'ring': obj['ring']})
    assert kind == 'saved_ring' and polygon.area == 100


def test_outline_agreement_identical_and_shifted():
    a = exp.to_local(rect(20, 20, 120, 120), SCENE)
    same = exp.outline_agreement(a, a)
    assert same['iou'] == 1. and same['boundary_mean_m'] == 0. and same['hausdorff_m'] == 0.
    shifted = exp.outline_agreement(a, exp.to_local(rect(30, 20, 130, 120), SCENE))
    assert shifted['iou'] == pytest.approx(90/110, abs=1e-3)
    assert shifted['hausdorff_m'] == pytest.approx(1., abs=1e-3)
    assert 0 < shifted['boundary_mean_m'] < 1.


def test_edge_snap_moves_one_short_edge_onto_the_image_edge_and_is_deterministic():
    rgb = step_image()                      # bright square spans x 20..120, y 20..120 (edges at 19.5/119.5 between pixels)
    distance = exp.edge_distance_map(rgb)
    start = rect(20, 20, 117, 120)          # right edge 3 px inside the true edge
    snapped, info = exp.edge_snap(start, distance, SCENE)
    again, _ = exp.edge_snap(start, distance, SCENE)
    assert info['accepted'] and info['moved_edges'] == 1 and info['reason'] == 'edge-gain'
    xs = np.asarray(snapped.exterior.coords)[:, 0]
    assert xs.max() == pytest.approx(119.5, abs=1.)   # snapped to the Canny ridge at the step
    assert xs.min() == pytest.approx(20., abs=1e-6)   # the already supported left edge does not move
    assert snapped.equals(again)


def test_edge_snap_respects_the_shift_budget_and_leaves_flat_frames_alone():
    rgb = step_image()
    distance = exp.edge_distance_map(rgb)
    # 8 px away: outside the min(0.45 m, 4 px) = 4 px search, so no offset gains support.
    unchanged, info = exp.edge_snap(rect(20, 20, 112, 120), distance, SCENE)
    assert not info['accepted'] and unchanged.equals(rect(20, 20, 112, 120))
    flat = exp.edge_distance_map(np.full((160, 160, 3), 90, np.uint8))
    same, info = exp.edge_snap(rect(20, 20, 117, 120), flat, SCENE)
    assert info['reason'] == 'no-edge-gain' and same.equals(rect(20, 20, 117, 120))


def test_edge_snap_rejects_a_move_that_changes_the_area_too_much():
    rgb = step_image()
    distance = exp.edge_distance_map(rgb)
    # A 42 px wide strip whose right edge is 2 px short of the step (edge column 119): the 1.5 px move onto that
    # column is 3.6 % of the area (> 3 %). Its left edge sits in the flat roof interior, so nothing else can move.
    start = rect(75, 20, 117, 120)
    result, info = exp.edge_snap(start, distance, SCENE)
    assert info['reason'] == 'exceeds-budget' and result.equals(start)


# --------------------------------------------------------------------------- summaries

def row(ref_id, base_iou, variant_ious, base_dist=.3, variant_dists=None, tier='T1'):
    variant_dists = variant_dists or {k: base_dist for k in variant_ious}
    def cell(iou, dist):
        return {'iou': iou, 'boundary_mean_m': dist, 'edge_support': {'original': .7, 'clahe': .8}, 'accepted': True, 'reason': 'edge-gain',
                'moved_edges': 1, 'shift_m': .1}
    return {'ref_id': ref_id, 'tier': tier, 'scene_id': 's', 'object_id': ref_id,
            'production': {'original': cell(base_iou, base_dist)},
            'edge_snap': {k: cell(v, variant_dists[k]) for k, v in variant_ious.items()}}


ROWS = [row('a', .80, {'x': .85, 'y': .80}), row('b', .90, {'x': .89, 'y': .90}), row('c', .70, {'x': .70, 'y': .75}),
        row('d', .60, {'x': .65, 'y': .55}), row('e', .95, {'x': .96, 'y': .95})]


def test_paired_summary_reports_medians_sign_counts_and_change_counts():
    summary = exp.paired_summary(ROWS, 'edge_snap', 'x')
    assert summary['n'] == 5 and summary['baseline'] == 'production:original'
    assert summary['iou']['baseline_median'] == .8 and summary['iou']['variant_median'] == .85
    assert summary['iou']['delta_median'] == pytest.approx(.01)
    assert (summary['iou']['n_better'], summary['iou']['n_worse'], summary['iou']['n_unchanged']) == (3, 1, 1)
    assert (summary['iou']['n_improved_over'], summary['iou']['n_worsened_over']) == (2, 0)
    assert summary['iou']['n_changed'] == 4 and summary['iou']['delta_median_among_changed'] == pytest.approx(.03)
    assert summary['boundary_mean_m']['delta_median'] == 0. and summary['boundary_mean_m']['n_better'] == 0
    assert summary['edge_support_median'] == {'original': .7, 'clahe': .8}


def test_bootstrap_ci_is_deterministic_and_brackets_the_median():
    deltas = [.01, .02, -.01, .03, .0, .015, -.02, .005]
    first, second = exp.bootstrap_median_ci(deltas), exp.bootstrap_median_ci(deltas)
    assert first == second
    assert first[0] <= np.median(deltas) <= first[1]
    assert exp.bootstrap_median_ci([.1]) is None
    assert exp.bootstrap_median_ci([]) is None


def test_per_roof_deltas_sort_by_magnitude_and_best_variant_prefers_higher_median_delta():
    deltas = exp.per_roof_deltas(ROWS, 'edge_snap', 'x')
    assert [d['ref_id'] for d in deltas] == ['a', 'd', 'b', 'e', 'c']
    assert deltas[0]['iou_delta'] == pytest.approx(.05) and deltas[0]['snap']['accepted']
    summaries = [exp.paired_summary(ROWS, 'edge_snap', v) for v in ('x', 'y')]
    assert exp.best_variant(summaries)['variant'] == 'x'
    assert exp.best_variant([]) is None


def test_paired_summary_skips_rows_missing_the_variant():
    rows = [*ROWS, {'ref_id': 'z', 'tier': 'T1', 'scene_id': 's', 'object_id': 'z', 'production': {'original': {'iou': .5, 'boundary_mean_m': .1, 'edge_support': {}}}, 'edge_snap': {}}]
    assert exp.paired_summary(rows, 'edge_snap', 'x')['n'] == 5
