import { McpServer, ResourceTemplate, type RegisteredTool, type ToolCallback } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { CfWorkerJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/cfworker";
import {
  CallToolResultSchema, ErrorCode, ListToolsRequestSchema, McpError,
  type CallToolResult, type ResourceLink, type Tool, type ToolAnnotations,
} from "@modelcontextprotocol/sdk/types.js";
import { registerAppResource, registerAppTool, RESOURCE_MIME_TYPE } from "@modelcontextprotocol/ext-apps/server";
import { OpenAIExtensions, type OpenAIUiToolMetadata } from "@openai/mcp-extensions/server";
import { z } from "zod/v4";
import { CRM_APP_HTML } from "./mcp-app";
import type { McpBackendEnvironment } from "./mcp-env";
import { reviewLookup, reviewToolRefusal } from "./mcp-guard";

export const CRM_WORKSPACE_URI = "ui://addr/crm-workspace";
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const MAX_RESPONSE_BYTES = 128 * 1024 * 1024;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const LEGACY_TOOLS = new Set([
  "get_info", "list_projects", "list_items", "get_item", "list_labels", "list_changes", "export_project",
  "save_project", "save_label", "save_item", "save_annotation", "delete_annotation", "delete_item", "delete_label",
  "save_agent_review",
]);
const READ_TOOLS = new Set([
  "get_info", "list_projects", "list_items", "get_item", "list_labels", "list_changes", "export_project",
]);
const READ_ONLY = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false };
const ICONS = [{
  src: "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.3"><path d="m3 9 7-6 7 6v8H3Z"/><path d="M8 17v-6h4v6"/></svg>'),
  mimeType: "image/svg+xml",
}];
type JsonObject = Record<string, unknown>;
type RegistrationConfig = {
  title?: string; description?: string; inputSchema?: z.ZodType | z.ZodRawShape; outputSchema?: z.ZodType | z.ZodRawShape;
  annotations?: ToolAnnotations; _meta?: Record<string, unknown>;
};
type Registration = (name: string, config: RegistrationConfig, callback: ToolCallback<z.ZodType>) => RegisteredTool;
export interface McpAccess { upstreamAuthorization: string; scopes?: string[] }

class BackendHTTPError extends Error {
  constructor(readonly response: Response) { super("The workspace backend request failed."); }
}
class MissingRPCError extends Error {}

function hasScope(access: McpAccess, scope: string): boolean {
  // Existing workspace bearer connections keep their original authority.
  return access.scopes === undefined || access.scopes.includes(scope);
}

function object(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new McpError(ErrorCode.InternalError, "The workspace returned an invalid document.");
  }
  return value as JsonObject;
}

async function readJSON(response: Response): Promise<JsonObject> {
  if (!response.ok) throw new BackendHTTPError(response);
  const declaredLength = Number(response.headers.get("Content-Length"));
  if (declaredLength > MAX_RESPONSE_BYTES) {
    throw new McpError(ErrorCode.InternalError, "The workspace response is too large. Request a smaller page.");
  }
  const reader = response.body?.getReader();
  if (!reader) throw new McpError(ErrorCode.InternalError, "The workspace returned an empty response.");
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    bytes += value.byteLength;
    if (bytes > MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new McpError(ErrorCode.InternalError, "The workspace response is too large. Request a smaller page.");
    }
    chunks.push(value);
  }
  const decoder = new TextDecoder();
  let text = "";
  for (const chunk of chunks) text += decoder.decode(chunk, { stream: true });
  text += decoder.decode();
  try { return object(JSON.parse(text)); }
  catch { throw new McpError(ErrorCode.InternalError, "The workspace returned an invalid response."); }
}

function jsonResult(value: JsonObject): CallToolResult {
  return { content: [{ type: "text", text: JSON.stringify(value) }], structuredContent: value, isError: false };
}

function base64(bytes: Uint8Array): string {
  const pieces: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    pieces.push(String.fromCharCode(...bytes.subarray(offset, offset + 8192)));
  }
  return btoa(pieces.join(""));
}

function imageSignatureMatches(bytes: Uint8Array, mimeType: string): boolean {
  if (mimeType === "image/png") return [137, 80, 78, 71, 13, 10, 26, 10].every((value, i) => bytes[i] === value);
  if (mimeType === "image/jpeg") return bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  return bytes.length >= 12 && new TextDecoder().decode(bytes.subarray(0, 4)) === "RIFF" &&
    new TextDecoder().decode(bytes.subarray(8, 12)) === "WEBP";
}

function documentLink(kind: "project" | "item", document: JsonObject): ResourceLink {
  const id = document.id;
  const title = kind === "project" ? document.name : document.title;
  if (typeof id !== "string" || !ID.test(id) || typeof title !== "string") {
    throw new McpError(ErrorCode.InternalError, "The workspace returned an invalid resource identity.");
  }
  return {
    type: "resource_link", uri: `fieldwork://${kind}/${id}`, name: id, title,
    mimeType: "application/json",
    description: kind === "project" ? "Annotation project" : `Annotation item in project ${document.projectId}`,
  };
}

/** SDK transport and OpenAI extensions over the existing authenticated Go service. */
export async function handleFieldworkMcp(
  request: Request, env: McpBackendEnvironment, context: ExecutionContext, access: McpAccess,
): Promise<Response> {
  if (!access.upstreamAuthorization || !hasScope(access, "fieldwork:read")) {
    return Response.json({ code: "permission_denied", message: "The fieldwork:read scope is required." }, {
      status: 403, headers: { "Cache-Control": "no-store" },
    });
  }
  const backend = env.ANNOTATION_BACKEND.getByName("fieldwork");
  const headers = new Headers({
    Authorization: access.upstreamAuthorization, "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
  });
  const origin = request.headers.get("Origin");
  // The companion validates incoming origins before this adapter. The original
  // service then validates the known editor origin on these binding requests.
  if (origin) headers.set("Origin", env.FIELDWORK_EDITOR_ORIGIN);
  const backendURL = new URL(env.FIELDWORK_EDITOR_ORIGIN);
  backendURL.search = "";
  backendURL.hash = "";
  let sequence = 0;
  const callBackendMcp = async (method: string, params: JsonObject): Promise<JsonObject> => {
    backendURL.pathname = "/mcp";
    const response = await backend.fetch(new Request(backendURL, {
      method: "POST", headers,
      body: JSON.stringify({ jsonrpc: "2.0", id: ++sequence, method, params }),
    }));
    const result = await readJSON(response);
    if (result.error) {
      const error = object(result.error);
      throw new McpError(typeof error.code === "number" ? error.code : ErrorCode.InternalError,
        typeof error.message === "string" ? error.message : "The workspace request failed.");
    }
    return object(result.result);
  };
  const rawRPC = async (method: string, data: JsonObject): Promise<JsonObject> => {
    const url = new URL(env.FIELDWORK_EDITOR_ORIGIN);
    url.pathname = `/api/v1/rpc/${method}`;
    url.search = "";
    url.hash = "";
    const response = await backend.fetch(new Request(url, { method: "POST", headers, body: JSON.stringify(data) }));
    if (response.status === 404) throw new MissingRPCError();
    if (response.status === 400 || response.status === 409) {
      const error = await response.json() as JsonObject;
      throw new McpError(ErrorCode.InvalidParams, typeof error.message === "string" ? error.message : "The workspace request was refused.");
    }
    return readJSON(response);
  };
  const rpc = async (method: string, data: JsonObject): Promise<JsonObject> => {
    try { return await rawRPC(method, data); }
    catch (error) {
      if (!(error instanceof MissingRPCError)) throw error;
      // The deployed Go image may predate the extension-only read RPCs. Use
      // existing list operations, bounded and only after an HTTP 404 response.
      if (method === "GetProject") {
        const page = await rawRPC("ListProjects", { limit: 500 });
        const project = (page.projects as unknown[]).map(object).find((value) => value.id === data.id);
        if (project) return { project };
        if (page.nextCursor) throw new McpError(ErrorCode.InvalidParams, "This project is outside the lookup window. Open it in Fieldwork.");
      } else if (method === "SearchItems") {
        const page = await rawRPC("ListProjects", { limit: 50 });
        const items: JsonObject[] = [];
        for (const project of (page.projects as unknown[]).map(object)) {
          const found = await rawRPC("ListItems", { projectId: project.id, query: data.query, limit: 20 - items.length });
          items.push(...(found.items as unknown[]).map(object));
          if (items.length >= 20) break;
        }
        items.sort((a, b) => String(a.id).localeCompare(String(b.id)));
        return { items: items.slice(0, 20), nextCursor: "" };
      }
      throw new McpError(-32002, "Resource not found");
    }
  };

  let server: McpServer | undefined;
  try {
    // Authenticated discovery keeps the authoritative schemas at the Go boundary.
    // It also checks the original bearer credential and Origin on every request,
    // including initialize and App-resource reads, before any data is exposed.
    const catalogue = await callBackendMcp("tools/list", {});
    const backendTools = z.array(z.custom<Tool>((value) => {
      const entry = value as Tool | undefined;
      return !!entry && typeof entry.name === "string" && !!entry.inputSchema;
    })).parse(catalogue.tools).filter((tool) => LEGACY_TOOLS.has(tool.name));
    if (backendTools.length !== LEGACY_TOOLS.size || new Set(backendTools.map((tool) => tool.name)).size !== LEGACY_TOOLS.size) {
      throw new McpError(ErrorCode.InternalError, "The workspace tool catalogue is incomplete.");
    }
    const validator = new CfWorkerJsonSchemaValidator({ draft: "2020-12" });
    server = new McpServer({ name: "addr-crm", title: "Addr CRM", version: "1.4.0", icons: ICONS }, {
      jsonSchemaValidator: validator,
      instructions: "Hosted Addr CRM / Fieldwork annotation workspace. Read get_info and current documents before edits. " +
        "Writes require current expectedRevision; annotation writes use the parent item's revision. Preserve source, " +
        "provenance and fields not being edited. Reload and reconcile on conflict. Projects/items use fieldwork:// resources. " +
        "open_crm_workspace opens the hosted workspace. Use save_agent_review for separate agent assessments, including -review projects. " +
        "Agent reviews never change human decisions or establish downstream human training approval.",
    });
    const sdkDefinitions = new Map<string, Tool>();
    const register = server.registerTool.bind(server) as Registration;
    // Capture the SDK's public registration inputs, including metadata generated
    // by OpenAIExtensions. No private SDK fields or a parallel extension contract.
    server.registerTool = ((name: string, config: RegistrationConfig, callback: ToolCallback<z.ZodType>) => {
      const registered = register(name, config, callback);
      const schema = (value: unknown) => z.toJSONSchema(
        value && typeof value === "object" && "_zod" in value ? value as z.ZodType : z.object(value as z.ZodRawShape),
      ) as Tool["inputSchema"];
      sdkDefinitions.set(name, {
        name, title: config.title, description: config.description,
        inputSchema: config.inputSchema ? schema(config.inputSchema) : { type: "object", properties: {} },
        ...(config.outputSchema ? { outputSchema: schema(config.outputSchema) } : {}),
        annotations: config.annotations, _meta: config._meta,
      });
      return registered;
    }) as McpServer["registerTool"];
    const extensions = new OpenAIExtensions(server);

    for (const definition of backendTools) {
      if (!READ_TOOLS.has(definition.name) && !hasScope(access, "fieldwork:write")) continue;
      const validate = validator.getValidator<JsonObject>(definition.inputSchema);
      server.registerTool(definition.name, {
        title: definition.title, description: definition.description,
        // Keep raw JSON untouched: the authoritative schema validates without
        // inserting defaults or dropping adapter-specific source/provenance.
        inputSchema: z.looseObject({}), annotations: definition.annotations, _meta: definition._meta,
      }, async (args) => {
        if (!READ_TOOLS.has(definition.name) && !hasScope(access, "fieldwork:write")) {
          throw new Error("The fieldwork:write scope is required.");
        }
        const checked = validate(args);
        if (!checked.valid) throw new Error(`Invalid arguments: ${checked.errorMessage}`);
        // Only save_agent_review may append agent assessments in review projects.
        // Ordinary writes remain reserved for the human UI (mcp-guard.ts).
        const refusal = await reviewToolRefusal(definition.name, args, reviewLookup(rawRPC));
        if (refusal) throw new Error(refusal);
        return CallToolResultSchema.parse(await callBackendMcp("tools/call", { name: definition.name, arguments: args }));
      });
      sdkDefinitions.set(definition.name, definition);
    }

    extensions.mentions.setHandler(async ({ query }) => {
      if (query.length > 300) throw new Error("query is too long");
      const projects = await rpc("ListProjects", { query, limit: 20 });
      const items = await rpc("SearchItems", { query, limit: 20 });
      const projectLinks = (projects.projects as unknown[]).map((value) => documentLink("project", object(value)));
      const itemLinks = (items.items as unknown[]).map((value) => documentLink("item", object(value)));
      const links: ResourceLink[] = [];
      for (let i = 0; links.length < 20 && (i < projectLinks.length || i < itemLinks.length); i++) {
        if (i < projectLinks.length) links.push(projectLinks[i]);
        if (i < itemLinks.length && links.length < 20) links.push(itemLinks[i]);
      }
      return { items: links };
    });

    server.registerTool("get_item_image", {
      title: "Inspect item image",
      description: "Read an item's original protected image, or its linked saved-review image, without edits or inference. " +
        "Returns native source pixels and the current item revision/SHA-256 binding. Only linked image assets up to 10 MiB are supported.",
      inputSchema: z.strictObject({ id: z.string().regex(ID), variant: z.enum(["source", "review"]).optional() }),
      annotations: READ_ONLY, _meta: { ui: { visibility: ["app", "model"] } },
    }, async ({ id, variant = "source" }) => {
      const document = await rpc("GetItem", { id });
      const item = object(document.item);
      const source = object(item.source);
      let ref: JsonObject;
      if (item.adapter === "addr-scene") {
        const role = variant === "source" ? "image" : "reviewImage";
        if (!source[role]) throw new Error("This item has no image for that role.");
        ref = object(source[role]);
      } else if (item.adapter === "image" && variant === "source") {
        const match = typeof source.url === "string" ? /^\/api\/v1\/assets\/([a-f0-9]{64})$/.exec(source.url) : null;
        if (!match) throw new Error("This item's image is not a protected hosted asset.");
        ref = { sha256: match[1] };
      } else throw new Error("This item does not support a protected image for that role.");
      const digest = ref.sha256;
      if (typeof digest !== "string" || !/^[a-f0-9]{64}$/.test(digest)) throw new Error("The item image has an invalid SHA-256 binding.");
      if (variant === "source" && source.imageSha256 !== undefined && source.imageSha256 !== digest) {
        throw new Error("The item image source digest does not match its image reference.");
      }
      if (typeof ref.bytes === "number" && ref.bytes > MAX_IMAGE_BYTES) throw new Error("The item image exceeds the 10 MiB limit.");
      if (ref.contentType !== undefined && !IMAGE_TYPES.has(String(ref.contentType))) throw new Error("This item role is not a supported image asset.");
      // A role-bound digest selects the route. Source URLs/path fields never
      // control credentialed network requests or allow fetching a model asset.
      const url = new URL(`/api/v1/assets/${digest}`, env.FIELDWORK_EDITOR_ORIGIN);
      const response = await backend.fetch(new Request(url, { headers, redirect: "manual" }));
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel();
        throw new Error("The protected image route returned a redirect; the image was not fetched.");
      }
      if (!response.ok) throw new BackendHTTPError(response);
      const mimeType = response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() ?? "";
      if (!IMAGE_TYPES.has(mimeType) || ref.contentType !== undefined && ref.contentType !== mimeType) {
        await response.body?.cancel();
        throw new Error("The linked asset is not a supported image.");
      }
      if (Number(response.headers.get("Content-Length")) > MAX_IMAGE_BYTES) {
        await response.body?.cancel();
        throw new Error("The item image exceeds the 10 MiB limit.");
      }
      const reader = response.body?.getReader();
      if (!reader) throw new Error("The item image is empty.");
      const chunks: Uint8Array[] = [];
      let length = 0;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > MAX_IMAGE_BYTES) {
          await reader.cancel();
          throw new Error("The item image exceeds the 10 MiB limit.");
        }
        chunks.push(value);
      }
      const bytes = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      const verifiedDigest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
        (value) => value.toString(16).padStart(2, "0")).join("");
      if (verifiedDigest !== digest || response.headers.has("X-Content-SHA256") && response.headers.get("X-Content-SHA256") !== digest) {
        throw new Error("The protected image bytes do not match the item's SHA-256 binding.");
      }
      if (typeof ref.bytes === "number" && ref.bytes !== length || !imageSignatureMatches(bytes, mimeType)) {
        throw new Error("The protected image does not match its recorded format or size.");
      }
      const binding: JsonObject = {
        itemId: item.id, itemRevision: item.revision, variant, imageSha256: digest,
        mimeType, bytes: length,
        ...(variant === "source" ? {
          coordinateSpace: "original image pixels", width: source.width, height: source.height,
        } : { coordinateSpace: "review image; use source image for geometry coordinates" }),
      };
      return {
        content: [{ type: "text", text: JSON.stringify(binding) }, { type: "image", mimeType, data: base64(bytes) }],
        structuredContent: binding, isError: false,
      };
    });

    for (const kind of ["project", "item"] as const) {
      server.registerResource(`annotation-${kind}`, new ResourceTemplate(`fieldwork://${kind}/{id}`, { list: undefined }), {
        title: kind === "project" ? "Annotation project" : "Annotation item", mimeType: "application/json",
        description: "Current hosted document with its revision; protected source bytes remain separate.",
      }, async (uri) => {
        const match = /^fieldwork:\/\/(project|item)\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})$/.exec(uri.href);
        if (!match || match[1] !== kind) throw new McpError(ErrorCode.InvalidParams, "Expected a canonical Fieldwork resource URI.");
        const value = await rpc(kind === "project" ? "GetProject" : "GetItem", { id: match[2] });
        return { contents: [{ uri: uri.href, mimeType: "application/json", text: JSON.stringify(value) }] };
      });
    }
    registerAppResource(server, "addr-crm-workspace", CRM_WORKSPACE_URI, {
      title: "Addr CRM", description: "Search and review hosted Fieldwork projects and items.",
      _meta: { ui: { csp: { connectDomains: [], resourceDomains: [] } } },
    }, async () => ({ contents: [{
      uri: CRM_WORKSPACE_URI, mimeType: RESOURCE_MIME_TYPE, text: CRM_APP_HTML,
      _meta: {
        ui: { csp: { connectDomains: [], resourceDomains: [] } },
        "openai/ui": { preferredDisplayMode: "fullscreen", availableDisplayModes: ["inline", "fullscreen"] },
      },
    }] }));
    registerAppTool(server, "open_crm_workspace", {
      title: "Addr CRM", description: "Open the hosted CRM workspace to browse projects and review items.",
      inputSchema: { projectId: z.string().regex(ID).optional(), itemId: z.string().regex(ID).optional() },
      annotations: READ_ONLY,
      _meta: {
        ui: { resourceUri: CRM_WORKSPACE_URI, visibility: ["app", "model"] },
        "openai/ui": { entrypoints: [{ type: "global" }, { type: "thread" }] } satisfies OpenAIUiToolMetadata,
      },
    }, async ({ projectId, itemId }) => {
      const page = await rpc("ListProjects", { limit: 20 });
      const projects = (page.projects as unknown[]).map(object);
      const result: JsonObject = {
        title: "Addr CRM", projects, nextCursor: page.nextCursor,
        editorUrl: new URL("/", env.FIELDWORK_EDITOR_ORIGIN).href,
      };
      if (itemId) {
        const value = await rpc("GetItem", { id: itemId });
        const item = object(value.item);
        if (projectId && item.projectId !== projectId) throw new Error("The item belongs to another project.");
        result.item = item;
        result.labels = value.labels;
        result.labelsNextCursor = value.labelsNextCursor;
        projectId = typeof item.projectId === "string" ? item.projectId : undefined;
      }
      if (projectId && !projects.some((project) => project.id === projectId)) {
        const value = await rpc("GetProject", { id: projectId });
        projects.push(object(value.project));
      }
      result.selectedProjectId = projectId ?? projects[0]?.id;
      return jsonResult(result);
    });

    server.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: [...sdkDefinitions.values()].sort((a, b) => a.name.localeCompare(b.name)),
    }));
    const transport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: undefined, enableJsonResponse: true, maxRequestBodySize: 4 * 1024 * 1024,
    });
    await server.connect(transport);
    const response = await transport.handleRequest(request);
    // JSON-mode transport has completed its protocol response before returning.
    context.waitUntil(server.close());
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch (error) {
    if (server) context.waitUntil(server.close());
    if (error instanceof BackendHTTPError && [401, 403].includes(error.response.status)) return error.response;
    console.error("Fieldwork MCP request failed.");
    return Response.json({ code: "unavailable", message: "The workspace is starting. Retry shortly." }, {
      status: 503, headers: { "Cache-Control": "no-store", "Retry-After": "5" },
    });
  }
}
