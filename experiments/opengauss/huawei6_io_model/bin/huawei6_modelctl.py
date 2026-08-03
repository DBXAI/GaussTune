#!/usr/bin/env python3
"""Bootstrap, validate, and run the Huawei6 model on a new machine."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from portable_joint_model import predict_file, read_json


ROOT = Path(__file__).resolve().parents[1]
STATE_SCHEMA = "huawei6.modelctl-state/v1"
CONFIG_SCHEMA = "huawei6.portable-config/v1"


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Controller:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.config = expand_env(read_json(self.config_path))
        if self.config.get("schema") != CONFIG_SCHEMA:
            raise ValueError(f"config schema must be {CONFIG_SCHEMA}")
        self.workspace = Path(str(self.config["workspace"])).resolve()
        self.state_path = self.workspace / "state.json"
        self.inventory_path = self.workspace / "machine_inventory.json"
        self.surface_root = self.workspace / "storage_surface"
        self.surface_path = self.surface_root / "frozen" / "frozen_surface.json"
        self.anchor_root = self.workspace / "tp_path_anchors"
        self.anchors_path = self.anchor_root / "anchors.json"
        self.model_path = self.workspace / "model" / "frozen_model.json"
        self.holdout_root = self.workspace / "model_holdout"
        self.prediction_root = self.workspace / "predictions"
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            self.state = read_json(self.state_path)
            old_hash = self.state.get("config_sha256")
            if old_hash and old_hash != hash_file(self.config_path):
                raise RuntimeError(
                    "workspace belongs to a different config; use a new workspace or restore the original config"
                )
        else:
            self.state = {
                "schema": STATE_SCHEMA,
                "created_epoch_seconds": time.time(),
                "config_path": str(self.config_path),
                "config_sha256": hash_file(self.config_path),
                "stages": {},
            }
            self.save_state()

    def save_state(self) -> None:
        self.state["updated_epoch_seconds"] = time.time()
        atomic_json(self.state_path, self.state)

    def complete(self, name: str, artifacts: list[Path] | None = None) -> bool:
        stage = self.state["stages"].get(name, {})
        if stage.get("status") != "completed":
            return False
        expected = {str(Path(item["path"]).resolve()): item for item in stage.get("artifacts", [])}
        for path in artifacts or []:
            resolved = path.resolve()
            recorded = expected.get(str(resolved))
            if recorded is None or not resolved.exists():
                return False
            if resolved.stat().st_size != int(recorded["size_bytes"]):
                return False
            digest = recorded.get("sha256")
            if digest is not None and hash_file(resolved) != digest:
                return False
        return True

    @contextmanager
    def stage(self, name: str, artifacts: list[Path] | None = None):
        if self.complete(name, artifacts):
            print(json.dumps({"stage": name, "status": "already_completed"}), flush=True)
            yield False
            return
        self.state["stages"][name] = {
            "status": "running", "started_epoch_seconds": time.time(),
        }
        self.save_state()
        try:
            yield True
            recorded = []
            for path in artifacts or []:
                if not path.exists():
                    raise RuntimeError(f"stage {name} did not create {path}")
                size = path.stat().st_size
                recorded.append({
                    "path": str(path),
                    "size_bytes": size,
                    "sha256": hash_file(path) if size <= 16 * 1024 * 1024 else None,
                })
            self.state["stages"][name] = {
                "status": "completed",
                "completed_epoch_seconds": time.time(),
                "artifacts": recorded,
            }
            self.save_state()
            print(json.dumps({"stage": name, "status": "completed"}), flush=True)
        except Exception as exc:
            self.state["stages"][name] = {
                "status": "failed",
                "failed_epoch_seconds": time.time(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.save_state()
            raise

    def run(self, command: list[str], cwd: Path | None = None) -> None:
        print(json.dumps({"exec": command, "cwd": str(cwd) if cwd else None}), flush=True)
        subprocess.run(command, cwd=cwd, check=True)

    def database_command(self, action: str) -> list[str]:
        database = self.config["database"]
        gausshome = str(database["gausshome"])
        data_dir = str(database["data_dir"])
        gs_ctl = str(database.get("gs_ctl", Path(gausshome) / "bin" / "gs_ctl"))
        library_path = str(database.get(
            "library_path", f"{gausshome}/lib:{gausshome}/lib/postgresql",
        ))
        if action == "status":
            inner = [gs_ctl, "status", "-D", data_dir]
        elif action == "stop":
            inner = [gs_ctl, "stop", "-D", data_dir, "-m", "fast"]
        elif action == "start":
            inner = [
                gs_ctl, "start", "-D", data_dir, "-l",
                str(self.workspace / "opengauss_modelctl_start.log"),
            ]
        else:
            raise ValueError(action)
        os_user = str(database.get("os_user", ""))
        if os_user and os.geteuid() == 0:
            shell = f"export LD_LIBRARY_PATH={shlex.quote(library_path)}; {shlex.join(inner)}"
            return ["su", "-", os_user, "-c", shell]
        return inner

    def database_running(self) -> bool:
        result = subprocess.run(
            self.database_command("status"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def ensure_database(self, running: bool) -> None:
        current = self.database_running()
        if running and not current:
            self.run(self.database_command("start"))
        elif not running and current:
            self.run(self.database_command("stop"))

    def inventory(self) -> dict[str, Any]:
        device = str(self.config["device"])
        sys_block = Path("/sys/block") / device
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = value.strip()
        inventory = {
            "created_epoch_seconds": time.time(),
            "hostname": platform.node(),
            "kernel": platform.release(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "memory": meminfo,
            "device": {
                "name": device,
                "size_sectors": int((sys_block / "size").read_text().strip()),
                "logical_block_size": int((sys_block / "queue/logical_block_size").read_text().strip()),
                "physical_block_size": int((sys_block / "queue/physical_block_size").read_text().strip()),
                "rotational": int((sys_block / "queue/rotational").read_text().strip()),
                "model": (
                    (sys_block / "device/model").read_text(errors="replace").strip()
                    if (sys_block / "device/model").exists() else "unknown"
                ),
            },
            "tools": {
                name: shutil.which(name) for name in (
                    "python3", "sysbench", "bpftrace", "taskset", "stdbuf", "su",
                )
            },
        }
        atomic_json(self.inventory_path, inventory)
        return inventory

    def doctor(self) -> None:
        with self.stage("doctor", [self.inventory_path]) as execute:
            if not execute:
                return
            if "${" in json.dumps(self.config):
                raise RuntimeError("config contains unresolved ${ENVIRONMENT_VARIABLE} references")
            if parse_bool(self.config.get("requirements", {}).get("require_root", True)) and os.geteuid() != 0:
                raise RuntimeError("root is required for BPF tracepoints and optional cache dropping")
            missing = [
                name for name in ("sysbench", "bpftrace", "taskset", "stdbuf")
                if shutil.which(name) is None
            ]
            if missing:
                raise RuntimeError(f"missing required tools: {', '.join(missing)}")
            device = Path("/sys/block") / str(self.config["device"])
            if not device.exists():
                raise RuntimeError(f"block device is missing: {device}")
            tracepoint_roots = (
                Path("/sys/kernel/tracing/events/block/block_rq_issue"),
                Path("/sys/kernel/debug/tracing/events/block/block_rq_issue"),
            )
            if not any(path.exists() for path in tracepoint_roots):
                raise RuntimeError("block_rq_issue tracepoint is not available")
            storage = self.config["storage_probe"]
            if int(storage.get("ap_block_kib", 128)) != 128:
                raise RuntimeError("portable model v1 requires storage_probe.ap_block_kib=128")
            if int(storage.get("repeats", 2)) != 2:
                raise RuntimeError("portable model v1 requires storage_probe.repeats=2")
            file_dir = Path(str(storage["file_dir"])).resolve()
            file_dir.mkdir(parents=True, exist_ok=True)
            data_dir = Path(str(self.config["database"]["data_dir"])).resolve()
            try:
                file_dir.relative_to(data_dir)
            except ValueError:
                pass
            else:
                raise RuntimeError("storage_probe.file_dir must not be inside the openGauss data directory")
            free_gib = shutil.disk_usage(file_dir).free / 1024**3
            minimum = float(storage.get("minimum_free_gib", 8.0))
            if free_gib < minimum:
                raise RuntimeError(f"only {free_gib:.1f} GiB free at {file_dir}; need {minimum:.1f} GiB")
            tp_template = list(self.config["tp_anchor"].get("command", []))
            if not tp_template:
                raise RuntimeError("tp_anchor.command is required")
            executable = str(tp_template[0])
            if shutil.which(executable) is None and not Path(executable).is_file():
                raise RuntimeError(f"TP command executable is missing: {executable}")
            database = self.config["database"]
            for name, default in (
                ("gsql", Path(str(database["gausshome"])) / "bin/gsql"),
                ("gs_ctl", Path(str(database["gausshome"])) / "bin/gs_ctl"),
            ):
                path = Path(str(database.get(name, default)))
                if not path.is_file():
                    raise RuntimeError(f"openGauss tool is missing: {path}")
            for cpu_mask in (str(storage["tp_cpus"]), str(storage["ap_cpus"])):
                probe = subprocess.run(
                    ["taskset", "-c", cpu_mask, "true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if probe.returncode != 0:
                    raise RuntimeError(f"invalid or unavailable CPU mask: {cpu_mask}")
            prediction_enabled = parse_bool(
                self.config.get("prediction", {}).get("enabled", True)
            )
            source = self.config.get("candidate_source", {})
            if prediction_enabled and not source.get("path"):
                raise RuntimeError(
                    "candidate_source.path is required unless prediction.enabled=false"
                )
            source_command = list(source.get("command", []))
            if prediction_enabled and source_command:
                producer = str(source_command[0])
                if shutil.which(producer) is None and not Path(producer).is_file():
                    raise RuntimeError(f"candidate source executable is missing: {producer}")
            self.inventory()

    def prepare_files(self) -> None:
        storage = self.config["storage_probe"]
        file_dir = Path(str(storage["file_dir"])).resolve()
        marker = file_dir / "test_file.0"
        with self.stage("prepare_files", [marker]) as execute:
            if not execute:
                return
            file_dir.mkdir(parents=True, exist_ok=True)
            existing = [file_dir / f"test_file.{index}" for index in range(int(storage.get("file_num", 8)))]
            if marker.exists():
                missing = [str(path) for path in existing if not path.exists()]
                if missing:
                    raise RuntimeError(
                        "partial sysbench file set exists; remove or repair it explicitly: " + ", ".join(missing)
                    )
                return
            self.run([
                "sysbench", "fileio",
                f"--file-num={int(storage.get('file_num', 8))}",
                f"--file-total-size={storage.get('file_total_size', '4G')}",
                "prepare",
            ], cwd=file_dir)

    def calibrate_storage(self) -> None:
        train_csv = self.surface_root / "train" / "mixed_storage_surface.csv"
        holdout_csv = self.surface_root / "holdout" / "mixed_storage_surface.csv"
        report = self.surface_root / "holdout" / "evaluation" / "mixed_surface_holdout_report.json"
        with self.stage("calibrate_storage", [self.surface_path, report]) as execute:
            if not execute:
                return
            storage = self.config["storage_probe"]
            was_running = self.database_running()
            try:
                if was_running:
                    self.ensure_database(False)
                if parse_bool(storage.get("drop_caches", True)):
                    subprocess.run(["sync"], check=True)
                    if os.geteuid() == 0:
                        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="ascii")
                common = [
                    "--file-dir", str(Path(str(storage["file_dir"])).resolve()),
                    "--device", str(self.config["device"]),
                    "--seconds", str(int(storage.get("seconds", 15))),
                    "--repeats", str(int(storage.get("repeats", 2))),
                    "--tp-cpus", str(storage["tp_cpus"]),
                    "--ap-cpus", str(storage["ap_cpus"]),
                    "--file-num", str(int(storage.get("file_num", 8))),
                    "--file-total-size", str(storage.get("file_total_size", "4G")),
                    "--tp-threads", str(int(self.config["tp_anchor"]["terminals"])),
                ]
                repeats = int(storage.get("repeats", 2))
                if csv_row_count(train_csv) != 6 * repeats:
                    self.run([
                        sys.executable, str(ROOT / "bin/run_mixed_storage_surface.py"),
                        "--out-dir", str(self.surface_root / "train"), "--split", "train", *common,
                    ])
                if not self.surface_path.exists():
                    self.run([
                        sys.executable, str(ROOT / "bin/mixed_storage_surface_formula.py"), "freeze",
                        "--training-csv", str(train_csv), "--out", str(self.surface_path),
                    ])
                else:
                    frozen = read_json(self.surface_path)
                    if frozen.get("training_sha256") != hash_file(train_csv):
                        raise RuntimeError("existing frozen surface does not match the current training CSV")
                if csv_row_count(holdout_csv) != 3 * repeats:
                    self.run([
                        sys.executable, str(ROOT / "bin/run_mixed_storage_surface.py"),
                        "--out-dir", str(self.surface_root / "holdout"), "--split", "holdout", *common,
                    ])
                if not report.exists():
                    self.run([
                        sys.executable, str(ROOT / "bin/mixed_storage_surface_formula.py"), "evaluate",
                        "--frozen", str(self.surface_path), "--holdout-csv", str(holdout_csv),
                        "--out-dir", str(self.surface_root / "holdout/evaluation"),
                    ])
                result = read_json(report)
                if not result["acceptance"]["passed"]:
                    raise RuntimeError(f"storage surface holdout failed: {result}")
            finally:
                if was_running:
                    self.ensure_database(True)

    def calibrate_path(self) -> None:
        with self.stage("calibrate_path", [self.anchors_path]) as execute:
            if not execute:
                return
            self.ensure_database(True)
            anchor = self.config["tp_anchor"]
            depths = ",".join(str(value) for value in anchor.get("anchor_depths", [6, 12, 24]))
            self.run([
                sys.executable, str(ROOT / "bin/portable_tp_path_probe.py"),
                "--config", str(self.config_path), "--mode", "anchor",
                "--depths", depths, "--repeats", str(int(anchor.get("anchor_repeats", 2))),
                "--out-dir", str(self.anchor_root),
            ])

    def build_model(self) -> None:
        with self.stage("build_model", [self.model_path]) as execute:
            if not execute:
                return
            self.run([
                sys.executable, str(ROOT / "bin/portable_joint_model.py"), "build",
                "--surface", str(self.surface_path), "--anchors", str(self.anchors_path),
                "--inventory", str(self.inventory_path), "--out", str(self.model_path),
            ])

    def validate_model(self) -> None:
        report = self.holdout_root / "holdout_report.json"
        with self.stage("validate_model", [report]) as execute:
            if not execute:
                return
            self.ensure_database(True)
            anchor = self.config["tp_anchor"]
            depths = ",".join(str(value) for value in anchor.get("holdout_depths", [9, 18]))
            self.run([
                sys.executable, str(ROOT / "bin/portable_tp_path_probe.py"),
                "--config", str(self.config_path), "--mode", "holdout",
                "--depths", depths, "--repeats", str(int(anchor.get("holdout_repeats", 2))),
                "--out-dir", str(self.holdout_root), "--model", str(self.model_path),
            ])
            result = read_json(report)
            if not result["acceptance"]["passed"]:
                raise RuntimeError(f"portable model holdout failed: {result}")

    def candidate_path(self) -> Path | None:
        source = self.config.get("candidate_source", {})
        path_value = source.get("path")
        if not path_value:
            return None
        path = Path(str(path_value)).resolve()
        command_template = source.get("command")
        if command_template and not path.exists():
            values = {
                "output": str(path),
                "workspace": str(self.workspace),
                "model": str(self.model_path),
                "config": str(self.config_path),
            }
            command = [str(item).format(**values) for item in command_template]
            path.parent.mkdir(parents=True, exist_ok=True)
            self.run(command)
        if not path.exists():
            raise RuntimeError(f"candidate source does not exist: {path}")
        return path

    def predict(self) -> None:
        if not parse_bool(self.config.get("prediction", {}).get("enabled", True)):
            print(json.dumps({
                "stage": "predict", "status": "skipped",
                "reason": "prediction.enabled=false",
            }))
            return
        candidates = self.candidate_path()
        if candidates is None:
            raise RuntimeError("candidate_source.path is required for prediction")
        summary_path = self.prediction_root / "prediction_summary.json"
        with self.stage("predict", [summary_path]) as execute:
            if not execute:
                return
            predict_file(
                self.model_path,
                candidates,
                self.prediction_root,
                float(self.config.get("search", {}).get("path_tolerance", 0.03)),
            )

    def run_all(self) -> None:
        self.doctor()
        self.prepare_files()
        self.calibrate_storage()
        self.calibrate_path()
        self.build_model()
        if parse_bool(self.config.get("validation", {}).get("enabled", True)):
            self.validate_model()
        self.predict()


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "action",
        choices=(
            "doctor", "prepare-files", "calibrate-storage", "calibrate-path",
            "build-model", "validate", "predict", "run-all", "status",
        ),
    )
    args = parser.parse_args()
    controller = Controller(args.config)
    actions: dict[str, Callable[[], None]] = {
        "doctor": controller.doctor,
        "prepare-files": controller.prepare_files,
        "calibrate-storage": controller.calibrate_storage,
        "calibrate-path": controller.calibrate_path,
        "build-model": controller.build_model,
        "validate": controller.validate_model,
        "predict": controller.predict,
        "run-all": controller.run_all,
    }
    if args.action == "status":
        print(json.dumps(controller.state, indent=2))
    else:
        actions[args.action]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
