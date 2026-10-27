/* eslint-disable @next/next/no-img-element */
import { useEffect, useState } from "react";
import { FileJson, LoaderCircle } from "lucide-react";
import { asAsset, assetBytes, useAssetImage, useAssetJson, type AssetRef, type NamedAsset } from "../../lib/annotations/assets";

export function SourceState({ error, label = "Loading verified source…" }: { error?: string; label?: string }) {
  return <div className="fw-source-state" role={error ? "alert" : "status"}>{error ? <><strong>Source unavailable</strong><p>{error}</p><p>Its saved reference is preserved. Reopen the item after checking the connection.</p></> : <><LoaderCircle size={20} className="fw-spin" /><span>{label}</span></>}</div>;
}
export function StructuredData({ value, label = "Source data", depth = 0 }: { value: unknown; label?: string; depth?: number }) {
  const [open, setOpen] = useState(depth === 0);
  const [limit, setLimit] = useState(40);
  const asset = asAsset(value);
  if (asset) return <details className="fw-data-node" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>{label} <small>Verified asset</small></summary>{open && <AssetPreview asset={asset} />}</details>;
  if (value === null || typeof value !== "object") return <div className="fw-data-value"><span>{label.replaceAll("_", " ")}</span><strong>{value === null ? "Not available (null)" : value === undefined ? "Not recorded" : String(value)}</strong></div>;
  const entries = Object.entries(value);
  return <details className="fw-data-node" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}><summary>{label.replaceAll("_", " ")} <small>{entries.length} {Array.isArray(value) ? "entries" : "fields"}</small></summary>{open && <div className="fw-data-children">{entries.slice(0, limit).map(([key, entry]) => <StructuredData key={key} value={entry} label={key} depth={depth + 1} />)}{entries.length > limit && <button className="fw-button" onClick={() => setLimit((count) => count + 100)}>Show next {Math.min(100, entries.length - limit)} entries</button>}</div>}</details>;
}
export function AssetPreview({ asset }: { asset: AssetRef }) {
  const type = asset.contentType || asset.mimeType || "application/json";
  return /image\//.test(type) ? <SourceImage asset={asset} /> : /json/.test(type) ? <SourceJson asset={asset} /> : /text\//.test(type) ? <SourceText asset={asset} /> : <p className="fw-field-help">Binary source preserved ({type}, {asset.bytes?.toLocaleString() || "unknown"} bytes). Use the snapshot restore command for this format. Derived scene and analysis views remain available in this workspace.</p>;
}
function SourceText({ asset }: { asset: AssetRef }) {
  const [state, setState] = useState<{ text?: string; error?: string }>({});
  useEffect(() => { let live = true; assetBytes(asset).then((bytes) => { if (live) setState({ text: new TextDecoder().decode(bytes) }); }, (error: unknown) => { if (live) setState({ error: error instanceof Error ? error.message : "Source could not load." }); }); return () => { live = false; }; }, [asset]);
  return state.text === undefined ? <SourceState error={state.error} /> : <pre className="fw-source-text">{state.text}</pre>;
}
function SourceImage({ asset }: { asset: AssetRef }) {
  const state = useAssetImage(asset);
  return state.url ? <img className="fw-source-image" src={state.url} alt="Preserved source evidence" /> : <SourceState error={state.error} />;
}
function SourceJson({ asset }: { asset: AssetRef }) {
  const state = useAssetJson(asset);
  return state.data !== undefined ? <StructuredData value={state.data} label="Stored contents" /> : <SourceState error={state.error} />;
}
export function AssetCollection({ assets, title = "Preserved analysis" }: { assets: NamedAsset[]; title?: string }) {
  const [selection, setSelection] = useState<number | null>(null);
  const selected = selection === null ? undefined : assets[selection];
  const ref = selected && (asAsset(selected.ref) || asAsset(selected));
  return <section className="fw-source-collection"><h3>{title}</h3>{assets.length ? <><div className="fw-asset-list">{assets.map((asset, index) => <button key={`${asset.path || asset.title}-${index}`} className={index === selection ? "is-active" : ""} onClick={() => setSelection(index)}><FileJson size={14} /><span>{asset.title || asset.path || `Source ${index + 1}`}</span><small>{asset.kind}</small></button>)}</div>{ref && <AssetPreview key={ref.sha256} asset={ref} />}</> : <p className="fw-field-help">No additional analysis asset is attached to this item.</p>}</section>;
}
