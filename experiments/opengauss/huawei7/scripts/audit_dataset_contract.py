#!/usr/bin/env python3
"""Audit either an existing dataset profile or a fresh PPT-sized load."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import sha256


def gsql(gsql_path: Path, library: Path, port: int, database: str, sql: str) -> str:
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(library)
    return subprocess.check_output([
        "runuser", "-u", "omm", "--", str(gsql_path), "-X", "-At",
        "-v", "ON_ERROR_STOP=1", "-p", str(port), "-d", database,
        "-F", "\t", "-c", sql,
    ], text=True, env=environment).strip()


def parse_range(contract: Dict[str, object], section: str) -> Tuple[float, float]:
    datasets = contract["datasets"]
    values = datasets[section]["database_bytes_range_decimal_gb"]  # type: ignore[index]
    return float(values[0]) * 1e9, float(values[1]) * 1e9


def database_name(
    contract: Dict[str, object], section: str, override: str,
) -> str:
    datasets = contract["datasets"]
    configured = str(datasets[section].get("database", ""))  # type: ignore[index]
    legacy_defaults = {
        "ap": "h7_tpch_sf60", "sysbench": "h7_sysbench_20gb",
        "benchbase_tpcc": "h7_tpcc_20gb",
    }
    value = override or configured or legacy_defaults[section]
    if not value or not value.replace("_", "").isalnum():
        raise ValueError("invalid/missing database name for %s" % section)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--machine", type=Path, required=True)
    parser.add_argument("--gsql", type=Path, required=True)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--ap-database", default="")
    parser.add_argument("--sysbench-database", default="")
    parser.add_argument("--tpcc-database", default="")
    parser.add_argument("--query-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    if contract.get("schema") not in (
        "huawei7.reproduction-contract/v1", "huawei7.dataset-contract/v2",
    ):
        raise ValueError("unsupported dataset contract schema")
    machine = json.loads(args.machine.read_text(encoding="utf-8"))
    if (
        machine.get("schema") != "huawei7.machine/v1"
        or not str(machine.get("machine_fingerprint", ""))
    ):
        raise ValueError("dataset audit requires a valid machine artifact")
    databases = {
        "ap": database_name(contract, "ap", args.ap_database),
        "sysbench": database_name(contract, "sysbench", args.sysbench_database),
        "benchbase_tpcc": database_name(
            contract, "benchbase_tpcc", args.tpcc_database,
        ),
    }
    sizes = {}
    database_oids = {}
    for key, database in databases.items():
        sizes[key] = int(gsql(
            args.gsql, args.library_dir, args.port, "postgres",
            "SELECT pg_database_size('%s');" % database.replace("'", "''"),
        ))
        database_oids[key] = int(gsql(
            args.gsql, args.library_dir, args.port, "postgres",
            "SELECT oid FROM pg_database WHERE datname='%s';"
            % database.replace("'", "''"),
        ))
    table_rows = gsql(
        args.gsql, args.library_dir, args.port, databases["ap"],
        "SELECT relname,oid,relfilenode,pg_total_relation_size(oid),reltuples::bigint "
        "FROM pg_class WHERE relkind='r' AND relnamespace=("
        "SELECT oid FROM pg_namespace WHERE nspname='public') ORDER BY 2 DESC;",
    )
    ap_tables = []
    for line in table_rows.splitlines():
        name, oid, relfilenode, size, tuples = line.split("\t")
        ap_tables.append({
            "table": name, "oid": int(oid), "relfilenode": int(relfilenode),
            "bytes": int(size), "estimated_rows": int(tuples),
        })
    if not ap_tables:
        raise RuntimeError("AP database contains no public tables")
    warehouse_count = int(gsql(
        args.gsql, args.library_dir, args.port, databases["benchbase_tpcc"],
        "SELECT count(*) FROM warehouse;",
    ))
    sysbench_rows = gsql(
        args.gsql, args.library_dir, args.port, databases["sysbench"],
        "SELECT count(*),min(reltuples)::bigint,max(reltuples)::bigint "
        "FROM pg_class WHERE relkind='r' AND relname ~ '^sbtest[0-9]+$';",
    ).split("\t")
    sysbench_table_count, sysbench_min_rows, sysbench_max_rows = map(int, sysbench_rows)
    sysbench_columns = gsql(
        args.gsql, args.library_dir, args.port, databases["sysbench"],
        "SELECT table_name,string_agg(column_name,',' ORDER BY ordinal_position) "
        "FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name ~ '^sbtest[0-9]+$' GROUP BY table_name ORDER BY table_name;",
    )
    sysbench_column_rows = dict(
        line.split("\t", 1) for line in sysbench_columns.splitlines() if line
    )
    sysbench_index_rows = gsql(
        args.gsql, args.library_dir, args.port, databases["sysbench"],
        "SELECT tablename,indexname,indexdef FROM pg_indexes "
        "WHERE schemaname='public' AND tablename ~ '^sbtest[0-9]+$' "
        "ORDER BY tablename,indexname;",
    )
    sysbench_indexes: Dict[str, list[str]] = {}
    for line in sysbench_index_rows.splitlines():
        table, _name, definition = line.split("\t", 2)
        sysbench_indexes.setdefault(table, []).append(definition)
    tpcc_table_rows = gsql(
        args.gsql, args.library_dir, args.port, databases["benchbase_tpcc"],
        "SELECT relname FROM pg_class WHERE relkind='r' AND relnamespace=("
        "SELECT oid FROM pg_namespace WHERE nspname='public') ORDER BY relname;",
    ).splitlines()
    tpcc_index_rows = gsql(
        args.gsql, args.library_dir, args.port, databases["benchbase_tpcc"],
        "SELECT tablename,indexname,indexdef FROM pg_indexes "
        "WHERE schemaname='public' ORDER BY tablename,indexname;",
    )
    tpcc_indexes: Dict[str, list[str]] = {}
    for line in tpcc_index_rows.splitlines():
        table, _name, definition = line.split("\t", 2)
        tpcc_indexes.setdefault(table, []).append(definition)
    failures = []
    for section in ("ap", "sysbench", "benchbase_tpcc"):
        section_contract = contract["datasets"][section]
        if "database_bytes_range_decimal_gb" in section_contract:
            lower, upper = parse_range(contract, section)
            if not lower <= sizes[section] <= upper:
                failures.append("%s database size %.3fGB outside [%.1f,%.1f]GB" % (
                    section, sizes[section] / 1e9, lower / 1e9, upper / 1e9,
                ))
    ap_contract = contract["datasets"]["ap"]
    largest = max(ap_tables, key=lambda row: int(row["bytes"]))
    if "largest_table_bytes_range_decimal_gb" in ap_contract:
        largest_range = ap_contract["largest_table_bytes_range_decimal_gb"]
        largest_lower = float(largest_range[0]) * 1e9
        largest_upper = float(largest_range[1]) * 1e9
        if not largest_lower <= largest["bytes"] <= largest_upper:
            failures.append("AP largest table %.3fGB outside [%.1f,%.1f]GB" % (
                largest["bytes"] / 1e9, largest_lower / 1e9, largest_upper / 1e9,
            ))
    required_ap_tables = set(ap_contract.get("required_tables", (
        "region", "nation", "supplier", "customer", "part", "partsupp",
        "orders", "lineitem",
    )))
    actual_ap_tables = {str(row["table"]) for row in ap_tables}
    if not required_ap_tables <= actual_ap_tables:
        failures.append("AP database lacks required TPC-H tables")
    expected_lineitem = 6_000_000 * int(ap_contract["scale_factor"])
    lineitem = next((row for row in ap_tables if row["table"] == "lineitem"), None)
    if lineitem is None or abs(lineitem["estimated_rows"] - expected_lineitem) > expected_lineitem * .01:
        failures.append(
            "lineitem analyzed cardinality is not TPC-H SF%d"
            % int(ap_contract["scale_factor"])
        )
    sysbench_contract = contract["datasets"]["sysbench"]
    if sysbench_table_count != int(sysbench_contract["tables"]):
        failures.append("sysbench table count differs from contract")
    expected_rows = int(sysbench_contract["rows_per_table"])
    if min(sysbench_min_rows, sysbench_max_rows) < expected_rows * .99 or max(
        sysbench_min_rows, sysbench_max_rows
    ) > expected_rows * 1.01:
        failures.append(
            "sysbench ANALYZE row estimates differ from %d/table" % expected_rows
        )
    required_columns = list(sysbench_contract.get(
        "required_columns", ["id", "k", "c", "pad"],
    ))
    if (
        len(sysbench_column_rows) != sysbench_table_count
        or any(value.split(",") != required_columns
               for value in sysbench_column_rows.values())
    ):
        failures.append("sysbench tables do not have the standard id,k,c,pad schema")
    required_sysbench_indexes = [
        str(value) for value in sysbench_contract.get(
            "required_index_columns", ["id", "k"],
        )
    ]
    expected_sysbench_tables = {
        "sbtest%d" % index
        for index in range(1, int(sysbench_contract["tables"]) + 1)
    }
    for table in sorted(expected_sysbench_tables):
        definitions = "\n".join(sysbench_indexes.get(table, ())).lower()
        if any("(%s)" % column.lower() not in definitions
               for column in required_sysbench_indexes):
            failures.append(
                "%s lacks required Sysbench id/k indexes" % table
            )
    expected_warehouses = int(contract["datasets"]["benchbase_tpcc"]["warehouses"])
    if warehouse_count != expected_warehouses:
        failures.append("TPCC warehouse count differs from contract")
    required_tpcc_tables = set(contract["datasets"]["benchbase_tpcc"].get(
        "required_tables", (
            "warehouse", "district", "customer", "history", "oorder",
            "new_order", "order_line", "stock", "item",
        ),
    ))
    if not required_tpcc_tables <= set(tpcc_table_rows):
        failures.append("TPCC database lacks required BenchBase tables")
    default_tpcc_indexes = {
        "warehouse": [["w_id"]],
        "district": [["d_w_id", "d_id"]],
        "customer": [
            ["c_w_id", "c_d_id", "c_id"],
            ["c_w_id", "c_d_id", "c_last", "c_first"],
        ],
        "oorder": [["o_w_id", "o_d_id", "o_id"]],
        "new_order": [["no_w_id", "no_d_id", "no_o_id"]],
        "order_line": [["ol_w_id", "ol_d_id", "ol_o_id", "ol_number"]],
        "stock": [["s_w_id", "s_i_id"]],
        "item": [["i_id"]],
    }
    required_tpcc_indexes = contract["datasets"]["benchbase_tpcc"].get(
        "required_indexes", default_tpcc_indexes,
    )
    if not isinstance(required_tpcc_indexes, dict):
        raise ValueError("TPCC required_indexes must be an object")
    for table, index_columns in required_tpcc_indexes.items():
        definitions = "\n".join(tpcc_indexes.get(str(table), ())).lower()
        if not isinstance(index_columns, list):
            raise ValueError("TPCC required index rows must be lists")
        for columns in index_columns:
            if not isinstance(columns, list) or not columns:
                raise ValueError("TPCC required index columns must be nonempty lists")
            signature = "(%s)" % ", ".join(
                str(column).lower() for column in columns
            )
            if signature not in definitions:
                failures.append(
                    "TPCC table %s lacks required index %s" % (table, signature)
                )
    query_artifacts = []
    required_query_ids = [int(value) for value in ap_contract.get(
        "required_query_ids", [],
    )]
    if required_query_ids and args.query_dir is None:
        failures.append("AP contract requires --query-dir with rendered queries")
    for query_id in required_query_ids if args.query_dir is not None else []:
        path = args.query_dir / ("q%d.sql" % query_id)
        if not path.is_file():
            failures.append("missing rendered AP query Q%d" % query_id)
            continue
        sql = path.read_text(encoding="utf-8").strip().rstrip(";")
        try:
            gsql(
                args.gsql, args.library_dir, args.port, databases["ap"],
                "SET enable_vector_engine=off; SET query_dop=1; EXPLAIN " + sql,
            )
        except subprocess.CalledProcessError:
            failures.append("AP query Q%d cannot be planned on current schema" % query_id)
        query_artifacts.append({
            "query_id": query_id, "path": str(path.resolve()),
            "sha256": sha256(path),
        })
    identity_material = {
        "profile": contract.get("profile", "ppt-exact"),
        "databases": databases, "database_oids": database_oids,
        "database_sizes_bytes": sizes, "ap_tables": ap_tables,
        "sysbench_table_count": sysbench_table_count,
        "sysbench_min_estimated_rows": sysbench_min_rows,
        "sysbench_max_estimated_rows": sysbench_max_rows,
        "sysbench_indexes": sysbench_indexes,
        "tpcc_warehouse_count": warehouse_count,
        "tpcc_indexes": tpcc_indexes,
        "query_sha256": {
            str(row["query_id"]): row["sha256"] for row in query_artifacts
        },
    }
    dataset_fingerprint = hashlib.sha256(json.dumps(
        identity_material, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    result = {
        "schema": "huawei7.dataset-contract-audit/v3",
        "profile": contract.get("profile", "ppt-exact"),
        "machine_fingerprint": machine["machine_fingerprint"],
        "machine_artifact": {
            "path": str(args.machine.resolve()), "sha256": sha256(args.machine),
        },
        "contract_artifact": {
            "path": str(args.contract.resolve()), "sha256": sha256(args.contract),
        },
        "dataset_fingerprint": dataset_fingerprint,
        "databases": databases, "database_oids": database_oids,
        "database_sizes_bytes": sizes, "ap_tables": ap_tables,
        "sysbench_table_count": sysbench_table_count,
        "sysbench_min_estimated_rows": sysbench_min_rows,
        "sysbench_max_estimated_rows": sysbench_max_rows,
        "sysbench_columns": sysbench_column_rows,
        "sysbench_indexes": sysbench_indexes,
        "tpcc_warehouse_count": warehouse_count,
        "tpcc_tables": tpcc_table_rows,
        "tpcc_indexes": tpcc_indexes,
        "query_artifacts": query_artifacts,
        "failures": failures, "valid": not failures,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    if failures:
        raise RuntimeError("dataset contract failed: " + "; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
