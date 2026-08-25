"""Source/binary provenance checks used before every real Huawei7 run."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Dict, Mapping, Set


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_path_hash_evidence(value: object, context: str = "evidence") -> int:
    """Recursively rehash canonical ``{path, sha256}`` evidence rows."""

    checked = 0
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            path = Path(str(value["path"]))
            if not path.is_absolute():
                raise ValueError("%s evidence path is not absolute: %s" % (context, path))
            if not path.is_file() or sha256(path) != str(value["sha256"]):
                raise ValueError("%s evidence is missing or changed: %s" % (context, path))
            checked += 1
        for key, child in value.items():
            checked += validate_path_hash_evidence(
                child, context + "." + str(key),
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            checked += validate_path_hash_evidence(
                child, "%s[%d]" % (context, index),
            )
    return checked


def validate_json_evidence_tree(
    path: Path, context: str = "evidence", visited: Set[Path] | None = None,
) -> int:
    """Rehash canonical rows and follow every JSON artifact they reference.

    This turns a derived report's ``source_artifacts`` list into a transitive
    audit: collection JSONs lead to probe logs and transaction JSONs, and those
    transaction JSONs lead to the native benchmark output that was parsed.
    """

    resolved = path.resolve()
    seen = visited if visited is not None else set()
    if resolved in seen:
        return 0
    seen.add(resolved)
    if not resolved.is_file():
        raise ValueError("%s JSON evidence is missing: %s" % (context, resolved))
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("%s is not valid JSON evidence: %s" % (context, resolved)) from error
    checked = validate_path_hash_evidence(document, context)

    def visit(value: object, child_context: str) -> int:
        followed = 0
        if isinstance(value, dict):
            if "path" in value and "sha256" in value:
                child = Path(str(value["path"]))
                if child.suffix.lower() == ".json":
                    followed += validate_json_evidence_tree(child, child_context, seen)
            for key, child_value in value.items():
                followed += visit(child_value, child_context + "." + str(key))
        elif isinstance(value, list):
            for index, child_value in enumerate(value):
                followed += visit(child_value, "%s[%d]" % (child_context, index))
        return followed

    return checked + visit(document, context)


def check_manifest(manifest_path: Path, source_root: Path, gaussdb: Path) -> Dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest["source_files"]
    mismatches = []
    actual_files = {}
    for relative, expected in files.items():
        path = source_root / relative
        actual = sha256(path) if path.is_file() else "missing"
        actual_files[relative] = actual
        if actual != expected:
            mismatches.append("%s expected=%s actual=%s" % (relative, expected, actual))
    commit = subprocess.check_output(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True,
    ).strip()
    if commit != manifest["source_commit"]:
        mismatches.append("source commit expected=%s actual=%s" % (
            manifest["source_commit"], commit,
        ))
    symbols_text = subprocess.check_output(
        ["nm", "-D", "--defined-only", str(gaussdb)], text=True,
    )
    for symbol in manifest["required_symbols"]:
        if symbol not in symbols_text:
            mismatches.append("required gaussdb symbol missing: %s" % symbol)
    actual_gaussdb_sha256 = sha256(gaussdb)
    expected_gaussdb_sha256 = manifest.get("reference_gaussdb_sha256", "")
    if not expected_gaussdb_sha256:
        mismatches.append("reference_gaussdb_sha256 is missing from source manifest")
    elif actual_gaussdb_sha256 != expected_gaussdb_sha256:
        mismatches.append("gaussdb sha256 expected=%s actual=%s" % (
            expected_gaussdb_sha256, actual_gaussdb_sha256,
        ))
    result = {
        "source_commit": commit,
        "source_files": actual_files,
        "gaussdb_sha256": actual_gaussdb_sha256,
        "expected_reference_gaussdb_sha256": expected_gaussdb_sha256,
        "mismatches": mismatches,
        "valid": not mismatches,
    }
    if mismatches:
        raise RuntimeError("provenance check failed:\n" + "\n".join(mismatches))
    return result
