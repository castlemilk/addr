"""Agent check for a Corners queue (roof corners card 2026-10-12, item 1): Claude Haiku 5.5 reads each item's check image and
writes a hint (right / partly right / wrong / unclear + one sentence) into the items file as agent_hint. Hints are shown to the
reviewer in Fieldwork; they never set a human answer or training eligibility.

Runs `claude -p` (Claude Code's own login) with a replaced system prompt and only the Read tool, one roof per call.
Resumable: lines that already have a hint from this checker are skipped."""
import argparse, json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL = 'claude-haiku-5-5'
SOURCE = f'agent-check:{MODEL}:corners-v1'
SYSTEM = ('You check roof outlines drawn on aerial photos. You answer only with the JSON object asked for. '
          'Read the image file you are given with the Read tool before answering.')
ASK = """Read the image {path}. It shows one aerial photo crop twice: plain on the left, and on the right the same crop with a
PROPOSED roof outline drawn in cyan (corner points dotted). The outline is meant to be the simplest right-angle drawing of the
one building roof in the middle of the crop.

Judge only the cyan outline against the roof you can see:
- "right": it follows the roof's outer edges; every real corner of the roof is a corner of the outline, and no corner is invented.
- "partly right": the right building and roughly the right shape, but a corner or edge is off - e.g. an L-shaped roof drawn as a
  rectangle, a notch that is really a tree or shadow, an edge cutting through the roof or running onto the ground.
- "wrong": not a building roof, or a different / merged building.
- "unclear": trees, shadow or blur hide too much of the roof to tell.

Answer with only this JSON: {{"verdict": "right" | "partly right" | "wrong" | "unclear", "note": "<one short sentence naming what
is off, or what you checked>"}}"""
VERDICTS = ('right', 'partly right', 'wrong', 'unclear')
# Second question (12 Oct, reviewer found merged terrace / townhouse units in queue 2): does one outline span several units?
UNITS_SOURCE = f'agent-check:{MODEL}:units-v1'
UNITS_ASK = """Read the image {path}. It shows one aerial photo crop twice: plain on the left, and on the right the same crop with a
PROPOSED roof outline drawn in cyan.

Question: does the cyan outline cover MORE THAN ONE building or dwelling unit? Count separate houses, attached townhouses or
terrace units (separate roofs or roof sections divided by party walls, gutters, a change of roof colour or a repeated pattern of
identical units), or a house plus a separate garage / shed / carport. One house with its own attached garage under a continuous
roof is one.

Answer with only this JSON: {{"units": "one" | "several" | "unclear", "note": "<one short sentence: what you counted>"}}"""
UNIT_ANSWERS = ('one', 'several', 'unclear')


def check(path, units=False):
    cmd = ['claude', '-p', '--model', MODEL, '--system-prompt', SYSTEM, '--strict-mcp-config', '--tools', 'Read',
           '--allowedTools', 'Read', '--output-format', 'json', (UNITS_ASK if units else ASK).format(path=path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(Path(path).parent))
    d = json.loads(r.stdout)
    if d.get('is_error'):
        raise RuntimeError(str(d.get('result'))[:200])
    if MODEL not in (d.get('modelUsage') or {}):
        raise RuntimeError(f"answered by {list((d.get('modelUsage') or {}).keys())}, not {MODEL}")
    text = d['result'].strip()
    obj = json.loads(text[text.index('{'): text.rindex('}') + 1])
    if units:
        if obj.get('units') not in UNIT_ANSWERS:
            raise RuntimeError(f'bad units answer {obj!r}')
        return {'units': obj['units'], 'note': str(obj.get('note', ''))[:300], 'source': UNITS_SOURCE, 'cost_usd': d.get('total_cost_usd')}
    if obj.get('verdict') not in VERDICTS:
        raise RuntimeError(f'bad verdict {obj!r}')
    return {'verdict': obj['verdict'], 'note': str(obj.get('note', ''))[:300], 'source': SOURCE,
            'cost_usd': d.get('total_cost_usd')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--items', required=True)
    ap.add_argument('--tiles', required=True)
    ap.add_argument('--lines', help='comma list (default all)')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--units', action='store_true', help='ask the several-units question; stored as unit_check')
    args = ap.parse_args()
    doc = json.load(open(args.items))
    want = {int(n) for n in args.lines.split(',')} if args.lines else None
    todo = [x for x in doc['corner'] if (want is None or x['line'] in want)
            and (x.get('unit_check' if args.units else 'agent_hint') or {}).get('source') != (UNITS_SOURCE if args.units else SOURCE)]
    tiles = Path(args.tiles).resolve()

    def one(x):
        try:
            return x, check(tiles / f"line-{x['line']:03d}.png", args.units), None
        except Exception as e:  # noqa: BLE001 - recorded per line and retried on the next run
            return x, None, str(e)
    failed = []
    with ThreadPoolExecutor(args.workers) as ex:
        for x, hint, err in ex.map(one, todo):
            if hint:
                x['unit_check' if args.units else 'agent_hint'] = hint
            else:
                failed.append((x['line'], err))
            print(x['line'], (hint.get('units') or hint.get('verdict')) if hint else f'FAILED {err}', flush=True)
    Path(args.items).write_text(json.dumps(doc, indent=1))
    print(json.dumps({'checked': len(todo) - len(failed), 'failed': failed}))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
