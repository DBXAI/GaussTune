"""Resolve openGauss 5.1 BufferTag prefixes to real relation files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable

from .bio import FiemapPageResolver
from .schema import PageKey, TraceEvent, read_trace


DEFAULTTABLESPACE_OID = 1663
GLOBALTABLESPACE_OID = 1664
FORK_NAMES = ("main", "fsm", "vm", "bcm", "init")


def _relation_name(page: PageKey) -> str:
    name = str(page.rel_node)
    if page.bucket_node >= 0:
        name += "_b%d" % page.bucket_node
    if page.fork_num != 0:
        if not 0 <= page.fork_num < len(FORK_NAMES):
            raise ValueError("unsupported fork number %d" % page.fork_num)
        name += "_" + FORK_NAMES[page.fork_num]
    return name


def relation_path(data_dir: Path, page: PageKey) -> Path:
    """Reproduce `relpathbackend` for permanent row-store relations."""

    name = _relation_name(page)
    if page.spc_node == GLOBALTABLESPACE_OID:
        if page.db_node != 0:
            raise ValueError("global tablespace BufferTag has a nonzero dbNode")
        return data_dir / "global" / name
    if page.spc_node == DEFAULTTABLESPACE_OID:
        return data_dir / "base" / str(page.db_node) / name
    base = data_dir / "pg_tblspc" / str(page.spc_node)
    candidates = [
        path / str(page.db_node) / name
        for path in base.iterdir()
        if path.is_dir() or path.is_symlink()
    ] if base.is_dir() else []
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1:
        raise ValueError(
            "custom tablespace path is ambiguous/missing for %r: %r"
            % (page.prefix(), [str(path) for path in existing])
        )
    return existing[0]


def build_relation_manifest(events: Iterable[TraceEvent], data_dir: Path) -> Dict[str, str]:
    # Only the BufferTag prefix is needed to resolve a relation file.  Keep
    # one page per relation rather than retaining every ACCESS page from a
    # multi-million-event trace.
    representatives: Dict[str, PageKey] = {}
    for event in events:
        if event.page is not None and event.event == "ACCESS":
            key = FiemapPageResolver.relation_key(event.page)
            representatives.setdefault(key, event.page)
    result: Dict[str, str] = {}
    for key, page in sorted(representatives.items()):
        path = relation_path(data_dir, page)
        if not path.is_file():
            raise FileNotFoundError("BufferTag relation file does not exist: %s" % path)
        result[key] = str(path.resolve())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = build_relation_manifest(read_trace(args.trace), args.data_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("relations=%d out=%s" % (len(manifest), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
