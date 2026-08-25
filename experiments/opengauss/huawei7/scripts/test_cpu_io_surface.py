#!/usr/bin/env python3
"""Deterministic tests for the joint CPU/IO fixed-point model."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from huawei7.cpu_io_surface import predict_stage_with_cpu_io_surface
from huawei7.buffered_path import BufferedPathPoint, BufferedTPRequestSurface
from huawei7.device import DeviceSurface, ServiceTimes, SurfacePoint


def _surface() -> DeviceSurface:
    points = []
    for tp in (0.0, 2.0, 4.0):
        for ap in (0.0, 2.0, 4.0):
            points.append(SurfacePoint(tp, ap, 1.0 + 0.25 * (tp + ap)))
    return DeviceSurface(
        points, "a" * 64, ap_read_fraction=1.0, ap_mix_tolerance=0.05,
    )


def _predict(ap_cpu: float):
    return predict_stage_with_cpu_io_surface(
        benchmark="test",
        stage="S1",
        terminals=10,
        base_predicted_tps=900.0,
        base_latency_ms=11.111111111,
        base_disk_latency_ms=1.0475,
        p_disk=0.5,
        accesses_per_tx=10.0,
        tp_read_requests_per_tx=0.10,
        tp_write_requests_per_tx=0.0,
        ap_read_iops=100.0,
        ap_write_iops=0.0,
        service=ServiceTimes(
            tp_read_ms=1.0, tp_write_ms=1.0,
            ap_read_ms=1.0, ap_write_ms=1.0,
        ),
        surface=_surface(),
        tp_cpu_ms_per_tx=1.0,
        ap_cpu_seconds_per_second=ap_cpu,
        cpu_capacity_seconds_per_second=4.0,
    )


def _buffered_surface() -> BufferedTPRequestSurface:
    points = [
        BufferedPathPoint(0.0, 1.0, 250.0, 3, 0.0),
        BufferedPathPoint(1000.0, 3.0, 350.0, 3, 1.0),
        BufferedPathPoint(2000.0, 5.0, 450.0, 3, 1.0),
    ]
    return BufferedTPRequestSurface(
        points, "b" * 64,
        baseline_tp_buffer_access_await_ms=1.0,
        ap_read_fraction=1.0,
    )


def main() -> int:
    no_ap = _predict(0.0)
    with_ap = _predict(0.5)
    assert 0 < no_ap.predicted_tps <= no_ap.base_predicted_tps * (1.0 + 1e-8)
    assert 0 < with_ap.predicted_tps < no_ap.predicted_tps
    assert with_ap.total_cpu_utilization > no_ap.total_cpu_utilization
    assert with_ap.cpu_queue_delay_ms >= no_ap.cpu_queue_delay_ms
    assert abs(with_ap.io_latency_delta_ms) < 1.0
    assert with_ap.iterations > 0

    buffered = predict_stage_with_cpu_io_surface(
        benchmark="test",
        stage="S1",
        terminals=10,
        base_predicted_tps=900.0,
        base_latency_ms=11.111111111,
        base_disk_latency_ms=1.0475,
        p_disk=0.5,
        accesses_per_tx=10.0,
        tp_read_requests_per_tx=0.10,
        tp_write_requests_per_tx=0.0,
        ap_read_iops=1000.0,
        ap_write_iops=0.0,
        service=ServiceTimes(
            tp_read_ms=1.0, tp_write_ms=1.0,
            ap_read_ms=1.0, ap_write_ms=1.0,
        ),
        surface=_surface(),
        buffered_surface=_buffered_surface(),
        ap_buffer_accesses_per_second=1000.0,
        tp_cpu_ms_per_tx=1.0,
        ap_cpu_seconds_per_second=0.0,
        cpu_capacity_seconds_per_second=4.0,
        native_tp_buffer_accesses_per_tx=250.0,
    )
    assert buffered.buffered_tp_access_added_wait_ms > 0.0
    assert buffered.buffered_ap_accesses_per_second == 1000.0
    assert buffered.io_latency_delta_ms > 0.0
    assert buffered.buffered_tp_buffer_accesses_per_tx > 250.0
    assert buffered.buffered_transaction_latency_delta_ms == (
        buffered.io_latency_delta_ms
    )
    # The direct FIO delta is retained as diagnostics but is not added a
    # second time once database-buffered access latency is measured.
    assert buffered.direct_device_latency_delta_ms >= 0.0

    print("CPU/IO surface tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
