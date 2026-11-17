"""Four original mechanisms, small/offline; synthetic performance inputs labeled."""
import json
from pathlib import Path


def run(fixtures):
    from jmt.innovation_1_cache.schema import read_trace
    from jmt.innovation_1_cache.cache_replay import replay_cache
    from jmt.innovation_2_spill.forecast import ForecastSession
    from jmt.innovation_3_search.search import QueryOption, solve_work_mem_dp
    from jmt.innovation_4_performance.device import DeviceSurface, SurfacePoint, ServiceTimes
    from jmt.innovation_4_performance.tps import TpLatencyCalibration, solve_capacity_tps

    fixtures = Path(fixtures)
    events = tuple(read_trace(fixtures / "cache_trace.csv"))
    cache = [
        {"sb_mib": sb, **replay_cache(
            events, shared_buffer_pages=sb*128, os_cache_pages=1024*128,
            measured_workload_classes=("tp",),
        ).stats.path_fractions()}
        for sb in (512, 1024, 2048, 4096)
    ]
    sql = (fixtures / "tpch-03/query.sql").read_text()
    catalog = json.loads((fixtures / "catalog_tpch.json").read_text())
    session = ForecastSession(sql, catalog)
    spill = []
    for wm in (12, 80):
        result = session.forecast(json.loads((fixtures / ("tpch-03/blind_wm%d.json" % wm)).read_text()), wm)
        spill.append({
            "wm_mib": wm, "complete": result["logical_temp_estimate_complete"],
            "logical_temp_read_bytes": result["logical_temp_read_bytes_estimate"],
            "logical_temp_write_bytes": result["logical_temp_write_bytes_estimate"],
            "automatic_recommendation_qualified": False,
        })
    # Explicit synthetic UNIT inputs, never labeled as machine calibration.
    frontier = solve_work_mem_dp({
        1: (QueryOption(1, 64, 80, 100, 50, 10, "unit-a"),
            QueryOption(1, 128, 150, 30, 10, 5, "unit-a")),
        2: (QueryOption(2, 64, 90, 200, 100, 20, "unit-b"),),
    }, 250)
    surface = DeviceSurface([
        SurfacePoint(x, y, 1+0.1*x+0.2*y) for x in (0., 4.) for y in (0., 4.)
    ], "SYNTHETIC_UNIT_NOT_A_MACHINE")
    performance = solve_capacity_tps(
        calibration=TpLatencyCalibration(10, 2, 0.01, 0.1, 10, "SYNTHETIC_UNIT_NOT_A_MACHINE"),
        p_sb=.8, p_os=.1, p_disk=.1,
        tp_read_requests_per_tx=.1, tp_write_requests_per_tx=.05,
        ap_read_iops=100, ap_write_iops=50,
        service=ServiceTimes(1, 1, 1, 1), surface=surface,
    )
    return {
        "innovation_1_cache": {"real_saved_trace_events": len(events), "ratios": cache,
                               "qualification": "Historical small-workset trace; flat ratios are not fabricated."},
        "innovation_2_spill": {"real_saved_blind_plan_forecasts": spill,
                              "qualification": "Known difficult development queries; NOT an accuracy claim."},
        "innovation_3_search": {"synthetic_unit_frontier_states": len(frontier)},
        "innovation_4_performance": {"synthetic_unit_fixed_point": performance},
        "automatic_recommendation_qualified": False,
        "new_configuration_applied": False,
    }
