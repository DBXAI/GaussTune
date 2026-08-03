#!/usr/bin/env python3
"""Cache-state-aware I/O queue correction for TP TPS.

The original Huawei6 queue model treated a configured sysbench rate as a TPS
ceiling and one device service time as valid throughout a run.  That fails
when a cold TP cache warms while AP begins.  This model instead aligns every
target window to a TP-only run started with the same SB and cache-reset
protocol.  The matched baseline supplies the cache-warmth transaction
capacity and device await; only the *incremental* queue delay from AP/other
physical requests is converted to TPS loss.

Training profiles fit two global physical coefficients.  The holdout TPS and
await fields are used only after predictions are generated for evaluation.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path


TERMINALS = 128


@dataclass(frozen=True)
class Window:
    profile: str
    second: int
    tps: float
    total_iops: float
    tp_iops: float
    ap_iops: float
    other_iops: float
    await_ms: float


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def tps_by_second(profile_dir: Path) -> dict[int, float]:
    return {
        int(float(row["elapsed_seconds"])): float(row["tp_tps"])
        for row in read_csv(profile_dir / "tp_tps_samples.csv")
        if row.get("stage") == "stage5_tp_surge"
    }


def load_profile(root: Path, profile: str, warmup_seconds: int) -> list[Window]:
    profile_dir = root / profile
    tps = tps_by_second(profile_dir)
    result = []
    for row in read_csv(profile_dir / "block_trace_attribution.csv"):
        second = int(row["elapsed_second"])
        if second < warmup_seconds or second not in tps:
            continue
        def ops(group: str) -> float:
            return float(row[f"{group}_read_ops"]) + float(row[f"{group}_write_ops"])
        total = float(row["total_ops"])
        if total <= 0:
            continue
        result.append(Window(
            profile=profile,
            second=second,
            tps=tps[second],
            total_iops=total,
            tp_iops=ops("tp"),
            ap_iops=ops("ap"),
            other_iops=ops("other"),
            await_ms=float(row["total_await_ms"]),
        ))
    return result


def match_baseline(baseline: list[Window], target: Window) -> Window:
    """Find the TP-only cache state with comparable physical TP read pressure.

    BPF attachment and database startup can move the first observable block
    request by several seconds between otherwise identical cold starts.  The
    TP physical-request rate is the replay-visible cache-warmth marker: it
    falls as the SB/Linux cache fills.  Use it as the primary anchor, retaining
    elapsed time only as a weak deterministic tie-breaker.
    """
    target_rate = max(target.tp_iops, 1.0)
    return min(
        baseline,
        key=lambda row: (
            abs(math.log(max(row.tp_iops, 1.0) / target_rate)),
            abs(row.second - target.second) / 10_000.0,
        ),
    )


def queue_increment(total_iops: float, baseline_iops: float, service_ms: float, queues: int) -> float:
    def await_for(iops: float) -> float:
        rho = min(0.985, iops * service_ms / 1000.0 / queues)
        return service_ms / max(1e-6, 1.0 - rho)
    return max(0.0, await_for(total_iops) - await_for(baseline_iops))


def fit_parameters(baseline: list[Window], train: list[Window]) -> tuple[float, int, float]:
    # Service time is measured only after cache warm-up, from the lower tail of
    # a TP-only run.  Cold-cache delay remains in the aligned baseline state.
    service_ms = max(0.02, percentile([row.await_ms for row in baseline], 0.15))
    candidate_queues = range(1, 129)
    def queue_error(queues: int) -> float:
        values = []
        for row in train:
            base = match_baseline(baseline, row)
            observed = max(0.0, row.await_ms - base.await_ms)
            predicted = queue_increment(row.total_iops, base.total_iops, service_ms, queues)
            values.append(abs(predicted - observed))
        return statistics.fmean(values)
    queues = min(candidate_queues, key=queue_error)

    x_values: list[float] = []
    y_values: list[float] = []
    for row in train:
        base = match_baseline(baseline, row)
        additional = queue_increment(row.total_iops, base.total_iops, service_ms, queues)
        # Physical TP requests are the part whose service latency is exposed
        # to a TP transaction.  The baseline's measured TPS defines capacity.
        x = row.tp_iops / max(base.tps, 1.0) * additional
        y = max(0.0, TERMINALS * 1000.0 / max(row.tps, 1.0)
                - TERMINALS * 1000.0 / max(base.tps, 1.0))
        if x > 1e-9:
            x_values.append(x)
            y_values.append(y)
    weight = sum(x * y for x, y in zip(x_values, y_values)) / max(sum(x * x for x in x_values), 1e-9)
    return service_ms, queues, max(0.0, weight)


def predict(row: Window, baseline: list[Window], service_ms: float, queues: int, weight: float) -> dict[str, float]:
    base = match_baseline(baseline, row)
    extra_await = queue_increment(row.total_iops, base.total_iops, service_ms, queues)
    base_tx_ms = TERMINALS * 1000.0 / max(base.tps, 1.0)
    tx_ms = base_tx_ms + weight * row.tp_iops / max(base.tps, 1.0) * extra_await
    return {
        "baseline_tps": base.tps,
        "baseline_await_ms": base.await_ms,
        "predicted_incremental_await_ms": extra_await,
        "predicted_await_ms": base.await_ms + extra_await,
        "predicted_tps": TERMINALS * 1000.0 / max(tx_ms, 1e-9),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--train", required=True, help="comma-separated AP profiles")
    parser.add_argument("--holdout", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--warmup-seconds", type=int, default=20)
    parser.add_argument(
        "--steady-after-seconds", type=int, default=35,
        help="stage-level recommendation metric begins after AP admission settles",
    )
    args = parser.parse_args()
    names = [item for item in args.train.split(",") if item]
    if args.holdout in names or args.holdout == args.baseline:
        parser.error("holdout must not occur in baseline or training profiles")
    baseline = load_profile(args.root, args.baseline, args.warmup_seconds)
    train = [row for name in names for row in load_profile(args.root, name, args.warmup_seconds)]
    holdout = load_profile(args.root, args.holdout, args.warmup_seconds)
    if not baseline or not train or not holdout:
        parser.error("baseline, training and holdout each need warm aligned trace/TPS windows")
    service_ms, queues, weight = fit_parameters(baseline, train)
    output = []
    for split, rows in (("train", train), ("holdout", holdout)):
        for row in rows:
            prediction = predict(row, baseline, service_ms, queues, weight)
            output.append({
                "split": split, "profile": row.profile, "elapsed_second": row.second,
                "tp_request_iops": round(row.tp_iops, 6),
                "ap_request_iops": round(row.ap_iops, 6),
                "other_request_iops": round(row.other_iops, 6),
                "actual_await_ms": round(row.await_ms, 6),
                "actual_tps": round(row.tps, 6),
                **{name: round(value, 6) for name, value in prediction.items()},
            })
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "cache_state_queue_tps_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output[0]))
        writer.writeheader(); writer.writerows(output)
    metrics = {}
    for split in ("train", "holdout"):
        rows = [row for row in output if row["split"] == split]
        steady = [row for row in rows if int(row["elapsed_second"]) >= args.steady_after_seconds]
        actual_steady = statistics.fmean(row["actual_tps"] for row in steady)
        predicted_steady = statistics.fmean(row["predicted_tps"] for row in steady)
        metrics[split] = {
            "windows": len(rows),
            "await_mae_ms": statistics.fmean(abs(row["predicted_await_ms"] - row["actual_await_ms"]) for row in rows),
            "tps_mape_pct": statistics.fmean(abs(row["predicted_tps"] - row["actual_tps"]) / max(row["actual_tps"], 1.0) * 100.0 for row in rows),
            "steady_windows": len(steady),
            "steady_actual_tps": actual_steady,
            "steady_predicted_tps": predicted_steady,
            "steady_tps_error_pct": abs(predicted_steady - actual_steady) / max(actual_steady, 1.0) * 100.0,
        }
    (args.out_dir / "cache_state_queue_tps_summary.json").write_text(json.dumps({
        "parameters": {"service_ms": service_ms, "effective_queues": queues, "tp_io_delay_weight": weight},
        "warmup_seconds": args.warmup_seconds,
        "steady_after_seconds": args.steady_after_seconds,
        "metrics": metrics,
    }, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
