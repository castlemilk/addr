import math

import pytest
from scripts.ml.scene_split import group_split, image_split_audit
from scripts.ml.tiles import WORLD_SPAN_M, tile_xy_to_lonlat


def frame(x=118441.5, y=80418.5, width=100):
    lon, lat = tile_xy_to_lonlat(x, y, 17)
    return {'centroid': {'lon': lon, 'lat': lat}, 'image_width': 512, 'image_height': 512,
            'pixel_to_meters': [[width/512, 0], [0, -width/512], [-width/2, width/2]]}


def test_whole_image_and_declared_split_agree():
    assert group_split('z17:118441:80418') == 'validation'
    assert group_split('z17:118442:80418') == 'train'
    audit = image_split_audit(frame(), 'val')
    assert audit['status'] == 'verified'
    assert audit['groups'] == [{'group': 'z17:118441:80418', 'split': 'validation'}]
    assert image_split_audit(frame(), 'train')['status'] == 'split_mismatch'


def test_frame_crossing_is_quarantined_even_with_validation_centroid():
    model = frame(x=118441.99)
    result = image_split_audit(model, 'validation')
    assert result['status'] == 'cross_split'
    assert result['splits'] == ['train', 'validation']
    assert len(result['groups']) == 2


def test_multiple_groups_in_same_split_are_allowed():
    result = image_split_audit(frame(y=80419), 'validation')
    assert result['status'] == 'verified'
    assert {g['group'] for g in result['groups']} == {'z17:118441:80418', 'z17:118441:80419'}


def test_line_touch_does_not_include_neighbouring_split_and_tiny_real_crossing_does():
    model = frame(x=118441.5)
    side = WORLD_SPAN_M*math.cos(math.radians(model['centroid']['lat']))/2**17
    model['pixel_to_meters'] = [[side/512, 0], [0, -.1], [-side/2, 25.6]]
    assert image_split_audit(model, 'validation')['status'] == 'verified'
    model['pixel_to_meters'][2][0] += .001
    assert image_split_audit(model, 'validation')['status'] == 'cross_split'


@pytest.mark.parametrize('change', [
    {'pixel_to_meters': [[0, 0], [0, 0], [0, 0]]},
    {'pixel_to_meters': [[1, 0], [0, 1]]},
    {'pixel_to_meters': [[float('nan'), 0], [0, 1], [0, 0]]},
    {'pixel_to_meters': [[1e6, 0], [0, 1e6], [0, 0]]},
    {'image_width': 0}, {'image_width': True}, {'centroid': {'lon': 180, 'lat': -37}},
])
def test_invalid_frames_fail_closed(change):
    model = frame()
    model.update(change)
    assert image_split_audit(model, 'validation')['status'] == 'invalid_frame'


def test_rotated_image_does_not_count_empty_bounding_box_corners():
    model = frame(x=118441.7, y=80418.7)
    radius = .4 * WORLD_SPAN_M*math.cos(math.radians(model['centroid']['lat']))/2**17
    model['pixel_to_meters'] = [[radius/512, -radius/512], [-radius/512, -radius/512], [0, radius]]
    groups = {g['group'] for g in image_split_audit(model, 'validation')['groups']}
    assert groups == {'z17:118441:80418', 'z17:118442:80418', 'z17:118441:80419'}
