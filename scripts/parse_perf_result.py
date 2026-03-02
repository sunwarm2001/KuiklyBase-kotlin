#!/usr/bin/env python3
"""Summarise Kotlin/Native performance results at the test case level."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a simplified case-level CSV from Kotlin/Native benchmarks."
    )
    parser.add_argument("input_json", help="Path to the benchmark result JSON file")
    parser.add_argument(
        "--output",
        default="case_metrics.csv",
        help="Filename for the case CSV (defaults to case_metrics.csv)",
    )
    parser.add_argument(
        "--baseline",
        help="Path to baseline CSV for comparison (optional)",
    )
    parser.add_argument(
        "--summary",
        help="Path to write summary output for stable tests (optional)",
    )
    return parser.parse_args()


def load_benchmarks(input_path: Path) -> List[Dict[str, Any]]:
    with input_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if isinstance(payload, dict):
        benchmarks = payload.get("benchmarks", [])
    else:
        benchmarks = payload

    if not isinstance(benchmarks, list):
        raise ValueError("Expected a list of benchmark entries in the input file")

    return benchmarks


def normalised_case_name(raw_name: str) -> str:
    parts = raw_name.split("::", 1)
    return parts[-1] if len(parts) == 2 else raw_name


def collect_case_rows(benchmarks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated: Dict[str, Dict[str, Any]] = {}

    for bench in benchmarks:
        if bench.get("metric") != "EXECUTION_TIME":
            continue

        name = bench.get("name")
        if not name:
            continue

        case_name = normalised_case_name(name)
        data = aggregated.setdefault(
            case_name,
            {
                "case": case_name,
                "statuses": set(),
                "runtimes": [],
                "repeat": None,
                "warmup": None,
            },
        )

        status = bench.get("status")
        if status:
            data["statuses"].add(status)

        runtime = bench.get("runtimeInUs", bench.get("score"))
        if runtime is not None:
            data["runtimes"].append(float(runtime))

        repeat = bench.get("repeat")
        if repeat is not None:
            current_repeat = data["repeat"]
            data["repeat"] = repeat if current_repeat is None else max(current_repeat, repeat)

        warmup = bench.get("warmup")
        if warmup is not None and data["warmup"] is None:
            data["warmup"] = warmup

    rows: List[Dict[str, Any]] = []
    for case_name in sorted(aggregated):
        case_data = aggregated[case_name]
        runtimes = case_data["runtimes"]

        average = mean(runtimes) if runtimes else 0.0
        spread = pstdev(runtimes) if len(runtimes) > 1 else 0.0
        # Represent spread as coefficient of variation to normalise against average.
        spread_ratio = (spread / average) if average else 0.0
        statuses = case_data["statuses"]
        status = "PASSED" if statuses == {"PASSED"} else "FAILED"

        rows.append(
            {
                "case": case_name,
                "status": status,
                "execution_time_us_avg": round(average, 6),
                "execution_time_us_spread": round(spread_ratio, 6),
                "repeat": case_data["repeat"] if case_data["repeat"] is not None else "",
                "warmup": case_data["warmup"] if case_data["warmup"] is not None else "",
            }
        )

    return rows


def write_case_csv(rows: List[Dict[str, Any]], output_path: Path) -> None:
    header = [
        "case",
        "status",
        "execution_time_us_avg",
        "execution_time_us_spread",
        "repeat",
        "warmup",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=header)
        writer.writeheader()
        sorted_rows = sorted(
            rows,
            key=lambda row: (row["execution_time_us_spread"], row["case"]),
        )
        writer.writerows(sorted_rows)


def load_baseline(baseline_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load baseline CSV and return as dictionary keyed by case name."""
    baseline_map = {}
    with baseline_path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            baseline_map[row["case"]] = row
    return baseline_map


def compare_with_baseline(
        rows: List[Dict[str, Any]],
        baseline_path: Path,
        summary_path: Path | None = None
) -> List[Dict[str, Any]]:
    """
    Filter stable tests (spread <= 20%) and compare with baseline.
    Returns enhanced rows with baseline_compare column.
    Optionally writes summary to summary_path.
    """
    baseline_map = load_baseline(baseline_path)

    stable_tests = []
    baseline_comparisons = []

    for row in rows:
        spread = float(row["execution_time_us_spread"])

        # Only process stable tests (spread <= 20% = 0.20)
        if spread <= 0.20:
            case_name = row["case"]
            current_avg = float(row["execution_time_us_avg"])

            # Look up baseline
            if case_name in baseline_map:
                baseline_avg = float(baseline_map[case_name]["execution_time_us_avg"])
                baseline_compare = current_avg / baseline_avg

                # Add baseline_compare to the row
                row["baseline_compare"] = round(baseline_compare, 6)
                baseline_comparisons.append(baseline_compare)
            else:
                # No baseline found for this case
                row["baseline_compare"] = "N/A"

            stable_tests.append(row)

    # Generate summary if requested
    if summary_path:
        num_stable = len(stable_tests)
        num_with_baseline = len(baseline_comparisons)
        avg_baseline_compare = sum(baseline_comparisons) / len(baseline_comparisons) if baseline_comparisons else 0.0

        with summary_path.open("w", encoding="utf-8") as fh:
            fh.write("### Performance Test Summary\n\n")
            fh.write(f"- **Stable test cases** (spread ≤ 20%): {num_stable}\n")
            fh.write(f"- **Cases with baseline comparison**: {num_with_baseline}\n")
            fh.write(f"- **Average baseline comparison ratio**: {avg_baseline_compare:.4f}\n\n")

            if avg_baseline_compare > 0:
                percentage_change = (avg_baseline_compare - 1.0) * 100
                if percentage_change > 0:
                    fh.write(f"- Performance is **{percentage_change:.2f}% slower** than baseline on average\n")
                elif percentage_change < 0:
                    fh.write(f"- Performance is **{abs(percentage_change):.2f}% faster** than baseline on average\n")
                else:
                    fh.write(f"- Performance is **equivalent** to baseline on average\n")
            fh.write("\n")

    return stable_tests


def write_enhanced_csv(rows: List[Dict[str, Any]], output_path: Path) -> None:
    """Write CSV with baseline_compare column included."""
    if not rows:
        return

    # Get all keys from first row to include baseline_compare
    header = list(rows[0].keys())

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_json).expanduser().resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = input_path.parent / output_path

    benchmarks = load_benchmarks(input_path)
    case_rows = collect_case_rows(benchmarks)
    write_case_csv(case_rows, output_path)

    print(f"Case CSV written to {output_path}")

    # If baseline comparison is requested
    if args.baseline:
        baseline_path = Path(args.baseline).expanduser().resolve()
        if not baseline_path.exists():
            raise FileNotFoundError(f"Baseline file not found: {baseline_path}")

        summary_path = Path(args.summary) if args.summary else None
        if summary_path and not summary_path.is_absolute():
            summary_path = input_path.parent / summary_path

        # Compare with baseline and filter stable tests
        stable_rows = compare_with_baseline(case_rows, baseline_path, summary_path)

        # Write enhanced CSV with baseline_compare column
        enhanced_output = output_path.parent / "result_with_baseline.csv"
        write_enhanced_csv(stable_rows, enhanced_output)
        print(f"Enhanced CSV (stable tests only) written to {enhanced_output}")

        if summary_path:
            print(f"Summary written to {summary_path}")


if __name__ == "__main__":
    main()