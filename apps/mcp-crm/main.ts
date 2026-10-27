import { App, applyDocumentTheme, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import { OpenAIExtensions } from "@openai/mcp-extensions/app";
import { errorText, item, itemLink, objectMetadata, payload, project, record, records, text } from "./model";
import type { Item, Label, Project, RecordData } from "./model";
import type { ResourceLink } from "@modelcontextprotocol/sdk/types.js";
import "./styles.css";

const app = new App({ name: "Addr CRM", version: "1.0.0" }, {}, { strict: true, autoResize: true });
const extensions = new OpenAIExtensions(app);
const editorUrl = "https://fieldwork-annotations.ben-ebsworth.workers.dev/";
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
let connected = false;
let receivedLaunch = false;
let projects: Project[] = [];
let projectsCursor = "";
let projectSequence = 0;
let selectedProjectId = "";
let items: Item[] = [];
let itemsCursor = "";
let itemSequence = 0;
let selectedItem: Item | undefined;
let labels: Label[] = [];
let labelsCursor = "";
let detailSequence = 0;
let attached: ResourceLink[] = [];
let attachmentBusy = false;
let lastDeepLink = "";
let annotationQuery = "";
let annotationLimit = 25;

function element<T extends HTMLElement = HTMLElement>(id: string): T {
  const value = document.getElementById(id);
  if (!value) throw new Error(`Missing CRM element: ${id}`);
  return value as T;
}

function node<K extends keyof HTMLElementTagNameMap>(tag: K, value = "", className = ""): HTMLElementTagNameMap[K] {
  const result = document.createElement(tag);
  result.textContent = value;
  result.className = className;
  return result;
}

function notice(error?: unknown): void {
  const value = element("notice");
  value.hidden = !error;
  value.textContent = error ? errorText(error) : "";
}

function setSearchEnabled(): void {
  for (const control of element<HTMLFormElement>("project-search").querySelectorAll("input,button")) {
    (control as HTMLInputElement | HTMLButtonElement).disabled = !connected;
  }
  for (const control of element<HTMLFormElement>("item-search").querySelectorAll("input,button")) {
    (control as HTMLInputElement | HTMLButtonElement).disabled = !connected || !selectedProjectId;
  }
  element<HTMLButtonElement>("refresh").disabled = !connected;
}

function documentButton(title: string, meta: string, active: boolean, action: () => void): HTMLLIElement {
  const entry = node("li");
  const button = node("button", "", "document-button");
  button.type = "button";
  button.setAttribute("aria-current", String(active));
  button.append(node("span", title, "document-title"), node("span", meta, "document-meta"));
  button.addEventListener("click", action);
  entry.append(button);
  return entry;
}

function renderProjects(): void {
  element("projects").replaceChildren(...projects.map((value) => documentButton(value.name,
    text(value.description, `Revision ${text(value.revision)}`), value.id === selectedProjectId,
    () => { void selectProject(value.id); })));
  element<HTMLButtonElement>("more-projects").hidden = !projectsCursor;
  const selected = projects.find((value) => value.id === selectedProjectId);
  element("project-title").textContent = selected?.name ?? (selectedProjectId || "Choose a project");
  element("project-description").textContent = selected ? text(selected.description, "Saved project items") : "Browse saved items and annotation feedback.";
  setSearchEnabled();
}

function renderItems(): void {
  element("items").replaceChildren(...items.map((value) => documentButton(value.title,
    `${text(value.status).replaceAll("_", " ")} · ${records(value.annotations).length} annotations · r${text(value.revision)}`,
    value.id === selectedItem?.id, () => { void selectItem(value.id); })));
  element<HTMLButtonElement>("more-items").hidden = !itemsCursor;
}

async function loadProjects(append = false): Promise<void> {
  const sequence = ++projectSequence;
  const query = element<HTMLInputElement>("project-query").value.trim();
  const more = element<HTMLButtonElement>("more-projects");
  more.disabled = true;
  element("project-status").textContent = "Loading projects…";
  try {
    const data = payload(await app.callServerTool({ name: "list_projects", arguments: { limit: 20,
      ...(query ? { query } : {}), ...(append && projectsCursor ? { cursor: projectsCursor } : {}) } }));
    if (sequence !== projectSequence) return;
    const page = records(data.projects).flatMap((value) => project(value) ?? []);
    projects = append ? [...projects, ...page.filter((value) => !projects.some((existing) => existing.id === value.id))] : page;
    projectsCursor = text(data.nextCursor, "");
    renderProjects();
    element("project-status").textContent = projects.length ? `${projects.length} ${projects.length === 1 ? "project" : "projects"}${projectsCursor ? " shown" : ""}` : "No matching projects.";
    notice();
  } catch (error) {
    if (sequence === projectSequence) { element("project-status").textContent = "Projects could not load. Search again to retry."; notice(error); }
  } finally { if (sequence === projectSequence) more.disabled = false; }
}

async function loadItems(append = false): Promise<void> {
  if (!selectedProjectId) return;
  const sequence = ++itemSequence;
  const projectId = selectedProjectId;
  const query = element<HTMLInputElement>("item-query").value.trim();
  const more = element<HTMLButtonElement>("more-items");
  more.disabled = true;
  element("item-status").textContent = "Loading items…";
  try {
    const data = payload(await app.callServerTool({ name: "list_items", arguments: { projectId, limit: 8,
      ...(query ? { query } : {}), ...(append && itemsCursor ? { cursor: itemsCursor } : {}) } }));
    if (sequence !== itemSequence || projectId !== selectedProjectId) return;
    const page = records(data.items).flatMap((value) => item(value) ?? []);
    if (page.some((value) => value.projectId !== projectId)) throw new Error("The server returned items from another project. Reload the workspace.");
    items = append ? [...items, ...page.filter((value) => !items.some((existing) => existing.id === value.id))] : page;
    itemsCursor = text(data.nextCursor, "");
    renderItems();
    element("item-status").textContent = items.length ? `${items.length} ${items.length === 1 ? "item" : "items"}${itemsCursor ? " shown" : ""} · search matches titles` : "No matching items. Search by title or address.";
    notice();
  } catch (error) {
    if (sequence === itemSequence) { element("item-status").textContent = "Items could not load. Search again to retry."; notice(error); }
  } finally { if (sequence === itemSequence) more.disabled = false; }
}

async function selectProject(id: string): Promise<void> {
  if (!idPattern.test(id)) return;
  selectedProjectId = id;
  selectedItem = undefined;
  labels = [];
  annotationQuery = "";
  annotationLimit = 25;
  items = [];
  itemsCursor = "";
  detailSequence++;
  element<HTMLInputElement>("item-query").value = "";
  renderProjects();
  renderItems();
  renderDetail();
  await loadItems();
}

async function selectItem(id: string): Promise<void> {
  if (!idPattern.test(id)) return;
  if (selectedItem?.id !== id) { annotationQuery = ""; annotationLimit = 25; }
  const sequence = ++detailSequence;
  element("detail").replaceChildren(node("p", "Loading item…", "text-muted"));
  try {
    const data = payload(await app.callServerTool({ name: "get_item", arguments: { id } }));
    if (sequence !== detailSequence) return;
    const value = item(data.item);
    if (!value || value.id !== id) throw new Error("The returned item does not match this selection. Select the item again.");
    selectedItem = value;
    labels = records(data.labels).filter((entry) => typeof entry.id === "string" && typeof entry.name === "string") as Label[];
    labelsCursor = text(data.labelsNextCursor, "");
    if (selectedProjectId !== value.projectId) { selectedProjectId = value.projectId; items = []; itemsCursor = ""; void loadItems(); }
    renderProjects();
    renderItems();
    renderDetail();
    notice();
  } catch (error) {
    if (sequence === detailSequence) {
      selectedItem = undefined;
      renderItems();
      element("detail").replaceChildren(node("h2", "Item could not load"), node("p", errorText(error), "text-muted"));
    }
  }
}

function metadata(values: Array<[string, string]>): HTMLDListElement {
  const output = node("dl", "", "metadata");
  for (const [label, value] of values) output.append(node("dt", label), node("dd", value));
  return output;
}

function rawDetails(title: string, value: unknown): HTMLDetailsElement {
  const details = node("details");
  details.append(node("summary", title));
  details.addEventListener("toggle", () => {
    if (details.open && details.children.length === 1) details.append(node("pre", JSON.stringify(value, null, 2)));
  });
  return details;
}

function canAttach(): boolean {
  return connected && (!!extensions.modelContext || !!app.getHostCapabilities()?.updateModelContext?.resourceLink);
}

function syncAttached(): void {
  const current = extensions.modelContext?.getCurrent();
  if (current !== undefined) attached = (current?.content ?? []).filter((value): value is ResourceLink =>
    value.type === "resource_link" && /^fieldwork:\/\/item\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value.uri));
}

async function attachItem(button: HTMLButtonElement, status: HTMLElement, value: Item): Promise<void> {
  if (!canAttach() || attachmentBusy) return;
  const link = itemLink(value);
  const exists = attached.some((entry) => entry.uri === link.uri);
  const next = exists ? attached.filter((entry) => entry.uri !== link.uri) : [...attached, link];
  attachmentBusy = true;
  button.disabled = true;
  try {
    if (extensions.modelContext) await extensions.modelContext.update({ content: next });
    else await app.updateModelContext({ content: next });
    attached = next;
    if (selectedItem?.id === value.id) {
      button.textContent = exists ? "Attach item to chat" : "Remove chat attachment";
      status.textContent = exists ? "Item removed from chat context." : "Item reference attached. The assistant can read its current saved revision.";
    }
  } catch (error) { if (selectedItem?.id === value.id) status.textContent = errorText(error); }
  finally { attachmentBusy = false; button.disabled = false; }
}

function renderDetail(): void {
  const detail = element("detail");
  const value = selectedItem;
  if (!value) {
    const empty = node("div", "", "empty-state");
    empty.append(node("h2", "Item details"), node("p", "Select an item to inspect its saved status, labels and annotations.", "text-muted"));
    detail.replaceChildren(empty);
    return;
  }
  const heading = node("div", "", "detail-heading");
  heading.append(node("h2", value.title), node("span", text(value.status, "unreviewed").replaceAll("_", " "), "badge"));
  const info = metadata([["Item", value.id], ["Revision", text(value.revision)], ["Adapter", text(value.adapter)], ["Updated", text(value.updatedAt)]]);
  const actions = node("div", "", "detail-actions");
  const actionStatus = node("p", "", "text-small text-muted");
  if (app.getHostCapabilities()?.openLinks) {
    const editor = node("button", "Open CRM editor", "btn");
    editor.type = "button";
    editor.addEventListener("click", async () => {
      try { if ((await app.openLink({ url: editorUrl })).isError) throw new Error("The host could not open the editor."); }
      catch (error) { actionStatus.textContent = errorText(error); }
    });
    actions.append(editor);
  } else {
    const editor = node("a", "Open CRM editor", "btn");
    editor.href = editorUrl;
    editor.target = "_blank";
    editor.rel = "noopener noreferrer";
    actions.append(editor);
  }
  if (canAttach()) {
    const attach = node("button", attached.some((entry) => entry.uri === itemLink(value).uri) ? "Remove chat attachment" : "Attach item to chat", "btn");
    attach.type = "button";
    attach.disabled = attachmentBusy;
    attach.addEventListener("click", () => { void attachItem(attach, actionStatus, value); });
    actions.append(attach);
  }
  const help = node("p", `In the editor, choose ${projects.find((entry) => entry.id === value.projectId)?.name ?? value.projectId} and search the title “${value.title}”.`, "editor-help text-small text-muted");
  detail.replaceChildren(heading, info, actions, actionStatus, help);
  if (text(value.notes, "")) {
    const notes = node("section", "", "detail-section");
    notes.append(node("h3", "Item notes"), node("p", text(value.notes), "notes"));
    detail.append(notes);
  }
  const annotations = records(value.annotations);
  const section = node("section", "", "detail-section");
  section.append(node("h3", `Annotations · ${annotations.length}`));
  const filter = node("input", "", "form-control annotation-search");
  filter.type = "search";
  filter.placeholder = "Find object, class or saved status";
  filter.setAttribute("aria-label", "Find annotations");
  filter.value = annotationQuery;
  const entries = node("div");
  const count = node("p", "", "text-small text-muted");
  const more = node("button", "More annotations", "btn");
  more.type = "button";
  function renderAnnotations(): void {
    const query = filter.value.trim().toLocaleLowerCase();
    const matches = annotations.filter((annotation) => {
      const data = objectMetadata(annotation, labels);
      return [annotation.id, data.label, data.kind, data.structureKind, data.status].map((value) => text(value, "")).join(" ").toLocaleLowerCase().includes(query);
    });
    entries.replaceChildren(...matches.slice(0, annotationLimit).map((annotation) => {
      const data = objectMetadata(annotation, labels);
      const entry = node("article", "", "annotation");
      const header = node("div", "", "annotation-header");
      header.append(node("h4", text(data.label, "Unlabelled object")), node("span", text(data.status, "Status not recorded").replaceAll("_", " "), "badge"));
      const values: Array<[string, string]> = [["Class", text(data.kind)], ["Geometry", text(record(annotation.geometry)?.type)]];
      if (data.structureKind) values.push(["Structure", text(data.structureKind).replaceAll("-", " ")]);
      if (typeof data.geometryReviewed === "boolean") values.push(["Outline", data.geometryReviewed ? "Reviewed" : "Pending review"]);
      entry.append(header, node("p", text(annotation.id), "annotation-id"), metadata(values));
      if (text(data.notes, "")) entry.append(node("p", text(data.notes), "notes"));
      entry.append(rawDetails("Annotation data", annotation));
      return entry;
    }));
    count.textContent = matches.length ? `${Math.min(annotationLimit, matches.length)} of ${matches.length} ${matches.length === 1 ? "annotation" : "annotations"} shown` : query ? "No matching annotations." : "No annotations saved.";
    more.hidden = matches.length <= annotationLimit;
  }
  filter.addEventListener("input", () => { annotationQuery = filter.value; annotationLimit = 25; renderAnnotations(); });
  more.addEventListener("click", () => { annotationLimit += 25; renderAnnotations(); });
  section.append(filter, count, entries, more);
  if (labelsCursor) {
    const moreLabels = node("button", "Load remaining labels", "btn");
    moreLabels.type = "button";
    const labelStatus = node("p", "Some labels are on the next page.", "text-small text-muted");
    moreLabels.addEventListener("click", async () => {
      const sequence = detailSequence;
      moreLabels.disabled = true;
      try {
        const data = payload(await app.callServerTool({ name: "list_labels", arguments: { projectId: value.projectId, cursor: labelsCursor, limit: 100 } }));
        if (sequence !== detailSequence) return;
        const page = records(data.labels).filter((entry) => typeof entry.id === "string" && typeof entry.name === "string") as Label[];
        labels = [...labels, ...page.filter((entry) => !labels.some((existing) => existing.id === entry.id))];
        labelsCursor = text(data.nextCursor, "");
        moreLabels.hidden = !labelsCursor;
        labelStatus.textContent = labelsCursor ? "More labels are available." : "All labels loaded.";
        renderAnnotations();
      } catch (error) { if (sequence === detailSequence) labelStatus.textContent = errorText(error); }
      finally { moreLabels.disabled = false; }
    });
    section.append(labelStatus, moreLabels);
  }
  detail.append(section, rawDetails("Saved item data", value));
  renderAnnotations();
}

function receiveLaunch(data: RecordData): void {
  receivedLaunch = true;
  projects = records(data.projects).flatMap((value) => project(value) ?? []);
  projectsCursor = text(data.nextCursor, "");
  selectedItem = item(data.item);
  selectedProjectId = selectedItem?.projectId ?? (typeof data.selectedProjectId === "string" && idPattern.test(data.selectedProjectId) ? data.selectedProjectId : "");
  labels = records(data.labels).filter((entry) => typeof entry.id === "string" && typeof entry.name === "string") as Label[];
  labelsCursor = text(data.labelsNextCursor, "");
  projectSequence++;
  itemSequence++;
  detailSequence++;
  annotationQuery = "";
  annotationLimit = 25;
  items = [];
  itemsCursor = "";
  renderProjects();
  renderItems();
  renderDetail();
  element("project-status").textContent = projects.length ? `${projects.length} ${projects.length === 1 ? "project" : "projects"}${projectsCursor ? " shown" : ""}` : "No projects available.";
  notice();
  if (connected && selectedProjectId) void loadItems();
}

async function applyDeepLink(): Promise<void> {
  const url = extensions.deepLink.getCurrent()?.url;
  if (!connected || !url || url === lastDeepLink) return;
  const match = /^(?:fieldwork:\/\/|\/)(project|item)\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})$/.exec(url);
  if (!match) return;
  lastDeepLink = url;
  if (match[1] === "item") await selectItem(match[2]);
  else await selectProject(match[2]);
}

function hostContext(context: ReturnType<App["getHostContext"]>): void {
  if (context?.theme) applyDocumentTheme(context.theme);
  if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
  syncAttached();
  if (connected) { renderDetail(); void applyDeepLink(); }
}

app.ontoolresult = (result) => {
  try { receiveLaunch(payload(result)); }
  catch (error) { notice(error); }
};
app.onhostcontextchanged = hostContext;
function bindSearch(formId: string, inputId: string, search: () => Promise<void>): void {
  const form = element<HTMLFormElement>(formId);
  const button = form.querySelector<HTMLButtonElement>("button");
  if (!button) throw new Error(`Missing search button: ${formId}`);
  // Hosts may sandbox the app without allow-forms. UI tool calls do not need
  // browser form submission, so handle both buttons and Enter directly.
  button.type = "button";
  button.addEventListener("click", () => { void search(); });
  form.addEventListener("submit", (event) => { event.preventDefault(); void search(); });
  element<HTMLInputElement>(inputId).addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); void search(); }
  });
}
bindSearch("project-search", "project-query", () => loadProjects());
bindSearch("item-search", "item-query", () => loadItems());
element("more-projects").addEventListener("click", () => { void loadProjects(true); });
element("more-items").addEventListener("click", () => { void loadItems(true); });
element("refresh").addEventListener("click", () => {
  void loadProjects();
  if (selectedProjectId) void loadItems();
  if (selectedItem) void selectItem(selectedItem.id);
});

void app.connect().then(async () => {
  connected = true;
  element("connection").textContent = "Connected";
  hostContext(app.getHostContext());
  setSearchEnabled();
  if (receivedLaunch && selectedProjectId) await loadItems();
  // Some hosts open a resource directly without an initial tool result.
  setTimeout(() => { if (!receivedLaunch && !projects.length) void loadProjects(); }, 200);
}).catch((error) => { element("connection").textContent = "Connection unavailable"; notice(error); });
