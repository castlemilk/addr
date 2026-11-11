"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import {
  Search,
  MapPin,
  Building2,
  Globe,
  Phone,
  Layers,
  Database,
  RefreshCw,
  ExternalLink,
  Copy,
  Check,
  ShieldCheck,
  Sun,
  Moon,
  Box,
  X,
} from "lucide-react";
import { Address, ReverseGeocodeResult, SearchResultItem, DbStats } from "@/lib/geo-engine";
import { geoClient } from "@/lib/connect-client";
import HouseReviewLink from "@/components/scenes/HouseReviewLink";
import type { AddressEnrichment } from "@/lib/address-enrichment";
import { WorkbenchNav } from "@/components/shell/WorkbenchNav";

interface SidebarProps {
  selectedLocation: { lat: number; lon: number } | null;
  reverseResult: ReverseGeocodeResult | null;
  isLoadingReverse: boolean;
  onSelectCoordinates: (lat: number, lon: number) => void;
  showPois: boolean;
  setShowPois: (val: boolean) => void;
  showAddresses: boolean;
  setShowAddresses: (val: boolean) => void;
  isDarkMode?: boolean;
  onToggleTheme?: () => void;
  onOpenDeep3D?: () => void;
  onOpenCrm?: (address: Address) => void;
  /** Lookup served by the in-browser fallback (Go service unreachable). */
  usingFallbackGeo?: boolean;
}

/** Search radius used by both the Connect and fallback reverse lookups (page.tsx). */
const REVERSE_RADIUS_M = 300;

/** Match labels. "POI + G-NAF Address" and "G-NAF Rooftop Address" are pinned by
 *  scripts/live-check.mjs, fallback-check.mjs and poi-join-check.mjs; keep them verbatim. */
const MATCH_LABELS: Record<string, string> = {
  poi_and_address: "POI + G-NAF Address",
  poi_only: "POI match",
  address_only: "G-NAF Rooftop Address",
};

/** Metres for the provenance line: one decimal under 10 m, whole metres above. */
export function formatMetres(metres: number): string {
  return `${metres < 10 ? metres.toFixed(1) : Math.round(metres)} m`;
}

/** A distance is only printed when the payload carries a finite number (never "NaN m"). */
const hasMetres = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** Index counts for the footer: a placeholder until the stat exists, never a crash on a partial payload. */
export function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "…";
}

/** Plain-words lines for the Property structures section, built by src/lib/address-enrichment.ts. */
type EnrichmentCopy = { summary: string | null; roof: string | null; structures: { line: string; url: string | null; key: string }[]; solar: string | null; lot: string[]; allocation: string | null; reason: string; roofUrl: string | null };

/** Dark-tone nav tokens for the sidebar's dark map mode (shell.css only scopes the light tone). */
const DARK_NAV_TOKENS = { "--review-text": "#edf3f8", "--review-muted": "#a0b1c5", "--review-accent": "#67d7e7" } as React.CSSProperties;

export default function Sidebar({
  selectedLocation,
  reverseResult,
  isLoadingReverse,
  onSelectCoordinates,
  showPois,
  setShowPois,
  showAddresses,
  setShowAddresses,
  isDarkMode = false,
  onToggleTheme,
  onOpenDeep3D,
  onOpenCrm,
  usingFallbackGeo = false,
}: SidebarProps) {
  const isDark = isDarkMode;
  // Search state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResultItem[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [showResultsDropdown, setShowResultsDropdown] = useState(false);

  // Stats & Ingestion Modal
  const [stats, setStats] = useState<DbStats | null>(null);
  const [isIngestModalOpen, setIsIngestModalOpen] = useState(false);
  const [ingestPreset, setIngestPreset] = useState("melbourne_cbd");
  const [ingestLimit, setIngestLimit] = useState(3000);
  const [isIngesting, setIsIngesting] = useState(false);
  const [ingestLogs, setIngestLogs] = useState<string[]>([]);
  const [copiedCoords, setCopiedCoords] = useState(false);
  // Per-address enrichment (agent hints and model estimates, never labels): null until the route answers 200.
  const [enrichmentState, setEnrichment] = useState<{ addressId: number; copy: EnrichmentCopy } | null>(null);
  const enrichmentAddressId = reverseResult?.address?.id;
  const enrichment = enrichmentState && enrichmentState.addressId === enrichmentAddressId ? enrichmentState.copy : null;
  useEffect(() => {
    if (typeof enrichmentAddressId !== "number" || !Number.isInteger(enrichmentAddressId)) return;
    const controller = new AbortController();
    // The parser and copy helpers load with the data so the sidebar bundle stays as it was.
    void Promise.all([fetch(`/api/address/enrichment?address=${enrichmentAddressId}`, { signal: controller.signal }), import("@/lib/address-enrichment")])
      .then(async ([response, lib]) => {
        if (!response.ok || controller.signal.aborted) return;
        const doc: AddressEnrichment | null = lib.parseEnrichment(await response.json());
        if (!doc || controller.signal.aborted) return;
        setEnrichment({ addressId: enrichmentAddressId, copy: {
          summary: lib.structureSummary(doc), roof: lib.primaryRoofLine(doc.primary_roof), roofUrl: doc.primary_roof?.review_url ?? null,
          structures: doc.structures.map((structure, index) => ({ line: lib.structureLine(structure), url: structure.review_url, key: `${structure.scene_id ?? ""}/${structure.object_id ?? index}` })),
          solar: lib.solarLine(doc.solar), lot: lib.lotLines(doc), allocation: lib.describeAllocation(doc.allocation), reason: doc.allocation?.reason ?? "",
        } });
      })
      .catch(() => { /* a missing or unreadable document renders nothing */ });
    return () => controller.abort();
  }, [enrichmentAddressId]);

  // Fetch DB stats via Connect-ES
  const fetchStats = async () => {
    try {
      const statsRes = await geoClient.getStats({});
      setStats({
        placeCount: Number(statsRes.placeCount),
        addressCount: Number(statsRes.addressCount),
        dbSizeBytes: Number(statsRes.dbSizeBytes),
        dbSizeFormatted: statsRes.dbSizeFormatted,
        regions: statsRes.regions.map((r) => ({
          id: Number(r.id),
          name: r.name,
          bbox: r.bbox,
          place_count: Number(r.placeCount),
          address_count: Number(r.addressCount),
          created_at: r.createdAt,
        })),
      });
    } catch (e) {
      // Fallback to local REST API
      try {
        const res = await fetch("/api/stats");
        const data = await res.json();
        if (!data.error) setStats(data);
      } catch (err) {
        console.error("Failed to load stats", err);
      }
    }
  };

  useEffect(() => {
    fetchStats();
  }, []);

  // Debounced search via Connect-ES
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults([]);
      setShowResultsDropdown(false);
      return;
    }

    const timer = setTimeout(async () => {
      setIsSearching(true);
      try {
        const searchRes = await geoClient.search({
          query: searchQuery.trim(),
          latitude: selectedLocation?.lat,
          longitude: selectedLocation?.lon,
          limit: 15,
        });

        const items: SearchResultItem[] = searchRes.results.map((r) => ({
          type: r.type as "place" | "address",
          id: Number(r.id),
          title: r.title,
          subtitle: r.subtitle,
          category: r.category,
          lat: r.latitude,
          lon: r.longitude,
          distance_meters: r.distanceMeters,
        }));
        setSearchResults(items);
        setShowResultsDropdown(true);
      } catch (err) {
        // Fallback to local REST API
        try {
          const params = new URLSearchParams({ q: searchQuery.trim() });
          if (selectedLocation) {
            params.set("lat", selectedLocation.lat.toString());
            params.set("lon", selectedLocation.lon.toString());
          }
          const res = await fetch(`/api/search?${params.toString()}`);
          const data = await res.json();
          setSearchResults(data.results || []);
          setShowResultsDropdown(true);
        } catch (fallbackErr) {
          console.error("Search failed:", fallbackErr);
        }
      } finally {
        setIsSearching(false);
      }
    }, 250);

    return () => clearTimeout(timer);
  }, [searchQuery, selectedLocation]);

  // Copy coordinates
  const handleCopyCoords = () => {
    if (!selectedLocation) return;
    navigator.clipboard.writeText(`${selectedLocation.lat.toFixed(6)}, ${selectedLocation.lon.toFixed(6)}`);
    setCopiedCoords(true);
    setTimeout(() => setCopiedCoords(false), 1500);
  };

  // Trigger ingestion
  const handleStartIngest = async () => {
    setIsIngesting(true);
    setIngestLogs(["Initiating offline ingestion..."]);
    try {
      const res = await fetch("/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset: ingestPreset, limit: ingestLimit }),
      });
      const data = await res.json();
      if (data.error) {
        setIngestLogs((prev) => [...prev, `[Error] ${data.error}`]);
        setIsIngesting(false);
        return;
      }

      // Poll ingestion status
      const interval = setInterval(async () => {
        const pollRes = await fetch("/api/ingest");
        const pollData = await pollRes.json();
        if (pollData.logs) {
          setIngestLogs(pollData.logs);
        }
        if (pollData.status === "completed" || pollData.status === "error") {
          clearInterval(interval);
          setIsIngesting(false);
          fetchStats();
        }
      }, 1500);
    } catch (err: unknown) {
      setIngestLogs((prev) => [
        ...prev,
        `[Error] ${err instanceof Error ? err.message : "Ingestion failed"}`,
      ]);
      setIsIngesting(false);
    }
  };

  const place = reverseResult?.place;
  const address = reverseResult?.address;
  // Provenance names a match only when the lookup headlined one. "approximate" and "none"
  // (nothing at the pin) carry no label rather than a vague one beside the empty state.
  const matchLabel = address || place ? MATCH_LABELS[reverseResult?.match_type ?? ""] : undefined;
  const addressMetres = address && hasMetres(address.distance_meters) ? formatMetres(address.distance_meters) : null;
  const provenance = matchLabel ? (addressMetres ? `${matchLabel}, ${addressMetres} from the address point` : matchLabel) : null;
  const lookupMs = reverseResult?.execution_time_ms;
  const showLookupMs = typeof lookupMs === "number" && Number.isFinite(lookupMs);

  const iconButton = `inline-flex h-8 w-8 items-center justify-center rounded-md border transition ${
    isDark
      ? "bg-slate-800 hover:bg-slate-700 text-slate-300 border-slate-700"
      : "bg-white hover:bg-slate-100 text-slate-700 border-slate-300"
  }`;
  const quietButton = `inline-flex min-h-9 items-center justify-center gap-2 rounded-md border px-3 text-xs font-medium transition disabled:opacity-50 ${
    isDark
      ? "bg-slate-800 hover:bg-slate-700 text-slate-100 border-slate-700"
      : "bg-white hover:bg-slate-50 text-slate-800 border-slate-300"
  }`;
  const chip = `px-2 py-1 rounded-md border text-xs whitespace-nowrap transition ${
    isDark
      ? "bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border-slate-700"
      : "bg-slate-100 hover:bg-slate-200 text-slate-700 hover:text-slate-900 border-slate-200"
  }`;
  const layerToggle = `flex items-center gap-2 rounded-md border px-2 py-2 ${
    isDark ? "bg-slate-800 border-slate-700 text-slate-200" : "bg-slate-50 border-slate-200 text-slate-700"
  }`;
  const muted = isDark ? "text-slate-400" : "text-slate-500";

  // 640px holds the full workspace row (526px nav + two icon buttons) from xl up; below that the row scrolls (globals.css .map-shell-nav).
  return (
    <aside aria-label="Map sidebar" className={`w-full md:w-[460px] xl:w-[640px] h-full flex flex-col z-10 select-none transition-colors duration-200 ${
      isDark
        ? "bg-slate-900 border-r border-slate-800 text-slate-100"
        : "bg-white border-r border-slate-200 text-slate-900"
    }`}>
      {/* App header: one nav row, then the search bar */}
      <div className={`relative z-20 px-4 pb-4 border-b transition-colors duration-200 ${
        isDark
          ? "border-slate-800 bg-slate-950/60 backdrop-blur"
          : "border-slate-200 bg-white/95 backdrop-blur"
      }`}>
        <div className="flex items-center justify-between gap-3">
          <div className="map-shell-nav min-w-0 flex-1" style={isDark ? DARK_NAV_TOKENS : undefined}>
            <WorkbenchNav tone={isDark ? "dark" : "light"} current="map" />
          </div>
          <div className="flex flex-none items-center gap-1.5">
            {onToggleTheme && (
              <button
                type="button"
                onClick={onToggleTheme}
                className={iconButton}
                aria-label={isDark ? "Switch to light map" : "Switch to dark map"}
                title={isDark ? "Switch to light map" : "Switch to dark map"}
              >
                {isDark ? <Sun className="w-4 h-4 text-amber-400" aria-hidden="true" /> : <Moon className="w-4 h-4 text-slate-600" aria-hidden="true" />}
              </button>
            )}
            <button
              type="button"
              onClick={() => setIsIngestModalOpen(true)}
              className={iconButton}
              aria-label="Manage offline index"
              title="Manage offline index"
            >
              <Database className="w-4 h-4 text-blue-600" aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* Search Bar */}
        <div className="relative mt-1">
          <div className="relative flex items-center">
            <Search className="w-4 h-4 text-slate-400 absolute left-3 pointer-events-none" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search address, business or coffee..."
              className={`w-full pl-9 pr-8 py-2 text-sm rounded-xl border focus:outline-none transition shadow-sm ${
                isDark
                  ? "bg-slate-800/90 text-slate-100 placeholder-slate-400 border-slate-700 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                  : "bg-slate-50 text-slate-900 placeholder-slate-400 border-slate-200 hover:border-slate-300 focus:bg-white focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              }`}
            />
            {isSearching && (
              <RefreshCw className="w-3.5 h-3.5 text-blue-500 animate-spin absolute right-3" />
            )}
            {searchQuery && !isSearching && (
              <button
                type="button"
                onClick={() => {
                  setSearchQuery("");
                  setShowResultsDropdown(false);
                }}
                aria-label="Clear search"
                className={`absolute right-2 inline-flex h-6 w-6 items-center justify-center rounded ${isDark ? "text-slate-400 hover:text-slate-200" : "text-slate-400 hover:text-slate-600"}`}
              >
                <X className="w-3.5 h-3.5" aria-hidden="true" />
              </button>
            )}
          </div>

          {/* Autocomplete Results Dropdown */}
          {showResultsDropdown && searchResults.length > 0 && (
            <div className={`absolute top-full left-0 right-0 mt-1.5 border rounded-lg max-h-72 overflow-y-auto z-50 divide-y ${
              isDark
                ? "bg-slate-800 border-slate-600 divide-slate-700/50"
                : "bg-white border-slate-300 divide-slate-100"
            }`}>
              {searchResults.map((item, idx) => (
                <button
                  key={`${item.type}-${item.id}-${idx}`}
                  onClick={() => {
                    onSelectCoordinates(item.lat, item.lon);
                    setShowResultsDropdown(false);
                    setSearchQuery(item.title);
                  }}
                  className={`w-full text-left p-2.5 transition flex items-start space-x-2.5 group ${
                    isDark ? "hover:bg-slate-700/70" : "hover:bg-slate-50"
                  }`}
                >
                  <div className="mt-0.5">
                    {item.type === "place" ? (
                      <Building2 className="w-4 h-4 text-amber-500" />
                    ) : (
                      <MapPin className="w-4 h-4 text-emerald-500" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                      <span className={`font-semibold text-sm truncate ${
                        isDark ? "text-slate-200 group-hover:text-blue-400" : "text-slate-900 group-hover:text-blue-600"
                      }`}>
                        {item.title}
                      </span>
                      {hasMetres(item.distance_meters) && (
                        <span className={`text-[11px] tabular-nums whitespace-nowrap ml-2 ${muted}`}>
                          {item.distance_meters > 1000
                            ? `${(item.distance_meters / 1000).toFixed(1)} km`
                            : `${Math.round(item.distance_meters)} m`}
                        </span>
                      )}
                    </div>
                    <div className={`text-xs truncate ${muted}`}>
                      {item.subtitle}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Quick Location Shortcuts */}
        <div className="flex items-center gap-1.5 mt-2.5 overflow-x-auto pb-1 text-xs">
          <span className={`text-xs mr-1 ${muted}`}>Go to</span>
          <button type="button" onClick={() => onSelectCoordinates(-37.79044, 145.31252)} className={chip}>
            27 Cambridge Rd
          </button>
          <button type="button" onClick={() => onSelectCoordinates(-37.81005, 144.9587)} className={chip}>
            Melbourne CBD
          </button>
          <button type="button" onClick={() => onSelectCoordinates(-33.8848, 151.1960)} className={chip}>
            Sydney Broadway
          </button>
        </div>
      </div>

      {/* Main Content Area */}
      <div className={`flex-1 overflow-y-auto p-4 space-y-4 ${
        isDark ? "bg-slate-900" : "bg-[#f8f9fa]"
      }`}>
        {/* Selected address card: the address is the page's only h1 */}
        {selectedLocation && (
          <section aria-label="Selected address" aria-busy={isLoadingReverse} className={`border rounded-lg p-4 relative overflow-hidden ${
            isDark
              ? "bg-slate-800/70 border-slate-700/80 text-white"
              : "bg-white border-slate-200 text-slate-900"
          }`}>
            {!reverseResult && isLoadingReverse ? (
              <p role="status" className={`text-xs ${muted}`}>Reading the nearest G-NAF address point…</p>
            ) : (
              <>
                {address ? (
                  <>
                    <h1 className={`text-[20px] font-semibold leading-snug ${isDark ? "text-white" : "text-slate-900"}`}>
                      {address.full_address}
                    </h1>
                    <p className={`mt-0.5 text-xs ${muted}`}>
                      {[address.suburb, address.state, address.postcode].filter(Boolean).join(" ")}
                    </p>
                  </>
                ) : (
                  <p className={`text-xs ${muted}`}>No G-NAF address point within {REVERSE_RADIUS_M} m of the pin.</p>
                )}

                {/* Provenance line: match source, metres to the address point, lookup time */}
                {(provenance || usingFallbackGeo || showLookupMs) && (
                <p className={`mt-1.5 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs ${muted}`}>
                  {provenance && (
                    <>
                      <MapPin className="w-3.5 h-3.5 flex-shrink-0 text-emerald-600" aria-hidden="true" />
                      <span>{provenance}</span>
                    </>
                  )}
                  {usingFallbackGeo && (
                    <span
                      title="Go service unreachable. Served by the in-browser fallback; restart it with: task start:server"
                      className="rounded border border-orange-300 bg-orange-50 px-1.5 text-[11px] font-medium text-orange-700"
                    >
                      Fallback data
                    </span>
                  )}
                  {showLookupMs && (
                    <span className="ml-auto tabular-nums text-[11px]">{lookupMs} ms lookup</span>
                  )}
                </p>
                )}
              </>
            )}

            {/* Matched place (if the pin sits on a business) */}
            {place ? (
              <div className={`mt-3 pt-3 border-t ${isDark ? "border-slate-700/60" : "border-slate-200"}`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <h2 className={`text-sm font-semibold leading-snug ${isDark ? "text-white" : "text-slate-900"}`}>{place.name}</h2>
                    {place.category && (
                      <span className={`inline-block mt-1 text-[11px] px-2 py-0.5 rounded border ${
                        isDark ? "bg-slate-700/50 text-slate-300 border-slate-600" : "bg-blue-50 text-blue-700 border-blue-200"
                      }`}>
                        {place.category.replace(/_/g, " ")}
                      </span>
                    )}
                  </div>
                  {hasMetres(place.distance_meters) && (
                    <span className={`text-xs tabular-nums whitespace-nowrap ${muted}`}>
                      {formatMetres(place.distance_meters)} from the pin
                    </span>
                  )}
                </div>

                {place.address_text && (
                  <p className={`text-xs mt-2 flex items-center gap-1.5 ${isDark ? "text-slate-300" : "text-slate-600"}`}>
                    <Building2 className="w-3.5 h-3.5 text-slate-400 flex-shrink-0" aria-hidden="true" />
                    <span>{place.address_text}</span>
                  </p>
                )}

                {(place.brand || place.phone || place.website) && (
                  <div className="flex flex-wrap gap-2 mt-2">
                    {place.brand && (
                      <span className={`text-[11px] px-2 py-0.5 rounded ${
                        isDark ? "text-slate-400 bg-slate-700/50" : "text-slate-600 bg-slate-100"
                      }`}>
                        Brand: <b className={isDark ? "text-slate-200" : "text-slate-800"}>{place.brand}</b>
                      </span>
                    )}
                    {place.phone && (
                      <a
                        href={`tel:${place.phone}`}
                        className={`text-[11px] hover:underline flex items-center gap-1 px-2 py-0.5 rounded ${
                          isDark ? "text-blue-400 bg-slate-700/50" : "text-blue-700 bg-slate-100"
                        }`}
                      >
                        <Phone className="w-3 h-3" aria-hidden="true" />
                        <span>{place.phone}</span>
                      </a>
                    )}
                    {place.website && (
                      <a
                        href={place.website}
                        target="_blank"
                        rel="noopener noreferrer"
                        className={`text-[11px] hover:underline flex items-center gap-1 px-2 py-0.5 rounded ${
                          isDark ? "text-blue-400 bg-slate-700/50" : "text-blue-700 bg-slate-100"
                        }`}
                      >
                        <Globe className="w-3 h-3" aria-hidden="true" />
                        <span>Website</span>
                        <ExternalLink className="w-2.5 h-2.5" aria-hidden="true" />
                      </a>
                    )}
                  </div>
                )}
              </div>
            ) : null}

            {/* Property structures: what the enrichment pipeline read for this address; nothing renders without a document */}
            {address && enrichment && (
              <section aria-label="Property structures" className={`mt-3 pt-3 border-t text-xs ${isDark ? "border-slate-700/60" : "border-slate-200"}`}>
                <h2 className={`text-sm font-semibold leading-snug ${isDark ? "text-white" : "text-slate-900"}`}>Property structures</h2>
                {enrichment.summary && <p className={`mt-0.5 tabular-nums ${muted}`}>{enrichment.summary}</p>}
                <ul className="mt-1.5 space-y-1 tabular-nums">
                  {enrichment.roof && <li className="flex flex-wrap justify-between gap-x-3"><span>{enrichment.roof}</span>{enrichment.roofUrl && <Link href={enrichment.roofUrl} className={`underline underline-offset-2 ${muted}`}>Open</Link>}</li>}
                  {enrichment.structures.map(structure => <li key={structure.key} className="flex flex-wrap justify-between gap-x-3"><span>{structure.line}</span>{structure.url && <Link href={structure.url} className={`underline underline-offset-2 ${muted}`}>Open</Link>}</li>)}
                  {enrichment.solar && <li>{enrichment.solar}</li>}
                  {enrichment.lot.map(line => <li key={line}>{line}</li>)}
                </ul>
                {enrichment.allocation && <p className={`mt-1.5 leading-relaxed ${muted}`} title={enrichment.reason || undefined}>{enrichment.allocation}</p>}
                <p className={`mt-1 text-[11px] ${muted}`}>Agent hints, not labels; bands and areas are model estimates.</p>
              </section>
            )}

            {/* Primary action: the house review link owns analysis and training enrolment */}
            {address && !isLoadingReverse && <HouseReviewLink key={address.id} addressId={address.id} dark={isDark}/>}

            {/* Quiet pair: CRM editor and the Deep 3D Studio */}
            <div className="mt-3 grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => address && onOpenCrm?.(address)}
                disabled={!address}
                className={quietButton}
                title="Edit CRM metadata, boundaries and POIs for this address"
              >
                <ShieldCheck className="w-4 h-4 text-emerald-600" aria-hidden="true" />
                <span>Address CRM</span>
              </button>
              <button
                type="button"
                onClick={() => onOpenDeep3D?.()}
                className={quietButton}
                title="Open the monocular Deep 3D Studio for this site"
              >
                <Box className="w-4 h-4 text-blue-600" aria-hidden="true" />
                <span>Inspect in Deep 3D Studio</span>
              </button>
            </div>

            {/* Coordinates */}
            <div className={`mt-3.5 pt-3 border-t flex items-center justify-between text-xs font-mono p-2 rounded-md ${
              isDark
                ? "border-slate-700/60 text-slate-400 bg-slate-900/60"
                : "border-slate-200 text-slate-600 bg-slate-50"
            }`}>
              <span>
                {selectedLocation.lat.toFixed(6)}, {selectedLocation.lon.toFixed(6)}
              </span>
              <button
                type="button"
                onClick={handleCopyCoords}
                className="flex items-center gap-1 text-blue-600 hover:text-blue-700 transition"
              >
                {copiedCoords ? <Check className="w-3.5 h-3.5 text-emerald-500" aria-hidden="true" /> : <Copy className="w-3.5 h-3.5" aria-hidden="true" />}
                <span className="text-[11px]">{copiedCoords ? "Copied" : "Copy"}</span>
              </button>
            </div>
          </section>
        )}

        {/* Map layers */}
        <section className={`border rounded-lg p-3 ${
          isDark
            ? "bg-slate-800/50 border-slate-700/60"
            : "bg-white border-slate-200"
        }`}>
          <h2 className={`flex items-center gap-2 text-sm font-semibold ${
            isDark ? "text-slate-100" : "text-slate-800"
          }`}>
            <Layers className="w-3.5 h-3.5 text-blue-600" aria-hidden="true" />
            Map layers
          </h2>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
            <label className={layerToggle}>
              <input type="checkbox" checked={showPois} onChange={(e) => setShowPois(e.target.checked)} />
              <span className="w-2.5 h-2.5 rounded-sm bg-amber-500 rotate-45" aria-hidden="true" />
              Places
            </label>
            <label className={layerToggle}>
              <input type="checkbox" checked={showAddresses} onChange={(e) => setShowAddresses(e.target.checked)} />
              <span className="w-2 h-2 rounded-full bg-emerald-500" aria-hidden="true" />
              Address points
            </label>
          </div>
        </section>

        {/* Nearby places */}
        {reverseResult?.nearby_places && reverseResult.nearby_places.length > 0 && (
          <section>
            <h2 className={`mb-2 text-sm font-semibold ${
              isDark ? "text-slate-100" : "text-slate-800"
            }`}>Nearby places ({reverseResult.nearby_places.length})</h2>

            <div className="space-y-1.5 max-h-56 overflow-y-auto pr-1">
              {reverseResult.nearby_places.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => onSelectCoordinates(p.lat, p.lon)}
                  className={`w-full text-left p-2 rounded-md border transition flex items-center justify-between group ${
                    isDark
                      ? "bg-slate-800/40 hover:bg-slate-800 border-slate-700/50 hover:border-slate-600"
                      : "bg-white hover:bg-slate-50 border-slate-200 hover:border-blue-300"
                  }`}
                >
                  <div className="min-w-0 flex-1 pr-2">
                    <div className={`text-xs font-medium truncate ${
                      isDark ? "text-slate-200 group-hover:text-blue-400" : "text-slate-800 group-hover:text-blue-600"
                    }`}>
                      {p.name}
                    </div>
                    <div className={`text-[11px] truncate ${muted}`}>
                      {p.category?.replace(/_/g, " ") || p.address_text || "Place"}
                    </div>
                  </div>
                  {hasMetres(p.distance_meters) && (
                    <span className={`text-[11px] tabular-nums px-1.5 py-0.5 rounded whitespace-nowrap ${
                      isDark ? "text-slate-400 bg-slate-700/50" : "text-slate-600 bg-slate-100"
                    }`}>
                      {Math.round(p.distance_meters)} m
                    </span>
                  )}
                </button>
              ))}
            </div>
          </section>
        )}
      </div>

      {/* Footer: index status */}
      <div className={`p-3 border-t text-xs flex items-center justify-between gap-2 ${
        isDark
          ? "border-slate-800 bg-slate-950/80 text-slate-400"
          : "border-slate-200 bg-white text-slate-500"
      }`}>
        <div className="flex items-center gap-2 min-w-0">
          <span className="w-2 h-2 flex-none rounded-full bg-emerald-500" aria-hidden="true" />
          <span className="tabular-nums truncate">
            {formatCount(stats?.placeCount)} places, {formatCount(stats?.addressCount)} addresses indexed
          </span>
        </div>
        <span className={`tabular-nums text-[11px] whitespace-nowrap ${isDark ? "text-slate-500" : "text-slate-400"}`}>
          SQLite {stats?.dbSizeFormatted ?? "…"}
        </span>
      </div>

      {/* Ingestion Modal */}
      {isIngestModalOpen && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div role="dialog" aria-labelledby="offline-index-title" className={`border rounded-lg w-full max-w-lg p-5 space-y-4 ${
            isDark
              ? "bg-slate-900 border-slate-700 text-slate-100"
              : "bg-white border-slate-300 text-slate-900"
          }`}>
            <div className={`flex items-center justify-between border-b pb-3 ${
              isDark ? "border-slate-800" : "border-slate-200"
            }`}>
              <div className="flex items-center gap-2">
                <Database className="w-5 h-5 text-blue-600" aria-hidden="true" />
                <h3 id="offline-index-title" className="text-sm font-semibold">Manage the offline Australian index</h3>
              </div>
              <button
                type="button"
                onClick={() => setIsIngestModalOpen(false)}
                aria-label="Close"
                className={`inline-flex h-7 w-7 items-center justify-center rounded ${isDark ? "text-slate-400 hover:text-white" : "text-slate-400 hover:text-slate-800"}`}
              >
                <X className="w-4 h-4" aria-hidden="true" />
              </button>
            </div>

            <div className={`space-y-3 text-xs ${isDark ? "text-slate-300" : "text-slate-600"}`}>
              <p>
                Download and index Australian address cadastres (G-NAF) and commercial POIs directly into your local SQLite R*Tree.
              </p>

              <div>
                <label className={`block font-semibold mb-1 ${isDark ? "text-slate-400" : "text-slate-700"}`}>
                  Region preset
                </label>
                <select
                  value={ingestPreset}
                  onChange={(e) => setIngestPreset(e.target.value)}
                  disabled={isIngesting}
                  className={`w-full border rounded-md p-2 focus:outline-none focus:border-blue-500 ${
                    isDark ? "bg-slate-800 border-slate-700 text-slate-200" : "bg-slate-50 border-slate-200 text-slate-900"
                  }`}
                >
                  <option value="melbourne_cbd">Melbourne CBD & Southbank</option>
                  <option value="sydney_inner">Sydney Inner (Broadway, Surry Hills, Newtown, North Sydney)</option>
                  <option value="sydney_cbd">Sydney CBD Core</option>
                  <option value="brisbane_cbd">Brisbane CBD & South Bank</option>
                  <option value="perth_cbd">Perth CBD</option>
                </select>
              </div>

              <div>
                <label className={`block font-semibold mb-1 ${isDark ? "text-slate-400" : "text-slate-700"}`}>
                  Record limit (per type)
                </label>
                <select
                  value={ingestLimit}
                  onChange={(e) => setIngestLimit(parseInt(e.target.value, 10))}
                  disabled={isIngesting}
                  className={`w-full border rounded-md p-2 focus:outline-none focus:border-blue-500 ${
                    isDark ? "bg-slate-800 border-slate-700 text-slate-200" : "bg-slate-50 border-slate-200 text-slate-900"
                  }`}
                >
                  <option value="2000">2,000 places & 2,000 addresses (Quick ~15s)</option>
                  <option value="5000">5,000 places & 5,000 addresses (~30s)</option>
                  <option value="15000">15,000 places & 15,000 addresses (~1.5 min)</option>
                </select>
              </div>

              {/* Ingestion Logs Console */}
              {ingestLogs.length > 0 && (
                <div className={`rounded-md p-3 font-mono text-[11px] border max-h-40 overflow-y-auto space-y-1 ${
                  isDark
                    ? "bg-slate-950 border-slate-800 text-slate-300"
                    : "bg-slate-50 border-slate-200 text-slate-700"
                }`}>
                  {ingestLogs.map((log, i) => (
                    <div key={i} className="leading-tight">
                      {log}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className={`flex items-center justify-end gap-2 pt-2 border-t ${
              isDark ? "border-slate-800" : "border-slate-200"
            }`}>
              <button
                type="button"
                onClick={() => setIsIngestModalOpen(false)}
                disabled={isIngesting}
                className={`px-3 py-1.5 rounded-md border text-xs transition ${
                  isDark ? "bg-slate-800 hover:bg-slate-700 text-slate-300 border-slate-700" : "bg-white hover:bg-slate-100 text-slate-700 border-slate-300"
                }`}
              >
                Close
              </button>
              <button
                type="button"
                onClick={handleStartIngest}
                disabled={isIngesting}
                className="px-4 py-1.5 rounded-md bg-blue-600 hover:bg-blue-500 disabled:bg-blue-800 text-white text-xs font-semibold flex items-center gap-1.5 transition"
              >
                {isIngesting && <RefreshCw className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />}
                <span>{isIngesting ? "Indexing region…" : "Start ingestion"}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
