"""Structured native roof results, current sidecars and canonical draft API writes."""
import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from scripts.ml import scene_line_feedback, scene_roof_observations, scene_roof_review
from scripts.ml.mcp_server import server
from scripts.ml.tests.test_scene_roof_review import saved as saved


def call(name, args):
    return asyncio.run(server.call_tool(name, args)).model_dump(by_alias=True, exclude_none=True)


def test_roof_results_are_direct_structured_payloads_with_native_images(saved, monkeypatch):
    root, scene, _, _ = saved
    packet = scene_roof_review.review_roof(scene['id'], 'roof', root)
    monkeypatch.setattr(scene_roof_review, 'review_roof', lambda *a, **kw: packet.copy())
    result = call('scene_roof_review', {'scene_id': scene['id'], 'object_id': 'roof', 'include_source': True})
    structured = result['structuredContent']
    assert structured['review_id'] == packet['review_id'] and 'result' not in structured
    assert json.loads(result['content'][0]['text']) == structured
    assert [block['type'] for block in result['content']] == ['text', 'image', 'image']
    assert structured['editor_url'].endswith(f'/scenes?scene={scene["id"]}&object=roof')
    assert 'layer=model' not in structured['editor_url']
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in ('scene_roof_review', 'low_confidence_roofs', 'record_roof_observation', 'roof_observations'):
        assert tools[name].meta['ui']['visibility'] == ['app', 'model']
        assert tools[name].output_schema['type'] == 'object'
    assert 'review_id' in tools['scene_roof_review'].output_schema['required']
    assert 'objects' in tools['roof_line_feedback'].output_schema['required']
    assert tools['update_draft_roof_feedback'].annotations.read_only_hint is False


def test_observations_read_without_rendering_and_refuse_altered_payloads(saved, monkeypatch):
    root, scene, _, _ = saved
    packet = scene_roof_review.review_roof(scene['id'], 'roof', root)
    note = scene_roof_review.record_observation(packet['review_id'], observed='Visible brightness change.', root=root)
    monkeypatch.setattr(scene_roof_review, 'render_card', lambda *a, **kw: pytest.fail('Read must not render'))
    result = scene_roof_observations.list_observations(scene['id'], 'roof', root=root)
    assert result['identity'] == packet['identity'] and result['scope'] == packet['scope']
    assert result['observations'] == [note['observation']]
    assert result['training_eligible'] is False and result['human_approval'] is False
    model_path = root/'data/scenes'/scene['id']/'latest.json'
    original = model_path.read_bytes()
    changed = json.loads(original)
    changed['title'] = 'Changed source model bytes'
    model_path.write_text(json.dumps(changed))
    assert scene_roof_observations.list_observations(scene['id'], 'roof', root=root)['observations'] == []
    model_path.write_bytes(original)
    path = Path(note['path'])
    altered = json.loads(path.read_text())
    altered['observed'] = 'Changed note'
    path.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match='altered'):
        scene_roof_observations.list_observations(scene['id'], 'roof', root=root)


def test_editor_url_requires_safe_http_app_location(monkeypatch):
    monkeypatch.setenv('ADDR_APP_URL', 'https://addr.example/review')
    assert scene_roof_observations.editor_url('scene', 'roof') == 'https://addr.example/review/scenes?scene=scene&object=roof'
    for invalid in ('file:///tmp', 'https://user:secret@example.com', 'https://example.com?token=x', 'javascript:alert(1)'):
        monkeypatch.setenv('ADDR_APP_URL', invalid)
        with pytest.raises(ValueError):
            scene_roof_observations.editor_url('scene', 'roof')


def test_observation_reader_does_not_load_the_vision_stack():
    check = subprocess.run([sys.executable, '-c',
        'import sys; from scripts.ml import scene_roof_observations; '
        'assert not any(name in sys.modules for name in ("cv2", "torch", "numpy", "scripts.ml.scene_roof_review"))'],
        cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def draft():
    return dict(scene_id='scene', object_id='roof', expected_revision=7, model_signature='a'*64,
                image_sha256='b'*64, outline_pixels={'ring': [[10, 10], [20, 10], [20, 20]], 'holes': []},
                changes={'notes': 'Unchecked evidence'})


def acknowledgment():
    return {'schema_version': 1, 'scene_id': 'scene', 'object_id': 'roof', 'revision': 8,
            'model_signature': 'a'*64, 'image_sha256': 'b'*64, 'object': {'id': 'roof'},
            'training_eligible': False, 'human_approval': False}


def test_draft_client_delegates_exact_patch_with_auth_without_storage_writes(monkeypatch):
    calls = []
    def respond(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(json.dumps(acknowledgment()).encode())
    monkeypatch.setattr(scene_line_feedback, 'urlopen', respond)
    monkeypatch.setenv('CRM_TOKEN', 'test-token')
    result = scene_line_feedback.update_draft_roof_feedback(**draft(), base_url='http://localhost:3008')
    request, timeout = calls[0]
    assert request.method == 'PATCH' and request.full_url.endswith('/api/scenes/roof-feedback')
    assert request.get_header('Authorization') == 'Bearer test-token' and timeout == 30
    assert json.loads(request.data) == draft()
    assert result == acknowledgment()


@pytest.mark.parametrize('field,value', [('scene_id', 'other'), ('revision', 7), ('model_signature', 'c'*64),
                                      ('image_sha256', 'c'*64), ('human_approval', True), ('training_eligible', True)])
def test_draft_client_refuses_ambiguous_or_incompatible_ack(monkeypatch, field, value):
    response = {**acknowledgment(), field: value}
    monkeypatch.setattr(scene_line_feedback, 'urlopen', lambda *a, **kw: io.BytesIO(json.dumps(response).encode()))
    with pytest.raises(ValueError, match='Reload'):
        scene_line_feedback.update_draft_roof_feedback(**draft())


def test_draft_client_preserves_conflicts_and_never_retries(monkeypatch):
    calls = []
    def conflict(*args, **kwargs):
        calls.append(True)
        raise HTTPError('http://localhost', 409, 'Conflict', {}, io.BytesIO(b'{"error":"Revision changed"}'))
    monkeypatch.setattr(scene_line_feedback, 'urlopen', conflict)
    with pytest.raises(ValueError, match='409: Revision changed'):
        scene_line_feedback.update_draft_roof_feedback(**draft())
    assert len(calls) == 1
    monkeypatch.setattr(scene_line_feedback, 'urlopen', lambda *a, **kw: (_ for _ in ()).throw(URLError('private detail')))
    with pytest.raises(ValueError, match='save may have completed'):
        scene_line_feedback.update_draft_roof_feedback(**draft())


def test_draft_client_rejects_nonfinite_and_missing_identity_before_request(monkeypatch):
    monkeypatch.setattr(scene_line_feedback, 'urlopen', lambda *a, **kw: pytest.fail('Must validate before request'))
    for fields in ({'expected_revision': True}, {'scene_id': '../scene'}, {'model_signature': ''},
                   {'changes': {'roof_line_edits': {'vertices_meters': [[float('nan'), 1]]}}}):
        with pytest.raises(ValueError):
            scene_line_feedback.update_draft_roof_feedback(**(draft() | fields))


def test_expected_mcp_conflicts_are_actionable_tool_errors(monkeypatch):
    def conflict(**kwargs):
        raise ValueError('409: Source changed. Reload before editing.')
    monkeypatch.setattr(scene_line_feedback, 'update_draft_roof_feedback', conflict)
    with pytest.raises(ToolError, match='409: Source changed'):
        call('update_draft_roof_feedback', draft())
