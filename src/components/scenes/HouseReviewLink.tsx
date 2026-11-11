"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getCrmToken } from "@/lib/crm-client";
import { CLASS_NAMES, type SceneKind } from "@/lib/scene-types";

type HouseScene = { id: string; objects: number; reviewed: number; revision: number; stale: boolean; classes: SceneKind[]; review_url: string };
export default function HouseReviewLink({ addressId, dark }: { addressId: number; dark: boolean }) {
  const router = useRouter(), active = useRef(false), analysisRequest = useRef<AbortController | null>(null);
  useEffect(() => () => analysisRequest.current?.abort(), []);
  const [scene, setScene] = useState<HouseScene | null>(null), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState(""), [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void fetch(`/api/scenes/house?address_id=${addressId}`, { signal: controller.signal }).then(async r => {
      const value = await r.json(); if (!r.ok) throw new Error(value.error); if (!controller.signal.aborted) setScene(value.scene);
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [addressId, retry]);
  async function analyse() {
    if (active.current) return;
    const controller = new AbortController(); analysisRequest.current = controller;
    active.current = true; setBusy(true); setError("");
    try {
      const token = getCrmToken(), response = await fetch("/api/scenes/house", { method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify({ address_id: addressId }) });
      const result = await response.json(); if (!response.ok || !result.scene) throw new Error(result.error || "The scene is not ready. Retry to continue.");
      if (!controller.signal.aborted) router.push(result.scene.review_url);
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Analysis could not finish."); }
    finally { active.current = false; if (!controller.signal.aborted) setBusy(false); }
  }
  const button = `block w-full rounded-lg border px-3 py-2.5 text-left text-xs font-semibold disabled:opacity-50 ${dark ? "border-cyan-800 bg-cyan-950/50 text-cyan-100" : "border-cyan-200 bg-cyan-50 text-cyan-900"}`;
  return <section aria-label="House analysis and training" className="mt-3 space-y-2">
    {scene ? <Link href={scene.review_url} className={button}>Review analysed layers · {scene.objects} objects</Link> : <button className={button} disabled={loading || busy || !!error} onClick={() => void analyse()}>{loading ? "Checking saved analysis…" : busy ? "Analysing house…" : "Analyse house & add to training"}</button>}
    <p className={`text-[11px] leading-relaxed ${dark ? "text-slate-400" : "text-slate-600"}`}>{scene ? `${scene.classes.map(k => CLASS_NAMES[k]).join(", ")} · ${scene.reviewed} decisions saved${scene.stale ? " · New analysis needs feedback recovery" : ""}` : busy ? "Finding outlines and estimating roof shapes. This may take a few minutes." : "Review outlines, roof apexes and objects. Saved corrections return to the map."}</p>
    {error && <p role="alert" className="text-xs text-red-600">{error} <button className="underline" onClick={() => { setError(""); setLoading(true); setRetry(n => n + 1); }}>Check again</button></p>}
  </section>;
}
