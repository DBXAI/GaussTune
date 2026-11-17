"""O(1) alignment of buffer events to complete observed block windows.

This establishes a common cache/IO interval. It does NOT establish a
transaction-count interval, probe-overhead qualification or performance truth.
"""
import math


def align_complete_block_window(
    block, requested_start_ns, requested_end_ns, tolerance_seconds=.001,
):
    endpoints = (
        requested_start_ns, requested_end_ns, block.start_ns, block.end_ns,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in endpoints):
        raise ValueError("measurement window endpoints must be integer nanoseconds")
    if (not math.isfinite(tolerance_seconds) or tolerance_seconds < 0
            or requested_end_ns <= requested_start_ns):
        raise ValueError("invalid requested measurement window")
    if not requested_start_ns <= block.start_ns < block.end_ns <= requested_end_ns:
        raise ValueError("complete block window lies outside requested measurement interval")
    seconds = (block.end_ns - block.start_ns) / 1e9
    if (not math.isfinite(block.duration_seconds)
            or abs(seconds - block.duration_seconds) > tolerance_seconds):
        raise ValueError("block windows are incomplete, gapped or duration-inconsistent")
    return {
        "clock": "monotonic_ns",
        "start_ns": block.start_ns,
        "end_ns": block.end_ns,
        "duration_seconds": seconds,
        "requested_start_ns": requested_start_ns,
        "requested_end_ns": requested_end_ns,
        "discarded_edge_seconds": (
            requested_end_ns - requested_start_ns
            - (block.end_ns - block.start_ns)) / 1e9,
        "transaction_count_rescaled": False,
        "transaction_window_qualified": False,
    }
