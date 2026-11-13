#!/usr/bin/env python3
"""Read actual WM GUC limits and test SESSION SET only; never execute AP SQL."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor")]
from jmt.common.db import Gsql
from jmt.common.database_memory_domain import (
    collect_work_mem_domain, validate_candidate_work_mem,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="h5_tpch")
    parser.add_argument("--work-mem-mib", default="4096,8192,16384")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    client = Gsql("/opt/openGauss/bin/gsql", run_as_user="omm", timeout_s=15)
    domain = collect_work_mem_domain(client, args.database)
    trials = []
    for wm in sorted(set(int(s) for s in args.work_mem_mib.split(","))):
        validate_candidate_work_mem(wm, domain)
        row = client.json(args.database, """SELECT json_build_object(
            'work_mem_kib',current_setting('work_mem'),
            'work_mem_pg_setting',(SELECT setting FROM pg_settings WHERE name='work_mem'))""",
                          wm_mib=wm)
        if int(row["work_mem_pg_setting"]) != wm * 1024:
            raise ValueError("database did not accept requested session work_mem")
        trials.append({"requested_work_mem_mib": wm, "accepted": True,
                       "database_response": row,
                       "only_SET_and_metadata_SELECT_no_AP_or_large_allocation": True})
    result = {
        "schema": "jmt.actual-database-work-mem-SET-probe/v1",
        "checked_at": datetime.now().astimezone().isoformat(), "domain": domain,
        "session_SET_trials": trials, "global_configuration_changed": False,
        "memory_protection_disabled": False,
        "SET_acceptance_does_not_prove_concurrent_allocation_or_physical_memory_safety": True,
        "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    with (args.out / "DATABASE_WM_SET_PROBE.json").open("x") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
