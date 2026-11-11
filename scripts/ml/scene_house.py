"""Capture an address on demand without changing curated cohorts or reviews."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from .scene_region import capture_region
from .tiles import WORLD_SPAN_M


def house_source(address_id, *, root=Path('.'), fetcher=None):
    if isinstance(address_id, bool) or not isinstance(address_id, int) or address_id <= 0:
        raise ValueError('Choose a valid address')
    with sqlite3.connect(f'file:{root / "data/australia_geo.db"}?mode=ro', uri=True) as db:
        row = db.execute('SELECT full_address, lat, lon FROM addresses WHERE id=?', (address_id,)).fetchone()
    if not row:
        raise ValueError('Address not found')
    title, lat, lon = row
    size = 65.
    records_path = root/'public/address-roofs.json'
    records = json.loads(records_path.read_text()) if records_path.exists() else []
    record = next((r for r in records if r['address_id'] == address_id), None)
    if record and record.get('distance_meters', 999) <= 30:
        # Centre on the recorded roof, with context, rather than its street-access pin.
        lat, lon = record['centroid']['lat'], record['centroid']['lon']
        size = min(150., max(65., max(abs(v) for p in record['ring_meters'] for v in p)*2+20))
    north = size / 2 / (WORLD_SPAN_M / 360)
    east = north / math.cos(math.radians(lat))
    scene, image, data = capture_region([lon-east, lat-north, lon+east, lat+north], root=root, fetcher=fetcher)
    scene.update(id=f'house-{address_id}', address_id=address_id, title=title)
    return scene, image, data


PIPELINE = 'sam3-scene-roof-v12'
MULTIVIEW_PIPELINE = PIPELINE+'+multiview'


def normalise_evidence_views(evidence_views, image):
    """[{name, array, stack_id}] or (name, array[, stack_id]) tuples -> (pairs for propose_roof, provenance).

    Arrays must be HxW uint8 on the saved image grid; validated up front so a bad view fails the
    call instead of being recorded as a per-roof fitting failure.
    """
    import hashlib

    import numpy as np

    from .roof_line_fit import validate_extra_views
    if not isinstance(evidence_views, (list, tuple)):
        raise ValueError('evidence_views must be a list of {name, array, stack_id} views')
    pairs, provenance = [], []
    for item in evidence_views:
        if isinstance(item, dict):
            name, array, stack_id = item.get('name'), item.get('array'), item.get('stack_id')
        elif isinstance(item, (list, tuple)) and len(item) in (2, 3):
            name, array, stack_id = (*item, None)[:3]
        else:
            raise ValueError('evidence_views must be a list of {name, array, stack_id} views')
        if not isinstance(name, str) or not name or not isinstance(array, np.ndarray):
            raise ValueError('evidence_views entries need a name and an HxW uint8 array')
        pairs.append((name, array))
        provenance.append({'name': name, 'sha256': hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest(),
                           'stack_id': stack_id})
    validate_extra_views(pairs, image)
    return pairs, provenance


def add_roof_estimates(scene, image, *, evidence_views=None, context_hints=None, optical_guidance=True):
    """Use the shared audited inference path; these are never checked labels.

    context_hints (default None: output byte-identical) is the dict from scene_object_quality.class_hints_for_scene
    (or a plain {object_id: class_hint} mapping); each hinted roof's propose_roof call receives its hint, which
    flat_evidence may use only to waive the gates named in FLAT_RULES['context']. Summarised in analysis.context_hints.

    evidence_views (default None: output byte-identical) are registered extra LSD views
    ([{name, array, stack_id}]); they are forwarded to propose_roof, recorded in
    analysis.evidence_views and mark the pipeline as '+multiview'. The saved PNG stays
    the evidence gate for every line they contribute.
    """
    import hashlib
    import os

    import numpy as np
    from shapely.errors import GEOSException

    from .roof_assembly import VERSION as assembly_version
    from .roof_boundary import VERSION as canopy_version
    from .roof_junctions import VERSION as junction_version
    from .roof_optical_guidance import VERSION as guidance_version
    from .roof_optical_guidance import compact_report
    from .roof_surface import VERSION as boundary_version
    from .roof_topology import VERSION as topology_version
    from .roof_wings import VERSION as wings_version
    from .scene_roof import propose_roof

    view_kwargs, provenance = {}, None
    if evidence_views is not None:
        pairs, provenance = normalise_evidence_views(evidence_views, image)
        view_kwargs = {'evidence_views': pairs}
    frame = np.asarray(scene['pixel_to_meters'])
    completed, failed, failures = 0, 0, []
    hints = {}
    if context_hints is not None:
        hints = context_hints.get('hints', context_hints) if isinstance(context_hints, dict) else None
        if not isinstance(hints, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in hints.items()):
            raise ValueError('context_hints must map object ids to class-hint strings')
    for obj in scene['objects']:
        if obj['kind'] != 'building' or obj.get('roof_parts') or obj.get('roof_annotation'):
            continue
        try:
            def local(ring):
                return (np.c_[ring, np.ones(len(ring))] @ frame).tolist()
            proposal = propose_roof(local(obj['ring']), obj['model']['height_m'], 2., 'auto', image=image,
                                    meters_to_pixel=scene['meters_to_pixel'], holes=[local(h) for h in obj.get('holes', [])],
                                    **view_kwargs, **({'context_hint': hints[obj['id']]} if obj['id'] in hints else {}),
                                    **({'optical_guidance': False} if not optical_guidance else {}))
            obj.update(roof_annotation=proposal['annotation'], roof_parts=proposal['parts'], roof_lines=proposal['lines'],
                       roof_type=proposal['roof_type'], roof_geometry_source=proposal['source'], roof_geometry_note=proposal['note'])
            obj['roof_form_evidence'] = proposal.get('form_evidence')
            obj['roof_form_uncertain'] = bool(proposal.get('form_uncertain'))
            if proposal.get('form_comparison'):
                obj['roof_form_comparison'] = proposal['form_comparison']
            if proposal.get('optical_guidance'):
                obj['roof_optical_guidance'] = compact_report(proposal['optical_guidance'])
            construction = (obj.get('boundary_refinement') or {}).get('construction') or {}
            if construction.get('accepted'):
                obj['roof_geometry_note'] += ' Boundary angles follow visible eave directions; this is an unreviewed construction hypothesis.'
                if construction.get('inferred_edges'):
                    obj['roof_geometry_note'] += ' Straight boundary sections behind detected tree canopy are inferred, not visible or measured roof edges.'
            completed += 1
        except (ValueError, ArithmeticError, RuntimeError, GEOSException) as error:
            obj['roof_geometry_note'] = 'Roof surface could not be fitted. Review the outline and propose its shape in the editor.'
            failures.append({'object_id': obj['id'], 'reason': str(error)[:500]})
            failed += 1
    model = Path(os.environ.get('ROOF_FORM_MODEL', 'scripts/ml/models/roof-form.json'))
    from .roof_form import _trained_model
    try:
        active = model.exists() and _trained_model(str(model), model.stat().st_mtime_ns) is not None
    except (ValueError, OSError, KeyError, TypeError):
        active = False
    scene['analysis'] = {'pipeline': PIPELINE if provenance is None else MULTIVIEW_PIPELINE,
                         'roof_proposals': completed, 'roof_failures': failed,
                         'roof_optical_guidance': guidance_version if optical_guidance else 'disabled',
                         'roof_optical_guidance_code': hashlib.sha256(Path(__file__).with_name('roof_optical_guidance.py').read_bytes()).hexdigest(),
                         'optical_layers_code': hashlib.sha256(Path(__file__).with_name('optical_layers.py').read_bytes()).hexdigest(),
                         'roof_failure_details': failures,
                         'roof_model': hashlib.sha256(model.read_bytes()).hexdigest() if active else 'colour-line-heuristic',
                         'boundary_version': boundary_version, 'topology_version': topology_version, 'junction_version': junction_version,
                         'canopy_version': canopy_version,
                         'canopy_boundary_code': hashlib.sha256(Path(__file__).with_name('roof_boundary.py').read_bytes()).hexdigest(),
                         'assembly_version': assembly_version,
                         'wings_version': wings_version,
                         'roof_wings_code': hashlib.sha256(Path(__file__).with_name('roof_wings.py').read_bytes()).hexdigest(),
                         'roof_assembly_code': hashlib.sha256(Path(__file__).with_name('roof_assembly.py').read_bytes()).hexdigest(),
                         'boundary_code': hashlib.sha256(Path(__file__).with_name('roof_surface.py').read_bytes()).hexdigest(),
                         'roof_line_code': hashlib.sha256(Path(__file__).with_name('roof_line_fit.py').read_bytes()).hexdigest(),
                         'roof_topology_code': hashlib.sha256(Path(__file__).with_name('roof_topology.py').read_bytes()).hexdigest(),
                         'roof_junction_code': hashlib.sha256(Path(__file__).with_name('roof_junctions.py').read_bytes()).hexdigest(),
                         'roof_end_code': hashlib.sha256(Path(__file__).with_name('roof_form.py').read_bytes()).hexdigest(),
                         'roof_physics_code': hashlib.sha256(Path(__file__).with_name('roof_physics.py').read_bytes()).hexdigest(),
                         'roof_structure_code': hashlib.sha256(Path(__file__).with_name('roof_structure.py').read_bytes()).hexdigest(),
                         'geometry_code': hashlib.sha256(Path(__file__).with_name('scene_roof.py').read_bytes()).hexdigest()}
    if provenance is not None:
        from .roof_line_fit import EXTRA_VIEWS_VERSION
        scene['analysis'].update(evidence_views=provenance, extra_views_version=EXTRA_VIEWS_VERSION)
    if context_hints is not None:
        from collections import Counter

        from .roof_form import FLAT_RULES
        meta = context_hints if 'hints' in context_hints else {}
        scene['analysis']['context_hints'] = {
            'version': meta.get('version'), 'hinted_objects': len(hints), 'by_hint': dict(sorted(Counter(hints.values()).items())),
            'read_by': {'flat_evidence': FLAT_RULES.get('context')}, 'context': meta.get('context'), 'error': meta.get('error'),
            'structure_version': meta.get('structure_version'), 'structure_hints_applied': meta.get('structure_hints_applied'),
            'by_structure': dict(sorted(Counter(v.get('sub_kind') for v in (meta.get('structure') or {}).values()).items())),
            'structure_error': meta.get('structure_error'),
            'note': 'Agent class hints from land use, POIs, addresses and size, not labels; only the flat-roof gates named in read_by consult them.'}


def pipeline_context_hints(scene, image, root=Path('.')):
    """Class hints for add_roof_estimates from cached context only; never raises, so the pipeline cannot fail on them.

    Structure sub-kinds (structure_kinds.assess_scene, geometry and appearance only, no SAM3) are added as
    result['structure'] = {object_id: {sub_kind, confidence}}; a sub-kind listed in FLAT_RULES['context']['flat_hints']
    with at least moderate confidence replaces the class hint for that roof, so propose_roof sees e.g.
    'shipping-container' and selects the flat template. Agent hints, never labels.
    """
    from .scene_object_quality import class_hints_for_scene
    try:
        result = class_hints_for_scene(scene, image=image, root=root)
    except Exception as error:  # noqa: BLE001 - hints are optional evidence; record the failure instead of losing the roof
        return {'version': None, 'hints': {}, 'context': None, 'error': f'{type(error).__name__}: {error}'[:200]}
    payloads = result.pop('flag_payloads', {}) or {}
    try:
        import numpy as np

        from . import structure_kinds
        from .roof_form import FLAT_RULES
        flags = {(scene.get('id'), oid): payload for oid, payload in payloads.items()}
        rgb = None if image is None else np.asarray(image.convert('RGB') if hasattr(image, 'convert') else image)
        structure = {}
        for row in structure_kinds.assess_scene(scene, image=rgb, flags=flags):
            payload = row.get('payload') or {}
            if payload.get('sub_kind') in structure_kinds.SUB_KINDS:
                structure[row['object_id']] = {'sub_kind': payload['sub_kind'], 'confidence': payload.get('confidence')}
        flat_hints = (FLAT_RULES.get('context') or {}).get('flat_hints') or []
        replaced = 0
        for oid, entry in structure.items():
            if entry['sub_kind'] in flat_hints and entry['confidence'] in ('moderate', 'strong'):
                result['hints'][oid] = entry['sub_kind']
                replaced += 1
        result.update(structure=structure, structure_version=structure_kinds.VERSION, structure_hints_applied=replaced)
    except Exception as error:  # noqa: BLE001 - structure hints are optional; class hints stand
        result.update(structure={}, structure_version=None, structure_error=f'{type(error).__name__}: {error}'[:200])
    return result
