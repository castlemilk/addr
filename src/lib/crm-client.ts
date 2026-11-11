import { Interceptor, createPromiseClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { CrmService } from "@/gen/crm/v1/crm_connect";

const TOKEN_KEY = "addr.crm.token";

export function getCrmToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setCrmToken(token: string): void {
  if (typeof window === "undefined") return;
  if (token.trim()) {
    window.localStorage.setItem(TOKEN_KEY, token.trim());
  } else {
    window.localStorage.removeItem(TOKEN_KEY);
  }
}

const authInterceptor: Interceptor = (next) => async (request) => {
  const token = getCrmToken();
  if (token) request.header.set("Authorization", `Bearer ${token}`);
  return next(request);
};

const transport = createConnectTransport({
  baseUrl: process.env.NEXT_PUBLIC_GEO_SERVICE_URL || "http://localhost:8085",
  interceptors: [authInterceptor],
});

export const crmClient = createPromiseClient(CrmService, transport);

/** Boundary drawing modes. "box" is a drag-a-rectangle roof proposal saved as
 *  an OBJECT feature labelled "roof-box proposal" (feedback for the review
 *  loop, not a committed roof override). */
export type CrmDrawKind = "roof" | "tree" | "object" | "box";

export const ROOF_BOX_LABEL = "roof-box proposal";

export const CRM_SCOPE_LABELS: Record<string, string> = {
  "crm.read": "View CRM",
  "crm.address.write": "Edit address metadata",
  "crm.roof.write": "Edit roof boundaries",
  "crm.tree.write": "Edit tree boundaries",
  "crm.object.write": "Edit other objects",
  "crm.poi.write": "Edit POIs",
  "crm.users.read": "Manage users",
  "crm.admin": "Administrator",
};

export function bboxPolygonToGeoJson(ring: [number, number][]): string {
  return JSON.stringify({
    type: "Polygon",
    coordinates: [[...ring, ring[0]]],
  });
}
