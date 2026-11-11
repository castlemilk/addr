"""Shadow-trim sidecar v1 (tree-shadow-2026-10-08, lens D): synthetic frames only, no scene file is read. 0.1 m/px, north up,
a 200 x 200 px frame of lit grey paving; a crown = lit green foliage with 30 % dark-green gaps; a cast shadow = blue-grey dark.
The census thresholds are copied here (census/calibration.json, shadow-census-calibration-v1) so the tests read no data file."""
import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml.experiments import shadow_census as sc
from scripts.ml.experiments import shadow_trim as st
from scripts.ml.scene_extract import APP_RING_RULES, app_ring_issues
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

TH = {'t_v': 0.7197, 't_c_chroma': 0.1412, 't_b': 0.0059, 't_t': 0.1153, 't_g': 0.0519, 't_c': 0.3175}
MPP = 0.1
SIZE = 200
LIN = [[MPP, 0.], [0., -MPP]]
PAVING, FOLIAGE, GAP, SHADOW, GRASS_SHADOW = (150, 150, 145), (90, 130, 60), (30, 50, 25), (40, 50, 70), (35, 62, 30)


def outline(geometry):
    """A shapely polygon as the scenes store it: open ring, 2 dp."""
    g = geometry.simplify(0.3)
    return [[round(x, 2), round(y, 2)] for x, y in g.exterior.coords[:-1]]


def paint(rgb, geometry, colour, rng=None, gap=None, gap_share=0.3):
    x0, y0, x1, y1 = 0, 0, rgb.shape[1], rgb.shape[0]
    inside = st.rasterise(geometry, x0, y0, x1, y1)
    rgb[inside] = colour
    if gap is not None:
        holes = inside & (rng.random(inside.shape) < gap_share)
        rgb[holes] = gap


def frame(tmp_path, crown, shadow=None, shadow_colour=SHADOW, extra=(), name='f.png'):
    rng = np.random.default_rng(7)
    rgb = np.empty((SIZE, SIZE, 3), np.uint8)
    rgb[:] = PAVING
    if shadow is not None:
        paint(rgb, shadow, shadow_colour)
    for geometry, colour in extra:
        paint(rgb, geometry, colour)
    paint(rgb, crown, FOLIAGE, rng, GAP)
    path = tmp_path/name
    Image.fromarray(rgb).save(path)
    return sc.frame_features(path, MPP)


def trim(feats, poly, bearing, **kw):
    return st.trim_object(poly, feats, TH, lin=LIN, width=SIZE, height=SIZE, shadow_bearing=bearing, **kw)


def crown_centres_inside(res, final):
    x0, y0 = res['crop'][:2]
    cy, cx = np.nonzero(res['crown'])
    return all(final.contains(Point(x+x0+.5, y+y0+.5)) for x, y in zip(cx, cy, strict=True))


def test_arc_kernel_follows_compass_bearings():
    k = st.arc_kernel(LIN, 80., 20., 5.)               # east (90) +- 10: offsets to the right only
    c = k.shape[0]//2
    assert k[c, c] and k[c, c+5] and not k[c, c-5] and not k[c-5, c] and not k[c+5, c]
    k = st.arc_kernel(LIN, 170., 20., 5.)              # south (180): image down
    assert k[c+5, c] and not k[c-5, c]


def test_shadow_arc_widens_to_the_area_mean_only_when_close():
    start, span = st.shadow_arc(107.4, 141.7)
    assert start == pytest.approx(77.4) and span == pytest.approx(94.3)
    start, span = st.shadow_arc(190., 300.)            # 110 deg apart: window bearing +- fan only
    assert start == pytest.approx(160.) and span == pytest.approx(60.)
    start, span = st.shadow_arc(10., None)
    assert start == pytest.approx(340.) and span == pytest.approx(60.)


def test_attached_shadow_on_the_sun_opposite_side_is_trimmed(tmp_path):
    crown = Point(80, 80).buffer(25, 64)
    shadow = Point(101, 101).buffer(25, 64)            # SE of the crown: shadows fall to 135 deg
    feats = frame(tmp_path, crown, shadow)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 135.)
    assert res['outcome'] == 'proposal', res['reasons']
    final = Polygon(res['ring'], res['holes'])
    lobe = poly.difference(crown)
    assert final.intersection(lobe).area < 0.1*lobe.area      # most of the shadow lobe is gone
    assert final.intersection(crown).area > 0.97*crown.area   # the crown stays
    assert crown_centres_inside(res, final)
    assert res['removed_blue_dark_share'] > 0.8
    assert app_ring_issues(res['ring'], res['holes'], SIZE, SIZE, rules=APP_RING_RULES) == []
    assert not res['frame_cut_sides']
    cut = poly.difference(final)
    far = [p for p in poly.exterior.coords[:-1] if cut.distance(Point(p)) > 1.0]
    kept = {tuple(p) for p in res['ring']}
    assert far and all(tuple(p) in kept for p in far)      # outline vertices away from the cut are kept exactly


def test_same_shadow_on_the_sun_side_is_not_trimmed(tmp_path):
    crown = Point(80, 80).buffer(25, 64)
    shadow = Point(101, 101).buffer(25, 64)
    feats = frame(tmp_path, crown, shadow)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 315.)                      # light from the SE: the dark lobe lies towards the sun
    assert res['outcome'] == 'no_trim'
    assert res['reasons'] == ['no_shadow_component']


def test_dark_crown_interior_is_not_trimmed(tmp_path):
    crown = Point(100, 100).buffer(35, 64)
    interior = Point(108, 108).buffer(14, 64)          # blue-dark patch inside the crown, towards the shadow side
    rng = np.random.default_rng(7)
    rgb = np.empty((SIZE, SIZE, 3), np.uint8)
    rgb[:] = PAVING
    paint(rgb, crown, FOLIAGE, rng, GAP)
    paint(rgb, interior, SHADOW)
    Image.fromarray(rgb).save(tmp_path/'f.png')
    feats = sc.frame_features(tmp_path/'f.png', MPP)
    poly = Polygon(outline(crown))
    res = trim(feats, poly, 135.)
    assert res['outcome'] == 'no_trim'
    x0, y0 = res['crop'][:2]
    assert res['crown'][int(108-y0), int(108-x0)]       # the interior counts as crown body (holes filled)


def test_green_tinted_shadow_on_grass_is_not_trimmed(tmp_path):
    crown = Point(80, 80).buffer(25, 64)
    shadow = Point(101, 101).buffer(25, 64)
    feats = frame(tmp_path, crown, shadow, shadow_colour=GRASS_SHADOW)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 135.)
    assert res['outcome'] == 'no_trim' and res['reasons'] == ['no_shadow_component']
    assert not any(c.get('accepted') for c in res['components'])   # dark green is shaded foliage / grass: never a candidate


def test_frame_cut_edge_the_crown_reaches_is_kept(tmp_path):
    crown = Point(10, 100).buffer(30, 64).intersection(box(0, 0, SIZE, SIZE))   # the frame cuts the crown on the left
    shadow = Point(40, 100).buffer(25, 64)             # east of the crown, away from the frame
    feats = frame(tmp_path, crown, shadow)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 90.)
    assert res['outcome'] == 'proposal', res['reasons']
    assert res['frame_cut_sides'] == ['left'] and res['frame_cut_crown_sides'] == ['left']
    final = Polygon(res['ring'])
    frame_edge = LineString([(0, 0), (0, SIZE)])
    before_left = poly.exterior.intersection(frame_edge)
    after_left = final.exterior.intersection(frame_edge)
    assert before_left.length > 40 and after_left.equals(before_left)   # the frame-cut edge is untouched
    assert not res['frame_cut_dropped']


def test_shadow_touching_a_frame_side_the_crown_reaches_is_refused(tmp_path):
    crown = Point(60, 185).buffer(25, 64).intersection(box(0, 0, SIZE, SIZE))   # crown cut by the bottom frame
    shadow = Point(90, 190).buffer(25, 64).intersection(box(0, 0, SIZE, SIZE))  # shadow east of it, also on the frame
    feats = frame(tmp_path, crown, shadow)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 90.)
    assert 'bottom' in res['frame_cut_crown_sides']
    assert any(c.get('skip') == 'frame_cut_kept' for c in res['components'])
    assert res['outcome'] == 'no_trim'


def test_frame_cut_edge_wholly_in_shadow_policy(tmp_path):
    crown = Point(100, 150).buffer(22, 64)             # crown inside the frame
    shadow = Point(118, 182).buffer(26, 64).intersection(box(0, 0, SIZE, SIZE))  # its shadow runs off the bottom
    feats = frame(tmp_path, crown, shadow)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 150.)
    assert res['frame_cut_sides'] == ['bottom'] and res['frame_cut_crown_sides'] == []
    assert res['outcome'] == 'proposal' and res['frame_cut_dropped'] == ['bottom']
    strict = trim(feats, poly, 150., policy='keep_always')
    assert strict['outcome'] == 'no_trim'
    assert any(c.get('skip') == 'frame_cut_kept' for c in strict['components'])


def test_trim_over_sixty_percent_is_flagged(tmp_path):
    crown = Point(60, 60).buffer(9, 64)
    shadow = box(55, 55, 150, 150)                     # a large dark area SE of a small crown
    feats = frame(tmp_path, crown, shadow)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 135.)
    assert res['outcome'] == 'flag' and res['reasons'] == ['over_60pct']
    assert res['removed_share'] > 0.6
    assert res.get('ring')                             # the geometry is recorded for the flag, not proposed


def test_outline_without_crown_is_flagged(tmp_path):
    shadow = Point(100, 100).buffer(25, 64)
    feats = frame(tmp_path, Point(20, 20).buffer(1), shadow)
    res = trim(feats, Polygon(outline(shadow)), 135.)
    assert res['outcome'] == 'flag' and res['reasons'] == ['no_crown']


def scene_inputs(tmp_path):
    crown = Point(80, 80).buffer(25, 64)
    shadow = Point(101, 101).buffer(25, 64)
    feats = frame(tmp_path, crown, shadow)
    ring = outline(unary_union([crown, shadow]))
    meta = {'pixel_to_meters': [[MPP, 0.], [0., -MPP], [144.9, -37.8]], 'image_width': SIZE, 'image_height': SIZE}
    sun = {'reliable': True, 'shadow_bearing_used': 135., 'shadow_bearing_source': 'window', 'bootstrap_circ_std_deg_B': 5.}
    row = {'shadow_share': 0.25, 'sun_opposite_share': 1.0, 'shadow_side_excess': 0.3, 'vertices': len(ring)}
    return feats, ring, meta, sun, row


def test_proposal_is_deterministic_and_carries_lineage(tmp_path):
    feats, ring, meta, sun, row = scene_inputs(tmp_path)
    obj = {'id': 'abc', 'kind': 'tree', 'label': 'Tree', 'ring': ring, 'status': 'confirmed', 'geometry_reviewed': True,
           'score': 0.6, 'model': {'height_m': 3.2, 'base_m': 0, 'height_source': 'class-prior', 'height_verified': False}}
    a = st.process_object('win-x', obj, feats, TH, meta, sun, row, area_bearing=140.)
    b = st.process_object('win-x', json.loads(json.dumps(obj)), feats, TH, meta, sun, row, area_bearing=140.)
    assert a['outcome'] == 'proposal'
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    p = a['proposal']
    assert p['replaces_on_accept'] == ['abc'] and p['replaces'] == [] and p['derived_from'] == ['abc']
    assert p['status'] == 'proposal' and p['source'] == st.VERSION and p['model']['height_m'] == 3.2
    undecided = st.process_object('win-x', {**obj, 'status': 'candidate', 'geometry_reviewed': False}, feats, TH, meta, sun, row)
    assert undecided['proposal']['replaces'] == ['abc'] and undecided['proposal']['replaces_on_accept'] == []


def test_guards_sun_uncertain_and_not_eligible(tmp_path):
    feats, ring, meta, sun, row = scene_inputs(tmp_path)
    obj = {'id': 'abc', 'kind': 'tree', 'ring': ring, 'status': 'candidate'}
    out = st.process_object('win-x', obj, feats, TH, meta, {**sun, 'reliable': False}, row)
    assert out['outcome'] == 'guard' and out['reasons'] == ['sun_uncertain']
    out = st.process_object('win-x', obj, feats, TH, meta, sun, {**row, 'shadow_share': 0.05, 'shadow_side_excess': 0.1})
    assert out['outcome'] == 'skip' and out['reasons'] == ['not_eligible']
    out = st.process_object('win-x', obj, feats, TH, meta, sun, None)
    assert out['outcome'] == 'skip' and out['reasons'] == ['not_in_census']


def test_shaded_half_of_a_crown_is_kept_and_the_shadow_beyond_it_removed(tmp_path):
    crown = Point(80, 80).buffer(28, 64)
    shaded = crown.intersection(box(80, 80, 120, 120))  # the crown's own shaded SE quarter: dark green, no lit leaves
    shadow = Point(110, 110).buffer(26, 64)
    rng = np.random.default_rng(7)
    rgb = np.empty((SIZE, SIZE, 3), np.uint8)
    rgb[:] = PAVING
    paint(rgb, shadow, SHADOW)
    paint(rgb, crown, FOLIAGE, rng, GAP)
    paint(rgb, shaded, GAP)
    Image.fromarray(rgb).save(tmp_path/'f.png')
    feats = sc.frame_features(tmp_path/'f.png', MPP)
    poly = Polygon(outline(unary_union([crown, shadow])))
    res = trim(feats, poly, 135.)
    assert res['outcome'] == 'proposal', res['reasons']
    final = Polygon(res['ring'], res['holes'])
    assert final.intersection(shaded).area > 0.95*shaded.area     # shaded foliage stays
    lobe = poly.difference(crown)
    assert final.intersection(lobe).area < 0.15*lobe.area         # the blue-grey cast shadow goes
