"""Default experimental AP predictor: offline samples, online bounded arithmetic."""
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from sample_service import Service, PlanProvider
from jmt.innovation_2_spill.forecast import ForecastSession

FORBIDDEN_IMPORT_ROOTS = {
    "rspn", "spn", "scipy", "sklearn", "pandas", "torch", "tensorflow",
}
OBSERVED_PLAN_FIELDS = {
    "Execution Time", "Temp Read Blocks", "Temp Written Blocks",
    "Peak Memory Usage", "Disk Usage", "Hash Buckets", "Hash Batches",
}


def reject_observed_plan(value):
    """Only prior EXPLAIN plans are allowed at the prediction boundary."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.startswith("Actual ") or key in OBSERVED_PLAN_FIELDS:
                raise ValueError("execution observation is not a blind input: " + key)
            reject_observed_plan(item)
    elif isinstance(value, list):
        for item in value:
            reject_observed_plan(item)


class SampleRuntime:
    def __init__(self, index_path, artifact_root=None):
        self.index_path = Path(index_path).resolve()
        self.index_bytes = self.index_path.read_bytes()
        self.index = json.loads(self.index_bytes)
        expiry = self.index["expires_unix"]
        if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or time.time() > expiry):
            raise ValueError("expired/invalid statistics; refuse before loading frames")
        self.artifact_root = (
            Path(artifact_root).resolve() if artifact_root is not None
            else self.index_path.parent.parent
        )
        # Freshness and identity are checked again for EVERY request.
        self.service = Service(self.index, self.artifact_root)

    def forecast(self, sql, catalog, plans, metadata_sha256):
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("nonempty SQL is required")
        if catalog.get("database") != self.index["database"]:
            raise ValueError("catalog database differs from statistics")
        if not plans or len(plans) > 16:
            raise ValueError("a request must have 1..16 WM plans")
        self.service.validate(catalog["database"], metadata_sha256)
        for point in plans:
            wm = point["wm_mib"]
            if isinstance(wm, bool) or not isinstance(wm, (int, float)):
                raise ValueError("WM must be numeric")
            if not math.isfinite(wm) or not 0 < wm <= 4096:
                raise ValueError("WM must be finite, positive and <=4096 MiB")
            reject_observed_plan(point["document"])
        self.service.clear_request_cache()
        provider = PlanProvider(self.service, metadata_sha256, groups=True)
        started = time.perf_counter()
        session = ForecastSession(sql, catalog, cardinality_provider=provider)
        predictions = [
            {"wm_mib": point["wm_mib"],
             "forecast": session.forecast(point["document"], point["wm_mib"])}
            for point in plans
        ]
        # Never return a successful batch spanning expiry.
        self.service.validate(catalog["database"], metadata_sha256)
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "schema": "jmt.sample-default-experiment/v1",
            "backend": "direct_sample",
            "statistics_index_sha256": hashlib.sha256(self.index_bytes).hexdigest(),
            "statistics_expires_unix": self.index["expires_unix"],
            "sample_frames_loaded": len(self.service.frames),
            "learned_models_loaded": len(self.service.models),
            "heavy_modules_imported": sorted({
                name.split(".")[0] for name in sys.modules
                if name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS
            }),
            "warm_prediction_batch_ms": elapsed,
            "predictions": predictions,
            "automatic_recommendation_qualified": False,
            "physical_request_prediction": None,
            "ap_runtime_prediction_seconds": None,
            "physical_memory_safety_qualified": False,
            "qualification": (
                "Experimental logical temp IO only. No database call, target observation, "
                "fake clock, TTL extension or five-stage control in this entrypoint."
            ),
        }
