"""window-readiness-v1: the render-and-look readiness gate, the LAST step of the frame-cut window pipeline.

Decision card `data/reviews/eyes/recapture/slice-body-census-2026-10-06/DECISION-CARD.md` item 4 (reviewer "yes to defaults",
2026-10-06; handover 29: "look at a window's render before handing it over; a window without a model layer is not ready for the
reviewer"): no window link reaches the reviewer until its render shows the asked object as a whole roof. The gate runs after the
takeover / body carry and before the link enters the worklist. It reads data/scenes/<window>/{latest,review}.json and the frame
PNG, renders the frame at native resolution with the asked objects (the carried frame-cut slices and their replacers) outlined, the
model-layer buildings thin and the frame edge marked, and decides READY / NOT READY with reasons exactly as the card states:

  slice_only    the asked object for a frozen key is a carried frame_cut_target slice with no replacer (undecided; a slice the
                reviewer has decided stands as decided and is never superseded, so it is not refused);
  model_empty   the model layer (latest.json building objects) is empty and no carried whole_roof_body replaces the slice;
  ring_on_frame an asked ring has a vertex within frame_tolerance_px (1.0, the TAKEOVER constant) of the frame, or beyond it.

A refused window is listed "not ready" with its reasons and render path. Writes only under --out (renders and verdicts) and the dated
report pair data/reviews/eyes/recapture/window-readiness-<date>.{json,md}; never a scene, the registry, RULES / WINDOW / DESIGN.
Every figure is a count or a geometry, never accuracy; a stale review (model_signature behind latest.json) is recorded, not a refusal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .scene_focus import TAKEOVER, _polygon, touching_frame, window_scenes
from .scene_recapture import _read_json
from .scene_self_review import validate_id

VERSION = 'window-readiness-v1'
CARD = 'data/reviews/eyes/recapture/slice-body-census-2026-10-06/DECISION-CARD.md#4'
SLICE_ROLE = 'frame_cut_target'
BODY_ROLE = 'whole_roof_body'
REASONS = ('slice_only', 'model_empty', 'ring_on_frame')
FRAME_TOLERANCE_PX = TAKEOVER['frame_tolerance_px']
REPORT_DIR = Path('data/reviews/eyes/recapture')
RULE_FILES = {'design': 'data/reviews/eyes/recapture/DESIGN.json', 'window': 'data/reviews/eyes/recapture/WINDOW.json',
              'rules': 'data/reviews/eyes/training/sam3-mac/round5/RULES.json'}
# Render palette (RGB): the problem is red, the whole roof green, the model thin cyan, the frame yellow, a touched edge magenta.
COLOURS = {'model': (0, 200, 255), 'superseded': (160, 160, 160), 'slice_pending': (255, 40, 40), 'slice_decided': (255, 170, 0),
           'replacer': (40, 255, 60), 'frame': (255, 255, 0), 'touched': (255, 0, 255), 'ready': (40, 255, 60),
           'not_ready': (255, 40, 40), 'text': (255, 255, 255), 'box': (0, 0, 0)}


def _now():
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    try:
        return _sha(Path(path).read_bytes())
    except OSError:
        return None


def _frozen_key(copy):
    frozen = copy.get('carried_from') or {}
    return [frozen.get('scene_id'), frozen.get('object_id')]


def _decided(copy):
    """The takeover's copy_decided test: anything but an unreviewed needs_adjustment copy (superseded handled by the caller)."""
    return copy.get('status') != 'needs_adjustment' or bool(copy.get('geometry_reviewed'))


def _replacer_pending(obj):
    """A replacer (the window's own detection or a carried body) still waits for the reviewer while it is a candidate or
    needs_adjustment without geometry_reviewed; confirmed / rejected / superseded or geometry_reviewed is decided."""
    return obj.get('status') in ('candidate', 'needs_adjustment') and not obj.get('geometry_reviewed')


def _asked_record(obj, role, width, height):
    ring = obj.get('ring') or []
    polygon = _polygon(obj)
    return {'id': obj.get('id'), 'role': role, 'kind': obj.get('kind'), 'status': obj.get('status'), 'source': obj.get('source'),
            'vertices': len(ring), 'area_px': round(polygon.area, 1),
            'frame_edges': touching_frame(ring, width, height, FRAME_TOLERANCE_PX),
            'carried_role': (obj.get('carried_from') or {}).get('role')}


def assess(review, latest):
    """The readiness of one window, pure: the carried frame_cut_target copies of `review` and their replacers against the model
    layer of `latest` and the frame. Returns {ready, reasons [{reason, ...}], keys, asked, model_buildings, review_buildings,
    frame, frame_tolerance_px, stale_review, nothing_pending}."""
    width, height = int(latest['image_width']), int(latest['image_height'])
    objects = [o for o in review.get('objects') or () if isinstance(o, dict) and 'id' in o]
    in_review = {o['id']: o for o in objects}
    model_buildings = [o for o in latest.get('objects') or () if isinstance(o, dict) and o.get('kind') == 'building']
    review_buildings = [o for o in objects if o.get('kind') == 'building']
    copies = [o for o in objects if isinstance(o.get('carried_from'), dict)]
    slices = [o for o in copies if o['carried_from'].get('role') == SLICE_ROLE]
    bodies = [o for o in copies if o['carried_from'].get('role') == BODY_ROLE]
    keys, asked, reasons = [], {}, []
    for copy in slices:
        key = {'copy_id': copy['id'], 'frozen_key': _frozen_key(copy), 'status': copy.get('status'),
               'geometry_reviewed': bool(copy.get('geometry_reviewed')), 'replacers': [], 'body_replacer': False,
               'decided': False, 'pending': False, 'asked': []}
        if copy.get('status') == 'superseded':
            replacers = [in_review[rid] for rid in copy.get('replacement_ids') or () if rid in in_review]
            key['replacers'] = [r['id'] for r in replacers]
            key['body_replacer'] = any((r.get('carried_from') or {}).get('role') == BODY_ROLE for r in replacers)
            key['replacer_reciprocal'] = all(copy['id'] in (r.get('replaces') or ()) for r in replacers)
            key['pending'] = any(_replacer_pending(r) for r in replacers) if replacers else True
            asked[copy['id']] = _asked_record(copy, 'slice_superseded', width, height)
            if not replacers:
                reasons.append({'reason': 'slice_only', 'copy_id': copy['id'], 'frozen_key': key['frozen_key'],
                                'detail': 'superseded but no replacer object in the review'})
            for r in replacers:
                role = 'body' if (r.get('carried_from') or {}).get('role') == BODY_ROLE else 'replacer'
                asked[r['id']] = _asked_record(r, role, width, height)
                asked[r['id']]['replaces'] = sorted(r.get('replaces') or ())
            key['asked'] = key['replacers']
        elif _decided(copy):
            key['decided'] = True
            key['asked'] = [copy['id']]
            asked[copy['id']] = _asked_record(copy, 'slice_decided', width, height)
        else:
            key['pending'] = True
            key['asked'] = [copy['id']]
            asked[copy['id']] = _asked_record(copy, 'slice_pending', width, height)
            reasons.append({'reason': 'slice_only', 'copy_id': copy['id'], 'frozen_key': key['frozen_key'],
                            'detail': 'carried frame-cut slice shown without a replacer (no takeover, no body carry)'})
        keys.append(key)
    for body in bodies:
        if body['id'] not in asked:                     # a body no slice names: shown and frame-checked all the same
            asked[body['id']] = _asked_record(body, 'body', width, height)
            asked[body['id']]['replaces'] = sorted(body.get('replaces') or ())
            asked[body['id']]['orphan'] = True
    needing_model = [k for k in keys if not k['decided'] and not k['body_replacer']]
    if not model_buildings and (needing_model or not keys):
        reasons.append({'reason': 'model_empty', 'copy_ids': [k['copy_id'] for k in needing_model],
                        'detail': 'latest.json holds no building object and no carried whole_roof_body replaces the slice'})
    for record in asked.values():
        if record['frame_edges']:
            reasons.append({'reason': 'ring_on_frame', 'object_id': record['id'], 'role': record['role'],
                            'edges': record['frame_edges'],
                            'detail': f'a vertex within {FRAME_TOLERANCE_PX:g} px of the frame or beyond it'})
    return {'ready': not reasons, 'reasons': reasons, 'reason_names': sorted({r['reason'] for r in reasons}), 'keys': keys,
            'asked': list(asked.values()), 'model_buildings': len(model_buildings), 'review_buildings': len(review_buildings),
            'copies': len(copies), 'frame': [width, height], 'frame_tolerance_px': FRAME_TOLERANCE_PX,
            'stale_review': review.get('model_signature') != latest.get('model_signature'),
            'nothing_pending': bool(keys) and not any(k['pending'] for k in keys)}


def _font(side):
    try:
        return ImageFont.load_default(size=20 if side >= 1024 else 13)
    except (TypeError, OSError):
        return ImageFont.load_default()


def _outline(draw, ring, colour, width, dots=False):
    points = [(float(p[0]), float(p[1])) for p in ring]
    if len(points) < 2:
        return
    draw.polygon(points, outline=colour, width=width) if len(points) >= 3 else draw.line(points, fill=colour, width=width)
    if dots:
        for x, y in points:
            draw.ellipse([x-3, y-3, x+3, y+3], outline=colour, width=1)


def render(image, review, latest, verdict, window_id):
    """The native-resolution frame with the model layer thin, superseded slices grey, pending slices red, decided slices amber,
    replacers and bodies green with vertex dots, the frame edge yellow, a touched edge magenta and the verdict banner."""
    frame = image.convert('RGB')
    draw = ImageDraw.Draw(frame)
    width, height = frame.size
    in_review = {o['id']: o for o in review.get('objects') or () if isinstance(o, dict) and 'id' in o}
    for obj in latest.get('objects') or ():
        if isinstance(obj, dict) and obj.get('kind') == 'building':
            _outline(draw, obj.get('ring') or [], COLOURS['model'], 1)
    styles = {'slice_superseded': ('superseded', 1, False), 'slice_pending': ('slice_pending', 3, False),
              'slice_decided': ('slice_decided', 2, False), 'replacer': ('replacer', 3, True), 'body': ('replacer', 3, True)}
    font = _font(max(width, height))
    for record in verdict['asked']:
        obj = in_review.get(record['id'])
        if obj is None:
            continue
        colour_key, stroke, dots = styles[record['role']]
        colour = COLOURS[colour_key]
        _outline(draw, obj.get('ring') or [], colour, stroke, dots)
        ring = obj.get('ring') or []
        if ring:
            x, y = float(ring[0][0]), float(ring[0][1])
            label = f"{record['id'][:8]} {record['role']}"
            if record['role'] in ('replacer', 'body', 'slice_decided') and obj.get('status'):
                label += f" {obj.get('status')}"
            draw.text((min(max(x, 2), width-160), min(max(y-16, 2), height-16)), label, fill=colour, font=font)
    draw.rectangle([0, 0, width-1, height-1], outline=COLOURS['frame'], width=1)
    touched = {edge for r in verdict['reasons'] if r['reason'] == 'ring_on_frame' for edge in r['edges']}
    lines = {'left': [(0, 0), (0, height-1)], 'right': [(width-1, 0), (width-1, height-1)],
             'top': [(0, 0), (width-1, 0)], 'bottom': [(0, height-1), (width-1, height-1)]}
    for edge in touched:
        draw.line(lines[edge], fill=COLOURS['touched'], width=4)
    state = 'READY' if verdict['ready'] else 'NOT READY: ' + ', '.join(verdict['reason_names'])
    text = [f'{window_id}  {state}',
            f"model buildings {verdict['model_buildings']}  copies {verdict['copies']}  frame {width}x{height}"
            + ('  stale review' if verdict['stale_review'] else '') + ('  nothing pending' if verdict['nothing_pending'] else '')]
    box_h = (font.size+4)*len(text)+6 if hasattr(font, 'size') else 14*len(text)+6
    draw.rectangle([4, 4, min(width-4, 12+max(draw.textlength(t, font=font) for t in text)), 4+box_h], fill=COLOURS['box'])
    for i, line in enumerate(text):
        colour = (COLOURS['ready'] if verdict['ready'] else COLOURS['not_ready']) if i == 0 else COLOURS['text']
        draw.text((8, 7+i*(box_h-6)//len(text)), line, fill=colour, font=font)
    return frame


def judge_window(root, window_id, out):
    """Assess ONE window, write <out>/<window>.png (native resolution) and <out>/<window>.json, return the verdict."""
    root, out = Path(root), Path(out)
    wid = validate_id(window_id)
    folder = root/'data/scenes'/wid
    latest, review = _read_json(folder/'latest.json'), _read_json(folder/'review.json')
    if not isinstance(latest, dict):
        raise ValueError(f'{wid}: no latest.json')
    if not isinstance(review, dict):
        review = {'objects': [], 'model_signature': None, 'revision': None}
    image_path = folder/f"{latest.get('image_sha256')}.png"
    data = image_path.read_bytes()
    if _sha(data) != latest.get('image_sha256'):
        raise ValueError(f'{wid}: image bytes do not hash to image_sha256')
    verdict = assess(review, latest)
    out.mkdir(parents=True, exist_ok=True)
    render_path = out/f'{wid}.png'
    with Image.open(image_path) as image:
        if image.size != (int(latest['image_width']), int(latest['image_height'])):
            raise ValueError(f'{wid}: image size {image.size} is not the frame {latest["image_width"]}x{latest["image_height"]}')
        render(image, review, latest, verdict, wid).save(render_path)
    verdict = {'version': VERSION, 'window_id': wid, 'at': _now(), 'render': str(render_path), **verdict,
               'image_sha256': latest.get('image_sha256'), 'model_signature': latest.get('model_signature'),
               'review_model_signature': review.get('model_signature'), 'review_revision': review.get('revision'),
               'review_updated_by': review.get('updated_by'), 'review_sha256': _file_sha(folder/'review.json'),
               'card': CARD}
    (out/f'{wid}.json').write_text(json.dumps(verdict, indent=2, ensure_ascii=False)+'\n')
    return verdict


def _line(verdict):
    wid = verdict['window_id']
    notes = []
    if verdict['stale_review']:
        notes.append('stale review')
    if verdict['nothing_pending']:
        notes.append('nothing pending (every key decided)')
    if verdict['ready']:
        bodies = [a['id'][:8] for a in verdict['asked'] if a['role'] == 'body']
        replacers = [a['id'][:8] for a in verdict['asked'] if a['role'] == 'replacer']
        what = []
        if bodies:
            what.append(f"body {', '.join(bodies)}")
        if replacers:
            what.append(f"own detection {', '.join(replacers)}")
        if not what:
            what.append('decided slices only' if verdict['nothing_pending'] else 'no carried slice')
        state = 'READY (' + '; '.join(what + notes) + ')'
    else:
        parts = []
        for r in verdict['reasons']:
            if r['reason'] == 'slice_only':
                parts.append(f"slice_only {r['copy_id'][:8]} ({'/'.join(str(k) for k in r['frozen_key'])})")
            elif r['reason'] == 'model_empty':
                parts.append('model_empty')
            else:
                parts.append(f"ring_on_frame {r['object_id'][:8]} {r['role']} {'+'.join(r['edges'])}")
        state = 'NOT READY: ' + '; '.join(parts + notes)
    return f"- {wid}: {state}; render {verdict['render']}"


def batch(root, window_ids, stem, *, overwrite=False):
    """Assess every window, renders and verdicts under <stem>/, the report <stem>.json and <stem>.md (one line per window)."""
    root, stem = Path(root), Path(stem)
    report_json, report_md = stem.with_suffix('.json'), stem.with_suffix('.md')
    if (report_json.exists() or report_md.exists()) and not overwrite:
        raise FileExistsError(f'{report_json} exists: a dated report is not rewritten without --overwrite')
    verdicts = [judge_window(root, wid, stem) for wid in window_ids]
    by_reason = {name: sum(1 for v in verdicts if name in v['reason_names']) for name in REASONS}
    ready = [v['window_id'] for v in verdicts if v['ready']]
    not_ready = [v['window_id'] for v in verdicts if not v['ready']]
    header = {'version': VERSION, 'written_at': _now(), 'card': CARD, 'windows': len(verdicts), 'ready': len(ready),
              'not_ready': len(not_ready), 'by_reason': by_reason,
              'ready_with_body': sum(1 for v in verdicts if v['ready'] and any(a['role'] == 'body' for a in v['asked'])),
              'ready_nothing_pending': sum(1 for v in verdicts if v['ready'] and v['nothing_pending']),
              'stale_reviews': sum(1 for v in verdicts if v['stale_review']),
              'frame_tolerance_px': FRAME_TOLERANCE_PX, 'takeover': dict(TAKEOVER),
              'sha256': {name: _file_sha(root/path) for name, path in RULE_FILES.items()}, 'renders': str(stem)}
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps({'header': header, 'windows': verdicts}, indent=2, ensure_ascii=False)+'\n')
    lines = [f'# Window readiness ({VERSION}), {header["written_at"]}', '',
             f'Render-and-look gate, the last step of the window pipeline (decision card item 4, "yes to defaults" 2026-10-06). '
             f'{header["windows"]} windows: {header["ready"]} ready ({header["ready_with_body"]} by a carried body, '
             f'{header["ready_nothing_pending"]} with nothing pending), {header["not_ready"]} not ready: '
             + ', '.join(f'{k} {v}' for k, v in by_reason.items()) + f' (a window may carry several reasons). Frame tolerance '
             f'{FRAME_TOLERANCE_PX:g} px (TAKEOVER). Stale reviews {header["stale_reviews"]} (recorded, not a refusal). '
             f'Counts and geometry, never accuracy. DESIGN {str(header["sha256"]["design"])[:16]}, '
             f'WINDOW {str(header["sha256"]["window"])[:16]}, RULES {str(header["sha256"]["rules"])[:16]}.', '',
             '## Not ready', '', *[_line(v) for v in verdicts if not v['ready']], '', '## Ready', '',
             *[_line(v) for v in verdicts if v['ready']], '']
    report_md.write_text('\n'.join(lines))
    return header, verdicts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', default='.')
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument('--scene', help='ONE window scene id (lot-* / win-*; qa_* for a rehearsal)')
    which.add_argument('--all', action='store_true', help='every frame-cut window under data/scenes')
    parser.add_argument('--out', help='renders and verdicts folder for --scene (default .run/window-ready/)')
    parser.add_argument('--stem', help='report stem for --all (default data/reviews/eyes/recapture/window-readiness-<UTC date>)')
    parser.add_argument('--overwrite', action='store_true', help='rewrite an existing dated report (default refused)')
    parser.add_argument('--date', help='report date for --all (default the UTC date; the session date when it differs)')
    parser.add_argument('--json', action='store_true', help='print the verdict / header as JSON')
    args = parser.parse_args(argv)
    if args.scene:
        verdict = judge_window(args.root, args.scene, args.out or Path(args.root)/'.run/window-ready')
        print(json.dumps(verdict, indent=2) if args.json else _line(verdict))
        return 0 if verdict['ready'] else 1
    date = args.date or f'{datetime.now(UTC):%Y-%m-%d}'
    stem = Path(args.stem) if args.stem else Path(args.root)/REPORT_DIR/f'window-readiness-{date}'
    header, verdicts = batch(args.root, window_scenes(args.root), stem, overwrite=args.overwrite)
    if args.json:
        print(json.dumps(header, indent=2))
    else:
        print(f"{header['windows']} windows: {header['ready']} ready, {header['not_ready']} not ready "
              + ', '.join(f'{k} {v}' for k, v in header['by_reason'].items()) + f"; report {stem}.md")
    return 0


if __name__ == '__main__':
    sys.exit(main())
