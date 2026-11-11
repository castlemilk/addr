import math

import pytest
from shapely.geometry import Polygon

from scripts.ml import parcel_units as pu
from scripts.ml.scene_addresses import WORLD

LON, LAT = 145.3, -37.79


def scene(width=400, height=400, mpp=0.1, bounds=True):
    # pixel = metres / mpp, image centred on the centroid, y down (north up)
    m2p = [[1 / mpp, 0], [0, 1 / mpp], [width / 2, height / 2]]
    p2m = [[mpp, 0], [0, mpp], [-width / 2 * mpp, -height / 2 * mpp]]
    s = {'id': 's1', 'image_sha256': 'x', 'image_width': width, 'image_height': height, 'centroid': {'lon': LON, 'lat': LAT},
         'meters_to_pixel': m2p, 'pixel_to_meters': p2m}
    if bounds:
        s['region'] = {'bounds': pu.scene_bounds(s)}
    return s


def lonlat(s, x, y):
    """Inverse of the image projection for the test frame (axis-aligned, no rotation)."""
    mx, my = (x - s['meters_to_pixel'][2][0]) / s['meters_to_pixel'][0][0], (y - s['meters_to_pixel'][2][1]) / s['meters_to_pixel'][1][1]
    scale = WORLD * math.cos(math.radians(LAT))
    lon = LON + mx / scale * 360
    y0 = math.asinh(math.tan(math.radians(LAT))) / (2 * math.pi)
    lat = math.degrees(math.atan(math.sinh((my / scale + y0) * 2 * math.pi)))
    return [lon, lat]


def lot(s, x0, y0, x1, y1, number, plan='PS1'):
    ring = [lonlat(s, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))]
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]},
            'properties': {'pfi': f'{plan}-{number}', 'lot': number, 'plan': plan, 'status': 'approved'}}


def parcels(*features):
    return {'type': 'FeatureCollection', 'features': list(features)}


def test_scene_bounds_cover_the_image_and_round_trip():
    s = scene()
    w, so, e, n = pu.scene_bounds(s)
    assert w < LON < e and so < LAT < n
    assert lonlat(s, 0, 0)[0] == pytest.approx(w, abs=1e-9) and lonlat(s, 400, 0)[0] == pytest.approx(e, abs=1e-9)


def test_two_townhouses_on_two_lots_are_several_and_split_on_the_lot_line():
    s = scene()
    p = parcels(lot(s, 100, 100, 200, 300, '1'), lot(s, 200, 100, 300, 300, '2'))
    roof = [[120, 150], [280, 150], [280, 250], [120, 250]]
    verdict = pu.unit_verdict(s, roof, p)
    assert verdict['units'] == 'several' and [x['lot'] for x in verdict['lots']] == ['1', '2']
    pieces = pu.split_by_lots(s, roof, p)
    assert len(pieces) == 2
    areas = sorted(Polygon(x['ring']).area for x in pieces)
    assert areas == pytest.approx([8000, 8000], rel=1e-3)
    assert Polygon(pieces[0]['ring']).union(Polygon(pieces[1]['ring'])).area == pytest.approx(16000, rel=1e-3)


def test_units_never_leave_their_own_lot_even_when_the_roof_runs_onto_common_property():
    s = scene()
    # three units on lots 100 px deep; the roof runs 20 px further onto common property along their whole length
    p = parcels(*[lot(s, 100 + 60 * i, 100, 160 + 60 * i, 200, str(i + 1)) for i in range(3)], lot(s, 100, 200, 280, 260, 'CM1'))
    roof = [[100, 120], [280, 120], [280, 220], [100, 220]]
    pieces = pu.split_by_lots(s, roof, p)
    assert len(pieces) == 3
    for i, piece in enumerate(sorted(pieces, key=lambda q: q['lot'])):
        b = Polygon(piece['ring']).bounds
        assert b[0] >= 100 + 60 * i - 1e-6 and b[2] <= 160 + 60 * i + 1e-6 and b[3] <= 200 + 1e-6, f"lot {piece['lot']} crosses: {b}"
        assert piece['outside_unit_lots_m2'] == pytest.approx(36, rel=0.05)  # 180 x 20 px at 0.1 m/px



def test_a_house_inside_its_own_lot_with_a_sliver_over_the_boundary_is_one():
    s = scene()
    p = parcels(lot(s, 100, 100, 200, 300, '1'), lot(s, 200, 100, 300, 300, '2'))
    roof = [[110, 150], [203, 150], [203, 250], [110, 250]]  # 3 px (0.3 m) over the line: registration, not a unit
    assert pu.unit_verdict(s, roof, p)['units'] == 'one'
    assert pu.split_by_lots(s, roof, p) == []


def test_common_property_and_reserves_never_count_as_a_unit():
    s = scene()
    p = parcels(lot(s, 100, 100, 200, 300, '1'), lot(s, 200, 100, 300, 300, 'CM1'), lot(s, 300, 100, 350, 300, 'RES1'))
    roof = [[120, 150], [340, 150], [340, 250], [120, 250]]
    assert pu.unit_verdict(s, roof, p)['units'] == 'one'


def test_a_terrace_of_six_small_units_is_several_even_though_each_lot_holds_a_small_share():
    s = scene(width=1200)
    p = parcels(*[lot(s, 100 + 80 * i, 100, 180 + 80 * i, 300, str(i + 1)) for i in range(10)])
    roof = [[100, 150], [900, 150], [900, 250], [100, 250]]  # 10 lots, 10 % each, 80 m2 each at 0.1 m/px
    v = pu.unit_verdict(s, roof, p)
    assert v['units'] == 'several' and len(v['lots']) == 10
    assert len(pu.split_by_lots(s, roof, p)) == 10


def test_overlapping_stratum_lots_are_one_building_not_side_by_side_units():
    s = scene()
    # apartments / strata shops: several titles over the same footprint, plus a common-property lot
    p = parcels(*[lot(s, 100, 100, 300, 300, str(i + 1), plan='SP9') for i in range(5)])
    roof = [[120, 150], [280, 150], [280, 250], [120, 250]]
    v = pu.unit_verdict(s, roof, p)
    assert v['units'] == 'one' and v.get('strata') is True
    assert pu.split_by_lots(s, roof, p) == []


def test_no_parcels_is_unknown_not_one():
    s = scene()
    assert pu.unit_verdict(s, [[0, 0], [10, 0], [10, 10]], None)['units'] == 'unknown'
    assert pu.unit_verdict(s, [[0, 0], [10, 0], [10, 10]], parcels())['units'] == 'unknown'


def test_bound_to_lot_clips_an_outline_that_spills_over_its_lot_line():
    s = scene()
    p = parcels(lot(s, 100, 100, 200, 300, '1'), lot(s, 200, 100, 300, 300, '2'))
    ring, info = pu.bound_to_lot(s, [[110, 150], [205, 150], [205, 250], [110, 250]], p)
    assert info['lot'] == '1' and info['clipped_m2'] == pytest.approx(5.0, rel=0.01)  # 5 x 100 px at 0.1 m/px
    assert Polygon(ring).bounds[2] <= 200 + 1e-6
    named, info2 = pu.bound_to_lot(s, [[110, 150], [205, 150], [205, 250], [110, 250]], p, pfi='PS1-2')
    assert info2['lot'] == '2' and Polygon(named).bounds[0] >= 200 - 1e-6
    assert pu.bound_to_lot(s, [[0, 0], [10, 0], [10, 10]], parcels()) == ([[0, 0], [10, 0], [10, 10]], None)


def test_bound_to_lot_keeps_common_property_of_the_same_plan_but_never_a_neighbours_lot():
    s = scene()
    # a garage block mostly on common property CM1 of PS7, spilling onto unit lot 3 of PS7 and onto lot 9 of another plan
    p = parcels(lot(s, 100, 100, 300, 200, 'CM1', plan='PS7'), lot(s, 100, 200, 300, 300, '3', plan='PS7'),
                lot(s, 300, 100, 400, 300, '9', plan='LP1'))
    ring, info = pu.bound_to_lot(s, [[120, 120], [320, 120], [320, 210], [120, 210]], p)
    assert info['lot'] == 'CM1'
    b = Polygon(ring).bounds
    assert b[2] <= 300 + 1e-6, 'never into lot 9 of another plan'
    assert b[3] <= 200 + 1e-6, "nor into unit lot 3: the garage block is CM1's"
    # a house on lot 3 whose eaves run over CM1 (its plan's driveway) keeps the eaves
    ring2, info2 = pu.bound_to_lot(s, [[120, 195], [280, 195], [280, 290], [120, 290]], p)
    assert info2['lot'] == '3' and Polygon(ring2).bounds[1] == pytest.approx(195, abs=1e-6)


def test_shop_tenancies_under_one_commercial_roof_stay_one_building_and_are_not_clipped_away():
    s = scene(width=2400, mpp=0.1)
    # an anchor lot plus thirty 2 m tenancies of the same plan, all under one 80 x 20 m roof
    p = parcels(lot(s, 100, 100, 300, 300, '1', plan='SP9'), *[lot(s, 300 + 20 * i, 100, 320 + 20 * i, 300, str(i + 2), plan='SP9')
                                                              for i in range(30)])
    roof = [[100, 100], [900, 100], [900, 300], [100, 300]]
    assert pu.unit_verdict(s, roof, p)['units'] == 'one'
    ring, info = pu.bound_to_lot(s, roof, p)
    assert info['clipped_m2'] == pytest.approx(0, abs=0.5), 'tenancies of the same plan under the roof are kept'


def test_one_building_on_several_titles_is_bounded_by_all_of_its_lots():
    s = scene()
    p = parcels(lot(s, 100, 100, 200, 300, '1'), lot(s, 200, 100, 300, 300, '2'), lot(s, 300, 100, 400, 300, '3'))
    roof = [[120, 150], [320, 150], [320, 250], [120, 250]]  # a house on lots 1 and 2 with eaves 2 m into lot 3
    ring, info = pu.bound_to_lot(s, roof, p, pfis={'PS1-1', 'PS1-2'})
    b = Polygon(ring).bounds
    assert b[0] == pytest.approx(120) and b[2] == pytest.approx(300), 'kept across lots 1-2, clipped at lot 3'


def test_photo_guard_keeps_roof_the_photo_supports_even_across_the_lot_line():
    s = scene()
    p = parcels(lot(s, 100, 100, 200, 300, '1'), lot(s, 200, 100, 300, 300, '2'))
    roof = [[110, 150], [215, 150], [215, 250], [110, 250]]
    photo = Polygon([[110, 150], [212, 150], [212, 250], [110, 250]])  # the photo shows roof to x 212, not 215
    ring, info = pu.bound_to_lot(s, roof, p, protect=photo)
    assert Polygon(ring).bounds[2] == pytest.approx(212, abs=1e-6), 'clipped only where the photo shows no roof'
