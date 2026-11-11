"""Exact pure partitions, sealed native-image proofs and machine child binding."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from scripts.ml import scene_line_feedback as feedback
from scripts.ml import scene_object_split as splits
from scripts.ml import scene_polygon_feedback as polygons
from scripts.ml.mcp_server import server
from scripts.ml.scene_instances import partition_outlines
from scripts.ml.scene_roof_observations import _identity
from scripts.ml.tests.test_scene_polygon_feedback import saved as saved_fixture
from scripts.ml.tests.test_scene_polygon_feedback import source_bytes

saved = saved_fixture

SOURCE = {'ring': [[64, 80], [192, 80], [192, 176], [64, 176]], 'holes': []}
LEFT = {'ring': [[64, 80], [128, 80], [128, 176], [64, 176]], 'holes': []}
RIGHT = {'ring': [[128, 80], [192, 80], [192, 176], [128, 176]], 'holes': []}
CHILDREN = [{'key': 'left', 'outline_pixels': LEFT, 'label': 'Visible crown patch'},
            {'key': 'right', 'outline_pixels': RIGHT, 'label': 'Unresolved crown remainder', 'role': 'unresolved-residual'}]


def preview(saved, **kwargs):
    root, model, obj, _ = saved
    return splits.preview_split(model['id'], obj['id'], copy.deepcopy(CHILDREN), root=root, **kwargs)


def test_partition_retains_exact_supplied_rings_and_holes():
    hole = [[85, 100], [100, 100], [100, 120], [85, 120]]
    source = {**SOURCE, 'holes': [hole]}
    children = [{**LEFT, 'holes': [hole]}, RIGHT]
    result = partition_outlines(source, children)
    assert result['rings'] == [LEFT['ring'], RIGHT['ring']]
    assert result['holes'] == [[hole], []]
    assert result['children'] == {}
    assert result['area_px'] == result['source_area_px'] == 12288-300
    assert all(result[key] == 0 for key in ('outside_area_px', 'overlap_area_px', 'missing_area_px'))


@pytest.mark.parametrize('bad', ['missing', 'overlap', 'outside', 'hole', 'tiny', 'invalid', 'count', 'boolean'])
def test_partition_refuses_lost_pixels_overlap_gaps_and_invalid_parts(bad):
    source, children = copy.deepcopy(SOURCE), copy.deepcopy([LEFT, RIGHT])
    if bad == 'missing':
        children[0]['ring'][1][0] = children[0]['ring'][2][0] = 127
    elif bad == 'overlap':
        children[0]['ring'][1][0] = children[0]['ring'][2][0] = 129
    elif bad == 'outside':
        children[1]['ring'][1][0] = children[1]['ring'][2][0] = 193
    elif bad == 'hole':
        source['holes'] = [[[85, 100], [100, 100], [100, 120], [85, 120]]]
    elif bad == 'tiny':
        children[0]['ring'] = [[64, 80], [64.01, 80], [64.01, 176], [64, 176]]
    elif bad == 'invalid':
        children[0]['ring'] = [[64, 80], [128, 176], [128, 80], [64, 176]]
    elif bad == 'count':
        children = [LEFT]
    else:
        children[0]['ring'][0][0] = True
    with pytest.raises(ValueError):
        partition_outlines(source, children)


def test_native_preview_is_sealed_source_bound_and_exactly_registered(saved):
    root, model, obj, image = saved
    before = source_bytes(root)
    packet = preview(saved, group_label='Jardin 🌳', notes='Scratch partition only', uncertainty='Crown patches do not imply individual trees')
    assert packet['can_apply'] and packet['tier'] == 3 and packet['object_kind'] == 'building'
    assert packet['training_eligible'] is False and packet['human_approval'] is False
    assert packet['binding']['outline_pixels'] == SOURCE and packet['binding']['object_kind'] == 'building'
    assert packet['inherited_group_id'] is None
    expected_group = 'splitgroup_'+hashlib.sha256(json.dumps([model['id'], obj['id'], model['model_signature'], 'Jardin 🌳'], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:32]
    assert packet['group'] == {'id': expected_group, 'label': 'Jardin 🌳'}
    assert packet['children'][1]['role'] == 'unresolved-residual'
    render = packet['render']
    size = render['panel_size_pixels']
    x, y = render['panel_origins_in_card_pixels'][0]
    original = image.crop(render['crop_box_pixels']).resize(tuple(size), Image.Resampling.NEAREST)
    with Image.open(packet['paths']['card']) as card:
        assert card.crop((x, y, x+size[0], y+size[1])).tobytes() == original.tobytes()
        px, py = render['panel_origins_in_card_pixels'][2]
        assert card.crop((px, py, px+size[0], py+size[1])).tobytes() != original.tobytes()
    assert source_bytes(root) == before
    proof = splits.verify_for_api(packet['preview_id'], root=root)
    assert proof['content'] == splits.read_preview(packet['preview_id'], root=root)
    assert proof['content']['coverage']['missing_area_px'] == 0
    assert preview(saved, group_label='Jardin 🌳', notes='Scratch partition only', uncertainty='Crown patches do not imply individual trees')['preview_id'] == packet['preview_id']
    changed_notes = preview(saved, notes='Different wording')
    assert changed_notes['preview_id'] != packet['preview_id']
    assert [child['object_id'] for child in changed_notes['children']] == [child['object_id'] for child in packet['children']]


@pytest.mark.parametrize('change', ['class', 'child-id', 'card', 'render'])
def test_altered_manifest_or_audit_image_refuses_before_api(saved, monkeypatch, change):
    root, *_ = saved
    packet = preview(saved)
    manifest = Path(packet['paths']['manifest'])
    data = json.loads(manifest.read_bytes())
    if change == 'card':
        Path(packet['paths']['card']).write_bytes(b'changed native image')
    else:
        if change == 'class':
            data['content']['object_kind'] = 'tree'
        elif change == 'child-id':
            data['content']['children'][0]['object_id'] = 'foreign'
        else:
            data['content']['render']['display_scale'][0] += 1
        manifest.write_text(json.dumps(data))
    calls = []
    monkeypatch.setattr(feedback, 'apply_object_split', lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match='altered'):
        splits.apply_split(packet['preview_id'], root=root)
    assert not calls


@pytest.mark.parametrize('change', ['source', 'image', 'revision'])
def test_source_and_revision_cas_refuses_stale_preview(saved, monkeypatch, change):
    root, model, obj, _ = saved
    packet = preview(saved)
    folder = root/'data/scenes'/model['id']
    if change == 'source':
        (folder/'latest.json').write_text(json.dumps({**model, 'title': 'New source bytes'}))
    elif change == 'image':
        (folder/f"{model['image_sha256']}.png").write_bytes(b'Changed image')
    else:
        (folder/'review.json').write_text(json.dumps({'revision': 1, 'model_signature': model['model_signature'], 'objects': [obj]}))
    calls = []
    monkeypatch.setattr(feedback, 'apply_object_split', lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match='changed|fresh'):
        splits.apply_split(packet['preview_id'], root=root)
    assert not calls


def test_child_and_edge_inspection_remain_original_rgb_with_indices(saved):
    root, *_ = saved
    packet = preview(saved)
    whole = splits.inspect_split(packet['preview_id'], root=root)
    child = splits.inspect_split(packet['preview_id'], 'right', 0, root=root)
    assert whole['outline_role'] == 'proposed_partition'
    assert child['outline_role'] == 'proposed_split_child' and child['outline_pixels'] == RIGHT
    assert child['child']['role'] == 'unresolved-residual' and child['render']['selected_outer_edge_index'] == 0
    assert Path(child['paths']['card']).is_file() and child['human_approval'] is False
    for args in [(None, 0), ('absent', None), ('right', 4)]:
        with pytest.raises(ValueError):
            splits.inspect_split(packet['preview_id'], *args, root=root)


@pytest.mark.parametrize('field', ['protected', 'geometry', 'status', 'attached', 'collision'])
def test_protection_and_physical_children_preserved(saved, field):
    root, model, obj, _ = saved
    current = copy.deepcopy(obj)
    objects = [current]
    if field == 'protected':
        current['protected'] = True
    elif field == 'geometry':
        current['geometry_reviewed'] = True
    elif field == 'status':
        current['status'] = 'confirmed'
    elif field == 'attached':
        objects.append({**obj, 'id': 'attached', 'parent_id': obj['id']})
    else:
        # Stable identities depend on original source binding, not the revision.
        binding = polygons._current(root, model['id'], obj['id'], object_scope=True)[0]
        objects.append({**obj, 'id': splits.child_id(binding, 'left')})
    (root/'data/scenes'/model['id']/'review.json').write_text(json.dumps({'revision': 1, 'model_signature': model['model_signature'], 'objects': objects}))
    packet = preview(saved)
    assert packet['can_apply'] is False
    with pytest.raises(ValueError):
        splits.verify_for_api(packet['preview_id'], root=root)


@pytest.mark.parametrize('bad', [{'kind': 'tree'}, {'object_id': 'foreign'}, {'geometry_reviewed': True}, {'role': 'approved'}, {'key': 'left/right'}])
def test_children_cannot_change_class_ids_authority_or_keys(saved, bad):
    root, model, obj, _ = saved
    children = copy.deepcopy(CHILDREN)
    children[0].update(bad)
    with pytest.raises(ValueError):
        splits.preview_split(model['id'], obj['id'], children, root=root)


def test_apply_uses_only_preview_id_and_verified_identity(saved, monkeypatch):
    root, model, obj, _ = saved
    packet = preview(saved)
    calls = []
    monkeypatch.setattr(feedback, 'apply_object_split', lambda **kwargs: calls.append(kwargs) or {'revision': 1})
    before = source_bytes(root)
    result = splits.apply_split(packet['preview_id'], root=root)
    assert result['revision'] == 1 and calls == [{'preview_id': packet['preview_id'], 'scene_id': model['id'], 'object_id': obj['id'],
        'expected_revision': 0, 'model_signature': model['model_signature'], 'image_sha256': model['image_sha256'],
        'expected_child_ids': [child['object_id'] for child in packet['children']], 'expected_kind': 'building'}]
    assert source_bytes(root) == before


@pytest.mark.parametrize('tamper', [None, 'manual', 'foreign-kind', 'foreign-parent', 'missing-origin', 'human', 'import'])
def test_review_only_identity_requires_trusted_machine_lineage(saved, tamper):
    root, model, obj, _ = saved
    child = {**copy.deepcopy(obj), 'id': 'split_fixture', 'source': 'machine:mcp-object-split-draft-v1', 'derived_from': [obj['id']],
        'roof_line_edits': None, 'provenance': {'source': 'machine:mcp-polygon-draft-v1', 'draft_fingerprint': 'verified-by-api',
          'source_scene_id': model['id'], 'source_model_signature': model['model_signature'], 'source_image_sha256': model['image_sha256'],
          'split_preview_id': 'c'*64, 'human_approval': False, 'human_confirmed': False}}
    parent = {**obj, 'status': 'superseded', 'replacement_ids': [child['id']]}
    if tamper == 'manual':
        child['source'] = 'reviewer-split'
    elif tamper == 'foreign-kind':
        child['kind'] = 'tree'
    elif tamper == 'foreign-parent':
        parent['replacement_ids'] = ['foreign']
    elif tamper == 'missing-origin':
        del child['provenance']['source_scene_id']
    elif tamper == 'human':
        child['provenance']['human_confirmed'] = True
    elif tamper == 'import':
        child['fieldwork_import'] = {'pending': True}
    (root/'data/scenes'/model['id']/'review.json').write_text(json.dumps({'revision': 1, 'model_signature': model['model_signature'], 'objects': [parent, child]}))
    if tamper:
        with pytest.raises(ValueError, match='trusted'):
            _identity(root, model['id'], child['id'], require_building=False)
    else:
        assert _identity(root, model['id'], child['id'], require_building=False)['identity_origin'] == 'saved-review'
        with pytest.raises(ValueError):
            _identity(root, model['id'], child['id'])


def test_mcp_split_contract_has_native_images_and_fixed_write_signature(saved, monkeypatch):
    root, model, obj, _ = saved
    monkeypatch.chdir(root)
    async def exercise():
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert tools['preview_object_split'].annotations.read_only_hint is True
        assert tools['inspect_object_split'].annotations.read_only_hint is True
        assert tools['apply_object_split'].annotations.read_only_hint is False
        assert set(tools['apply_object_split'].input_schema['properties']) == {'preview_id'}
        result = await server.call_tool('preview_object_split', {'scene_id': model['id'], 'object_id': obj['id'], 'children': CHILDREN})
        assert not result.is_error and any(block.type == 'image' for block in result.content)
        assert result.structured_content['object_kind'] == 'building'
        inspected = await server.call_tool('inspect_object_split', {'preview_id': result.structured_content['preview_id'], 'child_key': 'left', 'edge_index': 1})
        assert not inspected.is_error and any(block.type == 'image' for block in inspected.content)
    asyncio.run(exercise())
