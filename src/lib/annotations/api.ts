import { Struct, type JsonValue } from "@bufbuild/protobuf";
import { Code, ConnectError, createPromiseClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { AnnotationService } from "../../gen/annotation/v1/annotation_connect";
import type { AdapterInfo, Item, Label, Project } from "./model";
import { firebaseToken, recheckRefusedSignIn } from "./firebase";

const tokenKey = "fieldwork.session.token";
const urlKey = "fieldwork.api.url";
let connectionGeneration = 0;
export const getConnectionGeneration = () => connectionGeneration;
export function connectionSettings() {
  if (typeof window === "undefined") return { url: "", token: "" };
  try { return { url: localStorage.getItem(urlKey) ?? "", token: sessionStorage.getItem(tokenKey) ?? "" }; }
  catch { return { url: "", token: "" }; }
}
export function setConnection(url: string, token: string) {
  if (url.trim()) localStorage.setItem(urlKey, url.trim().replace(/\/$/, "")); else localStorage.removeItem(urlKey);
  if (token.trim()) sessionStorage.setItem(tokenKey, token.trim()); else sessionStorage.removeItem(tokenKey);
  connectionGeneration++;
}
function client() {
  const { url, token } = connectionSettings();
  const generation = connectionGeneration;
  return createPromiseClient(AnnotationService, createConnectTransport({
    baseUrl: url || window.location.origin,
    interceptors: [(next) => async (request) => {
      const google = token ? "" : await firebaseToken(url);
      const credential = token || google;
      if (generation !== connectionGeneration) throw new ConnectError("Connection changed. Retry this action.",Code.Canceled);
      if (credential) request.header.set("Authorization", `Bearer ${credential}`);
      if (!google) return next(request);
      try { return await next(request); }
      catch (failure) {
        // The workspace refused the saved Google sign-in. One more look (fresh token, /api/auth/session) tells a passing
        // failure, which retries this request once, from a real refusal (e.g. the account left the allowlist), which signs
        // this device out once so the login page shows with a reason. A 401 means the request was not carried out, so the
        // single retry is safe for saves too. Workspace tokens are never touched.
        const code = ConnectError.from(failure).code;
        if ((code !== Code.Unauthenticated && code !== Code.PermissionDenied) || generation !== connectionGeneration) throw failure;
        const fresh = await recheckRefusedSignIn(url, google).catch(() => "");
        if (!fresh || generation !== connectionGeneration) throw failure;
        request.header.set("Authorization", `Bearer ${fresh}`);
        return next(request);
      }
    }],
  }));
}
type Method = keyof ReturnType<typeof client>;
async function call<T>(method: Method, data: unknown = {}, connection = client()): Promise<T> {
  const result = await connection[method]({ data: Struct.fromJson(JSON.parse(JSON.stringify(data)) as JsonValue) });
  return (result.data?.toJson() ?? {}) as T;
}
export const api = {
  info: async () => {
    const info=await call<{ adapters: AdapterInfo[]; authRequired: boolean; storage: string;workspace?:{id:string;name:string} }>("getInfo");
    // Only a receipt from the authenticated service selects a draft namespace.
    const id=info.workspace?.id;
    if(id==="legacy" || /^ws_[a-f0-9]{32}$/.test(id??""))sessionStorage.setItem("fieldwork.draft.workspace",id!);
    else sessionStorage.removeItem("fieldwork.draft.workspace");
    return info;
  },
  projects: async () => {
    const connection = client();
    const projects: Project[] = []; let cursor = "";
    do { const page = await call<{ projects: Project[]; nextCursor?: string }>("listProjects", { cursor, limit: 500 }, connection); projects.push(...page.projects); cursor = page.nextCursor ?? ""; } while (cursor);
    return { projects };
  },
  saveProject: (project: Partial<Project>) => call<{ project: Project }>("saveProject", { project, expectedRevision: project.revision ?? 0 }),
  // ListItems filters by projectId, cursor, query (literal title substring), status and adapter only; tag queues filter client-side (queue.ts).
  // `tag` filters on the server (case-insensitive); `summary` returns light list entries (open items with item()).
  items: (projectId: string, cursor?: string, query?: string, limit = 20, options: { tag?: string; summary?: boolean } = {}) => call<{ items: Item[]; nextCursor?: string }>("listItems", { projectId, cursor, query, limit, ...(options.tag ? { tag: options.tag } : {}), ...(options.summary ? { summary: true } : {}) }),
  item: (id: string) => call<{ item: Item; labels: Label[]; labelsNextCursor?: string }>("getItem", { id }),
  saveItem: (item: Item, expectedRevision = item.revision) => call<{ item: Item }>("saveItem", { item: { ...item, tags: [...new Set(item.tags.map((tag) => tag.trim()).filter(Boolean))] }, expectedRevision }),
  labels: async (projectId: string) => {
    const connection = client();
    const labels: Label[] = []; let cursor = "";
    do { const page = await call<{ labels: Label[]; nextCursor?: string }>("listLabels", { projectId, cursor, limit: 500 }, connection); labels.push(...page.labels); cursor = page.nextCursor ?? ""; } while (cursor);
    return { labels };
  },
  saveLabel: (label: Partial<Label>) => call<{ label: Label }>("saveLabel", { label, expectedRevision: label.revision ?? 0 }),
  deleteLabel: (label: Label) => call("deleteLabel", { id: label.id, expectedRevision: label.revision }),
  exportProject: async (projectId: string) => {
    // Keep one server for the replay; abort if the connection changes between pages.
    const connection = client();
    try { return await call("exportProject", { projectId }, connection); }
    catch (error) {
      const failure = ConnectError.from(error);
      if (failure.code !== Code.ResourceExhausted && !failure.rawMessage.includes("export exceeds 32 MiB")) throw error;
      const documents = new Map<string, { kind: string; document: unknown }>();
      let cursor = "0", more = true;
      while (more) {
        const page = await call<{ changes: { kind: string; id: string; operation: string; document: unknown }[]; nextCursor: string; hasMore: boolean }>("listChanges", { projectId, cursor, limit: 100 }, connection);
        for (const change of page.changes) {
          const key = `${change.kind}/${change.id}`;
          if (change.operation === "delete") documents.delete(key); else documents.set(key, { kind: change.kind, document: change.document });
        }
        cursor = page.nextCursor; more = page.hasMore;
      }
      const values = [...documents.values()];
      return { schemaVersion: 1, project: values.find((entry) => entry.kind === "project")?.document, items: values.filter((entry) => entry.kind === "item").map((entry) => entry.document), labels: values.filter((entry) => entry.kind === "label").map((entry) => entry.document), cursor };
    }
  },
};
export function apiError(error: unknown): string {
  const failure = ConnectError.from(error);
  if (failure.code === Code.Unauthenticated || failure.code === Code.PermissionDenied) return "Access is required. Open Connection to sign in with Google or enter a workspace access token, then retry.";
  if (failure.code === Code.Aborted || failure.code === Code.AlreadyExists) return "This item changed on the server. Your draft is kept. Download it before loading the latest version to compare.";
  if (failure.code === Code.Unavailable) return "The server could not be reached. Check your connection and retry. Your local draft is kept on this device.";
  return failure.rawMessage || "The request could not be completed. Try again.";
}
