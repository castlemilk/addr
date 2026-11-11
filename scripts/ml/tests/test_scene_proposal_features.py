import copy
import hashlib

import numpy as np
import pytest
from PIL import Image
from scripts.ml.scene_proposal_features import CONTEXT_FEATURES, case_context, image_context


def outline(ring=None, **kwargs):
    return {'id': 'p', 'ring': ring or [[8, 8], [24, 8], [24, 24], [8, 24]], **kwargs}


def test_context_detects_clipping_and_separates_texture_from_mean_colour():
    plain = Image.new('RGB', (32, 32), (80, 100, 80))
    obj = outline()
    values = image_context([obj], plain)['p']
    np.testing.assert_allclose(values, np.zeros(len(CONTEXT_FEATURES)), atol=1e-12)
    texture = np.asarray(plain).copy()
    texture[8:24:2, 8:24] = [30, 50, 30]
    texture[9:24:2, 8:24] = [130, 150, 130]
    textured = image_context([obj], Image.fromarray(texture))['p']
    assert textured[2] > .3
    clipped = image_context([outline([[0, 8], [16, 8], [16, 24], [0, 24]])], plain)['p']
    assert clipped[0] == 1 and .25 <= clipped[1] < .4
    assert image_context([], plain) == {}


def test_neighbourhood_contrast_and_holes_use_only_their_intended_pixels():
    array = np.full((40, 40, 3), 180, dtype=np.uint8)
    array[8:32, 8:32] = [40, 80, 40]
    obj = outline([[8, 8], [32, 8], [32, 32], [8, 32]], holes=[[[14, 14], [26, 14], [26, 26], [14, 26]]])
    baseline = image_context([obj], Image.fromarray(array))['p']
    assert baseline[5] < -.4 and baseline[6] > .1
    # An unrelated courtyard/roof visible through the mask's hole cannot alter
    # the object's colour, texture or the exterior neighbourhood contrast.
    array[14:26, 14:26] = [255, 0, 255]
    np.testing.assert_allclose(image_context([obj], Image.fromarray(array))['p'], baseline, atol=1e-12)
    obj.update(status='confirmed', geometry_reviewed=True, notes='trusted label', model={'height_m': 90})
    np.testing.assert_allclose(image_context([obj], Image.fromarray(array))['p'], baseline, atol=1e-12)


def test_context_is_hash_bound_and_never_reads_test_images(tmp_path):
    path = tmp_path/'image.png'
    Image.new('RGB', (32, 32), 'green').save(path)
    case = {'split': 'train', 'image_path': str(path), 'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'predictions': [outline()]}
    before = copy.deepcopy(case)
    assert len(case_context(case)['p']) == len(CONTEXT_FEATURES)
    assert case == before
    with pytest.raises(ValueError, match='held-out test'):
        case_context({**case, 'split': 'test', 'image_path': 'must-not-open'})
    Image.new('RGB', (32, 32), 'red').save(path)
    with pytest.raises(ValueError, match='Image bytes changed'):
        case_context(case)


def test_invalid_or_invisible_context_is_not_silently_replaced_with_zeros():
    image = Image.new('RGB', (32, 32))
    for obj in [outline([[-1, 4], [4, 4], [4, 8], [-1, 8]]), outline([[1, 1], [1.1, 1], [1.1, 1.1], [1, 1.1]])]:
        with pytest.raises(ValueError):
            image_context([obj], image)
    with pytest.raises(ValueError, match='Duplicate'):
        image_context([outline(), outline()], image)
