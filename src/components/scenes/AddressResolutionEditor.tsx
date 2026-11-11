"use client";
import { useEffect, useState } from "react";
import type { BuildingAddress } from "@/lib/building-addresses";

type Decision = { action: string; scope: string; addresses: BuildingAddress["candidates"]; note: string; source_kind: string; at: string };
type Context = { binding: string; revision: number; evidence: BuildingAddress; resolved: BuildingAddress;
  geometry_layer: "saved_edits" | "model";
  decision: Decision | null; decision_stale: boolean; has_saved_local_link: boolean; history_count: number };

export default function AddressResolutionEditor({ sceneId, objectId, onSaved }: {
  sceneId: string; objectId: string; onSaved: (address: BuildingAddress) => void;
}) {
  const [data, setData] = useState<Context | null>(null), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [reload, setReload] = useState(0), [busy, setBusy] = useState(false);
  const [keys, setKeys] = useState<string[]>([]), [scope, setScope] = useState("unit"), [source, setSource] = useState("title_or_site_plan"), [note, setNote] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/scenes/address-resolution?scene=${encodeURIComponent(sceneId)}&object=${encodeURIComponent(objectId)}`, { signal: controller.signal, cache: "no-store" })
      .then(async r => { const d = await r.json(); if (!r.ok) throw new Error(d.error); return d as Context; })
      .then(d => { if (controller.signal.aborted) return; setData(d); setError("");
        setKeys(d.decision && !d.decision_stale ? d.decision.addresses.map(a => `${a.property_pfi}/${a.address_pfi}`) : []);
        setScope(d.decision?.scope || (d.evidence.scope === "shared_roof" ? "shared_roof" : "unit"));
        setNote(d.decision?.note || ""); setSource(d.decision?.source_kind || "title_or_site_plan"); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [sceneId, objectId, reload]);
  const save = async (action: string) => {
    if (!data || busy) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await fetch("/api/scenes/address-resolution", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scene_id: sceneId, object_id: objectId, binding: data.binding, revision: data.revision,
          action, scope, source_kind: source, note, address_keys: keys }) });
      const result = await response.json(); if (!response.ok) throw new Error(result.error);
      setData(result); onSaved(result.resolved); setMessage(action === "clear" ? "Decision cleared; history retained." : "Address decision saved. Roof geometry and approvals are unchanged.");
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save the decision."); }
    finally { setBusy(false); }
  };
  if (!data) return <div>{error ? <p role="alert">{error}</p> : <p>Loading unit and property evidence…</p>}<button onClick={() => setReload(v => v+1)}>Reload evidence</button></div>;
  return <div className="address-resolution-editor">
    <p><small>Address evidence uses {data.geometry_layer === "saved_edits" ? "your saved scene outline. The inventory preview still shows the model." : "the current model outline."}</small></p>
    {data.decision_stale && <p role="status">The previous decision needs rechecking against changed geometry or address records.</p>}
    <p>{data.evidence.next_action || data.evidence.reason}</p>
    {data.evidence.scope === "multi_level" && <p>Floor information is present. A roof image alone cannot locate an upstairs or downstairs unit.</p>}
    <fieldset disabled={busy || data.has_saved_local_link}><legend>Addresses belonging to this building or shared roof</legend>
      <div className="address-resolution-candidates">{data.evidence.candidates.map(a => {
        const key = `${a.property_pfi}/${a.address_pfi}`;
        const allocation = data.evidence.allocations?.find(p => p.property_pfi === a.property_pfi && p.address_pfi === a.address_pfi);
        return <label key={key}><input type="checkbox" checked={keys.includes(key)} onChange={e => setKeys(previous => e.target.checked ? [...previous, key] : previous.filter(k => k !== key))}/>
          <span>{a.full_address}<small>{[a.blg_unit_type, a.floor_type ? `${a.floor_type} ${a.floor_no_1 ?? ""}` : null, a.building_name || a.complex_name,
            allocation ? `${Math.round(allocation.roof_fraction*100)}% of roof overlaps this property` : null].filter(Boolean).join(" · ")}</small></span></label>;
      })}</div>
      {!data.evidence.candidates.length && <p>No authoritative candidates are available. Defer until address evidence is available.</p>}
      <label>Association<select value={scope} onChange={e => setScope(e.target.value)}><option value="unit">Individual unit / tenancy</option><option value="shared_roof">Shared roof across units</option><option value="site">Commercial building / shared site</option></select></label>
      <label>Evidence source<select value={source} onChange={e => setSource(e.target.value)}><option value="title_or_site_plan">Title, floor or site plan</option><option value="verified_entrance">Verified entrance / address signage</option><option value="local_knowledge">Direct local knowledge</option></select></label>
    </fieldset>
    {data.has_saved_local_link && <p>This building already has a saved CRM link. Change that address in the scene editor.</p>}
    <label>Evidence or remaining uncertainty<textarea rows={3} maxLength={2000} value={note} disabled={busy} onChange={e => setNote(e.target.value)} placeholder="Describe the plan, entrance or local knowledge supporting this association…"/></label>
    <p><small>Air conditioners, exhausts and repeated bays can suggest unit patterns; they do not establish addresses or legal walls.</small></p>
    <div className="address-resolution-actions"><button disabled={busy || data.has_saved_local_link || !keys.length || note.trim().length < 10 || (scope === "unit" && keys.length !== 1)} onClick={() => save("confirm")}>Save address association</button>
      <button disabled={busy || note.trim().length < 10} onClick={() => save("defer")}>Needs more evidence</button>
      {!!data.decision && <button disabled={busy || note.trim().length < 10} onClick={() => save("clear")}>Clear decision</button>}
      <button disabled={busy} onClick={() => setReload(v => v+1)}>Reload evidence</button></div>
    <small>Revision {data.revision} · {data.history_count} recorded decisions</small>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
  </div>;
}
