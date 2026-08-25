#!/usr/bin/env python3
"""Replace one dedicated BenchBase TPCC dataset with a seeded baseline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Mapping
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.dataset import dataset_audit_from_runtime
from huawei7.provenance import sha256
from huawei7.stage_execution import tp_connection


TABLES = (
    "history", "new_order", "order_line", "oorder", "customer",
    "district", "stock", "item", "warehouse",
)


def _identifier(value: str, label: str) -> str:
    if not value or not value.replace("_", "").isalnum():
        raise ValueError("%s must be a simple identifier" % label)
    return value


def _gsql(
    gauss_home: Path, port: int, database: str, sql: str,
) -> str:
    environment = dict(os.environ)
    environment["GAUSSHOME"] = str(gauss_home)
    environment["LD_LIBRARY_PATH"] = str(gauss_home / "lib")
    return subprocess.check_output([
        "runuser", "-u", "omm", "--", str(gauss_home / "bin" / "gsql"),
        "-X", "-At", "-v", "ON_ERROR_STOP=1", "-p", str(port),
        "-d", database, "-c", sql,
    ], text=True, env=environment).strip()


def _one_row(text: str, expected_fields: int) -> list[str]:
    rows = [row for row in text.splitlines() if row and not row.startswith("(")]
    if len(rows) != 1:
        raise RuntimeError("expected one gsql result row")
    values = rows[0].split("|")
    if len(values) != expected_fields:
        raise RuntimeError("gsql result has an unexpected shape")
    return values


def _load_xml(
    runtime: Mapping[str, object], *, password: str, seed: int,
) -> str:
    postgres = runtime["postgres"]
    tp_root = runtime["tp"]
    if not isinstance(postgres, dict) or not isinstance(tp_root, dict):
        raise ValueError("invalid runtime config")
    tp = tp_root["benchbase-tpcc"]
    if not isinstance(tp, dict):
        raise ValueError("invalid BenchBase runtime config")
    connection = tp_connection(runtime, "benchbase-tpcc")
    host = str(postgres.get("host", "127.0.0.1"))
    port = int(postgres.get("port", 5432))
    url = (
        "jdbc:postgresql://%s:%d/%s?sslmode=disable"
        "&amp;ApplicationName=tpcc_seeded_reset"
    ) % (host, port, connection["database"])
    return """<?xml version="1.0"?>
<parameters>
  <type>POSTGRES</type><driver>org.postgresql.Driver</driver>
  <url>%s</url><username>%s</username><password>%s</password>
  <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
  <isolation>TRANSACTION_READ_COMMITTED</isolation>
  <batchsize>%d</batchsize><randomSeed>%d</randomSeed>
  <scalefactor>%d</scalefactor><terminals>16</terminals>
  <works><work><time>1</time><rate>unlimited</rate>
  <weights>45,43,4,4,4</weights></work></works>
  <transactiontypes>
    <transactiontype><name>NewOrder</name></transactiontype>
    <transactiontype><name>Payment</name></transactiontype>
    <transactiontype><name>OrderStatus</name></transactiontype>
    <transactiontype><name>Delivery</name></transactiontype>
    <transactiontype><name>StockLevel</name></transactiontype>
  </transactiontypes>
</parameters>
""" % (
        url, escape(str(connection["user"])), escape(password),
        int(tp.get("batch_size", 128)), seed, int(tp["warehouses"]),
    )


def _available_bytes(path: Path) -> int:
    stat = os.statvfs(path)
    return int(stat.f_bavail * stat.f_frsize)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--gauss-home", type=Path, default=Path("/opt/openGauss"))
    parser.add_argument("--owner-role", default="h7v_tpcc")
    parser.add_argument("--random-seed", type=int, default=15721)
    parser.add_argument(
        "--minimum-free-bytes", type=int, default=20 * 1024 ** 3,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--confirm-replace-tables", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("TPCC reset must run as root")
    if not args.confirm_replace_tables:
        parser.error("the dedicated TPCC tables require explicit replacement confirmation")
    if args.random_seed < 0 or args.minimum_free_bytes <= 0:
        parser.error("seed and free-space threshold must be positive")

    runtime = json.loads(args.runtime_config.read_text(encoding="utf-8"))
    if not isinstance(runtime, dict):
        raise ValueError("runtime config root must be an object")
    machine = str(runtime["machine_fingerprint"])
    dataset, dataset_path = dataset_audit_from_runtime(
        runtime, machine_fingerprint=machine,
    )
    connection = tp_connection(runtime, "benchbase-tpcc")
    database = _identifier(connection["database"], "database")
    user = _identifier(connection["user"], "user")
    owner_role = _identifier(args.owner_role, "owner role")
    postgres = runtime["postgres"]
    tp_root = runtime["tp"]
    if not isinstance(postgres, dict) or not isinstance(tp_root, dict):
        raise ValueError("invalid runtime config")
    tp = tp_root["benchbase-tpcc"]
    if not isinstance(tp, dict):
        raise ValueError("invalid BenchBase runtime config")
    warehouses = int(tp["warehouses"])
    if warehouses <= 0:
        raise ValueError("warehouse count must be positive")
    if dataset.get("databases", {}).get("benchbase_tpcc") != database:
        raise RuntimeError("runtime TPCC database differs from the dataset audit")
    expected_oid = int(dataset["database_oids"]["benchbase_tpcc"])
    port = int(postgres.get("port", 5432))
    identity = _one_row(_gsql(
        args.gauss_home, port, "postgres",
        "SELECT oid,datname FROM pg_database WHERE datname='%s';" % database,
    ), 2)
    if int(identity[0]) != expected_oid or identity[1] != database:
        raise RuntimeError("TPCC database identity differs from the audited OID")
    if _gsql(
        args.gauss_home, port, "postgres",
        "SELECT count(*) FROM pg_roles WHERE rolname IN ('%s','%s');"
        % (user, owner_role),
    ) != "2":
        raise RuntimeError("TPCC runtime or owner role is missing")
    password = os.environ.get(connection["password_env"], "")
    if not password or "\x00" in password or "\n" in password:
        raise RuntimeError("TPCC password environment variable is unset or invalid")

    before_size = int(_gsql(
        args.gauss_home, port, "postgres",
        "SELECT pg_database_size('%s');" % database,
    ))
    drop_sql = "DROP TABLE IF EXISTS %s CASCADE;" % \
        ",".join(TABLES)
    _gsql(args.gauss_home, port, database, drop_sql)
    _gsql(
        args.gauss_home, port, database,
        "GRANT USAGE,CREATE ON SCHEMA public TO %s;" % user,
    )

    scratch = Path(tempfile.mkdtemp(
        prefix="huawei7-tpcc-reset-", dir="/dev/shm",
    ))
    os.chmod(scratch, 0o700)
    try:
        xml_path = scratch / "tpcc.xml"
        result_dir = scratch / "results"
        result_dir.mkdir(mode=0o700)
        xml_path.write_text(
            _load_xml(runtime, password=password, seed=args.random_seed),
            encoding="utf-8",
        )
        os.chmod(xml_path, 0o600)
        home = Path(str(tp["home"]))
        classpath = "%s:%s:%s" % (
            tp["jdbc_jar"], home / "benchbase.jar", home / "lib/*",
        )
        command = [
            str(tp.get("java", "java")), "-Xmx4g", "-cp", classpath,
            "com.oltpbenchmark.DBWorkload", "-b", "tpcc", "-c",
            str(xml_path), "--create=true", "--load=true",
            "--execute=false", "-d", str(result_dir),
        ]
        completed = subprocess.run(
            command, cwd=home, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        # BenchBase does not normally print the password, but keep the loader
        # log secret-safe even if a future version echoes its XML settings.
        print(completed.stdout.replace(password, "REDACTED"), end="")
        completed.check_returncode()
    finally:
        resolved = scratch.resolve()
        if (
            resolved.parent == Path("/dev/shm")
            and resolved.name.startswith("huawei7-tpcc-reset-")
        ):
            shutil.rmtree(resolved)

    ownership_sql = " ".join(
        "ALTER TABLE %s OWNER TO %s;" % (table, owner_role)
        for table in TABLES
    )
    _gsql(args.gauss_home, port, database, ownership_sql)
    _gsql(
        args.gauss_home, port, database,
        (
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public "
            "TO %s; "
            "GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public "
            "TO %s; "
            "REVOKE CREATE ON SCHEMA public FROM %s; ANALYZE;"
        ) % (user, user, user),
    )
    counts_sql = "SELECT " + ",".join(
        "(SELECT count(*) FROM %s)" % table for table in TABLES
    ) + ";"
    counts_values = _one_row(
        _gsql(args.gauss_home, port, database, counts_sql), len(TABLES),
    )
    counts: Dict[str, int] = {
        table: int(value) for table, value in zip(TABLES, counts_values)
    }
    district_state = _one_row(_gsql(
        args.gauss_home, port, database,
        "SELECT min(d_next_o_id),max(d_next_o_id),count(*) FROM district;",
    ), 3)
    expected_counts = {
        "warehouse": warehouses,
        "district": warehouses * 10,
        "customer": warehouses * 10 * 3000,
        "history": warehouses * 10 * 3000,
        "oorder": warehouses * 10 * 3000,
        "new_order": warehouses * 10 * 900,
        "stock": warehouses * 100000,
        "item": 100000,
    }
    counts_valid = all(
        counts[name] == expected for name, expected in expected_counts.items()
    )
    district_valid = (
        int(district_state[0]) == 3001
        and int(district_state[1]) == 3001
        and int(district_state[2]) == warehouses * 10
    )
    available = _available_bytes(args.gauss_home / "data")
    after_size = int(_gsql(
        args.gauss_home, port, "postgres",
        "SELECT pg_database_size('%s');" % database,
    ))
    valid = (
        counts_valid and district_valid
        and counts["order_line"] > warehouses * 10 * 3000 * 5
        and available >= args.minimum_free_bytes
    )
    report = {
        "schema": "huawei7.tpcc-dataset-reset/v1",
        "machine_fingerprint": machine,
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "database": database,
        "database_oid": expected_oid,
        "warehouses": warehouses,
        "random_seed": args.random_seed,
        "transaction_weights": [45, 43, 4, 4, 4],
        "database_size_before_bytes": before_size,
        "database_size_after_bytes": after_size,
        "table_row_counts": counts,
        "expected_exact_row_counts": expected_counts,
        "district_next_order_id": {
            "minimum": int(district_state[0]),
            "maximum": int(district_state[1]),
        },
        "minimum_free_bytes": args.minimum_free_bytes,
        "available_bytes_after_reset": available,
        "runtime_config": {
            "path": str(args.runtime_config.resolve()),
            "sha256": sha256(args.runtime_config),
        },
        "dataset_audit": {
            "path": str(dataset_path.resolve()),
            "sha256": sha256(dataset_path),
        },
        "connection_transport": "password-authenticated-dedicated-role",
        "valid": valid,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
