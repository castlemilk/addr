"""Registered imagery quality diagnostics and conservative multi-capture fusion.

Fusion preserves the native reference grid; it is not generative upscaling or
new geometric truth. Inputs must already be orthorectified to the same frame.
"""
from __future__ import annotations

import hashlib
from datetime import date

import cv2
import numpy as np
from PIL import Image

VERSION = 'roof-image-evidence-v1'


def quality_report(obj, scene, image):
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillPoly(mask, [np.rint(obj['ring']).astype(np.int32)], 1)
    for ring in obj.get('holes', []):
        cv2.fillPoly(mask, [np.rint(ring).astype(np.int32)], 0)
    mask = cv2.erode(mask, np.ones((5, 5), np.uint8)).astype(bool)
    laplacian = cv2.Laplacian(gray, cv2.CV_32F)
    values = gray[mask]
    frame = np.asarray(scene['pixel_to_meters'])
    return {'version': VERSION, 'ground_pixel_m': round(float(np.sqrt(abs(np.linalg.det(frame[:2])))), 4),
            'laplacian_variance': round(float(laplacian[mask].var()), 3) if len(values) else None,
            'contrast_p90_p10': round(float(np.percentile(values, 90)-np.percentile(values, 10)), 2) if len(values) else None,
            'provider': scene.get('region', {}).get('provider', 'unknown'),
            'acquisition_date': scene.get('region', {}).get('acquisition_date'),
            'enhancement': 'Denoise + local contrast, same pixel registration',
            'fusion_status': 'needs_independent_registered_captures',
            'reason': 'Texture metrics are not accuracy. Different map tiles or providers may reuse one acquisition; missing dates prevent reliable multi-date fusion.'}


def fuse_registered(images, captures):
    """Fuse 3–8 verified distinct, nearby-date captures with small translations.

    Callers provide the same frame_id only after orthorectification. Reject
    duplicated acquisitions, low alignment and substantial changed content.
    Changed/occluded pixels retain the reference; no holes are inpainted.
    """
    if not 3 <= len(images) <= 8 or len(images) != len(captures):
        raise ValueError('Use 3–8 independently acquired, registered images.')
    if not all(c.get('acquisition_id') and c.get('acquisition_date') and c.get('frame_id') for c in captures):
        raise ValueError('Acquisition identities, dates and a shared registered frame are required.')
    dates = [date.fromisoformat(c['acquisition_date']) for c in captures]
    if (len({c['acquisition_id'] for c in captures}) != len(captures)
            or len({c['frame_id'] for c in captures}) != 1 or (max(dates)-min(dates)).days > 90):
        raise ValueError('Captures must be independent, in one frame and within 90 days; inspect temporal change separately.')
    arrays = [np.asarray(im.convert('RGB')) for im in images]
    hashes = [hashlib.sha256(a.tobytes()).hexdigest() for a in arrays]
    if len(set(hashes)) != len(hashes) or any(a.shape != arrays[0].shape for a in arrays):
        raise ValueError('Duplicate pixels or mismatched grids cannot supply independent detail.')
    reference = arrays[0]
    h, w = reference.shape[:2]
    gray = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY).astype(np.float32)/255
    aligned, valid, registrations = [reference], [np.ones((h, w), bool)], []
    for array in arrays[1:]:
        source = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY).astype(np.float32)/255
        # Exclude gross photometric changes from alignment as well as fusion.
        # The inputs are already orthorectified; this is a bounded fine shift.
        alignment_mask = cv2.erode((np.abs(gray-source) < .2).astype(np.uint8), np.ones((5, 5), np.uint8))
        if alignment_mask.mean() < .75:
            raise ValueError('Too much changed content to establish alignment.')
        try:
            correlation, transform = cv2.findTransformECC(gray, source, np.eye(2, 3, dtype=np.float32),
                cv2.MOTION_TRANSLATION, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6), alignment_mask)
        except cv2.error as error:
            raise ValueError('Could not establish subpixel alignment; keep the original captures separate.') from error
        if correlation < .9 or np.linalg.norm(transform[:, 2]) > 3:
            raise ValueError('Alignment fails the correlation or displacement budget.')
        warped = cv2.warpAffine(array, transform, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        cover = cv2.warpAffine(np.ones((h, w), np.uint8), transform, (w, h),
                              flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP).astype(bool)
        aligned.append(warped)
        valid.append(cover)
        registrations.append({'correlation': float(correlation), 'translation_px': transform[:, 2].tolist()})
    stack = np.stack(aligned).astype(float)
    stable = (np.ptp(stack, axis=0).max(axis=2) <= 35) & np.logical_and.reduce(valid)
    if np.mean(stable) < .75:
        raise ValueError('Too much changed or unregistered content; do not fuse these captures.')
    result = reference.copy()
    result[stable] = np.clip(np.mean(stack, axis=0)[stable], 0, 255).astype(np.uint8)
    return Image.fromarray(result), {'version': VERSION, 'captures': captures, 'pixel_sha256': hashes,
                                   'registration': registrations, 'fused_fraction': float(np.mean(stable)),
                                   'new_resolution': False, 'filled_hidden_regions': False,
                                   'reference_preserved_elsewhere': True}


if __name__ == '__main__':
    import argparse
    import json
    from pathlib import Path

    from .scene_batch import signature
    from .scene_training import atomic_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    captures = json.loads(args.manifest.read_text())['captures']
    images = [Image.open(args.manifest.parent/c['image_path']).convert('RGB') for c in captures]
    fused, report = fuse_registered(images, captures)
    destination = Path('data/roof-review-loop/imagery')/signature(report)[:16]
    destination.mkdir(parents=True, exist_ok=True)
    fused.save(destination/'fused-native.png')
    atomic_json(destination/'report.json', report)
    print(json.dumps({'artifact': str(destination), 'resolution_increased': False}))
