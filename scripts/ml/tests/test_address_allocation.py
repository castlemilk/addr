"""Roof -> address allocation rules on synthetic lots, address points and roofs; index hygiene."""
from contextlib import closing

import pytest
from scripts.ml import address_allocation as aa
from scripts.ml import eyes_index
from shapely.geometry import box, mapping

LON, LAT = 145.0, -37.0
D = .0001  # about 9 m of longitude at this latitude


def cell(x, y, w=1, h=1):
    """A lot-sized box on a coarse grid: (x, y) in D units from the origin, w by h cells."""
    return box(LON+x*D, LAT+y*D, LON+(x+w)*D, LAT+(y+h)*D)


def roof(x, y, w=.5, h=.5):
    return cell(x, y, w, h)


def gnaf_row(address_id, number, street='EXAMPLE ROAD', unit=None, lon=LON, lat=LAT, suburb='MOOROOLBARK'):
    unit_text = f'UNIT {unit}' if unit else None
    full = f'{"Unit " + unit_text + " " if unit_text else ""}{number} {street}, {suburb.title()} VIC 3138'
    return {'address_id': address_id, 'full_address': full, 'unit': unit_text, 'number': str(number), 'street': street,
            'suburb': suburb, 'lon': lon, 'lat': lat}


def candidate(number, street='EXAMPLE ROAD', unit=None, pfi='P', locality='MOOROOLBARK'):
    road = f'{unit}/{number} {street}' if unit else f'{number} {street}'
    out = {'full_address': f'{road} {locality} 3138', 'address_pfi': f'A{pfi}', 'property_pfi': pfi,
           'num_road_address': road, 'locality_name': locality, 'postcode': '3138'}
    if unit:
        out.update(blg_unit_type='UNIT', blg_unit_id_1=int(unit))
    return out


def unnumbered(street='EXAMPLE ROAD', pfi='P0'):
    return {'full_address': f'{street} MOOROOLBARK 3138', 'address_pfi': f'A{pfi}', 'property_pfi': pfi,
            'num_road_address': street, 'locality_name': 'MOOROOLBARK', 'postcode': '3138'}


def prop(pfi, geometry, *candidates):
    return {'source': 'vicmap-property', 'pfi': pfi, 'plan': None, 'lot': None, 'property_pfi': pfi,
            'candidates': list(candidates), 'address_id': None, 'geometry': geometry, 'scene_ids': set()}


def parcel(pfi, geometry, lot='1', plan='PS1'):
    return {'source': 'vicmap-parcel', 'pfi': pfi, 'plan': plan, 'lot': lot, 'property_pfi': None,
            'candidates': [], 'address_id': None, 'geometry': geometry, 'scene_ids': {'scene'}}


def feature(object_id, geometry, before, area=120., scene_id='scene', **props):
    return {'type': 'Feature', 'geometry': mapping(geometry),
            'properties': {'scene_id': scene_id, 'object_id': object_id, 'key': f'{scene_id}/{object_id}', 'area_m2': area,
                           'band': 'medium', 'review_url': aa.review_url(scene_id, object_id), 'clipped': False,
                           'hidden_overlaps': [], 'property_address': before, **props}}


def before(status, *candidates, method='vicmap-property', full_address=None, address_kind='property', **more):
    return {'status': status, 'method': method, 'candidates': list(candidates), 'full_address': full_address,
            'address_kind': address_kind, 'reason': 'test', **more}


def allocate(features, gnaf_rows, lots, hints=None, splits=None, use_saved_links=True):
    allocator = aa.Allocator(aa.Gnaf(gnaf_rows), aa.Lots(lots), hints or {}, scenes=None, splits=splits, use_saved_links=use_saved_links)
    return {r['object_id']: r for r in allocator.allocate(features)}


# --- keys -------------------------------------------------------------------------------------------------------------

def test_vicmap_and_gnaf_keys_agree_on_units_and_localities():
    assert aa.vicmap_key(candidate(29, unit='4', street='CAMBRIDGE ROAD')) == ('4', '29', 'CAMBRIDGE ROAD', 'MOOROOLBARK')
    assert aa.address_key('UNIT 4', '29', 'Cambridge  Road', 'Mooroolbark') == ('4', '29', 'CAMBRIDGE ROAD', 'MOOROOLBARK')
    assert aa.vicmap_key(candidate('5A', street='GREENVILLE STREET')) == (None, '5A', 'GREENVILLE STREET', 'MOOROOLBARK')
    assert aa.vicmap_key(unnumbered()) is None
    gnaf = aa.Gnaf([gnaf_row(1, 7, suburb='CROYDON'), gnaf_row(2, 7)])
    assert [r['address_id'] for r in gnaf.by_name(candidate(7))] == [2]  # the locality is part of the key


# --- rules ------------------------------------------------------------------------------------------------------------

def test_rule1_saved_link_is_strong_and_beats_everything():
    rows = allocate([feature('r', roof(0, 0), before('linked', method='saved-link', address_id=9, full_address='9 EXAMPLE ROAD'))],
                    [gnaf_row(9, 9, lon=LON+5*D, lat=LAT+5*D)], [prop('P1', cell(0, 0), candidate(3, pfi='P1'))])
    row = rows['r']
    assert (row['status'], row['method'], row['confidence'], row['address_id']) == ('matched', 'saved-link', 'strong', 9)
    assert row['evidence']['rule'] == 1


def test_rule2_vicmap_match_with_one_gnaf_record_is_strong_and_without_one_keeps_no_id():
    lots = [prop('P1', cell(0, 0), candidate(3, pfi='P1')), prop('P2', cell(2, 0), candidate(4, pfi='P2'))]
    features = [feature('a', roof(0, 0), before('matched', candidate(3, pfi='P1'), full_address='3 EXAMPLE ROAD MOOROOLBARK 3138', property_pfi='P1')),
                feature('b', roof(2, 0), before('matched', candidate(4, pfi='P2'), full_address='4 EXAMPLE ROAD MOOROOLBARK 3138', property_pfi='P2'))]
    rows = allocate(features, [gnaf_row(30, 3, lon=LON+.5*D, lat=LAT+.5*D)], lots)
    assert (rows['a']['status'], rows['a']['confidence'], rows['a']['address_id'], rows['a']['full_address']) == (
        'matched', 'strong', 30, '3 EXAMPLE ROAD, Mooroolbark VIC 3138')
    assert (rows['b']['status'], rows['b']['confidence'], rows['b']['address_id']) == ('matched', 'moderate', None)
    assert rows['b']['full_address'] == '4 EXAMPLE ROAD MOOROOLBARK 3138' and rows['b']['evidence']['gnaf_lookup'] == 'none'


def test_rule3_numbered_property_containment_resolves_stacked_unnumbered_site():
    # The roof lies in the numbered property P1 and in the unnumbered parent site P0 covering the whole block.
    lots = [prop('P0', cell(-2, -2, 6, 6), unnumbered(pfi='P0')), prop('P1', cell(0, 0), candidate(5, pfi='P1'))]
    ambiguous = before('ambiguous', unnumbered(pfi='P0'), candidate(5, pfi='P1'))
    point_inside = [gnaf_row(50, 5, lon=LON+.8*D, lat=LAT+.8*D)]
    rows = allocate([feature('r', roof(0, 0), ambiguous)], point_inside, lots)
    row = rows['r']
    assert (row['status'], row['method'], row['confidence'], row['address_id']) == ('matched', 'property-containment', 'moderate', 50)
    assert row['evidence']['containment']['address_source'] == 'gnaf-point' and row['evidence']['parcel_evidence'] == 'no parcel evidence'
    # With the point sitting outside the polygon the property's unique numbered name still vouches.
    rows = allocate([feature('r', roof(0, 0), ambiguous)], [gnaf_row(50, 5, lon=LON+1.5*D, lat=LAT+.5*D)], lots)
    assert rows['r']['address_id'] == 50 and rows['r']['evidence']['containment']['address_source'] == 'vicmap-name'


def test_rule3_parcel_lot_with_one_point_resolves_and_records_the_lot():
    lots = [parcel('L1', cell(0, 0), lot='12', plan='PS777')]
    rows = allocate([feature('r', roof(0, 0), before('ambiguous', unnumbered()))], [gnaf_row(12, 12, lon=LON+.9*D, lat=LAT+.9*D)], lots)
    row = rows['r']
    assert (row['method'], row['address_id'], row['lot']) == ('parcel-containment', 12, {'plan': 'PS777', 'lot': '12'})
    assert row['evidence']['parcel_evidence'] == 'vicmap-parcel'


def test_rule3_conflicting_polygons_and_common_property_lots_stay_ambiguous():
    # Two numbered properties each hold the whole roof and vouch for different addresses.
    lots = [prop('P1', cell(0, 0), candidate(5, pfi='P1')), prop('P2', cell(0, 0), candidate(6, pfi='P2'))]
    rows = allocate([feature('r', roof(0, 0), before('ambiguous', candidate(5, pfi='P1'), candidate(6, pfi='P2')))],
                    [gnaf_row(50, 5, lon=LON+5*D, lat=LAT+5*D), gnaf_row(60, 6, lon=LON+6*D, lat=LAT+5*D)], lots)
    assert rows['r']['status'] == 'ambiguous' and {a['address_id'] for a in rows['r']['alternatives']} == {50, 60}
    assert rows['r']['evidence']['containment'] == {'conflict': [50, 60]}
    # A common-property lot holding the roof with one point inside decides nothing.
    common = [parcel('CM', cell(0, 0), lot='CM1', plan='PS1')]
    rows = allocate([feature('r', roof(0, 0), before('ambiguous', unnumbered()))], [gnaf_row(1, 1, lon=LON+.1*D, lat=LAT+.1*D)], common)
    assert rows['r']['status'] == 'ambiguous' and rows['r']['address_id'] is None


def test_shared_roofs_stay_shared_without_a_saved_link():
    lots = [prop('P1', cell(0, 0), candidate(1, unit='1', pfi='P1')), prop('P2', cell(1, 0), candidate(1, unit='2', pfi='P2'))]
    shared = before('shared', candidate(1, unit='1', pfi='P1'), candidate(1, unit='2', pfi='P2'), method='property-allocation', address_kind='unit_address')
    rows = allocate([feature('r', roof(.6, 0, .8, .5), shared)], [gnaf_row(11, 1, unit='1', lon=LON+.5*D, lat=LAT+.5*D), gnaf_row(12, 1, unit='2', lon=LON+1.5*D, lat=LAT+.5*D)], lots)
    row = rows['r']
    assert (row['status'], row['method'], row['address_id']) == ('shared', 'property-allocation', None)
    assert {a['address_id'] for a in row['alternatives']} == {11, 12}


def test_rule4_secondary_structure_inherits_the_primary_one_step_lower():
    lots = [prop('P0', cell(-2, -2, 6, 6), unnumbered(pfi='P0')), prop('P1', cell(0, 0, 2, 2), candidate(5, pfi='P1'))]
    house = feature('house', roof(0, 0, 1, 1), before('linked', method='saved-link', address_id=50, full_address='5 EXAMPLE ROAD', property_pfi='P1'), area=180.)
    # The shed is ambiguous (parent site + property); two G-NAF records carry the property's name, so containment cannot vouch.
    shed = feature('shed', roof(1.2, 1.2, .4, .4), before('ambiguous', unnumbered(pfi='P0'), candidate(5, pfi='P1')), area=20.)
    points = [gnaf_row(50, 5, lon=LON+.5*D, lat=LAT+.5*D), gnaf_row(51, 5, lon=LON+1.5*D, lat=LAT+.5*D)]
    hints = {('scene', 'shed'): {'class_hint': 'outbuilding', 'flags': ['secondary-structure'], 'sub_kind': 'shed', 'sub_confidence': 'strong'}}
    rows = allocate([house, shed], points, lots, hints)
    assert rows['house']['confidence'] == 'strong' and rows['house']['address_id'] == 50
    row = rows['shed']
    assert (row['status'], row['method'], row['confidence'], row['address_id']) == ('matched', 'secondary-inherits', 'moderate', 50)
    assert row['evidence']['inherited_from']['object_id'] == 'house' and row['evidence']['rule'] == 4
    # Without the secondary hint the same shed stays ambiguous: inheritance is for secondary structures only.
    assert allocate([house, shed], points, lots)['shed']['status'] == 'ambiguous'


def test_rule4_does_not_pick_between_primaries_on_two_properties():
    lots = [prop('P1', cell(0, 0), candidate(5, pfi='P1')), prop('P2', cell(1, 0), candidate(6, pfi='P2'))]
    houses = [feature('h1', roof(0, .5), before('matched', candidate(5, pfi='P1'), full_address='5 EXAMPLE ROAD MOOROOLBARK 3138', property_pfi='P1'), area=150.),
              feature('h2', roof(1.5, .5), before('matched', candidate(6, pfi='P2'), full_address='6 EXAMPLE ROAD MOOROOLBARK 3138', property_pfi='P2'), area=140.)]
    shed = feature('shed', roof(.8, 0, .4, .4), before('ambiguous', candidate(5, pfi='P1'), candidate(6, pfi='P2')), area=15.)
    hints = {('scene', 'shed'): {'class_hint': 'outbuilding', 'flags': ['secondary-structure'], 'sub_kind': 'shed', 'sub_confidence': 'moderate'}}
    rows = allocate(houses+[shed], [gnaf_row(50, 5, lon=LON+5*D, lat=LAT+5*D), gnaf_row(60, 6, lon=LON+6*D, lat=LAT+5*D)], lots, hints)
    assert rows['shed']['status'] == 'ambiguous' and rows['shed']['evidence']['inherit_conflict'] == [50, 60]


def test_rule5_unit_lot_centroid_is_a_moderate_suggestion_only_for_unit_sites():
    lots = [prop('P0', cell(-1, -1, 4, 3), unnumbered(pfi='P0')),
            prop('U1', cell(0, 0), candidate(9, unit='1', pfi='U1')), prop('U2', cell(1, 0), candidate(9, unit='2', pfi='U2'))]
    points = [gnaf_row(91, 9, unit='1', lon=LON+.2*D, lat=LAT+.2*D), gnaf_row(92, 9, unit='2', lon=LON+1.8*D, lat=LAT+.2*D)]
    ambiguous = before('ambiguous', unnumbered(pfi='P0'), candidate(9, unit='1', pfi='U1'), candidate(9, unit='2', pfi='U2'), address_kind='unit_address')
    rows = allocate([feature('r', roof(.4, .4, .8, .3), ambiguous)], points, lots)  # 75% in U1, centroid in U1
    row = rows['r']
    assert (row['status'], row['method'], row['confidence'], row['address_id']) == ('suggested', 'unit-lot-centroid', 'moderate', 91)
    # The same geometry between two ordinary numbered properties is not a unit site: no centroid suggestion.
    plain_lots = [prop('P0', cell(-1, -1, 4, 3), unnumbered(pfi='P0')), prop('N1', cell(0, 0), candidate(9, pfi='N1')), prop('N2', cell(1, 0), candidate(11, pfi='N2'))]
    plain = before('ambiguous', unnumbered(pfi='P0'), candidate(9, pfi='N1'), candidate(11, pfi='N2'))
    assert allocate([feature('r', roof(.4, .4, .8, .3), plain)], points, plain_lots)['r']['status'] == 'ambiguous'


def test_rule6_address_point_inside_roof_is_weak_and_blocked_by_site_lots():
    point = [gnaf_row(7, 7, lon=LON+.25*D, lat=LAT+.25*D)]
    rows = allocate([feature('r', roof(0, 0), before('unmatched', method='unresolved'))], point, [])
    row = rows['r']
    assert (row['status'], row['method'], row['confidence'], row['address_id']) == ('suggested', 'address-point', 'weak', 7)
    # The same point inside a multi-address site polygon is parcel evidence against a single point.
    site = [prop('S', cell(-1, -1, 4, 4), unnumbered(pfi='S'))]
    many = point+[gnaf_row(8, 8, lon=LON+2*D, lat=LAT+2*D)]
    rows = allocate([feature('r', roof(0, 0), before('ambiguous', unnumbered(pfi='S')))], many, site)
    assert rows['r']['status'] == 'ambiguous' and rows['r']['evidence']['site_lot'] is True


def test_unmatched_roof_with_nothing_around_stays_unmatched_and_statuses_are_valid():
    rows = allocate([feature('r', roof(0, 0), before('unmatched', method='unresolved'))], [], [])
    assert rows['r']['status'] == 'unmatched' and rows['r']['confidence'] == 'weak' and rows['r']['address_id'] is None
    assert rows['r']['status'] in aa.STATUSES and rows['r']['confidence'] in aa.CONFIDENCES


def test_use_saved_links_false_gives_the_rule_based_answer_for_agreement_checks():
    lots = [prop('P1', cell(0, 0), candidate(3, pfi='P1'))]
    linked = before('linked', method='saved-link', address_id=99, full_address='99 OTHER ROAD')
    gnaf = [gnaf_row(99, 99, street='OTHER ROAD', lon=LON+5*D, lat=LAT+5*D), gnaf_row(30, 3, lon=LON+.5*D, lat=LAT+.5*D)]
    with_link = allocate([feature('r', roof(0, 0), linked)], gnaf, lots)['r']
    rule_based = allocate([feature('r', roof(0, 0), linked)], gnaf, lots, use_saved_links=False)['r']
    assert with_link['address_id'] == 99 and rule_based['address_id'] == 30
    check = aa.agreement({('scene', 'r'): rule_based}, {('scene', 'r'): 99}, 'saved-link')
    assert (check['denominator'], check['disagree'], check['agreement_rate']) == (1, 1, 0.)


# --- index ------------------------------------------------------------------------------------------------------------

def test_index_writes_replace_only_this_version(tmp_path):
    rows = allocate([feature('r', roof(0, 0), before('unmatched', method='unresolved'))], [gnaf_row(7, 7, lon=LON+.25*D, lat=LAT+.25*D)], [])
    with closing(eyes_index.connection(tmp_path)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, aa.TABLE, [{'scene_id': 'scene', 'object_id': 'r', 'version': 'someone-else-v9', 'payload': {'keep': True}}])
        assert aa.replace_version_rows(db, aa.TABLE, aa.VERSION, aa.index_rows(list(rows.values()))) == 1
        assert aa.replace_version_rows(db, aa.TABLE, aa.VERSION, aa.index_rows(list(rows.values()))) == 1
        stored = eyes_index.rows(db, aa.TABLE, scene_id='scene')
    assert {r['version'] for r in stored} == {'someone-else-v9', aa.VERSION}
    assert len(stored) == 2
    assert aa.load_rows(tmp_path)[0]['address_id'] == 7
    assert set(eyes_index.TABLES) >= {'address_allocation', 'address_enrichment'}


def test_step_down_and_counts_helpers():
    assert [aa.step_down(c) for c in aa.CONFIDENCES] == ['moderate', 'weak', 'weak']
    assert aa._counts(['b', None, 'a', 'a']) == {'a': 2, 'b': 1, 'null': 1}


@pytest.mark.parametrize('lot,common', [('CM1', True), ('CP3', True), ('12', False), (None, False)])
def test_common_property_lot_detection(lot, common):
    assert aa.is_common_lot({'lot': lot}) is common
