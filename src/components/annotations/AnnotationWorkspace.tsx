"use client";

/* Native image elements keep the editor usable in both Next and the standalone SPA. */
/* eslint-disable @next/next/no-img-element */

import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Code, ConnectError } from "@connectrpc/connect";
import { ArrowLeft, ArrowRight, Check, CheckCheck, ChevronDown, Circle, Crop, Download, FolderOpen, Globe2, ImagePlus, Layers3, List, LoaderCircle, MapPin, PanelRight, Plus, Redo2, RefreshCw, Save, Search, Settings2, Shapes, Tag, Trash2, Undo2, WifiOff, X } from "lucide-react";
import { getAnnotationAdapter, getAnnotationAdapters } from "../../lib/annotations/adapters";
import { api, apiError, connectionSettings, setConnection } from "../../lib/annotations/api";
import { downloadJson, readDraft, reconcileDraft, removeDraft, writeDraft, type LocalDraft } from "../../lib/annotations/drafts";
import { cloneItem, isDirty, LABEL_COLORS, normaliseItem, replaceVertex, STATUSES, uid, vertices, type AdapterInfo, type Geometry, type Item, type Label, type Position, type Project, type Tool } from "../../lib/annotations/model";
import { collectQueuePage, LINK_SEEK_PAGES, mergeQueue, normaliseTag, parseQueueSearch, queueCounter, readQueueLink, sharedTags, TAG_SCAN_MAX_PAGES, writeQueueLink, type QueuePage } from "../../lib/annotations/queue";
import { isReviewProject } from "../../lib/annotations/addr-scene";
import "leaflet/dist/leaflet.css";
import "./annotations.css";
import type { AnnotationEditorProps } from "./AnnotationCanvas";
import GoogleSignIn from "./GoogleSignIn";
import FieldworkLogin from "./FieldworkLogin";
import AgentReviews from "./AgentReviews";

interface EditorSession { saved: Item; draft: Item; past: Item[]; future: Item[] }
type ModalKind = "project" | "item" | "labels" | "connection" | null;
/** The search the loaded list belongs to; Load more and Next continue it even while the search box is being edited. */
interface LoadedQueue { key: string; projectId: string; text: string; tag: string }
const queueKey = (projectId: string, text: string, tag: string) => JSON.stringify([projectId, text, normaliseTag(tag)]);
/** A deep link's item that could not be opened (unknown id, or an item of another project). */
class LinkMiss extends Error {}

export default function AnnotationWorkspace() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [labels, setLabels] = useState<Label[]>([]);
  const [adapters, setAdapters] = useState<AdapterInfo[]>([]);
  const [cursor, setCursor] = useState("");
  const [session, setSession] = useState<EditorSession | null>(null);
  const [recovery, setRecovery] = useState<LocalDraft | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeLabelId, setActiveLabelId] = useState("");
  const [tool, setTool] = useState<Tool>("select");
  const [drawing, setDrawing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [accessRequired, setAccessRequired] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [storageError, setStorageError] = useState("");
  const [message, setMessage] = useState("");
  const [online, setOnline] = useState(true);
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState("");
  const [notice, setNotice] = useState("");
  const [restoring, setRestoring] = useState(true);
  const [queueLoading, setQueueLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [filter, setFilter] = useState("all");
  const [modal, setModal] = useState<ModalKind>(null);
  const [panel, setPanel] = useState<"queue" | "inspector" | null>(null);
  const [pendingNav, setPendingNav] = useState<{ run: () => void; label: string } | null>(null);
  const [viewState, setViewState] = useState<Record<string, unknown>>({});
  // The list position of the last list item opened: Next from an item opened outside the list continues from there.
  const [listAnchor, setListAnchor] = useState(-1);
  const loadGeneration = useRef(0);
  const queueGeneration = useRef(0);
  const loadedQueue = useRef<LoadedQueue>({ key: "", projectId: "", text: "", tag: "" });

  const searchText = parseQueueSearch(query).text;
  const item = session?.draft;
  const dirty = !!session && isDirty(session.saved, session.draft);
  const project = projects.find((entry) => entry.id === projectId);
  const selected = item?.annotations.find((annotation) => annotation.id === selectedId);
  const itemIndex = items.findIndex((entry) => entry.id === item?.id);
  if (itemIndex >= 0 && itemIndex !== listAnchor) setListAnchor(itemIndex);
  const reviewQueue = isReviewProject(projectId);
  // The list filter (All, Needs review / Unanswered, Images, Maps) also decides which item Next opens.
  const matchesFilter = (entry: Item) => filter === "all" || (filter === "unanswered" ? entry.status === "unreviewed" : filter === "review" ? entry.status !== "approved" && entry.status !== "rejected" : (getAnnotationAdapter(entry.adapter)?.mediaKind || entry.adapter) === filter);
  // The next matching item in the loaded list after the open item (after the last list item opened, when the open item is not in the
  // list); at the end of the loaded list a non-empty cursor still means there is a next item: openNext() loads the following page of
  // the same search first.
  const anchorIndex = itemIndex >= 0 ? itemIndex : Math.min(listAnchor, items.length - 1);
  const directNextId = items.slice(anchorIndex + 1).find((entry) => entry.id !== item?.id && matchesFilter(entry))?.id;
  const hasNext = !!item && (!!directNextId || !!cursor);
  const adapter = item ? getAnnotationAdapter(item.adapter) : undefined;
  const Editor = adapter?.Editor;
  const Inspector = adapter?.Inspector;
  const supported = !!Editor;
  const workspaceReady = projectsLoaded && !accessRequired;
  const reportFailure = useCallback((failure: unknown) => {
    const code = ConnectError.from(failure).code;
    if (code === Code.Unauthenticated || code === Code.PermissionDenied) { setAccessRequired(true); setPanel(null); }
    setError(apiError(failure));
  }, []);

  // `link` marks an item named by a deep link: an unknown id, or an item of another project, becomes a notice ("missing")
  // instead of an error. An item that is not in the loaded list stays out of it (the footer says so).
  const openItem = useCallback(async (id: string, annotationId?: string, link?: { projectId: string }): Promise<"opened" | "missing" | "failed" | "stale"> => {
    const generation = ++loadGeneration.current;
    setLoading(true); setError(""); setMessage(""); setRecovery(null); setSelectedId(null); setDrawing(false);
    try {
      const response = await api.item(id);
      if (link && response.item.projectId !== link.projectId) throw new LinkMiss(`The linked item “${id}” belongs to another project (“${response.item.projectId}”), so it was not opened here.`);
      if (response.labelsNextCursor) response.labels = (await api.labels(response.item.projectId)).labels;
      if (generation !== loadGeneration.current) return "stale";
      const document = normaliseItem(response.item);
      setSession({ saved: document, draft: cloneItem(document), past: [], future: [] });
      setViewState(annotationId ? { showInactive: true } : {}); setSelectedId(annotationId || null);
      setLabels(response.labels ?? []); setTool(document.adapter === "map" ? "pan" : "select");
      const local = readDraft(id);
      if (local && isDirty(document, local.item)) setRecovery(local);
      setPanel(null);
      return "opened";
    } catch (failure) {
      if (generation !== loadGeneration.current) return "stale";
      const code = failure instanceof LinkMiss ? Code.NotFound : ConnectError.from(failure).code;
      if (link && (code === Code.NotFound || code === Code.InvalidArgument)) {
        setNotice(failure instanceof LinkMiss ? failure.message : `The linked item “${id}” was not found in this project.`);
        return "missing";
      }
      reportFailure(failure); return "failed";
    }
    finally { if (generation === loadGeneration.current) setLoading(false); }
  }, [reportFailure]);

  // One queue page: a title search on the server, a tag filter here (ListItems has no tag filter).
  // Review queues hold a few hundred items; elsewhere a tagged load reads fewer whole-item pages before Load more.
  const fetchQueue = useCallback((id: string, from: string, text: string, tagValue: string, isCurrent?: () => boolean) =>
    collectQueuePage((pageCursor, limit) => api.items(id, pageCursor || undefined, text, limit, { tag: tagValue || undefined, summary: true }), from, tagValue, { isCurrent, maxPages: isReviewProject(id) ? TAG_SCAN_MAX_PAGES : 3 }), []);

  // `link` restores a deep link: its search text and tag, and its item (looked up a few pages ahead so Next keeps working).
  const openProject = useCallback(async (id: string, link?: { q?: string; tag?: string; item?: string }) => {
    const generation = ++loadGeneration.current;
    const parsed = parseQueueSearch(link?.q ?? "", true);
    const text = parsed.text, tagValue = (link?.tag || parsed.tag).trim();
    const queueGenerationAtOpen = ++queueGeneration.current;
    setListAnchor(-1);
    loadedQueue.current = { key: queueKey(id, text, tagValue), projectId: id, text, tag: tagValue };
    setProjectId(id); setLoading(true); setSession(null); setRecovery(null); setItems([]); setCursor(""); setQueueLoading(false); setLabels([]); setError(""); setQuery(text); setTag(tagValue); setFilter("all"); setActiveLabelId("");
    const current = () => generation === loadGeneration.current;
    try {
      const [documents, palette] = await Promise.all([fetchQueue(id, "", text, tagValue, current), api.labels(id)]);
      if (!current()) return;
      let list = documents.items, next = documents.nextCursor;
      const target = link?.item;
      const seekPages = isReviewProject(id) ? LINK_SEEK_PAGES : 3;
      for (let pages = 0; target && next && !list.some((entry) => entry.id === target) && pages < seekPages; pages++) {
        const page = await fetchQueue(id, next, text, tagValue, current);
        if (!current()) return;
        list = mergeQueue(list, page.items); next = page.nextCursor;
      }
      // A newer search typed meanwhile owns the list (and its cursor); this link still opens its item.
      if (queueGeneration.current === queueGenerationAtOpen) { setItems(list); setCursor(next); }
      setLabels(palette.labels ?? []);
      if (target) { if (await openItem(target, undefined, { projectId: id }) === "missing" && list[0]) await openItem(list[0].id); }
      else if (list[0]) await openItem(list[0].id);
    } catch (failure) { if (current()) reportFailure(failure); }
    finally { if (current()) setLoading(false); }
  }, [fetchQueue, openItem, reportFailure]);

  // Every (re)connection restores the place named by the URL (?project=&item=&q=&tag=), which the sync effect below keeps
  // current, so a deep link waits for a working connection and a reconnect returns to the same item.
  const refresh = useCallback(async () => {
    setLoading(true); setProjectsLoaded(false); setError(""); setNotice(""); setRestoring(true);
    try {
      // Resolve the authenticated draft workspace before loading any items.
      const info=await api.info();
      const result=await api.projects();
      setProjectsLoaded(true); setAccessRequired(false);
      const list = result.projects ?? [];
      setProjects(list); setAdapters(info.adapters ?? []);
      const link = readQueueLink(window.location.search);
      let linked = link.project;
      if (!linked && link.item) {
        try { linked = (await api.item(link.item)).item.projectId; }
        catch { setNotice(`The linked item “${link.item}” was not found on this server.`); }
      }
      const known = !!linked && list.some((entry) => entry.id === linked);
      if (linked && !known) setNotice(`The linked project “${linked}” is not available on this server${list[0] ? `; “${list[0].name}” is open instead` : ""}.`);
      const target = known ? linked : list[0]?.id;
      if (target) await openProject(target, known ? { q: link.q, tag: link.tag, item: link.item } : undefined);
      else { setSession(null); setProjectId(""); setItems([]); }
    } catch (failure) { reportFailure(failure); }
    finally { setLoading(false); setRestoring(false); }
  }, [openProject, reportFailure]);

  // Begin the initial external service load after client mount.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    // openProject loads the first page of its search; this effect reloads the list when the title text or the tag changes,
    // including clearing them. A tag token still being typed in the box is not part of the search yet.
    if (!projectId || loadedQueue.current.key === queueKey(projectId, searchText, tag)) return;
    const generation = ++queueGeneration.current;
    const timer = setTimeout(() => {
      if (queueGeneration.current !== generation) return; // openProject has loaded another search since
      loadedQueue.current = { key: queueKey(projectId, searchText, tag), projectId, text: searchText, tag };
      setQueueLoading(true);
      fetchQueue(projectId, "", searchText, tag, () => queueGeneration.current === generation).then((page) => {
        if (queueGeneration.current !== generation) return;
        setListAnchor(-1);
        setItems(page.items); setCursor(page.nextCursor);
      }, (failure: unknown) => { if (queueGeneration.current === generation) { loadedQueue.current = { ...loadedQueue.current, key: "" }; reportFailure(failure); } })
        .finally(() => { if (queueGeneration.current === generation) setQueueLoading(false); });
    }, 300);
    return () => { clearTimeout(timer); };
  }, [projectId, searchText, tag, fetchQueue, reportFailure]);
  useEffect(() => {
    // Keep ?project=&item=&q=&tag= current without adding history entries; never while a link is being restored or loaded.
    if (restoring || loading || !workspaceReady || !projectId) return;
    const next = writeQueueLink(window.location.href, { project: projectId, item: item?.projectId === projectId ? item.id : "", q: searchText, tag });
    if (next !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.replaceState(window.history.state, "", next);
  }, [restoring, loading, workspaceReady, projectId, item?.id, item?.projectId, searchText, tag]);
  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    update(); window.addEventListener("online", update); window.addEventListener("offline", update);
    return () => { window.removeEventListener("online", update); window.removeEventListener("offline", update); };
  }, []);
  useEffect(() => {
    if (!session || recovery) return;
    // Synchronise the document to browser storage and report actual persistence failure.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    try { reconcileDraft(session.draft, dirty, !!recovery); setStorageError(""); }
    catch { setStorageError("This browser cannot keep a recovery copy. Save your changes or download your draft before leaving."); }
  }, [session, dirty, recovery]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => { if (dirty || drawing) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty, drawing]);

  function commit(update: (draft: Item) => Item) {
    if (saving || recovery) return;
    setSession((current) => {
      if (!current) return current;
      const next = update(cloneItem(current.draft));
      if (!isDirty(current.draft, next)) return current;
      return { ...current, draft: next, past: [...current.past.slice(-49), current.draft], future: [] };
    });
    setMessage("");
  }
  const undo = useCallback(() => {
    if (saving || drawing || recovery) return;
    setSession((current) => {
      if (!current?.past.length) return current;
      return { ...current, draft: current.past[current.past.length - 1], past: current.past.slice(0, -1), future: [current.draft, ...current.future] };
    });
  }, [saving, drawing, recovery]);
  const redo = useCallback(() => {
    if (saving || drawing || recovery) return;
    setSession((current) => {
      if (!current?.future.length) return current;
      return { ...current, draft: current.future[0], past: [...current.past, current.draft], future: current.future.slice(1) };
    });
  }, [saving, drawing, recovery]);

  // `draftOverride` lets an adapter save a document it has just committed (the session state has not
  // re-rendered yet); without it the current session draft is saved and a clean draft is a no-op success.
  const save = useCallback(async (draftOverride?: Item): Promise<boolean> => {
    if (!session || saving || drawing || recovery) return false;
    const draft = draftOverride ?? session.draft;
    if (!draftOverride && !dirty) return true;
    setSaving(true); setError("");
    const savingId = draft.id;
    try {
      const result = await api.saveItem(draft, draft.revision);
      const saved = normaliseItem(result.item);
      setSession({ saved, draft: cloneItem(saved), past: [], future: [] });
      setItems((current) => current.map((entry) => entry.id === savingId ? saved : entry));
      try { removeDraft(savingId); } catch { /* The server save has succeeded. */ }
      setMessage("Changes saved"); return true;
    } catch (failure) { reportFailure(failure); return false; }
    finally { setSaving(false); }
  }, [session, saving, drawing, recovery, dirty, reportFailure]);

  useEffect(() => {
    const shortcuts = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (event.defaultPrevented || modal || pendingNav || target.closest("input,textarea,select,[contenteditable=true]")) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") { event.preventDefault(); if (event.shiftKey) redo(); else undo(); return; }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") { event.preventDefault(); void save(); return; }
      if (event.metaKey || event.ctrlKey || event.altKey || saving || recovery) return;
      const tools: Record<string, Tool> = { v: "select", h: "pan", p: "point", l: "line", r: "rectangle", g: "polygon", c: "crop" };
      const next = tools[event.key.toLowerCase()];
      if (next && (next !== "crop" || item?.adapter === "image")) { setTool(next); event.preventDefault(); }
    };
    window.addEventListener("keydown", shortcuts);
    return () => window.removeEventListener("keydown", shortcuts);
  }, [undo, redo, save, modal, pendingNav, saving, recovery, item?.adapter]);

  function navigate(run: () => void, label: string) {
    if (saving) return;
    if (dirty || drawing || recovery) setPendingNav({ run, label });
    else run();
  }
  function removeAnnotation() {
    if (!selectedId) return;
    commit((draft) => ({ ...draft, annotations: draft.annotations.filter((annotation) => annotation.id !== selectedId) }));
    setSelectedId(null);
  }
  function updateGeometry(id: string, geometry: Geometry) {
    commit((draft) => ({ ...draft, annotations: draft.annotations.map((annotation) => annotation.id === id ? { ...annotation, geometry } : annotation) }));
  }
  function addGeometry(geometry: Geometry, shape?: string) {
    const id = uid();
    commit((draft) => ({ ...draft, annotations: [...draft.annotations, { id, labelId: activeLabelId, geometry, properties: shape ? { shape } : {} }] }));
    setSelectedId(id);
  }
  // The next page of the loaded search (same title text and tag), appended; null when superseded or failed.
  async function loadMore(from = cursor): Promise<QueuePage | null> {
    const loaded = loadedQueue.current;
    if (!from || loaded.projectId !== projectId) return null;
    const generation = queueGeneration.current;
    setQueueLoading(true);
    try {
      const page = await fetchQueue(projectId, from, loaded.text, loaded.tag, () => generation === queueGeneration.current);
      if (generation !== queueGeneration.current) return null;
      setItems((current) => mergeQueue(current, page.items)); setCursor(page.nextCursor);
      return page;
    } catch (failure) { if (generation === queueGeneration.current) reportFailure(failure); return null; }
    finally { if (generation === queueGeneration.current) setQueueLoading(false); }
  }
  // Next item across pages: the following loaded item, else the first new item of the next page(s) of the same search.
  async function openNext() {
    if (directNextId) { await openItem(directNextId); return; }
    let from = cursor;
    for (let attempt = 0; from && attempt < 5; attempt++) {
      const page = await loadMore(from);
      if (!page) return;
      const fresh = page.items.find((entry) => entry.id !== item?.id && !items.some((value) => value.id === entry.id) && matchesFilter(entry));
      if (fresh) { await openItem(fresh.id); return; }
      from = page.nextCursor;
    }
    setMessage(from ? "No further items yet · use Load more items" : "End of this list");
  }
  const visibleItems = items.filter(matchesFilter);
  // Tags every loaded item carries (the queue's shared set tag) say nothing on a row; the row chips leave them out.
  const commonTags = sharedTags(items);
  const blocked = saving || !!recovery || loading || accessRequired;
  const editorProps: AnnotationEditorProps | undefined = item ? {
    item, labels, selectedId, activeLabelId, tool, disabled: blocked || !supported,
    onToolChange: setTool, onSelect: setSelectedId, onAdd: addGeometry, onUpdate: updateGeometry,
    onCrop: (crop) => commit((draft) => ({ ...draft, crop })),
    onMapView: (center, zoom) => commit((draft) => ({ ...draft, source: { ...draft.source, center, zoom } })),
    onPendingChange: setDrawing, onItemChange: (next) => commit(() => next),
    onNavigateItem: (id, annotation) => navigate(() => void openItem(id, annotation), "open another scene"),
    viewState, onViewStateChange: setViewState,
    savedItem: session?.saved,
    onSaveItem: (draft) => { commit(() => draft); return save(draft); },
    onNextItem: hasNext ? () => void openNext() : undefined,
  } : undefined;

  // An unopened or clean signed-out workspace gets the dedicated login. An
  // expired session with a draft keeps its recovery/navigation guards available.
  if ((!session && !projectsLoaded) || (accessRequired && !dirty && !drawing && !recovery)) return <>
    <FieldworkLogin server={connectionSettings().url} loading={loading} unavailable={!loading && !accessRequired}
      onConnected={() => { setConnection(connectionSettings().url, ""); void refresh(); }}
      onSettings={() => setModal("connection")} onRetry={() => void refresh()} />
    {modal === "connection" && <ConnectionDialog onClose={() => setModal(null)} onSave={() => { setModal(null); void refresh(); }} />}
  </>;

  return <main className="fw-app">
    <header className="fw-topbar">
      <a className="fw-brand" href="#" onClick={(event) => event.preventDefault()} aria-label="Fieldwork annotation workspace"><span className="fw-brand-mark"><Layers3 size={21} strokeWidth={1.8} /></span>Fieldwork</a>
      <div className="fw-topbar-divider" /><span className="fw-workspace-name">Annotation workspace</span>
      <div className="fw-topbar-actions">
        {!online && <span className="fw-offline"><WifiOff size={14} /><span>Offline</span></span>}
        {project && <button className="fw-button fw-quiet fw-export" disabled={exporting} onClick={async () => { setExporting(true); try { downloadJson(await api.exportProject(project.id), `${project.name.replace(/[^a-z0-9]/gi, "-")}.json`); } catch (failure) { setError(apiError(failure)); } finally { setExporting(false); } }}><Download size={15} />{exporting ? "Exporting…" : "Export"}</button>}
        <button className="fw-button fw-quiet" aria-label="Connection settings" onClick={() => navigate(() => setModal("connection"), "change connection settings")}><Settings2 size={17} /><span className="fw-connection-text">Connection</span></button>
      </div>
    </header>
    <div className="fw-layout">
      {panel && <button className="fw-panel-backdrop" aria-label="Close side panel" onClick={() => setPanel(null)} />}
      <aside className={`fw-queue ${panel === "queue" ? "is-open" : ""}`} aria-label="Project and item queue">
        <div className="fw-project-heading"><div className="fw-overline">Project</div><button className="fw-icon-button fw-panel-close" aria-label="Close item queue" onClick={() => setPanel(null)}><X size={18} /></button></div>
        <div className="fw-project-picker"><select aria-label="Current project" value={workspaceReady ? projectId : ""} onChange={(event) => { const id = event.target.value; navigate(() => void openProject(id), "switch projects"); }} disabled={saving || !workspaceReady}>{(!workspaceReady || !projects.length) && <option value="">{loading ? "Loading projects…" : accessRequired ? "Connection required" : projectsLoaded ? "No projects yet" : "Projects unavailable"}</option>}{workspaceReady && projects.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}</select><ChevronDown size={16} /><button className="fw-icon-button" aria-label="Create project" title="Create project" disabled={!workspaceReady} onClick={() => navigate(() => setModal("project"), "create a project")}><Plus size={18} /></button></div>
        {workspaceReady && project?.description && <p className="fw-project-description">{project.description}</p>}
        <div className="fw-queue-heading"><h2>Items {workspaceReady && <span>{items.length}{cursor ? "+" : ""}</span>}</h2><button className="fw-button fw-add-button" disabled={!workspaceReady || !projectId || saving} onClick={() => navigate(() => setModal("item"), "add an item")}><Plus size={15} />Add item</button></div>
        <label className="fw-search"><Search size={15} /><input placeholder="Search titles, or tag:name…" aria-label="Find an item" title="Type words from item titles. tag:name followed by a space or Enter shows only items with that tag." disabled={!workspaceReady || restoring} value={query}
          onChange={(event) => { const value = event.target.value, parsed = parseQueueSearch(value); if (!parsed.tag) { setQuery(value); return; } setTag(parsed.tag); setQuery([parsed.text, parsed.pendingTag && `tag:${parsed.pendingTag}`].filter(Boolean).join(" ") + (parsed.text && /\s$/.test(value) ? " " : "")); }}
          onKeyDown={(event) => { if (event.key !== "Enter") return; const parsed = parseQueueSearch(query, true); if (parsed.tag) { event.preventDefault(); setTag(parsed.tag); setQuery(parsed.text); } }} />{query && <button aria-label="Clear search" onClick={() => setQuery("")}><X size={14} /></button>}</label>
        {tag && <div className="fw-active-tag" data-testid="fw-active-tag"><span>Only tag</span><button type="button" className="fw-tag-chip is-active" aria-label={`Remove tag filter ${tag}`} title="Show items with any tag" disabled={!workspaceReady} onClick={() => setTag("")}>{tag}<X size={11} /></button></div>}
        <p className="fw-queue-filter-scope">Filter loaded items</p>
        <div className="fw-queue-filters" role="group" aria-label="Filter loaded items">{[{ value: "all", label: "All" }, reviewQueue ? { value: "unanswered", label: "Unanswered" } : { value: "review", label: "Needs review" }, { value: "image", label: "Images" }, { value: "map", label: "Maps" }].map((entry) => <button key={entry.value} className={filter === entry.value ? "is-active" : ""} aria-pressed={filter === entry.value} disabled={!workspaceReady} onClick={() => setFilter(entry.value)}>{entry.label}</button>)}</div>
        <div className="fw-items">
          {workspaceReady && visibleItems.map((entry) => {
            const definition = getAnnotationAdapter(entry.adapter);
            const ItemIcon = definition?.Icon || Shapes;
            const capture = typeof entry.source.sceneId === "string" ? entry.source.sceneId : "";
            const tags = (entry.tags ?? []).filter((value) => value.trim() && !commonTags.has(normaliseTag(value)));
            return <div key={entry.id} className="fw-item-row"><button className={`fw-item ${item?.id === entry.id ? "is-active" : ""}`} title={capture ? `${entry.title} · ${capture}` : entry.title} aria-current={item?.id === entry.id ? "true" : undefined} disabled={saving} onClick={() => { if (entry.id !== item?.id) navigate(() => void openItem(entry.id), "open another item"); else setPanel(null); }}>
            <span className={`fw-thumbnail fw-thumbnail-${entry.adapter}`}>{entry.adapter === "image" && entry.source.url ? <img src={entry.source.url} alt="" loading="lazy" /> : <ItemIcon size={22} strokeWidth={1.3} />}</span>
            <span className="fw-item-copy"><strong>{entry.title}</strong><span>{definition?.name || entry.adapter} <span aria-hidden="true">·</span> {entry.annotationCount ?? entry.annotations?.length ?? 0} marks</span>{capture && <small className="fw-capture-id">{capture}</small>}</span>
            <span className={`fw-status-dot fw-status-${entry.status}`} title={STATUSES.find((status) => status.value === entry.status)?.label}><span className="sr-only">{STATUSES.find((status) => status.value === entry.status)?.label}</span></span>
          </button>{tags.length > 0 && <div className="fw-item-tags" aria-label={`Tags of ${entry.title}`}>{tags.slice(0, 4).map((value) => <button key={value} type="button" className={`fw-tag-chip ${normaliseTag(value) === normaliseTag(tag) ? "is-active" : ""}`} aria-pressed={normaliseTag(value) === normaliseTag(tag)} title={`Show only items tagged ${value}`} disabled={!workspaceReady} onClick={() => setTag(value.trim())}>{value}</button>)}{tags.length > 4 && <span className="fw-tag-more" title={tags.slice(4).join(", ")}>+{tags.length - 4}</span>}</div>}</div>; })}
          {!loading && !queueLoading && (!workspaceReady || !visibleItems.length) && <div className="fw-queue-empty"><FolderOpen size={24} /><p>{accessRequired ? "Connect to load your projects and items." : !projectsLoaded ? "Projects could not be loaded." : query || tag || items.length ? "No matching items." : projectId ? "Add an image or map area to begin." : "Create a project to organise your annotations."}</p>{workspaceReady && !projectId && <button className="fw-button" onClick={() => setModal("project")}>Create project</button>}</div>}
          {workspaceReady && cursor && <button className="fw-button fw-load-more" disabled={loading || queueLoading} onClick={() => void loadMore()}>{queueLoading ? "Loading…" : "Load more items"}</button>}
        </div>
        <div className="fw-queue-footer"><Layers3 size={14} /><span>{!workspaceReady ? "Waiting for a connection" : queueLoading ? (tag ? "Reading project items for this tag…" : "Searching project items…") : cursor ? "More matches available · use Load more" : "All search results loaded"}</span></div>
      </aside>
      <section className="fw-main" aria-label="Annotation editor">
        <div className="fw-item-heading">
          <button className="fw-icon-button fw-queue-toggle" aria-label="Open item queue" onClick={() => setPanel("queue")}><List size={20} /></button>
          <div className="fw-item-title"><div className="fw-breadcrumb">{project?.name || "Your workspace"}{item && <><span>/</span>{adapter?.name || item.adapter}</>}</div><h1>{accessRequired ? "Connect to your workspace" : item?.title || (project ? "Your project" : "Your annotation workspace")}</h1></div>
          {item && <div className="fw-editor-actions"><div className="fw-history"><button className="fw-icon-button" aria-label="Undo edit" title="Undo (⌘/Ctrl Z)" disabled={!session?.past.length || blocked || drawing} onClick={undo}><Undo2 size={17} /></button><button className="fw-icon-button" aria-label="Redo edit" title="Redo (⌘/Ctrl Shift Z)" disabled={!session?.future.length || blocked || drawing} onClick={redo}><Redo2 size={17} /></button></div><button className="fw-button fw-primary fw-save" disabled={!dirty || blocked || drawing} onClick={() => void save()}>{saving ? <LoaderCircle size={16} className="fw-spin" /> : dirty ? <Save size={16} /> : <Check size={16} />}<span>{saving ? "Saving…" : dirty ? "Save changes" : "Saved"}</span></button><button className="fw-icon-button fw-inspector-toggle" aria-label="Open inspector" onClick={() => setPanel("inspector")}><PanelRight size={19} /></button></div>}
        </div>
        {(error || storageError) && <div className="fw-notice fw-error" role="alert"><div><strong>{accessRequired ? "Connection required" : storageError ? "Recovery copy unavailable" : "Could not complete the request"}</strong><p>{error || storageError}</p></div><div className="fw-notice-actions">{item && <button className="fw-button" onClick={() => downloadJson(item, `${item.id}-draft.json`)}>Download draft</button>}{accessRequired ? <button className="fw-button" onClick={() => navigate(() => setModal("connection"), "change connection settings")}>Open Connection</button> : <button className="fw-button" onClick={() => navigate(() => { if (item) void openItem(item.id); else if (projectId) void openProject(projectId); else void refresh(); }, "reload from the server")}><RefreshCw size={14} />Retry load</button>}<button className="fw-icon-button" aria-label="Dismiss error" onClick={() => { setError(""); setStorageError(""); }}><X size={16} /></button></div></div>}
        {notice && <div className="fw-notice fw-link-notice" role="status" data-testid="fw-link-notice"><div><strong>Link opened differently</strong><p>{notice}</p></div><div className="fw-notice-actions"><button className="fw-icon-button" aria-label="Dismiss notice" onClick={() => setNotice("")}><X size={16} /></button></div></div>}
        {recovery && <div className="fw-notice fw-recovery" role="status"><div><strong>A local draft is available</strong><p>{recovery.baseRevision !== item?.revision ? "The server has a different revision. Restore to inspect your draft, or download it before comparing the server copy." : "Restore your unsaved changes from this device. They have not been sent to the server."}</p></div><div className="fw-notice-actions"><button className="fw-button fw-primary" onClick={() => { setSession((current) => current ? { ...current, draft: recovery.item, past: [current.draft], future: [] } : current); setRecovery(null); }}>Restore draft</button><button className="fw-button" onClick={() => downloadJson(recovery.item, `${recovery.item.id}-draft.json`)}>Download</button><button className="fw-button" onClick={() => { try { removeDraft(recovery.item.id); } catch {} setRecovery(null); }}>Use server copy</button></div></div>}
        {accessRequired && !loading ? <div className="fw-welcome"><Settings2 size={30} /><h2>Sign in to Fieldwork</h2><p>Sign in with an allowed Google account or a workspace access token.<br />Your existing projects and feedback will be ready to review.</p><button className="fw-button fw-primary" onClick={() => navigate(() => setModal("connection"), "change connection settings")}><Settings2 size={17} />Sign in</button></div>
          : !projectsLoaded && !loading ? <div className="fw-welcome"><RefreshCw size={30} /><h2>Workspace unavailable</h2><p>Your projects could not be loaded.<br />Check the connection, then try again.</p><button className="fw-button fw-primary" onClick={() => void refresh()}><RefreshCw size={17} />Retry connection</button></div>
          : item && Editor ? <Editor key={item.id} {...editorProps!} /> : item ? <div className="fw-welcome"><Shapes size={30} /><h2>Viewer unavailable</h2><p>The “{item.adapter}” editor is not installed. Its saved data is preserved.</p></div> : <div className="fw-welcome">
          <div className="fw-welcome-illustration" aria-hidden="true"><div className="fw-empty-grid" /><svg viewBox="0 0 240 160"><path d="M38 104 L78 43 L154 31 L203 78 L180 128 L94 140 Z" fill="#2563eb0d" stroke="#2563eb" strokeWidth="1.6" />{[[38,104],[78,43],[154,31],[203,78],[180,128],[94,140]].map(([x,y],i) => <circle key={i} cx={x} cy={y} r="4" fill="white" stroke="#2563eb" strokeWidth="1.6" />)}<rect x="88" y="63" width="74" height="29" rx="6" fill="white" stroke="#e2e8f0" /><text x="125" y="82" textAnchor="middle" fill="#334155" fontSize="11">Observation</text></svg></div>
          {loading ? <><LoaderCircle size={24} className="fw-spin" /><h2>Opening your workspace…</h2></> : <><h2>{project ? "Add an image or map area" : "Create your first project"}</h2><p>Annotate an image or a place on the map.<br />Draw, label, and review the details that matter.</p><button className="fw-button fw-primary" onClick={() => setModal(project ? "item" : "project")}><Plus size={17} />{project ? "Add your first item" : "Create a project"}</button><div className="fw-welcome-types"><span><ImagePlus size={16} />Image annotation</span><span><Globe2 size={16} />Global mapping</span></div></>}
        </div>}
        <div className="fw-statusbar" role="status"><span>{loading ? <LoaderCircle size={13} className="fw-spin" /> : dirty ? <Circle size={8} fill="currentColor" /> : <CheckCheck size={14} />}{loading ? "Loading…" : accessRequired ? "Connect to load your workspace" : !projectsLoaded ? "Workspace not loaded" : drawing ? "Finish or cancel your drawing" : dirty ? (storageError ? "Unsaved changes · recovery copy unavailable" : "Unsaved changes · recovery copy on this device") : message || (item ? `All changes saved · revision ${item.revision}` : "Ready when you are")}</span>{item && <span>{item.annotations.length} {item.annotations.length === 1 ? "annotation" : "annotations"}</span>}</div>
      </section>
      <aside className={`fw-inspector ${panel === "inspector" ? "is-open" : ""}`} aria-label="Annotation inspector">
        <div className="fw-inspector-title"><h2>Inspector</h2><button className="fw-icon-button fw-panel-close" aria-label="Close inspector" onClick={() => setPanel(null)}><X size={18} /></button><span className="fw-shortcut-hint">Details & review</span></div>
        {item && !accessRequired ? <>
          <div className="fw-inspector-scroll"><AgentReviews item={item} annotationId={selectedId || undefined} />{Inspector ? <Inspector key={item.id} {...editorProps!} disabled={blocked || drawing} /> : <><fieldset disabled={blocked || !supported} className="fw-inspector-fields">
            <section className="fw-inspector-section"><div className="fw-section-heading"><h3><Tag size={15} />Labels</h3><button className="fw-text-button" onClick={() => setModal("labels")}>Manage</button></div><p className="fw-field-help">{selected ? "Label for the selected annotation." : "Choose a label for your next mark."}</p><div className="fw-label-list"><button className={`fw-label-choice ${!(selected?.labelId ?? activeLabelId) ? "is-active" : ""}`} aria-pressed={!(selected?.labelId ?? activeLabelId)} onClick={() => { setActiveLabelId(""); if (selected) commit((draft) => ({ ...draft, annotations: draft.annotations.map((annotation) => annotation.id === selected.id ? { ...annotation, labelId: "" } : annotation) })); }}><span className="fw-label-dot fw-unlabelled" />Unlabelled{!(selected?.labelId ?? activeLabelId) && <Check size={15} />}</button>{labels.map((label) => <button key={label.id} className={`fw-label-choice ${(selected?.labelId ?? activeLabelId) === label.id ? "is-active" : ""}`} aria-pressed={(selected?.labelId ?? activeLabelId) === label.id} onClick={() => { setActiveLabelId(label.id); if (selected) commit((draft) => ({ ...draft, annotations: draft.annotations.map((annotation) => annotation.id === selected.id ? { ...annotation, labelId: label.id } : annotation) })); }}><span className="fw-label-dot" style={{ background: label.color }} />{label.name}{(selected?.labelId ?? activeLabelId) === label.id && <Check size={15} />}</button>)}</div>{!labels.length && <button className="fw-button fw-full" onClick={() => setModal("labels")}><Plus size={14} />Create a label</button>}</section>
            <section className="fw-inspector-section"><div className="fw-section-heading"><h3><Shapes size={15} />Annotations <span>{item.annotations.length}</span></h3></div>{!item.annotations.length ? <p className="fw-field-help">Choose a drawing tool to add your first mark.</p> : <div className="fw-annotation-list">{item.annotations.map((annotation, index) => <button key={annotation.id} className={selectedId === annotation.id ? "is-active" : ""} onClick={() => { setSelectedId(annotation.id); setTool("select"); }}><span className="fw-label-dot" style={{ background: labels.find((label) => label.id === annotation.labelId)?.color || "#64748b" }} /><span>{labels.find((label) => label.id === annotation.labelId)?.name || `Mark ${index + 1}`}</span><small>{annotation.geometry.type === "LineString" ? "Line" : annotation.geometry.type}</small></button>)}</div>}
              {selected && <div className="fw-selection-detail"><div className="fw-selection-heading"><strong>Selected mark</strong><button className="fw-icon-button fw-danger" title="Delete selected annotation" aria-label="Delete selected annotation" onClick={removeAnnotation}><Trash2 size={15} /></button></div><label className="fw-field">Annotation note<textarea rows={2} placeholder="Describe this mark…" value={selected.notes ?? ""} onChange={(event) => commit((draft) => ({ ...draft, annotations: draft.annotations.map((annotation) => annotation.id === selected.id ? { ...annotation, notes: event.target.value } : annotation) }))} /></label><details className="fw-coordinate-details"><summary>Edit coordinates</summary><p className="fw-field-help">{item.adapter === "image" ? "X and Y are fractions of the original image (0–1)." : "Longitude and latitude in decimal degrees."}</p><div className="fw-vertices">{vertices(selected.geometry).map((point, index) => <CoordinateRow key={`${selected.id}-${index}`} index={index} value={point} image={item.adapter === "image"} onChange={(position) => updateGeometry(selected.id, replaceVertex(selected.geometry, index, position))} />)}</div></details></div>}
              <details className="fw-coordinate-details fw-add-coordinates"><summary>Add a point by coordinates</summary><CoordinateRow index={0} value={item.adapter === "image" ? [.5, .5] : item.source.center ?? [0, 20]} image={item.adapter === "image"} add onChange={(position) => addGeometry({ type: "Point", coordinates: position })} /></details>
            </section>
            {item.crop && <section className="fw-inspector-section"><div className="fw-section-heading"><h3><Crop size={15} />Image crop</h3><button className="fw-text-button" onClick={() => commit((draft) => ({ ...draft, crop: undefined }))}>Reset crop</button></div><p className="fw-field-help">The original image and annotation coordinates are preserved.</p></section>}
            <section className="fw-inspector-section"><div className="fw-section-heading"><h3><CheckCheck size={15} />Human review</h3></div><label className="fw-field">Status<select value={item.status} onChange={(event) => commit((draft) => ({ ...draft, status: event.target.value as Item["status"] }))}>{STATUSES.map((status) => <option key={status.value} value={status.value}>{status.label}</option>)}</select></label><label className="fw-field">Review notes<textarea rows={4} placeholder="What should the next reviewer know?" value={item.notes} onChange={(event) => commit((draft) => ({ ...draft, notes: event.target.value }))} /></label><label className="fw-field">Tags<input value={item.tags.join(", ")} placeholder="Add tags, separated by commas" onChange={(event) => commit((draft) => ({ ...draft, tags: event.target.value.split(",").map((value) => value.trimStart()) }))} /><span className="fw-field-help">Separate tags with commas.</span></label></section>
          </fieldset><section className="fw-inspector-section fw-source-details"><h3>Source</h3><p>{item.adapter === "map" ? "OpenStreetMap" : "Original image"}</p>{item.source.url && <a href={item.source.url} target="_blank" rel="noreferrer">Open source <ArrowRight size={12} /></a>}<p className="fw-field-help">{item.adapter === "map" ? "Map marks are stored as geographic coordinates." : "Image marks are stored against the original image dimensions."}</p></section></>}</div>
          <div className="fw-inspector-footer"><span data-testid="fw-queue-counter" title={queueCounter(itemIndex, items.length, !!cursor)}>{queueCounter(itemIndex, items.length, !!cursor)}</span><div><button className="fw-icon-button" aria-label="Previous item" disabled={itemIndex <= 0 || saving} onClick={() => navigate(() => void openItem(items[itemIndex - 1].id), "open the previous item")}><ArrowLeft size={17} /></button><button className="fw-button" data-testid="fw-next-item" title={directNextId ? "Open the next item" : "Load the next page of this list, then open its first item"} disabled={!hasNext || saving || queueLoading} onClick={() => navigate(() => void openNext(), "open the next item")}>Next item<ArrowRight size={16} /></button><button className="fw-button fw-primary" data-testid="fw-save-next" title="Save this item, then open the next one (⌘/Ctrl S saves without moving on)" disabled={!dirty || blocked || drawing || !hasNext || queueLoading} onClick={async () => { if (hasNext && await save()) void openNext(); }}>Save & next item</button></div></div>
          <div className="fw-inspector-save"><div role="status"><strong>{saving ? "Saving feedback…" : recovery ? "Local draft available" : drawing ? "Finish your drawing" : dirty ? "Unsaved changes" : `Saved · revision ${item.revision}`}</strong><span>{saving ? "Sending to the workspace" : recovery ? "Review the recovery copy before saving." : drawing ? "Finish or cancel before saving." : error ? error : storageError ? storageError : dirty ? "Your feedback is not saved yet." : "All changes saved to the workspace."}</span></div><button className="fw-button fw-primary" disabled={!dirty || blocked || drawing} onClick={() => void save()}>{saving ? <LoaderCircle size={16} className="fw-spin" /> : dirty ? <Save size={16} /> : <Check size={16} />}{saving ? "Saving…" : dirty ? "Save changes" : "Saved"}</button></div>
        </> : <div className="fw-inspector-empty"><Shapes size={28} strokeWidth={1.3} /><p>Select an item to inspect its annotations and review notes.</p></div>}
      </aside>
    </div>
    {modal === "project" && <ProjectDialog onClose={() => setModal(null)} onCreate={async (name, description) => { const response = await api.saveProject({ name, description }); setProjects((current) => [...current, response.project]); setModal(null); await openProject(response.project.id); }} />}
    {modal === "item" && <ItemDialog projectId={projectId} adapters={adapters} onClose={() => setModal(null)} onCreate={async (document) => { const response = await api.saveItem(document, 0); setItems((current) => [...current, response.item]); setModal(null); await openItem(response.item.id); }} />}
    {modal === "labels" && <LabelsDialog labels={labels} projectId={projectId} onClose={() => setModal(null)} onSaved={(label) => setLabels((current) => current.some((entry) => entry.id === label.id) ? current.map((entry) => entry.id === label.id ? label : entry) : [...current, label])} onDeleted={(id) => { setLabels((current) => current.filter((entry) => entry.id !== id)); if (activeLabelId === id) setActiveLabelId(""); }} />}
    {modal === "connection" && <ConnectionDialog onClose={() => setModal(null)} onSave={() => { setModal(null); void refresh(); }} />}
    {pendingNav && <Modal title="Keep your changes safe" onClose={() => setPendingNav(null)} locked={saving}><p className="fw-modal-copy">{drawing ? "There is an unfinished drawing. Finish or cancel it before continuing." : `Save your changes before you ${pendingNav.label}, or keep a recovery copy on this device.`}</p><div className="fw-dialog-actions"><button className="fw-button" onClick={() => setPendingNav(null)}>Stay here</button>{!drawing && !recovery && <><button className="fw-button" onClick={() => { try { if (session && dirty) writeDraft(session.draft, session.draft.revision); const next = pendingNav.run; setPendingNav(null); next(); } catch { setStorageError("This browser cannot keep a recovery copy. Save or download the draft before leaving."); setPendingNav(null); } }}>Keep draft & continue</button><button className="fw-button fw-primary" disabled={saving} onClick={async () => { if (await save()) { const next = pendingNav.run; setPendingNav(null); next(); } }}>{saving ? "Saving…" : "Save & continue"}</button></>}{recovery && <button className="fw-button" onClick={() => { const next = pendingNav.run; setPendingNav(null); next(); }}>Keep recovery copy & continue</button>}</div></Modal>}
  </main>;
}

function Modal({ title, children, onClose, locked = false }: { title: string; children: ReactNode; onClose: () => void; locked?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={ref} className="fw-dialog" onCancel={(event) => { event.preventDefault(); if (!locked) onClose(); }} onClick={(event) => { if (!locked && event.target === event.currentTarget) onClose(); }} aria-label={title}><div className="fw-dialog-content"><div className="fw-dialog-heading"><h2>{title}</h2><button className="fw-icon-button" type="button" aria-label="Close dialog" disabled={locked} onClick={onClose}><X size={19} /></button></div><fieldset className="fw-dialog-fields" disabled={locked}>{children}</fieldset></div></dialog>;
}

function ProjectDialog({ onClose, onCreate }: { onClose: () => void; onCreate: (name: string, description: string) => Promise<void> }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  return <Modal title="Create a project" onClose={onClose} locked={busy}><p className="fw-modal-copy">Keep related images, map areas, and labels together.</p><form onSubmit={async (event) => { event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); try { await onCreate(String(data.get("name")).trim(), String(data.get("description")).trim()); } catch (failure) { setError(apiError(failure)); } finally { setBusy(false); } }}><label className="fw-field">Project name<input name="name" required maxLength={120} placeholder="e.g. Coastal observations" autoFocus /></label><label className="fw-field">Description <span className="fw-optional">optional</span><textarea name="description" rows={3} placeholder="What are you looking for?" /></label>{error && <p className="fw-form-error" role="alert">{error}</p>}<div className="fw-dialog-actions"><button className="fw-button" type="button" onClick={onClose}>Cancel</button><button className="fw-button fw-primary" disabled={busy}>{busy ? "Creating…" : "Create project"}</button></div></form></Modal>;
}

function ItemDialog({ projectId, adapters, onClose, onCreate }: { projectId: string; adapters: AdapterInfo[]; onClose: () => void; onCreate: (item: Item) => Promise<void> }) {
  const options = getAnnotationAdapters().filter((entry) => entry.canCreate !== false && (!adapters.length || adapters.some((available) => available.id === entry.id)));
  const [adapter, setAdapter] = useState(options[0]?.id ?? "image"); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const definition = getAnnotationAdapter(adapter);
  return <Modal title="Add an item" onClose={onClose} locked={busy}><div className="fw-adapter-options" role="group" aria-label="Item type">{options.map((entry) => <button key={entry.id} className={adapter === entry.id ? "is-active" : ""} aria-pressed={adapter === entry.id} onClick={() => setAdapter(entry.id)}><entry.Icon size={23} strokeWidth={1.5} /><strong>{entry.name}</strong><span>{entry.description}</span></button>)}</div><form onSubmit={async (event) => {
    event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); setError("");
    const document: Item = { id: "", projectId, title: String(data.get("title")).trim(), adapter, source: definition?.makeSource(data) ?? {}, metadata: {}, tags: [], status: "unreviewed", notes: "", annotations: [], revision: 0 };
    try { await onCreate(document); } catch (failure) { setError(apiError(failure)); } finally { setBusy(false); }
  }}><label className="fw-field">Item name<input name="title" required maxLength={200} placeholder={adapter === "map" ? "e.g. Harbour survey" : "e.g. Shoreline reference"} /></label>{definition?.fields.map((field) => <label className="fw-field" key={`${adapter}-${field.name}`}>{field.label}<input name={field.name} type={field.type} required={field.required} min={field.min} max={field.max} step={field.type === "number" ? "any" : undefined} defaultValue={field.defaultValue} placeholder={field.placeholder} />{field.help && <span className="fw-field-help">{field.help}</span>}</label>)}{error && <p className="fw-form-error" role="alert">{error}</p>}<div className="fw-dialog-actions"><button className="fw-button" type="button" onClick={onClose}>Cancel</button><button className="fw-button fw-primary" disabled={busy}>{busy ? "Adding…" : "Add item"}</button></div></form></Modal>;
}

function LabelsDialog({ labels, projectId, onClose, onSaved, onDeleted }: { labels: Label[]; projectId: string; onClose: () => void; onSaved: (label: Label) => void; onDeleted: (id: string) => void }) {
  const [editing, setEditing] = useState<Label | null>(null); const [name, setName] = useState(""); const [color, setColor] = useState(LABEL_COLORS[0]); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  return <Modal title="Project labels" onClose={onClose} locked={busy}><p className="fw-modal-copy">Labels are shared by every item in this project.</p><div className="fw-manage-labels">{labels.map((label) => <div key={label.id}><button className="fw-label-edit" onClick={() => { setEditing(label); setName(label.name); setColor(label.color); }}><span className="fw-label-dot" style={{ background: label.color }} /><span>{label.name}</span><small>Edit</small></button><button className="fw-icon-button fw-danger" disabled={busy} aria-label={`Delete ${label.name} label`} onClick={async () => { setBusy(true); setError(""); try { await api.deleteLabel(label); onDeleted(label.id); if (editing?.id === label.id) { setEditing(null); setName(""); } } catch (failure) { setError(apiError(failure)); } finally { setBusy(false); } }}><Trash2 size={15} /></button></div>)}</div><form onSubmit={async (event) => { event.preventDefault(); setBusy(true); setError(""); try { const response = await api.saveLabel({ ...editing, projectId, name: name.trim(), color }); onSaved(response.label); setEditing(null); setName(""); } catch (failure) { setError(apiError(failure)); } finally { setBusy(false); } }}><label className="fw-field">{editing ? "Edit label name" : "New label name"}<input required maxLength={80} placeholder="e.g. Vegetation" value={name} onChange={(event) => setName(event.target.value)} /></label><div className="fw-colors" role="group" aria-label="Label colour">{LABEL_COLORS.map((entry) => <button key={entry} type="button" style={{ background: entry }} aria-label={`Colour ${entry}`} aria-pressed={color === entry} onClick={() => setColor(entry)}>{color === entry && <Check size={17} />}</button>)}<input type="color" aria-label="Custom label colour" value={color} onChange={(event) => setColor(event.target.value)} /></div>{error && <p className="fw-form-error" role="alert">{error}</p>}<div className="fw-dialog-actions">{editing && <button className="fw-button" type="button" onClick={() => { setEditing(null); setName(""); }}>Cancel edit</button>}<button className="fw-button fw-primary" disabled={busy}>{busy ? "Saving…" : editing ? "Save label" : "Create label"}</button></div></form></Modal>;
}

function ConnectionDialog({ onClose, onSave }: { onClose: () => void; onSave: () => void }) {
  const [settings] = useState(() => connectionSettings()); const [error, setError] = useState("");
  return <Modal title="Connection" onClose={onClose}><GoogleSignIn server={settings.url} onConnected={() => { setConnection(settings.url, ""); onSave(); }} onSignedOut={() => { setConnection(settings.url, ""); onSave(); }} /><details className="fw-token-connection"><summary>Advanced · workspace access token</summary><p className="fw-modal-copy">Connect to your annotation service. Leave the server URL blank to use this site. Google sign-in uses the currently connected server.</p><form onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); try { setConnection(String(data.get("url")), String(data.get("token"))); onSave(); } catch { setError("Browser storage is unavailable. Allow session storage and try again."); } }}><label className="fw-field">Server URL<input type="url" name="url" defaultValue={settings.url} placeholder={typeof window === "undefined" ? "Same origin" : window.location.origin} /></label><label className="fw-field">Access token<input type="password" name="token" defaultValue={settings.token} autoComplete="off" placeholder="Only if your server requires one" /><span className="fw-field-help">Kept only for this browser tab session.</span></label>{error && <p role="alert" className="fw-form-error">{error}</p>}<div className="fw-dialog-actions"><button type="button" className="fw-button" onClick={onClose}>Cancel</button><button className="fw-button fw-primary">Connect</button></div></form></details></Modal>;
}

function CoordinateRow({ value, index, image, add, onChange }: { value: Position; index: number; image: boolean; add?: boolean; onChange: (position: Position) => void }) {
  const [x, setX] = useState(String(value[0])); const [y, setY] = useState(String(value[1]));
  const [longitude, latitude] = value;
  // The numeric alternative follows pointer edits while retaining in-progress input text.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setX(String(Number(longitude.toFixed(7)))); setY(String(Number(latitude.toFixed(7)))); }, [longitude, latitude]);
  function submit(event: FormEvent) { event.preventDefault(); onChange([Number(x), Number(y)]); }
  return <form className="fw-coordinate-row" onSubmit={submit}><span>{add ? <MapPin size={14} /> : index + 1}</span><input aria-label={`${add ? "New" : `Vertex ${index + 1}`} ${image ? "X" : "longitude"}`} type="number" step="any" min={image ? 0 : -180} max={image ? 1 : 180} required value={x} onChange={(event) => setX(event.target.value)} /><input aria-label={`${add ? "New" : `Vertex ${index + 1}`} ${image ? "Y" : "latitude"}`} type="number" step="any" min={image ? 0 : -85.0511} max={image ? 1 : 85.0511} required value={y} onChange={(event) => setY(event.target.value)} /><button className="fw-icon-button" aria-label={add ? "Add point at coordinates" : `Apply vertex ${index + 1} coordinates`} title={add ? "Add point" : "Apply coordinates"}>{add ? <Plus size={15} /> : <Check size={15} />}</button></form>;
}
