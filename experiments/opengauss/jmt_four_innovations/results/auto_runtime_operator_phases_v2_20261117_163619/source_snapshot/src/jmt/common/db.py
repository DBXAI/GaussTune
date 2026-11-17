"""Portable gsql adapter: session-only, read-only, timed, explicit connection.

Credentials stay in the caller's environment/.pgpass; never written in reports.
The shipped CLI has no apply/restart/reset operation. Legacy control helpers are
not called here. All sessions roll back and have a server statement_timeout.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def readonly_query(sql):
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        raise RuntimeError("Live validation requires sqlglot==26.33.0 (SQL safety parser).")
    parsed = sqlglot.parse(sql, read="postgres")
    if len(parsed) != 1 or parsed[0] is None or not isinstance(
        parsed[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
    ):
        raise ValueError("exactly one SELECT/UNION/INTERSECT/EXCEPT is required")
    for node in parsed[0].walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
                             exp.Alter, exp.Command, exp.Into, exp.Lock)):
            raise ValueError("write/DDL/locking constructs are prohibited")
        if isinstance(node, exp.Func):
            name = node.name.lower() if isinstance(node, exp.Anonymous) else node.sql_name().lower()
            if re.search(r"(pg_sleep|pg_cancel|pg_terminate|dblink|lo_export|set_config|pg_read_file|nextval|setval)", name):
                raise ValueError("unsafe function in validation SQL: " + name)
    return sql.rstrip().rstrip(";")


def extract_json(stdout):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*([\[{])", stdout):
        try:
            value, _ = decoder.raw_decode(stdout[match.start():].lstrip())
            return value
        except ValueError:
            continue
    raise ValueError("gsql did not return a JSON object/array")


class Gsql:
    def __init__(self, gsql="gsql", host="/tmp", port=5432, user=None,
                 run_as_user=None, timeout_s=45):
        binary = shutil.which(gsql)
        if not binary or not Path(binary).is_file():
            raise ValueError("gsql not found; pass --gsql or put it on PATH")
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 120:
            raise ValueError("validation timeout must be in (0,120] seconds")
        if not 1 <= int(port) <= 65535:
            raise ValueError("invalid port")
        self.binary, self.host, self.port = str(Path(binary).resolve()), host, int(port)
        self.user, self.run_as_user, self.timeout_s = user, run_as_user, timeout_s

    def execute(self, database, sql, wm_mib=32, row_executor=True):
        from .memory_units import MAX_WORK_MEM_MIB, valid_work_mem_mib
        if wm_mib is not None and not valid_work_mem_mib(wm_mib):
            raise ValueError("integer work_mem in 1..%d MiB required" % MAX_WORK_MEM_MIB)
        timeout_ms = int(self.timeout_s * 1000)
        settings = (
            "SET default_transaction_read_only=on;"
            "SET application_name='jmt_release_validation';"
            "SET statement_timeout='%dms';" % timeout_ms
        )
        if wm_mib is not None:
            settings += "SET work_mem='%dMB';" % wm_mib
        if row_executor:
            settings += "SET enable_vector_engine=off;SET query_dop=1;"
        command = [self.binary, "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1",
                   "-h", self.host, "-p", str(self.port), "-d", database]
        if self.user:
            command.extend(["-U", self.user])
        # Use an explicit batch command. SQL text is visible in argv; credentials
        # are NEVER placed
        # there. Callers should use trusted benchmark SQL, not secret literals.
        # In openGauss A compatibility, gsql treats BEGIN as a PL/SQL block
        # opener and can truncate a multiline EXPLAIN. START TRANSACTION is
        # unambiguous in both A and PostgreSQL compatibility modes.
        batch = settings + "START TRANSACTION READ ONLY;" + sql.rstrip("; \n") + ";ROLLBACK;"
        command.extend(["-c", batch])
        environment = dict(os.environ)
        home = str(Path(self.binary).parent.parent)
        environment["GAUSSHOME"] = home
        environment["LD_LIBRARY_PATH"] = str(Path(home) / "lib") + (
            ":" + environment["LD_LIBRARY_PATH"] if environment.get("LD_LIBRARY_PATH") else ""
        )
        if self.run_as_user:
            command = ["runuser", "-u", self.run_as_user, "--", "env",
                       "GAUSSHOME=" + environment["GAUSSHOME"],
                       "LD_LIBRARY_PATH=" + environment["LD_LIBRARY_PATH"]] + command
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command, text=True, capture_output=True, env=environment,
                timeout=self.timeout_s + 5,
            )
            result = {"exit_code": proc.returncode, "stdout": proc.stdout,
                      "stderr": proc.stderr, "wall_s": time.monotonic() - started}
        except subprocess.TimeoutExpired:
            result = {"exit_code": 124, "stdout": "", "stderr": "client deadline exceeded",
                      "wall_s": time.monotonic() - started}
        result["sql_sha256"] = hashlib.sha256(sql.encode()).hexdigest()
        return result

    def json(self, database, sql, wm_mib=32):
        result = self.execute(database, sql, wm_mib)
        if result["exit_code"]:
            raise RuntimeError("read-only gsql request failed: " + result["stderr"][-1600:])
        return extract_json(result["stdout"])

    def explain(self, database, sql, wm_mib, analyze=False):
        query = readonly_query(sql)
        options = "ANALYZE,BUFFERS,VERBOSE,FORMAT JSON" if analyze else "VERBOSE,FORMAT JSON"
        return self.execute(database, "EXPLAIN (" + options + ") " + query, wm_mib)
