"""Pure-function tests for the read-only train-tile negatives worklist (scripts/ml/experiments/train_tile_negatives.py)."""
import math

import numpy as np
from scripts.ml.experiments import train_tile_negatives as ttn
from shapely.geometry import Polygon


def _ev(**over):
    ev = {'area_m2': 10., 'score': .7, 'parent_id': None, 'surface_by_kind': {}, 'surface_share': 0., 'car_share': 0., 'canopy_share': 0.,
          'roof_part_share': 0., 'brightness_ratio': 1., 'texture_ratio': 1., 'aspect': 1.5, 'rectilinearity': .9, 'shadow': None, 'lot_flags': None,
          'tree_distance_m': None}
    ev.update(over)
    return ev


def test_score_terms_are_named_and_summed():
    total, terms, contributions = ttn.score_row(_ev(surface_by_kind={'road': .8}, surface_share=.8, score=.5, area_m2=5.))
    assert set(contributions) <= set(ttn.SCORE_TERMS)
    assert contributions['surface'] == round(3. * .8, 3) and contributions['tiny'] == .5 and contributions['low_score'] == round(2. * .1, 3)
    assert total == round(sum(contributions.values()), 3)
    words = ttn.evidence_words(_ev(surface_by_kind={'road': .8}, surface_share=.8, score=.5, area_m2=5.), contributions)
    assert words[0] == 'road 0.80' and 'tiny' in words


def test_brightness_dead_band_and_roof_part_counts_against():
    _, _, near = ttn.score_row(_ev(brightness_ratio=1.08))
    assert 'brightness' not in near
    _, _, far = ttn.score_row(_ev(brightness_ratio=.5))
    assert far['brightness'] == round(.5 * (1. - ttn.BRIGHTNESS_DEAD_BAND), 3)
    total, _, part = ttn.score_row(_ev(roof_part_share=.9))
    assert part['roof_part'] == -.9 and total == -.9


def test_shadow_terms_are_exclusive():
    _, _, side = ttn.score_row(_ev(shadow='shadow_side'))
    _, _, adj = ttn.score_row(_ev(shadow='adjacent_dark'))
    assert side == {'tree_shadow_side': 1.5} and adj == {'tree_adjacent_dark': .75}
    assert ttn.angle_between((1, 0), (0, 1)) == 90. and ttn.angle_between((0, 0), (0, 1)) is None


def test_shadow_azimuth_reads_the_dark_side():
    luma = np.full((200, 200), 180., dtype=np.float32)
    luma[120:150, 80:120] = 40.         # a dark band south (+y) of the tree
    tree = Polygon([(80, 80), (120, 80), (120, 120), (80, 120)])
    canopy = ttn.raster(tree, 0, 0, 200, 200)
    out = ttn.shadow_azimuth(luma, [tree], canopy, mpp=.25)
    assert out['direction'] == ttn.DIRECTIONS[2] and out['index'] == 2 and out['contrast'] > ttn.SHADOW_CONTRAST_MIN


def test_interior_stats_and_shape_terms():
    luma = np.zeros((50, 50), dtype=np.float32)
    luma[10:30, 10:40] = 100.
    poly = Polygon([(10, 10), (40, 10), (40, 30), (10, 30)])
    mean, std, n, how = ttn.interior_stats(luma, poly)
    assert how == 'eroded' and n > 0 and math.isclose(mean, 100.) and std == 0.
    shape = ttn.shape_terms(poly, .25)
    assert shape['aspect'] == 1.5 and shape['rectilinearity'] == 1. and shape['long_m'] == 7.5
    assert ttn.share_in(poly, Polygon([(10, 10), (25, 10), (25, 30), (10, 30)])) == .5
