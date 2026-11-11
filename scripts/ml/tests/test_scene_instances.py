import pytest
from scripts.ml.scene_instances import replace_outlines
from shapely.geometry import Polygon
from shapely.ops import unary_union

RECT = [[10, 10], [90, 10], [90, 60], [10, 60]]


def test_split_then_merge_retains_exact_coverage_and_assigns_only_contained_children():
    children = [{'id': 'left', 'ring': [[20, 20], [30, 20], [30, 30], [20, 30]]},
                {'id': 'crossing', 'ring': [[40, 20], [60, 20], [60, 30], [40, 30]]}]
    result = replace_outlines('split', [RECT], [[50, 0], [50, 80]], children)
    assert len(result['rings']) == 2
    assert result['children'] == {'left': 0, 'crossing': None}
    assert unary_union([Polygon(r) for r in result['rings']]).equals(Polygon(RECT))
    joined = replace_outlines('merge', result['rings'], children=children)
    assert Polygon(joined['rings'][0]).equals(Polygon(RECT))
    assert joined['children'] == {'left': 0, 'crossing': 0}


def test_concave_split_can_make_multiple_parts_without_filling_notches():
    u = [[0, 0], [10, 0], [10, 10], [7, 10], [7, 3], [3, 3], [3, 10], [0, 10]]
    result = replace_outlines('split', [u], [[0, 7], [10, 7]])
    assert len(result['rings']) == 3
    assert sum(Polygon(r).area for r in result['rings']) == Polygon(u).area
    assert unary_union([Polygon(r) for r in result['rings']]).equals(Polygon(u))


@pytest.mark.parametrize(('operation', 'rings', 'cut'), [
    ('split', [RECT], [[20, 20], [30, 30]]),
    ('split', [RECT], [[50, 50], [50, 50]]),
    ('split', [RECT], [[10.001, 0], [10.001, 80]]),
    ('merge', [RECT, [[100, 10], [120, 10], [120, 60], [100, 60]]], None),
    ('merge', [RECT], None),
    ('split', [[[0, 0], [10, 10], [0, 10], [10, 0]]], [[5, 0], [5, 10]]),
])
def test_invalid_replacements_never_fabricate_geometry(operation, rings, cut):
    with pytest.raises(ValueError):
        replace_outlines(operation, rings, cut)


def test_merging_around_a_hole_does_not_silently_fill_it():
    rings = [[[0, 0], [10, 0], [10, 2], [0, 2]], [[0, 8], [10, 8], [10, 10], [0, 10]],
             [[0, 0], [2, 0], [2, 10], [0, 10]], [[8, 0], [10, 0], [10, 10], [8, 10]]]
    result = replace_outlines('merge', rings)
    assert len(result['rings']) == len(result['holes']) == 1
    assert len(result['holes'][0]) == 1
    polygon = Polygon(result['rings'][0], result['holes'][0])
    assert polygon.area == result['area_px'] == 64
    assert polygon.equals(unary_union([Polygon(r) for r in rings]))
    assert polygon.intersection(Polygon([[2, 2], [8, 2], [8, 8], [2, 8]])).area == 0


@pytest.mark.parametrize('cut_x', [1, 5])
def test_split_then_merge_preserves_gaps_and_does_not_mount_children_in_them(cut_x):
    ring = [[0, 0], [10, 0], [10, 10], [0, 10]]
    hole = [[2, 2], [8, 2], [8, 8], [2, 8]]
    children = [{'id': 'in-gap', 'ring': [[3, 3], [4, 3], [4, 4], [3, 4]]},
                {'id': 'on-roof', 'ring': [[8.5, 3], [9.5, 3], [9.5, 4], [8.5, 4]]}]
    split = replace_outlines('split', [ring], [[cut_x, -1], [cut_x, 11]], children, [[hole]])
    parts = [Polygon(r, h) for r, h in zip(split['rings'], split['holes'], strict=True)]
    assert unary_union(parts).equals(Polygon(ring, [hole]))
    assert split['children'] == {'in-gap': None, 'on-roof': 1}
    merged = replace_outlines('merge', split['rings'], children=children, holes=split['holes'])
    assert Polygon(merged['rings'][0], merged['holes'][0]).equals(Polygon(ring, [hole]))
    assert merged['children'] == {'in-gap': None, 'on-roof': 0}


def test_trace_replaces_disconnected_fragments_with_the_reviewers_exact_outline():
    rings = [[[x, 10], [x+2, 10], [x+2, 14], [x, 14]] for x in range(10, 58, 4)]
    outline = {'ring': [[9, 9], [60, 9], [60, 16], [9, 16]],
               'holes': [[[11, 11], [12, 11], [12, 12], [11, 12]]]}
    children = [{'id': 'inside', 'ring': [[20, 10], [22, 10], [22, 12], [20, 12]]},
                {'id': 'outside', 'ring': [[20, 14], [22, 14], [22, 18], [20, 18]]},
                {'id': 'gap', 'ring': [[11, 11], [12, 11], [12, 12], [11, 12]]}]
    result = replace_outlines('trace', rings, outline=outline, children=children)
    assert result['rings'] == [outline['ring']]
    assert result['holes'] == [outline['holes']]
    assert result['children'] == {'inside': 0, 'outside': None, 'gap': None}
    assert result['area_px'] == Polygon(outline['ring'], outline['holes']).area
    assert result['area_px'] > unary_union([Polygon(r) for r in rings]).area
    with pytest.raises(ValueError, match='disconnected'):
        replace_outlines('merge', rings[:2])


@pytest.mark.parametrize('outline', [None, {}, {'ring': []}, {'ring': [[0, 0], [float('inf'), 1], [1, 1]]},
                                   {'ring': [[0, 0], [10, 10], [0, 10], [10, 0]]},
                                   {'ring': [[100, 100], [110, 100], [110, 110], [100, 110]]}])
def test_invalid_or_unrelated_traces_do_not_supersede_sources(outline):
    with pytest.raises(ValueError):
        replace_outlines('trace', [RECT], outline=outline)


def test_trace_requires_overlap_with_every_source_and_has_a_bounded_fan_in():
    rings = [[[x, 10], [x+2, 10], [x+2, 14], [x, 14]] for x in range(10, 270, 4)]
    outline = {'ring': [[9, 9], [270, 9], [270, 16], [9, 16]]}
    assert len(replace_outlines('trace', rings[:64], outline=outline)['rings']) == 1
    with pytest.raises(ValueError, match='64'):
        replace_outlines('trace', rings, outline=outline)
    with pytest.raises(ValueError, match='every selected'):
        replace_outlines('trace', rings[:2], outline={'ring': rings[0]})
