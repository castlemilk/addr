import json

import numpy as np
from PIL import Image
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.scene_extract import build_scene, extract_objects, group_objects, polygon_details
from shapely.geometry import Polygon


def test_scene_measurements_and_grouping_do_not_claim_verified_height():
    image = Image.new('RGB', (512, 512), (100, 150, 100))
    affine = np.array([[.1, 0], [0, -.1], [0, 50]])
    details = polygon_details([(10, 10), (40, 10), (40, 30), (10, 30)], image, affine, 'car')
    assert details['area_m2'] == 6
    assert details['length_m'] == 3
    assert details['width_m'] == 2
    assert details['height_verified'] is False
    assert details['height_source'] == 'class-prior'
    roof = {'id': 'roof', 'kind': 'building', 'label': 'Home', 'ring': [[0, 0], [100, 0], [100, 100], [0, 100]], 'model': {'height_m': 4}}
    panel = {'id': 'panel', 'kind': 'solar_panel', 'ring': [[10, 10], [20, 10], [20, 20], [10, 20]], 'model': {'base_m': 0}}
    groups = group_objects([roof, panel])
    assert groups == [{'id': 'roof', 'label': 'Home'}]
    assert roof['group_id'] == panel['group_id'] == groups[0]['id']
    assert panel['parent_id'] == 'roof'
    assert panel['model']['base_m'] == 4


def test_scene_snapshot_is_immutable_and_rerun_preserves_review(tmp_path):
    source = tmp_path / 'data/reviews/cohorts/test/1'
    source.mkdir(parents=True)
    Image.new('RGB', (512, 512), 'green').save(source / 'site-clean.png')
    ring = [[0, 0], [10, 0], [10, 10], [0, 10]]
    pixels = [[100, 400], [200, 400], [200, 300], [100, 300]]
    (source.parent/'report.json').write_text(json.dumps({'results': [{'address_id': 1, 'footprint_coords': ring, 'ring_pixels': pixels, 'parts': [], 'rooflines': []}]}))
    (tmp_path/'public').mkdir()
    (tmp_path/'public/address-roofs.json').write_text(json.dumps([{'address_id': 1, 'centroid': {'lat': -37.8, 'lon': 145.3}, 'full_address': 'Test roof', 'eave_height': 3}]))
    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, *args, **kwargs):
            return []
    first = build_scene(1, 'test', Segmenter(), root=tmp_path)
    folder = tmp_path/'data/scenes/test-1'
    (folder/'review.json').write_text('{"revision":7}')
    second = build_scene(1, 'test', Segmenter(), root=tmp_path)
    assert first['model_signature'] == second['model_signature']
    assert json.loads((folder/'review.json').read_text())['revision'] == 7
    assert (folder/'models'/f"{first['model_signature']}.json").exists()
    assert np.allclose(np.r_[pixels[0], 1] @ np.array(first['pixel_to_meters']), ring[0])


def test_masks_with_gaps_are_retained_and_material_colour_excludes_the_gap():
    image = np.full((100, 100, 3), [130, 70, 40], dtype=np.uint8)
    image[20:80, 20:80] = [20, 180, 250]
    mask = np.zeros((100, 100), dtype=bool)
    mask[10:90, 10:90] = True
    mask[20:80, 20:80] = False

    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, *args, **kwargs):
            return [Sam3Proposal(mask, .9, (10, 10, 90, 90))] * 2

    objects, scans = extract_objects(Image.fromarray(image), 'image', np.array([[.1, 0], [0, -.1], [0, 10]]),
                                     Segmenter(), kinds=['building'])
    assert scans['building']['count'] == len(objects) == 1, 'Duplicate masks are still suppressed'
    roof = objects[0]
    assert len(roof['holes']) == 1
    assert Polygon(roof['ring'], roof['holes']).area == mask.sum() == 2800
    assert roof['model']['area_m2'] == 28 and roof['model']['color'] == '#824628'
    assert roof['status'] == 'candidate' and not roof['geometry_reviewed']
    assert roof['model']['height_verified'] is False
    panel = {'id': 'panel', 'kind': 'solar_panel', 'ring': [[30, 30], [40, 30], [40, 40], [30, 40]], 'model': {'base_m': 0}}
    group_objects([roof, panel])
    assert 'parent_id' not in panel, 'An object in a courtyard is not supported by its enclosing roof'


def test_materially_different_roof_masks_are_not_suppressed_by_pipeline_seeds():
    mask = np.zeros((100, 100), dtype=bool)
    mask[10:70, 10:70] = True

    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, *args, **kwargs):
            return [Sam3Proposal(mask, .9, (10, 10, 70, 70))]

    for source in ['roof-pipeline', 'crm-roof-pipeline']:
        seed = {'id': 'old', 'kind': 'building', 'source': source, 'ring': [[10, 10], [90, 10], [90, 90], [10, 90]]}
        args = (Image.new('RGB', (100, 100)), 'image', np.array([[.1, 0], [0, -.1], [0, 10]]), Segmenter())
        objects, _ = extract_objects(*args, objects=[seed], kinds=['building'])
        assert len(objects) == 2 and objects[0] == seed
        seed['ring'] = objects[1]['ring']
        objects, _ = extract_objects(*args, objects=[seed], kinds=['building'])
        assert len(objects) == 1, 'Essentially identical seed and image masks still collapse'


def test_detector_only_prediction_ignores_saved_geometry_and_runs_roof_inference(tmp_path, monkeypatch):
    import copy

    import cv2
    from scripts.ml import scene_region
    from scripts.ml.scene_extract import analyse_scene, save_scene
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    # A noisy detector silhouette with a shallow projecting gable. The only
    # ridge supervision here is a brightness step in the input image.
    mask = np.zeros((180, 300), dtype=bool)
    mask[30:110, 20:260] = True
    for x in range(20, 260, 4):
        mask[30:32, x:x+2] = False
    mask[110:130, 100:160] = True
    lab = np.full((180, 300, 3), [150, 145, 145], dtype=np.uint8)
    lab[:, :130, 0] = 200
    image = Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))
    frame = np.array([[.1, 0], [0, -.1], [-2, 11.]])
    scene = {'id': 'fresh-roof', 'image_sha256': 'fixture', 'pixel_to_meters': frame.tolist(),
             'meters_to_pixel': [[10, 0], [0, -10], [20, 110]], 'objects': [{'id': 'manual', 'ring': [[0, 0], [1, 0], [1, 1]]}],
             'analysis': {'pipeline': 'stale'}}
    original = copy.deepcopy(scene)
    def no_crm(*args, **kwargs):
        raise AssertionError('Detector-only inference must not import CRM geometry')
    monkeypatch.setattr(scene_region, 'corrected_roofs', no_crm)

    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, *args, **kwargs):
            return [Sam3Proposal(mask, .95, (20, 30, 260, 130))]

    result = analyse_scene(scene, image, Segmenter(), kinds=['building'], roof_models=True, detector_only=True, root=tmp_path)
    assert scene == original
    assert result['analysis']['input_geometry'] == 'imagery-only'
    assert result['analysis']['roof_structure_code']
    assert result['analysis']['boundary_version'] == 'roof-envelope-v5'
    obj, = result['objects']
    assert obj['id'] != 'manual' and obj['source'] == 'sam3-text-onnx'
    assert len(obj['ring']) < 15
    assert obj['roof_parts'] and not obj['geometry_reviewed'] and obj['status'] == 'candidate'
    assert not obj.get('roof_feedback') and not obj.get('roof_line_edits')
    ridges = unary_union([LineString(line['line_meters']) for line in obj['roof_lines'] if line['kind'] == 'ridge'])
    assert ridges.distance(LineString([(8, -2), (14, -2)])) < .12, 'The inferred apex reaches the projecting front'
    folder = tmp_path/'data/scenes/fresh-roof'
    folder.mkdir(parents=True)
    review_bytes = b'{"revision":7,"objects":[{"id":"manual"}]}'
    (folder/'review.json').write_bytes(review_bytes)
    save_scene(result, b'image-fixture', tmp_path)
    assert (folder/'review.json').read_bytes() == review_bytes


def test_main_out_root_redirects_writes_and_default_path_is_unchanged(tmp_path, monkeypatch):
    import hashlib
    import io
    import sys

    import pytest
    from scripts.ml import scene_extract, scene_property_roofs
    from scripts.ml.scene_extract import analyse_scene, save_scene
    from scripts.ml.scene_region import region_frame

    pixels = np.full((512, 512, 3), [86, 118, 70], dtype=np.uint8)
    pixels[100:180, 100:220] = [180, 176, 172]
    mask = np.zeros((512, 512), dtype=bool)
    mask[100:180, 100:220] = True
    image = Image.fromarray(pixels)
    out = io.BytesIO()
    image.save(out, format='PNG')
    data = out.getvalue()

    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, features, prompt, *args, **kwargs):
            return [Sam3Proposal(mask, .9, (100, 100, 220, 180))] if prompt == 'building roof' else []

    repo, isolated, reference = tmp_path/'repo', tmp_path/'isolated', tmp_path/'reference'
    source = {**region_frame([145.31, -37.7905, 145.311, -37.7895]), 'id': 'map-fixture', 'title': 'Fixture',
              'image_width': 512, 'image_height': 512, 'image_sha256': hashlib.sha256(data).hexdigest(),
              'objects': [], 'groups': [], 'scans': {}}
    save_scene(source, data, repo)
    (repo/'data/scenes/.extraction.lock').touch()
    fetched = []
    monkeypatch.setattr(scene_property_roofs, 'fetch_parcels', lambda bounds: fetched.append(bounds) or [])
    monkeypatch.setattr(scene_extract, 'Sam3TextSegmenter', lambda path: Segmenter())
    monkeypatch.chdir(repo)

    def snapshot():
        return {str(p): p.read_bytes() for p in sorted((repo/'data').rglob('*')) if p.is_file()}

    argv = ['scene_extract', '--scene-id', 'map-fixture', '--classes', 'building', '--detector-only']
    before = snapshot()
    monkeypatch.setattr(sys, 'argv', [*argv, '--out-root', str(isolated)])
    scene_extract.main()
    assert snapshot() == before, 'An out-root run leaves the working tree byte-identical'
    candidate = json.loads((isolated/'data/scenes/map-fixture/latest.json').read_text())
    assert list((isolated/'data/scene-parcels').glob('*.json')) and len(fetched) == 1
    assert (isolated/'data/scenes/map-fixture'/f"{source['image_sha256']}.png").read_bytes() == data

    monkeypatch.setattr(sys, 'argv', argv)
    scene_extract.main()
    default = json.loads((repo/'data/scenes/map-fixture/latest.json').read_text())
    expected = save_scene(analyse_scene(source, image, Segmenter(), kinds=['building'], detector_only=True, root=reference),
                          data, reference)
    assert default['model_signature'] == expected['model_signature'] == candidate['model_signature']
    assert list((repo/'data/scene-parcels').glob('*.json')), 'Default parcels still freeze under the working tree'

    for bad in (repo/'data/scenes/x', repo):
        monkeypatch.setattr(sys, 'argv', [*argv, '--out-root', str(bad)])
        with pytest.raises(SystemExit) as refused:
            scene_extract.main()
        assert refused.value.code == 2


# --- Canopy fragment merge and the post-assembly building floor (Step 5) --------------------------------------------

def _prompted_segmenter(by_prompt):
    """A fake Segmenter answering each prompt with its own masks."""
    class Segmenter:
        def encode_image(self, image):
            return None
        def proposals(self, features, prompt, size, **kwargs):
            return [Sam3Proposal(mask, .9, (0, 0, size[0], size[1])) for mask in by_prompt.get(prompt, [])]
    return Segmenter()


def _mask(size, x0, y0, x1, y1):
    mask = np.zeros((size, size), dtype=bool)
    mask[y0:y1, x0:x1] = True
    return mask


AFFINE = np.array([[.1, 0], [0, -.1], [0, 20]])   # 0.1 m/px: 1 px² == 0.01 m²


def test_canopy_fragment_inside_a_tree_is_merged_into_it_with_replaces():
    from scripts.ml.scene_extract import PROMPTS
    image = Image.new('RGB', (200, 200), (90, 120, 80))
    tree, piece, far = _mask(200, 40, 40, 140, 140), _mask(200, 80, 80, 88, 88), _mask(200, 170, 10, 178, 18)
    objects, scans = extract_objects(image, 'img', AFFINE, _prompted_segmenter({PROMPTS['tree']: [tree], PROMPTS['vegetation']: [piece, far]}),
                                     kinds=['tree', 'vegetation'])
    trees = [o for o in objects if o['kind'] == 'tree']
    vegetation = [o for o in objects if o['kind'] == 'vegetation']
    assert len(trees) == 1 and len(vegetation) == 1, 'the 0.64 m² piece inside the 100 m² canopy is folded in; the far patch stays'
    assert scans['vegetation']['count'] == 1
    alone, _ = extract_objects(image, 'img', AFFINE, _prompted_segmenter({PROMPTS['vegetation']: [piece, far]}), kinds=['vegetation'])
    piece_id = next(o['id'] for o in alone if Polygon(o['ring']).area < 100)
    far_id = next(o['id'] for o in alone if Polygon(o['ring']).area >= 60 and o['id'] != piece_id)
    assert trees[0]['replaces'] == [piece_id], 'the container remembers the fragment it absorbed'
    assert vegetation[0]['id'] == far_id, 'untouched objects keep their identity'


def test_canopy_fragment_outside_the_canopy_stays_separate():
    from scripts.ml.scene_extract import PROMPTS
    image = Image.new('RGB', (200, 200), (90, 120, 80))
    tree, nearby = _mask(200, 40, 40, 140, 140), _mask(200, 148, 60, 154, 65)   # 0.3 m², 0.8 m outside the canopy
    objects, _ = extract_objects(image, 'img', AFFINE, _prompted_segmenter({PROMPTS['tree']: [tree], PROMPTS['vegetation']: [nearby]}), kinds=['tree', 'vegetation'])
    assert [o['kind'] for o in objects].count('vegetation') == 1 and 'replaces' not in next(o for o in objects if o['kind'] == 'tree')


def test_reviewed_canopy_container_is_never_modified_and_the_fragment_is_kept():
    from scripts.ml.scene_extract import PROMPTS
    image = Image.new('RGB', (200, 200), (90, 120, 80))
    reviewed = {'id': 'tree-reviewed', 'kind': 'tree', 'label': 'Tree', 'status': 'candidate', 'geometry_reviewed': True, 'source': 'sam3-text-onnx',
                'ring': [[40, 40], [140, 40], [140, 140], [40, 140]], 'model': {}}
    before = json.dumps(reviewed, sort_keys=True)
    objects, _ = extract_objects(image, 'img', AFFINE, _prompted_segmenter({PROMPTS['vegetation']: [_mask(200, 80, 80, 88, 88)]}),
                                 objects=[reviewed], kinds=['vegetation'])
    kept = [o for o in objects if o['kind'] == 'vegetation']
    assert len(kept) == 1, 'a reviewed container keeps the fragment visible instead of being edited'
    assert json.dumps(next(o for o in objects if o['id'] == 'tree-reviewed'), sort_keys=True) == before


def test_post_assembly_building_floor_drops_sub_square_metre_rings():
    from scripts.ml.scene_extract import BUILDING_FLOOR_M2, apply_building_floor
    small = (Polygon([(0, 0), (7, 0), (7, 7), (0, 7)]), .9, 'small', None)      # 49 px² = 0.49 m²
    large = (Polygon([(0, 0), (40, 0), (40, 40), (0, 40)]), .9, 'large', None)
    assert apply_building_floor([small, large], .01) == [large]
    assert BUILDING_FLOOR_M2 == 1.
    image = Image.new('RGB', (200, 200), (120, 120, 120))
    objects, scans = extract_objects(image, 'img', AFFINE, _prompted_segmenter({'building roof': [_mask(200, 10, 10, 17, 17)]}), kinds=['building'])
    assert objects == [] and scans['building']['count'] == 0


def test_merge_plan_reads_latest_json_only_and_writes_nothing(tmp_path):
    from scripts.ml.scene_extract import merge_canopy_fragments, merge_plan
    scene = {'id': 'fixture', 'pixel_to_meters': AFFINE.tolist(), 'image_width': 200, 'image_height': 200, 'objects': [
        {'id': 'big', 'kind': 'tree', 'status': 'candidate', 'ring': [[40, 40], [140, 40], [140, 140], [40, 140]]},
        {'id': 'bit', 'kind': 'vegetation', 'status': 'candidate', 'ring': [[80, 80], [88, 80], [88, 88], [80, 88]]},
        {'id': 'old', 'kind': 'vegetation', 'status': 'superseded', 'ring': [[81, 81], [87, 81], [87, 87], [81, 87]]},
        {'id': 'tiny-roof', 'kind': 'building', 'status': 'candidate', 'ring': [[0, 0], [5, 0], [5, 5], [0, 5]]},
        {'id': 'roof', 'kind': 'building', 'status': 'candidate', 'ring': [[150, 150], [190, 150], [190, 190], [150, 190]]}]}
    folder = tmp_path/'data/scenes/fixture'
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(scene))
    listing = sorted(str(p) for p in tmp_path.rglob('*'))
    plan = merge_plan(json.loads((folder/'latest.json').read_text()))
    assert [m['fragment_id'] for m in plan['merges']] == ['bit'] and plan['merges'][0]['container_id'] == 'big'
    assert plan['merges'][0]['fragment_m2'] == .64 and plan['merges'][0]['cover'] == 1.
    assert plan['buildings_under_floor'] == ['tiny-roof'] and plan['canopy_objects'] == 2
    assert sorted(str(p) for p in tmp_path.rglob('*')) == listing, 'the dry run writes nothing'
    kept, replaces = merge_canopy_fragments([dict(o) for o in scene['objects'] if o['kind'] == 'vegetation' and o['status'] == 'candidate'],
                                            [dict(o) for o in scene['objects'] if o['kind'] == 'tree'], AFFINE)
    assert kept == [] and replaces == {'bit': 'big'}
