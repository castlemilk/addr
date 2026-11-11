import hashlib
import io
import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml import unit_split_candidates as units
from shapely.geometry import Polygon, box, mapping

# 0.25 m/px, origin at the image centre, y up in metres (same frame as the diagnostics tests).
P2M = [[.25, 0], [0, -.25], [-32, 32]]
M2P = [[4, 0], [0, -4], [128, 128]]


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _parcel(lot, polygon, plan='PS1', pfi=None):
    return {'type': 'Feature', 'geometry': mapping(polygon), 'properties': {
        'pfi': pfi or f'{lot}-{plan}', 'spi': f'{lot}\\{plan}', 'plan': plan, 'lot': lot,
        'status': 'approved', 'horizontal_uncertainty_m': .5}}


@pytest.fixture
def site(monkeypatch):
    # Parcel coordinates are given directly in image pixels for the test.
    monkeypatch.setattr(units, 'image_projection', lambda _: lambda x, y, z=None: (x, y))
    image = Image.fromarray(np.full((256, 256, 3), 90, np.uint8), 'RGB')
    ring = [[40, 60], [200, 60], [200, 140], [40, 140]]                 # 40 m x 20 m = 800 m2
    hole = [[130, 90], [150, 90], [150, 110], [130, 110]]              # 5 m x 5 m = 25 m2 inside lot 2
    obj = {'id': 'row', 'kind': 'building', 'label': 'Building', 'status': 'candidate', 'source': 'sam3-text-onnx',
           'ring': ring, 'holes': [hole], 'model': {'height_m': 3.2}}
    scene = {'id': 'synthetic-units', 'title': 'synthetic', 'schema_version': 1, 'model_signature': 'sig-1',
             'image_sha256': hashlib.sha256(_png(image)).hexdigest(), 'image_width': 256, 'image_height': 256,
             'pixel_to_meters': P2M, 'meters_to_pixel': M2P, 'centroid': {'lon': 145.31, 'lat': -37.79},
             'region': {'bounds': [145.30, -37.80, 145.32, -37.78]}, 'objects': [obj]}
    # Three numbered lots of 60 px (15 m) each across the row, a common-property strip to the east.
    features = [_parcel(str(i+1), box(40+i*60, 20, 100+i*60, 180)) for i in range(3)]
    features.append(_parcel('CM1', box(220, 20, 250, 180)))
    parcels = {'type': 'FeatureCollection', 'features': features, 'bounds': scene['region']['bounds'],
               'source': {'name': 'Vicmap Parcel'}, 'retrieved_at': '2026-09-26T00:00:00+00:00'}
    return scene, obj, parcels, image


def _write_scene(root, scene, image):
    folder = root/'data/scenes'/scene['id']
    folder.mkdir(parents=True)
    (folder/f"{scene['image_sha256']}.png").write_bytes(_png(image))
    (folder/'latest.json').write_text(json.dumps(scene))


def test_split_produces_per_lot_candidates_with_holes_and_a_common_property_remainder(site):
    scene, obj, parcels, _ = site
    report = units.split_outline(scene, obj, parcels)
    assert report['version'] == units.VERSION == 'unit-split-candidates-v2'
    assert report['policy'] == 'cadastral cuts are allocation edges, never physical folds'
    assert report['status'] == 'split'
    assert [lot['lot'] for lot in report['lots']] == ['1', '2', '3']
    # Lot 1: 15 m x 20 m = 300 m2; lot 2: 300 m2 minus the 25 m2 hole; lot 3: 10 m x 20 m = 200 m2.
    assert [lot['area_m2'] for lot in report['lots']] == pytest.approx([300, 275, 200], abs=.5)
    assert all(lot['plan'] == 'PS1' and lot['pfi'] and len(lot['ring_px']) >= 4 and len(lot['ring_m']) == len(lot['ring_px']) for lot in report['lots'])
    assert report['lots'][1]['holes_px'] and Polygon(report['lots'][1]['holes_px'][0]).area == pytest.approx(400, abs=1)
    assert not report['lots'][0]['holes_px'] and not report['lots'][2]['holes_px']
    # Every part of the outline sits in a numbered lot here, so nothing is left as common property.
    assert report['common_property']['area_m2'] == pytest.approx(0, abs=.5) and report['common_property']['rings_px'] == []
    assert report['outline_area_m2'] == pytest.approx(775, abs=.5)
    assert report['coverage_share'] == pytest.approx(1, abs=1e-3)
    # ring_px is the inverse of ring_m under the saved frame.
    lot = report['lots'][0]
    back = (np.c_[np.asarray(lot['ring_m']), np.ones(len(lot['ring_m']))] @ np.asarray(M2P))
    assert np.allclose(back, np.asarray(lot['ring_px']), atol=1e-6)
    assert report['parcel_source']['retrieved_at'] == parcels['retrieved_at']
    assert 'import' in report['import_instructions'].lower()


def test_remainder_outside_numbered_lots_is_reported_not_allocated(site):
    scene, obj, parcels, _ = site
    parcels['features'] = parcels['features'][:2]+[parcels['features'][3]]      # drop lot 3
    report = units.split_outline(scene, obj, parcels)
    assert [lot['lot'] for lot in report['lots']] == ['1', '2']
    assert report['coverage_share'] == pytest.approx(575/775, abs=1e-3)
    assert report['common_property']['area_m2'] == pytest.approx(200, abs=.5)
    assert report['common_property']['rings_px'] and report['common_property']['lots'] == []
    assert report['status'] == 'split'


def test_slivers_below_thirty_square_metres_are_dropped_and_listed(site):
    scene, obj, parcels, _ = site
    parcels['features'].append(_parcel('9', box(196, 40, 230, 160), plan='PS1'))    # 1 m x 20 m = 20 m2 in the outline
    report = units.split_outline(scene, obj, parcels)
    assert [lot['lot'] for lot in report['lots']] == ['1', '2', '3']
    assert report['dropped_pieces'] == [{'lot': '9', 'plan': 'PS1', 'area_m2': pytest.approx(20, abs=.5)}]


def test_stacked_strata_lots_have_no_cadastral_cuts(site):
    scene, obj, parcels, _ = site
    stacked = box(30, 30, 210, 170)
    parcels['features'] = [_parcel(str(i+1), stacked, plan='PS9') for i in range(4)]+[_parcel('CM1', stacked, plan='PS9')]
    report = units.split_outline(scene, obj, parcels)
    assert report['status'] == 'no_cadastral_cuts'
    assert report['lots'] == [] and report['coverage_share'] == 0
    assert report['stacked_lot_groups'] == [{'plan': 'PS9', 'lots': ['1', '2', '3', '4', 'CM1'], 'count': 5}]
    assert 'stacked' in report['reason'].lower()


def test_stacked_lots_surface_saved_roof_parts_as_physical_not_cadastral_candidates(site):
    scene, obj, parcels, _ = site
    stacked = box(30, 30, 210, 170)
    parcels['features'] = [_parcel(str(i+1), stacked, plan='PS9') for i in range(2)]
    obj = {**obj, 'boundary_refinement': {'assembly': {'components': [
        {'id': 'west', 'ring': [[40, 60], [120, 60], [120, 140], [40, 140]], 'holes': [], 'score': .7},
        {'id': 'east', 'ring': [[120, 60], [200, 60], [200, 140], [120, 140]], 'holes': [], 'score': .6},
        {'id': 'sliver', 'ring': [[40, 60], [44, 60], [44, 64], [40, 64]], 'holes': [], 'score': .5}]}}}
    report = units.split_outline(scene, obj, parcels)
    assert report['status'] == 'no_cadastral_cuts' and report['lots'] == []
    assert [part['id'] for part in report['assembly_components']] == ['west', 'east'], 'parts under 30 m2 are not candidates'
    assert [part['area_m2'] for part in report['assembly_components']] == pytest.approx([400, 400], abs=.5)
    assert all('not a cadastral lot' in part['source'] and len(part['ring_px']) == 4 for part in report['assembly_components'])
    assert '2 physical components' in report['reason']
    assert units.split_outline(scene, {**obj, 'boundary_refinement': {}}, parcels)['assembly_components'] == []


def test_outline_touching_no_lot_is_an_honest_negative(site):
    scene, obj, parcels, _ = site
    obj = {**obj, 'ring': [[2, 2], [20, 2], [20, 12], [2, 12]], 'holes': []}
    report = units.split_outline(scene, obj, parcels)
    assert report['status'] == 'no_lots' and report['lots'] == [] and report['coverage_share'] == 0


def test_run_writes_only_under_reviews_eyes_units_and_reuses_the_parcel_cache(site, tmp_path):
    scene, obj, parcels, image = site
    _write_scene(tmp_path, scene, image)
    before = (tmp_path/'data/scenes'/scene['id']/'latest.json').read_bytes()
    calls = []

    def fetch(bounds):
        calls.append(bounds)
        return parcels

    first = units.run(tmp_path, scene['id'], obj['id'], fetch=fetch)
    second = units.run(tmp_path, scene['id'], obj['id'], fetch=fetch)
    assert calls == [scene['region']['bounds']], 'one public lookup, then the cache'
    folder = tmp_path/'data/reviews/eyes/units'
    assert (folder/f"parcels-{scene['id']}.json").exists()
    report_path = folder/scene['id']/f"{obj['id']}.json"
    sheet_path = folder/scene['id']/f"{obj['id']}.png"
    assert report_path.exists() and sheet_path.exists()
    saved = json.loads(report_path.read_text())
    assert saved['version'] == units.VERSION and saved['status'] == 'split' and saved['image_sha256'] == scene['image_sha256']
    assert saved['sheet'] == str(sheet_path.relative_to(tmp_path))
    assert first['lots'] == second['lots'] == saved['lots']
    assert (tmp_path/'data/scenes'/scene['id']/'latest.json').read_bytes() == before
    assert not (tmp_path/'data/scene-parcels').exists()
    assert set(p.name for p in (tmp_path/'data/scenes'/scene['id']).iterdir()) == {'latest.json', f"{scene['image_sha256']}.png"}
    with Image.open(sheet_path) as sheet:
        assert sheet.size[0] > 200 and sheet.size[1] > 100


def test_cache_bound_to_the_image_identity_is_not_reused_for_another_capture(site, tmp_path):
    scene, obj, parcels, image = site
    _write_scene(tmp_path, scene, image)
    calls = []

    def fetch(bounds):
        calls.append(bounds)
        return parcels

    units.load_parcels(tmp_path, scene, fetch=fetch)
    other = {**scene, 'image_sha256': '0'*64}
    units.load_parcels(tmp_path, other, fetch=fetch)
    assert len(calls) == 2


def test_cli_filters_feedback_by_issue_and_reports(site, tmp_path, capsys, monkeypatch):
    scene, obj, parcels, image = site
    _write_scene(tmp_path, scene, image)
    monkeypatch.setattr(units, 'fetch_parcels', lambda bounds: parcels)
    feedback = tmp_path/'feedback.json'
    feedback.write_text(json.dumps({'items': [
        {'scene_id': scene['id'], 'object_id': obj['id'], 'issues': ['units-should-split']},
        {'scene_id': scene['id'], 'object_id': 'other', 'issues': ['tree-as-building']}]}))
    assert units.main(['--root', str(tmp_path), '--feedback', str(feedback), '--issue', 'units-should-split']) == 0
    out = capsys.readouterr().out
    assert 'row' in out and '3 lots' in out and 'Model estimate, not accuracy' in out
    assert (tmp_path/'data/reviews/eyes/units'/scene['id']/f"{obj['id']}.json").exists()
    assert not (tmp_path/'data/reviews/eyes/units'/scene['id']/'other.json').exists()


def test_missing_object_is_reported_as_an_error_row_not_a_crash(site, tmp_path, capsys, monkeypatch):
    scene, obj, parcels, image = site
    _write_scene(tmp_path, scene, image)
    monkeypatch.setattr(units, 'fetch_parcels', lambda bounds: parcels)
    assert units.main(['--root', str(tmp_path), '--objects', f"{scene['id']}:missing"]) == 1
    assert 'missing' in capsys.readouterr().out


def test_stacked_group_beside_a_neighbouring_lot_is_one_candidate_not_one_per_stacked_lot(site):
    scene, obj, parcels, _ = site
    stacked = box(30, 30, 160, 170)                                          # 30 m of the 40 m row
    parcels['features'] = [_parcel(str(i+1), stacked, plan='PS9') for i in range(4)]+[_parcel('CM1', stacked, plan='PS9')]
    parcels['features'].append(_parcel('7', box(160, 30, 260, 170), plan='LP2'))   # the neighbour, 10 m of the row
    report = units.split_outline(scene, obj, parcels)
    assert report['status'] == 'split'
    assert [(lot['lot'], lot['plan'], lot.get('stacked')) for lot in report['lots']] == [('1, 2, 3, 4', 'PS9', True), ('7', 'LP2', False)]
    assert report['lots'][0]['lots'] == ['1', '2', '3', '4'] and report['lots'][1]['lots'] == ['7']
    assert [lot['area_m2'] for lot in report['lots']] == pytest.approx([600-25, 200], abs=.5)
    assert report['coverage_share'] == pytest.approx(1, abs=1e-3), 'stacked lots are allocated once, never once per lot'
    assert report['stacked_lot_groups'] == [{'plan': 'PS9', 'lots': ['1', '2', '3', '4', 'CM1'], 'count': 5}]
    assert 'no unit cuts' in report['reason'].lower()


def test_two_stacked_plans_side_by_side_give_one_candidate_each(site):
    scene, obj, parcels, _ = site
    parcels['features'] = [_parcel(str(i+1), box(30, 30, 120, 170), plan='PS9') for i in range(2)] \
        + [_parcel(str(i+1), box(120, 30, 210, 170), plan='PS8') for i in range(3)]
    report = units.split_outline(scene, obj, parcels)
    assert [(lot['lot'], lot['plan']) for lot in report['lots']] == [('1, 2, 3', 'PS8'), ('1, 2', 'PS9')] \
        or [(lot['lot'], lot['plan']) for lot in report['lots']] == [('1, 2', 'PS9'), ('1, 2, 3', 'PS8')]
    assert report['coverage_share'] == pytest.approx(1, abs=1e-3)
    assert all(lot['stacked'] for lot in report['lots'])


def test_overlapping_cadastral_lots_do_not_inflate_coverage_above_one(site):
    scene, obj, parcels, _ = site
    parcels['features'] = [_parcel('1', box(40, 20, 140, 180)), _parcel('2', box(100, 20, 200, 180))]   # 10 m overlap
    report = units.split_outline(scene, obj, parcels)
    assert [lot['lot'] for lot in report['lots']] == ['1', '2']
    assert report['coverage_share'] <= 1 and report['coverage_share'] == pytest.approx(1, abs=1e-3)
    assert report['overlap_m2'] == pytest.approx(187.5, abs=.5), '10 m x 20 m overlap minus the 12.5 m2 of the hole inside it'


def test_degenerate_outline_is_reported_not_raised(site):
    scene, obj, parcels, _ = site
    report = units.split_outline(scene, {**obj, 'ring': [[1, 1], [2, 2]], 'holes': []}, parcels)
    assert report['status'] == 'invalid_outline' and report['lots'] == [] and report['coverage_share'] == 0
    assert report['outline_area_m2'] == 0


def test_report_is_written_even_when_the_sheet_cannot_be_rendered(site, tmp_path):
    scene, obj, parcels, image = site
    obj['ring'] = [[300, 300], [340, 300], [340, 340], [300, 340]]          # wholly outside the 256 px image
    _write_scene(tmp_path, scene, image)
    report = units.run(tmp_path, scene['id'], obj['id'], fetch=lambda bounds: parcels)
    folder = tmp_path/'data/reviews/eyes/units'/scene['id']
    assert (folder/f"{obj['id']}.json").exists()
    assert report['sheet'] is None and 'sheet_error' in report
    assert not (folder/f"{obj['id']}.png").exists()


def test_version_is_recorded_and_bumped_for_the_grouped_allocation_schema(site):
    scene, obj, parcels, _ = site
    report = units.split_outline(scene, obj, parcels)
    assert report['version'] == units.VERSION == 'unit-split-candidates-v2'
    assert all(lot['lots'] == [lot['lot']] and lot['stacked'] is False for lot in report['lots'])
