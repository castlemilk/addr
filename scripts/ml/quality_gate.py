"""Quality gate: fails when the system's measured quality degrades.

Reads the reports produced by `task perf` and `task isolation` and compares them
against thresholds. Intended for CI or post-build verification.

Usage:
    python -m scripts.ml.quality_gate \
        --performance data/reviews/performance.json \
        --isolation data/reviews/isolation.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    name: str
    value: float
    limit: float
    comparison: str

    @property
    def passed(self) -> bool:
        if self.comparison == ">=":
            return self.value >= self.limit
        return self.value <= self.limit

    def describe(self) -> str:
        symbol = ">=" if self.comparison == ">=" else "<="
        status = "PASS" if self.passed else "FAIL"
        return f"{status}  {self.name:<34} {self.value:>8.2f}  (want {symbol} {self.limit})"


def evaluate(
    performance: dict,
    isolation: dict | None,
    *,
    min_average_quality: float,
    max_c_grades: float,
    max_d_grades: float,
    min_coverage: float,
    max_mostly_inside: float,
    max_canopy_records: float,
    max_tree_overlaps: float,
) -> list[Check]:
    grades = performance.get("quality_grades", {})
    structural = performance.get("structural", {})
    checks = [
        Check("average_quality", performance.get("average_quality", 0.0), min_average_quality, ">="),
        Check("c_grade_records", grades.get("C", 0), max_c_grades, "<="),
        Check("d_grade_records", grades.get("D", 0), max_d_grades, "<="),
    ]
    if min_coverage > 0 and performance.get("coverage") is not None:
        checks.append(Check("coverage", performance["coverage"], min_coverage, ">="))
    if isolation is not None:
        checks.extend(
            [
                Check(
                    "trees_mostly_inside_buildings",
                    isolation.get("trees_mostly_inside_buildings", 0),
                    max_mostly_inside,
                    "<=",
                ),
                Check(
                    "records_with_canopy_over_roof",
                    isolation.get("records_with_canopy_over_roof", 0),
                    max_canopy_records,
                    "<=",
                ),
                Check(
                    "tree_tree_overlap_pairs",
                    isolation.get("tree_tree_overlap_pairs", 0),
                    max_tree_overlaps,
                    "<=",
                ),
            ]
        )
    if structural:
        checks.append(
            Check(
                "trees_mostly_inside (structural)",
                structural.get("trees_mostly_inside_buildings", 0),
                max_mostly_inside,
                "<=",
            )
        )
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--performance", type=Path, default=Path("data/reviews/performance.json"))
    parser.add_argument("--isolation", type=Path, default=Path("data/reviews/isolation.json"))
    parser.add_argument("--min-average-quality", type=float, default=97.0)
    parser.add_argument("--max-c-grades", type=float, default=3)
    parser.add_argument("--max-d-grades", type=float, default=0)
    parser.add_argument("--min-coverage", type=float, default=0.0)
    parser.add_argument("--max-mostly-inside", type=float, default=8)
    parser.add_argument("--max-canopy-records", type=float, default=25)
    parser.add_argument("--max-tree-overlaps", type=float, default=30)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.performance.exists():
        print(f"missing {args.performance}; run `task perf` first", file=sys.stderr)
        return 2
    performance = json.loads(args.performance.read_text())
    isolation = (
        json.loads(args.isolation.read_text()) if args.isolation.exists() else None
    )
    checks = evaluate(
        performance,
        isolation,
        min_average_quality=args.min_average_quality,
        max_c_grades=args.max_c_grades,
        max_d_grades=args.max_d_grades,
        min_coverage=args.min_coverage,
        max_mostly_inside=args.max_mostly_inside,
        max_canopy_records=args.max_canopy_records,
        max_tree_overlaps=args.max_tree_overlaps,
    )
    for check in checks:
        print(check.describe())
    failed = [check for check in checks if not check.passed]
    if failed:
        print(f"quality gate FAILED ({len(failed)} check(s))")
        return 1
    print("quality gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
