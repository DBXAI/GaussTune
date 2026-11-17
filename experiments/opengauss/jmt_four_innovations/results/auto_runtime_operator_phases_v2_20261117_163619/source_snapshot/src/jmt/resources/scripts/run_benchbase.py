#!/usr/bin/env python3
"""Run a frozen BenchBase argv from the package directory it requires."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not args.home.is_dir() or not (args.home / "config" / "plugin.xml").is_file():
        raise ValueError("BenchBase home lacks config/plugin.xml")
    if not command or Path(command[0]).name != "java":
        raise ValueError("BenchBase wrapper accepts only an explicit Java command")
    return subprocess.run(command, cwd=args.home).returncode


if __name__ == "__main__":
    raise SystemExit(main())
