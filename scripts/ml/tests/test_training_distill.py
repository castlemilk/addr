"""Hi-res teacher and distillation dataset: window selection, downsample/blur maths, stitching, COCO schema, provenance.

Runs in the main ML venv with no torch, no network and no JPEG2000 reader: SAM3 is replaced by a fake segmenter
that returns the connected components of bright pixels, so the whole teach -> finalize -> measure -> export path runs
on synthetic windows.
"""
import json
import math

import numpy as np
import pytest
from PIL import Image
from scipy import integrate, ndimage
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.training import common
from scripts.ml.training import distill_dataset as distill
from scripts.ml.training import hires_teacher as ht

ERS = '''DatasetHeader Begin
	CoordinateSpace Begin
		Datum		= "GDA94"
		Projection	= "MGA55"
	CoordinateSpace End
	RasterInfo Begin
		CellInfo Begin
			Xdimension	= 0.099999999999999867
			Ydimension	= 0.099999999999997813
		CellInfo End
		NrOfLines	= 85008
		NrOfCellsPerLine	= 84418
		RegistrationCoord Begin
			Eastings	= 314836.01286414493
			Northings	= 5817198.2562519331
		RegistrationCoord End
		NrOfBands	= 4
	RasterInfo End
DatasetHeader End
'''


# --- window selection ----------------------------------------------------------------------------------------------

def synthetic_overview(seed=0):
    """400 x 400 RGBA: outside imagery (x < 40), parkland block, water block, textured built-up elsewhere."""
    rng = np.random.default_rng(seed)
    grey = rng.integers(40, 230, (400, 400)).astype(np.uint8)          # strong pixel-to-pixel gradients = structure
    rgba = np.stack([grey, grey, grey, np.full((400, 400), 255, np.uint8)], axis=-1)
    rgba[:, :40, :3] = ht.NODATA_RGB                                   # the ortho's no-data fill (alpha is 1 there)
    rgba[40:160, 40:160, :3] = (60, 140, 50)                           # vegetation
    rgba[240:360, 240:360, :3] = (57, 91, 89)                          # smooth teal (docks water in the real file)
    return rgba


def test_window_selection_is_deterministic_and_follows_the_rule():
    masks = ht.classify_overview(synthetic_overview())
    cells = ht.grid_cells(masks, 40.)
    assert len(cells) == 100
    by_cell = {(c['row'], c['col']): c for c in cells}
    assert 'outside_imagery' in by_cell[(0, 0)]['reasons']
    assert 'vegetation' in by_cell[(2, 2)]['reasons']
    assert 'water' in by_cell[(7, 7)]['reasons']
    assert by_cell[(0, 5)]['eligible'] and by_cell[(9, 9)]['eligible']
    first, again = ht.select_order(cells, 20260928), ht.select_order(cells, 20260928)
    assert first == again
    assert first != ht.select_order(cells, 1)
    eligible = {(c['row'], c['col']) for c in cells if c['eligible']}
    assert {(w['row'], w['col']) for w in first} == eligible          # a permutation of exactly the eligible cells
    assert len({w['id'] for w in first}) == len(first)                  # grid cells: windows never overlap
    assert first[0]['id'] == ht.window_id(first[0]['row'], first[0]['col'])


# --- downsample and blur maths -------------------------------------------------------------------------------------

def test_area_matrix_is_exact_area_averaging():
    matrix = ht.area_matrix(2718, 1152)
    assert matrix.shape == (1152, 2718)
    assert np.allclose(matrix.sum(axis=1), 1.)                         # every output pixel is a weighted mean
    assert np.allclose(matrix.sum(axis=0), 1152 / 2718)                # every input pixel is used once in total
    blocks = np.arange(36, dtype=float).reshape(6, 6)
    assert np.allclose(ht.area_downsample(blocks, 2, 2), blocks.reshape(2, 3, 2, 3).mean(axis=(1, 3)))
    rng = np.random.default_rng(3)
    image = rng.random((2718, 2718, 3)) * 255
    down = ht.area_downsample(image, 1152, 1152)
    assert down.shape == (1152, 1152, 3)
    assert np.allclose(down.mean(axis=(0, 1)), image.mean(axis=(0, 1)), atol=1e-9)
    assert np.allclose(ht.area_downsample(np.full((2718, 2718), 77.), 1152, 1152), 77.)
    import cv2
    reference = cv2.resize(image[..., 0].astype(np.float32), (1152, 1152), interpolation=cv2.INTER_AREA)
    assert np.abs(reference - down[..., 0]).max() < .05                # the same area weights OpenCV uses


def pink_noise(size, seed):
    """1/f amplitude (1/f^2 power) noise scaled to 0..255, like natural aerial scenes."""
    rng = np.random.default_rng(seed)
    fy, fx = np.meshgrid(np.fft.fftfreq(size), np.fft.fftfreq(size), indexing='ij')
    radius = np.hypot(fx, fy)
    radius[0, 0] = 1.
    spectrum = (rng.normal(size=(size, size)) + 1j * rng.normal(size=(size, size))) / radius
    image = np.real(np.fft.ifft2(spectrum))
    image = (image - image.min()) / (image.max() - image.min())
    return image * 200 + 28


def quantise(image):
    return np.clip(np.rint(image), 0, 255).astype(np.uint8)


def test_blur_keeps_the_mean_and_sigma_matching_recovers_a_known_blur():
    image = pink_noise(256, 0)
    assert np.array_equal(ht.gaussian_blur(image, 0.), image.astype(np.float32))
    assert abs(float(ht.gaussian_blur(image, .8).mean()) - image.mean()) < .05
    ratios = [ht.gradient_energy_ratio(quantise(ht.gaussian_blur(image, s)).astype(np.float32)) for s in (0., .4, .8, 1.2)]
    assert all(a > b for a, b in zip(ratios, ratios[1:], strict=False))  # sharper -> higher ratio
    target = ht.gradient_energy_ratio(quantise(ht.gaussian_blur(pink_noise(256, 1), .8)).astype(np.float32))
    curve = [(s, ht.gradient_energy_ratio(quantise(ht.gaussian_blur(image, s)).astype(np.float32))) for s in ht.SIGMA_GRID]
    sigma, how = ht.match_sigma(curve, target)
    assert how == 'interpolated'
    assert abs(sigma - .8) < .12                                        # another scene with the same statistics
    assert ht.match_sigma(curve, curve[0][1] + 1)[0] == 0.              # already softer than the target: no blur
    student = ht.make_student(np.full((2718, 2718, 3), 90, np.uint8), .6)
    assert student.shape == (1152, 1152, 3) and student.dtype == np.uint8 and np.all(student == 90)


# --- stitching and dedupe ------------------------------------------------------------------------------------------

def tile_pieces(rectangles, origins, tile=ht.SUBTILE):
    """What a perfect detector returns per sub-tile: every rectangle clipped to the sub-tile extent."""
    pieces = []
    for r, oy in enumerate(origins):
        for c, ox in enumerate(origins):
            for x0, y0, x1, y1 in rectangles:
                cx0, cy0, cx1, cy1 = max(x0, ox), max(y0, oy), min(x1, ox + tile), min(y1, oy + tile)
                if cx1 > cx0 and cy1 > cy0:
                    pieces.append(ht.Piece(cx0, cy0, np.ones((cy1 - cy0, cx1 - cx0), bool), .9, (r, c)))
    return pieces


def canvas(pieces, size):
    out = np.zeros((size, size), np.int32)
    for p in pieces:
        out[p.y0:p.y1, p.x0:p.x1] += p.mask
    return out


def test_stitch_merges_buildings_cut_by_subtiles_and_keeps_neighbours_apart():
    size = 2718
    origins = ht.subtile_origins(size)
    assert origins == [0, 783, 1566]
    assert min(origins[k - 1] + ht.SUBTILE - origins[k] for k in (1, 2)) >= ht.MIN_OVERLAP
    rectangles = [(700, 100, 1300, 300),       # cut by the first vertical sub-tile edge
                  (100, 1000, 2600, 1100),     # longer than any sub-tile: six pieces
                  (900, 1400, 950, 1450),      # inside a four-way overlap: seen four times
                  (400, 2200, 600, 2400), (610, 2200, 800, 2400)]   # two roofs 1 m apart
    pieces = tile_pieces(rectangles, origins)
    merged, stats = ht.stitch(pieces, origins, ht.SUBTILE, size)
    kept, dropped = ht.suppress(merged)
    assert len(kept) == len(rectangles) and dropped == 0
    assert stats['groups_merged'] >= 3
    for x0, y0, x1, y1 in rectangles:
        truth = np.zeros((size, size), bool)
        truth[y0:y1, x0:x1] = True
        best = max(kept, key=lambda p: ht.iou(p, ht.piece_from_mask(truth)))
        assert ht.iou(best, ht.piece_from_mask(truth)) == 1.
    assert canvas(kept, size).max() == 1                               # overlaps are never double counted


def test_stitch_keeps_a_building_one_subtile_missed():
    size = 2718
    origins = ht.subtile_origins(size)
    pieces = [p for p in tile_pieces([(700, 100, 1300, 300)], origins) if p.tile != (0, 1)]
    merged, _ = ht.stitch(pieces, origins, ht.SUBTILE, size)
    assert len(merged) == 1 and merged[0].area == (1152 - 700) * 200   # only tile (0, 0) saw it: kept as seen


def test_suppress_follows_the_production_building_rule():
    big = ht.Piece(0, 0, np.ones((100, 100), bool), .9)
    inside = ht.Piece(10, 10, np.ones((50, 50), bool), .95)             # 100 % inside a kept roof
    near_copy = ht.Piece(0, 5, np.ones((100, 100), bool), .8)           # IoU 0.90
    neighbour = ht.Piece(80, 0, np.ones((100, 40), bool), .7)           # IoU 0.14, half outside
    kept, dropped = ht.suppress([inside, near_copy, big, neighbour])
    assert dropped == 2
    assert {(p.x0, p.y0, p.mask.shape) for p in kept} == {(0, 0, (100, 100)), (80, 0, (100, 40))}


def test_downsample_piece_and_rle_roundtrip():
    size = 2718
    rng = np.random.default_rng(5)
    mask = np.zeros((size, size), bool)
    mask[1003:1411, 577:1290] = True
    mask[1100:1200, 700:800] = False                                     # a courtyard
    mask |= rng.random((size, size)) > .9999                             # speckles
    piece = ht.piece_from_mask(mask)
    dy = dx = ht.area_matrix(size, 1152)
    small = ht.downsample_piece(piece, dy, dx)
    expected = ht.area_downsample(mask.astype(float), 1152, 1152) >= .5 - 1e-9
    full = np.zeros((1152, 1152), bool)
    full[small.y0:small.y1, small.x0:small.x1] = small.mask
    assert np.array_equal(full, expected)
    rle = ht.rle_encode(piece, size, size)
    assert sum(rle['counts']) == size * size and rle['size'] == [size, size]
    assert np.array_equal(ht.rle_decode(rle), mask)
    again = ht.rle_piece(rle, [piece.x0, piece.y0, piece.mask.shape[1], piece.mask.shape[0]])
    assert (again.x0, again.y0) == (piece.x0, piece.y0) and np.array_equal(again.mask, piece.mask)
    corner = ht.Piece(0, 0, np.ones((2, 2), bool), 1.)
    assert ht.rle_encode(corner, 4, 4)['counts'][0] == 0                 # COCO: first run counts zeros


# --- shape metrics and georeference --------------------------------------------------------------------------------

def test_corner_and_fill_metrics():
    rectangle = ht.Piece(10, 10, np.ones((80, 120), bool), 1.)
    metrics = ht.shape_metrics(rectangle, .1)
    assert (metrics['corners'], metrics['right'], metrics['fill']) == (4, 4, 1.)
    chamfered = np.ones((80, 120), bool)
    for k in range(20):                                                  # 2 m 45-degree chamfer on every corner
        chamfered[k, :20 - k] = chamfered[k, 100 + k:] = False
        chamfered[79 - k, :20 - k] = chamfered[79 - k, 100 + k:] = False
    rounded = ht.shape_metrics(ht.Piece(0, 0, chamfered, 1.), .1)
    assert rounded['corners'] == 8 and rounded['right'] == 0 and rounded['fill'] < 1.


def test_ers_georeference_and_window_size():
    geo = ht.parse_ers(ERS)
    assert geo['width'] == 84418 and geo['height'] == 85008 and geo['bands'] == 4
    assert geo['origin_e'] == pytest.approx(314836.0129, abs=1e-3) and geo['projection'] == 'MGA55'
    assert geo['gsd_x_m'] == pytest.approx(.1, abs=1e-12)
    geo['gsd_m'] = .1
    assert ht.native_window_px(.1) == 2718                                # 1152 x 0.23595 m / 0.1 m
    native, student, student_m = ht.window_affines(geo, 2, 3, 2718)
    assert native[0] == pytest.approx(geo['origin_e'] + 3 * 271.8) and native[3] == pytest.approx(geo['origin_n'] - 2 * 271.8)
    assert student_m == pytest.approx(2718 * .1 / 1152) and abs(student_m - ht.PIPELINE_PX_M) < 1e-4
    assert student[1] == student_m and student[5] == -student_m


def test_mga55_matches_an_independent_meridian_arc_and_round_trips():
    a, f = 6378137., 1 / 298.257222101
    e2 = f * (2 - f)
    phi = math.radians(-37.8)
    arc = integrate.quad(lambda p: a * (1 - e2) / (1 - e2 * math.sin(p) ** 2) ** 1.5, 0, phi)[0]
    easting, northing = ht.lonlat_to_mga55(147., -37.8)
    assert easting == pytest.approx(500000., abs=1e-6)
    assert northing == pytest.approx(10000000. + .9996 * arc, abs=1e-3)   # series vs numeric integral, 1 mm
    lon, lat = ht.mga55_to_lonlat(*ht.lonlat_to_mga55(144.9631, -37.8136))
    assert lon == pytest.approx(144.9631, abs=1e-9) and lat == pytest.approx(-37.8136, abs=1e-9)
    lon, lat = ht.mga55_to_lonlat(314836.0 + 4221., 5817198.3 - 4250.)   # middle of the City of Melbourne ortho
    assert 144.85 < lon < 145.05 and -37.9 < lat < -37.75


# --- end to end with a fake SAM3 -----------------------------------------------------------------------------------

class FakeSam3:
    """encode_image returns the pixels; proposals are the connected components of bright pixels, largest first."""

    def __init__(self):
        self.encodes = 0

    def encode_image(self, image):
        self.encodes += 1
        return np.asarray(image)

    def proposals(self, features, prompt, image_size, threshold=.4, max_proposals=128):
        assert prompt == ht.PROMPT and features.shape[1::-1] == tuple(image_size)
        labels, count = ndimage.label(features[..., 0] > 128)
        out = []
        for index in range(1, count + 1):
            mask = labels == index
            ys, xs = np.nonzero(mask)
            box = (xs.min() / image_size[0], ys.min() / image_size[1], (xs.max() + 1) / image_size[0],
                   (ys.max() + 1) / image_size[1])
            out.append(Sam3Proposal(mask=mask, score=.9 - index * 1e-3, box=box))
        out.sort(key=lambda p: -p.mask.sum())
        return out[:max_proposals]


RECTANGLES = [(700, 100, 1300, 300), (100, 1000, 2600, 1100), (900, 1400, 1000, 1500), (2000, 2000, 2300, 2400),
              (400, 2200, 600, 2400), (620, 2200, 800, 2400)]


def make_teacher_run(root, window_ids=('com2018-r001-c002',)):
    size, gsd = 2718, .1
    geo = {**ht.parse_ers(ERS), 'gsd_m': gsd, 'source': 'test'}
    crops = []
    for k, wid in enumerate(window_ids):
        native = np.full((size, size, 3), 30, np.uint8)
        for x0, y0, x1, y1 in RECTANGLES:
            native[y0 + k:y1, x0:x1] = 220
        path = ht.native_path(root, wid)
        sha = ht.save_png(path, native)
        row, col = 1, 2 + k
        native_affine, student_affine, student_m = ht.window_affines(geo, row, col, size)
        crops.append({'id': wid, 'row': row, 'col': col, 'shape': [size, size, 4], 'inside_fraction': 1.,
                      'read_seconds': 1.5, 'sha256': sha, 'native_affine': native_affine, 'student_affine': student_affine,
                      'student_px_m': student_m, 'native_file': str(path.relative_to(root))})
    common.write_json_atomic(root / ht.TEACHER / 'crops.json', {'version': ht.VERSION, 'native_window_px': size, 'gsd_m': gsd,
                                                                'windows': crops, 'skipped_partial': []})
    common.write_json_atomic(root / ht.TEACHER / 'windows.json', {'seed': 1, 'n': len(window_ids),
                                                                  'rule': dict(ht.OVERVIEW_RULE), 'rule_text': 'test',
                                                                  'counts': {}, 'geo': geo})
    common.write_json_atomic(root / ht.TEACHER / 'blur_calibration.json', {'sigma_px': .6, 'sigma_m': .14})
    common.write_json_atomic(root / ht.CACHE / 'overview-r5.json', {'jp2': None, 'geo_box': None})
    (root / ht.CACHE / 'provenance.json').write_text(json.dumps({'licence': 'CC BY 4.0'}))
    fake = FakeSam3()
    ht.teach(root, segmenter=fake)
    return fake


@pytest.fixture(scope='module')
def teacher_run(tmp_path_factory):
    root = tmp_path_factory.mktemp('distill')
    fake = make_teacher_run(root, ('com2018-r001-c002', 'com2018-r001-c003'))
    finalized = ht.finalize(root)
    premise = ht.measure(root, sheet_pairs=4)
    manifest = distill.export(root, seed=7)
    return root, fake, finalized, premise, manifest


def test_teacher_run_finds_every_roof_once_on_both_grids(teacher_run):
    root, fake, finalized, _, _ = teacher_run
    assert fake.encodes == 2 * (9 + 1)                                    # nine native sub-tiles plus the control
    record = common.read_json(ht.window_dir(root, 'com2018-r001-c002') / 'record.json')
    stats = record['stats']
    assert stats['teacher_instances'] == len(RECTANGLES) and stats['control_instances'] == len(RECTANGLES)
    assert stats['teacher_student_instances'] == len(RECTANGLES)
    for x0, y0, x1, y1 in RECTANGLES:
        truth = np.zeros((2718, 2718), bool)
        truth[y0:y1, x0:x1] = True
        best = max(ht.iou(ht.rle_piece(r['rle_native'], r['bbox_native']), ht.piece_from_mask(truth))
                   for r in record['teacher'])
        assert best == 1.
    label = np.asarray(Image.open(ht.window_dir(root, 'com2018-r001-c002') / 'teacher_student.png'))
    assert label.dtype == np.uint16 and label.shape == (1152, 1152) and label.max() == len(RECTANGLES)
    assert finalized['instances'] == {'teacher_native': 12, 'teacher_student': 12, 'control': 12}
    manifest = common.read_json(root / ht.TEACHER / 'manifest.json')
    window = manifest['windows'][0]
    assert window['sha256']['native'] == common.sha256_file(root / window['files']['native'])
    assert window['sha256']['student'] == common.sha256_file(root / ht.TEACHER / window['files']['student'])
    assert len(window['native_affine']) == 6 and window['mga55_bounds'][2] - window['mga55_bounds'][0] == pytest.approx(271.8)
    assert 144.8 < window['centre_lonlat'][0] < 145.1
    for name in ('teacher_native', 'teacher_student', 'control'):
        coco = common.read_json(root / ht.TEACHER / f'{name}_coco.json')
        assert {a['provenance'] for a in coco['annotations']} == {ht.PROVENANCE_TAG}
        for annotation in coco['annotations']:
            image = next(i for i in coco['images'] if i['id'] == annotation['image_id'])
            decoded = ht.rle_decode(annotation['segmentation'])
            assert decoded.shape == (image['height'], image['width']) and decoded.sum() == annotation['area']


def test_premise_summary_has_denominators(teacher_run):
    root, _, _, premise, _ = teacher_run
    assert premise['counts']['matched_pairs'] == 12 and premise['pairs_measured'] == 12
    native = premise['arms']['teacher_native']
    assert native['corners_total'] == 48 and native['right_corners'] == 48   # axis-aligned rectangles, exact masks
    assert premise['arms']['control']['corners_total'] > 0
    assert premise['hausdorff_m']['teacher_native_vs_control']['n'] == 12
    assert premise['hausdorff_m']['teacher_native_vs_control']['median'] < 1.
    assert (root / premise['contact_sheet']['path']).exists() and premise['contact_sheet']['pairs'] == 4
    lines = (root / ht.TEACHER / 'premise' / 'pairs.jsonl').read_text().splitlines()
    assert len(lines) == 12 and 'accuracy' in premise['note']


def test_distill_dataset_matches_the_dataset_v1_schema(teacher_run):
    root, _, _, _, manifest = teacher_run
    out = root / ht.OUT
    image_ids, annotation_ids = set(), set()
    required_image = {'id', 'file_name', 'width', 'height', 'scene_id', 'sha256', 'label_file', 'label_sha256',
                      'pixel_size_m', 'tile_group', 'split'}
    required_annotation = {'id', 'image_id', 'category_id', 'iscrowd', 'segmentation', 'holes', 'bbox', 'area',
                           'label_index', 'scene_id', 'object_id', 'provenance', 'tier', 'weight', 'boundary_reference',
                           'source', 'image_identity', 'detector_score', 'area_m2'}
    total = 0
    for split in distill.SPLITS:
        coco = common.read_json(out / f'{split}.json')
        assert coco['categories'] == [common.CATEGORY]
        for image in coco['images']:
            assert required_image <= set(image) and image['id'] not in image_ids and image['split'] == split
            image_ids.add(image['id'])
            assert common.sha256_file(out / image['file_name']) == image['sha256']
            label = np.asarray(Image.open(out / image['label_file']))
            assert label.dtype == np.uint16 and label.shape == (image['height'], image['width'])
            assert common.sha256_file(out / image['label_file']) == image['label_sha256']
            annotations = [a for a in coco['annotations'] if a['image_id'] == image['id']]
            assert len(annotations) == len(RECTANGLES) == label.max()
            for annotation in annotations:
                polygon = common.coco_to_polygon(annotation)
                region = common.polygon_mask(polygon, image['width'], image['height'])
                assert np.array_equal(region, label == annotation['label_index'])   # label PNG = rasterised polygon
                assert annotation['segmentation_subpixel'] and len(annotation['segmentation_subpixel'][0]) >= 8
        for annotation in coco['annotations']:
            assert required_annotation <= set(annotation) and annotation['id'] not in annotation_ids
            annotation_ids.add(annotation['id'])
            assert annotation['provenance'] == 'hires-teacher-com-2018' and annotation['weight'] == 1.
            assert annotation['tier'] == 'T3' and annotation['boundary_reference'] is False
            assert annotation['category_id'] == 1 and annotation['iscrowd'] == 0
            total += 1
    assert total == 12 and manifest['n_windows'] == 2
    assert manifest['weights'] == {'hires-teacher-com-2018': 1.}
    for name, digest in manifest['files'].items():
        assert common.sha256_file(out / name) == digest
    control = common.read_json(out / 'sam3_control.json')['images']
    assert set(control) == {'com2018-r001-c002', 'com2018-r001-c003'} and all(len(v) == 6 for v in control.values())
    assert common.read_json(out / 'references.json')['references'] == []
    assert not (root / 'data/scenes').exists()                            # nothing written under data/scenes


def test_extra_parts_of_a_teacher_roof_are_ignore_not_background():
    mask = np.zeros((60, 80), bool)
    mask[5:40, 5:50] = True
    mask[45:55, 60:75] = True                                              # a detached second piece of the same roof
    polygon, others, dropped = distill.label_polygon(ht.Piece(100, 200, mask, .9))
    assert polygon.area == 35 * 45 and dropped == 150 and len(others) == 1
    assert polygon.bounds == (105., 205., 150., 240.) and others[0].bounds == (160., 245., 175., 255.)


def test_splits_are_by_window_deterministic_and_seeded():
    ids = [ht.window_id(r, c) for r in range(6) for c in range(10)]
    splits = distill.assign_splits(ids, 20260928)
    assert splits == distill.assign_splits(list(reversed(ids)), 20260928)
    assert splits != distill.assign_splits(ids, 1)
    counts = {s: sum(v == s for v in splits.values()) for s in distill.SPLITS}
    assert counts == {'train': 42, 'val': 9, 'test': 9}


def test_provenance_fields(teacher_run):
    root, _, _, _, manifest = teacher_run
    prov = common.read_json(root / ht.TEACHER / 'provenance.json')
    for record in (prov, ht.provenance(root), manifest['source']):
        assert record['licence'] == 'CC BY 4.0'
        assert 'City of Melbourne' in record['attribution'] and 'CC BY 4.0' in record['attribution']
        assert record['url'].endswith(ht.ZIP_NAME) and record['capture'] == 'May 2018'
        assert record['crs'].startswith('EPSG:28355')
    assert prov['labels_are'].startswith('SAM3 model outputs')
    card = (root / ht.OUT / 'DATASET_CARD.md').read_text()
    assert 'CC BY 4.0' in card and 'City of Melbourne' in card and 'not accuracy' in card


# --- round 3: whole-building label target and the outline policy ----------------------------------------------------

def test_merge_groups_unions_pieces_inside_one_outline_and_leaves_the_rest_single():
    from shapely.geometry import box
    outlines = [('A', box(0, 0, 100, 100)), ('B', box(200, 0, 300, 100))]
    pieces = [(1, box(5, 5, 50, 95)), (2, box(55, 5, 95, 95)),          # two halves of building A
              (3, box(205, 5, 295, 95)),                                # B whole, alone
              (4, box(40, 0, 260, 100)),                                # covers more than half of A and of B: spans two outlines
              (5, box(400, 400, 420, 420)),                             # nothing here
              (6, box(90, 40, 130, 60))]                                # a quarter inside A: not mostly inside
    groups, stats = distill.merge_groups(pieces, outlines)
    assert groups == {'A': [1, 2]}
    assert stats['groups_merged'] == 1 and stats['pieces_merged'] == 2 and stats['pieces_in_one_outline_alone'] == 1
    assert stats['pieces_spanning_two_outlines'] == 1 and stats['pieces_no_outline'] == 2
    assert distill.merge_groups(pieces, []) == ({}, {'pieces_no_outline': 6, 'pieces_in_one_outline_alone': 0, 'groups_merged': 0, 'pieces_merged': 0})


def test_unit_pieces_union_masks_and_unit_row_synthesises_members():
    size, student = 40, 20
    def row(index, x0, x1, score):
        native = np.zeros((size, size), bool)
        native[10:30, x0:x1] = True
        piece = ht.piece_from_mask(native, score=score)
        stud = np.zeros((student, student), bool)
        stud[5:15, x0 // 2:x1 // 2] = True
        spiece = ht.piece_from_mask(stud, score=score)
        return {'index': index, 'score': score, 'subtiles': [[0, 0]],
                'rle_native': ht.rle_encode(piece, size, size), 'bbox_native': [piece.x0, piece.y0, piece.x1 - piece.x0, piece.y1 - piece.y0],
                'student': {'rle': ht.rle_encode(spiece, student, student), 'bbox': [spiece.x0, spiece.y0, spiece.x1 - spiece.x0, spiece.y1 - spiece.y0]}}
    rows = [row(3, 4, 14, .7), row(9, 14, 24, .9)]
    piece, native = distill.unit_pieces(rows, size, student)
    assert native.area == 2 * 20 * 10 and native.x0 == 4 and native.x1 == 24 and piece.area == 10 * 10 and piece.x0 == 2 and piece.x1 == 12
    assert distill.unit_row(rows) == {'index': 3, 'score': .9, 'subtiles': [(0, 0)]}
    single, single_native = distill.unit_pieces(rows[:1], size, student)
    assert single.area == 5 * 10 and single_native.area == 200 and distill.unit_row(rows[:1]) is rows[0]
    units = distill.teacher_units({'teacher': rows + [row(11, 30, 36, .5)]}, {'A': [3, 9]})
    assert [u['merged_from'] for u in units] == [[3, 9], None] and units[0]['outline_id'] == 'A' and units[1]['rows'][0]['index'] == 11


def test_whole_squared_gated_export_merges_pieces_by_open_outline(teacher_run, tmp_path):
    root, _, _, _, _ = teacher_run
    crops = common.read_json(root / ht.TEACHER / 'crops.json')
    outlines_dir = root / 'data/imagery/com-2018/outlines'
    outlines_dir.mkdir(parents=True, exist_ok=True)
    for window in crops['windows']:
        e0, px, _, n0, _, _ = window['native_affine']
        x0, y0, x1, y1 = 390, 2190, 810, 2410                          # covers RECTANGLES[4] and RECTANGLES[5]
        ring = [[e0 + px * x, n0 - px * y] for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))]
        common.write_json_atomic(outlines_dir / f"{window['id']}.geojson",
                                 {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {'building_id': 77},
                                                                             'geometry': {'type': 'Polygon', 'coordinates': [ring]}}]})
    out = tmp_path / 'merged'
    manifest = distill.export(root, out=out, seed=7, label_target='whole_squared_gated', source_id='com-2018')
    assert manifest['label_target'] == 'whole_squared_gated' and manifest['merge']['windows_with_outlines'] == 2
    assert manifest['merge']['groups_merged'] == 2 and manifest['merge']['pieces_merged'] == 4 and manifest['merge']['pieces_no_outline'] == 8
    # the two outlined rectangles sit 2 m apart: their union is disconnected on the student grid, so the group reverts
    # to its single pieces instead of painting the smaller one IGNORE (background for SAM3)
    assert manifest['merge']['merged_union_multipart_reverted'] == 2
    total = 0
    for split in distill.SPLITS:
        coco = common.read_json(out / f'{split}.json')
        for image in coco['images']:
            anns = [a for a in coco['annotations'] if a['image_id'] == image['id']]
            assert len(anns) == len(RECTANGLES) and all(a['merged_from'] is None and a['outline_id'] is None for a in anns)
            label = np.asarray(Image.open(out / image['label_file']))
            assert label.max() == len(RECTANGLES) and not (label == common.IGNORE).any()
            total += len(anns)
    assert total == 2 * len(RECTANGLES)
    assert sum(manifest['counts'][s].get('_merged_instances', 0) for s in distill.SPLITS) == 0
    # touching pieces do merge: bridge RECTANGLES[4] and RECTANGLES[5] in the native image and replace the two teacher rows
    # by two halves of the bridged block that share an edge
    size = crops['native_window_px']
    scale = ht.STUDENT_PX / size
    for window in crops['windows']:
        path = root / window['native_file']
        native = np.asarray(Image.open(path).convert('RGB')).copy()
        native[2200:2400, 600:620] = 220
        ht.save_png(path, native)
        record_path = ht.window_dir(root, window['id']) / 'record.json'
        record = common.read_json(record_path)
        kept = [r for r in record['teacher'] if not (r['bbox_native'][1] >= 2200 and r['bbox_native'][0] in (400, 620))]
        halves = []
        for index, (x0, x1) in ((98, (400, 600)), (99, (600, 800))):
            block = np.zeros((size, size), bool)
            block[2200:2400, x0:x1] = True
            piece = ht.piece_from_mask(block, score=.8)
            student = np.zeros((ht.STUDENT_PX, ht.STUDENT_PX), bool)
            student[int(2200 * scale):int(2400 * scale), int(x0 * scale):int(x1 * scale)] = True
            spiece = ht.piece_from_mask(student, score=.8)
            halves.append({'index': index, 'score': .8, 'subtiles': [[0, 0]], 'area_native_px': piece.area,
                           'rle_native': ht.rle_encode(piece, size, size), 'bbox_native': [piece.x0, piece.y0, piece.x1 - piece.x0, piece.y1 - piece.y0],
                           'student': {'rle': ht.rle_encode(spiece, ht.STUDENT_PX, ht.STUDENT_PX), 'area_px': spiece.area,
                                       'bbox': [spiece.x0, spiece.y0, spiece.x1 - spiece.x0, spiece.y1 - spiece.y0]}})
        assert len(kept) == len(RECTANGLES) - 2, [r['bbox_native'] for r in record['teacher']]
        record['teacher'] = kept + halves
        record['sha256']['native'] = common.sha256_file(path)
        common.write_json_atomic(record_path, record)
    out2 = tmp_path / 'merged-touching'
    manifest2 = distill.export(root, out=out2, seed=7, label_target='whole_squared_gated', source_id='com-2018')
    assert manifest2['merge']['groups_merged'] == 2 and manifest2['merge'].get('merged_union_multipart_reverted', 0) == 0
    merged = [a for s in distill.SPLITS for a in common.read_json(out2 / f'{s}.json')['annotations'] if a['merged_from']]
    assert len(merged) == 2, manifest2['merge']
    for a in merged:
        assert sorted(a['merged_from']) == [98, 99] and a['outline_id'] == 77 and a['object_id'] == 'teacher-98', {k: a[k] for k in ('merged_from', 'outline_id', 'object_id')}
        assert a['area_m2'] == 800.
    assert sum(manifest2['counts'][s].get('_merged_instances', 0) for s in distill.SPLITS) == 2
    assert 'whole_squared_gated' in manifest['label_target_note'] and 'merged_from' in manifest['label_target_note']
    with pytest.raises(SystemExit):
        distill.export(root, out=tmp_path / 'nope', label_target='whole_squared_gated')       # needs the source id for its outlines


def test_outline_policy_reads_outline_rows_and_never_drops_parts(tmp_path):
    judges = tmp_path / 'judges'
    judges.mkdir()
    (judges / 'ledger_rows.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'window': 'w', 'index': 3, 'recommendation': 'ignore', 'cross_capture': 'only_here'},
        {'window': 'w', 'index': 5, 'recommendation': 'review', 'cross_capture': 'matched'},
        {'window': 'w', 'index': 6, 'recommendation': 'review', 'cross_capture': 'only_here'},
        {'window': 'w', 'index': 7, 'recommendation': 'review', 'cross_capture': 'only_here'},
        {'window': 'w', 'index': 8, 'recommendation': 'part', 'cross_capture': 'only_here'}]) + '\n')
    (judges / 'outlines_rows.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'window': 'w', 'arm': 'teacher', 'index': 5, 'kind': 'unmatched', 'area_m2': 10.},     # small unmatched but the other capture saw it
        {'window': 'w', 'arm': 'teacher', 'index': 6, 'kind': 'unmatched', 'area_m2': 20.},     # small unmatched, only here: ignore
        {'window': 'w', 'arm': 'teacher', 'index': 7, 'kind': 'unmatched', 'area_m2': 30.},     # 25 m2 or more: never dropped
        {'window': 'w', 'arm': 'teacher', 'index': 8, 'kind': 'part', 'area_m2': 5.},           # a part: never dropped
        {'window': 'w', 'arm': 'control', 'index': 1, 'kind': 'unmatched', 'area_m2': 5.}]) + '\n')   # control rows are not teacher labels
    assert distill._policy_ignored(tmp_path, 'ledger-v1') == {('w', 3)}
    assert distill._policy_ignored(tmp_path, 'outline-v1') == {('w', 3), ('w', 6)}
    assert distill._policy_ignored(tmp_path, 'none') == set()
    (judges / 'ledger_rows.jsonl').unlink()
    assert distill._policy_ignored(tmp_path, 'outline-v1') == {('w', 5), ('w', 6)}, 'without a ledger the cross-capture exemption does not exist'
    assert distill.LABEL_SUFFIX['whole_squared_gated'] == '-squared-merged' and distill.POLICY_SUFFIX['outline-v1'] == '-outline'
