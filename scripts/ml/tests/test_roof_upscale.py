"""Adapter contract tests; fake inference is explicitly not a Real-ESRGAN proof."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from scripts.ml import roof_upscale as upscale


@pytest.fixture
def crop_runtime(tmp_path, monkeypatch):
    source = tmp_path / 'source.png'
    Image.new('RGB', (80, 60), (113, 92, 64)).save(source)
    runtime = {'name': 'realesrgan-x4plus', 'scale': 4, 'weights_sha256': 'test-weights'}
    calls = []
    monkeypatch.setattr(upscale, '_runtime', lambda *_: (tmp_path / 'fake', tmp_path, runtime.copy()))

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        # An explicit unit-test stub. Real inference is verified separately on saved roof crops.
        with Image.open(command[command.index('-i') + 1]) as image:
            image.resize((image.width * 4, image.height * 4)).save(command[command.index('-o') + 1])
        return SimpleNamespace(returncode=0, stdout='unit-test stub', stderr='')

    monkeypatch.setattr(upscale.subprocess, 'run', fake_run)
    return source, tmp_path / 'cache', [[10, 12], [30, 12], [30, 32], [10, 32]], calls, runtime


def test_crop_affines_preserve_source_grid_and_clip_padding():
    crop = upscale.crop_geometry((100, 80), [[2, 5], [20, 5], [20, 25]], padding_px=8)
    assert crop['crop_box'] == [0, 0, 28, 33]
    assert crop['clipped_edges'] == ['left', 'top']
    for point in [[2.25, 5.75], [20, 25], [0, 0]]:
        enlarged = np.r_[point, 1] @ np.asarray(crop['source_to_upscaled'])
        restored = np.r_[enlarged, 1] @ np.asarray(crop['upscaled_to_source'])
        assert restored == pytest.approx(point)
    # Original pixel-center indices [2,5] map to the center of their 4x4 output group.
    source_center = np.array([9.5, 21.5, 1]) @ crop['upscaled_pixel_center_index_to_source']
    assert source_center == pytest.approx([2, 5])


def test_inference_is_explicit_photo_model_bounded_and_source_immutable(crop_runtime):
    source, cache, ring, calls, _ = crop_runtime
    before = source.read_bytes()
    result = upscale.upscale_roof_crop(source, ring, cache_dir=cache, padding_px=4, timeout=17)
    assert result['status'] == 'available' and not result['cache_hit']
    assert result['crop_box'] == [6, 8, 34, 36] and result['output_size'] == [112, 112]
    command, options = calls[0]
    assert command[command.index('-n') + 1] == 'realesrgan-x4plus'
    assert command[command.index('-s') + 1] == '4'
    assert command[command.index('-j') + 1] == '1:1:1'
    assert options['timeout'] == 17
    assert source.read_bytes() == before
    assert result['source_sha256'] == upscale.sha256_bytes(before)
    assert result['training_eligible'] is False
    again = upscale.upscale_roof_crop(source, ring, cache_dir=cache, padding_px=4)
    assert again['cache_hit'] and len(calls) == 1


def test_model_and_source_changes_do_not_reuse_stale_derivative(crop_runtime):
    source, cache, ring, calls, runtime = crop_runtime
    first = upscale.upscale_roof_crop(source, ring, cache_dir=cache)
    runtime['weights_sha256'] = 'different-test-weights'
    second = upscale.upscale_roof_crop(source, ring, cache_dir=cache)
    Image.new('RGB', (80, 60), (130, 92, 64)).save(source)
    third = upscale.upscale_roof_crop(source, ring, cache_dir=cache)
    assert len({result['cache_key'] for result in (first, second, third)}) == 3
    assert len(calls) == 3


@pytest.mark.parametrize('corruption', ['missing-hashes', 'wrong-size', 'changed-pixels'])
def test_cache_never_accepts_unverified_output(crop_runtime, corruption):
    source, cache, ring, calls, _ = crop_runtime
    result = upscale.upscale_roof_crop(source, ring, cache_dir=cache)
    path = Path(result['manifest_path'])
    manifest = json.loads(path.read_text())
    if corruption == 'missing-hashes':
        manifest['artifacts'] = {}
    elif corruption == 'wrong-size':
        Image.new('RGB', (4, 4)).save(result['output_path'])
        manifest['output_size'] = [4, 4]
        manifest['artifacts']['upscaled.png'] = upscale.file_sha256(result['output_path'])
    else:
        Image.new('RGB', tuple(result['output_size'])).save(result['output_path'])
    path.write_text(json.dumps(manifest))
    with pytest.raises(upscale.UpscaleError, match='could not be verified'):
        upscale.upscale_roof_crop(source, ring, cache_dir=cache)
    assert len(calls) == 1, 'Cache corruption must not silently generate a replacement'


def test_timeout_publishes_no_derivative_or_fallback(crop_runtime, monkeypatch):
    source, cache, ring, _, _ = crop_runtime

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(upscale.subprocess, 'run', timeout)
    with pytest.raises(upscale.UpscaleError, match='no derivative was published'):
        upscale.upscale_roof_crop(source, ring, cache_dir=cache)
    assert not list(cache.rglob('upscaled.png'))
    assert not list(cache.rglob('manifest.json'))


@pytest.mark.parametrize('ring,bound', [([[0, 0], [79, 0], [79, 59]], 32),
                                       ([[-1, 0], [20, 0], [20, 20]], 768),
                                       ([[0, 0], [float('nan'), 0], [20, 20]], 768)])
def test_oversize_or_outside_crops_refuse_without_resizing(ring, bound):
    with pytest.raises(upscale.UpscaleError):
        upscale.crop_geometry((80, 60), ring, max_crop_px=bound)


def test_incompatible_binary_is_rejected_before_inference(monkeypatch):
    monkeypatch.setattr(upscale.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(upscale.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(upscale.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='Mach-O 64-bit executable x86_64'))
    with pytest.raises(upscale.UpscaleError, match='not native-compatible'):
        upscale._architecture('/not/executed')


def test_unavailable_runtime_has_no_interpolation_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv('ADDR_REALESRGAN_BIN', raising=False)
    monkeypatch.setattr(upscale, 'DEFAULT_INSTALL', tmp_path / 'absent')
    with pytest.raises(upscale.UpscaleError, match='no interpolation fallback'):
        upscale._runtime()
