"""Agent visual review of structure-kinds hints: review cards, tier-3 observations, agreement, training export.

The rule hints in structure_kinds (structure-kinds-v1) are cheap but coarse. This module closes the loop
with a vision-capable agent (or a human) looking at the imagery:

  cards      render review pages: each object as an original crop and a CLAHE crop at 4x with a 5 m bar,
             the rule hint and its evidence summary, so a reviewer can name the sub-kind by eye.
  record     write observations to the eyes index as version structure-kinds-agent-review-v1 (object_quality).
             An observation carries the reviewer identity and tier. Agent observations are tier 3: they are
             never labels and never training data on their own. Human-confirmed rows (tier 1 or 2) are the
             only ones a fine-tune or classifier head may learn from.
  agreement  compare observations against the current rule hints: per sub-kind confusion counts.
  export     write crops, masks and a manifest (with provenance tier per object) for a later SAM3 fine-tune
             or classifier-head experiment. The manifest says which rows are human-confirmed; nothing here
             promotes an agent observation to a label.

Nothing here rewrites latest.json or any production artifact; other index versions are untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import eyes_index
from . import structure_kinds as sk
from .scene_object_quality import _row

REVIEW_VERSION = 'structure-kinds-agent-review-v1'
HUMAN_VERSION = 'structure-kinds-human-review-v1'   # human tiers live under their own version so they never overwrite agent rows
REVIEW_VERSIONS = (REVIEW_VERSION, HUMAN_VERSION)
TABLE = 'object_quality'
OUT_DIR = Path('data/reviews/eyes/structure-kinds/review')
TIERS = {'human-confirmed': 1, 'human-observed': 2, 'agent': 3}


def version_for(tier):
    return REVIEW_VERSION if TIERS[tier] == 3 else HUMAN_VERSION


def review_rows(db, **where):
    rows = []
    for version in REVIEW_VERSIONS:
        rows.extend(eyes_index.rows(db, TABLE, version=version, **where))
    return rows
OBSERVED_KINDS = sk.SUB_KINDS + ('caravan-or-boat', 'vehicle', 'tree-or-vegetation', 'pool-or-ground', 'part-of-house', 'unclear')
OBSERVATION_NOTE = 'agent observation, tier 3, not a label'


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def hint_rows(root, keys=None, sub_kinds=None):
    """structure-kinds-v1 rows, optionally restricted to (scene_id, object_id) keys or sub-kinds."""
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = eyes_index.rows(db, TABLE, version=sk.VERSION)
    if keys is not None:   # keep the caller's order (sample order) so card indices match the sample file
        order = {tuple(k[:2]): i for i, k in enumerate(keys)}
        rows = sorted((r for r in rows if (r['scene_id'], r['object_id']) in order), key=lambda r: order[(r['scene_id'], r['object_id'])])
    if sub_kinds:
        rows = [r for r in rows if r['payload']['sub_kind'] in sub_kinds]
    return rows


def _crop(image, ring, pad_px, scale, ring_colour=(255, 64, 200)):
    x0, y0 = max(int(ring[:, 0].min()-pad_px), 0), max(int(ring[:, 1].min()-pad_px), 0)
    x1, y1 = min(int(ring[:, 0].max()+pad_px)+1, image.shape[1]), min(int(ring[:, 1].max()+pad_px)+1, image.shape[0])
    crop = Image.fromarray(image[y0:y1, x0:x1]).resize(((x1-x0)*scale, (y1-y0)*scale), Image.Resampling.LANCZOS)
    ImageDraw.Draw(crop).polygon([((x-x0)*scale, (y-y0)*scale) for x, y in ring], outline=ring_colour, width=2)
    return crop


def render_cards(root, rows, out_dir, *, per_page=8, scale=4, pad_m=4., panel=360, font_size=20):
    """Review pages (PNG) of `rows`; returns the page paths. Each card: original | CLAHE, 5 m bar, hint, evidence."""
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    font, small = ImageFont.load_default(size=font_size), ImageFont.load_default(size=font_size-5)
    text_h = 5*(font_size+4)+10
    card_w, card_h = 2*panel+30, panel+text_h
    scenes, images, enhanced = {}, {}, {}
    pages = []
    for start in range(0, len(rows), per_page):
        chunk = rows[start:start+per_page]
        cols = 2
        sheet = Image.new('RGB', (cols*card_w, math.ceil(len(chunk)/cols)*card_h), (16, 16, 20))
        draw = ImageDraw.Draw(sheet)
        for i, row in enumerate(chunk):
            sid = row['scene_id']
            if sid not in scenes:
                scenes[sid] = sk.load_scene(root, sid)
                images[sid] = sk.load_image(root, scenes[sid])
                enhanced[sid] = sk.enhance(images[sid]) if images[sid] is not None else None
            scene, p = scenes[sid], row['payload']
            cx, cy = (i % cols)*card_w, (i//cols)*card_h
            obj = next((o for o in scene['objects'] if o['id'] == row['object_id']), None)
            if images[sid] is not None and obj is not None:
                ring = np.asarray(obj['ring'], dtype=float)
                px_m = sk.pixel_size_m(scene)
                for k, source in enumerate((images[sid], enhanced[sid])):
                    crop = _crop(source, ring, pad_m/px_m, scale)
                    fit = min(panel/crop.width, panel/crop.height, 1.)
                    if fit < 1:
                        crop = crop.resize((max(1, int(crop.width*fit)), max(1, int(crop.height*fit))), Image.Resampling.LANCZOS)
                    bar = 5./px_m*scale*fit
                    ImageDraw.Draw(crop).line([(6, crop.height-8), (6+bar, crop.height-8)], fill=(255, 255, 0), width=3)
                    sheet.paste(crop, (cx+10+k*(panel+10), cy+(panel-crop.height)//2))
            g, a, c = p['evidence']['geometry'], p['evidence']['appearance'], p['evidence']['context']
            lines = [f"#{start+i}  {sid.rsplit('-', 2)[-2]}-{sid.rsplit('-', 1)[-1]}  {row['object_id'][:8]}",
                     f"hint {p['sub_kind']} ({p['confidence']})  {g['area_m2']:.1f} m2  {g['rect_width_m']} x {g['rect_length_m']} m",
                     f"rect {g['rectilinearity']}  circ {g['circularity']}  v {g['vertex_count']}  ribs {a.get('rib_anisotropy')}  irv {a.get('interior_relative_variance')}",
                     f"bright {a.get('brightness_median')}  attached {c['attached_to_larger_roof']}  flag {c['secondary_structure_flag']}  nearest larger {c.get('nearest_larger_roof_m')} m",
                     'left: original   right: enhanced contrast   yellow bar 5 m']
            for j, text in enumerate(lines):
                draw.text((cx+10, cy+panel+6+j*(font_size+4)), text, fill=(235, 235, 235) if j else (250, 204, 21), font=font if j < 4 else small)
        path = out_dir/f'page-{start//per_page:02d}.png'
        sheet.save(path)
        pages.append(path)
    (out_dir/'index.json').write_text(json.dumps([{'index': i, 'scene_id': r['scene_id'], 'object_id': r['object_id'],
                                                   'hint': r['payload']['sub_kind'], 'confidence': r['payload']['confidence']}
                                                  for i, r in enumerate(rows)], indent=1))
    return pages


def observation_rows(root, observations, *, reviewer, tier='agent', session=None):
    """Observations [{scene_id, object_id, observed, confidence?, note?}] -> index rows joined to the current hint."""
    hints = {(r['scene_id'], r['object_id']): r for r in hint_rows(root)}
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    rows = []
    for ob in observations:
        if ob['observed'] not in OBSERVED_KINDS:
            raise ValueError(f"unknown observed kind {ob['observed']!r}; allowed: {OBSERVED_KINDS}")
        key = (ob['scene_id'], ob['object_id'])
        hint = hints.get(key)
        payload = {'version': version_for(tier), 'note': OBSERVATION_NOTE if TIERS[tier] == 3 else 'human reference observation',
                   'reviewer': reviewer, 'tier': TIERS[tier], 'tier_name': tier, 'session': session,
                   'observed': ob['observed'], 'observed_confidence': ob.get('confidence', 'moderate'), 'observer_note': ob.get('note'),
                   'hint_version': sk.VERSION, 'hint': hint['payload']['sub_kind'] if hint else None,
                   'hint_confidence': hint['payload']['confidence'] if hint else None,
                   'hint_code_sha256': hint['payload'].get('code_sha256') if hint else None,
                   'agrees': (hint['payload']['sub_kind'] == ob['observed']) if hint else None,
                   'training_eligible': TIERS[tier] <= 2, 'computed_at': stamp, 'code_sha256': code}
        scene = {'id': ob['scene_id'], 'model_signature': hint['model_signature'] if hint else None}
        rows.append(_row(scene, {'id': ob['object_id']}, sk.plain(payload), version_for(tier)))
    return rows


def record(root, observations, *, reviewer, tier='agent', session=None):
    rows = observation_rows(root, observations, reviewer=reviewer, tier=tier, session=session)
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        written = eyes_index.replace_rows(db, TABLE, rows)   # keyed per object+version: never touches other versions
    return rows, written


def agreement(root, *, reviewer=None, session=None):
    """Observation-vs-hint counts: overall, per hint kind (rows, agree), and the confusion pairs."""
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = review_rows(db)
    rows = [r for r in rows if (reviewer is None or r['payload'].get('reviewer') == reviewer)
            and (session is None or r['payload'].get('session') == session)]
    per_hint, confusion, per_tier = {}, Counter(), Counter()
    agree = 0
    for r in rows:
        p = r['payload']
        per_tier[p.get('tier_name')] += 1
        entry = per_hint.setdefault(p.get('hint'), {'rows': 0, 'agree': 0})
        entry['rows'] += 1
        if p.get('agrees'):
            entry['agree'] += 1
            agree += 1
        confusion[f"{p.get('hint')} -> {p['observed']}"] += 1
    return {'versions': list(REVIEW_VERSIONS), 'note': 'agreement between observations and rule hints; neither side is ground truth',
            'rows': len(rows), 'agree': agree, 'per_hint': per_hint, 'confusion': dict(confusion), 'per_tier': dict(per_tier)}


def export_training(root, out_dir, *, min_tier=2, include_agent=False, scale=1, pad_m=3.):
    """Crops + masks + manifest for observed objects. Agent rows are exported only with include_agent and are
    marked training_eligible=false in the manifest; human rows (tier <= min_tier) are eligible."""
    root, out_dir = Path(root), Path(out_dir)
    (out_dir/'crops').mkdir(parents=True, exist_ok=True)
    (out_dir/'masks').mkdir(parents=True, exist_ok=True)
    with closing(eyes_index.connection(root, readonly=True)) as db:
        rows = review_rows(db)
    manifest, scenes, images = [], {}, {}
    for r in rows:
        p = r['payload']
        if p['tier'] > min_tier and not include_agent:
            continue
        sid = r['scene_id']
        if sid not in scenes:
            scenes[sid] = sk.load_scene(root, sid)
            images[sid] = sk.load_image(root, scenes[sid])
        scene, image = scenes[sid], images[sid]
        obj = next((o for o in scene['objects'] if o['id'] == r['object_id']), None)
        if image is None or obj is None:
            continue
        ring = np.asarray(obj['ring'], dtype=float)
        pad = pad_m/sk.pixel_size_m(scene)
        x0, y0 = max(int(ring[:, 0].min()-pad), 0), max(int(ring[:, 1].min()-pad), 0)
        x1, y1 = min(int(ring[:, 0].max()+pad)+1, image.shape[1]), min(int(ring[:, 1].max()+pad)+1, image.shape[0])
        name = f"{sid}--{r['object_id']}"
        Image.fromarray(image[y0:y1, x0:x1]).save(out_dir/'crops'/f'{name}.png')
        mask = Image.new('L', (x1-x0, y1-y0), 0)
        ImageDraw.Draw(mask).polygon([(x-x0, y-y0) for x, y in ring], fill=255)
        mask.save(out_dir/'masks'/f'{name}.png')
        manifest.append({'scene_id': sid, 'object_id': r['object_id'], 'crop': f'crops/{name}.png', 'mask': f'masks/{name}.png',
                         'crop_origin_px': [x0, y0], 'pixel_size_m': sk.pixel_size_m(scene), 'observed': p['observed'],
                         'observed_confidence': p.get('observed_confidence'), 'tier': p['tier'], 'tier_name': p.get('tier_name'),
                         'reviewer': p.get('reviewer'), 'training_eligible': bool(p['tier'] <= min_tier), 'rule_hint': p.get('hint'),
                         'image_sha256': scene.get('image_sha256')})
    summary = {'version': REVIEW_VERSION, 'exported': len(manifest), 'training_eligible': sum(m['training_eligible'] for m in manifest),
               'note': 'agent observations are exported for inspection only; only tier <= min_tier rows are training eligible',
               'by_observed': dict(Counter(m['observed'] for m in manifest)), 'items': manifest}
    (out_dir/'manifest.json').write_text(json.dumps(summary, indent=1))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='Agent/human review loop for structure-kinds hints (observations, not labels).')
    parser.add_argument('--root', default='.')
    sub = parser.add_subparsers(dest='command', required=True)
    cards = sub.add_parser('cards', help='Render review pages for a sample JSON ([[scene_id, object_id], ...]) or sub-kinds.')
    cards.add_argument('--sample', help='JSON list of [scene_id, object_id(, ...)] pairs.')
    cards.add_argument('--sub-kinds', nargs='*')
    cards.add_argument('--out', default=str(OUT_DIR/'cards'))
    cards.add_argument('--per-page', type=int, default=8)
    rec = sub.add_parser('record', help='Record observations from a JSON file [{scene_id, object_id, observed, confidence, note}].')
    rec.add_argument('--observations', required=True)
    rec.add_argument('--reviewer', required=True, help='e.g. agent:claude-fable-5.1 or human:<name>')
    rec.add_argument('--tier', choices=sorted(TIERS), default='agent')
    rec.add_argument('--session')
    agr = sub.add_parser('agreement')
    agr.add_argument('--reviewer')
    agr.add_argument('--session')
    exp = sub.add_parser('export')
    exp.add_argument('--out', default=str(OUT_DIR/'training'))
    exp.add_argument('--include-agent', action='store_true')
    exp.add_argument('--min-tier', type=int, default=2)
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.command == 'cards':
        keys = [tuple(k[:2]) for k in json.loads(Path(args.sample).read_text())] if args.sample else None
        rows = hint_rows(root, keys, args.sub_kinds)
        pages = render_cards(root, rows, args.out, per_page=args.per_page)
        out = {'rows': len(rows), 'pages': [str(p) for p in pages]}
    elif args.command == 'record':
        rows, written = record(root, json.loads(Path(args.observations).read_text()), reviewer=args.reviewer, tier=args.tier, session=args.session)
        out = {'written': written, 'agree': sum(1 for r in rows if r['payload']['agrees']), 'rows': len(rows)}
    elif args.command == 'agreement':
        out = agreement(root, reviewer=args.reviewer, session=args.session)
    else:
        out = export_training(root, args.out, min_tier=args.min_tier, include_agent=args.include_agent)
        out = {k: v for k, v in out.items() if k != 'items'}
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
