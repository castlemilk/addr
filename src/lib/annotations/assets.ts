import { useEffect, useState } from "react";
import { connectionSettings, getConnectionGeneration } from "./api";
import { firebaseToken } from "./firebase";

export interface AssetRef { url?: string; sha256: string; bytes?: number; contentType?: string; mimeType?: string; chunks?: AssetRef[] }
export interface NamedAsset { title?: string; path?: string; kind?: string; ref?: AssetRef; url?: string; sha256?: string; contentType?: string }
export const asAsset = (value: unknown): AssetRef | undefined => {
  if (!value || typeof value !== "object") return;
  const ref = value as AssetRef;
  return (typeof ref.url === "string" || Array.isArray(ref.chunks)) && /^[a-f0-9]{64}$/.test(ref.sha256) ? ref : undefined;
};
const pending = new Map<string, Promise<ArrayBuffer>>();
let cacheScope = "";

/** Only send the session credential to the configured API's protected asset route. */
export async function assetBytes(ref: AssetRef): Promise<ArrayBuffer> {
  if (!ref.url && ref.chunks?.length) {
    if ((ref.bytes || 0) > 64 * 1024 * 1024) throw new Error("This preserved file exceeds the 64 MB browser preview limit. Reproduce it with the snapshot restore command.");
    const buffers: ArrayBuffer[] = [];
    for (const chunk of ref.chunks) buffers.push(await assetBytes({ ...chunk, url: chunk.url || `/api/v1/assets/${chunk.sha256}` }));
    const joined = new Uint8Array(buffers.reduce((length, bytes) => length + bytes.byteLength, 0));
    let offset = 0; for (const buffer of buffers) { joined.set(new Uint8Array(buffer), offset); offset += buffer.byteLength; }
    const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", joined))].map((value) => value.toString(16).padStart(2, "0")).join("");
    if (hash !== ref.sha256 || (ref.bytes !== undefined && offset !== ref.bytes)) throw new Error("The reconstructed file does not match its saved content hash or length.");
    return joined.buffer;
  }
  const { url, token: workspaceToken } = connectionSettings();
  const generation = getConnectionGeneration();
  const base = new URL(url || window.location.origin);
  const address = new URL(ref.url || "", base);
  if (address.origin !== base.origin || address.pathname !== `/api/v1/assets/${ref.sha256}` || address.search || address.hash) throw new Error("This source is not a protected asset on the connected server.");
  const token = workspaceToken || await firebaseToken(url);
  if (generation !== getConnectionGeneration()) throw new Error("Connection changed. Retry loading this source.");
  const scope = `${base.origin}\n${token}`;
  if (cacheScope !== scope) { pending.clear(); cacheScope = scope; }
  const key = address.href;
  let request = pending.get(key);
  if (!request) {
    request = (async () => {
      const response = await fetch(address, { headers: token ? { Authorization: `Bearer ${token}` } : {}, cache: "no-store", redirect: "error" });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? "Access to this source was denied. Sign in again through Connection." : `Source could not load (HTTP ${response.status}).`);
      const bytes = await response.arrayBuffer();
      const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map((value) => value.toString(16).padStart(2, "0")).join("");
      if (hash !== ref.sha256) throw new Error("Source integrity check failed. The downloaded bytes do not match the saved SHA-256.");
      if (ref.bytes !== undefined && bytes.byteLength !== ref.bytes) throw new Error("Source length does not match the saved asset reference.");
      return bytes;
    })();
    pending.set(key, request);
    if (pending.size > 20) pending.delete(pending.keys().next().value!);
    request.catch(() => pending.delete(key));
  }
  return request;
}
export async function assetJson<T = unknown>(ref: AssetRef): Promise<T> {
  return JSON.parse(new TextDecoder().decode(await assetBytes(ref))) as T;
}
export function useAssetJson<T = unknown>(ref?: AssetRef) {
  const [state, setState] = useState<{ key?: string; data?: T; error?: string }>({});
  const key = ref ? `${connectionSettings().url}/${ref.sha256}/${getConnectionGeneration()}` : undefined;
  useEffect(() => {
    if (!ref) return;
    let live = true;
    assetJson<T>(ref).then((data) => { if (live) setState({ key, data }); }, (error: unknown) => { if (live) setState({ key, error: error instanceof Error ? error.message : "Source could not load." }); });
    return () => { live = false; };
    // Asset identity is its content hash; immutable refs never change in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state.key === key ? state : {};
}
export function useAssetImage(ref?: AssetRef) {
  const [state, setState] = useState<{ key?: string; url?: string; error?: string }>({});
  const key = ref ? `${connectionSettings().url}/${ref.sha256}/${getConnectionGeneration()}` : undefined;
  useEffect(() => {
    if (!ref) return;
    let live = true, objectUrl: string | undefined;
    assetBytes(ref).then((bytes) => {
      if (!live) return;
      objectUrl = URL.createObjectURL(new Blob([bytes], { type: ref.contentType || ref.mimeType || "image/png" }));
      setState({ key, url: objectUrl });
    }, (error: unknown) => { if (live) setState({ key, error: error instanceof Error ? error.message : "Image could not load." }); });
    return () => { live = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state.key === key ? state : {};
}
