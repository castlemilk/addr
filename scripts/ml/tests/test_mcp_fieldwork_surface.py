"""The local MCP push/artefact tools are thin wrappers over modules that may not exist yet.

They must import lazily (the server loads without them), forward arguments by the spec-defined
function names, expose the wording invariants in their descriptions and never leak a token.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

PUSH_TOOLS = ('plan_fieldwork_push', 'apply_fieldwork_push', 'fieldwork_push_status')
TRAINING_TOOLS = ('publish_training_artefacts', 'list_training_manifests', 'pull_training_artefacts')


def _stub(monkeypatch, name, **functions):
    module = types.ModuleType(name)
    for attr, function in functions.items():
        setattr(module, attr, function)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _call(tool, arguments):
    """Return (is_error, payload): mcp 2.x raises ToolError for anticipated failures (client sees isError)."""
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    from scripts.ml.mcp_server import server

    try:
        result = asyncio.run(server.call_tool(tool, arguments)).model_dump(by_alias=True)
    except UnexpectedToolError as error:  # text hidden from the client; a crash, not an anticipated refusal
        pytest.fail(f'{tool} crashed instead of raising ToolError: {error!r} caused by {error.__cause__!r}')
    except ToolError as error:
        return True, str(error)
    text = result['content'][0]['text']
    assert not result['isError']
    return False, json.loads(text)


def test_server_loads_without_push_modules_and_reports_missing_module_clearly(monkeypatch):
    for name in ('scripts.ml.fieldwork_push', 'scripts.ml.training.publish_artefacts',
                 'scripts.ml.training.pull_artefacts'):
        monkeypatch.delitem(sys.modules, name, raising=False)
    from scripts.ml.mcp_server import server

    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert set(PUSH_TOOLS) | set(TRAINING_TOOLS) <= names
    is_error, text = _call('fieldwork_push_status', {})
    if is_error:
        assert 'scripts.ml.fieldwork_push is not available' in text
        assert 'local-to-fieldwork.md' in text
    else:  # the module has landed; the wrapper must then answer without network
        assert isinstance(text, dict)


def test_descriptions_state_the_wording_invariants():
    from scripts.ml.mcp_server import server

    tools = {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
    for name in PUSH_TOOLS:
        text = tools[name]
        assert 'never edits Fieldwork overrides' in text, name
        assert 'never a human label' in text, name
        assert 'No training is started' in text, name
        assert 'never returned' in text, name
    for name in TRAINING_TOOLS:
        text = tools[name]
        assert 'No training is started' in text, name
        assert 'never human labels' in text, name
        assert 'never returned' in text, name
    for name in ('apply_fieldwork_push', 'publish_training_artefacts'):
        assert 'FIELDWORK_PUSH_ALLOW_LIVE=1' in tools[name], name


def test_plan_forwards_dry_run_and_never_returns_the_token(monkeypatch):
    calls = []
    _stub(monkeypatch, 'scripts.ml.fieldwork_push',
          push_fieldwork=lambda **kwargs: calls.append(kwargs) or {'runId': 'r1', 'counts': {'create': 2}},
          push_status=lambda **kwargs: {})
    is_error, result = _call('plan_fieldwork_push', {'scenes': ['house-25822'], 'remote_url': 'http://127.0.0.1:18131',
                                                     'token_file': '/tmp/secret-token.txt'})
    assert not is_error and result == {'runId': 'r1', 'counts': {'create': 2}}
    assert calls == [{'project_id': None, 'scenes': ['house-25822'], 'dry_run': True, 'mirror_dir': None,
                      'remote_url': 'http://127.0.0.1:18131', 'token_file': '/tmp/secret-token.txt'}]
    assert 'secret-token' not in json.dumps(result)


def test_apply_forwards_run_id_and_allow_unrecovered(monkeypatch):
    calls = []
    _stub(monkeypatch, 'scripts.ml.fieldwork_push',
          push_fieldwork=lambda **kwargs: calls.append(kwargs) or {'runId': kwargs['run_id'], 'created': 1},
          push_status=lambda **kwargs: {})
    is_error, result = _call('apply_fieldwork_push', {'run_id': '20260929T000000Z-deadbeef', 'allow_unrecovered': True})
    assert not is_error and result['created'] == 1
    assert calls == [{'project_id': None, 'run_id': '20260929T000000Z-deadbeef', 'dry_run': False,
                      'remote_url': None, 'token_file': None, 'allow_unrecovered': True}]


def test_apply_requires_run_id():
    from scripts.ml.mcp_server import server

    tools = {tool.name: tool.model_dump(by_alias=True) for tool in asyncio.run(server.list_tools())}
    assert tools['apply_fieldwork_push']['inputSchema']['required'] == ['run_id']


def test_status_reads_receipts_only_and_narrows_to_a_scene(monkeypatch):
    calls = []
    status = {'project': 'mooroolbark', 'mirrorCursor': '538',
              'scenes': [{'sceneId': 'house-25822', 'latestItemId': 'addr-scene-house-25822-m8657343a6e23-r35'},
                         {'sceneId': 'house-30286', 'latestItemId': 'addr-scene-house-30286'}]}
    _stub(monkeypatch, 'scripts.ml.fieldwork_push',
          push_fieldwork=lambda **kwargs: pytest.fail('status must not push'),
          push_status=lambda **kwargs: calls.append(kwargs) or status)
    is_error, result = _call('fieldwork_push_status', {'scene_id': 'house-25822'})
    assert not is_error and calls == [{'project_id': None}]
    assert [entry['sceneId'] for entry in result['scenes']] == ['house-25822']
    is_error, result = _call('fieldwork_push_status', {})
    assert not is_error and len(result['scenes']) == 2


def test_publish_defaults_to_dry_run_and_forwards_selection(monkeypatch):
    calls = []
    _stub(monkeypatch, 'scripts.ml.training.publish_artefacts',
          publish=lambda select, **kwargs: calls.append((select, kwargs)) or {'dryRun': kwargs['dry_run'],
                                                                              'groups': {g: 1 for g in select}})
    is_error, result = _call('publish_training_artefacts', {'select': ['dataset-v2', 'eyes']})
    assert not is_error and result['dryRun'] is True and set(result['groups']) == {'dataset-v2', 'eyes'}
    assert calls == [(['dataset-v2', 'eyes'], {'dry_run': True, 'url': None, 'token_file': None,
                                                'with_images': False, 'tarball': False})]
    _call('publish_training_artefacts', {'select': ['sweep'], 'dry_run': False, 'with_images': True,
                                         'remote_url': 'http://127.0.0.1:18131', 'token_file': '/tmp/t'})
    assert calls[-1] == (['sweep'], {'dry_run': False, 'url': 'http://127.0.0.1:18131', 'token_file': '/tmp/t',
                                     'with_images': True, 'tarball': False})


def test_list_and_pull_forward_by_spec_names(monkeypatch):
    calls = []
    _stub(monkeypatch, 'scripts.ml.training.pull_artefacts',
          list_manifests=lambda url, token_file, **kwargs: calls.append(('list', url, token_file, kwargs)) or [
              {'itemId': 'training-manifest-20260929-abc', 'manifestSha256': 'abc'}],
          pull=lambda url, token_file, out, **kwargs: calls.append(('pull', url, token_file, out, kwargs)) or {
              'out': out, 'filesVerified': 3})
    is_error, result = _call('list_training_manifests', {'remote_url': 'http://127.0.0.1:18131', 'limit': 3})
    assert not is_error and result[0]['itemId'].startswith('training-manifest-')
    assert calls[-1] == ('list', 'http://127.0.0.1:18131', None, {'project': 'addr-training', 'limit': 3})
    is_error, result = _call('pull_training_artefacts', {'out': '/tmp/addr-training-pull', 'latest': True,
                                                         'only': ['dataset-v2', 'edit-diffs'], 'with_scenes': True})
    assert not is_error and result == {'out': '/tmp/addr-training-pull', 'filesVerified': 3}
    assert calls[-1] == ('pull', None, None, '/tmp/addr-training-pull',
                         {'project': 'addr-training', 'manifest': None, 'latest': True,
                          'only': ['dataset-v2', 'edit-diffs'], 'with_scenes': True, 'resume': False})


def test_module_errors_surface_as_tool_errors_not_tracebacks(monkeypatch):
    def refuse(**kwargs):
        raise RuntimeError('refusing the live Fieldwork origin without FIELDWORK_PUSH_ALLOW_LIVE=1')

    _stub(monkeypatch, 'scripts.ml.fieldwork_push', push_fieldwork=refuse, push_status=lambda **kwargs: {})
    is_error, text = _call('apply_fieldwork_push', {'run_id': 'r1'})
    assert is_error and 'FIELDWORK_PUSH_ALLOW_LIVE=1' in text
