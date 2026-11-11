"use client";
import { useState } from "react";
import { addressStatus, type BuildingAddress } from "@/lib/building-addresses";
import AddressResolutionEditor from "./AddressResolutionEditor";

export default function BuildingAddressSummary({ address: original, sceneId, objectId, disabled = false }: { address?: BuildingAddress | null; sceneId?: string; objectId?: string; disabled?: boolean }) {
  const [open, setOpen] = useState(false), [saved, setSaved] = useState<BuildingAddress | null>(null);
  const address = saved && saved.evidence_binding === original?.evidence_binding && (saved.decision_revision || 0) >= (original?.decision_revision || 0) ? saved : original;
  return <section className="building-address-summary" aria-label="Building address evidence">
    <strong>{addressStatus(address)}</strong>
    {address?.status === "reviewed" && address.full_address && <p>{address.full_address}</p>}
    <p>{address?.reason || "Address evidence is unavailable for this building."}</p>
    {!!address?.site_names?.length && <p>{address.site_names.join(", ")}</p>}
    {address?.decision_state === "stale" && <p>Previous address decision needs rechecking.</p>}
    {address?.decision_state === "deferred" && <p>Address decision deferred; more evidence is needed.</p>}
    {address?.next_action && <p>{address.next_action}</p>}
    {(address?.status === "ambiguous" || address?.status === "shared" || address?.status === "reviewed" && address.candidates.length > 1) && <details><summary>View {address.candidates.length} associated or possible addresses</summary>
      <ul>{address.candidates.map(a => <li key={`${a.property_pfi}/${a.address_pfi}`}>{a.full_address}</li>)}</ul>
    </details>}
    {!!address?.site_context?.length && <details><summary>Enclosing site address</summary><ul>{address.site_context.map(a => <li key={`${a.property_pfi}/${a.address_pfi}`}>{a.full_address}</li>)}</ul></details>}
    {address?.source && <small><a href={address.source.url} target="_blank" rel="noreferrer">{address.source.name}</a>
      {", "}{address.source.attribution}{", "}{address.source.license}.
      {address.fetched_at && <> Retrieved {new Date(address.fetched_at).toLocaleDateString("en-AU")}.</>}
    </small>}
    {sceneId && objectId && <><button className="review-button" disabled={disabled} onClick={() => setOpen(v => !v)} aria-expanded={open && !disabled}>{open ? "Close address resolver" : "Resolve units / site"}</button>
      {disabled && <p>Save or discard outline changes before resolving the address.</p>}
      {open && !disabled && <AddressResolutionEditor key={`${sceneId}/${objectId}/${original?.evidence_binding}`} sceneId={sceneId} objectId={objectId} onSaved={setSaved}/>}</>}
  </section>;
}
