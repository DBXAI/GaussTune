#!/usr/bin/env python3
"""Create/load the fresh-machine 125-warehouse (~20GB) BenchBase TPCC DB."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape


def gsql(args, database, sql):
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.gauss_home / "lib")
    return subprocess.check_output([
        "runuser", "-u", "omm", "--", str(args.gauss_home / "bin" / "gsql"),
        "-X", "-At", "-v", "ON_ERROR_STOP=1", "-p", str(args.port),
        "-d", database, "-c", sql,
    ], text=True, env=environment).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gauss-home", type=Path, default=Path("/opt/openGauss"))
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", default="h7_tpcc_20gb")
    parser.add_argument("--user", default="h7_tp")
    parser.add_argument("--password-env", default="HUAWEI7_TP_PASSWORD")
    parser.add_argument("--benchbase-home", type=Path, required=True)
    parser.add_argument("--jdbc-jar", type=Path, required=True)
    parser.add_argument("--java", default="java")
    parser.add_argument("--warehouses", type=int, default=125)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("TPCC loader must run as root")
    if args.warehouses != 125:
        raise ValueError("PPT reproduction contract fixes TPCC at 125 warehouses")
    if not args.database.replace("_", "").isalnum() or not args.user.replace("_", "").isalnum():
        raise ValueError("database/user must be simple identifiers")
    password = os.environ.get(args.password_env, "")
    if not password:
        raise RuntimeError("required password variable is unset: %s" % args.password_env)
    if gsql(args, "postgres", "SELECT 1 FROM pg_roles WHERE rolname='%s';" % args.user) != "1":
        raise RuntimeError("create login role %s before loading" % args.user)
    if gsql(args, "postgres", "SELECT 1 FROM pg_database WHERE datname='%s';" % args.database):
        raise RuntimeError("refusing to overwrite existing database %s" % args.database)
    gsql(args, "postgres", "CREATE DATABASE %s OWNER %s;" % (args.database, args.user))
    temporary = Path(tempfile.mkdtemp(prefix="huawei7-tpcc-load-"))
    try:
        config = temporary / "tpcc.xml"
        result_dir = temporary / "results"
        result_dir.mkdir()
        xml = """<?xml version="1.0"?>
<parameters><type>POSTGRES</type><driver>org.postgresql.Driver</driver>
<url>jdbc:postgresql://127.0.0.1:%d/%s?sslmode=disable&amp;ApplicationName=tpcc_load</url>
<username>%s</username><password>%s</password>
<reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
<isolation>TRANSACTION_READ_COMMITTED</isolation><batchsize>512</batchsize>
<scalefactor>%d</scalefactor><terminals>16</terminals>
<works><work><time>1</time><rate>unlimited</rate><weights>45,43,4,4,4</weights></work></works>
<transactiontypes><transactiontype><name>NewOrder</name></transactiontype>
<transactiontype><name>Payment</name></transactiontype>
<transactiontype><name>OrderStatus</name></transactiontype>
<transactiontype><name>Delivery</name></transactiontype>
<transactiontype><name>StockLevel</name></transactiontype></transactiontypes>
</parameters>""" % (args.port, args.database, args.user, escape(password), args.warehouses)
        config.write_text(xml, encoding="utf-8")
        config.chmod(0o600)
        classpath = "%s:%s:%s" % (
            args.jdbc_jar, args.benchbase_home / "benchbase.jar",
            args.benchbase_home / "lib/*",
        )
        subprocess.run([
            args.java, "-Xmx4g", "-cp", classpath,
            "com.oltpbenchmark.DBWorkload", "-b", "tpcc", "-c", str(config),
            "--create=true", "--load=true", "--execute=false",
            "-d", str(result_dir),
        ], check=True)
    finally:
        # This private mkdtemp contains the plaintext BenchBase password and
        # no experiment evidence; remove only this resolved, known prefix.
        resolved = temporary.resolve()
        if resolved.parent == Path("/tmp") and resolved.name.startswith("huawei7-tpcc-load-"):
            shutil.rmtree(resolved)
    gsql(args, args.database, "ANALYZE;")
    result = {
        "database": args.database, "warehouses": args.warehouses,
        "database_size_bytes": int(gsql(
            args, "postgres", "SELECT pg_database_size('%s');" % args.database,
        )),
        "warehouse_rows": int(gsql(args, args.database, "SELECT count(*) FROM warehouse;")),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
