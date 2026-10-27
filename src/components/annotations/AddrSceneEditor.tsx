import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Box, Eye, FileSearch, History, Image as ImageIcon, Spline } from "lucide-react";
import { CLASS_COLORS, CLASS_NAMES, SCENE_CLASSES, SCENE_REVIEW_ISSUES, polygonIssues, transformPoint, type SceneModelSnapshot, type SceneObject, type SceneReview } from "../../lib/scene-types";
import { currentRoofLineEdits, estimatedLineGraph } from "../../lib/roof-line-edits";
import { annotationAddr, decideSceneObject, editedItemStatus, focusObjectId, isReviewProject, isVegetationItem, isVegetationLabel, nearlySameGeometry, nextOpenObjectId, objectGeometry, objectLabelIds, outlinePendingFor, reviewFamily, sameSceneFrame, sceneColourKey, sceneColourMode, sceneObjects, sceneOrigin, sceneSource, updateSceneObject, vegetationLabel, vegetationLabelType, vegetationNeedsType, type SceneColourMode, type SceneDecision } from "../../lib/annotations/addr-scene";
import { carriesVegetationType, VEGETATION_TYPE_LABELS, VEGETATION_TYPES } from "../../lib/scene-vegetation";
import { asAsset, useAssetImage, useAssetJson, type NamedAsset } from "../../lib/annotations/assets";
import { isDirty, STATUSES, uid, type Geometry, type Item, type Label, type Position } from "../../lib/annotations/model";
import AnnotationCanvas, { type AnnotationEditorProps } from "./AnnotationCanvas";
import { AssetCollection, SourceState, StructuredData } from "./SourceData";

const Scene3D = lazy(() => import("../scenes/Scene3D"));
const sceneTools = ["select", "pan", "rectangle", "polygon"] as const;
function useScene(item: Item, historyIndex = -1) {
  const source = sceneSource(item);
  const model = useAssetJson<SceneModelSnapshot>(asAsset(source.model));
  const review = useAssetJson<SceneReview>(asAsset(source.review));
  const bound = useAssetJson<SceneModelSnapshot>(asAsset(source.reviewModel));
  const confidence = useAssetJson(asAsset(source.confidence));
  const sourceFrame = bound.data || model.data;
  const history = historyIndex >= 0 ? review.data?.history?.[historyIndex] : undefined;
  const historyRef = history && history.model_signature !== sourceFrame?.model_signature ? source.assets?.find((asset) => asset.path?.endsWith(`/models/${history.model_signature}.json`)) : undefined;
  const historicalModel = useAssetJson<SceneModelSnapshot>(asAsset(historyRef?.ref) || asAsset(historyRef));
  const frame = history && history.model_signature !== sourceFrame?.model_signature ? historicalModel.data : sourceFrame;
  const historicalImage = frame && sourceFrame && frame.image_sha256 !== sourceFrame.image_sha256 ? source.assets?.find((asset) => asset.path?.endsWith(`/${frame.image_sha256}.png`)) : undefined;
  const imageRef = historicalImage ? asAsset(historicalImage.ref) || asAsset(historicalImage) : frame && sourceFrame && frame.image_sha256 !== sourceFrame.image_sha256 ? undefined : asAsset(source.reviewImage || source.image);
  const image = useAssetImage(imageRef);
  const predictionImage = useAssetImage(asAsset(source.image));
  const objects = useMemo(() => sourceFrame ? sceneObjects(item, sourceFrame, review.data) : [], [item, sourceFrame, review.data]);
  const ready = !!model.data && !!frame && !!image.url && (!source.review || !!review.data) && (!source.reviewModel || !!bound.data);
  const missingHistory = history && sourceFrame && history.model_signature !== sourceFrame.model_signature && !historyRef;
  const error = model.error || review.error || bound.error || historicalModel.error || image.error || (missingHistory ? "The preserved review's matching model is not attached. Its exact JSON remains in the source inventory." : !asAsset(source.model) || !asAsset(source.image) || (frame && !imageRef) ? "The scene is missing its protected model or matching image reference." : undefined);
  return { source, model: model.data, review: review.data, frame, image: image.url, predictionImage: predictionImage.url, confidence: confidence.data, confidenceError: confidence.error, objects, ready, error };
}
export default function AddrSceneEditor(props: AnnotationEditorProps) {
  const { item, selectedId, disabled, viewState = {}, onViewStateChange, onItemChange, savedItem, onSaveItem, onNextItem, onSelect } = props;
  const historyIndex = typeof viewState.historyIndex === "number" ? viewState.historyIndex : -1;
  const data = useScene(item, historyIndex);
  const [drawing, setDrawing] = useState(false);
  const [editError, setEditError] = useState("");
  // Colour by class or by project label, remembered per project (localStorage) and shared with the inspector's object list (viewState).
  const colourMode = currentColourMode(item.projectId, viewState);
  const deciding = useRef(false);
  // Focus outline (review queues): once per opened item, when nothing is selected yet, select the agent's proposal (focusObjectId) in
  // the Image view, answered or not; an answered Wrong proposal is hidden by the Rejected / superseded filter, so the filter opens.
  const focusChecked = useRef(false);
  const currentLayer = !viewState.sceneLayer || viewState.sceneLayer === "current";
  useEffect(() => {
    if (focusChecked.current || !data.ready || disabled) return;
    focusChecked.current = true;
    if (selectedId || historyIndex >= 0 || !currentLayer || String(viewState.sceneView || "image") !== "image") return;
    const id = focusObjectId(item, data.objects);
    if (!id) return;
    const target = data.objects.find((object) => object.id === id);
    if (target && ["rejected", "superseded"].includes(target.status) && !viewState.showInactive) onViewStateChange?.({ ...viewState, showInactive: true });
    onSelect(id);
  }, [data.ready, data.objects, disabled, item, selectedId, historyIndex, currentLayer, viewState, onViewStateChange, onSelect]);
  const view = String(viewState.sceneView || "image");
  const layer = String(viewState.sceneLayer || "current");
  const kind = String(viewState.sceneKind || "all");
  const group = String(viewState.sceneGroup || "all");
  const showInactive = !!viewState.showInactive;
  const history = historyIndex >= 0 ? data.review?.history?.[historyIndex] : undefined;
  const state = (next: Record<string, unknown>) => onViewStateChange?.({ ...viewState, ...next });
  const chooseColour = (mode: SceneColourMode) => { try { localStorage.setItem(sceneColourKey(item.projectId), mode); } catch { /* The choice still applies to this item. */ } state({ sceneColour: mode }); };
  if (!data.ready || !data.model || !data.frame || !data.image) return <SourceState error={data.error} />;
  const frame = layer === "prediction" ? data.model : data.frame;
  const bound = !(history || data.review) || (history || data.review)!.model_signature === data.frame.model_signature;
  const comparable = sameSceneFrame(data.frame, data.model);
  const historyBound = !history || history.model_signature === data.frame.model_signature;
  const readonly = disabled || layer !== "current" || !!history || !bound;
  const objects = layer === "prediction" ? data.model.objects : history?.objects || data.objects;
  const filtered = objects.filter((object) => (kind === "all" || object.kind === kind) && (group === "all" || (group === "ungrouped" ? !object.group_id : object.group_id === group)) && (showInactive || !["rejected", "superseded"].includes(object.status)));
  const ids = new Set(filtered.map((object) => object.id));
  const image = layer === "prediction" ? data.predictionImage : data.image;
  const groups = history?.groups || data.review?.groups || data.model.groups;
  const baseline = data.review?.objects || data.frame.objects;
  const edit = (next: SceneObject) => {
    if (readonly) return;
    const issues = polygonIssues(next.ring, data.frame!.image_width, data.frame!.image_height, next.holes);
    if (issues.length) { setEditError(issues.join("; ")); return; }
    setEditError("");
    const original = baseline.find((object) => object.id === next.id) || ({ id: next.id } as SceneObject);
    onItemChange?.(updateSceneObject(item, original, next, data.frame!));
  };
  const updateGeometry = (id: string, geometry: Geometry) => {
    const original = data.objects.find((object) => object.id === id);
    if (!original || geometry.type !== "Polygon") return;
    // A handle released where it was picked up re-encodes the outline with float noise only: not an edit.
    if (nearlySameGeometry(objectGeometry(original, frame), geometry)) return;
    const rings = geometry.coordinates.map((ring) => ring.slice(0, -1).map(([x, y]): Position => [x * frame.image_width, y * frame.image_height]));
    edit({ ...original, ring: rings[0], holes: rings.slice(1), geometry_reviewed: false, status: "needs_adjustment" });
  };
  const add = (geometry: Geometry) => {
    if (geometry.type !== "Polygon" || readonly) return;
    const id = `fieldwork-${uid()}`;
    const rings = geometry.coordinates.map((ring) => ring.slice(0, -1).map(([x, y]): Position => [x * frame.image_width, y * frame.image_height]));
    edit({ id, kind: kind === "all" ? "other" : kind as SceneObject["kind"], label: "New Fieldwork object", ring: rings[0], holes: rings.slice(1), status: "candidate", geometry_reviewed: false, source: "fieldwork-user-feedback", model: { height_m: 0, base_m: 0, height_source: "unestimated", height_verified: false, width_m: 0, length_m: 0, area_m2: 0, rotation_deg: 0, color: "#c4b5fd" } });
    props.onSelect(id);
  };
  // Decision bar. Review queues (project id ending in -review): Image or Roof lines view, editable layer, any selected outline; each
  // answer writes an explicit status + geometry_reviewed (SCENE_DECISIONS) into the outline's Fieldwork overrides, saves the item once
  // and moves on (vegetation items: the next open outline of the item, else the next item). Shadow, cut and join items are answered
  // on the agent's proposal only (focusObjectId): with CURRENT or a PIECE selected the bar says so and answers nothing. Main projects
  // keep the original bar: a building in the Image view, active only once its outline has been edited ("pending", a draft-vs-saved
  // comparison of the ring/holes overrides). The local import policy (feedback.ts) lands either as needs_adjustment +
  // fieldwork_import.pending; the Fieldwork choice survives in overrides and the receipt.
  const reviewQueue = isReviewProject(item.projectId);
  // Review queues answer only the item's own outlines (its annotations); any other scene object in the frame has no answer.
  const answerable = reviewQueue ? new Set(item.annotations.map((mark) => annotationAddr(mark)?.objectId || mark.id)) : undefined;
  const decisionObject = !readonly && (reviewQueue ? view === "image" || view === "lines" : view === "image") ? objects.find((object) => object.id === selectedId && (answerable ? answerable.has(object.id) : object.kind === "building")) : undefined;
  const decisionOriginal = decisionObject && baseline.find((object) => object.id === decisionObject.id);
  const pending = !!decisionObject && outlinePendingFor(item, savedItem, decisionObject.id);
  // The answer this outline already carries in Fieldwork (its status override), shown on the bar when an item is revisited.
  const answered = decisionObject ? annotationAddr(item.annotations.find((mark) => (annotationAddr(mark)?.objectId || mark.id) === decisionObject.id))?.overrides?.status : undefined;
  const vegetationItem = reviewQueue && isVegetationItem(item, data.objects);
  const proposalId = reviewQueue && !vegetationItem ? focusObjectId(item, data.objects) : undefined;
  const proposalObject = proposalId ? data.objects.find((object) => object.id === proposalId) : undefined;
  const offProposal = !!decisionObject && !!proposalId && decisionObject.id !== proposalId;
  const proposalLabel = decisionOriginal?.label ?? decisionObject?.label;
  // Vegetation: "tree or shrub?" and "type?" outlines need a type word before Right means anything (the pull stores nothing for
  // a bare confirm there).
  const typeable = !!decisionObject && reviewQueue && carriesVegetationType(decisionObject.kind) && (isVegetationLabel(decisionObject.label) || isVegetationLabel(decisionOriginal?.label));
  const needsType = vegetationItem && typeable && vegetationNeedsType(proposalLabel, decisionObject.label);
  // Vegetation items move to the next open outline of the same item; others to the next item.
  const nextOutlineId = decisionObject && vegetationItem ? nextOpenObjectId(item, filtered, decisionObject.id) : undefined;
  const moveOn = () => { if (nextOutlineId) onSelect(nextOutlineId); else onNextItem?.(); };
  const decide = async (decision: SceneDecision) => {
    if (!decisionObject || !onSaveItem || readonly || drawing || deciding.current || offProposal) return;
    if (!reviewQueue && !pending) return; // main projects: an edited outline only, as before
    if (decision === "right" && needsType) return;
    deciding.current = true;
    try {
      // In vegetation items Wrong and Not sure replace a picked type: the outline goes back to its proposal label, so the answer is
      // one word, never a type the reviewer has just contradicted.
      const object = vegetationItem && decision !== "right" && vegetationLabelType(decisionObject.label) && proposalLabel !== undefined ? { ...decisionObject, label: proposalLabel } : decisionObject;
      const nextItem = decideSceneObject(item, decisionOriginal || ({ id: decisionObject.id } as SceneObject), object, decision, data.frame!);
      // Same answer as the saved copy and nothing else unsaved: no new revision, just move on.
      if (savedItem && !isDirty(savedItem, nextItem)) { moveOn(); return; }
      if (await onSaveItem(nextItem)) moveOn();
    } finally { deciding.current = false; }
  };
  // Put the selected outline back as saved (review queues: after an accidental or unwanted outline edit).
  const revert = () => {
    if (!decisionObject || readonly || drawing) return;
    const own = (mark: Item["annotations"][number]) => (annotationAddr(mark)?.objectId || mark.id) === decisionObject.id;
    const saved = savedItem?.annotations.find(own);
    const next: Item = { ...item, annotations: saved ? item.annotations.map((mark) => own(mark) ? saved : mark) : item.annotations.filter((mark) => !own(mark)) };
    onItemChange?.(savedItem && !isDirty(savedItem, { ...next, status: savedItem.status }) ? { ...next, status: savedItem.status } : next);
  };
  // Vegetation type picker (review queues only): the type is stored as the Label word the review pull reads
  // (properties.addr.overrides.label = "tree · 0012"); vegetation_type is not a mapped Fieldwork field (feedback.ts). A pick is a
  // draft: the selection stays on the outline, so Right (Enter) then saves this outline with its type and moves on.
  const pickType = (type: string | null) => {
    if (!decisionObject || readonly || drawing) return;
    const proposal = proposalLabel ?? decisionObject.label;
    onItemChange?.(updateSceneObject(item, decisionOriginal || ({ id: decisionObject.id } as SceneObject), { ...decisionObject, label: type ? vegetationLabel(proposal, type) : proposal }, data.frame!));
    // A clicked type button keeps focus, and Enter on it would click it again: hand the keys back to the page.
    const focused = typeof document === "undefined" ? null : document.activeElement;
    if (focused instanceof HTMLElement && focused.closest(".fw-veg-types")) focused.blur();
  };
  // Colour by label: each outline takes its project label colour (annotation.labelId); outlines without one keep the class colour.
  const markLabels = objectLabelIds(item);
  const projectLabels = new Map(props.labels.map((label) => [label.id, label]));
  const labelOf = (object: SceneObject) => colourMode === "label" ? projectLabels.get(markLabels.get(object.id) ?? "") : undefined;
  const colourOf = (object: SceneObject) => labelOf(object)?.color || CLASS_COLORS[object.kind];
  const legend = colourMode === "label" ? labelLegend(objects, labelOf) : [];
  const canvasItem: Item = { ...item, adapter: "image", source: { url: image, width: frame.image_width, height: frame.image_height }, annotations: filtered.map((object) => ({ id: object.id, labelId: labelOf(object) ? `label:${labelOf(object)!.id}` : object.kind, geometry: objectGeometry(object, frame) })) };
  const palette: Label[] = [...SCENE_CLASSES.map((value) => ({ id: value, projectId: item.projectId, name: CLASS_NAMES[value], color: CLASS_COLORS[value], revision: 0 })), ...(colourMode === "label" ? props.labels.map((label) => ({ ...label, id: `label:${label.id}` })) : [])];
  const analysis = [...(data.source.analysisRefs || []), ...(((item.metadata.addr as { analysisRefs?: NamedAsset[] })?.analysisRefs) || [])];
  return <div className="fw-scene-editor">
    <div className="fw-scene-controls"><div className="fw-segmented" aria-label="Scene view">{[{ id: "image", name: "Image", Icon: ImageIcon }, { id: "3d", name: "3D", Icon: Box }, { id: "lines", name: "Roof lines", Icon: Spline }, { id: "evidence", name: "Analysis", Icon: FileSearch }, { id: "history", name: "History", Icon: History }].map(({ id, name, Icon }) => <button key={id} aria-pressed={view === id} disabled={drawing} onClick={() => state({ sceneView: id, ...(id === "evidence" || id === "history" ? { historyIndex: -1 } : {}) })}><Icon size={15} /><span>{name}</span></button>)}</div><select aria-label="Scene layer" disabled={drawing} value={layer} onChange={(event) => state({ sceneLayer: event.target.value, historyIndex: -1 })}><option value="current">Saved feedback + Fieldwork</option><option value="prediction">Original prediction</option><option value="compare" disabled={!comparable}>Compare with prediction</option></select></div>
    <div className="fw-scene-filterbar"><select aria-label="Object class filter" disabled={drawing} value={kind} onChange={(event) => state({ sceneKind: event.target.value })}><option value="all">All classes · {objects.length}</option>{SCENE_CLASSES.map((value) => <option value={value} key={value}>{CLASS_NAMES[value]} · {objects.filter((object) => object.kind === value).length}</option>)}</select><select aria-label="Object group filter" disabled={drawing} value={group} onChange={(event) => state({ sceneGroup: event.target.value })}><option value="all">All groups</option><option value="ungrouped">Ungrouped</option>{groups.map((entry) => <option key={entry.id} value={entry.id}>{entry.label || entry.id}</option>)}</select><label><input type="checkbox" checked={showInactive} disabled={drawing} onChange={(event) => state({ showInactive: event.target.checked })} />Rejected / superseded</label><div className="fw-colour-by" role="group" aria-label="Colour outlines by"><span>Colour by</span>{(["class", "label"] as const).map((mode) => <button key={mode} type="button" data-testid={`fw-colour-${mode}`} aria-pressed={colourMode === mode} onClick={() => chooseColour(mode)}>{mode === "class" ? "Class" : "Label"}</button>)}</div><span>{filtered.length} visible</span></div>
    {legend.length > 0 && (view === "image" || view === "lines") && <div className="fw-scene-legend" data-testid="fw-scene-legend" aria-label="Outline colours by label">{legend.map((entry) => <span key={entry.id || "class"}><i style={entry.color ? { background: entry.color } : undefined} className={entry.color ? undefined : "fw-legend-class"} />{entry.name}<b>{entry.count}</b></span>)}</div>}
    {(!bound || !historyBound) && <div className="fw-scene-warning" role="alert">This review is bound to a different model signature. Geometry editing and overlays are unavailable until its matching source frame is attached.</div>}
    {editError && <div className="fw-scene-warning" role="alert">{editError} The previous geometry is kept.</div>}
    {history && <div className="fw-scene-warning">Inspecting preserved revision {history.revision} · read only <button className="fw-text-button" onClick={() => state({ historyIndex: -1 })}>Return to current feedback</button></div>}
    {view === "evidence" ? <div className="fw-rich-scroll"><section className="fw-source-intro"><h2>Source evidence and analysis</h2><p>These are frozen pipeline observations. Scores, passing audits, and machine decisions remain separate from human review and measured accuracy.</p></section><StructuredData value={data.confidence} label="Confidence evidence" />{data.confidenceError && <p role="alert">{data.confidenceError}</p>}<StructuredData value={data.model} label="Original model and retained analysis" /><AssetCollection assets={analysis} /><AssetCollection assets={data.source.assets || []} title="Other preserved scene assets" /></div>
      : view === "history" ? <div className="fw-rich-scroll"><section className="fw-source-intro"><h2>Review history and provenance</h2><p>Source revisions are immutable. Fieldwork item revision {item.revision} is a separate review layer; it does not change local training eligibility.</p></section><StructuredData value={item.metadata} label="Import provenance" /><div className="fw-history-list">{[data.review, ...(data.review?.history || [])].filter(Boolean).map((revision, index) => <article key={`${revision!.revision}-${index}`}><div><strong>{index ? "Earlier" : "Current source"} revision {revision!.revision}</strong><span>{revision!.updated_at || "Time not recorded"} · {revision!.updated_by || "Actor not recorded"}</span></div><button className="fw-button" disabled={revision!.model_signature !== data.frame!.model_signature && !data.source.assets?.some((asset) => asset.path?.endsWith(`/models/${revision!.model_signature}.json`))} onClick={() => state({ historyIndex: index - 1, sceneLayer: "current", sceneView: "image" })}>{index ? "Inspect image / 3D" : "Open current feedback"}</button><StructuredData value={revision} label="Exact review snapshot" /></article>)}</div>{!data.review && <p>This scene has no saved source review.</p>}</div>
      : !historyBound ? <SourceState error="The selected historical review has a different source frame. Its exact state remains readable in History." />
      : view === "3d" ? <Suspense fallback={<SourceState label="Loading saved 3D renderer…" />}><div className="fw-scene-3d"><Scene3D scene={frame} objects={objects} predictions={layer === "compare" && comparable ? data.model.objects : undefined} visible={new Set(SCENE_CLASSES)} objectIds={ids} selected={selectedId} onSelect={props.onSelect} onChange={readonly ? undefined : edit} onEditingChange={(pending) => { setDrawing(pending); props.onPendingChange(pending); }} disabled={readonly} /></div></Suspense>
      : view === "lines" || layer === "compare" ? <RoofImage image={image || ""} scene={frame} objects={filtered} predictions={layer === "compare" && comparable ? data.model.objects.filter((object) => kind === "all" || object.kind === kind) : undefined} selected={selectedId} onSelect={props.onSelect} colourOf={colourOf} />
      : <AnnotationCanvas {...props} item={canvasItem} labels={palette} disabled={disabled} readOnly={readonly} showAnnotationLabels={false} tool={sceneTools.includes(props.tool as typeof sceneTools[number]) && (!readonly || props.tool === "pan") ? props.tool : "select"} allowedTools={readonly ? ["select", "pan"] : [...sceneTools]} onAdd={add} onUpdate={updateGeometry} onPendingChange={(pending) => { setDrawing(pending); props.onPendingChange(pending); }} />}
    {decisionObject && !reviewQueue && <SceneDecisions pending={pending} disabled={disabled || drawing || !onSaveItem} onDecide={(looksRight) => void decide(looksRight ? "right" : "partly")} />}
    {decisionObject && reviewQueue && (offProposal
      ? <div className="fw-scene-decisions fw-scene-decisions-off" data-testid="fw-scene-decisions" data-review="true" data-off-proposal="true"><p className="fw-decision-target"><strong>{decisionObject.label || decisionObject.id}</strong> is the outline already saved in your scene. Answers go on {proposalObject?.label || "the proposal"}.</p><button type="button" className="fw-button fw-primary" data-testid="fw-select-proposal" onClick={() => onSelect(proposalId!)}>Select {proposalObject?.label || "the proposal"}</button></div>
      : <ReviewDecisions outline={decisionObject.label || decisionObject.id} answered={answered} family={vegetationItem ? "veg" : reviewFamily(item)} pending={pending} held={!!(item.metadata?.addr as { wholeRoof?: unknown } | undefined)?.wholeRoof && !vegetationItem}
        advances={nextOutlineId ? "outline" : onNextItem ? "item" : undefined} disabled={disabled || drawing || !onSaveItem} onDecide={decide} onRevert={revert}
        vegetation={typeable ? { active: vegetationLabelType(decisionObject.label), proposal: proposalLabel ?? decisionObject.label, needsType, onPick: pickType } : undefined} />)}
    <div className="fw-scene-caption"><Eye size={13} /><span>{layer === "prediction" ? "Immutable original prediction" : layer === "compare" ? "Solid: saved feedback · amber: original prediction" : "Source feedback preserved · edits are a separate Fieldwork layer"} · {frame.image_width} × {frame.image_height}px</span></div>
  </div>;
}

function storedValue(key: string) {
  try { return typeof window === "undefined" ? null : localStorage.getItem(key); } catch { return null; }
}
interface LegendEntry { id: string; name: string; color?: string; count: number }
/** Labels present among the objects, most used first; objects without a project label are one "class colour" entry. */
function labelLegend(objects: SceneObject[], labelOf: (object: SceneObject) => Label | undefined): LegendEntry[] {
  const entries = new Map<string, LegendEntry>();
  for (const object of objects) {
    const label = labelOf(object);
    const key = label?.id ?? "";
    const entry = entries.get(key) ?? { id: key, name: label?.name ?? "No label · class colour", color: label?.color, count: 0 };
    entry.count++; entries.set(key, entry);
  }
  return [...entries.values()].sort((a, b) => Number(!a.id) - Number(!b.id) || b.count - a.count || a.name.localeCompare(b.name));
}

/** The colour mode in force: the one chosen on this item (viewState.sceneColour), else the project's remembered choice, else the
 *  default (label in review queues, class elsewhere). The editor and the inspector's object list read the same value. */
function currentColourMode(projectId: string, viewState: Record<string, unknown>): SceneColourMode {
  const chosen = viewState.sceneColour;
  return chosen === "class" || chosen === "label" ? chosen : sceneColourMode(projectId, storedValue(sceneColourKey(projectId)));
}

// Main projects: the original outline decision bar, unchanged. It acts only on an edited building outline.
function SceneDecisions({ pending, disabled, onDecide }: { pending: boolean; disabled: boolean; onDecide: (looksRight: boolean) => void }) {
  const active = pending && !disabled;
  useEffect(() => {
    if (!active) return;
    // Same gate as the scene editor page: Enter / Shift+Enter write a decision only from the page body or a focused
    // vertex handle. The canvas, buttons, inputs and summaries keep their own Enter; the workspace shortcut effect
    // ignores Enter, so nothing else reacts to it.
    const keyboard = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      const onHandle = !!target.closest?.(".fw-vertex-handle");
      if (event.defaultPrevented || event.isComposing || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
      if (target !== document.body && !onHandle) return;
      if (!onHandle && target.closest?.("input,select,textarea,[contenteditable=true],button,summary")) return;
      if (event.key !== "Enter") return;
      event.preventDefault();
      onDecide(!event.shiftKey);
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [active, onDecide]);
  return <div className="fw-scene-decisions" data-testid="fw-scene-decisions" data-outline-pending={pending ? "true" : "false"}>
    <button type="button" className="fw-button" data-testid="fw-outline-needs-work" disabled={!active} title="Stores your outline and keeps the object marked as still needing work (needs adjustment), then opens the next item." onClick={() => onDecide(false)}>Save outline · Still needs work<small>Shift+Enter · then next item</small></button>
    <button type="button" className="fw-button fw-primary" data-testid="fw-outline-looks-right" disabled={!active} title="Stores your outline with your confirmation, then opens the next item." onClick={() => onDecide(true)}>Save outline · Looks right to me<small>Enter · then next item</small></button>
    <p>{pending
      ? "Saved as your outline in Fieldwork. The local review imports it as a pending draft (needs adjustment) until a local reviewer decides; your choice stays in the receipt. Nothing here is a label."
      : "Move an outline point to make a decision available. Enter saves it as looking right to you; Shift+Enter saves it as still needing work."}</p>
  </div>;
}

interface VegetationPicker { active?: string; proposal: string; needsType: boolean; onPick: (type: string | null) => void }
/** What each answer means, per review family, in the words the review pull reads them with. */
const REVIEW_COPY: Record<string, { partly: string; partlyTitle: string; caption: string }> = {
  shadow: { partly: "Partly", partlyTitle: "Partly right: some shadow is still in, and the agent trims again", caption: "Right: the trim is right. Partly: some shadow is still in. Wrong: the trim is wrong." },
  cut: { partly: "Not sure", partlyTitle: "Not sure: nothing is written. Write why in Object notes before you press it", caption: "Right: the fix is right. Not sure: write why in Object notes first, then press it. Wrong: the fix is wrong." },
  join: { partly: "Not sure", partlyTitle: "Not sure: nothing is written. Write why in Object notes before you press it", caption: "Right: the join is right. Not sure: write why in Object notes first, then press it. Wrong: the join is wrong." },
  veg: { partly: "Not sure", partlyTitle: "Not sure: read as unclear", caption: "Right keeps the proposed type, or the type you picked. Not sure is read as unclear. Wrong means it is not that type." },
};
const GENERIC_COPY = { partly: "Partly", partlyTitle: "Partly right: needs work", caption: "Right stores confirmed, Partly stores needs adjustment, Wrong stores rejected." };
// Review queues: three answers on the selected outline, keys Enter (Right), Shift+Enter (Partly / Not sure), X (Wrong) and, on
// vegetation outlines, 1-4 (Grass, Hedge, Shrub, Tree). An edited outline (pending) answers nothing until it is saved as an edit or
// put back.
function ReviewDecisions({ outline, answered, family, pending, held, advances, disabled, onDecide, onRevert, vegetation }: { outline: string; answered?: string; family?: string; pending: boolean; held: boolean; advances?: "outline" | "item"; disabled: boolean; onDecide: (decision: SceneDecision) => void; onRevert: () => void; vegetation?: VegetationPicker }) {
  const copy = (family && REVIEW_COPY[family]) || GENERIC_COPY;
  const veg = family === "veg";
  const active = !disabled && !pending;
  const rightBlocked = !!vegetation?.needsType;
  useEffect(() => {
    if (!active) return;
    // Keys act from the page body, an outline point handle or the image canvas when it is not finishing a drawing (the canvas marks
    // its own Enter as handled). Buttons, inputs, selects and summaries keep their own keys; the workspace tool shortcuts
    // (V H P L R G C) never use X or 1-4.
    const keyboard = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      const onHandle = !!target.closest?.(".fw-vertex-handle");
      const onCanvas = !!target.closest?.(".fw-drawing-surface");
      if (event.defaultPrevented || event.isComposing || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
      if (target !== document.body && !onHandle && !onCanvas) return;
      if (!onHandle && !onCanvas && target.closest?.("input,select,textarea,[contenteditable=true],button,summary")) return;
      const typeIndex = vegetation && !event.shiftKey ? ["1", "2", "3", "4"].indexOf(event.key) : -1;
      if (typeIndex >= 0) { event.preventDefault(); vegetation!.onPick(VEGETATION_TYPES[typeIndex]); return; }
      const decision: SceneDecision | undefined = event.key === "Enter" ? (event.shiftKey ? "partly" : "right") : event.key.toLowerCase() === "x" && !event.shiftKey ? "wrong" : undefined;
      if (!decision) return;
      event.preventDefault();
      if (decision === "right" && rightBlocked) return;
      onDecide(decision);
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [active, onDecide, vegetation, rightBlocked]);
  const then = advances === "outline" ? "then next outline" : advances ? "then next item" : "saves (no next item)";
  const moves = advances === "outline" ? " and selects the next open outline; after the last one it opens the next item" : advances ? " and opens the next item" : "";
  if (pending) return <div className="fw-scene-decisions" data-testid="fw-scene-decisions" data-review="true" data-outline-pending="true">
    <p className="fw-decision-target">Answer for <strong>{outline}</strong></p>
    <button type="button" className="fw-button fw-primary" data-testid="fw-outline-revert" disabled={disabled} onClick={onRevert}>Put the outline back</button>
    {!veg && <button type="button" className="fw-button" data-testid="fw-outline-save-edit" disabled={disabled} title="Saves your moved outline. It is read as edited and goes to a preview; it is not read as right or wrong." onClick={() => onDecide("partly")}>Save my outline · edited<small>{then}</small></button>}
    <p>{veg
      ? "You moved this outline. Outline changes are not read in vegetation items. Put it back to answer this outline."
      : "You moved this outline. Saving it records your new outline as edited (it goes to a preview); it is not read as right or wrong. To answer on the agent's outline, put it back first."}</p>
  </div>;
  const answerWord = answered === "confirmed" ? "Right" : answered === "needs_adjustment" ? copy.partly : answered === "rejected" ? "Wrong" : undefined;
  return <div className="fw-scene-decisions" data-testid="fw-scene-decisions" data-review="true" data-outline-pending="false">
    <p className="fw-decision-target">Answer for <strong>{outline}</strong>{answerWord && <span data-testid="fw-current-answer"> · your answer so far: {answerWord}</span>}</p>
    {vegetation && <div className="fw-veg-types" role="group" aria-label="Vegetation type" data-testid="fw-veg-types"><span>Type</span>{VEGETATION_TYPES.map((type, index) => <button key={type} type="button" className="fw-button" data-testid={`fw-veg-type-${type}`} aria-pressed={vegetation.active === type} disabled={disabled} title={`Key ${index + 1}. Sets this outline's type to ${type} (not saved yet). Then press Right (Enter) to save it.`} onClick={() => vegetation.onPick(type)}>{VEGETATION_TYPE_LABELS[type]}<small>{index + 1}</small></button>)}{vegetation.active && <button type="button" className="fw-text-button" disabled={disabled} onClick={() => vegetation.onPick(null)}>Undo type · back to “{vegetation.proposal}”</button>}</div>}
    <button type="button" className="fw-button fw-decision-wrong" data-testid="fw-outline-wrong" disabled={!active} title={`Wrong. Saves the item${moves}.`} onClick={() => onDecide("wrong")}>Wrong<small>X · {then}</small></button>
    <button type="button" className="fw-button" data-testid="fw-outline-needs-work" disabled={!active} title={`${copy.partlyTitle}. Saves the item${moves}.`} onClick={() => onDecide("partly")}>{copy.partly}<small>Shift+Enter · {then}</small></button>
    <button type="button" className="fw-button fw-primary" data-testid="fw-outline-looks-right" disabled={!active || rightBlocked} title={rightBlocked ? "Pick a type first: Right alone stores nothing for this outline." : `Right. Saves the item${moves}.`} onClick={() => onDecide("right")}>Right<small>Enter · {then}</small></button>
    {rightBlocked && <p className="fw-decision-warning" data-testid="fw-needs-type">Pick a type first (keys 1-4). Right alone stores nothing for this outline.</p>}
    {held && <p className="fw-decision-warning" data-testid="fw-held">Whole-roof copy: Right is recorded but held. Nothing is written until the shape check and your separate whole-roofs choice.</p>}
    <p>{copy.caption} Each answer saves this item once{moves}.{veg ? " A type pick is not saved on its own: press Right after it, or Save & next item. To answer a whole group at once, add a line such as “grass right” to Fieldwork notes." : ""}</p>
  </div>;
}

function RoofImage({ image, scene, objects, predictions, selected, onSelect, colourOf = (object) => CLASS_COLORS[object.kind] }: { image: string; scene: SceneModelSnapshot; objects: SceneObject[]; predictions?: SceneObject[]; selected: string | null; onSelect: (id: string) => void; colourOf?: (object: SceneObject) => string }) {
  const [zoom, setZoom] = useState(1);
  const path = (object: SceneObject) => [object.ring, ...(object.holes || [])].map((ring) => `M ${ring.map((point) => point.join(",")).join(" L ")} Z`).join(" ");
  return <div className="fw-roof-image-wrap"><div className="fw-roof-legend"><span>Ridge <i style={{ background: "#fbbf24" }} /></span><span>Hip <i style={{ background: "#67e8f9" }} /></span><span>Valley <i style={{ background: "#fda4af" }} /></span><button className="fw-button" onClick={() => setZoom(zoom === 1 ? 2 : 1)}>{zoom === 1 ? "Zoom 2×" : "Fit image"}</button></div><div className="fw-roof-image-scroll"><svg viewBox={`0 0 ${scene.image_width} ${scene.image_height}`} style={{ width: `${zoom * 100}%`, height: `${zoom * 100}%` }} role="img" aria-label="Source image, exact footprints and roof linework"><image href={image} width={scene.image_width} height={scene.image_height} />{predictions?.map((object) => <path key={`p-${object.id}`} d={path(object)} fill="none" stroke="#fbbf24" strokeWidth={1.5} strokeDasharray="5 3" vectorEffect="non-scaling-stroke" />)}{objects.map((object) => {
    const graph = currentRoofLineEdits(object) || estimatedLineGraph(object);
    return <g key={object.id} onClick={() => onSelect(object.id)} tabIndex={0} role="button" aria-label={`Select ${object.label || object.id}`} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(object.id); } }}><path d={path(object)} fill={selected === object.id ? "#ffffff20" : "#00000001"} fillRule="evenodd" stroke={selected === object.id ? "#fff" : colourOf(object)} strokeWidth={selected === object.id ? 3 : 1.5} vectorEffect="non-scaling-stroke" />{graph.edges.map((edge, index) => {
      if (!graph.vertices[edge.a] || !graph.vertices[edge.b]) return null;
      const a = transformPoint(graph.vertices[edge.a], scene.meters_to_pixel), b = transformPoint(graph.vertices[edge.b], scene.meters_to_pixel);
      return <line key={index} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={edge.kind === "ridge" ? "#fbbf24" : edge.kind === "valley" ? "#fda4af" : "#67e8f9"} strokeWidth={selected === object.id ? 2.5 : 1.3} vectorEffect="non-scaling-stroke" />;
    })}</g>;
  })}</svg></div></div>;
}

export function AddrSceneInspector(props: AnnotationEditorProps) {
  const { item, selectedId, disabled, onItemChange, viewState = {}, onViewStateChange } = props;
  const data = useScene(item, typeof viewState.historyIndex === "number" ? viewState.historyIndex : -1);
  const [query, setQuery] = useState("");
  const [selectionHint, setSelectionHint] = useState("");
  if (!data.ready || !data.frame || !data.model) return <SourceState error={data.error} />;
  const layer = String(viewState.sceneLayer || "current");
  const history = typeof viewState.historyIndex === "number" && viewState.historyIndex >= 0 ? data.review?.history?.[viewState.historyIndex] : undefined;
  const activeObjects = layer === "prediction" ? data.model.objects : history?.objects || data.objects;
  const object = activeObjects.find((entry) => entry.id === selectedId);
  const original = (layer === "prediction" ? data.model.objects : history?.objects || data.review?.objects || data.frame.objects).find((entry) => entry.id === selectedId);
  const mark = item.annotations.find((entry) => (annotationAddr(entry)?.objectId || entry.id) === selectedId);
  const readonly = disabled || (!!viewState.sceneLayer && viewState.sceneLayer !== "current") || (typeof viewState.historyIndex === "number" && viewState.historyIndex >= 0) || (!!data.review && data.review.model_signature !== data.frame.model_signature);
  const edit = (next: SceneObject) => { if (!readonly) onItemChange?.(updateSceneObject(item, original || ({ id: next.id } as SceneObject), next, data.frame!)); };
  const selectObject = (id: string) => {
    const target = activeObjects.find((entry) => entry.id === id);
    const next = { ...viewState };
    let adjusted = false;
    if (target) {
      if (viewState.sceneKind && viewState.sceneKind !== "all" && viewState.sceneKind !== target.kind) { next.sceneKind = "all"; adjusted = true; }
      const group = String(viewState.sceneGroup || "all");
      if (group !== "all" && (group === "ungrouped" ? !!target.group_id : group !== target.group_id)) { next.sceneGroup = "all"; adjusted = true; }
      if (!viewState.showInactive && ["rejected", "superseded"].includes(target.status)) { next.showInactive = true; adjusted = true; }
    }
    if (adjusted) onViewStateChange?.(next);
    setSelectionHint(adjusted ? "Canvas filters adjusted for this selection." : "");
    props.onSelect(id);
  };
  const objects = activeObjects.filter((entry) => `${entry.label} ${entry.id} ${entry.kind} ${entry.status}`.toLowerCase().includes(query.toLowerCase()));
  // List dots use the canvas colours: project label colours in Label mode, class colours otherwise.
  const markLabels = objectLabelIds(item);
  const labelColours = new Map(props.labels.map((label) => [label.id, label.color]));
  const dotColour = (entry: SceneObject) => (currentColourMode(item.projectId, viewState) === "label" && labelColours.get(markLabels.get(entry.id) ?? "")) || CLASS_COLORS[entry.kind];
  const reviewQueue = isReviewProject(item.projectId);
  const family = reviewQueue ? (isVegetationItem(item, data.objects) ? "veg" : reviewFamily(item)) : undefined;
  return <div className="fw-scene-inspector"><section className="fw-inspector-section"><div className="fw-section-heading"><h3>Scene objects <span>{activeObjects.length}</span></h3></div><input className="fw-scene-search" aria-label="Find source object" placeholder="Find object, class or status…" value={query} onChange={(event) => setQuery(event.target.value)} /><div className="fw-scene-object-list">{objects.map((entry) => <button key={entry.id} className={entry.id === selectedId ? "is-active" : ""} disabled={disabled} onClick={(event) => { selectObject(entry.id); if (event.detail > 0) event.currentTarget.blur(); /* a pointer pick hands the keys back to the page (Enter = Right) */ }}><span className="fw-label-dot" style={{ background: dotColour(entry) }} /><span><strong>{entry.label || entry.id}</strong><small>{entry.kind} · {entry.status.replaceAll("_", " ")}</small></span></button>)}</div>{selectionHint && <p className="fw-field-help" role="status">{selectionHint}</p>}</section>
    {object ? <><section className="fw-inspector-section"><div className="fw-section-heading"><h3>Human object review</h3></div><p className="fw-source-id">{object.id}</p><p className="fw-field-help">Source: {sceneOrigin(original || object)}. Saved review actor: {layer === "prediction" ? "original model prediction" : history?.updated_by || data.review?.updated_by || "not recorded"}. Changes here are Fieldwork feedback and do not grant training eligibility.</p><fieldset disabled={readonly} className="fw-inspector-fields">
        <label className="fw-field">Object review status<select value={object.status} onChange={(event) => edit({ ...object, status: event.target.value as SceneObject["status"] })}>{["candidate", "confirmed", "needs_adjustment", "rejected", "superseded"].map((status) => <option key={status}>{status}</option>)}</select></label>
        <label className="fw-field">Object notes<textarea rows={3} value={object.notes || ""} onChange={(event) => edit({ ...object, notes: event.target.value })} /></label>
        <label className="fw-scene-check"><input type="checkbox" checked={object.geometry_reviewed} onChange={(event) => edit({ ...object, geometry_reviewed: event.target.checked })} />Geometry reviewed in this layer</label>
        <label className="fw-field">Label<input value={object.label} onChange={(event) => edit({ ...object, label: event.target.value })} /></label>
        <label className="fw-field">Class<select value={object.kind} onChange={(event) => edit({ ...object, kind: event.target.value as SceneObject["kind"], geometry_reviewed: false, status: "needs_adjustment" })}>{SCENE_CLASSES.map((kind) => <option key={kind}>{kind}</option>)}</select></label>
        <label className="fw-field">Visibility<select value={object.visibility || ""} onChange={(event) => edit({ ...object, visibility: event.target.value as SceneObject["visibility"] })}><option value="" disabled>Not recorded</option>{["full", "occluded", "truncated", "occluded_truncated"].map((value) => <option key={value}>{value}</option>)}</select></label>
        <div className="fw-scene-issues"><span>Review issues</span>{SCENE_REVIEW_ISSUES.map((issue) => <label key={issue}><input type="checkbox" checked={object.review_issues?.includes(issue) || false} onChange={(event) => edit({ ...object, review_issues: event.target.checked ? [...(object.review_issues || []), issue] : object.review_issues?.filter((value) => value !== issue) })} />{issue.replaceAll("_", " ")}</label>)}</div>
      </fieldset></section>
      <section className="fw-inspector-section"><h3>Geometry and relationships</h3><dl className="fw-facts"><dt>Outer vertices / holes</dt><dd>{object.ring.length} / {object.holes?.length || 0}</dd><dt>Group</dt><dd>{(history?.groups || data.review?.groups || data.model.groups).find((group) => group.id === object.group_id)?.label || object.group_id || "None"}</dd><dt>Physical parent</dt><dd>{object.parent_id ? <button className="fw-text-button" onClick={() => selectObject(object.parent_id!)}>{object.parent_id}</button> : "None"}</dd><dt>Height</dt><dd>{object.model.height_m} m · {object.model.height_verified ? "verified in source" : "unverified estimate"}</dd><dt>Height source</dt><dd>{object.model.height_source}</dd><dt>Source score</dt><dd>{object.score ?? "Not recorded"}</dd><dt>Roof family</dt><dd>{object.roof_type || "Not recorded"}</dd><dt>Roof line feedback</dt><dd>{object.roof_line_edits ? `${object.roof_line_edits.edges.length} lines · ${currentRoofLineEdits(object) ? "outline matches" : "bound to an earlier outline"} · ${object.roof_line_edits.checked ? "checked in source/layer" : "unchecked"}` : "None"}</dd></dl><p className="fw-field-help">Score belongs to the original prediction. Group membership and physical parenthood remain distinct.</p><StructuredData value={object.roof_feedback} label="Saved roof feedback" /><StructuredData value={object.roof_line_edits} label="Saved metric line graph and baseline" /><StructuredData value={{ derived_from: object.derived_from, replacement_ids: object.replacement_ids }} label="Object lineage" /><button className="fw-button fw-full" onClick={() => onViewStateChange?.({ ...viewState, sceneView: "3d" })}>Inspect / edit in 3D</button></section>
      <section className="fw-inspector-section"><StructuredData value={original || object} label="Exact source object" /><StructuredData value={annotationAddr(mark)?.overrides} label="Fieldwork overrides" /></section></> : <section className="fw-inspector-section"><p className="fw-field-help">Select an object in the image, 3D view, or list to inspect its source and review state.</p></section>}
    <section className="fw-inspector-section"><h3>Human scene review</h3>{reviewQueue && <label className="fw-field">Item review status<select data-testid="fw-item-status" disabled={disabled} value={item.status} onChange={(event) => onItemChange?.({ ...item, status: event.target.value as Item["status"] })}>{STATUSES.map((status) => <option key={status.value} value={status.value}>{status.label}</option>)}</select><span className="fw-field-help">{family === "veg" ? "Not read on its own here. Answer each outline, or add a line such as “tree right” to Fieldwork notes." : family === "shadow" || family === "cut" || family === "join" ? "Approved counts as right and Rejected as wrong for this line. It must agree with your answer on the proposal, or nothing is recorded. The answer bar under the picture is the quicker way." : "Your answer for the whole item. It never changes an outline."}</span></label>}<label className="fw-field">Fieldwork notes<textarea rows={3} disabled={disabled} value={item.notes} onChange={(event) => onItemChange?.({ ...item, notes: event.target.value, status: editedItemStatus(item.status, reviewQueue) })} /></label><dl className="fw-facts"><dt>Source review revision</dt><dd>{data.review?.revision ?? "No source review"}</dd><dt>Fieldwork revision</dt><dd>{item.revision}</dd><dt>Frame binding</dt><dd>{(history || data.review) && (history || data.review)!.model_signature !== data.frame.model_signature ? "Mismatch — editing disabled" : "Matching source frame"}</dd></dl><p className="fw-field-help">Image boundaries, holes, classification, visibility and notes are editable. The 3D viewer supports saved height and roof-point edits. Pipeline reruns, source history mutation, new hole drawing, and roof XY graph editing remain in the source application.</p></section>
  </div>;
}
