"""Call one addr polygon review MCP tool and retain structured responses/native images."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {
    'low_confidence_roofs', 'scene_roof_review', 'inspect_roof_boundary',
    'preview_roof_polygon', 'apply_roof_polygon', 'roof_line_feedback',
    'roof_observations', 'record_roof_observation', 'update_draft_roof_feedback',
    'object_feedback', 'inspect_object_boundary', 'preview_object_polygon', 'apply_object_polygon',
    'preview_object_split', 'inspect_object_split', 'apply_object_split',
}


async def call(args):
    repo = (args.repo or Path(__file__).resolve().parents[4]).resolve()
    python = repo/'scripts/ml/.venv/bin/python'
    if not python.is_file() or not (repo/'scripts/ml/mcp_server.py').is_file():
        raise ValueError('Select an addr checkout with its installed Python MCP environment.')
    arguments = json.loads(args.arguments_file.read_text()) if args.arguments_file else {}
    if not isinstance(arguments, dict):
        raise TypeError('Tool arguments must be a JSON object.')
    # Non-finite JSON is refused before any tool invocation, including saves.
    serialized = json.dumps(arguments, sort_keys=True, allow_nan=False)
    parameters = StdioServerParameters(command=str(python), args=['-m', 'scripts.ml.mcp_server'], cwd=str(repo))
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write, extensions={'io.modelcontextprotocol/ui': {}}) as session,
    ):
        await session.discover()
        result = await session.call_tool(args.tool, arguments)
    if result.is_error:
        messages = [block.text for block in result.content if block.type == 'text']
        raise ValueError('\n'.join(messages) or 'The roof MCP tool refused this call.')
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload) == {'result'} and isinstance(payload['result'], str):
        payload = json.loads(payload['result'])
    if payload is None:
        blocks = [block.text for block in result.content if block.type == 'text']
        if not blocks:
            raise ValueError('The tool returned no structured response.')
        payload = json.loads(blocks[0])
    if not isinstance(payload, dict):
        raise TypeError('The tool returned an unexpected response; reload before retrying a save.')
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    identifier = hashlib.sha256((args.tool+serialized).encode()).hexdigest()[:12]
    prefix = f'{args.tool}-{timestamp}-{identifier}'
    images = []
    for index, block in enumerate(result.content):
        if block.type != 'image':
            continue
        mime = getattr(block, 'mime_type', 'image/png')
        suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}.get(mime, '.bin')
        target = out/f'{prefix}-{index}{suffix}'
        target.write_bytes(base64.b64decode(block.data, validate=True))
        images.append(str(target))
    receipt = out/f'{prefix}.json'
    receipt.write_text(json.dumps({'tool': args.tool, 'arguments': arguments, 'payload': payload,
                                  'transport_images': images}, indent=2, allow_nan=False)+'\n')
    print(json.dumps({'response': str(receipt), 'images': images,
                      **{key: payload[key] for key in ('scene_id', 'object_id', 'preview_id', 'inspection_id', 'revision', 'can_apply') if key in payload}}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', choices=sorted(TOOLS))
    parser.add_argument('--arguments-file', type=Path)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--repo', type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(call(args))
    except (OSError, TypeError, ValueError) as error:
        parser.exit(1, str(error)+'\n')


if __name__ == '__main__':
    main()
