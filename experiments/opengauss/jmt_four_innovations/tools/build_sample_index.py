#!/usr/bin/env python3
"""Build offline sample frames from an existing collected bundle, WITHOUT ML.

Does not collect data, train DeepDB, renew TTL or invent certificates. A future
fresh read-only collection can use this builder instead of the old train.py.
Certificates, when supplied, must already bind the exact population and frames.
"""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import pickle
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "runtime", ROOT / "vendor"):
    sys.path.insert(0, str(directory))
from legacy_service import SampleFrame
from sample_scopes import MODEL_SCOPES
from sample_service import Service

CERTIFICATES = {
    "SMALL_DIMENSION.json": "small_dimension_certificate",
    "EXISTENCE_CERTIFICATE.json": "existence_certificate",
    "FULL_SHIPPING_CERTIFICATE.json": "full_shipping_certificate",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(bundle_path, out_root, certificates_dir=None):
    bundle_path, out_root = Path(bundle_path).resolve(), Path(out_root).resolve()
    bundle_bytes = bundle_path.read_bytes()
    bundle = json.loads(bundle_bytes)
    expiry = bundle["expires_unix"]
    if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry) or time.time() > expiry):
        raise ValueError("expired/invalid input bundle; no TTL renewal allowed")
    if digest(json.dumps(bundle["metadata"], sort_keys=True).encode()) != bundle["metadata_sha256"]:
        raise ValueError("bundle metadata watermark mismatch")
    if not 1 <= len(bundle["profiles"]) <= 32:
        raise ValueError("sample profile budget is 1..32")
    catalog_bytes = (bundle_path.parent / "catalog.json").read_bytes()
    catalog = json.loads(catalog_bytes)
    if catalog["database"] != bundle["database"]:
        raise ValueError("catalog database mismatch")
    out_root.mkdir(parents=True, exist_ok=False)
    (out_root / "models").mkdir()
    (out_root / "data").mkdir()
    predictive_fields = {
        field for scope in MODEL_SCOPES.values() for field, _ in scope["features"]
    }
    frames, scopes = {}, {}
    started = time.perf_counter()
    for name, manifest in bundle["profiles"].items():
        if not name.replace("_", "").isalnum() or manifest["database"] != bundle["database"]:
            raise ValueError("invalid sample name/database")
        if not 0 < manifest["p"] <= 1 or not 1 <= manifest["rows"] <= 180000:
            raise ValueError("sample probability/row budget invalid")
        raw = (bundle_path.parent / (name + ".json.gz")).read_bytes()
        if digest(raw) != manifest["gzip_sha256"]:
            raise ValueError("changed offline sample: " + name)
        rows = json.loads(gzip.decompress(raw))
        if len(rows) != manifest["rows"]:
            raise ValueError("sample row count mismatch: " + name)
        frame = SampleFrame(rows, catalog)
        del rows
        if name in MODEL_SCOPES:
            scope = MODEL_SCOPES[name]
            keep = {field for field, _ in scope["features"]}
            if not keep <= set(frame.columns):
                raise ValueError("sample lacks required feature columns")
            frame.columns = {field: array for field, array in frame.columns.items() if field in keep}
            frame.kinds = {field: kind for field, kind in frame.kinds.items() if field in keep}
            population = manifest["population_estimate"]
            if not math.isfinite(population) or population <= 0:
                raise ValueError("sample population estimate invalid")
            scopes[name] = {
                **scope, "sample_rows": frame.rows,
                "population_estimate": population,
                "estimator": "direct_sample_count_only",
            }
        frame.mappings = {
            field: mapping for field, mapping in frame.mappings.items()
            if field in predictive_fields
        }
        data = pickle.dumps(frame, protocol=4)
        path = out_root / "models" / (name + "_compact.pkl")
        path.write_bytes(data)
        frames[name] = {
            "path": str(path.relative_to(out_root)), "sha256": digest(data),
            "bytes": len(data), "p": manifest["p"],
            "complete_key": manifest.get("complete_key"),
        }
        del frame
    index = {
        "schema": "jmt.sample-statistics/v1",
        "database": bundle["database"], "frames": frames,
        # Compatibility key contains feature/population metadata, NOT models.
        "models": scopes,
        "metadata_sha256": bundle["metadata_sha256"],
        "created_unix": time.time(), "expires_unix": expiry,
        "source_bundle_sha256": digest(bundle_bytes),
        "learned_models_trained": 0,
        "qualification": "Offline collected samples only; inherited original TTL, no target labels or ML training.",
    }
    if certificates_dir is not None:
        for filename, field in CERTIFICATES.items():
            path = Path(certificates_dir) / filename
            if path.is_file():
                certificate = json.loads(path.read_text())
                if (certificate["metadata_sha256"] != index["metadata_sha256"]
                        or certificate["database"] != index["database"]
                        or certificate["expires_unix"] != expiry
                        or certificate["valid"] is not True):
                    raise ValueError("certificate identity/TTL invalid: " + filename)
                index[field] = certificate
    # Existing Service validates hashes, signatures and exact frame bindings.
    checked = Service(index, out_root)
    checked.validate(index["database"], index["metadata_sha256"])
    del checked
    (out_root / "data/catalog.json").write_bytes(catalog_bytes)
    (out_root / "data/SOURCE_BUNDLE.json").write_bytes(bundle_bytes)
    (out_root / "models/INDEX.json").write_text(json.dumps(index, indent=2) + "\n")
    summary = {
        "schema": "jmt.sample-offline-build/v1",
        "profiles": len(frames), "feature_scopes": len(scopes),
        "learned_models_trained": 0, "learned_model_files_written": 0,
        "frame_bytes": sum(frame["bytes"] for frame in frames.values()),
        "wall_s": time.perf_counter() - started,
        "expires_unix": expiry, "TTL_extended": False,
        "certificate_fields": [field for field in CERTIFICATES.values() if field in index],
        "automatic_recommendation_qualified": False,
        "qualification": "Re-encoding existing offline evidence, not new data collection/accuracy validation.",
    }
    (out_root / "BUILD.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--certificates-dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.bundle, args.out_root, args.certificates_dir), indent=2))


if __name__ == "__main__":
    main()
