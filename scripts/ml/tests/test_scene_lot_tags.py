"""lot-tags-v1: per-object lot tags from injected parcels on a synthetic 512 px scene (hints, never labels)."""
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from scripts.ml import eyes_index
from scripts.ml import scene_lot_tags as T
from scripts.ml.scene_addresses import image_projection
from scripts.ml.scene_batch import signature

SCENE = {
    'schema_version': 1, 'id': 'qa_lot_tags', 'address_id': 4242, 'cohort': 'roof_training_250',
    'title': 'Unit UNIT 4 63 TEST ROAD, Mooroolbark VIC 3138',
    'centroid': {'lon': 145.3125, 'lat': -37.7905}, 'image_sha256': 'a'*64, 'image_width': 512, 'image_height': 512,
    'pixel_to_meters': [[.1, 0], [0, -.1], [-25.6, 25.6]], 'meters_to_pixel': [[10, 0], [0, -10], [256, 256]],
    'model_signature': 'f'*64, 'objects': [], 'groups': [], 'scans': {}, 'height_note': 'test',
}


def px_ring(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def lot(pfi, number, plan, x0, y0, x1, y1, scene=SCENE):
    unproject = T.inverse_projection(scene)
    ring = [list(unproject(x, y)) for x, y in px_ring(x0, y0, x1, y1)]
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring+[ring[0]]]},
            'properties': {'pfi': pfi, 'spi': f'{number}\\{plan}', 'lot': number, 'plan': plan, 'status': 'approved',
                           'horizontal_uncertainty_m': .5}}


def obj(identity, kind, ring, **extra):
    return {'id': identity, 'kind': kind, 'label': kind, 'ring': ring, 'status': 'candidate', 'source': 'sam3-text-onnx',
            'geometry_reviewed': False, 'model': {'height_m': 3.}, **extra}


@pytest.fixture
def site():
    scene = copy.deepcopy(SCENE)
    scene['objects'] = [
        obj('edge0000', 'building', px_ring(400, 150, 512, 250)),           # on the neighbour lot, touching the frame
        obj('straddle', 'building', px_ring(260, 150, 350, 250)),           # 40% Lot 1, 60% Lot 7
        obj('stacked0', 'building', px_ring(110, 350, 290, 490)),           # inside the stacked Lots 2/3 polygon
        obj('common00', 'vegetation', px_ring(120, 305, 280, 335)),         # on CM1
        obj('remnant0', 'building', px_ring(150, 150, 200, 200), clipped_by_frame=[50, 50, 250, 250]),
        obj('interior', 'building', px_ring(105, 105, 245, 245)),           # the largest roof on the address lot
    ]
    # A frame in the same geometry as the scene: Lot 1 (address), Lot 7 of another plan leaving the frame, CM1, and
    # Lots 2 and 3 stacked on one polygon (Vicmap carries no unit cuts).
    features = [lot('p1', '1', 'PS1', 100, 100, 300, 300), lot('p7', '7', 'LP9', 300, 100, 600, 300),
                lot('pcm', 'CM1', 'PS1', 100, 300, 300, 340), lot('p2', '2', 'PS1', 100, 340, 300, 500),
                lot('p3', '3', 'PS1', 100, 340, 300, 500)]
    parcels = {'type': 'FeatureCollection', 'features': features, 'bounds': [145.311, -37.792, 145.314, -37.789]}
    seed = {'address_id': 4242, 'ring': px_ring(120, 120, 230, 230), 'point': [170, 170], 'title': scene['title']}
    return scene, parcels, seed


def test_inverse_projection_round_trips_image_projection():
    unproject, project = T.inverse_projection(SCENE), image_projection(SCENE)
    for x, y in ((0, 0), (512, 512), (37.5, 400.25), (256, 256)):
        lon, lat = unproject(x, y)
        assert project(lon, lat) == pytest.approx((x, y), abs=1e-6)
    west, south, east, north = T.frame_lonlat_bounds(SCENE)
    assert west < SCENE['centroid']['lon'] < east and south < SCENE['centroid']['lat'] < north
    assert (east-west)*T.WORLD/360*abs(__import__('math').cos(__import__('math').radians(-37.7905))) == pytest.approx(51.2, abs=.05)


def test_edge_object_on_a_neighbour_lot_is_frame_cut_and_unassociated(site):
    scene, parcels, seed = site
    tags, scene_tag = T.analyse(scene, parcels, seed=seed)
    tag = tags['edge0000']
    assert tag['flags'] == ['frame_cut', 'lot_incomplete_in_frame', 'lot_unassociated']
    assert tag['dominant'] == {'pfi': 'p7', 'lot': '7', 'plan': 'LP9', 'share': 1., 'kind': 'lot'}
    assert tag['lot_complete_in_image'] is False and tag['on_address_lot'] is False
    assert tag['sentences'] == ['Touches the image edge; the frame cut it, not the building.',
                                'On neighbouring Lot 7 LP9, not the address lot; cut by the frame.']
    assert scene_tag['address_plan'] == 'PS1' and scene_tag['address_lot'] == '1' and scene_tag['address_method'] == 'crm-seed'


def test_straddling_object_spans_lots_but_stacked_strata_do_not(site):
    scene, parcels, seed = site
    tags, _ = T.analyse(scene, parcels, seed=seed)
    straddle = tags['straddle']
    assert 'spans_lots' in straddle['flags'] and 'lot_unassociated' in straddle['flags']
    assert [lot['lot'] for lot in straddle['lots']] == ['7', '1']
    assert straddle['lots'][0]['share'] == pytest.approx(.5556, abs=.001) and straddle['lots'][1]['share'] == pytest.approx(.4444, abs=.001)
    assert any(s.startswith('Spans Lot 7 (56%) and Lot 1 (44%) of LP9, PS1; a cadastral cut') for s in straddle['sentences'])
    stacked = tags['stacked0']
    assert 'spans_lots' not in stacked['flags'] and 'lot_unassociated' not in stacked['flags']
    assert stacked['dominant']['lot'] == '2' and stacked['lots'][0]['stacked_lots'] == ['2', '3']
    assert stacked.get('neighbouring_unit') is True
    assert stacked['sentences'] == ['On Lot 2 of the address plan PS1: a neighbouring unit of the same block.']


def test_common_property_and_interior_objects(site):
    scene, parcels, seed = site
    tags, _ = T.analyse(scene, parcels, seed=seed)
    common = tags['common00']
    assert common['flags'] == ['common_property'] and common['dominant']['kind'] == 'common_property'
    assert common['sentences'] == ['On common property CM1 of PS1.']
    interior = tags['interior']
    assert interior['flags'] == [] and interior['sentences'] == [] and interior['on_address_lot'] is True
    assert interior['lot_primary'] == 'interior' and [m['object_id'] for m in interior['lot_members']] == ['interior', 'remnant0']
    assert interior['lot_members'][0]['area_m2'] == pytest.approx(196., abs=.01)


def test_clipped_remnant_is_frame_cut_unless_another_object_replaces_it(site):
    scene, parcels, seed = site
    tags, _ = T.analyse(scene, parcels, seed=seed)
    remnant = tags['remnant0']
    assert remnant['flags'] == ['frame_cut'] and remnant['clipped_by_frame'] == [50, 50, 250, 250]
    assert remnant['sentences'] == ['Cut by the earlier 512 px frame; nothing replaced it when the scene was widened.']
    assert remnant['lot_primary'] == 'interior' and remnant['primary_ratio'] == pytest.approx(25/196, abs=.001)
    scene['objects'].append(obj('fuller00', 'building', px_ring(140, 140, 210, 210), replaces=['remnant0']))
    tags, _ = T.analyse(scene, parcels, seed=seed)
    assert 'frame_cut' not in tags['remnant0']['flags']
    # A superseded object cannot vouch for a replacement.
    scene['objects'][-1]['status'] = 'superseded'
    tags, _ = T.analyse(scene, parcels, seed=seed)
    assert 'frame_cut' in tags['remnant0']['flags'] and 'fuller00' not in tags


def test_unit_site_coverage_and_title_mismatch_sentences(site):
    scene, parcels, seed = site
    _, scene_tag = T.analyse(scene, parcels, seed=seed)
    assert scene_tag['unit_site'] is True
    assert scene_tag['unit_site_sentence'] == ('Plan PS1: Lots 1-3 + CM1 (3 polygons). This image covers Lot 1 fully, '
                                               'Common property CM1 fully, Lots 2, 3 fully.')
    assert scene_tag['title_mismatch_sentence'] == 'Title says Unit 4; the CRM roof sits on Lot 1 of PS1. A hint to check the plan, not a correction.'
    # A plan without common property is no unit site; a seed on the titled unit raises no mismatch.
    ordinary = {**parcels, 'features': [f for f in parcels['features'] if f['properties']['lot'] != 'CM1']}
    _, plain = T.analyse(scene, ordinary, seed=seed)
    assert plain['unit_site'] is False and plain['unit_site_sentence'] is None
    _, unit4 = T.analyse(scene, parcels, seed={**seed, 'ring': px_ring(110, 350, 290, 490)})
    assert unit4['address_lot'] == '2' and 'Unit 4' in unit4['title_mismatch_sentence']


def test_seed_straddling_lots_defers_to_the_address_point(site):
    scene, parcels, seed = site
    straddling = {**seed, 'ring': px_ring(200, 250, 400, 350), 'point': [450, 200]}  # 25/25/20/5% across four polygons
    _, scene_tag = T.analyse(scene, parcels, seed=straddling)
    assert scene_tag['address_method'] == 'address-point' and scene_tag['address_lot'] == '7'
    _, no_point = T.analyse(scene, parcels, seed={**straddling, 'point': None})
    assert no_point['address_method'] == 'crm-seed' and no_point['address_lot'] in ('1', '7')


def test_without_parcels_every_object_is_untagged_and_nothing_flags(site):
    scene, _, seed = site
    tags, scene_tag = T.analyse(scene, None, seed=seed)
    assert all(tag['flags'] in ([], ['frame_cut']) and tag['dominant'] is None for tag in tags.values())
    assert scene_tag['groups'] == 0 and scene_tag['address_plan'] is None and scene_tag['unit_site'] is False


def test_lot_labels_and_geometry_keys():
    assert T.lot_label(['1']) == 'Lot 1' and T.lot_label(['CM1']) == 'Common property CM1'
    assert T.lot_label(['1', 'CM1']) == 'Lot 1 + CM1' and T.lot_label(['4', '1', '2', '3', 'CM1']) == 'Lots 1-4 + CM1'
    assert T.lot_label(['1', '3']) == 'Lots 1, 3' and T.lot_label([]) == 'unnumbered parcel'
    a = {'type': 'Polygon', 'coordinates': [[[145.1, -37.1], [145.2, -37.1], [145.2, -37.2], [145.1, -37.1]]]}
    b = {'type': 'Polygon', 'coordinates': [[[145.1+1e-9, -37.1], [145.2, -37.1], [145.2, -37.2], [145.1, -37.1]]]}
    assert T.geometry_key(a) == T.geometry_key(b)
    assert T.geometry_key(a) != T.geometry_key({**a, 'coordinates': [[[145.1, -37.1], [145.3, -37.1], [145.2, -37.2], [145.1, -37.1]]]})


def _write_scene(root, scene):
    folder = root/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene, indent=2))
    return folder/'latest.json'


def _freeze(root, scene, parcels):
    path, key = T.frozen_parcels_path(scene, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'key': key, 'sha256': signature(parcels), 'data': parcels}))
    return path


def test_parcels_for_scene_order_and_offline_behaviour(site, tmp_path):
    scene, parcels, _ = site
    assert T.parcels_for_scene(scene, tmp_path, offline=True) == (None, None, 'unavailable')
    _freeze(tmp_path, scene, parcels)
    data, digest, source = T.parcels_for_scene(scene, tmp_path, offline=True)
    assert source == 'eyes-parcels' and digest == signature(parcels) and data == parcels
    owned = {**scene, 'property_roofs': {'parcels': parcels}}
    assert T.parcels_for_scene(owned, tmp_path, offline=True)[2] == 'latest.json'
    # A frozen units cache whose bounds cover the frame is used before any network read.
    units = tmp_path/'data/reviews/eyes/units'
    units.mkdir(parents=True)
    (units/'parcels-tile.json').write_text(json.dumps({'scene_id': 'tile', 'data': parcels}))
    scene_b = {**scene, 'image_sha256': 'b'*64}
    assert T.parcels_for_scene(scene_b, tmp_path, offline=True)[2] == 'parcel-caches'
    (units/'parcels-tile.json').write_text(json.dumps({'scene_id': 'tile', 'data': {**parcels, 'bounds': [145.3126, -37.79, 145.314, -37.789]}}))
    assert T.parcels_for_scene(scene_b, tmp_path, offline=True) == (None, None, 'unavailable')


def test_run_never_touches_latest_json_and_write_false_leaves_the_index_absent(site, tmp_path):
    scene, parcels, seed = site
    latest = _write_scene(tmp_path, scene)
    before = latest.read_bytes()
    _freeze(tmp_path, scene, parcels)
    rows, summary = T.run([scene['id']], tmp_path, offline=True, write=False)
    assert latest.read_bytes() == before
    assert not eyes_index.path(tmp_path).exists() and summary['written'] == 0
    assert len(rows) == len(scene['objects'])+1 and rows[-1]['object_id'] == T.SCENE_OBJECT_ID
    assert all(row['version'] == T.VERSION and row['model_signature'] == scene['model_signature'] for row in rows)
    payload = rows[0]['payload']
    assert payload['note'] == T.NOTE and payload['parcels_source'] == 'eyes-parcels' and len(payload['code_sha256']) == 64
    assert summary['scenes'][scene['id']]['parcels_source'] == 'eyes-parcels'
    # Without an address index there is no seed: no lot is called the address lot, and the lot-frame plan records why.
    scene_row = rows[-1]['payload']['scene']
    assert scene_row['address_plan'] is None and scene_row['lot_frame'] == {'error': 'Address not found'}


def test_run_write_replaces_only_this_version_and_keeps_latest_bytes(site, tmp_path):
    scene, parcels, _ = site
    latest = _write_scene(tmp_path, scene)
    before = latest.read_bytes()
    _freeze(tmp_path, scene, parcels)
    # A local address index gives the seed point; the CRM roof file gives the seed ring.
    (tmp_path/'data/australia_geo.db').unlink(missing_ok=True)
    with closing(sqlite3.connect(tmp_path/'data/australia_geo.db')) as db:
        db.execute('CREATE TABLE addresses(id INTEGER PRIMARY KEY, full_address TEXT, lat REAL, lon REAL)')
        lon, lat = T.inverse_projection(scene)(170, 170)
        db.execute('INSERT INTO addresses VALUES(4242, ?, ?, ?)', (scene['title'], lat, lon))
        db.commit()
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, T.TABLE, [{'scene_id': scene['id'], 'object_id': 'interior', 'model_signature': scene['model_signature'],
                                                'version': 'structure-kinds-v3', 'payload': {'sub_kind': 'shed'}}])
    rows, summary = T.run([scene['id']], tmp_path, offline=True, write=True)
    assert latest.read_bytes() == before and summary['written'] == len(rows)
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        ours = eyes_index.rows(db, T.TABLE, scene_id=scene['id'], version=T.VERSION)
        others = eyes_index.rows(db, T.TABLE, scene_id=scene['id'], version='structure-kinds-v3')
    assert len(ours) == len(rows) and len(others) == 1
    by_id = {row['object_id']: row['payload'] for row in ours}
    assert by_id['edge0000']['tag']['flags'] == ['frame_cut', 'lot_incomplete_in_frame', 'lot_unassociated']
    scene_row = by_id[T.SCENE_OBJECT_ID]['scene']
    assert scene_row['address_method'] == 'address-point' and scene_row['address_lot'] == '1'
    assert scene_row['lot_frame']['group_kind'] == 'plan-block' and scene_row['lot_frame']['sentence']
    assert 'Reviews' not in scene_row['lot_frame']['sentence'] and 'No saved review' in scene_row['lot_frame']['sentence']


def test_report_path_must_sit_under_the_eyes_folder(tmp_path):
    with pytest.raises(ValueError, match='data/reviews/eyes'):
        T._report_path(tmp_path, tmp_path/'data/scenes/x.json')
    assert T._report_path(tmp_path, tmp_path/'data/reviews/eyes/lot-tags/x.json') == Path(tmp_path/'data/reviews/eyes/lot-tags/x.json')


# --- Review layer: carried review.json objects ---------------------------------------------------------------

def carried(identity, ring, status='needs_adjustment', **extra):
    """A review-layer copy as scene_focus.carry_window writes it: carried_from names the source scene and object."""
    return obj(identity, 'building', ring, status=status, source='carried',
               carried_from={'scene_id': 'map-source', 'model_signature': 's'*64, 'object_id': 'o'*16, 'status': 'needs_adjustment',
                             'geometry_reviewed': False, 'iou_with_new': 0, 'matched_id': None, 'method': 'georeferenced-affine'},
               **extra)


def test_carried_review_object_on_a_neighbour_lot_is_tagged_in_the_review_layer(site, tmp_path):
    scene, parcels, seed = site
    review = [carried('carried0', px_ring(320, 150, 420, 250))]  # inside Lot 7 LP9, clear of the frame edge
    tags, scene_tag = T.analyse(scene, parcels, seed=seed, review_objects=review)
    tag = tags['carried0']
    assert tag['dominant'] == {'pfi': 'p7', 'lot': '7', 'plan': 'LP9', 'share': 1., 'kind': 'lot'}
    assert tag['kind'] == 'building' and tag['status'] == 'needs_adjustment' and tag['area_m2'] == pytest.approx(100., abs=.01)
    assert tag['flags'] == ['lot_incomplete_in_frame', 'lot_unassociated'] and tag['on_address_lot'] is False
    assert tag['sentences'] == ['On neighbouring Lot 7 LP9, not the address lot; the lot leaves this image.']
    # It reads the lot's model buildings but never joins them.
    assert tag['lot_primary'] == 'edge0000' and 'carried0' not in [m['object_id'] for m in tag['lot_members']]
    assert scene_tag['objects'] == len(scene['objects']) and scene_tag['review_objects'] == 1
    rows, _ = T.scene_rows(scene, tmp_path, parcels=parcels, seed=seed, review_objects=review, with_lot_frame=False)
    layers = {row['object_id']: row['payload'].get('layer') for row in rows}
    assert layers['carried0'] == 'review' and layers['interior'] == 'model' and layers[T.SCENE_OBJECT_ID] is None
    assert all(row['model_signature'] == scene['model_signature'] for row in rows)


def test_superseded_and_live_duplicated_review_objects_are_not_tagged_twice(site, tmp_path):
    scene, parcels, seed = site
    review = [carried('carried0', px_ring(320, 150, 420, 250)),
              carried('carried1', px_ring(320, 260, 420, 290), status='superseded', replacement_ids=['interior']),
              carried('interior', px_ring(105, 105, 245, 245)),          # also a live latest.json object
              obj('plainrev', 'building', px_ring(330, 160, 410, 240)),  # a review object with no carried_from
              carried('carried0', px_ring(330, 160, 410, 240))]          # a duplicate id: the first copy wins
    folder = _write_scene(tmp_path, scene).parent
    (folder/'review.json').write_text(json.dumps({'model_signature': scene['model_signature'], 'revision': 1, 'objects': review}))
    assert [o['id'] for o in T.review_layer_objects(scene, tmp_path)] == ['carried0']
    assert T.review_layer_objects(scene, tmp_path/'elsewhere') == []
    rows, _ = T.scene_rows(scene, tmp_path, parcels=parcels, seed=seed, with_lot_frame=False)
    ids = [row['object_id'] for row in rows]
    assert len(ids) == len(set(ids)) == len(scene['objects'])+2
    layers = {row['object_id']: row['payload'].get('layer') for row in rows}
    assert layers['carried0'] == 'review' and layers['interior'] == 'model'
    assert 'carried1' not in layers and 'plainrev' not in layers
    tags, scene_tag = T.analyse(scene, parcels, seed=seed, review_objects=review)
    assert set(tags) == {o['id'] for o in scene['objects']} | {'carried0'} and scene_tag['review_objects'] == 1
    assert tags['interior']['status'] == 'candidate' and tags['carried0']['area_m2'] == pytest.approx(100., abs=.01)


def test_review_layer_objects_leave_every_model_tag_and_the_scene_count_unchanged(site):
    scene, parcels, seed = site
    before, scene_before = T.analyse(scene, parcels, seed=seed)
    review = [carried('carried0', px_ring(310, 110, 460, 290)),                      # larger than edge0000 on Lot 7
              carried('carried2', px_ring(102, 102, 248, 248)),                      # larger than interior on Lot 1
              carried('carried3', px_ring(150, 350, 250, 450), replaces=['remnant0'])]  # a review copy listing a remnant
    after, scene_after = T.analyse(scene, parcels, seed=seed, review_objects=review)
    assert {k: after[k] for k in before} == before
    assert scene_after['objects'] == scene_before['objects'] == len(scene['objects'])
    assert scene_after['flag_counts'] == scene_before['flag_counts'] and scene_after['review_objects'] == 3
    assert {k: v for k, v in scene_after.items() if k != 'review_objects'} == {k: v for k, v in scene_before.items() if k != 'review_objects'}
    assert scene_before['review_objects'] == 0
    assert 'frame_cut' in after['remnant0']['flags']  # a review-layer replaces list vouches for nothing
    assert all(m['object_id'] in before for tag in after.values() for m in tag['lot_members'])
    assert after['carried2']['lot_primary'] == 'interior' and after['carried2']['primary_ratio'] == pytest.approx(213.16/196, abs=.001)


def test_run_write_with_a_review_json_keeps_latest_and_review_bytes_and_writes_review_rows(site, tmp_path):
    scene, parcels, _ = site
    latest = _write_scene(tmp_path, scene)
    review_path = latest.parent/'review.json'
    review_path.write_text(json.dumps({'model_signature': scene['model_signature'], 'revision': 3,
                                       'objects': [carried('carried0', px_ring(320, 150, 420, 250)),
                                                   carried('carried1', px_ring(320, 260, 420, 290), status='superseded')]}, indent=2))
    before_latest, before_review = latest.read_bytes(), review_path.read_bytes()
    _freeze(tmp_path, scene, parcels)
    rows, summary = T.run([scene['id']], tmp_path, offline=True, write=True)
    assert latest.read_bytes() == before_latest and review_path.read_bytes() == before_review
    assert summary['written'] == len(rows) == len(scene['objects'])+2
    assert summary['scenes'][scene['id']]['objects'] == len(scene['objects']) and summary['scenes'][scene['id']]['review_objects'] == 1
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        ours = {row['object_id']: row for row in eyes_index.rows(db, T.TABLE, scene_id=scene['id'], version=T.VERSION)}
    assert len(ours) == len(rows) and 'carried1' not in ours
    assert ours['carried0']['payload']['layer'] == 'review' and ours['carried0']['payload']['tag']['dominant']['lot'] == '7'
    assert ours['carried0']['model_signature'] == scene['model_signature']
    assert ours['edge0000']['payload']['layer'] == 'model' and 'layer' not in ours[T.SCENE_OBJECT_ID]['payload']
    scene_row = ours[T.SCENE_OBJECT_ID]['payload']['scene']
    assert scene_row['objects'] == len(scene['objects']) and scene_row['review_objects'] == 1
