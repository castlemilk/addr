import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import fs from "node:fs";

export interface Place {
  id: number;
  uuid: string;
  name: string;
  category: string | null;
  brand: string | null;
  address_text: string | null;
  suburb: string | null;
  state: string | null;
  postcode: string | null;
  lat: number;
  lon: number;
  phone: string | null;
  website: string | null;
  confidence: number | null;
  distance_meters?: number;
}

export interface Address {
  id: number;
  uuid: string;
  unit: string | null;
  number: string | null;
  street: string | null;
  suburb: string | null;
  state: string | null;
  postcode: string | null;
  full_address: string;
  lat: number;
  lon: number;
  distance_meters?: number;
}

interface PlaceSearchRow {
  id: number;
  name: string;
  category: string | null;
  address_text: string | null;
  lat: number;
  lon: number;
}

interface AddressSearchRow {
  id: number;
  full_address: string;
  number: string | null;
  suburb: string | null;
  state: string | null;
  postcode: string | null;
  lat: number;
  lon: number;
}

export interface ReverseGeocodeResult {
  query: { lat: number; lon: number };
  match_type: "poi_and_address" | "poi_only" | "address_only" | "approximate" | "none";
  place: Place | null;
  address: Address | null;
  formatted_title: string;
  formatted_subtitle: string;
  nearby_places: Place[];
  nearby_addresses: Address[];
  execution_time_ms: number;
}

export interface SearchResultItem {
  type: "place" | "address";
  id: number;
  title: string;
  subtitle: string;
  category?: string | null;
  lat: number;
  lon: number;
  distance_meters?: number;
}

export interface DbStats {
  placeCount: number;
  addressCount: number;
  regions: Array<{
    id: number;
    name: string;
    bbox: string;
    place_count: number;
    address_count: number;
    created_at: string;
  }>;
  dbSizeBytes: number;
  dbSizeFormatted: string;
}

let dbInstance: DatabaseSync | null = null;

export function getDb(): DatabaseSync {
  if (!dbInstance) {
    const dbPath = path.resolve(process.cwd(), "data", "australia_geo.db");
    if (!fs.existsSync(dbPath)) {
      throw new Error(`Database not found at ${dbPath}. Please run the ingestion script first.`);
    }
    dbInstance = new DatabaseSync(dbPath, { readOnly: true });
  }
  return dbInstance;
}

/**
 * High-accuracy Haversine distance in meters
 */
export function haversineDistance(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number
): number {
  const R = 6371000; // Earth radius in meters
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return Math.round(R * c * 10) / 10; // Round to 1 decimal place
}

/**
 * Reverse geocode a GPS coordinate (Lat, Lon)
 * Tiers:
 * 1. Check POI (Places) within tight threshold (~40m)
 * 2. Check Cadastral Address (G-NAF) within standard threshold (~100m)
 * 3. Fallback to wider radius if rural / sparse
 */
export function reverseGeocode(
  lat: number,
  lon: number,
  options: { maxRadiusMeters?: number; includeNearby?: boolean } = {}
): ReverseGeocodeResult {
  const startTime = performance.now();
  const db = getDb();
  const maxRadiusMeters = options.maxRadiusMeters ?? 250;
  const includeNearby = options.includeNearby ?? true;

  // Convert meters to approximate degree delta for R*Tree bounding box
  // 1 deg lat ≈ 111,000m
  // 1 deg lon at lat -33.8 ≈ 111,000 * cos(-33.8) ≈ 92,000m
  const latDelta = (maxRadiusMeters + 50) / 111000;
  const lonDelta = (maxRadiusMeters + 50) / (111000 * Math.cos((lat * Math.PI) / 180));

  const boxMinX = lon - lonDelta;
  const boxMaxX = lon + lonDelta;
  const boxMinY = lat - latDelta;
  const boxMaxY = lat + latDelta;

  // 1. Fetch Candidate Places via R*Tree
  const placeCandidates = db
    .prepare(
      `
      SELECT p.id, p.uuid, p.name, p.category, p.brand, p.address_text,
             p.suburb, p.state, p.postcode, p.lat, p.lon, p.phone, p.website, p.confidence
      FROM places_rtree r
      JOIN places p ON p.id = r.id
      WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
    `
    )
    .all(boxMaxX, boxMinX, boxMaxY, boxMinY) as unknown as Place[];

  // Calculate distance for candidate places and sort
  const scoredPlaces: Place[] = placeCandidates
    .map((p) => ({
      ...p,
      distance_meters: haversineDistance(lat, lon, p.lat, p.lon),
    }))
    .filter((p) => (p.distance_meters ?? Infinity) <= maxRadiusMeters)
    .sort((a, b) => (a.distance_meters ?? 0) - (b.distance_meters ?? 0));

  // 2. Fetch Candidate Addresses via R*Tree
  const addressCandidates = db
    .prepare(
      `
      SELECT a.id, a.uuid, a.unit, a.number, a.street, a.suburb,
             a.state, a.postcode, a.full_address, a.lat, a.lon
      FROM addresses_rtree r
      JOIN addresses a ON a.id = r.id
      WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
    `
    )
    .all(boxMaxX, boxMinX, boxMaxY, boxMinY) as unknown as Address[];

  const scoredAddresses: Address[] = addressCandidates
    .map((a) => ({
      ...a,
      distance_meters: haversineDistance(lat, lon, a.lat, a.lon),
    }))
    .filter((a) => (a.distance_meters ?? Infinity) <= maxRadiusMeters)
    .sort((a, b) => (a.distance_meters ?? 0) - (b.distance_meters ?? 0));

  const bestPlace = scoredPlaces.length > 0 && (scoredPlaces[0].distance_meters ?? Infinity) <= 50 ? scoredPlaces[0] : null;
  const bestAddress = scoredAddresses.length > 0 ? scoredAddresses[0] : null;

  // Determine match type
  let matchType: ReverseGeocodeResult["match_type"] = "none";
  if (bestPlace && bestAddress) {
    matchType = "poi_and_address";
  } else if (bestPlace) {
    matchType = "poi_only";
  } else if (bestAddress) {
    matchType = "address_only";
  } else if (scoredPlaces.length > 0) {
    matchType = "approximate";
  }

  // Format title and subtitle
  let formattedTitle = "Location Not In Local Index";
  let formattedSubtitle = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;

  if (bestPlace && bestAddress) {
    formattedTitle = bestPlace.name;
    formattedSubtitle = bestAddress.full_address;
  } else if (bestPlace) {
    formattedTitle = bestPlace.name;
    formattedSubtitle = bestPlace.address_text || `${bestPlace.category || "Place"} • ${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  } else if (bestAddress) {
    formattedTitle = bestAddress.full_address;
    formattedSubtitle = [bestAddress.suburb, bestAddress.state, bestAddress.postcode].filter(Boolean).join(" ") || "Australia";
  } else if (scoredPlaces.length > 0) {
    formattedTitle = `Near ${scoredPlaces[0].name}`;
    formattedSubtitle = scoredPlaces[0].address_text || `Approx. ${scoredPlaces[0].distance_meters}m away`;
  }

  const executionTimeMs = Math.round((performance.now() - startTime) * 100) / 100;

  return {
    query: { lat, lon },
    match_type: matchType,
    place: bestPlace || (scoredPlaces.length > 0 ? scoredPlaces[0] : null),
    address: bestAddress,
    formatted_title: formattedTitle,
    formatted_subtitle: formattedSubtitle,
    nearby_places: includeNearby ? scoredPlaces.slice(0, 8) : [],
    nearby_addresses: includeNearby ? scoredAddresses.slice(0, 8) : [],
    execution_time_ms: executionTimeMs,
  };
}

const STREET_ABBREVS: Record<string, string[]> = {
  rd: ["road", "rd"],
  road: ["road", "rd"],
  st: ["street", "st"],
  street: ["street", "st"],
  ave: ["avenue", "ave", "av"],
  av: ["avenue", "ave", "av"],
  avenue: ["avenue", "ave", "av"],
  dr: ["drive", "dr"],
  drive: ["drive", "dr"],
  cl: ["close", "cl"],
  close: ["close", "cl"],
  cres: ["crescent", "cres", "cr"],
  cr: ["crescent", "cres", "cr"],
  crescent: ["crescent", "cres", "cr"],
  ct: ["court", "ct"],
  court: ["court", "ct"],
  hwy: ["highway", "hwy"],
  highway: ["highway", "hwy"],
  pde: ["parade", "pde"],
  parade: ["parade", "pde"],
  pl: ["place", "pl"],
  place: ["place", "pl"],
  tce: ["terrace", "tce"],
  terrace: ["terrace", "tce"],
  ln: ["lane", "ln"],
  lane: ["lane", "ln"],
  bvd: ["boulevard", "bvd", "blvd"],
  blvd: ["boulevard", "bvd", "blvd"],
  boulevard: ["boulevard", "bvd", "blvd"],
  esp: ["esplanade", "esp"],
  esplanade: ["esplanade", "esp"],
  cct: ["circuit", "cct"],
  circuit: ["circuit", "cct"],
  way: ["way"],
  sq: ["square", "sq"],
  square: ["square", "sq"],
};

function damerauLevenshtein(s1: string, s2: string): number {
  const len1 = s1.length;
  const len2 = s2.length;
  if (len1 === 0) return len2;
  if (len2 === 0) return len1;
  if (s1 === s2) return 0;

  const d: number[][] = Array.from({ length: len1 + 1 }, () =>
    Array(len2 + 1).fill(0)
  );
  for (let i = 0; i <= len1; i++) d[i][0] = i;
  for (let j = 0; j <= len2; j++) d[0][j] = j;

  for (let i = 1; i <= len1; i++) {
    for (let j = 1; j <= len2; j++) {
      const cost = s1[i - 1] === s2[j - 1] ? 0 : 1;
      d[i][j] = Math.min(
        d[i - 1][j] + 1,
        d[i][j - 1] + 1,
        d[i - 1][j - 1] + cost
      );
      if (i > 1 && j > 1 && s1[i - 1] === s2[j - 2] && s1[i - 2] === s2[j - 1]) {
        d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
      }
    }
  }
  return d[len1][len2];
}

/**
 * Unified Search across POIs and Addresses with elastic autocomplete and typo tolerance
 */
export function search(
  query: string,
  options: { lat?: number; lon?: number; limit?: number } = {}
): SearchResultItem[] {
  const db = getDb();
  const limit = options.limit ?? 15;
  const cleanQuery = query.trim().toLowerCase().replace(/[^\w\s-]/g, " ");
  if (!cleanQuery) return [];

  const rawTokens = cleanQuery.split(/\s+/).filter((w) => w.length > 0);
  if (rawTokens.length === 0) return [];

  let houseNum: string | null = null;
  const coreTokens: string[] = [];
  let streetType: string | null = null;

  for (const t of rawTokens) {
    if (!houseNum && /^[0-9]+[a-z]?$|^[0-9]+-[0-9]+[a-z]?$/.test(t)) {
      houseNum = t;
    } else if (!streetType && STREET_ABBREVS[t]) {
      streetType = t;
    } else {
      coreTokens.push(t);
    }
  }

  // Build FTS query with synonym expansion
  const clauses: string[] = [];
  for (const t of rawTokens) {
    if (STREET_ABBREVS[t]) {
      clauses.push(`(${STREET_ABBREVS[t].map((s) => s + "*").join(" OR ")})`);
    } else {
      clauses.push(`${t}*`);
    }
  }
  const ftsMatch = clauses.join(" AND ");

  interface Candidate {
    item: SearchResultItem;
    score: number;
    number?: string;
  }
  const candidateMap = new Map<string, Candidate>();

  const scoreCandidate = (title: string, subtitle: string, candNum?: string, lat?: number, lon?: number): number => {
    let score = 0;
    const candTokens = (title + " " + subtitle).toLowerCase().split(/[^\w-]+/).filter(Boolean);

    if (houseNum) {
      if (candNum && candNum.toLowerCase() === houseNum) score += 120;
      else if (title.toLowerCase().startsWith(houseNum + " ")) score += 100;
      else if (candTokens.includes(houseNum)) score += 60;
      else score -= 20;
    }

    const allQueryWords = [...coreTokens];
    if (streetType) allQueryWords.push(streetType);

    let matched = 0;
    for (const qw of allQueryWords) {
      let best = 0;
      for (const ct of candTokens) {
        if (qw === ct) {
          best = Math.max(best, 60);
        } else if (STREET_ABBREVS[qw]?.includes(ct)) {
          best = Math.max(best, 55);
        } else if (ct.startsWith(qw)) {
          best = Math.max(best, 45 + (qw.length / ct.length) * 10);
        } else if (qw.length >= 4 && ct.length >= 4) {
          const d = damerauLevenshtein(qw, ct);
          if (d === 1) best = Math.max(best, 38);
          else if (qw.length >= 6 && d === 2) best = Math.max(best, 22);
        }
      }
      if (best > 0) {
        matched++;
        score += best;
      } else {
        score -= 15;
      }
    }

    if (allQueryWords.length > 0 && matched === allQueryWords.length) {
      score += 40;
    }

    if (options.lat != null && options.lon != null && lat != null && lon != null) {
      const dist = haversineDistance(options.lat, options.lon, lat, lon);
      score += 15 / (1 + dist / 10000);
    }
    return score;
  };

  try {
    // 1. Exact token-expanded FTS search
    const matchedPlaces = db
      .prepare(
        `SELECT p.id, p.name, p.category, p.address_text, p.lat, p.lon
         FROM places_fts f
         JOIN places p ON p.id = f.rowid
         WHERE places_fts MATCH ?
         LIMIT 30`
      )
      .all(ftsMatch) as unknown as PlaceSearchRow[];

    for (const p of matchedPlaces) {
      const key = `p:${p.id}`;
      const subtitle = p.address_text || "Australia";
      const score = scoreCandidate(p.name, subtitle, undefined, p.lat, p.lon);
      candidateMap.set(key, {
        item: {
          type: "place",
          id: p.id,
          title: p.name,
          subtitle,
          category: p.category,
          lat: p.lat,
          lon: p.lon,
        },
        score,
      });
    }

    const matchedAddrs = db
      .prepare(
        `SELECT a.id, a.full_address, a.number, a.suburb, a.state, a.postcode, a.lat, a.lon
         FROM addresses_fts f
         JOIN addresses a ON a.id = f.rowid
         WHERE addresses_fts MATCH ?
         LIMIT 40`
      )
      .all(ftsMatch) as unknown as AddressSearchRow[];

    for (const a of matchedAddrs) {
      const key = `a:${a.id}`;
      const subtitle = [a.suburb, a.state, a.postcode].filter(Boolean).join(" ");
      const score = scoreCandidate(a.full_address, subtitle, a.number || undefined, a.lat, a.lon);
      candidateMap.set(key, {
        item: {
          type: "address",
          id: a.id,
          title: a.full_address,
          subtitle,
          lat: a.lat,
          lon: a.lon,
        },
        score,
        number: a.number ?? undefined,
      });
    }

    // 2. Typo-tolerant candidate queries if results < limit
    if (candidateMap.size < limit) {
      const typoQueries: string[] = [];
      const streetClause = streetType
        ? `(${STREET_ABBREVS[streetType]?.map((s) => s + "*").join(" OR ")})`
        : "";

      for (const ct of coreTokens) {
        if (ct.length < 4) continue;
        const prefixes = [ct.slice(0, 3) + "*"];
        if (ct.length >= 5) prefixes.push(ct.slice(0, 4) + "*");
        for (const p of prefixes) {
          if (houseNum && streetClause) typoQueries.push(`${houseNum}* AND ${p} AND ${streetClause}`);
          if (houseNum) typoQueries.push(`${houseNum}* AND ${p}`);
          if (streetClause) typoQueries.push(`${p} AND ${streetClause}`);
          typoQueries.push(p);
        }
      }

      for (const tq of typoQueries) {
        if (candidateMap.size >= limit * 2) break;
        try {
          const tAddrs = db
            .prepare(
              `SELECT a.id, a.full_address, a.number, a.suburb, a.state, a.postcode, a.lat, a.lon
               FROM addresses_fts f
               JOIN addresses a ON a.id = f.rowid
               WHERE addresses_fts MATCH ?
               LIMIT 30`
            )
            .all(tq) as unknown as AddressSearchRow[];

          for (const a of tAddrs) {
            const key = `a:${a.id}`;
            if (candidateMap.has(key)) continue;
            const subtitle = [a.suburb, a.state, a.postcode].filter(Boolean).join(" ");
            const score = scoreCandidate(a.full_address, subtitle, a.number || undefined, a.lat, a.lon);
            candidateMap.set(key, {
              item: {
                type: "address",
                id: a.id,
                title: a.full_address,
                subtitle,
                lat: a.lat,
                lon: a.lon,
              },
              score,
              number: a.number ?? undefined,
            });
          }
        } catch {}
      }

      // House number anchor lookup
      if (candidateMap.size < limit && houseNum) {
        const numAddrs = db
          .prepare(
            `SELECT id, full_address, number, suburb, state, postcode, lat, lon
             FROM addresses
             WHERE number = ? OR number LIKE ?
             LIMIT 30`
          )
          .all(houseNum, `${houseNum}%`) as unknown as AddressSearchRow[];

        for (const a of numAddrs) {
          const key = `a:${a.id}`;
          if (candidateMap.has(key)) continue;
          const subtitle = [a.suburb, a.state, a.postcode].filter(Boolean).join(" ");
          const score = scoreCandidate(a.full_address, subtitle, a.number || undefined, a.lat, a.lon);
          candidateMap.set(key, {
            item: {
              type: "address",
              id: a.id,
              title: a.full_address,
              subtitle,
              lat: a.lat,
              lon: a.lon,
            },
            score,
            number: a.number ?? undefined,
          });
        }
      }
    }
  } catch (err) {
    console.warn("FTS search error:", err);
  }

  const sorted = Array.from(candidateMap.values())
    .filter((c) => c.score >= 25 || candidateMap.size <= limit)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);

  return sorted.map((c) => {
    if (options.lat != null && options.lon != null) {
      c.item.distance_meters = haversineDistance(options.lat, options.lon, c.item.lat, c.item.lon);
    }
    return c.item;
  });
}

/**
 * Get all nearby places and addresses for map exploration
 */
export function getNearby(
  lat: number,
  lon: number,
  radiusMeters = 500,
  limit = 40
): { places: Place[]; addresses: Address[] } {
  const db = getDb();
  const latDelta = radiusMeters / 111000;
  const lonDelta = radiusMeters / (111000 * Math.cos((lat * Math.PI) / 180));

  const boxMinX = lon - lonDelta;
  const boxMaxX = lon + lonDelta;
  const boxMinY = lat - latDelta;
  const boxMaxY = lat + latDelta;

  const places = (
    db
      .prepare(
        `
      SELECT p.id, p.uuid, p.name, p.category, p.brand, p.address_text,
             p.suburb, p.state, p.postcode, p.lat, p.lon, p.phone, p.website, p.confidence
      FROM places_rtree r
      JOIN places p ON p.id = r.id
      WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
      LIMIT ?
    `
      )
      .all(boxMaxX, boxMinX, boxMaxY, boxMinY, limit) as unknown as Place[]
  ).map((p) => ({
    ...p,
    distance_meters: haversineDistance(lat, lon, p.lat, p.lon),
  }));

  const addresses = (
    db
      .prepare(
        `
      SELECT a.id, a.uuid, a.unit, a.number, a.street, a.suburb,
             a.state, a.postcode, a.full_address, a.lat, a.lon
      FROM addresses_rtree r
      JOIN addresses a ON a.id = r.id
      WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
      LIMIT ?
    `
      )
      .all(boxMaxX, boxMinX, boxMaxY, boxMinY, limit) as unknown as Address[]
  ).map((a) => ({
    ...a,
    distance_meters: haversineDistance(lat, lon, a.lat, a.lon),
  }));

  return { places, addresses };
}

/**
 * Get database statistics
 */
export function getDbStats(): DbStats {
  const db = getDb();
  const placeCount = (
    db.prepare("SELECT count(*) as c FROM places").get() as { c: number }
  ).c;
  const addressCount = (
    db.prepare("SELECT count(*) as c FROM addresses").get() as { c: number }
  ).c;
  const regions = db
    .prepare("SELECT * FROM indexed_regions ORDER BY id DESC")
    .all() as unknown as DbStats["regions"];

  const dbPath = path.resolve(process.cwd(), "data", "australia_geo.db");
  const stats = fs.statSync(dbPath);
  const sizeMb = (stats.size / (1024 * 1024)).toFixed(1);

  return {
    placeCount,
    addressCount,
    regions,
    dbSizeBytes: stats.size,
    dbSizeFormatted: `${sizeMb} MB`,
  };
}
