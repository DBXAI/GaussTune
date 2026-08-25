#!/usr/bin/env python3
"""Fail-fast preflight for a real Huawei7 experiment host."""

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.provenance import check_manifest


def tracepoint_layout():
    candidates = (
        Path("/sys/kernel/debug/tracing/events/block/block_rq_issue/format"),
        Path("/sys/kernel/tracing/events/block/block_rq_issue/format"),
    )
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise RuntimeError("block_rq_issue tracepoint format is unavailable")
    content = path.read_text(encoding="utf-8")
    match = re.search(
        r"field:char rwbs\[8\];\s*offset:(\d+);\s*size:(\d+);", content,
    )
    if match is None:
        raise RuntimeError("block_rq_issue rwbs field is missing")
    offset, size = map(int, match.groups())
    if (offset, size) != (32, 8):
        raise RuntimeError(
            "block probe requires rwbs offset/size 32/8, got %d/%d"
            % (offset, size)
        )
    return {"path": str(path), "rwbs_offset": offset, "rwbs_size": size}


def command_version(command):
    path = shutil.which(command)
    if path is None:
        return {"path": None, "version": None}
    completed = subprocess.run(
        [path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False,
    )
    return {"path": path, "version": completed.stdout.splitlines()[0] if completed.stdout else ""}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--gaussdb", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    tools = {name: command_version(name) for name in (
        "python3", "bpftrace", "fio", "sysbench", "java", "git", "nm",
    )}
    missing = [name for name, row in tools.items() if row["path"] is None]
    if missing:
        raise RuntimeError("required commands missing: " + ", ".join(missing))
    provenance = check_manifest(
        ROOT / "config" / "source_manifest.json", args.source_root, args.gaussdb,
    )
    result = {
        "schema": "huawei7.doctor/v1",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "tools": tools,
        "provenance": provenance,
        "tracepoint_layout": tracepoint_layout(),
        "valid": True,
    }
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
