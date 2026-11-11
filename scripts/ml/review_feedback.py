"""Geometry-bound reviewer feedback shared by extraction and training export."""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_FEEDBACK = Path(__file__).resolve().parents[2] / "data/reviews/feedback.json"


def boundary_signature(record: dict) -> str:
    center, ring = record.get("centroid"), record.get("ring_meters")
    if not center or not ring:
        return ""
    return (f"{center['lon']:.7f},{center['lat']:.7f}|"
            + ";".join(f"{x:.2f},{y:.2f}" for x, y in ring))


def latest_feedback(path: Path = DEFAULT_FEEDBACK) -> dict[int, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    result = {}
    for key, item in data.items():
        address = key.rsplit(":", 1)[-1]
        if address.isdigit() and isinstance(item, dict):
            previous = result.get(int(address), {})
            if item.get("updated_at", "") >= previous.get("updated_at", ""):
                result[int(address)] = item
    return result


def rejected_cuts_for(record: dict, feedback: dict) -> list:
    signature = boundary_signature(record)
    if not signature or feedback.get("boundary_signature") != signature:
        return []
    return feedback.get("rejected_cuts", [])
