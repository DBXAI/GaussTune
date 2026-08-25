"""Audited identities for either fresh or already-loaded experiment data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping

from .provenance import sha256, validate_path_hash_evidence


AUDIT_SCHEMAS = (
    "huawei7.dataset-contract-audit/v2",
    "huawei7.dataset-contract-audit/v3",
)


def read_dataset_audit(
    path: Path, *, machine_fingerprint: str,
) -> Mapping[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("schema") not in AUDIT_SCHEMAS
        or document.get("machine_fingerprint") != machine_fingerprint
        or document.get("valid") is not True
        or document.get("failures") != []
    ):
        raise ValueError("dataset audit is invalid or belongs to another machine")
    validate_path_hash_evidence(document, "dataset_audit")
    if document.get("schema") == "huawei7.dataset-contract-audit/v3":
        fingerprint = str(document.get("dataset_fingerprint", ""))
        databases = document.get("databases")
        if len(fingerprint) != 64 or not isinstance(databases, dict):
            raise ValueError("dataset v3 audit lacks identity fingerprint/databases")
    return document


def dataset_audit_from_runtime(
    config: Mapping[str, object], *, machine_fingerprint: str,
) -> tuple[Mapping[str, object], Path]:
    path = Path(str(config.get("dataset_audit", "")))
    if not path.is_absolute() or not path.is_file():
        raise ValueError("runtime config requires an absolute dataset_audit path")
    return read_dataset_audit(
        path, machine_fingerprint=machine_fingerprint,
    ), path


def tp_dataset_identity(
    audit: Mapping[str, object], audit_path: Path, *, benchmark: str,
    database: str, configured_tables: int = 0, configured_rows: int = 0,
    configured_warehouses: int = 0,
) -> Dict[str, object]:
    if audit.get("schema") != "huawei7.dataset-contract-audit/v3":
        raise ValueError("adaptive TP commands require dataset audit v3")
    databases = audit["databases"]
    if not isinstance(databases, dict):
        raise ValueError("dataset audit databases are invalid")
    section = "sysbench" if benchmark == "sysbench" else "benchbase_tpcc"
    if str(databases.get(section, "")) != database:
        raise ValueError("runtime TP database differs from audited dataset")
    common: Dict[str, object] = {
        "schema": "huawei7.dataset-identity/v1",
        "profile": audit.get("profile"),
        "database": database,
        "database_oid": int(audit["database_oids"][section]),  # type: ignore[index]
        "database_size_bytes": int(audit["database_sizes_bytes"][section]),  # type: ignore[index]
        "dataset_fingerprint": str(audit["dataset_fingerprint"]),
        "audit_artifact": {
            "path": str(audit_path.resolve()), "sha256": sha256(audit_path),
        },
    }
    if benchmark == "sysbench":
        actual_tables = int(audit["sysbench_table_count"])
        minimum = int(audit["sysbench_min_estimated_rows"])
        maximum = int(audit["sysbench_max_estimated_rows"])
        if (
            configured_tables != actual_tables or configured_rows <= 0
            or minimum < configured_rows * .99 or maximum > configured_rows * 1.01
        ):
            raise ValueError("Sysbench runtime shape differs from audited tables")
        common.update({
            "tables": actual_tables, "rows_per_table": configured_rows,
            "minimum_estimated_rows": minimum,
            "maximum_estimated_rows": maximum,
        })
    else:
        actual_warehouses = int(audit["tpcc_warehouse_count"])
        if configured_warehouses != actual_warehouses:
            raise ValueError("BenchBase runtime scale differs from audited warehouses")
        common.update({
            "warehouses": actual_warehouses,
            "transaction_weights": [45, 43, 4, 4, 4],
        })
    return common


def ap_dataset_identity(
    audit: Mapping[str, object], audit_path: Path, *, database: str,
) -> Dict[str, object]:
    if audit.get("schema") != "huawei7.dataset-contract-audit/v3":
        raise ValueError("adaptive AP commands require dataset audit v3")
    databases = audit.get("databases")
    if not isinstance(databases, dict) or databases.get("ap") != database:
        raise ValueError("runtime AP database differs from audited dataset")
    return {
        "schema": "huawei7.dataset-identity/v1",
        "profile": audit.get("profile"), "database": database,
        "database_oid": int(audit["database_oids"]["ap"]),  # type: ignore[index]
        "database_size_bytes": int(
            audit["database_sizes_bytes"]["ap"]  # type: ignore[index]
        ),
        "dataset_fingerprint": str(audit["dataset_fingerprint"]),
        "audit_artifact": {
            "path": str(audit_path.resolve()), "sha256": sha256(audit_path),
        },
    }


def validate_ap_dataset_identity(
    dataset: Mapping[str, object], *, machine_fingerprint: str,
) -> None:
    evidence = dataset.get("audit_artifact")
    if dataset.get("schema") != "huawei7.dataset-identity/v1" or not isinstance(
        evidence, dict,
    ):
        raise ValueError("AP command lacks audited dataset identity")
    path = Path(str(evidence.get("path", "")))
    if not path.is_file() or sha256(path) != evidence.get("sha256"):
        raise ValueError("AP dataset audit is missing or changed")
    audit = read_dataset_audit(path, machine_fingerprint=machine_fingerprint)
    if (
        dataset.get("database") != audit["databases"]["ap"]  # type: ignore[index]
        or dataset.get("dataset_fingerprint") != audit.get("dataset_fingerprint")
        or int(dataset.get("database_oid", -1))
        != int(audit["database_oids"]["ap"])  # type: ignore[index]
    ):
        raise ValueError("AP dataset identity differs from its audit")


def validate_tp_dataset_identity(
    dataset: Mapping[str, object], *, machine_fingerprint: str,
    benchmark: str,
) -> None:
    if dataset.get("schema") != "huawei7.dataset-identity/v1":
        raise ValueError("TP command lacks audited dataset identity")
    evidence = dataset.get("audit_artifact")
    if not isinstance(evidence, dict):
        raise ValueError("TP dataset identity lacks audit artifact")
    path = Path(str(evidence.get("path", "")))
    if not path.is_file() or sha256(path) != evidence.get("sha256"):
        raise ValueError("TP dataset audit is missing or changed")
    audit = read_dataset_audit(path, machine_fingerprint=machine_fingerprint)
    databases = audit["databases"]
    section = "sysbench" if benchmark == "sysbench" else "benchbase_tpcc"
    if (
        dataset.get("dataset_fingerprint") != audit.get("dataset_fingerprint")
        or dataset.get("database") != databases[section]  # type: ignore[index]
        or int(dataset.get("database_oid", -1))
        != int(audit["database_oids"][section])  # type: ignore[index]
    ):
        raise ValueError("TP dataset identity differs from its audit")
    if benchmark == "sysbench":
        valid = (
            int(dataset.get("tables", -1)) == int(audit["sysbench_table_count"])
            and int(dataset.get("minimum_estimated_rows", -1))
            == int(audit["sysbench_min_estimated_rows"])
            and int(dataset.get("maximum_estimated_rows", -1))
            == int(audit["sysbench_max_estimated_rows"])
            and int(dataset.get("rows_per_table", 0)) > 0
        )
    else:
        valid = (
            int(dataset.get("warehouses", -1))
            == int(audit["tpcc_warehouse_count"])
            and list(dataset.get("transaction_weights", [])) == [45, 43, 4, 4, 4]
        )
    if not valid:
        raise ValueError("TP dataset shape differs from its audited database")
