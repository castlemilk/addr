"""Shared footprint dataclasses and planar merge logic.

OSM cadastre is authoritative: where an ML footprint matches an OSM footprint the
OSM geometry wins, and a depth-derived height is backfilled only when OSM lacks
one. Unmatched ML footprints are kept so AI fills gaps in the open data.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from shapely.geometry import Polygon

ML_SOURCE = "ml"
OSM_SOURCE = "osm"


@dataclass
class LocalFootprint:
    geometry: Polygon
    source: str
    height: float | None = None
    min_height: float | None = None
    levels: int | None = None
    confidence: float | None = None
    area_m2: float | None = None
    kind: str | None = None
    roof_type: str | None = None
    eave_height: float | None = None
    ridge_height: float | None = None
    ridge_angle_deg: float | None = None


def merge_footprints(
    ml: list[LocalFootprint],
    osm: list[LocalFootprint],
    iou_threshold: float = 0.45,
    max_center_distance: float = 25.0,
) -> list[LocalFootprint]:
    merged: list[LocalFootprint] = []
    osm_list = list(osm)

    for candidate in ml:
        if candidate.geometry.is_empty or candidate.geometry.area <= 0:
            continue
        centroid = candidate.geometry.centroid
        best_index = -1
        best_iou = 0.0
        for index, existing in enumerate(osm_list):
            if existing.geometry.is_empty or existing.geometry.area <= 0:
                continue
            if centroid.distance(existing.geometry.centroid) > max_center_distance:
                continue
            if not candidate.geometry.intersects(existing.geometry):
                continue
            union_area = candidate.geometry.union(existing.geometry).area
            if union_area <= 0:
                continue
            iou = candidate.geometry.intersection(existing.geometry).area / union_area
            if iou > best_iou:
                best_iou = iou
                best_index = index

        if best_index >= 0 and best_iou >= iou_threshold:
            existing = osm_list[best_index]
            if existing.height is None and candidate.height is not None:
                osm_list[best_index] = replace(
                    existing,
                    height=candidate.height,
                    min_height=existing.min_height or candidate.min_height,
                    source="osm+ml",
                    confidence=candidate.confidence,
                )
            continue
        merged.append(candidate)

    merged.extend(osm_list)
    return merged


def footprint_properties(footprint: LocalFootprint) -> dict:
    properties: dict = {"source": footprint.source}
    if footprint.kind:
        properties["kind"] = footprint.kind
    if footprint.height is not None:
        properties["height"] = round(float(footprint.height), 1)
        properties["render_height"] = round(float(footprint.height), 1)
    if footprint.min_height:
        properties["min_height"] = round(float(footprint.min_height), 1)
        properties["render_min_height"] = round(float(footprint.min_height), 1)
    if footprint.levels:
        properties["levels"] = int(footprint.levels)
    if footprint.area_m2:
        properties["area_m2"] = round(float(footprint.area_m2), 1)
    if footprint.roof_type:
        properties["roof_type"] = footprint.roof_type
    if footprint.eave_height is not None:
        properties["eave_height"] = round(float(footprint.eave_height), 1)
    if footprint.ridge_height is not None:
        properties["ridge_height"] = round(float(footprint.ridge_height), 1)
    if footprint.ridge_angle_deg is not None:
        properties["ridge_angle"] = round(float(footprint.ridge_angle_deg), 1)
    if footprint.confidence is not None:
        properties["confidence"] = round(float(footprint.confidence), 3)
    return properties
