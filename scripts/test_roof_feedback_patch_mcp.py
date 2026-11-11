"""Exercise the real MCP draft-save handoff on an isolated, removed QA scene."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from copy import deepcopy
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def transform(point: list[float], matrix: list[list[float]]) -> list[float]:
    return [point[0] * matrix[0][0] + point[1] * matrix[1][0] + matrix[2][0],
            point[0] * matrix[0][1] + point[1] * matrix[1][1] + matrix[2][1]]


async def run() -> None:
    scene_id = f'qa_roof_patch_mcp_{time.time_ns()}'
    source = ROOT / 'data/scenes/roof_training_250-29790'
    directory = ROOT / 'data/scenes' / scene_id
    model = json.loads((source / 'latest.json').read_text())
    roof = deepcopy(next(item for item in model['objects'] if item['kind'] == 'building'))
    for key in ('group_id', 'parent_id', 'roof_parts', 'roof_lines', 'roof_type', 'roof_feedback', 'roof_line_edits'):
        roof.pop(key, None)
    roof.update(id='roof', label='MCP draft fixture', ring=[[100, 100], [400, 100], [400, 300], [100, 300]],
                holes=[], status='candidate', geometry_reviewed=False, roof_annotation=None)
    roof['model']['height_verified'] = False
    other = deepcopy(roof)
    other.update(id='other', notes='Preserve this object', provenance={'source': 'reviewer:fixture'},
                 fieldwork_import={'pending': True, 'origin': 'http://example.test', 'projectId': 'fixture',
                                   'itemId': 'other', 'revision': 1, 'cursor': '1'})
    model.update(id=scene_id, groups=[], objects=[roof, other])
    directory.mkdir()
    original_model = json.dumps(model).encode()
    (directory / 'latest.json').write_bytes(original_model)
    shutil.copyfile(source / f'{model["image_sha256"]}.png', directory / f'{model["image_sha256"]}.png')
    binding = {'scene_id': scene_id, 'object_id': 'roof', 'model_signature': model['model_signature'],
               'image_sha256': model['image_sha256'], 'outline_pixels': {'ring': roof['ring'], 'holes': []}}
    parameters = StdioServerParameters(command=str(ROOT / 'scripts/ml/.venv/bin/python'),
                                       args=['-m', 'scripts.ml.mcp_server'], cwd=str(ROOT),
                                       env={'ADDR_APP_URL': os.environ.get('APP_URL', 'http://localhost:3008'),
                                            **({'CRM_TOKEN': os.environ['CRM_TOKEN']} if os.environ.get('CRM_TOKEN') else {})})
    try:
        async with stdio_client(parameters) as (read, write), ClientSession(read, write, extensions={'io.modelcontextprotocol/ui': {}}) as session:
                await session.discover()
                listed = await session.list_tools()
                tool = next(item for item in listed.tools if item.name == 'update_draft_roof_feedback')
                assert tool.output_schema, 'MCP save results need a structured output schema'
                assert tool.annotations and tool.annotations.read_only_hint is False

                first = await session.call_tool(tool.name, {**binding, 'expected_revision': 0,
                                                'changes': {'notes': 'MCP source-bound draft note'}})
                assert not first.is_error, first.content
                note = first.structured_content
                assert isinstance(note, dict) and note['revision'] == 1
                assert note['object']['notes'] == 'MCP source-bound draft note'
                assert note['human_approval'] is False and note['training_eligible'] is False
                assert json.loads(next(content.text for content in first.content if content.type == 'text')) == note
                saved_other = json.loads((directory / 'review.json').read_text())['objects'][1]

                lines = {'vertices_meters': [transform(point, model['pixel_to_meters']) for point in [[150, 200], [350, 200]]],
                         'edges': [{'a': 0, 'b': 1, 'kind': 'ridge'}], 'notes': 'Uncertain visible ridge draft'}
                second = await session.call_tool(tool.name, {**binding, 'expected_revision': 1,
                                                 'changes': {'roof_line_edits': lines, 'review_issues': ['shape_3d']}})
                assert not second.is_error, second.content
                draft = second.structured_content
                assert isinstance(draft, dict) and draft['revision'] == 2
                assert draft['object']['status'] == 'needs_adjustment'
                assert draft['object']['geometry_reviewed'] is False
                assert draft['object']['model']['height_verified'] is False
                assert draft['object']['roof_line_edits']['checked'] is False
                saved = json.loads((directory / 'review.json').read_text())
                assert len(saved['history']) == 1 and saved['objects'][1] == saved_other
                assert saved['objects'][1]['fieldwork_import']['pending'] is True
                before = (directory / 'review.json').read_bytes()

                stale = await session.call_tool(tool.name, {**binding, 'expected_revision': 1,
                                                'changes': {'notes': 'Stale revision must not overwrite'}})
                assert stale.is_error and '409' in str(stale.content)
                assert (directory / 'review.json').read_bytes() == before

                response = await session.call_tool('roof_line_feedback', {'scene_id': scene_id, 'object_id': 'roof'})
                assert not response.is_error, response.content
                feedback = response.structured_content
                assert isinstance(feedback, dict) and feedback['revision'] == 2
                assert feedback['objects'][0]['feedback_status'] == 'draft'
                assert feedback['objects'][0]['training_eligible'] is False
                assert feedback['objects'][0]['feedback']['vertices_meters'] == lines['vertices_meters']
                assert (directory / 'latest.json').read_bytes() == original_model
        print('MCP draft feedback: real discovery/tool calls, structured result and text fallback, notes/XY draft persistence, source binding, pending imports, canonical history and stale-revision rejection passed.')
    finally:
        shutil.rmtree(directory)


if __name__ == '__main__':
    asyncio.run(run())
