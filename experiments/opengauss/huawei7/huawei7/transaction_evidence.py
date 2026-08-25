"""Versioned, re-verifiable TP transaction counts from benchmark output."""

from __future__ import annotations

import json
import hashlib
import re
import random
import statistics
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple
from xml.etree import ElementTree

from .provenance import sha256, validate_path_hash_evidence
from .dataset import validate_tp_dataset_identity


SCHEMA = "huawei7.transaction-evidence/v1"
COMBINED_SCHEMA = "huawei7.transaction-evidence/v2"
COMMAND_SCHEMAS = ("huawei7.tp-command/v1", "huawei7.tp-command/v2")
BENCHMARKS = ("sysbench", "benchbase-tpcc")
SYSBENCH_TPS = re.compile(r"\[\s*(\d+)s\s*\].*?\btps:\s*([0-9.]+)")


def tp_driver_topology(command: Mapping[str, object]) -> Tuple[Mapping[str, object], ...]:
    """Return and validate the warmup/measurement driver topology."""

    schema = command.get("schema")
    terminals = int(command.get("terminals", 0))
    if schema == "huawei7.tp-command/v1":
        argv = command.get("argv")
        if terminals <= 0 or not isinstance(argv, list) or not argv:
            raise ValueError("legacy TP command has no runnable baseline driver")
        return ({
            "role": "baseline", "terminals": terminals,
            "start_phase": "warmup", "argv": argv,
            "benchbase_xml": command.get("benchbase_xml"),
        },)
    if schema != "huawei7.tp-command/v2":
        raise ValueError("unsupported TP command schema")
    drivers = command.get("drivers")
    if not isinstance(drivers, list) or not drivers:
        raise ValueError("TP command v2 has no drivers")
    rows = []
    for raw in drivers:
        if not isinstance(raw, dict):
            raise ValueError("TP command driver must be an object")
        role = str(raw.get("role", ""))
        start_phase = str(raw.get("start_phase", ""))
        count = int(raw.get("terminals", 0))
        argv = raw.get("argv")
        if (
            role not in ("baseline", "surge")
            or start_phase not in ("warmup", "measurement")
            or count <= 0
            or not isinstance(argv, list) or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ValueError("invalid TP command driver")
        rows.append(raw)
    roles = [str(row["role"]) for row in rows]
    if roles not in (["baseline"], ["baseline", "surge"]):
        raise ValueError("TP drivers must be ordered baseline[, surge]")
    if rows[0]["start_phase"] != "warmup":
        raise ValueError("baseline TP driver must start before warmup")
    if len(rows) == 2 and rows[1]["start_phase"] != "measurement":
        raise ValueError("surge TP driver must start at the measurement boundary")
    if sum(int(row["terminals"]) for row in rows) != terminals:
        raise ValueError("TP driver terminals do not sum to total terminals")
    if int(command.get("baseline_terminals", -1)) != int(rows[0]["terminals"]):
        raise ValueError("TP baseline terminal metadata differs from driver")
    expected_surge = int(rows[1]["terminals"]) if len(rows) == 2 else 0
    if int(command.get("surge_terminals", -1)) != expected_surge:
        raise ValueError("TP surge terminal metadata differs from driver")
    return tuple(rows)


def tp_topology_signature(command: Mapping[str, object]) -> Tuple[int, int, str]:
    drivers = tp_driver_topology(command)
    baseline = int(drivers[0]["terminals"])
    surge = int(drivers[1]["terminals"]) if len(drivers) == 2 else 0
    start = str(drivers[1]["start_phase"]) if len(drivers) == 2 else "none"
    return baseline, surge, start


def _benchbase_argv_signature(argv_raw: object) -> Tuple[str, ...]:
    if not isinstance(argv_raw, list) or not argv_raw:
        raise ValueError("BenchBase driver has no argv")
    argv = [str(value) for value in argv_raw]
    for flag, replacement in (("-c", "<xml-artifact>"), ("-d", "<result-dir>")):
        if argv.count(flag) != 1:
            raise ValueError("BenchBase argv lacks exactly one %s" % flag)
        index = argv.index(flag)
        if index + 1 >= len(argv):
            raise ValueError("BenchBase argv has no value for %s" % flag)
        argv[index + 1] = replacement
    return tuple(argv)


def _validate_benchbase_parameters(
    driver: Mapping[str, object], xml_path: Path,
) -> None:
    parameters = driver.get("benchbase_parameters")
    if (
        not isinstance(parameters, dict)
        or parameters.get("schema") != "huawei7.benchbase-driver-contract/v1"
    ):
        raise ValueError("BenchBase driver lacks semantic XML parameters")
    root = ElementTree.parse(xml_path).getroot()
    work = root.find("./works/work")
    if work is None:
        raise ValueError("BenchBase XML lacks a work interval")
    actual = {
        "schema": "huawei7.benchbase-driver-contract/v1",
        "terminals": int(root.findtext("terminals", "-1")),
        "scale_factor": int(root.findtext("scalefactor", "-1")),
        "batch_size": int(root.findtext("batchsize", "-1")),
        "warmup_seconds": int(work.findtext("warmup", "-1")),
        "measure_seconds": int(work.findtext("time", "-1")),
        "transaction_weights": [
            int(value.strip())
            for value in work.findtext("weights", "").split(",")
            if value.strip()
        ],
    }
    if actual != parameters or actual["terminals"] != int(driver["terminals"]):
        raise ValueError("BenchBase XML differs from its semantic contract")
    argv = driver.get("argv")
    if (
        not isinstance(argv, list)
        or argv.count(str(xml_path)) != 1
        or argv.count(str(driver.get("benchbase_xml", {}).get("result_dir", ""))) != 1
    ):
        raise ValueError("BenchBase argv differs from its XML/result artifacts")


def tp_command_contract_id(command: Mapping[str, object]) -> str:
    material = {
        "machine_fingerprint": command.get("machine_fingerprint"),
        "benchmark": command.get("benchmark"),
        "terminals": command.get("terminals"),
        "warmup_seconds": command.get("warmup_seconds"),
        "measure_seconds": command.get("measure_seconds"),
        "password_env": command.get("password_env"),
        "runtime_config_sha256": command.get("runtime_config_sha256"),
        "dataset": command.get("dataset"),
    }
    if command.get("schema") == "huawei7.tp-command/v2":
        topology = []
        for row in tp_driver_topology(command):
            driver = {
                "role": row.get("role"),
                "terminals": row.get("terminals"),
                "start_phase": row.get("start_phase"),
            }
            if command.get("benchmark") == "sysbench":
                driver["argv"] = row.get("argv")
            else:
                driver["argv_signature"] = _benchbase_argv_signature(
                    row.get("argv")
                )
                driver["benchbase_parameters"] = row.get(
                    "benchbase_parameters"
                )
            topology.append(driver)
        material["driver_topology"] = topology
        material["workload_contract"] = command.get("workload_contract")
    return hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def tp_zero_io_directions(command: Mapping[str, object]) -> Tuple[str, ...]:
    """Return directions that an exact, hash-bound workload cannot issue.

    This is intentionally narrower than inferring semantics from a benchmark
    name.  Only the real Sysbench read-only Lua file emitted by the v2 command
    builder can justify replacing a negative paired-idle write delta with zero.
    """

    contract = command.get("workload_contract")
    if contract is None:
        return ()
    if not isinstance(contract, dict):
        raise ValueError("TP workload contract must be an object")
    if command.get("benchmark") != "sysbench":
        if contract.get("mode") != "read_write":
            raise ValueError("non-Sysbench workload cannot claim read-only I/O")
        return ()
    script = contract.get("script_artifact")
    if (
        contract.get("schema") != "huawei7.tp-workload-contract/v1"
        or contract.get("mode") != "read_only"
        or contract.get("issued_io_directions") != ["R"]
        or contract.get("zero_io_directions") != ["W"]
        or not isinstance(script, dict)
    ):
        raise ValueError("Sysbench zero-I/O claim lacks a read-only contract")
    path = Path(str(script.get("path", "")))
    if (
        path.name != "oltp_read_only.lua"
        or not path.is_file()
        or sha256(path) != script.get("sha256")
    ):
        raise ValueError("Sysbench read-only Lua evidence is missing or changed")
    for driver in tp_driver_topology(command):
        argv = driver.get("argv")
        if not isinstance(argv, list) or argv.count(str(path)) != 1:
            raise ValueError("Sysbench driver does not execute the bound read-only Lua")
    return ("W",)


def validate_tp_command_evidence(
    collection: Mapping[str, object], *, machine_fingerprint: str,
    benchmark: str,
) -> Mapping[str, object]:
    evidence = collection.get("tp_command_artifact")
    if not isinstance(evidence, dict):
        raise ValueError("TP collection lacks command artifact evidence")
    path = Path(str(evidence.get("path", "")))
    if not path.is_file() or sha256(path) != evidence.get("sha256"):
        raise ValueError("TP command artifact is missing or changed")
    command = json.loads(path.read_text(encoding="utf-8"))
    if (
        command.get("schema") not in COMMAND_SCHEMAS
        or command.get("machine_fingerprint") != machine_fingerprint
        or command.get("benchmark") != benchmark
        or int(command.get("terminals", 0)) <= 0
        or int(command.get("warmup_seconds", -1)) < 0
        or int(command.get("measure_seconds", 0)) <= 0
        or int(collection.get("terminals", -1)) != int(command["terminals"])
        or command.get("command_contract_id") != tp_command_contract_id(command)
        or collection.get("tp_command_contract_id")
        != command.get("command_contract_id")
    ):
        raise ValueError("TP command artifact identity/window is invalid")
    drivers = tp_driver_topology(command)
    tp_zero_io_directions(command)
    if command.get("schema") == "huawei7.tp-command/v2":
        baseline, surge, _start = tp_topology_signature(command)
        if (
            int(collection.get("baseline_terminals", -1)) != baseline
            or int(collection.get("surge_terminals", -1)) != surge
        ):
            raise ValueError("TP collection topology differs from command artifact")
        raw = collection.get("raw_artifacts")
        if not isinstance(raw, list):
            raise ValueError("synchronized collection lacks raw artifacts")
        native_method = (
            collection.get("schema") == "huawei7.synchronized-tp-native/v1"
            and collection.get("measurement_method")
            == "native-db-stats+whole-device-completions/v1"
        )
        required = (
            {
                "native_database_stats", "native_stats_source",
                "block_probe_raw", "block_probe_stderr", "block_probe_source",
                "transaction_evidence", "tp_driver_log",
            } if native_method else {
                "buffer_probe_raw", "buffer_probe_stderr", "block_probe_raw",
                "block_probe_stderr", "attribution_snapshots",
                "attribution_observer_log", "normalized_buffer_trace",
                "transaction_evidence", "tp_driver_log",
            }
        )
        kinds = {
            str(row.get("kind", "")) for row in raw if isinstance(row, dict)
        }
        if not required <= kinds or validate_path_hash_evidence(
            raw, "synchronized_collection.raw_artifacts",
        ) < len(raw):
            raise ValueError("synchronized collection raw artifact set is incomplete")
        driver_roles = [
            str(row.get("role", "")) for row in raw
            if isinstance(row, dict) and row.get("kind") == "tp_driver_log"
        ]
        expected_roles = ["baseline"] + (["surge"] if len(drivers) == 2 else [])
        if driver_roles != expected_roles:
            raise ValueError("synchronized collection raw driver logs differ from command")
        by_kind = {
            str(row.get("kind")): row for row in raw
            if isinstance(row, dict) and row.get("kind") != "tp_driver_log"
        }
        if (
            Path(str(by_kind["transaction_evidence"]["path"])).resolve()
            != Path(str(collection.get("transaction_evidence", ""))).resolve()
            or (
                not native_method
                and Path(str(by_kind["normalized_buffer_trace"]["path"])).resolve()
                != Path(str(collection.get("trace_csv", ""))).resolve()
            )
        ):
            raise ValueError("collection derived paths differ from raw artifact bindings")
        if native_method:
            block = collection.get("block_summary")
            scratch = collection.get("instrumentation_output_during_measurement")
            native = collection.get("native_database_stats")
            probe = Path(__file__).resolve().parents[1] / "probes" / (
                "block_rq_completion_total_bcc.py"
                if benchmark == "benchbase-tpcc"
                else "block_rq_completion_total.bt"
            )
            stats_source = Path(__file__).resolve().parents[1] / "huawei7" / "native_stats.py"
            if (
                not isinstance(block, dict)
                or block.get("request_count_method") != "block_rq_complete_whole_device"
                or not isinstance(native, dict)
                or not isinstance(native.get("delta"), dict)
                or native["delta"].get("valid") is not True
                or not isinstance(scratch, dict)
                or scratch.get("filesystem") != "tmpfs"
                or scratch.get("promoted_after_probes_stopped") is not True
                or by_kind["block_probe_source"].get("path") != str(probe.resolve())
                or by_kind["block_probe_source"].get("sha256") != sha256(probe)
                or by_kind["native_stats_source"].get("path") != str(stats_source.resolve())
                or by_kind["native_stats_source"].get("sha256") != sha256(stats_source)
            ):
                raise ValueError("native TP collection evidence is incomplete")
        if collection.get("schema") == "huawei7.synchronized-cache-validation/v2":
            block = collection.get("block_summary")
            scratch = collection.get("instrumentation_output_during_measurement")
            probe = Path(__file__).resolve().parents[1] / "probes" / "block_rq_completion_total.bt"
            buffer_probe = (
                Path(__file__).resolve().parents[1]
                / "probes" / "opengauss_buffer_trace_bcc.py"
            )
            source_rows = [
                row for row in raw if isinstance(row, dict)
                and row.get("kind") == "block_probe_source"
            ]
            buffer_source_rows = [
                row for row in raw if isinstance(row, dict)
                and row.get("kind") == "buffer_probe_source"
            ]
            if (
                not isinstance(block, dict)
                or block.get("request_count_method")
                != "block_rq_complete_whole_device"
                or block.get("service_time_source")
                != "not_collected; independent fio four-class calibration"
                or not isinstance(scratch, dict)
                or scratch.get("filesystem") != "tmpfs"
                or scratch.get("mountpoint") != "/dev/shm"
                or scratch.get("promoted_after_probes_stopped") is not True
                or scratch.get(
                    "promoted_files_fsynced_before_return"
                ) is not True
                or len(source_rows) != 1
                or source_rows[0].get("path") != str(probe.resolve())
                or source_rows[0].get("sha256") != sha256(probe)
                or collection.get("buffer_probe_encoding")
                != "huawei7.buffer-probe-binary/v1"
                or len(buffer_source_rows) != 1
                or buffer_source_rows[0].get("path")
                != str(buffer_probe.resolve())
                or buffer_source_rows[0].get("sha256") != sha256(buffer_probe)
            ):
                raise ValueError(
                    "TP request counts lack exact completion-probe evidence"
                )
    for driver in drivers:
        if benchmark != "benchbase-tpcc":
            continue
        xml = driver.get("benchbase_xml")
        if not isinstance(xml, dict):
            raise ValueError("BenchBase TP driver lacks XML evidence")
        xml_path = Path(str(xml.get("path", "")))
        if not xml_path.is_file() or sha256(xml_path) != xml.get("sha256"):
            raise ValueError("BenchBase XML is missing or changed")
        _validate_benchbase_parameters(driver, xml_path)
    dataset = command.get("dataset")
    if not isinstance(dataset, dict):
        raise ValueError("TP command artifact has no dataset contract")
    if command.get("schema") == "huawei7.tp-command/v2":
        validate_tp_dataset_identity(
            dataset, machine_fingerprint=machine_fingerprint,
            benchmark=benchmark,
        )
    else:
        if benchmark == "sysbench":
            valid_dataset = (
                int(dataset.get("tables", -1)) == 16
                and int(dataset.get("rows_per_table", -1)) == 4_000_000
            )
        else:
            valid_dataset = (
                int(dataset.get("warehouses", -1)) == 125
                and list(dataset.get("transaction_weights", []))
                == [45, 43, 4, 4, 4]
            )
        if not valid_dataset:
            raise ValueError("legacy TP command dataset differs from its contract")
    return command


def validate_probe_overhead_evidence(
    document: Mapping[str, object], *, machine_fingerprint: str, benchmark: str,
) -> None:
    """Reparse every randomized arm and recompute the accepted slowdown."""

    if (
        document.get("schema") != "huawei7.buffer-probe-overhead/v2"
        or document.get("machine_fingerprint") != machine_fingerprint
        or document.get("benchmark") != benchmark
    ):
        raise ValueError("buffer-probe overhead identity/schema is invalid")
    validate_path_hash_evidence(document, "buffer_probe_overhead")
    binary_encoding = (
        document.get("buffer_probe_encoding")
        == "huawei7.buffer-probe-binary/v1"
    )
    native_observer = (
        document.get("buffer_probe_encoding")
        == "huawei7.tp-native-observer/v1"
    )
    if binary_encoding or native_observer:
        source_row = document.get("buffer_probe_source_artifact")
        expected_source = Path(__file__).resolve().parents[1] / "probes" / (
            (
                "block_rq_completion_total_bcc.py"
                if benchmark == "benchbase-tpcc"
                else "block_rq_completion_total.bt"
            ) if native_observer else "opengauss_buffer_trace_bcc.py"
        )
        if (
            not isinstance(source_row, dict)
            or Path(str(source_row.get("path", ""))).resolve()
            != expected_source.resolve()
            or source_row.get("sha256") != sha256(expected_source)
        ):
            raise ValueError("TP observer source is missing or changed")
        if native_observer:
            from .block_trace import raw_device_number
            device = Path(str(document.get("device", "")))
            if (
                not device.is_block_device()
                or int(document.get("raw_device_number", -1))
                != raw_device_number(device)
            ):
                raise ValueError("TP native observer device identity is invalid")
    command_row = document.get("command_artifact")
    if not isinstance(command_row, dict):
        raise ValueError("buffer-probe overhead lacks command artifact")
    command_path = Path(str(command_row.get("path", "")))
    command = json.loads(command_path.read_text(encoding="utf-8"))
    scratch = document.get("instrumentation_output_during_measurement")
    if command.get("schema") == "huawei7.tp-command/v2" and (
        not isinstance(scratch, dict)
        or scratch.get("filesystem") != "tmpfs"
        or scratch.get("mountpoint") != "/dev/shm"
        or scratch.get("promoted_after_probe_stopped") is not True
        or scratch.get("promoted_files_fsynced_before_next_arm") is not True
    ):
        raise ValueError("buffer-probe overhead sink differs from real collection")
    drivers = tp_driver_topology(command)
    baseline = int(drivers[0]["terminals"])
    surge = int(drivers[1]["terminals"]) if len(drivers) == 2 else 0
    if (
        command.get("machine_fingerprint") != machine_fingerprint
        or command.get("benchmark") != benchmark
        or command.get("command_contract_id") != tp_command_contract_id(command)
        or document.get("command_contract_id") != command.get("command_contract_id")
        or int(document.get("terminals", -1)) != int(command.get("terminals", -2))
        or int(document.get("baseline_terminals", -1)) != baseline
        or int(document.get("surge_terminals", -1)) != surge
        or int(command.get("warmup_seconds", -1))
        != int(document.get("warmup_seconds", -2))
        or int(command.get("measure_seconds", -1))
        != int(document.get("measure_seconds", -2))
    ):
        raise ValueError("buffer-probe overhead command/topology differs")
    warmup = int(document.get("warmup_seconds", -1))
    measure = int(document.get("measure_seconds", 0))
    repeats = int(document.get("repeats_per_arm", 0))
    samples = document.get("samples")
    if warmup < 0 or measure <= 0 or repeats < 3 or not isinstance(samples, list):
        raise ValueError("buffer-probe overhead window/repeats are invalid")
    preconditioning = document.get("preconditioning")
    if native_observer and benchmark == "benchbase-tpcc":
        if not isinstance(preconditioning, dict):
            raise ValueError("TPCC observer overhead lacks preconditioning evidence")
        precondition_samples = preconditioning.get("samples")
        minimum_runs = int(preconditioning.get("minimum_runs", 0))
        maximum_runs = int(preconditioning.get("maximum_runs", 0))
        stability_window = int(preconditioning.get("stability_window_runs", 0))
        maximum_span = float(
            preconditioning.get("maximum_tps_span_fraction", -1)
        )
        if (
            preconditioning.get("required") is not True
            or preconditioning.get("settled") is not True
            or stability_window != 3
            or minimum_runs < stability_window
            or maximum_runs < minimum_runs
            or not isinstance(precondition_samples, list)
            or not minimum_runs <= len(precondition_samples) <= maximum_runs
        ):
            raise ValueError("TPCC observer preconditioning is incomplete")
        recent = precondition_samples[-stability_window:]
        if any(
            not isinstance(row, dict)
            or row.get("kind") != "precondition"
            or int(row.get("order", 0)) <= 0
            or float(row.get("tps", 0)) <= 0
            for row in recent
        ):
            raise ValueError("TPCC observer preconditioning samples are invalid")
        recent_tps = [float(row["tps"]) for row in recent]
        observed_span = (
            (max(recent_tps) - min(recent_tps))
            / statistics.median(recent_tps)
        )
        if (
            observed_span > maximum_span
            or abs(
                observed_span
                - float(preconditioning.get("observed_tps_span_fraction", -1))
            ) > 1e-9
        ):
            raise ValueError("TPCC observer preconditioning did not converge")
    recalculated: Dict[str, list[float]] = {"baseline": [], "probe": []}
    identities = set()
    observed_schedule = {}
    for row in samples:
        if not isinstance(row, dict):
            raise ValueError("buffer-probe overhead sample is not an object")
        kind = str(row.get("kind", ""))
        repeat = int(row.get("repeat", 0))
        order = int(row.get("order", 0))
        trace_id = str(row.get("trace_id", ""))
        components = row.get("transaction_components")
        if kind not in recalculated or repeat <= 0 or not trace_id:
            raise ValueError("buffer-probe overhead sample identity is invalid")
        if (
            (kind, repeat) in identities or order in observed_schedule
            or not isinstance(components, list)
        ):
            raise ValueError("duplicate or incomplete buffer-probe overhead sample")
        identities.add((kind, repeat))
        observed_schedule[order] = (kind, repeat)
        for component in components:
            if not isinstance(component, dict):
                raise ValueError("invalid overhead transaction component")
            source_row = component.get("source_artifact")
            if (
                not isinstance(source_row, dict)
                or Path(str(component.get("source", ""))).resolve()
                != Path(str(source_row.get("path", ""))).resolve()
            ):
                raise ValueError("overhead transaction source is not hash-bound")
        expected_roles = [str(driver["role"]) for driver in drivers]
        actual_roles = [str(component.get("role", "")) for component in components]
        actual_warmups = [
            int(component.get("warmup_seconds", -1)) for component in components
        ]
        if (
            actual_roles != expected_roles
            or actual_warmups != [warmup] + ([0] if len(drivers) == 2 else [])
        ):
            raise ValueError("overhead transaction topology/window differs from command")
        if len(components) == 1:
            component = components[0]
            if not isinstance(component, dict):
                raise ValueError("invalid overhead transaction component")
            evidence = build_transaction_evidence(
                benchmark=benchmark, source=Path(str(component.get("source", ""))),
                machine_fingerprint=machine_fingerprint, trace_id=trace_id,
                warmup_seconds=int(component.get("warmup_seconds", -1)),
                measure_seconds=measure,
            )
        else:
            evidence = build_combined_transaction_evidence(
                benchmark=benchmark, components=components,
                machine_fingerprint=machine_fingerprint, trace_id=trace_id,
                measure_seconds=measure,
            )
        tps = float(evidence["transactions"]) / float(evidence["scored_seconds"])
        if abs(tps - float(row.get("tps", -1))) > max(1e-9, abs(tps) * 1e-9):
            raise ValueError("buffer-probe overhead TPS differs from native output")
        if kind == "probe":
            raw_row = row.get("buffer_raw_artifact")
            if not isinstance(raw_row, dict):
                raise ValueError("probed overhead arm lacks raw probe artifact")
            raw_path = Path(str(raw_row["path"]))
            if native_observer:
                accesses = sum(
                    line.startswith("WINDOW,")
                    for line in raw_path.read_text(
                        encoding="utf-8", errors="replace",
                    ).splitlines()
                )
                expected_accesses = int(row.get("probe_observation_windows", -1))
            elif binary_encoding:
                from .trace import inspect_binary_probe
                summary = inspect_binary_probe(raw_path)
                accesses = int(summary["access_records"])
                expected_accesses = int(row.get("probe_access_records", -1))
            else:
                accesses = sum(
                    line.startswith("ACCESS_A,")
                    for line in raw_path.read_text(
                        encoding="utf-8", errors="replace",
                    ).splitlines()
                )
                expected_accesses = int(row.get("probe_access_fragments", -1))
            if accesses <= 0 or accesses != expected_accesses:
                raise ValueError("buffer-probe ACCESS evidence differs from sample")
        recalculated[kind].append(tps)
    if any(len(rows) != repeats for rows in recalculated.values()):
        raise ValueError("buffer-probe overhead does not contain every paired arm")
    expected_schedule = [
        (kind, repeat) for repeat in range(1, repeats + 1)
        for kind in ("baseline", "probe")
    ]
    random.Random(int(document.get("randomization_seed", -1))).shuffle(
        expected_schedule,
    )
    if [observed_schedule.get(order) for order in range(1, 2 * repeats + 1)] != expected_schedule:
        raise ValueError("buffer-probe overhead schedule was not the declared randomization")
    baseline_median = statistics.median(recalculated["baseline"])
    probe_median = statistics.median(recalculated["probe"])
    slowdown = (baseline_median - probe_median) / baseline_median
    maximum = float(document.get("maximum_slowdown_fraction", -1))
    if (
        abs(baseline_median - float(document.get("baseline_median_tps", -1))) > 1e-9
        or abs(probe_median - float(document.get("probe_median_tps", -1))) > 1e-9
        or abs(slowdown - float(document.get("slowdown_fraction", 2))) > 1e-9
        or document.get("valid") is not (slowdown <= maximum)
    ):
        raise ValueError("buffer-probe overhead aggregate was not recomputed honestly")


def build_transaction_evidence(
    *, benchmark: str, source: Path, machine_fingerprint: str,
    trace_id: str, warmup_seconds: int, measure_seconds: int,
) -> Dict[str, object]:
    if benchmark not in BENCHMARKS:
        raise ValueError("unsupported TP benchmark: %s" % benchmark)
    if not source.is_file() or not machine_fingerprint or not trace_id:
        raise ValueError("source, machine fingerprint and trace ID are required")
    if warmup_seconds < 0 or measure_seconds <= 0:
        raise ValueError("invalid benchmark evidence window")
    if benchmark == "sysbench":
        text = source.read_text(encoding="utf-8", errors="replace")
        rows = [
            (int(second), float(tps))
            for second, tps in SYSBENCH_TPS.findall(text)
            if warmup_seconds < int(second) <= warmup_seconds + measure_seconds
        ]
        seconds = [second for second, _ in rows]
        if len(rows) < max(1, measure_seconds - 2):
            raise ValueError("sysbench log has an incomplete scored window")
        if any(right <= left for left, right in zip(seconds, seconds[1:])):
            raise ValueError("sysbench report seconds are not strictly increasing")
        transactions = sum(tps for _, tps in rows)
        scored_seconds = float(len(rows))
        method = "sum of post-warmup one-second sysbench TPS reports"
        details: Mapping[str, object] = {
            "report_seconds": seconds,
            "report_tps": [tps for _, tps in rows],
        }
    else:
        document = json.loads(source.read_text(encoding="utf-8"))
        transactions = float(document["Measured Requests"])
        throughput = float(document["Throughput (requests/second)"])
        if transactions <= 0 or throughput <= 0:
            raise ValueError("BenchBase summary has no measured transactions")
        scored_seconds = transactions / throughput
        if abs(scored_seconds - measure_seconds) > max(2.0, measure_seconds * .05):
            raise ValueError("BenchBase measured window differs from requested duration")
        method = "BenchBase Measured Requests / reported throughput window"
        details = {"reported_throughput": throughput}
    if transactions <= 0 or scored_seconds <= 0:
        raise ValueError("transaction evidence must be positive")
    return {
        "schema": SCHEMA, "benchmark": benchmark,
        "machine_fingerprint": machine_fingerprint, "trace_id": trace_id,
        "warmup_seconds": warmup_seconds, "requested_measure_seconds": measure_seconds,
        "transactions": transactions, "scored_seconds": scored_seconds,
        "method": method, "details": dict(details),
        "source_path": str(source.resolve()), "source_sha256": sha256(source),
        "source_artifact": {
            "path": str(source.resolve()), "sha256": sha256(source),
        },
        "valid": True,
    }


def build_combined_transaction_evidence(
    *, benchmark: str, components: Sequence[Mapping[str, object]],
    machine_fingerprint: str, trace_id: str, measure_seconds: int,
) -> Dict[str, object]:
    """Combine simultaneous baseline and surge driver results for one window."""

    if len(components) < 2:
        raise ValueError("combined transaction evidence requires multiple drivers")
    rows = []
    roles = []
    for component in components:
        role = str(component.get("role", ""))
        source = Path(str(component.get("source", "")))
        warmup = int(component.get("warmup_seconds", -1))
        if role not in ("baseline", "surge") or role in roles or warmup < 0:
            raise ValueError("invalid combined transaction component")
        roles.append(role)
        evidence = build_transaction_evidence(
            benchmark=benchmark, source=source,
            machine_fingerprint=machine_fingerprint,
            trace_id=trace_id + ":" + role,
            warmup_seconds=warmup, measure_seconds=measure_seconds,
        )
        rows.append(dict(evidence, role=role))
    if roles != ["baseline", "surge"]:
        raise ValueError("combined evidence must contain baseline then surge")
    seconds = [float(row["scored_seconds"]) for row in rows]
    tolerance = max(2.0, measure_seconds * .05)
    if max(abs(value - measure_seconds) for value in seconds) > tolerance:
        raise ValueError("combined TP driver window differs from requested duration")
    scored_seconds = statistics.median(seconds)
    return {
        "schema": COMBINED_SCHEMA, "benchmark": benchmark,
        "machine_fingerprint": machine_fingerprint, "trace_id": trace_id,
        "requested_measure_seconds": measure_seconds,
        "transactions": sum(float(row["transactions"]) for row in rows),
        "scored_seconds": scored_seconds,
        "method": "sum of simultaneous baseline and measurement-phase surge drivers",
        "components": rows, "valid": True,
    }


def read_transaction_evidence(
    path: Path, *, machine_fingerprint: str, trace_id: str,
    benchmark: str,
) -> Tuple[float, float, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        document.get("schema") not in (SCHEMA, COMBINED_SCHEMA)
        or document.get("machine_fingerprint") != machine_fingerprint
        or document.get("trace_id") != trace_id
        or document.get("benchmark") != benchmark
        or document.get("valid") is not True
    ):
        raise ValueError("transaction evidence identity/schema is invalid")
    if document.get("schema") == SCHEMA:
        source = Path(str(document.get("source_path", "")))
        if not source.is_file() or sha256(source) != document.get("source_sha256"):
            raise ValueError("transaction evidence source is missing or changed")
    else:
        components = document.get("components")
        if not isinstance(components, list) or len(components) != 2:
            raise ValueError("combined transaction evidence lacks two components")
        recomputed_transactions = 0.0
        recomputed_seconds = []
        roles = []
        for row in components:
            if not isinstance(row, dict):
                raise ValueError("combined transaction component is invalid")
            role = str(row.get("role", ""))
            roles.append(role)
            if (
                row.get("machine_fingerprint") != machine_fingerprint
                or row.get("benchmark") != benchmark
                or row.get("trace_id") != trace_id + ":" + role
            ):
                raise ValueError("combined transaction component identity is invalid")
            source = Path(str(row.get("source_path", "")))
            rebuilt = build_transaction_evidence(
                benchmark=benchmark, source=source,
                machine_fingerprint=machine_fingerprint,
                trace_id=str(row.get("trace_id", "")),
                warmup_seconds=int(row.get("warmup_seconds", -1)),
                measure_seconds=int(row.get("requested_measure_seconds", 0)),
            )
            if (
                rebuilt["source_sha256"] != row.get("source_sha256")
                or abs(float(rebuilt["transactions"]) - float(row.get("transactions", -1))) > 1e-9
                or abs(float(rebuilt["scored_seconds"]) - float(row.get("scored_seconds", -1))) > 1e-9
            ):
                raise ValueError("combined transaction component changed")
            recomputed_transactions += float(rebuilt["transactions"])
            recomputed_seconds.append(float(rebuilt["scored_seconds"]))
        if roles != ["baseline", "surge"]:
            raise ValueError("combined transaction roles are invalid")
        if abs(recomputed_transactions - float(document.get("transactions", -1))) > 1e-9:
            raise ValueError("combined transaction total is invalid")
        if abs(statistics.median(recomputed_seconds) - float(document.get("scored_seconds", -1))) > 1e-9:
            raise ValueError("combined transaction window is invalid")
    transactions = float(document["transactions"])
    seconds = float(document["scored_seconds"])
    if transactions <= 0 or seconds <= 0:
        raise ValueError("transaction evidence count/window is invalid")
    return transactions, seconds, sha256(path)
