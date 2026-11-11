"""Train a regularized roof-end classifier from reviewed imagery features.

Hyperparameters use spatial validation roofs; the test set is reporting only.
Insufficient labels produce a readiness report, never a pretend trained model.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

FEATURES = ["hip_support", "gable_support", "support_difference"]


def features(end):
    hip, gable = end.get("hip_support", 0), end.get("gable_support", 0)
    return [hip, gable, gable - hip]


def sigmoid(x):
    return 1 / (1 + np.exp(-np.clip(x, -30, 30)))


def fit_logistic(x, y, regularization=.1, roof_weights=None):
    matrix = np.c_[np.ones(len(x)), x]
    weights = np.zeros(matrix.shape[1])
    base = np.ones(len(y)) if roof_weights is None else np.asarray(roof_weights)
    prevalence = np.average(y, weights=base)
    sample_weights = base * np.where(y == 1, .5 / max(float(prevalence), 1e-9), .5 / max(float(1 - prevalence), 1e-9))
    for _ in range(1200):
        gradient = matrix.T @ ((sigmoid(matrix @ weights) - y) * sample_weights) / sample_weights.sum()
        gradient[1:] += regularization * weights[1:]
        weights -= .2 * gradient
    return weights


def metrics(x, y, weights, roof_weights=None):
    p = sigmoid(np.c_[np.ones(len(x)), x] @ weights)
    return probability_metrics(p, y, roof_weights)


def probability_metrics(p, y, roof_weights=None):
    base = np.ones(len(y)) if roof_weights is None else np.asarray(roof_weights)
    loss = float(-np.average(y * np.log(p + 1e-9) + (1 - y) * np.log(1 - p + 1e-9), weights=base))
    balanced = float(np.mean([np.average((p[y == label] >= .5) == label, weights=base[y == label]) for label in (0, 1)]))
    return {"log_loss": round(loss, 5), "balanced_accuracy": round(balanced, 5), "ends": len(y)}


def train(dataset_path, out_dir):
    rows = [json.loads(line) for line in Path(dataset_path).read_text().splitlines()]
    splits = {key: [] for key in ("train", "validation", "test")}
    roof_counts = {key: {"hipped": set(), "gabled": set()} for key in splits}
    excluded_family_labels = 0
    for row in rows:
        label = row["reference"].get("roof_family")
        if label not in ("hipped", "gabled"):
            continue
        ends = [end for end in (row["prediction"].get("roof_form_selection") or {}).get("ends", []) if "hip_support" in end]
        if not ends:
            excluded_family_labels += 1
        for end in ends:
            if "hip_support" in end:
                splits[row["split"]].append((features(end), int(label == "gabled"), 1 / len(ends)))
                roof_counts[row["split"]][label].add(row["address_id"])
    counts = {split: {label: len(ids) for label, ids in kinds.items()} for split, kinds in roof_counts.items()}
    required = {"train": 10, "validation": 3, "test": 3}
    missing = [f"{split}: {label} needs {minimum - counts[split][label]} more reviewed roofs"
               for split, minimum in required.items() for label in ("hipped", "gabled") if counts[split][label] < minimum]
    report = {"schema_version": 1, "dataset": str(dataset_path), "reviewed_roofs": counts, "requirements": required,
              "excluded_family_labels": excluded_family_labels,
              "status": "needs-reviewed-labels" if missing else "trained-candidate", "missing": missing,
              "model_kind": "regularized-logistic-roof-end-classifier", "feature_names": FEATURES}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not missing:
        arrays = {k: tuple(np.asarray([p[i] for p in v]) for i in range(3)) for k, v in splits.items()}
        x, y, roof_weights = arrays["train"]
        mean = np.average(x, axis=0, weights=roof_weights)
        scale = np.maximum(np.sqrt(np.average((x - mean) ** 2, axis=0, weights=roof_weights)), .05)
        trials = []
        for regularization in (.001, .01, .1, 1.):
            weights = fit_logistic((x - mean) / scale, y, regularization, roof_weights)
            score = metrics((arrays["validation"][0] - mean) / scale, arrays["validation"][1], weights, arrays["validation"][2])
            trials.append((score["log_loss"], regularization, weights, score))
        _, penalty, weights, validation = min(trials, key=lambda t: t[0])
        test = metrics((arrays["test"][0] - mean) / scale, arrays["test"][1], weights, arrays["test"][2])
        # Frozen evidence-only baseline; never use reviewer-forced choices as a prediction.
        vx, vy, vw = arrays["validation"]
        baseline = probability_metrics(np.where((vx[:, 1] >= .4) & (vx[:, 2] >= .2), .8,
                                                np.where((vx[:, 0] >= .4) & (vx[:, 2] <= -.2), .2, .5)), vy, vw)
        report.update({"regularization": penalty, "validation": validation, "test": test,
                       "validation_baseline": baseline, "weighting": "equal roof weight within class",
                       "promotion_eligible": validation["balanced_accuracy"] >= .75 and validation["balanced_accuracy"] >= baseline["balanced_accuracy"]
                       and validation["log_loss"] < min(.60, baseline["log_loss"])})
        model = {"schema_version": 1, "feature_names": FEATURES, "mean": mean.tolist(), "scale": scale.tolist(),
                 "weights": weights.tolist(), "dataset": str(dataset_path), "validated": report["promotion_eligible"],
                 "validation": validation, "test": test}
        (out_dir / "roof-form.json").write_text(json.dumps(model, indent=2))
    (out_dir / "training-report.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", default="roof_training_250")
    args = parser.parse_args()
    latest = Path(f"data/training/{args.cohort}/latest.json")
    manifest = json.loads(latest.read_text())
    print(json.dumps(train(manifest["dataset"], Path(manifest["dataset"]).parent), indent=2))


if __name__ == "__main__":
    main()
