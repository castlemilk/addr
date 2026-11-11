"""Map projections must retain saved frames, review priority and separate instances."""
import copy
import json
import sqlite3

import pytest
from PIL import Image
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_house import house_source
from scripts.ml.scene_map import map_layers, render_snapshot
from scripts.ml.scene_region import capture_region
from shapely.geometry import shape

BOUNDS = [145.3121, -37.7908, 145.313, -37.7900]


class Tiles:
    def _child(self, *args):
        return Image.new('RGB', (256, 256), (110, 80, 60))


def scene(root, identity, rings=None):
    model, _, image = capture_region(BOUNDS, root=root, fetcher=Tiles())
    model['id'] = identity
    model['objects'] = [{'id': f'{identity}-{i}', 'kind': 'building', 'label': 'Roof', 'status': 'candidate',
                         'geometry_reviewed': False, 'ring': ring} for i, ring in enumerate(rings or [[[100, 100], [300, 100], [300, 300], [100, 300]]])]
    model['scans'] = {'building': {'status': 'complete'}}
    saved = save_scene(model, image, root)
    return saved, image


def feedback(root, model, status='confirmed'):
    objects = copy.deepcopy(model['objects'])
    objects[0].update(status=status, geometry_reviewed=True)
    review = {'model_signature': model['model_signature'], 'revision': 2, 'updated_at': '2026-09-23T01:00:00Z', 'objects': objects, 'groups': []}
    path = root/'data/scenes'/model['id']/'review.json'
    path.write_text(json.dumps(review))
    return path


def test_saved_feedback_wins_over_overlapping_captures_without_writes(tmp_path):
    a, _ = scene(tmp_path, 'a')
    scene(tmp_path, 'b')
    path = feedback(tmp_path, a)
    before = path.read_bytes()
    result = map_layers(BOUNDS, root=tmp_path)
    assert len(result['outlines']['features']) == 1
    assert result['outlines']['features'][0]['properties']['scene_id'] == 'a'
    assert result['duplicates'] == 1
    assert path.read_bytes() == before
    assert shape(result['coverage']['building']).is_valid
    assert len(result['coverage']['building']['coordinates']) == 1
    assert result['coverage']['tree']['coordinates'] == []
    ring = result['outlines']['features'][0]['geometry']['coordinates'][0]
    assert ring[0][0] < ring[1][0] and ring[0][1] > ring[2][1]


def test_rejected_feedback_vetoes_other_proposals_but_model_remains_available(tmp_path):
    a, _ = scene(tmp_path, 'a')
    scene(tmp_path, 'b')
    feedback(tmp_path, a, 'rejected')
    assert map_layers(BOUNDS, root=tmp_path)['outlines']['features'] == []
    assert len(map_layers(BOUNDS, 'model', root=tmp_path)['outlines']['features']) == 1


def test_separate_instances_in_one_capture_remain_separate(tmp_path):
    ring = [[100, 100], [300, 100], [300, 300], [100, 300]]
    scene(tmp_path, 'a', [ring, ring])
    assert len(map_layers(BOUNDS, root=tmp_path)['outlines']['features']) == 2


def test_stale_review_uses_original_frozen_coordinate_frame(tmp_path):
    a, image = scene(tmp_path, 'a')
    path = feedback(tmp_path, a)
    before = path.read_bytes()
    original = map_layers(BOUNDS, root=tmp_path)['outlines']['features'][0]['geometry']
    changed = copy.deepcopy(a)
    changed['pixel_to_meters'][2][0] += 5
    save_scene(changed, image, tmp_path)
    result = map_layers(BOUNDS, root=tmp_path)
    assert result['scenes'][0]['stale']
    assert result['outlines']['features'][0]['geometry'] == original
    assert map_layers(BOUNDS, 'model', root=tmp_path)['outlines']['features'][0]['geometry'] != original
    assert path.read_bytes() == before


@pytest.mark.parametrize('bounds', [[0, 0, 1, 1], [1, 1, 0, 0], [145, -37, float('nan'), -36]])
def test_map_rejects_unbounded_requests(bounds, tmp_path):
    with pytest.raises(ValueError):
        map_layers(bounds, root=tmp_path)


def test_house_capture_does_not_require_a_curated_cohort(tmp_path):
    (tmp_path/'data').mkdir()
    with sqlite3.connect(tmp_path/'data/australia_geo.db') as db:
        db.execute('CREATE TABLE addresses(id INTEGER PRIMARY KEY, full_address TEXT, lat REAL, lon REAL)')
        db.execute('INSERT INTO addresses VALUES(901, ?, ?, ?)', ('29 Cambridge Road', -37.7904, 145.3125))
    model, image, raw = house_source(901, root=tmp_path, fetcher=Tiles())
    assert model['id'] == 'house-901' and model['address_id'] == 901
    assert model['title'] == '29 Cambridge Road'
    assert model['cohort'] is None and model['region']['spatial_group'].startswith('z17:')
    assert image.size == (512, 512) and raw
    assert model['objects'] == []  # A capture never invents labels.
    with pytest.raises(ValueError, match='not found'):
        house_source(999, root=tmp_path, fetcher=Tiles())
    with pytest.raises(ValueError):
        house_source(True, root=tmp_path, fetcher=Tiles())


def test_new_roof_estimates_are_proposals_not_training_labels(tmp_path):
    import numpy as np
    from scripts.ml.scene_extract import polygon_details
    from scripts.ml.scene_house import add_roof_estimates
    model, image, _ = capture_region(BOUNDS, root=tmp_path, fetcher=Tiles())
    ring = [[160, 200], [350, 200], [350, 300], [160, 300]]
    obj = {'id': 'new', 'kind': 'building', 'ring': ring, 'status': 'candidate', 'geometry_reviewed': False,
           'model': polygon_details(ring, image, np.asarray(model['pixel_to_meters']), 'building')}
    model['objects'] = [obj]
    add_roof_estimates(model, image)
    assert model['analysis']['roof_proposals'] == 1
    assert obj['roof_parts'] and obj['roof_annotation']['vertices']
    assert obj['status'] == 'candidate' and not obj['geometry_reviewed']
    assert not obj['model']['height_verified']
    assert 'roof_feedback' not in obj


def test_pitched_map_loads_captures_near_camera_focus_not_horizon_bounds(tmp_path):
    a, _ = scene(tmp_path, 'a')
    b, image = scene(tmp_path, 'b')
    b['centroid']['lon'] += .00025
    save_scene(b, image, tmp_path)
    result = map_layers(BOUNDS, root=tmp_path, limit=1, focus=[b['centroid']['lon'], b['centroid']['lat']])
    assert result['limited'] and result['available_scenes'] == 2
    assert result['scenes'][0]['model']['id'] == 'b'
    assert a['id'] != result['scenes'][0]['model']['id']


def test_suburb_context_does_not_claim_neighbour_tile_coverage(tmp_path):
    from shapely.geometry import box, mapping
    model, image = scene(tmp_path, 'suburb')
    model['objects'].append({**copy.deepcopy(model['objects'][0]), 'id': 'context'})
    model['owned_object_ids'] = [model['objects'][0]['id']]
    core = box(145.3123, -37.7906, 145.3127, -37.7902)
    model['analysis_core'] = mapping(core)
    save_scene(model, image, tmp_path)
    result = map_layers(BOUNDS, root=tmp_path)
    assert len(result['outlines']['features']) == 1
    assert shape(result['coverage']['building']).symmetric_difference(core).area < 1e-14


def test_suburb_meshes_are_not_crowded_out_by_old_training_crops(tmp_path):
    old, _ = scene(tmp_path, 'old-training')
    current, image = scene(tmp_path, 'suburb-current')
    current['suburb_run'] = 'completed-run'
    save_scene(current, image, tmp_path)
    result = map_layers(BOUNDS, 'model', root=tmp_path, limit=1)
    assert result['scenes'][0]['model']['id'] == 'suburb-current'
    result = map_layers(BOUNDS, 'model', root=tmp_path)
    assert result['outlines']['features'][0]['properties']['scene_id'] == 'suburb-current'
    feedback(tmp_path, old)
    result = map_layers(BOUNDS, root=tmp_path, limit=1)
    edited = next(s for s in result['scenes'] if s['model']['id'] == 'old-training')
    assert edited['revision'] == 2
    assert map_layers(BOUNDS, root=tmp_path)['outlines']['features'][0]['properties']['scene_id'] == 'old-training'


def test_transport_retains_exact_geometry_and_hidden_mounting_parents():
    roof = {'id': 'roof', 'kind': 'building', 'ring': [[0, 0], [10, 0], [10, 10]],
            'holes': [[[2, 2], [3, 2], [3, 3]]], 'visibility': 'occluded',
            'model': {'height_m': 4, 'base_m': 1},
            'roof_annotation': {'vertices': [[0, 0, 4], [10, 0, 6], [10, 10, 4]], 'faces': [[0, 1, 2]]},
            'boundary_refinement': {'large_diagnostic': [1] * 1000}}
    panel = {'id': 'panel', 'kind': 'solar_panel', 'parent_id': 'roof', 'ring': [[2, 0], [4, 0], [4, 1]],
             'model': {'height_m': .1, 'solar': {'tilt_deg': 20}}}
    model = {'centroid': {'lon': 145, 'lat': -37}, 'pixel_to_meters': [[1, 0], [0, -1], [10, 10]],
             'image_width': 512, 'objects': [roof, panel, {'id': 'offscreen'}], 'analysis': {'large': [0] * 1000}}
    before = copy.deepcopy(model)
    transported = render_snapshot(model, ['panel'])
    assert [o['id'] for o in transported['objects']] == ['roof', 'panel']
    rendered_roof = transported['objects'][0]
    for key in ('ring', 'holes', 'visibility', 'model', 'roof_annotation'):
        assert rendered_roof[key] == roof[key]
    assert transported['objects'][1] == panel
    assert transported['pixel_to_meters'] == model['pixel_to_meters']
    assert 'analysis' not in transported and 'boundary_refinement' not in rendered_roof
    assert model == before


def test_saved_drafts_do_not_displace_current_neighbour_roofs_from_budget(tmp_path):
    rings = [[[50, 50], [180, 50], [180, 180], [50, 180]],
             [[250, 250], [380, 250], [380, 380], [250, 380]]]
    old, _ = scene(tmp_path, 'old', rings)
    current, image = scene(tmp_path, 'current', rings)
    current['suburb_run'] = 'completed-run'
    save_scene(current, image, tmp_path)
    path = feedback(tmp_path, old, 'candidate')
    review = json.loads(path.read_text())
    review['objects'][0]['geometry_reviewed'] = False
    review['objects'][0]['label'] = 'Saved draft correction'
    path.write_text(json.dumps(review))
    before = path.read_bytes()
    result = map_layers(BOUNDS, root=tmp_path, limit=1)
    ids = {f['properties']['object_id'] for f in result['outlines']['features']}
    assert ids == {'old-0', 'current-1'}
    assert path.read_bytes() == before
    assert {f['properties']['object_id'] for f in map_layers(BOUNDS, 'model', root=tmp_path, limit=1)['outlines']['features']} == {'current-0', 'current-1'}
