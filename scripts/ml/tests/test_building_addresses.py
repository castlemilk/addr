import json
from pathlib import Path

import pytest
from scripts.ml import building_addresses as a
from shapely.geometry import Polygon, box, mapping, shape


def feature(pfi='1', name='1 TEST ROAD', address='10', geometry=None, oid=1, **props):
    return {'type': 'Feature', 'geometry': mapping(geometry if geometry is not None else box(0, 0, 1, 1)),
            'properties': {'OBJECTID': oid, 'prop_pfi': pfi, 'prop_status': 'A', 'propv_z_level': 'G',
                           'add_property_pfi': pfi, 'add_pfi': address, 'add_ezi_address': name, **props}}


def snapshot(features):
    return {'complete': True, 'version': a.VERSION, 'bounds': [-1, -1, 5, 5],
            'source': a.SOURCE, 'fetched_at': '2026-09-25', 'dataset_signature': 'fixture', 'features': features}


def test_one_site_can_address_detached_roofs_without_pins_inside_roof():
    index = a.PropertyIndex(snapshot([feature()]))
    for roof in (box(.1, .1, .4, .4), box(.6, .6, .8, .8)):
        result = index.match(roof)
        assert result['status'] == 'matched'
        assert result['full_address'] == '1 TEST ROAD'
        assert result['overlap'] == 1
        assert 'address_id' not in result


def test_multiple_unit_addresses_never_choose_primary_or_nearest():
    index = a.PropertyIndex(snapshot([feature(name='UNIT 1 1 TEST ROAD', add_is_primary='Y'),
                                      feature(name='UNIT 2 1 TEST ROAD', address='11', oid=2)]))
    result = index.match(box(.1, .1, .2, .2))
    assert result['status'] == 'ambiguous'
    assert len(result['candidates']) == 2
    assert 'full_address' not in result


def test_roof_spanning_units_requires_review_but_contained_unit_resolves():
    index = a.PropertyIndex(snapshot([feature(), feature(pfi='2', address='20', name='2 TEST ROAD', geometry=box(1, 0, 2, 1), oid=2)]))
    assert index.match(box(.5, .1, 1.5, .8))['status'] == 'shared'
    assert index.match(box(1.1, .1, 1.8, .8))['full_address'] == '2 TEST ROAD'


def test_overlapping_properties_with_distinct_addresses_are_ambiguous():
    index = a.PropertyIndex(snapshot([feature(), feature(pfi='2', name='UNIT 2', address='20', oid=2)]))
    assert index.match(box(.1, .1, .5, .5))['status'] == 'ambiguous'


def test_duplicate_geometry_or_address_rows_do_not_create_ambiguity():
    index = a.PropertyIndex(snapshot([feature(), feature(oid=2)]))
    assert index.match(box(.1, .1, .5, .5))['status'] == 'matched'


@pytest.mark.parametrize('props', [{'prop_status': 'R'}, {'propv_z_level': 'U'}, {'add_property_pfi': '999'}])
def test_retired_non_ground_or_wrong_property_addresses_do_not_match(props):
    index = a.PropertyIndex(snapshot([feature(**props)]))
    assert index.match(box(.1, .1, .5, .5))['status'] == 'unmatched'


def test_missing_neighbor_and_coverage_are_not_nearest_address_guesses():
    index = a.PropertyIndex(snapshot([feature()]))
    assert index.match(box(1.01, 0, 2, 1))['status'] == 'unmatched'
    assert index.match(box(-2, 0, .5, 1))['status'] == 'unmatched'


def test_property_holes_do_not_claim_roofs_inside_them():
    polygon = Polygon(box(0, 0, 1, 1).exterior.coords, [box(.2, .2, .8, .8).exterior.coords])
    index = a.PropertyIndex(snapshot([feature(geometry=polygon)]))
    assert index.match(box(.3, .3, .4, .4))['status'] == 'unmatched'


def test_saved_local_links_take_precedence_and_do_not_use_vicmap_ids():
    assert a.linked({'address_id': 7}, {'7': '27 CAMBRIDGE ROAD'})['method'] == 'saved-link'
    assert a.linked({'address_id': 1339330}, {'7': '27 CAMBRIDGE ROAD'}) is None


def test_incomplete_refresh_preserves_previous_cache(tmp_path):
    path = tmp_path/a.DATA
    path.parent.mkdir(parents=True)
    path.write_text('previous complete snapshot')
    calls = []
    def fetch(params):
        calls.append(params)
        return {'count': 2} if params.get('returnCountOnly') else {'features': []}
    with pytest.raises(ValueError, match='Incomplete'):
        a.refresh([145, -38, 145.05, -37.95], tmp_path, fetch)
    assert path.read_text() == 'previous complete snapshot'


def test_repeated_pagination_rejected(tmp_path):
    def fetch(params):
        return {'count': 2} if params.get('returnCountOnly') else {'features': [feature()]}
    with pytest.raises(ValueError, match='repeated'):
        a.refresh([145, -38, 145.05, -37.95], tmp_path, fetch)
    assert not (tmp_path/a.DATA).exists()


def test_new_snapshot_invalidates_index(tmp_path):
    path = tmp_path/a.DATA
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(snapshot([feature()])))
    assert a.load_index(tmp_path).match(box(.1, .1, .4, .4))['full_address'] == '1 TEST ROAD'
    path.write_text(json.dumps(snapshot([feature(name='CORRECTED ADDRESS')])) )
    assert a.load_index(tmp_path).match(box(.1, .1, .4, .4))['full_address'] == 'CORRECTED ADDRESS'


def test_reported_cc22_roof_maps_to_hayrick_not_nearest_local_pin():
    fixture = json.loads((Path(__file__).parent/'fixtures/hayrick-property-address.json').read_text())
    data = snapshot([fixture['feature']])
    data['bounds'] = [145.31, -37.79, 145.34, -37.76]
    result = a.PropertyIndex(data).match(shape(fixture['roof']))
    assert result['full_address'] == '118 HAYRICK LANE MOOROOLBARK 3138'
    assert result['property_pfi'] == '1339330'
    assert result['overlap'] == 1


def test_scene_lookup_rechecks_geometry_and_respects_saved_link(tmp_path, monkeypatch):
    from scripts.ml import scene_addresses, scene_confidence
    monkeypatch.setattr(a, 'load_index', lambda root: a.PropertyIndex(snapshot([feature()])))
    monkeypatch.setattr(scene_addresses, 'catalog', lambda ids, root: {'7': 'SAVED ADDRESS'})
    monkeypatch.setattr(scene_confidence, 'geographic', lambda obj, scene: shape(obj['geometry']))
    obj = {'id': 'roof', 'kind': 'building', 'geometry': mapping(box(.1, .1, .5, .5))}
    assert a.resolve_scene({}, [obj], tmp_path)['roof']['status'] == 'matched'
    obj['geometry'] = mapping(box(2, 2, 3, 3))
    assert a.resolve_scene({}, [obj], tmp_path)['roof']['status'] == 'unmatched'
    obj['address_id'] = 7
    assert a.resolve_scene({}, [obj], tmp_path)['roof']['full_address'] == 'SAVED ADDRESS'
