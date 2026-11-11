/**
 * Agents append separate assessments with save_agent_review. Ordinary saves to a Fieldwork review project
 * (a project id ending in -review, e.g. mooroolbark-review) remain reserved for the reviewer. The reviewer marks it
 * in the Fieldwork UI. scripts/ml/fieldwork_review.py pull separates server-stamped agent:fieldwork:<reviewer> events from
 * human decisions. The narrow agent endpoint cannot edit status, geometry or human checks.
 *
 * This module refuses ordinary MCP write tools aimed at a review project, for the main Worker's /mcp proxy (worker.ts) and the hosted MCP
 * gateway (mcp.ts). Read tools pass. The browser's REST and Connect saves (the reviewer's own) are never inspected. The rules
 * match cmd/annotate/reviewguard.go and internal/annotation/migration/reviewguard.go:
 * - a project id counts as a review project in any spelling (normalizeProjectId);
 * - a request field counts in any key spelling the Go backend decodes (encoding/json matches keys case-insensitively and keeps
 *   whichever spelling it reads last, so every candidate is checked: fieldValues);
 * - a write naming its target only by item id (save_annotation, delete_annotation, delete_item) is looked up with a read
 *   (GetItem), and so is an update of a stored item that names a project (save_item) or of a stored label (save_label, delete_label:
 *   the review projects' labels); a lookup that fails refuses rather than guesses;
 * - a JSON-RPC batch (nested arrays included) holding one refused message is refused whole;
 * - the backend decodes tools/call arguments into a map and merges every key that folds to "arguments" (a repeated key, or another
 *   spelling) into it, where JSON.parse keeps only the last copy: a tools/call naming its arguments more than once is refused, and
 *   a body holding a tools/call is forwarded as checked (re-serialised), so the backend never reads a key this guard did not see.
 */

export const REVIEW_PROJECT_SUFFIX = "-review";
/** The Go backend's read-only MCP tools. Every other tool name is checked. */
export const MCP_READ_TOOLS: ReadonlySet<string> = new Set([
  "get_info", "list_projects", "list_items", "get_item", "list_labels", "list_changes", "export_project", "search_mentions",
]);
/** The Go backend's MaxRequestBytes: it refuses a larger body anyway. */
export const MAX_MCP_BODY_BYTES = 4 << 20;
const MAX_LIST_PAGES = 200;
const ID_EDGES = /^[\s\p{Cc}]+|[\s\p{Cc}]+$/gu;

type JsonObject = Record<string, unknown>;
const isObject = (value: unknown): value is JsonObject => !!value && typeof value === "object" && !Array.isArray(value);

/** The spelling every review guard compares: Unicode NFKC (fullwidth and other compatibility forms become ASCII), format characters
 *  (zero-width spaces, soft hyphens, a BOM) dropped, every dash (Unicode Pd and the minus sign) read as '-', lowercased, and
 *  whitespace or control characters trimmed from both ends. Same steps as migration.NormalizeProjectID (Go) and
 *  normalize_project_id (scripts/ml/fieldwork_review.py). */
export function normalizeProjectId(id: string): string {
  return id.normalize("NFKC").replace(/\p{Cf}/gu, "").replace(/[\p{Pd}−]/gu, "-").toLowerCase().replace(ID_EDGES, "");
}

export function isReviewProject(id: unknown): boolean {
  return typeof id === "string" && normalizeProjectId(id).endsWith(REVIEW_PROJECT_SUFFIX);
}

/** A key as Go's strings.EqualFold compares it with an ASCII field name: ASCII lowercased, the long s (U+017F) and the Kelvin sign
 *  (U+212A) read as s and k (the only non-ASCII runes that fold to ASCII letters). */
function goFold(key: string): string {
  let out = "";
  for (const char of key) out += char === "ſ" ? "s" : char === "K" ? "k" : char < "\u0080" ? char.toLowerCase() : char;
  return out;
}

/** Every value at a key path, each key matched the way the Go backend's encoding/json decoder matches it; every candidate is kept
 *  because the decoder keeps whichever spelling it reads last. fieldValues(args, "item", "projectId") reads item.projectId. */
export function fieldValues(value: unknown, ...path: string[]): unknown[] {
  let values = [value];
  for (const key of path) {
    const folded = key.toLowerCase();
    values = values.flatMap((entry) => isObject(entry)
      ? Object.keys(entry).filter((name) => goFold(name) === folded).sort().map((name) => entry[name])
      : []);
  }
  return values;
}

export function fieldStrings(value: unknown, ...path: string[]): string[] {
  return fieldValues(value, ...path).filter((entry): entry is string => typeof entry === "string" && entry !== "");
}

export function reviewWriteRefusal(tool: string, project: string): string {
  return `refusing ${tool} on project ${project}: agents never edit a review project (id ending in ${REVIEW_PROJECT_SUFFIX}); ` +
    "use save_agent_review for a separate agent assessment; human decisions are saved in the Fieldwork UI";
}

/** The reads the guard needs. Both throw when the answer cannot be read; the guard then refuses. */
export interface ReviewLookup {
  /** The project of a stored item (a missing item throws too). */
  itemProject(id: string): Promise<string>;
  /** The review project holding a label id, or null when no review project has it. */
  labelReviewProject(id: string): Promise<string | null>;
}

export type JsonRpc = (method: string, data: JsonObject) => Promise<JsonObject>;

/** ReviewLookup over the backend's RPC methods (GetItem, ListProjects, ListLabels). */
export function reviewLookup(rpc: JsonRpc): ReviewLookup {
  const listIds = async (method: string, filter: JsonObject, key: string): Promise<string[]> => {
    const ids: string[] = [];
    let cursor = "";
    for (let page = 0; page < MAX_LIST_PAGES; page++) {
      const value = await rpc(method, { ...filter, cursor, limit: 500 });
      const docs = value[key];
      if (!Array.isArray(docs)) throw new Error(`${method} returned no ${key} list`);
      for (const doc of docs) if (isObject(doc) && typeof doc.id === "string") ids.push(doc.id);
      const next = typeof value.nextCursor === "string" ? value.nextCursor : "";
      if (!next || next === cursor) return ids;
      cursor = next;
    }
    throw new Error(`${method} has more than ${MAX_LIST_PAGES} pages`);
  };
  return {
    async itemProject(id) {
      const value = await rpc("GetItem", { id });
      const project = isObject(value.item) ? value.item.projectId : undefined;
      if (typeof project !== "string" || !project) throw new Error(`item ${id} has no project`);
      return project;
    },
    async labelReviewProject(id) {
      for (const project of await listIds("ListProjects", {}, "projects")) {
        if (isReviewProject(project) && (await listIds("ListLabels", { projectId: project }, "labels")).includes(id)) return project;
      }
      return null;
    },
  };
}

/** Whether a save may replace a stored document: any expectedRevision other than 0 (absent reads as 0, a create; the backend refuses
 *  a create over an existing id). */
function updatesStored(args: unknown): boolean {
  return fieldValues(args, "expectedRevision").some((value) => value !== 0);
}

/** Why a tools/call of `tool` with `args` must not be forwarded, or null when it may be. */
export async function reviewToolRefusal(tool: string, args: unknown, lookup: ReviewLookup): Promise<string | null> {
  // The canonical endpoint appends only agent provenance and cannot change human decisions.
  if (tool === "save_agent_review" || MCP_READ_TOOLS.has(tool)) return null;
  const declared = [
    ...fieldStrings(args, "projectId"), ...fieldStrings(args, "project", "id"),
    ...fieldStrings(args, "item", "projectId"), ...fieldStrings(args, "label", "projectId"),
  ].find(isReviewProject);
  if (declared !== undefined) return reviewWriteRefusal(tool, declared);
  const items = fieldStrings(args, "itemId");
  const labels: string[] = [];
  if (tool === "delete_item") items.push(...fieldStrings(args, "id"));
  if (tool === "delete_label") labels.push(...fieldStrings(args, "id"));
  // The backend refuses an item without a projectId, so only an update naming one can move a stored item.
  if (tool === "save_item" && updatesStored(args) && fieldStrings(args, "item", "projectId").length) {
    items.push(...fieldStrings(args, "item", "id"));
  }
  if (tool === "save_label" && updatesStored(args)) labels.push(...fieldStrings(args, "label", "id"));
  for (const id of items) {
    let project: string;
    try { project = await lookup.itemProject(id); }
    catch { return `refusing ${tool}: cannot check which project item ${id} belongs to; agents never edit a review project`; }
    if (isReviewProject(project)) return reviewWriteRefusal(tool, project);
  }
  for (const id of labels) {
    let project: string | null;
    try { project = await lookup.labelReviewProject(id); }
    catch { return `refusing ${tool}: cannot check whether label ${id} belongs to a review project; agents never edit a review project`; }
    if (project !== null) return reviewWriteRefusal(tool, project);
  }
  return null;
}

/** The refusal for one JSON-RPC message or batch, read the way the Go backend decodes it, or null when it may be forwarded. A
 *  batch (nested arrays included) is refused whole when any of its messages would be refused on its own. */
export async function mcpMessageRefusal(message: unknown, lookup: ReviewLookup): Promise<string | null> {
  if (Array.isArray(message)) {
    for (let i = 0; i < message.length; i++) {
      const refusal = await mcpMessageRefusal(message[i], lookup);
      if (refusal) return `refusing the whole MCP batch (message ${i + 1} of ${message.length}): ${refusal}`;
    }
    return null;
  }
  if (!fieldValues(message, "method").includes("tools/call")) return null;
  for (const params of fieldValues(message, "params")) {
    const argumentSets = fieldValues(params, "arguments");
    if (argumentSets.length > 1) {
      return `refusing an MCP tools/call that names its arguments ${argumentSets.length} times: the backend would merge them; send one arguments object`;
    }
    for (const name of fieldStrings(params, "name")) {
      for (const args of argumentSets.length ? argumentSets : [{}]) {
        const refusal = await reviewToolRefusal(name, args, lookup);
        if (refusal) return refusal;
      }
    }
  }
  return null;
}

/** The request ids in a message or batch; notifications have none. */
export function mcpRequestIds(message: unknown): unknown[] {
  if (Array.isArray(message)) return message.flatMap(mcpRequestIds);
  if (!isObject(message)) return [];
  const ids = "id" in message ? [message.id] : fieldValues(message, "id");
  return ids.filter((id) => typeof id === "string" || typeof id === "number").slice(0, 1);
}

/** A refused message answered locally: one tool error for a request, an array of them (one per request id) for a batch, and 202
 *  with no body when it holds only notifications. */
export function mcpRefusalResponse(message: unknown, refusal: string, headers?: HeadersInit): Response {
  const toolError = (id: unknown) => ({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: refusal }], isError: true } });
  const ids = mcpRequestIds(message);
  const responseHeaders = new Headers(headers);
  responseHeaders.set("Cache-Control", "no-store");
  if (!ids.length) return new Response(null, { status: 202, headers: responseHeaders });
  return Response.json(Array.isArray(message) ? ids.map(toolError) : toolError(ids[0]), { headers: responseHeaders });
}

/** The backend's REST RPC (POST /api/v1/rpc/{method}) through `backend`, with an already authenticated bearer credential. */
export function backendRpc(backend: Pick<Fetcher, "fetch">, origin: string, authorization: string): JsonRpc {
  return async (method, data) => {
    const response = await backend.fetch(new Request(new URL(`/api/v1/rpc/${method}`, origin), {
      method: "POST", headers: { Authorization: authorization, "Content-Type": "application/json" }, body: JSON.stringify(data),
    }));
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(`${method} failed with HTTP ${response.status}`);
    }
    const value: unknown = await response.json();
    if (!isObject(value)) throw new Error(`${method} returned no JSON object`);
    return value;
  };
}

/** True when a message or batch (nested arrays included) holds a tools/call in any key spelling. */
export function holdsToolCall(message: unknown): boolean {
  return Array.isArray(message) ? message.some(holdsToolCall) : fieldValues(message, "method").includes("tools/call");
}

/** The main Worker's /mcp guard. A POST whose JSON-RPC body calls a write tool aimed at a review project is answered here with a
 *  tool error (a Response) and never forwarded; a body that is not JSON is answered with the backend's own parse error, unread by
 *  the backend. Otherwise it returns the request to forward: a body holding a tools/call as the guard parsed it (JSON.stringify of
 *  the checked message: a repeated key the backend's decoder would merge is gone, so the backend reads exactly what was checked),
 *  any other request unchanged. */
export async function guardMcpForward(request: Request, lookup: ReviewLookup): Promise<Response | Request> {
  if (request.method !== "POST") return request;
  const headers = new Headers({ "Cache-Control": "no-store" });
  const origin = request.headers.get("Origin");
  if (origin) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Vary", "Origin");
  }
  const tooLarge = () => Response.json({ code: "invalid_argument", message: "MCP request body exceeds 4 MiB" }, { status: 413, headers });
  if (Number(request.headers.get("Content-Length")) > MAX_MCP_BODY_BYTES) return tooLarge();
  const bytes = await request.clone().arrayBuffer();
  if (bytes.byteLength > MAX_MCP_BODY_BYTES) return tooLarge();
  let message: unknown;
  try { message = JSON.parse(new TextDecoder().decode(bytes)); }
  catch { return Response.json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, { headers }); }
  const refusal = await mcpMessageRefusal(message, lookup);
  if (refusal) return mcpRefusalResponse(message, refusal, headers);
  if (!holdsToolCall(message)) return request;
  const forwardHeaders = new Headers(request.headers);
  forwardHeaders.delete("Content-Length");
  return new Request(request, { body: JSON.stringify(message), headers: forwardHeaders });
}

/** guardMcpForward's refusal, or null when the request may be forwarded (as guardMcpForward returns it). */
export async function guardMcpRequest(request: Request, lookup: ReviewLookup): Promise<Response | null> {
  const result = await guardMcpForward(request, lookup);
  return result instanceof Response ? result : null;
}
