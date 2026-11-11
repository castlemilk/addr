import hashlib
import json

import numpy as np
import pytest
from PIL import Image
from scripts.ml import optical_layers, roof_line_fit, scene_stack
from scripts.ml.optical_layers import (
    CHEAP_LAYERS,
    MULTIDATE,
    SAME_PIXEL_LAYERS,
    build,
    contact_sheet,
    evidence_views,
    find_stack,
    guided_filter,
    layer_arrays,
    multidate_median,
    pairs,
    provenance,
)
from scripts.ml.roof_line_fit import image_segments
from scripts.ml.tests.test_roof_line_fit import FRAME, ROOF, roof_image


def textured_image(seed=3):
    """The synthetic roof with deterministic tile texture, so smoothers have something to flatten."""
    rng = np.random.default_rng(seed)
    pixels = np.asarray(roof_image()).astype(float)
    pixels += rng.normal(0, 6, pixels.shape[:2])[:, :, None]
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def test_build_returns_every_same_pixel_layer_on_the_saved_grid_as_uint8():
    image = roof_image()
    layers = build(image)
    assert tuple(layers) == SAME_PIXEL_LAYERS
    assert set(CHEAP_LAYERS) < set(layers)
    for name, array in layers.items():
        assert array.shape == (image.height, image.width), name
        assert array.dtype == np.uint8, name
        assert array.flags['C_CONTIGUOUS'], name
    assert image.tobytes() == roof_image().tobytes(), 'the source image is never modified'


def test_layers_are_deterministic_and_distinct():
    image = textured_image()
    first, second = build(image, pixels_per_meter=20.), build(image, pixels_per_meter=20.)
    for name in first:
        assert first[name].tobytes() == second[name].tobytes(), name
    digests = {name: hashlib.sha256(a.tobytes()).hexdigest() for name, a in first.items()}
    assert len(set(digests.values())) == len(digests), 'every layer is a different transform'
    assert build(np.asarray(image))['gradient'].tobytes() == first['gradient'].tobytes(), 'array and PIL inputs agree'


def test_shadow_norm_removes_a_broad_brightness_offset_and_guided_keeps_edges():
    image = textured_image()
    layers = build(image, pixels_per_meter=20.)
    brighter = Image.fromarray(np.clip(np.asarray(image).astype(int)+25, 0, 255).astype(np.uint8))
    shifted = build(brighter, pixels_per_meter=20.)
    interior = (slice(40, 200), slice(40, 240))
    assert np.abs(layers['shadow_norm'][interior].astype(int)-shifted['shadow_norm'][interior]).mean() < 2.5
    assert abs(int(layers['bilateral'][interior].mean())-int(shifted['bilateral'][interior].mean())) > 15
    flat = np.full((40, 60), 90., np.float64)
    assert np.allclose(guided_filter(flat, 4, 150.), 90.)
    step = np.full((40, 60), 90., np.float64)
    step[:, 30:] = 170.
    smoothed = guided_filter(step, 4, 150.)
    assert abs(smoothed[:, :20].mean()-90.) < 3 and abs(smoothed[:, 40:].mean()-170.) < 3
    noisy = step+np.random.default_rng(1).normal(0, 5, step.shape)
    assert guided_filter(noisy, 4, 150.)[:, :20].std() < noisy[:, :20].std()*.5


def test_provenance_records_hashes_params_source_and_stack_binding():
    image = roof_image()
    layers = build(image)
    rows = provenance(layers, source_image_sha256='ab'*32, pixels_per_meter=4.)
    assert [r['name'] for r in rows] == list(layers)
    for row in rows:
        assert row['sha256'] == hashlib.sha256(layers[row['name']].tobytes()).hexdigest()
        assert row['source_image_sha256'] == 'ab'*32 and row['stack_id'] is None
        assert row['params']['pixels_per_meter'] == 4. and row['params'] == {**optical_layers.LAYER_PARAMS[row['name']], 'pixels_per_meter': 4.}
    with_median = {**layers, MULTIDATE: layers['bilateral'].copy()}
    rows = provenance(with_median, source_image_sha256='ab'*32, stack_id='0123456789abcdef')
    assert [r['stack_id'] for r in rows] == [None]*len(layers)+['0123456789abcdef']
    with pytest.raises(ValueError, match='Unknown layer'):
        provenance({'mystery': layers['bilateral']}, source_image_sha256='ab'*32)


def test_evidence_views_prefix_same_pixel_layers_and_leave_the_median_gated_on_the_original():
    layers = {**build(roof_image()), MULTIDATE: np.zeros((240, 280), np.uint8)}
    views = evidence_views(layers, stack_id='0123456789abcdef')
    assert [v['name'] for v in views] == [f'layer:{n}' for n in SAME_PIXEL_LAYERS]+[MULTIDATE]
    assert all(roof_line_fit.is_layer_view(v['name']) for v in views[:-1]) and not roof_line_fit.is_layer_view(views[-1]['name'])
    assert [v['stack_id'] for v in views] == [None]*len(SAME_PIXEL_LAYERS)+['0123456789abcdef']
    cheap = pairs(layers, names=CHEAP_LAYERS)
    assert [name for name, _ in cheap] == ['layer:bilateral', 'layer:unsharp', 'layer:shadow_norm']
    assert roof_line_fit.validate_extra_views(cheap, roof_image()) == [name for name, _ in cheap]
    with pytest.raises(ValueError, match='not built'):
        pairs(layers, names=('bilateral', 'missing'))


def test_layers_feed_image_segments_with_origins_and_evidence_layers():
    image = roof_image()
    views = pairs(build(image))
    default = image_segments(image, ROOF, FRAME)
    with_layers = image_segments(image, ROOF, FRAME, extra_views=views)
    assert len(with_layers) >= len(default) > 0
    names = {name for name, _ in views}
    assert all(s['view_origin'] in {'gray', 'clahe', *names} for s in with_layers)
    assert all(s['evidence_layer'] == 'original' or s['evidence_layer'] == s['view_origin'] for s in with_layers)
    assert all('evidence_layer' not in s and 'view_origin' not in s for s in default)


def _stack_folder(tmp_path, scene_id, image, *, status='available', stack_id='0123456789abcdef', computed_at='2026-09-26T00:00:00+00:00',
                  median=None, source_sha=None):
    folder = tmp_path/scene_stack.STACKS/stack_id
    folder.mkdir(parents=True)
    height, width = image.height, image.width
    median = np.full((height, width), 77, np.uint8) if median is None else median
    consensus = np.zeros((height, width), np.uint8)
    assets = {}
    for name, array in (('stack-median.png', median), ('edge-consensus.png', consensus)):
        Image.fromarray(array).save(folder/name)
        assets[name] = {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}
    report = {'stack_id': stack_id, 'status': status, 'scene_id': scene_id, 'computed_at': computed_at,
              'source_image_sha256': source_sha or hashlib.sha256(image.tobytes()).hexdigest(),
              'stack': {'views': 3}, 'assets': assets}
    (folder/'report.json').write_text(json.dumps(report))
    return report


def test_find_stack_returns_the_newest_available_report_for_the_scene(tmp_path):
    image = roof_image()
    assert find_stack('scene-a', tmp_path) is None
    _stack_folder(tmp_path, 'scene-a', image, stack_id='a'*16, computed_at='2026-09-25T00:00:00+00:00')
    newest = _stack_folder(tmp_path, 'scene-a', image, stack_id='b'*16, computed_at='2026-09-26T00:00:00+00:00')
    _stack_folder(tmp_path, 'scene-a', image, stack_id='c'*16, status='pair', computed_at='2026-09-27T00:00:00+00:00')
    _stack_folder(tmp_path, 'scene-b', image, stack_id='d'*16, computed_at='2026-09-28T00:00:00+00:00')
    assert find_stack('scene-a', tmp_path)['stack_id'] == newest['stack_id']
    assert find_stack('scene-c', tmp_path) is None


def test_multidate_median_is_hash_bound_to_the_saved_image_and_needs_an_available_stack(tmp_path):
    image = roof_image()
    sha = hashlib.sha256(image.tobytes()).hexdigest()
    report = _stack_folder(tmp_path, 'scene-a', image, source_sha=sha)
    median, stack_id = multidate_median(report, image, root=tmp_path, image_sha256=sha)
    assert median.shape == (image.height, image.width) and median.dtype == np.uint8 and int(median[0, 0]) == 77
    assert stack_id == report['stack_id']
    layers = build(image, stack=report['stack_id'], root=tmp_path, image_sha256=sha)
    assert tuple(layers) == (*SAME_PIXEL_LAYERS, MULTIDATE)
    rows = provenance(layers, source_image_sha256=sha, stack_id=stack_id)
    assert rows[-1] == {'name': MULTIDATE, 'params': optical_layers.LAYER_PARAMS[MULTIDATE], 'sha256': hashlib.sha256(median.tobytes()).hexdigest(),
                        'source_image_sha256': sha, 'stack_id': stack_id}
    with pytest.raises(ValueError, match='different saved image'):
        multidate_median(report, image, root=tmp_path, image_sha256='0'*64)
    pair = _stack_folder(tmp_path, 'scene-a', image, status='pair', stack_id='e'*16, source_sha=sha)
    with pytest.raises(ValueError, match='available stack'):
        multidate_median(pair, image, root=tmp_path, image_sha256=sha)
    (tmp_path/scene_stack.STACKS/report['stack_id']/'stack-median.png').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='sha256'):
        multidate_median(report, image, root=tmp_path, image_sha256=sha)


def test_contact_sheet_renders_every_layer_through_one_crop():
    image = roof_image()
    layers = build(image, pixels_per_meter=4.)
    sheet = contact_sheet(image, layers, pixels_per_meter=4., title='synthetic')
    assert isinstance(sheet, Image.Image) and sheet.mode == 'RGB'
    tiles = 1+len(layers)
    rows = (tiles+2)//3
    assert sheet.width == 3*360 and sheet.height == rows*360+18
    centred = contact_sheet(image, layers, center_px=(20, 20), pixels_per_meter=4., crop_m=10, tile_px=120, columns=4, title='')
    assert centred.size == (4*120, 2*120)
    assert np.asarray(sheet).std() > 0, 'the sheet is not blank'


def test_cli_writes_npy_layers_provenance_and_sheet(tmp_path, monkeypatch):
    image = roof_image()
    scene = {'image_sha256': 'cd'*32, 'meters_to_pixel': FRAME.tolist(), 'image_width': image.width, 'image_height': image.height}
    monkeypatch.setattr('scripts.ml.scene_roof_diagnostics.load_scene', lambda scene_id, root: (scene, image))
    out = tmp_path/'layers'
    assert optical_layers.main(['--scene', 'synthetic', '--root', str(tmp_path), '--out', str(out), '--no-stack']) == 0
    document = json.loads((out/'provenance.json').read_text())
    assert document['version'] == optical_layers.VERSION and document['scene_id'] == 'synthetic'
    assert document['source_image_sha256'] == 'cd'*32 and document['stack_id'] is None
    assert document['pixels_per_meter'] == pytest.approx(20.)
    assert document['view_names'] == [f'layer:{n}' for n in SAME_PIXEL_LAYERS]
    for row in document['layers']:
        array = np.load(out/f"{row['name']}.npy")
        assert array.dtype == np.uint8 and array.shape == (image.height, image.width)
        assert hashlib.sha256(array.tobytes()).hexdigest() == row['sha256']
    assert (out/'contact-sheet.png').exists()
    assert Image.open(out/'contact-sheet.png').size[0] == 3*360
    assert sorted(p.name for p in out.iterdir()) == sorted([*(f'{n}.npy' for n in SAME_PIXEL_LAYERS), 'provenance.json', 'contact-sheet.png'])


def test_layer_arrays_reject_bad_input():
    with pytest.raises(ValueError, match='pixels_per_meter'):
        layer_arrays(roof_image(), pixels_per_meter=0)
    with pytest.raises(ValueError, match='HxWx3 uint8'):
        layer_arrays(np.zeros((10, 10), np.uint8))
