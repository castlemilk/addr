import copy
import hashlib

import numpy as np
import pytest
from PIL import Image
from scripts.ml.scene_context import context_image
from scripts.ml.scene_region import capture_region
from scripts.ml.tiles import lonlat_to_tile_xy


class GridFetcher:
    def _child(self, z, x, y):
        yy, xx = np.mgrid[:256, :256]
        return Image.fromarray(np.stack([(xx+x*256) % 193, (yy+y*256) % 191, xx*0+100], axis=2).astype('uint8'))


def test_surrounding_tiles_are_registered_and_original_capture_is_immutable(tmp_path):
    scene, image, source = capture_region([145.3121, -37.7908, 145.313, -37.7900], root=tmp_path, fetcher=GridFetcher())
    original_scene = copy.deepcopy(scene)
    context = context_image(scene, source, root=tmp_path, fetcher=GridFetcher())
    assert context.size == (1024, 1024)
    assert context.crop((256, 256, 768, 768)).tobytes() == image.tobytes()
    assert scene == original_scene and hashlib.sha256(source).hexdigest() == scene['image_sha256']
    # Verify outside pixels against provider coordinates, not the pasted source.
    west, south, east, north = scene['region']['bounds']
    zoom = scene['region']['imagery_zoom']
    a = np.asarray(lonlat_to_tile_xy(west, north, zoom))*256
    b = np.asarray(lonlat_to_tile_xy(east, south, zoom))*256
    for px, py in [(40, 40), (810, 400), (100, 800)]:
        native = a+(np.array([px, py])-256+.5)*(b-a)/512-.5
        expected = native % [193, 191]
        # Avoid wrap discontinuities; bicubic sampling rounds by <2 pixels.
        assert np.max(np.abs(np.asarray(context.getpixel((px, py)))[:2]-expected)) < 2
    assert not (tmp_path/'data/scenes').exists(), 'Viewing context cannot create model or review snapshots'


def test_unavailable_context_and_unregistered_images_fail_without_replacing_source(tmp_path):
    scene, _, source = capture_region([145.3121, -37.7908, 145.313, -37.7900], root=tmp_path, fetcher=GridFetcher())
    class Missing:
        def _child(self, *args):
            return None
    with pytest.raises(ValueError, match='unavailable'):
        context_image(scene, source, root=tmp_path, fetcher=Missing())
    with pytest.raises(ValueError, match='changed'):
        context_image(scene, b'wrong image', root=tmp_path, fetcher=GridFetcher())
    with pytest.raises(ValueError, match='registered'):
        context_image({**scene, 'region': None}, source, root=tmp_path, fetcher=GridFetcher())
