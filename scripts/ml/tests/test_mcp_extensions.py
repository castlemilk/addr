"""Exercise extension discovery and search-to-resource reads through the MCP SDK."""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from scripts.ml import mcp_resources
from scripts.ml.mcp_server import WORKSPACE_URI, server


@pytest.fixture
def saved_resources(tmp_path, monkeypatch):
    records = [
        {'address_id': 29790, 'full_address': '27 CAMBRIDGE ROAD', 'kind': 'house', 'area_m2': 120,
         'boundary_source': 'crm', 'roof_form_source': 'reviewer'},
        {'address_id': 30286, 'full_address': '1 CAMBRIDGE ROAD', 'kind': 'units', 'area_m2': 90},
    ]
    monkeypatch.setattr(mcp_resources, 'load_records', lambda: records)
    monkeypatch.setattr(mcp_resources, 'SCENES_ROOT', tmp_path)
    directory = tmp_path / 'house-29790'
    directory.mkdir()
    model = {'id': 'house-29790', 'title': 'Cambridge capture', 'objects': [], 'model_signature': 'frozen'}
    (directory / 'latest.json').write_text(json.dumps(model))
    # Search must not incorporate, edit or promote a separately saved review.
    (directory / 'review.json').write_text('{"revision":7,"status":"draft"}')
    (tmp_path / 'unfinished').mkdir()
    qa = tmp_path / 'qa_scratch'
    qa.mkdir()
    (qa / 'latest.json').write_text(json.dumps({'id': 'qa_scratch', 'title': 'Cambridge QA'}))
    return tmp_path, records, model


def call_tool(name, arguments):
    return asyncio.run(server.call_tool(name, arguments)).model_dump(by_alias=True, exclude_none=True)


def read_resource(uri):
    contents = list(asyncio.run(server.read_resource(uri)))
    assert len(contents) == 1
    assert contents[0].mime_type == 'application/json'
    return json.loads(contents[0].content)


def test_native_mentions_advertise_app_visibility_and_structured_schema():
    tools = {tool.name: tool.model_dump(by_alias=True, exclude_none=True)
             for tool in asyncio.run(server.list_tools())}
    search = tools['search_mentions']
    assert search['_meta'] == {'openai/extensions': {'mentions/search': {}}, 'ui': {'visibility': ['app']}}
    assert search['inputSchema']['required'] == ['query']
    assert search['inputSchema']['properties']['query']['type'] == 'string'
    assert search['outputSchema']['required'] == ['items']
    assert search['annotations']['readOnlyHint'] is True
    # Existing model-facing tools and push safeguards remain available.
    assert {'search_addresses', 'address_metadata', 'scene_roof_review', 'apply_fieldwork_push'} <= tools.keys()


def test_search_to_read_lifecycle_keeps_models_and_reviews_separate(saved_resources):
    directory, records, model = saved_resources
    before = {path: path.read_bytes() for path in directory.rglob('*.json')}
    result = call_tool('search_mentions', {'query': '  cAmBrIdGe  '})
    assert result['content'] == []
    items = result['structuredContent']['items']
    assert [item['uri'] for item in items] == [
        'addr://addresses/30286', 'addr://addresses/29790', 'addr://scenes/house-29790',
    ]
    assert all(item['type'] == 'resource_link' and item['mimeType'] == 'application/json' for item in items)
    assert read_resource(items[1]['uri']) == {'record': records[0], 'provenance': mcp_resources.ADDRESS_NOTE}
    assert 'CRM or reviewer' in read_resource(items[1]['uri'])['provenance']
    assert read_resource(items[2]['uri']) == {'model': model, 'provenance': mcp_resources.MODEL_NOTE}
    assert {path: path.read_bytes() for path in directory.rglob('*.json')} == before


def test_empty_missing_and_id_queries_are_supported(saved_resources):
    assert len(call_tool('search_mentions', {'query': ''})['structuredContent']['items']) == 3
    assert call_tool('search_mentions', {'query': 'missing'})['structuredContent'] == {'items': []}
    assert {item['uri'] for item in call_tool('search_mentions', {'query': '29790'})['structuredContent']['items']} == {
        'addr://addresses/29790', 'addr://scenes/house-29790',
    }
    assert call_tool('app_search_resources', {'query': 'cambridge'})['structuredContent']['items'] == \
        call_tool('search_mentions', {'query': 'cambridge'})['structuredContent']['items']


def test_search_is_bounded_and_missing_local_data_is_an_empty_catalogue(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_resources, 'SCENES_ROOT', tmp_path / 'missing')
    monkeypatch.setattr(mcp_resources, 'load_records', lambda: [
        {'address_id': i + 1, 'full_address': f'ADDRESS {i:03}'} for i in range(60)
    ])
    assert len(mcp_resources.search_resources('')) == mcp_resources.SEARCH_LIMIT

    def missing():
        raise FileNotFoundError('no generated records')

    monkeypatch.setattr(mcp_resources, 'load_records', missing)
    assert mcp_resources.search_resources('') == []


def test_scene_title_cache_refreshes_when_the_model_changes(saved_resources):
    directory, _, model = saved_resources
    assert len(mcp_resources.search_resources('Cambridge capture')) == 1
    (directory / 'house-29790' / 'latest.json').write_text(json.dumps({**model, 'title': 'Updated capture title'}))
    assert not mcp_resources.search_resources('Cambridge capture')
    assert len(mcp_resources.search_resources('Updated capture title')) == 1


@pytest.mark.parametrize('address_id', ['0', '-1', '../29790', '29790?token=secret', 'missing', '00029790'])
def test_address_ids_cannot_escape_the_resource_catalogue(saved_resources, address_id):
    with pytest.raises(ValueError):
        mcp_resources.read_address(address_id)


@pytest.mark.parametrize('scene_id', ['../house-29790', '/house-29790', 'house-29790/latest.json', '%2e%2e', 'missing'])
def test_scene_ids_cannot_escape_the_resource_catalogue(saved_resources, scene_id):
    with pytest.raises(ValueError):
        mcp_resources.read_scene(scene_id)


def test_symlinked_scene_models_are_not_resources(saved_resources, tmp_path):
    directory, _, _ = saved_resources
    outside = tmp_path / 'private.json'
    outside.write_text('{"token":"secret"}')
    target = directory / 'linked'
    target.mkdir()
    (target / 'latest.json').symlink_to(outside)
    assert not mcp_resources.search_resources('linked')
    with pytest.raises(ValueError, match='saved scene models'):
        mcp_resources.read_scene('linked')


@pytest.mark.parametrize('uri', ['addr://addresses/99999', 'addr://scenes/missing'])
def test_missing_resource_reads_are_expected_protocol_errors(saved_resources, uri):
    with pytest.raises(ResourceNotFoundError):
        asyncio.run(server.read_resource(uri))


def test_resource_templates_and_empty_argument_entrypoints(saved_resources):
    templates = {template.uri_template for template in asyncio.run(server.list_resource_templates())}
    assert templates == {'addr://addresses/{address_id}', 'addr://scenes/{scene_id}'}
    tool = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == 'open_review_workspace')
    assert tool.title == 'Address reviews' and tool.icons
    assert tool.meta['ui']['resourceUri'] == WORKSPACE_URI
    assert tool.meta['openai/ui']['entrypoints'] == [{'type': 'global'}, {'type': 'thread'}]
    assert not tool.input_schema.get('required')
    result = call_tool('open_review_workspace', {})
    assert result['structuredContent']['title'] == 'Address reviews'
    assert len(result['structuredContent']['items']) == 3
    # Text fallback remains useful to MCP clients without an Apps host.
    assert '27 CAMBRIDGE ROAD' in result['content'][0]['text']


def test_sidebar_resource_serves_bundled_html_and_display_metadata():
    contents = list(asyncio.run(server.read_resource(WORKSPACE_URI)))
    assert len(contents) == 1 and contents[0].mime_type == 'text/html;profile=mcp-app'
    assert '<title>Address reviews</title>' in contents[0].content
    assert '<script>' in contents[0].content and '<style>' in contents[0].content
    assert contents[0].meta['openai/ui']['preferredDisplayMode'] == 'fullscreen'
    assert contents[0].meta['ui']['csp'] == {'connectDomains': [], 'resourceDomains': []}
