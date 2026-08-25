"""Final fail-closed audit for one complete fresh-machine reproduction."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Dict, Mapping, Sequence

from .provenance import sha256
from .stability import (
    assess_precondition_convergence, cache_normalization_from_text,
    storage_quiescence_from_text,
)
from .stage_execution import (
    read_recommendations, tpcc_reset_logical_state,
    validate_stage_raw_evidence,
)
from .stage_spec import Stage, read_stage_spec


def _document(path: Path, schema: str) -> Dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError("unexpected artifact schema: %s" % path)
    return value


def _document_one_of(path: Path, schemas: Sequence[str]) -> Dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") not in schemas:
        raise ValueError("unexpected artifact schema: %s" % path)
    return value


def _same(left: object, right: object, label: str) -> None:
    if not math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("%s differs across final evidence" % label)


def _validate_episode(
    row: Mapping[str, object], *, machine: str, stage: Stage,
    benchmark: str, repeat: int, dataset_fingerprint: str,
    stable_protocol: bool, expected_cache_oids: Sequence[int],
) -> Mapping[str, object]:
    summary_path = Path(str(row.get("summary", "")))
    if (
        not summary_path.is_file()
        or sha256(summary_path) != row.get("summary_sha256")
    ):
        raise ValueError("final stage summary is missing or changed")
    restart_log = Path(str(row.get("restart_log", "")))
    if (
        not restart_log.is_file()
        or sha256(restart_log) != row.get("restart_log_sha256")
    ):
        raise ValueError("stage restart log is missing or changed")
    summary = _document(
        summary_path,
        (
            "huawei7.real-stage-episode/v3"
            if stable_protocol else "huawei7.real-stage-episode/v2"
        ),
    )
    if (
        summary.get("valid") is not True
        or summary.get("machine_fingerprint") != machine
        or summary.get("dataset_fingerprint") != dataset_fingerprint
        or summary.get("stage") != stage.name
        or summary.get("benchmark") != benchmark
        or int(summary.get("repeat", -1)) != repeat
        or int(summary.get("tp_terminals", -1)) != stage.tp_terminals
        or int(summary.get("tp_baseline_terminals", -1))
        != stage.tp_baseline_terminals
        or int(summary.get("tp_surge_terminals", -1))
        != stage.tp_surge_terminals
        or tuple(int(value) for value in summary.get("ap_queries", []))
        != stage.ap_queries
        or float(summary.get("warmup_seconds", 0)) < 10
        or float(summary.get("measurement_seconds", 0)) < 29
        or summary.get("ap_failures") != []
        or int(summary.get("ap_active_slots_cancelled_at_boundary", -1))
        != len(stage.ap_queries)
    ):
        raise ValueError("final episode identity/topology differs from PPT")
    if stable_protocol:
        if summary.get("connection_transport") \
                != "password-authenticated-dedicated-role":
            raise ValueError(
                "final reproduction cannot use diagnostic local peer transport"
            )
        cache = cache_normalization_from_text(
            restart_log.read_text(encoding="utf-8", errors="replace"),
            expected_cache_oids,
        )
        if row.get("cache_normalization") != cache:
            raise ValueError("stable episode cache normalization differs")
    validate_stage_raw_evidence(summary)
    _same(row["throughput_tps"], summary["throughput_tps"], "episode TPS")
    _same(row["predicted_tps"], summary["predicted_tps"], "episode prediction")
    return summary


def _validate_normalized_tpcc_episode(
    row: Mapping[str, object], *, machine: str, dataset_fingerprint: str,
    terminals: int, inputs: Mapping[str, object],
    reset_contract: Mapping[str, object],
) -> Mapping[str, object]:
    """Rehash one v4 TPCC reset, precondition, and quiescence chain."""

    reset_ref = row.get("dataset_reset")
    precondition_ref = row.get("adaptive_precondition")
    checkpoint_path = Path(str(row.get("checkpoint_log", "")))
    if not isinstance(reset_ref, dict) or not isinstance(precondition_ref, dict):
        raise ValueError("normalized TPCC episode lacks initial-state evidence")
    reset_path = Path(str(reset_ref.get("path", "")))
    reset_log = Path(str(reset_ref.get("log", "")))
    precondition_path = Path(str(precondition_ref.get("path", "")))
    if (
        not reset_path.is_file()
        or sha256(reset_path) != reset_ref.get("sha256")
        or not reset_log.is_file()
        or sha256(reset_log) != reset_ref.get("log_sha256")
        or not precondition_path.is_file()
        or sha256(precondition_path) != precondition_ref.get("sha256")
        or not checkpoint_path.is_file()
        or sha256(checkpoint_path) != row.get("checkpoint_log_sha256")
    ):
        raise ValueError("normalized TPCC episode artifacts changed")

    reset = json.loads(reset_path.read_text(encoding="utf-8"))
    counts = reset.get("table_row_counts") if isinstance(reset, dict) else None
    expected_counts = (
        reset.get("expected_exact_row_counts")
        if isinstance(reset, dict) else None
    )
    district = (
        reset.get("district_next_order_id") if isinstance(reset, dict) else None
    )
    warehouses = int(reset_contract.get("warehouses", 0))
    required_counts = {
        "warehouse": warehouses,
        "district": warehouses * 10,
        "customer": warehouses * 10 * 3000,
        "history": warehouses * 10 * 3000,
        "oorder": warehouses * 10 * 3000,
        "new_order": warehouses * 10 * 900,
        "stock": warehouses * 100000,
        "item": 100000,
    }
    if (
        not isinstance(reset, dict)
        or reset.get("schema") != "huawei7.tpcc-dataset-reset/v1"
        or reset.get("valid") is not True
        or reset.get("machine_fingerprint") != machine
        or reset.get("dataset_fingerprint") != dataset_fingerprint
        or reset.get("connection_transport")
        != "password-authenticated-dedicated-role"
        or reset.get("database") != reset_contract.get("database")
        or int(reset.get("database_oid", 0))
        != int(reset_contract.get("database_oid", -1))
        or int(reset.get("warehouses", 0)) != warehouses
        or int(reset.get("random_seed", -1))
        != int(reset_contract.get("random_seed", -2))
        or reset.get("transaction_weights") != [45, 43, 4, 4, 4]
        or reset.get("runtime_config") != inputs.get("runtime_config")
        or reset.get("dataset_audit") != inputs.get("dataset_audit")
        or not isinstance(counts, dict)
        or expected_counts != required_counts
        or any(
            int(counts.get(name, -1)) != count
            for name, count in required_counts.items()
        )
        or int(counts.get("order_line", 0))
        <= warehouses * 10 * 3000 * 5
        or not isinstance(district, dict)
        or int(district.get("minimum", 0)) != 3001
        or int(district.get("maximum", 0)) != 3001
        or int(reset.get("available_bytes_after_reset", -1))
        < int(reset.get("minimum_free_bytes", 0))
        or int(reset.get("minimum_free_bytes", 0)) <= 0
    ):
        raise ValueError("normalized TPCC reset report is invalid")

    precondition = json.loads(precondition_path.read_text(encoding="utf-8"))
    samples = (
        precondition.get("samples") if isinstance(precondition, dict) else None
    )
    convergence = (
        precondition.get("convergence")
        if isinstance(precondition, dict) else None
    )
    postcondition = (
        precondition.get("between_run_postcondition")
        if isinstance(precondition, dict) else None
    )
    if (
        not isinstance(precondition, dict)
        or precondition.get("schema")
        != "huawei7.tp-adaptive-precondition/v1"
        or precondition.get("valid") is not True
        or precondition.get("converged") is not True
        or precondition.get("benchmark") != "benchbase-tpcc"
        or precondition.get("connection_transport")
        != "password-authenticated-dedicated-role"
        or int(precondition.get("terminals", 0)) != terminals
        or precondition.get("runtime_config") != inputs.get("runtime_config")
        or not isinstance(samples, list)
        or not isinstance(convergence, dict)
        or not isinstance(postcondition, dict)
        or postcondition.get("checkpoint_command")
        != inputs.get("checkpoint_command")
    ):
        raise ValueError("normalized TPCC precondition report is invalid")
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("invalid normalized TPCC precondition sample")
        for name in ("driver_log", "summary", "checkpoint_log"):
            artifact = sample.get(name)
            if not isinstance(artifact, dict):
                raise ValueError("TPCC precondition sample artifact is invalid")
            artifact_path = Path(str(artifact.get("path", "")))
            if (
                not artifact_path.is_file()
                or sha256(artifact_path) != artifact.get("sha256")
            ):
                raise ValueError("TPCC precondition sample artifact changed")
        sample_checkpoint = Path(str(sample["checkpoint_log"]["path"]))
        sample_quiescence = storage_quiescence_from_text(
            sample_checkpoint.read_text(encoding="utf-8", errors="replace")
        )
        if sample.get("storage_quiescence") != sample_quiescence:
            raise ValueError("TPCC precondition sample storage state differs")
    recomputed = assess_precondition_convergence(
        [float(sample["throughput_tps"]) for sample in samples],
        required_tail_runs=int(convergence.get("required_tail_runs", 0)),
        maximum_relative_range=float(
            convergence.get("maximum_relative_range", 0)
        ),
    )
    if recomputed != convergence or recomputed.get("converged") is not True:
        raise ValueError("TPCC precondition convergence does not recompute")
    quiescence = storage_quiescence_from_text(
        checkpoint_path.read_text(encoding="utf-8", errors="replace")
    )
    if row.get("storage_quiescence") != quiescence:
        raise ValueError("TPCC final storage quiescence differs")
    return tpcc_reset_logical_state(reset)


def audit_reproduction(
    *, doctor_path: Path, fresh_doctor_path: Path, dataset_audit_path: Path,
    machine_path: Path, recommendations_path: Path, final_validation_path: Path,
    stage_spec_path: Path,
) -> Dict[str, object]:
    doctor = _document(doctor_path, "huawei7.doctor/v1")
    provenance = doctor.get("provenance")
    if (
        doctor.get("valid") is not True or not isinstance(provenance, dict)
        or provenance.get("valid") is not True
        or provenance.get("mismatches") != []
        or provenance.get("gaussdb_sha256")
        != provenance.get("expected_reference_gaussdb_sha256")
    ):
        raise ValueError("base doctor/provenance is not valid")

    fresh = _document(fresh_doctor_path, "huawei7.fresh-machine-doctor/v1")
    fresh_machine = fresh.get("machine")
    if (
        fresh.get("valid") is not True or fresh.get("failures") != []
        or not isinstance(fresh_machine, dict)
        or not isinstance(fresh.get("provenance"), dict)
        or fresh["provenance"].get("valid") is not True  # type: ignore[index]
        or int(fresh.get("free_bytes", -1))
        < int(fresh.get("minimum_free_bytes", 0))
    ):
        raise ValueError("fresh-machine doctor is not valid")
    if fresh.get("dataset_mode") == "reuse-audited":
        reuse_evidence = fresh.get("dataset_audit_artifact")
        if not isinstance(reuse_evidence, dict):
            raise ValueError("reuse doctor lacks dataset audit artifact")
        reuse_path = Path(str(reuse_evidence.get("path", "")))
        if (
            reuse_path.resolve() != dataset_audit_path.resolve()
            or not reuse_path.is_file()
            or sha256(reuse_path) != reuse_evidence.get("sha256")
        ):
            raise ValueError("reuse doctor dataset audit is missing or changed")

    machine = _document(machine_path, "huawei7.machine/v1")
    fingerprint = str(machine.get("machine_fingerprint", ""))
    if not fingerprint or fresh_machine.get("machine_fingerprint") != fingerprint:
        raise ValueError("machine artifact differs from fresh-machine doctor")

    dataset = json.loads(dataset_audit_path.read_text(encoding="utf-8"))
    if (
        not isinstance(dataset, dict)
        or dataset.get("schema") not in (
            "huawei7.dataset-contract-audit/v2",
            "huawei7.dataset-contract-audit/v3",
        )
    ):
        raise ValueError("unexpected dataset audit schema")
    machine_evidence = dataset.get("machine_artifact")
    contract_evidence = dataset.get("contract_artifact")
    if (
        dataset.get("valid") is not True or dataset.get("failures") != []
        or dataset.get("machine_fingerprint") != fingerprint
        or not isinstance(machine_evidence, dict)
        or not isinstance(contract_evidence, dict)
    ):
        raise ValueError("dataset audit is invalid or belongs to another machine")
    dataset_fingerprint = str(dataset.get("dataset_fingerprint", ""))
    if len(dataset_fingerprint) != 64:
        raise ValueError("dataset audit lacks a stable fingerprint")
    if (
        fresh.get("dataset_mode") == "reuse-audited"
        and fresh.get("dataset_fingerprint") != dataset_fingerprint
    ):
        raise ValueError("reuse doctor dataset fingerprint differs from audit")
    bound_machine = Path(str(machine_evidence.get("path", "")))
    contract = Path(str(contract_evidence.get("path", "")))
    if (
        bound_machine.resolve() != machine_path.resolve()
        or not bound_machine.is_file()
        or sha256(bound_machine) != machine_evidence.get("sha256")
        or not contract.is_file()
        or sha256(contract) != contract_evidence.get("sha256")
        or contract_evidence.get("sha256")
        != fresh.get("dataset_contract_sha256", fresh.get("contract_sha256"))
    ):
        raise ValueError("dataset machine/contract evidence is missing or changed")

    stages = read_stage_spec(stage_spec_path)
    recommendations = read_recommendations(
        recommendations_path, stages, fingerprint,
    )
    if len(recommendations) != 10:
        raise ValueError("final recommendations do not cover 2 x 5 stages")

    final = _document_one_of(
        final_validation_path, (
            "huawei7.real-five-stage-validation/v2",
            "huawei7.real-five-stage-validation/v3",
            "huawei7.real-five-stage-validation/v4",
        ),
    )
    stable_protocol = (
        final.get("schema") in (
            "huawei7.real-five-stage-validation/v3",
            "huawei7.real-five-stage-validation/v4",
        )
    )
    normalized_tpcc_state = (
        final.get("schema") == "huawei7.real-five-stage-validation/v4"
    )
    repeats = int(final.get("repeats", 0))
    episodes = final.get("episodes")
    final_inputs = final.get("input_artifacts")
    if (
        final.get("valid") is not True
        or final.get("accuracy_valid") is not True
        or final.get("recommendations_frozen_before_measurement") is not True
        or final.get("machine_fingerprint") != fingerprint
        or final.get("dataset_fingerprint") != dataset_fingerprint
        or final.get("recommendations_sha256") != sha256(recommendations_path)
        or final.get("benchmarks") != ["sysbench", "benchbase-tpcc"]
        or int(final.get("stage_count", 0)) != 5
        or repeats < 3 or not isinstance(episodes, list)
        or not isinstance(final_inputs, dict)
        or set(final_inputs) != (
            {
                "stage_spec", "recommendations", "runtime_config",
                "restart_command", "dataset_audit", "checkpoint_command",
                "dataset_reset_command", "randomized_schedule",
            }
            if normalized_tpcc_state else {
                "stage_spec", "recommendations", "runtime_config",
                "restart_command", "dataset_audit", "randomized_schedule",
            }
        )
        or len(episodes) != 10 * repeats
        or int(final.get("episode_count", -1)) != len(episodes)
    ):
        raise ValueError("final validation header/count/accuracy is invalid")
    input_paths = {}
    for name, row in final_inputs.items():
        if not isinstance(row, dict):
            raise ValueError("invalid final input artifact: %s" % name)
        path = Path(str(row.get("path", "")))
        if not path.is_file() or sha256(path) != row.get("sha256"):
            raise ValueError("final input artifact is missing or changed: %s" % name)
        input_paths[name] = path
    if (
        input_paths["stage_spec"].resolve() != stage_spec_path.resolve()
        or input_paths["recommendations"].resolve()
        != recommendations_path.resolve()
        or input_paths["dataset_audit"].resolve()
        != dataset_audit_path.resolve()
    ):
        raise ValueError("final stage-spec/recommendation inputs differ from audit")

    schedule = _document(
        input_paths["randomized_schedule"],
        (
            "huawei7.five-stage-randomized-schedule/v2"
            if normalized_tpcc_state
            else "huawei7.five-stage-randomized-schedule/v1"
        ),
    )
    scheduled = schedule.get("episodes")
    schedule_inputs = schedule.get("input_artifacts")
    expected_schedule_inputs = {
        name: row for name, row in final_inputs.items()
        if name != "randomized_schedule"
    }
    schedule_warmup = int(schedule.get("warmup_seconds", -1))
    schedule_measure = int(schedule.get("measure_seconds", -1))
    if stable_protocol:
        if (
            not isinstance(final.get("initial_state_protocol"), dict)
            or final.get("initial_state_protocol")
            != schedule.get("initial_state_protocol")
        ):
            raise ValueError("stable final protocol differs from its schedule")
    if (
        int(schedule.get("seed", -1))
        != int(final.get("randomization_seed", -2))
        or schedule.get("machine_fingerprint") != fingerprint
        or schedule.get("dataset_fingerprint") != dataset_fingerprint
        or int(schedule.get("repeats", -1)) != repeats
        or schedule_warmup < 10 or schedule_measure < 30
        or schedule_inputs != expected_schedule_inputs
        or not isinstance(scheduled, list)
        or len(scheduled) != len(episodes)
    ):
        raise ValueError("final randomized episode schedule is invalid")
    expected_schedule = [
        (
            int(row.get("order", -1)), str(row.get("benchmark", "")),
            int(row.get("repeat", -1)), str(row.get("stage", "")),
        )
        for row in scheduled if isinstance(row, dict)
    ]
    actual_schedule = [
        (
            int(row.get("order", -1)), str(row.get("benchmark", "")),
            int(row.get("repeat", -1)), str(row.get("stage", "")),
        )
        for row in episodes if isinstance(row, dict)
    ]
    if (
        len(expected_schedule) != len(episodes)
        or expected_schedule != actual_schedule
        or sorted(row[0] for row in actual_schedule)
        != list(range(1, len(episodes) + 1))
    ):
        raise ValueError("final episodes differ from randomized schedule")

    by_key = {}
    for raw in episodes:
        if not isinstance(raw, dict):
            raise ValueError("final episode row is not an object")
        key = (str(raw.get("benchmark", "")), str(raw.get("stage", "")),
               int(raw.get("repeat", 0)))
        if key in by_key:
            raise ValueError("duplicate final episode")
        by_key[key] = raw
    throughput = {}
    database_oids = dataset.get("database_oids")
    if stable_protocol and not isinstance(database_oids, dict):
        raise ValueError("dataset audit lacks workload database OIDs")
    expected_cache_oids = (
        sorted(int(value) for value in database_oids.values())
        if isinstance(database_oids, dict) else []
    )
    reset_contract = final.get("dataset_reset")
    if normalized_tpcc_state and (
        not isinstance(reset_contract, dict)
        or reset_contract.get("schema") != "huawei7.tpcc-dataset-reset/v1"
        or reset_contract.get("before_every_tpcc_episode") is not True
        or reset_contract.get(
            "identical_logical_state_across_tpcc_episodes"
        ) is not True
        or int(reset_contract.get("database_oid", 0))
        != int(database_oids.get("benchbase_tpcc", -1))
        or int(reset_contract.get("warehouses", 0)) <= 0
        or int(reset_contract.get("random_seed", -1)) < 0
        or not isinstance(reset_contract.get("baseline_state"), dict)
    ):
        raise ValueError("normalized TPCC reset contract is invalid")
    observed_reset_state = None
    for benchmark in ("sysbench", "benchbase-tpcc"):
        for stage in stages:
            values = []
            for repeat in range(1, repeats + 1):
                key = (benchmark, stage.name, repeat)
                if key not in by_key:
                    raise ValueError("missing final episode: %r" % (key,))
                summary = _validate_episode(
                    by_key[key], machine=fingerprint, stage=stage,
                    benchmark=benchmark, repeat=repeat,
                    dataset_fingerprint=dataset_fingerprint,
                    stable_protocol=stable_protocol,
                    expected_cache_oids=expected_cache_oids,
                )
                if normalized_tpcc_state:
                    if benchmark == "benchbase-tpcc":
                        assert isinstance(reset_contract, dict)
                        state = _validate_normalized_tpcc_episode(
                            by_key[key], machine=fingerprint,
                            dataset_fingerprint=dataset_fingerprint,
                            terminals=stage.tp_baseline_terminals,
                            inputs=final_inputs,
                            reset_contract=reset_contract,
                        )
                        if observed_reset_state is None:
                            observed_reset_state = state
                        elif observed_reset_state != state:
                            raise ValueError(
                                "TPCC reset state differs across holdout episodes"
                            )
                    elif any(
                        name in by_key[key] for name in (
                            "dataset_reset", "adaptive_precondition",
                            "checkpoint_log", "storage_quiescence",
                        )
                    ):
                        raise ValueError(
                            "Sysbench episode contains TPCC initial-state evidence"
                        )
                summary_inputs = summary.get("input_artifacts")
                assert isinstance(summary_inputs, dict)  # validated above
                if (
                    int(summary.get("warmup_seconds", -1)) != schedule_warmup
                    or not schedule_measure - 1
                    <= float(summary.get("measurement_seconds", -1))
                    <= schedule_measure + 2
                ):
                    raise ValueError(
                        "episode measurement window differs from schedule"
                    )
                for name in (
                    "stage_spec", "recommendations", "runtime_config",
                    "dataset_audit",
                ):
                    if summary_inputs.get(name) != final_inputs.get(name):
                        raise ValueError("episode input differs from final run: %s" % name)
                recommendation = recommendations[(benchmark, stage.name)]
                _same(
                    summary["predicted_tps"], recommendation.predicted_tps,
                    "episode frozen prediction",
                )
                if (
                    summary.get("model_result_sha256")
                    != recommendation.model_result_sha256
                ):
                    raise ValueError("episode model result differs from recommendation")
                values.append(float(summary["throughput_tps"]))
            throughput[(benchmark, stage.name)] = values
    if normalized_tpcc_state:
        assert isinstance(reset_contract, dict)
        if observed_reset_state != reset_contract.get("baseline_state"):
            raise ValueError("TPCC reset baseline differs from episode evidence")

    medians = final.get("median_throughput")
    if not isinstance(medians, list) or len(medians) != 10:
        raise ValueError("final validation lacks ten median rows")
    median_index = {
        (str(row.get("benchmark", "")), str(row.get("stage", ""))): row
        for row in medians if isinstance(row, dict)
    }
    maximum_error = float(final.get("maximum_stage_mape", 0))
    if not 0 < maximum_error < 1 or len(median_index) != 10:
        raise ValueError("final accuracy threshold/rows are invalid")
    for key, values in throughput.items():
        row = median_index.get(key)
        if row is None or int(row.get("repeats", 0)) != repeats:
            raise ValueError("missing final median row: %r" % (key,))
        median = statistics.median(values)
        prediction = recommendations[key].predicted_tps
        error = abs(median - prediction) / median
        _same(row["median_tps"], median, "median TPS")
        _same(row["minimum_tps"], min(values), "minimum TPS")
        _same(row["maximum_tps"], max(values), "maximum TPS")
        _same(row["predicted_tps"], prediction, "median prediction")
        _same(row["absolute_prediction_error_fraction"], error, "median error")
        if error > maximum_error:
            raise ValueError("final prediction error exceeds threshold: %r" % (key,))

    paths = {
        "doctor": doctor_path, "fresh_doctor": fresh_doctor_path,
        "dataset_audit": dataset_audit_path, "machine": machine_path,
        "recommendations": recommendations_path,
        "final_validation": final_validation_path,
        "stage_spec": stage_spec_path,
        "runtime_config": input_paths["runtime_config"],
        "restart_command": input_paths["restart_command"],
        "randomized_schedule": input_paths["randomized_schedule"],
    }
    if normalized_tpcc_state:
        paths.update({
            "checkpoint_command": input_paths["checkpoint_command"],
            "dataset_reset_command": input_paths["dataset_reset_command"],
        })
    result = {
        "schema": "huawei7.complete-reproduction-audit/v1",
        "machine_fingerprint": fingerprint,
        "dataset_fingerprint": dataset_fingerprint,
        "benchmarks": ["sysbench", "benchbase-tpcc"],
        "stage_count": 5, "repeats": repeats,
        "episode_count": len(episodes),
        "artifacts": {
            name: {"path": str(path.resolve()), "sha256": sha256(path)}
            for name, path in paths.items()
        },
        "valid": True,
    }
    if stable_protocol:
        result["initial_state_protocol"] = final["initial_state_protocol"]
    return result
