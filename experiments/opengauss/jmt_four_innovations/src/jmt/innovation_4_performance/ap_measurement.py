"""Explicit offline query measurement adapter; never imported by inference."""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile

from jmt.common.db import readonly_query


class ReadonlyAPMeasurement:
    def __init__(self, worker_script, database, gaussdb_pid, application_name,
                 host="/tmp", port=5432, os_user="omm", python="/usr/bin/python3"):
        self.directory = Path(tempfile.mkdtemp(prefix="jmt-ap-worker-"))
        self.directory.chmod(0o755)
        script = self.directory / "worker.py"
        shutil.copyfile(worker_script, script)
        script.chmod(0o644)
        self.stderr = (self.directory / "stderr.log").open("w+")
        self.process = subprocess.Popen([
            "runuser", "-u", os_user, "--", "env",
            "LD_LIBRARY_PATH=/opt/openGauss/lib", python, str(script),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, bufsize=1, cwd=str(self.directory))
        self._write({"database": database, "host": host, "port": port,
                     "user": os_user, "gaussdb_pid": gaussdb_pid,
                     "application_name": application_name})
        initial = self._read()
        if initial.get("status") != "connected":
            self.close()
            raise RuntimeError("measurement connection rejected: " + str(initial))
        self.tid = initial["linux_tid"]

    def _write(self, document):
        self.process.stdin.write(json.dumps(document) + "\n")
        self.process.stdin.flush()

    def _read(self, timeout=55):
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("measurement worker RPC deadline")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("measurement worker exited without a record")
        return json.loads(line)

    def rpc(self, document):
        self._write(document)
        return self._read()

    def measure(self, sql, wm_mib, observer=None):
        sql = readonly_query(sql)
        if shutil.disk_usage(self.directory).free < 8 * 1024**3:
            raise RuntimeError("less than 8GiB free; refusing target temp/trace load")
        ready = self.rpc({"command": "prepare", "sql": sql, "wm_mib": wm_mib})
        if ready.get("status") != "ready":
            return ready
        before = None
        try:
            if observer is not None:
                observer.watch(self.tid)
                before = observer.snapshot()
            result = self.rpc({"command": "execute"})
            if observer is not None:
                try:
                    result["block"] = observer.finish(before)
                except Exception as error:
                    result["block"] = None
                    result["block_error"] = str(error)
            else:
                result["block"] = None
                result["block_error"] = "block observer unavailable; NOT zero IO"
            return result
        finally:
            if observer is not None:
                observer.watch(0)
            self.rpc({"command": "rollback"})

    def close(self):
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            try:
                self._write({"command": "close"})
                process.wait(timeout=5)
            except Exception:
                process.terminate()
                process.wait(timeout=5)
        handle = getattr(self, "stderr", None)
        if handle is not None:
            handle.close()
        if getattr(self, "directory", None) is not None:
            shutil.rmtree(self.directory)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
