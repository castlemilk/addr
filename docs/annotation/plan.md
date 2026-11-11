# Fieldwork implementation plan

Fieldwork is a general human annotation workspace. Projects contain items;
items select an editor adapter and retain annotations, labels, review status,
notes and revision history. Global maps and images are the first adapters.
The existing address and roof workspaces remain available in this repository.

## Design direction

The editor is the main surface. A compact project and item queue sits beside
a large canvas and a contextual inspector. Desktop uses three columns; mobile
keeps the canvas, drawing controls and save action reachable, with collapsible
navigation and details. Colour identifies selection and annotation geometry;
text communicates review and persistence status.

Palette: paper `#ffffff`, canvas `#f3f6fa`, ink `#17233a`, muted `#637087`,
action `#245eea`, geometry `#39c9dc`. Use the existing Geist/system sans stack,
native form semantics, visible focus and touch targets of at least 44 pixels.
No fabricated progress figures or implied verification. Examples remain marked
as examples. A saved annotation records human feedback, not survey accuracy.

## Architecture

- Shared React editor and adapter registry, hosted by the existing Next route
  and a small standalone React entry point for cloud deployment.
- Generated Connect-Go and Connect-ES service contract, with a compact JSON
  model for adapter payloads and matching runtime validation.
- Neon PostgreSQL for canonical documents and an append-only change feed.
  Saves compare revisions; conflicting writes preserve the user's draft.
- Cloudflare Worker static assets and routing to the Go service in Containers.
- Authenticated REST, Connect, CLI and MCP access to the same canonical data.
- A local draft is recoverable work, never an unconfirmed remote save.

## Acceptance pass

Create a project and image/map item; draw, undo and edit; assign a label and
note; save and reload; verify a stale write is rejected; pull the result via
CLI/API/MCP; check desktop and phone layouts; verify unauthenticated cloud
access cannot read or mutate project data. Run focused Go checks, lint and
typechecking, and the appropriate UI smoke check. Avoid rerunning unrelated
ML extraction or altering previous roof feedback.

## Concept artifact

`fieldwork-concept.png` was generated using the built-in imagegen tool as
design exploration. It is an illustrative mockup, not a screenshot of the
implemented app. The complete prompt is saved in `imagegen-prompt.txt`.
