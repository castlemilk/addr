"""Fieldwork integration: review items and human words become cases; adopted assessments return as agent reviews.

    build_cases   pulled decisions + push receipts + local scenes -> data/harness/cases-fieldwork
                  `fr-<item>` assessment cases: current and proposed outline -> approved / rejected /
                  needs_adjustment / uncertain, held out against the human word (and the Codex review)
                  `fo-<item>` outline cases: the saved ring -> the approved proposal or the human edit
    post_reviews  adopted `fr-` assessments -> SaveAgentReview on the same review items (dry run by default)

Rings come from the staged frame blob (verified against the receipt's ring hashes) or, when that blob is
not local, from the pushed annotation. Agent hints in item titles and metadata never enter a case. Agent
reviews are append-only agent history: they never set human status, checks or training eligibility.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from .. import fieldwork_review as fr
from . import cases, schedule
from .cases import PROTECTED_SCENES

ROOT = Path('data/harness/cases-fieldwork')
RECEIPTS = Path('data/fieldwork-sync') / fr.DEFAULT_PROJECT / 'review' / 'items'
STAGES = Path('.run/fieldwork-review') / fr.SET
TRACE_STAGE = Path('data/harness/fieldwork/stage')  # trace bundles; never the shared .run LATEST pointer
QUEUE = Path('data/harness/fieldwork/queue')
PULLS = Path('data/reviews/eyes/experiments')
DECISIONS = (PULLS/'review-2026-10-08/fieldwork/decisions-1766.json', PULLS/'roof-corners-2026-10-10/pull-3149-decisions.json')
AGENT_REVIEWS = (PULLS/'review-2026-10-08/fieldwork/agent-reviews-1766.json',)
FAMILIES = ('shadow', 'cut', 'corner', 'lot', 'trace')  # one current and one proposed outline per item
# Outline references must be visible boundaries: corner targets are right-angle simplifications and lot
# targets are legal-boundary clips, so only these families also yield `fo-` tracing cases.
OUTLINE_FAMILIES = ('shadow', 'cut', 'trace')
PULL_OUT = Path('data/harness/fieldwork/pulls')
DECISION_OF_WORD = {'right': 'approved', 'wrong': 'rejected', 'partly': 'needs_adjustment',
                    'unclear': 'needs_adjustment', 'edited': 'needs_adjustment'}
REVIEW_DECISIONS = ('approved', 'rejected', 'needs_adjustment', 'uncertain')
POSTS = Path('data/harness/fieldwork/agent-reviews')
NOTES_LIMIT = 9000


def _scene(ring):
    return [[round(x, 3), round(y, 3)] for x, y in ring]


def item_rings(receipt, root='.'):
    """(current, proposed, verified) outlines in scene pixels for one pushed review item."""
    doc, meta = receipt['document'], receipt['document']['metadata']['addr']
    crop, object_id = meta['crop'], meta['objectIds'][0]
    digest = str((doc['source'].get('model') or {}).get('sha256'))
    blob = next((b for folder in (STAGES, TRACE_STAGE)
                 if (b := Path(root)/folder/str(receipt.get('stageSha'))/'blobs'/digest).is_file()), Path('/nonexistent'))
    if blob.is_file():
        objects = {o['id']: o for o in json.loads(blob.read_text())['objects']}
        shift = [crop['x0'], crop['y0']]

        def outline(tag):
            obj = objects[f'{tag}-{object_id}']
            ring = _scene([[x + shift[0], y + shift[1]] for x, y in obj['ring']])
            holes = [_scene([[x + shift[0], y + shift[1]] for x, y in hole]) for hole in obj.get('holes') or []]
            return {'ring': ring, 'holes': holes}
        current, proposed = outline('cur'), outline('new')
    else:
        marks = {a['id']: a for a in doc['annotations']}

        def outline(tag):
            rings = [_scene([[p[0]*crop['width'] + crop['x0'], p[1]*crop['height'] + crop['y0']] for p in ring[:-1]])
                     for ring in marks[f'{tag}-{object_id}']['geometry']['coordinates']]
            return {'ring': rings[0], 'holes': rings[1:]}
        current, proposed = outline('cur'), outline('new')
    verified = (fr.ring_sha(proposed['ring'], proposed['holes']) == meta.get('proposedRingSha')
                or fr.ring_sha(proposed['ring'], []) == meta.get('proposedRingSha'))
    return current, proposed, verified


def _rows(paths, key):
    out = {}
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        for row in data.get(key) or []:
            previous = out.get(row['itemId'])
            if previous is None or int(data.get('cursor') or 0) >= previous[0]:
                out[row['itemId']] = (int(data.get('cursor') or 0), row)
    return {item: row for item, (_cursor, row) in out.items()}


def default_decisions(root='.'):
    return (*DECISIONS, *sorted((Path(root)/PULL_OUT).glob('decisions-*.json')))


def default_agent_reviews(root='.'):
    return (*AGENT_REVIEWS, *sorted((Path(root)/PULL_OUT).glob('agent-reviews-*.json')))


def pull(root='.', origin=None, token_file=None):
    """Read-only pull of the latest human words and agent reviews into PULL_OUT (fieldwork_review.pull)."""
    out = Path(root)/PULL_OUT
    out.mkdir(parents=True, exist_ok=True)
    return fr.pull(root=str(root), url=origin, out=str(out), token_file=token_file)


def decisions_by_item(paths=DECISIONS):
    """The newest human decision per review item across pulled decision files."""
    return _rows(paths, 'decisions')


def codex_by_item(paths=AGENT_REVIEWS):
    """The current Codex agent review per item (a comparison baseline, never a label)."""
    rows = _rows(paths, 'reviews')
    return {item: row for item, row in rows.items() if row.get('reviewer') == 'codex' and row.get('current', True)}


def _question(doc):
    """The reviewer's question without any agent hint: the title before ' · agent:' and the '#' instructions."""
    title = re.split(r'\s+·\s+agent:', doc.get('title') or '')[0].strip()
    lines = [line.lstrip('# ').strip() for line in (doc.get('notes') or '').splitlines() if line.startswith('#')]
    return {'title': title, 'reviewer_instructions': [line for line in lines if line]}


def build_cases(root='.', dest=None, *, decisions=None, agent_reviews=None, receipts=None,
                families=FAMILIES, tasks=('review', 'outline'), include_undecided=False, limit_per_domain=None):
    """Rebuild the Fieldwork case set; returns the index written (same layout as cases.build)."""
    root = Path(root)
    dest = Path(dest) if dest else root/ROOT
    receipts = Path(receipts) if receipts else root/RECEIPTS
    words = decisions_by_item(decisions or default_decisions(root))
    codex = codex_by_item(agent_reviews or default_agent_reviews(root))
    entries, skipped = {}, {}
    for path in sorted(receipts.glob('*.json')):
        receipt = json.loads(path.read_text())
        family, item_id = receipt.get('family'), receipt['itemId']
        meta = receipt['document']['metadata']['addr']
        if family not in families or len(meta.get('objectIds') or []) != 1:
            continue
        decision = words.get(item_id)
        word = (decision or {}).get('word')
        if decision is None and not include_undecided:
            continue
        if decision is not None and word not in DECISION_OF_WORD:
            skipped[item_id] = f'word {word!r}'
            continue
        scene_id, base = meta['sceneId'], meta['base']
        folder = root/'data/scenes'/scene_id
        model_path = folder/'models'/f'{base["modelSignature"]}.json'
        image = folder/f'{base["imageSha256"]}.png'
        if not model_path.is_file() or not image.is_file():
            skipped[item_id] = 'scene model or image missing'
            continue
        model = json.loads(model_path.read_text())
        try:
            current, proposed, verified = item_rings(receipt, root)
        except (KeyError, IndexError, TypeError) as exc:
            skipped[item_id] = f'rings unreadable: {exc}'
            continue
        common = {'version': cases.VERSION, 'scene_id': scene_id, 'object_id': meta['objectIds'][0],
                  'kind': meta.get('objectKind') or 'building', 'family': family, 'image': 'image.png',
                  'image_sha256': base['imageSha256'], 'source_frame': cases.frame_of(model),
                  'pixel_size_m': cases.pixel_size_m(model),
                  'fieldwork': {'item_id': item_id, 'annotation_id': f'new-{meta["objectIds"][0]}',
                                'project_id': receipt['projectId'], 'rings_verified': verified}}
        human = DECISION_OF_WORD.get(word)
        if 'review' in tasks:
            case = {**common, 'case_id': f'fr-{item_id}', 'task': 'review', 'domain': f'fieldwork-{family}-review',
                    'start_outline_pixels': current, 'current_outline_pixels': current,
                    'proposed_outline_pixels': proposed, 'question': _question(receipt['document'])}
            heldout = None if decision is None else {
                'version': cases.VERSION, 'case_id': case['case_id'], 'human_decision': human, 'human_word': word,
                'fieldwork_cursor': (decision.get('fieldwork') or {}).get('pullCursor'),
                'codex_decision': (codex.get(item_id) or {}).get('decision')}
            entries[case['case_id']] = (case, heldout, image)
        edited = (decision or {}).get('editedRingFrame') or []
        if 'outline' in tasks and family in OUTLINE_FAMILIES and (word == 'right' or (word == 'edited' and edited)):
            reference = proposed if word == 'right' else {'ring': _scene(edited[0]),
                                                          'holes': [_scene(h) for h in edited[1:]]}
            case = {**common, 'case_id': f'fo-{item_id}', 'task': 'outline', 'domain': f'fieldwork-{family}-outline',
                    'start_outline_pixels': current, 'machine': {'source': 'saved outline before the review proposal'}}
            heldout = {'version': cases.VERSION, 'case_id': case['case_id'], 'reference_outline_pixels': reference,
                       'change': 'unchanged' if reference == current else 'edited', 'human_status': word,
                       # A 'right' answer confirms an agent's geometry, which carries that agent's style; an edit
                       # puts the reviewer's own geometry on the edited edges.
                       'reference_origin': 'agent-drawn, human-confirmed' if word == 'right' else 'human-edited',
                       'reference_vertices': len(reference['ring']), 'boundary_reference': True}
            entries[case['case_id']] = (case, heldout, image)
    return _write(dest, entries, limit_per_domain, skipped)


def _write(dest, entries, limit_per_domain, skipped):
    by_domain = {}
    for case_id in sorted(entries):
        by_domain.setdefault(entries[case_id][0]['domain'], []).append(case_id)
    keep = {c for ids in by_domain.values() for c in (ids[:limit_per_domain] if limit_per_domain else ids)}
    if dest.is_dir():
        for folder in dest.iterdir():
            if folder.is_dir() and folder.name not in keep and (folder/'source'/'case.json').is_file():
                shutil.rmtree(folder)
    index = {}
    for case_id in sorted(keep):
        case, heldout, image = entries[case_id]
        cases.write_case(dest, case, heldout, image)
        index[case_id] = {'task': case['task'], 'domain': case['domain'], 'scene_id': case['scene_id'],
                          'object_ids': [case['object_id']], 'split': cases.split_of(case['scene_id']),
                          'change': (heldout or {}).get('change') or ((heldout or {}).get('human_decision') or 'undecided'),
                          'kind': case['kind'], 'fieldwork_item': case['fieldwork']['item_id']}
    dest.mkdir(parents=True, exist_ok=True)
    (dest/'index.json').write_text(json.dumps({'version': cases.VERSION, 'cases': index, 'skipped': skipped},
                                              indent=2, sort_keys=True) + '\n')
    return index


def reviewer_id(adapter, tag=''):
    name = f'harness-{adapter.executor}' + (f'-{tag}' if tag else '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', name):
        raise ValueError(f'reviewer id {name!r} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,127}}')
    return name


def assessments(adapter, runs_root, ns):
    """(case, submission, selection) for every adopted assessment of a Fieldwork review case in `ns`."""
    agg = schedule.Namespace(ns.adapter, ns.executor, 'agg', ns.k, ns.seed, ns.tag)
    for path in sorted((Path(runs_root)/str(agg)).glob('*/summary.json')):
        summary = json.loads(path.read_text())
        sel = (summary.get('selections') or {}).get(ns.mode) or {}
        task_id = summary['task_id']
        if not task_id.startswith('fr-') or not sel.get('ok') or not sel.get('chosen_run_dir'):
            continue
        submission = adapter.submission(Path(sel['chosen_run_dir']))
        if submission and submission.get('task') == 'review':
            yield adapter.case(task_id), submission, sel


def payload_for(case, submission, sel, *, reviewer, revision, namespace):
    provenance = (f'Harness² assessment ({namespace}, run {sel["chosen_run_id"]}, harness '
                  f'{sel.get("harness_fingerprint", "?")}); offline case {case["case_id"]}, original image '
                  f'{case["image_sha256"][:16]}. Agent history only.')
    notes = f'{submission["notes"].strip()}\n\n{provenance}'
    while len(notes.encode()) > NOTES_LIMIT:
        notes = notes[:-200]
    return {'itemId': case['fieldwork']['item_id'], 'annotationId': case['fieldwork']['annotation_id'],
            'expectedRevision': revision, 'reviewer': reviewer, 'decision': submission['decision'], 'notes': notes}


def _digest(payload):
    stable = {k: v for k, v in payload.items() if k != 'expectedRevision'}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def post_reviews(adapter, runs_root, ns, *, origin=fr.DEFAULT_REMOTE_URL, reviewer=None, dry_run=True,
                 token_file=None, limit=None, client=None, root='.', posts=None):
    """SaveAgentReview for each adopted assessment; identical re-posts are skipped by receipt."""
    token = None if client else fr.guard_origin(origin, dry_run, token_file, root)
    client = client or fr.Client(origin, token)
    reviewer = reviewer or reviewer_id(adapter, ns.tag)
    host = urllib.parse.urlsplit(origin).hostname or 'local'
    folder = Path(root)/(posts or POSTS)/host
    results = []
    for case, submission, sel in assessments(adapter, runs_root, ns):
        if limit is not None and len([r for r in results if r['outcome'] in ('posted', 'planned')]) >= limit:
            break
        item_id = case['fieldwork']['item_id']
        receipt = folder/f'{item_id}.json'
        status, body = client.rpc('GetItem', {'id': item_id})
        if status != 200:
            results.append({'itemId': item_id, 'outcome': 'item_unreadable', 'status': status, 'body': body})
            continue
        payload = payload_for(case, submission, sel, reviewer=reviewer, revision=body['item']['revision'],
                              namespace=str(ns))
        if receipt.is_file() and json.loads(receipt.read_text()).get('digest') == _digest(payload):
            results.append({'itemId': item_id, 'outcome': 'skip_receipt'})
            continue
        if dry_run:
            results.append({'itemId': item_id, 'outcome': 'planned', 'payload': payload})
            continue
        for _attempt in range(2):
            status, saved = client.rpc('SaveAgentReview', payload)
            if status != 409:
                break
            status, body = client.rpc('GetItem', {'id': item_id})
            payload['expectedRevision'] = body['item']['revision']
        outcome = 'posted' if status == 200 else 'failed'
        row = {'itemId': item_id, 'outcome': outcome, 'status': status, 'decision': payload['decision'],
               'agentReview': (saved.get('agentReview') or {}).get('id') if isinstance(saved, dict) else None}
        if outcome == 'posted':
            folder.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({'digest': _digest(payload), 'origin': origin, 'postedAt': datetime.now(UTC)
                                           .isoformat(), 'payload': payload, 'response': row}, indent=2))
        else:
            row['body'] = saved
        results.append(row)
    return results


def _iou(a, b):
    from shapely.geometry import Polygon
    pa, pb = Polygon(a['ring'], a.get('holes') or []).buffer(0), Polygon(b['ring'], b.get('holes') or []).buffer(0)
    union = pa.union(pb).area
    return pa.intersection(pb).area/union if union else 1.0


def rank_traces(adapter, rows):
    """Per case: the medoid outline of its repeats, their disagreement and the edit size, most informative first.

    Disagreement (mean pairwise 1 - IoU between repeats) is the agent's own uncertainty: where it disagrees
    with itself a human answer teaches the most. Cases whose repeats all kept the saved outline are skipped.
    """
    by_case, skipped = {}, {}
    for row in rows:
        if row['task_id'] not in adapter.index:
            skipped[row['task_id']] = 'no longer in the case index'
            continue
        run_dir = Path(row['run_dir']) if row.get('run_dir') else None
        if run_dir and (sub := adapter.submission(run_dir)) and sub.get('task') == 'outline':
            meta = json.loads((run_dir/'meta.json').read_text()) if (run_dir/'meta.json').is_file() else {}
            by_case.setdefault(row['task_id'], []).append({**sub, '_harness': meta.get('harness'),
                                                           '_model': meta.get('model'), '_run_dir': str(run_dir)})
    ranked = []
    for task_id, subs in sorted(by_case.items()):
        case = adapter.case(task_id)
        start = case['start_outline_pixels']
        changed = [s for s in subs if not s.get('kept_start_outline')]
        if not changed:
            skipped[task_id] = 'every repeat kept the saved outline'
            continue
        outlines = [s['outline_pixels'] for s in subs]
        pairs = [(i, j) for i in range(len(outlines)) for j in range(i + 1, len(outlines))]
        disagreement = sum(1 - _iou(outlines[i], outlines[j]) for i, j in pairs)/len(pairs) if pairs else 0.0
        medoid = max(changed, key=lambda s: sum(_iou(s['outline_pixels'], o) for o in outlines))
        ranked.append({'task_id': task_id, 'case': case, 'submission': medoid, 'repeats': len(subs),
                       'changed': len(changed), 'disagreement': round(disagreement, 4),
                       'edit': round(1 - _iou(medoid['outline_pixels'], start), 4)})
    ranked.sort(key=lambda r: (-r['disagreement'], -r['edit'], r['task_id']))
    return ranked, skipped


def _live(root, scene_id, object_id):
    folder = Path(root)/'data/scenes'/scene_id
    source = folder/'review.json' if (folder/'review.json').is_file() else folder/'latest.json'
    return next((o for o in json.loads(source.read_text()).get('objects', []) if o.get('id') == object_id), None)


def next_trace_line(root, project=fr.DEFAULT_PROJECT):
    """1 + the highest trace line already pushed or queued: lines stay unique across batches."""
    lines = [0]
    for path in (Path(root)/'data/fieldwork-sync'/project/'review'/'items').glob('*-trace-*.json'):
        lines.append(int(json.loads(path.read_text()).get('line') or 0))
    for path in (Path(root)/QUEUE).glob('*.json'):
        lines += [row['line'] for row in json.loads(path.read_text()).get('trace', [])]
    return max(lines) + 1


def queue_traces(adapter, rows, *, label, root='.', limit=20, prefix='r1019-14', project=fr.DEFAULT_PROJECT,
                 context=None, harness=None):
    """Rank traces, write `<QUEUE>/<label>.json` in the fieldwork_review `trace` family format and stage it
    (local files only). Pushing is a separate, explicit step."""
    ranked, skipped = rank_traces(adapter, rows)
    line, items = next_trace_line(root, project), []
    for row in ranked:
        if len(items) >= limit:
            break
        case, sub = row['case'], row['submission']
        if case['scene_id'] in PROTECTED_SCENES:
            skipped[row['task_id']] = 'protected checkpoint scene'
            continue
        live = _live(root, case['scene_id'], case['object_id'])
        if live is None or live.get('status') in ('rejected', 'superseded'):
            skipped[row['task_id']] = 'the saved object is gone or no longer active'
            continue
        started_from_saved = {'ring': live['ring'], 'holes': live.get('holes') or []} == case['start_outline_pixels']
        outline = sub['outline_pixels']
        proposal_id = hashlib.sha256(json.dumps([case['scene_id'], case['object_id'], outline],
                                                sort_keys=True).encode()).hexdigest()[:16]
        notes = ' '.join(part for part in (sub.get('notes'), sub.get('uncertainty')) if part)
        items.append({'scene': case['scene_id'], 'line': line, 'object_id': case['object_id'],
                      'status_at_compute': live['status'], 'current_ring': live['ring'],
                      'current_holes': live.get('holes') or [], 'proposed_ring': outline['ring'],
                      'proposed_holes': outline['holes'], 'proposal_id': proposal_id, 'agent_notes': notes[:1500],
                      'source': 'harness-trace-v1',
                      'harness': {**(harness or {}), 'label': label, 'case': row['task_id'],
                                  'fingerprint': sub.get('_harness'), 'model': sub.get('_model'),
                                  'run_dir': sub.get('_run_dir'), 'started_from_saved': started_from_saved,
                                  'repeats': row['repeats'], 'disagreement': row['disagreement'], 'edit': row['edit']}})
        line += 1
    if not items:
        return {'queued': 0, 'skipped': skipped, 'ranked': len(ranked)}
    queue = Path(root)/QUEUE/f'{label}.json'
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(json.dumps({'trace': items, 'trace_set': {'prefix': prefix, 'tag': f'harness-traces-{label}'}},
                                indent=1))
    staged = fr.stage(root=str(root), items=str(queue.relative_to(root) if Path(root) != Path('.') else queue),
                      context=context or fr.DEFAULT_CONTEXT, out=str(TRACE_STAGE), project=project,
                      drop_stale=True, families=('trace',))
    return {'queued': len(items), 'queue': str(queue), 'stage': staged['stage'], 'stage_sha': staged['stageSha'],
            'dropped': staged['dropped'], 'skipped': skipped, 'ranked': len(ranked),
            'top': [{k: r[k] for k in ('task_id', 'disagreement', 'edit', 'changed', 'repeats')} for r in ranked[:5]]}
