"""Isolated re-inference of one saved scene into a separate output root.

Identical input bytes (the saved PNG, sha-verified), one declared variable, and
no production writes: nothing is written under the repository's data/scenes.
Predictions, parcels, confidence.json and the sqlite index land under out_root.
Candidate outputs are paired-comparison diagnostics, not labels/accuracy.

Layout under out_root:
  baseline/data/scenes/<id>/  byte copies of production latest.json and PNG; the
                              baseline is re-assessed with the current code
                              (confidence.json) and production's copy is kept as
                              confidence.production.json (the baseline root for
                              paired_compare, so both sides share one assess code)
  data/scenes/<id>/           the source PNG copy, then the candidate latest.json,
                              models/, confidence.json, candidate-objects.json
                              (every object; the index holds owned roofs only)
  data/suburbs/mooroolbark/confidence.sqlite, data/scene-parcels/ (fetch fallback)
  isolated-run.json           latest run record (also stored per scene)

image_variant: an HxWx3 uint8 array registered to the saved PNG. It replaces
only the whole-frame detector encode (segmenter.encode_image); surface
refinement, colours, roof-line evidence and lot-search crops keep the PNG.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import inspect
import io
import json
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

from .scene_extract import PROMPTS, analyse_scene, save_scene

VERSION = 'scene-isolated-v1'
EXIT_BUSY = 75
REPO = Path(__file__).resolve().parents[2]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class ExtractionBusy(RuntimeError):
    """Another extraction holds data/scenes/.extraction.lock (exit code 75, as scene_extract)."""
    code = EXIT_BUSY


def validate_out_root(out_root, repo=None):
    """Absolute, not the repo root, not in data/scenes; inside the repo only under data/reviews/eyes."""
    repo = Path(repo or REPO).resolve()
    if out_root is None or str(out_root) in ('', '.') or not Path(out_root).is_absolute():
        raise ValueError('Choose an absolute out_root; relative paths resolve into production.')
    target = Path(out_root).resolve()
    scenes = (repo/'data/scenes').resolve()
    if target == repo or target.is_relative_to(scenes) or (target/'data/scenes').resolve() == scenes:
        raise ValueError('out_root must not be the repository root or inside data/scenes.')
    if target.is_relative_to(repo) and not target.is_relative_to((repo/'data/reviews/eyes').resolve()):
        raise ValueError('Inside the repository, isolated runs write only under data/reviews/eyes.')
    return target


def code_hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))}


class VariantSegmenter:
    """Whole-frame detector encode sees the variant; crops and everything else keep the PNG."""

    def __init__(self, segmenter, original, variant):
        self._segmenter, self._original, self._variant = segmenter, original, variant
        self.substituted = 0

    def __getattr__(self, name):
        return getattr(self._segmenter, name)

    def encode_image(self, image):
        if image is self._original:
            self.substituted += 1
            return self._segmenter.encode_image(self._variant)
        return self._segmenter.encode_image(image)

    def proposals(self, *args, **kwargs):
        return self._segmenter.proposals(*args, **kwargs)


def variant_image(array, image):
    data = np.ascontiguousarray(np.asarray(array))
    if data.shape != (image.height, image.width, 3) or data.dtype != np.uint8:
        raise ValueError('image_variant must be an HxWx3 uint8 array matching the saved PNG.')
    return Image.fromarray(data), {'sha256': hashlib.sha256(data.tobytes()).hexdigest(), 'shape': list(data.shape), 'dtype': 'uint8'}


def evidence_view_kwargs(evidence_views, image=None, *, roof_models=True):
    """evidence_views -> analyse_scene(evidence_views=...) -> add_roof_estimates(evidence_views=...).

    Views ([{name, array, stack_id}] or (name, array[, stack_id]) tuples) only reach the roof
    fitter when roof_models is set; otherwise nothing is passed and the record says
    applied=False so a candidate is never labelled as an extra-views experiment it did not
    perform. Arrays are validated against the saved PNG grid up front (image given).
    """
    if evidence_views is None:
        return {}, None
    if not isinstance(evidence_views, (list, tuple)):
        raise ValueError('evidence_views must be a list of {name, array, stack_id} views')
    from .scene_house import add_roof_estimates, normalise_evidence_views
    supported = 'evidence_views' in inspect.signature(add_roof_estimates).parameters
    record = {'requested': len(evidence_views), 'add_roof_estimates_accepts': supported, 'applied': False,
              'hook': 'scene_isolated.evidence_view_kwargs'}
    if not roof_models or not supported:
        return {}, record
    if image is not None:
        _, provenance = normalise_evidence_views(evidence_views, image)
        record['views'] = provenance
    return {'evidence_views': evidence_views}, {**record, 'applied': True}


def frozen_parcels(scene, repo):
    """Reuse the production frozen parcel lookup read-only so both sides see identical cadastral input."""
    from .scene_batch import signature
    bounds = (scene.get('region') or {}).get('bounds')
    if not bounds:
        return None, 'no-region'
    key = signature([scene['image_sha256'], bounds, scene.get('centroid'), scene['pixel_to_meters']])
    path = repo/'data/scene-parcels'/f'{key}.json'
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get('key') == key and saved.get('sha256') == signature(saved.get('data')):
            return saved['data'], 'frozen-repo-cache'
    output = scene.get('property_roofs') or {}
    if output.get('parcels') and (output.get('inputs') or {}).get('parcels') == signature(output['parcels']):
        return output['parcels'], 'baseline-scene'
    return None, 'fetched-into-out-root'


def reapply_ownership(prediction):
    """Scan tiles: scene_suburb assigns owners after inference; repeat it with the saved core."""
    tile, core = prediction.get('analysis_tile'), prediction.get('analysis_core')
    if not tile or not core:
        return False
    from shapely.geometry import shape

    from .scene_suburb import ownership
    ownership(prediction, tuple(tile), shape(core))
    # The saved core already is tile ∩ suburb boundary; re-intersecting only reorders vertices.
    prediction['analysis_core'] = core
    return True


def signature_diff(baseline, candidate):
    """Which keys separate two model files; explains a non-reproduced model_signature.

    Scan tiles carry analysis.inference_profile from scene_suburb.run and older
    scenes may lack property_roofs; analyse_scene sets neither the same way.
    """
    skip = {'objects', 'groups', 'model_signature'}
    def keys(a, b):
        return {'only_in_baseline': sorted(set(a) - set(b)), 'only_in_candidate': sorted(set(b) - set(a)),
                'changed': sorted(k for k in set(a) & set(b) if a[k] != b[k])}
    ids_b, ids_a = ({o['id'] for o in scene.get('objects', [])} for scene in (baseline, candidate))
    return {'top_level': keys({k: v for k, v in baseline.items() if k not in skip}, {k: v for k, v in candidate.items() if k not in skip}),
            'analysis': keys(baseline.get('analysis') or {}, candidate.get('analysis') or {}),
            'objects': {'baseline': len(ids_b), 'candidate': len(ids_a), 'shared_ids': len(ids_b & ids_a)}}


def reassess_baseline(scene_id, baseline_root, assess):
    """Band the baseline copy with today's audit code; report drift from production's bands."""
    folder = baseline_root/'data/scenes'/scene_id
    result = {'status': 'reassessed', 'error': None, 'production': None}
    try:
        rows = {r['object_id']: r['band'] for r in assess(scene_id, root=baseline_root)['objects']}
    except Exception as error:  # The candidate is already saved; record the audit failure instead of losing the run.
        return {**result, 'status': 'failed', 'error': str(error)[:500]}
    production = folder/'confidence.production.json'
    if production.exists():
        data = production.read_bytes()
        report = json.loads(data)
        before = {r['object_id']: r.get('band') for r in report.get('objects', [])}
        shared = sorted(before.keys() & rows.keys())
        result['production'] = {'sha256': hashlib.sha256(data).hexdigest(), 'version': report.get('version'),
                                'same_model_signature': report.get('model_signature') == json.loads((folder/'latest.json').read_text()).get('model_signature'),
                                'compared': len(shared), 'band_changes': sum(before[k] != rows[k] for k in shared),
                                'note': 'Band changes here are assess-code drift, not the experiment variable.'}
    return result


def _atomic(path, value):
    from .scene_training import atomic_json
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


def run(scene_id, out_root, *, detector_only=True, roof_models=True, evidence_views=None, image_variant=None,
        segmenter=None, parcel_data=None, kinds=None, repo=None):
    from .scene_confidence import assess, profile
    from .scene_self_review import validate_id

    started, stamp = time.monotonic(), datetime.now(UTC).isoformat()
    repo = Path(repo or REPO).resolve()
    out = validate_out_root(out_root, repo)
    folder = repo/'data/scenes'/validate_id(scene_id)
    if not (folder/'latest.json').exists():
        raise FileNotFoundError(f'No saved scene {scene_id}')
    view_kwargs, views = evidence_view_kwargs(evidence_views, roof_models=roof_models)
    warnings = []
    if views and not views['applied']:
        warnings.append('evidence_views requested but not applied: roof_models is off, so add_roof_estimates never runs.')
    if roof_models and Path.cwd().resolve() != repo:
        warnings.append('add_roof_estimates resolves scripts/ml/models/roof-form.json from the working directory, not the repo root.')
    # Same mutex as scene_extract/API model loads; opening in append mode writes nothing.
    with (repo/'data/scenes/.extraction.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ExtractionBusy('Another scene extraction is running. Retry when it finishes.') from error
        source = (folder/'latest.json').read_bytes()
        baseline = json.loads(source)
        sha = baseline['image_sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('Invalid imagery identity')
        image_bytes = (folder/f'{sha}.png').read_bytes()
        if hashlib.sha256(image_bytes).hexdigest() != sha:
            raise ValueError('Source imagery changed')
        image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        if image.size != (baseline['image_width'], baseline['image_height']):
            raise ValueError('Imagery dimensions changed')
        if view_kwargs:
            # Validate the views against the verified PNG grid before anything is written.
            view_kwargs, views = evidence_view_kwargs(evidence_views, image, roof_models=roof_models)
        detector, variant = segmenter, None
        if image_variant is not None:
            variant_frame, variant = variant_image(image_variant, image)
        confidence = folder/'confidence.json'
        base_dir, cand_dir = out/'baseline/data/scenes'/scene_id, out/'data/scenes'/scene_id
        for target in (base_dir, cand_dir):
            target.mkdir(parents=True, exist_ok=True)
            (target/f'{sha}.png').write_bytes(image_bytes)
        (base_dir/'latest.json').write_bytes(source)
        if confidence.exists():
            (base_dir/'confidence.production.json').write_bytes(confidence.read_bytes())
        crm_priors = None
        if not detector_only and (repo/'public/address-roofs.json').exists():
            # analyse_scene reads CRM priors from root/public; give out_root the same bytes.
            (out/'public').mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo/'public/address-roofs.json', out/'public/address-roofs.json')
            crm_priors = hashlib.sha256((out/'public/address-roofs.json').read_bytes()).hexdigest()
        if kinds is None:
            kinds = [k for k in baseline.get('scans', {}) if k in PROMPTS] or None
        if segmenter is None:
            from . import sam3
            segmenter = sam3.Sam3TextSegmenter(Path(__file__).parent/'models/sam3')
        detector = VariantSegmenter(segmenter, image, variant_frame) if variant else segmenter
        parcel_source = 'caller' if parcel_data else 'caller-none'
        if parcel_data is None:
            parcel_data, parcel_source = frozen_parcels(baseline, repo)
        prediction = analyse_scene(baseline, image, detector, kinds=kinds, roof_models=roof_models,
                                   detector_only=detector_only, root=out, parcel_data=parcel_data, **view_kwargs)
        owned = reapply_ownership(prediction)
        saved = save_scene(prediction, image_bytes, root=out)
    baseline_confidence = reassess_baseline(scene_id, out/'baseline', assess)
    assess_error, rows = None, {}
    try:
        rows = {r['object_id']: r for r in assess(scene_id, root=out)['objects']}
    except Exception as error:  # Same reasoning as reassess_baseline: keep the saved candidate and its run record.
        assess_error = str(error)[:500]
    owned_ids = set(saved.get('owned_object_ids', [o['id'] for o in saved['objects']]))
    _atomic(cand_dir/'candidate-objects.json', {
        'version': VERSION, 'scene_id': scene_id, 'model_signature': saved['model_signature'], 'image_sha256': sha,
        'note': 'Every candidate object; confidence.sqlite indexes owned roofs only. Diagnostics, not labels/accuracy.',
        'objects': [{'object_id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'score': o.get('score'),
                     'owned': o['id'] in owned_ids, 'band': rows.get(o['id'], {}).get('band'),
                     'observed_segments': rows.get(o['id'], {}).get('observed_segments'),
                     'image_line_mismatch_m': rows.get(o['id'], {}).get('image_line_mismatch_m')} for o in saved['objects']]})
    record = {'version': VERSION, 'scene_id': scene_id, 'image_sha256': sha,
              'baseline_model_signature': baseline.get('model_signature'),
              'candidate_model_signature': saved['model_signature'],
              'signature_reproduced': saved['model_signature'] == baseline.get('model_signature'),
              'baseline_diff': signature_diff(baseline, saved),
              'baseline_profile': profile(baseline), 'candidate_profile': profile(saved),
              'baseline_root': str(out/'baseline'), 'candidate_root': str(out),
              'code_sha256': code_hashes(), 'module_sha256': CODE_SHA256,
              'elapsed': round(time.monotonic()-started, 3), 'elapsed_unit': 's',
              'variables': {'detector_only': detector_only, 'roof_models': roof_models, 'kinds': kinds,
                            'image_variant': variant, 'evidence_views': views, 'parcel_source': parcel_source,
                            'crm_priors_sha256': crm_priors, 'ownership_reapplied': owned,
                            'segmenter': type(segmenter).__name__,
                            'detector_weights': getattr(segmenter, 'model_identity', None),
                            'detector_frame_substitutions': detector.substituted if variant else 0},
              'objects': len(saved['objects']), 'confidence_objects': len(rows), 'assess_error': assess_error,
              'baseline_confidence': baseline_confidence,
              'warnings': warnings, 'started_at': stamp, 'computed_at': datetime.now(UTC).isoformat(),
              'note': 'Isolated candidate for paired comparison; diagnostics, not labels/accuracy.'}
    _atomic(cand_dir/'isolated-run.json', record)
    _atomic(out/'isolated-run.json', record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('scene_id')
    parser.add_argument('--out-root', type=Path, required=True,
                        help='Absolute output root (inside the repo only under data/reviews/eyes).')
    parser.add_argument('--detector-only', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--roof-models', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--image-variant', type=Path, help='HxWx3 uint8 .npy fed only to the whole-frame detector encode.')
    parser.add_argument('--no-parcel-fetch', action='store_true',
                        help='Pass an empty parcel set instead of reading the frozen cache or fetching Vicmap parcels '
                             '(offline, deterministic; recorded as parcel_source caller-none).')
    args = parser.parse_args(argv)
    variant = np.load(args.image_variant, allow_pickle=False) if args.image_variant else None
    try:
        record = run(args.scene_id, args.out_root.absolute(), detector_only=args.detector_only,
                     roof_models=args.roof_models, image_variant=variant, parcel_data=[] if args.no_parcel_fetch else None)
    except ExtractionBusy as error:
        sys.stderr.write(f'{error}\n')
        return EXIT_BUSY
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    keys = ('scene_id', 'baseline_model_signature', 'candidate_model_signature', 'signature_reproduced', 'baseline_diff',
            'baseline_profile', 'candidate_profile', 'objects', 'confidence_objects', 'assess_error', 'elapsed', 'warnings')
    print(json.dumps({k: record[k] for k in keys}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
