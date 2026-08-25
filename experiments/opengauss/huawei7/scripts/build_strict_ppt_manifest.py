#!/usr/bin/env python3
"""Assemble the fail-closed strict PPT artifact manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from huawei7.provenance import sha256


def _read(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact root must be an object: %s" % path)
    return value


def _row(path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine-fingerprint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--matrix-dir", type=Path, required=True)
    parser.add_argument("--fit-dir", type=Path, required=True)
    parser.add_argument("--overhead-dir", type=Path, required=True)
    parser.add_argument(
        "--machine", type=Path,
        default=Path("validation/live_component/fio_surface_20260814/machine.json"),
    )
    parser.add_argument(
        "--memory-budget", type=Path,
        default=Path("validation/full_current_20260815/memory/memory-budget.json"),
    )
    parser.add_argument(
        "--ap-model-bundle", type=Path,
        default=Path("validation/full_current_20260815/ap/ap-model-bundle.json"),
    )
    parser.add_argument(
        "--fio-validation-sysbench", type=Path,
        default=Path(
            "validation/full_current_20260815/fio/pure-read/holdout-report-v2.json"
        ),
    )
    parser.add_argument(
        "--fio-validation-tpcc", type=Path,
        default=Path("validation/full_current_20260815/fio/holdout-report-v2.json"),
    )
    parser.add_argument(
        "--service-calibration", type=Path,
        default=Path("validation/full_current_20260815/fio/service-times-v2.json"),
    )
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/openGauss/data"))
    parser.add_argument(
        "--sb-sample-count", type=int, default=3,
        help="number of SB candidates; default matches the three measured TP points",
    )
    args = parser.parse_args()
    if args.sb_sample_count < 2:
        raise ValueError("--sb-sample-count must be at least two")

    common = {
        "machine": _row(args.machine),
        "memory_budget": _row(args.memory_budget),
        "ap_model_bundle": _row(args.ap_model_bundle),
        "openGauss_data_dir": str(args.data_dir.resolve()),
    }
    storage = {
        "sysbench": {
            "fio_validation": _row(args.fio_validation_sysbench),
            "service_calibration": _row(args.service_calibration),
        },
        "benchbase-tpcc": {
            "fio_validation": _row(args.fio_validation_tpcc),
            "service_calibration": _row(args.service_calibration),
        },
    }
    topologies = {}
    for benchmark, short in (("sysbench", "sysbench"), ("benchbase-tpcc", "tpcc")):
        topologies[benchmark] = {}
        for topology in ("n128", "n144"):
            fit_chain = args.fit_dir / benchmark / topology
            overhead = args.overhead_dir / benchmark / topology / "probe_overhead.json"
            matrix = _read(args.matrix_dir / benchmark / topology / "matrix.json")
            samples = matrix.get("samples")
            if not isinstance(samples, list) or not samples:
                raise ValueError("matrix has no samples: %s/%s" % (benchmark, topology))
            representative = next(
                row for row in samples if int(row["repeat"]) == 3
                and int(row["shared_buffers_mb"]) == 5120
            )
            collection = Path(str(representative["collection"]))
            topologies[benchmark][topology[1:]] = {
                "os_cache_model": _row(fit_chain / "os-cache-model.json"),
                "tp_sweep": _row(fit_chain / "tp-sweep.json"),
                "tp_calibration": _row(
                    fit_chain / "tp-latency-calibration.json"
                ),
                "tp_collection": _row(collection),
                "buffer_probe_overhead": _row(overhead),
            }
    document = {
        "schema": "huawei7.ppt-pipeline-artifacts/v1",
        "machine_fingerprint": args.machine_fingerprint,
        "common": common,
        "storage": storage,
        "topologies": topologies,
        "memory_grid_mb": 64,
        "sb_sample_count": args.sb_sample_count,
        "hit_plateau_fraction": .99,
        "ap_mix_tolerance": .05,
        "practical_tps_tolerance": .03,
        "minimum_tp_access_fraction": .90,
        "maximum_hit_mismatch_fraction": .01,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
