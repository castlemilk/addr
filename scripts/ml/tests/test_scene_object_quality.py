import hashlib
import io
import json
import math
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import eyes_index
from scripts.ml import scene_object_quality as q
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_region import region_frame

REPO = Path(__file__).resolve().parents[3]
TILE = 'mooroolbark-50011498-17-118442-80417'


def obj(oid, kind, ring, score=.9, status='candidate', **extra):
    label = {'tree': 'Tree', 'solar_panel': 'Solar panel', 'building': 'Building'}.get(kind, kind.title())
    return {'id': oid, 'kind': kind, 'label': label, 'status': status, 'score': score, 'ring': ring, 'model': {}, **extra}


def square(x, y, size):
    return [[x, y], [x+size, y], [x+size, y+size], [x, y+size]]


def synthetic_scene(scene_id='quality-test'):
    """400 px frame at 0.25 m/px: 1 px^2 == 0.0625 m^2."""
    scale = .25
    objects = [
        obj('B1', 'building', square(20, 20, 100)),                      # 625 m2
        obj('S1', 'solar_panel', square(30, 30, 20), parent_id='B1'),    # 25 m2 fully inside B1
        obj('S2', 'solar_panel', square(150, 150, 10), score=.7),        # 6.25 m2, no building -> unparented
        obj('S3', 'solar_panel', [[110, 30], [130, 30], [130, 50], [110, 50]], parent_id='B1'),  # half inside B1
        obj('S4', 'solar_panel', [[200, 20], [300, 20], [300, 80], [200, 80]]),  # 375 m2 giant + unparented
        obj('T1', 'tree', square(100, 100, 40)),                          # 100 m2, quarter over B1
        obj('T2', 'tree', square(180, 10, 2), score=.3),                  # .25 m2 tiny, low support
        obj('T3', 'tree', square(0, 150, 10)),                            # touches the left image edge
        obj('T4', 'tree', square(102, 102, 38)),                          # heavy overlap with T1
        obj('T5', 'tree', square(200, 200, 190)),                         # 2256 m2 giant tree
        obj('T6', 'tree', square(300, 100, 20)),                          # not owned
        obj('T7', 'tree', square(300, 130, 20), status='superseded'),     # never assessed
        obj('C1', 'car', square(160, 60, 16), score=.55),
    ]
    return {**region_frame([145.31, -37.7905, 145.311, -37.7895]), 'id': scene_id, 'title': 'Quality',
            'analysis': {'inference_profile': 'f'*64}, 'image_width': 400, 'image_height': 400,
            'image_sha256': 'a'*64, 'model_signature': 'sig-1', 'schema_version': 1,
            'pixel_to_meters': [[scale, 0], [0, -scale], [-50, 50]], 'meters_to_pixel': [[1/scale, 0], [0, -1/scale], [200, 200]],
            'owned_object_ids': [o['id'] for o in objects if o['id'] not in ('T6',)], 'objects': objects}


def saved_scene(root, scene_id='quality-test'):
    image = Image.new('RGB', (400, 400), '#777777')
    out = io.BytesIO()
    image.save(out, format='PNG')
    data = out.getvalue()
    scene = synthetic_scene(scene_id)
    scene['image_sha256'] = hashlib.sha256(data).hexdigest()
    return save_scene(scene, data, root)


def by_id(rows):
    return {r['object_id']: r['payload'] for r in rows}


def test_support_tier_boundaries_are_labelled_detector_support():
    assert q.support_tier(.49) == '<0.5'
    assert q.support_tier(.5) == '0.5-0.65'
    assert q.support_tier(.65) == '0.65-0.8'
    assert q.support_tier(.8) == '>=0.8'
    assert q.support_tier(1.) == '>=0.8'
    for bad in (None, float('nan'), 'x', True, 1.2):
        assert q.support_tier(bad) is None
    assert 'not accuracy' in q.SUPPORT_TIER_NOTE


def test_assess_scene_geometry_signals_and_flags():
    scene = synthetic_scene()
    rows = q.assess_scene(scene)
    payload = by_id(rows)
    assert set(payload) == {'S1', 'S2', 'S3', 'S4', 'T1', 'T2', 'T3', 'T4', 'T5', 'C1'}
    for row in rows:
        assert row['version'] == q.VERSION == 'object-quality-v1'
        assert row['scene_id'] == 'quality-test' and row['model_signature'] == 'sig-1'
        assert len(row['payload']['code_sha256']) == 64 and row['payload']['computed_at']
        assert row['payload']['support_tier_note'] == q.SUPPORT_TIER_NOTE
    s1 = payload['S1']
    assert s1['kind'] == 'solar_panel' and s1['label'] == 'Solar panel'
    assert s1['area_m2'] == pytest.approx(25)
    assert s1['parent_coverage'] == pytest.approx(1) and s1['parent_id'] == 'B1' and s1['parent_source'] == 'parent_id'
    assert s1['unparented'] is False and s1['plausibility_flags'] == []
    assert s1['support_tier'] == '>=0.8' and s1['edge_touch'] is False
    s2 = payload['S2']
    assert s2['parent_coverage'] == 0 and s2['building_overlap_fraction'] == 0
    assert s2['unparented'] is True and 'unparented_solar' in s2['plausibility_flags']
    assert s2['support_tier'] == '0.65-0.8'
    s3 = payload['S3']
    assert s3['parent_coverage'] == pytest.approx(.5) and s3['unparented'] is False
    s4 = payload['S4']
    assert s4['area_m2'] == pytest.approx(375)
    assert {'giant_solar', 'unparented_solar'} <= set(s4['plausibility_flags'])
    t1 = payload['T1']
    assert t1['area_m2'] == pytest.approx(100)
    assert t1['building_overlap_fraction'] == pytest.approx(.25)
    assert t1['unparented'] is None  # solar-only signal
    assert t1['parent_coverage'] == pytest.approx(.25)  # best-covering building when no parent_id
    assert t1['parent_source'] == 'best_covering'
    assert t1['same_class_overlap_max'] > .5 and t1['same_class_overlap_partner'] == 'T4'
    assert 'heavy_same_class_overlap' in t1['plausibility_flags'] and 'heavy_same_class_overlap' in payload['T4']['plausibility_flags']
    t2 = payload['T2']
    assert t2['area_m2'] == pytest.approx(.25)
    assert t2['support_tier'] == '<0.5'
    assert {'tiny_object', 'low_support'} <= set(t2['plausibility_flags'])
    assert payload['T3']['edge_touch'] is True and payload['T1']['edge_touch'] is False
    assert payload['T5']['plausibility_flags'] == ['giant_tree']
    c1 = payload['C1']
    assert c1['kind'] == 'car' and c1['support_tier'] == '0.5-0.65' and c1['plausibility_flags'] == []
    assert payload['S1']['same_class_overlap_max'] == 0


def test_only_owned_filter_and_superseded_skip():
    scene = synthetic_scene()
    owned = {r['object_id'] for r in q.assess_scene(scene)}
    everything = {r['object_id'] for r in q.assess_scene(scene, only_owned=False)}
    assert 'T6' not in owned and 'T6' in everything
    assert 'T7' not in everything
    assert 'B1' not in everything
    del scene['owned_object_ids']
    assert 'T6' in {r['object_id'] for r in q.assess_scene(scene)}


def test_log_area_z_uses_class_median_and_mad():
    scene = synthetic_scene()
    stats = {'tree': {'median_log_area': math.log(100), 'mad_log_area': .5, 'count': 10}}
    payload = by_id(q.assess_scene(scene, class_stats=stats))
    assert payload['T1']['log_area_z'] == pytest.approx(0)
    assert payload['T5']['log_area_z'] == pytest.approx((math.log(2256.25)-math.log(100))/(1.4826*.5), abs=1e-3)
    assert payload['S1']['log_area_z'] is None  # no solar statistics passed
    degenerate = {'tree': {'median_log_area': 1, 'mad_log_area': 0, 'count': 3}}
    assert by_id(q.assess_scene(scene, class_stats=degenerate))['T1']['log_area_z'] is None
    # Without stats the scene's own owned objects form the class population.
    own = by_id(q.assess_scene(scene))
    assert own['T1']['log_area_z'] is not None and own['C1']['log_area_z'] is None


def test_class_statistics_over_owned_objects(tmp_path):
    saved_scene(tmp_path)
    stats = q.class_statistics(tmp_path, ['quality-test'])
    assert stats['tree']['count'] == 5  # T1..T5; T6 unowned, T7 superseded
    assert stats['solar_panel']['count'] == 4
    # Tree areas 100, .25, 6.25, 90.25, 2256.25 m2 -> median is T4's 90.25 m2.
    assert stats['tree']['median_log_area'] == pytest.approx(math.log(90.25), abs=1e-5)
    assert stats['tree']['mad_log_area'] > 0
    assert 'building' not in stats


def test_run_writes_sidecar_rows_and_rates(tmp_path):
    scene = saved_scene(tmp_path)
    before = (tmp_path/'data/scenes/quality-test/latest.json').read_bytes()
    written = q.run(tmp_path, ['quality-test'])
    assert written == 10
    assert (tmp_path/'data/scenes/quality-test/latest.json').read_bytes() == before
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = eyes_index.rows(db, 'object_quality', scene_id='quality-test', version='object-quality-v1')
    assert len(rows) == 10 and all(r['model_signature'] == scene['model_signature'] for r in rows)
    rates = q.rates(tmp_path, ['quality-test'])
    assert rates['version'] == q.VERSION
    assert rates['population'] == {'scenes': 1, 'objects': 10, 'by_kind': {'car': 1, 'solar_panel': 4, 'tree': 5},
                                   'invalid_geometry': 0, 'no_score': 0}
    assert rates['solar_unparented_rate'] == {'numerator': 2, 'denominator': 4, 'rate': .5}
    assert rates['tree_low_score_share'] == {'numerator': 1, 'denominator': 5, 'rate': .2}
    assert rates['giant_object_counts']['denominator'] == 10
    assert rates['giant_object_counts']['by_flag'] == {'giant_solar': 1, 'giant_tree': 1}
    assert rates['same_class_overlap_pairs'] == {'numerator': 1, 'denominator': 10, 'pairs': [['quality-test', 'T1', 'T4']]}
    assert 'not accuracy' in rates['note']
    # Re-running replaces rather than duplicates rows.
    q.run(tmp_path, ['quality-test'])
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        assert len(eyes_index.rows(db, 'object_quality', scene_id='quality-test')) == 10


def test_rates_without_index_are_empty_with_denominators(tmp_path):
    rates = q.rates(tmp_path, ['nothing'])
    assert rates['population']['objects'] == 0
    assert rates['solar_unparented_rate'] == {'numerator': 0, 'denominator': 0, 'rate': None}


def test_cli_writes_and_reports(tmp_path, capsys):
    saved_scene(tmp_path)
    assert q.main(['--root', str(tmp_path), '--scenes', 'quality-test', '--rates']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['written'] == 10 and out['rates']['population']['objects'] == 10


@pytest.mark.skipif(not (REPO/'data/scenes'/TILE/'latest.json').exists(), reason='cluster scene not present')
def test_real_tile_scene_smoke_read_only():
    scene = json.loads((REPO/'data/scenes'/TILE/'latest.json').read_text())
    owned = set(scene['owned_object_ids'])
    expected = [o for o in scene['objects'] if o['kind'] != 'building' and o['status'] != 'superseded' and o['id'] in owned]
    rows = q.assess_scene(scene)
    assert len(rows) == len(expected)
    kinds = {r['payload']['kind'] for r in rows}
    assert kinds <= {'tree', 'solar_panel'}
    assert all(r['payload']['area_m2'] > 0 for r in rows)


def test_invalid_outline_is_recorded_not_raised():
    scene = synthetic_scene()
    scene['objects'].append(obj('BAD', 'tree', [[5, 5], [6, 6]]))
    scene['owned_object_ids'].append('BAD')
    payload = by_id(q.assess_scene(scene))
    assert payload['BAD']['plausibility_flags'] == ['invalid_geometry'] and payload['BAD']['area_m2'] is None
    assert payload['T1']['area_m2'] == pytest.approx(100)


def test_rerun_after_scene_edit_scope_change_and_duplicate_ids_do_not_double_count(tmp_path):
    scene = saved_scene(tmp_path)
    q.run(tmp_path, ['quality-test'])
    folder = tmp_path/'data/scenes/quality-test'
    edited = json.loads((folder/'latest.json').read_text())
    edited['objects'][1]['status'] = 'confirmed'  # a human decision re-saves the scene with a new model_signature
    edited = save_scene(edited, (folder/f"{scene['image_sha256']}.png").read_bytes(), tmp_path)
    assert edited['model_signature'] != scene['model_signature']
    assert q.run(tmp_path, ['quality-test', 'quality-test']) == 10
    q.run(tmp_path, ['quality-test'], only_owned=False)
    assert q.run(tmp_path, ['quality-test']) == 10
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = eyes_index.rows(db, 'object_quality', scene_id='quality-test')
    assert len(rows) == 10 and {r['model_signature'] for r in rows} == {edited['model_signature']}
    assert {r['payload']['population_scope'] for r in rows} == {'owned_object_ids'}
    rates = q.rates(tmp_path, ['quality-test', 'quality-test'])
    assert rates['population']['scenes'] == 1 and rates['population']['objects'] == 10
    assert rates['by_scene'] == {'quality-test': {'objects': 10, 'model_signatures': [edited['model_signature']],
                                                  'population_scope': ['owned_object_ids']}}


def test_rates_denominators_exclude_unmeasurable_objects(tmp_path):
    scene = synthetic_scene()
    scene['objects'] += [obj('BADS', 'solar_panel', [[5, 5], [6, 6]]), obj('BADT', 'tree', [[7, 7], [8, 8]], score=.2),
                         obj('NOSCORE', 'tree', square(300, 300, 10), score=None)]
    scene['owned_object_ids'] += ['BADS', 'BADT', 'NOSCORE']
    rows = q.assess_scene(scene)
    payload = by_id(rows)
    assert payload['BADT']['plausibility_flags'] == ['invalid_geometry', 'low_support']
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'object_quality', rows)
    rates = q.rates(tmp_path, ['quality-test'])
    assert rates['population']['objects'] == 13 and rates['population']['invalid_geometry'] == 2
    assert rates['population']['no_score'] == 1
    # Unmeasurable solar cannot be unparented or not: excluded from the denominator.
    assert rates['solar_unparented_rate'] == {'numerator': 2, 'denominator': 4, 'rate': .5}
    # Low support is a score property: the invalid-outline tree counts, the unscored tree does not.
    assert rates['tree_low_score_share'] == {'numerator': 2, 'denominator': 6, 'rate': .3333}
    assert rates['giant_object_counts']['denominator'] == 11
    assert rates['same_class_overlap_pairs']['denominator'] == 11


def test_owned_ids_null_and_self_intersecting_rings():
    scene = synthetic_scene()
    scene['owned_object_ids'] = None  # treated like an absent key (every live object), not a crash
    assert 'T6' in {r['object_id'] for r in q.assess_scene(scene)}
    bowtie = synthetic_scene()
    bowtie['objects'][1]['ring'] = [[30, 30], [50, 50], [50, 30], [30, 50]]  # two 100 px^2 lobes
    assert by_id(q.assess_scene(bowtie))['S1']['area_m2'] == pytest.approx(12.5)


# --- Building classification hints (building-flags-v2) -------------------------------------------------------
# Every hint is an agent hint for a reviewer, never a label; nothing here decides anything.
from scripts.ml import context_landuse as cl  # noqa: E402
from scripts.ml.scene_confidence import geographic  # noqa: E402


def star(cx, cy, outer, inner, spikes=8):
    points = []
    for k in range(2*spikes):
        radius = outer if k % 2 == 0 else inner
        angle = math.pi*k/spikes
        points.append([round(cx+radius*math.cos(angle), 2), round(cy+radius*math.sin(angle), 2)])
    return points


def building_scene(scene_id='quality-test'):
    """400 px frame at 0.25 m/px (1 px^2 == 0.0625 m^2) with one building per hint rule and controls."""
    objects = [
        obj('B1', 'building', square(20, 20, 100), score=.9),        # 625 m2 house; the property's primary roof
        obj('B2', 'building', square(140, 20, 20), score=.5),        # 25 m2 shed, centroid 22.4 m from B1: secondary structure
        obj('B3', 'building', star(250, 250, 40, 20), score=.55),    # lobed green outline: tree-like by solidity
        obj('B4', 'building', square(300, 20, 80), score=.8),        # 400 m2 next to a cafe POI, business address: commercial
        obj('B5', 'building', square(20, 200, 40), score=.7),        # 100 m2, 5 m from a park polygon: non-residential context
        obj('B6', 'building', square(150, 150, 60), score=.85),      # 225 m2 plain house: no flags
        obj('B7', 'building', square(350, 350, 30), score=.9),       # not owned
        obj('B8', 'building', square(300, 300, 40), score=.6),       # convex green outline under a tree object: tree-like by overlap
        obj('B9', 'building', square(150, 340, 40), score=.6),       # convex green outline, no tree: NOT tree-like (control)
        obj('B10', 'building', square(200, 60, 8), status='superseded'),
        obj('T1', 'tree', square(230, 230, 40)),
        obj('T2', 'tree', square(300, 300, 40)),
        obj('T3', 'tree', square(360, 200, 30), status='superseded'),
        obj('S1', 'solar_panel', square(30, 30, 20), parent_id='B1'),
    ]
    scale = .25
    return {**region_frame([145.31, -37.7905, 145.311, -37.7895]), 'id': scene_id, 'title': 'Buildings',
            'analysis': {'inference_profile': 'f'*64}, 'image_width': 400, 'image_height': 400,
            'image_sha256': 'a'*64, 'model_signature': 'sig-b', 'schema_version': 1,
            'pixel_to_meters': [[scale, 0], [0, -scale], [-50, 50]], 'meters_to_pixel': [[1/scale, 0], [0, -1/scale], [200, 200]],
            'owned_object_ids': [o['id'] for o in objects if o['id'] != 'B7'], 'objects': objects}


def building_image():
    from PIL import ImageDraw
    image = Image.new('RGB', (400, 400), '#777777')
    draw = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in [(205, 205, 295, 295), (300, 300, 340, 340), (150, 340, 190, 380)]:
        draw.rectangle([x0, y0, x1-1, y1-1], fill='#33aa22')  # ExG = 2*170-51-34 = 255
    return image


def lonlat_of(pixel_ring, scene):
    return geographic({'ring': pixel_ring}, scene).centroid.coords[0]


def building_context(scene, *, inventory=True, landuse='available'):
    lon, lat = lonlat_of(square(392, 56, 6), scene)   # 3 m east of B4
    park = geographic({'ring': square(20, 260, 40)}, scene)   # 20 px == 5 m south of B5
    features = [{'osm_type': 'way', 'osm_id': 11, 'tag_key': 'leisure', 'tag_value': 'park', 'name': 'Kiloran Park',
                 'geometry': json.loads(json.dumps(cl.mapping(park)))}]
    doc = cl.document(features, (145.30, -37.80, 145.32, -37.78)) if landuse == 'available' else {'status': 'unavailable', 'features': []}
    props = {'quality-test/B1': {'property_pfi': 'P1', 'area_m2': 625., 'full_address': '1 TEST STREET MOOROOLBARK 3138'},
             'quality-test/B2': {'property_pfi': 'P1', 'area_m2': 25., 'full_address': '1 TEST STREET MOOROOLBARK 3138'},
             'quality-test/B4': {'property_pfi': 'P4', 'area_m2': 400., 'full_address': '2 SHOP STREET MOOROOLBARK 3138'},
             'quality-test/B5': {'property_pfi': 'P5', 'area_m2': 100., 'site_names': ['KILORAN PARK'], 'address_kind': 'unit_address'}}
    return q.Context(inventory=props if inventory else None,
                     address_kinds={q.normalise_address('2 SHOP STREET, Mooroolbark VIC 3138'): 'business'},
                     pois=[{'id': 1, 'name': 'Corner cafe', 'category': 'cafe', 'lon': lon, 'lat': lat}],
                     landuse=cl.Landuse(doc))


def hints(rows):
    return {r['object_id']: r['payload']['class_hints'] for r in rows if r['payload']['kind'] == 'building'}


def flags(payload):
    return [f['flag'] for f in payload['flags']]


def test_building_rows_are_opt_in_versioned_and_leave_other_rows_unchanged():
    scene = building_scene()
    plain = q.assess_scene(scene)
    assert {r['payload']['kind'] for r in plain} == {'tree', 'solar_panel'}
    rows = q.assess_scene(scene, buildings=True, image=building_image(), context=building_context(scene))
    buildings = [r for r in rows if r['payload']['kind'] == 'building']
    assert {r['object_id'] for r in buildings} == {'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B8', 'B9'}  # B7 unowned, B10 superseded
    for row in buildings:
        assert row['version'] == q.BUILDING_VERSION == 'building-flags-v2' == row['payload']['version']
        assert row['payload']['class_hints']['note'] == q.HINT_NOTE == 'agent hint, not a label'
        assert row['payload']['class_hints']['version'] == q.BUILDING_VERSION
        assert row['payload']['image_available'] is True and row['payload']['area_m2'] > 0
        assert row['payload']['support_tier_note'] == q.SUPPORT_TIER_NOTE
        for hint in row['payload']['class_hints']['flags']:
            assert hint['evidence'] and hint['evidence']['rule']
    others = [r for r in rows if r['payload']['kind'] != 'building']
    strip = lambda r: {**r, 'payload': {k: v for k, v in r['payload'].items() if k != 'computed_at'}}  # noqa: E731
    assert [strip(r) for r in others] == [strip(r) for r in plain]
    assert [r['version'] for r in others] == [q.VERSION]*len(others)


def test_tree_like_needs_green_and_lobes_or_a_tree():
    scene = building_scene()
    payload = hints(q.assess_scene(scene, buildings=True, image=building_image(), context=building_context(scene)))
    lobed = payload['B3']['features']['tree_like']
    assert lobed['exg_green_share'] >= .95 and lobed['solidity'] < .7 and lobed['vertex_density'] > 5
    assert lobed['tree_overlap_iou'] > 0 and lobed['texture'] >= 0
    assert flags(payload['B3']) == ['tree-like'] and payload['B3']['class_hint'] == 'tree-like'
    evidence = payload['B3']['flags'][0]['evidence']
    assert evidence['exg_green_share'] == lobed['exg_green_share'] and evidence['solidity'] == lobed['solidity']
    assert 'exg_green_share >= 0.45' in evidence['rule']
    convex_under_tree = payload['B8']['features']['tree_like']
    assert convex_under_tree['solidity'] == pytest.approx(1, abs=1e-6) and convex_under_tree['tree_overlap_iou'] == pytest.approx(1, abs=1e-6)
    assert 'tree-like' in flags(payload['B8'])
    control = payload['B9']['features']['tree_like']
    assert control['exg_green_share'] >= .95 and control['solidity'] == pytest.approx(1, abs=1e-6) and control['tree_overlap_iou'] == 0
    assert 'tree-like' not in flags(payload['B9']) and payload['B9']['class_hint'] == 'house'
    grey = payload['B1']['features']['tree_like']
    assert grey['exg_green_share'] == 0 and 'tree-like' not in flags(payload['B1'])


def test_secondary_structure_uses_property_then_proximity():
    scene = building_scene()
    with_property = hints(q.assess_scene(scene, buildings=True, image=building_image(), context=building_context(scene)))
    shed = with_property['B2']['features']['secondary_structure']
    assert shed == {'area_m2': 25., 'largest_same_property_area': 625., 'largest_object': 'quality-test/B1', 'ratio': 25.,
                    'source': 'vicmap-property', 'property_pfi': 'P1', 'same_property_buildings': 2}
    assert flags(with_property['B2']) == ['secondary-structure'] and with_property['B2']['class_hint'] == 'outbuilding'
    assert with_property['B1']['features']['secondary_structure']['ratio'] == pytest.approx(25/625) and 'secondary-structure' not in flags(with_property['B1'])
    # Without property evidence the proxy is the largest building whose centroid lies within 25 m.
    proxy = hints(q.assess_scene(scene, buildings=True, image=building_image(), context=building_context(scene, inventory=False)))
    shed = proxy['B2']['features']['secondary_structure']
    assert shed['source'] == 'proximity-25m' and shed['largest_object'] == 'B1' and shed['largest_same_property_area'] == 625.
    assert shed['property_pfi'] is None and shed['ratio'] == 25.
    assert flags(proxy['B2']) == ['secondary-structure']
    # A house with nothing bigger nearby has no partner and no flag.
    lone = proxy['B6']['features']['secondary_structure']
    assert lone['largest_same_property_area'] is not None and lone['ratio'] < 3 and 'secondary-structure' not in flags(proxy['B6'])
    assert proxy['B6']['class_hint'] == 'house'


def test_non_residential_context_and_commercial_hint():
    scene = building_scene()
    payload = hints(q.assess_scene(scene, buildings=True, image=building_image(), context=building_context(scene)))
    shop = payload['B4']['features']['non_residential']
    assert shop['poi_within_30m'] == [{'name': 'Corner cafe', 'category': 'cafe', 'distance_m': pytest.approx(3.75, abs=.3)}]  # 15 px east of B4
    assert shop['address_kind'] == 'business' and shop['landuse'] == [] and shop['landuse_status'] == 'available'
    assert flags(payload['B4']) == ['non-residential-context'] and payload['B4']['class_hint'] == 'commercial'
    assert payload['B4']['flags'][0]['evidence']['poi_within_30m'][0]['category'] == 'cafe' and payload['B4']['flags'][0]['evidence']['area_m2'] == 400.
    park = payload['B5']['features']['non_residential']
    assert park['landuse'] == [{'tag': 'leisure=park', 'name': 'Kiloran Park', 'distance_m': pytest.approx(5, abs=.3), 'osm_id': 11}]
    assert park['poi_within_30m'] == [] and park['site_names'] == ['KILORAN PARK'] and park['site_name_match'] is True
    assert flags(payload['B5']) == ['non-residential-context'] and payload['B5']['class_hint'] == 'non-residential'
    house = payload['B6']['features']['non_residential']
    assert house == {'poi_within_30m': [], 'landuse': [], 'landuse_status': 'available', 'address_kind': None, 'site_names': [],
                     'site_name_match': False, 'property_address_kind': None}
    assert payload['B6']['flags'] == [] and payload['B6']['class_hint'] == 'house'


def test_landuse_unavailable_and_missing_image_are_recorded_not_flagged():
    scene = building_scene()
    payload = hints(q.assess_scene(scene, buildings=True, image=None, context=building_context(scene, landuse='unavailable')))
    assert payload['B3']['features']['tree_like'] == {'exg_green_share': None, 'texture': None, 'solidity': pytest.approx(payload['B3']['features']['tree_like']['solidity']),
                                                      'vertex_density': pytest.approx(payload['B3']['features']['tree_like']['vertex_density']),
                                                      'tree_overlap_iou': pytest.approx(payload['B3']['features']['tree_like']['tree_overlap_iou']), 'image': 'unavailable'}
    assert 'tree-like' not in flags(payload['B3']) and 'tree-like' not in flags(payload['B8'])
    assert payload['B5']['features']['non_residential']['landuse_status'] == 'unavailable' and payload['B5']['features']['non_residential']['landuse'] == []
    assert flags(payload['B5']) == ['non-residential-context']  # the Vicmap site name still says park
    assert flags(payload['B2']) == ['secondary-structure']
    rows = q.assess_scene(scene, buildings=True, image=None, context=building_context(scene))
    assert all(r['payload']['image_available'] is False for r in rows if r['payload']['kind'] == 'building')


def test_units_hint_and_class_rules():
    assert q.class_hint([], {'secondary_structure': {'area_m2': 200}, 'non_residential': {'address_kind': 'units', 'property_address_kind': None}}) == 'units'
    assert q.class_hint([], {'secondary_structure': {'area_m2': 200}, 'non_residential': {'address_kind': None, 'property_address_kind': 'unit_address'}}) == 'units'
    assert q.class_hint(['tree-like', 'non-residential-context'], {'secondary_structure': {'area_m2': 900}, 'non_residential': {'address_kind': 'business'}}) == 'tree-like'
    assert q.class_hint([], {'secondary_structure': {'area_m2': 450}, 'non_residential': {'address_kind': 'large', 'property_address_kind': None}}) == 'commercial'
    assert q.class_hint([], {'secondary_structure': {'area_m2': 399}, 'non_residential': {'address_kind': 'large', 'property_address_kind': None}}) == 'house'
    assert q.class_hint(['non-residential-context', 'secondary-structure'], {'secondary_structure': {'area_m2': 40}, 'non_residential': {'address_kind': None}}) == 'non-residential'
    assert q.class_hint(['secondary-structure'], {'secondary_structure': {'area_m2': 40}, 'non_residential': {'address_kind': None, 'property_address_kind': None}}) == 'outbuilding'
    assert set(q.CLASS_HINTS) == {'house', 'units', 'commercial', 'outbuilding', 'tree-like', 'non-residential'}


def test_normalise_address_matches_gnaf_and_vicmap_spellings():
    assert q.normalise_address('77 HAWTHORY ROAD, Mooroolbark VIC 3138') == q.normalise_address('77 HAWTHORY ROAD MOOROOLBARK 3138') == '77 HAWTHORY ROAD MOOROOLBARK 3138'
    assert q.normalise_address('  Unit 3/12 Main St,  Mooroolbark  VIC 3138 ') == 'UNIT 3/12 MAIN ST MOOROOLBARK 3138'
    assert q.normalise_address(None) is None


def saved_building_scene(root):
    out = io.BytesIO()
    building_image().save(out, format='PNG')
    data = out.getvalue()
    scene = building_scene()
    scene['image_sha256'] = hashlib.sha256(data).hexdigest()
    return save_scene(scene, data, root)


def test_run_with_buildings_writes_rows_rates_and_examples(tmp_path):
    scene = saved_building_scene(tmp_path)
    before = (tmp_path/'data/scenes/quality-test/latest.json').read_bytes()
    assert q.run(tmp_path, ['quality-test']) == 3  # default: non-buildings only (S1, T1, T2), as before
    context = building_context(scene)
    assert q.run(tmp_path, ['quality-test'], buildings=True, context=context) == 3+8
    assert (tmp_path/'data/scenes/quality-test/latest.json').read_bytes() == before
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = eyes_index.rows(db, 'object_quality', scene_id='quality-test', version=q.BUILDING_VERSION)
        assert len(rows) == 8 and {r['model_signature'] for r in rows} == {scene['model_signature']}
        assert len(eyes_index.rows(db, 'object_quality', scene_id='quality-test', version=q.VERSION)) == 3
        assert all(r['payload']['image_available'] for r in rows)
    rates = q.rates(tmp_path, ['quality-test'])
    assert rates['population']['objects'] == 3  # non-building population unchanged
    building = rates['building_flags']
    assert building['version'] == q.BUILDING_VERSION and building['note'] == q.HINT_NOTE
    assert building['buildings'] == 8 and building['with_image'] == 8 and building['scenes'] == 1
    assert building['flags'] == {'tree-like': {'numerator': 2, 'denominator': 8, 'rate': .25},
                                 'secondary-structure': {'numerator': 1, 'denominator': 8, 'rate': .125},
                                 'non-residential-context': {'numerator': 2, 'denominator': 8, 'rate': .25}}
    assert building['any_flag'] == {'numerator': 5, 'denominator': 8, 'rate': .625}
    assert building['class_hints'] == {'commercial': 1, 'house': 3, 'non-residential': 1, 'outbuilding': 1, 'tree-like': 2}
    assert building['secondary_source'] == {'vicmap-property': 4, 'proximity-25m': 4}
    assert building['landuse_status'] == {'available': 8}
    # Which evidence carried each non-residential flag (a row may count under several kinds), with the flag count as denominator.
    assert building['non_residential_evidence'] == {'denominator': 2, 'landuse': 1, 'poi_within_30m': 1, 'address_kind': 1, 'site_name': 1}
    assert building['poi_categories'] == {'cafe': 1} and building['landuse_tags'] == {'leisure=park': 1}
    examples = q.examples(tmp_path, ['quality-test'], n=3, seed=1)
    assert len(examples) == 3 and all(e['flags'] for e in examples) and all(e['area_m2'] > 0 for e in examples)
    assert examples == q.examples(tmp_path, ['quality-test'], n=3, seed=1) and {e['scene_id'] for e in examples} == {'quality-test'}
    assert all(set(e) == {'scene_id', 'object_id', 'area_m2', 'flags', 'class_hint', 'review_url'} for e in examples)
    assert len(q.examples(tmp_path, ['quality-test'], n=20, seed=1)) == 5
    # Re-running replaces rather than duplicates, and the unowned building never appears.
    q.run(tmp_path, ['quality-test'], buildings=True, context=context)
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        rows = eyes_index.rows(db, 'object_quality', scene_id='quality-test')
    assert len(rows) == 11 and 'B7' not in {r['object_id'] for r in rows}
    assert q.run(tmp_path, ["quality-test"], only_owned=False, buildings=True, context=context) == 3+9  # B7 joins the buildings


def test_building_cli_flags(tmp_path, capsys):
    saved_building_scene(tmp_path)
    assert q.main(['--root', str(tmp_path), '--scenes', 'quality-test', '--buildings', '--offline', '--rates', '--examples', '2']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['written'] == 11 and out['rates']['building_flags']['buildings'] == 8
    assert out['rates']['building_flags']['landuse_status'] == {'unavailable': 8}
    assert len(out['examples']) == 2 and out['context']['landuse'] == 'unavailable' and out['context']['inventory'] == 'unavailable'
    assert out['context']['pois'] == 0


def test_invalid_building_outline_is_recorded():
    scene = building_scene()
    scene['objects'].append(obj('BADB', 'building', [[5, 5], [6, 6]]))
    scene['owned_object_ids'].append('BADB')
    rows = {r['object_id']: r['payload'] for r in q.assess_scene(scene, buildings=True, image=building_image(), context=building_context(scene))}
    assert rows['BADB']['error'] == 'invalid outline' and rows['BADB']['class_hints']['flags'] == [] and rows['BADB']['class_hints']['class_hint'] is None
    assert rows['B2']['class_hints']['class_hint'] == 'outbuilding'


@pytest.mark.skipif(not (REPO/'data/scenes'/TILE/'latest.json').exists(), reason='cluster scene not present')
def test_real_tile_building_hints_smoke_read_only():
    scene = json.loads((REPO/'data/scenes'/TILE/'latest.json').read_text())
    rows = q.assess_scene(scene, buildings=True, image=None, context=q.Context())
    buildings = [r for r in rows if r['payload']['kind'] == 'building']
    owned = set(scene['owned_object_ids'])
    assert len(buildings) == sum(1 for o in scene['objects'] if o['kind'] == 'building' and o['status'] != 'superseded' and o['id'] in owned)
    assert all(r['payload']['class_hints']['features']['non_residential']['landuse_status'] == 'unavailable' for r in buildings)
    assert all('tree-like' not in flags(r['payload']['class_hints']) for r in buildings)  # no image, no tree-like


def test_scene_outside_context_coverage_is_recorded_not_available():
    """Land use fetched for the suburb box says nothing about a capture 30 km away; the row must not claim 'available'."""
    scene = building_scene()
    far = {**building_scene(), **region_frame([144.98, -37.80, 144.981, -37.799]), 'id': 'quality-test'}  # same id, objects and inventory keys, 30 km west
    context = building_context(scene)
    assert context.coverage == (145.30, -37.80, 145.32, -37.78) and context.covers(scene) and not context.covers(far)
    assert context.landuse_status(scene) == 'available' and context.landuse_status(far) == 'outside coverage'
    near = hints(q.assess_scene(scene, buildings=True, image=building_image(), context=context))
    away = hints(q.assess_scene(far, buildings=True, image=building_image(), context=context))
    assert near['B5']['features']['non_residential']['landuse_status'] == 'available' and near['B5']['features']['non_residential']['landuse']
    assert away['B5']['features']['non_residential']['landuse_status'] == 'outside coverage' and away['B5']['features']['non_residential']['landuse'] == []
    assert away['B5']['flags'][0]['evidence']['landuse_status'] == 'outside coverage'  # the Vicmap site name still carries the flag
    assert all(p['features']['non_residential']['landuse_status'] == 'outside coverage' for p in away.values() if p['features'])
    # Rows inside the coverage box are unchanged by the coverage check (byte identity for the suburb tiles).
    assert {k: v['features']['non_residential']['landuse_status'] for k, v in near.items()} == dict.fromkeys(near, 'available')
    assert 'coverage' in context.describe() and context.describe()['coverage'] == [145.30, -37.80, 145.32, -37.78]
    # Offline stays 'unavailable' everywhere; an unknown coverage box never invents a gap.
    offline = building_context(scene, landuse='unavailable')
    assert offline.coverage is None and offline.landuse_status(far) == 'unavailable'
    assert q.Context(landuse=cl.Landuse(cl.document([], (145.30, -37.80, 145.32, -37.78))), coverage=(0, 0, 1, 1)).landuse_status(scene) == 'outside coverage'


def test_non_rgb_images_give_the_same_hints_as_rgb():
    scene, context = building_scene(), building_context(building_scene())
    image = building_image()
    rgb = hints(q.assess_scene(scene, buildings=True, image=image, context=context))
    for variant in (np.asarray(image.convert('RGBA')), image.convert('RGBA'), np.asarray(image)[..., :3].copy()):
        got = hints(q.assess_scene(scene, buildings=True, image=variant, context=context))
        assert {k: flags(v) for k, v in got.items()} == {k: flags(v) for k, v in rgb.items()}
        assert got['B3']['features']['tree_like']['exg_green_share'] == rgb['B3']['features']['tree_like']['exg_green_share']
    grey = hints(q.assess_scene(scene, buildings=True, image=np.asarray(image.convert('L')), context=context))
    assert grey['B3']['features']['tree_like']['exg_green_share'] == 0 and 'tree-like' not in flags(grey['B3'])  # grey has no green excess
    assert q._rgb_array(np.zeros((4, 4, 2))) is None and q._rgb_array(None) is None
    unusable = q.assess_scene(scene, buildings=True, image=np.zeros((4, 4, 2)), context=context)
    assert all(r['payload']['image_available'] is False for r in unusable if r['payload']['kind'] == 'building')


def test_class_hints_for_scene_uses_cached_only_context_and_returns_hints_per_building(tmp_path):
    from scripts.ml.scene_object_quality import (
        BUILDING_VERSION,
        Context,
        class_hints_for_scene,
        load_context_cached,
    )
    context = load_context_cached(tmp_path, ('nope',))
    assert context.describe()['inventory'] == 'unavailable' and context.describe()['landuse'] == 'unavailable'
    assert context.notes.get('mode') == 'cached-only' and 'inventory_error' in context.notes
    frame = [[.25, 0], [0, -.25], [-64, 64]]
    ring = [[176, 208], [336, 208], [336, 304], [176, 304]]
    shed = [[204, 186], [220, 186], [220, 198], [204, 198]]     # 4 m x 3 m, centroids 19 m apart: a proximity secondary structure
    scene = {'id': 'synthetic', 'image_width': 512, 'image_height': 512, 'pixel_to_meters': frame, 'meters_to_pixel': [[4, 0], [0, -4], [256, 256]],
             'objects': [{'id': 'b1', 'kind': 'building', 'ring': ring, 'status': 'candidate', 'score': .9},
                         {'id': 'b2', 'kind': 'building', 'ring': shed, 'status': 'candidate', 'score': .8},
                         {'id': 't1', 'kind': 'tree', 'ring': shed, 'status': 'candidate', 'score': .8}]}
    result = class_hints_for_scene(scene, context=Context())
    assert result['version'] == BUILDING_VERSION and result['note'] == 'agent hint, not a label'
    assert set(result['hints']) == {'b1', 'b2'} and result['hints']['b1'] == 'house' and result['hints']['b2'] == 'outbuilding'
    assert result['context']['inventory'] == 'unavailable'
    cached = class_hints_for_scene(scene, root=tmp_path)      # memoised cached-only context for a root without caches
    assert cached['hints'] == result['hints'] and cached['context']['landuse'] == 'unavailable'


def test_secondary_structure_ratio_is_tiered_by_evidence_source():
    """building-flags-v2: a 63 m² roof with a 151 m² sibling on the same Vicmap property (ratio 2.39) is a secondary
    structure; the same ratio through the 25 m proximity proxy is not (the proxy keeps the 3x rule)."""
    from scripts.ml import scene_object_quality as q
    assert q.BUILDING_VERSION == 'building-flags-v2'
    base = {'tree_like': {'exg_green_share': 0., 'solidity': 1., 'tree_overlap_iou': 0.},
            'non_residential': {'landuse': [], 'poi_within_30m': [], 'address_kind': None, 'site_name_match': False, 'landuse_status': 'available'}}
    prop = {**base, 'secondary_structure': {'area_m2': 63.379, 'largest_same_property_area': 151.325, 'ratio': 2.3876, 'source': 'vicmap-property',
                                            'property_pfi': '1348074', 'largest_object': 'x/y'}}
    proxy = {**base, 'secondary_structure': {**prop['secondary_structure'], 'source': 'proximity-25m', 'property_pfi': None}}
    strong = {**base, 'secondary_structure': {**proxy['secondary_structure'], 'ratio': 3.1}}
    assert [f['flag'] for f in q.building_hints(prop)] == ['secondary-structure']
    assert q.class_hint(['secondary-structure'], prop) == 'outbuilding'
    assert q.building_hints(proxy) == []
    assert [f['flag'] for f in q.building_hints(strong)] == ['secondary-structure']
    assert 'same Vicmap property' in q.RULES['secondary-structure'] and q.BUILDING_THRESHOLDS['secondary_ratio_property'] == 2.
