import hashlib
import io
import json

import pytest
from PIL import Image
from scripts.ml import scene_house_batch as batch
from scripts.ml.scene_region import region_frame


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, 'model_fingerprint', lambda _: {'detector': 'frozen'})
    code = tmp_path/'scripts/ml'
    code.mkdir(parents=True)
    (code/'inference.py').write_text('frozen code')
    baseline = tmp_path/'data/scenes/baseline'
    baseline.mkdir(parents=True)
    (baseline/'latest.json').write_text('{"image_sha256":"image", "model_signature":"model"}')
    (baseline/'image.png').write_bytes(b'baseline image')
    (baseline/'review.json').write_text('{"revision":16}')
    checkpoint = batch.freeze_baseline('baseline', tmp_path)
    directory = batch.prepare_batch([1, 2], checkpoint, tmp_path)
    calls = []

    def capture(aid, **kwargs):
        calls.append(aid)
        image = Image.new('RGB', (512, 512), (aid, 100, 100))
        output = io.BytesIO()
        image.save(output, format='PNG')
        data = output.getvalue()
        scene = region_frame([145.312, -37.791, 145.3126, -37.7905])
        scene.update(id=f'house-{aid}', title=f'House {aid}', address_id=aid,
                     image_sha256=hashlib.sha256(data).hexdigest(), image_width=512, image_height=512)
        return scene, image, data

    def analyse(source, image, segmenter, **kwargs):
        assert kwargs['detector_only'] and kwargs['roof_models']
        return {**source, 'objects': [], 'analysis': {'input_geometry': 'imagery-only'}}

    monkeypatch.setattr(batch, 'analyse_scene', analyse)
    return tmp_path, checkpoint, directory, capture, calls


def test_new_house_batch_preserves_baseline_and_resumes_without_loading_detector(prepared):
    root, checkpoint, directory, capture, calls = prepared
    result = batch.run_batch(directory, root, segmenter_factory=object, source_factory=capture)
    assert result['counts'] == {'complete': 2} and calls == [1, 2]
    assert not list((root/'data/scenes').glob('house-*/review.json'))
    (root/'data/scenes/house-1/review.json').write_text('{"revision":1}')

    def forbidden():
        pytest.fail('A resume must not re-analyse published scenes')

    assert batch.run_batch(directory, root, segmenter_factory=forbidden)['counts'] == {'complete': 2}
    batch.verify_baseline(checkpoint, root)
    assert (root/'data/scenes/house-1/review.json').read_text() == '{"revision":1}'


@pytest.mark.parametrize('name', ['scripts/ml/inference.py', 'data/scenes/baseline/review.json'])
def test_checkpoint_rejects_live_code_or_reference_drift(prepared, name):
    root, checkpoint, directory, _, _ = prepared
    (root/name).write_text('changed')
    with pytest.raises(ValueError, match='Baseline or code changed'):
        batch.run_batch(directory, root)
    assert (checkpoint/'snapshot'/name).read_text() != 'changed'
    assert not (root/'data/scenes/house-1').exists()


def test_checkpoint_checks_archived_bytes_and_model_weights(prepared, monkeypatch):
    root, checkpoint, _, _, _ = prepared
    monkeypatch.setattr(batch, 'model_fingerprint', lambda _: {'detector': 'new weights'})
    with pytest.raises(ValueError, match='weights changed'):
        batch.verify_baseline(checkpoint, root)
    (checkpoint/'snapshot/scripts/ml/inference.py').write_text('changed archive')
    with pytest.raises(ValueError, match='snapshot changed'):
        batch.verify_baseline(checkpoint, root, weights=False)


def test_batch_preserves_new_review_even_if_it_arrives_during_inference(prepared, monkeypatch):
    root, _, directory, capture, _ = prepared
    analyse = batch.analyse_scene

    def concurrent_review(source, *args, **kwargs):
        folder = root/'data/scenes'/source['id']
        folder.mkdir()
        (folder/'review.json').write_text('{"revision":9}')
        return analyse(source, *args, **kwargs)

    monkeypatch.setattr(batch, 'analyse_scene', concurrent_review)
    result = batch.run_batch(directory, root, segmenter_factory=object, source_factory=capture)
    assert result['counts'] == {'preserved_existing': 2}
    assert not list((root/'data/scenes').glob('house-*/latest.json'))


def test_retry_uses_frozen_capture_and_rejects_corrupt_frame(prepared, monkeypatch):
    root, _, directory, capture, calls = prepared
    analyse = batch.analyse_scene

    def failed(*args, **kwargs):
        raise ValueError('No inference available')

    monkeypatch.setattr(batch, 'analyse_scene', failed)
    assert batch.run_batch(directory, root, segmenter_factory=object, source_factory=capture)['counts'] == {'failed': 2}
    source_path = directory/'house-1/source.json'
    source = json.loads(source_path.read_text())
    source['pixel_to_meters'][0][0] *= 2
    source_path.write_text(json.dumps(source))
    monkeypatch.setattr(batch, 'analyse_scene', analyse)
    result = batch.run_batch(directory, root, segmenter_factory=object, source_factory=capture)
    assert result['counts'] == {'failed': 1, 'complete': 1} and calls == [1, 2]
    assert 'source frame changed' in result['results']['house-1']['error']


def test_batch_does_not_publish_approved_inference(prepared, monkeypatch):
    root, _, directory, capture, _ = prepared

    def invalid(source, *args, **kwargs):
        return {**source, 'objects': [{'status': 'confirmed', 'geometry_reviewed': True}]}

    monkeypatch.setattr(batch, 'analyse_scene', invalid)
    result = batch.run_batch(directory, root, segmenter_factory=object, source_factory=capture)
    assert result['counts'] == {'failed': 2}
    assert not list((root/'data/scenes').glob('house-*/latest.json'))
