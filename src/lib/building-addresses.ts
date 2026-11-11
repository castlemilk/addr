/** Derived property evidence never substitutes a Vicmap PFI for a local CRM ID. */
export type BuildingAddress = {
  status: "matched" | "linked" | "ambiguous" | "unmatched" | "shared" | "reviewed";
  method: "vicmap-property" | "saved-link" | "unresolved" | "unit-property" | "property-allocation" | "reviewer-evidence";
  full_address?: string; property_pfi?: string; address_id?: number; overlap?: number;
  candidates: { full_address: string; property_pfi: string; address_pfi: string; blg_unit_type?: string; floor_type?: string; floor_no_1?: number; building_name?: string; complex_name?: string }[];
  scope?: string; address_kind?: string; next_action?: string; site_names?: string[];
  site_address?: string | null;
  allocations?: { property_pfi: string; address_pfi: string; roof_fraction: number }[];
  site_context?: BuildingAddress["candidates"];
  decision_revision?: number; decision_state?: "confirmed" | "deferred" | "stale";
  evidence_binding?: string;
  reason: string; fetched_at?: string; dataset_signature?: string;
  source?: { name: string; url: string; attribution: string; license: string };
};
export const addressStatus = (address?: BuildingAddress | null) => address?.status === "linked" ? "Saved address"
  : address?.status === "reviewed" ? "Reviewer-resolved address" : address?.status === "shared" ? "Shared roof · mapped properties"
  : address?.status === "matched" ? address.scope === "unit" ? "Unit property match" : "Property address match"
  : address?.status === "ambiguous" ? "Multiple possible addresses" : "Address unresolved";
export const buildingAddressTitle = (address?: BuildingAddress | null) => address?.full_address ||
  (address?.site_address ? `${address.site_address} · ${address.candidates.length} addresses` : addressStatus(address));
