import fcntl
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scripts.ml import paired_compare, sam3, scene_isolated, scene_property_roofs
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.scene_extract import analyse_scene, save_scene
from scripts.ml.scene_region import region_frame
from scripts.ml.scene_suburb import ownership, tile_polygon
from scripts.ml.tiles import lonlat_to_tile_xy

SID = 'mooroolbark-fixture-17-1-1'


class Segmenter:
    """Fixed detector: one roof mask for the building prompt, nothing else."""

    def __init__(self, mask):
        self.mask, self.encoded = mask, []

    def encode_image(self, image):
        self.encoded.append(np.asarray(image).copy())
        return None

    def proposals(self, features, prompt, image_size, threshold=.4, max_proposals=128):
        return [Sam3Proposal(self.mask, .9, (100., 100., 220., 180.))] if prompt == 'building roof' else []


def fixture_image():
    pixels = np.full((512, 512, 3), [86, 118, 70], dtype=np.uint8)
    pixels[100:180, 100:160] = [205, 200, 196]
    pixels[100:180, 160:220] = [150, 146, 144]
    mask = np.zeros((512, 512), dtype=bool)
    mask[100:180, 100:220] = True
    return Image.fromarray(pixels), mask


def build_repo(tmp_path, roof_models=True):
    """A production-shaped repo whose baseline came from the scan's own path."""
    repo = tmp_path/'repo'
    image, mask = fixture_image()
    out = io.BytesIO()
    image.save(out, format='PNG')
    data = out.getvalue()
    frame = region_frame([145.31, -37.7905, 145.311, -37.7895])
    tile = [17, *map(int, lonlat_to_tile_xy(frame['centroid']['lon'], frame['centroid']['lat'], 17))]
    source = {**frame, 'id': SID, 'title': 'Isolated fixture', 'image_width': 512, 'image_height': 512,
              'image_sha256': hashlib.sha256(data).hexdigest(), 'objects': [], 'groups': [], 'scans': {},
              'analysis_tile': tile, 'suburb_run': 'fixture'}
    prediction = analyse_scene(source, image, Segmenter(mask), kinds=['building'], detector_only=True,
                               roof_models=roof_models, root=repo, parcel_data=[])
    ownership(prediction, tile, tile_polygon(*tile))
    baseline = save_scene(prediction, data, repo)
    (repo/'data/scenes/.extraction.lock').touch()
    return repo, baseline, mask


def snapshot(folder):
    if not folder.exists():
        return None
    return {str(p.relative_to(folder)): (p.is_dir(), p.stat().st_mtime_ns, None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob('*'))}


@pytest.fixture
def no_network(monkeypatch):
    calls = []
    def fetch(bounds):
        calls.append(bounds)
        return []
    monkeypatch.setattr(scene_property_roofs, 'fetch_parcels', fetch)
    return calls


@pytest.fixture
def no_sam3(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('SAM3 must not be constructed when a segmenter is supplied')
    monkeypatch.setattr(sam3, 'Sam3TextSegmenter', refuse)


def test_null_experiment_is_isolated_and_reproduces_the_baseline(tmp_path, monkeypatch, no_network, no_sam3):
    repo, baseline, mask = build_repo(tmp_path)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    out = tmp_path/'isolated'
    before = snapshot(repo/'data/scenes')
    record = scene_isolated.run(SID, out, segmenter=Segmenter(mask))
    assert snapshot(repo/'data/scenes') == before, 'Nothing is written under the production data/scenes'
    assert not (repo/'data/suburbs').exists() and not (repo/'data/scene-parcels').exists()
    folder = out/'data/scenes'/SID
    candidate = json.loads((folder/'latest.json').read_text())
    assert record['candidate_model_signature'] == candidate['model_signature'] == baseline['model_signature']
    assert record['baseline_model_signature'] == baseline['model_signature'] and record['signature_reproduced'] is True
    assert candidate['owned_object_ids'] == baseline['owned_object_ids'], 'Scan ownership is re-applied like scene_suburb'
    base_folder = out/'baseline/data/scenes'/SID
    png = f"{baseline['image_sha256']}.png"
    assert (base_folder/'latest.json').read_bytes() == (repo/'data/scenes'/SID/'latest.json').read_bytes()
    assert (base_folder/png).read_bytes() == (folder/png).read_bytes() == (repo/'data/scenes'/SID/png).read_bytes()
    assert (folder/'confidence.json').exists() and (out/'data/suburbs/mooroolbark/confidence.sqlite').exists()
    listed = json.loads((folder/'candidate-objects.json').read_text())
    assert [o['object_id'] for o in listed['objects']] == [o['id'] for o in candidate['objects']]
    assert list((out/'data/scene-parcels').glob('*.json')) and len(no_network) == 1, 'Parcels land under out_root'
    run = json.loads((out/'isolated-run.json').read_text())
    assert run == json.loads((folder/'isolated-run.json').read_text())
    assert {'scene_id', 'baseline_model_signature', 'candidate_model_signature', 'code_sha256', 'elapsed', 'variables'} <= set(run)
    assert run['code_sha256']['scene_isolated.py'] == hashlib.sha256(Path(scene_isolated.__file__).read_bytes()).hexdigest()
    assert run['variables']['detector_only'] is True and run['variables']['roof_models'] is True
    assert run['variables']['kinds'] == ['building'] and run['variables']['parcel_source'] == 'fetched-into-out-root'
    assert run['variables']['image_variant'] is None and run['variables']['evidence_views'] is None
    report = paired_compare.compare(out/'baseline', out, [SID])
    owned = [o for o in baseline['objects'] if o['kind'] == 'building' and o['id'] in baseline['owned_object_ids']]
    assert owned and report['counts'] == {'paired': len(owned), 'gained': 0, 'lost': 0}
    # Both sides are assessed by the same code, so a null experiment keeps every band.
    assert run['baseline_confidence']['status'] == 'reassessed' and run['baseline_confidence']['production'] is None
    matrix = report['band_transition_matrix']
    assert sum(sum(v.values()) for v in matrix.values()) == len(owned)
    assert all(after == before and before != 'missing' for before, row in matrix.items() for after in row)


def test_production_confidence_is_kept_and_band_drift_recorded(tmp_path, monkeypatch, no_network):
    repo, baseline, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    roof = next(o for o in baseline['objects'] if o['kind'] == 'building')
    production = {'version': 'roof-evidence-v1', 'model_signature': baseline['model_signature'],
                  'objects': [{'object_id': roof['id'], 'band': 'high'}]}
    (repo/'data/scenes'/SID/'confidence.json').write_text(json.dumps(production))
    before = snapshot(repo/'data/scenes')
    record = scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask), roof_models=False)
    assert snapshot(repo/'data/scenes') == before
    folder = tmp_path/'isolated/baseline/data/scenes'/SID
    assert json.loads((folder/'confidence.production.json').read_text()) == production
    reassessed = json.loads((folder/'confidence.json').read_text())
    assert reassessed['model_signature'] == baseline['model_signature']
    drift = record['baseline_confidence']
    assert drift['production']['sha256'] == hashlib.sha256((folder/'confidence.production.json').read_bytes()).hexdigest()
    assert drift['production']['compared'] == 1
    assert drift['production']['band_changes'] == (0 if reassessed['objects'][0]['band'] == 'high' else 1)


@pytest.mark.parametrize('bad', ['.', 'relative/out', 'REPO', 'REPO/data/scenes/x', 'REPO/data/scenes', 'REPO/data/other'])
def test_out_root_must_stay_out_of_production(tmp_path, monkeypatch, bad):
    repo = tmp_path/'repo'
    (repo/'data/scenes').mkdir(parents=True)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    path = bad if not bad.startswith('REPO') else str(repo)+bad[4:]
    with pytest.raises(ValueError):
        scene_isolated.validate_out_root(path)
    with pytest.raises(ValueError):
        scene_isolated.run(SID, path, segmenter=object())
    assert scene_isolated.validate_out_root(repo/'data/reviews/eyes/isolated/x') == (repo/'data/reviews/eyes/isolated/x').resolve()
    assert scene_isolated.validate_out_root(tmp_path/'elsewhere') == (tmp_path/'elsewhere').resolve()


def test_busy_extraction_lock_exits_75(tmp_path, monkeypatch, no_network):
    repo, _, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    out = tmp_path/'isolated'
    with (repo/'data/scenes/.extraction.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(scene_isolated.ExtractionBusy) as busy:
            scene_isolated.run(SID, out, segmenter=Segmenter(mask))
        assert busy.value.code == 75
        assert scene_isolated.main([SID, '--out-root', str(out)]) == 75
    assert not out.exists(), 'A refused run writes nothing'


def test_changed_source_png_is_refused(tmp_path, monkeypatch, no_network):
    repo, baseline, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    (repo/'data/scenes'/SID/f"{baseline['image_sha256']}.png").write_bytes(b'changed')
    with pytest.raises(ValueError, match='imagery changed'):
        scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask))
    assert not (tmp_path/'isolated/data/scenes').exists()


def test_image_variant_feeds_only_the_whole_frame_detector(tmp_path, monkeypatch, no_network):
    repo, baseline, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    fake = Segmenter(mask)
    monkeypatch.setattr(sam3, 'Sam3TextSegmenter', lambda path: fake)  # lazily built only when none is supplied
    original = np.asarray(Image.open(repo/'data/scenes'/SID/f"{baseline['image_sha256']}.png").convert('RGB'))
    variant = (255 - original).astype(np.uint8)
    record = scene_isolated.run(SID, tmp_path/'isolated', roof_models=False, image_variant=variant,
                                evidence_views=['clahe'], parcel_data=[])
    assert np.array_equal(fake.encoded[0], variant), 'The detector sees the variant'
    candidate = json.loads((tmp_path/'isolated/data/scenes'/SID/'latest.json').read_text())
    colours = {o['id']: o['model']['color'] for o in baseline['objects']}
    assert {o['id']: o['model']['color'] for o in candidate['objects']} == colours, 'Non-detector pixels stay the saved PNG'
    variables = record['variables']
    assert variables['image_variant']['sha256'] == hashlib.sha256(variant.tobytes()).hexdigest()
    assert variables['image_variant']['shape'] == [512, 512, 3] and variables['detector_frame_substitutions'] == 1
    assert variables['evidence_views']['applied'] is False and variables['parcel_source'] == 'caller-none'
    assert any('evidence_views' in w for w in record['warnings'])
    assert not no_network
    with pytest.raises(ValueError, match='HxWx3'):
        scene_isolated.run(SID, tmp_path/'isolated-2', image_variant=variant[:, :, 0], segmenter=fake, roof_models=False)


def test_frozen_parcels_are_read_not_fetched(tmp_path, monkeypatch):
    from scripts.ml.scene_batch import signature
    repo, baseline, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    monkeypatch.setattr(scene_property_roofs, 'fetch_parcels', lambda bounds: pytest.fail('network fetch'))
    key = signature([baseline['image_sha256'], baseline['region']['bounds'], baseline['centroid'], baseline['pixel_to_meters']])
    (repo/'data/scene-parcels').mkdir(parents=True)
    (repo/'data/scene-parcels'/f'{key}.json').write_text(json.dumps({'key': key, 'sha256': signature([]), 'data': []}))
    before = snapshot(repo/'data/scene-parcels')
    record = scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask), roof_models=False)
    assert record['variables']['parcel_source'] == 'frozen-repo-cache'
    assert snapshot(repo/'data/scene-parcels') == before
    assert not (tmp_path/'isolated/data/scene-parcels').exists()


def test_signature_drift_is_diagnosed_not_hidden(tmp_path, monkeypatch, no_network, no_sam3):
    """Scan tiles carry analysis.inference_profile from scene_suburb; analyse_scene never sets it."""
    from scripts.ml.scene_confidence import profile
    repo, baseline, mask = build_repo(tmp_path, roof_models=False)
    scene = json.loads((repo/'data/scenes'/SID/'latest.json').read_text())
    scene['analysis']['inference_profile'] = 'frozen-plan-signature'
    frozen = save_scene(scene, (repo/'data/scenes'/SID/f"{baseline['image_sha256']}.png").read_bytes(), repo)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    record = scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask), roof_models=False)
    assert record['signature_reproduced'] is False
    diff = record['baseline_diff']
    assert diff['analysis']['only_in_baseline'] == ['inference_profile'] and diff['analysis']['only_in_candidate'] == []
    assert diff['top_level']['changed'] == ['analysis'] and diff['top_level']['only_in_baseline'] == []
    assert diff['objects'] == {'baseline': len(frozen['objects']), 'candidate': len(frozen['objects']), 'shared_ids': len(frozen['objects'])}
    assert record['baseline_profile'] == profile(frozen) and record['candidate_profile'] != record['baseline_profile']
    report = paired_compare.compare(tmp_path/'isolated/baseline', tmp_path/'isolated', [SID])
    assert report['scenes'][0]['profile_before'] == record['baseline_profile']
    assert any('profile' in line for line in report['cannot_claim'])


def test_cli_can_refuse_parcel_fetch_for_offline_determinism(tmp_path, monkeypatch, no_network):
    repo, _, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    monkeypatch.setattr(sam3, 'Sam3TextSegmenter', lambda path: Segmenter(mask))
    out = tmp_path/'isolated'
    assert scene_isolated.main([SID, '--out-root', str(out), '--no-roof-models', '--no-parcel-fetch']) == 0
    record = json.loads((out/'isolated-run.json').read_text())
    assert record['variables']['parcel_source'] == 'caller-none' and record['variables']['roof_models'] is False
    assert not no_network and not (out/'data/scene-parcels').exists()


def test_assess_failures_are_recorded_after_the_candidate_is_saved(tmp_path, monkeypatch, no_network):
    from scripts.ml import scene_confidence
    repo, _, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    def broken(scene_id, root=Path('.')):
        raise TypeError('unexpected audit failure')
    monkeypatch.setattr(scene_confidence, 'assess', broken)
    record = scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask), roof_models=False)
    assert (tmp_path/'isolated/data/scenes'/SID/'latest.json').exists()
    assert record['assess_error'] == 'unexpected audit failure'
    assert record['baseline_confidence'] == {'status': 'failed', 'error': 'unexpected audit failure', 'production': None}
    listed = json.loads((tmp_path/'isolated/data/scenes'/SID/'candidate-objects.json').read_text())
    assert listed['objects'] and all(o['band'] is None for o in listed['objects'])


# --- CP3: evidence views thread run -> analyse_scene -> add_roof_estimates -------------------------

def test_evidence_views_are_applied_through_add_roof_estimates(tmp_path, monkeypatch, no_network, no_sam3):
    from scripts.ml import roof_line_fit, scene_extract
    repo, baseline, mask = build_repo(tmp_path)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    monkeypatch.chdir(repo)
    view = np.full((512, 512), 90, np.uint8)
    view[140:] = 200
    views = [{'name': 'stack-median', 'array': view, 'stack_id': 'stack-fixture'}]
    forwarded = []
    original = scene_extract.analyse_scene

    def spy(*args, **kwargs):
        forwarded.append(kwargs.get('evidence_views'))
        return original(*args, **kwargs)
    monkeypatch.setattr(scene_isolated, 'analyse_scene', spy)
    record = scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask), evidence_views=views, parcel_data=[])
    assert forwarded == [views]
    candidate = json.loads((tmp_path/'isolated/data/scenes'/SID/'latest.json').read_text())
    analysis = candidate['analysis']
    assert analysis['pipeline'] == 'sam3-scene-roof-v12+multiview'
    assert analysis['evidence_views'] == [{'name': 'stack-median', 'sha256': hashlib.sha256(view.tobytes()).hexdigest(),
                                           'stack_id': 'stack-fixture'}]
    assert analysis['extra_views_version'] == roof_line_fit.EXTRA_VIEWS_VERSION
    variables = record['variables']['evidence_views']
    assert variables['applied'] is True and variables['requested'] == 1
    assert variables['views'] == analysis['evidence_views']
    assert not any('evidence_views' in w for w in record['warnings'])
    assert record['signature_reproduced'] is False, 'the declared variable changes the analysis record'
    assert record['baseline_diff']['analysis']['only_in_candidate'] == ['evidence_views', 'extra_views_version']
    assert record['baseline_diff']['analysis']['changed'] == ['pipeline']


def test_evidence_views_without_roof_models_are_not_applied_and_bad_views_fail_early(tmp_path, monkeypatch, no_network, no_sam3):
    repo, _, mask = build_repo(tmp_path, roof_models=False)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    view = np.zeros((512, 512), np.uint8)
    record = scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask), roof_models=False,
                                evidence_views=[('stack-median', view)], parcel_data=[])
    assert record['variables']['evidence_views']['applied'] is False
    assert any('roof_models' in w for w in record['warnings'])
    with pytest.raises(ValueError, match='evidence_views'):
        scene_isolated.run(SID, tmp_path/'isolated-2', segmenter=Segmenter(mask), evidence_views=['clahe'], parcel_data=[])
    assert not (tmp_path/'isolated-2').exists(), 'invalid views are refused before any output is written'


def test_evidence_views_must_be_a_list_of_views(tmp_path, monkeypatch, no_network, no_sam3):
    repo, _, mask = build_repo(tmp_path)
    monkeypatch.setattr(scene_isolated, 'REPO', repo)
    with pytest.raises(ValueError, match='evidence_views'):
        scene_isolated.evidence_view_kwargs(5)
    with pytest.raises(ValueError, match='evidence_views'):
        scene_isolated.run(SID, tmp_path/'isolated', segmenter=Segmenter(mask), evidence_views=5, parcel_data=[])
    assert not (tmp_path/'isolated').exists(), 'refused before any output is written'
