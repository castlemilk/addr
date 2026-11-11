"""Per-address enrichment documents: exact schema, primary/structure choice, lot-based counts, duplicates, index hygiene."""
import copy
from contextlib import closing

import pytest
from scripts.ml import address_allocation as aa
from scripts.ml import address_enrichment as ae
from scripts.ml import eyes_index
from scripts.ml.scene_confidence import geographic

SCENE_BASE = {'centroid': {'lon': 145.0, 'lat': -37.0}, 'pixel_to_meters': [[1, 0], [0, -1], [0, 0]],
              'image_width': 400, 'image_height': 400, 'image_sha256': 'f'*64, 'model_signature': 'm'}


def ring(x, y, w, h):
    """Rectangle in local metres (x east, y north) as pixel coordinates for the identity-ish frame above."""
    return [[x, -y], [x+w, -y], [x+w, -(y+h)], [x, -(y+h)]]


def scene(scene_id, objects):
    return {**SCENE_BASE, 'id': scene_id, 'objects': objects}


def obj(object_id, kind, x, y, w, h, **more):
    return {'id': object_id, 'kind': kind, 'status': 'candidate', 'ring': ring(x, y, w, h), **more}


class FakeScenes:
    def __init__(self, *scenes):
        self.scenes = {s['id']: s for s in scenes}

    def get(self, scene_id):
        return self.scenes.get(scene_id)

    def obj(self, scene_id, object_id):
        return next((o for o in self.scenes.get(scene_id, {}).get('objects', []) if o['id'] == object_id), None)


def wgs84(x, y, w, h):
    return geographic({'ring': ring(x, y, w, h)}, SCENE_BASE)


def lots_with_property(pfi='P1', x=0, y=0, w=40, h=40):
    return aa.Lots([{'source': 'vicmap-property', 'pfi': pfi, 'plan': None, 'lot': None, 'property_pfi': pfi, 'candidates': [],
                     'address_id': None, 'geometry': wgs84(x, y, w, h), 'scene_ids': set()}])


def allocation_row(scene_id, object_id, area, *, address_id=42, method='vicmap-property', confidence='strong', hints=None,
                   property_pfi='P1', status='matched', alternatives=()):
    return {'scene_id': scene_id, 'object_id': object_id, 'version': aa.VERSION, 'address_id': address_id,
            'full_address': '42 EXAMPLE ROAD, Mooroolbark VIC 3138', 'property_pfi': property_pfi, 'lot': None, 'status': status,
            'method': method, 'confidence': confidence, 'reason': 'test', 'alternatives': list(alternatives), 'area_m2': area,
            'band': 'medium', 'review_url': aa.review_url(scene_id, object_id),
            'evidence': {'hints': hints, 'lots': [], 'secondary': False}}


GNAF = {'address_id': 42, 'full_address': '42 EXAMPLE ROAD, Mooroolbark VIC 3138', 'unit': None, 'number': '42',
        'street': 'EXAMPLE ROAD', 'suburb': 'MOOROOLBARK', 'lon': 145.0001, 'lat': -37.0001}


def build(rows, scenes, lots=None, gnaf_row=GNAF, address_id=42, stats=None):
    gnaf = aa.Gnaf([gnaf_row] if gnaf_row else [])
    lots = lots if lots is not None else lots_with_property()
    return ae.build_document(address_id, gnaf_row, rows, lots, gnaf, scenes, ae.SceneObjects(scenes), '2026-09-26T00:00:00+00:00', stats)


def test_document_matches_the_published_schema_exactly():
    house = obj('house', 'building', 5, 5, 12, 10, roof_type='gabled')
    scenes = FakeScenes(scene('s1', [house, obj('shed', 'building', 25, 25, 4, 3),
                                     obj('sun', 'solar_panel', 8, 8, 2, 1), obj('tree-in', 'tree', 30, 5, 4, 4),
                                     obj('tree-out', 'tree', 80, 80, 4, 4), obj('pool', 'pool', 20, 5, 5, 3)]))
    rows = [allocation_row('s1', 'house', 120.),
            allocation_row('s1', 'shed', 12., method='secondary-inherits', confidence='moderate',
                           hints={'class_hint': 'outbuilding', 'flags': ['secondary-structure'], 'sub_kind': 'shed', 'sub_confidence': 'strong'})]
    document = build(rows, scenes)
    assert set(document) == set(ae.SCHEMA) and document['version'] == 'address-enrichment-v1'
    assert document['note'] == 'agent hints and model estimates, not labels or accuracy'
    assert document['address_id'] == 42 and document['full_address'] == GNAF['full_address'] and document['property_pfi'] == 'P1'
    assert document['lot'] is None
    assert document['allocation'] == {'status': 'matched', 'method': 'vicmap-property', 'confidence': 'strong', 'reason': 'test', 'alternatives': []}
    assert document['primary_roof'] == {'scene_id': 's1', 'object_id': 'house', 'area_m2': 120., 'roof_type': 'gabled', 'band': 'medium',
                                        'review_url': '/scenes?scene=s1&object=house&kind=building&status=all&layer=model'}
    assert document['structures'] == [{'scene_id': 's1', 'object_id': 'shed', 'sub_kind': 'shed', 'confidence': 'strong', 'area_m2': 12.,
                                       'review_url': '/scenes?scene=s1&object=shed&kind=building&status=all&layer=model'}]
    assert document['solar'] == {'count': 1, 'area_m2': 2.0}
    assert document['trees'] == {'count_on_lot': 1}
    assert document['pool'] == {'present': True}
    assert document['roofs_total'] == 2 and document['computed_at'] == '2026-09-26T00:00:00+00:00'
    assert ae.validate(document) is document


def test_validate_rejects_extra_keys_wrong_types_and_bad_allocation():
    document = build([allocation_row('s1', 'house', 100.)], FakeScenes(scene('s1', [obj('house', 'building', 5, 5, 10, 10)])))
    with pytest.raises(ValueError):
        ae.validate({**document, 'extra': 1})
    with pytest.raises(ValueError):
        ae.validate({k: v for k, v in document.items() if k != 'pool'})
    with pytest.raises(ValueError):
        ae.validate({**document, 'roofs_total': '1'})
    with pytest.raises(ValueError):
        ae.validate({**document, 'allocation': {**document['allocation'], 'confidence': 'certain'}})
    with pytest.raises(ValueError):
        ae.validate({**document, 'lot': {'plan': 'PS1'}})
    with pytest.raises(ValueError):
        ae.validate({**document, 'structures': [{'scene_id': 's1'}]})
    bad = copy.deepcopy(document)
    bad['pool']['present'] = 'yes'
    with pytest.raises(ValueError):
        ae.validate(bad)


def test_primary_skips_outbuilding_hints_even_when_larger():
    scenes = FakeScenes(scene('s1', [obj('barn', 'building', 0, 0, 20, 20), obj('house', 'building', 22, 0, 10, 10, roof_type='hipped')]))
    rows = [allocation_row('s1', 'barn', 400., hints={'class_hint': 'outbuilding', 'flags': ['secondary-structure'], 'sub_kind': 'garage', 'sub_confidence': 'moderate'}),
            allocation_row('s1', 'house', 100., hints={'class_hint': 'house', 'flags': [], 'sub_kind': None, 'sub_confidence': None})]
    document = build(rows, scenes)
    assert document['primary_roof']['object_id'] == 'house' and document['primary_roof']['roof_type'] == 'hipped'
    assert [s['object_id'] for s in document['structures']] == ['barn'] and document['structures'][0]['sub_kind'] == 'garage'


def test_duplicate_roofs_across_captures_count_once_and_are_reported():
    same = obj('roof', 'building', 5, 5, 12, 10)
    scenes = FakeScenes(scene('s1', [same]), scene('s2', [{**same, 'id': 'again'}]), scene('s3', [obj('far', 'building', 30, 30, 5, 5)]))
    rows = [allocation_row('s1', 'roof', 120.), allocation_row('s2', 'again', 119.), allocation_row('s3', 'far', 25.)]
    stats = {'duplicate_roofs_dropped': 0}
    document = build(rows, scenes, stats=stats)
    assert document['roofs_total'] == 2 and stats['duplicate_roofs_dropped'] == 1
    assert document['primary_roof']['object_id'] == 'roof' and [s['object_id'] for s in document['structures']] == ['far']
    kept, dropped = ae.distinct_roofs(rows, scenes)
    assert [r['object_id'] for r in kept] == ['roof', 'far'] and dropped[0][0]['object_id'] == 'again'


def test_solar_and_trees_are_deduplicated_across_captures_and_null_without_a_lot():
    panel = obj('p1', 'solar_panel', 8, 8, 2, 1)
    scenes = FakeScenes(scene('s1', [obj('house', 'building', 5, 5, 12, 10), panel, obj('t1', 'tree', 30, 5, 4, 4)]),
                        scene('s2', [obj('house2', 'building', 5, 5, 12, 10), {**panel, 'id': 'p1-again'}, obj('t1-again', 'tree', 30.5, 5.5, 4, 4)]))
    rows = [allocation_row('s1', 'house', 120.), allocation_row('s2', 'house2', 120.)]
    document = build(rows, scenes)
    assert document['solar'] == {'count': 1, 'area_m2': 2.0} and document['trees'] == {'count_on_lot': 1}
    without_lot = build([allocation_row('s1', 'house', 120., property_pfi=None)], scenes, lots=aa.Lots([]))
    assert without_lot['trees'] == {'count_on_lot': None} and without_lot['pool'] == {'present': None}
    assert without_lot['solar'] == {'count': 0, 'area_m2': 0} and without_lot['property_pfi'] is None


def test_address_without_roofs_gets_an_unmatched_document_and_crm_lot():
    lots = aa.Lots([{'source': 'crm-boundary', 'pfi': 'crm-22', 'plan': 'LP12345', 'lot': '27', 'property_pfi': None, 'candidates': [],
                     'address_id': 42, 'geometry': wgs84(0, 0, 40, 40), 'scene_ids': set()}])
    document = build([], FakeScenes(), lots=lots)
    assert document['primary_roof'] is None and document['structures'] == [] and document['roofs_total'] == 0
    assert document['allocation']['status'] == 'unmatched' and document['allocation']['method'] == 'none'
    assert document['lot'] == {'plan': 'LP12345', 'lot': '27'} and document['trees'] == {'count_on_lot': None}
    ae.validate(document)


def test_alternatives_and_lot_from_the_primary_allocation_are_carried():
    scenes = FakeScenes(scene('s1', [obj('house', 'building', 5, 5, 12, 10)]))
    row = allocation_row('s1', 'house', 120., method='parcel-containment', confidence='moderate',
                         alternatives=[{'address_id': None, 'full_address': 'EXAMPLE ROAD MOOROOLBARK 3138'}, {'address_id': 43, 'full_address': '43 EXAMPLE ROAD'}])
    row['lot'] = {'plan': 'PS1', 'lot': '3'}
    document = build([row], scenes)
    assert document['lot'] == {'plan': 'PS1', 'lot': '3'}
    assert document['allocation']['alternatives'] == [{'address_id': None, 'full_address': 'EXAMPLE ROAD MOOROOLBARK 3138'}, {'address_id': 43, 'full_address': '43 EXAMPLE ROAD'}]


def test_index_and_files_replace_only_this_version(tmp_path):
    scenes = FakeScenes(scene('s1', [obj('house', 'building', 5, 5, 12, 10)]))
    document = build([allocation_row('s1', 'house', 120.)], scenes)
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, ae.TABLE, [{'address_id': 42, 'full_address': 'x', 'version': 'other-v0', 'payload': {'keep': True}}])
    assert ae.write_index(tmp_path, [document]) == 1
    assert ae.write_index(tmp_path, [document]) == 1
    with closing(eyes_index.connection(tmp_path, readonly=True)) as db:
        stored = eyes_index.rows(db, ae.TABLE, address_id=42)
    assert {r['version'] for r in stored} == {'other-v0', ae.VERSION} and len(stored) == 2
    report = tmp_path/'report'
    (report/'addresses').mkdir(parents=True)
    (report/'addresses'/'999.json').write_text('{}')  # stale output from an earlier run
    index = ae.write_documents(tmp_path, [document], report)
    assert sorted(p.name for p in (report/'addresses').glob('*.json')) == ['42.json']
    assert index['addresses']['42']['roofs_total'] == 1 and index['counts']['addresses'] == 1
    assert (report/'index.json').exists()
