"""Bounded operator-boundary work program, NOT a tuple/event simulator.

Reuses innovation 2 work verbatim (including its estimation errors). The cost
vector is conserved, not a second spill estimator. Hash batch replay and hash
aggregate spill are coarse fluid phases, not claimed exact execution traces.
Unknown physical IO is never filled with logical bytes or zero.
"""
import math
import time

from .ap_resource_bridge import BASIS, BridgeUnavailable, extract_work, resource_vector
from jmt.innovation_2_spill.ap_plan_model import blind_root

SCHEMA = "jmt.operator-boundary-phases/v2"
MAX_NODES, MAX_PHASES, MAX_ATTACHMENTS = 256, 512, 65536
N = len(BASIS)
STREAMING = {"Result", "Subquery Scan", "Limit", "Unique", "Group"}
LEAVES = {"Seq Scan", "Index Scan", "Index Only Scan", "Values Scan",
          "Bitmap Heap Scan", "Bitmap Index Scan", "Cte Scan", "CTE Scan"}


def zero():
    return [0.0] * N


def scaled(vector, fraction):
    return [v * fraction for v in vector]


def plus(left, right):
    return [a + b for a, b in zip(left, right)]


def phase(label, output=0.0):
    return {"label": label, "vector": zero(), "contributions": {},
            "output_fraction": output, "ends": []}


def add(stage, path, vector):
    stage["vector"] = plus(stage["vector"], vector)
    stage["contributions"][path] = plus(stage["contributions"].get(path, zero()), vector)


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def build_phases(forecast, document, catalog=None):
    """Pure, blind input; no execution statistics/timers enter a work program."""
    started = time.process_time_ns()
    if forecast.get("is_hypothetical_scenario"):
        raise BridgeUnavailable("hypothetical work cannot become a phase forecast")
    work = extract_work(forecast, document if catalog is not None else None, catalog,
                        include_per_operator=True)
    operators = forecast["operators"]
    if not 1 <= len(operators) <= MAX_NODES:
        raise BridgeUnavailable("phase operator budget")
    ops = {op["path"]: op for op in operators}
    if len(ops) != len(operators):
        raise ValueError("duplicate operator path")
    correction = work["CPU_feature_corrections"]
    vectors = {}
    unsupported, timing_uncertainties, assumptions = [], [], [
        "DOP=1 row executor; one coarse fluid segment per blocking/replay boundary, not per tuple",
        "Innovation 2 cardinality/spill errors remain; work conservation does not prove accuracy",
        "Hash replay CPU absent from innovation 2 is not invented by this layer",
        "Coarse spill replay distribution is approximate; grants/cache/physical IO are not inferred",
    ]
    for path, op in ops.items():
        if not _number(op["calls"]) or op["calls"] != 1:
            unsupported.append({"path": path, "reason": "rescans/correlated execution not phase-qualified"})
        features = dict(op["features"])
        local = correction["per_operator"][path] if correction else {}
        if correction:
            features["predicate_evaluations"] = local["predicate_evaluations"]
        hashed = (op["node_type"] == "HashAggregate"
                  or op["node_type"] == "Aggregate" and op.get("strategy") == "Hashed")
        interaction = (op["input_rows_per_call"] * op["calls"] / 1e6
                       * math.log2(1 + op["memory_mib"]) if hashed else 0.0)
        vector = resource_vector(features, interaction,
                                 local.get("numeric_arithmetic_M", 0.0),
                                 local.get("numeric_avg_M", 0.0))
        vector[0] = 0.0  # one query startup, not one intercept per operator
        vectors[path] = vector
    root = blind_root(document)
    seen = set()
    attachments = 0

    def distribute(stages, path, vector):
        nonlocal attachments
        producers = [s for s in stages if s["output_fraction"] > 0]
        total = sum(s["output_fraction"] for s in producers)
        if not producers or not math.isclose(total, 1.0, rel_tol=1e-8, abs_tol=1e-8):
            raise BridgeUnavailable("invalid producer phase fractions")
        for stage in producers:
            attachments += 1
            if attachments > MAX_ATTACHMENTS:
                raise BridgeUnavailable("phase work-attachment budget")
            add(stage, path, scaled(vector, stage["output_fraction"] / total))

    def barrier(stages, path, kind):
        stages[-1]["ends"].append({"path": path, "kind": kind})
        for stage in stages:
            stage["output_fraction"] = 0.0

    def visit(node, path="", depth=0):
        if depth > MAX_NODES or path not in ops or path in seen:
            raise BridgeUnavailable("phase plan/forecast path mismatch or depth")
        seen.add(path)
        op, vector = ops[path], list(vectors[path])
        kind, children = op["node_type"], node.get("Plans", [])
        if kind != node.get("Node Type"):
            raise BridgeUnavailable("phase node type differs from innovation 2")
        if any(c.get("Parent Relationship") in ("InitPlan", "SubPlan") for c in children):
            raise BridgeUnavailable("InitPlan/SubPlan execution multiplicity not phase-qualified")
        if kind in LEAVES and not children:
            if kind in ("Cte Scan", "CTE Scan", "Bitmap Heap Scan", "Bitmap Index Scan"):
                raise BridgeUnavailable("shared CTE/bitmap producer dependencies not phase-qualified")
            stage = phase(kind, 1.0)
            add(stage, path, vector)
            return [stage]
        if kind == "Hash Join":
            if len(children) != 2 or children[1].get("Node Type") != "Hash":
                raise BridgeUnavailable("Hash Join must have explicit inner Hash")
            hash_path = path + ".1"
            if hash_path not in vectors:
                raise BridgeUnavailable("missing hash build work")
            # Hash.demand describes build spill; Hash Join owns the temp totals.
            # Move ONLY initial inner writes to build; never add both totals.
            build_write = min(vector[-1], max(
                0.0, ops[hash_path]["details"].get("write_bytes", 0.0)
                * ops[hash_path]["calls"] / 1024**3))
            vectors[hash_path][-1] += build_write
            vector[-1] -= build_write
            inner = visit(children[1], hash_path, depth + 1)
            for stage in inner:
                stage["output_fraction"] = 0.0
            outer = visit(children[0], path + ".0", depth + 1)
            prefix = []
            join_type = node.get("Join Type", "Inner")
            prefetch = (join_type not in ("Right", "Full", "Right Anti", "Right Anti Full")
                        and (join_type in ("Left", "Anti", "Left Anti Full")
                             or children[0].get("Startup Cost", math.inf)
                             < children[1].get("Total Cost", -math.inf)))
            if prefetch:
                # nodeHashjoin.cpp HJ_BUILD_HASHTABLE fetches one OUTER tuple
                # before MultiExecHash. Blocking outer dependencies run first.
                index = next(i for i, s in enumerate(outer) if s["output_fraction"] > 0)
                prefix, remaining = outer[:index], outer[index:]
                first = remaining[0]
                rows = ops[path + ".0"]["output_rows_per_call"]
                fraction_first = min(1.0, 1.0 / max(1.0, rows * first["output_fraction"]))
                gate = phase("Hash Join outer first-tuple gate")
                tail = phase(first["label"], first["output_fraction"] * (1 - fraction_first))
                for owner, contribution in first["contributions"].items():
                    add(gate, owner, scaled(contribution, fraction_first))
                    add(tail, owner, scaled(contribution, 1 - fraction_first))
                tail["ends"] = first["ends"]
                if fraction_first == 1.0:
                    gate["ends"], tail["ends"] = list(first["ends"]), []
                gate["ends"].append({"path": path, "kind": "Outer first tuple before Hash build"})
                gate["timing_uncertainty"] = {
                    "kind": "unknown_physical_filter_prefix_and_synchronized_scan_start",
                    "point_assumption": "uniform qualifying rows, NOT measured first-match position",
                    "prefix_vector_lower": zero(), "prefix_vector_upper": first["vector"],
                }
                timing_uncertainties.append({"path": path, **gate["timing_uncertainty"]})
                for stage in prefix:
                    stage["output_fraction"] = 0.0
                prefix.append(gate)
                cached = phase("Cached outer first tuple", first["output_fraction"] * fraction_first)
                outer = [cached, tail] + remaining[1:]
            batches = max(1.0, float(op["details"].get("batches", 1)))
            fraction = 1.0 / batches
            initial = scaled(vector, fraction)
            initial[-1], initial[-2] = vector[-1], 0.0
            distribute(outer, path, initial)
            for stage in outer:
                stage["output_fraction"] *= fraction
            result = prefix + inner + outer
            if batches > 1:
                result[-1]["ends"].append({"path": path, "kind": "Hash probe first batch"})
                replay_vector = [v - i for v, i in zip(vector, initial)]
                replay = phase("Hash batch replay", 1 - fraction)
                add(replay, path, replay_vector)
                replay["ends"].append({"path": path, "kind": "Hash batch replay"})
                result.append(replay)
            return result
        if len(children) != 1:
            # Avoid hiding partial graphs behind a zero-demand forecast.
            raise BridgeUnavailable("unsupported phase topology: " + kind)
        stages = visit(children[0], path + ".0", depth + 1)
        if kind in STREAMING or kind == "Aggregate" and op.get("strategy") == "Sorted":
            distribute(stages, path, vector)
            return stages
        if kind == "Hash":
            distribute(stages, path, vector)
            barrier(stages, path, "Hash build")
            stages.append(phase("Hash build ready", 1.0))
            return stages
        if kind == "Sort":
            # Run writes occur while consuming. Comparisons/merge work is
            # conservatively at sort completion; final on-demand merge timing
            # is not identifiable from a total-IO estimate alone.
            writes = zero()
            writes[-1] = vector[-1]
            distribute(stages, path, writes)
            for stage in stages:
                stage["output_fraction"] = 0.0
            finish = phase("Sort finish")
            vector[-1] = 0.0
            output = zero()
            output[2], vector[2] = vector[2], 0.0
            add(finish, path, vector)
            finish["ends"].append({"path": path, "kind": "Sort"})
            stages.append(finish)
            emit = phase("Sort output", 1.0)
            add(emit, path, output)
            stages.append(emit)
            if op["temp_read_bytes"] > 0:
                assumptions.append("Sort %s: total merge-read work at finish; lazy final merge is approximate" % path)
            return stages
        if kind in ("Aggregate", "HashAggregate"):
            strategy = op.get("strategy")
            if kind != "HashAggregate" and strategy not in ("Hashed", "Plain"):
                raise BridgeUnavailable("aggregate strategy not phase-qualified")
            groups = max(1.0, op["features"]["aggregate_groups"])
            spilling = op["temp_read_bytes"] > 0
            fraction = (min(1.0, max(0.0, op["details"].get("resident_groups", groups) / groups))
                        if spilling else 1.0)
            consume, emit = list(vector), zero()
            # Group finalization, HAVING/projection and aggregate result work.
            emit[2] = op["features"]["projection_evaluations"] / 1e6
            consume[2] -= emit[2]
            emit[1], consume[1] = consume[1], 0.0  # aggregate HAVING, not input scan
            group_units = op["features"]["aggregate_groups"] / 1e6
            emit[4] = min(group_units, consume[4])
            consume[4] -= emit[4]
            replay = zero()
            if spilling:
                # Transitions/typed arithmetic on spilled rows happen on
                # replay. Hash lookup proxy stays on initial input pass.
                for i in (4, 8, 9):
                    replay[i] = consume[i] * (1 - fraction)
                    consume[i] *= fraction
                replay[-2], consume[-2] = consume[-2], 0.0
            distribute(stages, path, consume)
            barrier(stages, path, "Hash aggregate consume" if strategy != "Plain" else "Blocking aggregate")
            first = phase("Aggregate resident output", fraction)
            add(first, path, scaled(emit, fraction))
            stages.append(first)
            if spilling:
                replay = plus(replay, scaled(emit, 1 - fraction))
                later = phase("Hash aggregate spill replay", 1 - fraction)
                add(later, path, replay)
                later["ends"].append({"path": path, "kind": "Hash aggregate spill replay"})
                stages.append(later)
            return stages
        raise BridgeUnavailable("unsupported phase operator: " + kind)

    stages = []
    try:
        if unsupported:
            raise BridgeUnavailable("rescanned operators require lifecycle extension")
        stages = visit(root)
        if seen != set(ops):
            raise BridgeUnavailable("unvisited operator work")
        if len(stages) > MAX_PHASES:
            raise BridgeUnavailable("phase count budget")
        startup = zero()
        startup[0] = 1.0
        add(stages[0], "@query-startup", startup)
        total = [sum(s["vector"][i] for s in stages) for i in range(N)]
        if not all(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8)
                   for a, b in zip(total, work["vector"])):
            raise BridgeUnavailable("phase work conservation failed")
        for i, stage in enumerate(stages):
            stage["id"] = i
        complete = True
    except BridgeUnavailable as exc:
        unsupported.append({"reason": str(exc)})
        stages, total, complete = [], None, False
    return {
        "schema": SCHEMA, "basis": list(BASIS), "stages": stages,
        "operator_count": len(ops), "phase_count": len(stages),
        "phase_topology_complete": complete, "unsupported": unsupported,
        "total_vector": total, "innovation_2_vector": work["vector"],
        "innovation_2_work_conserved": complete,
        "plan_structure_sha256": forecast["plan_structure_sha256"],
        "work_mem_mib": forecast["work_mem_mib"],
        "physical_IO_estimate": None, "physical_IO_qualified": False,
        "phase_timing_accuracy_qualified": False,
        "phase_timing_inputs_complete": complete and not timing_uncertainties,
        "automatic_recommendation_qualified": False,
        "timing_uncertainties": timing_uncertainties,
        "assumptions": list(dict.fromkeys(assumptions)),
        "compute_process_cpu_ns": time.process_time_ns() - started,
    }


def cost_phases(program, cpu_coefficients, wait_coefficients, *, basis=None):
    """Explicitly calibrated cost only; caller must validate machine binding.

    Wait coefficients are empirical non-CPU seconds/work, NOT a device IO
    completion model. Existing whole-query CPU coefficients are often not
    identifiable per operator and must be independently phase-validated.
    """
    if not program["phase_topology_complete"]:
        raise BridgeUnavailable("incomplete phase program")
    if basis is not None and list(basis) != program["basis"]:
        raise ValueError("phase cost coefficient basis/order mismatch")
    for values in (cpu_coefficients, wait_coefficients):
        if len(values) != N or not all(_number(v) for v in values):
            raise ValueError("finite nonnegative cost vector required")
    stages = []
    for stage in program["stages"]:
        c = sum(a * b for a, b in zip(stage["vector"], cpu_coefficients))
        w = sum(a * b for a, b in zip(stage["vector"], wait_coefficients))
        stages.append({
            "id": stage["id"], "label": stage["label"], "ends": stage["ends"],
            "cpu_seconds": c, "non_CPU_seconds_proxy": w,
            "isolated_seconds_proxy": c + w,
            "logical_scan_bytes": stage["vector"][-3] * 1024**3,
            "logical_temp_read_bytes": stage["vector"][-2] * 1024**3,
            "logical_temp_write_bytes": stage["vector"][-1] * 1024**3,
            "timing_uncertainty": stage.get("timing_uncertainty"),
        })
    return {
        "schema": "jmt.costed-operator-phases/v1", "stages": stages,
        "physical_IO_qualified": False, "phase_timing_accuracy_qualified": False,
        "automatic_recommendation_qualified": False,
    }


def forecast_window(programs, seconds, *, cpu_capacity, TP_reserved_cores=0.0,
                    cursors=None, max_events=2048):
    """Fluid progress, next phase-completion events only; independent of N rows.

    Caller can persist cursors between windows. No future measured progress is
    accepted; optional online boundary anchoring belongs to the caller and must
    be time/identity checked before prediction. CPU reservation is conditional,
    not itself an end-to-end TPS prediction.
    """
    for value in (seconds, cpu_capacity, TP_reserved_cores):
        if not _number(value):
            raise ValueError("finite nonnegative window/capacity required")
    if not 0 < seconds <= 3600 or cpu_capacity <= 0 or TP_reserved_cores > cpu_capacity:
        raise ValueError("invalid fluid window/capacity")
    if not 1 <= len(programs) <= 128 or not 1 <= max_events <= 16384:
        raise ValueError("fluid program/event budget")
    state = {}
    for key, program in programs.items():
        stages = program["stages"]
        if len(stages) > MAX_PHASES:
            raise ValueError("phase count budget")
        for stage in stages:
            for field in ("cpu_seconds", "non_CPU_seconds_proxy",
                          "logical_scan_bytes", "logical_temp_read_bytes",
                          "logical_temp_write_bytes"):
                if not _number(stage[field]):
                    raise ValueError("invalid phase demand")
        supplied = (cursors or {}).get(key, {"index": 0, "fraction_done": 0.0})
        index, fraction = supplied["index"], supplied["fraction_done"]
        if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= len(stages)
                or not _number(fraction) or fraction >= 1
                or index == len(stages) and fraction != 0):
            raise ValueError("invalid phase cursor")
        state[key] = {"index": index, "fraction_done": fraction,
                      "cpu_seconds": 0.0, "logical_scan_bytes": 0.0,
                      "logical_temp_read_bytes": 0.0, "logical_temp_write_bytes": 0.0}
    elapsed, events, trace = 0.0, 0, []
    available = cpu_capacity - TP_reserved_cores
    while elapsed < seconds - 1e-12:
        events += 1
        if events > max_events:
            raise BridgeUnavailable("fluid boundary-event budget exhausted")
        active = {}
        for key, row in state.items():
            stages = programs[key]["stages"]
            while row["index"] < len(stages):
                stage = stages[row["index"]]
                duration = stage["cpu_seconds"] + stage["non_CPU_seconds_proxy"]
                if duration > 1e-12:
                    break
                for field in ("logical_scan_bytes", "logical_temp_read_bytes", "logical_temp_write_bytes"):
                    row[field] += stage[field] * (1 - row["fraction_done"])
                row["index"] += 1
                row["fraction_done"] = 0.0
            if row["index"] < len(stages):
                active[key] = (stage, duration, stage["cpu_seconds"] / duration)
        if not active:
            break
        wanted = sum(v[2] for v in active.values())
        sharing = 1.0
        if wanted > available:
            # A bounded scalar fair-service solve, not a row/event simulation.
            # c/(c/s+w) is monotone in s; a naive available/wanted factor
            # exceeds the CPU budget whenever a phase also has IO wait.
            low, high = 0.0, 1.0
            for _ in range(40):
                middle = (low + high) / 2
                busy = sum(
                    stage["cpu_seconds"] / (
                        stage["cpu_seconds"] / middle + stage["non_CPU_seconds_proxy"])
                    for stage, _, _ in active.values() if stage["cpu_seconds"])
                if busy > available:
                    high = middle
                else:
                    low = middle
            sharing = low
        # CPU and wait are serial service, so only CPU service slows down.
        times = {
            key: (c / sharing if c and sharing else math.inf if c else 0.0)
                 + stage["non_CPU_seconds_proxy"]
            for key, (stage, duration, ratio) in active.items()
            for c in [stage["cpu_seconds"]]}
        dt = min([seconds - elapsed] + [
            times[key] * (1 - state[key]["fraction_done"]) for key in active])
        if not math.isfinite(dt) or dt <= 0:
            raise BridgeUnavailable("no finite fluid progress")
        completed = []
        for key, (stage, _, _) in active.items():
            row = state[key]
            fraction = min(1 - row["fraction_done"], dt / times[key])
            for field in ("cpu_seconds", "logical_scan_bytes",
                          "logical_temp_read_bytes", "logical_temp_write_bytes"):
                row[field] += stage[field] * fraction
            row["fraction_done"] += fraction
            if row["fraction_done"] >= 1 - 1e-10:
                completed.append({"query": key, "stage_id": row["index"], "ends": stage["ends"]})
                row["index"] += 1
                row["fraction_done"] = 0.0
        elapsed += dt
        trace.append({"elapsed_seconds": elapsed, "completed": completed})
    return {
        "schema": "jmt.operator-phase-window/v1", "window_seconds": seconds,
        "AP_CPU_seconds": sum(r["cpu_seconds"] for r in state.values()),
        "AP_average_CPU_cores": sum(r["cpu_seconds"] for r in state.values()) / seconds,
        "queries": state,
        "next_cursors": {k: {"index": r["index"], "fraction_done": r["fraction_done"]}
                         for k, r in state.items()},
        "boundary_event_count": events, "trace": trace,
        "TP_reserved_cores": TP_reserved_cores,
        "conditional_CPU_reservation_not_end_to_end_TPS": True,
        "physical_IO_completions": None,
        "automatic_recommendation_qualified": False,
    }
