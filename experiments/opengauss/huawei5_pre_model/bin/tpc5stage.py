#!/usr/bin/env python3
"""Run a reproducible TPC-C + TPC-H five-stage mixed workload on local openGauss.

This is for internal workload validation, not an audited TPC result.
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("HUAWEI5_TPC5_ROOT", PACKAGE_ROOT))
CONF = ROOT / "generated"
RESULTS = ROOT / "results"
BENCHBASE = Path(os.environ.get("BENCHBASE_POSTGRES_HOME", "/opt/benchbase/target/benchbase-postgres/benchbase-postgres"))
OG_JDBC = Path(os.environ.get("OPENGAUSS_JDBC_JAR", "/root/.m2/repository/org/opengauss/opengauss-jdbc/5.1.0/opengauss-jdbc-5.1.0.jar"))
GSQL = os.environ.get("OPENGAUSS_GSQL", "/opt/openGauss/bin/gsql")
LD_LIBRARY_PATH = os.environ.get("OPENGAUSS_LIB", "/opt/openGauss/lib")
PORT = int(os.environ.get("OPENGAUSS_PORT", "5432"))

TP_USER = "h5_tpuser"
AP_USER = "h5_apuser"
TP_PASS = "Huawei5Tp2026"
AP_PASS = "Huawei5Ap2026"
TPCC_DB = "h5_tpcc"
TPCH_DB = "h5_tpch"
LAST_CPU: tuple[int, int] | None = None

TPCC_WEIGHTS = "45,43,4,4,4"
TPCH_ALL_WEIGHTS = ",".join(["1"] * 22)
TPCH_HEAVY_WEIGHTS = "1,0,1,0,1,0,1,0,1,0,0,0,1,0,0,0,0,1,0,0,1,0"
TPCH_HEAVY_QUERY_IDS = [1, 3, 5, 7, 9, 13, 18, 21]
DEFAULT_STABLE_TP_HIGH_RATE = "180"


@dataclass(frozen=True)
class ProcSpec:
    name: str
    proc: subprocess.Popen
    log: Path


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True)


def gsql(sql: str, db: str = "postgres") -> None:
    cmd = (
        f"LD_LIBRARY_PATH={LD_LIBRARY_PATH} {GSQL} "
        f"-p {PORT} -d {db} -v ON_ERROR_STOP=1 -X -q"
    )
    subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=sql,
        text=True,
        check=True,
    )


def gsql_output(sql: str, db: str = "postgres") -> str:
    cmd = (
        f"LD_LIBRARY_PATH={LD_LIBRARY_PATH} {GSQL} "
        f"-p {PORT} -d {db} -v ON_ERROR_STOP=1 -X -q -At"
    )
    result = subprocess.run(
        ["su", "-", "omm", "-c", cmd],
        input=sql,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def database_exists(name: str) -> bool:
    out = gsql_output(f"SELECT 1 FROM pg_database WHERE datname = '{name}';\n")
    return out == "1"


def benchbase_cmd(bench: str, config: Path, *, create: bool, load: bool, execute: bool) -> list[str]:
    cp = f"{OG_JDBC}:{BENCHBASE / 'benchbase.jar'}:{BENCHBASE / 'lib/*'}"
    return [
        "java",
        "-Xmx2g",
        "-cp",
        cp,
        "com.oltpbenchmark.DBWorkload",
        "-b",
        bench,
        "-c",
        str(config),
        f"--create={str(create).lower()}",
        f"--load={str(load).lower()}",
        f"--execute={str(execute).lower()}",
        "-d",
        str(RESULTS),
        "--sample=1",
        "--interval-monitor=1000",
        "--monitor-type=throughput",
    ]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tpcc_xml(seed: int, warehouses: int, terminals: int, rate: str, seconds: int) -> str:
    return f"""<?xml version="1.0"?>
<parameters>
  <type>POSTGRES</type>
  <driver>org.postgresql.Driver</driver>
  <url>jdbc:postgresql://127.0.0.1:{PORT}/{TPCC_DB}?ApplicationName=tpcc&amp;batchMode=on</url>
  <username>{TP_USER}</username>
  <password>{TP_PASS}</password>
  <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
  <randomSeed>{seed}</randomSeed>
  <isolation>TRANSACTION_READ_COMMITTED</isolation>
  <batchsize>512</batchsize>
  <scalefactor>{warehouses}</scalefactor>
  <terminals>{terminals}</terminals>
  <works>
    <work>
      <time>{seconds}</time>
      <warmup>0</warmup>
      <rate>{rate}</rate>
      <weights>{TPCC_WEIGHTS}</weights>
    </work>
  </works>
  <transactiontypes>
    <transactiontype><name>NewOrder</name></transactiontype>
    <transactiontype><name>Payment</name></transactiontype>
    <transactiontype><name>OrderStatus</name></transactiontype>
    <transactiontype><name>Delivery</name></transactiontype>
    <transactiontype><name>StockLevel</name></transactiontype>
  </transactiontypes>
</parameters>
"""


def tpch_xml(
    seed: int,
    scale: float,
    terminals: int,
    rate: str,
    seconds: int,
    setup: Path,
    *,
    serial: bool = False,
    weights: str = TPCH_HEAVY_WEIGHTS,
) -> str:
    serial_text = "true" if serial else "false"
    return f"""<?xml version="1.0"?>
<parameters>
  <type>POSTGRES</type>
  <driver>org.postgresql.Driver</driver>
  <url>jdbc:postgresql://127.0.0.1:{PORT}/{TPCH_DB}?ApplicationName=tpch&amp;batchMode=on</url>
  <username>{AP_USER}</username>
  <password>{AP_PASS}</password>
  <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
  <randomSeed>{seed}</randomSeed>
  <sessionsetupfile>{setup}</sessionsetupfile>
  <isolation>TRANSACTION_READ_COMMITTED</isolation>
  <batchsize>1024</batchsize>
  <scalefactor>{scale}</scalefactor>
  <terminals>{terminals}</terminals>
  <works>
    <work>
      <time>{seconds}</time>
      <warmup>0</warmup>
      <serial>{serial_text}</serial>
      <rate>{rate}</rate>
      <weights>{weights}</weights>
    </work>
  </works>
  <transactiontypes>
    <transactiontype><name>Q1</name><id>1</id></transactiontype>
    <transactiontype><name>Q2</name><id>2</id></transactiontype>
    <transactiontype><name>Q3</name><id>3</id></transactiontype>
    <transactiontype><name>Q4</name><id>4</id></transactiontype>
    <transactiontype><name>Q5</name><id>5</id></transactiontype>
    <transactiontype><name>Q6</name><id>6</id></transactiontype>
    <transactiontype><name>Q7</name><id>7</id></transactiontype>
    <transactiontype><name>Q8</name><id>8</id></transactiontype>
    <transactiontype><name>Q9</name><id>9</id></transactiontype>
    <transactiontype><name>Q10</name><id>10</id></transactiontype>
    <transactiontype><name>Q11</name><id>11</id></transactiontype>
    <transactiontype><name>Q12</name><id>12</id></transactiontype>
    <transactiontype><name>Q13</name><id>13</id></transactiontype>
    <transactiontype><name>Q14</name><id>14</id></transactiontype>
    <transactiontype><name>Q15</name><id>15</id></transactiontype>
    <transactiontype><name>Q16</name><id>16</id></transactiontype>
    <transactiontype><name>Q17</name><id>17</id></transactiontype>
    <transactiontype><name>Q18</name><id>18</id></transactiontype>
    <transactiontype><name>Q19</name><id>19</id></transactiontype>
    <transactiontype><name>Q20</name><id>20</id></transactiontype>
    <transactiontype><name>Q21</name><id>21</id></transactiontype>
    <transactiontype><name>Q22</name><id>22</id></transactiontype>
  </transactiontypes>
</parameters>
"""


def parse_tpch_query_cycle(value: str) -> list[int]:
    query_ids: list[int] = []
    for raw in value.split(","):
        raw = raw.strip().upper()
        if raw.startswith("Q"):
            raw = raw[1:]
        if not raw:
            continue
        query_id = int(raw)
        if query_id < 1 or query_id > 22:
            raise ValueError(f"TPC-H query id must be in [1, 22]: {query_id}")
        query_ids.append(query_id)
    if not query_ids:
        raise ValueError("TPC-H query cycle cannot be empty")
    return query_ids


def tpch_single_query_weights(query_id: int) -> str:
    weights = ["0"] * 22
    weights[query_id - 1] = "1"
    return ",".join(weights)


def stable_workload_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "stable_workload", False))


def fixed_ap_clients_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "ap_fixed_query_clients", False) or stable_workload_enabled(args))


def effective_tp_high_rate(args: argparse.Namespace) -> str:
    rate = str(args.tp_high_rate)
    if stable_workload_enabled(args) and rate == "unlimited":
        return str(getattr(args, "stable_tp_high_rate", DEFAULT_STABLE_TP_HIGH_RATE))
    return rate


def ap_group_configs(
    args: argparse.Namespace,
    key: str,
    terminals: int,
    setup: Path,
    stage_index: int,
) -> Path | list[Path]:
    ap_rate = str(getattr(args, "ap_rate", "unlimited"))
    ap_serial = bool(getattr(args, "ap_serial", False) or stable_workload_enabled(args))
    if not fixed_ap_clients_enabled(args):
        path = CONF / f"tpch_{key}.xml"
        write(
            path,
            tpch_xml(
                args.seed,
                args.tpch_scale,
                terminals,
                ap_rate,
                args.total_seconds,
                setup,
                serial=ap_serial,
                weights=TPCH_HEAVY_WEIGHTS,
            ),
        )
        return path

    query_cycle = parse_tpch_query_cycle(getattr(args, "ap_query_cycle", ",".join(map(str, TPCH_HEAVY_QUERY_IDS))))
    paths: list[Path] = []
    offset = sum(getattr(args, name) for name in ("ap_s1", "ap_s2", "ap_s3", "ap_s4", "ap_s5")[:stage_index])
    for idx in range(terminals):
        query_id = query_cycle[(offset + idx) % len(query_cycle)]
        path = CONF / f"tpch_{key}_client{idx + 1:02d}_q{query_id}.xml"
        write(
            path,
            tpch_xml(
                args.seed,
                args.tpch_scale,
                1,
                ap_rate,
                args.total_seconds,
                setup,
                serial=ap_serial,
                weights=tpch_single_query_weights(query_id),
            ),
        )
        paths.append(path)
    return paths


def render_configs(args: argparse.Namespace) -> dict[str, Path | list[Path]]:
    CONF.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    ap_setup = CONF / "tpch_ap_session.sql"
    ap_setup_lines = [
        "SET application_name = 'tpch_ap';",
        f"SET work_mem = '{args.ap_work_mem}';",
    ]
    if args.ap_temp_file_limit:
        ap_setup_lines.append(f"SET temp_file_limit = '{args.ap_temp_file_limit}';")
    ap_setup_lines.extend(
        [
            "SET enable_hashjoin = on;",
            "SET enable_mergejoin = on;",
            "SET enable_nestloop = on;",
        ]
    )
    write(ap_setup, "\n".join(ap_setup_lines) + "\n")

    paths: dict[str, Path | list[Path]] = {
        "tpcc_load": CONF / "tpcc_load.xml",
        "tpch_load": CONF / "tpch_load.xml",
        "tpcc_low": CONF / "tpcc_low.xml",
        "tpcc_high": CONF / "tpcc_high.xml",
    }

    write(paths["tpcc_load"], tpcc_xml(args.seed, args.tpcc_warehouses, 1, "1", 1))
    write(paths["tpch_load"], tpch_xml(args.seed, args.tpch_scale, 1, "1", 1, ap_setup))
    write(paths["tpcc_low"], tpcc_xml(args.seed, args.tpcc_warehouses, args.tp_low_terminals, str(args.tp_low_rate), args.total_seconds))
    write(paths["tpcc_high"], tpcc_xml(args.seed, args.tpcc_warehouses, args.tp_high_terminals, effective_tp_high_rate(args), args.stage_seconds + 20))
    for stage_index, (key, terms) in enumerate([
        ("ap_s1", args.ap_s1),
        ("ap_s2", args.ap_s2),
        ("ap_s3", args.ap_s3),
        ("ap_s4", args.ap_s4),
        ("ap_s5", args.ap_s5),
    ]):
        paths[key] = ap_group_configs(args, key, terms, ap_setup, stage_index)
    return paths


def prepare(args: argparse.Namespace) -> None:
    paths = render_configs(args)
    if not OG_JDBC.exists():
        raise SystemExit(f"missing openGauss JDBC: {OG_JDBC}")

    reset_sql = ""
    if args.reset:
        reset_sql = f"""
DROP DATABASE IF EXISTS {TPCC_DB};
DROP DATABASE IF EXISTS {TPCH_DB};
"""

    gsql(
        f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TP_USER}') THEN
    CREATE ROLE {TP_USER} LOGIN PASSWORD '{TP_PASS}';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{AP_USER}') THEN
    CREATE ROLE {AP_USER} LOGIN PASSWORD '{AP_PASS}';
  END IF;
END $$;
{reset_sql}
""",
    )
    if not database_exists(TPCC_DB):
        gsql(f"CREATE DATABASE {TPCC_DB} OWNER {TP_USER};\n")
    if not database_exists(TPCH_DB):
        gsql(f"CREATE DATABASE {TPCH_DB} OWNER {AP_USER};\n")
    gsql(
        f"""
ALTER SCHEMA public OWNER TO {TP_USER};
GRANT ALL ON SCHEMA public TO {TP_USER};
""",
        db=TPCC_DB,
    )
    gsql(
        f"""
ALTER SCHEMA public OWNER TO {AP_USER};
GRANT ALL ON SCHEMA public TO {AP_USER};
""",
        db=TPCH_DB,
    )

    load_tpcc = args.load or args.load_tpcc
    load_tpch = args.load or args.load_tpch
    if load_tpcc:
        run(benchbase_cmd("tpcc", paths["tpcc_load"], create=True, load=True, execute=False), cwd=BENCHBASE)
    if load_tpch:
        run(benchbase_cmd("tpch", paths["tpch_load"], create=True, load=True, execute=False), cwd=BENCHBASE)
    if load_tpcc:
        gsql("ANALYZE;\n", db=TPCC_DB)
    if load_tpch:
        gsql("ANALYZE;\n", db=TPCH_DB)


def start(name: str, cmd: list[str], log: Path) -> ProcSpec:
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = log.open("w", encoding="utf-8")
    print(f"[{time.strftime('%F %T')}] start {name}: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, cwd=BENCHBASE, stdout=fh, stderr=subprocess.STDOUT, text=True)
    return ProcSpec(name=name, proc=proc, log=log)


def config_paths(config: Path | list[Path]) -> list[Path]:
    return config if isinstance(config, list) else [config]


def start_configs(name: str, bench: str, configs: Path | list[Path], log_dir: Path) -> list[ProcSpec]:
    config_list = config_paths(configs)
    specs: list[ProcSpec] = []
    for idx, config in enumerate(config_list):
        suffix = "" if len(config_list) == 1 else f"_{idx + 1:02d}"
        specs.append(
            start(
                f"{name}{suffix}",
                benchbase_cmd(bench, config, create=False, load=False, execute=True),
                log_dir / f"{name}{suffix}.log",
            )
        )
    return specs


def stop(spec: ProcSpec) -> None:
    if spec.proc.poll() is not None:
        return
    print(f"[{time.strftime('%F %T')}] stop {spec.name}", flush=True)
    spec.proc.send_signal(signal.SIGTERM)
    try:
        spec.proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        spec.proc.kill()
        spec.proc.wait(timeout=10)


def cpu_percent() -> str:
    global LAST_CPU
    with open("/proc/stat", "r", encoding="utf-8") as fh:
        parts = fh.readline().split()
    values = [int(v) for v in parts[1:]]
    idle = values[3] + values[4]
    total = sum(values)
    if LAST_CPU is None:
        LAST_CPU = (total, idle)
        return ""
    prev_total, prev_idle = LAST_CPU
    LAST_CPU = (total, idle)
    total_delta = total - prev_total
    idle_delta = idle - prev_idle
    if total_delta <= 0:
        return ""
    return f"{100.0 * (total_delta - idle_delta) / total_delta:.2f}"


def sample_db(stage: str, writer: csv.writer) -> None:
    sql = """
SELECT
  now(),
  (SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'tpcc%'),
  (SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'tpch%'),
  (SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'tpch%' AND state = 'active'),
  (SELECT pg_database_size('h5_tpcc')),
  (SELECT pg_database_size('h5_tpch'));
"""
    cmd = f"LD_LIBRARY_PATH={LD_LIBRARY_PATH} {GSQL} -p {PORT} -d postgres -At -F ',' -c \"{sql}\""
    out = subprocess.check_output(["su", "-", "omm", "-c", cmd], text=True).strip()
    if out:
        fs = os.statvfs("/")
        fs_avail_bytes = fs.f_bavail * fs.f_frsize
        writer.writerow([stage, cpu_percent(), *out.split(","), fs_avail_bytes])


def terminate_residual_workload_backends() -> None:
    sql = """
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT pid FROM pg_stat_activity WHERE application_name IN ('tpch_ap')
  LOOP
    PERFORM pg_terminate_backend(r.pid);
  END LOOP;
END $$;
"""
    try:
        gsql(sql)
    except subprocess.CalledProcessError as exc:
        print(f"[warn] failed to terminate residual workload backends: {exc}", flush=True)


def run_stages(args: argparse.Namespace) -> None:
    args.total_seconds = args.stage_seconds * 5 + 60
    paths = render_configs(args)
    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS / f"stagefit_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    timeline_path = run_dir / "timeline.csv"
    live: list[ProcSpec] = []

    with timeline_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["stage", "cpu_percent", "ts", "tpcc_sessions", "tpch_sessions", "tpch_active", "tpcc_db_bytes", "tpch_db_bytes", "fs_avail_bytes"])

        tp_low = start("tpcc_low", benchbase_cmd("tpcc", paths["tpcc_low"], create=False, load=False, execute=True), run_dir / "tpcc_low.log")
        live.append(tp_low)
        time.sleep(5)

        stages = [
            ("stage1_memory_rich", "ap_s1"),
            ("stage2_reach_limit", "ap_s2"),
            ("stage3_protect_tp", "ap_s3"),
            ("stage4_backpressure", "ap_s4"),
        ]
        for stage, key in stages:
            live.extend(start_configs(stage, "tpch", paths[key], run_dir))
            end = time.time() + args.stage_seconds
            while time.time() < end:
                sample_db(stage, writer)
                fh.flush()
                time.sleep(args.sample_interval)

        tp_high = start("stage5_tp_surge", benchbase_cmd("tpcc", paths["tpcc_high"], create=False, load=False, execute=True), run_dir / "tpcc_high.log")
        live.append(tp_high)
        live.extend(start_configs("stage5_ap_pressure", "tpch", paths["ap_s5"], run_dir))
        end = time.time() + args.stage_seconds
        while time.time() < end:
            sample_db("stage5_tp_surge", writer)
            fh.flush()
            time.sleep(args.sample_interval)

    for spec in reversed(live):
        stop(spec)
    terminate_residual_workload_backends()
    print(f"stage run directory: {run_dir}")


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=20260614)
    parser.add_argument("--tpcc-warehouses", type=int, default=10)
    parser.add_argument("--tpch-scale", type=float, default=0.3)
    parser.add_argument("--ap-work-mem", default="4MB")
    parser.add_argument("--ap-temp-file-limit", default="")
    parser.add_argument("--tp-low-terminals", type=int, default=2)
    parser.add_argument("--tp-low-rate", type=int, default=40)
    parser.add_argument("--tp-high-terminals", type=int, default=12)
    parser.add_argument("--tp-high-rate", default="unlimited")
    parser.add_argument("--stable-tp-high-rate", default=DEFAULT_STABLE_TP_HIGH_RATE)
    parser.add_argument("--stable-workload", action="store_true")
    parser.add_argument("--ap-rate", default="unlimited")
    parser.add_argument("--ap-serial", action="store_true")
    parser.add_argument("--ap-fixed-query-clients", action="store_true")
    parser.add_argument("--ap-query-cycle", default=",".join(map(str, TPCH_HEAVY_QUERY_IDS)))
    parser.add_argument("--ap-s1", type=int, default=1)
    parser.add_argument("--ap-s2", type=int, default=2)
    parser.add_argument("--ap-s3", type=int, default=4)
    parser.add_argument("--ap-s4", type=int, default=8)
    parser.add_argument("--ap-s5", type=int, default=8)
    parser.add_argument("--stage-seconds", type=int, default=120)
    parser.add_argument("--sample-interval", type=int, default=5)
    parser.set_defaults(total_seconds=660)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prepare = sub.add_parser("prepare")
    add_common(p_prepare)
    p_prepare.add_argument("--load", action="store_true")
    p_prepare.add_argument("--load-tpcc", action="store_true")
    p_prepare.add_argument("--load-tpch", action="store_true")
    p_prepare.add_argument("--reset", action="store_true")

    p_run = sub.add_parser("run")
    add_common(p_run)

    args = parser.parse_args()
    if args.cmd == "prepare":
        prepare(args)
    elif args.cmd == "run":
        run_stages(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
