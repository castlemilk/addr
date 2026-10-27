import type { ComponentType } from "react";
import { Box, Globe2, ImagePlus, BarChart3, type LucideIcon } from "lucide-react";
import AnnotationCanvas, { type AnnotationEditorProps } from "../../components/annotations/AnnotationCanvas";
import AddrSceneEditor, { AddrSceneInspector } from "../../components/annotations/AddrSceneEditor";
import AddrReportEditor, { AddrReportInspector } from "../../components/annotations/AddrReportEditor";
import type { Item } from "./model";

export interface AdapterField {
  name: string; label: string; type: "url" | "number" | "text";
  required?: boolean; min?: number; max?: number; defaultValue?: string | number; placeholder?: string; help?: string;
}
/** Register a viewer and its source form without changing the workspace.
 * The service must register the same adapter id and validate its source schema.
 * Persisted item metadata and source extensions pass through unchanged. */
export interface AnnotationAdapter {
  id: string; name: string; description: string; Icon: LucideIcon;
  mediaKind?: "image" | "map" | "report";
  fields: AdapterField[];
  makeSource: (data: FormData) => Item["source"];
  Editor: ComponentType<AnnotationEditorProps>;
  Inspector?: ComponentType<AnnotationEditorProps>;
  canCreate?: boolean;
}
const adapters = new Map<string, AnnotationAdapter>();
export function registerAnnotationAdapter(adapter: AnnotationAdapter) { adapters.set(adapter.id, adapter); }
export function getAnnotationAdapter(id: string) { return adapters.get(id); }
export function getAnnotationAdapters() { return [...adapters.values()]; }

registerAnnotationAdapter({
  id: "image", name: "Image", mediaKind: "image", description: "Annotate an image URL", Icon: ImagePlus, Editor: AnnotationCanvas,
  fields: [{ name: "url", label: "Image URL", type: "url", required: true, placeholder: "https://example.com/image.jpg", help: "Use a direct, publicly accessible image URL. The original stays unchanged." }],
  makeSource: (data) => ({ url: String(data.get("url")).trim() }),
});
registerAnnotationAdapter({
  id: "map", name: "Map area", mediaKind: "map", description: "Mark a place in the world", Icon: Globe2, Editor: AnnotationCanvas,
  fields: [
    { name: "longitude", label: "Longitude", type: "number", min: -180, max: 180, defaultValue: 0, required: true },
    { name: "latitude", label: "Latitude", type: "number", min: -85, max: 85, defaultValue: 20, required: true },
    { name: "zoom", label: "Starting zoom", type: "number", min: 1, max: 19, defaultValue: 3, required: true, help: "Move the map and choose ‘Use this view’ to update it." },
  ],
  makeSource: (data) => ({ center: [Number(data.get("longitude")), Number(data.get("latitude"))], zoom: Number(data.get("zoom")) }),
});
registerAnnotationAdapter({ id: "addr-scene", name: "Scene review", mediaKind: "image", description: "Source-bound imagery, objects and 3D", Icon: Box, fields: [], makeSource: () => ({}), canCreate: false, Editor: AddrSceneEditor, Inspector: AddrSceneInspector });
registerAnnotationAdapter({ id: "addr-report", name: "Dataset report", mediaKind: "report", description: "Preserved processing and geographic evidence", Icon: BarChart3, fields: [], makeSource: () => ({}), canCreate: false, Editor: AddrReportEditor, Inspector: AddrReportInspector });
