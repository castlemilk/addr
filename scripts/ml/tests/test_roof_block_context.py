import copy
import json
from pathlib import Path

import numpy as np
import pytest
from scripts.ml import roof_block_context as block
from scripts.ml.roof_boundary import regularize_eaves
from scripts.ml.roof_family_review import straight_flat_outline
from shapely.affinity import rotate
from shapely.geometry import Polygon, box, mapping


def roof(identity, polygon):
    return {'id': identity, 'kind': 'building', 'ring': list(polygon.exterior.coords[:-1]),
            'source': 'sam3-text-onnx', 'status': 'candidate', 'geometry_reviewed': False}


def parcel(lot, polygon, plan='PS1'):
    return {'type': 'Feature', 'geometry': mapping(polygon), 'properties': {
        'pfi': lot+plan, 'plan': plan, 'lot': lot, 'status': 'approved', 'horizontal_uncertainty_m': 1.5}}


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(block, 'image_projection', lambda _: lambda x, y, z=None: (x, y))
    objects = [roof('west', box(12, 12, 33, 148)), roof('east', box(57, 12, 78, 148)),
               roof('neighbour', box(105, 12, 125, 148))]
    scene = {'image_width': 200, 'image_height': 200, 'objects': objects,
             'pixel_to_meters': [[.1, 0], [0, .1], [0, 0]]}
    features = [parcel(str(i+1+row*4), box(x, 10+i*35, x+25, 45+i*35))
                for row, x in enumerate([10, 55]) for i in range(4)]
    features += [parcel('CM1', box(35, 10, 55, 150)), parcel('9', box(100, 10, 130, 150))]
    return scene, {'features': features, 'bounds': [0, 0, 200, 200]}


def test_common_property_groups_both_roof_rows_without_filling_driveway(site):
    scene, parcels = site
    original = copy.deepcopy(scene)
    result = block.block_context(scene, scene['objects'][0], parcels)
    assert result['status'] == 'grouped' and result['numbered_lots'] == 8
    assert {m['object_id'] for m in result['members']} == {'west', 'east'}
    assert not result['roof_surfaces_merged'] and result['unit_count'] is None
    assert scene == original
    assert Polygon(scene['objects'][0]['ring']).distance(Polygon(scene['objects'][1]['ring'])) > 0


def test_same_plan_disconnected_lot_and_adjacent_other_plan_are_excluded(site):
    scene, parcels = site
    parcels['features'].append(parcel('1', box(80, 10, 100, 150), plan='PS2'))
    assert {m['object_id'] for m in block.block_context(scene, scene['objects'][0], parcels)['members']} == {'west', 'east'}


def test_without_common_property_multiple_lots_do_not_imply_one_block(site):
    scene, parcels = site
    parcels['features'] = [f for f in parcels['features'] if f['properties']['lot'] != 'CM1']
    assert not block.block_context(scene, scene['objects'][0], parcels)['members']


def test_single_parcel_can_contain_separate_buildings(site):
    scene, parcels = site
    parcels['features'] = [parcel('1', box(10, 10, 80, 150))]
    result = block.block_context(scene, scene['objects'][0], parcels)
    assert len(result['members']) == 2 and result['numbered_lots'] == 1


def test_single_parcel_still_works_within_a_larger_subdivision_plan(site):
    scene, parcels = site
    parcels['features'] = [parcel('1', box(10, 10, 80, 150)), parcel('2', box(80, 10, 130, 150))]
    result = block.block_context(scene, scene['objects'][0], parcels)
    assert result['status'] == 'grouped' and result['numbered_lots'] == 1
    assert {m['object_id'] for m in result['members']} == {'west', 'east'}


def test_partial_block_and_ambiguous_roofs_are_explicit(site):
    scene, parcels = site
    scene['image_height'] = 140
    scene['objects'].append(roof('crosses', box(75, 15, 95, 60)))
    result = block.block_context(scene, scene['objects'][0], parcels)
    assert result['status'] == 'partial_block' and not result['complete_in_image']
    assert result['ambiguous_object_ids'] == ['crosses']


def test_parcel_hole_does_not_pull_an_unrelated_building_into_block(site):
    scene, parcels = site
    shell = box(10, 10, 130, 150)
    parcels['features'] = [parcel('1', Polygon(shell.exterior, [box(100, 11, 126, 149).exterior]))]
    assert {m['object_id'] for m in block.block_context(scene, scene['objects'][0], parcels)['members']} == {'west', 'east'}


def test_overlapping_plans_and_missing_data_do_not_guess_a_group(site):
    scene, parcels = site
    assert block.block_context(scene, scene['objects'][0], None)['status'] == 'unavailable'
    parcels['features'].append(parcel('1', box(10, 10, 80, 150), plan='PS2'))
    assert block.block_context(scene, scene['objects'][0], parcels)['status'] == 'unavailable'


def test_parcels_are_frozen_and_bound_to_the_source_image(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(block, 'fetch_parcels', lambda bounds: calls.append(bounds) or {'features': []})
    scene = {'id': 'scene', 'image_sha256': 'image-a', 'region': {'bounds': [1, 2, 3, 4]}}
    first = block.parcel_snapshot(tmp_path, 0, scene)
    assert block.parcel_snapshot(tmp_path, 0, scene) == first and len(calls) == 1
    with pytest.raises(ValueError, match='another image'):
        block.parcel_snapshot(tmp_path, 0, {**scene, 'image_sha256': 'image-b'})


@pytest.mark.parametrize('index', [0, 1])
@pytest.mark.parametrize('angle', [0, 29])
def test_flat_comparison_straightens_runs_with_auditable_movement_limits(index, angle):
    data = json.loads((Path(__file__).parent/'fixtures/attached-flat-outline.json').read_text())
    obj = data['objects'][index]
    raw = rotate(Polygon(obj['boundary_refinement']['raw_ring']), angle, origin='centroid')
    result, report = regularize_eaves(raw, (1152, 1152), data['pixel_to_meters'], hypothesis=True)
    assert report['accepted'] and report['hypothesis_only'] and report['review_required']
    assert len(result.exterior.coords)-1 < len(obj['ring'])
    assert report['max_visible_deviation_m'] <= report['limits']['max_visible_deviation_m']
    assert report['visible_area_change_fraction'] <= report['limits']['visible_area_change_fraction']
    assert raw.intersection(result).area/raw.union(result).area > .9
    vertices = np.asarray(result.exterior.coords) @ np.asarray(data['pixel_to_meters'])[:2]
    edges = np.diff(vertices, axis=0)
    angles = np.arctan2(edges[:, 1], edges[:, 0])
    errors = abs((angles-np.deg2rad(report['axis_angle_deg'])+np.pi/4) % (np.pi/2)-np.pi/4)
    # Reported direction is rounded to .001 degrees; the actual runs are straight.
    assert sum(errors < np.deg2rad(.001)) >= len(edges)-2


def test_flat_boundary_requires_real_image_edges_and_preserves_saved_feedback():
    from PIL import Image
    data = json.loads((Path(__file__).parent/'fixtures/attached-flat-outline.json').read_text())
    obj = {**data['objects'][0], 'source': 'sam3-text-onnx', 'model': {'height_m': 3}}
    data['objects'] = [obj]
    original = copy.deepcopy(obj)
    candidate, report = straight_flat_outline(obj, data, Image.new('RGB', (1152, 1152), 'gray'))
    assert not report['accepted'] and candidate['ring'] == obj['ring'] and original == obj
    obj['roof_line_edits'] = {'edges': []}
    _, report = straight_flat_outline(obj, data, Image.new('RGB', (1152, 1152), 'gray'))
    assert not report['accepted']
