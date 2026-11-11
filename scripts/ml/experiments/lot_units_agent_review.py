"""Trial agent review (reviewer 2026-10-13: "run a review with haiku-5-5 and annotate agent review and corrections, then ill compare
with my review") of the Units (units-2026-10-13) and Lots (lot-bound-2026-10-13) queues.

Per item: a check image (the photo crop twice; right panel: CURRENT saved outline grey, PROPOSED cyan, aligned Vicmap lot lines red,
a 64 px grid). Claude Haiku 5.5 (`claude -p`, Claude Code's own login, only the Read tool) answers right / partly right / wrong /
unclear with a note and, when not right, a corrected outline as corner points in the right panel's pixels. Corrections are mapped
back to scene pixels and kept in <out>/reviews.json (agent reviews cannot carry geometry); --post appends each assessment to its
Fieldwork item with SaveAgentReview (annotationId = the PROPOSED outline; never human status, geometry or training eligibility).
"""
import argparse, json, subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw
from shapely.geometry import shape
from shapely.ops import transform

MODEL = 'claude-haiku-5-5'
SOURCE = f'agent-review:{MODEL}:lot-units-v2'
T = 384
SYSTEM = ('You review roof outlines drawn on aerial photos against legal property (lot) boundaries. You answer only with the JSON '
          'object asked for. Read the image file you are given with the Read tool before answering.')
ASK = {
    'unit': """Read the image {path}. It shows one aerial photo crop twice: plain on the left; on the right the same crop with
- a GREY outline: the roof outline saved today (it may cover several dwellings),
- a CYAN outline: the PROPOSED outline of the building part on ONE lot (a dwelling, or a garage / shed / outbuilding that
  stands on its own lot),
- RED lines: legal property (lot) boundaries,
- faint grid lines every 64 px (pixel coordinates start at 0,0 top-left of the RIGHT panel; the panel is 384 x 384).

Judge the CYAN outline: it should outline the roof of the building on this one red lot and follow the roof's visible edges.
A garage or shed on its own lot is a correct answer; a lot line running through one house with no wall, gutter or roof change
along it is not.
- "right": this lot's building, inside its lot, edges on the roof.
- "partly right": the right building, but an edge is off (cuts into the roof, takes in a neighbour's roof or the ground).
- "wrong": the lot line splits one building that shows no separate unit along it (one house, one warehouse bay strip), or the
  lot holds no roof.
- "unclear": trees, shadow or blur hide too much.
If not "right", give the corrected outline as corner points [x, y] in RIGHT-panel pixels (clockwise, 4-12 points), inside the red lot.

Answer with only this JSON: {{"verdict": "...", "note": "<one or two sentences: what is right or off>", "corrected": [[x, y], ...] or null}}""",
    'lot': """Read the image {path}. It shows one aerial photo crop twice: plain on the left; on the right the same crop with
- a GREY outline: the roof outline saved today, which runs over a legal property (lot) boundary,
- a CYAN outline: the PROPOSED outline, the saved outline clipped to its lot,
- RED lines: legal property (lot) boundaries,
- faint grid lines every 64 px (pixel coordinates start at 0,0 top-left of the RIGHT panel; the panel is 384 x 384).

Judge the CYAN outline: it should follow this building's roof and stay inside its red lot.
- "right": the clipped outline is a good outline of the building inside its lot.
- "partly right": clip is right but an edge is off (the red line itself sits off the fence / wall here, or the clip cuts the roof).
- "wrong": keep the grey outline (e.g. one structure genuinely built over the boundary) or the outline is not a building.
- "unclear": trees, shadow or blur hide too much.
If not "right", give the corrected outline as corner points [x, y] in RIGHT-panel pixels (clockwise, 4-12 points).

Answer with only this JSON: {{"verdict": "...", "note": "<one or two sentences>", "corrected": [[x, y], ...] or null}}""",
}
DECISION = {'right': 'approved', 'partly right': 'needs_adjustment', 'wrong': 'rejected', 'unclear': 'uncertain'}


def tile(x, out_path):
    from scripts.ml import parcel_units as pu
    from scripts.ml.parcel_align import aligned_projection
    L = json.load(open(f"data/scenes/{x['scene']}/latest.json"))
    img = Image.open(f"data/scenes/{x['scene']}/{L['image_sha256']}.png").convert('RGB')
    R = x['current_ring']
    xs = [p[0] for p in R] + [p[0] for p in x['proposed_ring']]; ys = [p[1] for p in R] + [p[1] for p in x['proposed_ring']]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.75 + 10
    box = (cx - half, cy - half, cx + half, cy + half)
    k = T / (2 * half)
    f = lambda r: [((a - box[0]) * k, (b - box[1]) * k) for a, b in r]
    crop = img.crop(tuple(int(round(v)) for v in box)).resize((T, T), Image.BICUBIC)
    drawn = crop.copy(); d = ImageDraw.Draw(drawn)
    for g in range(64, T, 64):
        d.line([(g, 0), (g, T)], fill=(255, 255, 255), width=1); d.line([(0, g), (T, g)], fill=(255, 255, 255), width=1)
    proj, _ = aligned_projection(L)
    for ft in pu.scene_parcels(L)['features']:
        geo = transform(proj, shape(ft['geometry'])).buffer(0)
        for poly in getattr(geo, 'geoms', [geo]):
            d.line(f(list(poly.exterior.coords)), fill=(255, 40, 40), width=2)
    q = f(R); d.line(q + q[:1], fill=(170, 170, 170), width=2)
    q = f(x['proposed_ring']); d.line(q + q[:1], fill=(0, 230, 255), width=3)
    out = Image.new('RGB', (2 * T + 8, T), (0, 0, 0))
    out.paste(crop, (0, 0)); out.paste(drawn, (T + 8, 0))
    out.save(out_path)
    return {'box': box, 'k': k}


def ask(family, path):
    cmd = ['claude', '-p', '--model', MODEL, '--system-prompt', SYSTEM, '--strict-mcp-config', '--tools', 'Read',
           '--allowedTools', 'Read', '--output-format', 'json', ASK[family].format(path=path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(Path(path).parent))
    d = json.loads(r.stdout)
    if d.get('is_error'):
        raise RuntimeError(str(d.get('result'))[:200])
    if MODEL not in (d.get('modelUsage') or {}):
        raise RuntimeError(f"answered by {list((d.get('modelUsage') or {}).keys())}")
    text = d['result'].strip()
    obj = json.loads(text[text.index('{'): text.rindex('}') + 1])
    if obj.get('verdict') not in DECISION:
        raise RuntimeError(f'bad verdict {obj!r}')
    return obj, d.get('total_cost_usd')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--limit', type=int)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--reviewer', default='haiku-5-5-trial')
    ap.add_argument('--post', action='store_true', help='append the assessments to Fieldwork (SaveAgentReview)')
    args = ap.parse_args()
    B = Path('data/reviews/eyes/experiments/parcel-units-2026-10-12')
    receipts = {p.stem for p in Path('data/fieldwork-sync/mooroolbark-review/review/items').glob('r1013-*.json')}
    rows = []
    rv = B / 'revalidate-2026-10-13.json'  # items withdrawn on 2026-10-13 are not reviewed again
    withdrawn = {k for fam in (json.load(open(rv)) if rv.exists() else {}).values() for k, v in fam.items() if not v['keep']}
    for path, fam, prefix in ((B / 'queue-v2/items.json', 'unit', 'r1013-9-unit-'), (B / 'queue-v2b/items.json', 'unit', 'r1013-9-unit-'),
                              (B / 'lots-queue-v1/items.json', 'lot', 'r1013-10-lot-'), (B / 'lots-queue-v2/items.json', 'lot', 'r1013-10-lot-')):
        for x in json.load(open(path))[fam]:
            if fam == 'unit' and x['line'] in (50, 51):  # replaced by 178-186
                continue
            ids = [i for i in receipts if i.startswith(f"{prefix}{x['line']:03d}-p{x['proposal_id'][:8]}")]
            if len(ids) != 1:
                continue
            if ids[0] in withdrawn:
                continue
            rows.append((fam, x, ids[0]))
    rows = rows[:args.limit] if args.limit else rows
    out = Path(args.out); (out / 'tiles').mkdir(parents=True, exist_ok=True)
    done = json.loads((out / 'reviews.json').read_text()) if (out / 'reviews.json').exists() else {}

    def one(row):
        fam, x, item_id = row
        if item_id in done:
            return item_id, done[item_id], None
        p = (out / 'tiles' / f'{item_id}.png').resolve()
        frame = tile(x, p)
        try:
            obj, cost = ask(fam, str(p))
        except Exception as e:  # noqa: BLE001 - recorded, retried on the next run
            return item_id, None, str(e)
        corrected = None
        if obj.get('corrected'):
            bx, k = frame['box'], frame['k']
            corrected = [[round(px / k + bx[0], 2), round(py / k + bx[1], 2)] for px, py in obj['corrected']]
        return item_id, {'family': fam, 'line': x['line'], 'scene': x['scene'], 'object_id': x['object_id'],
                         'annotation_id': f"new-{x['proposal_id'] if fam == 'unit' else x['object_id']}",
                         'verdict': obj['verdict'], 'note': str(obj.get('note', ''))[:600], 'corrected_scene_px': corrected,
                         'source': SOURCE, 'cost_usd': cost}, None
    failed = []
    with ThreadPoolExecutor(args.workers) as ex:
        for item_id, rec, err in ex.map(one, rows):
            if rec:
                done[item_id] = rec
            else:
                failed.append((item_id, err))
            (out / 'reviews.json').write_text(json.dumps(done, indent=1))
    print(json.dumps({'items': len(rows), 'reviewed': len(done), 'failed': failed[:5], 'n_failed': len(failed),
                      'cost_usd': round(sum(r.get('cost_usd') or 0 for r in done.values()), 3)}))
    if args.post:
        from scripts.ml import fieldwork_review as fr
        client = fr.Client(fr.DEFAULT_REMOTE_URL, fr.guard_origin(fr.DEFAULT_REMOTE_URL, False, None, '.'))
        posted = 0
        for item_id, rec in done.items():
            if rec.get('posted'):
                continue
            status, body = client.rpc('GetItem', {'id': item_id}); assert status == 200, (item_id, status)
            item = body['item']
            note = f"Haiku 5.5 ({SOURCE.split(':')[-1]}, {rec['verdict']}): {rec['note']}"
            if rec['corrected_scene_px']:
                note += f" Correction (scene px, {len(rec['corrected_scene_px'])} corners): {json.dumps(rec['corrected_scene_px'])}"
            status, body = client.rpc('SaveAgentReview', {'itemId': item_id, 'annotationId': rec['annotation_id'],
                                                          'expectedRevision': item['revision'], 'reviewer': args.reviewer,
                                                          'decision': DECISION[rec['verdict']], 'notes': note[:4000]})
            assert status == 200, (item_id, status, body)
            rec['posted'] = True; posted += 1
            (out / 'reviews.json').write_text(json.dumps(done, indent=1))
        print(json.dumps({'posted': posted}))


if __name__ == '__main__':
    main()
