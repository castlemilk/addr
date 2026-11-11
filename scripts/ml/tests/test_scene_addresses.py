import copy
import sqlite3

import pytest
from scripts.ml.scene_addresses import catalog, image_projection, match_addresses, nearby

SCENE = {'centroid': {'lon': 145., 'lat': -37.}, 'meters_to_pixel': [[1, 0], [0, -1], [0, 0]], 'region': {}}
BOUNDS = [144.99, -37.01, 145.01, -36.99]


def parcel(w=145., s=-37., e=145.001, n=-36.999, **props):
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
            'properties': {'pfi': 'one', 'lot': '1', **props}}


def roof():
    project = image_projection(SCENE)
    return {'id': 'roof', 'kind': 'building', 'status': 'candidate',
            'ring': [project(x, y) for x, y in [(145.0002, -36.9998), (145.0008, -36.9998), (145.0008, -36.9992), (145.0002, -36.9992)]]}


def address(id=27, lon=145.0001, lat=-36.9999):
    return {'address_id': id, 'full_address': f'{id} EXAMPLE ROAD', 'lon': lon, 'lat': lat}


def match(obj=None, addresses=None, features=None, bounds=BOUNDS):
    return match_addresses(SCENE, [obj or roof()], addresses if addresses is not None else [address()],
                           {'features': features if features is not None else [parcel()]}, {'27': '27 EXAMPLE ROAD'}, bounds)


def test_parcel_assigns_pin_outside_roof_without_using_nearest_neighbour():
    # A closer pin on the adjoining parcel must not steal this roof.
    result = match(addresses=[address(), address(29, 145.00101, -36.9995)])[0]
    assert result['status'] == 'suggested' and result['address_id'] == 27
    assert result['method'] == 'parcel'


def test_saved_link_wins_and_inputs_are_unchanged():
    obj = {**roof(), 'address_id': 27}
    before = copy.deepcopy(obj)
    result = match(obj, addresses=[address(29)])[0]
    assert result['status'] == 'linked' and result['address_id'] == 27
    assert obj == before


@pytest.mark.parametrize('features,addresses,bounds', [
    ([parcel()], [address(), address(28)], BOUNDS),
    ([parcel(), parcel(pfi='overlap')], [address()], BOUNDS),
    ([parcel(lot='CM1')], [address()], BOUNDS),
    ([parcel()], [address(lon=145.)], BOUNDS),
    ([parcel()], [address()], [145.0001, -36.9999, 145.0009, -36.9991]),
])
def test_shared_overlapping_common_boundary_and_partial_parcels_stay_ambiguous(features, addresses, bounds):
    result = match(features=features, addresses=addresses, bounds=bounds)[0]
    assert result['status'] == 'ambiguous'
    assert 'address_id' not in result


def test_no_parcels_uses_unique_contained_pin_not_nearest_pin():
    assert match(features=[])[0]['status'] == 'unmatched'
    pins = [address(lon=145.0005, lat=-36.9995)]
    result = match(features=[], addresses=pins)[0]
    assert result['method'] == 'address-point' and result['address_id'] == 27
    assert match(features=[], addresses=pins+[address(29, 145.0006, -36.9994)])[0]['status'] == 'ambiguous'


def test_parcel_holes_do_not_link_roof():
    feature = parcel()
    feature['geometry']['coordinates'].append(parcel(145.0001, -36.9999, 145.0009, -36.9991)['geometry']['coordinates'][0])
    result = match(features=[feature], addresses=[address(lon=145.00005, lat=-36.99995)])[0]
    assert result['status'] == 'unmatched'


@pytest.mark.parametrize('status', ['superseded', 'rejected'])
def test_inactive_buildings_do_not_establish_addresses(status):
    assert match({**roof(), 'status': status, 'address_id': 27}) == []


def test_read_only_catalog_and_spatial_lookup(tmp_path):
    (tmp_path/'data').mkdir()
    with sqlite3.connect(tmp_path/'data/australia_geo.db') as db:
        db.execute('CREATE TABLE addresses (id INTEGER, full_address TEXT, lon REAL, lat REAL)')
        db.execute('CREATE VIRTUAL TABLE addresses_rtree USING rtree(id,minX,maxX,minY,maxY)')
        db.execute("INSERT INTO addresses VALUES (27,'27 EXAMPLE ROAD',145.0001,-36.9999)")
        db.execute('INSERT INTO addresses_rtree VALUES (27,145.0001,145.0001,-36.9999,-36.9999)')
    assert catalog([27, 999, None, True], root=tmp_path) == {'27': '27 EXAMPLE ROAD'}
    assert nearby(BOUNDS, root=tmp_path) == [address()]
    assert nearby([146, -38, 147, -37], root=tmp_path) == []
