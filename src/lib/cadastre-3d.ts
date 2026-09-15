import type { FeatureCollection, Feature, Polygon } from "geojson";
import type { Address, Place } from "./geo-engine";

/**
 * Procedural 3D Cadastral & LoD2 Architecture Generator
 *
 * Synthesizes multi-tiered 3D architectural volumes, pitched roof ridges,
 * and volumetric 3D tree canopies directly into MapLibre's WebGL extrusion pipeline.
 * Guarantees visible, dramatic 3D buildings and trees anywhere on the map.
 */
export function generate3DBuildingFootprints(
  selectedLocation: { lat: number; lon: number } | null,
  addresses: Address[],
  places: Place[]
): FeatureCollection<Polygon> {
  const features: Feature<Polygon>[] = [];
  const processedKeys = new Set<string>();

  // Helper to create a rectangular footprint polygon in meters
  const makeFootprint = (
    lat: number,
    lon: number,
    widthMeters: number,
    depthMeters: number,
    rotationDegrees: number,
    props: Record<string, unknown>
  ): Feature<Polygon> => {
    const latOffset = depthMeters / 2 / 111320;
    const lonOffset = widthMeters / 2 / (111320 * Math.cos((lat * Math.PI) / 180));
    const rad = (rotationDegrees * Math.PI) / 180;
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);

    const corners = [
      [-lonOffset, -latOffset],
      [lonOffset, -latOffset],
      [lonOffset, latOffset],
      [-lonOffset, latOffset],
      [-lonOffset, -latOffset],
    ].map(([dx, dy]) => {
      const rx = dx * cos - dy * sin;
      const ry = dx * sin + dy * cos;
      return [lon + rx, lat + ry];
    });

    return {
      type: "Feature",
      properties: props,
      geometry: {
        type: "Polygon",
        coordinates: [corners],
      },
    };
  };

  // Helper to create an 8-sided circular polygon for volumetric 3D trees
  const makeTreeCanopy = (
    lat: number,
    lon: number,
    radiusMeters: number,
    minHeight: number,
    height: number,
    color = "#2d6a4f"
  ): Feature<Polygon> => {
    const latOffset = radiusMeters / 111320;
    const lonOffset = radiusMeters / (111320 * Math.cos((lat * Math.PI) / 180));
    const sides = 8;
    const coords: [number, number][] = [];

    for (let i = 0; i <= sides; i++) {
      const angle = (i / sides) * Math.PI * 2;
      coords.push([
        lon + Math.cos(angle) * lonOffset,
        lat + Math.sin(angle) * latOffset,
      ]);
    }

    return {
      type: "Feature",
      properties: {
        type: "tree",
        color,
        min_height: minHeight,
        height,
      },
      geometry: {
        type: "Polygon",
        coordinates: [coords],
      },
    };
  };

  const centerLat = selectedLocation?.lat ?? -37.79044;
  const centerLon = selectedLocation?.lon ?? 145.31252;

  // 1. Primary Selected Location (e.g. 27 Cambridge Road, Mooroolbark)
  if (selectedLocation) {
    const key = `${selectedLocation.lat.toFixed(5)},${selectedLocation.lon.toFixed(5)}`;
    processedKeys.add(key);

    // Tier 1: Ground Floor Envelope (18m x 20m, height: 7m)
    features.push(
      makeFootprint(selectedLocation.lat, selectedLocation.lon, 18, 20, 18, {
        isSelected: true,
        height: 7.5,
        min_height: 0,
        color: "#f8fafc",
        type: "target_ground",
      })
    );

    // Tier 2: Upper Storey Setback (14m x 15m, min: 7.5m, height: 14.5m)
    features.push(
      makeFootprint(selectedLocation.lat, selectedLocation.lon, 14, 15, 18, {
        isSelected: true,
        height: 14.5,
        min_height: 7.5,
        color: "#2563eb", // Signature architectural blue highlight
        type: "target_upper",
      })
    );

    // Tier 3: LoD2 Pitched Roof Ridge Volume (8m x 12m, min: 14.5m, height: 19.5m)
    features.push(
      makeFootprint(selectedLocation.lat, selectedLocation.lon, 8, 12, 18, {
        isSelected: true,
        height: 19.5,
        min_height: 14.5,
        color: "#ffffff", // Crisp white roof cap
        type: "target_roof",
      })
    );

    // Volumetric 3D Trees on Property Boundaries
    const latOffTree = 14 / 111320;
    const lonOffTree = 14 / (111320 * Math.cos((centerLat * Math.PI) / 180));
    features.push(makeTreeCanopy(centerLat - latOffTree, centerLon - lonOffTree, 4.2, 2.5, 9.8, "#387d44"));
    features.push(makeTreeCanopy(centerLat + latOffTree * 0.8, centerLon + lonOffTree * 0.9, 4.8, 2.8, 11.2, "#2b6b37"));
    features.push(makeTreeCanopy(centerLat - latOffTree * 0.6, centerLon + lonOffTree * 0.7, 3.5, 2.0, 8.5, "#488a53"));
  }

  // 2. Nearby Cadastral Addresses
  addresses.forEach((addr, idx) => {
    const key = `${addr.lat.toFixed(5)},${addr.lon.toFixed(5)}`;
    if (processedKeys.has(key)) return;
    processedKeys.add(key);

    const hash = Math.abs(addr.id || idx * 17);
    const width = 14 + (hash % 5);
    const depth = 16 + ((hash >> 2) % 6);
    const rot = (hash * 19) % 45;

    // Roof & facade palette
    const roofColor = hash % 3 === 0 ? "#a84a2d" : hash % 3 === 1 ? "#2e3846" : "#6c7a89"; // Terracotta, Monument, Slate
    const wallColor = hash % 2 === 0 ? "#f1ede6" : "#e8e1d5"; // Sandstone, Off-white

    // Tier 1: Ground floor (height: 6.5m - 8.5m)
    const groundHeight = 6.8 + (hash % 2) * 1.5;
    features.push(
      makeFootprint(addr.lat, addr.lon, width, depth, rot, {
        isSelected: false,
        height: groundHeight,
        min_height: 0,
        color: wallColor,
        address: addr.full_address,
        type: "residential_ground",
      })
    );

    // Tier 2: LoD2 Pitched Roof Ridge Extrusion (height: 11.5m - 14.5m)
    const ridgeHeight = groundHeight + 4.5 + (hash % 3) * 0.8;
    features.push(
      makeFootprint(addr.lat, addr.lon, width * 0.65, depth * 0.75, rot, {
        isSelected: false,
        height: ridgeHeight,
        min_height: groundHeight,
        color: roofColor,
        type: "residential_roof",
      })
    );

    // Volumetric 3D Garden Tree for each address
    const tLatOff = (10 + (hash % 6)) / 111320;
    const tLonOff = (10 + (hash % 6)) / (111320 * Math.cos((addr.lat * Math.PI) / 180));
    features.push(
      makeTreeCanopy(
        addr.lat - tLatOff,
        addr.lon - tLonOff,
        3.2 + (hash % 3) * 0.5,
        2.2,
        8.5 + (hash % 4),
        hash % 2 === 0 ? "#2e6f40" : "#3b7d48"
      )
    );
  });

  // 3. Fallback Neighborhood Synthesis (Guarantees immediate 3D houses around center even before nearby API finishes)
  if (features.length < 10) {
    const offsets = [
      { dx: -25, dy: 15, rot: 12 },
      { dx: 26, dy: 18, rot: 15 },
      { dx: -30, dy: -22, rot: 20 },
      { dx: 28, dy: -24, rot: 16 },
      { dx: -55, dy: 22, rot: 10 },
      { dx: 58, dy: 25, rot: 14 },
      { dx: -60, dy: -32, rot: 22 },
      { dx: 62, dy: -35, rot: 18 },
      { dx: 0, dy: 45, rot: 8 },
      { dx: 0, dy: -50, rot: 12 },
    ];

    offsets.forEach((off, i) => {
      const pLat = centerLat + off.dy / 111320;
      const pLon = centerLon + off.dx / (111320 * Math.cos((centerLat * Math.PI) / 180));
      const key = `${pLat.toFixed(5)},${pLon.toFixed(5)}`;
      if (processedKeys.has(key)) return;
      processedKeys.add(key);

      const rColor = i % 2 === 0 ? "#a84a2d" : "#2d3748";
      // Ground tier
      features.push(
        makeFootprint(pLat, pLon, 15, 17, off.rot, {
          isSelected: false,
          height: 7.2,
          min_height: 0,
          color: "#eee9df",
          type: "residential_ground",
        })
      );
      // Roof tier
      features.push(
        makeFootprint(pLat, pLon, 10, 12, off.rot, {
          isSelected: false,
          height: 12.8,
          min_height: 7.2,
          color: rColor,
          type: "residential_roof",
        })
      );
      // Tree
      features.push(makeTreeCanopy(pLat - 7 / 111320, pLon - 7 / 111320, 3.4, 2.2, 9.0, "#2e6f40"));
    });
  }

  // 4. Commercial Places & POIs
  places.forEach((place, idx) => {
    const key = `${place.lat.toFixed(5)},${place.lon.toFixed(5)}`;
    if (processedKeys.has(key)) return;
    processedKeys.add(key);

    const isCommercial = place.category && /store|supermarket|mall|hospital/i.test(place.category);
    const width = isCommercial ? 32 : 20;
    const depth = isCommercial ? 36 : 24;
    const height = isCommercial ? 22.0 : 12.0;

    features.push(
      makeFootprint(place.lat, place.lon, width, depth, (idx * 31) % 45, {
        isSelected: false,
        height: height,
        min_height: 0,
        color: isCommercial ? "#334155" : "#e2ded4",
        name: place.name,
        type: "commercial",
      })
    );
  });

  return {
    type: "FeatureCollection",
    features,
  };
}
