"""Prints the routing benchmark: baseline dispatch plan versus the live re-optimiser.

Usage:  uv run python -m scripts.run_routing_benchmark
"""

from __future__ import annotations

import time

from app.routing.benchmark import BenchmarkRunner


def main() -> None:
    started = time.perf_counter()
    rows = BenchmarkRunner().run()
    elapsed = time.perf_counter() - started
    print("| scenario | total min (base→live) | longest (base→live) | crossings | gap to bound |")
    print("|---|---|---|---|---|")
    for row in rows:
        b, a = row.baseline, row.reoptimized
        gap_b = "—" if b.lower_bound_gap is None else f"{b.lower_bound_gap:.0%}"
        gap_a = "—" if a.lower_bound_gap is None else f"{a.lower_bound_gap:.0%}"
        print(
            f"| {row.scenario} | {b.total_minutes:.1f} → {a.total_minutes:.1f} "
            f"| {b.longest_minutes:.1f} → {a.longest_minutes:.1f} "
            f"| {b.crossings} → {a.crossings} | {gap_b} → {gap_a} |"
        )
    summary = BenchmarkRunner.summarize(rows)
    print()
    for key, value in summary.items():
        print(f"{key}: {value:.2f}")
    print(f"elapsed seconds: {elapsed:.1f}")


if __name__ == "__main__":
    main()
