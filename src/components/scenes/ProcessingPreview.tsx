"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { Box, ImageIcon, Layers, LoaderCircle } from "lucide-react";
import { roofImageLines, ROOF_LINE_COLORS } from "@/lib/scene-roof-lines";
import { scenePathData } from "@/lib/scene-types";
import type { ProcessingRoofRow, RoofPreviewData } from "@/lib/scene-processing";

const Scene3D = dynamic(() => import("./Scene3D"), { ssr: false, loading: () => <div className="processing-preview-placeholder">Loading the 3D model</div> });
const visible = new Set(["building"]);
const previewCache = new Map<string, RoofPreviewData>();
const noSelect = () => {};

export default function ProcessingPreview({ roof, detail = false }: { roof: ProcessingRoofRow; detail?: boolean }) {
  const p = roof.properties, key = `${p.key}/${p.model_signature}`;
  const [data, setData] = useState<RoofPreviewData | null>(() => previewCache.get(key) || null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState("lines");
  useEffect(() => {
    const cached = previewCache.get(key);
    if (cached) return;
    const controller = new AbortController();
    void fetch(`/api/scenes/preview?${new URLSearchParams({ id: p.scene_id, object: p.object_id, version: p.model_signature })}`, { signal: controller.signal })
      .then(async r => { const result = await r.json(); if (!r.ok) throw new Error(result.error); return result as RoofPreviewData; })
      .then(result => {
        if (controller.signal.aborted) return;
        if (previewCache.size >= 100) previewCache.delete(previewCache.keys().next().value!);
        previewCache.set(key, result); setData(result);
      }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [key, p.scene_id, p.object_id, p.model_signature]);
  const obj = data?.scene.objects[0];
  const ring = obj?.ring || [], xs = ring.map(p => p[0]), ys = ring.map(p => p[1]);
  const left = Math.min(...xs), top = Math.min(...ys), width = Math.max(...xs) - left, height = Math.max(...ys) - top;
  const span = Math.max(width, height) * 1.25;
  const viewBox = `${left + width/2 - span/2} ${top + height/2 - span/2} ${span} ${span}`;
  return <div className={`processing-preview ${detail ? "is-detail" : ""}`}>
    {detail && <div className="processing-preview-tabs" aria-label="Roof preview mode">
      {[["aerial", "Aerial", ImageIcon], ["lines", "Roof lines", Layers], ["3d", "3D", Box]].map(([id, label, Icon]) => {
        const Glyph = Icon as typeof Box;
        return <button type="button" key={id as string} aria-pressed={mode === id} onClick={() => setMode(id as string)}><Glyph size={13}/>{label as string}</button>;
      })}
    </div>}
    {error ? <div className="processing-preview-placeholder" title={error}>{detail ? error : "No preview"}</div> : !data || !obj ? <div className="processing-preview-placeholder"><LoaderCircle size={18} className="processing-spin"/><span className="sr-only">Loading roof preview</span></div> : detail && mode === "3d"
      ? <div className="processing-model"><Scene3D scene={data.scene} objects={data.scene.objects} selected={obj.id} visible={visible} objectIds={new Set([obj.id])} onSelect={noSelect} disabled/></div>
      : <svg viewBox={viewBox} role="img" aria-label={`${mode === "aerial" ? "Aerial image" : "Predicted roof outline and apex lines"}: ${p.label}`}>
        {data.crop && (detail ? data.detail_crop_url : data.crop_url)
          ? <image href={detail ? data.detail_crop_url : data.crop_url} x={data.crop.x} y={data.crop.y} width={data.crop.w} height={data.crop.h} preserveAspectRatio="none"/>
          : <image href={data.image_url} width={data.scene.image_width} height={data.scene.image_height}/>}
        {mode !== "aerial" && <><path d={scenePathData(obj)} fill="#67d7e714" stroke="#67d7e7" strokeWidth={detail ? 1.6 : 1.2} vectorEffect="non-scaling-stroke"/>
          {roofImageLines(obj, data.scene.meters_to_pixel).map((line, i) => <line key={i} x1={line.a[0]} y1={line.a[1]} x2={line.b[0]} y2={line.b[1]} stroke={ROOF_LINE_COLORS[line.kind]} strokeWidth={detail ? 1.6 : 1} strokeDasharray={line.kind === "valley" ? "4 3" : undefined} vectorEffect="non-scaling-stroke"/>)}</>}
      </svg>}
    {detail && <p className="processing-preview-caption">{mode === "3d" ? "Model shape, heights are estimates" : "Original imagery with the model prediction"}</p>}
  </div>;
}
