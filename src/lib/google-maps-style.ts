import type { StyleSpecification } from "maplibre-gl";

export type MapLayerMode = "streets" | "satellite" | "topography" | "dark";
export type SatelliteVintage = "google-latest" | "google-hybrid" | "esri-recent" | "esri-wayback";

/**
 * Generates an open-source Google Maps clone Vector Style Specification.
 * Supports:
 * - Google Maps Light ("streets")
 * - ESRI World Imagery High-Resolution Aerial ("satellite")
 * - OpenTopoMap Elevation & Relief ("topography")
 * - Google Maps Dark ("dark")
 * - 3D Ambient Occlusion Extruded Buildings with dynamic sun lighting
 * - Multi-Vintage Satellite Timeline (Google Ultra-HD, ESRI Recent 2024, Wayback Archive)
 */
export function createGoogleMapsStyle(
  tileServerUrl: string,
  layerMode: MapLayerMode | boolean = "streets",
  enable3D = true,
  enableAiSatellite = true,
  satelliteVintage: SatelliteVintage = "google-latest",
  enableTrees = true
): StyleSpecification {
  // Normalize backward compatibility: boolean true -> "dark", false -> "streets"
  const mode: MapLayerMode =
    typeof layerMode === "boolean"
      ? layerMode
        ? "dark"
        : "streets"
      : layerMode;

  const isDark = mode === "dark";
  const isSatellite = mode === "satellite";
  const isTopo = mode === "topography";

  // Google Maps color palette
  const colors = isDark
    ? {
        background: "#1e222b",
        water: "#17263c",
        park: "#1f3327",
        building: "#2b323f",
        buildingOutline: "#3b4555",
        roadLocal: "#323c4a",
        roadLocalCasing: "#1c232c",
        roadArterial: "#3e4c60",
        roadArterialCasing: "#253040",
        roadHighway: "#d97706",
        roadHighwayCasing: "#92400e",
        text: "#f8fafc",
        textHalo: "#0f172a",
      }
    : {
        background: isSatellite ? "#0a0f18" : isTopo ? "#e8ece8" : "#f4f3f0",
        water: "#aadaff",
        park: "#c8facc",
        building: "#ebe7e0",
        buildingOutline: "#dbd7d0",
        roadLocal: "#ffffff",
        roadLocalCasing: "#e5e5e0",
        roadArterial: "#ffffff",
        roadArterialCasing: "#fed89f",
        roadHighway: "#fbbc04",
        roadHighwayCasing: "#f59e0b",
        text: "#202124",
        textHalo: "#ffffff",
      };

  // Base raster layer selection
  const rasterSourceId = isSatellite
    ? "satellite-raster"
    : isTopo
    ? "topo-raster"
    : "osm-raster-fallback";

  const rasterPaint = isSatellite
    ? {
        "raster-opacity": 1.0,
        "raster-saturation": 0.08,
        "raster-contrast": 0.05,
        "raster-brightness-max": 1.0,
        "raster-brightness-min": 0.0,
      }
    : isTopo
    ? {
        "raster-opacity": 1.0,
        "raster-saturation": 0.12,
        "raster-contrast": 0.06,
        "raster-brightness-max": 1.0,
        "raster-brightness-min": 0.0,
      }
    : isDark
    ? {
        "raster-opacity": 0.7,
        "raster-saturation": -0.2,
        "raster-contrast": 0.15,
        "raster-brightness-max": 0.88,
        "raster-brightness-min": 0.08,
      }
    : {
        "raster-opacity": 1.0,
        "raster-saturation": 0.18,
        "raster-contrast": 0.08,
        "raster-brightness-max": 1.0,
        "raster-brightness-min": 0.0,
      };

  const style: StyleSpecification = {
    version: 8,
    name: `Google Maps Style (${mode})`,
    sources: {
      "local-vector": {
        type: "vector",
        tiles: [`${tileServerUrl}/tiles/{z}/{x}/{y}.pbf`],
        minzoom: 0,
        maxzoom: 16,
      },
      "osm-raster-fallback": {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      },
      "satellite-raster": {
        type: "raster",
        tiles:
          satelliteVintage === "esri-recent"
            ? [
                enableAiSatellite
                  ? "ai-satellite://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
                  : "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
              ]
            : satelliteVintage === "esri-wayback"
            ? [
                enableAiSatellite
                  ? "ai-satellite://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/23001/{z}/{y}/{x}"
                  : "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/23001/{z}/{y}/{x}",
              ]
            : satelliteVintage === "google-hybrid"
            ? [
                enableAiSatellite
                  ? "ai-satellite://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"
                  : "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
              ]
            : [
                enableAiSatellite
                  ? "ai-satellite://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"
                  : "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
              ],
        tileSize: 256,
        attribution:
          satelliteVintage === "esri-recent"
            ? "&copy; Esri, Maxar, Earthstar (Recent Satellite ~2024)"
            : satelliteVintage === "esri-wayback"
            ? "&copy; Esri Wayback Archive (Historical Flight Epoch)"
            : satelliteVintage === "google-hybrid"
            ? "&copy; Google Hybrid (Aerial + Roads/Labels)"
            : "&copy; Google Earth Ultra-HD Aerial (~2022)",
        maxzoom: satelliteVintage === "esri-recent" || satelliteVintage === "esri-wayback" ? 19 : 21,
      },
      "topo-raster": {
        type: "raster",
        tiles: ["https://tile.opentopomap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: '&copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
        maxzoom: 17,
      },
      "cadastre-3d-buildings": {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: [],
        },
      },
    },
    // Directional Sun lighting for realistic 3D building wall shadows
    light: {
      anchor: "viewport",
      color: isDark ? "#94a3b8" : "#ffffff",
      intensity: isDark ? 0.35 : 0.52,
      position: [1.5, 90, 48],
    },
    layers: [
      // 1. Base canvas background
      {
        id: "background",
        type: "background",
        paint: {
          "background-color": colors.background,
        },
      },
      // 2. Active Base Raster Layer (Streets / Satellite / Topo / Dark)
      {
        id: "base-raster-layer",
        type: "raster",
        source: rasterSourceId,
        paint: rasterPaint,
      },
      // 3. Vector Water Bodies (hidden in satellite to preserve true aerial water)
      {
        id: "water",
        type: "fill",
        source: "local-vector",
        "source-layer": "water",
        layout: {
          visibility: isSatellite ? "none" : "visible",
        },
        paint: {
          "fill-color": colors.water,
          "fill-opacity": 0.95,
        },
      },
      // 4. Parks and Greenery
      {
        id: "park",
        type: "fill",
        source: "local-vector",
        "source-layer": "park",
        layout: {
          visibility: isSatellite ? "none" : "visible",
        },
        paint: {
          "fill-color": colors.park,
          "fill-opacity": 0.85,
        },
      },
      // 4b. Tree Canopy Fills (AI / depth-derived vegetation, 2D)
      {
        id: "tree-2d",
        type: "fill",
        source: "local-vector",
        "source-layer": "tree",
        minzoom: 13,
        maxzoom: enable3D ? 14 : 24,
        layout: {
          visibility: enableTrees ? "visible" : "none",
        },
        paint: {
          "fill-color": isSatellite ? "#2f7d43" : "#7bc47f",
          "fill-opacity": isSatellite ? 0.45 : 0.55,
        },
      },
      // 4c. Tree Canopy Outlines
      {
        id: "tree-outline",
        type: "line",
        source: "local-vector",
        "source-layer": "tree",
        minzoom: 13,
        maxzoom: enable3D ? 14 : 24,
        layout: {
          visibility: enableTrees ? "visible" : "none",
          "line-join": "round",
          "line-cap": "round",
        },
        paint: {
          "line-color": isSatellite ? "rgba(134, 239, 172, 0.9)" : "#3f9142",
          "line-width": ["interpolate", ["linear"], ["zoom"], 13, 0.4, 16, 1.2],
        },
      },
      // 5. 2D Building Footprints (Google Maps flat fill, AI/OSM derived)
      {
        id: "building-2d",
        type: "fill",
        source: "local-vector",
        "source-layer": "building",
        minzoom: 12,
        maxzoom: enable3D ? 14 : 24,
        paint: {
          "fill-color": isSatellite ? "#f8fafc" : colors.building,
          "fill-opacity": isSatellite ? 0.18 : 0.92,
        },
      },
      // 5b. Crisp Building Outlines (overlaid on streets, dark and satellite rasters)
      {
        id: "building-outline",
        type: "line",
        source: "local-vector",
        "source-layer": "building",
        minzoom: 12,
        maxzoom: enable3D ? 14 : 24,
        layout: {
          "line-join": "round",
          "line-cap": "round",
        },
        paint: {
          "line-color": isSatellite
            ? "rgba(248, 250, 252, 0.9)"
            : isDark
            ? "#4b5563"
            : "#c9c3ba",
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            12, 0.4,
            15, 1.0,
            17, 1.8,
          ],
          "line-opacity": isSatellite ? 0.85 : 0.95,
        },
      },
      // 5d. Volumetric AI Trees (extruded canopies shaded by height)
      {
        id: "tree-3d-vector",
        type: "fill-extrusion",
        source: "local-vector",
        "source-layer": "tree",
        minzoom: 14,
        layout: {
          visibility: enableTrees ? "visible" : "none",
        },
        paint: {
          "fill-extrusion-color": [
            "interpolate",
            ["linear"],
            ["coalesce", ["get", "height"], 8],
            3, "#5aa85c",
            9, "#2f7d3f",
            18, "#1f5c31",
          ],
          "fill-extrusion-height": [
            "coalesce",
            ["get", "render_height"],
            ["get", "height"],
            8,
          ],
          "fill-extrusion-base": [
            "coalesce",
            ["get", "render_min_height"],
            ["get", "min_height"],
            2,
          ],
          "fill-extrusion-opacity": 0.85,
          "fill-extrusion-vertical-gradient": true,
        },
      },
      // 6. 3D Extruded Buildings (from local vector tiles)
      {
        id: "building-3d-vector",
        type: "fill-extrusion",
        source: "local-vector",
        "source-layer": "building",
        minzoom: 14,
        layout: {
          visibility: enable3D ? "visible" : "none",
        },
        paint: {
          "fill-extrusion-color": [
            "case",
            ["has", "color"],
            ["get", "color"],
            isSatellite ? "#e7e4dd" : colors.building,
          ],
          "fill-extrusion-height": [
            "interpolate",
            ["linear"],
            ["zoom"],
            14, 0,
            14.5, ["coalesce", ["get", "render_height"], ["get", "height"], 9]
          ],
          "fill-extrusion-base": [
            "coalesce",
            ["get", "render_min_height"],
            ["get", "min_height"],
            0,
          ],
          "fill-extrusion-opacity": isSatellite ? 0.85 : 0.95,
          "fill-extrusion-vertical-gradient": true,
        },
      },
      // 7. Dynamic Cadastral 3D Rooftops & Buildings (Synthesized from Rooftop Coordinates)
      {
        id: "cadastre-3d-buildings-layer",
        type: "fill-extrusion",
        source: "cadastre-3d-buildings",
        filter: ["!=", "type", "tree"],
        minzoom: 13.0,
        layout: {
          visibility: enable3D ? "visible" : "none",
        },
        paint: {
          "fill-extrusion-color": [
            "case",
            ["boolean", ["get", "isSelected"], false],
            isDark ? "#3b82f6" : "#ffffff", // Glowing white / blue for target address
            ["has", "color"],
            ["get", "color"],
            isDark ? "#334155" : "#e2ded4",
          ],
          "fill-extrusion-height": ["coalesce", ["get", "height"], 12.0],
          "fill-extrusion-base": ["coalesce", ["get", "min_height"], 0],
          "fill-extrusion-opacity": isSatellite ? 0.94 : 0.96,
          "fill-extrusion-vertical-gradient": true,
        },
      },
      // 7b. Volumetric 3D Trees (Synthesized canopy crowns with vertical ambient lighting)
      {
        id: "cadastre-3d-trees-layer",
        type: "fill-extrusion",
        source: "cadastre-3d-buildings",
        filter: ["==", "type", "tree"],
        minzoom: 13.5,
        layout: {
          visibility: enable3D ? "visible" : "none",
        },
        paint: {
          "fill-extrusion-color": ["coalesce", ["get", "color"], "#2d6a4f"],
          "fill-extrusion-height": ["coalesce", ["get", "height"], 9.0],
          "fill-extrusion-base": ["coalesce", ["get", "min_height"], 2.2],
          "fill-extrusion-opacity": 0.92,
          "fill-extrusion-vertical-gradient": true,
        },
      },
      // 8. Local Roads (in satellite mode, rendered as clear semi-transparent navigation guides)
      {
        id: "road-local-casing",
        type: "line",
        source: "local-vector",
        "source-layer": "transportation",
        filter: ["==", "class", "minor"],
        paint: {
          "line-color": isSatellite ? "rgba(255, 255, 255, 0.4)" : colors.roadLocalCasing,
          "line-width": ["interpolate", ["linear"], ["zoom"], 12, 2, 16, 8],
        },
      },
      {
        id: "road-local",
        type: "line",
        source: "local-vector",
        "source-layer": "transportation",
        filter: ["==", "class", "minor"],
        paint: {
          "line-color": isSatellite ? "rgba(255, 255, 255, 0.85)" : colors.roadLocal,
          "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1.2, 16, 6],
        },
      },
      // 9. Arterial Roads & Highways (Google golden yellow)
      {
        id: "road-highway-casing",
        type: "line",
        source: "local-vector",
        "source-layer": "transportation",
        filter: ["in", "class", "primary", "secondary", "motorway", "trunk"],
        paint: {
          "line-color": isSatellite ? "rgba(245, 158, 11, 0.8)" : colors.roadHighwayCasing,
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2, 16, 12],
        },
      },
      {
        id: "road-highway",
        type: "line",
        source: "local-vector",
        "source-layer": "transportation",
        filter: ["in", "class", "primary", "secondary", "motorway", "trunk"],
        paint: {
          "line-color": colors.roadHighway,
          "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.4, 16, 9],
        },
      },
    ],
  };

  return style;
}
