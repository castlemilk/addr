import type { SceneObject } from "./scene-types";
import type { BuildingAddress } from "./building-addresses";

export type AddressNames = Record<string, string>;
export type SceneProperty = { address_id: number; object_id: string; title: string; has_feedback: boolean };
export type AddressCandidate = { address_id: number; full_address: string };
export type AddressMatch = {
  object_id: string; status: "linked" | "suggested" | "ambiguous" | "unmatched";
  method: "saved-link" | "parcel" | "address-point" | "unresolved";
  address_id?: number; full_address?: string; parcel_pfi?: string;
  candidates: AddressCandidate[]; reason: string;
};
export type SceneAddressEvidence = {
  model_signature: string; revision: number; objects: AddressMatch[];
  property_addresses?: Record<string, BuildingAddress>;
  parcel_status: "available" | "unavailable"; parcel_source?: string;
};
export const activeBuilding = (o: SceneObject) => o.kind === "building" && o.status !== "rejected" && o.status !== "superseded";
export function sceneProperties(objects: SceneObject[], names: AddressNames = {}): SceneProperty[] {
  return objects.filter(o => activeBuilding(o) && Number.isSafeInteger(o.address_id) && o.address_id! > 0).map(o => ({
    address_id: o.address_id!, object_id: o.id,
    title: names[o.address_id!] || o.label,
    has_feedback: !!o.roof_line_edits || !!o.roof_feedback || o.geometry_reviewed,
  }));
}
export function propertyTitle(properties: SceneProperty[]): string | undefined {
  return properties.length && new Set(properties.map(p => p.address_id)).size === 1 ? properties[0].title.split(",")[0].split(" · ")[0] : undefined;
}

/** Feedback associations win; a dedicated capture outranks an unreviewed imported association. */
export function rankAddressScenes<T extends { id: string; address_id: number | null; properties: SceneProperty[]; stale: boolean }>(scenes: T[], addressId: number): T[] {
  const links = (s: T) => s.properties.filter(p => p.address_id === addressId);
  const score = (s: T) => links(s).some(p => p.has_feedback) ? 30 : s.id === `house-${addressId}` ? 20 : links(s).length ? 10 : 0;
  return scenes.filter(s => links(s).length || s.address_id === addressId).sort((a,b) =>
    score(b)-score(a) || Number(a.stale)-Number(b.stale) || Number(b.id === `house-${addressId}`)-Number(a.id === `house-${addressId}`) || a.id.localeCompare(b.id));
}
