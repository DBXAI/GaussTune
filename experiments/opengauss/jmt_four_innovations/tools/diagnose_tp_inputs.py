#!/usr/bin/env python3
"""Report independent TP-input failures without changing artifacts or gates."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jmt.common.transaction_evidence import (
    validate_native_counter_window, validate_probe_overhead_evidence,
    validate_tp_command_evidence, validate_transaction_window,
)


def check(function):
    try:
        function()
        return {"passed": True}
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        return {"passed": False, "reason": type(error).__name__ + ": " + str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base = Path("/root/GaussTune/experiments/opengauss/huawei7/validation")
    roots = {
        "strict": base / "model_calibration_20260824/strict-ppt-evidence-matrix/pipeline/configs/sysbench",
        "native": base / "full_current_20260815/pipeline-native-v2/sysbench",
    }
    rows, hashes = [], {}
    for method, directory in roots.items():
        for stage in ("S1", "S2", "S3", "S4", "S5"):
            config_path = directory / stage / "pipeline-config.json"
            config = json.loads(config_path.read_text())
            collection_path = Path(config["tp_collection"])
            collection = json.loads(collection_path.read_text())
            overhead_path = Path(config["buffer_probe_overhead"])
            overhead = json.loads(overhead_path.read_text())
            tx_path = Path(collection["transaction_evidence"])
            transaction = json.loads(tx_path.read_text())
            for path in (config_path, collection_path, overhead_path, tx_path):
                hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            block = collection.get("block_summary", {})
            expected_encoding = (
                collection.get("buffer_probe_encoding") if method == "strict"
                else "huawei7.tp-native-observer/v1")
            row = {
                "method": method, "stage": stage, "config": str(config_path),
                "TP_command_and_capture_source_evidence": check(
                    lambda: validate_tp_command_evidence(
                        collection, machine_fingerprint=config["machine_fingerprint"],
                        benchmark="sysbench")),
                "probe_overhead_evidence": check(
                    lambda: validate_probe_overhead_evidence(
                        overhead, machine_fingerprint=config["machine_fingerprint"],
                        benchmark="sysbench")),
                "probe_encoding_matches_collection": (
                    expected_encoding == overhead.get("buffer_probe_encoding")),
                "expected_probe_encoding": expected_encoding,
                "overhead_probe_encoding": overhead.get("buffer_probe_encoding"),
                "transaction_scored_seconds": transaction.get("scored_seconds"),
                "device_scored_seconds": block.get("duration_seconds"),
                "absolute_transaction_anchors_present": (
                    "measurement_window" in transaction),
                "transaction_device_alignment": check(
                    lambda: validate_transaction_window(collection, transaction)),
            }
            if method == "native":
                row["native_counter_alignment"] = check(
                    lambda: validate_native_counter_window(collection, transaction))
            rows.append(row)
            print(method, stage,
                  "source:", row["TP_command_and_capture_source_evidence"]["passed"],
                  "encoding:", row["probe_encoding_matches_collection"],
                  "window:", row["transaction_device_alignment"]["passed"], flush=True)
    result = {
        "schema": "jmt.TP-input-diagnosis/v1",
        "created_at": datetime.datetime.now().astimezone().isoformat(),
        "rows": rows, "evidence_sha256": hashes,
        "source_relocation_checks_passed": sum(
            r["TP_command_and_capture_source_evidence"]["passed"] for r in rows),
        "absolute_transaction_device_alignment_passed": sum(
            r["transaction_device_alignment"]["passed"] for r in rows),
        "qualification": "Diagnosis only. Old inputs are not rewritten/rescaled or "
        "promoted to current-machine calibration.",
        "automatic_recommendation_qualified": False,
    }
    with args.out.open("x") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
