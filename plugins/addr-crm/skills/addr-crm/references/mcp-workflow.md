# Hosted tool contract

Read the connected tool catalogue before using operations described by another
reference. The hosted CRM has fifteen revision-checked annotation tools,
including `save_agent_review`, plus `get_item_image`, `open_crm_workspace` and
the SDK's mentions search tool.
Resources read projects and items; the UI resource opens the workspace.

Local addr tracing tools such as `inspect_object_boundary`,
`preview_object_split` and `apply_object_split` are separate capabilities. Their
presence in a tracing example does not make them available on this connection.
The hosted CRM does not expose the local `/api/scenes/instances` endpoint.

When a polygon transform, protected source binding or preview is unavailable,
retain the geometry and report the missing evidence or operation. An annotation
CRUD save does not validate a split's full lineage and coverage. Never bypass
the canonical protections by inventing an equivalent unrestricted write.

For classification or supported annotation changes: read `get_item`, preserve
the annotation's unrelated fields, call `save_annotation` using the parent
item's current `expectedRevision`, then reload `get_item`. A conflict requires
reconciliation, not a stale automatic retry. Read-only grants expose no saves.

For projects ending in `-review`, use only `save_agent_review` for an agent
assessment. It appends server-stamped agent history at the current parent
revision and preserves human decisions, geometry and checks. The human CMS UI
and review pull retain separate attribution. Record visible evidence and any
uncertainty; an agent approval never establishes human approval or training
eligibility. The narrow endpoint cannot change an outline or a human status.
