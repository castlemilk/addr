"""Vegetation sub-class sidecar v1 (vegetation-classes-2026-10-08, lens E): synthetic feature rows only, no scene file is
read. One test per class boundary of RULE (vegetation_classes.py docstring); the thresholds are the module's own, so the
tests pin the decision structure, not tuned values."""
import pytest
from scripts.ml.experiments import vegetation_classes as vcl

R = vcl.RULE


def row(**over):
    """A mown lawn: vegetation candidate, smooth and light, compact, no cast shadow, away from the frame."""
    base = {'kind': 'vegetation', 'status': 'candidate', 'area_m2': 40.0, 'eq_diam_m': 7.1, 'mrr_width_m': 5.0,
            'mrr_length_m': 8.0, 'elongation': 1.6, 'solidity': 0.95, 'texture_p50': 0.03, 'lab_L_rel': 5.0,
            'hue_deg': 130.0, 'green_share': 0.95, 'shadow_share': 0.02, 'ring_shadow_share': 0.02,
            'lee_contrast': 0.05, 'lee_shadow_share': 0.02, 'sun_shadow_share': 0.02, 'sun_reliable': True,
            'building_overlap': 0.0, 'frame_edge_m': 0.0, 'clipped_by_frame': False, 'mpp': 0.1,
            'height_p50_m': None, 'height_p90_m': None}
    base.update(over)
    return base


def crown(**over):
    """A tree crown: tree candidate, dark and textured, 7 m spread."""
    return row(**{'kind': 'tree', 'texture_p50': 0.07, 'lab_L_rel': -8.0, **over})


CASTS = {'lee_contrast': -0.5, 'lee_shadow_share': 0.4, 'sun_shadow_share': 0.05}
STRIP = {'area_m2': 15.0, 'eq_diam_m': 4.4, 'mrr_width_m': 1.5, 'mrr_length_m': 10.0, 'elongation': 6.7, 'solidity': 0.9}


def test_lawn_is_grass_high():
    p = vcl.classify(row())
    assert (p['proposed'], p['confidence'], p['stature']) == ('grass', 'high', 'ground')
    assert any(e.startswith('height: none') for e in p['evidence'])


def test_classify_is_pure():
    r = row()
    before = dict(r)
    vcl.classify(r)
    assert r == before


@pytest.mark.parametrize('texture, expect', [(R['lawn_texture_max']-0.001, 'grass'), (R['lawn_texture_max'], 'unclear')])
def test_lawn_texture_boundary(texture, expect):
    p = vcl.classify(row(texture_p50=texture))
    assert p['proposed'] == expect
    if expect == 'unclear':     # textured ground: garden bed or striped lawn, never proposed as garden-bed
        assert p['unclear_reason'] == 'textured-ground' and p['candidates'] == ['grass', 'garden-bed']


def test_garden_bed_never_proposed():
    for texture in (0.06, 0.09, 0.15):
        assert vcl.classify(row(texture_p50=texture))['proposed'] != 'garden-bed'


@pytest.mark.parametrize('area, expect', [(R['speck_area_max']-0.01, 'flag:speck'), (R['speck_area_max'], None)])
def test_speck_boundary(area, expect):
    p = vcl.classify(row(area_m2=area, eq_diam_m=1.1, mrr_width_m=1.0, mrr_length_m=1.2, elongation=1.2))
    assert p['unclear_reason'] == expect


def test_blocking_flags():
    p = vcl.classify(crown(green_share=R['not_veg_green_max']-0.01, hue_deg=60.0))
    assert (p['proposed'], p['unclear_reason'], p['candidates']) == ('unclear', 'flag:not-green', ['tree', 'not-veg'])
    # a brown-hued object inside the lawn hue band is not flagged
    assert vcl.classify(crown(green_share=0.1, hue_deg=R['lawn_hue'][0]))['unclear_reason'] != 'flag:not-green'
    assert vcl.classify(crown(shadow_share=R['shadow_dominant_min']))['unclear_reason'] == 'flag:shadow'
    assert vcl.classify(crown(shadow_share=R['shadow_dominant_min']-0.01))['unclear_reason'] is None
    assert vcl.classify(row(building_overlap=R['on_roof_min']))['unclear_reason'] == 'flag:on-roof'


@pytest.mark.parametrize('spread, expect', [(R['tree_eq_diam_min']-0.01, 'shrub'), (R['tree_eq_diam_min'], 'tree')])
def test_shrub_tree_spread_boundary(spread, expect):
    p = vcl.classify(crown(eq_diam_m=spread, area_m2=3.14159*(spread/2)**2, mrr_width_m=spread*0.9,
                           mrr_length_m=spread*1.1, elongation=1.2))
    assert p['proposed'] == expect and p['stature'] == 'standing'
    assert p['confidence'] != 'high'      # inside the 2.5-5 m band without height


def test_frame_cut_small_crown_is_unclear_but_large_stays_tree():
    small = vcl.classify(crown(eq_diam_m=3.5, area_m2=9.6, mrr_width_m=2.8, mrr_length_m=3.8, elongation=1.4,
                               frame_edge_m=R['frame_cut_edge_min']))
    assert (small['proposed'], small['unclear_reason'], small['candidates']) == ('unclear', 'frame-cut-small',
                                                                                 ['shrub', 'tree'])
    assert 'frame-cut' in small['flags']
    uncut = vcl.classify(crown(eq_diam_m=3.5, area_m2=9.6, frame_edge_m=R['frame_cut_edge_min']-0.01))
    assert uncut['proposed'] == 'shrub'
    big = vcl.classify(crown(eq_diam_m=8.0, area_m2=50.0, clipped_by_frame=True))
    assert big['proposed'] == 'tree' and 'frame-cut' in big['flags']
    assert big['score'] < vcl.classify(crown(eq_diam_m=8.0, area_m2=50.0))['score']


@pytest.mark.parametrize('area, expect', [(R['tree_group_area_min']-0.1, 'tree'), (R['tree_group_area_min'], 'tree-group')])
def test_tree_group_area_boundary(area, expect):
    assert vcl.classify(crown(area_m2=area, eq_diam_m=13.8, mrr_width_m=12.0, mrr_length_m=16.0,
                              elongation=1.3))['proposed'] == expect


def test_tree_group_band_is_low_confidence():
    shape = {'eq_diam_m': 16.0, 'mrr_width_m': 14.0, 'mrr_length_m': 18.0, 'elongation': 1.3}
    band = vcl.classify(crown(area_m2=R['tree_group_sure_area']-1, **shape, **CASTS))
    sure = vcl.classify(crown(area_m2=R['tree_group_sure_area'], **shape, **CASTS))
    assert band['proposed'] == sure['proposed'] == 'tree-group'
    assert band['confidence'] == 'low' and sure['confidence'] == 'medium'     # crowns are not counted: never high


def test_textured_strip_standing_is_hedge():
    p = vcl.classify(crown(**STRIP, lab_L_rel=3.0, **CASTS))          # tall screen planting: tree kind, casts shadow
    assert p['proposed'] == 'hedge' and p['confidence'] in ('medium', 'low')
    wide = {**STRIP, 'mrr_width_m': R['hedge_width'][1]+0.1, 'area_m2': 60.0, 'eq_diam_m': 8.7, 'elongation': 3.1}
    assert vcl.classify(crown(**wide))['proposed'] == 'tree'
    assert vcl.classify(crown(**{**STRIP, 'elongation': R['hedge_elongation_min']-0.1}))['proposed'] != 'hedge'
    assert vcl.classify(crown(**{**STRIP, 'solidity': R['hedge_solidity_min']-0.01}))['proposed'] != 'hedge'


def test_smooth_strip_casting_shadow_is_unclear_hedge_or_grass():
    p = vcl.classify(row(**STRIP, **CASTS))             # clipped hedge or a lawn strip beside a building's shadow
    assert (p['proposed'], p['unclear_reason']) == ('unclear', 'stature-conflict')
    assert set(p['candidates']) >= {'hedge', 'grass'}


def test_smooth_shaded_strip_casting_shadow_is_a_tie():
    p = vcl.classify(row(**STRIP, **CASTS, ring_shadow_share=R['lawn_ring_shadow_max']))
    assert (p['proposed'], p['unclear_reason']) == ('unclear', 'stature-tie')


def test_cast_shadow_test_needs_shadow_class_pixels():
    weak = {**CASTS, 'lee_shadow_share': CASTS['sun_shadow_share']+R['cast_shadow_min']-0.01}
    p = vcl.classify(row(**STRIP, **weak, texture_p50=0.07))   # darker lee without shadow pixels: no standing vote
    assert p['unclear_reason'] == 'textured-ground'


def test_unreliable_sun_weakens_the_cast_shadow():
    p = vcl.classify(row(texture_p50=0.07, **CASTS, sun_reliable=False))   # vegetation kind 1 vs weak shadow 1
    assert (p['proposed'], p['unclear_reason']) == ('unclear', 'stature-tie')
    p = vcl.classify(row(texture_p50=0.07, **CASTS))                         # reliable: standing 2 vs ground 1
    assert p['stature'] == 'standing' and p['proposed'] == 'tree'


def test_confirmed_tree_that_looks_like_lawn_is_unclear():
    p = vcl.classify(row(kind='tree', status='confirmed'))
    assert (p['proposed'], p['unclear_reason']) == ('unclear', 'stature-conflict')
    p = vcl.classify(row(kind='tree'))          # candidate 'tree canopy' that looks smooth: tie (v2), not grass
    assert (p['proposed'], p['unclear_reason'], p['candidates'][0]) == ('unclear', 'stature-tie', 'tree')
    p = vcl.classify(row(kind='tree', **CASTS))  # ...with a cast shadow: standing 4 vs ground 2 -> tree, not high
    assert p['proposed'] == 'tree' and p['confidence'] != 'high'
    p = vcl.classify(row(kind='tree', ring_shadow_share=0.5))   # smooth-shaded 1 vs kind 2 -> tree
    assert p['proposed'] == 'tree'


def test_dark_vegetation_kind_is_standing():
    p = vcl.classify(row(texture_p50=0.06, lab_L_rel=R['dark_L_rel_max']-0.1))
    assert p['stature'] == 'standing' and p['proposed'] == 'tree'


def test_frame_sliver_strip_is_unclear():
    p = vcl.classify(crown(**STRIP, frame_edge_m=STRIP['mrr_length_m']*R['sliver_frame_share_min']))
    assert (p['proposed'], p['unclear_reason']) == ('unclear', 'frame-cut-strip')
    assert 'frame-sliver' in p['flags']


def test_frame_cut_lawn_stays_grass():
    p = vcl.classify(row(frame_edge_m=12.0, clipped_by_frame=True))
    assert p['proposed'] == 'grass' and 'frame-cut' in p['flags']


@pytest.mark.parametrize('height, stature', [(R['ground_height_max']-0.01, 'ground'), (R['ground_height_max'], 'standing')])
def test_height_ground_boundary(height, stature):
    p = vcl.classify(row(height_p90_m=height))
    assert p['stature'] == stature
    assert p['proposed'] == ('grass' if stature == 'ground' else 'shrub')    # standing below 3 m, not a strip


@pytest.mark.parametrize('height, expect', [(R['tall_height_min']-0.01, 'shrub'), (R['tall_height_min'], 'tree')])
def test_height_shrub_tree_boundary(height, expect):
    p = vcl.classify(crown(eq_diam_m=3.0, area_m2=7.1, height_p90_m=height))
    assert p['proposed'] == expect and not any(e.startswith('height: none') for e in p['evidence'])


def test_height_settles_a_conflict():
    no_height = vcl.classify(row(**STRIP, **CASTS))
    assert no_height['proposed'] == 'unclear'
    hedge = vcl.classify(row(**STRIP, **CASTS, height_p90_m=1.5))
    assert hedge['proposed'] == 'hedge' and hedge['stature'] == 'standing'
    grass = vcl.classify(row(**STRIP, **CASTS, height_p90_m=0.1))
    assert grass['proposed'] == 'grass'
    big = vcl.classify(crown(eq_diam_m=3.0, area_m2=7.1, frame_edge_m=3.0, height_p90_m=6.0))
    assert big['proposed'] == 'tree'                    # height replaces the frame-censored spread


def test_confidence_orders_agreeing_evidence():
    strong = vcl.classify(crown(**CASTS))
    weak = vcl.classify(crown(lab_L_rel=3.0))           # light textured crown: only the kind says standing
    assert strong['proposed'] == weak['proposed'] == 'tree'
    assert strong['score'] > weak['score'] and strong['confidence'] == 'high' and weak['confidence'] == 'medium'
    assert vcl.confidence_label(R['conf_high']) == 'high' and vcl.confidence_label(R['conf_medium']) == 'medium'
    assert vcl.confidence_label(R['conf_medium']-0.001) == 'low'


@pytest.mark.parametrize('text, cls', [('Hedge A', 'hedge'), ('front lawn', 'grass'), ('Bush', 'shrub'),
                                       ('garden bed', 'garden-bed'), ('tree', 'tree'), ('Tree group', 'tree-group'),
                                       ('vegitation', 'vegetation'), ('Vegetation', 'vegetation'), ('Car', None),
                                       ('', None)])
def test_label_class(text, cls):
    assert vcl.label_class(text) == cls


def test_label_agreement():
    assert vcl.label_agrees('tree', {'proposed': 'tree-group'}) is True
    assert vcl.label_agrees('hedge', {'proposed': 'shrub'}) is False
    assert vcl.label_agrees('tree', {'proposed': 'unclear'}) is None
    assert vcl.label_agrees(None, {'proposed': 'tree'}) is None
    assert vcl.label_agrees('vegetation', {'proposed': 'tree'}) is False      # generic word = ground / low family
    assert vcl.label_agrees('vegetation', {'proposed': 'grass'}) is True


def test_census_thresholds_shared():
    from scripts.ml.experiments import vegetation_census as vc
    assert all(vc.RULE[k] == R[k] for k in vcl.CENSUS_SHARED)


# ------------------------------------------------------------------------------------------------ design v2
# After the by-eye skeptic (.run/session-2026-10-08-cuts-veg/eye-veg.md): no tree-group class (crown-peak note instead),
# shrub only on strong evidence else tree (or shrub) uncertain, clipped hedge. Synthetic rows only.

R2 = vcl.RULE_V2
ALONE = {'neighbours_measured': True, 'larger_crown_gap_m': None, 'larger_crown_area_m2': None}
SHRUB = {'eq_diam_m': 2.5, 'area_m2': 4.9, 'mrr_width_m': 2.3, 'mrr_length_m': 2.7, 'elongation': 1.17,
         'solidity': 0.95, 'texture_p50': 0.045, 'lab_L_rel': -8.0, **ALONE}
CANOPY = {'area_m2': 200.0, 'eq_diam_m': 16.0, 'mrr_width_m': 14.0, 'mrr_length_m': 18.0, 'elongation': 1.3}


def test_v2_keeps_v1_rule_and_adds_keys():
    assert all(R2[k] == R[k] for k in R)                 # v1 thresholds unchanged inside v2
    assert vcl.V2_KEYS and not set(vcl.V2_KEYS) & set(R)
    assert 'tree-group' not in vcl.PROPOSED_V2 and 'tree-group' in vcl.PROPOSED
    assert vcl.classify(crown(**CANOPY))['proposed'] == 'tree-group'     # v1 still reproduces its tree-group


def test_v2_classify_is_pure():
    r = crown(**CANOPY, crown_peaks=3)
    before = dict(r)
    vcl.classify_v2(r)
    assert r == before


def test_v2_tree_group_band_is_tree():
    for area in (R['tree_group_area_min'], R['tree_group_sure_area'], 600.0):
        p = vcl.classify_v2(crown(**{**CANOPY, 'area_m2': area}, crown_peaks=1))
        assert (p['proposed'], p['display'], p['uncertain'], p['notes']) == ('tree', 'tree', False, [])
        assert p['candidates'] == ['tree']             # no shrub alternative for a 150 m2+ canopy
    p = vcl.classify_v2(crown(**CANOPY, **CASTS))
    assert p['confidence'] == 'high'                   # a single large crown is not held down any more


@pytest.mark.parametrize('peaks, noted', [(None, False), (1, False), (R2['group_peaks_min'], True), (5, True)])
def test_v2_group_note_needs_two_crown_peaks(peaks, noted):
    p = vcl.classify_v2(crown(**CANOPY, crown_peaks=peaks))
    assert p['proposed'] == 'tree'
    assert bool(p['notes']) is noted
    if noted:
        assert p['notes'][0].startswith(vcl.GROUP_NOTE)
    plain = vcl.classify_v2(crown(**CANOPY, crown_peaks=1))
    assert p['score'] == plain['score']                # the note changes neither class nor confidence


def test_v2_group_note_only_in_the_v1_band_and_names_the_sidecar_line():
    small = vcl.classify_v2(crown(area_m2=R['tree_group_area_min']-0.1, eq_diam_m=13.8, mrr_width_m=12.0,
                                  mrr_length_m=16.0, elongation=1.3, crown_peaks=3))
    assert small['proposed'] == 'tree' and small['notes'] == []
    p = vcl.classify_v2(crown(**CANOPY, crown_peaks=2, crown_split={'decision': 'run', 'T': 5, 'cut': False}))
    assert 'sidecar-v1 line T5: no cut' in p['notes'][0]
    p = vcl.classify_v2(crown(**CANOPY, crown_peaks=2, crown_split={'decision': 'skip_frame_cut'}))
    assert 'skipped it: frame-cut' in p['notes'][0]


def test_v2_strong_shrub():
    p = vcl.classify_v2(crown(**SHRUB))
    assert (p['proposed'], p['display'], p['uncertain']) == ('shrub', 'shrub', False)
    assert [t[0] for t in p['shrub_tests']] == ['small', 'compact', 'alone', 'low texture', 'no cast shadow']
    assert all(t[1] for t in p['shrub_tests'])
    assert p['confidence'] != 'high'                   # no height: never high


@pytest.mark.parametrize('over, failed', [
    ({'eq_diam_m': R2['shrub_spread_max'], 'area_m2': 7.07}, 'small'),
    ({'elongation': R2['shrub_elongation_max']+0.01}, 'compact'),
    ({'solidity': R2['shrub_solidity_min']-0.01}, 'compact'),
    ({'larger_crown_gap_m': R2['neighbour_gap_m'], 'larger_crown_area_m2': 60.0}, 'alone'),   # crown fragment
    ({'neighbours_measured': False}, 'alone'),
    ({'texture_p50': R2['shrub_texture_max']}, 'low texture'),
    (CASTS, 'no cast shadow'),                                                               # tall narrow crown
])
def test_v2_weak_shrub_evidence_is_tree_or_shrub(over, failed):
    p = vcl.classify_v2(crown(**{**SHRUB, **over}))
    assert (p['proposed'], p['display'], p['uncertain']) == ('tree', vcl.TREE_OR_SHRUB, True)
    assert p['confidence'] == 'low' and p['candidates'] == ['tree', 'shrub']
    assert [t[0] for t in p['shrub_tests'] if not t[1]] == [failed]
    assert 'not strong shrub evidence' in p['reason']
    assert vcl.classify(crown(**{**SHRUB, **over}))['proposed'] == 'shrub'     # v1 called every one of these shrub


def test_v2_shrub_boundaries_pass():
    p = vcl.classify_v2(crown(**{**SHRUB, 'eq_diam_m': R2['shrub_spread_max']-0.01,
                                 'larger_crown_gap_m': R2['neighbour_gap_m']+0.01, 'larger_crown_area_m2': 60.0}))
    assert p['proposed'] == 'shrub'


def test_v2_spread_tree_and_frame_cut_small_unchanged():
    assert vcl.classify_v2(crown(**{**SHRUB, 'eq_diam_m': R['tree_eq_diam_min'], 'area_m2': 12.6}))['display'] == 'tree'
    p = vcl.classify_v2(crown(**SHRUB, frame_edge_m=R['frame_cut_edge_min']))
    assert (p['proposed'], p['unclear_reason']) == ('unclear', 'frame-cut-small')


def test_v2_height_still_decides_shrub_and_tree():
    p = vcl.classify_v2(crown(**{**SHRUB, 'larger_crown_gap_m': 0.0, 'larger_crown_area_m2': 80.0, **CASTS},
                              height_p90_m=R['tall_height_min']-0.01))
    assert (p['proposed'], p['uncertain']) == ('shrub', False)     # height is the strong evidence
    p = vcl.classify_v2(crown(**SHRUB, height_p90_m=R['tall_height_min']))
    assert (p['proposed'], p['uncertain']) == ('tree', False)
    p = vcl.classify_v2(crown(**CANOPY, crown_peaks=3, height_p90_m=12.0))
    assert p['proposed'] == 'tree' and p['notes']


def test_v2_uncertain_tree_in_a_stature_tie_offers_shrub():
    over = {**SHRUB, 'texture_p50': 0.03, 'lab_L_rel': 5.0, 'eq_diam_m': 3.5, 'area_m2': 9.6}   # smooth vs tree kind
    p = vcl.classify_v2(row(kind='tree', **over))
    assert (p['proposed'], p['unclear_reason']) == ('unclear', 'stature-tie')
    assert p['candidates'] == ['tree', 'shrub', 'grass']
    p = vcl.classify_v2(row(kind='tree', **{**over, 'eq_diam_m': 2.5, 'area_m2': 4.9}))     # strong shrub: offered alone
    assert p['candidates'] == ['shrub', 'grass']


def test_rectangularity():
    assert vcl.rectangularity({'area_m2': 15.0, 'mrr_width_m': 1.5, 'mrr_length_m': 10.0}) == 1.0
    assert vcl.rectangularity({'area_m2': 3.0, 'mrr_width_m': 0.0, 'mrr_length_m': 10.0}) is None


def test_v2_clipped_hedge():
    p = vcl.classify_v2(row(**STRIP, **CASTS))          # smooth strip, own cast shadow, fills its rectangle
    assert (p['proposed'], p['stature'], p['candidates']) == ('hedge', 'standing', ['hedge', 'grass'])
    assert p['confidence'] == 'low' and any(e.startswith('clipped hedge') for e in p['evidence'])
    assert vcl.classify(row(**STRIP, **CASTS))['unclear_reason'] == 'stature-conflict'      # v1 unchanged


@pytest.mark.parametrize('over, reason', [
    ({'area_m2': STRIP['mrr_width_m']*STRIP['mrr_length_m']*(R2['clipped_hedge_rect_min']-0.01)}, 'stature-conflict'),
    ({'sun_reliable': False}, None),                                  # weak shadow vote: ground wins, grass
    ({'ring_shadow_share': R['lawn_ring_shadow_max']}, 'stature-tie'),  # smooth-shaded: lawn beside a shadow
    ({'frame_edge_m': STRIP['mrr_length_m']*R['sliver_frame_share_min']}, 'stature-conflict'),
])
def test_v2_clipped_hedge_needs_every_cue(over, reason):
    p = vcl.classify_v2(row(**{**STRIP, **CASTS, **over}))
    assert p['proposed'] != 'hedge' and p['unclear_reason'] == reason


def test_v2_clipped_hedge_rect_boundary():
    area = STRIP['mrr_width_m']*STRIP['mrr_length_m']*R2['clipped_hedge_rect_min']
    assert vcl.classify_v2(row(**{**STRIP, **CASTS, 'area_m2': area}))['proposed'] == 'hedge'


def test_v2_clipped_hedge_without_cast_shadow_stays_grass():
    assert vcl.classify_v2(row(**STRIP))['proposed'] == 'grass'


def test_v2_flags_and_grass_match_v1():
    for r in (row(), row(texture_p50=0.07), crown(shadow_share=R['shadow_dominant_min']), row(area_m2=0.5),
              crown(**STRIP, frame_edge_m=STRIP['mrr_length_m'])):
        v1, v2 = vcl.classify(r), vcl.classify_v2(r)
        assert (v1['proposed'], v1['unclear_reason'], v1['candidates']) == (v2['proposed'], v2['unclear_reason'],
                                                                           v2['candidates'])


def test_v2_label_agreement_tree_group_label_agrees_with_tree():
    assert vcl.label_agrees('tree-group', vcl.classify_v2(crown(**CANOPY))) is True


def test_twin_groups():
    lat = -37.8
    m = 1/111_320.0
    items = [('a', 'w1', 145.0, lat, 10.0), ('b', 'w2', 145.0, lat-1.5*m, 12.0), ('c', 'w1', 145.0, lat+1.0*m, 10.0),
             ('d', 'w3', 145.0, lat+1.0*m, 30.0), ('e', 'w3', 145.0, lat+50*m, 10.0), ('f', 'w4', None, None, 10.0)]
    g = vcl.twin_groups(items)
    assert g['a'] == ['b'] and g['b'] == ['a']        # same window (c) never joins; area ratio > 2 (d) never joins
    assert g['c'] == [] and g['d'] == [] and g['e'] == [] and 'f' not in g
