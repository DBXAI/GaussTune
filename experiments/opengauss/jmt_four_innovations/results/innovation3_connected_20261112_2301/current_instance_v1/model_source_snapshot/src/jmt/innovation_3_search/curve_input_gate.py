"""Live step-three input gate; validate evidence, never refit spill curves.

Expiry alone is insufficient: data/catalog/machine can change before expiry.
Callbacks do read-only probes, keeping this module and its tests stdlib-only.
Frozen replay intentionally does not call this live-validity function.
"""
from datetime import datetime
import hashlib
from pathlib import Path
import time

from jmt.innovation_2_spill.curves import validate_spill_curves


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(2 ** 20), b""):
            digest.update(block)
    return digest.hexdigest()


def revalidate_curve_inputs(curves, *, machine_probe, catalog_probe, statistics_probe, now=None):
    """Return recorded validation, without changing curves/statistic timestamps."""
    validate_spill_curves(curves)
    clock = time.time() if now is None else now
    if curves.get("frozen_replay_only") or curves.get("current_inputs_revalidated") is not True:
        raise ValueError("normal search requires a non-frozen validated curve artifact")
    valid_from, expires = curves.get("valid_from_unix"), curves.get("expires_unix")
    if (isinstance(valid_from, bool) or isinstance(expires, bool)
            or not isinstance(valid_from, (int, float)) or not isinstance(expires, (int, float))
            or not valid_from <= clock <= expires):
        raise ValueError("curve statistics expired/from future; never renew acquisition times")
    sources = curves.get("source_artifacts")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 20000:
        raise ValueError("bounded source provenance required for live curve consumption")
    for source in sources:
        if file_sha(Path(source["path"])) != source["sha256"]:
            raise ValueError("spill curve source changed: " + source["path"])
    catalog_paths = {Path(s["path"]) for s in sources if Path(s["path"]).name == "CATALOG.json"}
    if len(catalog_paths) != 1:
        raise ValueError("one bound prediction catalog is required")
    import json
    stored_catalog = json.loads(next(iter(catalog_paths)).read_text())
    if stored_catalog["database"] != curves["database"]:
        raise ValueError("curve/catalog database mismatch")
    machine = machine_probe(curves["machine"])
    if machine["machine_fingerprint"] != curves["machine_fingerprint"]:
        raise ValueError("live machine/executor no longer matches spill curves")
    live_catalog = catalog_probe(curves["database"])
    if live_catalog["catalog_snapshot_fingerprint"] != stored_catalog["catalog_snapshot_fingerprint"]:
        raise ValueError("live catalog changed since spill prediction")
    bindings, checked_from, checked_expires = statistics_probe(curves["statistics"])
    if (checked_from, checked_expires) != (valid_from, expires):
        raise ValueError("curve validity interval differs from raw sample acquisition interval")
    if set(bindings) != set(curves["statistics"]):
        raise ValueError("curve statistics identity mismatch")
    for kind, old in curves["statistics"].items():
        current = bindings[kind]
        for key in ("sha256", "generated_unix", "expires_unix", "watermark_sha256"):
            if current[key] != old[key]:
                raise ValueError("sample identity/watermark/clock changed: " + kind)
        if current.get("current_watermark_verified") is not True or current.get("TTL_renewed") is not False:
            raise ValueError("live sample watermark check required, without TTL renewal")
    return {
        "checked_at": datetime.fromtimestamp(clock).astimezone().isoformat(),
        "source_artifact_hashes_verified": len(sources),
        "current_machine_verified": True, "current_catalog_verified": True,
        "current_statistics_watermarks_verified": True,
        "statistics_valid_from_unix": checked_from, "statistics_expires_unix": checked_expires,
        "statistic_or_curve_TTL_renewed": False, "spill_predictions_refit": False,
        "new_target_AP_queries_executed": False,
    }
