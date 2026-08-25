#!/usr/bin/env python3
"""Create the two dedicated Huawei7 login roles without exposing passwords in argv."""

import argparse
import os
import re
import subprocess
from pathlib import Path


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gauss-home", type=Path, default=Path("/opt/openGauss"))
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--tp-user", default="h7_tp")
    parser.add_argument("--ap-user", default="h7_ap")
    parser.add_argument("--tp-password-env", default="HUAWEI7_TP_PASSWORD")
    parser.add_argument("--ap-password-env", default="HUAWEI7_AP_PASSWORD")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("role creation must run as root and invokes gsql as omm")
    for value in (args.tp_user, args.ap_user):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("role names must be simple identifiers")
    tp_password = os.environ.get(args.tp_password_env, "")
    ap_password = os.environ.get(args.ap_password_env, "")
    if not tp_password or not ap_password:
        raise RuntimeError("both password environment variables must be set")
    sql = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '%s') THEN
    CREATE ROLE %s LOGIN PASSWORD %s;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '%s') THEN
    CREATE ROLE %s LOGIN PASSWORD %s;
  END IF;
END $$;
""" % (
        args.tp_user, args.tp_user, quote_literal(tp_password),
        args.ap_user, args.ap_user, quote_literal(ap_password),
    )
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.gauss_home / "lib")
    # Passwords travel only on stdin, not command argv or an on-disk SQL file.
    subprocess.run([
        "runuser", "-u", "omm", "--", str(args.gauss_home / "bin" / "gsql"),
        "-X", "-v", "ON_ERROR_STOP=1", "-p", str(args.port), "-d", "postgres",
    ], input=sql, text=True, env=environment, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
