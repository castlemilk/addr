"""Stdio MCP server exposing one offline case to a rollout agent as the `addr-case` tools.

    python -m scripts.ml.harnessing.case_server --case WORKSPACE/case --run WORKSPACE/run

Tool names and arguments match the production addr-review tools the tracing skill names, so a
harness refined here transfers to live review; only the tools that fit the case task are listed.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .casetools import CaseTools

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
RECORD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
INSPECT = ('Returns the original RGB, a source-pixel grid and an indexed outline panel as a native image. '
           'Numbered v indices label vertices; e indices label the outgoing edge, including last vertex back to '
           'first. Optional edge_index focuses one outer-ring edge with its neighbouring corners; hole labels use '
           'hN:v/e. With preview_id it renders that proposal (amber, labelled proposed). Display magnification adds '
           'no source detail; never read card pixels as source pixels.')
PREVIEW = ('Preview a traced outline: proposed_outline_pixels is {"ring": [[x, y], ...], "holes": [[[x, y], ...]]} '
           'in original-image pixels, x right / y down, open rings (do not repeat the first point), 3-200 vertices '
           'per ring. Returns a registered original/current/proposed card and an immutable preview_id. Inspect the '
           'proposal with the inspect tool and that preview_id before applying.')
APPLY = ('Apply exactly one inspected preview_id. In this offline case applying records the outline as the '
         "case's current outline and advances its revision; earlier previews become stale. Nothing else is saved.")


def _result(packet, card=None):
    result = CallToolResult(content=[TextContent(type='text', text=json.dumps(packet, ensure_ascii=False))],
                            structured_content=packet)
    if card:
        result.content.append(Image(path=card).to_image_content())
    return result


def build_server(tools: CaseTools) -> MCPServer:
    case = tools.case
    server = MCPServer('addr-case', title='Addr offline case', version='1',
                       instructions=f'One offline {case["task"]} case for scene {case["scene_id"]}. '
                                    'Tools bind to this case only; nothing is saved outside the rollout.')

    def call(name, function, *args, image=True, **kwargs):
        arguments = {'args': args, **kwargs}
        try:
            packet = function(*args, **kwargs)
        except ValueError as error:
            tools.log(name, arguments, error=str(error))
            raise ToolError(str(error)) from error
        except OSError as error:
            tools.log(name, arguments, error=f'{type(error).__name__}: {error}')
            raise ToolError('Case evidence could not be read; reload with the feedback tool.') from error
        tools.log(name, arguments, result=packet)
        return _result(packet, (packet.get('paths') or {}).get('card') if image else None)

    def feedback(roof):
        def run(scene_id: str, object_id: str | None = None) -> CallToolResult:
            return call('roof_line_feedback' if roof else 'object_feedback', tools.feedback, scene_id, object_id,
                        roof=roof, image=False)
        return run

    def inspect(roof):
        def run(scene_id: str, object_id: str, edge_index: int | None = None,
                preview_id: str | None = None) -> CallToolResult:
            return call('inspect_roof_boundary' if roof else 'inspect_object_boundary', tools.inspect, scene_id,
                        object_id, edge_index, preview_id, roof=roof)
        return run

    def preview(roof):
        def run(scene_id: str, object_id: str, proposed_outline_pixels: dict[str, Any], notes: str = '',
                uncertainty: str = '', reviewer: str = 'agent') -> CallToolResult:
            return call('preview_roof_polygon' if roof else 'preview_object_polygon', tools.preview, scene_id,
                        object_id, proposed_outline_pixels, notes, uncertainty, reviewer, roof=roof)
        return run

    def apply(roof):
        def run(preview_id: str) -> CallToolResult:
            return call('apply_roof_polygon' if roof else 'apply_object_polygon', tools.apply, preview_id, roof=roof,
                        image=False)
        return run

    def finish_review(scene_id: str, object_id: str, notes: str = '', uncertainty: str = '') -> CallToolResult:
        return call('finish_review', tools.finish, scene_id, object_id, notes, uncertainty, image=False)

    def inspect_review_outline(roof):
        def run(scene_id: str, object_id: str, edge_index: int | None = None,
                outline: str = 'current') -> CallToolResult:
            return call('inspect_roof_boundary' if roof else 'inspect_object_boundary', tools.inspect, scene_id,
                        object_id, edge_index, None, roof=roof, outline_role=outline)
        return run

    def inspect_review_item(scene_id: str, object_id: str) -> CallToolResult:
        return call('inspect_review_item', tools.inspect_review, scene_id, object_id)

    def submit_agent_review(scene_id: str, object_id: str, decision: str, notes: str) -> CallToolResult:
        return call('submit_agent_review', tools.submit_review, scene_id, object_id, decision, notes, image=False)

    def inspect_detections(scene_id: str, kind: str | None = None) -> CallToolResult:
        return call('inspect_detections', tools.inspect_detections, scene_id, kind)

    def submit_detection_review(scene_id: str, decisions: list[dict[str, Any]],
                                additions: list[dict[str, Any]] | None = None, notes: str = '') -> CallToolResult:
        return call('submit_detection_review', tools.submit_detections, scene_id, decisions, additions or [], notes,
                    image=False)

    if case['task'] == 'outline':
        building = case['kind'] == 'building'
        for roof in ((True, False) if building else (False,)):
            label = 'roof' if roof else 'object'
            server.add_tool(feedback(roof), name=f'{"roof_line" if roof else "object"}_feedback', annotations=READ_ONLY,
                            description=f'Reload the case {label} outline, revision, class and machine metadata.')
            server.add_tool(inspect(roof), name=f'inspect_{label}_boundary', annotations=READ_ONLY,
                            description=f'Inspect the current {label} boundary or an unchecked proposal. {INSPECT}')
            server.add_tool(preview(roof), name=f'preview_{label}_polygon', annotations=READ_ONLY,
                            description=PREVIEW)
            server.add_tool(apply(roof), name=f'apply_{label}_polygon', annotations=RECORD, description=APPLY)
        server.add_tool(finish_review, name='finish_review', annotations=RECORD,
                        description='End the case: record final notes naming every unresolved edge or corner. '
                                    'Required, including when the current outline is kept unchanged.')
    elif case['task'] == 'review':
        server.add_tool(inspect_review_item, name='inspect_review_item', annotations=READ_ONLY,
                        description='The review item as the human reviewer sees it: the question, and one registered '
                                    'card with the original RGB, the CURRENT outline and the PROPOSED outline.')
        server.add_tool(feedback(False), name='object_feedback', annotations=READ_ONLY,
                        description='Reload the object: current and proposed outlines, class and the question.')
        for roof in ((True, False) if case['kind'] == 'building' else (False,)):
            label = 'roof' if roof else 'object'
            server.add_tool(inspect_review_outline(roof), name=f'inspect_{label}_boundary', annotations=READ_ONLY,
                            description=f'Inspect the {label} boundary at native resolution; outline is "current" or '
                                        f'"proposed" (the review proposal, amber). {INSPECT}')
        server.add_tool(submit_agent_review, name='submit_agent_review', annotations=RECORD,
                        description='Record the assessment once (the case equivalent of save_agent_review): decision '
                                    'is approved | rejected | needs_adjustment | uncertain for the PROPOSED outline, '
                                    'with evidence notes naming what in the imagery decided it.')
    else:
        server.add_tool(inspect_detections, name='inspect_detections', annotations=READ_ONLY,
                        description='List the machine detections of this class family with their outlines, and a '
                                    'card numbering them d0, d1, ... over the original RGB. Optional kind filters.')
        server.add_tool(feedback(False), name='object_feedback', annotations=READ_ONLY,
                        description='Reload one detection: outline, class and machine metadata.')
        server.add_tool(inspect(False), name='inspect_object_boundary', annotations=READ_ONLY,
                        description=f'Inspect one detection boundary at native resolution. {INSPECT}')
        server.add_tool(submit_detection_review, name='submit_detection_review', annotations=RECORD,
                        description='Submit the review once: decisions is [{"object_id", "decision": keep|reject|'
                                    'relabel, "kind" (relabel only), "note"}] covering EVERY listed detection; '
                                    'additions is [{"kind", "outline_pixels": {"ring", "holes"}, "note"}] for clearly '
                                    'missed objects of this family.')
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--case', required=True, help='the workspace copy of the case source directory')
    parser.add_argument('--run', required=True, help='where tool state, cards and the submission are written')
    args = parser.parse_args(argv)
    build_server(CaseTools(args.case, args.run)).run(transport='stdio')


if __name__ == '__main__':
    main()
