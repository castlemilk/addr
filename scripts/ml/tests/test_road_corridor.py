"""road_corridor (experiments): the road corridor v1 sidecar on synthetic geometry.

A 600 x 600 px frame at 0.1 m/px. The parcel reserve is a horizontal 14 m strip (y 230-370 px); numbered lots lie above and
below it. Roads are rectangles inside the strip with car-shaped bites. Pure functions only: nothing is read from or written to
data/scenes, no model is loaded. Counts and geometry, never accuracy.
"""
import copy
import json

import numpy as np
from scripts.ml.experiments import road_corridor as rc
from shapely.geometry import LineString, Polygon, box

W = H = 600
MPP = .1
AFFINE = [[.1, 0.], [0., -.1], [0., 60.]]
RESERVE = box(0, 230, W, 370)
LOTS = {'numbered_lot': box(0, 0, W, 230).union(box(0, 370, W, H))}


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def obj(oid, kind, ring, status='candidate', score=.8, holes=None):
    o = {'id': oid, 'kind': kind, 'label': kind.capitalize(), 'ring': ring, 'status': status, 'geometry_reviewed': False,
         'source': 'sam3-text-onnx', 'score': score, 'prompt': kind}
    if holes:
        o['holes'] = holes
    return o


def run(objects, design=None, reserve=RESERVE, lots=None):
    rep, ctx, cache = rc.corridor_layer(objects, width=W, height=H, mpp=MPP, reserve=reserve,
                                        lots=LOTS if lots is None else lots, design=design or rc.DESIGN, affine=AFFINE)
    return rep


def street(oid='road1', status='candidate', x0=60, x1=540, y0=260, y1=340):
    """An 8 m x 48 m road with a parked-car bite on the top kerb (a notch 2 m deep, 4.5 m long)."""
    ring = [[x0, y0], [200, y0], [200, y0+20], [245, y0+20], [245, y0], [x1, y0], [x1, y1], [x0, y1]]
    return obj(oid, 'road', ring, status)


def car(oid, x0, y0, x1, y1, status='candidate'):
    return obj(oid, 'car', rect(x0, y0, x1, y1), status)


def only(rep, seed):
    return next(x for x in rep['proposals']+rep['exits'] if x['seed'] == seed)


def test_band_is_straight_and_constant_width():
    rep = run([street(), car('c1', 201, 261, 244, 279)])
    rec = only(rep, 'road1')
    assert 'proposal' in rec, rec.get('exit_reasons')
    ring = rec['proposal']['ring']
    assert len(ring) <= rc.DESIGN['max_band_vertices']
    assert len(ring) == 4                                       # a straight band: one rectangle
    band = Polygon(ring)
    axis = LineString(rec['axis_px'])
    assert len(axis.coords) == 2                                # one straight segment
    # constant width: every cross-section of the band has the same length
    widths = []
    for x in range(120, 500, 20):
        cut = LineString([(x, 0), (x, H)]).intersection(band)
        widths.append(cut.length*MPP)
    assert max(widths)-min(widths) < .05
    assert abs(widths[0]-rec['width_m']) < .05
    assert rec['width_cv_after'] is not None and rec['width_cv_after'] < .01
    assert abs(rec['width_m']-8.) < .3


def test_car_union_only_within_the_adjacency_buffer():
    # a car standing 0.4 m below the bottom kerb line of a road whose lower edge is at y 340
    road = street()
    near = car('c_near', 300, 344, 345, 362)
    for adj, unioned in ((.3, False), (.5, True)):
        rep = run([road, near], design={**rc.DESIGN, 'adjacency_m': adj})
        rec = only(rep, 'road1')
        assert ('c_near' in rec['cars_unioned']) is unioned, (adj, rec['cars_unioned'])


def test_trees_are_never_unioned_and_tree_ended_sections_are_dropped():
    road = street()
    # a canopy biting 2 m into the bottom kerb between x 300 and 380
    road['ring'] = [[60, 260], [540, 260], [540, 340], [380, 340], [380, 320], [300, 320], [300, 340], [60, 340]]
    tree = obj('t1', 'tree', rect(298, 318, 382, 400))
    rep = run([road, tree])
    rec = only(rep, 'road1')
    assert 't1' not in rec['cars_unioned']
    assert rec['section_ends'].get('tree', 0) >= 6              # the bitten stations end at the tree
    # the band width ignores the bitten (tree-ended) stations: the full 8 m, not 6 m
    assert abs(rec['width_m']-8.) < .3
    assert 'proposal' in rec


def test_gate_exits():
    # short: a 7 m road
    rep = run([obj('short', 'road', rect(100, 270, 170, 330))])
    assert only(rep, 'short')['exit'] == 'short'
    # junction: a crossroads (two arms off the main path, each >= 3 m beyond the half width)
    cross = Polygon(rect(60, 260, 540, 340)).union(Polygon(rect(260, 60, 340, 540)))
    rep = run([obj('x', 'road', [list(c) for c in cross.exterior.coords[:-1]])],
              reserve=box(0, 0, W, H), lots={})
    assert 'junction' in only(rep, 'x')['exit_reasons']
    # low IoU: an L-shaped blob a straight band cannot fit
    ell = Polygon(rect(60, 240, 300, 360)).union(Polygon(rect(220, 240, 300, 560)))
    for gate_value in (.5, .6, .7):
        rep = run([obj('ell', 'road', [list(c) for c in ell.exterior.coords[:-1]])], reserve=box(0, 0, W, H), lots={},
                  design={**rc.DESIGN, 'max_segments': 1, 'iou_gate': gate_value})
        rec = only(rep, 'ell')
        assert ('low_iou' in rec['exit_reasons']) is (rec['iou_amodal'] < gate_value)
    assert rec['iou_amodal'] < .7 and 'low_iou' in rec['exit_reasons']
    # high CV: a wedge from 3 m to 13 m wide
    wedge = [[60, 285], [540, 235], [540, 365], [60, 315]]
    rep = run([obj('wedge', 'road', wedge)], reserve=box(0, 200, W, 400))
    assert 'high_cv' in only(rep, 'wedge')['exit_reasons']
    # lot class: a road lying on common property (CM) with no reserve in the frame
    rep = run([street('cm')], reserve=None, lots={'common_property': box(0, 0, W, H)})
    assert 'lot_class' in only(rep, 'cm')['exit_reasons']
    # reserve fraction: a road half outside the reserve
    rep = run([obj('half', 'road', rect(60, 190, 540, 290))])
    assert 'reserve_fraction' in only(rep, 'half')['exit_reasons']


def test_confirmed_road_is_never_rewritten():
    road = street(status='confirmed')
    road['geometry_reviewed'] = True
    objects = [road, car('c1', 201, 261, 244, 279)]
    before = copy.deepcopy(objects)
    rep = run(objects)
    rec = only(rep, 'road1')
    assert objects == before                                    # the input is untouched
    p = rec['proposal']
    assert p['status'] == 'proposal'
    assert 'road1' in p['replaces_on_accept'] and 'road1' not in p['replaces']


def test_decided_twin_keeps_class():
    road = street()
    twin = obj('dw', 'driveway', copy.deepcopy(road['ring']), status='confirmed')
    rep = run([road, twin])
    group = rep['twin_groups'][0]
    assert group['resolved_class'] == 'driveway' and group['how'].startswith('confirmed')
    rec = only(rep, 'road1')
    assert rec['exit'] == 'p0_decided_other_class'
    assert not rep['proposals']
    # a confirmed road with a needs_adjustment driveway twin: the road's corridor is proposed, the twin is kept as it is
    road2 = street(status='confirmed')
    twin2 = obj('dw2', 'driveway', copy.deepcopy(road2['ring']), status='needs_adjustment')
    rep = run([road2, twin2])
    p = only(rep, 'road1')['proposal']
    t = next(x for x in p['xclass_twin'] if x['id'] == 'dw2')
    assert t['decided'] and t['action'].startswith('kept') and t['kind'] == 'driveway'
    assert 'dw2' not in p['replaces'] and 'dw2' not in p['replaces_on_accept']
    # a candidate twin in the reserve is absorbed as road (recorded, not dropped)
    road3, twin3 = street(), obj('dw3', 'driveway', copy.deepcopy(street()['ring']))
    p = only(run([road3, twin3]), 'road1')['proposal']
    assert 'dw3' in p['replaces'] and any(x['id'] == 'dw3' and not x['decided'] for x in p['xclass_twin'])


def test_no_band_over_a_building():
    # a building standing 1 m into the road's middle: the band may not cover it
    bld = obj('b1', 'building', rect(280, 330, 330, 420))
    rec = only(run([street(), bld]), 'road1')
    assert rec['exit'] == 'covers_blocker'
    # a building at the road's end: the band stops before it (trimmed), and covers none of it
    bld_end = obj('b2', 'building', rect(500, 330, 560, 420))
    rec = only(run([street(), bld_end]), 'road1')
    assert 'proposal' in rec, rec.get('exit_reasons')
    assert Polygon(rec['proposal']['ring']).intersection(Polygon(bld_end['ring'])).area*MPP*MPP <= rc.DESIGN['blocker_eps_m2']
    assert rec['trimmed_ends'] and rec['trimmed_ends'][0]['blocker'] == 'b2'
    # cars and trees are the classes a band may go under
    rec = only(run([street(), car('c1', 201, 261, 244, 279), obj('t', 'tree', rect(400, 330, 440, 380))]), 'road1')
    assert 'proposal' in rec and 'c1' in rec['proposal']['cars_under']


def test_specks_and_same_mask_siblings_are_absorbed():
    road = street(status='confirmed')
    speck = obj('speck', 'road', rect(205, 262, 212, 268), status='needs_adjustment', score=.8)   # same score: same mask
    other = obj('frag', 'road', rect(400, 300, 420, 320), score=.5)                                # a candidate inside the band
    rep = run([road, speck, other, car('c1', 213, 261, 244, 279)])
    p = only(rep, 'road1')['proposal']
    absorbed = {a['id'] for a in p['absorbs']}
    assert {'speck', 'frag'} <= absorbed
    assert 'speck' in p['replaces_on_accept'] and 'frag' in p['replaces']
    assert rep['counts']['specks_absorbed'] >= 1


def test_narrow_band_exits():
    rep = run([obj('lane', 'road', rect(60, 290, 540, 315))])         # a 2.5 m strip
    assert only(rep, 'lane')['exit'] == 'narrow'


def test_deterministic():
    objects = [street(), car('c1', 201, 261, 244, 279), obj('t', 'tree', rect(400, 330, 440, 380)),
               obj('dw', 'driveway', copy.deepcopy(street()['ring']))]
    a = json.dumps(rc.jsonable(run(objects)), sort_keys=True)
    b = json.dumps(rc.jsonable(run(list(reversed(copy.deepcopy(objects))))), sort_keys=True)
    assert a == b


def test_lot_class_and_reserve():
    assert rc.lot_class({'lot': 'CM', 'spi': 'CM\\PS432924'}) == 'common_property'
    assert rc.lot_class({'lot': '12', 'spi': '12\\LP1'}) == 'numbered_lot'
    assert rc.lot_class({'lot': 'R1', 'spi': 'R1\\PS1'}) == 'reserve_lot'
    assert rc.lot_class({'lot': None, 'spi': 'CP153157'}) == 'single_lot_or_crown'
    shares, dominant = rc.lot_shares(box(0, 250, 100, 300), RESERVE, LOTS)
    assert dominant == 'reserve' and shares['reserve'] == 1.
    medial = rc.reserve_medial(RESERVE, W, H, MPP)
    assert len(medial) > 100 and np.allclose(np.median(medial[:, 1]), 300, atol=2)


# --- road corridor v2 (design 'v2'): each test below fails when its change is removed (mutation proof in the session folder
# .run/session-2026-10-07-roads/v2/mut/). The v1 tests above run on rc.DESIGN and stay unchanged.

V2 = rc.DESIGN_V2


def run2(objects, reserve=RESERVE, lots=None, design=None):
    return run(objects, design=design or V2, reserve=reserve, lots=lots)


def poly_ring(geometry):
    return [list(c) for c in geometry.exterior.coords[:-1]]


def test_v2_slice_exits():
    # 1a: a street cut by the frame bottom: the band's long edge runs along the frame edge -> slice
    reserve = box(0, 480, W, H)
    rec = only(run2([obj('cut', 'road', rect(60, 520, 540, 600))], reserve=reserve, lots={'numbered_lot': box(0, 0, W, 480)}), 'cut')
    assert rec['exit'] == 'slice', rec['exit_reasons']
    assert rec['slice']['frame_slice']
    # ... and when part of it stays 0.5 m clear of the frame (a band is built): its bottom edge runs along the frame
    part = obj('part', 'road', [[60, 525], [540, 525], [540, 595], [330, 595], [330, 600], [60, 600]])
    rec = only(run2([part], reserve=reserve, lots={'numbered_lot': box(0, 0, W, 480)}), 'part')
    assert rec['exit'] == 'slice' and rec.get('width_m') and rec['slice']['frame_edge_share'] >= V2['slice_frame_share']
    # 1b: a 5 m lane of a 10 m street made of two road pieces (different masks) -> slice (5 < 0.75 x 10)
    lane_a = obj('lane_a', 'road', rect(60, 250, 540, 300), score=.8)
    lane_b = obj('lane_b', 'road', rect(60, 300, 540, 350), score=.5)
    rep = run2([lane_a, lane_b])
    rec = only(rep, 'lane_a')
    assert rec['slice']['narrow_slice'] and abs(rec['slice']['street_width_m']-10.) < .3
    assert 'slice' in rec['exit_reasons'] and not rep['proposals']
    # the full street is no slice
    assert 'proposal' in only(run2([street()]), 'road1')


def perpendicular_cars(y0=345):
    return [car(f'p{i}', 200+25*i, y0, 220+25*i, y0+45) for i in range(4)]


def test_v2_no_reserve_and_car_park():
    # 2a: no reserve anywhere in the frame -> no band
    rec = only(run2([street()], reserve=None, lots={'single_lot_or_crown': box(0, 0, W, H)}), 'road1')
    assert rec['exit'] == 'no_reserve' and 'proposal' not in rec
    # 2b: no reserve and cars side by side perpendicular to the axis -> car_park
    rec = only(run2([street(), *perpendicular_cars()], reserve=None, lots={'single_lot_or_crown': box(0, 0, W, H)}), 'road1')
    assert rec['exit'] == 'car_park' and rec['car_park']['perpendicular'] >= 3 and rec['car_park']['side_by_side_pairs'] >= 1
    # 2c: a large single-lot site (a reserve elsewhere in the frame) with the same evidence -> car_park
    rec = only(run2([street(), *perpendicular_cars()], reserve=box(0, 0, W, 100), lots={'single_lot_or_crown': box(0, 100, W, H)}), 'road1')
    assert rec['exit'] == 'car_park' and rec['car_park']['why'].startswith('large single-lot')
    # parallel parked cars are no car park
    rec = only(run2([street(), car('c1', 201, 261, 244, 279), car('c2', 300, 321, 345, 339)]), 'road1')
    assert 'proposal' in rec and not rec['car_park']['evidence']


def arc_road(sag_px=30, half=40):
    xs = list(range(60, 541, 20))
    yc = [300+sag_px*((x-300)/240)**2 for x in xs]
    return [[x, y-half] for x, y in zip(xs, yc, strict=True)]+[[x, y+half] for x, y in reversed(list(zip(xs, yc, strict=True)))]


def test_v2_curved_exits():
    wide = box(0, 150, W, 450)
    lots = {'numbered_lot': box(0, 0, W, 150).union(box(0, 450, W, H))}
    # 3a: a curved street (3 m sagitta over 48 m): the kerb-side mask edge leaves the straight band by > 1 m over 15 m
    rec = only(run2([obj('arc', 'road', arc_road())], reserve=wide, lots=lots), 'arc')
    assert rec['exit'] == 'curved', rec['exit_reasons']
    assert rec['curvature']['edge_runs'] and not rec['curvature']['axis_off']
    # 3b: a straight street rotated 5 degrees off the reserve medial line
    from shapely import affinity
    rot = affinity.rotate(Polygon(rect(100, 260, 500, 340)), 5, origin=(300, 300))
    rec = only(run2([obj('rot', 'road', poly_ring(rot))], reserve=wide, lots=lots), 'rot')
    assert 'curved' in rec['exit_reasons'] and rec['curvature']['axis_off']
    # a straight street on the reserve line is not curved
    rec = only(run2([obj('ok', 'road', rect(60, 260, 540, 340))], reserve=wide, lots=lots), 'ok')
    assert 'proposal' in rec and not rec['curvature']['curved']


def test_v2_footpath_and_vegetation_candidates():
    # 4a: a 10 m2 candidate footpath in the middle of the band -> covers_footpath
    rec = only(run2([street(), obj('fp', 'footpath', rect(280, 290, 330, 310))]), 'road1')
    assert rec['exit'] == 'covers_footpath', rec['exit_reasons']
    # up to 2 m2 is allowed under the band
    rec = only(run2([street(), obj('fp', 'footpath', rect(280, 290, 290, 305))]), 'road1')
    assert 'proposal' in rec
    # 4b: a vegetation candidate 0.5 m inside the top edge for 30 m: the edge is trimmed back to it
    rec = only(run2([street(), obj('veg', 'vegetation', rect(150, 260, 450, 265))]), 'road1')
    assert 'proposal' in rec, rec['exit_reasons']
    assert rec['footpath_trims'] and rec['footpath_trims'][0]['edge'] in ('lo', 'hi')
    band = Polygon(rec['proposal']['ring'])
    assert band.intersection(Polygon(rect(150, 260, 450, 265))).area*MPP*MPP <= V2['footpath_eps_m2']


def test_v2_decided_twins_block_or_retire():
    # 4c: a decided driveway twin of the seed (IoU 0.83: not the same street) blocks: covers_decided
    road = street(status='confirmed')
    twin = obj('dw', 'driveway', rect(60, 260, 540, 340), status='needs_adjustment')
    twin['ring'] = rect(60, 268, 540, 340)                      # IoU about 0.9 against the notched road: shrink it
    twin['ring'] = rect(60, 274, 540, 340)
    rep = run2([road, twin])
    assert rep['twin_groups'] and rep['twin_groups'][0]['resolved_class'] == 'road'
    rec = only(rep, 'road1')
    assert rec['exit'] == 'covers_decided', rec['exit_reasons']
    # 4d: the same street (IoU >= 0.9, reserve fraction >= 0.95): the proposal retires the twin on accept
    twin2 = obj('dw2', 'driveway', copy.deepcopy(road['ring']), status='needs_adjustment')
    rec = only(run2([road, twin2]), 'road1')
    p = rec['proposal']
    assert p['retires_on_accept'] == ['dw2'] and 'dw2' not in p['replaces'] and 'dw2' in p['derived_from']
    assert next(x for x in p['xclass_twin'] if x['id'] == 'dw2')['action'].startswith('retires')
    # 4e: a decided road of another mask in the middle of the band blocks as well
    other = obj('dec', 'road', rect(250, 280, 330, 330), status='confirmed', score=.4)
    rec = only(run2([street(), other]), 'road1')
    assert rec['exit'] == 'covers_decided' and any(b['id'] == 'dec' for b in rec['blockers_decided'])


def test_v2_one_street_one_band_and_one_width():
    # 5a: two collinear pieces with a 3 m gap holding a parked car -> one band over both
    a = obj('a', 'road', rect(60, 260, 280, 340), score=.8)
    b = obj('b', 'road', rect(310, 260, 540, 340), score=.6)
    rep = run2([a, b, car('gapcar', 282, 300, 308, 320)])
    assert len(rep['proposals']) == 1
    rec = rep['proposals'][0]
    assert {rec['seed'], *rec['joined']} == {'a', 'b'} and {'a', 'b'} <= set(rec['proposal']['replaces'])
    assert rec['axis_length_m'] >= 47.
    # 5b: two pieces of one reserve segment that may not join (a footpath crosses the gap) share one width
    a = obj('a', 'road', rect(60, 265, 250, 335), score=.8)    # 7 m
    b = obj('b', 'road', rect(330, 255, 540, 345), score=.6)   # 9 m
    rep = run2([a, b, obj('xing', 'footpath', rect(255, 230, 325, 370))])
    assert len(rep['proposals']) == 2, [e['exit_reasons'] for e in rep['exits']]
    wa, wb = (only(rep, k)['width_m'] for k in ('a', 'b'))
    assert wa == wb and only(rep, 'a')['width_shared'] and rep['shared_widths']


def test_v2_band_continues_under_a_canopy():
    # 6: a canopy across the street; the road resumes beyond it on the same axis -> one band through the canopy
    a = obj('a', 'road', rect(60, 260, 255, 340), score=.8)
    b = obj('b', 'road', rect(345, 260, 540, 340), score=.6)
    tree = obj('t', 'tree', rect(250, 235, 350, 365))
    rep = run2([a, b, tree])
    assert len(rep['proposals']) == 1, [e['exit_reasons'] for e in rep['exits']]
    rec = rep['proposals'][0]
    band = Polygon(rec['proposal']['ring'])
    assert band.contains(Polygon(rect(270, 280, 330, 320)))   # under the canopy
    assert band.bounds[0] >= 59. and band.bounds[2] <= 541.     # never past the last road piece
    # grass in the gap instead of a tree: no join, two separate pieces
    rep = run2([a, b, obj('g', 'vegetation', rect(255, 250, 345, 350))])
    assert all(not p.get('joined') for p in rep['proposals'])


def test_v2_width_median_unbuffered_cars_and_mirror():
    # 7a: a row of cars parked over the bottom kerb line, unioned UNBUFFERED: kerb (top) to the cars' outer edge (8.5 m)
    road = obj('r', 'road', rect(60, 260, 540, 330))
    cars = [car(f'k{i}', 62+48*i, 325, 107+48*i, 345) for i in range(10)]
    rec = only(run2([road, *cars]), 'r')
    assert 'proposal' in rec, rec['exit_reasons']
    assert abs(rec['width_m']-8.5) < .2, rec['width_m']
    # 7b: the median, p75 recorded: 60 % of the stations 8 m, 40 % 10 m (a widening on the bottom side)
    wide = Polygon(rect(60, 260, 540, 340)).union(Polygon(rect(350, 330, 540, 360)))
    rec = only(run2([obj('w', 'road', poly_ring(wide))], reserve=box(0, 230, W, 380)), 'w')
    assert abs(rec['width_m']-8.) < .2 and rec['width_p75_m'] >= 9.5, (rec['width_m'], rec['width_p75_m'])
    # 7c: tree-ended sections leave the width: a canopy bites 2 m into 70 % of the bottom kerb
    bitten = obj('bt', 'road', [[60, 260], [540, 260], [540, 340], [460, 340], [460, 320], [100, 320], [100, 340], [60, 340]])
    rec = only(run2([bitten, obj('t', 'tree', rect(98, 318, 462, 400))]), 'bt')
    assert abs(rec['width_m']-8.) < .3, (rec['width_m'], rec.get('width_source'))
    # 7d: canopy half: one kerb visible, the other half under a tree -> mirrored about the reserve medial line (y 300)
    half = obj('h', 'road', rect(60, 260, 540, 300))
    rec = only(run2([half, obj('t', 'tree', rect(40, 298, 560, 345))]), 'h')
    assert rec['width_source'] == 'mirrored' and abs(rec['width_m']-8.) < .3, (rec.get('width_source'), rec.get('width_m'))
    band = Polygon(rec['proposal']['ring'])
    assert abs((band.bounds[1]+band.bounds[3])/2-300) < 3


def test_v2_junction_yields_its_longest_straight_arm():
    # 8: a T-junction road: the through street (48 m) gets the band, the stem stays modal (remainder on accept)
    tee = Polygon(rect(60, 260, 540, 340)).union(Polygon(rect(260, 340, 340, 580)))
    reserve = box(0, 230, W, 370).union(box(240, 370, 360, H))
    lots = {'numbered_lot': box(0, 0, W, 230).union(box(0, 370, 240, H)).union(box(360, 370, W, H))}
    rec = only(run2([obj('tee', 'road', poly_ring(tee))], reserve=reserve, lots=lots), 'tee')
    assert 'proposal' in rec, rec['exit_reasons']
    assert rec['junction']['is_junction'] and rec['junction']['arm_m'] >= V2['arm_min_m']
    band = Polygon(rec['proposal']['ring'])
    assert band.intersection(box(260, 380, 340, 580)).area == 0      # the stem is not under the band
    rem = rec['proposal']['remainder_on_accept']
    assert rem and Polygon(rem[0]['ring']).intersection(box(260, 380, 340, 580)).area > .9*80*200
    # an arm under 20 m still exits as a junction
    short = Polygon(rect(200, 260, 380, 340)).union(Polygon(rect(260, 340, 340, 580)))
    rec = only(run2([obj('st', 'road', poly_ring(short))], reserve=reserve, lots=lots), 'st')
    assert 'proposal' not in rec


def test_v2_v1_unchanged_and_deterministic():
    # v1 stays the default design and is untouched by v2; v2 is deterministic under object order
    assert rc.DESIGN['version'] == 'road-corridor-v1' and rc.DESIGNS['v1'] is rc.DESIGN
    objects = [street(), car('c1', 201, 261, 244, 279), obj('t', 'tree', rect(400, 330, 440, 380)),
               obj('dw', 'driveway', copy.deepcopy(street()['ring']))]
    a = json.dumps(rc.jsonable(run2(objects)), sort_keys=True)
    b = json.dumps(rc.jsonable(run2(list(reversed(copy.deepcopy(objects))))), sort_keys=True)
    assert a == b


def test_v2_additions_bites_and_verge():
    # addition to 7: short notches (no tree object) on 60 % of one kerb are bites, not kerbs: the width stays 8 m
    ring = [[60, 260], [540, 260], [540, 340]]
    for x0 in (480, 390, 300, 210, 120):                          # five 6 m notches, 2 m deep, 3 m apart
        ring += [[x0+60, 340], [x0+60, 320], [x0, 320], [x0, 340]]
    ring += [[60, 340]]
    rec = only(run2([obj('n', 'road', ring)]), 'n')
    assert 'proposal' in rec and abs(rec['width_m']-8.) < .3 and rec['section_ends'].get('bite', 0) >= 20, (rec.get('width_m'), rec.get('section_ends'))
    # addition to 3: a mask that fills the verge up to the property line for 20 m (a footpath spill) is no kerb evidence
    spill = Polygon(rect(60, 260, 540, 340)).union(Polygon(rect(150, 235, 350, 261)))
    rec = only(run2([obj('sp', 'road', poly_ring(spill))]), 'sp')
    assert 'proposal' in rec and not rec['curvature']['curved'] and rec['section_ends'].get('verge', 0) >= 15, rec['exit_reasons']


# --- road corridor v3 (design 'v3'): each test below fails when its change is removed (mutation proof in the session folder
# .run/session-2026-10-07-roads/v3/mut/). The v1 and v2 tests above run on rc.DESIGN / rc.DESIGN_V2 and stay unchanged.

V3 = rc.DESIGN_V3
WORLD_M = 40075016.68557849                                 # scene_addresses.WORLD (web mercator circumference)


def kerb_image(y0=260, y1=340, kerb_m=.5, asphalt=90., kerb=170., ground=110., x_range=None):
    """A luminance frame: asphalt between y0 and y1, a light kerb-and-channel strip kerb_m wide on both sides, darker ground
    beyond (the fall the v3 kerb step looks for)."""
    a = np.full((H, W), ground, np.float32)
    k = int(round(kerb_m/MPP))
    xs = slice(None) if x_range is None else slice(*x_range)
    a[y0-k:y1+k, xs] = kerb
    a[y0:y1, xs] = asphalt
    return a


def run3(objects, image=None, reserve=RESERVE, lots=None, xwin=None, design=None):
    rep, ctx, cache = rc.corridor_layer(objects, width=W, height=H, mpp=MPP, reserve=reserve, lots=LOTS if lots is None else lots,
                                        design=design or V3, affine=AFFINE, image=image, xwin=xwin)
    return rep


def test_v3_width_runs_to_the_outer_kerb():
    # 2: an 8 m asphalt mask with a 0.5 m kerb and channel on both sides -> the band runs kerb back to kerb back (9 m)
    rec = only(run3([street(), car('c1', 201, 261, 244, 279)], image=kerb_image()), 'road1')
    assert 'proposal' in rec, rec['exit_reasons']
    assert abs(rec['width_m']-9.) < .15 and rec['kerb']['flag'] is None, (rec['width_m'], rec['kerb'])
    assert rec['width_v2_median_m'] == 8. and rec['kerb']['two_kerb_n'] >= 40
    assert abs(np.mean([y for _, y in rec['proposal']['ring']])-300) < 1.5
    # no image, or no fall within 0.8 m (a 1.2 m light strip: a footpath abutting the kerb), or no light strip at all ->
    # the v2 median, flagged
    for image in (None, kerb_image(kerb_m=1.2), kerb_image(kerb=110.)):
        rec = only(run3([street()], image=image), 'road1')
        assert rec['width_m'] == 8. and rec['kerb']['flag'] == 'kerb_not_found', (rec['width_m'], rec['kerb'])
    # one side only: the top kerb strip is missing -> that side keeps the mask edge
    img = kerb_image()
    img[255:260] = 110.
    rec = only(run3([street()], image=img), 'road1')
    assert abs(rec['width_m']-8.5) < .15 and rec['kerb']['flag'] in ('kerb_not_found:lo', 'kerb_not_found:hi'), (rec['width_m'], rec['kerb'])
    # the extension never enters another object: a confirmed footpath laid over the top kerb strip stops that side at the
    # mask edge (no covers_decided exit, no footpath under the band)
    fp = obj('fp', 'footpath', rect(60, 245, 540, 256.5), status='confirmed')       # 0.35 m clear of the mask edge
    rec = only(run3([street(), fp], image=kerb_image()), 'road1')
    assert 'proposal' in rec and abs(rec['width_m']-8.5) < .15, (rec['exit_reasons'], rec.get('width_m'), rec.get('kerb'))
    assert Polygon(rec['proposal']['ring']).intersection(Polygon(fp['ring'])).area*MPP*MPP <= V3['blocker_eps_m2']
    # a side whose kerb fall shows on under 30 % of the clean stations (a light patch 8 m long on a 48 m street) keeps the
    # mask edge: one patch is no kerb line
    img[255:260, 100:180] = 170.
    rec = only(run3([street()], image=img), 'road1')
    k = rec['kerb']
    side = 'lo' if k['found_lo'] < k['found_hi'] else 'hi'                 # the patched (top) side
    assert 0 < k[f'found_{side}'] < .3*k['clean'] and k[f'ext_{side}_m'] is None, k
    assert abs(rec['width_m']-8.5) < .15, (rec['width_m'], rec['kerb'])


def scene_meta(sid, east_m):
    lat0 = -37.8
    lon = 145.+east_m*360./(WORLD_M*np.cos(np.radians(lat0)))
    return {'id': sid, 'centroid': {'lon': lon, 'lat': lat0}, 'pixel_to_meters': [[.1, 0.], [0., -.1], [-30., 30.]],
            'meters_to_pixel': [[10., 0.], [0., -10.], [300., 300.]], 'image_width': W, 'image_height': H,
            'region': {'bounds': [0, 0, 1, 1]}}


def bands_of(rep, sid, layer='review'):
    out = []
    for p in rep['proposals']:
        x = p.pop('_xwin')
        out.append({'scene': sid, 'layer': layer, 'seed': p['seed'], 'proposal': p['proposal']['id'], 'width_m': p['width_m'], **x})
    return out


def test_v3_one_street_one_width_across_windows():
    # 1: window B lies 20 m east of window A; the same street (reserve y 230-370 in both) is masked 8 m wide in A and 7 m
    # (centre 1 m lower) in B. Pooled: one width and one centre line for both bands, each naming the other.
    sa, sb = scene_meta('A', 0.), scene_meta('B', 20.)
    road_a = obj('ra', 'road', rect(60, 260, 540, 340))
    road_b = obj('rb', 'road', rect(10, 275, 380, 345))
    img_a, img_b = kerb_image(), kerb_image(275, 345)                  # B reads its kerbs 1 m lower (centre y 310)
    dm = {**V3, 'offset_source': 'mask'}                                # each window centres on its own mask
    rep_a, rep_b = run3([road_a], image=img_a, design=dm), run3([road_b], image=img_b, design=dm)
    wa, wb = only(rep_a, 'ra')['width_m'], only(rep_b, 'rb')['width_m']
    assert abs(wa-wb) > .5
    overrides, groups, pairs = rc.xwin_pool(bands_of(rep_a, 'A')+bands_of(rep_b, 'B'), {'A': sa, 'B': sb}, dm)
    assert len(groups) == 1 and pairs and pairs[0]['same'], pairs
    ra = only(run3([road_a], image=img_a, xwin=overrides['A']['review'], design=dm), 'ra')
    rb = only(run3([road_b], image=img_b, xwin=overrides['B']['review'], design=dm), 'rb')
    assert 'proposal' in ra and 'proposal' in rb, (ra['exit_reasons'], rb['exit_reasons'])
    assert ra['width_m'] == rb['width_m'] == groups[0]['width_m'], (ra['width_m'], rb['width_m'])
    assert ra['shared_with'][0]['scene'] == 'B' and rb['shared_with'][0]['scene'] == 'A'
    # one centre line: A's axis mapped into B lies on B's axis
    from scripts.ml.scene_recapture import frame_transform
    t = np.asarray(frame_transform(sa, sb)['pixel_affine'], float)
    ax_a = np.asarray(ra['axis_px'], float) @ t[:2]+t[2]
    assert abs(ax_a[:, 1].mean()-np.mean([y for _, y in rb['axis_px']]))*MPP < .1
    # the pooled stations' median centre (A holds more stations: y ~300), not B's own centre (y 310)
    assert ra['offset_sources'] == ['shared'] and abs(np.mean([y for _, y in rb['axis_px']])-300) < 2, rb['axis_px']
    # a street in another reserve of B (no common medial line) is not pooled
    reserve_b2 = box(0, 430, W, 570)
    rep_c = run3([obj('rc', 'road', rect(10, 460, 380, 540))], reserve=reserve_b2, lots={'numbered_lot': box(0, 0, W, 430)})
    _, groups2, _ = rc.xwin_pool(bands_of(run3([road_a]), 'A')+bands_of(rep_c, 'B'), {'A': sa, 'B': sb}, V3)
    assert not groups2


def tree(oid, x0, y0, x1, y1):
    return obj(oid, 'tree', rect(x0, y0, x1, y1))


def test_v3_band_continues_through_the_canopy():
    # 3b: the road stops at a canopy that covers the street to the frame edge (reserve goes on) -> the band runs to the frame
    road = obj('r', 'road', rect(60, 260, 330, 340))
    canopy = tree('t', 330, 240, 600, 360)
    rec = only(run3([road, canopy]), 'r')
    assert 'proposal' in rec, rec['exit_reasons']
    band = Polygon(rec['proposal']['ring'])
    assert band.bounds[2] >= 599 and rec['continuation'][0]['to'] == 'frame', rec.get('continuation_log')
    # the IoU gate counts the continuation as band: a 20 m road continued 34 m to the frame is no low_iou exit
    rec = only(run3([obj('r2', 'road', rect(60, 260, 260, 340)), tree('t2', 260, 240, 600, 360)]), 'r2')
    assert 'proposal' in rec and rec['iou_amodal_mask_only'] < V3['iou_gate'] <= rec['iou_amodal'], (rec['exit_reasons'], rec.get('iou_amodal'))
    # ... to the next road piece on the axis that may not join (a crossing street), and no further
    cross = obj('x', 'road', rect(480, 0, 560, 600), score=.3)
    rec = only(run3([road, canopy, cross], reserve=RESERVE.union(box(470, 0, 570, H)),
                    lots={'numbered_lot': box(0, 0, 470, 230).union(box(570, 0, W, 230)).union(box(0, 370, 470, H)).union(box(570, 370, W, H))}), 'r')
    assert 'proposal' in rec and rec['continuation'][0]['to'] == 'x' and 479 <= Polygon(rec['proposal']['ring']).bounds[2] <= 482
    # a building, or a footpath candidate > 2 m2, in the path: no continuation
    for blocker in (obj('b', 'building', rect(450, 280, 500, 320)), obj('fp', 'footpath', rect(450, 290, 480, 310))):
        rec = only(run3([road, canopy, blocker]), 'r')
        assert not rec.get('continuation') and Polygon(rec['proposal']['ring']).bounds[2] <= 331, rec.get('continuation_log')
    # 3a: a collinear piece across a canopy on ANOTHER reserve segment (the reserve jogs 2 m at a side lane) joins
    reserve = box(0, 230, 300, 370).union(box(300, 250, W, 390)).union(box(280, 0, 320, 250))
    lots = {'numbered_lot': box(0, 0, W, H).difference(reserve)}
    a = obj('a', 'road', rect(60, 262, 255, 342), score=.8)
    b = obj('b', 'road', rect(345, 268, 540, 348), score=.6)
    rep = run3([a, b, tree('t', 250, 230, 350, 400)], reserve=reserve, lots=lots)
    v2 = run3([a, b, tree('t', 250, 230, 350, 400)], reserve=reserve, lots=lots, design=V2)
    assert not any(p.get('joined') for p in v2['proposals'])            # v2: another segment, no join
    rec = rep['exits'][0] if rep['exits'] else rep['proposals'][0]
    assert set(rec['joined']) & {'a', 'b'} and rec['canopy_join_log'][0]['joined'], [(x['seed'], x.get('canopy_join_log')) for x in rep['exits']]
    assert any(a['why'] == 'joined across a canopy' for x in rep['proposals'] for a in x['proposal']['absorbs']) or not rep['proposals']
    # vegetation in the gap counts as canopy in v3 (v2 does not join across grass)
    a2, b2 = obj('a', 'road', rect(60, 260, 255, 340), score=.8), obj('b', 'road', rect(345, 260, 540, 340), score=.6)
    rep = run3([a2, b2, obj('g', 'vegetation', rect(250, 240, 350, 360))])
    assert len(rep['proposals']) == 1 and rep['proposals'][0]['joined'], [e['exit_reasons'] for e in rep['exits']]


def test_v3_narrow_band_needs_kerbs_or_a_mirror():
    # 4: a 3.5 m strip in a 14 m reserve with no kerb in the image -> lane_slice; with both kerbs seen -> a band
    lane = obj('l', 'road', rect(60, 283, 540, 318))
    rec = only(run3([lane], image=kerb_image(kerb=110.)), 'l')
    assert rec['exit'] == 'lane_slice' and rec['narrow_check']['narrow'], rec['exit_reasons']
    rec = only(run3([lane], image=kerb_image(283, 318)), 'l')
    assert 'proposal' in rec and rec['narrow_check']['two_kerb_stations'] >= 5, rec['exit_reasons']
    # under half the reserve minus 2 x 2 m footpaths: 6.5 m in a 20 m reserve (limit 8 m), no kerb -> lane_slice
    wide = box(0, 200, W, 400)
    lots = {'numbered_lot': box(0, 0, W, 200).union(box(0, 400, W, H))}
    rec = only(run3([obj('m', 'road', rect(60, 268, 540, 333))], image=kerb_image(kerb=110.), reserve=wide, lots=lots), 'm')
    assert rec['exit'] == 'lane_slice' and rec['narrow_check']['reserve_limit_m'] == 8., rec['narrow_check']
    # the same strip in the 14 m reserve (limit 5 m) is no slice
    rec = only(run3([obj('m', 'road', rect(60, 268, 540, 333))], image=kerb_image(kerb=110.)), 'm')
    assert 'proposal' in rec and not rec['narrow_check']['narrow'], rec['exit_reasons']


def test_v3_cars_under_only_at_90_percent_and_indent_bays():
    # 5a: a car half over the kerb line (no bay: grass beyond) is no longer 'under' the band (v2 counted it at >= 50 %)
    half = car('h', 300, 330, 345, 350)
    rec = only(run3([obj('r', 'road', rect(60, 260, 540, 340)), half, obj('g', 'vegetation', rect(60, 341, 540, 369))]), 'r')
    assert 'proposal' in rec and 'h' not in rec['proposal']['cars_under'] and [c['id'] for c in rec['cars_straddling']] == ['h']
    v2 = only(run2([obj('r', 'road', rect(60, 260, 540, 340)), half]), 'r')
    assert 'h' in v2['proposal']['cars_under']
    # 5b: a parking bay indented 0.9 m into the verge over 14 m, a parked car in it: that side widens by <= 1 m
    ring = [[60, 260], [540, 260], [540, 340], [420, 340], [420, 349], [280, 349], [280, 340], [60, 340]]
    parked = car('p', 320, 330, 365, 349)
    rec = only(run3([obj('r', 'road', ring), parked]), 'r')
    assert 'proposal' in rec, rec['exit_reasons']
    assert [b['car'] for b in rec['indent_bays']] == ['p'] and 'p' in rec['proposal']['cars_under'], (rec.get('indent_seen'), rec['width_m'])
    assert 8.9 <= rec['width_m'] <= 9.0 and Polygon(rec['proposal']['ring']).bounds[3] >= 349
    # the same car on a driveway crossover (a driveway candidate in the bay strip) is no bay: no widening
    rec = only(run3([obj('r', 'road', ring), parked, obj('dw', 'driveway', rect(300, 341, 390, 349))]), 'r')
    assert not rec.get('indent_bays') and rec['width_m'] == 8. and 'p' not in rec['proposal']['cars_under'], rec.get('indent_seen')


# --- 6: the guards the v2 code skeptic found unpinned (v2/skeptic-notes.md), on v2 and v3 ---------------------------------------

def test_v3_guard_a_decided_footpath_mid_band_blocks():
    fp = obj('fp', 'footpath', rect(280, 285, 330, 315), status='confirmed')     # not a twin (IoU ~0.05)
    for design in (V2, V3):
        rec = only(run([street(), fp], design=design), 'road1')
        assert rec['exit'] == 'covers_decided' and any(b['id'] == 'fp' for b in rec['blockers_decided']), rec['exit_reasons']


def test_v3_guard_b_candidate_driveway_swallowed_exits():
    dw = obj('dw', 'driveway', rect(250, 280, 330, 320))                          # 32 m2, all of it under the band
    for design in (V2, V3):
        rec = only(run([street(), dw], design=design), 'road1')
        assert rec['exit'] == 'covers_other_class' and rec['blockers_soft'][0]['id'] == 'dw', rec['exit_reasons']


def test_v3_guard_building_under_band():
    bld = obj('b1', 'building', rect(280, 330, 330, 420))
    for design in (V2, V3):
        rec = only(run([street(), bld], design=design), 'road1')
        assert rec['exit'] == 'covers_blocker' and rec['blockers_hard'][0]['id'] == 'b1', (design['version'], rec['exit_reasons'])


def test_v3_guard_car_park_inside_the_reserve():
    # perpendicular cars side by side inside the reserve outnumber the parallel ones -> car_park (no parcel test needed)
    cars = [car(f'p{i}', 200+25*i, 290, 220+25*i, 335) for i in range(4)]
    for design in (V2, V3):
        rec = only(run([street(), *cars], design=design), 'road1')
        assert rec['exit'] == 'car_park' and rec['car_park']['why'].startswith('perpendicular cars outnumber'), rec['exit_reasons']


def test_v3_guard_edge_trim_is_capped_at_1_m():
    # vegetation 1.5 m inside the top edge for 30 m: deeper than edge_trim_max_m -> no trim, the band exits
    for design in (V2, V3):
        rec = only(run([street(), obj('veg', 'vegetation', rect(150, 260, 450, 275))], design=design), 'road1')
        assert rec['exit'] == 'covers_footpath' and not rec.get('footpath_trims'), (design['version'], rec['exit_reasons'], rec.get('width_m'))


def test_v3_guard_mirror_only_when_the_frame_does_not_cut_the_reserve():
    # the canopy-half street of test 7d, but the reserve runs off the frame bottom: its medial line is no street centre
    half = obj('h', 'road', rect(60, 260, 540, 300))
    reserve = box(0, 230, W, H)
    lots = {'numbered_lot': box(0, 0, W, 230)}
    for design in (V2, V3):
        rec = only(run([half, obj('t', 'tree', rect(40, 298, 560, 345))], design=design, reserve=reserve, lots=lots), 'h')
        assert rec.get('sections_mirrored') == 0 and rec.get('width_source') != 'mirrored', (rec.get('sections_mirrored'), rec.get('width_m'))


def test_v3_guard_join_angle_10_degrees():
    from shapely import affinity
    # a 12 m piece rotated about its own centre right after the seed (the canopy-half lateral rule passes at both angles)
    a = obj('a', 'road', rect(60, 260, 290, 340), score=.8)
    for deg, joins in ((8., True), (12., False)):
        b = affinity.rotate(Polygon(rect(300, 260, 420, 340)), deg, origin=(360, 300))
        rep = run([a, obj('b', 'road', poly_ring(b), score=.6), car('gc', 286, 290, 306, 310)], design=V2)
        rec = only(rep, 'a')
        assert ('b' in (rec.get('joined') or [])) is joins, (deg, rec.get('join_log'), rec.get('exit_reasons'))


def test_v3_guard_long_bare_gap_is_not_joined():
    a = obj('a', 'road', rect(60, 260, 300, 340), score=.8)                 # the larger piece: the seed
    for gap_px, joins in ((40, True), (80, False)):                          # 4 m and 8 m of nothing between the pieces
        b = obj('b', 'road', rect(300+gap_px, 260, 540, 340), score=.6)
        rep = run([a, b], design=V2)
        rec = only(rep, 'a')
        assert ('b' in (rec.get('joined') or [])) is joins, (gap_px, rec.get('join_log'))


def test_v3_guard_arm_min_20_m_junction():
    # a crossroads of two 6 m streets, 22 m across: its longest straight arm is 15 m (< arm_min_m 20) -> the exit is
    # 'junction' itself (with the arm minimum at 0 the same seed gets a band on the 15 m arm)
    cross = Polygon(rect(190, 270, 410, 330)).union(Polygon(rect(270, 190, 330, 410)))
    reserve = box(0, 230, W, 370).union(box(230, 0, 370, H))
    lots = {'numbered_lot': box(0, 0, W, H).difference(reserve)}
    for design in (V2, V3):
        rec = only(run([obj('x', 'road', poly_ring(cross))], design=design, reserve=reserve, lots=lots), 'x')
        assert rec['exit'] == 'junction' and rec['exit_reasons'] == ['junction'], rec.get('exit_reasons')
        assert rec['junction']['is_junction'] and abs(rec['junction']['arm_m']-15.) <= 1., rec['junction']


def test_v3_v1_v2_unchanged_and_deterministic():
    assert rc.DESIGNS['v1'] is rc.DESIGN and rc.DESIGNS['v2'] is rc.DESIGN_V2 and rc.DESIGN_V2['version'] == 'road-corridor-v2'
    objects = [street(), car('c1', 201, 261, 244, 279), obj('t', 'tree', rect(400, 330, 440, 380)),
               obj('dw', 'driveway', copy.deepcopy(street()['ring']))]
    a = json.dumps(rc.jsonable(run3(objects, image=kerb_image())), sort_keys=True)
    b = json.dumps(rc.jsonable(run3(list(reversed(copy.deepcopy(objects))), image=kerb_image())), sort_keys=True)
    assert a == b


def test_v3_footpath_trim_takes_the_least_depth():
    # addition: a vegetation candidate bulging 0.6 m into the top edge over 6 m (3.6 m2 under the band): v3 pulls the edge
    # back only until <= 2 m2 of it is left under the band (about 0.27 m); v2 clears the whole bulge (0.65 m)
    veg = obj('veg', 'vegetation', rect(300, 254, 360, 266))
    rec3 = only(run3([street(), veg]), 'road1')
    rec2 = only(run2([street(), veg]), 'road1')
    assert 'proposal' in rec3 and 'proposal' in rec2, (rec3['exit_reasons'], rec2['exit_reasons'])
    assert rec2['width_m'] <= 7.4 and 7.6 <= rec3['width_m'] <= 7.85, (rec2['width_m'], rec3['width_m'])
    under = Polygon(rec3['proposal']['ring']).intersection(Polygon(veg['ring'])).area*MPP*MPP
    assert 1.5 <= under <= V3['footpath_eps_m2']+.05, under


def test_v3_cross_window_retrim_keeps_one_width():
    # 1 (third pass): window A has a candidate footpath reaching 0.6 m over the top mask edge, window B does not.
    # After the pooled pass A's guard trims its copy; xwin_retrim narrows the whole group by that trim, so A and B end at
    # one width again (and A no longer needs a trim)
    sa, sb = scene_meta('A', 0.), scene_meta('B', 20.)
    road_a, road_b = obj('ra', 'road', rect(60, 260, 540, 340)), obj('rb', 'road', rect(10, 260, 380, 340))
    fp = obj('fp', 'footpath', rect(250, 252, 400, 266))
    rep_a, rep_b = run3([road_a, fp], image=kerb_image()), run3([road_b], image=kerb_image())
    scenes = {'A': sa, 'B': sb}
    overrides, groups, _ = rc.xwin_pool(bands_of(rep_a, 'A')+bands_of(rep_b, 'B'), scenes, V3)
    ra = only(run3([road_a, fp], image=kerb_image(), xwin=overrides['A']['review']), 'ra')
    rb = only(run3([road_b], image=kerb_image(), xwin=overrides['B']['review']), 'rb')
    assert ra['footpath_trims'] and ra['width_m'] < rb['width_m']-.3, (ra['width_m'], rb['width_m'])
    retrim, notes = rc.xwin_retrim(overrides, groups, {('A', 'review', 'ra'): ra, ('B', 'review', 'rb'): rb}, scenes)
    assert notes and set(retrim) == {'A', 'B'}
    ra3 = only(run3([road_a, fp], image=kerb_image(), xwin=retrim['A']['review']), 'ra')
    rb3 = only(run3([road_b], image=kerb_image(), xwin=retrim['B']['review']), 'rb')
    assert 'proposal' in ra3 and 'proposal' in rb3, (ra3['exit_reasons'], rb3['exit_reasons'])
    assert ra3['width_m'] == rb3['width_m'] and not ra3.get('footpath_trims') and ra3['xwin_trim'], (ra3['width_m'], rb3['width_m'], ra3.get('footpath_trims'))


def test_v3_continuation_linked_bands_share_one_width():
    # 3 + 1 in one window: two CONFIRMED roads of different masks (decided pieces never join), 8 m and 9 m, on one street
    # with a canopy between them. a's band continues through the canopy to b; the two bands then share one width and line
    a = obj('a', 'road', rect(60, 260, 250, 340), status='confirmed', score=.8)
    b = obj('b', 'road', rect(350, 255, 540, 345), status='confirmed', score=.6)
    rep = run3([a, b, tree('t', 250, 235, 350, 365)])
    ra, rb = only(rep, 'a'), only(rep, 'b')
    assert 'proposal' in ra and 'proposal' in rb, (ra['exit_reasons'], rb['exit_reasons'])
    ids = {'a', 'b', f"band:{ra['proposal']['id']}", f"band:{rb['proposal']['id']}"}
    assert not ra.get('joined') and not rb.get('joined') and any(c['to'] in ids for c in ra['continuation']+rb['continuation'])
    assert ra['width_m'] == rb['width_m'] and ra['linked_with'][0]['seed'] == 'b' and rep['linked_groups'], (ra['width_m'], rb['width_m'])


def test_v3_narrow_slice_compares_mask_widths():
    # a 5.6 m lane of an 8 m street (two road masks): both lane edges show a kerb-like light line, so the outer-kerb width
    # is 6.6 m (above 0.75 x 8); the slice test compares the 5.6 m MASK width with the street -> still a slice
    lane_a = obj('lane_a', 'road', rect(60, 250, 540, 306), score=.8)
    lane_b = obj('lane_b', 'road', rect(60, 306, 540, 330), score=.5)
    rec = only(run3([lane_a, lane_b], image=kerb_image(250, 306)), 'lane_a')
    assert rec['width_v2_median_m'] < .75*rec['slice']['street_width_m'] <= rec.get('width_own_m', 0), rec.get('slice')
    assert rec['slice']['narrow_slice'] and 'slice' in rec['exit_reasons'], rec['exit_reasons']


# --- road corridor v3.1 (design 'v3.1'): (1) a member's own footpath trim narrows that member only (no group retrim), with
# 'width_mismatch_m' / 'trimmed_by'; (2) a CANDIDATE footpath / vegetation lying over the kerb read in the image (edge more than
# 0.2 m inside it on >= 30 % of the clean stations) no longer stops the kerb extension. Mutation proof in the session folder
# .run/session-2026-10-07-roads/v31/mut/. The v3 tests above stay v3 tests (test_v3_cross_window_retrim_keeps_one_width pins
# v3's max rule, which v3 keeps).

V31 = rc.DESIGN_V31


def over_kerb_image():
    """kerb_image with a 0.8 m light kerb strip: the fall is read at 0.8 m above the 8 m mask (y 260) and 0.76 m below it."""
    return kerb_image(kerb_m=.8)


def footpath_over_kerb(entry_m, x0=60, x1=540, status='candidate', kind='footpath'):
    """A footpath along the top kerb whose edge lies entry_m beyond the mask edge (y 260): with the kerb at 0.8 m it lies
    0.8 - entry_m inside the kerb line."""
    return obj('fp', kind, rect(x0, 235, x1, 260-entry_m/MPP), status=status)


def test_v31_candidate_over_kerb_depth_threshold():
    road = obj('r', 'road', rect(60, 260, 540, 340))
    # 0.21 m inside the kerb (edge 0.59 m beyond the mask edge): lifted, the top side reads its kerb, no trim back off it
    rec = only(run3([road, footpath_over_kerb(.59)], image=over_kerb_image(), design=V31), 'r')
    assert 'proposal' in rec and rec['kerb']['ext_hi_m'] == .8 and abs(rec['width_m']-9.56) <= .05, (rec['exit_reasons'], rec.get('kerb'), rec.get('width_m'))
    cok = rec['candidate_over_kerb']
    assert [(c['id'], c['side']) for c in cok] == [('fp', 'hi')] and cok[0]['stations_over'] >= .9*cok[0]['stations'], cok
    # the edge is measured exactly along the ray (0.21 m), not on the 0.04 m probe grid (which would read 0.20)
    assert abs(cok[0]['over_m_median']-.21) <= .005 and not rec.get('footpath_trims') and rec['candidate_over_kerb_under_band'], (cok, rec.get('footpath_trims'))
    # v3 on the same scene: the candidate stops the extension (the top kerb is not found)
    v3 = only(run3([road, footpath_over_kerb(.59)], image=over_kerb_image()), 'r')
    assert v3['kerb']['ext_hi_m'] is None and v3['width_m'] <= 8.8 and not v3.get('candidate_over_kerb'), v3['kerb']
    # 0.19 m inside the kerb (edge 0.61 m beyond): not over the kerb; it stops the extension as in v3
    rec = only(run3([road, footpath_over_kerb(.61)], image=over_kerb_image(), design=V31), 'r')
    assert not rec.get('candidate_over_kerb') and not rec.get('candidate_over_kerb_below'), rec.get('candidate_over_kerb_below')
    assert rec['kerb']['ext_hi_m'] is None and rec['kerb']['flag'] == 'kerb_not_found:hi' and rec['width_m'] <= 8.8, (rec['kerb'], rec['width_m'])
    # the other side: a footpath 0.215 m inside the bottom kerb (read at 0.76 m; its edge 0.545 m beyond the mask edge, between
    # two 0.04 m probes) is lifted on 'lo', no trim back off it
    rec = only(run3([road, obj('fp2', 'footpath', rect(60, 345.45, 540, 365))], image=over_kerb_image(), design=V31), 'r')
    assert [(c['id'], c['side']) for c in rec.get('candidate_over_kerb') or []] == [('fp2', 'lo')] and rec['kerb']['ext_lo_m'] == .76, rec.get('kerb')
    assert not rec.get('footpath_trims') and abs(rec['width_m']-9.56) <= .05, (rec.get('footpath_trims'), rec['width_m'])


def test_v31_candidate_over_kerb_share_threshold():
    # 49 clean stations (x 60..540 every 1 m); the candidate (0.49 m inside the kerb) covers 15 of them (30.6 %) -> lifted;
    # 14 (28.6 %) -> stays a stop (recorded 'below'), and the band, built to the kerb read elsewhere, is trimmed back off it
    road = obj('r', 'road', rect(60, 260, 540, 340))
    rec = only(run3([road, footpath_over_kerb(.31, 95, 245)], image=over_kerb_image(), design=V31), 'r')
    cok = rec.get('candidate_over_kerb') or []
    assert [(c['id'], c['stations_over'], c['stations']) for c in cok] == [('fp', 15, 49)], (cok, rec.get('candidate_over_kerb_below'))
    assert 'proposal' in rec and not rec.get('footpath_trims') and abs(rec['width_m']-9.56) <= .05, (rec.get('footpath_trims'), rec['width_m'])
    rec = only(run3([road, footpath_over_kerb(.31, 95, 235)], image=over_kerb_image(), design=V31), 'r')
    below = rec.get('candidate_over_kerb_below') or []
    assert not rec.get('candidate_over_kerb') and [(c['id'], c['stations_over'], c['stations']) for c in below] == [('fp', 14, 49)], below
    assert 'proposal' in rec and [t['blocker'] for t in rec.get('footpath_trims') or []] == ['fp'] and rec['width_m'] <= 9.35, (rec.get('footpath_trims'), rec['width_m'])


def test_v31_decided_footpath_still_stops():
    # the over-kerb footpath of the tests above (0.49 m inside the kerb along the whole street), but CONFIRMED: it stops the
    # top kerb extension as in v3 and is never lifted; a candidate driveway (another stop kind) in the same place stops too
    road = obj('r', 'road', rect(60, 260, 540, 340))
    dm = {**V31, 'offset_source': 'mask'}                                # centre on the mask so the band edge sits on the stop
    for fp in (footpath_over_kerb(.31, status='confirmed'), footpath_over_kerb(.31, kind='driveway')):
        rec = only(run3([road, fp], image=over_kerb_image(), design=dm), 'r')
        assert not rec.get('candidate_over_kerb') and not rec.get('candidate_over_kerb_below'), (fp['status'], rec.get('candidate_over_kerb'))
        assert rec['kerb']['ext_hi_m'] is None and rec['kerb']['flag'] == 'kerb_not_found:hi' and rec['width_m'] <= 8.8, (fp['kind'], rec['kerb'])
        assert 'proposal' in rec and Polygon(rec['proposal']['ring']).intersection(Polygon(fp['ring'])).area*MPP*MPP <= V31['blocker_eps_m2'], rec['exit_reasons']
    # the same object as a candidate footpath is lifted (the control)
    rec = only(run3([road, footpath_over_kerb(.31)], image=over_kerb_image(), design=dm), 'r')
    assert [c['id'] for c in rec.get('candidate_over_kerb') or []] == ['fp'] and rec['kerb']['ext_hi_m'] == .8, rec.get('kerb')


def two_windows(objs, imgs, design, seeds=('ra', 'rb')):
    """xwin_passes over the synthetic windows A and B (B 20 m east of A, the same reserve); returns the final records."""
    scenes = {'A': scene_meta('A', 0.), 'B': scene_meta('B', 20.)}

    def run_window(sid, xw):
        rep = run3(copy.deepcopy(objs[sid]), image=imgs[sid], xwin=(xw or {}).get('review'), design=design)
        return {'review': rep}, bands_of(rep, sid)
    final, _, _, groups, _, notes = rc.xwin_passes(run_window, ['A', 'B'], scenes, design)
    return only(final[('A', 'review')], seeds[0]), only(final[('B', 'review')], seeds[1]), groups, notes


def test_v31_shared_street_keeps_each_kerb_width():
    # the d43d / lot-25641 shape: one street (kerb to kerb 9.56 m) in two windows; window A holds a candidate footpath 0.49 m
    # inside the top kerb along its whole length, window B does not (and holds more stations, so the pooled width is B's)
    road_a, road_b = obj('ra', 'road', rect(60, 260, 400, 340)), obj('rb', 'road', rect(10, 260, 540, 340))
    objs = {'A': [road_a, footpath_over_kerb(.31, 60, 400)], 'B': [road_b]}
    imgs = {'A': over_kerb_image(), 'B': over_kerb_image()}
    # v3: A's kerb read stops at the footpath, the pooled band is trimmed in A and the group retrim narrows B by the same trim
    a3, b3, _, notes3 = two_windows(objs, imgs, V3)
    assert 'proposal' in a3 and 'proposal' in b3 and notes3 and a3['xwin_trim'], (a3['exit_reasons'], b3['exit_reasons'], notes3)
    assert max(a3['width_m'], b3['width_m']) <= 9.3 and abs(a3['width_m']-b3['width_m']) <= .02, (a3['width_m'], b3['width_m'])
    # v3.1: A reads its kerb through the candidate; each member gets its own kerb width, no trim, no mismatch
    a, b, groups, notes = two_windows(objs, imgs, V31)
    assert 'proposal' in a and 'proposal' in b and not notes, (a['exit_reasons'], b['exit_reasons'])
    assert abs(a['width_m']-9.56) <= .05 and abs(b['width_m']-9.56) <= .05 and a['width_own_m'] == b['width_own_m'], (a['width_m'], b['width_m'], a['width_own_m'])
    assert [c['id'] for c in a['candidate_over_kerb']] == ['fp'] and not a.get('footpath_trims') and not a.get('width_mismatch_m'), a.get('footpath_trims')
    assert len(groups) == 1 and a['shared_with'][0]['scene'] == 'B' and not b.get('width_mismatch_m')


def test_v31_member_trim_narrows_that_member_only():
    # the v3 retrim scene (test_v3_cross_window_retrim_keeps_one_width): window A has a candidate footpath reaching 0.6 m over
    # its top mask edge (no kerb read there: not an over-kerb case), B has none. v3 narrows both to A's trim; v3.1 trims A only
    road_a, road_b = obj('ra', 'road', rect(60, 260, 540, 340)), obj('rb', 'road', rect(10, 260, 380, 340))
    objs = {'A': [road_a, obj('fp', 'footpath', rect(250, 252, 400, 266))], 'B': [road_b]}
    imgs = {'A': kerb_image(), 'B': kerb_image()}
    a3, b3, _, _ = two_windows(objs, imgs, V3)
    assert a3['width_m'] == b3['width_m'] and a3['xwin_trim'], (a3['width_m'], b3['width_m'])
    a, b, groups, notes = two_windows(objs, imgs, V31)
    assert 'proposal' in a and 'proposal' in b and not notes and not a.get('candidate_over_kerb'), (a['exit_reasons'], b['exit_reasons'])
    group_w = groups[0]['width_m']
    assert b['width_m'] == group_w and not b.get('width_mismatch_m') and a['width_m'] < b['width_m']-.3, (a['width_m'], b['width_m'], group_w)
    assert a['width_mismatch_m'] == round(group_w-a['width_m'], 2), (a.get('width_mismatch_m'), group_w, a['width_m'])
    assert [(t['blocker'], t['edge']) for t in a['trimmed_by']] == [(t['blocker'], t['edge']) for t in a['footpath_trims']] and a['trimmed_by'][0]['blocker'] == 'fp'
    assert b3['width_m'] < b['width_m'], (b3['width_m'], b['width_m'])                # v3 narrowed B by A's trim
    m = rc.xwin_members_final(copy.deepcopy(groups), {('A', 'review'): {'proposals': [a], 'exits': []}, ('B', 'review'): {'proposals': [b], 'exits': []}})[0]['members']
    assert [(x['scene'], x['width_mismatch_m']) for x in m] == [('A', a['width_mismatch_m']), ('B', None)], m


def test_v31_over_kerb_part_beyond_the_kerb_still_trims():
    # A lifts its over-kerb footpath (kerb to kerb 9.56 m), but B's street reads 10.56 m (a 9 m mask) and B holds more stations:
    # the pooled band runs ~0.5 m past A's kerb line, over the footpath's part beyond it -> A (only) is trimmed back to about
    # its own kerb line (y 252), not to the footpath's inner edge (y 256.9), and records the mismatch
    road_a, road_b = obj('ra', 'road', rect(60, 260, 400, 340)), obj('rb', 'road', rect(10, 255, 540, 345))
    objs = {'A': [road_a, footpath_over_kerb(.31, 60, 400)], 'B': [road_b]}
    imgs = {'A': over_kerb_image(), 'B': kerb_image(255, 345, kerb_m=.8)}
    a, b, groups, _ = two_windows(objs, imgs, V31)
    assert 'proposal' in a and 'proposal' in b and b['width_m'] >= 10.4 and not b.get('width_mismatch_m'), (a['exit_reasons'], b.get('width_m'))
    assert [c['id'] for c in a['candidate_over_kerb']] == ['fp'] and [t['blocker'] for t in a['trimmed_by']] == ['fp'], (a.get('candidate_over_kerb'), a.get('trimmed_by'))
    top = min(y for _, y in a['proposal']['ring'])
    assert 250.5 <= top <= 253.5 and a['width_m'] >= 9.5 and a['width_mismatch_m'] >= .3, (top, a['width_m'], a.get('width_mismatch_m'))


def run31_retired(objects, retired, image=None):
    rep, _, _ = rc.corridor_layer(objects, width=W, height=H, mpp=MPP, reserve=RESERVE, lots=LOTS, design=V31, affine=AFFINE,
                                  image=kerb_image() if image is None else image, retired=retired)
    return rep


def test_v31_retire_option_resolves_the_conflict_and_lists_the_twin(tmp_path):
    # the win-69468 shape: a confirmed road and a confirmed driveway twin of the same street -> p0_conflict, no band
    road = street(status='confirmed')
    twin = obj('dw', 'driveway', copy.deepcopy(road['ring']), status='confirmed')
    base = run31_retired([road, twin], None)
    assert only(base, 'road1')['exit'] == 'p0_conflict' and base['twin_groups'][0]['how'].startswith('conflict')
    assert 'reviewer_retired' not in base and 'retired_by_reviewer' not in base['twin_groups'][0]
    # an empty retire map changes nothing (the option absent reproduces byte-identically)
    dump = lambda rep: json.dumps(rc.jsonable(rep), sort_keys=True, default=str)       # noqa: E731
    assert dump(run31_retired([road, twin], {})) == dump(base)
    # the reviewer retired the driveway: the group resolves to road, the band is built and lists the twin as retires_on_accept
    decision = {'by': 'reviewer', 'entry': 'defaults_accepted.3'}
    rep = run31_retired([road, twin], {'dw': decision})
    g = rep['twin_groups'][0]
    assert g['resolved_class'] == 'road' and g['confirmed_classes_without_retire'] == ['driveway', 'road']
    assert [x['id'] for x in g['retired_by_reviewer']] == ['dw'] and g['retired_by_reviewer'][0]['decision'] == decision
    rec = only(rep, 'road1')
    assert 'proposal' in rec, rec['exit_reasons']
    p = rec['proposal']
    assert p['retires_on_accept'] == ['dw'] and 'dw' in p['derived_from'] and 'dw' not in p['replaces'] and 'road1' in p['replaces_on_accept']
    assert next(x for x in p['xclass_twin'] if x['id'] == 'dw')['action'] == 'retires on accept: retired by the reviewer (--retire)'
    t = rec['retires_on_accept'][0]
    assert t['retired_by'] == 'reviewer' and t['same_street_test'] is True and t['decision'] == decision
    assert not any(b['id'] == 'dw' for b in rec['blockers_decided'])
    assert rep['reviewer_retired'] == [{'id': 'dw', 'found': True, 'kind': 'driveway', 'status': 'confirmed', 'twin_group': g['group'],
                                        'decision': decision, 'retired_by_proposal': [p['id']], 'listed_by_exit': []}]
    # a twin that fails the same-street test (IoU < retire_iou) is still listed when the reviewer retired it (recorded False)
    loose = obj('dw', 'driveway', rect(60, 274, 540, 340), status='confirmed')
    assert only(run31_retired([road, loose], None), 'road1')['exit'] == 'p0_conflict'
    rec = only(run31_retired([road, loose], {'dw': decision}), 'road1')
    assert 'proposal' in rec and rec['proposal']['retires_on_accept'] == ['dw'] and rec['retires_on_accept'][0]['same_street_test'] is False, rec['exit_reasons']
    # a retire naming an object outside the layer is reported, not applied
    rep = run31_retired([road, twin], {'zz': decision})
    assert only(rep, 'road1')['exit'] == 'p0_conflict' and rep['reviewer_retired'][0]['found'] is False
    # v1 / v2 refuse the option
    for design in (rc.DESIGN, rc.DESIGN_V2):
        try:
            rc.corridor_layer([road, twin], width=W, height=H, mpp=MPP, reserve=RESERVE, lots=LOTS, design=design, affine=AFFINE, retired={'dw': decision})
        except ValueError:
            pass
        else:
            raise AssertionError(f'{design["version"]} accepted retired')
    # the decisions-file check (refusals before any scene is read)
    doc = {'source': 'human:chat', 'sentence': 'yes to all defaults', 'marks_by_band_number': {'1': 'right', '8': 'not_applied'},
           'defaults_accepted': {'3': 'the duplicate driveway dw is retired with band 1', '9': 'the twin zz is retired with band 8'}}
    f = tmp_path/'decisions.json'

    def refused(specs, scenes=None, **change):
        f.write_text(json.dumps({**doc, **change}))
        try:
            rc.retire_decisions(specs, str(f), scenes)
        except ValueError as e:
            return str(e)
        return None
    assert 'needs --decisions' in _no_decisions()
    assert 'not a human decision' in refused(['s:dw'], source='agent')
    assert 'expected SCENE:OBJECT_ID' in refused(['dw'])
    assert 'not in --scenes' in refused(['s:dw'], scenes=['t'])
    assert 'records no retire of qq' in refused(['s:qq'])
    assert "marked 'not_applied' not right" in refused(['s:zz'])


def _no_decisions():
    try:
        rc.retire_decisions(['s:dw'], None)
    except ValueError as e:
        return str(e)
    return ''


# --- road corridor v3.2 (design 'v3.2'): the car spur rule. One street shape for all three tests: an 8 m street (56 m long)
# with two 7 m tree gaps in its top kerb (3.2 m deep); in each gap a 5 m x 2 m lobe at the kerb, its west 1.5 m on the street.
# The amodal mask (street + lobes) is the same in every case, so the skeleton, its two 3.16 m spurs and the 18.07 m longest
# straight arm are the same; only what the lobe pixels are (car / road / both / half and half) differs. Mutation proof in
# .run/session-2026-10-07-roads/v32/mut/.

V32 = rc.DESIGN_V32
GAPS = (230, 380)


def lobe(g, y0=262, y1=282):
    return box(g-15, y0, g+35, y1)


def gap_street(extra_road=()):
    from shapely.ops import unary_union
    p = Polygon(rect(20, 260, 580, 340)).difference(box(GAPS[0], 250, GAPS[0]+70, 292)).difference(box(GAPS[1], 250, GAPS[1]+70, 292))
    p = unary_union([p, *extra_road])
    return obj('r', 'road', [list(c) for c in p.exterior.coords[:-1]])


def gap_trees():
    return [tree('t1', GAPS[0]+40, 240, GAPS[0]+70, 290), tree('t2', GAPS[1]+40, 240, GAPS[1]+70, 290)]


def car_of(oid, p):
    return obj(oid, 'car', [list(c) for c in p.exterior.coords[:-1]])


def test_v32_car_bulge_spurs_are_not_a_junction():
    # the band-1 shape (win-69468b2b783cd964 at rev 47): two cars parked at the kerb beside canopies stick out of the road mask;
    # v3.1 reads their two 3.16 m lobes as two side streets (junction, arm 18.07 m < 20 m) and builds no band
    objs = [gap_street(), car_of('c1', lobe(GAPS[0])), car_of('c2', lobe(GAPS[1])), *gap_trees()]
    r31 = only(run3(copy.deepcopy(objs), image=kerb_image(), design=V31), 'r')
    assert r31['exit_reasons'] == ['junction'] and r31['junction']['spur_clusters'] == 2 and r31['junction']['arm_m'] < V31['arm_min_m'], r31['junction']
    assert 'car_spurs' not in r31['junction'] and 'spur_tests' not in r31['junction']               # v3.1 records as before
    # v3.2: both spurs end in car pixels only (no road pixel) -> car spurs, not side streets -> one band over the whole street
    rec = only(run3(copy.deepcopy(objs), image=kerb_image(), design=V32), 'r')
    assert 'proposal' in rec and rec['exit_reasons'] == [], rec['exit_reasons']
    j = rec['junction']
    assert j['is_junction'] is False and j['spur_clusters'] == 0 and j['spurs_ge_3m'] == 0 and 'arm_a' not in j, j
    assert [(c['cars'], c['protrusion_m'] >= V32['junction_spur_m']) for c in j['car_spurs']] == [(['c1'], True), (['c2'], True)], j['car_spurs']
    assert all(c['spur_m'] > c['protrusion_m'] and c['end_region_px'] > 0 for c in j['car_spurs'])
    assert [(t['car_spur'], t['road_px'], t['outside_car_px']) for t in j['spur_tests']] == [(True, 0, 0), (True, 0, 0)], j['spur_tests']
    assert len(rec['spurs_ge_3m']) == 2                          # the raw spurs stay listed; the junction block says which count
    assert abs(rec['width_m']-9.) < .15 and len(rec['proposal']['ring']) == 4, rec['width_m']
    assert {'c1', 'c2'} <= set(rec['proposal']['cars_under'])
    # band ids and the proposal source stay v3.1's (the reviewer's band numbers and scene_road_promote resolve them)
    p = rec['proposal']
    assert p['source'] == 'road-corridor-v3.1' and V32['version'] == 'road-corridor-v3.2' and rc.DESIGNS['v3.2'] is V32
    assert p['id'] == rc.hashlib.sha256(('road-corridor-v3.1:'+','.join(p['derived_from'])).encode()).hexdigest()[:16]


def test_v32_side_street_with_road_pixels_still_exits_junction():
    # the same shape with the lobes made of road pixels (two side-street mouths of the SAM road mask): junction exit as before,
    # with or without a car parked on the road pixels at the spur end
    bare = [gap_street([lobe(GAPS[0]), lobe(GAPS[1])]), *gap_trees()]
    parked = [*copy.deepcopy(bare), car_of('c1', lobe(GAPS[0])), car_of('c2', lobe(GAPS[1]))]
    for objs, cars in ((bare, 0), (parked, 200)):
        r31 = only(run3(copy.deepcopy(objs), image=kerb_image(), design=V31), 'r')
        rec = only(run3(copy.deepcopy(objs), image=kerb_image(), design=V32), 'r')
        assert r31['exit_reasons'] == rec['exit_reasons'] == ['junction'], (r31['exit_reasons'], rec['exit_reasons'])
        j = rec['junction']
        assert j['spur_clusters'] == 2 and j['is_junction'] and j['car_spurs'] == [] and j['arm_m'] == r31['junction']['arm_m'], j
        assert [(t['car_spur'], t['car_px'], t['road_px'] > 0) for t in j['spur_tests']] == [(False, cars, True)]*2, j['spur_tests']


def test_v32_spur_half_in_a_car_half_on_road_is_a_junction():
    # the same shape, each lobe half car (north) and half road (south): the end region holds road pixels -> junction as before;
    # also when the spur's end point itself lies in the car and only the region's edge reaches the road (car 1.4 m, road 0.6 m)
    for split in (272, 276):
        objs = [gap_street([lobe(GAPS[0], split, 282), lobe(GAPS[1], split, 282)]), car_of('c1', lobe(GAPS[0], 262, split)),
                car_of('c2', lobe(GAPS[1], 262, split)), *gap_trees()]
        rec = only(run3(copy.deepcopy(objs), image=kerb_image(), design=V32), 'r')
        assert rec['exit_reasons'] == ['junction'] and rec['junction']['car_spurs'] == [], (split, rec['exit_reasons'], rec['junction'])
        assert all(0 < t['car_px'] < t['region_px'] and t['road_px'] > 0 for t in rec['junction']['spur_tests']), rec['junction']['spur_tests']
        assert only(run3(copy.deepcopy(objs), image=kerb_image(), design=V31), 'r')['exit_reasons'] == ['junction']
    # the test itself (frame px at 0.1 m/px: the region is the last 1 m of the spur buffered by 0.5 m)
    pts = [(260., 272.), (250., 272.)]
    car_box = box(200, 262, 300, 282)

    def test(cars, road):
        return rc.car_spur_test(pts, {'cars': cars, 'car_polys': {'c': cars} if cars is not None else {}, 'road': road}, MPP, V32)
    assert test(car_box, box(0, 0, 10, 10))['car_spur'] is True                            # all car, no road pixel
    assert test(car_box, box(240, 262, 300, 282))['car_spur'] is False                     # road under the car
    half = test(box(200, 262, 255, 282), None)                                             # half car, half nothing (no road)
    assert half['car_spur'] is False and half['road_px'] == 0 and 0 < half['car_px'] < half['region_px'], half
    assert test(None, None)['car_spur'] is False
