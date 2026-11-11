import copy
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw
from scripts.ml import roof_block_context as context
from scripts.ml import scene_property_roofs as model
from scripts.ml.roof_lot_review import POLICY, geometry
from shapely.geometry import Polygon, box, mapping


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(context, 'image_projection', lambda _: lambda x, y, z=None: (x, y))
    roof = {'id': 'roof', 'kind': 'building', 'ring': list(box(40, 40, 100, 150).exterior.coords[:-1]),
            'source': 'sam3-text-onnx', 'status': 'candidate', 'geometry_reviewed': False}
    scene = {'id': 'shared', 'image_sha256': 'image', 'image_width': 240, 'image_height': 240,
             'pixel_to_meters': [[.5, 0], [0, .5], [0, 0]], 'objects': [roof],
             'region': {'bounds': [0, 0, 240, 240]}}
    parcels = {'features': [{'type': 'Feature', 'geometry': mapping(box(30, 30+i*70, 110, 100+i*70)),
                            'properties': {'pfi': str(i), 'lot': str(i+1), 'plan': 'PS1',
                                           'status': 'approved', 'horizontal_uncertainty_m': 1.5}}
                           for i in range(2)], 'bounds': [0, 0, 240, 240]}
    image = Image.new('RGB', (240, 240), '#263b22')
    ImageDraw.Draw(image).rectangle((40, 40, 99, 149), fill='white')
    return json.loads(json.dumps(scene)), json.loads(json.dumps(parcels)), image


class Segmenter:
    model_identity = {'detector': 'frozen-test-weights'}

    def __init__(self):
        self.calls = 0

    def encode_image(self, image):
        self.calls += 1
        return np.asarray(image)

    def proposals(self, features, prompt, size, **_):
        assert prompt in ('roof', 'building roof')
        mask = features[:, :, 0] > 200
        return [SimpleNamespace(mask=mask, score=.9)]


def test_shared_roof_uses_individual_lots_without_common_property(site, tmp_path):
    scene, parcels, image = site
    original = copy.deepcopy(scene)
    block = context.lot_context(scene, scene['objects'][0], parcels)
    assert block['status'] == 'individual_lots' and block['numbered_lots'] == 2
    segmenter = Segmenter()
    result = model.infer_property_roofs(scene, image, segmenter, root=tmp_path, parcels=parcels)
    assert result['status'] == 'complete' and segmenter.calls == 2
    first, second = result['blocks'][0]['lot_review']['lots']
    a, b = geometry(first['polygons']), geometry(second['polygons'])
    assert a.area > 0 and b.area > 0 and a.intersection(b).area == 0
    assert a.union(b).difference(Polygon(scene['objects'][0]['ring'])).area < 1e-8
    assert first['allocation_edges'] and second['allocation_edges']
    assert not result['human_approved'] and not result['training_eligible'] and scene == original


def test_same_plan_neighbour_is_not_a_whole_estate_allocation(site, tmp_path):
    scene, parcels, image = site
    neighbour = copy.deepcopy(parcels['features'][0])
    neighbour['geometry'] = mapping(box(110, 30, 180, 170))
    neighbour['properties'].update(pfi='neighbour', lot='9')
    parcels['features'].append(neighbour)
    result = model.infer_property_roofs(scene, image, Segmenter(), root=tmp_path, parcels=parcels)
    assert result['status'] == 'complete'
    assert result['sam_searches'] == 2
    assert {lot['lot'] for lot in result['blocks'][0]['lot_review']['lots']} == {'1', '2'}


def test_overlapping_search_contexts_are_deduplicated_by_parcel_not_plan(site):
    scene, parcels, _ = site
    block = context.lot_context(scene, scene['objects'][0], parcels)
    first, second, distant = [copy.deepcopy(block) for _ in range(3)]
    first['lots'] = [block['lots'][0]]
    distant['lots'][0]['pfi'] = 'distant-1'
    distant['lots'][1]['pfi'] = 'distant-2'
    groups = model.canonical_blocks([first, second, distant], .5)
    assert len(groups) == 2
    ids = [lot['pfi'] for group in groups for lot in group['lots']]
    assert len(ids) == len(set(ids)) == 4
    assert all(not group.get('search_error') for group in groups)
    second['lots'][0]['horizontal_uncertainty_m'] = 3
    assert model.canonical_blocks([first, second], .5)[0]['search_error']


@pytest.mark.parametrize('case', ['overlap', 'missing_plan'])
def test_ambiguous_strata_or_missing_plan_never_guess_lots(site, tmp_path, case):
    scene, parcels, image = site
    if case == 'overlap':
        parcels['features'].append(copy.deepcopy(parcels['features'][0]))
        parcels['features'][-1]['properties']['pfi'] = 'strata'
    else:
        for feature in parcels['features']:
            feature['properties']['plan'] = None
    segmenter = Segmenter()
    result = model.infer_property_roofs(scene, image, segmenter, root=tmp_path, parcels=parcels)
    assert result['status'] == 'not_applicable' and segmenter.calls == 0


@pytest.mark.parametrize('case', ['no_parcels', 'missing_weights', 'wrong_image', 'saved_edit'])
def test_unavailable_evidence_keeps_original_geometry(site, tmp_path, case):
    scene, parcels, image = site
    segmenter = Segmenter()
    if case == 'no_parcels':
        parcels = None
    elif case == 'missing_weights':
        segmenter.model_identity = None
    elif case == 'wrong_image':
        image = Image.new('RGB', (10, 10))
    else:
        scene['objects'][0]['roof_line_edits'] = {'edges': []}
    original = copy.deepcopy(scene)
    result = model.infer_property_roofs(scene, image, segmenter, root=tmp_path, parcels=parcels)
    assert result['status'] in ('unavailable', 'not_applicable') and segmenter.calls == 0
    assert scene == original


def test_crop_failure_continues_other_lots_and_budget_is_explicit(site, tmp_path, monkeypatch):
    scene, parcels, image = site
    class Failing(Segmenter):
        def encode_image(self, crop):
            features = super().encode_image(crop)
            if self.calls == 1:
                raise RuntimeError('detector unavailable')
            return features
    result = model.infer_property_roofs(scene, image, Failing(), root=tmp_path, parcels=parcels)
    assert result['status'] == 'partial' and result['counts'] == {'failed': 1, 'needs_review': 1}
    monkeypatch.setitem(POLICY, 'max_lots_per_scene', 1)
    result = model.infer_property_roofs(scene, image, Segmenter(), root=tmp_path, parcels=parcels)
    assert result['sam_searches'] == 1 and result['counts'] == {'needs_review': 1, 'not_run': 1}


def test_reproducible_output_reused_only_with_matching_frame_and_geometry(site, tmp_path):
    scene, parcels, image = site
    model.add_property_roofs(scene, image, Segmenter(), root=tmp_path, parcels=parcels)
    assert scene['analysis']['parcel_roof_model'] == model.VERSION
    assert model.inherited_parcels(scene) == parcels
    block = scene['property_roofs']['blocks'][0]
    assert model.inherited_review(scene, block) == block['lot_review']
    for field in ['image_sha256', 'pixel_to_meters', 'objects']:
        changed = copy.deepcopy(scene)
        if field == 'image_sha256':
            changed[field] = 'new'
        elif field == 'pixel_to_meters':
            changed[field][2][0] += 1
        else:
            changed['objects'][0]['ring'][0][0] += 1
        assert model.inherited_review(changed, block) is None
    changed = copy.deepcopy(scene)
    changed['property_roofs']['blocks'][0]['lot_review']['policy']['max_boundary_shift_m'] = 99
    assert model.inherited_review(changed, block) is None
    second = copy.deepcopy(scene)
    model.add_property_roofs(second, image, Segmenter(), root=tmp_path, parcels=parcels)
    assert scene == second


def test_public_parcels_are_frozen_but_errors_are_retryable(site, tmp_path, monkeypatch):
    scene, parcels, _ = site
    calls = []
    def fetch(_):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('offline')
        return parcels
    monkeypatch.setattr(model, 'fetch_parcels', fetch)
    with pytest.raises(OSError):
        model.parcel_input(tmp_path, scene)
    assert model.parcel_input(tmp_path, scene) == parcels
    assert model.parcel_input(tmp_path, scene) == parcels and len(calls) == 2
    cached = next((tmp_path/'data/scene-parcels').glob('*.json'))
    payload = json.loads(cached.read_text())
    payload['data']['features'] = []
    cached.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='changed'):
        model.parcel_input(tmp_path, scene)


def test_default_scene_reanalysis_runs_per_lot_model_and_discards_stale_allocations(site, tmp_path, monkeypatch):
    from scripts.ml.scene_extract import analyse_scene
    scene, parcels, image = site
    scene['property_roofs'] = {'version': 'stale', 'blocks': [{'manual': True}]}
    scene['objects'][0]['roof_line_edits'] = {'edges': [{'reviewer': True}]}
    original = copy.deepcopy(scene)
    result = analyse_scene(scene, image, Segmenter(), kinds=['building'], detector_only=True,
                           roof_models=False, parcel_data=parcels, root=tmp_path)
    assert result['property_roofs']['version'] == model.VERSION
    assert result['property_roofs']['sam_searches'] == 2
    assert result['property_roofs']['status'] == 'complete'
    assert all(not obj.get('roof_line_edits') for obj in result['objects'])
    assert scene == original
