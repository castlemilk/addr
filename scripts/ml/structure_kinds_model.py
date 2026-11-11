"""Classifier head for structure sub-kinds over frozen features (structure-kinds-model-v1).

A multinomial logistic regression (numpy + scipy L-BFGS, no new dependencies) trained on
human-confirmed observations (structure-kinds-agent-review-v1 rows with tier <= 2) using:
  * the rule-hint evidence (geometry, appearance, context) from the current structure_kinds rows,
  * the extra signals from structure_kinds_signals (oriented ribs, LAB variance, roof continuity,
    multi-date support),
  * optionally the pooled SAM3 embedding saved by `structure_kinds_signals --embed`.

Evaluation is grouped by tile (scene) so neighbours never leak between folds; the report gives
per-class agreement with denominators and a reliability table by predicted probability. Every number
is agreement with reviewer observations, not accuracy against ground truth.

Label policy: by default only human tiers train or evaluate the model. `--allow-agent-labels` runs the
same code on tier-3 agent observations as a smoke test; such runs are labelled `smoke`, are never
written to the index and must not be quoted as model quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from . import eyes_index
from . import structure_kinds as sk
from . import structure_kinds_review as review
from . import structure_kinds_signals as signals
from .scene_object_quality import _row

VERSION = 'structure-kinds-model-v1'
TABLE = 'object_quality'
MODEL_DIR = Path('data/reviews/eyes/structure-kinds/model')
NUMERIC = ['geometry.area_m2', 'geometry.rect_width_m', 'geometry.rect_length_m', 'geometry.aspect', 'geometry.rectilinearity',
           'geometry.circularity', 'geometry.vertex_count', 'appearance.brightness_median', 'appearance.saturation_median',
           'appearance.rib_anisotropy', 'appearance.rib_period_m', 'appearance.rib_axis_alignment',
           'appearance.interior_relative_variance', 'appearance.driveway_like_share_3m', 'context.nearest_larger_roof_m',
           'context.distance_to_largest_roof_m', 'context.largest_roof_within_40m_area', 'context.largest_same_property_area']
BOOLEAN = ['context.attached_to_larger_roof', 'context.secondary_structure_flag', 'context.touches_image_edge']
SIGNAL_NUMERIC = ['ribs_oriented.period_along_m', 'ribs_oriented.period_across_m', 'ribs_oriented.axis_gradient_share',
                  'lab_variance_ratio_original', 'roof_continuity.chroma_step', 'roof_continuity.seam_edge_median',
                  'multidate.other_date_support_min']
SIGNAL_BOOLEAN = ['roof_continuity.continuous_roof_hint', 'multidate.absent_on_another_date']
MIN_LABELS_PER_CLASS = 5
MIN_TILES_FOR_CV = 4


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _get(payload, dotted):
    node = payload
    for part in dotted.split('.'):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def feature_vector(hint_payload, signal_payload=None, embedding=None):
    """Numeric vector + presence flags (missing values -> 0 with a paired 'missing' indicator)."""
    values, names = [], []
    evidence = (hint_payload or {}).get('evidence') or {}
    for name in NUMERIC:
        v = _get(evidence, name)
        v = None if isinstance(v, bool) else v
        values += [float(v) if v is not None else 0., 0. if v is not None else 1.]
        names += [name, f'{name}.missing']
    for name in BOOLEAN:
        values.append(1. if _get(evidence, name) else 0.)
        names.append(name)
    for name in SIGNAL_NUMERIC:
        v = _get(signal_payload or {}, name)
        v = None if isinstance(v, bool) else v
        values += [float(v) if v is not None else 0., 0. if v is not None else 1.]
        names += [name, f'{name}.missing']
    for name in SIGNAL_BOOLEAN:
        values.append(1. if _get(signal_payload or {}, name) else 0.)
        names.append(name)
    if embedding is not None:
        emb = np.asarray(embedding, dtype=np.float64)
        values += emb.tolist()
        names += [f'emb{i}' for i in range(emb.size)]
    return np.asarray(values, dtype=np.float64), names


def load_embeddings(root, scene_id):
    path = Path(root)/signals.EMBED_DIR/f'{scene_id}.npz'
    if not path.exists():
        return {}
    with np.load(path) as data:
        return {k: data[k] for k in data.files if k != '__meta__'}


def dataset(root, *, max_tier=2, allow_agent=False, use_embeddings=False, reviewer=None):
    """Labelled examples: one per observed object with a current hint row. Returns dict with X, y, groups, meta."""
    root = Path(root)
    with closing(eyes_index.connection(root, readonly=True)) as db:
        observations = review.review_rows(db)
        hints = {(r['scene_id'], r['object_id']): r['payload'] for r in eyes_index.rows(db, TABLE, version=sk.VERSION)}
        extra = {(r['scene_id'], r['object_id']): r['payload'] for r in eyes_index.rows(db, TABLE, version=signals.VERSION)}
    tier_limit = 3 if allow_agent else max_tier
    rows, embeddings_cache = [], {}
    for r in observations:
        p = r['payload']
        if p.get('tier', 3) > tier_limit or (reviewer and p.get('reviewer') != reviewer):
            continue
        key = (r['scene_id'], r['object_id'])
        if key not in hints or p['observed'] not in review.OBSERVED_KINDS:
            continue
        emb = None
        if use_embeddings:
            if r['scene_id'] not in embeddings_cache:
                embeddings_cache[r['scene_id']] = load_embeddings(root, r['scene_id'])
            emb = embeddings_cache[r['scene_id']].get(r['object_id'])
            if emb is None:
                continue
        x, names = feature_vector(hints[key], extra.get(key), emb)
        rows.append((x, p['observed'], r['scene_id'], key, p.get('tier')))
    if not rows:
        return {'X': np.zeros((0, 0)), 'y': [], 'groups': [], 'keys': [], 'tiers': [], 'names': [], 'label_policy': 'human' if not allow_agent else 'smoke'}
    return {'X': np.vstack([r[0] for r in rows]), 'y': [r[1] for r in rows], 'groups': [r[2] for r in rows],
            'keys': [r[3] for r in rows], 'tiers': [r[4] for r in rows], 'names': names,
            'label_policy': 'human' if not allow_agent else 'smoke'}


class SoftmaxHead:
    """Multinomial logistic regression with L2, standardised inputs; fit by L-BFGS."""

    def __init__(self, classes, l2=1.0):
        self.classes = list(classes)
        self.l2 = l2
        self.mean = self.std = self.W = None

    def _design(self, X):
        Z = (X-self.mean)/self.std
        return np.hstack([Z, np.ones((len(Z), 1))])

    def fit(self, X, y):
        self.mean = X.mean(axis=0)
        self.std = np.where(X.std(axis=0) > 1e-9, X.std(axis=0), 1.)
        Z = self._design(X)
        index = {c: i for i, c in enumerate(self.classes)}
        Y = np.zeros((len(y), len(self.classes)))
        Y[np.arange(len(y)), [index[v] for v in y]] = 1.
        k, d = len(self.classes), Z.shape[1]

        def objective(flat):
            W = flat.reshape(d, k)
            logits = Z @ W
            logits -= logits.max(axis=1, keepdims=True)
            log_probs = logits-np.log(np.exp(logits).sum(axis=1, keepdims=True))
            loss = -(Y*log_probs).sum()/len(Z)+self.l2*(W[:-1]**2).sum()/(2*len(Z))
            probs = np.exp(log_probs)
            grad = Z.T @ (probs-Y)/len(Z)
            grad[:-1] += self.l2*W[:-1]/len(Z)
            return loss, grad.ravel()

        result = minimize(objective, np.zeros(d*k), jac=True, method='L-BFGS-B', options={'maxiter': 500})
        self.W = result.x.reshape(d, k)
        return self

    def predict_proba(self, X):
        logits = self._design(X) @ self.W
        logits -= logits.max(axis=1, keepdims=True)
        probs = np.exp(logits)
        return probs/probs.sum(axis=1, keepdims=True)

    def to_json(self):
        return {'classes': self.classes, 'l2': self.l2, 'mean': self.mean.tolist(), 'std': self.std.tolist(), 'W': self.W.tolist()}

    @classmethod
    def from_json(cls, doc):
        head = cls(doc['classes'], doc['l2'])
        head.mean, head.std, head.W = np.asarray(doc['mean']), np.asarray(doc['std']), np.asarray(doc['W'])
        return head


def grouped_folds(groups, k):
    """Assign whole tiles to folds (round-robin over shuffled tiles); returns fold id per row."""
    tiles = sorted(set(groups))
    rng = np.random.default_rng(7)
    rng.shuffle(tiles)
    fold_of = {t: i % k for i, t in enumerate(tiles)}
    return np.asarray([fold_of[g] for g in groups])


def evaluate(data, *, l2=1.0, folds=5):
    """Tile-grouped cross-validation: per-class agreement with denominators, confusion, reliability bins."""
    y = np.asarray(data['y'])
    counts = Counter(data['y'])
    classes = sorted(c for c, n in counts.items() if n >= MIN_LABELS_PER_CLASS)
    dropped = {c: n for c, n in counts.items() if n < MIN_LABELS_PER_CLASS}
    keep = np.isin(y, classes)
    X, y, groups = data['X'][keep], y[keep], np.asarray(data['groups'])[keep]
    report = {'version': VERSION, 'label_policy': data['label_policy'], 'examples': int(keep.sum()), 'classes': classes,
              'dropped_classes_under_min': dropped, 'tiles': len(set(data['groups'])), 'features': len(data['names']),
              'note': 'agreement with reviewer observations under tile-grouped cross-validation; not accuracy against ground truth'}
    if len(classes) < 2 or len(set(groups)) < MIN_TILES_FOR_CV:
        report['status'] = 'insufficient data for cross-validation'
        return report
    k = min(folds, len(set(groups)))
    fold = grouped_folds(groups, k)
    predicted, prob_max = np.empty(len(y), dtype=object), np.zeros(len(y))
    for f in range(k):
        train, test = fold != f, fold == f
        present = sorted(set(y[train]))
        head = SoftmaxHead(present, l2).fit(X[train], y[train])
        probs = head.predict_proba(X[test])
        predicted[test] = [present[i] for i in probs.argmax(axis=1)]
        prob_max[test] = probs.max(axis=1)
    per_class = {c: {'rows': int((y == c).sum()), 'agree': int(((y == c) & (predicted == c)).sum()),
                     'predicted_rows': int((predicted == c).sum())} for c in classes}
    confusion = Counter(f'{a} -> {b}' for a, b in zip(y, predicted, strict=True))
    bins = [(0., .5), (.5, .7), (.7, .9), (.9, 1.01)]
    reliability = [{'bin': f'{lo:.1f}-{min(hi, 1.):.1f}', 'rows': int(((prob_max >= lo) & (prob_max < hi)).sum()),
                    'agree': int((((prob_max >= lo) & (prob_max < hi)) & (predicted == y)).sum())} for lo, hi in bins]
    report.update({'status': 'ok', 'folds': k, 'agree': int((predicted == y).sum()), 'per_class': per_class,
                   'confusion': dict(confusion), 'reliability': reliability,
                   'hint_agree_same_rows': None})
    return report


def train_full(data, l2=1.0):
    counts = Counter(data['y'])
    classes = sorted(c for c, n in counts.items() if n >= MIN_LABELS_PER_CLASS)
    keep = np.isin(np.asarray(data['y']), classes)
    return SoftmaxHead(classes, l2).fit(data['X'][keep], np.asarray(data['y'])[keep])


def score_scenes(root, head, scene_ids, *, use_embeddings=False, names=None):
    """Probability rows for every object with a hint row in the scenes (written under VERSION)."""
    root = Path(root)
    rows = []
    stamp, code = datetime.now(UTC).isoformat(), code_sha256()
    with closing(eyes_index.connection(root, readonly=True)) as db:
        for sid in dict.fromkeys(scene_ids):
            hints = eyes_index.rows(db, TABLE, scene_id=sid, version=sk.VERSION)
            extra = {r['object_id']: r['payload'] for r in eyes_index.rows(db, TABLE, scene_id=sid, version=signals.VERSION)}
            embeddings = load_embeddings(root, sid) if use_embeddings else {}
            for h in hints:
                emb = embeddings.get(h['object_id']) if use_embeddings else None
                if use_embeddings and emb is None:
                    continue
                x, _ = feature_vector(h['payload'], extra.get(h['object_id']), emb)
                probs = head.predict_proba(x[None])[0]
                order = np.argsort(-probs)
                payload = {'version': VERSION, 'note': 'model estimate from a head trained on reviewer observations; not a label',
                           'kind': 'building', 'predicted': head.classes[order[0]], 'probability': round(float(probs[order[0]]), 4),
                           'probabilities': {c: round(float(p), 4) for c, p in zip(head.classes, probs, strict=True)},
                           'rule_hint': h['payload'].get('sub_kind'), 'agrees_with_hint': head.classes[order[0]] == h['payload'].get('sub_kind'),
                           'features': len(x), 'embedding_used': bool(use_embeddings), 'computed_at': stamp, 'code_sha256': code}
                rows.append(_row({'id': sid, 'model_signature': h.get('model_signature')}, {'id': h['object_id']}, payload, VERSION))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description='Classifier head over frozen features for structure sub-kinds (reviewer-observation agreement, not accuracy).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--allow-agent-labels', action='store_true', help='Smoke test on tier-3 agent observations; never written.')
    parser.add_argument('--reviewer')
    parser.add_argument('--embeddings', action='store_true', help='Use pooled SAM3 embeddings saved by structure_kinds_signals --embed.')
    parser.add_argument('--l2', type=float, default=1.0)
    parser.add_argument('--folds', type=int, default=5)
    parser.add_argument('--score-scenes', nargs='*', default=None, help='After training on human labels, write model rows for these scenes.')
    parser.add_argument('--report')
    args = parser.parse_args(argv)
    root = Path(args.root)
    data = dataset(root, allow_agent=args.allow_agent_labels, use_embeddings=args.embeddings, reviewer=args.reviewer)
    report = evaluate(data, l2=args.l2, folds=args.folds) if len(data['y']) else {'version': VERSION, 'status': 'no labelled examples',
                                                                                   'label_policy': data['label_policy'], 'examples': 0}
    report['label_counts'] = dict(Counter(data['y']))
    report['tier_counts'] = dict(Counter(data['tiers']))
    written = 0
    if args.score_scenes is not None:
        if data['label_policy'] != 'human':
            report['scoring'] = 'refused: model rows are only written from human-confirmed labels'
        elif report.get('status') != 'ok':
            report['scoring'] = f"refused: {report.get('status')}"
        else:
            head = train_full(data, args.l2)
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            (root/MODEL_DIR/'head.json').write_text(json.dumps({**head.to_json(), 'features': data['names'], 'trained_at': datetime.now(UTC).isoformat(),
                                                                'examples': len(data['y']), 'code_sha256': code_sha256()}))
            rows = score_scenes(root, head, args.score_scenes, use_embeddings=args.embeddings)
            with closing(eyes_index.connection(root)) as db:
                eyes_index.ensure_schema(db)
                written = sk.replace_scene_rows(db, TABLE, VERSION, list(dict.fromkeys(args.score_scenes)), rows)
            report['scoring'] = {'scenes': len(set(args.score_scenes)), 'written': written}
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=1))
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
