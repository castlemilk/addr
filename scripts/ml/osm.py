"""OpenStreetMap building footprint ingestion via the Overpass API."""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

DEFAULT_LEVEL_HEIGHT_M = 3.1


@dataclass
class OsmFootprint:
    ring: list[tuple[float, float]]
    height: float | None = None
    min_height: float | None = None
    levels: int | None = None
    tags: dict = field(default_factory=dict)

    @property
    def source(self) -> str:
        return "osm"

    def bbox(self) -> tuple[float, float, float, float]:
        lons = [point[0] for point in self.ring]
        lats = [point[1] for point in self.ring]
        return min(lons), min(lats), max(lons), max(lats)


def _parse_meters(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("meters", "").replace("metres", "").strip()
    text = text.removesuffix("m").strip()
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def parse_levels(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).split(";")[0]))
    except ValueError:
        return None


def extract_height(tags: dict) -> tuple[float | None, float | None, int | None]:
    levels = parse_levels(tags.get("building:levels"))
    height = _parse_meters(tags.get("height"))
    min_height = _parse_meters(tags.get("min_height"))
    min_levels = parse_levels(tags.get("building:min_level"))
    if height is None and levels:
        height = levels * DEFAULT_LEVEL_HEIGHT_M
    if min_height is None and min_levels:
        min_height = min_levels * DEFAULT_LEVEL_HEIGHT_M
    return height, min_height, levels


def parse_overpass(payload: dict) -> list[OsmFootprint]:
    footprints: list[OsmFootprint] = []
    for element in payload.get("elements", []):
        if element.get("type") != "way":
            continue
        tags = element.get("tags") or {}
        if "building" not in tags and "building:part" not in tags:
            continue
        geometry = element.get("geometry") or []
        ring = [
            (float(node["lon"]), float(node["lat"]))
            for node in geometry
            if "lon" in node and "lat" in node
        ]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(ring) < 3:
            continue
        height, min_height, levels = extract_height(tags)
        footprints.append(
            OsmFootprint(
                ring=ring,
                height=height,
                min_height=min_height,
                levels=levels,
                tags={key: value for key, value in tags.items() if key != "geometry"},
            )
        )
    return footprints


def build_query(bbox: tuple[float, float, float, float], timeout: int = 180) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        f"[out:json][timeout:{timeout}];"
        f'way["building"]({min_lat},{min_lon},{max_lat},{max_lon});'
        "out geom;"
    )


def fetch_osm_buildings(
    bbox: tuple[float, float, float, float],
    endpoint: str | None = None,
    timeout: int = 180,
    session: requests.Session | None = None,
) -> list[OsmFootprint]:
    session = session or requests.Session()
    session.headers.setdefault(
        "User-Agent",
        "addr-ai-building-pipeline/1.0 (+https://github.com/benebsworth/addr)",
    )
    query = build_query(bbox, timeout)
    endpoints = [endpoint] if endpoint else OVERPASS_ENDPOINTS
    last_error: Exception | None = None
    for candidate in endpoints:
        if not candidate:
            continue
        try:
            response = session.post(
                candidate, data={"data": query}, timeout=timeout + 30
            )
            response.raise_for_status()
            return parse_overpass(response.json())
        except Exception as error:
            last_error = error
    if last_error is not None:
        raise last_error
    return []
