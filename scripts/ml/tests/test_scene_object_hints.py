"""object-hints (the v1 codes and v2's solar_fixture_like): synthetic PIL scenes fire each code on the shape that should fire it and stay
quiet on its foil.

Agent hints, never labels: the module returns sentences and numbers; nothing here changes a scene.
"""
import hashlib
import json
import math

import numpy as np
import pytest
from PIL import Image
from scripts.ml import scene_lot_tags as lt
from scripts.ml import scene_object_hints as oh
from shapely.geometry import box as sbox
from shapely.geometry import mapping

SCALE = .1                                    # metres per pixel
SIZE = 512
FRAME = [[SCALE, 0], [0, -SCALE], [0, SIZE*SCALE]]           # pixels -> local metres (y up in metres, down in pixels)
INVERSE = [[1/SCALE, 0], [0, -1/SCALE], [0, SIZE]]           # local metres -> pixels
CENTROID = {'lon': 145.3155, 'lat': -37.7915}


def scene_with(objects, scene_id='hints-test'):
    return {'id': scene_id, 'schema_version': 1, 'address_id': 4242, 'title': '1 TEST STREET', 'centroid': CENTROID,
            'image_width': SIZE, 'image_height': SIZE, 'image_sha256': 'a'*64, 'model_signature': 'sig-'+hashlib.sha256(scene_id.encode()).hexdigest()[:8],
            'pixel_to_meters': FRAME, 'meters_to_pixel': INVERSE, 'objects': objects}


def rect(x, y, w, h):
    """Pixel ring of a rectangle at pixel (x, y) with pixel width w and height h."""
    return [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]


def obj(oid, kind, ring, **extra):
    return {'id': oid, 'kind': kind, 'label': kind.title(), 'status': 'candidate', 'score': .8, 'ring': ring, 'source': 'sam3-text-onnx', 'model': {}, **extra}


def canvas(colour):
    return np.full((SIZE, SIZE, 3), colour, dtype=np.uint8)


def paint(image, ring, colour):
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    image[min(ys):max(ys), min(xs):max(xs)] = colour


def parcels_for(scene, lots):
    """GeoJSON parcels from pixel rings using the scene's inverse projection (the same frame the module projects with)."""
    unproject = lt.inverse_projection(scene)
    features = []
    for lot, plan, ring in lots:
        polygon = sbox(min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring))
        geo = {'type': 'Polygon', 'coordinates': [[list(unproject(x, y)) for x, y in polygon.exterior.coords]]}
        features.append({'type': 'Feature', 'geometry': geo, 'properties': {'pfi': f'{lot}{plan}', 'spi': f'{lot}\\{plan}', 'plan': plan, 'lot': lot, 'status': 'approved'}})
    bounds = lt.frame_lonlat_bounds(scene)
    return {'type': 'FeatureCollection', 'features': features, 'bounds': [bounds[0]-.001, bounds[1]-.001, bounds[2]+.001, bounds[3]+.001]}


def codes(rows, oid):
    payload = next(r['payload'] for r in rows if r['object_id'] == oid)
    return payload['codes'], payload


def test_pool_not_blue_fires_on_a_green_pool_and_not_on_a_blue_one():
    green, blue = rect(100, 100, 40, 60), rect(300, 100, 40, 60)     # 4 x 6 m each
    scene = scene_with([obj('green', 'pool', green), obj('blue', 'pool', blue)])
    image = canvas((120, 120, 120))
    paint(image, green, (60, 150, 50))
    paint(image, blue, (40, 120, 220))
    rows = oh.hints_for_scene(scene, Image.fromarray(image))
    fired, payload = codes(rows, 'green')
    assert 'pool_not_blue' in fired
    sentence = next(h for h in payload['hints'] if h['code'] == 'pool_not_blue')['sentence']
    assert 'reads green' in sentence and 'like grass' in sentence and 'is grass' not in sentence
    assert payload['colour']['green_share'] >= .5 and payload['colour']['blue_share'] < .2
    assert 'pool_not_blue' not in codes(rows, 'blue')[0]


def test_grey_building_on_grey_ground_blends_and_a_dark_one_does_not():
    small, dark = rect(200, 200, 12, 12), rect(300, 300, 12, 12)     # 1.44 m² each
    scene = scene_with([obj('flat', 'building', small), obj('dark', 'building', dark)])
    image = canvas((150, 150, 150))
    paint(image, small, (154, 154, 154))
    paint(image, dark, (40, 40, 40))
    rows = oh.hints_for_scene(scene, image)
    fired, payload = codes(rows, 'flat')
    assert 'building_blends_into_surroundings' in fired and 'tiny_area_for_kind' in fired
    assert 'pavement' in next(h for h in payload['hints'] if h['code'] == 'building_blends_into_surroundings')['sentence']
    assert 'building_blends_into_surroundings' not in codes(rows, 'dark')[0]
    assert 'tiny_area_for_kind' in codes(rows, 'dark')[0], 'size alone still says small for a building'


def test_canopy_fragment_inside_a_large_tree_fires_and_outside_does_not():
    tree = rect(100, 100, 100, 100)                                    # 100 m²
    inside, outside = rect(150, 150, 4, 5), rect(210, 100, 4, 5)       # 0.2 m² each; outside sits 1 m past the canopy
    scene = scene_with([obj('tree', 'tree', tree), obj('piece', 'vegetation', inside), obj('apart', 'vegetation', outside)])
    rows = oh.hints_for_scene(scene, None)
    fired, payload = codes(rows, 'piece')
    assert 'canopy_fragment_inside' in fired
    evidence = next(h for h in payload['hints'] if h['code'] == 'canopy_fragment_inside')['evidence']
    assert evidence['container_id'] == 'tree' and evidence['cover'] >= .9 and evidence['container_area_m2'] == pytest.approx(100, abs=.1)
    assert 'the model split one bush' in next(h for h in payload['hints'] if h['code'] == 'canopy_fragment_inside')['sentence']
    assert 'canopy_fragment_inside' not in codes(rows, 'apart')[0]
    assert 'tiny_area_for_kind' in codes(rows, 'apart')[0]
    assert payload['image_available'] is False


def jittered_rectangle(x, y, w, h, n=54, amplitude_px=5, seed=3):
    """A w x h pixel rectangle traced with n vertices pushed in and out of the edge (a shadow-following mask)."""
    rng = np.random.default_rng(seed)
    corners = [(x, y), (x+w, y), (x+w, y+h), (x, y+h)]
    ring = []
    per_side = n//4
    for i, (a, b) in enumerate(zip(corners, corners[1:]+corners[:1], strict=True)):
        for k in range(per_side):
            t = k/per_side
            px, py = a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t
            offset = amplitude_px*(1 if k % 2 else -1)*(.6+.4*rng.random())
            if i % 2 == 0:
                py += offset
            else:
                px += offset
            ring.append([round(px, 2), round(py, 2)])
    return ring


def test_noisy_outline_fires_on_a_jittered_roof_and_not_on_a_clean_rectangle():
    noisy = jittered_rectangle(100, 100, 100, 90)                       # 10 x 9 m, 52 vertices
    clean = rect(300, 100, 100, 90)
    scene = scene_with([obj('noisy', 'building', noisy), obj('clean', 'building', clean)])
    rows = oh.hints_for_scene(scene, None)
    fired, payload = codes(rows, 'noisy')
    assert 'noisy_outline' in fired, payload['outline']
    hint = next(h for h in payload['hints'] if h['code'] == 'noisy_outline')
    assert hint['evidence']['svpm'] >= .45 and hint['evidence']['wonky_share'] >= .85
    assert 'roof edges are usually straight' in hint['sentence'] and 'Check the shadow side' in hint['sentence']
    assert hint['evidence']['orthogonalise_gate'] in ('passed', 'refused')
    fired_clean, clean_payload = codes(rows, 'clean')
    assert 'noisy_outline' not in fired_clean
    assert clean_payload['outline']['vertices'] == 4 and clean_payload['outline']['off_axis'] == 0, 'the quiet count is recorded without a flag'
    assert clean_payload['squared_candidate'] and clean_payload['squared_candidate'].get('ring'), 'a clean rectangle has a squared candidate'
    assert clean_payload['squared_candidate']['iou'] >= .85


def test_secondary_by_lot_names_the_primary_on_the_same_injected_lot():
    big, small = rect(100, 100, 200, 100), rect(320, 120, 50, 40)      # 200 m² and 20 m² on one lot
    scene = scene_with([obj('big', 'building', big), obj('small', 'building', small)])
    parcels = parcels_for(scene, [('20', 'LP51872', rect(80, 80, 320, 160))])
    rows = oh.hints_for_scene(scene, None, parcels=parcels)
    fired, payload = codes(rows, 'small')
    assert 'secondary_by_lot' in fired
    hint = next(h for h in payload['hints'] if h['code'] == 'secondary_by_lot')
    assert hint['evidence']['lot_primary'] == 'big' and hint['evidence']['ratio'] == pytest.approx(.1, abs=.01)
    assert '200.0 m² main roof' in hint['sentence'] and 'Lot 20 LP51872' in hint['sentence'] and 'garage-sized' in hint['sentence']
    assert 'secondary_by_lot' not in codes(rows, 'big')[0]
    assert payload['lot']['lot_primary'] == 'big'


def test_roof_fit_on_fragment_and_degenerate_outline():
    tiny_roof = rect(100, 100, 14, 14)                                  # 1.96 m² with a roof model
    sliver = rect(300, 300, 3, 3)                                       # 0.3 x 0.3 m
    scene = scene_with([obj('roofed', 'building', tiny_roof, roof_parts=[{'ring_meters': [], 'ring_z': []}]*4, roof_type='hipped'),
                        obj('sliver', 'building', sliver)])
    rows = oh.hints_for_scene(scene, None)
    fired, payload = codes(rows, 'roofed')
    assert 'roof_fit_on_fragment' in fired
    assert 'A hipped roof with 4 planes was fitted on 2.0 m²' in next(h for h in payload['hints'] if h['code'] == 'roof_fit_on_fragment')['sentence']
    fired, payload = codes(rows, 'sliver')
    assert 'degenerate_outline' in fired and 'tiny_area_for_kind' in fired
    assert '0.30 x 0.30 m outline' in next(h for h in payload['hints'] if h['code'] == 'degenerate_outline')['sentence']


def test_structure_fragment_hint_also_flags_a_roof_fit():
    scene = scene_with([obj('frag', 'building', rect(100, 100, 24, 22), roof_parts=[{}]*8, roof_type='hipped')])   # 5.3 m²
    rows = oh.hints_for_scene(scene, None, structure_rows={'frag': {'sub_kind': 'fragment'}})
    fired, payload = codes(rows, 'frag')
    assert 'roof_fit_on_fragment' in fired and 'tiny_area_for_kind' not in fired
    assert 'the structure hint calls a fragment' in next(h for h in payload['hints'] if h['code'] == 'roof_fit_on_fragment')['sentence']


def test_lot_codes_are_copied_with_their_sentences_and_unit_site_reaches_every_object():
    edge = rect(0, 200, 60, 60)                                          # touches the left frame edge, on the neighbour's lot
    home = rect(200, 200, 100, 80)
    inner = rect(20, 160, 40, 40)                                        # on the same neighbour lot, 20 px clear of the frame
    bush = rect(0, 280, 30, 20)                                          # vegetation touching the frame on that lot
    scene = scene_with([obj('edge', 'building', edge), obj('inner', 'building', inner), obj('bush', 'vegetation', bush), obj('home', 'building', home),
                        obj('roof-4242', 'building', rect(210, 210, 60, 50), source='roof-pipeline', address_id=4242)])
    scene['title'] = 'Unit UNIT 4 63 TEST ROAD'
    parcels = parcels_for(scene, [('1', 'PS1', rect(180, 180, 150, 120)), ('2', 'PS1', rect(330, 180, 150, 120)), ('CM1', 'PS1', rect(180, 300, 300, 100)),
                                  ('7', 'LP9', rect(-100, 150, 170, 160))])
    tags, scene_tag = lt.analyse(scene, parcels, seed={'address_id': 4242, 'ring': rect(210, 210, 60, 50), 'point': None, 'title': scene['title']})
    rows = oh.hints_for_scene(scene, None, lot_tags=tags, scene_tag=scene_tag, parcels_sha256='p'*64)
    fired, payload = codes(rows, 'edge')
    assert {'frame_cut', 'lot_incomplete_in_frame', 'lot_unassociated', 'unit_site'} <= set(fired)
    sentences = {h['code']: h['sentence'] for h in payload['hints']}
    assert sentences['frame_cut'] == 'Touches the image edge; the frame cut it, not the building.'
    assert sentences['lot_unassociated'].startswith('On neighbouring Lot 7 LP9, not the address lot')
    assert sentences['lot_incomplete_in_frame'] == 'Sits on Lot 7 LP9, which leaves this image; the frame cut it, not the building.'
    # The frame is blamed only on a measured touch (edge_px within 1 px); a lot that merely continues past the image says so.
    inner_codes, inner_payload = codes(rows, 'inner')
    inner_sentences = {h['code']: h['sentence'] for h in inner_payload['hints']}
    assert 'lot_incomplete_in_frame' in inner_codes and 'frame_cut' not in inner_codes and inner_payload['lot']['dominant']['lot'] == '7'
    assert inner_sentences['lot_incomplete_in_frame'] == 'Sits on Lot 7 LP9, which leaves this image; only part of the lot is visible.'
    assert 'frame cut' not in inner_sentences['lot_incomplete_in_frame']
    bush_sentences = {h['code']: h['sentence'] for h in codes(rows, 'bush')[1]['hints']}
    assert bush_sentences['lot_incomplete_in_frame'] == 'Sits on Lot 7 LP9, which leaves this image; the frame cut it, not the vegetation.'
    assert sentences['unit_site'].startswith('Plan PS1: Lots 1, 2 + CM1')
    assert next(h for h in payload['hints'] if h['code'] == 'lot_incomplete_in_frame')['severity'] == 'context'
    assert next(h for h in payload['hints'] if h['code'] == 'frame_cut')['severity'] == 'review'
    assert payload['lot_ref'] == 'p'*64
    home_codes, _ = codes(rows, 'home')
    assert 'lot_unassociated' not in home_codes and 'frame_cut' not in home_codes and 'unit_site' in home_codes
    assert 'unit_title_mismatch' in codes(rows, 'roof-4242')[0], 'the title/lot mismatch sits on the seed roof only'
    assert 'unit_title_mismatch' not in home_codes


def test_rows_carry_signature_code_hash_and_note_and_never_touch_the_scene():
    scene = scene_with([obj('b', 'building', rect(100, 100, 100, 90))])
    before = json.dumps(scene, sort_keys=True)
    rows = oh.hints_for_scene(scene, None)
    assert json.dumps(scene, sort_keys=True) == before
    row = rows[0]
    assert row['scene_id'] == 'hints-test' and row['object_id'] == 'b' and row['model_signature'] == scene['model_signature'] and row['version'] == oh.VERSION
    payload = row['payload']
    assert payload['note'] == 'agent hint, not a label' and len(payload['code_sha256']) == 64 and payload['code_sha256'] == oh.code_sha256()
    assert payload['version'] == oh.VERSION and 'computed_at' in payload
    assert payload['sentences_version'] == oh.SENTENCES_VERSION == 2, 'wording changes after the scored run are counted on every row'
    assert all(h['severity'] in ('context', 'review') for h in payload['hints'])
    assert set(oh.RULES) >= {code for code in oh.DESIGN}, 'every designed code has a stated rule'


def test_secondary_falls_back_to_building_flags_without_parcels():
    scene = scene_with([obj('shed', 'building', rect(100, 100, 40, 40))])
    flags = {'shed': {'class_hints': {'flags': [{'flag': 'secondary-structure', 'evidence': {'largest_same_property_area': 151.3}}]}}}
    rows = oh.hints_for_scene(scene, None, flags=flags)
    fired, payload = codes(rows, 'shed')
    assert 'secondary_by_lot' in fired
    hint = next(h for h in payload['hints'] if h['code'] == 'secondary_by_lot')
    assert hint['evidence']['source'] == 'building-flags-v2 secondary-structure' and '151.3 m²' in hint['sentence']


def test_outline_perimeter_counts_the_exterior_ring_only():
    from shapely.geometry import Polygon
    holed = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(4, 4), (4.5, 4), (4.5, 4.5), (4, 4.5)]])
    metrics = oh.outline_metrics(holed, 4)
    assert metrics['perimeter_m'] == pytest.approx(40.) and metrics['svpm'] == pytest.approx(.1), 'a speck hole adds no edge to the sentence or the svpm denominator'
    assert metrics['vertices'] == 4


def test_outline_metrics_of_a_square_are_quiet():
    from shapely.geometry import Polygon
    metrics = oh.outline_metrics(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), 4)
    assert metrics['vertices'] == 4 and metrics['off_axis'] == 0 and metrics['svpm'] == pytest.approx(.1)
    assert math.isclose(metrics['rectilinearity'], 1., abs_tol=1e-6)
    assert mapping  # shapely stays importable for parcel fixtures


# --- object-hints-v2: solar_fixture_like ------------------------------------------------------------------------

def disc(cx, cy, radius, n=24):
    """Pixel ring of a regular n-gon (a round rooftop unit): rectangle fill ~0.79, so only brightness can flag it."""
    return [[round(cx+radius*math.cos(2*math.pi*k/n), 2), round(cy+radius*math.sin(2*math.pi*k/n), 2)] for k in range(n)]


def fill_ring(image, ring, colour):
    """Paint the polygon itself (not its bounding box) so the median inside is the painted colour."""
    from PIL import ImageDraw
    canvas_image = Image.fromarray(image)
    ImageDraw.Draw(canvas_image).polygon([tuple(p) for p in ring], fill=tuple(colour))
    image[:] = np.asarray(canvas_image)


def hint_of(payload, code):
    return next(h for h in payload['hints'] if h['code'] == code)


def test_solar_fixture_like_fires_on_a_bright_round_panel_and_not_on_a_dark_rectangle():
    round_unit, array = disc(150, 150, 15), rect(300, 300, 30, 20)          # ~6.9 m² disc; a 3 x 2 m panel array
    scene = scene_with([obj('unit', 'solar_panel', round_unit), obj('array', 'solar_panel', array)])
    image = canvas((110, 110, 110))                                          # the scene's 60th-percentile grey is 110
    fill_ring(image, round_unit, (226, 226, 222))
    fill_ring(image, array, (35, 42, 60))
    rows = oh.hints_for_scene(scene, image)
    fired, payload = codes(rows, 'unit')
    assert 'solar_fixture_like' in fired
    hint = hint_of(payload, 'solar_fixture_like')
    assert hint['severity'] == 'review'
    evidence = hint['evidence']
    assert evidence['reasons'] == ['bright']
    assert evidence['median_luminance'] > 200 and evidence['luminance_ceiling'] == pytest.approx(110, abs=.5)
    assert evidence['rect_fill'] >= .7, 'a disc fills ~0.79 of its rectangle: brightness alone fired'
    for key in ('area_m2', 'rect_fill', 'off_axis_share', 'vertices', 'perimeter_m', 'vertices_per_m', 'median_luminance', 'luminance_ceiling',
                'blue_minus_red', 'pixels'):
        assert key in evidence, key
    assert evidence['vertices'] == 24 and evidence['vertices_per_m'] == pytest.approx(24/evidence['perimeter_m'], abs=.01)
    sentence = hint['sentence']
    assert sentence.startswith('Bright for a solar panel (median grey ') and "above the image's 60th percentile of 110" in sentence
    assert sentence.endswith('check for a hot-water unit, air-conditioner or skylight.')
    assert 'is a ' not in sentence and 'not a solar panel' not in sentence, 'a hint, never a label'
    fired_array, array_payload = codes(rows, 'array')
    assert 'solar_fixture_like' not in fired_array
    quiet = array_payload['solar']
    assert quiet['reasons'] == [] and quiet['rect_fill'] == pytest.approx(1., abs=.01) and quiet['median_luminance'] < 60
    assert quiet['blue_minus_red'] == pytest.approx(25, abs=1), 'panels read slightly blue: recorded as evidence, not a gate'
    assert payload['colour'] is None, 'the pool/building colour block stays theirs'


def test_solar_fixture_like_ceiling_is_the_scene_60th_percentile_grey():
    mid, dark, level = rect(100, 60, 30, 20), rect(300, 60, 30, 20), rect(200, 60, 30, 20)
    scene = scene_with([obj('mid', 'solar_panel', mid), obj('dark', 'solar_panel', dark), obj('level', 'solar_panel', level)])
    image = canvas((100, 100, 100))
    image[int(SIZE*.7):, :] = 200                                            # 70% of the frame at 100, 30% at 200: p60 = 100
    fill_ring(image, mid, (150, 150, 150))
    fill_ring(image, dark, (90, 90, 90))
    fill_ring(image, level, (100, 100, 100))                                 # exactly the scene's p60 grey
    rows = oh.hints_for_scene(scene, image)
    fired, payload = codes(rows, 'mid')
    assert 'solar_fixture_like' in fired and hint_of(payload, 'solar_fixture_like')['evidence']['luminance_ceiling'] == pytest.approx(100, abs=.5)
    assert 'solar_fixture_like' not in codes(rows, 'dark')[0], 'darker than the scene percentile: quiet'
    fired, payload = codes(rows, 'level')
    assert payload['solar']['median_luminance'] == payload['solar']['luminance_ceiling'] == 100.
    assert 'solar_fixture_like' not in fired and payload['solar']['reasons'] == [], "'bright' means strictly above the scene percentile"


def test_solar_fixture_like_non_rectangular_and_area_floor_need_no_image():
    ell = [[100, 100], [130, 100], [130, 110], [110, 110], [110, 130], [100, 130]]
    small, panel = rect(300, 100, 10, 11), rect(300, 300, 12, 12)            # 1.1 m² (under one panel) and 1.44 m²
    round_unit = disc(200, 300, 15)
    scene = scene_with([obj('ell', 'solar_panel', ell), obj('small', 'solar_panel', small), obj('panel', 'solar_panel', panel),
                        obj('round', 'solar_panel', round_unit)])
    rows = oh.hints_for_scene(scene, None)
    fired, payload = codes(rows, 'ell')                                      # an L of 5 m² in a 3 x 3 m rectangle: fill 0.56
    assert 'solar_fixture_like' in fired
    evidence = hint_of(payload, 'solar_fixture_like')['evidence']
    assert evidence['reasons'] == ['non_rectangular'] and evidence['rect_fill'] == pytest.approx(5/9, abs=.01)
    assert evidence['median_luminance'] is None and evidence['luminance_ceiling'] is None, 'no image: brightness is not judged'
    assert 'Non-rectangular for a solar panel (fills 56% of its rectangle)' in hint_of(payload, 'solar_fixture_like')['sentence']
    fired, payload = codes(rows, 'small')
    assert 'solar_fixture_like' in fired and hint_of(payload, 'solar_fixture_like')['evidence']['reasons'] == ['small']
    assert 'smaller than one panel' in hint_of(payload, 'solar_fixture_like')['sentence']
    assert 'solar_fixture_like' not in codes(rows, 'panel')[0], '1.44 m² clears the 1.2 m² floor'
    fired, payload = codes(rows, 'round')
    assert 'solar_fixture_like' not in fired and payload['solar']['median_luminance'] is None and payload['image_available'] is False


def test_solar_fixture_like_is_only_for_solar_panels():
    ring = disc(150, 150, 15)
    scene = scene_with([obj('b', 'building', ring), obj('s', 'skylight', disc(300, 300, 15))])
    image = canvas((110, 110, 110))
    fill_ring(image, ring, (230, 230, 230))
    rows = oh.hints_for_scene(scene, image)
    for oid in ('b', 's'):
        fired, payload = codes(rows, oid)
        assert 'solar_fixture_like' not in fired and payload['solar'] is None


def test_version_two_names_its_predecessor_and_the_rows_can_be_restricted_to_kinds():
    assert oh.VERSION == 'object-hints-v2' and oh.PREVIOUS_VERSIONS == ('object-hints-v1',)
    assert oh.DESIGN['solar_fixture_like'] == {'rect_fill': .70, 'luminance_percentile': 60, 'area_m2': 1.2}
    scene = scene_with([obj('b', 'building', rect(100, 100, 100, 90)), obj('s', 'solar_panel', rect(300, 300, 30, 20))])
    rows = oh.hints_for_scene(scene, None, kinds=('solar_panel',))
    assert [r['object_id'] for r in rows] == ['s']


def test_solar_sentence_names_only_the_met_reasons_and_never_prints_equal_numbers():
    measure = {'reasons': ['bright', 'non_rectangular', 'small'], 'median_luminance': 120.5, 'luminance_ceiling': 120.2, 'rect_fill': .62, 'area_m2': 1.05}
    assert oh.solar_sentence(measure) == ("Bright, non-rectangular and small for a solar panel (median grey 120.5, above the image's 60th percentile "
                                          "of 120.2; fills 62% of its rectangle; 1.1 m², smaller than one panel); check for a hot-water unit, "
                                          "air-conditioner or skylight.")
    assert oh.solar_sentence({**measure, 'reasons': ['bright'], 'median_luminance': 148., 'luminance_ceiling': 120.}) == (
        "Bright for a solar panel (median grey 148, above the image's 60th percentile of 120); check for a hot-water unit, air-conditioner or skylight.")


def test_a_write_replaces_the_scene_rows_and_drops_its_earlier_version_rows(tmp_path):
    from contextlib import closing

    from scripts.ml import eyes_index
    scene = scene_with([obj('b', 'building', rect(100, 100, 100, 90)), obj('s', 'solar_panel', rect(300, 300, 30, 20))])
    folder = tmp_path/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    old = {'codes': ['frame_cut'], 'hints': [{'code': 'frame_cut', 'severity': 'review', 'sentence': 'x', 'evidence': {}}]}
    seeded = [(scene['id'], 'b', scene['model_signature'], 'object-hints-v1'), (scene['id'], 'gone', 'earlier-sig', 'object-hints-v1'),
              (scene['id'], 'gone', scene['model_signature'], oh.VERSION), ('other-scene', 'x', 'sig', 'object-hints-v1')]
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, oh.TABLE, [{'scene_id': s, 'object_id': o, 'model_signature': m, 'version': v, 'payload': old} for s, o, m, v in seeded])
    summary = oh.run([scene['id']], tmp_path, write=True)
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        left = sorted((r['scene_id'], r['object_id'], r['version']) for r in eyes_index.rows(db, oh.TABLE))
    assert left == [(scene['id'], 'b', oh.VERSION), (scene['id'], 's', oh.VERSION), ('other-scene', 'x', 'object-hints-v1')], \
        'a scene rewritten under the current version keeps no row of an earlier one; other scenes are untouched'
    assert summary['written'] == 2 and summary['removed_previous'] == 2
