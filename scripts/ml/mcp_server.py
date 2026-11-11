"""MCP server exposing per-address review tools for the addr project.

Tools:
  * ``low_confidence_roofs`` — protected training-frame scene review queue
  * ``scene_roof_review`` — native source/overlay image, geometry and eyes handoff
  * ``record_roof_observation`` — immutable tier-3 notes bound to that handoff
  * ``inspect_roof_boundary`` — current RGB grid and indexed outline/edge crops
  * ``review_queue``     — rank records needing review (per-address quality)
  * ``list_corrections`` — CRM correction training reference
  * ``performance_report`` — coverage, quality grades, worst addresses
  * ``isolation_report`` — roof/tree conflict summary
  * ``search_addresses`` — resolve an address string to records
  * ``address_metadata`` — record + CRM state (metadata, boundary overrides, POIs)
  * ``site_image``       — satellite image with extracted/CRM boundaries overlaid
  * ``image_3d``         — Deep 3D Studio render of the extracted roof mesh
  * ``rooflines``        — TU Delft-style roofline/plane partition for an address
  * ``proposal_feedback`` — measure user-drawn roof-box proposals (IoU vs the
    record ring + adopt/adjust/reject verdict); pair with ``site_image``, which
    renders proposal boxes in magenta
  * ``attribution_audit`` — find records whose ring fits another address's pin
    better than its own (the 27-Cambridge failure class); pair with
    ``site_image``, which labels nearby G-NAF pins with house numbers
  * ``plan_fieldwork_push`` / ``apply_fieldwork_push`` / ``fieldwork_push_status``
    — publish local scene frames + review state to Fieldwork as new immutable
    items (thin wrappers over ``scripts.ml.fieldwork_push``; never edit
    Fieldwork overrides; the live origin needs FIELDWORK_PUSH_ALLOW_LIVE=1)
  * ``publish_training_artefacts`` / ``list_training_manifests`` /
    ``pull_training_artefacts`` — content-addressed training bundles in the
    ``addr-training`` project (wrappers over ``scripts.ml.training.publish_artefacts``
    and ``pull_artefacts``); no training is started

Run over stdio (configured in .opencode/opencode.json):

    scripts/ml/.venv/bin/python -m scripts.ml.mcp_server
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.apps import APP_MIME_TYPE, Apps
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.server.mcpserver.resources import FileResource
from mcp.types import CallToolResult, Icon, ResourceLink, TextContent, ToolAnnotations
from openai_mcp_extensions import (
    OpenAIExtensions,
    OpenAIGlobalEntrypoint,
    OpenAIMentionSearchParams,
    OpenAIMentionSearchResult,
    OpenAIThreadEntrypoint,
    OpenAIUiResourceMetadata,
    OpenAIUiToolMetadata,
)
from pydantic import BaseModel, ConfigDict

from .address_review import (
    DEFAULT_OUT,
    build_context,
    load_records,
    metadata_summary,
    render_3d_image,
    render_site_image,
)
from .mcp_resources import read_address, read_scene, search_resources

openai_extensions = OpenAIExtensions()
apps = Apps()
WORKSPACE_URI = 'ui://addr/review-workspace'
REVIEW_ICON = Icon(
    src='data:image/svg+xml,' + quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.33" stroke-linecap="round" stroke-linejoin="round"><path d="m3 9 7-6 7 6v8H3Z"/><path d="M8 17v-6h4v6"/></svg>'),
    mime_type='image/svg+xml',
)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class ReviewWorkspaceResult(BaseModel):
    title: str
    items: list[ResourceLink]


class RoofQueueResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    version: str
    scope: str
    rows: list[dict[str, Any]]
    eligible_captures: int
    next_offset: int | None


class RoofReviewResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    review_id: str
    version: str
    identity: dict[str, str]
    scope: str
    geometry: dict[str, Any]
    eyes: dict[str, Any]
    eyes_status: dict[str, Any]
    saved_feedback: dict[str, Any]
    observations: list[dict[str, Any]]
    editor_url: str


class RoofObservationResult(BaseModel):
    observation_id: str
    review_id: str
    path: str
    observation: dict[str, Any]


class RoofObservationsResult(BaseModel):
    schema_version: int
    identity: dict[str, str]
    observations: list[dict[str, Any]]
    scope: str
    training_eligible: bool
    human_approval: bool


class RoofFeedbackResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    schema_version: int
    scene_id: str
    revision: int
    model_signature: str
    objects: list[dict[str, Any]]


class RoofDraftSaveResult(BaseModel):
    schema_version: int
    scene_id: str
    object_id: str
    revision: int
    model_signature: str
    image_sha256: str
    object: dict[str, Any]
    training_eligible: bool
    human_approval: bool


class RoofPolygonPreviewResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    preview_id: str
    version: str
    binding: dict[str, Any]
    proposed_outline_pixels: dict[str, Any]
    coordinate_space: str
    scope: str
    can_apply: bool
    render: dict[str, Any]
    training_eligible: bool
    human_approval: bool


class RoofBoundaryInspectionResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    inspection_id: str
    version: str
    binding: dict[str, Any]
    outline_pixels: dict[str, Any]
    outline_role: str
    preview_id: str | None = None
    coordinate_space: str
    rings: list[dict[str, Any]]
    can_apply: bool
    object_state: dict[str, Any]
    render: dict[str, Any]
    training_eligible: bool
    human_approval: bool


class ObjectSplitPreviewResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    preview_id: str
    version: str
    binding: dict[str, Any]
    object_kind: str
    children: list[dict[str, Any]]
    coverage: dict[str, Any]
    render: dict[str, Any]
    can_apply: bool
    training_eligible: bool
    human_approval: bool


class ObjectSplitInspectionResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    inspection_id: str
    preview_id: str
    binding: dict[str, Any]
    outline_role: str
    can_apply: bool
    render: dict[str, Any]
    training_eligible: bool
    human_approval: bool


class ObjectSplitSaveResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    schema_version: int
    preview_id: str
    scene_id: str
    object_id: str
    revision: int
    model_signature: str
    image_sha256: str
    source_object: dict[str, Any]
    children: list[dict[str, Any]]
    training_eligible: bool
    human_approval: bool


def _json_result(value: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(value, ensure_ascii=False, allow_nan=False))],
                          structured_content=value)


def _roof_action(function, *args, **kwargs):
    """Keep expected source/CAS failures actionable for MCP clients."""
    try:
        return function(*args, **kwargs)
    except ValueError as error:
        raise ToolError(str(error)) from error
    except OSError as error:
        raise ToolError('Saved roof evidence could not be read. Reload the scene and retry.') from error


def _resource_result(payload: BaseModel) -> CallToolResult:
    value = payload.model_dump(by_alias=True, exclude_none=True, mode='json')
    return _json_result(value)


@openai_extensions.mentions.search
def search_mentions(params: OpenAIMentionSearchParams, context: Context[Any, Any]) -> OpenAIMentionSearchResult:
    return OpenAIMentionSearchResult(items=search_resources(params.query))


apps.add_resource(FileResource(
    uri=WORKSPACE_URI, name='addr-review-workspace', title='Address reviews',
    description='Search saved addresses and scenes and attach their model records to the composer.',
    mime_type=APP_MIME_TYPE, path=Path(__file__).with_name('mcp_app.html'),
    meta={
        'ui': {'csp': {'connectDomains': [], 'resourceDomains': []}},
        'openai/ui': OpenAIUiResourceMetadata(
            preferred_display_mode='fullscreen', available_display_modes=['inline', 'fullscreen'],
        ).model_dump(by_alias=True, exclude_none=True),
    },
))


@apps.tool(
    resource_uri=WORKSPACE_URI, title='Address reviews', icons=[REVIEW_ICON], annotations=READ_ONLY,
    description='Open the saved address and scene review browser. Estimates and approvals remain separate.',
    meta={'openai/ui': OpenAIUiToolMetadata(
        entrypoints=[OpenAIGlobalEntrypoint(), OpenAIThreadEntrypoint()],
    ).model_dump(by_alias=True, exclude_none=True)},
)
def open_review_workspace() -> Annotated[CallToolResult, ReviewWorkspaceResult]:
    return _resource_result(ReviewWorkspaceResult(title='Address reviews', items=search_resources('')))

server = MCPServer(
    "addr-review",
    title='Addr', version='0.3.0', icons=[REVIEW_ICON], extensions=[apps, openai_extensions],
    instructions=(
        "Per-address review for the addr offline geo engine. Use address_id from "
        "search_addresses, or pass an address substring / lat+lon. site_image and "
        "image_3d write PNGs under data/reviews/<address_id>/ and return the image. "
        "For low-evidence scene roofs, use low_confidence_roofs, then scene_roof_review "
        "to inspect the original RGB, model lines and detected image segments. "
        "record_roof_observation stores source-bound tier-3 notes only, never labels. "
        "For cloud Fieldwork edits, pull_fieldwork_feedback syncs compatible feedback into local scenes automatically. "
        "Conflicts are preserved and reported; rerun the same tool after resolving them. "
        "Imported geometry remains draft feedback until checked in the local scene UI. "
        "plan_fieldwork_push / apply_fieldwork_push publish local scene frames and review state to Fieldwork "
        "as NEW immutable items only (never Fieldwork overrides); publish/pull_training_artefacts move "
        "content-addressed training bundles. None of these tools starts training, and a pushed or imported "
        "value is a model estimate or an agreement record, never a human label. Tokens stay in files."
    ),
)


@server.tool(
    title='Search review resources', annotations=READ_ONLY, meta={'ui': {'visibility': ['app']}},
    description='Search saved address and scene references for the review browser; reads saved models only.',
)
def app_search_resources(query: str) -> Annotated[CallToolResult, OpenAIMentionSearchResult]:
    return _resource_result(OpenAIMentionSearchResult(items=search_resources(query)))


@server.resource('addr://addresses/{address_id}', name='address-record', title='Address model record',
                 mime_type='application/json', description='Generated per-address record, without changing review state.')
def address_reference(address_id: str) -> str:
    try:
        return read_address(address_id)
    except ValueError as error:
        raise ResourceNotFoundError(str(error)) from error
    except OSError as error:
        raise ResourceError('The saved address records could not be read.') from error


@server.resource('addr://scenes/{scene_id}', name='scene-model', title='Saved scene model',
                 mime_type='application/json', description='Saved scene estimates; review feedback remains separate.')
def scene_reference(scene_id: str) -> str:
    try:
        return read_scene(scene_id)
    except ValueError as error:
        raise ResourceNotFoundError(str(error)) from error
    except OSError as error:
        raise ResourceError('The saved scene model could not be read.') from error


@server.tool(
    title='Find roofs to review', meta={'ui': {'visibility': ['app', 'model']}},
    description=(
        "List low-evidence scene roofs eligible for agent review: one owned detector "
        "roof per capture, current model/geometry, training image frames only. Saved "
        "reviews, protected references and held-out/mixed frames are excluded. Returns "
        "scene_id/object_id for scene_roof_review. Capture titles are image context, "
        "not an address assignment. Does not run inference or change the queue."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def low_confidence_roofs(limit: int = 12, offset: int = 0) -> Annotated[CallToolResult, RoofQueueResult]:
    from .scene_roof_review import low_confidence_roofs as queue

    return _json_result(_roof_action(queue, limit=limit, offset=offset))


@server.tool(
    title='Inspect a roof', meta={'ui': {'visibility': ['app', 'model']}},
    description=(
        "Return a native image card with registered original RGB, model outline/roof "
        "lines and detected image segments, plus compact eyes evidence and source-pixel "
        "geometry. Source PNG/model hashes and exact crop affines bind the handoff. "
        "Writes generated card/manifest files only; the default read does not refit roofs. Detected "
        "segments are not human roof lines. include_source adds the full saved RGB "
        "image; include_feedback reads separately labelled saved edits via the canonical "
        "scene API. include_optical_guidance runs a fresh unapplied automatic proposal "
        "and adds source-bound contradictions and possible valleys in a fourth panel; "
        "its bounded modelling stage uses original-RGB-gated cues. include_boundary_guidance "
        "instead previews a fresh unapplied outline and roof with original RGB material "
        "profiles; boundary_upscale optionally nominates eaves from a cached Real-ESRGAN "
        "4x crop, while all gates and roof inference still use original RGB. Ambiguous "
        "shadow/canopy comparisons remain unaccepted. Inspect the image "
        "before record_roof_observation."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def scene_roof_review(
    scene_id: str,
    object_id: str,
    include_feedback: bool = False,
    include_source: bool = False,
    include_optical_guidance: bool = False,
    include_boundary_guidance: bool = False,
    boundary_upscale: bool = False,
) -> Annotated[CallToolResult, RoofReviewResult]:
    from .scene_roof_review import review_roof

    packet = _roof_action(review_roof, scene_id, object_id, include_feedback=include_feedback, include_optical_guidance=include_optical_guidance,
                         include_boundary_guidance=include_boundary_guidance, boundary_upscale=boundary_upscale)
    from .scene_roof_observations import editor_url

    packet['editor_url'] = _roof_action(editor_url, scene_id, object_id)
    result = _json_result(packet)
    result.content.append(Image(path=packet['paths']['card']).to_image_content())
    if include_source:
        result.content.append(Image(path=packet['paths']['source_image']).to_image_content())
    return result


@server.tool(
    title='Record a roof observation', meta={'ui': {'visibility': ['app', 'model']}},
    description=(
        "Record an agent's visible observation, hypothesis, remaining ambiguity and next "
        "action after inspecting a scene_roof_review image. The immutable note is tier 3, "
        "never a human approval or training label. Requires its review_id; refuses changed "
        "source model/image/geometry or altered review artifacts. Writes only separate "
        "review sidecar files. Identical retries return the same note."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def record_roof_observation(
    review_id: str,
    observed: str,
    hypothesis: str = '',
    ambiguity: str = '',
    next_action: str = '',
    reviewer: str = 'codex',
) -> Annotated[CallToolResult, RoofObservationResult]:
    from .scene_roof_review import record_observation

    return _json_result(_roof_action(record_observation, review_id, observed=observed, hypothesis=hypothesis,
                                          ambiguity=ambiguity, next_action=next_action, reviewer=reviewer))


@server.tool(title='Read roof observations', annotations=READ_ONLY, meta={'ui': {'visibility': ['app', 'model']}},
             description='Read immutable tier-3 agent notes bound to the current scene model, image and roof geometry. Stale notes are excluded. No rendering or approval.')
def roof_observations(scene_id: str, object_id: str) -> Annotated[CallToolResult, RoofObservationsResult]:
    from .scene_roof_observations import list_observations

    return _json_result(_roof_action(list_observations, scene_id, object_id))


def _resolve(
    address: str | None,
    lat: float | None,
    lon: float | None,
    address_id: int | None,
):
    context = build_context(query=address, lat=lat, lon=lon, address_id=address_id)
    return context


@server.tool(
    description=(
        "Fetch saved ridge/apex, hip and valley line corrections from a scene review. "
        "Returns revision/author, immutable image identity, metre and pixel coordinates, "
        "starting estimate, changes and optional reviewer notes. Draft observations are "
        "not checked training labels. Never apply stale/inactive feedback or infer "
        "measured heights. Use scene_id from the scene URL and optionally object_id."
    )
)
def roof_line_feedback(scene_id: str, object_id: str | None = None) -> Annotated[CallToolResult, RoofFeedbackResult]:
    from .scene_line_feedback import load_roof_line_feedback

    return _json_result(_roof_action(load_roof_line_feedback, scene_id, object_id))


@server.tool(title='Update draft roof feedback',
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
             description=(
                 'Patch one building through the canonical scene API using its saved revision, model/image hashes and exact current outline. '
                 'changes supports notes, review_issues, roof_line_edits with vertices_meters/edges {a,b,kind}, or replacement outline_pixels with optional polygon_preview_id. '
                 'Use preview_roof_polygon then apply_roof_polygon for image-checked polygon drafts; polygon and line changes cannot be combined. '
                 'Read roof_line_feedback for saved revision/image identity/outline (and the scene resource for an unsaved model). '
                 'New line graphs are unchecked drafts; source baseline and unrelated objects are preserved. '
                 'Changes cannot attest roof family, heights, approval or training eligibility. Stale feedback must be recovered explicitly in the CMS.'))
def update_draft_roof_feedback(scene_id: str, object_id: str, expected_revision: int, model_signature: str,
                               image_sha256: str, outline_pixels: dict[str, Any], changes: dict[str, Any]) -> Annotated[CallToolResult, RoofDraftSaveResult]:
    from .scene_line_feedback import update_draft_roof_feedback as update

    return _json_result(_roof_action(update, scene_id=scene_id, object_id=object_id, expected_revision=expected_revision,
                               model_signature=model_signature, image_sha256=image_sha256,
                               outline_pixels=outline_pixels, changes=changes))


@server.tool(title='Inspect a roof boundary or unchecked proposal', annotations=READ_ONLY, meta={'ui': {'visibility': ['app', 'model']}},
             description=(
                 'Inspect current saved/model roof boundaries before tracing: returns original RGB, source-pixel grid, and a separate indexed outline panel as a native image. '
                 'Optional preview_id inspects that immutable proposal instead, explicitly labelled proposed/unchecked; its complete current source/revision binding must match these scene/object IDs. '
                 'Numbered v indices label vertices; e indices label the outgoing edge, including last vertex back to first. '
                 'Optional edge_index selects one outer-ring edge and includes its neighbouring corners. Hole labels use hN:v/e indices. '
                 'Exact inspected source-pixel ring/holes, outline_role, crop/panel transforms, current saved revision/image/model/outline binding and human-protection state are returned. '
                 'Nearest-neighbour display magnification adds no source detail; never interpret card pixels as source pixels. '
                 'Writes immutable diagnostic sidecars only. Inspect all current edges before tracing, then all proposed edges with preview_id before applying; hidden roof edges remain uncertain.'))
def inspect_roof_boundary(scene_id: str, object_id: str, edge_index: int | None = None,
                          preview_id: str | None = None) -> Annotated[CallToolResult, RoofBoundaryInspectionResult]:
    from .scene_polygon_feedback import inspect_boundary

    packet = _roof_action(inspect_boundary, scene_id, object_id, edge_index, preview_id)
    result = _json_result(packet)
    result.content.append(Image(path=packet['paths']['card']).to_image_content())
    return result


@server.tool(title='Preview a roof polygon', annotations=READ_ONLY, meta={'ui': {'visibility': ['app', 'model']}},
             description=(
                 'Preview an agent-supplied outer ring and holes over the original saved RGB, alongside its current outline. '
                 'proposed_outline_pixels uses open rings of [x,y] source-image pixels, x right/y down; omit repeated closing vertices. '
                 'Returns an immutable preview_id, native registered image, exact source/crop transforms and current revision/model/image bindings. '
                 'Writes diagnostic sidecars only. Inspect the image before applying; hidden eaves and heights remain unverified. '
                 'Human-reviewed or otherwise protected outlines may be previewed but cannot be applied automatically.'))
def preview_roof_polygon(scene_id: str, object_id: str, proposed_outline_pixels: dict[str, Any],
                         notes: str = '', uncertainty: str = '', reviewer: str = 'codex') -> Annotated[CallToolResult, RoofPolygonPreviewResult]:
    from .scene_polygon_feedback import preview_polygon

    packet = _roof_action(preview_polygon, scene_id, object_id, proposed_outline_pixels,
                          notes=notes, uncertainty=uncertainty, reviewer=reviewer)
    result = _json_result(packet)
    result.content.append(Image(path=packet['paths']['card']).to_image_content())
    return result


@server.tool(title='Apply a draft roof polygon',
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
             meta={'ui': {'visibility': ['app', 'model']}},
             description=(
                 'Apply exactly the supplied preview_id after visually inspecting preview_roof_polygon. '
                 'Rechecks immutable preview artifacts, original image/model bytes, exact current outline and saved revision, then calls the canonical scene PATCH API. '
                 'The server stamps machine agent provenance (tier 3), clears geometry/height checks and derived roof estimates, and preserves source models, history and other objects. '
                 'Human-reviewed geometry, checked lines and protected imports are refused. Saved old-outline line observations are retained unchecked and stale. '
                 'This never approves geometry or training labels. After a conflict or lost save response, reload saved feedback before retrying.'))
def apply_roof_polygon(preview_id: str) -> Annotated[CallToolResult, RoofDraftSaveResult]:
    from .scene_polygon_feedback import apply_polygon

    return _json_result(_roof_action(apply_polygon, preview_id))


@server.tool(title='Read current object polygon feedback', annotations=READ_ONLY,
             description=('Read canonical saved polygons and source/revision identities for any scene object class; original model objects are returned if no review exists. '
                          'Includes object kind, notes and automated editing protection state. Does not attest geometry, class, heights or training eligibility.'))
def object_feedback(scene_id: str, object_id: str | None = None) -> Annotated[CallToolResult, RoofFeedbackResult]:
    from .scene_line_feedback import load_object_feedback

    return _json_result(_roof_action(load_object_feedback, scene_id, object_id))


@server.tool(title='Inspect an object boundary or unchecked proposal', annotations=READ_ONLY, meta={'ui': {'visibility': ['app', 'model']}},
             description=('Inspect any active scene object: original RGB, source-pixel grid and numbered polygon vertices/edges as a native image. '
                          'Binds object kind and exact canonical revision/outline/model/image identities. Optional preview_id renders its matching source-bound proposal. '
                          'Optional edge_index selects one outer edge and adjacent corners. Inspect every edge and corner, including hole rings. '
                          'Nearest-neighbour magnification adds no source detail. Tree targets are visible canopy, solar targets are array outlines; shadows are separate. '
                          'Writes immutable diagnostic sidecars only; protected objects can be inspected but not automatically overwritten.'))
def inspect_object_boundary(scene_id: str, object_id: str, edge_index: int | None = None,
                            preview_id: str | None = None) -> Annotated[CallToolResult, RoofBoundaryInspectionResult]:
    from .scene_polygon_feedback import inspect_boundary

    packet = _roof_action(inspect_boundary, scene_id, object_id, edge_index, preview_id, object_scope=True)
    result = _json_result(packet)
    result.content.append(Image(path=packet['paths']['card']).to_image_content())
    return result


@server.tool(title='Preview an object polygon', annotations=READ_ONLY, meta={'ui': {'visibility': ['app', 'model']}},
             description=('Preview an agent supplied polygon for any scene class against original RGB and current saved/source outline. '
                          'proposed_outline_pixels contains open ring/holes of [x,y] source PNG pixels, x right/y down; omit repeated closing vertices. '
                          'Returns native registered image, immutable preview_id, object kind and complete source/revision binding. '
                          'Inspect all proposed edges with inspect_object_boundary(preview_id) before apply_object_polygon. '
                          'Protected human/imported/reference work may be previewed but cannot be applied. Does not save reviews or create approvals.'))
def preview_object_polygon(scene_id: str, object_id: str, proposed_outline_pixels: dict[str, Any],
                           notes: str = '', uncertainty: str = '', reviewer: str = 'codex') -> Annotated[CallToolResult, RoofPolygonPreviewResult]:
    from .scene_polygon_feedback import preview_polygon

    packet = _roof_action(preview_polygon, scene_id, object_id, proposed_outline_pixels,
                          notes=notes, uncertainty=uncertainty, reviewer=reviewer, object_scope=True)
    result = _json_result(packet)
    result.content.append(Image(path=packet['paths']['card']).to_image_content())
    return result


@server.tool(title='Apply a draft object polygon',
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
             description=('Apply exactly the inspected preview_object_polygon preview_id through the canonical per-class authorized scene transaction. '
                          'Rechecks immutable proposal, source model/image bytes, object kind, exact saved outline/revision and human/import/reference protection. '
                          'Saves unchecked T3 machine provenance, reopens this object class and clears geometry/height checks and old footprint-derived estimates. '
                          'Preserves source predictions/images, history, relationships and unrelated object feedback. Roof lines can only be edited with building-only roof tools. '
                          'Reload object_feedback after conflict or an uncertain save response before retrying.'))
def apply_object_polygon(preview_id: str) -> Annotated[CallToolResult, RoofDraftSaveResult]:
    from .scene_polygon_feedback import apply_polygon

    return _json_result(_roof_action(apply_polygon, preview_id, object_scope=True))


@server.tool(title='Preview a pure object split', annotations=READ_ONLY, meta={'ui': {'visibility': ['app', 'model']}},
             description=('Preview two to eight same-class child polygons that exactly partition one current object, using original RGB, a native-pixel grid and indexed child/seam outlines. '
                          'Each child accepts key, outline_pixels {ring,holes}, optional label/notes and role object or unresolved-residual. Stable keys determine server-owned child IDs; caller class/approval/source fields are refused. '
                          'Every source area and gap must be preserved, with no positive child overlap or out-of-parent area. Uncertain remainder must be an explicit unresolved-residual child. '
                          'First correct the parent outer mask and gaps through an audited polygon draft if needed. This tool does not remove background or invent individual tree ownership. '
                          'Optional group_label creates one shared flat group; otherwise current group affiliation is retained. Grouping is not hierarchical and parent_id remains physical attachment. '
                          'Protected/human/imported work and objects with active attached children cannot be applied. Writes immutable diagnostic sidecars only; inspect all child perimeters and internal seams before apply.'))
def preview_object_split(scene_id: str, object_id: str, children: list[dict[str, Any]],
                         group_label: str = '', notes: str = '', uncertainty: str = '', reviewer: str = 'codex') -> Annotated[CallToolResult, ObjectSplitPreviewResult]:
    from .scene_object_split import preview_split

    packet = _roof_action(preview_split, scene_id, object_id, children, group_label=group_label,
                          notes=notes, uncertainty=uncertainty, reviewer=reviewer)
    result = _json_result(packet)
    result.content.append(Image(path=packet['paths']['card']).to_image_content())
    return result


@server.tool(title='Inspect split children and internal seams', annotations=READ_ONLY, meta={'ui': {'visibility': ['app', 'model']}},
             description=('Inspect an immutable current object split preview as native original-RGB/grid/indexed child imagery. '
                          'Optional child_key focuses that exact child polygon; optional edge_index selects its outer edge with adjacent corners. Without child_key, inspect the complete partition including visibly labelled residual children. '
                          'Source/revision bindings remain the current unsplit object; after a source/save conflict request a new split preview. '
                          'Nearest-neighbour magnification adds no independent source detail. Inspect every child corner, outer/hole edge and internal seam. No review writes or approvals.'))
def inspect_object_split(preview_id: str, child_key: str | None = None,
                         edge_index: int | None = None) -> Annotated[CallToolResult, ObjectSplitInspectionResult]:
    from .scene_object_split import inspect_split

    packet = _roof_action(inspect_split, preview_id, child_key, edge_index)
    result = _json_result(packet)
    result.content.append(Image(path=packet['paths']['card']).to_image_content())
    return result


@server.tool(title='Apply an unchecked pure object split',
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
             description=('Apply only the visually inspected preview_object_split preview_id through an atomic canonical scene transaction. '
                          'Reverifies sealed preview/image, exact source PNG/model/review bytes, current kind/outline/revision/group, coverage and human/import/protected-work guards under the save lock. '
                          'Retains the source as superseded with reciprocal replacement_ids/derived_from lineage; stable same-class children are unchecked T3 machine drafts with unassessed visibility and no old footprint-derived 3D/raw-mask authority. '
                          'Source predictions/images, unrelated objects, existing physical attachments, history and pending imports remain preserved. Splits with active attached children are refused. '
                          'Logical grouping is flat. Child labels and unresolved residual roles are hypotheses, never a class change or human approval. Reload object_feedback after lost response or conflicts before retrying.'))
def apply_object_split(preview_id: str) -> Annotated[CallToolResult, ObjectSplitSaveResult]:
    from .scene_object_split import apply_split

    return _json_result(_roof_action(apply_split, preview_id))


@server.tool(
    description=(
        "Rank per-address records that most need review: over-merged blobs (the "
        "address point falls inside a large footprint), clamped-eave large "
        "detections, and CRM-corrected records. Use this to pick the next "
        "addresses for the review loop."
    )
)
def review_queue(limit: int = 10) -> str:

    records = load_records()
    ranked = sorted(
        records,
        key=lambda record: (
            record.get("quality_score", 100),
            -record["area_m2"],
        ),
    )
    lines = []
    for record in ranked[: max(1, limit)]:
        score = record.get("quality_score", 100)
        grade = record.get("quality_grade", "A")
        flags = ",".join(record.get("quality_flags", []))
        if not flags and score >= 95:
            break
        lines.append(
            f"{record['address_id']:>6}  {score:>3} {grade}  {record['full_address']:<52} "
            f"{record['kind']:<8} {record['area_m2']:>6.0f}m²  d={record['distance_meters']:>5.2f}  "
            f"eave={record['eave_height']}  {flags}"
        )
    return "\n".join(lines) if lines else "review queue clean (all records are A grade)"


@server.tool(
    description=(
        "List CRM boundary corrections (the training reference dataset from "
        "`task training:export`): corrected vs extracted geometry, authorship, "
        "and address context."
    )
)
def list_corrections(limit: int = 10) -> str:
    import json

    path = Path("data/training/corrections.jsonl")
    if not path.exists():
        return "no training export yet; run `task training:export`"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        return "no corrections recorded yet"
    lines = [f"{len(rows)} correction(s):"]
    for row in rows[: max(1, limit)]:
        extracted = row.get("extracted_geometry")
        shift = row.get("pin_to_corrected_centroid_m")
        review = " NEEDS-REVIEW" if row.get("needs_review") else ""
        lines.append(
            f"  feature {row['feature_id']}  {row['kind']:<6} address {row['address_id']:<6} "
            f"by {row['updated_by']} at {row['updated_at']}  "
            f"corrected={len(row['corrected_geometry']['coordinates'][0]) - 1}pts "
            f"extracted={len(extracted['coordinates'][0]) - 1 if extracted else 0}pts  "
            f"pin_shift={shift if shift is not None else '-'}m{review}  "
            f"{(row['context'].get('full_address') or '')[:40]}"
        )
    return "\n".join(lines)


@server.tool(
    description=(
        "Performance dashboard from the last `task perf` run "
        "(data/reviews/performance.json): coverage, per-address quality grades, "
        "isolation/structural metrics, and the worst addresses to review."
    )
)
def performance_report(limit: int = 10) -> str:
    import json

    path = Path("data/reviews/performance.json")
    if not path.exists():
        return "no performance report yet; run `task perf`"
    report = json.loads(path.read_text())
    summary = {
        "records": report["records"],
        "addresses_total": report["addresses_total"],
        "coverage": report["coverage"],
        "average_quality": report["average_quality"],
        "quality_grades": report["quality_grades"],
        "isolation_grades": report["isolation_grades"],
        "structural": report["structural"],
    }
    lines = [json.dumps(summary, indent=2), "worst addresses:"]
    for item in report["worst"][: max(1, limit)]:
        lines.append(
            f"  {item['address_id']:>6}  {item['quality_score']:>3} {item['quality_grade']}  "
            f"{item['full_address'][:44]:<46} {item['kind']:<8} {item['area_m2']:>6.0f}m²  "
            f"{','.join(item['quality_flags'])}"
        )
    return "\n".join(lines)


@server.tool(
    description=(
        "Isolation report: roof/tree conflict counts from the last `task "
        "isolation` run (data/reviews/isolation.json), including the addresses "
        "with the most canopy over their roofs."
    )
)
def isolation_report(limit: int = 10) -> str:
    import json

    path = DEFAULT_OUT.parent.parent / "isolation.json"
    if not path.exists():
        path = Path("data/reviews/isolation.json")
    if not path.exists():
        return "no isolation report yet; run `task isolation`"
    report = json.loads(path.read_text())
    summary = {
        "buildings": report["buildings"],
        "trees_analyzed": report["trees_analyzed"],
        "trees_touching_buildings": report["trees_touching_buildings"],
        "trees_mostly_inside_buildings": report["trees_mostly_inside_buildings"],
        "tree_tree_overlap_pairs": report["tree_tree_overlap_pairs"],
        "records_with_canopy_over_roof": report["records_with_canopy_over_roof"],
    }
    lines = [json.dumps(summary, indent=2), "top records by canopy over roof:"]
    for item in report["record_conflicts"][: max(1, limit)]:
        lines.append(
            f"  {item['address_id']:>6}  {item['full_address'][:48]:<50} "
            f"{item['canopy_share']:.2f}"
        )
    return "\n".join(lines)


@server.tool(
    description="Search per-address records by address substring (e.g. '27 CAMBRIDGE ROAD')."
)
def search_addresses(query: str, limit: int = 10) -> str:
    needle = query.strip().upper()
    matches = [
        record
        for record in load_records()
        if needle in record["full_address"].upper()
    ]
    matches.sort(key=lambda item: len(item["full_address"]))
    if not matches:
        return f"no address records match {query!r}"
    lines = [
        f"{record['address_id']:>6}  {record['full_address']:<58} "
        f"{record['kind']:<8} {record['area_m2']:>7.0f}m²"
        for record in matches[: max(1, limit)]
    ]
    return "\n".join(lines)


@server.tool(
    description=(
        "Per-address metadata: extracted roof record (kind, roof type, eave/ridge, "
        "area, address point, neighbours) plus CRM metadata, boundary overrides, and POIs."
    )
)
def address_metadata(
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
) -> str:
    context = _resolve(address, lat, lon, address_id)
    if context is None:
        return "no matching address record (try search_addresses first)"
    import json

    return json.dumps(metadata_summary(context), indent=2)


def _image_result(context, kind: str, path) -> list[TextContent | Image]:
    record = context.record
    isolation = record.get("isolation", "clean")
    canopy = record.get("canopy_over_roof_share", 0.0)
    caption = (
        f"{kind} for {record['full_address']} (address_id={record['address_id']}, "
        f"{record['kind']}, roof {record['roof_type']} {record['area_m2']:.0f}m², "
        f"eave {record['eave_height']}m ridge {record['ridge_height']}m, "
        f"isolation {isolation} (canopy {canopy:.0%})). Saved to {path}"
    )
    return [TextContent(type="text", text=caption), Image(path=path)]


@server.tool(
    description=(
        "Render a top-down site image: satellite imagery with the extracted roof "
        "boundary (red), extracted canopies (teal), CRM boundary overrides "
        "(orange/green/blue), neighbouring footprints (grey), and the address pin."
    )
)
def site_image(
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
) -> list[TextContent | Image]:
    context = _resolve(address, lat, lon, address_id)
    if context is None:
        return [TextContent(type="text", text="no matching address record")]
    path = render_site_image(context, DEFAULT_OUT / str(context.record["address_id"]) / "site.png")
    return _image_result(context, "site image", path)


@server.tool(
    description=(
        "Render the Deep 3D Studio view for the address (extracted roof mesh over the "
        "satellite-displaced heightfield) and return it as an image."
    )
)
def image_3d(
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
) -> list[TextContent | Image]:
    context = _resolve(address, lat, lon, address_id)
    if context is None:
        return [TextContent(type="text", text="no matching address record")]
    centroid = context.record["centroid"]
    path = render_3d_image(
        centroid["lat"],
        centroid["lon"],
        DEFAULT_OUT / str(context.record["address_id"]) / "3d.png",
    )
    return _image_result(context, "3D render", path)


@server.tool(
    description=(
        "Extract rooflines for an address (Python port of the TU Delft kinetic "
        "partition): LSD lines -> mean-shift angle/offset regularization -> ray "
        "propagation -> planar partition -> depth plane fits -> per-plane roof "
        "surfaces. Returns the partition summary and a shaded preview of the "
        "reconstructed roof planes (saved under data/reviews/<address_id>/)."
    )
)
def rooflines(
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
) -> list[TextContent | Image]:
    context = _resolve(address, lat, lon, address_id)
    if context is None:
        return [TextContent(type="text", text="no matching address record")]
    from .rooflines import extract_rooflines, render_mesh

    record = context.record
    out_dir = DEFAULT_OUT / str(record["address_id"])
    result = extract_rooflines(
        record, debug_path=out_dir / "rooflines-site.png"
    )
    mesh_path = out_dir / "rooflines-mesh.png"
    render_mesh(result, mesh_path)
    summary = (
        f"rooflines for {record['full_address']} (address_id={record['address_id']}): "
        f"footprint {result['footprint_area_m2']}m², {result['faces']} partition faces, "
        f"{result['groups']} roof planes, {len(result['rooflines'])} rooflines, "
        f"depth={'yes' if result['depth_available'] else 'no'}. Saved to {mesh_path}"
    )
    return [TextContent(type="text", text=summary), Image(path=mesh_path)]


@server.tool(
    description=(
        "Measure user-drawn roof-box proposals for an address (Address CRM -> "
        "'Propose box', saved as labelled object features): box/record areas, "
        "IoU against the record ring, centroid shift, and an adopt/adjust/"
        "reject verdict per proposal. Pair with site_image, which renders "
        "proposal boxes in magenta over the extracted roof."
    )
)
def proposal_feedback(
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
) -> list[TextContent | Image]:
    from .proposal import show

    result = show(query=address, lat=lat, lon=lon, address_id=address_id)
    if "error" in result:
        return [TextContent(type="text", text=result["error"])]
    lines = [
        f"proposals for {result['full_address']} (address_id={result['address_id']}): "
        f"record {result['record_area_m2']}m² {result['record_kind']} "
        f"({result['boundary_source']})"
    ]
    if not result["proposals"]:
        lines.append("no roof-box proposals yet — draw one via Propose box in Address CRM")
    for item in result["proposals"]:
        lines.append(
            f"proposal {item['id']} ({item['label']}, {item['updated_at']} by "
            f"{item['updated_by']}): box {item['box_area_m2']}m², IoU "
            f"{item['iou']}, shift {item['centroid_shift_m']}m, covers "
            f"{item['box_covers_record_share'] * 100:.0f}% of record -> "
            f"{item['verdict'].upper()}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


@server.tool(
    description=(
        "Audit record-to-address attribution: flags records whose roof ring "
        "fits another address's G-NAF pin better than its own pin by 10 m or "
        "more (the 27-Cambridge failure, where a correction landed on Unit "
        "4/29's roof). Cross-address suspects first, then same-development. "
        "Pair with site_image, which labels nearby pins with house numbers "
        "so the misattribution is visible."
    )
)
def attribution_audit() -> list[TextContent | Image]:
    from .address_review import load_records
    from .attribution import DEFAULT_DB, audit, load_pins

    records = load_records()
    pins = load_pins(DEFAULT_DB)
    result = audit(records, pins)
    lines = [
        f"attribution audit: {result['records_checked']} records checked, "
        f"{len(result['suspects'])} suspects ({result['cross_address']} cross-address)"
    ]
    if not result["suspects"]:
        lines.append("clean — every ring fits its own pin best")
    for item in result["suspects"][:15]:
        lines.append(
            f"{item['tier']:<16} {item['address_id']:<6} diff={item['disagreement_m']}m "
            f"(own pin {item['own_pin_dist_m']}m, other pin {item['other_pin_dist_m']}m) "
            f"{item['full_address'][:38]}  <- {item['other_full_address'][:38]}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


@server.tool(
    description=(
        "Validate the Deep 3D Studio mesh for an address: footprint shape "
        "metrics (solidity, notch angles, partition coverage) checked against "
        "an optional expected shape ('concave' for L-houses, 'convex'), plus "
        "the studio's own mesh tri counts and a canvas-coverage read from a "
        "headless render (saved under data/reviews/<address_id>/)."
    )
)
def studio_check(
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    address_id: int | None = None,
    expected: str | None = None,
) -> list[TextContent | Image]:
    from .studio_check import check

    result = check(
        query=address, lat=lat, lon=lon, address_id=address_id, expected=expected
    )
    if "error" in result:
        return [TextContent(type="text", text=result["error"])]
    shape = result["shape"]
    lines = [
        f"studio check for {result['full_address']} (address_id={result['address_id']}): "
        f"{result['verdict']}",
        f"shape: solidity {shape.get('solidity')}, max notch "
        f"{shape.get('max_reflex_deg')}°, coverage {shape.get('coverage')}, "
        f"{shape.get('parts')} parts; mesh: wall {result['mesh']['wall_tris']} / "
        f"roof {result['mesh']['roof_tris']} tris; canvas fill "
        f"{result['canvas'].get('mesh_fill')}",
    ]
    for finding in result["findings"]:
        lines.append(f"  - {finding}")
    outputs: list[TextContent | Image] = [
        TextContent(type="text", text="\n".join(lines))
    ]
    shot = Path(result["shot"])
    if shot.exists():
        outputs.append(Image(path=shot))
    return outputs


@server.tool(
    description=(
        "Evaluate a roof review cohort (e.g. 'suburban_benchmark') and return "
        "summary statistics plus the multi-panel contact sheet image "
        "(data/reviews/cohorts/<cohort>/contact-sheet.png) showing 2D multi-layer "
        "site analysis and 3D LoD2 meshes for each address."
    )
)
def review_cohort(
    cohort: str = "suburban_benchmark",
    recompute: bool = False,
) -> list[TextContent | Image]:
    from .roof_cohort import DEFAULT_REVIEWS_DIR, evaluate_cohort, load_cohort

    cohort_dir = DEFAULT_REVIEWS_DIR / "cohorts" / cohort
    sheet_path = cohort_dir / "contact-sheet.png"
    report_path = cohort_dir / "report.json"

    if recompute or not sheet_path.exists() or not report_path.exists():
        cohort_data = load_cohort(cohort)
        report = evaluate_cohort(cohort_data)
    else:
        import json

        report = json.loads(report_path.read_text())

    results = report.get("results", [])
    lines = [
        f"Cohort '{cohort}': {len(results)} roofs evaluated in {report.get('total_eval_time_sec', 0)}s "
        f"(mean {report.get('mean_time_per_roof_ms', 0):.0f}ms/roof):",
    ]
    for item in results:
        lines.append(
            f"  {item['address_id']:>5} | {item['face_count']:>2} faces, {item['roofline_count']:>2} cuts | "
            f"{item['footprint_area_m2']:>5.0f}m² | {item['category']:<25} | {item['full_address'][:36]}"
        )
    lines.append(f"\nContact sheet saved to: {sheet_path}")

    outputs: list[TextContent | Image] = [TextContent(type="text", text="\n".join(lines))]
    if sheet_path.exists():
        outputs.append(Image(path=sheet_path))
    return outputs


class CohortRoofFeedbackResult(BaseModel):
    cohort: str
    address_id: int
    revision: int
    boundary_signature: str
    model_signature: str | None
    stale_review: bool
    annotation_stale: bool
    feedback: dict[str, Any]
    review_url: str


class RoofFeedbackSaveResult(BaseModel):
    cohort: str
    address_id: int
    feedback: dict[str, Any]
    training_eligible: bool
    review_url: str
    note: str


@server.tool(
    annotations=READ_ONLY,
    description=(
        "Read one roof's saved cohort feedback through the canonical API, including its "
        "revision, boundary/model signatures and stale flags. These are the explicit "
        "prerequisites for apply_roof_feedback. Does not read unsaved browser drafts or grant approval."
    ),
)
def cohort_roof_feedback(address_id: int, cohort: str) -> Annotated[CallToolResult, CohortRoofFeedbackResult]:
    from .roof_feedback_client import RoofFeedbackError, load_roof_feedback

    try:
        return _resource_result(CohortRoofFeedbackResult(**load_roof_feedback(address_id, cohort)))
    except RoofFeedbackError as error:
        raise ToolError(str(error)) from error


@server.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    description=(
        "Save draft cohort roof feedback through the canonical API with authentication, "
        "revision checks and history. First read cohort_roof_feedback; supply its cohort, "
        "revision as expected_revision and boundary_signature. Actions: notes, reject_cut, "
        "restore_cuts, set_status. Only unreviewed/needs_adjustment statuses are allowed. "
        "model_signature is required for reject_cut or when retaining saved annotations/objects. "
        "Unknown/stale feedback is refused; unrelated saved fields are preserved. "
        "Never writes feedback.json directly, grants human approval or starts training."
    ),
)
def apply_roof_feedback(
    address_id: int,
    cohort: str,
    expected_revision: int,
    boundary_signature: str,
    action: str = "notes",
    cut_coords: list[list[float]] | None = None,
    status: str = "needs_adjustment",
    notes: str | None = None,
    model_signature: str | None = None,
) -> Annotated[CallToolResult, RoofFeedbackSaveResult]:
    from .roof_feedback_client import RoofFeedbackError
    from .roof_feedback_client import apply_roof_feedback as apply

    try:
        result = apply(address_id, cohort, expected_revision, boundary_signature, action=action,
                       cut_coords=cut_coords, status=status, notes=notes, model_signature=model_signature)
        return _resource_result(RoofFeedbackSaveResult(**result))
    except RoofFeedbackError as error:
        raise ToolError(str(error)) from error


@server.tool(
    description=(
        "Sync Fieldwork feedback into local scene reviews in one call: pull incremental cloud documents, "
        "check source/revision compatibility, and apply compatible changes automatically. Returns compact "
        "status, cursor, counts, scene links and conflict reasons. Incoming geometry remains a pending local "
        "draft; this never grants training approval or starts training. Interrupted applies resume on the "
        "next call; conflicts and packets are preserved. Restore the local app or resolve reported conflicts, "
        "then call again. dry_run downloads and previews only; skip_preview downloads only; neither resumes "
        "a pending apply. item_id selects one scene. No cloud writes. Defaults: Mooroolbark and localhost:3008."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def pull_fieldwork_feedback(project_id: str | None = None, snapshot: str | None = None,
                            mirror_dir: str | None = None, packet_file: str | None = None,
                            remote_url: str | None = None, token_file: str | None = None,
                            item_id: str | None = None, local_url: str | None = None,
                            local_token_file: str | None = None, skip_preview: bool = False,
                            dry_run: bool = False) -> str:
    import json

    from .fieldwork_feedback import pull_feedback

    return json.dumps(pull_feedback(project_id=project_id, snapshot=snapshot, mirror_dir=mirror_dir,
                                   packet_file=packet_file, remote_url=remote_url, token_file=token_file,
                                   item_id=item_id, local_url=local_url, local_token_file=local_token_file,
                                   skip_preview=skip_preview, dry_run=dry_run), indent=2)


@server.tool(
    description=(
        "Preview a saved Fieldwork packet against current local scene reviews. Returns planId, canApply, "
        "scene review links, proposed field changes, conflicts and quarantined entries. Read only; "
        "does not fetch the cloud, edit scenes, or train. Advanced diagnostic tool; ordinary sync uses "
        "pull_fieldwork_feedback. item_id optionally narrows the saved packet to one scene."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def preview_fieldwork_feedback(packet_file: str | None = None, local_url: str | None = None,
                               local_token_file: str | None = None, item_id: str | None = None) -> str:
    import json

    from .fieldwork_feedback import preview_feedback

    return json.dumps(preview_feedback(packet_file=packet_file, local_url=local_url,
                                      local_token_file=local_token_file, item_id=item_id), indent=2)


@server.tool(
    description=(
        "Advanced: apply the exact Fieldwork preview plan to local scenes using canonical revision-checked "
        "saves and receipts. plan_id is mandatory; stale or conflicting plans fail instead of overwriting. "
        "Substantive imported changes remain pending local review and cannot become training labels. "
        "No cloud writes or training. Ordinary sync needs only pull_fieldwork_feedback. item_id must match "
        "the preview selection. Open the returned scene links and explicitly check drafts locally."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def apply_fieldwork_feedback(plan_id: str, packet_file: str | None = None, local_url: str | None = None,
                             local_token_file: str | None = None, item_id: str | None = None) -> str:
    import json

    from .fieldwork_feedback import apply_feedback

    return json.dumps(apply_feedback(plan_id, packet_file=packet_file, local_url=local_url,
                                    local_token_file=local_token_file, item_id=item_id), indent=2)


def _fieldwork_module(name: str):
    """Import a push/artefact module lazily so this server loads before those modules land.

    A missing module surfaces as a clear tool error instead of a traceback; a missing
    dependency inside an existing module is re-raised unchanged.
    """
    import importlib

    qualified = f"{__package__}.{name}"
    try:
        return importlib.import_module(qualified)
    except ModuleNotFoundError as error:
        if error.name == qualified or (error.name and qualified.startswith(error.name + ".")):
            raise ToolError(
                f"{qualified} is not available in this checkout; the Fieldwork push/artefact modules "
                "are documented in docs/annotation/local-to-fieldwork.md and land separately."
            ) from error
        raise


def _forward(function, *args, **kwargs):
    """Call a push/artefact function; anticipated refusals (RuntimeError, incl. FeedbackError) reach the client.

    mcp 2.x hides the text of any other exception behind "Error executing tool", so the reason for a
    live-origin refusal, stale plan hash, lock or hash mismatch must travel as a ToolError.
    """
    try:
        return function(*args, **kwargs)
    except RuntimeError as error:
        raise ToolError(str(error)) from error


_PUSH_WORDING = (
    "A pushed item is the model estimate plus the local review state as of publication, never a human "
    "label; push never edits Fieldwork overrides (they only flow back through pull_fieldwork_feedback as "
    "drafts). No training is started. Tokens are read from files and never returned. "
)


@server.tool(
    description=(
        "Plan a push of local scene frames/reviews to Fieldwork without writing anything remote: syncs the "
        "mirror (ListChanges), classifies every scene (create | current | blocked_remote_edits | "
        "blocked_stale_review | blocked_review_lock | too_large | invalid_frame | needs_label) and HEADs "
        "assets to size the upload. Returns runId, counts, blocked scenes with reasons, tooLarge, "
        "bytesToUpload and the plan file under data/fieldwork-sync/<project>/push/runs/<runId>/. "
        "Pass the runId to apply_fieldwork_push. "
        + _PUSH_WORDING
        + "Read only against the remote (ListChanges/GetItem/GetInfo/HEAD); writes only the local plan file. "
        "Defaults: project mooroolbark, mirror data/fieldwork-sync/<project>."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def plan_fieldwork_push(project_id: str | None = None, scenes: list[str] | None = None,
                        remote_url: str | None = None, token_file: str | None = None,
                        mirror_dir: str | None = None) -> str:
    import json

    push = _fieldwork_module("fieldwork_push")
    return json.dumps(_forward(push.push_fieldwork, project_id=project_id, scenes=scenes, dry_run=True,
                               mirror_dir=mirror_dir, remote_url=remote_url, token_file=token_file), indent=2)


@server.tool(
    description=(
        "Apply a saved push plan (run_id from plan_fieldwork_push): uploads missing content-addressed assets, "
        "creates one NEW immutable addr-scene item per changed scene with expectedRevision 0 (an existing "
        "different item is a conflict_id_reuse, never overwritten), marks superseded items with the "
        "superseded-frame tag and metadata.addr.supersededBy in a revision-checked patch, and writes receipts. "
        "Refuses a stale plan hash; interrupted runs converge on re-run. allow_unrecovered pushes past "
        "blocked_remote_edits and records metadata.addr.unrecoveredFieldwork instead. Returns runId, created, "
        "skippedIdentical, marked, markConflicts, blocked, receiptsDir, mirrorCursor. "
        + _PUSH_WORDING
        + "Never calls DeleteItem. Refuses the live Fieldwork origin unless FIELDWORK_PUSH_ALLOW_LIVE=1; "
        "point remote_url at a local dev server otherwise."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def apply_fieldwork_push(run_id: str, project_id: str | None = None, remote_url: str | None = None,
                         token_file: str | None = None, allow_unrecovered: bool = False) -> str:
    import json

    push = _fieldwork_module("fieldwork_push")
    return json.dumps(_forward(push.push_fieldwork, project_id=project_id, run_id=run_id, dry_run=False,
                               remote_url=remote_url, token_file=token_file,
                               allow_unrecovered=allow_unrecovered), indent=2)


@server.tool(
    description=(
        "Read the local push receipts for a project (data/fieldwork-sync/<project>/push/): latest published "
        "generation item per scene, supersede marks, run ids, mirror cursor. scene_id narrows the answer to "
        "one scene. No network, no writes. "
        + _PUSH_WORDING
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def fieldwork_push_status(project_id: str | None = None, scene_id: str | None = None) -> str:
    import json

    push = _fieldwork_module("fieldwork_push")
    status = _forward(push.push_status, project_id=project_id)
    if scene_id is not None and isinstance(status, dict):
        scenes = status.get("scenes")
        if isinstance(scenes, dict):
            status = {**status, "scenes": {key: value for key, value in scenes.items() if key == scene_id}}
        elif isinstance(scenes, list):
            status = {**status, "scenes": [entry for entry in scenes
                                           if isinstance(entry, dict) and entry.get("sceneId") == scene_id]}
    return json.dumps(status, indent=2)


_TRAINING_WORDING = (
    "No training is started by this tool. Published or pulled values are model estimates and review/agreement "
    "records, never human labels; nothing here edits Fieldwork overrides. Tokens are read from files and never "
    "returned. "
)


@server.tool(
    description=(
        "Stage selected training artefact groups (dataset-v2, edit-diffs/<date>, line-corrections/<date>, "
        "edit-diff, sweep, coco-v1, eyes) into a content-addressed bundle under .run/training-bundles/ and, when "
        "dry_run is false, upload the blobs and save ONE immutable addr-report manifest item in the addr-training "
        "Fieldwork project (created if absent). eyes.sqlite is snapshotted with VACUUM INTO, never copied raw. "
        "dry_run (default) only reports per-group file counts and bytes and touches no network. "
        + _TRAINING_WORDING
        + "Non-dry-run refuses the live Fieldwork origin unless FIELDWORK_PUSH_ALLOW_LIVE=1."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def publish_training_artefacts(select: list[str], with_images: bool = False, tarball: bool = False,
                               dry_run: bool = True, remote_url: str | None = None,
                               token_file: str | None = None) -> str:
    import json

    publish = _fieldwork_module("training.publish_artefacts")
    return json.dumps(_forward(publish.publish, select, dry_run=dry_run, url=remote_url, token_file=token_file,
                               with_images=with_images, tarball=tarball), indent=2)


@server.tool(
    description=(
        "List published training manifests (addr-report items of the addr-training project): itemId, "
        "manifestSha256, publishedAt, groups and bytes, newest first. Read only; an absent project is reported, "
        "not created. "
        + _TRAINING_WORDING
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def list_training_manifests(remote_url: str | None = None, token_file: str | None = None,
                            project_id: str = "addr-training", limit: int = 10) -> str:
    import json

    pull = _fieldwork_module("training.pull_artefacts")
    return json.dumps(_forward(pull.list_manifests, remote_url, token_file, project=project_id, limit=limit),
                      indent=2)


@server.tool(
    description=(
        "Pull one published training manifest (manifest = item id or sha256, or latest) into a NEW local out "
        "directory: every chunk and whole file is sha256-verified before anything under out/data is written; "
        "only restricts groups (e.g. dataset-v2, edit-diffs); with_scenes also restores the referenced scene "
        "generations (needs the eyes group); resume skips verified blobs. Writes TRAINING_MANIFEST.json and "
        ".pull-receipt.json. Consumers (export_dataset, prepare_coco --copy, edit_diff) run unchanged on out. "
        + _TRAINING_WORDING
        + "Remote reads only; writes only under out."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def pull_training_artefacts(out: str, manifest: str | None = None, latest: bool = False,
                            only: list[str] | None = None, with_scenes: bool = False, resume: bool = False,
                            remote_url: str | None = None, token_file: str | None = None) -> str:
    import json

    pull = _fieldwork_module("training.pull_artefacts")
    return json.dumps(_forward(pull.pull, remote_url, token_file, out, project="addr-training", manifest=manifest,
                               latest=latest, only=only, with_scenes=with_scenes, resume=resume), indent=2)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
