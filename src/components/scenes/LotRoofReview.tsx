"use client";

import { useState } from "react";
import Link from "next/link";
import type { SceneModelSnapshot } from "@/lib/scene-types";

type Polygons = number[][][][];
type Lot = { pfi: string; lot: string; status: string; reason: string; polygons: Polygons; parcel_polygons: Polygons;
  roof_edges?: number[][][]; allocation_edges?: number[][][]; uncertain_polygons?: Polygons;
  horizontal_uncertainty_m: number | null; area_m2?: number; lot_coverage_fraction?: number;
  parent_object_ids: string[]; boundary_fits?: { accepted: boolean }[]; context_boundary_fits?: { accepted: boolean }[];
  crop_polygons?: Polygons; boundary_strategy?: string; scale_comparison?: { lost_context_fraction: number } };
export type LotReview = { status: string; reason: string; lots: Lot[] };
const statuses: Record<string, string> = { needs_review: "Needs review", not_run: "Search prepared", no_roof_evidence: "No roof evidence", needs_context: "Needs more context", failed: "Search failed" };
const polygonPath = (polygons: Polygons) => polygons.map(p => p.map(r => `M${r.map(x => x.join(",")).join("L")}Z`).join(" ")).join(" ");

export default function LotRoofReview({ review, scene, imageUrl, enhanced }: { review: LotReview; scene: SceneModelSnapshot; imageUrl: string; enhanced: boolean }) {
  const [selected, setSelected] = useState(review.lots[0]?.pfi || "all");
  const [showUncertainty, setShowUncertainty] = useState(false);
  const [showCrop, setShowCrop] = useState(false);
  const lots = selected === "all" ? review.lots : review.lots.filter(lot => lot.pfi === selected);
  const points = lots.flatMap(lot => lot.parcel_polygons.flat(2)).filter(p => p.length === 2 && p.every(Number.isFinite));
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const span = Math.max(Math.max(...xs)-Math.min(...xs), Math.max(...ys)-Math.min(...ys))*1.25;
  const hasFrame = points.length >= 3 && Number.isFinite(span) && span > 0;
  const viewBox = hasFrame ? `${(Math.max(...xs)+Math.min(...xs)-span)/2} ${(Math.max(...ys)+Math.min(...ys)-span)/2} ${span} ${span}` : `0 0 ${scene.image_width} ${scene.image_height}`;
  return <section className="lot-roof-review" aria-label="Parcel-conditioned roof review">
    <h4>Roof search by lot</h4><p>{review.reason}</p>
    {review.status === "partial" && <p role="status">Some lot searches need more evidence or another processing pass. Completed results remain available below.</p>}
    <div className="self-review-tools" aria-label="Choose a cadastral lot">
      {review.lots.map(lot => <button key={lot.pfi} aria-pressed={selected === lot.pfi} onClick={() => setSelected(lot.pfi)}>Lot {lot.lot}</button>)}
      <button aria-pressed={selected === "all"} onClick={() => setSelected("all")}>All lots</button>
    </div>
    <p className="lot-roof-legend"><span>Orange: cadastral lot</span><span>Cyan: detected roof edge</span><span>Purple dashed: allocation cut</span></p>
    {!hasFrame && <p role="status">This lot has no usable registered boundary. Its roof allocation is withheld.</p>}
    <div className="self-review-comparison">{[false, true].map(overlay => <div key={String(overlay)}>
      <h4>{overlay ? "Roof allocation · not applied" : "Original imagery + lot"}</h4>
      <svg viewBox={viewBox} role="img" aria-label={(overlay ? "Roof allocation" : "Source image") + " for " + (selected === "all" ? "all lots" : "lot " + lots[0]?.lot)}>
        <image href={imageUrl+(enhanced ? "&view=roof-detail-v1" : "")} width={scene.image_width} height={scene.image_height}/>
        {lots.map(lot => <g key={lot.pfi}>
          <path d={polygonPath(lot.parcel_polygons)} fill="none" stroke="#fbbf24" strokeWidth="1.5" strokeDasharray="7 5" vectorEffect="non-scaling-stroke"/>
          {overlay && <>
            <path d={polygonPath(lot.polygons)} fill="#67d7e7" fillOpacity=".12" fillRule="evenodd"/>
            {showUncertainty && <path d={polygonPath(lot.uncertain_polygons || [])} fill="#fbbf24" fillOpacity=".25" fillRule="evenodd"/>}
            {showCrop && <path d={polygonPath(lot.crop_polygons || [])} fill="none" stroke="#fb923c" strokeWidth="1.5" vectorEffect="non-scaling-stroke"/>}
            {lot.roof_edges?.map((line, i) => <polyline key={`edge-${i}`} points={line.map(p => p.join(",")).join(" ")} fill="none" stroke="#67d7e7" strokeWidth="2" vectorEffect="non-scaling-stroke"/>)}
            {lot.allocation_edges?.map((line, i) => <polyline key={`cut-${i}`} points={line.map(p => p.join(",")).join(" ")} fill="none" stroke="#d8b4fe" strokeWidth="2" strokeDasharray="5 4" vectorEffect="non-scaling-stroke"/>)}
          </>}
        </g>)}
      </svg>
    </div>)}</div>
    <div className="self-review-tools"><label><input type="checkbox" checked={showUncertainty} onChange={e => setShowUncertainty(e.target.checked)}/>Show roof evidence outside the lot in its uncertainty margin</label>
      <label><input type="checkbox" checked={showCrop} onChange={e => setShowCrop(e.target.checked)}/>Compare close-crop SAM outlines (orange)</label></div>
    <p className="processing-evidence-note">Lot numbers identify cadastral parcels, not confirmed unit addresses. Dashed allocation cuts do not create roof folds or establish wall positions. Roof slope and height remain unresolved.</p>
    {lots.map(lot => <div className="lot-roof-result" key={lot.pfi}>
      <div><strong>Lot {lot.lot} · {statuses[lot.status] || lot.status}</strong>
        {lot.area_m2 !== undefined && <p>{lot.area_m2.toFixed(1)} m² of estimated roof within this lot · {Math.round((lot.lot_coverage_fraction || 0)*100)}% of its search area</p>}
        <p>{lot.reason}</p>{lot.boundary_strategy === "context-retained" && <p>The close crop omitted {Math.round((lot.scale_comparison?.lost_context_fraction || 0)*100)}% of the wider model’s roof area in this lot. Its result has not replaced that outline.</p>}
        <small>Parcel position uncertainty: {lot.horizontal_uncertainty_m === null ? "unavailable" : `${lot.horizontal_uncertainty_m.toFixed(2)} m`}. {(lot.boundary_strategy === "context-retained" ? lot.context_boundary_fits : lot.boundary_fits)?.some(f => f.accepted) ? "A bounded straight-edge fit passed the image checks." : "No extra straight-edge fit accepted."}</small>
      </div>
      <div className="self-review-tools">{lot.parent_object_ids.map(id => <Link key={id} className="processing-secondary" href={`/scenes?${new URLSearchParams({scene: scene.id, object: id, kind: "building", status: "all", layer: "edited"})}`}>Edit source roof · {id.slice(0, 8)}</Link>)}</div>
    </div>)}
  </section>;
}
