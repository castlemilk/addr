"""Independent geometry validation for saved reviewer vertices and roof faces."""
import json
import sys

from shapely.geometry import Polygon

from .roof_structure import audit_roof


def check_annotation(annotation, ring, eave, holes=None):
    vertices = annotation["vertices"]
    parts = [{"coords": [vertices[i][:2] for i in face], "ring_z": [vertices[i][2] for i in face]}
             for face in annotation["faces"]]
    return audit_roof(parts, Polygon(ring, holes), eave, pitched=annotation["roof_type"] != "flat")


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    print(json.dumps(check_annotation(payload["annotation"], payload["ring"], payload["eave"], payload.get("holes"))))
