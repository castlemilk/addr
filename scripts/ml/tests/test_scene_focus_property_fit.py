"""property-fit-v1: windows planned from 2026-10-08 frame the PROPERTY (WINDOW.json amendment span_rule.property_fit).

Reviewer, 8 Oct 2026 ~09:15 AEDT: "i just want to avoid cutting frames like it does, when its clearly not the entire permiter of the
property etc"; ~18:00 AEDT "yes to defaults" on data/reviews/eyes/experiments/review-2026-10-08/DECISION-CARD.md: default 4a (windows
made from now on use the property-fit rule, up to 120 m) and 4b (outlines nobody has reviewed do not size the frame).

PILOTS holds census v2's R7 inputs for the three pilot windows (and win-69468b2b783cd964, the card's 4b example) as bounding boxes in
each window's local metres (x east, y north), extracted read-only by .run/session-2026-10-08-defaults/frame/extract_pilot_inputs.py
with frame_context_census.scan_scenes / analyse_window (the census v2 run's code, sha 1e726a60, and its extra parcels folder); the
re-run gave census-r7fix/census.json's R7 frames for all four. A frame depends on the bounds alone (property_frame reads the property's
and the keep set's bounding boxes), so boxes are the same inputs. census_objects are census v2's property objects (a cut object's
whole copy or lower bound, a review-layer object, another scene's cluster); reviewed_outlines are the reviewer-marked outlines on the
property, each at its OWN extent, which is what the planner counts for a new window (no window review layer exists yet).

Synthetic planner tests use registered frames about one point and injected parcels; nothing reads or writes the repository's
data/scenes/ (the WINDOW.json test reads the live file and its pre-amendment byte copy when they exist).
"""
import hashlib
import json
import math
import random
import shutil
from pathlib import Path

import pytest
from scripts.ml import scene_focus as F
from scripts.ml import scene_lot as L
from scripts.ml import scene_lot_tags as T
from scripts.ml import scene_recapture as R
from scripts.ml.experiments import frame_context_census as fc
from scripts.ml.scene_addresses import image_projection
from scripts.ml.scene_region import region_frame
from shapely.geometry import box

REPO = Path(__file__).resolve().parents[3]
LON, LAT = 145.3125, -37.7905
METRES = L._local_frame(LON, LAT)
UNPROJECT = T.inverse_projection(METRES)
PROJECT = image_projection(METRES)
DESIGN = {'version': 'recapture-design-v1', 'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6, 'match_iou': .5, 'close_iou': .9,
          'subject_recapture': {'min_overlap_px': 128, 'applies_to': "frame_transform.role == 'frame_cut_target' (scene_focus windows)"}}
ALLOWANCE = F.PROPERTY_FIT['placement_allowance_m']


# --- census v2 R7 inputs of the pilot windows ---------------------------------------------------------------------------

PILOTS = {
    'win-3dd37155248c04ef': {
        'h0': 22.630745152451734, 'subject': (-6.3147, -5.6838, 6.3139, 5.6848), 'lot': (-9.6232, -8.6588, 8.4712, 4.4524), 'development': (-28.4766, -10.568, 23.537, 47.686),
        'census': {'span_m': 68.25, 'cx': -3.17, 'cy': 18.56, 'status': 'recentred'},
        # census v2 property objects: (id, source C cut row / R review layer / O other scene, reviewer-marked, bounds)
        'census_objects': [
            ('5ac0d3cb', 'C', True, (-29.8804, -5.9434, -19.2621, 4.9108)), ('ddbe1722', 'C', True, (-22.6307, 7.6909, -19.9787, 11.9342)),
            ('a6b959f6', 'C', True, (-22.6307, -0.2652, -21.9235, 4.7737)), ('1bac44c9', 'C', True, (-15.8711, 10.8901, -2.7281, 25.7155)),
            ('55ca4c5c', 'C', True, (10.7898, 12.5322, 21.797, 27.9149)), ('a32331b5', 'C', True, (4.079, 11.9837, 11.3161, 24.3706)),
            ('57fcc91c', 'R', True, (5.8345, 5.3925, 12.6414, 14.9398)), ('fb1fb231', 'R', True, (-0.3536, 5.1273, 4.3317, 10.5198)),
            ('d9e39af8', 'R', True, (-15.1166, -6.1881, -10.785, -1.5912)), ('7578eb53', 'R', True, (-3.1824, 17.8571, -0.9724, 20.5091)),
            ('3c03015e', 'R', True, (-4.4201, 15.0282, -2.21, 18.5643)), ('500853a0', 'R', True, (6.8953, -5.5693, 8.7517, -1.6796)),
            ('852dcd76', 'R', True, (-13.0834, 0.9724, -10.9618, 3.8013)), ('44e3f186', 'R', True, (-6.0033, -5.335, 6.3074, 4.9735)),
            ('1b041aef', 'R', True, (-22.1233, -3.277, -11.6257, 9.0629)), ('ef80b81f', 'R', True, (8.9524, -7.0566, 19.3263, 3.4132)),
            ('361774a3', 'R', True, (-13.0797, -5.222, -6.0413, 1.2002)), ('d2311586', 'R', True, (-19.9672, 10.8672, -13.2576, 16.4524)),
            ('99a2e8df', 'R', True, (12.5733, 2.4752, 18.3848, 6.3773)), ('ccb9dcb2', 'R', True, (15.5578, 4.8046, 20.5507, 8.8684)),
            ('ef934494', 'R', True, (12.6379, 9.748, 16.6186, 13.1718)), ('476dd776', 'R', True, (-6.0997, 11.1386, -0.7956, 18.9179)),
            ('9a9b542c', 'R', True, (-16.7963, 16.0006, -13.5254, 21.0395)), ('411d6d24', 'R', True, (-14.4094, 5.3041, -9.8125, 10.2546)),
            ('6aebcb4e', 'R', True, (-4.5969, 5.0389, -0.1768, 7.5141)), ('534ece30', 'R', True, (3.7129, -2.1216, 10.343, 6.3649)),
            ('fe25d234', 'R', True, (16.5311, 8.2213, 20.8627, 11.4038)), ('9b3a3258', 'R', False, (4.7737, -7.3373, 6.9837, -1.5028)),
            ('ef4ba362', 'O', False, (-9.3869, 23.7665, 3.1261, 37.6976)), ('d3936605', 'O', False, (-0.488, 23.0639, 7.6527, 42.5304)),
            ('9ac03d75', 'O', False, (8.5325, 28.0589, 20.5712, 33.9044)), ('3ba085d2', 'O', False, (-3.585, 40.6722, 4.7327, 47.043)),
            ('a3de3737', 'O', False, (-13.4068, 24.1256, -6.7704, 28.9037)), ('69f86240', 'O', False, (-7.3898, 37.4867, -2.9656, 42.8842)),
            ('fd0846d7', 'O', False, (-11.1947, 29.0807, -6.2395, 37.4867)), ('42f60acf', 'O', False, (0.8779, 25.9711, 2.8959, 28.7546)),
            ('86aed09e', 'O', False, (10.8985, 23.3964, 11.316, 23.6748)),
        ],
        # reviewer-marked outlines on the property, each at its own extent: (scene id, object id, kind, bounds)
        'reviewed_outlines': [
            ('win-3dd37155', '57fcc91c', 'tree', (5.8345, 5.3925, 12.6414, 14.9398)), ('win-3dd37155', 'fb1fb231', 'tree', (-0.3536, 5.1273, 4.3317, 10.5198)),
            ('win-3dd37155', '5ac0d3cb', 'tree', (-22.6307, -5.3041, -18.9179, 0.0884)), ('win-3dd37155', 'd9e39af8', 'tree', (-15.1166, -6.1881, -10.785, -1.5912)),
            ('win-3dd37155', '7578eb53', 'tree', (-3.1824, 17.8571, -0.9724, 20.5091)), ('win-3dd37155', 'ddbe1722', 'tree', (-22.6307, 7.6909, -19.9787, 11.9342)),
            ('win-3dd37155', '3c03015e', 'tree', (-4.4201, 15.0282, -2.21, 18.5643)), ('win-3dd37155', '500853a0', 'tree', (6.8953, -5.5693, 8.7517, -1.6796)),
            ('win-3dd37155', '852dcd76', 'tree', (-13.0834, 0.9724, -10.9618, 3.8013)), ('win-3dd37155', 'a6b959f6', 'tree', (-22.6307, -0.2652, -21.9235, 4.7737)),
            ('win-3dd37155', '44e3f186', 'building', (-6.0033, -5.335, 6.3074, 4.9735)), ('win-3dd37155', '1bac44c9', 'building', (-15.8344, 11.1571, -2.5548, 22.6307)),
            ('win-3dd37155', '55ca4c5c', 'building', (10.8486, 12.4876, 21.333, 22.6307)), ('win-3dd37155', '1b041aef', 'building', (-22.1233, -3.277, -11.6257, 9.0629)),
            ('win-3dd37155', 'ef80b81f', 'building', (8.9524, -7.0566, 19.3263, 3.4132)), ('win-3dd37155', '361774a3', 'building', (-13.0797, -5.222, -6.0413, 1.2002)),
            ('win-3dd37155', 'd2311586', 'building', (-19.9672, 10.8672, -13.2576, 16.4524)), ('win-3dd37155', '99a2e8df', 'building', (12.5733, 2.4752, 18.3848, 6.3773)),
            ('win-3dd37155', 'ccb9dcb2', 'building', (15.5578, 4.8046, 20.5507, 8.8684)), ('win-3dd37155', 'ef934494', 'building', (12.6379, 9.748, 16.6186, 13.1718)),
            ('win-3dd37155', 'a32331b5', 'vegetation', (3.8013, 11.9342, 9.8125, 22.6307)), ('win-3dd37155', '476dd776', 'vegetation', (-6.0997, 11.1386, -0.7956, 18.9179)),
            ('win-3dd37155', '9a9b542c', 'vegetation', (-16.7963, 16.0006, -13.5254, 21.0395)), ('win-3dd37155', '411d6d24', 'vegetation', (-14.4094, 5.3041, -9.8125, 10.2546)),
            ('win-3dd37155', '6aebcb4e', 'vegetation', (-4.5969, 5.0389, -0.1768, 7.5141)), ('win-3dd37155', '534ece30', 'vegetation', (3.7129, -2.1216, 10.343, 6.3649)),
            ('win-3dd37155', 'fe25d234', 'vegetation', (16.5311, 8.2213, 20.8627, 11.4038)), ('map-5bb1f928', '65eb5e45', 'building', (10.7898, 12.5322, 21.797, 27.9149)),
        ],
    },
    'win-a4b2d1f0b895439b': {
        'h0': 20.00008998812516, 'subject': (-1.6934, -2.8565, 1.6934, 2.8545), 'lot': (-24.9528, -4.3133, 7.2661, 9.8382), 'development': None,
        'census': {'span_m': 42.76, 'cx': -9.11, 'cy': 2.76, 'status': 'recentred'},
        # census v2 property objects: (id, source C cut row / R review layer / O other scene, reviewer-marked, bounds)
        'census_objects': [
            ('f62ba66b', 'C', False, (-22.1468, -0.7979, -8.7989, 9.5617)), ('78fcfd64', 'R', False, (2.8125, 2.0313, 6.6407, 4.6094)),
            ('e6ca8de7', 'R', False, (-3.2031, 2.9688, 0.0781, 6.6407)), ('0734d310', 'R', False, (0.7031, 5.0781, 3.2031, 6.9532)),
            ('3330119e', 'R', False, (-9.4774, -0.9719, -1.15, 7.2086)), ('d3eca290', 'R', False, (-1.6313, -2.6711, 5.7899, 2.9445)),
            ('e7197392', 'O', False, (-25.4897, -0.9926, -22.2374, 4.1481)),
        ],
        # reviewer-marked outlines on the property, each at its own extent: (scene id, object id, kind, bounds)
        'reviewed_outlines': [
        ],
    },
    'win-75f63a60578722a8': {
        'h0': 38.71661757204312, 'subject': (-10.1925, -13.9535, 11.2564, 14.7606), 'lot': (-14.7749, -20.2561, 14.7731, 20.2505), 'development': (-69.9695, -85.6028, 14.7731, 27.3214),
        'census': {'span_m': 120.0, 'cx': -27.6, 'cy': -29.14, 'status': 'capped'},
        # census v2 property objects: (id, source C cut row / R review layer / O other scene, reviewer-marked, bounds)
        'census_objects': [
            ('f366a44a', 'C', False, (-40.9922, -24.2128, -25.6621, -8.6515)), ('1a4d3245', 'C', False, (-43.0078, -1.2592, -25.1038, 11.0682)),
            ('dd1eb12d', 'C', False, (-66.3807, -66.4413, -29.436, -22.2599)), ('b16d1d07', 'C', False, (-21.7867, -40.1999, -13.4545, -31.7412)),
            ('975f2a91', 'C', False, (-40.1578, -3.8389, -32.7194, 2.9746)), ('defa56f5', 'C', False, (-37.6565, -58.7877, -18.8652, -34.6363)),
            ('bc764ba1', 'R', False, (-3.5541, 14.5943, 3.8565, 23.5173)), ('8daa75f0', 'R', False, (-27.2226, -5.2177, -19.812, 3.0247)),
            ('41d964f5', 'R', False, (-20.8707, 1.9661, -16.1067, 7.1837)), ('869553ba', 'R', False, (-32.2891, -5.1421, -27.5251, -0.2269)),
            ('33d9a73c', 'R', False, (-5.747, 6.1251, -2.1929, 9.7548)), ('bbe1c3c4', 'R', False, (-20.7951, 21.3244, -17.6947, 24.8785)),
            ('7cc413a4', 'R', False, (-14.7456, -8.7717, -9.9816, -1.7392)), ('d77c071f', 'R', False, (-8.8474, 0.6049, -3.8565, 5.3689)),
            ('e709fd34', 'R', False, (-10.4353, 18.6777, -4.764, 23.8954)), ('f6d058d4', 'R', False, (-28.5081, 16.636, -25.1809, 20.1145)),
            ('790a2c36', 'R', False, (-19.0558, -18.829, -15.8799, -15.653)), ('02fbb94f', 'R', False, (-30.8523, -14.8212, -23.8198, -9.5279)),
            ('a10408f2', 'R', False, (-32.5068, 5.5617, -9.5831, 25.2482)), ('c823f65f', 'R', False, (-4.7004, -0.0181, 11.0252, 14.707)),
            ('2c170c22', 'R', False, (-10.0951, -13.7361, 2.9453, 3.9427)), ('c7e41f72', 'R', False, (-22.3679, -33.9269, -10.7802, -18.4532)),
            ('6b298a8e', 'R', False, (-12.2993, 9.8697, -1.8058, 19.2948)), ('126a909f', 'R', False, (-12.9043, -18.6256, -3.3847, -9.8206)),
            ('0a376175', 'R', False, (-29.2999, 3.5972, -22.3604, 9.8304)), ('163e854b', 'R', False, (-1.8897, -1.5759, 5.5859, 4.9144)),
            ('02ee02c7', 'R', False, (-5.5958, -19.3583, 5.8982, -1.8148)), ('5f85a7a2', 'R', False, (-11.1915, -25.7859, -4.6127, -17.9216)),
            ('f7b1ff7d', 'R', False, (-34.1795, -10.2085, -30.6254, -7.335)), ('69cecc31', 'R', False, (-37.053, 10.2085, -32.7428, 13.9894)),
            ('fdd777ae', 'R', False, (-32.5915, 13.8382, -27.5251, 17.8459)), ('96f4ccff', 'R', False, (-25.1809, 17.241, -23.0636, 19.5095)),
            ('90dc5e28', 'O', False, (-66.2065, -66.9794, -49.0958, -50.4799)), ('56ddc08f', 'O', False, (-60.1146, -15.2536, -43.7554, 0.6619)),
            ('d8ff8a6d', 'O', False, (-48.4039, -80.0815, -35.4732, -62.5897)), ('a21978bb', 'O', False, (-64.3939, -78.1176, -54.1103, -64.5332)),
            ('15f539ee', 'O', False, (-38.7072, -67.9974, -28.726, -57.2014)), ('b8d6bf16', 'O', False, (-54.5931, -43.2858, -45.3907, -35.2631)),
            ('a41c5470', 'O', False, (-47.3885, -19.503, -38.7974, -10.4457)), ('9f03dd8a', 'O', False, (-64.8066, -7.4408, -48.5567, 3.0963)),
            ('80063a85', 'O', False, (-49.1915, -4.1032, -41.0158, 4.2388)), ('1ca8e220', 'O', False, (-54.0658, -66.9491, -46.0675, -55.269)),
            ('1e45deec', 'O', False, (-26.2816, -50.6828, -17.93, -41.5163)), ('b9b3b55d', 'O', False, (-37.7289, -57.0946, -34.1334, -46.6739)),
            ('3f6b0a94', 'O', False, (-48.6066, -69.1074, -43.2744, -60.8551)), ('67092a2e', 'O', False, (-53.5155, -78.8845, -48.2269, -72.6165)),
            ('31224c36', 'O', False, (-43.3172, 0.7567, -38.9398, 6.9164)), ('bf8c532a', 'O', False, (-45.4739, -54.5172, -41.2156, -49.5289)),
            ('e346d024', 'O', False, (-39.2117, -52.222, -34.7682, -46.3819)), ('3a3f4b8c', 'O', False, (-59.3439, -55.004, -55.5722, -50.259)),
            ('c8615bf2', 'O', False, (-45.9743, -32.2662, -42.3506, -27.7611)), ('ca0a3bae', 'O', False, (-38.9578, -44.0967, -35.6569, -39.6531)),
            ('2d3d1ac7', 'O', False, (-40.9711, -18.9856, -38.9398, -13.9698)), ('bf41bc22', 'O', False, (-34.0511, -58.6754, -29.3537, -56.7888)),
            ('9b5ee8c6', 'O', False, (-35.276, -46.7628, -34.1334, -44.3506)),
        ],
        # reviewer-marked outlines on the property, each at its own extent: (scene id, object id, kind, bounds)
        'reviewed_outlines': [
        ],
    },
    'win-69468b2b783cd964': {
        'h0': 37.772448874029166, 'subject': (-13.8886, -10.8426, 13.8854, 10.8083), 'lot': (-12.8398, -6.5246, 14.1869, 12.9148), 'development': None,
        'census': {'span_m': 75.54, 'cx': 0.71, 'cy': 12.65, 'status': 'recentred'},
        # census v2 property objects: (id, source C cut row / R review layer / O other scene, reviewer-marked, bounds)
        'census_objects': [
            ('5e9d6927', 'R', False, (-13.0241, -10.3882, 15.3015, 36.1347)), ('92b21ff3', 'R', True, (-13.5056, -10.8426, 11.9956, -3.322)),
        ],
        # reviewer-marked outlines on the property, each at its own extent: (scene id, object id, kind, bounds)
        'reviewed_outlines': [
            ('win-69468b2b', '92b21ff3', 'building', (-13.5056, -10.8426, 11.9956, -3.322)),
        ],
    },
}


def _box(bounds):
    return None if bounds is None else box(*bounds)


def _fit(pilot, objects, **kwargs):
    return F.property_fit(pilot['h0'], _box(pilot['subject']), _box(pilot['lot']), _box(pilot['development']), objects, **kwargs)


def census_objects(pilot):
    return [{'id': oid, 'reviewed': marked, 'geo': box(*bounds), 'source': source} for oid, source, marked, bounds in pilot['census_objects']]


def reviewed_outlines(pilot):
    return [{'id': f'{sid}/{oid}', 'reviewed': True, 'kind': kind, 'geo': box(*bounds)} for sid, oid, kind, bounds in pilot['reviewed_outlines']]


def frame(fit):
    return round(2*fit['half'], 2), round(fit['cx'], 2), round(fit['cy'], 2)


def test_property_frame_is_census_v2_property_frame():
    rng = random.Random(8)
    for _ in range(300):
        x0, y0 = rng.uniform(-80, 40), rng.uniform(-80, 40)
        prop = box(x0, y0, x0+rng.uniform(1, 160), y0+rng.uniform(1, 160))
        kx, ky = rng.uniform(-20, 10), rng.uniform(-20, 10)
        keep = box(kx, ky, kx+rng.uniform(1, 30), ky+rng.uniform(1, 30))
        h0 = rng.uniform(15, 60)
        ours = F.property_frame(prop, h0, keep, margin=fc.MARGIN_M)
        theirs = fc.property_frame(prop, h0, keep)
        for key in ('half', 'cx', 'cy', 'recentred', 'capped', 'property_fits'):
            assert ours[key] == pytest.approx(theirs[key]) if isinstance(ours[key], float) else ours[key] == theirs[key], key
    assert F.open_area(box(0, 0, 10, 10), box(-5, -5, 20, 20)) == fc.open_area(box(0, 0, 10, 10), box(-5, -5, 20, 20)) == 0.
    assert F.open_area(box(0, 0, 10, 10), box(-5, -5, 8, 20)) == pytest.approx(fc.open_area(box(0, 0, 10, 10), box(-5, -5, 8, 20)))


@pytest.mark.parametrize('wid', sorted(PILOTS))
def test_counting_every_outline_reproduces_census_v2_r7_for_the_pilot_windows(wid):
    """The rule with census v2's own object set (every outline counts, no placement allowance) gives census v2's R7 frame."""
    pilot = PILOTS[wid]
    fit = _fit(pilot, census_objects(pilot), count_unreviewed=True, allowance=0.)
    census = pilot['census']
    assert fit['status'] == census['status']
    assert 2*fit['half'] == pytest.approx(census['span_m'], abs=.006)
    assert (fit['cx'], fit['cy']) == (pytest.approx(census['cx'], abs=.006), pytest.approx(census['cy'], abs=.006))


# Census v2 against the registered default (unreviewed outlines left out), no allowance: (span m, cx m, cy m, status).
DEFAULT_FRAMES = {
    # the whole development (lot 6 included) still sets the span; the west edge moves 0.70 m east because tree 5ac0d3cb, confirmed
    # in win-3dd but cut there at x = -22.63, reached -29.88 only through an unreviewed whole copy in another scene
    'win-3dd37155248c04ef': (68.25, -2.47, 18.56, 'recentred'),
    # nothing on this property is reviewer-marked: the lot alone sets the frame, census v1's R7 frame (42.22 m at -8.84, 2.76);
    # census v2's 42.76 m came from an unreviewed tree (e7197392 in win-e2bd7a275d466c8d) 0.54 m past the lot
    'win-a4b2d1f0b895439b': (42.22, -8.84, 2.76, 'recentred'),
    # the 16-parcel development needs more than 120 m either way: identical, capped
    'win-75f63a60578722a8': (120.0, -27.6, -29.14, 'capped'),
    # the card's 4b example: the unreviewed 28 x 46 m candidate 5e9d6927 no longer moves the frame 12.65 m north
    'win-69468b2b783cd964': (75.54, 0., 0., 'unchanged'),
}


@pytest.mark.parametrize('wid', sorted(PILOTS))
def test_unreviewed_outlines_do_not_size_the_pilot_frames(wid):
    pilot = PILOTS[wid]
    span, cx, cy, status = DEFAULT_FRAMES[wid]
    fit = _fit(pilot, reviewed_outlines(pilot), allowance=0.)
    assert fit['status'] == status and frame(fit) == (pytest.approx(span, abs=.006), pytest.approx(cx, abs=.006), pytest.approx(cy, abs=.006))
    assert fit['objects_counted'] == len(pilot['reviewed_outlines']) and fit['objects_left_out_unreviewed'] == 0
    # census v2's objects with the unreviewed ones left out: the same frame where the reviewed objects' extents are their own outlines
    filtered = _fit(pilot, census_objects(pilot), allowance=0.)
    assert filtered['objects_left_out_unreviewed'] == sum(not marked for _, _, marked, _ in pilot['census_objects'])
    if wid != 'win-3dd37155248c04ef':
        assert frame(filtered) == frame(fit)
    else:   # census v2 gave confirmed 5ac0d3cb its unreviewed whole copy's extent; per outline it counts at its own extent
        assert frame(filtered) == (68.25, -3.17, 18.56)
    if filtered['if_unreviewed_counted'] is not None:
        census = pilot['census']
        alt = filtered['if_unreviewed_counted']
        assert (round(2*alt['half'], 2), round(alt['cx'], 2), round(alt['cy'], 2)) == (census['span_m'], census['cx'], census['cy'])
        assert alt['differs'] is (wid in ('win-a4b2d1f0b895439b', 'win-69468b2b783cd964'))


def test_the_registered_allowance_adds_a_placement_margin_to_frames_sized_by_the_property():
    """placement_allowance_m (0.05 m) keeps a property sized to the margin from landing 4 mm short after the 1e-7 degree rounding
    (census v2 pilot win-3dd37155248c04ef: development 4.996 m from the edge)."""
    for wid, (span, cx, cy, status) in DEFAULT_FRAMES.items():
        fit = _fit(PILOTS[wid], reviewed_outlines(PILOTS[wid]))
        grown = 2*ALLOWANCE if status == 'recentred' else 0.
        assert fit['margin_m'] == pytest.approx(5.05) and fit['status'] == status
        assert 2*fit['half'] == pytest.approx(span+grown, abs=.006)
        assert (fit['cx'], fit['cy']) == (pytest.approx(cx, abs=.006), pytest.approx(cy, abs=.006))
    capped = _fit(PILOTS['win-75f63a60578722a8'], [])
    assert capped['capped'] and not capped['property_fits'] and capped['lot_inside_margin'] and capped['subject_inside_margin']
    assert capped['development_inside_margin'] is False and capped['keep'] == 'subject_and_lot'


def test_over_the_cap_the_subject_and_lot_stay_inside_and_a_lot_wider_than_the_cap_keeps_the_subject():
    subject, lot = box(30, -5, 40, 5), box(28, -8, 45, 8)
    fit = F.property_fit(30., subject, lot, box(-150, -10, 45, 10), [])
    assert fit['status'] == 'capped' and fit['half'] == 60. and fit['keep'] == 'subject_and_lot'
    assert fit['cx']+fit['half'] == pytest.approx(45+5.05) and fit['lot_inside_margin'] and fit['subject_inside_margin']
    wide_lot = box(-100, -10, 40, 10)
    fit = F.property_fit(60., subject, wide_lot, None, [])
    assert fit['status'] == 'capped' and fit['keep'] == 'subject' and fit['subject_inside_margin'] and not fit['lot_inside_margin']
    assert fit['cx']+fit['half'] == pytest.approx(40+5.05)
    # never smaller than the base frame: a small off-centre property moves the frame at the base half
    fit = F.property_fit(40., box(-2, -2, 2, 2), box(-2, -2, 2, 40), None, [])
    assert fit['half'] == 40. and fit['cy'] == pytest.approx(19.) and fit['status'] == 'recentred'


# --- the planner -------------------------------------------------------------------------------------------------------

def registered(scene_id, cx, cy, span, *, frame_px=512):
    west, south = UNPROJECT(cx-span/2, cy-span/2)
    east, north = UNPROJECT(cx+span/2, cy+span/2)
    frame = region_frame([west, south, east, north], frame_px=frame_px)
    scene = {'schema_version': 1, **frame, 'id': scene_id, 'address_id': None, 'cohort': None, 'title': scene_id,
             'image_sha256': hashlib.sha256(scene_id.encode()).hexdigest(), 'image_width': frame_px, 'image_height': frame_px,
             'objects': [], 'groups': [], 'scans': {}}
    scene['model_signature'] = R.content_signature(scene)
    return scene


def building(scene, object_id, x0, y0, x1, y1, *, kind='building', status='candidate', **extra):
    project = image_projection(scene)
    ring = [list(project(*UNPROJECT(x, y))) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {'id': object_id, 'kind': kind, 'label': kind.title(), 'ring': ring, 'status': status, 'geometry_reviewed': False,
            'model': {'area_m2': (x1-x0)*(y1-y0), 'height_verified': False}, **extra}


def lot(pfi, number, plan, x0, y0, x1, y1):
    ring = [list(UNPROJECT(x, y)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring+[ring[0]]]},
            'properties': {'pfi': pfi, 'spi': f'{number}\\{plan}', 'lot': number, 'plan': plan, 'status': 'approved'}}


def parcels(*features, bounds=None):
    return {'type': 'FeatureCollection', 'features': list(features), 'bounds': bounds or [LON-.01, LAT-.01, LON+.01, LAT+.01]}


def write_design(root):
    path = root/'data/reviews/eyes/recapture/DESIGN.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DESIGN))
    if not (root/F.WINDOW_PATH).exists():
        F.write_window_rule(root)


def write_review(root, scene, objects):
    folder = root/'data/scenes'/scene['id']
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'review.json').write_text(json.dumps({'scene_id': scene['id'], 'revision': 2, 'objects': objects}))


@pytest.fixture
def source():
    scene = registered('qa_source', 0, 0, 36)
    scene['objects'] = [{'id': 'slice-a', 'kind': 'building', 'label': 'Building', 'status': 'candidate', 'geometry_reviewed': False,
                         'ring': [[200., 0.], [300., 0.], [300., 40.], [200., 40.]], 'model': {'area_m2': 20., 'height_verified': False}}]
    scene['model_signature'] = R.content_signature(scene)
    return scene


@pytest.fixture
def north():
    scene = registered('qa_north', 0, 30, 36)
    scene['objects'] = [building(scene, 'body-a', -6, 15, 6, 27)]
    scene['model_signature'] = R.content_signature(scene)
    return scene


LOT1_TP = lot('p1', '1', 'TP1', -12, 15, 12, 50)


def plan_one(root, scenes, **kwargs):
    write_design(root)
    result = F.plan(root, {'qa_source': ['slice-a']}, scenes=scenes, offline=kwargs.pop('offline', True), addresses=[], **kwargs)
    assert len(result['rows']) == 1
    return result, result['rows'][0]


def centre_m(row):
    return PROJECT(row['window']['centre']['lon'], row['window']['centre']['lat'])


def test_a_lot_that_fits_keeps_the_base_frame_and_an_unreviewed_outline_does_not_move_it(tmp_path, source, north):
    east = registered('qa_east', 0, 45, 80, frame_px=1024)
    # 20 x 55 m, 55 % on lot 1, reaching 25 m past it (the card's 4b example: win-69468b2b783cd964's candidate 5e9d6927)
    east['objects'] = [building(east, 'big', -10, 20, 10, 75)]
    result, row = plan_one(tmp_path, [source, north, east], parcels=parcels(LOT1_TP))
    fit = row['window']['property_fit']
    # left out: the slice and its body (unreviewed candidates; the subject itself always counts) and the big candidate
    assert fit['status'] == 'unchanged' and fit['objects_left_out_unreviewed'] == 3 and fit['objects_counted'] == 0
    assert fit['left_out_sample'] == ['qa_east/big', 'qa_north/body-a', 'qa_source/slice-a']
    assert row['window']['span_m'] == pytest.approx(35*1.4+10, abs=.1) and centre_m(row) == (pytest.approx(0, abs=.3), pytest.approx(32.5, abs=.3))
    assert fit['if_unreviewed_counted']['differs'] is True and fit['if_unreviewed_counted']['status'] == 'recentred'
    assert result['header']['property_fit']['totals']['would_differ_if_unreviewed_counted'] == 1
    assert row['dedupe'] == 'new' and 'parcels_open' not in row['flags']
    # a carried copy still needing adjustment is the carry's status, not the reviewer's: still left out
    write_review(tmp_path, east, [{**east['objects'][0], 'status': 'needs_adjustment', 'carried_from': {'scene_id': 'x', 'object_id': 'y'}}])
    _, row = plan_one(tmp_path, [source, north, east], parcels=parcels(LOT1_TP))
    assert row['window']['property_fit']['status'] == 'unchanged' and row['window']['property_fit']['objects_left_out_unreviewed'] == 3


def test_a_reviewer_marked_outline_on_the_lot_sizes_the_frame(tmp_path, source, north):
    east = registered('qa_east', 0, 45, 80, frame_px=1024)
    east['objects'] = [building(east, 'big', -10, 20, 10, 75)]
    write_review(tmp_path, east, [{**east['objects'][0], 'status': 'confirmed'}])
    result, row = plan_one(tmp_path, [source, north, east], parcels=parcels(LOT1_TP))
    fit, window = row['window']['property_fit'], row['window']
    assert fit['status'] == 'recentred' and fit['objects_counted'] == 1 and fit['objects_left_out_unreviewed'] == 2
    assert fit['property_inside'] and fit['lot_inside_margin'] and fit['subject_inside_margin']
    assert window['span_m'] == pytest.approx(60+2*5.05, abs=.05) and window['frame_px'] == 1024
    assert centre_m(row) == (pytest.approx(0, abs=.05), pytest.approx(45, abs=.05))
    assert fit['base']['span_m'] == pytest.approx(59, abs=.1) and window['centre_shift_m'] == pytest.approx(12.5, abs=.05)
    assert row['window_id'] == F.window_id('google', window['requested_bounds'], 1024)
    assert row['acceptance_predicted']['inside_with_margin'] is True
    assert result['header']['property_fit']['totals']['frame_changed'] == 1


def test_the_development_sizes_the_frame_and_an_open_development_refuses_the_window(tmp_path, source, north, monkeypatch):
    dev = [lot('p1', '1', 'PS9', -12, 15, 12, 50), lot('pcm', 'CM1', 'PS9', 12, 15, 40, 50), lot('p2', '2', 'PS9', 40, 15, 60, 50)]
    _, row = plan_one(tmp_path, [source, north], parcels=parcels(*dev))
    fit = row['window']['property_fit']
    assert fit['status'] == 'recentred' and fit['development_plans'] == ['PS9'] and fit['development_parcels'] == 3
    assert fit['development_inside_margin'] and fit['complete'] and fit['development_open_m2'] == 0.
    assert row['window']['span_m'] == pytest.approx(72+2*5.05, abs=.05) and centre_m(row)[0] == pytest.approx(24, abs=.05)
    # the frozen payload's query box stops at x = 50 m: the development grown by 1 m is not covered -> parcels_open, base frame
    east_edge, _ = UNPROJECT(50, 0)
    short = parcels(*dev, bounds=[LON-.01, LAT-.01, east_edge, LAT+.01])
    _, row = plan_one(tmp_path, [source, north], parcels=short)
    fit = row['window']['property_fit']
    assert fit['status'] == 'parcels_open' and 'parcels_open' in row['flags'] and fit['development_open_m2'] > 0
    assert 'offline' in fit['reason'] and row['window']['span_m'] == pytest.approx(59, abs=.1) and fit['parcel_reads'] == []
    with pytest.raises(ValueError, match='parcels_open'):
        F.capture_window(tmp_path, row)
    # not offline: ONE bounded read (frozen like the subject read), then the development is complete and framed
    reads = []

    def fake_fetch(bounds, path, key):
        reads.append((bounds, Path(path).parent.name, key))
        return parcels(*dev)
    monkeypatch.setattr(F, 'fetch_frozen', fake_fetch)
    result, row = plan_one(tmp_path, [source, north], parcels=short, offline=False)
    fit = row['window']['property_fit']
    assert len(reads) == 1 and reads[0][1] == 'parcels' and fit['status'] == 'recentred' and fit['parcel_reads'][0]['source'] == 'vicmap'
    west, south, east, north_ = reads[0][0]
    assert west < LON < east and east > east_edge, 'the read covers the uncovered part, padded'
    assert result['header']['property_fit']['parcel_reads'] == fit['parcel_reads']


def test_an_open_lot_counts_only_on_a_plan_that_can_carry_common_property(tmp_path, source, north):
    _, top = UNPROJECT(0, 45)
    short = [LON-.01, LAT-.01, LON+.01, top]
    _, row = plan_one(tmp_path, [source, north], parcels=parcels(lot('p1', '1', 'PS1', -12, 15, 12, 50), bounds=short))
    fit = row['window']['property_fit']
    assert fit['status'] == 'parcels_open' and fit['lot_may_join_development'] and fit['lot_open_m2'] > 0
    _, row = plan_one(tmp_path, [source, north], parcels=parcels(LOT1_TP, bounds=short))
    fit = row['window']['property_fit']
    assert fit['status'] == 'unchanged' and fit['complete'] and not fit['lot_may_join_development'] and fit['lot_open_m2'] > 0


def test_a_development_over_the_cap_keeps_subject_and_lot_inside_and_flags_property_capped(tmp_path, source, north):
    dev = [lot('p1', '1', 'PS9', -12, 15, 12, 50), lot('pcm', 'CM1', 'PS9', 12, 15, 60, 50), lot('p2', '2', 'PS9', 60, 15, 106, 50)]
    _, row = plan_one(tmp_path, [source, north], parcels=parcels(*dev))
    fit, window = row['window']['property_fit'], row['window']
    assert fit['status'] == 'capped' and 'property_capped' in row['flags'] and fit['keep'] == 'subject_and_lot'
    assert window['span_m'] == pytest.approx(120, abs=.01) and window['frame_px'] == 1024 and window['zoom'] == 20
    assert fit['lot_inside_margin'] and fit['subject_inside_margin'] and fit['development_inside_margin'] is False
    assert centre_m(row)[0] == pytest.approx(-12-5.05+60, abs=.05), 'moved east only as far as the lot allows'
    assert row['acceptance_predicted']['inside_with_margin'] is True
    # a plan wider than the cap is no development: its sibling lots stay neighbours (census v2)
    wide = [lot('p1', '1', 'PS9', -12, 15, 12, 50), lot('pcm', 'CM1', 'PS9', 12, 15, 80, 50), lot('p2', '2', 'PS9', 80, 15, 130, 50)]
    _, row = plan_one(tmp_path, [source, north], parcels=parcels(*wide))
    fit = row['window']['property_fit']
    assert fit['status'] == 'unchanged' and fit['development_plans'] == [] and fit['development_wider_than_cap'] == {'PS9': 142.0}


def test_a_subject_that_already_has_a_window_is_skipped_and_never_re_planned(tmp_path, source, north):
    _, row = plan_one(tmp_path, [source, north], parcels=parcels(LOT1_TP))
    assert row['dedupe'] == 'new' and 'existing_windows' not in row
    # the planned id exists on disk: exists_skip as before
    (tmp_path/'data/scenes'/row['window_id']).mkdir(parents=True)
    _, again = plan_one(tmp_path, [source, north], parcels=parcels(LOT1_TP))
    assert again['window_id'] == row['window_id'] and again['dedupe'] == 'exists_skip'
    shutil.rmtree(tmp_path/'data/scenes'/row['window_id'])
    # a captured window under ANOTHER id holds this subject (planned under the base rule): skipped, named, never re-planned
    old = {'id': 'win-00000000000000aa', 'window_frame': {'version': F.VERSION, 'subjects': [{'scene_id': 'qa_source', 'object_id': 'slice-a'}]}}
    result, skipped = plan_one(tmp_path, [source, north, old], parcels=parcels(LOT1_TP))
    assert skipped['dedupe'] == 'exists_skip' and skipped['existing_windows'] == ['win-00000000000000aa']
    assert result['header']['totals']['captures_after_dedupe'] == 0 and result['header']['property_fit']['totals']['exists_skip_by_subject'] == 1
    # the same window found on disk (scenes injected) counts too; a qa_ scratch window never does
    folder = tmp_path/'data/scenes/lot-31337'
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps({**old, 'id': 'lot-31337'}))
    scratch = {**old, 'id': 'qa_frame_win-00000000000000aa'}
    _, skipped = plan_one(tmp_path, [source, north, scratch], parcels=parcels(LOT1_TP))
    assert skipped['dedupe'] == 'exists_skip' and skipped['existing_windows'] == ['lot-31337']


def _old_window_json(root):
    """A WINDOW.json as registered before 2026-10-08: the body without the property-fit record, register serialisation."""
    path = root/F.WINDOW_PATH
    rule = json.loads(path.read_text())
    rule.pop('property_fit')
    path.write_text(json.dumps(rule, indent=1, sort_keys=True)+'\n')
    return rule, hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_planner_refuses_a_window_json_without_the_property_fit_record_and_accepts_the_amendment(tmp_path, source, north):
    write_design(tmp_path)
    targets = {'qa_source': ['slice-a']}
    rule, old_sha = _old_window_json(tmp_path)
    assert F.property_fit_constants(rule) is None and F.load_window_rule(tmp_path)[1] == old_sha, 'an old file still loads (capture)'
    with pytest.raises(ValueError, match='property-fit'):
        F.plan(tmp_path, targets, scenes=[source, north], offline=True, parcels=parcels(LOT1_TP), addresses=[])
    entry = F.property_fit_amendment(rule, old_sha, at='2026-10-08T08:00:00Z', reason='test')
    assert entry['field'] == 'span_rule.property_fit' and entry['constants_changed'] is False and entry['thresholds_changed'] is False
    assert entry['property_fit'] == json.loads(json.dumps(F.PROPERTY_FIT)) and entry['previous_sha256'] == old_sha
    path = tmp_path/F.WINDOW_PATH
    path.write_text(json.dumps({**rule, 'amendments': [*(rule.get('amendments') or []), entry]}, indent=1, sort_keys=True)+'\n')
    amended, new_sha = F.load_window_rule(tmp_path)
    assert new_sha != old_sha and F.property_fit_constants(amended) == entry['property_fit']
    result = F.plan(tmp_path, targets, scenes=[source, north], offline=True, parcels=parcels(LOT1_TP), addresses=[])
    row = result['rows'][0]
    assert result['header']['window_sha256'] == new_sha == row['window_sha256'] and row['window']['property_fit']['status'] == 'unchanged'
    with pytest.raises(ValueError, match='WINDOW.json'):
        F.capture_window(tmp_path, {**row, 'window_sha256': old_sha})
    with pytest.raises(ValueError, match='already registers'):
        F.property_fit_amendment(amended, new_sha, at='x', reason='again')
    drifted = json.loads(path.read_text())
    drifted['amendments'][-1]['property_fit']['edge_margin_m'] = 3.
    path.write_text(json.dumps(drifted, indent=1, sort_keys=True)+'\n')
    with pytest.raises(ValueError, match='property-fit constants'):
        F.load_window_rule(tmp_path)


LIVE_BACKUP = REPO/'.run/focus-recapture/WINDOW.before-property-fit-2026-10-08.json'


@pytest.mark.skipif(not LIVE_BACKUP.exists(), reason='the dated WINDOW.json amendment of 2026-10-08 has not been applied')
def test_the_live_window_json_carries_the_amendment_and_its_previous_bytes_refuse_a_plan(tmp_path, source, north):
    rule, sha = F.load_window_rule(REPO)
    entries = [a for a in rule['amendments'] if a.get('field') == F.PROPERTY_FIT_FIELD]
    assert len(entries) == 1 and entries[0]['at'].startswith('2026-10-08') and F.property_fit_constants(rule) == json.loads(json.dumps(F.PROPERTY_FIT))
    entry = entries[0]
    before = LIVE_BACKUP.read_bytes()
    assert entry['previous_sha256'] == hashlib.sha256(before).hexdigest() != sha
    assert 'yes to defaults' in entry['reason'] and 'when its clearly not the entire permiter of the property etc' in entry['reason']
    assert 'accuracy' not in json.dumps(entry).lower()
    target = tmp_path/F.WINDOW_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(before)
    write_design(tmp_path)
    assert F.load_window_rule(tmp_path)[1] == entry['previous_sha256']
    with pytest.raises(ValueError, match='window_rule_mismatch'):
        F.plan(tmp_path, {'qa_source': ['slice-a']}, scenes=[source, north], offline=True, parcels=parcels(LOT1_TP), addresses=[])


def test_a_counterfactual_plan_counting_unreviewed_outlines_is_never_captured(tmp_path, source, north):
    east = registered('qa_east', 0, 45, 80, frame_px=1024)
    east['objects'] = [building(east, 'big', -10, 20, 10, 75)]
    _, row = plan_one(tmp_path, [source, north, east], parcels=parcels(LOT1_TP), count_unreviewed=True)
    assert row['window']['property_fit']['status'] == 'recentred' and 'counterfactual_count_unreviewed' in row['flags']
    with pytest.raises(ValueError, match='counterfactual'):
        F.capture_window(tmp_path, row)
    assert math.isclose(row['window']['span_m'], 60+2*5.05, abs_tol=.05)


# --- subjects a captured window already holds (the skeptic's two findings on the 2026-10-08 change) ---------------------

OLD = 'win-00000000000000aa'


def _two_slices(source):
    """The source with a second frame-cut slice east of slice-a on the same lot, under the same north body: one house, one group."""
    scene = json.loads(json.dumps(source))
    scene['objects'].append({**scene['objects'][0], 'id': 'slice-b', 'ring': [[310., 0.], [400., 0.], [400., 40.], [310., 40.]],
                             'model': {'area_m2': 15., 'height_verified': False}})
    scene['model_signature'] = R.content_signature(scene)
    return scene


def _window(window_id, *object_ids):
    return {'id': window_id, 'window_frame': {'version': F.VERSION, 'subjects': [{'scene_id': 'qa_source', 'object_id': o} for o in object_ids]}}


class NoFetch:
    """A tile fetcher that refuses every tile: a refused capture never reaches the imagery."""

    def _child(self, z, x, y):
        raise AssertionError('no tile is fetched')


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))


def test_a_group_mixing_a_windowed_subject_with_a_new_target_plans_the_new_target_alone_and_lists_the_windowed_one(tmp_path, source, north):
    write_design(tmp_path)
    both, targets = _two_slices(source), {'qa_source': ['slice-a', 'slice-b']}

    def run(*extra, only=None):
        return F.plan(tmp_path, {'qa_source': only} if only else targets, scenes=[both, north, *extra], offline=True,
                      parcels=parcels(LOT1_TP), addresses=[])
    together = run()['rows']
    assert len(together) == 1 and [s['object_id'] for s in together[0]['subjects']] == ['slice-a', 'slice-b'], 'one house, one group'
    alone = run(only=['slice-b'])['rows'][0]
    # slice-a already belongs to a captured window: before 2026-10-08 the whole group was exists_skip and slice-b vanished
    result = run(_window(OLD, 'slice-a'))
    (row,) = result['rows']
    assert [s['object_id'] for s in row['subjects']] == ['slice-b'] and row['dedupe'] == 'new' and 'existing_windows' not in row
    assert row['window_id'] == alone['window_id'] and row['window'] == alone['window'], "the new target's frame is its own property fit"
    assert row['window']['property_fit']['status'] in ('unchanged', 'recentred') and 'parcels_open' not in row['flags']
    held = [{'scene_id': 'qa_source', 'object_id': 'slice-a', 'existing_windows': [OLD]}]
    assert row['group']['split_from_windowed'] == {'size': 2, 'reasons': together[0]['group']['reasons'], 'windowed': held}
    assert [s for s in result['header']['skipped'] if s['reason'] == 'subject_already_windowed'] == [
        {**held[0], 'reason': 'subject_already_windowed', 'planned_apart': ['qa_source/slice-b']}]
    totals, fit = result['header']['totals'], result['header']['property_fit']['totals']
    assert totals['captures_after_dedupe'] == 1 and totals['exists_skip'] == 0 and totals['subjects'] == 2
    assert fit['windowed_subjects_skipped'] == 1 and fit['rows_split_from_windowed'] == 1 and fit['exists_skip_by_subject'] == 0
    # every subject windowed: one exists_skip row naming the window, as before; nothing is split or listed apart
    result = run(_window(OLD, 'slice-a', 'slice-b'))
    (row,) = result['rows']
    assert row['dedupe'] == 'exists_skip' and row['existing_windows'] == [OLD] and len(row['subjects']) == 2
    assert 'split_from_windowed' not in row['group'] and not [s for s in result['header']['skipped'] if s['reason'] == 'subject_already_windowed']


def test_capture_refuses_a_row_with_any_subject_a_captured_window_holds_before_anything_is_read(tmp_path, source, north):
    write_design(tmp_path)
    both, targets = _two_slices(source), {'qa_source': ['slice-a', 'slice-b']}

    def run(*extra):
        return F.plan(tmp_path, targets, scenes=[both, north, *extra], offline=True, parcels=parcels(LOT1_TP), addresses=[])['rows'][0]
    # the planner marked the row exists_skip by its subjects (the window was injected, nothing on disk): refused by name
    skip = run(_window(OLD, 'slice-a', 'slice-b'))
    assert skip['dedupe'] == 'exists_skip' and skip['existing_windows'] == [OLD] and skip['window_id'] != OLD
    before = _tree(tmp_path)
    for scene_id in (None, 'qa_frame_rehearsal'):
        with pytest.raises(ValueError, match=f'subject_already_windowed: row.existing_windows -> {OLD}'):
            F.capture_window(tmp_path, skip, fetcher=NoFetch(), scene_id=scene_id)
    assert _tree(tmp_path) == before, 'no second, reframed window for the existing window subjects'
    # a row planned new, then a window captured since holds SOME of its subjects (on disk now): refused, the subject named
    row = run()
    assert row['dedupe'] == 'new' and 'existing_windows' not in row and len(row['subjects']) == 2
    folder = tmp_path/'data/scenes/lot-31337'
    folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps(_window('lot-31337', 'slice-b')))
    before = _tree(tmp_path)
    with pytest.raises(ValueError, match='subject_already_windowed: qa_source/slice-b -> lot-31337\\. A captured window keeps its frame'):
        F.capture_window(tmp_path, row, fetcher=NoFetch())
    assert _tree(tmp_path) == before
    assert F.captured_window_subjects(tmp_path) == {('qa_source', 'slice-b'): {'lot-31337'}}
    # a qa_ scratch window on disk never holds a subject: the guard passes (the capture stops later, on the missing source)
    shutil.rmtree(folder)
    scratch = tmp_path/'data/scenes/qa_frame_lot-31337'
    scratch.mkdir(parents=True)
    (scratch/'latest.json').write_text(json.dumps(_window('qa_frame_lot-31337', 'slice-b')))
    with pytest.raises(Exception) as later:
        F.capture_window(tmp_path, row, fetcher=NoFetch())
    assert 'subject_already_windowed' not in str(later.value) and F.captured_window_subjects(tmp_path) == {}
