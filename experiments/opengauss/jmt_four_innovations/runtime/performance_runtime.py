"""Connect the sample-only AP kernel to calibrated resource-cost inference."""
import time
from sample_runtime import SampleRuntime
from jmt.innovation_4_performance.ap_resource_bridge import (
    BridgeUnavailable, extract_work, predict_bridge,
)


class PerformanceRuntime:
    def __init__(self, index_path, bridge_bundle, artifact_root=None):
        self.samples = SampleRuntime(index_path, artifact_root)
        self.bundle = bridge_bundle

    def forecast(self, request):
        started = time.perf_counter()
        result = self.samples.forecast(
            request["sql"], request["catalog"], request["plans"],
            request["metadata_sha256"],
        )
        for point, plan in zip(result["predictions"], request["plans"]):
            try:
                work = extract_work(point["forecast"], plan["document"], request["catalog"])
                point["resource_work"] = work
                point["performance"] = predict_bridge(
                    self.bundle, work, request["machine_fingerprint"],
                    request["environment_profile"],
                    query_dop=request.get("query_dop", 1),
                    vector_engine=request.get("vector_engine", False),
                )
            except BridgeUnavailable as error:
                point["performance"] = {
                    "status": "unavailable", "reason": str(error),
                    "isolated_wall_seconds_estimate": None,
                    "automatic_recommendation_qualified": False,
                }
        result["physical_memory_safety_qualified"] = False
        result["automatic_recommendation_qualified"] = False
        result["performance_request_ms"] = (time.perf_counter() - started) * 1000
        result["performance_request_timing_scope"] = (
            "SQL/sample N/G + all WM spill + CPU AST work + bridge, resident artifacts; no DB/network/loading"
        )
        return result
