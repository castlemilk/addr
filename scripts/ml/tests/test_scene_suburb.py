from types import SimpleNamespace

import pytest
from PIL import Image
from scripts.ml import scene_suburb as s
from scripts.ml.scene_confidence import geographic
from scripts.ml.tiles import lonlat_to_tile_xy
from shapely.geometry import box
from shapely.ops import unary_union


def test_plan_covers_entire_polygon_without_bbox_only_tiles():
    a = s.tile_polygon(17, 118442, 80414)
    b = s.tile_polygon(17, 118443, 80415)
    boundary = unary_union([a, b])
    tiles = s.tile_plan(boundary)
    assert len(tiles) == 2
    assert unary_union([s.tile_polygon(*t) for t in tiles]).symmetric_difference(boundary).area < 1e-14


def test_capture_halo_frame_and_core_ownership(tmp_path, monkeypatch):
    class Fetcher:
        def __init__(self, **kwargs):
            pass

        def window(self, *args):
            return SimpleNamespace(image=Image.new('RGB', (1152, 1152)), tile_span_meters=240,
                                   base_px=1024, imagery_zoom=19)
    monkeypatch.setattr(s, 'CompleteImagery', Fetcher)
    plan = {'provider': 'google', 'imagery_offset': 2, 'out_px': 512, 'margin_px': 32}
    tile = [17, 118442, 80414]
    scene, data = s.capture('test', tile, plan, tmp_path)
    assert data.startswith(b'\x89PNG')
    assert scene['region']['imagery_zoom'] == 19
    center = geographic({'ring': [[570, 570], [582, 570], [582, 582], [570, 582]]}, scene).centroid
    tx, ty = lonlat_to_tile_xy(center.x, center.y, 17)
    assert (tx, ty) == pytest.approx((tile[1]+.5, tile[2]+.5))
    scene['objects'] = [{'id': 'inside', 'ring': [[500, 500], [600, 500], [600, 600], [500, 600]]},
                        {'id': 'halo', 'ring': [[0, 500], [10, 500], [10, 600], [0, 600]]}]
    original = list(scene['objects'][0]['ring'])
    s.ownership(scene, tile, box(140, -40, 150, -30))
    assert scene['owned_object_ids'] == ['inside']
    assert scene['objects'][0]['ring'] == original


def test_missing_child_fails_instead_of_black_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(s.ImageryFetcher, '_child', lambda *args: None)
    fetcher = s.CompleteImagery(cache_dir=tmp_path)
    with pytest.raises(ValueError, match='no partial capture'):
        fetcher.window(17, 118442, 80414)


def test_resume_preserves_completed_capture_and_retries_failure(tmp_path, monkeypatch):
    import hashlib
    import io
    import json

    from scripts.ml import sam3
    from scripts.ml.scene_batch import signature
    from scripts.ml.scene_region import region_frame
    from scripts.ml.scene_training import atomic_json

    boundary = box(145, -38, 146, -37)
    plan = {'code': {}, 'weights': {}, 'roof_form': None, 'classes': ['building'], 'prompts': {'building': 'building'},
            'tiles': [[17, 118442, 80414], [17, 118443, 80414]],
            'boundary': {'features': [{'geometry': s.mapping(boundary)}]}}
    directory = tmp_path/s.SUBDIR/'runs'/signature(plan)[:16]
    (directory/'snapshot').mkdir(parents=True)
    atomic_json(directory/'plan.json', plan)
    monkeypatch.chdir(directory/'snapshot')
    monkeypatch.setattr(s, 'model_fingerprint', lambda path: {})
    monkeypatch.setattr(sam3, 'Sam3TextSegmenter', lambda path: object())
    monkeypatch.setattr(s, 'assess', lambda *args: {'objects': []})
    attempts = []

    def capture(sid, tile, plan, root):
        image = Image.new('RGB', (512, 512))
        out = io.BytesIO()
        image.save(out, format='PNG')
        data = out.getvalue()
        frame = region_frame([145.31, -37.7905, 145.311, -37.7895])
        return {**frame, 'id': sid, 'image_width': 512, 'image_height': 512, 'image_sha256': hashlib.sha256(data).hexdigest(),
                'suburb_run': directory.name, 'objects': []}, data

    def analyse(source, *args, **kwargs):
        attempts.append(source['id'])
        if source['id'].endswith('118443-80414') and len(attempts) == 2:
            raise ValueError('test retry')
        return {**source, 'analysis': {}, 'objects': []}

    monkeypatch.setattr(s, 'capture', capture)
    monkeypatch.setattr(s, 'analyse_scene', analyse)
    first = s.run(directory, tmp_path)
    assert first['counts'] == {'complete': 1, 'failed': 1}
    path = next((tmp_path/'data/scenes').glob('*/latest.json'))
    before = path.read_bytes()
    assert s.run(directory, tmp_path)['counts'] == {'complete': 2}
    assert path.read_bytes() == before
    assert len(attempts) == 3
    assert json.loads((directory/'run.json').read_text())['status'] == 'complete'
