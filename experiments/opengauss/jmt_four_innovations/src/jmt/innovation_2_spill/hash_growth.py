"""Bounded event model of openGauss row-Hash bucket/batch growth.

Source: nodeHash.cpp ExecHashTableInsert / ExecHashIncreaseNumBatches.
This is a uniform-hash, estimated-tuple-width model, NOT an observed grant,
skew simulation, per-tuple simulation or physical-memory admission bound.
"""
import math

MAX_EVENTS = 64
MAX_BUCKETS = 1 << 30
POINTER_BYTES = 8


def predict_growth(rows, tuple_bytes, grant_bytes, initial_buckets, initial_batches):
    if not all(isinstance(x, (int, float)) and math.isfinite(x)
               for x in (rows, tuple_bytes, grant_bytes)):
        raise ValueError("nonfinite Hash growth input")
    if rows < 0 or tuple_bytes <= 0 or grant_bytes <= 0:
        raise ValueError("invalid Hash growth volume/grant")
    for x in (initial_buckets, initial_batches):
        if type(x) is not int or x <= 0 or x & (x - 1):
            raise ValueError("Hash directory must be positive powers of two")
    total = math.ceil(rows)
    buckets, batches, events, first_spill = initial_buckets, initial_batches, 0, None
    # While nbatch==1, each power-of-two tuple threshold can enlarge the
    # optimal bucket array. At the first spill the enlarged directory is kept;
    # after nbatch>1 the executor no longer grows optimal buckets this way.
    if batches == 1:
        while True:
            spill_row = math.floor(
                (grant_bytes - buckets * POINTER_BYTES) / tuple_bytes
            ) + 1
            grow_row = buckets + 1 if buckets < MAX_BUCKETS else math.inf
            if total < min(spill_row, grow_row):
                break
            if spill_row < grow_row:
                first_spill = max(1, spill_row)
                batches = 2
                break
            if events >= MAX_EVENTS:
                raise ValueError("Hash growth event budget exceeded")
            buckets *= 2
            events += 1
            if grow_row * tuple_bytes + buckets * POINTER_BYTES > grant_bytes:
                first_spill = grow_row
                batches = 2
                break
    usable = grant_bytes - buckets * POINTER_BYTES
    if usable <= 0:
        raise ValueError("Hash bucket array exhausts the assumed grant")
    # Batch doubling uses uniform expected occupancy; bucket array is fixed
    # after first spill. Do not shrink an executor's original batch directory.
    needed = max(1, math.ceil(rows * tuple_bytes / usable))
    batches = max(batches, 1 << (needed - 1).bit_length())
    return {
        "runtime_buckets": buckets, "batches": batches,
        "dynamic_bucket_growth_events": events,
        "estimated_first_spill_inner_row": first_spill,
        "simulator_event_budget": MAX_EVENTS,
        "qualification": (
            "source-derived power-of-two events; uniform hash and estimated width; "
            "assumed session work_mem, not verified operator grant; "
            "no memory auto-spread, skew or later-batch rewrite simulation"
        ),
    }
