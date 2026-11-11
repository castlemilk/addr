"""outline_reflection: measured geometry on synthetic outlines, the editor-hint rules it mirrors, and the write guard.

A synthetic 512 px scene at 0.1 m/px under tmp_path; nothing under the repo's data/ is read or written.
"""
from __future__ import annotations

import hashlib
import io
import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml import outline_reflection as reflection

SCALE = .1


def _ring(points_m):
    """Pixel ring for metre points: y down from the bottom of a 512 px frame so every object sits inside the image."""
    return [[x / SCALE, 512 - y / SCALE] for x, y in points_m]


def _rect(w, length, x0=0., y0=0.):
    return _ring([(x0, y0), (x0 + w, y0), (x0 + w, y0 + length), (x0, y0 + length)])


def _jittered(w=10., length=9., n=13):
    ring = []

    def side(ax, ay, bx, by):
        for i in range(n):
            t = i / n
            k = (1 if i % 2 else -1) * (.15 + .2 * ((i * 7) % 5) / 5)
            dx, dy = bx - ax, by - ay
            length = float(np.hypot(dx, dy))
            ring.append((ax + dx * t + (-dy / length) * k, ay + dy * t + (dx / length) * k))
    side(0, 0, w, 0)
    side(w, 0, w, length)
    side(w, length, 0, length)
    side(0, length, 0, 0)
    return _ring(ring)


def _scene(tmp_path, objects, size=512):
    frame = [[SCALE, 0.], [0., -SCALE], [0., 0.]]
    image = Image.fromarray(np.full((size, size, 3), 120, dtype=np.uint8))
    folder = tmp_path / 'data/scenes/qa-outline'
    folder.mkdir(parents=True)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    digest = hashlib.sha256(buffer.getvalue()).hexdigest()
    (folder / f'{digest}.png').write_bytes(buffer.getvalue())
    scene = {'id': 'qa-outline', 'image_width': size, 'image_height': size, 'image_sha256': digest, 'model_signature': 's' * 64,
             'pixel_to_meters': frame, 'meters_to_pixel': [[1 / SCALE, 0.], [0., -1 / SCALE], [0., 0.]], 'objects': objects}
    (folder / 'latest.json').write_text(json.dumps(scene))
    return scene


def _object(oid, ring, kind='building', **extra):
    return {'id': oid, 'kind': kind, 'status': 'candidate', 'ring': ring, 'label': kind, 'source': 'sam3-text-onnx',
            'model': {'area_m2': 0., 'height_m': 3.}, **extra}


def test_geometry_on_a_clean_rectangle_and_the_editor_rules(tmp_path):
    scene = _scene(tmp_path, [_object('big', _rect(12, 10, 30, 0)), _object('speck', _rect(.3, .3, 30, 30)), _object('noisy', _jittered())])
    big = reflection.geometry(scene['objects'][0], scene)
    assert big['area_m2'] == pytest.approx(120., abs=1e-6) and big['perimeter_m'] == pytest.approx(44., abs=1e-6)
    assert big['vertices'] == 4 and big['simplified_vertices'] == 4 and big['off_axis_edges'] == 0 and big['wonky_share'] == 0.
    assert (big['rect_width_m'], big['rect_length_m']) == (10., 12.) and big['rectilinearity'] == pytest.approx(1., abs=1e-6)
    assert big['orthogonalise']['passed'] is True and big['orthogonalise']['iou'] >= .99
    assert big['frame_edge_px'] == 0. and big['clipped_by_frame'] is None and big['replaced_by'] == [], 'the rectangle sits on the bottom frame edge'
    assert reflection.editor_hints('building', big) == []
    speck = reflection.geometry(scene['objects'][1], scene)
    assert speck['area_m2'] == pytest.approx(.09, abs=1e-6) and speck['rect_width_m'] == pytest.approx(.3, abs=1e-6)
    assert reflection.editor_hints('building', speck) == ['degenerate_outline', 'tiny_area_for_kind']
    assert reflection.editor_hints('tree', speck) == ['degenerate_outline', 'tiny_area_for_kind']
    assert reflection.editor_hints('car', speck) == ['degenerate_outline']
    noisy = reflection.geometry(scene['objects'][2], scene)
    assert noisy['vertices'] == 52 and noisy['vertices_per_metre'] >= .45 and noisy['wonky_share'] >= .85
    assert reflection.editor_hints('building', noisy) == ['noisy_outline']
    assert reflection.editor_hints('tree', noisy) == []
    near = reflection.neighbours(scene['objects'][1], scene)
    assert near['largest']['id'] == 'big' and near['largest']['ratio'] == round(.09 / 120, 3), 'ratio rounded to three decimals'
    assert near['nearest']['id'] in {'big', 'noisy'}


def test_reflect_reads_the_image_and_writes_only_under_the_given_folder(tmp_path, monkeypatch):
    clipped = _object('remnant', _rect(2, 2.4, 20, 20), clipped_by_frame=[256, 256, 768, 768], roof_type='hipped',
                      roof_parts=[{'ring_meters': [[0, 0], [1, 0], [1, 1]], 'ring_z': [3, 3, 3]}] * 8, roof_geometry_source='image-guided-prior')
    _scene(tmp_path, [_object('big', _rect(10, 9)), clipped, _object('taker', _rect(3, 3, 19.5, 19.5), replaces=['remnant'])])
    review = {'revision': 3, 'objects': [{'id': 'remnant', 'status': 'needs_adjustment', 'notes': 'reflect'}]}
    (tmp_path / 'data/scenes/qa-outline/review.json').write_text(json.dumps(review))
    before = {p: p.read_bytes() for p in (tmp_path / 'data/scenes/qa-outline').iterdir()}
    case = reflection.reflect(tmp_path, 'qa-outline', 'remnant')
    assert case['geometry']['clipped_by_frame'] == [256, 256, 768, 768] and case['geometry']['replaced_by'] == ['taker']
    assert case['fitter'] == {'roof_type': 'hipped', 'roof_parts': 8, 'roof_lines': 0, 'roof_geometry_source': 'image-guided-prior', 'roof_annotation': False,
                              'height_m': 3., 'source': 'sam3-text-onnx', 'score': None, 'label': 'building', 'structure_kind': None, 'geometry_reviewed': None}
    assert case['image_evidence'] is not None and case['image_evidence']['edges_measured'] >= 1
    assert case['image_evidence']['shadow_side_edges'] == 0, 'a flat grey image has no shadow side'
    assert case['human'] == {'revision': 3, 'status': 'needs_adjustment', 'notes': 'reflect', 'structure_kind': None, 'geometry_reviewed': None}
    assert case['served'] == {'available': False}, 'no eyes index under tmp_path'
    assert any('clipped_by_frame' in line for line in case['reflection']) and any(line.startswith('Fitter: roof_type hipped with 8 roof parts') for line in case['reflection'])
    assert any('under the 6 m² gate' in line for line in case['reflection'])
    assert reflection.reflect(tmp_path, 'qa-outline', 'nobody') == {'scene_id': 'qa-outline', 'object_id': 'nobody', 'status': 'object_missing'}
    assert {p: p.read_bytes() for p in (tmp_path / 'data/scenes/qa-outline').iterdir()} == before, 'nothing under data/scenes changed'
    # main() refuses a folder outside the reflections root and writes exactly two files inside it.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        reflection.main(['--case', 'qa-outline:remnant', '--out', str(tmp_path / 'elsewhere')])
    assert not (tmp_path / 'elsewhere').exists()
    out = tmp_path / reflection.REFLECTIONS_ROOT / '2026-09-30'
    reflection.main(['--case', 'qa-outline:remnant', '--case', 'qa-outline:nobody', '--out', str(out), '--population'])
    assert sorted(p.name for p in out.iterdir()) == ['REFLECTION.md', 'reflections.json']
    report = json.loads((out / 'reflections.json').read_text())
    assert report['version'] == 'outline-reflection-v1' and len(report['code_sha256']) == 64 and report['design'] == json.loads(json.dumps(reflection.DESIGN))
    assert [c.get('status') for c in report['cases']] == ['candidate', 'object_missing']
    assert report['population']['scenes'] == 1 and report['population']['live_buildings'] == 3 and report['population']['roof_parts_under_gate'] == 1
    assert report['population']['clipped_sam3_buildings'] == 1 and report['population']['clipped_sam3_under_gate_covered_80'] == 1
    text = (out / 'REFLECTION.md').read_text()
    assert 'never accuracy' in text and 'not applied' in text and 'Object absent from latest.json' in text
    assert {p: p.read_bytes() for p in (tmp_path / 'data/scenes/qa-outline').iterdir()} == before


def test_axis_deviation_and_pixel_classes():
    assert reflection.axis_deviation(92., 0.) == pytest.approx(2.)
    assert reflection.axis_deviation(44., 0.) == pytest.approx(44.)
    assert reflection.axis_deviation(47., 0.) == pytest.approx(43.)
    assert reflection.axis_deviation(183., 90.) == pytest.approx(3.)
    assert [reflection.pixel_class(v) for v in (.059, .127, .24)] == ['< 0.08', '0.08-0.2', '>= 0.2']
