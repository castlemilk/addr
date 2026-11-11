"use client";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  Check,
  ChevronLeft,
  ChevronRight,
  Layers3,
  Loader2,
  Pencil,
  RotateCcw,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import MetricInput from "@/components/reviews/MetricInput";
import SceneStart from "@/components/scenes/SceneStart";
import { recordRecentScene } from "@/lib/recent-scenes";
import SceneCanvas from "@/components/scenes/SceneCanvas";
import Scene3D from "@/components/scenes/Scene3D";
import SceneInstanceEditor from "@/components/scenes/SceneInstanceEditor";
import SceneGroupEditor from "@/components/scenes/SceneGroupEditor";
import SceneCarControls from "@/components/scenes/SceneCarControls";
import SceneSurfaceControls from "@/components/scenes/SceneSurfaceControls";
import SceneSolarControls from "@/components/scenes/SceneSolarControls";
import RoofLineEditor from "@/components/scenes/RoofLineEditor";
import { reanchorRoofLineEdits, withRoofLineEdits } from "@/lib/roof-line-edits";
import { coerceObjectHints, coerceSceneLotTags, describeObjectHints, hasReviewerRoofEdits, hintStaleNotes, machineLabelName, OBJECT_HINTS_VERSION, STRUCTURE_KINDS, STRUCTURE_LABELS, type Decision, type ObjectHints, type SceneLotAddress, type SceneLotEntry, type StructureSubKind } from "@/lib/queues";
import { mergeObjectHints, outlineEvidenceLine, outlineHints, OUTLINE_HINTS_SOURCE } from "@/lib/outline-hints";
import SceneRoofFeedback from "@/components/scenes/SceneRoofFeedback";
import SceneRoofObservations from "@/components/scenes/SceneRoofObservations";
import SceneRelationships from "@/components/scenes/SceneRelationships";
import SceneVisibilityControls from "@/components/scenes/SceneVisibilityControls";
import SceneIssueControls from "@/components/scenes/SceneIssueControls";
import { hasOutlineIssue, REVIEW_ISSUE_LABELS } from "@/lib/scene-review-issues";
import SceneReviewProgress from "@/components/scenes/SceneReviewProgress";
import SceneAddressControls from "@/components/scenes/SceneAddressControls";
import SceneQueueNavigator from "@/components/scenes/SceneQueueNavigator";
import { filterExclusion, frameFilterAllows, frameFilterValue, holdInView, lotAnchorTitle, lotScopeAllows, lotScopeSummary, lotScopeValue, lotTitle, matchesSceneQueue, resolveLotAnchor, sceneProgress, sceneQueueFilter, sceneSummary, sceneTitle, type FrameFilter, type LotScope, type SceneSummary } from "@/lib/scene-queue";
import { sceneVisibility } from "@/lib/scene-visibility";
import { objectHeightRange, withObjectHeight, withObjectRoofAnnotation } from "@/lib/scene-height";
import { isPavedSurface } from "@/lib/scene-surface";
import RoofAnnotationEditor from "@/components/reviews/RoofAnnotationEditor";
import { annotationFromModel } from "@/lib/roof-annotations";
import { roofMount } from "@/lib/scene-roof-mount";
import { clearConventionalRoofDraft, hasConventionalRoof, isShadeSail, withStructureKind } from "@/lib/scene-structure";
import { carriesVegetationType, VEGETATION_TYPE_LABELS, VEGETATION_TYPES, vegetationFilterAllows, vegetationFilterValue, vegetationTypeCounts, withReviewVegetationType, type VegetationFilter, type VegetationType } from "@/lib/scene-vegetation";
import SceneVegetationType from "@/components/scenes/SceneVegetationType";
import { selectionAfterRestore } from "@/lib/scene-instances";
import {
  CLASS_NAMES,
  CLASS_COLORS,
  SCENE_CLASSES,
  SCENE_REVIEW_ISSUES,
  checkOutlineWithGapRepair,
  droppedGapsNotice,
  footprintMetrics,
  polygonIssues,
  transformPoint,
  type SceneData,
  type SceneObject,
  type SceneReview,
  type SceneKind,
  type ScenePoint,
} from "@/lib/scene-types";
import { simplifyMessage, simplifyOutline } from "@/lib/scene-simplify";
import { getCrmToken } from "@/lib/crm-client";
import "../reviews/reviews.css";
import "./scenes.css";
// The shell is imported after the page CSS so shell.css follows reviews.css and its 52 px header row wins.
import { WorkbenchHeader } from "@/components/shell/WorkbenchHeader";
const surfaceClassHelp: Partial<Record<SceneKind,string>> = {
  road: "Carriageways and junctions carrying through traffic. Trace the visible paved surface.",
  driveway: "Vehicle access lanes, crossovers and parking aprons serving a site.",
  footpath: "Pedestrian routes. Trace the visible walking surface, including shaded pavement.",
};
/**
 * Reports the URL's scene selection to the page. It lives in its own Suspense boundary because
 * useSearchParams bails static prerendering out to the client only up to the nearest boundary.
 * Same-route navigations (hub rows, the Scene review nav link, Back/Forward) do not remount the
 * page, so this is the only way the page learns that ?scene= changed. The handler is read from a
 * ref so the effect fires once per URL, never once per render.
 */
// Editor imagery views. Keys: "original", "roof-detail-v1" (sharp CLAHE display view), "aligned:<stack>:<index>"
// (one registered Wayback date on the saved grid), "composite:<stack>" (pair mean or stack median). All are
// display views: the saved PNG remains the evidence image; selecting one only changes the canvas image href.
type SceneStackSummary = { stack_id: string; status: string; composite: string | null; assets: string[];
  dates: { index: number; date: string | null; sensor: string | null; accepted: boolean }[] };
const ORIGINAL_IMAGERY = "original", DETAIL_IMAGERY = "roof-detail-v1";
const IMAGERY_UNAVAILABLE = "This view is unavailable; showing the original";
function imageryQuery(key: string): string {
  if (key === DETAIL_IMAGERY) return "&view=roof-detail-v1";
  const aligned = /^aligned:([a-f0-9]{16}):(\d{1,2})$/.exec(key);
  if (aligned) return `&view=multidate-aligned&stack=${aligned[1]}&index=${aligned[2]}`;
  const composite = /^composite:([a-f0-9]{16})$/.exec(key);
  if (composite) return `&view=multidate-composite&stack=${composite[1]}`;
  return "";
}
function stackImageryOptions(stacks: SceneStackSummary[]): { value: string; label: string }[] {
  const stack = stacks.find(s => (s.status === "available" || s.status === "pair") && /^[a-f0-9]{16}$/.test(s.stack_id));
  if (!stack) return [];
  const options = stack.dates.filter(d => d.accepted && stack.assets.includes(`aligned-${d.index}.png`))
    .map(d => ({ value: `aligned:${stack.stack_id}:${d.index}`, label: `Aligned ${d.date ?? "undated"} (${d.sensor ?? "unknown sensor"})` }));
  if (stack.composite) options.push({ value: `composite:${stack.stack_id}`, label: stack.composite === "enhanced-pair-rgb.png" ? "Two-view composite" : "Multi-date median composite" });
  return options;
}
function SceneUrlSync({ onChange }: { onChange: (params: URLSearchParams) => void }) {
  const params = useSearchParams();
  const search = params.toString();
  const handler = useRef(onChange);
  useEffect(() => { handler.current = onChange; }, [onChange]);
  useEffect(() => { handler.current(new URLSearchParams(search)); }, [search]);
  return null;
}
const fresh = (data: SceneData): SceneReview =>
  data.review && !data.stale
    ? data.review
    : {
        revision: data.review?.revision || 0,
        model_signature: data.model.model_signature,
        objects: data.model.objects,
        groups: data.model.groups,
        reviewed_classes: [],
      };
/** The recapture banner sentence (recovery method georeferenced-affine): the source scene, the re-projection and its measured
 *  residual from the snapshot's frame_transform (agreement between two frames, never accuracy), and that every carried verdict
 *  returns as needs review on this image. Pinned by scripts/verify-scene-recapture.mjs and scripts/test-scene-recapture-wiring.mjs. */
const recaptureSentence = (data: SceneData): string => {
  const residual = data.model.frame_transform?.residual;
  const px = residual && Array.isArray(residual.shift_px) ? Math.hypot(residual.shift_px[0], residual.shift_px[1]) : NaN;
  const measured = residual && Number.isFinite(px) && Number.isFinite(residual.shift_m) && Number.isFinite(residual.ncc)
    ? `residual ${px.toFixed(1)} px (${residual.shift_m.toFixed(2)} m), NCC ${residual.ncc.toFixed(2)}`
    : "residual not recorded";
  const source = data.recovery?.source_scene_id || data.model.frame_transform?.source_scene_id || "the earlier scene";
  return `Earlier decisions were re-projected through the georeferenced frame from ${source} onto this image (georeferenced affine; ${measured}). ` +
    "Confirmed and rejected decisions come back as needs review on this image: every carried decision returns for a recheck.";
};
/** What the recapture carry left behind or cut, when anything was. */
const recaptureFitSentence = (recovery: NonNullable<SceneData["recovery"]>): string => {
  const dropped = recovery.dropped?.length || 0, clipped = recovery.clipped?.length || 0;
  return [
    dropped ? `${dropped} earlier object${dropped === 1 ? "" : "s"} stayed with the earlier scene (outside this image, or not a reviewer decision).` : "",
    clipped ? `${clipped} carried outline${clipped === 1 ? " was" : "s were"} clipped at this image's edge.` : "",
  ].filter(Boolean).join(" ");
};
/** One-key structure relabels shown under the decision bar; labels are the short forms of STRUCTURE_LABELS. */
const QUICK_KINDS: { key: string; kind: StructureSubKind | ""; label: string }[] = [
  { key: "1", kind: "shed", label: "Shed" },
  { key: "2", kind: "garage", label: "Garage" },
  { key: "3", kind: "carport", label: "Carport" },
  { key: "4", kind: "attached-cover", label: "Cover" },
  { key: "5", kind: "pergola-or-shade", label: "Shade" },
  { key: "6", kind: "shipping-container", label: "Container" },
  { key: "7", kind: "water-tank", label: "Tank" },
  { key: "8", kind: "pool-cover", label: "Pool cover" },
  { key: "9", kind: "fragment", label: "Fragment" },
  { key: "0", kind: "", label: "No sub-kind" },
];
/** Object-hint codes whose sentence pre-highlights the existing "Not a roof" button (a suggestion; the reviewer still presses it). */
const REJECT_SUGGESTING_CODES = ["pool_not_blue", "building_blends_into_surroundings", "degenerate_outline", "canopy_fragment_inside"];
/** Plain words for one hint's evidence numbers: "vertices 54, off axis edges 49, wonky share 0.91" (the rule text stays in the tooltip). */
/** A stable empty lots map so the queue memo does not recompute while the lots of a scene are loading or unavailable. */
const EMPTY_LOTS: Record<string, SceneLotEntry> = {};
const evidenceWords = (evidence: Record<string, unknown>) => Object.entries(evidence).filter(([key, value]) => key !== "rule" && (typeof value === "number" || typeof value === "string" || typeof value === "boolean"))
  .map(([key, value]) => `${key.replaceAll("_", " ")} ${typeof value === "number" ? (Number.isInteger(value) ? value : value.toFixed(2)) : String(value)}`).join(", ");

export default function SceneReviewPage() {
  const [scenes, setScenes] = useState<SceneSummary[]>([]),
    [id, setId] = useState(""),
    [data, setData] = useState<SceneData | null>(null),
    [draft, setDraft] = useState<SceneReview | null>(null);
  const [selected, setSelected] = useState<string | null>(null),
    [query, setQuery] = useState(""),
    [kind, setKind] = useState("all"),
    [status, setStatus] = useState("candidate"),
    [issue, setIssue] = useState("all"),
    // Client-side only: frame-cut objects (touchesFrame) leave the queue under "skip"; the server's summaries never see it.
    [frameFilter, setFrameFilter] = useState<FrameFilter>("skip"),
    // Client-side only: objects whose dominant lot (lot tags from the eyes index) lies on other land leave the queue under "land".
    [lotScope, setLotScope] = useState<LotScope>("land"),
    // Client-side only and not in the URL: tree and vegetation outlines by stored vegetation type (scene-vegetation.ts); "all" filters nothing.
    [vegetationFilter, setVegetationFilter] = useState<VegetationFilter>("all"),
    [view, setView] = useState("aerial");
  const [layer, setLayer] = useState<"model" | "edited" | "overlay">("edited"),
    [mode, setMode] = useState<"review" | "edit" | "draw">("review"),
    [newKind, setNewKind] = useState<SceneKind>("tree"),
    [visible, setVisible] = useState(() => new Set<string>(SCENE_CLASSES));
  const [isolateLayer, setIsolateLayer] = useState(true);
  // Imagery views are display-only: they swap the canvas image href and nothing else. Outlines, roof lines and
  // saves stay in saved-image pixel coordinates on the original PNG (the sole evidence image).
  const [imagery, setImagery] = useState<{source:string; key:string}|null>(null);
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState("");
  const [stacks, setStacks] = useState<{source:string; stacks:SceneStackSummary[]}>({source:"", stacks:[]});
  const detailRequest = useRef(0);
  const imageSource = data?.image_url ?? "";
  const imageryKey = imageSource && imagery?.source === imageSource ? imagery.key : ORIGINAL_IMAGERY;
  const detailEnabled = imageryKey === DETAIL_IMAGERY;
  const reviewImage = data ? `${imageSource}${imageryQuery(imageryKey)}` : "";
  const imageryOptions = useMemo(() => stackImageryOptions(imageSource && stacks.source === imageSource ? stacks.stacks : []), [imageSource, stacks]);
  useEffect(() => {
    if (!imageSource) return;
    const source = imageSource, controller = new AbortController();
    fetch(`/api/scenes/stacks?scene=${encodeURIComponent(id)}`, { signal: controller.signal, cache: "no-store" })
      .then(response => response.ok ? response.json() : { stacks: [] })
      .then(body => setStacks({ source, stacks: Array.isArray(body?.stacks) ? body.stacks : [] }))
      .catch(() => { if (!controller.signal.aborted) setStacks({ source, stacks: [] }); });
    return () => controller.abort();
  }, [imageSource, id]);
  const selectImagery = async (key: string) => {
    if (!data || detailLoading) return;
    const request = ++detailRequest.current, source = data.image_url;
    if (key === ORIGINAL_IMAGERY) { setImagery(null); setDetailError(""); return; }
    setDetailLoading(true); setDetailError("");
    try {
      const response = await fetch(`${source}${imageryQuery(key)}`);
      if (!response.ok) throw new Error(IMAGERY_UNAVAILABLE);
      await response.arrayBuffer();
      if (detailRequest.current === request) setImagery({ source, key });
    } catch {
      if (detailRequest.current === request) { setImagery(null); setDetailError(IMAGERY_UNAVAILABLE); }
    } finally {
      if (detailRequest.current === request) setDetailLoading(false);
    }
  };
  // Shortcut kept for the roof editors and verify scripts: toggles between the original and the contrast view.
  const toggleDetail = () => selectImagery(detailEnabled ? ORIGINAL_IMAGERY : DETAIL_IMAGERY);
  // Warm the server's contrast-view cache as soon as a scene loads, so Enhance roof detail answers at once.
  useEffect(() => {
    if (!imageSource) return;
    const controller = new AbortController();
    fetch(`${imageSource}${imageryQuery(DETAIL_IMAGERY)}`, { signal: controller.signal, priority: "low" } as RequestInit).catch(() => {});
    return () => controller.abort();
  }, [imageSource]);
  const [loading, setLoading] = useState(true),
    [listLoading, setListLoading] = useState(true),
    [listError, setListError] = useState(""),
    [urlRestore, setUrlRestore] = useState(0),
    [working, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [reload, setReload] = useState(0),
    [undo, setUndo] = useState<SceneReview[]>([]),
    [lastDecision, setLastDecision] = useState<SceneReview | null>(null);
  const [extractId, setExtractId] = useState("29790"),
    [prompt, setPrompt] = useState(""),
    [extracting, setExtracting] = useState(false),
    [apexMode, setApexMode] = useState(false);
  const [roofEditorMode,setRoofEditorMode]=useState<"lines"|"apexes">("apexes");
  const [roofLinePending,setRoofLinePending]=useState(false);
  const [roofForm, setRoofForm] = useState("auto"), [roofRise, setRoofRise] = useState(2), [roofPending, setRoofPending] = useState(false);
  const [instanceMode,setInstanceMode]=useState(false),[instancePending,setInstancePending]=useState(false);
  const [boundaryDrawing,setBoundaryDrawing]=useState(false);
  const [instanceDrawing,setInstanceDrawing]=useState(false);
  const outlineDrawing=boundaryDrawing||instanceDrawing;
  const [heightEditing,setHeightEditing]=useState(false);
  const editingGeometry=outlineDrawing||heightEditing||roofLinePending;
  const [groupMode,setGroupMode]=useState(false),[selectedGroup,setSelectedGroup]=useState<string|null>(null),[returnGroup,setReturnGroup]=useState<string|null>(null);
  const busy=working||editingGeometry;
  const [relatedReturn,setRelatedReturn]=useState<{sceneId:string;objectId:string;kind:string;status:string;query:string;issue:string;layer:typeof layer;view:string;frameFilter:FrameFilter;lotScope:LotScope}|null>(null);
  const onInstancePending=useCallback((pending:boolean)=>{setBusy(pending);setInstancePending(pending);if(pending){setMessage("");setError("");}},[]);
  const roofRequest = useRef<AbortController | null>(null);
  const apexSection = useRef<HTMLElement | null>(null);
  const reviewCard = useRef<HTMLElement | null>(null), relatedFocus = useRef<string | null>(null);
  useEffect(() => {
    if (apexMode && !roofPending) {
      if(roofEditorMode==="lines")window.scrollTo({top:0});
      else apexSection.current?.scrollIntoView({ block: "start" });
    }
  }, [apexMode, roofPending, roofEditorMode]);
  const [skipped, setSkipped] = useState<Set<string>>(() => new Set());
  const [mobileLayersOpen, setMobileLayersOpen] = useState(false);
  const [addressCheck,setAddressCheck]=useState<{scene:string;address:number}|null>(null);
  const [showEmptyLayers,setShowEmptyLayers]=useState(false);
  const search = useRef<HTMLInputElement>(null),
    saving = useRef(false);
  const requestedSelection=useRef<{sceneId:string;objectId:string|null}|null>(null);
  // The object the reviewer asked for by name (URL object=, canvas click), with the filter key in force when it was asked for. While
  // the frame filter or the lot scope excludes it and nothing holds it in view, the card shows the first queue member (and `selected`
  // converges on it, see `effective`) and a notice names it. Cleared when the reviewer moves on (a decision or rejection that advances,
  // a save that moves the selection, Previous / Next / Skip, a related, grouped or replacement open, Return to <object>); a filter change
  // retires it through its key. The render-time convergence below never clears it (that render is the notice), and the page never
  // changes a filter on its behalf.
  const [requested,setRequested]=useState<{id:string;filters:string}|null>(null);
  const filterKeyOf=(...parts:string[])=>parts.join("\u0000");
  const filterKey=filterKeyOf(kind,status,query,issue,frameFilter,lotScope);
  // The load effect reads the key when its fetch settles (after the URL effect applied the URL's filters), not from its closure.
  const filterKeyRef=useRef(filterKey);
  useEffect(()=>{filterKeyRef.current=filterKey;},[filterKey]);
  // The scene param this page last applied from the URL (null until the mount effect has read it),
  // so SceneUrlSync acts only on later changes and ignores the page's own replaceState writes.
  const urlScene = useRef<string | null>(null);
  const dirty = Boolean(
    draft && data && JSON.stringify(draft) !== JSON.stringify(fresh(data)),
  );
  // No scene chosen: the main column is the hub (SceneStart) and the rail stays usable.
  const hub = !id && !loading;
  // While a requested scene loads (or fails), the h1 names it from the list or its id.
  const listedScene = scenes.find((scene) => scene.id === id);
  const requestedTitle = listedScene ? sceneTitle(listedScene) : id || "Scene";
  const loadList = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch("/api/scenes", { signal });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    setScenes(result.scenes);
    return result.scenes as SceneSummary[];
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams(window.location.search);
    const filter=sceneQueueFilter({kind:params.get("kind"),status:params.get("status"),query:params.get("q"),issue:params.get("issue")});
    const requestedScene=params.get("scene");
    const requestedLayer=params.get("layer");
    if(requestedLayer==="model"||requestedLayer==="edited"||requestedLayer==="overlay")setLayer(requestedLayer);
    const requestedAddress=Number(params.get("address"));
    if(requestedScene&&Number.isSafeInteger(requestedAddress)&&requestedAddress>0)setAddressCheck({scene:requestedScene,address:requestedAddress});
    setQuery(params.get("q")?.slice(0,200) || "");
    setKind(filter.kind);
    setStatus(filter.status);
    setIssue(filter.issue);
    setFrameFilter(frameFilterValue(params.get("frame")));
    setLotScope(lotScopeValue(params.get("lots")));
    if(requestedScene){requestedSelection.current={sceneId:requestedScene,objectId:params.get("object")};setId(requestedScene);}
    // Without a scene in the URL the page is the hub: list saved scenes and let the
    // reviewer choose. Nothing is auto-selected, so nothing loads imagery unasked.
    else setLoading(false);
    urlScene.current = requestedScene || "";
    setListLoading(true);
    setListError("");
    loadList(controller.signal)
      .catch((e) => {
        if (!controller.signal.aborted) { setError(e.message); setListError(e.message); }
      })
      .finally(() => {
        if (!controller.signal.aborted) setListLoading(false);
      });
    return () => controller.abort();
  }, [loadList]);
  const retryList = () => {
    setError("");
    setListError("");
    setListLoading(true);
    loadList().catch((e) => { setError(e.message); setListError(e.message); }).finally(() => setListLoading(false));
  };
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    const params=new URLSearchParams(window.location.search);
    const requestedObject=requestedSelection.current?.sceneId===id?requestedSelection.current.objectId:
      params.get("scene")===id?params.get("object"):null;
    const requestedAddress = id.match(/-([0-9]+)$/)?.[1];
    if (requestedAddress) setExtractId(requestedAddress);
    setLoading(true);
    setError("");
    setData(null);
    setDraft(null);
    fetch(`/api/scenes?id=${encodeURIComponent(id)}`, {
      signal: controller.signal,
    })
      .then(async (r) => {
        const result = await r.json();
        if (!r.ok) throw new Error(result.error);
        return result as SceneData;
      })
      .then((result) => {
        if(controller.signal.aborted)return;
        setData(result);
        setDraft(fresh(result));
        recordRecentScene(result.model.id, sceneTitle(sceneSummary(result.model, fresh(result).objects, undefined, result.stale, result.address_names)));
        setSelected(requestedObject || result.model.objects[0]?.id || null);
        setRequested(requestedObject?{id:requestedObject,filters:filterKeyRef.current}:null);
        if(requestedSelection.current?.sceneId===id)requestedSelection.current=null;
        setUndo([]);
        setSkipped(new Set());
        setLastDecision(null);
        setExtractId(String(result.model.address_id));
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [id, reload]);
  useEffect(() => {
    if (!dirty&&!editingGeometry) return;
    const handler = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty,editingGeometry]);
  // The queue before the frame filter. A frame-cut object the reviewer opened (URL, canvas click) is held in view from this list
  // under Skip frame-cut, as an edited object that left the queue is: in view, but not in the queue or the advance order.
  const unframed = useMemo(
    () =>
      draft?.objects.filter(
        (o) =>
          matchesSceneQueue(o,sceneQueueFilter({kind,status,query,issue})) &&
          vegetationFilterAllows(o, vegetationFilter) &&
          (status !== "candidate" || !skipped.has(o.id)),
      ) || [],
    [draft, kind, status, query, issue, skipped, vegetationFilter],
  );
  // The scene's lot tags (lot-tags-v1 from the eyes index, GET /api/scenes/lots) behind the lot scope: one request per scene and
  // image version, read-only and silent. A failed request or an unbuilt index reads as unavailable, which leaves the scope inert
  // (no anchor: nothing hidden, the select disabled). Hints, never labels: the rows only filter the queue and the advance order.
  const [sceneLots, setSceneLots] = useState<{ sceneId: string; signature: string; address: SceneLotAddress | null; objects: Record<string, SceneLotEntry> } | null>(null);
  const modelSignature = data?.model.model_signature ?? "";
  useEffect(() => {
    if (!id || !modelSignature || data?.model.id !== id) return;
    const controller = new AbortController();
    fetch(`/api/scenes/lots?scene=${encodeURIComponent(id)}&signature=${encodeURIComponent(modelSignature)}`, { signal: controller.signal, cache: "no-store" })
      .then(response => (response.ok ? response.json() : null))
      .then(document => {
        if (controller.signal.aborted) return;
        const lots = coerceSceneLotTags(document);
        setSceneLots({ sceneId: id, signature: modelSignature, address: lots?.address ?? null, objects: lots?.objects ?? EMPTY_LOTS });
      })
      .catch(() => { if (!controller.signal.aborted) setSceneLots({ sceneId: id, signature: modelSignature, address: null, objects: EMPTY_LOTS }); });
    return () => controller.abort();
  }, [id, modelSignature, data?.model.id]);
  const loadedLots = sceneLots && sceneLots.sceneId === id && sceneLots.signature === modelSignature ? sceneLots : null;
  const sceneLotRows = loadedLots?.objects ?? EMPTY_LOTS;
  // The lot anchor the scope compares against: the scene row's lot and plan ("address"), else the planned window lot ("planned"), else on
  // a window scene the land of the roof it was cut for ("asked": the dominant lots of the whole-roof takeovers and carried slices in the
  // review layer, so the draft's objects are read); null leaves the scope inert. A hint, never a label.
  const lotAnchor = useMemo(() => (loadedLots ? resolveLotAnchor(loadedLots.address, data?.model, draft?.objects ?? data?.model.objects, sceneLotRows) : null), [loadedLots, data?.model, draft?.objects, sceneLotRows]);
  const queue = useMemo(() => unframed.filter((o) => frameFilterAllows(o, data?.model, frameFilter) && lotScopeAllows(sceneLotRows[o.id], lotAnchor, lotScope)), [unframed, data?.model, frameFilter, sceneLotRows, lotAnchor, lotScope]);
  // What the lot scope hides beyond the frame filter, for the note beside the filters; unknown objects are never hidden.
  const lotSummary = useMemo(() => lotScopeSummary(unframed.filter((o) => frameFilterAllows(o, data?.model, frameFilter)).map((o) => sceneLotRows[o.id]), lotAnchor, lotScope), [unframed, data?.model, frameFilter, sceneLotRows, lotAnchor, lotScope]);
  // "Lot 3 PS631347: 4 objects on other land hidden"; an asked anchor reads "Asked roofs: Lot 2 LP27328 and Lot 1 PS530708: ...".
  const lotNote = lotAnchor && lotSummary.hidden > 0
    ? `${lotAnchorTitle(lotAnchor)}: ${lotSummary.hidden} ${lotSummary.hidden === 1 ? "object" : "objects"} on other ${lotScope === "lot" ? "lots" : "land"} hidden`
    : null;
  const current = draft?.objects.find((o) => o.id === selected) || null;
  // Which client-side filter excludes the selected object (frame first, then the lot scope); null when neither does.
  const exclusion = current ? filterExclusion(current, data?.model, frameFilter, sceneLotRows[current.id], lotAnchor, lotScope) : null;
  // The selected object is held in view with unsaved edits, an open geometry edit or in edit mode; otherwise only while it is in the
  // queue: the status/kind/issue/search filter still matches it and neither the frame filter nor the lot scope excludes it. An object
  // opened by URL or canvas click that a filter hides is therefore not shown "anyway": the card falls to the first queue member and
  // the notice below names it (holdInView, filterExclusion).
  const excluded = !!current && (exclusion !== null || !unframed.some((o) => o.id === current.id));
  const effective = current && holdInView({ excluded, dirty, editingGeometry: editingGeometry || mode === "edit" }) ? current : queue[0] || null;
  const selectedId = effective?.id || null;
  // The selection converges on the shown object in the same render (React's adjust-state-while-rendering pattern; the condition is
  // false again once `current` is the shown object, so it runs at most once). Otherwise `selected` would keep pointing at the hidden
  // object and the page-wide dirty/geometry hold above would swap the card to it on the next bare change() with no setSelected (a
  // Layer review coverage checkbox, Reset object to prediction, Edit roof lines, 3D height editing): the reviewer would be looking at,
  // and deciding on, an object a filter hides. The requested id for the notice is kept separately in `requested`.
  if (current && effective?.id !== current.id) setSelected(effective?.id ?? null);
  // The requested object while the frame filter or the lot scope hides it: the notice above the card and its Show it anyway button.
  // Looked up by id, not through `current`, since the selection has converged on the shown object.
  const requestedTarget = requested && requested.filters === filterKey ? draft?.objects.find((o) => o.id === requested.id) ?? null : null;
  const requestedHidden = requestedTarget && requestedTarget.id !== effective?.id &&
    filterExclusion(requestedTarget, data?.model, frameFilter, sceneLotRows[requestedTarget.id], lotAnchor, lotScope) !== null ? requestedTarget : null;
  // The id alone, for the URL effect: the object's identity changes with every draft, its id does not.
  const requestedHiddenId = requestedHidden?.id ?? null;
  const hiddenByFrame = !!requestedHidden && !frameFilterAllows(requestedHidden, data?.model, frameFilter);
  const hiddenByLot = !!requestedHidden && !lotScopeAllows(sceneLotRows[requestedHidden.id], lotAnchor, lotScope);
  const hiddenLot = requestedHidden ? sceneLotRows[requestedHidden.id]?.dominant ?? null : null;
  const hiddenReasons = requestedHidden ? [
    ...(hiddenByFrame ? [frameFilter === "only" ? "it is clear of the image edge (frame-cut only)" : "it touches the image edge (frame cut)"] : []),
    ...(hiddenByLot ? [`it is on other land (${hiddenLot?.plan ? lotTitle({ lot: hiddenLot.lot, plan: hiddenLot.plan }) : "lot unknown"})`] : []),
  ] : [];
  // Index of the inspected object in the queue; -1 while an edited or frame-cut object has left the filtered queue but stays in view.
  const position = queue.findIndex((o) => o.id === selectedId);
  // Agent hints for the inspected object (class hint, structure kind and the decision policy's machine label from the
  // eyes index). Read-only and silent: a missing row, an unbuilt index or a failed request all render nothing. The
  // decision is kept so "Use machine label" can apply it through changeObject; the reviewer still saves. Debounced
  // and aborted on change.
  const [agentHints, setAgentHints] = useState<{ sceneId: string; objectId: string; hints: ObjectHints; decision: Decision | null } | null>(null);
  useEffect(() => {
    // No synchronous reset: the render guards on scene/object identity, so a stale line never shows for another object.
    if (!id || !selectedId) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      // The model signature lets the index prefer rows computed on this image version and say when it could not (stale).
      fetch(`/api/scenes/hints?scene=${encodeURIComponent(id)}&object=${encodeURIComponent(selectedId)}${modelSignature ? `&signature=${encodeURIComponent(modelSignature)}` : ""}`, { signal: controller.signal, cache: "no-store" })
        .then(response => (response.ok ? response.json() : null))
        .then(document => {
          if (controller.signal.aborted) return;
          const hints = document ? coerceObjectHints(document) : null;
          setAgentHints(hints ? { sceneId: id, objectId: selectedId, hints, decision: hints.decision } : null);
        })
        .catch(() => { if (!controller.signal.aborted) setAgentHints(null); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [id, selectedId, modelSignature]);
  const currentSummary=data&&draft?sceneSummary(data.model,draft.objects,undefined,data.stale,data.address_names):null;
  const selectedAddress=effective?.address_id?data?.address_names?.[effective.address_id]:undefined;
  // Scope by identity so a reclassified object's original prediction remains
  // available for comparison. Keep all objects available for roof support.
  const reviewObjectIds = useMemo(() => {
    if (!isolateLayer || kind === "all") return undefined;
    const ids = new Set((draft?.objects || []).filter(o => o.kind === kind).map(o => o.id));
    if (selectedId) ids.add(selectedId);
    return ids;
  }, [isolateLayer, kind, draft?.objects, selectedId]);
  // What the canvas draws and lets the reviewer click: the layer scope above minus the objects the frame filter or the lot scope hides,
  // the inspected object always included (it may be held in view by edits). The canvas still receives the full object list, which its
  // roof-mount and lot sentence read for context; SceneRelationships and the group panels keep the full list too.
  const canvasObjectIds = useMemo(() => {
    if (!draft) return reviewObjectIds;
    const hidden = draft.objects.filter((o) => o.id !== selectedId && filterExclusion(o, data?.model, frameFilter, sceneLotRows[o.id], lotAnchor, lotScope) !== null);
    if (!hidden.length) return reviewObjectIds;
    const ids = new Set(reviewObjectIds ?? draft.objects.map((o) => o.id));
    for (const o of hidden) ids.delete(o.id);
    return ids;
  }, [draft, reviewObjectIds, selectedId, data?.model, frameFilter, sceneLotRows, lotAnchor, lotScope]);
  const selectCanvasObject = (objectId: string) => {
    const nextKind = kind !== "all" && draft?.objects.find(o => o.id === objectId)?.kind !== kind ? "all" : kind;
    setSelected(objectId);setStatus("all");setIssue("all");
    if (nextKind !== kind) setKind(nextKind);
    const clicked = draft?.objects.find(o => o.id === objectId);
    if (clicked && !vegetationFilterAllows(clicked, vegetationFilter)) setVegetationFilter("all");
    // Recorded under the filters this click leaves in force, so the notice (3D view clicks can still reach a hidden object) shows.
    setRequested({id:objectId,filters:filterKeyOf(nextKind,"all",query,"all",frameFilter,lotScope)});
  };
  useEffect(() => {
    if(groupMode||relatedFocus.current!==selectedId||!selectedId)return;
    relatedFocus.current=null;
    reviewCard.current?.focus({preventScroll:true});
    reviewCard.current?.scrollIntoView({block:"start"});
  },[selectedId,groupMode]);
  useEffect(() => {
    if (!id || loading || data?.model.id!==id) return;
    const params = new URLSearchParams({ scene: id });
    if (query) params.set("q", query);
    if (kind !== "all") params.set("kind", kind);
    if (status !== "candidate") params.set("status", status);
    if (issue !== "all") params.set("issue", issue);
    if (frameFilter !== "skip") params.set("frame", frameFilter);
    if (lotScope !== "land") params.set("lots", lotScope);
    // While the asked-for object is hidden behind the notice the URL keeps naming it, so a reload (or a shared link) reproduces the
    // notice instead of landing on the shown object; once the reviewer moves on (`requested` cleared) it tracks the shown object again.
    const urlObject = requestedHiddenId ?? selectedId;
    if (urlObject) params.set("object", urlObject);
    params.set("layer",layer);
    if(addressCheck?.scene===id)params.set("address",String(addressCheck.address));
    window.history.replaceState(null, "", `/scenes?${params}`);
  }, [id, query, kind, status, issue, frameFilter, lotScope, selectedId, requestedHiddenId, loading, data?.model.id, addressCheck, layer, urlRestore]);
  const progress=useMemo(()=>sceneProgress(draft?.objects||[]),[draft]);
  const vegetationCounts=useMemo(()=>vegetationTypeCounts(draft?.objects||[]),[draft]);
  const layerProgress=useMemo(()=>Object.fromEntries(SCENE_CLASSES.map(k=>[k,sceneProgress(draft?.objects.filter(o=>o.kind===k)||[])])),[draft]);
  const navigationReason=instanceDrawing?"Finish or cancel the replacement trace before changing images.":boundaryDrawing?"Finish or cancel the interior gap before changing images.":dirty?"Save or discard edits before changing images.":groupMode?"Return to object review before changing images.":instanceMode?"Finish or close the outline operation before changing images.":
    busy||extracting?"Finish the current operation before changing images.":loading?"Loading this image…":null;
  const navigateScene=(sceneId:string,objectId:string|null=null)=>{
    if(navigationReason)return;
    requestedSelection.current={sceneId,objectId};setId(sceneId);setSelected(null);setMessage("");
    setGroupMode(false);setSelectedGroup(null);setReturnGroup(null);
    setMode("review");setView("aerial");setLayer("edited");setApexMode(false);setInstanceMode(false);setMobileLayersOpen(false);
  };
  const originalObject = data?.model.objects.find(o=>o.id===selectedId);
  const outlineDiffers=effective?.kind==="building"&&originalObject?.kind==="building"&&
    JSON.stringify([effective.ring,effective.holes||[]])!==JSON.stringify([originalObject.ring,originalObject.holes||[]]);
  // The inspected object's outline differs from what is saved (fresh(data) is the saved review), or the object is not saved
  // yet (drawn/traced). Independent of mode: after Finish editing or Escape the bar must still say that a click stores the
  // reviewer's outline, not the model estimate.
  const savedObject = data ? fresh(data).objects.find(o => o.id === selectedId) : undefined;
  const outlinePending = !!effective && !["superseded", "rejected"].includes(effective.status) &&
    (!savedObject || JSON.stringify([effective.ring, effective.holes || []]) !== JSON.stringify([savedObject.ring, savedObject.holes || []]));
  const toggleEdit = useCallback(() => {
    if (busy || !effective || effective.status === "superseded") return;
    setSelected(selectedId); setInstanceMode(false);
    // Leaving edit mode after a Save scene: a now needs_adjustment object would drop out of a candidate queue and the card
    // would jump to another object. Widen only in that case; while a draft is pending `effective` keeps it anyway.
    if (mode === "edit" && !dirty && !matchesSceneQueue(effective, sceneQueueFilter({ kind, status, query, issue }))) setStatus("all");
    setMode(mode === "edit" ? "review" : "edit"); setLayer("edited"); setView("aerial");
  }, [busy, effective, dirty, mode, selectedId, kind, status, query, issue]);
  // From the line workspace into the outline editor. Enters edit mode only (no filter widening): every line commit already
  // reached the draft through changeObject, `dirty` stays true and `effective` keeps the object in view.
  const editOutlineFromLines = useCallback(() => {
    if (busy || !effective || effective.status === "superseded") return;
    setSelected(selectedId); setInstanceMode(false); setApexMode(false);
    setMode("edit"); setLayer("edited"); setView("aerial");
  }, [busy, effective, selectedId]);
  const predictionLabel = originalObject
    ? `Model: ${originalObject.kind.replaceAll("_"," ")}${typeof originalObject.score === "number" ? ` · ${Math.round(originalObject.score*100)}%` : ""}`
    : effective ? "Your annotation" : "";
  useEffect(() => () => roofRequest.current?.abort(), [id, selectedId]);
  const inspected = layer === "model" ? data?.model.objects.find(o => o.id === selectedId) || null : effective;
  // The machine label offered for the inspected object, only while the hints belong to this scene and object.
  const machineDecision = inspected && agentHints && agentHints.sceneId === id && agentHints.objectId === inspected.id ? agentHints.decision : null;
  // Served hints (eyes index rows) for the inspected object, plus the outline hints the editor measures itself from the stored ring
  // (degenerate, tiny, noisy: the same object-hints rules, unchanged since v1, so they show before the index has a row). Served codes win; the
  // editor fills in the rest. Everything here is read-only text: nothing below changes a kind, a status or a ring.
  const servedHints = inspected && agentHints && agentHints.sceneId === id && agentHints.objectId === inspected.id ? agentHints.hints : null;
  const localOutlineHints = useMemo(() => (inspected && data ? outlineHints(inspected, data.model) : []), [inspected, data]);
  const objectHintList = useMemo(() => mergeObjectHints(servedHints?.object_hints?.hints, localOutlineHints), [servedHints, localOutlineHints]);
  const agentHintsText = inspected ? describeObjectHints({ class_hints: servedHints?.class_hints ?? null, structure: servedHints?.structure ?? null, decision: servedHints?.decision ?? null,
    object_hints: objectHintList.length || servedHints?.object_hints ? { hints: objectHintList, squared_candidate: servedHints?.object_hints?.squared_candidate ?? null } : null, lot: servedHints?.lot ?? null }) : null;
  // One notice per stale reason (an earlier image version; object hints from older rules where those rules differ for this kind);
  // the served version itself is named in the Why-flagged footer whenever it is older.
  const staleNotes = servedHints && inspected ? hintStaleNotes(servedHints, inspected.kind) : [];
  const hintsStale = staleNotes.length > 0;
  const olderObjectHints = servedHints?.object_hints_served_version && servedHints.object_hints_served_version !== OBJECT_HINTS_VERSION ? servedHints.object_hints_served_version : null;
  const outlineEvidence = inspected && data ? outlineEvidenceLine(inspected, data.model) : null;
  const rejectSuggestion = objectHintList.find(h => REJECT_SUGGESTING_CODES.includes(h.code)) ?? null;
  const secondarySuggestion = objectHintList.find(h => h.code === "secondary_by_lot") ?? null;
  // The quick-kind chip a secondary_by_lot hint points at: the structure hint's own band when it has one, else the size word in the sentence.
  const suggestedKind: StructureSubKind | null = secondarySuggestion
    ? (servedHints?.structure && QUICK_KINDS.some(q => q.kind === servedHints.structure?.sub_kind) ? servedHints.structure.sub_kind
      : /garage/i.test(secondarySuggestion.sentence) ? "garage" : /shed/i.test(secondarySuggestion.sentence) ? "shed" : null)
    : null;
  const lineWorkspaceOpen=!groupMode&&apexMode&&roofEditorMode==="lines"&&!!effective&&hasConventionalRoof(effective)&&effective.status!=="superseded";
  const heightRange = inspected ? objectHeightRange(inspected) : { min: 0, max: 150 };
  const rooftopObject = inspected && ["solar_panel", "skylight"].includes(inspected.kind);
  const pavedObject = inspected && isPavedSurface(inspected.kind);
  const hasThickness = rooftopObject || pavedObject;
  const visibleExtent = inspected && data ? sceneVisibility(inspected,data.model) : null;
  const placement = useMemo(() => {
    if (!inspected || !data) return null;
    const objects = layer === "model" ? data.model.objects : draft?.objects || [];
    return roofMount(inspected, objects, data.model.pixel_to_meters);
  }, [inspected, data, draft, layer]);
  const followsRoof = placement && placement.mode !== "absolute";
  const inspectRelated = (object:SceneObject) => {
    if(busy||instanceMode||roofPending||!selectedId)return;
    setRelatedReturn(old=>old?.sceneId===id&&old.objectId!==selectedId?old:{sceneId:id,objectId:selectedId,kind,status,query,issue,layer,view,frameFilter,lotScope});
    relatedFocus.current=object.id;
    setSelected(object.id);setRequested(null);setKind("all");setStatus(object.status==="superseded"?"superseded":"all");setQuery("");setIssue("all");setFrameFilter("all");setLotScope("all");setVegetationFilter("all");
    setVisible(old=>new Set([...old,object.kind]));setMode("review");setApexMode(false);
  };
  const openGroups=(groupId?:string)=>{
    if(busy)return;
    if(groupId)setSelectedGroup(groupId);
    setGroupMode(true);setInstanceMode(false);setApexMode(false);setMode("review");
  };
  const inspectGrouped=(object:SceneObject,groupId:string)=>{
    if(busy)return;
    setGroupMode(false);setReturnGroup(groupId||null);setRelatedReturn(null);
    relatedFocus.current=object.id;setSelected(object.id);setRequested(null);setKind("all");setStatus("all");setQuery("");setIssue("all");setFrameFilter("all");setLotScope("all");setVegetationFilter("all");
    setView("aerial");setMode("review");setVisible(old=>new Set([...old,object.kind]));
  };
  const returnToObject = () => {
    if(!relatedReturn||relatedReturn.sceneId!==id||busy||instanceMode||roofPending)return;
    const object=draft?.objects.find(o=>o.id===relatedReturn.objectId);
    if(!object)return;
    relatedFocus.current=object.id;
    const matches=matchesSceneQueue(object,sceneQueueFilter(relatedReturn));
    // The frame filter and the lot scope the related open widened come back only when the returned object passes them; otherwise that
    // one stays at all, so the return never lands behind the hidden-object notice.
    const frame=frameFilterAllows(object,data?.model,relatedReturn.frameFilter)?relatedReturn.frameFilter:"all";
    const lots=lotScopeAllows(sceneLotRows[object.id],lotAnchor,relatedReturn.lotScope)?relatedReturn.lotScope:"all";
    setSelected(object.id);setRequested(null);setKind(matches?relatedReturn.kind:"all");setStatus(matches?relatedReturn.status:object.status==="superseded"?"superseded":"all");
    setQuery(matches?relatedReturn.query:"");setIssue(matches?relatedReturn.issue:"all");setFrameFilter(frame);setLotScope(lots);setLayer(relatedReturn.layer);setView(relatedReturn.view);
    setMode("review");setApexMode(false);setVisible(old=>new Set([...old,object.kind]));setRelatedReturn(null);
  };
  const change = (next: SceneReview) => {
    if (!draft) return;
    setUndo((old) => [...old.slice(-39), draft]);
    setDraft(next);
    setMessage("");
  };
  const restoreSelection = (next: SceneReview) => {
    const target=selectionAfterRestore(selectedId,draft?.objects||[],next.objects);
    setSelected(target);setRequested(null);setInstanceMode(false);
    const restored=next.objects.find(o=>o.id===target);
    // Widen the queue filter only when the restored object would otherwise leave view. While editing, or while the restored
    // draft still differs from the saved review, `effective` keeps it, so an Undo during an outline edit does not switch the
    // reviewer's queue to All objects.
    const staysInView=mode==="edit"||(!!data&&JSON.stringify(next)!==JSON.stringify(fresh(data)));
    if(restored&&!staysInView&&(!matchesSceneQueue(restored,sceneQueueFilter({kind,status,query,issue}))||!vegetationFilterAllows(restored,vegetationFilter))){setStatus("all");setQuery("");setKind("all");setIssue("all");setVegetationFilter("all");}
  };
  const changeObject = (object: SceneObject) => {
    if (!draft || !data || object.status === "superseded") return;
    if (isShadeSail(object)) object = clearConventionalRoofDraft(object);
    setSelected(object.id);
    const old = draft.objects.find((o) => o.id === object.id);
    const ringChanged =
      JSON.stringify([old?.ring,old?.holes||[]]) !== JSON.stringify([object.ring,object.holes||[]]);
    const outlineDecisionChanged = ringChanged || old?.kind!==object.kind || old?.structure_kind!==object.structure_kind || old?.status!==object.status || old?.geometry_reviewed!==object.geometry_reviewed;
    // Pending roof line edits follow an edited outline; lines that leave the new roof are dropped and reported below
    // (the page undo restores them together with the ring).
    const { object: anchored, dropped } = ringChanged && object.kind === "building" && object.roof_line_edits
      ? reanchorRoofLineEdits(object, data.model.pixel_to_meters)
      : { object, dropped: 0 };
    change({
      ...draft,
      reviewed_classes: outlineDecisionChanged ? draft.reviewed_classes.filter(
        (k) => k !== object.kind && k !== old?.kind,
      ) : draft.reviewed_classes,
      objects: draft.objects.map((o) =>
        o.id === object.id
          ? {
              ...object,
              roof_feedback: hasConventionalRoof(object) ? object.roof_feedback : undefined,
              roof_line_edits: hasConventionalRoof(object) ? anchored.roof_line_edits : undefined,
              model: {
                ...object.model,
                ...footprintMetrics(object.ring, data.model.pixel_to_meters, object.holes),
              },
              ...(ringChanged && object.kind === "building"
                ? {
                    roof_parts: undefined,
                    roof_lines: undefined,
                    roof_annotation: null,
                    roof_geometry_source: undefined,
                    roof_geometry_note: undefined,
                  }
                : {}),
            }
          : o,
      ),
    });
    // After change(): it clears the message.
    if (dropped)
      setMessage(`${dropped} roof line${dropped === 1 ? "" : "s"} fell outside the edited outline and ${dropped === 1 ? "was" : "were"} removed from your pending line edits · Undo restores ${dropped === 1 ? "it" : "them"}.`);
    return dropped;
  };
  /** Inspector vegetation-type buttons: set or clear the type on a tree or vegetation outline through change(), so Undo edit restores
   *  the prior draft. Status, outline, label and reviewed classes stay as they are; nothing is saved here. */
  const changeVegetationType = (objectId: string, type: VegetationType | null) => {
    if (!draft || layer === "model") return;
    const next = withReviewVegetationType(draft, objectId, type);
    if (next === draft) return;
    setSelected(objectId);
    change(next);
  };
  /** "Use machine label": the label text from the structure sub-kind or class hint in sentence case, and roof_type only when the
   *  policy names one and no reviewer has shaped this roof. Kind and status are left to changeObject; nothing is saved here. */
  const applyMachineLabel = (object: SceneObject, decision: Decision) => {
    const name = machineLabelName(decision.machine_label);
    if (!name || object.status === "superseded" || layer === "model") return;
    const next: SceneObject = { ...object, label: name };
    const roofType = decision.machine_label.roof_type;
    if (roofType && !hasReviewerRoofEdits(object)) next.roof_type = roofType;
    changeObject(next);
  };
  const persist = useCallback(
    async (next: SceneReview, advance = false, message?: string) => {
      if (saving.current || !data) return false;
      saving.current = true;
      setBusy(true);
      setError("");
      const prior = fresh(data);
      // First queue member after the decided object in scene order, else the last one before it (today's previous-neighbour
      // fallback). Uses next.objects so it works when the decided object has just left the queue and under any status filter.
      const filter = sceneQueueFilter({ kind, status, query, issue });
      const members = next.objects.filter(o => o.id !== selectedId && matchesSceneQueue(o, filter) && vegetationFilterAllows(o, vegetationFilter) && (status !== "candidate" || !skipped.has(o.id)) && frameFilterAllows(o, data.model, frameFilter) && lotScopeAllows(sceneLotRows[o.id], lotAnchor, lotScope));
      const decidedAt = next.objects.findIndex(o => o.id === selectedId);
      const nextId = members.find(o => next.objects.indexOf(o) > decidedAt)?.id ?? members.at(-1)?.id ?? null;
      try {
        const token = getCrmToken();
        const response = await fetch("/api/scenes", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({ id, review: next }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error);
        setData((old) =>
          old ? { ...old, review: result.review, stale: false } : old,
        );
        setDraft(result.review);
        setScenes(old=>old.map(scene=>scene.id===id?sceneSummary(data.model,result.review.objects,undefined,false,data.address_names):scene));
        setUndo([]);
        setLastDecision(prior);
        // A move of the card (the next queue member, or the restored selection after a save) retires the hidden-object notice.
        if (advance) {
          setSelected(nextId);
          setRequested(null);
          setMode("review");
          setApexMode(false);
        } else {
          const target=selectionAfterRestore(selectedId,prior.objects,result.review.objects);
          if(target!==selectedId){setSelected(target);setRequested(null);setStatus("all");setQuery("");setKind("all");setIssue("all");}
        }
        setMessage(message ?? (advance ? "Decision saved." : "Scene saved."));
        return true;
      } catch (e) {
        setDraft(next);
        setError(
          e instanceof Error
            ? e.message
            : "Save did not finish. Your draft is still here.",
        );
        return false;
      } finally {
        saving.current = false;
        setBusy(false);
      }
    },
    [data, id, selectedId, kind, status, query, issue, skipped, frameFilter, sceneLotRows, lotAnchor, lotScope, vegetationFilter],
  );
  const decide = useCallback(
    (right: boolean) => {
      if (!draft || !effective || busy || layer === "model" || instanceMode || effective.status === "superseded") return;
      if (right && hasOutlineIssue(effective)) {
        setError("Resolve outline, object type and separation flags in Needs attention before confirming.");
        return;
      }
      // An outline edit can leave a gap illegal (a model sliver at the roof edge, a gap the new outline crosses): only an
      // outline that would otherwise be refused has those gaps dropped, and the saved message names them.
      const outlineCheck = checkOutlineWithGapRepair(effective.ring, effective.holes, data?.model.image_width, data?.model.image_height);
      const problems = outlineCheck.issues;
      if (right && problems.length) {
        setError(problems[0]);
        setMode("edit");
        return;
      }
      const repairedGaps = problems.length ? [] : outlineCheck.dropped;
      const frame = data?.model.pixel_to_meters;
      const next = {
        ...draft,
        objects: draft.objects.map((o) =>
          o.id === effective.id
            ? {
                // Pending roof line edits follow the repaired gaps, as changeObject makes them follow any outline change.
                ...(repairedGaps.length && frame ? reanchorRoofLineEdits({ ...o, holes: outlineCheck.holes }, frame).object : o),
                status: right
                  ? ("confirmed" as const)
                  : ("needs_adjustment" as const),
                geometry_reviewed: right,
                // A machine-written label that a reviewer confirms as-is would otherwise be indistinguishable from the
                // machine write; stamp the human confirmation so the reflection can count the agreement.
                ...(right && typeof (o as { provenance?: { source?: unknown } }).provenance?.source === "string"
                  && ((o as { provenance?: { source?: string } }).provenance!.source as string).startsWith("machine:")
                  ? { provenance: { ...(o as { provenance?: Record<string, unknown> }).provenance, human_confirmed: true, human_confirmed_at: new Date().toISOString() } }
                  : {}),
              }
            : o,
        ),
      };
      const savedMessage = outlinePending ? (right ? "Outline saved as the reference outline." : "Outline saved; still open for another look.") : undefined;
      // The editor named the dropped gaps before the save; the saved message names them again, with the object, because the page
      // moves on to the next object. A save that does not finish puts the gaps back in the draft (persist keeps `next`), so the
      // editor goes on naming them and the next save reports them.
      void persist(next, true, repairedGaps.length ? `${savedMessage ?? "Decision saved."} ${droppedGapsNotice(repairedGaps, { label: effective.label })}` : savedMessage)
        .then((saved) => {
          if (!saved && repairedGaps.length)
            setDraft((d) => (d === next ? { ...next, objects: next.objects.map((o) => (o.id === effective.id ? { ...o, holes: effective.holes, roof_line_edits: effective.roof_line_edits } : o)) } : d));
        });
    },
    [draft, effective, busy, layer, instanceMode, persist, outlinePending, data?.model.image_width, data?.model.image_height, data?.model.pixel_to_meters],
  );
  // The rejected form of an object (not a roof or structure). A machine-written label that a reviewer rejects is stamped so
  // the reflection can count the disagreement; the reviewer's edited ring, if any, stays on the rejected object only.
  const rejectedObject = (o: SceneObject): SceneObject => ({
    ...o,
    status: "rejected" as const,
    geometry_reviewed: false,
    ...(typeof (o as { provenance?: { source?: unknown } }).provenance?.source === "string"
      && ((o as { provenance?: { source?: string } }).provenance!.source as string).startsWith("machine:")
      ? { provenance: { ...(o as { provenance?: Record<string, unknown> }).provenance, human_rejected: true, human_rejected_at: new Date().toISOString() } }
      : {}),
  });
  // Reject the inspected detection and move on. Same guards as a decision.
  const reject = useCallback(() => {
    if (!draft || !effective || busy || layer === "model" || instanceMode || data?.stale || effective.status === "superseded" || effective.status === "rejected") return;
    const next = {
      ...draft,
      objects: draft.objects.map((o) => (o.id === effective.id ? rejectedObject(o) : o)),
    };
    void persist(next, true);
  }, [draft, effective, busy, layer, instanceMode, data?.stale, persist]);
  // One-key structure relabel: sets the kind and label and saves at once (status needs_adjustment, as the select does),
  // so the reviewer can follow with a confirm or a reject without leaving the keyboard.
  const relabel = useCallback((kind: StructureSubKind | "") => {
    if (!draft || !effective || busy || layer === "model" || instanceMode || data?.stale || effective.kind !== "building"
      || effective.status === "superseded" || effective.status === "rejected") return;
    const name = kind ? STRUCTURE_LABELS[kind] : undefined;
    const next = {
      ...draft,
      objects: draft.objects.map((o) =>
        o.id === effective.id
          ? withStructureKind(o, kind, name ?? (kind ? o.label : "Building"))
          : o,
      ),
      reviewed_classes: draft.reviewed_classes.filter((k) => k !== "building"),
    };
    void persist(next, false);
  }, [draft, effective, busy, layer, instanceMode, data?.stale, persist]);
  useEffect(() => {
    const keyboard = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const onHandle = !!target.closest?.(".scene-vertex-handle");
      // Enter / Shift+Enter write a decision only from the page body, the review card or a vertex handle. A focused
      // <summary>, table region, canvas polygon or any other focusable element keeps its own Enter (open, scroll,
      // select): a keypress meant to open a panel must never confirm or save an outline.
      const onPage = target === document.body || onHandle || !!target.closest?.(".scene-card");
      if (
        e.defaultPrevented ||
        e.isComposing ||
        e.repeat ||
        e.ctrlKey ||
        e.metaKey ||
        e.altKey ||
        apexMode ||
        groupMode ||
        instanceMode ||
        (!onHandle && target.closest?.(
          "input,select,textarea,[contenteditable=true],button,a,summary,[role=slider],[role=button]",
        ))
      )
        return;
      if (mode === "edit") {
        // Enter = Save outline · Looks right; Shift+Enter = Still needs work; nothing pending -> no-op.
        if (e.key === "Enter") { if (outlinePending && onPage) { e.preventDefault(); decide(!e.shiftKey); } return; }
        // Same as Finish editing: draft kept, nothing saved; toggleEdit's busy guard covers hole/trace drawing.
        if (e.key === "Escape") { e.preventDefault(); toggleEdit(); return; }
        // Arrows/Backspace belong to the focused handle; digits are review-only.
        return;
      }
      if (mode !== "review") return;
      if (e.key === "Enter") {
        if (onPage) { e.preventDefault(); decide(!e.shiftKey); }
        return;
      }
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        decide(e.key === "ArrowRight");
        return;
      }
      if (e.key === "Backspace" || e.key === "Delete") {
        e.preventDefault();
        reject();
        return;
      }
      const quick = QUICK_KINDS.find((q) => q.key === e.key);
      if (quick) {
        e.preventDefault();
        relabel(quick.kind);
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [decide, reject, relabel, mode, instanceMode, groupMode, apexMode, outlinePending, toggleEdit]);
  const move = (delta: number) => {
    if(busy)return;
    const pos = queue.findIndex((o) => o.id === selectedId);
    const object = queue[pos + delta];
    if (object) {
      setSelected(object.id);
      setRequested(null);
      setApexMode(false);
    }
  };
  const drawObject = (ring: ScenePoint[]) => {
    if (!draft || !data) return;
    const issues = polygonIssues(ring);
    if (issues.length) {
      setError(issues[0]);
      return;
    }
    const object: SceneObject = {
      id: crypto.randomUUID(),
      kind: newKind,
      label: CLASS_NAMES[newKind].replace(/s$/, ""),
      ring,
      status: "needs_adjustment",
      geometry_reviewed: false,
      source: "reviewer",
      model: {
        height_m:
          newKind === "tree"
            ? 7
            : newKind === "car"
              ? 1.5
              : newKind === "building"
                ? 3.2
                : 0.15,
        base_m: 0,
        height_source: "reviewer-estimate",
        height_verified: false,
        color: CLASS_COLORS[newKind],
        ...footprintMetrics(ring, data.model.pixel_to_meters),
      },
    };
    change({
      ...draft,
      objects: [...draft.objects, object],
      reviewed_classes: draft.reviewed_classes.filter((k) => k !== newKind),
    });
    setStatus("all");
    setIssue("all");
    setKind("all");
    setQuery("");
    setSelected(object.id);
    setRequested(null);
    setMode("edit");
  };
  const leaveMessage = () => busy||extracting?"Finish the current operation before leaving.":heightEditing?"Release or cancel the height edit before leaving.":instanceDrawing?"Finish or cancel the replacement trace before leaving.":boundaryDrawing?"Finish or cancel the interior gap before leaving.":"Save or discard this scene draft before leaving.";
  const guard = (e: React.MouseEvent) => {
    if (dirty||editingGeometry||busy||extracting) {
      e.preventDefault();
      setError(leaveMessage());
    }
  };
  // The URL changed without a remount (hub row, Scene review nav link, Back/Forward). Open the
  // requested scene, return to the hub, or keep the current scene and its draft when leaving is blocked.
  const applyUrl = (params: URLSearchParams) => {
    if (urlScene.current === null) return;
    const requested = params.get("scene") || "";
    if (requested === urlScene.current || requested === id) return;
    if (navigationReason) {
      // Drafts and open operations keep the scene; a load in flight is simply left to finish.
      if (dirty||editingGeometry||busy||extracting) setError(leaveMessage());
      else if (!loading) setError(navigationReason);
      setUrlRestore((n) => n + 1);
      return;
    }
    urlScene.current = requested;
    if (requested) {
      navigateScene(requested, params.get("object"));
      const requestedLayer = params.get("layer");
      if (requestedLayer === "model" || requestedLayer === "edited" || requestedLayer === "overlay") setLayer(requestedLayer);
      return;
    }
    requestedSelection.current = null;
    setId(""); setData(null); setDraft(null); setSelected(null); setLoading(false); setError(""); setMessage("");
    setUndo([]); setLastDecision(null); setSkipped(new Set()); setAddressCheck(null); setRelatedReturn(null);
    setGroupMode(false); setSelectedGroup(null); setReturnGroup(null);
    setMode("review"); setView("aerial"); setLayer("edited"); setApexMode(false); setInstanceMode(false); setMobileLayersOpen(false);
  };
  const extract = async (expandContext = false) => {
    if (dirty) {
      setError("Save or discard your draft before extracting.");
      return;
    }
    setExtracting(true);
    setBusy(true);
    setError("");
    try {
      const token = getCrmToken();
      const response = await fetch("/api/scenes/extract", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(
          data?.model.region
            ? { scene_id: id, prompt, expand_context: expandContext }
            : {
                cohort:
                  data?.model.cohort ||
                  id.match(/^(.*)-[0-9]+$/)?.[1] ||
                  "roof_training_250",
                address_id: Number(extractId),
                prompt,
              },
        ),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      await loadList();
      setId(result.id);
      setReload((n) => n + 1);
      setMessage("Object proposals are ready for review.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Extraction did not finish.");
    } finally {
      setExtracting(false);
      setBusy(false);
    }
  };
  const exportScene = () => {
    if (!data || !draft) return;
    const saved = fresh(data);
    const artifact = {
      schema_version: 1,
      scene: data.model,
      review: saved,
      training: {
        objects: saved.objects
          .filter((o) => o.status === "confirmed" && o.geometry_reviewed)
          .map((o) => ({
            id: o.id,
            kind: o.kind,
            label: o.label,
            ring: o.ring,
            holes: o.holes,
            group_id: o.group_id,
            parent_id: o.parent_id,
            derived_from: o.derived_from,
          })),
        false_positives: saved.objects.filter((o) => o.status === "rejected"),
        reviewed_classes: saved.reviewed_classes,
        height_supervision:
          "Scene confirmations cover class and outline only; heights and apexes are not training labels",
      },
    };
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(artifact, null, 2)], {
        type: "application/json",
      }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `${id}-scene-review.json`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const modelGraph = useMemo(() => {
    if (!effective || !hasConventionalRoof(effective)) return null;
    return effective.roof_parts?.length
      ? annotationFromModel(effective.roof_parts, effective.roof_lines || [], effective.roof_type)
      : effective.roof_annotation || null;
  }, [effective]);
  const originalGraph = useMemo(() => {
    const original = data?.model.objects.find(o => o.id === selectedId);
    return original && hasConventionalRoof(original) && original.roof_parts?.length ? annotationFromModel(original.roof_parts, original.roof_lines || [], original.roof_type) : null;
  }, [data, selectedId]);
  const editorGraph = layer === "model" ? originalGraph : modelGraph;
  const proposeRoof = async () => {
    if (!data || !draft || !effective || !hasConventionalRoof(effective) || busy || roofRequest.current) return;
    const controller = new AbortController(); roofRequest.current = controller;
    setBusy(true); setRoofPending(true); setError("");
    try {
      const token = getCrmToken();
      const response = await fetch("/api/scenes/roof", { method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ id, object: effective, model_signature: data.model.model_signature, revision: draft.revision, form: roofForm, rise: roofRise }) });
      const result = await response.json(); if (!response.ok) throw new Error(result.error);
      if (controller.signal.aborted) return;
      const proposal = result.proposal;
      changeObject({ ...effective, roof_parts: proposal.parts, roof_lines: proposal.lines, roof_annotation: annotationFromModel(proposal.parts, proposal.lines, proposal.roof_type),
        roof_type: proposal.roof_type, roof_geometry_source: proposal.source, roof_geometry_note: proposal.note,
        status: "needs_adjustment", geometry_reviewed: false, model: {...effective.model, height_verified: false} });
      setLayer("edited"); setView("aerial"); setStatus("all"); setInstanceMode(false); setRoofEditorMode("apexes"); setApexMode(true);
      setMessage("Roof proposal ready. Edit its points against the image, then save the scene.");
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "The roof could not be proposed. Your edits are preserved.");
    } finally {
      roofRequest.current = null; setBusy(false); setRoofPending(false);
    }
  };
  return (
    <div className={`review-shell scene-shell ${lineWorkspaceOpen?"is-line-editing":""}`}>
      <Suspense fallback={null}><SceneUrlSync onChange={applyUrl} /></Suspense>
      <WorkbenchHeader
        onNavigate={guard}
        hrefs={{ map: data ? `/?lat=${data.model.centroid.lat}&lon=${data.model.centroid.lon}` : "/" }}
        actions={
          <details className="scene-action-menu"><summary>Scene actions</summary><div>
          <button className="review-button" disabled={!data || dirty || busy || extracting || data.stale || editingGeometry} onClick={() => void extract()} title={data?.stale ? "Recover saved feedback before re-analysing again" : "Run the current model on this captured image; saved feedback is retained"}>{extracting ? "Re-analysing…" : "Re-analyse scene"}</button>
          <Link className="review-button" href="/scenes/new" onClick={guard}>
            Capture map area
          </Link>
          {data?.model.address_id && data.model.cohort && (
            <Link
              className="review-button"
              href={`/reviews?cohort=${data?.model.cohort || "roof_training_250"}&id=${data?.model.address_id || 29790}`}
              onClick={guard}
            >
              Roof workspace
            </Link>
          )}
          <button
            className="review-button"
            disabled={!data || dirty || busy}
            onClick={exportScene}
          >
            Export saved labels
          </button>
          </div></details>
        }
      />
      <div className="scene-layout">
        <aside
          className={`scene-layers ${mobileLayersOpen ? "is-expanded" : ""}`}
          aria-label="Scene layers"
        >
          <label htmlFor="scene-choice">Scene</label>
          <select
            id="scene-choice"
            value={id}
            disabled={!!navigationReason}
            onChange={(e) => navigateScene(e.target.value)}
          >
            {!scenes.length && <option value="">{listLoading ? "Loading scenes…" : listError ? "Scene list unavailable" : "No extracted scenes"}</option>}
            {!id && scenes.length > 0 && <option value="">Choose a scene</option>}
            {id && !scenes.some((scene) => scene.id === id) && (
              <option value={id}>
                {currentSummary ? sceneTitle(currentSummary) : "Requested scene"}
              </option>
            )}
            {scenes.map((s) => (
              <option key={s.id} value={s.id}>
                {sceneTitle(s.id===id&&currentSummary?currentSummary:s)} · {s.progress.candidate+s.progress.needs_adjustment} to review
              </option>
            ))}
          </select>
          <button
            className="review-button scene-layers-toggle"
            aria-expanded={mobileLayersOpen}
            aria-controls="scene-layer-options scene-extraction"
            onClick={() => setMobileLayersOpen(!mobileLayersOpen)}
          >
            {mobileLayersOpen
              ? "Hide layers and extraction"
              : "Layers and extraction"}
          </button>
          <div className="scene-section-title">
            <h2>Layers</h2>
            <button
              className="review-text-button"
              onClick={() => {setVisible(new Set(SCENE_CLASSES));setIsolateLayer(false);}}
            >
              Show all
            </button>
          </div>
          <p className="scene-layer-count-note">{status==="rejected"?"Rejected detections":status==="superseded"?"Replaced outlines":"Active objects"}</p>
          <div className="scene-layer-list" id="scene-layer-options">
            {SCENE_CLASSES.map((k) => {
              const counts=layerProgress[k];
              const count =status==="rejected"?counts.rejected:status==="superseded"?counts.superseded:counts.active;
              if(!count&&!showEmptyLayers&&kind!==k)return null;
              return (
                <div
                  key={k}
                  className={`scene-layer-row ${kind === k ? "is-current" : ""}`}
                >
                  <label title={`Show ${CLASS_NAMES[k]}`}>
                    <input
                      type="checkbox"
                      aria-label={`Show ${CLASS_NAMES[k]}`}
                      disabled={editingGeometry}
                      checked={visible.has(k)}
                      onChange={(e) =>
                        setVisible((old) => {
                          const next = new Set(old);
                          if (e.target.checked) next.add(k);
                          else next.delete(k);
                          return next;
                        })
                      }
                    />
                    <span
                      className="scene-layer-dot"
                      style={{ background: CLASS_COLORS[k] }}
                    />
                  </label>
                  <button
                    disabled={editingGeometry||groupMode}
                    onClick={() => {setKind(kind === k ? "all" : k);setApexMode(false);}}
                    aria-pressed={kind === k}
                    title={`${counts.active} active · ${counts.rejected} rejected · ${counts.superseded} replaced`}
                  >
                    {CLASS_NAMES[k]}
                    <span>{count}</span>
                  </button>
                </div>
              );
            })}
          </div>
          <button className="review-text-button scene-empty-layers" onClick={()=>setShowEmptyLayers(!showEmptyLayers)} aria-expanded={showEmptyLayers}>{showEmptyLayers?"Hide empty layers":"Show empty layers"}</button>
          <details className="scene-extract" id="scene-extraction">
            <summary>
              <Sparkles size={15} /> Extract a scene
            </summary>
            {!data?.model.region && (
              <label>
                Address ID
                <input
                  value={extractId}
                  type="number"
                  onChange={(e) => setExtractId(e.target.value)}
                />
              </label>
            )}
            <label>
              Additional object to find
              <input
                value={prompt}
                maxLength={150}
                placeholder="e.g. garden shed"
                onChange={(e) => setPrompt(e.target.value)}
              />
            </label>
            <button
              className="review-button"
              disabled={
                extracting ||
                dirty ||
                busy ||
                (!data?.model.region &&
                  !Number.isSafeInteger(Number(extractId)))
              }
              onClick={() => void extract()}
            >
              {extracting ? (
                <Loader2 className="animate-spin" size={15} />
              ) : (
                <Sparkles size={15} />
              )}{" "}
              {extracting ? "Finding objects…" : "Find objects"}
            </button>
            <p>
              Uses this captured image. Buildings, trees, roads, cars and
              surface objects are proposed for review.
            </p>
            {data?.model.region && (
              <p>
                {data.model.region.provider === "google"
                  ? "Google aerial"
                  : "ESRI World Imagery"}{" "}
                · {Math.round(data.model.region.span_m)} m area.{" "}
                {data.model.region.split === "unassigned"
                  ? "Crosses training split boundaries; labels remain outside training exports."
                  : ""}
              </p>
            )}
          </details>
        </aside>
        <main className="scene-main">
          {hub ? <SceneStart scenes={scenes} loading={listLoading} error={listError} onRetry={retryList} /> : <>
          <div className="scene-heading">
            <div>
              <h1>
                {currentSummary ? !groupMode && effective?.address_id
                  ? selectedAddress?.split(",")[0] || effective.label : sceneTitle(currentSummary)
                  : requestedTitle}
              </h1>
              {data && <p>{selectedAddress?"Reviewing this property.":"Area review."} {draft ? `${progress.total} annotations; ` : "Extract a scene to start; "}3D heights are model estimates. Neighbouring buildings remain individually selectable.</p>}
              {addressCheck?.scene===id&&<p role="status">Address check needed for {data?.address_names?.[addressCheck.address]?.split(",")[0]||`address #${addressCheck.address}`}. Select its roof and confirm the property link; this image also contains neighbours.</p>}
            </div>
            <div className="scene-heading-actions">
              {!groupMode&&<button className="review-button scene-review-jump" disabled={!data||!draft} aria-controls="scene-review-card"
                onClick={()=>{reviewCard.current?.focus({preventScroll:true});reviewCard.current?.scrollIntoView({block:"start"});}}>
                Jump to review
              </button>}
              <button className="review-button" aria-pressed={groupMode} disabled={busy||!data||!draft} onClick={()=>groupMode?setGroupMode(false):openGroups()}><Layers3 size={15}/>Groups</button>
            {!groupMode&&<div className="review-view-switch" aria-label="Scene view">
              <button
                disabled={editingGeometry}
                aria-pressed={view === "aerial"}
                onClick={() => setView("aerial")}
              >
                Aerial
              </button>
              <button
                disabled={editingGeometry}
                aria-pressed={view === "3d"}
                onClick={() => {setView("3d");setApexMode(false);}}
              >
                3D scene
              </button>
            </div>}
            </div>
          </div>
          <div className="scene-status-space" aria-live="polite">
            {error ? (
              <p className="review-error" role="alert">
                {error}{" "}
                <button
                  className="review-text-button"
                  disabled={busy}
                  onClick={() => {
                    if (dirty)
                      setError(
                        "Your draft is retained. Save again or discard before reloading.",
                      );
                    else setReload((n) => n + 1);
                  }}
                >
                  Reload scene
                </button>
              </p>
            ) : (
              <p className="review-muted">
                {message ||
                  "Review an outline, adjust its details, then move to the next object."}
              </p>
            )}
          </div>
          {data?.stale && (
            <div className="review-notice">
              <p>A newer extraction is available. Previous edits remain in history.</p>
              {data.recovery && data.recovery.carried_objects > 0 ? <>
                <p>{data.recovery.method === "georeferenced-affine" ? recaptureSentence(data) : data.model.context_expansion ? "The original image pixels were preserved inside a larger capture. Earlier edits keep their geographic positions." : "The image is unchanged."} Keep {data.recovery.carried_objects} earlier objects alongside {data.recovery.new_candidates} fresh proposals. Carried outlines need another check; layer approvals reset. Similar detections remain visible for duplicate review.</p>
                {data.recovery.method === "georeferenced-affine" && recaptureFitSentence(data.recovery) ? <p>{recaptureFitSentence(data.recovery)}</p> : null}
                <button className="review-button" disabled={busy} onClick={() => {
                  change(data.recovery!.review);
                  setSelected(data.recovery!.review.objects.find(o => o.status !== "candidate")?.id || null);setRequested(null);
                  setView("aerial");
                  setMode("review");
                  setApexMode(false);
                  setLayer("overlay");
                  setStatus("all");
                  setKind("all");
                  setQuery("");
                }}>Preview earlier edits</button>{" "}
                <button className="review-button review-primary" disabled={busy} onClick={() => {
                  setStatus("all");
                  void persist(data.recovery!.review);
                }}>Carry edits forward</button>{" "}
              </> : data.recovery_reason ? <p>{data.recovery_reason}</p> : null}
              <button
                className="review-button"
                disabled={busy}
                onClick={() => void persist(fresh(data))}
              >
                Start from predictions
              </button>
            </div>
          )}
          {skipped.size > 0 && (
            <button
              className="review-text-button"
              onClick={() => setSkipped(new Set())}
            >
              Return to {skipped.size} skipped object
              {skipped.size === 1 ? "" : "s"}
            </button>
          )}
          {!groupMode&&!lineWorkspaceOpen&&<div className="scene-queue-controls">
            <div className="review-search">
              <Search size={15} />
              <input
                ref={search}
                aria-label="Search scene objects"
                disabled={editingGeometry}
                placeholder="Find an object…"
                maxLength={200}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              {query && (
                <button
                  aria-label="Clear object search"
                  disabled={editingGeometry}
                  onClick={() => {
                    setQuery("");
                    search.current?.focus();
                  }}
                >
                  <X size={15} />
                </button>
              )}
            </div>
            <select
              aria-label="Object layer filter"
              disabled={editingGeometry}
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              <option value="all">All layers</option>
              {SCENE_CLASSES.map((k) => (
                <option key={k} value={k}>
                  {CLASS_NAMES[k]}
                </option>
              ))}
            </select>
            <select
              aria-label="Object review filter"
              disabled={editingGeometry}
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              <option value="candidate">To review</option>
              <option value="needs_adjustment">Needs edit</option>
              <option value="confirmed">Looks right</option>
              <option value="rejected">Rejected detections</option>
              <option value="superseded">Replaced outlines</option>
              <option value="all">All objects</option>
            </select>
            <select aria-label="Feedback filter" disabled={editingGeometry} value={issue} onChange={event=>{
              setIssue(event.target.value);setStatus("all");setSkipped(new Set());
            }}>
              <option value="all">All feedback</option>
              <option value="any">Flagged objects</option>
              {SCENE_REVIEW_ISSUES.map(value=><option key={value} value={value}>{REVIEW_ISSUE_LABELS[value]}</option>)}
            </select>
            <select aria-label="Frame filter" disabled={editingGeometry} value={frameFilter} onChange={event=>setFrameFilter(frameFilterValue(event.target.value))}>
              <option value="skip">Skip frame-cut</option>
              <option value="all">All objects</option>
              <option value="only">Frame-cut only</option>
            </select>
            <select aria-label="Lot filter" disabled={editingGeometry||!lotAnchor} value={lotAnchor?lotScope:"all"} data-lot-anchor={lotAnchor?.source??"none"} onChange={event=>setLotScope(lotScopeValue(event.target.value))}
              title={lotAnchor?`${lotAnchor.source==="asked"?"This window has no address lot: the land of the roof it was cut for (the whole-roof takeover or the carried slices) stands in. ":""}Objects whose dominant lot (lot tags from the eyes index) lies outside this scope leave the queue; an object you open stays in view. A hint, never a label.`:"The address lot could not be resolved for this scene: nothing is hidden."}>
              {lotAnchor?<>
                <option value="land">{lotAnchor.source==="asked"?"The asked roof's land":"This address's land"}</option>
                <option value="lot">{lotAnchor.source==="asked"?"The asked roof's lot only":"This lot only"}</option>
                <option value="all">All objects</option>
              </>:<option value="all">Lot unknown: all objects</option>}
            </select>
            <select aria-label="Vegetation type filter" data-testid="scene-vegetation-filter" disabled={editingGeometry} value={vegetationFilter}
              title="Tree and vegetation outlines by the vegetation type a reviewer stored. Not set keeps the tree and vegetation outlines without a type; other kinds leave the queue under any choice but All vegetation types. A filter, never a label."
              onChange={event=>{const value=vegetationFilterValue(event.target.value);setVegetationFilter(value);if(value!=="all"&&kind!=="all"&&!carriesVegetationType(kind))setKind("all");}}>
              <option value="all">All vegetation types</option>
              <option value="unset">Vegetation type not set ({vegetationCounts.unset})</option>
              {VEGETATION_TYPES.map(type=><option key={type} value={type}>{VEGETATION_TYPE_LABELS[type]} ({vegetationCounts[type]})</option>)}
            </select>
            {lotNote&&<span className="review-muted scene-lot-note" data-testid="scene-lot-note" data-lot-hidden={lotSummary.hidden}>{lotNote}</span>}
          </div>}
          {loading ? (
            <div className="review-state" role="status">
              <Loader2 className="animate-spin" />
              <h2>Loading scene</h2>
            </div>
          ) : !data || !draft ? (
            <div className="review-state">
              <h2>No scene ready yet</h2>
              <p>Use Extract a scene to find objects in a captured image.</p>
            </div>
          ) : (
            <>
              {groupMode?<SceneGroupEditor key={id} data={data} review={draft} visible={visible} selectedGroup={selectedGroup} onSelectGroup={setSelectedGroup}
                layer={layer} onLayer={setLayer} onChange={change} onInspect={inspectGrouped} disabled={busy}
                onClose={()=>{setGroupMode(false);relatedFocus.current=selectedId;}}/>:lineWorkspaceOpen&&effective?<section className="scene-line-workspace" ref={apexSection}>
                  <RoofLineEditor key={`${id}:${effective.id}`} object={effective} scene={data.model} image={reviewImage}
                    disabled={working||data.stale} onPending={setRoofLinePending} dirty={dirty} saving={working} revision={draft.revision}
                    onChange={lines=>changeObject(withRoofLineEdits(effective,lines))} onSave={()=>void persist(draft)}
                    onClose={()=>setApexMode(false)} onApex={()=>setRoofEditorMode("apexes")} hasApexes={!!modelGraph} onEditOutline={editOutlineFromLines}
                    onEnhance={()=>void toggleDetail()} enhanced={detailEnabled} enhancing={detailLoading} imageError={detailError}/>
                </section>:<div className="scene-workspace">
                <div className="scene-review-column">
                {requestedHidden && <p className="review-notice scene-hidden-notice" role="status" data-testid="scene-hidden-notice" data-hidden-object={requestedHidden.id}>
                  <span>{CLASS_NAMES[requestedHidden.kind].replace(/s$/, "")} {requestedHidden.id.slice(0, 8)} is hidden: {hiddenReasons.join(" and ")}.</span>
                  <button type="button" className="review-button" disabled={busy} onClick={() => {
                    // Widen only the filter(s) that hide it, then select it; a hint widened by the reviewer, never by the page.
                    if (hiddenByFrame) setFrameFilter("all");
                    if (hiddenByLot) setLotScope("all");
                    setSelected(requestedHidden.id);
                  }}>Show it anyway</button>
                </p>}
                <section id="scene-review-card" ref={reviewCard} tabIndex={-1} className="scene-card" aria-label="Object review card">
                  <div className="scene-card-heading">
                    <div>
                      <span
                        className="scene-object-dot"
                        style={{
                          background: effective
                            ? CLASS_COLORS[effective.kind]
                            : undefined,
                        }}
                      />
                      <strong>
                        {inspected?.label || effective?.label || "This queue is clear"}
                      </strong>
                      <span className="review-muted">
                        {predictionLabel}
                      </span>
                    </div>
                    <div className="scene-card-step">
                      <button
                        aria-label="Previous scene object"
                        disabled={busy || outlinePending || position <= 0}
                        title={outlinePending ? "Save or discard the outline edit first." : undefined}
                        onClick={() => move(-1)}
                      >
                        <ChevronLeft size={18} />
                      </button>
                      <span>
                        {queue.length
                          ? position >= 0 ? `${position + 1} / ${queue.length}` : `${queue.length} left`
                          : "0 left"}
                      </span>
                      <button
                        aria-label="Next scene object"
                        disabled={busy || outlinePending || position >= queue.length - 1}
                        title={outlinePending ? "Save or discard the outline edit first." : undefined}
                        onClick={() => move(1)}
                      >
                        <ChevronRight size={18} />
                      </button>
                    </div>
                  </div>
                  <div className="scene-mode-bar">
                    <div
                      className="review-view-switch"
                      aria-label="Scene geometry layer"
                    >
                      {(["model", "edited", "overlay"] as const).map((l) => (
                        <button
                          key={l}
                          disabled={editingGeometry}
                          aria-pressed={layer === l}
                          onClick={() => {
                            setLayer(l);
                            setInstanceMode(false);
                            if (l === "model") setMode("review");
                          }}
                        >
                          {l === "model"
                            ? "Prediction"
                            : l === "edited"
                              ? "Your edits"
                              : "Compare"}
                        </button>
                      ))}
                    </div>
                    {effective && hasConventionalRoof(effective) && <button className="review-button" disabled={busy || data.stale || effective.status === "superseded"} onClick={()=>{
                      setRoofEditorMode("lines");setApexMode(true);setInstanceMode(false);setMode("review");setLayer("edited");setView("aerial");
                    }}>Edit roof lines</button>}
                    <label className="scene-layer-scope">
                      <input type="checkbox" checked={isolateLayer && kind !== "all"} disabled={editingGeometry || kind === "all"}
                        onChange={event => setIsolateLayer(event.target.checked)}/>
                      Only review layer
                    </label>
                    <button
                      className="review-button"
                      disabled={busy || !effective || effective.status === "superseded"}
                      onClick={toggleEdit}
                    >
                      <Pencil size={14} />
                      {mode === "edit" ? "Finish editing" : "Edit outline"}
                    </button>
                    <button
                      className="review-button"
                      disabled={busy || data.stale}
                      onClick={() => {
                        setMode(mode === "draw" ? "review" : "draw");
                        setInstanceMode(false);
                        setLayer("edited");
                        setView("aerial");
                      }}
                    >
                      {" "}
                      {mode === "draw" ? "Cancel drawing" : "Add object"}
                    </button>
                  </div>
                  {outlineDiffers&&<div className="scene-outline-version" role="status" aria-label="Roof outline versions">
                    <strong>{layer==="model"?"Showing current prediction":layer==="overlay"?"Comparing roof outlines":"Showing saved feedback"}</strong>
                    <span>{layer==="model"?"The saved outline and apex edits are preserved in Your edits.":
                      layer==="overlay"?"Cyan: saved outline. Dashed amber: current prediction. Roof lines show saved feedback.":
                      "The current prediction has a different outline. Saved apex edits keep the outline they were drawn on."}</span>
                  </div>}
                  {mode === "draw" && (
                    <label className="scene-draw-kind">
                      Object class
                      <select
                        value={newKind}
                        onChange={(e) =>
                          setNewKind(e.target.value as SceneKind)
                        }
                      >
                        {SCENE_CLASSES.map((k) => (
                          <option value={k} key={k}>
                            {CLASS_NAMES[k]}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {view === "aerial" && effective?.kind === "building" && <div className="scene-roof-legend scene-imagery-controls">
                    <label>Imagery
                      <select aria-label="Imagery" value={imageryKey} disabled={detailLoading} onChange={e=>void selectImagery(e.target.value)}>
                        <option value={ORIGINAL_IMAGERY}>Original imagery</option>
                        <option value={DETAIL_IMAGERY}>Enhanced contrast</option>
                        {imageryOptions.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                      </select>
                    </label>
                    <button className="review-button" aria-pressed={detailEnabled} disabled={detailLoading} onClick={()=>void toggleDetail()}>
                      {detailLoading ? "Preparing view…" : detailEnabled ? "Show original imagery" : isShadeSail(effective) ? "Enhance image detail" : "Enhance roof detail"}
                    </button>
                    <small style={{fontSize:12}}>{detailLoading ? "Preparing view…" : imageryKey === ORIGINAL_IMAGERY ? isShadeSail(effective) ? "Inspect fabric boundaries in the original image" : "Make faint roof lines easier to inspect" : "Display view for inspection; saves still use the original image"}</small>
                    {detailError && <span role="alert">{detailError}</span>}
                  </div>}
                  {view === "3d" ? (
                    <Scene3D
                      scene={data.model}
                      predictions={layer === "overlay" ? data.model.objects : undefined}
                      objects={
                        layer === "model" ? data.model.objects : draft.objects
                      }
                      selected={selectedId}
                      visible={visible}
                      objectIds={reviewObjectIds}
                      onSelect={selectCanvasObject}
                      onChange={layer === "model" ? undefined : changeObject}
                      onEditingChange={setHeightEditing}
                      disabled={working || outlineDrawing || data.stale || instanceMode}
                    />
                  ) : (
                  <SceneCanvas
                    scene={data.model}
                      key={`${id}:${mode}:${mode==="edit"?`${selectedId}:${layer}`:""}`}
                      onExpand={dirty||data.stale ? undefined : ()=>void extract(true)}
                      lotFrameSentence={servedHints?.lot_frame ?? null}
                      image={reviewImage}
                      objects={draft.objects}
                      original={data.model.objects}
                      selected={selectedId}
                      visible={visible}
                      objectIds={canvasObjectIds}
                      mode={mode}
                      layer={layer}
                      disabled={working || instanceDrawing || data.stale || effective?.status === "superseded"}
                      onDrawingChange={setBoundaryDrawing}
                      onSelect={selectCanvasObject}
                      onChange={changeObject}
                      onDraw={drawObject}
                      onSwipe={decide}
                      pendingOutline={outlinePending}
                      squaredCandidate={servedHints?.object_hints?.squared_candidate ?? null}
                    />
                  )}
                  <div className="scene-decisions" data-outline-pending={outlinePending ? "true" : "false"}>
                    <button
                      className="scene-decision needs-edit"
                      data-testid="scene-needs-edit-decision"
                      disabled={
                        busy || !effective || layer === "model" || data.stale || instanceMode || effective.status === "superseded"
                      }
                      title={outlinePending ? "Stores your outline and keeps the object open for another look (needs adjustment)." : undefined}
                      onClick={() => decide(false)}
                    >
                      <X size={20} />
                      <span>
                        {outlinePending ? <>Save outline · Still needs work<small>Stays open · Shift+Enter</small></> : <>Needs edit<small>Swipe left</small></>}
                      </span>
                    </button>
                    <button
                      className={`scene-decision not-a-roof${rejectSuggestion && inspected?.id === effective?.id ? " suggested" : ""}`}
                      data-testid="scene-reject-decision"
                      data-suggested-by={rejectSuggestion && inspected?.id === effective?.id ? rejectSuggestion.code : undefined}
                      disabled={
                        busy || !effective || layer === "model" || data.stale || instanceMode || effective.status === "superseded" || effective.status === "rejected"
                      }
                      title={outlinePending ? "Rejects this detection; the edited outline is stored on the rejected object only."
                        : rejectSuggestion && inspected?.id === effective?.id ? `Not a roof or structure: reject this detection (Backspace). Agent hint: ${rejectSuggestion.sentence} Reject only if you agree.`
                        : "Not a roof or structure: reject this detection (Backspace)"}
                      onClick={() => reject()}
                    >
                      <X size={20} />
                      <span>
                        Not a roof<small>Backspace</small>
                      </span>
                    </button>
                    <button
                      className="scene-skip"
                      disabled={!effective || busy || outlinePending}
                      title={outlinePending ? "Save or discard the outline edit first." : undefined}
                      onClick={() => {
                        if (effective) {
                          setSkipped((old) => new Set([...old, effective.id]));
                          setRequested(null);
                          move(1);
                        }
                      }}
                    >
                      Skip
                    </button>
                    <button
                      className="scene-decision looks-right"
                      data-testid="scene-looks-right-decision"
                      disabled={
                        busy || !effective || layer === "model" || data.stale || instanceMode || effective.status === "superseded"
                      }
                      title={outlinePending ? "Stores your outline as the reviewed reference outline (confirmed, geometry reviewed)." : undefined}
                      onClick={() => decide(true)}
                    >
                      <Check size={20} />
                      <span>
                        {outlinePending ? <>Save outline · Looks right<small>Reference outline · Enter</small></> : <>Looks right<small>Swipe right</small></>}
                      </span>
                    </button>
                  </div>
                  {effective?.kind === "building" && effective.status !== "superseded" && (
                    <div className="scene-quick-kinds" data-testid="scene-quick-kinds" aria-label="Quick structure relabel">
                      {QUICK_KINDS.map((q) => (
                        <button
                          key={q.key}
                          type="button"
                          className={`scene-quick-kind ${(effective.structure_kind ?? "") === q.kind ? "is-current" : ""}${suggestedKind && q.kind === suggestedKind && inspected?.id === effective.id ? " suggested" : ""}`}
                          aria-describedby={suggestedKind && q.kind === suggestedKind && inspected?.id === effective.id ? "scene-secondary-suggestion" : undefined}
                          disabled={busy || layer === "model" || data.stale || instanceMode || effective.status === "rejected"}
                          title={`${q.label}: press ${q.key}${suggestedKind && q.kind === suggestedKind && inspected?.id === effective.id ? ". Agent hint suggests this band; press only if you agree." : ""}`}
                          onClick={() => relabel(q.kind)}
                        >
                          <kbd>{q.key}</kbd> {q.label}
                        </button>
                      ))}
                      {secondarySuggestion && suggestedKind && inspected?.id === effective.id && <span id="scene-secondary-suggestion" className="review-muted scene-quick-kind-suggestion" data-testid="scene-secondary-suggestion">{secondarySuggestion.sentence} A hint, not a label: the chip changes nothing until you press it.</span>}
                    </div>
                  )}
                  <p className="scene-decision-note">
                    {outlinePending
                      ? "Your outline is not saved yet. Save outline · Looks right (Enter) stores it as the reviewed reference outline; Save outline · Still needs work (Shift+Enter) stores it and keeps the object open. The model estimate you edited from stays in the model snapshot for comparison. Backspace rejects; Digits (review mode) set the structure kind and save. Save scene stores everything without a decision."
                      : "A right swipe or Enter confirms the outline and object class; a left swipe or Shift+Enter keeps it open. Backspace rejects a detection that is not a roof or structure. Digits set the structure kind and save; follow with a confirm or a reject. Heights stay unverified."}
                  </p>
                </section>
                <SceneQueueNavigator currentId={id} kind={kind} status={status} query={query} issue={issue} revision={draft.revision}
                  modelSignature={data.model.model_signature} disabledReason={navigationReason} onNavigate={navigateScene}/>
                </div>
                <aside className="scene-inspector" aria-label="Object details">
                  <SceneReviewProgress progress={progress} dirty={dirty}/>
                  {effective?.kind==="building"&&effective.status!=="superseded"&&effective.status!=="rejected"&&<SceneAddressControls
                    key={`${id}:${effective.id}:${draft.revision}`} sceneId={id} signature={data.model.model_signature} revision={draft.revision}
                    object={effective} title={selectedAddress} disabled={busy||dirty||layer==="model"||data.stale}
                    onChoose={address=>{
                      setData(old=>old?{...old,address_names:{...old.address_names,[address.address_id]:address.full_address}}:old);
                      changeObject({...effective,address_id:address.address_id});
                    }}/>} 
                  {returnGroup&&draft.groups.some(g=>g.id===returnGroup)&&<button className="review-button scene-related-return" disabled={busy} onClick={()=>openGroups(returnGroup)}>
                    <ArrowLeft size={14}/>Return to group · {draft.groups.find(g=>g.id===returnGroup)?.label}
                  </button>}
                  {relatedReturn?.sceneId===id&&relatedReturn.objectId!==selectedId&&draft.objects.some(o=>o.id===relatedReturn.objectId)&&
                    <button className="review-button scene-related-return" disabled={busy||instanceMode||roofPending} onClick={returnToObject}>
                      <ArrowLeft size={14}/> Return to {draft.objects.find(o=>o.id===relatedReturn.objectId)?.label}
                    </button>}
                  {effective?.status === "superseded" && <div className="review-notice">
                    <p>This outline was replaced and is kept for history. It is excluded from training targets and false positives.</p>
                    {effective.replacement_ids?.map(id=><button key={id} className="review-button" onClick={()=>{setSelected(id);setRequested(null);setQuery("");setKind("all");setStatus("all");setIssue("all");setFrameFilter("all");setLotScope("all");setVegetationFilter("all");}}>
                      View {draft.objects.find(o=>o.id===id)?.label || "replacement"}
                    </button>)}
                    <p>Use Undo or saved history to restore the earlier set of outlines.</p>
                  </div>}
                  <fieldset disabled={busy || layer === "model" || data.stale || inspected?.status === "superseded"}>
                    <h2>Object details</h2>
                    {inspected ? (
                      <>
                        <label>
                          Label
                          <input
                            value={inspected.label}
                            maxLength={150}
                            disabled={busy}
                            onChange={(e) =>
                              changeObject({
                                ...inspected,
                                label: e.target.value,
                              })
                            }
                          />
                        </label>
                        <label>
                          Class
                          <select
                            value={inspected.kind}
                            disabled={busy}
                            onChange={(e) =>
                              changeObject({
                                ...inspected,
                                kind: e.target.value as SceneKind,
                                vegetation_type: carriesVegetationType(e.target.value) ? inspected.vegetation_type : undefined,
                                model: { ...inspected.model, tree: e.target.value === "tree" ? inspected.model.tree : undefined,
                                  car: e.target.value === "car" ? inspected.model.car : undefined,
                                  solar: e.target.value === "solar_panel" ? inspected.model.solar : undefined,
                                  surface: isPavedSurface(e.target.value as SceneKind) ? inspected.model.surface : undefined },
                                status: "needs_adjustment",
                                geometry_reviewed: false,
                              })
                            }
                          >
                            {SCENE_CLASSES.map((k) => (
                              <option value={k} key={k}>
                                {CLASS_NAMES[k]}
                              </option>
                            ))}
                          </select>
                        </label>
                        {inspected.kind === "building" && <label>
                          Structure kind
                          <select
                            aria-label="Structure kind"
                            value={inspected.structure_kind ?? ""}
                            disabled={busy}
                            onChange={(e) => {
                              const kind = e.target.value;
                              const name = kind ? STRUCTURE_LABELS[kind as StructureSubKind] : undefined;
                              changeObject(withStructureKind(inspected, kind, name ?? inspected.label));
                            }}
                          >
                            <option value="">No sub-kind (not set)</option>
                            {STRUCTURE_KINDS.filter(k => STRUCTURE_LABELS[k]).map(k => <option value={k} key={k}>{STRUCTURE_LABELS[k]}</option>)}
                          </select>
                        </label>}
                        {carriesVegetationType(inspected.kind) && <SceneVegetationType object={inspected} disabled={busy}
                          onChange={type => changeVegetationType(inspected.id, type)}/>}
                        {inspected.kind === "pool" && objectHintList.some(h => h.code === "pool_not_blue") && <p className="review-muted scene-pool-colour-note" data-testid="scene-pool-colour-note">Model says pool; colour reads as grass.</p>}
                        {(agentHintsText || hintsStale) && <p className="review-muted scene-agent-hints" data-testid="scene-agent-hints" data-hint-stale={hintsStale ? "true" : undefined} data-hint-stale-reasons={servedHints?.stale_reasons?.length ? servedHints.stale_reasons.join(" ") : undefined} data-hints-served-version={servedHints?.object_hints_served_version ?? undefined} data-hints-served={servedHints ? "true" : undefined}>Agent hints: {hintsStale && <span className="scene-hint-stale">{staleNotes.join(" ")} </span>}{agentHintsText}{machineDecision && <>{" "}<button type="button" className="review-button scene-use-machine-label" data-testid="scene-use-machine-label"
                          disabled={busy || layer === "model" || data.stale || inspected.status === "superseded" || !machineLabelName(machineDecision.machine_label)}
                          title="Applies the machine label to this object's Label and roof type; you still save the scene."
                          onClick={() => applyMachineLabel(inspected, machineDecision)}>Use machine label</button></>}</p>}
                        {objectHintList.length > 0 && <details className="scene-object-hints" data-testid="scene-object-hints" data-hint-codes={objectHintList.map(h => h.code).join(" ")}>
                          <summary>Why flagged ({objectHintList.length})</summary>
                          <ul>
                            {objectHintList.map(h => <li key={h.code} className={h.severity === "review" ? "is-review" : "is-context"} data-hint-code={h.code} data-hint-severity={h.severity}>
                              {h.sentence}
                              <small>{h.severity === "review" ? "Review" : "Context"} · {h.code.replaceAll("_", " ")} · {"source" in h && h.source === OUTLINE_HINTS_SOURCE ? "measured in the editor from the stored outline" : "eyes index"}{evidenceWords(h.evidence) ? ` · ${evidenceWords(h.evidence)}` : ""}</small>
                            </li>)}
                          </ul>
                          {outlineEvidence && <p>{outlineEvidence}</p>}
                          {servedHints?.lot && servedHints.lot.flags.length > 0 && <p>Lot flags: {servedHints.lot.flags.map(f => f.replaceAll("_", " ")).join(", ")}.</p>}
                          <p>Agent hints, not labels. Thresholds are design values ({OBJECT_HINTS_VERSION}{olderObjectHints ? `; served from ${olderObjectHints} rows` : ""}); population flag rates are the only over-flag measure so far.</p>
                        </details>}
                        {inspected.clipped_by_frame && <p className="review-muted scene-frame-clip" data-testid="scene-frame-clip">Cut by the earlier {inspected.clipped_by_frame[2] - inspected.clipped_by_frame[0]} px frame: the model drew this outline before the scene was widened and found no fuller shape on the wider image. The straight edge is the old frame, not the object; reject it or redraw the boundary if the object continues.</p>}
                        {surfaceClassHelp[inspected.kind] && <p className="review-muted">{surfaceClassHelp[inspected.kind]}</p>}
                        {hasConventionalRoof(inspected) && <SceneRoofObservations key={`${id}:${inspected.id}`} sceneId={id} objectId={inspected.id}
                          modelSignature={data.model.model_signature} imageSha256={data.model.image_sha256}/>}
                        <SceneIssueControls object={inspected} onChange={changeObject}/>
                        <SceneVisibilityControls object={inspected} scene={data.model} onChange={changeObject}/>
                        <div className="scene-measurements">
                          <span>
                            {visibleExtent?.partial?"Visible footprint":"Footprint"}
                            <strong>
                              {inspected.model.area_m2.toFixed(1)} m²
                            </strong>
                          </span>
                          <span>
                            {hasThickness ? "Thickness" : visibleExtent?.footprintOnly ? "Stored height" : "Model height"}
                            <strong>
                              {inspected.model.height_m.toFixed(hasThickness ? 2 : 1)} m
                            </strong>
                          </span>
                        </div>
                        <div className="scene-dimensions">
                          <label>
                            {hasThickness ? "Thickness (m)" : visibleExtent?.footprintOnly ? "Stored height (m)" : "Height (m)"}
                            <MetricInput
                              key={`height:${inspected.id}`}
                              aria-label={hasThickness ? "Thickness (m)" : visibleExtent?.footprintOnly ? "Stored height (m)" : "Height (m)"}
                              min={heightRange.min}
                              max={heightRange.max}
                              step={hasThickness ? ".01" : ".1"}
                              value={inspected.model.height_m}
                              disabled={busy}
                              onValueChange={(value) =>
                                changeObject(withObjectHeight(inspected, value))
                              }
                            />
                          </label>
                          {followsRoof ? <div className="scene-roof-elevation">
                            <span>{placement.solar?"Bank underside (m)":"Supporting roof (m)"}</span>
                            <strong>{placement.min.toFixed(2)}–{placement.max.toFixed(2)}</strong>
                          </div> : <label>
                            Base elevation (m)
                            <MetricInput
                              key={`base:${inspected.id}`}
                              min="0"
                              max="150"
                              step=".1"
                              value={inspected.model.base_m}
                              disabled={busy}
                              onValueChange={(value) =>
                                changeObject({
                                  ...inspected,
                                  model: {
                                    ...inspected.model,
                                    base_m: value,
                                    height_verified: false,
                                  },
                                })
                              }
                            />
                          </label>}
                        </div>
                        {visibleExtent?.footprintOnly && <p className="review-muted">Height feedback is saved; this footprint preview has no modelled height.</p>}
                        {inspected.kind === "tree" && !visibleExtent?.footprintOnly && <details className="scene-tree-controls" open>
                          <summary>Tree shape</summary>
                          <label>Crown shape<select aria-label="Crown shape" value={inspected.model.tree?.crown_profile || "rounded"} onChange={e => changeObject({
                            ...inspected, model: { ...inspected.model, height_verified: false,
                              tree: { crown_base_ratio: inspected.model.tree?.crown_base_ratio ?? .25, crown_profile: e.target.value as "rounded" | "conical" | "spreading" } },
                          })}>
                            <option value="rounded">Rounded</option><option value="conical">Conical</option><option value="spreading">Spreading</option>
                          </select></label>
                          <label>Crown base above ground (m)<MetricInput aria-label="Crown base above ground (m)" key={`crown:${inspected.id}`}
                            min="0" max={inspected.model.height_m*.95} step=".1" disabled={busy || inspected.model.height_m <= 0}
                            value={Number(((inspected.model.tree?.crown_base_ratio ?? .25)*inspected.model.height_m).toFixed(2))}
                            onValueChange={value => {
                              if (value < 0 || value > inspected.model.height_m*.95 || inspected.model.height_m <= 0) return;
                              changeObject({ ...inspected, model: { ...inspected.model, height_verified: false,
                                tree: { crown_profile: inspected.model.tree?.crown_profile || "rounded", crown_base_ratio: value/inspected.model.height_m } } });
                            }} /></label>
                          <p className="review-muted">The crown follows its outline. Crown shape, trunk position and height remain estimates. Changing total height preserves the crown proportions.</p>
                        </details>}
                        {inspected.kind === "car" && <SceneCarControls object={inspected} scene={data.model} onChange={changeObject}/>}
                        {hasConventionalRoof(inspected) && <SceneRoofFeedback object={inspected} frame={data.model.pixel_to_meters} onChange={changeObject}/>}
                        {isShadeSail(inspected) && <p className="review-muted" data-testid="scene-shade-sail-note">Fabric shade sail: inspect the membrane and its corner anchors. The 3D view shows its footprint; curved fabric and suspension heights are not modelled.</p>}
                        {pavedObject && <SceneSurfaceControls object={inspected} scene={data.model} onChange={changeObject}/>}
                        {rooftopObject && <>
                          <label className="scene-placement-toggle">
                            <input type="checkbox" checked={inspected.model.placement !== "absolute"} onChange={e => changeObject({
                              ...inspected,
                              model: { ...inspected.model, placement: e.target.checked ? "roof" : "absolute", height_verified: false },
                            })} />
                            Follow parent roof
                          </label>
                          <p className="review-muted">
                            {placement?.solar ? (placement.mode==="absolute"?"Uses the entered base elevation independently of the parent roof.":"One flat mounting plane above the estimated roof. Thickness is vertical.") : placement?.issue ? "Roof following is unavailable. Check the parent relationship below." : (placement?.mode === "roof"
                              ? "Follows the parent roof slopes and apex edits. Thickness is vertical; roof elevations remain estimates."
                              : placement?.mode === "flat" ? "The parent has no roof slopes. Using its estimated flat top."
                                : "Uses the entered base elevation independently of the parent roof.")}
                          </p>
                          {inspected.kind==="solar_panel"&&placement&&<SceneSolarControls object={inspected} mount={placement} onChange={changeObject}/>}
                        </>}
                        <p className="review-muted">
                          {inspected.model.height_source.replaceAll("-", " ")} ·
                          estimated
                        </p>
                        <label>
                          Surface colour
                          <input
                            type="color"
                            value={inspected.model.color}
                            disabled={busy}
                            onChange={(e) =>
                              changeObject({
                                ...inspected,
                                model: {
                                  ...inspected.model,
                                  color: e.target.value,
                                },
                              })
                            }
                          />
                        </label>
                        <label>
                          Group
                          <select
                            value={inspected.group_id || ""}
                            disabled={busy}
                            onChange={(e) =>
                              changeObject({
                                ...inspected,
                                group_id: e.target.value || undefined,
                              })
                            }
                          >
                            <option value="">Ungrouped</option>
                            {draft.groups.map((g) => (
                              <option key={g.id} value={g.id}>
                                {g.label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <button className="review-button" disabled={busy} onClick={()=>openGroups(inspected.group_id)}>
                          Manage this group
                        </button>
                        <label>
                          Review notes
                          <textarea
                            className="resize-none"
                            value={inspected.notes || ""}
                            maxLength={5000}
                            disabled={busy}
                            rows={3}
                            onChange={(e) =>
                              changeObject({
                                ...inspected,
                                notes: e.target.value,
                              })
                            }
                          />
                        </label>
                        {hasConventionalRoof(inspected) && <details className="scene-roof-proposal">
                          <summary>{modelGraph ? "Try another roof shape" : "Create roof shape"}</summary>
                          <p className="review-muted">Build editable roof planes inside this outline. Gabled roofs have triangular vertical ends; hipped roofs slope down at the ends. The result stays a draft until saved.</p>
                          <label>Starting roof form<select value={roofForm} onChange={e => setRoofForm(e.target.value)}>
                            <option value="auto">Compare imagery</option><option value="gabled">Gabled</option><option value="hipped">Hipped</option><option value="flat">Flat</option>
                          </select></label>
                          <label>Rise above eave (m)<MetricInput value={roofRise} min=".1" max="30" step=".1" disabled={roofForm === "flat"} onValueChange={setRoofRise} /></label>
                          <button className="review-button" type="button" onClick={() => void proposeRoof()}>{roofPending ? "Building proposal…" : "Propose roof shape"}</button>
                          <p className="review-muted">Image evidence may be ambiguous. Heights and pitch are estimates; generated geometry is not an approval.</p>
                        </details>}
                        <button className="review-button" disabled={busy || inspected.status === "rejected"} onClick={()=>{
                          setSelected(inspected.id);setInstanceMode(!instanceMode);setApexMode(false);setMode("review");setView("aerial");setLayer("edited");
                        }}>{instanceMode?"Close outline tools":"Correct outlines"}</button>
                        {inspected.roof_geometry_source && <p className="review-muted">{inspected.roof_geometry_source === "reviewer-template" ? "Template proposal" : "Image-guided proposal"}. {inspected.roof_geometry_note}</p>}
                        {!!modelGraph && (
                          <button
                            className="review-button"
                            onClick={() => {setInstanceMode(false);setRoofEditorMode("apexes");setApexMode(roofEditorMode!=="apexes"||!apexMode);}}
                          >
                            {apexMode && roofEditorMode==="apexes"
                              ? "Close roof points"
                              : "Edit roof apexes"}
                          </button>
                        )}
                        <button
                          className="review-button"
                          disabled={
                            busy ||
                            !data.model.objects.some(
                              (o) => o.id === inspected.id,
                            )
                          }
                          onClick={() => {
                            const original = data.model.objects.find(
                              (o) => o.id === inspected.id,
                            );
                            if (original)
                              change({
                                ...draft,
                                objects: draft.objects.map((o) =>
                                  o.id === original.id
                                    ? structuredClone(original)
                                    : o,
                                ),
                                reviewed_classes: draft.reviewed_classes.filter(
                                  (k) => k !== original.kind,
                                ),
                              });
                          }}
                        >
                          Reset object to prediction
                        </button>
                        <button
                          className="review-button"
                          data-testid="scene-simplify-outline"
                          disabled={busy || inspected.status === "superseded"}
                          title="One Douglas-Peucker step on this outline and its gaps: 1 px per click, then 2 px once 1 px removes nothing. Undo edit restores the outline."
                          onClick={() => {
                            // Decision card item 2 (6 October 2026, "yes to defaults" 7 October): one step per click through changeObject ->
                            // change() (one undo entry), the area change in the message; a superseded object or a result polygonIssues
                            // refuses leaves the outline alone and says so.
                            const result = simplifyOutline(inspected, data.model);
                            if (!result.ok) {
                              setMessage(result.reason);
                              return;
                            }
                            const dropped = changeObject(result.object);
                            setMessage(`${simplifyMessage(result, inspected.label)}${dropped ? ` ${dropped} roof line${dropped === 1 ? "" : "s"} fell outside the simplified outline and ${dropped === 1 ? "was" : "were"} removed from your pending line edits.` : ""}`);
                          }}
                        >
                          Simplify outline
                        </button>
                        <button
                          className="review-button scene-reject"
                          disabled={busy || data.stale || effective?.status === "rejected"}
                          onClick={() =>
                            void persist(
                              { ...draft, objects: draft.objects.map((o) => (o.id === inspected.id ? rejectedObject(o) : o)) },
                              true,
                            )
                          }
                        >
                          Reject detection
                        </button>
                      </>
                    ) : (
                      <p>
                        {layer === "model" && effective ? "This added or replacement object has no original prediction. Choose Your edits to inspect it."
                          : "Choose another layer or review status to keep going. You can add a missed object to the image."}
                      </p>
                    )}
                    <details>
                      <summary>Layer review coverage</summary>
                      <p className="review-muted">
                        Check only after looking for missed objects as well as
                        reviewing proposals.
                      </p>
                      {SCENE_CLASSES.map((k) => (
                        <label className="review-checkbox" key={k}>
                          <input
                            type="checkbox"
                            checked={draft.reviewed_classes.includes(k)}
                            disabled={
                              busy ||
                              draft.objects.some(
                                (o) =>
                                  o.kind === k &&
                                  ["candidate", "needs_adjustment"].includes(
                                    o.status,
                                  ),
                              )
                            }
                            onChange={(e) =>
                              change({
                                ...draft,
                                reviewed_classes: e.target.checked
                                  ? [...draft.reviewed_classes, k]
                                  : draft.reviewed_classes.filter(
                                      (c) => c !== k,
                                    ),
                              })
                            }
                          />
                          {CLASS_NAMES[k]}
                        </label>
                      ))}
                    </details>
                  </fieldset>
                  {inspected&&<SceneRelationships object={inspected} objects={layer==="model"?data.model.objects:draft.objects}
                    frame={data.model.pixel_to_meters} mount={placement} readOnly={layer==="model"||data.stale||inspected.status==="superseded"}
                    busy={busy||instanceMode||roofPending} onChange={changeObject} onInspect={inspectRelated}/>}
                </aside>
              </div>}
              {instanceMode && effective && effective.status !== "superseded" && layer !== "model" && <SceneInstanceEditor
                key={`${id}:${effective.id}`} data={data} review={draft} object={effective} disabled={working||boundaryDrawing||data.stale} onPending={onInstancePending} onDrawingChange={setInstanceDrawing} onClose={()=>setInstanceMode(false)}
                onApply={(review,selected)=>{change(review);setSelected(selected);setRequested(null);setStatus("all");setIssue("all");setQuery("");setInstanceMode(false);setMode("edit");setLayer("edited");
                  setMessage("Replacement outlines are ready for review. The scene is not saved yet.");}}
              />}
              {apexMode && !lineWorkspaceOpen && effective && hasConventionalRoof(effective) && effective.status !== "superseded" && (
                <section className="scene-apex" ref={apexSection}>
                  <div className="roof-line-toolbar"><h2>Roof ridge and apex feedback</h2>
                    <div className="review-segmented" role="group" aria-label="Roof feedback editor">
                      <button aria-pressed={roofEditorMode==="lines"} disabled={busy} onClick={()=>setRoofEditorMode("lines")}>Roof lines</button>
                      <button aria-pressed={roofEditorMode==="apexes"} disabled={busy||!modelGraph} onClick={()=>setRoofEditorMode("apexes")}>3D apexes</button>
                    </div>
                    <button className="review-button" disabled={busy} onClick={()=>setApexMode(false)}>Close roof editor</button>
                  </div>
                  {editorGraph ? <RoofAnnotationEditor
                    imageUrl={reviewImage}
                    ring={effective.ring.map((p) =>
                      transformPoint(p, data.model.pixel_to_meters),
                    )}
                    pixels={effective.ring}
                    holes={effective.holes?.map(h=>h.map(p=>transformPoint(p,data.model.pixel_to_meters)))}
                    key={`${effective.id}:${JSON.stringify(effective.roof_parts)}:${layer}`}
                    model={editorGraph}
                    value={layer === "model" ? editorGraph : effective.roof_annotation || editorGraph}
                    objects={[]}
                    showObjects={false}
                    focusRoofInitially
                    comparison={originalGraph}
                    baselineName={effective.roof_geometry_source ? "proposal" : "model"}
                    mode={layer}
                    disabled={busy || data.stale || layer === "model"}
                    onChange={(annotation) =>
                      changeObject(withObjectRoofAnnotation(effective, annotation))
                    }
                    onObjectsChange={() => {}}
                  /> : <p className="review-notice">The original prediction has no roof surfaces. Choose Your edits to see the proposed roof.</p>}
                </section>
              )}
              {!lineWorkspaceOpen&&<div className="scene-save-bar">
                <span>
                  {busy
                    ? roofLinePending ? "Finish the roof line or press Escape to cancel." : heightEditing ? "Release to keep height; Escape cancels." : instancePending ? "Updating outlines…" : instanceDrawing ? "Finish or cancel the replacement trace." : boundaryDrawing ? "Finish or cancel the interior gap." : roofPending ? "Building roof proposal…" : "Saving…"
                    : roofLinePending ? "Finish the roof line or press Escape to cancel."
                    : dirty
                      ? "Unsaved scene edits"
                      : `Saved revision ${draft.revision}`}
                </span>
                <button
                  className="review-button"
                  disabled={busy || !undo.length}
                  onClick={() => {
                    const previous=undo[undo.length-1];
                    restoreSelection(previous);setDraft(previous);setMessage("Previous draft restored.");
                    setUndo(undo.slice(0, -1));
                  }}
                >
                  Undo edit
                </button>
                <button
                  className="review-button"
                  disabled={busy || !lastDecision || dirty}
                  onClick={() => {
                    if (lastDecision)
                      void persist({
                        ...lastDecision,
                        revision: draft.revision,
                      }).then((ok) => {
                        if (ok) setLastDecision(null);
                      });
                  }}
                >
                  <RotateCcw size={14} />
                  Undo last save
                </button>
                <button
                  className="review-button"
                  disabled={busy || !dirty}
                  onClick={() => {
                    const saved=fresh(data);restoreSelection(saved);setDraft(saved);setMessage("Draft discarded.");
                    setUndo([]);
                    setError("");
                  }}
                >
                  Discard draft
                </button>
                <button
                  className="review-button review-primary"
                  disabled={busy || !dirty}
                  onClick={() => {
                    // Gaps an outline edit left illegal (the editor names them before the save) are dropped here, as on a
                    // decision: the server refuses illegal gaps, and the saved message names each dropped gap with its object.
                    // A save that does not finish puts the reviewer's draft back, gaps included.
                    const notices: string[] = [];
                    const objects = draft.objects.map((o) => {
                      const check = checkOutlineWithGapRepair(o.ring, o.holes, data.model.image_width, data.model.image_height);
                      if (!check.dropped.length || check.issues.length) return o;
                      notices.push(droppedGapsNotice(check.dropped, { label: o.label }));
                      return reanchorRoofLineEdits({ ...o, holes: check.holes }, data.model.pixel_to_meters).object;
                    });
                    if (!notices.length) {
                      void persist(draft);
                      return;
                    }
                    const next = { ...draft, objects };
                    void persist(next, false, `Scene saved. ${notices.join(" ")}`).then((saved) => {
                      if (!saved) setDraft((d) => (d === next ? draft : d));
                    });
                  }}
                >
                  {working ? (
                    <Loader2 size={15} className="animate-spin" />
                  ) : (
                    <Check size={15} />
                  )}
                  Save scene
                </button>
              </div>}
              {!!data.review?.history?.length && (
                <details className="scene-history">
                  <summary>
                    Saved history · {data.review.history.length} revisions
                  </summary>
                  {data.review.history
                    .slice()
                    .reverse()
                    .slice(0, 10)
                    .map((h) => (
                      <p key={h.revision}>
                        Revision {h.revision} ·{" "}
                        {h.updated_at
                          ? new Date(h.updated_at).toLocaleString("en-AU")
                          : ""}{" "}
                        · {h.updated_by}{" "}
                        <button
                          className="review-button"
                          disabled={
                            busy ||
                            h.model_signature !== data.model.model_signature
                          }
                          onClick={() => {const next={...h,revision:draft.revision};restoreSelection(next);change(next);}}
                        >
                          Restore as draft
                        </button>
                      </p>
                    ))}
                </details>
              )}
            </>
          )}
          </>}
        </main>
      </div>
    </div>
  );
}
