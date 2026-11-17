"""Sequential target-label replacements. NOT an online prediction entrypoint."""
import math

from .observations import cardinalities, exclusive_temp
from .labels import nodes, shape


def discrepancy(predicted, observed, relative_tolerance=.20, absolute_tolerance=0):
    if predicted is None or observed is None:
        return {"status": "unverifiable", "reason": "missing_prediction_or_observation"}
    if any(not math.isfinite(v) or v < 0 for v in (predicted, observed)):
        return {"status": "unverifiable", "reason": "invalid_numeric_value"}
    error = predicted - observed
    passed = abs(error) <= max(absolute_tolerance, relative_tolerance * observed)
    return {
        "status": "within_tolerance" if passed else "inaccurate",
        "predicted": predicted, "observed": observed, "signed_error": error,
        "relative_error": error / observed if observed else None,
    }


def spill_comparison(prediction, label):
    result = {}
    for direction in ("read", "write"):
        result[direction] = discrepancy(
            prediction["logical_temp_%s_bytes_estimate" % direction],
            label[direction + "_bytes"], absolute_tolerance=8192)
    result["complete"] = prediction["logical_temp_estimate_complete"]
    result["status"] = (
        "unverifiable" if not result["complete"] else
        "inaccurate" if any(result[d]["status"] == "inaccurate" for d in ("read", "write"))
        else "within_tolerance")
    return result


def diagnose_ap(session, blind, actual, wm, frozen_prediction, label):
    if shape(blind[0]["Plan"]) != shape(actual[0]["Plan"]):
        raise ValueError("POSTEXECUTION replacements require identical plan topology")
    observations = cardinalities(actual[0]["Plan"])
    actual_nodes = dict(nodes(actual[0]["Plan"]))
    operators = {op["path"]: op for op in frozen_prediction["operators"]}
    replacements, facts, comparisons, history = {}, {}, [], []
    history.append({"step": "frozen_blind_forecast",
                    "spill": spill_comparison(frozen_prediction, label)})
    stages = ("base_scan_rows", "join_rows", "aggregate_and_remaining_rows")
    latest = frozen_prediction
    for stage in stages:
        changed = []
        for item in observations:
            kind, path = item["node_type"], item["path"]
            category = (
                "base_scan_rows" if kind.endswith("Scan") else
                "join_rows" if "Join" in kind or kind == "Nested Loop" else
                "aggregate_and_remaining_rows")
            if category != stage:
                continue
            predicted = operators[path]["output_rows_per_call"]
            check = discrepancy(predicted, item["actual_rows_per_loop"])
            if not item["comparable"]:
                check = {"status": "unverifiable", "reason": item["abstention"]}
            check.update(item)
            comparisons.append(check)
            if check["status"] == "inaccurate":
                replacements[path] = item["actual_rows_per_loop"]
                changed.append(path)
            # Actual Loops is an independent upstream invocation observation.
            if item["comparable"] and operators[path]["calls"] != item["actual_loops"]:
                facts.setdefault(path, {})["diagnostic_actual_calls"] = item["actual_loops"]
            if item["comparable"] and item.get("pre_having_groups") is not None:
                facts.setdefault(path, {})["diagnostic_pre_having_groups"] = item[
                    "pre_having_groups"]
        latest = session._evaluate(blind, wm, diagnostic={"rows": replacements, "facts": facts})
        history.append({
            "step": stage, "new_inaccurate_paths_replaced": changed,
            "cumulative_rows_replaced": dict(replacements),
            "derived_pass_through_effects": latest["diagnostic_pass_through_row_effects"],
            "spill": spill_comparison(latest, label),
        })
    # Also measure the local operator kernel with ALL eligible N/G exact, not
    # just estimates that exceeded the 20% audit tolerance.
    all_rows = {o["path"]: o["actual_rows_per_loop"] for o in observations if o["comparable"]}
    all_facts = {
        o["path"]: {"diagnostic_actual_calls": o["actual_loops"]}
        for o in observations if o["comparable"]}
    for observation in observations:
        if observation["comparable"] and observation.get("pre_having_groups") is not None:
            all_facts[observation["path"]]["diagnostic_pre_having_groups"] = observation[
                "pre_having_groups"]
    oracle = session._evaluate(blind, wm, diagnostic={"rows": all_rows, "facts": all_facts})
    own = exclusive_temp(actual[0]["Plan"])
    operator_checks = []
    for op in oracle["operators"]:
        obs = own[op["path"]]
        row = {
            "path": op["path"], "node_type": op["node_type"], "mode": op["mode"],
            "actual_self_counter": obs, "uncertainties": op["uncertainties"],
        }
        for direction in ("read", "write"):
            row[direction] = discrepancy(
                op["temp_%s_bytes" % direction],
                obs.get(direction + "_bytes") if obs["valid"] else None,
                absolute_tolerance=8192)
        actual_node = actual_nodes[op["path"]]
        measured_peak, memory_source = None, None
        if op["node_type"] == "Hash" and "Peak Memory Usage" in actual_node:
            measured_peak, memory_source = actual_node["Peak Memory Usage"] / 1024, "Hash spacePeak KiB"
        elif op["node_type"] == "Sort" and actual_node.get("Sort Space Type") == "Memory":
            measured_peak, memory_source = actual_node["Sort Space Used"] / 1024, "Sort memory KiB"
        row["reported_memory"] = discrepancy(
            op["details"].get("executor_reported_peak_mib_estimate", op["memory_mib"]),
            measured_peak, absolute_tolerance=.0625)
        row["memory_label_source"] = memory_source
        row["structural_memory_estimate_is_NOT_physical_truth"] = op["memory_mib"]
        if op["node_type"] == "Hash" and "Hash Batches" in actual_node:
            row["hash_batches"] = discrepancy(op["details"]["batches"], actual_node["Hash Batches"],
                                              relative_tolerance=0)
        operator_checks.append(row)
    return {
        "schema": "jmt.POSTEXECUTION-stepwise-ap/v1",
        "qualification": (
            "CUMULATIVE TARGET-LABEL REPLACEMENT DIAGNOSTIC ONLY. Predictions, "
            "observations and conditioned kernels are separate. Oracle scores are "
            "NOT deployable accuracy, new holdout scores or fitted corrections. "
            "Planner hash numGroups, input_rows/width and initial buckets/batches stay "
            "BLIND even when actual volume is replaced. Semantic pass-through nodes "
            "carry corrected child rows, but joins are never magically recomputed."),
        "row_comparisons": comparisons, "cumulative_failure_replacement": history,
        "all_eligible_rows_oracle": {
            "spill": spill_comparison(oracle, label),
            "replacement_rows": all_rows,
            "operator_self_IO_checks": operator_checks,
            "unknown_inputs_NOT_replaced": [
                "actual tuple widths", "pre-HAVING groups without removed-row counter",
                "group-frequency histogram and input ordering", "actual per-operator memory grant",
                "operator physical memory peak", "physical device requests", "AP end-to-end runtime",
            ],
        },
        "next_step_logical_IO_replacement": {
            "read_bytes": label["read_bytes"], "write_bytes": label["write_bytes"],
            "source": "same execution whole-tree inclusive temp buffer counters",
            "next_physical_requests": None, "next_runtime_seconds": None,
            "status": "unverifiable_downstream_requires_independent_physical_IO_and_runtime_model",
        },
        "automatic_recommendation_qualified": False,
    }
