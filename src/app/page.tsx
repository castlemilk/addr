"use client";

import React, { useState, useEffect, useCallback } from "react";
import dynamic from "next/dynamic";
import Sidebar from "@/components/Sidebar";
import { ReverseGeocodeResult, Place, Address } from "@/lib/geo-engine";
import { geoClient } from "@/lib/connect-client";
import { Loader2 } from "lucide-react";

// Dynamically import MapComponent to avoid SSR errors with Leaflet
const MapComponent = dynamic(() => import("@/components/MapComponent"), {
  ssr: false,
  loading: () => (
    <div className="w-full h-full flex flex-col items-center justify-center bg-slate-950 text-slate-400">
      <Loader2 className="w-8 h-8 animate-spin text-blue-500 mb-2" />
      <span className="text-sm font-medium">Loading Offline Map Engine...</span>
    </div>
  ),
});

export default function Home() {
  // Default to 27 Cambridge Road, Mooroolbark VIC 3138
  const [selectedLocation, setSelectedLocation] = useState<{ lat: number; lon: number }>({
    lat: -37.79044,
    lon: 145.31252,
  });
  const [reverseResult, setReverseResult] = useState<ReverseGeocodeResult | null>(null);
  const [isLoadingReverse, setIsLoadingReverse] = useState(false);
  const [nearbyPlaces, setNearbyPlaces] = useState<Place[]>([]);
  const [nearbyAddresses, setNearbyAddresses] = useState<Address[]>([]);

  // Default to Google Maps Light Mode
  const [isDarkMode, setIsDarkMode] = useState(false);
  const toggleTheme = () => setIsDarkMode((prev) => !prev);

  // Layer toggles
  const [showPois, setShowPois] = useState(true);
  const [showAddresses, setShowAddresses] = useState(true);
  const [isDeep3DStudioOpen, setIsDeep3DStudioOpen] = useState(false);

  // Fetch reverse geocode data using Connect-ES client
  const handleLocationSelect = useCallback(async (lat: number, lon: number) => {
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

        setReverseResult({
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
        });
      } catch (connectErr) {
        console.warn("Connect-ES reverseGeocode call failed, falling back to local route:", connectErr);
        const revRes = await fetch(`/api/reverse?lat=${lat}&lon=${lon}&radius=300`);
        const revData: ReverseGeocodeResult = await revRes.json();
        if (!("error" in revData)) {
          setReverseResult(revData);
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
        if (!nearbyData.error) {
          setNearbyPlaces(nearbyData.places || []);
          setNearbyAddresses(nearbyData.addresses || []);
        }
      }
    } catch (err) {
      console.error("Failed to query location:", err);
    } finally {
      setIsLoadingReverse(false);
    }
  }, []);

  // Initial load
  useEffect(() => {
    handleLocationSelect(selectedLocation.lat, selectedLocation.lon);
  }, []);

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
        />

        {/* Floating Instruction Badge (Bottom Center) */}
        <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-10 pointer-events-none hidden sm:block">
          <div className={`backdrop-blur-md px-3.5 py-1.5 rounded-full shadow-lg text-xs flex items-center space-x-2 border ${
            isDarkMode
              ? "bg-slate-900/80 border-slate-700/80 text-slate-300"
              : "bg-white/90 border-slate-200 text-slate-600 shadow-slate-200/50"
          }`}>
            <span className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
            <span>Connect-Go Microservice • Click anywhere on map to reverse-geocode</span>
          </div>
        </div>
      </div>
    </main>
  );
}
