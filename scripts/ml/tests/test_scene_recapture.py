"""recapture-v1 (dry run): frame_transform, residual, the byte-level verifier, the offline plan and the pilot row shape.

Synthetic frames render one georeferenced 'world' (random rectangles on a textured ground) through each frame's own
affine, so two frames of the same place agree by construction and a planted shift is a known displacement.
Nothing here reads or writes the repository's data/scenes/.
"""
import copy
import hashlib
import io
import json
import math
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import scene_lot as L
from scripts.ml import scene_lot_tags as T
from scripts.ml import scene_recapture as R
from scripts.ml.scene_addresses import WORLD
from scripts.ml.scene_extract import save_scene
from scripts.ml.scene_region import region_frame

REPO = Path(__file__).resolve().parents[3]
LON0, LAT0 = 145.3111, -37.7907
SUBJECT = {'min_overlap_px': 128, 'applies_to': "frame_transform.role == 'frame_cut_target' (scene_focus windows)"}
DESIGN = {'version': 'recapture-design-v1', 'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6,
          'match_iou': .5, 'close_iou': .9, 'subject_recapture': SUBJECT}
PILOT = REPO/'data/reviews/eyes/recapture/pilot-2026-09-30.json'      # the dated pilot: read only, never rewritten


# --- synthetic georeferenced frames ------------------------------------------------------------------------------

def world(seed=7):
    """Colour of the ground at (x east, y north) metres about (LON0, LAT0): rectangles over a textured field."""
    rng = np.random.default_rng(seed)
    centres = np.r_[rng.uniform(-25, 25, (40, 2)), rng.uniform(-70, 70, (40, 2))]
    halves = rng.uniform(1.5, 6., (80, 2))
    levels = rng.uniform(25, 230, (80, 3))

    def colour(x, y):
        rgb = np.stack([110+30*np.sin(x/3.1+np.cos(y/4.7)), 105+25*np.cos(y/2.3+.5*np.sin(x/5.9)),
                        95+20*np.sin((x-y)/3.7)], -1)
        for (cx, cy), (hx, hy), level in zip(centres, halves, levels, strict=True):
            rgb[(np.abs(x-cx) < hx) & (np.abs(y-cy) < hy)] = level
        return np.clip(rgb, 0, 255).astype(np.uint8)
    return colour


def world_metres(lon, lat):
    scale = WORLD*math.cos(math.radians(LAT0))
    y0 = math.asinh(math.tan(math.radians(LAT0)))/(2*math.pi)
    return (lon-LON0)/360*scale, (np.arcsinh(np.tan(np.radians(lat)))/(2*np.pi)-y0)*scale


def render(scene, colour, shift_m=(0., 0.)):
    """PNG bytes of the frame; content displaced by +shift_m metres (east, north) when planted."""
    w, h = scene['image_width'], scene['image_height']
    px, py = np.meshgrid(np.arange(w)+.5, np.arange(h)+.5)
    a = np.asarray(scene['pixel_to_meters'], float)
    mx, my = px*a[0][0]+py*a[1][0]+a[2][0], px*a[0][1]+py*a[1][1]+a[2][1]
    lon0, lat0 = scene['centroid']['lon'], scene['centroid']['lat']
    scale = WORLD*math.cos(math.radians(lat0))
    y0 = math.asinh(math.tan(math.radians(lat0)))/(2*math.pi)
    x, y = world_metres(lon0+mx/scale*360, np.degrees(np.arctan(np.sinh(2*np.pi*(y0+my/scale)))))
    output = io.BytesIO()
    Image.fromarray(colour(x-shift_m[0], y-shift_m[1]), 'RGB').save(output, format='PNG')
    return output.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inverse(affine):
    full = np.eye(3)
    full[:, :2] = np.asarray(affine, float)
    return np.linalg.inv(full)[:, :2]


def fitted_scene(scene_id='roof_training_250-4242', east_m=6., north_m=-4., m_per_px=.0625):
    """A 512 px cohort-style frame: no region key, affines solved (not tiled), centroid off the image centre."""
    lon, lat = T.inverse_projection({'centroid': {'lon': LON0, 'lat': LAT0}, 'pixel_to_meters': [[1, 0], [0, 1], [0, 0]]})(east_m, north_m)
    p2m = np.array([[m_per_px, 1e-6], [2e-6, -m_per_px], [-256*m_per_px+.4, 256*m_per_px-.3]])
    return {'schema_version': 1, 'id': scene_id, 'address_id': 4242, 'cohort': 'roof_training_250', 'title': 'Test',
            'centroid': {'lon': lon, 'lat': lat}, 'image_width': 512, 'image_height': 512,
            'pixel_to_meters': p2m.tolist(), 'meters_to_pixel': inverse(p2m).tolist(), 'objects': [], 'groups': [], 'scans': {}}


def z20_scene(scene_id='house-4242', lon=LON0, lat=LAT0):
    """A 65 m z20 capture expanded to 1024 px about the same centroid (scene_expand.expanded_source), like house-*."""
    frame = region_frame([lon-.00037, lat-.0002, lon+.00037, lat+.0002])
    assert frame['region']['imagery_zoom'] == 20
    p2m, m2p = np.asarray(frame['pixel_to_meters'], float), np.asarray(frame['meters_to_pixel'], float)
    p2m[2] -= 256*(p2m[0]+p2m[1])
    m2p[2] += 256
    return {'schema_version': 1, 'id': scene_id, 'address_id': 4242, 'cohort': None, 'title': 'Test', 'centroid': frame['centroid'],
            'region': {**frame['region'], 'span_m': frame['region']['span_m']*2}, 'image_width': 1024, 'image_height': 1024,
            'pixel_to_meters': p2m.tolist(), 'meters_to_pixel': m2p.tolist(), 'objects': [], 'groups': [], 'scans': {}}


def spacing(scene):
    return math.sqrt(abs(np.linalg.det(np.asarray(scene['pixel_to_meters'], float)[:2])))


def write_design(root, **changes):
    path = Path(root)/R.DESIGN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: v for k, v in {**DESIGN, **changes}.items() if v is not None}, indent=1))
    return path


# --- frame_transform ---------------------------------------------------------------------------------------------

def test_transform_round_trips_a_fitted_cohort_snapshot_into_a_z20_1024_px_region_snapshot():
    source, target = fitted_scene(), z20_scene()
    before = copy.deepcopy((source, target))
    ft = R.frame_transform(source, target)
    assert (source, target) == before, 'pure: the snapshots are not touched'
    assert ft['version'] == 'frame-transform-v1' and ft['method'] == 'georeferenced-affine'
    assert ft['source_scene_id'] == source['id'] and ft['source_size'] == [512, 512] and ft['source_registration'] == 'fitted'
    assert R.frame_transform(target, source)['source_registration'] == 'region'
    points = np.array([[0, 0], [512, 0], [512, 512], [0, 512], [256, 256], [100.5, 400.25], [3.7, 77.1]])
    mapped = R.apply_affine(points, ft['pixel_affine'])
    np.testing.assert_allclose(R.apply_affine(mapped, R.invert_affine(ft['pixel_affine'])), points, atol=1e-9)
    # the same ground point: both frames' own projections agree to well under a millimetre
    here = np.array([world_metres(*T.inverse_projection(source)(*p)) for p in points])
    there = np.array([world_metres(*T.inverse_projection(target)(*q)) for q in mapped])
    assert np.abs(here-there).max() < 1e-3
    # metre_offset is the source's local-metre origin (its centroid) in the target's local metres
    origin = R.apply_affine([ft['metre_offset']], target['meters_to_pixel'])[0]
    lon, lat = T.inverse_projection(target)(*origin)
    assert lon == pytest.approx(source['centroid']['lon'], abs=1e-10) and lat == pytest.approx(source['centroid']['lat'], abs=1e-10)
    scale = math.sqrt(abs(np.linalg.det(np.asarray(ft['pixel_affine'])[:2])))
    assert ft['overlap'] == {'source_px_inside_target': 512*512, 'fraction': 1.0,
                             'region_px': {'side_px': round(512*scale-2*R.EDGE_PX, 1), 'area_px': int(round((512*scale-2*R.EDGE_PX)**2)),
                                           'eroded_by_px': R.EDGE_PX}}
    # the contract: pixel_affine = target.meters_to_pixel o (+metre_offset) o source.pixel_to_meters
    a = np.asarray(source['pixel_to_meters'], float)
    expected = R.apply_affine(np.c_[points, np.ones(len(points))] @ a+ft['metre_offset'], target['meters_to_pixel'])
    np.testing.assert_allclose(mapped, expected, atol=1e-9)
    assert ft['residual'] is None and ft['verified'] is False, 'no pixels were measured yet'


def test_overlap_counts_the_source_pixels_that_land_inside_the_target_frame():
    target = z20_scene()
    east = target['region']['span_m']/2
    straddling = fitted_scene(east_m=east-.4, north_m=.3)  # image centre = centroid + (0.4, -0.3) m: on the east edge
    ft = R.frame_transform(straddling, target)
    assert ft['overlap']['fraction'] == pytest.approx(.5, abs=.01)
    assert ft['overlap']['source_px_inside_target'] == pytest.approx(512*512/2, abs=512*512*.01)
    assert R.frame_transform(fitted_scene(east_m=east+40), target)['overlap'] == {
        'source_px_inside_target': 0, 'fraction': 0.0, 'region_px': {'side_px': 0., 'area_px': 0, 'eroded_by_px': R.EDGE_PX}}
    assert ft['role'] is None, 'a same-address transform carries no role: the fraction rule applies'


def test_overlap_records_the_eroded_region_the_residual_measures_on():
    """region_px is the overlapping region in TARGET px after the EDGE_PX erosion residual() applies to its cover mask: its
    bounding-box side (the smaller of width and height) and area; the residual's own box agrees with it."""
    colour = world()
    target = z20_scene()
    east = target['region']['span_m']/2
    straddling = fitted_scene(east_m=east-.4, north_m=.3)
    ft = R.frame_transform(straddling, target)
    scale = math.sqrt(abs(np.linalg.det(np.asarray(ft['pixel_affine'])[:2])))      # target px per source px
    region = ft['overlap']['region_px']
    assert region['eroded_by_px'] == R.EDGE_PX == 10
    assert region['side_px'] == pytest.approx(256*scale-2*R.EDGE_PX, abs=2), 'half the frame is inside: the eroded half-width is the side'
    assert region['area_px'] == pytest.approx((256*scale-20)*(512*scale-20), rel=.02)
    whole = R.frame_transform(fitted_scene(), target)['overlap']['region_px']
    assert whole['side_px'] == pytest.approx(512*scale-2*R.EDGE_PX, abs=1) and whole['area_px'] == pytest.approx(whole['side_px']**2, rel=.01)
    _, detail = R.residual(render(straddling, colour), render(target, colour), ft, spacing(target), detail=True)
    x0, y0, x1, y1 = detail['box_px']
    assert min(x1-x0, y1-y0) == pytest.approx(region['side_px'], abs=2), 'the geometric region is the box the residual correlates'


# --- residual ----------------------------------------------------------------------------------------------------

def test_residual_recovers_a_planted_2_px_shift_and_fails_a_1_5_m_one():
    colour = world()
    source, target = fitted_scene(), z20_scene()
    source_png = render(source, colour)
    ft = R.frame_transform(source, target)
    s = spacing(target)
    same = R.residual(source_png, render(target, colour), ft, s)
    assert same['method'] == 'gradient-ncc+phase-correlation' and same['warped_from'] == sha(source_png)
    assert same['shift_m'] < .06 and same['ncc'] > .6 and same['samples'] > 40000
    assert R.verdict({**ft, 'residual': same}, DESIGN) == (True, [])
    planted = R.residual(source_png, render(target, colour, (2*s, 0.)), ft, s)
    assert planted['shift_px'][0] == pytest.approx(2, abs=.25) and planted['shift_px'][1] == pytest.approx(0, abs=.25)
    assert planted['shift_m'] == pytest.approx(2*s, abs=.035) and planted['ncc'] > .6
    assert R.verdict({**ft, 'residual': planted}, DESIGN)[0] is True
    far = R.residual(source_png, render(target, colour, (1.2, -.9)), ft, s)
    assert far['shift_px'][0] == pytest.approx(1.2/s, abs=.3) and far['shift_px'][1] == pytest.approx(.9/s, abs=.3)
    assert far['shift_m'] == pytest.approx(1.5, abs=.06)
    assert R.verdict({**ft, 'residual': far}, DESIGN) == (False, [f"shift_m {far['shift_m']:.3f} > max_shift_m 0.7"])
    elsewhere = R.residual(source_png, render(target, world(seed=99)), ft, s)
    ok, reasons = R.verdict({**ft, 'residual': elsewhere}, DESIGN)
    assert elsewhere['ncc'] < .35 and not ok and any(r.startswith('ncc') for r in reasons)


def test_a_featureless_frame_is_unmeasured_not_verified():
    source, target = fitted_scene(), z20_scene()
    flat = io.BytesIO()
    Image.new('RGB', (512, 512), (90, 110, 130)).save(flat, format='PNG')
    ft = R.frame_transform(source, target)
    result = R.residual(flat.getvalue(), render(target, world()), ft, spacing(target))
    assert result['ncc'] is None and result['shift_m'] is None
    assert R.verdict({**ft, 'residual': result}, DESIGN)[0] is False


# --- verifier CLI --------------------------------------------------------------------------------------------------

def saved_pair(root, planted_m=(0., 0.), colour=None):
    write_design(root)
    colour = colour or world()
    source = fitted_scene()
    source_png = render(source, colour)
    source = save_scene({**source, 'image_sha256': sha(source_png)}, source_png, root)
    target = z20_scene('lot-4242')
    target_png = render(target, colour, planted_m)
    target['image_sha256'] = sha(target_png)
    target['frame_transform'] = R.measured_transform(source, target, source_png, target_png, R.load_design(root))
    target = save_scene(target, target_png, root)
    return source, target


def snapshot_path(root, scene):
    return Path(root)/'data/scenes'/scene['id']/'models'/f"{scene['model_signature']}.json"


def verify_args(root, source, target):
    return ['--verify-source', str(snapshot_path(root, source)), '--verify-target', str(snapshot_path(root, target))]


def test_verify_cli_accepts_a_measured_recapture_and_rejects_changed_bytes_with_the_stable_message(tmp_path, capsys):
    source, target = saved_pair(tmp_path)
    ft = target['frame_transform']
    assert ft['verified'] is True and ft['design']['design_sha256'] == sha((tmp_path/R.DESIGN_PATH).read_bytes())
    assert set(ft) == {'version', 'method', 'source_scene_id', 'source_model_signature', 'source_image_sha256', 'source_size',
                       'source_registration', 'metre_offset', 'pixel_affine', 'overlap', 'residual', 'design', 'verified', 'role'}
    assert set(ft['residual']) == {'method', 'warped_from', 'shift_px', 'shift_m', 'ncc', 'samples'}
    assert set(ft['design']) == {'max_shift_m', 'min_ncc', 'min_overlap_fraction', 'match_iou', 'close_iou', 'design_sha256'}, \
        'a same-address recapture records the fraction design only: the subject floor is not applied to it'
    assert ft['role'] is None
    assert R.main(verify_args(tmp_path, source, target)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['verified'] is True and out['shift_m'] == ft['residual']['shift_m']
    # the source image bytes change under the same name: the verifier re-hashes, never trusts metadata
    png = tmp_path/'data/scenes'/source['id']/f"{source['image_sha256']}.png"
    original = png.read_bytes()
    png.write_bytes(render(source, world(seed=3)))
    with pytest.raises(SystemExit) as failed:
        R.main(verify_args(tmp_path, source, target))
    assert failed.value.code == 1
    assert capsys.readouterr().err.splitlines()[0] == R.FAILURE == 'Recaptured frame does not agree with the source frame'
    png.write_bytes(original)
    assert R.main(verify_args(tmp_path, source, target)) == 0
    # a recorded affine that the snapshots do not reproduce is refused, even when re-saved with a fresh signature
    forged = copy.deepcopy(target)
    forged['frame_transform']['pixel_affine'][2][0] += 1e-3
    forged = save_scene(forged, (tmp_path/'data/scenes'/target['id']/f"{target['image_sha256']}.png").read_bytes(), tmp_path)
    with pytest.raises(SystemExit):
        R.main(verify_args(tmp_path, source, forged))
    assert 'pixel_affine' in capsys.readouterr().err
    # the design changed after measurement: re-measure under the new pre-registration before any carry
    write_design(tmp_path, max_shift_m=.5)
    with pytest.raises(SystemExit):
        R.main(verify_args(tmp_path, source, target))
    assert 'design' in capsys.readouterr().err


def test_verify_cli_refuses_a_misregistered_recapture(tmp_path, capsys):
    source, target = saved_pair(tmp_path, planted_m=(1.2, -.9))
    assert target['frame_transform']['verified'] is False
    with pytest.raises(SystemExit) as failed:
        R.main(verify_args(tmp_path, source, target))
    err = capsys.readouterr().err
    assert failed.value.code == 1 and err.splitlines()[0] == R.FAILURE and 'shift_m' in err


def test_verify_module_entry_point_exit_codes(tmp_path):
    source, target = saved_pair(tmp_path)
    command = [sys.executable, '-m', 'scripts.ml.scene_recapture', *verify_args(tmp_path, source, target)]
    good = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=120)
    assert good.returncode == 0 and json.loads(good.stdout)['verified'] is True
    (tmp_path/'data/scenes'/source['id']/f"{source['image_sha256']}.png").write_bytes(render(source, world(seed=5)))
    bad = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=120)
    assert bad.returncode == 1 and bad.stderr.splitlines()[0] == 'Recaptured frame does not agree with the source frame'


# --- object projection -------------------------------------------------------------------------------------------

def test_metre_fields_are_translated_and_pixel_fields_affine_mapped():
    source, target = fitted_scene(), z20_scene()
    ft = R.frame_transform(source, target)
    dx, dy = ft['metre_offset']
    ring, hole = [[100, 100], [200, 100], [200, 180], [100, 180]], [[120, 120], [140, 120], [140, 140]]
    obj = {'id': 'a', 'kind': 'building', 'status': 'confirmed', 'ring': ring, 'holes': [hole],
           'roof_feedback': {'outline': {'ring': ring, 'holes': [hole]}, 'family': 'hipped',
                             'linework': {'vertices': [[1., 2.], [3., 4.]], 'faces': [], 'edges': []}},
           'roof_line_edits': {'outline': {'ring': ring, 'holes': []}, 'vertices': [[1., 2.], [5., 6.]],
                               'baseline': {'vertices': [[7., 8.]], 'edges': []}, 'edges': [], 'checked': False, 'notes': ''},
           'roof_annotation': {'vertices': [[1., 2., 3.2], [4., 5., 4.8]], 'faces': [], 'edges': []},
           'roof_parts': [{'ring_meters': [[1., 1.], [2., 1.], [2., 2.]], 'ring': [[1., 1.], [2., 1.], [2., 2.]],
                           'coords': [[1., 1.], [2., 1.], [2., 2.]], 'ring_z': [3., 3., 4.], 'ring_pixels': ring[:3],
                           'ring_lonlat': [[145.3, -37.7]]}],
           'roof_lines': [{'line_meters': [[1., 1.], [2., 2.]], 'line_pixels': [[100, 100], [200, 180]], 'line_z': [3., 4.],
                           'line_lonlat': [[145.3, -37.7], [145.31, -37.71]]}],
           'boundary_refinement': {'method': 'roof-envelope-v1', 'raw_ring': ring, 'raw_holes': [hole]},
           'model': {'height_m': 3.2, 'area_m2': 99.}}
    before = copy.deepcopy(obj)
    moved = R.project_object(obj, ft)
    assert obj == before, 'the source object is not mutated'
    px = [list(p) for p in R.apply_affine(ring, ft['pixel_affine'])]
    hole_px = [list(p) for p in R.apply_affine(hole, ft['pixel_affine'])]
    assert moved['ring'] == px and moved['holes'] == [hole_px]
    assert moved['roof_feedback']['outline'] == {'ring': px, 'holes': [hole_px]}
    assert moved['roof_line_edits']['outline'] == {'ring': px, 'holes': []}
    assert moved['boundary_refinement']['raw_ring'] == px and moved['boundary_refinement']['raw_holes'] == [hole_px]
    assert moved['roof_parts'][0]['ring_pixels'] == px[:3] and moved['roof_lines'][0]['line_pixels'] == [px[0], px[2]]
    shift = [[1+dx, 2+dy], [5+dx, 6+dy]]
    assert moved['roof_line_edits']['vertices'] == shift and moved['roof_line_edits']['baseline']['vertices'] == [[7+dx, 8+dy]]
    assert moved['roof_feedback']['linework']['vertices'] == [[1+dx, 2+dy], [3+dx, 4+dy]]
    assert moved['roof_annotation']['vertices'] == [[1+dx, 2+dy, 3.2], [4+dx, 5+dy, 4.8]], 'z is unchanged'
    part = moved['roof_parts'][0]
    assert part['ring_meters'] == part['ring'] == part['coords'] == [[1+dx, 1+dy], [2+dx, 1+dy], [2+dx, 2+dy]]
    assert part['ring_z'] == [3., 3., 4.] and part['ring_lonlat'] == [[145.3, -37.7]], 'heights and lon/lat stay'
    assert moved['roof_lines'][0]['line_meters'] == [[1+dx, 1+dy], [2+dx, 2+dy]] and moved['roof_lines'][0]['line_z'] == [3., 4.]
    assert moved['status'] == 'confirmed' and moved['model'] == obj['model'], 'projection moves geometry; the carry decides status'
    # a metre point and its pixel land on the same target pixel (exact inverse affines here)
    metre = R.apply_affine([[1+dx, 1+dy]], target['meters_to_pixel'])[0]
    pixel = R.apply_affine(R.apply_affine([[1., 1.]], source['meters_to_pixel']), ft['pixel_affine'])[0]
    np.testing.assert_allclose(metre, pixel, atol=1e-6)


def test_roof_part_solids_planes_gable_edges_and_assembly_rings_move_with_the_frame():
    # Fields the contract's list does not name but real scenes carry: left behind they would contradict the moved ring.
    ft = R.frame_transform(fitted_scene(), z20_scene())
    dx, dy = ft['metre_offset']
    ring = [[100, 100], [200, 100], [200, 180]]
    plane = {'a': .17, 'b': -.09, 'c': 6.}
    obj = {'id': 'p', 'kind': 'building', 'ring': ring, 'model': {'footprint_area_px': 4000., 'area_m2': 16.},
           'roof_parts': [{'ring_meters': [[-6.39, 3.69], [-8.04, .62]], 'plane': dict(plane),
                           'triangles': [[[-6.39, 3.69, 4.6], [-8.04, .62, 4.6], [-3.99, .37, 5.3]]],
                           'gable_ends': [[[-8.04, .62], [-3.99, .37]]], 'z': [4.6, 4.6], 'eave_m': 4.6}],
           'boundary_refinement': {'raw_ring': ring, 'assembly': {'method': 'connected-roof-v1',
                                                                  'components': [{'id': 'c', 'ring': ring}]}}}
    moved = R.project_object(obj, ft)
    part = moved['roof_parts'][0]
    assert part['triangles'] == [[[-6.39+dx, 3.69+dy, 4.6], [-8.04+dx, .62+dy, 4.6], [-3.99+dx, .37+dy, 5.3]]]
    assert part['gable_ends'] == [[[-8.04+dx, .62+dy], [-3.99+dx, .37+dy]]]
    for x, y, _ in part['triangles'][0]:     # the plane still gives each moved vertex its unchanged height
        assert part['plane']['a']*x+part['plane']['b']*y+part['plane']['c'] == pytest.approx(plane['a']*(x-dx)+plane['b']*(y-dy)+plane['c'])
    assert part['z'] == [4.6, 4.6] and part['eave_m'] == 4.6
    px = [list(p) for p in R.apply_affine(ring, ft['pixel_affine'])]
    assert moved['boundary_refinement']['assembly']['components'][0]['ring'] == px
    scale = abs(np.linalg.det(np.asarray(ft['pixel_affine'])[:2]))
    assert moved['model']['footprint_area_px'] == pytest.approx(4000*scale) and moved['model']['area_m2'] == 16.


# --- offline plan ------------------------------------------------------------------------------------------------

LOT_ID, OTHER_ID, LON, LAT = 4242, 4343, 145.3125, -37.7905


def metre_lot(pfi, number, plan, x0, y0, x1, y1):
    unproject = T.inverse_projection(L._local_frame(LON, LAT))
    ring = [list(unproject(x, y)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring+[ring[0]]]},
            'properties': {'pfi': pfi, 'spi': f'{number}\\{plan}', 'lot': number, 'plan': plan, 'status': 'approved'}}


PARCELS = {'type': 'FeatureCollection', 'features': [metre_lot('p1', '1', 'PS1', -21.6, -15, 21.6, 15)],
           'bounds': [LON-.002, LAT-.002, LON+.002, LAT+.002]}


def cohort_root(tmp_path):
    (tmp_path/'data').mkdir()
    with closing(sqlite3.connect(tmp_path/'data/australia_geo.db')) as db:
        db.execute('CREATE TABLE addresses(id INTEGER PRIMARY KEY, full_address TEXT, lat REAL, lon REAL)')
        db.execute('INSERT INTO addresses VALUES(?, ?, ?, ?)', (LOT_ID, '1 TEST ROAD, Mooroolbark VIC 3138', LAT, LON))
        db.execute('INSERT INTO addresses VALUES(?, ?, ?, ?)', (OTHER_ID, '9 FAR ROAD, Mooroolbark VIC 3138', LAT-.01, LON+.01))
        db.commit()
    (tmp_path/'public').mkdir()
    (tmp_path/'public/address-roofs.json').write_text(json.dumps([
        {'address_id': LOT_ID, 'boundary_source': 'crm', 'centroid': {'lon': LON, 'lat': LAT}, 'ring_meters': [[-5, -4], [5, -4], [5, 4], [-5, 4]]}]))
    write_design(tmp_path)
    grey = io.BytesIO()
    Image.new('RGB', (512, 512), (100, 100, 100)).save(grey, format='PNG')

    def cohort(address_id, lon, lat, objects):
        scene = {**fitted_scene(f'roof_training_250-{address_id}', m_per_px=.065), 'address_id': address_id,
                 'centroid': {'lon': lon, 'lat': lat}, 'image_sha256': sha(grey.getvalue()), 'objects': objects}
        return save_scene(scene, grey.getvalue(), tmp_path)

    def model(oid, x0, y0, kind='building', **extra):
        ring = [[x0, y0], [x0+40, y0], [x0+40, y0+30], [x0, y0+30]]
        return {'id': oid, 'kind': kind, 'label': kind, 'ring': ring, 'status': 'candidate', 'geometry_reviewed': False,
                'source': 'sam3-text-onnx', 'model': {'height_m': 3.2, 'base_m': 0., 'height_source': 'class-prior',
                                                      'height_verified': False, 'color': '#777777'}, **extra}
    objects = [model('m1', 200, 200), model('m2', 260, 220), model('m3', 100, 100, 'tree'), model('m4', 300, 300, 'car'),
               model('m5', 20, 20, 'car'), model('m6', 400, 400), model('m7', 60, 400, 'pool'), model('m8', 150, 300),
               model('m9', 330, 90)]
    first = cohort(LOT_ID, LON, LAT, objects)
    cohort(OTHER_ID, LON+.01, LAT-.01, [model('n1', 200, 200)])
    decided = {o['id']: copy.deepcopy(o) for o in objects}
    decided['m1'].update(status='confirmed', geometry_reviewed=True)
    decided['m2'].update(status='rejected')
    decided['m3'].update(status='needs_adjustment', review_issues=['outline'])
    decided['m4'].update(status='superseded', replacement_ids=['drawn-1'])
    decided['m5'].update(status='confirmed', geometry_reviewed=True,
                         provenance={'source': 'machine:decision-policy-v2', 'tier': 'auto'})
    decided['m6'].update(status='confirmed', geometry_reviewed=True,
                         notes='Assistant visually traced from saved Google imagery on 2026-09-23.')
    decided['m7'].update(roof_parts=[{'ring_meters': [[1, 1], [2, 1], [2, 2]], 'ring_z': [3, 3, 3]}])
    drawn = {**model('drawn-1', 290, 290, 'car'), 'id': 'drawn-1', 'source': 'reviewer', 'status': 'confirmed',
             'geometry_reviewed': True, 'derived_from': ['m4']}
    review = {'model_signature': first['model_signature'], 'revision': 5, 'updated_at': '2026-09-30T11:00:00Z',
              'updated_by': 'local', 'history': [], 'groups': [], 'objects': [*decided.values(), drawn]}
    (tmp_path/'data/scenes'/first['id']/'review.json').write_text(json.dumps(review))
    return tmp_path


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))


def test_plan_offline_opens_no_socket_and_reads_parcels_only_through_the_offline_path(tmp_path, monkeypatch):
    root = cohort_root(tmp_path)
    calls = []

    def fake_parcels(scene, root, *, offline=False):
        calls.append((scene['id'], offline))
        return (PARCELS, 'sha-of-test-parcels', 'eyes-parcels') if scene['address_id'] == LOT_ID else (None, None, 'unavailable')

    def no_network(*args, **kwargs):
        raise AssertionError('the offline plan opened a socket')
    monkeypatch.setattr(L, 'parcels_for_scene', fake_parcels)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(socket, 'getaddrinfo', no_network)
    before = _tree(root)
    out = root/'plan.jsonl'
    assert R.main(['plan', '--cohort', 'roof_training_250', '--offline', '--out', str(out), '--root', str(root)]) == 0
    assert sorted(calls) == [(f'roof_training_250-{LOT_ID}', True), (f'roof_training_250-{OTHER_ID}', True)]
    assert [p for p in _tree(root) if p not in before] == ['plan.jsonl'], 'the plan writes only its own file'
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r['scene_id'] for r in rows] == [f'roof_training_250-{LOT_ID}', f'roof_training_250-{OTHER_ID}']
    planned, unplanned = rows
    assert planned['lot_frame']['zoom'] == 20 and planned['lot_frame']['group_kind'] == 'lot' and planned['parcels'] == 'eyes-parcels'
    assert unplanned['lot_frame'] is None and unplanned['parcels'] == 'parcels_unavailable'
    assert planned['registration'] == 'fitted' and planned['residual'] == 'unmeasured'
    assert planned['m_per_px']['source'] == pytest.approx(.065, abs=1e-4) and planned['m_per_px']['comparison'] == 'finer in native detail'
    assert 'upsample of a z19 composite (median of up to two providers by design' in planned['m_per_px']['source_wording']
    assert 'two-provider' not in planned['m_per_px']['source_wording'], 'the provider count is the design, not a per-scene fact'
    predicted = planned['predicted_post_carry']
    assert predicted['status'] == {'needs_adjustment': predicted['carried']} and predicted['all_needs_adjustment'] is True
    assert predicted['confirmed'] == predicted['rejected'] == 0 and predicted['geometry_reviewed'] == predicted['height_verified'] == 0
    assert planned['cost']['sam3_extractions'] == 1 and planned['cost']['capture_tiles'] > 0
    assert planned['training_impact'].startswith('none')
    assert planned['frame_transform_predicted']['overlap']['fraction'] == pytest.approx(1., abs=1e-6)
    assert set(planned['carry']['inside']) | set(planned['carry']['partial']) | set(planned['carry']['outside']) == set(planned['carry']['ids'])
    with pytest.raises(SystemExit):
        R.main(['plan', '--cohort', 'roof_training_250', '--out', str(out), '--root', str(root)])  # never without --offline
    with pytest.raises(SystemExit):
        R.main(['plan', '--cohort', 'roof_training_250', '--offline', '--root', str(root),
                '--out', str(root/'data/scenes/plan.jsonl')])  # never under data/scenes/


def test_plan_status_tally_follows_the_counting_rule(tmp_path, monkeypatch):
    root = cohort_root(tmp_path)
    monkeypatch.setattr(L, 'parcels_for_scene', lambda scene, root, *, offline=False: (None, None, 'unavailable'))
    out = root/'plan.jsonl'
    R.main(['plan', '--cohort', 'roof_training_250', '--offline', '--out', str(out), '--root', str(root)])
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    reviewed, bare = rows
    # all five statuses over every review.json object, machine_label and agent_authored separately
    assert reviewed['statuses'] == {'candidate': 3, 'confirmed': 4, 'needs_adjustment': 1, 'rejected': 1, 'superseded': 1}
    assert reviewed['machine_only'] == {'count': 1, 'ids': ['m5'], 'by_status': {'confirmed': 1}}
    assert reviewed['agent_authored'] == {'count': 1, 'ids': ['m6'], 'by_status': {'confirmed': 1}}
    # a human decision is confirmed / needs_adjustment / rejected only: a candidate is undecided, a superseded member is lineage
    assert reviewed['human_decisions'] == {'confirmed': 2, 'needs_adjustment': 1, 'rejected': 1}
    assert reviewed['statuses_excluding_machine_pending_agent'] == {'candidate': 3, 'confirmed': 2, 'needs_adjustment': 1,
                                                                    'rejected': 1, 'superseded': 1}
    assert reviewed['machine_confirmed_by_human'] == {'count': 0, 'ids': [], 'by_status': {}}
    assert reviewed['metre_fields'] == {'roof_parts': 1, 'roof_lines': 0, 'roof_feedback': 0, 'roof_annotation': 0, 'roof_line_edits': 0}
    # carried: human decisions, edited candidates and human-drawn objects; machine-only dropped; unedited candidates stay
    assert reviewed['carry']['ids'] == ['drawn-1', 'm1', 'm2', 'm3', 'm4', 'm6', 'm7']
    assert reviewed['carry']['dropped_machine_only'] == ['m5'] and reviewed['carry']['unedited_candidates'] == 2
    assert reviewed['lineage_sets'] == [{'members': ['drawn-1', 'm4'], 'status': 'unplanned'}]
    assert reviewed['predicted_post_carry']['status'] == {'needs_adjustment': 6} and reviewed['predicted_post_carry']['superseded_lineage_members'] == 1
    assert reviewed['review']['revision'] == 5 and reviewed['review']['model_signature_matches'] is True
    assert bare['review'] == {'exists': False} and bare['statuses'] == dict.fromkeys(R.STATUSES, 0)
    totals = reviewed['plan_totals']
    assert totals == bare['plan_totals'], 'every row carries the same totals, computed once at write time'
    assert totals['scenes'] == 2 and totals['scenes_with_review'] == 1 and totals['statuses'] == reviewed['statuses']
    assert totals['machine_only'] == 1 and totals['agent_authored'] == 1 and totals['metre_fields']['roof_parts'] == 1
    assert totals['predicted_post_carry'] == {'needs_adjustment': 6, 'confirmed': 0, 'rejected': 0}
    assert totals['computed_at'].endswith('Z') and totals['design_sha256'] == sha((root/R.DESIGN_PATH).read_bytes())
    assert totals['human_decisions'] == {'confirmed': 2, 'needs_adjustment': 1, 'rejected': 1}
    assert totals['statuses_excluding_machine_pending_agent'] == reviewed['statuses_excluding_machine_pending_agent']
    assert totals['machine_confirmed_by_human'] == 0 and 'replaces_run' not in totals
    assert set(totals['definitions']) >= {'human_decisions', 'statuses_excluding_machine_pending_agent', 'machine_only',
                                          'machine_confirmed_by_human'}
    # a second write names the file it replaces (its sha, time and code), so an earlier run is never silently lost
    first = sha(out.read_bytes())
    R.main(['plan', '--cohort', 'roof_training_250', '--offline', '--out', str(out), '--root', str(root)])
    again = json.loads(out.read_text().splitlines()[0])['plan_totals']['replaces_run']
    assert again['sha256'] == first and again['computed_at'] == totals['computed_at'] and again['code_sha256'] == R.code_sha256()


def test_a_machine_label_a_reviewer_confirmed_is_counted_under_both_named_rules_and_never_as_a_human_decision(tmp_path, monkeypatch):
    # The counting rule (review_authority_blocker) counts any machine:* provenance as machine_only; the carry's drop is the
    # review app's rule (machine:* and not human_confirmed). The plan names the objects where the two differ.
    root = cohort_root(tmp_path)
    monkeypatch.setattr(L, 'parcels_for_scene', lambda scene, root, *, offline=False: (None, None, 'unavailable'))
    path = root/'data/scenes'/f'roof_training_250-{LOT_ID}'/'review.json'
    review = json.loads(path.read_text())
    for obj in review['objects']:
        if obj['id'] == 'm9':
            obj.update(status='confirmed', geometry_reviewed=True,
                       provenance={'source': 'machine:decision-policy-v2', 'tier': 'auto', 'human_confirmed': True})
    path.write_text(json.dumps(review))
    row = R.plan('roof_training_250', root=root)[0]
    assert row['machine_only']['ids'] == ['m5', 'm9'] and row['machine_confirmed_by_human']['ids'] == ['m9']
    assert 'm9' in row['carry']['ids'] and row['carry']['dropped_machine_only'] == ['m5'], 'the app rule carries it'
    assert row['human_decisions'] == {'confirmed': 2, 'needs_adjustment': 1, 'rejected': 1}, 'never upgraded to a human decision'
    totals = row['plan_totals']
    assert (totals['machine_only'], totals['machine_confirmed_by_human'], totals['dropped_machine_only']) == (2, 1, 1)
    assert totals['predicted_post_carry'] == {'needs_adjustment': 7, 'confirmed': 0, 'rejected': 0}


# --- pilot pair selection ----------------------------------------------------------------------------------------

def test_pilot_rows_measure_a_house_pair_and_compare_model_objects(tmp_path):
    colour = world()
    write_design(tmp_path)
    source = fitted_scene('roof_training_250-4242')
    source_png = render(source, colour)
    roof = [[180, 180], [330, 180], [330, 300], [180, 300]]
    source['objects'] = [{'id': 'c1', 'kind': 'building', 'status': 'candidate', 'ring': roof, 'source': 'sam3-text-onnx'},
                         {'id': 'c2', 'kind': 'driveway', 'status': 'candidate', 'ring': roof, 'source': 'sam3-text-onnx'}]
    source = save_scene({**source, 'image_sha256': sha(source_png)}, source_png, tmp_path)
    target = z20_scene('house-4242')
    target_png = render(target, colour)
    moved = [list(p) for p in R.apply_affine(roof, R.frame_transform(source, target)['pixel_affine'])]
    target['objects'] = [{'id': 'h1', 'kind': 'building', 'status': 'candidate', 'ring': moved, 'source': 'sam3-text-onnx'}]
    target = save_scene({**target, 'image_sha256': sha(target_png)}, target_png, tmp_path)
    (tmp_path/'data/scenes/house-4242/review.json').write_text(json.dumps(
        {'model_signature': target['model_signature'], 'revision': 2, 'objects': [{**target['objects'][0], 'status': 'confirmed'}]}))
    row = R.pilot_pair(tmp_path, 'house', source['id'], target['id'], R.load_design(tmp_path))
    assert row['verified'] is True and row['overlap_fraction'] == 1.0 and row['shift_m'] < .06 and row['ncc'] > .6
    assert row['objects'][0]['iou'] == pytest.approx(1., abs=1e-6) and row['objects'][0]['matched_id'] == 'h1'
    assert row['objects'][0]['target_human_status'] == 'confirmed' and row['iou_median'] == pytest.approx(1., abs=1e-6)
    assert row['compares'] == 'model objects of the two frames'
    # the target never looked for driveways: nothing to agree with, so no IoU (not a 0 that reads as disagreement)
    assert row['objects'][1] == {'id': 'c2', 'kind': 'driveway', 'status': 'candidate', 'reason': 'kind_not_scanned_in_target'}
    assert row['not_comparable'] == 1 and row['matched_share'] == 1.0
    assert row['target_status_of_matched'] == {'confirmed': 1} and row['target_review_status_of_matched'] == {'confirmed': 1}
    # both figures are visible: the median over compared objects and the median with the unscanned kinds counted as 0
    assert row['iou_median_unscanned_as_zero'] == pytest.approx(.5, abs=1e-6)
    summary = R._group_summary([row], DESIGN)
    assert summary['iou_median'] == pytest.approx(1., abs=1e-6) and summary['iou_median_unscanned_as_zero'] == pytest.approx(.5, abs=1e-6)
    # the pilot's computed sentences read every number from the summary at write time
    assert R._computed_wording({'house': summary, 'mooroolbark': {**summary, 'pairs': 0}}) == [
        'house: the IoU median 1.0 is over the 1 of 2 cohort model objects of kinds the target scanned (of other kinds, with no '
        'IoU: 1; median 0.5 when they count as 0). Matched target objects with a saved status: 1; human decisions among them: 1 '
        "(confirmed 1); the rest are undecided candidates or not the reviewer's own."]
    # the signed residual says which way the new frame's content sits: planted 0.3 m east, 0.2 m south
    moved_png = render(target, colour, (.3, -.2))
    shifted = save_scene({**target, 'id': 'house-4243', 'image_sha256': sha(moved_png)}, moved_png, tmp_path)
    east, north = R.pilot_pair(tmp_path, 'house', source['id'], shifted['id'], R.load_design(tmp_path))['shift_east_north_m']
    assert east == pytest.approx(.3, abs=.04) and north == pytest.approx(-.2, abs=.04)


@pytest.mark.parametrize(('saved', 'human', 'authority'), [
    ({'status': 'confirmed'}, 'confirmed', None),
    ({'status': 'needs_adjustment'}, 'needs_adjustment', None),
    ({'status': 'rejected'}, 'rejected', None),
    ({'status': 'candidate'}, None, None),
    ({'status': 'superseded'}, None, None),
    ({'status': 'confirmed', 'provenance': {'source': 'machine:decision-policy-v2'}}, None, 'machine_label'),
    ({'status': 'confirmed', 'notes': 'Assistant visually traced from saved Google imagery on 2026-09-23.'}, None, 'agent_authored'),
    ({'status': 'rejected', 'fieldwork_import': {'pending': True}}, None, 'fieldwork_pending'),
])
def test_target_human_status_is_a_saved_human_decision_never_a_candidate(saved, human, authority):
    source, target = fitted_scene(), z20_scene()
    ft = R.frame_transform(source, target)
    ring = [[180, 180], [330, 180], [330, 300], [180, 300]]
    detection = {'id': 'h1', 'kind': 'building', 'status': 'candidate', 'ring': [list(p) for p in R.apply_affine(ring, ft['pixel_affine'])]}
    by_id = {'h1': {**detection, **saved}}
    decisions = R._by_kind(o for o in by_id.values() if R._decided(o))
    entry = R._compare({'id': 'c1', 'kind': 'building', 'status': 'confirmed', 'ring': ring}, ft, target,
                       R._by_kind([detection]), decisions, by_id, DESIGN, {'building'})
    assert entry['matched_id'] == 'h1' and entry['target_review_status'] == saved['status'], 'the raw saved status is kept'
    assert entry['target_human_status'] == human and entry['target_review_authority'] == authority
    assert ('decision_status' in entry) is (human is not None), 'only a human decision is a decision to agree with'


def cohort_stub(scene_id, east_m, objects=None):
    return {**fitted_scene(scene_id, east_m=east_m), 'objects': objects or []}


def test_pilot_pairs_list_every_reviewed_cohort_scene_they_leave_out_with_its_reason(tmp_path):
    def put(scene, review=None):
        folder = tmp_path/'data/scenes'/scene['id']
        folder.mkdir(parents=True)
        (folder/'latest.json').write_text(json.dumps(scene))
        if review is not None:
            (folder/'review.json').write_text(json.dumps({'revision': 1, 'objects': review}))
    put(z20_scene('mooroolbark-test-1'))
    ring = [[10, 10], [40, 10], [40, 40]]
    human = [{'id': 'a', 'kind': 'building', 'status': 'confirmed', 'ring': ring}]
    machine = [{'id': 'b', 'kind': 'building', 'status': 'confirmed', 'ring': ring, 'provenance': {'source': 'machine:decision-policy-v2'}},
               {'id': 'c', 'kind': 'tree', 'status': 'candidate', 'ring': ring}]
    put(cohort_stub('roof_training_250-1', 6.), human)                              # inside, decided: paired
    put(cohort_stub('roof_training_250-2', 500.), human)                            # decided, outside every frame
    put(cohort_stub('roof_training_250-3', 6.), machine)                            # inside, only a machine label
    put(cohort_stub('roof_training_250-4', 500.), [{**machine[1], 'id': 'd'}])       # neither
    put(cohort_stub('roof_training_250-5', 6.))                                     # no review.json: not a candidate
    pairs, selection = R.pilot_pairs(tmp_path)
    assert pairs[:8] == [('house', f'roof_training_250-{a}', f'house-{a}') for a in R.HOUSE_PAIRS]
    assert pairs[8:] == [('mooroolbark', 'roof_training_250-1', 'mooroolbark-test-1')]
    assert selection['excluded'] == [
        {'scene_id': 'roof_training_250-2', 'reason': 'outside_every_registered_frame', 'human_decision': True, 'inside_registered_frame': False},
        {'scene_id': 'roof_training_250-3', 'reason': 'no_human_decision', 'human_decision': False, 'inside_registered_frame': True},
        {'scene_id': 'roof_training_250-4', 'reason': 'no_human_decision', 'human_decision': False, 'inside_registered_frame': False}]
    assert (selection['cohort_scenes'], selection['with_review'], selection['with_human_decision'],
            selection['centroid_inside_registered_frame'], selection['mooroolbark_pairs']) == (5, 4, 2, 2, 1)
    assert selection['with_review'] == selection['mooroolbark_pairs']+len(selection['excluded'])


def test_signed_offset_reads_the_axes_of_the_target_frame_never_an_assumed_y_down():
    s = .118
    assert R.east_north([2, 3], {'pixel_to_meters': [[s, 0], [0, -s], [5, 5]]}) == [round(2*s, 4), round(-3*s, 4)]
    assert R.east_north([2, 3], {'pixel_to_meters': [[s, 0], [0, s], [5, 5]]}) == [round(2*s, 4), round(3*s, 4)]
    assert R.east_north([2, 3], {'pixel_to_meters': [[0, s], [s, 0], [5, 5]]}) == [round(3*s, 4), round(2*s, 4)]
    assert R.east_north(None, {'pixel_to_meters': [[s, 0], [0, -s], [5, 5]]}) is None
    # end to end on a frame whose pixel y points north: planted 0.3 m east and 0.2 m south stay east and south
    colour = world()
    source, target = fitted_scene(), z20_scene()
    p2m = np.asarray(target['pixel_to_meters'], float)
    p2m[2] += 1024*p2m[1]
    p2m[1] *= -1
    north_up = {**target, 'pixel_to_meters': p2m.tolist(), 'meters_to_pixel': inverse(p2m).tolist()}
    ft = R.frame_transform(source, north_up)
    measured = R.residual(render(source, colour), render(north_up, colour, (.3, -.2)), ft, spacing(north_up))
    assert measured['shift_px'][1] == pytest.approx(-.2/spacing(north_up), abs=.35), 'pixel y is north here'
    east, north = R.east_north(measured['shift_px'], north_up)
    assert east == pytest.approx(.3, abs=.04) and north == pytest.approx(-.2, abs=.04)


def test_pilot_wording_states_the_design_and_no_cause_for_the_shared_offset():
    assert 'median of up to two providers by design' in R.WORDING['cohort'] and 'two-provider' not in R.WORDING['cohort']
    assert 'its cause is not established here' in R.WORDING['signed'] and 'never corrected' in R.WORDING['signed']
    assert 'is a registration offset' not in R.WORDING['signed']
    assert all('accuracy' not in text or 'never accuracy' in text for text in R.WORDING.values())


# --- plan sentence wording ---------------------------------------------------------------------------------------

def sentence_plan(source_m_per_px, registration, zoom=20, source_native=None, lat=-37.7905):
    return {'address_id': 4242, 'span_m': 70.5, 'm_per_px': .1377, 'zoom': zoom, 'frame_px': 512, 'lots_label': 'Lot 1',
            'plan': 'PS1', 'native_m_per_px': L.native_m_per_px(lat, zoom), 'source_m_per_px': source_m_per_px,
            'source_registration': registration, 'source_zoom': 19 if registration == 'fitted' else zoom,
            'source_native_m_per_px': source_native or L.native_m_per_px(lat, 19), 'group_inside_source': False,
            'source_review_revision': None, 'source_scene_id': 'qa'}


@pytest.mark.parametrize('crop_m_per_px', [.047, .063, .085, .122, .224])
def test_a_z20_lot_frame_is_finer_in_native_detail_than_any_cohort_crop_never_coarser(crop_m_per_px):
    text = L.plan_sentence(sentence_plan(crop_m_per_px, 'fitted'))
    assert 'finer in native detail' in text and 'coarser' not in text
    assert f'this {crop_m_per_px:.3f} m/px crop' in text and 'two-provider' not in text
    assert 'upsample of a z19 composite (median of up to two providers by design; 0.236 m/px native)' in text


def test_native_detail_wording_for_registered_sources_and_a_z18_frame():
    same = L.plan_sentence(sentence_plan(.127, 'region', source_native=L.native_m_per_px(-37.7905, 20)))
    assert 'the same native detail' in same and 'coarser' not in same and 'finer' not in same
    z19 = L.plan_sentence(sentence_plan(.236, 'region', source_native=L.native_m_per_px(-37.7905, 19)))
    assert 'finer in native detail (z20, 0.118 m/px) than this 0.236 m/px frame' in z19
    wide = L.plan_sentence(sentence_plan(.063, 'fitted', zoom=18))
    assert 'coarser in native detail (z18, 0.472 m/px)' in wide, 'a z18 frame really is coarser than the z19 composite'


# --- subject recaptures: the absolute overlap floor (DESIGN.json subject_recapture, amendment of 2026-10-01) ------------

def _subject_transform(side_px, fraction=.2, shift_m=.1, ncc=.8, role=R.SUBJECT_ROLE):
    return {'role': role, 'overlap': {'fraction': fraction, 'source_px_inside_target': 1, 'region_px': {'side_px': side_px, 'area_px': int(side_px**2), 'eroded_by_px': R.EDGE_PX}},
            'residual': {'shift_m': shift_m, 'ncc': ncc, 'shift_px': [0., 0.], 'samples': int(side_px**2)}}


def test_subject_role_verdict_uses_the_px_floor_and_keeps_shift_and_ncc():
    # a house-centred window keeps 20 percent of the source frame but a 200 px region: verified under the floor
    assert R.verdict(_subject_transform(200.), DESIGN) == (True, [])
    # the same overlap with no role is a same-address recapture: the pre-registered fraction rule, unchanged
    ok, reasons = R.verdict(_subject_transform(200., role=None), DESIGN)
    assert not ok and reasons == ['overlap 0.200 < min_overlap_fraction 0.6']
    # below the floor: the reason names the floor and records the fraction beside it
    ok, reasons = R.verdict(_subject_transform(100.), DESIGN)
    assert not ok and reasons == ['overlap region side 100 px < min_overlap_px 128 (fraction 0.200)']
    assert R.verdict(_subject_transform(128.), DESIGN) == (True, []), 'the floor is inclusive'
    # shift and ncc keep their own reasons, whatever the role
    ok, reasons = R.verdict(_subject_transform(200., shift_m=1., ncc=.2), DESIGN)
    assert not ok and reasons == ['shift_m 1.000 > max_shift_m 0.7', 'ncc 0.200 < min_ncc 0.35']
    # a design with no pre-registered floor never verifies a subject recapture (and never falls back to the fraction)
    bare = {k: v for k, v in DESIGN.items() if k != 'subject_recapture'}
    ok, reasons = R.verdict(_subject_transform(400., fraction=1.), bare)
    assert not ok and reasons == ['subject_recapture.min_overlap_px is not pre-registered in DESIGN.json: a subject recapture has no overlap floor']
    # the role can also be given by the caller
    assert R.verdict(_subject_transform(200., role=None), DESIGN, role=R.SUBJECT_ROLE) == (True, [])
    # a transform without region_px (an older record) is unmeasured under the floor, not verified
    old = _subject_transform(200.)
    del old['overlap']['region_px']
    assert R.verdict(old, DESIGN) == (False, ['overlap region unmeasured (no region_px on the transform)'])


def test_load_design_validates_the_subject_floor_and_design_block_applies_it_by_role(tmp_path):
    write_design(tmp_path)
    design = R.load_design(tmp_path)
    assert R.subject_floor(design[0]) == SUBJECT
    assert R.design_block(design) == {'max_shift_m': .7, 'min_ncc': .35, 'min_overlap_fraction': .6, 'match_iou': .5, 'close_iou': .9,
                                      'design_sha256': design[1]}
    applied = R.design_block(design, R.SUBJECT_ROLE)
    assert applied['subject_recapture'] == SUBJECT and applied['design_sha256'] == design[1] == sha((tmp_path/R.DESIGN_PATH).read_bytes())
    for bad in ({'min_overlap_px': 0}, {'min_overlap_px': True}, {'min_overlap_px': 12.5}, 'x'):
        write_design(tmp_path, subject_recapture=bad)
        with pytest.raises(ValueError, match='subject_recapture'):
            R.load_design(tmp_path)
    bare = write_design(tmp_path, subject_recapture=None)
    assert bare.exists() and 'subject_recapture' not in json.loads(bare.read_text())
    design = R.load_design(tmp_path)
    assert R.subject_floor(design[0]) is None and R.design_block(design)['min_overlap_fraction'] == .6
    with pytest.raises(ValueError, match='subject_recapture'):
        R.design_block(design, R.SUBJECT_ROLE)


def test_a_measured_subject_transform_records_role_region_and_the_applied_design_and_verifies(tmp_path):
    """A window centred past the cut keeps about half the source frame (fraction below 0.6): unverified as a same-address
    recapture, verified as a subject recapture on the region floor; the verifier reads the role from the record."""
    write_design(tmp_path)
    colour = world()
    target = z20_scene('win-0123456789abcdef')
    east = target['region']['span_m']/2
    # a 51.2 m cohort-style frame whose image centre sits 0.6 m past the window's east edge: about 25 of its 51.2 m
    # (fraction 0.49) lie inside, a band of about 197 target px that erodes to about 177 px, above the 128 px floor
    source = fitted_scene(east_m=east+.2, north_m=.3, m_per_px=.1)
    source_png = render(source, colour)
    source = save_scene({**source, 'image_sha256': sha(source_png)}, source_png, tmp_path)
    target_png = render(target, colour)
    target['image_sha256'] = sha(target_png)
    design = R.load_design(tmp_path)
    same_address = R.measured_transform(source, target, source_png, target_png, design)
    assert same_address['role'] is None and same_address['verified'] is False
    assert R.verdict(same_address, design[0])[1] == [f"overlap {same_address['overlap']['fraction']:.3f} < min_overlap_fraction 0.6"]
    subject = R.measured_transform(source, target, source_png, target_png, design, role=R.SUBJECT_ROLE)
    assert subject['role'] == 'frame_cut_target' and subject['verified'] is True
    assert subject['overlap']['fraction'] == pytest.approx(25/51.2, abs=.02) and subject['overlap']['fraction'] < .6
    assert subject['overlap']['region_px']['side_px'] == pytest.approx(25/spacing(target)-2*R.EDGE_PX, abs=3)
    assert subject['overlap']['region_px']['side_px'] >= 128
    assert subject['design'] == {**R.design_block(design), 'subject_recapture': SUBJECT}
    assert subject['residual']['shift_m'] < .06 and subject['residual']['ncc'] > .6
    target['frame_transform'] = subject
    target = save_scene(target, target_png, tmp_path)
    ok, reasons, measured = R.verify(snapshot_path(tmp_path, source), snapshot_path(tmp_path, target), tmp_path)
    assert (ok, reasons) == (True, []) and measured['ncc'] == subject['residual']['ncc']
    assert R.main(verify_args(tmp_path, source, target)) == 0
    # the same bytes recorded without the role would be refused: the fraction rule is the record's own
    forged = copy.deepcopy(target)
    forged['frame_transform'].update(role=None, design=R.design_block(design))
    forged = save_scene(forged, target_png, tmp_path)
    ok, reasons, _ = R.verify(snapshot_path(tmp_path, source), snapshot_path(tmp_path, forged), tmp_path)
    assert not ok and any(r.startswith('overlap') for r in reasons), reasons
    # a design that loses its floor after the capture: re-measure (the recorded design differs)
    write_design(tmp_path, subject_recapture=None)
    ok, reasons, _ = R.verify(snapshot_path(tmp_path, source), snapshot_path(tmp_path, target), tmp_path)
    assert not ok and any('subject_recapture' in r and 'pre-registered' in r for r in reasons), reasons
    with pytest.raises(ValueError, match='subject_recapture'):
        R.measured_transform(source, target, source_png, target_png, R.load_design(tmp_path), role=R.SUBJECT_ROLE)


@pytest.mark.skipif(not PILOT.exists(), reason='the dated pilot file is not in this checkout')
def test_the_dated_pilot_verdicts_are_reproduced_for_three_pairs_under_the_amended_design():
    """Pure recomputation from pilot-2026-09-30.json (read only): verdict() on the recorded transform and overlap() on the
    recorded sizes and affine give the recorded verified/reasons/fraction, under the pilot's design values and under the
    current repo DESIGN.json (amended with subject_recapture): a same-address pair never reads the floor."""
    pilot = json.loads(PILOT.read_text())
    rows = {(r['source']['scene_id'], r['target']['scene_id']): r for r in pilot['rows']}
    chosen = [('roof_training_250-29772', 'house-29772'),                                    # house pair, fraction 1.0
              ('roof_training_250-25218', 'mooroolbark-50011498-17-118444-80418'),           # the smallest overlap region of the pilot
              ('roof_training_250-25136', 'mooroolbark-50011498-17-118444-80418')]           # a pair with fraction below 1
    current = R.load_design(REPO)
    assert R.subject_floor(current[0]) is not None, 'the repo design carries the subject floor'
    for key in chosen:
        row = rows[key]
        ft = row['frame_transform']
        assert 'role' not in ft and 'region_px' not in ft['overlap'], 'the pilot predates roles and regions'
        for values in (pilot['design'], current[0]):
            assert R.verdict(ft, values) == (row['verified'], row['reasons']), key
        source = {'image_width': ft['source_size'][0], 'image_height': ft['source_size'][1]}
        target = {'image_width': row['target']['size'][0], 'image_height': row['target']['size'][1]}
        again = R.overlap(source, target, ft['pixel_affine'])
        assert again['fraction'] == pytest.approx(ft['overlap']['fraction'], abs=1e-6), key
        assert again['source_px_inside_target'] == ft['overlap']['source_px_inside_target'], key
        # the region the floor would read bounds the samples the residual actually correlated on: its eroded area is at
        # least the kept samples, which are at least the square of its side shrunk by the residual's shift margin
        side, area = again['region_px']['side_px'], again['region_px']['area_px']
        margin = math.ceil(max(abs(v) for v in ft['residual']['shift_px']))+1
        assert area >= ft['residual']['samples'] >= (side-2*margin-1)**2, (key, side, area, ft['residual']['samples'])
    assert rows[chosen[1]]['frame_transform']['overlap']['fraction'] == 1.0 and rows[chosen[2]]['frame_transform']['overlap']['fraction'] < 1
    assert R.overlap({'image_width': 512, 'image_height': 512}, {'image_width': 1152, 'image_height': 1152},
                     rows[chosen[1]]['frame_transform']['pixel_affine'])['region_px']['side_px'] < 128, \
        'the smallest pilot region sits below the floor: the floor was set above every region the pilot measured'


# --- design sha through the dated amendment chain (scripts/ml/design_chain.py; 2026-10-01) ---------------------------

def test_design_chain_accepts_the_current_sha_and_ancestors_behind_thresholds_unchanged_amendments():
    """A record pins the WHOLE-FILE sha of DESIGN.json. A dated amendment that changes no threshold still moves that sha, so the
    accepted set is the current sha plus the previous_sha256 of every amendment reached from the newest entry backwards while
    thresholds_changed is exactly false; the walk stops at the first true or missing flag, so a record written before a
    THRESHOLD change stays refused, as does an unknown sha."""
    from scripts.ml import design_chain as C
    s0, s1, s2, s3 = (hashlib.sha256(bytes([i])).hexdigest() for i in range(4))
    values = {**DESIGN, 'amendments': [{'at': 't1', 'field': 'subject_recapture', 'previous_sha256': s0, 'thresholds_changed': True},
                                       {'at': 't2', 'field': 'counting_rule', 'previous_sha256': s1, 'thresholds_changed': False},
                                       {'at': 't3', 'field': 'identity', 'previous_sha256': s2, 'thresholds_changed': False}]}
    accepted = C.accepted_shas(values, s3)
    assert accepted == {s3: {'match': C.CURRENT, 'hops_back': 0},
                        s2: {'match': C.VIA_CHAIN, 'hops_back': 1, 'amendment_index': 3, 'amendment_at': 't3', 'amendment_field': 'identity'},
                        s1: {'match': C.VIA_CHAIN, 'hops_back': 2, 'amendment_index': 2, 'amendment_at': 't2', 'amendment_field': 'counting_rule'}}
    assert C.match(accepted, s3)['match'] == C.CURRENT and C.match(accepted, s1)['match'] == C.VIA_CHAIN
    assert C.match(accepted, s0) is None, 'behind a thresholds_changed true amendment: refused'
    assert C.match(accepted, 'f'*64) is None and C.match(accepted, None) is None and C.match({}, s3) is None
    # the walk never assumes false: a missing flag or a missing previous sha ends the chain at that entry
    assert set(C.accepted_shas({**values, 'amendments': [*values['amendments'], {'at': 't4', 'field': 'x'}]}, s3)) == {s3}
    assert set(C.accepted_shas({**values, 'amendments': [*values['amendments'], {'at': 't4', 'thresholds_changed': False}]}, s3)) == {s3}
    assert C.accepted_shas(DESIGN, s3) == {s3: {'match': C.CURRENT, 'hops_back': 0}} and C.accepted_shas({}, None) == {}
    # scene_recapture's wrapper reads a loaded (values, sha) design the same way; the shipped file accepts its own sha as current
    design = R.load_design(REPO)
    assert R.design_sha_accepted(design, design[1]) == {'match': C.CURRENT, 'hops_back': 0}
    assert R.design_sha_accepted(design, 'f'*64) is None


def test_verify_accepts_a_record_behind_a_thresholds_unchanged_amendment_and_refuses_a_threshold_change(tmp_path, capsys):
    source, target = saved_pair(tmp_path)
    path = tmp_path/R.DESIGN_PATH
    kept = path.read_bytes()
    before = sha(kept)
    assert target['frame_transform']['verified'] is True and target['frame_transform']['design']['design_sha256'] == before
    paths = (snapshot_path(tmp_path, source), snapshot_path(tmp_path, target))
    assert R.verify(*paths, tmp_path)[:2] == (True, [])
    # an identity amendment (no threshold changed) moves the file sha; the recorded sha is accepted through the chain
    identity = {**json.loads(kept), 'identity': 'win-<hash16>',
                'amendments': [{'at': '2026-10-01T08:00:00Z', 'field': 'identity', 'previous_sha256': before, 'thresholds_changed': False}]}
    path.write_text(json.dumps(identity, indent=1))
    after = sha(path.read_bytes())
    assert after != before
    assert R.verify(*paths, tmp_path)[:2] == (True, [])
    hop = {'match': 'design_sha_via_chain', 'hops_back': 1, 'amendment_index': 1, 'amendment_at': '2026-10-01T08:00:00Z',
           'amendment_field': 'identity'}
    assert R.design_sha_accepted(R.load_design(tmp_path), before) == hop
    assert R.main(verify_args(tmp_path, source, target)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['design_sha256'] == before and out['design_sha_match'] == hop and out['design_sha_current'] == after
    # the same entry marked as a threshold change refuses the record by name
    path.write_text(json.dumps({**identity, 'amendments': [{**identity['amendments'][0], 'thresholds_changed': True}]}, indent=1))
    ok, reasons, _ = R.verify(*paths, tmp_path)
    assert not ok and any('neither' in r and 'thresholds_changed' in r for r in reasons), reasons
    # a chain that never names the recorded sha refuses it too
    path.write_text(json.dumps({**identity, 'amendments': [{**identity['amendments'][0], 'previous_sha256': 'f'*64}]}, indent=1))
    assert R.verify(*paths, tmp_path)[0] is False
    # a value that moved behind an amendment claiming thresholds_changed false is still refused: values are compared beside the sha
    path.write_text(json.dumps({**identity, 'max_shift_m': .9}, indent=1))
    ok, reasons, _ = R.verify(*paths, tmp_path)
    assert not ok and any('recorded design values differ' in r for r in reasons), reasons
