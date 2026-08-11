#!/usr/bin/env python3
"""Create small deterministic public summaries from paired benchmark evidence."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


class BenchmarkContractError(RuntimeError):
    pass


_SCHEMA = 1
_ANALYSIS_SCHEMA = "cmux.perf.mixed-workload-analysis/v1"
_METRICS = (
    "browser_latency_ms",
    "browser_render_rate",
    "browser_throughput_per_second",
    "churn_full_tree_cpu_percent",
    "churn_parent_cpu_percent",
    "churn_terminal_cpu_percent",
    "churn_webkit_cpu_percent",
    "steady_full_tree_cpu_percent",
    "steady_parent_cpu_percent",
    "steady_terminal_cpu_percent",
    "steady_webkit_cpu_percent",
    "terminal_render_rate",
    "terminal_throughput_per_second",
)


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkContractError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise BenchmarkContractError(f"{name} must be finite")
    return number


def _rounded(value: float) -> float:
    return round(value, 6)


def encode_json(document: Mapping[str, object]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"


def load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkContractError(f"could not read benchmark JSON: {path.name}") from error
    if not isinstance(document, dict):
        raise BenchmarkContractError("benchmark JSON must be an object")
    return document


def summarize_analysis(analysis: Mapping[str, Any], baseline_sha: str, candidate_sha: str) -> dict[str, object]:
    if analysis.get("schema") != _ANALYSIS_SCHEMA:
        raise BenchmarkContractError("unexpected benchmark analysis schema")
    groups = analysis.get("groups")
    scenarios = analysis.get("scenarios")
    if not isinstance(groups, list) or not groups:
        raise BenchmarkContractError("benchmark analysis has no comparison groups")
    if not isinstance(scenarios, list) or not scenarios:
        raise BenchmarkContractError("benchmark analysis has no scenarios")

    baseline_values: dict[str, list[float]] = {metric: [] for metric in _METRICS}
    candidate_values: dict[str, list[float]] = {metric: [] for metric in _METRICS}
    for group in groups:
        if not isinstance(group, dict):
            raise BenchmarkContractError("benchmark group must be an object")
        metric = group.get("metric")
        comparison = group.get("comparison")
        if metric not in baseline_values or not isinstance(comparison, dict):
            continue
        baseline = comparison.get("baseline")
        candidate = comparison.get("candidate")
        if not isinstance(baseline, dict) or not isinstance(candidate, dict):
            raise BenchmarkContractError("benchmark comparison is incomplete")
        baseline_values[metric].append(_finite_number(baseline.get("median"), f"{metric} baseline median"))
        candidate_values[metric].append(_finite_number(candidate.get("median"), f"{metric} candidate median"))

    observed = sorted(metric for metric in _METRICS if baseline_values[metric] and candidate_values[metric])
    if not observed:
        raise BenchmarkContractError("benchmark analysis contains no public aggregate metrics")
    metrics = {metric: _rounded(statistics.median(candidate_values[metric])) for metric in observed}
    deltas = {
        metric: _rounded(metrics[metric] - statistics.median(baseline_values[metric]))
        for metric in observed
    }

    conclusions: dict[str, str] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise BenchmarkContractError("benchmark scenario must be an object")
        scenario_id = scenario.get("scenario_id")
        conclusion = scenario.get("conclusion")
        if not isinstance(scenario_id, str) or conclusion not in {"proven", "inconclusive", "regressed"}:
            raise BenchmarkContractError("benchmark scenario conclusion is invalid")
        conclusions[scenario_id] = conclusion

    status = analysis.get("status")
    accepted = analysis.get("final_acceptance")
    if status not in {"accepted", "rejected"} or type(accepted) is not bool:
        raise BenchmarkContractError("benchmark acceptance fields are invalid")
    return {
        "schema": _SCHEMA,
        "baseline_sha": baseline_sha,
        "candidate_sha": candidate_sha,
        "status": status,
        "accepted": accepted,
        "metrics": metrics,
        "deltas_from_baseline": deltas,
        "scenarios": dict(sorted(conclusions.items())),
    }


def compare_with_previous(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, object]:
    current_metrics = current.get("metrics")
    previous_metrics = previous.get("metrics", {})
    if not isinstance(current_metrics, dict) or not isinstance(previous_metrics, dict):
        raise BenchmarkContractError("benchmark metrics must be objects")
    deltas: dict[str, float] = {}
    for metric in sorted(set(current_metrics) & set(previous_metrics)):
        current_value = _finite_number(current_metrics[metric], f"current {metric}")
        previous_value = _finite_number(previous_metrics[metric], f"previous {metric}")
        deltas[metric] = _rounded(current_value - previous_value)
    result = dict(current)
    result["deltas_from_previous"] = deltas
    return dict(sorted(result.items()))


def write_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encode_json(document), encoding="utf-8")
    temporary.replace(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--analysis", type=Path, required=True)
    summarize.add_argument("--baseline-sha", required=True)
    summarize.add_argument("--candidate-sha", required=True)
    summarize.add_argument("--output", type=Path, required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--previous", type=Path, required=True)
    compare.add_argument("--current", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "summarize":
            summary = summarize_analysis(
                load_json(args.analysis),
                args.baseline_sha.strip().lower(),
                args.candidate_sha.strip().lower(),
            )
        else:
            summary = compare_with_previous(load_json(args.previous), load_json(args.current))
        write_json(args.output, summary)
    except BenchmarkContractError as error:
        print(f"benchmark contract: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
