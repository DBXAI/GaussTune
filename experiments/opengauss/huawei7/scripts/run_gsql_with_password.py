#!/usr/bin/env python3
"""Run openGauss gsql with its stdin password pipeline, never argv secrets."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--library-dir", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    password = os.environ.get(args.password_env, "")
    if not password:
        raise RuntimeError(
            "required password environment variable is unset: %s"
            % args.password_env
        )
    if not command or Path(command[0]).name != "gsql":
        raise ValueError("wrapper accepts only an explicit gsql command")
    if "-2" not in command and "--pipeline" not in command:
        command.insert(1, "-2")
    environment = dict(os.environ)
    environment["LD_LIBRARY_PATH"] = str(args.library_dir)
    completed = subprocess.run(
        command, input=password + "\n", text=True, env=environment,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
