import { App, applyDocumentTheme, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import { OpenAIExtensions } from "@openai/mcp-extensions/app";
import type { CallToolResult, ResourceLink } from "@modelcontextprotocol/sdk/types.js";
import "./styles.css";

const app = new App({ name: "Address reviews", version: "1.0.0" }, {}, { strict: true, autoResize: true });
const extensions = new OpenAIExtensions(app);
const resourcePattern = /^addr:\/\/(?:addresses\/[1-9]\d{0,15}|scenes\/[A-Za-z0-9_-]{1,100})$/;

function element<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing app element: ${id}`);
  return node as T;
}

const form = element<HTMLFormElement>("search-form");
const query = element<HTMLInputElement>("query");
const searchButton = element<HTMLButtonElement>("search-button");
const queueButton = element<HTMLButtonElement>("queue-button");
const list = element<HTMLUListElement>("resources");
const listStatus = element("list-status");
const resultCount = element("result-count");
const detail = element("detail");
const attachmentStatus = element("attachment-status");
let items: ResourceLink[] = [];
let selected: ResourceLink | undefined;
let attached: ResourceLink[] = [];
let filter = "all";
let connected = false;
let receivedToolResult = false;
let searchSequence = 0;
let detailSequence = 0;
let attachBusy = false;
let detailLoaded = false;
let deepLinkUrl: string | undefined;
let roofSequence = 0;
let queueSequence = 0;

type JsonRecord = Record<string, unknown>;

function optionalRecord(value: unknown): JsonRecord | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : undefined;
}

function toolPayload(result: CallToolResult): JsonRecord {
  if (result.isError) {
    const error = result.content.find((part) => part.type === "text");
    throw new Error(error?.type === "text" ? error.text : "The request could not finish.");
  }
  if (result.structuredContent) return recordObject(result.structuredContent);
  const text = result.content.find((part) => part.type === "text");
  if (text?.type !== "text") throw new Error("Missing review data.");
  return recordObject(JSON.parse(text.text));
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message.length < 500 ? error.message : "The request could not finish. Try again.";
}

function jsonDetails(label: string, data: unknown): HTMLDetailsElement {
  const raw = document.createElement("details");
  raw.append(textNode("summary", label), textNode("pre", JSON.stringify(data, null, 2)));
  return raw;
}

function observationsSection(values: unknown): HTMLElement {
  const section = textNode("section", "", "roof-section observations-section");
  section.append(textNode("h3", "Agent observations"));
  section.append(textNode("p", "Source-bound tier 3 notes for you to verify. Human approval and training eligibility remain separate.", "text-muted text-small"));
  const notes = Array.isArray(values) ? values.map(optionalRecord).filter((note): note is JsonRecord => !!note) : [];
  if (!notes.length) section.append(textNode("p", "No agent observations saved for this source.", "text-muted text-small"));
  for (const note of notes) {
    const entry = textNode("article", "", "observation-note");
    entry.append(textNode("p", `${display(note.reviewer)} · ${display(note.recorded_at)}`, "text-muted text-small"));
    for (const [label, key] of [["Observed", "observed"], ["Hypothesis", "hypothesis"], ["Ambiguity", "ambiguity"], ["Next action", "next_action"]]) {
      if (typeof note[key] !== "string" || !note[key]) continue;
      const paragraph = document.createElement("p");
      paragraph.append(textNode("strong", `${label}: `), document.createTextNode(String(note[key])));
      entry.append(paragraph);
    }
    section.append(entry);
  }
  return section;
}

function renderRoofResult(target: HTMLElement, result: CallToolResult, sceneId: string, objectId: string, sequence: number): void {
  const packet = toolPayload(result);
  const identity = optionalRecord(packet.identity);
  if (identity?.scene_id !== sceneId || identity.object_id !== objectId || typeof packet.review_id !== "string") {
    throw new Error("The returned roof does not match this selection. Choose the roof again.");
  }
  const heading = textNode("div", "", "roof-heading");
  heading.append(textNode("h3", "Roof evidence"), textNode("p", `Object ${objectId} · ${display(packet.model_roof_type)}`, "text-muted text-small"));
  const image = result.content.find((part) => part.type === "image" && ["image/png", "image/jpeg", "image/webp"].includes(part.mimeType));
  const content: HTMLElement[] = [heading];
  if (image?.type === "image") {
    const card = document.createElement("img");
    card.className = "roof-card";
    card.alt = "Registered original aerial image, estimated roof outline and lines, and detected image segments.";
    card.src = `data:${image.mimeType};base64,${image.data}`;
    content.push(card);
  } else content.push(textNode("p", "The host did not return an image card. Inspect the original imagery before writing observations.", "status"));
  content.push(textNode("p", "Compare the original RGB with the model overlay. Detected segments may be shadows, texture or solar edges; heights remain estimates.", "text-muted text-small"));

  const evidence = textNode("section", "", "roof-section");
  evidence.append(textNode("h3", "Cached eyes diagnostics"));
  const eyesStatus = optionalRecord(packet.eyes_status);
  evidence.append(textNode("p", `${display(eyesStatus?.rows)} cached rows. ${typeof eyesStatus?.note === "string" ? eyesStatus.note : "Cached diagnostics remain separate from calibrated accuracy."}`, "text-muted text-small"));
  const eyes = optionalRecord(packet.eyes) ?? {};
  const diagnostics = document.createElement("ul");
  diagnostics.className = "diagnostic-list";
  for (const [name, value] of Object.entries(eyes)) {
    if (!Array.isArray(value) || !value.length) continue;
    const bindings = [...new Set(value.map((row) => display(optionalRecord(row)?.binding)))].join(", ");
    diagnostics.append(textNode("li", `${name.replaceAll("_", " ")}: ${value.length} ${value.length === 1 ? "row" : "rows"} · ${bindings.replaceAll("_", " ")}`));
  }
  evidence.append(diagnostics, jsonDetails("Diagnostic details", eyes));
  content.push(evidence);

  const feedback = optionalRecord(packet.saved_feedback);
  const saved = optionalRecord(feedback?.data);
  const objects = Array.isArray(saved?.objects) ? saved.objects : [];
  const feedbackObject = objects.map(optionalRecord).find((obj) => obj?.object_id === objectId);
  const savedSection = textNode("section", "", "roof-section");
  savedSection.append(textNode("h3", "Saved reviewer feedback"));
  savedSection.append(textNode("p", feedback?.state === "available"
    ? `Revision ${display(saved?.revision)} · ${display(feedbackObject?.feedback_status)}${saved?.stale ? " · stale model" : ""}`
    : `Feedback ${display(feedback?.state)}${typeof feedback?.reason === "string" ? `: ${feedback.reason}` : ""}`, "text-small"));
  if (feedback?.state === "available") {
    savedSection.append(textNode("p", "Saved reviewer edits remain distinct from model estimates and agent notes. Draft lines do not grant training approval.", "text-muted text-small"), jsonDetails("Saved feedback details", saved));
  }
  content.push(savedSection);
  let notes = observationsSection(packet.observations);
  content.push(notes);

  if (typeof packet.editor_url === "string" && app.getHostCapabilities()?.openLinks) {
    try {
      const url = new URL(packet.editor_url);
      if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password && url.searchParams.get("layer") !== "model") {
        const open = textNode("button", "Open scene editor", "btn");
        open.type = "button";
        const linkStatus = textNode("p", "", "text-muted text-small");
        open.addEventListener("click", async () => {
          try { if ((await app.openLink({ url: url.href })).isError) throw new Error("The host could not open the scene editor."); }
          catch (error) { linkStatus.textContent = errorMessage(error); }
        });
        const actions = textNode("div", "", "detail-actions");
        actions.append(open, linkStatus);
        content.push(actions);
      }
    } catch { /* Malformed editor links are unavailable. */ }
  }

  const form = textNode("form", "", "observation-form roof-section");
  form.append(textNode("h3", "Record an agent observation"), textNode("p", "Save what is visible, then separate your hypothesis and uncertainty. This adds a note to this source; it does not edit or approve the roof.", "text-muted text-small"));
  const fields: Record<string, HTMLInputElement | HTMLTextAreaElement> = {};
  for (const [name, label, placeholder] of [
    ["observed", "Visible observation", "What can you see in the original image?"],
    ["hypothesis", "Hypothesis", "What might explain it?"],
    ["ambiguity", "Remaining ambiguity", "What cannot be decided from this image?"],
    ["next_action", "Next action", "What should the next reviewer check?"],
    ["reviewer", "Agent / reviewer identity", "codex"],
  ]) {
    const wrapper = textNode("label", label, "observation-field");
    const field = name === "reviewer" ? document.createElement("input") : document.createElement("textarea");
    field.name = name;
    field.maxLength = name === "reviewer" ? 120 : 2000;
    field.required = ["observed", "reviewer"].includes(name);
    field.placeholder = placeholder;
    if (name === "reviewer") field.value = "codex";
    if (field instanceof HTMLTextAreaElement) field.rows = name === "observed" ? 3 : 2;
    field.disabled = !image;
    wrapper.append(field);
    fields[name] = field;
    form.append(wrapper);
  }
  const save = textNode("button", "Save agent observation", "btn btn-primary");
  save.type = "button";
  save.disabled = !image;
  const saveStatus = textNode("p", "", "text-muted text-small");
  saveStatus.setAttribute("role", "status");
  saveStatus.setAttribute("aria-live", "polite");
  let saving = false;
  async function saveObservation(): Promise<void> {
    if (saving || !image || sequence !== roofSequence || !form.reportValidity()) return;
    saving = true;
    save.disabled = true;
    save.textContent = "Saving…";
    saveStatus.textContent = "";
    try {
      const arguments_ = Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, value.value.trim()]));
      const response = toolPayload(await app.callServerTool({ name: "record_roof_observation", arguments: { review_id: packet.review_id, ...arguments_ } }));
      if (sequence !== roofSequence) return;
      const observation = recordObject(response.observation);
      if (observation.review_id !== packet.review_id || observation.tier !== 3 || observation.human_approval !== false || observation.training_eligible !== false) {
        throw new Error("The saved note could not be verified. Reload this roof to check saved observations.");
      }
      const previous = Array.isArray(packet.observations) ? packet.observations : [];
      packet.observations = [...previous.filter((entry) => optionalRecord(entry)?.observation_id !== observation.observation_id), observation];
      const next = observationsSection(packet.observations);
      notes.replaceWith(next);
      notes = next;
      saveStatus.textContent = "Agent observation saved. Roof geometry and approval are unchanged.";
    } catch (error) {
      if (sequence === roofSequence) saveStatus.textContent = errorMessage(error);
    } finally {
      saving = false;
      save.disabled = false;
      save.textContent = "Save agent observation";
    }
  }
  form.addEventListener("submit", (event) => { event.preventDefault(); void saveObservation(); });
  save.addEventListener("click", () => { void saveObservation(); });
  form.append(save, saveStatus);
  content.push(form, jsonDetails("Review packet", packet));
  target.replaceChildren(...content);
}

function roofControls(sceneId: string, model: JsonRecord, preferredObject?: string): HTMLElement {
  const section = textNode("section", "", "roof-workspace");
  section.append(textNode("h3", "Review a roof"));
  const buildings = (Array.isArray(model.objects) ? model.objects : []).map(optionalRecord)
    .filter((obj): obj is JsonRecord => !!obj && obj.kind === "building" && typeof obj.id === "string");
  if (!buildings.length) {
    section.append(textNode("p", "This saved scene has no building objects.", "text-muted text-small"));
    return section;
  }
  const label = textNode("label", "Building object", "observation-field");
  const select = document.createElement("select");
  select.className = "form-control";
  for (const obj of buildings) {
    const option = textNode("option", `${display(obj.label ?? obj.roof_type ?? "Building")} · ${obj.id}`);
    option.value = String(obj.id);
    select.append(option);
  }
  if (preferredObject && buildings.some((obj) => obj.id === preferredObject)) select.value = preferredObject;
  label.append(select);
  const review = textNode("button", "Inspect roof evidence", "btn");
  review.type = "button";
  review.disabled = !app.getHostCapabilities()?.serverTools;
  const status = textNode("p", "", "status text-muted");
  status.setAttribute("role", "status");
  const result = textNode("div", "", "roof-result");
  async function inspect(): Promise<void> {
    const sequence = ++roofSequence;
    const objectId = select.value;
    review.disabled = true;
    result.replaceChildren();
    status.textContent = "Preparing original RGB, overlays and saved feedback…";
    try {
      const response = await app.callServerTool({ name: "scene_roof_review", arguments: { scene_id: sceneId, object_id: objectId, include_source: false, include_feedback: true } });
      if (sequence === roofSequence) {
        renderRoofResult(result, response, sceneId, objectId, sequence);
        status.textContent = "";
      }
    } catch (error) { if (sequence === roofSequence) status.textContent = errorMessage(error); }
    finally { if (sequence === roofSequence) review.disabled = false; }
  }
  review.addEventListener("click", () => { void inspect(); });
  select.addEventListener("change", () => { void inspect(); });
  section.append(label, review, status, result);
  if (preferredObject && select.value === preferredObject && !review.disabled) void inspect();
  return section;
}

async function loadRoofQueue(): Promise<void> {
  if (!connected || !app.getHostCapabilities()?.serverTools) return;
  const sequence = ++queueSequence;
  ++detailSequence;
  ++roofSequence;
  selected = undefined;
  detailLoaded = false;
  renderList();
  queueButton.disabled = true;
  detail.replaceChildren(textNode("p", "Loading eligible low-evidence roofs…", "text-muted"));
  try {
    const payload = toolPayload(await app.callServerTool({ name: "low_confidence_roofs", arguments: { limit: 12, offset: 0 } }));
    if (sequence !== queueSequence) return;
    const section = textNode("section", "", "roof-queue");
    section.append(textNode("h2", "Low-evidence roof queue"), textNode("p", typeof payload.selection === "string" ? payload.selection : "Owned detector roofs eligible for agent review.", "text-muted text-small"));
    const rows = Array.isArray(payload.rows) ? payload.rows.map(optionalRecord).filter((row): row is JsonRecord => !!row) : [];
    if (!rows.length) section.append(textNode("p", "No eligible roofs are available.", "text-muted"));
    for (const row of rows) {
      if (typeof row.scene_id !== "string" || typeof row.object_id !== "string") continue;
      const item: ResourceLink = { type: "resource_link", uri: `addr://scenes/${row.scene_id}`, name: row.scene_id, title: display(row.capture_title ?? row.scene_id), mimeType: "application/json" };
      if (!isResourceLink(item)) continue;
      const button = textNode("button", "", "resource-button queue-roof");
      button.type = "button";
      button.append(textNode("span", title(item), "resource-title"), textNode("span", `${row.object_id} · ${display(row.area_m2, " m²")}`, "text-small"));
      if (Array.isArray(row.reasons)) button.append(textNode("span", row.reasons.filter((value): value is string => typeof value === "string").join("; "), "resource-description"));
      button.addEventListener("click", () => { void selectResource(item, String(row.object_id)); });
      section.append(button);
    }
    detail.replaceChildren(section);
  } catch (error) { if (sequence === queueSequence) detail.replaceChildren(textNode("p", errorMessage(error), "text-muted")); }
  finally { queueButton.disabled = !connected || !app.getHostCapabilities()?.serverTools; }
}

function isResourceLink(value: unknown): value is ResourceLink {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return candidate.type === "resource_link" && typeof candidate.name === "string"
    && typeof candidate.uri === "string" && resourcePattern.test(candidate.uri)
    && (candidate.title === undefined || typeof candidate.title === "string")
    && (candidate.description === undefined || typeof candidate.description === "string");
}

function textNode<K extends keyof HTMLElementTagNameMap>(tag: K, text: string, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function kind(item: ResourceLink): string {
  return item.uri.startsWith("addr://addresses/") ? "addresses" : "scenes";
}

function title(item: ResourceLink): string {
  return item.title || item.name;
}

function canAttach(): boolean {
  return connected && (app.getHostCapabilities()?.updateModelContext?.resourceLink != null || extensions.modelContext != null);
}

function renderList(): void {
  const visible = items.filter((item) => filter === "all" || kind(item) === filter);
  resultCount.textContent = `${visible.length} ${visible.length === 1 ? "resource" : "resources"}`;
  list.replaceChildren(...visible.map((item) => {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "resource-button";
    button.dataset.uri = item.uri;
    button.setAttribute("aria-pressed", String(selected?.uri === item.uri));
    button.append(textNode("span", kind(item) === "addresses" ? "Address" : "Scene", "resource-type"));
    button.append(textNode("span", title(item), "resource-title"));
    if (item.description) button.append(textNode("span", item.description, "resource-description"));
    button.addEventListener("click", () => { void selectResource(item); });
    row.append(button);
    return row;
  }));
  if (items.length > 0) listStatus.textContent = visible.length ? "" : "No resources of this type in these results.";
}

function applyToolResult(result: CallToolResult): void {
  const payload = toolPayload(result);
  const rawItems = payload?.items;
  if (!Array.isArray(rawItems)) throw new Error("Invalid search result");
  items = rawItems.filter(isResourceLink);
  renderList();
  listStatus.textContent = items.length ? "" : query.value.trim()
    ? "No matches. Try a different address, suburb or scene ID."
    : "No saved address or scene estimates are available yet.";
}

async function search(): Promise<void> {
  if (!connected) return;
  if (!app.getHostCapabilities()?.serverTools) {
    listStatus.textContent = "Search is unavailable in this host.";
    return;
  }
  const sequence = ++searchSequence;
  listStatus.textContent = "Searching…";
  list.setAttribute("aria-busy", "true");
  searchButton.disabled = true;
  try {
    const result = await app.callServerTool({ name: "app_search_resources", arguments: { query: query.value.trim() } });
    if (sequence === searchSequence) applyToolResult(result);
  } catch {
    if (sequence === searchSequence) listStatus.textContent = "Unable to load results. Try searching again.";
  } finally {
    if (sequence === searchSequence) {
      list.removeAttribute("aria-busy");
      searchButton.disabled = !connected || !app.getHostCapabilities()?.serverTools;
    }
  }
}

function display(value: unknown, suffix = ""): string {
  if (typeof value === "number" && Number.isFinite(value)) return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)}${suffix}`;
  if (typeof value === "string" && value.trim()) return value;
  return "Not available";
}

function recordObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid model data");
  return value as Record<string, unknown>;
}

function renderDetail(item: ResourceLink, data: Record<string, unknown>, preferredObject?: string): void {
  const heading = textNode("div", "", "detail-heading");
  const isAddress = kind(item) === "addresses";
  const record = recordObject(isAddress ? data.record : data.model);
  heading.append(textNode("span", isAddress ? "Address estimate" : "Scene estimate", "resource-type"));
  heading.append(textNode("h2", typeof record.full_address === "string" ? record.full_address : typeof record.title === "string" ? record.title : title(item)));
  if (item.description) heading.append(textNode("p", item.description, "text-muted text-small"));
  const grid = textNode("dl", "", "summary-grid");
  const fields: [string, string][] = isAddress ? [
    ["Address ID", item.uri.split("/").pop() ?? "Not available"], ["Building type", display(record.kind)],
    ["Roof type", display(record.roof_type)], ["Estimated area", display(record.area_m2, " m²")],
    ["Eave height", display(record.eave_height, " m")], ["Ridge height", display(record.ridge_height, " m")],
  ] : [
    ["Scene ID", display(record.scene_id ?? record.id ?? item.uri.split("/").pop())],
    ["Objects", Array.isArray(record.objects) ? String(record.objects.length) : "Not available"],
    ["Image width", display(record.image_width, " px")], ["Image height", display(record.image_height, " px")],
  ];
  for (const [label, value] of fields) {
    const pair = document.createElement("div");
    pair.append(textNode("dt", label), textNode("dd", value));
    grid.append(pair);
  }
  const provenance = textNode("p", typeof data.provenance === "string" ? data.provenance : "Model estimate; review metadata remains separate.", "provenance");
  const actions = textNode("div", "", "detail-actions");
  if (canAttach()) {
    const button = textNode("button", "", "btn btn-primary");
    button.id = "attach-button";
    button.type = "button";
    button.addEventListener("click", () => { void toggleAttachment(item); });
    actions.append(button);
  } else {
    actions.append(textNode("p", "This host does not support adding resources to chat.", "text-muted text-small"));
  }
  const raw = jsonDetails("Model data", data);
  detail.replaceChildren(heading, grid, provenance, actions);
  if (!isAddress) detail.append(roofControls(item.uri.split("/").pop()!, record, preferredObject));
  detail.append(raw);
  detailLoaded = true;
  renderAttachmentButton();
}

async function selectResource(item: ResourceLink, preferredObject?: string): Promise<void> {
  ++roofSequence;
  ++queueSequence;
  selected = item;
  detailLoaded = false;
  for (const button of list.querySelectorAll<HTMLButtonElement>(".resource-button")) {
    button.setAttribute("aria-pressed", String(button.dataset.uri === item.uri));
  }
  const sequence = ++detailSequence;
  detail.replaceChildren(textNode("p", "Loading model estimate…", "text-muted"));
  detail.setAttribute("aria-busy", "true");
  if (!connected || !app.getHostCapabilities()?.serverResources) {
    detail.removeAttribute("aria-busy");
    detail.replaceChildren(textNode("p", "Resource details are unavailable in this host.", "text-muted"));
    return;
  }
  try {
    const result = await app.readServerResource({ uri: item.uri });
    const content = result.contents.find((part) => part.uri === item.uri && "text" in part);
    if (!content || !("text" in content)) throw new Error("Missing resource data");
    const data = recordObject(JSON.parse(content.text));
    if (sequence === detailSequence) renderDetail(item, data, preferredObject);
  } catch {
    if (sequence === detailSequence) {
      const retry = textNode("button", "Retry", "btn");
      retry.type = "button";
      retry.addEventListener("click", () => { void selectResource(item, preferredObject); });
      detail.replaceChildren(textNode("p", "Unable to load this estimate. It may no longer be available.", "text-muted"), retry);
    }
  } finally {
    if (sequence === detailSequence) detail.removeAttribute("aria-busy");
  }
}

function renderAttachmentButton(): void {
  const button = document.getElementById("attach-button") as HTMLButtonElement | null;
  if (!button || !selected) return;
  const isAttached = attached.some((item) => item.uri === selected?.uri);
  button.textContent = attachBusy ? "Updating…" : isAttached ? "Remove from chat" : "Add to chat";
  button.classList.toggle("btn-primary", !isAttached);
  button.disabled = attachBusy || !detailLoaded || !canAttach();
}

async function toggleAttachment(item: ResourceLink): Promise<void> {
  if (attachBusy || !canAttach()) return;
  const wasAttached = attached.some((resource) => resource.uri === item.uri);
  const next = wasAttached ? attached.filter((resource) => resource.uri !== item.uri) : [...attached, item];
  attachBusy = true;
  attachmentStatus.textContent = "";
  renderAttachmentButton();
  try {
    // Updates replace this app's context; retain its previous explicit selections.
    const params = { content: next };
    if (extensions.modelContext) await extensions.modelContext.update(params);
    else await app.updateModelContext(params);
    attached = next;
    attachmentStatus.textContent = `${title(item)} ${wasAttached ? "removed from" : "added to"} your chat.`;
  } catch {
    attachmentStatus.textContent = "Unable to update your chat. Try again.";
  } finally {
    attachBusy = false;
    renderAttachmentButton();
  }
}

function applyHostContext(forceDeepLink = false): void {
  const context = app.getHostContext();
  if (context?.theme) applyDocumentTheme(context.theme);
  if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
  // Only use host style variables: no remote fonts or assets are loaded.
  const current = extensions.modelContext?.getCurrent();
  if (current !== undefined) {
    attached = current?.content?.filter(isResourceLink) ?? [];
    if (current === null) attachmentStatus.textContent = "";
    renderAttachmentButton();
  }
  const deepLink = extensions.deepLink.getCurrent();
  if (!connected || !deepLink || (deepLink.url === deepLinkUrl && !forceDeepLink)) return;
  deepLinkUrl = deepLink.url;
  try {
    const url = new URL(deepLink.url, "https://addr.invalid");
    const path = url.pathname.match(/^\/(addresses|scenes)\/([^/]+)$/);
    if (!path) return;
    const uri = `addr://${path[1]}/${decodeURIComponent(path[2])}`;
    if (!resourcePattern.test(uri)) return;
    const item = items.find((resource) => resource.uri === uri) ?? {
      type: "resource_link" as const, uri, name: path[2],
      title: `${path[1] === "addresses" ? "Address" : "Scene"} ${decodeURIComponent(path[2])}`,
      mimeType: "application/json",
    };
    const objectId = url.searchParams.get("object");
    void selectResource(item, objectId && objectId.length <= 150 ? objectId : undefined);
  } catch {
    // Ignore malformed routes supplied by a host.
  }
}

form.addEventListener("submit", (event) => { event.preventDefault(); void search(); });
// MCP sandboxes may disallow native form submissions, including prevented ones.
searchButton.addEventListener("click", () => { void search(); });
queueButton.addEventListener("click", () => { void loadRoofQueue(); });
query.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); void search(); }
});
for (const button of document.querySelectorAll<HTMLButtonElement>("[data-kind]")) {
  button.addEventListener("click", () => {
    filter = button.dataset.kind ?? "all";
    for (const sibling of document.querySelectorAll("[data-kind]")) sibling.setAttribute("aria-pressed", String(sibling === button));
    renderList();
  });
}
app.addEventListener("toolresult", (result) => {
  receivedToolResult = true;
  ++searchSequence;
  list.removeAttribute("aria-busy");
  searchButton.disabled = !connected || !app.getHostCapabilities()?.serverTools;
  try { applyToolResult(result); }
  catch { listStatus.textContent = "Unable to load results. Try searching again."; }
});
app.addEventListener("toolcancelled", () => {
  listStatus.textContent = "Loading was cancelled. Search to try again.";
});
app.addEventListener("hostcontextchanged", (context) => {
  applyHostContext(Object.prototype.hasOwnProperty.call(context, "openai/deepLink"));
});

async function start(): Promise<void> {
  if (window.parent === window) {
    listStatus.textContent = "Open Address reviews from your connected addr MCP server to browse resources.";
    return;
  }
  try {
    await app.connect(undefined, { timeout: 15000 });
    connected = true;
    const canSearch = Boolean(app.getHostCapabilities()?.serverTools);
    query.disabled = !canSearch;
    searchButton.disabled = !canSearch;
    queueButton.disabled = !canSearch;
    applyHostContext();
    if (!receivedToolResult) listStatus.textContent = "Waiting for saved resources…";
  } catch {
    listStatus.textContent = "Unable to connect to addr. Reopen Address reviews to try again.";
  }
}
void start();
