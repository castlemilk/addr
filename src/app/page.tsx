"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import CrmPanel from "@/components/CrmPanel";
import { FeatureKind } from "@/gen/crm/v1/crm_pb";
import { crmClient, bboxPolygonToGeoJson, ROOF_BOX_LABEL } from "@/lib/crm-client";
import type { CrmDrawKind } from "@/lib/crm-client";
import { ReverseGeocodeResult, Place, Address } from "@/lib/geo-engine";
import { geoClient } from "@/lib/connect-client";
import { Loader2 } from "lucide-react";

// Dynamically import MapComponent to avoid SSR errors with Leaflet
const MapComponent = dynamic(() => import("@/components/MapComponent"), {
  ssr: false,
  loading: () => (
    <div className="w-full h-full flex flex-col items-center justify-center bg-slate-950 text-slate-400">
      <Loader2 className="w-8 h-8 animate-spin text-blue-500 mb-2" />
      <span className="text-sm font-medium">Loading the map…</span>
    </div>
  ),
});

export default function Home() {
  const router = useRouter();
  // Default to 27 Cambridge Road, Mooroolbark VIC 3138
  const [selectedLocation, setSelectedLocation] = useState<{ lat: number; lon: number }>({
    lat: -37.79044,
    lon: 145.31252,
  });
  const [reverseResult, setReverseResult] = useState<ReverseGeocodeResult | null>(null);
  const [isLoadingReverse, setIsLoadingReverse] = useState(false);
  // True when the last lookup fell back to the in-browser engine because the
  // Go service was unreachable (same rules, but served stale/locally).
  const [usingFallbackGeo, setUsingFallbackGeo] = useState(false);
  const [nearbyPlaces, setNearbyPlaces] = useState<Place[]>([]);
  const [nearbyAddresses, setNearbyAddresses] = useState<Address[]>([]);

  // Default to Google Maps Light Mode
  const [isDarkMode, setIsDarkMode] = useState(false);
  const toggleTheme = () => setIsDarkMode((prev) => !prev);

  // Layer toggles
  const [showPois, setShowPois] = useState(true);
  const [showAddresses, setShowAddresses] = useState(true);
  const [isDeep3DStudioOpen, setIsDeep3DStudioOpen] = useState(false);

  // Per-address CRM state
  const [crmTarget, setCrmTarget] = useState<{ addressId: bigint; fullAddress: string } | null>(null);
  const [crmDraw, setCrmDraw] = useState<{ kind: CrmDrawKind | "scene" } | null>(null);
  const [crmPending, setCrmPending] = useState<{
    kind: FeatureKind;
    addressId: bigint;
    geojson: string;
    label?: string;
    properties?: Record<string, string>;
  } | null>(null);
  const [crmEdit, setCrmEdit] = useState<{
    id: bigint;
    kind: FeatureKind;
    geojson: string;
    label: string | null;
  } | null>(null);
  const [crmRefresh, setCrmRefresh] = useState(0);
  // /?crm=1 opens the Address CRM for the address the first completed lookup resolves. The flag is
  // consumed by that lookup whether or not it found an address, so a later map click never opens
  // the panel unexpectedly.
  const crmDeepLink = useRef(false);
  const openCrmForDeepLink = useCallback((address: Address | null | undefined) => {
    if (!crmDeepLink.current) return;
    crmDeepLink.current = false;
    if (!address || !Number.isFinite(address.id)) return;
    setCrmTarget({ addressId: BigInt(address.id), fullAddress: address.full_address });
  }, []);

  const handleDrawComplete = useCallback(
    (ring: [number, number][]) => {
      const kind = crmDraw?.kind;
      setCrmDraw(null);
      if(kind === "scene") {
        const xs=ring.map(p=>p[0]),ys=ring.map(p=>p[1]);
        router.push(`/scenes/new?bounds=${[Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)].join(',')}`);
        return;
      }
      if (!kind || !crmTarget) return;
      const isBox = kind === "box";
      const featureKind =
        kind === "roof"
          ? FeatureKind.ROOF
          : kind === "tree"
          ? FeatureKind.TREE
          : FeatureKind.OBJECT;
      // Headless tooling hook: the latest drawn proposal (read back a drawn
      // box without CRM auth, e.g. in scripts/map-shot.mjs flows).
      if (isBox) {
        (window as unknown as { __addrProposal?: unknown }).__addrProposal = {
          addressId: crmTarget.addressId.toString(),
          fullAddress: crmTarget.fullAddress,
          label: ROOF_BOX_LABEL,
          ring,
          savedAt: new Date().toISOString(),
        };
      }
      setCrmPending({
        kind: featureKind,
        addressId: crmTarget.addressId,
        geojson: bboxPolygonToGeoJson(ring),
        ...(isBox
          ? { label: ROOF_BOX_LABEL, properties: { source: "proposal" } }
          : {}),
      });
    },
    [crmDraw, crmTarget, router]
  );

  const handleEditFeature = useCallback(
    (feature: {
      id: bigint;
      kind: FeatureKind;
      geometryJson: string;
      label: string | null;
    }) => {
      setCrmEdit({
        id: feature.id,
        kind: feature.kind,
        geojson: feature.geometryJson,
        label: feature.label,
      });
    },
    []
  );

  const handleEditSave = useCallback(
    async (ring: [number, number][]) => {
      const edit = crmEdit;
      const target = crmTarget;
      setCrmEdit(null);
      if (!edit || !target) return;
      try {
        await crmClient.upsertFeature({
          id: edit.id,
          addressId: target.addressId,
          kind: edit.kind,
          geometryJson: bboxPolygonToGeoJson(ring),
          label: edit.label ?? "",
        });
        setCrmRefresh((value) => value + 1);
      } catch (error) {
        console.error("Failed to save boundary edit:", error);
      }
    },
    [crmEdit, crmTarget]
  );

  const handleEditCancel = useCallback(() => setCrmEdit(null), []);

  // Fetch reverse geocode data using Connect-ES client
  const locationRequest = useRef(0);
  const handleLocationSelect = useCallback(async (lat: number, lon: number) => {
    const request = ++locationRequest.current;
    setSelectedLocation({ lat, lon });
    setIsLoadingReverse(true);

    try {
      // 1. Reverse geocoding lookup via Connect-ES client (talking to Go microservice)
      try {
        const revRes = await geoClient.reverseGeocode({
          latitude: lat,
          longitude: lon,
          maxRadiusMeters: 300,
        });

        if (request !== locationRequest.current) return;
        const result: ReverseGeocodeResult = {
          query: {
            lat: revRes.query?.latitude ?? lat,
            lon: revRes.query?.longitude ?? lon,
          },
          match_type: revRes.matchType as ReverseGeocodeResult["match_type"],
          place: revRes.place
            ? {
                id: Number(revRes.place.id),
                uuid: revRes.place.uuid,
                name: revRes.place.name,
                category: revRes.place.category ?? null,
                brand: revRes.place.brand ?? null,
                address_text: revRes.place.addressText ?? null,
                suburb: revRes.place.suburb ?? null,
                state: revRes.place.state ?? null,
                postcode: revRes.place.postcode ?? null,
                lat: revRes.place.latitude,
                lon: revRes.place.longitude,
                phone: revRes.place.phone ?? null,
                website: revRes.place.website ?? null,
                confidence: revRes.place.confidence ?? null,
                distance_meters: revRes.place.distanceMeters ?? undefined,
              }
            : null,
          address: revRes.address
            ? {
                id: Number(revRes.address.id),
                uuid: revRes.address.uuid,
                unit: revRes.address.unit ?? null,
                number: revRes.address.number ?? null,
                street: revRes.address.street ?? null,
                suburb: revRes.address.suburb ?? null,
                state: revRes.address.state ?? null,
                postcode: revRes.address.postcode ?? null,
                full_address: revRes.address.fullAddress,
                lat: revRes.address.latitude,
                lon: revRes.address.longitude,
                distance_meters: revRes.address.distanceMeters ?? undefined,
              }
            : null,
          formatted_title: revRes.formattedTitle,
          formatted_subtitle: revRes.formattedSubtitle,
          nearby_places: revRes.nearbyPlaces.map((p) => ({
            id: Number(p.id),
            uuid: p.uuid,
            name: p.name,
            category: p.category ?? null,
            brand: p.brand ?? null,
            address_text: p.addressText ?? null,
            suburb: p.suburb ?? null,
            state: p.state ?? null,
            postcode: p.postcode ?? null,
            lat: p.latitude,
            lon: p.longitude,
            phone: p.phone ?? null,
            website: p.website ?? null,
            confidence: p.confidence ?? null,
            distance_meters: p.distanceMeters ?? undefined,
          })),
          nearby_addresses: revRes.nearbyAddresses.map((a) => ({
            id: Number(a.id),
            uuid: a.uuid,
            unit: a.unit ?? null,
            number: a.number ?? null,
            street: a.street ?? null,
            suburb: a.suburb ?? null,
            state: a.state ?? null,
            postcode: a.postcode ?? null,
            full_address: a.fullAddress,
            lat: a.latitude,
            lon: a.longitude,
            distance_meters: a.distanceMeters ?? undefined,
          })),
          execution_time_ms: revRes.executionTimeMs,
        };
        setReverseResult(result);
        setUsingFallbackGeo(false);
        openCrmForDeepLink(result.address);
      } catch (connectErr) {
        if (request !== locationRequest.current) return;
        console.warn("Connect-ES reverseGeocode call failed, falling back to local route:", connectErr);
        const revRes = await fetch(`/api/reverse?lat=${lat}&lon=${lon}&radius=300`);
        const revData: ReverseGeocodeResult = await revRes.json();
        if (request !== locationRequest.current) return;
        if (!("error" in revData)) {
          setReverseResult(revData);
          setUsingFallbackGeo(true);
          openCrmForDeepLink(revData.address);
        }
      }

      // 2. Fetch nearby items for map markers via Connect-ES client
      try {
        const nearbyRes = await geoClient.getNearby({
          latitude: lat,
          longitude: lon,
          radiusMeters: 500,
          limit: 60,
        });

        if (request !== locationRequest.current) return;
        setNearbyPlaces(
          nearbyRes.places.map((p) => ({
            id: Number(p.id),
            uuid: p.uuid,
            name: p.name,
            category: p.category ?? null,
            brand: p.brand ?? null,
            address_text: p.addressText ?? null,
            suburb: p.suburb ?? null,
            state: p.state ?? null,
            postcode: p.postcode ?? null,
            lat: p.latitude,
            lon: p.longitude,
            phone: p.phone ?? null,
            website: p.website ?? null,
            confidence: p.confidence ?? null,
            distance_meters: p.distanceMeters ?? undefined,
          }))
        );

        setNearbyAddresses(
          nearbyRes.addresses.map((a) => ({
            id: Number(a.id),
            uuid: a.uuid,
            unit: a.unit ?? null,
            number: a.number ?? null,
            street: a.street ?? null,
            suburb: a.suburb ?? null,
            state: a.state ?? null,
            postcode: a.postcode ?? null,
            full_address: a.fullAddress,
            lat: a.latitude,
            lon: a.longitude,
            distance_meters: a.distanceMeters ?? undefined,
          }))
        );
      } catch (nearbyErr) {
        console.warn("Connect-ES getNearby call failed, falling back to local route:", nearbyErr);
        const nearbyRes = await fetch(`/api/nearby?lat=${lat}&lon=${lon}&radius=500&limit=60`);
        const nearbyData = await nearbyRes.json();
        if (request !== locationRequest.current) return;
        if (!nearbyData.error) {
          setNearbyPlaces(nearbyData.places || []);
          setNearbyAddresses(nearbyData.addresses || []);
        }
      }
    } catch (err) {
      console.error("Failed to query location:", err);
    } finally {
      if (request === locationRequest.current) {
        setIsLoadingReverse(false);
        // A failed or empty lookup still consumes the ?crm=1 deep link.
        crmDeepLink.current = false;
      }
    }
  }, [openCrmForDeepLink]);

  // Initial load
  useEffect(() => {
    handleLocationSelect(selectedLocation.lat, selectedLocation.lon);
  }, []);

  // Deep links for tooling: /?lat=..&lon=..&studio=1 renders the studio for a site
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const latParam = params.get("lat");
    const lonParam = params.get("lon");
    if (latParam && lonParam) {
      const lat = Number.parseFloat(latParam);
      const lon = Number.parseFloat(lonParam);
      if (Number.isFinite(lat) && Number.isFinite(lon)) {
        handleLocationSelect(lat, lon);
      }
    }
    if (params.get("studio") === "1") {
      window.setTimeout(() => setIsDeep3DStudioOpen(true), 1800);
    }
    if(params.get("selectScene") === "1") setCrmDraw({kind:"scene"});
    if (params.get("crm") === "1") crmDeepLink.current = true;
  }, [handleLocationSelect]);

  return (
    <main className={`flex flex-col md:flex-row h-screen w-screen overflow-hidden transition-colors duration-200 ${
      isDarkMode ? "bg-slate-950 text-slate-100" : "bg-[#f8f9fa] text-[#202124]"
    }`}>
      {/* Sidebar for Search & POI/Address Display */}
      <Sidebar
        selectedLocation={selectedLocation}
        reverseResult={reverseResult}
        isLoadingReverse={isLoadingReverse}
        onSelectCoordinates={handleLocationSelect}
        showPois={showPois}
        setShowPois={setShowPois}
        showAddresses={showAddresses}
        setShowAddresses={setShowAddresses}
        isDarkMode={isDarkMode}
        onToggleTheme={toggleTheme}
        onOpenDeep3D={() => setIsDeep3DStudioOpen(true)}
        onOpenCrm={(address) =>
          setCrmTarget({ addressId: BigInt(address.id), fullAddress: address.full_address })
        }
        usingFallbackGeo={usingFallbackGeo}
      />

      {/* Main Map View */}
      <div className="flex-1 h-[50vh] md:h-full relative">
        <MapComponent
          selectedLocation={selectedLocation}
          onLocationSelect={handleLocationSelect}
          nearbyPlaces={nearbyPlaces}
          nearbyAddresses={nearbyAddresses}
          showPois={showPois}
          showAddresses={showAddresses}
          isDarkMode={isDarkMode}
          onToggleTheme={toggleTheme}
          isDeep3DStudioOpen={isDeep3DStudioOpen}
          setIsDeep3DStudioOpen={setIsDeep3DStudioOpen}
          crmDraw={crmDraw}
          onRequestScene={() => setCrmDraw({kind:"scene"})}
          crmEdit={crmEdit ? { id: crmEdit.id, geojson: crmEdit.geojson } : null}
          onCrmEditSave={handleEditSave}
          onCrmEditCancel={handleEditCancel}
          onCrmDrawComplete={handleDrawComplete}
          onCrmDrawCancel={() => setCrmDraw(null)}
          proposalAddressId={crmTarget?.addressId ?? null}
          proposalRefresh={crmRefresh}
        />

        {/* Floating Instruction Badge (Bottom Center) */}
        <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-10 pointer-events-none hidden sm:block">
          <div className={`backdrop-blur-md px-3.5 py-1.5 rounded-full shadow-lg text-xs flex items-center space-x-2 border ${
            isDarkMode
              ? "bg-slate-900/80 border-slate-700/80 text-slate-300"
              : "bg-white/90 border-slate-200 text-slate-600 shadow-slate-200/50"
          }`}>
            <span className="w-2 h-2 rounded-full bg-blue-500" aria-hidden="true" />
            <span>Click anywhere on the map to look up the nearest address</span>
          </div>
        </div>
      </div>

      {/* Per-address CRM editor */}
      <CrmPanel
        isOpen={crmTarget !== null && crmDraw === null && crmEdit === null}
        addressId={crmTarget?.addressId ?? null}
        fullAddress={crmTarget?.fullAddress ?? ""}
        currentLocation={selectedLocation}
        refreshToken={crmRefresh}
        pendingGeometry={crmPending}
        onGeometryConsumed={() => {
          setCrmPending(null);
          setCrmRefresh((value) => value + 1);
        }}
        onRequestDraw={(kind) => setCrmDraw({ kind })}
        onEditFeature={handleEditFeature}
        editingFeatureId={crmEdit ? crmEdit.id.toString() : null}
        onClose={() => {
          setCrmTarget(null);
          setCrmDraw(null);
        }}
      />
    </main>
  );
}
