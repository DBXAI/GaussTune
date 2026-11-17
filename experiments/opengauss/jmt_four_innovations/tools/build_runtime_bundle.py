#!/usr/bin/env python3
"""Build a small relocatable runtime archive, without old results, venv or DB."""
import argparse
import hashlib
import io
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def build(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = {}
    for folder in ("src", "vendor", "runtime"):
        for path in sorted((ROOT / folder).rglob("*")):
            if (path.is_file() and "__pycache__" not in path.parts
                    and path.suffix not in (".pyc", ".pyo", ".private")):
                files[path.relative_to(ROOT).as_posix()] = path
    for name in (
        "run_jmt.sh", "requirements-offline.txt", "PORTABLE_RUNTIME.md",
        "RUNTIME_AND_OPERATOR_PHASES_DESIGN.md", "RUNTIME_AND_OPERATOR_PHASES_CURRENT.md",
        "tools/validate_operator_phases.py", "tools/build_runtime_bundle.py",
        "tests/test_runtime_discovery.py", "tests/test_operator_phases.py",
        "tests/test_operator_phase_acceptance.py", "tests/test_boundary_observer.py",
        "tests/test_release_safety.py",
    ):
        files[name] = ROOT / name
    manifest = []
    with tarfile.open(destination, "x:gz") as archive:
        for name, path in sorted(files.items()):
            data = path.read_bytes()
            info = tarfile.TarInfo("jmt-runtime/" + name)
            info.size, info.mtime = len(data), 0
            info.mode = 0o755 if name == "run_jmt.sh" else 0o644
            archive.addfile(info, io.BytesIO(data))
            manifest.append(hashlib.sha256(data).hexdigest() + "  " + name)
        data = ("\n".join(manifest) + "\n").encode()
        info = tarfile.TarInfo("jmt-runtime/BUNDLE_SHA256SUMS")
        info.size, info.mtime, info.mode = len(data), 0, 0o644
        archive.addfile(info, io.BytesIO(data))
    return {"files": len(files), "bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(json.dumps(build(parser.parse_args().out), indent=2))
