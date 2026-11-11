"""Pick the best stage-1 learning rate from sweep/results.jsonl and print it (sam3-mac-sweep-v1).

Criterion, pre-registered: highest paired mean best-IoU change of tuned over base on the human references, among runs
whose false-positive proxy did not rise by more than 0.10 over base and whose training did not skip batches. Ties go
to the lower learning rate. Prints the learning rate, or 'none' when no run qualifies.

The proxy is the share of predictions that overlap no human outline, no production pseudo label and no Microsoft
building footprint (novel_no_footprint_share_of_predictions) when the evaluator recorded it, else the plain novel
share. Stage 1 of 2026-09-28 was judged with the plain novel share and refused every run (tuned models detected
about three times as many novel objects); stage 2 was then started at 8e-5 by an explicit, logged override.
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[4] / 'data/reviews/eyes/training/sam3-mac/sweep'


def best(rows):
    ok = []
    for r in rows:
        if not str(r['run']).startswith('s1-') or r.get('eval_error') or r.get('train_error'):
            continue
        neg = r.get('negatives') or {}
        key = 'novel_no_footprint_share_of_predictions' if (neg.get('tuned') or {}).get('novel_no_footprint_share_of_predictions') is not None else 'novel_share'
        base_novel = (neg.get('base') or {}).get(key)
        tuned_novel = (neg.get('tuned') or {}).get(key)
        if base_novel is not None and tuned_novel is not None and tuned_novel - base_novel > .10:
            continue
        if (r.get('skipped_batches') or 0) > 0:
            continue
        delta = (r.get('paired') or {}).get('best_iou_delta_mean')
        if delta is None:
            continue
        ok.append((delta, -r['lr'], r['lr']))
    if not ok:
        return None
    ok.sort(reverse=True)
    return ok[0][2]


def main():
    path = OUT / 'results.jsonl'
    rows = [json.loads(line) for line in open(path)] if path.exists() else []
    lr = best(rows)
    print('none' if lr is None else f'{lr:g}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
