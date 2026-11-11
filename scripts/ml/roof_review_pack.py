"""Make reproducible visual review packs from completed candidate batches.

Original imagery, model overlays and observed-line overlays share one crop.
Packs are agent review aids, never CRM reviews or exportable training labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .roof_candidate_review import evidence_context
from .roof_line_fit import enhance_roof_detail
from .roof_review_diagnostics import diagnose, fuller_captures
from .roof_review_loop import check_input, run_path
from .scene_batch import signature
from .scene_confidence import geographic
from .scene_training import atomic_json, atomic_text
from .tiles import lonlat_to_tile_xy

VERSION = 'roof-visual-review-pack-v1'
COLORS = {'ridge': '#7bf0a8', 'hip': '#66deed', 'valley': '#d4a0ff'}


def displayed_roof_lines(obj):
    """The saved annotation graph is authoritative; no flat cache fallback."""
    annotation = obj.get('roof_annotation')
    if isinstance(annotation, dict) and isinstance(annotation.get('vertices'), list) and isinstance(annotation.get('edges'), list):
        vertices = annotation['vertices']
        lines = []
        for edge in annotation['edges']:
            a, b = edge.get('a'), edge.get('b')
            if not isinstance(a, int) or not isinstance(b, int) or not (0 <= a < len(vertices) and 0 <= b < len(vertices)):
                continue
            points = np.asarray([vertices[a][:2], vertices[b][:2]], dtype=float)
            if points.shape == (2, 2) and np.isfinite(points).all():
                lines.append({'line_meters': points.tolist(), 'kind': edge.get('kind', 'ridge')})
        return lines
    if obj.get('roof_type') == 'flat':
        return []
    return obj.get('roof_lines', [])


def render_card(obj, scene, image, target, *, observations=None, optical_guidance=None):
    """A registered diagnostic visualization; return its exact pixel transforms.

    A caller may supply the existing image-segment observations so a handoff and
    its picture use the same evidence without detecting the lines twice.
    """
    candidate = (optical_guidance or {}).get('candidate_object', obj)
    points = np.asarray([*obj['ring'], *candidate['ring']])
    lower, upper = points.min(axis=0), points.max(axis=0)
    span = max(float(max(upper-lower))*1.25, 24.)
    corner = (lower+upper-span)/2
    box = tuple(int(round(v)) for v in (*corner, *(corner+span)))
    corner = np.asarray(box[:2])
    width = 360
    scale = width/(np.asarray(box[2:])-corner)
    comparison = (optical_guidance or {}).get('family_comparison_object')
    boundary = (optical_guidance or {}).get('boundary_guidance')
    columns = 5 if comparison else 4 if optical_guidance else 3
    canvas = Image.new('RGB', (10+370*columns, 430), '#101827')
    font = ImageFont.load_default(size=13)
    draw = ImageDraw.Draw(canvas)
    draw.text((12, 8), f'{scene["id"]} / {obj["id"]}', font=font, fill='white')
    if observations is None:
        _, observations, _ = evidence_context(obj, scene, image)

    def project(xy):
        return [tuple(p) for p in (np.asarray(xy)-corner)*scale]

    titles = ['Original RGB', 'Saved model outline and roof lines', 'Detected lines on enhanced contrast']
    if optical_guidance:
        accepted = bool((optical_guidance.get('report') or {}).get('accepted'))
        form = optical_guidance.get('form_evidence') or {}
        coplanar = optical_guidance.get('candidate_object', {}).get('roof_type') == 'flat'
        unresolved = form.get('decision') == 'uncertain'
        titles[2] = 'Detected: yellow base / cyan optical'
        titles.append('Coplanar start / family unresolved' if coplanar and unresolved else
                      'Flat / low-slope hypothesis' if coplanar else 'Pitched prior / family unresolved' if unresolved else
                      'Selected proposal / valley hypotheses' if accepted else 'Fresh baseline / unresolved optical cues')
        if boundary:
            titles[-1] = 'Boundary + roof / review hypothesis' if boundary.get('accepted') else 'Boundary + roof / uncertain comparison'
        if comparison:
            titles.append('Level comparison / family unresolved')
    for col, title in enumerate(titles):
        source = Image.fromarray(enhance_roof_detail(image)).convert('RGB') if col == 2 else image
        tile = source.crop(box).resize((width, width))
        overlay = ImageDraw.Draw(tile)
        if col in (1, 3, 4):
            drawn = comparison if col == 4 else candidate if col == 3 else obj
            for ring in [drawn['ring'], *drawn.get('holes', [])]:
                overlay.line(project([*ring, ring[0]]), fill=COLORS['hip'], width=2)
            for line in displayed_roof_lines(drawn):
                pixels = np.c_[line['line_meters'], np.ones(2)] @ np.asarray(scene['meters_to_pixel'])
                colour = '#8a95a8' if col == 3 and line['kind'] == 'valley' else COLORS.get(line['kind'], '#ffffff')
                overlay.line(project(pixels), fill=colour, width=2)
            if col == 3 and boundary:
                for cue in boundary.get('inferred_edges', []):
                    a, b = np.asarray(project(cue['line_pixels']))
                    length = np.linalg.norm(b-a)
                    for start in np.arange(0., length, 12.):
                        overlay.line([tuple(a+(b-a)*start/length), tuple(a+(b-a)*min(start+6., length)/length)], fill='#ffb45b', width=3)
        elif col == 2:
            for line in observations:
                pixels = np.c_[line['points'], np.ones(2)] @ np.asarray(scene['meters_to_pixel'])
                overlay.line(project(pixels), fill='#ffd259', width=2)
            optical_evidence = ((optical_guidance or {}).get('report') or {}).get('observations') or {}
            for line in optical_evidence.get('additional_segment_evidence', []):
                pixels = np.c_[line['points_meters'], np.ones(2)] @ np.asarray(scene['meters_to_pixel'])
                overlay.line(project(pixels), fill='#62cfff', width=2)
        if col == 3 and not coplanar and not unresolved and not boundary:
            report = optical_guidance.get('report') or {}
            # Accepted output must not display already resolved baseline issues
            # as current contradictions. Their coordinates remain in the report.
            for cue in ([] if report.get('accepted') else report.get('contradictions', []))[:20]:
                points = cue.get('points_meters')
                if not points:
                    continue
                pixels = np.c_[points, np.ones(len(points))] @ np.asarray(scene['meters_to_pixel'])
                colour = '#ff785d' if cue['code'] == 'model_fold_without_line_support' else '#ffd259'
                overlay.line(project(pixels), fill=colour, width=2)
            valleys = report.get('selected_valleys', []) if report.get('accepted') else report.get('valley_candidates', [])
            for cue in valleys:
                if cue.get('status') != 'valley_supported' or (not report.get('accepted') and not cue.get('potential_missing_valley')):
                    continue
                pixels = np.c_[cue['points_meters'], np.ones(2)] @ np.asarray(scene['meters_to_pixel'])
                overlay.line(project(pixels), fill=COLORS['valley'], width=3)
        x = 10+370*col
        draw.text((x, 31), title, font=font, fill='white')
        canvas.paste(tile, (x, 53))
    footer = (('Fourth panel: grey = valley geometry only; purple = image-supported valley hypothesis in the selected proposal. '
               'Heights unverified; black = outside source.' if accepted else
               'Fourth panel: grey = valley geometry only; yellow = ambiguous crease; red = weak line support; purple = image-supported valley in an alternative. '
               'Unverified; black = outside source.') if optical_guidance else
              'Black outside source = missing image context. Detected lines and roof heights are unverified.')
    if optical_guidance and coplanar:
        features = form.get('features') or {}
        footer = ('Fourth panel: unresolved coplanar starting surface; flat, shed and low-pitch remain possible. ' if unresolved else
                  f"Fourth panel: {form.get('rule', 'flat')} appearance rule; material plane brightness spread {features.get('pitched_plane_l_spread')} L. ")
        footer += 'No internal folds inferred. Slope, heights and drainage unmeasured; black = outside source.'
    elif comparison:
        footer = ('Fourth/fifth panels compare pitched and level starting surfaces; area or incomplete evidence cannot decide roof family. '
                  'Optical folds are not promoted. Heights and drainage unmeasured; black = outside source.')
    if boundary:
        footer = ('Fourth panel uses the proposed outline: orange dashes = inferred hidden span; RGB/SR nominations are not observations of hidden eaves. '
                  + ('Material-supported review hypothesis; ' if boundary.get('accepted') else 'Ambiguous comparison, not accepted; ')
                  + 'saved geometry unchanged.')
    draw.text((12, 415), footer, font=font, fill='#acbacc')
    canvas.save(target)
    return {'crop_box_pixels': list(box), 'panel_size_pixels': [width, width],
            'panel_origins_in_card_pixels': [[10+370*col, 53] for col in range(columns)],
            'source_to_panel': [[float(scale[0]), 0], [0, float(scale[1])],
                                [float(-corner[0]*scale[0]), float(-corner[1]*scale[1])]],
            'panel_to_source': [[float(1/scale[0]), 0], [0, float(1/scale[1])], list(map(float, corner))],
            'affine_convention': '[x, y, 1] @ matrix; source pixels are x-right/y-down; panel pixels exclude the title and border'}


def create_pack(root, run, limit=12):
    if not 1 <= limit <= 24:
        raise ValueError('Inspect between 1 and 24 roofs')
    folder = run_path(root, run)
    payload = (folder/'report.json').read_bytes()
    report = json.loads(payload)
    if report['status'] != 'complete':
        raise ValueError('Finish the candidate batch before freezing a visual review pack')
    code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
            (root/'scripts/ml').glob('roof_review*.py')}
    identity = {'version': VERSION, 'run': run, 'report_sha256': hashlib.sha256(payload).hexdigest(), 'limit': limit, 'code': code}
    pack = signature(identity)[:16]
    directory = root/'data/roof-review-loop/inspections'/pack
    if (directory/'report.json').exists():
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, row in enumerate(report['rows'][:limit]):
        item = {k: row[k] for k in ('scene_id', 'object_id', 'source_model', 'image_sha256', 'capture_title', 'review_url', 'status')}
        try:
            scene, obj, image = check_input(root, folder, index, row)
            item['triage'] = diagnose(obj, scene, image, row.get('before'))
            if item['triage']['primary'] == 'capture_context':
                item['triage']['fuller_captures'] = fuller_captures(root, scene, obj)
            center = geographic(obj, scene).centroid
            tile = lonlat_to_tile_xy(center.x, center.y, 17)
            item['tile'] = [17, int(tile[0]), int(tile[1])]
            name = f'roof-{index:02}.png'
            render_card(obj, scene, image, directory/name)
            item['card'] = name
            item['inspection_status'] = 'ready'
        except (ValueError, OSError, KeyError, StopIteration) as error:
            item.update(inspection_status='unavailable', error=str(error))
        rows.append(item)
    counts = Counter(r['triage']['primary'] for r in rows if r.get('triage'))
    output = {**identity, 'pack': pack, 'rows': rows, 'counts': dict(counts),
              'scope': 'Agent diagnostics, not human approvals, training labels or calibrated accuracy.'}
    lines = ['# Low-evidence roof visual review', '', f'Candidate batch: `{run}`. Review pack: `{pack}`.', '', output['scope'], '',
             'Compare original RGB first. Use model and extracted-line panels to explain a defect, not as ground truth.', '']
    for index, row in enumerate(rows):
        lines += [f'## {index+1}. {row["capture_title"]} · {row["object_id"][:8]}', '',
                  f'Capture context only; the title does not assign an address to this roof. [Open source roof](http://localhost:3008{row["review_url"]}).', '']
        if not row.get('card'):
            lines += [f'Unavailable: {row["error"]}', '']
            continue
        lines += [f'![Registered roof comparison]({directory/row["card"]})', '']
        for action in row['triage']['actions']:
            lines.append(f'- **{action["title"]}:** {action["reason"]} {action["action"]}')
        lines += ['', 'Diagnostic metrics: `'+json.dumps(row['triage']['metrics'], sort_keys=True)+'`.', '']
        for match in row['triage'].get('fuller_captures', []):
            lines += [f'- [Possible fuller capture: {match["capture_title"]} · {match["object_id"][:8]}](http://localhost:3008{match["review_url"]}). Verify that it is the same roof.', '']
    atomic_text(directory/'REVIEW.md', '\n'.join(lines))
    atomic_json(directory/'report.json', output)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    print(create_pack(args.root.resolve(), args.run, args.limit))


if __name__ == '__main__':
    main()
