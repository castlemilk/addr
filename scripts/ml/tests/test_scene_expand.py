import copy
import hashlib
import io
from types import SimpleNamespace

import numpy as np
from PIL import Image
from scripts.ml.scene_expand import analyse_expansion, expanded_source, verify_preserved_pixels
from scripts.ml.scene_extract import polygon_details, save_scene
from scripts.ml.scene_region import capture_region


class Tiles:
    def _child(self, *args):
        return Image.new('RGB', (256, 256), (115, 103, 95))


def source_scene(root):
    scene, image, raw = capture_region([145.3121, -37.7908, 145.313, -37.7900], root=root, fetcher=Tiles())
    ring = [[20, 20], [120, 20], [120, 120], [20, 120]]
    scene['objects'] = [{'id': 'main-roof', 'kind': 'building', 'label': 'Main roof', 'ring': ring,
        'source': 'sam3-text-onnx', 'status': 'candidate', 'geometry_reviewed': False,
        'roof_line_edits': {'outline': {'ring': copy.deepcopy(ring), 'holes': []}, 'vertices': [[-10, 5], [-4, 5]], 'checked': False},
        'model': polygon_details(ring, image, np.asarray(scene['pixel_to_meters']), 'building')}]
    scene['scans'] = {'car': {'status': 'complete', 'count': 0, 'prompt': 'car'}}
    return save_scene(scene, raw, root), image, raw


def test_expansion_preserves_pixels_geography_feedback_binding_and_holds_split(tmp_path):
    source, image, raw = source_scene(tmp_path)
    before = copy.deepcopy(source)
    current, expanded, data = expanded_source(source, raw, root=tmp_path, fetcher=Tiles())
    assert source == before
    assert expanded.crop((256, 256, 768, 768)).tobytes() == image.tobytes()
    old = source['objects'][0]
    new = current['objects'][0]
    np.testing.assert_allclose(np.c_[old['ring'], np.ones(4)] @ source['pixel_to_meters'],
                               np.c_[new['ring'], np.ones(4)] @ current['pixel_to_meters'], atol=1e-10)
    assert new['roof_line_edits']['vertices'] == old['roof_line_edits']['vertices']
    assert new['roof_line_edits']['outline']['ring'] == new['ring']
    assert current['centroid'] == source['centroid']
    assert current['region']['split'] == 'unassigned'
    assert current['context_expansion']['source_model_signature'] == source['model_signature']
    saved = save_scene(current, data, tmp_path)
    directory = tmp_path/'data/scenes'/saved['id']
    assert verify_preserved_pixels(source, saved, directory)
    expanded.putpixel((300, 300), (255, 0, 0))
    output = io.BytesIO()
    expanded.save(output, format='PNG')
    digest = hashlib.sha256(output.getvalue()).hexdigest()
    (directory/f'{digest}.png').write_bytes(output.getvalue())
    assert not verify_preserved_pixels(source, {**saved, 'image_sha256': digest}, directory), 'Metadata alone cannot authorize feedback recovery'


def test_surrounding_roof_is_proposed_by_detector_and_original_roof_keeps_identity(tmp_path):
    source, _, raw = source_scene(tmp_path)
    class Detector:
        def encode_image(self, image):
            assert image.size == (1024, 1024)
            return None

        def proposals(self, features, prompt, size, **kwargs):
            assert prompt == 'building roof'
            mask = np.zeros((size[1], size[0]), dtype=bool)
            mask[320:400, 100:180] = True  # Detached roof outside the old image.
            return [SimpleNamespace(mask=mask, score=.92)]
    current, _, _ = analyse_expansion(source, raw, Detector(), root=tmp_path, kinds=['building'], fetcher=Tiles())
    assert len(current['objects']) == 2
    assert current['objects'][0]['id'] == 'main-roof'
    garage = current['objects'][1]
    assert garage['source'] == 'sam3-text-onnx' and garage['status'] == 'candidate'
    assert not garage['geometry_reviewed'] and 'roof_line_edits' not in garage
    assert max(p[0] for p in garage['ring']) < 256
    assert current['scans']['building']['status'] == 'complete'
    assert current['scans']['car']['status'] == 'partial'
    assert not (tmp_path/'data/scenes'/source['id']/'review.json').exists()


def test_clipped_tree_seed_is_replaced_by_the_fuller_canopy_seen_in_the_padded_frame(tmp_path):
    # A canopy cut by the old 512 px frame edge is a model seed, not a reviewer correction: when the padded frame
    # shows the whole canopy the fuller detection takes over and records which seed it replaces. Buildings were
    # already handled this way; trees and every other class must be too (house-25822 kept two half trees).
    source, image, raw = source_scene(tmp_path)
    clipped = [[400, 400], [512, 400], [512, 512], [400, 512]]
    lonely = [[0, 300], [40, 300], [40, 340], [0, 340]]   # also cut by the old frame, but the detector sees nothing fuller
    for oid, ring in (('half-tree', clipped), ('lonely-tree', lonely)):
        source['objects'].append({'id': oid, 'kind': 'tree', 'label': 'Tree', 'ring': ring, 'source': 'sam3-text-onnx',
            'status': 'candidate', 'geometry_reviewed': False,
            'model': polygon_details(ring, image, np.asarray(source['pixel_to_meters']), 'tree')})
    class Detector:
        def encode_image(self, image):
            return None

        def proposals(self, features, prompt, size, **kwargs):
            assert prompt == 'tree canopy'
            mask = np.zeros((size[1], size[0]), dtype=bool)
            mask[640:820, 640:820] = True  # the whole canopy, straddling the old frame line at 768
            return [SimpleNamespace(mask=mask, score=.9)]
    current, _, _ = analyse_expansion(source, raw, Detector(), root=tmp_path, kinds=['tree'], fetcher=Tiles())
    trees = {o['id']: o for o in current['objects'] if o['kind'] == 'tree'}
    roof, = [o for o in current['objects'] if o['kind'] == 'building']
    assert roof['id'] == 'main-roof' and 'clipped_by_frame' not in roof
    assert 'half-tree' not in trees, 'the clipped seed is not kept beside the fuller canopy'
    fuller, = [o for o in trees.values() if o.get('replaces')]
    assert fuller['replaces'] == ['half-tree'] and fuller['status'] == 'candidate' and 'clipped_by_frame' not in fuller
    xs = [p[0] for p in fuller['ring']]
    assert min(xs) < 768 < max(xs), 'the replacement crosses the old frame line'
    # the seed nobody completed stays, shifted, and says which frame cut it
    assert trees['lonely-tree']['clipped_by_frame'] == [256, 256, 768, 768]
    assert trees['lonely-tree']['ring'] == [[256, 556], [296, 556], [296, 596], [256, 596]]
