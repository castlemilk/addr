"""Canonical delegation, source/revision refusals and field preservation."""
from __future__ import annotations

import asyncio
import copy
import io
import json
from urllib.error import HTTPError, URLError

import pytest
from scripts.ml import roof_feedback_client as client

BOUNDARY = '145.0000000,-37.0000000|0.00,0.00;5.00,0.00;5.00,5.00'
MODEL = 'a' * 64
CUT = [[0, 0], [4, 0]]


def entry(feedback=None):
    return {'address_id': 27, 'boundary_signature': BOUNDARY, 'model_signature': MODEL,
            'stale_review': False, 'annotation_stale': False,
            'feedback': feedback if feedback is not None else {
                'revision': 4, 'status': 'needs_adjustment', 'notes': 'Human notes',
                'boundary_verified': True, 'boundary_signature': BOUNDARY,
                'rejected_cuts': [CUT], 'issues': ['apex'],
            }}


@pytest.fixture
def api(monkeypatch):
    state = {'entry': entry(), 'requests': []}

    def request(base, path, body=None):
        state['requests'].append((base, path, copy.deepcopy(body)))
        if body is None:
            return {'cohort': 'qa', 'entries': [copy.deepcopy(state['entry'])]}
        saved = {**copy.deepcopy(body), 'revision': body['revision'] + 1, 'updated_by': 'api-principal',
                 'history': [copy.deepcopy(state['entry']['feedback'])]}
        return {'success': True, 'address_id': 27, 'feedback': saved, 'training_eligible': False}

    monkeypatch.setattr(client, '_request_json', request)
    monkeypatch.delenv('ADDR_APP_URL', raising=False)
    return state


def apply(**kwargs):
    return client.apply_roof_feedback(27, 'qa', 4, BOUNDARY, **kwargs)


def test_saved_geometry_and_prior_human_checks_survive_notes_update(api):
    feedback = api['entry']['feedback']
    feedback.update(annotation={'vertices': [[0, 0, 3]], 'custom': 'kept'}, objects=[], model_signature=MODEL,
                    apex_verified=True, roof_form_verified=True, objects_verified=True,
                    object_reviewed_classes=['solar_panel'])
    before = copy.deepcopy(feedback)
    result = apply(notes='Observed roof is partly hidden.', model_signature=MODEL)
    body = api['requests'][-1][2]
    for field in ('annotation', 'objects', 'issues', 'rejected_cuts', 'boundary_verified', 'apex_verified',
                  'roof_form_verified', 'objects_verified', 'object_reviewed_classes'):
        assert body[field] == before[field]
    assert feedback == before
    assert body['notes'] == 'Observed roof is partly hidden.' and body['status'] == 'needs_adjustment'
    assert result['feedback']['revision'] == 5 and result['feedback']['updated_by'] == 'api-principal'
    assert result['training_eligible'] is False
    assert result['review_url'] == '/reviews?cohort=qa&id=27'
    assert api['requests'][0][:2] == ('http://localhost:3008', '/api/reviews?cohort=qa')
    assert api['requests'][-1][1] == '/api/reviews'


def test_new_notes_draft_cannot_create_human_checks(api):
    api['entry']['feedback'] = {}
    result = client.apply_roof_feedback(27, 'qa', 0, BOUNDARY, notes='Agent observation')
    assert result['feedback']['boundary_verified'] is False
    assert not any(result['feedback'].get(key) for key in ('apex_verified', 'roof_form_verified', 'objects_verified'))


def test_cuts_can_be_added_without_duplicates_or_restored_without_losing_notes(api):
    apply(action='reject_cut', cut_coords=CUT, model_signature=MODEL)
    assert api['requests'][-1][2]['rejected_cuts'] == [CUT]
    apply(action='reject_cut', cut_coords=[[0, 1], [3, 1]], model_signature=MODEL)
    assert len(api['requests'][-1][2]['rejected_cuts']) == 2
    apply(action='restore_cuts')
    assert api['requests'][-1][2]['rejected_cuts'] == []
    assert api['requests'][-1][2]['notes'] == 'Human notes'
    apply(notes='')
    assert api['requests'][-1][2]['notes'] == ''


@pytest.mark.parametrize('kwargs', [
    {'action': 'approve'}, {'action': 'notes'}, {'notes': 'x', 'status': 'approved'},
    {'notes': 'x', 'status': 'rejected'}, {'notes': 'x' * 5001},
    {'action': 'reject_cut', 'cut_coords': [[float('inf'), 0], [1, 1]]},
    {'action': 'reject_cut', 'cut_coords': [[10**1000, 0], [1, 1]]},
    {'action': 'reject_cut', 'cut_coords': [[True, 0], [1, 1]]},
    {'action': 'notes', 'notes': 'x', 'cut_coords': CUT},
])
def test_invalid_actions_labels_and_coordinates_fail_before_network(api, kwargs):
    with pytest.raises(client.RoofFeedbackError):
        apply(**kwargs)
    assert api['requests'] == []


@pytest.mark.parametrize('change, kwargs', [
    ({'feedback': {**entry()['feedback'], 'revision': 5}}, {'notes': 'x'}),
    ({'boundary_signature': BOUNDARY + ';1.00,2.00'}, {'notes': 'x'}),
    ({'feedback': {**entry()['feedback'], 'boundary_signature': 'older'}}, {'notes': 'x'}),
    ({'annotation_stale': True}, {'notes': 'x'}),
    ({}, {'action': 'reject_cut', 'cut_coords': CUT}),
    ({'model_signature': 'b' * 64}, {'action': 'reject_cut', 'cut_coords': CUT, 'model_signature': MODEL}),
    ({'feedback': {**entry()['feedback'], 'annotation': {}, 'model_signature': MODEL}}, {'notes': 'x'}),
])
def test_stale_revisions_boundaries_and_models_never_post(api, change, kwargs):
    api['entry'].update(change)
    with pytest.raises(client.RoofFeedbackError):
        apply(**kwargs)
    assert len(api['requests']) == 1 and api['requests'][0][2] is None


@pytest.mark.parametrize('corrupt', [
    {'revision': 4}, {**entry()['feedback'], 'issues': [{}]},
    {**entry()['feedback'], 'status': {}},
    {**entry()['feedback'], 'boundary_verified': 'yes'},
    {**entry()['feedback'], 'apex_verified': 'yes'},
    {**entry()['feedback'], 'rejected_cuts': [[[0, 0]]]},
])
def test_corrupt_saved_feedback_is_preserved_without_post(api, corrupt):
    api['entry']['feedback'] = corrupt
    with pytest.raises(client.RoofFeedbackError, match='incomplete or invalid'):
        apply(notes='x')
    assert len(api['requests']) == 1


def test_cohort_identity_and_ambiguous_roofs_fail_closed(monkeypatch):
    for response in ({'cohort': 'other', 'entries': [entry()]},
                     {'cohort': 'qa', 'entries': [entry(), entry()]}):
        monkeypatch.setattr(client, '_request_json', lambda *_, response=response: response)
        with pytest.raises(client.RoofFeedbackError):
            client.load_roof_feedback(27, 'qa')


def test_authenticated_http_conflicts_and_unreadable_responses_never_fall_back_to_files(monkeypatch, tmp_path):
    local = tmp_path / 'feedback.json'
    local.write_text('{corrupt existing feedback')
    monkeypatch.setenv('CRM_TOKEN', 'secret-local-token')
    requests = []

    def request(req, timeout):
        requests.append(req)
        assert timeout == 30 and req.get_header('Authorization') == 'Bearer secret-local-token'
        if req.method == 'GET':
            return io.BytesIO(json.dumps({'cohort': 'qa', 'entries': [entry()]}).encode())
        raise HTTPError(req.full_url, 409, 'Conflict', {}, io.BytesIO(b'{"error":"Reload current revision"}'))

    monkeypatch.setattr(client, 'urlopen', request)
    with pytest.raises(client.RoofFeedbackError, match='409: Reload current revision'):
        apply(notes='x')
    assert len(requests) == 2 and requests[-1].full_url.endswith('/api/reviews')
    assert json.loads(requests[-1].data)['revision'] == 4
    assert local.read_text() == '{corrupt existing feedback'
    monkeypatch.setattr(client, 'urlopen', lambda *_args, **_kwargs: io.BytesIO(b'not json'))
    with pytest.raises(client.RoofFeedbackError, match='response could not be read'):
        apply(notes='x')
    monkeypatch.setattr(client, 'urlopen', lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError('connection down')))
    with pytest.raises(client.RoofFeedbackError, match='API is unavailable'):
        apply(notes='x')


def test_unverified_save_acknowledgement_requires_reload(api, monkeypatch):
    read = client._request_json
    monkeypatch.setattr(client, '_request_json', lambda base, path, body=None:
                        read(base, path) if body is None else {'success': True, 'address_id': 28, 'feedback': {}})
    with pytest.raises(client.RoofFeedbackError, match='app may already have saved'):
        apply(notes='x')


def test_nonfinite_json_is_an_expected_refusal_before_post(monkeypatch):
    monkeypatch.setattr(client, 'urlopen', lambda *_args, **_kwargs: io.BytesIO(b'{"cohort":"qa","entries":[],"bad":NaN}'))
    with pytest.raises(client.RoofFeedbackError, match='response could not be read'):
        client.load_roof_feedback(27, 'qa')
    monkeypatch.setattr(client, 'urlopen', lambda *_args, **_kwargs: pytest.fail('Invalid JSON must not reach the API'))
    with pytest.raises(client.RoofFeedbackError, match='invalid JSON fields'):
        client._request_json('http://localhost:3008', '/api/reviews', {'annotation': {'height': float('nan')}})


def test_lost_post_response_requires_reload_instead_of_a_blind_retry(monkeypatch):
    def request(req, timeout):
        if req.method == 'GET':
            return io.BytesIO(json.dumps({'cohort': 'qa', 'entries': [entry()]}).encode())
        raise ConnectionResetError('connection lost after save')

    monkeypatch.setattr(client, 'urlopen', request)
    with pytest.raises(client.RoofFeedbackError, match='save may have completed'):
        apply(notes='x')


def test_mcp_discovery_returns_direct_structured_results_and_expected_errors(api):
    from mcp.server.mcpserver.exceptions import ToolError
    from scripts.ml.mcp_server import server

    tools = {tool.name: tool.model_dump(by_alias=True) for tool in asyncio.run(server.list_tools())}
    assert tools['apply_roof_feedback']['inputSchema']['required'] == ['address_id', 'cohort', 'expected_revision', 'boundary_signature']
    assert tools['cohort_roof_feedback']['annotations']['readOnlyHint'] is True
    assert 'boundary_signature' in tools['cohort_roof_feedback']['outputSchema']['properties']
    arguments = {'address_id': 27, 'cohort': 'qa'}
    read = asyncio.run(server.call_tool('cohort_roof_feedback', arguments)).model_dump(by_alias=True)
    assert read['structuredContent']['revision'] == 4
    saved = asyncio.run(server.call_tool('apply_roof_feedback', {
        **arguments, 'expected_revision': 4, 'boundary_signature': BOUNDARY, 'notes': 'MCP draft',
    })).model_dump(by_alias=True)
    assert saved['structuredContent']['feedback']['notes'] == 'MCP draft'
    assert saved['structuredContent']['training_eligible'] is False
    with pytest.raises(ToolError, match='only unreviewed'):
        asyncio.run(server.call_tool('apply_roof_feedback', {
            **arguments, 'expected_revision': 4, 'boundary_signature': BOUNDARY, 'notes': 'x', 'status': 'approved',
        }))
