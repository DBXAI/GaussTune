"""Bind observed workload features to the strict JMT model.

For a stage signature (observed TP terminals, demanded AP queries) the
planner:

1. maps the observed terminal count to a calibrated TP domain (exact strict
   pipeline config for that N; no extrapolation across N);
2. evaluates the strict pipeline (``pipeline.evaluate_bundle``) for the AP
   set, with running queries pinned to the work_mem they started with;
3. applies the TP-target / AP-latency objective and FIFO admission from
   ``objective.plan_admission``.

Evaluations are deterministic functions of the frozen evidence and stage
inputs, so they are cached by content hash.  The cache never stores or
adjusts anything observed during validation runs.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from jmt.innovation_3_search.objective import POLICY, plan_admission
from jmt.common.provenance import sha256
from jmt.innovation_3_search.workload_monitor import map_terminal_domain

CONFIG_SCHEMA = "huawei7.jmt-controller-config/v1"
PLAN_SCHEMA = "huawei7.jmt-plan/v1"

Evaluate = Callable[[Mapping[str, object]], Mapping[str, object]]


def _default_evaluate(config: Mapping[str, object]) -> Mapping[str, object]:
    from jmt.innovation_3_search.pipeline import evaluate_bundle

    return evaluate_bundle(config)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class JmtPlanner:
    def __init__(
        self, config: Mapping[str, object], *,
        evaluate: Optional[Evaluate] = None,
        cache_dir: Optional[Path] = None,
    ) -> None:
        if config.get("schema") != CONFIG_SCHEMA:
            raise ValueError("unsupported JMT controller config")
        self.config = config
        self.target_tps = float(config.get("target_tps", 0))
        if not self.target_tps > 0:
            raise ValueError("target_tps must be set by the user and positive")
        self.tolerance = int(config.get("terminal_match_tolerance", 0))
        selection = config.get("selection", {})
        if not isinstance(selection, dict):
            raise ValueError("selection must be an object")
        self.selection = dict(selection)
        self.evaluate_config = evaluate or _default_evaluate
        self.cache_dir = cache_dir
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: Dict[str, Mapping[str, object]] = {}
        self.domains: Dict[int, Tuple[Dict[str, object], str, float]] = {}
        rows = config.get("tp_domains")
        if not isinstance(rows, list) or not rows:
            raise ValueError("tp_domains must list calibrated TP topologies")
        bundle_paths = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("tp_domains entries must be objects")
            path = Path(str(row["pipeline_config"]))
            pipeline_config = json.loads(path.read_text(encoding="utf-8"))
            if pipeline_config.get("schema") != "huawei7.pipeline-config/v1":
                raise ValueError("tp domain must reference a strict pipeline config")
            stage = pipeline_config.get("stage")
            if not isinstance(stage, dict):
                raise ValueError("strict pipeline config lacks stage topology")
            terminals = int(row["terminals"])
            if int(stage.get("tp_terminals", -1)) != terminals:
                raise ValueError(
                    "tp domain N=%d differs from its pipeline stage N=%s"
                    % (terminals, stage.get("tp_terminals"))
                )
            if terminals in self.domains:
                raise ValueError("duplicate TP domain N=%d" % terminals)
            target = float(row.get("target_tps", self.target_tps))
            if not target > 0:
                raise ValueError("domain target_tps must be positive")
            self.domains[terminals] = (pipeline_config, sha256(path), target)
            bundle_paths.add(str(Path(str(pipeline_config["ap_model_bundle"])).resolve()))
        if len(bundle_paths) != 1:
            raise ValueError("all TP domains must share one AP model bundle")
        bundle = json.loads(Path(next(iter(bundle_paths))).read_text(encoding="utf-8"))
        hashes = bundle.get("query_sha256")
        if not isinstance(hashes, dict) or not hashes:
            raise ValueError("AP model bundle lacks bound query SHA-256 values")
        self.query_hashes = {str(key): str(value) for key, value in hashes.items()}
        self.ap_bundle_path = next(iter(bundle_paths))

    @property
    def calibrated_terminals(self) -> Tuple[int, ...]:
        return tuple(sorted(self.domains))

    def domain_for(self, observed_terminals: int) -> Optional[int]:
        return map_terminal_domain(
            observed_terminals, self.calibrated_terminals, self.tolerance,
        )

    def _stage_config(
        self, terminals: int, queries: Sequence[int], fixed: Mapping[int, int],
    ) -> Tuple[Dict[str, object], str]:
        base, base_sha, target = self.domains[terminals]
        config = copy.deepcopy(base)
        stage = dict(config["stage"])  # type: ignore[arg-type]
        stage["ap_queries"] = sorted(int(query) for query in queries)
        stage["allow_tp_only"] = True
        stage["fixed_work_mem"] = {
            str(query): int(memory) for query, memory in sorted(fixed.items())
        }
        if "sb_sample_count" in self.selection:
            stage["sb_sample_count"] = int(self.selection["sb_sample_count"])
        config["stage"] = stage
        config["selection_policy"] = POLICY
        config["target_tps"] = target
        config["sb_interval_policy"] = str(
            self.selection.get("sb_interval_policy", "ppt-bounded")
        )
        config["sb_refine_rounds"] = int(self.selection.get("sb_refine_rounds", 1))
        config["sb_refine_change_fraction"] = float(
            self.selection.get("sb_refine_change_fraction", 0.02)
        )
        key = _canonical_digest({
            "pipeline_config_sha256": base_sha,
            "stage": stage,
            "selection_policy": POLICY,
            "target_tps": target,
            "sb_interval_policy": config["sb_interval_policy"],
            "sb_refine_rounds": config["sb_refine_rounds"],
            "sb_refine_change_fraction": config["sb_refine_change_fraction"],
        })
        return config, key

    def evaluate(
        self, terminals: int, queries: Sequence[int], fixed: Mapping[int, int],
    ) -> Tuple[Mapping[str, object], Mapping[str, object]]:
        """Strict-model evaluation of one AP set; raises when out of domain."""

        config, key = self._stage_config(terminals, queries, fixed)
        summary = self._memory_cache.get(key)
        cache_path = None if self.cache_dir is None else self.cache_dir / (key + ".json")
        if summary is None and cache_path is not None and cache_path.is_file():
            summary = json.loads(cache_path.read_text(encoding="utf-8"))
        if summary is None:
            try:
                result = self.evaluate_config(config)
            except (RuntimeError, ValueError) as error:
                summary = {"error": str(error)}
            else:
                summary = {
                    "best": result["best"],
                    "selection": result["selection"],
                    "sb_interval": result.get("sb_interval"),
                    "valid_candidate_count": result.get("valid_candidate_count"),
                    "candidate_count": result.get("candidate_count"),
                }
            summary = dict(summary)
            summary.update({
                "schema": "huawei7.jmt-evaluation-cache/v1",
                "cache_key": key,
                "pipeline_config_sha256": self.domains[terminals][1],
                "tp_terminals": terminals,
                "ap_queries": sorted(int(query) for query in queries),
                "fixed_work_mem": {
                    str(query): int(memory) for query, memory in sorted(fixed.items())
                },
            })
            if cache_path is not None:
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
                temporary.replace(cache_path)
        self._memory_cache[key] = summary
        if "error" in summary:
            raise RuntimeError(str(summary["error"]))
        return summary["best"], summary["selection"]  # type: ignore[return-value]

    def plan(
        self, *, observed_terminals: int, running: Mapping[int, int],
        waiting: Sequence[int],
    ) -> Dict[str, object]:
        domain = self.domain_for(observed_terminals)
        base = {
            "schema": PLAN_SCHEMA,
            "observed_tp_terminals": int(observed_terminals),
            "calibrated_tp_terminals": list(self.calibrated_terminals),
            "running": {str(query): int(memory) for query, memory in sorted(running.items())},
            "waiting": [int(query) for query in waiting],
        }
        if domain is None:
            base.update({
                "status": "out_of_domain",
                "reason": "observed TP terminal count has no calibrated model",
                "admitted": [], "queued": [int(query) for query in waiting],
            })
            return base
        target = self.domains[domain][2]
        result = plan_admission(
            running=running, waiting=waiting,
            evaluate=lambda queries, fixed: self.evaluate(domain, queries, fixed),
        )
        selected = result["selected"]
        base.update(result)
        base.update({
            "status": "planned" if selected is not None else "no_candidate",
            "domain_tp_terminals": domain,
            "target_tps": target,
            "pipeline_config_sha256": self.domains[domain][1],
        })
        if selected is not None:
            work_mem = {
                int(query): int(memory)
                for query, memory in selected.get("work_mem", ())  # type: ignore[union-attr]
            }
            base["shared_buffers_mb"] = int(selected["shared_buffers_mb"])  # type: ignore[index]
            base["work_mem"] = {str(query): memory for query, memory in sorted(work_mem.items())}
            base["predicted_tps"] = float(selected["predicted_tps"])  # type: ignore[index]
            base["predicted_ap_latency_seconds"] = selected.get("ap_execution_seconds")  # type: ignore[union-attr]
        return base
