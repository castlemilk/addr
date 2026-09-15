"use client";

import React, { useState, useEffect } from "react";
import {
  Search,
  MapPin,
  Building2,
  Navigation,
  Compass,
  Zap,
  Globe,
  Phone,
  Layers,
  Database,
  RefreshCw,
  ExternalLink,
  Copy,
  Check,
  ChevronRight,
  ShieldCheck,
  Sun,
  Moon,
  Sparkles,
  Box,
} from "lucide-react";
import { ReverseGeocodeResult, SearchResultItem, DbStats } from "@/lib/geo-engine";
import { geoClient } from "@/lib/connect-client";

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
}

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

  return (
    <div className={`w-full md:w-[460px] h-full flex flex-col z-10 select-none shadow-xl transition-colors duration-200 ${
      isDark
        ? "bg-slate-900 border-r border-slate-800 text-slate-100"
        : "bg-white border-r border-slate-200 text-slate-900"
    }`}>
      {/* App Header */}
      <div className={`p-4 border-b transition-colors duration-200 ${
        isDark
          ? "border-slate-800 bg-slate-950/60 backdrop-blur"
          : "border-slate-200 bg-white/95 backdrop-blur"
      }`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-2.5">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center shadow-md shadow-blue-500/20">
              <Compass className="w-5 h-5 text-white" />
            </div>
            <div>
              <div className="flex items-center space-x-1.5">
                <span className={`font-bold text-base tracking-tight ${isDark ? "text-white" : "text-slate-900"}`}>
                  GeoAU Offline
                </span>
                <span className={`text-[10px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded border ${
                  isDark
                    ? "bg-blue-500/20 text-blue-400 border-blue-500/30"
                    : "bg-blue-50 text-blue-600 border-blue-200"
                }`}>
                  G-NAF + POI
                </span>
              </div>
              <p className={`text-xs ${isDark ? "text-slate-400" : "text-slate-500"}`}>
                100% Offline Australian Search & Cadastre
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-1.5">
            {onToggleTheme && (
              <button
                onClick={onToggleTheme}
                className={`p-1.5 rounded-lg transition flex items-center space-x-1 text-xs border ${
                  isDark
                    ? "bg-slate-800 hover:bg-slate-700 text-slate-300 border-slate-700/60"
                    : "bg-slate-100 hover:bg-slate-200 text-slate-700 border-slate-200"
                }`}
                title="Toggle Google Maps Light / Dark Mode"
              >
                {isDark ? <Sun className="w-3.5 h-3.5 text-amber-400" /> : <Moon className="w-3.5 h-3.5 text-indigo-500" />}
              </button>
            )}
            <button
              onClick={() => setIsIngestModalOpen(true)}
              className={`p-1.5 rounded-lg transition flex items-center space-x-1 text-xs border ${
                isDark
                  ? "bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border-slate-700/60"
                  : "bg-slate-100 hover:bg-slate-200 text-slate-700 hover:text-slate-900 border-slate-200"
              }`}
              title="Manage Offline Database"
            >
              <Database className="w-3.5 h-3.5 text-blue-500" />
              <span>Index</span>
            </button>
          </div>
        </div>

        {/* Search Bar */}
        <div className="relative mt-3">
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
                onClick={() => {
                  setSearchQuery("");
                  setShowResultsDropdown(false);
                }}
                className={`text-xs absolute right-3 ${isDark ? "text-slate-400 hover:text-slate-200" : "text-slate-400 hover:text-slate-600"}`}
              >
                ✕
              </button>
            )}
          </div>

          {/* Autocomplete Results Dropdown */}
          {showResultsDropdown && searchResults.length > 0 && (
            <div className={`absolute top-full left-0 right-0 mt-1.5 border rounded-xl shadow-2xl max-h-72 overflow-y-auto z-50 divide-y ${
              isDark
                ? "bg-slate-800 border-slate-700 divide-slate-700/50"
                : "bg-white border-slate-200 divide-slate-100"
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
                      {item.distance_meters != null && (
                        <span className={`text-[11px] whitespace-nowrap ml-2 ${isDark ? "text-slate-400" : "text-slate-500"}`}>
                          {item.distance_meters > 1000
                            ? `${(item.distance_meters / 1000).toFixed(1)}km`
                            : `${item.distance_meters}m`}
                        </span>
                      )}
                    </div>
                    <div className={`text-xs truncate ${isDark ? "text-slate-400" : "text-slate-500"}`}>
                      {item.subtitle}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Quick Location Shortcuts */}
        <div className="flex items-center space-x-1.5 mt-2.5 overflow-x-auto pb-1 text-xs">
          <span className={`text-[11px] uppercase font-bold tracking-wider mr-1 ${isDark ? "text-slate-500" : "text-slate-400"}`}>
            Jump:
          </span>
          <button
            onClick={() => onSelectCoordinates(-37.79044, 145.31252)}
            className={`px-2 py-1 rounded-md border text-[11px] whitespace-nowrap transition ${
              isDark
                ? "bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border-slate-700"
                : "bg-slate-100 hover:bg-slate-200 text-slate-700 hover:text-slate-900 border-slate-200"
            }`}
          >
            27 Cambridge Rd
          </button>
          <button
            onClick={() => onSelectCoordinates(-37.81005, 144.9587)}
            className={`px-2 py-1 rounded-md border text-[11px] whitespace-nowrap transition ${
              isDark
                ? "bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border-slate-700"
                : "bg-slate-100 hover:bg-slate-200 text-slate-700 hover:text-slate-900 border-slate-200"
            }`}
          >
            Melbourne CBD
          </button>
          <button
            onClick={() => onSelectCoordinates(-33.8848, 151.1960)}
            className={`px-2 py-1 rounded-md border text-[11px] whitespace-nowrap transition ${
              isDark
                ? "bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white border-slate-700"
                : "bg-slate-100 hover:bg-slate-200 text-slate-700 hover:text-slate-900 border-slate-200"
            }`}
          >
            Sydney Broadway
          </button>
        </div>
      </div>

      {/* Main Content Area */}
      <div className={`flex-1 overflow-y-auto p-4 space-y-4 ${
        isDark ? "bg-slate-900" : "bg-[#f8f9fa]"
      }`}>
        {/* Active Pin Card */}
        {selectedLocation && (
          <div className={`border rounded-2xl p-4 shadow-sm relative overflow-hidden backdrop-blur ${
            isDark
              ? "bg-slate-800/70 border-slate-700/80 text-white"
              : "bg-white border-slate-200 text-slate-900 shadow-slate-200/50"
          }`}>
            {/* Top Badge: Execution Time & Match Type */}
            <div className="flex items-center justify-between mb-2.5">
              <div className="flex items-center space-x-1.5">
                <span
                  className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full border ${
                    reverseResult?.match_type === "poi_and_address"
                      ? "bg-amber-500/20 text-amber-500 border-amber-500/30"
                      : reverseResult?.match_type === "poi_only"
                      ? "bg-purple-500/20 text-purple-500 border-purple-500/30"
                      : reverseResult?.match_type === "address_only"
                      ? "bg-emerald-500/20 text-emerald-600 border-emerald-500/30"
                      : isDark ? "bg-slate-700 text-slate-300 border-slate-600" : "bg-slate-100 text-slate-700 border-slate-200"
                  }`}
                >
                  {reverseResult?.match_type === "poi_and_address"
                    ? "POI + G-NAF Address"
                    : reverseResult?.match_type === "poi_only"
                    ? "POI Match"
                    : reverseResult?.match_type === "address_only"
                    ? "G-NAF Rooftop Address"
                    : "Nearest Map Point"}
                </span>
              </div>

              {reverseResult?.execution_time_ms != null && (
                <div className={`flex items-center space-x-1 text-[11px] font-mono px-2 py-0.5 rounded border ${
                  isDark
                    ? "text-emerald-400 bg-emerald-950/40 border-emerald-800/40"
                    : "text-emerald-700 bg-emerald-50 border-emerald-200"
                }`}>
                  <Zap className="w-3 h-3 text-emerald-500" />
                  <span>{reverseResult.execution_time_ms} ms</span>
                </div>
              )}
            </div>

            {/* POI Info (if present) */}
            {place ? (
              <div className={`mb-3.5 pb-3.5 border-b ${isDark ? "border-slate-700/60" : "border-slate-100"}`}>
                <div className="flex items-start justify-between">
                  <div>
                    <h2 className={`text-lg font-bold leading-snug ${isDark ? "text-white" : "text-slate-900"}`}>
                      {place.name}
                    </h2>
                    {place.category && (
                      <span className="inline-block mt-1 text-[11px] font-medium bg-blue-50 text-blue-600 px-2 py-0.5 rounded-md border border-blue-200">
                        {place.category.replace(/_/g, " ")}
                      </span>
                    )}
                  </div>
                  {place.distance_meters != null && (
                    <div className="text-right ml-2">
                      <div className={`text-xs font-semibold px-2 py-1 rounded-md border whitespace-nowrap ${
                        isDark
                          ? "text-amber-400 bg-amber-950/40 border-amber-800/40"
                          : "text-amber-700 bg-amber-50 border-amber-200"
                      }`}>
                        🎯 {place.distance_meters}m from pin
                      </div>
                    </div>
                  )}
                </div>

                {place.address_text && (
                  <p className={`text-xs mt-2 flex items-center space-x-1.5 ${isDark ? "text-slate-300" : "text-slate-600"}`}>
                    <MapPin className="w-3.5 h-3.5 text-slate-400 flex-shrink-0" />
                    <span>{place.address_text}</span>
                  </p>
                )}

                {/* Additional POI Details */}
                <div className={`flex flex-wrap gap-2 mt-2.5 pt-2 border-t ${isDark ? "border-slate-700/40" : "border-slate-100"}`}>
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
                      className={`text-[11px] hover:underline flex items-center space-x-1 px-2 py-0.5 rounded ${
                        isDark ? "text-blue-400 bg-slate-700/50" : "text-blue-600 bg-slate-100"
                      }`}
                    >
                      <Phone className="w-3 h-3" />
                      <span>{place.phone}</span>
                    </a>
                  )}
                  {place.website && (
                    <a
                      href={place.website}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={`text-[11px] hover:underline flex items-center space-x-1 px-2 py-0.5 rounded ${
                        isDark ? "text-blue-400 bg-slate-700/50" : "text-blue-600 bg-slate-100"
                      }`}
                    >
                      <Globe className="w-3 h-3" />
                      <span>Website</span>
                      <ExternalLink className="w-2.5 h-2.5" />
                    </a>
                  )}
                </div>
              </div>
            ) : null}

            {/* Cadastral G-NAF Address Info */}
            <div>
              <div className={`flex items-center space-x-1.5 mb-1 text-xs font-semibold uppercase tracking-wider ${
                isDark ? "text-slate-400" : "text-slate-500"
              }`}>
                <ShieldCheck className="w-3.5 h-3.5 text-emerald-500" />
                <span>Authoritative Cadastral Address</span>
              </div>

              {address ? (
                <div>
                  <div className={`text-sm font-semibold leading-snug ${isDark ? "text-slate-100" : "text-slate-900"}`}>
                    {address.full_address}
                  </div>
                  <div className={`flex items-center justify-between mt-1 text-xs ${isDark ? "text-slate-400" : "text-slate-500"}`}>
                    <span>
                      {[address.suburb, address.state, address.postcode].filter(Boolean).join(" ")}
                    </span>
                    {address.distance_meters != null && (
                      <span className="text-emerald-600 text-[11px] font-medium">
                        📍 {address.distance_meters}m to rooftop
                      </span>
                    )}
                  </div>
                </div>
              ) : (
                <div className="text-xs text-slate-400 italic">
                  No registered G-NAF rooftop within search radius.
                </div>
              )}

              {/* Deep 3D Satellite Model Inspector Trigger */}
              <div className="mt-3">
                <button
                  onClick={() => onOpenDeep3D?.()}
                  className={`w-full p-2.5 rounded-xl border flex items-center justify-between text-xs transition font-semibold shadow-sm ${
                    isDark
                      ? "bg-gradient-to-r from-blue-900/30 via-indigo-900/30 to-purple-900/30 hover:from-blue-900/50 hover:to-purple-900/50 border-blue-500/40 text-blue-300"
                      : "bg-gradient-to-r from-blue-50 via-indigo-50 to-purple-50 hover:from-blue-100 hover:to-purple-100 border-blue-200 text-blue-700"
                  }`}
                  title="Launch Monocular Deep 3D Elevation Studio"
                >
                  <div className="flex items-center space-x-2">
                    <Sparkles className="w-4 h-4 text-purple-500 animate-pulse" />
                    <span>Inspect in Deep 3D Studio</span>
                  </div>
                  <span className="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-500">
                    3D RELIEF
                  </span>
                </button>
              </div>
            </div>

            {/* Lat / Lon bar */}
            <div className={`mt-3.5 pt-3 border-t flex items-center justify-between text-xs font-mono p-2 rounded-lg ${
              isDark
                ? "border-slate-700/60 text-slate-400 bg-slate-900/60"
                : "border-slate-100 text-slate-600 bg-slate-50"
            }`}>
              <span>
                {selectedLocation.lat.toFixed(6)}, {selectedLocation.lon.toFixed(6)}
              </span>
              <button
                onClick={handleCopyCoords}
                className="flex items-center space-x-1 text-blue-500 hover:text-blue-600 transition"
              >
                {copiedCoords ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
                <span className="text-[11px]">{copiedCoords ? "Copied" : "Copy"}</span>
              </button>
            </div>
          </div>
        )}

        {/* Map Layers Toggle */}
        <div className={`border rounded-xl p-3 shadow-sm ${
          isDark
            ? "bg-slate-800/50 border-slate-700/60"
            : "bg-white border-slate-200"
        }`}>
          <div className={`flex items-center space-x-2 text-xs font-semibold uppercase tracking-wider mb-2.5 ${
            isDark ? "text-slate-300" : "text-slate-600"
          }`}>
            <Layers className="w-3.5 h-3.5 text-blue-500" />
            <span>Map Layers</span>
          </div>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <button
              onClick={() => setShowPois(!showPois)}
              className={`p-2 rounded-lg border flex items-center justify-between transition ${
                showPois
                  ? "bg-amber-500/10 border-amber-500/40 text-amber-600"
                  : isDark ? "bg-slate-800 border-slate-700 text-slate-400" : "bg-slate-50 border-slate-200 text-slate-400"
              }`}
            >
              <div className="flex items-center space-x-2">
                <div className="w-2.5 h-2.5 rounded-sm bg-amber-500 rotate-45" />
                <span>POIs & Places</span>
              </div>
              <span className="text-[10px] font-mono">{showPois ? "ON" : "OFF"}</span>
            </button>

            <button
              onClick={() => setShowAddresses(!showAddresses)}
              className={`p-2 rounded-lg border flex items-center justify-between transition ${
                showAddresses
                  ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-600"
                  : isDark ? "bg-slate-800 border-slate-700 text-slate-400" : "bg-slate-50 border-slate-200 text-slate-400"
              }`}
            >
              <div className="flex items-center space-x-2">
                <div className="w-2 h-2 rounded-full bg-emerald-500" />
                <span>G-NAF Points</span>
              </div>
              <span className="text-[10px] font-mono">{showAddresses ? "ON" : "OFF"}</span>
            </button>
          </div>
        </div>

        {/* Nearby Places Section */}
        {reverseResult?.nearby_places && reverseResult.nearby_places.length > 0 && (
          <div>
            <div className="flex items-center justify-between mb-2">
              <span className={`text-xs font-bold uppercase tracking-wider ${
                isDark ? "text-slate-300" : "text-slate-600"
              }`}>
                Nearby Places ({reverseResult.nearby_places.length})
              </span>
            </div>

            <div className="space-y-1.5 max-h-56 overflow-y-auto pr-1">
              {reverseResult.nearby_places.map((p) => (
                <button
                  key={p.id}
                  onClick={() => onSelectCoordinates(p.lat, p.lon)}
                  className={`w-full text-left p-2 rounded-xl border transition flex items-center justify-between group ${
                    isDark
                      ? "bg-slate-800/40 hover:bg-slate-800 border-slate-700/50 hover:border-slate-600"
                      : "bg-white hover:bg-slate-50 border-slate-200/90 hover:border-blue-300 shadow-sm"
                  }`}
                >
                  <div className="min-w-0 flex-1 pr-2">
                    <div className={`text-xs font-medium truncate ${
                      isDark ? "text-slate-200 group-hover:text-blue-400" : "text-slate-800 group-hover:text-blue-600"
                    }`}>
                      {p.name}
                    </div>
                    <div className={`text-[11px] truncate ${isDark ? "text-slate-400" : "text-slate-500"}`}>
                      {p.category?.replace(/_/g, " ") || p.address_text || "Place"}
                    </div>
                  </div>
                  {p.distance_meters != null && (
                    <span className={`text-[11px] px-1.5 py-0.5 rounded whitespace-nowrap ${
                      isDark ? "text-slate-400 bg-slate-700/50" : "text-slate-600 bg-slate-100"
                    }`}>
                      {p.distance_meters}m
                    </span>
                  )}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Footer: Database Status */}
      <div className={`p-3 border-t text-xs flex items-center justify-between ${
        isDark
          ? "border-slate-800 bg-slate-950/80 text-slate-400"
          : "border-slate-200 bg-white text-slate-500"
      }`}>
        <div className="flex items-center space-x-2">
          <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
          <span>
            <b>{stats?.placeCount.toLocaleString() ?? "..."}</b> POIs •{" "}
            <b>{stats?.addressCount.toLocaleString() ?? "..."}</b> Addrs
          </span>
        </div>
        <span className={`font-mono text-[11px] ${isDark ? "text-slate-500" : "text-slate-400"}`}>
          SQLite: {stats?.dbSizeFormatted ?? "1.5 MB"}
        </span>
      </div>

      {/* Ingestion Modal */}
      {isIngestModalOpen && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className={`border rounded-2xl w-full max-w-lg p-5 shadow-2xl space-y-4 ${
            isDark
              ? "bg-slate-900 border-slate-700 text-slate-100"
              : "bg-white border-slate-200 text-slate-900"
          }`}>
            <div className={`flex items-center justify-between border-b pb-3 ${
              isDark ? "border-slate-800" : "border-slate-100"
            }`}>
              <div className="flex items-center space-x-2">
                <Database className="w-5 h-5 text-blue-500" />
                <h3 className="text-base font-bold">Manage Offline Australian Index</h3>
              </div>
              <button
                onClick={() => setIsIngestModalOpen(false)}
                className={`text-sm ${isDark ? "text-slate-400 hover:text-white" : "text-slate-400 hover:text-slate-800"}`}
              >
                ✕
              </button>
            </div>

            <div className={`space-y-3 text-xs ${isDark ? "text-slate-300" : "text-slate-600"}`}>
              <p>
                Download and index Australian address cadastres (G-NAF) and commercial POIs directly into your local SQLite R*Tree.
              </p>

              <div>
                <label className={`block font-semibold mb-1 ${isDark ? "text-slate-400" : "text-slate-700"}`}>
                  Select Region Preset:
                </label>
                <select
                  value={ingestPreset}
                  onChange={(e) => setIngestPreset(e.target.value)}
                  disabled={isIngesting}
                  className={`w-full border rounded-lg p-2 focus:outline-none focus:border-blue-500 ${
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
                  Record Limit (per type):
                </label>
                <select
                  value={ingestLimit}
                  onChange={(e) => setIngestLimit(parseInt(e.target.value, 10))}
                  disabled={isIngesting}
                  className={`w-full border rounded-lg p-2 focus:outline-none focus:border-blue-500 ${
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
                <div className={`rounded-lg p-3 font-mono text-[11px] border max-h-40 overflow-y-auto space-y-1 ${
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

            <div className={`flex items-center justify-end space-x-2 pt-2 border-t ${
              isDark ? "border-slate-800" : "border-slate-100"
            }`}>
              <button
                onClick={() => setIsIngestModalOpen(false)}
                disabled={isIngesting}
                className={`px-3 py-1.5 rounded-lg text-xs transition ${
                  isDark ? "bg-slate-800 hover:bg-slate-700 text-slate-300" : "bg-slate-100 hover:bg-slate-200 text-slate-700"
                }`}
              >
                Close
              </button>
              <button
                onClick={handleStartIngest}
                disabled={isIngesting}
                className="px-4 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:bg-blue-800 text-white text-xs font-semibold flex items-center space-x-1.5 transition shadow-md shadow-blue-600/20"
              >
                {isIngesting && <RefreshCw className="w-3.5 h-3.5 animate-spin" />}
                <span>{isIngesting ? "Indexing Region..." : "Start Ingestion"}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
