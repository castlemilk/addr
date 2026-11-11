"use client";

import Link from "next/link";
import React, { useCallback, useEffect, useState } from "react";
import {
  AddressMeta,
  FeatureKind,
  GetAddressCrmResponse,
  Poi,
} from "@/gen/crm/v1/crm_pb";
import { ConnectError, Code } from "@connectrpc/connect";
import {
  getCrmToken,
  setCrmToken,
  crmClient,
  CRM_SCOPE_LABELS,
} from "@/lib/crm-client";
import type { CrmDrawKind } from "@/lib/crm-client";
import {
  X,
  KeyRound,
  ShieldCheck,
  Save,
  Trash2,
  Pencil,
  Plus,
  Loader2,
  Trees,
  Home,
  Box,
  MapPin,
  Copy,
  Check,
  ListTree,
  ExternalLink,
} from "lucide-react";

interface CrmPanelProps {
  isOpen: boolean;
  addressId: bigint | null;
  fullAddress: string;
  currentLocation: { lat: number; lon: number };
  refreshToken: number;
  pendingGeometry: {
    kind: FeatureKind;
    addressId: bigint;
    geojson: string;
    label?: string;
    properties?: Record<string, string>;
  } | null;
  onGeometryConsumed: () => void;
  onRequestDraw: (kind: CrmDrawKind) => void;
  onEditFeature: (feature: {
    id: bigint;
    kind: FeatureKind;
    geometryJson: string;
    label: string | null;
  }) => void;
  editingFeatureId: string | null;
  onClose: () => void;
}

const STATUS_OPTIONS = ["", "lead", "quoted", "won", "lost", "archived"];
// Append-only pipeline reports (eyes checkpoints) arrive as POIs in this category.
// They are observations for the reviewer, never labels or accuracy claims.
export const PIPELINE_CHECKPOINT_CATEGORY = "pipeline-checkpoint";
export const isHttpUrl = (value: string) => /^https?:\/\/\S+$/i.test(value.trim());

function CopyableCode({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <span className="inline-flex items-center gap-1 min-w-0 max-w-full">
      <code className="truncate rounded bg-slate-950 px-1.5 py-0.5 text-[10px] text-cyan-200 border border-slate-800" title={value}>
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        className="p-1 rounded hover:bg-slate-700 text-slate-300 shrink-0"
        aria-label={copied ? "Copied report path" : "Copy report path"}
        title={copied ? "Copied" : "Copy"}
      >
        {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
      </button>
    </span>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ConnectError) {
    return `${error.code}: ${error.rawMessage}`;
  }
  return String(error);
}

export default function CrmPanel({
  isOpen,
  addressId,
  fullAddress,
  currentLocation,
  refreshToken,
  pendingGeometry,
  onGeometryConsumed,
  onRequestDraw,
  onEditFeature,
  editingFeatureId,
  onClose,
}: CrmPanelProps) {
  const [token, setToken] = useState("");
  const [principal, setPrincipal] = useState<{
    name: string;
    roles: string[];
    scopes: string[];
    keyLabel: string | null;
  } | null>(null);
  const [state, setState] = useState<GetAddressCrmResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [metaForm, setMetaForm] = useState({
    status: "",
    ownerName: "",
    ownerEmail: "",
    ownerPhone: "",
    notes: "",
    tags: "",
  });
  const [poiForm, setPoiForm] = useState({
    name: "",
    category: "",
    phone: "",
    website: "",
    notes: "",
  });
  const [editingPoi, setEditingPoi] = useState<Poi | null>(null);
  // Read-only disclosure of a spatial feature's provenance properties (e.g. reviewer source keys).
  const [shownProperties, setShownProperties] = useState<Set<string>>(() => new Set());
  const toggleProperties = (id: string) =>
    setShownProperties((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const has = useCallback(
    (scope: string) => principal?.scopes.includes(scope) ?? false,
    [principal]
  );

  const signIn = useCallback(async () => {
    setCrmToken(token);
    setError(null);
    setBusy(true);
    try {
      const who = await crmClient.whoAmI({});
      setPrincipal({
        name: who.principal,
        roles: who.roles,
        scopes: who.scopes,
        keyLabel: who.keyLabel ?? null,
      });
    } catch (err) {
      setToken("");
      setCrmToken("");
      setPrincipal(null);
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [token]);

  const load = useCallback(async () => {
    if (!addressId) return;
    setBusy(true);
    setError(null);
    try {
      const response = await crmClient.getAddressCrm({ addressId });
      setState(response);
      const meta = response.meta;
      setMetaForm({
        status: meta?.status ?? "",
        ownerName: meta?.ownerName ?? "",
        ownerEmail: meta?.ownerEmail ?? "",
        ownerPhone: meta?.ownerPhone ?? "",
        notes: meta?.notes ?? "",
        tags: (meta?.tags ?? []).join(", "),
      });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [addressId]);

  // Sign in on open: probe anonymous access first, fall back to any stored
  // key. Anonymous-first heals stale stored keys (a noauth dev server signs
  // in as local admin even when an old key lingers in localStorage) and keeps
  // keyed deployments working with one extra round trip.
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    const applyWho = (who: {
      principal: string;
      roles: string[];
      scopes: string[];
      keyLabel?: string;
    }) => {
      if (cancelled) return;
      setPrincipal({
        name: who.principal,
        roles: who.roles,
        scopes: who.scopes,
        keyLabel: who.keyLabel ?? null,
      });
    };
    // connect-web v1 surfaces dead-service network failures as
    // Code.Unknown (connection refused yields no response to map); treat
    // Unavailable the same so the unreachable hint fires either way.
    const isUnreachable = (err: unknown) =>
      err instanceof ConnectError &&
      (err.code === Code.Unavailable || err.code === Code.Unknown);
    const existing = getCrmToken();
    setToken(existing);
    setError(null);
    // Probe without credentials; temporarily shelve any stored key so the
    // probe is genuinely anonymous (restored below if the server wants auth).
    if (existing) setCrmToken("");
    crmClient
      .whoAmI({})
      .then((who) => {
        setCrmToken("");
        setToken("");
        applyWho(who);
      })
      .catch((anonErr: unknown) => {
        if (cancelled) return;
        if (!existing) {
          setPrincipal(null);
          if (isUnreachable(anonErr)) {
            setError(
              "CRM service unreachable — is the Go service running (task start:server)?"
            );
          }
          return;
        }
        setCrmToken(existing);
        crmClient
          .whoAmI({})
          .then(applyWho)
          .catch((keyErr: unknown) => {
            if (cancelled) return;
            setPrincipal(null);
            if (isUnreachable(keyErr)) {
              setError(
                "CRM service unreachable — is the Go service running (task start:server)?"
              );
            } else if (
              keyErr instanceof ConnectError &&
              keyErr.code === Code.Unauthenticated
            ) {
              // Stored key is dead (revoked/rotated): drop it so the next
              // open doesn't retry it, and leave the sign-in form ready.
              setToken("");
              setCrmToken("");
              setError("Stored API key was rejected — paste a fresh key below.");
            }
          });
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen]);

  // Load address state once authenticated
  useEffect(() => {
    if (!isOpen || !principal || !addressId) return;
    void load();
  }, [isOpen, principal, addressId, refreshToken, load]);

  // Persist a completed boundary drawing. A new roof drawing replaces the
  // existing roof override (if any): records carry a single ring, and the
  // batch applier would otherwise race two roof rows nondeterministically.
  useEffect(() => {
    if (!pendingGeometry || !addressId) return;
    (async () => {
      setBusy(true);
      setError(null);
      try {
        const existingRoofId =
          pendingGeometry.kind === FeatureKind.ROOF
            ? [...(state?.features ?? [])]
                .reverse()
                .find((feature) => feature.kind === FeatureKind.ROOF)?.id ?? null
            : null;
        await crmClient.upsertFeature({
          ...(existingRoofId != null ? { id: existingRoofId } : {}),
          addressId: pendingGeometry.addressId,
          kind: pendingGeometry.kind,
          geometryJson: pendingGeometry.geojson,
          ...(pendingGeometry.label ? { label: pendingGeometry.label } : {}),
          ...(pendingGeometry.properties ? { properties: pendingGeometry.properties } : {}),
        });
        onGeometryConsumed();
        await load();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    })();
  }, [pendingGeometry, addressId, onGeometryConsumed, load]);

  const saveMeta = async () => {
    if (!addressId) return;
    setBusy(true);
    setError(null);
    try {
      await crmClient.updateAddressMeta({
        addressId,
        status: metaForm.status,
        ownerName: metaForm.ownerName,
        ownerEmail: metaForm.ownerEmail,
        ownerPhone: metaForm.ownerPhone,
        notes: metaForm.notes,
        tags: metaForm.tags
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
      });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const deleteFeature = async (id: bigint) => {
    setBusy(true);
    setError(null);
    try {
      await crmClient.deleteFeature({ id });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const savePoi = async () => {
    if (!addressId) return;
    setBusy(true);
    setError(null);
    try {
      if (editingPoi) {
        await crmClient.updatePoi({
          id: editingPoi.id,
          name: poiForm.name,
          category: poiForm.category,
          phone: poiForm.phone,
          website: poiForm.website,
          notes: poiForm.notes,
        });
      } else {
        await crmClient.createPoi({
          name: poiForm.name,
          category: poiForm.category,
          phone: poiForm.phone,
          website: poiForm.website,
          notes: poiForm.notes,
          latitude: currentLocation.lat,
          longitude: currentLocation.lon,
          addressId,
        });
      }
      setPoiForm({ name: "", category: "", phone: "", website: "", notes: "" });
      setEditingPoi(null);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const deletePoi = async (id: bigint) => {
    setBusy(true);
    setError(null);
    try {
      await crmClient.deletePoi({ id });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (!isOpen) return null;

  const inputClass =
    "w-full px-2.5 py-1.5 rounded-lg bg-slate-950/70 border border-slate-700 text-xs text-slate-100 focus:outline-none focus:border-blue-500";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 backdrop-blur-sm">
      <div className="w-[720px] max-h-[86vh] overflow-y-auto rounded-2xl border border-slate-700 bg-slate-900 text-slate-100 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-800">
          <div className="flex items-center space-x-2">
            <ShieldCheck className="w-5 h-5 text-emerald-400" />
            <div>
              <div className="text-sm font-bold">Address CRM</div>
              <div className="text-xs text-slate-400">{fullAddress || "No address selected"}</div>
            </div>
          </div>
          <div className="flex items-center space-x-2">
            {principal && (
              <span className="text-[11px] px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                {principal.name} · {principal.roles.join(", ")}
              </span>
            )}
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-800">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="p-5 space-y-5">
          {/* Auth */}
          <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3.5 space-y-2">
            <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-400">
              <KeyRound className="w-3.5 h-3.5" />
              <span>CRM access</span>
              {principal?.keyLabel === "local-noauth" && (
                <span className="normal-case font-medium text-emerald-300">
                  Local dev · no key needed
                </span>
              )}
            </div>
            {principal?.keyLabel === "local-noauth" ? (
              <div className="text-[11px] text-slate-400">
                Signed in as <span className="font-semibold text-slate-200">local admin</span>{" "}
                (server runs with CRM auth disabled for loopback development).
              </div>
            ) : (
            <div className="flex items-center space-x-2">
              <input
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="Paste API key (task crm:key -- --user ...)"
                className={inputClass}
              />
              <button
                onClick={signIn}
                disabled={busy || !token}
                className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-xs font-semibold whitespace-nowrap"
              >
                {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "Connect"}
              </button>
              {principal && (
                <button
                  onClick={() => {
                    setToken("");
                    setCrmToken("");
                    setPrincipal(null);
                    setState(null);
                  }}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs"
                >
                  Sign out
                </button>
              )}
            </div>
            )}
            {principal && (
              <div className="flex flex-wrap gap-1.5">
                {principal.scopes.map((scope) => (
                  <span
                    key={scope}
                    className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-300 border border-blue-500/25"
                  >
                    {CRM_SCOPE_LABELS[scope] ?? scope}
                  </span>
                ))}
              </div>
            )}
            {error && (
              <div className="text-[11px] text-red-300 bg-red-500/10 border border-red-500/30 rounded-lg px-2.5 py-1.5">
                {error}
              </div>
            )}
          </div>

          {principal && !addressId && (
            <div className="text-xs text-slate-400">
              Select an address on the map (or search) to edit its CRM record.
            </div>
          )}

          {principal && addressId && state && (
            <>
              {/* Metadata */}
              <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3.5 space-y-3">
                <div className="text-xs font-bold uppercase tracking-wider text-slate-400">
                  Address metadata
                </div>
                <div className="grid grid-cols-2 gap-2.5">
                  <label className="space-y-1">
                    <span className="text-[11px] text-slate-400">Status</span>
                    <select
                      value={metaForm.status}
                      onChange={(event) =>
                        setMetaForm({ ...metaForm, status: event.target.value })
                      }
                      className={inputClass}
                    >
                      {STATUS_OPTIONS.map((option) => (
                        <option key={option} value={option}>
                          {option || "—"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="space-y-1">
                    <span className="text-[11px] text-slate-400">Tags (comma separated)</span>
                    <input
                      value={metaForm.tags}
                      onChange={(event) => setMetaForm({ ...metaForm, tags: event.target.value })}
                      className={inputClass}
                    />
                  </label>
                  <label className="space-y-1">
                    <span className="text-[11px] text-slate-400">Owner name</span>
                    <input
                      value={metaForm.ownerName}
                      onChange={(event) =>
                        setMetaForm({ ...metaForm, ownerName: event.target.value })
                      }
                      className={inputClass}
                    />
                  </label>
                  <label className="space-y-1">
                    <span className="text-[11px] text-slate-400">Owner phone</span>
                    <input
                      value={metaForm.ownerPhone}
                      onChange={(event) =>
                        setMetaForm({ ...metaForm, ownerPhone: event.target.value })
                      }
                      className={inputClass}
                    />
                  </label>
                  <label className="col-span-2 space-y-1">
                    <span className="text-[11px] text-slate-400">Owner email</span>
                    <input
                      value={metaForm.ownerEmail}
                      onChange={(event) =>
                        setMetaForm({ ...metaForm, ownerEmail: event.target.value })
                      }
                      className={inputClass}
                    />
                  </label>
                  <label className="col-span-2 space-y-1">
                    <span className="text-[11px] text-slate-400">Notes</span>
                    <textarea
                      value={metaForm.notes}
                      onChange={(event) => setMetaForm({ ...metaForm, notes: event.target.value })}
                      rows={2}
                      className={`${inputClass} resize-none`}
                    />
                  </label>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-slate-500">
                    {state.meta?.updatedAt
                      ? `last edit ${state.meta.updatedAt} by ${state.meta.updatedBy}`
                      : "no CRM edits yet"}
                  </span>
                  <button
                    onClick={saveMeta}
                    disabled={busy || !has("crm.address.write")}
                    className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-xs font-semibold"
                    title={has("crm.address.write") ? "" : "Requires crm.address.write"}
                  >
                    <Save className="w-3.5 h-3.5" />
                    <span>Save metadata</span>
                  </button>
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 border-y border-slate-700 py-4">
                <div className="max-w-sm"><h3 className="text-sm font-semibold">Boundary training data</h3><p className="mt-1 text-xs leading-relaxed text-slate-400">Draw or edit the actual roof edge, then review it against imagery. Corrections require review before entering the training export.</p></div>
                <Link href={`/reviews?cohort=all_roofs&id=${addressId}`} className="rounded-md border border-cyan-700 px-3 py-2 text-xs font-medium text-cyan-200 hover:bg-cyan-950">Review roof boundary</Link>
              </div>
              {/* Boundaries */}
              <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3.5 space-y-3">
                <div className="flex items-center justify-between">
                  <div className="text-xs font-bold uppercase tracking-wider text-slate-400">
                    Boundaries
                  </div>
                  <div className="flex items-center space-x-1.5">
                    <button
                      onClick={() => onRequestDraw("roof")}
                      disabled={!has("crm.roof.write")}
                      className="flex items-center space-x-1 px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 text-xs"
                    >
                      <Home className="w-3.5 h-3.5 text-orange-400" />
                      <span>Draw roof</span>
                    </button>
                    <button
                      onClick={() => onRequestDraw("tree")}
                      disabled={!has("crm.tree.write")}
                      className="flex items-center space-x-1 px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 text-xs"
                    >
                      <Trees className="w-3.5 h-3.5 text-emerald-400" />
                      <span>Draw tree</span>
                    </button>
                    <button
                      onClick={() => onRequestDraw("object")}
                      disabled={!has("crm.object.write")}
                      className="flex items-center space-x-1 px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-40 text-xs"
                    >
                      <Box className="w-3.5 h-3.5 text-sky-400" />
                      <span>Draw object</span>
                    </button>
                    <button
                      onClick={() => onRequestDraw("box")}
                      disabled={!has("crm.object.write")}
                      title="Drag a rectangle proposing the roof extent (saved as a labelled object for review feedback, not a roof override)"
                      className="flex items-center space-x-1 px-2.5 py-1.5 rounded-lg bg-fuchsia-950 hover:bg-fuchsia-900 disabled:opacity-40 text-xs border border-fuchsia-800"
                    >
                      <Box className="w-3.5 h-3.5 text-fuchsia-400" />
                      <span>Propose box</span>
                    </button>
                  </div>
                </div>
                {state.features.length === 0 ? (
                  <div className="text-[11px] text-slate-500">
                    No overrides. Drawing replaces the extracted boundary in the CRM; the tile
                    archive stays untouched.
                  </div>
                ) : (
                  <div className="space-y-1.5">
                    {state.features.map((feature) => (
                      <div
                        key={feature.id.toString()}
                        className="px-2.5 py-1.5 rounded-lg bg-slate-900/70 border border-slate-800 text-xs"
                      >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center space-x-2">
                          {feature.kind === FeatureKind.ROOF && (
                            <Home className="w-3.5 h-3.5 text-orange-400" />
                          )}
                          {feature.kind === FeatureKind.TREE && (
                            <Trees className="w-3.5 h-3.5 text-emerald-400" />
                          )}
                          {feature.kind === FeatureKind.OBJECT && (
                            <Box className="w-3.5 h-3.5 text-sky-400" />
                          )}
                          <span className="font-semibold">
                            {FeatureKind[feature.kind].replace("FEATURE_KIND_", "").toLowerCase()}
                          </span>
                          {feature.label && (
                            <span className="text-slate-400">({feature.label})</span>
                          )}
                          <span className="text-[10px] text-slate-500">
                            {feature.updatedAt} · {feature.updatedBy}
                          </span>
                        </div>
                        <div className="flex items-center space-x-1">
                          <button
                            onClick={() =>
                              onEditFeature({
                                id: feature.id,
                                kind: feature.kind,
                                geometryJson: feature.geometryJson,
                                label: feature.label ?? null,
                              })
                            }
                            disabled={
                              busy ||
                              editingFeatureId === feature.id.toString() ||
                              !has(
                                feature.kind === FeatureKind.ROOF
                                  ? "crm.roof.write"
                                  : feature.kind === FeatureKind.TREE
                                  ? "crm.tree.write"
                                  : "crm.object.write"
                              )
                            }
                            className="p-1 rounded hover:bg-sky-500/20 text-sky-300 disabled:opacity-40"
                            title="Edit boundary vertices"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          <button
                            onClick={() => deleteFeature(feature.id)}
                            disabled={busy}
                            className="p-1 rounded hover:bg-red-500/20 text-red-300"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => toggleProperties(feature.id.toString())}
                            aria-pressed={shownProperties.has(feature.id.toString())}
                            aria-controls={`crm-feature-properties-${feature.id.toString()}`}
                            className="p-1 rounded hover:bg-slate-700 text-slate-300"
                            title={shownProperties.has(feature.id.toString()) ? "Hide properties" : "Show properties (read-only)"}
                          >
                            <ListTree className="w-3.5 h-3.5" />
                            <span className="sr-only">
                              {shownProperties.has(feature.id.toString()) ? "Hide properties" : "Show properties"}
                            </span>
                          </button>
                        </div>
                      </div>
                      {shownProperties.has(feature.id.toString()) && (
                        <dl
                          id={`crm-feature-properties-${feature.id.toString()}`}
                          className="mt-1.5 grid grid-cols-[minmax(0,40%)_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-[10px] border-t border-slate-800 pt-1.5"
                          aria-label="Feature properties (read-only)"
                        >
                          {Object.keys(feature.properties).length === 0 ? (
                            <dd className="col-span-2 text-slate-500">No properties recorded.</dd>
                          ) : (
                            Object.entries(feature.properties)
                              .toSorted(([a], [b]) => a.localeCompare(b))
                              .map(([key, value]) => (
                                <React.Fragment key={key}>
                                  <dt className="truncate text-slate-500 font-mono" title={key}>{key}</dt>
                                  <dd className="break-all text-slate-300 font-mono">{value}</dd>
                                </React.Fragment>
                              ))
                          )}
                        </dl>
                      )}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* POIs */}
              <div className="rounded-xl border border-slate-800 bg-slate-950/50 p-3.5 space-y-3">
                <div className="text-xs font-bold uppercase tracking-wider text-slate-400">
                  POIs at this address
                </div>
                {state.pois.length > 0 && (
                  <div className="space-y-1.5">
                    {state.pois.map((poi) => (
                      <div
                        key={poi.id.toString()}
                        className={
                          poi.category === PIPELINE_CHECKPOINT_CATEGORY
                            ? "flex items-start justify-between px-2.5 py-2 rounded-lg bg-cyan-950/40 border border-cyan-900 text-xs"
                            : "flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-slate-900/70 border border-slate-800 text-xs"
                        }
                        data-poi-category={poi.category || undefined}
                      >
                        {poi.category === PIPELINE_CHECKPOINT_CATEGORY ? (
                          <div className="min-w-0 flex-1 space-y-1">
                            <div className="flex items-center gap-2 min-w-0">
                              <span className="shrink-0 rounded-full border border-cyan-700 bg-cyan-900/60 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-cyan-200">
                                {poi.category}
                              </span>
                              <span className="font-semibold truncate">{poi.name}</span>
                            </div>
                            {poi.notes && (
                              <p className="text-slate-300 whitespace-pre-wrap break-words leading-snug">{poi.notes}</p>
                            )}
                            {poi.website && (
                              isHttpUrl(poi.website) ? (
                                <a
                                  href={poi.website.trim()}
                                  target="_blank"
                                  rel="noreferrer noopener"
                                  className="inline-flex items-center gap-1 text-cyan-300 underline underline-offset-2 break-all"
                                >
                                  <ExternalLink className="w-3 h-3 shrink-0" />
                                  {poi.website.trim()}
                                </a>
                              ) : (
                                <CopyableCode value={poi.website} />
                              )
                            )}
                            <p className="text-[10px] text-slate-500">
                              Pipeline observation for review. Not a label or an accuracy claim.
                            </p>
                          </div>
                        ) : (
                        <div className="flex items-center space-x-2 min-w-0">
                          <MapPin className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                          <span className="font-semibold truncate">{poi.name}</span>
                          {poi.category && (
                            <span className="text-slate-400 truncate">{poi.category}</span>
                          )}
                          {poi.notes && (
                            <span className="text-slate-500 truncate">{poi.notes}</span>
                          )}
                        </div>
                        )}
                        <div className="flex items-center space-x-1">
                          <button
                            onClick={() => {
                              setEditingPoi(poi);
                              setPoiForm({
                                name: poi.name,
                                category: poi.category ?? "",
                                phone: poi.phone ?? "",
                                website: poi.website ?? "",
                                notes: poi.notes ?? "",
                              });
                            }}
                            className="p-1 rounded hover:bg-slate-700"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          <button
                            onClick={() => deletePoi(poi.id)}
                            disabled={busy}
                            className="p-1 rounded hover:bg-red-500/20 text-red-300"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <div className="grid grid-cols-2 gap-2.5">
                  <input
                    value={poiForm.name}
                    onChange={(event) => setPoiForm({ ...poiForm, name: event.target.value })}
                    placeholder="POI name"
                    className={inputClass}
                  />
                  <input
                    value={poiForm.category}
                    onChange={(event) => setPoiForm({ ...poiForm, category: event.target.value })}
                    placeholder="Category"
                    className={inputClass}
                  />
                  <input
                    value={poiForm.phone}
                    onChange={(event) => setPoiForm({ ...poiForm, phone: event.target.value })}
                    placeholder="Phone"
                    className={inputClass}
                  />
                  <input
                    value={poiForm.website}
                    onChange={(event) => setPoiForm({ ...poiForm, website: event.target.value })}
                    placeholder="Website"
                    className={inputClass}
                  />
                  <input
                    value={poiForm.notes}
                    onChange={(event) => setPoiForm({ ...poiForm, notes: event.target.value })}
                    placeholder="Notes"
                    className={`${inputClass} col-span-2`}
                  />
                </div>
                <div className="flex items-center justify-end space-x-2">
                  {editingPoi && (
                    <button
                      onClick={() => {
                        setEditingPoi(null);
                        setPoiForm({ name: "", category: "", phone: "", website: "", notes: "" });
                      }}
                      className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs"
                    >
                      Cancel edit
                    </button>
                  )}
                  <button
                    onClick={savePoi}
                    disabled={busy || !has("crm.poi.write") || !poiForm.name.trim()}
                    className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-xs font-semibold"
                    title={has("crm.poi.write") ? "" : "Requires crm.poi.write"}
                  >
                    <Plus className="w-3.5 h-3.5" />
                    <span>{editingPoi ? "Save POI" : "Add POI at pin"}</span>
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
