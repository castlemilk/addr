"""Exercise image registration, immutable previews and revision-bound draft writes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import scene_polygon_feedback as polygons
from scripts.ml.mcp_server import server
from scripts.ml.tests.test_scene_roof_diagnostics import (
    RECT_PX,
    _building,
    _image,
    _scene,
    _write_scene,
)

PROPOSED = {'ring': [[64, 80], [192, 80], [192, 168], [64, 168]],
            'holes': [[[90, 100], [110, 100], [110, 120], [90, 120]]]}


@pytest.fixture
def saved(tmp_path, monkeypatch):
    image = _image(step=True)
    obj = _building('roof', RECT_PX, source='sam3-text-onnx')
    model = _scene([obj], image)
    model['model_signature'] = 'a'*64
    _write_scene(tmp_path, model, image)

    def feedback(scene_id, object_id=None):
        current = json.loads((tmp_path/'data/scenes'/scene_id/'latest.json').read_bytes())
        review_path = tmp_path/'data/scenes'/scene_id/'review.json'
        review = json.loads(review_path.read_bytes()) if review_path.exists() else None
        objects = (review or current)['objects']
        return {'schema_version': 1, 'scene_id': scene_id, 'revision': (review or {}).get('revision', 0),
                'model_signature': current['model_signature'], 'stale': False,
                'image': {'sha256': current['image_sha256'], 'width': current['image_width'], 'height': current['image_height']},
                'objects': [{'object_id': item['id'], 'outline_pixels': {'ring': item['ring'], 'holes': item.get('holes', [])},
                             'automated_polygon_edit_allowed': not bool(item.get('geometry_reviewed') or item.get('status') == 'confirmed'
                                 or (item.get('roof_line_edits') or {}).get('checked') or item.get('protected')),
                             'automated_polygon_edit_reason': 'Protected reviewer feedback' if item.get('protected') else None}
                            for item in objects if object_id is None or item['id'] == object_id]}

    monkeypatch.setattr(polygons, 'load_roof_line_feedback', feedback)
    def object_feedback(*args):
        result = feedback(*args)
        current = json.loads((tmp_path/'data/scenes'/args[0]/'latest.json').read_bytes())
        review_path = tmp_path/'data/scenes'/args[0]/'review.json'
        current = json.loads(review_path.read_bytes()) if review_path.exists() else current
        for row in result['objects']:
            row['kind'] = next(item['kind'] for item in current['objects'] if item['id'] == row['object_id'])
        return result

    monkeypatch.setattr(polygons, 'load_object_feedback', object_feedback)
    return tmp_path, model, obj, image


def source_bytes(root):
    return {str(path): path.read_bytes() for path in (root/'data/scenes').rglob('*') if path.is_file()}


def test_preview_is_source_bound_registered_and_preserves_holes(saved):
    root, model, obj, image = saved
    before = source_bytes(root)
    packet = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root,
                                      notes='Assistant visually traced a proposed eave.', uncertainty='Shadowed lower edge.')
    assert packet['binding']['revision'] == 0 and packet['binding']['outline_pixels'] == {'ring': RECT_PX, 'holes': []}
    assert packet['proposed_outline_pixels'] == PROPOSED
    assert packet['can_apply'] and packet['tier'] == 3
    assert packet['training_eligible'] is False and packet['human_approval'] is False
    assert packet['coordinate_space'] == 'source_image_pixels' and packet['axes'] == ['right', 'down']
    render = packet['render']
    xy = np.array([[0., 0.], [97.3, 121.7], [256., 256.]])
    panel = np.c_[xy, np.ones(3)] @ np.asarray(render['source_to_panel'])
    assert np.allclose(np.c_[panel, np.ones(3)] @ np.asarray(render['panel_to_source']), xy)
    expected = image.crop(render['crop_box_pixels']).resize(tuple(render['panel_size_pixels']))
    with Image.open(packet['paths']['card']) as card:
        assert card.crop((10, 40, 490, 520)).tobytes() == expected.tobytes()
        assert card.crop((990, 40, 1470, 520)).tobytes() != expected.tobytes()
    assert source_bytes(root) == before
    assert polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root,
        notes='Assistant visually traced a proposed eave.', uncertainty='Shadowed lower edge.') == packet


def test_apply_delegates_exact_preview_through_api_without_caller_approval(saved, monkeypatch):
    root, model, obj, _ = saved
    packet = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root, notes='Assistant polygon proposal')
    calls = []

    def update(**kwargs):
        calls.append(kwargs)
        return {'schema_version': 1, 'scene_id': model['id'], 'object_id': obj['id'], 'revision': 1,
                'model_signature': model['model_signature'], 'image_sha256': model['image_sha256'],
                'object': {**obj, **PROPOSED}, 'training_eligible': False, 'human_approval': False}

    monkeypatch.setattr(polygons, 'update_draft_roof_feedback', update)
    before = source_bytes(root)
    result = polygons.apply_polygon(packet['preview_id'], root=root)
    assert result['preview_id'] == packet['preview_id'] and result['revision'] == 1
    assert calls == [{'scene_id': model['id'], 'object_id': obj['id'], 'expected_revision': 0,
                     'model_signature': model['model_signature'], 'image_sha256': model['image_sha256'],
                     'outline_pixels': {'ring': RECT_PX, 'holes': []},
                     'changes': {'outline_pixels': PROPOSED, 'polygon_preview_id': packet['preview_id'], 'notes': 'Assistant polygon proposal'}}]
    assert source_bytes(root) == before


@pytest.mark.parametrize('change', ['source', 'image', 'review'])
def test_changed_bindings_block_apply_before_api(saved, monkeypatch, change):
    root, model, obj, _ = saved
    packet = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    folder = root/'data/scenes'/model['id']
    if change == 'source':
        (folder/'latest.json').write_text(json.dumps({**model, 'title': 'Updated model bytes'}))
    elif change == 'image':
        (folder/f"{model['image_sha256']}.png").write_bytes(b'changed')
    else:
        (folder/'review.json').write_text(json.dumps({'revision': 1, 'model_signature': model['model_signature'], 'objects': [obj]}))
    calls = []
    monkeypatch.setattr(polygons, 'update_draft_roof_feedback', lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match='changed|fresh preview'):
        polygons.apply_polygon(packet['preview_id'], root=root)
    assert not calls


@pytest.mark.parametrize('artifact', ['content', 'render', 'card'])
def test_altered_sidecars_block_apply(saved, monkeypatch, artifact):
    root, model, obj, _ = saved
    packet = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    manifest = Path(packet['paths']['manifest'])
    saved_manifest = json.loads(manifest.read_bytes())
    if artifact == 'card':
        Path(packet['paths']['card']).write_bytes(b'altered')
    else:
        if artifact == 'content':
            saved_manifest['content']['proposed_outline_pixels']['ring'][0][0] += 1
        else:
            saved_manifest['render']['panel_to_source'][2][0] += 1
        manifest.write_text(json.dumps(saved_manifest))
    calls = []
    monkeypatch.setattr(polygons, 'update_draft_roof_feedback', lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match='altered'):
        polygons.apply_polygon(packet['preview_id'], root=root)
    assert not calls


@pytest.mark.parametrize('protected', ['geometry_reviewed', 'status', 'lines', 'canonical'])
def test_checked_or_protected_objects_preview_but_never_apply(saved, monkeypatch, protected):
    root, model, obj, _ = saved
    current = copy.deepcopy(obj)
    if protected == 'geometry_reviewed':
        current['geometry_reviewed'] = True
    elif protected == 'status':
        current['status'] = 'confirmed'
    elif protected == 'lines':
        current['roof_line_edits'] = {'checked': True}
    else:
        current['protected'] = True
    (root/'data/scenes'/model['id']/'review.json').write_text(json.dumps({'revision': 2, 'model_signature': model['model_signature'], 'objects': [current]}))
    packet = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    assert packet['can_apply'] is False
    with pytest.raises(ValueError, match='preserved|Protected'):
        polygons.apply_polygon(packet['preview_id'], root=root)


@pytest.mark.parametrize('outline', [
    {'ring': [[64, 80], [192, 80], [192, 168], [64, 80]], 'holes': []},
    {'ring': [[64, 80], [192, 168], [192, 80], [64, 168]], 'holes': []},
    {'ring': [[-1, 80], [192, 80], [192, 168]], 'holes': []},
    {'ring': [[True, 80], [192, 80], [192, 168]], 'holes': []},
    {'ring': [[64, 80], [float('nan'), 80], [192, 168]], 'holes': []},
    {'ring': PROPOSED['ring'], 'holes': [[[0, 0], [10, 0], [10, 10]]]},
])
def test_invalid_polygon_never_produces_preview(saved, outline):
    root, model, obj, _ = saved
    with pytest.raises(ValueError):
        polygons.preview_polygon(model['id'], obj['id'], outline, root=root)
    assert not (root/polygons.DIRECTORY).exists()


def test_api_binding_mismatch_and_missing_permission_flag_fail_closed(saved, monkeypatch):
    root, model, obj, _ = saved
    original = polygons.load_roof_line_feedback

    def missing_flag(*args):
        feedback = original(*args)
        feedback['objects'][0].pop('automated_polygon_edit_allowed')
        return feedback

    monkeypatch.setattr(polygons, 'load_roof_line_feedback', missing_flag)
    assert polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)['can_apply'] is False
    monkeypatch.setattr(polygons, 'load_roof_line_feedback', lambda *_: {**original(model['id']), 'model_signature': 'b'*64})
    with pytest.raises(ValueError, match='does not match'):
        polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)


def test_mcp_preview_has_native_image_direct_structured_output_and_annotations(saved, monkeypatch):
    root, model, obj, _ = saved
    preview = polygons.preview_polygon
    monkeypatch.setattr(polygons, 'preview_polygon', lambda *args, **kwargs: preview(*args, **kwargs, root=root))
    result = asyncio.run(server.call_tool('preview_roof_polygon', {'scene_id': model['id'], 'object_id': obj['id'], 'proposed_outline_pixels': PROPOSED}))
    packet = result.model_dump(by_alias=True, exclude_none=True)
    assert packet['structuredContent']['proposed_outline_pixels'] == PROPOSED
    assert packet['content'][0]['type'] == 'text' and packet['content'][1]['type'] == 'image'
    tools = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in asyncio.run(server.list_tools())}
    assert tools['preview_roof_polygon']['annotations']['readOnlyHint'] is True
    assert tools['apply_roof_polygon']['annotations']['readOnlyHint'] is False
    assert tools['apply_roof_polygon']['inputSchema']['required'] == ['preview_id']
    assert 'binding' in tools['preview_roof_polygon']['outputSchema']['required']
    manifest = json.loads(Path(packet['structuredContent']['paths']['manifest']).read_bytes())
    assert hashlib.sha256(polygons._bytes(manifest['content'])).hexdigest() == manifest['preview_id']


def test_boundary_inspection_source_grid_and_nearest_rgb_are_registered(saved):
    root, model, obj, image = saved
    before = source_bytes(root)
    packet = polygons.inspect_boundary(model['id'], obj['id'], root=root)
    assert packet['outline_pixels'] == {'ring': RECT_PX, 'holes': []}
    assert packet['binding']['revision'] == 0 and packet['can_apply']
    assert packet['human_approval'] is False and packet['training_eligible'] is False
    render = packet['render']
    box, size = render['crop_box_pixels'], render['panel_size_pixels']
    assert render['source_crop_size_pixels'] == [box[2]-box[0], box[3]-box[1]]
    assert render['grid']['coordinate_space'] == 'source_image_pixels'
    assert all(x % render['grid']['spacing_pixels'] == 0 for x in render['grid']['x'])
    assert all(y % render['grid']['spacing_pixels'] == 0 for y in render['grid']['y'])
    xy = np.asarray(RECT_PX)
    panel = np.c_[xy, np.ones(len(xy))] @ np.asarray(render['source_to_panel'])
    assert np.allclose(np.c_[panel, np.ones(len(xy))] @ np.asarray(render['panel_to_source']), xy)
    expected = image.crop(box).resize(tuple(size), Image.Resampling.NEAREST)
    with Image.open(packet['paths']['card']) as card:
        crops = [card.crop((x, y, x+size[0], y+size[1])) for x, y in render['panel_origins_in_card_pixels']]
        assert crops[0].tobytes() == expected.tobytes()
        assert crops[1].tobytes() != expected.tobytes() and crops[2].tobytes() != expected.tobytes()
    rings = packet['rings']
    assert rings[0]['vertices'][0] == {'index': 0, 'source_pixels': RECT_PX[0]}
    assert rings[0]['edges'][-1]['end_vertex_index'] == 0
    assert rings[0]['edges'][-1]['source_pixels'] == [RECT_PX[-1], RECT_PX[0]]
    assert source_bytes(root) == before
    assert polygons.inspect_boundary(model['id'], obj['id'], root=root) == packet


def test_boundary_edge_crop_includes_neighbour_corners_and_preserves_saved_holes(saved):
    root, model, obj, _ = saved
    # A concave footprint gives a local neighbourhood smaller than the full object.
    ring = [[20, 20], [230, 20], [230, 200], [170, 200], [170, 90], [80, 90], [80, 200], [20, 200]]
    current = {**obj, 'ring': ring, 'holes': [[[30, 30], [45, 30], [45, 45], [30, 45]]]}
    (root/'data/scenes'/model['id']/'review.json').write_text(json.dumps({'revision': 2, 'model_signature': model['model_signature'], 'objects': [current]}))
    whole = polygons.inspect_boundary(model['id'], obj['id'], root=root)
    packet = polygons.inspect_boundary(model['id'], obj['id'], edge_index=4, root=root)
    assert packet['binding']['revision'] == 2 and packet['outline_pixels']['ring'] == ring
    assert packet['outline_pixels']['holes'] == current['holes']
    assert packet['rings'][1]['ring'] == 'hole' and packet['rings'][1]['hole_index'] == 0
    box = packet['render']['crop_box_pixels']
    assert box[2]-box[0] < whole['render']['crop_box_pixels'][2]-whole['render']['crop_box_pixels'][0]
    for point in [ring[3], ring[4], ring[5], ring[6]]:
        assert box[0] < point[0] < box[2] and box[1] < point[1] < box[3]
    assert packet['render']['selected_outer_edge_index'] == 4


@pytest.mark.parametrize('index', [-1, 100, True, 1.5])
def test_boundary_inspection_invalid_edge_index_writes_no_sidecars(saved, index):
    root, model, obj, _ = saved
    with pytest.raises(ValueError, match='edge_index'):
        polygons.inspect_boundary(model['id'], obj['id'], edge_index=index, root=root)
    assert not (root/polygons.INSPECTION_DIRECTORY).exists()


def test_boundary_inspection_protection_and_source_change_fail_closed(saved, monkeypatch):
    root, model, obj, _ = saved
    current = {**obj, 'protected': True}
    path = root/'data/scenes'/model['id']/'review.json'
    path.write_text(json.dumps({'revision': 3, 'model_signature': model['model_signature'], 'objects': [current]}))
    packet = polygons.inspect_boundary(model['id'], obj['id'], root=root)
    assert not packet['can_apply'] and packet['apply_blocked_reason'] == 'Protected reviewer feedback'
    assert packet['object_state']['automated_polygon_edit_allowed'] is False
    render = polygons._render_inspection

    def changed(*args):
        render(*args)
        path.write_text(json.dumps({'revision': 4, 'model_signature': model['model_signature'], 'objects': [current]}))

    monkeypatch.setattr(polygons, '_render_inspection', changed)
    with pytest.raises(ValueError, match='changed during inspection'):
        polygons.inspect_boundary(model['id'], obj['id'], edge_index=1, root=root)


def test_mcp_boundary_inspection_native_image_and_direct_schema(saved, monkeypatch):
    root, model, obj, _ = saved
    inspect = polygons.inspect_boundary
    monkeypatch.setattr(polygons, 'inspect_boundary', lambda *args, **kwargs: inspect(*args, **kwargs, root=root))
    result = asyncio.run(server.call_tool('inspect_roof_boundary', {'scene_id': model['id'], 'object_id': obj['id'], 'edge_index': 0}))
    packet = result.model_dump(by_alias=True, exclude_none=True)
    assert packet['structuredContent']['outline_pixels']['ring'] == RECT_PX
    assert packet['content'][1]['type'] == 'image'
    tool = next(tool.model_dump(by_alias=True, exclude_none=True) for tool in asyncio.run(server.list_tools()) if tool.name == 'inspect_roof_boundary')
    assert tool['annotations']['readOnlyHint'] is True
    assert tool['inputSchema']['required'] == ['scene_id', 'object_id']
    assert 'outline_pixels' in tool['outputSchema']['required'] and 'render' in tool['outputSchema']['required']


def test_proposed_boundary_inspection_keeps_exact_current_binding_and_nearest_rgb(saved):
    root, model, obj, image = saved
    before = source_bytes(root)
    preview = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    packet = polygons.inspect_boundary(model['id'], obj['id'], preview_id=preview['preview_id'], root=root)
    assert packet['outline_role'] == 'proposed' and packet['preview_id'] == preview['preview_id']
    assert packet['outline_pixels'] == PROPOSED
    assert packet['binding'] == preview['binding']
    assert packet['binding']['outline_pixels'] == {'ring': RECT_PX, 'holes': []}
    assert packet['render']['outline_role'] == 'proposed' and packet['render']['display_resampling'] == 'nearest'
    assert packet['rings'][0]['edges'][-1]['source_pixels'] == [PROPOSED['ring'][-1], PROPOSED['ring'][0]]
    render = packet['render']
    x, y = render['panel_origins_in_card_pixels'][0]
    width, height = render['panel_size_pixels']
    expected = image.crop(render['crop_box_pixels']).resize((width, height), Image.Resampling.NEAREST)
    with Image.open(packet['paths']['card']) as card:
        assert card.crop((x, y, x+width, y+height)).tobytes() == expected.tobytes()
    assert source_bytes(root) == before
    assert polygons.inspect_boundary(model['id'], obj['id'], preview_id=preview['preview_id'], root=root) == packet
    current = polygons.inspect_boundary(model['id'], obj['id'], root=root)
    assert current['outline_role'] == 'current' and current['preview_id'] is None
    assert current['outline_pixels'] != packet['outline_pixels'] and current['inspection_id'] != packet['inspection_id']


@pytest.mark.parametrize('artifact', ['content', 'render', 'card'])
def test_proposal_inspection_rejects_tampered_preview_before_render(saved, artifact):
    root, model, obj, _ = saved
    preview = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    path = Path(preview['paths']['manifest'])
    manifest = json.loads(path.read_bytes())
    if artifact == 'content':
        manifest['content']['proposed_outline_pixels']['ring'][0][0] += 1
    elif artifact == 'render':
        manifest['render']['panel_to_source'][2][0] += 1
    else:
        Path(preview['paths']['card']).write_bytes(b'changed preview image')
    path.write_text(json.dumps(manifest))
    before = source_bytes(root)
    with pytest.raises(ValueError, match='altered'):
        polygons.inspect_boundary(model['id'], obj['id'], preview_id=preview['preview_id'], root=root)
    assert not (root/polygons.INSPECTION_DIRECTORY).exists() and source_bytes(root) == before


@pytest.mark.parametrize('binding_field', ['scene_id', 'object_id', 'revision', 'outline_pixels'])
def test_proposal_inspection_rejects_valid_sidecar_with_foreign_or_mismatched_binding(saved, binding_field):
    root, model, obj, _ = saved
    preview = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    manifest = json.loads(Path(preview['paths']['manifest']).read_bytes())
    binding = manifest['content']['binding']
    if binding_field in ('scene_id', 'object_id'):
        binding[binding_field] = 'other'
    elif binding_field == 'revision':
        binding['revision'] += 1
    else:
        binding['outline_pixels']['ring'][0][0] += 1
    preview_id = polygons._hash(polygons._bytes(manifest['content']))
    directory = polygons._folder(root, preview_id)
    directory.mkdir(parents=True)
    manifest['preview_id'] = preview_id
    (directory/'manifest.json').write_bytes(polygons._bytes(manifest))
    (directory/'card.png').write_bytes(Path(preview['paths']['card']).read_bytes())
    before = source_bytes(root)
    with pytest.raises(ValueError, match='another object|source/revision changed'):
        polygons.inspect_boundary(model['id'], obj['id'], preview_id=preview_id, root=root)
    assert not (root/polygons.INSPECTION_DIRECTORY).exists() and source_bytes(root) == before


def test_proposal_inspection_refuses_preview_after_saved_revision_changes(saved):
    root, model, obj, _ = saved
    preview = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    (root/'data/scenes'/model['id']/'review.json').write_text(json.dumps({'revision': 1, 'model_signature': model['model_signature'], 'objects': [obj]}))
    before = source_bytes(root)
    with pytest.raises(ValueError, match='source/revision changed'):
        polygons.inspect_boundary(model['id'], obj['id'], preview_id=preview['preview_id'], root=root)
    assert not (root/polygons.INSPECTION_DIRECTORY).exists() and source_bytes(root) == before


def test_mcp_proposal_inspection_returns_native_proposed_outline(saved, monkeypatch):
    root, model, obj, _ = saved
    preview = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root)
    inspect = polygons.inspect_boundary
    monkeypatch.setattr(polygons, 'inspect_boundary', lambda *args, **kwargs: inspect(*args, **kwargs, root=root))
    result = asyncio.run(server.call_tool('inspect_roof_boundary', {'scene_id': model['id'], 'object_id': obj['id'], 'preview_id': preview['preview_id'], 'edge_index': 2}))
    packet = result.model_dump(by_alias=True, exclude_none=True)
    assert packet['structuredContent']['outline_role'] == 'proposed'
    assert packet['structuredContent']['outline_pixels'] == PROPOSED
    assert packet['structuredContent']['binding']['outline_pixels'] == {'ring': RECT_PX, 'holes': []}
    assert packet['structuredContent']['preview_id'] == preview['preview_id']
    assert packet['content'][1]['type'] == 'image'


@pytest.mark.parametrize('kind', ['solar_panel', 'tree', 'pool', 'car', 'building'])
def test_generic_object_preview_inspection_and_apply_bind_class_and_preserve_sources(saved, monkeypatch, kind):
    root, model, obj, _ = saved
    obj = {**obj, 'kind': kind}
    model = {**model, 'objects': [obj]}
    (root/'data/scenes'/model['id']/'latest.json').write_text(json.dumps(model))
    before = source_bytes(root)
    preview = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root, object_scope=True)
    assert preview['version'] == polygons.OBJECT_VERSION
    assert preview['binding']['object_kind'] == kind and preview['can_apply']
    inspected = polygons.inspect_boundary(model['id'], obj['id'], preview_id=preview['preview_id'], root=root, object_scope=True)
    assert inspected['outline_role'] == 'proposed' and inspected['outline_pixels'] == PROPOSED
    assert inspected['binding'] == preview['binding']
    calls = []
    monkeypatch.setattr(polygons, 'update_draft_object_polygon', lambda **kwargs: calls.append(kwargs) or {'revision': 1})
    result = polygons.apply_polygon(preview['preview_id'], root=root, object_scope=True)
    assert result['revision'] == 1 and calls[0]['changes']['outline_pixels'] == PROPOSED
    assert source_bytes(root) == before
    with pytest.raises(ValueError, match='matching object or roof'):
        polygons.apply_polygon(preview['preview_id'], root=root)
    if kind != 'building':
        with pytest.raises(ValueError, match='building object'):
            polygons.inspect_boundary(model['id'], obj['id'], root=root)


def test_generic_binding_rejects_changed_canonical_class_and_protected_draft(saved, monkeypatch):
    root, model, obj, _ = saved
    original = polygons.load_object_feedback

    def changed_class(*args):
        result = original(*args)
        result['objects'][0]['kind'] = 'tree'
        return result

    monkeypatch.setattr(polygons, 'load_object_feedback', changed_class)
    with pytest.raises(ValueError, match='class changed'):
        polygons.inspect_boundary(model['id'], obj['id'], root=root, object_scope=True)
    monkeypatch.setattr(polygons, 'load_object_feedback', original)
    current = {**obj, 'protected': True}
    (root/'data/scenes'/model['id']/'review.json').write_text(json.dumps({'revision': 2, 'model_signature': model['model_signature'], 'objects': [current]}))
    preview = polygons.preview_polygon(model['id'], obj['id'], PROPOSED, root=root, object_scope=True)
    assert not preview['can_apply']
    with pytest.raises(ValueError, match='Protected'):
        polygons.apply_polygon(preview['preview_id'], root=root, object_scope=True)


def test_generic_mcp_native_image_contract_and_write_annotations(saved, monkeypatch):
    root, model, obj, _ = saved
    preview = polygons.preview_polygon
    monkeypatch.setattr(polygons, 'preview_polygon', lambda *args, **kwargs: preview(*args, **kwargs, root=root))
    result = asyncio.run(server.call_tool('preview_object_polygon', {'scene_id': model['id'], 'object_id': obj['id'], 'proposed_outline_pixels': PROPOSED}))
    packet = result.model_dump(by_alias=True, exclude_none=True)
    assert packet['structuredContent']['binding']['object_kind'] == 'building'
    assert packet['content'][1]['type'] == 'image'
    tools = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in asyncio.run(server.list_tools())}
    assert tools['inspect_object_boundary']['annotations']['readOnlyHint']
    assert tools['preview_object_polygon']['annotations']['readOnlyHint']
    assert not tools['apply_object_polygon']['annotations']['readOnlyHint']
    assert tools['apply_object_polygon']['inputSchema']['required'] == ['preview_id']
