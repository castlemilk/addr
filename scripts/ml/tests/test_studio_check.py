from pathlib import Path

from PIL import Image
from scripts.ml.studio_check import analyse_canvas, shape_metrics, verdict

LAT, LON = -37.79044, 145.31252


def record(ring_meters, **extra):
    return {
        "address_id": 1,
        "full_address": "1 TEST STREET",
        "centroid": {"lat": LAT, "lon": LON},
        "ring_meters": ring_meters,
        "rooflines": {"parts": [{"area_m2": 380.0}]},
        **extra,
    }


def bar():
    # Convex bar: no reflex vertices, solidity 1.
    return record([[-15, -6], [15, -6], [15, 6], [-15, 6]])


def ell():
    # L with one reflex vertex (notch cut out of the north-east corner).
    return record(
        [
            [-15, -6],
            [15, -6],
            [15, 6],
            [2, 6],
            [2, 0],
            [-15, 0],
        ]
    )


def test_shape_convex_bar():
    shape = shape_metrics(bar())
    assert shape["ok"] is True
    assert shape["concave"] is False
    assert shape["reflex_vertices"] == 0
    assert shape["solidity"] == 1.0
    assert shape["vertices"] == 4


def test_shape_concave_ell():
    shape = shape_metrics(ell())
    assert shape["ok"] is True
    assert shape["concave"] is True
    assert shape["reflex_vertices"] == 1
    assert shape["max_reflex_deg"] == 270.0
    assert shape["solidity"] < 0.95


def test_shape_shallow_wobble_is_not_concave():
    # A 0.5 m dent in a 10 m wall: reflex angle ~191°, solidity ~1.
    dented = record([[-5, -5], [5, -5], [5, 5], [0, 4.5], [-5, 5]])
    shape = shape_metrics(dented)
    assert shape["ok"] is True
    assert shape["concave"] is False
    assert shape["solidity"] > 0.95
    assert shape["max_reflex_deg"] < 225.0


def test_shape_invalid_ring():
    shape = shape_metrics(record([[0, 0], [1, 1]]))
    assert shape["ok"] is False


def test_verdict_passes_clean_convex():
    shape = shape_metrics(bar())
    state, findings = verdict(
        shape=shape,
        wall_tris=40,
        roof_tris=60,
        canvas={"mesh_fill": 0.4, "edge_density": 0.05},
        page_errors=[],
        expected="convex",
    )
    assert (state, findings) == ("PASS", [])


def test_verdict_flags_expectation_mismatch():
    shape = shape_metrics(bar())
    state, findings = verdict(
        shape=shape,
        wall_tris=40,
        roof_tris=60,
        canvas={"mesh_fill": 0.4, "edge_density": 0.05},
        page_errors=[],
        expected="concave",
    )
    assert state == "REVIEW"
    assert any("convex" in finding for finding in findings)


def test_verdict_flags_empty_mesh_and_bad_canvas():
    shape = shape_metrics(bar())
    state, findings = verdict(
        shape=shape,
        wall_tris=0,
        roof_tris=0,
        canvas={"mesh_fill": 0.02, "edge_density": 0.001},
        page_errors=["boom"],
        expected=None,
    )
    assert state == "REVIEW"
    assert len(findings) == 3


def test_analyse_canvas_blank_vs_drawn(tmp_path: Path):
    blank = Image.new("RGB", (200, 200), (10, 12, 20))
    blank_path = tmp_path / "blank.png"
    blank.save(blank_path)
    drawn = blank.copy()
    for x in range(60, 140):
        for y in range(60, 140):
            drawn.putpixel((x, y), (200, 200, 200))
    drawn_path = tmp_path / "drawn.png"
    drawn.save(drawn_path)
    blank_stats = analyse_canvas(blank_path)
    drawn_stats = analyse_canvas(drawn_path)
    assert blank_stats["mesh_fill"] < 0.10
    assert drawn_stats["mesh_fill"] > 0.10
    assert drawn_stats["edge_density"] > blank_stats["edge_density"]
