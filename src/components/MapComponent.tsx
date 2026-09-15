"use client";

import React, { useEffect, useRef, useState, useCallback } from "react";
import * as maplibregl from "maplibre-gl";
import { Place, Address } from "@/lib/geo-engine";
import { createGoogleMapsStyle, MapLayerMode, SatelliteVintage } from "@/lib/google-maps-style";
import { registerAiSatelliteProtocol } from "@/lib/ml-satellite-enhancer";
import { RealRoofLayer } from "@/lib/real-roof-layer";
import {
  Layers,
  Box,
  Crosshair,
  Globe,
  Check,
  Building2,
  Sparkles,
  Trees,
  Home,
  Clock,
  RotateCw,
  RotateCcw,
} from "lucide-react";
import Deep3DStudio from "@/components/Deep3DStudio";
import { Deep3DBuildingData } from "@/lib/deep-3d-model";
import { findAddressRoof, loadAddressRoofs } from "@/lib/address-roofs";

interface MapComponentProps {
  selectedLocation: { lat: number; lon: number } | null;
  onLocationSelect: (lat: number, lon: number) => void;
  nearbyPlaces: Place[];
  nearbyAddresses: Address[];
  showPois: boolean;
  showAddresses: boolean;
  isDarkMode?: boolean;
  onToggleTheme?: () => void;
  onQuickFilter?: (category: string) => void;
  isDeep3DStudioOpen?: boolean;
  setIsDeep3DStudioOpen?: (open: boolean) => void;
}

function ringContains(lon: number, lat: number, ring: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (
      yi > lat !== yj > lat &&
      lon < ((xj - xi) * (lat - yi)) / (yj - yi || 1e-12) + xi
    ) {
      inside = !inside;
    }
  }
  return inside;
}

function lngLatToMetres(lon: number, lat: number, refLon: number, refLat: number) {
  const cosLat = Math.cos((refLat * Math.PI) / 180);
  return [(lon - refLon) * 111320 * cosLat, (lat - refLat) * 111320] as [number, number];
}

function buildingFromFeature(
  polygon: { 0: [number, number][] },
  properties: Record<string, unknown>
): Deep3DBuildingData | null {
  const ring = polygon[0];
  if (!ring || ring.length < 3) return null;
  const centroidLon = ring.reduce((sum, [x]) => sum + x / ring.length, 0);
  const centroidLat = ring.reduce((sum, [, y]) => sum + y / ring.length, 0);
  const ringMeters = ring.map(([x, y]) =>
    lngLatToMetres(x, y, centroidLon, centroidLat)
  );
  const xs = ringMeters.map(([x]) => x);
  const ys = ringMeters.map(([, y]) => y);
  const widthMeters = Math.max(1, Math.max(...xs) - Math.min(...xs));
  const depthMeters = Math.max(1, Math.max(...ys) - Math.min(...ys));
  const numeric = (value: unknown, fallback = 0): number => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  const height = numeric(properties.render_height ?? properties.height, 0);
  if (height < 2) return null;
  const roofTypeRaw = String(properties.roof_type ?? "hipped");
  const roofType =
    roofTypeRaw === "flat" || roofTypeRaw === "gabled" ? roofTypeRaw : "hipped";
  return {
    height,
    eaveHeight: numeric(properties.eave_height, height * 0.6),
    ridgeHeight: numeric(properties.ridge_height, height),
    roofType,
    ridgeAngle:
      properties.ridge_angle != null && Number.isFinite(Number(properties.ridge_angle))
        ? Number(properties.ridge_angle)
        : null,
    areaM2: numeric(properties.area_m2, widthMeters * depthMeters),
    levels: properties.levels != null ? numeric(properties.levels, 0) || null : null,
    kind: properties.kind != null ? String(properties.kind) : null,
    centroid: { lat: centroidLat, lon: centroidLon },
    ringMeters,
    widthMeters,
    depthMeters,
  };
}

function findBuildingAt(
  map: maplibregl.Map,
  lon: number,
  lat: number
): Deep3DBuildingData | null {
  let features: maplibregl.GeoJSONFeature[] = [];
  try {
    features = map.querySourceFeatures("local-vector", { sourceLayer: "building" });
  } catch {
    return null;
  }
  let nearest: { distance: number; building: Deep3DBuildingData } | null = null;
  const seen = new Set<string>();
  for (const feature of features) {
    const geometry = feature.geometry as unknown as
      | { type: "Polygon"; coordinates: [number, number][][] }
      | { type: "MultiPolygon"; coordinates: [number, number][][][] };
    const polygons =
      geometry?.type === "Polygon"
        ? [geometry.coordinates]
        : geometry?.type === "MultiPolygon"
        ? geometry.coordinates
        : [];
    for (const polygon of polygons) {
      const ring = polygon[0] as unknown as [number, number][];
      if (!ring || ring.length < 3) continue;
      const cx = ring.reduce((sum, [x]) => sum + x / ring.length, 0);
      const cy = ring.reduce((sum, [, y]) => sum + y / ring.length, 0);
      const key = `${Math.round(cx * 1e5)},${Math.round(cy * 1e5)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const building = buildingFromFeature(
        polygon as unknown as { 0: [number, number][] },
        feature.properties ?? {}
      );
      if (!building) continue;
      if (ringContains(lon, lat, ring)) {
        return building;
      }
      let minDistance = Infinity;
      for (const [x, y] of ring) {
        const [mx, my] = lngLatToMetres(x, y, lon, lat);
        minDistance = Math.min(minDistance, Math.hypot(mx, my));
      }
      if (!nearest || minDistance < nearest.distance) {
        nearest = { distance: minDistance, building };
      }
    }
  }
  if (nearest && nearest.distance < 35) return nearest.building;
  return null;
}

export default function MapComponent({
  selectedLocation,
  onLocationSelect,
  nearbyPlaces,
  nearbyAddresses,
  showPois,
  showAddresses,
  isDarkMode: externalDarkMode,
  onToggleTheme: externalToggleTheme,
  onQuickFilter,
  isDeep3DStudioOpen: externalDeep3DOpen,
  setIsDeep3DStudioOpen: externalSetDeep3DOpen,
}: MapComponentProps) {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstanceRef = useRef<maplibregl.Map | null>(null);
  const selectedMarkerRef = useRef<maplibregl.Marker | null>(null);
  const poiMarkersRef = useRef<maplibregl.Marker[]>([]);
  const addressMarkersRef = useRef<maplibregl.Marker[]>([]);
  const roofLayerRef = useRef<RealRoofLayer | null>(null);

  // Layer mode state: streets | satellite | topography | dark
  const [layerMode, setLayerMode] = useState<MapLayerMode>("streets");
  const [satelliteVintage, setSatelliteVintage] = useState<SatelliteVintage>("google-latest");
  const [show3DBuildings, setShow3DBuildings] = useState(true);
  const [enableAiSatellite, setEnableAiSatellite] = useState(true);
  const [enableLoD2Houses, setEnableLoD2Houses] = useState(true);
  const [enableTrees, setEnableTrees] = useState(true);
  const [isLayersMenuOpen, setIsLayersMenuOpen] = useState(false);
  const [deep3DBuilding, setDeep3DBuilding] = useState<Deep3DBuildingData | null>(null);

  // Deep 3D Studio modal state
  const [internalDeep3DOpen, setInternalDeep3DOpen] = useState(false);
  const isDeep3DOpen = externalDeep3DOpen !== undefined ? externalDeep3DOpen : internalDeep3DOpen;
  const setIsDeep3DOpen = externalSetDeep3DOpen || setInternalDeep3DOpen;

  // 3D Tilt state (Default to 3D perspective ON so buildings and trees are immediately visible)
  const [is3DMode, setIs3DMode] = useState(true);

  const tileServiceUrl = process.env.NEXT_PUBLIC_GEO_SERVICE_URL || "http://localhost:8085";

  // Register AI Satellite Protocol and a bundle-safe MapLibre worker once
  useEffect(() => {
    maplibregl.setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");
    registerAiSatelliteProtocol(maplibregl);
  }, []);

  // Sync external dark mode with layerMode
  useEffect(() => {
    if (externalDarkMode !== undefined) {
      setLayerMode(externalDarkMode ? "dark" : "streets");
    }
  }, [externalDarkMode]);

  // Ensure the real roof-mesh layer is attached and rebuilt from vector tiles
  const ensureRoofLayerRef = useRef<() => void>(() => {});
  const ensureRoofLayer = useCallback(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    if (!map.isStyleLoaded()) {
      map.once("idle", () => ensureRoofLayerRef.current());
      return;
    }

    if (!roofLayerRef.current) {
      roofLayerRef.current = new RealRoofLayer();
    }

    if (!map.getLayer(roofLayerRef.current.id)) {
      try {
        map.addLayer(roofLayerRef.current);
      } catch (err) {
        console.warn("Could not attach real roof layer:", err);
        map.once("idle", () => ensureRoofLayerRef.current());
        return;
      }
    }

    const visible = show3DBuildings && enableLoD2Houses;
    roofLayerRef.current.setVisible(visible);
    if (visible) {
      roofLayerRef.current.rebuild();
    }
  }, [show3DBuildings, enableLoD2Houses]);

  useEffect(() => {
    ensureRoofLayerRef.current = ensureRoofLayer;
  }, [ensureRoofLayer]);

  // Initialize MapLibre GL instance
  useEffect(() => {
    if (!mapContainerRef.current || mapInstanceRef.current) return;

    registerAiSatelliteProtocol(maplibregl);

    const initialLat = selectedLocation?.lat ?? -37.79044; // Default: 27 Cambridge Rd, Mooroolbark VIC
    const initialLon = selectedLocation?.lon ?? 145.31252;

    const style = createGoogleMapsStyle(tileServiceUrl, layerMode, show3DBuildings, enableAiSatellite, satelliteVintage, enableTrees);

    const map = new maplibregl.Map({
      container: mapContainerRef.current,
      style: style,
      center: [initialLon, initialLat],
      zoom: 17,
      pitch: 58,
      bearing: -24,
      attributionControl: false,
    });

    // Add navigation controls (Zoom + Bearing)
    map.addControl(new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }), "bottom-right");
    map.addControl(
      new maplibregl.AttributionControl({
        compact: true,
        customAttribution: "Google Maps Style • AI Satellite • LoD2 3D • 100% Offline",
      }),
      "bottom-left"
    );

    // Map click handler: show AI uplift metadata for buildings/trees, else reverse geocode
    map.on("click", (e) => {
      const upliftLayers = [
        "building-3d-vector",
        "building-2d",
        "tree-3d-vector",
        "tree-2d",
      ].filter((layerId) => map.getLayer(layerId));
      const features = upliftLayers.length
        ? map.queryRenderedFeatures(e.point, { layers: upliftLayers })
        : [];
      if (features.length > 0) {
        const props = features[0].properties ?? {};
        const isTree = String(props.kind ?? "") === "tree";
        const height = Number(props.render_height ?? props.height ?? 0);
        const area = Number(props.area_m2 ?? 0);
        const levels =
          Number(props.levels ?? 0) ||
          (height > 0 ? Math.max(1, Math.round(height / 3.1)) : 0);
        const source = String(props.source ?? "ai");
        const roofType = String(props.roof_type ?? "");
        const eaveHeight = Number(props.eave_height ?? 0);
        const ridgeHeightValue = Number(props.ridge_height ?? height);
        const sourceLabel =
          source === "osm"
            ? "OpenStreetMap"
            : source === "osm+ml"
            ? "OSM + AI height"
            : "AI (imagery + topography)";
        new maplibregl.Popup({ offset: 14, closeButton: true })
          .setLngLat(e.lngLat)
          .setHTML(`
            <div style="font-family: system-ui, -apple-system, sans-serif; min-width: 200px;">
              <div style="font-weight: 700; color: #202124; font-size: 14px; margin-bottom: 2px;">
                ${isTree ? "🌳 Tree canopy" : "🏢 Building footprint"}
              </div>
              <div style="display: inline-block; font-size: 10px; font-weight: 600; background: #e8f0fe; color: #1a73e8; padding: 2px 8px; border-radius: 12px; margin-bottom: 6px;">
                ${sourceLabel}
              </div>
              <div style="font-size: 12px; color: #3c4043; line-height: 1.6;">
                ${height > 0 ? `<div><b>Est. height:</b> ${height.toFixed(1)} m</div>` : ""}
                ${!isTree && roofType ? `<div><b>Roof model:</b> ${roofType}${eaveHeight > 0 ? ` (eave ${eaveHeight.toFixed(1)} m, ridge ${ridgeHeightValue.toFixed(1)} m)` : ""}</div>` : ""}
                ${!isTree && levels > 0 ? `<div><b>Est. floors:</b> ~${levels} (${(height ? height / 3.1 : 3).toFixed(1)} m/level)</div>` : ""}
                ${area > 0 ? `<div><b>Footprint area:</b> ${area.toFixed(0)} m²</div>` : ""}
                ${area > 0 && height > 0 && !isTree ? `<div><b>Est. volume:</b> ~${(area * height).toFixed(0)} m³</div>` : ""}
              </div>
            </div>
          `)
          .addTo(map);
        return;
      }
      onLocationSelect(e.lngLat.lat, e.lngLat.lng);
    });

    // Load initial 3D layers and rebuild roof meshes after panning settles
    map.on("load", () => {
      ensureRoofLayer();
    });
    map.on("idle", () => {
      const roofLayer = roofLayerRef.current;
      if (!roofLayer || !map.getLayer(roofLayer.id)) {
        ensureRoofLayer();
        return;
      }
      roofLayer.rebuild();
    });

    mapInstanceRef.current = map;

    return () => {
      map.remove();
      mapInstanceRef.current = null;
    };
  }, []);

  // Update style when layer mode, 3D toggles, or satellite settings change
  const styleInitializedRef = useRef(false);
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;
    if (!styleInitializedRef.current) {
      styleInitializedRef.current = true;
      return;
    }

    map.setStyle(
      createGoogleMapsStyle(
        tileServiceUrl,
        layerMode,
        show3DBuildings && !enableLoD2Houses,
        enableAiSatellite,
        satelliteVintage,
        enableTrees
      )
    );
    map.once("idle", () => {
      ensureRoofLayer();
    });
  }, [layerMode, show3DBuildings, enableLoD2Houses, enableAiSatellite, satelliteVintage, enableTrees, tileServiceUrl, ensureRoofLayer]);

  // Rebuild the roof mesh layer when visibility toggles
  useEffect(() => {
    ensureRoofLayer();
  }, [ensureRoofLayer]);

  // Feed the selected address's extracted roof record (per-address pipeline)
  // into the Deep 3D Studio, falling back to the tile footprint lookup.
  useEffect(() => {
    if (!isDeep3DOpen) return;
    const map = mapInstanceRef.current;
    const lat = selectedLocation?.lat ?? -37.79044;
    const lon = selectedLocation?.lon ?? 145.31252;
    let cancelled = false;
    loadAddressRoofs().then((records) => {
      if (cancelled) return;
      const match = findAddressRoof(records, lon, lat);
      if (match) {
        setDeep3DBuilding(match.building);
        return;
      }
      setDeep3DBuilding(map ? findBuildingAt(map, lon, lat) : null);
    });
    return () => {
      cancelled = true;
    };
  }, [isDeep3DOpen, selectedLocation]);

  // Toggle 3D building perspective
  const toggle3D = () => {
    const map = mapInstanceRef.current;
    if (!map) return;
    const next3D = !is3DMode;
    setIs3DMode(next3D);
    map.easeTo({
      pitch: next3D ? 58 : 0,
      bearing: next3D ? -24 : 0,
      duration: 800,
    });
    if (next3D) {
      setShow3DBuildings(true);
      setEnableLoD2Houses(true);
    }
  };

  // Rotate the camera bearing (works in both 2D and 3D modes)
  const rotateMap = (deltaDegrees: number) => {
    const map = mapInstanceRef.current;
    if (!map) return;
    map.easeTo({
      bearing: map.getBearing() + deltaDegrees,
      duration: 350,
      essential: true,
    });
  };

  // Recenter map on selected location
  const recenterMap = () => {
    const map = mapInstanceRef.current;
    if (!map || !selectedLocation) return;
    map.flyTo({
      center: [selectedLocation.lon, selectedLocation.lat],
      zoom: 16.5,
      essential: true,
    });
  };

  // Sync selected location marker & camera
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map || !selectedLocation) return;

    if (!selectedMarkerRef.current) {
      const el = document.createElement("div");
      el.className = "google-pin-container google-pin-drop";
      el.style.cursor = "pointer";
      el.innerHTML = `
        <div style="position: relative; width: 36px; height: 50px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end;">
          <!-- Realistic ground contact shadow -->
          <div style="position: absolute; bottom: 0; width: 16px; height: 6px; background: rgba(0, 0, 0, 0.35); border-radius: 50%; filter: blur(1.5px);"></div>
          <!-- Google Maps Iconic Red Pin -->
          <svg width="34" height="46" viewBox="0 0 34 46" fill="none" style="filter: drop-shadow(0 3px 6px rgba(0,0,0,0.35));">
            <path d="M17 0C7.61 0 0 7.61 0 17C0 29.75 17 46 17 46C17 46 34 29.75 34 17C34 7.61 26.39 0 17 0Z" fill="url(#google_pin_grad)"/>
            <circle cx="17" cy="17" r="6.8" fill="#FFFFFF"/>
            <circle cx="17" cy="17" r="3.4" fill="#B31412"/>
            <defs>
              <linearGradient id="google_pin_grad" x1="17" y1="0" x2="17" y2="46" gradientUnits="userSpaceOnUse">
                <stop stop-color="#EA4335"/>
                <stop offset="1" stop-color="#D93025"/>
              </linearGradient>
            </defs>
          </svg>
        </div>
      `;
      selectedMarkerRef.current = new maplibregl.Marker({
        element: el,
        anchor: "bottom",
      })
        .setLngLat([selectedLocation.lon, selectedLocation.lat])
        .addTo(map);
    } else {
      selectedMarkerRef.current.setLngLat([selectedLocation.lon, selectedLocation.lat]);
    }

    const currentCenter = map.getCenter();
    const dist = Math.hypot(
      currentCenter.lat - selectedLocation.lat,
      currentCenter.lng - selectedLocation.lon
    );
    if (dist > 0.002) {
      map.flyTo({
        center: [selectedLocation.lon, selectedLocation.lat],
        zoom: Math.max(map.getZoom(), 16.5),
        essential: true,
      });
    }
  }, [selectedLocation]);

  // Render POI markers
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    poiMarkersRef.current.forEach((m) => m.remove());
    poiMarkersRef.current = [];

    if (!showPois) return;

    nearbyPlaces.forEach((place) => {
      const isFood = place.category && /cafe|restaurant|coffee|bar|bakery|food|dining/i.test(place.category);
      const isShop = place.category && /shop|store|retail|supermarket|clothing/i.test(place.category);
      const isPark = place.category && /park|garden|reserve|recreation/i.test(place.category);
      const isHealth = place.category && /hospital|doctor|clinic|pharmacy|medical/i.test(place.category);

      const bgColor = isFood ? "#ea4335" : isShop ? "#1a73e8" : isPark ? "#1e8e3e" : isHealth ? "#e37400" : "#8ab4f8";
      const iconEmoji = isFood ? "🍽️" : isShop ? "🛍️" : isPark ? "🌳" : isHealth ? "⚕️" : "📍";

      const el = document.createElement("div");
      el.className = "custom-poi-marker cursor-pointer transition-transform hover:scale-125 duration-150";
      el.innerHTML = `
        <div style="background: ${bgColor}; width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; border: 2px solid #ffffff; box-shadow: 0 2px 8px rgba(0,0,0,0.25); font-size: 12px;">
          ${iconEmoji}
        </div>
      `;

      el.addEventListener("click", (e) => {
        e.stopPropagation();
        onLocationSelect(place.lat, place.lon);
      });

      const popup = new maplibregl.Popup({ offset: [0, -14], closeButton: true }).setHTML(`
        <div style="font-family: system-ui, -apple-system, sans-serif; min-width: 190px;">
          <div style="font-weight: 700; color: #202124; font-size: 15px; margin-bottom: 2px; line-height: 1.3;">${place.name}</div>
          ${place.category ? `<div style="display: inline-block; font-size: 11px; font-weight: 600; background: #e8f0fe; color: #1a73e8; padding: 2px 8px; border-radius: 12px; text-transform: capitalize; margin-bottom: 6px;">${place.category.replace(/_/g, " ")}</div>` : ""}
          <div style="color: #5f6368; font-size: 12px; margin-bottom: 4px;">${place.address_text || "Australia"}</div>
          ${place.distance_meters != null ? `<div style="color: #1a73e8; font-weight: 500; font-size: 11px; background: #f1f3f4; padding: 2px 6px; border-radius: 6px; display: inline-flex; align-items: center; gap: 4px;">📍 ${place.distance_meters}m from pin</div>` : ""}
        </div>
      `);

      const marker = new maplibregl.Marker({ element: el })
        .setLngLat([place.lon, place.lat])
        .setPopup(popup)
        .addTo(map);

      poiMarkersRef.current.push(marker);
    });
  }, [nearbyPlaces, showPois, onLocationSelect]);

  // Render Address markers
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    addressMarkersRef.current.forEach((m) => m.remove());
    addressMarkersRef.current = [];

    if (!showAddresses) return;

    nearbyAddresses.forEach((addr) => {
      const el = document.createElement("div");
      el.className = "custom-address-marker cursor-pointer transition-transform hover:scale-150 duration-150";
      el.innerHTML = `
        <div style="background: #10b981; width: 12px; height: 12px; border-radius: 50%; border: 2px solid #ffffff; box-shadow: 0 1px 4px rgba(0,0,0,0.3);"></div>
      `;

      el.addEventListener("click", (e) => {
        e.stopPropagation();
        onLocationSelect(addr.lat, addr.lon);
      });

      const popup = new maplibregl.Popup({ offset: [0, -8], closeButton: true }).setHTML(`
        <div style="font-family: system-ui, -apple-system, sans-serif; min-width: 180px;">
          <div style="font-weight: 700; color: #059669; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px;">G-NAF Cadastral Address</div>
          <div style="color: #202124; font-weight: 600; font-size: 14px; margin-bottom: 4px;">${addr.full_address}</div>
          ${addr.distance_meters != null ? `<div style="color: #5f6368; font-size: 11px;">📍 ${addr.distance_meters}m from pin</div>` : ""}
        </div>
      `);

      const marker = new maplibregl.Marker({ element: el })
        .setLngLat([addr.lon, addr.lat])
        .setPopup(popup)
        .addTo(map);

      addressMarkersRef.current.push(marker);
    });
  }, [nearbyAddresses, showAddresses, onLocationSelect]);

  const isDark = layerMode === "dark";

  return (
    <div className="w-full h-full relative overflow-hidden">
      {/* MapLibre Canvas */}
      <div ref={mapContainerRef} className="w-full h-full" />

      {/* Floating Google Maps Quick Category Filter Chips */}
      <div className="absolute top-4 left-4 z-10 flex items-center space-x-2 overflow-x-auto max-w-[calc(100%-240px)] scrollbar-none py-0.5">
        {[
          { label: "🍽️ Restaurants", filter: "restaurant" },
          { label: "☕ Cafes", filter: "cafe" },
          { label: "🛒 Shopping", filter: "shop" },
          { label: "🌳 Parks", filter: "park" },
          { label: "🎯 Mooroolbark", coords: { lat: -37.79044, lon: 145.31252 } },
          { label: "🏙️ Melbourne", coords: { lat: -37.8136, lon: 144.9631 } },
        ].map((chip, idx) => (
          <button
            key={idx}
            onClick={() => {
              if (chip.coords) {
                onLocationSelect(chip.coords.lat, chip.coords.lon);
              } else if (chip.filter && onQuickFilter) {
                onQuickFilter(chip.filter);
              }
            }}
            className={`px-3.5 py-1.5 rounded-full text-xs font-medium shadow-md transition-all whitespace-nowrap flex items-center space-x-1.5 ${
              isDark
                ? "bg-slate-900/90 hover:bg-slate-800 text-slate-200 border border-slate-700/80"
                : "bg-white/95 hover:bg-white text-slate-700 hover:text-blue-600 border border-slate-200/90 hover:border-blue-300"
            }`}
          >
            <span>{chip.label}</span>
          </button>
        ))}
      </div>

      {/* Floating Satellite Vintage Timeline Bar (Active when in Satellite mode) */}
      {layerMode === "satellite" && (
        <div className="absolute top-16 left-4 z-10 flex items-center bg-white/95 dark:bg-slate-900/95 backdrop-blur-md rounded-2xl shadow-xl border border-slate-200/90 dark:border-slate-700/80 p-1 space-x-1 max-w-[calc(100%-32px)] overflow-x-auto scrollbar-none">
          <div className="px-2.5 py-1 flex items-center space-x-1.5 border-r border-slate-200/60 dark:border-slate-700/60 text-[11px] font-bold text-slate-500 uppercase tracking-wider">
            <Clock className="w-3.5 h-3.5 text-blue-500" />
            <span>Age:</span>
          </div>
          {[
            { id: "google-latest", label: "✈️ Google HD", sub: "~2022 (Sub-30cm)" },
            { id: "esri-recent", label: "🛰️ ESRI Recent", sub: "~2024 (Newer)" },
            { id: "google-hybrid", label: "🏷️ Hybrid", sub: "Aerial + Roads" },
            { id: "esri-wayback", label: "🕰️ Wayback", sub: "Historical Archive" },
          ].map((v) => (
            <button
              key={v.id}
              onClick={() => setSatelliteVintage(v.id as SatelliteVintage)}
              className={`px-3 py-1.5 rounded-xl text-xs font-semibold transition-all whitespace-nowrap flex items-center space-x-1.5 ${
                satelliteVintage === v.id
                  ? "bg-blue-600 text-white shadow-md"
                  : isDark
                  ? "text-slate-300 hover:bg-slate-800"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              <span>{v.label}</span>
              <span
                className={`text-[9px] px-1.5 py-0.5 rounded-full ${
                  satelliteVintage === v.id
                    ? "bg-white/20 text-white"
                    : "bg-slate-200/60 dark:bg-slate-800 text-slate-500 dark:text-slate-400"
                }`}
              >
                {v.sub}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Floating Action Tools (Top Right) */}
      <div className="absolute top-4 right-4 z-10 flex items-center space-x-2">
        {/* Recenter Pin Button */}
        <button
          onClick={recenterMap}
          className={`p-2 rounded-xl shadow-md transition-all flex items-center justify-center ${
            isDark
              ? "bg-slate-900/90 hover:bg-slate-800 text-slate-200 border border-slate-700"
              : "bg-white/95 hover:bg-white text-slate-700 hover:text-blue-600 border border-slate-200"
          }`}
          title="Recenter on active pin"
        >
          <Crosshair className="w-4 h-4 text-blue-500" />
        </button>

        {/* Map Rotation (2D & 3D) */}
        <button
          onClick={() => rotateMap(-20)}
          className={`p-2 rounded-xl shadow-md transition-all flex items-center justify-center ${
            isDark
              ? "bg-slate-900/90 hover:bg-slate-800 text-slate-200 border border-slate-700"
              : "bg-white/95 hover:bg-white text-slate-700 hover:text-blue-600 border border-slate-200"
          }`}
          title="Rotate map counter-clockwise"
        >
          <RotateCcw className="w-4 h-4" />
        </button>
        <button
          onClick={() => rotateMap(20)}
          className={`p-2 rounded-xl shadow-md transition-all flex items-center justify-center ${
            isDark
              ? "bg-slate-900/90 hover:bg-slate-800 text-slate-200 border border-slate-700"
              : "bg-white/95 hover:bg-white text-slate-700 hover:text-blue-600 border border-slate-200"
          }`}
          title="Rotate map clockwise"
        >
          <RotateCw className="w-4 h-4" />
        </button>

        {/* 3D Perspective Tilt Button */}
        <button
          onClick={toggle3D}
          className={`p-2 rounded-xl shadow-md transition-all flex items-center space-x-1 text-xs font-medium ${
            is3DMode ? "ring-2 ring-blue-500" : ""
          } ${
            isDark
              ? "bg-slate-900/90 hover:bg-slate-800 text-slate-200 border border-slate-700"
              : "bg-white/95 hover:bg-white text-slate-700 border border-slate-200"
          }`}
          title="Toggle 3D Buildings & Tilt Perspective"
        >
          <Box className={`w-4 h-4 ${is3DMode ? "text-blue-500" : "text-slate-500"}`} />
          <span className="hidden sm:inline">3D</span>
        </button>

        {/* Quick Satellite Toggle Pill */}
        <button
          onClick={() => setLayerMode(layerMode === "satellite" ? "streets" : "satellite")}
          className={`px-3 py-1.5 text-xs font-semibold rounded-xl shadow-md transition-all flex items-center space-x-1.5 ${
            layerMode === "satellite"
              ? "bg-blue-600 text-white ring-2 ring-blue-400 border-transparent"
              : isDark
              ? "bg-slate-900/90 hover:bg-slate-800 text-slate-200 border border-slate-700"
              : "bg-white/95 hover:bg-white text-slate-800 border border-slate-200"
          }`}
          title="Toggle Satellite Imagery"
        >
          <Globe className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">Satellite</span>
        </button>

        {/* Deep 3D Studio Launcher Button */}
        <button
          onClick={() => setIsDeep3DOpen(true)}
          className="px-3.5 py-1.5 text-xs font-semibold rounded-xl shadow-lg transition-all flex items-center space-x-1.5 bg-gradient-to-r from-blue-600 via-indigo-600 to-purple-600 hover:from-blue-500 hover:to-purple-500 text-white border border-blue-400/50 shadow-blue-500/20 active:scale-95"
          title="Launch Deep 3D Satellite Studio (Height Displacement & Roof Model)"
        >
          <Sparkles className="w-3.5 h-3.5 text-yellow-300 animate-pulse" />
          <span>Deep 3D Studio</span>
          <span className="text-[9px] px-1.5 py-0.2 rounded bg-white/20 font-mono font-bold">3D</span>
        </button>
      </div>

      {/* Floating Google Maps Style Layer Switcher (Bottom Left) */}
      <div className="absolute bottom-6 left-4 z-20">
        <div className="relative">
          {/* Main Layers Button */}
          <button
            onClick={() => setIsLayersMenuOpen(!isLayersMenuOpen)}
            className={`flex items-center space-x-2 px-3.5 py-2 rounded-2xl shadow-xl transition-all border ${
              isDark
                ? "bg-slate-900/95 hover:bg-slate-800 text-white border-slate-700"
                : "bg-white/95 hover:bg-white text-slate-800 border-slate-200 hover:border-blue-300"
            }`}
            title="Choose Map Layers & Styles"
          >
            <Layers className="w-4 h-4 text-blue-500" />
            <span className="text-xs font-bold capitalize">{layerMode}</span>
          </button>

          {/* Layer Popover Menu */}
          {isLayersMenuOpen && (
            <div
              className={`absolute bottom-full left-0 mb-3 w-72 rounded-2xl shadow-2xl p-3.5 border transition-all z-30 ${
                isDark
                  ? "bg-slate-900/95 backdrop-blur-md border-slate-700 text-white"
                  : "bg-white/95 backdrop-blur-md border-slate-200 text-slate-900"
              }`}
            >
              <div className="flex items-center justify-between pb-2 mb-2.5 border-b border-slate-200/40">
                <span className="text-xs font-bold uppercase tracking-wider text-slate-400">
                  Map Type
                </span>
                <button
                  onClick={() => setIsLayersMenuOpen(false)}
                  className="text-xs text-slate-400 hover:text-slate-200 p-1"
                >
                  ✕
                </button>
              </div>

              {/* 4 Map Style Tiles */}
              <div className="grid grid-cols-2 gap-2 mb-3">
                {[
                  { id: "streets", label: "Default", icon: "🗺️", desc: "Google Light" },
                  { id: "satellite", label: "Satellite", icon: "🛰️", desc: "Google Ultra-HD" },
                  { id: "topography", label: "Terrain", icon: "⛰️", desc: "Contours & DEM" },
                  { id: "dark", label: "Dark Mode", icon: "🌙", desc: "Night Vector" },
                ].map((item) => (
                  <button
                    key={item.id}
                    onClick={() => {
                      setLayerMode(item.id as MapLayerMode);
                      if (item.id === "dark" && externalToggleTheme && !externalDarkMode) {
                        externalToggleTheme();
                      } else if (item.id === "streets" && externalToggleTheme && externalDarkMode) {
                        externalToggleTheme();
                      }
                    }}
                    className={`p-2.5 rounded-xl border text-left transition-all flex flex-col justify-between ${
                      layerMode === item.id
                        ? "border-blue-500 ring-2 ring-blue-500/30 bg-blue-50/20"
                        : isDark
                        ? "border-slate-700/80 hover:bg-slate-800"
                        : "border-slate-200 hover:bg-slate-50"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-lg">{item.icon}</span>
                      {layerMode === item.id && (
                        <Check className="w-3.5 h-3.5 text-blue-500" />
                      )}
                    </div>
                    <div className="mt-1.5">
                      <div className="text-xs font-bold leading-tight">{item.label}</div>
                      <div className="text-[10px] text-slate-400">{item.desc}</div>
                    </div>
                  </button>
                ))}
              </div>

              {/* Map Details Section */}
              <div className="pt-2 border-t border-slate-200/40 space-y-2">
                <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
                  Map Enhancements
                </span>

                {/* 3D Buildings Extrusions Toggle */}
                <button
                  onClick={() => setShow3DBuildings(!show3DBuildings)}
                  className={`w-full p-2 rounded-xl border flex items-center justify-between text-xs transition ${
                    show3DBuildings
                      ? "bg-blue-500/10 border-blue-500/40 text-blue-600 font-semibold"
                      : isDark
                      ? "bg-slate-800/40 border-slate-700 text-slate-400"
                      : "bg-slate-50 border-slate-200 text-slate-500"
                  }`}
                >
                  <div className="flex items-center space-x-2">
                    <Building2 className="w-4 h-4 text-blue-500" />
                    <span>3D Building Extrusions</span>
                  </div>
                  <span className="text-[10px] font-mono font-bold">
                    {show3DBuildings ? "ON" : "OFF"}
                  </span>
                </button>

                {/* LoD2 3D Architecture & Volumetric Trees Toggle */}
                <button
                  onClick={() => setEnableLoD2Houses(!enableLoD2Houses)}
                  className={`w-full p-2 rounded-xl border flex items-center justify-between text-xs transition ${
                    enableLoD2Houses
                      ? "bg-blue-500/10 border-blue-500/40 text-blue-600 font-semibold"
                      : isDark
                      ? "bg-slate-800/40 border-slate-700 text-slate-400"
                      : "bg-slate-50 border-slate-200 text-slate-500"
                  }`}
                >
                  <div className="flex items-center space-x-2">
                    <Home className="w-4 h-4 text-blue-500" />
                    <span>LoD2 Pitched Roofs & 3D Trees</span>
                  </div>
                  <span className="text-[10px] font-mono font-bold">
                    {enableLoD2Houses ? "ON" : "OFF"}
                  </span>
                </button>

                {/* AI Satellite Remote Sensing Enhancer Toggle */}
                <button
                  onClick={() => setEnableAiSatellite(!enableAiSatellite)}
                  className={`w-full p-2 rounded-xl border flex items-center justify-between text-xs transition ${
                    enableAiSatellite
                      ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-600 font-semibold"
                      : isDark
                      ? "bg-slate-800/40 border-slate-700 text-slate-400"
                      : "bg-slate-50 border-slate-200 text-slate-500"
                  }`}
                >
                  <div className="flex items-center space-x-2">
                    <Sparkles className="w-4 h-4 text-emerald-500" />
                    <span>AI Satellite (ExG + CAS 3x3)</span>
                  </div>
                  <span className="text-[10px] font-mono font-bold">
                    {enableAiSatellite ? "ON" : "OFF"}
                  </span>
                </button>

                {/* AI Trees & Vegetation Canopies Toggle */}
                <button
                  onClick={() => setEnableTrees(!enableTrees)}
                  className={`w-full p-2 rounded-xl border flex items-center justify-between text-xs transition ${
                    enableTrees
                      ? "bg-green-500/10 border-green-500/40 text-green-600 font-semibold"
                      : isDark
                      ? "bg-slate-800/40 border-slate-700 text-slate-400"
                      : "bg-slate-50 border-slate-200 text-slate-500"
                  }`}
                >
                  <div className="flex items-center space-x-2">
                    <Trees className="w-4 h-4 text-green-500" />
                    <div className="text-left">
                      <div>AI Trees & Vegetation</div>
                      <div className="text-[10px] text-slate-400 font-normal">Depth-derived 3D canopies</div>
                    </div>
                  </div>
                  <span className="text-[10px] font-mono font-bold">
                    {enableTrees ? "ON" : "OFF"}
                  </span>
                </button>

                {/* Deep 3D Studio (Satellite to 3D Relief) Launcher */}
                <button
                  onClick={() => {
                    setIsLayersMenuOpen(false);
                    setIsDeep3DOpen(true);
                  }}
                  className="w-full p-2.5 rounded-xl border flex items-center justify-between text-xs transition bg-gradient-to-r from-blue-500/15 via-purple-500/15 to-indigo-500/15 hover:from-blue-500/25 hover:to-purple-500/25 border-blue-500/40 text-blue-600 dark:text-blue-400 font-semibold"
                >
                  <div className="flex items-center space-x-2">
                    <Sparkles className="w-4 h-4 text-purple-500 animate-pulse" />
                    <div className="text-left">
                      <div className="font-bold">✨ Deep 3D Studio</div>
                      <div className="text-[10px] text-slate-400 font-normal">Turn satellite photos into 3D relief</div>
                    </div>
                  </div>
                  <span className="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-500">
                    OPEN
                  </span>
                </button>
              </div>

              {/* Satellite Vintage & Age Selection (Only when in Satellite mode) */}
              {layerMode === "satellite" && (
                <div className="pt-2 border-t border-slate-200/40 space-y-1.5">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
                    Satellite Vintage & Age
                  </span>
                  <div className="grid grid-cols-1 gap-1">
                    {[
                      { id: "google-latest", label: "✈️ Google Ultra-HD", sub: "~2022 (Fixed-Wing Ortho 15-30cm)" },
                      { id: "esri-recent", label: "🛰️ ESRI Recent", sub: "~2024 (Orbital Satellite Mosaic)" },
                      { id: "google-hybrid", label: "🏷️ Google Hybrid", sub: "Ultra-HD Aerial with Roads & Names" },
                      { id: "esri-wayback", label: "🕰️ ESRI Wayback", sub: "Historical Flight Archive" },
                    ].map((v) => (
                      <button
                        key={v.id}
                        onClick={() => setSatelliteVintage(v.id as SatelliteVintage)}
                        className={`p-1.5 rounded-lg border text-left transition flex items-center justify-between text-xs ${
                          satelliteVintage === v.id
                            ? "border-blue-500 bg-blue-500/10 text-blue-600 font-semibold"
                            : isDark
                            ? "border-slate-700/80 hover:bg-slate-800 text-slate-300"
                            : "border-slate-200 hover:bg-slate-50 text-slate-700"
                        }`}
                      >
                        <div>
                          <div className="text-xs font-semibold">{v.label}</div>
                          <div className="text-[10px] text-slate-400">{v.sub}</div>
                        </div>
                        {satelliteVintage === v.id && <Check className="w-3.5 h-3.5 text-blue-500" />}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Floating AI & 3D Status Badges (Clickable to open Deep 3D Studio) */}
      <div className="absolute bottom-6 right-20 z-10 hidden md:flex items-center space-x-2">
        {layerMode === "satellite" && (
          <button
            onClick={() => setIsDeep3DOpen(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-full bg-slate-900/85 backdrop-blur-md text-white text-[11px] font-medium shadow-lg border border-slate-700/60 hover:border-blue-500/80 transition cursor-pointer"
          >
            <Clock className="w-3.5 h-3.5 text-blue-400" />
            <span>
              {satelliteVintage === "esri-recent"
                ? "ESRI ~2024 (Newer)"
                : satelliteVintage === "esri-wayback"
                ? "ESRI Wayback (Archive)"
                : satelliteVintage === "google-hybrid"
                ? "Hybrid Aerial + Roads"
                : "Google Ortho ~2022 (High-Res)"}
            </span>
          </button>
        )}
        {layerMode === "satellite" && enableAiSatellite && (
          <button
            onClick={() => setIsDeep3DOpen(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-full bg-slate-900/85 backdrop-blur-md text-white text-[11px] font-medium shadow-lg border border-slate-700/60 hover:border-emerald-500/80 transition cursor-pointer"
          >
            <Sparkles className="w-3.5 h-3.5 text-emerald-400 animate-pulse" />
            <span>AI Super-Resolution 2x + CAS</span>
          </button>
        )}
        {is3DMode && enableLoD2Houses && (
          <button
            onClick={() => setIsDeep3DOpen(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded-full bg-slate-900/85 backdrop-blur-md text-white text-[11px] font-medium shadow-lg border border-slate-700/60 hover:border-blue-500/80 transition cursor-pointer"
          >
            <Home className="w-3.5 h-3.5 text-blue-400" />
            <span>LoD2 3D: Pitched Roofs & Canopies</span>
          </button>
        )}
      </div>

      {/* Deep 3D Satellite Studio Modal */}
      <Deep3DStudio
        isOpen={isDeep3DOpen}
        onClose={() => setIsDeep3DOpen(false)}
        targetLat={selectedLocation?.lat ?? -37.79044}
        targetLon={selectedLocation?.lon ?? 145.31252}
        addressTitle={
          nearbyAddresses.length > 0
            ? nearbyAddresses[0].full_address
            : "27 Cambridge Road, Mooroolbark VIC 3138"
        }
        initialVintage={satelliteVintage}
        building={deep3DBuilding}
      />
    </div>
  );
}


