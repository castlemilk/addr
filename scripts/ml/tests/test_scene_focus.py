"""frame-cut-window-v1: subject-centred windows for frame-cut targets (plan, identity, dedupe, offline capture).

Synthetic registered frames about one point; parcels and address points are injected; the tile fetcher renders one
georeferenced ground so two captures of the same place agree by construction. Nothing here reads or writes the
repository's data/scenes/.
"""
import hashlib
import json
import math
import re
import shutil

import numpy as np
import pytest
from PIL import Image
from scripts.ml import scene_focus as F
from scripts.ml import scene_lot as L
from scripts.ml import scene_lot_tags as T
from scripts.ml import scene_recapture as R
from scripts.ml.scene_addresses import WORLD, image_projection
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_region import capture_region, region_frame

LON, LAT = 145.3125, -37.7905
METRES = L._local_frame(LON, LAT)
UNPROJECT = T.inverse_projection(METRES)          # metres about (LON, LAT) -> lon, lat
PROJECT = image_projection(METRES)                 # lon, lat -> metres about (LON, LAT)
NATIVE_512 = 512*L.native_m_per_px(LAT, 20)        # the 512 px native z20 span at this latitude (about 60.4 m)


def registered(scene_id, cx, cy, span, *, frame_px=512, address_id=None):
    """A registered Google frame of `span` metres centred `cx` east / `cy` north of (LON, LAT)."""
    west, south = UNPROJECT(cx-span/2, cy-span/2)
    east, north = UNPROJECT(cx+span/2, cy+span/2)
    frame = region_frame([west, south, east, north], frame_px=frame_px)
    scene = {'schema_version': 1, **frame, 'id': scene_id, 'address_id': address_id, 'cohort': None, 'title': scene_id,
             'image_sha256': hashlib.sha256(scene_id.encode()).hexdigest(), 'image_width': frame_px, 'image_height': frame_px,
             'objects': [], 'groups': [], 'scans': {}}
    scene['model_signature'] = R.content_signature(scene)
    return scene


def slice_object(object_id, x0, x1, depth_px=40, edge='top', frame_px=512):
    """A building outline cut by the frame: two vertices on the `edge` line, `depth_px` into the image."""
    if edge == 'top':
        ring = [[x0, 0], [x1, 0], [x1, depth_px], [x0, depth_px]]
    elif edge == 'left':
        ring = [[0, x0], [depth_px, x0], [depth_px, x1], [0, x1]]
    else:
        raise ValueError(edge)
    return {'id': object_id, 'kind': 'building', 'label': 'Building', 'ring': [[float(x), float(y)] for x, y in ring],
            'status': 'candidate', 'geometry_reviewed': False, 'source': 'sam3-text-onnx',
            'model': {'area_m2': 20., 'height_verified': False}}


def metre_building(scene, object_id, x0, y0, x1, y1):
    """A building whose metre rectangle (about LON/LAT) is written in `scene`'s pixels."""
    project = image_projection(scene)
    ring = [list(project(*UNPROJECT(x, y))) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {'id': object_id, 'kind': 'building', 'label': 'Building', 'ring': ring, 'status': 'candidate', 'geometry_reviewed': False,
            'model': {'area_m2': (x1-x0)*(y1-y0), 'height_verified': False}}


def lot(pfi, number, plan, x0, y0, x1, y1):
    ring = [list(UNPROJECT(x, y)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring+[ring[0]]]},
            'properties': {'pfi': pfi, 'spi': f'{number}\\{plan}', 'lot': number, 'plan': plan, 'status': 'approved'}}


def parcels(*features):
    return {'type': 'FeatureCollection', 'features': list(features), 'bounds': [LON-.01, LAT-.01, LON+.01, LAT+.01]}


def address(address_id, x, y, title='A HOUSE'):
    lon, lat = UNPROJECT(x, y)
    return {'address_id': address_id, 'full_address': title, 'lon': lon, 'lat': lat}


# The neighbour's lot (Lot 1, 24 x 35 m north of the source frame edge at y = 18), the source address lot south of it
# and a third lot east; one address point on each of the first two.
LOT1 = lot('p1', '1', 'PS1', -12, 15, 12, 50)
LOT2 = lot('p2', '2', 'PS1', -12, -20, 12, 15)
LOT3 = lot('p3', '3', 'PS1', 12, 15, 30, 50)
ADDRESSES = [address(777, 0, 35, '1 NORTH STREET'), address(4242, 0, -5, '2 SOUTH STREET')]
# The same neighbourhood with the boundary 5 m closer to the source centre (Lot 1 from y = 10): the lot window still
# centres north of the cut and keeps 20 of the source frame's 36 m (fraction about 0.56, below 0.6) with a 174 px band
# that erodes to about 154 px: above the 128 px subject floor.
NEAR = [lot('p1', '1', 'PS1', -12, 10, 12, 45), lot('p2', '2', 'PS1', -12, -20, 12, 10), lot('p3', '3', 'PS1', 12, 10, 30, 45)]
NEAR_ADDRESSES = [address(777, 0, 30, '1 NORTH STREET'), address(4242, 0, -5, '2 SOUTH STREET')]
SUBJECT = {'min_overlap_px': 128, 'applies_to': "frame_transform.role == 'frame_cut_target' (scene_focus windows)"}
DESIGN = {'version': 'recapture-design-v1', 'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6, 'match_iou': .5, 'close_iou': .9}


@pytest.fixture
def source():
    scene = registered('qa_source', 0, 0, 36)
    scene['objects'] = [slice_object('slice-a', 200, 300)]
    scene['model_signature'] = R.content_signature(scene)
    return scene


@pytest.fixture
def north(source):
    """Another registered frame holding the body of the cut roof: 12 x 12 m from y = 15 to 27 (9 m of it north of the cut)."""
    scene = registered('qa_north', 0, 30, 36)
    scene['objects'] = [metre_building(scene, 'body-a', -6, 15, 6, 27)]
    scene['model_signature'] = R.content_signature(scene)
    return scene


def one_row(root, source, scenes, **kwargs):
    if not (root/'data/reviews/eyes/recapture/DESIGN.json').exists():
        write_design(root)
    result = F.plan(root, {source['id']: [o['id'] for o in source['objects']]}, scenes=scenes, offline=True, **kwargs)
    assert len(result['rows']) == 1, [r['window_id'] for r in result['rows']]
    return result['rows'][0]


def metres_of(point):
    return PROJECT(point['lon'], point['lat'])


# --- window rule --------------------------------------------------------------------------------------------------

def test_frame_px_and_zoom_are_derived_from_the_native_z20_span_at_write_time():
    assert NATIVE_512 == pytest.approx(60.4, abs=.1)
    assert F.frame_px_for(60., LAT) == 512 and F.frame_px_for(NATIVE_512, LAT) == 512 and F.frame_px_for(61., LAT) == 1024
    assert F.frame_px_for(120., LAT) == 1024
    # region_frame keeps z20 for a 1024 px frame up to twice the native 512 px span (the generalised zoom term):
    # 121 m is z19 in the 512 px frame (the design's 120 -> 20, 121 -> 19) and still z20 at 1024 px
    west, south = UNPROJECT(-60.5, -60.5)
    east, north = UNPROJECT(60.5, 60.5)
    assert region_frame([west, south, east, north], frame_px=1024)['region']['imagery_zoom'] == 20
    assert region_frame([west, south, east, north])['region']['imagery_zoom'] == 19
    west, south = UNPROJECT(-60, -60)
    east, north = UNPROJECT(60, 60)
    assert region_frame([west, south, east, north])['region']['imagery_zoom'] == 20
    with pytest.raises(ValueError, match='frame_px'):
        region_frame([west, south, east, north], frame_px=768)
    # zoom = ceil(log2(frame_px/256/span)) stays 20 while span_m < 2*frame_px*native_m_per_px: 120.8 m at 512 px and about
    # 241.6 m at 1024 px (four times the 512 px native span). 120.8 m is only the point where a 1024 px frame reaches native
    # z20 detail, moot under the 120 m cap.
    assert F.z20_max_span_m(LAT, 512) == pytest.approx(2*NATIVE_512, abs=.01) and F.z20_max_span_m(LAT, 1024) == pytest.approx(4*NATIVE_512, abs=.01)
    assert F.z20_max_span_m(LAT, 1024) == pytest.approx(241.6, abs=.3)
    west, south = UNPROJECT(-120, -120)
    east, north = UNPROJECT(120, 120)
    assert region_frame([west, south, east, north], frame_px=1024)['region']['imagery_zoom'] == 20, '240 m at 1024 px is still z20'
    west, south = UNPROJECT(-121.5, -121.5)
    east, north = UNPROJECT(121.5, 121.5)
    assert region_frame([west, south, east, north], frame_px=1024)['region']['imagery_zoom'] == 19, '243 m at 1024 px drops to z19'


def test_lot_window_centres_on_the_neighbour_lot_and_sizes_to_the_lot(tmp_path, source, north):
    row = one_row(tmp_path, source, [source, north], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    subject = row['subjects'][0]
    assert subject['object_id'] == 'slice-a' and subject['touched_edges'] == ['top']
    assert [b['object_id'] for b in subject['bodies']] == ['body-a'] and subject['bodies'][0]['scene_id'] == 'qa_north'
    assert subject['lot']['pfi'] == 'p1' and subject['lot']['share'] == pytest.approx(1., abs=.01) and subject['lot']['address_id'] == 777
    window = row['window']
    assert window['rule'] == 'lot'
    cx, cy = metres_of(window['centre'])
    assert cx == pytest.approx(0, abs=.3) and cy == pytest.approx(32.5, abs=.3), 'the centre of the lot bounding box, north of the cut'
    assert window['span_m'] == pytest.approx(35*1.4+10, abs=.1) and window['frame_px'] == 512 and window['zoom'] == 20
    assert window['m_per_px'] == pytest.approx(window['span_m']/512, abs=1e-4)
    assert row['window_id'] == 'lot-777' and row['identity'] == 'lot' and row['address_id'] == 777
    assert row['lots_label'] == 'Lot 1' and row['title'] == 'Recapture window · qa_source · 1 NORTH STREET · 1 frame-cut target'
    assert row['acceptance_predicted']['inside_with_margin'] is True and row['acceptance_predicted']['widened'] is False
    # the source frame (36 m about the origin) keeps only its northern 15 m inside this window
    predicted = row['sources'][0]['predicted_overlap']
    assert predicted == pytest.approx(15/36, abs=.02) and row['overlap_gate'] == 'below_design'


def test_subject_window_without_parcels_centres_on_slice_plus_body(tmp_path, source, north):
    row = one_row(tmp_path, source, [source, north], parcels=parcels(), addresses=[])
    window = row['window']
    assert window['rule'] == 'subject' and row['subjects'][0]['lot'] is None
    cx, cy = metres_of(window['centre'])
    assert cx == pytest.approx(0, abs=.3) and cy == pytest.approx(21, abs=.3)
    assert window['span_m'] == pytest.approx(12*2+20, abs=.1) and window['frame_px'] == 512
    assert row['window_id'].startswith('win-') and len(row['window_id']) == 20 and row['identity'] == 'win'
    assert row['title'] == 'Recapture window · qa_source · lot unknown · 1 frame-cut target'


def test_object_only_window_when_neither_lot_nor_body_resolves(tmp_path, source):
    row = one_row(tmp_path, source, [source], parcels=parcels(), addresses=[])
    window = row['window']
    assert window['rule'] == 'subject_no_body' and row['subjects'][0]['bodies'] == []
    cx, cy = metres_of(window['centre'])
    slice_width = 100*36/512
    assert cx == pytest.approx((250-256)*36/512, abs=.3) and cy == pytest.approx(18-20*36/512, abs=.3)
    assert window['span_m'] == pytest.approx(max(40., slice_width*1.4+30), abs=.1) and window['frame_px'] == 512


def test_body_side_test_moves_the_subject_to_the_parcel_beyond_the_cut_edge(tmp_path, source):
    # the lot line runs 1 m inside the frame (y = 17): the slice lies mostly on Lot 2, the body beyond the edge on Lot 1
    row = one_row(tmp_path, source, [source], parcels=parcels(lot('p1', '1', 'PS1', -12, 17, 12, 50), lot('p2', '2', 'PS1', -12, -20, 12, 17)),
                  addresses=ADDRESSES)
    subject = row['subjects'][0]
    assert subject['lot']['pfi'] == 'p1' and subject['slice_parcel'] == {'pfi': 'p2', 'lot': '2', 'plan': 'PS1'}
    assert subject['reason'] == 'body_side'
    assert [entry['pfi'] for entry in row['lots']] == ['p1'] and row['window_id'] == 'lot-777'
    cx, cy = metres_of(row['window']['centre'])
    assert cy == pytest.approx(33.5, abs=.3)


def test_a_second_numbered_lot_joins_the_core_and_makes_the_window_win(tmp_path, source, north):
    # the body straddles Lot 1 (x < 2) and Lot 3 (x > 2): shares about .67 / .33
    straddle = parcels(lot('p1', '1', 'PS1', -12, 15, 2, 50), lot('p3', '3', 'PS1', 2, 15, 30, 50), LOT2)
    row = one_row(tmp_path, source, [source, north], parcels=straddle, addresses=ADDRESSES)
    assert [entry['pfi'] for entry in row['lots']] == ['p1', 'p3'] and row['lots_label'] == 'Lots 1, 3'
    assert row['identity'] == 'win' and row['window_id'].startswith('win-') and row['address_id'] is None
    assert row['subjects'][0]['lot']['share'] == pytest.approx(.67, abs=.03)


def test_common_property_joins_only_when_overlapped_and_never_the_plan_block(tmp_path, source, north):
    common = parcels(lot('pcm', 'CM1', 'PS1', -12, 15, 12, 50), lot('p9', '9', 'PS1', 12, 15, 30, 50), lot('p8', '8', 'PS1', 30, 15, 50, 50))
    row = one_row(tmp_path, source, [source, north], parcels=common, addresses=[address(777, 0, 35)])
    assert [entry['lot'] for entry in row['lots']] == ['CM1'] and 'common_property' in row['flags']
    assert row['identity'] == 'win' and row['address_id'] is None, 'common property never gives an address identity'
    assert row['lots_label'] == 'Common property CM1'


def test_a_plan_block_or_oversize_parcel_is_never_the_lot(tmp_path, source, north):
    # First plan, win-d73376483fb1949f: the only parcel of pfi 204419087 (lot None, plan CP153157, bounding span 311.28 m
    # in the local metre frame: metre_frame + _span over data/scene-parcels/3720833b*.json) acted as the lot. An
    # UNNUMBERED parcel whose plan starts CP or CM, or any parcel
    # wider than the 120 m cap, is a plan-block: never a lot, never a body-side target; the subject is subject-centred.
    block = lot('pcp', None, 'CP153157', -200, -200, 200, 200)
    row = one_row(tmp_path, source, [source, north], parcels=parcels(block), addresses=[address(777, 0, 35)])
    subject = row['subjects'][0]
    assert subject['lot'] is None and subject['reason'] == 'plan_block_only' and subject['slice_parcel'] is None
    assert subject['excluded_parcels'] == [{'pfi': 'pcp', 'lot': '', 'plan': 'CP153157', 'share': 1., 'class': 'plan_block',
                                            'span_m': pytest.approx(400, abs=.5)}]
    assert row['lots'] == [] and row['window']['rule'] == 'subject' and 'plan_block' in row['flags'] and row['identity'] == 'win'
    cx, cy = metres_of(row['window']['centre'])
    assert cx == pytest.approx(0, abs=.3) and cy == pytest.approx(21, abs=.3), 'the centre of slice plus body, past the cut edge'
    assert 'lot unknown' in row['title'] and 'CP153157' in row['title']
    # a small unnumbered CP parcel is a plan-block by its plan alone; a numbered 200 m lot is excluded by its span, so the
    # numbered lot inside it is the lot even though both hold the whole subject (the oversize pfi sorts first)
    small = lot('pcp', None, 'CP9', -12, 15, 12, 50)
    row = one_row(tmp_path, source, [source, north], parcels=parcels(small), addresses=[address(777, 0, 35)])
    assert row['subjects'][0]['excluded_parcels'][0]['class'] == 'plan_block' and row['lots'] == [] and row['window']['rule'] == 'subject'
    oversize = lot('p0', '1', 'TP1', -100, 15, 100, 215)
    row = one_row(tmp_path, source, [source, north], parcels=parcels(oversize, LOT1), addresses=ADDRESSES)
    assert [entry['pfi'] for entry in row['lots']] == ['p1'] and row['window_id'] == 'lot-777'
    assert row['subjects'][0]['excluded_parcels'][0] == {**row['subjects'][0]['excluded_parcels'][0], 'pfi': 'p0', 'class': 'oversize'}
    assert 'plan_block' not in row['flags'] and 'lot_exceeds_window' not in row['flags']
    # lot text starting CP is common property like CM (scene_addresses.py:94): it joins, never gives an address identity
    row = one_row(tmp_path, source, [source, north], parcels=parcels(lot('pcp1', 'CP1', 'PS1', -12, 15, 12, 50)), addresses=[address(777, 0, 35)])
    assert [entry['class'] for entry in row['lots']] == ['common'] and 'common_property' in row['flags'] and row['identity'] == 'win'


def test_four_targets_on_four_lots_make_four_windows_and_the_header_median_is_true(tmp_path):
    # First plan, win-0ee8dc9629f7270c: 4 targets on 4 numbered lots in one 120.006 m window with no flag (adjacent lot
    # windows overlap by more than half). A group is one house/lot: a window never spans more than one numbered lot core.
    scene = registered('qa_row', 0, 0, 36)
    scene['objects'] = [slice_object('s1', 20, 100), slice_object('s2', 150, 230), slice_object('s3', 280, 360), slice_object('s4', 410, 490)]
    scene['model_signature'] = R.content_signature(scene)
    lots = [lot('p1', '1', 'PS1', -18, 15, -9, 50), lot('p2', '2', 'PS1', -9, 15, 0, 50), lot('p3', '3', 'PS1', 0, 15, 9, 60), lot('p4', '4', 'PS1', 9, 15, 18, 60)]
    points = [address(1001, -13.5, 30), address(1002, -4.5, 30), address(1003, 4.5, 30), address(1004, 13.5, 30)]
    write_design(tmp_path)
    result = F.plan(tmp_path, {scene['id']: ['s1', 's2', 's3', 's4']}, scenes=[scene], offline=True, parcels=parcels(*lots), addresses=points)
    rows = result['rows']
    assert [r['window_id'] for r in rows] == ['lot-1001', 'lot-1002', 'lot-1003', 'lot-1004']
    assert all(len(r['lots']) == 1 and r['flags'] == [] and r['group'] == {'size': 1, 'reasons': [], 'two_lot_rule': False} for r in rows)
    assert all(s['kept_apart'] == ['window_overlap'] for r in rows for s in r['subjects']), 'the overlap was seen and refused by the lot core'
    assert result['header']['totals']['kept_apart_subjects'] == 4
    assert sorted(r['window']['span_m'] for r in rows) == pytest.approx([59, 59, 73, 73], abs=.1)
    assert [r['window']['frame_px'] for r in rows] == [512, 512, 1024, 1024]
    header = result['header']
    assert header['spans_m']['median'] == pytest.approx(66, abs=.1), 'a true median, not the upper-middle element (73)'
    assert header['totals']['windows'] == 4 and header['totals']['multi_lot_windows'] == 0 and header['totals']['plan_block_as_lot'] == 0
    assert header['totals']['multi_lot_outside_two_lot_rule'] == 0


def test_a_window_spans_two_numbered_lots_only_through_the_two_lot_rule(tmp_path, source, north):
    # the body straddles Lot 1 (x < 2) and Lot 3 (x > 2): the two-lot core {p1, p3}; slice-c wholly on Lot 3 joins that
    # window (its core lies inside the two-lot core); slice-d on Lot 4 further east never does, although its provisional
    # window overlaps by more than half
    source['objects'] += [slice_object('slice-c', 330, 380), slice_object('slice-d', 440, 500)]
    source['model_signature'] = R.content_signature(source)
    lots = [lot('p1', '1', 'PS1', -12, 15, 2, 50), lot('p3', '3', 'PS1', 2, 15, 12, 50), lot('p4', '4', 'PS1', 12, 15, 30, 50), LOT2]
    write_design(tmp_path)
    result = F.plan(tmp_path, {source['id']: ['slice-a', 'slice-c', 'slice-d']}, scenes=[source, north], offline=True,
                    parcels=parcels(*lots), addresses=[*ADDRESSES, address(4004, 20, 35, '4 NORTH STREET')])
    rows = result['rows']
    assert len(rows) == 2, [r['window_id'] for r in rows]
    two, one = rows
    assert sorted(s['object_id'] for s in two['subjects']) == ['slice-a', 'slice-c'] and [entry['pfi'] for entry in two['lots']] == ['p1', 'p3']
    assert two['identity'] == 'win' and 'shared_lot' in two['group']['reasons'] and two['group']['two_lot_rule'] is True
    assert all(len([entry for entry in s['core'] if entry['class'] == 'numbered']) <= 2 for s in two['subjects'])
    assert one['window_id'] == 'lot-4004' and [s['object_id'] for s in one['subjects']] == ['slice-d'] and one['group']['two_lot_rule'] is False
    totals = result['header']['totals']
    assert totals['windows'] == 2 and totals['multi_lot_windows'] == 1 and totals['multi_lot_outside_two_lot_rule'] == 0


def test_dedupe_names_the_first_row_s_own_subject():
    # plan()'s dedupe loop read a `members` variable leaked from the grouping loop, so duplicate_of named the LAST group's
    # first subject; it must be the first row's own first subject.
    first = {'window_id': 'win-x', 'dedupe': 'new', 'subjects': [{'scene_id': 'a', 'object_id': '1'}]}
    other = {'window_id': 'win-y', 'dedupe': 'new', 'subjects': [{'scene_id': 'b', 'object_id': '2'}]}
    twin = {'window_id': 'win-x', 'dedupe': 'exists_skip', 'subjects': [{'scene_id': 'c', 'object_id': '3'}]}
    rows = F.dedupe_rows([first, other, twin])
    assert [r['dedupe'] for r in rows] == ['new', 'new', 'duplicate_in_plan'] and rows[2]['duplicate_of'] == 'a/1', 'not b/2 (the last group)'
    assert 'duplicate_of' not in rows[0] and 'duplicate_of' not in rows[1]


def test_window_rule_is_pre_registered_once_and_its_sha_is_on_every_row(tmp_path, source, north):
    design = tmp_path/'data/reviews/eyes/recapture/DESIGN.json'
    design.parent.mkdir(parents=True)
    design.write_text(json.dumps({**DESIGN, 'subject_recapture': SUBJECT}))
    design_sha = hashlib.sha256(design.read_bytes()).hexdigest()
    targets = {source['id']: ['slice-a']}
    with pytest.raises(ValueError, match='WINDOW.json'):
        F.plan(tmp_path, targets, scenes=[source, north], offline=True, parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    path, sha = F.write_window_rule(tmp_path)
    assert path == tmp_path/'data/reviews/eyes/recapture/WINDOW.json' and sha == hashlib.sha256(path.read_bytes()).hexdigest()
    rule = json.loads(path.read_text())
    assert rule['version'] == 'frame-cut-window-v1' and rule['written_before'] == 'any non-qa capture' and rule['written_at'].endswith('Z')
    assert rule['constants'] == json.loads(json.dumps(F.WINDOW)) and rule['overlap_gate']['min_overlap_fraction'] == .6
    assert rule['overlap_gate']['design_sha256'] == design_sha and rule['overlap_gate']['min_overlap_px'] == 128
    assert rule['overlap_gate']['source'].startswith('DESIGN.json subject_recapture.min_overlap_px') and 'region' in rule['overlap_gate']['rule']
    zoom = rule['zoom']
    assert zoom['z20_max_span_1024_m'] == pytest.approx(4*zoom['native_span_512_m'], abs=.01) and rule['frame_px_rule'].startswith('512 when span_m <=')
    assert zoom['z20_max_span_1024_m'] == pytest.approx(241.6, abs=.3) and zoom['derived_at_latitude'] == -37.79
    assert set(rule) >= {'span_rule', 'frame_px_rule', 'zoom', 'subject_rule', 'parcel_classes', 'grouping', 'acceptance', 'overlap_gate', 'identity'}
    assert 'plan_block' in rule['parcel_classes'] and 'two_lot' in json.dumps(rule['grouping'])
    with pytest.raises(ValueError, match='exists'):
        F.write_window_rule(tmp_path)
    result = F.plan(tmp_path, targets, scenes=[source, north], offline=True, parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert result['header']['window_sha256'] == sha == result['rows'][0]['window_sha256'] and result['header']['design_sha256'] == design_sha
    assert result['header']['window_path'] == 'data/reviews/eyes/recapture/WINDOW.json'
    # a capture refuses a row planned under another WINDOW.json before reading any source
    with pytest.raises(ValueError, match='WINDOW.json'):
        F.capture_window(tmp_path, {**result['rows'][0], 'window_sha256': 'f'*64})
    # the registered constants are the code's: a drifted file refuses the plan
    drifted = {**rule, 'constants': {**rule['constants'], 'max_span_m': 100.}}
    path.write_text(json.dumps(drifted))
    with pytest.raises(ValueError, match='constants'):
        F.plan(tmp_path, targets, scenes=[source, north], offline=True, parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)


def test_spans_are_clamped_and_a_lot_too_large_is_flagged(tmp_path):
    tiny = registered('qa_tiny', 0, 0, 36)
    tiny['objects'] = [slice_object('sliver', 240, 268, depth_px=10)]
    tiny['model_signature'] = R.content_signature(tiny)
    row = one_row(tmp_path, tiny, [tiny], parcels=parcels(lot('p1', '1', 'PS1', -3, 15, 3, 20)), addresses=[address(777, 0, 18)])
    assert row['window']['span_m'] == pytest.approx(30, abs=.1) and row['window']['frame_px'] == 512, 'the 30 m floor keeps a tiny lot tight'
    big = registered('qa_big', 0, 0, 36)
    big['objects'] = [slice_object('slice-a', 200, 300)]
    big['model_signature'] = R.content_signature(big)
    row = one_row(tmp_path, big, [big], parcels=parcels(lot('p1', '1', 'PS1', -60, 15, 60, 135)), addresses=[address(777, 0, 100)])
    window = row['window']
    assert window['span_m'] == pytest.approx(120, abs=.1) and window['frame_px'] == 1024 and window['zoom'] == 20
    assert 'lot_exceeds_window' in row['flags'] and row['identity'] == 'win' and 'lot exceeds window' in row['title']
    assert row['acceptance_predicted']['inside_with_margin'] is True
    cx, cy = metres_of(window['centre'])
    assert cy > 18, 'the centre still moves past the cut edge'


def test_a_lot_wider_than_the_native_512_span_takes_the_1024_frame_at_z20(tmp_path, source):
    row = one_row(tmp_path, source, [source], parcels=parcels(lot('p1', '1', 'PS1', -20, 15, 20, 55)), addresses=[address(777, 0, 35)])
    window = row['window']
    assert window['span_m'] == pytest.approx(40*1.4+10, abs=.1) and window['frame_px'] == 1024 and window['zoom'] == 20
    assert window['m_per_px'] == pytest.approx(66/1024, abs=1e-4) and window['native_m_per_px'] == pytest.approx(NATIVE_512/512, abs=1e-4)
    assert row['frame']['image_width'] == 1024 and row['frame']['meters_to_pixel'][2] == [512, 512]


# --- identity, grouping and dedupe --------------------------------------------------------------------------------

def test_window_id_mirrors_map_hash_with_frame_px_and_rounded_bounds():
    bounds = [145.31234567891, -37.7912345678, 145.31298765432, -37.7905432]
    expected = 'win-'+hashlib.sha256(json.dumps(['google', [round(v, 7) for v in bounds], 512]).encode()).hexdigest()[:16]
    assert F.window_id('google', bounds, 512) == expected
    assert F.window_id('google', bounds, 1024) != expected and F.window_id('google', bounds, 512) == F.window_id('google', [round(v, 7) for v in bounds], 512)


def test_two_objects_on_one_house_form_one_window(tmp_path, source, north):
    source['objects'].append(slice_object('slice-b', 120, 180))
    source['model_signature'] = R.content_signature(source)
    row = one_row(tmp_path, source, [source, north], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert sorted(s['object_id'] for s in row['subjects']) == ['slice-a', 'slice-b'] and row['window_id'] == 'lot-777'
    assert row['sources'] == [{**row['sources'][0], 'scene_id': 'qa_source', 'object_ids': ['slice-a', 'slice-b'], 'role': 'carried_source'}]
    assert row['title'].endswith('2 frame-cut targets') and row['group']['reasons'] == sorted(row['group']['reasons'])
    assert 'shared_body' in row['group']['reasons']


def test_targets_from_two_source_scenes_on_one_lot_share_a_window_and_name_the_carry_source(tmp_path, source, north):
    other = registered('qa_other', 8, 0, 36)
    other['objects'] = [metre_building(other, 'edge-x', -4, 17.5, 4, 18.1)]
    other['objects'][0]['ring'] = [[x, min(max(y, 0.), 512.)] for x, y in other['objects'][0]['ring']]
    other['model_signature'] = R.content_signature(other)
    source['objects'].append(slice_object('slice-b', 120, 180))
    source['model_signature'] = R.content_signature(source)
    targets = {source['id']: ['slice-a', 'slice-b'], other['id']: ['edge-x']}
    write_design(tmp_path)
    result = F.plan(tmp_path, targets, scenes=[source, north, other], offline=True, parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert len(result['rows']) == 1
    row = result['rows'][0]
    roles = {s['scene_id']: s['role'] for s in row['sources']}
    assert roles == {'qa_source': 'carried_source', 'qa_other': 'uncarried_source'} and row['carry_from'] == 'qa_source'
    assert 'shared_lot' in row['group']['reasons']
    assert result['header']['totals'] == {'targets': 3, 'subjects': 3, 'windows': 1, 'captures_after_dedupe': 1, 'exists_skip': 0,
                                          'duplicate_in_plan': 0, 'lots_resolved': 1, 'unresolved_lot': 0, 'multi_lot_windows': 0,
                                          'multi_lot_outside_two_lot_rule': 0, 'plan_block_as_lot': 0, 'plan_block_subjects': 0,
                                          'kept_apart_subjects': 0, 'address_reserved_windows': 0, 'below_design_overlap': 1, 'overlap_gate_ok': 0,
                                          'below_fraction_rule': 1, 'passes_px_floor_not_fraction': 0,
                                          'uncarried_targets': 1, 'frame_px': {'512': 1}, 'zoom': {'20': 1}}


def test_grouping_reasons_can_be_restricted_to_shared_body_and_lot(tmp_path, source):
    # two slices on the same edge with no body and no lot: only their provisional windows overlap
    source['objects'].append(slice_object('slice-b', 300, 400))
    source['model_signature'] = R.content_signature(source)
    write_design(tmp_path)
    result = F.plan(tmp_path, {source['id']: ['slice-a', 'slice-b']}, scenes=[source], offline=True, parcels=parcels(), addresses=[])
    assert len(result['rows']) == 1 and result['rows'][0]['group']['reasons'] == ['window_overlap']
    subjects = [F.resolve_subject(tmp_path, source, obj, F.touched_edges(obj['ring'], source), F.RegisteredIndex(tmp_path, [source]).load([]),
                                  parcels=parcels(), addresses=[], body_distance_m=.7) for obj in source['objects']]
    assert len(F.group_subjects(subjects)) == 1
    assert [len(members) for members, _ in F.group_subjects(subjects, by=('shared_body', 'shared_lot'))] == [1, 1]


def test_identity_is_reserved_for_the_source_address_and_dedupes_against_an_existing_dir(tmp_path, source, north):
    reserved = {**source, 'address_id': 777}
    reserved['model_signature'] = R.content_signature(reserved)
    row = one_row(tmp_path, reserved, [reserved, north], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert row['identity'] == 'win' and 'address_reserved_for_source' in row['flags'] and row['address_id'] == 777
    (tmp_path/'data/scenes/lot-777').mkdir(parents=True)
    row = one_row(tmp_path, source, [source, north], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert row['window_id'] == 'lot-777' and row['dedupe'] == 'exists_skip'
    (tmp_path/'data/scenes'/row['window_id']).rmdir()
    assert one_row(tmp_path, source, [source, north], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)['dedupe'] == 'new'


def test_lot_identity_is_reserved_when_a_cohort_or_house_scene_of_that_address_exists(tmp_path, source, north):
    # Dated plan window-plan-2026-10-01.jsonl: lot-30381 (carry_from roof_training_250-30371; the NEIGHBOUR's address 30381)
    # collided with the cohort scene roof_training_250-30381, whose own-lot recapture (task recapture:capture ADDRESS=30381 ->
    # scene_lot.lot_source) writes lot-30381 and would be refused once the window existed: the window was held. lot-29772
    # (house-29772 exists) was captured and pushed before this rule and stays (recorded). Rule: a window takes lot-<address_id>
    # only when no scene roof_training_250-<address_id> or house-<address_id> exists (on disk or injected into the plan);
    # otherwise win-<hash16>, with the address, lots, rule and title unchanged inside the row.
    kwargs = dict(parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    row = one_row(tmp_path, source, [source, north], **kwargs)
    assert row['window_id'] == 'lot-777' and row['identity'] == 'lot' and row['reserved_by'] == []
    for reserving in ('roof_training_250-777', 'house-777'):
        folder = tmp_path/'data/scenes'/reserving
        folder.mkdir(parents=True)
        (folder/'latest.json').write_text(json.dumps({'id': reserving, 'address_id': 777}))
        row = one_row(tmp_path, source, [source, north], **kwargs)
        assert row['identity'] == 'win' and row['window_id'] == F.window_id('google', row['window']['requested_bounds'], row['window']['frame_px'])
        assert 'address_reserved_for_scene' in row['flags'] and row['reserved_by'] == [reserving]
        assert 'address_reserved_for_source' not in row['flags'], 'the source in the window has no address of its own'
        assert row['address_id'] == 777 and row['lots'][0]['address_id'] == 777 and row['window']['rule'] == 'lot' and row['dedupe'] == 'new'
        assert row['title'] == 'Recapture window · qa_source · 1 NORTH STREET · 1 frame-cut target'
        shutil.rmtree(folder)
    # a scene of the reserved id injected into the plan (not on disk) reserves it too
    cohort = registered('roof_training_250-777', 0, 35, 33, address_id=777)
    result = F.plan(tmp_path, {source['id']: ['slice-a']}, scenes=[source, north, cohort], offline=True, **kwargs)
    row = result['rows'][0]
    assert row['identity'] == 'win' and row['reserved_by'] == ['roof_training_250-777'] and 'address_reserved_for_scene' in row['flags']
    assert result['header']['totals']['address_reserved_windows'] == 1
    assert 'roof_training_250-<address_id>' in result['header']['window']['identity'] and 'house-<address_id>' in result['header']['window']['identity']
    # another address's scene reserves nothing; a bare directory without latest.json is not a scene
    (tmp_path/'data/scenes/roof_training_250-778').mkdir(parents=True)
    (tmp_path/'data/scenes/roof_training_250-778/latest.json').write_text(json.dumps({'id': 'roof_training_250-778', 'address_id': 778}))
    (tmp_path/'data/scenes/house-777').mkdir()
    row = one_row(tmp_path, source, [source, north], **kwargs)
    assert row['window_id'] == 'lot-777' and row['reserved_by'] == [] and 'address_reserved_for_scene' not in row['flags']
    assert F.reserved_by(tmp_path, 778) == ['roof_training_250-778'] and F.reserved_by(tmp_path, 777, known={'house-777', 'qa_x'}) == ['house-777']
    assert F.RESERVED_PREFIXES == ('roof_training_250', 'house')


def test_covered_by_existing_lists_a_registered_frame_holding_the_whole_subject_and_lot(tmp_path, source, north):
    wide = registered('qa_wide', 0, 20, 130, frame_px=1024)
    row = one_row(tmp_path, source, [source, north, wide], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert row['covered_by_existing'] == ['qa_wide'] and row['dedupe'] == 'new', 'recorded, never reused'


def test_a_target_that_is_not_frame_cut_is_listed_and_gets_no_window(tmp_path, source):
    source['objects'].append({**slice_object('inner', 200, 300), 'ring': [[200., 100.], [300., 100.], [300., 140.], [200., 140.]]})
    source['model_signature'] = R.content_signature(source)
    write_design(tmp_path)
    result = F.plan(tmp_path, {source['id']: ['inner', 'missing']}, scenes=[source], offline=True, parcels=parcels(), addresses=[])
    assert result['rows'] == [] and result['header']['skipped'] == [{'scene_id': 'qa_source', 'object_id': 'inner', 'reason': 'not_frame_cut'},
                                                                     {'scene_id': 'qa_source', 'object_id': 'missing', 'reason': 'object_not_in_snapshot'}]


# --- plan output --------------------------------------------------------------------------------------------------

def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))


def write_design(root, *, floor=SUBJECT):
    path = root/'data/reviews/eyes/recapture/DESIGN.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**DESIGN, **({'subject_recapture': floor} if floor else {})}))
    if not (root/F.WINDOW_PATH).exists():
        F.write_window_rule(root)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_plan_writes_one_header_and_one_row_per_window_and_refuses_an_existing_file(tmp_path, source, north):
    design_sha = write_design(tmp_path)
    out = tmp_path/'out/plan.jsonl'
    before = _tree(tmp_path)
    result = F.plan(tmp_path, {source['id']: ['slice-a']}, scenes=[source, north], offline=True, parcels=parcels(LOT1, LOT2, LOT3),
                    addresses=ADDRESSES, out=out)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 2 and lines[0]['header'] is True and lines[0]['version'] == 'focus-plan-v1' and lines[1]['window_id'] == 'lot-777'
    header = lines[0]
    assert header['design_sha256'] == design_sha == lines[1]['design_sha256'] and header['offline'] is True
    constants = header['window']
    assert constants['version'] == 'frame-cut-window-v1' and constants['native_span_512_m'] == pytest.approx(NATIVE_512, abs=.01)
    assert constants['native_span_1024_m'] == pytest.approx(2*NATIVE_512, abs=.01) and constants['latitude'] == pytest.approx(LAT, abs=.01)
    assert constants['body_distance_m'] == .7 and constants['lot_min_span_m'] == 30 and constants['max_span_m'] == 120
    assert header['totals']['windows'] == 1 and header['written_at'].endswith('Z') and 'host_load' in header
    assert result['rows'][0]['window_id'] == 'lot-777'
    added = [p for p in _tree(tmp_path) if p not in before]
    assert added == ['out', 'out/plan.jsonl'], added
    with pytest.raises(ValueError, match='exists'):
        F.plan(tmp_path, {source['id']: ['slice-a']}, scenes=[source, north], offline=True, parcels=parcels(), addresses=[], out=out)


def test_a_fitted_source_records_the_displacement_note_and_its_registration(tmp_path, north):
    fitted = registered('roof_training_250-1', 0, 0, 36)
    fitted.pop('region')
    fitted['objects'] = [slice_object('slice-a', 200, 300)]
    fitted['model_signature'] = R.content_signature(fitted)
    row = one_row(tmp_path, fitted, [fitted, north], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    src = row['sources'][0]
    assert src['registration'] == 'fitted' and src['displacement_note']['native_px'] == [.4444, .4444] and src['displacement_note']['applied'] is False
    assert src['displacement_note']['approx_m'] == pytest.approx(.4444*L.native_m_per_px(LAT, 19), abs=1e-3)
    assert [b['scene_id'] for b in row['subjects'][0]['bodies']] == ['qa_north'], 'a fitted crop still finds its body in a registered frame'


# --- capture (offline fetcher) ------------------------------------------------------------------------------------

class TexturedFetcher:
    """Tiles rendered from one georeferenced ground; shift_m displaces the content to plant a misregistered capture."""

    def __init__(self, seed=11, shift_m=(0., 0.)):
        rng = np.random.default_rng(seed)
        self.rects = list(zip(rng.uniform(-60, 60, (90, 2)), rng.uniform(1.5, 5., (90, 2)), rng.uniform(25, 230, (90, 3)), strict=True))
        self.shift = shift_m
        self.calls = 0

    def _child(self, z, x, y):
        self.calls += 1
        n = 2.**z
        i, j = np.meshgrid(np.arange(256)+.5, np.arange(256)+.5)
        lon, lat = (x+i/256)/n*360-180, np.degrees(np.arctan(np.sinh(np.pi*(1-2*(y+j/256)/n))))
        scale = WORLD*math.cos(math.radians(LAT))
        mx = (lon-LON)/360*scale-self.shift[0]
        my = (np.arcsinh(np.tan(np.radians(lat)))-math.asinh(math.tan(math.radians(LAT))))/(2*math.pi)*scale-self.shift[1]
        rgb = np.stack([110+30*np.sin(mx/3.1), 105+25*np.cos(my/2.3), 95+20*np.sin((mx-my)/3.7)], -1)
        for (cx, cy), (hx, hy), level in self.rects:
            rgb[(np.abs(mx-cx) < hx) & (np.abs(my-cy) < hy)] = level
        return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), 'RGB')


def captured_source(root, fetcher, scene_id='qa_source'):
    west, south = UNPROJECT(-18, -18)
    east, north = UNPROJECT(18, 18)
    scene, _, data = capture_region([west, south, east, north], root=root, fetcher=fetcher)
    scene.update(id=scene_id, objects=[slice_object('slice-a', 200, 300)])
    return save_scene(scene, data, root)


# a lot spanning both sides of the cut (24 x 70 m) so the window holds the whole source frame: overlap 1.0, 1024 px
WHOLE = parcels(lot('p1', '1', 'PS1', -12, -20, 12, 50))


def test_capture_writes_only_the_window_scene_with_a_verified_frame_transform(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = captured_source(tmp_path, fetcher)
    folder = tmp_path/'data/scenes/qa_source'
    untouched = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    assert row['window_id'] == 'lot-777' and row['window']['frame_px'] == 1024 and row['overlap_gate'] == 'ok'
    before = _tree(tmp_path)
    scene, image = F.capture_window(tmp_path, row, fetcher=fetcher)
    assert scene['id'] == 'lot-777' and scene['address_id'] == 777 and image.size == (1024, 1024) and scene['image_width'] == 1024
    ft = scene['frame_transform']
    assert ft['verified'] is True and ft['overlap']['fraction'] == 1.0 and ft['residual']['shift_m'] < .1 and ft['residual']['ncc'] > .5
    assert ft['source_scene_id'] == 'qa_source' and ft['source_model_signature'] == source['model_signature'] and ft['source_size'] == [512, 512]
    assert ft['role'] == 'frame_cut_target' and ft['design']['subject_recapture'] == SUBJECT and ft['design']['design_sha256'] == row['design_sha256']
    assert ft['overlap']['region_px']['side_px'] >= 128 and ft['overlap']['region_px']['eroded_by_px'] == R.EDGE_PX
    assert 'source_displacement' not in ft, 'a registered source carries no paste-rounding displacement'
    window = scene['window_frame']
    assert window['version'] == 'frame-cut-window-v1' and window['source_scene_id'] == 'qa_source'
    assert window['subjects'][0]['object_id'] == 'slice-a' and window['window']['frame_px'] == 1024 and window['window']['rule'] == 'lot'
    assert window['acceptance']['inside_with_margin'] is True and window['acceptance']['min_edge_margin_m'] >= 5
    assert scene['objects'] == [] and scene['title'] == row['title']
    added = [p for p in _tree(tmp_path) if p not in before]
    assert added and all(p.startswith('data/scenes/lot-777') for p in added), added
    assert not (tmp_path/'data/scenes/lot-777/review.json').exists(), 'no carry happens here'
    assert json.loads((tmp_path/'data/scenes/lot-777/latest.json').read_text())['model_signature'] == scene['model_signature']
    assert {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == untouched, 'the source scene is never written'
    assert R.verify(folder/'latest.json', tmp_path/'data/scenes/lot-777/latest.json', tmp_path)[0] is True
    calls = fetcher.calls
    with pytest.raises(ValueError, match='exists'):
        F.capture_window(tmp_path, row, fetcher=fetcher)
    assert fetcher.calls == calls, 'an existing window is refused before any tile is fetched'


def test_capture_refuses_a_frame_that_fails_the_design_and_saves_nothing(tmp_path):
    write_design(tmp_path)
    source = captured_source(tmp_path, TexturedFetcher())
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    before = _tree(tmp_path)
    with pytest.raises(L.UnverifiedRecapture) as refused:
        F.capture_window(tmp_path, row, fetcher=TexturedFetcher(shift_m=(1.2, -.9)))
    assert str(refused.value).splitlines()[0] == 'Recaptured frame does not agree with the source frame'
    assert any(r.startswith('shift_m') for r in refused.value.reasons), refused.value.reasons
    assert _tree(tmp_path) == before, 'nothing is saved for a frame that fails the design'
    scene, _ = F.capture_window(tmp_path, row, fetcher=TexturedFetcher(shift_m=(1.2, -.9)), allow_unverified=True)
    assert scene['frame_transform']['verified'] is False and scene['frame_transform']['residual']['shift_m'] == pytest.approx(1.5, abs=.15)


def test_capture_refuses_a_window_whose_predicted_overlap_is_below_the_design(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = captured_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert row['overlap_gate'] == 'below_design'
    before, calls = _tree(tmp_path), fetcher.calls
    with pytest.raises(L.UnverifiedRecapture) as refused:
        F.capture_window(tmp_path, row, fetcher=fetcher)
    assert any(r.startswith('overlap') for r in refused.value.reasons), refused.value.reasons
    assert _tree(tmp_path) == before and fetcher.calls == calls, 'the gate is read from the plan before any tile is fetched'


def test_capture_checks_the_source_snapshot_and_a_scratch_id_before_fetching(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = captured_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    calls = fetcher.calls
    with pytest.raises(ValueError, match='snapshot'):
        F.capture_window(tmp_path, {**row, 'source_model_signature': 'f'*64}, fetcher=fetcher)
    with pytest.raises(ValueError, match='qa_'):
        F.capture_window(tmp_path, row, fetcher=fetcher, scene_id='lot-1')
    assert fetcher.calls == calls
    scene, _ = F.capture_window(tmp_path, row, fetcher=fetcher, scene_id='qa_focus_rehearsal')
    assert scene['id'] == 'qa_focus_rehearsal' and (tmp_path/'data/scenes/qa_focus_rehearsal/latest.json').exists()
    assert scene['window_frame']['planned_window_id'] == 'lot-777'


def test_cache_only_fetcher_reads_tiles_from_the_cache_and_never_the_network(tmp_path):
    fetcher = F.CacheOnlyFetcher(tmp_path, 'google')
    assert fetcher._child(20, 1, 2) is None
    path = tmp_path/'data/imagery/google/20/1_2.jpg'
    path.parent.mkdir(parents=True)
    Image.new('RGB', (256, 256), (40, 80, 120)).save(path)
    pixel = fetcher._child(20, 1, 2).getpixel((0, 0))
    assert all(abs(a-b) <= 2 for a, b in zip(pixel, (40, 80, 120), strict=True)) and fetcher.hits == 1 and fetcher.misses == 1


def test_cli_plan_and_capture_flags(tmp_path, source, capsys):
    targets = tmp_path/'targets.json'
    targets.write_text(json.dumps({source['id']: ['slice-a']}))
    with pytest.raises(SystemExit):
        F.main(['capture', '--root', str(tmp_path), '--plan', 'x.jsonl'])      # a capture names its window
    with pytest.raises(SystemExit):
        F.main(['plan', '--root', str(tmp_path)])                              # a plan needs targets


# --- the subject overlap floor (DESIGN.json subject_recapture, amendment of 2026-10-01) -----------------------------

def test_the_overlap_gate_reads_the_eroded_region_side_in_target_px_not_the_source_fraction(tmp_path, source, north):
    """A lot window centred past the cut keeps under 0.6 of the source frame but a region above the 128 px floor: gate ok;
    the row records the fraction, the region and which rule each passes; the header counts both rules."""
    write_design(tmp_path)
    result = F.plan(tmp_path, {source['id']: ['slice-a']}, scenes=[source, north], offline=True, parcels=parcels(*NEAR), addresses=NEAR_ADDRESSES)
    row = result['rows'][0]
    carried = row['sources'][0]
    assert row['window_id'] == 'lot-777' and carried['role'] == 'carried_source'
    assert carried['predicted_overlap'] == pytest.approx(20/36, abs=.02) and carried['predicted_overlap'] < .6
    region = carried['predicted_region_px']
    assert region['eroded_by_px'] == R.EDGE_PX and region['side_px'] == pytest.approx(20/row['window']['m_per_px']-2*R.EDGE_PX, abs=3)
    assert region['side_px'] >= 128 and row['overlap_gate'] == 'ok'
    detail = row['overlap_gate_detail']
    assert detail == {'rule': 'subject_recapture.min_overlap_px', 'min_overlap_px': 128, 'region_side_px': region['side_px'],
                      'region_area_px': region['area_px'], 'fraction': carried['predicted_overlap'], 'min_overlap_fraction': .6,
                      'passes_px_floor': True, 'passes_fraction_rule': False}
    totals = result['header']['totals']
    assert totals['below_design_overlap'] == 0 and totals['below_fraction_rule'] == 1 and totals['passes_px_floor_not_fraction'] == 1
    assert result['header']['overlap_gate'] == {'rule': 'subject_recapture.min_overlap_px', 'min_overlap_px': 128, 'min_overlap_fraction': .6,
                                                'eroded_by_px': 10,
                                                'region': 'source footprint inside the planned frame, eroded by EDGE_PX; bounding-box side in target px'}
    # the original neighbourhood (boundary at y = 15) keeps a 15 m band: 130 px before erosion, about 110 after: below the floor
    far = one_row(tmp_path, source, [source, north], parcels=parcels(LOT1, LOT2, LOT3), addresses=ADDRESSES)
    assert far['overlap_gate'] == 'below_design' and far['overlap_gate_detail']['passes_px_floor'] is False
    assert far['sources'][0]['predicted_region_px']['side_px'] < 128 and far['overlap_gate_detail']['passes_fraction_rule'] is False
    # the uncarried source of a two-scene window is predicted too (fraction and region), but only carry_from gates
    assert all('predicted_region_px' in s for s in far['sources'])


def test_plan_refuses_a_design_without_the_subject_floor(tmp_path, source, north):
    write_design(tmp_path, floor=None)
    with pytest.raises(ValueError, match='subject_recapture'):
        F.plan(tmp_path, {source['id']: ['slice-a']}, scenes=[source, north], offline=True, parcels=parcels(*NEAR), addresses=NEAR_ADDRESSES)


def test_capture_verifies_a_subject_window_on_the_region_floor_when_the_fraction_is_below_0_6(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = captured_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=parcels(*NEAR), addresses=NEAR_ADDRESSES)
    assert row['overlap_gate'] == 'ok' and row['overlap_gate_detail']['passes_fraction_rule'] is False
    scene, _ = F.capture_window(tmp_path, row, fetcher=fetcher)
    ft = scene['frame_transform']
    assert ft['verified'] is True and ft['role'] == 'frame_cut_target' and ft['overlap']['fraction'] < .6
    assert ft['overlap']['region_px']['side_px'] >= 128 and ft['design']['subject_recapture'] == SUBJECT
    assert ft['residual']['shift_m'] <= .7 and ft['residual']['ncc'] >= .35, 'shift and ncc are unchanged requirements'
    assert R.verify(tmp_path/'data/scenes/qa_source/latest.json', tmp_path/f"data/scenes/{scene['id']}/latest.json", tmp_path)[0] is True
    # a plan row written under the pre-floor design is refused before any tile is fetched
    calls = fetcher.calls
    with pytest.raises(ValueError, match='re-plan'):
        F.capture_window(tmp_path, {**row, 'design_sha256': 'f'*64}, fetcher=fetcher)
    assert fetcher.calls == calls


def test_capture_accepts_a_plan_row_written_under_a_design_amended_without_a_threshold_change(tmp_path):
    """A plan row pins the DESIGN.json sha of its planning run. An identity amendment between plan and capture keeps the row
    capturable (the chain hop is recorded as window_frame.plan_design_sha_match); a threshold change still asks for a re-plan
    before any tile is fetched."""
    before = write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = captured_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    assert row['design_sha256'] == before
    path = tmp_path/'data/reviews/eyes/recapture/DESIGN.json'
    identity = {**json.loads(path.read_bytes()), 'identity': 'win-<hash16>',
                'amendments': [{'at': '2026-10-01T08:00:00Z', 'field': 'identity', 'previous_sha256': before, 'thresholds_changed': False}]}
    path.write_text(json.dumps({**identity, 'amendments': [{**identity['amendments'][0], 'thresholds_changed': True}]}))
    calls = fetcher.calls
    with pytest.raises(ValueError, match='re-plan before capturing'):
        F.capture_window(tmp_path, row, fetcher=fetcher)
    assert fetcher.calls == calls and not (tmp_path/'data/scenes/lot-777').exists()
    path.write_text(json.dumps(identity))
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    scene, _ = F.capture_window(tmp_path, row, fetcher=fetcher)
    ft, frame = scene['frame_transform'], scene['window_frame']
    assert ft['verified'] is True and ft['design']['design_sha256'] == after, 'the measured transform records the design it was verified under'
    assert frame['plan_design_sha256'] == before and frame['plan_design_sha_match'] == {
        'match': 'design_sha_via_chain', 'hops_back': 1, 'amendment_index': 1, 'amendment_at': '2026-10-01T08:00:00Z', 'amendment_field': 'identity'}


# --- target carry and registry (design section 4 A; runbook step 7) ------------------------------------------------

def _captured_window(tmp_path, fetcher, *, scene_id='qa_source'):
    source = captured_source(tmp_path, fetcher, scene_id=scene_id)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    scene, _ = F.capture_window(tmp_path, row, fetcher=fetcher)
    return source, row, scene


def test_carry_writes_the_window_review_at_revision_1_and_appends_registry_rows(tmp_path):
    from scripts.ml.training.sam3_mac import r5_precision as r5
    design_sha = write_design(tmp_path)
    fetcher = TexturedFetcher()
    source, row, scene = _captured_window(tmp_path, fetcher)
    folder = tmp_path/'data/scenes/qa_source'
    untouched = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    before = _tree(tmp_path)
    result = F.carry_window(tmp_path, 'lot-777', registry=registry, now='2026-10-01T07:00:00Z')
    added = [p for p in _tree(tmp_path) if p not in before]
    assert sorted(added) == ['data/reviews/eyes/recapture/windows-2026-10-01.jsonl', 'data/scenes/lot-777/review.json'], added
    assert {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == untouched, 'the source scene is never written'
    review = json.loads((tmp_path/'data/scenes/lot-777/review.json').read_text())
    assert review['revision'] == 1 and review['model_signature'] == scene['model_signature'] and review['groups'] == [] and review['reviewed_classes'] == []
    assert len(review['objects']) == 1
    copy = review['objects'][0]
    model_obj = next(o for o in source['objects'] if o['id'] == 'slice-a')
    assert copy['id'] == 'slice-a' and copy['kind'] == 'building' and copy['label'] == 'Building' and copy['source'] == 'sam3-text-onnx'
    assert copy['status'] == 'needs_adjustment' and copy['geometry_reviewed'] is False and copy['model']['height_verified'] is False
    assert copy['ring'] == R.project_object(model_obj, scene['frame_transform'])['ring'] and copy['ring'] != model_obj['ring']
    assert all(0 <= x <= 1024 and 0 <= y <= 1024 for x, y in copy['ring'])
    assert copy['notes'] == ('Carried from qa_source through the georeferenced frame as a frame-cut target: the earlier frame cut this '
                             'outline at its edge; decide roof / not a roof on this image.')
    assert copy['carried_from'] == {'scene_id': 'qa_source', 'model_signature': source['model_signature'], 'object_id': 'slice-a',
                                    'status': 'candidate', 'geometry_reviewed': False, 'iou_with_new': 0, 'matched_id': None,
                                    'method': 'georeferenced-affine', 'role': 'frame_cut_target'}
    assert not any(k in copy for k in ('provenance', 'fieldwork_import', 'replaces'))
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert len(rows) == 1 and result['rows'] == 1 and result['carried'][0]['id'] == 'slice-a' and result['registry_path'] == str(registry)
    assert rows[0] == {'version': 'windows-registry-v1', 'source_scene_id': 'qa_source', 'object_id': 'slice-a', 'window_scene_id': 'lot-777',
                       'carried_object_id': 'slice-a', 'role': 'carried_source', 'frame_transform_verified': True, 'design_sha256': design_sha,
                       'window_sha256': row['window_sha256'], 'source_model_signature': source['model_signature'],
                       'source_image_sha256': source['image_sha256'], 'written_at': '2026-10-01T07:00:00Z'}
    # the counter's hop reads this registry: the row registers the key to this window and the copy would be undecided (carried_undecided)
    lineage = r5.load_lineage(tmp_path/'data/reviews/eyes/recapture')
    assert lineage['design_sha256'] == design_sha and lineage['registry_rows'] == 1
    assert r5.registered(lineage, ('qa_source', 'slice-a'), 'lot-777') is True
    assert r5.classify(copy, review['revision']) == ('undecided', 'carried_undecided', 'carried_undecided', None)
    assert r5.carry_refusals(copy['carried_from'], scene, source['image_sha256'], True, design_sha) == []
    # a second carry skips: nothing appended, the review untouched
    tree = _tree(tmp_path)
    again = F.carry_window(tmp_path, 'lot-777', registry=registry)
    assert again['skipped'] == 'review_exists' and _tree(tmp_path) == tree and len(registry.read_text().splitlines()) == 1


def test_carry_refuses_by_name_and_writes_nothing(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = captured_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    # an unverified frame (kept with --allow-unverified) is never carried
    F.capture_window(tmp_path, row, fetcher=TexturedFetcher(shift_m=(1.2, -.9)), allow_unverified=True)
    before = _tree(tmp_path)
    with pytest.raises(ValueError, match='carry_unverified'):
        F.carry_window(tmp_path, 'lot-777', registry=registry)
    assert _tree(tmp_path) == before and not registry.exists()
    # a window without a registry file is refused unless it is a qa_ scratch scene
    (tmp_path/'data/scenes/lot-777').rename(tmp_path/'data/scenes/qa_focus_x')
    with pytest.raises(ValueError, match='no saved window scene'):
        F.carry_window(tmp_path, 'qa_focus_x', registry=None)                 # latest.json still says id lot-777
    import shutil
    shutil.rmtree(tmp_path/'data/scenes/qa_focus_x')
    scene, _ = F.capture_window(tmp_path, row, fetcher=fetcher)
    assert scene['frame_transform']['verified'] is True
    with pytest.raises(ValueError, match='registry file is required'):
        F.carry_window(tmp_path, 'lot-777', registry=None)
    # a frozen key already registered to ANOTHER window makes a second window ambiguous: refused, nothing written
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({'source_scene_id': 'qa_source', 'object_id': 'slice-a', 'window_scene_id': 'win-elsewhere',
                                    'role': 'carried_source', 'frame_transform_verified': True}) + '\n')
    before = _tree(tmp_path)
    with pytest.raises(ValueError, match='ambiguous_carry'):
        F.carry_window(tmp_path, 'lot-777', registry=registry)
    assert _tree(tmp_path) == before and len(registry.read_text().splitlines()) == 1
    # a withdrawn row frees the key again
    with registry.open('a') as handle:
        handle.write(json.dumps({'source_scene_id': 'qa_source', 'object_id': 'slice-a', 'window_scene_id': 'win-elsewhere', 'withdrawn': True}) + '\n')
    # ... but a DESIGN.json that moved since the capture with no dated chain to the recorded sha refuses (carry_design_mismatch)
    path = tmp_path/'data/reviews/eyes/recapture/DESIGN.json'
    kept = path.read_bytes()
    before = hashlib.sha256(kept).hexdigest()
    path.write_text(json.dumps({**json.loads(kept), 'amendments': [{'at': 'x'}]}))
    with pytest.raises(ValueError, match='carry_design_mismatch'):
        F.carry_window(tmp_path, 'lot-777', registry=registry)
    # the same move declared as a THRESHOLD change (previous_sha256 named) refuses too
    identity = {**json.loads(kept), 'identity': 'win-<hash16>',
                'amendments': [{'at': '2026-10-01T08:00:00Z', 'field': 'identity', 'previous_sha256': before, 'thresholds_changed': False}]}
    path.write_text(json.dumps({**identity, 'amendments': [{**identity['amendments'][0], 'thresholds_changed': True}]}))
    with pytest.raises(ValueError, match='carry_design_mismatch'):
        F.carry_window(tmp_path, 'lot-777', registry=registry)
    assert not (tmp_path/'data/scenes/lot-777/review.json').exists() and len(registry.read_text().splitlines()) == 2
    # a dated amendment that changes no threshold keeps the captured window carriable: the transform's recorded sha is accepted
    # through the chain (design_sha_via_chain, hop named) and the new registry row records the sha at write time
    path.write_text(json.dumps(identity))
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    result = F.carry_window(tmp_path, 'lot-777', registry=registry)
    assert result['rows'] == 1 and len(registry.read_text().splitlines()) == 3 and result['design_sha256'] == after != before
    hop = {'match': 'design_sha_via_chain', 'hops_back': 1, 'amendment_index': 1, 'amendment_at': '2026-10-01T08:00:00Z',
           'amendment_field': 'identity'}
    assert result['design_sha_match'] == hop
    row = json.loads(registry.read_text().splitlines()[-1])
    assert row['design_sha256'] == after and row['window_scene_id'] == 'lot-777'
    from scripts.ml.training.sam3_mac import r5_precision as r5
    lineage = r5.load_lineage(tmp_path/'data/reviews/eyes/recapture')
    assert lineage['design_sha256'] == after and r5.registered(lineage, ('qa_source', 'slice-a'), 'lot-777') is True
    latest = json.loads((tmp_path/'data/scenes/lot-777/latest.json').read_text())
    copy = json.loads((tmp_path/'data/scenes/lot-777/review.json').read_text())['objects'][0]
    assert latest['frame_transform']['design']['design_sha256'] == before, 'the saved window is never rewritten'
    assert r5.carry_refusals(copy['carried_from'], latest, scene['frame_transform']['source_image_sha256'], True, lineage) == []


def test_carry_keeps_the_source_status_as_a_hint_skips_a_decided_source_and_renames_a_taken_id(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source, row, scene = _captured_window(tmp_path, fetcher)
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    source_review = tmp_path/'data/scenes/qa_source/review.json'
    hinted = {**source['objects'][0], 'status': 'needs_adjustment', 'geometry_reviewed': True,
              'notes': 'Assistant image review · Codex · 2026-10-01: a pending draft', 'fieldwork_import': {'pending': True, 'cursor': '1'},
              'provenance': {'source': 'human:ui'}}
    source_review.write_text(json.dumps({'revision': 3, 'model_signature': source['model_signature'], 'objects': [hinted], 'groups': [],
                                         'reviewed_classes': []}))
    # the window model already uses the id (a detection ran first): the copy takes <id>-carried, carried_from keeps the source id
    window = tmp_path/'data/scenes/lot-777'
    latest = json.loads((window/'latest.json').read_text())
    latest['objects'] = [{**slice_object('slice-a', 10, 20, frame_px=1024), 'ring': [[10., 10.], [20., 10.], [20., 20.], [10., 20.]]}]
    (window/'latest.json').write_text(json.dumps(latest))
    result = F.carry_window(tmp_path, 'lot-777', registry=registry)
    review = json.loads((window/'review.json').read_text())
    copy = review['objects'][0]
    assert copy['id'] == 'slice-a-carried' and result['renamed'] == ['slice-a-carried']
    assert copy['carried_from']['object_id'] == 'slice-a' and copy['carried_from']['status'] == 'needs_adjustment' and copy['carried_from']['geometry_reviewed'] is True
    assert copy['status'] == 'needs_adjustment' and copy['geometry_reviewed'] is False
    assert copy['notes'].startswith('Carried from qa_source') and 'Codex' not in copy['notes'], 'no assistant text travels'
    assert 'fieldwork_import' not in copy and 'provenance' not in copy, 'the pending draft and its provenance stay on the source'
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert rows[0]['carried_object_id'] == 'slice-a-carried' and rows[0]['object_id'] == 'slice-a'
    # a source decision the counter already classifies (confirmed, reviewer's own, no assistant text) wins: not carried, no row
    import shutil
    shutil.rmtree(window)
    registry.unlink()
    decided = {**source['objects'][0], 'status': 'confirmed', 'geometry_reviewed': True}
    source_review.write_text(json.dumps({'revision': 2, 'model_signature': source['model_signature'], 'objects': [decided], 'groups': [],
                                         'reviewed_classes': []}))
    F.capture_window(tmp_path, row, fetcher=fetcher)
    before = _tree(tmp_path)
    result = F.carry_window(tmp_path, 'lot-777', registry=registry)
    assert result['skipped'] == 'nothing_to_carry' and result['decided_on_source'] == [{'scene_id': 'qa_source', 'object_id': 'slice-a',
                                                                                        'status': 'confirmed', 'reason': 'decided_on_source'}]
    assert _tree(tmp_path) == before and not registry.exists(), 'a decided source writes no review and no row'
    # an accepted-import confirmation on the source (the 2026-10-01 RULES amendment) is a decision too; a pending draft is not
    accepted = {**decided, 'notes': 'Assistant image review · Codex · 2026-10-01: roof',
                'fieldwork_import': {'pending': False, 'accepted_at': '2026-10-01T02:48:34Z', 'accepted_by': 'local'}}
    source_review.write_text(json.dumps({'revision': 4, 'model_signature': source['model_signature'], 'objects': [accepted], 'groups': [], 'reviewed_classes': []}))
    assert F.carry_window(tmp_path, 'lot-777', registry=registry)['skipped'] == 'nothing_to_carry'
    pending = {**accepted, 'fieldwork_import': {'pending': True}}
    source_review.write_text(json.dumps({'revision': 4, 'model_signature': source['model_signature'], 'objects': [pending], 'groups': [], 'reviewed_classes': []}))
    result = F.carry_window(tmp_path, 'lot-777', registry=registry)
    assert result['carried'][0]['hint_status'] == 'confirmed' and (window/'review.json').exists()


def test_carry_registers_the_other_source_scene_s_targets_as_uncarried(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    first = captured_source(tmp_path, fetcher, scene_id='qa_source')
    first['objects'] = [slice_object('slice-a', 200, 300), slice_object('slice-b', 60, 150)]
    first = save_scene(first, (tmp_path/'data/scenes/qa_source'/f"{first['image_sha256']}.png").read_bytes(), tmp_path)
    second = captured_source(tmp_path, fetcher, scene_id='qa_source_b')
    second['objects'] = [slice_object('slice-c', 330, 420)]
    second = save_scene(second, (tmp_path/'data/scenes/qa_source_b'/f"{second['image_sha256']}.png").read_bytes(), tmp_path)
    result = F.plan(tmp_path, {'qa_source': ['slice-a', 'slice-b'], 'qa_source_b': ['slice-c']}, scenes=[first, second], offline=True,
                    parcels=WHOLE, addresses=[address(777, 0, 35)])
    assert len(result['rows']) == 1
    row = result['rows'][0]
    assert row['carry_from'] == 'qa_source' and [s['role'] for s in row['sources']] == ['carried_source', 'uncarried_source']
    F.capture_window(tmp_path, row, fetcher=fetcher)
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    out = F.carry_window(tmp_path, row['window_id'], registry=registry)
    assert [c['id'] for c in out['carried']] == ['slice-a', 'slice-b'] and out['uncarried'] == [['qa_source_b', 'slice-c']] and out['rows'] == 3
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert [(r['source_scene_id'], r['object_id'], r['role'], r['carried_object_id']) for r in rows] == [
        ('qa_source', 'slice-a', 'carried_source', 'slice-a'), ('qa_source', 'slice-b', 'carried_source', 'slice-b'),
        ('qa_source_b', 'slice-c', 'uncarried_source', None)]
    review = json.loads((tmp_path/'data/scenes'/row['window_id']/'review.json').read_text())
    assert [o['carried_from']['scene_id'] for o in review['objects']] == ['qa_source', 'qa_source']


def test_cli_carry_prints_the_result_and_names_a_refusal(tmp_path, capsys):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    _captured_window(tmp_path, fetcher)
    registry = tmp_path/'out/windows-x.jsonl'
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', str(registry)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['window_scene_id'] == 'lot-777' and printed['rows'] == 1 and registry.exists()
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', str(registry)]) == 3     # review exists: skipped
    assert json.loads(capsys.readouterr().out)['skipped'] == 'review_exists'
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-404', '--registry', str(registry)]) == 1
    assert 'Window not carried' in capsys.readouterr().err
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', 'none']) == 3


# --- group records: the review app's save validator ------------------------------------------------------------------

def _save_shape_ok(review):
    """The group rules of the review app's save validator (src/lib/scene-store.ts validateSceneReview): a group is {id, label}
    with a valid unique id and a non-empty label of at most 150 characters; every object group_id names a group; every
    parent chain resolves inside the review without a loop."""
    ids = {o['id'] for o in review['objects']}
    groups = set()
    for g in review['groups']:
        assert set(g) == {'id', 'label'}, g
        assert re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', g['id']) and g['id'] not in groups, g
        assert isinstance(g['label'], str) and g['label'].strip() and len(g['label']) <= 150, g
        groups.add(g['id'])
    for o in review['objects']:
        assert not o.get('group_id') or o['group_id'] in groups, (o['id'], o.get('group_id'))
        parent, seen = o.get('parent_id'), {o['id']}
        while parent:
            assert parent not in seen and parent in ids, (o['id'], parent)
            seen.add(parent)
            parent = next(p for p in review['objects'] if p['id'] == parent).get('parent_id')
    return True


def _grouped_source(tmp_path, fetcher):
    """A captured source whose five frame-cut slices point at groups and parents: a and b share group g-a (the source review
    labels it, latest.json carries an extra key), c names a group no file defines, d's parent is a (carried), e's parent is
    body-x (never a target)."""
    source = captured_source(tmp_path, fetcher)
    source['objects'] = [{**slice_object('slice-a', 200, 300), 'group_id': 'g-a'}, {**slice_object('slice-b', 60, 150), 'group_id': 'g-a'},
                         {**slice_object('slice-c', 330, 420), 'group_id': 'g-missing'}, {**slice_object('slice-d', 430, 500), 'parent_id': 'slice-a'},
                         {**slice_object('slice-e', 10, 50), 'parent_id': 'body-x'}]
    source['groups'] = [{'id': 'g-a', 'label': 'Building', 'colour': '#ffffff'}, {'id': 'g-other', 'label': 'Shed'}]
    folder = tmp_path/'data/scenes/qa_source'
    source = save_scene(source, (folder/f"{source['image_sha256']}.png").read_bytes(), tmp_path)
    (folder/'review.json').write_text(json.dumps({'revision': 1, 'model_signature': source['model_signature'], 'objects': [],
                                                  'groups': [{'id': 'g-a', 'label': 'Shared roof'}], 'reviewed_classes': []}))
    return source


def test_carry_carries_the_group_records_the_copies_name_and_drops_unresolvable_pointers(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = _grouped_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    F.capture_window(tmp_path, row, fetcher=fetcher)
    window = tmp_path/'data/scenes/lot-777'
    # the window model already uses slice-a: the copy is renamed and its child's parent pointer follows the rename
    latest = json.loads((window/'latest.json').read_text())
    latest['objects'] = [{**slice_object('slice-a', 10, 20, frame_px=1024), 'ring': [[10., 10.], [20., 10.], [20., 20.], [10., 20.]]}]
    (window/'latest.json').write_text(json.dumps(latest))
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    result = F.carry_window(tmp_path, 'lot-777', registry=registry)
    review = json.loads((window/'review.json').read_text())
    copies = {c['carried_from']['object_id']: c for c in review['objects']}
    assert set(copies) == {'slice-a', 'slice-b', 'slice-c', 'slice-d', 'slice-e'} and copies['slice-a']['id'] == 'slice-a-carried'
    # one record for the two copies sharing it; the source review's label wins over latest.json; only SceneGroup keys travel
    assert review['groups'] == [{'id': 'g-a', 'label': 'Shared roof'}] and result['groups'] == ['g-a']
    assert copies['slice-a']['group_id'] == 'g-a' and copies['slice-b']['group_id'] == 'g-a'
    assert 'dropped_pointers' not in copies['slice-a']['carried_from'] and 'dropped_pointers' not in copies['slice-b']['carried_from']
    # a group no source file defines: the pointer is dropped and recorded
    assert 'group_id' not in copies['slice-c']
    assert copies['slice-c']['carried_from']['dropped_pointers'] == [{'field': 'group_id', 'value': 'g-missing', 'reason': 'group_not_found'}]
    # a parent that is carried stays (renamed with its copy); a parent that is not carried is dropped and recorded
    assert copies['slice-d']['parent_id'] == 'slice-a-carried' and 'dropped_pointers' not in copies['slice-d']['carried_from']
    assert 'parent_id' not in copies['slice-e']
    assert copies['slice-e']['carried_from']['dropped_pointers'] == [{'field': 'parent_id', 'value': 'body-x', 'reason': 'parent_not_carried'}]
    assert result['dropped_pointers'] == [{'id': 'slice-c', 'field': 'group_id', 'value': 'g-missing', 'reason': 'group_not_found'},
                                          {'id': 'slice-e', 'field': 'parent_id', 'value': 'body-x', 'reason': 'parent_not_carried'}]
    assert _save_shape_ok(review)
    # the other carried_from fields are the contract's
    assert {k: v for k, v in copies['slice-c']['carried_from'].items() if k != 'dropped_pointers'} == {
        'scene_id': 'qa_source', 'model_signature': source['model_signature'], 'object_id': 'slice-c', 'status': 'candidate',
        'geometry_reviewed': False, 'iou_with_new': 0, 'matched_id': None, 'method': 'georeferenced-affine', 'role': 'frame_cut_target'}


def test_carry_falls_back_to_the_snapshot_and_latest_groups_when_the_source_review_has_none(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = _grouped_source(tmp_path, fetcher)
    (tmp_path/'data/scenes/qa_source/review.json').unlink()
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    F.capture_window(tmp_path, row, fetcher=fetcher)
    result = F.carry_window(tmp_path, 'lot-777', registry=tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl')
    review = json.loads((tmp_path/'data/scenes/lot-777/review.json').read_text())
    assert review['groups'] == [{'id': 'g-a', 'label': 'Building'}] and result['groups'] == ['g-a'], 'latest.json defines g-a; the extra key is stripped'
    assert _save_shape_ok(review)


def _break_groups(window):
    """Re-create the defect: a carry review written with groups [] while its copies still name a group."""
    path = window/'review.json'
    review = json.loads(path.read_text())
    assert review['groups'], 'the fixed carry wrote the records'
    review['groups'] = []
    path.write_text(json.dumps(review, indent=2))
    return review


def test_repair_groups_adds_the_records_to_a_revision_1_carry_review_and_nothing_else(tmp_path):
    from scripts.ml.training.sam3_mac import r5_precision as r5
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = _grouped_source(tmp_path, fetcher)
    source['objects'] = source['objects'][:2]               # a and b: one shared group, nothing to drop
    folder = tmp_path/'data/scenes/qa_source'
    source = save_scene(source, (folder/f"{source['image_sha256']}.png").read_bytes(), tmp_path)
    (folder/'review.json').write_text(json.dumps({'revision': 1, 'model_signature': source['model_signature'], 'objects': [],
                                                  'groups': [{'id': 'g-a', 'label': 'Shared roof'}], 'reviewed_classes': []}))
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    F.capture_window(tmp_path, row, fetcher=fetcher)
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    F.carry_window(tmp_path, 'lot-777', registry=registry)
    window = tmp_path/'data/scenes/lot-777'
    broken = _break_groups(window)
    before_sha = hashlib.sha256((window/'review.json').read_bytes()).hexdigest()
    untouched = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    rows_before = len(registry.read_text().splitlines())
    tree = _tree(tmp_path)
    result = F.repair_groups(tmp_path, 'lot-777', registry=registry, now='2026-10-01T09:00:00Z')
    assert _tree(tmp_path) == tree, 'no new file: the review and the registry are the only writes'
    assert {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == untouched, 'the source scene is never written'
    repaired = json.loads((window/'review.json').read_text())
    assert repaired['groups'] == [{'id': 'g-a', 'label': 'Shared roof'}] and repaired['revision'] == 1
    assert repaired['updated_by'] == 'scripts.ml.scene_focus carry' and repaired['updated_at'] == '2026-10-01T09:00:00Z' != broken['updated_at']
    assert {k: v for k, v in repaired.items() if k not in ('groups', 'updated_at')} == {k: v for k, v in broken.items() if k not in ('groups', 'updated_at')}
    assert _save_shape_ok(repaired)
    after_sha = hashlib.sha256((window/'review.json').read_bytes()).hexdigest()
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert len(rows) == rows_before+1 and rows[:rows_before] == [json.loads(line) for line in registry.read_text().splitlines()[:rows_before]]
    assert rows[-1] == {'version': 'windows-registry-v1', 'role': 'carry_repair', 'window_scene_id': 'lot-777', 'groups_added': ['g-a'],
                        'at': '2026-10-01T09:00:00Z', 'previous_review_sha256': before_sha, 'new_review_sha256': after_sha}
    assert result['groups_added'] == ['g-a'] and result['dropped_pointers'] == [] and result['revision'] == 1 and result['rows'] == 1
    assert result['previous_review_sha256'] == before_sha and result['new_review_sha256'] == after_sha and result['registry_path'] == str(registry)
    # the counter's hop still reads the registry: the carried rows register the keys, the repair row points at no key
    lineage = r5.load_lineage(tmp_path/'data/reviews/eyes/recapture')
    assert lineage['registry_rows'] == rows_before+1 and r5.registered(lineage, ('qa_source', 'slice-a'), 'lot-777') is True
    assert r5.classify(repaired['objects'][0], repaired['revision']) == ('undecided', 'carried_undecided', 'carried_undecided', None)
    # a repaired review has nothing left to add: skipped, no write, no row
    tree = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    again = F.repair_groups(tmp_path, 'lot-777', registry=registry)
    assert again['skipped'] == 'nothing_to_repair' and {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == tree


def test_repair_groups_refuses_a_saved_or_foreign_or_locked_review_and_writes_nothing(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = _grouped_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    F.capture_window(tmp_path, row, fetcher=fetcher)
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    F.carry_window(tmp_path, 'lot-777', registry=registry)
    window = tmp_path/'data/scenes/lot-777'
    broken = _break_groups(window)
    path = window/'review.json'

    def refused(match):
        tree = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
        with pytest.raises(ValueError, match=match):
            F.repair_groups(tmp_path, 'lot-777', registry=registry)
        assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == tree, 'a refusal writes nothing'

    # the reviewer saved (revision 2): never patched
    path.write_text(json.dumps({**broken, 'revision': 2, 'updated_by': 'local'}, indent=2))
    refused('revision 2')
    # revision 1 written by someone other than the carry
    path.write_text(json.dumps({**broken, 'updated_by': 'local'}, indent=2))
    refused('updated_by')
    # the app is saving this scene right now
    path.write_text(json.dumps(broken, indent=2))
    (window/'review.lock').mkdir()
    refused('saving')
    (window/'review.lock').rmdir()
    # a window without a registry file is refused unless it is a qa_ scratch scene
    with pytest.raises(ValueError, match='registry file is required'):
        F.repair_groups(tmp_path, 'lot-777', registry=None)
    # no review at all
    path.unlink()
    refused('no review.json')
    assert len(registry.read_text().splitlines()) == 5, 'the five carried_source rows only'


def test_cli_carry_repair_groups(tmp_path, capsys):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = _grouped_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    F.capture_window(tmp_path, row, fetcher=fetcher)
    registry = tmp_path/'out/windows-x.jsonl'
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', str(registry)]) == 0
    capsys.readouterr()
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', str(registry), '--repair-groups']) == 3
    assert json.loads(capsys.readouterr().out)['skipped'] == 'nothing_to_repair'
    _break_groups(tmp_path/'data/scenes/lot-777')
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', str(registry), '--repair-groups']) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['groups_added'] == ['g-a'] and printed['revision'] == 1 and len(registry.read_text().splitlines()) == 6
    assert F.main(['carry', '--root', str(tmp_path), '--scene', 'lot-404', '--registry', str(registry), '--repair-groups']) == 1
    assert 'Window not repaired' in capsys.readouterr().err


# --- 2026-10-01 reviewer direction: no cut-offs (bounding-box frame-cut rule; uncarried_source rows never block a carry) ------

def test_a_ring_grazing_the_frame_with_one_vertex_is_frame_cut_and_planned(tmp_path, source):
    """map-806de95f666a01c7/815d84c355fa7b73 on 2026-10-01: one vertex at y = 511 on a 512 px frame; touches_frame (two vertices) said
    not_frame_cut, the reviewer wants no cut-off at all: the bounding-box rule plans it."""
    from scripts.ml.scene_expand import touches_frame
    graze = {**slice_object('graze', 179, 225), 'ring': [[179., 468.], [225., 470.], [225., 500.], [196., 511.], [179., 500.]]}
    source['objects'].append(graze)
    source['model_signature'] = R.content_signature(source)
    assert touches_frame(graze['ring'], [0, 0, 512, 512], 1.) is False, 'the old two-vertex test misses a single grazing vertex'
    assert F.touched_edges(graze['ring'], source) == {'bottom': [(196., 511.)]}
    assert F.touched_edges([[200., 100.], [300., 100.], [300., 140.], [200., 140.]], source) == {}, 'an inner ring is still not frame-cut'
    assert F.touched_edges([[100., -5.], [200., -5.], [200., 40.], [100., 40.]], source) == {}, 'vertices beyond the frame by more than the tolerance are ignored'
    assert F.touched_edges([[100., 0.], [200., 0.], [200., 40.], [100., 40.]], source) == {'top': [(100., 0.), (200., 0.)]}
    write_design(tmp_path)
    result = F.plan(tmp_path, {source['id']: ['graze']}, scenes=[source], offline=True, parcels=parcels(), addresses=[])
    assert result['header']['skipped'] == [] and len(result['rows']) == 1
    subject = result['rows'][0]['subjects'][0]
    assert subject['object_id'] == 'graze' and subject['touched_edges'] == ['bottom']
    assert result['header']['window']['frame_tolerance_px'] == 1.


def test_carry_is_not_blocked_by_an_uncarried_source_row_naming_another_window(tmp_path):
    """The four 2026-10-01 targets planned into another source scene's window got uncarried_source rows there; their own window's
    carry must not read those records as a carry elsewhere (ambiguous_carry counts copies, not records)."""
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source = captured_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    scene, _ = F.capture_window(tmp_path, row, fetcher=fetcher)
    assert scene['frame_transform']['verified'] is True
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({'source_scene_id': 'qa_source', 'object_id': 'slice-a', 'window_scene_id': 'win-elsewhere',
                                    'carried_object_id': None, 'role': 'uncarried_source', 'frame_transform_verified': True}) + '\n')
    out = F.carry_window(tmp_path, 'lot-777', registry=registry, now='2026-10-01T10:00:00Z')
    assert out['rows'] == 1 and [c['id'] for c in out['carried']] == ['slice-a']
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert [(r['window_scene_id'], r['role']) for r in rows] == [('win-elsewhere', 'uncarried_source'), ('lot-777', 'carried_source')]
    # a carried_source row elsewhere still refuses (the existing rule), and the review written above is not touched by the refusal
    shutil.rmtree(tmp_path/'data/scenes/lot-777')
    F.capture_window(tmp_path, row, fetcher=fetcher)
    with registry.open('a') as handle:
        handle.write(json.dumps({'source_scene_id': 'qa_source', 'object_id': 'slice-a', 'window_scene_id': 'lot-777', 'withdrawn': True}) + '\n')
        handle.write(json.dumps({'source_scene_id': 'qa_source', 'object_id': 'slice-a', 'window_scene_id': 'win-other',
                                 'carried_object_id': 'slice-a', 'role': 'carried_source', 'frame_transform_verified': True}) + '\n')
    with pytest.raises(ValueError, match='ambiguous_carry'):
        F.carry_window(tmp_path, 'lot-777', registry=registry)


# --- 2026-10-02 reviewer direction: the frozen slice is taken over by the whole-roof detection (takeover) -------------------

def _box_ring(minx, miny, maxx, maxy):
    return [[float(minx), float(miny)], [float(maxx), float(miny)], [float(maxx), float(maxy)], [float(minx), float(maxy)]]


def _detection(object_id, ring, **extra):
    return {'id': object_id, 'kind': 'building', 'label': 'Building', 'ring': ring, 'status': 'candidate', 'geometry_reviewed': False,
            'source': 'sam3-text-onnx', 'model': {'area_m2': 80., 'height_verified': False}, **extra}


def _lineage_ok(review):
    """The replacement-history rules of the review app's save validator (src/lib/scene-store.ts validateSceneReview ->
    src/lib/scene-instances.ts lineageIssue): derived_from / replacement_ids name existing distinct other objects (at most 64 / 8);
    superseded exactly when replacement_ids is non-empty and never geometry_reviewed; every link has its reciprocal; no loop."""
    objects = {o['id']: o for o in review['objects']}
    for o in review['objects']:
        for key, cap in (('derived_from', 64), ('replacement_ids', 8)):
            links = o.get(key)
            if links is not None:
                assert isinstance(links, list) and links and len(links) <= cap and len(set(links)) == len(links), (o['id'], key, links)
                assert all(isinstance(i, str) and i != o['id'] and i in objects for i in links), (o['id'], key, links)
        assert (o['status'] == 'superseded') == bool(o.get('replacement_ids')), (o['id'], o['status'], o.get('replacement_ids'))
        assert not (o['status'] == 'superseded' and o.get('geometry_reviewed')), o['id']
        assert all(o['id'] in (objects[i].get('replacement_ids') or []) for i in o.get('derived_from') or []), (o['id'], 'derived_from')
        assert all(o['id'] in (objects[i].get('derived_from') or []) for i in o.get('replacement_ids') or []), (o['id'], 'replacement_ids')
    visiting, done = set(), set()

    def cycle(i):
        if i in visiting:
            return True
        if i in done:
            return False
        visiting.add(i)
        if any(cycle(j) for j in objects[i].get('replacement_ids') or []):
            return True
        visiting.discard(i)
        done.add(i)
        return False
    assert not any(cycle(i) for i in objects), 'replacement history loop'
    return True


def test_takeover_plan_pairs_each_copy_with_its_single_covering_detection_inside_the_frame():
    """Pure: copies (review objects with carried_from) against the model's building detections on a 1024 px frame."""
    def copy(object_id, ring, **extra):
        return {**_detection(object_id, ring), 'status': 'needs_adjustment',
                'carried_from': {'scene_id': 'map-src', 'object_id': object_id, 'method': 'georeferenced-affine'}, **extra}
    copies = [copy('c1', _box_ring(100, 100, 200, 140)),           # whole roof r1 covers it: taken over
              copy('c5', _box_ring(210, 100, 290, 140)),           # the same roof r1: one detection, two slices
              copy('c2', _box_ring(400, 400, 500, 440)),           # r2 covers exactly half: below 0.8
              copy('c3', _box_ring(100, 300, 200, 340)),           # r3 covers all of it and r4 0.6 of it: two detections
              copy('c4', _box_ring(700, 700, 800, 740)),           # r5 covers it but reaches the right frame edge
              copy('c6', _box_ring(600, 100, 700, 140)),           # only a tree covers it: no building detection
              copy('c7', _box_ring(600, 300, 700, 340)),           # r6 covers it but the review already decided r6
              copy('c8', _box_ring(600, 500, 700, 540)),           # r7 covers it but the review does not hold r7
              copy('c9', _box_ring(600, 700, 700, 740), status='superseded', replacement_ids=['r1'])]
    detections = [_detection('r1', _box_ring(90, 90, 300, 150)), _detection('r2', _box_ring(400, 400, 450, 440)),
                  _detection('r3', _box_ring(90, 290, 210, 350)), _detection('r4', _box_ring(90, 290, 160, 350)),
                  _detection('r5', _box_ring(690, 690, 1024, 800)), {**_detection('t1', _box_ring(590, 90, 710, 150)), 'kind': 'tree'},
                  _detection('r6', _box_ring(590, 290, 710, 350)), _detection('r7', _box_ring(590, 490, 710, 550))]
    review = {'objects': [*copies, *[d for d in detections if d['id'] != 'r7'], {**_detection('r6', detections[6]['ring']), 'status': 'confirmed'}]}
    review['objects'] = [o for o in review['objects'] if not (o['id'] == 'r6' and o['status'] == 'candidate')]
    latest = {'image_width': 1024, 'image_height': 1024, 'objects': detections}
    plan = F.takeover_plan(review, latest)
    assert plan['copies'] == 9
    assert plan['pairs'] == [{'copy_id': 'c1', 'detection_id': 'r1', 'coverage': 1.0, 'frozen_key': ['map-src', 'c1']},
                             {'copy_id': 'c5', 'detection_id': 'r1', 'coverage': 1.0, 'frozen_key': ['map-src', 'c5']}]
    assert plan['replaces'] == {'r1': ['c1', 'c5']} and plan['detections_with_several_slices'] == ['r1']
    left = {row['copy_id']: row for row in plan['left_alone']}
    assert set(left) == {'c2', 'c3', 'c4', 'c6', 'c7', 'c8', 'c9'}
    assert left['c2']['reason'] == 'below_cover' and left['c2']['best'] == {'detection_id': 'r2', 'coverage': .5}
    assert left['c3']['reason'] == 'shared_cover' and left['c3']['best'] == {'detection_id': 'r3', 'coverage': 1.0}
    assert left['c3']['others'] == [{'detection_id': 'r4', 'coverage': .6}]
    assert left['c4']['reason'] == 'detection_touches_frame' and left['c4']['best']['detection_id'] == 'r5' and left['c4']['edges'] == ['right']
    assert left['c6']['reason'] == 'no_detection' and left['c6']['best'] is None
    assert left['c7']['reason'] == 'detection_not_candidate' and left['c7']['best']['detection_id'] == 'r6'
    assert left['c8']['reason'] == 'detection_not_in_review' and left['c8']['best']['detection_id'] == 'r7'
    assert left['c9']['reason'] == 'already_superseded'
    # the thresholds are the module's pre-registered constants, read not typed
    assert F.TAKEOVER == {'cover_min': .8, 'other_cover_max': .5, 'frame_tolerance_px': 1.0}
    # a vertex exactly 1 px inside the edge is within tolerance: the detection touches the frame
    assert F.touching_frame(_box_ring(1, 10, 100, 100), 1024, 1024) == ['left']
    assert F.touching_frame(_box_ring(1.5, 1.5, 1022.5, 1022.5), 1024, 1024) == []
    assert F.touching_frame(_box_ring(-5, 10, 100, 1023.5), 1024, 1024) == ['left', 'bottom']


def _forwarded_window(tmp_path, fetcher):
    """A carried window (the five slices of _grouped_source) whose detector then ran and whose review was carried forward the way
    the 2026-10-02 carry-forward did: the model's building detections sit in latest.json (signature recomputed) and in the review
    as candidates, the review serialised like the JS writer (indent 2, no ASCII escaping, no trailing newline), updated_by
    'scripts.ml.scene_focus carry-forward'. Detections: roof-1 holds slices a and b whole; roof-2 covers half of c; roof-3 holds d
    but reaches the top edge; roof-4 holds e whole while roof-5 covers 0.6 of e."""
    source = _grouped_source(tmp_path, fetcher)
    row = one_row(tmp_path, source, [source], parcels=WHOLE, addresses=[address(777, 0, 35)])
    F.capture_window(tmp_path, row, fetcher=fetcher)
    registry = tmp_path/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
    F.carry_window(tmp_path, 'lot-777', registry=registry)
    window = tmp_path/'data/scenes/lot-777'
    review = json.loads((window/'review.json').read_text())
    rings = {o['id']: np.array(o['ring']) for o in review['objects']}

    def bounds(*ids):
        pts = np.concatenate([rings[i] for i in ids])
        return pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()
    ax0, ay0, ax1, ay1 = bounds('slice-a', 'slice-b')
    cx0, cy0, cx1, cy1 = bounds('slice-c')
    dx0, dy0, dx1, dy1 = bounds('slice-d')
    ex0, ey0, ex1, ey1 = bounds('slice-e')
    detections = [_detection('roof-1', _box_ring(ax0-8, ay0-8, ax1+8, ay1+8), notes='Area 7 m² roof, whole'),
                  _detection('roof-2', _box_ring(cx0, cy0, (cx0+cx1)/2, cy1)),
                  _detection('roof-3', _box_ring(dx0-8, 0, dx1+8, dy1+8)),
                  _detection('roof-4', _box_ring(ex0-6, ey0-6, ex1+6, ey1+6)),
                  _detection('roof-5', _box_ring(ex0+(ex1-ex0)*.4, ey0-6, ex1+6, ey1+6))]
    latest = json.loads((window/'latest.json').read_text())
    latest['objects'] = [*latest['objects'], *detections]
    latest['model_signature'] = R.content_signature(latest)
    (window/'latest.json').write_text(json.dumps(latest))
    review['objects'] = [*review['objects'], *[json.loads(json.dumps(d)) for d in detections]]
    review['model_signature'] = latest['model_signature']
    review['updated_by'] = 'scripts.ml.scene_focus carry-forward'
    (window/'review.json').write_bytes(json.dumps(review, indent=2, ensure_ascii=False).encode())
    return source, window, registry, review


def test_takeover_supersedes_the_covered_copies_onto_their_detection_and_changes_nothing_else(tmp_path):
    from scripts.ml.training.sam3_mac import r5_precision as r5
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source, window, registry, before = _forwarded_window(tmp_path, fetcher)
    before_bytes = (window/'review.json').read_bytes()
    assert '\\u00b2' not in before_bytes.decode() and 'm²' in before_bytes.decode(), 'the JS writer does not escape non-ASCII'
    folder = tmp_path/'data/scenes/qa_source'
    untouched = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    rows_before = len(registry.read_text().splitlines())
    # dry run: the plan, nothing written
    tree = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    dry = F.takeover_window(tmp_path, 'lot-777', registry=registry)
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == tree, 'a dry run writes nothing'
    assert dry['written'] is False and dry['copies'] == 5 and dry['serialisation_roundtrip'] is True
    assert [(p['copy_id'], p['detection_id']) for p in dry['pairs']] == [('slice-a', 'roof-1'), ('slice-b', 'roof-1')]
    assert all(p['coverage'] == 1.0 and p['frozen_key'] == ['qa_source', p['copy_id']] for p in dry['pairs'])
    assert {row['copy_id']: row['reason'] for row in dry['left_alone']} == {'slice-c': 'below_cover', 'slice-d': 'detection_touches_frame',
                                                                             'slice-e': 'shared_cover'}
    assert dry['replaces'] == {'roof-1': ['slice-a', 'slice-b']} and dry['detections_with_several_slices'] == ['roof-1']
    # write
    result = F.takeover_window(tmp_path, 'lot-777', registry=registry, write=True, now='2026-10-02T01:00:00Z')
    added = [p for p in {str(p) for p in tmp_path.rglob('*') if p.is_file()} if p not in tree]
    assert added == [], 'the review and the registry are the only writes'
    assert {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == untouched, 'the source scene is never written'
    after_bytes = (window/'review.json').read_bytes()
    after = json.loads(after_bytes)
    assert after['revision'] == 1 and after['model_signature'] == before['model_signature'] and after['groups'] == before['groups']
    assert after['updated_by'] == 'scripts.ml.scene_focus takeover' and after['updated_at'] == '2026-10-02T01:00:00Z'
    objects = {o['id']: o for o in after['objects']}
    for cid in ('slice-a', 'slice-b'):
        copy, was = objects[cid], next(o for o in before['objects'] if o['id'] == cid)
        assert copy['status'] == 'superseded' and copy['replacement_ids'] == ['roof-1'] and copy['carried_from'] == was['carried_from']
        assert {k: v for k, v in copy.items() if k not in ('status', 'replacement_ids')} == {k: v for k, v in was.items() if k != 'status'}
        assert r5.classify(copy, after['revision']) == ('excluded', 'ui', 'superseded', None)
    roof = objects['roof-1']
    assert roof['replaces'] == ['slice-a', 'slice-b'] == roof['derived_from'] and roof['status'] == 'candidate'
    assert {k: v for k, v in roof.items() if k not in ('replaces', 'derived_from')} == next(o for o in before['objects'] if o['id'] == 'roof-1')
    assert r5.classify(roof, after['revision']) == ('undecided', 'ui', None, None)
    for oid in ('slice-c', 'slice-d', 'slice-e', 'roof-2', 'roof-3', 'roof-4', 'roof-5'):
        assert objects[oid] == next(o for o in before['objects'] if o['id'] == oid), oid
    assert [o['id'] for o in after['objects']] == [o['id'] for o in before['objects']], 'order and membership unchanged'
    # every other byte identical: the same edits on the previous document serialise to the written file
    expected = json.loads(before_bytes.decode())
    for o in expected['objects']:
        if o['id'] in ('slice-a', 'slice-b'):
            o['status'], o['replacement_ids'] = 'superseded', ['roof-1']
        if o['id'] == 'roof-1':
            o['replaces'], o['derived_from'] = ['slice-a', 'slice-b'], ['slice-a', 'slice-b']
    expected['updated_at'], expected['updated_by'] = '2026-10-02T01:00:00Z', 'scripts.ml.scene_focus takeover'
    assert json.dumps(expected, indent=2, ensure_ascii=False).encode() == after_bytes
    assert _save_shape_ok(after) and _lineage_ok(after)
    # the registry row
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert len(rows) == rows_before+1 and result['rows'] == 1 and result['registry_path'] == str(registry)
    row = rows[-1]
    assert row['version'] == 'windows-registry-v1' and row['role'] == 'takeover' and row['window_scene_id'] == 'lot-777'
    assert row['pairs'] == dry['pairs'] and row['left_alone'] == dry['left_alone'] and row['at'] == '2026-10-02T01:00:00Z'
    assert row['previous_review_sha256'] == hashlib.sha256(before_bytes).hexdigest() == result['previous_review_sha256']
    assert row['new_review_sha256'] == hashlib.sha256(after_bytes).hexdigest() == result['new_review_sha256']
    assert row['rule'] == F.TAKEOVER and row['model_signature'] == before['model_signature']
    assert result['written'] is True and result['revision'] == 1 and result['updated_by'] == 'scripts.ml.scene_focus takeover'
    # the counter's lineage still reads the registry; the carried rows still register the keys
    lineage = r5.load_lineage(tmp_path/'data/reviews/eyes/recapture')
    assert lineage['registry_rows'] == rows_before+1 and r5.registered(lineage, ('qa_source', 'slice-a'), 'lot-777') is True
    # a taken-over review is no longer the carry's: a second run is refused by name and writes nothing
    tree = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='updated_by'):
        F.takeover_window(tmp_path, 'lot-777', registry=registry, write=True)
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == tree


def test_takeover_refuses_by_name_and_writes_nothing(tmp_path):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source, window, registry, review = _forwarded_window(tmp_path, fetcher)
    path = window/'review.json'
    good = path.read_bytes()

    def refused(match, *, write=True):
        tree = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
        with pytest.raises(ValueError, match=match):
            F.takeover_window(tmp_path, 'lot-777', registry=registry, write=write)
        assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == tree, 'a refusal writes nothing'

    def put(**changes):
        path.write_bytes(json.dumps({**review, **changes}, indent=2, ensure_ascii=False).encode())
    # the reviewer saved (revision 2): never touched, in a dry run too
    put(revision=2, updated_by='local')
    refused('revision 2')
    refused('revision 2', write=False)
    # revision 1 by someone other than the carry / carry-forward
    put(updated_by='local')
    refused('updated_by')
    # the model moved on (or the review names another model)
    put(model_signature='f'*64)
    refused('model_signature')
    # the app is saving this scene right now
    path.write_bytes(good)
    (window/'review.lock').mkdir()
    refused('saving')
    (window/'review.lock').rmdir()
    assert F.takeover_window(tmp_path, 'lot-777', registry=registry)['written'] is False, 'a dry run ignores the lock'
    # a review that does not round-trip through the carry-forward serialisation would change more than the fields: refused on write
    path.write_bytes(json.dumps(review, indent=4).encode())
    refused('round-trip')
    assert F.takeover_window(tmp_path, 'lot-777', registry=registry)['serialisation_roundtrip'] is False
    path.write_bytes(good)
    # a window without a registry file is refused unless it is a qa_ scratch scene
    with pytest.raises(ValueError, match='registry file is required'):
        F.takeover_window(tmp_path, 'lot-777', registry=None, write=True)
    # no review at all
    path.unlink()
    refused('no review.json')
    assert len(registry.read_text().splitlines()) == 5, 'the five carried_source rows only'


def test_cli_takeover_dry_run_all_and_write(tmp_path, capsys):
    write_design(tmp_path)
    fetcher = TexturedFetcher()
    source, window, registry, review = _forwarded_window(tmp_path, fetcher)
    assert F.window_scenes(tmp_path) == ['lot-777'], 'only frame-cut windows (window_frame), never the source'
    tree = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert F.main(['takeover', '--root', str(tmp_path), '--all', '--registry', str(registry)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['mode'] == 'dry_run' and [w['window_scene_id'] for w in printed['windows']] == ['lot-777']
    totals = printed['totals']
    assert totals == {'windows': 1, 'planned': 1, 'refused': 0, 'copies': 5, 'taken_over': 2, 'left_alone': 3,
                      'left_alone_by_reason': {'below_cover': 1, 'detection_touches_frame': 1, 'shared_cover': 1},
                      'detections_with_several_slices': 1, 'refused_by_reason': {}, 'windows_with_pairs': 1}
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == tree, 'the dry run writes nothing'
    # write one window; a second write is refused by name (exit 1) and recorded
    assert F.main(['takeover', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', str(registry), '--write']) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['mode'] == 'write' and printed['windows'][0]['written'] is True and len(registry.read_text().splitlines()) == 6
    assert json.loads((window/'review.json').read_text())['updated_by'] == 'scripts.ml.scene_focus takeover'
    assert F.main(['takeover', '--root', str(tmp_path), '--scene', 'lot-777', '--registry', str(registry), '--write']) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed['totals']['refused'] == 1 and 'updated_by' in printed['windows'][0]['refused']
    assert len(registry.read_text().splitlines()) == 6


def test_write_takeover_amendment_appends_one_dated_entry_to_window_json(tmp_path):
    write_design(tmp_path)
    path = tmp_path/F.WINDOW_PATH
    raw = path.read_bytes()
    before = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match='expected'):
        F.write_takeover_amendment(tmp_path, expected_previous_sha256='0'*64)
    assert path.read_bytes() == raw
    result = F.write_takeover_amendment(tmp_path, expected_previous_sha256=before, now='2026-10-02T02:00:00Z')
    after = path.read_bytes()
    assert result['sha256_before'] == before and result['sha256_after'] == hashlib.sha256(after).hexdigest() != before
    rule, sha = F.load_window_rule(tmp_path)
    assert sha == result['sha256_after'] and len(rule['amendments']) == 1
    entry = rule['amendments'][0]
    assert entry == result['amendment']
    assert entry['field'] == 'takeover_rule' and entry['previous_sha256'] == before == entry['window_sha256_before']
    assert entry['thresholds_changed'] is False and entry['constants_changed'] is False and entry['at'] == '2026-10-02T02:00:00Z'
    assert entry['takeover'] == F.TAKEOVER and 'derived_from' in entry['change'] and 'ambiguous_takeover' in entry['count']
    assert 'organge boundig box' in entry['reason'], 'the reviewer direction, verbatim'
    assert set(entry['code_sha256']) == {'scripts/ml/scene_focus.py'}
    assert (json.dumps(rule, indent=1, sort_keys=True)+'\n').encode() == after, 'the register convention (indent 1, sorted keys)'
    assert {k: v for k, v in rule.items() if k != 'amendments'} == {k: v for k, v in json.loads(raw).items() if k != 'amendments'}, 'body untouched'
    # pre-registered once: a second entry is refused and nothing changes
    with pytest.raises(ValueError, match='already'):
        F.write_takeover_amendment(tmp_path)
    assert path.read_bytes() == after
    # the module's rule record carries the takeover rule for any future dated file
    assert F.window_rule(F.load_design(tmp_path))['takeover_rule']['constants'] == F.TAKEOVER
