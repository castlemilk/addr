"""Tree crown shape sidecar v1 (card tree-group-split-2026-10-03 item 2(a), answered 2026-10-07): synthetic outlines only,
no scene file is read. 0.1 m/px, a 400 x 400 px frame."""
import json

import pytest
from scripts.ml.experiments import tree_crown_split as t
from scripts.ml.scene_extract import APP_RING_RULES, app_ring_issues
from scripts.ml.scene_instances import partition_outlines
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

M_PER_PX = 0.1
SIZE = 400


def outline(geometry):
    """A shapely polygon as the scenes store it: open rings, 2 dp."""
    g = geometry.simplify(0.3)
    ring = [[round(x, 2), round(y, 2)] for x, y in g.exterior.coords[:-1]]
    holes = [[[round(x, 2), round(y, 2)] for x, y in h.coords[:-1]] for h in g.interiors]
    return ring, holes


def two_crowns(dx=0.):
    """Two 3 m crowns joined by a 1 m neck (saddle 0.5-0.6 m, ratio ~0.2: a cut)."""
    return unary_union([Point(150+dx, 200).buffer(30, 64), Point(225+dx, 200).buffer(30, 64), box(170+dx, 195, 205+dx, 205)])


def three_lobes():
    """Two 3.2 m crowns plus a 1.55 m lobe (~7.7 m2, under the 8 m2 floor) hanging off the left crown."""
    return unary_union([Point(130, 200).buffer(32, 64), Point(210, 200).buffer(32, 64), box(150, 195, 190, 205),
                        Point(130, 248).buffer(15.5, 64), box(127, 225, 133, 236)])


def run(geometry, **kwargs):
    ring, holes = outline(geometry)
    return ring, holes, t.crown_partition(ring, holes, M_PER_PX, SIZE, SIZE, **kwargs)


def test_design_is_the_card_item_2a():
    d = t.SIDECAR_V1
    assert (d['min_crown_radius_m'], d['saddle_merge_ratio'], d['min_part_m2']) == (1.5, 0.7, 8.0)
    assert d['fill_holes'] is True and d['max_children'] == 8
    assert d['population'] == {'prefixes': ['win-', 'lot-'], 'layer': 'review', 'kinds': ['tree'], 'min_area_m2': 50.0, 'min_peaks': 2}
    assert d['controls']['n'] == 10


def test_necked_pair_is_an_exact_partition_with_per_child_confidence():
    ring, holes, rec = run(two_crowns())
    assert rec['guard'] is None and rec['refusal'] is None and rec['cut'] is True
    assert rec['peaks_detected'] == 2 and rec['n_parts'] == 2 and len(rec['cuts']) == 1
    children = rec['children']
    source = Polygon(ring, holes)
    pieces = [Polygon(c['outline_pixels']['ring'], c['outline_pixels']['holes']) for c in children]
    union = unary_union(pieces)
    assert union.symmetric_difference(source).area <= 1e-7
    assert sum(p.area for p in pieces) - union.area <= 1e-7
    # the split API's own partition rule accepts the children as given
    check = partition_outlines({'ring': ring, 'holes': holes}, [c['outline_pixels'] for c in children])
    assert check['missing_area_px'] <= 1e-7 and check['overlap_area_px'] <= 1e-7 and check['outside_area_px'] <= 1e-7
    for c in children:
        assert app_ring_issues(c['outline_pixels']['ring'], c['outline_pixels']['holes'], SIZE, SIZE, rules=APP_RING_RULES) == []
        assert c['role'] == 'crown' and c['kind'] == 'tree'
        assert c['saddle_m'] is not None and 0 < c['saddle_m'] < 1.0
        assert c['depth_ratio'] is not None and c['depth_ratio'] >= 0.3
        assert isinstance(c['uncertainty'], str) and 'saddle' in c['uncertainty'] and 'depth ratio' in c['uncertainty']
        assert set(c) >= {'key', 'label', 'outline_pixels'}
    assert rec['partition_check']['missing_area_px'] <= 1e-7


def test_single_crown_is_not_cut():
    _, _, rec = run(Point(200, 200).buffer(40, 64))
    assert rec['peaks_detected'] == 1 and rec['cut'] is False and rec['children'] == [] and rec['cuts'] == []
    assert 'one crown peak' in rec['no_cut_reason']


def test_confirmed_object_is_never_split():
    ring, holes = outline(two_crowns())
    rec = t.crown_partition(ring, holes, M_PER_PX, SIZE, SIZE, status='confirmed')
    assert rec['guard'] == 'confirmed' and rec['cut'] is False and rec['children'] == [] and rec['peaks_detected'] is None
    # a control is the same confirmed object run as a null reading: measured, flagged, never a proposal
    control = t.crown_partition(ring, holes, M_PER_PX, SIZE, SIZE, status='confirmed', control=True)
    assert control['control'] is True and control['guard'] is None


def test_frame_cut_object_is_skipped():
    cut = two_crowns(dx=-120).intersection(box(0, 0, SIZE, SIZE))
    ring, holes = outline(cut)
    assert min(x for x, _ in ring) == 0
    rec = t.crown_partition(ring, holes, M_PER_PX, SIZE, SIZE)
    assert rec['guard'] == 'frame_cut' and rec['cut'] is False and rec['children'] == []
    # one pixel inside the frame still counts as cut (mask contours end on the last pixel row / column)
    near = two_crowns(dx=-119).intersection(box(1, 0, SIZE, SIZE))
    assert t.crown_partition(*outline(near), M_PER_PX, SIZE, SIZE)['guard'] == 'frame_cut'


def test_small_part_is_merged_into_the_longest_edge_neighbour_not_dropped():
    ring, holes, rec = run(three_lobes())
    assert rec['peaks_detected'] == 3 and rec['n_parts'] == 2
    assert len(rec['floor_merges']) == 1 and rec['floor_merges'][0]['area_m2'] < 8.0
    pieces = [Polygon(c['outline_pixels']['ring'], c['outline_pixels']['holes']) for c in rec['children']]
    source = Polygon(ring, holes)
    assert abs(sum(p.area for p in pieces) - source.area) <= 1e-7          # nothing dropped
    assert all(c['area_m2'] >= 8.0 for c in rec['children'])
    # the lobe went to the crown it hangs off (the longest shared edge), not to the other one
    lobe, left_crown = Point(130, 248), Point(130, 200)
    owner = [p for p in pieces if p.contains(lobe)]
    assert len(owner) == 1 and owner[0].contains(left_crown)


def test_a_lone_small_part_leaves_the_object_whole():
    lobe = unary_union([Point(150, 200).buffer(45, 64), Point(150+45+15.5+6, 200).buffer(15.5, 64), box(190, 197, 215, 203)])
    _, _, rec = run(lobe)
    assert rec['peaks_detected'] == 2 and rec['cut'] is False and len(rec['floor_merges']) == 1
    assert 'merged into the longest-edge neighbour' in rec['no_cut_reason']


def test_holes_are_filled_before_the_distance_transform_and_kept_in_the_children():
    slotted = Point(200, 200).buffer(50, 64).difference(unary_union([box(165, 196, 197, 204), box(203, 196, 235, 204)]))
    _, holes, rec = run(slotted)
    assert len(holes) == 2 and rec['holes_filled'] == 2
    assert rec['cut'] is False and rec['peaks_detected'] == 1               # filled: one crown
    unfilled = t.crown_partition(*outline(slotted), M_PER_PX, SIZE, SIZE, design={**t.SIDECAR_V1, 'fill_holes': False})
    assert unfilled['cut'] is True                                           # the neck through the hole cluster the fill removes
    ring, holes, rec = run(two_crowns().difference(Point(150, 200).buffer(6, 16)))
    assert rec['cut'] is True and sum(len(c['outline_pixels']['holes']) for c in rec['children']) == 1
    assert partition_outlines({'ring': ring, 'holes': holes}, [c['outline_pixels'] for c in rec['children']])


def test_deterministic():
    ring, holes = outline(three_lobes())
    a = t.crown_partition(ring, holes, M_PER_PX, SIZE, SIZE)
    b = t.crown_partition(ring, holes, M_PER_PX, SIZE, SIZE)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert t.count_peaks(ring, holes, M_PER_PX) == t.count_peaks(ring, holes, M_PER_PX)
    assert t.count_peaks(ring, holes, M_PER_PX)[0] == a['peaks_detected']


@pytest.mark.parametrize('status', [None, 'candidate', 'needs_adjustment'])
def test_unconfirmed_statuses_are_run(status):
    _, _, rec = run(two_crowns(), status=status)
    assert rec['guard'] is None and rec['cut'] is True
