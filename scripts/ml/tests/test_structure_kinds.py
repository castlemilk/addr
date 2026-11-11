"""Sub-kind hints for small/secondary building candidates: synthetic scenes, plain-JSON rows, index hygiene."""
import hashlib
import json
import math
import sqlite3
from contextlib import closing

import cv2
import numpy as np
import pytest
from PIL import Image
from scripts.ml import eyes_index
from scripts.ml import structure_kinds as sk
from scripts.ml.scene_object_quality import BUILDING_VERSION

SCALE = 4.24                                  # px per metre, as the saved Mooroolbark scenes
FRAME = [[SCALE, 0], [0, -SCALE], [40, 480]]  # local metres -> pixels (y up in metres, down in pixels)
INVERSE = [[1/SCALE, 0], [0, -1/SCALE], [-40/SCALE, 480/SCALE]]
SIZE = (512, 512)
RNG = np.random.default_rng(7)


def _pixels(points):
    return (np.c_[np.asarray(points, float), np.ones(len(points))] @ np.asarray(FRAME))


def _rect(x, y, w, h):
    return [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]


def _circle(cx, cy, r, n=64):
    return [[cx+r*math.cos(2*math.pi*i/n), cy+r*math.sin(2*math.pi*i/n)] for i in range(n)]


def _ground():
    """Textured lawn: seeded noise around a green, so see-through frames show variance inside."""
    base = np.array([70, 95, 55], np.float32)
    noise = RNG.normal(0, 22, (SIZE[1], SIZE[0], 3)).astype(np.float32)
    return np.clip(base+noise, 0, 255).astype(np.uint8)


def _fill(canvas, ring, value, jitter=2.):
    """Flat fill with a grey level or an (r, g, b) colour plus a little per-pixel jitter."""
    mask = np.zeros(canvas.shape[:2], np.uint8)
    cv2.fillPoly(mask, [_pixels(ring).round().astype(np.int32)], 1)
    colour = np.broadcast_to(np.asarray(value, np.float32), (3,))
    values = np.clip(colour+RNG.normal(0, jitter, canvas.shape[:2]+(3,)), 0, 255)
    canvas[mask.astype(bool)] = values[mask.astype(bool)].astype(np.uint8)


def _ribbed(canvas, ring, period_m=1.0, along='x'):
    """Corrugated metal: a sinusoid along one image axis, so gradients share one orientation."""
    mask = np.zeros(canvas.shape[:2], np.uint8)
    cv2.fillPoly(mask, [_pixels(ring).round().astype(np.int32)], 1)
    ys, xs = np.nonzero(mask)
    pos_m = (xs-FRAME[2][0])/SCALE if along == 'x' else (FRAME[2][1]-ys)/SCALE
    values = np.clip(150+55*np.sin(2*math.pi*pos_m/period_m), 0, 255).astype(np.uint8)
    canvas[ys, xs] = values[:, None]


def _frame_lines(canvas, ring, value=235, beams=5):
    """Pergola: perimeter and a few bright beams are drawn; the textured ground stays visible between them."""
    cv2.polylines(canvas, [_pixels(ring).round().astype(np.int32)], True, (value,)*3, 1)
    (x0, y0), (x1, y1) = ring[0], ring[2]
    for i in range(1, beams+1):
        y = y0+(y1-y0)*i/(beams+1)
        cv2.polylines(canvas, [_pixels([[x0, y], [x1, y]]).round().astype(np.int32)], False, (value,)*3, 1)


def _object(oid, ring, status='candidate'):
    return {'id': oid, 'kind': 'building', 'label': 'Building', 'ring': _pixels(ring).tolist(), 'holes': [],
            'status': status, 'score': .7}


POOL_BLUE = (40, 170, 210)      # OpenCV hue about 97 of 179, saturation about 0.8: the pool-cover band
DRIVEWAY = _rect(28, 36, 12, 14)  # bright, smooth paving west of the house, under and around the carport
ALL_IDS = {'container', 'shed', 'garage', 'pergola', 'tank', 'sliver', 'detached', 'stack', 'classroom', 'pool', 'carport'}


def build_scene():
    canvas = _ground()
    rings = {'container': _rect(5, 5, 12, 2.4), 'shed': _rect(20, 5, 5, 5), 'house': _rect(40, 40, 20, 10),
             'garage': _rect(60, 40, 6, 6), 'pergola': _rect(20, 20, 5, 5), 'tank': _circle(35, 15, 2),
             'sliver': _rect(45, 5, 1, 1), 'detached': _rect(70, 10, 8, 9), 'tree': _circle(80, 60, 3),
             # v2: three 40 ft containers side by side (ribbed 7.2 x 12.2), a 3.5 x 10 demountable, a blue 5 x 6 pool cover,
             # and a 5 x 7 solid roof attached to the house's west wall over a bright driveway (5 x 6 would be two 20 ft
             # containers by the container-stack rule, which runs first).
             'stack': _rect(85, 20, 12.2, 7.2), 'classroom': _rect(5, 30, 10, 3.5), 'pool': _rect(5, 45, 5, 6),
             'carport': _rect(35, 40, 5, 7)}
    _ribbed(canvas, rings['container'])
    _fill(canvas, rings['shed'], 200)
    _fill(canvas, rings['house'], 180)
    _fill(canvas, rings['garage'], 190)
    _frame_lines(canvas, rings['pergola'])
    _fill(canvas, rings['tank'], 170)
    _fill(canvas, rings['sliver'], 210)
    _fill(canvas, rings['detached'], 175)
    _ribbed(canvas, rings['stack'], along='y')
    _fill(canvas, rings['classroom'], 215)
    _fill(canvas, rings['pool'], POOL_BLUE)
    _fill(canvas, DRIVEWAY, 205, jitter=1.)
    _fill(canvas, rings['carport'], 150)
    objects = [_object(name, ring) for name, ring in rings.items() if name != 'tree']
    objects.append({**_object('tree', rings['tree']), 'kind': 'tree'})
    objects.append(_object('gone', _rect(90, 5, 5, 5), status='rejected'))
    png = cv2.imencode('.png', cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))[1].tobytes()
    scene = {'id': 'test-structure-kinds', 'image_width': SIZE[0], 'image_height': SIZE[1], 'pixel_to_meters': INVERSE,
             'meters_to_pixel': FRAME, 'objects': objects, 'model_signature': 'sig-test',
             'image_sha256': hashlib.sha256(png).hexdigest()}
    return scene, canvas, png


@pytest.fixture(scope='module')
def scene_image():
    return build_scene()


@pytest.fixture(scope='module')
def by_object(scene_image):
    scene, canvas, _ = scene_image
    rows = sk.assess_scene(scene, canvas, {}, eligible_only=False)
    return {r['object_id']: r['payload'] for r in rows}


def test_ribbed_box_is_a_shipping_container(by_object):
    p = by_object['container']
    assert p['sub_kind'] == 'shipping-container'
    g, a = p['evidence']['geometry'], p['evidence']['appearance']
    assert 2.0 <= g['rect_width_m'] <= 3.2 and 5.5 <= g['rect_length_m'] <= 13
    assert a['rib_anisotropy'] >= sk.DESIGN['ribbed'] and p['confidence'] == 'strong'
    assert a['rib_period_m'] is not None and 0.6 <= a['rib_period_m'] <= 1.4
    assert 'appearance' in p['agreeing_groups'] and 'geometry' in p['agreeing_groups']


def test_uniform_small_box_is_a_shed(by_object):
    p = by_object['shed']
    assert p['sub_kind'] == 'shed'
    assert p['evidence']['appearance']['interior_relative_variance'] < sk.DESIGN['see-through']['interior_relative_variance']


def test_box_attached_to_a_large_roof_is_a_garage(by_object):
    p = by_object['garage']
    assert p['sub_kind'] == 'garage'
    assert p['evidence']['context']['attached_to_larger_roof'] is True
    assert 'context' in p['agreeing_groups']
    assert p['evidence']['appearance']['driveway_like_share_3m'] < sk.DESIGN['carport']['driveway_share']   # lawn, not paving


def test_ribbed_three_by_forty_foot_box_is_a_container_stack(by_object):
    p = by_object['stack']
    assert p['sub_kind'] == 'container-stack'
    g = p['evidence']['geometry']
    assert sk.container_stack_width(g['rect_width_m']) == 3 and sk.container_stack_length(g['rect_length_m']) == 12.2
    assert g['rectilinearity'] >= sk.DESIGN['container-stack']['rectilinearity']
    assert {'geometry', 'appearance'} <= set(p['agreeing_groups'])     # ribs add confidence but are not required


def test_container_stack_width_and_length_bands():
    assert sk.container_stack_width(4.5) == 2 and sk.container_stack_width(5.1) == 2 and sk.container_stack_width(5.3) is None
    assert sk.container_stack_width(6.9) == 3 and sk.container_stack_width(7.5) == 3 and sk.container_stack_width(7.628) is None
    assert sk.container_stack_width(9.3) == 4 and sk.container_stack_width(9.9) == 4 and sk.container_stack_width(10.2) is None
    assert sk.container_stack_width(3.5) is None and sk.container_stack_width(None) is None
    assert sk.container_stack_length(5.6) == 6.1 and sk.container_stack_length(6.6) == 6.1 and sk.container_stack_length(7.) is None
    assert sk.container_stack_length(11.7) == 12.2 and sk.container_stack_length(12.7) == 12.2 and sk.container_stack_length(9.41) is None
    assert sk.container_stack_length(None) is None


def test_container_stack_needs_a_standard_length_not_ribs():
    geometry = {'area_m2': 84., 'rect_width_m': 7.2, 'rect_length_m': 12.1, 'rectilinearity': .97, 'circularity': .7, 'vertex_count': 4}
    smooth = {'rib_anisotropy': .2, 'interior_relative_variance': .4, 'brightness_median': 220., 'driveway_like_share_3m': .0}
    context = {'secondary_structure_flag': True, 'attached_to_larger_roof': False, 'nearest_larger_roof_m': 5., 'largest_roof_within_40m_area': 300.}
    kind, confidence, _, groups = sk.classify(geometry, smooth, context, None)
    assert (kind, confidence) == ('container-stack', 'moderate') and groups == ['context', 'geometry']
    kind, confidence, _, groups = sk.classify(geometry, {**smooth, 'rib_anisotropy': .4}, context, None)
    assert (kind, confidence) == ('container-stack', 'strong')
    assert sk.classify({**geometry, 'rect_length_m': 9.4}, {**smooth, 'rib_anisotropy': .4}, context, None)[0] == 'garage'
    assert sk.classify({**geometry, 'rectilinearity': .88}, {**smooth, 'rib_anisotropy': .4}, context, None)[0] == 'garage'
    assert sk.classify({**geometry, 'rect_length_m': 6.3, 'area_m2': 45.}, smooth, context, None)[0] == 'container-stack'


def test_users_ribbed_box_stays_a_garage_and_a_width_match_is_noted():
    # mooroolbark-50011498-17-118440-80408/7fdd138f819cb06c as measured by v1: garage (strong). 9.41 m is neither 20 ft nor 40 ft.
    geometry = {'area_m2': 63.379, 'rect_width_m': 7.628, 'rect_length_m': 9.414, 'rectilinearity': .8825, 'circularity': .8351, 'vertex_count': 9}
    appearance = {'rib_anisotropy': .3625, 'interior_relative_variance': 1.0161, 'brightness_median': 202., 'driveway_like_share_3m': .1606}
    context = {'secondary_structure_flag': True, 'attached_to_larger_roof': False, 'nearest_larger_roof_m': 3.987, 'largest_roof_within_40m_area': 317.618}
    kind, confidence, _, _ = sk.classify(geometry, appearance, context, None)
    assert (kind, confidence) == ('garage', 'strong')
    assert sk.container_stack_width(7.628) is None       # 0.13 m outside the three-width band 6.9-7.5
    notes = sk.notes_for(geometry, appearance, kind)
    assert notes and 'matches neither 2, 3 nor 4' in notes[0]
    within = {**geometry, 'rect_width_m': 7.3}
    assert sk.classify(within, appearance, context, None)[0] == 'garage'
    assert sk.notes_for(within, appearance, 'garage') == ['3 container widths but 9.414 m is not a 20 ft or 40 ft length; not a container-stack']


def test_portable_classroom_needs_the_non_residential_context(scene_image):
    scene, canvas, _ = scene_image
    plain = {r['object_id']: r['payload'] for r in sk.assess_scene(scene, canvas, {})}
    assert plain['classroom']['sub_kind'] == 'shed'                      # 3.5 m is wider than a container; no context: a shed
    assert plain['classroom']['evidence']['context']['non_residential_context_flag'] is False
    g = plain['classroom']['evidence']['geometry']
    assert 2.8 <= g['rect_width_m'] <= 4.2 and 6 <= g['rect_length_m'] <= 13 and g['rectilinearity'] >= .85
    flag = {'class_hints': {'class_hint': 'outbuilding', 'flags': [{'flag': 'non-residential-context'}], 'features': {}}}
    flagged = {r['object_id']: r['payload'] for r in sk.assess_scene(scene, canvas, {(scene['id'], 'classroom'): flag})}
    p = flagged['classroom']
    assert p['sub_kind'] == 'portable-classroom' and p['rule'] == sk.RULES['portable-classroom']
    assert p['evidence']['context']['non_residential_context_flag'] is True and p['evidence']['context']['class_hint'] == 'outbuilding'
    assert {'geometry', 'context'} <= set(p['agreeing_groups'])
    hint_only = {'class_hints': {'class_hint': 'commercial', 'flags': [], 'features': {}}}
    by_hint = {r['object_id']: r['payload'] for r in sk.assess_scene(scene, canvas, {(scene['id'], 'classroom'): hint_only})}
    assert by_hint['classroom']['sub_kind'] == 'portable-classroom'
    # the same context does not turn the 2.4 m wide ribbed container into a classroom
    by_flag_all = {r['object_id']: r['payload'] for r in sk.assess_scene(scene, canvas, {(scene['id'], 'container'): flag})}
    assert by_flag_all['container']['sub_kind'] == 'shipping-container'


def test_portable_classroom_takes_precedence_over_a_single_width_container():
    geometry = {'area_m2': 30., 'rect_width_m': 3., 'rect_length_m': 10., 'rectilinearity': .96, 'circularity': .6, 'vertex_count': 4}
    appearance = {'rib_anisotropy': .1, 'interior_relative_variance': .3, 'brightness_median': 220.}
    context = {'secondary_structure_flag': False, 'attached_to_larger_roof': False, 'largest_roof_within_40m_area': 900.,
               'non_residential_context_flag': False, 'class_hint': 'non-residential'}
    assert sk.classify(geometry, appearance, context, None)[0] == 'portable-classroom'
    assert sk.classify(geometry, appearance, {**context, 'class_hint': 'house'}, None)[0] == 'shipping-container'


def test_blue_rectangle_is_a_pool_cover(by_object):
    p = by_object['pool']
    assert p['sub_kind'] == 'pool-cover'
    a = p['evidence']['appearance']
    assert a['pool_blue_share'] >= sk.DESIGN['pool-cover']['blue_share']
    assert {'appearance', 'geometry'} <= set(p['agreeing_groups'])
    assert any(note.startswith('pool_blue_share') for note in p['evidence']['notes'])
    for other in ('shed', 'garage', 'container', 'carport'):
        assert by_object[other]['evidence']['appearance']['pool_blue_share'] < .1


def test_pool_cover_also_fires_on_a_sam3_swimming_pool_proposal():
    geometry = {'area_m2': 32., 'rect_width_m': 4., 'rect_length_m': 8., 'rectilinearity': .65, 'circularity': .7, 'vertex_count': 12}
    appearance = {'rib_anisotropy': .1, 'interior_relative_variance': .3, 'brightness_median': 120., 'pool_blue_share': .1}
    context = {'secondary_structure_flag': True, 'attached_to_larger_roof': False, 'largest_roof_within_40m_area': 150.}
    assert sk.classify(geometry, appearance, context, None)[0] != 'pool-cover'
    sam3 = {'image': 'original', 'prompts': {'house': {'best_iou': .1, 'score': .1}, 'swimming pool': {'best_iou': .62, 'score': .4}}}
    kind, _, _, groups = sk.classify(geometry, appearance, context, sam3)
    assert kind == 'pool-cover' and 'sam3' in groups and 'appearance' not in groups
    weak_fit = {'image': 'original', 'prompts': {'house': {'best_iou': .1, 'score': .1}, 'swimming pool': {'best_iou': .3, 'score': .4}}}
    assert sk.classify(geometry, appearance, context, weak_fit)[0] != 'pool-cover'


def test_pool_blue_share_measures_the_hue_band():
    hsv = np.zeros((4, 4, 3), np.uint8)
    hsv[:2, :, 0], hsv[:2, :, 1], hsv[:2, :, 2] = 97, 200, 200     # pool blue, saturated, bright
    hsv[2, :, 0], hsv[2, :, 1], hsv[2, :, 2] = 97, 80, 200         # right hue, washed out: not counted
    hsv[3, :, 0], hsv[3, :, 1], hsv[3, :, 2] = 97, 200, 90         # right hue, dark blue-grey Colorbond: not counted
    assert sk.pool_blue_share(hsv, np.ones((4, 4), bool)) == pytest.approx(.5)
    hsv[:, :, 0] = 30                            # yellow-green
    assert sk.pool_blue_share(hsv, np.ones((4, 4), bool)) == 0.
    assert sk.pool_blue_share(hsv, np.zeros((4, 4), bool)) is None


def test_attached_roof_over_a_driveway_is_a_carport(by_object):
    p = by_object['carport']
    assert p['sub_kind'] == 'carport' and p['rule'] == sk.RULES['carport'] and p['confidence'] == 'weak'
    a, c = p['evidence']['appearance'], p['evidence']['context']
    assert c['attached_to_larger_roof'] is True and a['driveway_like_share_3m'] >= sk.DESIGN['carport']['driveway_share']
    assert a['interior_relative_variance'] < sk.DESIGN['see-through']['interior_relative_variance']   # solid roof, not see-through
    assert set(p['agreeing_groups']) == {'geometry', 'appearance', 'context'}


def test_carport_needs_the_driveway_share_and_a_larger_roof_nearby():
    geometry = {'area_m2': 35., 'rect_width_m': 5., 'rect_length_m': 7., 'rectilinearity': .98, 'circularity': .78, 'vertex_count': 4}
    appearance = {'rib_anisotropy': .2, 'interior_relative_variance': .5, 'brightness_median': 150., 'driveway_like_share_3m': .6}
    attached = {'secondary_structure_flag': False, 'attached_to_larger_roof': True, 'nearest_larger_roof_m': 0., 'largest_roof_within_40m_area': 200.}
    assert sk.classify(geometry, appearance, attached, None)[:2] == ('carport', 'weak')
    assert sk.classify(geometry, {**appearance, 'driveway_like_share_3m': .2}, attached, None)[:2] == ('carport', 'weak')
    assert sk.classify(geometry, {**appearance, 'driveway_like_share_3m': .1}, attached, None)[0] == 'garage'
    assert sk.classify(geometry, {**appearance, 'driveway_like_share_3m': None}, attached, None)[0] == 'garage'
    near = {**attached, 'attached_to_larger_roof': False, 'nearest_larger_roof_m': 1.5}
    assert sk.classify(geometry, appearance, near, None)[0] == 'carport'
    far = {**attached, 'attached_to_larger_roof': False, 'nearest_larger_roof_m': 6.}
    assert sk.classify(geometry, appearance, far, None)[0] == 'shed'
    assert sk.classify({**geometry, 'area_m2': 50.}, appearance, attached, None)[0] == 'garage'   # above 45 m²


def test_see_through_frame_over_textured_ground_is_pergola_or_shade(by_object):
    p = by_object['pergola']
    assert p['sub_kind'] == 'pergola-or-shade'
    assert p['evidence']['appearance']['interior_relative_variance'] >= sk.DESIGN['see-through']['interior_relative_variance']
    assert p['evidence']['appearance']['brightness_median'] < sk.DESIGN['see-through']['brightness']
    assert p['confidence'] == 'weak'


def test_see_through_frame_is_pergola_unless_the_carport_rule_claims_it():
    geometry = {'area_m2': 25., 'rect_width_m': 5., 'rect_length_m': 5., 'rectilinearity': .98, 'circularity': .78, 'vertex_count': 4}
    appearance = {'rib_anisotropy': .2, 'interior_relative_variance': 2., 'brightness_median': 120., 'driveway_like_share_3m': .05}
    context = {'secondary_structure_flag': False, 'attached_to_larger_roof': True, 'nearest_larger_roof_m': 0., 'largest_roof_within_40m_area': 200.}
    kind, confidence, _, _ = sk.classify(geometry, appearance, context, None)
    assert (kind, confidence) == ('pergola-or-shade', 'weak')      # attached but no driveway: the v1 see-through carport branch is gone
    kind, confidence, _, _ = sk.classify(geometry, {**appearance, 'driveway_like_share_3m': .3}, context, None)
    assert (kind, confidence) == ('carport', 'weak')               # the carport rule (driveway share) runs before the see-through rule


def test_smooth_white_single_width_box_is_a_container_before_shed():
    geometry = {'area_m2': 28., 'rect_width_m': 2.4, 'rect_length_m': 11.8, 'rectilinearity': .95, 'circularity': .45, 'vertex_count': 5}
    appearance = {'rib_anisotropy': .2, 'interior_relative_variance': .3, 'brightness_median': 220.}
    context = {'secondary_structure_flag': True, 'attached_to_larger_roof': False, 'largest_roof_within_40m_area': 150.}
    kind, confidence, _, groups = sk.classify(geometry, appearance, context, None)
    assert (kind, confidence) == ('shipping-container', 'moderate') and 'appearance' not in groups


def test_square_pyramid_shed_is_not_a_water_tank():
    geometry = {'area_m2': 25., 'rect_width_m': 5., 'rect_length_m': 5., 'rectilinearity': .97, 'circularity': .81, 'vertex_count': 8}
    appearance = {'rib_anisotropy': .2, 'interior_relative_variance': .4, 'brightness_median': 200.}
    context = {'secondary_structure_flag': True, 'attached_to_larger_roof': False, 'largest_roof_within_40m_area': 150.}
    assert sk.classify(geometry, appearance, context, None)[0] == 'shed'


def test_solid_bright_roof_with_high_variance_ratio_is_not_see_through():
    geometry = {'area_m2': 40., 'rect_width_m': 6., 'rect_length_m': 7., 'rectilinearity': .95, 'circularity': .78, 'vertex_count': 6}
    appearance = {'rib_anisotropy': .2, 'interior_relative_variance': 2.5, 'brightness_median': 205.}
    context = {'secondary_structure_flag': True, 'attached_to_larger_roof': False, 'largest_roof_within_40m_area': 150.}
    kind, _, _, _ = sk.classify(geometry, appearance, context, None)
    assert kind == 'garage'


def test_circle_is_a_water_tank(by_object):
    p = by_object['tank']
    assert p['sub_kind'] == 'water-tank'
    assert p['evidence']['geometry']['circularity'] >= .8
    assert p['evidence']['geometry']['rectilinearity'] < .85


def test_sliver_is_a_fragment(by_object):
    p = by_object['sliver']
    assert p['sub_kind'] == 'fragment'
    assert p['rule'] == sk.RULES['fragment']


def test_large_roof_is_not_secondary(by_object):
    p = by_object['house']
    assert p['sub_kind'] == 'not-secondary'
    assert p['area_m2'] == pytest.approx(200, abs=.5)
    assert p['eligibility']['small'] is False


def test_detached_box_becomes_a_garage_only_with_the_secondary_flag(scene_image):
    scene, canvas, _ = scene_image
    plain = {r['object_id']: r['payload'] for r in sk.assess_scene(scene, canvas, {})}
    assert plain['detached']['sub_kind'] == 'not-secondary'
    flag = {'class_hints': {'flags': [{'flag': 'secondary-structure'}], 'features': {'secondary_structure': {'largest_same_property_area': 200.}}}}
    flagged = {r['object_id']: r['payload'] for r in sk.assess_scene(scene, canvas, {(scene['id'], 'detached'): flag})}
    assert flagged['detached']['sub_kind'] == 'garage'
    assert flagged['detached']['evidence']['context'] == {**plain['detached']['evidence']['context'],
                                                          'secondary_structure_flag': True, 'flags_row_present': True,
                                                          'largest_same_property_area': 200.}


def test_rejected_and_non_building_objects_and_large_roofs_are_skipped(scene_image):
    scene, canvas, _ = scene_image
    ids = {r['object_id'] for r in sk.assess_scene(scene, canvas, {})}
    assert 'gone' not in ids and 'tree' not in ids and 'house' not in ids
    assert ids == ALL_IDS


def _assert_plain(value, path='payload'):
    if isinstance(value, dict):
        for k, v in value.items():
            assert isinstance(k, str), path
            _assert_plain(v, f'{path}.{k}')
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_plain(v, f'{path}[{i}]')
    else:
        assert value is None or type(value) in (str, int, float, bool), f'{path}: {type(value)}'


def test_rows_are_plain_json_with_the_schema_keys(scene_image):
    scene, canvas, _ = scene_image
    for row in sk.assess_scene(scene, canvas, {}):
        assert set(row) == {'scene_id', 'object_id', 'model_signature', 'version', 'payload'}
        p = row['payload']
        assert row['version'] == sk.VERSION == 'structure-kinds-v3' and p['version'] == 'structure-kinds-v3'
        _assert_plain(p)
        json.dumps(p)
        assert {'version', 'note', 'kind', 'area_m2', 'sub_kind', 'confidence', 'rule', 'evidence', 'computed_at', 'code_sha256'} <= set(p)
        assert p['note'] == 'agent hint, not a label' and p['kind'] == 'building'
        assert p['sub_kind'] in sk.SUB_KINDS and p['confidence'] in sk.CONFIDENCES
        assert set(p['evidence']) == {'geometry', 'appearance', 'context', 'sam3', 'notes'} and p['evidence']['sam3'] is None
        assert {'area_m2', 'rect_width_m', 'rect_length_m', 'aspect', 'rectilinearity', 'circularity', 'vertex_count'} <= set(p['evidence']['geometry'])
        assert {'brightness_median', 'saturation_median', 'rib_anisotropy', 'rib_period_m', 'rib_axis_alignment',
                'interior_relative_variance', 'driveway_like_share_3m', 'pool_blue_share'} <= set(p['evidence']['appearance'])
        assert {'attached_to_larger_roof', 'nearest_larger_roof_m', 'distance_to_largest_roof_m', 'largest_same_property_area',
                'secondary_structure_flag', 'non_residential_context_flag', 'class_hint', 'touches_image_edge'} <= set(p['evidence']['context'])
        assert set(sk.LABELS) == set(sk.SUB_KINDS)


def test_without_image_the_appearance_is_recorded_unavailable(scene_image):
    scene, _, _ = scene_image
    rows = {r['object_id']: r['payload'] for r in sk.assess_scene(scene, None, {})}
    assert rows['sliver']['sub_kind'] == 'fragment'
    assert rows['shed']['evidence']['appearance']['image'] == 'unavailable'
    assert rows['shed']['evidence']['appearance']['rib_anisotropy'] is None


def test_confidence_counts_agreeing_groups():
    assert sk.confidence_for(['geometry']) == 'weak'
    assert sk.confidence_for(['geometry', 'context']) == 'moderate'
    assert sk.confidence_for(['geometry', 'context', 'appearance']) == 'strong'
    assert sk.confidence_for(['geometry', 'geometry']) == 'weak'


def test_sam3_negative_signal_raises_confidence_without_changing_a_fitting_rule():
    geometry = {'area_m2': 20., 'rect_width_m': 4., 'rect_length_m': 5., 'rectilinearity': 1., 'circularity': .78}
    appearance = {'rib_anisotropy': .2, 'interior_relative_variance': .1}
    context = {'secondary_structure_flag': False, 'attached_to_larger_roof': False, 'largest_roof_within_40m_area': None}
    kind, confidence, _, groups = sk.classify(geometry, appearance, context, None)
    assert (kind, confidence) == ('shed', 'weak')
    sam3 = {'image': 'original', 'prompts': {'house': {'best_iou': 0., 'score': None}, 'building roof': {'best_iou': 0., 'score': None},
                                             'garden shed': {'best_iou': .39, 'score': .2}}}
    kind, confidence, _, groups = sk.classify(geometry, appearance, context, sam3)
    assert (kind, confidence) == ('shed', 'moderate') and 'sam3' in groups


def test_run_writes_only_its_version_and_works_without_flags_or_sam3(tmp_path, scene_image):
    scene, _, png = scene_image
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    (folder/f"{scene['image_sha256']}.png").write_bytes(png)
    assert not eyes_index.path(tmp_path).exists()      # no eyes index: no building-flags rows to read
    rows, written = sk.run(tmp_path, [scene['id']])
    assert written == len(rows) == len(ALL_IDS)
    with closing(eyes_index.connection(tmp_path)) as db:
        stored = eyes_index.rows(db, 'object_quality', scene_id=scene['id'], version=sk.VERSION)
        assert {r['object_id'] for r in stored} == ALL_IDS
        assert all(r['payload']['evidence']['context']['flags_row_present'] is False for r in stored)
        assert {r['payload']['sub_kind'] for r in stored} >= {'shipping-container', 'container-stack', 'pool-cover', 'carport', 'shed', 'garage',
                                                              'pergola-or-shade', 'water-tank', 'fragment'}
        # v1 rows of the previous version survive a v2 run: replace_scene_rows scopes deletes to this version.
        v1 = {'scene_id': scene['id'], 'object_id': 'shed', 'model_signature': 'sig-test', 'version': 'structure-kinds-v1',
              'payload': {'version': 'structure-kinds-v1', 'sub_kind': 'shed'}}
        eyes_index.replace_rows(db, 'object_quality', [v1])
        # Another version's row survives a rerun untouched (replace_scene_rows scopes deletes to this version).
        other = {'scene_id': scene['id'], 'object_id': 'shed', 'model_signature': 'sig-test', 'version': BUILDING_VERSION,
                 'payload': {'class_hints': {'flags': [{'flag': 'secondary-structure'}], 'features': {'secondary_structure': {'largest_same_property_area': 200.}}}}}
        eyes_index.replace_rows(db, 'object_quality', [other])
    rows, written = sk.run(tmp_path, [scene['id']])
    with closing(sqlite3.connect(eyes_index.path(tmp_path))) as db:
        counts = dict(db.execute('SELECT version, COUNT(*) FROM object_quality GROUP BY version').fetchall())
    assert counts == {sk.VERSION: len(ALL_IDS), BUILDING_VERSION: 1, 'structure-kinds-v1': 1}
    shed = next(r['payload'] for r in rows if r['object_id'] == 'shed')
    assert shed['evidence']['context']['secondary_structure_flag'] is True and shed['sub_kind'] == 'shed'


def test_objects_form_upserts_without_dropping_the_scene(tmp_path, scene_image):
    scene, _, png = scene_image
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    (folder/f"{scene['image_sha256']}.png").write_bytes(png)
    sk.run(tmp_path, [scene['id']])
    rows, written = sk.run(tmp_path, objects=[(scene['id'], 'shed')])
    assert written == 1 and rows[0]['object_id'] == 'shed'
    with closing(sqlite3.connect(eyes_index.path(tmp_path))) as db:
        assert db.execute('SELECT COUNT(*) FROM object_quality WHERE version=?', (sk.VERSION,)).fetchone()[0] == len(ALL_IDS)


def test_enhance_matches_the_editor_recipe_shape():
    rgb = np.full((24, 24, 3), 90, np.uint8)
    out = sk.enhance(rgb)
    assert out.shape == rgb.shape and out.dtype == np.uint8


def test_sample_rows_is_seeded_and_capped():
    rows = list(range(20))
    assert sk.sample_rows(rows, None) == rows and sk.sample_rows(rows, 30) == rows
    picked = sk.sample_rows(rows, 5, seed=11)
    assert len(picked) == 5 and picked == sorted(picked) and picked == sk.sample_rows(rows, 5, seed=11)
    assert picked != sk.sample_rows(rows, 5, seed=13)


def test_render_sheet_writes_a_png(tmp_path, scene_image):
    scene, _, png = scene_image
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    (folder/f"{scene['image_sha256']}.png").write_bytes(png)
    rows, _ = sk.run(tmp_path, [scene['id']], write=False)
    path = sk.render_sheet(tmp_path, rows, tmp_path/'sheet.png')
    with Image.open(path) as sheet:
        assert sheet.size[0] == 4*330 and sheet.size[1] > 300


def test_attached_small_solid_roofs_are_covers_and_wobbly_detached_boxes_are_sheds():
    """house-25822 (user review, 27 Sep): a 28.6 m² roof attached to the house is a patio cover, not a shed; a detached
    28.6 m² box traced with 36 vertices (rectangle fill 0.69) is a shed, not a generic outbuilding."""
    from scripts.ml import structure_kinds as sk
    smooth = {'rib_anisotropy': .1, 'interior_relative_variance': .3, 'brightness_median': 200, 'pool_blue_share': 0., 'driveway_like_share_3m': 0.}
    base_ctx = {'secondary_structure_flag': True, 'largest_roof_within_40m_area': 210., 'touches_image_edge': False,
                'non_residential_context_flag': False, 'class_hint': 'outbuilding'}
    patio = {'area_m2': 28.6, 'rect_width_m': 4., 'rect_length_m': 7.5, 'rectilinearity': .96, 'circularity': .7, 'vertex_count': 11}
    kind, conf, rule, groups = sk.classify(patio, smooth, {**base_ctx, 'attached_to_larger_roof': True, 'nearest_larger_roof_m': 0.}, None)
    assert kind == 'attached-cover' and conf in ('moderate', 'strong') and 'patio' in rule
    shed = {'area_m2': 28.64, 'rect_width_m': 5., 'rect_length_m': 8.3, 'rectilinearity': .6866, 'circularity': .6, 'vertex_count': 36}
    assert sk.classify(shed, smooth, {**base_ctx, 'attached_to_larger_roof': False, 'nearest_larger_roof_m': 3.1}, None)[0] == 'shed'
    garage = {'area_m2': 31.2, 'rect_width_m': 5.2, 'rect_length_m': 7.1, 'rectilinearity': .85, 'circularity': .7, 'vertex_count': 9}
    assert sk.classify(garage, smooth, {**base_ctx, 'attached_to_larger_roof': True, 'nearest_larger_roof_m': .25}, None)[0] == 'garage'
    assert 'attached-cover' in sk.SUB_KINDS and sk.LABELS['attached-cover'] == 'Patio or carport cover'
